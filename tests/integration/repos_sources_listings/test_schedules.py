"""Scheduling slots, crawl runs, watermarks and coverage (spec 9 "Scheduling", "Discovery watermarks")."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from tests.integration.db.helpers import Seed, unique
from tests.integration.repos_sources_listings.support import T0, Env, card, ingest, page, start_run

from suv_deals.domain.enums import AccessState, Completeness, CoverageMode
from suv_deals.errors import ValidationFailed, VersionConflict
from suv_deals.persistence import sources_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.sources_repo import RunOutcome, ScheduleAdvance
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


def _schedule(seed: Seed, schedule_id: Any) -> dict[str, Any]:
    cur = seed.conn.execute(
        "select next_due_at, last_slot, cursor, run_id, coverage_mode, complete_watermark, page_depth,"
        " last_complete_traversal_at, incomplete_since, gap_reasons, backoff_until, consecutive_failures,"
        " row_version from ops.source_schedules where id = %s",
        (schedule_id,),
    )
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


def _slot_jobs(seed: Seed, schedule_id: Any) -> list[tuple[Any, ...]]:
    return seed.conn.execute(
        "select j.id, j.scheduled_slot, j.dedup_key, j.state from ops.jobs j join ops.source_schedules s"
        " on s.workspace_id = j.workspace_id and s.source_id = j.source_id and s.profile_id = j.profile_id"
        " and s.partition_key = j.partition_key where s.id = %s and j.job_type = 'discovery'"
        " order by j.scheduled_slot",
        (schedule_id,),
    ).fetchall()


async def _advance(db: Database, env: Env, schedule_id: uuid.UUID, **kwargs: Any) -> ScheduleAdvance:
    async with unit_of_work(db, env.system) as conn:
        return await sources_repo.advance_schedule(conn, env.system, schedule_id, **kwargs)


async def _watermark_schedule(db: Database, env: Env) -> sources_repo.ScheduleRecord:
    async with unit_of_work(db, env.system) as conn:
        return await sources_repo.ensure_schedule(
            conn,
            env.system,
            env.source_id,
            env.profiles["primary"],
            "modified-since",
            coverage_mode=CoverageMode.WATERMARK,
        )


async def _watermark_run(db: Database, env: Env, **kwargs: Any) -> sources_repo.CrawlRunRecord:
    async with unit_of_work(db, env.system) as conn:
        return await sources_repo.start_crawl_run(
            conn,
            env.system,
            source_id=env.source_id,
            profile_id=env.profiles["primary"],
            partition_key="modified-since",
            coverage_mode=CoverageMode.WATERMARK,
            adapter_version="fixture@1.0.0",
            **kwargs,
        )


async def _finish(db: Database, env: Env, run_id: uuid.UUID, outcome: RunOutcome) -> sources_repo.RunFinish:
    async with unit_of_work(db, env.system) as conn:
        return await sources_repo.finish_crawl_run(conn, env.system, run_id, outcome)


# --------------------------------------------------------------------------------------------
# Slots
# --------------------------------------------------------------------------------------------


async def test_ensure_schedule_is_idempotent_and_due(db: Database, env: Env) -> None:
    async with unit_of_work(db, env.system) as conn:
        again = await sources_repo.ensure_schedule(
            conn, env.system, env.source_id, env.profiles["primary"], coverage_mode=CoverageMode.ROLLING_PAGES
        )
        due = await sources_repo.due_schedules(conn, env.system)
    assert again.id == env.schedule_id
    assert env.schedule_id in {s.id for s in due}


async def test_advance_enqueues_one_job_per_slot(db: Database, env: Env, seed: Seed) -> None:
    first = await _advance(db, env, env.schedule_id)
    assert first.outcome == "enqueued" and first.job_id is not None
    assert first.next_due_at == first.slot + timedelta(minutes=15)
    jobs = _slot_jobs(seed, env.schedule_id)
    assert len(jobs) == 1 and jobs[0][1] == first.slot and jobs[0][3] == "queued"
    row = _schedule(seed, env.schedule_id)
    assert row["last_slot"] == first.slot and row["next_due_at"] == first.next_due_at
    second = await _advance(db, env, env.schedule_id)
    assert second.outcome == "not_due" and second.job_id is None
    assert len(_slot_jobs(seed, env.schedule_id)) == 1


async def test_two_concurrent_schedulers_create_exactly_one_job(db: Database, env: Env, seed: Seed) -> None:
    results = await asyncio.gather(*(_advance(db, env, env.schedule_id) for _ in range(4)))
    outcomes = sorted(r.outcome for r in results)
    assert outcomes.count("enqueued") == 1 and set(outcomes) <= {"enqueued", "not_due", "already_scheduled"}
    assert len(_slot_jobs(seed, env.schedule_id)) == 1
    # A scheduler restarting from stale state (lost next_due/last_slot) cannot schedule the slot again:
    # the slot key is unique for every job state.
    slot = next(r.slot for r in results if r.outcome == "enqueued")
    seed.conn.execute(
        "update ops.jobs set state = 'succeeded', completed_at = now() where id = %s",
        (_slot_jobs(seed, env.schedule_id)[0][0],),
    )
    seed.conn.execute(
        "update ops.source_schedules set next_due_at = now() - interval '1 minute', last_slot = null"
        " where id = %s",
        (env.schedule_id,),
    )
    stale = await asyncio.gather(*(_advance(db, env, env.schedule_id, slot=slot) for _ in range(2)))
    assert {r.outcome for r in stale} <= {"already_scheduled", "not_due"}
    assert len(_slot_jobs(seed, env.schedule_id)) == 1


async def test_skipped_slot_records_gap_and_advances(db: Database, env: Env, seed: Seed) -> None:
    version = seed.scalar("select version from app.sources where id = %s", (env.source_id,))
    async with unit_of_work(db, env.owner) as conn:
        await sources_repo.pause_source(
            conn, env.owner, env.source_id, version, "synthetic pause", "pause-sched-1"
        )
    async with unit_of_work(db, env.system) as conn:
        assert env.schedule_id not in {s.id for s in await sources_repo.due_schedules(conn, env.system)}
    result = await _advance(db, env, env.schedule_id)
    assert result.outcome == "skipped" and result.skip_reason == "source_paused" and result.job_id is None
    assert _slot_jobs(seed, env.schedule_id) == []
    row = _schedule(seed, env.schedule_id)
    assert row["gap_reasons"] == ["slot_skipped: source_paused"] and row["next_due_at"] == result.next_due_at


async def test_backlog_and_daily_budget_skip(db: Database, env: Env, seed: Seed) -> None:
    first = await _advance(db, env, env.schedule_id)
    assert first.outcome == "enqueued"
    seed.conn.execute(
        "update ops.source_schedules set next_due_at = now() - interval '1 minute', last_slot = %s"
        " where id = %s",
        (first.slot - timedelta(minutes=15), env.schedule_id),
    )
    backlog = await _advance(db, env, env.schedule_id, slot=first.slot + timedelta(minutes=15))
    assert backlog.outcome == "skipped" and backlog.skip_reason == "backlog"
    seed.conn.execute(
        "update ops.jobs set state = 'succeeded', completed_at = now() where id = %s", (first.job_id,)
    )
    seed.insert(
        "ops.host_budgets",
        workspace_id=env.workspace_id,
        host="dealer-a.synthetic.example",
        refill_per_second="0.05",
        tokens="1",
        refilled_at=T0,
        budget_day=seed.scalar("select (now() at time zone 'UTC')::date"),
        requests_today=10_000,
    )
    seed.conn.execute(
        "update ops.source_schedules set next_due_at = now() - interval '1 minute' where id = %s",
        (env.schedule_id,),
    )
    exhausted = await _advance(db, env, env.schedule_id, slot=first.slot + timedelta(minutes=30))
    assert exhausted.outcome == "skipped" and exhausted.skip_reason == "daily_budget_exhausted"


async def test_due_by_time_but_slot_already_handled_moves_due_time(
    db: Database, env: Env, seed: Seed
) -> None:
    first = await _advance(db, env, env.schedule_id)
    seed.conn.execute(
        "update ops.source_schedules set next_due_at = now() - interval '1 minute' where id = %s",
        (env.schedule_id,),
    )
    again = await _advance(db, env, env.schedule_id, slot=first.slot)
    assert again.outcome == "not_due" and again.next_due_at == first.slot + timedelta(minutes=15)
    assert _schedule(seed, env.schedule_id)["next_due_at"] == again.next_due_at


# --------------------------------------------------------------------------------------------
# Watermarks and coverage
# --------------------------------------------------------------------------------------------


async def test_budget_limited_run_does_not_advance_complete_watermark(
    db: Database, env: Env, seed: Seed
) -> None:
    schedule = await _watermark_schedule(db, env)
    complete = await _watermark_run(db, env, watermark_from=T0 - timedelta(hours=48))
    done = await _finish(
        db, env, complete.id, RunOutcome(completeness=Completeness.COMPLETE, watermark_to=T0)
    )
    assert done.watermark_advanced and done.schedule is not None and done.schedule.complete_watermark == T0
    limited = await _watermark_run(db, env, watermark_from=T0 - timedelta(hours=48))
    assert _schedule(seed, schedule.id)["run_id"] == limited.id  # in-progress run id is persisted
    result = await _finish(
        db,
        env,
        limited.id,
        RunOutcome(
            completeness=Completeness.BUDGET_LIMITED,
            watermark_to=T0 + timedelta(hours=6),
            cursor={"next_url_hash": "a" * 64, "page": 3},
            gap_reasons=("search page budget reached at page 2",),
        ),
    )
    assert not result.watermark_advanced
    row = _schedule(seed, schedule.id)
    assert row["complete_watermark"] == T0  # unseen results are never skipped
    assert row["cursor"] == {"next_url_hash": "a" * 64, "page": 3}
    assert row["incomplete_since"] == limited.started_at
    assert any(g.startswith("budget_limited: run") for g in row["gap_reasons"])
    assert "search page budget reached at page 2" in row["gap_reasons"]
    assert seed.scalar(
        "select watermark_to from ops.crawl_runs where id = %s", (limited.id,)
    ) == T0 + timedelta(hours=6)
    # A later complete traversal advances it (never backwards) and clears the incomplete state.
    newer = await _watermark_run(db, env, watermark_from=T0 - timedelta(hours=48))
    await _finish(
        db,
        env,
        newer.id,
        RunOutcome(completeness=Completeness.COMPLETE, watermark_to=T0 + timedelta(hours=8)),
    )
    older = await _watermark_run(db, env, watermark_from=T0 - timedelta(hours=48))
    regress = await _finish(
        db, env, older.id, RunOutcome(completeness=Completeness.COMPLETE, watermark_to=T0)
    )
    assert not regress.watermark_advanced
    row = _schedule(seed, schedule.id)
    assert row["complete_watermark"] == T0 + timedelta(hours=8)
    assert row["cursor"] is None and row["incomplete_since"] is None and row["gap_reasons"] == []


async def test_rolling_pages_coverage_records_depth_without_watermark(
    db: Database, env: Env, seed: Seed
) -> None:
    run = await start_run(db, env)
    for number in (1, 2, 3):
        await ingest(db, env, run, page(env.source_key, [card(unique("SYN"))], page_number=number))
    finished = await _finish(db, env, run.id, RunOutcome(completeness=Completeness.COMPLETE))
    assert finished.run.page_depth == 3 and finished.run.pages_fetched == 3 and finished.run.cards_seen == 3
    row = _schedule(seed, env.schedule_id)
    assert row["page_depth"] == 3 and row["last_complete_traversal_at"] == run.started_at
    assert row["complete_watermark"] is None  # never fabricated for rolling pages
    with pytest.raises(ValidationFailed):
        await _finish(
            db,
            env,
            (await start_run(db, env)).id,
            RunOutcome(completeness=Completeness.COMPLETE, watermark_to=T0),
        )
    async with unit_of_work(db, env.system) as conn:
        with pytest.raises(ValidationFailed):
            await sources_repo.start_crawl_run(
                conn,
                env.system,
                source_id=env.source_id,
                profile_id=env.profiles["primary"],
                coverage_mode=CoverageMode.ROLLING_PAGES,
                adapter_version="fixture@1.0.0",
                watermark_from=T0,
            )


async def test_failed_run_backs_off_and_finish_is_idempotent(db: Database, env: Env, seed: Seed) -> None:
    run = await start_run(db, env)
    outcome = RunOutcome(
        completeness=Completeness.BLOCKED, access_state=AccessState.ACCESS_BLOCKED, error_code="HTTP_403"
    )
    first = await _finish(db, env, run.id, outcome)
    assert first.run.outcome == "blocked" and first.run.access_state == AccessState.ACCESS_BLOCKED
    row = _schedule(seed, env.schedule_id)
    assert row["consecutive_failures"] == 1 and row["backoff_until"] is not None
    assert first.run.finished_at is not None
    assert row["backoff_until"] >= first.run.finished_at + timedelta(minutes=30)
    async with unit_of_work(db, env.system) as conn:
        assert env.schedule_id not in {s.id for s in await sources_repo.due_schedules(conn, env.system)}
    replay = await _finish(db, env, run.id, outcome)
    assert replay.run.id == run.id and _schedule(seed, env.schedule_id)["consecutive_failures"] == 1
    with pytest.raises(VersionConflict):
        await _finish(db, env, run.id, RunOutcome(completeness=Completeness.COMPLETE))


async def test_downtime_catchup_records_missed_slots_and_never_bursts(
    db: Database, env: Env, seed: Seed
) -> None:
    """Spec 31 Scheduler row "downtime catchup" (F7, wave D2): after a scheduler outage of several
    intervals the next advance enqueues ONE job for the current slot only (the missed slots are
    never replayed as a burst) and records them as a coverage gap (``scheduler_missed_slots``)."""
    first = await _advance(db, env, env.schedule_id)
    assert first.outcome == "enqueued"
    # Time travel: that slot's job ran three hours ago and the scheduler was down since then, so
    # last_slot is twelve 15-minute intervals behind the current slot.
    missed_from = first.slot - timedelta(hours=3)
    seed.conn.execute(
        "update ops.jobs set state = 'succeeded', completed_at = now(), scheduled_slot = %s,"
        " dedup_key = dedup_key || ':earlier' where id = %s",
        (missed_from, first.job_id),
    )
    seed.conn.execute(
        "update ops.source_schedules set last_slot = %s, next_due_at = now() - interval '1 minute'"
        " where id = %s",
        (missed_from, env.schedule_id),
    )
    before = {row[0] for row in _slot_jobs(seed, env.schedule_id)}

    caught_up = await _advance(db, env, env.schedule_id)

    assert caught_up.outcome == "enqueued" and caught_up.job_id is not None
    after = _slot_jobs(seed, env.schedule_id)
    new = [row for row in after if row[0] not in before]
    assert len(new) == 1  # exactly one job: no burst of replayed slots
    assert new[0][1] == caught_up.slot and new[0][0] == caught_up.job_id
    assert caught_up.slot >= first.slot  # the current wall-clock slot, nothing older
    assert sorted(row[1] for row in after) == [missed_from, caught_up.slot]  # no replayed slot
    row = _schedule(seed, env.schedule_id)
    assert row["last_slot"] == caught_up.slot
    assert row["next_due_at"] == caught_up.slot + timedelta(minutes=15)
    gaps = [g for g in row["gap_reasons"] or [] if g.startswith("scheduler_missed_slots")]
    assert len(gaps) == 1
    assert missed_from.isoformat() in gaps[0] and caught_up.slot.isoformat() in gaps[0]
