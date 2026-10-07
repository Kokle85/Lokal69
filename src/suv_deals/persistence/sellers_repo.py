"""Seller identity, evidenced aliases and exact-listing contact evidence (spec 37.3, 37.5, 37.8).

The domain decides (``domain.seller_contacts``: alias keys, recipient verification, contact-change
detection; ``domain.inquiries``: merge reconciliation); this module records and enforces:

- ``link_seller`` turns a ``SellerIdentity`` (evidenced aliases found on listings) into ONE
  persisted, unmerged ``app.seller_entities`` row BEFORE any reservation. Aliases are keyed by
  ``sha256(SellerAlias.alias_key())`` (``seller_entity_aliases_active_uidx``: one entity per
  alias). When the aliases of one identity already belong to several entities, those entities
  are merged (``merge_sellers``), so three cross-site appearances of one dealer are one seller.
- ``merge_sellers`` merges under the inquiry lock order (``app.seller_inquiry_controls`` ->
  ``app.seller_entities`` (id order) -> ``app.seller_inquiries`` (id order) ->
  ``ops.inquiry_quota_ledger``) and reconciles reservations in the same transaction: every
  never-transmitted inquiry of the absorbed entity is cancelled (its seller entity can never
  reserve or dispatch again, ``SV003 seller_entity_merged``), and where a (possibly) transmitted
  inquiry exists for the same actual vehicle, the other pending ones are cancelled as
  ``domain.inquiries.reconcile_identity_merge`` decides. Transmitted inquiries of the absorbed
  entity keep counting for the survivor's cooldown and one-inquiry rule (database guard
  ``app.seller_inquiry_vehicle_conflict``), so a merge can never enable a second send.
- ``record_contact`` stores the recipient evidence of ``domain.seller_contacts.verify_recipient``
  and the language decision of ``domain.language`` for exactly one listing and the linked
  seller entity (``app.seller_contacts``). Evidence rows are immutable; a material change
  supersedes the current verified contact (``changed``, ``superseded_by_id``), an identical fresh
  re-verification only records ``last_rechecked_at``. ``recipient_decision`` /
  ``language_decision`` rebuild the domain decisions from the stored evidence (the canonical
  ``RecipientBinding`` uses ``listing_incarnation_id = listing_id``: one listing row is one
  incarnation).

Scopes: writes are system work or ``config:admin``; reads need ``inquiries:read`` (or system).
No address, excerpt or body ever reaches a log line; audit reasons pass the redactor.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Final, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, field_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import InquiryState, MessageLanguage, Scope, SellerType
from suv_deals.domain.inquiries import (
    ExistingInquiry,
    VehicleIdentityRef,
    reconcile_identity_merge,
)
from suv_deals.domain.language import (
    LANGUAGE_RULES_VERSION,
    LanguageDecision,
    LanguageReason,
    LanguageStatus,
)
from suv_deals.domain.seller_contacts import (
    ACCEPTABLE_EVIDENCE_KINDS,
    CONTACT_RULES_VERSION,
    RECIPIENT_EVIDENCE_MAX_AGE,
    AddressError,
    ExtractionLocation,
    RecipientBinding,
    RecipientDecision,
    RecipientEvidence,
    RecipientEvidenceKind,
    RecipientReason,
    RecipientStatus,
    SellerAlias,
    SellerIdentity,
    canonicalize_address,
)
from suv_deals.errors import Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors

ContactStatus = Literal["verified", "unverified", "unavailable", "changed"]

#: Inquiry states that may be cancelled by a merge/reconciliation (never transmitted).
CANCELLABLE_STATES: Final = frozenset(
    {
        InquiryState.CANDIDATE,
        InquiryState.QUALIFYING,
        InquiryState.HELD_FACTS,
        InquiryState.RESERVED,
        InquiryState.QUEUED,
    }
)
#: SQL predicate (inquiry alias ``i``): no send attempt of the inquiry may have reached the
#: provider. Either there is none, or every one is a proven pre-submission failure or was
#: reconciled as ``proven_not_submitted`` (a ``queued`` inquiry after a guarded retry).
NEVER_TRANSMITTED_SQL: Final = (
    "not exists (select 1 from ops.email_delivery_attempts a"
    " where a.workspace_id = i.workspace_id and a.inquiry_id = i.id"
    " and not ((a.outcome = 'pre_submission_failure' and a.pre_submission_proof is not null)"
    "          or a.reconciled_outcome is not distinct from 'proven_not_submitted'))"
)
SELLER_ENTITY_MERGED: Final = "SELLER_ENTITY_MERGED"
IDENTITY_MERGE_DUPLICATE: Final = "IDENTITY_MERGE_DUPLICATE"
_STATUS_PREFIX: Final = "recipient_status:"
_LANGUAGE_REASON_PREFIX: Final = "language_reason:"
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_URL_RE: Final = re.compile(r"^https?://\S+$", re.IGNORECASE)
_FROZEN = ConfigDict(frozen=True, extra="ignore")

_CONTACT_KIND: Final[dict[RecipientEvidenceKind, str]] = {
    RecipientEvidenceKind.EMAIL_ON_ADVERTISEMENT: "ad_email",
    RecipientEvidenceKind.MARKETPLACE_RELAY_FOR_LISTING: "marketplace_relay",
    RecipientEvidenceKind.OFFICIAL_DEALER_CONTACT_VIA_LISTING: "official_dealer_contact",
}
_UNAVAILABLE_KINDS: Final = frozenset(
    {
        RecipientEvidenceKind.CONTACT_FORM_ONLY,
        RecipientEvidenceKind.CONTACT_REVEAL_RESTRICTED,
        RecipientEvidenceKind.NO_EMAIL_FOUND,
    }
)


# =============================================================================================
# Access
# =============================================================================================


def require_inquiry_writer(actor: ActorContext) -> None:
    """Seller/inquiry pipeline writes are system work (or an owner with ``config:admin``)."""
    if actor.principal_kind != "system" and not actor.has(Scope.CONFIG_ADMIN):
        raise Forbidden("Missing scope: config:admin")


def require_inquiry_reader(actor: ActorContext) -> None:
    if actor.principal_kind != "system":
        actor.require(Scope.INQUIRIES_READ)


# =============================================================================================
# Records
# =============================================================================================


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


class SellerEntityRecord(BaseModel):
    model_config = _FROZEN

    id: UUID
    seller_type: SellerType
    display_name: str | None = None
    verified_at: datetime | None = None
    merged_into_id: UUID | None = None
    merged_at: datetime | None = None
    merge_reason: str | None = None
    row_version: int
    created_at: datetime

    @field_validator("verified_at", "merged_at", "created_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @property
    def seller_key(self) -> str:
        return f"seller_entity:{self.merged_into_id or self.id}"


class SellerAliasRecord(BaseModel):
    model_config = _FROZEN

    id: UUID
    seller_entity_id: UUID
    source_id: UUID | None = None
    alias_kind: str
    reference: str
    alias_key_hash: str
    display_name: str | None = None
    evidence_kind: str
    observed_at: datetime
    unlinked_at: datetime | None = None

    @field_validator("observed_at", "unlinked_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class MergeOutcome(BaseModel):
    """What a seller merge did to the inquiries of both entities."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    survivor_id: UUID
    absorbed_id: UUID
    cancelled_inquiry_ids: tuple[UUID, ...] = ()
    transmitted_duplicates: tuple[UUID, ...] = ()
    conflict: bool = False


class LinkedSeller(BaseModel):
    """The persisted, unmerged seller entity of an identity (use ``seller_key`` for inquiries)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: UUID
    created: bool
    aliases_added: int
    merges: tuple[MergeOutcome, ...] = ()

    @property
    def seller_key(self) -> str:
        return f"seller_entity:{self.entity_id}"

    def identity(self, base: SellerIdentity) -> SellerIdentity:
        """``base`` bound to the persisted entity (its key is then ``seller_entity:<id>``)."""
        return base.model_copy(update={"seller_entity_id": self.entity_id})


class SellerContactRecord(BaseModel):
    """One ``app.seller_contacts`` evidence row (owner/system reads only: holds the address)."""

    model_config = _FROZEN

    id: UUID
    source_id: UUID
    source_key: str
    listing_id: UUID
    listing_revision_id: UUID | None = None
    listing_revision_number: int
    seller_entity_id: UUID
    address: str | None = None
    address_domain: str | None = None
    contact_kind: str | None = None
    evidence_kind: RecipientEvidenceKind
    relay_listing_reference: str | None = None
    listing_reference: str
    listing_url: str
    evidence_url: str | None = None
    extraction_location: ExtractionLocation
    extraction_excerpt: str | None = None
    language_code: str | None = None
    language_status: LanguageStatus
    language_basis: Literal["verified_seller_preference", "seller_ad_text", "none"]
    language_confidence: Decimal
    language_evidence_excerpt: str | None = None
    language_rules_version: str | None = None
    status: ContactStatus
    status_reasons: tuple[str, ...] = ()
    rules_version: str
    observed_at: datetime
    verified_at: datetime | None = None
    last_rechecked_at: datetime | None = None
    changed_at: datetime | None = None
    superseded_by_id: UUID | None = None
    created_at: datetime

    @field_validator("observed_at", "verified_at", "last_rechecked_at", "changed_at", "created_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @property
    def seller_key(self) -> str:
        return f"seller_entity:{self.seller_entity_id}"


# =============================================================================================
# Helpers
# =============================================================================================


def alias_key_hash(alias: SellerAlias) -> str:
    """``sha256(SellerAlias.alias_key())`` (``seller_entity_aliases.alias_key_hash``)."""
    return hashlib.sha256(alias.alias_key().encode("utf-8")).hexdigest()


def _clean_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    cleaned = _CONTROL_RE.sub(" ", value).strip()[:limit].strip()
    return cleaned or None


def _url_or_none(value: str | None) -> str | None:
    if value is None or not 8 <= len(value) <= 2048 or not _URL_RE.fullmatch(value):
        return None
    return value


def _reason_text(reason: str) -> str:
    text = _clean_text(reason, 500)
    if text is None or len(text) < 3:
        raise ValidationFailed("a reason of at least 3 characters is required")
    return text


async def lock_controls(conn: Conn, workspace_id: UUID) -> Mapping[str, Any] | None:
    """FIRST lock of the inquiry path: the workspace control row ``FOR UPDATE`` (may be absent)."""
    async with mapped_errors():
        return await fetch_one(
            conn,
            "select id, mode, kill_switch, kill_switch_reason, kill_switch_set_at, max_per_24h,"
            " max_per_15d, seller_cooldown, version, updated_at"
            " from app.seller_inquiry_controls where workspace_id = %(ws)s for update",
            {"ws": workspace_id},
        )


_GET_ENTITY_SQL: Final = (
    "select id, seller_type, display_name, verified_at, merged_into_id, merged_at, merge_reason,"
    " row_version, created_at from app.seller_entities where workspace_id = %(ws)s and id = %(id)s"
)


async def get_seller_entity(conn: Conn, actor: ActorContext, entity_id: UUID) -> SellerEntityRecord:
    require_inquiry_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _GET_ENTITY_SQL,
            {"ws": actor.workspace_id, "id": entity_id},
        )
    if row is None:
        raise NotFound("Seller not found")
    return SellerEntityRecord.model_validate(row)


async def seller_root(conn: Conn, actor: ActorContext, entity_id: UUID) -> UUID:
    """The surviving (unmerged) entity of ``entity_id`` (merges are one level deep)."""
    entity = await get_seller_entity(conn, actor, entity_id)
    return entity.merged_into_id or entity.id


async def list_aliases(conn: Conn, actor: ActorContext, entity_id: UUID) -> list[SellerAliasRecord]:
    """Active aliases of an entity and of every entity merged into it."""
    require_inquiry_reader(actor)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "select a.id, a.seller_entity_id, a.source_id, a.alias_kind, a.reference, a.alias_key_hash,"
            " a.display_name, a.evidence_kind, a.observed_at, a.unlinked_at"
            " from app.seller_entity_aliases a"
            " join app.seller_entities e on e.workspace_id = a.workspace_id and e.id = a.seller_entity_id"
            " where a.workspace_id = %(ws)s and a.unlinked_at is null"
            " and (e.id = %(id)s or e.merged_into_id = %(id)s)"
            " order by a.observed_at, a.id",
            {"ws": actor.workspace_id, "id": entity_id},
        )
    return [SellerAliasRecord.model_validate(r) for r in rows]


async def _source_ids(conn: Conn, workspace_id: UUID, keys: Iterable[str]) -> dict[str, UUID]:
    wanted = sorted(set(keys))
    if not wanted:
        return {}
    rows = await fetch_all(
        conn,
        "select source_key, id from app.sources where workspace_id = %(ws)s and source_key = any(%(keys)s)",
        {"ws": workspace_id, "keys": wanted},
    )
    return {r["source_key"]: r["id"] for r in rows}


async def _entity_family(conn: Conn, workspace_id: UUID, entity_id: UUID) -> Mapping[str, Any] | None:
    return await fetch_one(
        conn,
        "select e.id, e.merged_into_id, e.created_at,"
        " exists(select 1 from app.seller_entities c where c.workspace_id = e.workspace_id"
        "        and c.merged_into_id = e.id) as has_children"
        " from app.seller_entities e where e.workspace_id = %(ws)s and e.id = %(id)s",
        {"ws": workspace_id, "id": entity_id},
    )


# =============================================================================================
# Inquiry cancellation shared with inquiries_repo (never-transmitted work only)
# =============================================================================================


async def cancel_untransmitted(
    conn: Conn,
    actor: ActorContext,
    inquiry_ids: Iterable[UUID],
    *,
    reasons: Sequence[str],
    target: Literal["cancelled", "suppressed"] = "cancelled",
    suppression_reason: str | None = None,
) -> tuple[UUID, ...]:
    """Cancel (or suppress) never-transmitted inquiries; release the debit when nothing was attempted.

    A row changes when it is in a cancellable state and NO send attempt may have reached the
    provider: either it was never attempted, or (a ``queued`` inquiry after a guarded retry) every
    attempt is a proven pre-submission failure or reconciled as ``proven_not_submitted``
    (``NEVER_TRANSMITTED_SQL``). The quota debit is released only for a never-attempted inquiry
    (the ledger guard keeps the debit of anything that had an attempt). The caller must already
    hold the controls and seller locks of the inquiry path. Returns the ids that changed.
    """
    ids = sorted(set(inquiry_ids), key=str)
    if not ids:
        return ()
    codes = [r for r in dict.fromkeys(reasons) if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", r)][:30]
    if target == "suppressed" and suppression_reason is None:
        raise ValidationFailed("a suppression needs its reason")
    changed: list[UUID] = []
    async with mapped_errors():
        for inquiry_id in ids:
            row = await fetch_one(
                conn,
                "update app.seller_inquiries i set state = %(target)s, state_reasons = %(reasons)s,"  # noqa: S608
                " suppression_reason = %(suppression)s, row_version = i.row_version + 1"
                " where i.workspace_id = %(ws)s and i.id = %(id)s"
                f" and i.state = any(%(states)s) and {NEVER_TRANSMITTED_SQL}"
                " returning i.id, i.row_version, exists (select 1 from ops.email_delivery_attempts a"
                "   where a.workspace_id = i.workspace_id and a.inquiry_id = i.id) as attempted",
                {
                    "ws": actor.workspace_id,
                    "id": inquiry_id,
                    "target": target,
                    "reasons": codes,
                    "suppression": suppression_reason if target == "suppressed" else None,
                    "states": sorted(s.value for s in CANCELLABLE_STATES),
                },
            )
            if row is None:
                continue
            attempted = bool(row["attempted"])
            if not attempted:
                await conn.execute(
                    "update ops.inquiry_quota_ledger set released_at = now(), release_reason = %(code)s"
                    " where workspace_id = %(ws)s and inquiry_id = %(id)s and released_at is null",
                    {"ws": actor.workspace_id, "id": inquiry_id, "code": f"inquiry_{target}"},
                )
            await audit.record(
                conn,
                actor,
                f"seller_inquiry.{'cancel' if target == 'cancelled' else 'suppress'}",
                "seller_inquiry",
                inquiry_id,
                new_version=int(row["row_version"]),
                reason=", ".join(codes) or target,
                metadata={
                    "reasons": codes,
                    "suppression_reason": suppression_reason,
                    "debit_retained": attempted,
                },
            )
            changed.append(inquiry_id)
    return tuple(changed)


# =============================================================================================
# Seller merge
# =============================================================================================


async def _family_inquiries(
    conn: Conn, workspace_id: UUID, entity_ids: Sequence[UUID]
) -> list[dict[str, Any]]:
    """Inquiries of the entities (and entities merged into them), locked in id order."""
    rows = await fetch_all(
        conn,
        "select i.id, i.identity_key, i.vehicle_kind, coalesce(i.vehicle_cluster_id, i.vehicle_listing_id)"  # noqa: S608
        " as vehicle_id, i.seller_entity_id, i.state, i.reserved_at, i.send_attempted_at,"
        " (select count(*) from ops.email_delivery_attempts a"
        "   where a.workspace_id = i.workspace_id and a.inquiry_id = i.id) as attempts,"
        f" {NEVER_TRANSMITTED_SQL} as never_transmitted,"
        " array(select i.qualification_listing_id"
        "       union select m.listing_id from app.vehicle_cluster_members m"
        "        where m.workspace_id = i.workspace_id and m.cluster_id = i.vehicle_cluster_id"
        "          and m.unlinked_at is null"
        "       union select m2.listing_id from app.vehicle_cluster_members m1"
        "        join app.vehicle_clusters c on c.workspace_id = m1.workspace_id and c.id = m1.cluster_id"
        "         and c.review_status <> 'rejected'"
        "        join app.vehicle_cluster_members m2 on m2.workspace_id = m1.workspace_id"
        "         and m2.cluster_id = m1.cluster_id and m2.unlinked_at is null"
        "        where m1.workspace_id = i.workspace_id and m1.listing_id = i.qualification_listing_id"
        "          and m1.unlinked_at is null) as vehicle_listings"
        " from app.seller_inquiries i"
        " join app.seller_entities e on e.workspace_id = i.workspace_id and e.id = i.seller_entity_id"
        " where i.workspace_id = %(ws)s and (e.id = any(%(ids)s) or e.merged_into_id = any(%(ids)s))"
        " order by i.id for update of i",
        {"ws": workspace_id, "ids": list(entity_ids)},
    )
    return [dict(r) for r in rows]


def _existing(row: Mapping[str, Any], seller_key: str) -> ExistingInquiry:
    sent = row["send_attempted_at"]
    reserved = row["reserved_at"]
    return ExistingInquiry(
        inquiry_id=row["id"],
        identity_key=row["identity_key"],
        vehicle=VehicleIdentityRef(kind=row["vehicle_kind"], id=row["vehicle_id"]),
        seller_key=seller_key,
        state=InquiryState(row["state"]),
        transmission_attempts=int(row["attempts"]),
        reserved_at=reserved,
        last_contact_at=sent or reserved,
    )


def _vehicle_groups(rows: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Connected components of inquiries whose vehicle listing sets intersect (same actual car)."""
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[UUID, int] = {}
    for index, row in enumerate(rows):
        for listing in row["vehicle_listings"] or ():
            if listing in owner:
                parent[find(index)] = find(owner[listing])
            else:
                owner[listing] = index
    groups: dict[int, list[Mapping[str, Any]]] = {}
    for index, row in enumerate(rows):
        groups.setdefault(find(index), []).append(row)
    return list(groups.values())


async def _merge(
    conn: Conn, actor: ActorContext, survivor_id: UUID, absorbed_id: UUID, reason: str
) -> MergeOutcome:
    """Merge with the controls row already locked by the caller."""
    ws = actor.workspace_id
    if survivor_id == absorbed_id:
        raise ValidationFailed("a seller entity cannot be merged into itself")
    async with mapped_errors():
        await fetch_all(
            conn,
            "select id from app.seller_entities where workspace_id = %(ws)s and id = any(%(ids)s)"
            " order by id for update",
            {"ws": ws, "ids": [survivor_id, absorbed_id]},
        )
        survivor = await _entity_family(conn, ws, survivor_id)
        absorbed = await _entity_family(conn, ws, absorbed_id)
    if survivor is None or absorbed is None:
        raise NotFound("Seller not found")
    if survivor["merged_into_id"] is not None or absorbed["merged_into_id"] is not None:
        raise VersionConflict("Only unmerged seller entities can be merged", reason="seller_merge_invalid")
    if absorbed["has_children"]:
        if survivor["has_children"]:
            raise ValidationFailed(
                "Both seller entities already absorbed others; resolve the identity by technical review",
                details={"reason": "seller_merge_unresolvable"},
            )
        survivor_id, absorbed_id = absorbed_id, survivor_id  # the family root survives
    async with mapped_errors():
        await conn.execute(
            "update app.seller_entities set merged_into_id = %(survivor)s, merged_at = now(),"
            " merge_reason = %(reason)s, row_version = row_version + 1"
            " where workspace_id = %(ws)s and id = %(absorbed)s",
            {"ws": ws, "survivor": survivor_id, "absorbed": absorbed_id, "reason": reason},
        )
        rows = await _family_inquiries(conn, ws, [survivor_id])
    key = f"seller_entity:{survivor_id}"
    cancellable = {s.value for s in CANCELLABLE_STATES}
    to_cancel: set[UUID] = set()
    for row in rows:
        # Its seller entity can never reserve or dispatch again: cancel what was never transmitted
        # (including a re-queued inquiry whose only attempts provably never left).
        of_absorbed = row["seller_entity_id"] == absorbed_id
        if of_absorbed and row["never_transmitted"] and row["state"] in cancellable:
            to_cancel.add(row["id"])
    duplicates: list[UUID] = []
    conflict = False
    remaining = [r for r in rows if r["id"] not in to_cancel]
    for group in _vehicle_groups(remaining):
        if len(group) < 2:
            continue
        decision = reconcile_identity_merge([_existing(r, key) for r in group])
        by_id = {r["id"]: r for r in group}
        for inquiry_id in decision.cancel:
            item = by_id[inquiry_id]
            if (
                item["state"] in cancellable
                and item["send_attempted_at"] is None
                and not int(item["attempts"])
            ):
                to_cancel.add(inquiry_id)
        duplicates.extend(decision.transmitted_duplicates)
        conflict = conflict or decision.conflict
    cancelled = await cancel_untransmitted(
        conn, actor, to_cancel, reasons=[SELLER_ENTITY_MERGED, IDENTITY_MERGE_DUPLICATE]
    )
    await audit.record(
        conn,
        actor,
        "seller_entity.merge",
        "seller_entity",
        absorbed_id,
        reason=reason,
        metadata={
            "survivor_id": str(survivor_id),
            "cancelled": [str(i) for i in cancelled],
            "transmitted_duplicates": [str(i) for i in duplicates],
            "conflict": conflict,
        },
    )
    return MergeOutcome(
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        cancelled_inquiry_ids=cancelled,
        transmitted_duplicates=tuple(duplicates),
        conflict=conflict,
    )


async def merge_sellers(
    conn: Conn, actor: ActorContext, *, survivor_id: UUID, absorbed_id: UUID, reason: str
) -> MergeOutcome:
    """Merge two unmerged seller entities and reconcile their inquiries (see module doc).

    The caller's ``survivor_id`` is kept unless only ``absorbed_id`` already absorbed other
    entities (the database allows one merge level), in which case the roles swap.
    """
    require_inquiry_writer(actor)
    text = _reason_text(reason)
    await lock_controls(conn, actor.workspace_id)
    return await _merge(conn, actor, survivor_id, absorbed_id, text)


# =============================================================================================
# Linking
# =============================================================================================


async def link_seller(
    conn: Conn, actor: ActorContext, identity: SellerIdentity, *, reason: str = "evidenced seller aliases"
) -> LinkedSeller:
    """Create or find the ONE persisted seller entity of ``identity`` (merging when needed).

    Takes the inquiry lock order from its start (controls row first), so a merge triggered by a
    newly found alias can never deadlock with a concurrent reservation.
    """
    require_inquiry_writer(actor)
    text = _reason_text(reason)
    ws = actor.workspace_id
    await lock_controls(conn, ws)
    async with mapped_errors():
        sources = await _source_ids(conn, ws, (a.source_key for a in identity.aliases if a.source_key))
    missing = sorted({a.source_key for a in identity.aliases if a.source_key and a.source_key not in sources})
    if missing:
        raise ValidationFailed("an alias names a source that is not configured", details={"sources": missing})
    by_hash = {alias_key_hash(a): a for a in identity.aliases}
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "select a.alias_key_hash, coalesce(e.merged_into_id, e.id) as root, e2.created_at as root_created"
            " from app.seller_entity_aliases a"
            " join app.seller_entities e on e.workspace_id = a.workspace_id and e.id = a.seller_entity_id"
            " join app.seller_entities e2 on e2.workspace_id = e.workspace_id"
            "  and e2.id = coalesce(e.merged_into_id, e.id)"
            " where a.workspace_id = %(ws)s and a.unlinked_at is null and a.alias_key_hash = any(%(hashes)s)",
            {"ws": ws, "hashes": sorted(by_hash)},
        )
    roots: dict[UUID, datetime] = {r["root"]: r["root_created"] for r in rows}
    if identity.seller_entity_id is not None:
        given = await get_seller_entity(conn, actor, identity.seller_entity_id)
        root_id = given.merged_into_id or given.id
        roots.setdefault(root_id, given.created_at)
    created = False
    merges: list[MergeOutcome] = []
    if roots:
        target = min(roots, key=lambda r: (roots[r], str(r)))
        for other in sorted(set(roots) - {target}, key=str):
            outcome = await _merge(conn, actor, target, other, text)
            merges.append(outcome)
            target = outcome.survivor_id
    else:
        display = next((_clean_text(a.display_name, 200) for a in identity.aliases if a.display_name), None)
        async with mapped_errors():
            row = await fetch_one(
                conn,
                "insert into app.seller_entities (workspace_id, seller_type, display_name, evidence)"
                " values (%(ws)s, %(type)s, %(name)s, %(evidence)s) returning id",
                {
                    "ws": ws,
                    "type": identity.seller_type.value,
                    "name": display,
                    "evidence": Jsonb({"linked_from_aliases": len(identity.aliases)}),
                },
            )
        assert row is not None
        target = row["id"]
        created = True
        await audit.record(conn, actor, "seller_entity.create", "seller_entity", target, reason=text)
    present = {r["alias_key_hash"] for r in rows}
    added = 0
    for key_hash, alias in sorted(by_hash.items()):
        if key_hash in present:
            continue
        async with mapped_errors():
            inserted = await fetch_one(
                conn,
                "insert into app.seller_entity_aliases (workspace_id, seller_entity_id, source_id,"
                " alias_kind, reference, alias_key_hash, display_name, evidence_kind, evidence_excerpt,"
                " source_url, observed_at)"
                " values (%(ws)s, %(entity)s, %(source)s, %(kind)s, %(reference)s, %(hash)s, %(name)s,"
                " %(evidence)s, %(excerpt)s, %(url)s, %(observed)s)"
                " on conflict (workspace_id, alias_key_hash) where unlinked_at is null do nothing"
                " returning id",
                {
                    "ws": ws,
                    "entity": target,
                    "source": sources.get(alias.source_key) if alias.source_key else None,
                    "kind": alias.alias_kind,
                    "reference": alias.reference,
                    "hash": key_hash,
                    "name": _clean_text(alias.display_name, 200),
                    "evidence": alias.evidence_kind,
                    "excerpt": _clean_text(alias.evidence_excerpt, 500),
                    "url": _url_or_none(alias.source_url),
                    "observed": alias.observed_at,
                },
            )
        if inserted is not None:
            added += 1
            continue
        # Linked concurrently to another entity between the read and the insert: merge.
        async with mapped_errors():
            linked = await fetch_one(
                conn,
                "select coalesce(e.merged_into_id, e.id) as root from app.seller_entity_aliases a"
                " join app.seller_entities e on e.workspace_id = a.workspace_id and e.id = a.seller_entity_id"
                " where a.workspace_id = %(ws)s and a.alias_key_hash = %(hash)s and a.unlinked_at is null",
                {"ws": ws, "hash": key_hash},
            )
        if linked is not None and linked["root"] != target:
            outcome = await _merge(conn, actor, target, linked["root"], text)
            merges.append(outcome)
            target = outcome.survivor_id
    if identity.seller_type != SellerType.UNKNOWN:
        async with mapped_errors():
            await conn.execute(
                "update app.seller_entities set seller_type = %(type)s, row_version = row_version + 1"
                " where workspace_id = %(ws)s and id = %(id)s and seller_type = 'unknown'",
                {"ws": ws, "id": target, "type": identity.seller_type.value},
            )
    return LinkedSeller(entity_id=target, created=created, aliases_added=added, merges=tuple(merges))


# =============================================================================================
# Contact evidence
# =============================================================================================

_CONTACT_COLUMNS: Final = (
    "c.id, c.source_id, s.source_key, c.listing_id, c.listing_revision_id, c.listing_revision_number,"
    " c.seller_entity_id, c.address, c.address_domain, c.contact_kind, c.evidence_kind,"
    " c.relay_listing_reference, c.listing_reference, c.listing_url, c.evidence_url,"
    " c.extraction_location, c.extraction_excerpt, c.language_code, c.language_status, c.language_basis,"
    " c.language_confidence, c.language_evidence_excerpt, c.language_rules_version, c.status,"
    " c.status_reasons, c.rules_version, c.observed_at, c.verified_at, c.last_rechecked_at,"
    " c.changed_at, c.superseded_by_id, c.created_at"
)
_CONTACT_FROM: Final = (
    " from app.seller_contacts c join app.sources s on s.workspace_id = c.workspace_id and s.id = c.source_id"
)


def _contact(row: Mapping[str, Any]) -> SellerContactRecord:
    data = dict(row)
    data["status_reasons"] = tuple(data.get("status_reasons") or ())
    return SellerContactRecord.model_validate(data)


async def get_contact(conn: Conn, actor: ActorContext, contact_id: UUID) -> SellerContactRecord:
    require_inquiry_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            f"select {_CONTACT_COLUMNS}{_CONTACT_FROM} where c.workspace_id = %(ws)s and c.id = %(id)s",
            {"ws": actor.workspace_id, "id": contact_id},
        )
    if row is None:
        raise NotFound("Seller contact not found")
    return _contact(row)


async def current_contact(conn: Conn, actor: ActorContext, listing_id: UUID) -> SellerContactRecord | None:
    """The listing's verified contact, else its newest evidence row (``None`` = no evidence)."""
    require_inquiry_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            f"select {_CONTACT_COLUMNS}{_CONTACT_FROM}"
            " where c.workspace_id = %(ws)s and c.listing_id = %(listing)s and c.status <> 'changed'"
            " order by (c.status = 'verified') desc, c.created_at desc, c.id desc limit 1",
            {"ws": actor.workspace_id, "listing": listing_id},
        )
    return None if row is None else _contact(row)


def _confidence(value: Decimal) -> Decimal:
    return max(Decimal(0), min(Decimal(1), value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _status_for(
    decision: RecipientDecision, evidence: RecipientEvidence, address: str | None
) -> ContactStatus:
    if decision.status == RecipientStatus.VERIFIED:
        return "verified"
    if (
        decision.status == RecipientStatus.SELLER_EMAIL_UNAVAILABLE
        and address is None
        and evidence.kind in _UNAVAILABLE_KINDS
    ):
        return "unavailable"
    return "unverified"


def _material(
    row: SellerContactRecord, new: Mapping[str, Any]
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    return (
        row.address,
        row.contact_kind,
        row.evidence_kind.value,
        row.seller_entity_id,
        row.listing_reference,
        row.listing_url,
        row.evidence_url,
        row.extraction_location.value,
        row.relay_listing_reference,
        row.language_code,
        row.language_status.value,
        row.language_basis,
    ), (
        new["address"],
        new["contact_kind"],
        new["evidence_kind"],
        new["seller"],
        new["reference"],
        new["url"],
        new["evidence_url"],
        new["location"],
        new["relay"],
        new["language_code"],
        new["language_status"],
        new["language_basis"],
    )


async def record_contact(
    conn: Conn,
    actor: ActorContext,
    *,
    evidence: RecipientEvidence,
    decision: RecipientDecision,
    language: LanguageDecision | None,
    seller_entity_id: UUID,
) -> SellerContactRecord:
    """Store recipient + language evidence for exactly one listing and its linked seller.

    ``decision`` must be ``verify_recipient(evidence, ...)`` evaluated with the seller identity
    bound to the persisted entity (``LinkedSeller.identity``): a verified binding names
    ``seller_entity:<root>``. The current verified contact of the listing is superseded on any
    material change; an identical re-verification within ``RECIPIENT_EVIDENCE_MAX_AGE`` only
    records ``last_rechecked_at``.
    """
    require_inquiry_writer(actor)
    ws = actor.workspace_id
    root = await seller_root(conn, actor, seller_entity_id)
    if evidence.listing_incarnation_id not in (None, evidence.listing_id):
        raise ValidationFailed("one listing row is one incarnation: use the listing id as incarnation id")
    if decision.binding is not None and (
        decision.binding.seller_identity_key != f"seller_entity:{root}"
        or decision.binding.listing_id != evidence.listing_id
    ):
        raise ValidationFailed(
            "verify the recipient against the linked seller entity of this listing",
            details={"problems": ["RECIPIENT_SELLER_NOT_LINKED"]},
        )
    async with mapped_errors():
        listing = await fetch_one(
            conn,
            "select l.source_id, s.source_key from app.listings l"
            " join app.sources s on s.workspace_id = l.workspace_id and s.id = l.source_id"
            " where l.workspace_id = %(ws)s and l.id = %(id)s",
            {"ws": ws, "id": evidence.listing_id},
        )
    if listing is None:
        raise NotFound("Listing not found")
    if listing["source_key"] != evidence.source_key:
        raise ValidationFailed("the evidence names another source than the listing's")
    address: str | None = None
    if decision.binding is not None:
        address = decision.binding.canonical_address
    elif evidence.address and evidence.kind not in _UNAVAILABLE_KINDS:
        try:
            address = canonicalize_address(evidence.address).canonical
        except AddressError:
            address = None
    status = _status_for(decision, evidence, address)
    reasons = [f"{_STATUS_PREFIX}{decision.status.value}", *(r.value for r in decision.reasons)]
    lang_code: str | None = None
    lang_status = LanguageStatus.LANGUAGE_UNRESOLVED
    lang_basis: str = "none"
    lang_conf = Decimal(0)
    lang_excerpt: str | None = None
    lang_rules: str | None = None
    if language is not None:
        lang_status = language.status
        lang_code = language.language.value if language.language else language.detected_language
        if lang_code is not None and not re.fullmatch(r"[a-z]{2}", lang_code):
            lang_code = None
        if lang_status == LanguageStatus.UNSUPPORTED_LANGUAGE and (
            lang_code is None or lang_code in {m.value for m in MessageLanguage}
        ):
            lang_status = LanguageStatus.LANGUAGE_UNRESOLVED
        lang_basis = language.basis
        lang_conf = _confidence(language.confidence)
        lang_excerpt = _clean_text(language.evidence_excerpt, 500)
        lang_rules = _clean_text(language.rules_version, 80)
        reasons.append(f"{_LANGUAGE_REASON_PREFIX}{language.reason.value}")
    contact_kind = _CONTACT_KIND.get(evidence.kind)
    if contact_kind == "marketplace_relay" and not evidence.relay_listing_reference:
        raise ValidationFailed("relay evidence is recorded only with the listing reference it is bound to")
    new: dict[str, Any] = {
        "ws": ws,
        "source": listing["source_id"],
        "listing": evidence.listing_id,
        "revision": evidence.listing_revision_id,
        "revision_number": evidence.listing_revision_number,
        "seller": root,
        "address": address,
        "contact_kind": contact_kind,
        "evidence_kind": evidence.kind.value,
        "relay": _clean_text(evidence.relay_listing_reference, 200),
        "reference": _clean_text(evidence.listing_reference, 200),
        "url": evidence.listing_url,
        "evidence_url": _url_or_none(evidence.evidence_url),
        "location": evidence.extraction_location.value,
        "excerpt": decision.evidence_excerpt,
        "language_code": lang_code,
        "language_status": lang_status.value,
        "language_basis": lang_basis,
        "language_confidence": lang_conf,
        "language_excerpt": lang_excerpt,
        "language_rules": lang_rules,
        "status": "unavailable" if status == "unavailable" else "unverified",
        "reasons": [r[:80] for r in reasons][:30],
        "rules": decision.rules_version,
        "observed": evidence.observed_at,
    }
    current = await current_contact(conn, actor, evidence.listing_id)
    if current is not None and current.status != "verified":
        current = None
    if status == "verified" and current is not None:
        old, fresh = _material(current, new)
        if (
            old == fresh
            and current.verified_at is not None
            and current.verified_at
            <= evidence.verified_at
            <= current.verified_at + RECIPIENT_EVIDENCE_MAX_AGE
        ):
            async with mapped_errors():
                await conn.execute(
                    "update app.seller_contacts set last_rechecked_at = greatest("
                    " coalesce(last_rechecked_at, %(at)s), %(at)s)"
                    " where workspace_id = %(ws)s and id = %(id)s",
                    {"ws": ws, "id": current.id, "at": evidence.verified_at},
                )
            return await get_contact(conn, actor, current.id)
    supersede = current is not None and (
        status == "verified"
        or decision.status
        in (
            RecipientStatus.REJECTED,
            RecipientStatus.SELLER_EMAIL_UNAVAILABLE,
            RecipientStatus.NEEDS_TECHNICAL_REVIEW,
        )
    )
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into app.seller_contacts (workspace_id, source_id, listing_id, listing_revision_id,"
            " listing_revision_number, seller_entity_id, address, contact_kind, evidence_kind,"
            " relay_listing_reference, listing_reference, listing_url, evidence_url, extraction_location,"
            " extraction_excerpt, language_code, language_status, language_basis, language_confidence,"
            " language_evidence_excerpt, language_rules_version, status, status_reasons, rules_version,"
            " observed_at)"
            " values (%(ws)s, %(source)s, %(listing)s, %(revision)s, %(revision_number)s, %(seller)s,"
            " %(address)s, %(contact_kind)s, %(evidence_kind)s, %(relay)s, %(reference)s, %(url)s,"
            " %(evidence_url)s, %(location)s, %(excerpt)s, %(language_code)s, %(language_status)s,"
            " %(language_basis)s, %(language_confidence)s, %(language_excerpt)s, %(language_rules)s,"
            " %(status)s, %(reasons)s, %(rules)s, %(observed)s)"
            " returning id",
            new,
        )
        assert row is not None
        contact_id: UUID = row["id"]
        if supersede and current is not None:
            await conn.execute(
                "update app.seller_contacts set status = 'changed', changed_at = now(),"
                " superseded_by_id = %(new)s where workspace_id = %(ws)s and id = %(id)s",
                {"ws": ws, "id": current.id, "new": contact_id},
            )
        if status == "verified":
            await conn.execute(
                "update app.seller_contacts set status = 'verified', verified_at = %(at)s"
                " where workspace_id = %(ws)s and id = %(id)s",
                {"ws": ws, "id": contact_id, "at": evidence.verified_at},
            )
    await audit.record(
        conn,
        actor,
        "seller_contact.record",
        "seller_contact",
        contact_id,
        reason=f"recipient {decision.status.value}",
        metadata={
            "listing_id": str(evidence.listing_id),
            "evidence_kind": evidence.kind.value,
            "status": status,
            "superseded": str(current.id) if supersede and current is not None else None,
        },
    )
    return await get_contact(conn, actor, contact_id)


# =============================================================================================
# Domain decisions rebuilt from stored evidence
# =============================================================================================


def recipient_binding(contact: SellerContactRecord) -> RecipientBinding:
    """The canonical ``RecipientBinding`` of a verified contact row (bound at reservation)."""
    if contact.address is None or contact.address_domain is None or contact.verified_at is None:
        raise ValidationFailed("only a verified contact with an address has a recipient binding")
    return RecipientBinding(
        canonical_address=contact.address,
        address_domain=contact.address_domain,
        evidence_kind=contact.evidence_kind,
        listing_id=contact.listing_id,
        listing_incarnation_id=contact.listing_id,
        listing_revision_id=contact.listing_revision_id,
        listing_revision_number=contact.listing_revision_number,
        source_key=contact.source_key,
        listing_reference=contact.listing_reference,
        listing_url=contact.listing_url,
        evidence_url=contact.evidence_url,
        extraction_location=contact.extraction_location,
        seller_identity_key=contact.seller_key,
        verified_at=contact.verified_at,
        rules_version=contact.rules_version,
    )


def _recipient_reasons(contact: SellerContactRecord) -> tuple[RecipientReason, ...]:
    values = {r.value for r in RecipientReason}
    return tuple(RecipientReason(r) for r in contact.status_reasons if r in values)


def recipient_decision(contact: SellerContactRecord | None) -> RecipientDecision | None:
    """The recipient decision the stored evidence supports (``None`` = no evidence yet)."""
    if contact is None:
        return None
    reasons = _recipient_reasons(contact)
    if contact.status == "verified":
        return RecipientDecision(
            status=RecipientStatus.VERIFIED,
            reasons=reasons or (RecipientReason.VERIFIED_EMAIL_ON_ADVERTISEMENT,),
            binding=recipient_binding(contact),
            evidence_excerpt=contact.extraction_excerpt,
            rules_version=contact.rules_version,
        )
    stored = next(
        (r.removeprefix(_STATUS_PREFIX) for r in contact.status_reasons if r.startswith(_STATUS_PREFIX)),
        None,
    )
    if contact.status == "unavailable" or stored == RecipientStatus.SELLER_EMAIL_UNAVAILABLE.value:
        status = RecipientStatus.SELLER_EMAIL_UNAVAILABLE
        reasons = reasons or (RecipientReason.NO_EMAIL_FOUND,)
    elif stored in {s.value for s in RecipientStatus} and stored != RecipientStatus.VERIFIED.value:
        status = RecipientStatus(stored)
    elif contact.status == "changed" or contact.evidence_kind in ACCEPTABLE_EVIDENCE_KINDS:
        status = RecipientStatus.NEEDS_TECHNICAL_REVIEW
    else:
        status = RecipientStatus.REJECTED
    return RecipientDecision(
        status=status,
        reasons=reasons,
        evidence_excerpt=contact.extraction_excerpt,
        rules_version=contact.rules_version,
    )


_LANGUAGE_REASON_FALLBACK: Final[dict[tuple[str, str], LanguageReason]] = {
    ("resolved", "verified_seller_preference"): LanguageReason.VERIFIED_SELLER_PREFERENCE,
    ("resolved", "seller_ad_text"): LanguageReason.SELLER_AD_TEXT,
    ("unsupported_language", "verified_seller_preference"): LanguageReason.VERIFIED_PREFERENCE_UNSUPPORTED,
    ("unsupported_language", "seller_ad_text"): LanguageReason.AD_TEXT_UNSUPPORTED_LANGUAGE,
    ("unsupported_language", "none"): LanguageReason.AD_TEXT_UNSUPPORTED_LANGUAGE,
}


def language_decision(contact: SellerContactRecord | None) -> LanguageDecision | None:
    """The language decision stored with the contact (``None`` = never evaluated)."""
    if contact is None:
        return None
    stored = next(
        (
            r.removeprefix(_LANGUAGE_REASON_PREFIX)
            for r in contact.status_reasons
            if r.startswith(_LANGUAGE_REASON_PREFIX)
        ),
        None,
    )
    if (
        stored is None
        and contact.language_status == LanguageStatus.LANGUAGE_UNRESOLVED
        and (contact.language_code is None and contact.language_basis == "none")
    ):
        return None  # no language evaluation was recorded for this evidence
    reason = (
        LanguageReason(stored)
        if stored in {r.value for r in LanguageReason}
        else _LANGUAGE_REASON_FALLBACK.get(
            (contact.language_status.value, contact.language_basis), LanguageReason.INSUFFICIENT_TEXT_EVIDENCE
        )
    )
    resolved = contact.language_status == LanguageStatus.RESOLVED
    return LanguageDecision(
        language=MessageLanguage(contact.language_code) if resolved and contact.language_code else None,
        status=contact.language_status,
        confidence=contact.language_confidence,
        evidence_excerpt=contact.language_evidence_excerpt,
        reason=reason,
        basis=contact.language_basis,
        detected_language=contact.language_code,
        rules_version=contact.language_rules_version or LANGUAGE_RULES_VERSION,
    )


__all__ = [
    "CANCELLABLE_STATES",
    "CONTACT_RULES_VERSION",
    "IDENTITY_MERGE_DUPLICATE",
    "NEVER_TRANSMITTED_SQL",
    "SELLER_ENTITY_MERGED",
    "LinkedSeller",
    "MergeOutcome",
    "SellerAliasRecord",
    "SellerContactRecord",
    "SellerEntityRecord",
    "alias_key_hash",
    "cancel_untransmitted",
    "current_contact",
    "get_contact",
    "get_seller_entity",
    "language_decision",
    "link_seller",
    "list_aliases",
    "lock_controls",
    "merge_sellers",
    "recipient_binding",
    "recipient_decision",
    "record_contact",
    "require_inquiry_reader",
    "require_inquiry_writer",
    "seller_root",
]
