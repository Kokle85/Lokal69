"""AutoScout24 Italy and Germany through the runtime pipeline over their SAVED synthetic fixtures.

Activation step 5, pipeline half (spec 9, 10, 25, 31). The REAL source configuration
(``config/sources/autoscout24_*.yaml``) is switched IN MEMORY to ``mode: fixture`` and
``enabled: true`` for one throwaway test workspace, so the runtime serves the saved SYNTHETIC pages
(``tests/adapters/fixtures/autoscout24_*``) through the offline `FixtureCrawlClient`: nothing is
fetched from a network, while the URL policy, budget gate, leases and evidence handling run
unchanged. One discovery job capped at one page (what ``suv-deals crawl once --max-pages 1``
enqueues) is processed by the worker, then the detail jobs it queued: Italy fetches the four saved
detail pages, Germany (card_only) ingests the cards' own facts and never requests a detail page.
Before the first page the worker reads the host's saved SYNTHETIC robots.txt (one robots request,
counted in the host's budget, stored as the host's robots revision; the detail jobs reuse it).
The YAML files stay disabled; this proves wiring, never access to the live sites.

``suv-deals sources smoke-evidence`` (runbook 9.1, step 6) then reads back exactly what the run
persisted: the spec-31 evidence ids (run, job, fetch outcomes including the robots.txt request,
listings, revisions, field evidence), the robots revision in effect and the configuration revision.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from click.testing import CliRunner
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
from suv_deals.cli import cli
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


def _cli_smoke_evidence(db_url: str, env: PipelineEnv, key: str) -> dict[str, Any]:
    """``suv-deals sources smoke-evidence KEY`` exactly as the owner runs it (read-only)."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        result = CliRunner().invoke(
            cli,
            ["--no-env-file", "sources", "smoke-evidence", key, "--workspace", str(env.workspace_id)],
            env={
                "APP_ENV": "test",
                "DATABASE_URL": db_url,
                "DATABASE_SET_ROLE": "suv_backend",
                "DATABASE_POOL_MAX": "3",
                "BUILD_ID": "cli-test",
            },
            catch_exceptions=False,
        )
    finally:
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)
        root.setLevel(level)
    assert result.exit_code == 0, result.output
    evidence: dict[str, Any] = json.loads(result.stdout)
    return evidence


def _check_smoke_evidence(
    env: PipelineEnv, evidence: dict[str, Any], discovery_job: UUID, fetches: dict[str, int]
) -> None:
    """The command names every spec-31 evidence id the smoke persisted (and nothing else)."""
    [run_row] = env.rows("select id from ops.crawl_runs where workspace_id = %s", env.workspace_id)
    run = evidence["crawl_run"]
    assert (run["id"], run["job_id"]) == (str(run_row["id"]), str(discovery_job))
    assert (run["outcome"], run["access_state"], run["pages_fetched"]) == ("budget_limited", "ok", 1)
    assert run["adapter_version"] == evidence["source"]["adapter_version"] == "autoscout24@1.0.0"
    config = env.rows(
        "select id, revision from app.config_revisions where workspace_id = %s"
        " order by revision desc limit 1",
        env.workspace_id,
    )[0]
    assert (evidence["config_revision"]["id"], evidence["config_revision"]["revision"]) == (
        str(config["id"]),
        config["revision"],
    )
    # Redacted fetch outcomes: no URL is stored, so none is printed. The robots.txt request of the
    # discovery job comes first.
    assert Counter(f["purpose"] for f in evidence["fetches"]) == fetches
    assert all(f["access_state"] == "ok" and "url" not in f for f in evidence["fetches"])
    assert evidence["fetches"][0]["purpose"] == "robots"
    assert evidence["fetches"][0]["job_id"] == str(discovery_job)
    assert evidence["fetches"][0]["crawl_run_id"] == str(run_row["id"])
    [robots] = evidence["robots_revisions"]
    stored = env.rows(
        "select id, host, http_status, content_hash from ops.robots_revisions where workspace_id = %s",
        env.workspace_id,
    )
    assert [(robots["id"], robots["host"], robots["http_status"], robots["content_hash"])] == [
        (str(r["id"]), r["host"], r["http_status"], r["content_hash"]) for r in stored
    ]
    assert robots["availability"] == "available" and robots["fetched_at"] is not None
    assert "body" not in robots
    detail_jobs = {str(j["id"]) for j in jobs_of(env, JobType.DETAIL)}
    assert {j["id"] for j in evidence["detail_jobs"]} == detail_jobs
    listings = by_slid(env)
    assert {item["listing_id"] for item in evidence["listings"]} == {str(r["id"]) for r in listings.values()}
    countries = {
        str(r["id"]): r["seller_country"]
        for r in env.rows(
            "select id, seller_country from app.listing_revisions where workspace_id = %s", env.workspace_id
        )
    }
    printed_evidence: set[str] = set()
    for item in evidence["listings"]:
        revisions = item["revisions"]
        assert revisions and all(r["job_id"] in detail_jobs and r["evidence_ids"] for r in revisions)
        assert item["current_revision_id"] in {r["revision_id"] for r in revisions}
        # The seller country of each revision (runbook 9.1: the check of the cy search filter).
        assert all(r["seller_country"] == countries[r["revision_id"]] is not None for r in revisions)
        assert item["card_price_minor"] is not None and item["is_fixture"] is True
        printed_evidence.update(e for r in revisions for e in r["evidence_ids"])
    stored = env.rows("select id from app.field_evidence where workspace_id = %s", env.workspace_id)
    assert printed_evidence == {str(r["id"]) for r in stored}


async def test_italian_discovery_and_details_over_the_saved_fixtures(db_url: str, seed: Seed) -> None:
    env = await _env(db_url, seed, "autoscout24_it")
    try:
        discovery_job = await _enqueue_one_page_discovery(env)
        by_type = await _drain(env, "worker-as24-it")
        assert by_type[JobType.DISCOVERY] == 1 and by_type[JobType.DETAIL] == len(IT_SEARCH_CARDS)
        assert env.ctx.fixture_client is not None
        requests = [(r.url, r.purpose) for r in env.ctx.fixture_client.requests]
        # robots.txt once (the detail jobs reuse the stored revision), then the search page.
        assert requests[:2] == [
            ("https://www.autoscout24.it/robots.txt", "robots"),
            (model_search_urls("autoscout24_it")[0], "search"),
        ]
        assert sorted(requests[2:]) == sorted((detail_url(n), "detail") for n in IT_SEARCH_CARDS)
        assert all(job["state"] == "succeeded" for job in jobs_of(env, JobType.DETAIL))
        fetches = env.rows(
            "select purpose, host, access_state from ops.fetch_attempts where workspace_id = %s",
            env.workspace_id,
        )
        expected = {"robots": 1, "search": 1, "detail": len(IT_SEARCH_CARDS)}
        assert Counter(f["purpose"] for f in fetches) == expected
        assert {(f["host"], f["access_state"]) for f in fetches} == {("www.autoscout24.it", "ok")}
        # Every request, robots.txt included, counted against the host's daily budget.
        assert env.scalar(
            "select requests_today from ops.host_budgets where workspace_id = %s and host = %s",
            env.workspace_id,
            "www.autoscout24.it",
        ) == sum(expected.values())

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
        smoke = await asyncio.to_thread(_cli_smoke_evidence, db_url, env, "autoscout24_it")
        _check_smoke_evidence(env, smoke, discovery_job, expected)
    finally:
        await env.close()


async def test_german_card_only_discovery_never_requests_a_detail_page(db_url: str, seed: Seed) -> None:
    env = await _env(db_url, seed, "autoscout24_de")
    try:
        discovery_job = await _enqueue_one_page_discovery(env)
        by_type = await _drain(env, "worker-as24-de")
        assert by_type[JobType.DISCOVERY] == 1 and by_type[JobType.DETAIL] == 3  # card ingestion jobs
        assert env.ctx.fixture_client is not None
        assert [(r.url, r.purpose) for r in env.ctx.fixture_client.requests] == [
            ("https://www.autoscout24.de/robots.txt", "robots"),
            (model_search_urls("autoscout24_de")[0], "search"),
        ]
        listings = by_slid(env)
        assert set(listings) == {uuid_for(n) for n in (501, 502, 503)}
        # card_only: each listing has a card-sourced revision (no detail page was ever fetched).
        assert all(row["current_revision_id"] is not None for row in listings.values())
        assert all(count >= 1 for count in _revision_counts(env).values())
        assert env.scalar(
            "select bool_and(is_fixture) from app.listings where workspace_id = %s", env.workspace_id
        )
        # The only recorded fetches are robots.txt and the search page (the card ingestion jobs
        # request nothing).
        fetches = env.rows(
            "select purpose, host, access_state from ops.fetch_attempts where workspace_id = %s"
            " order by fetched_at, id",
            env.workspace_id,
        )
        assert fetches == [
            {"purpose": "robots", "host": "www.autoscout24.de", "access_state": "ok"},
            {"purpose": "search", "host": "www.autoscout24.de", "access_state": "ok"},
        ]
        smoke = await asyncio.to_thread(_cli_smoke_evidence, db_url, env, "autoscout24_de")
        _check_smoke_evidence(env, smoke, discovery_job, {"robots": 1, "search": 1})
    finally:
        await env.close()
