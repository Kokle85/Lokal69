"""Exact-ad seller, recipient and language evidence from a detail page (spec 37.3; F1, wave D2).

Before D2 nothing outside tests created a seller entity or an ``app.seller_contacts`` row, so every
eligible listing's inquiry plan stopped at ``seller_not_linked``. After every acquisition detail
page that yields a listing revision, `record_seller_evidence` runs in its OWN short transaction
(after the detail commit; the inquiry lock order starts with the controls row, which the detail
commit must not take while it holds listing rows):

1. the seller identity from the advertisement (the site's seller id, the seller's website domain;
   ``domain.seller_contacts.SellerAlias``) is linked to ONE persisted seller entity
   (`sellers_repo.link_seller`, merging on a shared strong alias);
2. the recipient evidence of the page (`ParsedListing.seller_contact`) is verified
   (`verify_recipient`): exactly one address visible on the ad, shown by its excerpt, at the
   listing URL -> ``verified``; several addresses -> rejected; a contact form, a reveal restriction
   or no address -> ``seller_email_unavailable`` (recorded explicitly, never guessed, never
   bypassed); our own sender address can never be a recipient;
3. the inquiry language is decided from the seller-written ad text only
   (`resolve_inquiry_language`; site language and country are recorded, never decisive) and an
   unresolved language is recorded as such;
4. one contact row records all of it (`sellers_repo.record_contact`; an identical re-verification
   only refreshes ``last_rechecked_at``).

A failure is logged with its code and never fails the detail job: the next detail fetch records
the evidence again, and the reconciliation contact sweep re-plans a listing whose contact arrives
after its first plan. Adapters without ``seller_contact_evidence`` report nothing here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID

from suv_deals.adapters.base import SellerContactEvidence
from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Confidence, ExtractionMethod
from suv_deals.domain.language import AdTextFragment, LanguageDecision, resolve_inquiry_language
from suv_deals.domain.listings import NormalizedListing
from suv_deals.domain.provenance import FieldProvenance
from suv_deals.domain.seller_contacts import (
    ExtractionLocation,
    RecipientEvidence,
    RecipientEvidenceKind,
    SellerAlias,
    SellerIdentity,
    verify_recipient,
)
from suv_deals.errors import AppError
from suv_deals.persistence import sellers_repo
from suv_deals.persistence.database import Conn, Database, db_now, fetch_one
from suv_deals.persistence.listings_repo import ListingRecord
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.settings import Settings

logger = logging.getLogger(__name__)

NO_SELLER_IDENTITY: Final = "no_seller_identity"
NO_CURRENT_REVISION: Final = "no_current_revision"
_LOCATIONS: Final = {
    "listing_contact_block": ExtractionLocation.LISTING_CONTACT_BLOCK,
    "listing_description": ExtractionLocation.LISTING_DESCRIPTION,
}


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower().removeprefix("www.") if host and "." in host else None


def seller_aliases(
    contact: SellerContactEvidence, *, source_key: str, listing_url: str, observed_at: datetime
) -> tuple[SellerAlias, ...]:
    """The evidenced aliases the advertisement shows (empty: no seller identity on the page)."""
    aliases: list[SellerAlias] = []
    if contact.seller_reference:
        aliases.append(
            SellerAlias(
                alias_kind="marketplace_seller_id",
                source_key=source_key,
                reference=contact.seller_reference,
                display_name=contact.seller_name,
                evidence_kind="listing_seller_block",
                source_url=listing_url,
                observed_at=observed_at,
            )
        )
    domain = _domain(contact.dealer_website)
    if domain is not None:
        aliases.append(
            SellerAlias(
                alias_kind="dealer_website_domain",
                reference=domain,
                display_name=contact.seller_name,
                evidence_kind="dealer_profile_link",
                source_url=contact.dealer_website,
                observed_at=observed_at,
            )
        )
    return tuple(aliases)


def recipient_evidence(
    contact: SellerContactEvidence,
    *,
    listing: ListingRecord,
    source_key: str,
    revision_id: UUID,
    revision_number: int,
    seller: SellerIdentity,
    observed_at: datetime,
    verified_at: datetime,
) -> RecipientEvidence:
    """The page's recipient evidence: the ONE visible address, or why there is none."""
    shown = contact.emails[0] if contact.emails else None
    if shown is not None:
        kind = RecipientEvidenceKind.EMAIL_ON_ADVERTISEMENT
    elif contact.contact_reveal_restricted:
        kind = RecipientEvidenceKind.CONTACT_REVEAL_RESTRICTED
    elif contact.contact_form:
        kind = RecipientEvidenceKind.CONTACT_FORM_ONLY
    else:
        kind = RecipientEvidenceKind.NO_EMAIL_FOUND
    return RecipientEvidence(
        kind=kind,
        address=None if shown is None else shown.address,
        listing_id=listing.id,
        listing_incarnation_id=listing.id,
        listing_revision_id=revision_id,
        listing_revision_number=revision_number,
        source_key=source_key,
        listing_reference=listing.source_listing_id,
        listing_url=listing.canonical_url,
        evidence_url=listing.canonical_url,
        extraction_location=(
            ExtractionLocation.LISTING_CONTACT_BLOCK if shown is None else _LOCATIONS[shown.location]
        ),
        extraction_excerpt=None if shown is None else shown.excerpt,
        seller=seller,
        distinct_addresses_on_page=contact.distinct_addresses,
        observed_at=observed_at,
        verified_at=verified_at,
    )


def inquiry_language(
    contact: SellerContactEvidence, normalized: NormalizedListing, observed_at: datetime
) -> LanguageDecision:
    """Seller-written ad text decides; site navigation language and country are recorded only."""
    fragments = [
        AdTextFragment(
            field=text.field,
            text=text.text,
            seller_written=text.seller_written,
            machine_translated=text.machine_translated,
            provenance=FieldProvenance(
                method=ExtractionMethod.JSON_LD if text.field == "description" else ExtractionMethod.CSS,
                selector=text.selector,
                source_url=normalized.canonical_url,
                confidence=Confidence.HIGH,
                observed_at=observed_at,
            ),
        )
        for text in contact.ad_texts
    ]
    return resolve_inquiry_language(None, fragments, contact.page_language, normalized.location.country)


async def _current_revision(conn: Conn, actor: ActorContext, listing_id: UUID) -> tuple[UUID, int] | None:
    row = await fetch_one(
        conn,
        "select r.id, r.revision_number from app.listings l"
        " join app.listing_revisions r on r.workspace_id = l.workspace_id and r.id = l.current_revision_id"
        " where l.workspace_id = %(ws)s and l.id = %(id)s",
        {"ws": actor.workspace_id, "id": listing_id},
    )
    return None if row is None else (row["id"], int(row["revision_number"]))


def sender_addresses(settings: Settings) -> tuple[str, ...]:
    return tuple(a for a in (settings.seller_email_from, settings.seller_email_reply_to) if a)


async def record_seller_evidence(
    db: Database,
    actor: ActorContext,
    settings: Settings,
    *,
    listing: ListingRecord,
    source_key: str,
    revision_id: UUID | None,
    revision_number: int | None,
    normalized: NormalizedListing,
    contact: SellerContactEvidence,
    observed_at: datetime,
) -> str:
    """Link the seller and record recipient + language evidence (see the module docstring).

    ``revision_id``/``revision_number`` name the revision the page produced; ``None`` (an
    unchanged re-fetch) binds the evidence to the listing's CURRENT revision, read in the same
    transaction. Returns the recipient status (``verified``, ``seller_email_unavailable``,
    ``rejected``, ...), ``no_seller_identity`` when the page names no seller identity,
    ``no_current_revision``, or ``error:<CODE>``.
    """
    observed = ensure_utc(observed_at)
    aliases = seller_aliases(
        contact, source_key=source_key, listing_url=listing.canonical_url, observed_at=observed
    )
    if not aliases:
        return NO_SELLER_IDENTITY
    try:
        async with unit_of_work(db, actor) as conn:
            if revision_id is None or revision_number is None:
                current = await _current_revision(conn, actor, listing.id)
                if current is None:
                    return NO_CURRENT_REVISION
                revision_id, revision_number = current
            now = max(await db_now(conn), observed)
            identity = SellerIdentity(seller_type=contact.seller_type, aliases=aliases)
            linked = await sellers_repo.link_seller(
                conn, actor, identity, reason="seller block of the advertisement (detail page)"
            )
            bound = identity.model_copy(update={"seller_entity_id": linked.entity_id})
            evidence = recipient_evidence(
                contact,
                listing=listing,
                source_key=source_key,
                revision_id=revision_id,
                revision_number=revision_number,
                seller=bound,
                observed_at=observed,
                verified_at=now,
            )
            decision = verify_recipient(evidence, now=now, sender_addresses=sender_addresses(settings))
            await sellers_repo.record_contact(
                conn,
                actor,
                evidence=evidence,
                decision=decision,
                language=inquiry_language(contact, normalized, observed),
                seller_entity_id=linked.entity_id,
            )
    except AppError as exc:
        logger.warning("seller evidence not recorded", extra={"error_code": exc.code.value})
        return f"error:{exc.code.value}"
    return decision.status.value


__all__ = [
    "NO_CURRENT_REVISION",
    "NO_SELLER_IDENTITY",
    "inquiry_language",
    "recipient_evidence",
    "record_seller_evidence",
    "seller_aliases",
    "sender_addresses",
]
