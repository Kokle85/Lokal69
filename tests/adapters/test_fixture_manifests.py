"""Every saved fixture is labelled, described and synthetic (spec section 31 fixture rules).

Two kinds of fixture directory exist. The SYNTHETIC dealer sources (``fixture_dealer_*``) carry
their own ``source.yaml`` and use only reserved ``.example`` hosts. A marketplace directory
(``designation: synthetic`` under the ``source_key`` of a real configured source, e.g.
``autoscout24_it``) has no ``source.yaml``: its pages and routes may name ONLY that source's own
configured host(s), so the real adapter and URL policy run unchanged; every other rule stays as
strict (marker, no other real host, e-mail addresses only on ``.example``, no phone numbers).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from tests.adapters.conftest import FIXTURE_SOURCES, FIXTURES, SYNTHETIC_MARKER, fixture_config

from suv_deals.adapters._policy import UrlPolicy
from suv_deals.adapters.dealer_inventory import ADAPTER_VERSION
from suv_deals.adapters.fixture_client import load_manifest
from suv_deals.adapters.registry import load_registry
from suv_deals.crawling.robots import parse_robots_txt
from suv_deals.crawling.url_policy import ROBOTS_PATH
from suv_deals.domain.enums import SourceMode, TechnicalStatus

REAL_MARKETPLACE_HOSTS = ("mobile.de", "autoscout24", "subito.it", "automobile.it", "pazar3", "reklama5")
REGISTRY = load_registry(Path(__file__).resolve().parents[2] / "config")
#: Synthetic fixture sets of real (configured, non-fixture) marketplace sources.
MARKETPLACE_FIXTURE_SOURCES = ("autoscout24_it", "autoscout24_de")
#: The enable switch of each real source in config/sources (step 7 done for autoscout24_it only).
MARKETPLACE_ENABLED = {"autoscout24_it": True, "autoscout24_de": False}
VOCABULARY = {"schema.org", "purl.org"}  # schema.org/GoodRelations vocabulary identifiers, not fetched
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@([A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+)")
_PHONE_SEP = r"[ \t\u00a0./()\-]{0,3}"
_PHONES = (
    re.compile(r"(?<![\w.:/=-])(?:\+|00)[1-9]\d{0,3}(?:" + _PHONE_SEP + r"\d){6,13}(?!\w)"),
    # National numbers: a separator may follow the area code's second digit (Rome "06 12345678",
    # Milan "02 1234 5678"), so the area code is never assumed to be three digits long.
    re.compile(r"(?<![\w.:/+=-])0[1-9](?:" + _PHONE_SEP + r"\d){6,10}(?!\w)"),
    re.compile(r"(?<![\w.:/+=-])3\d{2}(?:" + _PHONE_SEP + r"\d){7}(?!\w)"),
    re.compile(r"\btel:", re.IGNORECASE),
)

# Spec section 31: required fixture coverage per activated adapter (DE fixture source).
REQUIRED_DE_COVERAGE = {
    "normal search page": "search_page1.html",
    "paginated search": "search_page2.html",
    "empty result": "search_empty.html",
    "normal detail": "detail_normal.html",
    "missing-price detail": "detail_price_on_request.html",
    "contradictory mileage": "detail_mileage_conflict.html",
    "net/gross wording": "detail_net_price.html",
    "margin scheme wording": "detail_margin_scheme.html",
    "removed listing": "detail_removed.html",
    "login wall": "login_wall.html",
    "CAPTCHA/block page": "captcha.html",
    "malformed/changed markup": "detail_malformed.html",
    "prompt injection and XSS": "detail_injection.html",
}


@pytest.mark.parametrize("source_key", FIXTURE_SOURCES)
def test_manifest_is_complete_and_synthetic(source_key: str) -> None:
    directory = FIXTURES / source_key
    manifest = load_manifest(directory)
    assert manifest.source_key == source_key
    assert manifest.designation == "synthetic"
    assert manifest.created == date(2026, 10, 6)
    assert manifest.parser_version == ADAPTER_VERSION
    html_files = {p.name for p in directory.glob("*.html")}
    assert html_files | {ROBOTS_FILE} == set(manifest.files), (
        "every fixture file is described and every described file exists"
    )
    used = {r.file for r in manifest.routes.values() if r.file}
    assert used == html_files | {ROBOTS_FILE}, "every fixture file is reachable through a route"
    config = fixture_config(source_key)
    for url in manifest.routes:
        assert urlsplit(url).hostname in config.allowed_hosts
    _check_robots_route(directory, config.allowed_hosts)


def test_every_fixture_directory_is_a_known_kind() -> None:
    directories = {p.parent.name for p in FIXTURES.glob("*/MANIFEST.yaml")}
    assert directories == set(FIXTURE_SOURCES) | set(MARKETPLACE_FIXTURE_SOURCES)


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*/*.html")), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_fixture_file_is_marked_synthetic(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.startswith(SYNTHETIC_MARKER)
    if path.parent.name in MARKETPLACE_FIXTURE_SOURCES:
        _check_marketplace_page(path.parent.name, text)
        return
    hosts = {h.rstrip(".") for h in re.findall(r"https?://([a-z0-9.-]+)", text.lower())}
    assert all(h.endswith(".example") or h in VOCABULARY for h in hosts), hosts
    assert not any(real in text.lower() for real in REAL_MARKETPLACE_HOSTS)


def _check_marketplace_page(source_key: str, text: str) -> None:
    """A marketplace fixture page may name only its own source's configured host(s)."""
    own = {h.lower() for h in REGISTRY.config(source_key).allowed_hosts}
    lowered = text.lower()
    hosts = {h.rstrip(".") for h in re.findall(r"https?://([a-z0-9.-]+)", lowered)}
    assert all(h in own or h.endswith(".example") or h in VOCABULARY for h in hosts), hosts
    without_own = lowered
    for host in own:
        without_own = without_own.replace(host, "")
    assert not any(real in without_own for real in REAL_MARKETPLACE_HOSTS)
    for match in _EMAIL.finditer(text):
        assert match.group(1).lower().endswith(".example"), match.group(0)
    for pattern in _PHONES:
        assert not pattern.search(text), pattern.pattern


ROBOTS_FILE = "robots.txt"
ROBOTS_MARKER = "# SYNTHETIC FIXTURE:"


def _check_robots_route(directory: Path, hosts: Sequence[str]) -> None:
    """Every fixture set serves a SYNTHETIC robots.txt (the worker's robots guard reads it first):
    one route per host at exactly /robots.txt, plain text, marked synthetic, naming no real host."""
    manifest = load_manifest(directory)
    robots = {url: r for url, r in manifest.routes.items() if r.file == ROBOTS_FILE}
    assert {urlsplit(url).hostname for url in robots} == set(hosts)
    for url, route in robots.items():
        assert urlsplit(url).path == ROBOTS_PATH and not urlsplit(url).query, url
        assert route.content_type.startswith("text/plain") and route.status == 200
    text = (directory / ROBOTS_FILE).read_text(encoding="utf-8")
    assert text.startswith(ROBOTS_MARKER)
    assert parse_robots_txt(text.encode("utf-8")).recognized_lines > 0
    lowered = text.lower()
    found = {h.rstrip(".") for h in re.findall(r"https?://([a-z0-9.-]+)", lowered)}
    assert found <= {h.lower() for h in hosts}, found


@pytest.mark.parametrize(
    "phone",
    [
        "06 12345678",  # Rome: two-digit area code, then a space
        "02 1234 5678",  # Milan
        "06/1234567",
        "02.12345678",
        "02-12345678",
        "Tel. 06 4881 2345",
        "011 1234567",
        "030 1234567",
        "089 12345678",
        "0612345678",
        "+39 06 12345678",
        "+49 30 1234567",
        "333 1234567",  # Italian mobile
        "tel:+390612345678",
    ],
)
def test_the_marketplace_page_check_finds_phone_numbers(phone: str) -> None:
    page = (FIXTURES / "autoscout24_it" / "detail_normal.html").read_text(encoding="utf-8")
    _check_marketplace_page("autoscout24_it", page)  # the saved page itself carries none
    with pytest.raises(AssertionError):
        _check_marketplace_page("autoscout24_it", page.replace("</main>", f"<p>{phone}</p></main>"))


@pytest.mark.parametrize(
    "value", ["2026-01-01T00:00:00Z", "09/2018", "03-2017", "00000000-0000-4000-8000-000000000101"]
)
def test_dates_and_synthetic_ids_are_no_phone_numbers(value: str) -> None:
    page = (FIXTURES / "autoscout24_it" / "detail_normal.html").read_text(encoding="utf-8")
    _check_marketplace_page("autoscout24_it", page.replace("</main>", f"<p>{value}</p></main>"))


@pytest.mark.parametrize("source_key", MARKETPLACE_FIXTURE_SOURCES)
def test_marketplace_manifest_is_complete_and_synthetic(source_key: str) -> None:
    directory = FIXTURES / source_key
    manifest = load_manifest(directory)
    config = REGISTRY.config(source_key)  # a REAL configured source, never a fixture source
    assert config.mode != SourceMode.FIXTURE
    # autoscout24_it was enabled by the owner on 2026-10-18 (activation step 7); autoscout24_de stays off.
    assert config.enabled is MARKETPLACE_ENABLED[source_key]
    assert not (directory / "source.yaml").exists(), "a real source is configured only in config/sources"
    assert (manifest.source_key, manifest.designation) == (source_key, "synthetic")
    assert manifest.created == date(2026, 10, 16)
    assert manifest.parser_version == config.adapter_version
    assert "SYNTHETIC" in manifest.description and "invented" in manifest.description
    html_files = {p.name for p in directory.glob("*.html")}
    assert html_files | {ROBOTS_FILE} == set(manifest.files)
    assert {r.file for r in manifest.routes.values() if r.file} == html_files | {ROBOTS_FILE}
    _check_robots_route(directory, config.allowed_hosts)
    policy = UrlPolicy(config)
    for url, route in manifest.routes.items():
        assert urlsplit(url).hostname in config.allowed_hosts, url
        if urlsplit(url).path == ROBOTS_PATH:
            continue
        assert policy.is_search_url(url) or policy.is_detail_url(url), url
        if route.final_url is not None:
            final_host = urlsplit(route.final_url).hostname or ""
            assert final_host in config.allowed_hosts or final_host.endswith(".example"), route.final_url


@pytest.mark.parametrize("source_key", FIXTURE_SOURCES)
def test_fixture_source_configs(source_key: str) -> None:
    config = fixture_config(source_key)
    assert config.enabled is False
    assert config.mode == SourceMode.FIXTURE
    assert config.technical_status == TechnicalStatus.FIXTURE_TESTED
    assert config.adapter_version == ADAPTER_VERSION
    assert all(h.endswith(".example") for h in config.allowed_hosts)


def test_required_de_fixture_coverage() -> None:
    manifest = load_manifest(FIXTURES / "fixture_dealer_de")
    for category, filename in REQUIRED_DE_COVERAGE.items():
        assert filename in manifest.files, category
        assert any(r.file == filename for r in manifest.routes.values()), category
