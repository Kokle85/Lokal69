"""Shared fixtures for adapter tests. All data is SYNTHETIC (see fixtures/*/MANIFEST.yaml)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from suv_deals.adapters.base import (
    DiscoveryPage,
    FetchOutcome,
    ParsedListing,
    RawDocument,
    SearchRequest,
)
from suv_deals.adapters.dealer_inventory import SchemaOrgDealerAdapter
from suv_deals.adapters.fixture_client import FixtureCrawlClient
from suv_deals.clock import FrozenClock
from suv_deals.domain.enums import AccessState, TermsDecision, TermsStatus
from suv_deals.domain.sources import SourceConfig

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_SOURCES = ("fixture_dealer_de", "fixture_dealer_it", "fixture_dealer_ch")
NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)
SYNTHETIC_MARKER = "<!-- SYNTHETIC FIXTURE: not a real vehicle or offer -->"


def fixture_config(source_key: str) -> SourceConfig:
    data = yaml.safe_load((FIXTURES / source_key / "source.yaml").read_text(encoding="utf-8"))
    return SourceConfig.model_validate(data)


def enabled_fixture_config(source_key: str) -> SourceConfig:
    """In-memory activation of a SYNTHETIC fixture source with a synthetic terms record."""
    return fixture_config(source_key).model_copy(
        update={
            "enabled": True,
            "terms_status": TermsStatus.PERMITTED,
            "terms_decision": TermsDecision.PROCEED_PERMITTED,
            "terms_decision_actor": "synthetic-fixture",
            "terms_decision_note": "synthetic fixture source on a reserved example host; no real provider",
        }
    )


def raw_document(
    url: str,
    html: str | None,
    *,
    status: int | None = 200,
    final_url: str | None = None,
    success: bool | None = None,
    access_state: AccessState | None = None,
) -> RawDocument:
    ok = (status is not None and 200 <= status < 300) if success is None else success
    return RawDocument(
        url=url,
        final_url=final_url or url,
        fetched_at=NOW,
        html=html,
        fetch=FetchOutcome(
            requested_url=url,
            final_url=final_url or url,
            http_status=status,
            success=ok,
            access_state=access_state or (AccessState.OK if ok else AccessState.TRANSIENT_ERROR),
            fetched_at=NOW,
        ),
    )


def car_page(
    json_ld: str, body: str = "<p>Synthetic page body with enough visible text for tests.</p>"
) -> str:
    return (
        f"{SYNTHETIC_MARKER}\n<!doctype html><html lang='de'><head><title>Synthetic</title>"
        f"<script type='application/ld+json'>{json_ld}</script></head><body><main>{body}</main></body></html>"
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def client(clock: FrozenClock) -> FixtureCrawlClient:
    return FixtureCrawlClient([FIXTURES / key for key in FIXTURE_SOURCES], clock=clock)


@pytest.fixture
def de_adapter(clock: FrozenClock) -> SchemaOrgDealerAdapter:
    return SchemaOrgDealerAdapter(fixture_config("fixture_dealer_de"), clock=clock)


@pytest.fixture
def it_adapter(clock: FrozenClock) -> SchemaOrgDealerAdapter:
    return SchemaOrgDealerAdapter(fixture_config("fixture_dealer_it"), clock=clock)


@pytest.fixture
def ch_adapter(clock: FrozenClock) -> SchemaOrgDealerAdapter:
    return SchemaOrgDealerAdapter(fixture_config("fixture_dealer_ch"), clock=clock)


DetailFn = Callable[[SchemaOrgDealerAdapter, str], Awaitable[ParsedListing]]
SearchFn = Callable[..., Awaitable[DiscoveryPage]]


@pytest.fixture
def detail(client: FixtureCrawlClient) -> DetailFn:
    async def _detail(adapter: SchemaOrgDealerAdapter, url: str) -> ParsedListing:
        document = await adapter.fetch_detail(adapter.canonicalize(url), client)
        return adapter.parse_detail(document)

    return _detail


@pytest.fixture
def search(client: FixtureCrawlClient) -> SearchFn:
    async def _search(adapter: SchemaOrgDealerAdapter, url: str, page_number: int = 1) -> DiscoveryPage:
        request = SearchRequest(
            source_key=adapter.source_key, profile_key="primary", url=url, page_number=page_number
        )
        return await adapter.discover(request, client)

    return _search
