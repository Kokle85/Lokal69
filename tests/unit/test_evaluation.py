"""Unit tests for domain.evaluation (spec 3 quality goal, 37.9, 37.10: zero deals honestly; canary excluded).

All sources, candidates, inquiries and replies are SYNTHETIC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from suv_deals.domain.enums import (
    Completeness,
    EligibilityState,
    InquiryState,
    ReplyMessageType,
    SuppressionReason,
    ValuationState,
)
from suv_deals.domain.evaluation import (
    EVALUATION_WINDOW,
    DocumentResolution,
    EvaluationCandidate,
    EvaluationInquiry,
    EvaluationOutcome,
    EvaluationReply,
    SourceActivation,
    build_evaluation_report,
    evaluation_window_start,
)
from suv_deals.domain.lifecycle import ScanRecord, SourceHealth
from suv_deals.domain.money import Money

T0 = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
SOURCE = "fixture_dealer_de"
CLUSTER = UUID("22222222-2222-4222-8222-222222222222")


def scan(minutes: float, *, source: str = SOURCE, ok: bool = True, fixture: bool = False) -> ScanRecord:
    start = T0 + timedelta(minutes=minutes)
    return ScanRecord(
        scan_id=uuid4(),
        source_key=source,
        started_at=start,
        finished_at=start + timedelta(minutes=5),
        completeness=Completeness.COMPLETE if ok else Completeness.FAILED,
        filter_fingerprint="f",
        health=SourceHealth.HEALTHY,
        is_fixture=fixture,
    )


def regular_scans(hours: int, *, every_minutes: int = 30, source: str = SOURCE) -> list[ScanRecord]:
    return [scan(m, source=source) for m in range(0, hours * 60, every_minutes)]


ACTIVE = [SourceActivation(source_key=SOURCE, activated_at=T0)]


def candidate(**overrides: Any) -> EvaluationCandidate:
    data: dict[str, Any] = {
        "candidate_id": uuid4(),
        "first_seen_at": T0 + timedelta(hours=1),
        "eligibility": EligibilityState.ELIGIBLE_PRIMARY,
        "comparable_status": "adequate",
    }
    data.update(overrides)
    return EvaluationCandidate(**data)


def valued(conservative: str, base: str, **overrides: Any) -> EvaluationCandidate:
    return candidate(
        valuation_state=ValuationState.ESTIMATED,
        conservative_contribution=Money.of(conservative, "EUR"),
        base_contribution=Money.of(base, "EUR"),
        **overrides,
    )


class TestWindow:
    def test_not_started_without_activation_even_with_scans(self) -> None:
        report = build_evaluation_report(now=T0 + timedelta(days=2), activations=[], scans=regular_scans(10))
        assert report.window_status == "not_started"
        assert report.outcome == EvaluationOutcome.COVERAGE_NOT_ESTABLISHED
        assert report.window_start is None and report.coverage == ()
        assert "no source has been activated" in report.reasons[0]
        assert "not started" in report.summary_lines()[0]

    def test_activation_without_a_healthy_scan_does_not_start_the_window(self) -> None:
        report = build_evaluation_report(
            now=T0 + timedelta(days=1), activations=ACTIVE, scans=[scan(0, ok=False), scan(30, ok=False)]
        )
        assert report.outcome == EvaluationOutcome.COVERAGE_NOT_ESTABLISHED
        assert "no complete, healthy scan" in report.reasons[0]

    def test_fixture_sources_never_start_the_window(self) -> None:
        activations = [SourceActivation(source_key="fixture_src", activated_at=T0, is_fixture=True)]
        assert evaluation_window_start(activations, [scan(0, source="fixture_src", fixture=True)]) is None
        assert evaluation_window_start(ACTIVE, [scan(0, fixture=True)]) is None

    def test_window_starts_at_first_healthy_scan_after_activation(self) -> None:
        activations = [
            SourceActivation(source_key=SOURCE, activated_at=T0 + timedelta(hours=1)),
            SourceActivation(source_key="later_source", activated_at=T0 + timedelta(hours=5)),
            SourceActivation(source_key="never", activated_at=None),
        ]
        scans = [scan(0), scan(65), scan(310, source="later_source")]
        start = evaluation_window_start(activations, scans)
        # The start of the first complete healthy scan after activation (it finished at +70 min):
        # the vehicles that scan discovers are inside the window.
        assert start == T0 + timedelta(minutes=65)
        report = build_evaluation_report(now=T0 + timedelta(days=3), activations=activations, scans=scans)
        assert report.window_start == start
        assert report.window_end == start + EVALUATION_WINDOW
        assert report.window_status == "in_progress"
        assert {c.source_key for c in report.coverage} == {SOURCE, "later_source"}

    def test_window_completes_after_15_days(self) -> None:
        report = build_evaluation_report(now=T0 + timedelta(days=16), activations=ACTIVE, scans=[scan(0)])
        assert report.window_status == "complete"
        assert "complete" in report.summary_lines()[0]


class TestHonestZeroDeals:
    def test_zero_suitable_deals_is_reported_honestly(self) -> None:
        candidates = [
            candidate(eligibility=EligibilityState.REJECTED),
            candidate(comparable_status="insufficient_comparables"),
            candidate(unknowns=("import_duty", "transport"), valuation_state=ValuationState.INCOMPLETE),
            valued("-300", "200"),
        ]
        report = build_evaluation_report(
            now=T0 + timedelta(days=16),
            activations=ACTIVE,
            scans=regular_scans(24),
            candidates=candidates,
        )
        assert report.outcome == EvaluationOutcome.NO_SUITABLE_DEAL
        assert report.qualifying_deal_ids == () and report.owner_judgement_candidate_ids == ()
        assert report.eligible_vehicles == 3
        assert report.well_matched_vehicles == 2
        assert report.vehicles_with_incomplete_economics == 1
        assert report.most_common_unknowns == (("import_duty", 1), ("transport", 1))
        assert any("incomplete economics" in r for r in report.reasons)
        assert any("no positive conservative contribution" in r for r in report.reasons)
        assert any("coverage gaps" in r for r in report.reasons)  # the window ran past the last scan
        best = report.best_supported_economics
        assert best is not None and best.conservative_contribution == Money.of("-300", "EUR")
        lines = report.summary_lines()
        assert any("no suitable deal was found" in line for line in lines)
        assert any("Volume is not a goal" in line for line in lines)
        assert report.optimises_for_volume is False

    def test_nothing_eligible_and_in_progress(self) -> None:
        report = build_evaluation_report(
            now=T0 + timedelta(hours=10), activations=ACTIVE, scans=regular_scans(10)
        )
        assert report.outcome == EvaluationOutcome.NO_SUITABLE_DEAL_YET
        assert "no vehicle passed the primary price/mileage/SUV rules in the window" in report.reasons
        assert report.best_supported_economics is None
        assert any(
            "unknown - no candidate has a complete valuation" in line for line in report.summary_lines()
        )

    def test_eligible_but_no_comparables(self) -> None:
        report = build_evaluation_report(
            now=T0 + timedelta(hours=10),
            activations=ACTIVE,
            scans=regular_scans(10),
            candidates=[candidate(comparable_status="insufficient_comparables")],
        )
        assert any("none with adequate MK asking-price comparables" in r for r in report.reasons)


class TestDealsAndEconomics:
    def test_owner_judgement_without_an_approved_threshold(self) -> None:
        good = valued("900", "1800")
        report = build_evaluation_report(
            now=T0 + timedelta(days=2), activations=ACTIVE, scans=regular_scans(10), candidates=[good]
        )
        assert report.outcome == EvaluationOutcome.CANDIDATES_NEED_OWNER_JUDGEMENT
        assert report.owner_judgement_candidate_ids == (good.candidate_id,)
        assert report.best_supported_economics is not None
        assert report.best_supported_economics.meets_approved_threshold is None
        assert "estimated contribution before business tax" in report.best_supported_economics.label

    def test_deal_found_only_against_an_owner_approved_threshold(self) -> None:
        threshold = Money.of("1500", "EUR")
        winner = valued("1600", "2400", approved_contribution_threshold=threshold)
        below = valued("1000", "2000", approved_contribution_threshold=threshold)
        report = build_evaluation_report(
            now=T0 + timedelta(days=2),
            activations=ACTIVE,
            scans=regular_scans(10),
            candidates=[winner, below],
        )
        assert report.outcome == EvaluationOutcome.DEAL_FOUND
        assert report.qualifying_deal_ids == (winner.candidate_id,)
        best = report.best_supported_economics
        assert best is not None and best.candidate_id == winner.candidate_id and best.meets_approved_threshold
        only_below = build_evaluation_report(
            now=T0 + timedelta(days=16), activations=ACTIVE, scans=regular_scans(10), candidates=[below]
        )
        assert only_below.outcome == EvaluationOutcome.NO_SUITABLE_DEAL
        assert any("below the owner-approved threshold" in r for r in only_below.reasons)

    def test_best_economics_ignores_incomplete_and_non_eur(self) -> None:
        chf = candidate(
            valuation_state=ValuationState.ESTIMATED,
            conservative_contribution=Money.of("5000", "CHF"),
            base_contribution=Money.of("6000", "CHF"),
        )
        eur = valued("100", "500")
        report = build_evaluation_report(
            now=T0 + timedelta(days=2), activations=ACTIVE, scans=regular_scans(10), candidates=[chf, eur]
        )
        assert report.best_supported_economics is not None
        assert report.best_supported_economics.candidate_id == eur.candidate_id

    def test_unknown_economics_never_carry_figures(self) -> None:
        with pytest.raises(ValueError):
            candidate(
                valuation_state=ValuationState.INCOMPLETE, conservative_contribution=Money.of("1", "EUR")
            )
        with pytest.raises(ValueError):
            candidate(valuation_state=ValuationState.ESTIMATED)

    def test_same_vehicle_on_three_sites_counts_once(self) -> None:
        candidates = [candidate(vehicle_cluster_id=CLUSTER) for _ in range(3)]
        report = build_evaluation_report(
            now=T0 + timedelta(days=2), activations=ACTIVE, scans=regular_scans(10), candidates=candidates
        )
        assert report.eligible_vehicles == 1 and report.well_matched_vehicles == 1

    def test_same_qualifying_vehicle_on_three_sites_is_one_deal(self) -> None:
        threshold = Money.of("1500", "EUR")
        copies = [
            valued(amount, "2500", vehicle_cluster_id=CLUSTER, approved_contribution_threshold=threshold)
            for amount in ("1600", "1700", "1650")
        ]
        report = build_evaluation_report(
            now=T0 + timedelta(days=2), activations=ACTIVE, scans=regular_scans(10), candidates=copies
        )
        assert report.outcome == EvaluationOutcome.DEAL_FOUND
        assert report.qualifying_deal_ids == (copies[1].candidate_id,)  # best supported copy
        judgement = build_evaluation_report(
            now=T0 + timedelta(days=2),
            activations=ACTIVE,
            scans=regular_scans(10),
            candidates=[valued("900", "1800", vehicle_cluster_id=CLUSTER) for _ in range(3)],
        )
        assert len(judgement.owner_judgement_candidate_ids) == 1

    def test_contribution_in_another_currency_than_the_threshold_is_not_a_deal(self) -> None:
        chf = candidate(
            valuation_state=ValuationState.ESTIMATED,
            conservative_contribution=Money.of("5000", "CHF"),
            base_contribution=Money.of("6000", "CHF"),
            approved_contribution_threshold=Money.of("1500", "EUR"),
        )
        report = build_evaluation_report(
            now=T0 + timedelta(days=16), activations=ACTIVE, scans=regular_scans(10), candidates=[chf]
        )
        assert report.qualifying_deal_ids == ()
        assert report.outcome == EvaluationOutcome.NO_SUITABLE_DEAL

    def test_records_outside_the_window_are_excluded(self) -> None:
        before = candidate(first_seen_at=T0 - timedelta(days=1))
        after = candidate(first_seen_at=T0 + timedelta(days=20))
        report = build_evaluation_report(
            now=T0 + timedelta(days=30), activations=ACTIVE, scans=[scan(0)], candidates=[before, after]
        )
        assert report.eligible_vehicles == 0


class TestCanaryExclusion:
    def test_synthetic_canary_never_counts_as_deal_inquiry_or_reply(self) -> None:
        threshold = Money.of("1500", "EUR")
        canary_candidate = valued("9000", "9500", approved_contribution_threshold=threshold, is_canary=True)
        fixture_candidate = valued("9000", "9500", approved_contribution_threshold=threshold, is_fixture=True)
        canary_inquiry = EvaluationInquiry(
            inquiry_id=uuid4(), state=InquiryState.REPLIED, created_at=T0 + timedelta(hours=2), is_canary=True
        )
        canary_reply = EvaluationReply(
            reply_id=uuid4(),
            inquiry_id=canary_inquiry.inquiry_id,
            message_type=ReplyMessageType.SELLER_REPLY,
            received_at=T0 + timedelta(hours=3),
        )
        canary_doc = DocumentResolution(
            candidate_id=canary_candidate.candidate_id,
            document="coc",
            resolved_at=T0 + timedelta(hours=4),
            is_canary=True,
        )
        report = build_evaluation_report(
            now=T0 + timedelta(days=16),
            activations=ACTIVE,
            scans=regular_scans(10),
            candidates=[canary_candidate, fixture_candidate],
            inquiries=[canary_inquiry],
            replies=[canary_reply],
            document_resolutions=[canary_doc],
        )
        assert report.outcome == EvaluationOutcome.NO_SUITABLE_DEAL
        assert report.qualifying_deal_ids == ()
        assert report.eligible_vehicles == 0
        assert report.inquiries.attempted == 0 and report.inquiries.accepted == 0
        assert report.seller_replies == 0  # a reply to a canary inquiry is not a real reply either
        assert report.missing_documents_resolved == 0
        assert report.best_supported_economics is None
        # canary + fixture candidate, canary inquiry, the reply to it and the canary document
        assert report.excluded_synthetic_records == 5
        assert all("9.000" not in line and "9000" not in line for line in report.summary_lines())

    def test_records_linked_to_a_synthetic_candidate_never_count(self) -> None:
        canary = valued("9000", "9500", vehicle_cluster_id=CLUSTER, is_canary=True)
        same_vehicle_real_looking = valued("9000", "9500", vehicle_cluster_id=CLUSTER)
        inquiry_by_candidate = EvaluationInquiry(
            inquiry_id=uuid4(),
            state=InquiryState.ACCEPTED,
            created_at=T0 + timedelta(hours=2),
            candidate_id=canary.candidate_id,
        )
        inquiry_by_cluster = EvaluationInquiry(
            inquiry_id=uuid4(),
            state=InquiryState.REPLIED,
            created_at=T0 + timedelta(hours=2),
            vehicle_cluster_id=CLUSTER,
        )
        reply = EvaluationReply(
            reply_id=uuid4(),
            inquiry_id=inquiry_by_cluster.inquiry_id,
            message_type=ReplyMessageType.SELLER_REPLY,
            received_at=T0 + timedelta(hours=3),
        )
        doc = DocumentResolution(
            candidate_id=canary.candidate_id, document="coc", resolved_at=T0 + timedelta(hours=4)
        )
        report = build_evaluation_report(
            now=T0 + timedelta(days=16),
            activations=ACTIVE,
            scans=regular_scans(10),
            candidates=[canary, same_vehicle_real_looking],
            inquiries=[inquiry_by_candidate, inquiry_by_cluster],
            replies=[reply],
            document_resolutions=[doc],
        )
        assert report.eligible_vehicles == 0
        assert report.owner_judgement_candidate_ids == () and report.qualifying_deal_ids == ()
        assert report.inquiries.attempted == 0 and report.seller_replies == 0
        assert report.missing_documents_resolved == 0
        assert report.outcome == EvaluationOutcome.NO_SUITABLE_DEAL


class TestInquiriesRepliesDocuments:
    def test_counts_and_semantics(self) -> None:
        t = T0 + timedelta(hours=2)
        inquiries = [
            EvaluationInquiry(inquiry_id=uuid4(), state=InquiryState.ACCEPTED, created_at=t),
            EvaluationInquiry(inquiry_id=uuid4(), state=InquiryState.REPLIED, created_at=t),
            EvaluationInquiry(inquiry_id=uuid4(), state=InquiryState.BOUNCED, created_at=t),
            EvaluationInquiry(inquiry_id=uuid4(), state=InquiryState.UNCERTAIN, created_at=t),
            EvaluationInquiry(inquiry_id=uuid4(), state=InquiryState.FAILED_DEFINITE, created_at=t),
            EvaluationInquiry(
                inquiry_id=uuid4(),
                state=InquiryState.SUPPRESSED,
                created_at=t,
                suppression_reason=SuppressionReason.CONTRADICTORY_AVAILABILITY,
            ),
            EvaluationInquiry(inquiry_id=uuid4(), state=InquiryState.HELD_FACTS, created_at=t),
            EvaluationInquiry(inquiry_id=uuid4(), state=InquiryState.QUEUED, created_at=t),
            EvaluationInquiry(inquiry_id=uuid4(), state=InquiryState.CANCELLED, created_at=t),
        ]
        replied = inquiries[1].inquiry_id
        replies = [
            EvaluationReply(
                reply_id=uuid4(),
                inquiry_id=replied,
                message_type=ReplyMessageType.SELLER_REPLY,
                received_at=t,
            ),
            EvaluationReply(
                reply_id=uuid4(), inquiry_id=replied, message_type=ReplyMessageType.AUTO_REPLY, received_at=t
            ),
            EvaluationReply(
                reply_id=uuid4(),
                inquiry_id=inquiries[2].inquiry_id,
                message_type=ReplyMessageType.BOUNCE,
                received_at=t,
            ),
            EvaluationReply(
                reply_id=uuid4(),
                inquiry_id=inquiries[0].inquiry_id,
                message_type=ReplyMessageType.SELLER_REPLY,
                received_at=t,
                matched=False,
            ),
        ]
        cid = uuid4()
        docs = [
            DocumentResolution(candidate_id=cid, document="CoC", resolved_at=t),
            DocumentResolution(candidate_id=cid, document="coc", resolved_at=t + timedelta(hours=1)),
            DocumentResolution(candidate_id=cid, document="registration", resolved_at=t),
        ]
        report = build_evaluation_report(
            now=T0 + timedelta(days=2),
            activations=ACTIVE,
            scans=regular_scans(10),
            inquiries=inquiries,
            replies=replies,
            document_resolutions=docs,
        )
        counts = report.inquiries
        assert (counts.accepted, counts.uncertain, counts.failed_definite, counts.attempted) == (3, 1, 1, 5)
        assert (counts.suppressed, counts.held_for_facts, counts.in_progress, counts.cancelled) == (
            1,
            1,
            1,
            1,
        )
        assert counts.suppression_reasons == (("contradictory_availability", 1),)
        assert counts.with_seller_reply == 1
        assert report.seller_replies == 1  # quarantined (unmatched) replies and auto-replies are not replies
        assert report.auto_replies == 1 and report.bounces == 1
        assert report.missing_documents_resolved == 2
        assert any("uncertain" in r for r in report.reasons)
        assert any(
            "accepted by the provider (not proof of delivery)" in line for line in report.summary_lines()
        )

    def test_summary_lines_pass_the_owner_wording_guard(self) -> None:
        report = build_evaluation_report(
            now=T0 + timedelta(days=1, hours=23),
            activations=ACTIVE,
            scans=[*regular_scans(3), scan(400, ok=False)],
            candidates=[valued("900", "1800")],
        )
        lines = report.summary_lines()
        assert lines[0].startswith("Evaluation window 2026-10-06 to 2026-10-21 (in progress, day 2 of 15)")
        assert any(line.startswith(f"Coverage {SOURCE}: healthy") for line in lines)
        assert all("guarantee" not in line.lower() for line in lines)


class TestIndependentReviewRegressions:
    def test_threshold_in_another_currency_is_reported_not_silently_ignored(self) -> None:
        report = build_evaluation_report(
            now=T0 + EVALUATION_WINDOW + timedelta(hours=1),
            activations=ACTIVE,
            scans=regular_scans(3),
            candidates=[valued("1600", "2500", approved_contribution_threshold=Money.of("1500", "CHF"))],
        )
        assert report.outcome == EvaluationOutcome.NO_SUITABLE_DEAL
        assert report.qualifying_deal_ids == () and report.owner_judgement_candidate_ids == ()
        assert any("could not be compared with the owner-approved threshold" in r for r in report.reasons)
        assert all(r != "no candidate met the quality criteria" for r in report.reasons)
        report.summary_lines()  # passes the owner-wording guard


class TestThirdReviewRegressions:
    def test_vehicles_found_by_the_activating_scan_are_inside_the_window(self) -> None:
        # The first complete healthy scan runs 08:00-08:05 and finds a qualifying vehicle at 08:03.
        # A window opening only at the scan's finish dropped it and reported "no vehicle passed".
        found_during_first_scan = valued(
            "2000",
            "2600",
            first_seen_at=T0 + timedelta(minutes=3),
            approved_contribution_threshold=Money.of("1500", "EUR"),
        )
        report = build_evaluation_report(
            now=T0 + timedelta(days=2),
            activations=ACTIVE,
            scans=[scan(0)],
            candidates=[found_during_first_scan],
        )
        assert report.window_start == T0
        assert report.eligible_vehicles == 1 and report.well_matched_vehicles == 1
        assert report.outcome == EvaluationOutcome.DEAL_FOUND
        assert report.qualifying_deal_ids == (found_during_first_scan.candidate_id,)
        # The window never opens before the activation, even for a scan that started earlier.
        late_activation = [SourceActivation(source_key=SOURCE, activated_at=T0 + timedelta(minutes=2))]
        assert evaluation_window_start(late_activation, [scan(0)]) == T0 + timedelta(minutes=2)
        # A scan that finished before the activation does not open it at all.
        after_scan = [SourceActivation(source_key=SOURCE, activated_at=T0 + timedelta(minutes=6))]
        assert evaluation_window_start(after_scan, [scan(0)]) is None

    def test_real_listing_clustered_with_a_canary_is_excluded_and_counted(self) -> None:
        canary = valued("5000", "6000", vehicle_cluster_id=CLUSTER, is_canary=True)
        same_vehicle = valued("5000", "6000", vehicle_cluster_id=CLUSTER)
        inquiry = EvaluationInquiry(
            inquiry_id=uuid4(),
            state=InquiryState.ACCEPTED,
            created_at=T0 + timedelta(hours=2),
            candidate_id=same_vehicle.candidate_id,
        )
        document = DocumentResolution(
            candidate_id=same_vehicle.candidate_id, document="CoC", resolved_at=T0 + timedelta(hours=3)
        )
        report = build_evaluation_report(
            now=T0 + timedelta(days=1),
            activations=ACTIVE,
            scans=regular_scans(3),
            candidates=[canary, same_vehicle],
            inquiries=[inquiry],
            document_resolutions=[document],
        )
        assert report.eligible_vehicles == 0 and report.qualifying_deal_ids == ()
        assert report.inquiries.attempted == 0 and report.missing_documents_resolved == 0
        # canary + clustered listing + its inquiry + its document resolution
        assert report.excluded_synthetic_records == 4
