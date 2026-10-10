"""Every saved fixture is labelled, described and synthetic (spec section 31 fixture rules)."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from tests.adapters.conftest import FIXTURE_SOURCES, FIXTURES, SYNTHETIC_MARKER, fixture_config

from suv_deals.adapters.dealer_inventory import ADAPTER_VERSION
from suv_deals.adapters.fixture_client import load_manifest
from suv_deals.domain.enums import SourceMode, TechnicalStatus

REAL_MARKETPLACE_HOSTS = ("mobile.de", "autoscout24", "subito.it", "automobile.it", "pazar3", "reklama5")

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
    assert html_files == set(manifest.files), (
        "every fixture file is described and every described file exists"
    )
    used = {r.file for r in manifest.routes.values() if r.file}
    assert used == html_files, "every fixture file is reachable through a route"
    config = fixture_config(source_key)
    for url in manifest.routes:
        assert urlsplit(url).hostname in config.allowed_hosts


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*/*.html")), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_fixture_file_is_marked_synthetic(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.startswith(SYNTHETIC_MARKER)
    hosts = {h.rstrip(".") for h in re.findall(r"https?://([a-z0-9.-]+)", text.lower())}
    vocabulary = {"schema.org", "purl.org"}  # schema.org/GoodRelations vocabulary identifiers, not fetched
    assert all(h.endswith(".example") or h in vocabulary for h in hosts), hosts
    assert not any(real in text.lower() for real in REAL_MARKETPLACE_HOSTS)


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
