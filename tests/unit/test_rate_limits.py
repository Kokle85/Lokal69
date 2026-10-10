"""Unit tests for host budgets, backoff and circuit breaking (spec sections 9, 30)."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from suv_deals.adapters.base import FetchOutcome, RawDocument
from suv_deals.clock import FrozenClock
from suv_deals.crawling.policy_client import (
    BudgetRefused,
    BudgetRequest,
    InMemoryBudgetGate,
    PolicyEnforcingCrawlClient,
)
from suv_deals.crawling.rate_limits import (
    DEFAULT_BACKOFF,
    MAX_CRAWL_DELAY_SECONDS,
    Allow,
    BackoffPolicy,
    CircuitState,
    DailyUsage,
    Deny,
    DenyReason,
    HostBudgetState,
    PlanReason,
    RetryAction,
    RunUsage,
    Wait,
    WaitReason,
    abandon_request,
    backoff_delay_seconds,
    clear_access_block,
    decide,
    effective_delay_seconds,
    next_utc_midnight,
    parse_retry_after,
    record_outcome,
    start_request,
)
from suv_deals.crawling.url_policy import SourceUrlPolicy
from suv_deals.domain.enums import AccessState
from suv_deals.domain.sources import RateBudget
from suv_deals.errors import ErrorCode

NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)
HOST = "www.dealer-example.com"
BUDGET = RateBudget(min_delay_seconds=20, daily_request_budget=5, daily_byte_budget=1_000_000)


def fresh() -> HostBudgetState:
    return HostBudgetState(source_key="dealer_example", host=HOST)


def outcome(
    state: AccessState,
    *,
    code: str | None = None,
    retry_after: int | None = None,
    size: int = 0,
) -> FetchOutcome:
    return FetchOutcome(
        requested_url=f"https://{HOST}/search",
        success=state == AccessState.OK,
        access_state=state,
        error_code=code,
        retry_after_seconds=retry_after,
        bytes=size,
        fetched_at=NOW,
    )


def started(state: HostBudgetState | None = None, at: datetime = NOW) -> HostBudgetState:
    return start_request(state or fresh(), at, BUDGET)


class TestParseRetryAfter:
    @pytest.mark.parametrize(("value", "expected"), [("120", 120), (" 0 ", 0), ("86400", 86400)])
    def test_delay_seconds(self, value: str, expected: int) -> None:
        assert parse_retry_after(value, NOW) == expected

    def test_imf_fixdate(self) -> None:
        assert parse_retry_after("Tue, 06 Oct 2026 10:02:00 GMT", NOW) == 120

    def test_rfc850_and_asctime(self) -> None:
        assert parse_retry_after("Tuesday, 06-Oct-26 10:01:00 GMT", NOW) == 60
        assert parse_retry_after("Tue Oct  6 10:00:30 2026", NOW) == 30

    def test_past_date_is_zero(self) -> None:
        assert parse_retry_after("Mon, 05 Oct 2026 10:00:00 GMT", NOW) == 0

    def test_fractional_date_rounds_up(self) -> None:
        now = NOW + timedelta(milliseconds=500)
        assert parse_retry_after("Tue, 06 Oct 2026 10:00:10 GMT", now) == 10

    @pytest.mark.parametrize(
        "value", [None, "", "abc", "-5", "1.5", "+10", "9" * 13, "x" * 100, "Tue, 99 Foo"]
    )
    def test_invalid(self, value: str | None) -> None:
        assert parse_retry_after(value, NOW) is None

    def test_naive_now_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_retry_after("10", datetime(2026, 10, 6, 10, 0))


class TestTokenBucket:
    def test_fresh_host_allowed(self) -> None:
        assert decide(fresh(), NOW, BUDGET) == Allow()

    def test_one_navigation_at_a_time(self) -> None:
        state = started()
        decision = decide(state, NOW + timedelta(seconds=1), BUDGET)
        assert isinstance(decision, Wait)
        assert decision.reason == WaitReason.NAVIGATION_IN_FLIGHT
        assert decision.until == NOW + timedelta(seconds=DEFAULT_BACKOFF.in_flight_lease_seconds)

    def test_min_delay_after_completion(self) -> None:
        state = record_outcome(
            started(), outcome(AccessState.OK), NOW + timedelta(seconds=5), random.Random(1), budget=BUDGET
        ).state
        decision = decide(state, NOW + timedelta(seconds=5), BUDGET)
        assert decision == Wait(until=NOW + timedelta(seconds=20), reason=WaitReason.MIN_DELAY)
        assert decide(state, NOW + timedelta(seconds=20), BUDGET) == Allow()

    def test_crawl_delay_extends_spacing(self) -> None:
        state = record_outcome(started(), outcome(AccessState.OK), NOW, random.Random(1), budget=BUDGET).state
        decision = decide(state, NOW + timedelta(seconds=25), BUDGET, crawl_delay=Decimal(60))
        assert decision == Wait(until=NOW + timedelta(seconds=60), reason=WaitReason.MIN_DELAY)
        assert effective_delay_seconds(BUDGET, Decimal(5)) == Decimal(20)
        assert effective_delay_seconds(BUDGET, Decimal("NaN")) == Decimal(20)

    @pytest.mark.parametrize("hostile", ["1e30", "1e999999999", "99999999999", "86401"])
    def test_hostile_crawl_delay_is_capped_and_never_crashes(self, hostile: str) -> None:
        # robots.txt is untrusted: absurd Crawl-delay values must not overflow time arithmetic.
        delay = Decimal(hostile)
        assert effective_delay_seconds(BUDGET, delay) == MAX_CRAWL_DELAY_SECONDS
        state = record_outcome(
            start_request(fresh(), NOW, BUDGET, crawl_delay=delay),
            outcome(AccessState.OK),
            NOW,
            random.Random(1),
            budget=BUDGET,
        ).state
        decision = decide(state, NOW + timedelta(seconds=1), BUDGET, crawl_delay=delay)
        assert decision == Wait(until=NOW + timedelta(days=1), reason=WaitReason.MIN_DELAY)
        assert decide(state, NOW + timedelta(days=1), BUDGET, crawl_delay=delay) == Allow()

    def test_stale_in_flight_lease_expires(self) -> None:
        state = started()
        later = NOW + timedelta(seconds=DEFAULT_BACKOFF.in_flight_lease_seconds + 1)
        assert decide(state, later, BUDGET) == Allow()

    def test_abandon_releases_lease(self) -> None:
        state = abandon_request(started(), NOW)
        assert state.in_flight_until is None
        assert state.requests_today == 1

    def test_start_without_allow_rejected(self) -> None:
        with pytest.raises(ValueError, match="without an Allow"):
            start_request(started(), NOW, BUDGET)

    def test_naive_datetimes_rejected(self) -> None:
        with pytest.raises(ValueError):
            decide(fresh(), datetime(2026, 10, 6), BUDGET)
        with pytest.raises(ValueError):
            HostBudgetState(source_key="x_source", host=HOST, open_until=datetime(2026, 10, 6))


class TestDailyBudgets:
    def test_request_budget_exhausted_until_midnight(self) -> None:
        state = fresh().model_copy(update={"budget_day": NOW.date(), "requests_today": 5})
        decision = decide(state, NOW, BUDGET)
        assert decision == Deny(reason=DenyReason.BUDGET_EXHAUSTED, until=datetime(2026, 10, 7, tzinfo=UTC))
        assert next_utc_midnight(NOW) == datetime(2026, 10, 7, tzinfo=UTC)

    def test_new_day_resets_counters(self) -> None:
        state = fresh().model_copy(
            update={"budget_day": date(2026, 10, 5), "requests_today": 5, "bytes_today": 10**9}
        )
        assert decide(state, NOW, BUDGET) == Allow()
        assert start_request(state, NOW, BUDGET).requests_today == 1

    def test_byte_budget(self) -> None:
        state = fresh().model_copy(update={"budget_day": NOW.date(), "bytes_today": 1_000_000})
        decision = decide(state, NOW, BUDGET)
        assert isinstance(decision, Deny)
        assert decision.reason == DenyReason.BUDGET_EXHAUSTED

    def test_zero_budget_denies(self) -> None:
        assert isinstance(decide(fresh(), NOW, RateBudget(daily_request_budget=0)), Deny)

    def test_source_wide_usage_overrides_host_counters(self) -> None:
        usage = DailyUsage(day=NOW.date(), requests=5)
        assert isinstance(decide(fresh(), NOW, BUDGET, source_usage=usage), Deny)
        stale_usage = DailyUsage(day=date(2026, 10, 5), requests=5)
        assert decide(fresh(), NOW, BUDGET, source_usage=stale_usage) == Allow()

    def test_bytes_accumulate(self) -> None:
        state = record_outcome(
            started(), outcome(AccessState.OK, size=4096), NOW, random.Random(1), budget=BUDGET
        ).state
        assert state.bytes_today == 4096
        assert state.budget_day == NOW.date()

    def test_run_caps_never_catch_up(self) -> None:
        caps = RunUsage(
            search_pages=BUDGET.max_search_pages_per_run, detail_fetches=BUDGET.max_detail_jobs_per_run
        )
        assert decide(fresh(), NOW, BUDGET, purpose="search", run_usage=caps) == Deny(
            reason=DenyReason.RUN_CAP_REACHED
        )
        assert decide(fresh(), NOW, BUDGET, purpose="detail", run_usage=caps) == Deny(
            reason=DenyReason.RUN_CAP_REACHED
        )
        assert decide(fresh(), NOW, BUDGET, purpose="robots", run_usage=caps) == Allow()
        under = RunUsage(search_pages=1)
        assert decide(fresh(), NOW, BUDGET, purpose="search", run_usage=under) == Allow()


class TestRateLimited:
    def test_retry_after_honoured_and_never_shortened(self) -> None:
        for seed in range(20):
            result = record_outcome(
                started(),
                outcome(AccessState.RATE_LIMITED, retry_after=600),
                NOW,
                random.Random(seed),
                budget=BUDGET,
            )
            assert result.plan.action == RetryAction.RETRY
            assert result.plan.retry_at is not None
            assert result.plan.retry_at >= NOW + timedelta(seconds=600)
            assert result.plan.retry_after_seconds == 600
            assert result.state.retry_after_until == NOW + timedelta(seconds=600)
            assert result.state.last_retry_after_seconds == 600

    def test_host_waits_for_retry_after(self) -> None:
        state = record_outcome(
            started(),
            outcome(AccessState.RATE_LIMITED, retry_after=600),
            NOW,
            random.Random(1),
            budget=BUDGET,
        ).state
        assert decide(state, NOW + timedelta(seconds=599), BUDGET) == Wait(
            until=NOW + timedelta(seconds=600), reason=WaitReason.RETRY_AFTER
        )

    def test_without_retry_after_uses_floor(self) -> None:
        result = record_outcome(
            started(), outcome(AccessState.RATE_LIMITED), NOW, random.Random(3), budget=BUDGET
        )
        assert result.plan.delay_seconds is not None
        assert result.plan.delay_seconds >= DEFAULT_BACKOFF.rate_limited_floor_seconds
        assert result.state.next_request_not_before is not None
        assert result.state.consecutive_failures == 1

    def test_excessive_retry_after_pauses_route(self) -> None:
        result = record_outcome(
            started(),
            outcome(AccessState.RATE_LIMITED, retry_after=3 * 86400),
            NOW,
            random.Random(1),
            budget=BUDGET,
        )
        assert result.plan.action == RetryAction.STOP
        assert result.plan.reason == PlanReason.RETRY_AFTER_EXCEEDS_CAP
        assert result.pause is not None
        assert result.pause.reason == "retry_after_exceeds_cap"
        assert result.state.retry_after_until == NOW + timedelta(days=3)  # stored unshortened

    def test_crawler_rate_limit_does_not_touch_host(self) -> None:
        result = record_outcome(
            started(),
            outcome(AccessState.RATE_LIMITED, code="crawler_rate_limited", retry_after=30),
            NOW,
            random.Random(1),
            budget=BUDGET,
        )
        assert result.plan.reason == PlanReason.CRAWLER_INFRASTRUCTURE
        assert result.plan.retry_at is not None and result.plan.retry_at >= NOW + timedelta(seconds=30)
        assert result.state.consecutive_failures == 0
        assert result.state.retry_after_until is None


class TestTransientBackoff:
    def test_full_jitter_is_deterministic_with_seed(self) -> None:
        a = record_outcome(
            started(), outcome(AccessState.TRANSIENT_ERROR), NOW, random.Random(42), budget=BUDGET, attempt=3
        )
        b = record_outcome(
            started(), outcome(AccessState.TRANSIENT_ERROR), NOW, random.Random(42), budget=BUDGET, attempt=3
        )
        assert a == b
        delays = {
            record_outcome(
                started(),
                outcome(AccessState.TRANSIENT_ERROR),
                NOW,
                random.Random(seed),
                budget=BUDGET,
                attempt=3,
            ).plan.delay_seconds
            for seed in range(30)
        }
        assert len(delays) > 5  # jitter actually varies

    @pytest.mark.parametrize("attempt", [1, 2, 3, 4, 10])
    def test_delay_bounds(self, attempt: int) -> None:
        policy = BackoffPolicy(base_delay_seconds=30, max_delay_seconds=600, max_attempts=20)
        ceiling = min(600, 30 * 2 ** (attempt - 1))
        for seed in range(50):
            delay = backoff_delay_seconds(attempt, policy, random.Random(seed), floor=20)
            assert 20 <= delay <= max(20, ceiling)

    def test_attempts_are_bounded(self) -> None:
        result = record_outcome(
            started(), outcome(AccessState.TRANSIENT_ERROR), NOW, random.Random(1), budget=BUDGET, attempt=5
        )
        assert result.plan.action == RetryAction.GIVE_UP
        assert result.plan.reason == PlanReason.ATTEMPTS_EXHAUSTED
        assert result.plan.retry_at is None

    def test_invalid_attempt(self) -> None:
        with pytest.raises(ValueError):
            record_outcome(
                started(), outcome(AccessState.OK), NOW, random.Random(1), budget=BUDGET, attempt=0
            )

    def test_host_backs_off(self) -> None:
        result = record_outcome(
            started(), outcome(AccessState.TRANSIENT_ERROR), NOW, random.Random(7), budget=BUDGET
        )
        assert result.plan.action == RetryAction.RETRY
        assert result.state.next_request_not_before == result.plan.retry_at
        decision = decide(result.state, NOW + timedelta(seconds=1), BUDGET)
        assert isinstance(decision, Wait)
        assert decision.reason == WaitReason.BACKOFF

    def test_crawler_side_failures_not_counted(self) -> None:
        for code in ("crawler_unreachable", "crawler_server_error", "crawler_bad_response"):
            result = record_outcome(
                started(),
                outcome(AccessState.TRANSIENT_ERROR, code=code),
                NOW,
                random.Random(1),
                budget=BUDGET,
            )
            assert result.state.consecutive_failures == 0
            assert result.state.next_request_not_before is None
            assert result.plan.reason == PlanReason.CRAWLER_INFRASTRUCTURE
            assert result.plan.action == RetryAction.RETRY


class TestCircuit:
    def _fail(self, state: HostBudgetState, at: datetime, seed: int = 1) -> HostBudgetState:
        state = state.model_copy(
            update={"next_request_not_before": None, "tokens": Decimal(1), "refilled_at": None}
        )
        running = start_request(state, at, BUDGET)
        return record_outcome(
            running, outcome(AccessState.TRANSIENT_ERROR), at, random.Random(seed), budget=BUDGET
        ).state

    def test_opens_half_opens_and_closes(self) -> None:
        state = fresh()
        for _ in range(DEFAULT_BACKOFF.circuit_failure_threshold):
            state = self._fail(state, NOW)
        assert state.circuit_state == CircuitState.OPEN
        assert state.open_until == NOW + timedelta(seconds=DEFAULT_BACKOFF.circuit_cooldown_seconds)
        assert decide(state, NOW + timedelta(seconds=10), BUDGET) == Deny(
            reason=DenyReason.CIRCUIT_OPEN, until=state.open_until
        )

        after = state.open_until + timedelta(seconds=1)
        state = state.model_copy(update={"next_request_not_before": None})
        assert decide(state, after, BUDGET) == Allow(probe=True)
        probing = start_request(state, after, BUDGET)
        assert probing.circuit_state == CircuitState.HALF_OPEN
        wait = decide(probing, after + timedelta(seconds=1), BUDGET)
        assert isinstance(wait, Wait) and wait.reason == WaitReason.NAVIGATION_IN_FLIGHT

        closed = record_outcome(
            probing, outcome(AccessState.OK), after, random.Random(1), budget=BUDGET
        ).state
        assert closed.circuit_state == CircuitState.CLOSED
        assert closed.consecutive_failures == 0
        assert closed.open_until is None

    def test_failed_probe_reopens_with_longer_cooldown(self) -> None:
        state = fresh()
        for _ in range(DEFAULT_BACKOFF.circuit_failure_threshold):
            state = self._fail(state, NOW)
        first_cooldown = state.open_until - NOW  # type: ignore[operator]
        after = state.open_until + timedelta(seconds=1)  # type: ignore[operator]
        reopened = self._fail(state, after)
        assert reopened.circuit_state == CircuitState.OPEN
        assert reopened.open_until is not None
        assert reopened.open_until - after > first_cooldown

    def test_retry_plan_waits_for_circuit(self) -> None:
        state = fresh()
        for _ in range(DEFAULT_BACKOFF.circuit_failure_threshold - 1):
            state = self._fail(state, NOW)
        running = start_request(
            state.model_copy(update={"next_request_not_before": None, "refilled_at": None}), NOW, BUDGET
        )
        result = record_outcome(
            running, outcome(AccessState.TRANSIENT_ERROR), NOW, random.Random(1), budget=BUDGET
        )
        assert result.state.circuit_state == CircuitState.OPEN
        assert result.plan.retry_at is not None and result.state.open_until is not None
        assert result.plan.retry_at >= result.state.open_until

    def test_cooldown_is_capped(self) -> None:
        policy = BackoffPolicy(circuit_cooldown_seconds=900, circuit_cooldown_max_seconds=1800)
        state = fresh().model_copy(update={"consecutive_failures": 40})
        running = start_request(state, NOW, BUDGET, policy=policy)
        result = record_outcome(
            running, outcome(AccessState.TRANSIENT_ERROR), NOW, random.Random(1), budget=BUDGET, policy=policy
        )
        assert result.state.open_until == NOW + timedelta(seconds=1800)


class TestAccessBlocked:
    def test_never_retried_and_pauses_route(self) -> None:
        result = record_outcome(
            started(),
            outcome(AccessState.ACCESS_BLOCKED, code="anti_bot_block"),
            NOW,
            random.Random(1),
            budget=BUDGET,
        )
        assert result.plan.action == RetryAction.STOP
        assert result.plan.reason == PlanReason.ACCESS_BLOCKED
        assert result.plan.retry_at is None
        assert result.pause is not None
        assert result.pause.dedup_key == f"access_blocked:dealer_example:{HOST}"
        assert result.pause.requires_explicit_action is True
        assert result.pause.evidence == "anti_bot_block"
        assert decide(result.state, NOW + timedelta(days=30), BUDGET) == Deny(
            reason=DenyReason.ACCESS_BLOCKED
        )

    def test_explicit_clear_allows_single_probe(self) -> None:
        blocked = record_outcome(
            started(), outcome(AccessState.ACCESS_BLOCKED), NOW, random.Random(1), budget=BUDGET
        ).state
        cleared = clear_access_block(blocked, NOW + timedelta(hours=1))
        assert cleared.access_blocked_at is None
        assert decide(cleared, NOW + timedelta(hours=1), BUDGET) == Allow(probe=True)


class TestNotHostFailures:
    @pytest.mark.parametrize(
        ("state", "reason"),
        [
            (AccessState.UNEXPECTED_CONTENT, PlanReason.NOT_HOST_FAILURE),
            (AccessState.POLICY_DENIED, PlanReason.POLICY_DENIED),
        ],
    )
    def test_parser_and_policy_outcomes(self, state: AccessState, reason: PlanReason) -> None:
        base = started().model_copy(update={"consecutive_failures": 2})
        result = record_outcome(base, outcome(state), NOW, random.Random(1), budget=BUDGET)
        assert result.plan.action == RetryAction.STOP
        assert result.plan.reason == reason
        assert result.state.circuit_state == CircuitState.CLOSED
        assert result.state.in_flight_until is None
        assert result.pause is None

    def test_zero_matching_vehicles_is_success(self) -> None:
        base = started().model_copy(update={"consecutive_failures": 2})
        result = record_outcome(base, outcome(AccessState.OK), NOW, random.Random(1), budget=BUDGET)
        assert result.plan.action == RetryAction.NONE
        assert result.state.consecutive_failures == 0

    @pytest.mark.parametrize("state", [AccessState.NOT_FOUND, AccessState.REMOVED])
    def test_not_found_and_removed_are_answers(self, state: AccessState) -> None:
        result = record_outcome(started(), outcome(state), NOW, random.Random(1), budget=BUDGET)
        assert result.plan.action == RetryAction.NONE


# ---------------------------------------------------------------------------
# Budget gate wired through the policy-enforcing client


class _FakeInner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, url: str, *, purpose: str, source_key: str) -> RawDocument:
        self.calls.append(url)
        out = FetchOutcome(
            requested_url=url,
            final_url=url,
            http_status=200,
            success=True,
            access_state=AccessState.OK,
            bytes=10,
            fetched_at=NOW,
        )
        return RawDocument(url=url, final_url=url, fetched_at=NOW, html="<html>" + "x" * 300, fetch=out)


async def _public(host: str, port: int) -> list[str]:
    return ["93.184.215.14"]


def _client(gate: InMemoryBudgetGate, inner: _FakeInner, clock: FrozenClock) -> PolicyEnforcingCrawlClient:
    policy = SourceUrlPolicy(
        "dealer_example", allowed_hosts=(HOST,), search_paths=(r"/search",), detail_paths=(r"/fahrzeug/\d+",)
    )
    return PolicyEnforcingCrawlClient(inner, {"dealer_example": policy}, gate, resolver=_public, clock=clock)


class TestBudgetGateIntegration:
    async def test_no_request_without_budget(self) -> None:
        clock = FrozenClock(NOW)
        gate = InMemoryBudgetGate({"dealer_example": BUDGET}, clock=clock, rng=random.Random(1))
        inner = _FakeInner()
        client = _client(gate, inner, clock)
        doc = await client.fetch(f"https://{HOST}/search", purpose="search", source_key="dealer_example")
        assert doc.fetch.access_state == AccessState.OK
        assert gate.state("dealer_example", HOST).in_flight_until is None

        with pytest.raises(BudgetRefused) as info:
            await client.fetch(f"https://{HOST}/search", purpose="search", source_key="dealer_example")
        assert info.value.code == ErrorCode.RATE_LIMITED
        assert info.value.reason == "min_delay"
        assert info.value.retry_after_seconds is not None and info.value.retry_after_seconds >= 20
        assert len(inner.calls) == 1

        clock.advance(timedelta(seconds=20))
        await client.fetch(f"https://{HOST}/search", purpose="search", source_key="dealer_example")
        assert len(inner.calls) == 2

    async def test_run_cap(self) -> None:
        clock = FrozenClock(NOW)
        budget = RateBudget(min_delay_seconds=5, max_search_pages_per_run=1)
        gate = InMemoryBudgetGate({"dealer_example": budget}, clock=clock, rng=random.Random(1))
        inner = _FakeInner()
        client = _client(gate, inner, clock)
        await client.fetch(f"https://{HOST}/search", purpose="search", source_key="dealer_example")
        clock.advance(timedelta(minutes=5))
        with pytest.raises(BudgetRefused) as info:
            await client.fetch(f"https://{HOST}/search", purpose="search", source_key="dealer_example")
        assert info.value.reason == "run_cap_reached"
        assert info.value.retryable is False
        gate.reset_run("dealer_example")
        await client.fetch(f"https://{HOST}/search", purpose="search", source_key="dealer_example")
        assert len(inner.calls) == 2

    async def test_access_blocked_refusal_is_typed(self) -> None:
        clock = FrozenClock(NOW)
        gate = InMemoryBudgetGate({"dealer_example": BUDGET}, clock=clock)
        gate.set_state(fresh().model_copy(update={"access_blocked_at": NOW}))
        inner = _FakeInner()
        with pytest.raises(BudgetRefused) as info:
            await _client(gate, inner, clock).fetch(
                f"https://{HOST}/search", purpose="search", source_key="dealer_example"
            )
        assert info.value.code == ErrorCode.ACCESS_BLOCKED
        assert info.value.retryable is False
        assert inner.calls == []

    async def test_source_without_budget_is_denied(self) -> None:
        gate = InMemoryBudgetGate({}, clock=FrozenClock(NOW))
        decision = await gate.acquire(
            BudgetRequest(
                source_key="dealer_example", host=HOST, purpose="search", url=f"https://{HOST}/search"
            )
        )
        assert isinstance(decision, Deny)

    async def test_outcome_recorded(self) -> None:
        clock = FrozenClock(NOW)
        gate = InMemoryBudgetGate({"dealer_example": BUDGET}, clock=clock, rng=random.Random(1))
        await _client(gate, _FakeInner(), clock).fetch(
            f"https://{HOST}/fahrzeug/1", purpose="detail", source_key="dealer_example"
        )
        result = gate.last_result("dealer_example", HOST)
        assert result is not None and result.plan.action == RetryAction.NONE
        assert gate.state("dealer_example", HOST).bytes_today == 10
        assert gate.run_usage("dealer_example").detail_fetches == 1

    async def test_daily_budget_is_per_source_across_hosts(self) -> None:
        """A source with several hosts shares one daily request budget (spec section 30)."""
        clock = FrozenClock(NOW)
        budget = RateBudget(min_delay_seconds=5, daily_request_budget=3)
        gate = InMemoryBudgetGate({"dealer_example": budget}, clock=clock, rng=random.Random(1))
        hosts = (
            "a.dealer-example.com",
            "b.dealer-example.com",
            "c.dealer-example.com",
            "d.dealer-example.com",
        )
        decisions = []
        for host in hosts:
            request = BudgetRequest(
                source_key="dealer_example", host=host, purpose="robots", url=f"https://{host}/robots.txt"
            )
            decision = await gate.acquire(request)
            decisions.append(decision)
            if isinstance(decision, Allow):
                await gate.release(request, outcome(AccessState.OK, size=100))
        assert [d.kind for d in decisions] == ["allow", "allow", "allow", "deny"]
        refused = decisions[-1]
        assert isinstance(refused, Deny) and refused.reason == DenyReason.BUDGET_EXHAUSTED
        assert refused.until == next_utc_midnight(NOW)
        usage = gate.daily_usage("dealer_example", NOW)
        assert (usage.requests, usage.bytes) == (3, 300)
        assert gate.daily_usage("other_source", NOW).requests == 0

    async def test_daily_byte_budget_is_per_source_across_hosts(self) -> None:
        clock = FrozenClock(NOW)
        budget = RateBudget(min_delay_seconds=5, daily_byte_budget=1000)
        gate = InMemoryBudgetGate({"dealer_example": budget}, clock=clock, rng=random.Random(1))
        first = BudgetRequest(
            source_key="dealer_example", host="a.dealer-example.com", purpose="detail", url="https://a/x"
        )
        assert isinstance(await gate.acquire(first), Allow)
        await gate.release(first, outcome(AccessState.OK, size=1000))
        second = first.model_copy(update={"host": "b.dealer-example.com"})
        decision = await gate.acquire(second)
        assert isinstance(decision, Deny) and decision.reason == DenyReason.BUDGET_EXHAUSTED

    async def test_daily_usage_resets_on_new_utc_day(self) -> None:
        clock = FrozenClock(NOW)
        budget = RateBudget(min_delay_seconds=5, daily_request_budget=1)
        gate = InMemoryBudgetGate({"dealer_example": budget}, clock=clock, rng=random.Random(1))
        request = BudgetRequest(
            source_key="dealer_example", host=HOST, purpose="robots", url=f"https://{HOST}/robots.txt"
        )
        assert isinstance(await gate.acquire(request), Allow)
        await gate.release(request, outcome(AccessState.OK))
        assert isinstance(await gate.acquire(request), Deny)
        clock.advance(timedelta(days=1))
        assert isinstance(await gate.acquire(request), Allow)
