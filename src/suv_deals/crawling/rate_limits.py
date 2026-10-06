"""Per-host token bucket, daily budgets, circuit breaker and retry planning (spec sections 9, 30).

Everything here is pure: callers load a `HostBudgetState` row (ops.host_budgets),
call `decide()` before a request, `start_request()` when it is allowed, and
`record_outcome()` afterwards, then persist the returned state. Time and
randomness are injected so every decision is deterministic in tests.

Budget values come from `RateBudget` and the `BackoffPolicy` below. Both are
ENGINEERING DEFAULTS, not provider-approved quotas.

Rules implemented:

- One navigation at a time per host (`in_flight_until` lease) and a minimum delay
  between navigation starts (a capacity-1 token bucket refilled at
  `1 / max(min_delay_seconds, robots crawl-delay)` tokens per second).
- Daily request and byte budgets per source (UTC day) and per-run caps on search
  pages and detail fetches. A refusal is a `Deny`; nothing ever "catches up" by
  raising traffic later.
- 429: `Retry-After` (seconds or HTTP-date) is honoured and never shortened. A value
  above `retry_after_max_seconds` is not retried automatically: the route gets a
  pause recommendation instead.
- Transient errors (timeouts, 5xx, connection failures): exponential backoff with
  full jitter, bounded attempts, and a circuit that opens after N consecutive
  host failures, allows one half-open probe after the cooldown, and reopens with
  a longer cooldown when the probe fails.
- ACCESS_BLOCKED: never retried, the host is marked blocked and a typed route
  pause recommendation is returned. Only `clear_access_block()` (an explicit,
  permitted operator action) lifts it, and the next request is a half-open probe.
- Parser breakage (UNEXPECTED_CONTENT), our own policy refusals (POLICY_DENIED)
  and "zero matching vehicles" (an OK page) are not host failures.
- Outcomes whose `error_code` starts with `crawler_` are failures of our own
  crawler infrastructure (unreachable, overloaded, rejected config). They are
  retried with backoff but never counted against the target host.
"""

from __future__ import annotations

import math
import random
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from suv_deals.adapters.base import FetchOutcome, FetchPurpose
from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import AccessState
from suv_deals.domain.sources import RateBudget

_FROZEN = ConfigDict(frozen=True, extra="forbid")
INFRASTRUCTURE_ERROR_PREFIX: Final = "crawler_"
TOKEN_CAPACITY: Final = Decimal(1)
# Upper bound for storing a not-before instant (keeps absurd Retry-After values representable).
_MAX_STORED_WAIT: Final = timedelta(days=365)
_DELAY_SECONDS = re.compile(r"[0-9]{1,12}")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class DenyReason(StrEnum):
    BUDGET_EXHAUSTED = "budget_exhausted"
    RUN_CAP_REACHED = "run_cap_reached"
    CIRCUIT_OPEN = "circuit_open"
    ACCESS_BLOCKED = "access_blocked"


class WaitReason(StrEnum):
    MIN_DELAY = "min_delay"
    NAVIGATION_IN_FLIGHT = "navigation_in_flight"
    RETRY_AFTER = "retry_after"
    BACKOFF = "backoff"


class RetryAction(StrEnum):
    NONE = "none"  # the attempt succeeded or needs no retry
    RETRY = "retry"  # retry at `retry_at`
    GIVE_UP = "give_up"  # bounded attempts exhausted: dead letter / incomplete run
    STOP = "stop"  # do not retry automatically (access blocked, policy, parser, oversized wait)


class PlanReason(StrEnum):
    SUCCESS = "success"
    NOT_HOST_FAILURE = "not_host_failure"
    POLICY_DENIED = "policy_denied"
    ACCESS_BLOCKED = "access_blocked"
    RATE_LIMITED = "rate_limited"
    RETRY_AFTER_EXCEEDS_CAP = "retry_after_exceeds_cap"
    TRANSIENT_ERROR = "transient_error"
    CRAWLER_INFRASTRUCTURE = "crawler_infrastructure"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"


def _utc_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


class HostBudgetState(BaseModel):
    """Mirror of one ops.host_budgets row (per source and host)."""

    model_config = _FROZEN

    source_key: str = Field(min_length=1, max_length=60)
    host: str = Field(min_length=1, max_length=253)
    tokens: Decimal = Field(default=TOKEN_CAPACITY, ge=0, le=TOKEN_CAPACITY)
    refilled_at: datetime | None = None
    circuit_state: CircuitState = CircuitState.CLOSED
    open_until: datetime | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    day: date | None = None
    requests_today: int = Field(default=0, ge=0)
    bytes_today: int = Field(default=0, ge=0)
    # Not-before instant derived from the most recent Retry-After answer.
    last_retry_after: datetime | None = None
    # Not-before instant from exponential backoff after a transient host failure.
    backoff_until: datetime | None = None
    # One navigation at a time: lease that expires on its own if a worker dies mid-request.
    in_flight_until: datetime | None = None
    # Set on ACCESS_BLOCKED; cleared only by an explicit permitted action.
    access_blocked_at: datetime | None = None

    @field_validator(
        "refilled_at", "open_until", "last_retry_after", "backoff_until", "in_flight_until", "access_blocked_at"
    )
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _utc_or_none(value)


class BackoffPolicy(BaseModel):
    """Retry and circuit settings. ENGINEERING DEFAULTS, not provider-approved values."""

    model_config = _FROZEN

    label: str = "engineering_default"
    base_delay_seconds: int = Field(default=30, ge=1)
    max_delay_seconds: int = Field(default=3600, ge=1)
    max_attempts: int = Field(default=5, ge=1, le=20)
    rate_limited_floor_seconds: int = Field(default=60, ge=1)
    retry_after_max_seconds: int = Field(default=86_400, ge=60)
    circuit_failure_threshold: int = Field(default=3, ge=1)
    circuit_cooldown_seconds: int = Field(default=900, ge=1)
    circuit_cooldown_max_seconds: int = Field(default=6 * 3600, ge=1)
    in_flight_lease_seconds: int = Field(default=330, ge=30)


DEFAULT_BACKOFF: Final = BackoffPolicy()


class RunUsage(BaseModel):
    """Requests already made by the current crawl run (per source/profile/partition)."""

    model_config = _FROZEN

    search_pages: int = Field(default=0, ge=0)
    detail_fetches: int = Field(default=0, ge=0)


class DailyUsage(BaseModel):
    """Source-wide usage for one UTC day, aggregated over all of the source's hosts."""

    model_config = _FROZEN

    day: date
    requests: int = Field(default=0, ge=0)
    bytes: int = Field(default=0, ge=0)


class Allow(BaseModel):
    model_config = _FROZEN

    kind: Literal["allow"] = "allow"
    probe: bool = False  # half-open circuit probe: exactly one request decides the circuit


class Wait(BaseModel):
    model_config = _FROZEN

    kind: Literal["wait"] = "wait"
    until: datetime
    reason: WaitReason


class Deny(BaseModel):
    model_config = _FROZEN

    kind: Literal["deny"] = "deny"
    reason: DenyReason
    until: datetime | None = None  # earliest time the refusal may lift on its own; None = explicit action


BudgetDecision = Allow | Wait | Deny


class RetryPlan(BaseModel):
    model_config = _FROZEN

    action: RetryAction
    reason: PlanReason
    attempt: int = Field(ge=1)
    retry_at: datetime | None = None
    delay_seconds: int | None = Field(default=None, ge=0)
    retry_after_seconds: int | None = Field(default=None, ge=0)


class RoutePauseRecommendation(BaseModel):
    """Pause the affected source route; one deduplicated operational review item per key."""

    model_config = _FROZEN

    source_key: str
    host: str
    reason: Literal["access_blocked", "retry_after_exceeds_cap"]
    dedup_key: str
    evidence: str | None = Field(default=None, max_length=500)
    recommended_at: datetime
    requires_explicit_action: Literal[True] = True


class OutcomeResult(BaseModel):
    model_config = _FROZEN

    state: HostBudgetState
    plan: RetryPlan
    pause: RoutePauseRecommendation | None = None


# ---------------------------------------------------------------------------
# Helpers


def _seconds(delta: Decimal) -> timedelta:
    return timedelta(microseconds=int(delta * Decimal(1_000_000)))


def next_utc_midnight(now: datetime) -> datetime:
    now = ensure_utc(now)
    return datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)


def effective_delay_seconds(budget: RateBudget, crawl_delay: Decimal | None = None) -> Decimal:
    """Minimum spacing between navigation starts: the larger of our budget and robots crawl-delay."""
    delay = Decimal(budget.min_delay_seconds)
    if crawl_delay is not None and crawl_delay.is_finite() and crawl_delay > delay:
        delay = crawl_delay
    return delay


def _roll_day(state: HostBudgetState, now: datetime) -> HostBudgetState:
    today = now.date()
    if state.day == today:
        return state
    return state.model_copy(update={"day": today, "requests_today": 0, "bytes_today": 0})


def available_tokens(
    state: HostBudgetState, now: datetime, budget: RateBudget, crawl_delay: Decimal | None = None
) -> Decimal:
    if state.refilled_at is None:
        return TOKEN_CAPACITY
    elapsed = Decimal(str(max(0.0, (ensure_utc(now) - state.refilled_at).total_seconds())))
    refill = elapsed / effective_delay_seconds(budget, crawl_delay)
    return min(TOKEN_CAPACITY, state.tokens + refill)


def _later(*candidates: datetime | None) -> datetime | None:
    present = [c for c in candidates if c is not None]
    return max(present) if present else None


# ---------------------------------------------------------------------------
# Decisions


def decide(
    state: HostBudgetState,
    now: datetime,
    budget: RateBudget,
    *,
    purpose: FetchPurpose = "search",
    run_usage: RunUsage | None = None,
    source_usage: DailyUsage | None = None,
    crawl_delay: Decimal | None = None,
) -> BudgetDecision:
    """May one request start now? Pure; does not consume anything."""
    now = ensure_utc(now)
    if state.access_blocked_at is not None:
        return Deny(reason=DenyReason.ACCESS_BLOCKED)
    probe = False
    if state.circuit_state == CircuitState.OPEN:
        if state.open_until is not None and now < state.open_until:
            return Deny(reason=DenyReason.CIRCUIT_OPEN, until=state.open_until)
        probe = True
    elif state.circuit_state == CircuitState.HALF_OPEN:
        probe = True

    rolled = _roll_day(state, now)
    if source_usage is not None and source_usage.day == now.date():
        requests_today, bytes_today = source_usage.requests, source_usage.bytes
    else:
        requests_today, bytes_today = rolled.requests_today, rolled.bytes_today
    if requests_today >= budget.daily_request_budget or bytes_today >= budget.daily_byte_budget:
        return Deny(reason=DenyReason.BUDGET_EXHAUSTED, until=next_utc_midnight(now))

    if run_usage is not None:
        if purpose == "search" and run_usage.search_pages >= budget.max_search_pages_per_run:
            return Deny(reason=DenyReason.RUN_CAP_REACHED)
        if purpose == "detail" and run_usage.detail_fetches >= budget.max_detail_jobs_per_run:
            return Deny(reason=DenyReason.RUN_CAP_REACHED)

    if state.in_flight_until is not None and now < state.in_flight_until:
        return Wait(until=state.in_flight_until, reason=WaitReason.NAVIGATION_IN_FLIGHT)
    if state.last_retry_after is not None and now < state.last_retry_after:
        return Wait(until=state.last_retry_after, reason=WaitReason.RETRY_AFTER)
    if state.backoff_until is not None and now < state.backoff_until:
        return Wait(until=state.backoff_until, reason=WaitReason.BACKOFF)

    tokens = available_tokens(state, now, budget, crawl_delay)
    if tokens < TOKEN_CAPACITY:
        missing = TOKEN_CAPACITY - tokens
        until = now + _seconds(missing * effective_delay_seconds(budget, crawl_delay))
        return Wait(until=until, reason=WaitReason.MIN_DELAY)
    return Allow(probe=probe)


def start_request(
    state: HostBudgetState,
    now: datetime,
    budget: RateBudget,
    *,
    purpose: FetchPurpose = "search",
    run_usage: RunUsage | None = None,
    source_usage: DailyUsage | None = None,
    crawl_delay: Decimal | None = None,
    policy: BackoffPolicy = DEFAULT_BACKOFF,
) -> HostBudgetState:
    """Consume a token, take the per-host navigation lease and count the request.

    Raises ValueError unless `decide()` allows the request with the same inputs.
    """
    now = ensure_utc(now)
    decision = decide(
        state,
        now,
        budget,
        purpose=purpose,
        run_usage=run_usage,
        source_usage=source_usage,
        crawl_delay=crawl_delay,
    )
    if not isinstance(decision, Allow):
        raise ValueError(f"start_request without an Allow decision ({decision.kind})")
    rolled = _roll_day(state, now)
    tokens = available_tokens(rolled, now, budget, crawl_delay) - TOKEN_CAPACITY
    return rolled.model_copy(
        update={
            "tokens": max(Decimal(0), tokens),
            "refilled_at": now,
            "requests_today": rolled.requests_today + 1,
            "in_flight_until": now + timedelta(seconds=policy.in_flight_lease_seconds),
            "circuit_state": CircuitState.HALF_OPEN if decision.probe else rolled.circuit_state,
            "open_until": None if decision.probe else rolled.open_until,
        }
    )


def abandon_request(state: HostBudgetState, now: datetime) -> HostBudgetState:
    """Release the navigation lease when no outcome exists (cancelled job, lease loss)."""
    ensure_utc(now)
    return state.model_copy(update={"in_flight_until": None})


def clear_access_block(state: HostBudgetState, now: datetime) -> HostBudgetState:
    """Explicit, permitted operator action. The next request is a single half-open probe."""
    ensure_utc(now)
    return state.model_copy(
        update={
            "access_blocked_at": None,
            "circuit_state": CircuitState.HALF_OPEN,
            "open_until": None,
            "in_flight_until": None,
        }
    )


# ---------------------------------------------------------------------------
# Outcomes


def backoff_delay_seconds(attempt: int, policy: BackoffPolicy, rng: random.Random, *, floor: int) -> int:
    """Exponential backoff with full jitter: uniform(0, min(max, base * 2**(attempt-1))), floored."""
    exponent = min(max(attempt, 1) - 1, 30)
    ceiling = min(policy.max_delay_seconds, policy.base_delay_seconds * (2**exponent))
    jittered = rng.uniform(0, ceiling)
    return max(floor, math.ceil(jittered))


def _circuit_cooldown(failures: int, policy: BackoffPolicy) -> int:
    extra = min(max(0, failures - policy.circuit_failure_threshold), 30)
    return min(policy.circuit_cooldown_max_seconds, policy.circuit_cooldown_seconds * (2**extra))


def _bounded_wait(now: datetime, seconds: int) -> datetime:
    return now + min(timedelta(seconds=seconds), _MAX_STORED_WAIT)


def record_outcome(
    state: HostBudgetState,
    outcome: FetchOutcome,
    now: datetime,
    rng: random.Random,
    *,
    budget: RateBudget,
    attempt: int = 1,
    policy: BackoffPolicy = DEFAULT_BACKOFF,
) -> OutcomeResult:
    """Fold one fetch outcome into the host state and plan the job's next step."""
    now = ensure_utc(now)
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    rolled = _roll_day(state, now)
    base = rolled.model_copy(update={"in_flight_until": None, "bytes_today": rolled.bytes_today + outcome.bytes})
    access = outcome.access_state
    infrastructure = (outcome.error_code or "").startswith(INFRASTRUCTURE_ERROR_PREFIX)

    if access in (AccessState.OK, AccessState.NOT_FOUND, AccessState.REMOVED):
        healthy = base.model_copy(
            update={
                "consecutive_failures": 0,
                "circuit_state": CircuitState.CLOSED,
                "open_until": None,
                "backoff_until": None,
            }
        )
        return OutcomeResult(
            state=healthy, plan=RetryPlan(action=RetryAction.NONE, reason=PlanReason.SUCCESS, attempt=attempt)
        )

    if access == AccessState.UNEXPECTED_CONTENT:
        # The host answered; drift or an app shell is the parser-health tripwires' business.
        reachable = base
        if base.circuit_state != CircuitState.CLOSED and not infrastructure:
            reachable = base.model_copy(
                update={"circuit_state": CircuitState.CLOSED, "open_until": None, "consecutive_failures": 0}
            )
        return OutcomeResult(
            state=reachable,
            plan=RetryPlan(action=RetryAction.STOP, reason=PlanReason.NOT_HOST_FAILURE, attempt=attempt),
        )

    if access == AccessState.POLICY_DENIED:
        return OutcomeResult(
            state=base, plan=RetryPlan(action=RetryAction.STOP, reason=PlanReason.POLICY_DENIED, attempt=attempt)
        )

    if access == AccessState.ACCESS_BLOCKED:
        blocked = base.model_copy(update={"access_blocked_at": now})
        pause = RoutePauseRecommendation(
            source_key=state.source_key,
            host=state.host,
            reason="access_blocked",
            dedup_key=f"access_blocked:{state.source_key}:{state.host}",
            evidence=(outcome.error_code or outcome.error_message or "access_blocked")[:500],
            recommended_at=now,
        )
        return OutcomeResult(
            state=blocked,
            plan=RetryPlan(action=RetryAction.STOP, reason=PlanReason.ACCESS_BLOCKED, attempt=attempt),
            pause=pause,
        )

    if access == AccessState.RATE_LIMITED:
        return _rate_limited(base, outcome, now, rng, budget, attempt, policy, infrastructure)

    # TRANSIENT_ERROR
    delay = backoff_delay_seconds(attempt, policy, rng, floor=budget.min_delay_seconds)
    if infrastructure:
        return OutcomeResult(
            state=base, plan=_retry_plan(now, delay, attempt, policy, PlanReason.CRAWLER_INFRASTRUCTURE)
        )
    failed = _register_host_failure(base, now, delay, policy)
    plan = _retry_plan(now, delay, attempt, policy, PlanReason.TRANSIENT_ERROR, not_before=failed.open_until)
    return OutcomeResult(state=failed, plan=plan)


def _rate_limited(
    base: HostBudgetState,
    outcome: FetchOutcome,
    now: datetime,
    rng: random.Random,
    budget: RateBudget,
    attempt: int,
    policy: BackoffPolicy,
    infrastructure: bool,
) -> OutcomeResult:
    retry_after = outcome.retry_after_seconds
    delay = backoff_delay_seconds(attempt, policy, rng, floor=max(policy.rate_limited_floor_seconds, 1))
    if retry_after is not None:
        delay = max(delay, retry_after)  # never shorter than the server asked for
    if infrastructure:
        # Our own crawler's limiter: retry the job, leave the target host untouched.
        plan = _retry_plan(now, delay, attempt, policy, PlanReason.CRAWLER_INFRASTRUCTURE)
        return OutcomeResult(state=base, plan=plan.model_copy(update={"retry_after_seconds": retry_after}))

    failed = _register_host_failure(base, now, None, policy)
    updates: dict[str, object] = {}
    if retry_after is not None:
        updates["last_retry_after"] = _bounded_wait(now, retry_after)
    else:
        updates["backoff_until"] = _bounded_wait(now, delay)
    failed = failed.model_copy(update=updates)

    if retry_after is not None and retry_after > policy.retry_after_max_seconds:
        pause = RoutePauseRecommendation(
            source_key=base.source_key,
            host=base.host,
            reason="retry_after_exceeds_cap",
            dedup_key=f"retry_after_exceeds_cap:{base.source_key}:{base.host}",
            evidence=f"Retry-After {retry_after}s exceeds {policy.retry_after_max_seconds}s",
            recommended_at=now,
        )
        plan = RetryPlan(
            action=RetryAction.STOP,
            reason=PlanReason.RETRY_AFTER_EXCEEDS_CAP,
            attempt=attempt,
            retry_after_seconds=retry_after,
        )
        return OutcomeResult(state=failed, plan=plan, pause=pause)

    plan = _retry_plan(now, delay, attempt, policy, PlanReason.RATE_LIMITED, not_before=failed.open_until)
    return OutcomeResult(state=failed, plan=plan.model_copy(update={"retry_after_seconds": retry_after}))


def _register_host_failure(
    state: HostBudgetState, now: datetime, backoff_seconds: int | None, policy: BackoffPolicy
) -> HostBudgetState:
    failures = state.consecutive_failures + 1
    updates: dict[str, object] = {"consecutive_failures": failures}
    if backoff_seconds is not None:
        updates["backoff_until"] = _bounded_wait(now, backoff_seconds)
    probe_failed = state.circuit_state == CircuitState.HALF_OPEN
    if probe_failed or failures >= policy.circuit_failure_threshold:
        updates["circuit_state"] = CircuitState.OPEN
        updates["open_until"] = now + timedelta(seconds=_circuit_cooldown(failures, policy))
    return state.model_copy(update=updates)


def _retry_plan(
    now: datetime,
    delay: int,
    attempt: int,
    policy: BackoffPolicy,
    reason: PlanReason,
    *,
    not_before: datetime | None = None,
) -> RetryPlan:
    if attempt >= policy.max_attempts:
        return RetryPlan(action=RetryAction.GIVE_UP, reason=PlanReason.ATTEMPTS_EXHAUSTED, attempt=attempt)
    retry_at = _later(_bounded_wait(now, delay), not_before)
    assert retry_at is not None
    effective = math.ceil((retry_at - now).total_seconds())
    return RetryPlan(
        action=RetryAction.RETRY, reason=reason, attempt=attempt, retry_at=retry_at, delay_seconds=effective
    )


# ---------------------------------------------------------------------------
# Retry-After


_LEGACY_DATE_FORMATS: Final = (
    "%A, %d-%b-%y %H:%M:%S GMT",  # RFC 850
    "%a %b %d %H:%M:%S %Y",  # asctime
)


def _parse_http_date(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        for fmt in _LEGACY_DATE_FORMATS:
            try:
                parsed = datetime.strptime(value, fmt).replace(tzinfo=UTC)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:  # HTTP-dates are always GMT
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_retry_after(value: str | None, now: datetime) -> int | None:
    """Seconds to wait from a Retry-After header (delay-seconds or HTTP-date); None if absent/invalid.

    Past dates give 0. Values are not capped here; `record_outcome` decides what a
    too-long wait means so it is never silently shortened.
    """
    now = ensure_utc(now)
    if value is None:
        return None
    text = value.strip()
    if not text or len(text) > 64:
        return None
    if _DELAY_SECONDS.fullmatch(text):
        return int(text)
    when = _parse_http_date(text)
    if when is None:
        return None
    return max(0, math.ceil((when - now).total_seconds()))
