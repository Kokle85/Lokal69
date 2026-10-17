"""Policy-enforcing `CrawlClient` wrapper (spec sections 8, 9, 24).

Every fetch passes, in order:

1. Source lookup: an unknown `source_key` is denied.
2. `SourceUrlPolicy.check()` for the purpose (structural SSRF + host/path allow-lists).
3. robots.txt (`RobotsChecker`, the worker's `crawling.robots_guard.RobotsGuard`): the host's
   current robots revision (reused within its freshness window, otherwise fetched first as a
   request of its own through the same budget gate) and `robots.evaluate()` of the URL for our
   product token. A disallowed URL is never requested: it becomes a POLICY_DENIED document
   (``robots_disallowed``; reported, not retried). An unreachable robots.txt is a complete
   disallow: `RobotsUnavailable` (a `BudgetRefused`, nothing fetched; the job waits until the
   revision may be fetched again). A 401/403/407 answer to the robots request itself is an
   access denial: the page becomes an ACCESS_BLOCKED document without being requested. The
   robots crawl-delay travels with the page's budget request (`BudgetRequest.crawl_delay`).
4. The budget gate (`rate_limits` decision): no request is made without budget.
   A `Wait`/`Deny` raises a typed `BudgetRefused`; nothing is fetched.
5. Fetch-time DNS check (`resolve_check`): every answer must be a public address.
6. The inner client fetch.
7. `validate_redirect()` on the final URL: cross-host redirects, scheme downgrades and
   off-policy final paths are converted to POLICY_DENIED and the body is discarded; a
   same-host final URL that robots.txt disallows is discarded the same way.
   Exception: when the target itself blocked, throttled or failed the request
   (ACCESS_BLOCKED, RATE_LIMITED, TRANSIENT_ERROR, e.g. a redirect to a CAPTCHA host),
   that classification is kept (body still discarded) so the budget gate pauses the
   route, honours Retry-After or backs off instead of seeing "our own policy refusal".
8. The budget gate is released with the outcome, even on cancellation (shielded).

Policy violations become a `RawDocument` whose `FetchOutcome.access_state` is
POLICY_DENIED (no HTML, no exception). Typed `AppError`s from the inner client
(e.g. crawler authentication failure) and `BudgetRefused` propagate unchanged.
URLs are redacted (no userinfo, bounded) before they are stored in an outcome.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable

import anyio
from pydantic import BaseModel, ConfigDict, Field

from suv_deals.adapters.base import CrawlClient, FetchOutcome, FetchPurpose, RawDocument
from suv_deals.clock import Clock, SystemClock, ensure_utc
from suv_deals.crawling.rate_limits import (
    DEFAULT_BACKOFF,
    Allow,
    BackoffPolicy,
    BudgetDecision,
    DailyUsage,
    Deny,
    DenyReason,
    HostBudgetState,
    OutcomeResult,
    RunUsage,
    abandon_request,
    decide,
    record_outcome,
    start_request,
)
from suv_deals.crawling.robots import RobotsAvailability, RobotsBasis, RobotsRevision, RobotsVerdict, evaluate
from suv_deals.crawling.url_policy import (
    DEFAULT_DNS_TIMEOUT_SECONDS,
    DenialReason,
    PolicyDenied,
    SourceUrlPolicy,
    redact_url,
)
from suv_deals.domain.enums import AccessState
from suv_deals.domain.sources import RateBudget
from suv_deals.errors import AppError, ErrorCode
from suv_deals.netguard import Resolver, system_resolver


class BudgetRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_key: str
    host: str
    purpose: FetchPurpose
    url: str
    # The host's robots.txt Crawl-delay (untrusted, capped by `rate_limits`): the gate spaces
    # this request by the larger of it and the source's ``min_delay_seconds``.
    crawl_delay: Decimal | None = Field(default=None, ge=0)


@runtime_checkable
class BudgetGate(Protocol):
    """Reserve budget before a request and report its outcome afterwards.

    `acquire` must be atomic per host (database row lock or in-process lock) and,
    when it returns `Allow`, must already have taken the navigation lease.
    `release` receives `None` when no outcome exists (cancellation, typed error).
    """

    async def acquire(self, request: BudgetRequest) -> BudgetDecision: ...

    async def release(self, request: BudgetRequest, outcome: FetchOutcome | None) -> None: ...


class BudgetRefused(AppError):
    """No budget for this request now; nothing was fetched."""

    def __init__(self, decision: BudgetDecision, *, now: datetime) -> None:
        until: datetime | None
        if isinstance(decision, Deny):
            reason = decision.reason.value
            until = decision.until
        elif isinstance(decision, Allow):  # pragma: no cover - never constructed for Allow
            raise ValueError("Allow is not a refusal")
        else:
            reason = decision.reason.value
            until = decision.until
        access_blocked = isinstance(decision, Deny) and decision.reason == DenyReason.ACCESS_BLOCKED
        retry_after = None if until is None else max(0, int((until - ensure_utc(now)).total_seconds()) + 1)
        super().__init__(
            ErrorCode.ACCESS_BLOCKED if access_blocked else ErrorCode.RATE_LIMITED,
            f"Request budget unavailable ({decision.kind}: {reason})",
            retryable=not access_blocked and until is not None,
            retry_after_seconds=retry_after,
            details={"decision": decision.kind, "reason": reason},
        )
        self.decision = decision
        self.reason = reason
        self.until = until


class RobotsUnavailable(BudgetRefused):
    """The host's robots.txt could not be read (5xx, 429, network, unusable answer): a complete
    disallow (RFC 9309 2.3.1.3, `robots.RobotsAvailability.UNREACHABLE`). Nothing was fetched for
    the page; ``until`` is when the revision may be fetched again. Handled like any budget
    refusal: the job waits until then without consuming an attempt."""

    def __init__(self, *, host: str, until: datetime, now: datetime, detail: str | None = None) -> None:
        super().__init__(Deny(reason=DenyReason.ROBOTS_UNREACHABLE, until=ensure_utc(until)), now=now)
        self.message = "robots.txt could not be read; nothing is requested from the host until it can"
        self.host = host
        self.detail = detail


class RobotsCheck(BaseModel):
    """What the robots guard decided for one URL (see `RobotsChecker`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: RobotsRevision
    verdict: RobotsVerdict
    #: The User-Agent whose product token was evaluated (redirect targets are evaluated for it too).
    user_agent: str
    #: The robots.txt request made for this check (None: a stored or cached revision was reused).
    fetched: FetchOutcome | None = None
    crawl_delay: Decimal | None = None
    #: Until when the revision may be reused (the guard's freshness window).
    reusable_until: datetime


@runtime_checkable
class RobotsChecker(Protocol):
    """The worker's robots.txt guard (`crawling.robots_guard.RobotsGuard`).

    `check` returns the host's current revision (reused within its freshness window, otherwise
    fetched through ``gate`` as a ``robots`` request of its own and recorded) and the
    `robots.evaluate` verdict for ``url``. A budget refusal of the robots request propagates as
    `BudgetRefused`; a robots URL outside the policy raises `PolicyDenied`.
    """

    async def check(
        self, *, source_key: str, policy: SourceUrlPolicy, host: str, url: str, gate: BudgetGate
    ) -> RobotsCheck: ...


_ROBOTS_CODES: Final[dict[RobotsBasis, str]] = {
    RobotsBasis.DISALLOWED_BY_ROBOTS: "robots_disallowed",
    RobotsBasis.ROBOTS_STALE: "robots_stale",
    RobotsBasis.HOST_MISMATCH: "robots_host_mismatch",
}


def robots_refused_document(
    url: str,
    *,
    code: str,
    message: str,
    fetched_at: datetime,
    access_state: AccessState = AccessState.POLICY_DENIED,
    base: FetchOutcome | None = None,
) -> RawDocument:
    """A RawDocument for a URL robots.txt refused: no body; the page itself was not requested
    (``base`` only for a redirect target the crawler had already reached, whose body is dropped)."""
    safe_url = redact_url(url)
    outcome = FetchOutcome(
        requested_url=safe_url,
        final_url=redact_url(base.final_url) if base is not None and base.final_url else None,
        http_status=base.http_status if base is not None else None,
        success=False,
        access_state=access_state,
        error_code=code[:80],
        error_message=message[:500],
        elapsed_ms=base.elapsed_ms if base is not None else None,
        bytes=base.bytes if base is not None else 0,
        redirect_count=base.redirect_count if base is not None else 0,
        crawler_version=base.crawler_version if base is not None else None,
        fetched_at=base.fetched_at if base is not None else ensure_utc(fetched_at),
    )
    return RawDocument(
        url=safe_url, final_url=outcome.final_url, fetched_at=outcome.fetched_at, fetch=outcome
    )


def robots_refusal(url: str, check: RobotsCheck, *, now: datetime) -> RawDocument | None:
    """The document for a URL robots.txt does not let us request (None: request it).

    Raises `RobotsUnavailable` for an unreachable robots.txt (complete disallow, retried later).
    """
    revision = check.revision
    fetched = check.fetched
    if fetched is not None and fetched.access_state == AccessState.ACCESS_BLOCKED:
        status = revision.http_status
        return robots_refused_document(
            url,
            code=f"robots_http_{status}" if status is not None else "robots_access_blocked",
            message=f"robots.txt answered HTTP {status} (access denied); the page was not requested",
            fetched_at=now,
            access_state=AccessState.ACCESS_BLOCKED,
        )
    verdict = check.verdict
    if verdict.allowed:
        return None
    if (
        verdict.basis == RobotsBasis.ROBOTS_UNREACHABLE
        or revision.availability == RobotsAvailability.UNREACHABLE
    ):
        raise RobotsUnavailable(
            host=revision.host, until=check.reusable_until, now=now, detail=revision.error_kind
        )
    rule = f" ({verdict.matched_rule})" if verdict.matched_rule else ""
    return robots_refused_document(
        url,
        code=_ROBOTS_CODES.get(verdict.basis, "robots_disallowed"),
        message=f"robots.txt disallows this URL for our user agent{rule}; it was not requested",
        fetched_at=now,
    )


def robots_refused_redirect(
    url: str, final_url: str, check: RobotsCheck, document: RawDocument, *, now: datetime
) -> RawDocument | None:
    """A redirect target that robots.txt disallows (None: it may be kept). The body is dropped.

    The crawler followed the redirect itself, so this can only discard what it brought back. As
    for a refused URL-policy redirect, a block, throttle or failure reported by the target is
    kept so the budget gate still pauses, honours Retry-After or backs off.
    """
    verdict = evaluate(check.revision, final_url, check.user_agent, now=now)
    if verdict.allowed:
        return None
    inner = document.fetch
    note = "final URL disallowed by robots.txt"
    if inner.access_state in _KEEP_ON_REFUSED_REDIRECT:
        message = f"{inner.error_message}; {note}" if inner.error_message else note
        outcome = inner.model_copy(
            update={
                "requested_url": redact_url(inner.requested_url),
                "final_url": redact_url(inner.final_url) if inner.final_url else None,
                "success": False,
                "error_message": message[:500],
            }
        )
        return RawDocument(
            url=redact_url(url), final_url=outcome.final_url, fetched_at=outcome.fetched_at, fetch=outcome
        )
    return robots_refused_document(
        url,
        code=_ROBOTS_CODES.get(verdict.basis, "robots_disallowed"),
        message=f"the request was redirected to a URL robots.txt disallows ({note}); the body was dropped",
        fetched_at=now,
        base=inner,
    )


def policy_denied_document(
    url: str,
    reason: DenialReason,
    *,
    fetched_at: datetime,
    base: FetchOutcome | None = None,
    access_state: AccessState = AccessState.POLICY_DENIED,
) -> RawDocument:
    """A RawDocument for a refused URL: no body, redacted URLs, typed error code."""
    safe_url = redact_url(url)
    outcome = FetchOutcome(
        requested_url=safe_url,
        final_url=redact_url(base.final_url) if base is not None and base.final_url else None,
        http_status=base.http_status if base is not None else None,
        success=False,
        access_state=access_state,
        error_code=f"policy_{reason.value}",
        error_message=f"URL policy denied the request: {reason.value}",
        elapsed_ms=base.elapsed_ms if base is not None else None,
        bytes=base.bytes if base is not None else 0,
        redirect_count=base.redirect_count if base is not None else 0,
        crawler_version=base.crawler_version if base is not None else None,
        fetched_at=base.fetched_at if base is not None else ensure_utc(fetched_at),
    )
    return RawDocument(
        url=safe_url, final_url=outcome.final_url, fetched_at=outcome.fetched_at, fetch=outcome
    )


def larger_delay(configured: Decimal | None, robots: Decimal | None) -> Decimal | None:
    """The larger of a configured per-host crawl delay and the request's robots crawl-delay."""
    if configured is None:
        return robots
    if robots is None:
        return configured
    return max(configured, robots)


# Target-side failures that must survive a refused final URL: replacing them with
# POLICY_DENIED would lose the route pause (ACCESS_BLOCKED), Retry-After (RATE_LIMITED)
# or host backoff (TRANSIENT_ERROR) that the budget gate derives from them.
_KEEP_ON_REFUSED_REDIRECT: Final = frozenset(
    {AccessState.ACCESS_BLOCKED, AccessState.RATE_LIMITED, AccessState.TRANSIENT_ERROR}
)


def refused_redirect_document(
    url: str, reason: DenialReason, document: RawDocument, *, fetched_at: datetime
) -> RawDocument:
    """The document to return when the final URL fails the policy. The body is never kept."""
    inner = document.fetch
    if inner.access_state not in _KEEP_ON_REFUSED_REDIRECT:
        return policy_denied_document(url, reason, fetched_at=fetched_at, base=inner)
    note = f"final URL refused by policy ({reason.value})"
    message = f"{inner.error_message}; {note}" if inner.error_message else note
    outcome = inner.model_copy(
        update={
            "requested_url": redact_url(inner.requested_url),
            "final_url": redact_url(inner.final_url) if inner.final_url else None,
            "success": False,
            "error_message": message[:500],
        }
    )
    return RawDocument(
        url=redact_url(url), final_url=outcome.final_url, fetched_at=outcome.fetched_at, fetch=outcome
    )


class PolicyEnforcingCrawlClient:
    """The only `CrawlClient` adapters receive in production."""

    def __init__(
        self,
        inner: CrawlClient,
        policies: Mapping[str, SourceUrlPolicy],
        budget_gate: BudgetGate,
        *,
        robots: RobotsChecker | None,
        resolver: Resolver = system_resolver,
        clock: Clock | None = None,
        dns_timeout_s: float = DEFAULT_DNS_TIMEOUT_SECONDS,
    ) -> None:
        """``robots`` is required by keyword: the worker runtime always passes its robots guard;
        ``None`` (no application-level robots check) is an explicit choice of a test or tool."""
        self._inner = inner
        self._policies = dict(policies)
        self._gate = budget_gate
        self._robots = robots
        self._resolver = resolver
        self._clock: Clock = clock or SystemClock()
        self._dns_timeout_s = dns_timeout_s

    @property
    def robots(self) -> RobotsChecker | None:
        return self._robots

    async def fetch(self, url: str, *, purpose: FetchPurpose, source_key: str) -> RawDocument:
        now = ensure_utc(self._clock.now())
        policy = self._policies.get(source_key)
        if policy is None:
            return policy_denied_document(url, DenialReason.UNKNOWN_SOURCE, fetched_at=now)
        try:
            decision = policy.check(url, purpose)
        except PolicyDenied as exc:
            return policy_denied_document(url, exc.reason, fetched_at=now)

        robots: RobotsCheck | None = None
        if self._robots is not None and purpose != "robots":
            # Before any page request to the host: its current robots revision, then this URL's
            # verdict for our product token (see the module docstring, step 3).
            try:
                robots = await self._robots.check(
                    source_key=source_key,
                    policy=policy,
                    host=decision.host,
                    url=decision.url,
                    gate=self._gate,
                )
            except PolicyDenied as exc:
                return policy_denied_document(url, exc.reason, fetched_at=now)
            refused = robots_refusal(url, robots, now=ensure_utc(self._clock.now()))
            if refused is not None:
                return refused

        request = BudgetRequest(
            source_key=source_key,
            host=decision.host,
            purpose=purpose,
            url=decision.url,
            crawl_delay=None if robots is None else robots.crawl_delay,
        )
        budget = await self._gate.acquire(request)
        if not isinstance(budget, Allow):
            raise BudgetRefused(budget, now=now)

        outcome: FetchOutcome | None = None
        try:
            try:
                await policy.resolve_check(url, purpose, self._resolver, timeout_s=self._dns_timeout_s)
            except PolicyDenied as exc:
                state = AccessState.TRANSIENT_ERROR if exc.transient else AccessState.POLICY_DENIED
                document = policy_denied_document(url, exc.reason, fetched_at=now, access_state=state)
                outcome = document.fetch
                return document

            document = await self._inner.fetch(url, purpose=purpose, source_key=source_key)
            final_url = document.final_url
            if final_url is not None and final_url != url:
                try:
                    policy.validate_redirect(url, final_url, purpose)
                except PolicyDenied as exc:
                    document = refused_redirect_document(url, exc.reason, document, fetched_at=now)
                else:
                    if robots is not None:
                        refused = robots_refused_redirect(
                            url, final_url, robots, document, now=ensure_utc(self._clock.now())
                        )
                        document = document if refused is None else refused
            outcome = document.fetch
            return document
        finally:
            with anyio.CancelScope(shield=True):
                await self._gate.release(request, outcome)


class InMemoryBudgetGate:
    """Process-local `BudgetGate` over `rate_limits` (for `crawl once`, tests and single workers).

    Per-host state (lease, token bucket, circuit, Retry-After) is kept per (source, host);
    the daily request/byte budget is enforced per source by summing that source's hosts.
    Production workers persist `HostBudgetState` in ops.host_budgets instead, so state
    survives restarts and is shared between workers.
    """

    def __init__(
        self,
        budgets: Mapping[str, RateBudget],
        *,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        policy: BackoffPolicy = DEFAULT_BACKOFF,
        crawl_delays: Mapping[str, Decimal] | None = None,
    ) -> None:
        self._budgets = dict(budgets)
        self._clock: Clock = clock or SystemClock()
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security
        self._policy = policy
        self._crawl_delays = dict(crawl_delays or {})
        self._states: dict[tuple[str, str], HostBudgetState] = {}
        self._usage: dict[str, RunUsage] = {}
        self._results: dict[tuple[str, str], OutcomeResult] = {}
        self._lock = anyio.Lock()

    def state(self, source_key: str, host: str) -> HostBudgetState:
        return self._states.get((source_key, host)) or HostBudgetState(source_key=source_key, host=host)

    def last_result(self, source_key: str, host: str) -> OutcomeResult | None:
        return self._results.get((source_key, host))

    def set_state(self, state: HostBudgetState) -> None:
        self._states[(state.source_key, state.host)] = state

    def run_usage(self, source_key: str) -> RunUsage:
        return self._usage.get(source_key, RunUsage())

    def reset_run(self, source_key: str) -> None:
        self._usage.pop(source_key, None)

    def daily_usage(self, source_key: str, now: datetime) -> DailyUsage:
        """Source-wide usage for the UTC day of `now`, summed over all of the source's hosts."""
        today = ensure_utc(now).date()
        requests = 0
        size = 0
        for (key, _host), state in self._states.items():
            if key == source_key and state.budget_day == today:
                requests += state.requests_today
                size += state.bytes_today
        return DailyUsage(day=today, requests=requests, bytes=size)

    async def acquire(self, request: BudgetRequest) -> BudgetDecision:
        async with self._lock:
            budget = self._budgets.get(request.source_key)
            if budget is None:
                return Deny(reason=DenyReason.BUDGET_EXHAUSTED)
            now = ensure_utc(self._clock.now())
            state = self.state(request.source_key, request.host)
            usage = self.run_usage(request.source_key)
            # The daily request/byte budget belongs to the source, not to each of its hosts.
            source_usage = self.daily_usage(request.source_key, now)
            delay = larger_delay(self._crawl_delays.get(request.host), request.crawl_delay)
            decision = decide(
                state,
                now,
                budget,
                purpose=request.purpose,
                run_usage=usage,
                source_usage=source_usage,
                crawl_delay=delay,
            )
            if isinstance(decision, Allow):
                self.set_state(
                    start_request(
                        state,
                        now,
                        budget,
                        purpose=request.purpose,
                        run_usage=usage,
                        source_usage=source_usage,
                        crawl_delay=delay,
                        policy=self._policy,
                    )
                )
                if request.purpose == "search":
                    usage = usage.model_copy(update={"search_pages": usage.search_pages + 1})
                elif request.purpose == "detail":
                    usage = usage.model_copy(update={"detail_fetches": usage.detail_fetches + 1})
                self._usage[request.source_key] = usage
            return decision

    async def release(self, request: BudgetRequest, outcome: FetchOutcome | None) -> None:
        async with self._lock:
            budget = self._budgets.get(request.source_key)
            now = ensure_utc(self._clock.now())
            state = self.state(request.source_key, request.host)
            if outcome is None or budget is None:
                self.set_state(abandon_request(state, now))
                return
            result = record_outcome(state, outcome, now, self._rng, budget=budget, policy=self._policy)
            self.set_state(result.state)
            self._results[(request.source_key, request.host)] = result
