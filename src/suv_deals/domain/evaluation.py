"""The 15-day quality evaluation (spec 3 "Quality goal and evaluation window", 37.9, 37.10 U12).

The working goal is one genuinely useful deal in a 15-day window that starts when usable source
coverage is activated. It is a quality objective, not a guaranteed acquisition and not a volume
target. Pure domain code: no I/O; ``now`` is a parameter.

Window
    ``evaluation_window_start`` is the finish time of the first complete, healthy, non-fixture
    scan of an *activated* source at or after its activation time. Without such a scan the window
    has not started (``coverage_not_established``) - scans alone, or a configured schedule, do
    not start it. The window is ``[start, start + 15 days)``.

Metrics (inside the window, up to ``now``)
    Healthy coverage intervals and gaps per source; unique eligible and unique well-matched
    candidates (deduplicated by vehicle cluster, so the same car on three sites counts once);
    inquiries attempted/accepted/uncertain/failed/suppressed/held (``accepted`` = provider
    accepted, never "delivered"); matched seller replies (auto-replies, bounces and notices are
    counted separately and never as replies); missing documents resolved; the best supported
    economics (complete valuations only, conservative then base contribution, with the unknowns
    of every incomplete candidate listed - never zero).

Outcome
    ``deal_found`` needs a well-matched primary-profile candidate with a complete valuation whose
    conservative contribution meets the *owner-approved* threshold; with no approved threshold a
    positive supported contribution is ``candidates_need_owner_judgement``. Otherwise the report
    says ``no_suitable_deal_yet`` / ``no_suitable_deal`` with the honest reasons (coverage gaps,
    no eligible vehicles, insufficient comparables, unknown costs, negative economics). Nothing
    loosens price/mileage rules, hides costs or counts volume to reach the goal.

Synthetic data
    Fixture and canary candidates, inquiries, replies and document resolutions are excluded from
    every metric and reported only as an excluded count: a synthetic canary never counts as a
    deal, an inquiry or a reply.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.costs import CONTRIBUTION_LABEL
from suv_deals.domain.enums import (
    EligibilityState,
    InquiryState,
    ReplyMessageType,
    SuppressionReason,
    ValuationState,
)
from suv_deals.domain.lifecycle import ScanRecord, SourceCoverage, healthy_coverage
from suv_deals.domain.money import Money
from suv_deals.domain.notifications import check_owner_wording
from suv_deals.errors import ValidationFailed

EVALUATION_VERSION: Final = "evaluation/1.0.0"
EVALUATION_WINDOW_DAYS: Final = 15
EVALUATION_WINDOW: Final = timedelta(days=EVALUATION_WINDOW_DAYS)
DEFAULT_MAX_SCAN_GAP: Final = timedelta(hours=2)

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_COMPLETE_VALUATION: Final = frozenset({ValuationState.ESTIMATED, ValuationState.QUOTE_SUPPORTED})
_ACCEPTED_STATES: Final = frozenset(
    {
        InquiryState.ACCEPTED,
        InquiryState.REPLIED,
        InquiryState.BOUNCED,
        InquiryState.SELLER_OPTED_OUT,
        InquiryState.NO_REPLY_YET,
    }
)
_IN_PROGRESS_STATES: Final = frozenset(
    {
        InquiryState.CANDIDATE,
        InquiryState.QUALIFYING,
        InquiryState.RESERVED,
        InquiryState.QUEUED,
        InquiryState.SENDING,
    }
)


def _aware(value: datetime) -> datetime:
    try:
        return ensure_utc(value)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


# =============================================================================================
# Inputs
# =============================================================================================


class SourceActivation(BaseModel):
    """When a source's usable coverage was activated (``None``: not activated)."""

    model_config = _FROZEN

    source_key: str = Field(min_length=1, max_length=80)
    activated_at: datetime | None = None
    is_fixture: bool = False

    @field_validator("activated_at")
    @classmethod
    def _t(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


ComparableStatus = Literal["adequate", "small_sample", "insufficient_comparables"]


class EvaluationCandidate(BaseModel):
    """One source listing's evaluation facts. Unknown economics stay ``None`` (never zero)."""

    model_config = _FROZEN

    candidate_id: UUID
    vehicle_cluster_id: UUID | None = None
    first_seen_at: datetime
    eligibility: EligibilityState
    comparable_status: ComparableStatus | None = None
    valuation_state: ValuationState | None = None
    conservative_contribution: Money | None = None
    base_contribution: Money | None = None
    unknowns: tuple[str, ...] = Field(default=(), max_length=100)
    approved_contribution_threshold: Money | None = None  # owner-approved thresholds only
    is_fixture: bool = False
    is_canary: bool = False

    @field_validator("first_seen_at")
    @classmethod
    def _t(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _figures(self) -> EvaluationCandidate:
        figures = (self.conservative_contribution, self.base_contribution)
        if self.valuation_state not in _COMPLETE_VALUATION and any(f is not None for f in figures):
            raise ValueError("only an estimated/quote_supported valuation carries contribution figures")
        if self.valuation_state in _COMPLETE_VALUATION and any(f is None for f in figures):
            raise ValueError("a complete valuation carries conservative and base contributions")
        return self

    @property
    def synthetic(self) -> bool:
        return self.is_fixture or self.is_canary

    @property
    def vehicle_key(self) -> UUID:
        """Deduplication key: one physical vehicle counts once across sites."""
        return self.vehicle_cluster_id or self.candidate_id

    @property
    def eligible_primary(self) -> bool:
        return self.eligibility == EligibilityState.ELIGIBLE_PRIMARY

    @property
    def well_matched(self) -> bool:
        return self.eligible_primary and self.comparable_status in ("adequate", "small_sample")

    @property
    def valuation_complete(self) -> bool:
        return self.valuation_state in _COMPLETE_VALUATION


class EvaluationInquiry(BaseModel):
    model_config = _FROZEN

    inquiry_id: UUID
    state: InquiryState
    created_at: datetime
    vehicle_cluster_id: UUID | None = None
    candidate_id: UUID | None = None
    suppression_reason: SuppressionReason | None = None
    is_canary: bool = False

    @field_validator("created_at")
    @classmethod
    def _t(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class EvaluationReply(BaseModel):
    model_config = _FROZEN

    reply_id: UUID
    inquiry_id: UUID
    message_type: ReplyMessageType
    received_at: datetime
    matched: bool = True  # quarantined possible matches are not replies until verified
    is_canary: bool = False

    @field_validator("received_at")
    @classmethod
    def _t(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class DocumentResolution(BaseModel):
    """A previously missing document (CoC, registration papers, ...) that became available."""

    model_config = _FROZEN

    candidate_id: UUID
    document: str = Field(min_length=1, max_length=60)
    resolved_at: datetime
    is_canary: bool = False

    @field_validator("resolved_at")
    @classmethod
    def _t(cls, value: datetime) -> datetime:
        return ensure_utc(value)


# =============================================================================================
# Report
# =============================================================================================


class EvaluationOutcome(StrEnum):
    COVERAGE_NOT_ESTABLISHED = "coverage_not_established"
    DEAL_FOUND = "deal_found"
    CANDIDATES_NEED_OWNER_JUDGEMENT = "candidates_need_owner_judgement"
    NO_SUITABLE_DEAL_YET = "no_suitable_deal_yet"
    NO_SUITABLE_DEAL = "no_suitable_deal"


class InquiryCounts(BaseModel):
    model_config = _FROZEN

    attempted: int = 0  # accepted + uncertain + failed_definite
    accepted: int = 0  # provider accepted; not proof of delivery or reading
    uncertain: int = 0
    failed_definite: int = 0
    suppressed: int = 0
    held_for_facts: int = 0
    cancelled: int = 0
    in_progress: int = 0
    with_seller_reply: int = 0
    suppression_reasons: tuple[tuple[str, int], ...] = ()


class EconomicsSummary(BaseModel):
    """Best *supported* economics: a research estimate, never a confirmed profit."""

    model_config = _FROZEN

    candidate_id: UUID
    vehicle_cluster_id: UUID | None
    valuation_state: ValuationState
    conservative_contribution: Money
    base_contribution: Money
    meets_approved_threshold: bool | None  # None: no owner-approved threshold
    unknowns: tuple[str, ...]
    label: str = CONTRIBUTION_LABEL


class EvaluationReport(BaseModel):
    model_config = _FROZEN

    version: str = EVALUATION_VERSION
    generated_at: datetime
    window_status: Literal["not_started", "in_progress", "complete"]
    window_start: datetime | None
    window_end: datetime | None
    coverage: tuple[SourceCoverage, ...]
    sources_with_healthy_coverage: int
    eligible_vehicles: int
    well_matched_vehicles: int
    small_sample_matches: int
    inquiries: InquiryCounts
    seller_replies: int
    auto_replies: int
    bounces: int
    delivery_notices: int
    missing_documents_resolved: int
    best_supported_economics: EconomicsSummary | None
    vehicles_with_incomplete_economics: int
    most_common_unknowns: tuple[tuple[str, int], ...]
    qualifying_deal_ids: tuple[UUID, ...]
    owner_judgement_candidate_ids: tuple[UUID, ...]
    outcome: EvaluationOutcome
    reasons: tuple[str, ...]
    excluded_synthetic_records: int
    optimises_for_volume: Literal[False] = False

    def summary_lines(self) -> tuple[str, ...]:
        """Plain, honest lines for the dashboard; checked by the owner-wording guard."""
        lines: list[str] = []
        if self.window_start is None or self.window_end is None:
            lines.append("Evaluation window not started: no activated source has shown usable coverage yet.")
        else:
            elapsed = min(self.generated_at, self.window_end) - self.window_start
            day = min(EVALUATION_WINDOW_DAYS, elapsed.days + 1)
            state = "complete" if self.window_status == "complete" else f"in progress, day {day} of 15"
            lines.append(
                f"Evaluation window {self.window_start:%Y-%m-%d} to {self.window_end:%Y-%m-%d} ({state})."
            )
        for cov in self.coverage:
            ratio = (
                "n/a"
                if cov.coverage_ratio is None
                else f"{(cov.coverage_ratio * 100).quantize(Decimal('0.1'))} %"
            )
            gap_reasons = sorted({g.reason for g in cov.gaps})
            lines.append(
                f"Coverage {cov.source_key}: healthy {ratio} of the elapsed window, {len(cov.gaps)} gap(s)"
                + (f" ({', '.join(gap_reasons)})" if gap_reasons else "")
                + "."
            )
        lines.append(
            f"Candidates: {self.eligible_vehicles} eligible vehicle(s), "
            f"{self.well_matched_vehicles} well matched "
            "to MK asking-price comparables (asking prices, not sale prices)."
        )
        inq = self.inquiries
        lines.append(
            f"Seller inquiries: {inq.attempted} attempted, "
            f"{inq.accepted} accepted by the provider (not proof of "
            f"delivery), {inq.uncertain} uncertain, {inq.suppressed} suppressed."
        )
        lines.append(
            f"Seller replies: {self.seller_replies}; "
            f"missing documents resolved: {self.missing_documents_resolved}."
        )
        best = self.best_supported_economics
        if best is None:
            unknowns = ", ".join(name for name, _ in self.most_common_unknowns[:3]) or "not recorded"
            lines.append(
                "Best supported economics: unknown - no candidate has a complete valuation "
                f"(most common unknowns: {unknowns})."
            )
        else:
            lines.append(
                f"Best supported economics ({best.label}, research estimate): conservative "
                f"{best.conservative_contribution.display()}, base {best.base_contribution.display()}."
            )
        lines.append(f"Outcome: {_OUTCOME_TEXT[self.outcome]}")
        lines.extend(f"- {reason}" for reason in self.reasons)
        lines.append("Volume is not a goal: there are no targets for listings, alerts or e-mails.")
        for line in lines:
            check_owner_wording(line)
        return tuple(lines)


_OUTCOME_TEXT: Final[dict[EvaluationOutcome, str]] = {
    EvaluationOutcome.COVERAGE_NOT_ESTABLISHED: "usable source coverage is not established yet.",
    EvaluationOutcome.DEAL_FOUND: (
        "a well-matched candidate meets the owner-approved threshold (research result)."
    ),
    EvaluationOutcome.CANDIDATES_NEED_OWNER_JUDGEMENT: (
        "well-matched candidate(s) show a positive supported contribution; "
        "no owner-approved threshold exists."
    ),
    EvaluationOutcome.NO_SUITABLE_DEAL_YET: "no suitable deal found so far in this window.",
    EvaluationOutcome.NO_SUITABLE_DEAL: "no suitable deal was found in this window.",
}


# =============================================================================================
# Computation
# =============================================================================================


def evaluation_window_start(
    activations: Sequence[SourceActivation], scans: Sequence[ScanRecord]
) -> datetime | None:
    """First complete healthy non-fixture scan finish at/after an activated source's activation."""
    starts: list[datetime] = []
    for activation in activations:
        if activation.is_fixture or activation.activated_at is None:
            continue
        finishes = [
            s.finished_at
            for s in scans
            if s.source_key == activation.source_key
            and not s.is_fixture
            and s.healthy_complete
            and s.finished_at is not None
            and s.finished_at >= activation.activated_at
        ]
        if finishes:
            starts.append(min(finishes))
    return min(starts) if starts else None


def _in_window(moment: datetime, start: datetime | None, end: datetime | None, now: datetime) -> bool:
    return start is not None and end is not None and start <= moment < end and moment <= now


def _inquiry_counts(inquiries: Sequence[EvaluationInquiry], replied: set[UUID]) -> InquiryCounts:
    accepted = sum(1 for i in inquiries if i.state in _ACCEPTED_STATES)
    uncertain = sum(1 for i in inquiries if i.state == InquiryState.UNCERTAIN)
    failed = sum(1 for i in inquiries if i.state == InquiryState.FAILED_DEFINITE)
    reasons = Counter(
        (i.suppression_reason.value if i.suppression_reason else "unspecified")
        for i in inquiries
        if i.state == InquiryState.SUPPRESSED
    )
    return InquiryCounts(
        attempted=accepted + uncertain + failed,
        accepted=accepted,
        uncertain=uncertain,
        failed_definite=failed,
        suppressed=sum(reasons.values()),
        held_for_facts=sum(1 for i in inquiries if i.state == InquiryState.HELD_FACTS),
        cancelled=sum(1 for i in inquiries if i.state == InquiryState.CANCELLED),
        in_progress=sum(1 for i in inquiries if i.state in _IN_PROGRESS_STATES),
        with_seller_reply=sum(1 for i in inquiries if i.inquiry_id in replied),
        suppression_reasons=tuple(sorted(reasons.items())),
    )


def _meets_threshold(candidate: EvaluationCandidate) -> bool | None:
    threshold = candidate.approved_contribution_threshold
    conservative = candidate.conservative_contribution
    if threshold is None or conservative is None:
        return None
    if threshold.currency != conservative.currency:
        return None
    return conservative >= threshold


def _one_per_vehicle(candidates: Iterable[EvaluationCandidate]) -> list[UUID]:
    """One representative candidate id per physical vehicle (the same car on three sites is one
    deal): the best conservative contribution, ties broken by candidate id."""
    best: dict[UUID, EvaluationCandidate] = {}
    for candidate in candidates:
        current = best.get(candidate.vehicle_key)
        if current is None or _economics_key(candidate) > _economics_key(current):
            best[candidate.vehicle_key] = candidate
    return sorted((c.candidate_id for c in best.values()), key=str)


def _economics_key(candidate: EvaluationCandidate) -> tuple[Decimal, Decimal, str]:
    conservative = candidate.conservative_contribution
    base = candidate.base_contribution
    return (
        conservative.amount if conservative is not None else Decimal("-Infinity"),
        base.amount if base is not None else Decimal("-Infinity"),
        str(candidate.candidate_id),
    )


def _best_economics(candidates: Sequence[EvaluationCandidate]) -> EconomicsSummary | None:
    complete = [
        c
        for c in candidates
        if c.eligible_primary
        and c.valuation_complete
        and c.conservative_contribution is not None
        and c.base_contribution is not None
        and c.conservative_contribution.currency == "EUR"
        and c.base_contribution.currency == "EUR"
    ]
    if not complete:
        return None
    best = max(complete, key=_economics_key)
    assert best.valuation_state is not None
    assert best.conservative_contribution is not None and best.base_contribution is not None
    return EconomicsSummary(
        candidate_id=best.candidate_id,
        vehicle_cluster_id=best.vehicle_cluster_id,
        valuation_state=best.valuation_state,
        conservative_contribution=best.conservative_contribution,
        base_contribution=best.base_contribution,
        meets_approved_threshold=_meets_threshold(best),
        unknowns=best.unknowns,
    )


def build_evaluation_report(
    *,
    now: datetime,
    activations: Sequence[SourceActivation],
    scans: Sequence[ScanRecord],
    candidates: Sequence[EvaluationCandidate] = (),
    inquiries: Sequence[EvaluationInquiry] = (),
    replies: Sequence[EvaluationReply] = (),
    document_resolutions: Sequence[DocumentResolution] = (),
    max_scan_gap: timedelta = DEFAULT_MAX_SCAN_GAP,
) -> EvaluationReport:
    """The 15-day quality evaluation report (see module docstring)."""
    current = _aware(now)
    start = evaluation_window_start(activations, scans)
    end = start + EVALUATION_WINDOW if start is not None else None
    if start is None:
        window_status: Literal["not_started", "in_progress", "complete"] = "not_started"
    elif end is not None and current >= end:
        window_status = "complete"
    else:
        window_status = "in_progress"

    # Synthetic (fixture/canary) records - and anything linked to them - never count.
    synthetic_candidates = {c.candidate_id for c in candidates if c.synthetic}
    synthetic_clusters = {
        c.vehicle_cluster_id for c in candidates if c.synthetic and c.vehicle_cluster_id is not None
    }

    def synthetic_inquiry(item: EvaluationInquiry) -> bool:
        return (
            item.is_canary
            or (item.candidate_id is not None and item.candidate_id in synthetic_candidates)
            or (item.vehicle_cluster_id is not None and item.vehicle_cluster_id in synthetic_clusters)
        )

    canary_inquiries = {i.inquiry_id for i in inquiries if synthetic_inquiry(i)}
    synthetic_reply = [r for r in replies if r.is_canary or r.inquiry_id in canary_inquiries]
    synthetic_docs = [
        d for d in document_resolutions if d.is_canary or d.candidate_id in synthetic_candidates
    ]
    synthetic = len(synthetic_candidates) + len(canary_inquiries) + len(synthetic_reply) + len(synthetic_docs)
    real_candidates = [
        c
        for c in candidates
        if not c.synthetic
        and (c.vehicle_cluster_id is None or c.vehicle_cluster_id not in synthetic_clusters)
        and _in_window(c.first_seen_at, start, end, current)
    ]
    real_inquiries = [
        i for i in inquiries if not synthetic_inquiry(i) and _in_window(i.created_at, start, end, current)
    ]
    real_replies = [
        r
        for r in replies
        if not r.is_canary
        and r.inquiry_id not in canary_inquiries
        and _in_window(r.received_at, start, end, current)
    ]
    real_docs = [
        d
        for d in document_resolutions
        if not d.is_canary
        and d.candidate_id not in synthetic_candidates
        and _in_window(d.resolved_at, start, end, current)
    ]

    coverage: tuple[SourceCoverage, ...] = ()
    if start is not None and end is not None:
        activated = {a.source_key for a in activations if a.activated_at is not None and not a.is_fixture}
        coverage = healthy_coverage(
            [s for s in scans if s.source_key in activated],
            window_start=start,
            window_end=min(end, max(current, start)),
            max_gap=max_scan_gap,
        )

    eligible = {c.vehicle_key for c in real_candidates if c.eligible_primary}
    well_matched = {c.vehicle_key for c in real_candidates if c.well_matched}
    small_sample = {
        c.vehicle_key for c in real_candidates if c.well_matched and c.comparable_status == "small_sample"
    }
    matched_replies = [r for r in real_replies if r.matched]
    seller_replies = [r for r in matched_replies if r.message_type == ReplyMessageType.SELLER_REPLY]
    replied_inquiries = {r.inquiry_id for r in seller_replies}

    well = [c for c in real_candidates if c.well_matched]
    incomplete = {c.vehicle_key for c in well if not c.valuation_complete}
    unknown_counter = Counter(u for c in well if not c.valuation_complete for u in dict.fromkeys(c.unknowns))
    qualifying = _one_per_vehicle(c for c in well if c.valuation_complete and _meets_threshold(c) is True)
    judgement = _one_per_vehicle(
        c
        for c in well
        if c.valuation_complete
        and c.approved_contribution_threshold is None
        and c.conservative_contribution is not None
        and c.conservative_contribution.amount > 0
    )
    best = _best_economics(real_candidates)

    reasons: list[str] = []
    if start is None:
        outcome = EvaluationOutcome.COVERAGE_NOT_ESTABLISHED
        if not any(a.activated_at is not None and not a.is_fixture for a in activations):
            reasons.append("no source has been activated with usable coverage")
        else:
            reasons.append("no complete, healthy scan of an activated source has finished since activation")
    elif qualifying:
        outcome = EvaluationOutcome.DEAL_FOUND
    elif judgement:
        outcome = EvaluationOutcome.CANDIDATES_NEED_OWNER_JUDGEMENT
        reasons.append(
            "the contribution threshold is not owner-approved; the owner decides whether these are useful"
        )
    else:
        outcome = (
            EvaluationOutcome.NO_SUITABLE_DEAL
            if window_status == "complete"
            else EvaluationOutcome.NO_SUITABLE_DEAL_YET
        )
        reasons.extend(
            _no_deal_reasons(
                coverage=coverage,
                eligible=len(eligible),
                well_matched=len(well_matched),
                incomplete=len(incomplete),
                well=well,
            )
        )
    if (
        start is not None
        and real_inquiries
        and any(i.state == InquiryState.UNCERTAIN for i in real_inquiries)
    ):
        reasons.append(
            "some inquiry send outcomes are uncertain and await reconciliation (never resent blindly)"
        )

    return EvaluationReport(
        generated_at=current,
        window_status=window_status,
        window_start=start,
        window_end=end,
        coverage=coverage,
        sources_with_healthy_coverage=sum(1 for c in coverage if c.has_coverage),
        eligible_vehicles=len(eligible),
        well_matched_vehicles=len(well_matched),
        small_sample_matches=len(small_sample),
        inquiries=_inquiry_counts(real_inquiries, replied_inquiries),
        seller_replies=len(seller_replies),
        auto_replies=sum(1 for r in matched_replies if r.message_type == ReplyMessageType.AUTO_REPLY),
        bounces=sum(1 for r in matched_replies if r.message_type == ReplyMessageType.BOUNCE),
        delivery_notices=sum(
            1 for r in matched_replies if r.message_type == ReplyMessageType.DELIVERY_NOTICE
        ),
        missing_documents_resolved=len({(d.candidate_id, d.document.strip().lower()) for d in real_docs}),
        best_supported_economics=best,
        vehicles_with_incomplete_economics=len(incomplete),
        most_common_unknowns=tuple(sorted(unknown_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:10]),
        qualifying_deal_ids=tuple(qualifying),
        owner_judgement_candidate_ids=tuple(judgement),
        outcome=outcome,
        reasons=tuple(reasons),
        excluded_synthetic_records=synthetic,
    )


def _no_deal_reasons(
    *,
    coverage: Sequence[SourceCoverage],
    eligible: int,
    well_matched: int,
    incomplete: int,
    well: Sequence[EvaluationCandidate],
) -> list[str]:
    reasons: list[str] = []
    gap_seconds = sum(int(g.duration.total_seconds()) for cov in coverage for g in cov.gaps)
    if gap_seconds:
        reasons.append(
            f"coverage gaps total {gap_seconds // 3600} h {gap_seconds % 3600 // 60} min in the window"
        )
    if eligible == 0:
        reasons.append("no vehicle passed the primary price/mileage/SUV rules in the window")
    elif well_matched == 0:
        reasons.append(f"{eligible} eligible vehicle(s), none with adequate MK asking-price comparables")
    if incomplete:
        reasons.append(
            f"{incomplete} well-matched vehicle(s) have incomplete economics (unknown costs stay unknown)"
        )
    complete = [c for c in well if c.valuation_complete]
    non_positive = [
        c
        for c in complete
        if c.conservative_contribution is not None and c.conservative_contribution.amount <= 0
    ]
    below = [c for c in complete if _meets_threshold(c) is False]
    if non_positive:
        reasons.append(
            f"{len(non_positive)} complete valuation(s) show no positive conservative contribution"
        )
    if below:
        reasons.append(f"{len(below)} complete valuation(s) are below the owner-approved threshold")
    if not reasons:
        reasons.append("no candidate met the quality criteria")
    return reasons


__all__ = [
    "DEFAULT_MAX_SCAN_GAP",
    "EVALUATION_VERSION",
    "EVALUATION_WINDOW",
    "EVALUATION_WINDOW_DAYS",
    "DocumentResolution",
    "EconomicsSummary",
    "EvaluationCandidate",
    "EvaluationInquiry",
    "EvaluationOutcome",
    "EvaluationReply",
    "EvaluationReport",
    "InquiryCounts",
    "SourceActivation",
    "build_evaluation_report",
    "evaluation_window_start",
]
