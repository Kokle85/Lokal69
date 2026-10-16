"""Scheduler isolation and environment gates (spec 9, 13, 30). All data is SYNTHETIC.

The end-to-end slot/dedup behaviour lives in ``test_e2e_fixture_pipeline``; these tests cover what
one tick must never do: let one failing schedule stop the others, or create jobs that can only
block (a fixture source in production, whose processes load no fixture data), and how a source
with an explicit discovery window (owner decision 2026-10-16) is ensured: with the window's
interval and due time, or refused when the enabled profiles would overspend its budget.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.pipeline.support import (
    REPO,
    SOURCE_KEY,
    PipelineEnv,
    build_env,
    fixture_source_config,
    jobs_of,
    pipeline_settings,
    run,
    run_pipeline,
)

from suv_deals.clock import SystemClock
from suv_deals.crawling.scheduler import ensure_schedules, run_scheduler_tick
from suv_deals.domain.enums import JobType, ProfileKey
from suv_deals.domain.profiles import load_business_config
from suv_deals.domain.sources import DiscoverySchedule
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


# --------------------------------------------------------------------------------------------
# Explicit discovery windows (owner decision 2026-10-16)
# --------------------------------------------------------------------------------------------

SKOPJE = ZoneInfo("Europe/Skopje")
WINDOW = DiscoverySchedule(
    interval_minutes=30, window_start="08:00", window_end="22:00", timezone="Europe/Skopje"
)


def _local(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=SKOPJE).astimezone(UTC)


@pytest.fixture
async def windowed(db_url: str, seed: Seed) -> AsyncIterator[PipelineEnv]:
    """The fixture source with the owner's window (2 search pages x 28 runs = 56 of the 100
    search requests a fetch source may use of its 200-request budget)."""
    built = await build_env(db_url, seed, source=fixture_source_config(schedule=WINDOW), name="Window")
    try:
        yield built
    finally:
        await built.close()


async def _ensure(env: PipelineEnv, now: datetime | None) -> Any:
    record = await run(
        env.ctx, env.system, lambda c: sources_repo.get_source_record(c, env.system, env.source_id)
    )
    return await ensure_schedules(env.ctx.db, env.system, [record], 900, now=now)


def _schedules(env: PipelineEnv) -> list[dict[str, Any]]:
    return env.rows(
        "select id, interval_seconds, next_due_at, last_slot from ops.source_schedules"
        " where workspace_id = %s and source_id = %s",
        env.workspace_id,
        env.source_id,
    )


async def test_ensure_schedules_creates_a_windowed_schedule(windowed: PipelineEnv) -> None:
    """Frozen clock: created at 07:00 local, the schedule is due at 08:00 with the 30-minute
    interval, runs the 08:00 slot and is then due at 08:30; a later ensure changes nothing."""
    result = await _ensure(windowed, _local(20, 7))
    assert result.ensured == 1 and result.over_budget == ()
    [row] = _schedules(windowed)
    assert row["interval_seconds"] == 1800 and row["next_due_at"] == _local(20, 8)
    advance = await run(
        windowed.ctx,
        windowed.system,
        lambda c: sources_repo.advance_schedule(c, windowed.system, row["id"], now=_local(20, 8, 0, 1)),
    )
    assert advance.outcome == "enqueued" and advance.slot == _local(20, 8)
    slots = windowed.rows(
        "select scheduled_slot from ops.jobs where workspace_id = %s and job_type = 'discovery'",
        windowed.workspace_id,
    )
    assert [j["scheduled_slot"] for j in slots] == [_local(20, 8)]
    again = await _ensure(windowed, _local(20, 8, 10))
    assert again.ensured == 1
    [after] = _schedules(windowed)
    assert after["next_due_at"] == _local(20, 8, 30) and after["last_slot"] == _local(20, 8)
    audits = windowed.scalar(
        "select count(*) from ops.audit_events where workspace_id = %s and action = 'schedule.reconfigure'",
        windowed.workspace_id,
    )
    assert audits == 0


async def test_a_window_over_budget_is_refused_and_reported(windowed: PipelineEnv) -> None:
    """Two enabled profiles need 2 x 56 = 112 search requests a day, more than the 100 allowed:
    the tick reports the source and creates no schedule (never stretched, never overspent)."""
    await enable_manual_profile(windowed)
    tick = await run_scheduler_tick(
        windowed.ctx.db, windowed.ctx.settings, SystemClock(), workspace_ids=[windowed.workspace_id]
    )
    [mine] = tick.workspaces
    assert mine.error is None
    assert mine.schedules_over_budget == (SOURCE_KEY,) and mine.schedules_ensured == 0
    assert mine.enqueued == () and _schedules(windowed) == []
    assert jobs_of(windowed, JobType.DISCOVERY) == []
