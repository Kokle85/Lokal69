"""Unit tests for parser-health tripwires (spec section 25)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from suv_deals.adapters.base import ParseOutcome
from suv_deals.crawling.parser_health import (
    DEFAULT_THRESHOLDS,
    SEVERITY,
    Baseline,
    HealthReason,
    HealthThresholds,
    RecommendedAction,
    SearchPageSignal,
    assess,
    assess_detailed,
    build_baseline,
    recommended_actions,
)
from suv_deals.domain.enums import AccessState

NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)


def detail(i: int = 0, **overrides: Any) -> ParseOutcome:
    values: dict[str, Any] = {
        "page_type": "detail",
        "access_state": AccessState.OK,
        "ok": True,
        "listing_count": 1,
        "has_price": True,
        "has_mileage": True,
        "has_make_model": True,
        "currency": "EUR",
        "price_minor": 250_000 + i * 1_700,
        "mileage_km": Decimal(150_000 + i * 900),
        "observed_at": NOW,
    }
    values.update(overrides)
    return ParseOutcome(**values)


def search(listings: int, **overrides: Any) -> ParseOutcome:
    values: dict[str, Any] = {
        "page_type": "search",
        "listing_count": listings,
        "price_minor": None,
        "currency": None,
        "mileage_km": None,
    }
    values.update(overrides)
    return detail(**values)


def healthy_details(n: int = 20) -> list[ParseOutcome]:
    return [detail(i) for i in range(n)]


def signal(listings: int = 20, marker: bool = True, reported: int | None = 240) -> SearchPageSignal:
    return SearchPageSignal(
        listing_count=listings,
        pagination_marker_present=marker,
        result_count_reported=reported,
        observed_at=NOW,
    )


def baseline_from(samples: list[ParseOutcome], signals: list[SearchPageSignal] | None = None) -> Baseline:
    return build_baseline(samples, signals or [], built_at=NOW)


def reasons(
    samples: list[ParseOutcome], baseline: Baseline | None = None, **kw: Any
) -> tuple[HealthReason, ...]:
    return assess_detailed(samples, baseline, **kw).reasons


class TestMinimumSample:
    def test_insufficient_sample_suppresses_everything(self) -> None:
        # Nine pages all challenges with identical implausible prices: still no claim.
        samples = [detail(page_type="challenge", access_state=AccessState.ACCESS_BLOCKED) for _ in range(5)]
        samples += [detail(price_minor=100) for _ in range(4)]
        result = assess_detailed(samples, None)
        assert result.health.status == "insufficient_sample"
        assert result.health.sample_size == 9
        assert result.reasons == (HealthReason.INSUFFICIENT_SAMPLE,)
        assert result.health.reasons == ("insufficient_sample",)
        assert result.recommended_actions == ()

    def test_empty(self) -> None:
        assert assess([], None).status == "insufficient_sample"

    def test_small_baseline_is_ignored(self) -> None:
        small = baseline_from([search(30) for _ in range(5)])
        current = [search(0) for _ in range(12)]
        result = assess_detailed(current, small)
        assert not result.baseline_used
        assert HealthReason.NEAR_ZERO_LISTINGS not in result.reasons

    def test_healthy(self) -> None:
        samples = healthy_details()
        result = assess_detailed(samples, baseline_from(samples))
        assert result.health.status == "healthy"
        assert result.reasons == ()
        assert result.recommended_actions == ()
        assert result.health.metrics["thresholds"] == "engineering_default"
        assert result.health.metrics["price_coverage"] == "1.000"
        assert result.baseline_used


class TestNearZeroListings:
    def _baseline(self) -> Baseline:
        return baseline_from([search(25) for _ in range(12)])

    def test_positive(self) -> None:
        current = [search(0) for _ in range(9)] + [search(1) for _ in range(3)]
        result = assess_detailed(current, self._baseline())
        assert HealthReason.NEAR_ZERO_LISTINGS in result.reasons
        assert result.health.status == "unhealthy"
        assert result.recommended_actions == (
            RecommendedAction.PAUSE_NEW_ALERTS,
            RecommendedAction.QUARANTINE_NEW_REVISIONS,
        )

    def test_zero_results_without_populated_baseline_is_not_an_alarm(self) -> None:
        # "Zero matching vehicles" on a search that was never populated is a valid answer.
        sparse = baseline_from([search(2) for _ in range(12)])
        assert reasons([search(0) for _ in range(12)], sparse) == ()
        assert reasons([search(0) for _ in range(12)], None) == ()

    def test_negative_normal_variation(self) -> None:
        assert reasons([search(22) for _ in range(12)], self._baseline()) == ()

    def test_moderate_drop_is_result_count_change_not_near_zero(self) -> None:
        found = reasons([search(8) for _ in range(12)], self._baseline())
        assert found == (HealthReason.RESULT_COUNT_CHANGE,)
        assert SEVERITY[HealthReason.RESULT_COUNT_CHANGE] == "degraded"


class TestCoverage:
    def test_price_coverage_drop(self) -> None:
        base = baseline_from(healthy_details())
        current = [detail(i, has_price=i < 6, price_minor=None if i >= 6 else 250_000 + i) for i in range(20)]
        assert HealthReason.PRICE_COVERAGE_DROP in reasons(current, base)

    def test_mileage_coverage_drop(self) -> None:
        base = baseline_from(healthy_details())
        current = [detail(i, has_mileage=i < 5) for i in range(20)]
        found = reasons(current, base)
        assert HealthReason.MILEAGE_COVERAGE_DROP in found
        assert HealthReason.PRICE_COVERAGE_DROP not in found

    def test_absolute_drop_alone_is_not_enough(self) -> None:
        # 1.0 -> 0.65 is a 0.35 absolute drop but above the 0.60 relative ratio.
        base = baseline_from(healthy_details())
        current = [detail(i, has_price=i < 13) for i in range(20)]
        assert HealthReason.PRICE_COVERAGE_DROP not in reasons(current, base)

    def test_relative_drop_alone_is_not_enough(self) -> None:
        # Baseline coverage 0.2 -> 0.05: ratio 0.25 but only 0.15 absolute.
        base = baseline_from([detail(i, has_price=i < 4) for i in range(20)])
        current = [detail(i, has_price=i < 1) for i in range(20)]
        assert HealthReason.PRICE_COVERAGE_DROP not in reasons(current, base)

    def test_needs_baseline(self) -> None:
        current = [detail(i, has_price=False, price_minor=None) for i in range(20)]
        assert HealthReason.PRICE_COVERAGE_DROP not in reasons(current, None)


class TestPrices:
    def test_identical_prices(self) -> None:
        current = [detail(i, price_minor=299_000) for i in range(18)] + [detail(18), detail(19)]
        assert HealthReason.IDENTICAL_PRICES in reasons(current)

    def test_varied_prices(self) -> None:
        assert HealthReason.IDENTICAL_PRICES not in reasons(healthy_details())

    def test_identical_needs_min_priced_samples(self) -> None:
        current = [detail(i, price_minor=299_000) for i in range(5)]
        current += [detail(i, has_price=False, price_minor=None, currency=None) for i in range(10)]
        assert HealthReason.IDENTICAL_PRICES not in reasons(current)

    def test_implausibly_low_prices(self) -> None:
        current = [detail(i, price_minor=19_900 + i) for i in range(8)] + healthy_details(12)
        result = assess_detailed(current, None)
        assert HealthReason.IMPLAUSIBLE_LOW_PRICES in result.reasons
        assert result.health.status == "unhealthy"

    def test_low_price_floor_is_per_currency(self) -> None:
        # MKD 100,000.00 is far above the EUR 300 equivalent; CHF 250 is below it.
        mkd = [detail(i, currency="MKD", price_minor=10_000_000 + i) for i in range(12)]
        assert HealthReason.IMPLAUSIBLE_LOW_PRICES not in reasons(mkd)
        chf = [detail(i, currency="CHF", price_minor=25_000 + i) for i in range(12)]
        assert HealthReason.IMPLAUSIBLE_LOW_PRICES in reasons(chf)

    def test_few_low_prices_are_tolerated(self) -> None:
        current = [detail(i, price_minor=15_000 + i) for i in range(2)] + healthy_details(18)
        assert HealthReason.IMPLAUSIBLE_LOW_PRICES not in reasons(current)

    def test_unknown_currency_not_judged(self) -> None:
        current = [detail(i, currency="XYZ", price_minor=100 + i) for i in range(12)]
        assert HealthReason.IMPLAUSIBLE_LOW_PRICES not in reasons(current)


class TestCurrencyChange:
    def test_positive(self) -> None:
        base = baseline_from(healthy_details())
        current = [detail(i, currency="CHF", price_minor=300_000 + i * 900) for i in range(20)]
        result = assess_detailed(current, base)
        assert result.reasons == (HealthReason.CURRENCY_DISTRIBUTION_CHANGE,)
        assert result.health.status == "degraded"
        assert result.recommended_actions == (RecommendedAction.PAUSE_NEW_ALERTS,)

    def test_small_shift_is_fine(self) -> None:
        base = baseline_from(healthy_details())
        current = [
            detail(i, currency="CHF" if i < 4 else "EUR", price_minor=300_000 + i * 900) for i in range(20)
        ]
        assert HealthReason.CURRENCY_DISTRIBUTION_CHANGE not in reasons(current, base)


class TestChallengeShare:
    def test_positive(self) -> None:
        current = healthy_details(14) + [
            detail(page_type="login", access_state=AccessState.ACCESS_BLOCKED, ok=False) for _ in range(6)
        ]
        assert HealthReason.HIGH_CHALLENGE_SHARE in reasons(current)

    def test_below_share(self) -> None:
        current = healthy_details(18) + [detail(page_type="challenge", ok=False) for _ in range(2)]
        assert HealthReason.HIGH_CHALLENGE_SHARE not in reasons(current)

    def test_min_count_in_small_markets(self) -> None:
        # 2 of 10 is 20%, under the share; 2 also under the absolute minimum of 3.
        current = healthy_details(8) + [detail(page_type="paywall", ok=False) for _ in range(2)]
        assert HealthReason.HIGH_CHALLENGE_SHARE not in reasons(current)


class TestUnexpectedPages:
    def _baseline(self) -> Baseline:
        return baseline_from([search(25) for _ in range(12)])

    def test_all_empty_shells_after_populated_search(self) -> None:
        # Classic drift: the site switched to client-side rendering; nothing parses as a search page.
        shells = [
            search(0, page_type="empty_shell", access_state=AccessState.UNEXPECTED_CONTENT, ok=False)
            for _ in range(20)
        ]
        result = assess_detailed(shells, self._baseline())
        assert HealthReason.HIGH_UNEXPECTED_PAGE_SHARE in result.reasons
        assert result.health.status == "unhealthy"
        assert result.health.metrics["unexpected_page_count"] == "20"
        assert RecommendedAction.QUARANTINE_NEW_REVISIONS in result.recommended_actions
        # Fires without a baseline as well: an app shell is never a valid page.
        assert HealthReason.HIGH_UNEXPECTED_PAGE_SHARE in reasons(shells, None)

    def test_unknown_page_type_counts(self) -> None:
        current = healthy_details(12) + [detail(page_type="unknown", ok=False) for _ in range(8)]
        assert HealthReason.HIGH_UNEXPECTED_PAGE_SHARE in reasons(current)

    def test_unexpected_content_on_detail_pages_counts(self) -> None:
        current = healthy_details(12) + [
            detail(access_state=AccessState.UNEXPECTED_CONTENT, ok=False) for _ in range(8)
        ]
        assert HealthReason.HIGH_UNEXPECTED_PAGE_SHARE in reasons(current)

    def test_occasional_shell_tolerated(self) -> None:
        current = healthy_details(18) + [
            detail(page_type="empty_shell", access_state=AccessState.UNEXPECTED_CONTENT, ok=False)
            for _ in range(2)
        ]
        assert HealthReason.HIGH_UNEXPECTED_PAGE_SHARE not in reasons(current)

    def test_min_count_in_small_markets(self) -> None:
        # 2 of 10 is under both the absolute minimum (3) and the share (0.30).
        current = healthy_details(8) + [detail(page_type="empty_shell", ok=False) for _ in range(2)]
        assert HealthReason.HIGH_UNEXPECTED_PAGE_SHARE not in reasons(current)

    def test_challenge_pages_are_not_double_counted(self) -> None:
        current = healthy_details(12) + [
            detail(page_type="challenge", access_state=AccessState.ACCESS_BLOCKED, ok=False) for _ in range(8)
        ]
        found = reasons(current)
        assert HealthReason.HIGH_CHALLENGE_SHARE in found
        assert HealthReason.HIGH_UNEXPECTED_PAGE_SHARE not in found

    def test_removed_and_not_found_pages_are_answers(self) -> None:
        current = healthy_details(10) + [
            detail(page_type="removed", access_state=AccessState.REMOVED, ok=True) for _ in range(5)
        ]
        current += [
            detail(page_type="detail", access_state=AccessState.NOT_FOUND, ok=False) for _ in range(5)
        ]
        assert HealthReason.HIGH_UNEXPECTED_PAGE_SHARE not in reasons(current)


class TestUnexpectedHosts:
    def test_positive(self) -> None:
        current = healthy_details(17) + [detail(unexpected_host=True) for _ in range(3)]
        assert HealthReason.UNEXPECTED_HOSTS in reasons(current)

    def test_single_ad_card_tolerated(self) -> None:
        current = [*healthy_details(19), detail(unexpected_host=True)]
        assert HealthReason.UNEXPECTED_HOSTS not in reasons(current)


class TestParseFailures:
    def test_positive(self) -> None:
        current = healthy_details(8) + [detail(i, ok=False) for i in range(12)]
        assert HealthReason.HIGH_PARSE_FAILURE_SHARE in reasons(current)

    def test_negative(self) -> None:
        current = healthy_details(18) + [detail(i, ok=False) for i in range(2)]
        assert HealthReason.HIGH_PARSE_FAILURE_SHARE not in reasons(current)


class TestPagination:
    def _base(self) -> Baseline:
        return baseline_from([search(20) for _ in range(12)], [signal() for _ in range(12)])

    def test_missing_markers(self) -> None:
        current = [search(20) for _ in range(12)]
        signals = [signal(marker=False) for _ in range(12)]
        assert HealthReason.MISSING_PAGINATION_MARKERS in reasons(current, self._base(), page_signals=signals)

    def test_markers_present(self) -> None:
        current = [search(20) for _ in range(12)]
        assert reasons(current, self._base(), page_signals=[signal() for _ in range(12)]) == ()

    def test_unexplained_result_count_change(self) -> None:
        current = [search(20) for _ in range(12)]
        signals = [signal(reported=40) for _ in range(12)]
        result = assess_detailed(current, self._base(), page_signals=signals)
        assert result.reasons == (HealthReason.RESULT_COUNT_CHANGE,)
        assert result.health.status == "degraded"

    def test_small_absolute_change_in_low_volume_market(self) -> None:
        base = baseline_from([search(6) for _ in range(12)], [signal(6, reported=12) for _ in range(12)])
        current = [search(6) for _ in range(12)]
        signals = [signal(6, reported=3) for _ in range(12)]  # ratio 0.25 but only 9 fewer
        assert reasons(current, base, page_signals=signals) == ()

    def test_needs_enough_pages(self) -> None:
        current = [search(20) for _ in range(12)]
        signals = [signal(marker=False, reported=1) for _ in range(2)]
        assert reasons(current, self._base(), page_signals=signals) == ()


class TestThresholdsAndActions:
    def test_thresholds_are_labelled_engineering_defaults(self) -> None:
        assert DEFAULT_THRESHOLDS.label == "engineering_default"

    def test_custom_thresholds(self) -> None:
        strict = HealthThresholds(label="owner_reviewed", min_samples=3, challenge_min_count=1)
        current = [detail(i) for i in range(3)] + [detail(page_type="challenge", ok=False) for _ in range(2)]
        result = assess_detailed(current, None, strict)
        assert HealthReason.HIGH_CHALLENGE_SHARE in result.reasons
        assert result.thresholds_label == "owner_reviewed"

    def test_actions_never_remove_listings(self) -> None:
        values = {a.value for a in RecommendedAction}
        assert values == {"pause_new_alerts", "quarantine_new_revisions"}
        assert recommended_actions("healthy") == ()
        assert recommended_actions("insufficient_sample") == ()

    def test_every_reason_has_severity(self) -> None:
        assert set(SEVERITY) == set(HealthReason) - {HealthReason.INSUFFICIENT_SAMPLE}

    def test_assess_returns_shared_model(self) -> None:
        health = assess(healthy_details(), None)
        assert health.status == "healthy"
        assert health.sample_size == 20

    @pytest.mark.parametrize("bad", [-1])
    def test_signal_validation(self, bad: int) -> None:
        with pytest.raises(ValueError):
            SearchPageSignal(listing_count=bad, pagination_marker_present=True, observed_at=NOW)

    def test_naive_signal_time_rejected(self) -> None:
        with pytest.raises(ValueError):
            SearchPageSignal(
                listing_count=1,
                pagination_marker_present=True,
                observed_at=datetime(2026, 10, 6),
            )

    def test_baseline_contents(self) -> None:
        base = baseline_from(
            [search(10), search(20), *healthy_details(10)], [signal(marker=True), signal(marker=False)]
        )
        assert base.sample_size == 12
        assert base.search_sample_count == 2
        assert base.mean_listings_per_search_page == Decimal(15)
        assert base.currency_shares == {"EUR": Decimal(1)}
        assert base.pagination_marker_share == Decimal("0.5")
        assert base.median_result_count_reported == Decimal(240)
