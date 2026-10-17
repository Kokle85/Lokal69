"""Worker health and honest coverage gaps (spec 37.6, 37.9).

Reported separately: worker heartbeat, Outlook connection, mailbox sync lag, last successful
reconciliation, binding sync, upload backlog (count and age), server reachability/credential and
the downstream Slack/MCP health that only the backend can observe. A desktop worker covers the
mailbox only while the computer is awake, the worker runs and classic Outlook is open and
synchronising; every interval without that is recorded as a gap. Nothing here claims 24-hour
monitoring.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from outlook_bridge.compatibility import CompatibilityReport
from outlook_bridge.config import BridgeConfig
from outlook_bridge.credentials import CredentialState
from outlook_bridge.local_queue import IntentState, LocalStore, Outcome
from outlook_bridge.outlook_adapter import ConnectionState
from outlook_bridge.wire import (
    CheckpointReport,
    FolderRoleWire,
    GapReport,
    HeartbeatEnvelope,
    WorkerHeartbeat,
)

GAP_WORKER_OFFLINE: Final = "worker_offline"
GAP_WORKER_SUSPENDED: Final = "worker_suspended"
GAP_OUTLOOK_NOT_RUNNING: Final = "outlook_not_running"
GAP_OUTLOOK_DISCONNECTED: Final = "outlook_disconnected"
GAP_CATCHUP_EXCEEDED: Final = "catchup_window_exceeded"
GAP_BACKEND_UNREACHABLE: Final = "backend_unreachable"
GAP_CREDENTIAL_UNUSABLE: Final = "credential_unusable"
GAP_UNRESOLVED_MATCHING: Final = "unresolved_reply_matching"
GAP_AMBIGUOUS_MATCH: Final = "ambiguous_multi_inquiry"
GAP_PENDING_CAPACITY: Final = "pending_capacity_exceeded"
GAP_CROSS_MAILBOX: Final = "cross_mailbox_binding_rejected"
GAP_EVENT_OVERFLOW: Final = "new_mail_event_overflow"
GAP_UPLOAD_BUILD_FAILED: Final = "upload_build_failed"
GAP_ACCOUNT_CHANGED: Final = "account_binding_mismatch"
GAP_BINDING_SYNC_INCOMPLETE: Final = "binding_sync_incomplete"
GAP_FOLDER_SCOPE: Final = "folder_scope_invalid"
GAP_ITEM_UNREADABLE: Final = "item_unreadable"

#: Gaps during which new mail may not have been seen; they hold back scan watermarks.
DETECTION_GAP_KINDS: Final = frozenset(
    {
        GAP_WORKER_OFFLINE,
        GAP_WORKER_SUSPENDED,
        GAP_OUTLOOK_NOT_RUNNING,
        GAP_OUTLOOK_DISCONNECTED,
        GAP_FOLDER_SCOPE,
        GAP_ACCOUNT_CHANGED,
    }
)
COVERAGE_STATEMENT: Final = (
    "Local reply detection runs only while this computer is awake, the worker is running and classic "
    "Outlook is open and synchronising the configured mailbox. Listed gaps had no local coverage; "
    "this is not 24-hour monitoring."
)
_LAST_ALIVE: Final = "last_alive_at"
_STARTED: Final = "started_at"
_PERSIST_EVERY: Final = timedelta(seconds=30)

Status = Literal["ok", "degraded", "down", "unknown", "not_observed"]


class CoverageMonitor:
    """Records worker-offline, suspend/sleep and Outlook connectivity gaps."""

    def __init__(
        self,
        store: LocalStore,
        *,
        offline_threshold: timedelta = timedelta(minutes=2),
        suspend_threshold: timedelta = timedelta(seconds=90),
    ) -> None:
        self._store = store
        self._offline_threshold = offline_threshold
        self._suspend_threshold = suspend_threshold
        self._last_tick: datetime | None = None
        self._last_persist: datetime | None = None

    def on_startup(self, now: datetime) -> None:
        last_alive = self._store.get_runtime_time(_LAST_ALIVE)
        if last_alive is not None and now - last_alive > self._offline_threshold:
            self._store.record_gap(GAP_WORKER_OFFLINE, last_alive, now, "worker was not running")
        self._store.set_runtime_time(_STARTED, now)
        self._persist(now, force=True)
        self._last_tick = now

    def on_tick(self, now: datetime) -> None:
        last = self._last_tick or self._store.get_runtime_time(_LAST_ALIVE)
        if last is not None and now - last > self._suspend_threshold:
            self._store.record_gap(GAP_WORKER_SUSPENDED, last, now, "sleep, hibernation or a stalled loop")
            self._persist(now, force=True)
        self._last_tick = now
        self._persist(now)

    def on_shutdown(self, now: datetime) -> None:
        self._persist(now, force=True)

    def _persist(self, now: datetime, *, force: bool = False) -> None:
        if force or self._last_persist is None or now - self._last_persist >= _PERSIST_EVERY:
            self._store.set_runtime_time(_LAST_ALIVE, now)
            self._last_persist = now

    def on_connection(self, state: ConnectionState, now: datetime) -> None:
        if not state.outlook_running:
            self._store.open_gap(GAP_OUTLOOK_NOT_RUNNING, now)
        else:
            self._store.close_gap(GAP_OUTLOOK_NOT_RUNNING, now)
        if state.outlook_running and state.connected is False:
            self._store.open_gap(GAP_OUTLOOK_DISCONNECTED, now)
        elif state.connected is True or not state.outlook_running:
            self._store.close_gap(GAP_OUTLOOK_DISCONNECTED, now)

    def on_transmission(self, ok: bool, now: datetime, *, kind: str = GAP_BACKEND_UNREACHABLE) -> None:
        if ok:
            self._store.close_gap(kind, now)
        else:
            self._store.open_gap(kind, now)

    def watermark_cap(self, now: datetime, settle: timedelta) -> datetime | None:
        """Earliest start of a detection gap that is open or closed less than ``settle`` ago."""
        cap: datetime | None = None
        for gap in self._store.gaps(kinds=DETECTION_GAP_KINDS):
            if gap.ended_at is None or now - gap.ended_at < settle:
                cap = gap.started_at if cap is None else min(cap, gap.started_at)
        return cap


# =============================================================================================
# Snapshot
# =============================================================================================


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WorkerHealth(_Model):
    status: Status
    started_at: datetime | None
    last_alive_at: datetime | None


class OutlookHealth(_Model):
    status: Status
    running: bool | None
    connected: bool | None
    flavour: str
    supported: bool | None


class MailboxSyncHealth(_Model):
    status: Status
    last_send_receive_end_at: datetime | None
    lag_seconds: int | None


class FolderHealth(_Model):
    role: str
    folder_id_hash: str
    scan_watermark: datetime | None
    acknowledged_watermark: datetime | None
    last_complete_scan_at: datetime | None
    gap_reasons: tuple[str, ...]


class ReconciliationHealth(_Model):
    status: Status
    last_successful_at: datetime | None
    interval_seconds: int
    folders: tuple[FolderHealth, ...]


class BindingSyncHealth(_Model):
    status: Status
    last_complete_at: datetime | None
    active_bindings: int
    tombstoned_bindings: int
    anomalies: int


class BacklogHealth(_Model):
    status: Status
    pending_uploads: int
    oldest_pending_age_seconds: int | None
    acknowledged_uploads: int
    rejected_uploads: int
    conflict_uploads: int
    revoked_uploads: int
    pending_locators: int
    unresolved_matching: int


class ServerHealth(_Model):
    status: Status
    credential_state: CredentialState
    last_success_at: datetime | None
    last_error_kind: str | None


class SendingHealth(_Model):
    enabled: bool
    waiting_intents: int
    unreported_results: int
    attempted_last_24h: int


class HealthSnapshot(_Model):
    generated_at: datetime
    worker: WorkerHealth
    outlook: OutlookHealth
    mailbox_sync: MailboxSyncHealth
    reconciliation: ReconciliationHealth
    binding_sync: BindingSyncHealth
    backlog: BacklogHealth
    server: ServerHealth
    downstream: dict[str, str] = Field(default_factory=dict)
    sending: SendingHealth
    gaps: tuple[GapReport, ...] = ()
    coverage_statement: str = COVERAGE_STATEMENT
    monitoring_24h_claimed: Literal[False] = False


def _age(now: datetime, then: datetime | None) -> int | None:
    return None if then is None else max(0, int((now - then).total_seconds()))


def build_health(
    store: LocalStore,
    *,
    config: BridgeConfig,
    now: datetime,
    connection: ConnectionState | None,
    compat: CompatibilityReport | None,
    credential_state: CredentialState,
    gap_window: timedelta = timedelta(days=15),
) -> HealthSnapshot:
    last_alive = store.get_runtime_time(_LAST_ALIVE)
    worker_status: Status = (
        "ok" if last_alive is not None and now - last_alive < timedelta(minutes=5) else "down"
    )
    if connection is None:
        outlook_status: Status = "unknown"
    elif not connection.outlook_running:
        outlook_status = "down"
    elif connection.connected is False:
        outlook_status = "degraded"
    else:
        outlook_status = "ok" if connection.connected else "unknown"
    sync_end = connection.last_send_receive_end_at if connection else None
    lag = _age(now, sync_end)
    folders = tuple(
        FolderHealth(
            role=cp.role,
            folder_id_hash=cp.folder_id_hash,
            scan_watermark=cp.scan_watermark,
            acknowledged_watermark=store.acknowledged_watermark(cp.folder_key),
            last_complete_scan_at=cp.last_complete_scan_at,
            gap_reasons=cp.gap_reasons,
        )
        for cp in store.folder_checkpoints()
    )
    last_reconcile = store.get_runtime_time("last_successful_reconcile_at")
    stale_after = config.reconcile_interval * 3
    reconcile_status: Status = (
        "ok" if last_reconcile is not None and now - last_reconcile <= stale_after else "degraded"
    )
    sync = store.binding_sync_status()
    by_state: dict[str, int] = sync["by_state"]
    last_sync: datetime | None = sync["last_complete_at"]
    stats = store.backlog_stats()
    oldest_age = _age(now, stats.oldest_pending_created_at)
    unresolved = store.outcome_counts().get(Outcome.MATCHING_GAP.value, 0)
    backlog_status: Status = "ok"
    if stats.pending and oldest_age is not None and oldest_age > 3600:
        backlog_status = "degraded"
    if stats.rejected or stats.conflict:
        backlog_status = "degraded"
    last_ok = store.get_runtime_time("last_server_ok_at")
    last_error = store.get_runtime("last_server_error_kind")
    if credential_state != CredentialState.ACTIVE:
        server_status: Status = "down"
    elif last_ok is None:
        server_status = "unknown"
    else:
        server_status = "ok" if last_error is None else "degraded"
    downstream_raw = store.get_runtime("downstream_health")
    downstream: dict[str, str] = {"slack_signal": "not_observed", "mcp": "not_observed"}
    if downstream_raw:
        try:
            parsed = json.loads(downstream_raw)
            if isinstance(parsed, dict):
                downstream.update({str(k)[:32]: str(v)[:32] for k, v in list(parsed.items())[:10]})
        except json.JSONDecodeError:
            pass
    intents = store.intents()
    gaps = tuple(GapReport.of(g.kind, g.started_at, g.ended_at) for g in store.gaps(since=now - gap_window))[
        -50:
    ]
    return HealthSnapshot(
        generated_at=now,
        worker=WorkerHealth(
            status=worker_status, started_at=store.get_runtime_time(_STARTED), last_alive_at=last_alive
        ),
        outlook=OutlookHealth(
            status=outlook_status,
            running=connection.outlook_running if connection else None,
            connected=connection.connected if connection else None,
            flavour=compat.flavour.value if compat else "unknown",
            supported=compat.supported if compat else None,
        ),
        mailbox_sync=MailboxSyncHealth(
            status="unknown" if lag is None else ("ok" if lag <= 900 else "degraded"),
            last_send_receive_end_at=sync_end,
            lag_seconds=lag,
        ),
        reconciliation=ReconciliationHealth(
            status=reconcile_status,
            last_successful_at=last_reconcile,
            interval_seconds=config.reconcile_interval_seconds,
            folders=folders,
        ),
        binding_sync=BindingSyncHealth(
            status="ok" if last_sync is not None and now - last_sync <= stale_after else "degraded",
            last_complete_at=last_sync,
            active_bindings=sum(v for k, v in by_state.items() if k != "tombstoned"),
            tombstoned_bindings=by_state.get("tombstoned", 0),
            anomalies=int(sync["anomalies"]),
        ),
        backlog=BacklogHealth(
            status=backlog_status,
            pending_uploads=stats.pending,
            oldest_pending_age_seconds=oldest_age,
            acknowledged_uploads=stats.acked,
            rejected_uploads=stats.rejected,
            conflict_uploads=stats.conflict,
            revoked_uploads=stats.revoked,
            pending_locators=store.pending_count(),
            unresolved_matching=unresolved,
        ),
        server=ServerHealth(
            status=server_status,
            credential_state=credential_state,
            last_success_at=last_ok,
            last_error_kind=last_error,
        ),
        downstream=downstream,
        sending=SendingHealth(
            enabled=config.send_intents_enabled,
            waiting_intents=sum(1 for i in intents if i.state == IntentState.RECEIVED),
            unreported_results=sum(1 for i in intents if i.report_json and not i.report_acked),
            attempted_last_24h=store.attempted_since(now - timedelta(hours=24)),
        ),
        gaps=gaps,
    )


def _wire_role(role: str) -> FolderRoleWire:
    if role == "inbox":
        return "inbox"
    if role == "junk":
        return "junk"
    if role == "rule_target":
        return "rule_target"
    return "other"


def heartbeat_envelope(
    store: LocalStore,
    *,
    config: BridgeConfig,
    now: datetime,
    connection: ConnectionState | None,
) -> tuple[HeartbeatEnvelope, tuple[int, ...]]:
    """The heartbeat body and the ids of closed gaps it reports (marked once acknowledged)."""
    stats = store.backlog_stats()
    checkpoints: list[CheckpointReport] = []
    for cp in store.folder_checkpoints()[:20]:
        pending_count, pending_oldest = store.pending_backlog_for_folder(cp.folder_key)
        checkpoints.append(
            CheckpointReport(
                store_id_hash=cp.store_id_hash,
                folder_id_hash=cp.folder_id_hash,
                folder_role=_wire_role(cp.role),
                overlap_watermark=cp.scan_watermark,
                acknowledged_watermark=store.acknowledged_watermark(cp.folder_key),
                last_complete_scan_at=cp.last_complete_scan_at,
                last_scan_started_at=cp.last_scan_started_at,
                backlog_count=pending_count,
                backlog_oldest_at=pending_oldest,
                gap_reasons=cp.gap_reasons[:30],
            )
        )
    candidates = [g for g in store.gaps() if g.ended_at is None or not g.reported]
    reported = candidates[-50:]
    sync_end = connection.last_send_receive_end_at if connection else None
    heartbeat = WorkerHeartbeat(
        mailbox_binding_id=config.mailbox_binding_id,
        worker_id=config.worker_id,
        at=now,
        outlook_running=bool(connection and connection.outlook_running),
        mailbox_connected=bool(connection and connection.connected),
        sync_lag_seconds=_age(now, sync_end),
        pending_intents=len(store.intents([IntentState.RECEIVED])),
    )
    envelope = HeartbeatEnvelope(
        heartbeat=heartbeat,
        last_successful_reconciliation_at=store.get_runtime_time("last_successful_reconcile_at"),
        mailbox_last_sync_at=sync_end,
        backlog_count=stats.pending,
        backlog_oldest_age_seconds=_age(now, stats.oldest_pending_created_at),
        unresolved_matching_gaps=store.outcome_counts().get(Outcome.MATCHING_GAP.value, 0),
        checkpoints=tuple(checkpoints),
        gaps=tuple(GapReport.of(g.kind, g.started_at, g.ended_at) for g in reported),
    )
    return envelope, tuple(g.id for g in reported if g.ended_at is not None)


__all__ = [
    "COVERAGE_STATEMENT",
    "DETECTION_GAP_KINDS",
    "GAP_ACCOUNT_CHANGED",
    "GAP_AMBIGUOUS_MATCH",
    "GAP_BACKEND_UNREACHABLE",
    "GAP_BINDING_SYNC_INCOMPLETE",
    "GAP_CATCHUP_EXCEEDED",
    "GAP_CREDENTIAL_UNUSABLE",
    "GAP_CROSS_MAILBOX",
    "GAP_EVENT_OVERFLOW",
    "GAP_FOLDER_SCOPE",
    "GAP_ITEM_UNREADABLE",
    "GAP_OUTLOOK_DISCONNECTED",
    "GAP_OUTLOOK_NOT_RUNNING",
    "GAP_PENDING_CAPACITY",
    "GAP_UNRESOLVED_MATCHING",
    "GAP_UPLOAD_BUILD_FAILED",
    "GAP_WORKER_OFFLINE",
    "GAP_WORKER_SUSPENDED",
    "CoverageMonitor",
    "HealthSnapshot",
    "build_health",
    "heartbeat_envelope",
]
