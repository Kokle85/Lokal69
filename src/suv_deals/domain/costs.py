"""Cost lines, cost profiles and scenario economics (spec sections 17 and 18).

Business rules implemented here:

- Decimal only. Amounts are exact ``Money``; nothing is rounded before a comparison.
  Rounding happens only for display/storage (``Money.display``/``to_minor_rounded``).
- Every line is ``quoted``, ``estimated``, ``actual``, ``not_applicable`` or ``unknown``
  with currency, low/base/high, evidence, expiry and scope. ``unknown`` carries no
  amounts; ``not_applicable`` needs a reason; quotes/actuals need evidence. An expired
  quote is downgraded to an estimate with a warning. A quote whose scope (city,
  vehicle condition, listing) conflicts with the vehicle being valued is not applied.
- UNKNOWN IS NEVER ZERO. Every required category must be represented by a line (a
  ``not_applicable`` line with a reason is fine). A missing category or any unknown
  constituent makes the dependent totals ``None``; the scenario still returns a
  ``known_subtotal`` and the list of unknown lines.
- Core arithmetic (spec 18), each cash item counted once::

      cash_required = purchase_cash_outlay_excluding_deposits + cash_costs_before_sale
                      + refundable_deposits
      landed_cost = purchase_economic_cost + transport + import_components + clearance
      ready_to_sell_cost = landed_cost + repairs + preparation + included_registration + reserves
      contribution_before_business_tax = expected_realized_proceeds - ready_to_sell_cost
                                         - selling_costs

  A deposit included in a seller's price is split out before calculating. A refund that
  is not confirmed is a labelled assumption in base/upside; the conservative scenario
  carries the no-refund downside (the deposit becomes an economic cost).
- Scenarios: conservative = lower proceeds bound and upper cost bounds (incl. reserve);
  base = base values; upside = upper proceeds and lower cost bounds, never beyond the
  stated bounds. Lines sharing a ``correlation_group`` describe one underlying risk: the
  group takes its single largest overrun (or underrun) instead of stacking every bound,
  which avoids incoherent double counting.
- Terminology: "estimated contribution before business tax", never "net profit".
  Business tax/accounting treatment is not modelled.
- The EUR 1,500 minimum contribution is PROPOSED/unapproved by default. A threshold
  alert needs an approved threshold, complete scenarios and material inputs supported
  by quotes/actuals, approved assumptions or an active approved tax rule.
"""

from __future__ import annotations

import decimal
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import (
    CostCategory,
    CostLineStatus,
    EvidenceKind,
    FxPurpose,
    ScenarioName,
)
from suv_deals.domain.listings import sha256_json
from suv_deals.domain.money import CurrencyCode, FxRate, Money
from suv_deals.domain.profiles import ContributionThreshold
from suv_deals.domain.tax_engine import IMPORT_CATEGORIES, ComponentStatus, TaxCalculation
from suv_deals.errors import ValidationFailed

COST_MODEL_VERSION: Final = "costs/1.0.0"
CONTRIBUTION_LABEL: Final = "estimated contribution before business tax"
KNOWN_SUBTOTAL_LABEL: Final = "known_subtotal"
DISCOUNT_UNKNOWN_LABEL: Final = "asking-based; discount unknown"

_CTX: Final = decimal.Context(prec=50)
_HUNDRED: Final = Decimal(100)


class Term(StrEnum):
    """The arithmetic term each cost category feeds (each category feeds exactly one)."""

    ACQUISITION_COSTS = "acquisition_costs"  # part of purchase_economic_cost
    TRANSPORT = "transport"
    IMPORT_COMPONENTS = "import_components"
    CLEARANCE = "clearance"
    REPAIRS = "repairs"
    PREPARATION = "preparation"
    INCLUDED_REGISTRATION = "included_registration"
    RESERVES = "reserves"
    SELLING_COSTS = "selling_costs"
    REFUNDABLE_DEPOSITS = "refundable_deposits"


#: Category -> term mapping (documented in config/cost_profiles/default_unapproved.yaml).
TERM_BY_CATEGORY: Final[Mapping[CostCategory, Term]] = MappingProxyType(
    {
        CostCategory.BANK_FX_CHARGES: Term.ACQUISITION_COSTS,
        CostCategory.TRAVEL_INSPECTION: Term.ACQUISITION_COSTS,
        CostCategory.TRANSPORT: Term.TRANSPORT,
        CostCategory.EXPORT_PLATES_INSURANCE: Term.TRANSPORT,
        CostCategory.CUSTOMS_BROKER: Term.CLEARANCE,
        CostCategory.IMPORT_DUTY: Term.IMPORT_COMPONENTS,
        CostCategory.MOTOR_VEHICLE_TAX: Term.IMPORT_COMPONENTS,
        CostCategory.IMPORT_VAT: Term.IMPORT_COMPONENTS,
        CostCategory.OTHER_IMPORT_CHARGES: Term.IMPORT_COMPONENTS,
        CostCategory.HOMOLOGATION_REGISTRATION: Term.INCLUDED_REGISTRATION,
        CostCategory.REPAIRS: Term.REPAIRS,
        CostCategory.PREPARATION: Term.PREPARATION,
        CostCategory.STORAGE_HOLDING: Term.PREPARATION,
        CostCategory.RISK_RESERVE: Term.RESERVES,
        CostCategory.SELLING_COSTS: Term.SELLING_COSTS,
        CostCategory.REFUNDABLE_DEPOSIT: Term.REFUNDABLE_DEPOSITS,
    }
)

#: Every spec 18 category except the purchase (which is ``PurchaseInput``).
REQUIRED_CATEGORIES: Final = frozenset(TERM_BY_CATEGORY)

#: Categories whose support level decides quote support / alert eligibility.
MATERIAL_CATEGORIES: Final = frozenset(
    {
        CostCategory.TRANSPORT,
        CostCategory.CUSTOMS_BROKER,
        CostCategory.HOMOLOGATION_REGISTRATION,
        CostCategory.REPAIRS,
        *IMPORT_CATEGORIES,
    }
)

_AMOUNT_STATUSES: Final = frozenset({CostLineStatus.QUOTED, CostLineStatus.ESTIMATED, CostLineStatus.ACTUAL})
_EVIDENCED_STATUSES: Final = frozenset({CostLineStatus.QUOTED, CostLineStatus.ACTUAL})


# --------------------------------------------------------------------------- models


def _has_float(value: Any) -> bool:
    if isinstance(value, float):
        return True
    return isinstance(value, list | tuple) and any(isinstance(v, float) for v in value)


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _refuse_floats(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, value in data.items():
                if _has_float(value):
                    raise ValueError(f"{key}: binary float is not allowed; use a decimal string")
        return data


def _check_range(
    low: Money | None, base: Money | None, high: Money | None, currency: str, *, allow_negative: bool = False
) -> None:
    for name, value in (("low", low), ("base", base), ("high", high)):
        if value is None:
            continue
        if value.currency != currency:
            raise ValueError(f"{name} currency {value.currency} differs from {currency}")
        if not allow_negative and value.amount < 0:
            raise ValueError(f"{name} must not be negative")
    present = [v for v in (low, base, high) if v is not None]
    for left, right in pairwise(present):
        if left.amount > right.amount:
            raise ValueError("amounts must satisfy low <= base <= high")


class CostScope(_Contract):
    """What a quote/estimate covers. A quote for one city/condition never silently applies elsewhere."""

    listing_id: str | None = Field(default=None, max_length=100)
    origin_country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    origin_city: str | None = Field(default=None, max_length=120)
    destination_city: str | None = Field(default=None, max_length=120)
    vehicle_running: Literal["running", "non_running"] | None = None
    note: str | None = Field(default=None, max_length=500)

    def conflicts_with(self, target: CostScope) -> list[str]:
        """Fields set on both sides with different values (case-insensitive for text)."""
        conflicts: list[str] = []
        for name in ("listing_id", "origin_country", "origin_city", "destination_city", "vehicle_running"):
            mine, theirs = getattr(self, name), getattr(target, name)
            if mine is not None and theirs is not None and str(mine).casefold() != str(theirs).casefold():
                conflicts.append(name)
        return conflicts


class CostLine(_Contract):
    """One cost item with status, evidence, bounds, expiry and scope (spec 18)."""

    category: CostCategory
    label: str = Field(min_length=1, max_length=200)
    status: CostLineStatus
    currency: CurrencyCode
    low: Money | None = None
    base: Money | None = None
    high: Money | None = None
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=50)
    provider: str | None = Field(default=None, min_length=1, max_length=200)
    expires_at: datetime | None = None
    scope: CostScope | None = None
    reason: str | None = Field(default=None, min_length=3, max_length=500)
    cash_before_sale: bool = True
    refundable: bool = False
    refund_confirmed: bool = False
    refund_prerequisites: tuple[str, ...] = Field(default=(), max_length=20)
    assumption_approved: bool = False
    #: Amount computed by an ACTIVE, approved, hash-verified tax rule from complete inputs.
    rule_supported: bool = False
    correlation_group: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,60}$")

    @field_validator("expires_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _rules(self) -> CostLine:
        if self.category == CostCategory.PURCHASE:
            raise ValueError("the purchase is modelled by PurchaseInput, not a cost line")
        amounts = (self.low, self.base, self.high)
        if self.status in (CostLineStatus.UNKNOWN, CostLineStatus.NOT_APPLICABLE):
            if any(a is not None for a in amounts):
                raise ValueError(f"a {self.status.value} line carries no amounts (unknown is never zero)")
            if self.status == CostLineStatus.NOT_APPLICABLE and not self.reason:
                raise ValueError("not_applicable requires a reason")
        else:
            if self.base is None:
                raise ValueError(f"a {self.status.value} line needs a base amount")
            _check_range(self.low, self.base, self.high, self.currency)
        if self.status in _EVIDENCED_STATUSES and not self.evidence_ids:
            raise ValueError(f"a {self.status.value} line needs evidence_ids")
        if self.status == CostLineStatus.QUOTED and not self.provider:
            raise ValueError("a quoted line needs a provider")
        if (
            self.refundable != (self.category == CostCategory.REFUNDABLE_DEPOSIT)
            and self.status in _AMOUNT_STATUSES
        ):
            raise ValueError("refundable lines must use category refundable_deposit (and vice versa)")
        if self.refundable and self.category != CostCategory.REFUNDABLE_DEPOSIT:
            raise ValueError("only refundable_deposit lines can be refundable")
        if self.refund_confirmed and (not self.refundable or not self.evidence_ids):
            raise ValueError("a confirmed refund needs a refundable line with evidence")
        if self.rule_supported and (self.status != CostLineStatus.ESTIMATED or not self.evidence_ids):
            raise ValueError("rule_supported lines are evidenced estimates from the tax engine")
        return self

    def effective_status(self, as_of: datetime) -> CostLineStatus:
        """An expired quote is treated as an estimate."""
        if self.status == CostLineStatus.QUOTED and self.expires_at is not None and self.expires_at <= as_of:
            return CostLineStatus.ESTIMATED
        return self.status

    def display_name(self) -> str:
        return f"{self.category.value}: {self.label}"


class PurchaseInput(_Contract):
    """The vehicle purchase payable amount for this buyer (spec 17).

    If the payable amount contains a refundable deposit (e.g. a VAT deposit), it is
    stated in ``included_refundable_deposit`` and split out before calculating.
    ``None`` there means the price evidence shows no deposit inside the amount.
    """

    label: str = Field(default="Vehicle purchase payable amount", min_length=1, max_length=200)
    status: CostLineStatus
    amount: Money | None = None
    basis: str | None = Field(default=None, max_length=200)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=50)
    included_refundable_deposit: Money | None = None
    deposit_refund_confirmed: bool = False
    deposit_refund_prerequisites: tuple[str, ...] = Field(default=(), max_length=20)
    assumption_approved: bool = False

    @model_validator(mode="after")
    def _rules(self) -> PurchaseInput:
        if self.status == CostLineStatus.NOT_APPLICABLE:
            raise ValueError("the purchase is never not_applicable")
        if (self.status == CostLineStatus.UNKNOWN) != (self.amount is None):
            raise ValueError("an unknown purchase has no amount; a known one needs an amount")
        if self.status in _EVIDENCED_STATUSES and not self.evidence_ids:
            raise ValueError(f"a {self.status.value} purchase needs evidence_ids")
        if self.amount is not None and self.amount.amount < 0:
            raise ValueError("purchase amount must not be negative")
        deposit = self.included_refundable_deposit
        if deposit is not None:
            if self.amount is None:
                raise ValueError("an included deposit needs a known purchase amount")
            if deposit.currency != self.amount.currency or deposit.amount < 0 or deposit > self.amount:
                raise ValueError("included deposit must be in the purchase currency and within the amount")
        if self.deposit_refund_confirmed and (deposit is None or not self.evidence_ids):
            raise ValueError("a confirmed deposit refund needs a deposit and evidence")
        return self


ProceedsBasis = Literal[
    "mk_asking_prices", "owner_estimate", "seller_reported_sales", "verified_sales", "dealer_offer"
]


class ProceedsEstimate(_Contract):
    """Expected realized proceeds in MK. Asking prices are not realized sales (spec 15)."""

    status: CostLineStatus
    currency: CurrencyCode
    low: Money | None = None
    base: Money | None = None
    high: Money | None = None
    basis: ProceedsBasis
    evidence_kind: EvidenceKind | None = None
    sample_size: int | None = Field(default=None, ge=0)
    negotiation_discount_pct: Decimal | None = Field(default=None, ge=0, lt=100)  # None = unknown
    discount_approved: bool = False
    assumption_approved: bool = False
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=200)
    comparable_set_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _rules(self) -> ProceedsEstimate:
        if self.status == CostLineStatus.NOT_APPLICABLE:
            raise ValueError("proceeds are never not_applicable")
        if self.status == CostLineStatus.UNKNOWN:
            if any(a is not None for a in (self.low, self.base, self.high)):
                raise ValueError("unknown proceeds carry no amounts")
        else:
            if self.base is None:
                raise ValueError("known proceeds need a base amount")
            _check_range(self.low, self.base, self.high, self.currency)
        if self.status in _EVIDENCED_STATUSES and not self.evidence_ids:
            raise ValueError(f"{self.status.value} proceeds need evidence_ids")
        if self.negotiation_discount_pct is not None and self.basis != "mk_asking_prices":
            raise ValueError("a negotiation discount only applies to asking-price evidence")
        if self.discount_approved and self.negotiation_discount_pct is None:
            raise ValueError("an approved discount needs a value")
        return self

    def label(self) -> str:
        if self.basis != "mk_asking_prices":
            return f"{self.basis.replace('_', ' ')} (realized-proceeds basis)"
        if self.negotiation_discount_pct is None:
            return DISCOUNT_UNKNOWN_LABEL
        state = "approved" if self.discount_approved else "unapproved assumption"
        return f"asking-based less {self.negotiation_discount_pct}% negotiation discount ({state})"


class CostAssumption(_Contract):
    """A versioned cost-profile assumption. Profiles never contain quotes."""

    category: CostCategory
    label: str = Field(min_length=1, max_length=200)
    status: CostLineStatus = CostLineStatus.UNKNOWN
    currency: CurrencyCode = "EUR"
    low: Decimal | None = None
    base: Decimal | None = None
    high: Decimal | None = None
    note: str = Field(min_length=3, max_length=1000)
    reason: str | None = Field(default=None, min_length=3, max_length=500)
    cash_before_sale: bool = True
    correlation_group: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,60}$")
    scope: CostScope | None = None

    @model_validator(mode="after")
    def _rules(self) -> CostAssumption:
        if self.status not in (
            CostLineStatus.UNKNOWN,
            CostLineStatus.ESTIMATED,
            CostLineStatus.NOT_APPLICABLE,
        ):
            raise ValueError("cost profile assumptions are unknown, estimated or not_applicable")
        if self.category == CostCategory.PURCHASE:
            raise ValueError("the purchase comes from listing price evidence, not a cost profile")
        return self

    def to_line(self, *, approved: bool) -> CostLine:
        def money(value: Decimal | None) -> Money | None:
            return None if value is None else Money(amount=value, currency=self.currency)

        return CostLine(
            category=self.category,
            label=self.label,
            status=self.status,
            currency=self.currency,
            low=money(self.low),
            base=money(self.base),
            high=money(self.high),
            reason=self.reason,
            cash_before_sale=self.cash_before_sale,
            refundable=self.category == CostCategory.REFUNDABLE_DEPOSIT
            and self.status == CostLineStatus.ESTIMATED,
            assumption_approved=approved and self.status == CostLineStatus.ESTIMATED,
            correlation_group=self.correlation_group,
            scope=self.scope,
        )


class CostProfileRef(_Contract):
    profile_key: str
    version: int
    sha256: str
    approval_status: Literal["unapproved", "approved"]
    is_fixture: bool


class CostProfile(_Contract):
    """Versioned cost assumptions; unapproved by default (activation gate "Cost assumptions")."""

    profile_key: str = Field(pattern=r"^[a-z0-9_]{1,80}$")
    version: int = Field(ge=1)
    basis: str = Field(min_length=3, max_length=2000)
    currency: CurrencyCode = "EUR"
    approval_status: Literal["unapproved", "approved"] = "unapproved"
    approved_by: str | None = Field(default=None, min_length=1, max_length=200)
    approved_at: datetime | None = None
    is_fixture: bool = False
    notes: tuple[str, ...] = Field(default=(), max_length=50)
    assumptions: tuple[CostAssumption, ...] = Field(default=(), max_length=200)

    @field_validator("approved_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _rules(self) -> CostProfile:
        if self.approval_status == "approved" and (not self.approved_by or self.approved_at is None):
            raise ValueError("an approved cost profile needs approved_by and approved_at")
        if self.is_fixture and self.approval_status == "approved":
            raise ValueError("a fixture cost profile can never be approved")
        keys = [(a.category, a.label) for a in self.assumptions]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate (category, label) assumptions")
        return self

    def sha256(self) -> str:
        return sha256_json(self.model_dump(mode="json"))

    def reference(self) -> CostProfileRef:
        return CostProfileRef(
            profile_key=self.profile_key,
            version=self.version,
            sha256=self.sha256(),
            approval_status=self.approval_status,
            is_fixture=self.is_fixture,
        )

    def lines(self) -> tuple[CostLine, ...]:
        approved = self.approval_status == "approved"
        return tuple(a.to_line(approved=approved) for a in self.assumptions)

    def missing_categories(self) -> frozenset[CostCategory]:
        return REQUIRED_CATEGORIES - {a.category for a in self.assumptions}


def load_cost_profile(path: Path) -> CostProfile:
    """Load a cost profile YAML (config/cost_profiles/*.yaml). Unquoted decimals are refused."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValidationFailed(f"cannot read cost profile {path.name}") from exc
    if not isinstance(raw, dict):
        raise ValidationFailed("a cost profile must be a mapping")
    try:
        return CostProfile.model_validate(raw)
    except ValidationError as exc:
        raise ValidationFailed(
            "invalid cost profile",
            details={"problems": [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()][:50]},
        ) from exc


# --------------------------------------------------------------------------- tax -> cost lines


def tax_cost_lines(calc: TaxCalculation | None, *, missing_reason: str | None = None) -> tuple[CostLine, ...]:
    """One line per import category from a tax calculation (in the rule currency).

    No calculation -> every import category is ``unknown`` (never zero). Lines from an
    ACTIVE, approved, non-fixture, complete calculation are ``rule_supported``.
    """
    if calc is None:
        reason = missing_reason or "no applicable ACTIVE tax rule set; import costs unknown"
        return tuple(
            CostLine(
                category=c,
                label=f"{c.value} (no tax rule)",
                status=CostLineStatus.UNKNOWN,
                currency="EUR",
                reason=reason[:500],
            )
            for c in sorted(IMPORT_CATEGORIES)
        )
    totals = calc.category_totals()
    evidence = (f"tax_rule_set:{calc.rule_set_id}@{calc.version}:{calc.rule_sha256 or 'unhashed'}",)
    supported = calc.production_ready
    lines: list[CostLine] = []
    for category in sorted(IMPORT_CATEGORIES):
        label = f"{category.value} ({calc.rule_set_id}@{calc.version}, {calc.rule_status.value})"
        total = totals.get(category)
        if total is None:
            lines.append(
                CostLine(
                    category=category,
                    label=label,
                    status=CostLineStatus.NOT_APPLICABLE,
                    currency=calc.currency,
                    reason=f"rule {calc.rule_set_id}@{calc.version} defines no {category.value} component"[
                        :500
                    ],
                    evidence_ids=evidence,
                )
            )
        elif total.status == ComponentStatus.UNKNOWN:
            detail = ", ".join([*calc.unknown_components, *(f"missing:{m}" for m in calc.missing_inputs)])
            lines.append(
                CostLine(
                    category=category,
                    label=label,
                    status=CostLineStatus.UNKNOWN,
                    currency=calc.currency,
                    reason=f"tax calculation incomplete ({detail})"[:500],
                    evidence_ids=evidence,
                )
            )
        elif total.status == ComponentStatus.NOT_APPLICABLE:
            lines.append(
                CostLine(
                    category=category,
                    label=label,
                    status=CostLineStatus.NOT_APPLICABLE,
                    currency=calc.currency,
                    reason=f"rule {calc.rule_set_id}@{calc.version}: no {category.value} component applies"[
                        :500
                    ],
                    evidence_ids=evidence,
                )
            )
        else:
            assert total.amount is not None
            lines.append(
                CostLine(
                    category=category,
                    label=label,
                    status=CostLineStatus.ESTIMATED,
                    currency=calc.currency,
                    base=total.amount,
                    evidence_ids=evidence,
                    rule_supported=supported,
                    assumption_approved=supported,
                )
            )
    return tuple(lines)


# --------------------------------------------------------------------------- results


class UnknownLine(_Contract):
    item: str  # category value, "purchase" or "proceeds"
    label: str
    reason: str


class ScenarioResult(_Contract):
    """One scenario. Every total is None when any constituent is unknown."""

    scenario: ScenarioName
    currency: str
    expected_realized_proceeds: Money | None
    proceeds_label: str
    purchase_cash_outlay_excluding_deposits: Money | None
    acquisition_costs: Money | None
    deposits_assumed_not_refunded: Money | None
    purchase_economic_cost: Money | None
    transport: Money | None
    import_components: Money | None
    clearance: Money | None
    repairs: Money | None
    preparation: Money | None
    included_registration: Money | None
    reserves: Money | None
    selling_costs: Money | None
    refundable_deposits: Money | None
    cash_costs_before_sale: Money | None
    landed_cost: Money | None
    ready_to_sell_cost: Money | None
    total_modelled_cost: Money | None
    cash_required: Money | None
    contribution_before_business_tax: Money | None
    known_subtotal: Money  # sum of known modelled costs; never a total
    unknown_lines: tuple[UnknownLine, ...]
    assumptions: tuple[str, ...]
    complete: bool
    contribution_label: str = CONTRIBUTION_LABEL


class ThresholdEvaluation(_Contract):
    threshold: Money
    approval_status: str
    proposed_only: bool
    would_meet: bool | None  # base scenario
    would_meet_by_scenario: dict[ScenarioName, bool | None]
    alert_eligible: bool
    blockers: tuple[str, ...]


class ScenarioSet(_Contract):
    model_version: str = COST_MODEL_VERSION
    currency: str
    as_of: datetime
    scenarios: tuple[ScenarioResult, ...]
    complete: bool
    unknown_lines: tuple[UnknownLine, ...]
    proceeds_basis: str
    proceeds_label: str
    material_support: Literal["quote_supported", "approved_assumptions", "estimated", "incomplete"]
    unsupported_material: tuple[str, ...]
    assumptions: tuple[str, ...]
    warnings: tuple[str, ...]
    correlation_notes: tuple[str, ...]
    threshold: ThresholdEvaluation
    evidence_ids: tuple[str, ...]
    earliest_expiry: datetime | None
    fx_rates_used: tuple[FxRate, ...]
    #: sha256 over every scenario input (purchase, lines, proceeds, scope, currency, FX age).
    inputs_sha256: str
    contribution_label: str = CONTRIBUTION_LABEL

    def scenario(self, name: ScenarioName) -> ScenarioResult:
        for result in self.scenarios:
            if result.scenario == name:
                return result
        raise KeyError(name)  # pragma: no cover - all three are always present


# --------------------------------------------------------------------------- engine internals

_SCENARIOS: Final = (ScenarioName.CONSERVATIVE, ScenarioName.BASE, ScenarioName.UPSIDE)


class _Converter:
    """Converts to the valuation currency with explicit direction; never with customs rates."""

    def __init__(self, rates: Sequence[FxRate], target: str, on_date: date, max_age_days: int) -> None:
        self.rates = rates
        self.target = target
        self.on_date = on_date
        self.max_age_days = max_age_days
        self.warnings: list[str] = []
        self.used: dict[tuple[str, str, date, str, str], FxRate] = {}

    def rate_for(self, currency: str) -> FxRate | None:
        candidates = [
            r
            for r in self.rates
            if {r.base, r.quote} == {currency, self.target}
            and r.purpose != FxPurpose.CUSTOMS
            and r.rate_date <= self.on_date
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda r: (r.rate_date, r.purpose == FxPurpose.PAYMENT, r.provider))
        return candidates[-1]

    def convert(self, money: Money | None, what: str) -> Money | None:
        if money is None or money.currency == self.target:
            return money
        rate = self.rate_for(money.currency)
        if rate is None:
            note = (
                " (ECB does not publish MKD; an owner-approved source is required)"
                if "MKD" in (money.currency, self.target)
                else ""
            )
            self.warnings.append(f"FX_RATE_MISSING:{money.currency}->{self.target} for {what}{note}")
            return None
        if rate.is_stale(self.on_date, self.max_age_days):
            self.warnings.append(
                f"FX_RATE_STALE:{rate.base}/{rate.quote} {rate.rate_date.isoformat()} "
                f"older than {self.max_age_days} days"
            )
        self.used[(rate.base, rate.quote, rate.rate_date, rate.provider, rate.purpose.value)] = rate
        return rate.convert(money, self.target)


@dataclass(slots=True)
class _Item:
    """A line (or the purchase/deposit) with its scenario amounts in the valuation currency."""

    item: str
    label: str
    category: CostCategory | None
    status: CostLineStatus
    known: bool
    amounts: dict[ScenarioName, Decimal] = field(default_factory=dict)
    reason: str = ""
    cash_before_sale: bool = True
    line: CostLine | None = None
    low: Decimal | None = None
    base: Decimal | None = None
    high: Decimal | None = None


def _bounds(item: _Item) -> None:
    """Default scenario amounts: conservative=high, base=base, upside=low (point estimate if absent)."""
    assert item.base is not None
    low = item.low if item.low is not None else item.base
    high = item.high if item.high is not None else item.base
    item.amounts = {ScenarioName.CONSERVATIVE: high, ScenarioName.BASE: item.base, ScenarioName.UPSIDE: low}


def _apply_correlation(items: list[_Item], notes: list[str]) -> None:
    """Lines sharing a correlation group take the single largest overrun/underrun of the group."""
    groups: dict[str, list[_Item]] = {}
    for item in items:
        if item.known and item.line is not None and item.line.correlation_group and item.base is not None:
            groups.setdefault(item.line.correlation_group, []).append(item)
    for group, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        for member in members:
            assert member.base is not None
            member.amounts[ScenarioName.CONSERVATIVE] = member.base
            member.amounts[ScenarioName.UPSIDE] = member.base

        def over(m: _Item) -> Decimal:
            assert m.base is not None
            return (m.high if m.high is not None else m.base) - m.base

        def under(m: _Item) -> Decimal:
            assert m.base is not None
            return m.base - (m.low if m.low is not None else m.base)

        worst = max(members, key=over)
        best = max(members, key=under)
        worst.amounts[ScenarioName.CONSERVATIVE] = _CTX.add(
            worst.amounts[ScenarioName.CONSERVATIVE], over(worst)
        )
        best.amounts[ScenarioName.UPSIDE] = _CTX.subtract(best.amounts[ScenarioName.UPSIDE], under(best))
        notes.append(
            f"correlation group {group!r} ({', '.join(m.label for m in members)}): one underlying risk; "
            f"conservative adds only the largest overrun ({worst.label}), upside subtracts only the "
            f"largest underrun ({best.label}) instead of stacking every bound"
        )


def _sum(values: Iterable[Decimal | None]) -> Decimal | None:
    total = Decimal(0)
    for value in values:
        if value is None:
            return None
        total = _CTX.add(total, value)
    return total


def _money(value: Decimal | None, currency: str) -> Money | None:
    return None if value is None else Money(amount=value, currency=currency)


def _strictly_supported(status: CostLineStatus, line: CostLine | None) -> bool:
    if status in (CostLineStatus.QUOTED, CostLineStatus.ACTUAL, CostLineStatus.NOT_APPLICABLE):
        return True
    return line is not None and line.rule_supported and status == CostLineStatus.ESTIMATED


# --------------------------------------------------------------------------- public API


def compute_scenarios(  # noqa: PLR0917 - positional order is the documented package contract
    purchase: PurchaseInput,
    lines: Sequence[CostLine],
    proceeds: ProceedsEstimate,
    fx_rates: Sequence[FxRate],
    threshold: ContributionThreshold,
    valuation_currency: str = "EUR",
    *,
    as_of: datetime,
    fx_max_age_days: int = 7,
    target_scope: CostScope | None = None,
    required_categories: frozenset[CostCategory] = REQUIRED_CATEGORIES,
) -> ScenarioSet:
    """Conservative/base/upside economics with explicit unknowns (spec 18).

    Pure and deterministic. All arithmetic is unrounded Decimal; threshold comparisons
    use unrounded values. Any unknown constituent yields ``None`` for the dependent
    totals; ``known_subtotal`` and ``unknown_lines`` are always returned.
    """
    as_of = ensure_utc(as_of)
    if fx_max_age_days < 0:
        raise ValidationFailed("fx_max_age_days must not be negative")
    on_date = as_of.date()
    ccy = valuation_currency
    conv = _Converter(fx_rates, ccy, on_date, fx_max_age_days)
    warnings: list[str] = []
    assumptions: list[str] = []
    notes: list[str] = []
    unknown: list[UnknownLine] = []
    evidence: list[str] = [*purchase.evidence_ids, *proceeds.evidence_ids]
    expiries: list[datetime] = []

    # -- purchase (deposit split out first)
    purchase_amount = conv.convert(purchase.amount, "purchase")
    included_deposit = conv.convert(purchase.included_refundable_deposit, "included deposit")
    purchase_cash: Decimal | None = None
    if purchase.status == CostLineStatus.UNKNOWN:
        unknown.append(
            UnknownLine(item="purchase", label=purchase.label, reason="purchase payable amount unknown")
        )
    elif purchase_amount is None or (
        purchase.included_refundable_deposit is not None and included_deposit is None
    ):
        unknown.append(
            UnknownLine(item="purchase", label=purchase.label, reason="purchase amount not convertible")
        )
    else:
        deposit_part = included_deposit.amount if included_deposit is not None else Decimal(0)
        purchase_cash = _CTX.subtract(purchase_amount.amount, deposit_part)
        if included_deposit is not None:
            assumptions.append(
                f"seller price includes a refundable deposit of {included_deposit.display()}; split out "
                "of the purchase cash outlay and counted once under refundable deposits"
            )

    # -- cost lines
    items: list[_Item] = []
    for line in lines:
        status = line.effective_status(as_of)
        evidence.extend(line.evidence_ids)
        if line.expires_at is not None:
            expiries.append(line.expires_at)
        if status != line.status:
            warnings.append(f"QUOTE_EXPIRED:{line.display_name()}: treated as an estimate")
        item = _Item(
            item=line.category.value,
            label=line.label,
            category=line.category,
            status=status,
            known=False,
            cash_before_sale=line.cash_before_sale,
            line=line,
        )
        conflicts = line.scope.conflicts_with(target_scope) if line.scope and target_scope else []
        if conflicts:
            item.reason = (
                f"scope mismatch ({', '.join(conflicts)}): this estimate/quote covers a different case"
            )
            warnings.append(f"COST_SCOPE_MISMATCH:{line.display_name()}:{','.join(conflicts)}")
        elif status == CostLineStatus.UNKNOWN:
            item.reason = line.reason or "status unknown"
        elif status == CostLineStatus.NOT_APPLICABLE:
            item.known = True
            item.amounts = dict.fromkeys(_SCENARIOS, Decimal(0))
        else:
            low = conv.convert(line.low, line.display_name())
            base = conv.convert(line.base, line.display_name())
            high = conv.convert(line.high, line.display_name())
            if (
                base is None
                or (line.low is not None and low is None)
                or (line.high is not None and high is None)
            ):
                item.reason = f"no usable FX rate {line.currency}->{ccy}"
            else:
                item.known = True
                item.low = None if low is None else low.amount
                item.base = base.amount
                item.high = None if high is None else high.amount
                _bounds(item)
        if not item.known:
            unknown.append(UnknownLine(item=item.item, label=line.label, reason=item.reason))
        items.append(item)

    present = {line.category for line in lines}
    if purchase.included_refundable_deposit is not None:
        present.add(CostCategory.REFUNDABLE_DEPOSIT)
    for category in sorted(required_categories - present):
        reason = "no line supplied for a required category (unknown, not zero)"
        items.append(
            _Item(
                item=category.value,
                label=category.value,
                category=category,
                status=CostLineStatus.UNKNOWN,
                known=False,
                reason=reason,
            )
        )
        unknown.append(UnknownLine(item=category.value, label=category.value, reason=reason))

    _apply_correlation(items, notes)

    # -- proceeds
    proceeds_by: dict[ScenarioName, Decimal | None] = dict.fromkeys(_SCENARIOS)
    if proceeds.status == CostLineStatus.UNKNOWN:
        unknown.append(
            UnknownLine(item="proceeds", label="expected realized proceeds", reason="proceeds unknown")
        )
    else:
        factor = Decimal(1)
        if proceeds.negotiation_discount_pct is not None:
            factor = _CTX.subtract(Decimal(1), _CTX.divide(proceeds.negotiation_discount_pct, _HUNDRED))
        picks = {
            ScenarioName.CONSERVATIVE: proceeds.low,
            ScenarioName.BASE: proceeds.base,
            ScenarioName.UPSIDE: proceeds.high,
        }
        for name, bound in picks.items():
            chosen = bound
            if chosen is None:
                chosen = proceeds.base
                warnings.append(f"PROCEEDS_BOUND_MISSING:{name.value}: base proceeds used as point estimate")
            converted = conv.convert(chosen, f"proceeds ({name.value})")
            proceeds_by[name] = None if converted is None else _CTX.multiply(converted.amount, factor)
        if any(v is None for v in proceeds_by.values()):
            unknown.append(
                UnknownLine(
                    item="proceeds", label="expected realized proceeds", reason=f"no FX rate to {ccy}"
                )
            )
        if proceeds.basis == "mk_asking_prices" and proceeds.negotiation_discount_pct is None:
            assumptions.append(f"proceeds are {DISCOUNT_UNKNOWN_LABEL}: asking prices are not realized sales")

    # -- deposits (lines + included in purchase)
    deposits: list[tuple[str, dict[ScenarioName, Decimal] | None, bool, tuple[str, ...]]] = []
    if purchase.included_refundable_deposit is not None:
        amount = None if included_deposit is None else dict.fromkeys(_SCENARIOS, included_deposit.amount)
        deposits.append(
            (
                "deposit included in purchase price",
                amount,
                purchase.deposit_refund_confirmed,
                purchase.deposit_refund_prerequisites,
            )
        )
    for item in items:
        if item.category == CostCategory.REFUNDABLE_DEPOSIT and item.status != CostLineStatus.NOT_APPLICABLE:
            deposit_line = item.line
            confirmed = bool(deposit_line and deposit_line.refund_confirmed)
            prereqs = deposit_line.refund_prerequisites if deposit_line else ()
            deposits.append((item.label, item.amounts if item.known else None, confirmed, prereqs))
    for label, _, confirmed, prereqs in deposits:
        if not confirmed:
            needs = "; ".join(prereqs) if prereqs else "prerequisites not recorded"
            assumptions.append(
                f"refund of '{label}' is ASSUMED in base/upside (unconfirmed; prerequisites: {needs}); "
                "the conservative scenario carries the no-refund downside"
            )

    # -- scenarios
    results: list[ScenarioResult] = []
    for name in _SCENARIOS:

        def term(t: Term, scenario: ScenarioName = name) -> Decimal | None:
            members = [i for i in items if i.category is not None and TERM_BY_CATEGORY[i.category] == t]
            return _sum(i.amounts[scenario] if i.known else None for i in members)

        lost: Decimal | None = Decimal(0)
        for _label, amounts, confirmed, _prereqs in deposits:
            if confirmed or name != ScenarioName.CONSERVATIVE:
                continue
            lost = None if amounts is None or lost is None else _CTX.add(lost, amounts[name])
        acquisition = term(Term.ACQUISITION_COSTS)
        economic = _sum([purchase_cash, acquisition, lost])
        transport = term(Term.TRANSPORT)
        imports = term(Term.IMPORT_COMPONENTS)
        clearance = term(Term.CLEARANCE)
        repairs = term(Term.REPAIRS)
        preparation = term(Term.PREPARATION)
        registration = term(Term.INCLUDED_REGISTRATION)
        reserves = term(Term.RESERVES)
        selling = term(Term.SELLING_COSTS)
        line_deposits = term(Term.REFUNDABLE_DEPOSITS)
        refundable = _sum(
            [line_deposits, included_deposit.amount if included_deposit is not None else Decimal(0)]
        )
        if purchase.included_refundable_deposit is not None and included_deposit is None:
            refundable = None
        # A missing category (no line) is unknown, so it may well be paid before the sale.
        cash_costs = _sum(
            i.amounts[name] if i.known else None
            for i in items
            if i.category not in (None, CostCategory.REFUNDABLE_DEPOSIT)
            and (i.line is None or i.cash_before_sale)
        )
        landed = _sum([economic, transport, imports, clearance])
        ready = _sum([landed, repairs, preparation, registration, reserves])
        total_cost = _sum([ready, selling])
        cash_required = _sum([purchase_cash, cash_costs, refundable])
        proceeds_value = proceeds_by[name]
        contribution = None
        if proceeds_value is not None and ready is not None and selling is not None:
            contribution = _CTX.subtract(_CTX.subtract(proceeds_value, ready), selling)

        known_parts: list[Decimal] = [purchase_cash] if purchase_cash is not None else []
        known_parts.extend(
            i.amounts[name] for i in items if i.known and i.category != CostCategory.REFUNDABLE_DEPOSIT
        )
        if lost is not None:
            known_parts.append(lost)
        known_subtotal = sum(known_parts, Decimal(0))

        scenario_assumptions = list(assumptions)
        if name == ScenarioName.CONSERVATIVE and any(not d[2] for d in deposits):
            scenario_assumptions.append("no-refund downside: unconfirmed deposits counted as economic cost")
        complete = None not in (total_cost, contribution, cash_required)
        results.append(
            ScenarioResult(
                scenario=name,
                currency=ccy,
                expected_realized_proceeds=_money(proceeds_value, ccy),
                proceeds_label=proceeds.label(),
                purchase_cash_outlay_excluding_deposits=_money(purchase_cash, ccy),
                acquisition_costs=_money(acquisition, ccy),
                deposits_assumed_not_refunded=_money(lost, ccy),
                purchase_economic_cost=_money(economic, ccy),
                transport=_money(transport, ccy),
                import_components=_money(imports, ccy),
                clearance=_money(clearance, ccy),
                repairs=_money(repairs, ccy),
                preparation=_money(preparation, ccy),
                included_registration=_money(registration, ccy),
                reserves=_money(reserves, ccy),
                selling_costs=_money(selling, ccy),
                refundable_deposits=_money(refundable, ccy),
                cash_costs_before_sale=_money(cash_costs, ccy),
                landed_cost=_money(landed, ccy),
                ready_to_sell_cost=_money(ready, ccy),
                total_modelled_cost=_money(total_cost, ccy),
                cash_required=_money(cash_required, ccy),
                contribution_before_business_tax=_money(contribution, ccy),
                known_subtotal=Money(amount=known_subtotal, currency=ccy),
                unknown_lines=tuple(unknown),
                assumptions=tuple(dict.fromkeys(scenario_assumptions)),
                complete=complete,
            )
        )

    # -- support level of material inputs
    unsupported: list[str] = []
    strict_ok = True
    material = [i for i in items if i.category in MATERIAL_CATEGORIES]
    if purchase.status == CostLineStatus.UNKNOWN or purchase_cash is None:
        strict_ok = False
    elif purchase.status not in _EVIDENCED_STATUSES:
        strict_ok = False
        if not purchase.assumption_approved:
            unsupported.append(
                f"purchase: {purchase.status.value} (seller has not confirmed the payable amount)"
            )
    for item in material:
        if not item.known:
            strict_ok = False
            continue
        if not _strictly_supported(item.status, item.line):
            strict_ok = False
            if not (item.status == CostLineStatus.ESTIMATED and item.line and item.line.assumption_approved):
                unsupported.append(
                    f"{item.item}: {item.label} ({item.status.value}, assumption not approved)"
                )
    if not proceeds_supported(proceeds):
        unsupported.append(f"proceeds: {proceeds.label()} (not an approved/realized basis)")
    any_unknown = bool(unknown)
    if any_unknown:
        support: Literal["quote_supported", "approved_assumptions", "estimated", "incomplete"] = "incomplete"
    elif strict_ok:
        support = "quote_supported"
    elif not [u for u in unsupported if not u.startswith("proceeds")]:
        support = "approved_assumptions"
    else:
        support = "estimated"

    notes.append(
        "scenarios move all costs and proceeds within their stated bounds together; FX uses one recorded "
        "rate per currency pair (bank/FX spread belongs in bank_fx_charges); business tax is not modelled"
    )
    warnings.extend(conv.warnings)
    complete_all = all(r.complete for r in results)
    threshold_eval = _evaluate_threshold(
        threshold, ccy, results, complete=complete_all, unsupported=unsupported, warnings=warnings
    )
    return ScenarioSet(
        currency=ccy,
        as_of=as_of,
        scenarios=tuple(results),
        complete=complete_all,
        unknown_lines=tuple(unknown),
        proceeds_basis=proceeds.basis,
        proceeds_label=proceeds.label(),
        material_support=support,
        unsupported_material=tuple(dict.fromkeys(unsupported)),
        assumptions=tuple(dict.fromkeys(assumptions)),
        warnings=tuple(dict.fromkeys(warnings)),
        correlation_notes=tuple(notes),
        threshold=threshold_eval,
        evidence_ids=tuple(sorted(set(evidence))),
        earliest_expiry=min(expiries) if expiries else None,
        fx_rates_used=tuple(conv.used[k] for k in sorted(conv.used)),
        inputs_sha256=scenario_inputs_sha256(
            purchase,
            lines,
            proceeds,
            valuation_currency=ccy,
            fx_max_age_days=fx_max_age_days,
            target_scope=target_scope,
            required_categories=required_categories,
        ),
    )


def scenario_inputs_sha256(
    purchase: PurchaseInput,
    lines: Sequence[CostLine],
    proceeds: ProceedsEstimate,
    *,
    valuation_currency: str,
    fx_max_age_days: int,
    target_scope: CostScope | None,
    required_categories: frozenset[CostCategory],
) -> str:
    """Order-independent hash of the economic inputs (dependency fingerprint material)."""
    line_payloads = sorted((line.model_dump(mode="json") for line in lines), key=sha256_json)
    return sha256_json(
        {
            "model_version": COST_MODEL_VERSION,
            "purchase": purchase.model_dump(mode="json"),
            "lines": line_payloads,
            "proceeds": proceeds.model_dump(mode="json"),
            "valuation_currency": valuation_currency,
            "fx_max_age_days": fx_max_age_days,
            "target_scope": None if target_scope is None else target_scope.model_dump(mode="json"),
            "required_categories": sorted(c.value for c in required_categories),
        }
    )


def proceeds_supported(proceeds: ProceedsEstimate) -> bool:
    """Proceeds can support an alert only as a realized/quoted basis or an approved assumption
    (an asking-price basis additionally needs a known, approved negotiation discount)."""
    if proceeds.status in (CostLineStatus.QUOTED, CostLineStatus.ACTUAL):
        return True
    if proceeds.status != CostLineStatus.ESTIMATED or not proceeds.assumption_approved:
        return False
    if proceeds.basis == "mk_asking_prices":
        return proceeds.negotiation_discount_pct is not None and proceeds.discount_approved
    return True


def _evaluate_threshold(
    threshold: ContributionThreshold,
    currency: str,
    results: Sequence[ScenarioResult],
    *,
    complete: bool,
    unsupported: Sequence[str],
    warnings: list[str],
) -> ThresholdEvaluation:
    limit = Money(amount=threshold.amount_eur, currency="EUR")
    by_scenario: dict[ScenarioName, bool | None] = {}
    for result in results:
        value = result.contribution_before_business_tax
        if value is None or currency != "EUR":
            by_scenario[result.scenario] = None
        else:
            by_scenario[result.scenario] = value.amount >= limit.amount  # unrounded comparison
    if currency != "EUR":
        warnings.append("THRESHOLD_CURRENCY: the contribution threshold is defined in EUR only")
    approved = threshold.approval_status == "approved"
    blockers: list[str] = []
    if not approved:
        blockers.append(f"contribution threshold {limit.display()} is PROPOSED/unapproved")
    if not complete:
        blockers.append("scenarios are incomplete (unknown inputs)")
    blockers.extend(f"unsupported material input: {u}" for u in unsupported)
    conservative = by_scenario.get(ScenarioName.CONSERVATIVE)
    if conservative is not True:
        blockers.append(
            "conservative scenario does not meet the threshold"
            if conservative is False
            else "conservative contribution unknown"
        )
    return ThresholdEvaluation(
        threshold=limit,
        approval_status=threshold.approval_status,
        proposed_only=not approved,
        would_meet=by_scenario.get(ScenarioName.BASE),
        would_meet_by_scenario=by_scenario,
        alert_eligible=not blockers,
        blockers=tuple(blockers),
    )
