"""``possible_same_vehicle`` clusters across sources (spec 10, 37.9).

- `suggest_possible_same_vehicle` stores a `domain.identity.SameVehicleSuggestion` as a cluster
  with two members. It never merges, deletes or rewrites listings, never merges two existing
  clusters (that pair is reported for human review instead) and never re-links or re-suggests a
  pair that a reviewer unlinked as a false positive or whose cluster was rejected. The match
  basis holds signal codes and scores only: no plate numbers and no personal contact data.
- `confirm_member` / `unlink_member` / `review_cluster` are the manual review (signed-in owner or
  reviewer, ``reviews:write``); every change is audited and bumps the cluster ``row_version``.
  An unlinked member row is kept (who/when/why) so the false-positive decision stays visible.
- `cluster_lifecycle` derives the earliest observed appearance and the latest source presence of
  the vehicle while keeping each source listing's own evidence (spec 37.9).

Lock order: suggestions lock both listing rows (``app.listings``, in ``(source_id,
source_listing_id, incarnation)`` byte order -- within one source the order search ingestion and the
complete-scan absence pass use) and then the cluster; reviews lock the cluster row and then its
member row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, field_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Availability, Confidence, Role, Scope
from suv_deals.domain.identity import SameVehicleSuggestion
from suv_deals.errors import Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.observability.logging import redact
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors

_CLUSTER_COLUMNS: Final = (
    "id",
    "workspace_id",
    "confidence",
    "review_status",
    "match_basis",
    "reviewed_by",
    "reviewed_at",
    "row_version",
    "created_at",
    "updated_at",
)
_MEMBER_COLUMNS: Final = (
    "id",
    "workspace_id",
    "cluster_id",
    "listing_id",
    "evidence",
    "confidence",
    "manually_confirmed",
    "confirmed_by",
    "confirmed_at",
    "linked_at",
    "unlinked_at",
    "unlinked_by",
    "unlink_reason",
    "created_at",
    "updated_at",
)


def _cols(names: tuple[str, ...]) -> sql.Composable:
    return sql.SQL(", ").join(sql.Identifier(c) for c in names)


_CLUSTER_SQL: Final = sql.SQL(
    "select {columns} from app.vehicle_clusters where workspace_id = %(workspace_id)s and id = %(id)s"
).format(columns=_cols(_CLUSTER_COLUMNS))
_LOCK_CLUSTER_SQL: Final = sql.SQL(
    "select {columns} from app.vehicle_clusters where workspace_id = %(workspace_id)s and id = %(id)s"
    " for update"
).format(columns=_cols(_CLUSTER_COLUMNS))
_MEMBERS_SQL: Final = sql.SQL(
    "select {columns} from app.vehicle_cluster_members where workspace_id = %(workspace_id)s"
    " and cluster_id = %(cluster_id)s order by linked_at, id"
).format(columns=_cols(_MEMBER_COLUMNS))


class ClusterRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    confidence: Confidence
    review_status: Literal["unreviewed", "confirmed", "rejected"]
    match_basis: dict[str, Any]
    reviewed_by: UUID | None = None
    reviewed_at: datetime | None = None
    row_version: int
    created_at: datetime
    updated_at: datetime

    @field_validator("reviewed_at", "created_at", "updated_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


class ClusterMemberRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    cluster_id: UUID
    listing_id: UUID
    evidence: dict[str, Any]
    confidence: Confidence
    manually_confirmed: bool
    confirmed_by: UUID | None = None
    confirmed_at: datetime | None = None
    linked_at: datetime
    unlinked_at: datetime | None = None
    unlinked_by: UUID | None = None
    unlink_reason: str | None = None

    @field_validator("confirmed_at", "linked_at", "unlinked_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @property
    def active(self) -> bool:
        return self.unlinked_at is None


class ClusterView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cluster: ClusterRecord
    members: tuple[ClusterMemberRecord, ...]


SuggestionOutcome = Literal[
    "created_cluster",
    "added_member",
    "already_clustered",
    "not_suggested",
    "previously_unlinked",
    "both_clustered",
]


class SuggestionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: SuggestionOutcome
    cluster_id: UUID | None = None
    member_ids: tuple[UUID, ...] = ()


class SourcePresence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    listing_id: UUID
    source_id: UUID
    source_key: str
    first_seen_at: datetime
    last_seen_at: datetime
    last_detail_success_at: datetime | None
    availability: Availability


class ClusterLifecycle(BaseModel):
    """Vehicle-level appearance derived from independent per-source evidence (spec 37.9).

    ``earliest_observed_at`` is when THIS system first saw any member; it is not a publication
    date, so it never proves that the vehicle is "new today".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cluster_id: UUID
    earliest_observed_at: datetime | None
    latest_source_presence_at: datetime | None
    sources: tuple[SourcePresence, ...]


def _require_system(actor: ActorContext) -> None:
    if actor.principal_kind != "system" and not actor.has(Scope.CONFIG_ADMIN):
        raise Forbidden("Only the matching pipeline or the owner may suggest clusters")


def _require_reviewer(actor: ActorContext) -> None:
    actor.require(Scope.REVIEWS_WRITE)
    if actor.principal_kind != "user" or actor.role not in (Role.OWNER, Role.REVIEWER):
        raise Forbidden("Only a signed-in owner or reviewer may review vehicle clusters")


def _reason(reason: str) -> str:
    cleaned = redact(reason or "").strip()[:500]
    if len(cleaned) < 3:
        raise ValidationFailed("reason must be 3-500 characters")
    return cleaned


def _basis(suggestion: SameVehicleSuggestion) -> dict[str, Any]:
    return {
        "kind": "possible_same_vehicle",
        "score": str(suggestion.score),
        "signals": [
            {"code": s.code, "strength": s.strength, "weight": str(s.weight)} for s in suggestion.signals
        ],
    }


async def _active_clusters(conn: Conn, workspace_id: UUID, listing_id: UUID) -> list[UUID]:
    rows = await fetch_all(
        conn,
        "select m.cluster_id from app.vehicle_cluster_members m"
        " join app.vehicle_clusters c on c.workspace_id = m.workspace_id and c.id = m.cluster_id"
        " where m.workspace_id = %(workspace_id)s and m.listing_id = %(listing_id)s"
        " and m.unlinked_at is null and c.review_status <> 'rejected' order by m.cluster_id",
        {"workspace_id": workspace_id, "listing_id": listing_id},
    )
    return [r["cluster_id"] for r in rows]


async def _was_unlinked(conn: Conn, workspace_id: UUID, cluster_id: UUID, listing_id: UUID) -> bool:
    row = await fetch_one(
        conn,
        "select 1 from app.vehicle_cluster_members where workspace_id = %(workspace_id)s"
        " and cluster_id = %(cluster_id)s and listing_id = %(listing_id)s and unlinked_at is not null"
        " limit 1",
        {"workspace_id": workspace_id, "cluster_id": cluster_id, "listing_id": listing_id},
    )
    return row is not None


async def _rejected_pair(conn: Conn, workspace_id: UUID, listing_a: UUID, listing_b: UUID) -> UUID | None:
    """A cluster that held both listings and was rejected, or from which one of them was unlinked."""
    row = await fetch_one(
        conn,
        "select c.id from app.vehicle_clusters c"
        " join app.vehicle_cluster_members a on a.workspace_id = c.workspace_id and a.cluster_id = c.id"
        "  and a.listing_id = %(a)s"
        " join app.vehicle_cluster_members b on b.workspace_id = c.workspace_id and b.cluster_id = c.id"
        "  and b.listing_id = %(b)s"
        " where c.workspace_id = %(workspace_id)s"
        " and (c.review_status = 'rejected' or a.unlinked_at is not null or b.unlinked_at is not null)"
        " order by c.created_at, c.id limit 1",
        {"workspace_id": workspace_id, "a": listing_a, "b": listing_b},
    )
    return None if row is None else row["id"]


async def _add_member(  # noqa: PLR0917 - private helper
    conn: Conn,
    workspace_id: UUID,
    cluster_id: UUID,
    listing_id: UUID,
    other_id: UUID,
    suggestion: SameVehicleSuggestion,
) -> UUID:
    assert suggestion.confidence is not None
    row = await fetch_one(
        conn,
        "insert into app.vehicle_cluster_members (workspace_id, cluster_id, listing_id, evidence, confidence)"
        " values (%(workspace_id)s, %(cluster_id)s, %(listing_id)s, %(evidence)s, %(confidence)s)"
        " returning id",
        {
            "workspace_id": workspace_id,
            "cluster_id": cluster_id,
            "listing_id": listing_id,
            "evidence": Jsonb({**_basis(suggestion), "paired_with": str(other_id)}),
            "confidence": suggestion.confidence.value,
        },
    )
    assert row is not None
    member_id: UUID = row["id"]
    return member_id


async def suggest_possible_same_vehicle(
    conn: Conn,
    actor: ActorContext,
    listing_a: UUID,
    listing_b: UUID,
    suggestion: SameVehicleSuggestion,
) -> SuggestionResult:
    """Record a suggested same-vehicle link for human review (never a merge)."""
    _require_system(actor)
    if listing_a == listing_b:
        raise ValidationFailed("a cluster links two different listings")
    if not suggestion.suggest or suggestion.confidence is None:
        return SuggestionResult(outcome="not_suggested")
    ws = actor.workspace_id
    first, second = sorted((listing_a, listing_b))
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "select id from app.listings where workspace_id = %(workspace_id)s"
            " and id = any(%(ids)s::uuid[])"
            ' order by source_id, source_listing_id collate "C", incarnation for update',
            {"workspace_id": ws, "ids": [first, second]},
        )
        if len(rows) != 2:
            raise NotFound("Listing not found")
        in_a = await _active_clusters(conn, ws, listing_a)
        in_b = await _active_clusters(conn, ws, listing_b)
        shared = sorted(set(in_a) & set(in_b))
        if shared:
            return SuggestionResult(outcome="already_clustered", cluster_id=shared[0])
        rejected = await _rejected_pair(conn, ws, listing_a, listing_b)
        if rejected is not None:
            # A reviewer already decided this pair is a false positive; never re-suggest it.
            return SuggestionResult(outcome="previously_unlinked", cluster_id=rejected)
        if in_a and in_b:
            return SuggestionResult(outcome="both_clustered")
        if in_a or in_b:
            cluster_id = (in_a or in_b)[0]
            joining, anchor = (listing_b, listing_a) if in_a else (listing_a, listing_b)
            if await _was_unlinked(conn, ws, cluster_id, joining):
                return SuggestionResult(outcome="previously_unlinked", cluster_id=cluster_id)
            await fetch_one(conn, _LOCK_CLUSTER_SQL, {"workspace_id": ws, "id": cluster_id})
            member = await _add_member(conn, ws, cluster_id, joining, anchor, suggestion)
            await conn.execute(
                "update app.vehicle_clusters set row_version = row_version + 1"
                " where workspace_id = %(workspace_id)s and id = %(id)s",
                {"workspace_id": ws, "id": cluster_id},
            )
            await audit.record(
                conn,
                actor,
                "cluster.add_member",
                "vehicle_cluster",
                cluster_id,
                metadata={
                    "listing_id": str(joining),
                    "paired_with": str(anchor),
                    "score": str(suggestion.score),
                },
            )
            return SuggestionResult(outcome="added_member", cluster_id=cluster_id, member_ids=(member,))
        row = await fetch_one(
            conn,
            "insert into app.vehicle_clusters (workspace_id, confidence, match_basis)"
            " values (%(workspace_id)s, %(confidence)s, %(basis)s) returning id",
            {
                "workspace_id": ws,
                "confidence": suggestion.confidence.value,
                "basis": Jsonb(_basis(suggestion)),
            },
        )
        assert row is not None
        cluster_id = row["id"]
        members = (
            await _add_member(conn, ws, cluster_id, listing_a, listing_b, suggestion),
            await _add_member(conn, ws, cluster_id, listing_b, listing_a, suggestion),
        )
        await audit.record(
            conn,
            actor,
            "cluster.suggest",
            "vehicle_cluster",
            cluster_id,
            new_version=1,
            metadata={
                "listing_ids": [str(listing_a), str(listing_b)],
                "confidence": suggestion.confidence.value,
                "score": str(suggestion.score),
            },
        )
    return SuggestionResult(outcome="created_cluster", cluster_id=cluster_id, member_ids=members)


async def _member_for_update(
    conn: Conn, actor: ActorContext, member_id: UUID
) -> tuple[ClusterRecord, ClusterMemberRecord]:
    head = await fetch_one(
        conn,
        "select cluster_id from app.vehicle_cluster_members where workspace_id = %(workspace_id)s"
        " and id = %(id)s",
        {"workspace_id": actor.workspace_id, "id": member_id},
    )
    if head is None:
        raise NotFound("Cluster member not found")
    cluster_row = await fetch_one(
        conn, _LOCK_CLUSTER_SQL, {"workspace_id": actor.workspace_id, "id": head["cluster_id"]}
    )
    assert cluster_row is not None
    member_row = await fetch_one(
        conn,
        sql.SQL(
            "select {columns} from app.vehicle_cluster_members where workspace_id = %(workspace_id)s"
            " and id = %(id)s for update"
        ).format(columns=_cols(_MEMBER_COLUMNS)),
        {"workspace_id": actor.workspace_id, "id": member_id},
    )
    assert member_row is not None
    return ClusterRecord.model_validate(cluster_row), ClusterMemberRecord.model_validate(member_row)


async def _bump(conn: Conn, workspace_id: UUID, cluster_id: UUID) -> int:
    row = await fetch_one(
        conn,
        "update app.vehicle_clusters set row_version = row_version + 1"
        " where workspace_id = %(workspace_id)s and id = %(id)s returning row_version",
        {"workspace_id": workspace_id, "id": cluster_id},
    )
    assert row is not None
    return int(row["row_version"])


async def confirm_member(
    conn: Conn, actor: ActorContext, member_id: UUID, *, reason: str
) -> ClusterMemberRecord:
    """Manual confirmation that this listing belongs to the vehicle cluster (audited)."""
    _require_reviewer(actor)
    clean = _reason(reason)
    async with mapped_errors():
        cluster, member = await _member_for_update(conn, actor, member_id)
        if not member.active:
            raise VersionConflict("An unlinked member cannot be confirmed")
        if member.manually_confirmed:
            return member
        row = await fetch_one(
            conn,
            sql.SQL(
                "update app.vehicle_cluster_members set manually_confirmed = true, confirmed_by = %(by)s,"
                " confirmed_at = clock_timestamp() where workspace_id = %(workspace_id)s and id = %(id)s"
                " returning {columns}"
            ).format(columns=_cols(_MEMBER_COLUMNS)),
            {"workspace_id": actor.workspace_id, "id": member.id, "by": actor.principal_id},
        )
        assert row is not None
        version = await _bump(conn, actor.workspace_id, cluster.id)
        await audit.record(
            conn,
            actor,
            "cluster.confirm_member",
            "vehicle_cluster",
            cluster.id,
            prior_version=cluster.row_version,
            new_version=version,
            reason=clean,
            metadata={"member_id": str(member.id), "listing_id": str(member.listing_id)},
        )
    return ClusterMemberRecord.model_validate(row)


async def unlink_member(
    conn: Conn, actor: ActorContext, member_id: UUID, *, reason: str
) -> ClusterMemberRecord:
    """False-positive review: unlink a listing from a cluster; the row is kept with who/when/why."""
    _require_reviewer(actor)
    clean = _reason(reason)
    async with mapped_errors():
        cluster, member = await _member_for_update(conn, actor, member_id)
        if not member.active:
            return member
        row = await fetch_one(
            conn,
            sql.SQL(
                "update app.vehicle_cluster_members set unlinked_at = greatest(clock_timestamp(), linked_at),"
                " unlinked_by = %(by)s, unlink_reason = %(reason)s"
                " where workspace_id = %(workspace_id)s and id = %(id)s returning {columns}"
            ).format(columns=_cols(_MEMBER_COLUMNS)),
            {"workspace_id": actor.workspace_id, "id": member.id, "by": actor.principal_id, "reason": clean},
        )
        assert row is not None
        version = await _bump(conn, actor.workspace_id, cluster.id)
        await audit.record(
            conn,
            actor,
            "cluster.unlink_member",
            "vehicle_cluster",
            cluster.id,
            prior_version=cluster.row_version,
            new_version=version,
            reason=clean,
            metadata={
                "member_id": str(member.id),
                "listing_id": str(member.listing_id),
                "false_positive": True,
            },
        )
    return ClusterMemberRecord.model_validate(row)


async def review_cluster(
    conn: Conn,
    actor: ActorContext,
    cluster_id: UUID,
    status: Literal["confirmed", "rejected"],
    *,
    expected_version: int,
    reason: str,
) -> ClusterRecord:
    """Confirm or reject a whole suggestion (optimistic ``expected_version``; audited)."""
    _require_reviewer(actor)
    if status not in ("confirmed", "rejected"):
        raise ValidationFailed("status must be confirmed or rejected")
    clean = _reason(reason)
    async with mapped_errors():
        row = await fetch_one(conn, _LOCK_CLUSTER_SQL, {"workspace_id": actor.workspace_id, "id": cluster_id})
        if row is None:
            raise NotFound("Cluster not found")
        cluster = ClusterRecord.model_validate(row)
        if cluster.row_version != expected_version:
            raise VersionConflict(
                "The cluster changed; reload and retry", current_version=cluster.row_version
            )
        updated = await fetch_one(
            conn,
            sql.SQL(
                "update app.vehicle_clusters set review_status = %(status)s, reviewed_by = %(by)s,"
                " reviewed_at = clock_timestamp(), row_version = row_version + 1"
                " where workspace_id = %(workspace_id)s and id = %(id)s returning {columns}"
            ).format(columns=_cols(_CLUSTER_COLUMNS)),
            {
                "workspace_id": actor.workspace_id,
                "id": cluster.id,
                "status": status,
                "by": actor.principal_id,
            },
        )
        assert updated is not None
        record = ClusterRecord.model_validate(updated)
        await audit.record(
            conn,
            actor,
            "cluster.review",
            "vehicle_cluster",
            cluster.id,
            prior_version=cluster.row_version,
            new_version=record.row_version,
            reason=clean,
            metadata={"status": status, "prior_status": cluster.review_status},
        )
    return record


async def get_cluster(conn: Conn, actor: ActorContext, cluster_id: UUID) -> ClusterView:
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        row = await fetch_one(conn, _CLUSTER_SQL, {"workspace_id": actor.workspace_id, "id": cluster_id})
        if row is None:
            raise NotFound("Cluster not found")
        members = await fetch_all(
            conn, _MEMBERS_SQL, {"workspace_id": actor.workspace_id, "cluster_id": cluster_id}
        )
    return ClusterView(
        cluster=ClusterRecord.model_validate(row),
        members=tuple(ClusterMemberRecord.model_validate(m) for m in members),
    )


async def list_clusters_for_listing(conn: Conn, actor: ActorContext, listing_id: UUID) -> list[ClusterView]:
    """Clusters the listing is (or was) a member of, including unlinked history."""
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            "select distinct cluster_id from app.vehicle_cluster_members"
            " where workspace_id = %(workspace_id)s and listing_id = %(listing_id)s order by cluster_id",
            {"workspace_id": actor.workspace_id, "listing_id": listing_id},
        )
    return [await get_cluster(conn, actor, r["cluster_id"]) for r in rows]


async def cluster_lifecycle(conn: Conn, actor: ActorContext, cluster_id: UUID) -> ClusterLifecycle:
    """Earliest appearance and latest presence over ACTIVE members, per-source evidence kept."""
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        exists = await fetch_one(conn, _CLUSTER_SQL, {"workspace_id": actor.workspace_id, "id": cluster_id})
        if exists is None:
            raise NotFound("Cluster not found")
        rows = await fetch_all(
            conn,
            "select l.id, l.source_id, s.source_key, l.first_seen_at, l.last_seen_at,"
            " l.last_detail_success_at, l.availability"
            " from app.vehicle_cluster_members m"
            " join app.listings l on l.workspace_id = m.workspace_id and l.id = m.listing_id"
            " join app.sources s on s.workspace_id = l.workspace_id and s.id = l.source_id"
            " where m.workspace_id = %(workspace_id)s and m.cluster_id = %(cluster_id)s"
            " and m.unlinked_at is null order by l.first_seen_at, l.id",
            {"workspace_id": actor.workspace_id, "cluster_id": cluster_id},
        )
    sources = tuple(
        SourcePresence(
            listing_id=r["id"],
            source_id=r["source_id"],
            source_key=r["source_key"],
            first_seen_at=ensure_utc(r["first_seen_at"]),
            last_seen_at=ensure_utc(r["last_seen_at"]),
            last_detail_success_at=None
            if r["last_detail_success_at"] is None
            else ensure_utc(r["last_detail_success_at"]),
            availability=Availability(r["availability"]),
        )
        for r in rows
    )
    return ClusterLifecycle(
        cluster_id=cluster_id,
        earliest_observed_at=min((s.first_seen_at for s in sources), default=None),
        latest_source_presence_at=max((s.last_seen_at for s in sources), default=None),
        sources=sources,
    )


__all__ = [
    "ClusterLifecycle",
    "ClusterMemberRecord",
    "ClusterRecord",
    "ClusterView",
    "SourcePresence",
    "SuggestionResult",
    "cluster_lifecycle",
    "confirm_member",
    "get_cluster",
    "list_clusters_for_listing",
    "review_cluster",
    "suggest_possible_same_vehicle",
    "unlink_member",
]
