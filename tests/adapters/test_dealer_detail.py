"""Detail parsing of the schema.org dealer adapter on SYNTHETIC fixtures (spec sections 7, 17, 24, 31)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.adapters.conftest import NOW, DetailFn, car_page, raw_document

from suv_deals.adapters.dealer_inventory import SchemaOrgDealerAdapter
from suv_deals.adapters.fixture_client import FixtureCrawlClient
from suv_deals.domain.enums import (
    AccessState,
    Availability,
    BodyType,
    ClaimStatus,
    Co2Cycle,
    Confidence,
    Drive,
    ExtractionMethod,
    Fuel,
    Gearbox,
    OdometerClaim,
    Precision,
    PriceBasis,
    PriceType,
    SellerType,
    Tristate,
    VatTreatment,
)
from suv_deals.domain.listings import NormalizedListing
from suv_deals.domain.profiles import MAX_MILEAGE_KM_EXCLUSIVE
from suv_deals.errors import ValidationFailed

DE = "https://dealer.example/fahrzeug"
IT = "https://concessionario.example/usato/auto"
CH = "https://garage.example/occasion"


async def _listing(detail: DetailFn, adapter: SchemaOrgDealerAdapter, url: str) -> NormalizedListing:
    parsed = await detail(adapter, url)
    assert parsed.access_state == AccessState.OK, parsed.errors
    assert parsed.page_type == "detail"
    assert parsed.listing is not None, parsed.errors
    return parsed.listing


def _parse(
    adapter: SchemaOrgDealerAdapter,
    json_ld: str,
    body: str = "<p>Synthetic detail page body text.</p>",
    url: str = f"{DE}/TEST-500",
) -> NormalizedListing:
    parsed = adapter.parse_detail(raw_document(url, car_page(json_ld, body)))
    assert parsed.listing is not None, parsed.errors
    return parsed.listing


# --------------------------------------------------------------------------- DE fixtures


async def test_normal_detail(de_adapter: SchemaOrgDealerAdapter, detail: DetailFn) -> None:
    listing = await _listing(detail, de_adapter, f"{DE}/TEST-204")
    assert listing.source_key == "fixture_dealer_de"
    assert listing.source_listing_id == "TEST-204"
    assert listing.canonical_url == f"{DE}/TEST-204"
    assert listing.observed_at == NOW
    assert listing.parser_version == "schemaorg_dealer@1.1.0"
    assert listing.language == "de"
    assert listing.title == "Example Trail 2.0 Diesel 4x4"
    assert listing.seller_type == SellerType.DEALER
    assert (listing.location.country, listing.location.city) == ("DE", "Musterstadt")

    v = listing.vehicle
    assert (v.make, v.model, v.trim, v.model_year) == ("Example", "Trail", "Comfort", 2011)
    assert v.first_registration.value == "2011-05" and v.first_registration.precision == Precision.MONTH
    assert v.body_type == BodyType.SUV
    assert v.seats == 5
    assert v.fuel == Fuel.DIESEL
    assert v.engine_code is None
    assert v.engine_displacement_cm3 == 1995
    assert v.power_kw == 103  # stated kW preferred over the BHP value
    assert (v.gearbox, v.gearbox_subtype) == (Gearbox.MANUAL, "6-Gang")
    assert v.drive == Drive.AWD
    assert v.mileage_km == Decimal(187500) and isinstance(v.mileage_km, Decimal)
    assert (v.mileage_original.amount, v.mileage_original.unit) == (Decimal(187500), "km")
    assert v.mileage_claim == OdometerClaim.SELLER_REPORTED

    p = listing.price
    assert (p.amount_minor, p.currency) == (275000, "EUR")
    assert p.amount_minor is not None
    assert Decimal(2500) <= Decimal(p.amount_minor) / 100 <= Decimal(3000)
    assert p.basis == PriceBasis.GROSS
    assert p.type == PriceType.FULL_VEHICLE_ASKING
    assert p.vat_rate_stated == Decimal(19)
    assert p.vat_treatment == VatTreatment.NOT_STATED
    assert p.vat_reclaimable == Tristate.UNKNOWN
    assert p.gross_amount_minor == 275000 and p.net_amount_minor is None
    assert p.raw_text == "2.750 \u20ac"

    assert listing.availability == Availability.AVAILABLE
    assert listing.documentation.vin == "XXXSYNTH000000204"
    assert listing.documentation.vin_format_valid == Tristate.YES
    assert listing.documentation.previous_owners == 2
    assert listing.co2.g_per_km is None
    assert listing.condition.accident_free == ClaimStatus.SELLER_CLAIMED
    assert "CO2_MISSING" in listing.warnings and "ENGINE_CODE_UNVERIFIED" in listing.warnings
    # "Zahnriemen bei 150.000 km gewechselt" is not an odometer statement.
    assert listing.conflicts == ()
    assert listing.description_excerpt is not None and "Unfallfrei" in listing.description_excerpt

    prov = listing.provenance["price.amount_minor"]
    assert prov.method == ExtractionMethod.JSON_LD and prov.confidence == Confidence.HIGH
    assert prov.selector == "json_ld:offers.price" and prov.raw_text == "2750.00 EUR"
    assert prov.source_url == f"{DE}/TEST-204" and prov.observed_at == NOW
    assert listing.provenance["vehicle.mileage_km"].method == ExtractionMethod.JSON_LD
    assert listing.provenance["condition.accident_free"].confidence == Confidence.LOW  # claim, not truth


async def test_recaptcha_widget_on_real_detail_page_does_not_block(
    de_adapter: SchemaOrgDealerAdapter, client: FixtureCrawlClient
) -> None:
    document = await client.fetch(f"{DE}/TEST-204", purpose="detail", source_key="fixture_dealer_de")
    assert "g-recaptcha" in (document.html or "")
    assert de_adapter.detect_access_state(document) == AccessState.OK


async def test_semantic_hash_is_deterministic(de_adapter: SchemaOrgDealerAdapter, detail: DetailFn) -> None:
    first = await _listing(detail, de_adapter, f"{DE}/TEST-204")
    second = await _listing(detail, de_adapter, f"{DE}/TEST-204")
    assert first.semantic_hash() == second.semantic_hash()


async def test_missing_price_detail(de_adapter: SchemaOrgDealerAdapter, detail: DetailFn) -> None:
    listing = await _listing(detail, de_adapter, f"{DE}/TEST-205")
    assert listing.price.amount_minor is None
    assert listing.price.currency is None
    assert listing.price.type == PriceType.PRICE_ON_REQUEST
    assert listing.price.basis == PriceBasis.UNKNOWN
    assert {"PRICE_MISSING", "PRICE_ON_REQUEST"} <= set(listing.warnings)
    assert listing.vehicle.first_registration.precision == Precision.YEAR


async def test_contradictory_mileage(de_adapter: SchemaOrgDealerAdapter, detail: DetailFn) -> None:
    listing = await _listing(detail, de_adapter, f"{DE}/TEST-206")
    assert listing.vehicle.mileage_km == Decimal(187500)  # the lower title value never overrides
    assert listing.vehicle.mileage_claim == OdometerClaim.CONFLICTING
    (conflict,) = listing.conflicts
    assert conflict.field == "vehicle.mileage_km"
    assert conflict.resolution == "unresolved"
    assert any("187500" in value for value in conflict.values)
    assert any("87500" in value and "187500" not in value for value in conflict.values)
    assert "MILEAGE_CONFLICT" in listing.warnings
    assert listing.price.basis == PriceBasis.UNKNOWN and "PRICE_BASIS_UNKNOWN" in listing.warnings
    assert listing.vehicle.model == "Trail"  # ProductModel node


async def test_net_price_with_vat_shown(de_adapter: SchemaOrgDealerAdapter, detail: DetailFn) -> None:
    listing = await _listing(detail, de_adapter, f"{DE}/TEST-207")
    p = listing.price
    assert (p.amount_minor, p.currency, p.basis) == (231100, "EUR", PriceBasis.NET)
    assert (p.net_amount_minor, p.gross_amount_minor) == (231100, 275009)
    assert p.vat_treatment == VatTreatment.VAT_SHOWN
    assert p.vat_reclaimable == Tristate.YES  # seller wording only
    assert "seller wording" in (listing.provenance["price.vat_reclaimable"].transformation or "")
    assert p.vat_rate_stated == Decimal(19)
    assert not any(c.field == "price.basis" for c in listing.conflicts)
    assert (listing.co2.g_per_km, listing.co2.cycle) == (Decimal(189), Co2Cycle.NEDC)
    assert (listing.vehicle.gearbox, listing.vehicle.drive) == (Gearbox.AUTOMATIC, Drive.AWD)
    assert listing.provenance["vehicle.drive"].confidence == Confidence.LOW  # generic all-wheel wording


async def test_margin_scheme_and_200000_km(de_adapter: SchemaOrgDealerAdapter, detail: DetailFn) -> None:
    listing = await _listing(detail, de_adapter, f"{DE}/TEST-208")
    p = listing.price
    assert p.vat_treatment == VatTreatment.MARGIN_SCHEME
    assert p.vat_reclaimable == Tristate.NO
    assert p.basis == PriceBasis.GROSS
    assert listing.provenance["price.basis"].method == ExtractionMethod.DERIVED
    assert p.negotiable == Tristate.YES  # "VB"
    assert (p.amount_minor, p.currency) == (299000, "EUR")
    v = listing.vehicle
    assert v.mileage_km == Decimal(200000)
    assert not v.mileage_km < MAX_MILEAGE_KM_EXCLUSIVE  # parsed exactly; eligibility rejects later
    assert (v.drive, v.body_type, v.fuel, v.gearbox) == (
        Drive.FOUR_WD,
        BodyType.OFFROAD,
        Fuel.PETROL,
        Gearbox.MANUAL,
    )
    assert v.first_registration.value == "2009-05"


async def test_instalment_price_is_not_a_vehicle_price(
    de_adapter: SchemaOrgDealerAdapter, detail: DetailFn
) -> None:
    listing = await _listing(detail, de_adapter, f"{DE}/TEST-218")
    assert listing.price.type == PriceType.INSTALMENT
    assert "PRICE_IS_INSTALMENT" in listing.warnings


async def test_sold_is_sold_claimed_not_removed(de_adapter: SchemaOrgDealerAdapter, detail: DetailFn) -> None:
    listing = await _listing(detail, de_adapter, f"{DE}/TEST-211")
    assert listing.availability == Availability.SOLD_CLAIMED


@pytest.mark.parametrize(
    ("listing_id", "page_type", "state"),
    [
        ("TEST-209", "removed", AccessState.REMOVED),  # explicit wording, served with 200
        ("TEST-210", "removed", AccessState.REMOVED),  # 410 Gone
        ("TEST-220", "removed", AccessState.REMOVED),  # 404 with explicit wording
        ("TEST-216", "unknown", AccessState.NOT_FOUND),  # plain 404 is not evidence of removal
        ("TEST-214", "login", AccessState.ACCESS_BLOCKED),
        ("TEST-215", "challenge", AccessState.ACCESS_BLOCKED),
        ("TEST-221", "challenge", AccessState.ACCESS_BLOCKED),  # challenge served with 503
        ("TEST-217", "empty_shell", AccessState.UNEXPECTED_CONTENT),
    ],
)
async def test_non_listing_detail_pages(
    de_adapter: SchemaOrgDealerAdapter, detail: DetailFn, listing_id: str, page_type: str, state: AccessState
) -> None:
    parsed = await detail(de_adapter, f"{DE}/{listing_id}")
    assert parsed.listing is None
    assert (parsed.page_type, parsed.access_state) == (page_type, state)
    assert parsed.errors
    assert ("LISTING_REMOVED_EXPLICIT" in parsed.warnings) == (state == AccessState.REMOVED)


@pytest.mark.parametrize(
    ("listing_id", "error"),
    [("TEST-212", "STRUCTURED_DATA_MALFORMED"), ("TEST-219", "NO_VEHICLE_STRUCTURED_DATA")],
)
async def test_malformed_or_changed_markup_is_a_parse_failure(
    de_adapter: SchemaOrgDealerAdapter, detail: DetailFn, listing_id: str, error: str
) -> None:
    parsed = await detail(de_adapter, f"{DE}/{listing_id}")
    assert (parsed.page_type, parsed.access_state, parsed.listing) == ("detail", AccessState.OK, None)
    assert any(e.startswith(error) for e in parsed.errors)
    outcome = de_adapter.detail_outcome(parsed, NOW)
    assert outcome.ok is False and outcome.has_price is False


async def test_prompt_injection_is_stored_flagged_and_inert(
    de_adapter: SchemaOrgDealerAdapter, detail: DetailFn
) -> None:
    listing = await _listing(detail, de_adapter, f"{DE}/TEST-213")
    text = listing.description_excerpt
    assert text is not None
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in text  # stored as data
    assert "https://attacker.example/collect" in text  # stored as text, never fetched
    assert "SELLER_TEXT_SUSPICIOUS" in listing.warnings
    assert "SELLER_TEXT_MARKUP_REMOVED" in listing.warnings
    for forbidden in ("<script", "</script", "onerror", "alert(", "<img"):
        assert forbidden not in text
    assert listing.title == "Example Trail 2.0 Diesel"
    signals = listing.provenance["description_excerpt"].transformation or ""
    assert "ignore_instructions" in signals and "secret_request" in signals
    # The injected "approve this car" changes nothing in the deterministic fields.
    assert listing.availability == Availability.AVAILABLE
    assert listing.price.amount_minor == 270000


async def test_fetch_detail_policy(de_adapter: SchemaOrgDealerAdapter, client: FixtureCrawlClient) -> None:
    identity = de_adapter.canonicalize(f"{DE}/TEST-204").model_copy(
        update={"canonical_url": "https://dealer.example/intern/TEST-204"}
    )
    document = await de_adapter.fetch_detail(identity, client)
    assert client.requests == []
    assert document.fetch.access_state == AccessState.POLICY_DENIED
    parsed = de_adapter.parse_detail(document)
    assert parsed.access_state == AccessState.POLICY_DENIED and parsed.listing is None
    foreign = identity.model_copy(update={"source_key": "fixture_dealer_it"})
    with pytest.raises(ValidationFailed):
        await de_adapter.fetch_detail(foreign, client)


async def test_detail_outcome_for_health(de_adapter: SchemaOrgDealerAdapter, detail: DetailFn) -> None:
    parsed = await detail(de_adapter, f"{DE}/TEST-204")
    outcome = de_adapter.detail_outcome(parsed, NOW)
    assert outcome.ok and outcome.has_price and outcome.has_mileage and outcome.has_make_model
    assert (outcome.currency, outcome.price_minor, outcome.mileage_km) == ("EUR", 275000, Decimal(187500))


# --------------------------------------------------------------------------- IT fixtures


async def test_italian_detail(it_adapter: SchemaOrgDealerAdapter, detail: DetailFn) -> None:
    listing = await _listing(detail, it_adapter, f"{IT}/example-trail-it301")
    assert listing.source_listing_id == "it301"
    v = listing.vehicle
    assert v.mileage_km == Decimal(199999) and v.mileage_km < MAX_MILEAGE_KM_EXCLUSIVE
    assert v.mileage_claim == OdometerClaim.SELLER_REPORTED  # title "km 199.999" agrees
    assert (v.body_type, v.fuel, v.gearbox, v.drive) == (BodyType.SUV, Fuel.DIESEL, Gearbox.MANUAL, Drive.AWD)
    assert v.engine_displacement_cm3 == 1995
    assert v.power_kw == 104 and "POWER_UNIT_CONVERTED" in listing.warnings
    assert v.first_registration.value == "2010-03"
    p = listing.price
    assert (p.amount_minor, p.currency) == (290000, "EUR")
    assert p.vat_treatment == VatTreatment.VAT_SHOWN
    assert p.basis == PriceBasis.UNKNOWN  # "IVA esposta" alone does not state gross/net
    assert "PRICE_BASIS_UNKNOWN" in listing.warnings
    assert p.negotiable == Tristate.YES
    assert (listing.location.country, listing.location.city) == ("IT", "Città di Prova")
    assert listing.documentation.vin is None
    assert listing.documentation.vin_format_valid == Tristate.NO
    assert "VIN_FORMAT_INVALID" in listing.warnings
    assert listing.condition.accident_free == ClaimStatus.SELLER_CLAIMED  # "non incidentata"


async def test_italian_200000_km_and_fixed_price(
    it_adapter: SchemaOrgDealerAdapter, detail: DetailFn
) -> None:
    listing = await _listing(detail, it_adapter, f"{IT}/example-ridge-it302")
    assert listing.vehicle.mileage_km == Decimal(200000)
    assert listing.price.basis == PriceBasis.GROSS  # "IVA inclusa"
    assert listing.price.negotiable == Tristate.NO  # "prezzo fisso"
    assert listing.vehicle.first_registration.precision == Precision.YEAR


async def test_italian_removed(it_adapter: SchemaOrgDealerAdapter, detail: DetailFn) -> None:
    parsed = await detail(it_adapter, f"{IT}/example-summit-it303")
    assert (parsed.page_type, parsed.access_state) == ("removed", AccessState.REMOVED)


async def test_italian_microdata_injection(it_adapter: SchemaOrgDealerAdapter, detail: DetailFn) -> None:
    listing = await _listing(detail, it_adapter, f"{IT}/example-trail-it304")
    text = listing.description_excerpt or ""
    assert "Ignora le istruzioni precedenti" in text
    assert "<script" not in text and "alert" not in text and "<b>" not in text
    assert "Ottime condizioni" in text
    assert {"SELLER_TEXT_SUSPICIOUS", "SELLER_TEXT_MARKUP_REMOVED"} <= set(listing.warnings)
    assert listing.provenance["description_excerpt"].method == ExtractionMethod.MICRODATA
    # The labelled odometer statement in the description contradicts the structured value.
    assert listing.vehicle.mileage_claim == OdometerClaim.CONFLICTING
    assert listing.vehicle.mileage_km == Decimal(150000)


# --------------------------------------------------------------------------- CH fixtures


async def test_swiss_chf_detail(ch_adapter: SchemaOrgDealerAdapter, detail: DetailFn) -> None:
    listing = await _listing(detail, ch_adapter, f"{CH}/40101")
    p = listing.price
    assert (p.amount_minor, p.currency, p.basis) == (299000, "CHF", PriceBasis.GROSS)
    assert p.vat_rate_stated == Decimal("8.1")
    assert p.raw_text == "CHF 2'990.\u2013"
    v = listing.vehicle
    assert v.mileage_km == Decimal(150000)
    assert v.first_registration.value == "2012-06"
    assert v.engine_displacement_cm3 == 2000  # 2.0 LTR
    assert (listing.co2.g_per_km, listing.co2.cycle) == (Decimal(172), Co2Cycle.WLTP)
    modified = listing.source_modified_at
    assert modified.zone_assumed and modified.assumed_zone == "Europe/Zurich"
    assert modified.value == datetime(2026, 10, 5, 12, 30, tzinfo=UTC)
    assert listing.location.country == "CH" and listing.language == "de-CH"
    assert listing.source_listing_id == "40101"


async def test_swiss_miles_detail(ch_adapter: SchemaOrgDealerAdapter, detail: DetailFn) -> None:
    listing = await _listing(detail, ch_adapter, f"{CH}/40102")
    v = listing.vehicle
    assert v.mileage_km == Decimal("199999.616256")
    assert v.mileage_km == Decimal(124274) * Decimal("1.609344")
    assert v.mileage_km < MAX_MILEAGE_KM_EXCLUSIVE
    assert (v.mileage_original.amount, v.mileage_original.unit) == (Decimal(124274), "mi")
    assert "1.609344" in (listing.provenance["vehicle.mileage_km"].transformation or "")
    assert v.mileage_claim == OdometerClaim.SELLER_REPORTED  # title "124'274 Meilen" agrees
    assert (listing.price.amount_minor, listing.price.currency) == (275050, "CHF")


# --------------------------------------------------------------------------- inline edge cases


def test_unit_less_mileage_needs_visible_confirmation(de_adapter: SchemaOrgDealerAdapter) -> None:
    ld = (
        '{"@type":"Car","name":"X","mileageFromOdometer":{"value":187500},'
        '"offers":{"price":2750,"priceCurrency":"EUR"}}'
    )
    confirmed = _parse(de_adapter, ld, "<p>Kilometerstand: 187.500 km und weitere Angaben.</p>")
    assert confirmed.vehicle.mileage_km == Decimal(187500)
    assert confirmed.provenance["vehicle.mileage_km"].method == ExtractionMethod.DERIVED
    unconfirmed = _parse(de_adapter, ld)
    assert unconfirmed.vehicle.mileage_km is None
    assert unconfirmed.vehicle.mileage_original.unit == "unknown"
    assert "MILEAGE_UNIT_UNKNOWN" in unconfirmed.warnings


def test_ambiguous_json_numbers_are_unknown(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(
        de_adapter,
        '{"@type":"Car","name":"X","mileageFromOdometer":{"value":187.500,"unitCode":"KMT"},'
        '"offers":{"price":2.750,"priceCurrency":"EUR"}}',
    )
    assert listing.vehicle.mileage_km is None
    assert listing.price.amount_minor is None
    assert {"MILEAGE_FORMAT_AMBIGUOUS", "PRICE_FORMAT_AMBIGUOUS"} <= set(listing.warnings)


def test_text_only_mileage_is_low_confidence(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(
        de_adapter,
        '{"@type":"Car","name":"Example Trail ca. 120.000 km","offers":{"price":2750,"priceCurrency":"EUR"}}',
    )
    assert listing.vehicle.mileage_km == Decimal(120000)
    assert listing.vehicle.mileage_claim == OdometerClaim.ESTIMATED
    assert listing.vehicle.mileage_original.is_estimate is True
    assert listing.provenance["vehicle.mileage_km"].confidence == Confidence.LOW
    assert "MILEAGE_FROM_TEXT_ONLY" in listing.warnings


def test_missing_mileage_is_unknown(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(de_adapter, '{"@type":"Car","name":"X","offers":{"price":2750,"priceCurrency":"EUR"}}')
    assert listing.vehicle.mileage_km is None
    assert listing.vehicle.mileage_claim == OdometerClaim.UNKNOWN
    assert "MILEAGE_MISSING" in listing.warnings


def test_future_first_registration_is_rejected(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(
        de_adapter,
        '{"@type":"Car","name":"X","dateVehicleFirstRegistered":"2027-01",'
        '"offers":{"price":2750,"priceCurrency":"EUR"}}',
    )
    assert listing.vehicle.first_registration.value is None
    assert "FIRST_REGISTRATION_IN_FUTURE" in listing.warnings


def test_structured_vat_flag_contradicted_by_wording(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(
        de_adapter,
        '{"@type":"Car","name":"X","offers":{"price":"2750.00","priceCurrency":"EUR","priceSpecification":'
        '{"price":"2750.00","valueAddedTaxIncluded":true}}}',
        "<p>2.750 EUR zzgl. MwSt. Weitere Angaben zum Fahrzeug.</p>",
    )
    assert listing.price.basis == PriceBasis.UNKNOWN
    assert any(c.field == "price.basis" for c in listing.conflicts)
    assert "PRICE_BASIS_CONFLICT" in listing.warnings


def test_gross_and_net_wording_without_structure_is_ambiguous(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(
        de_adapter,
        '{"@type":"Car","name":"X","offers":{"price":"2750.00","priceCurrency":"EUR"}}',
        "<p>2.750 EUR inkl. MwSt. oder 2.311 EUR zzgl. MwSt. fuer Haendler.</p>",
    )
    assert listing.price.basis == PriceBasis.UNKNOWN
    assert "PRICE_BASIS_AMBIGUOUS" in listing.warnings


def test_export_price_wording(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(
        de_adapter,
        '{"@type":"Car","name":"X","offers":{"price":"2311.00","priceCurrency":"EUR"}}',
        "<p>Exportpreis netto 2.311 EUR - nur an Haendler.</p>",
    )
    assert listing.price.type == PriceType.EXPORT_NET
    assert listing.price.export_net_price_minor == 231100
    assert "PRICE_EXPORT_NET" in listing.warnings


def test_damaged_condition_is_not_ordinary_price(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(
        de_adapter,
        '{"@type":"Car","name":"X","itemCondition":"https://schema.org/DamagedCondition",'
        '"offers":{"price":"900.00","priceCurrency":"EUR"}}',
    )
    assert listing.condition.damaged_vehicle == ClaimStatus.SELLER_CLAIMED
    assert listing.price.type == PriceType.PARTS_OR_DAMAGED


def test_leasing_business_function(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(
        de_adapter,
        '{"@type":"Car","name":"X","offers":{"price":"199.00","priceCurrency":"EUR",'
        '"businessFunction":"http://purl.org/goodrelations/v1#LeaseOut"}}',
    )
    assert listing.price.type == PriceType.LEASING


@pytest.mark.parametrize(
    ("availability", "expected", "warning"),
    [
        ("https://schema.org/InStock", Availability.AVAILABLE, None),
        ("https://schema.org/Reserved", Availability.RESERVED, None),
        ("https://schema.org/SoldOut", Availability.SOLD_CLAIMED, None),
        ("https://schema.org/Discontinued", Availability.REMOVED, None),
        ("https://schema.org/OutOfStock", Availability.UNKNOWN, "AVAILABILITY_UNCLEAR"),
        ("https://schema.org/PreOrder", Availability.UNKNOWN, "AVAILABILITY_UNCLEAR"),
        ("Bald da", Availability.UNKNOWN, "AVAILABILITY_UNMAPPED"),
    ],
)
def test_availability_mapping(
    de_adapter: SchemaOrgDealerAdapter, availability: str, expected: Availability, warning: str | None
) -> None:
    listing = _parse(
        de_adapter,
        f'{{"@type":"Car","name":"X","offers":{{"price":2750,"priceCurrency":"EUR","availability":"{availability}"}}}}',
    )
    assert listing.availability == expected
    if warning:
        assert warning in listing.warnings


def test_missing_availability_on_live_offer_is_derived(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(de_adapter, '{"@type":"Car","name":"X","offers":{"price":2750,"priceCurrency":"EUR"}}')
    assert listing.availability == Availability.AVAILABLE
    assert listing.provenance["availability"].method == ExtractionMethod.DERIVED


def test_private_seller_and_country_name(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(
        de_adapter,
        '{"@type":"Car","name":"X","offers":{"price":2750,"priceCurrency":"EUR","seller":{"@type":"Person",'
        '"name":"Synthetic Person","address":{"addressCountry":"Schweiz","addressLocality":"Ort"}}}}',
    )
    assert listing.seller_type == SellerType.PRIVATE
    assert listing.location.country == "CH"
    assert "SELLER_COUNTRY_DIFFERS_FROM_SOURCE" in listing.warnings


@pytest.mark.parametrize(
    ("offers", "warning"),
    [
        ('{"price":0,"priceCurrency":"EUR"}', "PRICE_ZERO_IGNORED"),
        ('{"price":2750}', "PRICE_CURRENCY_MISSING"),
        ('{"price":2750,"priceCurrency":"XYZ"}', "PRICE_CURRENCY_UNSUPPORTED"),
        ('{"price":"2750.005","priceCurrency":"EUR"}', "PRICE_MISSING"),
        (
            '{"@type":"AggregateOffer","lowPrice":2500,"highPrice":3000,"priceCurrency":"EUR"}',
            "PRICE_AGGREGATE_ONLY",
        ),
    ],
)
def test_unusable_prices_stay_unknown(de_adapter: SchemaOrgDealerAdapter, offers: str, warning: str) -> None:
    listing = _parse(de_adapter, f'{{"@type":"Car","name":"X","offers":{offers}}}')
    assert listing.price.amount_minor is None
    assert listing.price.currency is None
    assert warning in listing.warnings


def test_product_node_with_offers_is_accepted(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(
        de_adapter,
        '{"@type":"Product","name":"Example Trail","brand":"Example","model":"Trail","sku":"TEST-501",'
        '"offers":{"price":"2650.00","priceCurrency":"EUR"}}',
    )
    assert listing.source_listing_id == "TEST-501"
    assert (listing.vehicle.make, listing.vehicle.model) == ("Example", "Trail")


def test_out_of_range_values_are_dropped_not_fatal(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(
        de_adapter,
        '{"@type":"Car","name":"X","vehicleSeatingCapacity":40,"numberOfPreviousOwners":-1,'
        '"vehicleEngine":{"enginePower":{"value":99999,"unitCode":"KWT"},'
        '"engineDisplacement":{"value":5,"unitCode":"CMQ"}},"emissionsCO2":5000,'
        '"bodyType":"Raumschiff","fuelType":"Plutonium","vehicleTransmission":"Teleport",'
        '"driveWheelConfiguration":"Hover","offers":{"price":2750,"priceCurrency":"EUR"}}',
    )
    v = listing.vehicle
    assert (v.seats, v.power_kw, v.engine_displacement_cm3) == (None, None, None)
    assert listing.documentation.previous_owners is None
    assert listing.co2.g_per_km is None
    assert (v.body_type, v.fuel, v.gearbox, v.drive) == (
        BodyType.UNKNOWN,
        Fuel.UNKNOWN,
        Gearbox.UNKNOWN,
        Drive.UNKNOWN,
    )
    assert {
        "POWER_IMPLAUSIBLE",
        "DISPLACEMENT_IMPLAUSIBLE",
        "BODY_TYPE_UNMAPPED",
        "FUEL_UNMAPPED",
        "GEARBOX_UNMAPPED",
        "DRIVE_UNMAPPED",
    } <= set(listing.warnings)


def test_detail_url_serving_a_result_list(de_adapter: SchemaOrgDealerAdapter) -> None:
    html = car_page('{"@type":"ItemList","itemListElement":["https://dealer.example/fahrzeug/TEST-204"]}')
    parsed = de_adapter.parse_detail(raw_document(f"{DE}/TEST-502", html))
    assert (parsed.page_type, parsed.access_state) == ("search", AccessState.UNEXPECTED_CONTENT)
    assert parsed.errors == ("EXPECTED_DETAIL_GOT_SEARCH",)


def test_json_ld_url_mismatch_is_flagged(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(
        de_adapter,
        '{"@type":"Car","name":"X","url":"https://dealer.example/fahrzeug/OTHER",'
        '"offers":{"price":2750,"priceCurrency":"EUR"}}',
    )
    assert "JSON_LD_URL_MISMATCH" in listing.warnings
    assert listing.canonical_url == f"{DE}/TEST-500"  # the fetched URL, never the claimed one


def test_redirect_to_foreign_host_is_unexpected_content(de_adapter: SchemaOrgDealerAdapter) -> None:
    html = car_page('{"@type":"Car","name":"X","offers":{"price":2750,"priceCurrency":"EUR"}}')
    parsed = de_adapter.parse_detail(
        raw_document(f"{DE}/TEST-503", html, final_url="https://elsewhere.example/x")
    )
    assert parsed.access_state == AccessState.UNEXPECTED_CONTENT and parsed.listing is None


def test_iri_is_not_a_make_or_model(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _parse(
        de_adapter,
        '{"@type":"Car","name":"X","brand":{"@id":"https://dealer.example/#brand"},'
        '"model":"https://dealer.example/models/trail","offers":{"price":2750,"priceCurrency":"EUR"}}',
    )
    assert (listing.vehicle.make, listing.vehicle.model) == (None, None)
    assert "MAKE_MODEL_MISSING" in listing.warnings


@pytest.mark.parametrize(
    ("body", "basis", "price_type"),
    [
        ("<p>Venditore privato, auto in ottimo stato e ben tenuta.</p>", PriceBasis.UNKNOWN, None),
        ("<p>Prezzo ivato, consegna immediata in sede.</p>", PriceBasis.GROSS, None),
        (
            "<p>Finanzierung auf Anfrage, Preis siehe oben im Inserat.</p>",
            PriceBasis.UNKNOWN,
            PriceType.UNKNOWN,
        ),
        (
            "<p>Preis: auf Anfrage beim Haendler erhaeltlich.</p>",
            PriceBasis.UNKNOWN,
            PriceType.PRICE_ON_REQUEST,
        ),
    ],
)
def test_wording_word_boundaries(
    de_adapter: SchemaOrgDealerAdapter, body: str, basis: PriceBasis, price_type: PriceType | None
) -> None:
    with_price = _parse(
        de_adapter, '{"@type":"Car","name":"X","offers":{"price":2750,"priceCurrency":"EUR"}}', body
    )
    assert with_price.price.basis == basis
    if price_type is not None:
        without = _parse(de_adapter, '{"@type":"Car","name":"X","offers":{"priceCurrency":"EUR"}}', body)
        assert without.price.type == price_type
