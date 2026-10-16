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
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from tests.adapters.conftest import FIXTURE_SOURCES, FIXTURES, SYNTHETIC_MARKER, fixture_config

from suv_deals.adapters._policy import UrlPolicy
from suv_deals.adapters.dealer_inventory import ADAPTER_VERSION
from suv_deals.adapters.fixture_client import load_manifest
from suv_deals.adapters.registry import load_registry
from suv_deals.domain.enums import SourceMode, TechnicalStatus

REAL_MARKETPLACE_HOSTS = ("mobile.de", "autoscout24", "subito.it", "automobile.it", "pazar3", "reklama5")
REGISTRY = load_registry(Path(__file__).resolve().parents[2] / "config")
#: Synthetic fixture sets of real (configured, non-fixture) marketplace sources.
MARKETPLACE_FIXTURE_SOURCES = ("autoscout24_it", "autoscout24_de")
VOCABULARY = {"schema.org", "purl.org"}  # schema.org/GoodRelations vocabulary identifiers, not fetched
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@([A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+)")
_PHONE_SEP = r"[ \t\u00a0./()\-]{0,3}"
_PHONES = (
    re.compile(r"(?<![\w.:/=-])(?:\+|00)[1-9]\d{0,3}(?:" + _PHONE_SEP + r"\d){6,13}(?!\w)"),
    re.compile(r"(?<![\w.:/+=-])0[1-9]\d(?:" + _PHONE_SEP + r"\d){6,9}(?!\w)"),
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
    assert html_files == set(manifest.files), (
        "every fixture file is described and every described file exists"
    )
    used = {r.file for r in manifest.routes.values() if r.file}
    assert used == html_files, "every fixture file is reachable through a route"
    config = fixture_config(source_key)
    for url in manifest.routes:
        assert urlsplit(url).hostname in config.allowed_hosts


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


@pytest.mark.parametrize("source_key", MARKETPLACE_FIXTURE_SOURCES)
def test_marketplace_manifest_is_complete_and_synthetic(source_key: str) -> None:
    directory = FIXTURES / source_key
    manifest = load_manifest(directory)
    config = REGISTRY.config(source_key)  # a REAL configured source, never a fixture source
    assert config.mode != SourceMode.FIXTURE and config.enabled is False
    assert not (directory / "source.yaml").exists(), "a real source is configured only in config/sources"
    assert (manifest.source_key, manifest.designation) == (source_key, "synthetic")
    assert manifest.created == date(2026, 10, 16)
    assert manifest.parser_version == config.adapter_version
    assert "SYNTHETIC" in manifest.description and "invented" in manifest.description
    html_files = {p.name for p in directory.glob("*.html")}
    assert html_files == set(manifest.files)
    assert {r.file for r in manifest.routes.values() if r.file} == html_files
    policy = UrlPolicy(config)
    for url, route in manifest.routes.items():
        assert urlsplit(url).hostname in config.allowed_hosts, url
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
