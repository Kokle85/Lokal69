"""Scheduler: one discovery job per due (workspace, source, profile, partition) slot (spec 9, 13, 30).

A tick is stateless and restart-safe (nothing is remembered between ticks; database time decides
slots and due times):

1. ``ops.active_workspace_ids()`` lists the workspaces (ADR 0001 point 5); each is processed with its
   own workspace-scoped transactions.
2. A schedule row exists for every enabled acquisition source x enabled search profile (partition
   ``default``; coverage mode from the adapter's capabilities). Creating one never schedules a job.
3. For each due schedule, ONE short transaction runs `sources_repo.advance_schedule`: lock the
   schedule, re-check the source (enabled, activation gate, not paused, not access-blocked or
   parser-unhealthy, terms decision), the profile, backlog, backoff and today's request budget,
   insert the slot's discovery job through the slot-unique key, and advance ``next_due_at`` in the
   same transaction (both commit or neither does). The commit happens before any network request.
4. Missed slots after downtime are recorded as a coverage gap. Exactly one job is enqueued for the
   CURRENT slot: the scheduler never replays missed slots as a burst (never silently increases
   traffic to catch up). Racing schedulers get ``already_scheduled`` for the same slot.

Real sources are not scheduled at all while ``SOURCE_NETWORK_ENABLED=false`` (their schedules stay
due and visibly unscanned); fixture sources run offline, and never in production, whose processes
load no fixture data (`workers.runtime.fixture_runs_allowed`). One schedule that cannot be advanced
(e.g. a lock timeout that outlived the transient retries) is reported in ``skipped`` and stays due;
it never stops the remaining schedules of the workspace.
"""

from __future__ import annotations

import logging
import signal
from collections import Counter
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import anyio
from pydantic import BaseModel, ConfigDict, Field

from suv_deals.adapters.registry import build_adapter, registry_problems
from suv_deals.clock import Clock, SystemClock, ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import CoverageMode, SourceMode
from suv_deals.errors import AppError, DependencyUnavailable, NotFound
from suv_deals.observability.logging import log_context
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence import config_repo, sources_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.sources_repo import ScheduleAdvance, SourceRecord
from suv_deals.persistence.transactions import retry_transient, unit_of_work
from suv_deals.settings import Settings
from suv_deals.workers.runtime import active_workspace_ids, build_runtime, fixture_runs_allowed

logger = logging.getLogger(__name__)

DEFAULT_PARTITION: Final = "default"
NETWORK_DISABLED: Final = "source_network_disabled"
FIXTURE_IN_PRODUCTION: Final = "fixture_source_in_production"
ADVANCE_FAILED: Final = "advance_failed"


class WorkspaceTick(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: UUID
    schedules_ensured: int = 0
    enqueued: tuple[UUID, ...] = ()
    already_scheduled: int = 0
    not_due: int = 0
    skipped: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class SchedulerTickReport(BaseModel):
    """What one tick did (also the scheduler-delay and last-success metrics)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    started_at: datetime
    planned_slot: datetime
    workspaces: tuple[WorkspaceTick, ...]

    @property
    def enqueued(self) -> int:
        return sum(len(w.enqueued) for w in self.workspaces)

    @property
    def outcome(self) -> str:
        if not self.workspaces:
            return "skipped"
        failed = sum(1 for w in self.workspaces if w.error is not None)
        if failed == len(self.workspaces):
            return "failed"
        return "partial" if failed else "ok"


def _slot_of(now: datetime, interval: timedelta) -> datetime:
    epoch = datetime(2000, 1, 1, tzinfo=now.tzinfo)
    steps = (now - epoch) // interval
    return epoch + steps * interval


async def _sources(db: Database, actor: ActorContext) -> list[SourceRecord]:
    async with unit_of_work(db, actor) as conn:
        listing = await sources_repo.list_sources(conn, actor)
        records = [
            await sources_repo.get_source_record(conn, actor, item.source_id) for item in listing.items
        ]
    return records


def _coverage_mode(source: SourceRecord) -> CoverageMode | None:
    config = source.source_config()
    if registry_problems(config):
        return None
    try:
        return build_adapter(config).capabilities().coverage_mode
    except AppError:
        return None


async def ensure_schedules(
    db: Database, actor: ActorContext, sources: list[SourceRecord], interval_seconds: int
) -> int:
    """Make sure a schedule row exists for every enabled acquisition source x enabled profile.

    Idempotent (``ensure_schedule`` inserts with ``ON CONFLICT DO NOTHING``); returns the number of
    schedule rows ensured. A new row is due immediately but schedules nothing by itself.
    """
    eligible = [s for s in sources if s.enabled and not s.paused and s.role == "acquisition"]
    if not eligible:
        return 0
    ensured = 0
    async with unit_of_work(db, actor) as conn:
        profiles = [p for p in await config_repo.list_profiles(conn, actor) if p.enabled]
        for source in eligible:
            mode = _coverage_mode(source)
            if mode is None:
                continue
            for profile in profiles:
                await sources_repo.ensure_schedule(
                    conn,
                    actor,
                    source.id,
                    profile.id,
                    DEFAULT_PARTITION,
                    coverage_mode=mode,
                    interval_seconds=interval_seconds,
                )
                ensured += 1
    return ensured


async def tick_workspace(
    db: Database, settings: Settings, workspace_id: UUID, *, request_id: str | None = None
) -> WorkspaceTick:
    """One workspace's part of a tick (see module docstring)."""
    actor = ActorContext.system(workspace_id, request_id=request_id or f"scheduler:{uuid4().hex[:12]}")
    sources = await _sources(db, actor)
    ensured = await ensure_schedules(db, actor, sources, settings.scheduler_interval_seconds)
    by_id = {s.id: s for s in sources}
    async with unit_of_work(db, actor) as conn:
        due = await sources_repo.due_schedules(conn, actor)
    enqueued: list[UUID] = []
    skipped: Counter[str] = Counter()
    already = not_due = 0
    for schedule in due:
        source = by_id.get(schedule.source_id)
        if source is None:
            continue
        if source.mode != SourceMode.FIXTURE and not settings.source_network_enabled:
            # Never enqueue work that may not run; the schedule stays due (visibly not scanned).
            skipped[NETWORK_DISABLED] += 1
            continue
        if source.mode == SourceMode.FIXTURE and not fixture_runs_allowed(settings):
            # Production processes have no fixture data: a job could only block, one per slot.
            skipped[FIXTURE_IN_PRODUCTION] += 1
            continue

        async def advance(schedule_id: UUID = schedule.id) -> ScheduleAdvance:
            async with unit_of_work(db, actor) as conn:
                return await sources_repo.advance_schedule(conn, actor, schedule_id)

        with log_context(source_key=source.source_key):
            try:
                result = await retry_transient(advance)
            except NotFound:
                continue
            except DependencyUnavailable:
                raise  # the database is gone: the whole workspace tick fails visibly
            except AppError as exc:
                # One schedule never stops the others; it stays due and is retried next tick.
                logger.warning(
                    "schedule could not be advanced",
                    extra={"schedule_id": str(schedule.id), "error_code": exc.code.value},
                )
                skipped[f"{ADVANCE_FAILED}:{exc.code.value}"] += 1
                continue
        if result.outcome == "enqueued" and result.job_id is not None:
            enqueued.append(result.job_id)
            logger.info(
                "discovery job enqueued",
                extra={"slot": result.slot.isoformat(), "schedule_id": str(schedule.id)},
            )
        elif result.outcome == "already_scheduled":
            already += 1
        elif result.outcome == "not_due":
            not_due += 1
        else:
            skipped[result.skip_reason or "skipped"] += 1
    return WorkspaceTick(
        workspace_id=workspace_id,
        schedules_ensured=ensured,
        enqueued=tuple(enqueued),
        already_scheduled=already,
        not_due=not_due,
        skipped=dict(skipped),
    )


async def run_scheduler_tick(
    db: Database,
    settings: Settings,
    clock: Clock,
    *,
    metrics: AppMetrics | None = None,
    workspace_ids: Collection[UUID] | None = None,
) -> SchedulerTickReport:
    """Evaluate every due schedule of every active workspace once (stateless, restart-safe).

    ``workspace_ids`` optionally restricts the tick to some workspaces (still only active ones).
    """
    started = ensure_utc(clock.now())
    interval = timedelta(seconds=settings.scheduler_interval_seconds)
    planned = _slot_of(started, interval)
    results: list[WorkspaceTick] = []
    wanted = None if workspace_ids is None else frozenset(workspace_ids)
    for workspace_id in await active_workspace_ids(db):
        if wanted is not None and workspace_id not in wanted:
            continue
        try:
            results.append(await tick_workspace(db, settings, workspace_id))
        except AppError as exc:
            # One workspace's failure (e.g. database timeout) never stops the others.
            logger.warning("scheduler tick failed for a workspace", extra={"error_code": exc.code.value})
            results.append(WorkspaceTick(workspace_id=workspace_id, error=exc.code.value))
    report = SchedulerTickReport(started_at=started, planned_slot=planned, workspaces=tuple(results))
    if metrics is not None:
        metrics.record_scheduler_cycle(planned_at=planned, started_at=started, outcome=report.outcome)
    return report


async def run_scheduler(
    db: Database,
    settings: Settings,
    *,
    clock: Clock | None = None,
    metrics: AppMetrics | None = None,
    stop: anyio.Event | None = None,
) -> None:
    """Tick every ``SCHEDULER_INTERVAL_SECONDS`` until ``stop`` is set (graceful shutdown)."""
    clock = clock or SystemClock()
    stop = stop or anyio.Event()
    interval = float(settings.scheduler_interval_seconds)
    while not stop.is_set():
        try:
            report = await run_scheduler_tick(db, settings, clock, metrics=metrics)
            logger.info(
                "scheduler tick finished",
                extra={"enqueued": report.enqueued, "outcome": report.outcome},
            )
        except AppError as exc:
            logger.warning("scheduler tick failed", extra={"error_code": exc.code.value})
        # Sleep until the next slot boundary (database slots are the same 15-minute grid).
        now = ensure_utc(clock.now())
        next_slot = _slot_of(now, timedelta(seconds=interval)) + timedelta(seconds=interval)
        with anyio.move_on_after(max(1.0, (next_slot - now).total_seconds() + 1.0)):
            await stop.wait()


async def run_scheduler_process(settings: Settings, *, stop: anyio.Event | None = None) -> None:
    """Process entry point (``suv-deals scheduler``): build the runtime, tick until SIGTERM/SIGINT."""
    ctx = await build_runtime(settings, application_name="suv-deals-scheduler", configure_logs=True)
    stop = stop or anyio.Event()
    try:
        async with anyio.create_task_group() as tg:

            async def watch_signals() -> None:
                with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                    async for _signum in signals:
                        logger.info("shutdown requested; the scheduler stops after the current tick")
                        stop.set()
                        return

            async def work() -> None:
                await run_scheduler(ctx.db, settings, clock=ctx.clock, metrics=ctx.metrics, stop=stop)
                tg.cancel_scope.cancel()

            tg.start_soon(watch_signals)
            tg.start_soon(work)
    finally:
        await ctx.aclose()


__all__ = [
    "SchedulerTickReport",
    "WorkspaceTick",
    "ensure_schedules",
    "run_scheduler",
    "run_scheduler_process",
    "run_scheduler_tick",
    "tick_workspace",
]
