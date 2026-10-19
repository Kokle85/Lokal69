"""MK comparable selection, exclusion and statistics (spec sections 15, 18, 31 "Comparables").

Pure domain code: no I/O, deterministic for identical inputs. ``select_comparables`` turns one
target vehicle plus candidate North Macedonian market observations into one inspectable,
reproducible ``ComparableSetResult``.

Business rules (spec 15):

- A European asking price never proves MK market value. Comparables are never fabricated: an
  empty or inadequate set returns ``insufficient_comparables`` with ``research_needed=True``.
- Matching order follows spec 15: make and exact model, generation/facelift, engine
  (code, displacement within 10 %, power within 15 % when both known), fuel, gearbox category,
  driven wheels (2WD versus AWD/4WD), year and mileage band, condition, then context
  (local registration, seller type, warranty, price basis).
- Never widened across make/model, fuel type, gearbox category, 2WD versus AWD/4WD, engine or
  generation (when both generations are known). An *unknown* fuel/gearbox/drive on either side
  cannot be shown not to cross those boundaries, so the candidate is excluded as
  ``*_UNVERIFIED`` instead of being silently accepted.
- Matching starts tight (registration year +/- ``comparable_year_window``, mileage
  +/- ``comparable_mileage_window_km``; PROPOSED defaults 1 year / 30,000 km, not statistically
  validated) and widens ONE dimension per step, in the fixed order mileage -> year -> facelift,
  only while the adequacy count is below ``comparable_min_sample``. Every widened step and every
  comparable admitted only by a widened step carry explicit labels.
- An unknown target generation labels every selected comparable ``generation_unverified``.
- Duplicates are removed per evidence kind, by vehicle cluster *and* by ad URL (``ad_url_key``:
  the same ad recorded again on a later day): linked rows form one group (transitively), the
  newest observation that passes the hard checks is kept (none passes: the newest; equal times:
  the smallest id) and the others are recorded as ``DUPLICATE_OF_CLUSTER`` and/or
  ``DUPLICATE_OF_URL`` with the kept id.
- Evidence kinds are never mixed: statistics are computed per ``EvidenceKind``. Removed or
  sold-claimed listings stay *asking-price* evidence; an advertised price at removal is never a
  sale price. ``owner_estimate`` is an assumption, not a comparable: it never enters a market
  set. ``select_comparables(..., owner_estimates=True)`` instead selects ONLY the owner's
  estimates (same matching rules, never ``adequate``, always ``research_needed``) so a valuation
  without any MK market evidence can still show a labelled owner-estimate orientation
  (``proceeds_from_owner_estimates``); it never replaces asking-price or sale evidence.
- Names: with a ``taxonomy`` the make and model of the target and of every candidate are compared
  by their canonical taxonomy names (``Skoda``/``Škoda``, ``Mokka X``/``Mokka``), so an imported
  MK row and a foreign listing that spell the same model differently still match.
- Statistics: n, min, max and median always (for n >= 1); quartiles only when n >= 5. Small
  samples are labelled; seller-reported sale statistics are labelled ``unverified_claims`` at any
  size; every individual point stays visible next to the aggregate. The MK band fit compares
  the unrounded asking median (spec 3 compare-before-rounding).
- Condition: a damaged or non-running comparable never values a running target; a damaged or
  non-running target valued from running comparables is labelled on every such comparable.
- Stale evidence (older than ``comparable_max_age_days`` at ``as_of``) is excluded but the
  caller keeps it historically. Evidence observed after ``as_of`` is excluded so a set is
  reproducible as of its computation time.
- Amounts are converted to EUR with the recorded FX rates (explicit direction, unrounded): per
  currency the latest *reference* rate dated on or before ``as_of`` (customs/payment rates and
  later-dated rates are never used), independent of input order; the rate used is reported and
  an old rate is flagged. An unconvertible currency excludes the candidate; it is never zero.

MK price orientation (owner decision 2026-10-16, ADR 0005): the MK resale rules are a framework
for orientation, not a strict gate. ``orient_comparables`` is a SEPARATE, never-persisted relaxed
view over one evidence kind (MK asking prices or the owner's estimates): only the same canonical
make and model, the MK market, the fixture lineage, a convertible price, observation on or
before ``as_of``, freshness and condition (damaged versus running) stay hard. Every other
mismatch or unknown (generation, facelift, engine, fuel, gearbox, drive, year, mileage) is a
labelled ``orientation_*`` difference with a documented distance penalty; the
``ORIENTATION_LIMIT`` nearest points are kept. The penalties and the limit are PROPOSED, not
statistically validated. The strict spec 15 ``ComparableSetResult`` above is never changed by it.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import (
    Availability,
    ClaimStatus,
    Confidence,
    CostLineStatus,
    Drive,
    EvidenceKind,
    Fuel,
    FxPurpose,
    Gearbox,
    PriceBasis,
    SellerType,
    Tristate,
)
from suv_deals.domain.listings import NormalizedListing, VehicleSpec
from suv_deals.domain.money import FxRate, Money, convert_to_eur, to_decimal
from suv_deals.domain.profiles import MK_ASKING_BAND_MAX_EUR, MK_ASKING_BAND_MIN_EUR
from suv_deals.domain.taxonomy import VehicleTaxonomy
from suv_deals.errors import ValidationFailed

CRITERIA_VERSION: Final = "mk-comparables@1.3.0"

#: Relative engine tolerances (spec 15 work package: displacement 10 %, power 15 %).
DISPLACEMENT_TOLERANCE: Final = Decimal("0.10")
POWER_TOLERANCE: Final = Decimal("0.15")
#: Quartiles are only reported from this sample size on.
QUARTILE_MIN_SAMPLE: Final = 5
#: Widening ladder: mileage window multiplied by this factor, year window grown by this many years.
MILEAGE_WIDEN_FACTOR: Final = Decimal(2)
YEAR_WIDEN_STEP: Final = 1
#: Bound on input size so one call cannot be used as an unbounded workload.
MAX_CANDIDATES: Final = 5000
#: FX rates older than this at ``as_of`` are flagged (same as the SearchProfile.fx_max_age_days default).
FX_RATE_WARN_AGE_DAYS: Final = 7
#: Weights by match level. Informational inclusion weights; statistics stay unweighted.
MATCH_WEIGHTS: Final[dict[str, Decimal]] = {
    "exact": Decimal("1.000000"),
    "close": Decimal("0.750000"),
    "widened": Decimal("0.500000"),
}
#: Evidence kinds whose selected count can make a set adequate (seller sale claims are unverified).
ADEQUACY_KINDS: Final = (EvidenceKind.ASKING_PRICE, EvidenceKind.VERIFIED_SALE)
#: The only kind an owner-estimate selection counts (it is never an adequate market sample).
OWNER_ESTIMATE_KINDS: Final = (EvidenceKind.OWNER_ESTIMATE,)
OWNER_ESTIMATE_LABEL: Final = "owner estimate (the owner's assumption; not MK market evidence)"
_AMOUNT_QUANTUM: Final = Decimal("0.000001")
_CENT: Final = Decimal("0.01")
_FROZEN = ConfigDict(frozen=True, extra="forbid")

LocalRegistrationStatus = Literal["locally_registered", "imported_unregistered", "unknown"]
ComparableStatus = Literal["adequate", "small_sample", "insufficient_comparables"]
MatchLevel = Literal["exact", "close", "widened"]
BandFit = Literal["below", "within", "above", "unknown"]
#: ``unverified_claims`` labels seller-reported sale statistics: never ``adequate`` at any size.
SampleLabel = Literal["single_observation", "small_sample", "adequate", "unverified_claims"]
WidenDimension = Literal["mileage", "year", "facelift"]

_STATUS_TO_DB_QUALITY: Final[dict[str, str]] = {
    "adequate": "adequate",
    "small_sample": "small",
    "insufficient_comparables": "insufficient",
}
_LEVEL_ORDER: Final[dict[str, int]] = {"exact": 0, "close": 1, "widened": 2}
_WIDEN_ORDER: Final[tuple[WidenDimension, ...]] = ("mileage", "year", "facelift")
_TWO_WD: Final = frozenset({Drive.FWD, Drive.RWD})
_ALL_WD: Final = frozenset({Drive.AWD, Drive.FOUR_WD})

_EVIDENCE_NOTES: Final[dict[EvidenceKind, str]] = {
    EvidenceKind.ASKING_PRICE: "advertised asking prices; not realized sale prices",
    EvidenceKind.SELLER_REPORTED_SALE: "unverified seller statements about transactions",
    EvidenceKind.VERIFIED_SALE: "sales supported by permitted transaction evidence",
    EvidenceKind.OWNER_ESTIMATE: "owner assumption; not market evidence",
}


class ExclusionReason(StrEnum):
    """Typed reasons a candidate is not in the selected set. Listed in spec 15 matching order."""

    WRONG_MARKET = "WRONG_MARKET"
    FIXTURE_MISMATCH = "FIXTURE_MISMATCH"
    NOT_COMPARABLE_EVIDENCE_KIND = "NOT_COMPARABLE_EVIDENCE_KIND"
    PRICE_MISSING = "PRICE_MISSING"
    CURRENCY_UNCONVERTIBLE = "CURRENCY_UNCONVERTIBLE"
    OBSERVED_AFTER_AS_OF = "OBSERVED_AFTER_AS_OF"
    STALE = "STALE"
    DUPLICATE_OF_CLUSTER = "DUPLICATE_OF_CLUSTER"
    DUPLICATE_OF_URL = "DUPLICATE_OF_URL"
    MODEL_UNKNOWN = "MODEL_UNKNOWN"
    WRONG_MODEL = "WRONG_MODEL"
    WRONG_GENERATION = "WRONG_GENERATION"
    FACELIFT_MISMATCH = "FACELIFT_MISMATCH"
    ENGINE_MISMATCH = "ENGINE_MISMATCH"
    FUEL_MISMATCH = "FUEL_MISMATCH"
    FUEL_UNVERIFIED = "FUEL_UNVERIFIED"
    GEARBOX_MISMATCH = "GEARBOX_MISMATCH"
    GEARBOX_UNVERIFIED = "GEARBOX_UNVERIFIED"
    DRIVE_MISMATCH = "DRIVE_MISMATCH"
    DRIVE_UNVERIFIED = "DRIVE_UNVERIFIED"
    YEAR_UNKNOWN = "YEAR_UNKNOWN"
    YEAR_OUT_OF_WINDOW = "YEAR_OUT_OF_WINDOW"
    MILEAGE_UNKNOWN = "MILEAGE_UNKNOWN"
    MILEAGE_OUT_OF_WINDOW = "MILEAGE_OUT_OF_WINDOW"
    CONDITION_MISMATCH = "CONDITION_MISMATCH"


_REASON_ORDER: Final[dict[ExclusionReason, int]] = {r: i for i, r in enumerate(ExclusionReason)}


class DifferenceSeverity(StrEnum):
    INFO = "info"  # numeric delta inside the tight window
    CONTEXT = "context"  # registration/seller/warranty/condition/price-basis context; no level effect
    UNVERIFIED = "unverified"  # a matching dimension could not be compared (unknown on a side)
    DIFFERS = "differs"  # known values differ but within the accepted tolerance
    WIDENED = "widened"  # admitted only because a widening step was applied


class ComparableConfig(Protocol):
    """The subset of ``BusinessConfig`` the matcher needs (``BusinessConfig`` satisfies it)."""

    @property
    def comparable_year_window(self) -> int: ...

    @property
    def comparable_mileage_window_km(self) -> Decimal: ...

    @property
    def comparable_max_age_days(self) -> int: ...

    @property
    def comparable_min_sample(self) -> int: ...


# ---------------------------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------------------------


class ConditionSummary(BaseModel):
    """Condition statements attached to an MK observation; each is a claim, not an inspection."""

    model_config = _FROZEN

    accident_free: ClaimStatus = ClaimStatus.UNKNOWN
    running: ClaimStatus = ClaimStatus.UNKNOWN
    damaged_vehicle: ClaimStatus = ClaimStatus.UNKNOWN
    documented_faults: tuple[str, ...] = Field(default=(), max_length=30)


class MarketObservation(BaseModel):
    """One MK market evidence row (``app.market_observations``).

    ``local_registration_status`` uses the persisted values; the alias ``registered_mk`` is
    accepted on input and normalised to ``locally_registered``.
    """

    model_config = _FROZEN

    id: UUID
    source_key: str = Field(min_length=1, max_length=80)
    url: str | None = Field(default=None, min_length=8, max_length=2048)
    observed_at: datetime
    evidence_kind: EvidenceKind
    amount: Money | None = None
    price_basis: PriceBasis = PriceBasis.UNKNOWN
    vehicle: VehicleSpec = VehicleSpec()
    local_registration_status: LocalRegistrationStatus = "unknown"
    seller_type: SellerType = SellerType.UNKNOWN
    warranty: Tristate = Tristate.UNKNOWN
    cluster_id: UUID | None = None
    availability: Availability = Availability.UNKNOWN
    condition: ConditionSummary = ConditionSummary()
    market: str = Field(default="MK", pattern=r"^[A-Z]{2}$")
    is_fixture: bool = False

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("local_registration_status", mode="before")
    @classmethod
    def _alias(cls, value: object) -> object:
        return "locally_registered" if value == "registered_mk" else value

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is not None and not value.lower().startswith(("https://", "http://")):
            raise ValueError("url must be an http(s) URL")
        return value

    @field_validator("amount")
    @classmethod
    def _non_negative(cls, value: Money | None) -> Money | None:
        if value is not None and value.amount < 0:
            raise ValueError("amount must not be negative")
        return value


class ComparableTarget(BaseModel):
    """The vehicle being valued, reduced to the comparable matching dimensions."""

    model_config = _FROZEN

    listing_id: UUID | None = None
    make: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=120)
    generation: str | None = Field(default=None, max_length=80)
    facelift: Tristate = Tristate.UNKNOWN
    engine_code: str | None = Field(default=None, max_length=40)
    engine_displacement_cm3: int | None = Field(default=None, ge=50, le=10000)
    power_kw: int | None = Field(default=None, ge=1, le=2000)
    fuel: Fuel = Fuel.UNKNOWN
    gearbox: Gearbox = Gearbox.UNKNOWN
    gearbox_subtype: str | None = Field(default=None, max_length=80)
    drive: Drive = Drive.UNKNOWN
    year: int | None = Field(default=None, ge=1950, le=2100)
    year_source: Literal["first_registration", "model_year", "unknown"] = "unknown"
    mileage_km: Decimal | None = Field(default=None, ge=0)
    damaged_vehicle: ClaimStatus = ClaimStatus.UNKNOWN
    running: ClaimStatus = ClaimStatus.UNKNOWN
    is_fixture: bool = False

    @classmethod
    def from_listing(
        cls, listing: NormalizedListing, *, listing_id: UUID | None = None, is_fixture: bool = False
    ) -> ComparableTarget:
        vehicle = listing.vehicle
        year, source = _vehicle_year(vehicle)
        return cls(
            listing_id=listing_id,
            make=vehicle.make,
            model=vehicle.model,
            generation=vehicle.generation,
            facelift=vehicle.facelift,
            engine_code=vehicle.engine_code,
            engine_displacement_cm3=vehicle.engine_displacement_cm3,
            power_kw=vehicle.power_kw,
            fuel=vehicle.fuel,
            gearbox=vehicle.gearbox,
            gearbox_subtype=vehicle.gearbox_subtype,
            drive=vehicle.drive,
            year=year,
            year_source=source,
            mileage_km=vehicle.mileage_km,
            damaged_vehicle=listing.condition.damaged_vehicle,
            running=listing.condition.running,
            is_fixture=is_fixture,
        )

    def with_canonical_names(self, taxonomy: VehicleTaxonomy | None) -> ComparableTarget:
        """This target with the taxonomy's canonical make and model (see `canonical_names`)."""
        make, model = canonical_names(taxonomy, self.make, self.model)
        if (make, model) == (self.make, self.model):
            return self
        return self.model_copy(update={"make": make, "model": model})


def canonical_names(
    taxonomy: VehicleTaxonomy | None, make: str | None, model: str | None
) -> tuple[str | None, str | None]:
    """The taxonomy's canonical make and model (``Skoda``/``Mokka X`` -> ``Škoda``/``Mokka``).

    Only a confident match of both fields counts (a model name that starts with a known alias);
    anything else keeps the names as given, so an unknown model still matches only itself.
    """
    if taxonomy is None or not make or not model:
        return make, model
    match = taxonomy.match_vehicle(make, model, None, None)
    if match.confidence != Confidence.HIGH or match.make is None or match.model is None:
        return make, model
    return match.make, match.model


# ---------------------------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------------------------


class MatchDifference(BaseModel):
    model_config = _FROZEN

    dimension: str = Field(max_length=40)
    code: str = Field(max_length=80)
    severity: DifferenceSeverity
    target: str | None = Field(default=None, max_length=200)
    comparable: str | None = Field(default=None, max_length=200)


class SelectedComparable(BaseModel):
    model_config = _FROZEN

    observation_id: UUID
    evidence_kind: EvidenceKind
    weight: Decimal
    match_level: MatchLevel
    differences: tuple[MatchDifference, ...]
    widened_dimensions: tuple[WidenDimension, ...]
    amount_eur: Decimal  # EUR equivalent, 6 decimal places (reproducible input to statistics)
    observation: MarketObservation


class ExcludedComparable(BaseModel):
    model_config = _FROZEN

    observation_id: UUID
    evidence_kind: EvidenceKind
    reasons: tuple[ExclusionReason, ...] = Field(min_length=1)
    duplicate_of: UUID | None = None
    details: tuple[str, ...] = ()


class StatPoint(BaseModel):
    """One visible member of an aggregate, so a single poor comparable is never hidden."""

    model_config = _FROZEN

    observation_id: UUID
    amount_eur: Decimal
    match_level: MatchLevel
    observed_at: datetime
    difference_codes: tuple[str, ...]


class EvidenceStats(BaseModel):
    """Statistics for exactly one evidence kind. Amounts are EUR, rounded to cents for display."""

    model_config = _FROZEN

    evidence_kind: EvidenceKind
    evidence_note: str
    currency: Literal["EUR"] = "EUR"
    n: int = Field(ge=1)
    sample_label: SampleLabel
    min: Decimal
    max: Decimal
    median: Decimal
    q1: Decimal | None = None
    q3: Decimal | None = None
    date_from: datetime
    date_to: datetime
    match_quality: dict[str, int]
    points: tuple[StatPoint, ...]


class WideningStep(BaseModel):
    model_config = _FROZEN

    step: int = Field(ge=1)
    dimension: WidenDimension
    label: str
    selected_count_after: int
    adequacy_count_after: int


class MatchingCriteria(BaseModel):
    model_config = _FROZEN

    criteria_version: str = CRITERIA_VERSION
    year_window: int
    mileage_window_km: Decimal
    max_year_window: int
    max_mileage_window_km: Decimal
    max_age_days: int
    min_sample: int
    displacement_tolerance: Decimal = DISPLACEMENT_TOLERANCE
    power_tolerance: Decimal = POWER_TOLERANCE
    widening_order: tuple[WidenDimension, ...] = _WIDEN_ORDER


class ComparableSetResult(BaseModel):
    """Reproducible comparable selection for one target (persisted as ``app.comparable_sets``)."""

    model_config = _FROZEN

    criteria_version: str = CRITERIA_VERSION
    as_of: datetime
    target: ComparableTarget
    criteria: MatchingCriteria
    selected: tuple[SelectedComparable, ...]
    excluded: tuple[ExcludedComparable, ...]
    stats: tuple[EvidenceStats, ...]
    status: ComparableStatus
    widening_steps: tuple[WideningStep, ...]
    mk_band_fit: BandFit
    mk_band_fit_basis: str
    research_needed: bool
    warnings: tuple[str, ...] = ()

    @property
    def sample_quality(self) -> str:
        """The persisted ``comparable_sets.sample_quality`` value (adequate, small, insufficient)."""
        return _STATUS_TO_DB_QUALITY[self.status]

    @property
    def adequacy_count(self) -> int:
        return _adequacy_count(self.selected)

    def stats_for(self, kind: EvidenceKind) -> EvidenceStats | None:
        for item in self.stats:
            if item.evidence_kind == kind:
                return item
        return None

    @property
    def date_span(self) -> tuple[datetime, datetime] | None:
        if not self.selected:
            return None
        times = [s.observation.observed_at for s in self.selected]
        return min(times), max(times)


# ---------------------------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------------------------


class _Evaluation:
    """Mutable per-candidate working state (internal)."""

    __slots__ = ("amount_eur", "differences", "hard", "needs", "notes", "obs", "prefilter", "widen_failures")

    def __init__(self, obs: MarketObservation) -> None:
        self.obs = obs
        self.prefilter: list[ExclusionReason] = []
        self.hard: list[ExclusionReason] = []
        self.differences: list[MatchDifference] = []
        # widenable dimension -> (difference to attach when admitted by widening)
        self.needs: dict[WidenDimension, MatchDifference] = {}
        # widenable dimension -> exclusion reason when outside even the widest window
        self.widen_failures: list[ExclusionReason] = []
        self.amount_eur: Decimal | None = None
        self.notes: list[str] = []

    def reject(self, reason: ExclusionReason, note: str) -> None:
        self.hard.append(reason)
        self.notes.append(_bounded(f"{reason.value}: {note}"))

    def required_level(self) -> int:
        return max((_WIDEN_ORDER.index(d) + 1 for d in self.needs), default=0)


def select_comparables(
    target: ComparableTarget,
    candidates: Sequence[MarketObservation],
    config: ComparableConfig,
    as_of: datetime,
    fx_rates: Sequence[FxRate] = (),
    *,
    owner_estimates: bool = False,
    taxonomy: VehicleTaxonomy | None = None,
) -> ComparableSetResult:
    """Select, exclude and summarise MK comparables for ``target`` (spec 15).

    Deterministic: the same inputs always produce the same result, independent of candidate
    order. Raises ``ValidationFailed`` for malformed input (duplicate ids, too many candidates,
    naive ``as_of``).

    ``owner_estimates=True`` selects only ``owner_estimate`` rows (every other kind is excluded
    as ``NOT_COMPARABLE_EVIDENCE_KIND``): widening stops at the first admitted estimate, the
    status is never ``adequate`` and research stays needed. ``taxonomy`` compares make and model
    by canonical names (module docstring).
    """
    as_of = _aware(as_of)
    if len(candidates) > MAX_CANDIDATES:
        raise ValidationFailed(f"too many comparable candidates (max {MAX_CANDIDATES})")
    ids = [c.id for c in candidates]
    if len(set(ids)) != len(ids):
        raise ValidationFailed("duplicate market observation ids in comparable candidates")
    criteria = _criteria(config)
    target = target.with_canonical_names(taxonomy)
    warnings = _target_warnings(target)
    currencies = {
        c.amount.currency for c in candidates if c.amount is not None and c.amount.currency != "EUR"
    }
    rates, fx_warnings = _usable_rates(fx_rates, as_of, currencies)
    warnings.extend(fx_warnings)

    kinds = OWNER_ESTIMATE_KINDS if owner_estimates else ADEQUACY_KINDS
    names = _NameResolver(taxonomy)
    ordered = sorted(candidates, key=lambda c: str(c.id))
    evaluations = [
        _evaluate(target, obs, criteria, as_of, rates, kinds=kinds, names=names) for obs in ordered
    ]
    duplicates = _dedupe(evaluations, _strict_eligible)

    # An owner estimate is one reference point per model: admit the closest, never widen for more.
    level, steps = _widen(evaluations, duplicates, criteria, kinds, 1 if owner_estimates else None)

    selected: list[SelectedComparable] = []
    excluded: list[ExcludedComparable] = []
    for ev in evaluations:
        reasons = list(ev.prefilter) + list(ev.hard) + list(ev.widen_failures)
        dup = duplicates.get(ev.obs.id)
        duplicate_of = None if dup is None else dup[0]
        if dup is not None:
            reasons.extend(dup[1])
        if ev.required_level() > level:
            reasons.extend(_unadmitted_reasons(ev, level))
        if reasons:
            notes = list(ev.notes) + _duplicate_notes(ev, dup)
            excluded.append(
                ExcludedComparable(
                    observation_id=ev.obs.id,
                    evidence_kind=ev.obs.evidence_kind,
                    reasons=tuple(sorted(set(reasons), key=_REASON_ORDER.__getitem__)),
                    duplicate_of=duplicate_of,
                    details=tuple(notes[:30]),
                )
            )
            continue
        assert ev.amount_eur is not None  # guaranteed by the prefilter
        widened = tuple(d for d in _WIDEN_ORDER if d in ev.needs)
        differences = tuple(ev.differences) + tuple(ev.needs[d] for d in widened)
        match_level = _match_level(differences)
        selected.append(
            SelectedComparable(
                observation_id=ev.obs.id,
                evidence_kind=ev.obs.evidence_kind,
                weight=MATCH_WEIGHTS[match_level],
                match_level=match_level,
                differences=differences,
                widened_dimensions=widened,
                amount_eur=ev.amount_eur,
                observation=ev.obs,
            )
        )

    selected.sort(key=lambda s: (_kind_order(s.evidence_kind), s.amount_eur, str(s.observation_id)))
    stats = tuple(
        _stats(kind, [s for s in selected if s.evidence_kind == kind], criteria.min_sample)
        for kind in EvidenceKind
        if any(s.evidence_kind == kind for s in selected)
    )
    adequacy = _adequacy_count(selected, kinds)
    status: ComparableStatus
    if adequacy >= criteria.min_sample and not owner_estimates:
        status = "adequate"
    elif adequacy > 0:
        status = "small_sample"
    else:
        status = "insufficient_comparables"
    if any(s.evidence_kind == EvidenceKind.SELLER_REPORTED_SALE for s in selected):
        warnings.append("SELLER_REPORTED_SALES_UNVERIFIED: seller sale claims never make a set adequate")
    if owner_estimates:
        warnings.append(
            "OWNER_ESTIMATES_ONLY: the owner's own estimates, not MK market evidence; research needed"
        )
    asking_amounts = sorted(s.amount_eur for s in selected if s.evidence_kind == EvidenceKind.ASKING_PRICE)
    fit, basis = _band_fit(
        next((s for s in stats if s.evidence_kind == EvidenceKind.ASKING_PRICE), None),
        quantile(asking_amounts, Decimal("0.5")) if asking_amounts else None,
        status,
    )
    return ComparableSetResult(
        as_of=as_of,
        target=target,
        criteria=criteria,
        selected=tuple(selected),
        excluded=tuple(excluded),
        stats=stats,
        status=status,
        widening_steps=tuple(steps),
        mk_band_fit=fit,
        mk_band_fit_basis=basis,
        research_needed=owner_estimates or status != "adequate",
        warnings=tuple(warnings),
    )


class _NameResolver:
    """Canonical (make, model) per raw pair, memoised for one selection (internal)."""

    __slots__ = ("_cache", "_taxonomy")

    def __init__(self, taxonomy: VehicleTaxonomy | None) -> None:
        self._taxonomy = taxonomy
        self._cache: dict[tuple[str | None, str | None], tuple[str | None, str | None]] = {}

    def __call__(self, make: str | None, model: str | None) -> tuple[str | None, str | None]:
        key = (make, model)
        if key not in self._cache:
            self._cache[key] = canonical_names(self._taxonomy, make, model)
        return self._cache[key]


def _criteria(config: ComparableConfig) -> MatchingCriteria:
    year_window = int(config.comparable_year_window)
    mileage_window = to_decimal(config.comparable_mileage_window_km)
    min_sample = int(config.comparable_min_sample)
    max_age = int(config.comparable_max_age_days)
    if year_window < 0 or mileage_window < 0 or min_sample < 1 or max_age < 1:
        raise ValidationFailed("invalid comparable windows")
    return MatchingCriteria(
        year_window=year_window,
        mileage_window_km=mileage_window,
        max_year_window=year_window + YEAR_WIDEN_STEP,
        max_mileage_window_km=mileage_window * MILEAGE_WIDEN_FACTOR,
        max_age_days=max_age,
        min_sample=min_sample,
    )


def _usable_rates(
    fx_rates: Sequence[FxRate], as_of: datetime, currencies: set[str]
) -> tuple[list[FxRate], list[str]]:
    """Pick exactly one FX rate per EUR currency pair, independent of input order (spec 18 FX).

    Only ``reference`` rates support estimation (customs and payment rates are separate) and only
    rates dated on or before ``as_of`` keep the set reproducible. Among those the latest rate date
    wins (then the latest retrieval, provider and rate as deterministic tie-breaks). The rate used
    for every encountered currency is reported, and an old rate is flagged but not discarded.
    """
    best: dict[str, FxRate] = {}
    ignored = 0
    for rate in fx_rates:
        if "EUR" not in (rate.base, rate.quote):
            continue
        other = rate.quote if rate.base == "EUR" else rate.base
        if rate.purpose != FxPurpose.REFERENCE or rate.rate_date > as_of.date():
            ignored += int(other in currencies)
            continue
        current = best.get(other)
        if current is None or _rate_rank(rate) > _rate_rank(current):
            best[other] = rate
    warnings: list[str] = []
    for currency in sorted(currencies & set(best)):
        rate = best[currency]
        age = (as_of.date() - rate.rate_date).days
        warnings.append(
            f"FX_RATE_USED: 1 {rate.base} = {rate.rate} {rate.quote} (reference, dated "
            f"{rate.rate_date.isoformat()}, provider {_bounded(rate.provider, 60)})"
        )
        if age > FX_RATE_WARN_AGE_DAYS:
            warnings.append(f"FX_RATE_OLD: EUR/{currency} rate is {age} days old at as_of")
    if ignored:
        warnings.append(
            f"FX_RATES_IGNORED: {ignored} customs/payment or post-as_of rate(s) not used for estimation"
        )
    return [best[c] for c in sorted(best)], warnings


def _rate_rank(rate: FxRate) -> tuple[Any, ...]:
    return (rate.rate_date, rate.retrieved_at, rate.provider, rate.rate)


def _target_warnings(target: ComparableTarget) -> list[str]:
    warnings: list[str] = []
    if not _norm(target.make) or not _norm(target.model):
        warnings.append("TARGET_MODEL_UNKNOWN: no comparable can be matched without make and model")
    if target.generation is None:
        warnings.append("TARGET_GENERATION_UNKNOWN: selected comparables are labelled generation_unverified")
    for name, value in (("FUEL", target.fuel), ("GEARBOX", target.gearbox), ("DRIVE", target.drive)):
        if value.value == "unknown":
            warnings.append(f"TARGET_{name}_UNKNOWN: candidates cannot be verified on this dimension")
    if target.year is None:
        warnings.append("TARGET_YEAR_UNKNOWN: year window not applied")
    elif target.year_source == "model_year":
        warnings.append("TARGET_YEAR_FROM_MODEL_YEAR: first registration unknown; model year used")
    if target.mileage_km is None:
        warnings.append("TARGET_MILEAGE_UNKNOWN: mileage window not applied")
    if _is_damaged(target.damaged_vehicle, target.running):
        warnings.append(
            "TARGET_DAMAGED_OR_NON_RUNNING: running-vehicle comparables overstate this target's value"
        )
    return warnings


def _evaluate(
    target: ComparableTarget,
    obs: MarketObservation,
    criteria: MatchingCriteria,
    as_of: datetime,
    rates: list[FxRate],
    *,
    kinds: tuple[EvidenceKind, ...],
    names: _NameResolver,
) -> _Evaluation:
    ev = _Evaluation(obs)
    _prefilter(ev, target, criteria, as_of, rates, kinds=kinds)
    _match_identity(ev, target, names)
    _match_engine(ev, target)
    _match_powertrain(ev, target)
    _match_year(ev, target, criteria)
    _match_mileage(ev, target, criteria)
    _match_condition(ev, target)
    _context(ev)
    return ev


def _prefilter(
    ev: _Evaluation,
    target: ComparableTarget,
    criteria: MatchingCriteria,
    as_of: datetime,
    rates: list[FxRate],
    *,
    kinds: tuple[EvidenceKind, ...],
) -> None:
    obs = ev.obs
    if obs.market != "MK":
        ev.prefilter.append(ExclusionReason.WRONG_MARKET)
    if obs.is_fixture != target.is_fixture:
        ev.prefilter.append(ExclusionReason.FIXTURE_MISMATCH)
    # A market set never holds an owner estimate; an owner-estimate set holds nothing else.
    if (obs.evidence_kind == EvidenceKind.OWNER_ESTIMATE) != (kinds == OWNER_ESTIMATE_KINDS):
        ev.prefilter.append(ExclusionReason.NOT_COMPARABLE_EVIDENCE_KIND)
    if obs.amount is None or obs.amount.amount == 0:
        # A zero amount is "price on request" or a parse artefact: unknown, never EUR 0.
        ev.prefilter.append(ExclusionReason.PRICE_MISSING)
    else:
        eur = convert_to_eur(obs.amount, rates)
        if eur is None:
            ev.prefilter.append(ExclusionReason.CURRENCY_UNCONVERTIBLE)
            ev.notes.append(
                f"CURRENCY_UNCONVERTIBLE: no recorded EUR/{obs.amount.currency} reference rate "
                "dated on or before as_of"
            )
        else:
            ev.amount_eur = eur.amount.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_EVEN)
    if obs.observed_at > as_of:
        ev.prefilter.append(ExclusionReason.OBSERVED_AFTER_AS_OF)
    elif as_of - obs.observed_at > timedelta(days=criteria.max_age_days):
        ev.prefilter.append(ExclusionReason.STALE)
        ev.notes.append(
            f"STALE: observed {(as_of - obs.observed_at).days} days before as_of; "
            f"limit {criteria.max_age_days} days (retained historically)"
        )


def _match_identity(ev: _Evaluation, target: ComparableTarget, names: _NameResolver) -> None:
    vehicle = ev.obs.vehicle
    c_make_name, c_model_name = names(vehicle.make, vehicle.model)
    t_make, t_model = _norm(target.make), _norm(target.model)
    c_make, c_model = _norm(c_make_name), _norm(c_model_name)
    if not (t_make and t_model and c_make and c_model):
        ev.reject(ExclusionReason.MODEL_UNKNOWN, "make or model missing on target or comparable")
    elif (t_make, t_model) != (c_make, c_model):
        ev.reject(
            ExclusionReason.WRONG_MODEL,
            f"target {target.make} {target.model}; comparable {vehicle.make} {vehicle.model}",
        )
    t_gen, c_gen = _norm(target.generation), _norm(vehicle.generation)
    if t_gen and c_gen:
        if t_gen != c_gen:
            ev.reject(
                ExclusionReason.WRONG_GENERATION,
                f"target {target.generation}; comparable {vehicle.generation}",
            )
    else:
        ev.differences.append(
            MatchDifference(
                dimension="generation",
                code="generation_unverified",
                severity=DifferenceSeverity.UNVERIFIED,
                target=target.generation,
                comparable=vehicle.generation,
            )
        )
    t_face, c_face = target.facelift, vehicle.facelift
    if Tristate.UNKNOWN in (t_face, c_face):
        ev.differences.append(
            MatchDifference(
                dimension="facelift",
                code="facelift_unverified",
                severity=DifferenceSeverity.UNVERIFIED,
                target=t_face.value,
                comparable=c_face.value,
            )
        )
    elif t_face != c_face:
        ev.needs["facelift"] = MatchDifference(
            dimension="facelift",
            code="facelift_differs_widened",
            severity=DifferenceSeverity.WIDENED,
            target=t_face.value,
            comparable=c_face.value,
        )


def _match_engine(ev: _Evaluation, target: ComparableTarget) -> None:
    vehicle = ev.obs.vehicle
    mismatch = False
    t_code, c_code = _engine_code(target.engine_code), _engine_code(vehicle.engine_code)
    if t_code and c_code:
        if not _codes_compatible(t_code, c_code):
            mismatch = True
        elif t_code != c_code:
            ev.differences.append(
                MatchDifference(
                    dimension="engine",
                    code="engine_code_variant",
                    severity=DifferenceSeverity.DIFFERS,
                    target=target.engine_code,
                    comparable=vehicle.engine_code,
                )
            )
    else:
        # Engine codes are rarely published. When displacement and power are known on both sides
        # they are the documented equivalent check, so a missing code is informational only.
        measured = None not in (
            target.engine_displacement_cm3,
            target.power_kw,
            vehicle.engine_displacement_cm3,
            vehicle.power_kw,
        )
        ev.differences.append(
            MatchDifference(
                dimension="engine",
                code="engine_code_unverified",
                severity=DifferenceSeverity.INFO if measured else DifferenceSeverity.UNVERIFIED,
                target=target.engine_code,
                comparable=vehicle.engine_code,
            )
        )
    for dimension, t_value, c_value, tolerance, unit in (
        (
            "displacement",
            target.engine_displacement_cm3,
            vehicle.engine_displacement_cm3,
            DISPLACEMENT_TOLERANCE,
            "cm3",
        ),
        ("power", target.power_kw, vehicle.power_kw, POWER_TOLERANCE, "kW"),
    ):
        if t_value is None or c_value is None:
            ev.differences.append(
                _unverified(
                    "engine",
                    f"{dimension}_unverified",
                    None if t_value is None else f"{t_value} {unit}",
                    None if c_value is None else f"{c_value} {unit}",
                )
            )
            continue
        if abs(Decimal(t_value) - Decimal(c_value)) > Decimal(t_value) * tolerance:
            mismatch = True
        elif t_value != c_value:
            ev.differences.append(
                MatchDifference(
                    dimension="engine",
                    code=f"{dimension}_differs_within_tolerance",
                    severity=DifferenceSeverity.DIFFERS,
                    target=f"{t_value} {unit}",
                    comparable=f"{c_value} {unit}",
                )
            )
    if mismatch:
        ev.reject(
            ExclusionReason.ENGINE_MISMATCH,
            f"target {_engine_text(target.engine_code, target.engine_displacement_cm3, target.power_kw)}; "
            "comparable "
            f"{_engine_text(vehicle.engine_code, vehicle.engine_displacement_cm3, vehicle.power_kw)}",
        )


def _match_powertrain(ev: _Evaluation, target: ComparableTarget) -> None:
    vehicle = ev.obs.vehicle
    if Fuel.UNKNOWN in (target.fuel, vehicle.fuel):
        ev.reject(
            ExclusionReason.FUEL_UNVERIFIED, f"target {target.fuel.value}; comparable {vehicle.fuel.value}"
        )
    elif target.fuel != vehicle.fuel:
        ev.reject(
            ExclusionReason.FUEL_MISMATCH, f"target {target.fuel.value}; comparable {vehicle.fuel.value}"
        )
    if Gearbox.UNKNOWN in (target.gearbox, vehicle.gearbox):
        ev.reject(
            ExclusionReason.GEARBOX_UNVERIFIED,
            f"target {target.gearbox.value}; comparable {vehicle.gearbox.value}",
        )
    elif target.gearbox != vehicle.gearbox:
        ev.reject(
            ExclusionReason.GEARBOX_MISMATCH,
            f"target {target.gearbox.value}; comparable {vehicle.gearbox.value}",
        )
    else:
        t_sub, c_sub = _norm(target.gearbox_subtype), _norm(vehicle.gearbox_subtype)
        if t_sub and c_sub and t_sub != c_sub:
            ev.differences.append(
                MatchDifference(
                    dimension="gearbox",
                    code="gearbox_subtype_differs",
                    severity=DifferenceSeverity.DIFFERS,
                    target=target.gearbox_subtype,
                    comparable=vehicle.gearbox_subtype,
                )
            )
    if Drive.UNKNOWN in (target.drive, vehicle.drive):
        ev.reject(
            ExclusionReason.DRIVE_UNVERIFIED, f"target {target.drive.value}; comparable {vehicle.drive.value}"
        )
    elif _drive_class(target.drive) != _drive_class(vehicle.drive):
        ev.reject(
            ExclusionReason.DRIVE_MISMATCH,
            f"2WD versus AWD/4WD: target {target.drive.value}; comparable {vehicle.drive.value}",
        )
    elif target.drive != vehicle.drive:
        ev.differences.append(
            MatchDifference(
                dimension="drive",
                code="drive_variant_differs",
                severity=DifferenceSeverity.DIFFERS,
                target=target.drive.value,
                comparable=vehicle.drive.value,
            )
        )


def _match_year(ev: _Evaluation, target: ComparableTarget, criteria: MatchingCriteria) -> None:
    c_year, c_source = _vehicle_year(ev.obs.vehicle)
    if target.year is None:
        ev.differences.append(
            _unverified("year", "target_year_unknown", None, None if c_year is None else str(c_year))
        )
        return
    if c_year is None:
        ev.reject(ExclusionReason.YEAR_UNKNOWN, "comparable registration/model year unknown")
        return
    delta = c_year - target.year
    if c_source != target.year_source:
        ev.differences.append(
            MatchDifference(
                dimension="year",
                code="year_basis_differs",
                severity=DifferenceSeverity.DIFFERS,
                target=target.year_source,
                comparable=c_source,
            )
        )
    if abs(delta) <= criteria.year_window:
        if delta:
            ev.differences.append(
                MatchDifference(
                    dimension="year",
                    code="year_within_window",
                    severity=DifferenceSeverity.INFO,
                    target=str(target.year),
                    comparable=str(c_year),
                )
            )
    elif abs(delta) <= criteria.max_year_window:
        ev.needs["year"] = MatchDifference(
            dimension="year",
            code=f"year_window_widened_to_{criteria.max_year_window}",
            severity=DifferenceSeverity.WIDENED,
            target=str(target.year),
            comparable=str(c_year),
        )
    else:
        ev.widen_failures.append(ExclusionReason.YEAR_OUT_OF_WINDOW)
        ev.notes.append(
            f"YEAR_OUT_OF_WINDOW: target {target.year}; comparable {c_year}; "
            f"widest window +/-{criteria.max_year_window}"
        )


def _match_mileage(ev: _Evaluation, target: ComparableTarget, criteria: MatchingCriteria) -> None:
    c_km = ev.obs.vehicle.mileage_km
    if target.mileage_km is None:
        ev.differences.append(
            _unverified("mileage", "target_mileage_unknown", None, None if c_km is None else _km(c_km))
        )
        return
    if c_km is None:
        ev.reject(ExclusionReason.MILEAGE_UNKNOWN, "comparable mileage unknown")
        return
    delta = abs(c_km - target.mileage_km)
    if delta <= criteria.mileage_window_km:
        if delta:
            ev.differences.append(
                MatchDifference(
                    dimension="mileage",
                    code="mileage_within_window",
                    severity=DifferenceSeverity.INFO,
                    target=_km(target.mileage_km),
                    comparable=_km(c_km),
                )
            )
    elif delta <= criteria.max_mileage_window_km:
        ev.needs["mileage"] = MatchDifference(
            dimension="mileage",
            code=f"mileage_window_widened_to_{_km(criteria.max_mileage_window_km)}",
            severity=DifferenceSeverity.WIDENED,
            target=_km(target.mileage_km),
            comparable=_km(c_km),
        )
    else:
        ev.widen_failures.append(ExclusionReason.MILEAGE_OUT_OF_WINDOW)
        ev.notes.append(
            f"MILEAGE_OUT_OF_WINDOW: target {_km(target.mileage_km)} km; comparable {_km(c_km)} km; "
            f"widest window +/-{_km(criteria.max_mileage_window_km)} km"
        )


def _match_condition(ev: _Evaluation, target: ComparableTarget) -> None:
    """Spec 15 dimension 8 (condition and running status).

    A damaged or non-running comparable never values a running target (excluded). The reverse is
    kept but labelled ``target_damaged_comparable_running`` (a ``close`` match): running-vehicle
    asking prices overstate a damaged target, and that must stay visible next to the number.
    """
    cond = ev.obs.condition
    cand_damaged = _is_damaged(cond.damaged_vehicle, cond.running)
    target_damaged = _is_damaged(target.damaged_vehicle, target.running)
    if cand_damaged and not target_damaged:
        ev.reject(ExclusionReason.CONDITION_MISMATCH, "comparable is damaged or non-running; target is not")
    elif target_damaged and not cand_damaged:
        ev.differences.append(
            MatchDifference(
                dimension="condition",
                code="target_damaged_comparable_running",
                severity=DifferenceSeverity.DIFFERS,
                target="damaged_or_non_running",
                comparable="not_reported_damaged",
            )
        )


def _is_damaged(damaged_vehicle: ClaimStatus, running: ClaimStatus) -> bool:
    return damaged_vehicle in (ClaimStatus.SELLER_CLAIMED, ClaimStatus.VERIFIED) or (
        running == ClaimStatus.SELLER_DENIED
    )


def _context(ev: _Evaluation) -> None:
    obs = ev.obs

    def add(dimension: str, code: str, value: str | None = None) -> None:
        ev.differences.append(
            MatchDifference(
                dimension=dimension, code=code, severity=DifferenceSeverity.CONTEXT, comparable=value
            )
        )

    if obs.price_basis != PriceBasis.GROSS:
        add("price", f"price_basis_{obs.price_basis.value}")
    if obs.local_registration_status != "locally_registered":
        add("registration", f"registration_{obs.local_registration_status}")
    if obs.seller_type == SellerType.DEALER:
        add("seller", "dealer_seller")
    elif obs.seller_type == SellerType.UNKNOWN:
        add("seller", "seller_type_unknown")
    if obs.warranty == Tristate.YES:
        add("warranty", "warranty_included")
    if obs.condition.accident_free == ClaimStatus.SELLER_DENIED:
        add("condition", "accident_reported")
    elif obs.condition.accident_free == ClaimStatus.UNKNOWN:
        add("condition", "accident_history_unknown")
    if obs.condition.documented_faults:
        add("condition", "faults_reported", str(len(obs.condition.documented_faults)))
    if obs.availability == Availability.REMOVED:
        add("availability", "listing_removed_asking_only")
    elif obs.availability == Availability.SOLD_CLAIMED:
        add("availability", "sold_claimed_asking_only")
    elif obs.availability == Availability.RESERVED:
        add("availability", "reserved")


#: ``_dedupe`` result: {duplicate id: (kept id, duplicate reasons)}.
_Duplicates = dict[UUID, tuple[UUID, tuple[ExclusionReason, ...]]]


def _dedupe(evaluations: list[_Evaluation], eligible: Callable[[_Evaluation], bool]) -> _Duplicates:
    """Duplicates per evidence kind, by vehicle cluster and by ad URL (module docstring).

    Only valid evidence (no prefilter reason) is deduplicated, so a newer row without a price
    never hides a valid older one. Rows linked by a shared cluster or a shared ``ad_url_key`` of
    the same kind form one group, transitively (union-find): A (url u, cluster c), B (url u) and
    C (cluster c) are one ad. The kept row is the newest member that is ``eligible`` in the
    caller's mode (no hard reason it cannot shed: a newer re-record that lost its mileage never
    hides the complete older one), preferring one that also passes the strict checks (so the
    orientation keeps the same row of an ad as the strict set whenever the strict set can use
    it); only when no member is eligible the newest one is kept. Equal times keep the
    lexicographically smallest id. Every other member records
    ``DUPLICATE_OF_CLUSTER`` when it shares a cluster with another member and
    ``DUPLICATE_OF_URL`` when it shares an ad URL.
    """
    valid = [ev for ev in evaluations if not ev.prefilter]
    parent: dict[UUID, UUID] = {ev.obs.id: ev.obs.id for ev in valid}

    def find(item: UUID) -> UUID:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    clusters: dict[tuple[UUID, EvidenceKind], list[UUID]] = {}
    urls: dict[tuple[str, EvidenceKind], list[UUID]] = {}
    for ev in valid:
        if ev.obs.cluster_id is not None:
            clusters.setdefault((ev.obs.cluster_id, ev.obs.evidence_kind), []).append(ev.obs.id)
        key = ad_url_key(ev.obs.url)
        if key is not None:
            urls.setdefault((key, ev.obs.evidence_kind), []).append(ev.obs.id)
    shared_cluster: set[UUID] = set()
    shared_url: set[UUID] = set()
    for groups, shared in ((clusters, shared_cluster), (urls, shared_url)):
        for linked in groups.values():
            if len(linked) < 2:
                continue
            shared.update(linked)
            for other_id in linked[1:]:
                a, b = find(linked[0]), find(other_id)
                if a != b:
                    low, high = sorted((a, b), key=str)
                    parent[high] = low
    components: dict[UUID, list[_Evaluation]] = {}
    for ev in valid:
        components.setdefault(find(ev.obs.id), []).append(ev)
    duplicates: _Duplicates = {}
    for members in components.values():
        if len(members) < 2:
            continue
        # The newest eligible observation wins (else the newest); equal times fall back to the
        # lexicographically smallest id (stable sort).
        by_id = sorted(members, key=lambda e: str(e.obs.id))
        ranked = sorted(
            by_id, key=lambda e: (eligible(e), _strict_eligible(e), e.obs.observed_at), reverse=True
        )
        keep = ranked[0].obs.id
        for other in ranked[1:]:
            oid = other.obs.id
            reasons = tuple(
                reason
                for reason, shared in (
                    (ExclusionReason.DUPLICATE_OF_CLUSTER, shared_cluster),
                    (ExclusionReason.DUPLICATE_OF_URL, shared_url),
                )
                if oid in shared
            )
            duplicates[oid] = (keep, reasons)
    return duplicates


def _strict_eligible(ev: _Evaluation) -> bool:
    """A row the strict set could select at some widening level (``select_comparables``)."""
    return not (ev.hard or ev.widen_failures)


def _orientation_eligible(ev: _Evaluation) -> bool:
    """A row an orientation could use (``orient_comparables``: only ``ORIENTATION_HARD`` is hard)."""
    return not any(r in ORIENTATION_HARD for r in ev.hard)


def _duplicate_notes(ev: _Evaluation, dup: tuple[UUID, tuple[ExclusionReason, ...]] | None) -> list[str]:
    """Exclusion details of a duplicate (the ad URL itself is never echoed)."""
    if dup is None:
        return []
    kept, reasons = dup
    notes: list[str] = []
    if ExclusionReason.DUPLICATE_OF_CLUSTER in reasons:
        notes.append(f"DUPLICATE_OF_CLUSTER: cluster {ev.obs.cluster_id}; kept {kept}")
    if ExclusionReason.DUPLICATE_OF_URL in reasons:
        notes.append(f"DUPLICATE_OF_URL: same ad url re-recorded; kept {kept}")
    return notes


def _widen(
    evaluations: list[_Evaluation],
    duplicates: _Duplicates,
    criteria: MatchingCriteria,
    kinds: tuple[EvidenceKind, ...],
    required: int | None,
) -> tuple[int, list[WideningStep]]:
    """Apply widening steps one dimension at a time while the count of ``kinds`` is below
    ``required`` (default ``min_sample``)."""
    needed = criteria.min_sample if required is None else required
    eligible = [
        ev
        for ev in evaluations
        if not (ev.prefilter or ev.hard or ev.widen_failures) and ev.obs.id not in duplicates
    ]

    def adequacy_at(level: int) -> tuple[int, int]:
        admitted = [ev for ev in eligible if ev.required_level() <= level]
        counts = {kind: sum(1 for ev in admitted if ev.obs.evidence_kind == kind) for kind in kinds}
        return len(admitted), max(counts.values())

    level = 0
    steps: list[WideningStep] = []
    _, adequacy = adequacy_at(level)
    max_level = len(_WIDEN_ORDER)
    while adequacy < needed and level < max_level:
        if not any(ev.required_level() > level for ev in eligible):
            break  # nothing further could be admitted; do not record empty widening
        level += 1
        dimension = _WIDEN_ORDER[level - 1]
        selected_count, adequacy = adequacy_at(level)
        steps.append(
            WideningStep(
                step=level,
                dimension=dimension,
                label=_widen_label(dimension, criteria),
                selected_count_after=selected_count,
                adequacy_count_after=adequacy,
            )
        )
    return level, steps


def _widen_label(dimension: WidenDimension, criteria: MatchingCriteria) -> str:
    if dimension == "mileage":
        return (
            f"mileage window widened from +/-{_km(criteria.mileage_window_km)} km "
            f"to +/-{_km(criteria.max_mileage_window_km)} km"
        )
    if dimension == "year":
        return (
            f"registration-year window widened from +/-{criteria.year_window} "
            f"to +/-{criteria.max_year_window}"
        )
    return "facelift difference admitted (pre/post facelift of the same generation)"


def _unadmitted_reasons(ev: _Evaluation, level: int) -> list[ExclusionReason]:
    mapping: dict[WidenDimension, ExclusionReason] = {
        "mileage": ExclusionReason.MILEAGE_OUT_OF_WINDOW,
        "year": ExclusionReason.YEAR_OUT_OF_WINDOW,
        "facelift": ExclusionReason.FACELIFT_MISMATCH,
    }
    return [mapping[d] for d in ev.needs if _WIDEN_ORDER.index(d) + 1 > level]


def _match_level(differences: Iterable[MatchDifference]) -> MatchLevel:
    level: MatchLevel = "exact"
    for diff in differences:
        candidate: MatchLevel
        if diff.severity == DifferenceSeverity.WIDENED:
            candidate = "widened"
        elif diff.severity in (DifferenceSeverity.UNVERIFIED, DifferenceSeverity.DIFFERS):
            candidate = "close"
        else:
            continue
        if _LEVEL_ORDER[candidate] > _LEVEL_ORDER[level]:
            level = candidate
    return level


def _adequacy_count(
    selected: Iterable[SelectedComparable], kinds: tuple[EvidenceKind, ...] = ADEQUACY_KINDS
) -> int:
    items = list(selected)
    return max(sum(1 for s in items if s.evidence_kind == kind) for kind in kinds)


def _kind_order(kind: EvidenceKind) -> int:
    return list(EvidenceKind).index(kind)


# ---------------------------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------------------------


def _stats(kind: EvidenceKind, items: list[SelectedComparable], min_sample: int) -> EvidenceStats:
    values = sorted(s.amount_eur for s in items)
    n = len(values)
    label: SampleLabel
    if kind == EvidenceKind.OWNER_ESTIMATE:
        label = "single_observation" if n == 1 else "small_sample"  # an assumption is never adequate
    elif kind not in ADEQUACY_KINDS:
        label = "unverified_claims"  # seller sale claims never become an adequate sample
    elif n == 1:
        label = "single_observation"
    else:
        label = "adequate" if n >= min_sample else "small_sample"
    quality = {lvl: sum(1 for s in items if s.match_level == lvl) for lvl in MATCH_WEIGHTS}
    times = [s.observation.observed_at for s in items]
    return EvidenceStats(
        evidence_kind=kind,
        evidence_note=_EVIDENCE_NOTES[kind],
        n=n,
        sample_label=label,
        min=_cents(values[0]),
        max=_cents(values[-1]),
        median=_cents(quantile(values, Decimal("0.5"))),
        q1=_cents(quantile(values, Decimal("0.25"))) if n >= QUARTILE_MIN_SAMPLE else None,
        q3=_cents(quantile(values, Decimal("0.75"))) if n >= QUARTILE_MIN_SAMPLE else None,
        date_from=min(times),
        date_to=max(times),
        match_quality=quality,
        points=tuple(
            StatPoint(
                observation_id=s.observation_id,
                amount_eur=s.amount_eur,
                match_level=s.match_level,
                observed_at=s.observation.observed_at,
                difference_codes=tuple(d.code for d in s.differences),
            )
            for s in sorted(items, key=lambda s: (s.amount_eur, str(s.observation_id)))
        ),
    )


def quantile(sorted_values: Sequence[Decimal], p: Decimal) -> Decimal:
    """Inclusive linear-interpolation quantile (Excel QUARTILE.INC / numpy 'linear'), exact Decimal.

    ``p`` in [0, 1]; the median (p = 0.5) of an even-sized sample is the mean of the middle pair.
    """
    if not sorted_values:
        raise ValidationFailed("quantile of an empty sample")
    if not Decimal(0) <= p <= Decimal(1):
        raise ValidationFailed("quantile p must be within [0, 1]")
    position = (len(sorted_values) - 1) * p
    lower = int(position)  # floor for non-negative values
    fraction = position - lower
    if lower + 1 >= len(sorted_values) or fraction == 0:
        return sorted_values[lower]
    return sorted_values[lower] + (sorted_values[lower + 1] - sorted_values[lower]) * fraction


def _band_fit(
    asking: EvidenceStats | None, unrounded_median: Decimal | None, status: ComparableStatus
) -> tuple[BandFit, str]:
    """Band fit of the MK asking median versus EUR 8,000-10,000 (inclusive at both ends).

    The comparison uses the *unrounded* median (spec 3: compare the unrounded Decimal EUR value
    before rounding for display), so a median of 7,999.995 is ``below`` even though it displays as
    8,000.00.
    """
    if asking is None or unrounded_median is None:
        return "unknown", "no selected MK asking-price comparables"
    fit: BandFit
    if unrounded_median < MK_ASKING_BAND_MIN_EUR:
        fit = "below"
    elif unrounded_median > MK_ASKING_BAND_MAX_EUR:
        fit = "above"
    else:
        fit = "within"
    quality = "adequate sample" if status == "adequate" else f"{asking.sample_label.replace('_', ' ')}"
    basis = (
        f"median asking price EUR {asking.median} of {asking.n} MK asking-price comparable(s) ({quality}) "
        f"versus the EUR {MK_ASKING_BAND_MIN_EUR}-{MK_ASKING_BAND_MAX_EUR} asking-price research band; "
        "asking prices are not realized sales"
    )
    return fit, basis


# ---------------------------------------------------------------------------------------------
# Proceeds
# ---------------------------------------------------------------------------------------------

ProceedsBasis = Literal["mk_asking_prices", "verified_sales", "owner_estimate"]
DISCOUNT_UNKNOWN_LABEL: Final = "asking-based; discount unknown"


class SensitivityPoint(BaseModel):
    """Hypothetical base proceeds under a caller-supplied discount; never an approved assumption."""

    model_config = _FROZEN

    discount_pct: Decimal
    base_after_discount: Money
    label: str = "hypothetical sensitivity; not an approved assumption"


class ComparableProceeds(BaseModel):
    """Proceeds data derived from a comparable set, shaped for ``costs.ProceedsEstimate``.

    ``low``/``base``/``high`` are the *undiscounted* evidence amounts. The scenario engine
    applies ``negotiation_discount_pct`` exactly once; applying it here as well would double
    count. ``labels`` state what the numbers are (asking-based, discount unknown, small sample).
    """

    model_config = _FROZEN

    status: CostLineStatus
    currency: Literal["EUR"] = "EUR"
    low: Money | None
    base: Money | None
    high: Money | None
    basis: ProceedsBasis
    evidence_kind: EvidenceKind | None
    sample_size: int = Field(ge=0)
    negotiation_discount_pct: Decimal | None
    discount_approved: bool = False
    evidence_ids: tuple[str, ...]
    comparable_set_id: str | None = None
    labels: tuple[str, ...]
    research_needed: bool
    sensitivity: tuple[SensitivityPoint, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> ComparableProceeds:
        amounts = (self.low, self.base, self.high)
        if self.status == CostLineStatus.UNKNOWN and any(a is not None for a in amounts):
            raise ValueError("unknown proceeds carry no amounts")
        if self.status != CostLineStatus.UNKNOWN and self.base is None:
            raise ValueError("known proceeds need a base amount")
        return self

    def as_proceeds_estimate_kwargs(self) -> dict[str, Any]:
        """Keyword arguments accepted by ``suv_deals.domain.costs.ProceedsEstimate``."""
        return {
            "status": self.status,
            "currency": self.currency,
            "low": self.low,
            "base": self.base,
            "high": self.high,
            "basis": self.basis,
            "evidence_kind": self.evidence_kind,
            "sample_size": self.sample_size,
            "negotiation_discount_pct": self.negotiation_discount_pct,
            "discount_approved": self.discount_approved,
            "evidence_ids": self.evidence_ids[:200],
            "comparable_set_id": self.comparable_set_id,
        }


def proceeds_from_comparables(
    result: ComparableSetResult,
    negotiation_discount_pct: Decimal | None,
    *,
    discount_approved: bool = False,
    comparable_set_id: UUID | None = None,
    sensitivity_discounts_pct: Sequence[Decimal] = (),
) -> ComparableProceeds:
    """Turn a comparable set into labelled low/base/high proceeds evidence (spec 15, 18).

    - An adequate verified-sale sample is used directly (no negotiation discount applies).
    - Otherwise selected MK asking prices are used: base = median; low/high = quartiles when
      n >= 5, else min/max. Labelled ``asking-based; discount unknown`` while no discount is
      supplied; a supplied discount is labelled as an (un)approved assumption.
    - No asking/verified evidence -> ``unknown`` proceeds, ``insufficient_comparables`` label and
      ``research_needed``. Seller-reported sale claims never become proceeds.
    - ``sensitivity_discounts_pct`` are caller-chosen hypothetical discounts, reported separately.
    """
    discount = _discount(negotiation_discount_pct, "negotiation_discount_pct")
    if discount_approved and discount is None:
        raise ValidationFailed("an approved discount needs a value")
    hypotheticals = tuple(_pct(d, "sensitivity_discounts_pct") for d in sensitivity_discounts_pct)
    set_id = None if comparable_set_id is None else str(comparable_set_id)
    min_sample = result.criteria.min_sample

    verified = result.stats_for(EvidenceKind.VERIFIED_SALE)
    if verified is not None and verified.n >= min_sample:
        labels = ["verified-sale-based (realized proceeds evidence)"]
        if discount is not None:
            labels.append("negotiation discount not applied to verified sales")
        low, high = _range(verified)
        return ComparableProceeds(
            status=CostLineStatus.ESTIMATED,
            low=low,
            base=Money.of(verified.median, "EUR"),
            high=high,
            basis="verified_sales",
            evidence_kind=EvidenceKind.VERIFIED_SALE,
            sample_size=verified.n,
            negotiation_discount_pct=None,
            evidence_ids=tuple(str(p.observation_id) for p in verified.points),
            comparable_set_id=set_id,
            labels=tuple(labels),
            research_needed=result.research_needed,
        )

    asking = result.stats_for(EvidenceKind.ASKING_PRICE)
    if verified is not None and asking is None:
        low, high = _range(verified)
        return ComparableProceeds(
            status=CostLineStatus.ESTIMATED,
            low=low,
            base=Money.of(verified.median, "EUR"),
            high=high,
            basis="verified_sales",
            evidence_kind=EvidenceKind.VERIFIED_SALE,
            sample_size=verified.n,
            negotiation_discount_pct=None,
            evidence_ids=tuple(str(p.observation_id) for p in verified.points),
            comparable_set_id=set_id,
            labels=(
                "verified-sale-based (realized proceeds evidence)",
                f"small sample (n={verified.n}); research needed",
            ),
            research_needed=True,
        )
    if asking is None:
        return ComparableProceeds(
            status=CostLineStatus.UNKNOWN,
            low=None,
            base=None,
            high=None,
            basis="mk_asking_prices",
            evidence_kind=None,
            sample_size=0,
            negotiation_discount_pct=discount,
            discount_approved=discount_approved,
            evidence_ids=(),
            comparable_set_id=set_id,
            labels=("insufficient_comparables", "research needed; no MK comparable proceeds evidence"),
            research_needed=True,
        )
    labels = ["asking-based"]
    if discount is None:
        labels = [DISCOUNT_UNKNOWN_LABEL]
    else:
        state = "approved" if discount_approved else "unapproved assumption"
        labels.append(f"less {discount}% negotiation discount ({state}) applied by the scenario engine")
    if asking.n < min_sample:
        labels.append(f"small sample (n={asking.n}); research needed")
    if asking.n < QUARTILE_MIN_SAMPLE:
        labels.append("low/high are min/max of the sample (quartiles need n >= 5)")
    else:
        labels.append("low/high are the first/third quartiles")
    low, high = _range(asking)
    base = Money.of(asking.median, "EUR")
    sensitivity = tuple(
        SensitivityPoint(discount_pct=d, base_after_discount=_discounted(base, d)) for d in hypotheticals
    )
    return ComparableProceeds(
        status=CostLineStatus.ESTIMATED,
        low=low,
        base=base,
        high=high,
        basis="mk_asking_prices",
        evidence_kind=EvidenceKind.ASKING_PRICE,
        sample_size=asking.n,
        negotiation_discount_pct=discount,
        discount_approved=discount_approved,
        evidence_ids=tuple(str(p.observation_id) for p in asking.points),
        comparable_set_id=set_id,
        labels=tuple(labels),
        research_needed=result.research_needed,
        sensitivity=sensitivity,
    )


def proceeds_from_owner_estimates(result: ComparableSetResult) -> ComparableProceeds:
    """Labelled proceeds from an owner-estimate selection (``select_comparables(owner_estimates=True)``).

    The orientation used only while a target has no MK market evidence at all: base = median of
    the matched estimates, low/high = min/max (quartiles from n >= 5). No negotiation discount
    applies (an estimate is not an asking price), it is never an approved assumption and research
    stays needed. No matched estimate -> ``unknown``. A market selection never holds estimates,
    so it also returns ``unknown``.
    """
    estimate = result.stats_for(EvidenceKind.OWNER_ESTIMATE)
    if estimate is None:
        return ComparableProceeds(
            status=CostLineStatus.UNKNOWN,
            low=None,
            base=None,
            high=None,
            basis="owner_estimate",
            evidence_kind=None,
            sample_size=0,
            negotiation_discount_pct=None,
            evidence_ids=(),
            labels=("no matching owner estimate", "research needed; no MK comparable proceeds evidence"),
            research_needed=True,
        )
    low, high = _range(estimate)
    spread = (
        "low/high are the first/third quartiles"
        if estimate.n >= QUARTILE_MIN_SAMPLE
        else "low/high are min/max of the matched estimates"
    )
    return ComparableProceeds(
        status=CostLineStatus.ESTIMATED,
        low=low,
        base=Money.of(estimate.median, "EUR"),
        high=high,
        basis="owner_estimate",
        evidence_kind=EvidenceKind.OWNER_ESTIMATE,
        sample_size=estimate.n,
        negotiation_discount_pct=None,
        evidence_ids=tuple(str(p.observation_id) for p in estimate.points),
        labels=(OWNER_ESTIMATE_LABEL, f"{estimate.n} matched estimate(s); {spread}", "research needed"),
        research_needed=True,
    )


def _range(stats: EvidenceStats) -> tuple[Money, Money]:
    if stats.q1 is not None and stats.q3 is not None:
        return Money.of(stats.q1, "EUR"), Money.of(stats.q3, "EUR")
    return Money.of(stats.min, "EUR"), Money.of(stats.max, "EUR")


def _discount(value: Decimal | None, name: str) -> Decimal | None:
    return None if value is None else _pct(value, name)


def _pct(value: Decimal, name: str) -> Decimal:
    if isinstance(value, float):  # defensive: typing forbids it
        raise ValidationFailed(f"{name} must be a Decimal, not a float")
    pct = to_decimal(value)
    if not Decimal(0) <= pct < Decimal(100):
        raise ValidationFailed(f"{name} must be within [0, 100)")
    return pct


def _discounted(money: Money, pct: Decimal) -> Money:
    factor = Decimal(1) - pct / Decimal(100)
    return Money.of(money.times(factor).amount.quantize(_CENT, rounding=ROUND_HALF_EVEN), money.currency)


# ---------------------------------------------------------------------------------------------
# MK price orientation (owner decision 2026-10-16, ADR 0005)
# ---------------------------------------------------------------------------------------------

ORIENTATION_VERSION: Final = "mk-orientation@1.0.0"
#: The nearest points an orientation keeps (PROPOSED, not statistically validated).
ORIENTATION_LIMIT: Final = 5
ORIENTATION_MAX_LIMIT: Final = 50
#: Distance up to which an asking-price orientation counts as close: the valuation pipeline
#: prefers it to the owner's estimates only up to here (PROPOSED, not statistically validated).
#: The closeness test uses the VERIFIED distance (``OrientationResult.nearest_verified_distance``:
#: an unknown fuel, gearbox or drive counted as a mismatch), so an ad that may be a petrol is
#: never close to a diesel and an unknown never looks closer than a known near-variant.
ORIENTATION_CLOSE: Final = Decimal(4)
#: Continuous distance terms (PROPOSED): 1 point per registration year of difference and per
#: 30,000 km of mileage difference, so a candidate inside the default tight windows (+/-1 year,
#: +/-30,000 km) scores at most 2 from year and mileage.
ORIENTATION_YEAR_POINTS: Final = Decimal(1)
ORIENTATION_MILEAGE_UNIT_KM: Final = Decimal(30000)
#: A known pre/post facelift difference of the same generation (PROPOSED).
ORIENTATION_FACELIFT_POINTS: Final = Decimal(1)
#: Distance penalty per strict-matching reason that orientation turns into a labelled difference
#: (PROPOSED, not statistically validated). A different fuel scores above ``ORIENTATION_CLOSE``
#: on its own, so a diesel is never "close" to a petrol. Unknowns score 1 in the ranking: they
#: cannot be shown to differ. A target-side unknown adds the same amount to every candidate (no
#: ranking effect). The closeness test counts every unknown fuel, gearbox or drive (either side)
#: as its mismatch instead (``OrientationResult.nearest_verified_distance``).
#: Unknowns strict matching only labels (generation, facelift, engine code/values) score 0.
ORIENTATION_PENALTIES: Final[dict[ExclusionReason, Decimal]] = {
    ExclusionReason.FUEL_MISMATCH: Decimal(5),
    ExclusionReason.WRONG_GENERATION: Decimal(3),
    ExclusionReason.ENGINE_MISMATCH: Decimal(2),
    ExclusionReason.GEARBOX_MISMATCH: Decimal(2),
    ExclusionReason.DRIVE_MISMATCH: Decimal(2),
    ExclusionReason.FUEL_UNVERIFIED: Decimal(1),
    ExclusionReason.GEARBOX_UNVERIFIED: Decimal(1),
    ExclusionReason.DRIVE_UNVERIFIED: Decimal(1),
    ExclusionReason.YEAR_UNKNOWN: Decimal(1),
    ExclusionReason.MILEAGE_UNKNOWN: Decimal(1),
}
#: Strict reasons that stay hard in an orientation (besides every prefilter and duplicate reason).
ORIENTATION_HARD: Final = frozenset(
    {ExclusionReason.MODEL_UNKNOWN, ExclusionReason.WRONG_MODEL, ExclusionReason.CONDITION_MISMATCH}
)
ORIENTATION_LABEL: Final = (
    "MK orientation (relaxed same-model framework over the nearest MK points; not a spec-15 comparable set)"
)

_ORIENTATION_DIMENSION: Final[dict[ExclusionReason, str]] = {
    ExclusionReason.FUEL_MISMATCH: "fuel",
    ExclusionReason.FUEL_UNVERIFIED: "fuel",
    ExclusionReason.WRONG_GENERATION: "generation",
    ExclusionReason.ENGINE_MISMATCH: "engine",
    ExclusionReason.GEARBOX_MISMATCH: "gearbox",
    ExclusionReason.GEARBOX_UNVERIFIED: "gearbox",
    ExclusionReason.DRIVE_MISMATCH: "drive",
    ExclusionReason.DRIVE_UNVERIFIED: "drive",
    ExclusionReason.YEAR_UNKNOWN: "year",
    ExclusionReason.MILEAGE_UNKNOWN: "mileage",
}
_ORIENTATION_UNKNOWN: Final = frozenset(
    {
        ExclusionReason.FUEL_UNVERIFIED,
        ExclusionReason.GEARBOX_UNVERIFIED,
        ExclusionReason.DRIVE_UNVERIFIED,
        ExclusionReason.YEAR_UNKNOWN,
        ExclusionReason.MILEAGE_UNKNOWN,
    }
)
#: The closeness test counts an unknown powertrain dimension as the mismatch it may hide.
_ORIENTATION_WORST_CASE: Final[dict[ExclusionReason, ExclusionReason]] = {
    ExclusionReason.FUEL_UNVERIFIED: ExclusionReason.FUEL_MISMATCH,
    ExclusionReason.GEARBOX_UNVERIFIED: ExclusionReason.GEARBOX_MISMATCH,
    ExclusionReason.DRIVE_UNVERIFIED: ExclusionReason.DRIVE_MISMATCH,
}
_ORIENTATION_PREFIX: Final = "orientation_"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class OrientationPoint(BaseModel):
    """One MK point of an orientation: its distance and every labelled difference."""

    model_config = _FROZEN

    rank: int = Field(ge=1)
    observation_id: UUID
    evidence_kind: EvidenceKind
    amount_eur: Decimal  # EUR equivalent, 6 decimal places (as in a comparable set)
    distance: Decimal = Field(ge=0)  # 6 decimal places; 0 = no penalised difference
    differences: tuple[MatchDifference, ...] = Field(max_length=50)
    observation: MarketObservation


class OrientationResult(BaseModel):
    """A relaxed same-model MK orientation (``orient_comparables``). Never persisted as a set."""

    model_config = _FROZEN

    version: str = ORIENTATION_VERSION
    as_of: datetime
    target: ComparableTarget
    evidence_kind: EvidenceKind
    limit: int = Field(ge=1, le=ORIENTATION_MAX_LIMIT)
    max_age_days: int = Field(ge=1)
    eligible_count: int = Field(ge=0)
    points: tuple[OrientationPoint, ...]  # nearest first, at most ``limit``
    excluded: tuple[ExcludedComparable, ...]
    #: The smallest distance of any eligible point with every unknown fuel, gearbox or drive
    #: (on either side) counted as the mismatch it may hide (``None`` without points). Only the
    #: closeness test uses it; the ranking uses ``distance``.
    nearest_verified_distance: Decimal | None = None
    warnings: tuple[str, ...] = ()

    @property
    def nearest_distance(self) -> Decimal | None:
        return self.points[0].distance if self.points else None

    @property
    def close(self) -> bool:
        """Some eligible point is verified within ``ORIENTATION_CLOSE`` (fallback step a)."""
        return (
            self.nearest_verified_distance is not None and self.nearest_verified_distance <= ORIENTATION_CLOSE
        )

    @property
    def fresh_until(self) -> datetime | None:
        """The oldest used point's evidence-age deadline (``None`` without points)."""
        if not self.points:
            return None
        oldest = min(p.observation.observed_at for p in self.points)
        return oldest + timedelta(days=self.max_age_days)


def orient_comparables(
    target: ComparableTarget,
    candidates: Sequence[MarketObservation],
    config: ComparableConfig,
    as_of: datetime,
    fx_rates: Sequence[FxRate] = (),
    *,
    kind: EvidenceKind,
    taxonomy: VehicleTaxonomy | None = None,
    limit: int = ORIENTATION_LIMIT,
) -> OrientationResult:
    """The ``limit`` nearest same-model MK points of one evidence ``kind`` (module docstring).

    ``kind`` is ``asking_price`` (MK asking prices) or ``owner_estimate`` (the owner's
    estimates); every other kind is excluded as ``NOT_COMPARABLE_EVIDENCE_KIND``. Hard: the
    prefilter (market, fixture lineage, price, FX, after ``as_of``, stale), the canonical make
    and model, condition and duplicates (cluster and ad URL, as in ``select_comparables``).
    Everything else adds a labelled ``orientation_*`` difference and its ``ORIENTATION_PENALTIES``
    distance; year and mileage add continuous terms. Ranking: distance, then the newer
    observation, then the id -- deterministic and independent of candidate order.
    """
    as_of = _aware(as_of)
    if len(candidates) > MAX_CANDIDATES:
        raise ValidationFailed(f"too many comparable candidates (max {MAX_CANDIDATES})")
    ids = [c.id for c in candidates]
    if len(set(ids)) != len(ids):
        raise ValidationFailed("duplicate market observation ids in comparable candidates")
    if kind not in (EvidenceKind.ASKING_PRICE, EvidenceKind.OWNER_ESTIMATE):
        raise ValidationFailed("an orientation is over MK asking prices or the owner's estimates only")
    if isinstance(limit, bool) or not 1 <= limit <= ORIENTATION_MAX_LIMIT:
        raise ValidationFailed(f"orientation limit must be within [1, {ORIENTATION_MAX_LIMIT}]")
    criteria = _criteria(config)
    target = target.with_canonical_names(taxonomy)
    currencies = {
        c.amount.currency for c in candidates if c.amount is not None and c.amount.currency != "EUR"
    }
    rates, warnings = _usable_rates(fx_rates, as_of, currencies)
    kinds = OWNER_ESTIMATE_KINDS if kind == EvidenceKind.OWNER_ESTIMATE else (kind,)
    names = _NameResolver(taxonomy)
    evaluations: list[_Evaluation] = []
    for obs in sorted(candidates, key=lambda c: str(c.id)):
        ev = _evaluate(target, obs, criteria, as_of, rates, kinds=kinds, names=names)
        # The prefilter only separates estimates from market rows: sales never orient asking prices.
        if obs.evidence_kind != kind and ExclusionReason.NOT_COMPARABLE_EVIDENCE_KIND not in ev.prefilter:
            ev.prefilter.append(ExclusionReason.NOT_COMPARABLE_EVIDENCE_KIND)
        evaluations.append(ev)
    duplicates = _dedupe(evaluations, _orientation_eligible)

    eligible: list[tuple[Decimal, _Evaluation, tuple[MatchDifference, ...]]] = []
    verified: list[Decimal] = []
    excluded: list[ExcludedComparable] = []
    for ev in evaluations:
        dup = duplicates.get(ev.obs.id)
        reasons = [*ev.prefilter, *(r for r in ev.hard if r in ORIENTATION_HARD), *(dup[1] if dup else ())]
        if reasons:
            excluded.append(
                ExcludedComparable(
                    observation_id=ev.obs.id,
                    evidence_kind=ev.obs.evidence_kind,
                    reasons=tuple(sorted(set(reasons), key=_REASON_ORDER.__getitem__)),
                    duplicate_of=None if dup is None else dup[0],
                    details=tuple((list(ev.notes) + _duplicate_notes(ev, dup))[:30]),
                )
            )
            continue
        distance, differences = _orientation_terms(ev, target, criteria)
        eligible.append((distance, ev, differences))
        verified.append(distance + _orientation_worst_case(ev))

    eligible.sort(key=lambda item: (item[0], -_micros(item[1].obs.observed_at), str(item[1].obs.id)))
    points: list[OrientationPoint] = []
    for rank, (distance, ev, differences) in enumerate(eligible[:limit], start=1):
        assert ev.amount_eur is not None  # guaranteed by the prefilter
        points.append(
            OrientationPoint(
                rank=rank,
                observation_id=ev.obs.id,
                evidence_kind=ev.obs.evidence_kind,
                amount_eur=ev.amount_eur,
                distance=distance,
                differences=differences[:50],
                observation=ev.obs,
            )
        )
    notes = [
        f"ORIENTATION_ONLY: no spec-15 comparable; relaxed same-model orientation ({ORIENTATION_VERSION}) "
        f"over MK {kind.value} evidence; research needed"
    ]
    nearest_verified = min(verified, default=None)
    if not points:
        notes.append(f"ORIENTATION_EMPTY: no same-model MK {kind.value} point")
    elif nearest_verified is None or nearest_verified > ORIENTATION_CLOSE:
        notes.append(
            f"ORIENTATION_DISTANT: no point is verified within distance {_num(ORIENTATION_CLOSE)} "
            f"(nearest {_num(points[0].distance)}, verified {_num(nearest_verified or Decimal(0))}; "
            "e.g. another fuel or generation, or an unknown fuel, gearbox or drive)"
        )
    excluded.sort(key=lambda e: str(e.observation_id))
    return OrientationResult(
        as_of=as_of,
        target=target,
        evidence_kind=kind,
        limit=limit,
        max_age_days=criteria.max_age_days,
        eligible_count=len(eligible),
        points=tuple(points),
        excluded=tuple(excluded),
        nearest_verified_distance=nearest_verified,
        warnings=tuple(notes + warnings),
    )


def _orientation_terms(
    ev: _Evaluation, target: ComparableTarget, criteria: MatchingCriteria
) -> tuple[Decimal, tuple[MatchDifference, ...]]:
    """Distance and labelled differences of one eligible candidate (``orient_comparables``)."""
    vehicle = ev.obs.vehicle
    differences = list(ev.differences)
    distance = Decimal(0)
    values: dict[str, tuple[str | None, str | None]] = {
        "fuel": (target.fuel.value, vehicle.fuel.value),
        "gearbox": (target.gearbox.value, vehicle.gearbox.value),
        "drive": (target.drive.value, vehicle.drive.value),
        "generation": (target.generation, vehicle.generation),
        "engine": (
            _engine_text(target.engine_code, target.engine_displacement_cm3, target.power_kw),
            _engine_text(vehicle.engine_code, vehicle.engine_displacement_cm3, vehicle.power_kw),
        ),
        "year": (None if target.year is None else str(target.year), None),
        "mileage": (None if target.mileage_km is None else _km(target.mileage_km), None),
    }
    for reason in sorted(set(ev.hard), key=_REASON_ORDER.__getitem__):
        penalty = ORIENTATION_PENALTIES.get(reason)
        if penalty is None:
            continue
        distance += penalty
        dimension = _ORIENTATION_DIMENSION[reason]
        t_value, c_value = values[dimension]
        differences.append(
            MatchDifference(
                dimension=dimension,
                code=f"{_ORIENTATION_PREFIX}{reason.value.lower()}",
                severity=DifferenceSeverity.UNVERIFIED
                if reason in _ORIENTATION_UNKNOWN
                else DifferenceSeverity.DIFFERS,
                target=_bounded(t_value, 200) if t_value else None,
                comparable=_bounded(c_value, 200) if c_value else None,
            )
        )
    facelift = ev.needs.get("facelift")
    if facelift is not None:
        distance += ORIENTATION_FACELIFT_POINTS
        differences.append(
            facelift.model_copy(
                update={
                    "code": f"{_ORIENTATION_PREFIX}facelift_differs",
                    "severity": DifferenceSeverity.DIFFERS,
                }
            )
        )
    c_year, _source = _vehicle_year(vehicle)
    if target.year is not None and c_year is not None:
        years = abs(c_year - target.year)
        distance += Decimal(years) * ORIENTATION_YEAR_POINTS
        if years > criteria.year_window:
            differences.append(
                MatchDifference(
                    dimension="year",
                    code=f"{_ORIENTATION_PREFIX}year_differs",
                    severity=DifferenceSeverity.DIFFERS,
                    target=str(target.year),
                    comparable=str(c_year),
                )
            )
    c_km = vehicle.mileage_km
    if target.mileage_km is not None and c_km is not None:
        km = abs(c_km - target.mileage_km)
        distance += km / ORIENTATION_MILEAGE_UNIT_KM
        if km > criteria.mileage_window_km:
            differences.append(
                MatchDifference(
                    dimension="mileage",
                    code=f"{_ORIENTATION_PREFIX}mileage_differs",
                    severity=DifferenceSeverity.DIFFERS,
                    target=_km(target.mileage_km),
                    comparable=_km(c_km),
                )
            )
    return distance.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_EVEN), tuple(differences)


def _orientation_worst_case(ev: _Evaluation) -> Decimal:
    """Extra distance of the closeness test: each unknown fuel, gearbox or drive counted as the
    mismatch it may hide (``ORIENTATION_PENALTIES`` of the mismatch less that of the unknown)."""
    return sum(
        (
            ORIENTATION_PENALTIES[_ORIENTATION_WORST_CASE[reason]] - ORIENTATION_PENALTIES[reason]
            for reason in set(ev.hard)
            if reason in _ORIENTATION_WORST_CASE
        ),
        Decimal(0),
    )


def proceeds_from_orientation(
    result: OrientationResult,
    negotiation_discount_pct: Decimal | None = None,
    *,
    discount_approved: bool = False,
    sensitivity_discounts_pct: Sequence[Decimal] = (),
) -> ComparableProceeds:
    """Labelled proceeds from an orientation's points (never a spec-15 comparable basis).

    base = median of the points' EUR amounts; low/high = first/third quartiles from n >= 5,
    else min/max. Asking prices: basis ``mk_asking_prices``, the negotiation discount passed
    through undiscounted (the scenario engine applies it once). Owner estimates: basis
    ``owner_estimate``, no discount (the discount arguments are ignored). Always
    ``research_needed`` and never a ``comparable_set_id`` (inquiries rely on that: such proceeds
    are no market evidence). No point -> ``unknown``.
    """
    asking = result.evidence_kind == EvidenceKind.ASKING_PRICE
    discount = _discount(negotiation_discount_pct, "negotiation_discount_pct") if asking else None
    if asking and discount_approved and discount is None:
        raise ValidationFailed("an approved discount needs a value")
    hypotheticals = (
        tuple(_pct(d, "sensitivity_discounts_pct") for d in sensitivity_discounts_pct) if asking else ()
    )
    basis: ProceedsBasis = "mk_asking_prices" if asking else "owner_estimate"
    if not result.points:
        return ComparableProceeds(
            status=CostLineStatus.UNKNOWN,
            low=None,
            base=None,
            high=None,
            basis=basis,
            evidence_kind=None,
            sample_size=0,
            negotiation_discount_pct=discount,
            discount_approved=discount_approved and asking,
            evidence_ids=(),
            labels=(
                "no same-model MK orientation point",
                "research needed; no MK comparable proceeds evidence",
            ),
            research_needed=True,
        )
    values = sorted(p.amount_eur for p in result.points)
    n = len(values)
    if n >= QUARTILE_MIN_SAMPLE:
        low, high = quantile(values, Decimal("0.25")), quantile(values, Decimal("0.75"))
        spread = "low/high are the first/third quartiles"
    else:
        low, high = values[0], values[-1]
        spread = "low/high are min/max of the orientation points"
    distances = [p.distance for p in result.points]
    labels = [ORIENTATION_LABEL]
    if asking:
        if discount is None:
            labels.append(DISCOUNT_UNKNOWN_LABEL)
        else:
            state = "approved" if discount_approved else "unapproved assumption"
            labels.append(f"less {discount}% negotiation discount ({state}) applied by the scenario engine")
    else:
        labels.append(OWNER_ESTIMATE_LABEL)
    labels += [
        f"{n} nearest same-model point(s), distance {_num(min(distances))}-{_num(max(distances))}",
        spread,
        "research needed",
    ]
    base = Money.of(_cents(quantile(values, Decimal("0.5"))), "EUR")
    return ComparableProceeds(
        status=CostLineStatus.ESTIMATED,
        low=Money.of(_cents(low), "EUR"),
        base=base,
        high=Money.of(_cents(high), "EUR"),
        basis=basis,
        evidence_kind=result.evidence_kind,
        sample_size=n,
        negotiation_discount_pct=discount,
        discount_approved=discount_approved and asking,
        evidence_ids=tuple(str(p.observation_id) for p in result.points),
        comparable_set_id=None,
        labels=tuple(labels),
        research_needed=True,
        sensitivity=tuple(
            SensitivityPoint(discount_pct=d, base_after_discount=_discounted(base, d)) for d in hypotheticals
        ),
    )


def orientation_warning(result: OrientationResult) -> str:
    """One valuation warning (at most 500 characters) that summarises an orientation."""
    head = f"MK_ORIENTATION:{result.evidence_kind.value}"
    if not result.points:
        return _bounded(f"{head}: no same-model MK point ({result.version})", 500)
    distances = [p.distance for p in result.points]
    counts: Counter[str] = Counter()
    for point in result.points:
        counts.update(
            {
                d.code.removeprefix(_ORIENTATION_PREFIX)
                for d in point.differences
                if d.code.startswith(_ORIENTATION_PREFIX)
            }
        )
    differs = ", ".join(f"{code} x{counts[code]}" for code in sorted(counts)) or "none"
    return _bounded(
        f"{head}: {len(result.points)} nearest same-model MK point(s), distance "
        f"{_num(min(distances))}-{_num(max(distances))} ({result.version}); differs: {differs}; "
        "a framework orientation, not a spec-15 comparable set",
        500,
    )


def _micros(value: datetime) -> int:
    """Integer microseconds since the epoch (exact; never a float timestamp)."""
    return (value - _EPOCH) // timedelta(microseconds=1)


def _num(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _aware(value: datetime) -> datetime:
    try:
        return ensure_utc(value)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def _norm(text: str | None) -> str:
    """Accent-insensitive, case-insensitive, punctuation-insensitive name key."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in stripped).split())


def ad_url_key(url: str | None) -> str | None:
    """The identity of an ad URL for duplicate detection (``None`` = no usable key).

    Host (lowercased, a leading ``www.`` removed) + path (trailing ``/`` removed) + the query
    string VERBATIM: a Reklama5 ad is ``/AdDetails?ad=<id>``, so dropping the query would merge
    every ad into one. Scheme, port, userinfo and fragment are dropped, so ``http``/``https``,
    ``www.``, a trailing slash and ``#photos`` all name the same ad.
    """
    if url is None:
        return None
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.removeprefix("www.")
    path = parts.path.rstrip("/")
    return f"{host}{path}?{parts.query}" if parts.query else f"{host}{path}"


def _engine_code(text: str | None) -> str:
    return "".join(c for c in _norm(text) if c.isalnum()).upper()


def _codes_compatible(a: str, b: str) -> bool:
    """Equal codes, or one is a >= 3 character prefix of the other (``N47`` vs ``N47D20``).

    Equal codes always match, including short ones (``K9`` = ``K9``); only the *prefix* rule needs
    three characters so that ``N4`` cannot claim to be a variant of ``N47D20``.
    """
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= 3 and longer.startswith(shorter)


def _drive_class(drive: Drive) -> str:
    if drive in _TWO_WD:
        return "2wd"
    if drive in _ALL_WD:
        return "awd_4wd"
    return "unknown"


def _vehicle_year(
    vehicle: VehicleSpec,
) -> tuple[int | None, Literal["first_registration", "model_year", "unknown"]]:
    if vehicle.first_registration.year is not None:
        return vehicle.first_registration.year, "first_registration"
    if vehicle.model_year is not None:
        return vehicle.model_year, "model_year"
    return None, "unknown"


def _unverified(dimension: str, code: str, target: str | None, comparable: str | None) -> MatchDifference:
    return MatchDifference(
        dimension=dimension,
        code=code,
        severity=DifferenceSeverity.UNVERIFIED,
        target=target,
        comparable=comparable,
    )


def _engine_text(code: str | None, displacement: int | None, power: int | None) -> str:
    return f"code={code or 'unknown'} {displacement or 'unknown'} cm3 {power or 'unknown'} kW"


def _bounded(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _km(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _cents(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_EVEN)
