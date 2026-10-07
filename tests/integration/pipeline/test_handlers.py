"""Discovery and detail handler edge cases (spec 9 budgets/watermarks, detail rules, access blocks).

SYNTHETIC fixture source only; nothing is fetched from a network.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.pipeline.support import (
    DEALER_DE,
    RATE_LIMITED_SEARCH_URL,
    PipelineEnv,
    build_env,
    by_slid,
    fixture_source_config,
    pipeline_settings,
    run,
    seed_comparables,
    seed_fx,
)

from suv_deals.adapters.base import DiscoveryPage, FetchPurpose, ParserHealth, RawDocument, SearchRequest
from suv_deals.adapters.fixture_client import FixtureCrawlClient
from suv_deals.clock import SystemClock
from suv_deals.crawling import discovery
from suv_deals.crawling.detail import CARD_ONLY_SKIP
from suv_deals.crawling.scheduler import run_scheduler_tick
from suv_deals.domain.enums import AccessState, JobState, JobType, SourceMode
from suv_deals.errors import VersionConflict
from suv_deals.persistence import jobs, listings_repo
from suv_deals.persistence.database import Conn
from suv_deals.workers.reconciliation import ReconcileOptions, Reconciler
from suv_deals.workers.runner import Worker
from suv_deals.workers.runtime import RuntimeContext, offline_fixture_resolver

pytestmark = pytest.mark.db


def worker(env: PipelineEnv, name: str, *types: JobType) -> Worker:
    return Worker(env.ctx, workspace_ids=[env.workspace_id], worker_id=name, job_types=list(types) or None)


async def tick(env: PipelineEnv) -> None:
    await run_scheduler_tick(env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id])


def set_source_config(env: PipelineEnv, path: str, value: str) -> None:
    """Change one stored source configuration value (SYNTHETIC arrangement, as a sync would)."""
    env.seed.conn.execute(
        "update app.sources set config = jsonb_set(config, %s::text[], %s::jsonb, true) where id = %s",
        ("{" + path + "}", value, env.source_id),
    )


async def rerun_discovery(env: PipelineEnv, key: str) -> str:
    profile_id = env.profiles["primary"]

    async def enqueue(conn: Conn) -> None:
        await jobs.enqueue(
            conn,
            env.system,
            jobs.JobSpec(
                job_type=JobType.DISCOVERY,
                dedup_key=f"discovery:{key}",
                source_id=env.source_id,
                profile_id=profile_id,
                partition_key="default",
            ),
        )

    await run(env.ctx, env.system, enqueue)
    [report] = await worker(env, f"worker-{key}", JobType.DISCOVERY).run_until_idle()
    assert report.state == JobState.SUCCEEDED
    return str(report.details["completeness"])


def absences(env: PipelineEnv) -> int:
    return int(
        env.scalar(
            "select count(*) from app.availability_events where workspace_id = %s"
            " and evidence_kind = 'complete_scan_absence'",
            env.workspace_id,
        )
    )


async def test_absences_are_marked_only_after_a_complete_traversal(env: PipelineEnv) -> None:
    # A: complete two-page traversal, details fetched (listings become available).
    await tick(env)
    await worker(env, "worker-all").run_until_idle()
    assert by_slid(env)["TEST-208"]["availability"] == "available"
    # B: the per-run page budget stops after page 1: budget_limited, cursor kept, NO absence.
    set_source_config(env, "rate_budget", '{"max_search_pages_per_run": 1}')
    assert await rerun_discovery(env, "budget-limited") == "budget_limited"
    [schedule] = env.rows(
        "select cursor, gap_reasons from ops.source_schedules where workspace_id = %s", env.workspace_id
    )
    assert schedule["cursor"] is not None and "seite=2" in str(schedule["cursor"])
    assert any("page budget" in g for g in schedule["gap_reasons"])
    assert absences(env) == 0 and by_slid(env)["TEST-208"]["availability"] == "available"
    # C: a COMPLETE traversal that no longer shows some cards: unknown (never removed or sold).
    set_source_config(env, "rate_budget", '{"max_search_pages_per_run": 2}')
    set_source_config(env, "search,search_url", '"https://dealer.example/suche?typ=liste"')
    assert await rerun_discovery(env, "complete-smaller") == "complete"
    listings = by_slid(env)
    # The smaller page lists its cards without provider ids (anchor fallback, URL identities), so
    # none of the four previously available listings was shown by this complete traversal.
    assert absences(env) == 4
    previous = ("TEST-204", "TEST-206", "TEST-207", "TEST-208")
    assert {s: listings[s]["availability"] for s in previous} == dict.fromkeys(previous, "unknown")
    sold = [
        r for s, r in listings.items() if s.startswith("urlsha256:") and r["availability"] == "sold_claimed"
    ]
    assert len(sold) == 1  # a sold claim is not overwritten by absence
    assert all(r["availability"] != "removed" for r in listings.values())


async def test_card_only_source_never_fetches_detail_pages(env: PipelineEnv) -> None:
    await tick(env)
    await worker(env, "worker-discovery", JobType.DISCOVERY).run_until_idle()
    env.seed.conn.execute("update app.sources set detail_mode = 'card_only' where id = %s", (env.source_id,))
    fetches_before = env.scalar(
        "select count(*) from ops.fetch_attempts where workspace_id = %s", env.workspace_id
    )
    reports = await worker(env, "worker-detail", JobType.DETAIL).run_until_idle()
    assert len(reports) == 5
    assert all(r.state == JobState.SUCCEEDED and r.details == {"skipped": CARD_ONLY_SKIP} for r in reports)
    fetches_after = env.scalar(
        "select count(*) from ops.fetch_attempts where workspace_id = %s", env.workspace_id
    )
    assert fetches_after == fetches_before
    assert (
        env.scalar("select count(*) from app.listing_revisions where workspace_id = %s", env.workspace_id)
        == 0
    )


async def test_login_wall_on_a_detail_page_blocks_the_job_and_pauses_the_route(env: PipelineEnv) -> None:
    await tick(env)
    await worker(env, "worker-discovery", JobType.DISCOVERY).run_until_idle()
    await worker(env, "worker-detail", JobType.DETAIL, JobType.VALUATION).run_until_idle()
    listing_id = by_slid(env)["TEST-204"]["id"]
    # The stored canonical URL now leads to a login wall (SYNTHETIC fixture route TEST-214).
    env.seed.conn.execute(
        "update app.listings set canonical_url = 'https://dealer.example/fahrzeug/TEST-214' where id = %s",
        (listing_id,),
    )

    async def recheck(conn: Conn) -> None:
        ref = await listings_repo.request_detail_refresh(
            conn, env.system, listing_id, reason="synthetic recheck"
        )
        assert ref is not None

    await run(env.ctx, env.system, recheck)
    [report] = await worker(env, "worker-recheck", JobType.RECHECK).run_until_idle()
    assert report.state == JobState.BLOCKED and report.code == "access_blocked"
    status = env.scalar("select technical_status from app.sources where id = %s", env.source_id)
    assert status == "access_blocked"
    revisions = env.scalar("select count(*) from app.listing_revisions where listing_id = %s", listing_id)
    assert revisions == 1  # nothing was ingested from the login wall


class _ContractStub(FixtureCrawlClient):
    """Offline stand-in for the crawler service: serves the SYNTHETIC fixture pages and reports
    a configurable contract-check result (no network; DNS answered by the offline resolver)."""

    def __init__(self, *, ok: bool) -> None:
        super().__init__([DEALER_DE])
        self.ok = ok
        self.inspections = 0

    async def inspect_contract(self) -> object:
        self.inspections += 1
        return SimpleNamespace(ok=self.ok, problems=() if self.ok else ("crawler_unreachable",))


@pytest.mark.parametrize("ready", [False, True])
async def test_real_sources_need_network_enabled_and_a_ready_crawler(
    db_url: str, seed: Seed, ready: bool
) -> None:
    settings = pipeline_settings(db_url, source_network_enabled=True)
    real = fixture_source_config().model_copy(update={"mode": SourceMode.PUBLIC_HTML})
    env = await build_env(db_url, seed, settings=settings, source=real)
    try:
        stub = _ContractStub(ok=ready)
        env.ctx.network_client = stub
        env.ctx.resolver = offline_fixture_resolver
        await tick(env)
        [report] = await worker(env, "worker-real", JobType.DISCOVERY).run_until_idle()
        assert stub.inspections == 1
        if ready:
            assert report.state == JobState.SUCCEEDED and report.details["completeness"] == "complete"
            assert len(stub.requests) == 2  # two search pages, through the policy client
        else:
            assert report.state == JobState.RETRY_WAIT and report.code == "DEPENDENCY_UNAVAILABLE"
            assert stub.requests == []  # nothing is fetched through an unready crawler
    finally:
        await env.close()


class _SearchLeaseThief(FixtureCrawlClient):
    """Fixture client that runs ``hook`` once while a SEARCH page is fetched (crash simulation)."""

    def __init__(self, hook: Callable[[], None]) -> None:
        super().__init__([DEALER_DE])
        self.hook: Callable[[], None] | None = hook

    async def fetch(self, url: str, *, purpose: FetchPurpose, source_key: str) -> RawDocument:
        document = await super().fetch(url, purpose=purpose, source_key=source_key)
        if purpose == "search" and self.hook is not None:
            hook, self.hook = self.hook, None
            hook()
        return document


def crawl_runs(env: PipelineEnv) -> list[dict[str, object]]:
    return env.rows(
        "select outcome, gap_reasons from ops.crawl_runs where workspace_id = %s order by started_at, id",
        env.workspace_id,
    )


def expire_running_discovery(env: PipelineEnv) -> None:
    env.seed.conn.execute(
        "update ops.jobs set lease_expires_at = clock_timestamp() - interval '1 second'"
        " where workspace_id = %s and state = 'running' and job_type = 'discovery'",
        (env.workspace_id,),
    )


async def test_a_run_interrupted_by_a_lost_lease_is_closed_by_the_next_attempt(env: PipelineEnv) -> None:
    """Spec 9/13: the lease-lost attempt commits nothing and cannot finish its run; the next lease
    holder closes that run (cancelled, gap recorded) instead of leaving it ``running`` forever."""
    await tick(env)
    env.ctx.fixture_client = _SearchLeaseThief(lambda: expire_running_discovery(env))
    [lost] = await worker(env, "worker-lost", JobType.DISCOVERY).run_until_idle()
    assert lost.lease_lost and lost.state is None
    assert [r["outcome"] for r in crawl_runs(env)] == ["running"]
    assert env.scalar("select count(*) from app.listings where workspace_id = %s", env.workspace_id) == 0
    reaped = await Reconciler(env.ctx, ReconcileOptions(job_retry_delay_seconds=0)).reconcile_workspace(
        env.workspace_id
    )
    assert reaped.jobs_requeued == 1 and reaped.crawl_runs_closed == 0  # the job will retry
    [done] = await worker(env, "worker-next", JobType.DISCOVERY).run_until_idle()
    assert done.state == JobState.SUCCEEDED and done.attempt == 2
    runs = crawl_runs(env)
    assert [r["outcome"] for r in runs] == ["cancelled", "complete"]
    assert any("superseded by attempt 2" in g for g in runs[0]["gap_reasons"])  # type: ignore[attr-defined]


async def test_the_reconciler_closes_the_run_of_a_job_that_ended(env: PipelineEnv) -> None:
    await tick(env)
    env.ctx.fixture_client = _SearchLeaseThief(lambda: expire_running_discovery(env))
    await worker(env, "worker-lost", JobType.DISCOVERY).run_until_idle()
    # No attempt is left: the reaper dead-letters the job, so no later attempt closes the run.
    env.seed.conn.execute(
        "update ops.jobs set max_attempts = attempts where workspace_id = %s and job_type = 'discovery'",
        (env.workspace_id,),
    )
    reconciler = Reconciler(env.ctx, ReconcileOptions())
    dry = await reconciler.reconcile_workspace(env.workspace_id, dry_run=True)
    assert dry.dry_run and dry.errors == []
    assert [r["outcome"] for r in crawl_runs(env)] == ["running"]  # a dry run changes nothing
    report = await reconciler.reconcile_workspace(env.workspace_id)
    assert report.errors == [] and report.jobs_dead_lettered == 1 and report.crawl_runs_closed == 1
    [run_row] = crawl_runs(env)
    assert run_row["outcome"] == "cancelled"
    assert any("dead_letter" in g for g in run_row["gap_reasons"])  # type: ignore[attr-defined]
    again = await reconciler.reconcile_workspace(env.workspace_id)
    assert again.crawl_runs_closed == 0


async def test_suspected_parser_drift_never_marks_absences(
    env: PipelineEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 9/25: a complete traversal whose parser health is suspect is recorded (source degraded)
    but is no evidence about the inventory: nothing becomes ``unknown``."""
    await tick(env)
    await worker(env, "worker-all").run_until_idle()
    set_source_config(env, "search,search_url", '"https://dealer.example/suche?typ=liste"')
    degraded = ParserHealth(
        status="degraded", sample_size=12, reasons=("SYNTHETIC sharp fall in listing count",)
    )
    monkeypatch.setattr(discovery, "_health_status", lambda *_args: degraded)
    assert await rerun_discovery(env, "drift") == "complete"
    assert absences(env) == 0
    listings = by_slid(env)
    assert all(listings[s]["availability"] == "available" for s in ("TEST-204", "TEST-206", "TEST-207"))
    status = env.scalar("select technical_status from app.sources where id = %s", env.source_id)
    assert status == "degraded"


async def test_an_exhausted_retry_is_reported_and_counted_as_a_dead_letter(db_url: str, seed: Seed) -> None:
    """The handler reports the APPLIED job state: a 429 retry with no attempt left is a dead letter
    (visible, counted), never reported as ``retry_wait``."""
    env = await build_env(db_url, seed, source=fixture_source_config(search_url=RATE_LIMITED_SEARCH_URL))
    try:
        await tick(env)
        env.seed.conn.execute(
            "update ops.jobs set max_attempts = 1 where workspace_id = %s and job_type = 'discovery'",
            (env.workspace_id,),
        )
        [report] = await worker(env, "worker-exhausted", JobType.DISCOVERY).run_until_idle()
        assert report.state == JobState.DEAD_LETTER and report.code == "RATE_LIMITED"
        state = env.scalar(
            "select state from ops.jobs where workspace_id = %s and job_type = 'discovery'", env.workspace_id
        )
        assert state == "dead_letter"
        counted = env.ctx.metrics.registry.get_sample_value(
            "suv_deals_dead_letters_total", {"job_type": "discovery"}
        )
        assert counted == 1.0
    finally:
        await env.close()


class _LoopingAdapter:
    """Wraps the real adapter: its LAST page claims a next page that links back to page 1."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.first_url: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def discover(self, request: SearchRequest, client: Any) -> DiscoveryPage:
        page: DiscoveryPage = await self._inner.discover(request, client)
        self.first_url = self.first_url or request.url
        if page.access_state == AccessState.OK and not page.has_more:
            return page.model_copy(update={"has_more": True, "next_url": self.first_url})
        return page


async def test_a_pagination_cycle_is_never_refetched(
    env: PipelineEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "next" link back to an already fetched page ends the traversal (``partial``, gap recorded)
    instead of re-fetching the same pages until the per-run page budget is used up."""
    set_source_config(env, "rate_budget", '{"max_search_pages_per_run": 6}')
    original = RuntimeContext.open_crawl_session

    async def looping(self: RuntimeContext, workspace_id: Any, source: Any) -> Any:
        session = await original(self, workspace_id, source)
        session.adapter = _LoopingAdapter(session.adapter)  # type: ignore[assignment]
        return session

    monkeypatch.setattr(RuntimeContext, "open_crawl_session", looping)
    assert await rerun_discovery(env, "pagination-loop") == "partial"
    fetches = env.scalar(
        "select count(*) from ops.fetch_attempts where workspace_id = %s and purpose = 'search'",
        env.workspace_id,
    )
    assert fetches == 2  # pages 1 and 2 once each; page 1 is never fetched again
    [run_row] = env.rows(
        "select outcome, pages_fetched, gap_reasons from ops.crawl_runs where workspace_id = %s",
        env.workspace_id,
    )
    assert run_row["outcome"] == "partial" and run_row["pages_fetched"] == 2
    assert any("pagination loop" in g for g in run_row["gap_reasons"])
    assert absences(env) == 0  # an incomplete traversal is no evidence about the inventory


async def test_a_concurrent_change_before_the_valuation_commit_is_retried_not_dropped(
    env: PipelineEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``persist_valuation`` refuses a calculation whose inputs changed meanwhile (VERSION_CONFLICT,
    "recompute"). The whole commit rolls back, so the job must run again with fresh reads; a dead
    letter would leave the candidate without a valuation AND without its pending review case."""
    from suv_deals.persistence import valuation_repo  # noqa: PLC0415

    await seed_comparables(env)
    await seed_fx(env)
    await tick(env)
    await worker(env, "worker-crawl", JobType.DISCOVERY, JobType.DETAIL).run_until_idle()
    eligible = by_slid(env)["TEST-204"]["id"]
    real_persist = valuation_repo.persist_valuation
    conflicts: list[Any] = []

    async def conflicting(conn: Any, actor: Any, valuation: Any, refs: Any, inputs: Any) -> Any:
        if refs.listing_id == eligible and not conflicts:
            conflicts.append(refs.listing_id)
            raise VersionConflict("The cost profile changed since the calculation; recompute")
        return await real_persist(conn, actor, valuation, refs, inputs)

    monkeypatch.setattr(valuation_repo, "persist_valuation", conflicting)
    first = await worker(env, "worker-value", JobType.VALUATION).run_until_idle()
    [conflicted] = [r for r in first if r.code == "VERSION_CONFLICT"]
    assert conflicted.state == JobState.RETRY_WAIT and conflicts == [eligible]
    cases = "select count(*) from app.review_cases where workspace_id = %s and listing_id = %s"
    assert env.scalar(cases, env.workspace_id, eligible) == 0  # rolled back with the valuation
    env.seed.conn.execute("update ops.jobs set available_at = now() where id = %s", (conflicted.job_id,))
    [again] = await worker(env, "worker-value-2", JobType.VALUATION).run_until_idle()
    assert (again.job_id, again.state, again.attempt) == (conflicted.job_id, JobState.SUCCEEDED, 2)
    assert env.scalar(cases, env.workspace_id, eligible) == 1
