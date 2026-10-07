"""Exact-listing seller contact evidence, address canonicalisation and seller identity (spec 37.3).

Pure functions, no I/O. Business rules:

- A recipient is acceptable only when the evidence ties the address to the seller of the exact
  listing: an address shown on that advertisement, a marketplace relay bound to that listing, or
  an official dealer contact reached through that listing's own link and positively matched to the
  same seller by a strong identifier (marketplace dealer id, legal-entity id, VAT id or the dealer
  website linked from the listing).
- Never acceptable: guessed addresses (``info@<domain>`` heuristics or any address the evidence
  excerpt does not actually show), generic search results for a similarly named dealer, unrelated
  harvested addresses, a choice between several branches/addresses, and contact forms. A contact
  form, a login/contact-reveal restriction or no e-mail at all yields ``seller_email_unavailable``;
  the restriction is never bypassed and the form is never submitted.
- Canonicalisation is conservative: the domain is lower-cased and IDNA-encoded; the local part is
  kept byte-for-byte (no Gmail dot/plus folding for any provider). Two different canonical
  addresses are equivalent only with explicit evidence for exactly that pair.
- A verified recipient is bound to source URL, listing revision, seller identity, extraction
  location and verification time. ``detect_contact_change`` flags material seller/contact changes
  (cancel the stale queued inquiry) and stale evidence (recheck before dispatch).
- Seller identity: one ``SellerIdentity`` per seller entity with evidenced aliases across sites.
  ``seller_identity_key`` uses the persisted seller entity when linked, else the smallest alias key.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import SellerType
from suv_deals.domain.listings import sha256_json
from suv_deals.domain.notifications import sanitize_seller_text
from suv_deals.errors import ValidationFailed

CONTACT_RULES_VERSION: Final = "seller_contacts@1.0.0"
#: PROPOSED engineering default: recipient evidence older than this is rechecked before dispatch.
RECIPIENT_EVIDENCE_MAX_AGE: Final = timedelta(days=3)
MAX_ADDRESS_LENGTH: Final = 254
MAX_LOCAL_PART_LENGTH: Final = 64
MAX_DOMAIN_LENGTH: Final = 253

_FROZEN = ConfigDict(frozen=True, extra="forbid")

# ---------------------------------------------------------------------------------------------
# Address canonicalisation
# ---------------------------------------------------------------------------------------------

# Conservative local part: common dot-atom characters only (no quoted strings, comments or
# exotic atext); anything else goes to technical review instead of being guessed at.
_LOCAL_RE: Final = re.compile(r"^[A-Za-z0-9_%+'-]+(?:\.[A-Za-z0-9_%+'-]+)*$")
_LABEL_RE: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NON_DELIVERABLE_LOCALS: Final = frozenset(
    {"noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster", "bounce"}
)


class CanonicalAddress(BaseModel):
    """``local_part`` exactly as shown; ``domain`` lower-cased ASCII (IDNA)."""

    model_config = _FROZEN

    local_part: str
    domain: str

    @property
    def canonical(self) -> str:
        return f"{self.local_part}@{self.domain}"

    def __str__(self) -> str:
        return self.canonical


class AddressError(ValidationFailed):
    def __init__(self, problem: str) -> None:
        self.problem = problem
        super().__init__("e-mail address rejected", details={"problems": [problem]})


def canonicalize_address(raw: str) -> CanonicalAddress:
    """Parse and canonicalise conservatively; raises ``AddressError`` with a problem code."""
    if not isinstance(raw, str):
        raise AddressError("INVALID_ADDRESS")
    if any(c in raw for c in "\r\n\x00\x85\u2028\u2029"):
        raise AddressError("HEADER_INJECTION")  # checked before stripping: never tolerated
    value = raw.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AddressError("HEADER_INJECTION")
    if value.lower().startswith("mailto:"):
        value = value[len("mailto:") :]
        if "?" in value or "&" in value:
            raise AddressError("MAILTO_PARAMETERS")  # could smuggle cc/bcc/body
    if not value or len(value) > MAX_ADDRESS_LENGTH:
        raise AddressError("INVALID_ADDRESS")
    if any(c.isspace() for c in value) or any(c in value for c in '<>,;:"()[]\\'):
        raise AddressError("INVALID_ADDRESS")
    if value.count("@") != 1:
        raise AddressError("INVALID_ADDRESS")
    local, domain = value.split("@")
    if not local or len(local) > MAX_LOCAL_PART_LENGTH or not _LOCAL_RE.fullmatch(local):
        raise AddressError("INVALID_ADDRESS")
    domain = domain.rstrip(".") if domain.endswith(".") and not domain.endswith("..") else domain
    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise AddressError("INVALID_ADDRESS") from exc
    labels = ascii_domain.split(".")
    if (
        len(ascii_domain) > MAX_DOMAIN_LENGTH
        or len(labels) < 2
        or not all(_LABEL_RE.fullmatch(label) for label in labels)
        or not (labels[-1].isalpha() or labels[-1].startswith("xn--"))
    ):
        raise AddressError("INVALID_ADDRESS")
    return CanonicalAddress(local_part=local, domain=ascii_domain)


class AddressEquivalenceEvidence(BaseModel):
    """Explicit evidence that two exact canonical addresses reach the same mailbox."""

    model_config = _FROZEN

    first: str
    second: str
    kind: Literal["provider_documented_alias", "seller_confirmed_in_writing"]
    evidence_ref: str = Field(min_length=1, max_length=200)


def addresses_equivalent(
    first: str, second: str, *, evidence: Collection[AddressEquivalenceEvidence] = ()
) -> bool:
    """Equal canonical forms, or an explicit evidence record for exactly this pair. No folding."""
    a, b = canonicalize_address(first).canonical, canonicalize_address(second).canonical
    if a == b:
        return True
    for item in evidence:
        pair = {canonicalize_address(item.first).canonical, canonicalize_address(item.second).canonical}
        if pair == {a, b}:
            return True
    return False


# ---------------------------------------------------------------------------------------------
# Seller identity
# ---------------------------------------------------------------------------------------------

AliasKind = Literal["marketplace_seller_id", "dealer_website_domain", "legal_entity_id", "vat_id"]
AliasEvidenceKind = Literal[
    "listing_seller_block",  # the seller block on the listing names this marketplace seller id
    "dealer_profile_link",  # the listing links to this dealer profile/website
    "same_legal_entity_id",  # imprint/registry id identical across sites
    "same_vat_id",  # VAT id identical across sites
    "same_dealer_website",  # both listings link the same official dealer website
    "manual_review",  # an owner/reviewer linked the aliases with a stored note
]


def _normalize_alias_reference(kind: AliasKind, reference: str) -> str:
    value = reference.strip()
    if kind == "dealer_website_domain":
        value = value.lower().removeprefix("www.")
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z0-9-]+", value):
            raise ValueError("dealer_website_domain must be a bare domain")
    elif kind in {"vat_id", "legal_entity_id"}:
        value = re.sub(r"[\s.\-/]", "", value).upper()
    if not value:
        raise ValueError("alias reference must not be empty")
    return value


class SellerAlias(BaseModel):
    """One evidenced appearance of a seller (on one site, or by a legal/VAT id or website)."""

    model_config = _FROZEN

    alias_kind: AliasKind
    source_key: str | None = Field(default=None, min_length=1, max_length=80)  # None for site-free ids
    reference: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=200)  # untrusted business name
    evidence_kind: AliasEvidenceKind
    evidence_excerpt: str | None = Field(default=None, max_length=500)
    source_url: str | None = Field(default=None, max_length=2048)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("reference")
    @classmethod
    def _normalize(cls, value: str, info: ValidationInfo) -> str:
        kind = info.data.get("alias_kind")
        if kind is None:  # alias_kind itself failed validation; that error is reported
            return value
        return _normalize_alias_reference(kind, value)

    @model_validator(mode="after")
    def _site(self) -> SellerAlias:
        if self.alias_kind == "marketplace_seller_id" and self.source_key is None:
            raise ValueError("a marketplace seller id needs its source_key")
        return self

    def alias_key(self) -> str:
        site = self.source_key or "*"
        return f"{self.alias_kind}:{site}:{self.reference}"


class SellerIdentity(BaseModel):
    """A seller with evidenced aliases. ``seller_entity_id`` is set once persisted/linked."""

    model_config = _FROZEN

    seller_entity_id: UUID | None = None
    seller_type: SellerType = SellerType.UNKNOWN
    aliases: tuple[SellerAlias, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _distinct(self) -> SellerIdentity:
        keys = [a.alias_key() for a in self.aliases]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate seller aliases")
        return self

    def alias_keys(self) -> frozenset[str]:
        return frozenset(a.alias_key() for a in self.aliases)


def seller_identity_key(identity: SellerIdentity) -> str:
    """Stable identity key: the linked seller entity, else the smallest alias key (hashed)."""
    if identity.seller_entity_id is not None:
        return f"seller_entity:{identity.seller_entity_id}"
    smallest = min(identity.alias_keys())
    return f"seller_alias:{sha256_json(smallest)}"


SellerRelation = Literal["same", "different", "unknown"]
_STRONG_SHARED: Final[frozenset[str]] = frozenset(
    {"marketplace_seller_id", "legal_entity_id", "vat_id", "dealer_website_domain"}
)


def seller_relation(first: SellerIdentity, second: SellerIdentity) -> SellerRelation:
    """``same`` on a shared entity or a shared strong alias; ``different`` only on positive
    evidence (two different linked entities, or two different VAT/legal ids); else ``unknown``."""
    if first.seller_entity_id is not None and first.seller_entity_id == second.seller_entity_id:
        return "same"
    shared = first.alias_keys() & second.alias_keys()
    if any(key.split(":", 1)[0] in _STRONG_SHARED for key in shared):
        return "same"
    if first.seller_entity_id is not None and second.seller_entity_id is not None:
        return "different"
    for kind in ("vat_id", "legal_entity_id"):
        a = {x.reference for x in first.aliases if x.alias_kind == kind}
        b = {x.reference for x in second.aliases if x.alias_kind == kind}
        if a and b and not (a & b):
            return "different"
    return "unknown"


# ---------------------------------------------------------------------------------------------
# Recipient evidence and verification
# ---------------------------------------------------------------------------------------------


class RecipientEvidenceKind(StrEnum):
    EMAIL_ON_ADVERTISEMENT = "email_on_advertisement"
    MARKETPLACE_RELAY_FOR_LISTING = "marketplace_relay_for_listing"
    OFFICIAL_DEALER_CONTACT_VIA_LISTING = "official_dealer_contact_via_listing"
    GUESSED_ADDRESS = "guessed_address"
    GENERIC_SEARCH_RESULT = "generic_search_result"
    UNRELATED_HARVESTED = "unrelated_harvested"
    CONTACT_FORM_ONLY = "contact_form_only"
    CONTACT_REVEAL_RESTRICTED = "contact_reveal_restricted"
    NO_EMAIL_FOUND = "no_email_found"


ACCEPTABLE_EVIDENCE_KINDS: Final = frozenset(
    {
        RecipientEvidenceKind.EMAIL_ON_ADVERTISEMENT,
        RecipientEvidenceKind.MARKETPLACE_RELAY_FOR_LISTING,
        RecipientEvidenceKind.OFFICIAL_DEALER_CONTACT_VIA_LISTING,
    }
)
_UNAVAILABLE_KINDS: Final = frozenset(
    {
        RecipientEvidenceKind.CONTACT_FORM_ONLY,
        RecipientEvidenceKind.CONTACT_REVEAL_RESTRICTED,
        RecipientEvidenceKind.NO_EMAIL_FOUND,
    }
)


class ExtractionLocation(StrEnum):
    LISTING_CONTACT_BLOCK = "listing_contact_block"  # structured seller contact section of the ad
    LISTING_DESCRIPTION = "listing_description"  # seller-written ad text
    LISTING_RELAY_CONTACT = "listing_relay_contact"  # relay shown/bound for this listing
    DEALER_PAGE_LINKED_FROM_LISTING = "dealer_page_linked_from_listing"  # official site via the ad link
    MARKETPLACE_DEALER_PROFILE = "marketplace_dealer_profile"  # profile linked from the ad
    OTHER = "other"


DealerIdentifier = Literal[
    "marketplace_dealer_id", "legal_entity_id", "vat_id", "dealer_website_domain", "business_name"
]
_STRONG_DEALER_IDENTIFIERS: Final[frozenset[str]] = frozenset(
    {"marketplace_dealer_id", "legal_entity_id", "vat_id", "dealer_website_domain"}
)


class DealerMatchEvidence(BaseModel):
    """How an official dealer contact page was reached and matched to the listing's seller."""

    model_config = _FROZEN

    reached_via_listing_link: bool
    listing_link_url: str | None = Field(default=None, max_length=2048)
    matched_identifiers: tuple[DealerIdentifier, ...] = ()
    conflicting_identifiers: tuple[DealerIdentifier, ...] = ()


class RecipientEvidence(BaseModel):
    """Where and how a seller e-mail address (or its absence) was observed for one listing."""

    model_config = _FROZEN

    kind: RecipientEvidenceKind
    address: str | None = Field(default=None, max_length=320)
    listing_id: UUID
    listing_incarnation_id: UUID | None = None
    listing_revision_id: UUID | None = None
    listing_revision_number: int = Field(ge=0)
    source_key: str = Field(min_length=1, max_length=80)
    listing_reference: str = Field(min_length=1, max_length=200)
    listing_url: str = Field(min_length=8, max_length=2048)
    evidence_url: str | None = Field(default=None, min_length=8, max_length=2048)
    extraction_location: ExtractionLocation
    extraction_excerpt: str | None = Field(default=None, max_length=500)
    seller: SellerIdentity
    relay_listing_reference: str | None = Field(default=None, max_length=200)
    dealer_match: DealerMatchEvidence | None = None
    distinct_addresses_on_page: int = Field(default=1, ge=0, le=1000)
    branch_count: int | None = Field(default=None, ge=0, le=10000)
    observed_at: datetime
    verified_at: datetime

    @field_validator("observed_at", "verified_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class RecipientStatus(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    SELLER_EMAIL_UNAVAILABLE = "seller_email_unavailable"
    NEEDS_TECHNICAL_REVIEW = "needs_technical_review"
    RECHECK_REQUIRED = "recheck_required"


class RecipientReason(StrEnum):
    VERIFIED_EMAIL_ON_ADVERTISEMENT = "verified_email_on_advertisement"
    VERIFIED_RELAY_FOR_LISTING = "verified_relay_for_listing"
    VERIFIED_DEALER_CONTACT_VIA_LISTING = "verified_dealer_contact_via_listing"
    GUESSED_ADDRESS = "guessed_address"
    GENERIC_SEARCH_RESULT = "generic_search_result"
    UNRELATED_HARVESTED = "unrelated_harvested"
    CONTACT_FORM_ONLY = "contact_form_only"
    CONTACT_REVEAL_RESTRICTED = "contact_reveal_restricted"
    NO_EMAIL_FOUND = "no_email_found"
    ADDRESS_MISSING = "address_missing"
    INVALID_ADDRESS = "invalid_address"
    NON_DELIVERABLE_ROLE = "non_deliverable_role"
    RECIPIENT_IS_SENDER = "recipient_is_sender"
    EXCERPT_DOES_NOT_SHOW_ADDRESS = "excerpt_does_not_show_address"
    EVIDENCE_NOT_ON_LISTING = "evidence_not_on_listing"
    WRONG_EXTRACTION_LOCATION = "wrong_extraction_location"
    MULTIPLE_ADDRESSES = "multiple_addresses"
    MULTIPLE_BRANCHES = "multiple_branches"
    RELAY_NOT_BOUND_TO_LISTING = "relay_not_bound_to_listing"
    RELAY_DOMAIN_UNREGISTERED = "relay_domain_unregistered"
    DEALER_SELLER_NOT_DEALER = "dealer_seller_not_dealer"
    DEALER_NOT_REACHED_VIA_LISTING = "dealer_not_reached_via_listing"
    DEALER_MATCH_WEAK = "dealer_match_weak"
    DEALER_MATCH_CONFLICT = "dealer_match_conflict"
    INVALID_LISTING_URL = "invalid_listing_url"
    INVALID_EVIDENCE_TIME = "invalid_evidence_time"
    EVIDENCE_STALE = "evidence_stale"


class RecipientBinding(BaseModel):
    """What a verified recipient is bound to; stored with the inquiry and rechecked at dispatch."""

    model_config = _FROZEN

    canonical_address: str
    address_domain: str
    evidence_kind: RecipientEvidenceKind
    listing_id: UUID
    listing_incarnation_id: UUID | None
    listing_revision_id: UUID | None
    listing_revision_number: int
    source_key: str
    listing_reference: str
    listing_url: str
    evidence_url: str | None
    extraction_location: ExtractionLocation
    seller_identity_key: str
    verified_at: datetime
    rules_version: str = CONTACT_RULES_VERSION

    def fingerprint(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


class RecipientDecision(BaseModel):
    model_config = _FROZEN

    status: RecipientStatus
    reasons: tuple[RecipientReason, ...]
    binding: RecipientBinding | None = None
    evidence_excerpt: str | None = None  # sanitized (no contact data is repeated)
    rules_version: str = CONTACT_RULES_VERSION

    @model_validator(mode="after")
    def _binding_iff_verified(self) -> RecipientDecision:
        if (self.binding is not None) != (self.status == RecipientStatus.VERIFIED):
            raise ValueError("a binding exists exactly for a verified recipient")
        return self

    @property
    def verified(self) -> bool:
        return self.status == RecipientStatus.VERIFIED


def _http_url(url: str | None) -> bool:
    if url is None:
        return False
    try:
        parts = urlsplit(url)
        return (
            parts.scheme in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and not any(c.isspace() for c in url)
        )
    except ValueError:
        return False


def _host(url: str | None) -> str | None:
    if url is None:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower().removeprefix("www.") if host else None


def _without_fragment(url: str) -> str:
    return url.split("#", 1)[0]


#: Characters that may continue an address on either side; a match must not be part of a longer
#: address (``info@dealer.example`` is not shown by ``sales.info@dealer.example``).
_ADDRESS_LEFT: Final = r"(?<![A-Za-z0-9._%+'@-])"
_ADDRESS_RIGHT: Final = r"(?![A-Za-z0-9_@-]|\.[A-Za-z0-9])"


def _excerpt_shows_address(excerpt: str | None, candidates: tuple[str, ...]) -> bool:
    """The excerpt shows exactly one of ``candidates`` as a whole address (domain case-insensitive)."""
    if not excerpt:
        return False
    for candidate in candidates:
        if not candidate or "@" not in candidate:
            continue
        local, _, domain = candidate.rpartition("@")
        pattern = rf"{_ADDRESS_LEFT}{re.escape(local)}@(?i:{re.escape(domain)}){_ADDRESS_RIGHT}"
        if re.search(pattern, excerpt):
            return True
    return False


def _decision(
    status: RecipientStatus, *reasons: RecipientReason, binding: RecipientBinding | None = None
) -> RecipientDecision:
    return RecipientDecision(status=status, reasons=tuple(dict.fromkeys(reasons)), binding=binding)


_REJECT_KINDS: Final[dict[RecipientEvidenceKind, RecipientReason]] = {
    RecipientEvidenceKind.GUESSED_ADDRESS: RecipientReason.GUESSED_ADDRESS,
    RecipientEvidenceKind.GENERIC_SEARCH_RESULT: RecipientReason.GENERIC_SEARCH_RESULT,
    RecipientEvidenceKind.UNRELATED_HARVESTED: RecipientReason.UNRELATED_HARVESTED,
}
_UNAVAILABLE_REASONS: Final[dict[RecipientEvidenceKind, RecipientReason]] = {
    RecipientEvidenceKind.CONTACT_FORM_ONLY: RecipientReason.CONTACT_FORM_ONLY,
    RecipientEvidenceKind.CONTACT_REVEAL_RESTRICTED: RecipientReason.CONTACT_REVEAL_RESTRICTED,
    RecipientEvidenceKind.NO_EMAIL_FOUND: RecipientReason.NO_EMAIL_FOUND,
}


def verify_recipient(
    evidence: RecipientEvidence,
    *,
    now: datetime,
    relay_domains: Collection[str] = (),
    sender_addresses: Collection[str] = (),
    max_evidence_age: timedelta = RECIPIENT_EVIDENCE_MAX_AGE,
) -> RecipientDecision:
    """Decide whether ``evidence`` verifies a recipient for the exact listing's seller.

    ``relay_domains`` are the relay mail domains registered for the evidence's source (a relay
    on any other domain needs technical review). ``sender_addresses`` are our own configured
    sender/reply-to addresses, which can never be a seller recipient.
    """
    now = ensure_utc(now)
    if evidence.kind in _REJECT_KINDS:
        return _decision(RecipientStatus.REJECTED, _REJECT_KINDS[evidence.kind])
    if evidence.kind in _UNAVAILABLE_KINDS:
        return _decision(RecipientStatus.SELLER_EMAIL_UNAVAILABLE, _UNAVAILABLE_REASONS[evidence.kind])

    if evidence.address is None or not evidence.address.strip():
        return _decision(RecipientStatus.SELLER_EMAIL_UNAVAILABLE, RecipientReason.ADDRESS_MISSING)
    try:
        address = canonicalize_address(evidence.address)
    except AddressError:
        return _decision(RecipientStatus.REJECTED, RecipientReason.INVALID_ADDRESS)
    if address.local_part.lower() in _NON_DELIVERABLE_LOCALS:
        return _decision(RecipientStatus.REJECTED, RecipientReason.NON_DELIVERABLE_ROLE)
    own: set[str] = set()
    for item in sender_addresses:
        try:
            own.add(canonicalize_address(item).canonical.lower())
        except AddressError:
            continue
    if address.canonical.lower() in own:
        return _decision(RecipientStatus.REJECTED, RecipientReason.RECIPIENT_IS_SENDER)
    if not _http_url(evidence.listing_url) or (
        evidence.evidence_url is not None and not _http_url(evidence.evidence_url)
    ):
        return _decision(RecipientStatus.NEEDS_TECHNICAL_REVIEW, RecipientReason.INVALID_LISTING_URL)
    if evidence.verified_at < evidence.observed_at or evidence.verified_at > now:
        return _decision(RecipientStatus.NEEDS_TECHNICAL_REVIEW, RecipientReason.INVALID_EVIDENCE_TIME)
    if evidence.distinct_addresses_on_page > 1:
        return _decision(RecipientStatus.REJECTED, RecipientReason.MULTIPLE_ADDRESSES)
    if evidence.branch_count is not None and evidence.branch_count > 1:
        return _decision(RecipientStatus.REJECTED, RecipientReason.MULTIPLE_BRANCHES)

    shows_address = _excerpt_shows_address(
        evidence.extraction_excerpt, (address.canonical, evidence.address.strip())
    )

    if evidence.kind == RecipientEvidenceKind.EMAIL_ON_ADVERTISEMENT:
        if evidence.extraction_location not in {
            ExtractionLocation.LISTING_CONTACT_BLOCK,
            ExtractionLocation.LISTING_DESCRIPTION,
        }:
            return _decision(RecipientStatus.REJECTED, RecipientReason.WRONG_EXTRACTION_LOCATION)
        if evidence.evidence_url is None or _without_fragment(evidence.evidence_url) != _without_fragment(
            evidence.listing_url
        ):
            return _decision(RecipientStatus.REJECTED, RecipientReason.EVIDENCE_NOT_ON_LISTING)
        if not shows_address:
            return _decision(RecipientStatus.REJECTED, RecipientReason.EXCERPT_DOES_NOT_SHOW_ADDRESS)
        reason = RecipientReason.VERIFIED_EMAIL_ON_ADVERTISEMENT
    elif evidence.kind == RecipientEvidenceKind.MARKETPLACE_RELAY_FOR_LISTING:
        if evidence.extraction_location != ExtractionLocation.LISTING_RELAY_CONTACT:
            return _decision(RecipientStatus.REJECTED, RecipientReason.WRONG_EXTRACTION_LOCATION)
        if evidence.relay_listing_reference is None or (
            evidence.relay_listing_reference.strip() != evidence.listing_reference.strip()
        ):
            return _decision(RecipientStatus.REJECTED, RecipientReason.RELAY_NOT_BOUND_TO_LISTING)
        if evidence.evidence_url is not None and _host(evidence.evidence_url) != _host(evidence.listing_url):
            return _decision(RecipientStatus.REJECTED, RecipientReason.EVIDENCE_NOT_ON_LISTING)
        registered = {d.strip().lower().rstrip(".") for d in relay_domains}
        if address.domain not in registered:
            return _decision(
                RecipientStatus.NEEDS_TECHNICAL_REVIEW, RecipientReason.RELAY_DOMAIN_UNREGISTERED
            )
        reason = RecipientReason.VERIFIED_RELAY_FOR_LISTING
    else:  # OFFICIAL_DEALER_CONTACT_VIA_LISTING
        match = evidence.dealer_match
        if evidence.seller.seller_type != SellerType.DEALER:
            return _decision(RecipientStatus.REJECTED, RecipientReason.DEALER_SELLER_NOT_DEALER)
        if evidence.extraction_location not in {
            ExtractionLocation.DEALER_PAGE_LINKED_FROM_LISTING,
            ExtractionLocation.MARKETPLACE_DEALER_PROFILE,
        }:
            return _decision(RecipientStatus.REJECTED, RecipientReason.WRONG_EXTRACTION_LOCATION)
        if (
            match is None
            or not match.reached_via_listing_link
            or not _http_url(match.listing_link_url)
            or evidence.evidence_url is None
            or _host(evidence.evidence_url) != _host(match.listing_link_url)
        ):
            return _decision(RecipientStatus.REJECTED, RecipientReason.DEALER_NOT_REACHED_VIA_LISTING)
        if match.conflicting_identifiers:
            return _decision(RecipientStatus.REJECTED, RecipientReason.DEALER_MATCH_CONFLICT)
        if not set(match.matched_identifiers) & _STRONG_DEALER_IDENTIFIERS:
            return _decision(RecipientStatus.NEEDS_TECHNICAL_REVIEW, RecipientReason.DEALER_MATCH_WEAK)
        if not shows_address:
            return _decision(RecipientStatus.REJECTED, RecipientReason.EXCERPT_DOES_NOT_SHOW_ADDRESS)
        reason = RecipientReason.VERIFIED_DEALER_CONTACT_VIA_LISTING

    if now - evidence.verified_at > max_evidence_age:
        return _decision(RecipientStatus.RECHECK_REQUIRED, reason, RecipientReason.EVIDENCE_STALE)
    binding = RecipientBinding(
        canonical_address=address.canonical,
        address_domain=address.domain,
        evidence_kind=evidence.kind,
        listing_id=evidence.listing_id,
        listing_incarnation_id=evidence.listing_incarnation_id,
        listing_revision_id=evidence.listing_revision_id,
        listing_revision_number=evidence.listing_revision_number,
        source_key=evidence.source_key,
        listing_reference=evidence.listing_reference,
        listing_url=evidence.listing_url,
        evidence_url=evidence.evidence_url,
        extraction_location=evidence.extraction_location,
        seller_identity_key=seller_identity_key(evidence.seller),
        verified_at=evidence.verified_at,
    )
    return RecipientDecision(
        status=RecipientStatus.VERIFIED,
        reasons=(reason,),
        binding=binding,
        evidence_excerpt=sanitize_seller_text(evidence.extraction_excerpt, max_length=160),
    )


# ---------------------------------------------------------------------------------------------
# Material change detection before dispatch
# ---------------------------------------------------------------------------------------------


class ContactChange(StrEnum):
    ADDRESS_CHANGED = "address_changed"
    SELLER_CHANGED = "seller_changed"
    LISTING_CHANGED = "listing_changed"
    LISTING_URL_CHANGED = "listing_url_changed"
    EVIDENCE_KIND_CHANGED = "evidence_kind_changed"
    RECIPIENT_NO_LONGER_VERIFIED = "recipient_no_longer_verified"
    REVISION_CHANGED = "revision_changed"  # informational for the contact; listing rules decide
    EVIDENCE_STALE = "evidence_stale"


_MATERIAL: Final = frozenset(
    {
        ContactChange.ADDRESS_CHANGED,
        ContactChange.SELLER_CHANGED,
        ContactChange.LISTING_CHANGED,
        ContactChange.LISTING_URL_CHANGED,
        ContactChange.EVIDENCE_KIND_CHANGED,
        ContactChange.RECIPIENT_NO_LONGER_VERIFIED,
    }
)


class ContactRecheck(BaseModel):
    model_config = _FROZEN

    material_change: bool  # the bound recipient is no longer the verified seller contact: cancel
    recheck_required: bool  # evidence must be refreshed before transmission: hold
    changes: tuple[ContactChange, ...]


def detect_contact_change(
    bound: RecipientBinding,
    current: RecipientDecision | None,
    *,
    now: datetime,
    max_evidence_age: timedelta = RECIPIENT_EVIDENCE_MAX_AGE,
) -> ContactRecheck:
    """Compare the inquiry's bound recipient with the latest recipient decision (if any)."""
    now = ensure_utc(now)
    changes: list[ContactChange] = []
    latest = bound
    if current is not None:
        if current.binding is None:
            changes.append(ContactChange.RECIPIENT_NO_LONGER_VERIFIED)
        else:
            latest = current.binding
            if latest.canonical_address != bound.canonical_address:
                changes.append(ContactChange.ADDRESS_CHANGED)
            if latest.seller_identity_key != bound.seller_identity_key:
                changes.append(ContactChange.SELLER_CHANGED)
            if (latest.listing_id, latest.listing_incarnation_id) != (
                bound.listing_id,
                bound.listing_incarnation_id,
            ):
                changes.append(ContactChange.LISTING_CHANGED)
            if latest.listing_url != bound.listing_url:
                changes.append(ContactChange.LISTING_URL_CHANGED)
            if latest.evidence_kind != bound.evidence_kind:
                changes.append(ContactChange.EVIDENCE_KIND_CHANGED)
            if latest.listing_revision_number != bound.listing_revision_number:
                changes.append(ContactChange.REVISION_CHANGED)
    if now - latest.verified_at > max_evidence_age:
        changes.append(ContactChange.EVIDENCE_STALE)
    material = any(c in _MATERIAL for c in changes)
    return ContactRecheck(
        material_change=material,
        recheck_required=material or ContactChange.EVIDENCE_STALE in changes,
        changes=tuple(changes),
    )


__all__ = [
    "ACCEPTABLE_EVIDENCE_KINDS",
    "CONTACT_RULES_VERSION",
    "RECIPIENT_EVIDENCE_MAX_AGE",
    "AddressEquivalenceEvidence",
    "AddressError",
    "CanonicalAddress",
    "ContactChange",
    "ContactRecheck",
    "DealerMatchEvidence",
    "ExtractionLocation",
    "RecipientBinding",
    "RecipientDecision",
    "RecipientEvidence",
    "RecipientEvidenceKind",
    "RecipientReason",
    "RecipientStatus",
    "SellerAlias",
    "SellerIdentity",
    "addresses_equivalent",
    "canonicalize_address",
    "detect_contact_change",
    "seller_identity_key",
    "seller_relation",
    "verify_recipient",
]
