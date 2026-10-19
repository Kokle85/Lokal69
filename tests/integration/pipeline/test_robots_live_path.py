"""robots.txt in the worker path, through the real discovery and detail handlers (spec 5, 8, 24, 31).

Every run here uses the SYNTHETIC ``fixture_dealer_de`` source and its saved fixture set (the
reserved host ``dealer.example``, served by the offline `FixtureCrawlClient`; nothing reaches a
network). Its saved ``robots.txt`` allows the reviewed paths and disallows the search variant
``typ=robots-gesperrt`` and the listing ``TEST-230``; variants (a changed, unreachable, denied or
slow robots.txt) are copies of the set in ``tmp_path``.

What the worker must show: one robots revision per host fetched before the first page and reused
by the later jobs of the day; the robots request logged (``purpose: robots``) and counted in the
host's daily budget; a disallowed URL never requested and reported; an unreachable robots.txt
meaning no request at all; a changed robots.txt audited (``source.robots_changed``) with nothing
switched on or off; the robots crawl-delay in the host's spacing; and the operator's read-only
views (``sources inspect --from-db``, ``sources smoke-evidence``) naming the revision.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from tests.integration.db.helpers import Seed
from tests.integration.pipeline.support import (
    DEALER_DE,
    SOURCE_KEY,
    PipelineEnv,
    build_env,
    by_slid,
    fixture_source_config,
    jobs_of,
    pipeline_settings,
    run,
)

from suv_deals.adapters.fixture_client import FixtureCrawlClient
from suv_deals.cli import cli
from suv_deals.clock import SystemClock
from suv_deals.crawling.rate_limits import next_utc_midnight
from suv_deals.crawling.robots import RobotsFetchResponse
from suv_deals.crawling.robots_guard import CrawlClientRobotsTransport
from suv_deals.crawling.scheduler import run_scheduler_tick
from suv_deals.domain.enums import JobState, JobType, SourceMode
from suv_deals.persistence import listings_repo
from suv_deals.persistence.database import Conn
from suv_deals.workers.runner import JobRunReport, Worker
from suv_deals.workers.runtime import offline_fixture_resolver

pytestmark = pytest.mark.db

HOST = "dealer.example"
ROBOTS_URL = f"https://{HOST}/robots.txt"
SEARCH_URL = f"https://{HOST}/suche?typ=suv"
DISALLOWED_SEARCH = f"https://{HOST}/suche?typ=robots-gesperrt"
DISALLOWED_DETAIL = f"https://{HOST}/fahrzeug/TEST-230"


async def _discover(env: PipelineEnv, name: str = "worker-robots") -> JobRunReport:
    await run_scheduler_tick(env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id])
    [report] = await Worker(
        env.ctx, workspace_ids=[env.workspace_id], worker_id=name, job_types=[JobType.DISCOVERY]
    ).run_until_idle()
    return report


async def _rediscover(env: PipelineEnv, key: str) -> JobRunReport:
    """One more discovery job of the source's primary profile (a later run of the same day)."""
    from suv_deals.persistence import jobs  # noqa: PLC0415

    async def enqueue(conn: Conn) -> None:
        await jobs.enqueue(
            conn,
            env.system,
            jobs.JobSpec(
                job_type=JobType.DISCOVERY,
                dedup_key=f"discovery:{key}",
                source_id=env.source_id,
                profile_id=env.profiles["primary"],
                partition_key="default",
            ),
        )

    await run(env.ctx, env.system, enqueue)
    [report] = await Worker(
        env.ctx, workspace_ids=[env.workspace_id], worker_id=f"worker-{key}", job_types=[JobType.DISCOVERY]
    ).run_until_idle()
    return report


def _requests(env: PipelineEnv) -> list[tuple[str, str]]:
    assert env.ctx.fixture_client is not None
    return [(r.url, r.purpose) for r in env.ctx.fixture_client.requests]


def _revisions(env: PipelineEnv) -> list[dict[str, Any]]:
    return env.rows(
        "select id, host, fetched_at, http_status, content_hash, body, parse_ok from ops.robots_revisions"
        " where workspace_id = %s order by fetched_at, id",
        env.workspace_id,
    )


def _fetches(env: PipelineEnv) -> list[dict[str, Any]]:
    return env.rows(
        "select purpose, host, http_status, access_state, error_code, job_id, crawl_run_id"
        " from ops.fetch_attempts where workspace_id = %s order by fetched_at, id",
        env.workspace_id,
    )


def _requests_today(env: PipelineEnv) -> int:
    return int(
        env.scalar(
            "select requests_today from ops.host_budgets where workspace_id = %s and host = %s",
            env.workspace_id,
            HOST,
        )
    )


def _robots_audits(env: PipelineEnv) -> list[dict[str, Any]]:
    return env.rows(
        "select target_id, reason, metadata from ops.audit_events where workspace_id = %s"
        " and action = 'source.robots_changed' order by occurred_at, id",
        env.workspace_id,
    )


def _copy_set(tmp_path: Path, *, robots: str | None = None, route: dict[str, Any] | None = None) -> Path:
    """A copy of the SYNTHETIC dealer set with another robots.txt body and/or robots route."""
    directory = tmp_path / SOURCE_KEY
    shutil.copytree(DEALER_DE, directory)
    if robots is not None:
        (directory / "robots.txt").write_text(robots, encoding="utf-8")
    if route is not None:
        manifest = yaml.safe_load((directory / "MANIFEST.yaml").read_text(encoding="utf-8"))
        manifest["routes"][ROBOTS_URL] = route
        (directory / "MANIFEST.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return directory


def _cli_json(db_url: str, env: PipelineEnv, *args: str) -> dict[str, Any]:
    """A read-only ``suv-deals sources ...`` command exactly as the owner runs it."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        result = CliRunner().invoke(
            cli,
            ["--no-env-file", "sources", *args, "--workspace", str(env.workspace_id)],
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
    data: dict[str, Any] = json.loads(result.stdout)
    return data


# --------------------------------------------------------------------------------------------


async def test_one_robots_revision_per_host_fetched_first_logged_and_counted(
    db_url: str, seed: Seed, env: PipelineEnv
) -> None:
    report = await _discover(env)
    assert report.state == JobState.SUCCEEDED and report.details["completeness"] == "complete"
    details = await Worker(
        env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-details", job_types=[JobType.DETAIL]
    ).run_until_idle()
    assert details and all(r.state == JobState.SUCCEEDED for r in details)
    requests = _requests(env)
    # robots.txt before the first page, once for the discovery AND every detail job of the day.
    assert requests[0] == (ROBOTS_URL, "robots")
    assert [r for r in requests if r[1] == "robots"] == [(ROBOTS_URL, "robots")]
    assert [p for _u, p in requests[1:3]] == ["search", "search"]

    [revision] = _revisions(env)
    saved = (DEALER_DE / "robots.txt").read_text(encoding="utf-8")
    assert revision["host"] == HOST and revision["http_status"] == 200 and revision["parse_ok"]
    assert revision["body"] == saved
    assert revision["content_hash"] == hashlib.sha256(saved.encode("utf-8")).hexdigest()

    fetches = _fetches(env)
    [robots] = [f for f in fetches if f["purpose"] == "robots"]
    [run_row] = env.rows("select id, job_id from ops.crawl_runs where workspace_id = %s", env.workspace_id)
    assert (robots["host"], robots["http_status"], robots["access_state"]) == (HOST, 200, "ok")
    assert (robots["job_id"], robots["crawl_run_id"]) == (run_row["job_id"], run_row["id"])
    assert fetches[0] == robots
    # Every request, robots.txt included, is in the host's daily budget.
    assert _requests_today(env) == len(fetches) == len(requests)
    assert _robots_audits(env) == []  # the first revision of a host is no change

    view = await asyncio.to_thread(_cli_json, db_url, env, "inspect", SOURCE_KEY, "--from-db")
    assert view["robots"]["fetch_status"] == 200
    assert view["robots"]["revision_hash"] == revision["content_hash"]
    assert view["robots"]["last_checked_at"] is not None and view["robots"]["policy"] == "obey"
    evidence = await asyncio.to_thread(_cli_json, db_url, env, "smoke-evidence", SOURCE_KEY)
    [shown] = evidence["robots_revisions"]
    assert (
        shown["id"],
        shown["host"],
        shown["http_status"],
        shown["content_hash"],
        shown["availability"],
    ) == (
        str(revision["id"]),
        HOST,
        200,
        revision["content_hash"],
        "available",
    )
    assert evidence["fetches"][0]["purpose"] == "robots"


async def test_a_robots_disallowed_search_url_is_never_requested_and_is_reported(
    db_url: str, seed: Seed
) -> None:
    env = await build_env(db_url, seed, source=fixture_source_config(search_url=DISALLOWED_SEARCH))
    try:
        report = await _discover(env)
        # Reported, not retried: the job completes with a failed traversal.
        assert report.state == JobState.SUCCEEDED and report.details["completeness"] == "failed"
        assert _requests(env) == [(ROBOTS_URL, "robots")]
        fetches = _fetches(env)
        assert [(f["purpose"], f["access_state"], f["error_code"]) for f in fetches] == [
            ("robots", "ok", None),
            ("search", "policy_denied", "robots_disallowed"),
        ]
        assert fetches[1]["http_status"] is None  # never requested
        [run_row] = env.rows(
            "select outcome, access_state, gap_reasons from ops.crawl_runs where workspace_id = %s",
            env.workspace_id,
        )
        assert run_row["outcome"] == "failed" and run_row["access_state"] == "policy_denied"
        assert any("robots.txt" in gap and "robots_disallowed" in gap for gap in run_row["gap_reasons"])
        assert _requests_today(env) == 1  # robots.txt only
        assert jobs_of(env, JobType.DETAIL) == []
        status = env.scalar("select technical_status from app.sources where id = %s", env.source_id)
        assert status not in ("access_blocked", "parser_unhealthy")
    finally:
        await env.close()


async def test_a_robots_disallowed_detail_url_is_never_requested(env: PipelineEnv) -> None:
    assert (await _discover(env)).state == JobState.SUCCEEDED
    await Worker(
        env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-details", job_types=[JobType.DETAIL]
    ).run_until_idle()
    listing_id = by_slid(env)["TEST-204"]["id"]
    env.seed.conn.execute(
        "update app.listings set canonical_url = %s where id = %s", (DISALLOWED_DETAIL, listing_id)
    )

    async def recheck(conn: Conn) -> None:
        assert await listings_repo.request_detail_refresh(conn, env.system, listing_id, reason="robots test")

    await run(env.ctx, env.system, recheck)
    before = len(_requests(env))
    [report] = await Worker(
        env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-recheck", job_types=[JobType.RECHECK]
    ).run_until_idle()
    # A visible dead letter (retrying cannot help), and no request at all: the day's robots
    # revision is reused, the disallowed page is refused before the budget gate.
    assert report.state == JobState.DEAD_LETTER and report.code == "policy_denied"
    assert _requests(env)[before:] == []
    assert DISALLOWED_DETAIL not in {url for url, _p in _requests(env)}
    [refused] = [f for f in _fetches(env) if f["error_code"] == "robots_disallowed"]
    assert (refused["purpose"], refused["access_state"], refused["job_id"]) == (
        "detail",
        "policy_denied",
        report.job_id,
    )


async def test_a_changed_robots_txt_is_audited_and_applied_at_once(
    db_url: str, seed: Seed, tmp_path: Path
) -> None:
    directory = _copy_set(tmp_path)
    env = await build_env(db_url, seed, fixture_dirs=[directory])
    try:
        assert (await _discover(env)).details["completeness"] == "complete"
        [first] = _revisions(env)
        # The site changes its robots.txt: the whole search path is disallowed now.
        (directory / "robots.txt").write_text(
            "# SYNTHETIC FIXTURE: changed robots.txt\nUser-agent: *\nDisallow: /suche\n", encoding="utf-8"
        )
        # Within the same UTC day the stored revision is still current: nothing is fetched again.
        again = await _rediscover(env, "same-day")
        assert again.details["completeness"] == "complete"
        assert len(_revisions(env)) == 1
        # The next day (the stored revision aged by a day) robots.txt is fetched first again.
        env.seed.conn.execute(
            "update ops.robots_revisions set fetched_at = fetched_at - interval '1 day'"
            " where workspace_id = %s",
            (env.workspace_id,),
        )
        before = _source(env)
        report = await _rediscover(env, "next-day")
        assert report.state == JobState.SUCCEEDED and report.details["completeness"] == "failed"
        first_row, second = _revisions(env)
        assert first_row["id"] == first["id"] and second["content_hash"] != first["content_hash"]
        [audit] = _robots_audits(env)
        assert audit["target_id"] == env.source_id
        assert audit["metadata"]["host"] == HOST
        assert audit["metadata"]["previous_hash"] == first["content_hash"]
        assert audit["metadata"]["content_hash"] == second["content_hash"]
        assert "review" in audit["reason"]
        # Nothing is switched on or off automatically; the new rules apply to the very next page.
        assert _source(env) == before
        assert _fetches(env)[-1]["error_code"] == "robots_disallowed"
    finally:
        await env.close()


def _source(env: PipelineEnv) -> dict[str, Any]:
    [row] = env.rows("select enabled, paused, technical_status from app.sources where id = %s", env.source_id)
    return row


async def test_an_unreachable_robots_txt_means_no_request_and_a_later_retry(
    db_url: str, seed: Seed, tmp_path: Path
) -> None:
    directory = _copy_set(tmp_path, route={"status": 503, "description": "robots.txt unavailable (503)"})
    env = await build_env(db_url, seed, fixture_dirs=[directory])
    try:
        report = await _discover(env)
        # Complete disallow: no page request; the job waits (no attempt consumed) until robots.txt
        # may be asked again.
        assert report.state == JobState.QUEUED and report.code == "ROBOTS_UNREACHABLE"
        assert _requests(env) == [(ROBOTS_URL, "robots")]
        [job] = jobs_of(env, JobType.DISCOVERY)
        assert job["state"] == "queued" and job["attempts"] == 0
        [revision] = _revisions(env)
        assert revision["http_status"] == 503 and revision["body"] is None and not revision["parse_ok"]
        assert revision["content_hash"] is None
        fetched = revision["fetched_at"]
        expected = min(fetched + timedelta(minutes=30), next_utc_midnight(fetched))
        available = env.scalar("select available_at from ops.jobs where id = %s", job["id"])
        assert abs(available - expected) < timedelta(seconds=5)
        [robots] = _fetches(env)
        assert (robots["purpose"], robots["access_state"], robots["error_code"]) == (
            "robots",
            "transient_error",
            "robots_http_503",
        )
        [run_row] = env.rows(
            "select outcome, gap_reasons from ops.crawl_runs where workspace_id = %s", env.workspace_id
        )
        assert run_row["outcome"] == "cancelled"
        assert any("robots.txt unreachable" in gap for gap in run_row["gap_reasons"])
        assert _robots_audits(env) == []
        assert _requests_today(env) == 1
    finally:
        await env.close()


async def test_an_access_denial_on_robots_txt_stops_and_reports(
    db_url: str, seed: Seed, tmp_path: Path
) -> None:
    directory = _copy_set(
        tmp_path, route={"file": "captcha.html", "status": 403, "description": "robots.txt 403"}
    )
    env = await build_env(db_url, seed, fixture_dirs=[directory])
    try:
        report = await _discover(env)
        assert report.state == JobState.BLOCKED and report.code == "access_blocked"
        assert _requests(env) == [(ROBOTS_URL, "robots")]
        assert _source(env)["technical_status"] == "access_blocked"
        fetches = _fetches(env)
        assert [(f["purpose"], f["access_state"], f["error_code"]) for f in fetches] == [
            ("robots", "access_blocked", "robots_http_403"),
            ("search", "access_blocked", "robots_http_403"),
        ]
        blocked = env.scalar(
            "select access_blocked_at is not null from ops.host_budgets"
            " where workspace_id = %s and host = %s",
            env.workspace_id,
            HOST,
        )
        assert blocked
        # The evidence names the answer an access denial, not "no robots.txt" (no restriction).
        evidence = await asyncio.to_thread(_cli_json, db_url, env, "smoke-evidence", SOURCE_KEY)
        [shown] = evidence["robots_revisions"]
        assert (shown["http_status"], shown["content_hash"], shown["availability"]) == (
            403,
            None,
            "access_denied",
        )
        assert _robots_audits(env) == []  # an access denial says nothing about the directives
    finally:
        await env.close()


async def test_a_non_standard_status_on_robots_txt_is_unreachable_and_recorded(
    env: PipelineEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vendor status (999) on robots.txt is no HTTP status code: the revision is stored without
    one (unreachable: complete disallow), the counted robots request is logged, and the job waits
    for the 30-minute retry instead of dying on a validation error."""

    async def vendor_status(
        self: CrawlClientRobotsTransport, url: str, *, source_key: str
    ) -> RobotsFetchResponse:
        del self, url, source_key
        return RobotsFetchResponse(999, b"Request denied", None)

    monkeypatch.setattr(CrawlClientRobotsTransport, "get", vendor_status)
    report = await _discover(env)
    assert report.state == JobState.QUEUED and report.code == "ROBOTS_UNREACHABLE"
    assert _requests(env) == []  # no page request (and robots.txt came from the patched transport)
    [revision] = _revisions(env)
    assert revision["http_status"] is None and revision["body"] is None and not revision["parse_ok"]
    [robots] = _fetches(env)
    assert (robots["purpose"], robots["http_status"], robots["error_code"]) == (
        "robots",
        None,
        "robots_http_999",
    )
    assert _requests_today(env) == 1  # the counted request is in the evidence
    [job] = jobs_of(env, JobType.DISCOVERY)
    assert job["state"] == "queued" and job["attempts"] == 0
    [run_row] = env.rows("select outcome from ops.crawl_runs where workspace_id = %s", env.workspace_id)
    assert run_row["outcome"] == "cancelled"


async def test_rules_for_our_product_token_apply_in_the_worker(
    db_url: str, seed: Seed, tmp_path: Path
) -> None:
    """robots.txt allows everything for `*` but disallows the search for SUVDealResearch: the
    worker evaluates our group, so the search page is never requested."""
    directory = _copy_set(
        tmp_path,
        robots=(
            "# SYNTHETIC FIXTURE: rules for our product token only\n"
            "User-agent: *\nAllow: /\n\n"
            "User-agent: SUVDealResearch\nDisallow: /suche?typ=suv\nCrawl-delay: 45\n"
        ),
    )
    env = await build_env(db_url, seed, fixture_dirs=[directory])
    try:
        report = await _discover(env)
        assert report.state == JobState.SUCCEEDED and report.details["completeness"] == "failed"
        assert _requests(env) == [(ROBOTS_URL, "robots")]
        assert [(f["purpose"], f["access_state"], f["error_code"]) for f in _fetches(env)] == [
            ("robots", "ok", None),
            ("search", "policy_denied", "robots_disallowed"),
        ]
        assert _requests_today(env) == 1
    finally:
        await env.close()


async def test_the_robots_crawl_delay_spaces_the_host(db_url: str, seed: Seed, tmp_path: Path) -> None:
    directory = _copy_set(
        tmp_path,
        robots="# SYNTHETIC FIXTURE: slow host\nUser-agent: *\nCrawl-delay: 45\nDisallow: /intern/\n",
    )
    env = await build_env(db_url, seed, fixture_dirs=[directory])
    try:
        assert (await _discover(env)).details["completeness"] == "complete"
        refill = env.scalar(
            "select refill_per_second from ops.host_budgets where workspace_id = %s and host = %s",
            env.workspace_id,
            HOST,
        )
        # One navigation per 45 s (robots Crawl-delay), not per the source's 20 s.
        assert refill == (Decimal(1) / Decimal(45)).quantize(Decimal("0.00000001"))
    finally:
        await env.close()


async def test_a_real_source_never_fetches_without_a_robots_transport(db_url: str, seed: Seed) -> None:
    settings = pipeline_settings(db_url, source_network_enabled=True)
    real = fixture_source_config().model_copy(update={"mode": SourceMode.PUBLIC_HTML})
    env = await build_env(db_url, seed, settings=settings, source=real)
    try:
        stub = FixtureCrawlClient([DEALER_DE])
        env.ctx.network_client = stub
        env.ctx.resolver = offline_fixture_resolver
        assert env.ctx.robots_transport is not None  # built with the crawler client
        await env.ctx.robots_transport.aclose()  # type: ignore[attr-defined]
        env.ctx.robots_transport = None
        report = await _discover(env)
        assert report.state == JobState.RETRY_WAIT and report.code == "DEPENDENCY_UNAVAILABLE"
        assert stub.requests == []
    finally:
        await env.close()
