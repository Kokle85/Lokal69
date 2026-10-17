"""Versioned, approval-gated import-tax rules engine (spec section 16).

The tax engine is a rules-and-evidence system, never a guessed formula:

- No North Macedonian (or any other) rate, tariff, coefficient or VAT percentage is
  shipped in code. Every amount comes from a declarative ``RuleSet`` that carries its
  official sources, effective dates, hash and a named approval record.
- The component language is restricted and declarative. Nothing from a rule file is
  ever executed (no ``eval``/``exec``, no code, no SQL). Components are evaluated in
  their declared order and may only reference declared inputs and *earlier*
  components (a DAG checked by ``validate_rule_set``).
- Every taxable base is listed explicitly. The engine never infers what VAT
  includes; a percentage component sums exactly the terms its rule lists.
- Unknown stays unknown. A missing input makes the dependent component ``unknown``
  and the calculation incomplete: only a ``known_subtotal`` is returned, never a
  ``total_import_cost`` (``missing_input_behavior = return_incomplete``).
- Origin is evidence, not geography. Seller and dispatch countries are recorded
  but never used to derive origin; only an accepted origin proof yields a
  preferential origin country. A German purchase does not imply EU preferential
  origin, Switzerland is never grouped with EU states (the engine has no country
  groups; rules list countries explicitly), and zero duty is never assumed because a
  vehicle is located in Europe.
- CO2 brackets are keyed by measurement cycle (NEDC, correlated NEDC, WLTP). A
  missing or unsupported cycle makes the component unknown; WLTP and NEDC values are
  never converted into each other.
- Customs value may differ from the invoice price. A rule that names
  ``customs_value`` never falls back to the invoice price. Money in a currency other
  than the rule currency is converted only with a customs-purpose rate for exactly
  that currency pair (direction honoured by ``FxRate.convert``).
- Intermediate components (``intermediate: true``) are helpers such as a customs value
  in the rule currency or a VAT base. They are evaluated and reported, feed later
  components, and are never counted in a total, a known subtotal or a category amount.
  An unknown helper still makes the calculation incomplete.
- Selection for production uses ``ACTIVE`` rule sets only. ``allow_unapproved`` is
  for visibly labelled synthetic fixtures; results always carry the rule status so a
  fixture result can never look production-ready. ``select_preview_rule_set`` picks an
  unapproved rule set for a clearly labelled indicative preview only; production
  callers never use it.

Lifecycle (spec 16, mirrored by the ``app.tax_rule_sets_guard`` trigger)::

    draft -> under_review -> approved -> active -> superseded | expired | revoked

plus ``under_review -> draft`` (rework), revocation from any open state and
``unapproved -> revoked`` for example/fixture rule sets.
"""

from __future__ import annotations

import decimal
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP, Decimal
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import Co2Cycle, CostCategory, Fuel, FxPurpose, TaxRuleStatus
from suv_deals.domain.listings import sha256_json
from suv_deals.domain.money import CurrencyCode, FxRate, Money, exponent
from suv_deals.errors import ValidationFailed

ENGINE_VERSION: Final = "tax-engine/1.1.0"

#: Cost categories a tax component may produce (spec 18 "duty, motor-vehicle tax,
#: import VAT and other applicable charges").
IMPORT_CATEGORIES: Final = frozenset(
    {
        CostCategory.IMPORT_DUTY,
        CostCategory.MOTOR_VEHICLE_TAX,
        CostCategory.IMPORT_VAT,
        CostCategory.OTHER_IMPORT_CHARGES,
    }
)

#: Fields excluded from the content hash. Lifecycle/approval metadata changes as a
#: rule set moves through its lifecycle; the approved *content* must not.
HASH_EXCLUDED_FIELDS: Final = frozenset({"sha256", "status", "approved_by", "approved_at", "review_record"})
#: Language fields added in engine 1.1, with their defaults. They are left out of the
#: content hash while they hold these defaults, so every older rule set hashes as before.
_COMPONENT_EXTENSION_DEFAULTS: Final[Mapping[str, object]] = MappingProxyType(
    {"intermediate": False, "key_terms": None, "key_currency": None, "integer_values_only": False}
)
_BRACKET_EXTENSION_DEFAULTS: Final[Mapping[str, object]] = MappingProxyType(
    {"over": None, "up_to": None, "amount_per_unit": None}
)

#: Statuses that can only be reached through an approval.
_APPROVAL_STATUSES: Final = frozenset(
    {TaxRuleStatus.APPROVED, TaxRuleStatus.ACTIVE, TaxRuleStatus.SUPERSEDED, TaxRuleStatus.EXPIRED}
)
#: Statuses a fixture/example rule set may hold (DB constraint tax_rule_sets_fixture_ck).
_FIXTURE_STATUSES: Final = frozenset({TaxRuleStatus.DRAFT, TaxRuleStatus.UNAPPROVED, TaxRuleStatus.REVOKED})
#: Never selectable, not even for fixtures.
_CLOSED_STATUSES: Final = frozenset({TaxRuleStatus.SUPERSEDED, TaxRuleStatus.EXPIRED, TaxRuleStatus.REVOKED})
#: Statuses that close a version for good (never selected, never previewed).
CLOSED_RULE_STATUSES: Final = _CLOSED_STATUSES
#: Statuses an indicative preview may run on, best first (``select_preview_rule_set``).
_PREVIEW_STATUS_ORDER: Final = (TaxRuleStatus.APPROVED, TaxRuleStatus.UNDER_REVIEW, TaxRuleStatus.DRAFT)

_MAX_RULE_JSON_BYTES: Final = 1_000_000
#: Sanity bound for money inputs (any currency). Larger values are input errors, and they
#: would exceed the exact-arithmetic precision used for rounding.
MAX_INPUT_AMOUNT: Final = Decimal("1000000000000")
_HEX64: Final = r"^[0-9a-f]{64}$"
_ID_PATTERN: Final = r"^[a-z][a-z0-9_]{0,63}$"
_CODE_PATTERN: Final = r"^[a-z0-9_]{1,60}$"
_COUNTRY_PATTERN: Final = r"^[A-Z]{2}$"
_CATEGORY_PATTERN: Final = r"^[a-z0-9_]{1,80}$"

# Wide context for intermediate arithmetic; quantisation happens explicitly.
_CTX: Final = decimal.Context(prec=50, rounding=ROUND_HALF_EVEN)


# --------------------------------------------------------------------------- helpers


def _contains_float(value: Any) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, list | tuple):
        return any(isinstance(item, float) for item in value)
    return False


class _Contract(BaseModel):
    """Frozen, strict contract base that refuses binary floats anywhere at this level."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _refuse_floats(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, value in data.items():
                if _contains_float(value):
                    raise ValueError(f"{key}: binary float is not allowed; use a decimal string")
        return data


def _aware(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


# --------------------------------------------------------------------------- input vocabulary


class InputKind(StrEnum):
    DATE = "date"
    TEXT = "text"
    NUMBER = "number"
    MONEY = "money"
    FX = "fx"


@dataclass(frozen=True, slots=True)
class InputSpec:
    """One named input a rule may declare. ``unit`` is the engine's canonical unit."""

    kind: InputKind
    unit: str | None
    description: str


#: The closed vocabulary of rule inputs (spec 16 "Inputs required by the applicable
#: rule set"). Rule sets may only reference these names (plus ``exemption_status:<code>``).
INPUT_REGISTRY: Final[Mapping[str, InputSpec]] = MappingProxyType(
    {
        "declaration_date": InputSpec(InputKind.DATE, "date", "intended import/declaration date"),
        "jurisdiction": InputSpec(InputKind.TEXT, None, "destination jurisdiction (ISO country)"),
        "classification": InputSpec(
            InputKind.TEXT, None, "approved tariff classification code (unapproved = missing)"
        ),
        "vehicle_category": InputSpec(
            InputKind.TEXT, None, "vehicle category from the approved classification"
        ),
        "vehicle_condition": InputSpec(InputKind.TEXT, None, "new | used"),
        "vehicle_age_years": InputSpec(
            InputKind.NUMBER, "years", "vehicle age in years as the rule defines it"
        ),
        "seller_country": InputSpec(InputKind.TEXT, None, "seller country; never implies origin"),
        "dispatch_country": InputSpec(InputKind.TEXT, None, "dispatch country; never implies origin"),
        "origin_country": InputSpec(InputKind.TEXT, None, "declared (non-preferential) country of origin"),
        "origin_evidence": InputSpec(
            InputKind.TEXT, None, "origin proof status: accepted | rejected | not_available | invalid_period"
        ),
        "preferential_origin_country": InputSpec(
            InputKind.TEXT, None, "country from an accepted preferential origin proof, or 'none'"
        ),
        "invoice_price": InputSpec(InputKind.MONEY, "money", "actual invoice/purchase price"),
        "customs_value": InputSpec(InputKind.MONEY, "money", "accepted customs value (requires a basis)"),
        "customs_value_basis": InputSpec(InputKind.TEXT, None, "customs valuation basis"),
        "included_costs_total": InputSpec(
            InputKind.MONEY, "money", "sum of costs included in customs value, each with a legal basis"
        ),
        "vat_incidental_costs_total": InputSpec(
            InputKind.MONEY,
            "money",
            "costs added to the import VAT base only (not in customs value), each with a legal basis",
        ),
        "co2_g_km": InputSpec(InputKind.NUMBER, "g/km", "CO2 emissions in g/km for the stated cycle"),
        "co2_cycle": InputSpec(InputKind.TEXT, None, "nedc | nedc_correlated | wltp"),
        "co2_source_document": InputSpec(InputKind.TEXT, None, "CoC/manufacturer/accepted document for CO2"),
        "emissions_class": InputSpec(InputKind.TEXT, None, "emissions class from an accepted document"),
        "fuel": InputSpec(InputKind.TEXT, None, "fuel type"),
        "engine_displacement_cm3": InputSpec(InputKind.NUMBER, "cm3", "engine displacement"),
        "power_kw": InputSpec(InputKind.NUMBER, "kW", "engine power"),
        "customs_fx_rate": InputSpec(InputKind.FX, None, "customs-prescribed exchange rate"),
        "importer_status": InputSpec(InputKind.TEXT, None, "importer/business status label"),
    }
)
EXEMPTION_INPUT_PREFIX: Final = "exemption_status:"


def input_spec(name: str) -> InputSpec | None:
    """Return the vocabulary entry for an input name, or None if the name is unknown."""
    if name.startswith(EXEMPTION_INPUT_PREFIX):
        code = name.removeprefix(EXEMPTION_INPUT_PREFIX)
        if code and len(code) <= 60 and all(c.islower() or c.isdigit() or c == "_" for c in code):
            return InputSpec(
                InputKind.TEXT, None, "exemption claim status: accepted | rejected | not_claimed"
            )
        return None
    return INPUT_REGISTRY.get(name)


# --------------------------------------------------------------------------- tax inputs


class OriginProofStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PENDING = "pending"  # treated as unknown
    NOT_AVAILABLE = "not_available"  # positively known: no proof will be presented
    UNKNOWN = "unknown"


class Classification(_Contract):
    """Tariff classification with evidence. Only an approved classification is usable."""

    tariff_code: str = Field(pattern=r"^[0-9]{4}([ .]?[0-9]{2}){0,4}$")
    vehicle_category: str | None = Field(default=None, pattern=_CATEGORY_PATTERN)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=20)
    approval_status: Literal["approved", "unapproved", "unknown"] = "unknown"
    approved_by: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _approval_evidence(self) -> Classification:
        if self.approval_status == "approved" and (not self.approved_by or not self.evidence_ids):
            raise ValueError("an approved classification needs approved_by and evidence_ids")
        return self


class OriginProof(_Contract):
    """Origin proof as evidence: type, issuer, validity and acceptance are kept explicitly."""

    proof_type: str = Field(min_length=1, max_length=120)
    issuing_authority: str | None = Field(default=None, max_length=200)
    origin_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    preferential: bool | None = None  # None = not established whether it supports preference
    valid_from: date | None = None
    valid_to: date | None = None
    acceptance_status: OriginProofStatus = OriginProofStatus.UNKNOWN
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def _consistent(self) -> OriginProof:
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("origin proof valid_to precedes valid_from")
        if self.acceptance_status == OriginProofStatus.ACCEPTED and (
            not self.evidence_ids or self.origin_country is None
        ):
            raise ValueError("an accepted origin proof needs evidence_ids and origin_country")
        return self


class IncludedCost(_Contract):
    """A cost with its legal basis and allocation: included in the customs value
    (``TaxInputs.included_costs``) or added to the import VAT base only
    (``TaxInputs.vat_incidental_costs``)."""

    label: str = Field(min_length=1, max_length=200)
    amount: Money
    legal_basis: str = Field(min_length=3, max_length=500)
    allocation: str | None = Field(default=None, max_length=300)

    @field_validator("amount")
    @classmethod
    def _non_negative(cls, value: Money) -> Money:
        if value.amount < 0:
            raise ValueError("included cost must not be negative")
        if value.amount > MAX_INPUT_AMOUNT:
            raise ValueError("included cost exceeds the input sanity bound")
        return value


class ExemptionClaim(_Contract):
    code: str = Field(pattern=_CODE_PATTERN)
    proof_ids: tuple[str, ...] = Field(default=(), max_length=20)
    acceptance_status: Literal["accepted", "rejected", "pending", "unknown"] = "unknown"

    @model_validator(mode="after")
    def _proof(self) -> ExemptionClaim:
        if self.acceptance_status == "accepted" and not self.proof_ids:
            raise ValueError("an accepted exemption needs proof_ids")
        return self


class TaxInputs(_Contract):
    """Every spec section 16 input. All optional: a missing value is unknown, never zero.

    Seller, dispatch and origin countries are separate facts; the engine never derives
    one from another. Negative numeric inputs are rejected.
    """

    declaration_date: date | None = None
    jurisdiction: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    classification: Classification | None = None
    vehicle_condition: Literal["new", "used"] | None = None
    vehicle_age_years: Decimal | None = Field(default=None, ge=0, le=200)
    seller_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    dispatch_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    origin_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    origin_proof: OriginProof | None = None
    invoice_price: Money | None = None
    customs_value: Money | None = None
    customs_value_basis: str | None = Field(default=None, min_length=3, max_length=300)
    included_costs: tuple[IncludedCost, ...] | None = None  # None = unknown; () = none included
    #: Costs in the import VAT base but not in the customs value. None = unknown; () = none.
    vat_incidental_costs: tuple[IncludedCost, ...] | None = Field(default=None, max_length=20)
    co2_g_km: Decimal | None = Field(default=None, ge=0, le=2000)
    co2_cycle: Co2Cycle = Co2Cycle.UNKNOWN
    co2_source_document: str | None = Field(default=None, min_length=1, max_length=300)
    emissions_class: str | None = Field(default=None, min_length=1, max_length=40)
    fuel: Fuel = Fuel.UNKNOWN
    engine_displacement_cm3: Decimal | None = Field(default=None, gt=0, le=20000)
    power_kw: Decimal | None = Field(default=None, gt=0, le=5000)
    customs_fx_rate: FxRate | None = None
    customs_fx_valid_from: date | None = None
    customs_fx_valid_to: date | None = None
    exemptions: tuple[ExemptionClaim, ...] | None = None  # None = unknown; () = none claimed
    importer_status: str | None = Field(default=None, pattern=_CODE_PATTERN)

    @field_validator("invoice_price", "customs_value")
    @classmethod
    def _money_non_negative(cls, value: Money | None) -> Money | None:
        if value is not None and value.amount < 0:
            raise ValueError("amount must not be negative")
        if value is not None and value.amount > MAX_INPUT_AMOUNT:
            raise ValueError("amount exceeds the input sanity bound")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> TaxInputs:
        start, end = self.customs_fx_valid_from, self.customs_fx_valid_to
        if start and end and end < start:
            raise ValueError("customs FX effective period ends before it starts")
        codes = [e.code for e in self.exemptions or ()]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate exemption codes")
        return self


# --------------------------------------------------------------------------- rule-set model


class RoundingSpec(_Contract):
    """Explicit rounding. ``quantum`` is the step (``0.01``, ``1``, ``10`` ...).

    ``stage``:
    - ``before_dependents``: the rounded amount is reported *and* feeds later components.
    - ``reported_only``: later components use the unrounded amount; only the reported
      amount is rounded. Totals always sum reported amounts.
    """

    quantum: Decimal = Field(gt=0)
    mode: Literal["half_up", "half_even", "down", "up"]
    stage: Literal["before_dependents", "reported_only"] = "before_dependents"


class RoundingRules(_Contract):
    component_default: RoundingSpec | None = None
    total: RoundingSpec | None = None  # stage is ignored for the total


class RuleSource(_Contract):
    url: str = Field(pattern=r"^https://", max_length=2048)
    title: str = Field(min_length=1, max_length=300)
    retrieved_at: datetime
    sha256: str = Field(pattern=_HEX64)

    _utc = field_validator("retrieved_at")(ensure_utc)


class ReviewRecord(_Contract):
    """Named owner-approved verification record binding the reviewed content hash."""

    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: datetime
    content_sha256: str = Field(pattern=_HEX64)
    scope: str = Field(min_length=3, max_length=2000)
    professional_review: str | None = Field(default=None, max_length=500)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=50)
    notes: str | None = Field(default=None, max_length=2000)

    _utc = field_validator("reviewed_at")(ensure_utc)


class Predicate(_Contract):
    """Restricted applicability test ``{input, op, value}``. No expressions, no code."""

    input: str = Field(min_length=1, max_length=120)
    op: Literal["eq", "ne", "in", "not_in", "lt", "le", "gt", "ge"]
    value: int | Decimal | str | tuple[str, ...]

    @field_validator("value", mode="before")
    @classmethod
    def _no_bool(cls, value: Any) -> Any:
        # JSON true/false would otherwise be coerced to 1/0 and compared as a number.
        items = value if isinstance(value, list | tuple) else (value,)
        if any(isinstance(item, bool) for item in items):
            raise ValueError("boolean predicate values are not allowed; use explicit text or numbers")
        return value

    @model_validator(mode="after")
    def _shape(self) -> Predicate:
        is_list = isinstance(self.value, tuple)
        if self.op in ("in", "not_in") and not is_list:
            raise ValueError(f"op {self.op} needs a list value")
        if self.op not in ("in", "not_in") and is_list:
            raise ValueError(f"op {self.op} needs a scalar value")
        if isinstance(self.value, tuple) and not 1 <= len(self.value) <= 100:
            raise ValueError("list value must hold 1-100 entries")
        return self


class Bracket(_Contract):
    """One bracket row, in one of two bound forms (all rows of a table use the same form):

    - ``lower_inclusive <= value < upper_exclusive`` (open-ended when upper is null);
    - law style "over X up to Y": ``over < value <= up_to``. Only the first row may have
      ``over`` null (no lower bound) and only the last ``up_to`` null (open-ended).

    Exactly one outcome: a fixed ``amount``, ``rate_of_base`` (fraction applied to the
    component's explicit base terms), ``amount_per_unit_above_lower`` (marginal: units
    above the lower bound) or ``amount_per_unit`` (whole value times the amount).
    """

    lower_inclusive: Decimal | None = Field(default=None, ge=0)
    upper_exclusive: Decimal | None = None
    over: Decimal | None = Field(default=None, ge=0)
    up_to: Decimal | None = Field(default=None, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    rate_of_base: Decimal | None = Field(default=None, ge=0, le=1)
    amount_per_unit_above_lower: Decimal | None = Field(default=None, ge=0)
    amount_per_unit: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _shape(self) -> Bracket:
        outcomes = [self.amount, self.rate_of_base, self.amount_per_unit_above_lower, self.amount_per_unit]
        if sum(o is not None for o in outcomes) != 1:
            raise ValueError(
                "a bracket needs exactly one of amount | rate_of_base | amount_per_unit_above_lower"
                " | amount_per_unit"
            )
        if self.lower_inclusive is not None:
            if self.over is not None or self.up_to is not None:
                raise ValueError("a bracket uses either lower_inclusive/upper_exclusive or over/up_to")
            if self.upper_exclusive is not None and self.upper_exclusive <= self.lower_inclusive:
                raise ValueError("bracket upper_exclusive must exceed lower_inclusive")
        elif self.upper_exclusive is not None:
            raise ValueError("bracket upper_exclusive needs lower_inclusive")
        elif self.over is not None and self.up_to is not None and self.up_to <= self.over:
            raise ValueError("bracket up_to must exceed over")
        return self

    @property
    def law_form(self) -> bool:
        """True for the ``over``/``up_to`` form."""
        return self.lower_inclusive is None

    def lower_bound(self) -> Decimal:
        """Lower bound for ``amount_per_unit_above_lower`` (``over``; 0 when unbounded)."""
        if self.lower_inclusive is not None:
            return self.lower_inclusive
        return self.over if self.over is not None else Decimal(0)

    def contains(self, value: Decimal) -> bool:
        if self.lower_inclusive is not None:
            return value >= self.lower_inclusive and (
                self.upper_exclusive is None or value < self.upper_exclusive
            )
        return (self.over is None or value > self.over) and (self.up_to is None or value <= self.up_to)

    def display(self) -> str:
        if self.lower_inclusive is not None:
            upper = "open" if self.upper_exclusive is None else _show(self.upper_exclusive)
            return f"[{_show(self.lower_inclusive)}, {upper})"
        lower = "open" if self.over is None else _show(self.over)
        upper = "open)" if self.up_to is None else f"{_show(self.up_to)}]"
        return f"({lower}, {upper}"


class _ComponentBase(_Contract):
    id: str = Field(pattern=_ID_PATTERN)
    label: str = Field(min_length=1, max_length=200)
    category: CostCategory
    currency: CurrencyCode
    depends_on: tuple[str, ...] = Field(default=(), max_length=50)
    applies_when: tuple[Predicate, ...] = Field(default=(), max_length=20)
    #: Components sharing a group are alternatives: exactly one must apply. If none or
    #: several apply, every member is unknown (guards against silent coverage gaps).
    alternative_group: str | None = Field(default=None, pattern=_CODE_PATTERN)
    rounding: RoundingSpec | None = None
    legal_reference: str | None = Field(default=None, max_length=500)
    #: A helper (e.g. a customs value in the rule currency or a VAT base). It is evaluated,
    #: reported and may feed later components through ``base`` or ``key_terms``, but it is
    #: never counted in a total, a known subtotal or a category amount.
    intermediate: bool = False

    @field_validator("category")
    @classmethod
    def _import_category(cls, value: CostCategory) -> CostCategory:
        if value not in IMPORT_CATEGORIES:
            raise ValueError(f"tax component category must be one of {sorted(IMPORT_CATEGORIES)}")
        return value


class PercentageComponent(_ComponentBase):
    """``rate`` (a fraction: 0.05 means 5 percent) times the sum of explicit base terms."""

    kind: Literal["percentage"]
    rate: Decimal = Field(ge=0, le=1)
    base: tuple[str, ...] = Field(min_length=1, max_length=20)


class FixedComponent(_ComponentBase):
    kind: Literal["fixed"]
    amount: Decimal = Field(ge=0)


class BracketComponent(_ComponentBase):
    """Bracket lookup on a numeric ``input`` or on a money key.

    CO2 uses per-cycle ``tables``; others use ``brackets``. With ``key_terms`` the bracket
    is keyed on the sum of money inputs and earlier components (in the rule currency),
    converted to ``key_currency`` with the customs rate when that differs.
    ``integer_values_only`` makes a non-integer input value unknown instead of silently
    falling into a band.
    """

    kind: Literal["bracket"]
    input: str | None = Field(default=None, min_length=1, max_length=120)
    key_terms: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=20)
    key_currency: CurrencyCode | None = None  # None = the rule currency
    integer_values_only: bool = False
    tables: dict[Co2Cycle, tuple[Bracket, ...]] | None = None
    brackets: tuple[Bracket, ...] | None = None
    base: tuple[str, ...] = Field(default=(), max_length=20)
    contiguous: bool = True

    @model_validator(mode="after")
    def _one_table_form(self) -> BracketComponent:
        if (self.input is None) == (self.key_terms is None):
            raise ValueError("a bracket component needs exactly one of input | key_terms")
        if (self.tables is None) == (self.brackets is None):
            raise ValueError("a bracket component needs exactly one of tables | brackets")
        if self.tables is not None and Co2Cycle.UNKNOWN in self.tables:
            raise ValueError("an 'unknown' CO2 cycle table is not allowed")
        return self

    def all_tables(self) -> dict[str, tuple[Bracket, ...]]:
        if self.tables is not None:
            return {cycle.value: rows for cycle, rows in self.tables.items()}
        return {"default": self.brackets or ()}


class PerUnitComponent(_ComponentBase):
    """``amount_per_unit`` per unit of a numeric input, optionally only above ``threshold``."""

    kind: Literal["per_unit"]
    input: str = Field(min_length=1, max_length=120)
    amount_per_unit: Decimal = Field(ge=0)
    threshold: Decimal | None = Field(default=None, ge=0)


Component = Annotated[
    PercentageComponent | FixedComponent | BracketComponent | PerUnitComponent,
    Field(discriminator="kind"),
]


class RuleSet(_Contract):
    """A versioned import-tax rule set. Shape follows spec section 16 plus audit fields."""

    rule_set_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{3,200}$")
    jurisdiction: str = Field(pattern=_COUNTRY_PATTERN)
    version: str = Field(min_length=1, max_length=80)
    status: TaxRuleStatus
    valid_from: date | None = None
    valid_to: date | None = None  # exclusive
    currency: CurrencyCode | None = None
    vehicle_categories: tuple[str, ...] = Field(default=(), max_length=20)
    description: str | None = Field(default=None, max_length=2000)
    sources: tuple[RuleSource, ...] = Field(default=(), max_length=50)
    approved_by: str | None = Field(default=None, min_length=1, max_length=200)
    approved_at: datetime | None = None
    review_record: ReviewRecord | None = None
    required_inputs: tuple[str, ...] = Field(default=(), max_length=60)
    optional_inputs: tuple[str, ...] = Field(default=(), max_length=60)
    input_units: dict[str, str] = Field(default_factory=dict)
    components: tuple[Component, ...] = Field(default=(), max_length=100)
    rounding_rules: RoundingRules | None = None
    missing_input_behavior: Literal["return_incomplete"] = "return_incomplete"
    sha256: str | None = Field(default=None, pattern=_HEX64)
    is_fixture: bool = False

    _utc = field_validator("approved_at")(_aware)

    @field_validator("vehicle_categories")
    @classmethod
    def _categories(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not re.fullmatch(_CATEGORY_PATTERN, item):
                raise ValueError(f"invalid vehicle category {item!r}")
        if len(set(value)) != len(value):
            raise ValueError("duplicate vehicle categories")
        return value

    @model_validator(mode="after")
    def _dates(self) -> RuleSet:
        if self.valid_to is not None and (self.valid_from is None or self.valid_to <= self.valid_from):
            raise ValueError("valid_to requires valid_from and must be after it")
        if (self.approved_by is None) != (self.approved_at is None):
            raise ValueError("approved_by and approved_at must be set together")
        return self

    def is_effective_on(self, on_date: date, *, open_start: bool = False) -> bool:
        """``valid_from <= on_date < valid_to``. A null ``valid_from`` is effective only if ``open_start``."""
        if self.valid_from is None:
            if not open_start:
                return False
        elif on_date < self.valid_from:
            return False
        return self.valid_to is None or on_date < self.valid_to

    def declared_inputs(self) -> frozenset[str]:
        return frozenset(self.required_inputs) | frozenset(self.optional_inputs)

    def label(self) -> str:
        return f"{self.rule_set_id}@{self.version}"


def _without_defaults(item: Mapping[str, Any], defaults: Mapping[str, object]) -> dict[str, Any]:
    # Identity comparison: the defaults are None/False, and 0 must never pass for False.
    return {key: value for key, value in item.items() if not (key in defaults and value is defaults[key])}


def _hash_payload(rule_set: RuleSet) -> dict[str, Any]:
    payload = rule_set.model_dump(mode="json", exclude=set(HASH_EXCLUDED_FIELDS))
    components: list[dict[str, Any]] = []
    for raw in payload["components"]:
        comp = _without_defaults(raw, _COMPONENT_EXTENSION_DEFAULTS)
        if comp.get("brackets") is not None:
            comp["brackets"] = [
                _without_defaults(row, _BRACKET_EXTENSION_DEFAULTS) for row in comp["brackets"]
            ]
        if comp.get("tables") is not None:
            comp["tables"] = {
                cycle: [_without_defaults(row, _BRACKET_EXTENSION_DEFAULTS) for row in rows]
                for cycle, rows in comp["tables"].items()
            }
        components.append(comp)
    return {**payload, "components": components}


def compute_rule_set_sha256(rule_set: RuleSet) -> str:
    """Canonical content hash: sha256 of the canonical JSON of every field except
    ``HASH_EXCLUDED_FIELDS`` (the hash itself and lifecycle/approval metadata).

    The engine 1.1 language fields are left out while they hold their defaults, so a rule
    set that does not use them hashes exactly as it did before they existed.
    """
    return sha256_json(_hash_payload(rule_set))


def seal_rule_set(rule_set: RuleSet) -> RuleSet:
    """Return a copy carrying its computed content hash (for drafts and fixtures)."""
    return rule_set.model_copy(update={"sha256": compute_rule_set_sha256(rule_set)})


# --------------------------------------------------------------------------- validation


def _term_problems(
    owner: str,
    terms: Iterable[str],
    earlier: Sequence[str],
    depends_on: Sequence[str],
    declared: frozenset[str],
    *,
    what: str = "base term",
) -> list[str]:
    problems: list[str] = []
    terms = list(terms)
    repeated = sorted({term for term in terms if terms.count(term) > 1})
    if repeated:
        # A repeated term would be summed twice (a doubled key selects the wrong band).
        problems.append(f"{owner}: duplicate {what}(s) {repeated}")
    for term in terms:
        if term in earlier:
            if term not in depends_on:
                problems.append(f"{owner}: {what} {term!r} must also be listed in depends_on")
            continue
        spec = input_spec(term)
        if spec is None:
            problems.append(f"{owner}: {what} {term!r} is neither an earlier component nor a known input")
        elif term not in declared:
            problems.append(f"{owner}: {what} {term!r} is not a declared input of this rule set")
        elif spec.kind != InputKind.MONEY:
            problems.append(f"{owner}: {what} {term!r} is not a money input")
    return problems


def _numeric_input_problems(
    owner: str, name: str, declared: frozenset[str], units: Mapping[str, str]
) -> list[str]:
    spec = input_spec(name)
    if spec is None:
        return [f"{owner}: unknown input {name!r}"]
    problems: list[str] = []
    if name not in declared:
        problems.append(f"{owner}: input {name!r} is not declared in required_inputs/optional_inputs")
    if spec.kind != InputKind.NUMBER:
        problems.append(f"{owner}: input {name!r} is not numeric")
    elif units.get(name) != spec.unit:
        problems.append(f"{owner}: input {name!r} needs a unit declaration of {spec.unit!r} in input_units")
    return problems


def _predicate_problems(
    owner: str, pred: Predicate, declared: frozenset[str], units: Mapping[str, str]
) -> list[str]:
    spec = input_spec(pred.input)
    if spec is None:
        return [f"{owner}: predicate on unknown input {pred.input!r}"]
    problems: list[str] = []
    if pred.input not in declared:
        problems.append(f"{owner}: predicate input {pred.input!r} is not declared")
    if spec.kind in (InputKind.MONEY, InputKind.FX):
        return [*problems, f"{owner}: predicates cannot test money/FX input {pred.input!r}"]
    if pred.op in ("lt", "le", "gt", "ge") and spec.kind == InputKind.TEXT:
        problems.append(f"{owner}: ordering op {pred.op} is not valid for text input {pred.input!r}")
    if pred.op in ("in", "not_in") and spec.kind != InputKind.TEXT:
        problems.append(f"{owner}: list op {pred.op} is only valid for text inputs")
    values = pred.value if isinstance(pred.value, tuple) else (pred.value,)
    for value in values:
        if spec.kind == InputKind.NUMBER:
            try:
                _as_decimal(value)
            except ValueError:
                problems.append(f"{owner}: predicate value {value!r} is not a decimal")
        elif spec.kind == InputKind.DATE:
            try:
                date.fromisoformat(str(value))
            except ValueError:
                problems.append(f"{owner}: predicate value {value!r} is not an ISO date")
        elif not isinstance(value, str):
            problems.append(f"{owner}: predicate value {value!r} must be text")
    if spec.kind == InputKind.NUMBER and units.get(pred.input) != spec.unit:
        problems.append(
            f"{owner}: input {pred.input!r} needs a unit declaration of {spec.unit!r} in input_units"
        )
    return problems


def _key_problems(
    owner: str, comp: BracketComponent, declared: frozenset[str], rule_currency: str | None
) -> list[str]:
    """Rules for a bracket keyed on money (``key_terms``)."""
    if comp.key_terms is None:
        return [f"{owner}: key_currency is only valid with key_terms"] if comp.key_currency else []
    problems: list[str] = []
    if comp.tables is not None:
        problems.append(f"{owner}: a bracket keyed on money (key_terms) cannot use per-cycle tables")
    if comp.integer_values_only:
        problems.append(f"{owner}: integer_values_only is only valid for a bracket on a numeric input")
    if comp.key_currency is not None:
        try:
            exponent(comp.key_currency)
        except ValidationFailed:
            problems.append(f"{owner}: key_currency {comp.key_currency} is not a supported currency")
        if comp.key_currency != rule_currency and "customs_fx_rate" not in declared:
            problems.append(
                f"{owner}: key_currency {comp.key_currency} differs from the rule currency, so "
                "customs_fx_rate must be declared as an input"
            )
    return problems


def _row_order_problems(where: str, rows: Sequence[Bracket], index: int, contiguous: bool) -> list[str]:
    """Bounds, order, overlap and contiguity of one row against the previous row."""
    row = rows[index]
    last = len(rows) - 1
    problems: list[str] = []
    if not row.law_form:
        if row.upper_exclusive is None and index != last:
            problems.append(f"{where}: only the last bracket may be open-ended")
        if index == 0:
            return problems
        prev = rows[index - 1]
        assert row.lower_inclusive is not None and prev.lower_inclusive is not None
        if row.lower_inclusive <= prev.lower_inclusive:
            problems.append(f"{where}: brackets must be sorted by lower_inclusive")
        elif prev.upper_exclusive is not None and row.lower_inclusive < prev.upper_exclusive:
            problems.append(f"{where}: brackets {index - 1} and {index} overlap")
        elif contiguous and prev.upper_exclusive != row.lower_inclusive:
            problems.append(f"{where}: brackets {index - 1} and {index} are not contiguous")
        return problems
    if row.over is None and index != 0:
        problems.append(f"{where}: only the first bracket may have no lower bound (over: null)")
    if row.up_to is None and index != last:
        problems.append(f"{where}: only the last bracket may be open-ended (up_to: null)")
    if index == 0:
        return problems
    prev = rows[index - 1]
    if row.over is None or prev.up_to is None:
        return problems  # reported above
    if prev.over is not None and row.over <= prev.over:
        problems.append(f"{where}: brackets must be sorted by over")
    elif row.over < prev.up_to:
        problems.append(f"{where}: brackets {index - 1} and {index} overlap")
    elif contiguous and row.over != prev.up_to:
        problems.append(f"{where}: brackets {index - 1} and {index} are not contiguous")
    return problems


def _bracket_problems(
    owner: str, comp: BracketComponent, declared: frozenset[str], rule_currency: str | None
) -> list[str]:
    problems: list[str] = []
    is_co2 = comp.input == "co2_g_km"
    keyed = comp.key_terms is not None
    if is_co2 and comp.tables is None:
        problems.append(f"{owner}: co2_g_km brackets must be keyed by CO2 cycle (tables)")
    if comp.tables is not None and "co2_cycle" not in declared:
        # The table is selected by the measurement cycle, so the cycle is an input of the rule.
        problems.append(f"{owner}: per-cycle CO2 tables need co2_cycle declared as an input")
    if not is_co2 and not keyed and comp.tables is not None:
        problems.append(f"{owner}: per-cycle tables are only valid for co2_g_km")
    problems.extend(_key_problems(owner, comp, declared, rule_currency))
    for table_key, rows in comp.all_tables().items():
        where = f"{owner}[{table_key}]"
        if not rows:
            problems.append(f"{where}: bracket table is empty")
            continue
        if len({row.law_form for row in rows}) > 1:
            problems.append(f"{where}: rows mix lower_inclusive/upper_exclusive and over/up_to bounds")
            continue
        for index, row in enumerate(rows):
            if row.rate_of_base is not None and not comp.base:
                problems.append(f"{where}: rate_of_base needs component base terms")
            if keyed and (row.amount_per_unit is not None or row.amount_per_unit_above_lower is not None):
                problems.append(f"{where}: a bracket keyed on money allows only amount or rate_of_base")
            problems.extend(_row_order_problems(where, rows, index, comp.contiguous))
    return problems


def rule_set_problems(rule_set: RuleSet) -> list[str]:
    """Every semantic problem with a rule set (empty list = valid). Never raises."""
    problems: list[str] = []
    declared = rule_set.declared_inputs()
    units = rule_set.input_units

    for name in (*rule_set.required_inputs, *rule_set.optional_inputs):
        if input_spec(name) is None:
            problems.append(f"unknown input name {name!r}")
    if len(set(rule_set.required_inputs)) != len(rule_set.required_inputs):
        problems.append("duplicate required_inputs")
    if len(set(rule_set.optional_inputs)) != len(rule_set.optional_inputs):
        problems.append("duplicate optional_inputs")
    if set(rule_set.required_inputs) & set(rule_set.optional_inputs):
        problems.append("an input cannot be both required and optional")
    for name, unit in units.items():
        spec = input_spec(name)
        if name not in declared:
            problems.append(f"input_units names undeclared input {name!r}")
        elif spec is not None and spec.unit != unit:
            problems.append(f"input {name!r} unit {unit!r} does not match engine unit {spec.unit!r}")

    if rule_set.components and rule_set.currency is None:
        problems.append("a rule set with components needs a currency")

    seen: list[str] = []
    for comp in rule_set.components:
        owner = f"component {comp.id!r}"
        if comp.id in seen:
            problems.append(f"{owner}: duplicate component id")
        if input_spec(comp.id) is not None:
            problems.append(f"{owner}: id collides with an input name")
        if rule_set.currency is not None and comp.currency != rule_set.currency:
            problems.append(
                f"{owner}: currency {comp.currency} differs from rule currency {rule_set.currency}"
            )
        for dep in comp.depends_on:
            if dep not in seen:
                problems.append(f"{owner}: depends_on {dep!r} is not an earlier component")
        for pred in comp.applies_when:
            problems.extend(_predicate_problems(owner, pred, declared, units))
        if isinstance(comp, PercentageComponent):
            problems.extend(_term_problems(owner, comp.base, seen, comp.depends_on, declared))
        elif isinstance(comp, BracketComponent):
            if comp.input is not None:
                problems.extend(_numeric_input_problems(owner, comp.input, declared, units))
            if comp.key_terms is not None:
                problems.extend(
                    _term_problems(owner, comp.key_terms, seen, comp.depends_on, declared, what="key term")
                )
            problems.extend(_term_problems(owner, comp.base, seen, comp.depends_on, declared))
            problems.extend(_bracket_problems(owner, comp, declared, rule_set.currency))
        elif isinstance(comp, PerUnitComponent):
            problems.extend(_numeric_input_problems(owner, comp.input, declared, units))
        seen.append(comp.id)

    groups: dict[str, int] = {}
    for comp in rule_set.components:
        if comp.alternative_group:
            groups[comp.alternative_group] = groups.get(comp.alternative_group, 0) + 1
    for group, count in groups.items():
        if count < 2:
            problems.append(f"alternative_group {group!r} needs at least two components")
    problems.extend(_intermediate_problems(rule_set))

    computed = compute_rule_set_sha256(rule_set)
    if rule_set.sha256 is not None and rule_set.sha256 != computed:
        problems.append("sha256 does not match the canonical content hash (content changed after hashing)")

    if rule_set.is_fixture and rule_set.status not in _FIXTURE_STATUSES:
        problems.append(f"a fixture rule set cannot hold status {rule_set.status.value}")

    if rule_set.status in _APPROVAL_STATUSES:
        problems.extend(_approval_problems(rule_set, computed))
    return problems


def _referenced_terms(comp: Component) -> tuple[str, ...]:
    """The ``base`` and ``key_terms`` a component sums (the only ways a helper is used).

    A bracket sums its ``base`` only in a ``rate_of_base`` row, so a base without such a row
    feeds nothing and does not count as a use."""
    if isinstance(comp, PercentageComponent):
        return comp.base
    if isinstance(comp, BracketComponent):
        uses_base = any(row.rate_of_base is not None for rows in comp.all_tables().values() for row in rows)
        return (*(comp.base if uses_base else ()), *(comp.key_terms or ()))
    return ()


def _zero_fixed(comp: Component) -> bool:
    return isinstance(comp, FixedComponent) and comp.amount == 0


def _intermediate_problems(rule_set: RuleSet) -> list[str]:
    """A helper must feed a later component, and an alternative group is all helpers or none.

    A group member nobody references is accepted only when it is a ``fixed`` amount of 0 and
    another member of its group is referenced: such members exist only to give the group full
    coverage. A non-zero unreferenced member would resolve while its referenced siblings do
    not apply, and its amount would silently vanish from the total.
    """
    components = rule_set.components
    position = {comp.id: index for index, comp in enumerate(components)}
    referenced: set[str] = set()
    for index, comp in enumerate(components):
        referenced.update(t for t in _referenced_terms(comp) if position.get(t, index) < index)
    members: dict[str, list[Component]] = {}
    for comp in components:
        if comp.alternative_group:
            members.setdefault(comp.alternative_group, []).append(comp)
    used_groups = {group for group, comps in members.items() if any(c.id in referenced for c in comps)}
    problems: list[str] = []
    for comp in components:
        if not comp.intermediate or comp.id in referenced:
            continue
        if comp.alternative_group not in used_groups:
            problems.append(
                f"component {comp.id!r}: an intermediate component must be a base or key term of a "
                "later component"
            )
        elif not _zero_fixed(comp):
            problems.append(
                f"component {comp.id!r}: an unreferenced alternative-group member must be a fixed 0 "
                "coverage member, or be a base or key term of a later component"
            )
    for group, comps in members.items():
        if len({c.intermediate for c in comps}) > 1:
            problems.append(f"alternative_group {group!r} mixes intermediate and normal components")
    return problems


def _approval_problems(rule_set: RuleSet, computed: str) -> list[str]:
    problems: list[str] = []
    status = rule_set.status.value
    if not rule_set.sources:
        problems.append(f"status {status} requires official sources")
    if not rule_set.approved_by or rule_set.approved_at is None:
        problems.append(f"status {status} requires approved_by and approved_at")
    if rule_set.sha256 is None:
        problems.append(f"status {status} requires a content sha256")
    if rule_set.review_record is None:
        problems.append(f"status {status} requires a review_record")
    elif rule_set.review_record.content_sha256 != computed:
        problems.append("review_record.content_sha256 does not match the rule content")
    if rule_set.valid_from is None:
        problems.append(f"status {status} requires valid_from")
    if rule_set.currency is None:
        problems.append(f"status {status} requires a currency")
    if not rule_set.vehicle_categories:
        problems.append(f"status {status} requires vehicle_categories")
    if not rule_set.components:
        problems.append(f"status {status} requires at least one component")
    elif all(comp.intermediate for comp in rule_set.components):
        problems.append(f"status {status} requires at least one non-intermediate component")
    default_rounding = rule_set.rounding_rules.component_default if rule_set.rounding_rules else None
    for comp in rule_set.components:
        if comp.rounding is None and default_rounding is None:
            problems.append(f"component {comp.id!r}: approved rule sets need explicit rounding")
    return problems


def validate_rule_set(rule_set: RuleSet) -> None:
    """Raise ``ValidationFailed`` listing every problem; return silently when valid."""
    problems = rule_set_problems(rule_set)
    if problems:
        raise ValidationFailed(
            f"tax rule set {rule_set.label()} is invalid",
            details={"problems": problems[:50]},
        )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise ValueError(f"JSON constant {name} is not allowed in a tax rule set")


def parse_rule_set_json(data: str | bytes) -> RuleSet:
    """Parse, structurally validate and semantically verify (incl. sha256) a rule-set JSON.

    JSON numbers are parsed as ``Decimal`` (never float); ``NaN``/``Infinity`` constants
    and duplicate keys are rejected.
    """
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if len(raw) > _MAX_RULE_JSON_BYTES:
        raise ValidationFailed("tax rule set document is too large")
    try:
        parsed = json.loads(
            raw,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationFailed(f"tax rule set is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationFailed("tax rule set must be a JSON object")
    try:
        rule_set = RuleSet.model_validate(parsed)
    except ValidationError as exc:
        raise ValidationFailed(
            "tax rule set has an invalid structure",
            details={"problems": [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()][:50]},
        ) from exc
    validate_rule_set(rule_set)
    return rule_set


def load_rule_set_file(path: Path) -> RuleSet:
    """Read a rule-set JSON file (config/tax_rules/*.json) and verify it."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValidationFailed(f"cannot read tax rule set {path.name}") from exc
    return parse_rule_set_json(data)


# --------------------------------------------------------------------------- lifecycle

ALLOWED_TRANSITIONS: Final[Mapping[TaxRuleStatus, frozenset[TaxRuleStatus]]] = MappingProxyType(
    {
        TaxRuleStatus.DRAFT: frozenset({TaxRuleStatus.UNDER_REVIEW, TaxRuleStatus.REVOKED}),
        TaxRuleStatus.UNDER_REVIEW: frozenset(
            {TaxRuleStatus.DRAFT, TaxRuleStatus.APPROVED, TaxRuleStatus.REVOKED}
        ),
        TaxRuleStatus.APPROVED: frozenset(
            {TaxRuleStatus.ACTIVE, TaxRuleStatus.SUPERSEDED, TaxRuleStatus.EXPIRED, TaxRuleStatus.REVOKED}
        ),
        TaxRuleStatus.ACTIVE: frozenset(
            {TaxRuleStatus.SUPERSEDED, TaxRuleStatus.EXPIRED, TaxRuleStatus.REVOKED}
        ),
        TaxRuleStatus.UNAPPROVED: frozenset({TaxRuleStatus.REVOKED}),
        TaxRuleStatus.SUPERSEDED: frozenset(),
        TaxRuleStatus.EXPIRED: frozenset(),
        TaxRuleStatus.REVOKED: frozenset(),
    }
)


def can_transition(current: TaxRuleStatus, target: TaxRuleStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def _ranges_overlap(a: RuleSet, b: RuleSet) -> bool:
    a_start = a.valid_from or date.min
    b_start = b.valid_from or date.min
    a_end = a.valid_to or date.max
    b_end = b.valid_to or date.max
    return a_start < b_end and b_start < a_end


def find_active_overlaps(rule_sets: Iterable[RuleSet]) -> list[tuple[str, str]]:
    """Pairs of ACTIVE rule sets with ambiguous applicability (same jurisdiction, a shared
    vehicle category and overlapping validity)."""
    active = [rs for rs in rule_sets if rs.status == TaxRuleStatus.ACTIVE]
    overlaps: list[tuple[str, str]] = []
    for i, left in enumerate(active):
        for right in active[i + 1 :]:
            if (
                left.jurisdiction == right.jurisdiction
                and set(left.vehicle_categories) & set(right.vehicle_categories)
                and _ranges_overlap(left, right)
            ):
                overlaps.append((left.label(), right.label()))
    return overlaps


def transition_rule_set(
    rule_set: RuleSet,
    target: TaxRuleStatus,
    *,
    at: datetime,
    approved_by: str | None = None,
    review_record: ReviewRecord | None = None,
    existing: Sequence[RuleSet] = (),
) -> RuleSet:
    """Return a copy of ``rule_set`` moved to ``target`` or raise ``ValidationFailed``.

    - Approval needs a named approver, a review record binding the content hash, official
      sources and a valid rule set; the hash is computed and stored.
    - Activation re-verifies the rule set and refuses ambiguous overlap with ``existing``
      ACTIVE versions. Approval never activates automatically.
    - The approval record is immutable once written.
    """
    at = ensure_utc(at)
    if not can_transition(rule_set.status, target):
        raise ValidationFailed(f"tax rule status {rule_set.status.value} -> {target.value} is not permitted")
    if rule_set.is_fixture and target not in _FIXTURE_STATUSES:
        # Synthetic rule sets never enter review/approval (DB constraint tax_rule_sets_fixture_ck).
        raise ValidationFailed(f"fixture rule sets can never move to {target.value}")
    update: dict[str, Any] = {"status": target}
    if target == TaxRuleStatus.APPROVED:
        if rule_set.is_fixture:
            raise ValidationFailed("fixture rule sets can never be approved")
        if not approved_by or review_record is None:
            raise ValidationFailed("approval requires approved_by and a review_record")
        content_hash = compute_rule_set_sha256(rule_set)
        if review_record.content_sha256 != content_hash:
            raise ValidationFailed("review_record does not bind the current rule content hash")
        update.update(
            approved_by=approved_by,
            approved_at=at,
            review_record=review_record,
            sha256=content_hash,
        )
    elif approved_by is not None or review_record is not None:
        raise ValidationFailed("approval data may only be supplied when approving")
    candidate = RuleSet.model_validate({**rule_set.model_dump(), **update})
    if target in (TaxRuleStatus.APPROVED, TaxRuleStatus.ACTIVE):
        validate_rule_set(candidate)
    if target == TaxRuleStatus.ACTIVE:
        others = [rs for rs in existing if rs.label() != candidate.label()]
        overlaps = find_active_overlaps([*others, candidate])
        if any(candidate.label() in pair for pair in overlaps):
            raise ValidationFailed(
                "activation would create overlapping ACTIVE versions with ambiguous applicability",
                details={"overlaps": [list(pair) for pair in overlaps]},
            )
    return candidate


# --------------------------------------------------------------------------- selection


class RuleSelection(_Contract):
    """Outcome of ``select_rule_set`` / ``select_preview_rule_set``. ``rule_set`` None means
    import costs stay unknown.

    ``mode``: ``active`` (production: ACTIVE, approved, non-fixture only),
    ``unapproved_allowed`` (labelled synthetic fixtures) or ``indicative_preview`` (an
    unapproved rule set for a labelled preview; never used for valuations, cost lines or
    alerts).
    """

    rule_set: RuleSet | None
    reason: str
    allow_unapproved: bool
    considered: tuple[str, ...] = ()
    mode: Literal["active", "unapproved_allowed", "indicative_preview"] = "active"

    @model_validator(mode="after")
    def _consistent_mode(self) -> RuleSelection:
        if (self.mode == "active") == self.allow_unapproved:
            raise ValueError("mode 'active' is exactly the production selection (allow_unapproved=False)")
        chosen = self.rule_set
        if (
            self.mode == "indicative_preview"
            and chosen is not None
            and (chosen.status not in _PREVIEW_STATUS_ORDER or chosen.is_fixture)
        ):
            raise ValueError(
                "an indicative preview runs only on a non-fixture approved/under_review/draft set"
            )
        return self


def select_rule_set(
    rule_sets: Iterable[RuleSet],
    jurisdiction: str,
    category: str,
    on_date: date,
    allow_unapproved: bool = False,
) -> RuleSelection:
    """Pick the single applicable rule set for a jurisdiction, vehicle category and date.

    Production (``allow_unapproved=False``) considers ACTIVE, non-fixture rule sets only.
    Superseded, expired and revoked rule sets are never selected: the valuation then has
    unknown import costs (incomplete). More than one candidate is an error, never a
    silent choice. Candidates are verified (hash, approval) and fail closed.
    ``allow_unapproved=True`` is for labelled synthetic fixtures; the selected rule's
    status travels into every calculation result.
    """
    considered: list[str] = []
    candidates: list[RuleSet] = []
    for rs in rule_sets:
        tag = rs.label()
        if rs.jurisdiction != jurisdiction or category not in rs.vehicle_categories:
            continue
        if rs.status in _CLOSED_STATUSES:
            considered.append(f"{tag}: {rs.status.value}")
            continue
        if not allow_unapproved and (rs.status != TaxRuleStatus.ACTIVE or rs.is_fixture):
            considered.append(
                f"{tag}: not active for production ({rs.status.value}, fixture={rs.is_fixture})"
            )
            continue
        if not rs.is_effective_on(on_date, open_start=allow_unapproved):
            considered.append(f"{tag}: not effective on {on_date.isoformat()}")
            continue
        validate_rule_set(rs)
        candidates.append(rs)
    if len(candidates) > 1:
        raise ValidationFailed(
            "ambiguous tax rule selection: overlapping applicable versions",
            details={"candidates": [rs.label() for rs in candidates]},
        )
    mode: Literal["active", "unapproved_allowed"] = "unapproved_allowed" if allow_unapproved else "active"
    if not candidates:
        return RuleSelection(
            rule_set=None,
            reason=f"no applicable {'selectable' if allow_unapproved else 'ACTIVE'} rule set for "
            f"{jurisdiction}/{category} on {on_date.isoformat()}; import costs unknown",
            allow_unapproved=allow_unapproved,
            considered=tuple(considered),
            mode=mode,
        )
    chosen = candidates[0]
    reason = f"selected {chosen.label()} ({chosen.status.value})"
    if chosen.status != TaxRuleStatus.ACTIVE or chosen.is_fixture:
        reason += "; NOT production-ready (unapproved/fixture selection)"
    return RuleSelection(
        rule_set=chosen,
        reason=reason,
        allow_unapproved=allow_unapproved,
        considered=tuple(considered),
        mode=mode,
    )


def select_preview_rule_set(
    rule_sets: Iterable[RuleSet],
    jurisdiction: str,
    category: str,
    on_date: date,
    *,
    created_at: Mapping[str, datetime] | None = None,
) -> RuleSelection:
    """Pick the rule set for an INDICATIVE PREVIEW when no ACTIVE rule set applies.

    Never for production: the valuation pipeline uses ``select_rule_set`` only, and a
    preview result must never reach valuations, cost lines or alerts.

    - Candidates: status ``approved`` (not yet active), ``under_review`` or ``draft``;
      not a fixture; at least one component; valid (``rule_set_problems`` empty); and
      ``valid_from <= on_date < valid_to`` (a null ``valid_from`` is accepted).
    - Never considered: unapproved examples, fixtures, superseded, expired and revoked
      sets. A version that is closed in any copy (e.g. revoked in the database while
      the draft file still ships) is excluded entirely.
    - Copies of one version (``rule_set_id@version``, e.g. a draft file and its database
      row) that are still open must have identical content; different content raises
      ``ValidationFailed`` (a version closed in any copy is excluded before that check).
      Identical copies count once, at their most advanced status.
    - Order: approved, then under_review, then draft; within a status the latest
      ``valid_from`` wins, then the newest ``created_at`` (keyed by ``label()``; known
      for database rows only). A tie that cannot be broken raises ``ValidationFailed``,
      exactly as ``select_rule_set`` does for overlapping ACTIVE versions.
    """
    stamps = created_at or {}
    considered: list[str] = []
    copies: dict[str, list[RuleSet]] = {}
    for rs in rule_sets:
        if rs.jurisdiction == jurisdiction and category in rs.vehicle_categories:
            copies.setdefault(rs.label(), []).append(rs)
    candidates: list[RuleSet] = []
    for tag, versions in copies.items():
        differs = len({compute_rule_set_sha256(rs) for rs in versions}) > 1
        # A closed copy excludes the whole version first, so a revoked copy whose content
        # differs from the shipped file can never block the previews of other versions.
        closed = sorted({rs.status.value for rs in versions if rs.status in _CLOSED_STATUSES})
        if closed:
            considered.append(
                f"{tag}: {', '.join(closed)}; never previewed"
                + ("; copies differ in content" if differs else "")
            )
            continue
        if differs:
            raise ValidationFailed(
                "the same tax rule set version exists with different content",
                details={"rule_set": tag},
            )
        open_copies = [rs for rs in versions if rs.status in _PREVIEW_STATUS_ORDER]
        if not open_copies:
            considered.append(f"{tag}: {versions[0].status.value} is never previewed")
            continue
        rs = min(open_copies, key=lambda r: _PREVIEW_STATUS_ORDER.index(r.status))
        if rs.is_fixture:
            considered.append(f"{tag}: fixture rule sets are never previewed")
        elif not rs.components:
            considered.append(f"{tag}: no components")
        elif problems := rule_set_problems(rs):
            considered.append(f"{tag}: invalid ({problems[0]})")
        elif not rs.is_effective_on(on_date, open_start=True):
            considered.append(f"{tag}: not effective on {on_date.isoformat()}")
        else:
            candidates.append(rs)
    if not candidates:
        return RuleSelection(
            rule_set=None,
            reason=f"no approved, under-review or draft rule set for {jurisdiction}/{category} on "
            f"{on_date.isoformat()}; no indicative preview, import costs unknown",
            allow_unapproved=True,
            considered=tuple(considered),
            mode="indicative_preview",
        )

    def rank(rs: RuleSet) -> tuple[int, int]:
        # Lower is better: status order first, then the latest valid_from (null = earliest).
        return (_PREVIEW_STATUS_ORDER.index(rs.status), -(rs.valid_from or date.min).toordinal())

    candidates.sort(key=rank)
    best = [rs for rs in candidates if rank(rs) == rank(candidates[0])]
    if len(best) > 1:
        stamped = [(stamps.get(rs.label()), rs) for rs in best]
        newest = max((s for s, _ in stamped if s is not None), default=None)
        winners = [rs for s, rs in stamped if s is not None and s == newest]
        if any(s is None for s, _ in stamped) or len(winners) != 1:
            raise ValidationFailed(
                "ambiguous indicative preview: several rule sets share status and valid_from",
                details={"candidates": [rs.label() for rs in best]},
            )
        best = winners
    chosen = best[0]
    return RuleSelection(
        rule_set=chosen,
        reason=f"INDICATIVE PREVIEW: selected {chosen.label()} ({chosen.status.value}); rules that are "
        "not ACTIVE, NOT production-ready, never used for valuations or alerts",
        allow_unapproved=True,
        considered=tuple(considered),
        mode="indicative_preview",
    )


# --------------------------------------------------------------------------- calculation output


class ComponentStatus(StrEnum):
    RESOLVED = "resolved"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class ComponentResult(_Contract):
    component_id: str
    label: str
    category: CostCategory
    kind: str
    rule_set_id: str
    version: str
    status: ComponentStatus
    amount: Money | None
    unrounded_amount: Decimal | None
    rounding: RoundingSpec | None
    inputs_used: dict[str, str]
    missing_inputs: tuple[str, ...]
    warnings: tuple[str, ...]
    intermediate: bool = False  # a helper: reported, never counted in a total

    @model_validator(mode="after")
    def _unknown_has_no_amount(self) -> ComponentResult:
        if (self.status == ComponentStatus.RESOLVED) != (self.amount is not None):
            raise ValueError("only a resolved component carries an amount")
        return self


class CategoryTotal(_Contract):
    category: CostCategory
    status: ComponentStatus
    amount: Money | None
    component_ids: tuple[str, ...]


class TaxCalculation(_Contract):
    """Calculation result. ``total_import_cost`` exists only when complete; otherwise only
    ``known_subtotal`` (sum of resolved components) plus the unknown component list.
    Intermediate components are reported but never summed into either."""

    engine_version: str = ENGINE_VERSION
    rule_set_id: str
    version: str
    rule_status: TaxRuleStatus
    rule_sha256: str | None
    is_fixture: bool
    jurisdiction: str
    currency: str
    effective_date: date
    rule_valid_from: date | None
    rule_valid_to: date | None
    as_of: datetime
    customs_fx_rate: FxRate | None  # the customs rate actually used for a conversion
    components: tuple[ComponentResult, ...]
    complete: bool
    total_import_cost: Money | None
    known_subtotal: Money | None
    unknown_components: tuple[str, ...]
    missing_inputs: tuple[str, ...]
    warnings: tuple[str, ...]

    _utc = field_validator("as_of")(ensure_utc)

    @model_validator(mode="after")
    def _labels(self) -> TaxCalculation:
        if self.complete:
            if self.total_import_cost is None or self.known_subtotal is not None:
                raise ValueError("a complete calculation has a total and no known_subtotal")
            if self.unknown_components or self.missing_inputs:
                raise ValueError("a complete calculation has no unknowns")
        elif self.total_import_cost is not None:
            raise ValueError("an incomplete calculation never carries total_import_cost")
        return self

    @property
    def production_ready(self) -> bool:
        """True only for a complete result from an ACTIVE, non-fixture, hash-verified rule set."""
        return (
            self.complete
            and self.rule_status == TaxRuleStatus.ACTIVE
            and not self.is_fixture
            and self.rule_sha256 is not None
        )

    def content_sha256(self) -> str:
        """Canonical hash of this exact result (rule identity, inputs used, amounts, status).

        Cost lines derived from it carry ``tax_calculation:<hash>`` as evidence so a
        valuation can verify that its scenarios used exactly the calculation it records.
        """
        return sha256_json(self.model_dump(mode="json"))

    def category_totals(self) -> dict[CostCategory, CategoryTotal]:
        """Per-category status/amount. A category is unknown if any of its components is
        unknown *or* a required input is missing (the rule says it cannot be trusted).

        Intermediate components count for the status (an unknown helper makes its category
        unknown, never not-applicable) but never for the amount. ``component_ids`` lists
        every member, helpers included.
        """
        result: dict[CostCategory, CategoryTotal] = {}
        for category in sorted({c.category for c in self.components}):
            members = [c for c in self.components if c.category == category]
            ids = tuple(c.component_id for c in members)
            if self.missing_inputs or any(c.status == ComponentStatus.UNKNOWN for c in members):
                result[category] = CategoryTotal(
                    category=category, status=ComponentStatus.UNKNOWN, amount=None, component_ids=ids
                )
                continue
            resolved = [c.amount for c in members if c.amount is not None and not c.intermediate]
            if not resolved:
                result[category] = CategoryTotal(
                    category=category, status=ComponentStatus.NOT_APPLICABLE, amount=None, component_ids=ids
                )
                continue
            total = Money.zero(self.currency)
            for amount in resolved:
                total = total + amount
            result[category] = CategoryTotal(
                category=category, status=ComponentStatus.RESOLVED, amount=total, component_ids=ids
            )
        return result


# --------------------------------------------------------------------------- evaluation


_ROUNDING_MODES: Final[Mapping[str, str]] = MappingProxyType(
    {"half_up": ROUND_HALF_UP, "half_even": ROUND_HALF_EVEN, "down": ROUND_DOWN, "up": ROUND_UP}
)


def apply_rounding(value: Decimal, spec: RoundingSpec) -> Decimal:
    """Round ``value`` to a multiple of ``spec.quantum`` with the declared mode.

    Raises ``ValidationFailed`` (never a raw ``decimal`` error) when the value is too large
    to round exactly at the engine precision.
    """
    try:
        units = _CTX.divide(value, spec.quantum).quantize(
            Decimal(1), rounding=_ROUNDING_MODES[spec.mode], context=_CTX
        )
        return _CTX.multiply(units, spec.quantum)
    except decimal.DecimalException as exc:
        raise ValidationFailed("amount cannot be rounded exactly; it exceeds the engine precision") from exc


def _as_decimal(value: object) -> Decimal:
    if isinstance(value, bool | float):
        raise ValueError("not a decimal")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        try:
            result = Decimal(value)
        except decimal.InvalidOperation as exc:
            raise ValueError("not a decimal") from exc
        if not result.is_finite():
            raise ValueError("not finite")
        return result
    raise ValueError("not a decimal")


Scalar = str | Decimal | date


@dataclass(slots=True)
class _Resolved:
    value: Scalar | None
    display: str
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _MoneyResolved:
    amount: Decimal | None  # in rule currency
    display: str
    warnings: list[str] = field(default_factory=list)


class _Resolver:
    """Turns ``TaxInputs`` into the scalar/money values rules may reference."""

    def __init__(self, inputs: TaxInputs, rule_currency: str) -> None:
        self.inputs = inputs
        self.currency = rule_currency
        self.customs_rate_used: FxRate | None = None

    # -- presence used for required_inputs
    def present(self, name: str) -> bool:
        spec = input_spec(name)
        if spec is None:
            return False
        if spec.kind == InputKind.MONEY:
            return self.money(name).amount is not None
        if spec.kind == InputKind.FX:
            rate = self.inputs.customs_fx_rate
            return rate is not None and rate.purpose == FxPurpose.CUSTOMS
        return self.scalar(name).value is not None

    def scalar(self, name: str) -> _Resolved:
        i = self.inputs
        simple: dict[str, Scalar | None] = {
            "declaration_date": i.declaration_date,
            "jurisdiction": i.jurisdiction,
            "vehicle_condition": i.vehicle_condition,
            "vehicle_age_years": i.vehicle_age_years,
            "seller_country": i.seller_country,
            "dispatch_country": i.dispatch_country,
            "origin_country": i.origin_country,
            "co2_g_km": i.co2_g_km,
            "co2_cycle": None if i.co2_cycle == Co2Cycle.UNKNOWN else i.co2_cycle.value,
            "co2_source_document": i.co2_source_document,
            "emissions_class": i.emissions_class,
            "fuel": None if i.fuel == Fuel.UNKNOWN else i.fuel.value,
            "engine_displacement_cm3": i.engine_displacement_cm3,
            "power_kw": i.power_kw,
            "importer_status": i.importer_status,
            "customs_value_basis": i.customs_value_basis,
        }
        if name in simple:
            value = simple[name]
            return _Resolved(value, "unknown" if value is None else _show(value))
        if name in ("classification", "vehicle_category"):
            return self._classification(name)
        if name == "origin_evidence":
            return self._origin_status()
        if name == "preferential_origin_country":
            return self._preferential_country()
        if name.startswith(EXEMPTION_INPUT_PREFIX):
            return self._exemption(name.removeprefix(EXEMPTION_INPUT_PREFIX))
        return _Resolved(None, "unknown", [f"INPUT_NOT_SCALAR:{name}"])

    def _classification(self, name: str) -> _Resolved:
        c = self.inputs.classification
        if c is None:
            return _Resolved(None, "unknown")
        if c.approval_status != "approved":
            return _Resolved(None, "unapproved", [f"CLASSIFICATION_NOT_APPROVED:{c.approval_status}"])
        value = c.tariff_code if name == "classification" else c.vehicle_category
        return _Resolved(value, "unknown" if value is None else value)

    def _origin_status(self) -> _Resolved:
        proof = self.inputs.origin_proof
        if proof is None:
            return _Resolved(
                None, "unknown", ["ORIGIN_PROOF_MISSING: origin is never derived from seller/dispatch"]
            )
        status = proof.acceptance_status
        if status in (OriginProofStatus.PENDING, OriginProofStatus.UNKNOWN):
            return _Resolved(None, status.value, [f"ORIGIN_PROOF_{status.value.upper()}"])
        if status != OriginProofStatus.ACCEPTED:
            return _Resolved(status.value, status.value)
        if proof.valid_from or proof.valid_to:
            on = self.inputs.declaration_date
            if on is None:
                return _Resolved(
                    None, "accepted; validity unverifiable", ["ORIGIN_PROOF_VALIDITY_UNVERIFIABLE"]
                )
            if (proof.valid_from and on < proof.valid_from) or (proof.valid_to and on > proof.valid_to):
                return _Resolved("invalid_period", "invalid_period", ["ORIGIN_PROOF_OUTSIDE_VALIDITY"])
        return _Resolved("accepted", "accepted")

    def _preferential_country(self) -> _Resolved:
        status = self._origin_status()
        if status.value is None:
            return _Resolved(None, "unknown", status.warnings)
        proof = self.inputs.origin_proof
        if status.value != "accepted" or proof is None:
            return _Resolved("none", "none", status.warnings)
        if proof.preferential is None:
            return _Resolved(None, "unknown", ["ORIGIN_PROOF_PREFERENCE_UNKNOWN"])
        if not proof.preferential or proof.origin_country is None:
            return _Resolved("none", "none")
        return _Resolved(proof.origin_country, proof.origin_country)

    def _exemption(self, code: str) -> _Resolved:
        claims = self.inputs.exemptions
        if claims is None:
            return _Resolved(None, "unknown")
        for claim in claims:
            if claim.code == code:
                if claim.acceptance_status in ("pending", "unknown"):
                    return _Resolved(
                        None, claim.acceptance_status, [f"EXEMPTION_{code}_{claim.acceptance_status}"]
                    )
                return _Resolved(claim.acceptance_status, claim.acceptance_status)
        return _Resolved("not_claimed", "not_claimed")

    # -- money in rule currency
    def money(self, name: str) -> _MoneyResolved:
        i = self.inputs
        if name == "invoice_price":
            return self._convert(i.invoice_price, name)
        if name == "customs_value":
            if i.customs_value is None:
                return _MoneyResolved(
                    None, "unknown", ["CUSTOMS_VALUE_MISSING: invoice price is never substituted"]
                )
            if i.customs_value_basis is None:
                return _MoneyResolved(None, "basis unknown", ["CUSTOMS_VALUE_BASIS_MISSING"])
            return self._convert(i.customs_value, name)
        if name == "included_costs_total":
            return self._costs_total(i.included_costs, "included_cost")
        if name == "vat_incidental_costs_total":
            return self._costs_total(i.vat_incidental_costs, "vat_incidental_cost")
        return _MoneyResolved(None, "unknown", [f"INPUT_NOT_MONEY:{name}"])

    def _costs_total(self, costs: tuple[IncludedCost, ...] | None, prefix: str) -> _MoneyResolved:
        """Sum of costs in the rule currency. None is unknown; () is a known zero."""
        if costs is None:
            return _MoneyResolved(None, "unknown")
        total = Decimal(0)
        parts: list[str] = []
        warnings: list[str] = []
        for cost in costs:
            converted = self._convert(cost.amount, f"{prefix}:{cost.label}")
            warnings.extend(converted.warnings)
            if converted.amount is None:
                return _MoneyResolved(None, "unknown", warnings)
            total = _CTX.add(total, converted.amount)
            parts.append(converted.display)
        return _MoneyResolved(total, f"{_show(total)} {self.currency} (" + "; ".join(parts) + ")", warnings)

    def _convert(self, money: Money | None, name: str) -> _MoneyResolved:
        """Money input -> rule currency (customs rate only)."""
        if money is None:
            return _MoneyResolved(None, "unknown")
        return self.convert_amount(money.amount, money.currency, self.currency, name)

    def convert_amount(self, amount: Decimal, source: str, target: str, name: str) -> _MoneyResolved:
        """``amount`` in ``source`` -> ``target`` with the customs rate.

        The rate must be customs-purpose, for exactly the {source, target} pair, dated no
        later than the declaration date and inside the recorded effective period.
        """
        if source == target:
            return _MoneyResolved(amount, f"{_show(amount)} {source}")
        rate = self.inputs.customs_fx_rate
        if rate is None:
            return _MoneyResolved(None, "no customs rate", [f"CUSTOMS_FX_RATE_MISSING:{name}"])
        if rate.purpose != FxPurpose.CUSTOMS:
            return _MoneyResolved(
                None,
                "wrong rate purpose",
                [
                    f"CUSTOMS_FX_RATE_WRONG_PURPOSE:{rate.purpose.value}: "
                    "reference/payment rates are not customs rates"
                ],
            )
        if {rate.base, rate.quote} != {source, target}:
            return _MoneyResolved(
                None, "rate pair mismatch", [f"CUSTOMS_FX_RATE_PAIR_MISMATCH:{rate.base}/{rate.quote}"]
            )
        start, end, on = (
            self.inputs.customs_fx_valid_from,
            self.inputs.customs_fx_valid_to,
            self.inputs.declaration_date,
        )
        warnings: list[str] = []
        if on is not None and rate.rate_date > on:
            # A rate observed after the declaration date cannot be the rate prescribed for it.
            return _MoneyResolved(
                None,
                "rate dated after declaration",
                [f"CUSTOMS_FX_RATE_AFTER_DECLARATION:{rate.rate_date.isoformat()}>{on.isoformat()}"],
            )
        if start or end:
            if on is None:
                return _MoneyResolved(None, "rate period unverifiable", ["CUSTOMS_FX_PERIOD_UNVERIFIABLE"])
            if (start and on < start) or (end and on > end):
                return _MoneyResolved(None, "rate not effective", ["CUSTOMS_FX_RATE_NOT_EFFECTIVE"])
        else:
            warnings.append("CUSTOMS_FX_EFFECTIVE_PERIOD_NOT_RECORDED")
        converted = rate.convert(Money(amount=amount, currency=source), target)
        self.customs_rate_used = rate
        display = (
            f"{_show(amount)} {source} -> {_show(converted.amount)} {target} "
            f"(customs {rate.base}/{rate.quote}={rate.rate} {rate.rate_date.isoformat()} {rate.provider})"
        )
        return _MoneyResolved(converted.amount, display, warnings)


def convert_with_customs_rate(
    money: Money, target: str, inputs: TaxInputs
) -> tuple[Money | None, tuple[str, ...]]:
    """``money`` in ``target`` with the customs rate of ``inputs``, under exactly the checks the
    engine applies to money inputs: customs purpose, the exact currency pair, a rate dated no
    later than the declaration date and inside the recorded effective period.

    Returns the unrounded amount (or ``None`` when the rate cannot be used) and the warning
    codes. Never falls back to a reference or payment rate.
    """
    resolved = _Resolver(inputs, target).convert_amount(
        money.amount, money.currency, target, f"{money.currency}->{target}"
    )
    amount = None if resolved.amount is None else Money(amount=resolved.amount, currency=target)
    return amount, tuple(resolved.warnings)


#: Plain notation only within this exponent range: beyond it a decimal such as ``1E-100000000``
#: (a valid, in-range CO2 figure from crawled JSON) would expand to one character per unit of
#: exponent, so it is shown in scientific notation instead.
_PLAIN_EXPONENT_LIMIT: Final = 30


def _show(value: Scalar) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        exp = value.as_tuple().exponent
        if isinstance(exp, int) and -_PLAIN_EXPONENT_LIMIT <= exp <= _PLAIN_EXPONENT_LIMIT:
            return format(value, "f")  # plain notation: an exact quotient never shows as 1.00E+4
        return str(value)
    return str(value)


_NOT_APPLICABLE: Final = object()


@dataclass(slots=True)
class _Eval:
    status: ComponentStatus
    raw: Decimal | None = None
    inputs_used: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _compare(op: str, left: Scalar, right: object) -> bool:
    if op in ("in", "not_in"):
        assert isinstance(right, tuple)
        found = str(left) in right
        return found if op == "in" else not found
    if isinstance(left, Decimal):
        target: Scalar = _as_decimal(right)
    elif isinstance(left, date):
        target = date.fromisoformat(str(right))
    else:
        target = str(right)
    ops: dict[str, Callable[[Any, Any], bool]] = {
        "eq": lambda a, b: a == b,
        "ne": lambda a, b: a != b,
        "lt": lambda a, b: a < b,
        "le": lambda a, b: a <= b,
        "gt": lambda a, b: a > b,
        "ge": lambda a, b: a >= b,
    }
    return ops[op](left, target)


def _applies(comp: _ComponentBase, resolver: _Resolver, ev: _Eval) -> bool | None:
    """Kleene AND over predicates: False dominates, then unknown, else True."""
    outcome: bool | None = True
    for pred in comp.applies_when:
        resolved = resolver.scalar(pred.input)
        ev.inputs_used[pred.input] = resolved.display
        ev.warnings.extend(resolved.warnings)
        if resolved.value is None:
            ev.missing.append(pred.input)
            if outcome is True:
                outcome = None
            continue
        if not _compare(pred.op, resolved.value, pred.value):
            outcome = False
    return outcome


def _sum_terms(
    terms: Sequence[str],
    resolver: _Resolver,
    dependents: Mapping[str, Decimal | object | None],
    ev: _Eval,
) -> Decimal | None:
    total = Decimal(0)
    known = True
    for term in terms:
        if term in dependents:
            value = dependents[term]
            if value is _NOT_APPLICABLE:
                ev.inputs_used[term] = "not_applicable (contributes nothing)"
                continue
            if value is None:
                ev.inputs_used[term] = "unknown"
                ev.missing.append(term)
                known = False
                continue
            assert isinstance(value, Decimal)
            ev.inputs_used[term] = f"{_show(value)} {resolver.currency}"
            total = _CTX.add(total, value)
            continue
        money = resolver.money(term)
        ev.inputs_used[term] = money.display
        ev.warnings.extend(money.warnings)
        if money.amount is None:
            ev.missing.append(term)
            known = False
            continue
        total = _CTX.add(total, money.amount)
    return total if known else None


def _bracket_key(
    comp: BracketComponent,
    resolver: _Resolver,
    dependents: Mapping[str, Decimal | object | None],
    ev: _Eval,
) -> Decimal | None:
    """Sum of ``key_terms`` in the rule currency, converted to ``key_currency`` (customs rate)."""
    assert comp.key_terms is not None
    total = _sum_terms(comp.key_terms, resolver, dependents, ev)
    if total is None:
        ev.inputs_used["key"] = "unknown"
        return None
    target = comp.key_currency or resolver.currency
    key = resolver.convert_amount(total, resolver.currency, target, f"{comp.id}:key")
    ev.inputs_used["key"] = key.display
    ev.warnings.extend(key.warnings)
    if key.amount is None:
        ev.missing.append("customs_fx_rate")
    return key.amount


def _evaluate_bracket(
    comp: BracketComponent,
    resolver: _Resolver,
    dependents: Mapping[str, Decimal | object | None],
    ev: _Eval,
) -> Decimal | None:
    if comp.key_terms is not None:
        key = _bracket_key(comp, resolver, dependents, ev)
        if key is None:
            return None
        return _bracket_outcome(
            comp, comp.brackets or (), key, subject="key", resolver=resolver, dependents=dependents, ev=ev
        )
    assert comp.input is not None
    resolved = resolver.scalar(comp.input)
    ev.inputs_used[comp.input] = resolved.display
    ev.warnings.extend(resolved.warnings)
    if resolved.value is None:
        ev.missing.append(comp.input)
        return None
    assert isinstance(resolved.value, Decimal)
    value = resolved.value
    if comp.tables is not None:
        cycle = resolver.inputs.co2_cycle
        ev.inputs_used["co2_cycle"] = cycle.value
        if cycle == Co2Cycle.UNKNOWN:
            ev.missing.append("co2_cycle")
            ev.warnings.append("CO2_CYCLE_UNKNOWN: no WLTP/NEDC conversion is performed")
            return None
        rows = comp.tables.get(cycle)
        if rows is None:
            ev.warnings.append(
                f"CO2_CYCLE_NOT_SUPPORTED:{cycle.value}: the rule has no table for this cycle; "
                "WLTP and NEDC values are never converted"
            )
            return None
    else:
        rows = comp.brackets or ()
    if comp.integer_values_only and value != value.to_integral_value():
        # The rule has bands for whole values only; which band a fraction belongs to is not
        # established, so the amount stays unknown (never silently the lower band).
        ev.warnings.append(f"VALUE_NOT_INTEGER_NO_BAND_RULE:{comp.input}={_show(value)}")
        return None
    return _bracket_outcome(
        comp, rows, value, subject=comp.input, resolver=resolver, dependents=dependents, ev=ev
    )


def _bracket_outcome(
    comp: BracketComponent,
    rows: Sequence[Bracket],
    value: Decimal,
    *,
    subject: str,
    resolver: _Resolver,
    dependents: Mapping[str, Decimal | object | None],
    ev: _Eval,
) -> Decimal | None:
    match = next((row for row in rows if row.contains(value)), None)
    if match is None:
        ev.warnings.append(f"VALUE_OUTSIDE_BRACKETS:{subject}={_show(value)}")
        return None
    ev.inputs_used["bracket"] = match.display()
    if match.amount is not None:
        return match.amount
    if match.amount_per_unit is not None:
        return _CTX.multiply(value, match.amount_per_unit)
    if match.amount_per_unit_above_lower is not None:
        excess = _CTX.subtract(value, match.lower_bound())
        return _CTX.multiply(excess, match.amount_per_unit_above_lower)
    assert match.rate_of_base is not None
    base = _sum_terms(comp.base, resolver, dependents, ev)
    return None if base is None else _CTX.multiply(base, match.rate_of_base)


def _evaluate_amount(
    comp: Component,
    resolver: _Resolver,
    dependents: Mapping[str, Decimal | object | None],
    ev: _Eval,
) -> Decimal | None:
    if isinstance(comp, FixedComponent):
        return comp.amount
    if isinstance(comp, PercentageComponent):
        base = _sum_terms(comp.base, resolver, dependents, ev)
        ev.inputs_used["rate"] = _show(comp.rate)
        return None if base is None else _CTX.multiply(base, comp.rate)
    if isinstance(comp, PerUnitComponent):
        resolved = resolver.scalar(comp.input)
        ev.inputs_used[comp.input] = resolved.display
        ev.warnings.extend(resolved.warnings)
        if resolved.value is None:
            ev.missing.append(comp.input)
            return None
        assert isinstance(resolved.value, Decimal)
        units = resolved.value
        if comp.threshold is not None:
            units = max(Decimal(0), _CTX.subtract(units, comp.threshold))
            ev.inputs_used["threshold"] = _show(comp.threshold)
        return _CTX.multiply(units, comp.amount_per_unit)
    return _evaluate_bracket(comp, resolver, dependents, ev)


def _resolve_alternative_groups(rule_set: RuleSet, applicability: dict[str, bool | None]) -> dict[str, str]:
    """Return component_id -> warning for alternatives whose group is not exactly-one-true."""
    problems: dict[str, str] = {}
    groups: dict[str, list[str]] = {}
    for comp in rule_set.components:
        if comp.alternative_group:
            groups.setdefault(comp.alternative_group, []).append(comp.id)
    for group, members in groups.items():
        states = [applicability[m] for m in members]
        if any(s is None for s in states):
            continue  # members are unknown already
        true_count = sum(1 for s in states if s)
        if true_count == 1:
            continue
        warning = (
            f"ALTERNATIVE_GROUP_{'GAP' if true_count == 0 else 'AMBIGUOUS'}:{group}: "
            f"{true_count} alternatives apply; exactly one is required"
        )
        for member in members:
            problems[member] = warning
    return problems


def calculate(rule_set: RuleSet, inputs: TaxInputs, as_of: datetime) -> TaxCalculation:
    """Evaluate ``rule_set`` for ``inputs``. Pure and deterministic.

    The rule set is verified first (fail closed). Its effectiveness is checked against
    the declaration date (or ``as_of`` when the declaration date is unknown, with a
    warning). Components are evaluated in order; unknown inputs propagate to
    dependants; a not-applicable dependency contributes nothing. The result is complete
    only when every component is resolved or not applicable and every required input is
    present. Intermediate components are reported but never summed.
    """
    validate_rule_set(rule_set)
    as_of = ensure_utc(as_of)
    warnings: list[str] = []
    if rule_set.status == TaxRuleStatus.REVOKED:
        raise ValidationFailed(f"tax rule set {rule_set.label()} is revoked and must not be used")
    if rule_set.currency is None:
        raise ValidationFailed("tax rule set has no currency; nothing can be calculated")
    currency = rule_set.currency
    if inputs.jurisdiction is not None and inputs.jurisdiction != rule_set.jurisdiction:
        raise ValidationFailed("inputs are for a different jurisdiction than the rule set")
    classified = inputs.classification
    if (
        classified is not None
        and classified.approval_status == "approved"
        and classified.vehicle_category is not None
        and rule_set.vehicle_categories
        and classified.vehicle_category not in rule_set.vehicle_categories
    ):
        raise ValidationFailed("the approved vehicle classification is not a category this rule set covers")
    effective_date = inputs.declaration_date or as_of.date()
    if inputs.declaration_date is None:
        warnings.append("DECLARATION_DATE_UNKNOWN: rule effectiveness checked against as_of date")
    open_start = rule_set.status != TaxRuleStatus.ACTIVE
    if not rule_set.is_effective_on(effective_date, open_start=open_start):
        raise ValidationFailed(
            f"tax rule set {rule_set.label()} is not effective on {effective_date.isoformat()}"
        )
    if rule_set.status != TaxRuleStatus.ACTIVE:
        warnings.append(f"RULE_SET_NOT_ACTIVE:{rule_set.status.value}: result is not production-ready")
    if rule_set.is_fixture:
        warnings.append("FIXTURE_RULE_SET: synthetic rules; never a real tax amount")

    resolver = _Resolver(inputs, currency)
    missing_required = tuple(name for name in rule_set.required_inputs if not resolver.present(name))
    for name in missing_required:
        warnings.append(f"REQUIRED_INPUT_MISSING:{name}")

    default_rounding = rule_set.rounding_rules.component_default if rule_set.rounding_rules else None
    evals: dict[str, _Eval] = {}
    applicability: dict[str, bool | None] = {}
    for comp in rule_set.components:
        ev = _Eval(status=ComponentStatus.UNKNOWN)
        applicability[comp.id] = _applies(comp, resolver, ev)
        evals[comp.id] = ev
    group_problems = _resolve_alternative_groups(rule_set, applicability)

    dependents: dict[str, Decimal | object | None] = {}
    results: list[ComponentResult] = []
    for comp in rule_set.components:
        ev = evals[comp.id]
        applies = applicability[comp.id]
        rounding = comp.rounding or default_rounding
        reported: Decimal | None = None
        if comp.id in group_problems:
            ev.warnings.append(group_problems[comp.id])
            ev.status = ComponentStatus.UNKNOWN
        elif applies is False:
            ev.status = ComponentStatus.NOT_APPLICABLE
        elif applies is None:
            ev.status = ComponentStatus.UNKNOWN
        else:
            ev.raw = _evaluate_amount(comp, resolver, dependents, ev)
            ev.status = ComponentStatus.UNKNOWN if ev.raw is None else ComponentStatus.RESOLVED
        if ev.status == ComponentStatus.RESOLVED:
            assert ev.raw is not None
            reported = apply_rounding(ev.raw, rounding) if rounding else ev.raw
            feeds = reported if rounding is None or rounding.stage == "before_dependents" else ev.raw
            dependents[comp.id] = feeds
        elif ev.status == ComponentStatus.NOT_APPLICABLE:
            dependents[comp.id] = _NOT_APPLICABLE
        else:
            dependents[comp.id] = None
        results.append(
            ComponentResult(
                component_id=comp.id,
                label=comp.label,
                category=comp.category,
                kind=comp.kind,
                rule_set_id=rule_set.rule_set_id,
                version=rule_set.version,
                status=ev.status,
                amount=None if reported is None else Money(amount=reported, currency=currency),
                unrounded_amount=ev.raw if ev.status == ComponentStatus.RESOLVED else None,
                rounding=rounding if ev.status == ComponentStatus.RESOLVED else None,
                inputs_used=dict(ev.inputs_used),
                missing_inputs=tuple(dict.fromkeys(ev.missing)),
                warnings=tuple(dict.fromkeys(ev.warnings)),
                intermediate=comp.intermediate,
            )
        )

    # An unknown helper keeps the calculation incomplete; a resolved one is never summed.
    unknown = tuple(r.component_id for r in results if r.status == ComponentStatus.UNKNOWN)
    resolved_amounts = [r.amount.amount for r in results if r.amount is not None and not r.intermediate]
    complete = not unknown and not missing_required
    total: Money | None = None
    subtotal: Money | None = None
    summed = Decimal(0)
    for amount in resolved_amounts:
        summed = _CTX.add(summed, amount)
    if complete:
        total_rounding = rule_set.rounding_rules.total if rule_set.rounding_rules else None
        if total_rounding is not None:
            summed = apply_rounding(summed, total_rounding)
        total = Money(amount=summed, currency=currency)
    elif resolved_amounts:
        subtotal = Money(amount=summed, currency=currency)
    if not complete:
        warnings.append("INCOMPLETE: only known_subtotal is available; it is not a total import cost")
    return TaxCalculation(
        rule_set_id=rule_set.rule_set_id,
        version=rule_set.version,
        rule_status=rule_set.status,
        rule_sha256=rule_set.sha256,
        is_fixture=rule_set.is_fixture,
        jurisdiction=rule_set.jurisdiction,
        currency=currency,
        effective_date=effective_date,
        rule_valid_from=rule_set.valid_from,
        rule_valid_to=rule_set.valid_to,
        as_of=as_of,
        customs_fx_rate=resolver.customs_rate_used,
        components=tuple(results),
        complete=complete,
        total_import_cost=total,
        known_subtotal=subtotal,
        unknown_components=unknown,
        missing_inputs=missing_required,
        warnings=tuple(dict.fromkeys(warnings)),
    )
