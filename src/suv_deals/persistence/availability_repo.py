"""Availability evidence across sites: ``app.availability_events`` (spec 37.9).

Every event is derived by ``domain.lifecycle.derive_availability_event`` from the listing's
current lifecycle state (`listing_lifecycle_state`: the listing's canonical availability plus the
evidence kind and effective time of its newest promoted event) and one signal, so the spec 37.9
rules hold in one place:

- canonical ``listings.availability`` values only; the evidence kind fixes the value (a seller's
  "sold" is ``sold_claimed`` with ``seller_reported_sold``, never ``removed``, never a purchase,
  a buyer or a transaction price; one missing search result is never a sale);
- older evidence is kept as history (``historical_only``) and never regresses the current status;
- a contradiction with a seller's "sold" (or a seller's "available" after a site's sold badge) is
  preserved (``conflicts_with_current``) without overriding anything, and outreach stops
  (``SuppressionReason.CONTRADICTORY_AVAILABILITY``; the caller records the suppression).

Only a promoted event (``promote_current``) changes ``app.listings.availability``; it is also
recorded as the ``listing.availability`` audit event the candidate history reads. The database
re-checks the evidence (a seller statement needs a non-quarantined seller reply about this
vehicle; an absence needs a finished complete scan of the listing's source).

Writes are server-side processing: system principals or the owner (``config:admin``). Reads need
``deals:read`` or ``inquiries:read``. Lock order: the listing row is locked (``FOR UPDATE``) before
the event is written; callers that also hold an inquiry lock take it first.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Availability, AvailabilityEvidenceKind, Scope
from suv_deals.domain.lifecycle import (
    AvailabilityDecision,
    AvailabilityEventDraft,
    AvailabilitySignal,
    AvailabilitySignalKind,
    SourceListingLifecycle,
    derive_availability_event,
)
from suv_deals.errors import Forbidden, NotFound, ValidationFailed
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.listings_repo import AuditAvailabilitySink, AvailabilityTransition

_FROZEN = ConfigDict(frozen=True, extra="forbid")
MAX_EVENTS: Final = 500
SELLER_STATUSES: Final = frozenset({Availability.SOLD_CLAIMED, Availability.AVAILABLE, Availability.RESERVED})


class AvailabilityEventRecord(BaseModel):
    """One stored ``app.availability_events`` row (never a purchase, buyer or price)."""

    model_config = _FROZEN

    id: UUID
    listing_id: UUID
    source_id: UUID
    vehicle_cluster_id: UUID | None
    old_availability: Availability
    new_availability: Availability
    evidence_kind: AvailabilityEvidenceKind
    reason: str
    reply_id: UUID | None
    effective_at: datetime
    observed_at: datetime
    confidence: Literal["high", "medium", "low"]
    promote_current: bool
    historical_only: bool
    conflicts_with_current: bool
    created_at: datetime


class AvailabilityResult(BaseModel):
    """The outcome of one signal: the stored event (if any) and whether the listing changed."""

    model_config = _FROZEN

    event: AvailabilityEventRecord | None
    no_event_reason: str | None
    listing_updated: bool

    @property
    def conflict(self) -> bool:
        return self.event is not None and self.event.conflicts_with_current


def _require_writer(actor: ActorContext) -> None:
    if actor.principal_kind != "system" and not actor.has(Scope.CONFIG_ADMIN):
        raise Forbidden("Only system workers or the owner record availability evidence")


def _require_reader(actor: ActorContext) -> None:
    if actor.principal_kind != "system" and not (
        actor.has(Scope.DEALS_READ) or actor.has(Scope.INQUIRIES_READ)
    ):
        raise Forbidden("Missing scope: deals:read or inquiries:read")


_STATE_SQL: Final = """
select l.id, l.source_id, l.availability, l.first_seen_at, l.last_seen_at, l.last_detail_success_at,
       l.is_fixture, s.source_key,
       (select m.cluster_id from app.vehicle_cluster_members m
          join app.vehicle_clusters c on c.workspace_id = m.workspace_id and c.id = m.cluster_id
         where m.workspace_id = l.workspace_id and m.listing_id = l.id and m.unlinked_at is null
           and c.review_status = 'confirmed'
         order by m.created_at desc limit 1) as cluster_id,
       e.evidence_kind as event_kind, e.reason as event_reason, e.effective_at as event_effective_at
  from app.listings l
  join app.sources s on s.workspace_id = l.workspace_id and s.id = l.source_id
  left join lateral (
    select ae.evidence_kind, ae.reason, ae.effective_at
      from app.availability_events ae
     where ae.workspace_id = l.workspace_id and ae.listing_id = l.id and ae.promote_current
     order by ae.effective_at desc, ae.created_at desc, ae.id desc
     limit 1) e on true
 where l.workspace_id = %(ws)s and l.id = %(id)s
"""


def _state_from_row(row: Mapping[str, Any]) -> SourceListingLifecycle:
    current = Availability(row["availability"])
    kind = None if row["event_kind"] is None else AvailabilityEvidenceKind(row["event_kind"])
    effective = None if row["event_effective_at"] is None else ensure_utc(row["event_effective_at"])
    first_seen = ensure_utc(row["first_seen_at"])
    detail = (
        None
        if row["last_detail_success_at"] is None
        else max(first_seen, ensure_utc(row["last_detail_success_at"]))
    )
    return SourceListingLifecycle(
        listing_id=row["id"],
        source_key=row["source_key"],
        vehicle_cluster_id=row["cluster_id"],
        first_seen_at=first_seen,
        last_seen_on_search_at=max(first_seen, ensure_utc(row["last_seen_at"])),
        last_detail_success_at=detail,
        availability=current,
        # The newest promoted event is the evidence of the current status only while it still
        # names that status (older code paths changed the listing without an event row).
        availability_evidence_kind=kind if kind is not None and _kind_yields(kind, current) else None,
        availability_reason=None if row["event_reason"] is None else str(row["event_reason"])[:80],
        availability_effective_at=effective if kind is not None and _kind_yields(kind, current) else None,
        is_fixture=bool(row["is_fixture"]),
    )


_KIND_VALUES: Final[Mapping[AvailabilityEvidenceKind, frozenset[Availability]]] = {
    AvailabilityEvidenceKind.SOURCE_OBSERVATION: frozenset(
        {Availability.AVAILABLE, Availability.RESERVED, Availability.UNKNOWN}
    ),
    AvailabilityEvidenceKind.SOURCE_SOLD_BADGE: frozenset({Availability.SOLD_CLAIMED}),
    AvailabilityEvidenceKind.SOURCE_REMOVED_PAGE: frozenset({Availability.REMOVED}),
    AvailabilityEvidenceKind.SOURCE_RESERVED_BADGE: frozenset({Availability.RESERVED}),
    AvailabilityEvidenceKind.SOURCE_DETAIL_NOT_FOUND: frozenset({Availability.UNKNOWN}),
    AvailabilityEvidenceKind.SELLER_REPORTED_SOLD: frozenset({Availability.SOLD_CLAIMED}),
    AvailabilityEvidenceKind.SELLER_REPORTED_AVAILABLE: frozenset({Availability.AVAILABLE}),
    AvailabilityEvidenceKind.SELLER_REPORTED_RESERVED: frozenset({Availability.RESERVED}),
    AvailabilityEvidenceKind.COMPLETE_SCAN_ABSENCE: frozenset({Availability.UNKNOWN}),
    AvailabilityEvidenceKind.MANUAL: frozenset(Availability),
}


def _kind_yields(kind: AvailabilityEvidenceKind, value: Availability) -> bool:
    return value in _KIND_VALUES.get(kind, frozenset())


async def listing_lifecycle_state(
    conn: Conn, actor: ActorContext, listing_id: UUID, *, lock: bool = False
) -> SourceListingLifecycle:
    """The listing's lifecycle state for the domain rules (``lock``: ``FOR UPDATE`` the listing)."""
    _require_reader(actor)
    params = {"ws": actor.workspace_id, "id": listing_id}
    async with mapped_errors():
        if lock:
            locked = await fetch_one(
                conn,
                "select id from app.listings where workspace_id = %(ws)s and id = %(id)s for update",
                params,
            )
            if locked is None:
                raise NotFound("Listing not found")
        row = await fetch_one(conn, _STATE_SQL, params)
    if row is None:
        raise NotFound("Listing not found")
    return _state_from_row(row)


_INSERT_SQL: Final = """
insert into app.availability_events (workspace_id, source_id, listing_id, vehicle_cluster_id,
  old_availability, new_availability, evidence_kind, reason, crawl_run_id, reply_id, manual_principal_id,
  source_reference, effective_at, observed_at, confidence, promote_current, historical_only,
  conflicts_with_current)
values (%(ws)s, %(source)s, %(listing)s, %(cluster)s, %(old)s, %(new)s, %(kind)s, %(reason)s, %(run)s,
        %(reply)s, %(manual)s, %(reference)s, %(effective)s, %(observed)s, %(confidence)s, %(promote)s,
        %(historical)s, %(conflict)s)
returning id, listing_id, source_id, vehicle_cluster_id, old_availability, new_availability, evidence_kind,
          reason, reply_id, effective_at, observed_at, confidence, promote_current, historical_only,
          conflicts_with_current, created_at
"""


def _record(row: Mapping[str, Any]) -> AvailabilityEventRecord:
    return AvailabilityEventRecord(
        id=row["id"],
        listing_id=row["listing_id"],
        source_id=row["source_id"],
        vehicle_cluster_id=row["vehicle_cluster_id"],
        old_availability=Availability(row["old_availability"]),
        new_availability=Availability(row["new_availability"]),
        evidence_kind=AvailabilityEvidenceKind(row["evidence_kind"]),
        reason=row["reason"],
        reply_id=row["reply_id"],
        effective_at=ensure_utc(row["effective_at"]),
        observed_at=ensure_utc(row["observed_at"]),
        confidence=row["confidence"],
        promote_current=row["promote_current"],
        historical_only=row["historical_only"],
        conflicts_with_current=row["conflicts_with_current"],
        created_at=ensure_utc(row["created_at"]),
    )


async def apply_signal(
    conn: Conn,
    actor: ActorContext,
    listing_id: UUID,
    signal: AvailabilitySignal,
    *,
    vehicle_cluster_id: UUID | None = None,
    crawl_run_id: UUID | None = None,
) -> AvailabilityResult:
    """Derive and store the event of one signal; promote it onto the listing when the rules say so.

    The listing row is locked first. ``vehicle_cluster_id`` (an inquiry's cluster) is recorded on
    the event; it defaults to the listing's confirmed cluster.
    """
    _require_writer(actor)
    state = await listing_lifecycle_state(conn, actor, listing_id, lock=True)
    decision: AvailabilityDecision = derive_availability_event(state, signal)
    if decision.event is None:
        return AvailabilityResult(event=None, no_event_reason=decision.no_event_reason, listing_updated=False)
    draft: AvailabilityEventDraft = decision.event
    async with mapped_errors():
        source = await fetch_one(
            conn,
            "select source_id from app.listings where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": listing_id},
        )
        assert source is not None
        row = await fetch_one(
            conn,
            _INSERT_SQL,
            {
                "ws": actor.workspace_id,
                "source": source["source_id"],
                "listing": listing_id,
                "cluster": vehicle_cluster_id or state.vehicle_cluster_id,
                "old": draft.old_status.value,
                "new": draft.new_status.value,
                "kind": draft.evidence_kind.value,
                "reason": draft.reason,
                "run": crawl_run_id,
                "reply": draft.reply_id,
                "manual": actor.principal_id
                if draft.evidence_kind == AvailabilityEvidenceKind.MANUAL
                else None,
                "reference": draft.source_reference,
                "effective": draft.effective_at,
                "observed": draft.observed_at,
                "confidence": draft.confidence,
                "promote": draft.promote_current,
                "historical": draft.historical_only,
                "conflict": draft.conflicts_with_current,
            },
        )
    assert row is not None
    record = _record(row)
    updated = False
    if draft.promote_current and draft.new_status != state.availability:
        async with mapped_errors():
            await conn.execute(
                "update app.listings set availability = %(new)s, row_version = row_version + 1"
                " where workspace_id = %(ws)s and id = %(id)s",
                {"ws": actor.workspace_id, "id": listing_id, "new": draft.new_status.value},
            )
            await AuditAvailabilitySink().record(
                conn,
                actor,
                AvailabilityTransition(
                    listing_id=listing_id,
                    source_id=source["source_id"],
                    previous=state.availability,
                    new=draft.new_status,
                    reason=draft.reason,
                    evidence_kind=draft.evidence_kind,
                    observed_at=draft.observed_at,
                ),
            )
        updated = True
    return AvailabilityResult(event=record, no_event_reason=None, listing_updated=updated)


async def record_seller_statement(
    conn: Conn,
    actor: ActorContext,
    *,
    listing_id: UUID,
    reply_id: UUID,
    status: Availability,
    stated_at: datetime,
    observed_at: datetime,
    vehicle_cluster_id: UUID | None = None,
) -> AvailabilityResult:
    """A seller's availability statement from a correlated, non-quarantined reply.

    ``sold_claimed`` is recorded with ``seller_reported_sold`` (never ``removed``, never a sale
    price or a buyer); ``available``/``reserved`` likewise. The statement's time is the reply's
    received time; an older statement than the current evidence is history only.
    """
    if status not in SELLER_STATUSES:
        raise ValidationFailed("a seller statement is available, reserved or sold_claimed")
    observed = ensure_utc(observed_at)
    stated = min(ensure_utc(stated_at), observed)
    signal = AvailabilitySignal(
        kind=AvailabilitySignalKind.SELLER_STATEMENT,
        observed_at=observed,
        effective_at=stated,
        seller_status=status,
        reply_id=reply_id,
    )
    return await apply_signal(conn, actor, listing_id, signal, vehicle_cluster_id=vehicle_cluster_id)


async def list_listing_events(
    conn: Conn, actor: ActorContext, listing_id: UUID, *, limit: int = 100
) -> list[AvailabilityEventRecord]:
    """The listing's availability history, newest first (each source's evidence stays separate)."""
    _require_reader(actor)
    if not 1 <= limit <= MAX_EVENTS:
        raise ValidationFailed("limit must be between 1 and 500")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "select id, listing_id, source_id, vehicle_cluster_id, old_availability, new_availability,"
            " evidence_kind, reason, reply_id, effective_at, observed_at, confidence, promote_current,"
            " historical_only, conflicts_with_current, created_at from app.availability_events"
            " where workspace_id = %(ws)s and listing_id = %(id)s"
            " order by effective_at desc, created_at desc, id desc limit %(limit)s",
            {"ws": actor.workspace_id, "id": listing_id, "limit": limit},
        )
    return [_record(r) for r in rows]


async def list_reply_events(conn: Conn, actor: ActorContext, reply_id: UUID) -> list[AvailabilityEventRecord]:
    """Events a seller reply produced (normally at most one)."""
    _require_reader(actor)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "select id, listing_id, source_id, vehicle_cluster_id, old_availability, new_availability,"
            " evidence_kind, reason, reply_id, effective_at, observed_at, confidence, promote_current,"
            " historical_only, conflicts_with_current, created_at from app.availability_events"
            " where workspace_id = %(ws)s and reply_id = %(reply)s order by created_at, id",
            {"ws": actor.workspace_id, "reply": reply_id},
        )
    return [_record(r) for r in rows]


__all__ = [
    "SELLER_STATUSES",
    "AvailabilityEventRecord",
    "AvailabilityResult",
    "apply_signal",
    "list_listing_events",
    "list_reply_events",
    "listing_lifecycle_state",
    "record_seller_statement",
]
