"""Job runner (spec 13, 30): payload-version guard, error mapping, lease loss, extension point, stop.

Handlers here are SYNTHETIC test doubles registered through the public `HandlerRegistry`; the
queue, leases and fencing are the real PostgreSQL ones.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from tests.integration.pipeline.support import PipelineEnv, run

from suv_deals.crawling.policy_client import BudgetRefused
from suv_deals.crawling.rate_limits import Deny, DenyReason, Wait, WaitReason
from suv_deals.domain.enums import JobState, JobType
from suv_deals.errors import DependencyUnavailable, SourcePaused, ValidationFailed, VersionConflict
from suv_deals.persistence import jobs
from suv_deals.persistence.database import Conn
from suv_deals.persistence.errors_map import LeaseLost
from suv_deals.persistence.transactions import job_unit_of_work
from suv_deals.workers.handlers import HandlerRegistry, default_registry
from suv_deals.workers.runner import Worker
from suv_deals.workers.runtime import (
    Disposition,
    JobExecution,
    JobOutcome,
    RuntimeContext,
    RuntimeOptions,
    apply_disposition,
)

pytestmark = pytest.mark.db

Handler = Callable[[RuntimeContext, JobExecution], Awaitable[JobOutcome]]


async def enqueue(env: PipelineEnv, job_type: JobType = JobType.VALUATION, **kwargs: object) -> uuid.UUID:
    async def go(conn: Conn) -> uuid.UUID:
        job_id, _ = await jobs.enqueue(
            conn,
            env.system,
            jobs.JobSpec(job_type=job_type, dedup_key=f"{job_type.value}:{uuid.uuid4()}", **kwargs),
        )
        return job_id

    return await run(env.ctx, env.system, go)


def registry_with(handler: Handler, job_type: JobType = JobType.VALUATION) -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register(job_type, handler, description="synthetic test handler")
    return registry


def worker(env: PipelineEnv, registry: HandlerRegistry | None = None, **kwargs: object) -> Worker:
    return Worker(env.ctx, registry, workspace_ids=[env.workspace_id], worker_id="worker-runner", **kwargs)  # type: ignore[arg-type]


def job_row(env: PipelineEnv, job_id: uuid.UUID) -> dict[str, object]:
    [row] = env.rows(
        "select state, attempts, last_error_code, blocker_code, result_reference from ops.jobs where id = %s",
        job_id,
    )
    return row


async def succeed(ctx: RuntimeContext, execution: JobExecution) -> JobOutcome:
    async with job_unit_of_work(ctx.db, execution.job) as (conn, _locked):
        state = await apply_disposition(conn, execution.job, Disposition.complete({"synthetic": True}))
    return JobOutcome(state=state)


async def test_incompatible_payload_version_is_blocked_not_retried(env: PipelineEnv) -> None:
    job_id = await enqueue(env, payload_version=2)
    assert await worker(env, registry_with(succeed)).run_until_idle() == []
    row = job_row(env, job_id)
    assert row["state"] == "blocked" and row["blocker_code"] == "incompatible_payload_version"
    assert row["attempts"] == 0  # the refused claim does not consume an attempt


@pytest.mark.parametrize(
    ("error", "state", "code"),
    [
        (ValidationFailed("synthetic invalid payload"), "dead_letter", "VALIDATION_ERROR"),
        (DependencyUnavailable("synthetic outage"), "retry_wait", "DEPENDENCY_UNAVAILABLE"),
        (SourcePaused("synthetic pause"), "blocked", "source_paused"),
        (VersionConflict("synthetic concurrent change"), "retry_wait", "VERSION_CONFLICT"),
        (RuntimeError("synthetic bug"), "retry_wait", "UNEXPECTED_ERROR"),
    ],
)
async def test_escaping_errors_map_to_typed_outcomes(
    env: PipelineEnv, error: Exception, state: str, code: str
) -> None:
    async def failing(ctx: RuntimeContext, execution: JobExecution) -> JobOutcome:
        del ctx, execution
        raise error

    job_id = await enqueue(env)
    [report] = await worker(env, registry_with(failing)).run_until_idle()
    assert report.state == state and report.code == code
    row = job_row(env, job_id)
    assert row["state"] == state
    assert (row["blocker_code"] if state == "blocked" else row["last_error_code"]) == code


@pytest.mark.parametrize(
    ("decision", "code"),
    [
        (Deny(reason=DenyReason.RUN_CAP_REACHED), "RUN_CAP_REACHED"),
        (
            Deny(reason=DenyReason.BUDGET_EXHAUSTED, until=datetime(2099, 1, 1, tzinfo=UTC)),
            "DAILY_BUDGET_EXHAUSTED",
        ),
        (Deny(reason=DenyReason.CIRCUIT_OPEN, until=datetime(2099, 1, 1, tzinfo=UTC)), "CIRCUIT_OPEN"),
        (
            Wait(reason=WaitReason.RETRY_AFTER, until=datetime(2099, 1, 1, tzinfo=UTC)),
            "BUDGET_WAIT_RETRY_AFTER",
        ),
    ],
)
async def test_budget_refusals_release_the_job_without_consuming_an_attempt(
    env: PipelineEnv, decision: Deny | Wait, code: str
) -> None:
    """Nothing was fetched: the job goes back to ``queued`` with its attempts unchanged, at the
    gate's time, audited; repeated refusals can never exhaust it into a dead letter."""

    async def refused(ctx: RuntimeContext, execution: JobExecution) -> JobOutcome:
        del ctx
        raise BudgetRefused(decision, now=datetime.now(UTC) - timedelta(seconds=1))

    job_id = await enqueue(env, max_attempts=1)
    started = datetime.now(UTC)
    [report] = await worker(env, registry_with(refused)).run_until_idle()
    assert report.state == JobState.QUEUED and report.code == code
    [row] = env.rows(
        "select state, attempts, last_error_code, available_at, lease_token from ops.jobs where id = %s",
        job_id,
    )
    assert row["state"] == "queued" and row["attempts"] == 0 and row["last_error_code"] == code
    assert row["lease_token"] is None and row["available_at"] >= started
    audited = env.scalar(
        "select count(*) from ops.audit_events where workspace_id = %s and action = 'job.release'"
        " and target_id = %s",
        env.workspace_id,
        job_id,
    )
    assert audited == 1
    # Another refusal of the same (single-attempt) job still does not dead-letter it.
    env.seed.conn.execute(
        "update ops.jobs set available_at = now() - interval '1 second' where id = %s", (job_id,)
    )
    [again] = await worker(env, registry_with(refused)).run_until_idle()
    assert again.state == JobState.QUEUED
    assert job_row(env, job_id)["attempts"] == 0 and job_row(env, job_id)["state"] == "queued"


async def test_an_access_blocked_host_still_blocks_the_job(env: PipelineEnv) -> None:
    async def blocked(ctx: RuntimeContext, execution: JobExecution) -> JobOutcome:
        del ctx
        raise BudgetRefused(Deny(reason=DenyReason.ACCESS_BLOCKED), now=datetime.now(UTC))

    job_id = await enqueue(env)
    [report] = await worker(env, registry_with(blocked)).run_until_idle()
    assert report.state == JobState.BLOCKED and report.code == "host_access_blocked"
    row = job_row(env, job_id)
    assert row["state"] == "blocked" and row["blocker_code"] == "host_access_blocked"


async def test_release_is_fenced_by_the_lease(env: PipelineEnv) -> None:
    job_id = await enqueue(env)
    claimed = await jobs.claim(env.ctx.db, env.workspace_id, "worker-release", [JobType.VALUATION], 30)
    assert claimed is not None and claimed.id == job_id and claimed.attempts == 1
    with pytest.raises(ValidationFailed):
        async with job_unit_of_work(env.ctx.db, claimed) as (conn, _locked):
            await jobs.release(conn, claimed, available_at=timedelta(seconds=-1), code="SYNTHETIC")
    stolen = claimed.model_copy(update={"lease_token": uuid.uuid4()})
    with pytest.raises(LeaseLost):
        async with job_unit_of_work(env.ctx.db, claimed) as (conn, _locked):
            await jobs.release(conn, stolen, available_at=timedelta(minutes=5), code="SYNTHETIC_REFUSAL")
    assert job_row(env, job_id)["state"] == "running"
    async with job_unit_of_work(env.ctx.db, claimed) as (conn, _locked):
        released = await jobs.release(
            conn, claimed, available_at=timedelta(minutes=5), code="SYNTHETIC_REFUSAL"
        )
    assert released.state == JobState.QUEUED and released.attempts == 0 and released.lease_token is None
    row = job_row(env, job_id)
    assert row["state"] == "queued" and row["attempts"] == 0 and row["last_error_code"] == "SYNTHETIC_REFUSAL"
    # The released job is a normal waiting job again: due later, claimable with a fresh token.
    assert await jobs.claim(env.ctx.db, env.workspace_id, "worker-release", [JobType.VALUATION], 30) is None


async def test_heartbeat_detects_a_lost_lease_and_cancels_the_handler(env: PipelineEnv) -> None:
    reached_end = False

    async def slow(ctx: RuntimeContext, execution: JobExecution) -> JobOutcome:
        nonlocal reached_end
        env.seed.conn.execute(
            "update ops.jobs set lease_expires_at = clock_timestamp() - interval '1 second' where id = %s",
            (execution.job.id,),
        )
        await anyio.sleep(10)  # cancelled by the heartbeat's lease-loss detection
        reached_end = True
        return await succeed(ctx, execution)

    fast = dataclasses.replace(
        env.ctx, options=RuntimeOptions(job_lease_seconds=5, heartbeat_seconds=0.2), owns_db=False
    )
    job_id = await enqueue(env)
    with anyio.fail_after(5):
        [report] = await Worker(
            fast, registry_with(slow), workspace_ids=[env.workspace_id], worker_id="worker-hb"
        ).run_until_idle()
    assert report.lease_lost and report.state is None and not reached_end
    assert job_row(env, job_id)["state"] == "running"  # left for the reaper, nothing committed


async def test_heartbeats_failing_past_the_lease_stop_the_handler(
    env: PipelineEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the database cannot be reached for heartbeats, the lease is certainly gone once a full
    lease period passed since the last extension: the handler's (network) work is cancelled instead
    of continuing for a job another worker may already hold."""
    reached_end = False

    async def slow(ctx: RuntimeContext, execution: JobExecution) -> JobOutcome:
        nonlocal reached_end
        await anyio.sleep(10)  # e.g. a long fetch; cancelled once the lease is certainly lost
        reached_end = True
        return await succeed(ctx, execution)

    async def unreachable(*_args: object, **_kwargs: object) -> bool:
        raise DependencyUnavailable("synthetic: database unreachable for heartbeats")

    monkeypatch.setattr(jobs, "heartbeat", unreachable)
    fast = dataclasses.replace(
        env.ctx, options=RuntimeOptions(job_lease_seconds=1, heartbeat_seconds=0.2), owns_db=False
    )
    job_id = await enqueue(env)
    started = anyio.current_time()
    with anyio.fail_after(5):
        [report] = await Worker(
            fast, registry_with(slow), workspace_ids=[env.workspace_id], worker_id="worker-hb-down"
        ).run_until_idle()
    assert report.lease_lost and report.state is None and not reached_end
    assert anyio.current_time() - started >= 1.0  # never before a full lease period
    assert job_row(env, job_id)["state"] == "running"  # nothing committed; the reaper recovers it


async def test_new_job_types_plug_into_the_registry(env: PipelineEnv) -> None:
    """Extension point for later packages (e.g. seller-inquiry dispatch): register, then claim."""
    registry = default_registry()
    assert set(registry.job_types()) == {
        JobType.DETAIL,
        JobType.DISCOVERY,
        JobType.RECHECK,
        JobType.VALUATION,
    }
    with pytest.raises(ValidationFailed):
        registry.register(JobType.VALUATION, succeed)  # replacing needs replace=True
    registry.register(JobType.COMPARABLES, succeed, payload_versions=(1, 2), description="synthetic")
    job_id = await enqueue(env, JobType.COMPARABLES, payload_version=2)
    reports = await worker(env, registry, job_types=[JobType.COMPARABLES]).run_until_idle()
    assert [(r.job_id, r.state) for r in reports] == [(job_id, JobState.SUCCEEDED)]
    assert job_row(env, job_id)["result_reference"] == {"synthetic": True}


async def test_stop_event_ends_the_loop_without_claiming(env: PipelineEnv) -> None:
    job_id = await enqueue(env)
    stop = anyio.Event()
    stop.set()
    with anyio.fail_after(5):
        await worker(env, registry_with(succeed)).run(stop)
    assert job_row(env, job_id)["state"] == "queued"
