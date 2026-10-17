"""Health reporting, coverage gaps, local matching units and the guarded run loop."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import timedelta
from uuid import uuid4

import pytest
from bridge_support import MAILBOX_ID, OWNER, SELLER, START, Harness
from outlook_bridge.credentials import CredentialState
from outlook_bridge.errors import UploadBuildError
from outlook_bridge.health import CoverageMonitor, build_health, heartbeat_envelope
from outlook_bridge.local_queue import LocalStore, Outcome
from outlook_bridge.matching import (
    DecisionKind,
    LocalDecision,
    LocalMatcher,
    idempotency_key_for,
    semantics_versions,
    wire_payload,
)
from outlook_bridge.outlook_adapter import ConnectionState, MailSnapshot
from outlook_bridge.testing import inquiry_message_id


@pytest.fixture
def store() -> LocalStore:
    return LocalStore.in_memory(MAILBOX_ID)


# ----------------------------------------------------------------------------------------- coverage


def test_startup_after_an_offline_period_records_the_gap(store: LocalStore) -> None:
    monitor = CoverageMonitor(store)
    monitor.on_startup(START)
    monitor.on_shutdown(START + timedelta(minutes=1))
    CoverageMonitor(store).on_startup(START + timedelta(minutes=2))  # short restart: no gap
    assert store.gaps() == []
    CoverageMonitor(store).on_startup(START + timedelta(hours=6))
    gaps = store.gaps(kinds={"worker_offline"})
    assert len(gaps) == 1 and gaps[0].started_at == START + timedelta(minutes=2)


def test_suspension_between_ticks_is_recorded(store: LocalStore) -> None:
    monitor = CoverageMonitor(store)
    monitor.on_startup(START)
    monitor.on_tick(START + timedelta(seconds=30))
    assert store.gaps() == []
    monitor.on_tick(START + timedelta(hours=2))
    gap = store.gaps(kinds={"worker_suspended"})[0]
    assert gap.started_at == START + timedelta(seconds=30) and gap.ended_at == START + timedelta(hours=2)


def test_detection_gaps_hold_the_watermark_until_settled(store: LocalStore) -> None:
    monitor = CoverageMonitor(store)
    settle = timedelta(minutes=10)
    assert monitor.watermark_cap(START, settle) is None
    monitor.on_connection(ConnectionState(outlook_running=False, connected=False), START)
    assert monitor.watermark_cap(START + timedelta(minutes=5), settle) == START  # open gap
    monitor.on_connection(
        ConnectionState(outlook_running=True, connected=True), START + timedelta(minutes=20)
    )
    assert monitor.watermark_cap(START + timedelta(minutes=25), settle) == START  # closed < settle ago
    assert monitor.watermark_cap(START + timedelta(minutes=31), settle) is None
    # Non-detection gaps (backend outage) never hold the mailbox watermark back.
    monitor.on_transmission(False, START + timedelta(minutes=40))
    assert monitor.watermark_cap(START + timedelta(minutes=45), settle) is None


def test_build_health_reports_dimensions_separately(harness: Harness) -> None:
    store = harness.store
    config = harness.config
    snapshot = build_health(
        store,
        config=config,
        now=START,
        connection=None,
        compat=None,
        credential_state=CredentialState.MISSING,
    )
    assert snapshot.worker.status == "down"  # never alive yet
    assert snapshot.outlook.status == "unknown" and snapshot.mailbox_sync.status == "unknown"
    assert snapshot.reconciliation.status == "degraded" and snapshot.server.status == "down"
    assert snapshot.monitoring_24h_claimed is False
    monitor = CoverageMonitor(store)
    monitor.on_startup(START)
    store.set_runtime_time("last_successful_reconcile_at", START)
    store.set_runtime_time("last_server_ok_at", START)
    store.set_runtime("downstream_health", "{not json")
    healthy = build_health(
        store,
        config=config,
        now=START + timedelta(minutes=1),
        connection=ConnectionState(outlook_running=True, connected=True, last_send_receive_end_at=START),
        compat=harness.compat,
        credential_state=CredentialState.ACTIVE,
    )
    assert healthy.worker.status == "ok" and healthy.outlook.status == "ok"
    assert healthy.mailbox_sync.lag_seconds == 60 and healthy.mailbox_sync.status == "ok"
    assert healthy.reconciliation.status == "ok" and healthy.server.status == "ok"
    assert healthy.downstream == {"mcp": "not_observed", "slack_signal": "not_observed"}
    stale = build_health(
        store,
        config=config,
        now=START + timedelta(hours=2),
        connection=ConnectionState(outlook_running=True, connected=False, last_send_receive_end_at=START),
        compat=harness.compat,
        credential_state=CredentialState.ACTIVE,
    )
    assert stale.outlook.status == "degraded" and stale.mailbox_sync.status == "degraded"
    assert stale.reconciliation.status == "degraded" and stale.worker.status == "down"


def test_heartbeat_marks_only_closed_gaps_as_reported(store: LocalStore, harness: Harness) -> None:
    store.open_gap("outlook_not_running", START)
    store.record_gap("worker_offline", START - timedelta(hours=2), START - timedelta(hours=1))
    envelope, closed_ids = heartbeat_envelope(store, config=harness.config, now=START, connection=None)
    assert {g.kind for g in envelope.gaps} == {"outlook_not_running", "worker_offline"}
    store.mark_gaps_reported(closed_ids)
    again, _ = heartbeat_envelope(store, config=harness.config, now=START, connection=None)
    assert {g.kind for g in again.gaps} == {"outlook_not_running"}  # open gaps keep being reported
    assert again.heartbeat.outlook_running is False and again.heartbeat.mailbox_connected is False


# ----------------------------------------------------------------------------------------- matching


def test_semantics_versions_and_matcher_validation() -> None:
    versions = semantics_versions()
    assert versions["reply_schema"] == "1.0"
    assert {"fingerprint", "correlation", "sanitizer"} <= set(versions)
    with pytest.raises(ValueError, match="retry window"):
        LocalMatcher(MAILBOX_ID, retry_window=timedelta(0))


def _decide(
    harness: Harness, *, sender: str = SELLER, subject: str = "AW: REF-1234"
) -> tuple[LocalMatcher, MailSnapshot, LocalDecision]:
    inquiry = uuid4()
    harness.bind(inquiry)
    harness.worker().start()
    item = harness.outlook.deliver(
        harness.inbox,
        subject=subject,
        body="Verfügbar.",
        sender=sender,
        message_id=f"<{uuid4().hex}@dealer.example.invalid>",
        received_at=harness.clock.now(),
        in_reply_to=inquiry_message_id(inquiry),
        references=(inquiry_message_id(inquiry),),
        fire_event=False,
    )
    ref = harness.session.locate_item(item.EntryID, None)
    assert ref is not None
    snapshot = harness.session.read_item(ref)
    assert snapshot is not None
    matcher = LocalMatcher(MAILBOX_ID, retry_window=timedelta(hours=24), own_addresses=(OWNER,))
    decision = matcher.evaluate(
        snapshot, harness.store.bindings_for_matching(), in_junk_folder=False, first_seen_at=START, now=START
    )
    return matcher, snapshot, decision


def test_quarantined_payload_carries_its_reasons_and_matched_payload_does_not(harness: Harness) -> None:
    matcher, snapshot, decision = _decide(harness, sender="changed@dealer.example.invalid")
    assert decision.kind == DecisionKind.QUARANTINE_UPLOAD
    upload = matcher.prepare_upload(snapshot, decision, {}, in_junk_folder=False, observed_at=START)
    assert upload.quarantined is True
    assert upload.payload["correlation_status"] == "quarantined"
    assert "CHANGED_ADDRESS" in upload.payload["correlation_reasons"]
    assert upload.idempotency_key == idempotency_key_for(upload.request)
    assert len(upload.idempotency_key) <= 128
    matched = upload.request.model_copy(update={"correlation_status": "matched"})
    assert "correlation_reasons" not in wire_payload(matched)
    assert "correlation_status" not in wire_payload(matched)


def test_own_messages_and_unrelated_mail_cannot_be_prepared_for_upload(harness: Harness) -> None:
    matcher, snapshot, decision = _decide(harness, sender=OWNER)
    assert decision.kind == DecisionKind.UNRELATED
    with pytest.raises(UploadBuildError):
        matcher.prepare_upload(snapshot, decision, {}, in_junk_folder=False, observed_at=START)


def test_unrelated_outcome_keeps_no_content(harness: Harness) -> None:
    harness.deliver_personal(subject="Very private subject line", body="Very private body text")
    harness.worker().start()
    assert harness.store.outcome_counts() == {Outcome.UNRELATED.value: 1}
    dump = "\n".join(harness.store._db.iterdump())
    assert "Very private" not in dump and "friend@private" not in dump


# ----------------------------------------------------------------------------------------- run loop


def test_run_loop_survives_a_failing_cycle(
    make_harness: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    worker = make_harness(tick_seconds=0.2).worker()
    calls = {"n": 0}

    def failing_tick(now: object = None) -> object:
        calls["n"] += 1
        raise RuntimeError("transient bug")

    monkeypatch.setattr(worker, "tick", failing_tick)
    caplog.set_level(logging.ERROR, logger="outlook_bridge")
    worker.run(threading.Event(), max_ticks=2)
    assert calls["n"] == 2
    events = [r for r in caplog.records if r.getMessage() == "worker_cycle_failed"]
    assert len(events) == 1  # rate-limited: the first failure is logged, not every tick
    fields = events[0].bridge_fields  # type: ignore[attr-defined]
    assert fields["error_type"] == "RuntimeError" and "transient bug" not in json.dumps(fields)
