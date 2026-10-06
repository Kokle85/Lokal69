"""Transactional outbox and delivery attempts (spec sections 13, 22, 30, 31 "Outbox" row).

- `enqueue_event` inserts the ``ops.outbox`` row inside the caller's DOMAIN transaction, keyed
  by a unique business dedup key ``(workspace_id, dedup_key)``; a duplicate returns the existing
  event id (``created=False``). Rolling back the domain transaction removes the event too. The
  payload passes the notification payload guard and is stored with its SHA-256 hash; event
  identity (id, type, aggregate, payload, hash, dedup key, ``is_fixture``) is frozen by a trigger.
- Fixtures never become deliverable (spec 18): a fixture event is inserted ``blocked``
  (``FIXTURE_EVENT``, the CHECK constraint forbids any other open state). A payload "looks like
  a fixture" exactly as `domain.notifications.to_mcp_occurrence` decides it: a present, non-false
  ``fixture`` marker, or a ``[SYNTHETIC FIXTURE]`` summary prefix. Such a payload is refused for a
  real event at enqueue time; at claim time any pending row that still looks like a fixture (or
  belongs to a fixture review case) is never leased and is moved to ``blocked`` instead.
- `claim_events` leases due rows with ``FOR UPDATE SKIP LOCKED`` (one fresh token per row,
  ``attempts + 1``). Every lifecycle update (`begin_send`, `record_attempt`, `mark_*`) is fenced
  on id + ``state = 'sending'`` + token + owner + unexpired lease (database time); zero rows
  raises `LeaseLost`.
- Database transactions cannot commit a network request: delivery is at-least-once or
  uncertain. A dispatcher calls `begin_send` (short transaction) immediately before the request.
  A timeout after possible acceptance is recorded with `record_attempt(..., uncertain=True)` and
  `mark_uncertain`: the row leaves the claimable states and stays visible
  (`outbox_stats`, `list_attention`) until `reconcile_uncertain` records provider evidence. It is
  never blindly resent. The reaper (`reap_expired_events`) applies the same rule to a crashed
  dispatcher: a lease that expired after `begin_send` becomes ``uncertain``, otherwise
  ``retry_wait`` (or ``dead_letter`` when attempts are exhausted).
- Separate timestamps: ``event_created_at`` (insert), ``send_attempted_at`` (start of the most
  recent send), ``provider_accepted_at`` (provider receipt) and ``owner_seen_at``, which is only
  set by `mark_owner_seen` from trustworthy channel read evidence, never inferred from delivery.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import OutboxState, Scope
from suv_deals.domain.listings import sha256_json
from suv_deals.domain.notifications import (
    FIXTURE_BLOCKER,
    FIXTURE_SUMMARY_PREFIX,
    MAX_PAYLOAD_BYTES,
    guard_payload,
)
from suv_deals.errors import Forbidden, IdempotencyConflict, NotFound, ValidationFailed, VersionConflict
from suv_deals.observability.logging import redact
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, Database, fetch_all, fetch_one
from suv_deals.persistence.errors_map import LeaseLost, TransientConflict, mapped_errors

Provider = Literal["slack", "mcp_events"]

LEASE_EXPIRED: Final = "LEASE_EXPIRED"
LOST_AFTER_SEND: Final = "DISPATCHER_LOST_AFTER_SEND"
ATTEMPTS_EXHAUSTED: Final = "ATTEMPTS_EXHAUSTED"
_EVENT_TYPE_RE: Final = re.compile(r"^[a-z][a-z0-9_.]{2,79}$")
_AGGREGATE_TYPE_RE: Final = re.compile(r"^[a-z][a-z0-9_]{2,59}$")
_CODE_RE: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_ENQUEUE_RETRIES: Final = 3

OUTBOX_COLUMNS: Final = (
    "id",
    "workspace_id",
    "event_id",
    "event_type",
    "event_version",
    "aggregate_type",
    "aggregate_id",
    "aggregate_version",
    "destination_binding_id",
    "payload",
    "payload_hash",
    "dedup_key",
    "state",
    "attempts",
    "max_attempts",
    "available_at",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "last_heartbeat_at",
    "event_created_at",
    "send_attempted_at",
    "provider_accepted_at",
    "owner_seen_at",
    "last_error_code",
    "blocker_code",
    "is_fixture",
    "created_at",
    "updated_at",
    "completed_at",
)


def _columns(alias: str | None = None) -> sql.Composable:
    if alias is None:
        return sql.SQL(", ").join(sql.Identifier(c) for c in OUTBOX_COLUMNS)
    return sql.SQL(", ").join(sql.Identifier(alias, c) for c in OUTBOX_COLUMNS)


class DeliveryOutcome(StrEnum):
    ACCEPTED = "accepted"  # provider receipt (2xx); not a completed review
    RETRYABLE = "retryable"  # 408/425/429/5xx, connection failure before sending
    TERMINAL = "terminal"  # e.g. 410/413 for MCP Events: terminal for this delivery
    UNCERTAIN = "uncertain"  # timeout after the request may have been accepted


class OutboxRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    event_id: UUID
    event_type: str
    event_version: int
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int | None = None
    destination_binding_id: UUID | None = None
    payload: dict[str, Any]
    payload_hash: str
    dedup_key: str
    state: OutboxState
    attempts: int
    max_attempts: int
    available_at: datetime
    lease_owner: str | None = None
    lease_token: UUID | None = None
    lease_expires_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    event_created_at: datetime
    send_attempted_at: datetime | None = None
    provider_accepted_at: datetime | None = None
    owner_seen_at: datetime | None = None
    last_error_code: str | None = None
    blocker_code: str | None = None
    is_fixture: bool
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @field_validator(
        "available_at",
        "lease_expires_at",
        "last_heartbeat_at",
        "event_created_at",
        "send_attempted_at",
        "provider_accepted_at",
        "owner_seen_at",
        "created_at",
        "updated_at",
        "completed_at",
    )
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


class ClaimedEvent(OutboxRecord):
    """An outbox event leased to one dispatcher; ``attempts`` is this attempt's number."""

    lease_owner: str
    lease_token: UUID
    lease_expires_at: datetime


class OutboxStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    counts: dict[OutboxState, int]
    oldest_due_age: timedelta | None
    uncertain: int
    blocked: int
    dead_letter: int


class EventReapResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    retry: tuple[UUID, ...] = ()
    uncertain: tuple[UUID, ...] = ()
    dead_letter: tuple[UUID, ...] = ()
    exhausted: tuple[UUID, ...] = Field(default=())


# --------------------------------------------------------------------------------------------
# Enqueue (inside the domain transaction)
# --------------------------------------------------------------------------------------------

_WRITE_SCOPES: Final = frozenset(
    {Scope.REVIEWS_WRITE, Scope.RECHECKS_REQUEST, Scope.NOTES_WRITE, Scope.SOURCES_PAUSE, Scope.CONFIG_ADMIN}
)

_INSERT_SQL: Final = (
    "insert into ops.outbox (workspace_id, event_id, event_type, event_version, aggregate_type,"
    " aggregate_id, aggregate_version, destination_binding_id, payload, payload_hash, dedup_key,"
    " state, blocker_code, max_attempts, available_at, is_fixture)"
    " values (%(workspace_id)s, %(event_id)s, %(event_type)s, %(event_version)s, %(aggregate_type)s,"
    " %(aggregate_id)s, %(aggregate_version)s, %(binding)s, %(payload)s, %(payload_hash)s,"
    " %(dedup_key)s, %(state)s, %(blocker_code)s, %(max_attempts)s,"
    " coalesce(%(available_at)s::timestamptz, now()), %(is_fixture)s)"
    " on conflict (workspace_id, dedup_key) do nothing"
    " returning event_id"
)
_FIND_DEDUP_SQL: Final = (
    "select event_id, event_type, aggregate_type, aggregate_id, is_fixture from ops.outbox"
    " where workspace_id = %(workspace_id)s and dedup_key = %(dedup_key)s"
)


def payload_looks_like_fixture(payload: Mapping[str, Any]) -> bool:
    """The fixture test of `domain.notifications.to_mcp_occurrence`, applied before storage.

    Any present marker other than ``None``/``False`` counts (``0``, ``"yes"``...), as does a
    summary starting with ``[SYNTHETIC FIXTURE]`` (case-insensitive, leading whitespace ignored).
    """
    marker = payload.get("fixture")
    if marker is not None and marker is not False:
        return True
    summary = payload.get("summary")
    return isinstance(summary, str) and summary.lstrip().upper().startswith(FIXTURE_SUMMARY_PREFIX)


async def enqueue_event(
    conn: Conn,
    actor: ActorContext,
    *,
    event_type: str,
    event_version: int,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int | None,
    payload: Mapping[str, Any],
    dedup_key: str,
    destination_binding_id: UUID | None = None,
    is_fixture: bool = False,
    available_at: datetime | None = None,
    event_id: UUID | None = None,
    max_attempts: int = 10,
) -> tuple[UUID, bool]:
    """Create the outbox event in the SAME transaction as the domain change.

    Returns ``(event_id, created)``. The same business dedup key returns the existing event id;
    reusing it for a different event type/aggregate raises `IdempotencyConflict`.
    """
    if actor.principal_kind != "system" and not (actor.scopes & _WRITE_SCOPES):
        raise Forbidden("Missing a write scope for creating events")
    if not _EVENT_TYPE_RE.fullmatch(event_type):
        raise ValidationFailed("event_type must be a dotted lower-case name")
    if not _AGGREGATE_TYPE_RE.fullmatch(aggregate_type):
        raise ValidationFailed("aggregate_type must be a lower-case name")
    if isinstance(event_version, bool) or not isinstance(event_version, int) or event_version < 1:
        raise ValidationFailed("event_version is a positive integer major version")
    if aggregate_version is not None and (isinstance(aggregate_version, bool) or aggregate_version < 1):
        raise ValidationFailed("aggregate_version must be positive")
    if not isinstance(dedup_key, str) or not 1 <= len(dedup_key) <= 300:
        raise ValidationFailed("dedup_key must be 1-300 characters")
    if not 1 <= max_attempts <= 50:
        raise ValidationFailed("max_attempts must be between 1 and 50")
    body = dict(payload)
    guard_payload(body, max_bytes=MAX_PAYLOAD_BYTES)
    if payload_looks_like_fixture(body) and not is_fixture:
        raise ValidationFailed("a fixture payload must be stored as a fixture event")
    resolved_id = _resolve_event_id(event_id, body)
    params = {
        "workspace_id": actor.workspace_id,
        "event_id": resolved_id,
        "event_type": event_type,
        "event_version": event_version,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "aggregate_version": aggregate_version,
        "binding": destination_binding_id,
        "payload": Jsonb(body),
        "payload_hash": sha256_json(body),
        "dedup_key": dedup_key,
        "state": OutboxState.BLOCKED.value if is_fixture else OutboxState.PENDING.value,
        "blocker_code": FIXTURE_BLOCKER if is_fixture else None,
        "max_attempts": max_attempts,
        "available_at": None if available_at is None else _aware(available_at),
        "is_fixture": bool(is_fixture),
    }
    async with mapped_errors():
        for _ in range(_ENQUEUE_RETRIES):
            row = await fetch_one(conn, _INSERT_SQL, params)
            if row is not None:
                return row["event_id"], True
            existing = await fetch_one(conn, _FIND_DEDUP_SQL, params)
            if existing is None:
                continue
            if (
                existing["event_type"],
                existing["aggregate_type"],
                existing["aggregate_id"],
                existing["is_fixture"],
            ) != (event_type, aggregate_type, aggregate_id, bool(is_fixture)):
                # Also refuses a real event whose key is held by a (blocked) fixture event: it
                # would otherwise be silently swallowed and never delivered.
                raise IdempotencyConflict("The dedup key is already used by a different event")
            return existing["event_id"], False
    raise TransientConflict("The outbox changed concurrently; retry")


def _resolve_event_id(event_id: UUID | None, payload: Mapping[str, Any]) -> UUID:
    embedded = payload.get("event_id")
    if embedded is None:
        return event_id or uuid4()
    try:
        parsed = UUID(str(embedded))
    except ValueError as exc:
        raise ValidationFailed("payload event_id must be a UUID") from exc
    if event_id is not None and event_id != parsed:
        raise ValidationFailed("payload event_id does not match event_id")
    return parsed


# --------------------------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------------------------

# "Looks like a fixture" in SQL, identical to `payload_looks_like_fixture` (plus the row flag
# and fixture review-case lineage). Shared by the refusal and the claim so a suspicious row can
# never be leased, even if the refusal statement skipped it because another transaction held it.
_SUSPICIOUS: Final = sql.SQL(
    "(o.is_fixture"
    " or (o.payload -> 'fixture' is not null"
    "     and o.payload -> 'fixture' not in ('false'::jsonb, 'null'::jsonb))"
    " or (pg_catalog.jsonb_typeof(o.payload -> 'summary') = 'string'"
    "     and pg_catalog.starts_with(pg_catalog.upper(pg_catalog.regexp_replace("
    "       o.payload ->> 'summary', '^[[:space:]]+', '')), %(fixture_prefix)s))"
    " or (o.aggregate_type = 'review_case' and exists ("
    "       select 1 from app.review_cases c"
    "        where c.workspace_id = o.workspace_id and c.id = o.aggregate_id and c.is_fixture)))"
)

_REFUSE_FIXTURES_SQL: Final = sql.SQL(
    """
with suspicious as (
  select o.id
  from ops.outbox o
  where o.workspace_id = %(workspace_id)s
    and o.state in ('pending', 'retry_wait')
    and o.available_at <= now()
    and {suspicious}
  for update of o skip locked
)
update ops.outbox o
set state = 'blocked', blocker_code = %(blocker)s, last_error_code = %(blocker)s,
    lease_owner = null, lease_token = null, lease_expires_at = null
from suspicious
where o.id = suspicious.id
  and o.workspace_id = %(workspace_id)s
returning o.event_id
"""
).format(suspicious=_SUSPICIOUS)

_CLAIM_SQL: Final = sql.SQL(
    """
with picked as (
  select o.id
  from ops.outbox o
  where o.workspace_id = %(workspace_id)s
    and o.state in ('pending', 'retry_wait')
    and not o.is_fixture
    and o.attempts < o.max_attempts
    and o.available_at <= now()
    and not {suspicious}
  order by o.available_at, o.id
  for update of o skip locked
  limit %(limit)s
)
update ops.outbox o
set state = 'sending',
    lease_owner = %(owner)s,
    lease_token = gen_random_uuid(),
    lease_expires_at = now() + %(lease)s::interval,
    last_heartbeat_at = now(),
    attempts = o.attempts + 1
from picked
where o.id = picked.id
  and o.workspace_id = %(workspace_id)s
returning {columns}
"""
).format(columns=_columns("o"), suspicious=_SUSPICIOUS)

_FENCE: Final = sql.SQL(
    " where workspace_id = %(workspace_id)s and id = %(id)s and state = 'sending'"
    " and lease_token = %(token)s and lease_owner = %(owner)s"
    " and lease_expires_at > clock_timestamp()"
)
_BEGIN_SEND_SQL: Final = sql.SQL(
    "update ops.outbox set send_attempted_at = clock_timestamp(){fence} returning send_attempted_at"
).format(fence=_FENCE)
_LOCK_LEASED_SQL: Final = sql.SQL(
    "select send_attempted_at, last_heartbeat_at from ops.outbox{fence} for update"
).format(fence=_FENCE)
_RECORD_ATTEMPT_SQL: Final = sql.SQL(
    "with started as ("
    " update ops.outbox set send_attempted_at = case"
    "   when send_attempted_at is not null and send_attempted_at >= last_heartbeat_at"
    "     then send_attempted_at"
    # A send happens under THIS lease: never before the claim, never in the future. A backdated
    # sent_at would make the reaper believe nothing was sent and resend blindly.
    "   else least(greatest(coalesce(%(sent_at)s::timestamptz, clock_timestamp()), last_heartbeat_at),"
    "     clock_timestamp()) end"
    "{fence} returning send_attempted_at)"
    " insert into ops.delivery_attempts (workspace_id, outbox_id, attempt_id, attempt_number,"
    " provider, sent_at, completed_at, external_receipt, response_code, error_code, error_detail,"
    " uncertain)"
    " select %(workspace_id)s, %(id)s, %(attempt_id)s,"
    " (select coalesce(max(attempt_number), 0) + 1 from ops.delivery_attempts"
    "   where workspace_id = %(workspace_id)s and outbox_id = %(id)s),"
    " %(provider)s, started.send_attempted_at,"
    " greatest(clock_timestamp(), started.send_attempted_at),"
    " %(receipt)s, %(response_code)s, %(error_code)s, %(error_detail)s, %(uncertain)s"
    " from started"
    " returning id"
).format(fence=_FENCE)
_MARK_RETRY_SQL: Final = sql.SQL(
    "update ops.outbox set"
    " state = case when attempts < max_attempts then 'retry_wait' else 'dead_letter' end,"
    " available_at = case when attempts < max_attempts then greatest(clock_timestamp(),"
    "   coalesce(%(retry_at)s::timestamptz, clock_timestamp() + %(delay)s::interval))"
    "   else available_at end,"
    " completed_at = case when attempts < max_attempts then null else clock_timestamp() end,"
    " lease_owner = null, lease_token = null, lease_expires_at = null,"
    " last_error_code = %(code)s{fence} returning state"
).format(fence=_FENCE)
_DELIVERED: Final = sql.SQL(
    "state = 'delivered',"
    " provider_accepted_at = coalesce(%(accepted)s::timestamptz, clock_timestamp()),"
    " send_attempted_at = coalesce(send_attempted_at,"
    "   coalesce(%(accepted)s::timestamptz, clock_timestamp())),"
    " completed_at = clock_timestamp(), lease_token = null, lease_expires_at = null,"
    " last_error_code = null"
)
_UNCERTAIN: Final = sql.SQL(
    "state = 'uncertain', lease_owner = null, lease_token = null, lease_expires_at = null,"
    " last_error_code = %(code)s"
)
_BLOCKED: Final = sql.SQL(
    "state = 'blocked', blocker_code = %(code)s, lease_owner = null, lease_token = null,"
    " lease_expires_at = null"
)
_DEAD_LETTER: Final = sql.SQL(
    "state = 'dead_letter', completed_at = clock_timestamp(), lease_owner = null,"
    " lease_token = null, lease_expires_at = null, last_error_code = %(code)s"
)
_CANCELLED: Final = sql.SQL(
    "state = 'cancelled', completed_at = clock_timestamp(), lease_owner = null,"
    " lease_token = null, lease_expires_at = null, last_error_code = %(code)s"
)


def _fence(event: ClaimedEvent) -> dict[str, Any]:
    return {
        "workspace_id": event.workspace_id,
        "id": event.id,
        "token": event.lease_token,
        "owner": event.lease_owner,
    }


async def claim_events(
    db: Database, workspace_id: UUID, dispatcher_id: str, lease_seconds: float, limit: int
) -> list[ClaimedEvent]:
    """Lease up to ``limit`` due events (one short transaction, ``SKIP LOCKED``).

    Rows that look like fixtures are refused here and moved to ``blocked`` (``FIXTURE_EVENT``).
    """
    if not isinstance(dispatcher_id, str) or not 1 <= len(dispatcher_id) <= 200:
        raise ValidationFailed("dispatcher_id must be 1-200 characters")
    if isinstance(lease_seconds, bool) or not 0.05 <= float(lease_seconds) <= 3600:
        raise ValidationFailed("lease_seconds must be between 0.05 and 3600")
    if not 1 <= limit <= 100:
        raise ValidationFailed("limit must be between 1 and 100")
    prefix = FIXTURE_SUMMARY_PREFIX.upper()
    async with mapped_errors(), db.transaction(workspace_id=workspace_id) as conn, mapped_errors():
        await conn.execute(
            _REFUSE_FIXTURES_SQL,
            {"workspace_id": workspace_id, "blocker": FIXTURE_BLOCKER, "fixture_prefix": prefix},
        )
        rows = await fetch_all(
            conn,
            _CLAIM_SQL,
            {
                "workspace_id": workspace_id,
                "owner": dispatcher_id,
                "lease": timedelta(seconds=float(lease_seconds)),
                "limit": limit,
                "fixture_prefix": prefix,
            },
        )
    events = [ClaimedEvent.model_validate(r) for r in rows]
    return sorted(events, key=lambda e: (e.available_at, e.id))


async def begin_send(conn: Conn, event: ClaimedEvent) -> datetime:
    """Record that a send is about to start (commit it before the network request).

    If the dispatcher dies afterwards, the reaper marks the event ``uncertain`` instead of
    resending it blindly.
    """
    async with mapped_errors():
        row = await fetch_one(conn, _BEGIN_SEND_SQL, _fence(event))
    if row is None:
        raise LeaseLost()
    return ensure_utc(row["send_attempted_at"])


async def record_attempt(  # noqa: PLR0917 - positional public contract (WP7a API)
    conn: Conn,
    event: ClaimedEvent,
    attempt_id: UUID,
    outcome: DeliveryOutcome,
    receipt: str | None = None,
    response_code: int | None = None,
    error: str | None = None,
    uncertain: bool | None = None,
    *,
    provider: Provider | None = None,
    error_detail: str | None = None,
    sent_at: datetime | None = None,
) -> UUID:
    """Append one ``ops.delivery_attempts`` row for the leased event (fenced).

    ``uncertain`` defaults to ``outcome is UNCERTAIN`` and must agree with it; an uncertain
    attempt carries no receipt. ``attempt_id`` is the stable reference sent to the provider.
    ``provider`` defaults to the provider of the event's destination binding.
    """
    outcome = DeliveryOutcome(outcome)
    is_uncertain = outcome == DeliveryOutcome.UNCERTAIN if uncertain is None else bool(uncertain)
    if is_uncertain != (outcome == DeliveryOutcome.UNCERTAIN):
        raise ValidationFailed("uncertain must match the uncertain outcome")
    if provider is None:
        provider = await _binding_provider(conn, event)
    if provider not in ("slack", "mcp_events"):
        raise ValidationFailed("unknown provider")
    if is_uncertain and receipt is not None:
        raise ValidationFailed("an uncertain attempt has no receipt")
    if receipt is not None and not 1 <= len(receipt) <= 500:
        raise ValidationFailed("receipt must be 1-500 characters")
    if response_code is not None and not 100 <= response_code <= 599:
        raise ValidationFailed("response_code must be an HTTP status")
    if outcome != DeliveryOutcome.ACCEPTED and error is None:
        raise ValidationFailed("a failed or uncertain attempt needs an error code")
    params = {
        **_fence(event),
        "attempt_id": attempt_id,
        "provider": provider,
        "sent_at": None if sent_at is None else _aware(sent_at),
        "receipt": receipt,
        "response_code": response_code,
        "error_code": None if error is None else _code(error),
        "error_detail": _detail(error_detail, 1000),
        "uncertain": is_uncertain,
    }
    async with mapped_errors():
        locked = await fetch_one(conn, _LOCK_LEASED_SQL, params)
        if locked is None:
            raise LeaseLost()
        row = await fetch_one(conn, _RECORD_ATTEMPT_SQL, params)
    if row is None:
        raise LeaseLost()
    result: UUID = row["id"]
    return result


async def _binding_provider(conn: Conn, event: ClaimedEvent) -> Provider:
    if event.destination_binding_id is None:
        raise ValidationFailed("provider is required for an event without a destination binding")
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select provider from app.destination_bindings"
            " where workspace_id = %(workspace_id)s and id = %(id)s",
            {"workspace_id": event.workspace_id, "id": event.destination_binding_id},
        )
    if row is None:
        raise NotFound("Destination binding not found")
    value = row["provider"]
    if value == "slack":
        return "slack"
    if value == "mcp_events":
        return "mcp_events"
    raise ValidationFailed("unknown provider")  # pragma: no cover - CHECK constraint


async def mark_delivered(
    conn: Conn, event: ClaimedEvent, *, provider_accepted_at: datetime | None = None
) -> None:
    """Provider receipt recorded: ``delivered`` (a receipt, never a completed review)."""
    accepted = None if provider_accepted_at is None else _aware(provider_accepted_at)
    await _transition(conn, event, _DELIVERED, {"accepted": accepted})


async def mark_retry(
    conn: Conn,
    event: ClaimedEvent,
    available_at: datetime | timedelta | None,
    error_code: str,
) -> OutboxState:
    """Retry later (``retry_wait``), or ``dead_letter`` once attempts are exhausted."""
    absolute: datetime | None = None
    delay = timedelta(0)
    if isinstance(available_at, timedelta):
        if available_at < timedelta(0):
            raise ValidationFailed("retry delay must not be negative")
        delay = available_at
    elif available_at is not None:
        absolute = _aware(available_at)
    params = {**_fence(event), "retry_at": absolute, "delay": delay, "code": _code(error_code)}
    async with mapped_errors():
        row = await fetch_one(conn, _MARK_RETRY_SQL, params)
    if row is None:
        raise LeaseLost()
    return OutboxState(row["state"])


async def mark_uncertain(conn: Conn, event: ClaimedEvent, error_code: str) -> None:
    """Possible acceptance without proof: keep visible, never resend blindly."""
    await _transition(conn, event, _UNCERTAIN, {"code": _code(error_code)})


async def mark_blocked(conn: Conn, event: ClaimedEvent, blocker_code: str) -> None:
    await _transition(conn, event, _BLOCKED, {"code": _code(blocker_code)})


async def mark_dead_letter(conn: Conn, event: ClaimedEvent, error_code: str) -> None:
    await _transition(conn, event, _DEAD_LETTER, {"code": _code(error_code)})


async def _transition(conn: Conn, event: ClaimedEvent, assignments: sql.SQL, extra: dict[str, Any]) -> None:
    query = sql.SQL("update ops.outbox set {assignments}{fence}").format(
        assignments=assignments, fence=_FENCE
    )
    async with mapped_errors():
        cur = await conn.execute(query, {**_fence(event), **extra})
    if cur.rowcount != 1:
        raise LeaseLost()


# --------------------------------------------------------------------------------------------
# Operator / system actions
# --------------------------------------------------------------------------------------------

_LOCK_EVENT_SQL: Final = sql.SQL(
    "select {columns} from ops.outbox where workspace_id = %(workspace_id)s and event_id = %(event_id)s"
    " for update"
).format(columns=_columns())


def _require_operator(actor: ActorContext) -> None:
    if actor.principal_kind != "system":
        actor.require(Scope.CONFIG_ADMIN)


async def cancel_stale(
    conn: Conn,
    actor: ActorContext,
    event_id: UUID,
    reason: str,
    *,
    lease: ClaimedEvent | None = None,
) -> None:
    """Suppress a stale event (e.g. its case version changed) before it is sent; audited.

    Without ``lease`` only pending/retry_wait/blocked events can be cancelled. An ``uncertain``
    event is never cancelled (its possible delivery must stay visible). With ``lease`` the
    dispatcher cancels the event it is holding (fenced).
    """
    _require_operator(actor)
    code = _code(reason)
    async with mapped_errors():
        if lease is not None:
            if lease.event_id != event_id or lease.workspace_id != actor.workspace_id:
                raise ValidationFailed("lease does not belong to this event")
            await _transition(conn, lease, _CANCELLED, {"code": code})
            prior = OutboxState.SENDING
        else:
            row = await fetch_one(
                conn, _LOCK_EVENT_SQL, {"workspace_id": actor.workspace_id, "event_id": event_id}
            )
            if row is None:
                raise NotFound("Event not found")
            current = OutboxRecord.model_validate(row)
            if current.state not in (OutboxState.PENDING, OutboxState.RETRY_WAIT, OutboxState.BLOCKED):
                raise VersionConflict(
                    "The event can no longer be cancelled", current_state=current.state.value
                )
            await conn.execute(
                "update ops.outbox set state = 'cancelled', completed_at = clock_timestamp(),"
                " last_error_code = %(code)s"
                " where workspace_id = %(workspace_id)s and id = %(id)s",
                {"workspace_id": actor.workspace_id, "id": current.id, "code": code},
            )
            prior = current.state
        await audit.record(
            conn,
            actor,
            "outbox.cancel_stale",
            "outbox_event",
            event_id,
            reason=code,
            metadata={"prior_state": prior.value},
        )


async def reconcile_uncertain(
    conn: Conn,
    actor: ActorContext,
    event_id: UUID,
    *,
    provider: Provider,
    accepted: bool,
    receipt: str | None = None,
    note: str | None = None,
    retry_at: datetime | None = None,
) -> OutboxState:
    """Resolve an ``uncertain`` event from provider evidence (message lookup / idempotency).

    ``accepted=True`` -> ``delivered`` with the receipt; ``accepted=False`` (the provider shows
    no delivery) -> ``retry_wait``. A reconciliation row is appended to ``ops.delivery_attempts``.
    """
    _require_operator(actor)
    if provider not in ("slack", "mcp_events"):
        raise ValidationFailed("unknown provider")
    if receipt is not None and not 1 <= len(receipt) <= 500:
        raise ValidationFailed("receipt must be 1-500 characters")
    async with mapped_errors():
        row = await fetch_one(
            conn, _LOCK_EVENT_SQL, {"workspace_id": actor.workspace_id, "event_id": event_id}
        )
        if row is None:
            raise NotFound("Event not found")
        current = OutboxRecord.model_validate(row)
        if current.state != OutboxState.UNCERTAIN:
            raise VersionConflict(
                "Only uncertain events can be reconciled", current_state=current.state.value
            )
        params = {
            "workspace_id": actor.workspace_id,
            "id": current.id,
            "attempt_id": uuid4(),
            "provider": provider,
            "receipt": receipt if accepted else None,
            "error_code": "RECONCILED_ACCEPTED" if accepted else "RECONCILED_NOT_DELIVERED",
            "error_detail": _detail(note, 1000),
            "retry_at": None if retry_at is None else _aware(retry_at),
        }
        await conn.execute(
            "insert into ops.delivery_attempts (workspace_id, outbox_id, attempt_id, attempt_number,"
            " provider, sent_at, completed_at, external_receipt, error_code, error_detail, uncertain)"
            " select %(workspace_id)s, %(id)s, %(attempt_id)s,"
            " (select coalesce(max(attempt_number), 0) + 1 from ops.delivery_attempts"
            "   where workspace_id = %(workspace_id)s and outbox_id = %(id)s),"
            " %(provider)s, coalesce(o.send_attempted_at, clock_timestamp()), clock_timestamp(),"
            " %(receipt)s, %(error_code)s, %(error_detail)s, false"
            " from ops.outbox o where o.workspace_id = %(workspace_id)s and o.id = %(id)s",
            params,
        )
        if accepted:
            new_state = OutboxState.DELIVERED
            await conn.execute(
                "update ops.outbox set state = 'delivered', provider_accepted_at = clock_timestamp(),"
                " send_attempted_at = coalesce(send_attempted_at, clock_timestamp()),"
                " completed_at = clock_timestamp(), last_error_code = null"
                " where workspace_id = %(workspace_id)s and id = %(id)s and state = 'uncertain'",
                params,
            )
        else:
            new_state = OutboxState.RETRY_WAIT
            await conn.execute(
                "update ops.outbox set state = 'retry_wait',"
                " available_at = greatest(clock_timestamp(), coalesce(%(retry_at)s::timestamptz,"
                "   clock_timestamp())),"
                " last_error_code = 'RECONCILED_NOT_DELIVERED'"
                " where workspace_id = %(workspace_id)s and id = %(id)s and state = 'uncertain'",
                params,
            )
        await audit.record(
            conn,
            actor,
            "outbox.reconcile",
            "outbox_event",
            event_id,
            reason=note,
            metadata={"accepted": accepted, "provider": provider},
        )
    return new_state


async def mark_owner_seen(
    conn: Conn,
    actor: ActorContext,
    event_id: UUID,
    *,
    evidence_source: Literal["provider_read_receipt"],
    seen_at: datetime,
) -> None:
    """Set ``owner_seen_at`` ONLY from trustworthy channel read evidence (system processes)."""
    if actor.principal_kind != "system":
        raise Forbidden("owner_seen_at is set only from verified channel evidence")
    if evidence_source != "provider_read_receipt":
        raise ValidationFailed("unsupported read evidence")
    async with mapped_errors():
        cur = await conn.execute(
            "update ops.outbox set owner_seen_at ="
            " greatest(provider_accepted_at, least(%(seen)s::timestamptz, clock_timestamp()))"
            " where workspace_id = %(workspace_id)s and event_id = %(event_id)s"
            " and state = 'delivered' and provider_accepted_at is not null and owner_seen_at is null",
            {"workspace_id": actor.workspace_id, "event_id": event_id, "seen": _aware(seen_at)},
        )
    if cur.rowcount != 1:
        raise VersionConflict("Read evidence applies only to a delivered, not yet seen event")


# --------------------------------------------------------------------------------------------
# Reaper, stats, reads
# --------------------------------------------------------------------------------------------

_REAP_SQL: Final = """
with expired as (
  select id
  from ops.outbox
  where workspace_id = %(workspace_id)s
    and state = 'sending'
    and lease_expires_at <= clock_timestamp()
  order by lease_expires_at, id
  for update skip locked
  limit %(limit)s
)
update ops.outbox o
set state = case
      when o.send_attempted_at is not null and o.send_attempted_at >= o.last_heartbeat_at then 'uncertain'
      when o.attempts < o.max_attempts then 'retry_wait'
      else 'dead_letter' end,
    completed_at = case
      when o.send_attempted_at is not null and o.send_attempted_at >= o.last_heartbeat_at then null
      when o.attempts < o.max_attempts then null
      else clock_timestamp() end,
    available_at = case
      when o.send_attempted_at is not null and o.send_attempted_at >= o.last_heartbeat_at then o.available_at
      when o.attempts < o.max_attempts then clock_timestamp() + %(delay)s::interval
      else o.available_at end,
    last_error_code = case
      when o.send_attempted_at is not null and o.send_attempted_at >= o.last_heartbeat_at
        then 'DISPATCHER_LOST_AFTER_SEND'
      else 'LEASE_EXPIRED' end,
    lease_owner = null,
    lease_token = null,
    lease_expires_at = null
from expired
where o.id = expired.id
  and o.workspace_id = %(workspace_id)s
returning o.event_id, o.state
"""

_EXHAUSTED_SQL: Final = """
with exhausted as (
  select id from ops.outbox
  where workspace_id = %(workspace_id)s and state in ('pending', 'retry_wait')
    and attempts >= max_attempts
  order by id
  for update skip locked
  limit %(limit)s
)
update ops.outbox o
set state = 'dead_letter', completed_at = clock_timestamp(), last_error_code = 'ATTEMPTS_EXHAUSTED'
from exhausted
where o.id = exhausted.id and o.workspace_id = %(workspace_id)s
returning o.event_id
"""


async def reap_expired_events(
    db: Database, workspace_id: UUID, *, retry_delay_seconds: int = 30, limit: int = 500
) -> EventReapResult:
    """Recover expired dispatcher leases; exhausted waiting events become dead letters."""
    if not 0 <= retry_delay_seconds <= 86_400 or not 1 <= limit <= 10_000:
        raise ValidationFailed("invalid reaper parameters")
    params = {
        "workspace_id": workspace_id,
        "delay": timedelta(seconds=retry_delay_seconds),
        "limit": limit,
    }
    async with mapped_errors(), db.transaction(workspace_id=workspace_id) as conn, mapped_errors():
        rows = await fetch_all(conn, _REAP_SQL, params)
        exhausted = await fetch_all(conn, _EXHAUSTED_SQL, params)
    buckets: dict[str, list[UUID]] = {"retry_wait": [], "uncertain": [], "dead_letter": []}
    for row in rows:
        buckets[row["state"]].append(row["event_id"])
    return EventReapResult(
        retry=tuple(buckets["retry_wait"]),
        uncertain=tuple(buckets["uncertain"]),
        dead_letter=tuple(buckets["dead_letter"]),
        exhausted=tuple(r["event_id"] for r in exhausted),
    )


async def get_event(conn: Conn, actor: ActorContext, event_id: UUID) -> OutboxRecord:
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            sql.SQL(
                "select {columns} from ops.outbox"
                " where workspace_id = %(workspace_id)s and event_id = %(event_id)s"
            ).format(columns=_columns()),
            {"workspace_id": actor.workspace_id, "event_id": event_id},
        )
    if row is None:
        raise NotFound("Event not found")
    return OutboxRecord.model_validate(row)


async def list_attention(conn: Conn, actor: ActorContext, *, limit: int = 50) -> list[OutboxRecord]:
    """Uncertain, blocked and dead-letter events (a failed alert is never silently discarded)."""
    actor.require(Scope.DEALS_READ)
    if not 1 <= limit <= 500:
        raise ValidationFailed("limit must be between 1 and 500")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            sql.SQL(
                "select {columns} from ops.outbox where workspace_id = %(workspace_id)s"
                " and state in ('uncertain', 'blocked', 'dead_letter')"
                " order by state, event_created_at, id limit %(limit)s"
            ).format(columns=_columns()),
            {"workspace_id": actor.workspace_id, "limit": limit},
        )
    return [OutboxRecord.model_validate(r) for r in rows]


async def outbox_stats(conn: Conn, actor: ActorContext) -> OutboxStats:
    """Pending/sending/retry/uncertain/blocked/dead-letter counts and the oldest due age."""
    actor.require(Scope.DEALS_READ)
    ws = {"workspace_id": actor.workspace_id}
    async with mapped_errors():
        now_row = await fetch_one(conn, "select clock_timestamp() as now")
        count_rows = await fetch_all(
            conn,
            "select state, count(*) as n from ops.outbox where workspace_id = %(workspace_id)s"
            " and state in ('pending', 'sending', 'retry_wait', 'uncertain', 'blocked', 'dead_letter')"
            " group by state",
            ws,
        )
        age_row = await fetch_one(
            conn,
            "select clock_timestamp() - min(available_at) as age from ops.outbox"
            " where workspace_id = %(workspace_id)s and state in ('pending', 'retry_wait')"
            " and available_at <= clock_timestamp()",
            ws,
        )
    assert now_row is not None and age_row is not None
    counts = {OutboxState(r["state"]): int(r["n"]) for r in count_rows}
    age = age_row["age"]
    return OutboxStats(
        as_of=ensure_utc(now_row["now"]),
        counts=counts,
        oldest_due_age=None if age is None else max(age, timedelta(0)),
        uncertain=counts.get(OutboxState.UNCERTAIN, 0),
        blocked=counts.get(OutboxState.BLOCKED, 0),
        dead_letter=counts.get(OutboxState.DEAD_LETTER, 0),
    )


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _aware(value: datetime) -> datetime:
    try:
        return ensure_utc(value)
    except ValueError as exc:
        raise ValidationFailed("timestamps must be timezone-aware") from exc


def _code(code: str) -> str:
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
        raise ValidationFailed("codes are 1-80 characters of letters, digits and _ . : -")
    return code


def _detail(detail: str | None, limit: int) -> str | None:
    if detail is None:
        return None
    cleaned = redact(str(detail)).strip()
    return cleaned[:limit] or None
