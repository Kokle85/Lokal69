"""Transparent, deterministic candidate ranking (spec sections 14, 18).

The ranking orders the human review queue. It is a documented points table, **not a
calibrated probability of profit** and never presented as one. Every feature contribution and
the ``SCORING_VERSION`` stay visible next to the total (spec 14).

Points table (``SCORING_VERSION = ranking@1.0.0``; total range -35 .. 100):

=========================  =========  ==========================================================
feature                    points     rule
=========================  =========  ==========================================================
acquisition_fit            0 .. 20    inside the acquisition band 20; below the band 10 (cheaper is
                                      not unsuitable, but outside the target); above the band
                                      20 - (excess EUR / 50), floor 0 (EUR 4,000 -> 0); unknown 0
comparable_quality         0 .. 20    adequate 20; small_sample 8; insufficient / not computed 0
resale_band_fit            0 .. 10    MK asking median within EUR 8,000-10,000: 10; above: 6;
                                      below / unknown: 0
conservative_scenario      0 .. 30    only from a *complete* valuation: conservative estimated
                                      contribution EUR / 100, clamped to [0, 30]; else 0
data_completeness          0 .. 10    known required facts / total required facts * 10
freshness                  0 .. 10    last successful check <= 24 h: 10; <= 72 h: 6; <= 7 d: 3;
                                      older, unknown or after ``as_of``: 0
mechanical_risk            -20 .. 0   -5 per distinct mechanical risk flag
document_risk              -15 .. 0   -5 per distinct document risk flag
=========================  =========  ==========================================================

No feature uses the PROPOSED EUR 1,500 contribution threshold; ranking research candidates
before an owner-approved threshold is allowed (spec 18), alerting on it is not. Ties are broken
deterministically by listing id. Pure code: no I/O, Decimal only.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import (
    Availability,
    ClaimStatus,
    Drive,
    Fuel,
    Gearbox,
    OdometerClaim,
    PriceBasis,
    PriceType,
    SellerType,
)
from suv_deals.domain.listings import NormalizedListing
from suv_deals.domain.profiles import PRIMARY_MAX_EUR, PRIMARY_MIN_EUR
from suv_deals.errors import ValidationFailed

SCORING_VERSION: Final = "ranking@1.0.0"
NOT_A_PROBABILITY: Final = (
    "Ranking score for ordering the review queue only; it is not a probability of profit."
)

MAX_POINTS: Final[dict[str, Decimal]] = {
    "acquisition_fit": Decimal(20),
    "comparable_quality": Decimal(20),
    "resale_band_fit": Decimal(10),
    "conservative_scenario": Decimal(30),
    "data_completeness": Decimal(10),
    "freshness": Decimal(10),
    "mechanical_risk": Decimal(0),
    "document_risk": Decimal(0),
}
MIN_POINTS: Final[dict[str, Decimal]] = {
    "mechanical_risk": Decimal(-20),
    "document_risk": Decimal(-15),
}
RISK_FLAG_PENALTY: Final = Decimal(-5)
ABOVE_BAND_EUR_PER_POINT: Final = Decimal(50)
CONTRIBUTION_EUR_PER_POINT: Final = Decimal(100)
_CENT: Final = Decimal("0.01")
_FROZEN = ConfigDict(frozen=True, extra="forbid")

ComparableStatus = Literal["adequate", "small_sample", "insufficient_comparables"]
BandFit = Literal["below", "within", "above", "unknown"]
FeatureName = Literal[
    "acquisition_fit",
    "comparable_quality",
    "resale_band_fit",
    "conservative_scenario",
    "data_completeness",
    "freshness",
    "mechanical_risk",
    "document_risk",
]
FEATURE_ORDER: Final[tuple[FeatureName, ...]] = (
    "acquisition_fit",
    "comparable_quality",
    "resale_band_fit",
    "conservative_scenario",
    "data_completeness",
    "freshness",
    "mechanical_risk",
    "document_risk",
)


class RankingFeatures(BaseModel):
    """Inputs to ``rank_candidate``. Unknown values stay ``None`` and earn no points."""

    model_config = _FROZEN

    listing_id: UUID
    as_of: datetime
    acquisition_price_eur: Decimal | None = Field(default=None, ge=0)
    band_min_eur: Decimal = PRIMARY_MIN_EUR
    band_max_eur: Decimal = PRIMARY_MAX_EUR
    comparable_status: ComparableStatus | None = None
    mk_band_fit: BandFit = "unknown"
    valuation_complete: bool = False
    conservative_contribution_eur: Decimal | None = None
    known_required_facts: int = Field(default=0, ge=0)
    total_required_facts: int = Field(default=1, ge=1)
    last_checked_at: datetime | None = None
    mechanical_risks: tuple[str, ...] = Field(default=(), max_length=50)
    document_risks: tuple[str, ...] = Field(default=(), max_length=50)

    @model_validator(mode="before")
    @classmethod
    def _no_float(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, float):
                    raise ValueError(f"{key}: binary float is not allowed; use Decimal")
        return data

    @field_validator("as_of", "last_checked_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _consistent(self) -> RankingFeatures:
        if self.known_required_facts > self.total_required_facts:
            raise ValueError("known_required_facts cannot exceed total_required_facts")
        if self.band_min_eur > self.band_max_eur:
            raise ValueError("band_min_eur must not exceed band_max_eur")
        if self.conservative_contribution_eur is not None and not self.valuation_complete:
            raise ValueError("a conservative contribution is only ranked from a complete valuation")
        return self


class FeatureContribution(BaseModel):
    model_config = _FROZEN

    feature: FeatureName
    points: Decimal
    min_points: Decimal
    max_points: Decimal
    explanation: str


class RankResult(BaseModel):
    model_config = _FROZEN

    listing_id: UUID
    scoring_version: str = SCORING_VERSION
    total: Decimal
    contributions: tuple[FeatureContribution, ...]
    label: str = NOT_A_PROBABILITY
    is_probability: Literal[False] = False

    @property
    def priority(self) -> int:
        """Integer queue priority for ``review_cases.priority`` (total in hundredths)."""
        return int((self.total * 100).to_integral_value(rounding=ROUND_HALF_EVEN))

    def sort_key(self) -> tuple[Decimal, str]:
        return (-self.total, str(self.listing_id))

    def contribution(self, feature: FeatureName) -> FeatureContribution:
        for item in self.contributions:
            if item.feature == feature:
                return item
        raise KeyError(feature)


def rank_candidate(features: RankingFeatures) -> RankResult:
    """Score one candidate with the documented points table; deterministic."""
    contributions = (
        _acquisition_fit(features),
        _comparable_quality(features),
        _resale_band_fit(features),
        _conservative_scenario(features),
        _data_completeness(features),
        _freshness(features),
        _risk("mechanical_risk", features.mechanical_risks),
        _risk("document_risk", features.document_risks),
    )
    total = sum((c.points for c in contributions), Decimal(0))
    return RankResult(listing_id=features.listing_id, total=_q(total), contributions=contributions)


def rank_candidates(items: Iterable[RankingFeatures]) -> list[RankResult]:
    """Rank many candidates: highest total first, ties broken by listing id (ascending)."""
    results = [rank_candidate(f) for f in items]
    ids = [r.listing_id for r in results]
    if len(set(ids)) != len(ids):
        raise ValidationFailed("duplicate listing ids in ranking input")
    return sorted(results, key=RankResult.sort_key)


# ---------------------------------------------------------------------------------------------
# Feature rules
# ---------------------------------------------------------------------------------------------


def _contribution(feature: FeatureName, points: Decimal, explanation: str) -> FeatureContribution:
    low = MIN_POINTS.get(feature, Decimal(0))
    high = MAX_POINTS[feature]
    bounded = min(max(points, low), high)
    return FeatureContribution(
        feature=feature, points=_q(bounded), min_points=low, max_points=high, explanation=explanation
    )


def _acquisition_fit(f: RankingFeatures) -> FeatureContribution:
    price = f.acquisition_price_eur
    band = f"EUR {f.band_min_eur}-{f.band_max_eur}"
    if price is None:
        return _contribution("acquisition_fit", Decimal(0), "acquisition price unknown; no points")
    if f.band_min_eur <= price <= f.band_max_eur:
        return _contribution("acquisition_fit", Decimal(20), f"EUR {price} inside the {band} band")
    if price < f.band_min_eur:
        return _contribution(
            "acquisition_fit",
            Decimal(10),
            f"EUR {price} below the {band} band (outside target, not unsuitable)",
        )
    excess = price - f.band_max_eur
    points = Decimal(20) - excess / ABOVE_BAND_EUR_PER_POINT
    return _contribution("acquisition_fit", points, f"EUR {price} is EUR {excess} above the {band} band")


def _comparable_quality(f: RankingFeatures) -> FeatureContribution:
    table: dict[str | None, tuple[Decimal, str]] = {
        "adequate": (Decimal(20), "adequate MK comparable sample"),
        "small_sample": (Decimal(8), "small MK comparable sample; research needed"),
        "insufficient_comparables": (Decimal(0), "insufficient MK comparables; research needed"),
        None: (Decimal(0), "MK comparables not computed"),
    }
    points, text = table[f.comparable_status]
    return _contribution("comparable_quality", points, text)


def _resale_band_fit(f: RankingFeatures) -> FeatureContribution:
    if f.comparable_status in (None, "insufficient_comparables"):
        return _contribution("resale_band_fit", Decimal(0), "no MK asking-price evidence for band fit")
    table: dict[str, tuple[Decimal, str]] = {
        "within": (Decimal(10), "MK asking median within the EUR 8,000-10,000 research band"),
        "above": (Decimal(6), "MK asking median above the research band; verify segment"),
        "below": (Decimal(0), "MK asking median below the research band"),
        "unknown": (Decimal(0), "MK band fit unknown"),
    }
    points, text = table[f.mk_band_fit]
    return _contribution("resale_band_fit", points, text + " (asking prices, not sales)")


def _conservative_scenario(f: RankingFeatures) -> FeatureContribution:
    if not f.valuation_complete or f.conservative_contribution_eur is None:
        return _contribution(
            "conservative_scenario",
            Decimal(0),
            "not available: valuation incomplete (unknown costs stay unknown)",
        )
    value = f.conservative_contribution_eur
    return _contribution(
        "conservative_scenario",
        value / CONTRIBUTION_EUR_PER_POINT,
        f"conservative estimated contribution before business tax EUR {value} (scenario, not a forecast)",
    )


def _data_completeness(f: RankingFeatures) -> FeatureContribution:
    points = Decimal(10) * Decimal(f.known_required_facts) / Decimal(f.total_required_facts)
    return _contribution(
        "data_completeness",
        points,
        f"{f.known_required_facts} of {f.total_required_facts} required facts known",
    )


def _freshness(f: RankingFeatures) -> FeatureContribution:
    checked = f.last_checked_at
    if checked is None:
        return _contribution("freshness", Decimal(0), "no successful detail check recorded")
    if checked > f.as_of:
        return _contribution("freshness", Decimal(0), "last check is after as_of; timestamp not trusted")
    age = f.as_of - checked
    hours = int(age.total_seconds() // 3600)
    for limit, points in ((timedelta(hours=24), 10), (timedelta(hours=72), 6), (timedelta(days=7), 3)):
        if age <= limit:
            return _contribution("freshness", Decimal(points), f"last successful check {hours} h ago")
    return _contribution("freshness", Decimal(0), f"last successful check {hours} h ago (stale)")


def _risk(feature: FeatureName, flags: Sequence[str]) -> FeatureContribution:
    distinct = sorted(set(flags))
    if not distinct:
        return _contribution(feature, Decimal(0), "no flagged risks")
    points = RISK_FLAG_PENALTY * len(distinct)
    return _contribution(feature, points, ", ".join(distinct))


def _q(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------------------------
# Feature extraction helpers (pure)
# ---------------------------------------------------------------------------------------------


def completeness_from_listing(listing: NormalizedListing) -> tuple[int, int]:
    """(known, total) over the required facts used by ``data_completeness``.

    An ``unknown`` enum or ``None`` counts as not known; a positively stated value counts.
    """
    v, p, d, c = listing.vehicle, listing.price, listing.documentation, listing.condition
    facts = (
        p.amount_minor is not None and p.currency is not None,
        p.basis != PriceBasis.UNKNOWN,
        p.type != PriceType.UNKNOWN,
        v.mileage_km is not None,
        v.mileage_claim not in (OdometerClaim.UNKNOWN, OdometerClaim.CONFLICTING),
        v.first_registration.year is not None,
        bool(v.make) and bool(v.model),
        v.generation is not None,
        v.fuel != Fuel.UNKNOWN,
        v.gearbox != Gearbox.UNKNOWN,
        v.drive != Drive.UNKNOWN,
        v.engine_displacement_cm3 is not None,
        v.power_kw is not None,
        listing.availability != Availability.UNKNOWN,
        listing.seller_type != SellerType.UNKNOWN,
        c.accident_free != ClaimStatus.UNKNOWN,
        d.vin is not None,
        listing.co2.g_per_km is not None,
    )
    return sum(1 for fact in facts if fact), len(facts)


def risk_flags_from_listing(listing: NormalizedListing) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(mechanical, document) risk flags derived from seller statements; claims, not inspections."""
    c, d, v = listing.condition, listing.documentation, listing.vehicle
    mechanical: list[str] = []
    if c.running == ClaimStatus.SELLER_DENIED:
        mechanical.append("non_running")
    if c.damaged_vehicle in (ClaimStatus.SELLER_CLAIMED, ClaimStatus.VERIFIED):
        mechanical.append("damaged_vehicle")
    if c.warning_lights_off == ClaimStatus.SELLER_DENIED:
        mechanical.append("warning_lights")
    if c.mechanical_faults:
        mechanical.append("faults_reported")
    if c.accident_free == ClaimStatus.SELLER_DENIED:
        mechanical.append("accident_reported")
    if c.accident_free == ClaimStatus.CONFLICTING:
        mechanical.append("accident_claims_conflict")
    if c.corrosion_free == ClaimStatus.SELLER_DENIED:
        mechanical.append("corrosion_reported")
    if v.mileage_claim == OdometerClaim.CONFLICTING:
        mechanical.append("odometer_conflict")
    document: list[str] = []
    if d.vin is None:
        document.append("vin_missing")
    if d.registration_documents == ClaimStatus.SELLER_DENIED:
        document.append("registration_documents_missing")
    if d.registration_documents == ClaimStatus.CONFLICTING:
        document.append("registration_documents_conflict")
    if d.coc_available == ClaimStatus.SELLER_DENIED:
        document.append("coc_missing")
    if listing.co2.g_per_km is None:
        document.append("co2_missing")
    return tuple(mechanical), tuple(document)
