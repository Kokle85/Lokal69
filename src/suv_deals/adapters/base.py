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
from suv_deals.domain.enums import AccessState, Completeness, CoverageMode, SellerType
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
    # False for placeholder adapters whose capabilities are conservative defaults, not established facts.
    verified: bool = False
    # True when parse_detail reports exact-ad seller-contact evidence (ParsedListing.seller_contact;
    # spec 37.3). Sources without it can never produce an inquiry recipient (wave D2, F1).
    seller_contact_evidence: bool = False


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
    # Allow-listed response headers only
    # (content-type, retry-after, last-modified, etag, x-robots-tag, x-robots-status).
    response_headers: dict[str, str] = Field(default_factory=dict)
    crawler_version: str | None = None
    server_processing_ms: int | None = Field(default=None, ge=0)
    cache_status: str | None = Field(default=None, max_length=40)
    # True when the failure is in our own crawler infrastructure (crawler down, crawler 5xx/429,
    # bad crawler response) rather than at the target host; never counted against the host circuit.
    infrastructure_failure: bool = False
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
    # Search-page diagnostics (dropped off-policy links, refused next link, ...).
    warnings: tuple[str, ...] = ()

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


#: Where an address was seen on the advertisement (``domain.seller_contacts.ExtractionLocation``).
ShownEmailLocation = Literal["listing_contact_block", "listing_description"]
AdTextFieldName = Literal["title", "description", "seller_note"]


class ShownEmail(BaseModel):
    """One e-mail address VISIBLE on the advertisement (never a guessed or hidden one)."""

    model_config = _FROZEN

    address: str = Field(min_length=3, max_length=320)
    location: ShownEmailLocation
    #: The visible text around the address on the page (bounded); it must show the address.
    excerpt: str = Field(min_length=3, max_length=500)


class AdText(BaseModel):
    """Advertisement text for the inquiry-language decision (``domain.language.AdTextFragment``).

    ``seller_written`` is False for platform labels and composed titles; ``machine_translated``
    when the site marks the text as an automatic translation. Neither decides the language.
    """

    model_config = _FROZEN

    field: AdTextFieldName
    text: str = Field(min_length=1, max_length=4000)
    seller_written: bool
    machine_translated: bool = False
    selector: str | None = Field(default=None, max_length=200)


class SellerContactEvidence(BaseModel):
    """What ONE advertisement shows about its seller and how to reach them (spec 37.3).

    Kept OUT of ``NormalizedListing`` (stored revisions never carry contact data): the detail
    pipeline turns it into a linked seller entity and one ``app.seller_contacts`` row
    (``crawling.seller_evidence``). Addresses are only those visible on the page; a contact form
    or a reveal restriction is reported as such, never bypassed or submitted.
    """

    model_config = _FROZEN

    seller_type: SellerType = SellerType.UNKNOWN
    seller_name: str | None = Field(default=None, max_length=200)
    #: The site's own seller/dealer id shown or linked on the ad (marketplace seller id).
    seller_reference: str | None = Field(default=None, max_length=200)
    #: The seller's own website linked from the ad (or the dealer's own inventory site).
    dealer_website: str | None = Field(default=None, max_length=2048)
    emails: tuple[ShownEmail, ...] = Field(default=(), max_length=20)
    #: Distinct addresses visible anywhere on the page (several -> no single recipient).
    distinct_addresses: int = Field(default=0, ge=0, le=1000)
    contact_form: bool = False
    contact_reveal_restricted: bool = False
    ad_texts: tuple[AdText, ...] = Field(default=(), max_length=10)
    #: The page's declared language (site navigation; recorded, never decisive).
    page_language: str | None = Field(default=None, max_length=20)


class ParsedListing(BaseModel):
    model_config = _FROZEN

    page_type: PageType
    access_state: AccessState
    listing: NormalizedListing | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    #: Exact-ad seller-contact evidence (adapters with ``seller_contact_evidence``); never stored
    #: with the revision. ``None`` = the adapter does not report it for this page.
    seller_contact: SellerContactEvidence | None = None

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
    # Search-page signals for pagination/result-count/locale tripwires (None = not applicable/unknown).
    pagination_marker_present: bool | None = None
    result_count_reported: int | None = Field(default=None, ge=0)
    locale: str | None = Field(default=None, max_length=20)
    observed_at: datetime


class ParserHealth(BaseModel):
    model_config = _FROZEN

    status: Literal["healthy", "degraded", "unhealthy", "insufficient_sample"]
    sample_size: int
    reasons: tuple[str, ...] = ()
    metrics: dict[str, str] = Field(default_factory=dict)
    # e.g. "pause_new_alerts", "quarantine_new_revisions"; never bulk removal of listings.
    recommended_actions: tuple[str, ...] = ()


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
