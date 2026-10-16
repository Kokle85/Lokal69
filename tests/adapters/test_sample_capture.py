"""Owner-run sample capture guard rails (suv_deals.crawling.sample_capture). SYNTHETIC pages, no network.

Every test drives `capture_samples` with a fake fetcher and a fake resolver, so the guard rails
(reviewed path, robots.txt, redirects, pacing, blocks, challenge pages, card_only, redaction) are
checked without touching a marketplace.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from suv_deals.adapters.base import FetchOutcome, RawDocument
from suv_deals.adapters.crawl4ai_client import CrawlFetchResult
from suv_deals.crawling.robots import RobotsFetchResponse
from suv_deals.crawling.sample_capture import (
    CaptureOutcome,
    CaptureRefused,
    capture_samples,
    challenge_reason,
    crawl4ai_page_fetcher,
    offer_stats,
    outline_html,
    redact_html,
)
from suv_deals.domain.enums import AccessState
from suv_deals.domain.sources import SourceConfig

HOST = "www.sample-market.it"
SEARCH = f"https://{HOST}/lst/toyota/rav-4"
ROBOTS = f"https://{HOST}/robots.txt"
DETAIL_1 = f"https://{HOST}/annunci/sample-car-1"
DETAIL_2 = f"https://{HOST}/annunci/sample-car-2"
ALLOW_ALL = b"User-agent: *\nDisallow: /private/\n"
SEARCH_HTML = (
    b"<!doctype html><html><head><title>Sample results</title>"
    b'<script type="application/ld+json">{"@context":"https://schema.org",'
    b'"@graph":[{"@type":"Car","offers":{"price":"2990"}}]}</script></head><body>'
    b"<h1>Results</h1>"
    b'<div class="card listing"><a href="/annunci/sample-car-1">Car 1</a>'
    b'<a href="tel:+393331234567">call</a>'
    b"<span>Contact seller@example.org or +39 333 123 4567</span></div>"
    b'<div class="card listing"><a href="/annunci/sample-car-2">Car 2</a></div>'
    b'<a href="https://other.example.net/annunci/elsewhere">off-host</a>'
    b'<a href="/angebote/not-reviewed">not reviewed</a>'
    b"</body></html>"
)
DETAIL_HTML = b"<html><head><title>Car</title></head><body><h1>Car</h1></body></html>"


def sample_source(**overrides: Any) -> SourceConfig:
    data: dict[str, Any] = {
        "source_key": "sample_market_it",
        "display_name": "Sample market (IT)",
        "country": "IT",
        "role": "acquisition",
        "mode": "public_html",
        "adapter": "sample_market_it",
        "adapter_version": "unimplemented",
        "terms_decision": "proceed_acknowledged",
        "terms_decision_actor": "owner (test)",
        "allowed_hosts": [HOST],
        "allowed_search_paths": [r"^/lst/[a-z0-9-]+(/[a-z0-9-]+){0,2}$"],
        "allowed_detail_paths": [r"^/annunci/[a-z0-9-]+$"],
    }
    data.update(overrides)
    return SourceConfig.model_validate(data)


class FakeSite:
    """Answers by full URL; anything unknown is a 404. Records every request in order."""

    def __init__(self, responses: dict[str, RobotsFetchResponse]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    async def __call__(self, url: str) -> RobotsFetchResponse:
        self.requests.append(url)
        return self.responses.get(url, RobotsFetchResponse(404, b"not found", None))


class FrozenClock:
    def now(self) -> datetime:
        return datetime(2026, 10, 15, 10, 15, tzinfo=UTC)


async def _resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def _healthy_site(**extra: RobotsFetchResponse) -> FakeSite:
    responses: dict[str, RobotsFetchResponse] = {
        ROBOTS: RobotsFetchResponse(200, ALLOW_ALL, None),
        SEARCH: RobotsFetchResponse(200, SEARCH_HTML, None),
        DETAIL_1: RobotsFetchResponse(200, DETAIL_HTML, None),
        DETAIL_2: RobotsFetchResponse(200, DETAIL_HTML, None),
    }
    responses.update(extra)
    return FakeSite(responses)


async def _run(
    site: FakeSite,
    tmp_path: Path,
    *,
    cfg: SourceConfig | None = None,
    search_path: str = "/lst/toyota/rav-4",
    detail_limit: int = 2,
    query: str | None = None,
) -> tuple[CaptureOutcome, list[float]]:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    outcome = await capture_samples(
        cfg or sample_source(),
        search_path=search_path,
        detail_limit=detail_limit,
        out_root=tmp_path,
        fetcher=site,
        clock=FrozenClock(),
        sleep=sleep,
        resolver=_resolver,
        query=query,
    )
    return outcome, waits


def _manifest(outcome: CaptureOutcome) -> dict[str, Any]:
    assert outcome.directory is not None
    loaded: dict[str, Any] = json.loads((outcome.directory / "manifest.json").read_text(encoding="utf-8"))
    return loaded


async def test_capture_saves_masked_pages_structure_and_manifest(tmp_path: Path) -> None:
    site = _healthy_site()
    outcome, waits = await _run(site, tmp_path)

    assert site.requests == [ROBOTS, SEARCH, DETAIL_1, DETAIL_2]
    assert outcome.stop_kind is None
    assert outcome.detail_links_matching == 2
    assert outcome.requests == 4
    assert waits == [20.0, 20.0, 20.0], "one wait before each request after the first"

    assert outcome.directory is not None
    saved = (outcome.directory / "search.html").read_bytes()
    assert b"seller@example.org" not in saved
    assert b"333 123 4567" not in saved
    assert b"+393331234567" not in saved
    assert b"[redacted-email]" in saved
    assert b"[redacted-phone]" in saved
    assert b"tel:[redacted]" in saved

    structure = json.loads((outcome.directory / "structure.json").read_text(encoding="utf-8"))
    assert structure["search"]["classes"]["listing"] == 2
    assert structure["search"]["h1"] == 1
    assert structure["search"]["jsonld"] == [{"type": "Car", "keys": ["@type", "offers"]}]
    assert structure["search"]["detail_links_matching"] == 2
    assert "Sample results" not in json.dumps(structure), "structure keeps no text"
    assert "seller@example.org" not in json.dumps(structure)

    manifest = _manifest(outcome)
    assert manifest["robots"]["http_status"] == 200
    assert [step["status"] for step in manifest["steps"]] == [200, 200, 200]
    assert manifest["stop"] is None
    assert manifest["saved_pages_are_masked"] is True
    assert all(step["sha256"] and len(step["sha256"]) == 64 for step in manifest["steps"])


async def test_detail_limit_and_card_only_are_respected(tmp_path: Path) -> None:
    site = _healthy_site()
    outcome, _ = await _run(site, tmp_path / "limited", detail_limit=1)
    assert site.requests == [ROBOTS, SEARCH, DETAIL_1]

    card_site = _healthy_site()
    card_outcome, _ = await _run(
        card_site, tmp_path / "card", cfg=sample_source(detail_mode="card_only", allowed_detail_paths=[])
    )
    assert card_site.requests == [ROBOTS, SEARCH]
    assert any("card_only" in note for note in card_outcome.skipped)
    assert outcome.stop_kind is None


async def test_robots_disallowing_the_search_path_requests_nothing_else(tmp_path: Path) -> None:
    site = FakeSite({ROBOTS: RobotsFetchResponse(200, b"User-agent: *\nDisallow: /lst/\n", None)})
    outcome, _ = await _run(site, tmp_path)

    assert site.requests == [ROBOTS]
    assert outcome.stop_kind == "robots_disallowed"
    assert _manifest(outcome)["stop"]["kind"] == "robots_disallowed", "the run still leaves evidence"


async def test_unreachable_robots_txt_requests_nothing_else(tmp_path: Path) -> None:
    site = FakeSite({ROBOTS: RobotsFetchResponse(503, b"", None)})
    outcome, _ = await _run(site, tmp_path)

    assert site.requests == [ROBOTS]
    assert outcome.stop_kind == "robots_unreachable"


async def test_block_on_search_stops_the_run_before_any_detail(tmp_path: Path) -> None:
    site = _healthy_site(**{SEARCH: RobotsFetchResponse(403, b"forbidden", None)})
    outcome, _ = await _run(site, tmp_path)

    assert site.requests == [ROBOTS, SEARCH]
    assert outcome.stop_kind == "access_blocked"
    assert outcome.steps[-1].status == 403
    assert outcome.directory is not None
    assert not (outcome.directory / "search.html").exists(), "blocked answers are not saved as pages"


async def test_challenge_page_with_200_is_detected_and_stops(tmp_path: Path) -> None:
    challenge = b"<html><head><title>Just a moment...</title></head><body>wait</body></html>"
    site = _healthy_site(**{SEARCH: RobotsFetchResponse(200, challenge, None)})
    outcome, _ = await _run(site, tmp_path)

    assert site.requests == [ROBOTS, SEARCH]
    assert outcome.stop_kind == "access_blocked"
    assert outcome.steps[-1].challenge == "title:just a moment"


async def test_same_host_redirect_to_a_reviewed_path_is_followed(tmp_path: Path) -> None:
    redirected = f"{SEARCH}?sort=price"
    site = _healthy_site(
        **{
            SEARCH: RobotsFetchResponse(308, b"", None, redirected),
            redirected: RobotsFetchResponse(200, SEARCH_HTML, None),
        }
    )
    outcome, _ = await _run(site, tmp_path)

    assert site.requests == [ROBOTS, SEARCH, redirected, DETAIL_1, DETAIL_2]
    assert outcome.stop_kind is None
    assert outcome.directory is not None
    assert (outcome.directory / "search.html").exists(), "the page behind the redirect is the one saved"
    manifest = _manifest(outcome)
    assert [step["label"] for step in manifest["steps"]] == [
        "search",
        "search-redirect-1",
        "detail-1",
        "detail-2",
    ]
    assert manifest["steps"][0]["location"] == SEARCH, "the target is recorded without its query string"
    assert manifest["steps"][0]["file"] is None
    assert manifest["steps"][1]["file"] == "search.html"


async def test_redirect_to_another_host_stops_the_run(tmp_path: Path) -> None:
    site = _healthy_site(
        **{SEARCH: RobotsFetchResponse(301, b"", None, "https://elsewhere.example.net/lst/toyota/rav-4")}
    )
    outcome, _ = await _run(site, tmp_path)

    assert site.requests == [ROBOTS, SEARCH]
    assert outcome.stop_kind == "redirect_refused"
    assert "URL policy" in (outcome.stop_reason or "")


async def test_redirect_to_an_unreviewed_path_stops_the_run(tmp_path: Path) -> None:
    site = _healthy_site(**{SEARCH: RobotsFetchResponse(302, b"", None, f"https://{HOST}/ricerca/avanzata")})
    outcome, _ = await _run(site, tmp_path)

    assert site.requests == [ROBOTS, SEARCH]
    assert outcome.stop_kind == "redirect_refused"


async def test_redirect_loop_stops_after_the_second_hop(tmp_path: Path) -> None:
    site = _healthy_site(
        **{
            SEARCH: RobotsFetchResponse(308, b"", None, f"{SEARCH}?a=1"),
            f"{SEARCH}?a=1": RobotsFetchResponse(308, b"", None, f"{SEARCH}?a=2"),
            f"{SEARCH}?a=2": RobotsFetchResponse(308, b"", None, f"{SEARCH}?a=3"),
        }
    )
    outcome, _ = await _run(site, tmp_path)

    assert site.requests == [ROBOTS, SEARCH, f"{SEARCH}?a=1", f"{SEARCH}?a=2"]
    assert outcome.stop_kind == "redirect_refused"
    assert "more than 2 times" in (outcome.stop_reason or "")


async def test_redirect_target_disallowed_by_robots_is_not_requested(tmp_path: Path) -> None:
    redirected = f"{SEARCH}?sort=price"
    site = _healthy_site(
        **{
            ROBOTS: RobotsFetchResponse(200, b"User-agent: *\nDisallow: /lst/toyota/rav-4?\n", None),
            SEARCH: RobotsFetchResponse(308, b"", None, redirected),
        }
    )
    outcome, _ = await _run(site, tmp_path)

    assert site.requests == [ROBOTS, SEARCH]
    assert outcome.stop_kind == "redirect_refused"
    assert "robots.txt" in (outcome.stop_reason or "")


async def test_redirect_without_a_location_is_not_followed(tmp_path: Path) -> None:
    site = _healthy_site(**{SEARCH: RobotsFetchResponse(301, b"", None, None)})
    outcome, _ = await _run(site, tmp_path)

    assert site.requests == [ROBOTS, SEARCH]
    assert outcome.stop_kind == "redirect_not_followed"


async def test_network_failure_is_reported_as_network_error(tmp_path: Path) -> None:
    site = _healthy_site(**{SEARCH: RobotsFetchResponse(None, None, "network")})
    outcome, _ = await _run(site, tmp_path)

    assert outcome.stop_kind == "network_error"
    assert site.requests == [ROBOTS, SEARCH]


async def test_detail_links_outside_the_reviewed_pattern_are_never_requested(tmp_path: Path) -> None:
    site = _healthy_site()
    outcome, _ = await _run(site, tmp_path, detail_limit=10)

    assert all("other.example.net" not in url for url in site.requests)
    assert all("/angebote/" not in url for url in site.requests)
    assert site.requests == [ROBOTS, SEARCH, DETAIL_1, DETAIL_2]
    assert outcome.detail_links_matching == 2


@pytest.mark.parametrize(
    ("search_path", "reason"),
    [
        ("/ricerca/avanzata", "not in the reviewed"),
        ("/lst/toyota/rav-4?page=2", "query string"),
        ("lst/toyota/rav-4", "must start with /"),
        ("/lst/toyota/rav-4#top", "query string"),
    ],
)
async def test_unreviewed_or_malformed_search_path_is_refused_before_any_request(
    tmp_path: Path, search_path: str, reason: str
) -> None:
    site = _healthy_site()
    with pytest.raises(CaptureRefused, match=reason):
        await _run(site, tmp_path, search_path=search_path)
    assert site.requests == []


async def test_source_without_reviewed_search_path_says_so(tmp_path: Path) -> None:
    site = _healthy_site()
    cfg = sample_source(allowed_search_paths=[])
    with pytest.raises(CaptureRefused, match="no reviewed search path yet"):
        await _run(site, tmp_path, cfg=cfg)
    assert site.requests == []


async def test_detail_limit_outside_bounds_is_refused(tmp_path: Path) -> None:
    site = _healthy_site()
    with pytest.raises(CaptureRefused, match="--detail-limit"):
        await _run(site, tmp_path, detail_limit=11)
    assert site.requests == []


def test_redaction_masks_contacts_and_keeps_prices() -> None:
    body = b"<p>Prezzo 2990 EUR, km 85000</p><a href='tel:+39 02 1234567'>x</a> mail: a.b@c.example.it"
    masked = redact_html(body)
    assert b"2990 EUR" in masked
    assert b"85000" in masked
    assert b"a.b@c.example.it" not in masked
    assert b"02 1234567" not in masked


@pytest.mark.parametrize(
    "escape",
    [b"\\u003e", b"\\u003E", b"\\n", b"\\t", b"\\/", b'\\"', b"\\\\", b"\\u003e\\u003c"],
)
def test_redaction_keeps_a_json_escape_before_an_address_intact(escape: bytes) -> None:
    """Regression: the local part used to swallow the letters of ``\\u003e`` (or ``\\n``) before an
    address in embedded JSON, leaving ``\\[redacted-email]``: an invalid escape that broke the saved
    page's ``__NEXT_DATA__``."""
    document = b'{"description":"<p>Contatti' + escape + b'privacy@sample-dealer.example.it</p>"}'
    masked = redact_html(b"<script id='__NEXT_DATA__'>" + document + b"</script>")
    inner = masked.removeprefix(b"<script id='__NEXT_DATA__'>").removesuffix(b"</script>")
    assert b"privacy@" not in inner and b"sample-dealer" not in inner
    assert escape + b"[redacted-email]" in inner
    parsed = json.loads(inner)  # still valid JSON
    assert parsed["description"].endswith("[redacted-email]</p>")


def test_redaction_of_plain_and_html_addresses_is_unchanged() -> None:
    masked = redact_html(b"<a href='mailto:info@sample.example.it'>info@sample.example.it</a> x@y.example")
    assert masked == (b"<a href='mailto:[redacted-email]'>[redacted-email]</a> [redacted-email]")


def test_challenge_reason_is_specific() -> None:
    assert challenge_reason(b"<title>Access Denied</title>") == "title:access denied"
    assert challenge_reason(b"<html><body>cf_chl_opt = {}</body></html>") == "marker:cf_chl_opt"
    assert challenge_reason(b"<title>Lista annunci</title><p>ok</p>") is None


def test_path_forms_read_escaped_urls_inside_scripts() -> None:
    body = (
        b'<script type="application/json">{"url":"https:\\/\\/www.sample-market.it\\/annunci\\/'
        b'sample-car-3-12345678","@type":"ItemList"}</script>'
    )
    outline, _ = outline_html(body, base_url=SEARCH)
    assert outline["path_shapes"]["same-host /annunci/ 2 segment(s) +id"] == 1
    assert outline["json_types"] == {"ItemList": 1}


def test_jsonld_shape_names_keys_and_url_shapes_but_no_values() -> None:
    body = (
        b'<script type="application/ld+json">{"@graph":[{"@type":"ItemList","url":"https://www.sample-market.it/'
        b'offers/sample-car-4-98765432","itemListElement":[{"@type":"Offer","price":2990,"seller":{"name":"Dealer"}}]}]}'
        b"</script>"
    )
    outline, _ = outline_html(body, base_url=SEARCH)
    shape = outline["jsonld_shape"]
    assert shape["ItemList.url: url: same-host /offers/ 2 segment(s) +id"] == 1
    assert shape["Offer.price: number"] == 1
    assert shape["Offer.seller: object"] == 1
    assert "Dealer" not in json.dumps(outline), "no values are stored"


def test_outline_keeps_names_and_resolves_links_without_text() -> None:
    outline, links = outline_html(SEARCH_HTML, base_url=SEARCH)
    assert outline["tags"]["a"] == 5
    assert outline["classes"]["card"] == 2
    assert "href" in outline["attributes"]
    assert f"{HOST}" in links[0]
    assert links[0] == DETAIL_1
    assert "Sample results" not in json.dumps(outline)
    assert outline["link_shapes"]["same-host /annunci/ 2 segment(s)"] == 2
    assert outline["link_shapes"]["other-host /annunci/ 2 segment(s)"] == 1
    assert outline["link_shapes"]["same-host /angebote/ 2 segment(s)"] == 1
    assert outline["scripts"][0]["type"] == "application/ld+json"
    assert outline["path_shapes"]["relative /annunci/ 2 segment(s)"] == 2
    assert outline["json_types"]["Car"] == 1
    assert outline["scripts"][0]["chars"] > 0
    assert "seller@example.org" not in json.dumps(outline)


JSONLD_ONLY_SEARCH = (
    b"<html><head><title>Results</title>"
    b'<link rel="next" href="/lst/toyota/rav-4?page=2">'
    b'<script type="application/ld+json">{"@type":"SearchResultsPage","mainEntity":{"@type":"ItemList",'
    b'"itemListElement":['
    b'{"@type":"ListItem","url":"/annunci/sample-car-1","item":{"@type":["Car","Product"],'
    b'"mileageFromOdometer":{"@type":"QuantitativeValue","value":187000,"unitCode":"KMT"},'
    b'"offers":{"@type":"Offer","price":2900,"priceCurrency":"EUR","url":"/annunci/sample-car-1"}}},'
    b'{"@type":"ListItem","url":"/annunci/sample-car-2","item":{"@type":["Car","Product"],'
    b'"mileageFromOdometer":{"@type":"QuantitativeValue","value":150000,"unitCode":"KMT"},'
    b'"offers":{"@type":"Offer","price":2650,"priceCurrency":"EUR","url":"/annunci/sample-car-2"}}}'
    b"]}}</script></head><body><h1>Results</h1>"
    b'<a href="/lst/toyota/rav-4?page=2&amp;sort=price">2</a></body></html>'
)


async def test_query_is_sent_and_recorded_and_checked_against_robots(tmp_path: Path) -> None:
    with_query = f"{SEARCH}?priceto=3000&kmto=200000"
    site = _healthy_site(**{with_query: RobotsFetchResponse(200, SEARCH_HTML, None)})
    outcome, _ = await _run(site, tmp_path / "ok", query="priceto=3000&kmto=200000", detail_limit=0)
    assert site.requests == [ROBOTS, with_query]
    assert outcome.stop_kind is None
    assert _manifest(outcome)["search_query"] == "priceto=3000&kmto=200000"

    blocked = _healthy_site(
        **{ROBOTS: RobotsFetchResponse(200, b"User-agent: *\nDisallow: /lst/toyota/rav-4?\n", None)}
    )
    refused, _ = await _run(blocked, tmp_path / "robots", query="priceto=3000")
    assert blocked.requests == [ROBOTS], "robots.txt is evaluated on the path WITH its query"
    assert refused.stop_kind == "robots_disallowed"


@pytest.mark.parametrize("query", ["a b=1", "x=<script>", "=1", "k=v&", "k=" + "9" * 81])
async def test_malformed_query_is_refused_before_any_request(tmp_path: Path, query: str) -> None:
    site = _healthy_site()
    with pytest.raises(CaptureRefused, match="key=value"):
        await _run(site, tmp_path, query=query)
    assert site.requests == []


async def test_detail_links_are_taken_from_json_ld_when_the_page_has_no_anchors(tmp_path: Path) -> None:
    site = _healthy_site(**{SEARCH: RobotsFetchResponse(200, JSONLD_ONLY_SEARCH, None)})
    outcome, _ = await _run(site, tmp_path)

    assert site.requests == [ROBOTS, SEARCH, DETAIL_1, DETAIL_2]
    assert outcome.links_total == 1, "only the pagination anchor is an <a> link"
    assert outcome.jsonld_links_total == 4, "ListItem.url and Offer.url for both cards"
    assert outcome.detail_links_matching == 2
    assert outcome.search_rel_next == 1
    assert outcome.search_query_keys == {"page": 2, "sort": 1}
    assert outcome.search_offer_stats == {
        "offers_with_price": 2,
        "price_min": 2650.0,
        "price_max": 2900.0,
        "currency": "EUR",
        "mileages_km": 2,
        "mileage_km_min": 150000.0,
        "mileage_km_max": 187000.0,
        "models": {},
        "brands": {},
    }
    assert set(outcome.detail_json_types) == {"detail-1", "detail-2"}


def test_offer_stats_ignore_non_numeric_prices_and_other_units() -> None:
    documents = [
        {"@type": "Offer", "price": "2900", "priceCurrency": "EUR"},
        {"@type": "Offer", "price": True, "priceCurrency": "EUR"},
        {"@type": "Car", "mileageFromOdometer": {"value": 90000, "unitCode": "SMI"}},
    ]
    stats = offer_stats(documents)
    assert stats["offers_with_price"] == 0
    assert stats["mileages_km"] == 0
    assert stats["price_min"] is None


def test_offer_stats_count_model_and_brand_names() -> None:
    documents = [
        {"@type": ["Car", "Product"], "model": "RAV 4", "brand": {"@type": "Brand", "name": "Toyota"}},
        {"@type": ["Car", "Product"], "model": "RAV 4", "brand": {"@type": "Brand", "name": "Toyota"}},
        {"@type": ["Car", "Product"], "model": "C-HR", "brand": "Toyota"},
    ]
    stats = offer_stats(documents)
    assert stats["models"] == {"RAV 4": 2, "C-HR": 1}
    assert stats["brands"] == {"Toyota": 3}


async def test_refused_detail_links_are_described_without_their_slug(tmp_path: Path) -> None:
    page = (
        b'<html><head><title>Results</title><script type="application/ld+json">{"@type":"ItemList",'
        b'"itemListElement":[{"@type":"ListItem","url":"/annunci/Sample.Car-1/"},'
        b'{"@type":"ListItem","url":"/annunci/sample-car-2"}]}</script></head><body></body></html>'
    )
    site = _healthy_site(**{SEARCH: RobotsFetchResponse(200, page, None)})
    outcome, _ = await _run(site, tmp_path, detail_limit=0)

    assert outcome.detail_links_matching == 1
    assert outcome.detail_misses == {
        "/annunci/ 2 segment(s) path_not_allowed [digit,dot,hyphen,lower,upper] trailing-slash": 1
    }
    assert "Sample.Car" not in json.dumps(outcome.detail_misses)


# ---------------------------------------------------------------------------------------------
# --via crawl4ai: pages through the crawler's browser, robots.txt still fetched directly
# ---------------------------------------------------------------------------------------------


class FakeCrawler:
    """Stands in for `Crawl4AIClient.fetch_detailed`; answers by URL, records every request."""

    def __init__(self, pages: dict[str, tuple[int | None, bytes, str | None, bool]]) -> None:
        self.pages = pages  # url -> (status, html, final_url, infrastructure_failure)
        self.requests: list[str] = []

    async def fetch_detailed(self, url: str, *, purpose: str, source_key: str) -> CrawlFetchResult:
        self.requests.append(url)
        status, body, final, infra = self.pages.get(url, (404, b"not found", None, False))
        outcome = FetchOutcome(
            requested_url=url,
            fetched_at=datetime(2026, 10, 16, 9, 0, tzinfo=UTC),
            final_url=final,
            http_status=status,
            success=status is not None and 200 <= status < 300,
            access_state=AccessState.OK if status == 200 else AccessState.TRANSIENT_ERROR,
            error_code="crawler_unreachable" if infra else None,
            infrastructure_failure=infra,
        )
        document = RawDocument(
            url=url,
            final_url=final,
            fetched_at=datetime(2026, 10, 16, 9, 0, tzinfo=UTC),
            html=body.decode("utf-8") if status is not None else None,
            fetch=outcome,
        )
        return CrawlFetchResult(document=document)


async def _run_via_crawler(
    crawler: FakeCrawler, robots_site: FakeSite, tmp_path: Path, *, detail_limit: int = 1
) -> CaptureOutcome:
    async def sleep(seconds: float) -> None:
        return None

    async with crawl4ai_page_fetcher(
        crawler,  # type: ignore[arg-type]  # duck-typed fake of Crawl4AIClient
        source_key="sample_market_it",
        robots_fetcher=robots_site,
    ) as fetcher:
        return await capture_samples(
            sample_source(),
            search_path="/lst/toyota/rav-4",
            detail_limit=detail_limit,
            out_root=tmp_path,
            fetcher=fetcher,
            clock=FrozenClock(),
            sleep=sleep,
            resolver=_resolver,
        )


async def test_crawler_route_reads_robots_directly_and_pages_through_the_browser(tmp_path: Path) -> None:
    robots_site = FakeSite({ROBOTS: RobotsFetchResponse(200, ALLOW_ALL, None)})
    crawler = FakeCrawler(
        {SEARCH: (200, SEARCH_HTML, SEARCH, False), DETAIL_1: (200, DETAIL_HTML, DETAIL_1, False)}
    )
    outcome = await _run_via_crawler(crawler, robots_site, tmp_path)
    assert robots_site.requests == [ROBOTS]
    assert crawler.requests == [SEARCH, DETAIL_1]
    assert outcome.stop_kind is None
    assert [step.status for step in outcome.steps] == [200, 200]


async def test_crawler_route_reports_a_final_url_on_another_host_as_a_refused_redirect(
    tmp_path: Path,
) -> None:
    robots_site = FakeSite({ROBOTS: RobotsFetchResponse(200, ALLOW_ALL, None)})
    crawler = FakeCrawler({SEARCH: (200, SEARCH_HTML, "https://consent.other.example/wall", False)})
    outcome = await _run_via_crawler(crawler, robots_site, tmp_path)
    assert crawler.requests == [SEARCH]
    assert outcome.steps[0].status == 307
    assert outcome.stop_kind is not None and outcome.stop_kind.startswith("redirect")


async def test_crawler_route_treats_crawler_failure_as_network_error_not_a_block(tmp_path: Path) -> None:
    robots_site = FakeSite({ROBOTS: RobotsFetchResponse(200, ALLOW_ALL, None)})
    crawler = FakeCrawler({SEARCH: (None, b"", None, True)})
    outcome = await _run_via_crawler(crawler, robots_site, tmp_path)
    assert outcome.stop_kind == "network_error"
    assert outcome.steps[0].error is not None and outcome.steps[0].error.startswith("crawler:")


async def test_crawler_route_detects_a_challenge_page(tmp_path: Path) -> None:
    robots_site = FakeSite({ROBOTS: RobotsFetchResponse(200, ALLOW_ALL, None)})
    challenge = b"<html><head><title>Just a moment...</title></head><body>cf-chl</body></html>"
    crawler = FakeCrawler({SEARCH: (403, challenge, SEARCH, False)})
    outcome = await _run_via_crawler(crawler, robots_site, tmp_path)
    assert outcome.stop_kind == "access_blocked"
    assert crawler.requests == [SEARCH]
