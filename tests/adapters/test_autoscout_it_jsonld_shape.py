"""The generic schema.org reader against the JSON-LD SHAPE that AutoScout24 Italy search pages use.

The shape (types, key names and value kinds, no values) was read from an owner-run capture on
2026-10-15 (`suv-deals capture-samples --source autoscout24_it --search-path /lst/toyota/rav4`):
a ``SearchResultsPage`` whose ``mainEntity`` is an ``ItemList`` of ``ListItem`` entries with a
relative ``url`` (``/annunci/<slug>``) and an ``item`` typed ``["Car", "Product"]`` that carries
``name``, ``brand`` (Brand), ``model``, ``fuelType``, ``vehicleTransmission``,
``vehicleConfiguration``, ``mileageFromOdometer`` (QuantitativeValue value + unitCode),
``image``, ``additionalProperty`` (PropertyValue) and ``offers`` (Offer: price, priceCurrency,
url, availability, itemCondition, seller AutoDealer with name, url and address). A separate
``BreadcrumbList`` also holds ``ListItem`` entries.

Every value below is SYNTHETIC and the host is a placeholder: this proves that the existing reader
understands the structure, not anything about real listings. No real page content is stored.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from tests.adapters.conftest import SYNTHETIC_MARKER, raw_document

from suv_deals.adapters._extract import local_type_names, offers_typed_item, parse_json_ld_blocks
from suv_deals.adapters.base import SearchRequest
from suv_deals.adapters.dealer_inventory import ADAPTER_KEY, ADAPTER_VERSION, SchemaOrgDealerAdapter
from suv_deals.domain.enums import AccessState, BodyType, Completeness, Drive, Fuel, Gearbox
from suv_deals.domain.sources import SourceConfig

HOST = "www.sample-market.it"
SEARCH_URL = f"https://{HOST}/lst/toyota/rav4"


def _config() -> SourceConfig:
    return SourceConfig.model_validate(
        {
            "source_key": "sample_market_it",
            "display_name": "Sample market (IT)",
            "country": "IT",
            "role": "acquisition",
            "mode": "public_html",
            "adapter": ADAPTER_KEY,
            "adapter_version": ADAPTER_VERSION,
            "terms_decision": "proceed_acknowledged",
            "terms_decision_actor": "owner (test)",
            "allowed_hosts": [HOST],
            "allowed_search_paths": [r"^/lst/[a-z0-9-]+(/[a-z0-9-]+){0,2}$"],
            "allowed_detail_paths": [r"^/annunci/[a-z0-9-]+$"],
            "source_timezone": "Europe/Rome",
            "search": {"search_url": SEARCH_URL},
        }
    )


def _card(position: int, slug: str, price: int, km: int) -> dict[str, Any]:
    return {
        "@type": "ListItem",
        "position": position,
        "url": f"/annunci/{slug}",
        "item": {
            "@type": ["Car", "Product"],
            "name": f"Synthetic SUV {position}",
            "brand": {"@type": "Brand", "name": "Synthetic"},
            "model": "Model",
            "fuelType": "Benzina",
            "vehicleTransmission": "Manuale",
            "vehicleConfiguration": "2.0",
            "mileageFromOdometer": {"@type": "QuantitativeValue", "value": km, "unitCode": "KMT"},
            "image": [f"https://images.example.net/listing-images/{slug}/1"],
            "itemCondition": "https://schema.org/UsedCondition",
            "additionalProperty": [{"@type": "PropertyValue", "name": "Synthetic label", "value": "x"}],
            "offers": {
                "@type": "Offer",
                "price": price,
                "priceCurrency": "EUR",
                "url": f"/annunci/{slug}",
                "availability": "https://schema.org/InStock",
                "itemCondition": "https://schema.org/UsedCondition",
                "seller": {
                    "@type": "AutoDealer",
                    "name": "Synthetic Dealer",
                    "url": f"https://{HOST}/concessionari/synthetic-dealer",
                    "address": {"@type": "PostalAddress", "addressLocality": "Synthetic"},
                },
            },
        },
    }


def _page(cards: list[dict[str, Any]], number_of_items: int) -> str:
    results = {
        "@context": "https://schema.org",
        "@type": "SearchResultsPage",
        "name": "Synthetic results",
        "url": SEARCH_URL,
        "inLanguage": "it-IT",
        "mainEntity": {"@type": "ItemList", "numberOfItems": number_of_items, "itemListElement": cards},
    }
    breadcrumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": f"https://{HOST}/"},
            {"@type": "ListItem", "position": 2, "name": "Synthetic", "item": f"https://{HOST}/lst/toyota"},
        ],
    }
    return (
        f"{SYNTHETIC_MARKER}\n<!doctype html><html lang='it'><head><title>Synthetic</title>"
        f"<script type='application/ld+json'>{json.dumps(results)}</script>"
        f"<script type='application/ld+json'>{json.dumps(breadcrumbs)}</script></head>"
        "<body><h1>Synthetic results</h1><p>Synthetic page body with enough visible text.</p></body></html>"
    )


def _request(adapter: SchemaOrgDealerAdapter) -> SearchRequest:
    return SearchRequest(source_key=adapter.source_key, profile_key="primary", url=SEARCH_URL)


def test_cards_are_read_from_the_nested_item_list() -> None:
    adapter = SchemaOrgDealerAdapter(_config())
    cards = [
        _card(1, "synthetic-suv-one-0a1b2c3d", 2900, 187000),
        _card(2, "synthetic-suv-two-4e5f6a7b", 2650, 199500),
    ]
    page = adapter.parse_search(_request(adapter), raw_document(SEARCH_URL, _page(cards, 2)))

    assert page.access_state == AccessState.OK
    assert page.completeness == Completeness.COMPLETE
    assert [o.canonical_url for o in page.observations] == [
        f"https://{HOST}/annunci/synthetic-suv-one-0a1b2c3d",
        f"https://{HOST}/annunci/synthetic-suv-two-4e5f6a7b",
    ]
    first = page.observations[0]
    assert first.title == "Synthetic SUV 1"
    assert (first.card_price_minor, first.card_currency) == (290000, "EUR")
    assert first.card_mileage_km == Decimal(187000)
    assert page.observations[1].card_mileage_km == Decimal(199500)


def test_breadcrumb_list_items_never_become_cards() -> None:
    adapter = SchemaOrgDealerAdapter(_config())
    page = adapter.parse_search(
        _request(adapter),
        raw_document(SEARCH_URL, _page([_card(1, "synthetic-suv-0a1b2c3d", 2900, 150000)], 1)),
    )
    assert len(page.observations) == 1


def test_a_reported_total_above_the_page_without_a_next_link_is_partial() -> None:
    """If ``numberOfItems`` is the total hit count, one page without a next link is not complete."""
    adapter = SchemaOrgDealerAdapter(_config())
    page = adapter.parse_search(
        _request(adapter),
        raw_document(SEARCH_URL, _page([_card(1, "synthetic-suv-0a1b2c3d", 2900, 150000)], 345)),
    )
    assert page.completeness == Completeness.PARTIAL
    assert page.result_count_reported == 345


# --------------------------------------------------------------------------- detail page shape
#
# AutoScout24 Italy DETAIL pages (owner capture 2026-10-16) publish a ``Product`` whose
# ``offers`` Offer carries price/currency/url and whose ``offers.itemOffered`` is the ``Car`` with the
# vehicle facts: manufacturer, model, bodyType, identifier, productionDate (= the FIRST REGISTRATION
# date; no dateVehicleFirstRegistered), driveWheelConfiguration "Anteriore", vehicleTransmission
# "Manuale", mileageFromOdometer with unitText KMT, and vehicleEngine[0] with fuelType, an
# enginePower LIST (the BHP entry first, the KWT entry second) and engineDisplacement CMQ.

DETAIL_URL = f"https://{HOST}/annunci/synthetic-suv-diesel-bianco-0a1b2c3d"


def _detail_html(car_extra: dict[str, Any] | None = None) -> str:
    car: dict[str, Any] = {
        "@type": "Car",
        "name": "Synthetic Model",
        "manufacturer": "Synthetic",
        "model": "Model",
        "bodyType": "SUV/Fuoristrada/Pick-up",
        "identifier": "0a1b2c3d",
        "productionDate": "2019-04-01",
        "driveWheelConfiguration": "Anteriore",
        "vehicleTransmission": "Manuale",
        "mileageFromOdometer": {"@type": "QuantitativeValue", "value": 155300, "unitText": "KMT"},
        "vehicleEngine": [
            {
                "@type": "EngineSpecification",
                "fuelType": "Diesel",
                "enginePower": [
                    {"@type": "QuantitativeValue", "value": 110, "unitCode": "BHP"},
                    {"@type": "QuantitativeValue", "value": 81, "unitCode": "KWT"},
                ],
                "engineDisplacement": {"@type": "QuantitativeValue", "value": 1461, "unitCode": "CMQ"},
            }
        ],
        **(car_extra or {}),
    }
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "brand": {"@type": "Brand", "name": "Synthetic"},
        "name": "Synthetic per € 8.000",
        "offers": {
            "@type": "Offer",
            "price": 8000,
            "priceCurrency": "EUR",
            "itemCondition": "UsedCondition",
            "availability": "InStock",
            "url": DETAIL_URL,
            "offeredBy": {"@type": "AutoDealer", "name": "Synthetic Dealer"},
            "itemOffered": car,
        },
    }
    return (
        f"{SYNTHETIC_MARKER}\n<!doctype html><html lang='it'><head><title>Synthetic</title>"
        f"<script type='application/ld+json'>{json.dumps(product)}</script></head>"
        "<body><h1>Synthetic Model</h1><p>Synthetic detail body with enough visible text.</p></body></html>"
    )


def test_the_generic_reader_finds_the_car_offered_by_a_product() -> None:
    """Regression: the reader only descended into @graph/mainEntity and lost every vehicle fact."""
    adapter = SchemaOrgDealerAdapter(_config())
    parsed = adapter.parse_detail(raw_document(DETAIL_URL, _detail_html()))
    assert parsed.access_state == AccessState.OK and parsed.listing is not None, parsed.errors
    listing = parsed.listing
    v = listing.vehicle
    assert (v.make, v.model) == ("Synthetic", "Model")
    assert v.mileage_km == Decimal(155300)
    assert (v.fuel, v.gearbox, v.drive, v.body_type) == (Fuel.DIESEL, Gearbox.MANUAL, Drive.FWD, BodyType.SUV)
    assert (v.engine_displacement_cm3, v.power_kw) == (1461, 81)  # the KWT entry, not 110 "BHP"
    # The GENERIC reader keeps schema.org semantics: productionDate is the production year and
    # never a first registration (only the AutoScout24 adapter reads it as a registration date).
    assert v.first_registration.value is None and v.production_year == 2019
    assert "vehicle.first_registration" not in listing.provenance
    assert (listing.price.amount_minor, listing.price.currency) == (800000, "EUR")
    assert listing.source_listing_id == "0a1b2c3d"  # Car.identifier
    assert "MAKE_MODEL_MISSING" not in listing.warnings and "MILEAGE_MISSING" not in listing.warnings


def test_a_real_registration_date_keeps_production_date_as_the_production_year() -> None:
    adapter = SchemaOrgDealerAdapter(_config())
    html = _detail_html({"dateVehicleFirstRegistered": "2019-06", "productionDate": "2018"})
    listing = adapter.parse_detail(raw_document(DETAIL_URL, html)).listing
    assert listing is not None
    assert (listing.vehicle.first_registration.value, listing.vehicle.production_year) == ("2019-06", 2018)
    assert "FIRST_REGISTRATION_FROM_PRODUCTION_DATE" not in listing.warnings


def test_flatten_yields_the_offered_item_with_its_offer() -> None:
    nodes = parse_json_ld_blocks([_detail_html().split("ld+json'>")[1].split("</script>")[0]]).nodes
    assert [sorted(local_type_names(n)) for n in nodes] == [["Product"], ["Car"]]
    car = nodes[1]
    assert car["offers"]["price"] == 8000 and "itemOffered" not in car["offers"]
    assert car["url"] == DETAIL_URL and car["brand"]["name"] == "Synthetic"
    assert offers_typed_item(nodes[0], frozenset({"Car"}))


def test_a_vehicle_naming_itself_as_its_offered_item_stays_one_vehicle_node() -> None:
    """Regression: a dealer Car whose Offer names the Car again inline was split into two nodes."""
    car = {
        "@context": "https://schema.org",
        "@type": "Car",
        "name": "Synthetic SUV 1.5",
        "brand": {"@type": "Brand", "name": "Synthetic"},
        "model": "Model",
        "mileageFromOdometer": {"@type": "QuantitativeValue", "value": 120000, "unitCode": "KMT"},
        "offers": {
            "@type": "Offer",
            "price": 9000,
            "priceCurrency": "EUR",
            "itemOffered": {"@type": "Car", "name": "Synthetic SUV 1.5"},
        },
    }
    nodes = parse_json_ld_blocks([json.dumps(car)]).nodes
    assert [sorted(local_type_names(n)) for n in nodes] == [["Car"]]
    html = (
        f"{SYNTHETIC_MARKER}\n<!doctype html><html lang='it'><head><title>Synthetic</title>"
        f"<script type='application/ld+json'>{json.dumps(car)}</script></head>"
        "<body><h1>Synthetic SUV</h1><p>Synthetic detail body with enough visible text.</p></body></html>"
    )
    parsed = SchemaOrgDealerAdapter(_config()).parse_detail(raw_document(DETAIL_URL, html))
    assert parsed.listing is not None, parsed.errors
    assert (parsed.listing.price.amount_minor, parsed.listing.vehicle.mileage_km) == (900000, Decimal(120000))


def test_a_generic_production_date_is_never_a_first_registration() -> None:
    """Regression: a dealer's year of manufacture became a day-precision registration date."""
    adapter = SchemaOrgDealerAdapter(_config())
    html = _detail_html({"productionDate": "2018-11-20"})
    listing = adapter.parse_detail(raw_document(DETAIL_URL, html)).listing
    assert listing is not None
    assert (listing.vehicle.first_registration.value, listing.vehicle.production_year) == (None, 2018)
    assert "FIRST_REGISTRATION_FROM_PRODUCTION_DATE" not in listing.warnings
