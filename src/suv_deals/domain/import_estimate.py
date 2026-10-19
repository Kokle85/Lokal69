# ruff: noqa: RUF001, RUF002
# (RUF001/RUF002: the Macedonian legal references and the preview banner are Cyrillic by design.)
"""Import-cost estimate for one vehicle: vehicle facts -> ``TaxInputs`` -> tax engine.

Pure: no I/O, no clock, no database. The caller supplies the rule sets, the transport cost
line, the customs exchange rate and ``as_of``.

- No tax rate, coefficient or threshold lives here. Every amount comes from the selected
  rule set through ``tax_engine.calculate``; this module only states facts and labels.
- Unknown is never zero. A missing fact leaves its input missing; the engine then reports
  the dependent components unknown and the result incomplete, and ``missing_facts`` names
  what to supply.
- Nothing is inferred: no origin (default: no proof, so standard duty), no VAT base, no
  tariff code (a caller-attested HS 8703 code is only passed through) and no WLTP <-> NEDC
  conversion. A CO2 figure missing from the documents is never replaced by a deemed value
  here; only an explicit ``none_available`` statement lets the rule set apply its own.
- Transport is the cost-profile ``transport`` line (owner decision: a fixed amount per
  vehicle), never a constant in code. By default all of it is transport to the place of
  entry and so part of the customs value (Царински закон чл. 35(1)(д)); only an inland leg
  that is separately invoiced leaves the customs value (чл. 36(а)) and enters the import
  VAT base alone (ЗДДВ чл. 21(2)(2)).
- Two values per vehicle (owner decision 2026-10-13). ``price`` is the REAL purchase price
  (реална набавна цена): the landed cost, and so the profit, always use it. The optional
  ``estimated_customs_value_eur`` (проценета царинска вредност, e.g. a SchwackeNet catalogue
  value; the vehicle only, without transport) with its ``estimated_customs_value_source`` is
  what the import charges of the ``primary`` scenario are computed on: the estimate plus the
  transport to the MK place of entry (whether Customs adds that transport to a catalogue value
  is UNCERTAIN). Without an estimate the customs value is the transaction value, the price
  actually paid plus that transport (Царински закон чл. 28, 35(1)(д)). With an estimate the
  ``transaction_value`` scenario shows the alternative where Customs accepts the price paid,
  its primary method; another method applies only on founded doubt, in the legal order. A
  car bought well under the usual market price is the case this separates.
- Whole units (owner decision 2026-10-13, "без децимали"): the rule set rounds every MKD
  component and the total to whole denars; the EUR amounts of the result (EUR equivalents,
  purchase price, customs value for duties, landed cost) are rounded half-up to whole euros
  from the exact figures, so a rounded part may differ from the rounded sum by EUR 1.
- Modes. ``active``: an ACTIVE, owner-approved rule set (the same production selection the
  valuations use). ``indicative_preview``: no ACTIVE set applies, so the newest
  unapproved set is used, labelled with a banner, never production-ready, and its totals
  are named ``indicative_total``. ``unavailable``: no rule set at all; import charges stay
  unknown. An estimate never carries a ``TaxCalculation`` and so can never become cost
  lines, a valuation or an alert (``costs.tax_cost_lines`` refuses anything else).
- The valuation pipeline shares the BUILDER only (``facts_from_listing``,
  ``build_tax_inputs``, ``single_transport_line``) on its own ACTIVE selection, so a
  valuation computes what the calculator shows; ``BuiltTaxInputs.unverified_inputs`` names
  the claims (advertised price, listing CO2, caller attestations) that keep such amounts
  from ever being rule-supported.
"""

from __future__ import annotations

import calendar
import decimal
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.costs import CostLine, SourceLabel
from suv_deals.domain.enums import Co2Cycle, CostCategory, CostLineStatus, Fuel, Precision, TaxRuleStatus
from suv_deals.domain.listings import NormalizedListing, PartialDate
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.tax_engine import (
    ENGINE_VERSION,
    IMPORT_CATEGORIES,
    MAX_INPUT_AMOUNT,
    Classification,
    ComponentResult,
    ComponentStatus,
    IncludedCost,
    OriginProof,
    OriginProofStatus,
    RuleSelection,
    RuleSet,
    TaxCalculation,
    TaxInputs,
    calculate,
    compute_rule_set_sha256,
    convert_with_customs_rate,
    input_spec,
    select_preview_rule_set,
    select_rule_set,
)
from suv_deals.errors import ValidationFailed

ESTIMATE_VERSION: Final = "import-estimate/1.0.0"
DEFAULT_JURISDICTION: Final = "MK"
DEFAULT_VEHICLE_CATEGORY: Final = "passenger_car"
#: Same convention as the valuation pipeline: a vehicle with this mileage is used.
USED_VEHICLE_MIN_KM: Final = Decimal(1000)
#: Sanity bound of a stated mileage (a larger figure is a parse error, never a fact).
MAX_MILEAGE_KM: Final = Decimal(100_000_000)
#: Most fraction digits a listing's CO2 figure may carry; more is a parse artefact, never a fact
#: (and ``1e-100000000`` would expand to 100 MB wherever it is shown in plain notation).
MAX_CO2_FRACTION_DIGITS: Final = 6
#: Fuels that burn fuel in an engine: such a car cannot emit under 1 g/km, so a lower figure is
#: a placeholder or a parse artefact (CO2 not established), never a fact.
COMBUSTION_FUELS: Final = frozenset(
    {
        Fuel.PETROL,
        Fuel.DIESEL,
        Fuel.LPG,
        Fuel.CNG,
        Fuel.HYBRID_PETROL,
        Fuel.HYBRID_DIESEL,
        Fuel.PLUGIN_HYBRID,
    }
)

Co2Document = Literal[
    "coc",
    "registration_consent",
    "manufacturer_certificate",
    "technical_assessment_certificate",
    "listing_claim_unverified",
    "none_available",
]
OriginProofKind = Literal["none", "unknown", "eu", "efta", "tr", "uk", "cefta", "ua", "us"]
ImporterStatus = Literal["private", "non_vat_business", "vat_dealer"]
#: How a caller states a price: an invoice/contract price or an advertised one.
PriceBasis = Literal["invoice", "advertised"]
#: Basis of the REAL purchase price: a stated basis (``PriceBasis``) or a value the owner recorded
#: for the listing (an actual purchase, a quote or an estimate of the purchase price).
PurchasePriceBasis = Literal[
    "invoice", "advertised", "recorded_actual", "recorded_quote", "recorded_estimate"
]
#: What the customs value for the import charges rests on: an estimated customs value (plus the
#: transport to the place of entry) or the transaction value (the price paid plus that transport).
CustomsValueBasis = Literal["estimated", "transaction_value"]
EstimateMode = Literal["active", "indicative_preview", "unavailable"]
EstimateScenarioName = Literal["primary", "standard_duty_if_proof_rejected", "transaction_value"]
Importable = Literal["yes", "no", "unknown"]

#: Partner code of a proof kind when the agreement has a single partner. EFTA and CEFTA
#: proofs must state the country. The rule set, not this table, decides the duty.
_PROOF_COUNTRY: Final[Mapping[str, str]] = {"eu": "EU", "tr": "TR", "uk": "GB", "ua": "UA", "us": "US"}
_CO2_CYCLE_PREFERENCE: Final = (Co2Cycle.WLTP, Co2Cycle.NEDC, Co2Cycle.NEDC_CORRELATED, Co2Cycle.UNKNOWN)
#: Statuses of a transport cost line that carry an amount.
_TRANSPORT_STATUSES: Final = frozenset(
    {CostLineStatus.ESTIMATED, CostLineStatus.QUOTED, CostLineStatus.ACTUAL}
)
_CUSTOMS_VALUE_REFERENCE: Final = "Царински закон чл. 35(1)(д)"
_TRANSACTION_VALUE_REFERENCE: Final = "Царински закон чл. 28"
_INLAND_LEG_REFERENCE: Final = "ЗДДВ чл. 21(2)(2)"
#: How each purchase-price basis is named in labels and bases.
_PRICE_WORDS: Final[Mapping[str, str]] = {
    "invoice": "invoice",
    "advertised": "advertised price",
    "recorded_actual": "recorded actual purchase price",
    "recorded_quote": "recorded purchase quote",
    "recorded_estimate": "recorded purchase-price estimate",
}
#: Default source label of the purchase price per basis (a caller or the service may name one).
_PRICE_SOURCES: Final[Mapping[str, str]] = {
    "invoice": "invoice or contract price",
    "advertised": "advertised price (a claim, not an invoice)",
    "recorded_actual": "recorded actual purchase price",
    "recorded_quote": "recorded purchase quote (not yet paid)",
    "recorded_estimate": "recorded purchase-price estimate",
}
#: Bases whose price is a claim, not evidence of the price paid: the amounts computed from them
#: are never rule-supported (``unverified_inputs``), and they stand in for the transaction value.
_CLAIMED_PRICE: Final[Mapping[str, str]] = {
    "advertised": "price: advertised price, not an invoice or contract price",
    "recorded_quote": "price: recorded quote, not a paid invoice or contract price",
    "recorded_estimate": "price: recorded estimate, not an invoice or contract price",
}
_ESTIMATE_TRANSPORT_UNCERTAIN: Final = (
    "ESTIMATED_CUSTOMS_VALUE_TRANSPORT_ADDED: UNCERTAIN - confirm whether Customs adds the transport "
    "to the MK place of entry to an estimated (catalogue) customs value; it is added here "
    "(Царински закон чл. 35(1)(д))"
)
_EURO_CLASS = re.compile(
    r"^\s*(?:euro|eu)?\s*[-_]?\s*(?:(?P<num>[1-7])(?!\d)|(?P<roman>vi|iv|v|iii|ii|i)(?![a-z]))",
    re.IGNORECASE,
)
_ROMAN: Final[Mapping[str, int]] = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6}
_COUNTRY: Final = r"^[A-Z]{2}$"
#: HS 2022 subheadings of heading 8703 -> the drive types they cover (nomenclature, not a rate).
#: An attested tariff code decides the motor-vehicle-tax treatment (ЗДМВ чл. 6(3)(1), 9(3)), so
#: a fuel that contradicts it is never silently used. 870310 and 870390 are not constrained.
_HS_FUELS: Final[Mapping[str, frozenset[Fuel]]] = {
    **dict.fromkeys(("870321", "870322", "870323", "870324"), frozenset({Fuel.PETROL, Fuel.LPG, Fuel.CNG})),
    **dict.fromkeys(("870331", "870332", "870333"), frozenset({Fuel.DIESEL})),
    "870340": frozenset({Fuel.HYBRID_PETROL}),
    "870350": frozenset({Fuel.HYBRID_DIESEL}),
    "870360": frozenset({Fuel.PLUGIN_HYBRID}),
    "870370": frozenset({Fuel.PLUGIN_HYBRID}),
    "870380": frozenset({Fuel.ELECTRIC}),
}
#: Wide context for exact sums; nothing is rounded here except EUR display amounts.
_CTX: Final = decimal.Context(prec=50)

#: Rule-set input -> the vehicle fact (or operator datum) that supplies it. The customs value
#: is not listed: the builder knows whether the price, the transport or the rate is missing.
_FACT_FOR_INPUT: Final[Mapping[str, str]] = {
    "customs_fx_rate": "customs_fx_rate",
    "declaration_date": "declaration_date",
    "preferential_origin_country": "origin_proof",
    "origin_evidence": "origin_proof",
    "fuel": "fuel",
    "co2_g_km": "co2",
    "co2_cycle": "co2",
    "co2_source_document": "co2_document",
    "classification": "hs_code",
    "vehicle_category": "hs_code",
    "vat_incidental_costs_total": "transport_after_entry_eur",
    "vehicle_age_years": "first_registration",
    "emissions_class": "emissions_class",
    "vehicle_condition": "mileage_km",
    "seller_country": "seller_country",
    "importer_status": "importer_status",
}

#: Preview banner wording per rule status (fit design 5).
_PREVIEW_RULE_WORDS: Final[Mapping[TaxRuleStatus, tuple[str, str]]] = {
    TaxRuleStatus.DRAFT: ("неодобрени нацрт-правила", "unapproved draft rules"),
    TaxRuleStatus.UNDER_REVIEW: ("неодобрени правила во преглед", "unapproved rules under review"),
    TaxRuleStatus.APPROVED: (
        "одобрени правила што сè уште не се активни",
        "approved rules that are not yet active",
    ),
}
LANDED_COST_NOTE: Final = (
    "real purchase price + transport + import charges (customs duty, motor-vehicle tax, import VAT), in "
    "EUR at the customs rate, rounded to whole euros; the charges follow the scenario's customs value, the "
    "purchase price is always the one actually paid; excludes customs broker/terminal fees, conformity "
    "confirmation, registration fees and MTPL insurance"
)


def _input_bound(name: str) -> Decimal:
    """The engine's own upper sanity bound of a numeric ``TaxInputs`` field (one source of truth)."""
    for item in TaxInputs.model_fields[name].metadata:
        bound = getattr(item, "le", None)
        if bound is not None:
            return Decimal(bound)
    raise AssertionError(f"TaxInputs.{name} has no upper bound")


class _Contract(BaseModel):
    """Frozen, closed contract base that refuses binary floats."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _refuse_floats(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, float):
                    raise ValueError(f"{key}: binary float is not allowed; use a decimal string")
        return data


# --------------------------------------------------------------------------- vehicle facts


class PriceFact(_Contract):
    """Purchase price as stated. Only EUR and MKD can become a customs value (a CHF price plus
    EUR transport would need two customs rates; a reference rate is never substituted)."""

    amount: Decimal = Field(ge=0, le=MAX_INPUT_AMOUNT)
    currency: Literal["EUR", "MKD", "CHF"]


class Co2Reading(_Contract):
    """A CO2 figure exactly as printed on the document, with its measurement cycle."""

    cycle: Co2Cycle
    g_km: Decimal = Field(ge=0, le=2000)


class OriginProofClaim(_Contract):
    """The proof of origin the importer holds. ``none`` (the default) means no proof, so the
    standard duty; ``unknown`` leaves the duty unknown. A claimed proof is assumed accepted by
    Customs and the standard-duty alternative is always shown next to it."""

    kind: OriginProofKind = "none"
    country: str | None = Field(default=None, pattern=_COUNTRY)
    evidence_ref: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _consistent(self) -> OriginProofClaim:
        if self.kind in ("none", "unknown") and (self.country or self.evidence_ref):
            raise ValueError(f"an origin proof of kind {self.kind!r} has no country or evidence")
        if self.kind in ("efta", "cefta") and self.country is None:
            raise ValueError(f"an {self.kind.upper()} proof must state its origin country")
        return self

    @property
    def claimed(self) -> bool:
        return self.kind not in ("none", "unknown")


class VehicleImportFacts(_Contract):
    """What is known about one vehicle and its import. Every field is optional; a missing fact
    stays unknown (never zero) and the estimate names it in ``missing_facts``.

    ``price`` is the REAL purchase price (the landed cost and the profit use it);
    ``estimated_customs_value_eur`` is the separate value of the vehicle for customs (without
    transport), used for the import charges when given, always with its source."""

    price: PriceFact | None = None
    price_basis: PurchasePriceBasis = "advertised"
    declaration_date: date | None = None
    fuel: Fuel = Fuel.UNKNOWN
    co2: tuple[Co2Reading, ...] = Field(default=(), max_length=3)
    co2_document: Co2Document | None = None
    co2_round_up_confirmed: bool = False
    first_registration: PartialDate = PartialDate()
    emissions_class: str | None = Field(default=None, min_length=1, max_length=40)
    origin_proof: OriginProofClaim = OriginProofClaim()
    hs_code: str | None = Field(default=None, min_length=4, max_length=16)
    hs_evidence_ref: str | None = Field(default=None, min_length=1, max_length=200)
    transport_after_entry_eur: Decimal | None = Field(default=None, ge=0, le=MAX_INPUT_AMOUNT)
    transport_after_entry_separately_invoiced: bool = False
    #: Estimated customs value of the VEHICLE in EUR, without transport (e.g. a SchwackeNet
    #: catalogue value): positive, at most two decimals, within the engine's input bound.
    estimated_customs_value_eur: Decimal | None = Field(
        default=None, gt=0, le=MAX_INPUT_AMOUNT, decimal_places=2
    )
    #: Where the estimate comes from (e.g. "SchwackeNet catalogue", "owner estimate"); required
    #: exactly when an estimate is given.
    estimated_customs_value_source: SourceLabel | None = None
    importer_status: ImporterStatus | None = None
    mileage_km: Decimal | None = Field(default=None, ge=0, le=MAX_MILEAGE_KM)
    seller_country: str | None = Field(default=None, pattern=_COUNTRY)
    #: Passed through to the engine (within its own sanity bounds); the MK passenger-car rules
    #: do not use them.
    engine_displacement_cm3: Decimal | None = Field(
        default=None, gt=0, le=_input_bound("engine_displacement_cm3")
    )
    power_kw: Decimal | None = Field(default=None, gt=0, le=_input_bound("power_kw"))

    @field_validator("hs_code")
    @classmethod
    def _hs6(cls, value: str | None) -> str | None:
        """Separators are stripped and the code is cut to HS-6; it is never inferred."""
        if value is None:
            return None
        digits = re.sub(r"[ .]", "", value)
        if not re.fullmatch(r"[0-9]{6,10}", digits):
            raise ValueError("hs_code must have 6-10 digits (spaces and dots allowed)")
        if not digits.startswith("8703"):
            raise ValueError(
                "only HS 8703 passenger cars are modelled; 8704 (e.g. double-cab pick-ups) and 9705 are not"
            )
        return digits[:6]

    @model_validator(mode="after")
    def _consistent(self) -> VehicleImportFacts:
        cycles = [reading.cycle for reading in self.co2]
        if len(cycles) != len(set(cycles)):
            raise ValueError("one CO2 reading per measurement cycle")
        split = self.transport_after_entry_eur is not None
        if split != self.transport_after_entry_separately_invoiced:
            raise ValueError(
                "transport_after_entry_eur is used only when the inland leg is separately invoiced "
                "(transport_after_entry_separately_invoiced: true), and then it is required"
            )
        if self.hs_evidence_ref is not None and self.hs_code is None:
            raise ValueError("hs_evidence_ref needs hs_code")
        if (self.estimated_customs_value_eur is None) != (self.estimated_customs_value_source is None):
            raise ValueError(
                "estimated_customs_value_eur and estimated_customs_value_source go together: an estimated "
                "customs value always names its source"
            )
        return self


def plausible_listing_co2(g_km: Decimal, fuel: Fuel) -> bool:
    """Whether a listing's CO2 figure can be a fact: finite, within the reading's bounds, at most
    ``MAX_CO2_FRACTION_DIGITS`` fraction digits, and not under 1 g/km for a combustion car."""
    exp = g_km.as_tuple().exponent
    if not isinstance(exp, int) or exp < -MAX_CO2_FRACTION_DIGITS:
        return False
    if not 0 <= g_km <= _input_bound("co2_g_km"):
        return False
    return not (fuel in COMBUSTION_FUELS and g_km < 1)


def facts_from_listing(
    listing: NormalizedListing, *, payable: Money | None, declaration_date: date | None = None
) -> VehicleImportFacts:
    """Facts a listing establishes. The price is the advertised payable amount (never an
    invoice); a listed CO2 figure is ``listing_claim_unverified``; origin stays "no proof".
    Callers override any fact with what they know better (invoice, COC, proof, HS code).

    Never raises for a listing value outside a fact's bounds (a parse error is not a fact):
    such a value is left unknown. So is a CO2 figure under 1 g/km for a combustion or hybrid
    car (a placeholder, see ``plausible_listing_co2``)."""
    vehicle = listing.vehicle
    price = None
    if (
        payable is not None
        and payable.currency in ("EUR", "MKD", "CHF")
        and 0 <= payable.amount <= MAX_INPUT_AMOUNT
    ):
        price = PriceFact(amount=payable.amount, currency=payable.currency)
    readings: tuple[Co2Reading, ...] = ()
    document: Co2Document | None = None
    if listing.co2.g_per_km is not None and plausible_listing_co2(listing.co2.g_per_km, vehicle.fuel):
        readings = (Co2Reading(cycle=listing.co2.cycle, g_km=listing.co2.g_per_km),)
        document = "listing_claim_unverified"
    mileage = vehicle.mileage_km
    displacement = vehicle.engine_displacement_cm3
    return VehicleImportFacts(
        price=price,
        price_basis="advertised",
        declaration_date=declaration_date,
        fuel=vehicle.fuel,
        co2=readings,
        co2_document=document,
        first_registration=vehicle.first_registration,
        emissions_class=listing.documentation.emissions_class or None,
        mileage_km=mileage if mileage is not None and mileage <= MAX_MILEAGE_KM else None,
        seller_country=listing.location.country,
        engine_displacement_cm3=None if displacement is None else Decimal(displacement),
        power_kw=None if vehicle.power_kw is None else Decimal(vehicle.power_kw),
    )


class EstimatePolicy(_Contract):
    """Presentation gates. They never change an amount (amounts come from the rule set only).

    - ``min_euro``: used cars below this Euro class cannot be imported or registered in
      practice (fact sheet 9; the legal act is UNCERTAIN - confirm). The calculation still runs.
    - ``oldtimer_min_age_years``: from this age the motor-vehicle-tax oldtimer exemption may
      apply (ЗДМВ чл. 17). It is not modelled; the estimate only warns.
    """

    min_euro: int = Field(default=4, ge=1, le=7)
    oldtimer_min_age_years: int = Field(default=35, ge=1, le=200)


DEFAULT_POLICY: Final = EstimatePolicy()


class ImportGates(_Contract):
    """Whether the vehicle can be imported at all (separate from the charges)."""

    importable: Importable
    reason: str = Field(min_length=3, max_length=500)
    euro_class: int | None = None
    min_euro: int
    legal_basis: str = "UNCERTAIN - confirm: legal act and gazette number of the Euro minimum (fact sheet 9)"


# --------------------------------------------------------------------------- builder


class ScenarioInputs(_Contract):
    """Engine inputs of one labelled scenario."""

    name: EstimateScenarioName
    label: str = Field(min_length=3, max_length=300)
    inputs: TaxInputs
    #: What this scenario's customs value rests on (the estimate or the transaction value).
    customs_value_kind: CustomsValueBasis
    assumptions: tuple[str, ...] = ()


class PurchasePrice(_Contract):
    """The REAL purchase price (реална набавна цена) in EUR: the basis of the landed cost and the
    profit in every scenario, never replaced by an estimated customs value. Exact in
    ``BuiltTaxInputs``; whole euros (half-up) in ``ImportCostEstimate``. Unknown (with
    ``reason``) when no price is given or it cannot be converted; never zero."""

    amount: Money | None
    #: ``None`` when no price is given.
    basis: PurchasePriceBasis | None
    source: str = Field(min_length=3, max_length=200)
    reason: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _explained(self) -> PurchasePrice:
        if self.amount is not None and self.amount.currency != "EUR":
            raise ValueError("the purchase price is stated in EUR")
        if (self.amount is None) == (self.reason is None):
            raise ValueError("an unknown purchase price carries a reason, a known one none")
        return self


class CustomsValueForDuties(_Contract):
    """The customs value the import charges of the ``primary`` scenario are computed on, in EUR,
    INCLUDING the transport to the MK place of entry: the estimated customs value plus that
    transport (``estimated``) or the transaction value (``transaction_value``). Exact in
    ``BuiltTaxInputs``; whole euros (half-up) in ``ImportCostEstimate``."""

    amount: Money | None
    basis: CustomsValueBasis
    source: str = Field(min_length=3, max_length=200)
    reason: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _explained(self) -> CustomsValueForDuties:
        if self.amount is not None and self.amount.currency != "EUR":
            raise ValueError("the customs value for duties is stated in EUR")
        if (self.amount is None) == (self.reason is None):
            raise ValueError("an unknown customs value carries a reason, a known one none")
        return self


class BuiltTaxInputs(_Contract):
    """``build_tax_inputs`` result: the primary scenario's inputs, every scenario (primary first)
    and the builder's assumptions, warnings and import gates."""

    inputs: TaxInputs
    scenarios: tuple[ScenarioInputs, ...]
    assumptions: tuple[str, ...]
    warnings: tuple[str, ...]
    gates: ImportGates
    #: The REAL purchase price in EUR for the landed cost (``None`` when unknown or not
    #: convertible); never the estimated customs value.
    price_eur: Money | None
    #: The real purchase price with its basis and source (exact).
    purchase_price: PurchasePrice
    #: The primary scenario's customs value in EUR with its basis and source (exact).
    customs_value_for_duties: CustomsValueForDuties
    #: The whole transport line amount (``None`` when the line is unusable).
    transport: Money | None
    #: Facts (or operator data) still needed, by the builder's own reasons.
    missing_facts: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    #: Inputs that are claims, not evidence (an advertised price, a listing's CO2 figure, a
    #: caller-attested proof of origin). Amounts computed from them are never presented as
    #: rule-supported (``costs.tax_cost_lines``), even under ACTIVE rules.
    unverified_inputs: tuple[str, ...] = ()
    #: The customs rate when it is valid on the declaration date (its month, or last month's
    #: when accepted for a preview); ``None`` when no rate was given or it is not effective.
    effective_customs_rate: FxRate | None = None

    def scenario(self, name: EstimateScenarioName) -> ScenarioInputs | None:
        return next((s for s in self.scenarios if s.name == name), None)


def declaration_date_for(facts: VehicleImportFacts, as_of: datetime) -> date:
    """The declaration date the estimate uses: the caller's, otherwise ``as_of``'s date."""
    return facts.declaration_date or ensure_utc(as_of).date()


def whole_units(money: Money) -> Money:
    """``money`` rounded half-up to whole units of its currency (owner decision 2026-10-13: amounts
    without decimals). For presentation only; every sum is formed from the exact figures first."""
    return Money(amount=money.amount.quantize(Decimal(1), rounding=ROUND_HALF_UP), currency=money.currency)


def plain_amount_text(money: Money) -> str:
    """An amount quoted in a text, without a needless fraction (owner decision 2026-10-13: amounts
    without decimals): ``3000.00`` reads ``3000``, while a real fraction stays exact (``2800.50``),
    because such a text quotes the figure a calculation actually used, never a rounded one."""
    quantized = money.quantized()
    if quantized.is_zero():
        return "0"
    whole = quantized.to_integral_value()
    return str(whole) if quantized == whole else str(quantized)


def _money_text(amount: Decimal, currency: str) -> str:
    return f"{plain_amount_text(Money(amount=amount, currency=currency))} {currency}"


def _within_bound(amount: Decimal) -> bool:
    """Whether a customs value stays inside the engine's input sanity bound."""
    return amount <= MAX_INPUT_AMOUNT


_OUT_OF_BOUND: Final = (
    "CUSTOMS_VALUE_OUT_OF_BOUNDS: {what} plus the transport exceeds the engine's input bound; the customs "
    "value is unknown"
)


def _month_end(day: date) -> date:
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def _next_month(day: date) -> tuple[int, int]:
    return (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)


def single_transport_line(lines: Iterable[CostLine]) -> CostLine:
    """The one cost-profile transport line that applies to a vehicle (``CostProfile.lines`` with
    its target scope). With none or several, an UNKNOWN line with the reason, so the customs
    value stays unknown rather than guessed or double counted."""
    transport = [line for line in lines if line.category == CostCategory.TRANSPORT]
    if len(transport) == 1:
        return transport[0]
    why = "no transport line" if not transport else f"{len(transport)} transport lines"
    return CostLine(
        category=CostCategory.TRANSPORT,
        label="Transport (cost profile)",
        status=CostLineStatus.UNKNOWN,
        currency="EUR",
        reason=f"the cost profile has {why} for this vehicle; the transport amount is unknown",
    )


def _transport_amount(line: CostLine) -> tuple[Decimal | None, list[str], list[str]]:
    """Amount (EUR), assumptions and warnings of the transport cost line."""
    if line.category != CostCategory.TRANSPORT:
        raise ValidationFailed(f"the transport line must have category transport, not {line.category.value}")
    if line.status not in _TRANSPORT_STATUSES or line.base is None:
        return None, [], [f"TRANSPORT_LINE_NOT_USABLE:{line.status.value}: transport cost unknown"]
    if line.base.currency != "EUR":
        return None, [], [f"TRANSPORT_LINE_CURRENCY_UNSUPPORTED:{line.base.currency}: only EUR is supported"]
    amount = _money_text(line.base.amount, "EUR")
    assumptions = [f"TRANSPORT_LINE:{amount} per vehicle ({line.status.value}: {line.label})"[:300]]
    if any(bound is not None and bound != line.base for bound in (line.low, line.high)):
        assumptions.append("TRANSPORT_BASE_AMOUNT_USED: the transport line's base amount is used")
    return line.base.amount, assumptions, []


def _euro_class(text: str | None) -> int | None:
    if text is None:
        return None
    match = _EURO_CLASS.match(text)
    if match is None:
        return None
    if match.group("num") is not None:
        return int(match.group("num"))
    return _ROMAN[match.group("roman").lower()]


def _gates(facts: VehicleImportFacts, policy: EstimatePolicy) -> ImportGates:
    euro = _euro_class(facts.emissions_class)
    if euro is None:
        return ImportGates(
            importable="unknown",
            reason=f"EURO_CLASS_UNKNOWN: emissions class not stated or not recognised; the Euro "
            f"{policy.min_euro} minimum cannot be checked",
            min_euro=policy.min_euro,
        )
    if euro < policy.min_euro:
        return ImportGates(
            importable="no",
            reason=f"EURO_CLASS_BELOW_MINIMUM: Euro {euro} is below the Euro {policy.min_euro} minimum for "
            "importing/registering a used car",
            euro_class=euro,
            min_euro=policy.min_euro,
        )
    return ImportGates(
        importable="yes",
        reason=f"Euro {euro} meets the Euro {policy.min_euro} minimum",
        euro_class=euro,
        min_euro=policy.min_euro,
    )


def _completed_years(born: date, on: date) -> int:
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def vehicle_age_range(first_registration: PartialDate, on: date) -> tuple[int, int] | None:
    """(fewest, most) completed years since first registration that the date's precision
    allows, or ``None`` when unknown. ``2012-05`` is any day of May 2012, never the 1st."""
    if first_registration.value is None:
        return None
    try:
        year = int(first_registration.value[:4])
        if first_registration.precision == Precision.DAY:
            earliest = latest = date.fromisoformat(first_registration.value)
        elif first_registration.precision == Precision.MONTH:
            month = int(first_registration.value[5:7])
            earliest, latest = date(year, month, 1), _month_end(date(year, month, 1))
        else:
            earliest, latest = date(year, 1, 1), date(year, 12, 31)
    except ValueError:
        return None  # e.g. year 0000: not a calendar date, so the age is unknown
    return _completed_years(latest, on), _completed_years(earliest, on)


def _age(facts: VehicleImportFacts, on: date, policy: EstimatePolicy) -> tuple[Decimal | None, list[str]]:
    """Vehicle age only when the registration date's precision leaves a single value. A date that
    is no calendar date, or an age beyond the engine's own bound, is implausible: unknown, never
    an error."""
    span = vehicle_age_range(facts.first_registration, on)
    implausible = (
        f"FIRST_REGISTRATION_IMPLAUSIBLE:{facts.first_registration.value}: not a plausible first "
        f"registration for a declaration on {on.isoformat()}; vehicle age unknown"
    )
    if span is None:
        return None, ([] if facts.first_registration.value is None else [implausible])
    fewest, most = span
    warnings: list[str] = []
    if most < 0:
        return None, ["FIRST_REGISTRATION_AFTER_DECLARATION: vehicle age unknown"]
    if most > _input_bound("vehicle_age_years"):
        return None, [implausible]
    if most >= policy.oldtimer_min_age_years:
        warnings.append(
            f"POSSIBLE_OLDTIMER_EXEMPTION_NOT_APPLIED: the vehicle may be {most} years old; the "
            "motor-vehicle-tax oldtimer exemption (ЗДМВ чл. 17) is not modelled and needs Customs "
            "certification"
        )
    if fewest != most:
        warnings.append(f"AGE_AMBIGUOUS:{max(fewest, 0)}-{most}: first registration is not precise enough")
        return None, warnings
    return Decimal(most), warnings


def _co2(facts: VehicleImportFacts) -> tuple[Decimal | None, Co2Cycle, list[str], list[str]]:
    """The CO2 figure and cycle the rule set receives, with assumptions and warnings."""
    assumptions: list[str] = []
    warnings: list[str] = []
    document = facts.co2_document
    if document is None and facts.fuel != Fuel.ELECTRIC:
        warnings.append("CO2_DOCUMENT_UNKNOWN: state which document gives the CO2 figure (or none_available)")
    elif document == "listing_claim_unverified":
        warnings.append(
            "CO2_NOT_FROM_ART12_DOCUMENT: the CO2 figure is a listing claim, not a COC or other "
            "ЗДМВ чл. 12(1) document; Customs re-assesses untrue data (чл. 11(4))"
        )
    elif document == "none_available":
        warnings.append(
            "CO2_NOT_ESTABLISHED: no ЗДМВ чл. 12(1) document; чл. 12(2) deems 400 g/km, applied only "
            "where the rule set models it for this vehicle"
        )
        if facts.co2:
            warnings.append("CO2_FIGURE_IGNORED: without a document the stated CO2 figure is not used")
    if not facts.co2:
        if document not in (None, "none_available") and facts.fuel != Fuel.ELECTRIC:
            warnings.append("CO2_MISSING: the document's CO2 figure is needed; it is never assumed")
        return None, Co2Cycle.UNKNOWN, assumptions, warnings
    by_cycle = {reading.cycle: reading.g_km for reading in facts.co2}
    cycle = next(c for c in _CO2_CYCLE_PREFERENCE if c in by_cycle)
    value = by_cycle[cycle]
    if value < 1 and facts.fuel in COMBUSTION_FUELS and document != "none_available":
        warnings.append(
            f"CO2_ZERO_FOR_COMBUSTION_VEHICLE:{value}: a {facts.fuel.value} car cannot emit under 1 g/km; "
            "the figure is treated as not established. State the document's figure, or "
            "co2_document=none_available for the deemed 400 g/km (ЗДМВ чл. 12(2))"
        )
        return None, Co2Cycle.UNKNOWN, assumptions, warnings
    if len(by_cycle) > 1:
        warnings.append(
            f"CO2_CYCLE_SELECTED:{cycle.value}: the document states several cycles; WLTP is used when "
            "present (Уредба 220/2023 чл. 2(2)), never converted"
        )
    if value != value.to_integral_value():
        if facts.co2_round_up_confirmed:
            rounded = Decimal(math.ceil(value))
            assumptions.append(f"CO2_ROUNDED_UP_BY_CALLER:{value}->{rounded} g/km")
            value = rounded
        else:
            warnings.append(
                f"CO2_NOT_INTEGER:{value}: no band rule for a fraction; confirm rounding up "
                "(co2_round_up_confirmed) or supply the document's whole figure"
            )
    return value, cycle, assumptions, warnings


def _origin(claim: OriginProofClaim) -> tuple[OriginProof | None, list[str], list[str]]:
    if claim.kind == "unknown":
        return None, [], ["ORIGIN_PROOF_UNKNOWN: whether a proof of origin is held is unknown; duty unknown"]
    if not claim.claimed:
        return _no_proof(), [_NO_PROOF_ASSUMPTION], []
    country = claim.country or _PROOF_COUNTRY[claim.kind]
    proof = OriginProof(
        proof_type=f"{claim.kind}_proof_of_origin" if claim.kind != "us" else "us_origin_evidence",
        origin_country=country,
        preferential=True,
        acceptance_status=OriginProofStatus.ACCEPTED,
        evidence_ids=(f"caller_attestation:{claim.evidence_ref or 'origin_proof'}",),
    )
    assumption = (
        f"ORIGIN_PROOF_ASSUMED_ACCEPTED_BY_CUSTOMS:{claim.kind}:{country}: the caller states the proof "
        "is held; Customs may reject it (standard-duty scenario shown)"
    )
    return proof, [assumption], []


_NO_PROOF_ASSUMPTION: Final = (
    "NO_ORIGIN_PROOF_STANDARD_DUTY: no proof of origin, so the standard duty "
    "(Царински закон чл. 19(4): a preference applies only on request with a proof)"
)


def _no_proof() -> OriginProof:
    return OriginProof(proof_type="none_claimed", acceptance_status=OriginProofStatus.NOT_AVAILABLE)


def _classification(
    facts: VehicleImportFacts, category: str, attested_by: str
) -> tuple[Classification | None, list[str], list[str]]:
    if facts.hs_code is None:
        if facts.fuel == Fuel.PLUGIN_HYBRID:
            return (
                None,
                [],
                ["PHEV_HS_CODE_REQUIRED: the plug-in hybrid HS code (870360/870370) decides the tax"],
            )
        return None, [], []
    classification = Classification(
        tariff_code=facts.hs_code,
        vehicle_category=category,
        evidence_ids=(f"caller_attestation:{facts.hs_evidence_ref or 'hs_code'}",),
        approval_status="approved",
        approved_by=attested_by,
    )
    warnings: list[str] = []
    if hs_code_contradicts_fuel(facts.hs_code, facts.fuel):
        expected = ", ".join(sorted(f.value for f in _HS_FUELS[facts.hs_code]))
        warnings.append(
            f"HS_CODE_FUEL_MISMATCH:{facts.hs_code}/{facts.fuel.value}: the attested tariff code covers "
            f"{expected}, not {facts.fuel.value}; the tariff code decides the motor-vehicle tax (ЗДМВ "
            "чл. 6(3)(1), 9(3)), so it stays unknown until fuel and hs_code agree"
        )
    return (
        classification,
        [f"HS_CODE_FROM_CALLER:{facts.hs_code}: attested by the caller, never inferred"],
        warnings,
    )


def hs_code_contradicts_fuel(hs_code: str | None, fuel: Fuel) -> bool:
    """Whether an attested HS-6 code names a drive type other than ``fuel`` (``other`` and
    ``unknown`` never contradict; codes without a single drive type are not checked)."""
    if hs_code is None or fuel in (Fuel.OTHER, Fuel.UNKNOWN):
        return False
    allowed = _HS_FUELS.get(hs_code)
    return allowed is not None and fuel not in allowed


def _with(inputs: TaxInputs, **changes: Any) -> TaxInputs:
    """A validated copy of ``inputs`` with ``changes`` applied."""
    return TaxInputs.model_validate({**inputs.model_dump(), **changes})


_TRANSACTION_VALUE_ASSUMPTION: Final = (
    "TRANSACTION_VALUE_ALTERNATIVE: Customs accepts the price actually paid as the transaction value "
    "(Царински закон чл. 28, its primary method); the charges follow that price + the transport to the "
    "MK place of entry instead of the estimated customs value"
)
_CHF_REASON: Final = (
    "a CHF price is not converted (it would need a CHF customs rate next to the EUR one; no other rate is "
    "substituted)"
)


@dataclass(frozen=True, slots=True)
class _TransactionValue:
    """The transaction value (price paid + transport to the place of entry) and the EUR price."""

    customs_value: Money | None
    basis: str | None
    price_eur: Money | None
    #: Why ``customs_value`` is unknown (``None`` when it is known).
    reason: str | None
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    needed: tuple[str, ...] = ()


def _transaction_value(
    facts: VehicleImportFacts,
    *,
    to_entry: Decimal | None,
    total_transport: Decimal | None,
    inland: Decimal,
    fx_inputs: TaxInputs,
) -> _TransactionValue:
    estimated = facts.estimated_customs_value_eur is not None
    price = facts.price
    if price is None:
        warning = (
            "PRICE_MISSING: real purchase price unknown; the landed cost and the transaction-value "
            "alternative are unknown"
            if estimated
            else "PRICE_MISSING: purchase price unknown; customs value unknown"
        )
        return _TransactionValue(None, None, None, "purchase price unknown", (), (warning,), ("price",))
    if price.currency == "CHF":
        # The price is known; what is missing is a price the customs value can use.
        what = (
            "the transaction value and the EUR purchase price of the landed cost are unknown"
            if estimated
            else "the customs value is unknown"
        )
        warning = (
            "CUSTOMS_VALUE_CHF_UNSUPPORTED: a CHF price plus EUR transport needs two customs rates; no "
            f"reference rate is substituted, so {what}"
        )
        return _TransactionValue(None, None, None, _CHF_REASON, (), (warning,), ("price_in_eur_or_mkd",))
    assumptions: list[str] = []
    warnings: list[str] = []
    needed: list[str] = []
    word = _PRICE_WORDS[facts.price_basis]
    if facts.price_basis in _CLAIMED_PRICE:
        code = "ADVERTISED_PRICE" if facts.price_basis == "advertised" else facts.price_basis.upper()
        assumptions.append(
            f"{code}_AS_TRANSACTION_VALUE: the {word} stands in for the invoice price "
            f"({_TRANSACTION_VALUE_REFERENCE})"
        )
    price_money = Money(amount=price.amount, currency=price.currency)
    price_eur = (
        price_money
        if price.currency == "EUR"
        else convert_with_customs_rate(price_money, "EUR", fx_inputs)[0]
    )
    if to_entry is None or total_transport is None:
        return _TransactionValue(
            None, None, price_eur, "transport to the MK place of entry unknown", tuple(assumptions)
        )
    total: Decimal | None
    if price.currency == "EUR":
        total = _CTX.add(price.amount, to_entry)
    else:
        converted, codes = convert_with_customs_rate(Money(amount=to_entry, currency="EUR"), "MKD", fx_inputs)
        if converted is None:
            needed.append("customs_fx_rate")
            warnings.extend(f"CUSTOMS_VALUE_TRANSPORT_NOT_CONVERTED:{code}" for code in codes)
            total = None
        else:
            total = _CTX.add(price.amount, converted.amount)
    reason = "no usable customs rate to convert the transport into MKD" if total is None else None
    if total is not None and not _within_bound(total):
        warnings.append(_OUT_OF_BOUND.format(what="the price"))
        total, reason = None, "the price plus the transport exceeds the engine's input bound"
    customs_value = None if total is None else Money(amount=total, currency=price.currency)
    basis = None
    if customs_value is not None:
        basis = (
            f"{_TRANSACTION_VALUE_REFERENCE}: {word} {_money_text(price.amount, price.currency)} + "
            f"чл. 35(1)(д) transport to MK place of entry {_money_text(to_entry, 'EUR')} (transport "
            f"line {_money_text(total_transport, 'EUR')}; separately invoiced inland leg "
            f"{_money_text(inland, 'EUR')})"
        )[:300]
    return _TransactionValue(
        customs_value, basis, price_eur, reason, tuple(assumptions), tuple(warnings), tuple(needed)
    )


@dataclass(frozen=True, slots=True)
class _EstimatedValue:
    """The estimated customs value plus the transport to the place of entry (``given`` false when
    the facts carry no estimate)."""

    given: bool
    customs_value: Money | None = None
    basis: str | None = None
    reason: str | None = None
    #: The estimate's assumptions: listed with the estimate and on every scenario that uses it.
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _estimated_value(
    facts: VehicleImportFacts, *, to_entry: Decimal | None, price_eur: Money | None
) -> _EstimatedValue:
    estimate, source = facts.estimated_customs_value_eur, facts.estimated_customs_value_source
    if estimate is None or source is None:
        return _EstimatedValue(given=False)
    used = (
        f"ESTIMATED_CUSTOMS_VALUE_USED:{source}: the import charges of the primary scenario are computed on "
        f"the estimated customs value {_money_text(estimate, 'EUR')} + the transport to the MK place of "
        "entry; the landed cost and the profit use the real purchase price (owner decision 2026-10-13)"
    )
    assumptions = (used, _ESTIMATE_TRANSPORT_UNCERTAIN)
    warnings = [
        "TRANSACTION_VALUE_ALTERNATIVE: Customs first applies the transaction value, the price actually "
        f"paid ({_TRANSACTION_VALUE_REFERENCE}), and another method only on founded doubt, in the legal "
        "order; the transaction_value scenario shows the charges if Customs accepts the price paid"
    ]
    if price_eur is not None and estimate < price_eur.amount:
        warnings.append(
            f"ESTIMATED_CUSTOMS_VALUE_BELOW_PRICE:{_money_text(estimate, 'EUR')} < "
            f"{_money_text(price_eur.amount, 'EUR')}: the estimate is below the real purchase price; Customs "
            f"starts from the price actually paid ({_TRANSACTION_VALUE_REFERENCE}), so the transaction_value "
            "scenario is the likelier assessment"
        )
    if to_entry is None:
        return _EstimatedValue(
            True,
            reason="transport to the MK place of entry unknown",
            assumptions=assumptions,
            warnings=tuple(warnings),
        )
    total = _CTX.add(estimate, to_entry)
    if not _within_bound(total):
        warnings.append(_OUT_OF_BOUND.format(what="the estimated customs value"))
        return _EstimatedValue(
            True,
            reason="the estimate plus the transport exceeds the engine's input bound",
            assumptions=assumptions,
            warnings=tuple(warnings),
        )
    basis = (
        f"ESTIMATED customs value ({source}) {_money_text(estimate, 'EUR')} + чл. 35(1)(д) transport to MK "
        f"place of entry {_money_text(to_entry, 'EUR')} (UNCERTAIN whether Customs adds it); the transaction "
        f"value ({_TRANSACTION_VALUE_REFERENCE}) is the alternative scenario"
    )[:300]
    return _EstimatedValue(
        True,
        customs_value=Money(amount=total, currency="EUR"),
        basis=basis,
        assumptions=assumptions,
        warnings=tuple(warnings),
    )


def _purchase_price(facts: VehicleImportFacts, price_eur: Money | None) -> PurchasePrice:
    """The real purchase price in EUR (exact) with its basis and source."""
    price = facts.price
    if price is None:
        return PurchasePrice(
            amount=None, basis=None, source="no purchase price given", reason="purchase price unknown"
        )
    source = _PRICE_SOURCES[facts.price_basis]
    if price_eur is not None:
        return PurchasePrice(amount=price_eur, basis=facts.price_basis, source=source)
    reason = (
        _CHF_REASON
        if price.currency == "CHF"
        else "no usable customs rate for the EUR equivalent of the price"
    )
    return PurchasePrice(amount=None, basis=facts.price_basis, source=source, reason=reason)


def _customs_value_for_duties(
    facts: VehicleImportFacts,
    *,
    transaction: _TransactionValue,
    estimated: _EstimatedValue,
    fx_inputs: TaxInputs,
) -> CustomsValueForDuties:
    """The primary scenario's customs value in EUR (exact), with its basis and source."""
    if estimated.given:
        assert facts.estimated_customs_value_source is not None
        return CustomsValueForDuties(
            amount=estimated.customs_value,
            basis="estimated",
            source=facts.estimated_customs_value_source,
            reason=estimated.reason,
        )
    source = "no purchase price given" if facts.price is None else _PRICE_SOURCES[facts.price_basis]
    value = transaction.customs_value
    if value is None:
        return CustomsValueForDuties(
            amount=None, basis="transaction_value", source=source, reason=transaction.reason or "unknown"
        )
    in_eur = value if value.currency == "EUR" else convert_with_customs_rate(value, "EUR", fx_inputs)[0]
    return CustomsValueForDuties(
        amount=in_eur,
        basis="transaction_value",
        source=source,
        reason=None if in_eur is not None else "no usable customs rate for the EUR equivalent",
    )


def build_tax_inputs(
    facts: VehicleImportFacts,
    *,
    transport: CostLine,
    customs_rate: FxRate | None,
    as_of: datetime,
    jurisdiction: str = DEFAULT_JURISDICTION,
    vehicle_category: str = DEFAULT_VEHICLE_CATEGORY,
    policy: EstimatePolicy = DEFAULT_POLICY,
    accept_previous_month_fx: bool = False,
    attested_by: str = "caller",
) -> BuiltTaxInputs:
    """Vehicle facts -> engine inputs for every applicable scenario (fit design section 3).

    Raises ``ValidationFailed`` only for contradictory inputs (a transport line of another
    category, an inland leg larger than the whole transport amount).
    """
    as_of = ensure_utc(as_of)
    assumptions: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    needed: list[str] = []

    declaration = declaration_date_for(facts, as_of)
    if facts.declaration_date is None:
        warnings.append(f"DECLARATION_DATE_ASSUMED_TODAY:{declaration.isoformat()}")

    valid_from: date | None = None
    valid_to: date | None = None
    if customs_rate is None:
        needed.append("customs_fx_rate")
        warnings.append(
            "CUSTOMS_FX_RATE_MISSING: record the Customs monthly EUR/MKD list rate "
            "(suv-deals fx record --purpose customs); no other rate is substituted"
        )
    else:
        valid_from, valid_to = customs_rate.rate_date, _month_end(customs_rate.rate_date)
        assumptions.append(
            f"CUSTOMS_FX_MONTHLY_LIST_VALIDITY:{valid_from.isoformat()}..{valid_to.isoformat()}: the "
            "customs rate is the monthly list valid in its month"
        )
        if (
            accept_previous_month_fx
            and declaration > valid_to
            and (declaration.year, declaration.month) == _next_month(valid_from)
        ):
            valid_to = declaration
            assumptions.append(
                f"CUSTOMS_FX_PREVIOUS_MONTH_ACCEPTED:{customs_rate.rate_date.isoformat()}: last month's "
                "customs rate is used for this preview"
            )
        if not valid_from <= declaration <= valid_to:
            # The engine refuses the rate (CUSTOMS_FX_RATE_NOT_EFFECTIVE); name the fact to supply.
            needed.append("customs_fx_rate")
            warnings.append(
                f"CUSTOMS_FX_RATE_NOT_EFFECTIVE_FOR_DECLARATION:{customs_rate.rate_date.isoformat()}: the "
                f"customs rate is valid {valid_from.isoformat()}..{valid_to.isoformat()}, not on "
                f"{declaration.isoformat()}; the amounts that depend on it are unknown. Record the Customs "
                f"monthly list rate for {declaration.strftime('%Y-%m')}"
            )
    effective_rate = (
        customs_rate
        if customs_rate is not None
        and valid_from is not None
        and valid_to is not None
        and valid_from <= declaration <= valid_to
        else None
    )
    fx_inputs = TaxInputs(
        declaration_date=declaration,
        customs_fx_rate=customs_rate,
        customs_fx_valid_from=valid_from,
        customs_fx_valid_to=valid_to,
    )

    # Transport: all of it to the place of entry (customs value), unless an inland leg is
    # separately invoiced; that leg goes to the import VAT base only.
    total_transport, transport_assumptions, transport_warnings = _transport_amount(transport)
    assumptions.extend(transport_assumptions)
    warnings.extend(transport_warnings)
    if total_transport is None:
        needed.append("transport")
    inland = facts.transport_after_entry_eur or Decimal(0)
    to_entry: Decimal | None = None
    if total_transport is not None:
        if inland > total_transport:
            raise ValidationFailed("transport_after_entry_eur exceeds the transport line amount")
        to_entry = _CTX.subtract(total_transport, inland)
        if inland:
            assumptions.append(
                f"TRANSPORT_SPLIT:{_money_text(to_entry, 'EUR')} to the MK place of entry in the customs "
                f"value ({_CUSTOMS_VALUE_REFERENCE}); {_money_text(inland, 'EUR')} separately invoiced "
                f"inland leg in the import VAT base only (чл. 36(а); {_INLAND_LEG_REFERENCE})"
            )
        else:
            assumptions.append(
                f"TRANSPORT_IN_CUSTOMS_VALUE:{_money_text(to_entry, 'EUR')}: the whole transport is to the "
                f"MK place of entry ({_CUSTOMS_VALUE_REFERENCE}); no separately invoiced inland leg "
                "(чл. 36(а))"
            )
    included = (
        None
        if to_entry is None
        else (
            IncludedCost(
                label="transport to MK place of entry",
                amount=Money(amount=to_entry, currency="EUR"),
                legal_basis=_CUSTOMS_VALUE_REFERENCE,
                allocation="per vehicle, transport cost line; included in the customs value",
            ),
        )
    )
    vat_only: tuple[IncludedCost, ...] = ()
    if inland:
        vat_only = (
            IncludedCost(
                label="inland transport after entry",
                amount=Money(amount=inland, currency="EUR"),
                legal_basis=_INLAND_LEG_REFERENCE,
                allocation="separately invoiced; import VAT base only",
            ),
        )

    # The real purchase price -> the transaction value (Царински закон чл. 28) and the EUR price
    # for the landed cost; an estimated customs value, when given, carries the primary charges.
    price = facts.price
    transaction = _transaction_value(
        facts,
        to_entry=to_entry,
        total_transport=total_transport,
        inland=inland,
        fx_inputs=fx_inputs,
    )
    assumptions.extend(transaction.assumptions)
    warnings.extend(transaction.warnings)
    needed.extend(transaction.needed)
    price_eur = transaction.price_eur
    estimated = _estimated_value(facts, to_entry=to_entry, price_eur=price_eur)
    assumptions.extend(estimated.assumptions)
    warnings.extend(estimated.warnings)

    origin_proof, origin_assumptions, origin_warnings = _origin(facts.origin_proof)
    if facts.origin_proof.kind == "unknown":
        needed.append("origin_proof")
    assumptions.extend(origin_assumptions)
    warnings.extend(origin_warnings)
    co2_value, co2_cycle, co2_assumptions, co2_warnings = _co2(facts)
    if facts.fuel != Fuel.ELECTRIC:
        if facts.co2_document is None:
            needed.append("co2_document")
        # From the result, not the input: a figure that is not established is still missing.
        if co2_value is None and facts.co2_document != "none_available":
            needed.append("co2")
    assumptions.extend(co2_assumptions)
    warnings.extend(co2_warnings)
    classification, class_assumptions, class_warnings = _classification(facts, vehicle_category, attested_by)
    if classification is None and facts.fuel == Fuel.PLUGIN_HYBRID:
        needed.append("hs_code")
    # An attested tariff code that contradicts the fuel leaves the fuel-keyed charges unknown
    # (never the treatment of the wrong drive type); the caller makes the two agree.
    engine_fuel = facts.fuel
    if hs_code_contradicts_fuel(facts.hs_code, facts.fuel):
        engine_fuel = Fuel.UNKNOWN
        needed.extend(("fuel", "hs_code"))
    assumptions.extend(class_assumptions)
    warnings.extend(class_warnings)
    age, age_warnings = _age(facts, declaration, policy)
    warnings.extend(age_warnings)
    gates = _gates(facts, policy)
    if gates.importable == "no":
        warnings.append(gates.reason)
    if facts.fuel == Fuel.UNKNOWN:
        needed.append("fuel")
        warnings.append("FUEL_UNKNOWN: the fuel decides the motor-vehicle-tax column")
    used = facts.mileage_km is not None and facts.mileage_km >= USED_VEHICLE_MIN_KM
    unverified: list[str] = []
    if price is not None and facts.price_basis in _CLAIMED_PRICE:
        unverified.append(_CLAIMED_PRICE[facts.price_basis])
    if facts.estimated_customs_value_source is not None:
        unverified.append(
            f"customs_value: estimated customs value ({facts.estimated_customs_value_source}), not a "
            "Customs assessment or the price paid"
        )
    if (
        facts.co2_document == "listing_claim_unverified"
        and co2_value is not None
        and facts.fuel != Fuel.ELECTRIC
    ):
        unverified.append("co2: listing claim, not a ЗДМВ чл. 12(1) document")
    if facts.origin_proof.claimed:
        unverified.append("origin_proof: caller attestation, acceptance by Customs assumed")
    if classification is not None:
        unverified.append("hs_code: caller attestation, not a binding tariff ruling")
    if facts.importer_status == "vat_dealer":
        notes.append(
            "IMPORT_VAT_DEDUCTIBLE_ONLY_FOR_TRADING: a VAT-registered dealer deducts import VAT on a "
            "passenger car only if it is used exclusively for trading (ЗДДВ чл. 35 т. 2); otherwise it is "
            "a final cost"
        )
    elif facts.importer_status is not None:
        notes.append("IMPORT_VAT_IS_A_FINAL_COST: not deductible for this importer (ЗДДВ чл. 35, 51(3))")

    transaction_inputs = TaxInputs(
        declaration_date=declaration,
        jurisdiction=jurisdiction,
        classification=classification,
        vehicle_condition="used" if used else None,
        vehicle_age_years=age,
        seller_country=facts.seller_country,
        origin_proof=origin_proof,
        # The real purchase price as stated; never a customs value by itself.
        invoice_price=None if price is None else Money(amount=price.amount, currency=price.currency),
        customs_value=transaction.customs_value,
        customs_value_basis=transaction.basis,
        included_costs=included,
        vat_incidental_costs=vat_only,
        co2_g_km=co2_value,
        co2_cycle=co2_cycle,
        co2_source_document=facts.co2_document,
        emissions_class=facts.emissions_class,
        fuel=engine_fuel,
        engine_displacement_cm3=facts.engine_displacement_cm3,
        power_kw=facts.power_kw,
        customs_fx_rate=customs_rate,
        customs_fx_valid_from=valid_from,
        customs_fx_valid_to=valid_to,
        exemptions=(),
        importer_status=facts.importer_status,
    )
    word = _PRICE_WORDS[facts.price_basis]
    if estimated.given:
        primary = ScenarioInputs(
            name="primary",
            label=(
                f"Primary: estimated customs value ({facts.estimated_customs_value_source}) + transport to "
                "the MK place of entry"
            )[:300],
            inputs=_with(
                transaction_inputs, customs_value=estimated.customs_value, customs_value_basis=estimated.basis
            ),
            customs_value_kind="estimated",
            assumptions=estimated.assumptions,
        )
    else:
        primary = ScenarioInputs(
            name="primary",
            label=(
                f"Primary: transaction value, {word} + transport to the MK place of entry "
                f"({_TRANSACTION_VALUE_REFERENCE})"
            ),
            inputs=transaction_inputs,
            customs_value_kind="transaction_value",
        )
    scenarios = [primary]
    if facts.origin_proof.claimed:
        scenarios.append(
            ScenarioInputs(
                name="standard_duty_if_proof_rejected",
                label=(
                    "Standard duty: Customs rejects or never receives the proof of origin (primary customs "
                    "value)"
                ),
                inputs=_with(primary.inputs, origin_proof=_no_proof()),
                customs_value_kind=primary.customs_value_kind,
                assumptions=(_NO_PROOF_ASSUMPTION, *primary.assumptions),
            )
        )
    if estimated.given:
        scenarios.append(
            ScenarioInputs(
                name="transaction_value",
                label=(
                    f"Alternative: Customs accepts the {word} as the transaction value "
                    f"({_TRANSACTION_VALUE_REFERENCE})"
                ),
                inputs=transaction_inputs,
                customs_value_kind="transaction_value",
                assumptions=(_TRANSACTION_VALUE_ASSUMPTION,),
            )
        )
    return BuiltTaxInputs(
        inputs=primary.inputs,
        scenarios=tuple(scenarios),
        assumptions=tuple(dict.fromkeys(assumptions)),
        warnings=tuple(dict.fromkeys(warnings)),
        gates=gates,
        price_eur=price_eur,
        purchase_price=_purchase_price(facts, price_eur),
        customs_value_for_duties=_customs_value_for_duties(
            facts, transaction=transaction, estimated=estimated, fx_inputs=fx_inputs
        ),
        transport=None if total_transport is None else Money(amount=total_transport, currency="EUR"),
        missing_facts=tuple(dict.fromkeys(needed)),
        notes=tuple(notes),
        unverified_inputs=tuple(unverified),
        effective_customs_rate=effective_rate,
    )


# --------------------------------------------------------------------------- selection


def select_estimate_rule_set(
    rule_sets: Iterable[RuleSet],
    jurisdiction: str,
    category: str,
    on_date: date,
    *,
    created_at: Mapping[str, datetime] | None = None,
) -> RuleSelection:
    """The production selection first (one ACTIVE, approved, non-fixture set); only when none
    applies, the indicative-preview selection. ``rule_set`` None means import costs unknown."""
    candidates = tuple(rule_sets)
    production = select_rule_set(candidates, jurisdiction, category, on_date)
    if production.rule_set is not None:
        return production
    preview = select_preview_rule_set(candidates, jurisdiction, category, on_date, created_at=created_at)
    considered = tuple(dict.fromkeys((*production.considered, *preview.considered)))
    if preview.rule_set is not None:
        return preview.model_copy(update={"considered": considered})
    return preview.model_copy(
        update={
            "reason": f"{production.reason}; {preview.reason}",
            "considered": considered,
        }
    )


# --------------------------------------------------------------------------- estimate result


class Banner(_Contract):
    """The label every surface shows (Macedonian and English)."""

    mk: str = Field(min_length=3, max_length=500)
    en: str = Field(min_length=3, max_length=500)

    def text(self) -> str:
        return f"{self.mk} / {self.en}"


class RuleIdentity(_Contract):
    rule_set_id: str
    version: str
    status: TaxRuleStatus
    jurisdiction: str
    currency: str | None
    valid_from: date | None
    valid_to: date | None
    #: The recorded hash, or for an unsealed draft the hash computed from its content, so a
    #: preview can be reproduced exactly.
    content_sha256: str
    sha256_recorded: bool


class CategoryEstimate(_Contract):
    """One import category. Helpers never count; the EUR amount is at the customs rate."""

    category: CostCategory
    status: ComponentStatus
    amount: Money | None
    amount_eur: Money | None
    component_ids: tuple[str, ...]
    reason: str | None = None


class UncertainItem(_Contract):
    """A rule-set statement or component marked UNCERTAIN. ``applies`` is false for a component
    that does not apply to this vehicle (e.g. the plug-in hybrid column for a diesel car)."""

    source: Literal["component", "rule_set"]
    component_id: str | None = None
    text: str = Field(min_length=1, max_length=1200)
    applies: bool = True


class EstimateScenario(_Contract):
    """One labelled scenario. Totals are named by mode: ``total`` (ACTIVE rules) or
    ``indicative_total`` (preview); the other stays empty."""

    name: EstimateScenarioName
    label: str
    #: The engine's customs value input (exact; EUR, or MKD for an MKD price) and its basis.
    customs_value: Money | None
    customs_value_basis: str | None
    customs_value_kind: CustomsValueBasis
    assumptions: tuple[str, ...]
    complete: bool
    #: ``customs_value`` in EUR at the customs rate (whole euros, half-up), so scenarios compare in
    #: one currency; ``None`` when the customs value is unknown or the rate cannot be used.
    customs_value_eur: Money | None = None
    production_ready: bool
    components: tuple[ComponentResult, ...]
    categories: tuple[CategoryEstimate, ...]
    total: Money | None = None
    total_eur: Money | None = None
    landed_cost_eur: Money | None = None
    indicative_total: Money | None = None
    indicative_total_eur: Money | None = None
    indicative_landed_cost_eur: Money | None = None
    known_subtotal: Money | None = None
    unknown_components: tuple[str, ...]
    missing_inputs: tuple[str, ...]
    warnings: tuple[str, ...]
    effective_date: date
    customs_fx_rate: FxRate | None
    engine_version: str = ENGINE_VERSION
    #: ``TaxCalculation.content_sha256()`` of the calculation behind this scenario.
    calculation_sha256: str

    def final_total(self) -> Money | None:
        """Whichever total this mode carries (for display; never a production figure in preview)."""
        return self.total or self.indicative_total


class ImportCostEstimate(_Contract):
    """Result of ``estimate_import_costs``. Display-only: never persisted to valuations, cost
    lines or alerts, and it carries no ``TaxCalculation`` that could become cost lines."""

    estimator_version: str = ESTIMATE_VERSION
    mode: EstimateMode
    production_ready: bool
    banner: Banner | None
    rule: RuleIdentity | None
    selection_reason: str
    considered: tuple[str, ...]
    jurisdiction: str
    vehicle_category: str
    declaration_date: date
    as_of: datetime
    currency: str | None
    customs_fx_rate: FxRate | None
    eur_basis: str
    landed_cost_note: str = LANDED_COST_NOTE
    #: The REAL purchase price (whole euros): the landed cost and the profit use it.
    purchase_price: PurchasePrice
    #: The customs value the ``primary`` charges are computed on (whole euros, incl. transport).
    customs_value_for_duties: CustomsValueForDuties
    scenarios: tuple[EstimateScenario, ...]
    assumptions: tuple[str, ...]
    warnings: tuple[str, ...]
    notes: tuple[str, ...]
    uncertain_items: tuple[UncertainItem, ...]
    gates: ImportGates
    missing_inputs: tuple[str, ...]
    missing_facts: tuple[str, ...]
    #: ``BuiltTaxInputs.unverified_inputs``: claims the figures rest on (never rule-supported).
    unverified_inputs: tuple[str, ...] = ()

    _utc = field_validator("as_of")(ensure_utc)

    @model_validator(mode="after")
    def _labelled(self) -> ImportCostEstimate:
        preview_fields = ("indicative_total", "indicative_total_eur", "indicative_landed_cost_eur")
        active_fields = ("total", "total_eur", "landed_cost_eur")
        if self.mode == "unavailable" and (self.rule is not None or self.scenarios or self.production_ready):
            raise ValueError(
                "an unavailable estimate has no rule set, no scenarios and is not production-ready"
            )
        if self.mode == "indicative_preview":
            if self.production_ready or self.banner is None:
                raise ValueError(
                    "an indicative preview is never production-ready and always carries the banner"
                )
            if self.rule is None or self.rule.status not in _PREVIEW_RULE_WORDS:
                raise ValueError("an indicative preview runs on an approved/under_review/draft rule set")
            if any(getattr(s, f) is not None for s in self.scenarios for f in active_fields):
                raise ValueError("an indicative preview names its totals indicative_total")
        if self.mode == "active":
            if self.rule is None or self.rule.status != TaxRuleStatus.ACTIVE:
                raise ValueError("an active estimate runs on an ACTIVE rule set")
            if any(getattr(s, f) is not None for s in self.scenarios for f in preview_fields):
                raise ValueError("an active estimate never carries indicative totals")
        if self.production_ready and (
            self.mode != "active" or not self.scenarios or not self.scenarios[0].production_ready
        ):
            raise ValueError("production_ready follows the primary scenario of an active estimate")
        if self.scenarios and self.scenarios[0].name != "primary":
            raise ValueError("the primary scenario comes first")
        return self

    def scenario(self, name: EstimateScenarioName) -> EstimateScenario | None:
        return next((s for s in self.scenarios if s.name == name), None)


# --------------------------------------------------------------------------- estimate service


def _preview_banner(rule_set: RuleSet) -> Banner:
    words_mk, words_en = _PREVIEW_RULE_WORDS[rule_set.status]
    return Banner(
        mk=f"ИНДИКАТИВНА ПРОЦЕНКА – {words_mk} ({rule_set.label()}). Не е царинска пресметка; не се користи "
        "за вреднувања ни известувања.",
        en=f"INDICATIVE PREVIEW – {words_en}; not a customs assessment; never used for valuations or alerts.",
    )


_UNAVAILABLE_BANNER: Final = Banner(
    mk="НЕМА ПРИМЕНЛИВИ ПРАВИЛА – увозните давачки се непознати (не нула).",
    en="NO APPLICABLE RULES – import charges unknown (not zero).",
)


def _whole(money: Money | None) -> Money | None:
    return None if money is None else whole_units(money)


def _whole_statement[S: (PurchasePrice, CustomsValueForDuties)](statement: S) -> S:
    """The statement with its EUR amount in whole euros (half-up)."""
    return statement.model_copy(update={"amount": _whole(statement.amount)})


def _in_eur(amount: Money | None, inputs: TaxInputs) -> Money | None:
    """Unrounded EUR equivalent at the customs rate (``None`` when the rate cannot be used)."""
    if amount is None:
        return None
    if amount.currency == "EUR":
        return amount
    converted, _ = convert_with_customs_rate(amount, "EUR", inputs)
    return converted


def _categories(calc: TaxCalculation, inputs: TaxInputs) -> tuple[CategoryEstimate, ...]:
    """Every import category, with the same unknown/not-applicable rules as the cost lines."""
    totals = calc.category_totals()
    rows: list[CategoryEstimate] = []
    for category in sorted(IMPORT_CATEGORIES):
        total = totals.get(category)
        if total is None:
            applicable_rule = calc.complete and bool(calc.components)
            rows.append(
                CategoryEstimate(
                    category=category,
                    status=ComponentStatus.NOT_APPLICABLE if applicable_rule else ComponentStatus.UNKNOWN,
                    amount=None,
                    amount_eur=None,
                    component_ids=(),
                    reason=f"the rule set defines no {category.value} component"
                    + ("" if applicable_rule else "; the calculation is incomplete (unknown, not zero)"),
                )
            )
            continue
        rows.append(
            CategoryEstimate(
                category=category,
                status=total.status,
                amount=total.amount,
                amount_eur=_whole(_in_eur(total.amount, inputs)),
                component_ids=total.component_ids,
            )
        )
    return tuple(rows)


def _missing(calc: TaxCalculation) -> tuple[str, ...]:
    """Missing rule inputs (never component ids), required ones first."""
    names = [*calc.missing_inputs]
    for comp in calc.components:
        if comp.status == ComponentStatus.UNKNOWN:
            names.extend(name for name in comp.missing_inputs if input_spec(name) is not None)
    return tuple(dict.fromkeys(names))


def _scenario(
    spec: ScenarioInputs,
    rule_set: RuleSet,
    *,
    mode: EstimateMode,
    built: BuiltTaxInputs,
    as_of: datetime,
) -> EstimateScenario:
    calc = calculate(rule_set, spec.inputs, as_of)
    preview = mode == "indicative_preview"
    total = calc.total_import_cost
    total_eur = _in_eur(total, spec.inputs)
    # Landed cost on the REAL purchase price (never the estimated customs value), summed exactly
    # and rounded to whole euros once.
    landed = None
    if total_eur is not None and built.price_eur is not None and built.transport is not None:
        landed = built.price_eur + built.transport + total_eur
    return EstimateScenario(
        name=spec.name,
        label=spec.label,
        customs_value=spec.inputs.customs_value,
        customs_value_eur=_whole(_in_eur(spec.inputs.customs_value, spec.inputs)),
        customs_value_basis=spec.inputs.customs_value_basis,
        customs_value_kind=spec.customs_value_kind,
        assumptions=spec.assumptions,
        complete=calc.complete,
        production_ready=calc.production_ready and mode == "active",
        components=calc.components,
        categories=_categories(calc, spec.inputs),
        known_subtotal=calc.known_subtotal,
        unknown_components=calc.unknown_components,
        missing_inputs=_missing(calc),
        warnings=calc.warnings,
        effective_date=calc.effective_date,
        customs_fx_rate=calc.customs_fx_rate,
        engine_version=calc.engine_version,
        calculation_sha256=calc.content_sha256(),
        total=None if preview else total,
        total_eur=None if preview else _whole(total_eur),
        landed_cost_eur=None if preview else _whole(landed),
        indicative_total=total if preview else None,
        indicative_total_eur=_whole(total_eur) if preview else None,
        indicative_landed_cost_eur=_whole(landed) if preview else None,
    )


def _sentences(text: str) -> list[str]:
    # Split after a full stop followed by a capital (Latin or Cyrillic), so "чл. 17" stays whole.
    return [s.strip() for s in re.split(r"(?<=\.)\s+(?=[A-ZА-Я])", text) if s.strip()]


def _uncertain_items(rule_set: RuleSet, primary: EstimateScenario | None) -> tuple[UncertainItem, ...]:
    """Every rule-set statement and component marked UNCERTAIN; the ones that apply to this
    vehicle first."""
    items = [
        UncertainItem(source="rule_set", text=sentence[:1200])
        for sentence in _sentences(rule_set.description or "")
        if "UNCERTAIN" in sentence
    ]
    not_applicable = {
        c.component_id
        for c in (primary.components if primary else ())
        if c.status == ComponentStatus.NOT_APPLICABLE
    }
    marked = [
        UncertainItem(
            source="component",
            component_id=comp.id,
            text=" | ".join(t for t in (comp.label, comp.legal_reference or "") if "UNCERTAIN" in t)[:1200],
            applies=comp.id not in not_applicable,
        )
        for comp in rule_set.components
        if "UNCERTAIN" in comp.label or "UNCERTAIN" in (comp.legal_reference or "")
    ]
    return (*items, *(i for i in marked if i.applies), *(i for i in marked if not i.applies))


def _rule_identity(rule_set: RuleSet) -> RuleIdentity:
    return RuleIdentity(
        rule_set_id=rule_set.rule_set_id,
        version=rule_set.version,
        status=rule_set.status,
        jurisdiction=rule_set.jurisdiction,
        currency=rule_set.currency,
        valid_from=rule_set.valid_from,
        valid_to=rule_set.valid_to,
        content_sha256=rule_set.sha256 or compute_rule_set_sha256(rule_set),
        sha256_recorded=rule_set.sha256 is not None,
    )


def _mode(selection: RuleSelection) -> EstimateMode:
    chosen = selection.rule_set
    if selection.mode == "unapproved_allowed":
        raise ValidationFailed("a fixture (allow_unapproved) selection never feeds an import-cost estimate")
    if chosen is None:
        return "unavailable"
    if selection.mode == "active":
        if chosen.status != TaxRuleStatus.ACTIVE or chosen.is_fixture:
            raise ValidationFailed("an active estimate needs an ACTIVE, non-fixture rule set")
        return "active"
    return "indicative_preview"


def _eur_basis(rate: FxRate | None, *, given: FxRate | None = None, declaration: date | None = None) -> str:
    """How EUR equivalents are computed: from the EFFECTIVE customs rate only. A rate that was
    given but is not valid on the declaration date is named as not used."""
    if rate is None and given is not None and declaration is not None:
        return (
            f"no customs rate effective for {declaration.strftime('%Y-%m')} (the given {given.base}/"
            f"{given.quote} {given.rate} of {given.rate_date.isoformat()} is not used): EUR equivalents "
            "and the landed cost are unknown"
        )[:500]
    if rate is None:
        return "no customs rate: EUR equivalents and the landed cost are unknown"
    return (
        f"EUR equivalents at the customs rate {rate.base}/{rate.quote} {rate.rate} "
        f"({rate.rate_date.isoformat()}, {rate.provider}); not a payment or reference rate"
    )[:500]


_GROUP_GAP: Final = re.compile(r"^ALTERNATIVE_GROUP_(GAP|AMBIGUOUS):(?P<group>[a-z0-9_]+):")


def _coverage_gaps(scenario: EstimateScenario) -> list[str]:
    """A rule-set alternative group with no (or several) applicable members for these facts.
    No fact the caller can supply completes such a charge, so it is named here instead of
    leaving an incomplete result with nothing listed as missing."""
    gaps: dict[str, str] = {}
    for comp in scenario.components:
        for text in comp.warnings:
            match = _GROUP_GAP.match(text)
            if match is not None:
                gaps.setdefault(match.group("group"), match.group(1).lower())
    return [
        f"RULE_SET_DOES_NOT_COVER_VEHICLE:{group}: {'no' if kind == 'gap' else 'more than one'} "
        f"alternative of the rule set's {group} group applies to these facts; the dependent charges "
        "stay unknown (not zero) and no further fact completes them under these rules"
        for group, kind in gaps.items()
    ]


def estimate_import_costs(
    selection: RuleSelection,
    facts: VehicleImportFacts,
    *,
    transport: CostLine,
    customs_rate: FxRate | None,
    as_of: datetime,
    jurisdiction: str = DEFAULT_JURISDICTION,
    vehicle_category: str = DEFAULT_VEHICLE_CATEGORY,
    policy: EstimatePolicy = DEFAULT_POLICY,
    accept_previous_month_fx: bool = False,
    attested_by: str = "caller",
) -> ImportCostEstimate:
    """Estimate customs duty, motor-vehicle tax and import VAT for one vehicle. Pure.

    ``accept_previous_month_fx`` applies to an indicative preview only (labelled assumption).

    ``selection`` comes from ``select_estimate_rule_set`` (production first, then the
    indicative preview) with ``declaration_date_for(facts, as_of)`` as its date. Every
    scenario is a separate ``calculate`` call on the selected rule set. In preview mode the
    totals are ``indicative_*`` and the result carries the banner; it is never
    production-ready. With no rule set the result is ``unavailable`` and every import charge
    stays unknown.
    """
    as_of = ensure_utc(as_of)
    mode = _mode(selection)
    rule_set = selection.rule_set
    if rule_set is not None and rule_set.jurisdiction != jurisdiction:
        raise ValidationFailed("the selected rule set is for another jurisdiction")
    # Last month's customs rate is a labelled preview convenience, never for ACTIVE rules.
    previous_month_fx = accept_previous_month_fx and mode == "indicative_preview"
    built = build_tax_inputs(
        facts,
        transport=transport,
        customs_rate=customs_rate,
        as_of=as_of,
        jurisdiction=jurisdiction,
        vehicle_category=vehicle_category,
        policy=policy,
        accept_previous_month_fx=previous_month_fx,
        attested_by=attested_by,
    )
    warnings = list(built.warnings)
    if accept_previous_month_fx and not previous_month_fx:
        warnings.append(
            "PREVIOUS_MONTH_FX_PREVIEW_ONLY: last month's customs rate is accepted only in a preview"
        )
    scenarios: tuple[EstimateScenario, ...] = ()
    if rule_set is None:
        warnings.append(f"IMPORT_COSTS_UNKNOWN: {selection.reason}"[:500])
        missing: tuple[str, ...] = ("tax_rule_set",)
    else:
        scenarios = tuple(
            _scenario(s, rule_set, mode=mode, built=built, as_of=as_of) for s in built.scenarios
        )
        missing = scenarios[0].missing_inputs
    primary = scenarios[0] if scenarios else None
    if primary is not None:
        warnings.extend(_coverage_gaps(primary))
    facts_needed = tuple(
        dict.fromkeys(
            [
                *(("tax_rule_set",) if rule_set is None else ()),
                *built.missing_facts,
                *(_FACT_FOR_INPUT[name] for name in missing if name in _FACT_FOR_INPUT),
            ]
        )
    )
    return ImportCostEstimate(
        mode=mode,
        production_ready=bool(primary and primary.production_ready),
        banner=_preview_banner(rule_set)
        if mode == "indicative_preview" and rule_set
        else (_UNAVAILABLE_BANNER if rule_set is None else None),
        rule=None if rule_set is None else _rule_identity(rule_set),
        selection_reason=selection.reason,
        considered=selection.considered,
        jurisdiction=jurisdiction,
        vehicle_category=vehicle_category,
        declaration_date=declaration_date_for(facts, as_of),
        as_of=as_of,
        currency=None if rule_set is None else rule_set.currency,
        customs_fx_rate=built.effective_customs_rate,
        eur_basis=_eur_basis(
            built.effective_customs_rate, given=customs_rate, declaration=declaration_date_for(facts, as_of)
        ),
        purchase_price=_whole_statement(built.purchase_price),
        customs_value_for_duties=_whole_statement(built.customs_value_for_duties),
        scenarios=scenarios,
        assumptions=built.assumptions,
        warnings=tuple(dict.fromkeys(warnings)),
        notes=built.notes,
        uncertain_items=() if rule_set is None else _uncertain_items(rule_set, primary),
        gates=built.gates,
        missing_inputs=missing,
        missing_facts=facts_needed,
        unverified_inputs=built.unverified_inputs,
    )


def estimate_from_rule_sets(
    rule_sets: Sequence[RuleSet],
    facts: VehicleImportFacts,
    *,
    transport: CostLine,
    customs_rate: FxRate | None,
    as_of: datetime,
    jurisdiction: str = DEFAULT_JURISDICTION,
    vehicle_category: str = DEFAULT_VEHICLE_CATEGORY,
    created_at: Mapping[str, datetime] | None = None,
    policy: EstimatePolicy = DEFAULT_POLICY,
    accept_previous_month_fx: bool = False,
    attested_by: str = "caller",
) -> ImportCostEstimate:
    """``select_estimate_rule_set`` on the declaration date, then ``estimate_import_costs``."""
    selection = select_estimate_rule_set(
        rule_sets,
        jurisdiction,
        vehicle_category,
        declaration_date_for(facts, as_of),
        created_at=created_at,
    )
    return estimate_import_costs(
        selection,
        facts,
        transport=transport,
        customs_rate=customs_rate,
        as_of=as_of,
        jurisdiction=jurisdiction,
        vehicle_category=vehicle_category,
        policy=policy,
        accept_previous_month_fx=accept_previous_month_fx,
        attested_by=attested_by,
    )
