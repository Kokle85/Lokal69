"""SQLSTATE mapping, transaction helpers and workspace isolation (ADR 0001, docs/schema.md)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import psycopg
import pytest
from psycopg import sql
from tests.integration.db.helpers import Seed, World, unique
from tests.integration.persistence_core.support import member, spec, system

from suv_deals.domain.enums import JobType, Role
from suv_deals.errors import (
    AppError,
    ErrorCode,
    Forbidden,
    NotFound,
    SourcePaused,
    ValidationFailed,
    VersionConflict,
)
from suv_deals.persistence import gates, jobs, outbox
from suv_deals.persistence.database import Database, fetch_one
from suv_deals.persistence.errors_map import (
    LeaseLost,
    TransactionAborted,
    TransientConflict,
    is_retryable_db_error,
    map_db_error,
)
from suv_deals.persistence.transactions import (
    LOCK_ORDER,
    check_lock_order,
    lock_job,
    lock_source,
    retry_transient,
    unit_of_work,
)

pytestmark = pytest.mark.db


def _raise(conn: psycopg.Connection, sqlstate: str) -> psycopg.Error:
    statement = sql.SQL(
        "do $$ begin raise exception using errcode = {}, message = 'synthetic'; end $$"
    ).format(sql.Literal(sqlstate))
    with pytest.raises(psycopg.Error) as excinfo:
        conn.execute(statement)
    return excinfo.value


@pytest.mark.parametrize(
    ("sqlstate", "expected", "code", "retryable"),
    [
        ("SV001", ValidationFailed, ErrorCode.VALIDATION_ERROR, False),
        ("SV002", VersionConflict, ErrorCode.VERSION_CONFLICT, False),
        ("SV003", ValidationFailed, ErrorCode.VALIDATION_ERROR, False),
        ("SV004", ValidationFailed, ErrorCode.VALIDATION_ERROR, False),
        ("SV005", VersionConflict, ErrorCode.VERSION_CONFLICT, False),
        ("SV006", ValidationFailed, ErrorCode.VALIDATION_ERROR, False),
        ("23505", VersionConflict, ErrorCode.VERSION_CONFLICT, False),
        ("23503", NotFound, ErrorCode.NOT_FOUND, False),
        ("23514", ValidationFailed, ErrorCode.VALIDATION_ERROR, False),
        ("23502", ValidationFailed, ErrorCode.VALIDATION_ERROR, False),
        ("22P02", ValidationFailed, ErrorCode.VALIDATION_ERROR, False),
        ("42501", Forbidden, ErrorCode.FORBIDDEN, False),
        ("40001", TransientConflict, ErrorCode.VERSION_CONFLICT, True),
        ("40P01", TransientConflict, ErrorCode.VERSION_CONFLICT, True),
        ("55P03", TransientConflict, ErrorCode.VERSION_CONFLICT, True),
        ("57014", AppError, ErrorCode.DEPENDENCY_UNAVAILABLE, True),
        ("XX000", AppError, ErrorCode.INTERNAL_ERROR, True),
    ],
)
def test_sqlstates_map_to_typed_safe_errors(
    db_conn: psycopg.Connection, sqlstate: str, expected: type[AppError], code: ErrorCode, retryable: bool
) -> None:
    exc = _raise(db_conn, sqlstate)
    mapped = map_db_error(exc)
    assert isinstance(mapped, expected) and mapped.code == code and mapped.retryable == retryable
    assert "synthetic" not in mapped.message and "raise" not in mapped.message.lower()
    assert is_retryable_db_error(exc) == (sqlstate in {"40001", "40P01", "55P03", "57014"})


async def test_real_constraint_violations_are_mapped(db: Database, world_a: World, world_b: World) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    # Foreign key to another workspace's source: NotFound (existence is not revealed).
    with pytest.raises(NotFound):
        async with unit_of_work(db, actor) as conn:
            await jobs.enqueue(conn, actor, spec(source_id=world_b.source_id))
    # CHECK constraint (state value) with its stable constraint name.
    with pytest.raises(ValidationFailed) as excinfo:
        async with unit_of_work(db, actor) as conn:
            await conn.execute(
                "insert into ops.jobs (workspace_id, job_type, dedup_key, priority)"
                " values (%s, 'valuation', %s, 5000)",
                (ws, unique("job")),
            )
    assert excinfo.value.details == {"constraint": "jobs_priority_ck"}
    # Unique violation outside an ON CONFLICT path.
    event_id = uuid.uuid4()
    with pytest.raises(VersionConflict):
        async with unit_of_work(db, actor) as conn:
            for _ in range(2):
                await outbox.enqueue_event(
                    conn,
                    actor,
                    event_type="review.pending",
                    event_version=1,
                    aggregate_type="review_case",
                    aggregate_id=uuid.uuid4(),
                    aggregate_version=1,
                    payload={"schema_version": "1.0"},
                    dedup_key=unique("review.pending"),
                    event_id=event_id,
                )


async def test_statement_and_lock_timeouts_are_retryable(db_url: str, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    job_id = seed.job(ws)
    slow_queries = Database(db_url, set_role="suv_backend", statement_timeout_ms=100)
    busy_rows = Database(db_url, set_role="suv_backend", statement_timeout_ms=5_000, lock_timeout_ms=100)
    await slow_queries.open()
    await busy_rows.open()
    holder = await psycopg.AsyncConnection.connect(db_url)
    try:
        with pytest.raises(AppError) as slow:
            async with unit_of_work(slow_queries, system(ws)) as conn:
                await conn.execute("select pg_sleep(1)")
        assert slow.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE and slow.value.retryable
        await holder.execute("select id from ops.jobs where id = %s for update", (job_id,))
        with pytest.raises(TransientConflict):
            async with unit_of_work(busy_rows, system(ws)) as conn:
                await conn.execute(
                    "update ops.jobs set priority = 1 where workspace_id = %s and id = %s", (ws, job_id)
                )
    finally:
        await holder.rollback()
        await holder.close()
        await slow_queries.close()
        await busy_rows.close()


async def test_retry_transient_reruns_the_whole_unit_of_work(db: Database, world_a: World) -> None:
    calls: list[int] = []

    async def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise TransientConflict()
        return "ok"

    assert await retry_transient(flaky, attempts=3, base_delay_seconds=0) == "ok" and len(calls) == 3

    async def lost() -> str:
        calls.append(1)
        raise LeaseLost()

    calls.clear()
    with pytest.raises(LeaseLost):
        await retry_transient(lost, attempts=5, base_delay_seconds=0)
    assert len(calls) == 1


def test_lock_order_checker() -> None:
    check_lock_order(list(LOCK_ORDER))
    check_lock_order(["ops.idempotency_records", "app.review_cases", "ops.outbox", "ops.audit_events"])
    check_lock_order(["ops.jobs", "app.listings", "app.listings", "app.listing_revisions"])
    with pytest.raises(ValueError):
        check_lock_order(["app.review_cases", "app.listings"])
    with pytest.raises(ValueError):
        check_lock_order(["ops.outbox", "ops.jobs"])
    with pytest.raises(ValueError):
        check_lock_order(["app.unknown"])


async def test_lock_source_revalidates_source_state(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    async with unit_of_work(db, system(ws)) as conn:
        # The synthetic source is not enabled: no new network work, but captured evidence may proceed.
        with pytest.raises(SourcePaused):
            await lock_source(conn, ws, world_a.source_id, for_network=True)
        source = await lock_source(conn, ws, world_a.source_id, for_network=False)
        assert source.id == world_a.source_id and not source.enabled
        with pytest.raises(NotFound):
            await lock_source(conn, ws, uuid.uuid4(), for_network=False)


async def test_backend_role_is_used_and_workspaces_are_isolated(
    db: Database, world_a: World, world_b: World, seed: Seed
) -> None:
    a, b = world_a.workspace_id, world_b.workspace_id
    async with db.transaction(system(a)) as conn:
        row = await fetch_one(conn, "select current_user as role")
    assert row is not None and row["role"] == "suv_backend"
    actor_a, actor_b = system(a), system(b)
    async with db.transaction(actor_b) as conn:
        job_b, _ = await jobs.enqueue(conn, actor_b, spec(priority=1000))
        event_b, _ = await outbox.enqueue_event(
            conn,
            actor_b,
            event_type="review.pending",
            event_version=1,
            aggregate_type="review_case",
            aggregate_id=uuid.uuid4(),
            aggregate_version=1,
            payload={"schema_version": "1.0"},
            dedup_key=unique("review.pending"),
        )
        await gates.seed_spec_gates(conn, actor_b)
    # Workspace A workers never see B's work.
    assert await jobs.claim(db, a, "worker-a", list(JobType), 60) is None
    assert await outbox.claim_events(db, a, "dispatcher-a", 60, 10) == []
    async with db.transaction(actor_a) as conn:
        assert await gates.list_gates(conn, actor_a) == []
        stats = await jobs.queue_stats(conn, actor_a)
        assert stats.depth == {}
    owner_a = member(a, Role.OWNER)
    checks: list[Any] = [
        lambda c: jobs.get_job(c, owner_a, job_b),
        lambda c: jobs.cancel(c, owner_a, job_b),
        lambda c: jobs.unblock(c, owner_a, job_b, reason="cross-workspace attempt"),
        lambda c: outbox.get_event(c, owner_a, event_b),
        lambda c: outbox.cancel_stale(c, owner_a, event_b, "stale_case_version"),
        lambda c: outbox.reconcile_uncertain(c, owner_a, event_b, provider="slack", accepted=True),
    ]
    for check in checks:
        with pytest.raises(NotFound):
            async with unit_of_work(db, owner_a) as conn:
                await check(conn)
    # Even with B's id passed explicitly, A's transaction cannot lock or write B's rows.
    claimed_b = await jobs.claim(db, b, "worker-b", [JobType.VALUATION], 60)
    assert claimed_b is not None and claimed_b.id == job_b
    async with unit_of_work(db, actor_a) as conn:
        with pytest.raises(LeaseLost):
            await lock_job(conn, b, job_b, claimed_b.lease_token, claimed_b.lease_owner)
    with pytest.raises(Forbidden):  # RLS WITH CHECK: the GUC says A, the row says B
        async with unit_of_work(db, actor_a) as conn:
            await jobs.enqueue(conn, actor_b, spec())
    async with unit_of_work(db, actor_a) as conn:
        cur = await conn.execute("update ops.jobs set priority = 0 where id = %s", (job_b,))
        assert cur.rowcount == 0
    assert seed.scalar("select state from ops.jobs where id = %s", (job_b,)) == "running"


async def test_concurrent_transactions_on_one_job_serialise_on_the_row_lock(
    db: Database, world_a: World, seed: Seed
) -> None:
    """The reaper skips a job whose worker currently holds the row lock (SKIP LOCKED)."""
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        job_id, _ = await jobs.enqueue(conn, actor, spec())
    job = await jobs.claim(db, ws, "worker-1", [JobType.VALUATION], 0.3)
    assert job is not None
    locked = asyncio.Event()
    release = asyncio.Event()

    async def worker() -> None:
        try:
            async with unit_of_work(db, actor) as conn:
                await lock_job(conn, ws, job.id, job.lease_token, job.lease_owner)
                locked.set()
                await release.wait()
                await jobs.complete(conn, job, {"done": True})
        except LeaseLost:
            pass

    task = asyncio.create_task(worker())
    await locked.wait()
    await asyncio.sleep(0.4)  # the lease expires while the worker holds the row lock
    reaped = await jobs.reap_expired(db, ws)
    assert reaped.total == 0  # skipped, not blocked
    release.set()
    await task
    assert seed.scalar("select state from ops.jobs where id = %s", (job_id,)) == "running"
    assert (await jobs.reap_expired(db, ws)).requeued == (job_id,)


async def test_a_swallowed_database_error_never_reports_a_silent_commit(
    db: Database, world_a: World, world_b: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    dedup = unique("valuation")
    with pytest.raises(TransactionAborted):
        async with unit_of_work(db, actor) as conn:
            await jobs.enqueue(conn, actor, spec(dedup_key=dedup))
            with contextlib.suppress(NotFound):  # a careless caller carries on regardless
                await jobs.enqueue(conn, actor, spec(source_id=world_b.source_id))
    assert seed.scalar("select count(*) from ops.jobs where dedup_key = %s", (dedup,)) == 0
