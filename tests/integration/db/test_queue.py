"""Durable job queue behaviour on real PostgreSQL (spec 13; spec 31 Queue row).

Uses the spec section 13 claim statement verbatim (parameters bound) as
suv_backend under workspace RLS:
- two concurrent workers never claim the same job;
- claims skip future, exhausted and other-workspace jobs and honour priority;
- completion is fenced by job id + running state + lease owner/token + an
  unexpired lease measured with clock_timestamp();
- the reaper requeues expired leases while attempts remain, otherwise
  dead-letters, always clearing the old lease;
- a worker that crashes before commit leaves the job claimable (crash after
  commit is the expired-lease/reaper path);
- a worker whose guarded completion fails rolls back its domain writes;
- two schedulers racing for one slot (spec 9) create exactly one job.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID

import psycopg
import pytest
from tests.integration.db.helpers import Seed, World, backend, unique

pytestmark = pytest.mark.db

# Spec section 13 claim, with named bound parameters.
CLAIM_SQL = """
with picked as (
  select id
  from ops.jobs
  where state in ('queued','retry_wait')
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
returning j.id, j.lease_token
"""

COMPLETE_SQL = """
update ops.jobs
set state = 'succeeded', completed_at = clock_timestamp(),
    result_reference = %(result)s::jsonb
where id = %(job_id)s
  and state = 'running'
  and lease_token = %(lease_token)s
  and lease_owner = %(worker_id)s
  and lease_expires_at > clock_timestamp()
"""

REAPER_SQL = """
update ops.jobs
set state = case when attempts < max_attempts then 'retry_wait' else 'dead_letter' end,
    completed_at = case when attempts < max_attempts then null else clock_timestamp() end,
    available_at = case when attempts < max_attempts then clock_timestamp() + interval '30 seconds'
                        else available_at end,
    lease_owner = null, lease_token = null, lease_expires_at = null,
    last_error_code = 'LEASE_EXPIRED'
where state = 'running'
  and lease_expires_at <= clock_timestamp()
returning id, state
"""


def claim(
    conn: psycopg.Connection, workspace_id: UUID, worker: str, lease: str = "5 minutes"
) -> tuple[UUID, UUID] | None:
    with backend(conn, workspace_id):
        row = conn.execute(
            CLAIM_SQL, {"worker_id": worker, "fresh_uuid": uuid.uuid4(), "lease_duration": lease}
        ).fetchone()
    return None if row is None else (row[0], row[1])


@contextmanager
def connections(url: str, count: int) -> Iterator[list[psycopg.Connection]]:
    conns = [psycopg.connect(url, autocommit=True) for _ in range(count)]
    try:
        yield conns
    finally:
        for c in conns:
            c.close()


def test_two_concurrent_workers_never_claim_the_same_job(db_url: str, seed: Seed, world_a: World) -> None:
    ws = world_a.workspace_id
    job_ids = {seed.job(ws, dedup_key=unique("concurrent")) for _ in range(40)}
    claimed: dict[str, list[UUID]] = {"worker-a": [], "worker-b": []}
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def work(conn: psycopg.Connection, worker: str) -> None:
        try:
            barrier.wait(timeout=10)
            while True:
                with backend(conn, ws):
                    row = conn.execute(
                        CLAIM_SQL,
                        {"worker_id": worker, "fresh_uuid": uuid.uuid4(), "lease_duration": "5 minutes"},
                    ).fetchone()
                    # Hold the row lock briefly to force real contention.
                    conn.execute("select pg_sleep(0.002)")
                if row is None:
                    return
                claimed[worker].append(row[0])
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    with connections(db_url, 2) as (conn_a, conn_b):
        threads = [
            threading.Thread(target=work, args=(conn_a, "worker-a")),
            threading.Thread(target=work, args=(conn_b, "worker-b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
    assert not errors, errors
    a, b = claimed["worker-a"], claimed["worker-b"]
    assert len(a) == len(set(a)) and len(b) == len(set(b))
    assert set(a).isdisjoint(b), "a job was claimed twice"
    assert set(a) | set(b) == job_ids
    assert a and b, "both workers should have obtained work under contention"
    states = seed.conn.execute(
        "select state, attempts, count(*) from ops.jobs where workspace_id = %s group by 1, 2", (ws,)
    ).fetchall()
    assert states == [("running", 1, 40)]


def test_claim_skips_future_exhausted_and_foreign_jobs_and_honours_priority(
    db_conn: psycopg.Connection, seed: Seed, world_a: World, world_b: World
) -> None:
    ws = world_a.workspace_id
    seed.job(ws, available_at=seed.scalar("select now() + interval '1 hour'"))
    seed.job(ws, attempts=5, max_attempts=5, state="retry_wait")
    seed.job(world_b.workspace_id, priority=1000)
    low = seed.job(ws, priority=1)
    high = seed.job(ws, priority=10)
    first = claim(db_conn, ws, "worker-1")
    second = claim(db_conn, ws, "worker-1")
    assert first is not None and first[0] == high
    assert second is not None and second[0] == low
    assert claim(db_conn, ws, "worker-1") is None
    # The other workspace's job is untouched.
    assert (
        seed.scalar("select state from ops.jobs where workspace_id = %s", (world_b.workspace_id,)) == "queued"
    )


def test_completion_requires_current_unexpired_lease(
    db_conn: psycopg.Connection, seed: Seed, world_a: World
) -> None:
    ws = world_a.workspace_id
    job = seed.job(ws)
    claimed = claim(db_conn, ws, "worker-1")
    assert claimed is not None and claimed[0] == job
    _, token = claimed
    params = {"job_id": job, "lease_token": token, "worker_id": "worker-1", "result": '{"synthetic": true}'}
    with backend(db_conn, ws):
        assert db_conn.execute(COMPLETE_SQL, {**params, "lease_token": uuid.uuid4()}).rowcount == 0
        assert db_conn.execute(COMPLETE_SQL, {**params, "worker_id": "worker-2"}).rowcount == 0
    with backend(db_conn, world_a.workspace_id):
        assert db_conn.execute(COMPLETE_SQL, params).rowcount == 1
    # A late duplicate completion cannot overwrite the committed result (fencing).
    with backend(db_conn, ws):
        assert db_conn.execute(COMPLETE_SQL, {**params, "result": '{"late": true}'}).rowcount == 0
    assert seed.scalar("select result_reference from ops.jobs where id = %s", (job,)) == {"synthetic": True}


def test_expired_lease_cannot_complete_and_reaper_recovers(
    db_conn: psycopg.Connection, seed: Seed, world_a: World
) -> None:
    ws = world_a.workspace_id
    retry_job = seed.job(ws, max_attempts=3, priority=5)
    final_job = seed.job(ws, max_attempts=1, priority=1)
    first = claim(db_conn, ws, "worker-1", lease="1 millisecond")
    second = claim(db_conn, ws, "worker-1", lease="1 millisecond")
    assert first is not None and second is not None
    assert {first[0], second[0]} == {retry_job, final_job}
    db_conn.execute("select pg_sleep(0.01)")
    with backend(db_conn, ws):
        late = db_conn.execute(
            COMPLETE_SQL,
            {"job_id": first[0], "lease_token": first[1], "worker_id": "worker-1", "result": "{}"},
        )
        assert late.rowcount == 0
    with backend(db_conn, ws):
        reaped: dict[UUID, str] = dict(db_conn.execute(REAPER_SQL).fetchall())
    assert reaped == {retry_job: "retry_wait", final_job: "dead_letter"}
    rows = seed.conn.execute(
        "select id, lease_token, lease_owner, attempts, completed_at is not null from ops.jobs"
        " where id = any(%s) order by priority desc",
        ([retry_job, final_job],),
    ).fetchall()
    assert rows == [(retry_job, None, None, 1, False), (final_job, None, None, 1, True)]
    # A recovered job gets a fresh token on its next claim.
    seed.conn.execute("update ops.jobs set available_at = now() where id = %s", (retry_job,))
    again = claim(db_conn, ws, "worker-2")
    assert again is not None and again[0] == retry_job and again[1] != first[1]
    assert seed.scalar("select attempts from ops.jobs where id = %s", (retry_job,)) == 2


def test_exhausted_waiting_jobs_are_visible_for_dead_letter_reconciliation(
    db_conn: psycopg.Connection, seed: Seed, world_a: World
) -> None:
    ws = world_a.workspace_id
    exhausted = seed.job(ws, state="retry_wait", attempts=2, max_attempts=2)
    with backend(db_conn, ws):
        rows = db_conn.execute(
            "update ops.jobs set state = 'dead_letter', completed_at = clock_timestamp(),"
            " last_error_code = 'ATTEMPTS_EXHAUSTED'"
            " where state in ('queued', 'retry_wait') and attempts >= max_attempts returning id"
        ).fetchall()
    assert rows == [(exhausted,)]


def test_heartbeat_extends_only_the_current_lease(
    db_conn: psycopg.Connection, seed: Seed, world_a: World
) -> None:
    ws = world_a.workspace_id
    job = seed.job(ws)
    claimed = claim(db_conn, ws, "worker-1", lease="2 minutes")
    assert claimed is not None
    heartbeat = (
        "update ops.jobs set lease_expires_at = clock_timestamp() + interval '5 minutes',"
        " last_heartbeat_at = clock_timestamp()"
        " where id = %s and state = 'running' and lease_token = %s and lease_owner = %s"
        " and lease_expires_at > clock_timestamp()"
    )
    with backend(db_conn, ws):
        assert db_conn.execute(heartbeat, (job, uuid.uuid4(), "worker-1")).rowcount == 0
        assert db_conn.execute(heartbeat, (job, claimed[1], "worker-1")).rowcount == 1
    remaining = seed.scalar("select lease_expires_at - now() from ops.jobs where id = %s", (job,))
    assert remaining > timedelta(minutes=4)


def test_worker_crash_before_commit_leaves_the_job_claimable(
    db_url: str, db_conn: psycopg.Connection, seed: Seed, world_a: World
) -> None:
    """Crash before commit (spec 31 Queue): the claim rolls back with the dead session."""
    ws = world_a.workspace_id
    job = seed.job(ws)
    crashed = psycopg.connect(db_url)  # not autocommit: the claim stays uncommitted
    try:
        crashed.execute("set local role suv_backend")
        crashed.execute("select set_config('app.workspace_id', %s, true)", (str(ws),))
        row = crashed.execute(
            CLAIM_SQL,
            {"worker_id": "worker-crash", "fresh_uuid": uuid.uuid4(), "lease_duration": "5 minutes"},
        ).fetchone()
        assert row is not None and row[0] == job
        pid = crashed.info.backend_pid
        # While the crashed worker holds the row lock, another worker skips it (SKIP LOCKED).
        assert claim(db_conn, ws, "worker-2") is None
        db_conn.execute("select pg_terminate_backend(%s)", (pid,))
    finally:
        crashed.close()
    again = claim(db_conn, ws, "worker-2")
    assert again is not None and again[0] == job
    assert seed.scalar("select attempts from ops.jobs where id = %s", (job,)) == 1


def test_lost_lease_rolls_back_the_workers_domain_writes(
    db_conn: psycopg.Connection, seed: Seed, world_a: World
) -> None:
    """A worker whose guarded completion updates 0 rows must commit nothing (spec 13 fencing)."""
    ws = world_a.workspace_id
    job = seed.job(ws)
    claimed = claim(db_conn, ws, "worker-1", lease="1 millisecond")
    assert claimed is not None and claimed[0] == job
    db_conn.execute("select pg_sleep(0.01)")
    marker = unique("domain-write")

    class LeaseLost(Exception):
        pass

    with pytest.raises(LeaseLost), backend(db_conn, ws):
        db_conn.execute("select id from ops.jobs where id = %s for update", (job,))
        db_conn.execute(
            "insert into ops.audit_events (workspace_id, actor_principal_id, actor_kind, action,"
            " target_type, target_id, request_id)"
            " values (%s, %s, 'system', 'listing.promote', 'job', %s, %s)",
            (ws, uuid.uuid4(), job, marker),
        )
        done = db_conn.execute(
            COMPLETE_SQL,
            {"job_id": job, "lease_token": claimed[1], "worker_id": "worker-1", "result": "{}"},
        )
        if done.rowcount == 0:
            raise LeaseLost
    assert seed.scalar("select count(*) from ops.audit_events where request_id = %s", (marker,)) == 0
    assert seed.scalar("select state from ops.jobs where id = %s", (job,)) == "running"


def test_concurrent_schedulers_create_one_discovery_job_per_slot(
    db_url: str, seed: Seed, world_a: World
) -> None:
    """Duplicate schedulers (spec 9, 31): the slot key admits exactly one job under a real race."""
    ws = world_a.workspace_id
    slot = seed.scalar("select date_bin('15 minutes', now(), timestamptz '2026-01-01T00:00:00Z')")
    insert = (
        "insert into ops.jobs (workspace_id, job_type, dedup_key, source_id, profile_id, partition_key,"
        " scheduled_slot) values (%(ws)s, 'discovery', %(key)s, %(src)s, %(prof)s, 'default', %(slot)s)"
        " on conflict do nothing returning id"
    )
    results: dict[str, UUID | None] = {}
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def schedule(conn: psycopg.Connection, name: str, hold: float) -> None:
        try:
            barrier.wait(timeout=10)
            with backend(conn, ws):
                row = conn.execute(
                    insert,
                    {
                        "ws": ws,
                        "key": f"discovery:{name}:{slot.isoformat()}",
                        "src": world_a.source_id,
                        "prof": world_a.profile_id,
                        "slot": slot,
                    },
                ).fetchone()
                conn.execute("select pg_sleep(%s)", (hold,))
            results[name] = None if row is None else row[0]
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    with connections(db_url, 2) as (conn_a, conn_b):
        threads = [
            threading.Thread(target=schedule, args=(conn_a, "scheduler-a", 0.2)),
            threading.Thread(target=schedule, args=(conn_b, "scheduler-b", 0.2)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    assert not errors, errors
    inserted = [job_id for job_id in results.values() if job_id is not None]
    assert len(results) == 2 and len(inserted) == 1, results
    assert (
        seed.scalar(
            "select count(*) from ops.jobs where workspace_id = %s and scheduled_slot = %s", (ws, slot)
        )
        == 1
    )
