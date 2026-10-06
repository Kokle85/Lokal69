"""Unit tests for the Crawl4AI 0.9.4 REST client (spec sections 8, 24, 27; research contract)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import anyio
import httpx
import pytest
import respx
from pydantic import SecretStr, ValidationError

from suv_deals.adapters.base import CrawlClient
from suv_deals.adapters.crawl4ai_client import (
    DEFAULT_USER_AGENT,
    MAX_OVERALL_DEADLINE_S,
    Crawl4AIClient,
    CrawlerAuthFailed,
    CrawlerProtocolError,
    SourceCrawlOptions,
    build_crawl_payload,
    expected_version_from_image,
    find_forbidden_fields,
)
from suv_deals.clock import FrozenClock
from suv_deals.crawling.rate_limits import DEFAULT_BACKOFF
from suv_deals.domain.enums import AccessState
from suv_deals.errors import ErrorCode
from suv_deals.settings import Settings

NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)
BASE = "http://crawl4ai:11235"
TOKEN = "s3cr3t-crawler-token"
URL = "https://www.dealer-example.com/fahrzeug/12345"
HTML = (
    "<!DOCTYPE html><html><head><title>Toyota RAV4</title></head><body>"
    + "<p>listing</p>" * 30
    + "</body></html>"
)

# Research section 5, written out independently of the module under test.
RESEARCH_FORBIDDEN = [
    # CrawlerRunConfig
    "js_code", "js_code_before_wait", "c4a_script", "deep_crawl_strategy", "proxy_config",
    "proxy_rotation_strategy", "proxy_session_id", "fallback_fetch_function", "experimental", "base_url",
    "simulate_user", "override_navigator", "magic", "process_in_browser", "shared_data", "session_id",
    # BrowserConfig
    "proxy", "extra_args", "user_data_dir", "channel", "chrome_channel", "cdp_url", "debugging_port", "host",
    "storage_state", "cookies", "headers", "init_scripts", "browser_context_id", "target_id",
    # every type
    "image_save_dir", "save_images_locally", "downloads_path", "output_path", "save_path", "file_path",
    "local_path", "code", "command", "hook", "hooks",
    # never on /crawl
    "stream", "user_agent_mode",
]  # fmt: skip


def all_keys(node: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            keys |= all_keys(value)
    elif isinstance(node, list):
        for value in node:
            keys |= all_keys(value)
    return keys


def result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "cache_status": "miss",
        "cached_at": None,
        "cleaned_html": "<div>listing</div>",
        "console_messages": None,
        "crawl_stats": {
            "attempts": 1,
            "retries": 0,
            "proxies_used": [],
            "fallback_fetch_used": False,
            "resolved_by": "direct",
        },
        "dispatch_result": None,
        "downloaded_files": None,
        "error_message": "",
        "extracted_content": None,
        "fit_html": None,
        "head_fingerprint": None,
        "html": HTML,
        "js_execution_result": None,
        "links": {"internal": [], "external": []},
        "markdown": {
            "raw_markdown": "# Toyota RAV4\n",
            "markdown_with_citations": "",
            "references_markdown": "",
            "fit_markdown": None,
            "fit_html": None,
        },
        "media": {"images": [], "videos": [], "audios": []},
        "metadata": {"title": "Toyota RAV4"},
        "mhtml": None,
        "network_requests": None,
        "pdf": None,
        "redirected_status_code": 200,
        "redirected_url": URL,
        "response_headers": {
            "content-type": "text/html; charset=utf-8",
            "Set-Cookie": "sid=secret",
            "X-Robots-Tag": "noarchive",
            "server": "nginx",
        },
        "screenshot": None,
        "session_id": None,
        "ssl_certificate": None,
        "status_code": 200,
        "success": True,
        "tables": [],
        "url": URL,
    }
    base.update(overrides)
    return base


def envelope(*results: dict[str, Any], success: bool = True) -> dict[str, Any]:
    return {
        "success": success,
        "results": list(results),
        "server_processing_time_s": 1.25,
        "server_memory_delta_mb": 3.0,
        "server_peak_memory_mb": 300.0,
    }


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def router() -> respx.MockRouter:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


def make(http: httpx.AsyncClient, token: str | None = TOKEN, **kwargs: Any) -> Crawl4AIClient:
    return Crawl4AIClient(
        BASE, SecretStr(token) if token is not None else None, http=http, clock=FrozenClock(NOW), **kwargs
    )


async def fetch(client: Crawl4AIClient, url: str = URL, source_key: str = "dealer_example") -> Any:
    return await client.fetch_detailed(url, purpose="detail", source_key=source_key)


class TestPayload:
    async def test_exact_payload_and_auth(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        route = router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(result())))
        await fetch(make(http))
        request = route.calls.last.request
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert json.loads(request.content) == {
            "urls": [URL],
            "browser_config": {
                "type": "BrowserConfig",
                "params": {"headless": True, "enable_stealth": False, "user_agent": DEFAULT_USER_AGENT},
            },
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {
                    "cache_mode": {"type": "CacheMode", "params": "bypass"},
                    "page_timeout": 30000,
                    "wait_until": "domcontentloaded",
                    "check_robots_txt": True,
                },
            },
        }

    async def test_payload_never_contains_forbidden_fields(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        route = router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(result())))
        options = {
            "dealer_example": SourceCrawlOptions(
                locale="de-DE", timezone_id="Europe/Berlin", wait_for="css:.listing-card"
            )
        }
        await fetch(make(http, source_options=options, page_timeout_ms=60000))
        sent = json.loads(route.calls.last.request.content)
        keys = all_keys(sent)
        for forbidden in RESEARCH_FORBIDDEN:
            assert forbidden not in keys, forbidden
        assert not any(k.startswith("proxy_session_") for k in keys)
        params = sent["crawler_config"]["params"]
        assert params["locale"] == "de-DE"
        assert params["timezone_id"] == "Europe/Berlin"
        assert params["wait_for"] == "css:.listing-card"
        assert params["page_timeout"] == 60000
        assert "user_agent" not in params  # CrawlerRunConfig UA would mutate the pooled browser

    def test_forbidden_scanner_finds_nested_keys(self) -> None:
        payload = build_crawl_payload(URL, user_agent=DEFAULT_USER_AGENT, page_timeout_ms=30000)
        assert find_forbidden_fields(payload) == ()
        payload["crawler_config"]["params"]["magic"] = False
        payload["browser_config"]["params"]["headers"] = {}
        payload["crawler_config"]["params"]["proxy_session_ttl"] = 1
        assert find_forbidden_fields(payload) == ("headers", "magic", "proxy_session_ttl")

    async def test_no_auth_header_without_token(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        route = router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(result())))
        await fetch(make(http, token=None))
        assert "authorization" not in route.calls.last.request.headers

    def test_empty_token_treated_as_missing(self, http: httpx.AsyncClient) -> None:
        assert "token=missing" in repr(make(http, token=""))

    def test_token_never_in_repr(self, http: httpx.AsyncClient) -> None:
        client = make(http)
        assert TOKEN not in repr(client)
        assert "token=set" in repr(client)


class TestServerErrors:
    async def test_401_is_our_credential_not_a_source_block(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        router.post("/crawl").mock(
            return_value=httpx.Response(401, json={"detail": "Authentication required"})
        )
        with pytest.raises(CrawlerAuthFailed) as info:
            await fetch(make(http))
        error = info.value
        assert error.code == ErrorCode.DEPENDENCY_UNAVAILABLE
        assert error.retryable is False
        assert error.details == {"kind": "crawler_auth_failed"}
        assert TOKEN not in str(error) and TOKEN not in repr(error)

    async def test_401_without_token_hint(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.post("/crawl").mock(
            return_value=httpx.Response(401, json={"detail": "Authentication required"})
        )
        with pytest.raises(CrawlerAuthFailed, match="no CRAWL4AI_API_TOKEN"):
            await fetch(make(http, token=None))

    @pytest.mark.parametrize(
        ("detail", "code"),
        [
            (
                "Rejected config: field 'magic' is not permitted on CrawlerRunConfig",
                "crawler_rejected_config",
            ),
            ("Rejected request: BrowserConfig field 'headers' not permitted", "crawler_rejected_config"),
            ("URL blocked (SSRF protection): URL blocked", "crawler_url_blocked"),
        ],
    )
    async def test_400_policy(
        self, http: httpx.AsyncClient, router: respx.MockRouter, detail: str, code: str
    ) -> None:
        router.post("/crawl").mock(return_value=httpx.Response(400, json={"detail": detail}))
        doc = (await fetch(make(http))).document
        assert doc.fetch.access_state == AccessState.POLICY_DENIED
        assert doc.fetch.error_code == code
        assert "magic" not in (doc.fetch.error_message or "")
        assert doc.html is None

    async def test_other_400_is_client_bug(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.post("/crawl").mock(return_value=httpx.Response(400, json={"detail": "something else"}))
        with pytest.raises(CrawlerProtocolError) as info:
            await fetch(make(http))
        assert info.value.kind == "crawler_bad_request"
        assert info.value.code == ErrorCode.INTERNAL_ERROR
        assert info.value.retryable is False

    @pytest.mark.parametrize(
        ("status", "kind"),
        [
            (413, "crawler_request_too_large"),
            (422, "crawler_request_invalid"),
            (403, "crawler_client_error"),
            (404, "crawler_client_error"),
            (405, "crawler_client_error"),
        ],
    )
    async def test_client_bug_statuses(
        self, http: httpx.AsyncClient, router: respx.MockRouter, status: int, kind: str
    ) -> None:
        router.post("/crawl").mock(return_value=httpx.Response(status, json={"detail": "x"}))
        with pytest.raises(CrawlerProtocolError) as info:
            await fetch(make(http))
        assert (info.value.http_status, info.value.kind) == (status, kind)

    async def test_429_with_retry_after(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.post("/crawl").mock(
            return_value=httpx.Response(
                429, json={"detail": "1000 per 1 minute"}, headers={"Retry-After": "45"}
            )
        )
        outcome = (await fetch(make(http))).document.fetch
        assert outcome.access_state == AccessState.RATE_LIMITED
        assert outcome.error_code == "crawler_rate_limited"
        assert outcome.retry_after_seconds == 45

    async def test_429_with_http_date(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.post("/crawl").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "Tue, 06 Oct 2026 10:05:00 GMT"})
        )
        assert (await fetch(make(http))).document.fetch.retry_after_seconds == 300

    @pytest.mark.parametrize(
        ("status", "code"),
        [
            (500, "crawler_server_error"),
            (502, "crawler_server_error"),
            (503, "crawler_server_error"),
            (504, "crawl_time_limit"),
        ],
    )
    async def test_5xx_transient(
        self, http: httpx.AsyncClient, router: respx.MockRouter, status: int, code: str
    ) -> None:
        router.post("/crawl").mock(return_value=httpx.Response(status, text="<html>bad gateway</html>"))
        outcome = (await fetch(make(http))).document.fetch
        assert outcome.access_state == AccessState.TRANSIENT_ERROR
        assert outcome.error_code == code
        assert outcome.success is False

    async def test_unexpected_status(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.post("/crawl").mock(return_value=httpx.Response(302, headers={"Location": "/elsewhere"}))
        assert (await fetch(make(http))).document.fetch.error_code == "crawler_bad_response"

    async def test_crawler_redirect_never_followed(self, router: respx.MockRouter) -> None:
        # Even an injected client configured to follow redirects must not re-POST the crawl
        # request (target URL included) to wherever the crawler endpoint points.
        router.post("/crawl").mock(
            return_value=httpx.Response(307, headers={"Location": "http://elsewhere.example.net:9999/crawl"})
        )
        elsewhere = router.post("http://elsewhere.example.net:9999/crawl").mock(
            return_value=httpx.Response(200, json=envelope(result()))
        )
        async with httpx.AsyncClient(follow_redirects=True) as following:
            outcome = (await fetch(make(following))).document.fetch
        assert not elsewhere.called
        assert outcome.error_code == "crawler_bad_response"

    async def test_non_json_200(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.post("/crawl").mock(return_value=httpx.Response(200, text="not json"))
        outcome = (await fetch(make(http))).document.fetch
        assert (outcome.access_state, outcome.error_code) == (
            AccessState.TRANSIENT_ERROR,
            "crawler_bad_response",
        )

    async def test_top_level_failure(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(result(), success=False)))
        assert (await fetch(make(http))).document.fetch.error_code == "crawler_reported_failure"


class TestTransport:
    @pytest.mark.parametrize(
        ("exc", "code"),
        [
            (httpx.ConnectError("refused"), "crawler_unreachable"),
            (httpx.ConnectTimeout("slow connect"), "crawler_unreachable"),
            (httpx.ReadTimeout("slow"), "fetch_timeout"),
            (httpx.RemoteProtocolError("eof"), "crawler_unreachable"),
        ],
    )
    async def test_transport_errors(
        self, http: httpx.AsyncClient, router: respx.MockRouter, exc: Exception, code: str
    ) -> None:
        router.post("/crawl").mock(side_effect=exc)
        doc = (await fetch(make(http))).document
        assert doc.fetch.access_state == AccessState.TRANSIENT_ERROR
        assert doc.fetch.error_code == code
        assert doc.fetch.elapsed_ms is not None
        assert doc.html is None

    async def test_overall_deadline(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        async def slow(request: httpx.Request) -> httpx.Response:
            await anyio.sleep(5)
            return httpx.Response(200, json=envelope(result()))

        router.post("/crawl").mock(side_effect=slow)
        with anyio.fail_after(3):
            outcome = (await fetch(make(http, overall_deadline_s=0.05))).document.fetch
        assert outcome.error_code == "fetch_timeout"
        assert outcome.error_message == "overall crawl deadline exceeded"

    async def test_cancellation_propagates(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        observed: list[str] = []

        async def slow(request: httpx.Request) -> httpx.Response:
            try:
                await anyio.sleep(5)
            except anyio.get_cancelled_exc_class():
                observed.append("cancelled")
                raise
            return httpx.Response(200, json=envelope(result()))

        router.post("/crawl").mock(side_effect=slow)
        client = make(http)
        finished: list[object] = []
        with anyio.move_on_after(0.05) as scope:  # e.g. the job lease was lost
            finished.append(await fetch(client))
        assert scope.cancelled_caught
        assert finished == []
        assert observed == ["cancelled"]

    def test_timeouts(self, http: httpx.AsyncClient) -> None:
        client = make(http)
        assert client._timeout.read == 60.0
        assert client._deadline_s == 75.0
        assert make(http, page_timeout_ms=60000)._timeout.read == 90.0
        assert make(http, page_timeout_ms=60000)._deadline_s <= 330.0

    def test_wait_for_budgeted_and_capped(self, http: httpx.AsyncClient) -> None:
        options = SourceCrawlOptions(wait_for="css:.card")
        timeout, deadline = make(http)._timing(options)
        assert timeout.read == 90.0 and deadline == 105.0
        timeout, deadline = make(http, page_timeout_ms=60000)._timing(options)
        assert timeout.read == 150.0
        assert make(http, page_timeout_ms=60000, overall_deadline_s=1.5)._timing(options)[1] == 1.5
        with pytest.raises(ValueError):
            make(http, overall_deadline_s=0)

    def test_deadline_never_exceeds_navigation_lease(self, http: httpx.AsyncClient) -> None:
        # The rate limiter's per-host lease (330 s) must outlive any request deadline.
        assert DEFAULT_BACKOFF.in_flight_lease_seconds >= MAX_OVERALL_DEADLINE_S
        options = SourceCrawlOptions(wait_for="css:.card")
        slowest = make(http, page_timeout_ms=60000, connect_timeout_s=60)
        assert slowest._timing(options)[1] <= MAX_OVERALL_DEADLINE_S
        assert make(http, overall_deadline_s=MAX_OVERALL_DEADLINE_S)._deadline_s == MAX_OVERALL_DEADLINE_S
        with pytest.raises(ValueError):
            make(http, overall_deadline_s=MAX_OVERALL_DEADLINE_S + 1)

    async def test_wait_for_timeout_sent_per_source(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        route = router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(result())))
        client = make(http, source_options={"slow_source": SourceCrawlOptions(wait_for="css:.card")})
        await client.fetch_detailed(URL, purpose="search", source_key="slow_source")
        assert route.calls.last.request.extensions["timeout"]["read"] == 90.0
        await client.fetch_detailed(URL, purpose="search", source_key="other_source")
        assert route.calls.last.request.extensions["timeout"]["read"] == 60.0


class TestResultMapping:
    async def test_success(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(result())))
        detailed = await fetch(make(http, crawler_version_hint="0.9.4"))
        doc = detailed.document
        out = doc.fetch
        assert out.access_state == AccessState.OK and out.success
        assert out.http_status == 200
        assert out.final_url == URL and doc.final_url == URL
        assert out.redirect_count == 0
        assert out.bytes == len(HTML.encode())
        assert out.fetched_at == NOW
        assert out.crawler_version == "0.9.4"
        assert out.response_headers == {
            "content-type": "text/html; charset=utf-8",
            "x-robots-tag": "noarchive",
        }
        assert doc.html == HTML
        assert doc.text == "# Toyota RAV4\n"
        assert doc.content_type == "text/html; charset=utf-8"
        assert doc.raw_content_hash == hashlib.sha256(HTML.encode("utf-8")).hexdigest()
        assert detailed.server_processing_time_s == 1.25
        assert detailed.cache_status == "miss"
        assert detailed.redirected_status_code == 200

    async def test_results_matched_by_url_out_of_order(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        other = result(url="https://www.dealer-example.com/fahrzeug/999", html="<html>OTHER</html>" * 30)
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(other, result())))
        assert (await fetch(make(http))).document.html == HTML

    async def test_scheme_normalised_match(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        normalised = result(url="HTTPS://WWW.Dealer-Example.com:443/fahrzeug/12345", redirected_url=None)
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(normalised)))
        doc = (await fetch(make(http))).document
        assert doc.fetch.access_state == AccessState.OK

    async def test_missing_result(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        other = result(url="https://www.dealer-example.com/fahrzeug/999")
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(other)))
        doc = (await fetch(make(http))).document
        assert doc.fetch.error_code == "crawler_result_missing"
        assert doc.html is None

    async def test_anti_bot_is_access_blocked(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        blocked = result(
            success=False,
            status_code=403,
            html="<html>challenge</html>",
            error_message="Blocked by anti-bot protection: Akamai \x1b[31mblock\npage",
        )
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(blocked)))
        out = (await fetch(make(http))).document.fetch
        assert out.access_state == AccessState.ACCESS_BLOCKED
        assert out.error_code == "anti_bot_block"
        assert out.error_message is not None
        assert "\n" not in out.error_message and "\x1b" not in out.error_message
        assert out.error_message.startswith("Blocked by anti-bot protection")

    async def test_robots_denied(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        denied = result(
            success=False,
            status_code=403,
            html="",
            error_message="Access denied by robots.txt",
            response_headers={"X-Robots-Status": "Blocked by robots.txt"},
            redirected_url=None,
        )
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(denied)))
        doc = (await fetch(make(http))).document
        assert doc.fetch.access_state == AccessState.POLICY_DENIED
        assert doc.fetch.error_code == "robots_disallowed"
        assert doc.fetch.response_headers == {"x-robots-status": "Blocked by robots.txt"}
        assert doc.html is None

    async def test_robots_by_header_only(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        denied = result(
            success=False,
            status_code=403,
            html="",
            error_message="denied",
            response_headers={"x-robots-status": "Blocked by robots.txt"},
        )
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(denied)))
        assert (await fetch(make(http))).document.fetch.access_state == AccessState.POLICY_DENIED

    async def test_navigation_error(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        nav = result(
            success=False,
            status_code=None,
            html="",
            redirected_url=None,
            response_headers=None,
            error_message="Failed on navigating ACS-GOTO:\nPage.goto: net::ERR_NAME_NOT_RESOLVED at " + URL,
        )
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(nav)))
        out = (await fetch(make(http))).document.fetch
        assert out.access_state == AccessState.TRANSIENT_ERROR
        assert out.error_code == "navigation_failed"
        assert out.error_message == "navigation failed (net::ERR_NAME_NOT_RESOLVED)"

    async def test_site_header_does_not_mask_access_block(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        # Only Crawl4AI's own robots denial (value mentions robots) is our policy refusal.
        denied = result(
            success=False,
            status_code=403,
            html="",
            error_message="x",
            response_headers={"X-Robots-Status": "edge-node-7"},
        )
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(denied)))
        out = (await fetch(make(http))).document.fetch
        assert (out.access_state, out.error_code) == (AccessState.ACCESS_BLOCKED, "http_access_denied")

    async def test_wait_condition_failure_is_unexpected_content(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        # The page loaded but the source's CSS wait_for never matched: drift or an app shell,
        # so it is not counted against the host and not retried as a transient failure.
        failed = result(
            success=False,
            status_code=None,
            html="",
            redirected_url=None,
            error_message=(
                "Unexpected error in _crawl_web at line 790: Error: Wait condition failed: "
                "Timeout after 30000ms waiting for selector '.listing-card'"
            ),
        )
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(failed)))
        out = (await fetch(make(http))).document.fetch
        assert (out.access_state, out.error_code) == (AccessState.UNEXPECTED_CONTENT, "wait_condition_failed")
        assert out.error_message == "expected page element did not appear (wait_for)"

    async def test_navigation_timeout(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        nav = result(
            success=False, status_code=None, html=None, error_message="Page.goto: Timeout 30000ms exceeded"
        )
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(nav)))
        assert (await fetch(make(http))).document.fetch.error_message == "navigation failed (timeout)"

    @pytest.mark.parametrize("success", [True, False])
    @pytest.mark.parametrize(
        ("status", "state", "code"),
        [
            (401, AccessState.ACCESS_BLOCKED, "http_access_denied"),
            (403, AccessState.ACCESS_BLOCKED, "http_access_denied"),
            (451, AccessState.ACCESS_BLOCKED, "http_access_denied"),
            (404, AccessState.NOT_FOUND, "http_not_found"),
            (410, AccessState.REMOVED, "http_gone"),
            (429, AccessState.RATE_LIMITED, "http_rate_limited"),
            (500, AccessState.TRANSIENT_ERROR, "http_server_error"),
            (503, AccessState.TRANSIENT_ERROR, "http_server_error"),
        ],
    )
    async def test_per_result_status(
        self,
        http: httpx.AsyncClient,
        router: respx.MockRouter,
        success: bool,
        status: int,
        state: AccessState,
        code: str,
    ) -> None:
        res = result(
            success=success,
            status_code=status,
            error_message="" if success else "x",
            response_headers={"Retry-After": "120"},
        )
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(res)))
        doc = (await fetch(make(http))).document
        assert doc.fetch.access_state == state
        assert doc.fetch.error_code == code
        assert doc.fetch.http_status == status
        assert doc.fetch.retry_after_seconds == (120 if status == 429 else None)

    async def test_404_body_kept_for_removed_page_detection(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(result(status_code=404))))
        doc = (await fetch(make(http))).document
        assert doc.fetch.access_state == AccessState.NOT_FOUND
        assert doc.html == HTML

    async def test_other_4xx_unexpected(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(result(status_code=400))))
        out = (await fetch(make(http))).document.fetch
        assert (out.access_state, out.error_code) == (
            AccessState.UNEXPECTED_CONTENT,
            "http_status_unexpected",
        )

    async def test_missing_status(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(result(status_code=None))))
        assert (await fetch(make(http))).document.fetch.error_code == "missing_status"

    @pytest.mark.parametrize("html", ["", "   ", "<html><body></body></html>", None])
    async def test_empty_or_tiny_body(
        self, http: httpx.AsyncClient, router: respx.MockRouter, html: str | None
    ) -> None:
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(result(html=html))))
        out = (await fetch(make(http))).document.fetch
        assert (out.access_state, out.error_code) == (AccessState.UNEXPECTED_CONTENT, "empty_body")

    async def test_oversize_html_dropped(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        big = "<html>" + "x" * 5000 + "</html>"
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(result(html=big))))
        doc = (await fetch(make(http, max_response_bytes=1000, max_envelope_bytes=10**6))).document
        assert doc.fetch.access_state == AccessState.UNEXPECTED_CONTENT
        assert doc.fetch.error_code == "response_too_large"
        assert doc.fetch.bytes == len(big)
        assert doc.html is None and doc.text is None and doc.raw_content_hash is None

    async def test_oversize_multibyte_counted_in_bytes(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        big = "<html>" + "ä" * 600 + "</html>"  # 613 characters, 1213 UTF-8 bytes
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(result(html=big))))
        doc = (await fetch(make(http, max_response_bytes=1000))).document
        assert doc.fetch.error_code == "response_too_large"

    async def test_oversize_envelope_streaming(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        async def stream() -> AsyncIterator[bytes]:
            for _ in range(100):
                yield b"x" * 1024

        router.post("/crawl").mock(return_value=httpx.Response(200, content=stream()))
        doc = (await fetch(make(http, max_envelope_bytes=10_000))).document
        assert doc.fetch.error_code == "response_too_large"
        assert doc.fetch.bytes > 10_000
        assert doc.html is None

    async def test_oversize_declared_content_length(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        router.post("/crawl").mock(
            return_value=httpx.Response(200, content=b"{}", headers={"Content-Length": "999999999"})
        )
        doc = (await fetch(make(http, max_envelope_bytes=10_000))).document
        assert doc.fetch.error_code == "response_too_large"

    async def test_final_url_other_host(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        moved = result(redirected_url="https://login.other-example.net/sso")
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(moved)))
        doc = (await fetch(make(http))).document
        assert doc.fetch.access_state == AccessState.UNEXPECTED_CONTENT
        assert doc.fetch.error_code == "final_url_host_mismatch"
        assert doc.final_url == "https://login.other-example.net/sso"
        assert doc.html is None

    async def test_final_url_invalid(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        moved = result(redirected_url="chrome-error://chromewebdata/")
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(moved)))
        doc = (await fetch(make(http))).document
        assert doc.fetch.error_code == "final_url_invalid"
        assert doc.final_url is None

    async def test_same_host_redirect(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        moved = result(redirected_url="https://www.dealer-example.com/fahrzeug/12345-toyota-rav4")
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(moved)))
        doc = (await fetch(make(http))).document
        assert doc.fetch.access_state == AccessState.OK
        assert doc.fetch.redirect_count == 1
        assert doc.final_url == "https://www.dealer-example.com/fahrzeug/12345-toyota-rav4"

    async def test_cache_hit_is_not_fresh(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.post("/crawl").mock(
            return_value=httpx.Response(200, json=envelope(result(cache_status="hit")))
        )
        doc = (await fetch(make(http))).document
        assert doc.fetch.error_code == "cache_hit_not_fresh"
        assert doc.html is None

    async def test_markdown_string_and_garbage_fields(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        odd = result(
            markdown="plain", response_headers=["not", "a", "dict"], status_code="200", extra_new_field=1
        )
        router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(odd)))
        doc = (await fetch(make(http))).document
        assert doc.fetch.access_state == AccessState.OK
        assert doc.text == "plain"
        assert doc.fetch.response_headers == {}


class TestTargetRejection:
    @pytest.mark.parametrize(
        "url",
        [
            "raw:<html><body>x</body></html>",
            "raw://<html></html>",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "http://127.0.0.1/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "https://localhost/",
            "https://user:pw@www.dealer-example.com/x",
        ],
    )
    async def test_never_forwarded(self, http: httpx.AsyncClient, router: respx.MockRouter, url: str) -> None:
        route = router.post("/crawl").mock(return_value=httpx.Response(200, json=envelope(result())))
        doc = (await fetch(make(http), url=url)).document
        assert doc.fetch.access_state == AccessState.POLICY_DENIED
        assert doc.fetch.error_code == "url_rejected"
        assert not route.called

    async def test_unknown_purpose(self, http: httpx.AsyncClient) -> None:
        with pytest.raises(ValueError):
            await make(http).fetch_detailed(URL, purpose="admin", source_key="x")  # type: ignore[arg-type]


class TestConstruction:
    def test_protocol(self, http: httpx.AsyncClient) -> None:
        assert isinstance(make(http), CrawlClient)

    @pytest.mark.parametrize("timeout", [0, 999, 60001, 120000])
    def test_page_timeout_range(self, http: httpx.AsyncClient, timeout: int) -> None:
        with pytest.raises(ValueError):
            make(http, page_timeout_ms=timeout)

    @pytest.mark.parametrize(
        "ua",
        ["", "Mozilla/5.0 (X11; Linux x86_64) Chrome/116", "Bot\r\nX-Injected: 1", "x" * 300, "Boté"],
    )
    def test_user_agent_must_be_honest_and_clean(self, http: httpx.AsyncClient, ua: str) -> None:
        with pytest.raises(ValueError):
            Crawl4AIClient(BASE, None, ua, http=http)

    @pytest.mark.parametrize(
        "base",
        [
            "ftp://crawl4ai:11235",
            "http://user:pw@crawl4ai:11235",
            "http://crawl4ai:11235/?x=1",
            "crawl4ai:11235",
            "http://crawl4ai:99999",
        ],
    )
    def test_base_url_validation(self, http: httpx.AsyncClient, base: str) -> None:
        with pytest.raises(ValueError):
            Crawl4AIClient(base, None, http=http)

    def test_loopback_base_url_is_infrastructure_exception(self, http: httpx.AsyncClient) -> None:
        assert Crawl4AIClient("http://127.0.0.1:11235/", None, http=http).base_url == "http://127.0.0.1:11235"

    @pytest.mark.parametrize("size", [0, 50_000_001])
    def test_max_response_bytes(self, http: httpx.AsyncClient, size: int) -> None:
        with pytest.raises(ValueError):
            make(http, max_response_bytes=size)

    @pytest.mark.parametrize("size", [0, -1])
    def test_max_envelope_bytes_must_be_positive(self, http: httpx.AsyncClient, size: int) -> None:
        with pytest.raises(ValueError):
            make(http, max_envelope_bytes=size)

    @pytest.mark.parametrize(
        "options",
        [
            {"wait_for": "js:() => window.ready"},
            {"wait_for": "() => document.querySelector('a')"},
            {"wait_for": "css:a\nb"},
            {"locale": "german"},
            {"timezone_id": "Mars/Olympus"},
            {"extra": "x"},
        ],
    )
    def test_source_options_validation(self, options: dict[str, str]) -> None:
        with pytest.raises(ValidationError):
            SourceCrawlOptions(**options)

    def test_from_settings(self, http: httpx.AsyncClient) -> None:
        settings = Settings(
            crawl4ai_base_url="http://crawl4ai:11235",
            crawl4ai_api_token=SecretStr(TOKEN),
            crawl4ai_image="unclecode/crawl4ai:0.9.4",
        )
        client = Crawl4AIClient.from_settings(settings, http=http)
        assert client.base_url == BASE
        assert client._expected_version == "0.9.4"

    @pytest.mark.parametrize(
        ("image", "expected"),
        [
            ("unclecode/crawl4ai:0.9.4", "0.9.4"),
            ("unclecode/crawl4ai:0.9.4@sha256:" + "a" * 64, "0.9.4"),
            ("registry.local:5000/unclecode/crawl4ai:0.9.4-r1", "0.9.4"),
            ("unclecode/crawl4ai:latest", None),
            ("unclecode/crawl4ai:0.9", None),
            ("unclecode/crawl4ai", None),
            ("registry.local:5000/crawl4ai", None),
        ],
    )
    def test_expected_version_from_image(self, image: str, expected: str | None) -> None:
        assert expected_version_from_image(image) == expected

    async def test_owned_client_closed(self) -> None:
        async with Crawl4AIClient(BASE, None) as client:
            assert client.crawler_version is None
        assert client._http.is_closed


class TestHealth:
    async def test_ok_records_version_without_token(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        route = router.get("/health").mock(
            return_value=httpx.Response(200, json={"status": "ok", "timestamp": 1.0, "version": "0.9.4"})
        )
        async with httpx.AsyncClient(headers={"Authorization": "Bearer leaked-default"}) as http_with_default:
            client = make(http_with_default, crawler_version_hint="hint")
            health = await client.health()
        assert health.ok and health.version == "0.9.4" and health.checked_at == NOW
        assert "authorization" not in route.calls.last.request.headers
        assert client.crawler_version == "0.9.4"

    async def test_legacy_status(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.get("/health").mock(
            return_value=httpx.Response(200, json={"status": "healthy", "version": "0.9.2"})
        )
        health = await make(http).health()
        assert health.reachable and not health.ok
        assert health.status == "healthy"

    async def test_unreachable(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.get("/health").mock(side_effect=httpx.ConnectError("refused"))
        health = await make(http).health()
        assert not health.reachable and health.error == "unreachable"

    async def test_non_200_and_garbage(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.get("/health").mock(return_value=httpx.Response(503))
        assert (await make(http).health()).error == "http_503"
        router.get("/health").mock(return_value=httpx.Response(200, text="<html/>"))
        assert (await make(http).health()).error == "non_json_body"
        router.get("/health").mock(
            return_value=httpx.Response(200, json={"status": "ok", "version": "bad version!"})
        )
        assert (await make(http).health()).version is None


def _contract_routes(
    router: respx.MockRouter,
    *,
    health: dict[str, Any] | None = None,
    probe_status: int = 401,
    crawler_echo: dict[str, Any] | None = None,
    browser_echo: dict[str, Any] | None = None,
    dump_status: int = 200,
) -> tuple[respx.Route, respx.Route, respx.Route]:
    health_route = router.get("/health").mock(
        return_value=httpx.Response(
            200, json=health or {"status": "ok", "timestamp": 1.0, "version": "0.9.4"}
        )
    )
    probe_route = router.post("/crawl").mock(
        return_value=httpx.Response(probe_status, json={"detail": "Authentication required"})
    )

    def dump(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if dump_status != 200:
            return httpx.Response(dump_status, json={"detail": "Rejected config: field 'x' is not permitted"})
        if body["type"] == "CrawlerRunConfig":
            echo = crawler_echo or {
                "type": "CrawlerRunConfig",
                "params": {"page_timeout": 30000, "check_robots_txt": True},
            }
        else:
            echo = browser_echo or {
                "type": "BrowserConfig",
                "params": {
                    "user_agent": DEFAULT_USER_AGENT,
                    "headers": {"type": "dict", "value": {"sec-ch-ua": "x"}},
                },
            }
        return httpx.Response(200, json=echo)

    dump_route = router.post("/config/dump").mock(side_effect=dump)
    return health_route, probe_route, dump_route


class TestInspectContract:
    async def test_read_only_flow(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        health_route, probe_route, dump_route = _contract_routes(router)
        report = await make(http).inspect_contract()
        assert report.ok, report.problems
        assert report.read_only is True
        assert report.version_matches is True
        assert report.auth_enforced is True and report.auth_probe_status == 401
        assert report.payload_forbidden_fields == ()
        assert report.crawler_config is not None and report.crawler_config.accepted
        assert report.browser_config is not None and report.browser_config.accepted
        assert report.browser_config.server_computed_fields == ("headers",)
        assert report.browser_config.echoed_forbidden_fields == ()

        # Exactly one health check, one unauthenticated probe, two dry-runs; nothing else.
        assert health_route.call_count == 1 and probe_route.call_count == 1 and dump_route.call_count == 2
        assert len(router.calls) == 4
        probe = probe_route.calls.last.request
        assert "authorization" not in probe.headers
        assert json.loads(probe.content) == {"urls": []}  # cannot trigger a crawl even without auth
        dumped = [json.loads(c.request.content) for c in dump_route.calls]
        assert [d["type"] for d in dumped] == ["CrawlerRunConfig", "BrowserConfig"]
        assert all(c.request.headers["authorization"] == f"Bearer {TOKEN}" for c in dump_route.calls)
        for d in dumped:
            for forbidden in RESEARCH_FORBIDDEN:
                assert forbidden not in all_keys(d)

    async def test_each_source_config_is_dry_run(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        _, _, dump_route = _contract_routes(router)
        options = {
            "it_source": SourceCrawlOptions(locale="it-IT", timezone_id="Europe/Rome"),
            "de_source": SourceCrawlOptions(locale="de-DE", wait_for="css:.listing"),
            "plain_source": SourceCrawlOptions(),
        }
        report = await make(http, source_options=options).inspect_contract()
        assert report.ok, report.problems
        assert sorted(report.source_crawler_configs) == ["de_source", "it_source"]
        dumped = [json.loads(c.request.content) for c in dump_route.calls]
        assert len(dumped) == 4  # default crawler + browser + one per source with options
        de_params = dumped[2]["params"]
        assert (de_params["locale"], de_params["wait_for"]) == ("de-DE", "css:.listing")
        assert dumped[3]["params"]["timezone_id"] == "Europe/Rome"
        for d in dumped:
            assert not set(RESEARCH_FORBIDDEN) & all_keys(d)

    async def test_rejected_source_config_is_a_problem(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        _contract_routes(router)

        def dump(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["params"].get("locale") == "it-IT":
                return httpx.Response(
                    400, json={"detail": "Rejected config: field 'locale' is not permitted"}
                )
            return httpx.Response(200, json={"type": body["type"], "params": {"check_robots_txt": True}})

        router.post("/config/dump").mock(side_effect=dump)
        options = {"it_source": SourceCrawlOptions(locale="it-IT")}
        report = await make(http, source_options=options).inspect_contract()
        assert "it_source:CrawlerRunConfig:config_dump_rejected_http_400" in report.problems
        assert not report.source_crawler_configs["it_source"].accepted

    async def test_version_mismatch_reported_not_fixed(
        self, http: httpx.AsyncClient, router: respx.MockRouter
    ) -> None:
        _contract_routes(router, health={"status": "healthy", "version": "0.9.2"})
        report = await make(http).inspect_contract()
        assert report.version_matches is False
        assert "version_mismatch" in report.problems
        assert "health_status_not_ok" in report.problems
        assert len(router.calls) == 4

    async def test_auth_not_enforced(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        _contract_routes(router, probe_status=422)
        report = await make(http).inspect_contract()
        assert report.auth_enforced is False
        assert "auth_not_enforced" in report.problems

    async def test_without_token_no_dry_run(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        _, _, dump_route = _contract_routes(router)
        report = await make(http, token=None).inspect_contract()
        assert "api_token_missing" in report.problems
        assert not dump_route.called
        assert report.crawler_config is None

    async def test_rejected_dump(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        _contract_routes(router, dump_status=400)
        report = await make(http).inspect_contract()
        assert report.crawler_config is not None and not report.crawler_config.accepted
        assert "CrawlerRunConfig:config_dump_rejected_http_400" in report.problems
        assert report.crawler_config.detail is not None

    async def test_echo_problems(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        _contract_routes(
            router,
            crawler_echo={
                "type": "CrawlerRunConfig",
                "params": {
                    "page_timeout": 120000,
                    "magic": True,
                    "cache_mode": {"type": "CacheMode", "params": "enabled"},
                },
            },
            browser_echo={
                "type": "BrowserConfig",
                "params": {"enable_stealth": True, "user_agent": "Mozilla/5.0"},
            },
        )
        report = await make(http).inspect_contract()
        assert set(report.problems) >= {
            "CrawlerRunConfig:forbidden_field_echoed:magic",
            "CrawlerRunConfig:check_robots_txt_not_enabled",
            "CrawlerRunConfig:cache_mode_not_bypass",
            "CrawlerRunConfig:page_timeout_not_clamped",
            "BrowserConfig:stealth_enabled",
            "BrowserConfig:user_agent_not_ours",
        }

    async def test_unreachable_crawler(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        router.get("/health").mock(side_effect=httpx.ConnectError("refused"))
        router.post("/crawl").mock(side_effect=httpx.ConnectError("refused"))
        router.post("/config/dump").mock(side_effect=httpx.ConnectError("refused"))
        report = await make(http).inspect_contract()
        assert {"crawler_unreachable", "auth_probe_failed"} <= set(report.problems)
        assert report.auth_enforced is None
        assert report.crawler_config is not None
        assert report.crawler_config.problems == ("config_dump_unreachable",)

    async def test_unpinned_expected_version(self, http: httpx.AsyncClient, router: respx.MockRouter) -> None:
        _contract_routes(router)
        report = await make(http, expected_version=None).inspect_contract()
        assert "expected_version_unpinned" in report.problems
        assert report.version_matches is None
