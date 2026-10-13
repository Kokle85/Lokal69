"""Import-cost calculator read model: customs duty, motor-vehicle tax (ДМВ) and import VAT for
one vehicle (``domain.import_estimate.ImportCostEstimate`` on the wire).

Conventions are those of ``views.valuations.TaxView``: decimals are strings, an unknown amount
is ``{"status": "unknown", "amount": null}`` with a reason (never 0), lists are bounded.

- ``mode``: ``active`` (an ACTIVE, owner-approved rule set; the same rules valuations use),
  ``indicative_preview`` (no ACTIVE rule set applies: the newest unapproved rule set, labelled
  with the MK/EN ``banner``; ``production_ready`` is always false and the totals are named
  ``indicative_total`` / ``indicative_total_eur`` / ``indicative_landed_cost_eur``) or
  ``unavailable`` (no rule set at all; every import charge unknown, no scenarios).
- Display only: an estimate is never stored as a valuation, a cost line or an alert
  (``usage_note``). Helper components (customs value in the rule currency, the VAT base) are
  flagged ``intermediate`` and never counted in a total.
- ``facts`` are the vehicle facts the estimate used and ``fact_sources`` where each came from
  (the listing, the request, or the builder's default such as "no proof of origin").
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from pydantic import Field, model_validator

from suv_deals.domain.enums import CostCategory, Fuel, TaxRuleStatus
from suv_deals.domain.import_estimate import (
    CategoryEstimate,
    Co2Document,
    EstimateMode,
    EstimateScenario,
    EstimateScenarioName,
    ImportCostEstimate,
    ImporterStatus,
    OriginProofKind,
    PriceBasis,
    VehicleImportFacts,
)
from suv_deals.domain.money import CurrencyCode, FxRate, Money
from suv_deals.domain.tax_engine import ComponentStatus
from suv_deals.views.common import AmountView, DecimalStr, FxRateView, Sha256Hex, ViewModel, decimal_str
from suv_deals.views.valuations import TaxComponentView

USAGE_NOTE: Final = (
    "Display only: an import-cost estimate is never stored as a valuation, a cost line or an alert. "
    "Not a customs assessment; Customs decides the customs value, origin and tariff code."
)
FIXTURE_LABEL: Final = "SYNTHETIC FIXTURE listing: never a real opportunity."
_DATE: Final = r"^\d{4}-\d{2}-\d{2}$"
_INDICATIVE: Final = ("indicative_total", "indicative_total_eur", "indicative_landed_cost_eur")
_FINAL: Final = ("total", "total_eur", "landed_cost_eur")
_CV_UNKNOWN: Final = "customs value unknown: the price, the transport or the customs rate is missing"
_PREVIEW_STATUSES: Final = frozenset(
    {TaxRuleStatus.APPROVED, TaxRuleStatus.UNDER_REVIEW, TaxRuleStatus.DRAFT}
)

FactSource = Literal["listing", "request", "default"]
CustomsFxSource = Literal["recorded", "caller_supplied", "not_needed", "none"]


def _iso(day: date | None) -> str | None:
    return None if day is None else day.isoformat()


def _fx(rate: FxRate | None) -> FxRateView | None:
    return None if rate is None else FxRateView.of(rate)


def _texts(values: tuple[str, ...], limit: int, size: int = 500) -> tuple[str, ...]:
    return tuple(v[:size] for v in values[:limit])


# --------------------------------------------------------------------------- parts


class ImportBannerView(ViewModel):
    """The label every surface shows with a preview or an unavailable result (MK and EN)."""

    mk: str = Field(max_length=500)
    en: str = Field(max_length=500)


class ImportRuleView(ViewModel):
    """The rule set behind the figures. ``content_sha256`` is the recorded hash or, for an
    unsealed draft, the hash computed from its content (``sha256_recorded`` false)."""

    rule_set_id: str = Field(max_length=120)
    version: str = Field(max_length=80)
    status: TaxRuleStatus
    jurisdiction: str = Field(max_length=10)
    currency: CurrencyCode | None
    valid_from: str | None = Field(pattern=_DATE)
    valid_to: str | None = Field(pattern=_DATE)
    content_sha256: Sha256Hex
    sha256_recorded: bool


class ImportCategoryView(ViewModel):
    """One import category in the rule currency, and in EUR at the customs rate."""

    category: CostCategory
    status: Literal["resolved", "unknown", "not_applicable"]
    amount: AmountView
    amount_eur: AmountView
    component_ids: tuple[str, ...] = Field(max_length=100)

    @classmethod
    def of(cls, row: CategoryEstimate, *, currency: str | None) -> ImportCategoryView:
        reason = row.reason or None
        if row.status == ComponentStatus.RESOLVED:
            amount = AmountView.of(row.amount, unknown_reason=reason, currency=currency)
            amount_eur = AmountView.of(
                row.amount_eur, unknown_reason="no usable customs rate for the EUR equivalent", currency="EUR"
            )
        elif row.status == ComponentStatus.NOT_APPLICABLE:
            why = (reason or "no component of this category applies")[:500]
            amount = AmountView.not_applicable(why, currency=currency)
            amount_eur = AmountView.not_applicable(why, currency="EUR")
        else:
            why = (reason or "a component or a required input of this category is unknown")[:500]
            amount = AmountView.unknown(why, currency=currency)
            amount_eur = AmountView.unknown(why, currency="EUR")
        return cls(
            category=row.category,
            status=row.status.value,
            amount=amount,
            amount_eur=amount_eur,
            component_ids=row.component_ids[:100],
        )


class ImportScenarioView(ViewModel):
    """One labelled scenario (``legal_base`` first). In ``active`` mode the totals are ``total``,
    ``total_eur`` and ``landed_cost_eur`` (the ``indicative_*`` fields are null); in a preview
    the reverse. A total is known only when the calculation is complete; otherwise it is
    unknown and ``known_subtotal`` (labelled, never a total) shows what resolved."""

    name: EstimateScenarioName
    label: str = Field(max_length=300)
    complete: bool
    production_ready: bool
    customs_value: AmountView
    customs_value_basis: str | None = Field(max_length=300)
    assumptions: tuple[str, ...] = Field(max_length=20)
    components: tuple[TaxComponentView, ...] = Field(max_length=100)
    categories: tuple[ImportCategoryView, ...] = Field(max_length=4)
    total: AmountView | None
    total_eur: AmountView | None
    landed_cost_eur: AmountView | None
    indicative_total: AmountView | None
    indicative_total_eur: AmountView | None
    indicative_landed_cost_eur: AmountView | None
    known_subtotal: AmountView | None
    known_subtotal_label: Literal["known_subtotal"] = "known_subtotal"
    unknown_components: tuple[str, ...] = Field(max_length=100)
    missing_inputs: tuple[str, ...] = Field(max_length=100)
    warnings: tuple[str, ...] = Field(max_length=100)
    effective_date: str = Field(pattern=_DATE)
    customs_fx_rate: FxRateView | None
    engine_version: str = Field(max_length=80)
    calculation_sha256: Sha256Hex

    @model_validator(mode="after")
    def _totals(self) -> ImportScenarioView:
        final = [getattr(self, f) for f in _FINAL]
        indicative = [getattr(self, f) for f in _INDICATIVE]
        if all(v is None for v in final) == all(v is None for v in indicative):
            raise ValueError("a scenario carries either the final totals or the indicative totals")
        total = self.total or self.indicative_total
        if total is None or any(v is None for v in (final if self.total else indicative)):
            raise ValueError("a scenario carries all three totals of its kind")
        if self.complete != (total.status == "known"):
            raise ValueError("a total is known exactly when the calculation is complete")
        if self.complete == (self.known_subtotal is not None):
            raise ValueError("an incomplete scenario shows a known_subtotal instead of a total")
        if self.production_ready and (not self.complete or self.total is None):
            raise ValueError("only a complete scenario under ACTIVE rules is production-ready")
        return self

    @classmethod
    def of(
        cls, scenario: EstimateScenario, *, mode: EstimateMode, currency: str | None
    ) -> ImportScenarioView:
        preview = mode == "indicative_preview"
        incomplete = "the calculation is incomplete: " + ", ".join(
            [*scenario.unknown_components, *(f"missing {m}" for m in scenario.missing_inputs)]
        )
        no_rate = "no usable customs rate for the EUR equivalent"
        landed = "needs the import total, the EUR price and the transport amount"

        def amount(value: Money | None, unknown_reason: str, unit: str | None) -> AmountView:
            reason = incomplete if not scenario.complete else unknown_reason
            return AmountView.of(value, unknown_reason=reason[:500], currency=unit)

        def pick(final: Money | None, indicative: Money | None) -> Money | None:
            return indicative if preview else final

        totals = {
            "total": amount(pick(scenario.total, scenario.indicative_total), incomplete, currency),
            "total_eur": amount(pick(scenario.total_eur, scenario.indicative_total_eur), no_rate, "EUR"),
            "landed_cost_eur": amount(
                pick(scenario.landed_cost_eur, scenario.indicative_landed_cost_eur), landed, "EUR"
            ),
        }
        named = {(f"indicative_{k}" if preview else k): v for k, v in totals.items()}
        return cls(
            name=scenario.name,
            label=scenario.label[:300],
            complete=scenario.complete,
            production_ready=scenario.production_ready and not preview,
            customs_value=AmountView.of(
                scenario.customs_value,
                unknown_reason=_CV_UNKNOWN,
            ),
            customs_value_basis=scenario.customs_value_basis,
            assumptions=_texts(scenario.assumptions, 20),
            components=tuple(TaxComponentView.of(c, currency=currency) for c in scenario.components),
            categories=tuple(ImportCategoryView.of(row, currency=currency) for row in scenario.categories),
            total=named.get("total"),
            total_eur=named.get("total_eur"),
            landed_cost_eur=named.get("landed_cost_eur"),
            indicative_total=named.get("indicative_total"),
            indicative_total_eur=named.get("indicative_total_eur"),
            indicative_landed_cost_eur=named.get("indicative_landed_cost_eur"),
            known_subtotal=None
            if scenario.complete
            else AmountView.of(scenario.known_subtotal, unknown_reason="nothing resolved", currency=currency),
            unknown_components=scenario.unknown_components[:100],
            missing_inputs=scenario.missing_inputs[:100],
            warnings=_texts(scenario.warnings, 100),
            effective_date=scenario.effective_date.isoformat(),
            customs_fx_rate=None
            if scenario.customs_fx_rate is None
            else FxRateView.of(scenario.customs_fx_rate),
            engine_version=scenario.engine_version,
            calculation_sha256=scenario.calculation_sha256,
        )


class ImportGatesView(ViewModel):
    """Whether the vehicle can be imported at all (separate from the charges)."""

    importable: Literal["yes", "no", "unknown"]
    reason: str = Field(max_length=500)
    euro_class: int | None = Field(ge=1, le=7)
    min_euro: int = Field(ge=1, le=7)
    legal_basis: str = Field(max_length=300)


class UncertainItemView(ViewModel):
    """A rule-set statement or component marked UNCERTAIN (to confirm before approval)."""

    source: Literal["component", "rule_set"]
    component_id: str | None = Field(max_length=64)
    text: str = Field(max_length=1200)
    applies: bool


class ImportListingView(ViewModel):
    """The listing whose facts were used (its current, promoted, non-quarantined revision)."""

    listing_id: UUID
    revision_id: UUID
    revision_number: int = Field(ge=1)
    source_key: str = Field(max_length=80)
    is_fixture: bool
    fixture_label: str | None = Field(max_length=200)
    screened: bool


class Co2ReadingView(ViewModel):
    cycle: str = Field(max_length=40)
    g_km: DecimalStr


class ImportFactsView(ViewModel):
    """The vehicle facts the estimate used (after request overrides)."""

    price: AmountView
    price_basis: PriceBasis
    declaration_date: str | None = Field(pattern=_DATE)
    fuel: Fuel
    co2: tuple[Co2ReadingView, ...] = Field(max_length=3)
    co2_document: Co2Document | None
    co2_round_up_confirmed: bool
    first_registration: str | None = Field(max_length=10)
    emissions_class: str | None = Field(max_length=40)
    origin_proof_kind: OriginProofKind
    origin_proof_country: str | None = Field(pattern=r"^[A-Z]{2}$")
    origin_proof_evidence_ref: str | None = Field(max_length=200)
    hs_code: str | None = Field(max_length=16)
    hs_evidence_ref: str | None = Field(max_length=200)
    transport_after_entry_eur: DecimalStr | None
    transport_after_entry_separately_invoiced: bool
    catalogue_value_eur: DecimalStr | None
    importer_status: ImporterStatus | None
    mileage_km: DecimalStr | None
    seller_country: str | None = Field(pattern=r"^[A-Z]{2}$")
    engine_displacement_cm3: DecimalStr | None
    power_kw: DecimalStr | None

    @classmethod
    def of(cls, facts: VehicleImportFacts) -> ImportFactsView:
        def dec(value: Decimal | None) -> str | None:
            return None if value is None else decimal_str(value)

        price = facts.price
        return cls(
            price=AmountView.of(
                None if price is None else Money(amount=price.amount, currency=price.currency),
                unknown_reason="purchase price unknown",
            ),
            price_basis=facts.price_basis,
            declaration_date=_iso(facts.declaration_date),
            fuel=facts.fuel,
            co2=tuple(Co2ReadingView(cycle=r.cycle.value, g_km=decimal_str(r.g_km)) for r in facts.co2),
            co2_document=facts.co2_document,
            co2_round_up_confirmed=facts.co2_round_up_confirmed,
            first_registration=facts.first_registration.value,
            emissions_class=facts.emissions_class,
            origin_proof_kind=facts.origin_proof.kind,
            origin_proof_country=facts.origin_proof.country,
            origin_proof_evidence_ref=facts.origin_proof.evidence_ref,
            hs_code=facts.hs_code,
            hs_evidence_ref=facts.hs_evidence_ref,
            transport_after_entry_eur=dec(facts.transport_after_entry_eur),
            transport_after_entry_separately_invoiced=facts.transport_after_entry_separately_invoiced,
            catalogue_value_eur=dec(facts.catalogue_value_eur),
            importer_status=facts.importer_status,
            mileage_km=dec(facts.mileage_km),
            seller_country=facts.seller_country,
            engine_displacement_cm3=dec(facts.engine_displacement_cm3),
            power_kw=dec(facts.power_kw),
        )


class ImportFactSourceView(ViewModel):
    fact: str = Field(max_length=60)
    source: FactSource


# --------------------------------------------------------------------------- result


class ImportCostEstimateView(ViewModel):
    """The import-cost calculator result (``deals_estimate_import_costs`` data)."""

    estimator_version: str = Field(max_length=80)
    mode: EstimateMode
    production_ready: bool
    usage_note: str = USAGE_NOTE
    banner: ImportBannerView | None
    rule: ImportRuleView | None
    selection_reason: str = Field(max_length=1000)
    considered: tuple[str, ...] = Field(max_length=100)
    jurisdiction: str = Field(max_length=10)
    vehicle_category: str = Field(max_length=80)
    declaration_date: str = Field(pattern=_DATE)
    currency: CurrencyCode | None
    customs_fx_rate: FxRateView | None
    customs_fx_source: CustomsFxSource
    eur_basis: str = Field(max_length=500)
    landed_cost_note: str = Field(max_length=500)
    listing: ImportListingView | None
    facts: ImportFactsView
    fact_sources: tuple[ImportFactSourceView, ...] = Field(max_length=40)
    scenarios: tuple[ImportScenarioView, ...] = Field(max_length=3)
    assumptions: tuple[str, ...] = Field(max_length=100)
    warnings: tuple[str, ...] = Field(max_length=100)
    notes: tuple[str, ...] = Field(max_length=20)
    unverified_inputs: tuple[str, ...] = Field(max_length=20)
    uncertain_items: tuple[UncertainItemView, ...] = Field(max_length=100)
    gates: ImportGatesView
    missing_inputs: tuple[str, ...] = Field(max_length=100)
    missing_facts: tuple[str, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def _labelled(self) -> ImportCostEstimateView:
        if self.mode == "unavailable":
            if self.rule is not None or self.scenarios or self.production_ready:
                raise ValueError(
                    "an unavailable estimate has no rule, no scenarios and is not production-ready"
                )
        elif self.rule is None or not self.scenarios:
            raise ValueError(f"an {self.mode} estimate names its rule set and has scenarios")
        if self.mode == "indicative_preview":
            if self.production_ready or self.banner is None:
                raise ValueError(
                    "an indicative preview is never production-ready and always shows the banner"
                )
            if self.rule is None or self.rule.status not in _PREVIEW_STATUSES:
                raise ValueError("an indicative preview runs on an approved, under-review or draft rule set")
            if any(s.total is not None for s in self.scenarios):
                raise ValueError("an indicative preview names its totals indicative_total")
        if self.mode == "active":
            if self.rule is None or self.rule.status != TaxRuleStatus.ACTIVE:
                raise ValueError("an active estimate runs on an ACTIVE rule set")
            if any(s.indicative_total is not None for s in self.scenarios):
                raise ValueError("an active estimate never carries indicative totals")
        if self.production_ready and (self.mode != "active" or not self.scenarios[0].production_ready):
            raise ValueError("production_ready follows the legal-base scenario of an active estimate")
        if self.listing is not None and self.listing.is_fixture != (self.listing.fixture_label is not None):
            raise ValueError("a fixture-lineage listing is labelled, and only such a listing")
        return self

    @classmethod
    def of(
        cls,
        estimate: ImportCostEstimate,
        *,
        facts: VehicleImportFacts,
        fact_sources: Mapping[str, FactSource],
        customs_fx_source: CustomsFxSource,
        listing: ImportListingView | None = None,
    ) -> ImportCostEstimateView:
        rule = estimate.rule
        banner = estimate.banner
        return cls(
            estimator_version=estimate.estimator_version,
            mode=estimate.mode,
            production_ready=estimate.production_ready,
            banner=None if banner is None else ImportBannerView(mk=banner.mk, en=banner.en),
            rule=None
            if rule is None
            else ImportRuleView(
                rule_set_id=rule.rule_set_id,
                version=rule.version,
                status=rule.status,
                jurisdiction=rule.jurisdiction,
                currency=rule.currency,
                valid_from=_iso(rule.valid_from),
                valid_to=_iso(rule.valid_to),
                content_sha256=rule.content_sha256,
                sha256_recorded=rule.sha256_recorded,
            ),
            selection_reason=estimate.selection_reason[:1000],
            considered=_texts(estimate.considered, 100),
            jurisdiction=estimate.jurisdiction,
            vehicle_category=estimate.vehicle_category,
            declaration_date=estimate.declaration_date.isoformat(),
            currency=estimate.currency,
            customs_fx_rate=None
            if estimate.customs_fx_rate is None
            else FxRateView.of(estimate.customs_fx_rate),
            customs_fx_source=customs_fx_source,
            eur_basis=estimate.eur_basis[:500],
            landed_cost_note=estimate.landed_cost_note[:500],
            listing=listing,
            facts=ImportFactsView.of(facts),
            fact_sources=tuple(
                ImportFactSourceView(fact=name, source=source) for name, source in fact_sources.items()
            )[:40],
            scenarios=tuple(
                ImportScenarioView.of(s, mode=estimate.mode, currency=estimate.currency)
                for s in estimate.scenarios
            ),
            assumptions=_texts(estimate.assumptions, 100),
            warnings=_texts(estimate.warnings, 100),
            notes=_texts(estimate.notes, 20),
            unverified_inputs=_texts(estimate.unverified_inputs, 20),
            uncertain_items=tuple(
                UncertainItemView(
                    source=item.source, component_id=item.component_id, text=item.text, applies=item.applies
                )
                for item in estimate.uncertain_items[:100]
            ),
            gates=ImportGatesView(
                importable=estimate.gates.importable,
                reason=estimate.gates.reason,
                euro_class=estimate.gates.euro_class,
                min_euro=estimate.gates.min_euro,
                legal_basis=estimate.gates.legal_basis[:300],
            ),
            missing_inputs=estimate.missing_inputs[:100],
            missing_facts=estimate.missing_facts[:50],
        )


__all__ = [
    "FIXTURE_LABEL",
    "USAGE_NOTE",
    "Co2ReadingView",
    "CustomsFxSource",
    "FactSource",
    "ImportBannerView",
    "ImportCategoryView",
    "ImportCostEstimateView",
    "ImportFactSourceView",
    "ImportFactsView",
    "ImportGatesView",
    "ImportListingView",
    "ImportRuleView",
    "ImportScenarioView",
    "UncertainItemView",
]
