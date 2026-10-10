"""Adversarial SSRF tests for the crawl path (spec sections 24, 31).

The policy-enforcing client wraps the real Crawl4AI client (HTTP mocked with respx):
a refused URL must never produce a request to the crawler, redirects are re-checked,
and DNS answers are validated at fetch time.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import httpx
import pytest
import respx
from pydantic import SecretStr

from suv_deals.adapters.base import CrawlClient, FetchOutcome, RawDocument
from suv_deals.adapters.crawl4ai_client import Crawl4AIClient, CrawlerAuthFailed
from suv_deals.clock import FrozenClock
from suv_deals.crawling.policy_client import (
    BudgetRefused,
    BudgetRequest,
    InMemoryBudgetGate,
    PolicyEnforcingCrawlClient,
)
from suv_deals.crawling.rate_limits import (
    Allow,
    BudgetDecision,
    Deny,
    DenyReason,
    PlanReason,
    RetryAction,
    Wait,
    WaitReason,
)
from suv_deals.crawling.url_policy import SourceUrlPolicy
from suv_deals.domain.enums import AccessState
from suv_deals.domain.sources import RateBudget

NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)
CRAWLER = "http://crawl4ai:11235"
HOST = "www.dealer-example.com"
SOURCE = "dealer_example"
DETAIL = f"https://{HOST}/fahrzeug/12345"
HTML = "<html><body>" + "<p>vehicle</p>" * 40 + "</body></html>"


class RecordingGate:
    def __init__(self, decision: BudgetDecision | None = None) -> None:
        self.decision = decision or Allow()
        self.acquired: list[BudgetRequest] = []
        self.released: list[tuple[BudgetRequest, FetchOutcome | None]] = []

    async def acquire(self, request: BudgetRequest) -> BudgetDecision:
        self.acquired.append(request)
        return self.decision

    async def release(self, request: BudgetRequest, outcome: FetchOutcome | None) -> None:
        self.released.append((request, outcome))


def crawl_result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "url": DETAIL,
        "success": True,
        "status_code": 200,
        "error_message": "",
        "html": HTML,
        "redirected_url": DETAIL,
        "response_headers": {"content-type": "text/html"},
        "cache_status": "miss",
        "markdown": {"raw_markdown": "vehicle"},
    }
    result.update(overrides)
    return {"success": True, "results": [result], "server_processing_time_s": 0.5}


async def public_dns(host: str, port: int) -> list[str]:
    return ["93.184.215.14"]


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=CRAWLER, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def policy(**overrides: Any) -> SourceUrlPolicy:
    kwargs: dict[str, Any] = {
        "allowed_hosts": (HOST,),
        "search_paths": (r"/suche",),
        "detail_paths": (r"/fahrzeug/[0-9]{1,12}(-[a-z0-9-]{1,80})?",),
    }
    kwargs.update(overrides)
    return SourceUrlPolicy(SOURCE, **kwargs)


def build(
    http: httpx.AsyncClient,
    *,
    gate: RecordingGate | None = None,
    resolver: Any = public_dns,
    source_policy: SourceUrlPolicy | None = None,
) -> tuple[PolicyEnforcingCrawlClient, RecordingGate]:
    inner = Crawl4AIClient(CRAWLER, SecretStr("token"), http=http, clock=FrozenClock(NOW))
    gate = gate or RecordingGate()
    client = PolicyEnforcingCrawlClient(
        inner, {SOURCE: source_policy or policy()}, gate, resolver=resolver, clock=FrozenClock(NOW)
    )
    return client, gate


BLOCKED_TARGETS = [
    # private / loopback / link-local / metadata literals
    "https://127.0.0.1/fahrzeug/1",
    "https://10.0.0.5/fahrzeug/1",
    "https://172.16.3.4/fahrzeug/1",
    "https://192.168.1.1/fahrzeug/1",
    "https://100.64.0.1/fahrzeug/1",
    "https://169.254.169.254/latest/meta-data/",
    "https://0.0.0.0/fahrzeug/1",
    # unusual numeric spellings
    "https://2130706433/fahrzeug/1",
    "https://0177.0.0.1/fahrzeug/1",
    "https://0x7f000001/fahrzeug/1",
    "https://127.1/fahrzeug/1",
    # IPv6 and embedded IPv4 forms
    "https://[::1]/fahrzeug/1",
    "https://[::ffff:127.0.0.1]/fahrzeug/1",
    "https://[::ffff:a9fe:a9fe]/fahrzeug/1",
    "https://[64:ff9b::7f00:1]/fahrzeug/1",
    "https://[2002:7f00:1::]/fahrzeug/1",
    "https://[fe80::1]/fahrzeug/1",
    "https://[fc00::1]/fahrzeug/1",
    "https://[fd00:ec2::254]/latest/meta-data/",
    "https://[fe80::1%25eth0]/fahrzeug/1",
    # metadata / internal names
    "https://metadata.google.internal/computeMetadata/v1/",
    "https://localhost/fahrzeug/1",
    "https://crawl4ai/fahrzeug/1",
    "https://db.internal/fahrzeug/1",
    # crawler and app infrastructure itself
    "http://crawl4ai:11235/crawl",
    "http://127.0.0.1:11235/config/dump",
    # schemes
    "raw:<html><script>fetch('http://169.254.169.254')</script></html>",
    "raw://<html></html>",
    "javascript:fetch('http://127.0.0.1')",
    "data:text/html,<iframe src=http://10.0.0.1>",
    "file:///etc/passwd",
    "gopher://www.dealer-example.com/_",
    "ws://www.dealer-example.com/fahrzeug/1",
    # credentials and host confusion
    f"https://user:pass@{HOST}/fahrzeug/1",
    f"https://{HOST}@127.0.0.1/fahrzeug/1",
    f"https://{HOST}.evil.example.net/fahrzeug/1",
    "https://evil.example.net/fahrzeug/1",
    f"https://{HOST}:8080/fahrzeug/1",
    # path outside the allow-list / traversal
    f"https://{HOST}/admin",
    f"https://{HOST}/fahrzeug/../admin",
    f"https://{HOST}/fahrzeug/%2e%2e/admin",
    f"https://{HOST}/fahrzeug%2f..%2fadmin",
    f"https://{HOST}/fahrzeug/..;/admin",
    f"https://{HOST}/fahrzeug/%2e%2e;jsessionid=1/admin",
    f"https://{HOST}/fahrzeug/1?next=http://127.0.0.1/" + "a" * 2100,
    # whitespace / CRLF smuggling
    f"https://{HOST}/fahrzeug/1\r\nHost: 127.0.0.1",
    f"https://{HOST}/fahrzeug/1 HTTP/1.1",
]


@pytest.mark.parametrize("target", BLOCKED_TARGETS)
async def test_blocked_targets_never_reach_crawler(
    http: httpx.AsyncClient, router: respx.MockRouter, target: str
) -> None:
    route = router.post("/crawl").mock(return_value=httpx.Response(200, json=crawl_result()))
    client, gate = build(http)
    doc = await client.fetch(target, purpose="detail", source_key=SOURCE)
    assert doc.fetch.access_state == AccessState.POLICY_DENIED
    assert doc.fetch.error_code is not None and doc.fetch.error_code.startswith("policy_")
    assert doc.html is None
    assert not route.called
    assert gate.acquired == []  # no budget consumed for refused URLs
    assert "pass@" not in doc.url and "pass@" not in doc.fetch.requested_url
    assert "\n" not in doc.fetch.requested_url


async def test_unknown_source_denied(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    route = router.post("/crawl").mock(return_value=httpx.Response(200, json=crawl_result()))
    client, _ = build(http)
    doc = await client.fetch(DETAIL, purpose="detail", source_key="someone_elses_source")
    assert doc.fetch.error_code == "policy_unknown_source"
    assert not route.called


async def test_purpose_mismatch_denied(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    route = router.post("/crawl").mock(return_value=httpx.Response(200, json=crawl_result()))
    client, _ = build(http)
    doc = await client.fetch(DETAIL, purpose="search", source_key=SOURCE)
    assert doc.fetch.error_code == "policy_path_not_allowed"
    assert not route.called


@pytest.mark.parametrize(
    "answers",
    [
        ["127.0.0.1"],
        ["10.20.30.40"],
        ["169.254.169.254"],
        ["93.184.215.14", "192.168.0.10"],  # mixed answer rebinding
        ["::1"],
        ["::ffff:10.0.0.1"],
        ["fd00:ec2::254"],
    ],
)
async def test_dns_rebinding_rejected_at_fetch_time(
    http: httpx.AsyncClient, router: respx.MockRouter, answers: list[str]
) -> None:
    async def rebinding(host: str, port: int) -> list[str]:
        return answers

    route = router.post("/crawl").mock(return_value=httpx.Response(200, json=crawl_result()))
    client, gate = build(http, resolver=rebinding)
    doc = await client.fetch(DETAIL, purpose="detail", source_key=SOURCE)
    assert doc.fetch.access_state == AccessState.POLICY_DENIED
    assert doc.fetch.error_code == "policy_dns_non_public"
    assert not route.called
    assert len(gate.released) == 1 and gate.released[0][1] is not None


async def test_dns_failure_is_transient_not_security(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    async def failing(host: str, port: int) -> list[str]:
        raise OSError("SERVFAIL")

    route = router.post("/crawl").mock(return_value=httpx.Response(200, json=crawl_result()))
    client, _ = build(http, resolver=failing)
    doc = await client.fetch(DETAIL, purpose="detail", source_key=SOURCE)
    assert doc.fetch.access_state == AccessState.TRANSIENT_ERROR
    assert doc.fetch.error_code == "policy_dns_resolution_failed"
    assert not route.called


@pytest.mark.parametrize(
    ("final", "code"),
    [
        ("https://evil.example.net/fahrzeug/12345", "policy_cross_host_redirect"),
        ("http://127.0.0.1/admin", "policy_cross_host_redirect"),
        ("http://169.254.169.254/latest/meta-data/", "policy_cross_host_redirect"),
        ("https://[::1]/fahrzeug/12345", "policy_cross_host_redirect"),
        ("https://dealer-example.com/fahrzeug/12345", "policy_cross_host_redirect"),
        (f"https://{HOST}/login?return=/fahrzeug/12345", "policy_path_not_allowed"),
        (f"http://{HOST}/fahrzeug/12345", "policy_scheme_not_allowed"),
    ],
)
async def test_redirects_rechecked(
    http: httpx.AsyncClient, router: respx.MockRouter, final: str, code: str
) -> None:
    router.post("/crawl").mock(return_value=httpx.Response(200, json=crawl_result(redirected_url=final)))
    client, gate = build(http)
    doc = await client.fetch(DETAIL, purpose="detail", source_key=SOURCE)
    assert doc.fetch.access_state == AccessState.POLICY_DENIED
    assert doc.fetch.error_code == code
    assert doc.html is None and doc.text is None and doc.raw_content_hash is None
    released = gate.released[0][1]
    assert released is not None and released.access_state == AccessState.POLICY_DENIED


async def test_scheme_downgrade_redirect(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    router.post("/crawl").mock(
        return_value=httpx.Response(200, json=crawl_result(redirected_url=f"http://{HOST}/fahrzeug/12345"))
    )
    client, _ = build(http, source_policy=policy(allowed_schemes=("https", "http")))
    doc = await client.fetch(DETAIL, purpose="detail", source_key=SOURCE)
    assert doc.fetch.error_code == "policy_scheme_downgrade"


async def test_allowed_same_host_redirect_passes(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    final = f"https://{HOST}/fahrzeug/12345-toyota-rav4"
    route = router.post("/crawl").mock(
        return_value=httpx.Response(200, json=crawl_result(redirected_url=final))
    )
    client, gate = build(http)
    doc = await client.fetch(DETAIL, purpose="detail", source_key=SOURCE)
    assert doc.fetch.access_state == AccessState.OK
    assert doc.final_url == final
    assert doc.html == HTML
    assert json.loads(route.calls.last.request.content)["urls"] == [DETAIL]
    assert gate.acquired[0] == BudgetRequest(source_key=SOURCE, host=HOST, purpose="detail", url=DETAIL)
    released = gate.released[0][1]
    assert released is not None and released.access_state == AccessState.OK


async def test_crawler_ssrf_block_is_policy_denied(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    router.post("/crawl").mock(
        return_value=httpx.Response(400, json={"detail": "URL blocked (SSRF protection): URL blocked"})
    )
    client, _ = build(http)
    doc = await client.fetch(DETAIL, purpose="detail", source_key=SOURCE)
    assert doc.fetch.access_state == AccessState.POLICY_DENIED
    assert doc.fetch.error_code == "crawler_url_blocked"


async def test_no_request_without_budget(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    route = router.post("/crawl").mock(return_value=httpx.Response(200, json=crawl_result()))
    gate = RecordingGate(Wait(until=NOW, reason=WaitReason.MIN_DELAY))
    client, _ = build(http, gate=gate)
    with pytest.raises(BudgetRefused):
        await client.fetch(DETAIL, purpose="detail", source_key=SOURCE)
    assert not route.called
    assert gate.released == []


async def test_typed_crawler_errors_propagate_and_release_budget(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    router.post("/crawl").mock(return_value=httpx.Response(401, json={"detail": "Authentication required"}))
    client, gate = build(http)
    with pytest.raises(CrawlerAuthFailed):
        await client.fetch(DETAIL, purpose="detail", source_key=SOURCE)
    assert gate.released == [(gate.acquired[0], None)]


async def test_budget_released_on_cancellation(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await anyio.sleep(5)
        return httpx.Response(200, json=crawl_result())

    router.post("/crawl").mock(side_effect=slow)
    client, gate = build(http)
    with anyio.move_on_after(0.05) as scope:
        await client.fetch(DETAIL, purpose="detail", source_key=SOURCE)
    assert scope.cancelled_caught
    assert gate.released == [(gate.acquired[0], None)]


class _FakeInner:
    """Inner client that claims a final URL; proves the wrapper never trusts it."""

    def __init__(self, final_url: str) -> None:
        self.final_url = final_url

    async def fetch(self, url: str, *, purpose: str, source_key: str) -> RawDocument:
        outcome = FetchOutcome(
            requested_url=url, final_url=self.final_url, http_status=200, success=True,
            access_state=AccessState.OK, bytes=len(HTML), fetched_at=NOW,
        )  # fmt: skip
        return RawDocument(url=url, final_url=self.final_url, fetched_at=NOW, html=HTML, fetch=outcome)


@pytest.mark.parametrize(
    "final",
    [
        "http://10.0.0.1/fahrzeug/12345",
        "https://user:pw@www.dealer-example.com/fahrzeug/12345",
        "javascript:alert(1)",
        "https://www.dealer-example.com/fahrzeug/12345/../../admin",
    ],
)
async def test_inner_final_url_is_never_trusted(final: str) -> None:
    inner: CrawlClient = _FakeInner(final)
    client = PolicyEnforcingCrawlClient(inner, {SOURCE: policy()}, RecordingGate(), resolver=public_dns)
    doc = await client.fetch(DETAIL, purpose="detail", source_key=SOURCE)
    assert doc.fetch.access_state == AccessState.POLICY_DENIED
    assert doc.html is None
    assert "pw@" not in (doc.final_url or "")


def test_wrapper_satisfies_protocol() -> None:
    client = PolicyEnforcingCrawlClient(_FakeInner(DETAIL), {SOURCE: policy()}, RecordingGate())
    assert isinstance(client, CrawlClient)


class _ClassifiedInner:
    """Inner client whose target answered with a failure on an off-policy final URL."""

    def __init__(self, state: AccessState, final_url: str, *, retry_after: int | None = None) -> None:
        self.state = state
        self.final_url = final_url
        self.retry_after = retry_after

    async def fetch(self, url: str, *, purpose: str, source_key: str) -> RawDocument:
        outcome = FetchOutcome(
            requested_url=url, final_url=self.final_url, http_status=403, success=False,
            access_state=self.state, error_code="target_failure", error_message="target said no",
            retry_after_seconds=self.retry_after, bytes=len(HTML), fetched_at=NOW,
        )  # fmt: skip
        return RawDocument(
            url=url, final_url=self.final_url, fetched_at=NOW, html=HTML, text="t",
            raw_content_hash="a" * 64, fetch=outcome,
        )  # fmt: skip


@pytest.mark.parametrize(
    "state", [AccessState.ACCESS_BLOCKED, AccessState.RATE_LIMITED, AccessState.TRANSIENT_ERROR]
)
@pytest.mark.parametrize(
    "final",
    [
        "https://captcha.example.net/challenge?u=1",
        "https://user:pw@sso.example.net/login",
        f"https://{HOST}/login?return=/fahrzeug/12345",
    ],
)
async def test_refused_redirect_keeps_target_failure_classification(state: AccessState, final: str) -> None:
    """A block/throttle/failure behind an off-policy redirect must still pause/back off the route."""
    gate = RecordingGate()
    inner: CrawlClient = _ClassifiedInner(state, final, retry_after=600)
    client = PolicyEnforcingCrawlClient(inner, {SOURCE: policy()}, gate, resolver=public_dns)
    doc = await client.fetch(DETAIL, purpose="detail", source_key=SOURCE)
    assert doc.fetch.access_state == state
    assert doc.fetch.error_code == "target_failure"
    assert doc.fetch.retry_after_seconds == 600
    assert doc.fetch.success is False
    assert doc.fetch.error_message is not None and "final URL refused by policy" in doc.fetch.error_message
    assert doc.html is None and doc.text is None and doc.raw_content_hash is None
    assert "pw@" not in (doc.final_url or "") and "pw@" not in (doc.fetch.final_url or "")
    released = gate.released[0][1]
    assert released is not None and released.access_state == state


@pytest.mark.parametrize(
    "state", [AccessState.NOT_FOUND, AccessState.REMOVED, AccessState.UNEXPECTED_CONTENT]
)
async def test_refused_redirect_never_reports_removal(state: AccessState) -> None:
    """A 404/410 reached through an off-policy redirect is not evidence that our listing was removed."""
    inner: CrawlClient = _ClassifiedInner(state, f"https://{HOST}/suche?removed=1")
    client = PolicyEnforcingCrawlClient(inner, {SOURCE: policy()}, RecordingGate(), resolver=public_dns)
    doc = await client.fetch(DETAIL, purpose="detail", source_key=SOURCE)
    assert doc.fetch.access_state == AccessState.POLICY_DENIED
    assert doc.fetch.error_code == "policy_path_not_allowed"
    assert doc.html is None


async def test_anti_bot_redirect_to_captcha_host_pauses_route(
    http: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    """End to end: Crawl4AI anti-bot verdict on a third-party CAPTCHA host -> route paused, no retry."""
    blocked = crawl_result(
        success=False,
        status_code=403,
        html="<html>captcha</html>",
        redirected_url="https://geo.captcha-delivery.example.net/captcha/?initialCid=x",
        error_message="Blocked by anti-bot protection: captcha page",
    )
    route = router.post("/crawl").mock(return_value=httpx.Response(200, json=blocked))
    clock = FrozenClock(NOW)
    gate = InMemoryBudgetGate({SOURCE: RateBudget(min_delay_seconds=5)}, clock=clock)
    inner = Crawl4AIClient(CRAWLER, SecretStr("token"), http=http, clock=clock)
    client = PolicyEnforcingCrawlClient(inner, {SOURCE: policy()}, gate, resolver=public_dns, clock=clock)

    doc = await client.fetch(DETAIL, purpose="detail", source_key=SOURCE)
    assert doc.fetch.access_state == AccessState.ACCESS_BLOCKED
    assert doc.fetch.error_code == "anti_bot_block"
    assert doc.html is None
    result = gate.last_result(SOURCE, HOST)
    assert result is not None
    assert result.plan.action == RetryAction.STOP and result.plan.reason == PlanReason.ACCESS_BLOCKED
    assert result.pause is not None and result.pause.requires_explicit_action

    clock.advance(timedelta(days=2))
    with pytest.raises(BudgetRefused) as info:
        await client.fetch(DETAIL, purpose="detail", source_key=SOURCE)
    assert isinstance(info.value.decision, Deny) and info.value.decision.reason == DenyReason.ACCESS_BLOCKED
    assert route.call_count == 1  # never retried, with or without evasion
