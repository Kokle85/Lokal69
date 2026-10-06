"""Technical-inspection wording and Swiss price wording in the dealer adapter (spec 7, 17, 19).

All pages are SYNTHETIC (fixtures/fixture_dealer_ch/MANIFEST.yaml or inline test strings).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.adapters.conftest import DetailFn, car_page, raw_document

from suv_deals.adapters.dealer_inventory import SchemaOrgDealerAdapter
from suv_deals.domain.enums import (
    AccessState,
    ClaimStatus,
    Confidence,
    ExtractionMethod,
    Precision,
    PriceBasis,
    PriceType,
)
from suv_deals.domain.listings import NormalizedListing

CH = "https://garage.example/occasion"
DE = "https://dealer.example/fahrzeug"
IT = "https://concessionario.example/usato/auto"
LD_DE = '{"@type":"Car","name":"Example Trail","offers":{"price":2750,"priceCurrency":"EUR"}}'
LD_CH = '{"@type":"Car","name":"Example Trail","offers":{"price":"2990","priceCurrency":"CHF"}}'


async def _fixture(detail: DetailFn, adapter: SchemaOrgDealerAdapter, url: str) -> NormalizedListing:
    parsed = await detail(adapter, url)
    assert parsed.access_state == AccessState.OK, parsed.errors
    assert parsed.listing is not None, parsed.errors
    return parsed.listing


def _inline(adapter: SchemaOrgDealerAdapter, url: str, json_ld: str, body: str) -> NormalizedListing:
    parsed = adapter.parse_detail(raw_document(url, car_page(json_ld, body)))
    assert parsed.listing is not None, parsed.errors
    return parsed.listing


# --------------------------------------------------------------------------- CH fixtures


async def test_swiss_export_price_and_fresh_mfk(ch_adapter: SchemaOrgDealerAdapter, detail: DetailFn) -> None:
    listing = await _fixture(detail, ch_adapter, f"{CH}/40104")
    price = listing.price
    # "Export ohne MWST": an export net amount, never an ordinary payable price (spec 17).
    assert (price.amount_minor, price.currency) == (275000, "CHF")
    assert price.type == PriceType.EXPORT_NET
    assert price.basis == PriceBasis.NET
    assert price.export_net_price_minor == 275000
    assert "PRICE_EXPORT_NET" in listing.warnings
    # "Occasion" is used-car wording only: no price or condition effect.
    assert listing.condition.damaged_vehicle == ClaimStatus.UNKNOWN
    # "Frisch ab MFK": a seller claim, never verified.
    assert listing.condition.roadworthy == ClaimStatus.SELLER_CLAIMED
    prov = listing.provenance["condition.roadworthy"]
    assert (prov.method, prov.confidence, prov.selector) == (
        ExtractionMethod.REGEX,
        Confidence.MEDIUM,
        "visible_text",
    )
    assert prov.raw_text is not None and "MFK" in prov.raw_text
    assert "never a verified inspection" in (prov.transformation or "")
    assert listing.documentation.inspection_expiry.value is None  # no date was stated
    assert "documentation.inspection_expiry" not in listing.provenance
    assert not any(w.startswith("INSPECTION_") for w in listing.warnings)


async def test_swiss_sold_without_mfk_is_never_positive(
    ch_adapter: SchemaOrgDealerAdapter, detail: DetailFn
) -> None:
    listing = await _fixture(detail, ch_adapter, f"{CH}/40105")
    assert listing.condition.roadworthy == ClaimStatus.UNKNOWN
    assert "condition.roadworthy" not in listing.provenance
    assert "INSPECTION_NOT_FRESH" in listing.warnings
    assert (listing.price.amount_minor, listing.price.basis) == (195000, PriceBasis.GROSS)
    assert listing.price.type == PriceType.FULL_VEHICLE_ASKING


async def test_pages_without_inspection_wording_stay_unknown(
    ch_adapter: SchemaOrgDealerAdapter, detail: DetailFn
) -> None:
    listing = await _fixture(detail, ch_adapter, f"{CH}/40101")
    assert listing.condition.roadworthy == ClaimStatus.UNKNOWN
    assert listing.documentation.inspection_expiry.precision == Precision.UNKNOWN
    assert not any("inspection" in key or "roadworthy" in key for key in listing.provenance)


# --------------------------------------------------------------------------- inline edge cases


def test_stated_future_hu_expiry_fills_documentation(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _inline(de_adapter, f"{DE}/TEST-601", LD_DE, "<p>Gepflegt, HU bis 05/2027, Zahnriemen neu.</p>")
    assert listing.condition.roadworthy == ClaimStatus.SELLER_CLAIMED
    expiry = listing.documentation.inspection_expiry
    assert (expiry.value, expiry.precision) == ("2027-05", Precision.MONTH)
    prov = listing.provenance["documentation.inspection_expiry"]
    assert (prov.method, prov.confidence) == (ExtractionMethod.REGEX, Confidence.MEDIUM)
    assert prov.raw_text == "HU bis 05/2027"
    assert "INSPECTION_EXPIRED" not in listing.warnings


def test_two_digit_hu_year_is_expanded_and_flagged(de_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _inline(de_adapter, f"{DE}/TEST-602", LD_DE, "<p>TÜV neu, HU 05/28.</p>")
    assert listing.documentation.inspection_expiry.value == "2028-05"
    assert listing.condition.roadworthy == ClaimStatus.SELLER_CLAIMED
    assert "INSPECTION_TWO_DIGIT_YEAR_EXPANDED" in listing.warnings


def test_expiry_before_observed_at_warns_and_is_never_positive(it_adapter: SchemaOrgDealerAdapter) -> None:
    # observed_at is 2026-10-06; May 2026 has passed.
    listing = _inline(
        it_adapter, f"{IT}/example-trail-it601", LD_DE, "<p>Ottime condizioni, revisione fino a 05/2026.</p>"
    )
    assert "INSPECTION_EXPIRED" in listing.warnings
    assert listing.condition.roadworthy == ClaimStatus.SELLER_DENIED
    assert listing.documentation.inspection_expiry.value == "2026-05"  # the stated fact is kept


def test_mfk_until_past_month_is_expired(ch_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _inline(ch_adapter, f"{CH}/40601", LD_CH, "<p>Occasion, MFK bis 06/2026.</p>")
    assert "INSPECTION_EXPIRED" in listing.warnings
    assert listing.condition.roadworthy == ClaimStatus.SELLER_DENIED
    assert listing.documentation.inspection_expiry.value == "2026-06"


def test_conditional_mfk_offer_is_not_a_claim(ch_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _inline(ch_adapter, f"{CH}/40602", LD_CH, "<p>Auf Wunsch frisch ab MFK (+ CHF 500.-).</p>")
    assert listing.condition.roadworthy == ClaimStatus.UNKNOWN
    assert "INSPECTION_CONDITIONAL" in listing.warnings


def test_contradictory_inspection_wording_is_a_conflict(ch_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _inline(
        ch_adapter, f"{CH}/40603", LD_CH, "<p>Preis vor MFK CHF 2'990.-, Preis ab MFK CHF 3'400.-</p>"
    )
    assert listing.condition.roadworthy == ClaimStatus.CONFLICTING
    assert "INSPECTION_CONFLICTING" in listing.warnings
    conflict = next(c for c in listing.conflicts if c.field == "condition.roadworthy")
    assert set(conflict.values) == {"vor MFK", "ab MFK"}


def test_swiss_french_export_and_inspection_wording(ch_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _inline(
        ch_adapter,
        f"{CH}/40604",
        LD_CH,
        "<p>Occasion expertisée du jour. Prix export CHF 2'990.- hors TVA.</p>",
    )
    assert listing.condition.roadworthy == ClaimStatus.SELLER_CLAIMED
    assert listing.price.type == PriceType.EXPORT_NET
    assert listing.price.basis == PriceBasis.NET


def test_vat_not_shown_wording_is_not_a_net_price(ch_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _inline(ch_adapter, f"{CH}/40605", LD_CH, "<p>CHF 2'990.- ohne MWST-Ausweis.</p>")
    assert listing.price.basis != PriceBasis.NET


# --------------------------------------------------------------------------- review regressions


def test_spec_table_negative_answer_is_never_positive(ch_adapter: SchemaOrgDealerAdapter) -> None:
    # Visible text joins a definition list as "Ab MFK Nein": a "no" answer, not a fresh MFK.
    listing = _inline(
        ch_adapter,
        f"{CH}/40606",
        LD_CH,
        "<dl><dt>Ab MFK</dt><dd>Nein</dd><dt>Garantie</dt><dd>Ja</dd></dl>",
    )
    assert listing.condition.roadworthy == ClaimStatus.UNKNOWN
    assert "condition.roadworthy" not in listing.provenance
    assert "INSPECTION_NOT_FRESH" in listing.warnings


def test_italian_engine_overhaul_is_not_a_roadworthiness_claim(it_adapter: SchemaOrgDealerAdapter) -> None:
    listing = _inline(
        it_adapter,
        f"{IT}/example-trail-it602",
        LD_DE,
        "<p>Motore revisionato, cambio automatico revisionato.</p>",
    )
    assert listing.condition.roadworthy == ClaimStatus.UNKNOWN
    assert not any(w.startswith("INSPECTION_") for w in listing.warnings)


@pytest.mark.parametrize(
    ("fetched_at", "claim", "expired"),
    [
        # 2026-10-31 22:30 in Zurich: October, the stated month, is still running.
        (datetime(2026, 10, 31, 21, 30, tzinfo=UTC), ClaimStatus.SELLER_CLAIMED, False),
        # 2026-11-01 00:30 in Zurich (still 31 October in UTC): the MFK month has passed.
        (datetime(2026, 10, 31, 23, 30, tzinfo=UTC), ClaimStatus.SELLER_DENIED, True),
    ],
)
def test_expiry_is_compared_with_the_observation_day_in_the_source_zone(
    ch_adapter: SchemaOrgDealerAdapter, fetched_at: datetime, claim: ClaimStatus, expired: bool
) -> None:
    document = raw_document(f"{CH}/40607", car_page(LD_CH, "<p>Occasion, MFK bis 10/2026.</p>"))
    document = document.model_copy(update={"fetched_at": fetched_at})
    parsed = ch_adapter.parse_detail(document)
    assert parsed.listing is not None, parsed.errors
    assert parsed.listing.condition.roadworthy == claim
    assert ("INSPECTION_EXPIRED" in parsed.listing.warnings) is expired
    assert parsed.listing.documentation.inspection_expiry.value == "2026-10"
