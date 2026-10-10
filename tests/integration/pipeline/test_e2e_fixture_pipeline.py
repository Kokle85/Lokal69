"""End-to-end SYNTHETIC fixture pipeline (spec 9, 10, 13, 14-18, 22, 25, 31 "Full pipeline").

scheduler tick -> discovery job -> worker (FixtureCrawlClient, no network) -> detail jobs ->
listings/revisions/evidence -> screening -> valuation job -> incomplete valuation (no approved tax
rules, unknowns explicit) -> pending review case -> blocked fixture ``review.pending`` outbox event ->
the dispatcher refuses every external delivery. Fixture tests prove wiring and dedup, never live
source access, external delivery or dot integration.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.pipeline.support import (
    BLOCKED_SEARCH_URL,
    DEALER_DE,
    RATE_LIMITED_SEARCH_URL,
    SOURCE_KEY,
    PipelineEnv,
    approve_events_route,
    build_env,
    by_slid,
    events_settings,
    fixture_source_config,
    jobs_of,
    run,
    seed_comparables,
    seed_fx,
    verified_subscriber,
)

from suv_deals.adapters.base import FetchPurpose, RawDocument
from suv_deals.adapters.fixture_client import FixtureCrawlClient
from suv_deals.clock import SystemClock
from suv_deals.crawling.scheduler import run_scheduler_tick
from suv_deals.domain.enums import JobState, JobType, SourceMode
from suv_deals.persistence import jobs, sources_repo
from suv_deals.persistence.database import Conn
from suv_deals.persistence.errors_map import LeaseLost
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.workers.dispatcher import Dispatcher
from suv_deals.workers.reconciliation import ReconcileOptions, Reconciler
from suv_deals.workers.runner import Worker

pytestmark = pytest.mark.db

TABLES = (
    "app.listings",
    "app.listing_revisions",
    "app.listing_observations",
    "app.detail_observations",
    "app.field_evidence",
    "app.valuations",
    "app.review_cases",
    "ops.outbox",
)


def counts(env: PipelineEnv) -> dict[str, int]:
    return {
        table: int(env.scalar(f"select count(*) from {table} where workspace_id = %s", env.workspace_id))
        for table in TABLES
    }


class _RecordingHttp:
    """A `SafeHttp` that must never be called in these scenarios."""

    def __init__(self, calls: list[Any]) -> None:
        self.calls = calls

    async def post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(("POST", url))
        raise AssertionError("no external request may be made")

    async def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(("GET", url))
        raise AssertionError("no external request may be made")


def _live(env: PipelineEnv) -> PipelineEnv:
    settings = events_settings(env.ctx.settings.database_url.get_secret_value())  # type: ignore[union-attr]
    return dataclasses.replace(env, ctx=dataclasses.replace(env.ctx, settings=settings, owns_db=False))


async def test_fixture_pipeline_end_to_end(env: PipelineEnv) -> None:
    await seed_comparables(env)
    await seed_fx(env)
    clock = SystemClock()

    # 1. Scheduler: one discovery job for the due (source, primary profile, default) slot.
    tick = await run_scheduler_tick(
        env.ctx.db, env.ctx.settings, clock, workspace_ids=[env.workspace_id], metrics=env.ctx.metrics
    )
    [workspace_tick] = [w for w in tick.workspaces if w.workspace_id == env.workspace_id]
    assert len(workspace_tick.enqueued) == 1 and workspace_tick.error is None
    [discovery] = jobs_of(env, JobType.DISCOVERY)
    assert discovery["id"] == workspace_tick.enqueued[0] and discovery["state"] == "queued"

    # 2. A second tick in the same slot, and a racing scheduler that still sees the schedule due,
    #    create no duplicate job (slot-unique key; the schedule advanced in the same commit).
    again = await run_scheduler_tick(env.ctx.db, env.ctx.settings, clock, workspace_ids=[env.workspace_id])
    assert all(not w.enqueued for w in again.workspaces if w.workspace_id == env.workspace_id)
    env.seed.conn.execute(
        "update ops.source_schedules set next_due_at = now() - interval '1 second' where workspace_id = %s",
        (env.workspace_id,),
    )
    raced = await run_scheduler_tick(env.ctx.db, env.ctx.settings, clock, workspace_ids=[env.workspace_id])
    assert all(not w.enqueued for w in raced.workspaces if w.workspace_id == env.workspace_id)
    assert len(jobs_of(env, JobType.DISCOVERY)) == 1

    # 3. Worker: discovery -> 5 detail jobs -> 5 revisions -> screening -> 3 valuation jobs, and
    #    ONE seller-inquiry plan job for the single inquiry candidate (eligible screening; spec 37.3).
    reports = await Worker(env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-e2e").run_until_idle()
    by_type: dict[JobType, list[Any]] = {}
    for report in reports:
        by_type.setdefault(report.job_type, []).append(report)
    assert {t: len(r) for t, r in by_type.items()} == {
        JobType.DISCOVERY: 1,
        JobType.DETAIL: 5,
        JobType.VALUATION: 3,
        JobType.SELLER_INQUIRY_PLAN: 1,
    }
    assert all(r.state == JobState.SUCCEEDED for r in reports), [(r.job_type, r.code) for r in reports]
    # Fixture lineage (and no linked seller): nothing is ever reserved or sent.
    assert (
        env.scalar("select count(*) from ops.inquiry_quota_ledger where workspace_id = %s", env.workspace_id)
        == 0
    )
    assert (
        env.scalar(
            "select count(*) from ops.email_delivery_attempts where workspace_id = %s", env.workspace_id
        )
        == 0
    )
    assert by_type[JobType.DISCOVERY][0].details["completeness"] == "complete"
    crawl_runs = env.rows(
        "select outcome, pages_fetched, cards_seen, new_listings from ops.crawl_runs where workspace_id = %s",
        env.workspace_id,
    )
    assert crawl_runs == [{"outcome": "complete", "pages_fetched": 2, "cards_seen": 5, "new_listings": 5}]

    listings = by_slid(env)
    eligible = listings["TEST-204"]
    assert (
        eligible["eligibility_state"] == "eligible_primary" and eligible["eligibility_profile"] == "primary"
    )
    [revision] = env.rows(
        "select asking_minor, currency, mileage_km from app.listing_revisions where id = %s",
        eligible["current_revision_id"],
    )
    assert revision == {"asking_minor": 275000, "currency": "EUR", "mileage_km": 187500}
    rejected = listings["TEST-208"]
    assert rejected["eligibility_state"] == "rejected"  # exactly 200,000 km fails (exclusive bound)
    assert (
        env.scalar(
            "select mileage_km from app.listing_revisions where id = %s", rejected["current_revision_id"]
        )
        == 200000
    )
    assert listings["TEST-207"]["eligibility_state"] == "needs_facts"  # net-only price wording
    evidence = env.scalar(
        "select count(*) from app.field_evidence where workspace_id = %s and listing_id = %s",
        env.workspace_id,
        eligible["id"],
    )
    assert evidence > 0
    rejected_valuations = env.scalar(
        "select count(*) from ops.jobs where workspace_id = %s and job_type = 'valuation'"
        " and listing_id = %s",
        env.workspace_id,
        rejected["id"],
    )
    assert rejected_valuations == 0

    # 4. Valuation: incomplete with explicit unknowns, never an invented tax figure; fixture lineage.
    [valuation] = env.rows(
        "select state, is_fixture, unknowns, comparable_set_id, tax_rule_set_id, base_contribution_minor"
        " from app.valuations where workspace_id = %s and listing_id = %s",
        env.workspace_id,
        eligible["id"],
    )
    assert valuation["state"] == "incomplete" and valuation["is_fixture"] is True
    assert valuation["tax_rule_set_id"] is None and valuation["base_contribution_minor"] is None
    assert valuation["comparable_set_id"] is not None
    assert any("no applicable ACTIVE rule set" in u for u in valuation["unknowns"])

    # 5. Review case pending + blocked fixture review.pending event (same transaction).
    [case] = env.rows(
        "select id, state, row_version, is_fixture, readiness from app.review_cases where workspace_id = %s",
        env.workspace_id,
    )
    assert case["state"] == "pending" and case["is_fixture"] is True
    assert case["readiness"] == "needs_import_costs"
    [event] = env.rows(
        "select event_id, event_type, state, blocker_code, is_fixture, payload from ops.outbox"
        " where workspace_id = %s",
        env.workspace_id,
    )
    assert event["event_type"] == "review.pending" and event["is_fixture"] is True
    assert event["state"] == "blocked" and event["blocker_code"] == "FIXTURE_EVENT"
    assert event["payload"]["case_id"] == str(case["id"]) and event["payload"]["fixture"] is True

    # 6. Dispatcher: external delivery disabled AND fixture -> nothing is claimed or sent.
    calls: list[Any] = []
    dispatched = await Dispatcher(env.ctx, http=_RecordingHttp(calls)).run_workspace(env.workspace_id)
    assert dispatched.events == [] and dispatched.deliveries == [] and calls == []
    # Even with native events allowed, an approved + verified route and a verified subscriber, a
    # fixture event is never claimed or delivered (spec 18: fixtures never notify).
    live = _live(env)
    await approve_events_route(live)
    await verified_subscriber(live)
    dispatched = await Dispatcher(live.ctx, http=_RecordingHttp(calls)).run_workspace(env.workspace_id)
    assert dispatched.events == [] and dispatched.deliveries == [] and calls == []
    assert env.scalar("select state from ops.outbox where event_id = %s", event["event_id"]) == "blocked"

    # 7. Re-running discovery replays the same cards: no new detail work, no duplicate revisions.
    before = counts(env)
    profile_id = env.profiles["primary"]

    async def enqueue(conn: Conn) -> None:
        await jobs.enqueue(
            conn,
            env.system,
            jobs.JobSpec(
                job_type=JobType.DISCOVERY,
                dedup_key="discovery:manual-rerun",
                payload={"source_id": str(env.source_id), "profile_id": str(profile_id)},
                source_id=env.source_id,
                profile_id=profile_id,
                partition_key="default",
            ),
        )

    await run(env.ctx, env.system, enqueue)
    rerun = await Worker(env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-e2e-2").run_until_idle()
    assert [(r.job_type, r.state) for r in rerun] == [(JobType.DISCOVERY, JobState.SUCCEEDED)]
    after = counts(env)
    assert after["app.listing_observations"] == before["app.listing_observations"] + 5  # new run evidence
    assert {k: v for k, v in after.items() if k != "app.listing_observations"} == {
        k: v for k, v in before.items() if k != "app.listing_observations"
    }
    assert len(jobs_of(env, JobType.DETAIL)) == 5


class _LeaseThief(FixtureCrawlClient):
    """Fixture client that runs ``hook`` once while a detail page is fetched (crash simulation)."""

    def __init__(self, hook: Callable[[], None]) -> None:
        super().__init__([DEALER_DE])
        self.hook: Callable[[], None] | None = hook

    async def fetch(self, url: str, *, purpose: FetchPurpose, source_key: str) -> RawDocument:
        document = await super().fetch(url, purpose=purpose, source_key=source_key)
        if purpose == "detail" and self.hook is not None:
            hook, self.hook = self.hook, None
            hook()
        return document


async def test_lost_lease_mid_detail_cannot_commit_and_the_reaper_recovers_it(env: PipelineEnv) -> None:
    await run_scheduler_tick(env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id])
    await Worker(
        env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-discovery", job_types=[JobType.DISCOVERY]
    ).run_until_idle()
    assert len(jobs_of(env, JobType.DETAIL)) == 5

    def expire_running_detail_leases() -> None:
        # The worker stalls mid-fetch long enough for its lease to expire (database time).
        env.seed.conn.execute(
            "update ops.jobs set lease_expires_at = clock_timestamp() - interval '1 second'"
            " where workspace_id = %s and state = 'running' and job_type = 'detail'",
            (env.workspace_id,),
        )

    env.ctx.fixture_client = _LeaseThief(expire_running_detail_leases)
    worker_a = Worker(
        env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-a", job_types=[JobType.DETAIL]
    )
    claimed = await worker_a.claim_next()
    assert claimed is not None and claimed.listing_id is not None
    lost = await worker_a.process(claimed)
    assert lost.lease_lost and lost.state is None
    observations = "select count(*) from app.detail_observations where workspace_id = %s and listing_id = %s"
    # Nothing of worker A's fetch was committed (the fenced unit of work rolled back).
    assert env.scalar(observations, env.workspace_id, claimed.listing_id) == 0
    # A late completion with the stale lease is refused as well.
    with pytest.raises(LeaseLost):
        async with unit_of_work(env.ctx.db, env.system) as conn:
            await jobs.complete(conn, claimed, {"late": True})

    reconciler = Reconciler(env.ctx, ReconcileOptions(job_retry_delay_seconds=0))
    reaped = await reconciler.reconcile_workspace(env.workspace_id)
    assert reaped.jobs_requeued == 1 and reaped.errors == []
    [row] = env.rows("select state, last_error_code, attempts from ops.jobs where id = %s", claimed.id)
    assert row == {"state": "retry_wait", "last_error_code": "LEASE_EXPIRED", "attempts": 1}

    worker_b = Worker(
        env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-b", job_types=[JobType.DETAIL]
    )
    done = await worker_b.run_until_idle()
    assert [(r.state, r.attempt) for r in done if r.job_id == claimed.id] == [(JobState.SUCCEEDED, 2)]
    assert env.scalar(observations, env.workspace_id, claimed.listing_id) == 1  # once logically
    revisions = env.scalar(
        "select count(*) from app.listing_revisions where workspace_id = %s and listing_id = %s",
        env.workspace_id,
        claimed.listing_id,
    )
    assert revisions == 1


async def test_access_blocked_search_pauses_the_source_route(db_url: str, seed: Seed) -> None:
    env = await build_env(db_url, seed, source=fixture_source_config(search_url=BLOCKED_SEARCH_URL))
    try:
        await run_scheduler_tick(
            env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id]
        )
        [report] = await Worker(
            env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-blocked"
        ).run_until_idle()
        assert report.state == JobState.BLOCKED and report.code == "access_blocked"
        [job] = jobs_of(env, JobType.DISCOVERY)
        assert job["state"] == "blocked" and job["blocker_code"] == "access_blocked"
        [source] = env.rows("select enabled, technical_status from app.sources where id = %s", env.source_id)
        assert source == {"enabled": False, "technical_status": "access_blocked"}
        gate = env.rows(
            "select status from ops.activation_gates where workspace_id = %s and capability = %s",
            env.workspace_id,
            f"source_access.{SOURCE_KEY}",
        )
        assert gate == [{"status": "blocked"}]  # exactly one deduplicated operational review item
        blocked_host = env.scalar(
            "select access_blocked_at is not null from ops.host_budgets"
            " where workspace_id = %s and host = 'dealer.example'",
            env.workspace_id,
        )
        assert blocked_host is True
        [crawl_run] = env.rows(
            "select outcome, access_state from ops.crawl_runs where workspace_id = %s", env.workspace_id
        )
        assert crawl_run == {"outcome": "blocked", "access_state": "access_blocked"}
        # No retry and no new slot job: the route stays paused until an explicit owner action.
        env.seed.conn.execute(
            "update ops.source_schedules set next_due_at = now() - interval '1 second',"
            " last_slot = last_slot - interval '15 minutes' where workspace_id = %s",
            (env.workspace_id,),
        )
        await run_scheduler_tick(
            env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id]
        )
        assert len(jobs_of(env, JobType.DISCOVERY)) == 1
        assert (
            await Worker(
                env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-blocked-2"
            ).run_until_idle()
            == []
        )
    finally:
        await env.close()


async def test_rate_limited_search_respects_retry_after(db_url: str, seed: Seed) -> None:
    env = await build_env(db_url, seed, source=fixture_source_config(search_url=RATE_LIMITED_SEARCH_URL))
    try:
        await run_scheduler_tick(
            env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id]
        )
        started = datetime.now(UTC)
        [report] = await Worker(
            env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-429"
        ).run_until_idle()
        assert report.state == JobState.RETRY_WAIT and report.code == "RATE_LIMITED"
        [job] = jobs_of(env, JobType.DISCOVERY)
        assert job["state"] == "retry_wait" and job["last_error_code"] == "RATE_LIMITED"
        # Retry-After: 120 is respected (never shortened) and persisted for every worker.
        assert job["available_at"] >= started + timedelta(seconds=115)
        retry_until = env.scalar(
            "select retry_after_until from ops.host_budgets where workspace_id = %s"
            " and host = 'dealer.example'",
            env.workspace_id,
        )
        assert retry_until is not None and retry_until >= started + timedelta(seconds=115)
        # Nothing is claimable before that time: no burst, no evasion.
        assert (
            await Worker(env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-429-b").run_until_idle()
            == []
        )
        status = env.scalar("select technical_status from app.sources where id = %s", env.source_id)
        assert status != "access_blocked"  # a 429 is not an access block
    finally:
        await env.close()


async def test_real_sources_never_run_while_source_network_is_disabled(env: PipelineEnv) -> None:
    """A non-fixture source is neither scheduled nor fetched with SOURCE_NETWORK_ENABLED=false."""
    real = fixture_source_config().model_copy(update={"mode": SourceMode.PUBLIC_HTML})

    async def sync(conn: Conn) -> None:
        await sources_repo.sync_sources_from_yaml(conn, env.system, [real])

    await run(env.ctx, env.system, sync)
    tick = await run_scheduler_tick(
        env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id]
    )
    [mine] = [w for w in tick.workspaces if w.workspace_id == env.workspace_id]
    assert mine.enqueued == () and mine.skipped == {"source_network_disabled": 1}
    # A job that exists anyway (e.g. enqueued before the switch) is blocked with a typed blocker.
    profile_id = env.profiles["primary"]

    async def enqueue(conn: Conn) -> None:
        await jobs.enqueue(
            conn,
            env.system,
            jobs.JobSpec(
                job_type=JobType.DISCOVERY,
                dedup_key="discovery:network-disabled",
                source_id=env.source_id,
                profile_id=profile_id,
                partition_key="default",
            ),
        )

    await run(env.ctx, env.system, enqueue)
    [report] = await Worker(
        env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-offline"
    ).run_until_idle()
    assert report.state == JobState.BLOCKED and report.code == "source_network_disabled"
    fetches = env.scalar("select count(*) from ops.fetch_attempts where workspace_id = %s", env.workspace_id)
    assert fetches == 0
