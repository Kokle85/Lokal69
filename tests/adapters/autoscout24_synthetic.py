"""SYNTHETIC AutoScout24 pages that mirror the STRUCTURE of the owner's captures (2026-10-15/16).

Only key names, nesting and value kinds come from the captures (Next.js ``__NEXT_DATA__`` with
``props.pageProps.listings`` on search pages, ``props.pageProps.listingDetails`` plus a JSON-LD
``Product.offers.itemOffered`` Car on Italian detail pages). Every listing value is INVENTED
(price 7400, 143200 km, 09-2018, synthetic UUIDs, timestamps and slugs) and differs from the
captured listings; only generic engine specifications (a 1.5 dCi with 81 kW / 1461 cm3, a TCe 140
with 103 kW / 1332 cm3) are model facts any brochure states. The seller/location objects carry
obvious placeholders, so the tests can prove that no seller name, phone, street or link is ever
copied. No real page content, dealer, address or photo URL is stored here.
"""

from __future__ import annotations

import json
from typing import Any

SYNTHETIC_MARKER = "<!-- SYNTHETIC FIXTURE: not a real vehicle or offer -->"
IT_HOST = "www.autoscout24.it"
DE_HOST = "www.autoscout24.de"
SELLER_PLACEHOLDERS = ("Synthetic Dealer Srl", "Mario Synthetic", "Via Synthetic 1", "[redacted-phone]")


def uuid_for(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _json(value: Any) -> str:
    # The real pages escape "<" inside the JSON (\u003c), so no "</script>" can occur in it.
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def card(
    n: int,
    *,
    country: str = "IT",
    make: str = "Renault",
    model: str = "Captur",
    model_group: str | None = "Captur",
    version: str = "Captur 1.5 dCi 110CV Synthetic Edition",
    price: int | None = 7400,
    price_formatted: str | None = None,
    tracking_price: str | None = None,
    km: int = 143200,
    km_text: str | None = None,
    first_registration: str = "09-2018",
    fuel_code: str = "d",
    fuel_text: str = "Diesel",
    transmission: str = "Manuale",
    power: str = "81 kW (110 CV)",
    displacement: str | None = "1.461 cm³",
    seller_type: str = "Dealer",
    result_type: str = "Organic",
    section: str = "Main",
    offer_type: str = "U",
    listing_id: str | None = None,
    url_id: str | None = None,
    slug: str = "renault-captur-captur-1-5-dci-synthetic-diesel-bianco-cat_ma60mo20235",
    vat_label: str | None = None,
    conditional: bool = False,
) -> dict[str, Any]:
    """One ``pageProps.listings[]`` entry (Italian or German wording via the arguments)."""
    lid = listing_id or uuid_for(n)
    prefix = "/annunci/" if country == "IT" else "/angebote/"
    thousands = f"{km:,}".replace(",", ".")
    price_text = (
        price_formatted
        if price_formatted is not None
        else (None if price is None else f"€ {price:,}".replace(",", "."))
    )
    price_obj: dict[str, Any] = {
        "priceFormatted": price_text,
        "priceEvaluation": 0,
        "isVatLabelLegallyRequired": False,
        "priceRaw": price,
        "isConditionalPrice": conditional,
    }
    if vat_label is not None:
        price_obj["vatLabel"] = vat_label
    vehicle: dict[str, Any] = {
        "articleType": "Car",
        "type": "Car",
        "make": make,
        "model": model,
        "modelGroup": model_group,
        "variant": None,
        "motorTypeName": None,
        "modelId": 20235,
        "modelVersionInput": version,
        "offerType": offer_type,
        "transmission": transmission,
        "fuel": fuel_text,
        "mileageInKm": km_text if km_text is not None else f"{thousands} km",
    }
    if displacement is not None:
        vehicle["engineDisplacementInCCM"] = displacement
    mm, yyyy = first_registration.split("-")
    return {
        "id": lid,
        "title": f"{make} {model}",
        "identifier": {"legacyId": None, "crossReferenceId": "SYN-1"},
        "price": price_obj,
        "url": f"{prefix}{slug}-{url_id or lid}",
        "vehicle": vehicle,
        "searchResultType": result_type,
        "searchResultSection": section,
        "isOcs": False,
        "tracking": {
            "firstRegistration": first_registration,
            "fuelType": fuel_code,
            "mileage": str(km),
            "price": tracking_price if tracking_price is not None else ("" if price is None else str(price)),
            "imageContent": "no-placeholder|0.5",
            "modelTaxonomy": "[make_id:60, model_group_id:201269, variant_id:, generation_id:];",
        },
        "vehicleDetails": [
            {"data": vehicle["mileageInKm"], "iconName": "mileage_odometer", "ariaLabel": "Chilometraggio"},
            {"data": transmission, "iconName": "gearbox", "ariaLabel": "Cambio"},
            {"data": f"{mm}/{yyyy}", "iconName": "calendar", "ariaLabel": "Anno"},
            {"data": fuel_text, "iconName": "gas_pump", "ariaLabel": "Carburante"},
            {"data": power, "iconName": "speedometer", "ariaLabel": "Potenza"},
        ],
        "seller": {
            "id": "synthetic-seller",
            "type": seller_type,
            "companyName": SELLER_PLACEHOLDERS[0],
            "contactName": SELLER_PLACEHOLDERS[1],
            "phones": [{"callTo": SELLER_PLACEHOLDERS[3]}],
            "links": {"infoPage": "/concessionari/synthetic"},
        },
        "location": {
            "countryCode": country,
            "zip": "SYNZIP",
            "city": "Synthville",
            "street": SELLER_PLACEHOLDERS[2],
        },
        "images": ["https://images.invalid/listing-images/synthetic.jpg"],
        "trackingParameters": [{"key": "boost_level", "value": "t50"}],
    }


def search_page(
    listings: list[Any] | None,
    *,
    number_of_results: int | None = None,
    number_of_pages: int = 1,
    lang: str = "it-IT",
    pageid: str = "list",
    next_data: str | None = None,
    include_next_data: bool = True,
    title: str = "Synthetic results",
) -> str:
    """A search page with ``__NEXT_DATA__``; ``next_data`` replaces the script body verbatim."""
    props: dict[str, Any] = {
        "pageid": pageid,
        "numberOfResults": len(listings or []) if number_of_results is None else number_of_results,
        "numberOfPages": number_of_pages,
        "numberOfOcsResults": 0,
        "pageQuery": {"sort": "age", "desc": "1", "ustate": "U", "kmto": "200000", "cy": "I", "atype": "C"},
        "listings": listings,
    }
    if listings is None:
        props.pop("listings")
    body = (
        next_data
        if next_data is not None
        else _json(
            {"props": {"pageProps": props}, "page": "/lst/[...slug]", "query": {}, "buildId": "synthetic"}
        )
    )
    script = (
        f'<script id="__NEXT_DATA__" type="application/json">{body}</script>' if include_next_data else ""
    )
    breadcrumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": []}
    return (
        f"{SYNTHETIC_MARKER}\n<!doctype html><html lang='{lang}'><head><title>{title}</title>"
        f"<script type='application/ld+json'>{json.dumps(breadcrumbs)}</script></head>"
        "<body><h1>Synthetic results</h1><main><p>Synthetic search page body with enough visible text"
        f" for the access classifier.</p></main>{script}</body></html>"
    )


def detail_page(
    n: int = 1,
    *,
    listing_id: str | None = None,
    nd_id: str | None = None,
    include_next_data: bool = True,
    include_json_ld: bool = True,
    km: int = 143200,
    ld_km: int | None = None,
    power_kw: int = 81,
    ld_power_kw: int | None = None,
    drive: str = "Anteriore",
    fuel_code: str = "D",
    plugin: bool = False,
    price: int = 7400,
    description: str = "<p>Synthetic description.</p>",
    version: str = "Captur 1.5 dci 110CV Synthetic",
    status: str = "Active",
    conditional: bool = False,
    final_price: bool = True,
    ld_identifier: str | None = None,
    ld_production_date: str = "2018-09-01",
) -> str:
    """An Italian ``/annunci/`` detail page: ``listingDetails`` and/or the JSON-LD Product/Offer/Car.

    The defaults are a clean page (no net-price wording, no commercial registration, a final and
    unconditional price); ``ld_identifier`` / ``ld_production_date`` let the JSON-LD Car disagree
    with ``__NEXT_DATA__``.
    """
    lid = listing_id or uuid_for(n)
    url = f"https://{IT_HOST}/annunci/renault-captur-synthetic-diesel-bianco-cat_ma60mo20235-{lid}"
    car = {
        "@type": "Car",
        "name": "Renault Captur",
        "manufacturer": "Renault",
        "model": "Captur",
        "color": "Bianco",
        "bodyType": "SUV/Fuoristrada/Pick-up",
        "identifier": ld_identifier or lid,
        "productionDate": ld_production_date,
        "driveWheelConfiguration": drive,
        "vehicleTransmission": "Manuale",
        "numberOfDoors": 5,
        "seatingCapacity": 5,
        "mileageFromOdometer": {"@type": "QuantitativeValue", "value": ld_km or km, "unitText": "KMT"},
        "vehicleEngine": [
            {
                "@type": "EngineSpecification",
                "fuelType": "Diesel",
                "enginePower": [
                    {"@type": "QuantitativeValue", "value": 110, "unitCode": "BHP"},
                    {"@type": "QuantitativeValue", "value": ld_power_kw or power_kw, "unitCode": "KWT"},
                ],
                "engineDisplacement": {"@type": "QuantitativeValue", "value": 1461, "unitCode": "CMQ"},
            }
        ],
        "emissionsCO2": 105,
    }
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "brand": {"@type": "Brand", "name": "Renault"},
        "name": f"Renault per € {price}",
        "description": description,
        "offers": {
            "@type": "Offer",
            "price": price,
            "priceCurrency": "EUR",
            "itemCondition": "UsedCondition",
            "url": url,
            "availability": "InStock",
            "offeredBy": {
                "@type": "AutoDealer",
                "name": SELLER_PLACEHOLDERS[0],
                "telephone": SELLER_PLACEHOLDERS[3],
                "address": {
                    "@type": "PostalAddress",
                    "streetAddress": SELLER_PLACEHOLDERS[2],
                    "addressCountry": "IT",
                },
            },
            "itemOffered": car,
        },
    }
    details = {
        "id": nd_id or lid,
        "status": status,
        "title": "Renault Captur",
        "createdTimestampWithOffset": "2026-01-01T00:00:00Z",
        "description": description,
        "price": {
            "priceFormatted": f"€ {price}",
            "priceEvaluation": 0,
            "isVatLabelLegallyRequired": False,
            "isConditionalPrice": conditional,
        },
        "prices": {
            "isFinalPrice": final_price,
            "public": {
                "price": f"€ {price}",
                "priceRaw": price,
                "taxDeductible": True,
                "negotiable": False,
                "onRequestOnly": False,
                "netPrice": None,
                "netPriceRaw": None,
                "vatRate": None,
            },
        },
        "seller": {
            "type": "Dealer",
            "isDealer": True,
            "companyName": SELLER_PLACEHOLDERS[0],
            "contactName": SELLER_PLACEHOLDERS[1],
            "phones": [SELLER_PLACEHOLDERS[3]],
        },
        "location": {
            "countryCode": "IT",
            "zip": "SYNZIP",
            "city": "Synthville",
            "street": SELLER_PLACEHOLDERS[2],
            "latitude": 45.0,
            "longitude": 9.0,
        },
        "vehicle": {
            "make": "Renault",
            "model": "Captur",
            "modelVersionInput": version,
            "rawPowerInKw": power_kw,
            "rawPowerInHp": 110,
            "rawDisplacementInCCM": 1461,
            "firstRegistrationDateRaw": "2018-09-01",
            "firstRegistrationDate": "09/2018",
            "productionYear": "2018",
            "mileageInKmRaw": km,
            "mileageInKm": f"{km} km",
            "fuelCategory": {"raw": fuel_code, "formatted": "Diesel"},
            "transmissionType": "Manuale",
            "driveTrain": drive,
            "bodyType": "SUV/Fuoristrada/Pick-up",
            "offerType": "U",
            "numberOfSeats": 5,
            "nextVehicleSafetyInspection": "10/2027",
            "rawData": {
                "engine": {
                    "transmissionType": {"raw": "Manual", "formatted": "Manuale"},
                    "driveTrain": {"formatted": drive},
                },
                "fuels": {
                    "isPluginHybrid": {"raw": plugin},
                    "fuelCategory": {"raw": fuel_code},
                    "primary": {"co2emissionInGramPerKmWithFallback": {"raw": 105, "isFallback": False}},
                },
                "condition": {
                    "firstRegistrationDate": {"raw": "2018-09-01", "formatted": "09/2018"},
                    "mileageInKm": {"raw": km},
                    "numberOfPreviousOwnersExtended": {"raw": 2},
                    "damage": {"isCurrentlyDamaged": None, "accidentFree": True},
                },
                "bodyType": {"formatted": "SUV/Fuoristrada/Pick-up", "raw": "Offroad"},
                "productionDate": {"formatted": "03/2018", "raw": "2018-03-01"},
            },
        },
    }
    next_data = {
        "props": {"pageProps": {"pageid": "detail", "listingDetails": details}},
        "page": "/details/[...slug]",
        "buildId": "synthetic",
    }
    ld_script = (
        f"<script type='application/ld+json'>{json.dumps(product)}</script>" if include_json_ld else ""
    )
    nd_script = (
        f'<script id="__NEXT_DATA__" type="application/json">{_json(next_data)}</script>'
        if include_next_data
        else ""
    )
    return (
        f"{SYNTHETIC_MARKER}\n<!doctype html><html lang='it-IT'><head><title>Renault Captur synthetic</title>"
        f"{ld_script}</head><body><main><h1>Renault Captur</h1><p>Synthetic detail page body with enough"
        f" visible text for the access classifier.</p></main>{nd_script}</body></html>"
    )


def detail_url(n: int = 1, listing_id: str | None = None) -> str:
    lid = listing_id or uuid_for(n)
    return f"https://{IT_HOST}/annunci/renault-captur-synthetic-diesel-bianco-cat_ma60mo20235-{lid}"
