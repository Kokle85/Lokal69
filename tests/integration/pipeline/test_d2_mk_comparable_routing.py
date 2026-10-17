"""F2 (wave D2): an enabled ``mk_comparable`` source feeds MK market evidence, never acquisitions.

Before D2 the scheduler only scheduled ``acquisition`` sources, so an MK comparable source would
never be crawled even once its adapter and terms existed, and nothing turned a crawled MK ad into
``app.market_observations``. Now:

- the scheduler schedules ``mk_comparable`` sources like acquisition sources;
- a detail page of an ``mk_comparable`` source becomes ONE ``asking_price`` market observation
  (market MK, the ad's URL, source and listing ids; the frozen lineage of the listing) and is
  never ingested as an acquisition revision: no screening, no valuation, no inquiry;
- re-fetching an unchanged ad records nothing new (content-derived observation id).

The pages are the SYNTHETIC dealer fixture pages served from memory, registered here under a
fixture ``mk_comparable`` source (no network; no real MK site).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.pipeline.support import (
    PipelineEnv,
    build_env,
    by_slid,
    fixture_source_config,
    jobs_of,
    run,
)

from suv_deals.clock import SystemClock
from suv_deals.crawling.scheduler import run_scheduler_tick
from suv_deals.domain.enums import JobType
from suv_deals.persistence import listings_repo
from suv_deals.persistence.database import Conn
from suv_deals.workers.runner import Worker

pytestmark = pytest.mark.db


@pytest.fixture
async def mk_env(db_url: str, seed: Seed) -> AsyncIterator[PipelineEnv]:
    env = await build_env(
        db_url, seed, source=fixture_source_config(role="mk_comparable", country="MK"), name="MK routing"
    )
    try:
        yield env
    finally:
        await env.close()


async def _crawl(env: PipelineEnv) -> None:
    await run_scheduler_tick(env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id])
    await Worker(env.ctx, workspace_ids=[env.workspace_id], worker_id="mk-worker").run_until_idle()


def _observations(env: PipelineEnv) -> list[dict[str, object]]:
    return env.rows(
        "select id, evidence_kind, market, source_id, listing_id, source_listing_id, url, is_fixture,"
        " amount_minor, currency from app.market_observations where workspace_id = %s"
        " order by source_listing_id",
        env.workspace_id,
    )


async def test_mk_comparable_source_is_scheduled_and_feeds_market_evidence(mk_env: PipelineEnv) -> None:
    env = mk_env
    await _crawl(env)
    discovery = jobs_of(env, JobType.DISCOVERY)
    assert discovery and discovery[0]["state"] == "succeeded"  # the scheduler scheduled it
    details = [j for j in jobs_of(env, JobType.DETAIL) if j["state"] == "succeeded"]
    assert details
    observations = _observations(env)
    assert observations, "an MK comparable detail page records market evidence"
    priced = {o["source_listing_id"] for o in observations}
    listings = by_slid(env)
    for row in observations:
        assert row["evidence_kind"] == "asking_price" and row["market"] == "MK"
        assert row["source_id"] == env.source_id and row["is_fixture"] is True
        assert row["listing_id"] == listings[str(row["source_listing_id"])]["id"]
        assert str(row["url"]).startswith("https://dealer.example/")
    # Never an acquisition candidate: no revision, no screening, no valuation, no inquiry.
    for slid in priced:
        listing = listings[str(slid)]
        assert listing["current_revision_id"] is None and listing["eligibility_state"] is None
    assert jobs_of(env, JobType.VALUATION) == []
    assert jobs_of(env, JobType.SELLER_INQUIRY_PLAN) == []

    # An unchanged ad fetched again (an owner recheck) records nothing new.
    before = len(observations)
    target = observations[0]["listing_id"]
    request = SimpleNamespace(
        listing_id=target, reason="recheck MK ad (synthetic)", idempotency_key="mk-recheck-1"
    )

    async def recheck(conn: Conn) -> object:
        return await listings_repo.request_recheck(conn, env.owner, request)  # type: ignore[arg-type]

    await run(env.ctx, env.owner, recheck)
    env.refill_budgets()
    await Worker(env.ctx, workspace_ids=[env.workspace_id], worker_id="mk-worker-2").run_until_idle()
    rechecks = [j for j in jobs_of(env, JobType.RECHECK) if j["listing_id"] == target]
    assert rechecks and rechecks[-1]["state"] == "succeeded", rechecks
    assert rechecks[-1]["result_reference"]["market_observation"] == "unchanged"
    assert len(_observations(env)) == before
