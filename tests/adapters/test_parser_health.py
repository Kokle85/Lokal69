"""Local parser-health tripwires (spec section 25). Thresholds are labelled engineering defaults."""

from __future__ import annotations

from decimal import Decimal

from tests.adapters.conftest import NOW, DetailFn

from suv_deals.adapters._health import HealthThresholds, assess_samples
from suv_deals.adapters.base import ParseOutcome
from suv_deals.adapters.dealer_inventory import SchemaOrgDealerAdapter
from suv_deals.domain.enums import AccessState


def _detail(
    price: int | None = 275000, mileage: bool = True, ok: bool = True, currency: str = "EUR"
) -> ParseOutcome:
    return ParseOutcome(
        page_type="detail",
        access_state=AccessState.OK,
        ok=ok,
        listing_count=1 if ok else 0,
        has_price=price is not None,
        has_mileage=mileage,
        has_make_model=True,
        currency=currency if price is not None else None,
        price_minor=price,
        mileage_km=Decimal(150000) if mileage else None,
        observed_at=NOW,
    )


def _blocked() -> ParseOutcome:
    return ParseOutcome(
        page_type="challenge", access_state=AccessState.ACCESS_BLOCKED, ok=False, observed_at=NOW
    )


def test_insufficient_sample() -> None:
    health = assess_samples([_detail()] * 4)
    assert health.status == "insufficient_sample" and health.sample_size == 4


def test_healthy_batch() -> None:
    samples = [_detail(price=250000 + i * 1000) for i in range(6)]
    health = assess_samples(samples)
    assert health.status == "healthy", health.reasons
    assert health.metrics["price_coverage"] == "1.000"


def test_challenge_rate_trips() -> None:
    samples = [_detail(price=250000 + i) for i in range(5)] + [_blocked()] * 3
    health = assess_samples(samples)
    assert health.status == "unhealthy"
    assert any("challenge" in r for r in health.reasons)


def test_identical_and_implausible_prices_trip() -> None:
    assert assess_samples([_detail(price=100000)] * 6).status == "unhealthy"
    low = assess_samples([_detail(price=100 + i) for i in range(6)])
    assert any("implausibly low" in r for r in low.reasons)


def test_coverage_drop_and_parse_failures() -> None:
    no_mileage = assess_samples([_detail(price=250000 + i, mileage=False) for i in range(6)])
    assert no_mileage.status == "unhealthy" and any("mileage" in r for r in no_mileage.reasons)
    failures = assess_samples([_detail(price=250000 + i) for i in range(3)] + [_detail(ok=False)] * 3)
    assert failures.status == "unhealthy" and any("parse failure" in r for r in failures.reasons)


def test_unexpected_hosts_and_mixed_currencies_degrade() -> None:
    off_host = ParseOutcome(
        page_type="search",
        access_state=AccessState.OK,
        ok=True,
        listing_count=3,
        unexpected_host=True,
        observed_at=NOW,
    )
    samples = [_detail(price=250000 + i) for i in range(9)] + [off_host]
    assert assess_samples(samples).status == "degraded"
    mixed = [_detail(price=250000 + i) for i in range(4)] + [_detail(price=300000, currency="CHF")] * 1
    mixed.append(_detail(price=310000, currency="CHF"))
    assert assess_samples(mixed).status == "degraded"


def test_custom_thresholds() -> None:
    strict = HealthThresholds(min_sample_size=2)
    assert assess_samples([_detail(price=1), _detail(price=2)], strict).sample_size == 2


async def test_adapter_health_over_fixture_outcomes(
    de_adapter: SchemaOrgDealerAdapter, detail: DetailFn
) -> None:
    urls = [f"https://dealer.example/fahrzeug/TEST-{n}" for n in (204, 205, 206, 207, 208, 211, 213)]
    outcomes = [de_adapter.detail_outcome(await detail(de_adapter, url), NOW) for url in urls]
    health = de_adapter.assess_parser_health(outcomes)
    assert health.status in {"healthy", "degraded"}
    blocked = [
        de_adapter.detail_outcome(await detail(de_adapter, "https://dealer.example/fahrzeug/TEST-215"), NOW)
    ]
    worse = de_adapter.assess_parser_health(outcomes + blocked * 4)
    assert worse.status == "unhealthy"
