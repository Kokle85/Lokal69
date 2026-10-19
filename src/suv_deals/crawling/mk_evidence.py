"""MK comparable detail pages -> market evidence (spec 15; F2, wave D2).

A source with ``role: mk_comparable`` (Pazar3, Reklama5 once an adapter and its terms exist) is
crawled like an acquisition source (the scheduler schedules it, discovery stores its cards as
listing lifecycle rows), but its detail pages are NEVER acquisition revisions: no screening, no
valuation, no review case, no seller inquiry. Each detail page with a usable price becomes one
``asking_price`` market observation instead (`detail.handle_detail` calls `mk_observation`):

- market ``MK``, the ad URL, the source and listing ids, the page's observation time, the parsed
  vehicle, price basis, seller type and availability; the listing's frozen fixture lineage;
- never any seller contact data (``MarketObservation`` has no such field) and never a sale;
- a content-derived id (workspace, listing, price, vehicle): re-fetching an unchanged ad records
  nothing new, a price change records a new observation (history is append-only).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Final
from uuid import UUID

from suv_deals.adapters.base import ParsedListing
from suv_deals.domain.comparables import MarketObservation
from suv_deals.domain.enums import AccessState, EvidenceKind
from suv_deals.domain.money import Money

MK_COMPARABLE_ROLE: Final = "mk_comparable"
_NAMESPACE: Final = uuid.UUID("0b7d5f62-5e0a-4f2a-9a49-8d3b7c1e6a10")


def mk_observation(
    parsed: ParsedListing,
    *,
    workspace_id: UUID,
    listing_id: UUID,
    source_key: str,
    canonical_url: str,
    observed_at: datetime,
    is_fixture: bool,
) -> MarketObservation | None:
    """The ``asking_price`` observation of one MK detail page, or ``None`` (no usable price)."""
    listing = parsed.listing
    if parsed.page_type != "detail" or parsed.access_state != AccessState.OK or listing is None:
        return None
    price = listing.price
    if price.amount_minor is None or price.currency is None or price.amount_minor <= 0:
        return None
    material = json.dumps(
        {
            "workspace": str(workspace_id),
            "listing": str(listing_id),
            "price": price.amount_minor,
            "currency": price.currency,
            "basis": price.basis.value,
            "vehicle": listing.vehicle.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return MarketObservation(
        id=uuid.uuid5(_NAMESPACE, hashlib.sha256(material.encode("utf-8")).hexdigest()),
        source_key=source_key,
        url=canonical_url,
        observed_at=observed_at,
        evidence_kind=EvidenceKind.ASKING_PRICE,
        amount=Money.from_minor(price.amount_minor, price.currency),
        price_basis=price.basis,
        vehicle=listing.vehicle,
        seller_type=listing.seller_type,
        availability=listing.availability,
        market="MK",
        is_fixture=is_fixture,
    )


__all__ = ["MK_COMPARABLE_ROLE", "mk_observation"]
