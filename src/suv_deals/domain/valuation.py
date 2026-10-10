"""Valuation assembly, dependency fingerprint, state and invalidation (spec sections 14 and 18).

One valuation references a specific listing revision, comparable set, tax rule set, FX
observations, cost profile/inputs and configuration revision. Its
``dependency_fingerprint`` is the sha256 of exactly those ids/versions/hashes, so any
dependency change is detectable even when the listing semantic hash is unchanged.

States (spec 14): ``not_started`` | ``incomplete`` | ``estimated`` | ``quote_supported``
| ``stale`` | ``invalid``.

- ``incomplete``: a material input is unknown (tax, costs, proceeds, eligibility facts).
  Such a valuation is a ``research_candidate`` with unknown costs: never a quantified
  opportunity, and it carries no contribution figures (unknown is never zero).
- ``estimated``: complete scenarios; some cost inputs are estimates.
- ``quote_supported``: complete; every material cost line is quoted/actual or computed by
  an ACTIVE approved tax rule.
- ``stale``: a dependency changed or a freshness deadline passed (quote expiry, FX age,
  comparable freshness, tax rule end). Contribution figures stay for audit.
- ``invalid``: the listing failed deterministic screening; terminal.

A non-fixture valuation never uses an unapproved/fixture tax rule: import costs are then
unknown. Fixture inputs produce a fixture valuation that can never notify. The scenario
import lines must come from the recorded tax calculation (``tax_calculation:<sha256>``
evidence), so the fingerprinted rule is the one the figures used.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.costs import (
    CONTRIBUTION_LABEL,
    TAX_CALCULATION_EVIDENCE_PREFIX,
    CostProfileRef,
    ScenarioSet,
    tax_calculation_evidence_id,
)
from suv_deals.domain.enums import (
    EligibilityState,
    FxPurpose,
    ProfileKey,
    ScenarioName,
    TaxRuleStatus,
    ValuationState,
)
from suv_deals.domain.listings import sha256_json
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.tax_engine import RuleSet, TaxCalculation
from suv_deals.errors import ValidationFailed

#: Bump whenever valuation/cost/tax arithmetic semantics change; part of the fingerprint.
CALCULATION_VERSION: Final = "valuation/1.0.0"

_HEX64: Final = r"^[0-9a-f]{64}$"
_CLOSED_TAX_STATUSES: Final = frozenset(
    {TaxRuleStatus.EXPIRED, TaxRuleStatus.REVOKED, TaxRuleStatus.SUPERSEDED}
)
_FIGURE_STATES: Final = frozenset({ValuationState.ESTIMATED, ValuationState.QUOTE_SUPPORTED})


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InvalidationReason(StrEnum):
    """Why a valuation must be rebuilt (spec 18 "Invalidation and material changes")."""

    LISTING_REVISION = "listing_revision"
    COMPARABLES = "comparables"
    FX = "fx"
    COST_QUOTE = "cost_quote"
    COST_PROFILE = "cost_profile"
    TAX_RULE = "tax_rule"
    CONFIG = "config"
    EVIDENCE = "evidence"
    FRESHNESS_DEADLINE = "freshness_deadline"
    CALCULATION_VERSION = "calculation_version"


class ScreeningInput(_Contract):
    """The part of a screening result a valuation depends on."""

    eligibility: EligibilityState
    profile_key: ProfileKey | None = None
    eur_payable: Money | None = None  # unrounded EUR equivalent of the full payable amount
    reasons: tuple[str, ...] = Field(default=(), max_length=50)
    is_fixture: bool = False

    @field_validator("eur_payable")
    @classmethod
    def _eur(cls, value: Money | None) -> Money | None:
        if value is not None and value.currency != "EUR":
            raise ValueError("eur_payable must be in EUR")
        return value


class ComparableReference(_Contract):
    """Reference to the MK comparable set used for proceeds (spec 15)."""

    comparable_set_id: str = Field(min_length=1, max_length=100)
    content_hash: str = Field(pattern=_HEX64)
    sample_size: int = Field(ge=0)
    quality: Literal["adequate", "small", "insufficient_comparables"]
    fresh_until: datetime | None = None
    is_fixture: bool = False

    @field_validator("fresh_until")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @staticmethod
    def quality_from_status(status: str) -> Literal["adequate", "small", "insufficient_comparables"]:
        """Map domain.comparables status (and the persisted sample_quality) to this literal.

        comparables: adequate | small_sample | insufficient_comparables
        app.comparable_sets.sample_quality: adequate | small | insufficient
        """
        mapping: dict[str, Literal["adequate", "small", "insufficient_comparables"]] = {
            "adequate": "adequate",
            "small_sample": "small",
            "small": "small",
            "insufficient_comparables": "insufficient_comparables",
            "insufficient": "insufficient_comparables",
        }
        try:
            return mapping[status]
        except KeyError as exc:
            raise ValueError(f"unknown comparable status {status!r}") from exc


class TaxRuleDependency(_Contract):
    rule_set_id: str
    version: str
    sha256: str | None
    status: TaxRuleStatus
    valid_from: date | None
    valid_to: date | None
    is_fixture: bool

    @classmethod
    def from_calculation(cls, calc: TaxCalculation) -> TaxRuleDependency:
        return cls(
            rule_set_id=calc.rule_set_id,
            version=calc.version,
            sha256=calc.rule_sha256,
            status=calc.rule_status,
            valid_from=calc.rule_valid_from,
            valid_to=calc.rule_valid_to,
            is_fixture=calc.is_fixture,
        )

    @classmethod
    def from_rule_set(cls, rule_set: RuleSet) -> TaxRuleDependency:
        return cls(
            rule_set_id=rule_set.rule_set_id,
            version=rule_set.version,
            sha256=rule_set.sha256,
            status=rule_set.status,
            valid_from=rule_set.valid_from,
            valid_to=rule_set.valid_to,
            is_fixture=rule_set.is_fixture,
        )


class FxDependency(_Contract):
    base: str
    quote: str
    rate: str  # exact Decimal string
    rate_date: date
    provider: str
    purpose: FxPurpose

    @classmethod
    def from_rate(cls, rate: FxRate) -> FxDependency:
        return cls(
            base=rate.base,
            quote=rate.quote,
            rate=str(rate.rate),
            rate_date=rate.rate_date,
            provider=rate.provider,
            purpose=rate.purpose,
        )

    def key(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.base,
            self.quote,
            self.rate_date.isoformat(),
            self.provider,
            self.purpose.value,
            self.rate,
        )


class ValuationDependencies(_Contract):
    """Exactly what a valuation was computed from. ``fingerprint()`` hashes all of it."""

    calculation_version: str = CALCULATION_VERSION
    listing_revision_id: str = Field(min_length=1, max_length=100)
    comparable_set_id: str | None = None
    comparable_hash: str | None = None
    tax_rule: TaxRuleDependency | None = None
    fx: tuple[FxDependency, ...] = ()
    cost_profile: CostProfileRef | None = None
    cost_evidence_ids: tuple[str, ...] = ()
    cost_inputs_sha256: str | None = None
    config_revision_id: str = Field(min_length=1, max_length=100)
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _canonical(self) -> ValuationDependencies:
        if list(self.fx) != sorted(self.fx, key=FxDependency.key) or len({f.key() for f in self.fx}) != len(
            self.fx
        ):
            raise ValueError("fx dependencies must be sorted and unique (use build_dependencies)")
        for name in ("cost_evidence_ids", "evidence_ids"):
            values: tuple[str, ...] = getattr(self, name)
            if list(values) != sorted(set(values)):
                raise ValueError(f"{name} must be sorted and unique (use build_dependencies)")
        return self

    def fingerprint(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


def build_dependencies(
    *,
    listing_revision_id: str,
    config_revision_id: str,
    comparable: ComparableReference | None = None,
    tax: TaxCalculation | TaxRuleDependency | None = None,
    scenarios: ScenarioSet | None = None,
    fx_rates: Iterable[FxRate] = (),
    cost_profile: CostProfileRef | None = None,
    evidence_ids: Iterable[str] = (),
    calculation_version: str = CALCULATION_VERSION,
) -> ValuationDependencies:
    """Canonical dependency record (sorted, de-duplicated). FX includes the rates passed,
    the rates the scenarios actually used and the customs rate used by the tax engine."""
    rates: list[FxRate] = list(fx_rates)
    if scenarios is not None:
        rates.extend(scenarios.fx_rates_used)
    tax_dep: TaxRuleDependency | None
    if isinstance(tax, TaxCalculation):
        tax_dep = TaxRuleDependency.from_calculation(tax)
        if tax.customs_fx_rate is not None:
            rates.append(tax.customs_fx_rate)
    else:
        tax_dep = tax
    fx = {dep.key(): dep for dep in (FxDependency.from_rate(r) for r in rates)}
    return ValuationDependencies(
        calculation_version=calculation_version,
        listing_revision_id=listing_revision_id,
        comparable_set_id=None if comparable is None else comparable.comparable_set_id,
        comparable_hash=None if comparable is None else comparable.content_hash,
        tax_rule=tax_dep,
        fx=tuple(fx[k] for k in sorted(fx)),
        cost_profile=cost_profile,
        cost_evidence_ids=tuple(sorted(set(scenarios.evidence_ids))) if scenarios is not None else (),
        cost_inputs_sha256=None if scenarios is None else scenarios.inputs_sha256,
        config_revision_id=config_revision_id,
        evidence_ids=tuple(sorted(set(evidence_ids))),
    )


class Valuation(_Contract):
    """An assembled, reproducible valuation."""

    calculation_version: str = CALCULATION_VERSION
    listing_revision_id: str
    dependencies: ValuationDependencies
    dependency_fingerprint: str = Field(pattern=_HEX64)
    state: ValuationState
    research_candidate: bool
    is_fixture: bool
    currency: str
    screening: ScreeningInput
    comparable: ComparableReference | None
    tax: TaxCalculation | None
    scenarios: ScenarioSet | None
    base_contribution: Money | None
    conservative_contribution: Money | None
    upside_contribution: Money | None
    alert_eligible: bool
    unknowns: tuple[str, ...]
    warnings: tuple[str, ...]
    created_at: datetime
    expires_at: datetime | None
    stale_at: datetime | None = None
    stale_reason: str | None = Field(default=None, max_length=500)
    contribution_label: str = CONTRIBUTION_LABEL

    @field_validator("created_at", "expires_at", "stale_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _invariants(self) -> Valuation:
        if self.dependency_fingerprint != self.dependencies.fingerprint():
            raise ValueError("dependency_fingerprint does not match dependencies")
        figures = (self.base_contribution, self.conservative_contribution, self.upside_contribution)
        no_figures = (ValuationState.NOT_STARTED, ValuationState.INCOMPLETE, ValuationState.INVALID)
        if self.state in no_figures and any(f is not None for f in figures):
            raise ValueError(f"a {self.state.value} valuation carries no contribution figures")
        if self.state in _FIGURE_STATES and (
            self.base_contribution is None or self.conservative_contribution is None
        ):
            raise ValueError(f"a {self.state.value} valuation needs base and conservative contributions")
        if (self.state == ValuationState.STALE) != (
            self.stale_at is not None and self.stale_reason is not None
        ):
            raise ValueError("stale_at/stale_reason are set exactly when the state is stale")
        if self.alert_eligible and (self.is_fixture or self.state not in _FIGURE_STATES):
            raise ValueError(
                "only a current, non-fixture estimated/quote_supported valuation can be alert eligible"
            )
        return self

    @property
    def can_notify(self) -> bool:
        """Fixtures, stale/incomplete valuations and unapproved thresholds never notify."""
        return self.alert_eligible and not self.is_fixture and self.state in _FIGURE_STATES


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=UTC)


def _expiry(
    scenarios: ScenarioSet | None,
    comparable: ComparableReference | None,
    tax: TaxCalculation | None,
    fx: Sequence[FxDependency],
    fx_max_age_days: int,
) -> tuple[datetime | None, list[str]]:
    """Earliest of quote expiries, FX max-age deadlines, comparable freshness and tax rule end."""
    deadlines: list[tuple[datetime, str]] = []
    if scenarios is not None and scenarios.earliest_expiry is not None:
        deadlines.append((scenarios.earliest_expiry, "cost quote/estimate expiry"))
    for dep in fx:
        if dep.purpose != FxPurpose.CUSTOMS:
            when = _midnight(dep.rate_date + timedelta(days=fx_max_age_days + 1))
            deadlines.append((when, f"FX {dep.base}/{dep.quote} {dep.rate_date.isoformat()} exceeds max age"))
    if comparable is not None and comparable.fresh_until is not None:
        deadlines.append((comparable.fresh_until, "comparable freshness"))
    if tax is not None and tax.rule_valid_to is not None:
        deadlines.append(
            (_midnight(tax.rule_valid_to), f"tax rule {tax.rule_set_id}@{tax.version} validity end")
        )
    if not deadlines:
        return None, []
    earliest = min(d[0] for d in deadlines)
    return earliest, [why for when, why in deadlines if when == earliest]


def assemble_valuation(
    *,
    listing_revision_id: str,
    screening: ScreeningInput,
    comparable: ComparableReference | None,
    tax: TaxCalculation | None,
    scenarios: ScenarioSet | None,
    fx_rates: Sequence[FxRate],
    cost_profile: CostProfileRef | None,
    config_revision_id: str,
    as_of: datetime,
    evidence_ids: Sequence[str] = (),
    fx_max_age_days: int = 7,
    fixture_inputs: bool = False,
    tax_unavailable_reason: str | None = None,
) -> Valuation:
    """Assemble a valuation with state, unknowns, expiry and dependency fingerprint. Pure.

    ``fx_rates`` are the FX observations this valuation relies on beyond those the
    scenarios and the tax engine already report (e.g. the screening conversion). Each one
    is a fingerprinted dependency and a freshness deadline, so pass only rates actually
    used, never a whole rate history. When ``tax`` is given, the scenarios' import lines
    must come from exactly that calculation (``costs.tax_cost_lines``); otherwise the
    valuation is incomplete, because its figures would not match its recorded rule.
    """
    as_of = ensure_utc(as_of)
    deps = build_dependencies(
        listing_revision_id=listing_revision_id,
        config_revision_id=config_revision_id,
        comparable=comparable,
        tax=tax,
        scenarios=scenarios,
        fx_rates=fx_rates,
        cost_profile=cost_profile,
        evidence_ids=evidence_ids,
    )
    fixture = (
        fixture_inputs
        or screening.is_fixture
        or (comparable is not None and comparable.is_fixture)
        or (tax is not None and tax.is_fixture)
        or (cost_profile is not None and cost_profile.is_fixture)
    )
    unknowns: list[str] = []
    warnings: list[str] = []
    if fixture:
        warnings.append("FIXTURE_VALUATION: synthetic inputs; never shown as a real opportunity or notified")

    state: ValuationState
    if screening.eligibility == EligibilityState.REJECTED:
        state = ValuationState.INVALID
        warnings.append("listing rejected by deterministic screening; valuation not applicable")
    elif scenarios is None:
        state = ValuationState.NOT_STARTED
    else:
        unknowns.extend(
            _material_unknowns(
                screening,
                comparable,
                tax,
                scenarios,
                fixture=fixture,
                tax_unavailable_reason=tax_unavailable_reason,
            )
        )
        warnings.extend(_consistency_warnings(screening, comparable, scenarios))
        if unknowns:
            state = ValuationState.INCOMPLETE
        elif scenarios.material_support == "quote_supported":
            state = ValuationState.QUOTE_SUPPORTED
        else:
            state = ValuationState.ESTIMATED

    figures: dict[ScenarioName, Money | None] = dict.fromkeys(
        (ScenarioName.CONSERVATIVE, ScenarioName.BASE, ScenarioName.UPSIDE)
    )
    if state in _FIGURE_STATES and scenarios is not None:
        for name in figures:
            figures[name] = scenarios.scenario(name).contribution_before_business_tax
    research_candidate = state == ValuationState.INCOMPLETE
    alert_eligible = (
        state in _FIGURE_STATES
        and not fixture
        and scenarios is not None
        and scenarios.threshold.alert_eligible
        and tax is not None
        and tax.production_ready
    )

    expires_at: datetime | None = None
    expiry_reasons: list[str] = []
    if state not in (ValuationState.INVALID, ValuationState.NOT_STARTED):
        expires_at, expiry_reasons = _expiry(scenarios, comparable, tax, deps.fx, fx_max_age_days)
    stale_at: datetime | None = None
    stale_reason: str | None = None
    if expires_at is not None and expires_at <= as_of:
        stale_at = as_of
        stale_reason = f"{InvalidationReason.FRESHNESS_DEADLINE.value}: " + "; ".join(expiry_reasons)
        stale_reason = stale_reason[:500]
        state = ValuationState.STALE
        alert_eligible = False

    return Valuation(
        listing_revision_id=listing_revision_id,
        dependencies=deps,
        dependency_fingerprint=deps.fingerprint(),
        state=state,
        research_candidate=research_candidate,
        is_fixture=fixture,
        currency="EUR" if scenarios is None else scenarios.currency,
        screening=screening,
        comparable=comparable,
        tax=tax,
        scenarios=scenarios,
        base_contribution=figures[ScenarioName.BASE],
        conservative_contribution=figures[ScenarioName.CONSERVATIVE],
        upside_contribution=figures[ScenarioName.UPSIDE],
        alert_eligible=alert_eligible,
        unknowns=tuple(dict.fromkeys(unknowns)),
        warnings=tuple(dict.fromkeys(warnings)),
        created_at=as_of,
        expires_at=expires_at,
        stale_at=stale_at,
        stale_reason=stale_reason,
    )


def _material_unknowns(
    screening: ScreeningInput,
    comparable: ComparableReference | None,
    tax: TaxCalculation | None,
    scenarios: ScenarioSet,
    *,
    fixture: bool,
    tax_unavailable_reason: str | None,
) -> list[str]:
    unknowns: list[str] = []
    if screening.eligibility == EligibilityState.NEEDS_FACTS:
        unknowns.append(
            "eligibility: required listing facts are missing, conflicting or uncertain (needs_facts)"
        )
    if screening.eur_payable is None:
        unknowns.append("acquisition: EUR payable amount unknown")
    unknowns.extend(f"{u.item}: {u.label} ({u.reason})" for u in scenarios.unknown_lines)
    calc_sources = [s for s in scenarios.import_line_sources if s.startswith(TAX_CALCULATION_EVIDENCE_PREFIX)]
    if tax is None:
        unknowns.append(f"import tax: {tax_unavailable_reason or 'no applicable ACTIVE rule set'}")
        if calc_sources:
            unknowns.append("import tax: scenarios use a tax calculation that this valuation does not record")
    else:
        expected = tax_calculation_evidence_id(tax)
        if set(scenarios.import_line_sources) != {expected}:
            # The recorded calculation (and its fingerprint) must be what the figures used.
            stray = sorted(set(scenarios.import_line_sources) - {expected})
            unknowns.append(
                "import tax: scenario import lines do not all come from the recorded tax calculation "
                f"({', '.join(stray) or 'no import lines'})"
            )
        if not tax.complete:
            parts = [*tax.unknown_components, *(f"missing input {m}" for m in tax.missing_inputs)]
            unknowns.append(f"import tax incomplete ({tax.rule_set_id}@{tax.version}): {', '.join(parts)}")
        if not fixture and (tax.rule_status != TaxRuleStatus.ACTIVE or tax.is_fixture):
            unknowns.append(
                f"import tax rule {tax.rule_set_id}@{tax.version} is {tax.rule_status.value}, not ACTIVE: "
                "amounts are not production-supported"
            )
    insufficient = comparable is None or comparable.quality == "insufficient_comparables"
    if insufficient and scenarios.proceeds_basis == "mk_asking_prices":
        unknowns.append("proceeds: insufficient_comparables for an asking-price basis")
    if not scenarios.complete and not scenarios.unknown_lines:
        unknowns.append("scenarios incomplete")
    return unknowns


def _consistency_warnings(
    screening: ScreeningInput, comparable: ComparableReference | None, scenarios: ScenarioSet
) -> list[str]:
    warnings: list[str] = []
    if comparable is None or comparable.quality == "insufficient_comparables":
        warnings.append("INSUFFICIENT_COMPARABLES: targeted MK research needed")
    elif comparable.quality == "small":
        warnings.append(f"SMALL_COMPARABLE_SAMPLE: n={comparable.sample_size}; label as small")
    base = scenarios.scenario(ScenarioName.BASE)
    purchase = base.purchase_cash_outlay_excluding_deposits
    payable = screening.eur_payable
    if payable is not None and purchase is not None and scenarios.currency == "EUR" and purchase > payable:
        warnings.append("PURCHASE_ABOVE_SCREENED_PAYABLE: re-screen the listing at the modelled price")
    if scenarios.threshold.proposed_only:
        warnings.append("CONTRIBUTION_THRESHOLD_PROPOSED: EUR threshold is not owner-approved")
    return warnings


class StalenessCheck(_Contract):
    stale: bool
    reasons: tuple[InvalidationReason, ...]
    details: tuple[str, ...]


def is_stale(valuation: Valuation, current: ValuationDependencies, now: datetime) -> StalenessCheck:
    """Compare a valuation with the *current* dependencies (spec 18 invalidation list)."""
    now = ensure_utc(now)
    prev = valuation.dependencies
    reasons: list[InvalidationReason] = []
    details: list[str] = []

    def check(changed: bool, reason: InvalidationReason, detail: str) -> None:
        if changed and reason not in reasons:
            reasons.append(reason)
            details.append(detail)
        elif changed:
            details.append(detail)

    check(
        prev.listing_revision_id != current.listing_revision_id,
        InvalidationReason.LISTING_REVISION,
        "listing revision changed",
    )
    check(
        (prev.comparable_set_id, prev.comparable_hash)
        != (current.comparable_set_id, current.comparable_hash),
        InvalidationReason.COMPARABLES,
        "comparable membership/value/availability changed",
    )
    check(prev.fx != current.fx, InvalidationReason.FX, "FX observations changed")
    check(prev.cost_profile != current.cost_profile, InvalidationReason.COST_PROFILE, "cost profile changed")
    check(
        prev.cost_evidence_ids != current.cost_evidence_ids,
        InvalidationReason.COST_QUOTE,
        "cost quotes/evidence changed",
    )
    check(
        prev.cost_inputs_sha256 != current.cost_inputs_sha256,
        InvalidationReason.COST_QUOTE,
        "cost inputs changed",
    )
    check(
        prev.tax_rule != current.tax_rule,
        InvalidationReason.TAX_RULE,
        "tax rule set/approval/effective dates changed",
    )
    check(
        current.tax_rule is not None and current.tax_rule.status in _CLOSED_TAX_STATUSES,
        InvalidationReason.TAX_RULE,
        "tax rule set is expired/revoked/superseded",
    )
    check(
        prev.config_revision_id != current.config_revision_id,
        InvalidationReason.CONFIG,
        "business configuration changed",
    )
    check(prev.evidence_ids != current.evidence_ids, InvalidationReason.EVIDENCE, "verified evidence changed")
    check(
        prev.calculation_version != current.calculation_version,
        InvalidationReason.CALCULATION_VERSION,
        "calculation version changed",
    )
    check(
        valuation.expires_at is not None and now >= valuation.expires_at,
        InvalidationReason.FRESHNESS_DEADLINE,
        "freshness deadline passed",
    )
    already = valuation.state == ValuationState.STALE
    if already:
        details.append(f"already stale: {valuation.stale_reason}")
    return StalenessCheck(stale=bool(reasons) or already, reasons=tuple(reasons), details=tuple(details))


def mark_stale(valuation: Valuation, reasons: Sequence[InvalidationReason], at: datetime) -> Valuation:
    """Move a valuation to ``stale`` (content unchanged; only state/stale fields move)."""
    if valuation.state == ValuationState.INVALID:
        raise ValidationFailed("an invalid valuation is terminal and cannot become stale")
    if valuation.state == ValuationState.STALE:
        return valuation
    if not reasons:
        raise ValidationFailed("marking a valuation stale needs at least one reason")
    update: dict[str, Any] = {
        "state": ValuationState.STALE,
        "stale_at": ensure_utc(at),
        "stale_reason": ", ".join(r.value for r in reasons)[:500],
        "alert_eligible": False,
    }
    return Valuation.model_validate({**valuation.model_dump(), **update})
