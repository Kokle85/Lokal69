"""Job worker: claim, heartbeat, dispatch, fence and shut down gracefully (spec 13, 30, 31 "Queue").

- **Claims** are per workspace (ADR 0001): the worker visits active workspaces round-robin and leases
  the next due job of its job types with `jobs.claim` (``FOR UPDATE SKIP LOCKED``, fresh token,
  ``attempts + 1``). The payload versions every handler understands are passed along, so an
  incompatible job is moved to ``blocked`` (``incompatible_payload_version``) by the claim itself.
- **Heartbeats** extend the lease every ``heartbeat_seconds`` while the handler works. A failed
  heartbeat means the lease is lost: the handler is cancelled (its network work stops) and nothing it
  would still commit can pass the fenced updates (`LeaseLost` rolls its transaction back).
- **Dispatch** goes through the `HandlerRegistry`. Handlers commit their own outcome together with
  their domain writes; an exception that escapes a handler is mapped by `disposition_for_error` to
  ``retry_wait`` (with backoff / Retry-After), ``blocked`` (typed blocker, no timer) or
  ``dead_letter`` (visible), in a separate short transaction that again locks the job first. A
  budget-gate refusal (`BudgetRefused`: nothing was fetched) that lifts on its own is the
  exception: the job is released to ``queued`` at the gate's time WITHOUT consuming an attempt
  (`detail.apply_budget_refusal`); an access-blocked host blocks it and a refusal without a known
  end (configuration, e.g. a zero per-run cap) is retried with the attempt counted.
- **Shutdown**: setting the stop event (SIGTERM/SIGINT in `run_worker`) lets the current job finish its
  commit; no new job is claimed. A job interrupted by a crash is recovered by the reaper
  (`workers.reconciliation`), which requeues it while attempts remain.
- **Observability**: structured logs carry ``job_id``/``request_id`` (and the handler adds
  ``run_id``/``source_key``); lease expirations and dead letters are counted.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
from collections.abc import Collection
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import anyio

from suv_deals.clock import ensure_utc
from suv_deals.crawling.detail import apply_budget_refusal
from suv_deals.crawling.policy_client import BudgetRefused
from suv_deals.domain.enums import JobState, JobType
from suv_deals.errors import AppError
from suv_deals.observability.logging import log_context
from suv_deals.persistence import jobs
from suv_deals.persistence.errors_map import LeaseLost
from suv_deals.persistence.jobs import ClaimedJob
from suv_deals.persistence.transactions import job_unit_of_work, retry_transient
from suv_deals.settings import Settings
from suv_deals.workers.handlers import HandlerRegistry, default_registry
from suv_deals.workers.runtime import (
    JobExecution,
    JobOutcome,
    RuntimeContext,
    active_workspace_ids,
    apply_disposition,
    build_runtime,
    disposition_for_error,
    system_actor,
)

logger = logging.getLogger(__name__)


def default_worker_id() -> str:
    return f"worker:{socket.gethostname()[:60]}:{os.getpid()}:{uuid4().hex[:8]}"


@dataclass(frozen=True, slots=True)
class JobRunReport:
    """What happened to one claimed job."""

    job_id: UUID
    workspace_id: UUID
    job_type: JobType
    attempt: int
    state: JobState | None  # None: the lease was lost (the reaper recovers the job)
    code: str | None = None
    lease_lost: bool = False
    details: dict[str, object] = field(default_factory=dict)


class Worker:
    """One worker loop. ``run_once`` processes at most one job (useful for tests and CLI)."""

    def __init__(
        self,
        ctx: RuntimeContext,
        registry: HandlerRegistry | None = None,
        *,
        worker_id: str | None = None,
        job_types: Collection[JobType] | None = None,
        workspace_ids: Collection[UUID] | None = None,
    ) -> None:
        self.ctx = ctx
        #: Optional restriction to some workspaces (still only ACTIVE ones are visited).
        self.workspace_ids = None if workspace_ids is None else frozenset(workspace_ids)
        self.registry = registry or default_registry()
        self.worker_id = worker_id or default_worker_id()
        wanted = tuple(self.registry.job_types() if job_types is None else job_types)
        unknown = [t for t in wanted if t not in self.registry]
        if unknown or not wanted:
            raise ValueError(f"no handler registered for job types {sorted(str(t) for t in unknown)}")
        self.job_types = wanted
        self._versions = self.registry.payload_versions(wanted)
        self._cursor = 0

    # ------------------------------------------------------------------ claiming

    async def claim_next(self) -> ClaimedJob | None:
        """Round-robin over active workspaces; the first due job wins."""
        workspaces = await active_workspace_ids(self.ctx.db)
        if self.workspace_ids is not None:
            workspaces = [w for w in workspaces if w in self.workspace_ids]
        if not workspaces:
            return None
        start = self._cursor % len(workspaces)
        for offset in range(len(workspaces)):
            workspace_id = workspaces[(start + offset) % len(workspaces)]
            job = await jobs.claim(
                self.ctx.db,
                workspace_id,
                self.worker_id,
                self.job_types,
                self.ctx.options.job_lease_seconds,
                payload_versions=self._versions,
            )
            if job is not None:
                self._cursor = start + offset + 1
                return job
        self._cursor = start + 1
        return None

    async def run_once(self) -> JobRunReport | None:
        job = await self.claim_next()
        return None if job is None else await self.process(job)

    async def run_until_idle(self, *, max_jobs: int = 1000) -> list[JobRunReport]:
        """Process due jobs until none is claimable (tests, ``worker --drain``)."""
        reports: list[JobRunReport] = []
        while len(reports) < max_jobs:
            report = await self.run_once()
            if report is None:
                break
            reports.append(report)
        return reports

    async def run(self, stop: anyio.Event) -> None:
        """Process jobs until ``stop`` is set; an idle worker polls every ``idle_poll_seconds``."""
        while not stop.is_set():
            try:
                report = await self.run_once()
            except AppError as exc:  # e.g. the database is briefly unavailable
                logger.warning("worker cycle failed", extra={"error_code": exc.code.value})
                report = None
            if report is None:
                with anyio.move_on_after(self.ctx.options.idle_poll_seconds):
                    await stop.wait()

    # ------------------------------------------------------------------ processing

    async def process(self, job: ClaimedJob) -> JobRunReport:
        spec = self.registry.get(job.job_type)
        actor = system_actor(job.workspace_id, f"job:{job.job_type.value}")
        execution = JobExecution(
            job=job, worker_id=self.worker_id, actor=actor, started_at=ensure_utc(self.ctx.clock.now())
        )
        outcome: JobOutcome | None = None
        error: BaseException | None = None
        with log_context(job_id=job.id, request_id=actor.request_id):
            logger.info("job started", extra={"job_type": job.job_type.value, "attempt": job.attempts})
            async with anyio.create_task_group() as tg:

                async def run_handler() -> None:
                    nonlocal outcome, error
                    try:
                        outcome = await spec.handler(self.ctx, execution)
                    except Exception as exc:  # mapped below; cancellation is never swallowed
                        error = exc
                    finally:
                        tg.cancel_scope.cancel()

                tg.start_soon(self._heartbeat, execution, tg.cancel_scope)
                tg.start_soon(run_handler)
            report = await self._finish(execution, outcome, error)
            logger.info(
                "job finished",
                extra={
                    "job_type": job.job_type.value,
                    "state": None if report.state is None else report.state.value,
                    "code": report.code,
                    "lease_lost": report.lease_lost,
                },
            )
        return report

    async def _heartbeat(self, execution: JobExecution, scope: anyio.CancelScope) -> None:
        options = self.ctx.options
        # The lease expires at most ``job_lease_seconds`` after the last successful extension was
        # answered (database time of the update <= our receipt of its answer). If heartbeats keep
        # failing past that point the lease is certainly gone, even though the database cannot say
        # so: stop the handler's network work instead of fetching for a job that another worker
        # may already hold (spec 8: cancel work whose lease is lost).
        extended_at = anyio.current_time()
        while True:
            await anyio.sleep(options.heartbeat_seconds)
            try:
                alive = await jobs.heartbeat(self.ctx.db, execution.job, options.job_lease_seconds)
            except AppError:
                if anyio.current_time() - extended_at < options.job_lease_seconds:
                    continue  # a transient database problem: try again while the lease may hold
                alive = False
            if not alive:
                execution.mark_lost()
                scope.cancel()  # stop the handler's network work; nothing more may be committed
                return
            extended_at = anyio.current_time()

    async def _finish(
        self, execution: JobExecution, outcome: JobOutcome | None, error: BaseException | None
    ) -> JobRunReport:
        job = execution.job
        if outcome is not None:
            if outcome.state == JobState.DEAD_LETTER:
                self.ctx.metrics.record_dead_letter(job.job_type)
            return _report(job, outcome.state, code=outcome.code, details=dict(outcome.details))
        if execution.lease_lost or error is None or isinstance(error, LeaseLost):
            # A lost lease (or a cancellation without an error): nothing may be committed; the
            # reaper recovers the job while attempts remain.
            return self._lost(job)
        if isinstance(error, BudgetRefused):
            return await self._release_refused(execution, error)
        disposition = disposition_for_error(error, job)
        if disposition is None:
            return self._lost(job)
        logger.warning(
            "job failed",
            extra={"job_type": job.job_type.value, "disposition": disposition.kind, "code": disposition.code},
        )

        async def commit() -> JobState:
            async with job_unit_of_work(self.ctx.db, job) as (conn, _locked):
                return await apply_disposition(conn, job, disposition)

        try:
            state = await retry_transient(commit)
        except LeaseLost:
            return self._lost(job)
        if state == JobState.DEAD_LETTER:
            self.ctx.metrics.record_dead_letter(job.job_type)
        return _report(job, state, code=disposition.code)

    async def _release_refused(self, execution: JobExecution, refused: BudgetRefused) -> JobRunReport:
        """Nothing was fetched: `detail.apply_budget_refusal` (release, retry or block)."""
        job = execution.job

        async def commit() -> tuple[JobState, str | None]:
            async with job_unit_of_work(self.ctx.db, job) as (conn, _locked):
                return await apply_budget_refusal(conn, job, refused, execution.actor)

        try:
            state, code = await retry_transient(commit)
        except LeaseLost:
            return self._lost(job)
        logger.info(
            "job released after a budget refusal", extra={"job_type": job.job_type.value, "code": code}
        )
        return _report(job, state, code=code)

    def _lost(self, job: ClaimedJob) -> JobRunReport:
        logger.warning("job lease lost; nothing committed, the reaper recovers it")
        self.ctx.metrics.record_lease_expiration(job.job_type)
        return _report(job, None, lease_lost=True)


def _report(
    job: ClaimedJob,
    state: JobState | None,
    *,
    code: str | None = None,
    lease_lost: bool = False,
    details: dict[str, object] | None = None,
) -> JobRunReport:
    return JobRunReport(
        job_id=job.id,
        workspace_id=job.workspace_id,
        job_type=job.job_type,
        attempt=job.attempts,
        state=state,
        code=code,
        lease_lost=lease_lost,
        details=details or {},
    )


async def run_worker(
    settings: Settings,
    *,
    job_types: Collection[JobType] | None = None,
    worker_id: str | None = None,
    registry: HandlerRegistry | None = None,
    stop: anyio.Event | None = None,
) -> None:
    """Process entry point (``suv-deals worker``): build the runtime, run until SIGTERM/SIGINT."""
    ctx = await build_runtime(settings, application_name="suv-deals-worker", configure_logs=True)
    stop = stop or anyio.Event()
    worker = Worker(ctx, registry, worker_id=worker_id, job_types=job_types)
    try:
        async with anyio.create_task_group() as tg:

            async def watch_signals() -> None:
                with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                    async for _signum in signals:
                        logger.info("shutdown requested; finishing the current job")
                        stop.set()
                        return

            async def work() -> None:
                await worker.run(stop)
                tg.cancel_scope.cancel()

            tg.start_soon(watch_signals)
            tg.start_soon(work)
    finally:
        await ctx.aclose()


__all__ = ["JobRunReport", "Worker", "default_worker_id", "run_worker"]
