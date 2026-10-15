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

from suv_deals.crawling.robots import RobotsFetchResponse
from suv_deals.crawling.sample_capture import (
    CaptureOutcome,
    CaptureRefused,
    capture_samples,
    challenge_reason,
    outline_html,
    redact_html,
)
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


async def test_redirects_are_not_followed_and_stop_the_run(tmp_path: Path) -> None:
    site = _healthy_site(**{SEARCH: RobotsFetchResponse(301, b"", None, "https://elsewhere.example/")})
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


def test_challenge_reason_is_specific() -> None:
    assert challenge_reason(b"<title>Access Denied</title>") == "title:access denied"
    assert challenge_reason(b"<html><body>cf_chl_opt = {}</body></html>") == "marker:cf_chl_opt"
    assert challenge_reason(b"<title>Lista annunci</title><p>ok</p>") is None


def test_outline_keeps_names_and_resolves_links_without_text() -> None:
    outline, links = outline_html(SEARCH_HTML, base_url=SEARCH)
    assert outline["tags"]["a"] == 5
    assert outline["classes"]["card"] == 2
    assert "href" in outline["attributes"]
    assert f"{HOST}" in links[0]
    assert links[0] == DETAIL_1
    assert "Sample results" not in json.dumps(outline)
