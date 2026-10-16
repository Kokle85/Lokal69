# ruff: noqa: RUF002
# (RUF002: the owner's decision is quoted verbatim in Macedonian, Cyrillic by design.)
"""Explicit discovery windows (owner decision of 2026-10-16; no database, no network).

"сакам на секои 30 минуту да го наместиш само од 08:00 до 22:00": AutoScout24 Italy and Germany
run every 30 minutes, slots 08:00, 08:30, ..., 21:30 Europe/Skopje (28 a day; 22:00 is the
exclusive end), 12 search pages each, so 336 search requests a day. These tests pin the slot
math (window edges, both 2026/2027 DST changes of Europe/Skopje, downtime), the configuration
rules (budget fit, window, zone) and the reconciliation of an existing schedule row.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import yaml
from pydantic import ValidationError

from suv_deals.adapters.registry import load_registry
from suv_deals.crawling import scheduler
from suv_deals.domain.enums import CoverageMode
from suv_deals.domain.sources import (
    DiscoverySchedule,
    RateBudget,
    SourceConfig,
    discovery_interval_seconds,
    load_source_configs,
    schedule_budget_problem,
)
from suv_deals.errors import ValidationFailed
from suv_deals.persistence import sources_repo
from suv_deals.persistence.sources_repo import ScheduleRecord

REPO = Path(__file__).resolve().parents[2]
CONFIGS = {c.source_key: c for c in load_source_configs(REPO / "config" / "sources")}
SKOPJE = ZoneInfo("Europe/Skopje")
OWNER = DiscoverySchedule(
    interval_minutes=30, window_start="08:00", window_end="22:00", timezone="Europe/Skopje"
)


def local(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """A Europe/Skopje wall-clock time as UTC (unambiguous times only)."""
    return datetime(year, month, day, hour, minute, second, tzinfo=SKOPJE).astimezone(UTC)


def wall(instant: datetime) -> str:
    return instant.astimezone(SKOPJE).strftime("%Y-%m-%d %H:%M %Z")


# --------------------------------------------------------------------------------------------
# Shipped configuration
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["autoscout24_it", "autoscout24_de"])
def test_autoscout24_follow_the_owner_window(key: str) -> None:
    cfg = CONFIGS[key]
    assert cfg.schedule == OWNER
    assert cfg.schedule.runs_per_day == 28 and cfg.schedule.max_runs_per_budget_day() == 28
    assert cfg.rate_budget.max_search_pages_per_run == 12
    assert 28 * 12 == 336
    assert discovery_interval_seconds(cfg, 900) == 1800  # never stretched
    # Unchanged safety settings: 20 s spacing, one connection per host, robots obeyed.
    assert cfg.rate_budget.min_delay_seconds == 20 and cfg.rate_budget.max_concurrency_per_host == 1
    assert cfg.robots_policy == "obey" and cfg.technical_denial_policy == "stop_and_report"
    assert not cfg.enabled


def test_autoscout24_budgets_fit_336_search_requests_a_day() -> None:
    de, it = CONFIGS["autoscout24_de"], CONFIGS["autoscout24_it"]
    # Germany is card_only (search pages only): 336 of 350.
    assert de.detail_mode == "card_only" and de.rate_budget.daily_request_budget == 350
    # Italy fetches detail pages: search pages may use half the budget (336 of 672), the other
    # half is left for the detail pages (at most 10 per run, 280 a day).
    assert it.detail_mode == "fetch" and it.rate_budget.daily_request_budget == 672
    assert it.rate_budget.max_detail_jobs_per_run * 28 <= 672 - 336
    for cfg in (de, it):
        assert cfg.schedule is not None
        assert schedule_budget_problem(cfg.schedule, cfg.rate_budget, cfg.detail_mode) is None
        # A second enabled search profile would double the search pages: refused, never stretched.
        problem = schedule_budget_problem(cfg.schedule, cfg.rate_budget, cfg.detail_mode, profiles=2)
        assert problem is not None and "672 requests a day" in problem


def test_sources_without_a_window_are_unchanged() -> None:
    plain = [c for c in CONFIGS.values() if c.schedule is None]
    assert {c.source_key for c in CONFIGS.values() if c.schedule is not None} == {
        "autoscout24_de",
        "autoscout24_it",
    }
    assert len(plain) == len(CONFIGS) - 2
    for cfg in plain:
        assert discovery_interval_seconds(cfg, 900) == 900  # the global interval, as before
        # The stored registry configuration of such a source has no ``schedule`` key at all
        # (no registry version bump for sources that did not change).
        assert "schedule" not in sources_repo._sync_values(cfg)["config"]
    stored = sources_repo._sync_values(CONFIGS["autoscout24_it"])["config"]
    assert stored["schedule"] == {
        "interval_minutes": 30,
        "window_start": "08:00",
        "window_end": "22:00",
        "timezone": "Europe/Skopje",
    }


def test_the_time_zone_database_ships_with_the_application() -> None:
    """``tzdata`` is a runtime dependency: a slim container image may have no system zone files."""
    assert importlib.metadata.version("tzdata")
    assert ZoneInfo("Europe/Skopje").utcoffset(datetime(2026, 7, 1)) == timedelta(hours=2)


# --------------------------------------------------------------------------------------------
# Slots
# --------------------------------------------------------------------------------------------


def test_a_day_has_28_slots_from_08_00_to_21_30() -> None:
    slots = OWNER.slots_on(date(2026, 10, 20))
    assert len(slots) == 28
    assert [wall(s)[11:16] for s in slots] == [f"{h:02d}:{m:02d}" for h in range(8, 22) for m in (0, 30)]


@pytest.mark.parametrize(
    ("at", "live", "following"),
    [
        # before the window: nothing runs, the first slot of the day is next
        (local(2026, 10, 20, 7, 59, 59), None, local(2026, 10, 20, 8)),
        (local(2026, 10, 20, 0, 0), None, local(2026, 10, 20, 8)),
        # at the window start
        (local(2026, 10, 20, 8), local(2026, 10, 20, 8), local(2026, 10, 20, 8, 30)),
        # inside the window: the slot that started last is live until the next one
        (local(2026, 10, 20, 13, 47), local(2026, 10, 20, 13, 30), local(2026, 10, 20, 14)),
        (local(2026, 10, 20, 14, 0), local(2026, 10, 20, 14), local(2026, 10, 20, 14, 30)),
        # the last slot (21:30) is live until the window end
        (local(2026, 10, 20, 21, 59, 59), local(2026, 10, 20, 21, 30), local(2026, 10, 21, 8)),
        # at the end (22:00 is exclusive) and after it: the next day's 08:00
        (local(2026, 10, 20, 22, 0), None, local(2026, 10, 21, 8)),
        (local(2026, 10, 20, 23, 15), None, local(2026, 10, 21, 8)),
    ],
)
def test_live_and_next_slot(at: datetime, live: datetime | None, following: datetime) -> None:
    assert OWNER.live_slot(at) == live
    assert OWNER.next_slot_after(at) == following


def test_downtime_runs_the_current_slot_once_never_a_burst() -> None:
    """Scheduler down from just after the 09:00 run until 11:10: only the live 11:00 slot may run;
    the 09:30-10:30 slots stay missed (a coverage gap), the next due time is 11:30."""
    last = local(2026, 10, 20, 9)
    back = local(2026, 10, 20, 11, 10)
    assert OWNER.live_slot(back) == local(2026, 10, 20, 11)
    assert OWNER.next_slot_after(OWNER.live_slot(back) or back) == local(2026, 10, 20, 11, 30)
    missed = [s for s in OWNER.slots_on(date(2026, 10, 20)) if last < s < local(2026, 10, 20, 11)]
    assert [wall(s)[11:16] for s in missed] == ["09:30", "10:00", "10:30"]  # recorded, not replayed
    # Down over the window end: nothing runs at 22:10; tomorrow's 08:00 is next.
    assert OWNER.live_slot(local(2026, 10, 20, 22, 10)) is None
    assert OWNER.next_slot_after(local(2026, 10, 20, 22, 10)) == local(2026, 10, 21, 8)


def test_dst_end_2026_10_25() -> None:
    """CEST -> CET at 03:00 local on 2026-10-25: the window stays 08:00-22:00 local time."""
    before, after = OWNER.slots_on(date(2026, 10, 24)), OWNER.slots_on(date(2026, 10, 25))
    assert (before[0], before[-1]) == (
        datetime(2026, 10, 24, 6, tzinfo=UTC),  # 08:00 CEST
        datetime(2026, 10, 24, 19, 30, tzinfo=UTC),  # 21:30 CEST
    )
    assert (after[0], after[-1]) == (
        datetime(2026, 10, 25, 7, tzinfo=UTC),  # 08:00 CET
        datetime(2026, 10, 25, 20, 30, tzinfo=UTC),  # 21:30 CET
    )
    assert len(before) == len(after) == 28
    assert {wall(s)[11:16] for s in after} == {wall(s)[11:16] for s in before}
    # The night between them is 25 hours long: 21:30 CEST -> 08:00 CET is 11.5 h + 1 h.
    following = OWNER.next_slot_after(before[-1])
    assert following == after[0] and following - before[-1] == timedelta(hours=11, minutes=30)
    assert OWNER.live_slot(datetime(2026, 10, 25, 6, 30, tzinfo=UTC)) is None  # 07:30 CET
    assert OWNER.live_slot(datetime(2026, 10, 25, 7, 10, tzinfo=UTC)) == after[0]


def test_dst_start_2027_03_28() -> None:
    """CET -> CEST at 02:00 local on 2027-03-28 (the night is 23 hours long)."""
    before, after = OWNER.slots_on(date(2027, 3, 27)), OWNER.slots_on(date(2027, 3, 28))
    assert (before[0], before[-1]) == (
        datetime(2027, 3, 27, 7, tzinfo=UTC),  # 08:00 CET
        datetime(2027, 3, 27, 20, 30, tzinfo=UTC),  # 21:30 CET
    )
    assert (after[0], after[-1]) == (
        datetime(2027, 3, 28, 6, tzinfo=UTC),  # 08:00 CEST
        datetime(2027, 3, 28, 19, 30, tzinfo=UTC),  # 21:30 CEST
    )
    assert len(after) == 28
    following = OWNER.next_slot_after(before[-1])
    assert following == after[0] and following - before[-1] == timedelta(hours=9, minutes=30)
    assert OWNER.live_slot(datetime(2027, 3, 28, 19, 59, tzinfo=UTC)) == after[-1]  # 21:59 CEST
    assert OWNER.live_slot(datetime(2027, 3, 28, 20, 0, tzinfo=UTC)) is None  # 22:00 CEST


def test_a_window_over_the_dst_hour_counts_each_wall_clock_slot_once() -> None:
    """02:00-03:00 local does not matter for 08:00-22:00, but the date math must hold for any
    window: a repeated hour is not run twice, a skipped hour's slots coincide with the next."""
    whole_day = DiscoverySchedule(interval_minutes=30, timezone="Europe/Skopje")
    assert whole_day.runs_per_day == 48
    autumn = whole_day.slots_on(date(2026, 10, 25))
    assert len(autumn) == len(set(autumn)) == 48  # 02:00 and 02:30 once (the first, CEST)
    assert datetime(2026, 10, 25, 1, tzinfo=UTC) not in autumn  # 02:00 CET (the repeat)
    spring = whole_day.slots_on(date(2027, 3, 28))
    assert len(spring) == 46  # 02:00 and 02:30 do not exist
    assert spring == tuple(sorted(spring))
    assert whole_day.max_runs_per_budget_day() == 48


def test_budget_day_is_the_utc_day() -> None:
    """The request budget is counted per UTC day: a window crossing UTC midnight can have more
    runs in one UTC day than in one local day when DST starts, and the budget check counts that."""
    assert OWNER.max_runs_per_budget_day() == 28  # 06:00-20:00 / 07:00-21:00 UTC, one UTC day
    evening = DiscoverySchedule(interval_minutes=30, window_start="18:00", timezone="America/New_York")
    assert evening.runs_per_day == 12
    assert evening.max_runs_per_budget_day() == 14
    budget = RateBudget(max_search_pages_per_run=1, daily_request_budget=13)
    problem = schedule_budget_problem(evening, budget, "card_only")
    assert problem is not None and "14 runs" in problem


def test_naive_times_are_refused() -> None:
    with pytest.raises(ValueError, match="naive"):
        OWNER.live_slot(datetime(2026, 10, 20, 8))  # a naive datetime is the point of the test


# --------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"interval_minutes": 7}, "divide the day"),
        ({"interval_minutes": 0}, "greater than or equal to 5"),
        ({"window_start": "22:00", "window_end": "08:00"}, "before window_end"),
        ({"window_start": "08:00", "window_end": "08:00"}, "before window_end"),
        ({"window_start": "8:00"}, "HH:MM"),
        ({"window_start": "24:00"}, "HH:MM"),
        ({"window_end": "24:30"}, "HH:MM"),
        ({"window_end": 1320}, "quoted"),  # YAML reads an unquoted 22:00 as 1320
        ({"timezone": "Mars/Olympus_Mons"}, "IANA"),
        ({"timezone": "../../etc/passwd"}, "IANA"),
        ({"days": "weekdays"}, "Extra inputs"),
    ],
)
def test_invalid_schedules_are_refused(values: dict[str, Any], message: str) -> None:
    data: dict[str, Any] = {
        "interval_minutes": 30,
        "window_start": "08:00",
        "window_end": "22:00",
        "timezone": "Europe/Skopje",
    }
    data.update(values)
    with pytest.raises(ValidationError, match=message):
        DiscoverySchedule.model_validate(data)


def test_unquoted_yaml_times_are_explained() -> None:
    parsed = yaml.safe_load("interval_minutes: 30\nwindow_start: '08:00'\nwindow_end: 22:00\ntimezone: UTC\n")
    assert parsed["window_end"] == 1320
    with pytest.raises(ValidationError, match="quoted"):
        DiscoverySchedule.model_validate(parsed)


def test_end_of_day_window() -> None:
    late = DiscoverySchedule(interval_minutes=60, window_start="20:00", window_end="24:00", timezone="UTC")
    assert late.runs_per_day == 4
    assert late.live_slot(datetime(2026, 10, 20, 23, 59, tzinfo=UTC)) == datetime(
        2026, 10, 20, 23, tzinfo=UTC
    )
    assert late.next_slot_after(datetime(2026, 10, 20, 23, tzinfo=UTC)) == datetime(
        2026, 10, 21, 20, tzinfo=UTC
    )


def _with(cfg: SourceConfig, **updates: Any) -> SourceConfig:
    data = cfg.model_dump()
    budget = data.pop("rate_budget")
    budget.update(updates.pop("rate_budget", {}))
    return SourceConfig.model_validate({**data, "rate_budget": budget, **updates})


def test_a_schedule_must_fit_the_budget_at_config_load() -> None:
    de, it = CONFIGS["autoscout24_de"], CONFIGS["autoscout24_it"]
    assert _with(de, rate_budget={"daily_request_budget": 336}).schedule == OWNER  # exactly fits
    with pytest.raises(ValidationError, match=r"autoscout24_de: the discovery schedule .* 336 requests"):
        _with(de, rate_budget={"daily_request_budget": 335})
    # Italy fetches detail pages: 600 leaves only 300 for search pages (half), less than 336.
    with pytest.raises(ValidationError, match=r"more than the 300 .*\(half of it"):
        _with(it, rate_budget={"daily_request_budget": 600})
    # An explicit schedule is never stretched to fit: the cure is the budget, pages or window.
    hourly = OWNER.model_copy(update={"interval_minutes": 60})
    assert _with(it, rate_budget={"daily_request_budget": 600}, schedule=hourly).schedule == hourly
    with pytest.raises(ValidationError, match="discovery schedule"):
        _with(it, rate_budget={"max_search_pages_per_run": 20})


def test_registry_load_names_the_schedule_problem(tmp_path: Path) -> None:
    data = yaml.safe_load((REPO / "config" / "sources" / "autoscout24_de.yaml").read_text(encoding="utf-8"))
    data["rate_budget"]["daily_request_budget"] = 200
    (tmp_path / "autoscout24_de.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValidationFailed) as caught:
        load_registry(tmp_path)
    assert "invalid source configuration: 1 error(s)" in caught.value.message
    assert "336 requests a day, more than the 200" in caught.value.message


# --------------------------------------------------------------------------------------------
# Existing schedule rows follow the configured cadence (pure part of ensure_schedule)
# --------------------------------------------------------------------------------------------


def _row(*, interval: int, next_due_at: datetime, last_slot: datetime | None) -> ScheduleRecord:
    return ScheduleRecord(
        id=uuid4(),
        workspace_id=uuid4(),
        source_id=uuid4(),
        profile_id=uuid4(),
        partition_key="default",
        interval_seconds=interval,
        next_due_at=next_due_at,
        last_slot=last_slot,
        coverage_mode=CoverageMode.ROLLING_PAGES,
        consecutive_failures=0,
        paused=False,
        row_version=3,
        created_at=last_slot or next_due_at,
        updated_at=last_slot or next_due_at,
    )


def test_an_unchanged_windowed_row_is_left_alone() -> None:
    now = local(2026, 10, 20, 8, 10)
    row = _row(interval=1800, next_due_at=local(2026, 10, 20, 8, 30), last_slot=local(2026, 10, 20, 8))
    assert sources_repo._cadence_due(row, 1800, OWNER, now) == row.next_due_at
    due_now = _row(interval=1800, next_due_at=local(2026, 10, 20, 7), last_slot=None)
    assert sources_repo._cadence_due(due_now, 1800, OWNER, now) == due_now.next_due_at  # advance decides


def test_an_interval_change_moves_a_row_to_the_new_slots() -> None:
    now = local(2026, 10, 20, 8, 10)
    row = _row(interval=1800, next_due_at=local(2026, 10, 20, 8, 30), last_slot=local(2026, 10, 20, 8))
    hourly = OWNER.model_copy(update={"interval_minutes": 60})
    assert sources_repo._cadence_due(row, 3600, hourly, now) == local(2026, 10, 20, 9)


def test_a_row_from_the_old_cadence_joins_the_window() -> None:
    """The 17f4e6a cadence (every 90 minutes, all day) -> the owner's window."""
    now = local(2026, 10, 20, 6, 50)
    old = _row(interval=5400, next_due_at=local(2026, 10, 20, 7, 30), last_slot=local(2026, 10, 20, 6))
    assert sources_repo._cadence_due(old, 1800, OWNER, now) == local(2026, 10, 20, 8)
    # Inside the window, a live slot that was not handled yet is due at once (one run).
    now = local(2026, 10, 20, 10, 40)
    old = _row(interval=5400, next_due_at=local(2026, 10, 20, 11, 30), last_slot=local(2026, 10, 20, 10))
    assert sources_repo._cadence_due(old, 1800, OWNER, now) == local(2026, 10, 20, 10, 30)


def test_removing_the_window_returns_to_the_interval() -> None:
    now = local(2026, 10, 20, 22, 5)
    windowed = _row(interval=1800, next_due_at=local(2026, 10, 21, 8), last_slot=local(2026, 10, 20, 21, 30))
    assert sources_repo._cadence_due(windowed, 900, None, now) == local(2026, 10, 20, 21, 45)
    fresh = _row(interval=1800, next_due_at=local(2026, 10, 21, 8), last_slot=None)
    assert sources_repo._cadence_due(fresh, 900, None, now) == now


# --------------------------------------------------------------------------------------------
# ensure_schedules: the real number of enabled profiles
# --------------------------------------------------------------------------------------------


def _record(cfg: SourceConfig) -> Any:
    return SimpleNamespace(
        id=cfg.source_key,
        source_key=cfg.source_key,
        enabled=True,
        paused=False,
        role=cfg.role,
        source_config=lambda: cfg,
    )


async def _ensure_with_profiles(monkeypatch: pytest.MonkeyPatch, enabled: int) -> tuple[Any, list[str]]:
    created: list[str] = []

    @asynccontextmanager
    async def fake_unit_of_work(db: Any, actor: Any) -> AsyncIterator[object]:
        yield object()

    async def fake_profiles(conn: Any, actor: Any) -> list[Any]:
        return [SimpleNamespace(id=uuid4(), enabled=True) for _ in range(enabled)]

    async def fake_ensure(conn: Any, actor: Any, source_id: Any, *a: Any, **kw: Any) -> None:
        created.append(str(source_id))

    monkeypatch.setattr(scheduler, "unit_of_work", fake_unit_of_work)
    monkeypatch.setattr(scheduler.config_repo, "list_profiles", fake_profiles)
    monkeypatch.setattr(scheduler.sources_repo, "ensure_schedule", fake_ensure)
    keys = ("autoscout24_de", "autoscout24_it", "example_dealer_template_de")
    db: Any = object()
    result = await scheduler.ensure_schedules(db, db, [_record(CONFIGS[k]) for k in keys], 900)
    return result, created


async def test_ensure_schedules_refuses_a_window_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two enabled profiles would double AutoScout24's 336 search requests: both windows are
    refused (reported, no row created or changed); the plain source is ensured as before."""
    result, created = await _ensure_with_profiles(monkeypatch, enabled=2)
    assert result == scheduler.EnsuredSchedules(ensured=2, over_budget=("autoscout24_de", "autoscout24_it"))
    assert created == ["example_dealer_template_de", "example_dealer_template_de"]
    fits, created = await _ensure_with_profiles(monkeypatch, enabled=1)
    assert fits == scheduler.EnsuredSchedules(ensured=3)
    assert created == ["autoscout24_de", "autoscout24_it", "example_dealer_template_de"]
