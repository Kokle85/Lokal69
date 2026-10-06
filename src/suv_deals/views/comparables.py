"""MK comparable set read model (spec 15, 21 ``deals_get_comparables``, 23 screen 4).

Asking prices and sale evidence are reported separately: an advertised asking price is never a
realized sale price, and seller-reported sales are unverified claims. Small samples keep their
label, every statistic lists its individual points, and members (selected and, on request,
excluded with reasons) are paginated by a stable ordinal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, Literal
from uuid import UUID

from pydantic import Field, model_validator

from suv_deals.domain.comparables import (
    ComparableSetResult,
    ComparableStatus,
    EvidenceStats,
    ExclusionReason,
    MarketObservation,
    MatchDifference,
    MatchingCriteria,
    MatchLevel,
    SampleLabel,
    WidenDimension,
    WideningStep,
)
from suv_deals.domain.enums import Availability, EvidenceKind, PriceBasis, SellerType
from suv_deals.domain.profiles import MK_ASKING_BAND_MAX_EUR, MK_ASKING_BAND_MIN_EUR
from suv_deals.views.candidates import BandFit, SampleQuality, safe_http_url
from suv_deals.views.common import (
    AmountView,
    DecimalStr,
    UtcDatetime,
    ViewModel,
    decimal_str,
    quantize_eur,
)

ASKING_VS_SALE_NOTICE: Final = (
    "Asking prices are advertised amounts, not realized sale prices. Seller-reported sales are "
    "unverified claims; only verified sales document transaction prices."
)
EVIDENCE_NOTES: Final[dict[EvidenceKind, str]] = {
    EvidenceKind.ASKING_PRICE: "advertised asking prices; not realized sale prices",
    EvidenceKind.SELLER_REPORTED_SALE: "unverified seller statements about transactions",
    EvidenceKind.VERIFIED_SALE: "sales supported by permitted transaction evidence",
    EvidenceKind.OWNER_ESTIMATE: "owner assumption; not market evidence",
}
_QUALITY: Final[dict[str, SampleQuality]] = {
    "adequate": "adequate",
    "small_sample": "small",
    "insufficient_comparables": "insufficient",
}
LocalRegistration = Literal["locally_registered", "imported_unregistered", "unknown"]


class StatPointView(ViewModel):
    observation_id: UUID
    amount_eur: DecimalStr
    match_level: MatchLevel
    observed_at: UtcDatetime
    difference_codes: tuple[str, ...] = Field(max_length=50)


class MatchQualityCount(ViewModel):
    match_level: str = Field(max_length=40)
    count: int = Field(ge=0)


class EvidenceStatsView(ViewModel):
    """Statistics for exactly one evidence kind, in EUR rounded to cents; points stay visible."""

    evidence_kind: EvidenceKind
    evidence_note: str = Field(max_length=200)
    currency: Literal["EUR"] = "EUR"
    n: int = Field(ge=1)
    sample_label: SampleLabel
    min: DecimalStr
    max: DecimalStr
    median: DecimalStr
    q1: DecimalStr | None
    q3: DecimalStr | None
    date_from: UtcDatetime
    date_to: UtcDatetime
    match_quality: tuple[MatchQualityCount, ...] = Field(max_length=10)
    points: tuple[StatPointView, ...] = Field(max_length=5000)

    @classmethod
    def of(cls, stats: EvidenceStats) -> EvidenceStatsView:
        return cls(
            evidence_kind=stats.evidence_kind,
            evidence_note=stats.evidence_note,
            n=stats.n,
            sample_label=stats.sample_label,
            min=decimal_str(stats.min),
            max=decimal_str(stats.max),
            median=decimal_str(stats.median),
            q1=None if stats.q1 is None else decimal_str(stats.q1),
            q3=None if stats.q3 is None else decimal_str(stats.q3),
            date_from=stats.date_from,
            date_to=stats.date_to,
            match_quality=tuple(
                MatchQualityCount(match_level=level, count=count)
                for level, count in sorted(stats.match_quality.items())
            ),
            points=tuple(
                StatPointView(
                    observation_id=p.observation_id,
                    amount_eur=decimal_str(p.amount_eur),
                    match_level=p.match_level,
                    observed_at=p.observed_at,
                    difference_codes=p.difference_codes,
                )
                for p in stats.points
            ),
        )


class MkBandView(ViewModel):
    min_eur: DecimalStr
    max_eur: DecimalStr
    fit: BandFit
    basis: str = Field(max_length=500)
    meaning: str = "MK asking-price research band; not a proven realized sale price"


class ComparableMemberView(ViewModel):
    """One selected or excluded MK observation, with its match differences or exclusion reasons.

    ``duplicate_cluster_id`` is the observation's duplicate cluster (spec 15); ``duplicate_of``
    names the kept observation for a member excluded as a duplicate. ``url`` is null unless it
    is a safe absolute http(s) link.
    """

    ordinal: int = Field(ge=0)
    observation_id: UUID
    role: Literal["selected", "excluded"]
    evidence_kind: EvidenceKind
    evidence_note: str = Field(max_length=200)
    match_level: MatchLevel | None
    weight: DecimalStr | None
    widened_dimensions: tuple[WidenDimension, ...] = Field(max_length=3)
    differences: tuple[MatchDifference, ...] = Field(max_length=50)
    exclusion_reasons: tuple[ExclusionReason, ...] = Field(max_length=30)
    duplicate_of: UUID | None
    duplicate_cluster_id: UUID | None
    exclusion_details: tuple[str, ...] = Field(max_length=30)
    advertised: AmountView
    amount_eur: AmountView
    price_basis: PriceBasis | None
    observed_at: UtcDatetime | None
    source_key: str | None = Field(max_length=80)
    url: str | None = Field(max_length=2048)
    make: str | None = Field(max_length=80)
    model: str | None = Field(max_length=120)
    generation: str | None = Field(max_length=80)
    mileage_km: DecimalStr | None
    local_registration_status: LocalRegistration | None
    seller_type: SellerType | None
    availability: Availability | None
    is_fixture: bool | None

    @model_validator(mode="after")
    def _role(self) -> ComparableMemberView:
        if self.role == "selected":
            if self.match_level is None or self.weight is None or self.exclusion_reasons:
                raise ValueError("a selected member has a match level and weight and no exclusion reasons")
        elif not self.exclusion_reasons or self.weight is not None or self.match_level is not None:
            raise ValueError("an excluded member has at least one reason and no weight or match level")
        return self


def _observation_fields(obs: MarketObservation | None) -> dict[str, object]:
    if obs is None:
        return {
            "advertised": AmountView.unknown("observation details not loaded"),
            "duplicate_cluster_id": None,
            "price_basis": None,
            "observed_at": None,
            "source_key": None,
            "url": None,
            "make": None,
            "model": None,
            "generation": None,
            "mileage_km": None,
            "local_registration_status": None,
            "seller_type": None,
            "availability": None,
            "is_fixture": None,
        }
    return {
        "advertised": AmountView.of(obs.amount, unknown_reason="no advertised amount"),
        "duplicate_cluster_id": obs.cluster_id,
        "price_basis": obs.price_basis,
        "observed_at": obs.observed_at,
        "source_key": obs.source_key,
        "url": safe_http_url(obs.url),
        "make": obs.vehicle.make,
        "model": obs.vehicle.model,
        "generation": obs.vehicle.generation,
        "mileage_km": None if obs.vehicle.mileage_km is None else decimal_str(obs.vehicle.mileage_km),
        "local_registration_status": obs.local_registration_status,
        "seller_type": obs.seller_type,
        "availability": obs.availability,
        "is_fixture": obs.is_fixture,
    }


def comparable_members(
    result: ComparableSetResult,
    *,
    include_excluded: bool,
    excluded_observations: Mapping[UUID, MarketObservation] | None = None,
) -> tuple[ComparableMemberView, ...]:
    """All members in a stable order (selected first, then excluded); ordinals are page keys.

    Excluded members carry observation details only when ``excluded_observations`` supplies them.
    """
    members: list[ComparableMemberView] = []
    for item in result.selected:
        members.append(
            ComparableMemberView.model_validate(
                {
                    "ordinal": len(members),
                    "observation_id": item.observation_id,
                    "role": "selected",
                    "evidence_kind": item.evidence_kind,
                    "evidence_note": EVIDENCE_NOTES[item.evidence_kind],
                    "match_level": item.match_level,
                    "weight": decimal_str(item.weight),
                    "widened_dimensions": item.widened_dimensions,
                    "differences": item.differences,
                    "exclusion_reasons": (),
                    "duplicate_of": None,
                    "exclusion_details": (),
                    "amount_eur": AmountView(
                        status="known", amount=decimal_str(quantize_eur(item.amount_eur)), currency="EUR"
                    ),
                    **_observation_fields(item.observation),
                }
            )
        )
    if include_excluded:
        lookup = excluded_observations or {}
        for excluded in result.excluded:
            members.append(
                ComparableMemberView.model_validate(
                    {
                        "ordinal": len(members),
                        "observation_id": excluded.observation_id,
                        "role": "excluded",
                        "evidence_kind": excluded.evidence_kind,
                        "evidence_note": EVIDENCE_NOTES[excluded.evidence_kind],
                        "match_level": None,
                        "weight": None,
                        "widened_dimensions": (),
                        "differences": (),
                        "exclusion_reasons": excluded.reasons,
                        "duplicate_of": excluded.duplicate_of,
                        "exclusion_details": excluded.details[:30],
                        "amount_eur": AmountView.unknown("excluded evidence is not converted"),
                        **_observation_fields(lookup.get(excluded.observation_id)),
                    }
                )
            )
    return tuple(members)


class ComparableSetView(ViewModel):
    """One reproducible comparable set with a page of its members (cursor in the envelope)."""

    comparable_set_id: UUID
    listing_id: UUID
    target_revision_id: UUID
    criteria_version: str = Field(max_length=80)
    as_of: UtcDatetime
    status: ComparableStatus
    sample_quality: SampleQuality
    research_needed: bool
    is_fixture: bool
    criteria: MatchingCriteria
    widening_steps: tuple[WideningStep, ...] = Field(max_length=10)
    mk_band: MkBandView
    asking_price_stats: EvidenceStatsView | None
    seller_reported_sale_stats: EvidenceStatsView | None
    verified_sale_stats: EvidenceStatsView | None
    asking_vs_sale_notice: str = ASKING_VS_SALE_NOTICE
    selected_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    include_excluded: bool
    members: tuple[ComparableMemberView, ...] = Field(max_length=100)
    warnings: tuple[str, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def _consistent(self) -> ComparableSetView:
        if _QUALITY[self.status] != self.sample_quality:
            raise ValueError("sample_quality must match status")
        if self.status == "insufficient_comparables" and not self.research_needed:
            raise ValueError("insufficient comparables always need targeted research")
        if not self.include_excluded and any(m.role == "excluded" for m in self.members):
            raise ValueError("excluded members are only listed when requested")
        for kind, stats in (
            (EvidenceKind.ASKING_PRICE, self.asking_price_stats),
            (EvidenceKind.SELLER_REPORTED_SALE, self.seller_reported_sale_stats),
            (EvidenceKind.VERIFIED_SALE, self.verified_sale_stats),
        ):
            if stats is not None and stats.evidence_kind != kind:
                raise ValueError("statistics are reported per evidence kind, never mixed")
        return self

    @classmethod
    def of(
        cls,
        result: ComparableSetResult,
        *,
        comparable_set_id: UUID,
        listing_id: UUID,
        target_revision_id: UUID,
        include_excluded: bool,
        members: Sequence[ComparableMemberView],
    ) -> ComparableSetView:
        """``members`` is the requested page of ``comparable_members(result, ...)``."""
        stats = {s.evidence_kind: EvidenceStatsView.of(s) for s in result.stats}
        return cls(
            comparable_set_id=comparable_set_id,
            listing_id=listing_id,
            target_revision_id=target_revision_id,
            criteria_version=result.criteria_version,
            as_of=result.as_of,
            status=result.status,
            sample_quality=_QUALITY[result.status],
            research_needed=result.research_needed,
            is_fixture=result.target.is_fixture,
            criteria=result.criteria,
            widening_steps=result.widening_steps,
            mk_band=MkBandView(
                min_eur=decimal_str(MK_ASKING_BAND_MIN_EUR),
                max_eur=decimal_str(MK_ASKING_BAND_MAX_EUR),
                fit=result.mk_band_fit,
                basis=result.mk_band_fit_basis[:500],
            ),
            asking_price_stats=stats.get(EvidenceKind.ASKING_PRICE),
            seller_reported_sale_stats=stats.get(EvidenceKind.SELLER_REPORTED_SALE),
            verified_sale_stats=stats.get(EvidenceKind.VERIFIED_SALE),
            selected_count=len(result.selected),
            excluded_count=len(result.excluded),
            include_excluded=include_excluded,
            members=tuple(members),
            warnings=result.warnings[:100],
        )
