"""robots.txt in the live worker path: one current revision per host before any page request
(spec sections 5, 8, 24; RFC 9309).

`PolicyEnforcingCrawlClient` asks the `RobotsGuard` of its crawl session about every search and
detail URL BEFORE the page's own budget request. The guard

1. returns the host's current robots revision: the one it already holds, else the latest stored
   one (``ops.robots_revisions`` through a `RobotsStore`) while it is fresh, else
2. fetches robots.txt as a request of its own: `robots.robots_url_for` (the URL policy's
   ``robots`` purpose allows exactly ``/robots.txt``), the budget gate (``purpose: robots``:
   counted in the host's daily budget and spaced like every other request, including the
   Crawl-delay of the revision it replaces; a refusal raises `BudgetRefused` and nothing is
   fetched), then `robots.fetch_robots` through a
   `RobotsTransport` (redirects only to the same host, at most five hops, public DNS checked
   before every hop, fail closed), and records the revision and the fetch attempt
   (``purpose: robots``) before the gate is released;
3. evaluates the URL with `robots.evaluate` for our product token.

Freshness (RFC 9309 section 2.4 allows a cached copy for at most 24 hours): a revision is reused
only within the UTC day it was fetched in, so every host's robots.txt is fetched again at the
first request of each UTC day (the first discovery run of the day) and never used for more than
24 hours. Within the day:

- an ``available`` revision, or an ``unavailable`` one (404/410 and other 4xx: no robots
  restriction), is reused until the end of the day;
- an ``unreachable`` revision (5xx, 429, network failure, an answer that could not be read
  completely; a complete disallow) is reused for `ROBOTS_RETRY_INTERVAL` (30 minutes), so a
  robots outage costs at most two robots requests an hour per host while every page waits;
- a 401/403/407 answer (an access denial) is never reused: the next request asks again, and the
  budget gate's access block (set by that answer) stops it until the owner clears the block.

Transports: the live worker reads robots.txt DIRECTLY (`SafeHttpRobotsTransport`: the netguard
`SafeHttpClient`, which connects only to the validated public address, verifies TLS, ignores
environment proxies and follows no redirect itself), with the same honest User-Agent as the
crawler and nothing else: no stealth, no proxy, no CAPTCHA handling. Crawl4AI renders pages in a
browser, not plain files (its own ``check_robots_txt: true`` stays on as a second line). Fixture
sources read the ``/robots.txt`` route of their saved fixture set (`CrawlClientRobotsTransport`
over the offline `FixtureCrawlClient`), so the same guard runs unchanged offline.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable

import anyio

from suv_deals.adapters.base import CrawlClient, FetchOutcome
from suv_deals.adapters.crawl4ai_client import DEFAULT_USER_AGENT
from suv_deals.clock import Clock, SystemClock, ensure_utc
from suv_deals.crawling.policy_client import BudgetGate, BudgetRefused, BudgetRequest, RobotsCheck
from suv_deals.crawling.rate_limits import Allow, next_utc_midnight, parse_retry_after
from suv_deals.crawling.robots import (
    ACCESS_DENIED_STATUSES,
    DEFAULT_FETCH_TIMEOUT_S,
    ROBOTS_MAX_AGE,
    ROBOTS_MAX_BYTES,
    TRUNCATED_ERROR_KIND,
    RobotsAvailability,
    RobotsFetchResponse,
    RobotsRevision,
    crawl_delay,
    evaluate,
    fetch_robots,
    robots_url_for,
)
from suv_deals.crawling.url_policy import DenialReason, SourceUrlPolicy
from suv_deals.domain.enums import AccessState
from suv_deals.integrations.safe_http import SafeHttp, SafeHttpError, SafeHttpFailure
from suv_deals.netguard import Resolver

logger = logging.getLogger(__name__)

#: How long an UNREACHABLE revision (complete disallow) is reused before robots.txt is asked again.
ROBOTS_RETRY_INTERVAL: Final = timedelta(minutes=30)
#: A revision fetched up to this much "in the future" (clock skew between workers) is still used.
_CLOCK_SKEW: Final = timedelta(minutes=5)
_MAX_RETRY_AFTER: Final = 30 * 86_400
_TRANSIENT_KINDS: Final = frozenset(
    {"timeout", "fetch_exception", "network", DenialReason.DNS_RESOLUTION_FAILED.value}
)
_POLICY_KINDS: Final = frozenset(r.value for r in DenialReason) - {DenialReason.DNS_RESOLUTION_FAILED.value}
DIRECT_TRANSPORT_LABEL: Final = "robots-direct@1"
_ACCEPT: Final = "text/plain, */*;q=0.1"


# ---------------------------------------------------------------------------
# Freshness


def revision_settled(revision: RobotsRevision) -> bool:
    """Whether a revision may be reused for the rest of its UTC day (see the module docstring)."""
    if revision.availability == RobotsAvailability.AVAILABLE:
        return True
    return (
        revision.availability == RobotsAvailability.UNAVAILABLE
        and revision.http_status not in ACCESS_DENIED_STATUSES
    )


def reusable_until(revision: RobotsRevision) -> datetime:
    """The end of a revision's freshness window: never past its UTC day, never past 24 hours."""
    fetched = revision.fetched_at
    if revision_settled(revision):
        window = ROBOTS_MAX_AGE
    elif revision.availability == RobotsAvailability.UNREACHABLE:
        window = ROBOTS_RETRY_INTERVAL
    else:  # an access denial: ask again at the next request
        window = timedelta(0)
    return min(next_utc_midnight(fetched), fetched + window)


def revision_is_fresh(revision: RobotsRevision, now: datetime) -> bool:
    """Whether ``revision`` may still be used at ``now`` instead of fetching robots.txt again."""
    now = ensure_utc(now)
    return revision.fetched_at <= now + _CLOCK_SKEW and now < reusable_until(revision)


# ---------------------------------------------------------------------------
# The fetch outcome of a robots request (ops.fetch_attempts, budget gate)


def robots_access(revision: RobotsRevision) -> tuple[AccessState, str | None]:
    """How the budget gate and the fetch record see one robots.txt fetch (state, error code)."""
    status = revision.http_status
    kind = revision.error_kind
    if revision.availability == RobotsAvailability.AVAILABLE:
        return AccessState.OK, None
    if revision.availability == RobotsAvailability.UNAVAILABLE:
        if status in ACCESS_DENIED_STATUSES:
            return AccessState.ACCESS_BLOCKED, f"robots_http_{status}"
        if status in (404, 410):
            return AccessState.NOT_FOUND, f"robots_http_{status}"
        return AccessState.UNEXPECTED_CONTENT, f"robots_{kind}" if kind else f"robots_http_{status}"
    if status == 429:
        return AccessState.RATE_LIMITED, "robots_http_429"
    if status is not None and 500 <= status <= 599:
        return AccessState.TRANSIENT_ERROR, f"robots_http_{status}"
    if kind is not None and (kind in _TRANSIENT_KINDS or kind.startswith("network")):
        return AccessState.TRANSIENT_ERROR, f"robots_{kind}"
    if kind in _POLICY_KINDS:
        return AccessState.POLICY_DENIED, f"robots_{kind}"
    if status is None:
        return AccessState.TRANSIENT_ERROR, f"robots_{kind or 'no_response'}"
    return AccessState.UNEXPECTED_CONTENT, f"robots_{kind or f'http_{status}'}"


def robots_fetch_outcome(
    revision: RobotsRevision,
    hops: Sequence[tuple[str, RobotsFetchResponse]],
    *,
    elapsed_ms: int | None = None,
    crawler_version: str | None = None,
) -> FetchOutcome:
    """The redacted `FetchOutcome` of one robots.txt fetch (every hop of a same-host redirect)."""
    access, code = robots_access(revision)
    last_url, last = hops[-1] if hops else (revision.robots_url, None)
    retry_after = None
    if last is not None and last.retry_after is not None:
        parsed = parse_retry_after(last.retry_after, revision.fetched_at)
        retry_after = None if parsed is None else min(parsed, _MAX_RETRY_AFTER)
    status = revision.http_status
    return FetchOutcome(
        requested_url=revision.robots_url,
        final_url=last_url,
        http_status=status if status is not None and 100 <= status <= 599 else None,
        success=access == AccessState.OK,
        access_state=access,
        error_code=None if code is None else code[:80],
        elapsed_ms=None if elapsed_ms is None else max(elapsed_ms, 0),
        bytes=sum(len(response.body or b"") for _url, response in hops),
        redirect_count=revision.redirect_count,
        retry_after_seconds=retry_after,
        crawler_version=crawler_version,
        fetched_at=revision.fetched_at,
    )


# ---------------------------------------------------------------------------
# Transports


@runtime_checkable
class RobotsTransport(Protocol):
    """Fetches ONE robots.txt URL without following redirects (the robots module follows them)."""

    label: str

    async def get(self, url: str, *, source_key: str) -> RobotsFetchResponse: ...


class SafeHttpRobotsTransport:
    """The live worker's robots.txt fetcher: the netguard `SafeHttpClient` (validated public IP
    pinned per connection, TLS verified, environment proxies ignored, no redirect followed, at most
    `robots.ROBOTS_MAX_BYTES` read, identity encoding only) with the honest crawler User-Agent
    the client was built with."""

    label = DIRECT_TRANSPORT_LABEL

    def __init__(
        self,
        http: SafeHttp,
        *,
        timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
        closer: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._http = http
        self._timeout_s = timeout_s
        self._closer = closer

    async def aclose(self) -> None:
        """Close the HTTP client when this transport owns it (``closer``)."""
        if self._closer is not None:
            closer, self._closer = self._closer, None
            await closer()

    async def get(self, url: str, *, source_key: str) -> RobotsFetchResponse:
        del source_key
        try:
            response = await self._http.get(
                url,
                headers={"Accept": _ACCEPT},
                timeout_s=self._timeout_s,
                max_response_bytes=ROBOTS_MAX_BYTES,
            )
        except SafeHttpError as exc:
            if exc.failure == SafeHttpFailure.REDIRECT_REFUSED and exc.status_code is not None:
                return RobotsFetchResponse(exc.status_code, None, None, exc.location)
            # A status seen before the failure is kept (a 2xx without its complete body is
            # unreachable in the robots module: rules we could not read never mean "allowed").
            return RobotsFetchResponse(exc.status_code, None, f"network_{exc.failure.value}")
        encoding = (response.headers.get("content-encoding") or "identity").strip().lower()
        if encoding != "identity":
            # We asked for identity; compressed bytes would parse as "no rules" (fail closed).
            return RobotsFetchResponse(response.status_code, None, "unsupported_content_encoding")
        return RobotsFetchResponse(
            response.status_code,
            response.body,
            TRUNCATED_ERROR_KIND if response.truncated else None,
            None,
            response.headers.get("retry-after"),
        )


class CrawlClientRobotsTransport:
    """robots.txt through an offline `CrawlClient` (the `FixtureCrawlClient` of fixture sources:
    the ``/robots.txt`` route of the saved fixture set; a missing route answers 404)."""

    def __init__(self, client: CrawlClient, *, label: str = "fixture") -> None:
        self._client = client
        self.label = label

    async def get(self, url: str, *, source_key: str) -> RobotsFetchResponse:
        document = await self._client.fetch(url, purpose="robots", source_key=source_key)
        fetch = document.fetch
        status = fetch.http_status
        if status is None:
            return RobotsFetchResponse(None, None, fetch.error_code or "no_response")
        final = document.final_url
        if final is not None and final != url:
            # The saved route stands for a redirect: hand the hop to the robots module.
            return RobotsFetchResponse(301, None, None, final)
        body = document.html.encode("utf-8") if document.html is not None else b""
        return RobotsFetchResponse(status, body, None, None, fetch.response_headers.get("retry-after"))


# ---------------------------------------------------------------------------
# Store and guard


@runtime_checkable
class RobotsStore(Protocol):
    """Where revisions live (``ops.robots_revisions``; `persistence.robots_store.DbRobotsStore`)."""

    async def latest(self, host: str) -> RobotsRevision | None: ...

    async def record(self, revision: RobotsRevision, outcome: FetchOutcome) -> bool:
        """Store the revision and the fetch attempt; True when robots.txt changed (audited)."""
        ...


class RobotsGuard:
    """`policy_client.RobotsChecker` of one crawl session (see the module docstring)."""

    def __init__(
        self,
        store: RobotsStore,
        transport: RobotsTransport,
        *,
        resolver: Resolver | None,
        clock: Clock | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        fetch_timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
    ) -> None:
        self._store = store
        self._transport = transport
        self._resolver = resolver
        self._clock: Clock = clock or SystemClock()
        self._user_agent = user_agent
        self._fetch_timeout_s = fetch_timeout_s
        self._cache: dict[str, RobotsRevision] = {}

    @property
    def user_agent(self) -> str:
        return self._user_agent

    @property
    def transport(self) -> RobotsTransport:
        return self._transport

    async def check(
        self, *, source_key: str, policy: SourceUrlPolicy, host: str, url: str, gate: BudgetGate
    ) -> RobotsCheck:
        revision, fetched = await self.current(source_key=source_key, policy=policy, host=host, gate=gate)
        now = ensure_utc(self._clock.now())
        return RobotsCheck(
            revision=revision,
            verdict=evaluate(revision, url, self._user_agent, now=now),
            user_agent=self._user_agent,
            fetched=fetched,
            crawl_delay=crawl_delay(revision, self._user_agent),
            reusable_until=reusable_until(revision),
        )

    async def current(
        self, *, source_key: str, policy: SourceUrlPolicy, host: str, gate: BudgetGate
    ) -> tuple[RobotsRevision, FetchOutcome | None]:
        """The host's current revision and, when it had to be fetched now, that fetch's outcome."""
        now = ensure_utc(self._clock.now())
        cached = self._cache.get(host)
        if cached is not None and revision_is_fresh(cached, now):
            return cached, None
        # Another worker may have fetched it since (re-read after every budget wait, too).
        stored = await self._store.latest(host)
        if stored is not None and revision_is_fresh(stored, now):
            self._cache[host] = stored
            return stored, None
        # The revision being replaced (stale, but the newest known) still states the host's
        # Crawl-delay for our token: the robots request is spaced by it like a page would be
        # (the first request of a UTC day may follow the previous day's last page closely).
        known = [r for r in (cached, stored) if r is not None]
        previous = max(known, key=lambda r: r.fetched_at) if known else None
        delay = None if previous is None else crawl_delay(previous, self._user_agent)
        return await self._fetch(source_key, policy, host, gate, delay)

    async def _fetch(
        self,
        source_key: str,
        policy: SourceUrlPolicy,
        host: str,
        gate: BudgetGate,
        delay: Decimal | None = None,
    ) -> tuple[RobotsRevision, FetchOutcome]:
        robots_url = robots_url_for(policy, host)  # PolicyDenied: never requested
        request = BudgetRequest(
            source_key=source_key, host=host, purpose="robots", url=robots_url, crawl_delay=delay
        )
        decision = await gate.acquire(request)
        if not isinstance(decision, Allow):
            raise BudgetRefused(decision, now=ensure_utc(self._clock.now()))
        outcome: FetchOutcome | None = None
        try:
            hops: list[tuple[str, RobotsFetchResponse]] = []

            async def fetcher(target: str) -> RobotsFetchResponse:
                response = await self._transport.get(target, source_key=source_key)
                hops.append((target, response))
                return response

            started = time.monotonic()
            revision = await fetch_robots(
                policy,
                host,
                fetcher,
                resolver=self._resolver,
                clock=self._clock,
                fetch_timeout_s=self._fetch_timeout_s,
            )
            outcome = robots_fetch_outcome(
                revision,
                hops,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                crawler_version=self._transport.label,
            )
            # Recorded while this request still holds the host's navigation lease, so a worker
            # waiting for the host finds the new revision instead of fetching robots.txt again.
            changed = await self._store.record(revision, outcome)
        finally:
            with anyio.CancelScope(shield=True):
                await gate.release(request, outcome)
        log = logger.warning if changed else logger.info
        log(
            "robots.txt fetched",
            extra={
                "host": revision.host,
                "http_status": revision.http_status,
                "availability": revision.availability.value,
                "robots_changed": changed,
            },
        )
        self._cache[revision.host] = revision
        return revision, outcome


__all__ = [
    "ACCESS_DENIED_STATUSES",
    "DIRECT_TRANSPORT_LABEL",
    "ROBOTS_RETRY_INTERVAL",
    "CrawlClientRobotsTransport",
    "RobotsGuard",
    "RobotsStore",
    "RobotsTransport",
    "SafeHttpRobotsTransport",
    "reusable_until",
    "revision_is_fresh",
    "revision_settled",
    "robots_access",
    "robots_fetch_outcome",
]
