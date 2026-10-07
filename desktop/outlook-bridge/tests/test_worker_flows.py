"""Worker-level delivery guarantees (spec 37.6 / 37.8 and the 37.10 delta tests).

Backend outage and replay; revoked/tombstoned bindings stop matching; cross-mailbox injection is
rejected client-side; checkpoints advance only after acknowledgement; credential expiry/rejection
stops transmission and keeps the backlog; sleep/offline outages show gaps and recover the backlog;
Outlook being closed is an honest gap, not a silent stop.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from bridge_support import MAILBOX_ID, OTHER_MAILBOX_ID, TOKEN, TOKEN_2, Harness, make_config
from outlook_bridge.api_client import BindingPage
from outlook_bridge.credentials import CredentialManager, CredentialState, WorkerCredential
from outlook_bridge.health import COVERAGE_STATEMENT, heartbeat_envelope
from outlook_bridge.local_queue import BacklogState, LocalStore
from outlook_bridge.matching import LocalMatcher
from outlook_bridge.outlook_adapter import SyncEventHandler
from outlook_bridge.testing import inquiry_message_id
from outlook_bridge.worker import MAX_BINDING_PAGES_PER_SYNC, BridgeWorker

from suv_deals.domain.replies import InquiryBindingState


def _interval(h: Harness) -> timedelta:
    return h.config.reconcile_interval + timedelta(seconds=1)


def _inbox_key(h: Harness) -> str:
    return next(cp.folder_key for cp in h.store.folder_checkpoints() if cp.role == "inbox")


# ---------------------------------------------------------------------------------- outage / replay


def test_backend_outage_keeps_the_local_queue_and_uploads_after_recovery(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.backend.down = True
    harness.deliver_reply(inquiry, fire_event=True)
    report = worker.tick()
    assert report.events["uploaded"] == 1  # correlated and durably queued locally
    assert report.uploads["deferred_transient"] == 1
    assert harness.reply_posts() == []
    health = worker.health()
    assert health.backlog.pending_uploads == 1
    assert any(g.kind == "backend_unreachable" and g.ended_at is None for g in harness.store.gaps())
    harness.backend.down = False
    harness.advance(timedelta(seconds=10))
    assert worker.tick().uploads == {}  # bounded backoff: not yet due
    harness.advance(timedelta(seconds=30))
    report = worker.tick()
    assert report.uploads["acked"] == 1
    assert len(harness.backend.stored_replies) == 1
    assert worker.health().backlog.pending_uploads == 0
    assert all(g.ended_at is not None for g in harness.store.gaps() if g.kind == "backend_unreachable")


def test_lost_acknowledgement_is_replayed_idempotently(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    harness.deliver_reply(inquiry, fire_event=False)
    harness.backend.store_then_timeout = 1  # stored server-side, acknowledgement lost
    worker = harness.worker()
    report = worker.start()
    assert report.uploads["deferred_transient"] == 1
    assert len(harness.backend.stored_replies) == 1
    harness.advance(timedelta(minutes=1))
    report = worker.tick()
    assert report.uploads["duplicate"] == 1
    assert len(harness.backend.stored_replies) == 1  # stored exactly once
    keys = {h["idempotency-key"] for h in harness.backend.reply_headers}
    assert len(keys) == 1  # same Idempotency-Key and same source identity on replay
    row = harness.store.backlog_rows()[0]
    assert row.state == BacklogState.ACKED and row.reply_id == next(iter(harness.backend.stored_replies))


def test_rate_limit_honours_retry_after(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    harness.deliver_reply(inquiry, fire_event=False)
    harness.backend.fail(
        "/v1/mail-workers/replies", harness.backend.status(429, code="RATE_LIMITED", retry_after=600)
    )
    worker = harness.worker()
    assert worker.start().uploads["deferred_transient"] == 1
    row = harness.store.backlog_rows()[0]
    assert row.next_attempt_at - harness.clock.now() >= timedelta(seconds=600)
    harness.advance(timedelta(minutes=5))
    assert worker.tick().uploads == {}
    harness.advance(timedelta(minutes=6))
    assert worker.tick().uploads["acked"] == 1


def test_validation_rejection_and_idempotency_conflict_are_final_and_reported(harness: Harness) -> None:
    a, b = uuid4(), uuid4()
    harness.bind(a)
    harness.bind(b)
    harness.deliver_reply(a, fire_event=False)
    harness.deliver_reply(b, fire_event=False)
    harness.backend.fail(
        "/v1/mail-workers/replies",
        harness.backend.status(422, code="VALIDATION_ERROR"),
        harness.backend.status(409, code="IDEMPOTENCY_CONFLICT"),
    )
    worker = harness.worker()
    report = worker.start()
    assert report.uploads["rejected"] == 1 and report.uploads["conflict"] == 1
    stats = harness.store.backlog_stats()
    assert stats.rejected == 1 and stats.conflict == 1 and stats.pending == 0
    health = worker.health()
    assert health.backlog.status == "degraded"
    assert {"upload_rejected", "upload_conflict"} <= {g.kind for g in harness.store.gaps()}


def test_forbidden_upload_forces_a_binding_resync_before_more_uploads(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    harness.deliver_reply(inquiry, fire_event=False)
    harness.backend.fail("/v1/mail-workers/replies", harness.backend.status(403, code="FORBIDDEN"))
    worker = harness.worker()
    assert worker.start().uploads["deferred_resync"] == 1
    requests_before = len(harness.backend.requests)
    harness.advance(timedelta(minutes=1))
    report = worker.tick()
    assert report.binding_pages >= 1  # resynced first ...
    assert report.uploads["acked"] == 1  # ... then uploaded
    assert harness.backend.requests[requests_before][1] == "/v1/mail-workers/inquiry-bindings"


# --------------------------------------------------------------------------------------- revocation


def test_tombstoned_binding_stops_matching(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.backend.tombstone(inquiry)
    harness.advance(_interval(harness))
    worker.tick()  # the tombstone is synced
    harness.deliver_reply(inquiry, fire_event=True)
    report = worker.tick()
    assert report.events["unrelated"] == 1
    assert harness.reply_posts() == []
    assert harness.store.backlog_rows() == []


def test_tombstone_revokes_a_queued_upload_before_it_is_sent(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.backend.down = True
    harness.deliver_reply(inquiry, fire_event=True)
    worker.tick()
    assert harness.store.backlog_stats().pending == 1
    harness.backend.down = False
    harness.backend.tombstone(inquiry)
    harness.advance(_interval(harness))
    worker.tick()
    assert harness.reply_posts() == []
    assert harness.store.backlog_stats().revoked == 1


def test_no_upload_before_bindings_were_synced_in_this_session(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.backend.down = True
    harness.deliver_reply(inquiry, fire_event=True)
    worker.tick()
    # Restart while the binding endpoint fails: the cached binding must not be trusted for upload.
    harness.backend.down = False
    harness.backend.fail("/v1/mail-workers/inquiry-bindings", harness.backend.status(503, code="UNAVAILABLE"))
    restarted = harness.worker()
    restarted.start()
    assert harness.reply_posts() == []
    harness.advance(_interval(harness))
    restarted.tick()
    assert len(harness.reply_posts()) == 1


def test_revoked_worker_identity_stops_all_transmission_and_keeps_the_backlog(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.backend.down = True
    harness.deliver_reply(inquiry, fire_event=True)
    worker.tick()
    harness.backend.down = False
    harness.backend.forbid_worker = True
    harness.advance(_interval(harness))
    worker.tick()
    assert harness.credentials.state(harness.clock.now()) == CredentialState.REJECTED
    seen = len(harness.backend.requests)
    for _ in range(3):
        harness.advance(_interval(harness))
        worker.tick()
    assert len(harness.backend.requests) == seen  # nothing more is transmitted
    assert harness.store.backlog_stats().pending == 1  # the backlog is kept


# ------------------------------------------------------------------------------------ cross-mailbox


def test_binding_page_for_another_mailbox_is_rejected_client_side(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry, mailbox_binding_id=OTHER_MAILBOX_ID)
    harness.deliver_reply(inquiry, fire_event=False)
    worker = harness.worker()
    report = worker.start()
    assert report.binding_pages == 0
    assert harness.store.binding_cursor() is None  # the page and its cursor were not applied
    assert harness.store.binding_state(inquiry) is None
    assert harness.reply_posts() == []
    assert any(g.kind == "cross_mailbox_binding_rejected" for g in harness.store.gaps())


def test_tampered_upload_for_another_mailbox_is_refused_locally(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.backend.down = True
    harness.deliver_reply(inquiry, fire_event=True)
    worker.tick()
    row = harness.store.backlog_rows()[0]
    payload = json.loads(row.request_json)
    payload["mailbox_binding_id"] = str(OTHER_MAILBOX_ID)
    harness.store._db.execute(
        "update upload_backlog set request_json = ? where id = ?", (json.dumps(payload), row.id)
    )
    harness.backend.down = False
    harness.advance(timedelta(minutes=2))
    report = worker.tick()
    assert report.uploads["refused_locally"] == 1
    assert harness.reply_posts() == []
    assert harness.store.backlog_rows()[0].state == BacklogState.REJECTED


def test_store_and_worker_refuse_another_mailbox(harness: Harness, tmp_path: Path) -> None:
    store = LocalStore.in_memory(OTHER_MAILBOX_ID)
    try:
        BridgeWorker(
            config=harness.config,
            store=store,
            mailbox=harness.session,
            api=harness.api,
            credentials=CredentialManager(harness.cred_store, store),
            clock=harness.clock,
            compat=harness.compat,
        )
    except Exception as exc:
        assert type(exc).__name__ == "MailboxMismatch"
    else:  # pragma: no cover - must not happen
        raise AssertionError("a worker must refuse a store of another mailbox")
    finally:
        store.close()


# -------------------------------------------------------------------------------------- checkpoints


def test_checkpoint_advances_only_after_server_acknowledgement(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.backend.down = True
    item = harness.deliver_reply(
        inquiry, fire_event=False, received_at=harness.clock.now() - timedelta(minutes=30)
    )
    harness.advance(_interval(harness))
    worker.tick()
    key = _inbox_key(harness)
    checkpoint = harness.store.folder_checkpoint(key)
    assert checkpoint is not None and checkpoint.scan_watermark is not None
    received = harness.store.backlog_rows()[0].received_at
    assert received < checkpoint.scan_watermark  # the local scan moved on (durable local commit) ...
    assert harness.store.acknowledged_watermark(key) == received  # ... the acknowledged one did not
    envelope, _ = heartbeat_envelope(
        harness.store, config=harness.config, now=harness.clock.now(), connection=None
    )
    inbox = next(cp for cp in envelope.checkpoints if cp.folder_role == "inbox")
    assert inbox.acknowledged_watermark == received and inbox.backlog_count == 1
    harness.backend.down = False
    harness.advance(timedelta(minutes=1))
    worker.tick()
    assert harness.store.acknowledged_watermark(key) == harness.store.folder_checkpoint(key).scan_watermark  # type: ignore[union-attr]
    assert item.EntryID in harness.outlook.root.item_reads


def test_scan_watermark_does_not_advance_past_an_uncommitted_item(harness: Harness, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    key = _inbox_key(harness)
    before = harness.store.folder_checkpoint(key).scan_watermark  # type: ignore[union-attr]
    harness.deliver_reply(inquiry, fire_event=False)
    original = LocalMatcher.prepare_upload
    calls = {"n": 0}

    def flaky(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash before the durable commit")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(LocalMatcher, "prepare_upload", flaky)
    harness.advance(_interval(harness))
    report = worker.tick()
    assert report.scan["read_failed"] == 1
    checkpoint = harness.store.folder_checkpoint(key)
    assert checkpoint is not None and checkpoint.scan_watermark == before
    assert "item_read_failed" in checkpoint.gap_reasons
    harness.advance(_interval(harness))
    worker.tick()
    assert len(harness.reply_posts()) == 1
    assert harness.store.folder_checkpoint(key).scan_watermark > before  # type: ignore[union-attr,operator]


# -------------------------------------------------------------------------------------- credentials


def test_credential_expiry_stops_transmission_and_keeps_the_backlog(harness: Harness) -> None:
    harness.cred_store.save(
        WorkerCredential(token=TOKEN, expires_at=harness.clock.now() + timedelta(minutes=10))
    )
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.advance(timedelta(minutes=15))
    seen = len(harness.backend.requests)
    harness.deliver_reply(inquiry, fire_event=True)
    worker.tick()
    harness.advance(_interval(harness))
    worker.tick()
    assert len(harness.backend.requests) == seen  # no transmission with an expired credential
    assert harness.store.backlog_stats().pending == 1  # correlated locally and kept
    health = worker.health()
    assert health.server.credential_state == CredentialState.EXPIRED and health.server.status == "down"
    assert any(g.kind == "credential_unusable" and g.ended_at is None for g in harness.store.gaps())
    harness.backend.tokens.add(TOKEN_2)
    harness.credentials.replace(WorkerCredential(token=TOKEN_2))
    harness.advance(_interval(harness))
    worker.tick()
    assert len(harness.reply_posts()) == 1
    assert all(g.ended_at is not None for g in harness.store.gaps() if g.kind == "credential_unusable")


def test_rejected_credential_stops_transmission_until_a_new_one_is_stored(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.backend.revoke_token(TOKEN)
    harness.deliver_reply(inquiry, fire_event=True)
    worker.tick()
    assert harness.credentials.state(harness.clock.now()) == CredentialState.REJECTED
    seen = len(harness.backend.requests)
    for _ in range(3):
        harness.advance(_interval(harness))
        worker.tick()
    assert len(harness.backend.requests) == seen
    assert harness.store.backlog_stats().pending == 1
    harness.backend.tokens.add(TOKEN_2)
    harness.credentials.replace(WorkerCredential(token=TOKEN_2))
    harness.advance(_interval(harness))
    worker.tick()
    assert len(harness.reply_posts()) == 1
    assert harness.backend.reply_headers[-1]["authorization"] == f"Bearer {TOKEN_2}"


# ------------------------------------------------------------------------------------ coverage gaps


def test_sleep_gap_is_recorded_and_mail_from_the_gap_is_recovered(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    asleep_at = harness.clock.now()
    # The laptop sleeps for three hours; Outlook synchronises the reply after wake-up without an event.
    harness.advance(timedelta(hours=3))
    harness.deliver_reply(inquiry, fire_event=False, received_at=asleep_at + timedelta(hours=1))
    report = worker.tick()
    assert report.scan["uploaded"] == 1
    gaps = [g for g in harness.store.gaps() if g.kind == "worker_suspended"]
    assert len(gaps) == 1 and gaps[0].started_at == asleep_at and gaps[0].ended_at == harness.clock.now()
    checkpoint = harness.store.folder_checkpoint(_inbox_key(harness))
    assert checkpoint is not None and "watermark_held_by_coverage_gap" in checkpoint.gap_reasons
    assert checkpoint.scan_watermark is not None and checkpoint.scan_watermark <= asleep_at
    health = worker.health()
    assert health.monitoring_24h_claimed is False and health.coverage_statement == COVERAGE_STATEMENT
    assert any(g.kind == "worker_suspended" for g in health.gaps)


def test_offline_restart_records_the_gap_and_recovers_the_durable_backlog(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    store_path = tmp_path / "state.sqlite3"
    h.store.close()
    h.store = LocalStore.open(store_path, MAILBOX_ID)
    h.credentials = CredentialManager(h.cred_store, h.store)
    inquiry = uuid4()
    h.bind(inquiry)
    worker = h.worker()
    worker.start()
    h.backend.down = True
    h.deliver_reply(inquiry, fire_event=True)
    worker.tick()
    assert h.store.backlog_stats().pending == 1
    stopped_at = h.clock.now()
    worker.shutdown()
    h.store.close()
    # Powered off for five hours; another reply arrives meanwhile (no worker, no event).
    h.advance(timedelta(hours=5))
    other = uuid4()
    h.bind(other)
    h.deliver_reply(other, fire_event=False, received_at=stopped_at + timedelta(hours=2))
    h.backend.down = False
    h.store = LocalStore.open(store_path, MAILBOX_ID)
    h.credentials = CredentialManager(h.cred_store, h.store)
    restarted = h.worker()
    restarted.start()
    assert {p["inquiry_id"] for p in h.reply_posts()} == {str(inquiry), str(other)}
    gaps = [g for g in h.store.gaps() if g.kind == "worker_offline"]
    assert len(gaps) == 1 and gaps[0].ended_at == h.clock.now()
    reported = h.backend.heartbeats[-1]["gaps"]
    assert any(g["kind"] == "worker_offline" for g in reported)
    h.close()


def test_outlook_closed_is_an_honest_gap_and_the_backlog_still_flows(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.backend.down = True
    harness.deliver_reply(inquiry, fire_event=True)
    worker.tick()
    harness.outlook.set_running(False)
    harness.backend.down = False
    harness.advance(_interval(harness))
    report = worker.tick()
    assert worker.connected is False
    assert report.uploads["acked"] == 1  # the committed backlog does not need Outlook
    beat = harness.backend.heartbeats[-1]["heartbeat"]
    assert beat["outlook_running"] is False and beat["mailbox_connected"] is False
    assert any(g.kind == "outlook_not_running" and g.ended_at is None for g in harness.store.gaps())
    harness.outlook.set_running(True)
    other = uuid4()
    harness.bind(other)
    harness.deliver_reply(other, fire_event=False)
    harness.advance(timedelta(seconds=5))
    worker.tick()  # reattached: bindings and reconciliation run at once
    assert worker.connected is True
    assert {p["inquiry_id"] for p in harness.reply_posts()} == {str(inquiry), str(other)}
    assert all(g.ended_at is not None for g in harness.store.gaps() if g.kind == "outlook_not_running")


def test_disconnected_mailbox_is_reported_separately(harness: Harness) -> None:
    worker = harness.worker()
    worker.start()
    harness.outlook.set_offline(True)
    harness.advance(_interval(harness))
    worker.tick()
    health = worker.health()
    assert health.outlook.status == "degraded" and health.outlook.connected is False
    assert any(g.kind == "outlook_disconnected" for g in harness.store.gaps())


def test_heartbeat_reports_health_dimensions_separately(harness: Harness) -> None:
    harness.bind(uuid4())
    worker = harness.worker()
    worker.start()
    beat = harness.backend.heartbeats[-1]
    assert beat["schema_version"] == "1.0"
    assert beat["heartbeat"]["mailbox_binding_id"] == str(MAILBOX_ID)
    assert beat["heartbeat"]["outlook_running"] is True
    assert beat["last_successful_reconciliation_at"] is not None
    assert {cp["folder_role"] for cp in beat["checkpoints"]} == {"inbox", "junk", "rule_target"}
    health = worker.health()
    assert health.downstream == {"mcp": "ok", "slack_signal": "unverified"}  # observed by the backend only
    assert health.worker.status == "ok" and health.reconciliation.status == "ok"
    assert health.binding_sync.status == "ok" and health.server.status == "ok"
    assert harness.backend.account_reports[-1]["outlook_flavour"] == "classic"
    assert harness.backend.account_reports[-1]["security_settings_unchanged"] is True


def test_dry_run_correlates_but_never_transmits_mail_data(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    harness.deliver_reply(inquiry, fire_event=False)
    worker = harness.worker(dry_run=True)
    report = worker.start()
    assert report.scan["uploaded"] == 1  # would upload (queued locally only)
    methods = {method for method, _ in harness.backend.requests}
    assert methods == {"GET"}  # bindings only: no reply, report, heartbeat or account report
    assert harness.reply_posts() == [] and harness.outlook.send_calls == []


def test_inquiry_message_ids_reference_helper() -> None:
    inquiry = uuid4()
    assert inquiry_message_id(inquiry).startswith(f"<inquiry-{inquiry}.1@")


# ------------------------------------------------------------------------------- binding sync / account


def test_binding_sync_must_reach_the_end_of_the_change_log_before_uploads(
    make_harness: Callable[..., Harness],
) -> None:
    h = make_harness(binding_page_limit=1)
    first = uuid4()
    h.bind(first)
    for _ in range(MAX_BINDING_PAGES_PER_SYNC + 5):
        h.bind(uuid4())  # more changes than one sync pass reads
    h.deliver_reply(first, fire_event=False)
    worker = h.worker()
    report = worker.start()
    assert report.binding_pages == MAX_BINDING_PAGES_PER_SYNC
    assert h.reply_posts() == []  # a later page could still hold a tombstone
    report = worker.tick()  # the forced continuation reads the rest
    assert report.binding_pages == 6
    assert len(h.reply_posts()) == 1


def test_binding_sync_that_cannot_progress_fails_closed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    harness.deliver_reply(inquiry, fire_event=False)
    stuck = BindingPage(schema_version="1.0", items=(), next_cursor=None, has_more=True)
    monkeypatch.setattr(harness.api, "fetch_binding_page", lambda cursor, *, limit: stuck)
    harness.worker().start()
    assert harness.reply_posts() == []
    assert any(g.kind == "binding_sync_incomplete" for g in harness.store.gaps())


def test_changing_the_configured_account_never_silently_reassigns_the_binding(harness: Harness) -> None:
    harness.worker().start()
    other_inbox = harness.outlook.folder(harness.other_account, "inbox")
    inquiry = uuid4()
    harness.bind(inquiry)
    item = harness.deliver_reply(inquiry, folder=other_inbox, fire_event=False)
    harness.config = make_config(
        harness.tmp_path / "data", account_smtp_address="private@other.example.invalid"
    )
    worker = harness.worker()
    worker.start()
    assert worker.connected is False
    assert item.EntryID not in harness.outlook.root.item_reads
    assert harness.reply_posts() == []
    assert any(g.kind == "account_binding_mismatch" and g.ended_at is None for g in harness.store.gaps())


# ------------------------------------------------------------------------------- review regressions


def test_rejected_binding_page_mid_session_stops_uploads_until_a_clean_sync(harness: Harness) -> None:
    """A later page for another mailbox may also carry a revocation: uploads stop (fail closed)."""
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.backend.tombstone(inquiry)  # the revocation ...
    harness.bind(uuid4(), mailbox_binding_id=OTHER_MAILBOX_ID)  # ... arrives in a poisoned page
    harness.deliver_reply(inquiry, fire_event=False)
    harness.advance(_interval(harness))
    worker.tick()
    assert harness.reply_posts() == []  # the stale cached binding granted nothing
    assert harness.store.backlog_stats().pending == 1  # correlated locally and kept
    assert any(g.kind == "binding_sync_incomplete" and g.ended_at is None for g in harness.store.gaps())
    assert sum(1 for g in harness.store.gaps() if g.kind == "cross_mailbox_binding_rejected") == 1
    fetches = sum(1 for _, path in harness.backend.requests if path.endswith("inquiry-bindings"))
    for _ in range(3):  # the same bad page is not re-fetched every tick
        harness.advance(timedelta(seconds=5))
        worker.tick()
    assert sum(1 for _, path in harness.backend.requests if path.endswith("inquiry-bindings")) == fetches
    # The server drops the foreign item: the next clean sync applies the tombstone and revokes.
    harness.backend.binding_log = [
        i for i in harness.backend.binding_log if i["mailbox_binding_id"] == str(MAILBOX_ID)
    ]
    harness.advance(_interval(harness))
    worker.tick()
    assert harness.reply_posts() == []
    assert harness.store.backlog_stats().revoked == 1
    assert all(g.ended_at is not None for g in harness.store.gaps() if g.kind == "binding_sync_incomplete")


def test_binding_sync_that_stops_progressing_mid_session_fails_closed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    calls = {"n": 0}
    stuck = BindingPage(schema_version="1.0", items=(), next_cursor=None, has_more=True)

    def fetch(cursor: str | None, *, limit: int) -> BindingPage:
        calls["n"] += 1
        return stuck

    monkeypatch.setattr(harness.api, "fetch_binding_page", fetch)
    harness.deliver_reply(inquiry, fire_event=False)
    harness.advance(_interval(harness))
    worker.tick()
    assert harness.reply_posts() == [] and harness.store.backlog_stats().pending == 1
    assert any(g.kind == "binding_sync_incomplete" and g.ended_at is None for g in harness.store.gaps())
    for _ in range(3):
        harness.advance(timedelta(seconds=5))
        worker.tick()
    assert calls["n"] == 1  # retried at the next interval, not every tick


def test_a_failing_first_cycle_keeps_the_periodic_schedule(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deep reconciliation still runs after a start cycle that raised (``run`` keeps ticking)."""
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()

    def broken_heartbeat(now: object) -> None:
        raise RuntimeError("simulated failure late in the first cycle")

    monkeypatch.setattr(worker, "_heartbeat", broken_heartbeat)
    worker._guarded(worker.start)
    monkeypatch.undo()
    harness.advance(timedelta(hours=1))
    worker.tick()
    # A server message received hours ago that synchronises only now (outside the overlap window).
    harness.deliver_reply(inquiry, fire_event=False, received_at=harness.clock.now() - timedelta(hours=3))
    harness.advance(_interval(harness))
    worker.tick()
    assert harness.reply_posts() == []
    harness.advance(timedelta(hours=24))
    worker.tick()  # the daily deep scan is still scheduled
    assert len(harness.reply_posts()) == 1


def test_unresolvable_configured_folder_is_an_open_gap_not_a_silent_partial_scan(
    make_harness: Callable[..., Harness], caplog: pytest.LogCaptureFixture
) -> None:
    h = make_harness(folders=[{"role": "inbox"}, {"role": "rule_target", "path": "Inbox/Typo"}])
    inquiry = uuid4()
    h.bind(inquiry)
    item = h.deliver_reply(inquiry, fire_event=False)
    worker = h.worker()
    worker.start()  # never raises: an honest, reported gap instead
    assert worker.connected is False and worker.folders == ()
    assert item.EntryID not in h.outlook.root.item_reads
    for _ in range(3):
        h.advance(timedelta(seconds=5))
        worker.tick()
    gaps = [g for g in h.store.gaps() if g.kind == "folder_scope_invalid"]
    assert len(gaps) == 1 and gaps[0].ended_at is None
    assert sum(1 for r in caplog.records if r.getMessage() == "folder_scope_invalid") == 1  # logged once
    assert worker.health().reconciliation.status == "degraded"
    assert any(g["kind"] == "folder_scope_invalid" for g in h.backend.heartbeats[-1]["gaps"])
    h.outlook.create_folder(h.inbox, "Typo")  # the owner fixes the folder: reattached at once
    h.advance(timedelta(seconds=5))
    worker.tick()
    assert worker.connected is True
    assert len(h.reply_posts()) == 1
    assert all(g.ended_at is not None for g in h.store.gaps() if g.kind == "folder_scope_invalid")


def test_refused_account_change_neither_leaks_sinks_nor_floods_the_log(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.worker().start()
    harness.config = make_config(
        harness.tmp_path / "data", account_smtp_address="private@other.example.invalid"
    )
    worker = harness.worker()
    worker.start()
    for _ in range(10):
        harness.advance(timedelta(seconds=3))
        worker.tick()
    sinks = [h for h in harness.session._state.events if isinstance(h, SyncEventHandler)]
    assert len(sinks) == 1
    assert sum(1 for r in caplog.records if r.getMessage() == "account_binding_mismatch") == 1
    assert worker.connected is False


def test_conflicting_binding_payload_recovers_with_the_next_server_version(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    worker = harness.worker()
    worker.start()
    harness.backend.publish_binding(  # a server bug: same version, different payload
        inquiry,
        version=1,
        outbound_message_ids=[inquiry_message_id(inquiry)],
        aliases=["changed@dealer.example.invalid"],
    )
    harness.deliver_reply(inquiry, fire_event=False)
    harness.advance(_interval(harness))
    worker.tick()
    assert harness.reply_posts() == []  # fail closed while the conflict is unresolved
    assert harness.store.binding_sync_status()["anomalies"] == 1
    assert harness.store.pending_count() == 1  # kept as a bounded locator, not discarded
    harness.bind(inquiry)  # the server publishes version 2
    harness.advance(_interval(harness))
    worker.tick()
    assert harness.store.binding_state(inquiry) == (InquiryBindingState.ACTIVE, 2)
    posts = harness.reply_posts()
    assert len(posts) == 1 and posts[0]["binding_version"] == 2
    assert harness.store.pending_count() == 0
