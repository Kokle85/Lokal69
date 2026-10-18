"""Scheduler isolation and environment gates (spec 9, 13, 30). All data is SYNTHETIC.

The end-to-end slot/dedup behaviour lives in ``test_e2e_fixture_pipeline``; these tests cover what
one tick must never do: let one failing schedule stop the others, or create jobs that can only
block (a fixture source in production, whose processes load no fixture data), and how a source
with an explicit discovery window (owner decision 2026-10-16) is ensured: with the window's
interval and due time, or refused when the enabled profiles would overspend its budget or the
scheduler's ticks cannot reach its slots; that a windowed slot's job never runs after its
slot's run window (checked when the job starts and before it is deferred); and that a slot job
whose lease was lost blocks the schedule only until a reconciler pass returns it to the queue.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
from suv_deals.crawling.discovery import SLOT_EXPIRED
from suv_deals.crawling.scheduler import OFF_TICK, ensure_schedules, run_scheduler_tick
from suv_deals.domain.enums import JobType, ProfileKey
from suv_deals.domain.profiles import load_business_config
from suv_deals.domain.sources import DiscoverySchedule, RateBudget
from suv_deals.persistence import config_repo, jobs, sources_repo
from suv_deals.persistence.errors_map import TransientConflict
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.workers.reconciliation import ReconcileOptions, Reconciler
from suv_deals.workers.runner import Worker
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
    """The fixture source with the owner's window and a 100-request budget (2 search pages x 28
    runs = 56 of the 90 search requests it allows: all but one run's 10 detail pages)."""
    source = fixture_source_config(schedule=WINDOW, rate_budget=RateBudget(daily_request_budget=100))
    built = await build_env(db_url, seed, source=source, name="Window")
    try:
        yield built
    finally:
        await built.close()


# Two slots a day, 00:00 and 12:00 UTC: the slot that is live when the test runs stays live for
# hours (no race with a slot boundary), and both are on the scheduler's 15-minute grid.
HALF_DAYS = DiscoverySchedule(interval_minutes=720, timezone="UTC")


@pytest.fixture
async def half_days(db_url: str, seed: Seed) -> AsyncIterator[PipelineEnv]:
    built = await build_env(db_url, seed, source=fixture_source_config(schedule=HALF_DAYS), name="HalfDays")
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
    """Two enabled profiles need 2 x 56 = 112 search requests a day, more than the 90 allowed:
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


async def test_a_window_its_ticks_cannot_reach_is_refused_and_not_advanced(windowed: PipelineEnv) -> None:
    """SCHEDULER_INTERVAL_SECONDS=3600 cannot start the :30 slots: the tick reports the source,
    leaves its existing (due) schedule untouched and enqueues nothing; it stays visibly due."""
    await _ensure(windowed, _local(10, 8))  # a row created earlier, due since 2026-10-10 08:00
    [before] = _schedules(windowed)
    hourly = windowed.ctx.settings.model_copy(update={"scheduler_interval_seconds": 3600})
    tick = await run_scheduler_tick(
        windowed.ctx.db, hourly, SystemClock(), workspace_ids=[windowed.workspace_id]
    )
    [mine] = tick.workspaces
    assert mine.error is None and mine.schedules_off_tick == (SOURCE_KEY,) and mine.schedules_ensured == 0
    assert mine.enqueued == () and mine.skipped == {OFF_TICK: 1}
    assert _schedules(windowed) == [before]
    assert jobs_of(windowed, JobType.DISCOVERY) == []


def _discovery_outcome(env: PipelineEnv) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    found = env.rows(
        "select state, last_error_code, result_reference from ops.jobs"
        " where workspace_id = %s and job_type = 'discovery'",
        env.workspace_id,
    )
    runs = env.rows(
        "select outcome, gap_reasons from ops.crawl_runs where workspace_id = %s", env.workspace_id
    )
    fetches = env.scalar(
        "select count(*) from ops.fetch_attempts where workspace_id = %s and purpose = 'search'",
        env.workspace_id,
    )
    return found, runs, int(fetches)


async def test_a_slot_job_released_past_its_run_window_fetches_nothing(windowed: PipelineEnv) -> None:
    """The 21:30 Skopje slot is enqueued, then released by the budget gate to 00:00 UTC (02:00
    Skopje). When a worker claims it (any time later) nothing is fetched: the job completes, its
    run is ``cancelled`` with the ``slot_expired`` gap, recorded on the schedule."""
    env = windowed
    await _ensure(env, _local(15, 21, 29))
    [row] = _schedules(env)
    advance = await run(
        env.ctx,
        env.system,
        lambda c: sources_repo.advance_schedule(c, env.system, row["id"], now=_local(15, 21, 30, 1)),
    )
    assert advance.outcome == "enqueued" and advance.slot == _local(15, 21, 30)
    claimed = await jobs.claim(env.ctx.db, env.workspace_id, "gate-worker", [JobType.DISCOVERY], 120)
    assert claimed is not None
    midnight = datetime(2026, 10, 16, tzinfo=UTC)
    assert WINDOW.live_slot(midnight) is None  # 02:00 Skopje
    async with unit_of_work(env.ctx.db, env.system) as conn:
        await jobs.release(
            conn, claimed, available_at=midnight, code="DAILY_BUDGET_EXHAUSTED", actor=env.system
        )

    await Worker(env.ctx, workspace_ids=[env.workspace_id], worker_id="late-worker").run_until_idle()
    found, runs, fetches = _discovery_outcome(env)
    assert fetches == 0
    assert [(j["state"], (j["result_reference"] or {}).get("skipped")) for j in found] == [
        ("succeeded", SLOT_EXPIRED)
    ]
    [crawl] = runs
    assert crawl["outcome"] == "cancelled"
    assert any(g.startswith(f"{SLOT_EXPIRED}: {advance.slot.isoformat()}") for g in crawl["gap_reasons"])
    gaps = env.rows("select gap_reasons from ops.source_schedules where id = %s", row["id"])[0]["gap_reasons"]
    assert any(g.startswith(SLOT_EXPIRED) for g in gaps)


async def test_a_refusal_past_the_run_window_ends_the_job_instead_of_deferring(
    half_days: PipelineEnv,
) -> None:
    """The live slot's job meets an exhausted daily budget: the gate would release it to the next
    UTC midnight, which is at or after the end of its slot's run window. The job ends instead
    (completed, nothing fetched, gap recorded) rather than running outside its slot."""
    env = half_days
    record = await run(
        env.ctx, env.system, lambda c: sources_repo.get_source_record(c, env.system, env.source_id)
    )
    await ensure_schedules(env.ctx.db, env.system, [record], 900)
    [row] = _schedules(env)
    advance = await run(
        env.ctx, env.system, lambda c: sources_repo.advance_schedule(c, env.system, row["id"])
    )
    assert advance.outcome == "enqueued"
    env.seed.conn.execute(
        "insert into ops.host_budgets (workspace_id, host, capacity, refill_per_second, tokens, refilled_at,"
        " budget_day, requests_today, daily_request_budget, daily_byte_budget)"
        " values (%s, 'dealer.example', 1, 0.05, 1, clock_timestamp(),"
        " (clock_timestamp() at time zone 'UTC')::date, 200, 200, 200000000)",
        (env.workspace_id,),
    )

    await Worker(env.ctx, workspace_ids=[env.workspace_id], worker_id="refused-worker").run_until_idle()
    found, runs, fetches = _discovery_outcome(env)
    assert fetches == 0
    assert [(j["state"], (j["result_reference"] or {}).get("skipped")) for j in found] == [
        ("succeeded", SLOT_EXPIRED)
    ]
    [crawl] = runs
    assert crawl["outcome"] == "cancelled"
    assert any("would start after the run window" in g for g in crawl["gap_reasons"])


async def test_a_lost_lease_blocks_the_slots_until_a_reconciler_pass_requeues_it(
    half_days: PipelineEnv,
) -> None:
    """The worker loses its slot job's lease (the PC slept, or the database was unreachable for
    longer than the lease): it gives the job up and commits nothing, so the job stays ``running``.
    No worker claims it again and every later slot is skipped as ``backlog``; the scheduler and
    the worker alone never recover. Only the reconciler's reaper returns it to the queue, which is
    why ``scripts/local_crawl.sh`` runs ``suv-deals reconcile --loop`` next to them (runbook 9.2).
    After one pass the job runs and the schedule's next slot is enqueued again."""
    env = half_days
    record = await run(
        env.ctx, env.system, lambda c: sources_repo.get_source_record(c, env.system, env.source_id)
    )
    await ensure_schedules(env.ctx.db, env.system, [record], 900)
    [row] = _schedules(env)
    first = await run(env.ctx, env.system, lambda c: sources_repo.advance_schedule(c, env.system, row["id"]))
    assert first.outcome == "enqueued" and first.job_id is not None
    claimed = await jobs.claim(env.ctx.db, env.workspace_id, "sleeping-worker", [JobType.DISCOVERY], 120)
    assert claimed is not None and claimed.id == first.job_id
    env.seed.conn.execute(
        "update ops.jobs set lease_expires_at = clock_timestamp() - interval '1 hour' where id = %s",
        (claimed.id,),
    )
    assert await jobs.heartbeat(env.ctx.db, claimed, 120) is False  # the worker marks the job lost
    assert await jobs.claim(env.ctx.db, env.workspace_id, "other-worker", [JobType.DISCOVERY], 120) is None

    async def later_slot(hours: int) -> Any:
        env.seed.conn.execute(
            "update ops.source_schedules set next_due_at = clock_timestamp() - interval '1 minute'"
            " where id = %s",
            (row["id"],),
        )
        slot = first.slot + timedelta(hours=hours)
        return await run(
            env.ctx, env.system, lambda c: sources_repo.advance_schedule(c, env.system, row["id"], slot=slot)
        )

    blocked = [await later_slot(12), await later_slot(24)]
    assert [(a.outcome, a.skip_reason) for a in blocked] == [("skipped", "backlog")] * 2
    assert env.rows("select state from ops.jobs where id = %s", claimed.id) == [{"state": "running"}]

    report = await Reconciler(env.ctx, ReconcileOptions(job_retry_delay_seconds=0)).reconcile_workspace(
        env.workspace_id
    )
    assert report.errors == [] and report.jobs_requeued == 1
    await Worker(env.ctx, workspace_ids=[env.workspace_id], worker_id="awake-worker").run_until_idle()
    assert env.rows("select state from ops.jobs where id = %s", claimed.id) == [{"state": "succeeded"}]
    resumed = await later_slot(36)
    assert resumed.outcome == "enqueued" and resumed.skip_reason is None
