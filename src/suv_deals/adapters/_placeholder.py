"""Explicit placeholders for sources whose routes, selectors and ID patterns are NOT verified.

A placeholder registers a candidate source so its terms/technical gates can be
tracked (spec section 5) without guessing any search URL, CSS selector, API
endpoint or listing-ID pattern. Every network or parsing operation raises
`AdapterUnimplemented` with a pointer to the activation checklist. Only URL
canonicalisation is implemented, generically (tracking-parameter stripping on top
of the shared destination guard), because it needs no site knowledge.
"""

from __future__ import annotations

from typing import ClassVar, NoReturn

from suv_deals.adapters._access import classify_document
from suv_deals.adapters._extract import parse_page
from suv_deals.adapters._policy import UrlPolicy, build_identity
from suv_deals.adapters.base import (
    AdapterUnimplemented,
    CanonicalIdentity,
    CrawlClient,
    DiscoveryPage,
    ParsedListing,
    ParseOutcome,
    ParserHealth,
    RawDocument,
    SearchRequest,
    SourceCapabilities,
)
from suv_deals.domain.enums import AccessState, CoverageMode, SourceMode
from suv_deals.domain.profiles import SearchProfile
from suv_deals.domain.sources import SourceConfig
from suv_deals.errors import ValidationFailed

UNIMPLEMENTED = "unimplemented"
ACTIVATION_CHECKLIST = "docs/source_access_register.md#activation-checklist"


class PlaceholderAdapter:
    """Base class: registered candidate source with no verified routes."""

    ADAPTER_KEY: ClassVar[str] = ""
    ADAPTER_VERSION: ClassVar[str] = UNIMPLEMENTED
    SUPPORTED_MODES: ClassVar[frozenset[SourceMode]] = frozenset({SourceMode.PUBLIC_HTML})
    DISPLAY_NAME: ClassVar[str] = ""
    COUNTRY: ClassVar[str | None] = None
    ROLE: ClassVar[str] = "acquisition"
    # Nothing about the provider's capabilities is verified; the values returned by
    # capabilities() are conservative defaults, not facts about the provider.
    CAPABILITIES_VERIFIED: ClassVar[bool] = False

    def __init__(self, config: SourceConfig) -> None:
        if config.adapter != self.ADAPTER_KEY:
            raise ValidationFailed(f"source {config.source_key} is not configured for {self.ADAPTER_KEY}")
        if self.COUNTRY is not None and config.country != self.COUNTRY:
            raise ValidationFailed(f"{self.ADAPTER_KEY} serves {self.COUNTRY}, not {config.country}")
        if config.role != self.ROLE:
            raise ValidationFailed(f"{self.ADAPTER_KEY} serves role {self.ROLE}, not {config.role}")
        self.config = config
        self.source_key: str = config.source_key
        self.adapter_version: str = self.ADAPTER_VERSION
        self.policy = UrlPolicy(config)

    def _unimplemented(self, operation: str) -> NoReturn:
        raise AdapterUnimplemented(
            f"{self.ADAPTER_KEY}.{operation} is not implemented: search routes, selectors and listing-ID "
            f"patterns for {self.DISPLAY_NAME} are not verified. Complete the activation checklist "
            f"({ACTIVATION_CHECKLIST}): terms decision record, permitted route verification, robots check, "
            "saved fixtures and a live low-volume smoke."
        )

    def capabilities(self) -> SourceCapabilities:
        """Conservative defaults; see CAPABILITIES_VERIFIED (always False for placeholders)."""
        return SourceCapabilities(
            coverage_mode=CoverageMode.ROLLING_PAGES,
            provides_listing_ids=False,
            supports_modified_since=False,
            supports_stable_sort=False,
            has_cursor_pagination=False,
            exposes_source_modified_at=False,
            detail_required_for_price=True,
            countries=(self.config.country,),
        )

    def build_search(self, profile: SearchProfile, cursor: str | None) -> SearchRequest:
        self._unimplemented("build_search")

    async def discover(self, request: SearchRequest, client: CrawlClient) -> DiscoveryPage:
        self._unimplemented("discover")

    def canonicalize(self, url: str) -> CanonicalIdentity:
        """Generic: structural URL safety + tracking-parameter stripping; URL-based identity."""
        canonical = self.policy.canonical(url)
        if self.policy.hosts and not self.policy.host_allowed(canonical):
            raise ValidationFailed(f"URL host is not allowed for source {self.source_key}")
        return build_identity(self.source_key, canonical, None)

    async def fetch_detail(self, identity: CanonicalIdentity, client: CrawlClient) -> RawDocument:
        self._unimplemented("fetch_detail")

    def parse_detail(self, document: RawDocument) -> ParsedListing:
        self._unimplemented("parse_detail")

    def detect_access_state(self, document: RawDocument) -> AccessState:
        """Generic status/challenge/login classification only (no site knowledge)."""
        return classify_document(
            document, parse_page(document.html), allowed_hosts=self.policy.hosts, content_present=False
        ).access_state

    def assess_parser_health(self, samples: list[ParseOutcome]) -> ParserHealth:
        return ParserHealth(
            status="insufficient_sample",
            sample_size=len(samples),
            reasons=("adapter is an unimplemented placeholder",),
        )
