"""The reply worker loop: startup reconciliation, NewMailEx prompts, binding sync, uploads,
send intents and heartbeats (spec 37.6-37.8).

Order of operations at startup: record any offline gap, attach to the configured Outlook account
and folders, subscribe ``NewMailEx`` (prompt only), sync bindings (tombstones included), reconcile
the configured folders (this recovers mail that arrived while the worker was not running or that
Outlook synchronised without an event), flush the upload backlog, process send intents and send a
heartbeat. Afterwards each tick drains NewMailEx prompts and, every reconciliation interval,
syncs bindings and rescans.

When Outlook (or the configured account) is unavailable the worker keeps doing everything that does
not need the mailbox: binding sync, uploading the already committed backlog and heartbeats that
report ``outlook_running=false`` - the coverage gap is recorded, never hidden - and it reattaches on
every tick.

Transmission rules: nothing is uploaded before bindings were synced to the end of the change log
in this session, and uploads stop again whenever a later binding page is rejected (another
mailbox, invalid content) or the sync cannot progress - a revocation in such a page must not be
skipped while stale bindings keep granting access; an upload whose inquiry binding is tombstoned
is revoked locally; a
stored upload that names another mailbox binding or inquiry is refused locally (cross-mailbox
injection); ``401`` (or ``403`` on the binding endpoint) stops all transmission and keeps the
backlog until a *different* credential is stored; transient failures back off and keep the backlog.
``--dry-run`` reads, syncs bindings (GET) and correlates but never uploads, reports or sends.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from pydantic import ValidationError

from outlook_bridge.api_client import ApiErrorKind, BridgeApiClient, BridgeApiError
from outlook_bridge.compatibility import CompatibilityReport, OutlookFlavour
from outlook_bridge.config import BridgeConfig
from outlook_bridge.credentials import CredentialManager, CredentialState
from outlook_bridge.errors import (
    CredentialError,
    FolderScopeError,
    MailboxMismatch,
    OutlookUnavailable,
    StaError,
)
from outlook_bridge.health import (
    GAP_ACCOUNT_CHANGED,
    GAP_BINDING_SYNC_INCOMPLETE,
    GAP_CREDENTIAL_UNUSABLE,
    GAP_CROSS_MAILBOX,
    GAP_EVENT_OVERFLOW,
    GAP_FOLDER_SCOPE,
    CoverageMonitor,
    HealthSnapshot,
    build_health,
    heartbeat_envelope,
)
from outlook_bridge.local_queue import BacklogRow, BacklogState, LocalStore, backoff_delay
from outlook_bridge.log import event, get_logger
from outlook_bridge.matching import LocalMatcher, encode_payload, wire_payload
from outlook_bridge.outlook_adapter import AccountInfo, ConnectionState, FolderRef, MailboxAdapter
from outlook_bridge.reconciliation import ItemProcessor, Reconciler
from outlook_bridge.sending import SendIntentProcessor
from outlook_bridge.wire import ReplyUpload, WorkerAccountReport
from suv_deals.clock import Clock
from suv_deals.domain.replies import InquiryBindingState

EVENT_QUEUE_MAX: Final = 1000
MAX_BINDING_PAGES_PER_SYNC: Final = 50
HEARTBEAT_EVERY: Final = timedelta(seconds=60)
ACCOUNT_REPORT_EVERY: Final = timedelta(minutes=10)
SEND_POLL_EVERY: Final = timedelta(seconds=30)
DEEP_RECONCILE_EVERY: Final = timedelta(hours=24)
PRUNE_EVERY: Final = timedelta(hours=6)
MAX_FORBIDDEN_ATTEMPTS: Final = 3
MAX_CONFLICT_ATTEMPTS: Final = 8
_LOG_FAILURE_EVERY: Final = 100
_BOUND_ACCOUNT_KEY: Final = "bound_account_smtp"
_LOG = get_logger("worker")


@dataclass
class CycleReport:
    """Content-free counters of one startup/tick cycle."""

    events: Counter[str] = field(default_factory=Counter)
    scan: Counter[str] = field(default_factory=Counter)
    uploads: Counter[str] = field(default_factory=Counter)
    sends: Counter[str] = field(default_factory=Counter)
    binding_pages: int = 0
    reconciled: bool = False


class BridgeWorker:
    """One mailbox binding, one Outlook account, one protected local store."""

    def __init__(
        self,
        *,
        config: BridgeConfig,
        store: LocalStore,
        mailbox: MailboxAdapter,
        api: BridgeApiClient | None,
        credentials: CredentialManager,
        clock: Clock,
        compat: CompatibilityReport,
        dry_run: bool = False,
    ) -> None:
        if store.mailbox_binding_id != config.mailbox_binding_id:
            raise MailboxMismatch("the local store belongs to another mailbox binding")
        self._config = config
        self._store = store
        self._mailbox = mailbox
        self._api = api
        self._credentials = credentials
        self._clock = clock
        self._compat = compat
        self._dry_run = dry_run
        self._matcher = LocalMatcher(
            config.mailbox_binding_id,
            retry_window=config.unmatched_retry_window,
            own_addresses=(config.account_smtp_address,),
        )
        self._monitor = CoverageMonitor(store)
        self._processor = ItemProcessor(
            store=store,
            matcher=self._matcher,
            mailbox=mailbox,
            max_pending_locators=config.max_pending_locators,
        )
        self._reconciler = Reconciler(
            config=config, store=store, mailbox=mailbox, processor=self._processor, monitor=self._monitor
        )
        self._account: AccountInfo | None = None
        self._folders: tuple[FolderRef, ...] = ()
        self._connection: ConnectionState | None = None
        self._events: queue.Queue[tuple[str, ...]] = queue.Queue(maxsize=EVENT_QUEUE_MAX)
        self._event_overflow = threading.Event()
        self._bindings_synced = False
        self._force_binding_sync = False
        self._cycle_now: datetime | None = None
        self._consecutive_failures = 0
        self._next_reconcile: datetime | None = None
        self._next_deep: datetime | None = None
        self._next_heartbeat: datetime | None = None
        self._next_account_report: datetime | None = None
        self._next_send_poll: datetime | None = None
        self._next_prune: datetime | None = None
        self._sender: SendIntentProcessor | None = None
        if api is not None and config.send_intents_enabled and not dry_run:
            self._sender = SendIntentProcessor(
                config=config,
                store=store,
                mailbox=mailbox,
                api=api,
                outlook_is_classic=lambda: (
                    self._compat.flavour == OutlookFlavour.CLASSIC and self._compat.supported
                ),
                account_smtp=lambda: self._account.smtp_address if self._account is not None else None,
                on_api_error=self._sender_api_failed,
            )

    # ------------------------------------------------------------------ NewMailEx sink (STA thread)

    def on_new_mail(self, entry_ids: tuple[str, ...]) -> None:
        """Called on the STA thread: copy plain strings into a bounded queue and return at once."""
        try:
            self._events.put_nowait(tuple(str(e) for e in entry_ids))
        except queue.Full:
            self._event_overflow.set()  # reconciliation covers whatever the prompt would have shown

    # ------------------------------------------------------------------ lifecycle

    @property
    def folders(self) -> tuple[FolderRef, ...]:
        return self._folders

    @property
    def connected(self) -> bool:
        return self._account is not None

    def start(self, now: datetime | None = None) -> CycleReport:
        now = now or self._clock.now()
        self._cycle_now = now
        report = CycleReport()
        try:
            self._monitor.on_startup(now)
            self._connect(now)
            self._sync_bindings(now, report)
            if self._account is not None:
                self._account_report(now)
                self._reconcile(now, report)
            self._flush_uploads(now, report)
            self._send(now, report, force=True)
            self._heartbeat(now)
        finally:
            # Even a failing first cycle must leave the periodic schedule (deep reconciliation,
            # account report, pruning) in place; ``run`` keeps ticking after a failure.
            self._schedule_after_start(now)
        return report

    def _schedule_after_start(self, now: datetime) -> None:
        self._next_reconcile = now + self._config.reconcile_interval
        self._next_deep = now + DEEP_RECONCILE_EVERY
        self._next_heartbeat = now + HEARTBEAT_EVERY
        self._next_account_report = now + ACCOUNT_REPORT_EVERY
        self._next_send_poll = now + SEND_POLL_EVERY
        self._next_prune = now + PRUNE_EVERY

    def _connect(self, now: datetime) -> None:
        try:
            account = self._mailbox.connect(self._config.account_smtp_address)
            if not self._account_matches_activation(account, now):
                raise OutlookUnavailable("the configured account differs from the activated one")
            try:
                folders = self._mailbox.resolve_folders(self._config.folders)
            except FolderScopeError:
                # A configured folder (rule target path, the store's Junk folder) cannot be
                # resolved: never scan a partial folder set silently - an open, reported gap.
                if self._store.open_gap(GAP_FOLDER_SCOPE, now, "a configured folder cannot be resolved"):
                    event(_LOG, "folder_scope_invalid", level=logging.ERROR)
                raise OutlookUnavailable("a configured folder cannot be resolved") from None
            self._store.close_gap(GAP_FOLDER_SCOPE, now)
            self._account = account
            self._folders = folders
            for folder in self._folders:
                self._store.upsert_folder(
                    folder_key=folder.key,
                    role=folder.role,
                    store_id_hash=folder.store_id_hash,
                    folder_id_hash=folder.folder_id_hash,
                )
            self._mailbox.subscribe_new_mail(self.on_new_mail)
        except (OutlookUnavailable, StaError):
            self._account = None
            self._folders = ()
        self._refresh_connection(now)

    def _account_matches_activation(self, account: AccountInfo, now: datetime) -> bool:
        """Mailbox/address changes never silently reassign a binding (spec 37.8).

        The first successfully attached account is recorded in the mailbox-bound local store; a
        later configuration naming another account for the same mailbox binding is refused (an
        open, reported gap) until the worker is re-activated with a new mailbox binding.
        """
        current = account.smtp_address.casefold()
        bound = self._store.get_runtime(_BOUND_ACCOUNT_KEY)
        if bound is None:
            self._store.set_runtime(_BOUND_ACCOUNT_KEY, current)
            return True
        if bound != current:
            if self._store.open_gap(
                GAP_ACCOUNT_CHANGED, now, "configured account differs from the activated one"
            ):
                event(_LOG, "account_binding_mismatch", level=logging.ERROR)
            return False
        self._store.close_gap(GAP_ACCOUNT_CHANGED, now)
        return True

    def _refresh_connection(self, now: datetime) -> None:
        try:
            self._connection = self._mailbox.connection_state()
        except (OutlookUnavailable, StaError):
            self._connection = ConnectionState(outlook_running=False, connected=False)
        if self._account is None and self._connection.outlook_running:
            # Outlook is running but the configured account/folders are not available.
            self._connection = ConnectionState(
                outlook_running=True,
                connected=False,
                last_send_receive_end_at=self._connection.last_send_receive_end_at,
            )
        self._monitor.on_connection(self._connection, now)

    def tick(self, now: datetime | None = None) -> CycleReport:
        now = now or self._clock.now()
        self._cycle_now = now
        report = CycleReport()
        self._monitor.on_tick(now)
        if self._account is None:
            self._connect(now)
            if self._account is not None:
                self._next_reconcile = now  # (re)attached: sync bindings and reconcile at once
        if self._account is not None:
            self._drain_events(now, report)
        else:
            self._discard_events()  # nothing to inspect; the next reconciliation covers them
        due = self._next_reconcile is None or now >= self._next_reconcile
        overflow = self._event_overflow.is_set()
        if due or overflow or self._force_binding_sync:
            if overflow:
                self._store.record_gap(GAP_EVENT_OVERFLOW, now, now)
                self._event_overflow.clear()
            self._sync_bindings(now, report)
            if self._account is not None and (due or overflow or report.binding_pages):
                self._reconcile(now, report)
            if due:
                self._next_reconcile = now + self._config.reconcile_interval
        if self._account is not None and self._next_deep is not None and now >= self._next_deep:
            self._reconcile(now, report, deep=True)
            self._next_deep = now + DEEP_RECONCILE_EVERY
        self._flush_uploads(now, report)
        self._send(now, report)
        if self._next_account_report is not None and now >= self._next_account_report:
            self._account_report(now)
            self._next_account_report = now + ACCOUNT_REPORT_EVERY
        if self._next_heartbeat is None or now >= self._next_heartbeat:
            self._heartbeat(now)
            self._next_heartbeat = now + HEARTBEAT_EVERY
        if self._next_prune is not None and now >= self._next_prune:
            self._prune(now)
            self._next_prune = now + PRUNE_EVERY
        return report

    def run(self, stop: threading.Event, *, max_ticks: int | None = None) -> None:
        """The long-running loop; one failing cycle never ends the worker (it is logged)."""
        self._guarded(self.start)
        ticks = 0
        while not stop.wait(self._config.tick_seconds):
            self._guarded(self.tick)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
        self.shutdown()

    def _guarded(self, step: Callable[[], CycleReport]) -> None:
        try:
            step()
        except Exception as exc:
            self._consecutive_failures += 1
            if self._consecutive_failures == 1 or self._consecutive_failures % _LOG_FAILURE_EVERY == 0:
                event(
                    _LOG,
                    "worker_cycle_failed",
                    level=logging.ERROR,
                    error_type=type(exc).__name__,
                    consecutive=self._consecutive_failures,
                )
        else:
            self._consecutive_failures = 0

    def shutdown(self) -> None:
        self._monitor.on_shutdown(self._clock.now())

    # ------------------------------------------------------------------ steps

    def _drain_events(self, now: datetime, report: CycleReport) -> None:
        ids: list[str] = []
        while True:
            try:
                ids.extend(self._events.get_nowait())
            except queue.Empty:
                break
        if ids:
            report.events.update(self._reconciler.process_event_ids(ids, self._folders, now=now))

    def _discard_events(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                return

    def _reconcile(self, now: datetime, report: CycleReport, *, deep: bool = False) -> None:
        self._refresh_connection(now)
        if self._connection is not None and not self._connection.outlook_running:
            self._account = None
            self._folders = ()
            return
        results = self._reconciler.reconcile(self._folders, now=now, deep=deep)
        for result in results:
            report.scan.update(result.counts)
        report.reconciled = bool(results) and all(r.complete for r in results)
        if not deep:
            report.scan.update(self._reconciler.retry_resolvable(self._folders, now=now))

    # ------------------------------------------------------------------ transmission state

    def _sender_api_failed(self, exc: BridgeApiError | CredentialError) -> None:
        self._transmission_failed(exc, self._cycle_now or self._clock.now())

    def _transmission_failed(self, exc: BridgeApiError | CredentialError, now: datetime) -> None:
        if isinstance(exc, CredentialError):
            self._monitor.on_transmission(False, now, kind=GAP_CREDENTIAL_UNUSABLE)
            return
        self._store.set_runtime("last_server_error_kind", exc.kind.value)
        if exc.kind == ApiErrorKind.UNAUTHENTICATED:
            self._credential_rejected(exc, now)
        elif exc.transient:
            self._monitor.on_transmission(False, now)

    def _credential_rejected(self, exc: BridgeApiError, now: datetime) -> None:
        """The server refused the credential: stop transmitting until another one is stored."""
        self._credentials.mark_rejected(exc.credential_fingerprint)
        self._monitor.on_transmission(False, now, kind=GAP_CREDENTIAL_UNUSABLE)
        event(_LOG, "credential_rejected", status=exc.status)

    def _transmission_ok(self, now: datetime) -> None:
        self._store.set_runtime_time("last_server_ok_at", now)
        self._store.delete_runtime("last_server_error_kind")
        self._monitor.on_transmission(True, now)
        self._monitor.on_transmission(True, now, kind=GAP_CREDENTIAL_UNUSABLE)

    def _can_transmit(self, now: datetime) -> bool:
        return self._api is not None and self._credentials.state(now) == CredentialState.ACTIVE

    # ------------------------------------------------------------------ binding sync

    def _sync_bindings(self, now: datetime, report: CycleReport) -> None:
        api = self._api
        if api is None:
            return
        if not self._can_transmit(now):
            self._monitor.on_transmission(False, now, kind=GAP_CREDENTIAL_UNUSABLE)
            return
        new_hashes: set[str] = set()
        cursor = self._store.binding_cursor()
        reached_end = False
        stuck = False
        for _ in range(MAX_BINDING_PAGES_PER_SYNC):
            try:
                page = api.fetch_binding_page(cursor, limit=self._config.binding_page_limit)
            except (BridgeApiError, CredentialError) as exc:
                if isinstance(exc, BridgeApiError) and exc.kind == ApiErrorKind.FORBIDDEN:
                    # The worker identity itself is no longer allowed (revoked mailbox binding).
                    self._store.set_runtime("last_server_error_kind", exc.kind.value)
                    self._credential_rejected(exc, now)
                else:
                    self._transmission_failed(exc, now)
                return
            foreign = [i for i in page.items if i.mailbox_binding_id != self._config.mailbox_binding_id]
            if foreign:
                # Cross-mailbox injection: reject the whole page and keep the old cursor.
                self._binding_sync_rejected(now, "binding page for another mailbox rejected")
                event(_LOG, "binding_page_rejected", foreign=len(foreign))
                return
            try:
                result = self._store.apply_binding_page(
                    [item.to_record() for item in page.items],
                    page.next_cursor,
                    complete=not page.has_more,
                    now=now,
                )
            except (MailboxMismatch, ValidationError, ValueError):
                self._binding_sync_rejected(now, "invalid binding page rejected")
                return
            report.binding_pages += 1
            new_hashes |= result.new_message_id_hashes
            for inquiry_id in result.tombstoned:
                self._revoke_backlog(inquiry_id, now)
            if not page.has_more:
                reached_end = True
                break
            if page.next_cursor is None or page.next_cursor == cursor:
                # "More pages" without a new cursor cannot progress: fail closed (no uploads until
                # a later sync reaches the end) and surface it; retried at the next interval.
                stuck = True
                break
            cursor = page.next_cursor
        self._transmission_ok(now)
        if reached_end:
            # Only a sync that read the change log to its end (tombstones included) allows uploads.
            self._bindings_synced = True
            self._force_binding_sync = False
            self._store.close_gap(GAP_BINDING_SYNC_INCOMPLETE, now)
        elif stuck:
            self._bindings_synced = False
            self._force_binding_sync = False
            self._store.open_gap(GAP_BINDING_SYNC_INCOMPLETE, now, "binding sync cannot progress")
        else:
            self._force_binding_sync = True  # more pages than one pass reads: continue next tick
        if new_hashes and self._folders:
            # Reply-before-binding race: re-read only the locators that reference new Message-IDs.
            report.scan.update(
                self._reconciler.retry_pending(
                    self._folders, now=now, message_id_hashes=frozenset(new_hashes)
                )
            )

    def _binding_sync_rejected(self, now: datetime, detail: str) -> None:
        """A rejected page may hold revocations: stop uploads until a clean sync reaches the end.

        The open ``binding_sync_incomplete`` gap and a one-time cross-mailbox gap are reported;
        the same bad page is re-fetched only at the next reconciliation interval.
        """
        self._bindings_synced = False
        self._force_binding_sync = False
        if self._store.open_gap(GAP_BINDING_SYNC_INCOMPLETE, now, detail):
            self._store.record_gap(GAP_CROSS_MAILBOX, now, now, detail)

    def _revoke_backlog(self, inquiry_id: UUID, now: datetime) -> None:
        for row in self._store.backlog_rows(BacklogState.PENDING):
            if row.inquiry_id == inquiry_id:
                self._store.mark_upload_final(
                    row.id, state=BacklogState.REVOKED, error_code="BINDING_REVOKED", now=now
                )

    # ------------------------------------------------------------------ uploads

    def _flush_uploads(self, now: datetime, report: CycleReport) -> None:
        api = self._api
        if self._dry_run or api is None or not self._bindings_synced or not self._can_transmit(now):
            return
        for row in self._store.due_uploads(now, limit=self._config.upload_batch_limit):
            binding = self._store.binding_state(row.inquiry_id)
            if binding is None or binding[0] == InquiryBindingState.TOMBSTONED:
                self._store.mark_upload_final(
                    row.id, state=BacklogState.REVOKED, error_code="BINDING_REVOKED", now=now
                )
                report.uploads["revoked"] += 1
                continue
            try:
                body = self._upload_body(row, binding_version=binding[1])
                ack = api.post_reply(
                    body, idempotency_key=row.idempotency_key, expected_inquiry_id=row.inquiry_id
                )
            except (MailboxMismatch, ValidationError, ValueError):
                self._store.mark_upload_final(
                    row.id, state=BacklogState.REJECTED, error_code="LOCAL_UPLOAD_REFUSED", now=now
                )
                self._store.record_gap(
                    GAP_CROSS_MAILBOX, now, now, "upload for another mailbox refused locally"
                )
                report.uploads["refused_locally"] += 1
                continue
            except CredentialError as exc:
                self._transmission_failed(exc, now)
                return
            except BridgeApiError as exc:
                outcome = self._upload_failed(row.id, row.attempts, exc, now)
                report.uploads[outcome] += 1
                if outcome in ("stopped", "deferred_transient", "deferred_resync"):
                    return
                continue
            self._store.mark_upload_acked(
                row.id, reply_id=ack.reply_id, duplicate=ack.duplicate, request_id=ack.request_id, now=now
            )
            self._transmission_ok(now)
            report.uploads["duplicate" if ack.duplicate else "acked"] += 1

    def _upload_body(self, row: BacklogRow, *, binding_version: int) -> bytes:
        """Re-validate the stored request and bind it to the current binding version.

        The binding version is transport metadata (not part of the immutable source
        fingerprint), so a newer version may be applied without changing the reply's identity.
        """
        data = json.loads(row.request_json)
        request = ReplyUpload.model_validate(data)
        if request.mailbox_binding_id != self._config.mailbox_binding_id:
            raise MailboxMismatch("a stored upload names another mailbox binding")
        if request.inquiry_id != row.inquiry_id:
            raise MailboxMismatch("a stored upload names another inquiry than its backlog entry")
        if request.binding_version == binding_version:
            return row.request_json.encode("utf-8")
        rebound = ReplyUpload.model_validate({**data, "binding_version": binding_version})
        body = encode_payload(wire_payload(rebound))
        self._store.update_upload_request(
            row.id, binding_version=binding_version, request_json=body.decode("utf-8")
        )
        return body

    def _upload_failed(self, backlog_id: int, attempts: int, exc: BridgeApiError, now: datetime) -> str:
        self._transmission_failed(exc, now)
        kind = exc.kind
        if kind == ApiErrorKind.UNAUTHENTICATED:
            return "stopped"  # credential rejected: transmission stops, backlog kept
        if kind in (ApiErrorKind.VALIDATION, ApiErrorKind.REQUEST_TOO_LARGE):
            self._store.mark_upload_final(
                backlog_id, state=BacklogState.REJECTED, error_code=exc.code or kind.value, now=now
            )
            return "rejected"
        if kind == ApiErrorKind.IDEMPOTENCY_CONFLICT:
            # Same source identity, different immutable content: the server quarantines it for
            # investigation; never retried as an overwrite.
            self._store.mark_upload_final(
                backlog_id, state=BacklogState.CONFLICT, error_code="IDEMPOTENCY_CONFLICT", now=now
            )
            return "conflict"
        if kind in (ApiErrorKind.FORBIDDEN, ApiErrorKind.CONFLICT, ApiErrorKind.NOT_FOUND):
            # Possibly a stale/revoked binding: resync before anything else is uploaded and give
            # up on this entry only after repeated refusals.
            self._force_binding_sync = True
            limit = MAX_FORBIDDEN_ATTEMPTS if kind == ApiErrorKind.FORBIDDEN else MAX_CONFLICT_ATTEMPTS
            if attempts + 1 >= limit:
                self._store.mark_upload_final(
                    backlog_id, state=BacklogState.REJECTED, error_code=exc.code or kind.value, now=now
                )
                return "rejected"
            self._store.mark_upload_deferred(
                backlog_id, next_attempt_at=now + backoff_delay(attempts), error_code=exc.code or kind.value
            )
            return "deferred_resync"
        delay = backoff_delay(attempts)
        if exc.retry_after_seconds is not None:
            delay = max(delay, timedelta(seconds=exc.retry_after_seconds))
        self._store.mark_upload_deferred(backlog_id, next_attempt_at=now + delay, error_code=kind.value)
        return "deferred_transient"

    # ------------------------------------------------------------------ sending, reports, health

    def _send(self, now: datetime, report: CycleReport, *, force: bool = False) -> None:
        if self._sender is None or self._account is None or not self._can_transmit(now):
            return
        if not force and self._next_send_poll is not None and now < self._next_send_poll:
            return
        report.sends.update(self._sender.run_once(now))
        self._next_send_poll = now + SEND_POLL_EVERY

    def _account_report(self, now: datetime) -> None:
        api = self._api
        account = self._account
        if self._dry_run or api is None or account is None or not self._can_transmit(now):
            return
        report = WorkerAccountReport(
            mailbox_binding_id=self._config.mailbox_binding_id,
            worker_id=self._config.worker_id,
            reported_at=now,
            outlook_flavour=self._compat.report_flavour,
            outlook_version=(account.outlook_version or self._compat.office_build or None),
            stable_account_key=account.stable_account_key,
            account_smtp_address=account.smtp_address,
            account_display_name=account.display_name,
            account_type=account.account_type,
        )
        try:
            api.post_account_report(report)
        except (BridgeApiError, CredentialError) as exc:
            self._transmission_failed(exc, now)

    def _heartbeat(self, now: datetime) -> None:
        api = self._api
        if self._dry_run or api is None or not self._can_transmit(now):
            return
        envelope, reported_ids = heartbeat_envelope(
            self._store, config=self._config, now=now, connection=self._connection
        )
        try:
            ack = api.post_heartbeat(envelope)
        except (BridgeApiError, CredentialError) as exc:
            self._transmission_failed(exc, now)
            return
        self._transmission_ok(now)
        self._store.mark_gaps_reported(reported_ids)
        if ack.downstream:
            self._store.set_runtime("downstream_health", json.dumps(ack.downstream, sort_keys=True))

    def _prune(self, now: datetime) -> None:
        horizon = max(self._config.initial_lookback, timedelta(hours=72)) + timedelta(days=1)
        self._store.prune(unrelated_before=now - horizon, history_before=now - timedelta(days=180))

    def health(self, now: datetime | None = None) -> HealthSnapshot:
        now = now or self._clock.now()
        return build_health(
            self._store,
            config=self._config,
            now=now,
            connection=self._connection,
            compat=self._compat,
            credential_state=self._credentials.state(now),
        )


__all__ = ["BridgeWorker", "CycleReport"]
