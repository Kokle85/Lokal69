"""Unit tests for domain.ranking (spec 14, 18). All listings and amounts are SYNTHETIC."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from suv_deals.domain.enums import ClaimStatus, Drive, Fuel, Gearbox, OdometerClaim
from suv_deals.domain.listings import (
    ConditionClaims,
    Documentation,
    NormalizedListing,
    PartialDate,
    PriceInfo,
    VehicleSpec,
)
from suv_deals.domain.ranking import (
    FEATURE_ORDER,
    MAX_POINTS,
    NOT_A_PROBABILITY,
    SCORING_VERSION,
    RankingFeatures,
    completeness_from_listing,
    rank_candidate,
    rank_candidates,
    risk_flags_from_listing,
)
from suv_deals.errors import ValidationFailed

AS_OF = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)


def features(**overrides: Any) -> RankingFeatures:
    data: dict[str, Any] = {
        "listing_id": UUID(int=1),
        "as_of": AS_OF,
        "acquisition_price_eur": Decimal("2750"),
        "comparable_status": "adequate",
        "mk_band_fit": "within",
        "valuation_complete": True,
        "conservative_contribution_eur": Decimal("1300"),
        "known_required_facts": 9,
        "total_required_facts": 18,
        "last_checked_at": AS_OF - timedelta(hours=2),
    }
    data.update(overrides)
    return RankingFeatures(**data)


def points(result: Any, feature: str) -> Decimal:
    return result.contribution(feature).points  # type: ignore[no-any-return]


def test_full_breakdown_is_visible_and_sums_to_total() -> None:
    result = rank_candidate(features())
    assert result.scoring_version == SCORING_VERSION
    assert [c.feature for c in result.contributions] == list(FEATURE_ORDER)
    assert result.total == sum(c.points for c in result.contributions)
    assert points(result, "acquisition_fit") == Decimal("20.00")
    assert points(result, "comparable_quality") == Decimal("20.00")
    assert points(result, "resale_band_fit") == Decimal("10.00")
    assert points(result, "conservative_scenario") == Decimal("13.00")
    assert points(result, "data_completeness") == Decimal("5.00")
    assert points(result, "freshness") == Decimal("10.00")
    assert result.total == Decimal("78.00")
    assert result.priority == 7800


def test_not_a_probability_label() -> None:
    result = rank_candidate(features())
    assert result.label == NOT_A_PROBABILITY
    assert "not a probability of profit" in result.label
    assert result.is_probability is False


def test_deterministic_for_identical_input() -> None:
    assert rank_candidate(features()) == rank_candidate(features())


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ("2500.00", "20.00"),
        ("3000.00", "20.00"),
        ("2499.99", "10.00"),
        ("3050.00", "19.00"),
        ("4000.00", "0.00"),
        ("5000.00", "0.00"),
    ],
)
def test_acquisition_fit(price: str, expected: str) -> None:
    result = rank_candidate(features(acquisition_price_eur=Decimal(price)))
    assert points(result, "acquisition_fit") == Decimal(expected)


def test_unknown_values_earn_no_points() -> None:
    result = rank_candidate(
        features(
            acquisition_price_eur=None,
            comparable_status=None,
            mk_band_fit="unknown",
            valuation_complete=False,
            conservative_contribution_eur=None,
            known_required_facts=0,
            last_checked_at=None,
        )
    )
    assert result.total == Decimal("0.00")
    assert "unknown" in result.contribution("acquisition_fit").explanation
    assert "valuation incomplete" in result.contribution("conservative_scenario").explanation


def test_contribution_requires_complete_valuation() -> None:
    with pytest.raises(ValueError):
        features(valuation_complete=False, conservative_contribution_eur=Decimal("1000"))


@pytest.mark.parametrize(
    ("contribution", "expected"),
    [
        ("-500", "0.00"),
        ("0", "0.00"),
        ("1500", "15.00"),
        ("2999", "29.99"),
        ("3000", "30.00"),
        ("9000", "30.00"),
    ],
)
def test_conservative_scenario_clamped(contribution: str, expected: str) -> None:
    result = rank_candidate(features(conservative_contribution_eur=Decimal(contribution)))
    assert points(result, "conservative_scenario") == Decimal(expected)


@pytest.mark.parametrize(
    ("status", "fit", "quality", "band"),
    [
        ("adequate", "within", "20.00", "10.00"),
        ("small_sample", "above", "8.00", "6.00"),
        ("small_sample", "below", "8.00", "0.00"),
        ("insufficient_comparables", "within", "0.00", "0.00"),
        (None, "within", "0.00", "0.00"),
    ],
)
def test_comparable_quality_and_band(status: str | None, fit: str, quality: str, band: str) -> None:
    result = rank_candidate(features(comparable_status=status, mk_band_fit=fit))
    assert points(result, "comparable_quality") == Decimal(quality)
    assert points(result, "resale_band_fit") == Decimal(band)


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(hours=24), "10.00"),
        (timedelta(hours=24, seconds=1), "6.00"),
        (timedelta(hours=72), "6.00"),
        (timedelta(days=7), "3.00"),
        (timedelta(days=7, seconds=1), "0.00"),
        (timedelta(seconds=-1), "0.00"),
    ],
)
def test_freshness(age: timedelta, expected: str) -> None:
    result = rank_candidate(features(last_checked_at=AS_OF - age))
    assert points(result, "freshness") == Decimal(expected)


def test_risk_penalties_are_bounded_and_listed() -> None:
    result = rank_candidate(
        features(
            mechanical_risks=(
                "non_running",
                "faults_reported",
                "warning_lights",
                "damaged_vehicle",
                "x",
                "x",
            ),
            document_risks=("vin_missing", "coc_missing", "co2_missing", "registration_documents_missing"),
        )
    )
    assert points(result, "mechanical_risk") == Decimal("-20.00")
    assert points(result, "document_risk") == Decimal("-15.00")
    assert "non_running" in result.contribution("mechanical_risk").explanation
    one = rank_candidate(features(mechanical_risks=("non_running", "non_running")))
    assert points(one, "mechanical_risk") == Decimal("-5.00")


def test_every_contribution_within_documented_bounds() -> None:
    result = rank_candidate(features(acquisition_price_eur=Decimal("100000")))
    for item in result.contributions:
        assert item.min_points <= item.points <= item.max_points
    assert sum(MAX_POINTS.values()) == Decimal(100)


def test_rank_candidates_orders_by_total_then_listing_id() -> None:
    a = features(listing_id=UUID(int=3))
    b = features(listing_id=UUID(int=2))  # identical score -> id tie-break
    c = features(listing_id=UUID(int=1), acquisition_price_eur=None)  # lower score
    ranked = rank_candidates([a, c, b])
    assert [r.listing_id for r in ranked] == [UUID(int=2), UUID(int=3), UUID(int=1)]
    assert rank_candidates([c, b, a]) == ranked


def test_rank_candidates_rejects_duplicate_ids() -> None:
    with pytest.raises(ValidationFailed):
        rank_candidates([features(), features()])


def test_float_inputs_rejected() -> None:
    with pytest.raises(ValueError):
        features(acquisition_price_eur=2750.0)


def test_invalid_fact_counts_rejected() -> None:
    with pytest.raises(ValueError):
        features(known_required_facts=19, total_required_facts=18)


def _listing(**overrides: Any) -> NormalizedListing:
    data: dict[str, Any] = {
        "source_key": "fixture_dealer_de",
        "source_listing_id": "TEST-204",
        "canonical_url": "https://dealer.example/vehicles/TEST-204",
        "observed_at": AS_OF,
        "parser_version": "fixture@1.0.0",
    }
    data.update(overrides)
    return NormalizedListing(**data)


def test_completeness_from_listing() -> None:
    known, total = completeness_from_listing(_listing())
    assert (known, total) == (0, 18)
    full = _listing(
        vehicle=VehicleSpec(
            make="Example",
            model="Trail",
            generation="G2",
            first_registration=PartialDate(value="2011-05", precision="month"),
            fuel=Fuel.DIESEL,
            gearbox=Gearbox.MANUAL,
            drive=Drive.AWD,
            engine_displacement_cm3=1995,
            power_kw=103,
            mileage_km=Decimal("187500"),
            mileage_claim=OdometerClaim.SELLER_REPORTED,
        ),
        price=PriceInfo(amount_minor=275000, currency="EUR", basis="gross", type="full_vehicle_asking"),
    )
    known2, _ = completeness_from_listing(full)
    # price, basis, type, mileage, claim, year, make+model, generation, fuel, gearbox, drive, cm3, kW
    assert known2 == 13


def test_risk_flags_from_listing() -> None:
    listing = _listing(
        condition=ConditionClaims(
            running=ClaimStatus.SELLER_DENIED,
            mechanical_faults=("synthetic: noise from gearbox",),
            accident_free=ClaimStatus.CONFLICTING,
        ),
        documentation=Documentation(coc_available=ClaimStatus.SELLER_DENIED),
    )
    mechanical, document = risk_flags_from_listing(listing)
    assert set(mechanical) == {"non_running", "faults_reported", "accident_claims_conflict"}
    assert set(document) == {"vin_missing", "coc_missing", "co2_missing"}
