"""Durable PostgreSQL job queue with leases and fencing (spec sections 9, 13, 30, 31).

State machine (spec 13)::

    queued -> running -> succeeded
                      -> retry_wait -> running
                      -> blocked
                      -> dead_letter
    queued/retry_wait -> cancelled
    running with expired lease -> retry_wait (attempts remain) or dead_letter (reaper)

Contracts (docs/schema.md section 7):

- `claim` is the spec section 13 statement (``FOR UPDATE SKIP LOCKED LIMIT 1``, ``attempts + 1``,
  fresh UUID token, ``priority desc, available_at, id``, ``attempts < max_attempts``) plus an
  explicit workspace predicate and a job-type filter, always with bound parameters, in its own
  short transaction. Two concurrent workers never receive the same job.
- Every lease-holder update (`heartbeat`, `complete`, `fail_retry`, `release`, `fail_blocked`,
  `dead_letter`) is fenced: ``id``, ``state = 'running'``, current ``lease_token``, current
  ``lease_owner`` and ``lease_expires_at > clock_timestamp()`` (database time, not the
  transaction start). Zero rows means the lease was lost: `LeaseLost` is raised and the caller
  must roll back the whole transaction (see `transactions.py` for the unit-of-work pattern).
  A late worker therefore cannot overwrite a result committed by a newer lease holder.
- `release` returns a claimed job to ``queued`` WITHOUT consuming an attempt (the claim's
  ``attempts + 1`` is undone): for work a budget refused before anything was fetched, so budget
  refusals never exhaust a job into a dead letter. The code is recorded and logged (audited when
  an actor is given).
- The reaper (`reap_expired`) moves expired leases to ``retry_wait`` while attempts remain,
  otherwise to ``dead_letter``; it clears the lease fields, records ``LEASE_EXPIRED`` with the
  previous holder, and never touches ``result_reference``. The one exception is
  ``seller_inquiry_send`` (spec 37.5): its worker may have handed the email to a provider before
  it died, so an expired send job moves to ``blocked`` with ``blocker_code
  EMAIL_DELIVERY_UNCERTAIN`` and is NEVER requeued or dead-lettered by the reaper (the inquiry's
  own attempt is made ``uncertain`` by ``inquiries_repo.reap_expired_attempts`` and only positive
  reconciliation evidence decides it). `reconcile_exhausted` dead-letters
  waiting jobs whose attempts are used up so they never stay invisible.
- Enqueue is idempotent on the open (non-terminal) dedup key (`jobs_dedup_open_uidx`); slot
  jobs are unique per ``(workspace, source, profile, partition, scheduled_slot)`` forever
  (`jobs_scheduler_slot_uidx`, spec 9), so racing schedulers create exactly one job.
- Payload versions: a worker declares the payload versions it understands per job type; a
  claimed job with any other version is moved to ``blocked`` with the typed blocker
  ``incompatible_payload_version`` (never an endless retry timer) and the claim moves on.

Scopes: recheck jobs need ``rechecks:request``; every other job type is system work (a
``system`` principal or ``config:admin``). Reads (`get_job`, `queue_stats`) need ``deals:read``.
No function performs network I/O; all timestamps that matter for leases come from the database.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import JobState, JobType, Scope
from suv_deals.errors import Forbidden, IdempotencyConflict, NotFound, ValidationFailed, VersionConflict
from suv_deals.observability.logging import redact
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, Database, fetch_all, fetch_one
from suv_deals.persistence.errors_map import LeaseLost, TransientConflict, mapped_errors

logger = logging.getLogger(__name__)

INCOMPATIBLE_PAYLOAD_VERSION: Final = "incompatible_payload_version"
LEASE_EXPIRED: Final = "LEASE_EXPIRED"
ATTEMPTS_EXHAUSTED: Final = "ATTEMPTS_EXHAUSTED"
CANCELLED: Final = "CANCELLED"

OPEN_STATES: Final = (JobState.QUEUED, JobState.RUNNING, JobState.RETRY_WAIT, JobState.BLOCKED)
WAITING_STATES: Final = (JobState.QUEUED, JobState.RETRY_WAIT)
MAX_JOB_PAYLOAD_BYTES: Final = 64 * 1024
MAX_RESULT_BYTES: Final = 16 * 1024
MIN_LEASE_SECONDS: Final = 0.05
MAX_LEASE_SECONDS: Final = 3600.0
DEFAULT_SLOT_INTERVAL: Final = timedelta(minutes=15)

_CODE_RE: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_PARTITION_RE: Final = re.compile(r"^[A-Za-z0-9_:.-]{1,80}$")
_ENQUEUE_RETRIES: Final = 3
_MAX_INCOMPATIBLE_PER_CLAIM: Final = 20

# Fixed allow-list of ops.jobs columns read by this module (never caller-controlled).
JOB_COLUMNS: Final = (
    "id",
    "workspace_id",
    "job_type",
    "dedup_key",
    "payload_version",
    "payload",
    "priority",
    "available_at",
    "attempts",
    "max_attempts",
    "state",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "last_heartbeat_at",
    "last_error_code",
    "last_error_detail",
    "blocker_code",
    "blocker_detail",
    "source_id",
    "profile_id",
    "partition_key",
    "scheduled_slot",
    "listing_id",
    "generation",
    "result_reference",
    "created_at",
    "updated_at",
    "completed_at",
)


def job_columns(alias: str | None = None) -> sql.Composable:
    if alias is None:
        return sql.SQL(", ").join(sql.Identifier(c) for c in JOB_COLUMNS)
    return sql.SQL(", ").join(sql.Identifier(alias, c) for c in JOB_COLUMNS)


# --------------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------------


class JobSpec(BaseModel):
    """A job to enqueue. ``available_at=None`` means "now" in database time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_type: JobType
    dedup_key: str = Field(min_length=1, max_length=300)
    payload_version: int = Field(default=1, ge=1, le=10_000)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-1000, le=1000)
    available_at: datetime | None = None
    max_attempts: int = Field(default=5, ge=1, le=50)
    partition_key: str | None = None
    source_id: UUID | None = None
    profile_id: UUID | None = None
    listing_id: UUID | None = None
    generation: int | None = Field(default=None, ge=1)

    @field_validator("available_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_validator("partition_key")
    @classmethod
    def _partition(cls, value: str | None) -> str | None:
        if value is not None and not _PARTITION_RE.fullmatch(value):
            raise ValueError("partition_key must be 1-80 characters of letters, digits and _ : . -")
        return value

    @field_validator("payload")
    @classmethod
    def _payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        _json_size(value, MAX_JOB_PAYLOAD_BYTES, "job payload")
        return value

    @model_validator(mode="after")
    def _binding(self) -> JobSpec:
        if self.job_type in (JobType.DETAIL, JobType.RECHECK) and (
            self.listing_id is None or self.generation is None
        ):
            raise ValueError("detail and recheck jobs need listing_id and generation")
        return self


class JobRecord(BaseModel):
    """One ``ops.jobs`` row as read by this module."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    job_type: JobType
    dedup_key: str
    payload_version: int
    payload: dict[str, Any]
    priority: int
    available_at: datetime
    attempts: int
    max_attempts: int
    state: JobState
    lease_owner: str | None = None
    lease_token: UUID | None = None
    lease_expires_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_error_code: str | None = None
    last_error_detail: str | None = None
    blocker_code: str | None = None
    blocker_detail: str | None = None
    source_id: UUID | None = None
    profile_id: UUID | None = None
    partition_key: str | None = None
    scheduled_slot: datetime | None = None
    listing_id: UUID | None = None
    generation: int | None = None
    result_reference: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @field_validator(
        "available_at",
        "lease_expires_at",
        "last_heartbeat_at",
        "scheduled_slot",
        "created_at",
        "updated_at",
        "completed_at",
    )
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


class ClaimedJob(JobRecord):
    """A job leased to one worker. The (id, lease_token, lease_owner) triple is the fence."""

    lease_owner: str
    lease_token: UUID
    lease_expires_at: datetime


class ReapResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requeued: tuple[UUID, ...] = ()
    dead_lettered: tuple[UUID, ...] = ()
    # Expired ``seller_inquiry_send`` jobs: ``blocked`` / EMAIL_DELIVERY_UNCERTAIN, never requeued.
    blocked_uncertain: tuple[UUID, ...] = ()
    # Every expired lease per job type (all three outcomes): `record_lease_expiration`.
    expired_by_type: dict[JobType, int] = Field(default_factory=dict)
    # The dead-lettered subset per job type: `record_dead_letter`.
    dead_lettered_by_type: dict[JobType, int] = Field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.requeued) + len(self.dead_lettered) + len(self.blocked_uncertain)


class QueueStats(BaseModel):
    """Queue metrics for one workspace (spec 30: depth, oldest age, lease expirations, dead letters)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    depth: dict[tuple[JobType, JobState], int]
    oldest_due_age: dict[JobType, timedelta]
    dead_letters: int
    blocked: int
    expired_leases: int
    exhausted_waiting: int


# --------------------------------------------------------------------------------------------
# Enqueue
# --------------------------------------------------------------------------------------------

_INSERT_COLUMNS: Final = sql.SQL(
    "workspace_id, job_type, dedup_key, payload_version, payload, priority, available_at,"
    " max_attempts, source_id, profile_id, partition_key, listing_id, generation, scheduled_slot"
)
_INSERT_VALUES: Final = sql.SQL(
    "%(workspace_id)s, %(job_type)s, %(dedup_key)s, %(payload_version)s, %(payload)s, %(priority)s,"
    " coalesce(%(available_at)s::timestamptz, now()), %(max_attempts)s, %(source_id)s, %(profile_id)s,"
    " %(partition_key)s, %(listing_id)s, %(generation)s, %(scheduled_slot)s::timestamptz"
)

_ENQUEUE_SQL: Final = sql.SQL(
    "insert into ops.jobs ({columns}) values ({values})"
    " on conflict (workspace_id, dedup_key)"
    " where state in ('queued', 'running', 'retry_wait', 'blocked') do nothing"
    " returning id"
).format(columns=_INSERT_COLUMNS, values=_INSERT_VALUES)

# Spec 9: the slot key admits one job per (workspace, source, profile, partition, slot) for
# all states; a conflict on the open dedup key is equally "already scheduled".
_ENQUEUE_SLOT_SQL: Final = sql.SQL(
    "insert into ops.jobs ({columns}) values ({values}) on conflict do nothing returning id"
).format(columns=_INSERT_COLUMNS, values=_INSERT_VALUES)

_FIND_OPEN_SQL: Final = (
    "select id, job_type from ops.jobs"
    " where workspace_id = %(workspace_id)s and dedup_key = %(dedup_key)s"
    " and state in ('queued', 'running', 'retry_wait', 'blocked')"
)
_FIND_SLOT_SQL: Final = (
    "select id, job_type from ops.jobs"
    " where workspace_id = %(workspace_id)s and source_id = %(source_id)s"
    " and profile_id = %(profile_id)s and partition_key = %(partition_key)s"
    " and scheduled_slot = %(scheduled_slot)s"
)


def _require_enqueue(actor: ActorContext, job_type: JobType) -> None:
    if job_type == JobType.RECHECK:
        actor.require(Scope.RECHECKS_REQUEST)
        return
    if actor.principal_kind != "system" and not actor.has(Scope.CONFIG_ADMIN):
        raise Forbidden("Only system workers or an owner may enqueue this job type")


def _same_type(existing: Mapping[str, Any], spec: JobSpec) -> None:
    if existing["job_type"] != spec.job_type.value:
        raise IdempotencyConflict("The dedup key is already used by a job of another type")


def _spec_params(actor: ActorContext, spec: JobSpec, scheduled_slot: datetime | None) -> dict[str, Any]:
    return {
        "workspace_id": actor.workspace_id,
        "job_type": spec.job_type.value,
        "dedup_key": spec.dedup_key,
        "payload_version": spec.payload_version,
        "payload": Jsonb(spec.payload),
        "priority": spec.priority,
        "available_at": spec.available_at,
        "max_attempts": spec.max_attempts,
        "source_id": spec.source_id,
        "profile_id": spec.profile_id,
        "partition_key": spec.partition_key,
        "listing_id": spec.listing_id,
        "generation": spec.generation,
        "scheduled_slot": scheduled_slot,
    }


async def enqueue(conn: Conn, actor: ActorContext, spec: JobSpec) -> tuple[UUID, bool]:
    """Insert a job unless an open job with the same dedup key exists.

    Returns ``(job_id, created)``; an existing queued/running/retry_wait/blocked job is returned
    with ``created=False``. Terminal jobs (succeeded, dead_letter, cancelled) do not block a new
    job. Runs inside the caller's transaction (same transaction as the domain change).
    """
    _require_enqueue(actor, spec.job_type)
    params = _spec_params(actor, spec, None)
    async with mapped_errors():
        for _ in range(_ENQUEUE_RETRIES):
            row = await fetch_one(conn, _ENQUEUE_SQL, params)
            if row is not None:
                return row["id"], True
            existing = await fetch_one(conn, _FIND_OPEN_SQL, params)
            if existing is not None:
                _same_type(existing, spec)
                return existing["id"], False
    raise TransientConflict("The job changed state concurrently; retry")


async def enqueue_slot(
    conn: Conn, actor: ActorContext, spec: JobSpec, scheduled_slot: datetime
) -> tuple[UUID, bool]:
    """Insert the job for one scheduler slot (spec 9) exactly once, whatever its later state.

    ``spec`` must name the source, profile and partition; ``scheduled_slot`` should come from
    `current_slot` (database time). Duplicate schedulers racing for the slot get the existing
    job id with ``created=False``. Advance ``ops.source_schedules`` in the same transaction.
    """
    _require_enqueue(actor, spec.job_type)
    if spec.source_id is None or spec.profile_id is None or spec.partition_key is None:
        raise ValidationFailed("slot jobs need source_id, profile_id and partition_key")
    slot = _aware(scheduled_slot)
    params = _spec_params(actor, spec, slot)
    async with mapped_errors():
        for _ in range(_ENQUEUE_RETRIES):
            row = await fetch_one(conn, _ENQUEUE_SLOT_SQL, params)
            if row is not None:
                return row["id"], True
            existing = await fetch_one(conn, _FIND_SLOT_SQL, params)
            if existing is None:
                existing = await fetch_one(conn, _FIND_OPEN_SQL, params)
            if existing is not None:
                _same_type(existing, spec)
                return existing["id"], False
    raise TransientConflict("The scheduler slot changed concurrently; retry")


async def current_slot(conn: Conn, interval: timedelta = DEFAULT_SLOT_INTERVAL) -> datetime:
    """The current scheduler slot in database time (``date_bin`` on a fixed UTC origin)."""
    if not timedelta(minutes=1) <= interval <= timedelta(days=1):
        raise ValidationFailed("slot interval must be between one minute and one day")
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select date_bin(%s::interval, now(), timestamptz '2000-01-01T00:00:00Z') as slot",
            (interval,),
        )
    assert row is not None
    return ensure_utc(row["slot"])


# --------------------------------------------------------------------------------------------
# Claim / heartbeat
# --------------------------------------------------------------------------------------------

_CLAIM_SQL: Final = sql.SQL(
    """
with picked as (
  select id
  from ops.jobs
  where workspace_id = %(workspace_id)s
    and state in ('queued', 'retry_wait')
    and job_type = any(%(job_types)s::text[])
    and attempts < max_attempts
    and available_at <= now()
  order by priority desc, available_at, id
  for update skip locked
  limit 1
)
update ops.jobs j
set state = 'running',
    lease_owner = %(worker_id)s,
    lease_token = %(fresh_uuid)s,
    lease_expires_at = now() + %(lease_duration)s::interval,
    last_heartbeat_at = now(),
    attempts = attempts + 1
from picked
where j.id = picked.id
  and j.workspace_id = %(workspace_id)s
returning {columns}
"""
).format(columns=job_columns("j"))

_BLOCK_INCOMPATIBLE_SQL: Final = (
    "update ops.jobs"
    " set state = 'blocked', blocker_code = %(code)s, blocker_detail = %(detail)s,"
    " lease_owner = null, lease_token = null, lease_expires_at = null,"
    " attempts = greatest(attempts - 1, 0)"
    " where workspace_id = %(workspace_id)s and id = %(id)s"
    " and state = 'running' and lease_token = %(token)s"
)

_FENCE: Final = sql.SQL(
    " where workspace_id = %(workspace_id)s and id = %(id)s and state = 'running'"
    " and lease_token = %(token)s and lease_owner = %(owner)s"
    " and lease_expires_at > clock_timestamp()"
)

_HEARTBEAT_SQL: Final = sql.SQL(
    "update ops.jobs set lease_expires_at = clock_timestamp() + %(lease)s::interval,"
    " last_heartbeat_at = clock_timestamp(){fence}"
).format(fence=_FENCE)


def _lease_duration(lease_seconds: float) -> timedelta:
    if isinstance(lease_seconds, bool) or not MIN_LEASE_SECONDS <= float(lease_seconds) <= MAX_LEASE_SECONDS:
        raise ValidationFailed("lease_seconds must be between 0.05 and 3600")
    return timedelta(seconds=float(lease_seconds))


def _worker(worker_id: str) -> str:
    if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 200 or not worker_id.isprintable():
        raise ValidationFailed("worker_id must be 1-200 printable characters")
    return worker_id


def _fence(job: ClaimedJob) -> dict[str, Any]:
    return {
        "workspace_id": job.workspace_id,
        "id": job.id,
        "token": job.lease_token,
        "owner": job.lease_owner,
    }


async def claim(
    db: Database,
    workspace_id: UUID,
    worker_id: str,
    job_types: Collection[JobType],
    lease_seconds: float,
    *,
    payload_versions: Mapping[JobType, Collection[int]] | None = None,
) -> ClaimedJob | None:
    """Lease the next due job of ``job_types`` in one workspace (spec 13 claim, one short transaction).

    ``payload_versions`` declares the payload versions this worker understands per job type
    (every requested type must be listed when it is given). A claimed job with another
    version is blocked with ``incompatible_payload_version`` and the next job is tried.
    """
    worker = _worker(worker_id)
    lease = _lease_duration(lease_seconds)
    types = sorted({JobType(t).value for t in job_types})
    if not types:
        raise ValidationFailed("at least one job type is required")
    if payload_versions is not None:
        missing = [t for t in types if JobType(t) not in payload_versions]
        if missing:
            raise ValidationFailed("payload_versions must list every claimed job type")
    async with mapped_errors(), db.transaction(workspace_id=workspace_id) as conn, mapped_errors():
        for _ in range(_MAX_INCOMPATIBLE_PER_CLAIM):
            row = await fetch_one(
                conn,
                _CLAIM_SQL,
                {
                    "workspace_id": workspace_id,
                    "job_types": types,
                    "worker_id": worker,
                    "fresh_uuid": uuid4(),
                    "lease_duration": lease,
                },
            )
            if row is None:
                return None
            job = ClaimedJob.model_validate(row)
            if payload_versions is None or job.payload_version in payload_versions[job.job_type]:
                return job
            await conn.execute(
                _BLOCK_INCOMPATIBLE_SQL,
                {
                    "workspace_id": workspace_id,
                    "id": job.id,
                    "token": job.lease_token,
                    "code": INCOMPATIBLE_PAYLOAD_VERSION,
                    "detail": (
                        f"payload_version {job.payload_version} is not understood by worker"
                        f" {worker[:80]} (supports {sorted(payload_versions[job.job_type])})"
                    )[:2000],
                },
            )
    return None


async def heartbeat(db: Database, job: ClaimedJob, lease_seconds: float) -> bool:
    """Extend the caller's own unexpired lease. ``False`` means the lease is lost: stop working."""
    lease = _lease_duration(lease_seconds)
    async with mapped_errors(), db.transaction(workspace_id=job.workspace_id) as conn, mapped_errors():
        cur = await conn.execute(_HEARTBEAT_SQL, {**_fence(job), "lease": lease})
        return cur.rowcount == 1


# --------------------------------------------------------------------------------------------
# Lease-holder outcomes (fenced; run inside the post-fetch unit of work)
# --------------------------------------------------------------------------------------------

_COMPLETE_SQL: Final = sql.SQL(
    "update ops.jobs set state = 'succeeded', completed_at = clock_timestamp(),"
    " result_reference = %(result)s{fence}"
).format(fence=_FENCE)
_FAIL_RETRY_SQL: Final = sql.SQL(
    "update ops.jobs set"
    " state = case when attempts < max_attempts then 'retry_wait' else 'dead_letter' end,"
    " available_at = case when attempts < max_attempts"
    "   then greatest(clock_timestamp(),"
    "     coalesce(%(retry_at)s::timestamptz, clock_timestamp() + %(delay)s::interval))"
    "   else available_at end,"
    " completed_at = case when attempts < max_attempts then null else clock_timestamp() end,"
    " lease_owner = null, lease_token = null, lease_expires_at = null,"
    " last_error_code = %(code)s, last_error_detail = %(detail)s{fence} returning state"
).format(fence=_FENCE)
_FAIL_BLOCKED_SQL: Final = sql.SQL(
    "update ops.jobs set state = 'blocked', blocker_code = %(code)s, blocker_detail = %(detail)s,"
    " lease_owner = null, lease_token = null, lease_expires_at = null{fence}"
).format(fence=_FENCE)
_DEAD_LETTER_SQL: Final = sql.SQL(
    "update ops.jobs set state = 'dead_letter', completed_at = clock_timestamp(),"
    " lease_owner = null, lease_token = null, lease_expires_at = null,"
    " last_error_code = %(code)s, last_error_detail = %(detail)s{fence}"
).format(fence=_FENCE)


async def complete(conn: Conn, job: ClaimedJob, result_reference: Mapping[str, Any] | None) -> None:
    """Guarded completion: the last statement of the unit of work before COMMIT.

    Raises `LeaseLost` when the lease is no longer held (expired, reaped, re-claimed); the caller
    must then roll back the entire transaction, including its domain writes.
    """
    result = None if result_reference is None else Jsonb(_json_object(result_reference, "result_reference"))
    async with mapped_errors():
        cur = await conn.execute(_COMPLETE_SQL, {**_fence(job), "result": result})
    if cur.rowcount != 1:
        raise LeaseLost()


async def fail_retry(
    conn: Conn,
    job: ClaimedJob,
    error_code: str,
    retry_at: datetime | timedelta | None = None,
    *,
    detail: str | None = None,
) -> JobState:
    """Release the lease for a later retry; dead-letters instead when attempts are exhausted.

    ``retry_at`` may be an absolute instant (e.g. a `RetryPlan.retry_at`), a delay from database
    time, or ``None`` (due immediately). It is never earlier than the current database time.
    Returns the new state (``retry_wait`` or ``dead_letter``).
    """
    absolute: datetime | None = None
    delay = timedelta(0)
    if isinstance(retry_at, timedelta):
        if retry_at < timedelta(0):
            raise ValidationFailed("retry delay must not be negative")
        delay = retry_at
    elif retry_at is not None:
        absolute = _aware(retry_at)
    params = {
        **_fence(job),
        "code": _code(error_code),
        "detail": _detail(detail),
        "retry_at": absolute,
        "delay": delay,
    }
    async with mapped_errors():
        row = await fetch_one(conn, _FAIL_RETRY_SQL, params)
    if row is None:
        raise LeaseLost()
    return JobState(row["state"])


_RELEASE_SQL: Final = sql.SQL(
    "update ops.jobs set state = 'queued', attempts = greatest(attempts - 1, 0),"
    " available_at = greatest(clock_timestamp(),"
    "   coalesce(%(available_at)s::timestamptz, clock_timestamp() + %(delay)s::interval)),"
    " lease_owner = null, lease_token = null, lease_expires_at = null,"
    " last_error_code = %(code)s, last_error_detail = %(detail)s{fence}"
    " returning attempts, available_at"
).format(fence=_FENCE)


async def release(
    conn: Conn,
    job: ClaimedJob,
    *,
    available_at: datetime | timedelta | None,
    code: str,
    detail: str | None = None,
    actor: ActorContext | None = None,
) -> JobRecord:
    """Return a claimed job to ``queued`` WITHOUT consuming an attempt (fenced by the lease).

    For work that never started because a budget refused it until a known time (host spacing,
    Retry-After, daily budget, circuit; `crawling.detail.apply_budget_refusal` decides): the
    claim's ``attempts + 1`` is undone, so budget pressure can never exhaust a job into a dead
    letter. ``available_at`` is an absolute instant, a delay
    from database time, or ``None`` (due now); it is never earlier than the current database
    time. ``code`` is recorded as ``last_error_code`` (and in the log); with ``actor`` the release
    is also audited (``job.release``). Raises `LeaseLost` when the lease is no longer held.
    """
    absolute: datetime | None = None
    delay = timedelta(0)
    if isinstance(available_at, timedelta):
        if available_at < timedelta(0):
            raise ValidationFailed("release delay must not be negative")
        delay = available_at
    elif available_at is not None:
        absolute = _aware(available_at)
    if actor is not None and actor.workspace_id != job.workspace_id:
        raise LeaseLost()
    params = {
        **_fence(job),
        "code": _code(code),
        "detail": _detail(detail),
        "available_at": absolute,
        "delay": delay,
    }
    async with mapped_errors():
        row = await fetch_one(conn, _RELEASE_SQL, params)
        if row is None:
            raise LeaseLost()
        released_at = ensure_utc(row["available_at"])
        if actor is not None:
            await audit.record(
                conn,
                actor,
                "job.release",
                "job",
                job.id,
                reason="released without consuming an attempt (nothing was fetched)",
                metadata={
                    "job_type": job.job_type.value,
                    "code": params["code"],
                    "attempts": int(row["attempts"]),
                    "available_at": released_at.isoformat(),
                },
            )
    logger.info(
        "job released without consuming an attempt",
        extra={"job_id": str(job.id), "job_type": job.job_type.value, "code": params["code"]},
    )
    return JobRecord.model_validate(
        {
            **job.model_dump(),
            "state": JobState.QUEUED,
            "attempts": int(row["attempts"]),
            "available_at": released_at,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "last_error_code": params["code"],
            "last_error_detail": params["detail"],
        }
    )


async def fail_blocked(conn: Conn, job: ClaimedJob, blocker_code: str, detail: str | None = None) -> None:
    """Typed blocker (e.g. ``source_paused``, ``incompatible_payload_version``): no retry timer."""
    params = {**_fence(job), "code": _code(blocker_code), "detail": _detail(detail)}
    async with mapped_errors():
        cur = await conn.execute(_FAIL_BLOCKED_SQL, params)
    if cur.rowcount != 1:
        raise LeaseLost()


async def dead_letter(conn: Conn, job: ClaimedJob, error_code: str, detail: str | None = None) -> None:
    """Terminal failure that must stay visible (dead letters are counted and reported)."""
    params = {**_fence(job), "code": _code(error_code), "detail": _detail(detail)}
    async with mapped_errors():
        cur = await conn.execute(_DEAD_LETTER_SQL, params)
    if cur.rowcount != 1:
        raise LeaseLost()


# --------------------------------------------------------------------------------------------
# Actor operations
# --------------------------------------------------------------------------------------------

_LOCK_FOR_ACTOR_SQL: Final = sql.SQL(
    "select {columns} from ops.jobs where workspace_id = %(workspace_id)s and id = %(id)s for update"
).format(columns=job_columns())
_GET_SQL: Final = sql.SQL(
    "select {columns} from ops.jobs where workspace_id = %(workspace_id)s and id = %(id)s"
).format(columns=job_columns())


async def get_job(conn: Conn, actor: ActorContext, job_id: UUID) -> JobRecord:
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        row = await fetch_one(conn, _GET_SQL, {"workspace_id": actor.workspace_id, "id": job_id})
    if row is None:
        raise NotFound("Job not found")
    return JobRecord.model_validate(row)


async def cancel(conn: Conn, actor: ActorContext, job_id: UUID, *, reason: str | None = None) -> JobRecord:
    """Cancel a job that has not started (queued or retry_wait only); audited."""
    if not (actor.principal_kind == "system" or actor.has(Scope.CONFIG_ADMIN)):
        actor.require(Scope.RECHECKS_REQUEST)
    async with mapped_errors():
        row = await fetch_one(conn, _LOCK_FOR_ACTOR_SQL, {"workspace_id": actor.workspace_id, "id": job_id})
        if row is None:
            raise NotFound("Job not found")
        job = JobRecord.model_validate(row)
        _require_enqueue(actor, job.job_type)
        if job.state not in WAITING_STATES:
            raise VersionConflict(
                "Only queued or waiting jobs can be cancelled", current_state=job.state.value
            )
        updated = await fetch_one(
            conn,
            sql.SQL(
                "update ops.jobs set state = 'cancelled', completed_at = clock_timestamp(),"
                " last_error_code = %(code)s, last_error_detail = %(detail)s"
                " where workspace_id = %(workspace_id)s and id = %(id)s"
                " and state in ('queued', 'retry_wait') returning {columns}"
            ).format(columns=job_columns()),
            {
                "workspace_id": actor.workspace_id,
                "id": job_id,
                "code": CANCELLED,
                "detail": _detail(reason),
            },
        )
        if updated is None:  # pragma: no cover - the row is locked above
            raise VersionConflict("The job changed state concurrently")
        await audit.record(
            conn,
            actor,
            "job.cancel",
            "job",
            job_id,
            reason=reason,
            metadata={"job_type": job.job_type.value, "prior_state": job.state.value},
        )
    return JobRecord.model_validate(updated)


async def unblock(conn: Conn, actor: ActorContext, job_id: UUID, *, reason: str) -> JobRecord:
    """Explicit operator action: move a ``blocked`` job back to ``queued`` (audited).

    The job always gets at least one attempt (``attempts`` is lowered to ``max_attempts - 1``
    when it was used up); otherwise it could never be claimed and reconciliation would
    dead-letter it at once, silently defeating the operator's decision.
    """
    if actor.principal_kind != "system":
        actor.require(Scope.CONFIG_ADMIN)
    async with mapped_errors():
        row = await fetch_one(conn, _LOCK_FOR_ACTOR_SQL, {"workspace_id": actor.workspace_id, "id": job_id})
        if row is None:
            raise NotFound("Job not found")
        job = JobRecord.model_validate(row)
        if job.state != JobState.BLOCKED:
            raise VersionConflict("Only blocked jobs can be unblocked", current_state=job.state.value)
        updated = await fetch_one(
            conn,
            sql.SQL(
                "update ops.jobs set state = 'queued', available_at = clock_timestamp(),"
                " attempts = least(attempts, max_attempts - 1),"
                " blocker_code = null, blocker_detail = null, lease_owner = null,"
                " lease_token = null, lease_expires_at = null, completed_at = null"
                " where workspace_id = %(workspace_id)s and id = %(id)s and state = 'blocked'"
                " returning {columns}"
            ).format(columns=job_columns()),
            {"workspace_id": actor.workspace_id, "id": job_id},
        )
        if updated is None:  # pragma: no cover - the row is locked above
            raise VersionConflict("The job changed state concurrently")
        await audit.record(
            conn,
            actor,
            "job.unblock",
            "job",
            job_id,
            reason=reason,
            metadata={
                "job_type": job.job_type.value,
                "blocker_code": job.blocker_code,
                "attempts_before": job.attempts,
                "attempts_after": int(updated["attempts"]),
            },
        )
    return JobRecord.model_validate(updated)


# --------------------------------------------------------------------------------------------
# Reaper and reconciliation (system processes, one workspace per call)
# --------------------------------------------------------------------------------------------

_REAP_SQL: Final = """
with expired as (
  select id
  from ops.jobs
  where workspace_id = %(workspace_id)s
    and state = 'running'
    and lease_expires_at <= least(clock_timestamp(), coalesce(%(now)s::timestamptz, clock_timestamp()))
  order by lease_expires_at, id
  for update skip locked
  limit %(limit)s
)
update ops.jobs j
set state = case when j.job_type = 'seller_inquiry_send' then 'blocked'
                 when j.attempts < j.max_attempts then 'retry_wait' else 'dead_letter' end,
    completed_at = case when j.job_type = 'seller_inquiry_send' or j.attempts < j.max_attempts
                        then null else clock_timestamp() end,
    available_at = case when j.job_type <> 'seller_inquiry_send' and j.attempts < j.max_attempts
                        then clock_timestamp() + %(delay)s::interval else j.available_at end,
    blocker_code = case when j.job_type = 'seller_inquiry_send'
                        then 'EMAIL_DELIVERY_UNCERTAIN' else j.blocker_code end,
    blocker_detail = case when j.job_type = 'seller_inquiry_send'
                          then 'send lease expired after a possible hand-over; reconcile, never resend'
                          else j.blocker_detail end,
    lease_owner = null,
    lease_token = null,
    lease_expires_at = null,
    last_error_code = 'LEASE_EXPIRED',
    last_error_detail = pg_catalog.left(pg_catalog.format(
      'lease held by %%s expired at %%s without completion (attempt %%s of %%s); recovered by the reaper',
      j.lease_owner, j.lease_expires_at, j.attempts, j.max_attempts), 2000)
from expired
where j.id = expired.id
  and j.workspace_id = %(workspace_id)s
returning j.id, j.job_type, j.state
"""

_RECONCILE_SQL: Final = """
with exhausted as (
  select id
  from ops.jobs
  where workspace_id = %(workspace_id)s
    and state in ('queued', 'retry_wait')
    and attempts >= max_attempts
  order by id
  for update skip locked
  limit %(limit)s
)
update ops.jobs j
set state = 'dead_letter',
    completed_at = clock_timestamp(),
    last_error_code = 'ATTEMPTS_EXHAUSTED',
    last_error_detail = pg_catalog.format(
      'waiting job had used %%s of %%s attempts', j.attempts, j.max_attempts)
from exhausted
where j.id = exhausted.id
  and j.workspace_id = %(workspace_id)s
returning j.id
"""


async def reap_expired(
    db: Database,
    workspace_id: UUID,
    now: datetime | None = None,
    *,
    retry_delay_seconds: int = 30,
    limit: int = 500,
) -> ReapResult:
    """Recover expired leases (crashed or stalled workers) in one short transaction.

    Expiry is judged by database time. ``now`` can only make the reaper MORE conservative
    (``least(now, clock_timestamp())``): an application clock running ahead can never reap a
    lease the database still considers live. Jobs whose worker holds the row lock are skipped
    until the next pass. Prior ``result_reference`` values are never touched. An expired
    ``seller_inquiry_send`` job is ``blocked`` with EMAIL_DELIVERY_UNCERTAIN, never requeued.
    """
    if not 0 <= retry_delay_seconds <= 86_400 or not 1 <= limit <= 10_000:
        raise ValidationFailed("invalid reaper parameters")
    requeued: list[UUID] = []
    dead: list[UUID] = []
    blocked: list[UUID] = []
    by_type: dict[JobType, int] = {}
    dead_by_type: dict[JobType, int] = {}
    async with mapped_errors(), db.transaction(workspace_id=workspace_id) as conn, mapped_errors():
        rows = await fetch_all(
            conn,
            _REAP_SQL,
            {
                "workspace_id": workspace_id,
                "now": None if now is None else _aware(now),
                "delay": timedelta(seconds=retry_delay_seconds),
                "limit": limit,
            },
        )
    for row in rows:
        job_type = JobType(row["job_type"])
        by_type[job_type] = by_type.get(job_type, 0) + 1
        if row["state"] == JobState.RETRY_WAIT.value:
            requeued.append(row["id"])
        elif row["state"] == JobState.BLOCKED.value:
            blocked.append(row["id"])
        else:
            dead.append(row["id"])
            dead_by_type[job_type] = dead_by_type.get(job_type, 0) + 1
    return ReapResult(
        requeued=tuple(requeued),
        dead_lettered=tuple(dead),
        blocked_uncertain=tuple(blocked),
        expired_by_type=by_type,
        dead_lettered_by_type=dead_by_type,
    )


async def reconcile_exhausted(db: Database, workspace_id: UUID, *, limit: int = 500) -> tuple[UUID, ...]:
    """Dead-letter queued/retry_wait jobs with ``attempts >= max_attempts`` (never invisible)."""
    if not 1 <= limit <= 10_000:
        raise ValidationFailed("invalid reconciliation limit")
    async with mapped_errors(), db.transaction(workspace_id=workspace_id) as conn, mapped_errors():
        rows = await fetch_all(conn, _RECONCILE_SQL, {"workspace_id": workspace_id, "limit": limit})
    return tuple(row["id"] for row in rows)


async def queue_stats(conn: Conn, actor: ActorContext) -> QueueStats:
    """Depth by (type, state), oldest due age per type, dead letters, blocked, expired leases."""
    actor.require(Scope.DEALS_READ)
    ws = {"workspace_id": actor.workspace_id}
    async with mapped_errors():
        now_row = await fetch_one(conn, "select clock_timestamp() as now")
        depth_rows = await fetch_all(
            conn,
            "select job_type, state, count(*) as n from ops.jobs"
            " where workspace_id = %(workspace_id)s"
            " and state in ('queued', 'running', 'retry_wait', 'blocked', 'dead_letter')"
            " group by job_type, state",
            ws,
        )
        age_rows = await fetch_all(
            conn,
            "select job_type, clock_timestamp() - min(available_at) as age from ops.jobs"
            " where workspace_id = %(workspace_id)s and state in ('queued', 'retry_wait')"
            " and attempts < max_attempts and available_at <= clock_timestamp()"
            " group by job_type",
            ws,
        )
        extra = await fetch_one(
            conn,
            "select"
            " count(*) filter (where state = 'running' and lease_expires_at <= clock_timestamp())"
            "   as expired_leases,"
            " count(*) filter (where state in ('queued', 'retry_wait') and attempts >= max_attempts)"
            "   as exhausted_waiting"
            " from ops.jobs where workspace_id = %(workspace_id)s"
            " and state in ('running', 'queued', 'retry_wait')",
            ws,
        )
    assert now_row is not None and extra is not None
    depth = {(JobType(r["job_type"]), JobState(r["state"])): int(r["n"]) for r in depth_rows}
    return QueueStats(
        as_of=ensure_utc(now_row["now"]),
        depth=depth,
        oldest_due_age={JobType(r["job_type"]): max(r["age"], timedelta(0)) for r in age_rows},
        dead_letters=sum(n for (_, state), n in depth.items() if state == JobState.DEAD_LETTER),
        blocked=sum(n for (_, state), n in depth.items() if state == JobState.BLOCKED),
        expired_leases=int(extra["expired_leases"]),
        exhausted_waiting=int(extra["exhausted_waiting"]),
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
        raise ValidationFailed("error/blocker codes are 1-80 characters of letters, digits and _ . : -")
    return code


def _detail(detail: str | None) -> str | None:
    if detail is None:
        return None
    cleaned = redact(str(detail)).strip()
    return cleaned[:2000] or None


def _json_size(value: Any, limit: int, label: str) -> int:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be plain JSON") from exc
    size = len(encoded.encode("utf-8"))
    if size > limit:
        raise ValueError(f"{label} exceeds {limit} bytes")
    return size


def _json_object(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    data = dict(value)
    try:
        _json_size(data, MAX_RESULT_BYTES, label)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return data
