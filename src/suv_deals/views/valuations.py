"""Valuation read model: versioned scenario breakdown with explicit unknowns (spec 16-18, 21, 23 screen 4).

- Every input line keeps its status (``quoted``, ``estimated``, ``actual``, ``not_applicable``,
  ``unknown``) and its low/base/high amounts as decimal strings.
- A scenario shows ``totals`` only when it is complete; otherwise it shows a ``known_subtotal``
  (labelled as such, never a total) plus the list of unknown lines.
- Contribution figures appear only for a valuation that has them (estimated or quote-supported,
  or stale after having them); incomplete/not-started/invalid valuations show them as unknown.
- Terminology: "estimated contribution before business tax", never "net profit".
- The EUR 1,500 threshold is labelled PROPOSED until the owner approves it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import Field, model_validator

from suv_deals.domain.costs import (
    CostLine,
    CostScope,
    ProceedsEstimate,
    PurchaseInput,
    ScenarioResult,
    ScenarioSet,
    ThresholdEvaluation,
)
from suv_deals.domain.enums import (
    CostCategory,
    CostLineStatus,
    EligibilityState,
    EvidenceKind,
    ProfileKey,
    ScenarioName,
    TaxRuleStatus,
    ValuationState,
)
from suv_deals.domain.money import CurrencyCode, Money
from suv_deals.domain.tax_engine import ComponentStatus, TaxCalculation
from suv_deals.domain.valuation import ComparableReference, Valuation, ValuationDependencies
from suv_deals.views.common import (
    AmountView,
    DecimalStr,
    Sha256Hex,
    UtcDatetime,
    ViewModel,
    decimal_str,
)

TERMINOLOGY_NOTE: Final = (
    "Estimated contribution before business tax; not net profit. Business tax and accounting "
    "treatment are not modelled. Scenarios are not probabilities."
)
FIXTURE_LABEL: Final = "SYNTHETIC FIXTURE: never a real opportunity; never notified."
ASKING_PROCEEDS_NOTICE: Final = "Asking-price evidence is not a realized sale price."
_FIGURE_STATES: Final = frozenset({ValuationState.ESTIMATED, ValuationState.QUOTE_SUPPORTED})
_NO_FIGURE_STATES: Final = frozenset(
    {ValuationState.NOT_STARTED, ValuationState.INCOMPLETE, ValuationState.INVALID}
)

ContributionLabel = Literal["estimated contribution before business tax"]

#: Scenario component terms, in arithmetic order (spec 18 core arithmetic).
COMPONENT_TERMS: Final[tuple[tuple[str, str], ...]] = (
    ("purchase_cash_outlay_excluding_deposits", "Vehicle purchase cash outlay (excluding deposits)"),
    ("acquisition_costs", "Bank/FX charges, travel and inspection"),
    ("deposits_assumed_not_refunded", "Deposits assumed not refunded (downside)"),
    ("transport", "Transport, export plates and insurance"),
    ("import_components", "Import duty, motor-vehicle tax, import VAT and other charges"),
    ("clearance", "Customs broker and clearance"),
    ("repairs", "Repairs"),
    ("preparation", "Preparation, storage and holding"),
    ("included_registration", "Homologation, documents and registration"),
    ("reserves", "Risk reserve"),
    ("selling_costs", "Selling costs"),
    ("refundable_deposits", "Refundable deposits (cash exposure)"),
)
#: Derived totals, shown only for complete scenarios.
TOTAL_TERMS: Final[tuple[tuple[str, str], ...]] = (
    ("purchase_economic_cost", "Purchase economic cost"),
    ("cash_costs_before_sale", "Cash costs before sale"),
    ("landed_cost", "Landed cost"),
    ("ready_to_sell_cost", "Ready-to-sell cost"),
    ("total_modelled_cost", "Total modelled cost"),
    ("cash_required", "Maximum cash required"),
)


# --------------------------------------------------------------------------- lines


class CostLineView(ViewModel):
    """One scenario input line with its status, bounds, evidence, expiry and scope (spec 18)."""

    category: CostCategory
    label: str = Field(max_length=200)
    status: CostLineStatus
    declared_status: CostLineStatus
    currency: CurrencyCode
    low: DecimalStr | None
    base: DecimalStr | None
    high: DecimalStr | None
    evidence_ids: tuple[str, ...] = Field(max_length=50)
    provider: str | None = Field(max_length=200)
    expires_at: UtcDatetime | None
    scope: CostScope | None
    reason: str | None = Field(max_length=500)
    cash_before_sale: bool
    refundable: bool
    refund_confirmed: bool
    assumption_approved: bool
    rule_supported: bool
    correlation_group: str | None = Field(max_length=60)

    @model_validator(mode="after")
    def _unknown_is_not_zero(self) -> CostLineView:
        if self.status in (CostLineStatus.UNKNOWN, CostLineStatus.NOT_APPLICABLE):
            if any(v is not None for v in (self.low, self.base, self.high)):
                raise ValueError(f"a {self.status.value} line carries no amounts")
            if self.status == CostLineStatus.NOT_APPLICABLE and not self.reason:
                raise ValueError("not_applicable needs a reason")
        elif self.base is None:
            raise ValueError(f"a {self.status.value} line needs a base amount")
        return self

    @classmethod
    def of(cls, line: CostLine, *, as_of: datetime) -> CostLineView:
        """``status`` is the effective status at ``as_of`` (an expired quote reads as an estimate)."""

        def amount(value: Money | None) -> str | None:
            return None if value is None else decimal_str(value.quantized())

        return cls(
            category=line.category,
            label=line.label,
            status=line.effective_status(as_of),
            declared_status=line.status,
            currency=line.currency,
            low=amount(line.low),
            base=amount(line.base),
            high=amount(line.high),
            evidence_ids=line.evidence_ids,
            provider=line.provider,
            expires_at=line.expires_at,
            scope=line.scope,
            reason=line.reason,
            cash_before_sale=line.cash_before_sale,
            refundable=line.refundable,
            refund_confirmed=line.refund_confirmed,
            assumption_approved=line.assumption_approved,
            rule_supported=line.rule_supported,
            correlation_group=line.correlation_group,
        )


class PurchaseView(ViewModel):
    label: str = Field(max_length=200)
    status: CostLineStatus
    amount: AmountView
    basis: str | None = Field(max_length=200)
    evidence_ids: tuple[str, ...] = Field(max_length=50)
    included_refundable_deposit: AmountView
    deposit_refund_confirmed: bool
    assumption_approved: bool

    @classmethod
    def of(cls, purchase: PurchaseInput) -> PurchaseView:
        return cls(
            label=purchase.label,
            status=purchase.status,
            amount=AmountView.of(purchase.amount, unknown_reason="payable purchase amount unknown"),
            basis=purchase.basis,
            evidence_ids=purchase.evidence_ids,
            included_refundable_deposit=(
                AmountView.of(purchase.included_refundable_deposit)
                if purchase.included_refundable_deposit is not None
                else AmountView.not_applicable("price evidence shows no deposit inside the amount")
            ),
            deposit_refund_confirmed=purchase.deposit_refund_confirmed,
            assumption_approved=purchase.assumption_approved,
        )


class ProceedsView(ViewModel):
    status: CostLineStatus
    basis: str = Field(max_length=40)
    label: str = Field(max_length=200)
    currency: CurrencyCode
    low: AmountView
    base: AmountView
    high: AmountView
    evidence_kind: EvidenceKind | None
    sample_size: int | None = Field(ge=0)
    negotiation_discount_pct: DecimalStr | None
    discount_status: Literal["unknown", "unapproved_assumption", "approved"]
    comparable_set_id: str | None = Field(max_length=100)
    evidence_ids: tuple[str, ...] = Field(max_length=200)
    notice: str = Field(max_length=300)

    @classmethod
    def of(cls, proceeds: ProceedsEstimate) -> ProceedsView:
        reason = "expected realized proceeds unknown"
        discount = proceeds.negotiation_discount_pct
        return cls(
            status=proceeds.status,
            basis=proceeds.basis,
            label=proceeds.label(),
            currency=proceeds.currency,
            low=AmountView.of(proceeds.low, unknown_reason=reason, currency=proceeds.currency),
            base=AmountView.of(proceeds.base, unknown_reason=reason, currency=proceeds.currency),
            high=AmountView.of(proceeds.high, unknown_reason=reason, currency=proceeds.currency),
            evidence_kind=proceeds.evidence_kind,
            sample_size=proceeds.sample_size,
            negotiation_discount_pct=None if discount is None else decimal_str(discount),
            discount_status=(
                "unknown"
                if discount is None
                else ("approved" if proceeds.discount_approved else "unapproved_assumption")
            ),
            comparable_set_id=proceeds.comparable_set_id,
            evidence_ids=proceeds.evidence_ids,
            notice=(
                ASKING_PROCEEDS_NOTICE
                if proceeds.basis == "mk_asking_prices"
                else "Realized-proceeds basis; check the cited evidence."
            ),
        )


# --------------------------------------------------------------------------- scenarios


class ScenarioTermView(ViewModel):
    term: str = Field(max_length=60)
    label: str = Field(max_length=200)
    amount: AmountView


class UnknownLineView(ViewModel):
    item: str = Field(max_length=60)
    label: str = Field(max_length=200)
    reason: str = Field(max_length=500)


class ScenarioView(ViewModel):
    """One coherent scenario (conservative/base/upside). Scenarios are not probabilities."""

    scenario: ScenarioName
    complete: bool
    currency: CurrencyCode
    proceeds_label: str = Field(max_length=200)
    expected_realized_proceeds: AmountView
    components: tuple[ScenarioTermView, ...] = Field(max_length=len(COMPONENT_TERMS))
    totals: tuple[ScenarioTermView, ...] | None = Field(max_length=len(TOTAL_TERMS))
    known_subtotal: AmountView | None
    known_subtotal_label: Literal["known_subtotal"] = "known_subtotal"
    unknown_lines: tuple[UnknownLineView, ...] = Field(max_length=100)
    contribution_before_business_tax: AmountView
    contribution_label: ContributionLabel = "estimated contribution before business tax"
    assumptions: tuple[str, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def _totals_or_subtotal(self) -> ScenarioView:
        if self.complete:
            if self.totals is None or self.known_subtotal is not None or self.unknown_lines:
                raise ValueError("a complete scenario shows totals, no known_subtotal and no unknown lines")
            if any(t.amount.status != "known" for t in self.totals):
                raise ValueError("complete totals are all known")
            if self.contribution_before_business_tax.status != "known":
                raise ValueError("a complete scenario has a contribution figure")
        else:
            if self.totals is not None or self.known_subtotal is None:
                raise ValueError("an incomplete scenario shows a known_subtotal instead of totals")
            if self.contribution_before_business_tax.status == "known":
                raise ValueError("an incomplete scenario has no contribution figure")
        return self

    @classmethod
    def of(cls, result: ScenarioResult, *, figures_visible: bool, hidden_reason: str) -> ScenarioView:
        currency = result.currency
        complete = result.complete and figures_visible

        def term(name: str, label: str) -> ScenarioTermView:
            value: Money | None = getattr(result, name)
            return ScenarioTermView(
                term=name,
                label=label,
                amount=AmountView.of(value, unknown_reason="depends on an unknown line", currency=currency),
            )

        unknown_reason = (
            hidden_reason if result.complete else "one or more cost or proceeds lines are unknown"
        )
        return cls(
            scenario=result.scenario,
            complete=complete,
            currency=currency,
            proceeds_label=result.proceeds_label,
            expected_realized_proceeds=AmountView.of(
                result.expected_realized_proceeds, unknown_reason="proceeds unknown", currency=currency
            ),
            components=tuple(term(name, label) for name, label in COMPONENT_TERMS),
            totals=tuple(term(name, label) for name, label in TOTAL_TERMS) if complete else None,
            known_subtotal=None if complete else AmountView.of(result.known_subtotal),
            unknown_lines=tuple(
                UnknownLineView(item=u.item, label=u.label, reason=u.reason) for u in result.unknown_lines
            ),
            contribution_before_business_tax=(
                AmountView.of(result.contribution_before_business_tax)
                if complete
                else AmountView.unknown(unknown_reason, currency=currency)
            ),
            assumptions=result.assumptions[:100],
        )


class ScenarioCheck(ViewModel):
    scenario: ScenarioName
    would_meet: bool | None


class ThresholdView(ViewModel):
    """Minimum-contribution threshold. ``PROPOSED`` until the owner approves it (spec 18, 32)."""

    threshold: AmountView
    approval_status: Literal["unapproved", "approved"]
    label: Literal["PROPOSED", "APPROVED"]
    proposed_only: bool
    would_meet: bool | None
    would_meet_by_scenario: tuple[ScenarioCheck, ...] = Field(max_length=3)
    alert_eligible: bool
    blockers: tuple[str, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def _label(self) -> ThresholdView:
        if (self.label == "PROPOSED") != (self.approval_status == "unapproved"):
            raise ValueError("an unapproved threshold is labelled PROPOSED")
        if self.approval_status == "unapproved" and self.alert_eligible:
            raise ValueError("an unapproved threshold never makes a valuation alert eligible")
        return self

    @classmethod
    def of(cls, evaluation: ThresholdEvaluation, *, figures_visible: bool) -> ThresholdView:
        status: Literal["unapproved", "approved"] = (
            "approved" if evaluation.approval_status == "approved" else "unapproved"
        )
        by_scenario = tuple(
            ScenarioCheck(
                scenario=name,
                would_meet=evaluation.would_meet_by_scenario.get(name) if figures_visible else None,
            )
            for name in (ScenarioName.CONSERVATIVE, ScenarioName.BASE, ScenarioName.UPSIDE)
        )
        return cls(
            threshold=AmountView.of(evaluation.threshold),
            approval_status=status,
            label="APPROVED" if status == "approved" else "PROPOSED",
            proposed_only=evaluation.proposed_only,
            would_meet=evaluation.would_meet if figures_visible else None,
            would_meet_by_scenario=by_scenario,
            alert_eligible=evaluation.alert_eligible and status == "approved" and figures_visible,
            blockers=evaluation.blockers[:50],
        )


# --------------------------------------------------------------------------- tax


class TaxInputUsed(ViewModel):
    name: str = Field(max_length=100)
    value: str = Field(max_length=500)


class TaxComponentView(ViewModel):
    component_id: str = Field(max_length=64)
    label: str = Field(max_length=200)
    category: CostCategory
    kind: str = Field(max_length=40)
    status: Literal["resolved", "unknown", "not_applicable"]
    amount: AmountView
    inputs_used: tuple[TaxInputUsed, ...] = Field(max_length=100)
    missing_inputs: tuple[str, ...] = Field(max_length=100)
    warnings: tuple[str, ...] = Field(max_length=100)


class TaxView(ViewModel):
    """Import-tax calculation under one versioned rule set. Unapproved rules are labelled."""

    rule_set_id: str = Field(max_length=120)
    version: str = Field(max_length=80)
    rule_status: TaxRuleStatus
    rule_sha256: Sha256Hex | None
    is_fixture: bool
    production_ready: bool
    approval_label: str = Field(max_length=300)
    jurisdiction: str = Field(max_length=10)
    currency: CurrencyCode
    effective_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    engine_version: str = Field(max_length=80)
    complete: bool
    total_import_cost: AmountView | None
    known_subtotal: AmountView | None
    known_subtotal_label: Literal["known_subtotal"] = "known_subtotal"
    unknown_components: tuple[str, ...] = Field(max_length=100)
    missing_inputs: tuple[str, ...] = Field(max_length=100)
    components: tuple[TaxComponentView, ...] = Field(max_length=100)
    warnings: tuple[str, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def _total_only_when_complete(self) -> TaxView:
        if self.complete != (self.total_import_cost is not None):
            raise ValueError("total_import_cost exists exactly when the calculation is complete")
        if self.complete and self.known_subtotal is not None:
            raise ValueError("a complete calculation shows its total, not a known_subtotal")
        return self

    @classmethod
    def of(cls, calc: TaxCalculation) -> TaxView:
        if calc.production_ready:
            approval = f"{calc.rule_set_id}@{calc.version} is ACTIVE and approved"
        else:
            parts = [f"rule set is {calc.rule_status.value}"]
            if calc.is_fixture:
                parts.append("synthetic fixture")
            if not calc.complete:
                parts.append("calculation incomplete")
            approval = "Not production-supported: " + ", ".join(parts)
        components = []
        for comp in calc.components:
            if comp.status == ComponentStatus.RESOLVED:
                amount = AmountView.of(comp.amount)
            elif comp.status == ComponentStatus.NOT_APPLICABLE:
                amount = AmountView.not_applicable("rule predicate not met", currency=calc.currency)
            else:
                amount = AmountView.unknown("missing or unsupported input", currency=calc.currency)
            components.append(
                TaxComponentView(
                    component_id=comp.component_id,
                    label=comp.label,
                    category=comp.category,
                    kind=comp.kind,
                    status=comp.status.value,
                    amount=amount,
                    inputs_used=tuple(
                        TaxInputUsed(name=k, value=v[:500]) for k, v in sorted(comp.inputs_used.items())
                    ),
                    missing_inputs=comp.missing_inputs,
                    warnings=comp.warnings,
                )
            )
        return cls(
            rule_set_id=calc.rule_set_id,
            version=calc.version,
            rule_status=calc.rule_status,
            rule_sha256=calc.rule_sha256,
            is_fixture=calc.is_fixture,
            production_ready=calc.production_ready,
            approval_label=approval[:300],
            jurisdiction=calc.jurisdiction,
            currency=calc.currency,
            effective_date=calc.effective_date.isoformat(),
            engine_version=calc.engine_version,
            complete=calc.complete,
            total_import_cost=AmountView.of(calc.total_import_cost) if calc.complete else None,
            known_subtotal=None
            if calc.complete
            else AmountView.of(calc.known_subtotal, currency=calc.currency),
            unknown_components=calc.unknown_components,
            missing_inputs=calc.missing_inputs,
            components=tuple(components),
            warnings=calc.warnings,
        )


# --------------------------------------------------------------------------- valuation


class ContributionsView(ViewModel):
    conservative: AmountView
    base: AmountView
    upside: AmountView
    label: ContributionLabel = "estimated contribution before business tax"


class VersionsView(ViewModel):
    calculation_version: str = Field(max_length=80)
    cost_model_version: str | None = Field(max_length=80)
    tax_engine_version: str | None = Field(max_length=80)
    tax_rule: str | None = Field(max_length=200)
    cost_profile: str | None = Field(max_length=200)
    config_revision_id: str = Field(max_length=100)


class ValuationView(ViewModel):
    """``schemas/valuation.schema.json`` and the ``deals_get_valuation`` result."""

    valuation_id: UUID
    listing_id: UUID
    listing_revision_id: str = Field(max_length=100)
    listing_revision: int | None = Field(ge=1)
    state: ValuationState
    research_candidate: bool
    is_fixture: bool
    fixture_label: str | None = Field(max_length=200)
    alert_eligible: bool
    currency: CurrencyCode
    created_at: UtcDatetime
    expires_at: UtcDatetime | None
    stale_at: UtcDatetime | None
    stale_reason: str | None = Field(max_length=500)
    contribution_label: ContributionLabel = "estimated contribution before business tax"
    terminology_note: str = TERMINOLOGY_NOTE
    contributions: ContributionsView
    scenarios: tuple[ScenarioView, ...] = Field(max_length=3)
    purchase: PurchaseView | None
    proceeds: ProceedsView | None
    cost_lines: tuple[CostLineView, ...] = Field(max_length=200)
    tax: TaxView | None
    threshold: ThresholdView | None
    material_support: Literal["quote_supported", "approved_assumptions", "estimated", "incomplete"] | None
    unsupported_material: tuple[str, ...] = Field(max_length=50)
    eligibility: EligibilityState
    eligibility_profile: ProfileKey | None
    payable_eur: AmountView
    comparable: ComparableReference | None
    unknowns: tuple[str, ...] = Field(max_length=200)
    assumptions: tuple[str, ...] = Field(max_length=200)
    warnings: tuple[str, ...] = Field(max_length=200)
    correlation_notes: tuple[str, ...] = Field(max_length=50)
    dependency_fingerprint: Sha256Hex
    dependencies: ValuationDependencies
    versions: VersionsView

    @model_validator(mode="after")
    def _invariants(self) -> ValuationView:
        if self.dependency_fingerprint != self.dependencies.fingerprint():
            raise ValueError("dependency_fingerprint does not match the dependencies")
        figures = (self.contributions.conservative, self.contributions.base, self.contributions.upside)
        if self.state in _NO_FIGURE_STATES and any(f.status == "known" for f in figures):
            raise ValueError(
                f"a {self.state.value} valuation shows no contribution figures (unknown is not zero)"
            )
        if self.state in _FIGURE_STATES and any(
            f.status != "known" for f in (self.contributions.conservative, self.contributions.base)
        ):
            raise ValueError(f"a {self.state.value} valuation shows base and conservative contributions")
        if self.is_fixture and (self.fixture_label is None or self.alert_eligible):
            raise ValueError("fixture valuations are labelled and never alert eligible")
        if (self.state == ValuationState.STALE) != (self.stale_at is not None):
            raise ValueError("stale_at is set exactly when the state is stale")
        names = [s.scenario for s in self.scenarios]
        if names and sorted(names) != sorted(ScenarioName):
            raise ValueError("a valuation shows all three scenarios or none")
        return self

    @classmethod
    def of(
        cls,
        valuation: Valuation,
        *,
        valuation_id: UUID,
        listing_id: UUID,
        listing_revision: int | None = None,
        cost_lines: Sequence[CostLine] = (),
        purchase: PurchaseInput | None = None,
        proceeds: ProceedsEstimate | None = None,
    ) -> ValuationView:
        """Build the view from the domain valuation and (when available) its input lines."""
        figures_visible = valuation.base_contribution is not None and valuation.state not in _NO_FIGURE_STATES
        hidden_reason = (
            "valuation is " + valuation.state.value + ": " + "; ".join(valuation.unknowns)
            if valuation.unknowns
            else f"valuation is {valuation.state.value}"
        )[:500]
        scenarios: ScenarioSet | None = valuation.scenarios
        currency = valuation.currency

        def contribution(value: Money | None) -> AmountView:
            if value is None or not figures_visible:
                return AmountView.unknown(hidden_reason, currency=currency)
            return AmountView.of(value)

        tax = valuation.tax
        cost_profile = valuation.dependencies.cost_profile
        return cls(
            valuation_id=valuation_id,
            listing_id=listing_id,
            listing_revision_id=valuation.listing_revision_id,
            listing_revision=listing_revision,
            state=valuation.state,
            research_candidate=valuation.research_candidate,
            is_fixture=valuation.is_fixture,
            fixture_label=FIXTURE_LABEL if valuation.is_fixture else None,
            alert_eligible=valuation.alert_eligible,
            currency=currency,
            created_at=valuation.created_at,
            expires_at=valuation.expires_at,
            stale_at=valuation.stale_at,
            stale_reason=valuation.stale_reason,
            contributions=ContributionsView(
                conservative=contribution(valuation.conservative_contribution),
                base=contribution(valuation.base_contribution),
                upside=contribution(valuation.upside_contribution),
            ),
            scenarios=(
                ()
                if scenarios is None
                else tuple(
                    ScenarioView.of(s, figures_visible=figures_visible, hidden_reason=hidden_reason)
                    for s in scenarios.scenarios
                )
            ),
            purchase=None if purchase is None else PurchaseView.of(purchase),
            proceeds=None if proceeds is None else ProceedsView.of(proceeds),
            cost_lines=tuple(CostLineView.of(line, as_of=valuation.created_at) for line in cost_lines),
            tax=None if tax is None else TaxView.of(tax),
            threshold=(
                None
                if scenarios is None
                else ThresholdView.of(scenarios.threshold, figures_visible=figures_visible)
            ),
            material_support=None if scenarios is None else scenarios.material_support,
            unsupported_material=() if scenarios is None else scenarios.unsupported_material[:50],
            eligibility=valuation.screening.eligibility,
            eligibility_profile=valuation.screening.profile_key,
            payable_eur=AmountView.of(
                valuation.screening.eur_payable, unknown_reason="EUR payable amount unknown", currency="EUR"
            ),
            comparable=valuation.comparable,
            unknowns=valuation.unknowns[:200],
            assumptions=() if scenarios is None else scenarios.assumptions[:200],
            warnings=valuation.warnings[:200],
            correlation_notes=() if scenarios is None else scenarios.correlation_notes[:50],
            dependency_fingerprint=valuation.dependency_fingerprint,
            dependencies=valuation.dependencies,
            versions=VersionsView(
                calculation_version=valuation.calculation_version,
                cost_model_version=None if scenarios is None else scenarios.model_version,
                tax_engine_version=None if tax is None else tax.engine_version,
                tax_rule=None if tax is None else f"{tax.rule_set_id}@{tax.version}",
                cost_profile=(
                    None if cost_profile is None else f"{cost_profile.profile_key}@v{cost_profile.version}"
                ),
                config_revision_id=valuation.dependencies.config_revision_id,
            ),
        )
