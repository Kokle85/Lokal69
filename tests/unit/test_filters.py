"""Unit tests for domain.filters (spec 3, 14, 17, 18 FX rules; spec 31 Eligibility/Mileage/Prices/FX).

Every listing, price, rate and URL here is SYNTHETIC test data, not a real offer or a real rate.
"""

from __future__ import annotations

import decimal
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from suv_deals.domain.enums import (
    Availability,
    BodyType,
    EligibilityState,
    FxPurpose,
    OdometerClaim,
    PriceBasis,
    PriceType,
    ProfileKey,
    SellerType,
    Tristate,
    VatTreatment,
)
from suv_deals.domain.filters import (
    SCREENING_VERSION,
    ReasonCode,
    ReasonSeverity,
    ScreeningResult,
    screen,
    select_reference_rate,
)
from suv_deals.domain.listings import LocationInfo, MileageOriginal, NormalizedListing, PriceInfo, VehicleSpec
from suv_deals.domain.money import FxRate
from suv_deals.domain.parsing import parse_mileage
from suv_deals.domain.profiles import BusinessConfig, load_business_config
from suv_deals.domain.provenance import FieldConflict
from suv_deals.domain.taxonomy import VehicleTaxonomy, default_taxonomy

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
AS_OF = date(2026, 10, 6)
OBSERVED = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)


def business_config(*, manual: bool = False, below: bool = False) -> BusinessConfig:
    return load_business_config(
        CONFIG_DIR,
        overrides={
            "profiles": {"manual_4000": {"enabled": manual}, "below_target_watch": {"enabled": below}}
        },
    )


DEFAULT_CONFIG = business_config()


def eur_minor(amount: str) -> int:
    return int(Decimal(amount) * 100)


def listing(
    price: dict[str, Any] | None = None,
    vehicle: dict[str, Any] | None = None,
    **overrides: Any,
) -> NormalizedListing:
    """A synthetic, otherwise-eligible listing: VW Tiguan, EUR 2,750 gross, 187,500 km, DE."""
    price_data: dict[str, Any] = {
        "amount_minor": 275000,
        "currency": "EUR",
        "basis": PriceBasis.GROSS,
        "type": PriceType.FULL_VEHICLE_ASKING,
        "required_seller_fees_known": Tristate.NO,
    }
    price_data.update(price or {})
    vehicle_data: dict[str, Any] = {
        "make": "Volkswagen",
        "model": "Tiguan",
        "body_type": BodyType.SUV,
        "mileage_km": Decimal("187500"),
        "mileage_claim": OdometerClaim.SELLER_REPORTED,
    }
    vehicle_data.update(vehicle or {})
    data: dict[str, Any] = {
        "source_key": "fixture_dealer_de",
        "source_listing_id": "SYNTH-204",
        "canonical_url": "https://dealer.example/vehicles/SYNTH-204",
        "observed_at": OBSERVED,
        "location": LocationInfo(country="DE"),
        "availability": Availability.AVAILABLE,
        "vehicle": VehicleSpec(**vehicle_data),
        "price": PriceInfo(**price_data),
        "parser_version": "fixture@1.0.0",
    }
    data.update(overrides)
    return NormalizedListing(**data)


def run(
    item: NormalizedListing,
    *,
    config: BusinessConfig = DEFAULT_CONFIG,
    rates: list[FxRate] | None = None,
    taxonomy: VehicleTaxonomy | None = None,
    use_taxonomy: bool = True,
) -> ScreeningResult:
    return screen(
        item, config, rates or [], AS_OF, (taxonomy or default_taxonomy()) if use_taxonomy else None
    )


def chf_rate(rate: str = "0.93", rate_date: date = AS_OF, purpose: FxPurpose = FxPurpose.REFERENCE) -> FxRate:
    """SYNTHETIC rate: 1 EUR = <rate> CHF (ECB-style direction)."""
    return FxRate(
        base="EUR",
        quote="CHF",
        rate=Decimal(rate),
        rate_date=rate_date,
        retrieved_at=datetime(2026, 10, 6, 6, 0, tzinfo=UTC),
        provider="synthetic-test",
        purpose=purpose,
    )


def chf_listing(amount: str) -> NormalizedListing:
    return listing(
        price={"amount_minor": eur_minor(amount), "currency": "CHF"}, location=LocationInfo(country="CH")
    )


# ---------------------------------------------------------------------------------------------
# Primary band boundaries (inclusive at both ends)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "state", "code"),
    [
        ("2499.99", EligibilityState.REJECTED, ReasonCode.PRICE_BELOW_BAND),
        ("2500.00", EligibilityState.ELIGIBLE_PRIMARY, ReasonCode.PRICE_IN_BAND),
        ("2750.00", EligibilityState.ELIGIBLE_PRIMARY, ReasonCode.PRICE_IN_BAND),
        ("3000.00", EligibilityState.ELIGIBLE_PRIMARY, ReasonCode.PRICE_IN_BAND),
        ("3000.01", EligibilityState.REJECTED, ReasonCode.PRICE_ABOVE_BAND),
    ],
)
def test_primary_band_boundaries(amount: str, state: EligibilityState, code: ReasonCode) -> None:
    result = run(listing(price={"amount_minor": eur_minor(amount)}))
    assert result.state == state
    assert code in result.codes()
    assert result.eur_amount == Decimal(amount)


def test_eligible_primary_result_shape() -> None:
    result = run(listing())
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert result.profile == ProfileKey.PRIMARY
    assert result.queue_label == "Primary queue"
    assert result.payable_amount is not None and result.payable_amount.amount == Decimal("2750.00")
    assert result.fx_rate_used is None
    assert result.missing_facts == ()
    assert result.screening_version == SCREENING_VERSION
    assert result.taxonomy_match is not None and result.taxonomy_match.model == "Tiguan"
    assert not any(r.severity in (ReasonSeverity.REJECT, ReasonSeverity.NEEDS_FACTS) for r in result.reasons)


# ---------------------------------------------------------------------------------------------
# Mileage
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("km", "state"),
    [
        ("199999", EligibilityState.ELIGIBLE_PRIMARY),
        ("199999.999", EligibilityState.ELIGIBLE_PRIMARY),
        ("200000", EligibilityState.REJECTED),
        ("200000.001", EligibilityState.REJECTED),
        ("250000", EligibilityState.REJECTED),
    ],
)
def test_mileage_strictly_below_200000(km: str, state: EligibilityState) -> None:
    result = run(listing(vehicle={"mileage_km": Decimal(km)}))
    assert result.state == state
    if state == EligibilityState.REJECTED:
        assert ReasonCode.MILEAGE_TOO_HIGH in result.codes()


@pytest.mark.parametrize(
    ("text", "state"),
    [("124.274 mi", EligibilityState.ELIGIBLE_PRIMARY), ("124.275 mi", EligibilityState.REJECTED)],
)
def test_miles_boundary_after_exact_conversion(text: str, state: EligibilityState) -> None:
    parsed = parse_mileage(text, "de")
    item = listing(
        vehicle={"mileage_km": parsed.km, "mileage_original": parsed.original, "mileage_claim": parsed.claim}
    )
    assert run(item).state == state


def test_locale_parsed_199_999_km_passes() -> None:
    parsed = parse_mileage("199.999 km", "de")
    item = listing(
        vehicle={"mileage_km": parsed.km, "mileage_original": parsed.original, "mileage_claim": parsed.claim}
    )
    assert run(item).state == EligibilityState.ELIGIBLE_PRIMARY


def test_missing_mileage_needs_facts() -> None:
    result = run(listing(vehicle={"mileage_km": None, "mileage_claim": OdometerClaim.UNKNOWN}))
    assert result.state == EligibilityState.NEEDS_FACTS
    assert ReasonCode.MILEAGE_MISSING in result.codes()
    assert "vehicle.mileage_km" in result.missing_facts
    assert result.profile == ProfileKey.PRIMARY
    assert result.queue_label == "Primary queue"


def test_range_only_mileage_needs_facts() -> None:
    parsed = parse_mileage("150.000 - 160.000 km", "de")
    item = listing(
        vehicle={"mileage_km": None, "mileage_original": parsed.original, "mileage_claim": parsed.claim}
    )
    result = run(item)
    assert result.state == EligibilityState.NEEDS_FACTS
    assert ReasonCode.MILEAGE_RANGE_ONLY in result.codes()


def test_range_starting_above_limit_rejects() -> None:
    original = MileageOriginal(unit="km", range_low=Decimal("200000"), range_high=Decimal("210000"))
    item = listing(
        vehicle={"mileage_km": None, "mileage_original": original, "mileage_claim": OdometerClaim.RANGE_ONLY}
    )
    assert run(item).state == EligibilityState.REJECTED


def test_range_in_miles_starting_above_limit_rejects() -> None:
    original = MileageOriginal(unit="mi", range_low=Decimal("125000"), range_high=Decimal("130000"))
    item = listing(
        vehicle={"mileage_km": None, "mileage_original": original, "mileage_claim": OdometerClaim.RANGE_ONLY}
    )
    assert run(item).state == EligibilityState.REJECTED


def test_title_vs_spec_mileage_conflict_needs_facts() -> None:
    conflict = FieldConflict(
        field="vehicle.mileage_km", values=["120000", "187500"], locations=["title", "specification table"]
    )
    result = run(listing(conflicts=(conflict,)))
    assert result.state == EligibilityState.NEEDS_FACTS
    assert ReasonCode.MILEAGE_CONFLICT in result.codes()


def test_mileage_conflict_all_above_limit_rejects() -> None:
    conflict = FieldConflict(field="vehicle.mileage_km", values=["205000", "210000"])
    item = listing(vehicle={"mileage_km": Decimal("210000")}, conflicts=(conflict,))
    assert run(item).state == EligibilityState.REJECTED


def test_mileage_conflict_unparseable_values_need_facts() -> None:
    conflict = FieldConflict(field="vehicle.mileage_km", values=["ca. 210.000", "210000"])
    assert run(listing(conflicts=(conflict,))).state == EligibilityState.NEEDS_FACTS


def test_conflicting_claim_status_needs_facts() -> None:
    result = run(listing(vehicle={"mileage_claim": OdometerClaim.CONFLICTING}))
    assert result.state == EligibilityState.NEEDS_FACTS


@pytest.mark.parametrize(
    ("km", "state", "code"),
    [
        ("179999", EligibilityState.ELIGIBLE_PRIMARY, ReasonCode.MILEAGE_ESTIMATE),
        ("180000", EligibilityState.NEEDS_FACTS, ReasonCode.MILEAGE_ESTIMATE_NEAR_LIMIT),
        ("199999", EligibilityState.NEEDS_FACTS, ReasonCode.MILEAGE_ESTIMATE_NEAR_LIMIT),
        ("219999", EligibilityState.NEEDS_FACTS, ReasonCode.MILEAGE_ESTIMATE_NEAR_LIMIT),
        ("220000", EligibilityState.REJECTED, ReasonCode.MILEAGE_TOO_HIGH),
    ],
)
def test_estimated_mileage(km: str, state: EligibilityState, code: ReasonCode) -> None:
    result = run(listing(vehicle={"mileage_km": Decimal(km), "mileage_claim": OdometerClaim.ESTIMATED}))
    assert result.state == state
    assert code in result.codes()


def test_estimate_flag_in_original_counts_as_estimate() -> None:
    item = listing(
        vehicle={"mileage_km": Decimal("185000"), "mileage_original": MileageOriginal(is_estimate=True)}
    )
    assert run(item).state == EligibilityState.NEEDS_FACTS


def test_unknown_claim_status_is_warning() -> None:
    result = run(listing(vehicle={"mileage_claim": OdometerClaim.UNKNOWN}))
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert ReasonCode.MILEAGE_CLAIM_UNKNOWN in result.codes()


# ---------------------------------------------------------------------------------------------
# Price types and basis
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("price_type", "state", "code"),
    [
        (PriceType.INSTALMENT, EligibilityState.NEEDS_FACTS, ReasonCode.PRICE_TYPE_INSTALMENT),
        (PriceType.LEASING, EligibilityState.NEEDS_FACTS, ReasonCode.PRICE_TYPE_LEASING),
        (PriceType.DEPOSIT, EligibilityState.NEEDS_FACTS, ReasonCode.PRICE_TYPE_DEPOSIT),
        (PriceType.PRICE_ON_REQUEST, EligibilityState.NEEDS_FACTS, ReasonCode.PRICE_ON_REQUEST),
        (PriceType.AUCTION_START, EligibilityState.REJECTED, ReasonCode.PRICE_TYPE_AUCTION),
        (PriceType.AUCTION_CURRENT_BID, EligibilityState.REJECTED, ReasonCode.PRICE_TYPE_AUCTION),
        (PriceType.PARTS_OR_DAMAGED, EligibilityState.REJECTED, ReasonCode.PRICE_TYPE_PARTS_OR_DAMAGED),
        (PriceType.UNKNOWN, EligibilityState.NEEDS_FACTS, ReasonCode.PRICE_TYPE_UNKNOWN),
        (PriceType.EXPORT_NET, EligibilityState.NEEDS_FACTS, ReasonCode.PRICE_TYPE_EXPORT_NET),
    ],
)
def test_non_asking_price_types(price_type: PriceType, state: EligibilityState, code: ReasonCode) -> None:
    result = run(listing(price={"type": price_type, "amount_minor": 9900}))
    assert result.state == state
    assert code in result.codes()
    assert result.eur_amount is None or price_type == PriceType.EXPORT_NET


def test_instalment_amount_in_band_is_not_eligible() -> None:
    result = run(listing(price={"type": PriceType.INSTALMENT, "amount_minor": 275000}))
    assert result.state == EligibilityState.NEEDS_FACTS
    assert result.eur_amount is None


def test_export_net_with_stated_gross_uses_gross() -> None:
    item = listing(
        price={
            "type": PriceType.EXPORT_NET,
            "basis": PriceBasis.NET,
            "amount_minor": 230000,
            "gross_amount_minor": 290000,
        }
    )
    result = run(item)
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert result.eur_amount == Decimal("2900.00")
    assert ReasonCode.PRICE_EXPORT_NET_GROSS_USED in result.codes()


def test_export_net_above_band_rejects() -> None:
    result = run(
        listing(price={"type": PriceType.EXPORT_NET, "basis": PriceBasis.NET, "amount_minor": 310000})
    )
    assert result.state == EligibilityState.REJECTED


def test_net_only_needs_facts() -> None:
    result = run(listing(price={"basis": PriceBasis.NET, "amount_minor": 240000}))
    assert result.state == EligibilityState.NEEDS_FACTS
    assert ReasonCode.PRICE_BASIS_NET_ONLY in result.codes()
    assert "price.gross_amount_minor" in result.missing_facts
    assert result.eur_amount is None


def test_net_only_with_net_above_max_rejects() -> None:
    result = run(listing(price={"basis": PriceBasis.NET, "amount_minor": 300001}))
    assert result.state == EligibilityState.REJECTED
    assert ReasonCode.PRICE_ABOVE_BAND in result.codes()


def test_net_with_stated_gross_uses_gross() -> None:
    result = run(
        listing(price={"basis": PriceBasis.NET, "amount_minor": 231100, "gross_amount_minor": 275000})
    )
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert result.eur_amount == Decimal("2750.00")
    assert ReasonCode.PRICE_GROSS_STATED_USED in result.codes()


def test_unknown_basis_dealer_needs_facts() -> None:
    result = run(listing(price={"basis": PriceBasis.UNKNOWN}, seller_type=SellerType.DEALER))
    assert result.state == EligibilityState.NEEDS_FACTS
    assert ReasonCode.PRICE_BASIS_UNKNOWN in result.codes()


def test_unknown_basis_above_max_rejects() -> None:
    result = run(listing(price={"basis": PriceBasis.UNKNOWN, "amount_minor": 350000}))
    assert result.state == EligibilityState.REJECTED


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        (
            {"price": {"basis": PriceBasis.UNKNOWN, "vat_treatment": VatTreatment.MARGIN_SCHEME}},
            ReasonCode.PRICE_BASIS_ASSUMED_NO_VAT,
        ),
        (
            {"price": {"basis": PriceBasis.UNKNOWN, "vat_treatment": VatTreatment.PRIVATE_SALE}},
            ReasonCode.PRICE_BASIS_ASSUMED_NO_VAT,
        ),
        (
            {"price": {"basis": PriceBasis.UNKNOWN}, "seller_type": SellerType.PRIVATE},
            ReasonCode.PRICE_BASIS_ASSUMED_NO_VAT,
        ),
        (
            {"price": {"basis": PriceBasis.UNKNOWN, "vat_treatment": VatTreatment.VAT_SHOWN}},
            ReasonCode.PRICE_BASIS_GROSS_FROM_VAT_WORDING,
        ),
    ],
)
def test_unknown_basis_with_vat_wording(overrides: dict[str, Any], code: ReasonCode) -> None:
    result = run(listing(**overrides))
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert code in result.codes()


def test_missing_price_needs_facts() -> None:
    result = run(listing(price={"amount_minor": None, "currency": None}))
    assert result.state == EligibilityState.NEEDS_FACTS
    assert ReasonCode.PRICE_MISSING in result.codes()
    assert "price.amount_minor" in result.missing_facts


def test_negotiable_is_screened_on_asking() -> None:
    result = run(listing(price={"negotiable": Tristate.YES, "amount_minor": 300000}))
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert ReasonCode.PRICE_NEGOTIABLE in result.codes()


def test_refundable_deposit_is_info_only() -> None:
    result = run(listing(price={"refundable_deposit_minor": 50000}))
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert ReasonCode.REFUNDABLE_DEPOSIT_STATED in result.codes()


# ---------------------------------------------------------------------------------------------
# Seller fees
# ---------------------------------------------------------------------------------------------


def test_known_required_fees_are_added() -> None:
    result = run(
        listing(
            price={
                "amount_minor": 295000,
                "required_seller_fees_known": Tristate.YES,
                "required_seller_fees_minor": 10000,
            }
        )
    )
    assert result.state == EligibilityState.REJECTED  # 2,950 + 100 = 3,050
    assert result.eur_amount == Decimal("3050.00")
    assert ReasonCode.SELLER_FEES_INCLUDED in result.codes()


def test_fees_bring_price_into_band() -> None:
    result = run(
        listing(
            price={
                "amount_minor": 245000,
                "required_seller_fees_known": Tristate.YES,
                "required_seller_fees_minor": 5000,
            }
        )
    )
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert result.eur_amount == Decimal("2500.00")


def test_required_fee_with_unknown_amount_needs_facts() -> None:
    result = run(listing(price={"required_seller_fees_known": Tristate.YES}))
    assert result.state == EligibilityState.NEEDS_FACTS
    assert ReasonCode.SELLER_FEE_AMOUNT_UNKNOWN in result.codes()


def test_unknown_fees_are_warning_only() -> None:
    result = run(listing(price={"required_seller_fees_known": Tristate.UNKNOWN}))
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert ReasonCode.SELLER_FEES_UNKNOWN in result.codes()


# ---------------------------------------------------------------------------------------------
# FX (rate direction, staleness near boundaries, missing)
# ---------------------------------------------------------------------------------------------


def test_chf_converted_by_division() -> None:
    result = run(chf_listing("2600.00"), rates=[chf_rate("0.93")])
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    expected = decimal.Context(prec=40).divide(Decimal("2600.00"), Decimal("0.93"))
    assert result.eur_amount == expected  # unrounded (~2795.6989...), never multiplied (2418.00)
    assert result.eur_amount is not None
    exponent = result.eur_amount.as_tuple().exponent
    assert isinstance(exponent, int) and exponent < -2
    assert result.fx_rate_used is not None
    assert ReasonCode.FX_CONVERTED in result.codes()


def test_inverse_rate_direction_is_honoured() -> None:
    inverse = FxRate(
        base="CHF",
        quote="EUR",
        rate=Decimal("1.075"),
        rate_date=AS_OF,
        retrieved_at=datetime(2026, 10, 6, 6, 0, tzinfo=UTC),
        provider="synthetic-test",
    )
    result = run(chf_listing("2600.00"), rates=[inverse])
    assert result.eur_amount == Decimal("2795.000")


@pytest.mark.parametrize(
    ("chf", "state"), [("2790.00", EligibilityState.ELIGIBLE_PRIMARY), ("2790.01", EligibilityState.REJECTED)]
)
def test_chf_upper_boundary_unrounded(chf: str, state: EligibilityState) -> None:
    result = run(chf_listing(chf), rates=[chf_rate("0.93")])
    assert result.state == state
    if state == EligibilityState.REJECTED:
        assert result.eur_amount is not None and result.eur_amount > Decimal("3000")
        assert result.eur_amount.quantize(Decimal("0.01")) == Decimal("3000.01")


def test_chf_lower_boundary() -> None:
    assert run(chf_listing("2325.00"), rates=[chf_rate("0.93")]).state == EligibilityState.ELIGIBLE_PRIMARY
    assert run(chf_listing("2324.99"), rates=[chf_rate("0.93")]).state == EligibilityState.REJECTED


def test_missing_fx_needs_facts() -> None:
    result = run(chf_listing("2600.00"))
    assert result.state == EligibilityState.NEEDS_FACTS
    assert ReasonCode.FX_MISSING in result.codes()
    assert "fx:CHF/EUR" in result.missing_facts


def test_stale_fx_near_boundary_needs_facts() -> None:
    stale = chf_rate("0.93", rate_date=date(2026, 9, 20))  # 16 days > 7
    result = run(chf_listing("2790.00"), rates=[stale])
    assert result.state == EligibilityState.NEEDS_FACTS
    assert ReasonCode.FX_STALE_NEAR_BOUNDARY in result.codes()


def test_stale_fx_far_from_boundary_is_warning() -> None:
    stale = chf_rate("0.93", rate_date=date(2026, 9, 20))
    result = run(chf_listing("2600.00"), rates=[stale])
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert ReasonCode.FX_STALE in result.codes()


def test_stale_fx_far_above_band_still_rejects() -> None:
    stale = chf_rate("0.93", rate_date=date(2026, 9, 20))
    assert run(chf_listing("9000.00"), rates=[stale]).state == EligibilityState.REJECTED


def test_fresh_fx_at_max_age_is_not_stale() -> None:
    edge = chf_rate("0.93", rate_date=date(2026, 9, 29))  # exactly 7 days
    result = run(chf_listing("2790.00"), rates=[edge])
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert ReasonCode.FX_STALE not in result.codes()


def test_latest_reference_rate_selected_and_others_ignored() -> None:
    older = chf_rate("0.95", rate_date=date(2026, 10, 1))
    newer = chf_rate("0.93", rate_date=date(2026, 10, 5))
    payment = chf_rate("0.80", purpose=FxPurpose.PAYMENT)
    future = chf_rate("0.50", rate_date=date(2026, 10, 9))
    rate, ignored_future = select_reference_rate([older, payment, newer, future], "CHF", AS_OF)
    assert rate == newer
    assert ignored_future
    result = run(chf_listing("2600.00"), rates=[older, payment, newer, future])
    assert result.fx_rate_used == newer
    assert ReasonCode.FX_RATE_FUTURE_DATED in result.codes()


def test_only_payment_rate_counts_as_missing() -> None:
    result = run(chf_listing("2600.00"), rates=[chf_rate(purpose=FxPurpose.PAYMENT)])
    assert ReasonCode.FX_MISSING in result.codes()


# ---------------------------------------------------------------------------------------------
# SUV identity, country, availability
# ---------------------------------------------------------------------------------------------


def test_non_suv_body_rejected() -> None:
    result = run(listing(vehicle={"model": "Golf", "body_type": BodyType.HATCHBACK}))
    assert result.state == EligibilityState.REJECTED
    assert ReasonCode.NOT_SUV in result.codes()


def test_unknown_model_and_body_needs_facts() -> None:
    result = run(listing(vehicle={"model": "Golf", "body_type": BodyType.UNKNOWN}))
    assert result.state == EligibilityState.NEEDS_FACTS
    assert ReasonCode.SUV_IDENTITY_UNKNOWN in result.codes()


def test_unmatched_model_with_suv_body_passes_with_warning() -> None:
    result = run(listing(vehicle={"make": "Example", "model": "Trail", "body_type": BodyType.SUV}))
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert ReasonCode.TAXONOMY_UNMATCHED in result.codes()


def test_taxonomy_match_passes_without_body_type() -> None:
    result = run(listing(vehicle={"body_type": BodyType.UNKNOWN}))
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY


def test_taxonomy_exclusion_beats_body_type() -> None:
    result = run(listing(vehicle={"make": "Nissan", "model": "Navara", "body_type": BodyType.SUV}))
    assert result.state == EligibilityState.REJECTED
    assert {ReasonCode.NOT_SUV, ReasonCode.BODY_TYPE_CONFLICTS_TAXONOMY} <= result.codes()


def test_taxonomy_suv_with_contradicting_body_warns() -> None:
    result = run(listing(vehicle={"body_type": BodyType.SEDAN}))
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert ReasonCode.BODY_TYPE_CONFLICTS_TAXONOMY in result.codes()


def test_without_taxonomy_body_type_decides() -> None:
    result = run(listing(), use_taxonomy=False)
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert ReasonCode.TAXONOMY_NOT_SUPPLIED in result.codes()
    assert result.taxonomy_match is None
    unknown = run(listing(vehicle={"body_type": BodyType.UNKNOWN}), use_taxonomy=False)
    assert unknown.state == EligibilityState.NEEDS_FACTS


@pytest.mark.parametrize(
    ("country", "state"),
    [
        ("DE", EligibilityState.ELIGIBLE_PRIMARY),
        ("IT", EligibilityState.ELIGIBLE_PRIMARY),
        ("FR", EligibilityState.REJECTED),
        (None, EligibilityState.NEEDS_FACTS),
    ],
)
def test_country_rule(country: str | None, state: EligibilityState) -> None:
    assert run(listing(location=LocationInfo(country=country))).state == state


@pytest.mark.parametrize(
    ("availability", "state", "code"),
    [
        (Availability.REMOVED, EligibilityState.REJECTED, ReasonCode.AVAILABILITY_REMOVED),
        (Availability.SOLD_CLAIMED, EligibilityState.REJECTED, ReasonCode.AVAILABILITY_SOLD_CLAIMED),
        (Availability.RESERVED, EligibilityState.NEEDS_FACTS, ReasonCode.AVAILABILITY_RESERVED),
        (Availability.UNKNOWN, EligibilityState.ELIGIBLE_PRIMARY, ReasonCode.AVAILABILITY_UNKNOWN),
    ],
)
def test_availability(availability: Availability, state: EligibilityState, code: ReasonCode) -> None:
    result = run(listing(availability=availability))
    assert result.state == state
    assert code in result.codes()


# ---------------------------------------------------------------------------------------------
# Profiles: manual_4000 and below_target_watch (disabled by default)
# ---------------------------------------------------------------------------------------------


def test_above_primary_with_manual_disabled_is_rejected_but_recorded() -> None:
    result = run(listing(price={"amount_minor": 350000}))
    assert result.state == EligibilityState.REJECTED
    infos = [r for r in result.reasons if r.code == ReasonCode.DISABLED_PROFILE_WOULD_MATCH]
    assert [r.profile for r in infos] == [ProfileKey.MANUAL_4000]
    assert infos[0].severity == ReasonSeverity.INFO


@pytest.mark.parametrize(
    ("amount", "state"),
    [
        ("3000.01", EligibilityState.ELIGIBLE_MANUAL_PROFILE),
        ("4000.00", EligibilityState.ELIGIBLE_MANUAL_PROFILE),
        ("4000.01", EligibilityState.REJECTED),
    ],
)
def test_manual_4000_when_enabled(amount: str, state: EligibilityState) -> None:
    result = run(listing(price={"amount_minor": eur_minor(amount)}), config=business_config(manual=True))
    assert result.state == state
    if state == EligibilityState.ELIGIBLE_MANUAL_PROFILE:
        assert result.profile == ProfileKey.MANUAL_4000
        assert result.queue_label == "Manual EUR 4,000 review queue"


def test_in_band_listing_stays_primary_when_manual_enabled() -> None:
    result = run(listing(price={"amount_minor": 300000}), config=business_config(manual=True))
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert result.profile == ProfileKey.PRIMARY


def test_below_target_disabled_records_option() -> None:
    result = run(listing(price={"amount_minor": 240000}))
    assert result.state == EligibilityState.REJECTED
    assert ReasonCode.DISABLED_PROFILE_WOULD_MATCH in result.codes()
    assert any(r.profile == ProfileKey.BELOW_TARGET_WATCH for r in result.reasons)


@pytest.mark.parametrize(
    ("amount", "state", "profile"),
    [
        ("2499.99", EligibilityState.ELIGIBLE_MANUAL_PROFILE, ProfileKey.BELOW_TARGET_WATCH),
        ("1000.00", EligibilityState.ELIGIBLE_MANUAL_PROFILE, ProfileKey.BELOW_TARGET_WATCH),
        (
            "2500.00",
            EligibilityState.ELIGIBLE_PRIMARY,
            ProfileKey.PRIMARY,
        ),  # exclusive upper bound: no gap, no overlap
    ],
)
def test_below_target_watch_when_enabled(amount: str, state: EligibilityState, profile: ProfileKey) -> None:
    result = run(listing(price={"amount_minor": eur_minor(amount)}), config=business_config(below=True))
    assert result.state == state
    assert result.profile == profile
    if profile == ProfileKey.BELOW_TARGET_WATCH:
        assert result.queue_label == "Below-target watch queue"


def test_disabled_profile_might_match_when_facts_missing() -> None:
    result = run(listing(price={"amount_minor": 350000}, vehicle={"mileage_km": None}))
    assert result.state == EligibilityState.REJECTED
    assert ReasonCode.DISABLED_PROFILE_MIGHT_MATCH in result.codes()


# ---------------------------------------------------------------------------------------------
# needs_facts vs rejected precedence
# ---------------------------------------------------------------------------------------------


def test_independent_reject_beats_missing_fact() -> None:
    result = run(
        listing(price={"amount_minor": None, "currency": None}, vehicle={"mileage_km": Decimal("250000")})
    )
    assert result.state == EligibilityState.REJECTED
    assert ReasonCode.MILEAGE_TOO_HIGH in result.codes()


def test_price_reject_beats_missing_mileage() -> None:
    result = run(listing(price={"amount_minor": 350000}, vehicle={"mileage_km": None}))
    assert result.state == EligibilityState.REJECTED


def test_enabled_manual_profile_needing_facts() -> None:
    result = run(
        listing(price={"amount_minor": 350000}, vehicle={"mileage_km": None}),
        config=business_config(manual=True),
    )
    assert result.state == EligibilityState.NEEDS_FACTS
    assert result.profile == ProfileKey.MANUAL_4000
    assert result.queue_label == "Manual EUR 4,000 review queue"
    assert "vehicle.mileage_km" in result.missing_facts
    assert any(
        r.code == ReasonCode.PRICE_ABOVE_BAND and r.profile == ProfileKey.PRIMARY for r in result.reasons
    )


def test_missing_fact_needed_by_rejecting_rule_gives_needs_facts() -> None:
    result = run(listing(price={"basis": PriceBasis.NET, "amount_minor": 280000}))
    assert result.state == EligibilityState.NEEDS_FACTS  # gross may be <= 3,000 or above it


def test_profile_evaluations_are_reported() -> None:
    result = run(listing())
    assert [e.profile for e in result.profile_evaluations] == [
        ProfileKey.PRIMARY,
        ProfileKey.MANUAL_4000,
        ProfileKey.BELOW_TARGET_WATCH,
    ]
    assert [e.enabled for e in result.profile_evaluations] == [True, False, False]
