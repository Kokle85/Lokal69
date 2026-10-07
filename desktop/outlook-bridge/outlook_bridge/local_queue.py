"""Durable local state: binding cache, processed keys, upload backlog, checkpoints, send intents.

SQLite in the user's protected application-data directory (WAL, ``synchronous=FULL``; owner-only
file mode on POSIX, per-user profile ACL on Windows). One store belongs to exactly one mailbox
binding; opening it for another mailbox fails (``MailboxMismatch``).

Durability rules (spec 37.6/37.8):

- A binding page and its sync cursor are committed atomically; a binding for another mailbox
  rolls the whole page back. Versions only grow, a tombstone is final, and two different payloads
  under one version fail closed (tombstoned) until the server publishes a newer version.
- An item's processing outcome and its upload-backlog entry (or bounded pending locator) are
  committed in one transaction. Folder scan watermarks advance only after the scan's items are
  committed; the *acknowledged* watermark never passes an item that the server has not yet
  acknowledged (or a reply candidate still waiting for a binding).
- Unrelated mail leaves only a hashed key and an outcome code locally - never a subject, body,
  sender or header. Pending reply-before-binding locators keep EntryID/StoreID, the Internet
  Message-ID and *hashes* of the referenced Message-IDs, nothing else, for a bounded window.
- Send intents are recorded before any claim/transmission and an ``attempting`` row is
  committed before ``MailItem.Send``; an intent id is never attempted twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import EmailProviderKind
from suv_deals.domain.replies import InquiryBinding, InquiryBindingState

from outlook_bridge.errors import LocalStoreError, MailboxMismatch

SCHEMA_VERSION: Final = 1
_BUSY_TIMEOUT_MS: Final = 10_000

_SCHEMA: Final = """
create table if not exists meta (key text primary key, value text not null);
create table if not exists bindings (
  inquiry_id text primary key,
  binding_version integer not null check (binding_version >= 1),
  state text not null check (state in ('active', 'suppressed', 'uncertain', 'tombstoned')),
  payload text,
  revoked text,
  updated_at text not null,
  check ((state = 'tombstoned') = (payload is null))
);
create table if not exists binding_sync (
  id integer primary key check (id = 1),
  cursor text,
  last_page_at text,
  last_complete_at text,
  pages integer not null default 0,
  anomalies integer not null default 0
);
create table if not exists folders (
  folder_key text primary key,
  role text not null,
  store_id_hash text not null,
  folder_id_hash text not null,
  scan_watermark text,
  last_scan_started_at text,
  last_complete_scan_at text,
  gap_reasons text not null default '[]'
);
create table if not exists processed (
  key_hash text primary key,
  outcome text not null,
  inquiry_id text,
  received_at text,
  first_seen_at text not null,
  last_seen_at text not null,
  entry_id text,
  store_id text,
  folder_key text
);
create index if not exists processed_received_idx on processed (received_at);
create table if not exists locator_history (
  id integer primary key autoincrement,
  key_hash text not null,
  entry_id text not null,
  store_id text not null,
  folder_key text,
  seen_at text not null
);
create index if not exists locator_history_key_idx on locator_history (key_hash);
create table if not exists pending_matches (
  key_hash text primary key,
  internet_message_id text,
  entry_id text not null,
  store_id text not null,
  folder_key text,
  received_at text,
  reference_hashes text not null,
  own_reference integer not null check (own_reference in (0, 1)),
  first_seen_at text not null,
  retry_until text not null,
  attempts integer not null default 0
);
create table if not exists upload_backlog (
  id integer primary key autoincrement,
  idempotency_key text not null unique,
  dedup_key_hash text not null unique,
  inquiry_id text not null,
  binding_version integer not null,
  folder_key text,
  received_at text not null,
  request_json text not null,
  state text not null check (state in ('pending', 'acked', 'rejected', 'conflict', 'revoked')),
  created_at text not null,
  attempts integer not null default 0,
  next_attempt_at text not null,
  last_error text,
  reply_id text,
  duplicate integer,
  acked_at text,
  request_id text
);
create index if not exists upload_backlog_due_idx on upload_backlog (state, next_attempt_at);
create table if not exists item_failures (
  entry_hash text primary key,
  failures integer not null,
  last_at text not null
);
create table if not exists send_intents (
  intent_id text primary key,
  inquiry_id text not null,
  payload_json text not null,
  state text not null check (state in (
    'received', 'refused', 'attempting', 'send_failed', 'submitted', 'confirmed')),
  received_at text not null,
  attempt_started_at text,
  submitted_at text,
  confirmed_at text,
  report_json text,
  report_acked integer not null default 0,
  observed_message_id text,
  error_code text
);
create table if not exists gaps (
  id integer primary key autoincrement,
  kind text not null,
  started_at text not null,
  ended_at text,
  detail text,
  reported integer not null default 0
);
create index if not exists gaps_open_idx on gaps (kind, ended_at);
create table if not exists runtime (key text primary key, value text not null);
"""


def ts(value: datetime) -> str:
    """Fixed-width, sortable UTC timestamp text."""
    return ensure_utc(value).isoformat(timespec="microseconds")


def parse_ts(value: str | None) -> datetime | None:
    return None if value is None else ensure_utc(datetime.fromisoformat(value))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def message_id_hash(message_id: str) -> str:
    """Local pseudonymous key of a Message-ID (pending-locator retry targeting)."""
    return sha256_text("msgid\x00" + message_id)


class Outcome(StrEnum):
    UPLOAD_QUEUED = "upload_queued"
    QUARANTINE_QUEUED = "quarantine_queued"
    UNRELATED = "unrelated"
    NON_MAIL = "non_mail"
    PENDING_BINDING = "pending_binding"
    MATCHING_GAP = "matching_gap"
    LOCAL_REJECT = "local_reject"
    UNREADABLE = "unreadable"


TERMINAL_OUTCOMES: Final = frozenset(Outcome) - {Outcome.PENDING_BINDING}


class BacklogState(StrEnum):
    PENDING = "pending"
    ACKED = "acked"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    REVOKED = "revoked"


class IntentState(StrEnum):
    RECEIVED = "received"
    REFUSED = "refused"
    ATTEMPTING = "attempting"
    SEND_FAILED = "send_failed"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"


ATTEMPTED_INTENT_STATES: Final = frozenset(
    {IntentState.ATTEMPTING, IntentState.SEND_FAILED, IntentState.SUBMITTED, IntentState.CONFIRMED}
)


@dataclass(frozen=True, slots=True)
class BindingRecord:
    """A validated binding-sync item ready to be cached (tombstones have no binding)."""

    inquiry_id: UUID
    binding_version: int
    mailbox_binding_id: UUID
    state: InquiryBindingState
    binding: InquiryBinding | None


@dataclass(frozen=True, slots=True)
class BindingApplyResult:
    applied: int
    ignored_stale: int
    conflicts: int
    tombstoned: tuple[UUID, ...]
    new_message_id_hashes: frozenset[str]
    cursor_advanced: bool


@dataclass(frozen=True, slots=True)
class ProcessedRow:
    key_hash: str
    outcome: Outcome
    inquiry_id: UUID | None
    received_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    entry_id: str | None
    store_id: str | None
    folder_key: str | None


@dataclass(frozen=True, slots=True)
class PendingLocator:
    key_hash: str
    internet_message_id: str | None
    entry_id: str
    store_id: str
    folder_key: str | None
    received_at: datetime | None
    reference_hashes: frozenset[str]
    own_reference: bool
    first_seen_at: datetime
    retry_until: datetime
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class BacklogEntry:
    idempotency_key: str
    dedup_key_hash: str
    inquiry_id: UUID
    binding_version: int
    folder_key: str | None
    received_at: datetime
    request_json: str


@dataclass(frozen=True, slots=True)
class BacklogRow:
    id: int
    idempotency_key: str
    dedup_key_hash: str
    inquiry_id: UUID
    binding_version: int
    folder_key: str | None
    received_at: datetime
    request_json: str
    state: BacklogState
    created_at: datetime
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    reply_id: UUID | None
    acked_at: datetime | None


@dataclass(frozen=True, slots=True)
class BacklogStats:
    pending: int
    oldest_pending_created_at: datetime | None
    oldest_pending_received_at: datetime | None
    acked: int
    rejected: int
    conflict: int
    revoked: int


@dataclass(frozen=True, slots=True)
class FolderCheckpoint:
    folder_key: str
    role: str
    store_id_hash: str
    folder_id_hash: str
    scan_watermark: datetime | None
    last_scan_started_at: datetime | None
    last_complete_scan_at: datetime | None
    gap_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IntentRow:
    intent_id: UUID
    inquiry_id: UUID
    payload_json: str
    state: IntentState
    received_at: datetime
    attempt_started_at: datetime | None
    submitted_at: datetime | None
    confirmed_at: datetime | None
    report_json: str | None
    report_acked: bool
    observed_message_id: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class GapRow:
    id: int
    kind: str
    started_at: datetime
    ended_at: datetime | None
    detail: str | None
    reported: bool


def _binding_payload(binding: InquiryBinding) -> str:
    return json.dumps(binding.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class LocalStore:
    """The worker's protected SQLite store (single writer: the worker thread)."""

    def __init__(self, connection: sqlite3.Connection, mailbox_binding_id: UUID) -> None:
        self._db = connection
        self._mailbox = mailbox_binding_id

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def open(cls, path: Path, mailbox_binding_id: UUID) -> LocalStore:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(path.parent, stat.S_IRWXU)
        existed = path.exists()
        try:
            connection = sqlite3.connect(str(path), isolation_level=None, timeout=_BUSY_TIMEOUT_MS / 1000)
        except sqlite3.Error as exc:
            raise LocalStoreError(f"cannot open the local store ({type(exc).__name__})") from None
        if os.name == "posix" and not existed:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        store = cls(connection, mailbox_binding_id)
        store._initialise(wal=True)
        return store

    @classmethod
    def in_memory(cls, mailbox_binding_id: UUID) -> LocalStore:
        """Non-persistent store (dry runs and tests)."""
        store = cls(sqlite3.connect(":memory:", isolation_level=None), mailbox_binding_id)
        store._initialise(wal=False)
        return store

    def _initialise(self, *, wal: bool) -> None:
        db = self._db
        db.execute(f"pragma busy_timeout = {_BUSY_TIMEOUT_MS}")
        if wal:
            mode = db.execute("pragma journal_mode = wal").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise LocalStoreError("the local store could not enable WAL journaling")
        db.execute("pragma synchronous = full")
        db.execute("pragma foreign_keys = on")
        with self._tx():
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            row = db.execute("select value from meta where key = 'mailbox_binding_id'").fetchone()
            if row is None:
                db.execute(
                    "insert into meta (key, value) values ('mailbox_binding_id', ?), ('schema_version', ?)",
                    (str(self._mailbox), str(SCHEMA_VERSION)),
                )
                db.execute("insert or ignore into binding_sync (id) values (1)")
            elif row[0] != str(self._mailbox):
                raise MailboxMismatch("the local store belongs to another mailbox binding")

    def close(self) -> None:
        self._db.close()

    @property
    def mailbox_binding_id(self) -> UUID:
        return self._mailbox

    def journal_mode(self) -> str:
        row = self._db.execute("pragma journal_mode").fetchone()
        return str(row[0]) if row else "unknown"

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT`` (rollback on any error)."""
        db = self._db
        db.execute("begin immediate")
        try:
            yield db
        except BaseException:
            db.execute("rollback")
            raise
        else:
            db.execute("commit")

    # ------------------------------------------------------------------ runtime values

    def get_runtime(self, key: str) -> str | None:
        row = self._db.execute("select value from runtime where key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_runtime(self, key: str, value: str) -> None:
        with self._tx() as db:
            db.execute(
                "insert into runtime (key, value) values (?, ?) on conflict (key) do update set value = excluded.value",
                (key, value),
            )

    def delete_runtime(self, key: str) -> None:
        with self._tx() as db:
            db.execute("delete from runtime where key = ?", (key,))

    def get_runtime_time(self, key: str) -> datetime | None:
        return parse_ts(self.get_runtime(key))

    def set_runtime_time(self, key: str, value: datetime) -> None:
        self.set_runtime(key, ts(value))

    # ------------------------------------------------------------------ bindings

    def binding_cursor(self) -> str | None:
        row = self._db.execute("select cursor from binding_sync where id = 1").fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def binding_sync_status(self) -> dict[str, Any]:
        row = self._db.execute(
            "select cursor is not null, last_page_at, last_complete_at, pages, anomalies from binding_sync where id = 1"
        ).fetchone()
        counts = dict(self._db.execute("select state, count(*) from bindings group by state").fetchall())
        return {
            "has_cursor": bool(row[0]) if row else False,
            "last_page_at": parse_ts(row[1]) if row else None,
            "last_complete_at": parse_ts(row[2]) if row else None,
            "pages": int(row[3]) if row else 0,
            "anomalies": int(row[4]) if row else 0,
            "by_state": {str(k): int(v) for k, v in counts.items()},
        }

    def apply_binding_page(
        self,
        records: Sequence[BindingRecord],
        next_cursor: str | None,
        *,
        complete: bool,
        now: datetime,
    ) -> BindingApplyResult:
        """Apply one validated page and its cursor atomically (see module docstring)."""
        applied = stale = conflicts = 0
        tombstoned: list[UUID] = []
        new_hashes: set[str] = set()
        with self._tx() as db:
            for record in records:
                if record.mailbox_binding_id != self._mailbox or (
                    record.binding is not None and record.binding.mailbox_binding_id != self._mailbox
                ):
                    raise MailboxMismatch("binding page contains a binding of another mailbox")
                key = str(record.inquiry_id)
                row = db.execute(
                    "select binding_version, state, payload from bindings where inquiry_id = ?", (key,)
                ).fetchone()
                previous: InquiryBinding | None = None
                if row is not None:
                    version, state, payload = int(row[0]), str(row[1]), row[2]
                    if state == InquiryBindingState.TOMBSTONED:
                        stale += 1
                        continue  # a tombstone is final
                    previous = InquiryBinding.model_validate_json(payload)
                    if record.binding_version < version:
                        stale += 1
                        continue
                    if record.binding_version == version:
                        if record.binding is not None and _binding_payload(record.binding) == payload:
                            continue
                        conflicts += 1
                        self._tombstone(db, record.inquiry_id, version, previous, now)
                        tombstoned.append(record.inquiry_id)
                        db.execute("update binding_sync set anomalies = anomalies + 1 where id = 1")
                        continue
                if record.state == InquiryBindingState.TOMBSTONED or record.binding is None:
                    self._tombstone(db, record.inquiry_id, record.binding_version, previous, now)
                    tombstoned.append(record.inquiry_id)
                    applied += 1
                    continue
                known = previous.all_message_ids() if previous is not None else frozenset()
                for message_id in record.binding.all_message_ids() - known:
                    new_hashes.add(message_id_hash(message_id))
                db.execute(
                    "insert into bindings (inquiry_id, binding_version, state, payload, revoked, updated_at)"
                    " values (?, ?, ?, ?, null, ?) on conflict (inquiry_id) do update set"
                    " binding_version = excluded.binding_version, state = excluded.state,"
                    " payload = excluded.payload, revoked = null, updated_at = excluded.updated_at",
                    (key, record.binding_version, record.binding.state.value, _binding_payload(record.binding), ts(now)),
                )
                applied += 1
            advanced = next_cursor is not None
            db.execute(
                "update binding_sync set cursor = coalesce(?, cursor), last_page_at = ?,"
                " last_complete_at = case when ? then ? else last_complete_at end, pages = pages + 1 where id = 1",
                (next_cursor, ts(now), 1 if complete else 0, ts(now)),
            )
        return BindingApplyResult(
            applied=applied,
            ignored_stale=stale,
            conflicts=conflicts,
            tombstoned=tuple(tombstoned),
            new_message_id_hashes=frozenset(new_hashes),
            cursor_advanced=advanced,
        )

    @staticmethod
    def _tombstone(
        db: sqlite3.Connection, inquiry_id: UUID, version: int, previous: InquiryBinding | None, now: datetime
    ) -> None:
        revoked = None
        if previous is not None:
            revoked = json.dumps(
                {"provider": previous.provider.value, "message_ids": sorted(previous.all_message_ids())},
                separators=(",", ":"),
            )
        db.execute(
            "insert into bindings (inquiry_id, binding_version, state, payload, revoked, updated_at)"
            " values (?, ?, 'tombstoned', null, ?, ?) on conflict (inquiry_id) do update set"
            " binding_version = max(bindings.binding_version, excluded.binding_version), state = 'tombstoned',"
            " payload = null, revoked = coalesce(excluded.revoked, bindings.revoked), updated_at = excluded.updated_at",
            (str(inquiry_id), version, revoked, ts(now)),
        )

    def bindings_for_matching(self) -> tuple[InquiryBinding, ...]:
        """Usable bindings plus tombstones carrying the revoked ids (they never grant access)."""
        result: list[InquiryBinding] = []
        for inquiry_id, version, state, payload, revoked in self._db.execute(
            "select inquiry_id, binding_version, state, payload, revoked from bindings order by inquiry_id"
        ):
            if state != InquiryBindingState.TOMBSTONED:
                result.append(InquiryBinding.model_validate_json(payload))
                continue
            info = json.loads(revoked) if revoked else {"provider": EmailProviderKind.OUTLOOK_LOCAL.value}
            ids = tuple(info.get("message_ids", ()))[:20]
            result.append(
                InquiryBinding(
                    inquiry_id=UUID(inquiry_id),
                    binding_version=int(version),
                    mailbox_binding_id=self._mailbox,
                    provider=EmailProviderKind(info.get("provider", EmailProviderKind.OUTLOOK_LOCAL.value)),
                    state=InquiryBindingState.TOMBSTONED,
                    outbound_message_ids=ids,
                )
            )
        return tuple(result)

    def binding_state(self, inquiry_id: UUID) -> tuple[InquiryBindingState, int] | None:
        row = self._db.execute(
            "select state, binding_version from bindings where inquiry_id = ?", (str(inquiry_id),)
        ).fetchone()
        return None if row is None else (InquiryBindingState(row[0]), int(row[1]))

    # ------------------------------------------------------------------ processed items

    def processed(self, key_hash: str) -> ProcessedRow | None:
        row = self._db.execute(
            "select key_hash, outcome, inquiry_id, received_at, first_seen_at, last_seen_at, entry_id, store_id,"
            " folder_key from processed where key_hash = ?",
            (key_hash,),
        ).fetchone()
        if row is None:
            return None
        return ProcessedRow(
            key_hash=row[0],
            outcome=Outcome(row[1]),
            inquiry_id=UUID(row[2]) if row[2] else None,
            received_at=parse_ts(row[3]),
            first_seen_at=ensure_utc(datetime.fromisoformat(row[4])),
            last_seen_at=ensure_utc(datetime.fromisoformat(row[5])),
            entry_id=row[6],
            store_id=row[7],
            folder_key=row[8],
        )

    def record_outcome(
        self,
        *,
        key_hash: str,
        outcome: Outcome,
        now: datetime,
        inquiry_id: UUID | None = None,
        received_at: datetime | None = None,
        entry_id: str | None = None,
        store_id: str | None = None,
        folder_key: str | None = None,
        backlog: BacklogEntry | None = None,
        pending: PendingLocator | None = None,
    ) -> int | None:
        """Commit an item's outcome with its backlog entry or pending locator atomically."""
        if backlog is not None and outcome not in (Outcome.UPLOAD_QUEUED, Outcome.QUARANTINE_QUEUED):
            raise LocalStoreError("only correlated outcomes may enqueue an upload")
        if (pending is not None) != (outcome == Outcome.PENDING_BINDING):
            raise LocalStoreError("a pending locator belongs exactly to the pending_binding outcome")
        backlog_id: int | None = None
        with self._tx() as db:
            db.execute(
                "insert into processed (key_hash, outcome, inquiry_id, received_at, first_seen_at, last_seen_at,"
                " entry_id, store_id, folder_key) values (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " on conflict (key_hash) do update set outcome = excluded.outcome,"
                " inquiry_id = coalesce(excluded.inquiry_id, processed.inquiry_id),"
                " received_at = coalesce(processed.received_at, excluded.received_at),"
                " last_seen_at = excluded.last_seen_at, entry_id = coalesce(excluded.entry_id, processed.entry_id),"
                " store_id = coalesce(excluded.store_id, processed.store_id),"
                " folder_key = coalesce(excluded.folder_key, processed.folder_key)",
                (
                    key_hash,
                    outcome.value,
                    str(inquiry_id) if inquiry_id else None,
                    ts(received_at) if received_at else None,
                    ts(now),
                    ts(now),
                    entry_id,
                    store_id,
                    folder_key,
                ),
            )
            if backlog is not None:
                cursor = db.execute(
                    "insert into upload_backlog (idempotency_key, dedup_key_hash, inquiry_id, binding_version,"
                    " folder_key, received_at, request_json, state, created_at, next_attempt_at)"
                    " values (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)"
                    " on conflict (dedup_key_hash) do nothing",
                    (
                        backlog.idempotency_key,
                        backlog.dedup_key_hash,
                        str(backlog.inquiry_id),
                        backlog.binding_version,
                        backlog.folder_key,
                        ts(backlog.received_at),
                        backlog.request_json,
                        ts(now),
                        ts(now),
                    ),
                )
                backlog_id = cursor.lastrowid if cursor.rowcount else None
            if pending is not None:
                db.execute(
                    "insert into pending_matches (key_hash, internet_message_id, entry_id, store_id, folder_key,"
                    " received_at, reference_hashes, own_reference, first_seen_at, retry_until, attempts)"
                    " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) on conflict (key_hash) do update set"
                    " entry_id = excluded.entry_id, store_id = excluded.store_id, folder_key = excluded.folder_key,"
                    " attempts = pending_matches.attempts + 1",
                    (
                        pending.key_hash,
                        pending.internet_message_id,
                        pending.entry_id,
                        pending.store_id,
                        pending.folder_key,
                        ts(pending.received_at) if pending.received_at else None,
                        json.dumps(sorted(pending.reference_hashes)),
                        1 if pending.own_reference else 0,
                        ts(pending.first_seen_at),
                        ts(pending.retry_until),
                        pending.attempts,
                    ),
                )
            else:
                db.execute("delete from pending_matches where key_hash = ?", (key_hash,))
        return backlog_id

    def record_locator(
        self, key_hash: str, *, entry_id: str, store_id: str, folder_key: str | None, now: datetime
    ) -> bool:
        """Remember a changed locator (moved message); returns whether it changed."""
        with self._tx() as db:
            row = db.execute(
                "select entry_id, store_id, folder_key from processed where key_hash = ?", (key_hash,)
            ).fetchone()
            if row is None:
                return False
            changed = (row[0], row[1], row[2]) != (entry_id, store_id, folder_key)
            db.execute(
                "update processed set last_seen_at = ?, entry_id = ?, store_id = ?, folder_key = ? where key_hash = ?",
                (ts(now), entry_id, store_id, folder_key, key_hash),
            )
            if changed:
                db.execute(
                    "insert into locator_history (key_hash, entry_id, store_id, folder_key, seen_at)"
                    " values (?, ?, ?, ?, ?)",
                    (key_hash, entry_id, store_id, folder_key, ts(now)),
                )
                db.execute(
                    "update pending_matches set entry_id = ?, store_id = ?, folder_key = ? where key_hash = ?",
                    (entry_id, store_id, folder_key, key_hash),
                )
            return changed

    def locator_history(self, key_hash: str) -> list[tuple[str, str, str | None]]:
        return [
            (str(r[0]), str(r[1]), r[2])
            for r in self._db.execute(
                "select entry_id, store_id, folder_key from locator_history where key_hash = ? order by id",
                (key_hash,),
            )
        ]

    def outcome_counts(self) -> dict[str, int]:
        return {str(k): int(v) for k, v in self._db.execute("select outcome, count(*) from processed group by outcome")}

    # ------------------------------------------------------------------ item read failures

    def note_item_failure(self, entry_hash: str, now: datetime) -> int:
        with self._tx() as db:
            db.execute(
                "insert into item_failures (entry_hash, failures, last_at) values (?, 1, ?)"
                " on conflict (entry_hash) do update set failures = item_failures.failures + 1, last_at = excluded.last_at",
                (entry_hash, ts(now)),
            )
            row = db.execute("select failures from item_failures where entry_hash = ?", (entry_hash,)).fetchone()
        return int(row[0]) if row else 1

    # ------------------------------------------------------------------ pending locators

    def _pending_from_row(self, row: Sequence[Any]) -> PendingLocator:
        return PendingLocator(
            key_hash=row[0],
            internet_message_id=row[1],
            entry_id=row[2],
            store_id=row[3],
            folder_key=row[4],
            received_at=parse_ts(row[5]),
            reference_hashes=frozenset(json.loads(row[6])),
            own_reference=bool(row[7]),
            first_seen_at=ensure_utc(datetime.fromisoformat(row[8])),
            retry_until=ensure_utc(datetime.fromisoformat(row[9])),
            attempts=int(row[10]),
        )

    _PENDING_COLUMNS: Final = (
        "key_hash, internet_message_id, entry_id, store_id, folder_key, received_at, reference_hashes,"
        " own_reference, first_seen_at, retry_until, attempts"
    )

    def pending_locators(self, *, limit: int = 10_000) -> list[PendingLocator]:
        return [
            self._pending_from_row(r)
            for r in self._db.execute(
                f"select {self._PENDING_COLUMNS} from pending_matches order by first_seen_at limit ?",  # noqa: S608
                (limit,),
            )
        ]

    def pending_matching(self, message_id_hashes: Iterable[str], *, limit: int = 500) -> list[PendingLocator]:
        wanted = set(message_id_hashes)
        if not wanted:
            return []
        return [p for p in self.pending_locators() if p.reference_hashes & wanted][:limit]

    def pending_count(self) -> int:
        row = self._db.execute("select count(*) from pending_matches").fetchone()
        return int(row[0]) if row else 0

    def take_expired_pending(self, now: datetime) -> list[PendingLocator]:
        """Remove and return locators whose bounded retry window has elapsed."""
        with self._tx() as db:
            rows = db.execute(
                f"select {self._PENDING_COLUMNS} from pending_matches where retry_until <= ?",  # noqa: S608
                (ts(now),),
            ).fetchall()
            expired = [self._pending_from_row(r) for r in rows]
            for item in expired:
                db.execute("delete from pending_matches where key_hash = ?", (item.key_hash,))
                outcome = Outcome.MATCHING_GAP if item.own_reference else Outcome.UNRELATED
                db.execute(
                    "update processed set outcome = ?, last_seen_at = ? where key_hash = ?",
                    (outcome.value, ts(now), item.key_hash),
                )
        return expired

    def evict_pending_over(self, capacity: int, now: datetime) -> list[PendingLocator]:
        """Keep at most ``capacity`` locators: drop foreign-reference ones first, oldest first."""
        excess = self.pending_count() - capacity
        if excess <= 0:
            return []
        candidates = sorted(self.pending_locators(), key=lambda p: (p.own_reference, p.first_seen_at))[:excess]
        with self._tx() as db:
            for item in candidates:
                db.execute("delete from pending_matches where key_hash = ?", (item.key_hash,))
                outcome = Outcome.MATCHING_GAP if item.own_reference else Outcome.UNRELATED
                db.execute(
                    "update processed set outcome = ?, last_seen_at = ? where key_hash = ?",
                    (outcome.value, ts(now), item.key_hash),
                )
        return candidates

    # ------------------------------------------------------------------ upload backlog

    _BACKLOG_COLUMNS: Final = (
        "id, idempotency_key, dedup_key_hash, inquiry_id, binding_version, folder_key, received_at, request_json,"
        " state, created_at, attempts, next_attempt_at, last_error, reply_id, acked_at"
    )

    @staticmethod
    def _backlog_from_row(row: Sequence[Any]) -> BacklogRow:
        return BacklogRow(
            id=int(row[0]),
            idempotency_key=row[1],
            dedup_key_hash=row[2],
            inquiry_id=UUID(row[3]),
            binding_version=int(row[4]),
            folder_key=row[5],
            received_at=ensure_utc(datetime.fromisoformat(row[6])),
            request_json=row[7],
            state=BacklogState(row[8]),
            created_at=ensure_utc(datetime.fromisoformat(row[9])),
            attempts=int(row[10]),
            next_attempt_at=ensure_utc(datetime.fromisoformat(row[11])),
            last_error=row[12],
            reply_id=UUID(row[13]) if row[13] else None,
            acked_at=parse_ts(row[14]),
        )

    def due_uploads(self, now: datetime, *, limit: int) -> list[BacklogRow]:
        return [
            self._backlog_from_row(r)
            for r in self._db.execute(
                f"select {self._BACKLOG_COLUMNS} from upload_backlog"  # noqa: S608
                " where state = 'pending' and next_attempt_at <= ? order by received_at, id limit ?",
                (ts(now), limit),
            )
        ]

    def backlog_rows(self, state: BacklogState | None = None) -> list[BacklogRow]:
        query = f"select {self._BACKLOG_COLUMNS} from upload_backlog"  # noqa: S608
        params: tuple[Any, ...] = ()
        if state is not None:
            query += " where state = ?"
            params = (state.value,)
        return [self._backlog_from_row(r) for r in self._db.execute(query + " order by id", params)]

    def mark_upload_acked(
        self, backlog_id: int, *, reply_id: UUID, duplicate: bool, request_id: str | None, now: datetime
    ) -> None:
        with self._tx() as db:
            db.execute(
                "update upload_backlog set state = 'acked', reply_id = ?, duplicate = ?, request_id = ?, acked_at = ?,"
                " attempts = attempts + 1, last_error = null where id = ? and state = 'pending'",
                (str(reply_id), 1 if duplicate else 0, request_id, ts(now), backlog_id),
            )

    def mark_upload_deferred(self, backlog_id: int, *, next_attempt_at: datetime, error_code: str) -> None:
        with self._tx() as db:
            db.execute(
                "update upload_backlog set attempts = attempts + 1, next_attempt_at = ?, last_error = ?"
                " where id = ? and state = 'pending'",
                (ts(next_attempt_at), error_code[:64], backlog_id),
            )

    def mark_upload_final(self, backlog_id: int, *, state: BacklogState, error_code: str, now: datetime) -> None:
        if state in (BacklogState.PENDING, BacklogState.ACKED):
            raise LocalStoreError("final upload states are rejected/conflict/revoked")
        with self._tx() as db:
            db.execute(
                "update upload_backlog set state = ?, last_error = ?, attempts = attempts + 1, acked_at = null"
                " where id = ? and state = 'pending'",
                (state.value, error_code[:64], backlog_id),
            )
            db.execute(
                "insert into gaps (kind, started_at, ended_at, detail) values (?, ?, ?, ?)",
                (f"upload_{state.value}", ts(now), ts(now), error_code[:64]),
            )

    def update_upload_request(self, backlog_id: int, *, binding_version: int, request_json: str) -> None:
        with self._tx() as db:
            db.execute(
                "update upload_backlog set binding_version = ?, request_json = ? where id = ? and state = 'pending'",
                (binding_version, request_json, backlog_id),
            )

    def pending_backlog_for_folder(self, folder_key: str) -> tuple[int, datetime | None]:
        row = self._db.execute(
            "select count(*), min(received_at) from upload_backlog where folder_key = ? and state = 'pending'",
            (folder_key,),
        ).fetchone()
        return (int(row[0]), parse_ts(row[1])) if row else (0, None)

    def backlog_stats(self) -> BacklogStats:
        counts = dict(self._db.execute("select state, count(*) from upload_backlog group by state").fetchall())
        oldest = self._db.execute(
            "select min(created_at), min(received_at) from upload_backlog where state = 'pending'"
        ).fetchone()
        return BacklogStats(
            pending=int(counts.get("pending", 0)),
            oldest_pending_created_at=parse_ts(oldest[0]) if oldest else None,
            oldest_pending_received_at=parse_ts(oldest[1]) if oldest else None,
            acked=int(counts.get("acked", 0)),
            rejected=int(counts.get("rejected", 0)),
            conflict=int(counts.get("conflict", 0)),
            revoked=int(counts.get("revoked", 0)),
        )

    # ------------------------------------------------------------------ folders and checkpoints

    def upsert_folder(self, *, folder_key: str, role: str, store_id_hash: str, folder_id_hash: str) -> None:
        with self._tx() as db:
            db.execute(
                "insert into folders (folder_key, role, store_id_hash, folder_id_hash) values (?, ?, ?, ?)"
                " on conflict (folder_key) do update set role = excluded.role",
                (folder_key, role, store_id_hash, folder_id_hash),
            )

    def folder_checkpoint(self, folder_key: str) -> FolderCheckpoint | None:
        row = self._db.execute(
            "select folder_key, role, store_id_hash, folder_id_hash, scan_watermark, last_scan_started_at,"
            " last_complete_scan_at, gap_reasons from folders where folder_key = ?",
            (folder_key,),
        ).fetchone()
        if row is None:
            return None
        return FolderCheckpoint(
            folder_key=row[0],
            role=row[1],
            store_id_hash=row[2],
            folder_id_hash=row[3],
            scan_watermark=parse_ts(row[4]),
            last_scan_started_at=parse_ts(row[5]),
            last_complete_scan_at=parse_ts(row[6]),
            gap_reasons=tuple(json.loads(row[7])),
        )

    def folder_checkpoints(self) -> list[FolderCheckpoint]:
        keys = [r[0] for r in self._db.execute("select folder_key from folders order by folder_key")]
        return [cp for key in keys if (cp := self.folder_checkpoint(key)) is not None]

    def begin_scan(self, folder_key: str, now: datetime) -> None:
        with self._tx() as db:
            db.execute("update folders set last_scan_started_at = ? where folder_key = ?", (ts(now), folder_key))

    def finish_scan(
        self,
        folder_key: str,
        *,
        complete: bool,
        new_watermark: datetime | None,
        gap_reasons: Sequence[str],
        now: datetime,
    ) -> None:
        """Advance the scan watermark (monotonic) only for a complete scan."""
        with self._tx() as db:
            row = db.execute("select scan_watermark from folders where folder_key = ?", (folder_key,)).fetchone()
            if row is None:
                raise LocalStoreError("unknown folder checkpoint")
            current = parse_ts(row[0])
            watermark = current
            if complete and new_watermark is not None and (current is None or new_watermark > current):
                watermark = new_watermark
            db.execute(
                "update folders set scan_watermark = ?, gap_reasons = ?,"
                " last_complete_scan_at = case when ? then ? else last_complete_scan_at end where folder_key = ?",
                (
                    ts(watermark) if watermark else None,
                    json.dumps(list(dict.fromkeys(gap_reasons))[:30]),
                    1 if complete else 0,
                    ts(now),
                    folder_key,
                ),
            )

    def acknowledged_watermark(self, folder_key: str) -> datetime | None:
        """Everything received before this instant in the folder is acknowledged or resolved."""
        checkpoint = self.folder_checkpoint(folder_key)
        if checkpoint is None:
            return None
        row = self._db.execute(
            "select min(t) from (select min(received_at) as t from upload_backlog where folder_key = ? and"
            " state = 'pending' union all select min(received_at) from pending_matches where folder_key = ?)",
            (folder_key, folder_key),
        ).fetchone()
        unacked = parse_ts(row[0]) if row and row[0] else None
        watermark = checkpoint.scan_watermark
        if unacked is not None and (watermark is None or unacked < watermark):
            return unacked
        return watermark

    # ------------------------------------------------------------------ send intents

    _INTENT_COLUMNS: Final = (
        "intent_id, inquiry_id, payload_json, state, received_at, attempt_started_at, submitted_at, confirmed_at,"
        " report_json, report_acked, observed_message_id, error_code"
    )

    @staticmethod
    def _intent_from_row(row: Sequence[Any]) -> IntentRow:
        return IntentRow(
            intent_id=UUID(row[0]),
            inquiry_id=UUID(row[1]),
            payload_json=row[2],
            state=IntentState(row[3]),
            received_at=ensure_utc(datetime.fromisoformat(row[4])),
            attempt_started_at=parse_ts(row[5]),
            submitted_at=parse_ts(row[6]),
            confirmed_at=parse_ts(row[7]),
            report_json=row[8],
            report_acked=bool(row[9]),
            observed_message_id=row[10],
            error_code=row[11],
        )

    def intent(self, intent_id: UUID) -> IntentRow | None:
        row = self._db.execute(
            f"select {self._INTENT_COLUMNS} from send_intents where intent_id = ?",  # noqa: S608
            (str(intent_id),),
        ).fetchone()
        return None if row is None else self._intent_from_row(row)

    def intents(self, states: Iterable[IntentState] | None = None) -> list[IntentRow]:
        rows = [
            self._intent_from_row(r)
            for r in self._db.execute(
                f"select {self._INTENT_COLUMNS} from send_intents order by received_at"  # noqa: S608
            )
        ]
        if states is None:
            return rows
        wanted = set(states)
        return [r for r in rows if r.state in wanted]

    def record_intent_received(self, *, intent_id: UUID, inquiry_id: UUID, payload_json: str, now: datetime) -> bool:
        """Durably note an intent before anything else; ``False`` if it was already known."""
        with self._tx() as db:
            cursor = db.execute(
                "insert into send_intents (intent_id, inquiry_id, payload_json, state, received_at)"
                " values (?, ?, ?, 'received', ?) on conflict (intent_id) do nothing",
                (str(intent_id), str(inquiry_id), payload_json, ts(now)),
            )
            return cursor.rowcount == 1

    def begin_attempt(self, intent_id: UUID, now: datetime) -> bool:
        """``received`` -> ``attempting`` (committed before ``.Send``); never a second attempt."""
        with self._tx() as db:
            cursor = db.execute(
                "update send_intents set state = 'attempting', attempt_started_at = ?"
                " where intent_id = ? and state = 'received' and attempt_started_at is null",
                (ts(now), str(intent_id)),
            )
            return cursor.rowcount == 1

    def finish_intent(
        self,
        intent_id: UUID,
        *,
        state: IntentState,
        report_json: str,
        now: datetime,
        error_code: str | None = None,
        observed_message_id: str | None = None,
    ) -> None:
        if state in (IntentState.RECEIVED, IntentState.ATTEMPTING):
            raise LocalStoreError("finish_intent needs a final or evidence state")
        with self._tx() as db:
            db.execute(
                "update send_intents set state = ?, report_json = ?, report_acked = 0,"
                " error_code = coalesce(?, error_code), observed_message_id = coalesce(?, observed_message_id),"
                " submitted_at = case when ? = 'submitted' then ? else submitted_at end,"
                " confirmed_at = case when ? = 'confirmed' then ? else confirmed_at end"
                " where intent_id = ?",
                (
                    state.value,
                    report_json,
                    error_code,
                    observed_message_id,
                    state.value,
                    ts(now),
                    state.value,
                    ts(now),
                    str(intent_id),
                ),
            )

    def mark_report_acked(self, intent_id: UUID, report_json: str) -> None:
        with self._tx() as db:
            db.execute(
                "update send_intents set report_acked = 1 where intent_id = ? and report_json = ?",
                (str(intent_id), report_json),
            )

    def attempted_since(self, since: datetime) -> int:
        row = self._db.execute(
            "select count(*) from send_intents where attempt_started_at is not null and attempt_started_at >= ?",
            (ts(since),),
        ).fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------ gaps

    def open_gap(self, kind: str, started_at: datetime, detail: str | None = None) -> None:
        """Open a gap of ``kind`` unless one is already open."""
        with self._tx() as db:
            row = db.execute("select id from gaps where kind = ? and ended_at is null", (kind,)).fetchone()
            if row is None:
                db.execute(
                    "insert into gaps (kind, started_at, detail) values (?, ?, ?)",
                    (kind, ts(started_at), detail[:120] if detail else None),
                )

    def close_gap(self, kind: str, ended_at: datetime) -> None:
        with self._tx() as db:
            db.execute(
                "update gaps set ended_at = max(started_at, ?), reported = 0 where kind = ? and ended_at is null",
                (ts(ended_at), kind),
            )

    def record_gap(self, kind: str, started_at: datetime, ended_at: datetime, detail: str | None = None) -> None:
        with self._tx() as db:
            db.execute(
                "insert into gaps (kind, started_at, ended_at, detail) values (?, ?, ?, ?)",
                (kind, ts(started_at), ts(max(started_at, ended_at)), detail[:120] if detail else None),
            )

    def gaps(self, *, since: datetime | None = None, kinds: Iterable[str] | None = None) -> list[GapRow]:
        rows = [
            GapRow(
                id=int(r[0]),
                kind=str(r[1]),
                started_at=ensure_utc(datetime.fromisoformat(r[2])),
                ended_at=parse_ts(r[3]),
                detail=r[4],
                reported=bool(r[5]),
            )
            for r in self._db.execute("select id, kind, started_at, ended_at, detail, reported from gaps order by id")
        ]
        if since is not None:
            rows = [g for g in rows if g.ended_at is None or g.ended_at >= since]
        if kinds is not None:
            wanted = set(kinds)
            rows = [g for g in rows if g.kind in wanted]
        return rows

    def mark_gaps_reported(self, ids: Iterable[int]) -> None:
        with self._tx() as db:
            for gap_id in ids:
                db.execute("update gaps set reported = 1 where id = ? and ended_at is not null", (gap_id,))

    # ------------------------------------------------------------------ retention

    def prune(self, *, unrelated_before: datetime, history_before: datetime) -> int:
        """Drop old non-reply bookkeeping (hashed keys only) and old acknowledged uploads."""
        with self._tx() as db:
            removed = db.execute(
                "delete from processed where outcome in ('unrelated', 'non_mail') and"
                " coalesce(received_at, last_seen_at) < ?",
                (ts(unrelated_before),),
            ).rowcount
            db.execute(
                "delete from upload_backlog where state = 'acked' and acked_at < ?", (ts(history_before),)
            )
            db.execute("delete from locator_history where seen_at < ?", (ts(history_before),))
            db.execute("delete from item_failures where last_at < ?", (ts(history_before),))
            db.execute("delete from gaps where ended_at is not null and ended_at < ?", (ts(history_before),))
        return int(removed)


def backoff_delay(attempts: int, *, base: timedelta = timedelta(seconds=30), cap: timedelta = timedelta(hours=1)) -> timedelta:
    """Bounded exponential backoff for transient upload failures."""
    exponent = min(max(attempts, 0), 12)
    return min(cap, base * (2**exponent))


__all__ = [
    "ATTEMPTED_INTENT_STATES",
    "SCHEMA_VERSION",
    "TERMINAL_OUTCOMES",
    "BacklogEntry",
    "BacklogRow",
    "BacklogState",
    "BacklogStats",
    "BindingApplyResult",
    "BindingRecord",
    "FolderCheckpoint",
    "GapRow",
    "IntentRow",
    "IntentState",
    "LocalStore",
    "Outcome",
    "PendingLocator",
    "ProcessedRow",
    "backoff_delay",
    "message_id_hash",
    "parse_ts",
    "sha256_text",
    "ts",
]
