"""Policy-enforcing `CrawlClient` wrapper (spec sections 8, 9, 24).

Every fetch passes, in order:

1. Source lookup: an unknown `source_key` is denied.
2. `SourceUrlPolicy.check()` for the purpose (structural SSRF + host/path allow-lists).
3. The budget gate (`rate_limits` decision): no request is made without budget.
   A `Wait`/`Deny` raises a typed `BudgetRefused`; nothing is fetched.
4. Fetch-time DNS check (`resolve_check`): every answer must be a public address.
5. The inner client fetch.
6. `validate_redirect()` on the final URL: cross-host redirects, scheme downgrades and
   off-policy final paths are converted to POLICY_DENIED and the body is discarded.
   Exception: when the target itself blocked, throttled or failed the request
   (ACCESS_BLOCKED, RATE_LIMITED, TRANSIENT_ERROR, e.g. a redirect to a CAPTCHA host),
   that classification is kept (body still discarded) so the budget gate pauses the
   route, honours Retry-After or backs off instead of seeing "our own policy refusal".
7. The budget gate is released with the outcome, even on cancellation (shielded).

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
from pydantic import BaseModel, ConfigDict

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
        resolver: Resolver = system_resolver,
        clock: Clock | None = None,
        dns_timeout_s: float = DEFAULT_DNS_TIMEOUT_SECONDS,
    ) -> None:
        self._inner = inner
        self._policies = dict(policies)
        self._gate = budget_gate
        self._resolver = resolver
        self._clock: Clock = clock or SystemClock()
        self._dns_timeout_s = dns_timeout_s

    async def fetch(self, url: str, *, purpose: FetchPurpose, source_key: str) -> RawDocument:
        now = ensure_utc(self._clock.now())
        policy = self._policies.get(source_key)
        if policy is None:
            return policy_denied_document(url, DenialReason.UNKNOWN_SOURCE, fetched_at=now)
        try:
            decision = policy.check(url, purpose)
        except PolicyDenied as exc:
            return policy_denied_document(url, exc.reason, fetched_at=now)

        request = BudgetRequest(source_key=source_key, host=decision.host, purpose=purpose, url=decision.url)
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
            delay = self._crawl_delays.get(request.host)
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
