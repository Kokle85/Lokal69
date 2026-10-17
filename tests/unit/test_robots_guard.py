"""The worker's robots.txt guard (`crawling.robots_guard`) through the policy-enforcing client.

Everything is offline and SYNTHETIC: the host is the reserved ``dealer.example``, robots.txt comes
from scripted transports, the budget gate is the in-memory `InMemoryBudgetGate` with a frozen
clock. What is checked: the freshness window (reuse within the UTC day; a new day always fetches
again; unreachable reused for 30 minutes; an access denial never reused), robots.txt fetched before
the first page and counted/spaced like a page request, a disallowed URL refused without any request,
an unreachable robots.txt meaning no page request at all, the robots crawl-delay reaching the gate,
the outcome classification of robots requests and both transports.
"""

from __future__ import annotations

import gzip
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from suv_deals.adapters.base import FetchOutcome, FetchPurpose, RawDocument
from suv_deals.adapters.crawl4ai_client import DEFAULT_USER_AGENT
from suv_deals.adapters.fixture_client import FixtureCrawlClient
from suv_deals.clock import FrozenClock
from suv_deals.crawling.detail import lifts_on_its_own
from suv_deals.crawling.policy_client import (
    BudgetRefused,
    BudgetRequest,
    InMemoryBudgetGate,
    PolicyEnforcingCrawlClient,
    RobotsUnavailable,
)
from suv_deals.crawling.rate_limits import BudgetDecision, DenyReason, Wait, WaitReason
from suv_deals.crawling.robots import (
    ROBOTS_MAX_BYTES,
    TRUNCATED_ERROR_KIND,
    RobotsAvailability,
    RobotsFetchResponse,
    RobotsRevision,
    parse_robots_txt,
)
from suv_deals.crawling.robots_guard import (
    DIRECT_TRANSPORT_LABEL,
    ROBOTS_RETRY_INTERVAL,
    CrawlClientRobotsTransport,
    RobotsGuard,
    SafeHttpRobotsTransport,
    reusable_until,
    revision_is_fresh,
    robots_access,
    robots_fetch_outcome,
)
from suv_deals.crawling.url_policy import SourceUrlPolicy
from suv_deals.domain.enums import AccessState
from suv_deals.domain.sources import RateBudget
from suv_deals.integrations.safe_http import DEFAULT_USER_AGENT as SAFE_HTTP_DEFAULT_USER_AGENT
from suv_deals.integrations.safe_http import SafeHttpClient
from suv_deals.workers.runtime import refusal_disposition, robots_transport

NOW = datetime(2026, 10, 17, 8, 0, 0, tzinfo=UTC)
HOST = "dealer.example"
SOURCE = "fixture_dealer_de"
ROBOTS_URL = f"https://{HOST}/robots.txt"
SEARCH = f"https://{HOST}/suche?typ=suv"
BLOCKED_SEARCH = f"https://{HOST}/suche?typ=robots-gesperrt"
DETAIL = f"https://{HOST}/fahrzeug/TEST-204"
BUDGET = RateBudget(min_delay_seconds=20, max_search_pages_per_run=5, daily_request_budget=50)
RULES = b"User-agent: *\nDisallow: /suche?typ=robots-gesperrt\nDisallow: /fahrzeug/TEST-230\n"
POLICY = SourceUrlPolicy(
    SOURCE, allowed_hosts=(HOST,), search_paths=("/suche",), detail_paths=(r"/fahrzeug/.+",)
)


async def public_dns(host: str, port: int) -> list[str]:
    del host, port
    return ["93.184.215.14"]


def revision(
    availability: RobotsAvailability = RobotsAvailability.AVAILABLE,
    *,
    fetched_at: datetime = NOW,
    status: int | None = 200,
    body: bytes = RULES,
) -> RobotsRevision:
    parsed = parse_robots_txt(body) if availability == RobotsAvailability.AVAILABLE else None
    return RobotsRevision(
        host=HOST,
        robots_url=ROBOTS_URL,
        fetched_at=fetched_at,
        http_status=status,
        availability=availability,
        parse_ok=availability != RobotsAvailability.UNREACHABLE,
        groups=parsed.groups if parsed else (),
    )


class Transport:
    """Scripted robots transport: one response per URL, every call recorded."""

    label = "test"

    def __init__(self, response: RobotsFetchResponse | None = None) -> None:
        self.response = response or RobotsFetchResponse(200, RULES, None)
        self.calls: list[str] = []

    async def get(self, url: str, *, source_key: str) -> RobotsFetchResponse:
        assert source_key == SOURCE
        self.calls.append(url)
        return self.response


class Store:
    """In-memory robots store: what the worker would read back from ops.robots_revisions."""

    def __init__(self, stored: RobotsRevision | None = None, *, changed: bool = False) -> None:
        self.stored = stored
        self.changed = changed
        self.recorded: list[tuple[RobotsRevision, FetchOutcome]] = []
        self.reads = 0

    async def latest(self, host: str) -> RobotsRevision | None:
        assert host == HOST
        self.reads += 1
        return self.stored

    async def record(self, revision: RobotsRevision, outcome: FetchOutcome) -> bool:
        self.recorded.append((revision, outcome))
        self.stored = revision
        return self.changed


class Inner:
    """The crawler stand-in: serves a page for any URL and records every request."""

    def __init__(self, *, final_url: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.final_url = final_url

    async def fetch(self, url: str, *, purpose: FetchPurpose, source_key: str) -> RawDocument:
        del source_key
        self.calls.append((url, purpose))
        final = self.final_url or url
        outcome = FetchOutcome(
            requested_url=url,
            final_url=final,
            http_status=200,
            success=True,
            access_state=AccessState.OK,
            bytes=400,
            fetched_at=NOW,
        )
        return RawDocument(url=url, final_url=final, fetched_at=NOW, html="<html>" + "x" * 400, fetch=outcome)


class RecordingGate(InMemoryBudgetGate):
    """The in-memory gate, recording every budget request in order."""

    def __init__(self, clock: FrozenClock, budget: RateBudget = BUDGET) -> None:
        super().__init__({SOURCE: budget}, clock=clock, rng=random.Random(1))
        self.requests: list[BudgetRequest] = []

    async def acquire(self, request: BudgetRequest) -> BudgetDecision:
        self.requests.append(request)
        return await super().acquire(request)


class World:
    def __init__(
        self,
        *,
        transport: Transport | None = None,
        store: Store | None = None,
        budget: RateBudget = BUDGET,
        inner: Inner | None = None,
    ) -> None:
        self.clock = FrozenClock(NOW)
        self.transport = transport or Transport()
        self.store = store or Store()
        self.gate = RecordingGate(self.clock, budget)
        self.inner = inner or Inner()
        self.guard = RobotsGuard(self.store, self.transport, resolver=public_dns, clock=self.clock)
        self.client = PolicyEnforcingCrawlClient(
            self.inner, {SOURCE: POLICY}, self.gate, robots=self.guard, resolver=public_dns, clock=self.clock
        )

    async def fetch(self, url: str, purpose: FetchPurpose = "search") -> RawDocument:
        return await self.client.fetch(url, purpose=purpose, source_key=SOURCE)

    def pass_time(self, seconds: float) -> None:
        self.clock.advance(timedelta(seconds=seconds))

    def purposes(self) -> list[str]:
        return [r.purpose for r in self.gate.requests]

    def requests_today(self) -> int:
        return self.gate.daily_usage(SOURCE, self.clock.now()).requests


# ---------------------------------------------------------------------------------- freshness


def test_a_revision_is_reused_within_its_utc_day_only() -> None:
    late = revision(fetched_at=datetime(2026, 10, 17, 23, 50, tzinfo=UTC))
    assert revision_is_fresh(late, datetime(2026, 10, 17, 23, 59, 59, tzinfo=UTC))
    # The first request of a new UTC day always fetches robots.txt again (never 24 h of reuse
    # across midnight, even when only ten minutes have passed).
    assert not revision_is_fresh(late, datetime(2026, 10, 18, 0, 0, 1, tzinfo=UTC))
    assert reusable_until(late) == datetime(2026, 10, 18, tzinfo=UTC)
    early = revision(fetched_at=datetime(2026, 10, 17, 0, 0, 5, tzinfo=UTC))
    assert revision_is_fresh(early, datetime(2026, 10, 17, 23, 0, tzinfo=UTC))
    assert reusable_until(early) - early.fetched_at <= timedelta(hours=24)


def test_no_robots_txt_is_reused_like_rules() -> None:
    missing = revision(RobotsAvailability.UNAVAILABLE, status=404)
    assert revision_is_fresh(missing, NOW + timedelta(hours=10))
    assert reusable_until(missing) == datetime(2026, 10, 18, tzinfo=UTC)


def test_an_unreachable_robots_txt_is_asked_again_after_thirty_minutes() -> None:
    down = revision(RobotsAvailability.UNREACHABLE, status=503)
    assert timedelta(minutes=30) == ROBOTS_RETRY_INTERVAL
    assert revision_is_fresh(down, NOW + timedelta(minutes=29))
    assert not revision_is_fresh(down, NOW + timedelta(minutes=30))
    near_midnight = revision(
        RobotsAvailability.UNREACHABLE, status=None, fetched_at=datetime(2026, 10, 17, 23, 45, tzinfo=UTC)
    )
    assert reusable_until(near_midnight) == datetime(2026, 10, 18, tzinfo=UTC)


@pytest.mark.parametrize("status", [401, 403, 407])
def test_an_access_denial_is_never_reused(status: int) -> None:
    denied = revision(RobotsAvailability.UNAVAILABLE, status=status)
    assert not revision_is_fresh(denied, NOW)
    assert reusable_until(denied) == NOW


def test_a_revision_from_the_future_is_not_trusted() -> None:
    assert revision_is_fresh(revision(fetched_at=NOW + timedelta(minutes=4)), NOW)
    assert not revision_is_fresh(revision(fetched_at=NOW + timedelta(minutes=6)), NOW)


# ---------------------------------------------------------------------------------- the guard


async def test_robots_txt_is_fetched_first_and_counted_like_any_request() -> None:
    world = World()
    # The first page request of the host: robots.txt first (a request of its own, budgeted).
    with pytest.raises(BudgetRefused) as spaced:
        await world.fetch(SEARCH)
    assert world.transport.calls == [ROBOTS_URL]
    assert world.purposes() == ["robots", "search"]
    assert world.inner.calls == []  # the page waits for the host's spacing like any request
    assert spaced.value.reason == WaitReason.MIN_DELAY.value
    assert world.requests_today() == 1
    [(stored, outcome)] = world.store.recorded
    assert stored.availability == RobotsAvailability.AVAILABLE and stored.host == HOST
    assert outcome.requested_url == ROBOTS_URL and outcome.access_state == AccessState.OK
    assert outcome.http_status == 200 and outcome.bytes == len(RULES) and outcome.crawler_version == "test"
    world.pass_time(20)
    document = await world.fetch(SEARCH)
    assert document.fetch.access_state == AccessState.OK
    assert world.inner.calls == [(SEARCH, "search")]
    assert world.transport.calls == [ROBOTS_URL]  # reused, not fetched again
    assert world.requests_today() == 2


async def test_a_fresh_revision_is_reused_and_a_new_utc_day_fetches_again() -> None:
    world = World()
    with pytest.raises(BudgetRefused):
        await world.fetch(SEARCH)
    for _ in range(3):
        world.pass_time(30)
        await world.fetch(DETAIL, "detail")
    assert world.transport.calls == [ROBOTS_URL]
    world.clock.advance(timedelta(days=1))
    with pytest.raises(BudgetRefused):  # robots.txt again, then the page waits for the spacing
        await world.fetch(DETAIL, "detail")
    assert world.transport.calls == [ROBOTS_URL, ROBOTS_URL]
    assert len(world.store.recorded) == 2


async def test_a_revision_another_worker_stored_today_is_used_without_a_request() -> None:
    stored = revision(fetched_at=NOW - timedelta(hours=2))
    world = World(store=Store(stored))
    document = await world.fetch(SEARCH)
    assert document.fetch.access_state == AccessState.OK
    assert world.transport.calls == [] and world.store.recorded == []
    assert world.purposes() == ["search"]
    # Yesterday's revision is not current: robots.txt is fetched before the page.
    yesterday = World(store=Store(revision(fetched_at=NOW - timedelta(hours=9))))
    yesterday.clock.advance(timedelta(hours=16, minutes=30))  # 00:30 the next day
    with pytest.raises(BudgetRefused):
        await yesterday.fetch(SEARCH)
    assert yesterday.transport.calls == [ROBOTS_URL]


@pytest.mark.parametrize(
    ("url", "purpose"), [(BLOCKED_SEARCH, "search"), (f"https://{HOST}/fahrzeug/TEST-230", "detail")]
)
async def test_a_disallowed_url_is_never_requested(url: str, purpose: FetchPurpose) -> None:
    world = World(store=Store(revision(fetched_at=NOW - timedelta(minutes=5))))
    document = await world.fetch(url, purpose)
    assert world.inner.calls == []
    assert world.purposes() == []  # not even a budget request for the page
    assert document.html is None
    assert document.fetch.access_state == AccessState.POLICY_DENIED
    assert document.fetch.error_code == "robots_disallowed"
    assert "Disallow:" in (document.fetch.error_message or "")


@pytest.mark.parametrize(
    "response",
    [
        RobotsFetchResponse(503, b"maintenance", None),
        RobotsFetchResponse(429, None, None, None, "120"),
        RobotsFetchResponse(None, None, "network_connect_failed"),
        RobotsFetchResponse(200, None, "network_connection_lost"),  # rules not read completely
    ],
    ids=["503", "429", "network", "incomplete-200"],
)
async def test_an_unreachable_robots_txt_means_no_page_request(response: RobotsFetchResponse) -> None:
    world = World(transport=Transport(response))
    with pytest.raises(RobotsUnavailable) as refused:
        await world.fetch(SEARCH)
    assert world.inner.calls == [] and world.purposes() == ["robots"]
    error = refused.value
    assert error.reason == DenyReason.ROBOTS_UNREACHABLE.value
    assert error.until == NOW + ROBOTS_RETRY_INTERVAL
    assert lifts_on_its_own(error)  # the job waits without consuming an attempt
    disposition = refusal_disposition(error, 1)
    assert (disposition.kind, disposition.code, disposition.retry_at) == (
        "retry",
        "ROBOTS_UNREACHABLE",
        NOW + ROBOTS_RETRY_INTERVAL,
    )
    [(stored, outcome)] = world.store.recorded
    assert stored.availability == RobotsAvailability.UNREACHABLE
    assert outcome.access_state in (AccessState.TRANSIENT_ERROR, AccessState.RATE_LIMITED)
    # Within the retry interval the stored outcome stands: no request at all.
    world.pass_time(600)
    with pytest.raises(RobotsUnavailable):
        await world.fetch(DETAIL, "detail")
    assert world.transport.calls == [ROBOTS_URL] and world.inner.calls == []


async def test_robots_txt_is_asked_again_after_an_outage() -> None:
    transport = Transport(RobotsFetchResponse(503, None, None))
    world = World(transport=transport)
    with pytest.raises(RobotsUnavailable):
        await world.fetch(SEARCH)
    transport.response = RobotsFetchResponse(200, RULES, None)
    world.pass_time(ROBOTS_RETRY_INTERVAL.total_seconds() + 1)
    world.gate.set_state(world.gate.state(SOURCE, HOST).model_copy(update={"next_request_not_before": None}))
    with pytest.raises(BudgetRefused):  # robots.txt read again, then the page waits for the spacing
        await world.fetch(SEARCH)
    assert transport.calls == [ROBOTS_URL, ROBOTS_URL]
    world.pass_time(20)
    assert (await world.fetch(SEARCH)).fetch.access_state == AccessState.OK


@pytest.mark.parametrize("status", [401, 403])
async def test_an_access_denial_on_robots_txt_blocks_without_a_page_request(status: int) -> None:
    world = World(transport=Transport(RobotsFetchResponse(status, b"<html>denied</html>", None)))
    document = await world.fetch(SEARCH)
    assert world.inner.calls == []
    assert document.fetch.access_state == AccessState.ACCESS_BLOCKED
    assert document.fetch.error_code == f"robots_http_{status}" and document.fetch.http_status is None
    # The gate saw the denial: the host is access-blocked until the owner clears it.
    assert world.gate.state(SOURCE, HOST).access_blocked_at is not None
    with pytest.raises(BudgetRefused) as again:  # robots.txt is asked again, and refused by the block
        await world.fetch(SEARCH)
    assert again.value.reason == DenyReason.ACCESS_BLOCKED.value
    assert world.transport.calls == [ROBOTS_URL]


async def test_no_robots_txt_means_no_robots_restriction() -> None:
    world = World(transport=Transport(RobotsFetchResponse(404, b"not found", None)))
    with pytest.raises(BudgetRefused):
        await world.fetch(BLOCKED_SEARCH)
    world.pass_time(20)
    document = await world.fetch(BLOCKED_SEARCH)
    assert document.fetch.access_state == AccessState.OK
    [(_stored, outcome)] = world.store.recorded
    assert outcome.access_state == AccessState.NOT_FOUND and outcome.error_code == "robots_http_404"


async def test_the_robots_crawl_delay_spaces_the_following_requests() -> None:
    rules = b"User-agent: *\nCrawl-delay: 60\nDisallow: /intern/\n"
    world = World(transport=Transport(RobotsFetchResponse(200, rules, None)))
    with pytest.raises(BudgetRefused) as spaced:
        await world.fetch(SEARCH)
    page_request = world.gate.requests[-1]
    assert page_request.purpose == "search" and page_request.crawl_delay == Decimal(60)
    # 60 s (robots.txt), not the source's 20 s, between the robots request and the page.
    assert spaced.value.until == NOW + timedelta(seconds=60)
    world.pass_time(30)
    with pytest.raises(BudgetRefused):
        await world.fetch(SEARCH)
    world.pass_time(30)
    assert (await world.fetch(SEARCH)).fetch.access_state == AccessState.OK


async def test_the_daily_robots_refetch_keeps_the_hosts_crawl_delay() -> None:
    """The first request of a new UTC day fetches robots.txt again; that robots request is spaced
    by the Crawl-delay of the revision it replaces, not only by the source's 20 s."""
    rules = b"User-agent: *\nCrawl-delay: 60\nDisallow: /intern/\n"
    world = World(transport=Transport(RobotsFetchResponse(200, rules, None)))
    world.clock.advance(timedelta(hours=15, minutes=57, seconds=50))  # 23:57:50 UTC
    with pytest.raises(BudgetRefused):  # robots.txt, then the page waits 60 s
        await world.fetch(SEARCH)
    world.pass_time(60)
    assert (await world.fetch(SEARCH)).fetch.access_state == AccessState.OK  # 23:58:50
    world.pass_time(60)
    assert (await world.fetch(DETAIL, "detail")).fetch.access_state == AccessState.OK  # 23:59:50
    last_page = world.clock.now()
    world.pass_time(25)  # 00:00:15: a new UTC day, the revision is stale
    with pytest.raises(BudgetRefused) as spaced:
        await world.fetch(DETAIL, "detail")
    robots_request = world.gate.requests[-1]
    assert robots_request.purpose == "robots" and robots_request.crawl_delay == Decimal(60)
    assert spaced.value.reason == WaitReason.MIN_DELAY.value
    assert spaced.value.until == last_page + timedelta(seconds=60)  # not 20 s after the last page
    assert world.transport.calls == [ROBOTS_URL]  # not requested before the host's delay
    world.pass_time(35)
    with pytest.raises(BudgetRefused):  # robots.txt now, the page 60 s later
        await world.fetch(DETAIL, "detail")
    assert world.transport.calls == [ROBOTS_URL, ROBOTS_URL]
    assert world.gate.requests[-1].purpose == "detail"
    assert world.gate.requests[-1].crawl_delay == Decimal(60)


# robots.txt with rules for our product token only: everything is allowed for `*`.
TOKEN_RULES = (
    b"User-agent: *\nAllow: /\n\n"
    b"User-agent: OtherBot\nCrawl-delay: 90\n\n"
    b"User-agent: SUVDealResearch\nDisallow: /suche?typ=suv\nCrawl-delay: 45\n"
)


async def test_urls_are_evaluated_for_our_product_token() -> None:
    world = World(transport=Transport(RobotsFetchResponse(200, TOKEN_RULES, None)))
    assert world.guard.user_agent == DEFAULT_USER_AGENT
    document = await world.fetch(SEARCH)
    # Allowed for `*`, disallowed for SUVDealResearch: never requested, reported as robots_disallowed.
    assert world.inner.calls == [] and world.purposes() == ["robots"]
    assert document.fetch.access_state == AccessState.POLICY_DENIED
    assert document.fetch.error_code == "robots_disallowed"
    # Our group's Crawl-delay (45 s) spaces the next page, neither `*` (none) nor OtherBot (90 s).
    with pytest.raises(BudgetRefused) as spaced:
        await world.fetch(DETAIL, "detail")
    page_request = world.gate.requests[-1]
    assert page_request.purpose == "detail" and page_request.crawl_delay == Decimal(45)
    assert spaced.value.until == NOW + timedelta(seconds=45)
    world.pass_time(45)
    assert (await world.fetch(DETAIL, "detail")).fetch.access_state == AccessState.OK
    assert world.inner.calls == [(DETAIL, "detail")]


async def test_the_revision_is_recorded_while_the_robots_request_holds_the_host() -> None:
    """A worker waiting for the host must find the new revision once the robots request ends,
    so the revision and the fetch attempt are stored before the gate is released."""
    world = World()
    seen: list[tuple[bool, BudgetDecision]] = []
    record = world.store.record

    async def record_holding_the_host(revision: RobotsRevision, outcome: FetchOutcome) -> bool:
        state = world.gate.state(SOURCE, HOST)
        leased = state.in_flight_until is not None and world.clock.now() < state.in_flight_until
        other = await world.gate.acquire(
            BudgetRequest(source_key=SOURCE, host=HOST, purpose="detail", url=DETAIL)
        )
        seen.append((leased, other))
        return await record(revision, outcome)

    world.store.record = record_holding_the_host  # type: ignore[method-assign]
    with pytest.raises(BudgetRefused):
        await world.fetch(SEARCH)
    [(leased, other)] = seen
    assert leased
    assert isinstance(other, Wait) and other.reason == WaitReason.NAVIGATION_IN_FLIGHT
    assert world.gate.state(SOURCE, HOST).in_flight_until is None  # released after recording


async def test_no_robots_request_without_budget() -> None:
    world = World(budget=RateBudget(min_delay_seconds=20, daily_request_budget=0))
    with pytest.raises(BudgetRefused) as refused:
        await world.fetch(SEARCH)
    assert refused.value.reason == DenyReason.BUDGET_EXHAUSTED.value
    assert world.transport.calls == [] and world.store.recorded == []


async def test_a_redirect_to_a_disallowed_url_drops_the_body() -> None:
    world = World(
        store=Store(revision(fetched_at=NOW - timedelta(minutes=1))),
        inner=Inner(final_url=f"https://{HOST}/fahrzeug/TEST-230"),
    )
    document = await world.fetch(DETAIL, "detail")
    assert document.html is None
    assert document.fetch.access_state == AccessState.POLICY_DENIED
    assert document.fetch.error_code == "robots_disallowed"


async def test_a_changed_robots_txt_is_logged_for_review(caplog: pytest.LogCaptureFixture) -> None:
    world = World(store=Store(changed=True))
    with (
        caplog.at_level(logging.INFO, logger="suv_deals.crawling.robots_guard"),
        pytest.raises(BudgetRefused),
    ):
        await world.fetch(SEARCH)
    [record] = [r for r in caplog.records if r.getMessage() == "robots.txt fetched"]
    assert record.levelno == logging.WARNING and record.__dict__["robots_changed"] is True


async def test_a_robots_guard_is_skipped_only_for_robots_purposes() -> None:
    world = World()
    document = await world.fetch(ROBOTS_URL, "robots")
    assert document.fetch.access_state == AccessState.OK and world.transport.calls == []


# ---------------------------------------------------------------------------------- outcomes


@pytest.mark.parametrize(
    ("availability", "status", "kind", "state", "code"),
    [
        (RobotsAvailability.AVAILABLE, 200, None, AccessState.OK, None),
        (RobotsAvailability.UNAVAILABLE, 404, "http_404", AccessState.NOT_FOUND, "robots_http_404"),
        (RobotsAvailability.UNAVAILABLE, 410, "http_410", AccessState.NOT_FOUND, "robots_http_410"),
        (RobotsAvailability.UNAVAILABLE, 403, "http_403", AccessState.ACCESS_BLOCKED, "robots_http_403"),
        (
            RobotsAvailability.UNAVAILABLE,
            301,
            "too_many_redirects",
            AccessState.UNEXPECTED_CONTENT,
            "robots_too_many_redirects",
        ),
        (RobotsAvailability.UNREACHABLE, 429, "http_429", AccessState.RATE_LIMITED, "robots_http_429"),
        (RobotsAvailability.UNREACHABLE, 503, "http_503", AccessState.TRANSIENT_ERROR, "robots_http_503"),
        (RobotsAvailability.UNREACHABLE, None, "timeout", AccessState.TRANSIENT_ERROR, "robots_timeout"),
        (
            RobotsAvailability.UNREACHABLE,
            None,
            "dns_resolution_failed",
            AccessState.TRANSIENT_ERROR,
            "robots_dns_resolution_failed",
        ),
        (
            RobotsAvailability.UNREACHABLE,
            None,
            "dns_non_public",
            AccessState.POLICY_DENIED,
            "robots_dns_non_public",
        ),
        (
            RobotsAvailability.UNREACHABLE,
            301,
            "redirect_off_host",
            AccessState.UNEXPECTED_CONTENT,
            "robots_redirect_off_host",
        ),
    ],
)
def test_robots_requests_are_classified_for_the_gate(
    availability: RobotsAvailability,
    status: int | None,
    kind: str | None,
    state: AccessState,
    code: str | None,
) -> None:
    rev = revision(availability, status=status).model_copy(update={"error_kind": kind})
    assert robots_access(rev) == (state, code)


def test_the_robots_outcome_keeps_retry_after_and_every_hop() -> None:
    rev = revision(RobotsAvailability.UNREACHABLE, status=429).model_copy(update={"redirect_count": 1})
    hops = [
        ("http://x", RobotsFetchResponse(301, None, None, ROBOTS_URL)),
        (ROBOTS_URL, RobotsFetchResponse(429, b"slow down", None, None, "120")),
    ]
    outcome = robots_fetch_outcome(rev, hops, elapsed_ms=5, crawler_version="test")
    assert outcome.retry_after_seconds == 120 and outcome.bytes == len(b"slow down")
    assert outcome.final_url == ROBOTS_URL and outcome.redirect_count == 1 and not outcome.success


# ---------------------------------------------------------------------------------- transports


def safe_http(handler: Callable[[httpx.Request], Awaitable[httpx.Response]]) -> SafeHttpClient:
    return SafeHttpClient(
        resolver=public_dns, transport=httpx.MockTransport(handler), user_agent=DEFAULT_USER_AGENT
    )


async def test_the_direct_transport_sends_the_honest_user_agent_and_reads_the_body() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=RULES, headers={"content-type": "text/plain"})

    transport = SafeHttpRobotsTransport(safe_http(handler))
    response = await transport.get(ROBOTS_URL, source_key=SOURCE)
    assert (response.status, response.body, response.error_kind) == (200, RULES, None)
    [request] = seen
    assert request.headers["user-agent"] == DEFAULT_USER_AGENT
    assert request.headers["host"] == HOST and request.url.path == "/robots.txt"
    assert not any(h in request.headers for h in ("cookie", "authorization", "proxy-authorization"))


async def test_the_runtime_robots_transport_is_the_honest_direct_client() -> None:
    """`workers.runtime.robots_transport` builds the only transport real sources use: the crawler's
    User-Agent (not the SafeHttp default), identity encoding, no credentials, bodies capped."""
    seen: list[httpx.Request] = []
    huge = b"# x\n" * (ROBOTS_MAX_BYTES // 2)

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=huge if len(seen) > 1 else RULES)

    transport = robots_transport(public_dns, transport=httpx.MockTransport(handler))
    try:
        assert transport.label == DIRECT_TRANSPORT_LABEL
        response = await transport.get(ROBOTS_URL, source_key=SOURCE)
        assert (response.status, response.body, response.error_kind) == (200, RULES, None)
        capped = await transport.get(ROBOTS_URL, source_key=SOURCE)
        assert capped.error_kind == TRUNCATED_ERROR_KIND and len(capped.body or b"") == ROBOTS_MAX_BYTES
    finally:
        await transport.aclose()
    for request in seen:
        assert request.headers["user-agent"] == DEFAULT_USER_AGENT
        assert request.headers["user-agent"] != SAFE_HTTP_DEFAULT_USER_AGENT
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["host"] == HOST and request.url.path == "/robots.txt"
        assert not any(h in request.headers for h in ("cookie", "authorization", "proxy-authorization"))


async def test_the_direct_transport_hands_redirects_to_the_robots_module() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": f"https://{HOST}/robots-new.txt"})

    response = await SafeHttpRobotsTransport(safe_http(handler)).get(ROBOTS_URL, source_key=SOURCE)
    assert response == RobotsFetchResponse(301, None, None, f"https://{HOST}/robots-new.txt")


async def test_the_direct_transport_fails_closed() -> None:
    async def gzip_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=gzip.compress(RULES), headers={"content-encoding": "gzip"})

    response = await SafeHttpRobotsTransport(safe_http(gzip_handler)).get(ROBOTS_URL, source_key=SOURCE)
    assert (
        response.status == 200
        and response.body is None
        and response.error_kind == "unsupported_content_encoding"
    )

    async def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    response = await SafeHttpRobotsTransport(safe_http(broken)).get(ROBOTS_URL, source_key=SOURCE)
    assert (
        response.status is None
        and response.body is None
        and (response.error_kind or "").startswith("network_")
    )

    async def huge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"# x\n" * (ROBOTS_MAX_BYTES // 2))

    response = await SafeHttpRobotsTransport(safe_http(huge)).get(ROBOTS_URL, source_key=SOURCE)
    assert response.error_kind == TRUNCATED_ERROR_KIND and len(response.body or b"") == ROBOTS_MAX_BYTES


def _fixture_dir(tmp_path: Path, routes: dict[str, Any]) -> Path:
    directory = tmp_path / SOURCE
    directory.mkdir()
    (directory / "robots.txt").write_bytes(b"# SYNTHETIC FIXTURE\n" + RULES)
    manifest = {
        "source_key": SOURCE,
        "designation": "synthetic",
        "created": "2026-10-17",
        "parser_version": "test",
        "description": "SYNTHETIC robots fixture",
        "files": {"robots.txt": "synthetic robots.txt"},
        "routes": routes,
    }
    (directory / "MANIFEST.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return directory


async def test_fixture_sources_read_their_saved_robots_route(tmp_path: Path) -> None:
    directory = _fixture_dir(
        tmp_path,
        {
            ROBOTS_URL: {"file": "robots.txt", "content_type": "text/plain", "description": "robots"},
            f"https://{HOST}/old-robots.txt": {
                "file": "robots.txt",
                "final_url": ROBOTS_URL,
                "description": "redirected robots",
            },
        },
    )
    client = FixtureCrawlClient([directory], clock=FrozenClock(NOW))
    transport = CrawlClientRobotsTransport(client)
    response = await transport.get(ROBOTS_URL, source_key=SOURCE)
    assert response.status == 200 and response.body is not None and RULES in response.body
    assert [(r.url, r.purpose) for r in client.requests] == [(ROBOTS_URL, "robots")]
    redirected = await transport.get(f"https://{HOST}/old-robots.txt", source_key=SOURCE)
    assert redirected == RobotsFetchResponse(301, None, None, ROBOTS_URL)
    missing = await CrawlClientRobotsTransport(FixtureCrawlClient([directory])).get(
        "https://other.example/robots.txt", source_key=SOURCE
    )
    assert missing.status == 404  # no saved route: no robots.txt (no restriction), never a network call


async def test_the_guard_works_offline_over_a_fixture_set(tmp_path: Path) -> None:
    directory = _fixture_dir(
        tmp_path, {ROBOTS_URL: {"file": "robots.txt", "content_type": "text/plain", "description": "robots"}}
    )
    fixture = FixtureCrawlClient([directory], clock=FrozenClock(NOW))
    world = World()
    world.guard = RobotsGuard(
        world.store, CrawlClientRobotsTransport(fixture), resolver=public_dns, clock=world.clock
    )
    world.client = PolicyEnforcingCrawlClient(
        world.inner, {SOURCE: POLICY}, world.gate, robots=world.guard, resolver=public_dns, clock=world.clock
    )
    document = await world.fetch(BLOCKED_SEARCH)
    assert document.fetch.error_code == "robots_disallowed" and world.inner.calls == []
    assert [(r.url, r.purpose) for r in fixture.requests] == [(ROBOTS_URL, "robots")]
    assert world.purposes() == ["robots"]
