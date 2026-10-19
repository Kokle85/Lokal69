"""The owner's source resume (`sources_repo.resume_source`, `jobs.requeue_source_paused`).

A resume clears the pause and, in the same transaction, requeues exactly this source's jobs that a
worker blocked as ``source_paused`` (the guards of `jobs.unblock`); every other blocker, every
other source's job and every other workspace's job stays as it is. A resume of a source that is
not paused is a no-op; a resume never enables a source and never changes its technical status.
SYNTHETIC data only.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed, unique
from tests.integration.persistence_core.support import member
from tests.integration.repos_sources_listings.support import HOST, Env, source_config

from suv_deals.domain.enums import JobState, JobType, Role, Scope, TechnicalStatus
from suv_deals.errors import Forbidden, IdempotencyConflict, NotFound, SourcePaused, VersionConflict
from suv_deals.persistence import jobs, sources_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.sources_repo import SourceRoute
from suv_deals.persistence.transactions import lock_source, unit_of_work

pytestmark = pytest.mark.db


def _job(seed: Seed, job_id: UUID) -> dict[str, Any]:
    cur = seed.conn.execute(
        "select state, blocker_code, blocker_detail, attempts, max_attempts, available_at, lease_owner,"
        " completed_at, available_at <= clock_timestamp() as due from ops.jobs where id = %s",
        (job_id,),
    )
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


def _blocked(
    seed: Seed, ws: UUID, source_id: UUID | None, blocker: str = "source_paused", **cols: Any
) -> UUID:
    values: dict[str, Any] = {
        "job_type": JobType.DISCOVERY.value,
        "state": "blocked",
        "blocker_code": blocker,
        "blocker_detail": f"SYNTHETIC {blocker}",
        "attempts": 1,
        "source_id": source_id,
    }
    values.update(cols)
    return seed.job(ws, **values)


def _source_row(seed: Seed, source_id: UUID) -> dict[str, Any]:
    cur = seed.conn.execute(
        "select enabled, paused, technical_status, version from app.sources where id = %s", (source_id,)
    )
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


async def _pause(db: Database, env: Env, source_id: UUID, version: int) -> int:
    async with unit_of_work(db, env.owner) as conn:
        result = await sources_repo.pause_source(
            conn, env.owner, source_id, version, "SYNTHETIC pause", f"pause-{uuid.uuid4().hex[:12]}"
        )
    return result.version


async def _second_source(db: Database, env: Env) -> UUID:
    """Another enabled source of the same workspace (the sync lists both: a source missing from the
    YAML would be disabled)."""
    key = unique("src2").lower()
    async with unit_of_work(db, env.system) as conn:
        await sources_repo.sync_sources_from_yaml(
            conn, env.system, [source_config(env.source_key), source_config(key)]
        )
        return (await sources_repo.get_source_by_key(conn, env.system, key)).id


def _audits(seed: Seed, ws: UUID, action: str) -> list[dict[str, Any]]:
    rows = seed.conn.execute(
        "select target_id, reason, metadata from ops.audit_events where workspace_id = %s and action = %s"
        " order by occurred_at, id",
        (ws, action),
    ).fetchall()
    return [{"target_id": r[0], "reason": r[1], "metadata": r[2]} for r in rows]


async def test_resume_requeues_only_this_sources_source_paused_jobs(
    db: Database, env: Env, env_b: Env, seed: Seed
) -> None:
    ws = env.workspace_id
    other_source = await _second_source(db, env)
    version = await _pause(db, env, env.source_id, _source_row(seed, env.source_id)["version"])
    # This source's jobs a worker blocked while it was paused: one used up all its attempts.
    exhausted = _blocked(seed, ws, env.source_id, attempts=5, max_attempts=5)
    plain = _blocked(seed, ws, env.source_id, attempts=1, max_attempts=5)
    # Never touched: other blockers of this source, a waiting job, other sources and workspaces.
    untouched = {
        "incompatible": _blocked(seed, ws, env.source_id, "incompatible_payload_version"),
        "uncertain": _blocked(seed, ws, env.source_id, "EMAIL_DELIVERY_UNCERTAIN"),
        "host_block": _blocked(seed, ws, env.source_id, "host_access_blocked"),
        "queued": seed.job(ws, job_type=JobType.DISCOVERY.value, source_id=env.source_id),
        "sourceless": _blocked(seed, ws, None, job_type=JobType.VALUATION.value),
        "other_source": _blocked(seed, ws, other_source),
        "other_workspace": _blocked(seed, env_b.workspace_id, env_b.source_id),
    }
    before = {name: _job(seed, job_id) for name, job_id in untouched.items()}

    async with unit_of_work(db, env.owner) as conn:
        result = await sources_repo.resume_source(
            conn, env.owner, env.source_id, version, reason="SYNTHETIC resume after maintenance"
        )

    assert result.was_paused is True and result.paused is False
    assert result.requeued_jobs == 2 and result.version == version + 1
    assert result.enabled is True and result.network_allowed is True
    assert result.technical_status == TechnicalStatus.FIXTURE_TESTED
    for job_id, attempts in ((exhausted, 4), (plain, 1)):
        job = _job(seed, job_id)
        assert job["state"] == "queued" and job["due"] is True
        assert job["blocker_code"] is None and job["blocker_detail"] is None
        assert job["lease_owner"] is None and job["completed_at"] is None
        assert job["attempts"] == attempts  # at least one attempt left, as `jobs.unblock` does
    assert {name: _job(seed, job_id) for name, job_id in untouched.items()} == before
    assert _source_row(seed, env.source_id)["paused"] is False

    [resume_audit] = _audits(seed, ws, "source.resume")
    assert resume_audit["target_id"] == env.source_id
    assert resume_audit["reason"] == "SYNTHETIC resume after maintenance"
    meta = resume_audit["metadata"]
    assert meta["requeued_jobs"] == 2 and sorted(meta["requeued_job_ids"]) == sorted(
        [str(exhausted), str(plain)]
    )
    assert meta["enabled"] is True and meta["technical_status"] == "fixture_tested"
    unblocks = _audits(seed, ws, "job.unblock")
    assert {a["target_id"] for a in unblocks} == {exhausted, plain}
    for audit in unblocks:
        assert audit["metadata"]["via"] == "source.resume"
        assert audit["metadata"]["blocker_code"] == "source_paused"
        assert audit["reason"].startswith("source resumed: ")
    by_job = {a["target_id"]: a["metadata"] for a in unblocks}
    assert (by_job[exhausted]["attempts_before"], by_job[exhausted]["attempts_after"]) == (5, 4)
    # The requeued jobs are claimable again (the network work they waited for may now run).
    claimed: set[UUID] = set()
    for _ in range(5):
        job = await jobs.claim(db, ws, "worker-resume", [JobType.DISCOVERY], 60)
        if job is None:
            break
        claimed.add(job.id)
    assert {exhausted, plain} <= claimed


async def test_resume_of_a_source_that_is_not_paused_is_a_no_op(db: Database, env: Env, seed: Seed) -> None:
    ws = env.workspace_id
    version = _source_row(seed, env.source_id)["version"]
    # A job blocked while the source was gated (also `source_paused`) is not this resume's business.
    gated = _blocked(seed, ws, env.source_id)
    before = _job(seed, gated)
    async with unit_of_work(db, env.owner) as conn:
        result = await sources_repo.resume_source(
            conn, env.owner, env.source_id, version, reason="SYNTHETIC resume of a running source"
        )
    assert result.was_paused is False and result.requeued_jobs == 0
    assert result.version == version and result.paused is False and result.network_allowed is True
    assert _job(seed, gated) == before
    assert _source_row(seed, env.source_id)["version"] == version
    assert _audits(seed, ws, "source.resume") == [] and _audits(seed, ws, "job.unblock") == []


async def test_resume_never_enables_a_disabled_source(db: Database, env: Env, seed: Seed) -> None:
    ws = env.workspace_id
    version = await _pause(db, env, env.source_id, _source_row(seed, env.source_id)["version"])
    # The configuration switched the source off while it was paused (a YAML revision + sync).
    seed.conn.execute(
        "update app.sources set enabled = false, version = version + 1 where id = %s", (env.source_id,)
    )
    version += 1
    job = _blocked(seed, ws, env.source_id)
    async with unit_of_work(db, env.owner) as conn:
        result = await sources_repo.resume_source(conn, env.owner, env.source_id, version, reason="SYNTHETIC")
    assert result.was_paused is True and result.enabled is False and result.network_allowed is False
    row = _source_row(seed, env.source_id)
    assert row["enabled"] is False and row["paused"] is False
    # Requeued like any `source_paused` job; the worker blocks it again before any request.
    assert _job(seed, job)["state"] == "queued"
    async with unit_of_work(db, env.system) as conn:
        with pytest.raises(SourcePaused):
            await lock_source(conn, ws, env.source_id, for_network=True)


async def test_resume_never_clears_access_blocked(db: Database, env: Env, seed: Seed) -> None:
    route = SourceRoute(host=HOST, purpose="detail", path="/vehicles/x")
    async with unit_of_work(db, env.system) as conn:
        blocked = await sources_repo.record_access_block(conn, env.system, env.source_id, route, "HTTP 403")
    version = await _pause(db, env, env.source_id, blocked.source_version)
    async with unit_of_work(db, env.owner) as conn:
        result = await sources_repo.resume_source(conn, env.owner, env.source_id, version, reason="SYNTHETIC")
    assert result.technical_status == TechnicalStatus.ACCESS_BLOCKED
    assert result.enabled is False and result.network_allowed is False
    row = _source_row(seed, env.source_id)
    assert row["technical_status"] == "access_blocked" and row["enabled"] is False and row["paused"] is False
    gate = seed.scalar(
        "select status from ops.activation_gates where workspace_id = %s and capability = %s",
        (env.workspace_id, sources_repo.access_gate_capability(env.source_key)),
    )
    assert gate == "blocked"  # the operational review item stays open


async def test_resume_scope_version_and_idempotency(db: Database, env: Env, env_b: Env, seed: Seed) -> None:
    other = await _second_source(db, env)
    version = await _pause(db, env, env.source_id, _source_row(seed, env.source_id)["version"])
    pause_only = member(
        env.workspace_id, Role.OWNER, scopes=frozenset({Scope.SOURCES_PAUSE, Scope.DEALS_READ})
    )
    for actor in (pause_only, member(env.workspace_id, Role.REVIEWER)):
        async with unit_of_work(db, actor) as conn:
            with pytest.raises(Forbidden):
                await sources_repo.resume_source(conn, actor, env.source_id, version, reason="SYNTHETIC")
    async with unit_of_work(db, env.owner) as conn:
        with pytest.raises(VersionConflict) as stale:
            await sources_repo.resume_source(conn, env.owner, env.source_id, version - 1, reason="SYNTHETIC")
    assert stale.value.details == {"current_version": version}
    async with unit_of_work(db, env_b.owner) as conn:  # another workspace's source is missing
        with pytest.raises(NotFound):
            await sources_repo.resume_source(conn, env_b.owner, env.source_id, version, reason="SYNTHETIC")
    assert _source_row(seed, env.source_id)["paused"] is True

    _blocked(seed, env.workspace_id, env.source_id)
    key = f"resume-{uuid.uuid4().hex[:12]}"
    async with unit_of_work(db, env.owner) as conn:
        first = await sources_repo.resume_source(
            conn, env.owner, env.source_id, version, reason="SYNTHETIC resume", idempotency_key=key
        )
    assert first.requeued_jobs == 1
    _blocked(seed, env.workspace_id, env.source_id)  # a later block is not part of the replay
    async with unit_of_work(db, env.owner) as conn:
        replay = await sources_repo.resume_source(
            conn, env.owner, env.source_id, version, reason="SYNTHETIC resume", idempotency_key=key
        )
    assert replay == first
    async with unit_of_work(db, env.owner) as conn:
        with pytest.raises(IdempotencyConflict):  # same key, different request
            await sources_repo.resume_source(
                conn, env.owner, env.source_id, version, reason="another reason", idempotency_key=key
            )
    other_version = await _pause(db, env, other, _source_row(seed, other)["version"])
    async with unit_of_work(db, env.owner) as conn:
        with pytest.raises(IdempotencyConflict):  # the key is bound to the source it resumed
            await sources_repo.resume_source(
                conn, env.owner, other, other_version, reason="SYNTHETIC resume", idempotency_key=key
            )
    assert _source_row(seed, other)["paused"] is True
    assert len(_audits(seed, env.workspace_id, "source.resume")) == 1


async def test_requeue_has_the_unblock_scope_guard(db: Database, env: Env, seed: Seed) -> None:
    job = _blocked(seed, env.workspace_id, env.source_id)
    reviewer = member(env.workspace_id, Role.REVIEWER)
    async with unit_of_work(db, reviewer) as conn:
        with pytest.raises(Forbidden):
            await jobs.requeue_source_paused(
                conn, reviewer, env.source_id, reason="SYNTHETIC", source_key="x"
            )
    assert _job(seed, job)["state"] == "blocked"
    async with unit_of_work(db, env.system) as conn:
        requeued = await jobs.requeue_source_paused(
            conn, env.system, env.source_id, reason="SYNTHETIC system requeue", source_key=env.source_key
        )
    assert requeued == (job,)
    assert _job(seed, job)["state"] == JobState.QUEUED.value
