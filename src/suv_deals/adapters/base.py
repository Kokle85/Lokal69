"""Source adapter contract (spec section 8).

Adapters translate one registered source into application-owned models.
No domain logic may depend on crawler response fields directly; the crawl
client wraps everything into `RawDocument`/`FetchOutcome`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import AccessState, Completeness, CoverageMode
from suv_deals.domain.listings import NormalizedListing
from suv_deals.domain.profiles import SearchProfile

_FROZEN = ConfigDict(frozen=True, extra="forbid")

FetchPurpose = Literal["search", "detail", "robots", "diagnostic"]
PageType = Literal["detail", "search", "removed", "challenge", "login", "paywall", "empty_shell", "unknown"]


class SourceCapabilities(BaseModel):
    model_config = _FROZEN

    coverage_mode: CoverageMode
    provides_listing_ids: bool
    supports_modified_since: bool = False
    supports_stable_sort: bool = False
    has_cursor_pagination: bool = False
    exposes_source_modified_at: bool = False
    detail_required_for_price: bool = False
    max_page_size: int | None = None
    countries: tuple[str, ...] = ()


class SearchRequest(BaseModel):
    model_config = _FROZEN

    source_key: str
    profile_key: str
    partition_key: str = "default"
    url: str
    page_number: int = Field(default=1, ge=1)
    cursor: str | None = None
    params: dict[str, str] = Field(default_factory=dict)
    modified_since: datetime | None = None


class FetchOutcome(BaseModel):
    """Redacted record of one fetch attempt (stored in ops.fetch_attempts)."""

    model_config = _FROZEN

    requested_url: str
    final_url: str | None = None
    http_status: int | None = None
    success: bool
    access_state: AccessState
    error_code: str | None = Field(default=None, max_length=80)
    error_message: str | None = Field(default=None, max_length=500)
    elapsed_ms: int | None = Field(default=None, ge=0)
    extraction_ms: int | None = Field(default=None, ge=0)
    bytes: int = Field(default=0, ge=0)
    redirect_count: int = Field(default=0, ge=0)
    retry_after_seconds: int | None = Field(default=None, ge=0)
    # Allow-listed response headers only (content-type, retry-after, last-modified, etag, x-robots-tag).
    response_headers: dict[str, str] = Field(default_factory=dict)
    crawler_version: str | None = None
    fetched_at: datetime

    @field_validator("fetched_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class RawDocument(BaseModel):
    model_config = _FROZEN

    url: str
    final_url: str | None
    fetched_at: datetime
    content_type: str | None = None
    html: str | None = None
    text: str | None = None
    raw_content_hash: str | None = None  # sha256 of retained bytes
    snapshot_id: UUID | None = None
    fetch: FetchOutcome

    @field_validator("fetched_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class SearchObservation(BaseModel):
    """One search-result card. `card_hash` is computed from `card_hash_material` only."""

    model_config = _FROZEN

    source_listing_id: str | None = Field(default=None, max_length=200)
    canonical_url: str = Field(max_length=2048)
    title: str | None = Field(default=None, max_length=300)
    card_price_raw: str | None = Field(default=None, max_length=200)
    card_price_minor: int | None = Field(default=None, ge=0)
    card_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    card_mileage_raw: str | None = Field(default=None, max_length=200)
    card_mileage_km: Decimal | None = Field(default=None, ge=0)
    source_modified_at: datetime | None = None
    position: int = Field(ge=0)
    card_hash: str = Field(min_length=64, max_length=64)
    card_hash_material: dict[str, str | None]


class DiscoveryPage(BaseModel):
    """Result of one search page. Empty observations always carry completeness/access classification."""

    model_config = _FROZEN

    request: SearchRequest
    observations: tuple[SearchObservation, ...]
    next_cursor: str | None = None
    next_url: str | None = None
    has_more: bool
    completeness: Completeness
    access_state: AccessState
    access_evidence: str | None = Field(default=None, max_length=500)
    result_count_reported: int | None = Field(default=None, ge=0)
    watermark_observed: datetime | None = None
    fetched_at: datetime
    fetch: FetchOutcome
    page_type: PageType = "search"

    @model_validator(mode="after")
    def _classified(self) -> DiscoveryPage:
        if self.has_more and not (self.next_cursor or self.next_url):
            raise ValueError("has_more requires next_cursor or next_url")
        if self.access_state != AccessState.OK and self.observations:
            raise ValueError("a blocked/failed page cannot carry observations")
        if self.access_state != AccessState.OK and self.completeness == Completeness.COMPLETE:
            raise ValueError("a non-OK page cannot be complete")
        return self


class CanonicalIdentity(BaseModel):
    model_config = _FROZEN

    source_key: str
    source_listing_id: str = Field(min_length=1, max_length=200)
    canonical_url: str = Field(max_length=2048)
    identity_method: Literal["provider_id", "canonical_url"]
    identity_material: str = Field(max_length=2048)
    identity_hash: str = Field(min_length=64, max_length=64)


class ParsedListing(BaseModel):
    model_config = _FROZEN

    page_type: PageType
    access_state: AccessState
    listing: NormalizedListing | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> ParsedListing:
        detail_ok = self.page_type == "detail" and self.access_state == AccessState.OK
        if detail_ok and self.listing is None and not self.errors:
            raise ValueError("a detail page without a listing must explain why in errors")
        return self


class ParseOutcome(BaseModel):
    """Per-sample summary fed to parser health."""

    model_config = _FROZEN

    page_type: PageType
    access_state: AccessState
    ok: bool
    listing_count: int = 0
    has_price: bool = False
    has_mileage: bool = False
    has_make_model: bool = False
    currency: str | None = None
    price_minor: int | None = None
    mileage_km: Decimal | None = None
    unexpected_host: bool = False
    observed_at: datetime


class ParserHealth(BaseModel):
    model_config = _FROZEN

    status: Literal["healthy", "degraded", "unhealthy", "insufficient_sample"]
    sample_size: int
    reasons: tuple[str, ...] = ()
    metrics: dict[str, str] = Field(default_factory=dict)


@runtime_checkable
class CrawlClient(Protocol):
    """Application-owned crawl interface. Implementations enforce URL policy, deadlines and size caps."""

    async def fetch(self, url: str, *, purpose: FetchPurpose, source_key: str) -> RawDocument: ...


@runtime_checkable
class SourceAdapter(Protocol):
    source_key: str
    adapter_version: str

    def capabilities(self) -> SourceCapabilities: ...

    def build_search(self, profile: SearchProfile, cursor: str | None) -> SearchRequest: ...

    async def discover(self, request: SearchRequest, client: CrawlClient) -> DiscoveryPage: ...

    def canonicalize(self, url: str) -> CanonicalIdentity: ...

    async def fetch_detail(self, identity: CanonicalIdentity, client: CrawlClient) -> RawDocument: ...

    def parse_detail(self, document: RawDocument) -> ParsedListing: ...

    def detect_access_state(self, document: RawDocument) -> AccessState: ...

    def assess_parser_health(self, samples: list[ParseOutcome]) -> ParserHealth: ...


class AdapterUnimplemented(RuntimeError):
    """Raised by placeholder adapters whose selectors/routes have not been verified."""
