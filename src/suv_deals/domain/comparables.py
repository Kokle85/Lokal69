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
- Duplicates are removed per vehicle cluster *and* evidence kind: the newest observation is kept
  and the others are recorded as ``DUPLICATE_OF_CLUSTER`` with the kept id.
- Evidence kinds are never mixed: statistics are computed per ``EvidenceKind``. Removed or
  sold-claimed listings stay *asking-price* evidence; an advertised price at removal is never a
  sale price. ``owner_estimate`` is an assumption, not a comparable.
- Statistics: n, min, max and median always (for n >= 1); quartiles only when n >= 5. Small
  samples are labelled; every individual point stays visible next to the aggregate.
- Stale evidence (older than ``comparable_max_age_days`` at ``as_of``) is excluded but the
  caller keeps it historically. Evidence observed after ``as_of`` is excluded so a set is
  reproducible as of its computation time.
- Amounts are converted to EUR with the recorded FX rates (explicit direction, unrounded).
  An unconvertible currency excludes the candidate; it is never treated as zero.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any, Final, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import (
    Availability,
    ClaimStatus,
    CostLineStatus,
    Drive,
    EvidenceKind,
    Fuel,
    Gearbox,
    PriceBasis,
    SellerType,
    Tristate,
)
from suv_deals.domain.listings import NormalizedListing, VehicleSpec
from suv_deals.domain.money import FxRate, Money, convert_to_eur, to_decimal
from suv_deals.domain.profiles import MK_ASKING_BAND_MAX_EUR, MK_ASKING_BAND_MIN_EUR
from suv_deals.errors import ValidationFailed

CRITERIA_VERSION: Final = "mk-comparables@1.0.0"

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
#: Weights by match level. Informational inclusion weights; statistics stay unweighted.
MATCH_WEIGHTS: Final[dict[str, Decimal]] = {
    "exact": Decimal("1.000000"),
    "close": Decimal("0.750000"),
    "widened": Decimal("0.500000"),
}
#: Evidence kinds whose selected count can make a set adequate (seller sale claims are unverified).
ADEQUACY_KINDS: Final = (EvidenceKind.ASKING_PRICE, EvidenceKind.VERIFIED_SALE)
_AMOUNT_QUANTUM: Final = Decimal("0.000001")
_CENT: Final = Decimal("0.01")
_FROZEN = ConfigDict(frozen=True, extra="forbid")

LocalRegistrationStatus = Literal["locally_registered", "imported_unregistered", "unknown"]
ComparableStatus = Literal["adequate", "small_sample", "insufficient_comparables"]
MatchLevel = Literal["exact", "close", "widened"]
BandFit = Literal["below", "within", "above", "unknown"]
SampleLabel = Literal["single_observation", "small_sample", "adequate"]
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
) -> ComparableSetResult:
    """Select, exclude and summarise MK comparables for ``target`` (spec 15).

    Deterministic: the same inputs always produce the same result, independent of candidate
    order. Raises ``ValidationFailed`` for malformed input (duplicate ids, too many candidates,
    naive ``as_of``).
    """
    as_of = _aware(as_of)
    if len(candidates) > MAX_CANDIDATES:
        raise ValidationFailed(f"too many comparable candidates (max {MAX_CANDIDATES})")
    ids = [c.id for c in candidates]
    if len(set(ids)) != len(ids):
        raise ValidationFailed("duplicate market observation ids in comparable candidates")
    criteria = _criteria(config)
    warnings = _target_warnings(target)
    rates = list(fx_rates)

    ordered = sorted(candidates, key=lambda c: str(c.id))
    evaluations = [_evaluate(target, obs, criteria, as_of, rates) for obs in ordered]
    duplicates = _dedupe(evaluations)

    level, steps = _widen(evaluations, duplicates, criteria)

    selected: list[SelectedComparable] = []
    excluded: list[ExcludedComparable] = []
    for ev in evaluations:
        reasons = list(ev.prefilter) + list(ev.hard) + list(ev.widen_failures)
        duplicate_of = duplicates.get(ev.obs.id)
        if duplicate_of is not None:
            reasons.append(ExclusionReason.DUPLICATE_OF_CLUSTER)
        if ev.required_level() > level:
            reasons.extend(_unadmitted_reasons(ev, level))
        if reasons:
            notes = list(ev.notes)
            if duplicate_of is not None:
                notes.append(f"DUPLICATE_OF_CLUSTER: cluster {ev.obs.cluster_id}; kept newer {duplicate_of}")
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
    adequacy = _adequacy_count(selected)
    status: ComparableStatus
    if adequacy >= criteria.min_sample:
        status = "adequate"
    elif adequacy > 0:
        status = "small_sample"
    else:
        status = "insufficient_comparables"
    if any(s.evidence_kind == EvidenceKind.SELLER_REPORTED_SALE for s in selected):
        warnings.append("SELLER_REPORTED_SALES_UNVERIFIED: seller sale claims never make a set adequate")
    fit, basis = _band_fit(
        next((s for s in stats if s.evidence_kind == EvidenceKind.ASKING_PRICE), None), status
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
        research_needed=status != "adequate",
        warnings=tuple(warnings),
    )


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
    return warnings


def _evaluate(
    target: ComparableTarget,
    obs: MarketObservation,
    criteria: MatchingCriteria,
    as_of: datetime,
    rates: list[FxRate],
) -> _Evaluation:
    ev = _Evaluation(obs)
    _prefilter(ev, target, criteria, as_of, rates)
    _match_identity(ev, target)
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
) -> None:
    obs = ev.obs
    if obs.market != "MK":
        ev.prefilter.append(ExclusionReason.WRONG_MARKET)
    if obs.is_fixture != target.is_fixture:
        ev.prefilter.append(ExclusionReason.FIXTURE_MISMATCH)
    if obs.evidence_kind == EvidenceKind.OWNER_ESTIMATE:
        ev.prefilter.append(ExclusionReason.NOT_COMPARABLE_EVIDENCE_KIND)
    if obs.amount is None or obs.amount.amount == 0:
        # A zero amount is "price on request" or a parse artefact: unknown, never EUR 0.
        ev.prefilter.append(ExclusionReason.PRICE_MISSING)
    else:
        eur = convert_to_eur(obs.amount, rates)
        if eur is None:
            ev.prefilter.append(ExclusionReason.CURRENCY_UNCONVERTIBLE)
            ev.notes.append(f"CURRENCY_UNCONVERTIBLE: no recorded EUR/{obs.amount.currency} rate")
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


def _match_identity(ev: _Evaluation, target: ComparableTarget) -> None:
    vehicle = ev.obs.vehicle
    t_make, t_model = _norm(target.make), _norm(target.model)
    c_make, c_model = _norm(vehicle.make), _norm(vehicle.model)
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
    cond = ev.obs.condition
    positive = (ClaimStatus.SELLER_CLAIMED, ClaimStatus.VERIFIED)
    cand_damaged = cond.damaged_vehicle in positive or cond.running == ClaimStatus.SELLER_DENIED
    target_damaged = target.damaged_vehicle in positive or target.running == ClaimStatus.SELLER_DENIED
    if cand_damaged and not target_damaged:
        ev.reject(ExclusionReason.CONDITION_MISMATCH, "comparable is damaged or non-running; target is not")


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


def _dedupe(evaluations: list[_Evaluation]) -> dict[UUID, UUID]:
    """Return {duplicate id: kept id}. Only valid evidence (no prefilter reason) is deduplicated."""
    groups: dict[tuple[UUID, EvidenceKind], list[_Evaluation]] = {}
    for ev in evaluations:
        if ev.prefilter or ev.obs.cluster_id is None:
            continue
        groups.setdefault((ev.obs.cluster_id, ev.obs.evidence_kind), []).append(ev)
    duplicates: dict[UUID, UUID] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        # Newest observation wins; equal times fall back to the lexicographically smallest id.
        by_id = sorted(members, key=lambda e: str(e.obs.id))
        ranked = sorted(by_id, key=lambda e: e.obs.observed_at, reverse=True)  # stable sort
        keep = ranked[0].obs.id
        for other in ranked[1:]:
            duplicates[other.obs.id] = keep
    return duplicates


def _widen(
    evaluations: list[_Evaluation], duplicates: dict[UUID, UUID], criteria: MatchingCriteria
) -> tuple[int, list[WideningStep]]:
    """Apply widening steps one dimension at a time while the adequacy count is below minimum."""
    eligible = [
        ev
        for ev in evaluations
        if not (ev.prefilter or ev.hard or ev.widen_failures) and ev.obs.id not in duplicates
    ]

    def adequacy_at(level: int) -> tuple[int, int]:
        admitted = [ev for ev in eligible if ev.required_level() <= level]
        counts = {kind: sum(1 for ev in admitted if ev.obs.evidence_kind == kind) for kind in ADEQUACY_KINDS}
        return len(admitted), max(counts.values())

    level = 0
    steps: list[WideningStep] = []
    _, adequacy = adequacy_at(level)
    max_level = len(_WIDEN_ORDER)
    while adequacy < criteria.min_sample and level < max_level:
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


def _adequacy_count(selected: Iterable[SelectedComparable]) -> int:
    items = list(selected)
    return max(sum(1 for s in items if s.evidence_kind == kind) for kind in ADEQUACY_KINDS)


def _kind_order(kind: EvidenceKind) -> int:
    return list(EvidenceKind).index(kind)


# ---------------------------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------------------------


def _stats(kind: EvidenceKind, items: list[SelectedComparable], min_sample: int) -> EvidenceStats:
    values = sorted(s.amount_eur for s in items)
    n = len(values)
    label: SampleLabel = (
        "single_observation" if n == 1 else ("adequate" if n >= min_sample else "small_sample")
    )
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


def _band_fit(asking: EvidenceStats | None, status: ComparableStatus) -> tuple[BandFit, str]:
    if asking is None:
        return "unknown", "no selected MK asking-price comparables"
    median = asking.median
    fit: BandFit
    if median < MK_ASKING_BAND_MIN_EUR:
        fit = "below"
    elif median > MK_ASKING_BAND_MAX_EUR:
        fit = "above"
    else:
        fit = "within"
    quality = "adequate sample" if status == "adequate" else f"{asking.sample_label.replace('_', ' ')}"
    basis = (
        f"median asking price EUR {median} of {asking.n} MK asking-price comparable(s) ({quality}) "
        f"versus the EUR {MK_ASKING_BAND_MIN_EUR}-{MK_ASKING_BAND_MAX_EUR} asking-price research band; "
        "asking prices are not realized sales"
    )
    return fit, basis


# ---------------------------------------------------------------------------------------------
# Proceeds
# ---------------------------------------------------------------------------------------------

ProceedsBasis = Literal["mk_asking_prices", "verified_sales"]
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


def _engine_code(text: str | None) -> str:
    return "".join(c for c in _norm(text) if c.isalnum()).upper()


def _codes_compatible(a: str, b: str) -> bool:
    """Equal codes, or one is a >= 3 character prefix of the other (``N47`` vs ``N47D20``)."""
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
