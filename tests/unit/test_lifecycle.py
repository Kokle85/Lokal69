"""Unit tests for domain.lifecycle (spec 37.9, 9, 10, 37.6; 37.10 delta tests).

All listings, scans, mailboxes and replies are SYNTHETIC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from suv_deals.domain.enums import (
    Availability,
    AvailabilityEvidenceKind,
    Completeness,
    Precision,
    SuppressionReason,
)
from suv_deals.domain.lifecycle import (
    INTERVAL_NOTE,
    AvailabilitySignal,
    AvailabilitySignalKind,
    LagStatus,
    LifecycleObservation,
    MailWorkerHeartbeat,
    ScanRecord,
    SellerAvailabilityStatement,
    SourceHealth,
    SourceListingLifecycle,
    TrustedSourceTime,
    advance_checkpoint,
    apply_observation,
    apply_scan_result,
    derive_availability_event,
    derive_cluster_lifecycle,
    detail_freshness,
    detect_availability_conflicts,
    detection_delay,
    healthy_coverage,
    is_new_today,
    mail_reply_detection_lag,
    mail_worker_coverage,
    notification_processing_lag,
    reconciliation_window,
    source_scan_lag,
)
from suv_deals.domain.listings import SourceTimestamp
from suv_deals.errors import ValidationFailed

T0 = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
LISTING = UUID("11111111-1111-4111-8111-111111111111")
LISTING_B = UUID("11111111-1111-4111-8111-111111111112")
LISTING_C = UUID("11111111-1111-4111-8111-111111111113")
CLUSTER = UUID("22222222-2222-4222-8222-222222222222")
REPLY = UUID("88888888-8888-4888-8888-888888888888")
MAILBOX = UUID("77777777-7777-4777-8777-777777777777")
FILTER = "filter-primary-v1"


def scan(
    minutes: int,
    *,
    duration: int = 5,
    completeness: Completeness = Completeness.COMPLETE,
    source: str = "fixture_dealer_de",
    filter_fp: str = FILTER,
    health: SourceHealth = SourceHealth.HEALTHY,
    incident: bool = False,
    fixture: bool = False,
    finished: bool = True,
) -> ScanRecord:
    start = T0 + timedelta(minutes=minutes)
    return ScanRecord(
        scan_id=uuid4(),
        source_key=source,
        started_at=start,
        finished_at=start + timedelta(minutes=duration) if finished else None,
        completeness=completeness,
        filter_fingerprint=filter_fp,
        parser_incident=incident,
        health=health,
        is_fixture=fixture,
    )


def listing(**overrides: Any) -> SourceListingLifecycle:
    data: dict[str, Any] = {
        "listing_id": LISTING,
        "source_key": "fixture_dealer_de",
        "vehicle_cluster_id": CLUSTER,
        "first_seen_at": T0,
        "last_seen_on_search_at": T0 + timedelta(minutes=30),
        "last_seen_filter_fingerprint": FILTER,
        "availability": Availability.AVAILABLE,
        "availability_evidence_kind": AvailabilityEvidenceKind.SOURCE_OBSERVATION,
        "availability_effective_at": T0,
        "source_health": SourceHealth.HEALTHY,
    }
    data.update(overrides)
    return SourceListingLifecycle(**data)


def absence(sc: ScanRecord) -> AvailabilitySignal:
    return AvailabilitySignal(
        kind=AvailabilitySignalKind.NOT_SEEN_IN_SCAN, observed_at=sc.finished_at or sc.started_at, scan=sc
    )


# =============================================================================================
# Per-source lifecycle
# =============================================================================================


class TestListingLifecycle:
    def test_first_seen_is_minimum_and_last_seen_is_greatest_out_of_order(self) -> None:
        state = listing(first_seen_at=T0 + timedelta(hours=1), last_seen_on_search_at=T0 + timedelta(hours=2))
        earlier = apply_observation(
            state, LifecycleObservation(kind="search_seen", observed_at=T0, filter_fingerprint="old-filter")
        )
        assert earlier.first_seen_at == T0
        assert earlier.last_seen_on_search_at == T0 + timedelta(hours=2)  # never regresses
        assert earlier.last_seen_filter_fingerprint == FILTER  # stale observation keeps the newer filter
        later = apply_observation(
            earlier,
            LifecycleObservation(
                kind="search_seen", observed_at=T0 + timedelta(hours=3), filter_fingerprint="f2"
            ),
        )
        assert later.last_seen_on_search_at == T0 + timedelta(hours=3)
        assert later.last_seen_filter_fingerprint == "f2"

    def test_detail_success_and_source_times(self) -> None:
        trusted = TrustedSourceTime(value=T0 - timedelta(days=3), trustworthy=True, precision=Precision.DAY)
        untrusted = TrustedSourceTime(
            value=T0 - timedelta(days=1), trustworthy=False, precision=Precision.DAY
        )
        state = apply_observation(
            listing(),
            LifecycleObservation(
                kind="detail_success", observed_at=T0 + timedelta(hours=1), source_published=trusted
            ),
        )
        assert state.last_detail_success_at == T0 + timedelta(hours=1)
        assert state.source_published == trusted
        kept = apply_observation(
            state,
            LifecycleObservation(
                kind="detail_success", observed_at=T0 + timedelta(hours=2), source_published=untrusted
            ),
        )
        assert kept.source_published == trusted  # an untrusted value never replaces a trusted one
        assert kept.latest_presence_at == T0 + timedelta(hours=2)

    def test_from_source_timestamp(self) -> None:
        stamp = SourceTimestamp(value=T0, raw="06.10.2026", zone_assumed=True, precision=Precision.DAY)
        assert TrustedSourceTime.from_source_timestamp(stamp, trustworthy=True).zone_assumed
        assert not TrustedSourceTime.from_source_timestamp(SourceTimestamp(), trustworthy=True).trustworthy
        with pytest.raises(ValueError):
            TrustedSourceTime(value=None, trustworthy=True)

    def test_validation(self) -> None:
        with pytest.raises(ValueError):
            listing(last_seen_on_search_at=T0 - timedelta(minutes=1))
        with pytest.raises(ValueError):
            LifecycleObservation(kind="search_seen", observed_at=T0)
        with pytest.raises(ValueError):
            ScanRecord(
                scan_id=uuid4(),
                source_key="x",
                started_at=T0,
                finished_at=T0 - timedelta(seconds=1),
                completeness=Completeness.COMPLETE,
                filter_fingerprint=FILTER,
            )

    def test_apply_scan_result(self) -> None:
        good = scan(60)
        state = apply_scan_result(listing(), good)
        assert state.last_complete_scan_at == good.finished_at
        partial = apply_scan_result(state, scan(120, completeness=Completeness.PARTIAL))
        assert partial.last_complete_scan_at == good.finished_at
        incident = apply_scan_result(state, scan(180, incident=True))
        assert incident.source_health == SourceHealth.PARSER_INCIDENT
        with pytest.raises(ValidationFailed):
            apply_scan_result(state, scan(60, source="other_source"))


# =============================================================================================
# Availability events (37.10: complete-scan absence -> unknown)
# =============================================================================================


class TestAvailabilityEvents:
    def test_complete_scan_absence_is_unknown_never_removed_or_sold(self) -> None:
        decision = derive_availability_event(listing(), absence(scan(60)))
        event = decision.event
        assert event is not None
        assert event.new_status == Availability.UNKNOWN
        assert event.reason == "not_seen_in_complete_scan"
        assert event.evidence_kind == AvailabilityEvidenceKind.COMPLETE_SCAN_ABSENCE
        assert event.new_status not in (Availability.REMOVED, Availability.SOLD_CLAIMED)
        assert event.establishes_purchase is False and event.transaction_price is None
        assert event.promote_current

    @pytest.mark.parametrize(
        ("kwargs", "reason_part"),
        [
            ({"completeness": Completeness.PARTIAL}, "partial"),
            ({"completeness": Completeness.FAILED}, "failed"),
            ({"completeness": Completeness.BUDGET_LIMITED}, "budget_limited"),
            ({"completeness": Completeness.BLOCKED}, "blocked"),
            ({"incident": True}, "parser incident"),
            ({"filter_fp": "changed-filter"}, "filter changed"),
            ({"health": SourceHealth.PAUSED}, "paused"),
            ({"health": SourceHealth.DEGRADED}, "degraded"),
            ({"finished": False}, "not finished"),
        ],
    )
    def test_failed_partial_filter_changes_and_incidents_produce_no_event(
        self, kwargs: dict[str, Any], reason_part: str
    ) -> None:
        decision = derive_availability_event(listing(), absence(scan(60, **kwargs)))
        assert decision.event is None
        assert decision.no_event_reason is not None and reason_part in decision.no_event_reason

    def test_scan_started_before_last_sighting_is_not_absence(self) -> None:
        decision = derive_availability_event(
            listing(last_seen_on_search_at=T0 + timedelta(hours=3)), absence(scan(60))
        )
        assert decision.event is None

    def test_absence_without_known_filter_or_on_non_active_status(self) -> None:
        assert (
            derive_availability_event(listing(last_seen_filter_fingerprint=None), absence(scan(60))).event
            is None
        )
        for status in (Availability.UNKNOWN, Availability.SOLD_CLAIMED, Availability.REMOVED):
            assert derive_availability_event(listing(availability=status), absence(scan(60))).event is None

    def test_explicit_sold_badge_removed_page_and_reserved_badge(self) -> None:
        sold = derive_availability_event(
            listing(),
            AvailabilitySignal(kind=AvailabilitySignalKind.SOLD_BADGE, observed_at=T0 + timedelta(hours=1)),
        ).event
        assert sold is not None
        assert (sold.new_status, sold.evidence_kind) == (
            Availability.SOLD_CLAIMED,
            AvailabilityEvidenceKind.SOURCE_SOLD_BADGE,
        )
        removed = derive_availability_event(
            listing(),
            AvailabilitySignal(kind=AvailabilitySignalKind.REMOVED_PAGE, observed_at=T0 + timedelta(hours=1)),
        ).event
        assert removed is not None
        assert (removed.new_status, removed.evidence_kind) == (
            Availability.REMOVED,
            AvailabilityEvidenceKind.SOURCE_REMOVED_PAGE,
        )
        reserved = derive_availability_event(
            listing(),
            AvailabilitySignal(
                kind=AvailabilitySignalKind.RESERVED_BADGE, observed_at=T0 + timedelta(hours=1)
            ),
        ).event
        assert reserved is not None and reserved.new_status == Availability.RESERVED

    def test_seller_statement_is_seller_reported_sold_only(self) -> None:
        signal = AvailabilitySignal(
            kind=AvailabilitySignalKind.SELLER_STATEMENT,
            observed_at=T0 + timedelta(hours=1),
            seller_status=Availability.SOLD_CLAIMED,
            reply_id=REPLY,
        )
        event = derive_availability_event(listing(), signal).event
        assert event is not None
        assert event.new_status == Availability.SOLD_CLAIMED
        assert event.evidence_kind == AvailabilityEvidenceKind.SELLER_REPORTED_SOLD
        assert event.reply_id == REPLY
        assert event.establishes_purchase is False and event.transaction_price is None

    def test_missing_or_inaccessible_pages_are_not_removals(self) -> None:
        missing = derive_availability_event(
            listing(), AvailabilitySignal(kind=AvailabilitySignalKind.DETAIL_NOT_FOUND, observed_at=T0)
        ).event
        assert missing is not None
        assert missing.new_status == Availability.UNKNOWN and missing.reason == "detail_not_found"
        assert missing.confidence == "low"
        inaccessible = derive_availability_event(
            listing(), AvailabilitySignal(kind=AvailabilitySignalKind.DETAIL_INACCESSIBLE, observed_at=T0)
        )
        assert inaccessible.event is None
        degraded = derive_availability_event(
            listing(),
            AvailabilitySignal(
                kind=AvailabilitySignalKind.DETAIL_NOT_FOUND,
                observed_at=T0,
                source_health=SourceHealth.DEGRADED,
            ),
        )
        assert degraded.event is None
        blocked = derive_availability_event(
            listing(),
            AvailabilitySignal(
                kind=AvailabilitySignalKind.SOLD_BADGE, observed_at=T0, source_health=SourceHealth.BLOCKED
            ),
        )
        assert blocked.event is None
        incident = derive_availability_event(
            listing(),
            AvailabilitySignal(kind=AvailabilitySignalKind.SOLD_BADGE, observed_at=T0, parser_incident=True),
        )
        assert incident.event is None

    def test_active_ad_after_seller_sold_is_recorded_as_conflict_not_override(self) -> None:
        state = listing(
            availability=Availability.SOLD_CLAIMED,
            availability_evidence_kind=AvailabilityEvidenceKind.SELLER_REPORTED_SOLD,
            availability_effective_at=T0 + timedelta(hours=1),
        )
        seen = AvailabilitySignal(
            kind=AvailabilitySignalKind.SEEN_IN_SEARCH, observed_at=T0 + timedelta(hours=2), scan=scan(115)
        )
        event = derive_availability_event(state, seen).event
        assert event is not None
        assert event.conflicts_with_current and not event.promote_current

    def test_seller_available_after_source_sold_badge_is_a_conflict(self) -> None:
        state = listing(
            availability=Availability.SOLD_CLAIMED,
            availability_evidence_kind=AvailabilityEvidenceKind.SOURCE_SOLD_BADGE,
            availability_effective_at=T0,
        )
        signal = AvailabilitySignal(
            kind=AvailabilitySignalKind.SELLER_STATEMENT,
            observed_at=T0 + timedelta(hours=1),
            seller_status=Availability.AVAILABLE,
            reply_id=REPLY,
        )
        event = derive_availability_event(state, signal).event
        assert event is not None and event.conflicts_with_current and not event.promote_current

    def test_older_evidence_never_regresses_current_status(self) -> None:
        state = listing(availability_effective_at=T0 + timedelta(hours=5))
        old = AvailabilitySignal(
            kind=AvailabilitySignalKind.SOLD_BADGE, observed_at=T0 + timedelta(hours=6), effective_at=T0
        )
        event = derive_availability_event(state, old).event
        assert event is not None and event.historical_only and not event.promote_current

    def test_no_change_and_relisting(self) -> None:
        seen = AvailabilitySignal(kind=AvailabilitySignalKind.SEEN_IN_SEARCH, observed_at=T0, scan=scan(0))
        assert derive_availability_event(listing(), seen).no_event_reason == "no change"
        removed = listing(
            availability=Availability.REMOVED,
            availability_evidence_kind=AvailabilityEvidenceKind.SOURCE_REMOVED_PAGE,
            availability_effective_at=T0,
        )
        back = AvailabilitySignal(
            kind=AvailabilitySignalKind.SEEN_IN_SEARCH, observed_at=T0 + timedelta(hours=1), scan=scan(55)
        )
        event = derive_availability_event(removed, back).event
        assert event is not None and event.new_status == Availability.AVAILABLE and event.promote_current

    def test_signal_validation_and_source_mismatch(self) -> None:
        with pytest.raises(ValueError):
            AvailabilitySignal(
                kind=AvailabilitySignalKind.SELLER_STATEMENT,
                observed_at=T0,
                seller_status=Availability.SOLD_CLAIMED,
            )
        with pytest.raises(ValueError):
            AvailabilitySignal(
                kind=AvailabilitySignalKind.SELLER_STATEMENT,
                observed_at=T0,
                seller_status=Availability.REMOVED,
                reply_id=REPLY,
            )
        with pytest.raises(ValueError):
            AvailabilitySignal(kind=AvailabilitySignalKind.NOT_SEEN_IN_SCAN, observed_at=T0)
        with pytest.raises(ValueError):
            AvailabilitySignal(kind=AvailabilitySignalKind.MANUAL, observed_at=T0)
        with pytest.raises(ValidationFailed):
            derive_availability_event(listing(), absence(scan(60, source="other")))
        with pytest.raises(ValidationFailed):
            derive_availability_event(listing(), absence(scan(60, fixture=True)))

    def test_manual_event(self) -> None:
        signal = AvailabilitySignal(
            kind=AvailabilitySignalKind.MANUAL,
            observed_at=T0 + timedelta(hours=1),
            manual_status=Availability.REMOVED,
        )
        event = derive_availability_event(listing(), signal).event
        assert event is not None and event.evidence_kind == AvailabilityEvidenceKind.MANUAL


# =============================================================================================
# Clusters and contradictions (37.10: contradictory availability stops outreach)
# =============================================================================================


class TestClusters:
    def test_cluster_derivation_preserves_per_source_evidence(self) -> None:
        a = listing(
            first_seen_at=T0 + timedelta(hours=2),
            last_seen_on_search_at=T0 + timedelta(hours=9),
            source_published=TrustedSourceTime(
                value=T0 - timedelta(days=2), trustworthy=True, precision=Precision.DAY
            ),
        )
        b = listing(
            listing_id=LISTING_B,
            source_key="fixture_portal_it",
            first_seen_at=T0,
            last_seen_on_search_at=T0 + timedelta(hours=1),
            last_detail_success_at=T0 + timedelta(hours=10),
            source_published=TrustedSourceTime(
                value=T0 - timedelta(days=5), trustworthy=False, precision=Precision.DAY
            ),
        )
        cluster = derive_cluster_lifecycle(CLUSTER, [a, b])
        assert cluster.earliest_observed_appearance == T0
        assert cluster.earliest_observed_source == "fixture_portal_it"
        assert cluster.earliest_trusted_publication == T0 - timedelta(days=2)  # untrusted date ignored
        assert cluster.earliest_trusted_publication_source == "fixture_dealer_de"
        assert cluster.latest_source_presence == T0 + timedelta(hours=10)
        assert cluster.latest_presence_source == "fixture_portal_it"
        assert {m.listing_id for m in cluster.members} == {LISTING, LISTING_B}
        assert cluster.conflicts == () and not cluster.outreach_blocked

    def test_cluster_validation(self) -> None:
        with pytest.raises(ValidationFailed):
            derive_cluster_lifecycle(CLUSTER, [])
        with pytest.raises(ValidationFailed):
            derive_cluster_lifecycle(CLUSTER, [listing(), listing()])
        with pytest.raises(ValidationFailed):
            derive_cluster_lifecycle(CLUSTER, [listing(vehicle_cluster_id=uuid4())])
        with pytest.raises(ValidationFailed):
            derive_cluster_lifecycle(CLUSTER, [listing(), listing(listing_id=LISTING_B, is_fixture=True)])

    def test_active_duplicate_vs_seller_sold_is_preserved_and_stops_outreach(self) -> None:
        sold_here = listing(
            availability=Availability.SOLD_CLAIMED,
            availability_evidence_kind=AvailabilityEvidenceKind.SELLER_REPORTED_SOLD,
            availability_effective_at=T0 + timedelta(hours=1),
        )
        active_elsewhere = listing(
            listing_id=LISTING_B,
            source_key="fixture_portal_it",
            last_seen_on_search_at=T0 + timedelta(hours=4),
        )
        statement = SellerAvailabilityStatement(
            reply_id=REPLY, status=Availability.SOLD_CLAIMED, stated_at=T0 + timedelta(hours=1)
        )
        cluster = derive_cluster_lifecycle(CLUSTER, [sold_here, active_elsewhere], [statement])
        (conflict,) = cluster.conflicts
        assert conflict.kind == "active_listing_vs_seller_sold"
        assert conflict.stop_outreach is True
        assert conflict.suppression_reason == SuppressionReason.CONTRADICTORY_AVAILABILITY
        assert set(conflict.listing_ids) == {LISTING, LISTING_B}
        assert conflict.reply_ids == (REPLY,)
        assert "seen after the statement" in conflict.detail
        assert cluster.outreach_blocked
        # The seller-sold listing itself and the active one keep their own statuses.
        statuses = {m.listing_id: m.availability for m in cluster.members}
        assert statuses == {LISTING: Availability.SOLD_CLAIMED, LISTING_B: Availability.AVAILABLE}

    def test_same_listing_still_advertised_after_seller_sold(self) -> None:
        sold = listing(
            availability=Availability.SOLD_CLAIMED,
            availability_evidence_kind=AvailabilityEvidenceKind.SELLER_REPORTED_SOLD,
            availability_effective_at=T0 + timedelta(hours=1),
            last_seen_on_search_at=T0 + timedelta(hours=3),
        )
        (conflict,) = detect_availability_conflicts([sold])
        assert conflict.kind == "active_listing_vs_seller_sold"

    def test_other_contradictions(self) -> None:
        badge = listing(
            availability=Availability.SOLD_CLAIMED,
            availability_evidence_kind=AvailabilityEvidenceKind.SOURCE_SOLD_BADGE,
        )
        active = listing(listing_id=LISTING_B, source_key="b")
        available_statement = SellerAvailabilityStatement(
            reply_id=REPLY, status=Availability.AVAILABLE, stated_at=T0
        )
        kinds = {c.kind for c in detect_availability_conflicts([badge, active], [available_statement])}
        assert kinds == {"seller_available_vs_source_unavailable", "sources_disagree"}
        # Different statuses stated at the same time contradict each other.
        later = uuid4()
        simultaneous = [
            SellerAvailabilityStatement(reply_id=REPLY, status=Availability.AVAILABLE, stated_at=T0),
            SellerAvailabilityStatement(reply_id=later, status=Availability.RESERVED, stated_at=T0),
        ]
        (conflict,) = detect_availability_conflicts([active], simultaneous)
        assert conflict.kind == "seller_statements_disagree"
        assert set(conflict.reply_ids) == {REPLY, later}

    def test_seller_progression_is_not_a_contradiction_but_reversal_after_sold_is(self) -> None:
        active = listing(listing_id=LISTING_B, source_key="b")

        def said(status: Availability, hours: int) -> SellerAvailabilityStatement:
            return SellerAvailabilityStatement(
                reply_id=uuid4(), status=status, stated_at=T0 + timedelta(hours=hours)
            )

        progression = [
            said(Availability.AVAILABLE, 0),
            said(Availability.RESERVED, 1),
            said(Availability.AVAILABLE, 2),  # reservation fell through
        ]
        assert detect_availability_conflicts([active], progression) == ()
        sold_then_available = [said(Availability.SOLD_CLAIMED, 0), said(Availability.AVAILABLE, 5)]
        kinds = {c.kind for c in detect_availability_conflicts([], sold_then_available)}
        assert kinds == {"seller_statements_disagree"}
        statuses = {c.kind: c for c in detect_availability_conflicts([active], sold_then_available)}
        # The seller's own "sold" is never silently cancelled by an active ad or a later reversal.
        assert set(statuses) == {"active_listing_vs_seller_sold", "seller_statements_disagree"}
        assert all(c.stop_outreach for c in statuses.values())

    def test_consistent_evidence_has_no_conflict(self) -> None:
        sold = listing(
            availability=Availability.SOLD_CLAIMED,
            availability_evidence_kind=AvailabilityEvidenceKind.SELLER_REPORTED_SOLD,
            availability_effective_at=T0 + timedelta(hours=1),
        )
        badge = listing(
            listing_id=LISTING_C,
            source_key="c",
            availability=Availability.SOLD_CLAIMED,
            availability_evidence_kind=AvailabilityEvidenceKind.SOURCE_SOLD_BADGE,
        )
        assert detect_availability_conflicts([sold, badge]) == ()
        with pytest.raises(ValueError):
            SellerAvailabilityStatement(reply_id=REPLY, status=Availability.REMOVED, stated_at=T0)


# =============================================================================================
# New today
# =============================================================================================


class TestNewToday:
    def test_first_seen_never_makes_an_ad_new(self) -> None:
        decision = is_new_today(TrustedSourceTime(), now=T0)
        assert decision.status == "unknown" and decision.reason == "publication_time_unknown"
        untrusted = TrustedSourceTime(value=T0, trustworthy=False, precision=Precision.DAY)
        assert is_new_today(untrusted, now=T0).status == "unknown"

    def test_trusted_publication_today_and_earlier(self) -> None:
        today = TrustedSourceTime(value=T0 - timedelta(hours=2), trustworthy=True, precision=Precision.DAY)
        assert is_new_today(today, now=T0).status == "yes"
        older = TrustedSourceTime(value=T0 - timedelta(days=3), trustworthy=True, precision=Precision.DAY)
        result = is_new_today(older, now=T0)
        assert result.status == "no" and result.local_date == "2026-10-03"

    def test_owner_timezone_boundary(self) -> None:
        # 22:30 UTC on 5 Oct is 00:30 on 6 Oct in Europe/Skopje (UTC+2).
        published = TrustedSourceTime(
            value=datetime(2026, 10, 5, 22, 30, tzinfo=UTC), trustworthy=True, precision=Precision.DAY
        )
        assert is_new_today(published, now=datetime(2026, 10, 6, 9, 0, tzinfo=UTC)).status == "yes"
        assert (
            is_new_today(published, now=datetime(2026, 10, 6, 9, 0, tzinfo=UTC), timezone="UTC").status
            == "no"
        )

    @pytest.mark.parametrize(
        ("published", "reason"),
        [
            (
                TrustedSourceTime(value=T0, trustworthy=True, precision=Precision.MONTH),
                "publication_time_too_coarse",
            ),
            (
                TrustedSourceTime(value=T0, trustworthy=True, precision=Precision.DAY, zone_assumed=True),
                "publication_zone_assumed",
            ),
            (
                TrustedSourceTime(value=T0 + timedelta(hours=1), trustworthy=True, precision=Precision.DAY),
                "publication_time_in_future",
            ),
        ],
    )
    def test_unknown_cases(self, published: TrustedSourceTime, reason: str) -> None:
        decision = is_new_today(published, now=T0)
        assert decision.status == "unknown" and decision.reason == reason

    def test_invalid_zone(self) -> None:
        with pytest.raises(ValidationFailed):
            is_new_today(TrustedSourceTime(), now=T0, timezone="Mars/Olympus")


# =============================================================================================
# Lags (37.10: lags unknown without timestamps)
# =============================================================================================


class TestLags:
    def test_every_lag_is_unknown_without_timestamps(self) -> None:
        lags = [
            source_scan_lag(None, now=T0, configured_interval=timedelta(minutes=15)),
            detection_delay(T0, TrustedSourceTime()),
            detection_delay(None, TrustedSourceTime(value=T0, trustworthy=True, precision=Precision.DAY)),
            detail_freshness(None, now=T0),
            mail_reply_detection_lag(T0, None, configured_interval=timedelta(minutes=2)),
            mail_reply_detection_lag(None, T0),
            notification_processing_lag(T0, None),
        ]
        for lag in lags:
            assert lag.status == LagStatus.UNKNOWN
            assert lag.value_seconds is None and lag.value is None
            assert lag.reason

    def test_configured_intervals_are_context_not_measurements(self) -> None:
        lag = source_scan_lag(None, now=T0, configured_interval=timedelta(minutes=15))
        assert lag.configured_interval_seconds == 900
        assert lag.note == INTERVAL_NOTE
        assert "not an observed latency guarantee" in lag.note

    def test_measured_values(self) -> None:
        assert source_scan_lag(T0, now=T0 + timedelta(minutes=20)).value == timedelta(minutes=20)
        published = TrustedSourceTime(value=T0, trustworthy=True, precision=Precision.DAY)
        assert detection_delay(T0 + timedelta(minutes=47), published).value_seconds == 47 * 60
        assert detail_freshness(T0, now=T0 + timedelta(hours=26)).value == timedelta(hours=26)
        assert mail_reply_detection_lag(T0, T0 + timedelta(seconds=95)).value_seconds == 95
        assert notification_processing_lag(T0, T0 + timedelta(seconds=3)).value_seconds == 3

    def test_detection_delay_requires_precise_known_zone(self) -> None:
        coarse = TrustedSourceTime(value=T0, trustworthy=True, precision=Precision.MONTH)
        assumed = TrustedSourceTime(value=T0, trustworthy=True, precision=Precision.DAY, zone_assumed=True)
        assert detection_delay(T0, coarse).status == LagStatus.UNKNOWN
        assert detection_delay(T0, assumed).status == LagStatus.UNKNOWN

    def test_inconsistent_timestamps(self) -> None:
        lag = mail_reply_detection_lag(T0, T0 - timedelta(hours=1))
        assert lag.status == LagStatus.INCONSISTENT and lag.value_seconds is None
        tolerated = mail_reply_detection_lag(T0, T0 - timedelta(minutes=1))
        assert tolerated.status == LagStatus.MEASURED and tolerated.value_seconds == 0


# =============================================================================================
# Coverage
# =============================================================================================


class TestCoverage:
    def test_intervals_gaps_and_reasons(self) -> None:
        scans = [
            scan(0),
            scan(15),
            scan(30),
            scan(45, completeness=Completeness.FAILED),
            scan(60, incident=True),
            scan(240),
            scan(255),
            scan(20, fixture=True),
        ]
        (report,) = healthy_coverage(
            scans, window_start=T0, window_end=T0 + timedelta(hours=6), max_gap=timedelta(minutes=30)
        )
        assert [(i.start, i.end, i.scans) for i in report.intervals] == [
            (T0, T0 + timedelta(minutes=35), 3),
            (T0 + timedelta(minutes=240), T0 + timedelta(minutes=260), 2),
        ]
        assert [g.reason for g in report.gaps] == ["parser_incident", "no_scans"]
        assert report.gaps[0].start == T0 + timedelta(minutes=35)
        assert report.gaps[1].end == T0 + timedelta(hours=6)
        assert report.healthy_seconds == (35 + 20) * 60
        assert report.coverage_ratio == Decimal("0.1528")
        assert report.has_coverage

    def test_overlapping_scans_are_never_double_counted(self) -> None:
        # A long scan (09:00-11:00 relative) starts before a short chain ends and finishes later.
        scans = [scan(60, duration=5), scan(0, duration=120)]
        (report,) = healthy_coverage(
            scans, window_start=T0, window_end=T0 + timedelta(hours=3), max_gap=timedelta(minutes=30)
        )
        assert [(i.start, i.end, i.scans) for i in report.intervals] == [(T0, T0 + timedelta(hours=2), 2)]
        assert report.healthy_seconds == 2 * 3600
        assert report.coverage_ratio is not None and report.coverage_ratio <= 1
        assert [(g.start, g.end) for g in report.gaps] == [(T0 + timedelta(hours=2), T0 + timedelta(hours=3))]

    def test_coverage_ratio_never_exceeds_one(self) -> None:
        scans = [scan(m, duration=d) for m, d in [(0, 50), (10, 5), (20, 90), (30, 2), (100, 30)]]
        (report,) = healthy_coverage(
            scans, window_start=T0, window_end=T0 + timedelta(hours=2), max_gap=timedelta(minutes=10)
        )
        assert report.coverage_ratio is not None and Decimal(0) <= report.coverage_ratio <= 1
        starts = [i.start for i in report.intervals]
        assert starts == sorted(starts)
        for first, second in zip(report.intervals, report.intervals[1:], strict=False):
            assert first.end < second.start

    def test_empty_window_has_no_ratio(self) -> None:
        (report,) = healthy_coverage([scan(0)], window_start=T0, window_end=T0, max_gap=timedelta(minutes=30))
        assert report.coverage_ratio is None

    def test_no_healthy_scans_is_one_explained_gap(self) -> None:
        (report,) = healthy_coverage(
            [scan(0, health=SourceHealth.PAUSED)],
            window_start=T0,
            window_end=T0 + timedelta(hours=1),
            max_gap=timedelta(minutes=30),
        )
        assert report.intervals == ()
        assert [g.reason for g in report.gaps] == ["source_paused"]
        assert report.coverage_ratio == Decimal("0.0000")

    def test_fixture_scans_only_when_requested_and_validation(self) -> None:
        fixture_only = [scan(0, fixture=True)]
        assert (
            healthy_coverage(
                fixture_only, window_start=T0, window_end=T0 + timedelta(hours=1), max_gap=timedelta(hours=1)
            )
            == ()
        )
        assert healthy_coverage(
            fixture_only,
            window_start=T0,
            window_end=T0 + timedelta(hours=1),
            max_gap=timedelta(hours=1),
            include_fixture=True,
        )
        with pytest.raises(ValidationFailed):
            healthy_coverage(
                [], window_start=T0, window_end=T0 - timedelta(seconds=1), max_gap=timedelta(hours=1)
            )
        with pytest.raises(ValidationFailed):
            healthy_coverage([], window_start=T0, window_end=T0, max_gap=timedelta(0))


class TestMailWorker:
    def beats(self, minutes: list[int], **kw: Any) -> list[MailWorkerHeartbeat]:
        return [
            MailWorkerHeartbeat(
                observed_at=T0 + timedelta(minutes=m),
                outlook_connected=kw.get("outlook", True),
                mailbox_sync_ok=True,
                mailbox_last_sync_at=T0 + timedelta(minutes=m) - timedelta(seconds=40),
                last_reconciliation_completed_at=T0 + timedelta(minutes=m) - timedelta(seconds=30),
                backlog_count=kw.get("backlog", 0),
                backlog_oldest_at=kw.get("oldest"),
            )
            for m in minutes
        ]

    def test_healthy_worker(self) -> None:
        report = mail_worker_coverage(
            MAILBOX,
            self.beats([0, 1, 2, 3, 4]),
            now=T0 + timedelta(minutes=4, seconds=30),
            window_start=T0,
            heartbeat_interval=timedelta(minutes=1),
            reconcile_interval=timedelta(minutes=2),
        )
        assert report.monitoring_active
        assert report.gaps == ()
        assert report.mailbox_sync_lag.value_seconds == 40
        assert report.backlog_age.value_seconds == 0
        assert report.slack_signal_status == "unknown" and report.mcp_read_status == "unknown"

    def test_sleep_or_power_off_shows_gaps_and_no_monitoring_claim(self) -> None:
        report = mail_worker_coverage(
            MAILBOX,
            self.beats([0, 1, 2, 90, 91], backlog=3, oldest=T0 + timedelta(minutes=3)),
            now=T0 + timedelta(minutes=200),
            window_start=T0,
            heartbeat_interval=timedelta(minutes=1),
            reconcile_interval=timedelta(minutes=2),
        )
        assert [(g.start, g.end, g.reason) for g in report.gaps] == [
            (T0 + timedelta(minutes=2), T0 + timedelta(minutes=90), "worker_offline"),
            (T0 + timedelta(minutes=91), T0 + timedelta(minutes=200), "worker_offline"),
        ]
        assert not report.monitoring_active
        assert report.heartbeat_status == "down"
        assert report.outlook_status == "stale"
        assert report.backlog_count == 3
        assert report.backlog_age.value_seconds == 197 * 60
        assert any("heartbeat" in r for r in report.reasons)

    def test_outlook_disconnected_and_no_heartbeats(self) -> None:
        down = mail_worker_coverage(
            MAILBOX,
            self.beats([0], outlook=False),
            now=T0 + timedelta(seconds=30),
            window_start=T0,
            heartbeat_interval=timedelta(minutes=1),
            reconcile_interval=timedelta(minutes=2),
        )
        assert down.outlook_status == "down" and not down.monitoring_active
        empty = mail_worker_coverage(
            MAILBOX,
            [],
            now=T0 + timedelta(hours=1),
            window_start=T0,
            heartbeat_interval=timedelta(minutes=1),
            reconcile_interval=timedelta(minutes=2),
            slack_last_accepted_at=T0,
            slack_expected_interval=timedelta(minutes=10),
        )
        assert empty.heartbeat_status == "unknown" and empty.backlog_age.status == LagStatus.UNKNOWN
        assert [g.reason for g in empty.gaps] == ["worker_offline"]
        assert empty.slack_signal_status == "stale"
        with pytest.raises(ValidationFailed):
            mail_worker_coverage(
                MAILBOX,
                [],
                now=T0,
                window_start=T0,
                heartbeat_interval=timedelta(0),
                reconcile_interval=timedelta(minutes=2),
            )

    def test_reconciliation_window_with_overlap_and_gaps(self) -> None:
        normal = reconciliation_window(
            mailbox_binding_id=MAILBOX,
            last_complete_scan_at=T0,
            now=T0 + timedelta(minutes=2),
            overlap=timedelta(minutes=10),
            retention_limit=timedelta(days=7),
        )
        assert (
            normal.since == T0 - timedelta(minutes=10)
            and not normal.full_rescan_required
            and normal.gap is None
        )
        none = reconciliation_window(
            mailbox_binding_id=MAILBOX,
            last_complete_scan_at=None,
            now=T0,
            overlap=timedelta(minutes=10),
            retention_limit=timedelta(days=7),
        )
        assert none.full_rescan_required and none.since == T0 - timedelta(days=7)
        assert none.gap is not None and none.gap.reason == "no_checkpoint_history_before_retention"
        old = reconciliation_window(
            mailbox_binding_id=MAILBOX,
            last_complete_scan_at=T0 - timedelta(days=30),
            now=T0,
            overlap=timedelta(minutes=10),
            retention_limit=timedelta(days=7),
        )
        assert old.full_rescan_required and old.gap is not None
        assert old.gap.reason == "checkpoint_older_than_retention"
        with pytest.raises(ValidationFailed):
            reconciliation_window(
                mailbox_binding_id=MAILBOX,
                last_complete_scan_at=T0 + timedelta(hours=1),
                now=T0,
                overlap=timedelta(minutes=10),
                retention_limit=timedelta(days=7),
            )

    def test_checkpoint_advances_only_after_durable_commit(self) -> None:
        assert (
            advance_checkpoint(T0, scanned_until=T0 + timedelta(minutes=2), all_candidates_committed=False)
            == T0
        )
        assert advance_checkpoint(
            T0, scanned_until=T0 + timedelta(minutes=2), all_candidates_committed=True
        ) == (T0 + timedelta(minutes=2))
        assert (
            advance_checkpoint(T0, scanned_until=T0 - timedelta(minutes=2), all_candidates_committed=True)
            == T0
        )
        assert advance_checkpoint(None, scanned_until=T0, all_candidates_committed=True) == T0
        assert advance_checkpoint(None, scanned_until=T0, all_candidates_committed=False) is None


# =============================================================================================
# Independent review regressions
# =============================================================================================


class TestIndependentReviewRegressions:
    def test_absence_is_not_evidence_when_a_detail_page_was_live_after_the_scan_started(self) -> None:
        # Last search sighting at +30 min; the scan runs 90-110 min; the detail page was fetched
        # successfully (active) at +100 min - the search simply did not list it.
        during = listing(last_detail_success_at=T0 + timedelta(minutes=100))
        decision = derive_availability_event(during, absence(scan(90, duration=20)))
        assert decision.event is None
        assert decision.no_event_reason == "scan started before the last sighting"
        later = listing(last_detail_success_at=T0 + timedelta(minutes=200))
        assert derive_availability_event(later, absence(scan(90, duration=20))).event is None
        # A detail success *before* the scan does not block the absence evidence.
        before = listing(last_detail_success_at=T0 + timedelta(minutes=60))
        event = derive_availability_event(before, absence(scan(90, duration=20))).event
        assert event is not None and event.new_status == Availability.UNKNOWN
        assert event.reason == "not_seen_in_complete_scan"

    @pytest.mark.parametrize(
        "signal",
        [
            AvailabilitySignal(
                kind=AvailabilitySignalKind.RESERVED_BADGE, observed_at=T0 + timedelta(hours=2)
            ),
            AvailabilitySignal(
                kind=AvailabilitySignalKind.DETAIL_ACTIVE, observed_at=T0 + timedelta(hours=2)
            ),
            AvailabilitySignal(
                kind=AvailabilitySignalKind.SELLER_STATEMENT,
                observed_at=T0 + timedelta(hours=2),
                seller_status=Availability.AVAILABLE,
                reply_id=UUID("88888888-8888-4888-8888-888888888889"),
            ),
            AvailabilitySignal(
                kind=AvailabilitySignalKind.SELLER_STATEMENT,
                observed_at=T0 + timedelta(hours=2),
                seller_status=Availability.RESERVED,
                reply_id=UUID("88888888-8888-4888-8888-888888888889"),
            ),
        ],
        ids=["reserved_badge", "detail_active", "seller_available", "seller_reserved"],
    )
    def test_nothing_silently_overrides_a_seller_sold_statement(self, signal: AvailabilitySignal) -> None:
        state = listing(
            availability=Availability.SOLD_CLAIMED,
            availability_evidence_kind=AvailabilityEvidenceKind.SELLER_REPORTED_SOLD,
            availability_effective_at=T0 + timedelta(hours=1),
        )
        event = derive_availability_event(state, signal).event
        assert event is not None
        assert event.conflicts_with_current and not event.promote_current
        # The cluster view reports the same contradiction and stops outreach.
        if signal.kind == AvailabilitySignalKind.SELLER_STATEMENT and signal.seller_status is not None:
            statements = [
                SellerAvailabilityStatement(reply_id=REPLY, status=Availability.SOLD_CLAIMED, stated_at=T0),
                SellerAvailabilityStatement(
                    reply_id=signal.reply_id or REPLY, status=signal.seller_status, stated_at=signal.when
                ),
            ]
            conflicts = detect_availability_conflicts([listing()], statements)
            assert any(c.kind == "seller_statements_disagree" for c in conflicts)
            assert all(c.stop_outreach for c in conflicts)

    def test_removal_and_manual_resolution_after_seller_sold_are_still_promoted(self) -> None:
        state = listing(
            availability=Availability.SOLD_CLAIMED,
            availability_evidence_kind=AvailabilityEvidenceKind.SELLER_REPORTED_SOLD,
            availability_effective_at=T0 + timedelta(hours=1),
        )
        removed = derive_availability_event(
            state,
            AvailabilitySignal(kind=AvailabilitySignalKind.REMOVED_PAGE, observed_at=T0 + timedelta(hours=2)),
        ).event
        assert removed is not None and removed.promote_current and removed.new_status == Availability.REMOVED
        manual = derive_availability_event(
            state,
            AvailabilitySignal(
                kind=AvailabilitySignalKind.MANUAL,
                observed_at=T0 + timedelta(hours=2),
                manual_status=Availability.AVAILABLE,
            ),
        ).event
        assert manual is not None and manual.promote_current and not manual.conflicts_with_current

    def test_a_late_older_scan_never_regresses_the_current_source_health(self) -> None:
        newer = scan(120)  # healthy, complete; finished at +125 min
        older = scan(60, health=SourceHealth.BLOCKED, completeness=Completeness.BLOCKED)
        state = apply_scan_result(apply_scan_result(listing(), newer), older)
        assert state.source_health == SourceHealth.HEALTHY
        assert state.source_health_at == newer.finished_at
        assert state.last_complete_scan_at == newer.finished_at
        # In order, the newer degraded scan does take over.
        degraded = scan(180, health=SourceHealth.DEGRADED, completeness=Completeness.PARTIAL)
        assert apply_scan_result(state, degraded).source_health == SourceHealth.DEGRADED
        assert apply_scan_result(state, degraded).last_complete_scan_at == newer.finished_at


# =============================================================================================
# Independent review, third round: each test pins a defect found and fixed in review
# =============================================================================================


class TestThirdReviewRegressions:
    def test_older_evidence_is_history_not_a_contradiction(self) -> None:
        # The ad was seen active (or the seller said "available") *before* the seller's "sold"
        # took effect: an ordinary progression, recorded as history, never as a conflict.
        seller_sold = listing(
            availability=Availability.SOLD_CLAIMED,
            availability_evidence_kind=AvailabilityEvidenceKind.SELLER_REPORTED_SOLD,
            availability_effective_at=T0 + timedelta(hours=5),
        )
        earlier_sighting = AvailabilitySignal(
            kind=AvailabilitySignalKind.SEEN_IN_SEARCH,
            observed_at=T0 + timedelta(hours=6),
            effective_at=T0 + timedelta(hours=1),
            scan=scan(55),
        )
        event = derive_availability_event(seller_sold, earlier_sighting).event
        assert event is not None and event.historical_only
        assert not event.conflicts_with_current and not event.promote_current
        earlier_statement = AvailabilitySignal(
            kind=AvailabilitySignalKind.SELLER_STATEMENT,
            observed_at=T0 + timedelta(hours=6),
            effective_at=T0 + timedelta(hours=1),
            seller_status=Availability.AVAILABLE,
            reply_id=REPLY,
        )
        statement_event = derive_availability_event(seller_sold, earlier_statement).event
        assert statement_event is not None and statement_event.historical_only
        assert not statement_event.conflicts_with_current
        # The same signals *after* the seller's "sold" are still contradictions.
        later = earlier_statement.model_copy(update={"effective_at": T0 + timedelta(hours=7)})
        later_event = derive_availability_event(seller_sold, later).event
        assert later_event is not None and later_event.conflicts_with_current

    def test_badge_after_a_seller_available_is_a_progression_not_a_conflict(self) -> None:
        sold_later = listing(
            availability=Availability.SOLD_CLAIMED,
            availability_evidence_kind=AvailabilityEvidenceKind.SOURCE_SOLD_BADGE,
            availability_effective_at=T0 + timedelta(days=3),
        )
        said_available = SellerAvailabilityStatement(
            reply_id=REPLY, status=Availability.AVAILABLE, stated_at=T0
        )
        assert detect_availability_conflicts([sold_later], [said_available]) == ()
        # A badge that already existed (or whose time is unknown) still contradicts "available".
        sold_before = sold_later.model_copy(update={"availability_effective_at": T0 - timedelta(hours=1)})
        (conflict,) = detect_availability_conflicts([sold_before], [said_available])
        assert conflict.kind == "seller_available_vs_source_unavailable" and conflict.stop_outreach
        unknown_time = sold_later.model_copy(update={"availability_effective_at": None})
        (unknown,) = detect_availability_conflicts([unknown_time], [said_available])
        assert unknown.kind == "seller_available_vs_source_unavailable"
        assert unknown.listing_ids == (LISTING,)
        # Listing-level view agrees: a seller "available" older than the badge is history only.
        older = AvailabilitySignal(
            kind=AvailabilitySignalKind.SELLER_STATEMENT,
            observed_at=T0 + timedelta(days=4),
            effective_at=T0,
            seller_status=Availability.AVAILABLE,
            reply_id=REPLY,
        )
        event = derive_availability_event(sold_later, older).event
        assert event is not None and event.historical_only and not event.conflicts_with_current
