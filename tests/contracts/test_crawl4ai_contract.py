"""Crawl4AI 0.9.4 REST contract tests against recorded-shape fixtures.

Fixtures in tests/contracts/fixtures/crawl4ai/ are derived from
docs/research/crawl4ai_rest_contract.md (sections 2, 3, 6, 8, 13). Each is labelled
"shape from 0.9.4 source; live values unverified": they prove our mapping of the
documented shapes, not live server behaviour (spec section 31: fixture tests do not
prove live access). Re-verify with `inspect_contract()` at activation.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from suv_deals.adapters.crawl4ai_client import (
    Crawl4AIClient,
    CrawlerAuthFailed,
    CrawlerProtocolError,
    CrawlFetchResult,
)
from suv_deals.clock import FrozenClock
from suv_deals.domain.enums import AccessState

FIXTURES = Path(__file__).parent / "fixtures" / "crawl4ai"
BASE = "http://crawl4ai:11235"
URL = "https://www.dealer-example.com/fahrzeug/12345"
NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)

# Research section 6: CrawlResult.model_dump() keys, exactly.
RESULT_KEYS = {
    "cache_status", "cached_at", "cleaned_html", "console_messages", "crawl_stats", "dispatch_result",
    "downloaded_files", "error_message", "extracted_content", "fit_html", "head_fingerprint", "html",
    "js_execution_result", "links", "markdown", "media", "metadata", "mhtml", "network_requests", "pdf",
    "redirected_status_code", "redirected_url", "response_headers", "screenshot", "session_id",
    "ssl_certificate", "status_code", "success", "tables", "url",
}  # fmt: skip
TOP_LEVEL_KEYS = {
    "success",
    "results",
    "server_processing_time_s",
    "server_memory_delta_mb",
    "server_peak_memory_mb",
}


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def response(fixture: dict[str, Any]) -> httpx.Response:
    return httpx.Response(fixture["status"], json=fixture["body"], headers=fixture["headers"])


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def client() -> AsyncIterator[Crawl4AIClient]:
    async with httpx.AsyncClient() as http:
        yield Crawl4AIClient(
            BASE, SecretStr("token"), http=http, clock=FrozenClock(NOW), crawler_version_hint="0.9.4"
        )


async def crawl(client: Crawl4AIClient, router: respx.MockRouter, name: str) -> CrawlFetchResult:
    router.post("/crawl").mock(return_value=response(load(name)))
    return await client.fetch_detailed(URL, purpose="detail", source_key="dealer_example")


class TestFixtureIntegrity:
    def test_every_fixture_is_labelled(self) -> None:
        names = sorted(p.stem for p in FIXTURES.glob("*.json"))
        assert len(names) >= 15
        for name in names:
            fixture = load(name)
            assert "live values unverified" in fixture["label"] or "UNVERIFIED" in fixture["label"], name
            assert fixture["source"] == "docs/research/crawl4ai_rest_contract.md"
            assert set(fixture) <= {"label", "source", "request", "status", "headers", "body", "note"}

    @pytest.mark.parametrize(
        "name",
        [
            "crawl_success",
            "crawl_antibot",
            "crawl_robots_denied",
            "crawl_result_404",
            "crawl_navigation_error",
        ],
    )
    def test_result_shape_matches_research(self, name: str) -> None:
        body = load(name)["body"]
        assert set(body) == TOP_LEVEL_KEYS
        assert body["success"] is True  # top-level success only means "handled"
        for result in body["results"]:
            assert set(result) == RESULT_KEYS

    def test_success_markdown_is_an_object(self) -> None:
        markdown = load("crawl_success")["body"]["results"][0]["markdown"]
        assert set(markdown) == {
            "raw_markdown", "markdown_with_citations", "references_markdown", "fit_markdown", "fit_html",
        }  # fmt: skip


class TestCrawlMapping:
    async def test_success(self, client: Crawl4AIClient, router: respx.MockRouter) -> None:
        result = await crawl(client, router, "crawl_success")
        doc = result.document
        assert doc.fetch.access_state == AccessState.OK
        assert doc.fetch.http_status == 200
        assert doc.final_url == URL
        assert doc.html is not None and "Toyota RAV4" in doc.html
        assert doc.text is not None and doc.text.startswith("# Toyota RAV4")
        # set-cookie and server never leave the client
        assert doc.fetch.response_headers == {
            "content-type": "text/html; charset=utf-8",
            "etag": '"abc123"',
            "last-modified": "Mon, 05 Oct 2026 08:00:00 GMT",
        }
        assert result.server_processing_time_s == pytest.approx(2.418)
        assert doc.fetch.crawler_version == "0.9.4"

    async def test_antibot(self, client: Crawl4AIClient, router: respx.MockRouter) -> None:
        doc = (await crawl(client, router, "crawl_antibot")).document
        assert doc.fetch.access_state == AccessState.ACCESS_BLOCKED
        assert doc.fetch.error_code == "anti_bot_block"
        assert doc.fetch.success is False

    async def test_robots_denied(self, client: Crawl4AIClient, router: respx.MockRouter) -> None:
        doc = (await crawl(client, router, "crawl_robots_denied")).document
        assert doc.fetch.access_state == AccessState.POLICY_DENIED
        assert doc.fetch.error_code == "robots_disallowed"
        assert doc.fetch.http_status == 403
        assert doc.fetch.response_headers == {"x-robots-status": "Blocked by robots.txt"}

    async def test_result_404(self, client: Crawl4AIClient, router: respx.MockRouter) -> None:
        doc = (await crawl(client, router, "crawl_result_404")).document
        assert doc.fetch.access_state == AccessState.NOT_FOUND
        assert doc.html is not None  # kept so the adapter can tell an explicit removed page apart

    async def test_navigation_error(self, client: Crawl4AIClient, router: respx.MockRouter) -> None:
        doc = (await crawl(client, router, "crawl_navigation_error")).document
        assert doc.fetch.access_state == AccessState.TRANSIENT_ERROR
        assert doc.fetch.error_message == "navigation failed (net::ERR_NAME_NOT_RESOLVED)"
        assert URL not in (doc.fetch.error_message or "")

    async def test_server_401(self, client: Crawl4AIClient, router: respx.MockRouter) -> None:
        fixture = load("server_401")
        assert fixture["headers"]["www-authenticate"] == "Bearer"
        with pytest.raises(CrawlerAuthFailed):
            await crawl(client, router, "server_401")

    @pytest.mark.parametrize(
        ("name", "state", "code"),
        [
            ("server_400_rejected_config", AccessState.POLICY_DENIED, "crawler_rejected_config"),
            ("server_400_url_blocked", AccessState.POLICY_DENIED, "crawler_url_blocked"),
            ("server_429", AccessState.RATE_LIMITED, "crawler_rate_limited"),
            ("server_500", AccessState.TRANSIENT_ERROR, "crawler_server_error"),
            ("server_504", AccessState.TRANSIENT_ERROR, "crawl_time_limit"),
        ],
    )
    async def test_server_errors(
        self, client: Crawl4AIClient, router: respx.MockRouter, name: str, state: AccessState, code: str
    ) -> None:
        doc = (await crawl(client, router, name)).document
        assert (doc.fetch.access_state, doc.fetch.error_code) == (state, code)
        if name == "server_429":
            assert doc.fetch.retry_after_seconds == 60
        if name == "server_500":
            assert "0a1b2c3d4e5f" not in (doc.fetch.error_message or "")

    @pytest.mark.parametrize("name", ["server_413", "server_422"])
    async def test_client_bug_statuses(
        self, client: Crawl4AIClient, router: respx.MockRouter, name: str
    ) -> None:
        with pytest.raises(CrawlerProtocolError):
            await crawl(client, router, name)


class TestHealthAndContract:
    async def test_health_ok(self, client: Crawl4AIClient, router: respx.MockRouter) -> None:
        router.get("/health").mock(return_value=response(load("health_ok")))
        health = await client.health()
        assert health.ok and health.version == "0.9.4"

    async def test_health_legacy(self, client: Crawl4AIClient, router: respx.MockRouter) -> None:
        router.get("/health").mock(return_value=response(load("health_legacy_0_9_2")))
        health = await client.health()
        assert health.status == "healthy" and not health.ok

    async def test_inspect_contract_with_recorded_shapes(
        self, client: Crawl4AIClient, router: respx.MockRouter
    ) -> None:
        router.get("/health").mock(return_value=response(load("health_ok")))
        router.post("/crawl").mock(return_value=response(load("server_401")))

        def dump(request: httpx.Request) -> httpx.Response:
            kind = json.loads(request.content)["type"]
            name = "config_dump_crawler" if kind == "CrawlerRunConfig" else "config_dump_browser"
            return response(load(name))

        router.post("/config/dump").mock(side_effect=dump)
        report = await client.inspect_contract()
        assert report.ok, report.problems
        assert report.auth_enforced is True
        assert report.browser_config is not None
        assert report.browser_config.server_computed_fields == ("headers",)
