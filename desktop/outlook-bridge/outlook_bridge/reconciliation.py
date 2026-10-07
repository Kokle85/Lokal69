"""Event prompts plus startup/periodic reconciliation (spec 37.6 "Event detection plus
reconciliation"; U8).

``NewMailEx`` only prompts an early look at specific EntryIDs; the source of truth is the
reconciliation scan of the configured folders over an overlapping received-time window:

- The scan starts at ``scan_watermark - overlap`` (first run: ``now - initial_lookback``) and is
  bounded by ``max_catchup``; an older checkpoint produces an explicit
  ``catchup_window_exceeded`` gap instead of a silent skip.
- Every item is processed through ``ItemProcessor``: its outcome (and upload-backlog entry or
  pending locator) is committed atomically before the next item; duplicates (same Internet
  Message-ID, e.g. a moved message seen again in a rule-target folder) only record the changed
  locator.
- The watermark advances only after a *complete* scan, to ``scan start - sync settle``, and never
  past the start of a detection gap (worker offline, sleep, Outlook not running/disconnected)
  that is still open or closed less than the settle time ago - mail that synchronises late after
  such a gap is still inside the next window.
- A periodic *deep* scan re-reads a longer window without moving the watermark (insurance against
  very late synchronisation).
- Reply-before-binding locators are re-read after binding syncs that bring matching Message-IDs
  and are dropped after their bounded window (a reply to this system's Message-ID format then
  surfaces as an unresolved matching gap; other mail is dropped silently).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from outlook_bridge.config import BridgeConfig
from outlook_bridge.errors import FolderScopeError, OutlookUnavailable, StaError, UploadBuildError
from outlook_bridge.health import (
    GAP_CATCHUP_EXCEEDED,
    GAP_ITEM_UNREADABLE,
    GAP_PENDING_CAPACITY,
    GAP_UNRESOLVED_MATCHING,
    GAP_UPLOAD_BUILD_FAILED,
    CoverageMonitor,
)
from outlook_bridge.local_queue import (
    TERMINAL_OUTCOMES,
    BacklogEntry,
    LocalStore,
    Outcome,
    PendingLocator,
    sha256_text,
)
from outlook_bridge.log import event, get_logger
from outlook_bridge.matching import DecisionKind, LocalMatcher
from outlook_bridge.outlook_adapter import FolderRef, ItemRef, MailboxAdapter, MailSnapshot
from suv_deals.domain.replies import is_processable_item

ItemOutcomeKind = Literal[
    "uploaded",
    "quarantined",
    "pending",
    "unrelated",
    "non_mail",
    "duplicate",
    "moved",
    "matching_gap",
    "local_reject",
    "out_of_scope",
    "unreadable",
]
MAX_ITEM_FAILURES: Final = 3
_LOG = get_logger("reconciliation")


@dataclass(frozen=True, slots=True)
class ItemOutcome:
    kind: ItemOutcomeKind
    key_hash: str | None = None
    backlog_id: int | None = None


@dataclass
class ScanResult:
    folder_key: str
    role: str
    complete: bool
    deep: bool
    since: datetime
    counts: Counter[str] = field(default_factory=Counter)
    gap_reasons: list[str] = field(default_factory=list)


class ItemProcessor:
    """Local decision and durable commit for one mailbox item."""

    def __init__(
        self,
        *,
        store: LocalStore,
        matcher: LocalMatcher,
        mailbox: MailboxAdapter,
        max_pending_locators: int,
    ) -> None:
        self._store = store
        self._matcher = matcher
        self._mailbox = mailbox
        self._max_pending = max_pending_locators

    def _record(
        self,
        outcome: Outcome,
        *,
        key_hash: str,
        ref: ItemRef,
        folder: FolderRef,
        now: datetime,
        inquiry_id: UUID | None = None,
        backlog: BacklogEntry | None = None,
        pending: PendingLocator | None = None,
    ) -> int | None:
        return self._store.record_outcome(
            key_hash=key_hash,
            outcome=outcome,
            now=now,
            inquiry_id=inquiry_id,
            received_at=ref.received_at,
            entry_id=ref.entry_id,
            store_id=ref.store_id,
            folder_key=folder.key,
            backlog=backlog,
            pending=pending,
        )

    def process(self, snapshot: MailSnapshot, folder: FolderRef, *, now: datetime) -> ItemOutcome:
        key = self._matcher.local_key(snapshot)
        existing = self._store.processed(key.key_hash)
        ref = snapshot.ref
        if existing is not None and existing.outcome in TERMINAL_OUTCOMES:
            moved = self._store.record_locator(
                key.key_hash, entry_id=ref.entry_id, store_id=ref.store_id, folder_key=folder.key, now=now
            )
            return ItemOutcome("moved" if moved else "duplicate", key.key_hash)
        first_seen = existing.first_seen_at if existing is not None else now
        junk = folder.role == "junk"
        decision = self._matcher.evaluate(
            snapshot,
            self._store.bindings_for_matching(),
            in_junk_folder=junk,
            first_seen_at=first_seen,
            now=now,
            own_sent_hashes=self._store.own_sent_message_id_hashes(),
        )
        if decision.kind in (DecisionKind.UPLOAD, DecisionKind.QUARANTINE_UPLOAD):
            digests = (
                self._mailbox.attachment_digests(ref, decision.digest_indices)
                if decision.digest_indices
                else {}
            )
            try:
                prepared = self._matcher.prepare_upload(
                    snapshot, decision, digests, in_junk_folder=junk, observed_at=now
                )
            except UploadBuildError:
                self._record(Outcome.LOCAL_REJECT, key_hash=key.key_hash, ref=ref, folder=folder, now=now)
                self._store.record_gap(GAP_UPLOAD_BUILD_FAILED, now, now)
                return ItemOutcome("local_reject", key.key_hash)
            quarantined = decision.kind == DecisionKind.QUARANTINE_UPLOAD
            backlog_id = self._record(
                Outcome.QUARANTINE_QUEUED if quarantined else Outcome.UPLOAD_QUEUED,
                key_hash=key.key_hash,
                ref=ref,
                folder=folder,
                now=now,
                inquiry_id=prepared.inquiry_id,
                backlog=BacklogEntry(
                    idempotency_key=prepared.idempotency_key,
                    dedup_key_hash=key.key_hash,
                    inquiry_id=prepared.inquiry_id,
                    binding_version=prepared.binding_version,
                    folder_key=folder.key,
                    received_at=ref.received_at or now,
                    request_json=prepared.body.decode("utf-8"),
                ),
            )
            event(_LOG, "reply_queued", inquiry_id=prepared.inquiry_id, quarantined=quarantined)
            return ItemOutcome("quarantined" if quarantined else "uploaded", key.key_hash, backlog_id)
        if decision.kind == DecisionKind.PENDING_BINDING and decision.retry_until is not None:
            self._record(
                Outcome.PENDING_BINDING,
                key_hash=key.key_hash,
                ref=ref,
                folder=folder,
                now=now,
                pending=PendingLocator(
                    key_hash=key.key_hash,
                    internet_message_id=key.internet_message_id,
                    entry_id=ref.entry_id,
                    store_id=ref.store_id,
                    folder_key=folder.key,
                    received_at=ref.received_at,
                    reference_hashes=decision.reference_hashes,
                    own_reference=decision.own_reference,
                    first_seen_at=first_seen,
                    retry_until=decision.retry_until,
                ),
            )
            for evicted in self._store.evict_pending_over(self._max_pending, now):
                if evicted.own_reference:
                    self._store.record_gap(GAP_PENDING_CAPACITY, evicted.first_seen_at, now)
            return ItemOutcome("pending", key.key_hash)
        if decision.kind == DecisionKind.MATCHING_GAP:
            self._record(Outcome.MATCHING_GAP, key_hash=key.key_hash, ref=ref, folder=folder, now=now)
            self._store.record_gap(decision.gap_kind or GAP_UNRESOLVED_MATCHING, first_seen, now)
            return ItemOutcome("matching_gap", key.key_hash)
        if decision.kind == DecisionKind.NON_MAIL:
            self._record(Outcome.NON_MAIL, key_hash=key.key_hash, ref=ref, folder=folder, now=now)
            return ItemOutcome("non_mail", key.key_hash)
        self._record(Outcome.UNRELATED, key_hash=key.key_hash, ref=ref, folder=folder, now=now)
        return ItemOutcome("unrelated", key.key_hash)


class Reconciler:
    """Folder scans, event prompts and pending-locator retries."""

    def __init__(
        self,
        *,
        config: BridgeConfig,
        store: LocalStore,
        mailbox: MailboxAdapter,
        processor: ItemProcessor,
        monitor: CoverageMonitor,
        deep_lookback: timedelta = timedelta(hours=72),
    ) -> None:
        self._config = config
        self._store = store
        self._mailbox = mailbox
        self._processor = processor
        self._monitor = monitor
        self._deep_lookback = deep_lookback

    # ------------------------------------------------------------------ scans

    def reconcile(
        self, folders: Sequence[FolderRef], *, now: datetime, deep: bool = False
    ) -> list[ScanResult]:
        results = [self._scan_folder(folder, now=now, deep=deep) for folder in folders]
        if results and all(r.complete for r in results) and not deep:
            self._store.set_runtime_time("last_successful_reconcile_at", now)
        self.expire_pending(now)
        return results

    def _scan_folder(self, folder: FolderRef, *, now: datetime, deep: bool) -> ScanResult:
        config = self._config
        self._store.upsert_folder(
            folder_key=folder.key,
            role=folder.role,
            store_id_hash=folder.store_id_hash,
            folder_id_hash=folder.folder_id_hash,
        )
        checkpoint = self._store.folder_checkpoint(folder.key)
        watermark = checkpoint.scan_watermark if checkpoint else None
        if deep:
            since = now - self._deep_lookback
        elif watermark is None:
            since = now - config.initial_lookback
        else:
            since = watermark - config.overlap
        result = ScanResult(folder_key=folder.key, role=folder.role, complete=True, deep=deep, since=since)
        floor = now - config.max_catchup
        if since < floor:
            if checkpoint is None or GAP_CATCHUP_EXCEEDED not in checkpoint.gap_reasons:
                # Recorded once per streak; while the checkpoint stays too old (e.g. held by a
                # long detection gap) the reason stays on the checkpoint instead of a new row per scan.
                self._store.record_gap(
                    GAP_CATCHUP_EXCEEDED, since, floor, "checkpoint older than the catch-up window"
                )
            result.gap_reasons.append(GAP_CATCHUP_EXCEEDED)
            since = floor
            result.since = floor
        self._store.begin_scan(folder.key, now)
        try:
            listing = self._mailbox.list_items(folder, since=since, max_items=config.max_scan_items)
        except (OutlookUnavailable, FolderScopeError, StaError):
            result.complete = False
            result.gap_reasons.append("folder_unavailable")
            self._store.finish_scan(
                folder.key, complete=False, new_watermark=None, gap_reasons=result.gap_reasons, now=now
            )
            return result
        if listing.truncated:
            result.complete = False
            result.gap_reasons.append("scan_item_cap_reached")
        if listing.unreadable:
            result.complete = False
            result.gap_reasons.append("item_without_entry_id")
        for ref in listing.refs:
            kind = self._process_ref(ref, folder, now=now)
            result.counts[kind] += 1
            if kind == "outlook_unavailable":
                result.complete = False
                result.gap_reasons.append("outlook_unavailable_during_scan")
                break
            if kind == "read_failed":
                result.complete = False
                result.gap_reasons.append("item_read_failed")
        new_watermark: datetime | None = None
        if result.complete and not deep:
            new_watermark = now - config.sync_settle
            cap = self._monitor.watermark_cap(now, config.sync_settle)
            if cap is not None and cap < new_watermark:
                new_watermark = cap
                result.gap_reasons.append("watermark_held_by_coverage_gap")
        self._store.finish_scan(
            folder.key,
            complete=result.complete,
            new_watermark=new_watermark,
            gap_reasons=result.gap_reasons,
            now=now,
        )
        event(
            _LOG,
            "folder_scanned",
            role=folder.role,
            complete=result.complete,
            deep=deep,
            items=len(listing.refs),
            uploaded=result.counts["uploaded"],
            pending=result.counts["pending"],
        )
        return result

    def _process_ref(self, ref: ItemRef, folder: FolderRef, *, now: datetime) -> str:
        entry_hash = sha256_text("entry\x00" + ref.store_id + "\x00" + ref.entry_id)
        if ref.message_class is not None and not is_processable_item(ref.message_class):
            # Meetings, sharing invitations, tasks and other non-mail items: ignored before any
            # content is read (only a hashed locator key and the outcome are kept).
            if self._store.processed(entry_hash) is None:
                self._store.record_outcome(
                    key_hash=entry_hash,
                    outcome=Outcome.NON_MAIL,
                    now=now,
                    received_at=ref.received_at,
                    folder_key=folder.key,
                )
            return "non_mail"
        given_up = self._store.processed(entry_hash)
        if given_up is not None and given_up.outcome == Outcome.UNREADABLE:
            # Already given up (and surfaced as a gap): not re-read on every overlapping scan.
            # A move gives the item a new EntryID, so it is retried once it shows up elsewhere.
            return "unreadable_skipped"
        try:
            snapshot = self._mailbox.read_item(ref)
            if snapshot is None:
                return "gone"  # moved/deleted since listing; seen again where it went (if configured)
            return self._processor.process(snapshot, folder, now=now).kind
        except (OutlookUnavailable, StaError):
            return "outlook_unavailable"
        except Exception:
            failures = self._store.note_item_failure(entry_hash, now)
            if failures >= MAX_ITEM_FAILURES:
                self._store.record_outcome(
                    key_hash=entry_hash,
                    outcome=Outcome.UNREADABLE,
                    now=now,
                    received_at=ref.received_at,
                    entry_id=ref.entry_id,
                    store_id=ref.store_id,
                    folder_key=folder.key,
                )
                # Given up after repeated failures: possibly a reply that is now never uploaded,
                # so it is surfaced as a coverage gap (count only), never silently dropped.
                self._store.record_gap(GAP_ITEM_UNREADABLE, ref.received_at or now, now, "item unreadable")
                return "unreadable"
            return "read_failed"

    # ------------------------------------------------------------------ NewMailEx prompts

    def process_event_ids(
        self, entry_ids: Iterable[str], folders: Sequence[FolderRef], *, now: datetime
    ) -> Counter[str]:
        """Inspect items named by ``NewMailEx``; out-of-scope items are ignored unread."""
        by_entry = {folder.entry_id: folder for folder in folders}
        counts: Counter[str] = Counter()
        for entry_id in dict.fromkeys(entry_ids):
            try:
                ref = self._mailbox.locate_item(entry_id, None)
            except (OutlookUnavailable, StaError):
                counts["outlook_unavailable"] += 1
                break
            folder = by_entry.get(ref.folder_entry_id) if ref is not None else None
            if ref is None or folder is None:
                counts["out_of_scope"] += 1
                continue
            counts[self._process_ref(ref, folder, now=now)] += 1
        return counts

    # ------------------------------------------------------------------ pending locators

    def retry_pending(
        self,
        folders: Sequence[FolderRef],
        *,
        now: datetime,
        message_id_hashes: frozenset[str] | None = None,
    ) -> Counter[str]:
        """Re-read pending reply locators (all, or those referencing newly synced Message-IDs)."""
        by_key = {folder.key: folder for folder in folders}
        by_entry = {folder.entry_id: folder for folder in folders}
        if message_id_hashes is None:
            candidates = self._store.pending_locators(limit=500)
        else:
            candidates = self._store.pending_matching(message_id_hashes)
        counts: Counter[str] = Counter()
        for pending in candidates:
            folder = by_key.get(pending.folder_key or "")
            ref = ItemRef(
                entry_id=pending.entry_id,
                store_id=pending.store_id,
                folder_entry_id=folder.entry_id if folder else "",
                received_at=pending.received_at,
                message_class=None,
            )
            try:
                snapshot = self._mailbox.read_item(ref)
                if snapshot is None and pending.internet_message_id:
                    # Moved since it was seen (new EntryID): re-find it inside the configured
                    # folders only, by its immutable Internet Message-ID.
                    found = self._mailbox.find_by_internet_message_id(pending.internet_message_id)
                    snapshot = self._mailbox.read_item(found) if found is not None else None
                if snapshot is None:
                    counts["not_found"] += 1  # kept until its bounded window ends
                    continue
                target = by_entry.get(snapshot.ref.folder_entry_id)
                if target is None:
                    counts["out_of_scope"] += 1
                    continue
                counts[self._processor.process(snapshot, target, now=now).kind] += 1
            except (OutlookUnavailable, StaError):
                counts["outlook_unavailable"] += 1
                break
            except Exception:
                # One unreadable item must not stop the others; the locator stays bounded by its
                # retry window and expiry then surfaces an unresolved gap (count only).
                counts["read_failed"] += 1
        return counts

    def retry_resolvable(self, folders: Sequence[FolderRef], *, now: datetime) -> Counter[str]:
        """Re-read only pending locators whose references now match a known binding.

        A pending reply-before-binding locator can only resolve when a binding names one of its
        referenced Message-IDs, so unrelated replies (personal threads) are never re-read. This
        also catches up a targeted retry that could not run at binding-sync time (for example
        while Outlook was briefly unavailable).
        """
        known = self._store.known_message_id_hashes()
        if not known:
            return Counter()
        return self.retry_pending(folders, now=now, message_id_hashes=known)

    def expire_pending(self, now: datetime) -> int:
        expired = self._store.take_expired_pending(now)
        for item in expired:
            if item.own_reference:
                self._store.record_gap(
                    GAP_UNRESOLVED_MATCHING, item.first_seen_at, now, "reply without binding"
                )
        return len(expired)


__all__ = ["MAX_ITEM_FAILURES", "ItemOutcome", "ItemProcessor", "Reconciler", "ScanResult"]
