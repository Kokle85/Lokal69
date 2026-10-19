"""Protected local SQLite store: bindings cache, processed keys, backlog, checkpoints, intents."""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from bridge_support import MAILBOX_ID, OTHER_MAILBOX_ID, SELLER, START
from outlook_bridge.errors import LocalStoreError, MailboxMismatch
from outlook_bridge.local_queue import (
    BacklogEntry,
    BacklogState,
    BindingRecord,
    IntentState,
    LocalStore,
    Outcome,
    PendingLocator,
    backoff_delay,
    message_id_hash,
)
from outlook_bridge.testing import inquiry_message_id

from suv_deals.domain.enums import EmailProviderKind
from suv_deals.domain.replies import InquiryBinding, InquiryBindingState


def _binding(
    inquiry: UUID, version: int, *, mailbox: UUID = MAILBOX_ID, alias: str = SELLER
) -> BindingRecord:
    binding = InquiryBinding(
        inquiry_id=inquiry,
        binding_version=version,
        mailbox_binding_id=mailbox,
        provider=EmailProviderKind.OUTLOOK_LOCAL,
        outbound_message_ids=(inquiry_message_id(inquiry),),
        verified_seller_aliases=(alias,),
    )
    return BindingRecord(
        inquiry_id=binding.inquiry_id,
        binding_version=version,
        mailbox_binding_id=binding.mailbox_binding_id,
        state=binding.state,
        binding=binding,
    )


def _tombstone(inquiry: object, version: int) -> BindingRecord:
    return BindingRecord(
        inquiry_id=inquiry,  # type: ignore[arg-type]
        binding_version=version,
        mailbox_binding_id=MAILBOX_ID,
        state=InquiryBindingState.TOMBSTONED,
        binding=None,
    )


@pytest.fixture
def store() -> LocalStore:
    return LocalStore.in_memory(MAILBOX_ID)


def test_file_store_uses_wal_owner_only_permissions_and_is_mailbox_bound(tmp_path: Path) -> None:
    path = tmp_path / "state" / "bridge-state.sqlite3"
    store = LocalStore.open(path, MAILBOX_ID)
    assert store.journal_mode().lower() == "wal"
    synchronous = store._db.execute("pragma synchronous").fetchone()
    assert synchronous is not None and int(synchronous[0]) == 2  # FULL
    store.set_runtime("k", "v")
    store.close()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    reopened = LocalStore.open(path, MAILBOX_ID)
    assert reopened.get_runtime("k") == "v"
    reopened.close()
    with pytest.raises(MailboxMismatch):
        LocalStore.open(path, OTHER_MAILBOX_ID)


def test_binding_page_and_cursor_commit_atomically(store: LocalStore) -> None:
    a, b = uuid4(), uuid4()
    result = store.apply_binding_page([_binding(a, 1), _binding(b, 1)], "c2", complete=True, now=START)
    assert result.applied == 2
    assert store.binding_cursor() == "c2"
    assert len(result.new_message_id_hashes) == 2
    # A page containing a binding of another mailbox rolls back completely (no partial apply).
    c = uuid4()
    with pytest.raises(MailboxMismatch):
        store.apply_binding_page(
            [_binding(c, 1), _binding(uuid4(), 1, mailbox=OTHER_MAILBOX_ID)], "c9", complete=True, now=START
        )
    assert store.binding_cursor() == "c2"
    assert store.binding_state(c) is None
    status = store.binding_sync_status()
    assert status["has_cursor"] and status["pages"] == 1


def test_binding_versions_only_grow_and_tombstones_are_final(store: LocalStore) -> None:
    inquiry = uuid4()
    store.apply_binding_page([_binding(inquiry, 2)], "c1", complete=True, now=START)
    stale = store.apply_binding_page(
        [_binding(inquiry, 1, alias="other@dealer.example.invalid")], "c2", complete=True, now=START
    )
    assert stale.ignored_stale == 1
    assert store.binding_state(inquiry) == (InquiryBindingState.ACTIVE, 2)
    same = store.apply_binding_page([_binding(inquiry, 2)], "c3", complete=True, now=START)
    assert same.applied == 0 and same.conflicts == 0
    tomb = store.apply_binding_page([_tombstone(inquiry, 3)], "c4", complete=True, now=START)
    assert tomb.tombstoned == (inquiry,)
    assert store.binding_state(inquiry) == (InquiryBindingState.TOMBSTONED, 3)
    revived = store.apply_binding_page([_binding(inquiry, 9)], "c5", complete=True, now=START)
    assert revived.ignored_stale == 1
    assert store.binding_state(inquiry) == (InquiryBindingState.TOMBSTONED, 3)
    # Tombstones are handed to matching with their revoked ids (they never grant access).
    tombstones = [b for b in store.bindings_for_matching() if b.inquiry_id == inquiry]
    assert tombstones[0].state == InquiryBindingState.TOMBSTONED
    assert tombstones[0].outbound_message_ids == (inquiry_message_id(inquiry),)


def test_two_payloads_under_one_version_fail_closed(store: LocalStore) -> None:
    inquiry = uuid4()
    store.apply_binding_page([_binding(inquiry, 1)], "c1", complete=True, now=START)
    result = store.apply_binding_page(
        [_binding(inquiry, 1, alias="changed@dealer.example.invalid")], "c2", complete=True, now=START
    )
    assert result.conflicts == 1
    assert store.binding_state(inquiry) == (InquiryBindingState.TOMBSTONED, 1)
    assert store.binding_sync_status()["anomalies"] == 1


def test_incomplete_page_does_not_mark_sync_complete(store: LocalStore) -> None:
    store.apply_binding_page([], "c1", complete=False, now=START)
    assert store.binding_sync_status()["last_complete_at"] is None
    store.apply_binding_page([], None, complete=True, now=START)
    assert store.binding_cursor() == "c1"  # a missing next cursor keeps the last one


def _entry(key: str, inquiry: object, received: object = START) -> BacklogEntry:
    return BacklogEntry(
        idempotency_key=f"mwr1-{key}-0123456789",
        dedup_key_hash=key,
        inquiry_id=inquiry,  # type: ignore[arg-type]
        binding_version=1,
        folder_key="folder-a",
        received_at=received,  # type: ignore[arg-type]
        request_json='{"schema_version":"1.0"}',
    )


def test_outcome_and_backlog_are_committed_together_and_deduplicated(store: LocalStore) -> None:
    inquiry = uuid4()
    first = store.record_outcome(
        key_hash="k1",
        outcome=Outcome.UPLOAD_QUEUED,
        now=START,
        inquiry_id=inquiry,
        backlog=_entry("k1", inquiry),
        folder_key="folder-a",
    )
    assert first is not None
    again = store.record_outcome(
        key_hash="k1",
        outcome=Outcome.UPLOAD_QUEUED,
        now=START,
        inquiry_id=inquiry,
        backlog=_entry("k1", inquiry),
        folder_key="folder-a",
    )
    assert again is None  # same source message: never a second backlog entry
    assert len(store.backlog_rows()) == 1
    with pytest.raises(LocalStoreError):
        store.record_outcome(
            key_hash="k2", outcome=Outcome.UNRELATED, now=START, backlog=_entry("k2", inquiry)
        )
    with pytest.raises(LocalStoreError):
        store.record_outcome(key_hash="k3", outcome=Outcome.PENDING_BINDING, now=START)
    assert store.processed("k2") is None  # the failed transaction left nothing behind


def test_backlog_failure_rolls_back_the_outcome(store: LocalStore) -> None:
    inquiry = uuid4()
    store.record_outcome(
        key_hash="k1",
        outcome=Outcome.UPLOAD_QUEUED,
        now=START,
        inquiry_id=inquiry,
        backlog=_entry("k1", inquiry),
    )
    clash = BacklogEntry(
        idempotency_key=_entry("k1", inquiry).idempotency_key,  # unique idempotency key violated
        dedup_key_hash="k2",
        inquiry_id=inquiry,
        binding_version=1,
        folder_key="folder-a",
        received_at=START,
        request_json="{}",
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.record_outcome(key_hash="k2", outcome=Outcome.UPLOAD_QUEUED, now=START, backlog=clash)
    assert store.processed("k2") is None


def test_acknowledged_watermark_never_passes_unacknowledged_items(store: LocalStore) -> None:
    store.upsert_folder(folder_key="folder-a", role="inbox", store_id_hash="s" * 64, folder_id_hash="f" * 64)
    store.begin_scan("folder-a", START)
    store.finish_scan("folder-a", complete=True, new_watermark=START, gap_reasons=(), now=START)
    assert store.acknowledged_watermark("folder-a") == START
    inquiry = uuid4()
    received = START - timedelta(hours=2)
    backlog_id = store.record_outcome(
        key_hash="k1",
        outcome=Outcome.UPLOAD_QUEUED,
        now=START,
        inquiry_id=inquiry,
        backlog=_entry("k1", inquiry, received),
    )
    assert backlog_id is not None
    assert store.acknowledged_watermark("folder-a") == received
    store.mark_upload_deferred(
        backlog_id, next_attempt_at=START + timedelta(minutes=1), error_code="transport"
    )
    assert store.acknowledged_watermark("folder-a") == received
    store.mark_upload_acked(backlog_id, reply_id=uuid4(), duplicate=False, request_id="req-1", now=START)
    assert store.acknowledged_watermark("folder-a") == START
    # A pending reply-before-binding locator also holds the acknowledged checkpoint back.
    pending_received = START - timedelta(hours=1)
    store.record_outcome(
        key_hash="k2",
        outcome=Outcome.PENDING_BINDING,
        now=START,
        pending=PendingLocator(
            key_hash="k2",
            internet_message_id="<r@x.example.invalid>",
            entry_id="E",
            store_id="S",
            folder_key="folder-a",
            received_at=pending_received,
            reference_hashes=frozenset({"h"}),
            own_reference=True,
            first_seen_at=START,
            retry_until=START + timedelta(hours=24),
        ),
    )
    assert store.acknowledged_watermark("folder-a") == pending_received


def test_scan_watermark_is_monotonic_and_only_moves_on_complete_scans(store: LocalStore) -> None:
    store.upsert_folder(folder_key="f", role="inbox", store_id_hash="s" * 64, folder_id_hash="f" * 64)
    store.finish_scan("f", complete=True, new_watermark=START, gap_reasons=(), now=START)
    store.finish_scan(
        "f", complete=False, new_watermark=START + timedelta(hours=1), gap_reasons=("x",), now=START
    )
    checkpoint = store.folder_checkpoint("f")
    assert checkpoint is not None and checkpoint.scan_watermark == START
    assert checkpoint.gap_reasons == ("x",)
    store.finish_scan("f", complete=True, new_watermark=START - timedelta(hours=1), gap_reasons=(), now=START)
    checkpoint = store.folder_checkpoint("f")
    assert checkpoint is not None and checkpoint.scan_watermark == START
    with pytest.raises(LocalStoreError):
        store.finish_scan("missing", complete=True, new_watermark=START, gap_reasons=(), now=START)


def test_pending_locators_are_bounded_and_expire(store: LocalStore) -> None:
    for index in range(5):
        store.record_outcome(
            key_hash=f"p{index}",
            outcome=Outcome.PENDING_BINDING,
            now=START,
            pending=PendingLocator(
                key_hash=f"p{index}",
                internet_message_id=None,
                entry_id=f"E{index}",
                store_id="S",
                folder_key="f",
                received_at=START,
                reference_hashes=frozenset({message_id_hash(f"<m{index}@x>")}),
                own_reference=index % 2 == 0,
                first_seen_at=START + timedelta(seconds=index),
                retry_until=START + timedelta(hours=1 + index),
            ),
        )
    evicted = store.evict_pending_over(3, START)
    assert {p.key_hash for p in evicted} == {"p1", "p3"}  # foreign-reference locators go first
    assert store.pending_count() == 3
    assert [p.key_hash for p in store.pending_matching({message_id_hash("<m2@x>")})] == ["p2"]
    expired = store.take_expired_pending(START + timedelta(hours=1, minutes=30))
    assert [p.key_hash for p in expired] == ["p0"]
    row = store.processed("p0")
    assert row is not None and row.outcome == Outcome.MATCHING_GAP
    row = store.processed("p1")
    assert row is not None and row.outcome == Outcome.UNRELATED


def test_pending_locator_rows_hold_metadata_only(store: LocalStore) -> None:
    columns = {r[1] for r in store._db.execute("pragma table_info(pending_matches)")}
    assert columns == {
        "key_hash",
        "internet_message_id",
        "entry_id",
        "store_id",
        "folder_key",
        "received_at",
        "reference_hashes",
        "own_reference",
        "first_seen_at",
        "retry_until",
        "attempts",
    }
    processed = {r[1] for r in store._db.execute("pragma table_info(processed)")}
    assert not processed & {"subject", "body", "sender", "headers", "from_address"}


def test_locator_history_records_moves(store: LocalStore) -> None:
    store.record_outcome(
        key_hash="k", outcome=Outcome.UNRELATED, now=START, entry_id="E1", store_id="S", folder_key="inbox"
    )
    assert store.record_locator("k", entry_id="E1", store_id="S", folder_key="inbox", now=START) is False
    assert store.record_locator("k", entry_id="E2", store_id="S", folder_key="cars", now=START) is True
    assert store.locator_history("k") == [("E2", "S", "cars")]
    assert store.record_locator("missing", entry_id="E", store_id="S", folder_key=None, now=START) is False


def test_send_intents_are_never_attempted_twice(store: LocalStore) -> None:
    intent, inquiry = uuid4(), uuid4()
    assert store.record_intent_received(intent_id=intent, inquiry_id=inquiry, payload_json="{}", now=START)
    assert not store.record_intent_received(
        intent_id=intent, inquiry_id=inquiry, payload_json="{}", now=START
    )
    assert store.begin_attempt(intent, START) is True
    assert store.begin_attempt(intent, START) is False
    assert store.attempted_since(START - timedelta(seconds=1)) == 1
    other = uuid4()
    store.record_intent_received(intent_id=other, inquiry_id=inquiry, payload_json="{}", now=START)
    assert store.inquiry_attempted_elsewhere(inquiry, intent_id=other) is True
    assert store.inquiry_attempted_elsewhere(inquiry, intent_id=intent) is False
    with pytest.raises(LocalStoreError):
        store.finish_intent(intent, state=IntentState.ATTEMPTING, report_json="{}", now=START)
    store.finish_intent(intent, state=IntentState.SUBMITTED, report_json='{"a":1}', now=START)
    row = store.intent(intent)
    assert row is not None and row.state == IntentState.SUBMITTED and not row.report_acked
    store.mark_report_acked(intent, '{"other":1}')
    assert store.intent(intent).report_acked is False  # type: ignore[union-attr]
    store.mark_report_acked(intent, '{"a":1}')
    assert store.intent(intent).report_acked is True  # type: ignore[union-attr]


def test_refused_intents_do_not_block_a_later_intent(store: LocalStore) -> None:
    refused, later, inquiry = uuid4(), uuid4(), uuid4()
    store.record_intent_received(intent_id=refused, inquiry_id=inquiry, payload_json="{}", now=START)
    store.finish_intent(refused, state=IntentState.REFUSED, report_json="{}", now=START)
    assert store.inquiry_attempted_elsewhere(inquiry, intent_id=later) is False


def test_gaps_open_close_and_report(store: LocalStore) -> None:
    store.open_gap("outlook_not_running", START)
    store.open_gap("outlook_not_running", START + timedelta(minutes=1))  # already open: no duplicate
    assert len(store.gaps(kinds={"outlook_not_running"})) == 1
    store.close_gap("outlook_not_running", START + timedelta(minutes=5))
    gap = store.gaps()[0]
    assert gap.ended_at == START + timedelta(minutes=5)
    store.mark_gaps_reported([gap.id])
    assert store.gaps()[0].reported is True
    store.record_gap("worker_offline", START, START - timedelta(hours=1))  # end never before start
    assert store.gaps(kinds={"worker_offline"})[0].ended_at == START


def test_backlog_final_states_and_prune(store: LocalStore) -> None:
    inquiry = uuid4()
    first = store.record_outcome(
        key_hash="k1",
        outcome=Outcome.UPLOAD_QUEUED,
        now=START,
        inquiry_id=inquiry,
        backlog=_entry("k1", inquiry),
    )
    assert first is not None
    with pytest.raises(LocalStoreError):
        store.mark_upload_final(first, state=BacklogState.ACKED, error_code="x", now=START)
    store.mark_upload_final(first, state=BacklogState.REVOKED, error_code="BINDING_REVOKED", now=START)
    stats = store.backlog_stats()
    assert stats.pending == 0 and stats.revoked == 1
    store.record_outcome(
        key_hash="old",
        outcome=Outcome.UNRELATED,
        now=START - timedelta(days=30),
        received_at=START - timedelta(days=30),
    )
    removed = store.prune(
        unrelated_before=START - timedelta(days=20), history_before=START - timedelta(days=180)
    )
    assert removed == 1
    assert store.processed("old") is None


def test_backoff_is_bounded() -> None:
    assert backoff_delay(0) == timedelta(seconds=30)
    assert backoff_delay(3) == timedelta(seconds=240)
    assert backoff_delay(100) == timedelta(hours=1)
    assert backoff_delay(-5) == timedelta(seconds=30)


# ------------------------------------------------------------------------------- review regressions


def test_conflict_tombstone_yields_to_a_strictly_newer_server_version(store: LocalStore) -> None:
    """A same-version payload conflict fails closed only *until* the server publishes a newer version."""
    inquiry = uuid4()
    store.apply_binding_page([_binding(inquiry, 1)], "c1", complete=True, now=START)
    store.apply_binding_page(
        [_binding(inquiry, 1, alias="changed@dealer.example.invalid")], "c2", complete=True, now=START
    )
    assert store.binding_state(inquiry) == (InquiryBindingState.TOMBSTONED, 1)
    assert not any(b.state != InquiryBindingState.TOMBSTONED for b in store.bindings_for_matching())
    same = store.apply_binding_page([_binding(inquiry, 1)], "c3", complete=True, now=START)
    assert same.ignored_stale == 1 and store.binding_state(inquiry) == (InquiryBindingState.TOMBSTONED, 1)
    newer = store.apply_binding_page([_binding(inquiry, 2)], "c4", complete=True, now=START)
    assert newer.applied == 1
    assert store.binding_state(inquiry) == (InquiryBindingState.ACTIVE, 2)
    assert message_id_hash(inquiry_message_id(inquiry)) in newer.new_message_id_hashes  # re-reads pending
    assert message_id_hash(inquiry_message_id(inquiry)) in store.known_message_id_hashes()


def test_a_server_tombstone_after_a_conflict_is_final(store: LocalStore) -> None:
    inquiry = uuid4()
    store.apply_binding_page([_binding(inquiry, 1)], "c1", complete=True, now=START)
    store.apply_binding_page(
        [_binding(inquiry, 1, alias="changed@dealer.example.invalid")], "c2", complete=True, now=START
    )
    store.apply_binding_page([_tombstone(inquiry, 2)], "c3", complete=True, now=START)
    assert store.binding_state(inquiry) == (InquiryBindingState.TOMBSTONED, 2)
    revived = store.apply_binding_page([_binding(inquiry, 3)], "c4", complete=True, now=START)
    assert revived.ignored_stale == 1
    assert store.binding_state(inquiry) == (InquiryBindingState.TOMBSTONED, 2)
    tombstones = [b for b in store.bindings_for_matching() if b.inquiry_id == inquiry]
    assert tombstones[0].outbound_message_ids == (inquiry_message_id(inquiry),)  # revoked ids kept


def test_intent_states_only_move_forward(store: LocalStore) -> None:
    """Nothing that may have been submitted can ever be re-labelled as 'refused before send'."""
    intent = uuid4()
    store.record_intent_received(intent_id=intent, inquiry_id=uuid4(), payload_json="{}", now=START)
    with pytest.raises(LocalStoreError):
        store.finish_intent(intent, state=IntentState.SUBMITTED, report_json="{}", now=START)  # no attempt
    assert store.begin_attempt(intent, START)
    store.finish_intent(intent, state=IntentState.SEND_FAILED, report_json="{}", now=START)
    with pytest.raises(LocalStoreError):
        store.finish_intent(intent, state=IntentState.REFUSED, report_json="{}", now=START)
    store.finish_intent(intent, state=IntentState.SUBMITTED, report_json="{}", now=START)
    with pytest.raises(LocalStoreError):
        store.finish_intent(intent, state=IntentState.SEND_FAILED, report_json="{}", now=START)
    store.finish_intent(intent, state=IntentState.CONFIRMED, report_json="{}", now=START)
    with pytest.raises(LocalStoreError):
        store.finish_intent(intent, state=IntentState.SUBMITTED, report_json="{}", now=START)
    with pytest.raises(LocalStoreError):
        store.finish_intent(uuid4(), state=IntentState.REFUSED, report_json="{}", now=START)
    assert store.intent(intent).state == IntentState.CONFIRMED  # type: ignore[union-attr]


def test_attempt_guard_is_atomic_with_duplicate_and_ceiling_checks(store: LocalStore) -> None:
    inquiry = uuid4()
    first, second, third = uuid4(), uuid4(), uuid4()
    for intent in (first, second):
        store.record_intent_received(intent_id=intent, inquiry_id=inquiry, payload_json="{}", now=START)
    assert store.begin_attempt(first, START, inquiry_id=inquiry)
    assert not store.begin_attempt(second, START, inquiry_id=inquiry)  # same inquiry: never two
    assert store.intent(second).state == IntentState.RECEIVED  # type: ignore[union-attr]
    store.record_intent_received(intent_id=third, inquiry_id=uuid4(), payload_json="{}", now=START)
    ceiling = ((START - timedelta(hours=24), 1),)
    assert not store.begin_attempt(third, START, ceilings=ceiling)  # one attempt already counted
    assert store.begin_attempt(third, START, ceilings=((START - timedelta(hours=24), 2),))


def test_refused_after_attempt_does_not_count_against_ceilings(store: LocalStore) -> None:
    intent = uuid4()
    store.record_intent_received(intent_id=intent, inquiry_id=uuid4(), payload_json="{}", now=START)
    assert store.begin_attempt(intent, START)
    assert store.attempted_since(START - timedelta(seconds=1)) == 1  # "attempting" may have sent
    store.finish_intent(intent, state=IntentState.REFUSED, report_json="{}", now=START)
    assert store.attempted_since(START - timedelta(seconds=1)) == 0  # proven: .Send never called


def test_open_gap_reports_whether_it_opened_a_new_gap(store: LocalStore) -> None:
    assert store.open_gap("folder_scope_invalid", START) is True
    assert store.open_gap("folder_scope_invalid", START + timedelta(minutes=1)) is False
    store.close_gap("folder_scope_invalid", START + timedelta(minutes=2))
    assert store.open_gap("folder_scope_invalid", START + timedelta(minutes=3)) is True
