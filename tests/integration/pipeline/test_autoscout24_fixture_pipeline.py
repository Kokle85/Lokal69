"""AutoScout24 Italy and Germany through the runtime pipeline over their SAVED synthetic fixtures.

Activation step 5, pipeline half (spec 9, 10, 25, 31). The REAL source configuration
(``config/sources/autoscout24_*.yaml``) is switched IN MEMORY to ``mode: fixture`` and
``enabled: true`` for one throwaway test workspace, so the runtime serves the saved SYNTHETIC pages
(``tests/adapters/fixtures/autoscout24_*``) through the offline `FixtureCrawlClient`: nothing is
fetched from a network, while the URL policy, budget gate, leases and evidence handling run
unchanged. One discovery job capped at one page (what ``suv-deals crawl once --max-pages 1``
enqueues) is processed by the worker, then the detail jobs it queued: Italy fetches the four saved
detail pages, Germany (card_only) ingests the cards' own facts and never requests a detail page.
The YAML files stay disabled; this proves wiring, never access to the live sites.
"""

from __future__ import annotations

import uuid
from collections import Counter
from pathlib import Path
from uuid import UUID

import pytest
from tests.adapters.autoscout24_fixture_set import (
    FIXTURES,
    IT_DAMAGED,
    IT_NET,
    IT_NORMAL,
    IT_SEARCH_CARDS,
    model_search_urls,
)
from tests.adapters.autoscout24_synthetic import detail_url, uuid_for
from tests.integration.db.helpers import Seed
from tests.integration.pipeline.support import PipelineEnv, build_env, by_slid, jobs_of, run

from suv_deals.adapters.registry import load_registry
from suv_deals.crawling.discovery import DEFAULT_PARTITION
from suv_deals.domain.enums import JobState, JobType
from suv_deals.domain.sources import SourceConfig
from suv_deals.persistence import jobs
from suv_deals.persistence.database import Conn
from suv_deals.workers.runner import Worker

pytestmark = pytest.mark.db

REPO = Path(__file__).resolve().parents[3]


def _fixture_mode(key: str) -> SourceConfig:
    """The real configuration, served from saved fixtures and enabled in memory only."""
    config = load_registry(REPO / "config").config(key)
    assert config.enabled is False  # the YAML itself stays disabled
    return SourceConfig.model_validate({**config.model_dump(), "mode": "fixture", "enabled": True})


async def _env(db_url: str, seed: Seed, key: str) -> PipelineEnv:
    return await build_env(
        db_url,
        seed,
        source=_fixture_mode(key),
        fixture_dirs=[FIXTURES / key],
        repository_taxonomy=True,
        name=f"AS24 fixtures {key}",
    )


async def _enqueue_one_page_discovery(env: PipelineEnv) -> UUID:
    """The discovery job ``suv-deals crawl once --max-pages 1`` enqueues (highest priority)."""
    actor = env.system
    profile_id = env.profiles["primary"]

    async def go(conn: Conn) -> UUID:
        spec = jobs.JobSpec(
            job_type=JobType.DISCOVERY,
            dedup_key=f"test-as24-fixtures:{uuid.uuid4().hex}",
            payload={
                "source_id": str(env.source_id),
                "profile_id": str(profile_id),
                "partition_key": DEFAULT_PARTITION,
                "requested_by": "operator-cli",
                "max_pages": 1,
            },
            priority=1000,
            max_attempts=1,
            partition_key=DEFAULT_PARTITION,
            source_id=env.source_id,
            profile_id=profile_id,
        )
        job_id, _created = await jobs.enqueue(conn, actor, spec)
        return job_id

    return await run(env.ctx, actor, go)


async def _drain(env: PipelineEnv, worker_id: str) -> Counter[JobType]:
    reports = await Worker(env.ctx, workspace_ids=[env.workspace_id], worker_id=worker_id).run_until_idle()
    assert all(r.state == JobState.SUCCEEDED for r in reports), [
        (r.job_type, r.state, r.code) for r in reports
    ]
    [discovery] = [r for r in reports if r.job_type == JobType.DISCOVERY]
    assert discovery.details.get("pages") == 1, discovery.details
    assert discovery.details.get("completeness") == "budget_limited", discovery.details  # 12 models, cap 1
    return Counter(r.job_type for r in reports)


def _revision_counts(env: PipelineEnv) -> dict[str, int]:
    rows = env.rows(
        "select l.source_listing_id, count(r.id) as revisions from app.listings l"
        " left join app.listing_revisions r on r.workspace_id = l.workspace_id and r.listing_id = l.id"
        " where l.workspace_id = %s group by l.source_listing_id",
        env.workspace_id,
    )
    return {r["source_listing_id"]: int(r["revisions"]) for r in rows}


async def test_italian_discovery_and_details_over_the_saved_fixtures(db_url: str, seed: Seed) -> None:
    env = await _env(db_url, seed, "autoscout24_it")
    try:
        await _enqueue_one_page_discovery(env)
        by_type = await _drain(env, "worker-as24-it")
        assert by_type[JobType.DISCOVERY] == 1 and by_type[JobType.DETAIL] == len(IT_SEARCH_CARDS)
        assert env.ctx.fixture_client is not None
        requests = [(r.url, r.purpose) for r in env.ctx.fixture_client.requests]
        assert requests[0] == (model_search_urls("autoscout24_it")[0], "search")
        assert sorted(requests[1:]) == sorted((detail_url(n), "detail") for n in IT_SEARCH_CARDS)
        assert all(job["state"] == "succeeded" for job in jobs_of(env, JobType.DETAIL))
        fetches = env.rows(
            "select purpose, host, access_state from ops.fetch_attempts where workspace_id = %s",
            env.workspace_id,
        )
        assert Counter(f["purpose"] for f in fetches) == {"search": 1, "detail": len(IT_SEARCH_CARDS)}
        assert {(f["host"], f["access_state"]) for f in fetches} == {("www.autoscout24.it", "ok")}

        listings = by_slid(env)
        assert set(listings) == {uuid_for(n) for n in IT_SEARCH_CARDS}
        assert all(row["current_revision_id"] is not None for row in listings.values())
        assert all(count >= 1 for count in _revision_counts(env).values())
        states = {n: listings[uuid_for(n)]["eligibility_state"] for n in IT_SEARCH_CARDS}
        assert states[IT_NORMAL] == "eligible_primary"  # EUR 2,900, clean page
        assert states[IT_NET] == "needs_facts"  # net-price wording
        assert states[IT_DAMAGED] == "rejected"  # damaged: no full asking price
        # Data served from saved fixtures is fixture lineage, frozen at ingest.
        assert env.scalar(
            "select bool_and(is_fixture) from app.listings where workspace_id = %s", env.workspace_id
        )
        evidence = env.scalar(
            "select count(*) from app.field_evidence where workspace_id = %s", env.workspace_id
        )
        assert evidence > 0
    finally:
        await env.close()


async def test_german_card_only_discovery_never_requests_a_detail_page(db_url: str, seed: Seed) -> None:
    env = await _env(db_url, seed, "autoscout24_de")
    try:
        await _enqueue_one_page_discovery(env)
        by_type = await _drain(env, "worker-as24-de")
        assert by_type[JobType.DISCOVERY] == 1 and by_type[JobType.DETAIL] == 3  # card ingestion jobs
        assert env.ctx.fixture_client is not None
        assert [(r.url, r.purpose) for r in env.ctx.fixture_client.requests] == [
            (model_search_urls("autoscout24_de")[0], "search")
        ]
        listings = by_slid(env)
        assert set(listings) == {uuid_for(n) for n in (501, 502, 503)}
        # card_only: each listing has a card-sourced revision (no detail page was ever fetched).
        assert all(row["current_revision_id"] is not None for row in listings.values())
        assert all(count >= 1 for count in _revision_counts(env).values())
        assert env.scalar(
            "select bool_and(is_fixture) from app.listings where workspace_id = %s", env.workspace_id
        )
        # The only recorded fetch is the search page (the card ingestion jobs request nothing).
        fetches = env.rows(
            "select purpose, host, access_state from ops.fetch_attempts where workspace_id = %s",
            env.workspace_id,
        )
        assert fetches == [{"purpose": "search", "host": "www.autoscout24.de", "access_state": "ok"}]
    finally:
        await env.close()
