"""Job runner (spec 13, 30): payload-version guard, error mapping, lease loss, extension point, stop.

Handlers here are SYNTHETIC test doubles registered through the public `HandlerRegistry`; the
queue, leases and fencing are the real PostgreSQL ones.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Awaitable, Callable

import anyio
import pytest
from tests.integration.pipeline.support import PipelineEnv, run

from suv_deals.domain.enums import JobState, JobType
from suv_deals.errors import DependencyUnavailable, SourcePaused, ValidationFailed
from suv_deals.persistence import jobs
from suv_deals.persistence.database import Conn
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
