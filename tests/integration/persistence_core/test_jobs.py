"""Durable job queue through the persistence API on real PostgreSQL (spec 9, 13, 31 Queue/Scheduler).

Everything runs as ``suv_backend`` under workspace RLS via ``Database(set_role=...)``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from uuid import UUID

import psycopg
import pytest
from tests.integration.db.helpers import Seed, World, unique
from tests.integration.persistence_core.support import (
    expire_job_lease,
    job_row,
    member,
    spec,
    system,
)

from suv_deals.domain.enums import JobState, JobType, Role
from suv_deals.errors import Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.persistence import jobs, outbox
from suv_deals.persistence.database import Conn, Database
from suv_deals.persistence.errors_map import LeaseLost
from suv_deals.persistence.transactions import job_unit_of_work, lock_job

pytestmark = pytest.mark.db


async def _enqueue(db: Database, ws: UUID, **kwargs: object) -> UUID:
    actor = system(ws)
    async with db.transaction(actor) as conn:
        job_id, created = await jobs.enqueue(conn, actor, spec(**kwargs))
    assert created
    return job_id


async def test_enqueue_is_idempotent_on_the_open_dedup_key(db: Database, world_a: World) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    key = unique("valuation")
    async with db.transaction(actor) as conn:
        first, created_first = await jobs.enqueue(conn, actor, spec(dedup_key=key, payload={"n": 1}))
        again, created_again = await jobs.enqueue(conn, actor, spec(dedup_key=key, payload={"n": 2}))
    assert created_first and not created_again and again == first
    job = await jobs.claim(db, ws, "worker-1", [JobType.VALUATION], 60)
    assert job is not None and job.id == first and job.payload == {"n": 1}
    # Still open while running: no duplicate.
    async with db.transaction(actor) as conn:
        assert await jobs.enqueue(conn, actor, spec(dedup_key=key)) == (first, False)
    async with job_unit_of_work(db, job) as (conn, _locked):
        await jobs.complete(conn, job, {"ok": True})
    # A terminal job does not block a new one.
    async with db.transaction(actor) as conn:
        newer, created = await jobs.enqueue(conn, actor, spec(dedup_key=key))
    assert created and newer != first


async def test_concurrent_enqueue_of_one_dedup_key_creates_one_job(db: Database, world_a: World) -> None:
    ws = world_a.workspace_id
    key = unique("detail")

    async def attempt() -> tuple[UUID, bool]:
        actor = system(ws)
        async with db.transaction(actor) as conn:
            result = await jobs.enqueue(conn, actor, spec(dedup_key=key))
            await conn.execute("select pg_sleep(0.05)")
        return result

    results = await asyncio.gather(*(attempt() for _ in range(5)))
    assert len({job_id for job_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1


async def test_concurrent_schedulers_create_exactly_one_job_per_slot(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id

    async def schedule(name: str) -> tuple[UUID, bool]:
        actor = system(ws)
        async with db.transaction(actor) as conn:
            slot = await jobs.current_slot(conn)
            result = await jobs.enqueue_slot(
                conn,
                actor,
                spec(
                    JobType.DISCOVERY,
                    dedup_key=f"discovery:{name}:{slot.isoformat()}",
                    source_id=world_a.source_id,
                    profile_id=world_a.profile_id,
                    partition_key="default",
                ),
                slot,
            )
            await conn.execute("select pg_sleep(0.05)")  # hold the slot insert open under contention
        return result

    results = await asyncio.gather(*(schedule(f"scheduler-{i}") for i in range(6)))
    ids = {job_id for job_id, _ in results}
    assert len(ids) == 1 and sum(created for _, created in results) == 1
    assert (
        seed.scalar(
            "select count(*) from ops.jobs where workspace_id = %s and job_type = 'discovery'", (ws,)
        )
        == 1
    )
    # The slot stays taken forever, even after the job finished (spec 9: never scheduled twice).
    job_id = ids.pop()
    seed.conn.execute(
        "update ops.jobs set state = 'succeeded', completed_at = now() where id = %s", (job_id,)
    )
    again = await schedule("late-scheduler")
    assert again == (job_id, False)


async def test_slot_jobs_require_their_partition_binding(db: Database, world_a: World) -> None:
    actor = system(world_a.workspace_id)
    async with db.transaction(actor) as conn:
        slot = await jobs.current_slot(conn)
        with pytest.raises(ValidationFailed):
            await jobs.enqueue_slot(conn, actor, spec(JobType.DISCOVERY), slot)


async def test_concurrent_workers_never_claim_the_same_job(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    actor = system(ws)
    async with db.transaction(actor) as conn:
        expected = {(await jobs.enqueue(conn, actor, spec()))[0] for _ in range(60)}

    async def worker(name: str) -> list[UUID]:
        mine: list[UUID] = []
        while True:
            job = await jobs.claim(db, ws, name, [JobType.VALUATION], 120)
            if job is None:
                return mine
            assert job.lease_owner == name and job.attempts == 1
            mine.append(job.id)

    claimed = await asyncio.gather(*(worker(f"worker-{i}") for i in range(6)))
    flat = [job_id for batch in claimed for job_id in batch]
    assert len(flat) == len(set(flat)), "a job was claimed twice"
    assert set(flat) == expected
    assert sum(1 for batch in claimed if batch) >= 2, "several workers should have obtained work"
    rows = seed.conn.execute(
        "select count(distinct lease_token), count(*) from ops.jobs where workspace_id = %s", (ws,)
    ).fetchone()
    assert rows == (60, 60)


async def test_claim_skips_rows_locked_by_another_transaction(
    db: Database, world_a: World, db_url: str
) -> None:
    ws = world_a.workspace_id
    locked_job = await _enqueue(db, ws, priority=100)
    free_job = await _enqueue(db, ws, priority=1)
    holder = await psycopg.AsyncConnection.connect(db_url)
    try:
        await holder.execute("select id from ops.jobs where id = %s for update", (locked_job,))
        job = await jobs.claim(db, ws, "worker-1", [JobType.VALUATION], 60)
        assert job is not None and job.id == free_job  # SKIP LOCKED, not a wait
        assert await jobs.claim(db, ws, "worker-1", [JobType.VALUATION], 60) is None
    finally:
        await holder.rollback()
        await holder.close()
    job = await jobs.claim(db, ws, "worker-2", [JobType.VALUATION], 60)
    assert job is not None and job.id == locked_job


async def test_claim_filters_job_types_priority_and_due_time(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    future = seed.scalar("select now() + interval '1 hour'")
    await _enqueue(db, ws, available_at=future, priority=1000)
    comparables = await _enqueue(db, ws, job_type=JobType.COMPARABLES, priority=900)
    low = await _enqueue(db, ws, priority=1)
    high = await _enqueue(db, ws, priority=50)
    first = await jobs.claim(db, ws, "w", [JobType.VALUATION], 60)
    second = await jobs.claim(db, ws, "w", [JobType.VALUATION], 60)
    assert first is not None and first.id == high
    assert second is not None and second.id == low
    assert await jobs.claim(db, ws, "w", [JobType.VALUATION], 60) is None
    other = await jobs.claim(db, ws, "w", [JobType.COMPARABLES, JobType.VALUATION], 60)
    assert other is not None and other.id == comparables


async def test_crash_before_commit_returns_the_job_through_the_reaper(
    db: Database, world_a: World, seed: Seed
) -> None:
    """Worker claims, starts its post-fetch transaction and dies before COMMIT (spec 31 Queue)."""
    ws = world_a.workspace_id
    job_id = await _enqueue(db, ws, max_attempts=3)
    job = await jobs.claim(db, ws, "worker-crash", [JobType.VALUATION], 60)
    assert job is not None and job.id == job_id
    marker = unique("domain-write")

    class Crash(Exception):
        pass

    with pytest.raises(Crash):
        async with job_unit_of_work(db, job) as (conn, _locked):
            await conn.execute(
                "insert into ops.audit_events (workspace_id, actor_principal_id, actor_kind, action,"
                " target_type, target_id, request_id) values (%s, %s, 'system', 'listing.promote',"
                " 'job', %s, %s)",
                (ws, uuid.uuid4(), job_id, marker),
            )
            raise Crash
    assert seed.scalar("select count(*) from ops.audit_events where request_id = %s", (marker,)) == 0
    assert job_row(seed, job_id)["state"] == "running"
    # Not reaped while the lease is live (database time decides).
    assert (await jobs.reap_expired(db, ws)).total == 0
    expire_job_lease(seed, job_id)
    reaped = await jobs.reap_expired(db, ws, retry_delay_seconds=0)
    assert reaped.requeued == (job_id,) and reaped.dead_lettered == ()
    assert reaped.expired_by_type == {JobType.VALUATION: 1}
    row = job_row(seed, job_id)
    assert row["state"] == "retry_wait" and row["lease_token"] is None and row["lease_owner"] is None
    assert row["last_error_code"] == "LEASE_EXPIRED" and "worker-crash" in row["last_error_detail"]
    again = await jobs.claim(db, ws, "worker-2", [JobType.VALUATION], 60)
    assert again is not None and again.id == job_id
    assert again.lease_token != job.lease_token and again.attempts == 2


async def test_crash_after_commit_has_no_duplicate_business_effect(
    db: Database, world_a: World, seed: Seed
) -> None:
    """The unit of work committed (domain write + outbox + completion) and the process died
    before acknowledging: nothing is re-run, and a replayed business operation is deduplicated."""
    ws = world_a.workspace_id
    actor = system(ws)
    job_id = await _enqueue(db, ws)
    job = await jobs.claim(db, ws, "worker-1", [JobType.VALUATION], 60)
    assert job is not None
    dedup = unique("review.pending")
    aggregate = uuid.uuid4()

    async def business_effect(conn: Conn) -> tuple[UUID, bool]:
        return await outbox.enqueue_event(
            conn,
            actor,
            event_type="review.pending",
            event_version=1,
            aggregate_type="review_case",
            aggregate_id=aggregate,
            aggregate_version=1,
            payload={"schema_version": "1.0", "synthetic": True},
            dedup_key=dedup,
        )

    async with job_unit_of_work(db, job) as (conn, _locked):
        event_id, created = await business_effect(conn)
        await jobs.complete(conn, job, {"event_id": str(event_id)})
    assert created
    # "Crash" here. The reaper and other workers find nothing to redo.
    assert (await jobs.reap_expired(db, ws)).total == 0
    assert await jobs.claim(db, ws, "worker-2", [JobType.VALUATION], 60) is None
    # A replay of the same business step (e.g. a re-enqueued job) cannot duplicate the effect.
    async with db.transaction(actor) as conn:
        assert await business_effect(conn) == (event_id, False)
    assert seed.scalar("select count(*) from ops.outbox where workspace_id = %s", (ws,)) == 1
    row = job_row(seed, job_id)
    assert row["state"] == "succeeded" and row["result_reference"] == {"event_id": str(event_id)}


async def test_lease_lost_during_the_unit_of_work_rolls_everything_back(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    job_id = await _enqueue(db, ws)
    job = await jobs.claim(db, ws, "worker-slow", [JobType.VALUATION], 0.3)
    assert job is not None
    marker = unique("late-write")
    with pytest.raises(LeaseLost):
        async with job_unit_of_work(db, job) as (conn, _locked):
            await conn.execute(
                "insert into ops.audit_events (workspace_id, actor_principal_id, actor_kind, action,"
                " target_type, target_id, request_id) values (%s, %s, 'system', 'listing.promote',"
                " 'job', %s, %s)",
                (ws, uuid.uuid4(), job_id, marker),
            )
            await asyncio.sleep(0.4)  # the lease expires mid-transaction (clock_timestamp)
            await jobs.complete(conn, job, {"late": True})
    assert seed.scalar("select count(*) from ops.audit_events where request_id = %s", (marker,)) == 0
    row = job_row(seed, job_id)
    assert row["state"] == "running" and row["result_reference"] is None


async def test_newer_lease_holder_wins_and_the_late_worker_is_fenced(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    job_id = await _enqueue(db, ws)
    old = await jobs.claim(db, ws, "worker-old", [JobType.VALUATION], 60)
    assert old is not None
    expire_job_lease(seed, job_id)
    assert (await jobs.reap_expired(db, ws, retry_delay_seconds=0)).requeued == (job_id,)
    new = await jobs.claim(db, ws, "worker-new", [JobType.VALUATION], 60)
    assert new is not None and new.id == job_id and new.lease_token != old.lease_token
    # The old worker can neither heartbeat, lock, complete, retry, block nor dead-letter.
    assert await jobs.heartbeat(db, old, 60) is False
    async with db.transaction(system(ws)) as conn:
        with pytest.raises(LeaseLost):
            await lock_job(conn, ws, job_id, old.lease_token, old.lease_owner)
    for action in (
        lambda c: jobs.complete(c, old, {"old": True}),
        lambda c: jobs.fail_retry(c, old, "TRANSIENT"),
        lambda c: jobs.fail_blocked(c, old, "source_paused"),
        lambda c: jobs.dead_letter(c, old, "BROKEN"),
    ):
        with pytest.raises(LeaseLost):
            async with db.transaction(system(ws)) as conn:
                await action(conn)
    async with job_unit_of_work(db, new) as (conn, locked):
        assert locked.lease_token == new.lease_token
        await jobs.complete(conn, new, {"new": True})
    # Even an exact replay of the old completion cannot overwrite the committed result.
    with pytest.raises(LeaseLost):
        async with db.transaction(system(ws)) as conn:
            await jobs.complete(conn, old, {"old": True})
    row = job_row(seed, job_id)
    assert row["state"] == "succeeded" and row["result_reference"] == {"new": True}


async def test_heartbeat_extends_only_the_current_lease(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    job_id = await _enqueue(db, ws)
    job = await jobs.claim(db, ws, "worker-1", [JobType.VALUATION], 30)
    assert job is not None
    assert await jobs.heartbeat(db, job, 600)
    remaining = seed.scalar("select lease_expires_at - clock_timestamp() from ops.jobs where id = %s", (job_id,))
    assert remaining > timedelta(minutes=9)
    forged = job.model_copy(update={"lease_token": uuid.uuid4()})
    assert await jobs.heartbeat(db, forged, 600) is False
    other_owner = job.model_copy(update={"lease_owner": "worker-2"})
    assert await jobs.heartbeat(db, other_owner, 600) is False


async def test_retry_then_dead_letter_after_max_attempts(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    job_id = await _enqueue(db, ws, max_attempts=2)
    first = await jobs.claim(db, ws, "w", [JobType.VALUATION], 60)
    assert first is not None
    async with db.transaction(system(ws)) as conn:
        state = await jobs.fail_retry(conn, first, "HTTP_503", timedelta(0), detail="upstream 503")
    assert state == JobState.RETRY_WAIT
    row = job_row(seed, job_id)
    assert row["lease_token"] is None and row["last_error_code"] == "HTTP_503"
    second = await jobs.claim(db, ws, "w", [JobType.VALUATION], 60)
    assert second is not None and second.attempts == 2
    async with db.transaction(system(ws)) as conn:
        state = await jobs.fail_retry(conn, second, "HTTP_503", timedelta(seconds=5))
    assert state == JobState.DEAD_LETTER
    row = job_row(seed, job_id)
    assert row["state"] == "dead_letter" and row["completed_at"] is not None
    assert await jobs.claim(db, ws, "w", [JobType.VALUATION], 60) is None


async def test_retry_at_is_never_in_the_past_and_details_are_redacted(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    job_id = await _enqueue(db, ws)
    job = await jobs.claim(db, ws, "w", [JobType.VALUATION], 60)
    assert job is not None
    past = seed.scalar("select now() - interval '1 day'")
    async with db.transaction(system(ws)) as conn:
        await jobs.fail_retry(
            conn, job, "AUTH", past, detail="token=abc123secret Authorization: Bearer eyJabc.def.ghi"
        )
    detail = job_row(seed, job_id)["last_error_detail"]
    assert "abc123secret" not in detail and "eyJabc" not in detail
    assert seed.scalar("select available_at >= now() - interval '1 second' from ops.jobs where id = %s", (job_id,))
    with pytest.raises(ValidationFailed):
        async with db.transaction(system(ws)) as conn:
            await jobs.fail_retry(conn, job, "not a code!")


async def test_exhausted_waiting_jobs_are_reconciled_into_dead_letter(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    exhausted = seed.job(ws, state="retry_wait", attempts=3, max_attempts=3)
    queued_exhausted = seed.job(ws, state="queued", attempts=1, max_attempts=1)
    healthy = seed.job(ws, state="retry_wait", attempts=1, max_attempts=3)
    async with db.transaction(member(ws)) as conn:
        stats = await jobs.queue_stats(conn, member(ws))
    assert stats.exhausted_waiting == 2
    reconciled = await jobs.reconcile_exhausted(db, ws)
    assert set(reconciled) == {exhausted, queued_exhausted}
    assert job_row(seed, exhausted)["last_error_code"] == "ATTEMPTS_EXHAUSTED"
    assert job_row(seed, healthy)["state"] == "retry_wait"
    async with db.transaction(member(ws)) as conn:
        stats = await jobs.queue_stats(conn, member(ws))
    assert stats.exhausted_waiting == 0 and stats.dead_letters == 2


async def test_expired_lease_without_attempts_left_is_dead_lettered(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    job_id = await _enqueue(db, ws, max_attempts=1)
    job = await jobs.claim(db, ws, "w", [JobType.VALUATION], 60)
    assert job is not None
    expire_job_lease(seed, job_id)
    reaped = await jobs.reap_expired(db, ws)
    assert reaped.dead_lettered == (job_id,) and reaped.requeued == ()
    row = job_row(seed, job_id)
    assert row["state"] == "dead_letter" and row["completed_at"] is not None and row["lease_token"] is None


async def test_reaper_now_parameter_can_only_be_more_conservative(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    job_id = await _enqueue(db, ws)
    job = await jobs.claim(db, ws, "w", [JobType.VALUATION], 60)
    assert job is not None
    far_future = seed.scalar("select now() + interval '1 day'")
    # An application clock running ahead cannot reap a lease the database considers live.
    assert (await jobs.reap_expired(db, ws, far_future)).total == 0
    expire_job_lease(seed, job_id)
    long_ago = seed.scalar("select now() - interval '1 day'")
    assert (await jobs.reap_expired(db, ws, long_ago)).total == 0
    assert (await jobs.reap_expired(db, ws)).requeued == (job_id,)


async def test_unknown_payload_version_blocks_the_job_with_a_typed_blocker(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    newer = await _enqueue(db, ws, payload_version=2, priority=10)
    current = await _enqueue(db, ws, payload_version=1, priority=1)
    understood = {JobType.VALUATION: {1}}
    job = await jobs.claim(db, ws, "old-worker", [JobType.VALUATION], 60, payload_versions=understood)
    assert job is not None and job.id == current
    row = job_row(seed, newer)
    assert row["state"] == "blocked" and row["blocker_code"] == jobs.INCOMPATIBLE_PAYLOAD_VERSION
    assert row["attempts"] == 0 and row["lease_token"] is None
    # The blocked job keeps its dedup key open (no duplicate) and is not retried by a timer.
    assert await jobs.claim(db, ws, "old-worker", [JobType.VALUATION], 60) is None
    with pytest.raises(ValidationFailed):
        await jobs.claim(db, ws, "w", [JobType.VALUATION, JobType.DETAIL], 60, payload_versions=understood)
    # Explicit operator action after deploying a compatible worker.
    owner = member(ws, Role.OWNER)
    async with db.transaction(owner) as conn:
        unblocked = await jobs.unblock(conn, owner, newer, reason="compatible worker deployed")
    assert unblocked.state == JobState.QUEUED
    upgraded = await jobs.claim(db, ws, "new-worker", [JobType.VALUATION], 60, payload_versions={JobType.VALUATION: {1, 2}})
    assert upgraded is not None and upgraded.id == newer and upgraded.payload_version == 2


async def test_cancel_only_from_queued_or_retry_wait_and_scoped(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    reviewer = member(ws, Role.REVIEWER)
    viewer = member(ws, Role.VIEWER)
    async with db.transaction(reviewer) as conn:
        recheck, _ = await jobs.enqueue(
            conn,
            reviewer,
            spec(JobType.RECHECK, listing_id=world_a.listing_id, generation=world_a.generation),
        )
    valuation = await _enqueue(db, ws)
    async with db.transaction(viewer) as conn:
        with pytest.raises(Forbidden):
            await jobs.cancel(conn, viewer, recheck)
    with pytest.raises(Forbidden):
        async with db.transaction(reviewer) as conn:
            await jobs.cancel(conn, reviewer, valuation)  # system job type
    async with db.transaction(reviewer) as conn:
        cancelled = await jobs.cancel(conn, reviewer, recheck, reason="owner changed mind")
    assert cancelled.state == JobState.CANCELLED and cancelled.completed_at is not None
    assert seed.scalar(
        "select count(*) from ops.audit_events where target_id = %s and action = 'job.cancel'", (recheck,)
    ) == 1
    running = await jobs.claim(db, ws, "w", [JobType.VALUATION], 60)
    assert running is not None
    with pytest.raises(VersionConflict):
        async with db.transaction(system(ws)) as conn:
            await jobs.cancel(conn, system(ws), valuation)
    with pytest.raises(NotFound):
        async with db.transaction(system(ws)) as conn:
            await jobs.cancel(conn, system(ws), uuid.uuid4())


async def test_enqueue_scopes(db: Database, world_a: World) -> None:
    ws = world_a.workspace_id
    reviewer = member(ws, Role.REVIEWER)
    viewer = member(ws, Role.VIEWER)
    async with db.transaction(reviewer) as conn:
        with pytest.raises(Forbidden):
            await jobs.enqueue(conn, reviewer, spec(JobType.DISCOVERY))
    async with db.transaction(viewer) as conn:
        with pytest.raises(Forbidden):
            await jobs.enqueue(
                conn,
                viewer,
                spec(JobType.RECHECK, listing_id=world_a.listing_id, generation=world_a.generation),
            )
    with pytest.raises(ValueError):
        spec(JobType.DETAIL)  # detail jobs need their listing/generation binding


async def test_queue_stats_report_depth_age_and_expired_leases(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    await _enqueue(db, ws)
    await _enqueue(db, ws, job_type=JobType.COMPARABLES)
    running_id = await _enqueue(db, ws, priority=999)
    job = await jobs.claim(db, ws, "w", [JobType.VALUATION], 60)
    assert job is not None and job.id == running_id
    expire_job_lease(seed, running_id)
    viewer = member(ws, Role.VIEWER)
    async with db.transaction(viewer) as conn:
        stats = await jobs.queue_stats(conn, viewer)
    assert stats.depth[(JobType.VALUATION, JobState.QUEUED)] == 1
    assert stats.depth[(JobType.VALUATION, JobState.RUNNING)] == 1
    assert stats.depth[(JobType.COMPARABLES, JobState.QUEUED)] == 1
    assert stats.expired_leases == 1
    assert stats.oldest_due_age[JobType.VALUATION] >= timedelta(0)
    async with db.transaction(viewer) as conn:
        record = await jobs.get_job(conn, viewer, running_id)
    assert record.state == JobState.RUNNING
