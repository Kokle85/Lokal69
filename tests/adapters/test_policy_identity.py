"""URL canonicalisation, host/path policy and identity hashing (spec sections 10 and 24)."""

from __future__ import annotations

import hashlib

import pytest
from tests.adapters.conftest import fixture_config

from suv_deals.adapters._policy import UrlPolicy, build_identity, canonicalize_url, clean_provider_id
from suv_deals.adapters.dealer_inventory import SchemaOrgDealerAdapter
from suv_deals.errors import ValidationFailed

TRACKING = ("utm_source", "gclid", "fbclid", "ref", "referrer")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://Dealer.Example/fahrzeug/TEST-204#galerie", "https://dealer.example/fahrzeug/TEST-204"),
        ("https://dealer.example:443/fahrzeug/TEST-204", "https://dealer.example/fahrzeug/TEST-204"),
        ("http://dealer.example:80/x", "http://dealer.example/x"),
        ("https://dealer.example", "https://dealer.example/"),
        (
            "https://dealer.example/f?id=5&utm_source=nl&b=2&utm_campaign=x&gclid=1&fbclid=2&ref=home",
            "https://dealer.example/f?id=5&b=2",
        ),
        ("https://dealer.example/f?b=2&a=1", "https://dealer.example/f?b=2&a=1"),  # order kept
        ("https://dealer.example/f?utm%5Fmedium=x&q=a%20b", "https://dealer.example/f?q=a%20b"),
        ("https://dealer.example/f?flag&utm_term=", "https://dealer.example/f?flag"),
        ("https://dealer.example/Fahrzeug/ABC", "https://dealer.example/Fahrzeug/ABC"),  # path case kept
    ],
)
def test_canonicalize_url(url: str, expected: str) -> None:
    assert canonicalize_url(url, tracking_params=TRACKING) == expected


def test_relative_urls_resolve_against_base() -> None:
    assert (
        canonicalize_url(
            "../fahrzeug/X?utm_source=a", tracking_params=TRACKING, base="https://dealer.example/a/b"
        )
        == "https://dealer.example/fahrzeug/X"
    )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,<b>x</b>",
        "https://user:pass@dealer.example/x",
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/x",
        "http://2130706433/x",
        "https://dealer.example:8443/x",
        "https://localhost/x",
        "",
    ],
)
def test_unsafe_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValidationFailed):
        canonicalize_url(url, tracking_params=TRACKING)


def test_identity_material_and_hash() -> None:
    by_id = build_identity("fixture_dealer_de", "https://dealer.example/fahrzeug/TEST-204", "TEST-204")
    assert by_id.identity_method == "provider_id"
    assert by_id.source_listing_id == "TEST-204"
    assert by_id.identity_material == "fixture_dealer_de|provider_id|TEST-204"
    assert by_id.identity_hash == hashlib.sha256(by_id.identity_material.encode()).hexdigest()

    by_url = build_identity("fixture_dealer_de", "https://dealer.example/fahrzeug/TEST-204", None)
    assert by_url.identity_method == "canonical_url"
    assert by_url.source_listing_id == "https://dealer.example/fahrzeug/TEST-204"
    assert by_url.identity_hash != by_id.identity_hash

    other_source = build_identity("fixture_dealer_it", "https://dealer.example/fahrzeug/TEST-204", "TEST-204")
    assert other_source.identity_hash != by_id.identity_hash  # identities are source-scoped


def test_long_url_identity_uses_hashed_listing_id() -> None:
    url = "https://dealer.example/fahrzeug/" + "a" * 300
    identity = build_identity("fixture_dealer_de", url, None)
    assert identity.source_listing_id.startswith("url-sha256:")
    assert len(identity.source_listing_id) <= 200
    assert identity.canonical_url == url
    with pytest.raises(ValidationFailed):
        build_identity("fixture_dealer_de", "https://dealer.example/" + "b" * 2100, None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TEST-204", "TEST-204"),
        ("  A  B ", "A B"),
        (1234, "1234"),
        (True, None),
        ("", None),
        ("x\x00y", None),
        ("z" * 201, None),
        ({"a": 1}, None),
    ],
)
def test_clean_provider_id(raw: object, expected: str | None) -> None:
    assert clean_provider_id(raw) == expected


def test_url_policy_is_anchored_and_host_exact() -> None:
    policy = UrlPolicy(fixture_config("fixture_dealer_de"))
    assert policy.is_search_url("https://dealer.example/suche?typ=suv")
    assert not policy.is_search_url("https://dealer.example/suche/extra")
    assert not policy.is_search_url("https://dealer.example/x/suche")
    assert policy.is_detail_url("https://dealer.example/fahrzeug/TEST-204")
    assert not policy.is_detail_url("https://dealer.example/fahrzeug/test-204")  # pattern is upper-case only
    assert not policy.is_detail_url("https://dealer.example.attacker.example/fahrzeug/TEST-204")
    assert not policy.is_detail_url("https://sub.dealer.example/fahrzeug/TEST-204")
    assert not policy.host_allowed("javascript:alert(1)")


def test_invalid_policy_regex_is_rejected() -> None:
    cfg = fixture_config("fixture_dealer_de").model_copy(update={"allowed_detail_paths": ("/fahrzeug/[",)})
    with pytest.raises(ValidationFailed):
        UrlPolicy(cfg)


def test_adapter_canonicalize_collapses_tracking_variants(de_adapter: SchemaOrgDealerAdapter) -> None:
    variants = [
        "https://dealer.example/fahrzeug/TEST-204",
        "https://DEALER.example/fahrzeug/TEST-204?utm_source=x#top",
        "https://dealer.example:443/fahrzeug/TEST-204?gclid=abc&ref=home",
    ]
    hashes = {de_adapter.canonicalize(v).identity_hash for v in variants}
    assert len(hashes) == 1
    with pytest.raises(ValidationFailed):
        de_adapter.canonicalize("https://tracker.example/fahrzeug/TEST-204")


def test_adapter_identity_prefers_provider_id(de_adapter: SchemaOrgDealerAdapter) -> None:
    identity = de_adapter.identity_for("https://dealer.example/fahrzeug/TEST-204?utm_source=x", "TEST-204")
    assert identity.identity_method == "provider_id"
    assert identity.canonical_url == "https://dealer.example/fahrzeug/TEST-204"
    assert de_adapter.identity_for("https://dealer.example/fahrzeug/TEST-204", None).identity_method == (
        "canonical_url"
    )


def test_configured_detail_id_regex(
    it_adapter: SchemaOrgDealerAdapter, ch_adapter: SchemaOrgDealerAdapter
) -> None:
    it = it_adapter.canonicalize("https://concessionario.example/usato/auto/example-trail-it301?utm_source=p")
    assert (it.identity_method, it.source_listing_id) == ("provider_id", "it301")
    ch = ch_adapter.canonicalize("https://garage.example/occasion/40101")
    assert (ch.identity_method, ch.source_listing_id) == ("provider_id", "40101")


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"search": {"search_url": "https://dealer.example/suche", "bogus": "x"}}, "unsupported search keys"),
        ({"search": {"detail_id_regex": "/fahrzeug/(.*)"}}, "named group"),
        ({"search": {"detail_id_regex": "(?P<id>["}}, "invalid detail_id_regex"),
        ({"source_timezone": "Mars/Olympus"}, "source_timezone"),
        ({"adapter": "mobile_de_public"}, "not configured"),
    ],
)
def test_adapter_rejects_bad_configuration(update: dict[str, object], message: str) -> None:
    cfg = fixture_config("fixture_dealer_de").model_copy(update=update)
    with pytest.raises(ValidationFailed, match=message):
        SchemaOrgDealerAdapter(cfg)
