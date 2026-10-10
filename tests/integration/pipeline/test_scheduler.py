"""Scheduler isolation and environment gates (spec 9, 13, 30). All data is SYNTHETIC.

The end-to-end slot/dedup behaviour lives in ``test_e2e_fixture_pipeline``; these tests cover what
one tick must never do: let one failing schedule stop the others, or create jobs that can only
block (a fixture source in production, whose processes load no fixture data).
"""

from __future__ import annotations

import dataclasses
from typing import Any
from uuid import UUID

import pytest
from tests.integration.pipeline.support import (
    REPO,
    PipelineEnv,
    jobs_of,
    pipeline_settings,
    run,
    run_pipeline,
)

from suv_deals.clock import SystemClock
from suv_deals.crawling.scheduler import run_scheduler_tick
from suv_deals.domain.enums import JobType, ProfileKey
from suv_deals.domain.profiles import load_business_config
from suv_deals.persistence import config_repo, sources_repo
from suv_deals.persistence.errors_map import TransientConflict
from suv_deals.workers.reconciliation import ReconcileOptions, Reconciler
from suv_deals.workers.runtime import fixture_runs_allowed

pytestmark = pytest.mark.db


async def enable_manual_profile(env: PipelineEnv) -> None:
    """A second enabled profile, so the fixture source has two due schedules."""
    config = load_business_config(REPO / "config")
    manual = config.profiles[ProfileKey.MANUAL_4000].model_copy(update={"enabled": True})
    changed = config.model_copy(update={"profiles": {**config.profiles, ProfileKey.MANUAL_4000: manual}})
    await run(
        env.ctx,
        env.owner,
        lambda c: config_repo.record_config_revision(
            c, env.owner, changed, "SYNTHETIC: enable the manual profile", config
        ),
    )


async def test_one_failing_schedule_never_stops_the_others(
    env: PipelineEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    await enable_manual_profile(env)
    real_advance = sources_repo.advance_schedule
    failed: list[UUID] = []

    async def flaky(conn: Any, actor: Any, schedule_id: UUID, slot: Any = None) -> Any:
        # The first schedule of the tick keeps hitting a lock timeout (it outlives the retries).
        if not failed or failed[0] == schedule_id:
            failed.append(schedule_id)
            raise TransientConflict()
        return await real_advance(conn, actor, schedule_id, slot)

    monkeypatch.setattr(sources_repo, "advance_schedule", flaky)
    tick = await run_scheduler_tick(
        env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id]
    )
    [mine] = tick.workspaces
    assert mine.error is None and mine.schedules_ensured == 2
    assert len(mine.enqueued) == 1  # the other schedule was still advanced
    assert mine.skipped == {"advance_failed:VERSION_CONFLICT": 1}
    assert len(set(failed)) == 1 and len(failed) == 3  # retried as transient, then reported
    assert [j["id"] for j in jobs_of(env, JobType.DISCOVERY)] == list(mine.enqueued)
    # The failed schedule was not consumed: it is still due and is enqueued on the next tick.
    monkeypatch.setattr(sources_repo, "advance_schedule", real_advance)
    retry = await run_scheduler_tick(
        env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id]
    )
    [again] = retry.workspaces
    assert len(again.enqueued) == 1 and again.skipped == {}
    assert len(jobs_of(env, JobType.DISCOVERY)) == 2


async def test_fixture_sources_never_get_jobs_in_production(env: PipelineEnv) -> None:
    """Production processes load no fixture data (`build_runtime`): every discovery or sweep job
    of a fixture source could only block, one more per slot, so none is created."""
    await run_pipeline(env)  # the fixture pipeline itself runs (non-production test settings)
    production = pipeline_settings(
        str(env.ctx.settings.database_url.get_secret_value()),  # type: ignore[union-attr]
        app_env="production",
    )
    assert not fixture_runs_allowed(production) and fixture_runs_allowed(env.ctx.settings)
    discovery_before = len(jobs_of(env, JobType.DISCOVERY))
    env.seed.conn.execute(
        "update ops.source_schedules set next_due_at = now() - interval '1 second',"
        " last_slot = last_slot - interval '15 minutes' where workspace_id = %s",
        (env.workspace_id,),
    )
    tick = await run_scheduler_tick(env.ctx.db, production, SystemClock(), workspace_ids=[env.workspace_id])
    [mine] = tick.workspaces
    assert mine.enqueued == () and mine.skipped == {"fixture_source_in_production": 1}
    assert len(jobs_of(env, JobType.DISCOVERY)) == discovery_before
    # The sweeps follow the same rule: a process without fixture data queues no detail work.
    env.seed.conn.execute(
        "update app.listings set last_detail_success_at = now() - interval '3 days' where workspace_id = %s",
        (env.workspace_id,),
    )
    env.seed.conn.execute(
        "update ops.jobs set created_at = now() - interval '3 days'"
        " where workspace_id = %s and job_type in ('detail', 'recheck')",
        (env.workspace_id,),
    )
    no_fixtures = dataclasses.replace(env.ctx, settings=production, fixture_client=None, owns_db=False)
    report = await Reconciler(no_fixtures, ReconcileOptions()).reconcile_workspace(env.workspace_id)
    assert report.errors == [] and report.stale_detail_jobs == 0
    # The same pass in a process that has the fixture data queues them (the gate is the cause).
    with_fixtures = await Reconciler(env.ctx, ReconcileOptions()).reconcile_workspace(env.workspace_id)
    assert with_fixtures.stale_detail_jobs == 3
