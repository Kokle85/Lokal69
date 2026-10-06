"""Discovery (search page) behaviour of the schema.org dealer adapter on SYNTHETIC fixtures."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from tests.adapters.conftest import SearchFn, car_page, fixture_config, raw_document

from suv_deals.adapters.base import CrawlClient, SearchRequest
from suv_deals.adapters.dealer_inventory import SchemaOrgDealerAdapter
from suv_deals.adapters.fixture_client import FixtureCrawlClient
from suv_deals.domain.enums import AccessState, Completeness, CoverageMode, ProfileKey
from suv_deals.domain.listings import sha256_json
from suv_deals.domain.profiles import load_business_config
from suv_deals.errors import ValidationFailed

DE = "https://dealer.example"


async def test_paginated_search_page_one(de_adapter: SchemaOrgDealerAdapter, search: SearchFn) -> None:
    page = await search(de_adapter, f"{DE}/suche?typ=suv")
    assert page.access_state == AccessState.OK
    assert page.page_type == "search"
    assert page.has_more is True
    assert page.next_url == f"{DE}/suche?typ=suv&seite=2"
    assert page.completeness == Completeness.PARTIAL
    assert page.result_count_reported == 4
    assert page.access_evidence is not None and "dropped 1" in page.access_evidence
    urls = [o.canonical_url for o in page.observations]
    assert urls == [f"{DE}/fahrzeug/TEST-204", f"{DE}/fahrzeug/TEST-206", f"{DE}/fahrzeug/TEST-207"]
    first = page.observations[0]
    assert first.source_listing_id == "TEST-204"
    assert first.title == "Example Trail 2.0 Diesel 4x4"
    assert (first.card_price_minor, first.card_currency) == (275000, "EUR")
    assert first.card_mileage_km == Decimal(187500)
    assert isinstance(first.card_mileage_km, Decimal)
    assert page.observations[2].card_mileage_km == Decimal(156000)  # "156.000" string in JSON-LD
    assert [o.position for o in page.observations] == [0, 1, 2]
    assert all("tracker.example" not in o.canonical_url for o in page.observations)


async def test_paginated_search_last_page(de_adapter: SchemaOrgDealerAdapter, search: SearchFn) -> None:
    page = await search(de_adapter, f"{DE}/suche?typ=suv&seite=2", page_number=2)
    assert page.has_more is False and page.next_url is None
    assert page.completeness == Completeness.COMPLETE
    assert [o.canonical_url for o in page.observations] == [
        f"{DE}/fahrzeug/TEST-208",
        f"{DE}/fahrzeug/TEST-211",
    ]
    ridge = page.observations[0]
    assert ridge.card_mileage_km == Decimal(200000)  # parsed exactly; eligibility rejects later
    bare = page.observations[1]
    assert bare.card_price_minor is None and bare.source_listing_id is None  # unknown stays unknown


async def test_budget_limited_when_page_budget_reached(
    de_adapter: SchemaOrgDealerAdapter, search: SearchFn
) -> None:
    budget = de_adapter.config.rate_budget.max_search_pages_per_run
    page = await search(de_adapter, f"{DE}/suche?typ=suv", page_number=budget)
    assert page.has_more is True
    assert page.completeness == Completeness.BUDGET_LIMITED


async def test_card_hash_stable_under_reordering_badges_and_tracking(
    de_adapter: SchemaOrgDealerAdapter, search: SearchFn
) -> None:
    original = await search(de_adapter, f"{DE}/suche?typ=suv")
    reordered = await search(de_adapter, f"{DE}/suche?typ=suv&variante=umsortiert")
    by_url_a = {o.canonical_url: o for o in original.observations}
    by_url_b = {o.canonical_url: o for o in reordered.observations}
    assert set(by_url_a) == set(by_url_b)
    for url, obs in by_url_a.items():
        other = by_url_b[url]
        assert obs.card_hash == other.card_hash, url
        assert obs.card_hash_material == other.card_hash_material
    assert [o.position for o in original.observations] != [
        by_url_b[o.canonical_url].position for o in original.observations
    ]
    for obs in original.observations:
        assert obs.card_hash == sha256_json(obs.card_hash_material)
        assert set(obs.card_hash_material) == {
            "canonical_url",
            "source_listing_id",
            "title",
            "price_minor",
            "currency",
            "mileage_km",
            "source_modified_at",
        }


def test_card_hash_changes_with_meaningful_card_fields(de_adapter: SchemaOrgDealerAdapter) -> None:
    def card(price: str, name: str = "Example Trail") -> str:
        return car_page(
            '{"@type":"ItemList","itemListElement":[{"@type":"ListItem","url":"https://dealer.example/fahrzeug/'
            f'TEST-300","item":{{"@type":"Car","name":"{name}","offers":{{"price":"{price}",'
            '"priceCurrency":"EUR"}}}]}'
        )

    request = SearchRequest(source_key="fixture_dealer_de", profile_key="primary", url=f"{DE}/suche")
    base = de_adapter.parse_search(request, raw_document(f"{DE}/suche", card("2750.00")))
    cheaper = de_adapter.parse_search(request, raw_document(f"{DE}/suche", card("2650.00")))
    renamed = de_adapter.parse_search(
        request, raw_document(f"{DE}/suche", card("2750.00", "Example Trail II"))
    )
    assert base.observations[0].card_hash != cheaper.observations[0].card_hash
    assert base.observations[0].card_hash != renamed.observations[0].card_hash


async def test_empty_result_is_ok_and_complete(de_adapter: SchemaOrgDealerAdapter, search: SearchFn) -> None:
    page = await search(de_adapter, f"{DE}/suche?typ=cabrio")
    assert page.observations == ()
    assert page.access_state == AccessState.OK
    assert page.completeness == Completeness.COMPLETE
    assert page.has_more is False
    assert page.result_count_reported == 0


async def test_empty_result_by_wording_only(it_adapter: SchemaOrgDealerAdapter, search: SearchFn) -> None:
    page = await search(it_adapter, "https://concessionario.example/usato/ricerca?categoria=cabrio")
    assert page.observations == ()
    assert (page.access_state, page.completeness) == (AccessState.OK, Completeness.COMPLETE)


async def test_anchor_fallback_without_json_ld(de_adapter: SchemaOrgDealerAdapter, search: SearchFn) -> None:
    page = await search(de_adapter, f"{DE}/suche?typ=liste")
    assert page.access_state == AccessState.OK
    assert [o.canonical_url for o in page.observations] == [
        f"{DE}/fahrzeug/TEST-204",
        f"{DE}/fahrzeug/TEST-205",
    ]
    trail, ridge = page.observations
    assert trail.title == "Example Trail 2.0 Diesel 4x4"  # title attribute, badge excluded
    assert (trail.card_price_minor, trail.card_currency) == (275000, "EUR")  # "ab 99 EUR mtl." skipped
    assert trail.card_mileage_km == Decimal(187500)
    assert ridge.card_price_minor is None  # "Preis auf Anfrage"
    assert ridge.card_mileage_km == Decimal(165000)
    assert page.completeness == Completeness.COMPLETE


async def test_off_policy_links_and_next_are_dropped(
    de_adapter: SchemaOrgDealerAdapter, search: SearchFn
) -> None:
    page = await search(de_adapter, f"{DE}/suche?typ=offpolicy")
    assert [o.canonical_url for o in page.observations] == [f"{DE}/fahrzeug/TEST-204"]
    assert page.has_more is False and page.next_url is None
    assert page.completeness == Completeness.PARTIAL  # a next link existed but was refused
    assert page.access_evidence is not None
    assert "dropped 3" in page.access_evidence and "next link refused" in page.access_evidence
    assert de_adapter.discovery_outcome(page).unexpected_host is True


async def test_all_links_off_host_is_unexpected_content(
    de_adapter: SchemaOrgDealerAdapter, search: SearchFn
) -> None:
    page = await search(de_adapter, f"{DE}/suche?typ=fremd")
    assert page.observations == ()
    assert page.access_state == AccessState.UNEXPECTED_CONTENT
    assert page.completeness == Completeness.FAILED
    assert de_adapter.discovery_outcome(page).unexpected_host is True


@pytest.mark.parametrize(
    ("query", "state", "page_type", "completeness"),
    [
        ("blockiert", AccessState.ACCESS_BLOCKED, "challenge", Completeness.BLOCKED),
        ("gesperrt", AccessState.ACCESS_BLOCKED, "challenge", Completeness.BLOCKED),
        ("login", AccessState.ACCESS_BLOCKED, "login", Completeness.BLOCKED),
        ("shell", AccessState.UNEXPECTED_CONTENT, "empty_shell", Completeness.FAILED),
        ("langsam", AccessState.TRANSIENT_ERROR, "unknown", Completeness.FAILED),
        ("verbindung", AccessState.TRANSIENT_ERROR, "unknown", Completeness.FAILED),
        ("viel", AccessState.RATE_LIMITED, "unknown", Completeness.FAILED),
        ("wartung", AccessState.TRANSIENT_ERROR, "unknown", Completeness.FAILED),
        ("umleitung", AccessState.UNEXPECTED_CONTENT, "unknown", Completeness.FAILED),
        ("zu-detail", AccessState.UNEXPECTED_CONTENT, "detail", Completeness.FAILED),
    ],
)
async def test_blocked_and_failed_pages_carry_no_observations(
    de_adapter: SchemaOrgDealerAdapter,
    search: SearchFn,
    query: str,
    state: AccessState,
    page_type: str,
    completeness: Completeness,
) -> None:
    page = await search(de_adapter, f"{DE}/suche?typ={query}")
    assert page.observations == ()
    assert page.access_state == state
    assert page.page_type == page_type
    assert page.completeness == completeness
    assert page.has_more is False
    assert page.access_evidence


async def test_rate_limit_keeps_retry_after(de_adapter: SchemaOrgDealerAdapter, search: SearchFn) -> None:
    page = await search(de_adapter, f"{DE}/suche?typ=viel")
    assert page.fetch.retry_after_seconds == 120
    assert page.fetch.response_headers.get("retry-after") == "120"


def test_empty_list_vs_blocked_vs_unknown_page(de_adapter: SchemaOrgDealerAdapter) -> None:
    request = SearchRequest(source_key="fixture_dealer_de", profile_key="primary", url=f"{DE}/suche")
    unknown = de_adapter.parse_search(
        request,
        raw_document(
            f"{DE}/suche", "<html><body><p>" + "Willkommen im Autohaus. " * 5 + "</p></body></html>"
        ),
    )
    assert (
        unknown.access_state == AccessState.UNEXPECTED_CONTENT and unknown.completeness == Completeness.FAILED
    )
    empty = de_adapter.parse_search(
        request, raw_document(f"{DE}/suche", "<html><body><p>0 Treffer für Ihre Suche</p></body></html>")
    )
    assert empty.access_state == AccessState.OK and empty.completeness == Completeness.COMPLETE
    ten = de_adapter.parse_search(
        request,
        raw_document(f"{DE}/suche", "<html><body><p>10 Treffer, Liste wird geladen</p></body></html>"),
    )
    assert ten.access_state != AccessState.OK or ten.completeness != Completeness.COMPLETE


def test_next_link_loop_is_refused(de_adapter: SchemaOrgDealerAdapter) -> None:
    html = car_page(
        '{"@type":"ItemList","itemListElement":["https://dealer.example/fahrzeug/TEST-204"]}',
        body='<a rel="next" href="/suche?typ=suv">Weiter</a>',
    )
    request = SearchRequest(source_key="fixture_dealer_de", profile_key="primary", url=f"{DE}/suche?typ=suv")
    page = de_adapter.parse_search(request, raw_document(f"{DE}/suche?typ=suv", html))
    assert page.has_more is False and page.completeness == Completeness.PARTIAL


def test_reported_count_without_pagination_is_partial(de_adapter: SchemaOrgDealerAdapter) -> None:
    html = car_page(
        '{"@type":"ItemList","numberOfItems":25,"itemListElement":["https://dealer.example/fahrzeug/TEST-204"]}'
    )
    request = SearchRequest(source_key="fixture_dealer_de", profile_key="primary", url=f"{DE}/suche")
    page = de_adapter.parse_search(request, raw_document(f"{DE}/suche", html))
    assert page.completeness == Completeness.PARTIAL
    assert page.access_evidence is not None and "no next link" in page.access_evidence


def test_top_level_vehicle_nodes_as_cards(de_adapter: SchemaOrgDealerAdapter) -> None:
    html = car_page(
        '[{"@type":"Car","name":"A","url":"https://dealer.example/fahrzeug/TEST-401","offers":{"price":2600,'
        '"priceCurrency":"EUR"}},{"@type":"Car","name":"B","url":"/fahrzeug/TEST-402"}]'
    )
    request = SearchRequest(source_key="fixture_dealer_de", profile_key="primary", url=f"{DE}/suche")
    page = de_adapter.parse_search(request, raw_document(f"{DE}/suche", html))
    assert [o.canonical_url for o in page.observations] == [
        f"{DE}/fahrzeug/TEST-401",
        f"{DE}/fahrzeug/TEST-402",
    ]
    assert page.observations[0].card_price_minor == 260000


async def test_swiss_cards_with_apostrophes_and_miles(
    ch_adapter: SchemaOrgDealerAdapter, search: SearchFn
) -> None:
    page = await search(ch_adapter, "https://garage.example/occasionen?kategorie=suv")
    assert page.access_state == AccessState.OK
    cards = {o.source_listing_id: o for o in page.observations}
    assert set(cards) == {"40101", "40102", "40103"}
    assert (cards["40101"].card_price_minor, cards["40101"].card_currency) == (299000, "CHF")
    assert cards["40101"].card_mileage_km == Decimal(150000)
    assert cards["40102"].card_price_minor == 275050
    assert cards["40102"].card_mileage_km == Decimal(124274) * Decimal("1.609344")
    assert cards["40103"].card_price_minor == 345000  # leasing "CHF 199.- / Monat" skipped
    assert cards["40102"].canonical_url == "https://garage.example/occasion/40102"


async def test_italian_graph_item_list(it_adapter: SchemaOrgDealerAdapter, search: SearchFn) -> None:
    page = await search(it_adapter, "https://concessionario.example/usato/ricerca?categoria=suv")
    cards = {o.source_listing_id: o for o in page.observations}
    assert set(cards) == {"it301", "it302"}
    assert cards["it301"].card_price_minor == 290000
    assert cards["it301"].card_mileage_km == Decimal(199999)
    assert cards["it302"].card_mileage_km == Decimal(200000)
    assert cards["it302"].canonical_url == "https://concessionario.example/usato/auto/example-ridge-it302"
    assert page.completeness == Completeness.COMPLETE


def test_capabilities(de_adapter: SchemaOrgDealerAdapter, ch_adapter: SchemaOrgDealerAdapter) -> None:
    caps = de_adapter.capabilities()
    assert caps.coverage_mode == CoverageMode.ROLLING_PAGES
    assert caps.supports_modified_since is False
    assert caps.provides_listing_ids is True and caps.countries == ("DE",)
    assert ch_adapter.capabilities().provides_listing_ids is False


def test_build_search(de_adapter: SchemaOrgDealerAdapter, repo_root: Path) -> None:
    business = load_business_config(repo_root / "config")
    profile = business.profiles[ProfileKey.PRIMARY]
    first = de_adapter.build_search(profile, None)
    assert first.url == f"{DE}/suche?typ=suv" and first.page_number == 1 and first.profile_key == "primary"
    assert first.params == {"typ": "suv"}
    nxt = de_adapter.build_search(profile, f"{DE}/suche?typ=suv&seite=2&utm_source=x")
    assert nxt.url == f"{DE}/suche?typ=suv&seite=2" and nxt.page_number == 2
    with pytest.raises(ValidationFailed):
        de_adapter.build_search(profile, "https://tracker.example/suche")
    with pytest.raises(ValidationFailed):
        de_adapter.build_search(profile, f"{DE}/admin")
    no_url = SchemaOrgDealerAdapter(fixture_config("fixture_dealer_de").model_copy(update={"search": {}}))
    with pytest.raises(ValidationFailed):
        no_url.build_search(profile, None)


async def test_discover_refuses_off_policy_request_without_fetching(
    de_adapter: SchemaOrgDealerAdapter, client: FixtureCrawlClient
) -> None:
    request = SearchRequest(
        source_key="fixture_dealer_de", profile_key="primary", url="https://tracker.example/x"
    )
    page = await de_adapter.discover(request, client)
    assert page.access_state == AccessState.POLICY_DENIED
    assert page.completeness == Completeness.BLOCKED
    assert client.requests == []
    wrong = SearchRequest(source_key="fixture_dealer_it", profile_key="primary", url=f"{DE}/suche")
    with pytest.raises(ValidationFailed):
        await de_adapter.discover(wrong, client)


def test_fixture_client_is_a_crawl_client(client: FixtureCrawlClient) -> None:
    assert isinstance(client, CrawlClient)
