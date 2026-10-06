"""Simple per-adapter parser-health assessment (spec section 25).

`crawling/parser_health.py` (another work package) will own baseline comparison
across runs. This local implementation only judges one batch of samples and
uses conservative, labelled ENGINEERING DEFAULT thresholds. It requires a minimum
sample size so low-volume markets do not trigger noisy statistical claims.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from suv_deals.adapters.base import ParseOutcome, ParserHealth
from suv_deals.domain.enums import AccessState

HealthStatus = Literal["healthy", "degraded", "unhealthy", "insufficient_sample"]

_BLOCKED_PAGE_TYPES = frozenset({"challenge", "login", "paywall"})


@dataclass(frozen=True, slots=True)
class HealthThresholds:
    """ENGINEERING DEFAULTS, not statistically validated. Tune per source with evidence."""

    min_sample_size: int = 5
    challenge_rate_unhealthy: Decimal = Decimal("0.30")
    challenge_rate_degraded: Decimal = Decimal("0.10")
    unexpected_host_rate_unhealthy: Decimal = Decimal("0.20")
    parse_failure_rate_unhealthy: Decimal = Decimal("0.50")
    parse_failure_rate_degraded: Decimal = Decimal("0.20")
    coverage_unhealthy: Decimal = Decimal("0.50")
    coverage_degraded: Decimal = Decimal("0.80")
    min_priced_for_uniformity: int = 5
    implausible_low_price_minor: int = 10_000  # below 100.00 in a 2-decimal currency


DEFAULT_THRESHOLDS = HealthThresholds()


def _rate(part: int, whole: int) -> Decimal:
    return Decimal(part) / Decimal(whole) if whole else Decimal(0)


def _fmt(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.001'))}"


def assess_samples(samples: list[ParseOutcome], thresholds: HealthThresholds = DEFAULT_THRESHOLDS) -> ParserHealth:
    """Assess a batch of parse outcomes. Pure and deterministic."""
    size = len(samples)
    if size < thresholds.min_sample_size:
        return ParserHealth(
            status="insufficient_sample",
            sample_size=size,
            reasons=(f"fewer than {thresholds.min_sample_size} samples",),
        )
    reasons_unhealthy: list[str] = []
    reasons_degraded: list[str] = []
    metrics: dict[str, str] = {"sample_size": str(size)}

    blocked = sum(
        1 for s in samples if s.access_state == AccessState.ACCESS_BLOCKED or s.page_type in _BLOCKED_PAGE_TYPES
    )
    challenge_rate = _rate(blocked, size)
    metrics["challenge_rate"] = _fmt(challenge_rate)
    if challenge_rate >= thresholds.challenge_rate_unhealthy:
        reasons_unhealthy.append("high proportion of login/challenge/blocked pages")
    elif challenge_rate >= thresholds.challenge_rate_degraded:
        reasons_degraded.append("elevated proportion of login/challenge/blocked pages")

    unexpected = sum(1 for s in samples if s.unexpected_host)
    unexpected_rate = _rate(unexpected, size)
    metrics["unexpected_host_rate"] = _fmt(unexpected_rate)
    if unexpected_rate >= thresholds.unexpected_host_rate_unhealthy:
        reasons_unhealthy.append("search cards point to unexpected hosts or paths")
    elif unexpected:
        reasons_degraded.append("some links point to unexpected hosts or paths")

    reachable = [s for s in samples if s.access_state == AccessState.OK]
    failures = sum(1 for s in reachable if not s.ok)
    failure_rate = _rate(failures, len(reachable))
    metrics["parse_failure_rate"] = _fmt(failure_rate)
    if reachable and failure_rate >= thresholds.parse_failure_rate_unhealthy:
        reasons_unhealthy.append("high parse failure rate on reachable pages")
    elif reachable and failure_rate >= thresholds.parse_failure_rate_degraded:
        reasons_degraded.append("elevated parse failure rate on reachable pages")

    details = [s for s in reachable if s.page_type == "detail" and s.ok]
    if details:
        for name, attr in (("price", "has_price"), ("mileage", "has_mileage"), ("make_model", "has_make_model")):
            coverage = _rate(sum(1 for s in details if getattr(s, attr)), len(details))
            metrics[f"{name}_coverage"] = _fmt(coverage)
            if len(details) >= thresholds.min_sample_size:
                if coverage < thresholds.coverage_unhealthy:
                    reasons_unhealthy.append(f"{name} extraction coverage fell below {thresholds.coverage_unhealthy}")
                elif coverage < thresholds.coverage_degraded:
                    reasons_degraded.append(f"{name} extraction coverage below {thresholds.coverage_degraded}")

    priced = [s for s in reachable if s.price_minor is not None]
    currencies = Counter(s.currency for s in priced if s.currency)
    metrics["currencies"] = ",".join(f"{c}:{n}" for c, n in sorted(currencies.items()))
    if len(currencies) > 1:
        reasons_degraded.append("mixed currencies in one batch")
    if len(priced) >= thresholds.min_priced_for_uniformity:
        values = {s.price_minor for s in priced}
        if len(values) == 1:
            reasons_unhealthy.append("all extracted prices are identical")
        low = sum(1 for s in priced if (s.price_minor or 0) < thresholds.implausible_low_price_minor)
        if _rate(low, len(priced)) >= Decimal("0.5"):
            reasons_unhealthy.append("most extracted prices are implausibly low")

    search = [s for s in reachable if s.page_type == "search"]
    if search:
        metrics["search_pages"] = str(len(search))
        metrics["search_listing_total"] = str(sum(s.listing_count for s in search))

    status: HealthStatus = "healthy"
    if reasons_unhealthy:
        status = "unhealthy"
    elif reasons_degraded:
        status = "degraded"
    return ParserHealth(
        status=status,
        sample_size=size,
        reasons=tuple(reasons_unhealthy + reasons_degraded),
        metrics=metrics,
    )
