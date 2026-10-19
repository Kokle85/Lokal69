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
    Precision,
    PriceBasis,
    PriceType,
    ProfileKey,
    SellerType,
    Tristate,
    VatTreatment,
)
from suv_deals.domain.filters import (
    MIN_AGE_YEARS_FOR_KM_FLOOR,
    MIN_PLAUSIBLE_KM_PER_YEAR,
    MIN_PLAUSIBLE_USED_MILEAGE_KM,
    SCREENING_VERSION,
    ReasonCode,
    ReasonSeverity,
    ScreeningResult,
    screen,
    select_reference_rate,
)
from suv_deals.domain.listings import (
    LocationInfo,
    MileageOriginal,
    NormalizedListing,
    PartialDate,
    PriceInfo,
    VehicleSpec,
)
from suv_deals.domain.money import FxRate
from suv_deals.domain.parsing import parse_mileage, parse_price
from suv_deals.domain.profiles import BusinessConfig, load_business_config
from suv_deals.domain.provenance import FieldConflict
from suv_deals.domain.taxonomy import VehicleTaxonomy, default_taxonomy

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
AS_OF = date(2026, 10, 6)
OBSERVED = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)


def business_config(
    *, manual: bool = False, below: bool = False, require_taxonomy: bool = True
) -> BusinessConfig:
    flag = {"require_taxonomy_match": require_taxonomy}
    return load_business_config(
        CONFIG_DIR,
        overrides={
            "profiles": {
                "primary": flag,
                "manual_4000": {"enabled": manual, **flag},
                "below_target_watch": {"enabled": below, **flag},
            }
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
    as_of: date = AS_OF,
) -> ScreeningResult:
    return screen(
        item, config, rates or [], as_of, (taxonomy or default_taxonomy()) if use_taxonomy else None
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
# Implausibly low mileage (thousands left out by the seller)
# ---------------------------------------------------------------------------------------------


def registered(value: str | None) -> PartialDate:
    if value is None:
        return PartialDate()
    precision = {4: Precision.YEAR, 7: Precision.MONTH, 10: Precision.DAY}[len(value)]
    return PartialDate(value=value, precision=precision)


def low_km(km: str, first_registration: str | None = None, **vehicle: Any) -> NormalizedListing:
    return listing(
        vehicle={"mileage_km": Decimal(km), "first_registration": registered(first_registration), **vehicle}
    )


def implausible_reason(result: ScreeningResult) -> str:
    reason = next(r for r in result.reasons if r.code == ReasonCode.MILEAGE_IMPLAUSIBLY_LOW)
    assert reason.severity == ReasonSeverity.NEEDS_FACTS
    assert reason.field == "vehicle.mileage_km"
    assert reason.profile is None
    return reason.message


def test_floor_constants() -> None:
    assert Decimal("1000") == MIN_PLAUSIBLE_USED_MILEAGE_KM
    assert MIN_AGE_YEARS_FOR_KM_FLOOR == 3
    assert Decimal("2000") == MIN_PLAUSIBLE_KM_PER_YEAR


def test_live_card_qashqai_16_km_needs_facts() -> None:
    # Modelled on the 2026-10-17 AutoScout24 Italy smoke card "Nissan Qashqai 2.0 dCi DPF Tekna",
    # EUR 3,000, card mileage 16 km (the seller left out the thousands). The listing is synthetic.
    def qashqai(km: str) -> NormalizedListing:
        return listing(
            price={"amount_minor": 300000},
            vehicle={
                "make": "Nissan",
                "model": "Qashqai",
                "body_type": BodyType.UNKNOWN,
                "mileage_km": Decimal(km),
                "first_registration": registered("2010"),
            },
            location=LocationInfo(country="IT"),
            availability=Availability.UNKNOWN,
        )

    assert run(qashqai("160000")).state == EligibilityState.ELIGIBLE_PRIMARY  # otherwise eligible
    result = run(qashqai("16"))
    assert result.state == EligibilityState.NEEDS_FACTS
    assert result.profile == ProfileKey.PRIMARY
    assert result.missing_facts == ("vehicle.mileage_km",)
    message = implausible_reason(result)
    assert message.startswith("mileage 16 km is implausibly low for a used car")
    assert "thousands left out" in message and "confirm the odometer reading" in message
    for config in (DEFAULT_CONFIG, business_config(manual=True, below=True)):
        assert run(qashqai("16"), config=config).state == EligibilityState.NEEDS_FACTS


@pytest.mark.parametrize("first_registration", [None, "2025-09"])
@pytest.mark.parametrize(
    ("km", "implausible"),
    [("0.5", True), ("1", True), ("16", True), ("999", True), ("999.999", True), ("1000", False)],
)
def test_absolute_floor(km: str, implausible: bool, first_registration: str | None) -> None:
    # 2025-09 is one full year old on AS_OF: below the age floor's three years, so only the
    # absolute 1,000 km floor can apply.
    result = run(low_km(km, first_registration))
    if implausible:
        assert result.state == EligibilityState.NEEDS_FACTS
        assert implausible_reason(result).startswith(f"mileage {km} km is implausibly low")
        assert "vehicle.mileage_km" in result.missing_facts
    else:
        assert result.state == EligibilityState.ELIGIBLE_PRIMARY
        assert ReasonCode.MILEAGE_IMPLAUSIBLY_LOW not in result.codes()


@pytest.mark.parametrize(
    ("first_registration", "as_of", "km", "implausible"),
    [
        # The "2 full years" rows use 3,999 km (or 1,000 km): below what a floor starting at
        # 2 years would demand (2 x 2,000 = 4,000 km), so they show the age floor is still off,
        # not only that the year count stops short of 3.
        # Month known: a full year counts from the last day of the registration month.
        ("2023-10", date(2026, 10, 31), "5999", True),  # exactly 3 full years, floor 6,000
        ("2023-10", date(2026, 10, 31), "6000", False),  # floor exactly met
        ("2023-10", date(2026, 10, 30), "3999", False),  # 2 full years: no age floor yet
        # Month unknown: counted from 31 December, so the car never looks older than it can be.
        ("2023", date(2026, 12, 31), "5999", True),
        ("2023", date(2026, 12, 31), "6000", False),
        ("2023", date(2026, 12, 30), "3999", False),
        ("2023", date(2026, 10, 31), "3999", False),  # "2023-10" would already be 3 years here
        # Day known.
        ("2023-10-15", date(2026, 10, 15), "5999", True),
        ("2023-10-15", date(2026, 10, 14), "3999", False),
        ("2023-10-15", date(2026, 10, 14), "1000", False),  # only the absolute floor applies
        # Older car: 2010 on AS_OF (2026-10-06) is 15 full years, floor 30,000 km.
        ("2010", AS_OF, "29999", True),
        ("2010", AS_OF, "29999.999", True),
        ("2010", AS_OF, "30000", False),
    ],
)
def test_age_floor_boundaries(first_registration: str, as_of: date, km: str, implausible: bool) -> None:
    result = run(low_km(km, first_registration), as_of=as_of)
    if implausible:
        assert result.state == EligibilityState.NEEDS_FACTS
        assert ReasonCode.MILEAGE_IMPLAUSIBLY_LOW in result.codes()
    else:
        assert result.state == EligibilityState.ELIGIBLE_PRIMARY
        assert ReasonCode.MILEAGE_IMPLAUSIBLY_LOW not in result.codes()


def test_age_floor_message_names_km_registration_and_floor() -> None:
    message = implausible_reason(run(low_km("29999", "2010")))
    assert "mileage 29999 km" in message
    assert "first registered 2010" in message
    assert "15 full years" in message and "at least 30000 km" in message
    assert "confirm the odometer reading" in message


@pytest.mark.parametrize("first_registration", [None, "0000", "2030", "2026-10"])
def test_unknown_impossible_or_future_registration_has_no_age_floor(first_registration: str | None) -> None:
    result = run(low_km("1500", first_registration))
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert ReasonCode.MILEAGE_IMPLAUSIBLY_LOW not in result.codes()


@pytest.mark.parametrize(("text", "registration"), [("ca. 150 km", None), ("ca. 20.000 km", "2010")])
def test_estimated_low_mileage_is_implausible_too(text: str, registration: str | None) -> None:
    parsed = parse_mileage(text, "de")
    assert parsed.claim == OdometerClaim.ESTIMATED
    item = low_km(
        str(parsed.km),
        registration,
        mileage_original=parsed.original,
        mileage_claim=parsed.claim,
    )
    result = run(item)
    assert result.state == EligibilityState.NEEDS_FACTS
    assert ReasonCode.MILEAGE_IMPLAUSIBLY_LOW in result.codes()
    assert ReasonCode.MILEAGE_ESTIMATE not in result.codes()


@pytest.mark.parametrize(
    "overrides",
    [
        {"price": {"amount_minor": 350000}},  # PRICE_ABOVE_BAND
        {"vehicle": {"model": "Golf", "body_type": BodyType.HATCHBACK}},  # NOT_SUV
        {"availability": Availability.SOLD_CLAIMED},
        {"location": LocationInfo(country="FR")},
    ],
)
def test_reject_from_another_rule_beats_implausible_mileage(overrides: dict[str, Any]) -> None:
    rest = {k: v for k, v in overrides.items() if k != "vehicle"}
    result = run(listing(vehicle={"mileage_km": Decimal("16"), **overrides.get("vehicle", {})}, **rest))
    assert result.state == EligibilityState.REJECTED
    assert ReasonCode.MILEAGE_IMPLAUSIBLY_LOW in result.codes()


@pytest.mark.parametrize(
    ("km", "claim"),
    [
        ("200000", OdometerClaim.SELLER_REPORTED),
        ("250000", OdometerClaim.SELLER_REPORTED),
        ("250000", OdometerClaim.ESTIMATED),
    ],
)
def test_implausibly_low_never_replaces_too_high(km: str, claim: OdometerClaim) -> None:
    # A registration year of 1900 puts the age floor above the ceiling (126 years x 2,000 km).
    result = run(low_km(km, "1900", mileage_claim=claim))
    assert result.state == EligibilityState.REJECTED
    assert ReasonCode.MILEAGE_TOO_HIGH in result.codes()
    assert ReasonCode.MILEAGE_IMPLAUSIBLY_LOW not in result.codes()


def test_new_or_demo_offer_wording_does_not_exempt_low_mileage() -> None:
    # NormalizedListing has no new/"km 0"/demo condition field; the AutoScout24 adapter keeps a
    # non-used offerType only as a parser warning, which screening does not read.
    result = run(listing(vehicle={"mileage_km": Decimal("16")}, warnings=("OFFER_TYPE_N",)))
    assert result.state == EligibilityState.NEEDS_FACTS
    assert ReasonCode.MILEAGE_IMPLAUSIBLY_LOW in result.codes()


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


@pytest.mark.parametrize(
    "price",
    [
        {"amount_minor": 0},
        {"amount_minor": 0, "basis": PriceBasis.UNKNOWN},
        {"basis": PriceBasis.NET, "amount_minor": 231100, "gross_amount_minor": 0},
        {"type": PriceType.EXPORT_NET, "basis": PriceBasis.NET, "amount_minor": 0},
    ],
)
def test_zero_price_is_a_placeholder_never_eligible(price: dict[str, Any]) -> None:
    # Unknown is never 0 (spec 2): a "0 €" placeholder must not land in any queue, including the
    # below-target watch whose band (< EUR 2,500) would otherwise contain it.
    for config in (DEFAULT_CONFIG, business_config(manual=True, below=True)):
        result = run(listing(price=price), config=config)
        assert result.state == EligibilityState.NEEDS_FACTS
        assert ReasonCode.PRICE_ZERO_PLACEHOLDER in result.codes()
        assert result.eur_amount is None
        assert "price.amount_minor" in result.missing_facts


def test_zero_mileage_is_a_placeholder() -> None:
    result = run(listing(vehicle={"mileage_km": Decimal("0")}))
    assert result.state == EligibilityState.NEEDS_FACTS
    assert ReasonCode.MILEAGE_ZERO_PLACEHOLDER in result.codes()
    assert ReasonCode.MILEAGE_IMPLAUSIBLY_LOW not in result.codes()  # 0 keeps its own code
    assert "vehicle.mileage_km" in result.missing_facts


def test_parsed_plus_vat_price_is_net_even_for_private_seller() -> None:
    parsed = parse_price("2.750 € + VAT", "de")
    assert parsed.basis == PriceBasis.NET
    item = listing(
        price={
            "amount_minor": parsed.amount_minor,
            "currency": parsed.currency,
            "basis": parsed.basis,
            "type": parsed.price_type,
            "vat_treatment": parsed.vat_treatment,
        },
        seller_type=SellerType.PRIVATE,
    )
    result = run(item)
    assert result.state == EligibilityState.NEEDS_FACTS
    assert ReasonCode.PRICE_BASIS_NET_ONLY in result.codes()


def test_parsed_negated_damage_wording_stays_eligible() -> None:
    parsed = parse_price("2.750 € inkl. MwSt., keine Defekte", "de")
    item = listing(
        price={"type": parsed.price_type, "basis": parsed.basis, "amount_minor": parsed.amount_minor}
    )
    assert run(item).state == EligibilityState.ELIGIBLE_PRIMARY


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
    # Same fact name as FX_MISSING, so a missing-facts list is stable whatever the FX problem.
    assert result.missing_facts == ("fx:CHF/EUR",)


@pytest.mark.parametrize(
    ("chf", "near"),
    [
        ("2250.00", False),  # EUR 2419.35: 80.65 below 2,500 (> 3 % = 75)
        ("2260.00", True),  # EUR 2430.11: within 75 of 2,500
        ("2324.99", True),  # EUR 2499.99
        ("2400.00", False),  # EUR 2580.65: 80.65 above 2,500
        ("2700.00", False),  # EUR 2903.23: 96.77 below 3,000 (> 3 % = 90)
        ("2710.00", True),  # EUR 2913.98: within 90 of 3,000
        ("2850.00", True),  # EUR 3064.52: above the band but within 90 -> cannot reject on a stale rate
        ("2880.00", False),  # EUR 3096.77: far enough above to reject despite the stale rate
    ],
)
def test_stale_fx_margin_applies_at_both_band_edges(chf: str, near: bool) -> None:
    # SYNTHETIC rate 0.93 CHF per EUR; the margin is fx_boundary_margin_pct (3 %) of each bound.
    stale = chf_rate("0.93", rate_date=date(2026, 9, 20))
    codes = run(chf_listing(chf), rates=[stale]).codes()
    assert (ReasonCode.FX_STALE_NEAR_BOUNDARY in codes) is near
    if not near:
        assert ReasonCode.FX_STALE in codes


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


def test_unmatched_model_with_suv_body_needs_facts_when_taxonomy_required() -> None:
    # Shipped profiles set require_taxonomy_match: true; a seller "SUV" category alone is not
    # enough because marketplaces file pickups and raised estates there too.
    result = run(listing(vehicle={"make": "Example", "model": "Trail", "body_type": BodyType.SUV}))
    assert result.state == EligibilityState.NEEDS_FACTS
    reason = next(r for r in result.reasons if r.code == ReasonCode.TAXONOMY_UNMATCHED)
    assert reason.severity == ReasonSeverity.NEEDS_FACTS
    assert "vehicle.model" in result.missing_facts


def test_unmatched_model_with_suv_body_passes_when_taxonomy_not_required() -> None:
    result = run(
        listing(vehicle={"make": "Example", "model": "Trail", "body_type": BodyType.SUV}),
        config=business_config(require_taxonomy=False),
    )
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    reason = next(r for r in result.reasons if r.code == ReasonCode.TAXONOMY_UNMATCHED)
    assert reason.severity == ReasonSeverity.WARNING


@pytest.mark.parametrize(
    ("make", "model", "body"),
    [
        ("Toyota", "Hilux", BodyType.SUV),
        ("Mitsubishi", "L200", BodyType.OFFROAD),
        ("Ford", "Ranger", BodyType.SUV),
        ("Volvo", "XC70", BodyType.SUV),
        ("Audi", "A6 allroad quattro", BodyType.CROSSOVER),
    ],
)
def test_pickups_and_raised_estates_in_suv_category_are_rejected(
    make: str, model: str, body: BodyType
) -> None:
    for config in (DEFAULT_CONFIG, business_config(require_taxonomy=False)):
        result = run(listing(vehicle={"make": make, "model": model, "body_type": body}), config=config)
        assert result.state == EligibilityState.REJECTED
        assert ReasonCode.NOT_SUV in result.codes()


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


def test_without_taxonomy_required_match_needs_facts() -> None:
    result = run(listing(), use_taxonomy=False)
    assert result.state == EligibilityState.NEEDS_FACTS
    assert {ReasonCode.TAXONOMY_NOT_SUPPLIED, ReasonCode.TAXONOMY_UNMATCHED} <= result.codes()
    assert result.taxonomy_match is None


def test_without_taxonomy_body_type_decides_when_match_not_required() -> None:
    config = business_config(require_taxonomy=False)
    result = run(listing(), config=config, use_taxonomy=False)
    assert result.state == EligibilityState.ELIGIBLE_PRIMARY
    assert ReasonCode.TAXONOMY_NOT_SUPPLIED in result.codes()
    assert result.taxonomy_match is None
    unknown = run(listing(vehicle={"body_type": BodyType.UNKNOWN}), config=config, use_taxonomy=False)
    assert unknown.state == EligibilityState.NEEDS_FACTS
    hatchback = run(listing(vehicle={"body_type": BodyType.HATCHBACK}), config=config, use_taxonomy=False)
    assert hatchback.state == EligibilityState.REJECTED


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
