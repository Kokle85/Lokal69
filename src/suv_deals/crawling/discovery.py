"""Discovery job handler: search pages -> listing observations and detail jobs (spec 8, 9, 10, 13, 25, 30).

Three phases, never holding a transaction across network I/O:

1. **Preflight** (short read transaction): the source must still be enabled, unpaused, not
   access-blocked or parser-unhealthy, with a satisfied activation gate (`lock_source(for_network=True)`
   plus `activation_problems()`), and the profile must be enabled. A real source is refused unless
   ``SOURCE_NETWORK_ENABLED=true``; a ``mode: fixture`` source runs against saved files only
   (`RuntimeContext.crawl_session`). The crawl run is opened in its own short transaction under the
   job lease (`job_unit_of_work` locks and revalidates the job first), so the in-progress run id is
   persisted.
2. **Network** (no transaction): the adapter fetches page after page through the policy-enforcing
   client (URL policy, `DbBudgetGate`, DNS check, redirect validation), within the source's per-run
   page budget -- or the job payload's optional ``max_pages`` (``suv-deals crawl once``), which can
   only LOWER it; the cap travels with the job, so it holds for whichever worker claims it. A short
   minimum-delay wait is waited out (`call_with_budget`); any other budget refusal stops the
   traversal. A non-OK page stops it too.
3. **Commit** (one short transaction, re-run only after a proven rollback): the job row is locked
   and revalidated first; an access block is recorded on the source (``access_blocked`` + one
   deduplicated operational review item) BEFORE listing rows are touched (global lock order);
   parser-health tripwires may mark the source degraded/unhealthy; every fetch outcome is stored
   (redacted, URL hashes only); OK pages are ingested (`listings_repo.ingest_search_page` enqueues the
   spec 9 detail jobs for new or changed cards); the run is finished with its coverage outcome
   (`finish_crawl_run`: only a complete traversal advances watermarks / last complete traversal;
   budget-limited and partial runs persist the resume cursor and record the gap; failed and blocked
   runs back the schedule off); and the job outcome is written by the fenced update. A late worker
   whose lease was lost cannot commit any of it (`LeaseLost` rolls everything back).

Outcomes: access blocked -> job ``blocked`` (``access_blocked``, never retried); a budget-gate or
host-budget refusal before the first page (nothing fetched) -> the job is released to ``queued`` at the
gate's time WITHOUT consuming an attempt (`detail.apply_budget_refusal` -> `jobs.release`; an
access-blocked host blocks it); 429 on the first page
-> ``retry_wait`` at the gate's Retry-After-respecting time; transient first-page failure ->
``retry_wait`` with the gate's backoff; unexpected content / policy refusal -> the job completes with
a failed traversal (retrying would not help; the schedule backs off and parser health records it);
later-page failures keep the committed pages and finish the run ``partial``. After a COMPLETE
traversal, `mark_complete_scan_absences` runs in its own short transaction (absence never means
removed or sold).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Final, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from suv_deals.adapters.base import DiscoveryPage, ParseOutcome, ParserHealth, SearchRequest
from suv_deals.crawling.detail import apply_budget_refusal
from suv_deals.crawling.policy_client import BudgetRefused
from suv_deals.crawling.rate_limits import Deny, DenyReason
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import AccessState, Completeness, CoverageMode, JobState, TechnicalStatus
from suv_deals.domain.profiles import SearchProfile
from suv_deals.errors import AppError, NotFound, SourcePaused, ValidationFailed
from suv_deals.observability.logging import log_context
from suv_deals.persistence import config_repo, listings_repo, sources_repo
from suv_deals.persistence.database import Conn, fetch_all
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.listings_repo import IngestReport
from suv_deals.persistence.sources_repo import CrawlRunRecord, RunOutcome, SourceRecord, SourceRoute
from suv_deals.persistence.transactions import job_unit_of_work, lock_source, retry_transient, unit_of_work
from suv_deals.workers.runtime import (
    CrawlSession,
    Disposition,
    JobExecution,
    JobOutcome,
    RuntimeContext,
    apply_disposition,
    backoff_delay,
    call_with_budget,
)

logger = logging.getLogger(__name__)

#: Spec 9 PROPOSED overlap for provider watermarks (engineering default).
WATERMARK_OVERLAP: Final = timedelta(hours=48)
DEFAULT_PARTITION: Final = "default"
#: Upper bound of a payload page cap (the source's per-run budget always applies as well).
MAX_PAGE_CAP: Final = 1000


class DiscoveryPayload(BaseModel):
    """The payload `sources_repo.advance_schedule` (or ``suv-deals crawl once``) writes; unknown keys
    are ignored. ``max_pages`` can only LOWER the source's ``max_search_pages_per_run``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schedule_id: UUID | None = None
    source_id: UUID | None = None
    profile_id: UUID | None = None
    partition_key: str = DEFAULT_PARTITION
    coverage_mode: CoverageMode | None = None
    slot: datetime | None = None
    complete_watermark: datetime | None = None
    max_pages: int | None = Field(default=None, ge=1, le=MAX_PAGE_CAP)


@dataclass(slots=True)
class Traversal:
    """What the network phase observed (nothing of it is committed yet)."""

    pages: list[DiscoveryPage] = field(default_factory=list)
    ok_pages: list[DiscoveryPage] = field(default_factory=list)
    completeness: Completeness = Completeness.FAILED
    cursor: dict[str, Any] | None = None
    gap_reasons: list[str] = field(default_factory=list)
    failure: DiscoveryPage | None = None
    refusal: BudgetRefused | None = None

    @property
    def last_access_state(self) -> AccessState | None:
        return self.pages[-1].access_state if self.pages else None


# --------------------------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------------------------


async def handle_discovery(ctx: RuntimeContext, execution: JobExecution) -> JobOutcome:
    job = execution.job
    actor = execution.actor
    try:
        payload = DiscoveryPayload.model_validate(job.payload)
    except ValidationError:
        raise ValidationFailed("the discovery job payload is invalid") from None
    source_id = job.source_id or payload.source_id
    profile_id = job.profile_id or payload.profile_id
    if source_id is None or profile_id is None:
        raise ValidationFailed("a discovery job needs its source and profile")
    partition = job.partition_key or payload.partition_key
    source, profile = await _preflight(ctx, actor, source_id, profile_id)
    session = await ctx.open_crawl_session(job.workspace_id, source)
    adapter = session.adapter
    coverage = payload.coverage_mode or adapter.capabilities().coverage_mode
    watermark_from = (
        payload.complete_watermark - WATERMARK_OVERLAP
        if coverage == CoverageMode.WATERMARK and payload.complete_watermark is not None
        else None
    )

    async def open_run() -> CrawlRunRecord:
        async with job_unit_of_work(ctx.db, job) as (conn, _locked):
            await close_interrupted_runs(conn, actor, job.id, attempt=job.attempts)
            return await sources_repo.start_crawl_run(
                conn,
                actor,
                source_id=source.id,
                profile_id=profile_id,
                partition_key=partition,
                coverage_mode=coverage,
                adapter_version=adapter.adapter_version,
                job_id=job.id,
                parser_version=adapter.adapter_version,
                build_id=ctx.settings.build_id,
                watermark_from=watermark_from,
            )

    run = await retry_transient(open_run)
    with log_context(run_id=run.id, source_key=source.source_key, adapter_version=adapter.adapter_version):
        traversal = await traverse(
            ctx,
            execution,
            session,
            source=source,
            profile=profile,
            watermark_from=watermark_from,
            partition_key=partition,
            max_pages=payload.max_pages,
        )
        health = _health_status(session, source, traversal)
        outcome = await _commit(
            ctx,
            execution,
            session,
            source=source,
            run=run,
            coverage=coverage,
            traversal=traversal,
            health=health,
        )
        if traversal.completeness == Completeness.COMPLETE and outcome.state == JobState.SUCCEEDED:
            if health is None:
                await _mark_absences(ctx, actor, run.id)
            else:
                # Spec 9/25: a suspected parser incident is never evidence about the inventory.
                logger.warning("absence marking skipped", extra={"parser_health": health.status})
        logger.info(
            "discovery finished",
            extra={"completeness": traversal.completeness.value, "pages": len(traversal.pages)},
        )
    return outcome


async def _preflight(
    ctx: RuntimeContext, actor: ActorContext, source_id: UUID, profile_id: UUID
) -> tuple[SourceRecord, SearchProfile]:
    async with unit_of_work(ctx.db, actor) as conn:
        await lock_source(conn, actor.workspace_id, source_id, for_network=True)
        source = await sources_repo.get_source_record(conn, actor, source_id)
        _record, config = await config_repo.current_config(conn, actor)
        profiles = await config_repo.list_profiles(conn, actor)
    problems = source.activation_problems()
    if problems:
        raise SourcePaused(f"source {source.source_key} is gated: {'; '.join(problems)[:300]}")
    stored = next((p for p in profiles if p.id == profile_id), None)
    if stored is None:
        raise NotFound("Search profile not found")
    profile = config.profiles.get(stored.profile_key)
    if profile is None or not stored.enabled or not profile.enabled:
        raise SourcePaused(f"profile {stored.profile_key.value} is not enabled")
    return source, profile


# --------------------------------------------------------------------------------------------
# Network phase
# --------------------------------------------------------------------------------------------


def _page_key(request: SearchRequest) -> tuple[str, str | None, tuple[tuple[str, str], ...]]:
    """What identifies the fetched page: its URL, plus an opaque cursor that is not that URL."""
    cursor = None if request.cursor in (None, request.url) else request.cursor
    return request.url, cursor, tuple(sorted(request.params.items()))


def _cursor(page: DiscoveryPage, request: SearchRequest) -> dict[str, Any]:
    return {
        "next_url": page.next_url,
        "next_cursor": page.next_cursor,
        "page_number": request.page_number + 1,
    }


async def traverse(
    ctx: RuntimeContext,
    execution: JobExecution,
    session: CrawlSession,
    *,
    source: SourceRecord,
    profile: SearchProfile,
    watermark_from: datetime | None,
    partition_key: str = DEFAULT_PARTITION,
    max_pages: int | None = None,
) -> Traversal:
    """Fetch search pages within the per-run page budget (see module docstring).

    ``max_pages`` (the job payload's cap) can only lower the source's per-run page budget.
    """
    adapter = session.adapter
    result = Traversal()
    budget = source.rate_budget().max_search_pages_per_run
    page_cap = budget if max_pages is None else max(1, min(max_pages, budget))

    def build(cursor: str | None) -> SearchRequest:
        # Every page of the traversal keeps the partition and the watermark filter.
        built = adapter.build_search(profile, cursor)
        update: dict[str, Any] = {"partition_key": partition_key}
        if watermark_from is not None:
            update["modified_since"] = watermark_from
        return SearchRequest.model_validate({**built.model_dump(), **update})

    request = build(None)
    visited: set[tuple[str, str | None, tuple[tuple[str, str], ...]]] = set()
    while True:
        current = request
        visited.add(_page_key(current))
        try:
            page = await call_with_budget(ctx, execution, partial(adapter.discover, current, session.client))
        except BudgetRefused as exc:
            if not result.ok_pages:
                result.refusal = exc
                result.completeness = Completeness.FAILED
                result.gap_reasons.append(f"budget refused before the first page ({exc.reason})")
                return result
            budget_stop = isinstance(exc.decision, Deny) and exc.decision.reason in (
                DenyReason.RUN_CAP_REACHED,
                DenyReason.BUDGET_EXHAUSTED,
            )
            result.completeness = Completeness.BUDGET_LIMITED if budget_stop else Completeness.PARTIAL
            result.cursor = {"next_url": current.url, "page_number": current.page_number}
            result.gap_reasons.append(f"traversal stopped at page {current.page_number}: {exc.reason}")
            return result
        result.pages.append(page)
        ctx.metrics.record_fetch(
            source.source_key,
            page_type="search",
            outcome=page.access_state,
            response_bytes=page.fetch.bytes,
        )
        if page.access_state != AccessState.OK:
            result.failure = page
            if page.access_state == AccessState.ACCESS_BLOCKED:
                result.completeness = Completeness.BLOCKED
            else:
                result.completeness = Completeness.PARTIAL if result.ok_pages else Completeness.FAILED
            result.gap_reasons.append(
                f"page {page.request.page_number}: {page.access_state.value}"
                + (f" ({page.access_evidence})" if page.access_evidence else "")
            )
            if result.ok_pages:
                result.cursor = {"next_url": current.url, "page_number": current.page_number}
            return result
        result.ok_pages.append(page)
        if not page.has_more:
            result.completeness = (
                Completeness.COMPLETE if page.completeness == Completeness.COMPLETE else Completeness.PARTIAL
            )
            if result.completeness != Completeness.COMPLETE:
                result.gap_reasons.append(
                    f"last page {page.request.page_number} is {page.completeness.value}"
                    + (f" ({page.access_evidence})" if page.access_evidence else "")
                )
            return result
        if len(result.ok_pages) >= page_cap:
            result.completeness = Completeness.BUDGET_LIMITED
            result.cursor = _cursor(page, current)
            result.gap_reasons.append(f"page budget of {page_cap} search page(s) per run reached")
            return result
        next_cursor = page.next_url or page.next_cursor
        try:
            request = build(next_cursor)
        except ValidationFailed as exc:
            result.completeness = Completeness.PARTIAL
            result.cursor = _cursor(page, current)
            result.gap_reasons.append(f"next page refused: {exc.message}")
            return result
        if _page_key(request) in visited:
            # A pagination cycle (e.g. a last page whose "next" link points back): stop instead of
            # re-fetching pages already seen in this run (spec 9: never add traffic silently).
            result.completeness = Completeness.PARTIAL
            result.cursor = None
            result.gap_reasons.append(
                f"pagination loop: page {current.page_number} links back to a page already fetched"
            )
            return result


# --------------------------------------------------------------------------------------------
# Commit phase
# --------------------------------------------------------------------------------------------


def _parse_outcome(page: DiscoveryPage) -> ParseOutcome:
    return ParseOutcome(
        page_type=page.page_type,
        access_state=page.access_state,
        ok=page.access_state == AccessState.OK,
        listing_count=len(page.observations),
        result_count_reported=page.result_count_reported,
        pagination_marker_present=page.has_more if page.access_state == AccessState.OK else None,
        observed_at=page.fetched_at,
    )


def _route(page: DiscoveryPage) -> SourceRoute:
    parts = urlsplit(page.fetch.requested_url)
    return SourceRoute(host=parts.hostname or "unknown.invalid", purpose="search", path=parts.path or None)


def _retry_time(
    session: CrawlSession, source: SourceRecord, page: DiscoveryPage, attempt: int
) -> datetime | timedelta:
    """Retry instant from the budget gate's plan (it never shortens Retry-After)."""
    host = urlsplit(page.fetch.requested_url).hostname
    result = session.gate.last_result(source.source_key, host) if host else None
    if result is not None and result.plan.retry_at is not None:
        return result.plan.retry_at
    if page.fetch.retry_after_seconds is not None:
        return timedelta(seconds=page.fetch.retry_after_seconds)
    return backoff_delay(attempt)


def _disposition(
    session: CrawlSession, source: SourceRecord, traversal: Traversal, attempt: int, result: dict[str, Any]
) -> Disposition:
    """The outcome of a traversal that fetched something (budget refusals: `apply_budget_refusal`)."""
    failure = traversal.failure
    if failure is not None and failure.access_state == AccessState.ACCESS_BLOCKED:
        return Disposition.blocked(
            "access_blocked", failure.access_evidence or "access blocked by the source"
        )
    if failure is not None and not traversal.ok_pages:
        if failure.access_state == AccessState.RATE_LIMITED:
            return Disposition.retry("RATE_LIMITED", _retry_time(session, source, failure, attempt))
        if failure.access_state == AccessState.TRANSIENT_ERROR:
            return Disposition.retry(
                failure.fetch.error_code or "TRANSIENT_ERROR", _retry_time(session, source, failure, attempt)
            )
    return Disposition.complete(result)


def _health_status(session: CrawlSession, source: SourceRecord, traversal: Traversal) -> ParserHealth | None:
    if traversal.completeness == Completeness.BLOCKED or not traversal.pages:
        return None
    if source.technical_status in (TechnicalStatus.ACCESS_BLOCKED, TechnicalStatus.PARSER_UNHEALTHY):
        return None
    health = session.adapter.assess_parser_health([_parse_outcome(p) for p in traversal.pages])
    return health if health.status in ("degraded", "unhealthy") else None


async def _record_health(conn: Conn, actor: ActorContext, source: SourceRecord, health: ParserHealth) -> None:
    status = TechnicalStatus.PARSER_UNHEALTHY if health.status == "unhealthy" else TechnicalStatus.DEGRADED
    if status == source.technical_status:
        return
    await sources_repo.set_technical_status(
        conn,
        actor,
        source.id,
        status,
        reason=f"parser health tripwire: {', '.join(health.reasons)[:400] or health.status}",
        parser_health=health,
    )


async def _commit(
    ctx: RuntimeContext,
    execution: JobExecution,
    session: CrawlSession,
    *,
    source: SourceRecord,
    run: CrawlRunRecord,
    coverage: CoverageMode,
    traversal: Traversal,
    health: ParserHealth | None,
) -> JobOutcome:
    job = execution.job
    actor = execution.actor
    watermark_to: datetime | None = None
    if coverage == CoverageMode.WATERMARK and traversal.completeness == Completeness.COMPLETE:
        marks = [p.watermark_observed for p in traversal.ok_pages if p.watermark_observed is not None]
        watermark_to = max(marks) if marks else None
    final: Completeness | Literal["cancelled"] = (
        traversal.completeness if traversal.refusal is None else "cancelled"
    )
    run_outcome = RunOutcome(
        completeness=final,
        watermark_to=watermark_to,
        cursor=traversal.cursor if final != Completeness.COMPLETE else None,
        gap_reasons=tuple(r[:300] for r in traversal.gap_reasons[:20]),
        access_state=traversal.last_access_state,
        error_code=None if traversal.failure is None else traversal.failure.fetch.error_code,
    )

    async def commit() -> tuple[list[IngestReport], str | None, JobState]:
        execution.check_lease()
        async with job_unit_of_work(ctx.db, job) as (conn, _locked):
            failure = traversal.failure
            if failure is not None and failure.access_state == AccessState.ACCESS_BLOCKED:
                await sources_repo.record_access_block(
                    conn,
                    actor,
                    source.id,
                    _route(failure),
                    {
                        "http_status": failure.fetch.http_status,
                        "error_code": failure.fetch.error_code,
                        "page_type": failure.page_type,
                        "evidence": failure.access_evidence,
                    },
                )
            elif health is not None:
                await _record_health(conn, actor, source, health)
            for page in traversal.pages:
                await _record_fetch(conn, actor, source, page, job_id=job.id, run_id=run.id)
            reports = [
                await listings_repo.ingest_search_page(conn, actor, run, page, job_id=job.id)
                for page in traversal.ok_pages
            ]
            await sources_repo.finish_crawl_run(conn, actor, run.id, run_outcome)
            result = {
                "run_id": str(run.id),
                "completeness": str(final),
                "pages": len(traversal.ok_pages),
                "cards": sum(r.stored for r in reports),
                "new_listings": sum(r.new_listings for r in reports),
                "detail_jobs": sum(len(r.detail_jobs) for r in reports),
            }
            if traversal.refusal is not None:
                # Nothing was fetched: released without consuming an attempt (or blocked).
                state, code = await apply_budget_refusal(conn, job, traversal.refusal, actor)
                return reports, code, state
            disposition = _disposition(session, source, traversal, job.attempts, result)
            # The applied state, not the requested kind: an exhausted retry is a dead letter.
            state = await apply_disposition(conn, job, disposition)
        return reports, disposition.code, state

    reports, code, state = await retry_transient(commit)
    _record_metrics(ctx, source, traversal, reports)
    return JobOutcome(
        state=state,
        code=code,
        details={"run_id": str(run.id), "completeness": str(final), "pages": len(traversal.pages)},
    )


async def _record_fetch(
    conn: Conn, actor: ActorContext, source: SourceRecord, page: DiscoveryPage, *, job_id: UUID, run_id: UUID
) -> None:
    try:
        await sources_repo.record_fetch_attempt(
            conn,
            actor,
            source_id=source.id,
            purpose="search",
            outcome=page.fetch,
            job_id=job_id,
            crawl_run_id=run_id,
        )
    except ValidationFailed:
        # A policy-refused URL without a usable host: nothing reached the network, nothing to store.
        logger.info("fetch outcome without a host was not recorded")


def _record_metrics(
    ctx: RuntimeContext, source: SourceRecord, traversal: Traversal, reports: list[IngestReport]
) -> None:
    metrics = ctx.metrics
    metrics.record_scan(source.source_key, completeness=traversal.completeness, watermark_lag=None)
    for report in reports:
        for _ in report.detail_jobs:
            metrics.record_detail_job(source.source_key, deduplicated=False)
        for _ in range(report.detail_jobs_deduplicated):
            metrics.record_detail_job(source.source_key, deduplicated=True)
    failure = traversal.failure
    if failure is not None and failure.access_state in (
        AccessState.UNEXPECTED_CONTENT,
        AccessState.POLICY_DENIED,
    ):
        metrics.record_parser_failure(source.source_key, page_type="search")


_INTERRUPTED_RUNS_SQL: Final = (
    "select id from ops.crawl_runs where workspace_id = %(workspace_id)s and job_id = %(job_id)s"
    " and outcome = 'running' order by started_at, id limit 20"
)


async def close_interrupted_runs(conn: Conn, actor: ActorContext, job_id: UUID, *, attempt: int) -> int:
    """Close the runs an earlier attempt of this job opened but never finished (``cancelled``).

    An attempt that lost its lease or failed after opening its run cannot finish it; the next
    holder of the job lease (the caller holds it, so no other attempt can still be working) closes
    it with the gap recorded instead of leaving a traversal ``running`` forever. Read-only select
    with a workspace predicate; the close goes through `sources_repo.finish_crawl_run`.
    """
    async with mapped_errors():
        rows = await fetch_all(
            conn, _INTERRUPTED_RUNS_SQL, {"workspace_id": actor.workspace_id, "job_id": job_id}
        )
    for row in rows:
        await sources_repo.finish_crawl_run(
            conn,
            actor,
            row["id"],
            RunOutcome(
                completeness="cancelled",
                gap_reasons=(
                    f"attempt interrupted before the run finished; superseded by attempt {attempt}",
                ),
            ),
        )
    return len(rows)


async def _mark_absences(ctx: RuntimeContext, actor: ActorContext, run_id: UUID) -> None:
    """Complete-scan absence marking in its own short transaction (never removal or sale)."""
    try:
        async with unit_of_work(ctx.db, actor) as conn:
            report = await listings_repo.mark_complete_scan_absences(conn, actor, run_id)
    except AppError as exc:
        logger.warning("absence marking failed", extra={"error_code": exc.code.value})
        return
    if report.marked_unknown:
        logger.info("listings not seen in a complete scan", extra={"count": len(report.marked_unknown)})


__all__ = [
    "MAX_PAGE_CAP",
    "WATERMARK_OVERLAP",
    "DiscoveryPayload",
    "Traversal",
    "close_interrupted_runs",
    "handle_discovery",
    "traverse",
]
