"""Discovery cadence sized to each source's daily request budget (no database, no network).

Regression: AutoScout24 Italy/Germany walk twelve model pages per run but kept the global
15-minute schedule, so their 200-request daily budget ran out after about four hours of each UTC
day and every later slot was skipped (``daily_budget_exhausted``). Since the owner decision of
2026-10-16 both run on an explicit window instead (every 30 minutes from 08:00 to 22:00
Europe/Skopje; `tests/unit/test_discovery_window.py`); a source without a ``schedule`` block keeps
the budget-stretched global interval tested here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from math import ceil
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from suv_deals.crawling import scheduler
from suv_deals.domain.sources import (
    SECONDS_PER_DAY,
    RateBudget,
    SourceConfig,
    discovery_interval_seconds,
    load_source_configs,
    window_search_share,
)

REPO = Path(__file__).resolve().parents[2]
CONFIGS = {c.source_key: c for c in load_source_configs(REPO / "config" / "sources")}
BASE = 900  # SCHEDULER_INTERVAL_SECONDS default


def _runs_per_day(interval: int) -> int:
    """Slots of one schedule in a UTC day (``date_bin`` from a midnight origin)."""
    return ceil(SECONDS_PER_DAY / interval)


@pytest.mark.parametrize("key", sorted(CONFIGS))
def test_a_day_of_scheduled_runs_fits_every_shipped_budget(key: str) -> None:
    cfg = CONFIGS[key]
    interval = discovery_interval_seconds(cfg, BASE)
    budget = cfg.rate_budget
    if cfg.schedule is not None:
        # An explicit window: its own slots, never stretched; its search pages fit the share.
        assert interval == cfg.schedule.interval_seconds
        search = budget.max_search_pages_per_run * cfg.schedule.max_runs_per_budget_day()
        assert search <= window_search_share(budget, cfg.detail_mode)
        return
    assert interval % BASE == 0 and BASE <= interval <= SECONDS_PER_DAY
    search = budget.max_search_pages_per_run * _runs_per_day(interval)
    assert search <= budget.daily_request_budget
    if interval > BASE and cfg.detail_mode == "fetch":
        # A source slowed down for its budget keeps half of it for the detail pages it queues.
        assert search <= budget.daily_request_budget // 2


def _without_window(cfg: SourceConfig, daily_request_budget: int) -> SourceConfig:
    budget = cfg.rate_budget.model_copy(update={"daily_request_budget": daily_request_budget})
    return SourceConfig.model_validate({**cfg.model_dump(), "schedule": None, "rate_budget": budget})


def test_a_large_source_without_a_window_is_spread_over_the_day() -> None:
    """The budget stretch of 17f4e6a, unchanged for a source without a ``schedule`` block (here
    AutoScout24 as it was configured before the owner's window: 200 requests a day)."""
    de = _without_window(CONFIGS["autoscout24_de"], 200)
    it = _without_window(CONFIGS["autoscout24_it"], 200)
    assert discovery_interval_seconds(de, BASE) == 5400  # 16 runs x 12 pages = 192 of 200
    assert discovery_interval_seconds(it, BASE) == 10800  # 8 runs x 12 pages = 96; 104 for details
    # Without it: 96 slots x 12 pages = 1152 requests against a budget of 200.
    assert de.rate_budget.max_search_pages_per_run * _runs_per_day(BASE) > de.rate_budget.daily_request_budget


def test_a_source_that_fits_keeps_the_global_interval() -> None:
    default = CONFIGS["example_dealer_template_de"]
    assert default.rate_budget == RateBudget()
    assert discovery_interval_seconds(default, BASE) == BASE
    assert discovery_interval_seconds(default, 3600) == 3600  # a slower global cadence is kept


def test_more_enabled_profiles_slow_a_large_source_further() -> None:
    de = _without_window(CONFIGS["autoscout24_de"], 200)
    assert discovery_interval_seconds(de, BASE, profiles=2) == 10800  # 8 runs x 2 schedules x 12
    tight = _without_window(CONFIGS["autoscout24_de"], 5)
    assert discovery_interval_seconds(tight, BASE) == SECONDS_PER_DAY  # never less than once a day


async def test_new_schedules_are_created_with_the_budget_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ensure_schedules`` passes the per-source interval (and window), not the global one."""
    created: dict[str, int] = {}
    windows: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_unit_of_work(db: Any, actor: Any) -> AsyncIterator[object]:
        yield object()

    async def fake_profiles(conn: Any, actor: Any) -> list[Any]:
        return [SimpleNamespace(id=uuid4(), enabled=True), SimpleNamespace(id=uuid4(), enabled=False)]

    async def fake_ensure(conn: Any, actor: Any, source_id: Any, profile_id: Any, *a: Any, **kw: Any) -> None:
        created[str(source_id)] = kw["interval_seconds"]
        windows[str(source_id)] = kw["window"]

    monkeypatch.setattr(scheduler, "unit_of_work", fake_unit_of_work)
    monkeypatch.setattr(scheduler.config_repo, "list_profiles", fake_profiles)
    monkeypatch.setattr(scheduler.sources_repo, "ensure_schedule", fake_ensure)

    def record(cfg: SourceConfig) -> Any:
        return SimpleNamespace(
            id=cfg.source_key,
            source_key=cfg.source_key,
            enabled=True,
            paused=False,
            role=cfg.role,
            source_config=lambda: cfg,
        )

    keys = ("autoscout24_de", "autoscout24_it", "example_dealer_template_de")
    db: Any = object()
    ensured = await scheduler.ensure_schedules(db, db, [record(CONFIGS[k]) for k in keys], BASE)
    assert ensured == scheduler.EnsuredSchedules(ensured=3)
    # AutoScout24: the owner's 30-minute window; the template keeps the global interval.
    assert created == {"autoscout24_de": 1800, "autoscout24_it": 1800, "example_dealer_template_de": BASE}
    assert CONFIGS["autoscout24_de"].schedule is not None
    assert windows["autoscout24_de"] == CONFIGS["autoscout24_de"].schedule
    assert windows["autoscout24_it"] == CONFIGS["autoscout24_it"].schedule
    assert windows["example_dealer_template_de"] is None
