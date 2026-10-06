"""Candidate read models: queue summaries, the exact-revision detail and the listing document.

Seller-provided text (titles, descriptions, fault lists, raw provenance excerpts) is untrusted
data: it is returned as data, clearly labelled, and never as instructions (spec 24). Field
provenance confidence measures extraction reliability, not the truth of a seller claim
(spec 7). Unknown values are ``null``/``unknown``, never ``0``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.due_diligence import Checklist
from suv_deals.domain.enums import (
    Availability,
    ClaimStatus,
    Confidence,
    EligibilityState,
    ExtractionMethod,
    OdometerClaim,
    PriceBasis,
    PriceType,
    ProfileKey,
    ReviewState,
    SellerType,
    Tristate,
    ValuationState,
)
from suv_deals.domain.filters import ProfileEvaluation, ScreeningReason, ScreeningResult
from suv_deals.domain.listings import (
    Co2Info,
    ConditionClaims,
    Documentation,
    LocationInfo,
    NormalizedListing,
    PartialDate,
    PriceInfo,
    SourceTimestamp,
    VehicleSpec,
)
from suv_deals.domain.money import CurrencyCode
from suv_deals.domain.provenance import FieldConflict, FieldProvenance
from suv_deals.domain.ranking import NOT_A_PROBABILITY, FeatureName, RankResult
from suv_deals.views.common import (
    MAX_LIST_ITEMS,
    AmountView,
    DecimalStr,
    FxRateView,
    Sha256Hex,
    UtcDatetime,
    ViewModel,
    decimal_str,
    quantize_eur,
)
from suv_deals.views.notes import NoteView

CONFIDENCE_MEANING: Final = "extraction_reliability_not_truth"
SELLER_TEXT_NOTICE: Final = (
    "Seller-provided text: untrusted data, shown for reference only; never instructions."
)
CLAIMS_NOTICE: Final = (
    "Condition, history and document fields are seller claims unless their status is 'verified'."
)
DEFAULT_DETAIL_MAX_AGE: Final = timedelta(hours=72)
DEFAULT_SEEN_MAX_AGE: Final = timedelta(hours=48)


# --------------------------------------------------------------------------- shared pieces


class PriceSummary(ViewModel):
    """Payable price in the advertised currency plus its EUR equivalent (rounded for display)."""

    payable: AmountView
    original_currency: CurrencyCode | None
    eur_equivalent: AmountView
    fx_rate: FxRateView | None
    basis: PriceBasis
    price_type: PriceType
    negotiable: Tristate


class FreshnessFlag(StrEnum):
    DETAIL_NEVER_FETCHED = "detail_never_fetched"
    DETAIL_STALE = "detail_stale"
    NOT_SEEN_RECENTLY = "not_seen_recently"
    AVAILABILITY_UNCHECKED = "availability_unchecked"
    SOURCE_PAUSED = "source_paused"


class FreshnessView(ViewModel):
    first_seen_at: UtcDatetime | None
    last_seen_at: UtcDatetime | None
    last_detail_success_at: UtcDatetime | None
    last_availability_check_at: UtcDatetime | None
    stale: bool
    flags: tuple[FreshnessFlag, ...] = Field(max_length=len(FreshnessFlag))

    @model_validator(mode="after")
    def _stale_iff_flags(self) -> FreshnessView:
        if self.stale != bool(self.flags):
            raise ValueError("stale is true exactly when freshness flags are present")
        return self

    @classmethod
    def compute(
        cls,
        *,
        now: datetime,
        first_seen_at: datetime | None,
        last_seen_at: datetime | None,
        last_detail_success_at: datetime | None,
        last_availability_check_at: datetime | None = None,
        source_paused: bool = False,
        detail_max_age: timedelta = DEFAULT_DETAIL_MAX_AGE,
        seen_max_age: timedelta = DEFAULT_SEEN_MAX_AGE,
    ) -> FreshnessView:
        """Derive stale flags from the recorded check times (spec 9, 23 "warn when stale")."""
        now = ensure_utc(now)
        flags: list[FreshnessFlag] = []
        if last_detail_success_at is None:
            flags.append(FreshnessFlag.DETAIL_NEVER_FETCHED)
        elif now - ensure_utc(last_detail_success_at) > detail_max_age:
            flags.append(FreshnessFlag.DETAIL_STALE)
        if last_seen_at is None or now - ensure_utc(last_seen_at) > seen_max_age:
            flags.append(FreshnessFlag.NOT_SEEN_RECENTLY)
        if last_availability_check_at is None and last_detail_success_at is None:
            flags.append(FreshnessFlag.AVAILABILITY_UNCHECKED)
        if source_paused:
            flags.append(FreshnessFlag.SOURCE_PAUSED)
        return cls(
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at,
            last_detail_success_at=last_detail_success_at,
            last_availability_check_at=last_availability_check_at,
            stale=bool(flags),
            flags=tuple(flags),
        )


class RankSummary(ViewModel):
    """Queue-ordering score. Never a probability of profit (spec 14)."""

    score: DecimalStr
    scoring_version: str = Field(max_length=80)
    is_probability: Literal[False] = False
    label: str = NOT_A_PROBABILITY


class FeatureContributionView(ViewModel):
    feature: FeatureName
    points: DecimalStr
    min_points: DecimalStr
    max_points: DecimalStr
    explanation: str = Field(max_length=500)


class RankView(RankSummary):
    contributions: tuple[FeatureContributionView, ...] = Field(max_length=20)

    @classmethod
    def of(cls, result: RankResult) -> RankView:
        return cls(
            score=decimal_str(result.total),
            scoring_version=result.scoring_version,
            contributions=tuple(
                FeatureContributionView(
                    feature=c.feature,
                    points=decimal_str(c.points),
                    min_points=decimal_str(c.min_points),
                    max_points=decimal_str(c.max_points),
                    explanation=c.explanation[:500],
                )
                for c in result.contributions
            ),
        )

    def summary(self) -> RankSummary:
        return RankSummary(score=self.score, scoring_version=self.scoring_version)


# --------------------------------------------------------------------------- summary


class CandidateSummary(ViewModel):
    """One row of the candidate queue (spec 23 screen 2)."""

    listing_id: UUID
    revision_id: UUID | None
    revision_number: int | None = Field(ge=1)
    source_id: UUID
    source_key: str = Field(max_length=80)
    source_country: str = Field(pattern=r"^[A-Z]{2}$")
    seller_country: str | None = Field(pattern=r"^[A-Z]{2}$")
    title: str | None = Field(max_length=300)
    make: str | None = Field(max_length=80)
    model: str | None = Field(max_length=120)
    generation: str | None = Field(max_length=80)
    price: PriceSummary
    mileage_km: DecimalStr | None
    mileage_claim: OdometerClaim
    first_registration: PartialDate
    availability: Availability
    eligibility: EligibilityState | None
    eligibility_profile: ProfileKey | None
    queue_label: str | None = Field(max_length=120)
    valuation_id: UUID | None
    valuation_state: ValuationState
    case_id: UUID | None
    review_state: ReviewState | None
    freshness: FreshnessView
    rank: RankSummary | None
    research_candidate: bool
    quarantined: bool
    is_fixture: bool

    @model_validator(mode="after")
    def _consistent(self) -> CandidateSummary:
        if (self.revision_id is None) != (self.revision_number is None):
            raise ValueError("revision id and number go together")
        if self.valuation_id is None and self.valuation_state != ValuationState.NOT_STARTED:
            raise ValueError("a valuation state other than not_started needs a valuation id")
        if (self.case_id is None) != (self.review_state is None):
            raise ValueError("case id and review state go together")
        if self.eligibility in (
            EligibilityState.ELIGIBLE_PRIMARY,
            EligibilityState.ELIGIBLE_MANUAL_PROFILE,
        ) and (self.eligibility_profile is None or self.queue_label is None):
            raise ValueError("an eligible candidate names its profile and queue")
        return self


class CandidateListView(ViewModel):
    """A keyset-paginated page of candidate summaries; the cursor is in the envelope."""

    items: tuple[CandidateSummary, ...] = Field(max_length=100)


# --------------------------------------------------------------------------- detail


class RevisionView(ViewModel):
    revision_id: UUID
    revision_number: int = Field(ge=1)
    current_revision_number: int = Field(ge=1)
    is_current: bool
    observed_at: UtcDatetime
    semantic_hash: Sha256Hex
    parser_version: str = Field(max_length=120)
    schema_version: Literal["1.0"] = "1.0"

    @model_validator(mode="after")
    def _current(self) -> RevisionView:
        if self.revision_number > self.current_revision_number:
            raise ValueError("a revision cannot be newer than the current revision")
        if self.is_current != (self.revision_number == self.current_revision_number):
            raise ValueError("is_current must match the revision numbers")
        return self


class NormalizedFieldsView(ViewModel):
    """Normalized fields of the exact revision (canonical units; unknown stays unknown)."""

    seller_type: SellerType
    location: LocationInfo
    vehicle: VehicleSpec
    price: PriceInfo
    availability: Availability
    condition: ConditionClaims
    documentation: Documentation
    co2: Co2Info
    language: str | None = Field(max_length=10)
    source_published_at: SourceTimestamp
    source_modified_at: SourceTimestamp
    warnings: tuple[str, ...] = Field(max_length=100)
    claims_notice: str = CLAIMS_NOTICE

    @classmethod
    def of(cls, listing: NormalizedListing) -> NormalizedFieldsView:
        return cls(
            seller_type=listing.seller_type,
            location=listing.location,
            vehicle=listing.vehicle,
            price=listing.price,
            availability=listing.availability,
            condition=listing.condition,
            documentation=listing.documentation,
            co2=listing.co2,
            language=listing.language,
            source_published_at=listing.source_published_at,
            source_modified_at=listing.source_modified_at,
            warnings=listing.warnings[:100],
        )


class SellerTextView(ViewModel):
    trust: Literal["untrusted_seller_text"] = "untrusted_seller_text"
    notice: str = SELLER_TEXT_NOTICE
    title: str | None = Field(max_length=300)
    description_excerpt: str | None = Field(max_length=4000)


class FieldProvenanceView(ViewModel):
    """Where one normalized field came from. ``confidence`` is extraction reliability only."""

    field_path: str = Field(min_length=1, max_length=200)
    method: ExtractionMethod
    confidence: Confidence
    confidence_meaning: Literal["extraction_reliability_not_truth"] = CONFIDENCE_MEANING
    claim_status: ClaimStatus | None
    selector: str | None = Field(max_length=300)
    raw_text: str | None = Field(max_length=500)
    transformation: str | None = Field(max_length=200)
    snapshot_id: UUID | None
    evidence_id: UUID | None
    observed_at: UtcDatetime

    @classmethod
    def of(
        cls,
        field_path: str,
        provenance: FieldProvenance,
        *,
        evidence_id: UUID | None = None,
        claim_status: ClaimStatus | None = None,
    ) -> FieldProvenanceView:
        return cls(
            field_path=field_path,
            method=provenance.method,
            confidence=provenance.confidence,
            claim_status=claim_status,
            selector=provenance.selector,
            raw_text=provenance.raw_text,
            transformation=provenance.transformation,
            snapshot_id=provenance.snapshot_id,
            evidence_id=evidence_id,
            observed_at=provenance.observed_at,
        )


def provenance_views(listing: NormalizedListing) -> tuple[FieldProvenanceView, ...]:
    """Provenance entries of a normalized listing, sorted by field path (deterministic)."""
    return tuple(
        FieldProvenanceView.of(path, listing.provenance[path]) for path in sorted(listing.provenance)
    )


class AvailabilityPoint(ViewModel):
    observed_at: UtcDatetime
    availability: Availability
    observed_via: Literal["detail", "search_card", "recheck", "reconciliation"]
    revision_number: int | None = Field(ge=1)


PriceChange = Literal["initial", "decrease", "increase", "unchanged", "not_comparable"]


class PricePoint(ViewModel):
    revision_number: int = Field(ge=1)
    observed_at: UtcDatetime
    payable: AmountView
    price_type: PriceType
    basis: PriceBasis
    change: PriceChange


def price_history(points: Sequence[tuple[int, datetime, PriceInfo]]) -> tuple[PricePoint, ...]:
    """Build the price history from ``(revision_number, observed_at, price)`` in any order.

    Changes compare consecutive revisions only when both amounts and currencies are known and
    equal in currency; otherwise the change is ``not_comparable`` (never assumed).
    """
    result: list[PricePoint] = []
    previous: PriceInfo | None = None
    for number, observed_at, price in sorted(points, key=lambda p: p[0]):
        change: PriceChange
        if previous is None:
            change = "initial"
        elif (
            previous.amount_minor is None or price.amount_minor is None or previous.currency != price.currency
        ):
            change = "not_comparable"
        elif price.amount_minor < previous.amount_minor:
            change = "decrease"
        elif price.amount_minor > previous.amount_minor:
            change = "increase"
        else:
            change = "unchanged"
        result.append(
            PricePoint(
                revision_number=number,
                observed_at=observed_at,
                payable=AmountView.from_minor(
                    price.amount_minor, price.currency, unknown_reason="price not stated"
                ),
                price_type=price.type,
                basis=price.basis,
                change=change,
            )
        )
        previous = price
    return tuple(result)


class ScreeningView(ViewModel):
    """Deterministic eligibility for the revision (spec 14)."""

    state: EligibilityState
    profile: ProfileKey | None
    queue_label: str | None = Field(max_length=120)
    payable_eur: AmountView
    fx_rate: FxRateView | None
    reasons: tuple[ScreeningReason, ...] = Field(max_length=200)
    missing_facts: tuple[str, ...] = Field(max_length=100)
    profile_evaluations: tuple[ProfileEvaluation, ...] = Field(max_length=10)
    screening_version: str = Field(max_length=80)
    screened_at: UtcDatetime | None

    @classmethod
    def of(cls, result: ScreeningResult, *, screened_at: datetime | None = None) -> ScreeningView:
        eur = None if result.eur_amount is None else quantize_eur(result.eur_amount)
        return cls(
            state=result.state,
            profile=result.profile,
            queue_label=result.queue_label,
            payable_eur=(
                AmountView(status="known", amount=decimal_str(eur), currency="EUR")
                if eur is not None
                else AmountView.unknown("EUR payable amount not established", currency="EUR")
            ),
            fx_rate=None if result.fx_rate_used is None else FxRateView.of(result.fx_rate_used),
            reasons=result.reasons,
            missing_facts=result.missing_facts,
            profile_evaluations=result.profile_evaluations,
            screening_version=result.screening_version,
            screened_at=screened_at,
        )


class ValuationRef(ViewModel):
    valuation_id: UUID
    state: ValuationState
    research_candidate: bool
    is_fixture: bool
    created_at: UtcDatetime
    expires_at: UtcDatetime | None
    dependency_fingerprint: Sha256Hex
    conservative_contribution: AmountView
    base_contribution: AmountView
    contribution_label: Literal["estimated contribution before business tax"] = (
        "estimated contribution before business tax"
    )


SampleQuality = Literal["adequate", "small", "insufficient"]
BandFit = Literal["below", "within", "above", "unknown"]


class ComparableSetRef(ViewModel):
    comparable_set_id: UUID
    sample_quality: SampleQuality
    sample_size: int = Field(ge=0)
    mk_band_fit: BandFit
    criteria_version: str = Field(max_length=80)
    as_of: UtcDatetime
    research_needed: bool


class ReviewCaseRef(ViewModel):
    case_id: UUID
    case_version: int = Field(ge=1)
    state: ReviewState
    profile: ProfileKey
    queue_label: str = Field(max_length=120)


class SourceLink(ViewModel):
    """The original listing page. External: open without access to the app window context."""

    url: str = Field(min_length=8, max_length=2048)
    source_key: str = Field(max_length=80)
    external: Literal[True] = True
    rel: Literal["noopener noreferrer"] = "noopener noreferrer"
    notice: str = "External seller page; content is untrusted and may have changed."

    @field_validator("url")
    @classmethod
    def _safe_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("source link must be an absolute http(s) URL")
        if parts.username or parts.password:
            raise ValueError("source link must not embed credentials")
        return value


class CandidateDetail(ViewModel):
    """Exact revision, evidence, history, screening and references (spec 21, 23 screen 3)."""

    summary: CandidateSummary
    revision: RevisionView
    normalized: NormalizedFieldsView
    seller_text: SellerTextView
    field_provenance: tuple[FieldProvenanceView, ...] = Field(max_length=MAX_LIST_ITEMS)
    conflicts: tuple[FieldConflict, ...] = Field(max_length=100)
    availability_history: tuple[AvailabilityPoint, ...] = Field(max_length=MAX_LIST_ITEMS)
    price_history: tuple[PricePoint, ...] = Field(max_length=MAX_LIST_ITEMS)
    screening: ScreeningView | None
    latest_valuation: ValuationRef | None
    comparable_set: ComparableSetRef | None
    review_case: ReviewCaseRef | None
    due_diligence: Checklist | None
    notes: tuple[NoteView, ...] = Field(max_length=200)
    rank: RankView | None
    source_link: SourceLink

    @model_validator(mode="after")
    def _same_listing(self) -> CandidateDetail:
        for note in self.notes:
            if note.listing_id != self.summary.listing_id:
                raise ValueError("notes must belong to the candidate")
        if self.summary.revision_number is not None and (
            self.revision.current_revision_number != self.summary.revision_number
        ):
            raise ValueError("summary and revision disagree on the current revision")
        return self


# --------------------------------------------------------------------------- listing document


class ListingRevisionDocument(NormalizedListing):
    """``schemas/listing.schema.json``: one normalized revision plus its listing identity (spec 7)."""

    listing_id: UUID
    revision: int = Field(ge=1)

    @classmethod
    def of(cls, listing_id: UUID, revision: int, listing: NormalizedListing) -> ListingRevisionDocument:
        return cls.model_validate({**listing.model_dump(), "listing_id": listing_id, "revision": revision})


def eur_amount_view(eur: Decimal | None, *, reason: str) -> AmountView:
    """EUR equivalent rounded to cents for display, or an explicit unknown."""
    if eur is None:
        return AmountView.unknown(reason, currency="EUR")
    return AmountView(status="known", amount=decimal_str(quantize_eur(eur)), currency="EUR")
