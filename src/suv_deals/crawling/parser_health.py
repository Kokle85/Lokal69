"""Parser-health tripwires and automatic safety pauses (spec section 25).

A successful HTTP response is not proof of valid extraction. `assess()` compares a
window of recent `ParseOutcome` samples (and optional per-search-page signals) with a
baseline built from recent healthy samples and returns the shared `ParserHealth`.

All thresholds are CONSERVATIVE ENGINEERING DEFAULTS (`HealthThresholds.label`), not
statistically validated values. Every tripwire needs a minimum sample size and both a
relative and an absolute threshold, so low-volume markets do not raise noisy alarms.
Below `min_samples` the status is `insufficient_sample` and nothing else is claimed.

Tripwires (typed `HealthReason` codes):

| Code | Needs baseline | Severity |
|---|---|---|
| near_zero_listings | yes | unhealthy |
| price_coverage_drop / mileage_coverage_drop | yes | unhealthy |
| identical_prices | no | unhealthy |
| implausible_low_prices (< ~EUR 300 equivalent) | no | unhealthy |
| high_challenge_share (login/challenge/paywall/blocked) | no | unhealthy |
| high_unexpected_page_share (empty shell/unknown page/unexpected content) | no | unhealthy |
| unexpected_hosts (cards pointing off-policy) | no | unhealthy |
| high_parse_failure_share | no | unhealthy |
| currency_distribution_change | yes | degraded |
| result_count_change | yes | degraded |
| missing_pagination_markers | yes | degraded |

Recommended actions are limited to `pause_new_alerts` (degraded and unhealthy) and
`quarantine_new_revisions` (unhealthy). There is deliberately no action that removes
listings or overwrites reliable fields with nulls; prior evidence is kept.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from statistics import median
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from suv_deals.adapters.base import ParseOutcome, ParserHealth
from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import AccessState

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_CONTENT_PAGES: Final = frozenset({"detail", "search"})
_CHALLENGE_PAGES: Final = frozenset({"challenge", "login", "paywall"})
# Page-type detection failures: an app shell or an unrecognised page where content was expected.
_UNEXPECTED_PAGES: Final = frozenset({"empty_shell", "unknown"})
_QUANTUM: Final = Decimal("0.001")


class HealthReason(StrEnum):
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    NEAR_ZERO_LISTINGS = "near_zero_listings"
    PRICE_COVERAGE_DROP = "price_coverage_drop"
    MILEAGE_COVERAGE_DROP = "mileage_coverage_drop"
    IDENTICAL_PRICES = "identical_prices"
    IMPLAUSIBLE_LOW_PRICES = "implausible_low_prices"
    CURRENCY_DISTRIBUTION_CHANGE = "currency_distribution_change"
    HIGH_CHALLENGE_SHARE = "high_challenge_share"
    HIGH_UNEXPECTED_PAGE_SHARE = "high_unexpected_page_share"
    UNEXPECTED_HOSTS = "unexpected_hosts"
    HIGH_PARSE_FAILURE_SHARE = "high_parse_failure_share"
    RESULT_COUNT_CHANGE = "result_count_change"
    MISSING_PAGINATION_MARKERS = "missing_pagination_markers"


class RecommendedAction(StrEnum):
    PAUSE_NEW_ALERTS = "pause_new_alerts"
    QUARANTINE_NEW_REVISIONS = "quarantine_new_revisions"


SEVERITY: Final[dict[HealthReason, Literal["degraded", "unhealthy"]]] = {
    HealthReason.NEAR_ZERO_LISTINGS: "unhealthy",
    HealthReason.PRICE_COVERAGE_DROP: "unhealthy",
    HealthReason.MILEAGE_COVERAGE_DROP: "unhealthy",
    HealthReason.IDENTICAL_PRICES: "unhealthy",
    HealthReason.IMPLAUSIBLE_LOW_PRICES: "unhealthy",
    HealthReason.HIGH_CHALLENGE_SHARE: "unhealthy",
    HealthReason.HIGH_UNEXPECTED_PAGE_SHARE: "unhealthy",
    HealthReason.UNEXPECTED_HOSTS: "unhealthy",
    HealthReason.HIGH_PARSE_FAILURE_SHARE: "unhealthy",
    HealthReason.CURRENCY_DISTRIBUTION_CHANGE: "degraded",
    HealthReason.RESULT_COUNT_CHANGE: "degraded",
    HealthReason.MISSING_PAGINATION_MARKERS: "degraded",
}

# Approximately EUR 300 in minor units per currency. Rough, deliberately conservative
# engineering defaults for a tripwire only; never used for valuation or FX.
DEFAULT_LOW_PRICE_FLOOR_MINOR: Final[dict[str, int]] = {
    "EUR": 30_000,
    "CHF": 28_000,
    "GBP": 25_000,
    "MKD": 1_800_000,
    "PLN": 125_000,
    "CZK": 750_000,
    "HUF": 11_500_000,
    "RON": 150_000,
    "BGN": 58_000,
    "RSD": 3_500_000,
    "SEK": 340_000,
    "DKK": 223_000,
    "NOK": 350_000,
    "USD": 32_000,
}


class HealthThresholds(BaseModel):
    """Tripwire thresholds. ENGINEERING DEFAULTS, conservative and not statistically validated."""

    model_config = _FROZEN

    label: str = "engineering_default"
    min_samples: int = Field(default=10, ge=1)
    min_search_samples: int = Field(default=3, ge=1)
    min_priced_samples: int = Field(default=8, ge=2)
    # near-zero listings on a previously populated search
    baseline_min_listings_per_page: Decimal = Decimal(5)
    near_zero_ratio: Decimal = Decimal("0.1")
    near_zero_abs: Decimal = Decimal(1)
    # coverage drops: both an absolute drop and a relative ratio are required
    coverage_abs_drop: Decimal = Decimal("0.30")
    coverage_rel_ratio: Decimal = Decimal("0.60")
    # price sanity
    identical_price_share: Decimal = Decimal("0.80")
    low_price_floor_minor: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_LOW_PRICE_FLOOR_MINOR))
    low_price_share: Decimal = Decimal("0.30")
    low_price_min_count: int = Field(default=3, ge=1)
    # currency distribution: total variation distance between share vectors
    currency_tvd: Decimal = Decimal("0.50")
    # access/challenge pages
    challenge_share: Decimal = Decimal("0.30")
    challenge_min_count: int = Field(default=3, ge=1)
    # empty app shells / unknown page types / unexpected content (e.g. client-side rendering switch)
    unexpected_page_share: Decimal = Decimal("0.30")
    unexpected_page_min_count: int = Field(default=3, ge=1)
    # cards pointing off-policy
    unexpected_host_min_count: int = Field(default=2, ge=1)
    unexpected_host_share: Decimal = Decimal("0.05")
    # parse failures among content pages
    parse_failure_share: Decimal = Decimal("0.50")
    parse_failure_min_count: int = Field(default=3, ge=1)
    # unexplained result-count / page-size changes
    result_count_ratio_low: Decimal = Decimal("0.33")
    result_count_ratio_high: Decimal = Decimal(3)
    result_count_abs_change: Decimal = Decimal(20)
    listings_per_page_ratio_low: Decimal = Decimal("0.5")
    listings_per_page_abs_change: Decimal = Decimal(5)
    # pagination markers
    pagination_baseline_share: Decimal = Decimal("0.70")
    pagination_current_share: Decimal = Decimal("0.20")


DEFAULT_THRESHOLDS: Final = HealthThresholds()


class SearchPageSignal(BaseModel):
    """Per search page: what parser health needs beyond `ParseOutcome`."""

    model_config = _FROZEN

    listing_count: int = Field(ge=0)
    pagination_marker_present: bool
    result_count_reported: int | None = Field(default=None, ge=0)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class Baseline(BaseModel):
    """Summary of recent healthy samples for one adapter/search."""

    model_config = _FROZEN

    built_at: datetime
    sample_size: int = Field(ge=0)
    content_sample_count: int = Field(ge=0)
    search_sample_count: int = Field(ge=0)
    priced_sample_count: int = Field(ge=0)
    page_signal_count: int = Field(default=0, ge=0)
    mean_listings_per_search_page: Decimal | None = None
    price_coverage: Decimal | None = None
    mileage_coverage: Decimal | None = None
    currency_shares: dict[str, Decimal] = Field(default_factory=dict)
    pagination_marker_share: Decimal | None = None
    median_result_count_reported: Decimal | None = None

    @field_validator("built_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ParserHealthAssessment(BaseModel):
    """`ParserHealth` plus typed reasons and the safe actions it recommends."""

    model_config = _FROZEN

    health: ParserHealth
    reasons: tuple[HealthReason, ...]
    recommended_actions: tuple[RecommendedAction, ...]
    thresholds_label: str
    baseline_used: bool


# ---------------------------------------------------------------------------
# Statistics


class _Stats:
    def __init__(self, samples: Sequence[ParseOutcome], signals: Sequence[SearchPageSignal]) -> None:
        self.n = len(samples)
        self.content = [
            s for s in samples if s.access_state == AccessState.OK and s.page_type in _CONTENT_PAGES
        ]
        self.search = [s for s in self.content if s.page_type == "search"]
        self.priced = [s for s in self.content if s.price_minor is not None and s.currency]
        self.challenge_count = sum(
            1
            for s in samples
            if s.page_type in _CHALLENGE_PAGES or s.access_state == AccessState.ACCESS_BLOCKED
        )
        self.unexpected_count = sum(1 for s in samples if s.unexpected_host)
        self.unexpected_page_count = sum(
            1
            for s in samples
            if s.page_type not in _CHALLENGE_PAGES
            and s.access_state != AccessState.ACCESS_BLOCKED
            and (s.page_type in _UNEXPECTED_PAGES or s.access_state == AccessState.UNEXPECTED_CONTENT)
        )
        self.parse_failures = sum(1 for s in self.content if not s.ok)
        self.price_coverage = _share(sum(1 for s in self.content if s.has_price), len(self.content))
        self.mileage_coverage = _share(sum(1 for s in self.content if s.has_mileage), len(self.content))
        self.mean_listings = (
            Decimal(sum(s.listing_count for s in self.search)) / Decimal(len(self.search))
            if self.search
            else None
        )
        currency_counts = Counter(s.currency for s in self.priced if s.currency)
        total_priced = sum(currency_counts.values())
        self.currency_shares = {c: Decimal(k) / Decimal(total_priced) for c, k in currency_counts.items()}
        self.signals = list(signals)
        self.pagination_share = _share(sum(1 for p in signals if p.pagination_marker_present), len(signals))
        reported = [p.result_count_reported for p in signals if p.result_count_reported is not None]
        self.median_reported = Decimal(str(median(reported))) if reported else None


def _share(count: int, total: int) -> Decimal | None:
    return Decimal(count) / Decimal(total) if total else None


def _fmt(value: Decimal | None) -> str:
    return "n/a" if value is None else str(value.quantize(_QUANTUM, rounding=ROUND_HALF_UP))


def build_baseline(
    samples: Iterable[ParseOutcome],
    page_signals: Iterable[SearchPageSignal] = (),
    *,
    built_at: datetime,
) -> Baseline:
    """Baseline from recent samples that were judged healthy (caller selects them)."""
    stats = _Stats(list(samples), list(page_signals))
    return Baseline(
        built_at=built_at,
        sample_size=stats.n,
        content_sample_count=len(stats.content),
        search_sample_count=len(stats.search),
        priced_sample_count=len(stats.priced),
        page_signal_count=len(stats.signals),
        mean_listings_per_search_page=stats.mean_listings,
        price_coverage=stats.price_coverage,
        mileage_coverage=stats.mileage_coverage,
        currency_shares=stats.currency_shares,
        pagination_marker_share=stats.pagination_share,
        median_result_count_reported=stats.median_reported,
    )


# ---------------------------------------------------------------------------
# Tripwires


def _coverage_dropped(current: Decimal | None, baseline: Decimal | None, t: HealthThresholds) -> bool:
    if current is None or baseline is None:
        return False
    return baseline - current >= t.coverage_abs_drop and current <= baseline * t.coverage_rel_ratio


def _total_variation(a: dict[str, Decimal], b: dict[str, Decimal]) -> Decimal:
    keys = set(a) | set(b)
    return sum((abs(a.get(k, Decimal(0)) - b.get(k, Decimal(0))) for k in keys), Decimal(0)) / 2


def _baseline_reasons(stats: _Stats, baseline: Baseline, t: HealthThresholds) -> list[HealthReason]:
    reasons: list[HealthReason] = []
    near_zero = False
    base_listings = baseline.mean_listings_per_search_page
    if (
        base_listings is not None
        and baseline.search_sample_count >= t.min_search_samples
        and base_listings >= t.baseline_min_listings_per_page
        and len(stats.search) >= t.min_search_samples
        and stats.mean_listings is not None
    ):
        limit = max(t.near_zero_abs, base_listings * t.near_zero_ratio)
        if stats.mean_listings <= limit:
            near_zero = True
            reasons.append(HealthReason.NEAR_ZERO_LISTINGS)
        elif (
            stats.mean_listings <= base_listings * t.listings_per_page_ratio_low
            and base_listings - stats.mean_listings >= t.listings_per_page_abs_change
        ):
            reasons.append(HealthReason.RESULT_COUNT_CHANGE)

    if len(stats.content) >= t.min_samples and baseline.content_sample_count >= t.min_samples:
        if _coverage_dropped(stats.price_coverage, baseline.price_coverage, t):
            reasons.append(HealthReason.PRICE_COVERAGE_DROP)
        if _coverage_dropped(stats.mileage_coverage, baseline.mileage_coverage, t):
            reasons.append(HealthReason.MILEAGE_COVERAGE_DROP)

    if (
        len(stats.priced) >= t.min_priced_samples
        and baseline.priced_sample_count >= t.min_priced_samples
        and baseline.currency_shares
        and _total_variation(stats.currency_shares, baseline.currency_shares) >= t.currency_tvd
    ):
        reasons.append(HealthReason.CURRENCY_DISTRIBUTION_CHANGE)

    enough_pages = (
        len(stats.signals) >= t.min_search_samples and baseline.page_signal_count >= t.min_search_samples
    )
    if (
        enough_pages
        and baseline.pagination_marker_share is not None
        and stats.pagination_share is not None
        and baseline.pagination_marker_share >= t.pagination_baseline_share
        and stats.pagination_share <= t.pagination_current_share
    ):
        reasons.append(HealthReason.MISSING_PAGINATION_MARKERS)
    base_reported = baseline.median_result_count_reported
    if (
        enough_pages
        and not near_zero
        and HealthReason.RESULT_COUNT_CHANGE not in reasons
        and base_reported is not None
        and stats.median_reported is not None
        and base_reported > 0
    ):
        ratio = stats.median_reported / base_reported
        changed = ratio <= t.result_count_ratio_low or ratio >= t.result_count_ratio_high
        if changed and abs(stats.median_reported - base_reported) >= t.result_count_abs_change:
            reasons.append(HealthReason.RESULT_COUNT_CHANGE)
    return reasons


def _standalone_reasons(stats: _Stats, t: HealthThresholds) -> list[HealthReason]:
    reasons: list[HealthReason] = []
    if len(stats.priced) >= t.min_priced_samples:
        price_counts = Counter((s.currency, s.price_minor) for s in stats.priced)
        top = price_counts.most_common(1)[0][1]
        if Decimal(top) / Decimal(len(stats.priced)) >= t.identical_price_share:
            reasons.append(HealthReason.IDENTICAL_PRICES)
        floors = t.low_price_floor_minor
        judged = [s for s in stats.priced if s.currency in floors]
        low = sum(1 for s in judged if s.price_minor is not None and s.price_minor < floors[s.currency or ""])
        if (
            judged
            and low >= t.low_price_min_count
            and Decimal(low) / Decimal(len(judged)) >= t.low_price_share
        ):
            reasons.append(HealthReason.IMPLAUSIBLE_LOW_PRICES)
    if stats.challenge_count >= t.challenge_min_count and (
        Decimal(stats.challenge_count) / Decimal(stats.n) >= t.challenge_share
    ):
        reasons.append(HealthReason.HIGH_CHALLENGE_SHARE)
    if stats.unexpected_page_count >= t.unexpected_page_min_count and (
        Decimal(stats.unexpected_page_count) / Decimal(stats.n) >= t.unexpected_page_share
    ):
        reasons.append(HealthReason.HIGH_UNEXPECTED_PAGE_SHARE)
    if stats.unexpected_count >= t.unexpected_host_min_count and (
        Decimal(stats.unexpected_count) / Decimal(stats.n) >= t.unexpected_host_share
    ):
        reasons.append(HealthReason.UNEXPECTED_HOSTS)
    if (
        stats.content
        and stats.parse_failures >= t.parse_failure_min_count
        and Decimal(stats.parse_failures) / Decimal(len(stats.content)) >= t.parse_failure_share
    ):
        reasons.append(HealthReason.HIGH_PARSE_FAILURE_SHARE)
    return reasons


def recommended_actions(status: str) -> tuple[RecommendedAction, ...]:
    if status == "unhealthy":
        return (RecommendedAction.PAUSE_NEW_ALERTS, RecommendedAction.QUARANTINE_NEW_REVISIONS)
    if status == "degraded":
        return (RecommendedAction.PAUSE_NEW_ALERTS,)
    return ()


def assess_detailed(
    samples: Sequence[ParseOutcome],
    baseline: Baseline | None,
    thresholds: HealthThresholds = DEFAULT_THRESHOLDS,
    *,
    page_signals: Sequence[SearchPageSignal] = (),
) -> ParserHealthAssessment:
    t = thresholds
    stats = _Stats(samples, page_signals)
    baseline_used = baseline is not None and baseline.sample_size >= t.min_samples
    metrics = {
        "thresholds": t.label,
        "sample_size": str(stats.n),
        "content_samples": str(len(stats.content)),
        "search_samples": str(len(stats.search)),
        "priced_samples": str(len(stats.priced)),
        "price_coverage": _fmt(stats.price_coverage),
        "mileage_coverage": _fmt(stats.mileage_coverage),
        "mean_listings_per_search_page": _fmt(stats.mean_listings),
        "challenge_count": str(stats.challenge_count),
        "unexpected_host_count": str(stats.unexpected_count),
        "unexpected_page_count": str(stats.unexpected_page_count),
        "parse_failures": str(stats.parse_failures),
        "baseline_used": "true" if baseline_used else "false",
    }
    if baseline is not None:
        metrics["baseline_price_coverage"] = _fmt(baseline.price_coverage)
        metrics["baseline_mileage_coverage"] = _fmt(baseline.mileage_coverage)
        metrics["baseline_mean_listings_per_search_page"] = _fmt(baseline.mean_listings_per_search_page)

    if stats.n < t.min_samples:
        health = ParserHealth(
            status="insufficient_sample",
            sample_size=stats.n,
            reasons=(HealthReason.INSUFFICIENT_SAMPLE.value,),
            metrics=metrics,
        )
        return ParserHealthAssessment(
            health=health,
            reasons=(HealthReason.INSUFFICIENT_SAMPLE,),
            recommended_actions=(),
            thresholds_label=t.label,
            baseline_used=False,
        )

    reasons = _standalone_reasons(stats, t)
    if baseline is not None and baseline_used:
        reasons.extend(_baseline_reasons(stats, baseline, t))
    ordered = tuple(dict.fromkeys(reasons))
    if any(SEVERITY[r] == "unhealthy" for r in ordered):
        status: Literal["healthy", "degraded", "unhealthy"] = "unhealthy"
    elif ordered:
        status = "degraded"
    else:
        status = "healthy"
    health = ParserHealth(
        status=status, sample_size=stats.n, reasons=tuple(r.value for r in ordered), metrics=metrics
    )
    return ParserHealthAssessment(
        health=health,
        reasons=ordered,
        recommended_actions=recommended_actions(status),
        thresholds_label=t.label,
        baseline_used=baseline_used,
    )


def assess(
    samples: Sequence[ParseOutcome],
    baseline: Baseline | None,
    thresholds: HealthThresholds = DEFAULT_THRESHOLDS,
    *,
    page_signals: Sequence[SearchPageSignal] = (),
) -> ParserHealth:
    return assess_detailed(samples, baseline, thresholds, page_signals=page_signals).health
