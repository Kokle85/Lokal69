"""Transaction helpers, the global lock order and the worker unit-of-work pattern.

Global lock order (docs/schema.md section 4, architecture.md). Every code path that locks
more than one of these takes them in this order, so concurrent transactions cannot deadlock::

    ops.jobs row -> app.sources -> app.listings -> app.listing_revisions
      -> app.valuations -> app.review_cases -> ops.outbox

Rows of persistence-core tables outside that chain:

- ``ops.idempotency_records``: an API/MCP mutation takes its idempotency record FIRST
  (`idempotency.begin`), before any domain row. Workers never use idempotency records.
- ``ops.audit_events`` and ``ops.delivery_attempts`` are insert-only and written LAST.
- ``ops.host_budgets``, ``ops.query_snapshots`` and ``ops.activation_gates`` are only touched in
  their own short transactions, never together with domain rows.

Worker unit of work (spec 13 "Network I/O happens outside database transactions")::

    job = await jobs.claim(db, ws, worker_id, [JobType.DETAIL], lease_seconds=300)  # short tx 1
    document = await crawl_client.fetch(...)                     # network I/O, no transaction
    async with job_unit_of_work(db, job) as (conn, locked):      # short tx 2
        # lock_job already ran FIRST: state, token, owner and lease expiry revalidated
        source = await lock_source(conn, ws, locked.source_id, for_network=False)
        ...   # lock listing -> revision -> valuation -> review case -> outbox, idempotent writes
        await outbox.enqueue_event(conn, actor, ...)              # same transaction
        await jobs.complete(conn, job, {"revision_id": ...})      # guarded; LeaseLost -> ROLLBACK

If `jobs.complete` (or any fenced update) raises `LeaseLost`, the exception leaves the
``async with`` block, so the ENTIRE transaction (domain writes, downstream jobs, outbox rows)
rolls back; a newer lease holder owns the work. Transient failures (serialization, deadlock,
lock timeout) surface as `TransientConflict`; `retry_transient` re-runs a whole unit of work.
Database errors, including deferred-constraint failures at COMMIT, are mapped to `AppError`s.
If the caller swallows a database error and leaves the block normally, PostgreSQL would turn the
COMMIT into a silent ROLLBACK; the helpers raise `TransactionAborted` instead.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Final
from uuid import UUID

from psycopg import pq, sql
from pydantic import BaseModel, ConfigDict

from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import TechnicalStatus
from suv_deals.errors import AppError, NotFound, SourcePaused
from suv_deals.persistence.database import Conn, Database, fetch_one
from suv_deals.persistence.errors_map import LeaseLost, TransactionAborted, mapped_errors
from suv_deals.persistence.jobs import ClaimedJob, JobRecord, job_columns

LOCK_ORDER: Final[tuple[str, ...]] = (
    "ops.jobs",
    "app.sources",
    "app.listings",
    "app.listing_revisions",
    "app.valuations",
    "app.review_cases",
    "ops.outbox",
)
# Taken before the chain by API/MCP mutations (never by workers).
PRE_CHAIN_LOCKS: Final[tuple[str, ...]] = ("ops.idempotency_records",)
# Insert-only tables written after the chain.
POST_CHAIN_INSERTS: Final[tuple[str, ...]] = ("ops.delivery_attempts", "ops.audit_events")

_RANK: Final[dict[str, int]] = {
    **{name: -1 for name in PRE_CHAIN_LOCKS},
    **{name: i for i, name in enumerate(LOCK_ORDER)},
    **{name: len(LOCK_ORDER) for name in POST_CHAIN_INSERTS},
}


def check_lock_order(tables: Sequence[str]) -> None:
    """Raise ``ValueError`` if ``tables`` (in the order a code path locks them) violates the
    global order. Repeated tables are fine (several rows of one table)."""
    previous = -2
    for name in tables:
        if name not in _RANK:
            raise ValueError(f"{name} is not part of the documented lock order")
        rank = _RANK[name]
        if rank < previous:
            raise ValueError(f"lock order violation: {name} after a later table")
        previous = rank


class SourceLock(BaseModel):
    """The source row as seen (``FOR SHARE``) inside a unit of work."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    source_key: str
    enabled: bool
    paused: bool
    technical_status: TechnicalStatus
    version: int


_LOCK_JOB_SQL: Final = sql.SQL(
    "select {columns} from ops.jobs"
    " where workspace_id = %(workspace_id)s and id = %(id)s and state = 'running'"
    " and lease_token = %(token)s and lease_owner = %(owner)s"
    " and lease_expires_at > clock_timestamp()"
    " for update"
).format(columns=job_columns())

_LOCK_SOURCE_SQL: Final = (
    "select id, source_key, enabled, paused, technical_status, version from app.sources"
    " where workspace_id = %(workspace_id)s and id = %(id)s for share"
)


async def lock_job(conn: Conn, workspace_id: UUID, job_id: UUID, lease_token: UUID, owner: str) -> JobRecord:
    """Lock the job row FIRST in the post-fetch transaction and revalidate the lease.

    Requires ``state = 'running'``, the caller's token and owner, and ``lease_expires_at >
    clock_timestamp()`` (actual database time, not the transaction start). Raises `LeaseLost`
    otherwise (also for a missing or foreign-workspace job: nothing to leak, nothing to do).
    """
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _LOCK_JOB_SQL,
            {"workspace_id": workspace_id, "id": job_id, "token": lease_token, "owner": owner},
        )
    if row is None:
        raise LeaseLost()
    return JobRecord.model_validate(row)


async def lock_source(conn: Conn, workspace_id: UUID, source_id: UUID, *, for_network: bool) -> SourceLock:
    """Second in the lock order: share-lock the source and revalidate it.

    ``for_network=True`` (work that will cause new requests) requires an enabled, unpaused source
    that is not access-blocked/parser-unhealthy, else `SourcePaused`. Normalisation and review of
    already captured evidence pass ``for_network=False`` (spec 13: a pause stops new network work
    only). A concurrent pause waits for this short transaction.
    """
    async with mapped_errors():
        row = await fetch_one(conn, _LOCK_SOURCE_SQL, {"workspace_id": workspace_id, "id": source_id})
    if row is None:
        raise NotFound("Source not found")
    source = SourceLock.model_validate(row)
    if for_network and (
        not source.enabled
        or source.paused
        or source.technical_status in (TechnicalStatus.ACCESS_BLOCKED, TechnicalStatus.PARSER_UNHEALTHY)
    ):
        raise SourcePaused()
    return source


@asynccontextmanager
async def unit_of_work(db: Database, actor: ActorContext) -> AsyncIterator[Conn]:
    """One short workspace-scoped transaction with database errors mapped to `AppError`s,
    including deferred-constraint failures raised at COMMIT.

    Errors raised by the caller's statements are mapped INSIDE the transaction, so a lock
    timeout, deadlock or serialization failure surfaces as a retryable `TransientConflict`
    rather than the generic "database unavailable" of the connection layer.
    """
    async with mapped_errors(), db.transaction(actor) as conn, mapped_errors():
        yield conn
        _refuse_aborted(conn)


@asynccontextmanager
async def job_unit_of_work(db: Database, job: ClaimedJob) -> AsyncIterator[tuple[Conn, JobRecord]]:
    """The post-fetch transaction of a leased job: `lock_job` runs first; any `LeaseLost`
    (from the revalidation or the final `jobs.complete`) rolls everything back."""
    async with mapped_errors(), db.transaction(workspace_id=job.workspace_id) as conn, mapped_errors():
        locked = await lock_job(conn, job.workspace_id, job.id, job.lease_token, job.lease_owner)
        yield conn, locked
        _refuse_aborted(conn)


def _refuse_aborted(conn: Conn) -> None:
    """COMMIT of an aborted transaction is a silent ROLLBACK: surface it as an error."""
    if conn.info.transaction_status == pq.TransactionStatus.INERROR:
        raise TransactionAborted()


async def retry_transient[T](
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay_seconds: float = 0.05,
    rng: random.Random | None = None,
) -> T:
    """Re-run a whole unit of work on retryable failures (serialization, deadlock, lock timeout).

    ``operation`` must open its own transaction each time. `LeaseLost` and other non-retryable
    errors propagate immediately.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    jitter = rng or random.Random()  # noqa: S311 - backoff jitter, not security
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except AppError as exc:
            if isinstance(exc, LeaseLost) or not exc.retryable or attempt == attempts:
                raise
            await asyncio.sleep(jitter.uniform(0, base_delay_seconds * (2 ** (attempt - 1))))
    raise AssertionError("unreachable")  # pragma: no cover
