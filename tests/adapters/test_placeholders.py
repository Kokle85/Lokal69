"""Placeholder adapters for unverified marketplaces and the gated mobile.de API skeleton."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from tests.adapters.conftest import raw_document

from suv_deals.adapters import (
    autoscout_public,
    it_marketplaces,
    mk_comparables,
    mobile_de_api,
    mobile_de_public,
)
from suv_deals.adapters._placeholder import PlaceholderAdapter
from suv_deals.adapters.base import AdapterUnimplemented, SearchRequest, SourceAdapter
from suv_deals.adapters.fixture_client import FixtureCrawlClient
from suv_deals.adapters.mobile_de_api import MobileDeApiAccess, MobileDeSearchApiAdapter
from suv_deals.adapters.registry import ADAPTERS, load_registry
from suv_deals.domain.enums import AccessState, ProfileKey
from suv_deals.domain.profiles import load_business_config
from suv_deals.errors import DependencyUnavailable, ValidationFailed

REPO = Path(__file__).resolve().parents[2]
PLACEHOLDER_SOURCES = (
    "mobile_de_public",
    "autoscout24_de",
    "autoscout24_it",
    "autoscout24_ch",
    "subito_it",
    "automobile_it",
    "pazar3_mk",
    "reklama5_mk",
)
DOCUMENTED_URLS = {
    "https://www.mobile.de/service/agbPublic",
    "https://www.autoscout24.com/company/agb/",
    "https://services.mobile.de/docs/search-api.html",
}


def _adapter(source_key: str) -> SourceAdapter:
    return load_registry(REPO / "config").adapter(source_key)


@pytest.mark.parametrize("source_key", PLACEHOLDER_SOURCES)
async def test_placeholders_raise_with_checklist_pointer(source_key: str, client: FixtureCrawlClient) -> None:
    adapter = _adapter(source_key)
    assert isinstance(adapter, PlaceholderAdapter)
    assert isinstance(adapter, SourceAdapter)
    assert adapter.adapter_version == "unimplemented"
    assert adapter.CAPABILITIES_VERIFIED is False
    profile = load_business_config(REPO / "config").profiles[ProfileKey.PRIMARY]
    identity = adapter.canonicalize("https://placeholder.example/x?utm_source=a&id=7#f")
    assert identity.canonical_url == "https://placeholder.example/x?id=7"
    assert identity.identity_method == "canonical_url"
    with pytest.raises(AdapterUnimplemented, match="activation checklist"):
        adapter.build_search(profile, None)
    with pytest.raises(AdapterUnimplemented, match="activation checklist"):
        adapter.parse_detail(raw_document("https://placeholder.example/x", "<html></html>"))
    with pytest.raises(AdapterUnimplemented, match="not verified"):
        await adapter.discover(
            SearchRequest(source_key=source_key, profile_key="primary", url="https://placeholder.example/s"),
            client,
        )
    with pytest.raises(AdapterUnimplemented):
        await adapter.fetch_detail(identity, client)
    assert client.requests == []  # nothing was fetched
    health = adapter.assess_parser_health([])
    assert health.status == "insufficient_sample"
    assert adapter.detect_access_state(raw_document("https://placeholder.example/x", None, status=403)) == (
        AccessState.ACCESS_BLOCKED
    )


@pytest.mark.parametrize(
    "module", [mobile_de_public, autoscout_public, it_marketplaces, mk_comparables, mobile_de_api]
)
def test_no_guessed_urls_or_selectors_in_placeholder_modules(module: object) -> None:
    source = inspect.getsource(module)  # type: ignore[arg-type]
    urls = {u.rstrip(".,;:") for u in re.findall(r"https?://[^\s)\"']+", source)}
    assert urls <= DOCUMENTED_URLS, urls - DOCUMENTED_URLS
    for selector_like in ("querySelector", "xpath", "css=", "div.", ".listing", "/api/", "/search?"):
        assert selector_like not in source


def test_each_autoscout_country_is_registered_separately() -> None:
    keys = {cls.ADAPTER_KEY: cls.COUNTRY for cls in autoscout_public.AUTOSCOUT24_ADAPTERS}
    assert keys == {
        "autoscout24_public_de": "DE",
        "autoscout24_public_it": "IT",
        "autoscout24_public_ch": "CH",
    }
    for key in keys:
        assert key in ADAPTERS


def test_country_and_role_are_enforced() -> None:
    registry = load_registry(REPO / "config")
    de_cfg = registry.config("autoscout24_de")
    with pytest.raises(ValidationFailed):
        autoscout_public.AutoScout24ItPublicAdapter(
            de_cfg.model_copy(update={"adapter": "autoscout24_public_it"})
        )
    pazar = registry.config("pazar3_mk")
    with pytest.raises(ValidationFailed):
        mk_comparables.Pazar3Adapter(pazar.model_copy(update={"role": "acquisition"}))


async def test_mobile_de_api_refuses_without_entitlement(client: FixtureCrawlClient) -> None:
    cfg = load_registry(REPO / "config").config("mobile_de_api")
    adapter = MobileDeSearchApiAdapter(cfg)
    profile = load_business_config(REPO / "config").profiles[ProfileKey.PRIMARY]
    with pytest.raises(DependencyUnavailable, match=r"^mobile\.de Search API entitlement not verified$"):
        adapter.build_search(profile, None)
    with pytest.raises(DependencyUnavailable):
        adapter.parse_detail(raw_document("https://placeholder.example/x", None))
    flagged_only = MobileDeSearchApiAdapter(cfg, feature_enabled=True)
    with pytest.raises(DependencyUnavailable):
        flagged_only.build_search(profile, None)
    no_reference = MobileDeSearchApiAdapter(
        cfg,
        feature_enabled=True,
        access=MobileDeApiAccess(credentials_configured=True, entitlement_verified=True),
    )
    with pytest.raises(DependencyUnavailable):
        no_reference.build_search(profile, None)
    complete = MobileDeSearchApiAdapter(
        cfg,
        feature_enabled=True,
        access=MobileDeApiAccess(
            credentials_configured=True,
            entitlement_verified=True,
            entitlement_reference="synthetic-test-only",
        ),
    )
    assert complete.access_ready
    with pytest.raises(AdapterUnimplemented, match=r"services\.mobile\.de/docs/search-api\.html"):
        complete.build_search(profile, None)
    assert client.requests == []
