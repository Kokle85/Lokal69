"""Periodic reconciliation: reapers, housekeeping, bounded sweeps (spec 9, 13, 18, 22, 30).

One pass per active workspace (``ops.active_workspace_ids()``, ADR 0001), every step in its own
short transaction, no network I/O:

1. **Reapers** (spec 13): expired job leases are requeued while attempts remain, otherwise
   dead-lettered (`jobs.reap_expired`; a crashed worker's job completes once logically through the
   next lease holder) -- except an expired ``seller_inquiry_send`` job, which is ``blocked`` with
   EMAIL_DELIVERY_UNCERTAIN and never requeued, while its expired running send attempt and the
   inquiry become ``uncertain`` (`inquiries_repo.reap_expired_attempts`, spec 37.5: no blind
   resend; only positive reconciliation evidence decides); exhausted waiting jobs become
   visible dead letters (`jobs.reconcile_exhausted`);
   expired dispatcher leases on outbox rows become ``uncertain`` when a send had started, otherwise
   ``retry_wait`` (`outbox.reap_expired_events`); leased MCP Events deliveries whose dispatcher died
   become ``uncertain`` (`subscriptions_repo.reap_expired_deliveries`), never blindly resent.
2. **Housekeeping**: expired review claims return to their restore state, expired query snapshots
   and idempotency records are purged; crawl runs left ``running`` by a discovery job that has
   ended (dead letter, cancelled, blocked) are closed as ``cancelled`` with the gap recorded (a
   retried job closes its own earlier run when it starts the next one).
3. **Stale-detail sweep** (spec 9 "Detail fetch rules"): due watchlist rechecks become bounded
   ``recheck`` jobs (the watch's next recheck moves one interval ahead); listings with an open review
   case or an eligible/needs-facts screening whose last detail check is older than the sweep age get a
   low-priority ``detail`` job -- at most one per listing per sweep age, so a page that keeps failing
   (dead letter, blocked) is not re-fetched on every pass. Both are capped per source by the source's
   per-run detail budget, only for sources that may make network requests (enabled, unpaused,
   unblocked, ``detail_mode=fetch``, ``SOURCE_NETWORK_ENABLED`` for real sources, loaded fixture data
   for fixture sources); stale candidates are selected per such source, so a paused or blocked
   source's backlog never crowds the others out. The jobs themselves still pass the persistent
   budget gate before any request. Nothing is fetched here.
4. **Valuation staleness** (spec 18): valuations past their freshness deadline (FX age, quote expiry,
   comparable freshness are folded into ``expires_at`` by the valuation assembly) are marked stale and
   their recomputation is queued (`valuation_repo.mark_stale`); valuations that used an older reference
   FX observation than the newest stored one, or an older business configuration, are invalidated
   through reverse invalidation (`valuation_repo.invalidate_dependents`).
5. **Seller inquiries** (spec 37.3-37.5, bounded, no network I/O):

   - a ``seller_inquiry_plan`` job for current eligible, real-lineage listings that have no inquiry
     record, once per (revision, current seller contact evidence): the valuation pipeline plans each
     new revision itself; this catches a seller contact recorded after that plan found nobody to ask;
   - a ``seller_inquiry_send`` job for a ``queued`` inquiry that has none (never for one with a
     running attempt);
   - a ``seller_inquiry_reconcile`` job (at most one per inquiry per hour) for every ``uncertain``
     inquiry whose unreconciled uncertain attempt ended more than ``inquiry_reconcile_after`` and
     less than ``inquiry_reconcile_horizon`` ago;
   - the ONE ``seller_reply_process`` job of every recent, unquarantined seller reply that has none
     (an ingest path that did not queue it).

   The plan sweep and the configuration-dependent invalidation skip a workspace without a business
   configuration.
6. **Metrics**: queue depth / oldest due age per job type, lease expirations and dead letters.

``dry_run=True`` reports what a pass WOULD do: transactional steps run and are rolled back, the
database-level reapers are replaced by read-only counts. Nothing is committed in a dry run.
"""

from __future__ import annotations

import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

import anyio

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import FxPurpose, JobType, Scope, SourceMode, TechnicalStatus
from suv_deals.domain.valuation import InvalidationReason
from suv_deals.errors import AppError, NotFound, ValidationFailed
from suv_deals.persistence import (
    config_repo,
    idempotency,
    inquiries_repo,
    jobs,
    listings_repo,
    notes_repo,
    outbox,
    query_snapshots,
    reviews_repo,
    sources_repo,
    subscriptions_repo,
    valuation_repo,
)
from suv_deals.persistence.database import Conn, db_now, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.replies_repo import REPLY_PROCESS_PREFIX
from suv_deals.persistence.sources_repo import SourceRecord
from suv_deals.persistence.transactions import retry_transient, unit_of_work
from suv_deals.settings import Settings
from suv_deals.workers.inquiry_handlers import (
    PLAN_PREFIX,
    enqueue_plan_job,
    enqueue_reconcile_job,
    enqueue_send_job,
)
from suv_deals.workers.reply_handlers import enqueue_reply_process_job
from suv_deals.workers.runtime import RuntimeContext, active_workspace_ids, build_runtime, system_actor

logger = logging.getLogger(__name__)

STALE_DETAIL_REASON: Final = "stale_detail_sweep"
WATCH_RECHECK_REASON: Final = "watchlist_recheck"
_NETWORK_BLOCKING: Final = frozenset(
    {TechnicalStatus.UNTESTED, TechnicalStatus.ACCESS_BLOCKED, TechnicalStatus.PARSER_UNHEALTHY}
)


@dataclass(frozen=True, slots=True)
class ReconcileOptions:
    """Engineering defaults (PROPOSED; not provider- or owner-approved values)."""

    job_retry_delay_seconds: int = 30
    outbox_retry_delay_seconds: int = 30
    reap_limit: int = 500
    housekeeping_limit: int = 1000
    claim_expiry_limit: int = 100
    #: A listing's detail page is re-checked at most this often by the sweep (conservative daily).
    stale_detail_age: timedelta = timedelta(hours=24)
    #: At most this many stale listings are selected per pass, over all network-eligible sources.
    stale_detail_candidates: int = 200
    watch_recheck_limit: int = 100
    stale_detail_priority: int = -10
    watch_recheck_priority: int = 10
    valuation_sweep_limit: int = 200
    #: Reference FX pairs (EUR/<currency>) checked for newer observations.
    fx_currencies: tuple[str, ...] = ("CHF", "MKD")
    interval_seconds: float = 300.0
    #: Seller-inquiry sweeps: plan jobs per pass, send jobs per pass, reconcile jobs per pass.
    inquiry_plan_limit: int = 50
    inquiry_send_limit: int = 50
    inquiry_reconcile_limit: int = 50
    #: An uncertain send is reconciled once it is this old (the provider/worker may still report)...
    inquiry_reconcile_after: timedelta = timedelta(minutes=15)
    #: ...and no longer automatically after this long (it stays visibly uncertain).
    inquiry_reconcile_horizon: timedelta = timedelta(days=30)
    #: Seller replies of this age or younger get their processing job if none exists.
    reply_process_horizon: timedelta = timedelta(days=7)
    reply_process_limit: int = 50

    def __post_init__(self) -> None:
        if self.stale_detail_age < timedelta(hours=1):
            raise ValueError("stale_detail_age must be at least one hour")
        if not 0 <= self.job_retry_delay_seconds <= 86_400:
            raise ValueError("job_retry_delay_seconds must be between 0 and 86400")


@dataclass(slots=True)
class ReconcileReport:
    """What one pass did (or, with ``dry_run``, would do) in one workspace."""

    workspace_id: UUID
    dry_run: bool
    jobs_requeued: int = 0
    jobs_dead_lettered: int = 0
    jobs_blocked_uncertain: int = 0
    send_attempts_uncertain: int = 0
    jobs_exhausted: int = 0
    events_retry: int = 0
    events_uncertain: int = 0
    events_dead_lettered: int = 0
    deliveries_uncertain: int = 0
    claims_expired: int = 0
    crawl_runs_closed: int = 0
    snapshots_deleted: int = 0
    idempotency_deleted: int = 0
    watch_rechecks: int = 0
    stale_detail_jobs: int = 0
    valuations_expired: int = 0
    valuations_invalidated: int = 0
    recompute_jobs: int = 0
    inquiry_plan_jobs: int = 0
    inquiry_send_jobs: int = 0
    inquiry_reconcile_jobs: int = 0
    reply_process_jobs: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name not in ("workspace_id", "errors")
        } | {"workspace_id": str(self.workspace_id), "errors": list(self.errors)}


class _DryRunRollback(Exception):
    """Raised inside a dry-run transaction so everything it did is rolled back."""


async def _step[T](
    ctx: RuntimeContext,
    actor: ActorContext,
    operation: Callable[[Conn], Awaitable[T]],
    *,
    dry_run: bool,
) -> T:
    """One short transaction; rolled back in a dry run (the operation's result is still returned)."""
    holder: list[T] = []

    async def once() -> None:
        holder.clear()
        try:
            async with unit_of_work(ctx.db, actor) as conn:
                holder.append(await operation(conn))
                if dry_run:
                    raise _DryRunRollback
        except _DryRunRollback:
            pass

    await retry_transient(once)
    return holder[0]


# --------------------------------------------------------------------------------------------
# Read-only selections without a repository equivalent yet (see foundation change requests)
# --------------------------------------------------------------------------------------------

_STALE_DETAIL_SQL: Final = """
select l.id
  from app.listings l
  join app.sources s on s.workspace_id = l.workspace_id and s.id = l.source_id
 where l.workspace_id = %(ws)s
   and l.source_id = %(source_id)s
   and s.role = 'acquisition' and s.detail_mode = 'fetch'
   and not l.quarantined and not l.identity_conflict
   and l.availability in ('available', 'reserved', 'unknown')
   and coalesce(l.last_detail_success_at, l.first_seen_at) < now() - %(age)s::interval
   and (l.eligibility_state in ('eligible_primary', 'eligible_manual_profile', 'needs_facts')
        or exists (select 1 from app.review_cases c
                    where c.workspace_id = l.workspace_id and c.listing_id = l.id
                      and c.state in ('pending', 'claimed', 'needs_information', 'watch', 'shortlisted')))
   and not exists (select 1 from ops.jobs j
                    where j.workspace_id = l.workspace_id and j.listing_id = l.id
                      and j.job_type in ('detail', 'recheck')
                      and (j.state in ('queued', 'running', 'retry_wait')
                           -- one refresh attempt per sweep age: a fetch that failed (dead letter,
                           -- blocked, skipped) is not re-queued on every pass
                           or j.created_at > now() - %(age)s::interval))
 order by coalesce(l.last_detail_success_at, l.first_seen_at), l.id
 limit %(limit)s
"""

_EXPIRED_VALUATIONS_SQL: Final = """
select v.id from app.valuations v
 where v.workspace_id = %(ws)s
   and v.state in ('incomplete', 'estimated', 'quote_supported')
   and v.expires_at is not null and v.expires_at <= now()
 order by v.expires_at, v.id
 limit %(limit)s
"""

_ORPHAN_RUNS_SQL: Final = """
select r.id, j.state
  from ops.crawl_runs r
  join ops.jobs j on j.workspace_id = r.workspace_id and j.id = r.job_id
 where r.workspace_id = %(ws)s
   and r.outcome = 'running'
   and j.state in ('succeeded', 'dead_letter', 'cancelled', 'blocked')
 order by r.started_at, r.id
 limit %(limit)s
"""

_EXPIRED_EVENT_LEASES_SQL: Final = """
select
  count(*) filter (where state = 'sending' and lease_expires_at <= clock_timestamp()
                   and send_attempted_at is not null and send_attempted_at >= last_heartbeat_at)
    as would_be_uncertain,
  count(*) filter (where state = 'sending' and lease_expires_at <= clock_timestamp()
                   and not (send_attempted_at is not null and send_attempted_at >= last_heartbeat_at))
    as would_retry,
  count(*) filter (where state in ('pending', 'retry_wait') and attempts >= max_attempts)
    as exhausted
  from ops.outbox where workspace_id = %(ws)s
"""

_EXPIRED_SEND_SQL: Final = """
select
  (select count(*) from ops.jobs where workspace_id = %(ws)s and job_type = 'seller_inquiry_send'
     and state = 'running' and lease_expires_at <= clock_timestamp()) as send_jobs,
  (select count(*) from ops.email_delivery_attempts where workspace_id = %(ws)s
     and outcome = 'running' and lease_expires_at <= clock_timestamp()) as send_attempts
"""


#: Eligible real-lineage listings without an inquiry record whose CURRENT contact evidence has
#: not been planned yet: one plan job per (listing revision, contact evidence row), so a seller
#: contact recorded after the valuation's plan job (``seller_not_linked``) is still planned once.
_PLAN_SWEEP_SQL: Final = """
select l.id, l.current_revision_id, c.id as contact_id
  from app.listings l
  join lateral (select c.id from app.seller_contacts c
                 where c.workspace_id = l.workspace_id and c.listing_id = l.id and c.status <> 'changed'
                 order by (c.status = 'verified') desc, c.created_at desc, c.id desc
                 limit 1) c on true
 where l.workspace_id = %(ws)s
   and l.eligibility_state in ('eligible_primary', 'eligible_manual_profile')
   and not l.is_fixture and not l.quarantined and not l.identity_conflict
   and l.current_revision_id is not null
   and l.availability in ('available', 'reserved', 'unknown')
   and not exists (select 1 from app.seller_inquiries i
                    where i.workspace_id = l.workspace_id and i.qualification_listing_id = l.id)
   and not exists (select 1 from ops.jobs j
                    where j.workspace_id = l.workspace_id and j.listing_id = l.id
                      and j.job_type = 'seller_inquiry_plan'
                      and j.dedup_key = %(prefix)s || ':' || l.id::text || ':'
                                        || l.current_revision_id::text || ':contact-' || c.id::text)
 order by l.last_seen_at desc, l.id
 limit %(limit)s
"""

_ORPHAN_QUEUED_SQL: Final = """
select i.id, i.qualification_listing_id as listing_id,
       (select count(*) from ops.email_delivery_attempts a
         where a.workspace_id = i.workspace_id and a.inquiry_id = i.id) as attempts
  from app.seller_inquiries i
 where i.workspace_id = %(ws)s
   and i.state = 'queued'
   and not exists (select 1 from ops.email_delivery_attempts a
                    where a.workspace_id = i.workspace_id and a.inquiry_id = i.id
                      and a.outcome = 'running')
   and not exists (select 1 from ops.jobs j
                    where j.workspace_id = i.workspace_id and j.job_type = 'seller_inquiry_send'
                      and j.payload ->> 'inquiry_id' = i.id::text
                      and j.state in ('queued', 'running', 'retry_wait', 'blocked'))
 order by i.updated_at, i.id
 limit %(limit)s
"""

_UNPROCESSED_REPLIES_SQL: Final = """
select r.id, r.inquiry_id, i.qualification_listing_id as listing_id
  from app.seller_replies r
  join app.seller_inquiries i on i.workspace_id = r.workspace_id and i.id = r.inquiry_id
 where r.workspace_id = %(ws)s
   and not r.quarantined and r.message_type = 'seller_reply'
   and r.ingested_at > clock_timestamp() - %(horizon)s::interval
   and not exists (select 1 from ops.jobs j
                    where j.workspace_id = r.workspace_id and j.job_type = 'seller_reply_process'
                      and j.dedup_key = %(prefix)s || ':' || r.id::text)
 order by r.ingested_at, r.id
 limit %(limit)s
"""

_UNCERTAIN_SQL: Final = """
select i.id, i.qualification_listing_id as listing_id,
       to_char(clock_timestamp() at time zone 'UTC', 'YYYYMMDDHH24') as bucket
  from app.seller_inquiries i
 where i.workspace_id = %(ws)s
   and i.state = 'uncertain'
   and exists (select 1 from ops.email_delivery_attempts a
                where a.workspace_id = i.workspace_id and a.inquiry_id = i.id and a.submission_uncertain
                  and coalesce(a.finished_at, a.send_intent_committed_at)
                      <= clock_timestamp() - %(after)s::interval
                  and coalesce(a.finished_at, a.send_intent_committed_at)
                      > clock_timestamp() - %(horizon)s::interval)
   and not exists (select 1 from ops.jobs j
                    where j.workspace_id = i.workspace_id and j.job_type = 'seller_inquiry_reconcile'
                      and j.payload ->> 'inquiry_id' = i.id::text
                      and j.state in ('queued', 'running', 'retry_wait'))
 order by i.updated_at, i.id
 limit %(limit)s
"""


async def _has_business_config(conn: Conn, actor: ActorContext) -> bool:
    """Whether the workspace has a usable business configuration (a workspace without one --
    or with a configuration revision that is not a business configuration -- is skipped)."""
    try:
        await config_repo.current_config(conn, actor)
    except (NotFound, ValidationFailed):
        return False
    return True


async def _stale_detail_candidates(
    conn: Conn, actor: ActorContext, source_id: UUID, age: timedelta, limit: int
) -> list[UUID]:
    """Stale listings of ONE source (oldest detail check first)."""
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _STALE_DETAIL_SQL,
            {"ws": actor.workspace_id, "source_id": source_id, "age": age, "limit": limit},
        )
    return [r["id"] for r in rows]


async def _close_orphan_runs(conn: Conn, actor: ActorContext, limit: int) -> int:
    """Close runs whose discovery job ended without finishing them (``cancelled``, gap recorded)."""
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        rows = await fetch_all(conn, _ORPHAN_RUNS_SQL, {"ws": actor.workspace_id, "limit": limit})
    for row in rows:
        await sources_repo.finish_crawl_run(
            conn,
            actor,
            row["id"],
            sources_repo.RunOutcome(
                completeness="cancelled",
                gap_reasons=(f"the discovery job ended ({row['state']}) before the run finished",),
            ),
        )
    return len(rows)


async def _expired_valuations(conn: Conn, actor: ActorContext, limit: int) -> list[UUID]:
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        rows = await fetch_all(conn, _EXPIRED_VALUATIONS_SQL, {"ws": actor.workspace_id, "limit": limit})
    return [r["id"] for r in rows]


# --------------------------------------------------------------------------------------------
# Pass
# --------------------------------------------------------------------------------------------


class Reconciler:
    def __init__(self, ctx: RuntimeContext, options: ReconcileOptions | None = None) -> None:
        self.ctx = ctx
        self.options = options or ReconcileOptions()

    async def run_once(self, *, dry_run: bool = False) -> list[ReconcileReport]:
        reports: list[ReconcileReport] = []
        for workspace_id in await active_workspace_ids(self.ctx.db):
            reports.append(await self.reconcile_workspace(workspace_id, dry_run=dry_run))
        return reports

    async def run(self, stop: anyio.Event) -> None:
        while not stop.is_set():
            try:
                for report in await self.run_once():
                    logger.info("reconciliation pass finished", extra={"summary": report.as_dict()})
            except AppError as exc:
                logger.warning("reconciliation pass failed", extra={"error_code": exc.code.value})
            with anyio.move_on_after(self.options.interval_seconds):
                await stop.wait()

    async def reconcile_workspace(self, workspace_id: UUID, *, dry_run: bool = False) -> ReconcileReport:
        actor = system_actor(workspace_id, "reconcile")
        report = ReconcileReport(workspace_id=workspace_id, dry_run=dry_run)
        steps: tuple[tuple[str, Callable[[ActorContext, ReconcileReport], Awaitable[None]]], ...] = (
            ("reap", self._reap),
            ("housekeeping", self._housekeeping),
            ("watch_rechecks", self._watch_rechecks),
            ("stale_detail", self._stale_detail),
            ("valuations", self._valuations),
            ("inquiries", self._inquiries),
            ("metrics", self._metrics),
        )
        for name, step in steps:
            try:
                await step(actor, report)
            except AppError as exc:
                # One failing step never stops the others (e.g. a lock timeout on a busy table).
                report.errors.append(f"{name}:{exc.code.value}")
                logger.warning(
                    "reconciliation step failed", extra={"step": name, "error_code": exc.code.value}
                )
        return report

    # ------------------------------------------------------------------ 1 reapers

    async def _reap(self, actor: ActorContext, report: ReconcileReport) -> None:
        db, ws, opts = self.ctx.db, actor.workspace_id, self.options
        if report.dry_run:
            async with unit_of_work(db, actor) as conn:
                stats = await jobs.queue_stats(conn, actor)
                async with mapped_errors():
                    row = await fetch_one(conn, _EXPIRED_EVENT_LEASES_SQL, {"ws": ws})
                    sends = await fetch_one(conn, _EXPIRED_SEND_SQL, {"ws": ws})
            assert row is not None and sends is not None
            report.jobs_blocked_uncertain = int(sends["send_jobs"])
            report.send_attempts_uncertain = int(sends["send_attempts"])
            # An upper bound: exhausted ones dead-letter, expired send jobs block.
            report.jobs_requeued = max(0, stats.expired_leases - report.jobs_blocked_uncertain)
            report.jobs_exhausted = stats.exhausted_waiting
            report.events_uncertain = int(row["would_be_uncertain"])
            report.events_retry = int(row["would_retry"])
            report.events_dead_lettered = int(row["exhausted"])
        else:
            reaped = await jobs.reap_expired(
                db, ws, retry_delay_seconds=opts.job_retry_delay_seconds, limit=opts.reap_limit
            )
            for job_type, count in reaped.expired_by_type.items():
                for _ in range(count):
                    self.ctx.metrics.record_lease_expiration(job_type)
            for job_type, count in reaped.dead_lettered_by_type.items():
                for _ in range(count):
                    self.ctx.metrics.record_dead_letter(job_type)
            report.jobs_requeued = len(reaped.requeued)
            report.jobs_dead_lettered = len(reaped.dead_lettered)
            report.jobs_blocked_uncertain = len(reaped.blocked_uncertain)
            report.send_attempts_uncertain = len(
                await inquiries_repo.reap_expired_attempts(db, ws, limit=min(opts.reap_limit, 1000))
            )
            report.jobs_exhausted = len(await jobs.reconcile_exhausted(db, ws, limit=opts.reap_limit))
            events = await outbox.reap_expired_events(
                db, ws, retry_delay_seconds=opts.outbox_retry_delay_seconds, limit=opts.reap_limit
            )
            report.events_retry = len(events.retry)
            report.events_uncertain = len(events.uncertain)
            report.events_dead_lettered = len(events.dead_letter) + len(events.exhausted)
        deliveries = await _step(
            self.ctx,
            actor,
            lambda c: subscriptions_repo.reap_expired_deliveries(c, actor, limit=opts.reap_limit),
            dry_run=report.dry_run,
        )
        report.deliveries_uncertain = len(deliveries)

    # ------------------------------------------------------------------ 2 housekeeping

    async def _housekeeping(self, actor: ActorContext, report: ReconcileReport) -> None:
        opts = self.options
        claims = await _step(
            self.ctx,
            actor,
            lambda c: reviews_repo.expire_claims(c, actor, limit=opts.claim_expiry_limit),
            dry_run=report.dry_run,
        )
        report.claims_expired = len(claims)
        report.snapshots_deleted = await _step(
            self.ctx,
            actor,
            lambda c: query_snapshots.delete_expired(c, actor, limit=opts.housekeeping_limit),
            dry_run=report.dry_run,
        )
        report.idempotency_deleted = await _step(
            self.ctx,
            actor,
            lambda c: idempotency.delete_expired(c, actor, limit=opts.housekeeping_limit),
            dry_run=report.dry_run,
        )
        report.crawl_runs_closed = await _step(
            self.ctx,
            actor,
            lambda c: _close_orphan_runs(c, actor, opts.reap_limit),
            dry_run=report.dry_run,
        )

    # ------------------------------------------------------------------ 3 stale-detail sweep

    def _network_allowed(self, source: SourceRecord) -> bool:
        if not source.enabled or source.paused or source.technical_status in _NETWORK_BLOCKING:
            return False
        if source.role != "acquisition" or source.detail_mode != "fetch" or source.activation_problems():
            return False
        if source.mode == SourceMode.FIXTURE:
            # Fixture pages exist only where this process loaded them (never in production).
            return self.ctx.fixture_client is not None
        return self.ctx.settings.source_network_enabled

    async def _sweep_sources(self, conn: Conn, actor: ActorContext) -> list[SourceRecord]:
        """Sources whose detail pages may be fetched now (stable order by source key)."""
        listing = await sources_repo.list_sources(conn, actor)
        records = await self._sources(conn, actor, {item.source_id for item in listing.items})
        allowed = [s for s in records.values() if self._network_allowed(s)]
        return sorted(allowed, key=lambda s: s.source_key)

    async def _sources(self, conn: Conn, actor: ActorContext, ids: set[UUID]) -> dict[UUID, SourceRecord]:
        found: dict[UUID, SourceRecord] = {}
        for source_id in sorted(ids, key=str):
            try:
                found[source_id] = await sources_repo.get_source_record(conn, actor, source_id)
            except NotFound:
                continue
        return found

    async def _watch_rechecks(self, actor: ActorContext, report: ReconcileReport) -> None:
        opts = self.options

        async def go(conn: Conn) -> int:
            # Only watches on sources this process may fetch from now fill the window: a large
            # backlog on a paused, blocked or disabled source never starves the others.
            eligible = await self._sweep_sources(conn, actor)
            if not eligible:
                return 0
            due = await notes_repo.due_watch_rechecks(
                conn, actor, limit=opts.watch_recheck_limit, source_ids=[s.id for s in eligible]
            )
            if not due:
                return 0
            listings = {w.listing_id: await listings_repo.get_listing(conn, actor, w.listing_id) for w in due}
            sources = await self._sources(conn, actor, {listing.source_id for listing in listings.values()})
            used: dict[UUID, int] = {}
            queued = 0
            for watch in due:
                source = sources.get(listings[watch.listing_id].source_id)
                if source is None or not self._network_allowed(source):
                    continue  # stays due: rechecked once the source may make requests again
                if used.get(source.id, 0) >= source.rate_budget().max_detail_jobs_per_run:
                    continue  # budget for this pass used; the watch stays due for the next pass
                ref = await listings_repo.request_detail_refresh(
                    conn,
                    actor,
                    watch.listing_id,
                    reason=WATCH_RECHECK_REASON,
                    job_type=JobType.RECHECK,
                    priority=opts.watch_recheck_priority,
                )
                await notes_repo.advance_watch_recheck(conn, actor, watch.id)
                used[source.id] = used.get(source.id, 0) + 1
                queued += int(ref is not None and ref.created)
            return queued

        report.watch_rechecks = await _step(self.ctx, actor, go, dry_run=report.dry_run)

    async def _stale_detail(self, actor: ActorContext, report: ReconcileReport) -> None:
        opts = self.options

        async def go(conn: Conn) -> int:
            # Candidates are selected PER network-eligible source: a paused, blocked or disabled
            # source with a large stale backlog can never fill the window and starve the others.
            remaining = opts.stale_detail_candidates
            queued = 0
            for source in await self._sweep_sources(conn, actor):
                if remaining <= 0:
                    break
                # Never more than one run's detail budget per source per pass.
                cap = min(source.rate_budget().max_detail_jobs_per_run, remaining)
                if cap <= 0:
                    continue
                candidates = await _stale_detail_candidates(
                    conn, actor, source.id, opts.stale_detail_age, cap
                )
                remaining -= len(candidates)
                for listing_id in candidates:
                    ref = await listings_repo.request_detail_refresh(
                        conn,
                        actor,
                        listing_id,
                        reason=STALE_DETAIL_REASON,
                        job_type=JobType.DETAIL,
                        priority=opts.stale_detail_priority,
                    )
                    queued += int(ref is not None and ref.created)
            return queued

        report.stale_detail_jobs = await _step(self.ctx, actor, go, dry_run=report.dry_run)

    # ------------------------------------------------------------------ 4 valuation staleness

    async def _valuations(self, actor: ActorContext, report: ReconcileReport) -> None:
        opts = self.options

        async def expired(conn: Conn) -> tuple[int, int]:
            marked = jobs_queued = 0
            for valuation_id in await _expired_valuations(conn, actor, opts.valuation_sweep_limit):
                change = await valuation_repo.mark_stale(
                    conn,
                    actor,
                    valuation_id,
                    InvalidationReason.FRESHNESS_DEADLINE,
                    detail="freshness deadline passed (FX age, quote expiry or comparable freshness)",
                )
                marked += int(change.changed)
                jobs_queued += int(change.recompute_job_id is not None)
            return marked, jobs_queued

        marked, queued = await _step(self.ctx, actor, expired, dry_run=report.dry_run)
        report.valuations_expired = marked

        async def dependencies(conn: Conn) -> tuple[int, int]:
            changes: list[valuation_repo.DependencyChange] = []
            today = ensure_utc(await db_now(conn)).date()
            for currency in opts.fx_currencies:
                for base, quote in (("EUR", currency), (currency, "EUR")):
                    # Real observations only: a fixture rate never invalidates real valuations.
                    latest = await valuation_repo.latest_fx_rates(
                        conn,
                        actor,
                        base=base,
                        quote=quote,
                        purpose=FxPurpose.REFERENCE,
                        on_or_before=today,
                        limit=1,
                    )
                    changes += [valuation_repo.DependencyChange.new_fx_rate(rate) for rate in latest]
            try:
                record, _config = await config_repo.current_config(conn, actor)
            except (NotFound, ValidationFailed):
                record = None  # no (business) configuration: nothing to invalidate against
            if record is not None:
                changes.append(
                    valuation_repo.DependencyChange(
                        reason=InvalidationReason.CONFIG,
                        current_config_revision_id=record.id,
                        detail="computed under an older business configuration",
                    )
                )
            stale = created = 0
            for change in changes:
                result = await valuation_repo.invalidate_dependents(
                    conn, actor, change, limit=opts.valuation_sweep_limit
                )
                stale += len(result.stale_valuation_ids)
                created += result.jobs_created
            return stale, created

        invalidated, created = await _step(self.ctx, actor, dependencies, dry_run=report.dry_run)
        report.valuations_invalidated = invalidated
        report.recompute_jobs = queued + created

    # ------------------------------------------------------------------ 5 seller inquiries

    async def _inquiries(self, actor: ActorContext, report: ReconcileReport) -> None:
        opts = self.options
        ws = actor.workspace_id

        async def plans(conn: Conn) -> int:
            if not await _has_business_config(conn, actor):
                return 0
            async with mapped_errors():
                rows = await fetch_all(
                    conn, _PLAN_SWEEP_SQL, {"ws": ws, "prefix": PLAN_PREFIX, "limit": opts.inquiry_plan_limit}
                )
            created = 0
            for row in rows:
                job_id = await enqueue_plan_job(
                    conn,
                    actor,
                    listing_id=row["id"],
                    revision_id=row["current_revision_id"],
                    reason="reconciliation_sweep",
                    suffix=f"contact-{row['contact_id']}",
                )
                created += int(job_id is not None)
            return created

        async def sends(conn: Conn) -> int:
            async with mapped_errors():
                rows = await fetch_all(conn, _ORPHAN_QUEUED_SQL, {"ws": ws, "limit": opts.inquiry_send_limit})
            created = 0
            for row in rows:
                job_id = await enqueue_send_job(
                    conn,
                    actor,
                    inquiry_id=row["id"],
                    listing_id=row["listing_id"],
                    attempt_number=int(row["attempts"]) + 1,
                )
                created += int(job_id is not None)
            return created

        async def reconciles(conn: Conn) -> int:
            async with mapped_errors():
                rows = await fetch_all(
                    conn,
                    _UNCERTAIN_SQL,
                    {
                        "ws": ws,
                        "after": opts.inquiry_reconcile_after,
                        "horizon": opts.inquiry_reconcile_horizon,
                        "limit": opts.inquiry_reconcile_limit,
                    },
                )
            created = 0
            for row in rows:
                job_id = await enqueue_reconcile_job(
                    conn, actor, inquiry_id=row["id"], listing_id=row["listing_id"], bucket=row["bucket"]
                )
                created += int(job_id is not None)
            return created

        async def replies(conn: Conn) -> int:
            async with mapped_errors():
                rows = await fetch_all(
                    conn,
                    _UNPROCESSED_REPLIES_SQL,
                    {
                        "ws": ws,
                        "prefix": REPLY_PROCESS_PREFIX,
                        "horizon": opts.reply_process_horizon,
                        "limit": opts.reply_process_limit,
                    },
                )
            created = 0
            for row in rows:
                job_id = await enqueue_reply_process_job(
                    conn,
                    actor,
                    reply_id=row["id"],
                    inquiry_id=row["inquiry_id"],
                    listing_id=row["listing_id"],
                )
                created += int(job_id is not None)
            return created

        report.inquiry_plan_jobs = await _step(self.ctx, actor, plans, dry_run=report.dry_run)
        report.inquiry_send_jobs = await _step(self.ctx, actor, sends, dry_run=report.dry_run)
        report.inquiry_reconcile_jobs = await _step(self.ctx, actor, reconciles, dry_run=report.dry_run)
        report.reply_process_jobs = await _step(self.ctx, actor, replies, dry_run=report.dry_run)

    # ------------------------------------------------------------------ 6 metrics

    async def _metrics(self, actor: ActorContext, report: ReconcileReport) -> None:
        del report
        async with unit_of_work(self.ctx.db, actor) as conn:
            stats = await jobs.queue_stats(conn, actor)
        self.ctx.metrics.set_queue_depth({key: count for key, count in stats.depth.items()})
        for job_type, age in stats.oldest_due_age.items():
            self.ctx.metrics.set_queue_oldest_age(job_type, age)


async def run_reconciliation(
    ctx: RuntimeContext, *, dry_run: bool = False, options: ReconcileOptions | None = None
) -> list[ReconcileReport]:
    """One pass over every active workspace (``suv-deals reconcile [--dry-run]``)."""
    return await Reconciler(ctx, options).run_once(dry_run=dry_run)


async def run_reconciler(
    settings: Settings, *, stop: anyio.Event | None = None, options: ReconcileOptions | None = None
) -> None:
    """Process entry point: a pass every ``interval_seconds`` until SIGTERM/SIGINT."""
    ctx = await build_runtime(settings, application_name="suv-deals-reconciler", configure_logs=True)
    stop = stop or anyio.Event()
    reconciler = Reconciler(ctx, options)
    try:
        async with anyio.create_task_group() as tg:

            async def watch_signals() -> None:
                with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                    async for _signum in signals:
                        stop.set()
                        return

            async def work() -> None:
                await reconciler.run(stop)
                tg.cancel_scope.cancel()

            tg.start_soon(watch_signals)
            tg.start_soon(work)
    finally:
        await ctx.aclose()


__all__ = [
    "STALE_DETAIL_REASON",
    "WATCH_RECHECK_REASON",
    "ReconcileOptions",
    "ReconcileReport",
    "Reconciler",
    "run_reconciler",
    "run_reconciliation",
]
