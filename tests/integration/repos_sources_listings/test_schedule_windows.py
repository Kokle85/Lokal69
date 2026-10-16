"""Discovery windows on PostgreSQL with a frozen clock (owner decision of 2026-10-16).

A SYNTHETIC source with the owner's window (every 30 minutes, slots 08:00 ... 21:30
Europe/Skopje) is advanced through `sources_repo.advance_schedule` with an explicit ``now`` (the
frozen clock; production uses database time): jobs only for live slots inside the window, one job
for the current slot after downtime (never a burst), the DST changes of 2026-10-25 and
2027-03-28, existing rows following a changed window (audited), and the budget re-check with the
workspace's enabled profiles.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from tests.integration.db.helpers import Seed, unique
from tests.integration.repos_sources_listings.support import Env, source_config

from suv_deals.domain.enums import CoverageMode
from suv_deals.domain.sources import DiscoverySchedule
from suv_deals.persistence import sources_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.sources_repo import ScheduleAdvance, ScheduleRecord
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db

SKOPJE = ZoneInfo("Europe/Skopje")
WINDOW = DiscoverySchedule(
    interval_minutes=30, window_start="08:00", window_end="22:00", timezone="Europe/Skopje"
)


def local(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=SKOPJE).astimezone(UTC)


async def _sync(db: Database, env: Env, *extra: tuple[str, DiscoverySchedule | None]) -> dict[str, uuid.UUID]:
    """Sync the env's source plus ``extra`` (key, window) sources; returns their ids."""
    configs = [source_config(env.source_key)]
    configs += [source_config(key, schedule=window) for key, window in extra]
    async with unit_of_work(db, env.system) as conn:
        await sources_repo.sync_sources_from_yaml(conn, env.system, configs)
        return {key: (await sources_repo.get_source_by_key(conn, env.system, key)).id for key, _ in extra}


async def _windowed(db: Database, env: Env, window: DiscoverySchedule = WINDOW) -> uuid.UUID:
    key = unique("win").lower()
    return (await _sync(db, env, (key, window)))[key]


async def _ensure(
    db: Database,
    env: Env,
    source_id: uuid.UUID,
    now: datetime,
    window: DiscoverySchedule | None = WINDOW,
    interval: int | None = None,
) -> ScheduleRecord:
    async with unit_of_work(db, env.system) as conn:
        return await sources_repo.ensure_schedule(
            conn,
            env.system,
            source_id,
            env.profiles["primary"],
            coverage_mode=CoverageMode.ROLLING_PAGES,
            interval_seconds=interval or (window.interval_seconds if window else 900),
            window=window,
            now=now,
        )


async def _advance(db: Database, env: Env, schedule_id: uuid.UUID, now: datetime) -> ScheduleAdvance:
    async with unit_of_work(db, env.system) as conn:
        return await sources_repo.advance_schedule(conn, env.system, schedule_id, now=now)


def _jobs(seed: Seed, schedule_id: uuid.UUID) -> list[datetime]:
    rows = seed.conn.execute(
        "select j.scheduled_slot from ops.jobs j join ops.source_schedules s"
        " on s.workspace_id = j.workspace_id and s.source_id = j.source_id and s.profile_id = j.profile_id"
        " and s.partition_key = j.partition_key where s.id = %s and j.job_type = 'discovery'"
        " order by j.scheduled_slot",
        (schedule_id,),
    ).fetchall()
    return [row[0] for row in rows]


def _finish_jobs(seed: Seed, schedule_id: uuid.UUID) -> None:
    """The worker ran the slot's job (no backlog for the next slot)."""
    seed.conn.execute(
        "update ops.jobs j set state = 'succeeded', completed_at = now() from ops.source_schedules s"
        " where s.id = %s and j.workspace_id = s.workspace_id and j.source_id = s.source_id"
        " and j.job_type = 'discovery' and j.state = 'queued'",
        (schedule_id,),
    )


def _row(seed: Seed, schedule_id: uuid.UUID) -> dict[str, Any]:
    cur = seed.conn.execute(
        "select interval_seconds, next_due_at, last_slot, gap_reasons, row_version"
        " from ops.source_schedules where id = %s",
        (schedule_id,),
    )
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


def _wall(instant: datetime) -> str:
    return instant.astimezone(SKOPJE).strftime("%H:%M")


async def test_a_window_runs_only_its_live_slots(db: Database, env: Env, seed: Seed) -> None:
    source_id = await _windowed(db, env)
    created = await _ensure(db, env, source_id, local(2026, 10, 20, 7))
    assert created.interval_seconds == 1800
    assert created.next_due_at == local(2026, 10, 20, 8)  # created before the window: due at 08:00

    early = await _advance(db, env, created.id, local(2026, 10, 20, 7, 30))
    assert early.outcome == "not_due" and early.next_due_at == local(2026, 10, 20, 8)

    first = await _advance(db, env, created.id, local(2026, 10, 20, 8, 0, 5))
    assert first.outcome == "enqueued" and first.slot == local(2026, 10, 20, 8)
    assert first.next_due_at == local(2026, 10, 20, 8, 30)
    assert (await _advance(db, env, created.id, local(2026, 10, 20, 8, 20))).outcome == "not_due"
    _finish_jobs(seed, created.id)

    second = await _advance(db, env, created.id, local(2026, 10, 20, 8, 30, 1))
    assert second.outcome == "enqueued" and second.slot == local(2026, 10, 20, 8, 30)
    _finish_jobs(seed, created.id)

    # The scheduler next runs at 21:30: the live 21:30 slot runs once, the 09:00-21:00 slots are a
    # recorded gap, and the next due time is tomorrow's 08:00.
    last = await _advance(db, env, created.id, local(2026, 10, 20, 21, 30, 2))
    assert last.outcome == "enqueued" and last.slot == local(2026, 10, 20, 21, 30)
    assert last.next_due_at == local(2026, 10, 21, 8)
    gaps = [g for g in _row(seed, created.id)["gap_reasons"] if g.startswith("scheduler_missed_slots")]
    assert gaps == [
        f"scheduler_missed_slots: {second.slot.isoformat()} .. {last.slot.isoformat()} not scheduled"
    ]
    _finish_jobs(seed, created.id)
    for late in (local(2026, 10, 20, 22), local(2026, 10, 20, 23, 59), local(2026, 10, 21, 7, 59)):
        assert (await _advance(db, env, created.id, late)).outcome == "not_due"

    slots = _jobs(seed, created.id)
    assert [_wall(s) for s in slots] == ["08:00", "08:30", "21:30"]  # one job per slot, all inside


async def test_a_due_row_outside_the_window_waits_for_the_next_slot(
    db: Database, env: Env, seed: Seed
) -> None:
    source_id = await _windowed(db, env)
    schedule = await _ensure(db, env, source_id, local(2026, 10, 20, 9, 10))
    assert schedule.next_due_at == local(2026, 10, 20, 9)  # the live 09:00 slot is due at once
    # Nobody advanced it during the day (e.g. the source network was off): at 22:10 it is still
    # due, but nothing may run outside the window.
    late = await _advance(db, env, schedule.id, local(2026, 10, 20, 22, 10))
    assert late.outcome == "not_due" and late.job_id is None
    assert late.next_due_at == local(2026, 10, 21, 8)
    row = _row(seed, schedule.id)
    assert row["next_due_at"] == local(2026, 10, 21, 8) and row["last_slot"] is None
    assert _jobs(seed, schedule.id) == []


async def test_downtime_runs_one_job_for_the_live_slot(db: Database, env: Env, seed: Seed) -> None:
    source_id = await _windowed(db, env)
    schedule = await _ensure(db, env, source_id, local(2026, 10, 20, 9))
    first = await _advance(db, env, schedule.id, local(2026, 10, 20, 9, 0, 1))
    assert first.outcome == "enqueued"
    _finish_jobs(seed, schedule.id)

    # Down from 09:01 to 11:10: ONE job for the live 11:00 slot, no burst for 09:30-10:30.
    back = await _advance(db, env, schedule.id, local(2026, 10, 20, 11, 10))
    assert back.outcome == "enqueued" and back.slot == local(2026, 10, 20, 11)
    assert back.next_due_at == local(2026, 10, 20, 11, 30)
    assert [_wall(s) for s in _jobs(seed, schedule.id)] == ["09:00", "11:00"]
    _finish_jobs(seed, schedule.id)

    # Down overnight until 09:40 the next day: one job for 09:30.
    morning = await _advance(db, env, schedule.id, local(2026, 10, 21, 9, 40))
    assert morning.outcome == "enqueued" and morning.slot == local(2026, 10, 21, 9, 30)
    assert morning.next_due_at == local(2026, 10, 21, 10)
    assert len(_jobs(seed, schedule.id)) == 3
    gaps = [g for g in _row(seed, schedule.id)["gap_reasons"] if g.startswith("scheduler_missed_slots")]
    assert len(gaps) == 2
    assert local(2026, 10, 20, 11).isoformat() in gaps[1] and morning.slot.isoformat() in gaps[1]


@pytest.mark.parametrize(
    ("evening", "morning_utc"),
    [
        # DST ends on 2026-10-25: 21:30 CEST (19:30Z) -> 08:00 CET (07:00Z)
        (local(2026, 10, 24, 21, 30), datetime(2026, 10, 25, 7, tzinfo=UTC)),
        # DST starts on 2027-03-28: 21:30 CET (20:30Z) -> 08:00 CEST (06:00Z)
        (local(2027, 3, 27, 21, 30), datetime(2027, 3, 28, 6, tzinfo=UTC)),
    ],
)
async def test_the_window_follows_local_time_across_dst(
    db: Database, env: Env, seed: Seed, evening: datetime, morning_utc: datetime
) -> None:
    source_id = await _windowed(db, env)
    schedule = await _ensure(db, env, source_id, evening)
    last = await _advance(db, env, schedule.id, evening.replace(second=3))
    assert last.outcome == "enqueued" and last.slot == evening
    assert last.next_due_at == morning_utc
    assert morning_utc.astimezone(SKOPJE).strftime("%H:%M") == "08:00"
    _finish_jobs(seed, schedule.id)
    before = await _advance(db, env, schedule.id, morning_utc.replace(minute=0) - (morning_utc - evening) / 2)
    assert before.outcome == "not_due"
    first = await _advance(db, env, schedule.id, morning_utc.replace(second=3))
    assert first.outcome == "enqueued" and first.slot == morning_utc
    assert first.next_due_at == morning_utc.replace(minute=30)
    assert not [g for g in _row(seed, schedule.id)["gap_reasons"] if g.startswith("scheduler_missed")]


def _audits(seed: Seed, schedule_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return seed.conn.execute(
        "select action, prior_version, new_version, metadata from ops.audit_events"
        " where target_type = 'source_schedule' and target_id = %s order by occurred_at, id",
        (schedule_id,),
    ).fetchall()


async def test_an_existing_row_follows_a_changed_window_audited(db: Database, env: Env, seed: Seed) -> None:
    """The row created with the global 15-minute interval picks up the owner's window, then an
    hourly window, then the window's removal; each change is one audited ``schedule.reconfigure``."""
    before = await _advance(db, env, env.schedule_id, local(2026, 10, 20, 10, 7))
    assert before.outcome == "enqueued" and before.slot == local(2026, 10, 20, 10)  # 15-min UTC grid
    assert before.next_due_at == local(2026, 10, 20, 10, 15)
    _finish_jobs(seed, env.schedule_id)

    await _sync(db, env)  # unchanged source: nothing to do
    async with unit_of_work(db, env.system) as conn:
        await sources_repo.sync_sources_from_yaml(
            conn, env.system, [source_config(env.source_key, schedule=WINDOW)]
        )
    row = await _ensure(db, env, env.source_id, local(2026, 10, 20, 10, 10))
    assert row.interval_seconds == 1800
    # 10:00 was already handled: the next window slot.
    assert row.next_due_at == local(2026, 10, 20, 10, 30)
    [change] = _audits(seed, env.schedule_id)
    assert change[0] == "schedule.reconfigure" and change[2] == change[1] + 1
    assert change[3]["interval_seconds_before"] == 900 and change[3]["interval_seconds_after"] == 1800
    assert change[3]["next_due_at_after"] == local(2026, 10, 20, 10, 30).isoformat()
    assert change[3]["cadence"] == "every 30 min from 08:00 to 22:00 Europe/Skopje"
    # Idempotent: the same configuration changes nothing.
    again = await _ensure(db, env, env.source_id, local(2026, 10, 20, 10, 20))
    assert again.row_version == row.row_version and len(_audits(seed, env.schedule_id)) == 1

    hourly = WINDOW.model_copy(update={"interval_minutes": 60})
    async with unit_of_work(db, env.system) as conn:
        await sources_repo.sync_sources_from_yaml(
            conn, env.system, [source_config(env.source_key, schedule=hourly)]
        )
    row = await _ensure(db, env, env.source_id, local(2026, 10, 20, 10, 25), window=hourly)
    assert row.interval_seconds == 3600 and row.next_due_at == local(2026, 10, 20, 11)
    handled = await _advance(db, env, env.schedule_id, local(2026, 10, 20, 11, 0, 4))
    assert handled.outcome == "enqueued" and handled.slot == local(2026, 10, 20, 11)
    assert handled.next_due_at == local(2026, 10, 20, 12)
    _finish_jobs(seed, env.schedule_id)

    async with unit_of_work(db, env.system) as conn:
        await sources_repo.sync_sources_from_yaml(conn, env.system, [source_config(env.source_key)])
    row = await _ensure(db, env, env.source_id, local(2026, 10, 20, 11, 5), window=None, interval=900)
    assert row.interval_seconds == 900
    assert row.next_due_at == local(2026, 10, 20, 11, 15)  # one interval after the last slot
    assert [a[0] for a in _audits(seed, env.schedule_id)] == ["schedule.reconfigure"] * 3


async def test_a_window_over_budget_is_skipped_with_more_profiles(db: Database, env: Env, seed: Seed) -> None:
    """28 runs x 2 pages = 56 of the synthetic 100 search requests (half of 200) with one
    profile; a second enabled profile needs 112: every run is refused, never stretched."""
    source_id = await _windowed(db, env)
    schedule = await _ensure(db, env, source_id, local(2026, 10, 20, 8))
    seed.conn.execute(
        "update app.search_profiles set enabled = true where id = %s", (env.profiles["manual_4000"],)
    )
    refused = await _advance(db, env, schedule.id, local(2026, 10, 20, 8, 0, 2))
    assert refused.outcome == "skipped" and refused.skip_reason == "schedule_over_budget"
    assert refused.next_due_at == local(2026, 10, 20, 8, 30) and refused.job_id is None
    assert "slot_skipped: schedule_over_budget" in _row(seed, schedule.id)["gap_reasons"]
    seed.conn.execute(
        "update app.search_profiles set enabled = false where id = %s", (env.profiles["manual_4000"],)
    )
    allowed = await _advance(db, env, schedule.id, local(2026, 10, 20, 8, 30, 2))
    assert allowed.outcome == "enqueued"
    assert _jobs(seed, schedule.id) == [local(2026, 10, 20, 8, 30)]
