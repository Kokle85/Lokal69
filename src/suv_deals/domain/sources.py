"""Source registry configuration (spec section 5) and discovery cadence (spec section 9).

Terms decisions and technical status are recorded independently. A stored
owner acknowledgement audits a decision; it is not legal permission.

Discovery cadence: a source without a ``schedule`` block runs at the global scheduler interval,
stretched to its daily request budget (`discovery_interval_seconds`). A source WITH one
(`DiscoverySchedule`, an owner decision) runs at fixed wall-clock slots inside a daily window in
its own IANA time zone; the scheduler computes those slots from database time
(`DiscoverySchedule.live_slot` / `next_slot_after`). An explicit schedule is never stretched: one
that does not fit the budget is a configuration error (`schedule_budget_problem`).
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from math import ceil
from pathlib import Path
from typing import Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from suv_deals.domain.enums import SourceMode, TechnicalStatus, TermsDecision, TermsStatus


class RateBudget(BaseModel):
    """Engineering defaults, not provider-approved quotas."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_concurrency_per_host: int = Field(default=1, ge=1, le=1)
    min_delay_seconds: int = Field(default=20, ge=5, le=3600)
    max_search_pages_per_run: int = Field(default=2, ge=1, le=20)
    max_detail_jobs_per_run: int = Field(default=10, ge=0, le=200)
    daily_request_budget: int = Field(default=200, ge=0, le=20000)
    daily_byte_budget: int = Field(default=200_000_000, ge=0)
    request_timeout_seconds: int = Field(default=45, ge=5, le=180)
    max_response_bytes: int = Field(default=8_000_000, ge=10_000, le=50_000_000)
    max_redirects: int = Field(default=3, ge=0, le=5)


MINUTES_PER_DAY: Final = 1_440
# The budget check counts the slots of every UTC day of this year (it holds both DST changes of
# any zone), because the daily request budget is counted per UTC day.
BUDGET_REFERENCE_YEAR: Final = 2026
_CLOCK_TIME_RE: Final = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def _minute_of_day(value: str, *, end: bool) -> int:
    if end and value == "24:00":
        return MINUTES_PER_DAY
    if not _CLOCK_TIME_RE.fullmatch(value):
        allowed = "'00:00' to '24:00'" if end else "'00:00' to '23:59'"
        raise ValueError(f"must be a 24-hour 'HH:MM' time from {allowed}")
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


SlotWindow = tuple[datetime, datetime]  # (slot start, end of the slot's run window), both UTC


def _wall_clock(day: date, minute: int, zone: ZoneInfo) -> datetime:
    """``day`` at ``minute`` past local midnight in ``zone``, as UTC.

    A wall-clock time that occurs twice (DST ends) is its FIRST occurrence; one that does not
    exist (DST starts) maps forward by the skipped hour (PEP 495, ``fold=0``), so it coincides
    with the slot an hour later and is counted once.
    """
    if minute >= MINUTES_PER_DAY:
        day, minute = day + timedelta(days=1), minute - MINUTES_PER_DAY
    local = datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=zone)
    return local.astimezone(UTC)


def _compute_day_slots(
    interval: int, start: int, end: int, zone_key: str, day: date
) -> tuple[SlotWindow, ...]:
    zone = ZoneInfo(zone_key)
    window_end = _wall_clock(day, end, zone)
    step = timedelta(minutes=interval)
    starts = sorted({_wall_clock(day, minute, zone) for minute in range(start, end, interval)})
    return tuple((slot, min(slot + step, window_end)) for slot in starts)


@lru_cache(maxsize=2048)
def _day_slots(interval: int, start: int, end: int, zone_key: str, day: date) -> tuple[SlotWindow, ...]:
    return _compute_day_slots(interval, start, end, zone_key, day)


@lru_cache(maxsize=256)
def _max_runs_per_utc_day(interval: int, start: int, end: int, zone_key: str) -> int:
    first, last = date(BUDGET_REFERENCE_YEAR, 1, 1), date(BUDGET_REFERENCE_YEAR, 12, 31)
    counts: Counter[date] = Counter()
    for offset in range(-1, (last - first).days + 2):
        for slot, _ in _compute_day_slots(interval, start, end, zone_key, first + timedelta(days=offset)):
            counts[slot.date()] += 1
    return max(count for day, count in counts.items() if first <= day <= last)


# The scheduler wakes on this UTC grid (`crawling.scheduler`: every SCHEDULER_INTERVAL_SECONDS
# counted from this origin, `jobs.current_slot` uses the same one).
TICK_GRID_ORIGIN: Final = datetime(2000, 1, 1, tzinfo=UTC)


@lru_cache(maxsize=64)
def _unreached_slots(
    interval: int, start: int, end: int, zone_key: str, tick_seconds: int
) -> tuple[int, datetime | None]:
    """Slots of the reference year whose run window holds no scheduler tick: (count, first one)."""
    tick = timedelta(seconds=tick_seconds)
    first, last = date(BUDGET_REFERENCE_YEAR, 1, 1), date(BUDGET_REFERENCE_YEAR, 12, 31)
    missed, example = 0, None
    for offset in range((last - first).days + 1):
        for slot, run_end in _compute_day_slots(
            interval, start, end, zone_key, first + timedelta(days=offset)
        ):
            first_tick = TICK_GRID_ORIGIN - ((TICK_GRID_ORIGIN - slot) // tick) * tick  # ceil to the grid
            if first_tick >= run_end:
                missed += 1
                example = example or slot
    return missed, example


class DiscoverySchedule(BaseModel):
    """An explicit discovery cadence for one source (an owner decision), e.g.::

        schedule:
          interval_minutes: 30
          window_start: "08:00"
          window_end: "22:00"
          timezone: Europe/Skopje

    Run slots start at ``window_start`` and every ``interval_minutes`` after it while before
    ``window_end`` (exclusive; ``"24:00"`` is the end of the day), in the wall-clock time of
    ``timezone`` on every calendar day: 08:00, 08:30, ..., 21:30 above (28 runs a day, in summer
    and winter time alike). The window cannot cross midnight. A slot's run may start until the
    next slot or the window end, whichever is first; outside that the schedule waits for the
    next slot (the next day's ``window_start`` after the window). Times must be quoted in YAML:
    an unquoted ``22:00`` is read as the number 1320.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_minutes: int = Field(ge=5, le=MINUTES_PER_DAY)
    window_start: str = "00:00"
    window_end: str = "24:00"
    timezone: str = Field(min_length=1, max_length=64)

    @field_validator("window_start", "window_end", mode="before")
    @classmethod
    def _quoted(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, str):
            raise ValueError(
                f"{info.field_name} must be a quoted 'HH:MM' string"
                " (YAML reads an unquoted 22:00 as a number)"
            )
        return value

    @field_validator("window_start")
    @classmethod
    def _start(cls, value: str) -> str:
        _minute_of_day(value, end=False)
        return value

    @field_validator("window_end")
    @classmethod
    def _end(cls, value: str) -> str:
        _minute_of_day(value, end=True)
        return value

    @field_validator("interval_minutes")
    @classmethod
    def _divides_the_day(cls, value: int) -> int:
        if MINUTES_PER_DAY % value:
            raise ValueError(
                "interval_minutes must divide the day (1440 minutes) evenly, e.g. 15, 20, 30, 60, 90 or 120"
            )
        return value

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"timezone {value!r} is not a known IANA time zone (e.g. Europe/Skopje)"
            ) from None
        return value

    @model_validator(mode="after")
    def _window(self) -> DiscoverySchedule:
        if self.start_minute >= self.end_minute:
            raise ValueError("window_start must be before window_end (a window cannot cross midnight)")
        return self

    @property
    def start_minute(self) -> int:
        return _minute_of_day(self.window_start, end=False)

    @property
    def end_minute(self) -> int:
        return _minute_of_day(self.window_end, end=True)

    @property
    def interval_seconds(self) -> int:
        return self.interval_minutes * 60

    @property
    def runs_per_day(self) -> int:
        """Nominal slots of a calendar day (a DST change inside the window can remove one that day)."""
        return len(range(self.start_minute, self.end_minute, self.interval_minutes))

    def max_runs_per_budget_day(self) -> int:
        """Most slots in any UTC day (the budget day), DST changes included."""
        return _max_runs_per_utc_day(self.interval_minutes, self.start_minute, self.end_minute, self.timezone)

    def describe(self) -> str:
        return (
            f"every {self.interval_minutes} min from {self.window_start} to {self.window_end} {self.timezone}"
        )

    def slot_windows(self, day: date) -> tuple[SlotWindow, ...]:
        """The run slots of local calendar ``day`` as (start, run-window end) UTC pairs, ascending."""
        return _day_slots(self.interval_minutes, self.start_minute, self.end_minute, self.timezone, day)

    def slots_on(self, day: date) -> tuple[datetime, ...]:
        return tuple(start for start, _ in self.slot_windows(day))

    def _around(self, instant: datetime, before: int, after: int) -> list[SlotWindow]:
        local_day = _aware(instant).astimezone(ZoneInfo(self.timezone)).date()
        return [
            pair
            for offset in range(-before, after + 1)
            for pair in self.slot_windows(local_day + timedelta(days=offset))
        ]

    def live_slot(self, now: datetime) -> datetime | None:
        """The slot whose run may start at ``now`` (start <= now < run-window end), else None."""
        now = _aware(now)
        live = [start for start, end in self._around(now, 1, 1) if start <= now < end]
        return max(live) if live else None

    def next_slot_after(self, instant: datetime) -> datetime:
        """The first slot strictly after ``instant``."""
        instant = _aware(instant)
        return min(start for start, _ in self._around(instant, 1, 2) if start > instant)

    def run_window_end(self, slot: datetime) -> datetime | None:
        """When the run of ``slot`` may no longer start (the next slot or the window end); None
        when ``slot`` is not a slot of this schedule (e.g. the window changed since)."""
        slot = _aware(slot)
        return next((end for start, end in self._around(slot, 1, 1) if start == slot), None)

    def runs_left_today(self, now: datetime) -> tuple[int, datetime | None]:
        """Slots of the UTC day of ``now`` (the budget day) whose run window has not ended yet,
        and when the last of those run windows ends (None: no run left today)."""
        now = _aware(now)
        ends = [end for start, end in self._around(now, 1, 1) if start.date() == now.date() and end > now]
        return len(ends), (max(ends) if ends else None)

    def tick_problem(self, tick_seconds: int) -> str | None:
        """Why a scheduler waking every ``tick_seconds`` on its UTC grid would never start some
        slots (a slot runs only if a tick falls inside its run window), or None."""
        missed, example = _unreached_slots(
            self.interval_minutes, self.start_minute, self.end_minute, self.timezone, tick_seconds
        )
        if not missed or example is None:
            return None
        local = example.astimezone(ZoneInfo(self.timezone)).strftime("%H:%M")
        return (
            f"the discovery schedule ({self.describe()}) has slots that no scheduler tick reaches"
            f" (SCHEDULER_INTERVAL_SECONDS={tick_seconds}: {missed} slots in {BUDGET_REFERENCE_YEAR},"
            f" e.g. {local}); use an interval_minutes that is a multiple of the scheduler interval with"
            " window_start on its grid, or lower SCHEDULER_INTERVAL_SECONDS"
        )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime is not allowed")
    return value.astimezone(UTC)


class SourceConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_key: str = Field(pattern=r"^[a-z0-9_]{3,60}$")
    display_name: str = Field(min_length=2, max_length=120)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    role: str = Field(pattern=r"^(acquisition|mk_comparable)$")
    mode: SourceMode
    adapter: str = Field(min_length=3, max_length=80)  # registry key of the adapter class
    adapter_version: str = Field(min_length=1, max_length=40)
    enabled: bool = False
    technical_status: TechnicalStatus = TechnicalStatus.UNTESTED
    terms_status: TermsStatus = TermsStatus.UNREVIEWED
    terms_url: str | None = None
    terms_reviewed_at: datetime | None = None
    terms_decision: TermsDecision = TermsDecision.PENDING
    terms_decision_actor: str | None = None
    terms_decision_note: str | None = Field(default=None, max_length=2000)
    technical_denial_policy: str = Field(default="stop_and_report", pattern=r"^stop_and_report$")
    robots_policy: str = Field(default="obey", pattern=r"^obey$")
    allowed_schemes: tuple[Literal["https", "http"], ...] = ("https",)
    allowed_hosts: tuple[str, ...] = ()
    allowed_search_paths: tuple[str, ...] = ()  # regex patterns on path (anchored)
    allowed_detail_paths: tuple[str, ...] = ()
    tracking_params: tuple[str, ...] = (
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
        "ref",
        "referrer",
    )
    source_timezone: str = "Europe/Berlin"
    # "fetch": detail pages are fetched when allowed; "card_only": robots/terms decision limits the
    # source to search-result cards (owner opens detail pages manually).
    detail_mode: Literal["fetch", "card_only"] = "fetch"
    # Optional Crawl4AI per-source options (CSS-only wait condition; never JavaScript).
    crawl_locale: str | None = Field(default=None, pattern=r"^[a-z]{2}-[A-Z]{2}$")
    crawl_timezone_id: str | None = Field(default=None, max_length=64)
    crawl_wait_for_css: str | None = Field(default=None, max_length=200)
    robots_checked_at: datetime | None = None
    robots_summary: str | None = Field(default=None, max_length=2000)
    rate_budget: RateBudget = RateBudget()
    # Explicit discovery cadence (owner decision); None = the global interval stretched to the
    # budget (`discovery_interval_seconds`).
    schedule: DiscoverySchedule | None = None
    search: dict[str, str] = Field(default_factory=dict)  # adapter-specific, documented per source
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _activation_guard(self) -> SourceConfig:
        if self.enabled:
            problems = activation_problems(self)
            if problems:
                raise ValueError(f"source {self.source_key} cannot be enabled: {'; '.join(problems)}")
        return self

    @model_validator(mode="after")
    def _schedule_fits_budget(self) -> SourceConfig:
        # The YAML cannot know how many search profiles a workspace enables: this check counts
        # ONE schedule (the minimum for any run). The scheduler re-checks with the workspace's
        # enabled profiles (`crawling.scheduler.ensure_schedules`, `sources_repo.advance_schedule`).
        if self.schedule is not None:
            problem = schedule_budget_problem(self.schedule, self.rate_budget, self.detail_mode, profiles=1)
            if problem:
                raise ValueError(f"source {self.source_key}: {problem}")
        return self


def activation_problems(cfg: SourceConfig) -> list[str]:
    """Reasons a source may not run network work. Empty list means the gate is satisfied."""
    problems: list[str] = []
    if cfg.adapter_version == "unimplemented":
        problems.append("adapter is unimplemented")
    if cfg.terms_decision in (TermsDecision.PENDING, TermsDecision.DO_NOT_USE):
        problems.append(f"terms decision is {cfg.terms_decision.value}")
    if cfg.terms_status == TermsStatus.UNREVIEWED:
        problems.append("terms not reviewed")
    if cfg.terms_decision != TermsDecision.PENDING and not cfg.terms_decision_actor:
        problems.append("terms decision has no recorded actor")
    if cfg.technical_status in (TechnicalStatus.ACCESS_BLOCKED, TechnicalStatus.PARSER_UNHEALTHY):
        problems.append(f"technical status is {cfg.technical_status.value}")
    if cfg.technical_status == TechnicalStatus.UNTESTED:
        problems.append("adapter has not passed fixture tests")
    if not cfg.allowed_hosts:
        problems.append("no allowed hosts")
    if not cfg.allowed_search_paths and cfg.role == "acquisition":
        problems.append("no allowed search paths")
    if not cfg.allowed_detail_paths and cfg.detail_mode == "fetch":
        problems.append("no allowed detail paths")
    return problems


SECONDS_PER_DAY: Final = 86_400


def search_request_share(budget: RateBudget, detail_mode: str) -> int:
    """Requests a day the search pages of a source WITHOUT a discovery window may use
    (`discovery_interval_seconds`): the whole budget when it is ``card_only`` (it makes no other
    requests), half of it otherwise (the other half is left for the detail pages its runs queue;
    a rule of thumb for the stretched cadence, not a reservation)."""
    return budget.daily_request_budget if detail_mode == "card_only" else budget.daily_request_budget // 2


def window_search_share(budget: RateBudget, detail_mode: str) -> int:
    """Requests a day the search pages of a source WITH a discovery window may use: the whole
    budget when it is ``card_only``, otherwise all but one run's detail allowance
    (``max_detail_jobs_per_run``). The rest is for detail pages; the budget gate keeps detail
    requests from using what the day's remaining runs still need for their search pages
    (`DiscoverySchedule.runs_left_today`, `crawling.rate_limits.SearchReserve`)."""
    if detail_mode == "card_only":
        return budget.daily_request_budget
    return max(budget.daily_request_budget - budget.max_detail_jobs_per_run, 0)


def schedule_budget_problem(
    schedule: DiscoverySchedule, budget: RateBudget, detail_mode: str, *, profiles: int = 1
) -> str | None:
    """Why an explicit discovery schedule does not fit the daily request budget, or None.

    A day of full runs (the most slots of any UTC day x ``max_search_pages_per_run`` x one
    schedule per enabled profile) must fit `window_search_share`. An explicit owner schedule is
    never stretched to fit (unlike `discovery_interval_seconds`): it is refused instead.
    """
    runs = schedule.max_runs_per_budget_day()
    pages = budget.max_search_pages_per_run
    schedules = max(profiles, 1)
    needed = runs * pages * schedules
    share = window_search_share(budget, detail_mode)
    if needed <= share:
        return None
    rule = (
        "the whole budget, card_only"
        if detail_mode == "card_only"
        else f"all but max_detail_jobs_per_run {budget.max_detail_jobs_per_run}, kept for detail pages"
    )
    return (
        f"the discovery schedule ({schedule.describe()}) needs {runs} runs x {pages} search pages x"
        f" {schedules} profile schedule(s) = {needed} requests a day, more than the {share} that"
        f" daily_request_budget {budget.daily_request_budget} allows for search pages ({rule}); raise"
        " daily_request_budget, lower max_search_pages_per_run, or narrow the window / lengthen the interval"
    )


def discovery_interval_seconds(cfg: SourceConfig, base_seconds: int, *, profiles: int = 1) -> int:
    """Seconds between the scheduled discovery runs of each of a source's ``profiles`` schedules.

    A source with an explicit ``schedule`` runs at its ``interval_minutes`` (inside its window;
    the slots come from `DiscoverySchedule`, never from this interval alone). Otherwise the global
    scheduler interval (``base_seconds``), unless a day of full runs at that cadence
    (``max_search_pages_per_run`` pages per run and schedule) would need more search requests than
    the source's ``daily_request_budget``. Such a source is slowed down instead of running into the
    budget every morning and leaving the rest of the day unscanned: its search pages may use
    `search_request_share` of the budget. The result is a whole number of scheduler intervals and
    at most one run a day.
    """
    if cfg.schedule is not None:
        return cfg.schedule.interval_seconds
    base = max(base_seconds, 60)
    budget = cfg.rate_budget
    pages = budget.max_search_pages_per_run * max(profiles, 1)
    if (
        budget.daily_request_budget <= 0
        or pages * ceil(SECONDS_PER_DAY / base) <= budget.daily_request_budget
    ):
        return base
    share = search_request_share(budget, cfg.detail_mode)
    runs = max(share // pages, 1)
    return min(ceil(ceil(SECONDS_PER_DAY / runs) / base) * base, SECONDS_PER_DAY)


def load_source_configs(sources_dir: Path) -> list[SourceConfig]:
    configs: list[SourceConfig] = []
    for path in sorted(sources_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        configs.append(SourceConfig.model_validate(data))
    keys = [c.source_key for c in configs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate source_key in config/sources")
    return configs
