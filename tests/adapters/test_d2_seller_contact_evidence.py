"""F1 (wave D2): the schema.org dealer adapter reports exact-ad seller-contact evidence.

``ParsedListing.seller_contact`` carries what the advertisement itself shows about reaching the
seller: the e-mail addresses visible on the page with where they appear and the visible text
around them, how many distinct addresses the page shows, whether only a contact form exists, the
seller's type/name/website and the seller-written ad text for the language decision. It is never
part of the stored revision (``NormalizedListing`` holds no contact data). SYNTHETIC fixtures.
"""

from __future__ import annotations

import pytest
from tests.adapters.conftest import DetailFn, car_page, raw_document

from suv_deals.adapters.base import ParsedListing
from suv_deals.adapters.dealer_inventory import SchemaOrgDealerAdapter
from suv_deals.domain.enums import SellerType

DE = "https://dealer.example/fahrzeug"
LD = (
    '{"@context": "https://schema.org", "@type": "Car", "name": "Example Trail", "sku": "TEST-500",'
    ' "brand": "Example", "model": "Trail", "description": "Verkaufen unseren gepflegten'
    ' Geländewagen, das Fahrzeug ist unfallfrei und das Scheckheft ist gepflegt.",'
    ' "offers": {"@type": "Offer", "price": 2750, "priceCurrency": "EUR",'
    ' "seller": {"@type": "AutoDealer", "name": "Example Fixture Autohaus"}}}'
)


async def test_shown_dealer_email_is_reported_with_its_excerpt(
    detail: DetailFn, de_adapter: SchemaOrgDealerAdapter
) -> None:
    parsed = await detail(de_adapter, f"{DE}/TEST-224")
    assert parsed.listing is not None, parsed.errors
    contact = parsed.seller_contact
    assert contact is not None
    assert contact.seller_type == SellerType.DEALER
    assert contact.seller_name == "Example Fixture Autohaus (synthetisch)"
    assert contact.dealer_website == "https://dealer.example/"
    assert contact.distinct_addresses == 1
    assert [(e.address, e.location) for e in contact.emails] == [
        ("verkauf@dealer.example", "listing_contact_block")
    ]
    assert "verkauf@dealer.example" in contact.emails[0].excerpt
    assert contact.contact_form is False and contact.contact_reveal_restricted is False
    assert contact.page_language == "de"
    description = [t for t in contact.ad_texts if t.field == "description"]
    assert description and description[0].seller_written and "Geländewagen" in description[0].text
    # The stored revision never carries the address.
    assert "verkauf@dealer.example" not in parsed.listing.model_dump_json()


async def test_contact_form_only_page_reports_no_address(
    detail: DetailFn, de_adapter: SchemaOrgDealerAdapter
) -> None:
    parsed = await detail(de_adapter, f"{DE}/TEST-204")
    contact = parsed.seller_contact
    assert contact is not None
    assert contact.emails == () and contact.distinct_addresses == 0
    assert contact.contact_form is True
    assert contact.dealer_website == "https://dealer.example/"  # the dealer's own inventory site


@pytest.mark.parametrize(
    ("body", "count"),
    [
        (
            '<section class="contact"><a href="mailto:a@dealer.example">a@dealer.example</a>'
            ' <a href="mailto:b@dealer.example">b@dealer.example</a></section>',
            2,
        ),
        ('<p>Schreiben Sie an info@dealer.example oder <a href="mailto:info@dealer.example">hier</a></p>', 1),
    ],
)
def test_distinct_addresses_are_counted(de_adapter: SchemaOrgDealerAdapter, body: str, count: int) -> None:
    parsed: ParsedListing = de_adapter.parse_detail(raw_document(f"{DE}/TEST-500", car_page(LD, body)))
    assert parsed.seller_contact is not None
    assert parsed.seller_contact.distinct_addresses == count


def test_address_inside_the_seller_text_is_located_there(de_adapter: SchemaOrgDealerAdapter) -> None:
    body = '<div itemprop="description">Fragen bitte an fahrzeuge@dealer.example senden.</div>'
    parsed = de_adapter.parse_detail(raw_document(f"{DE}/TEST-500", car_page(LD, body)))
    contact = parsed.seller_contact
    assert contact is not None
    assert [(e.address, e.location) for e in contact.emails] == [
        ("fahrzeuge@dealer.example", "listing_description")
    ]


def test_non_ok_pages_carry_no_contact(de_adapter: SchemaOrgDealerAdapter) -> None:
    parsed = de_adapter.parse_detail(raw_document(f"{DE}/TEST-500", "<html><body></body></html>"))
    assert parsed.seller_contact is None
