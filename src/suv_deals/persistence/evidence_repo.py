"""Field-level evidence (spec 7, 11: ``app.field_evidence``).

- `insert_provenance_evidence` turns a revision's `FieldProvenance` map into append-only evidence
  rows (field path, snapshot, bounded raw excerpt, method, transformation, extraction
  confidence). Extraction confidence is never a claim about truth; ``claim_status`` stays unset.
- `verify_field_evidence` records an owner/reviewer verification. Evidence is append-only, so a
  verification is a NEW row that supersedes the original (``supersedes_id``) and carries
  ``verified_by``/``verified_at``. Only signed-in owners and reviewers may verify (never an MCP
  client or a system process: caller text must not impersonate an owner), and only the latest row
  of a chain can be superseded.
- Reads need ``deals:read``; every query carries the workspace predicate.

Lock order: verification locks the listing row (``app.listings``, serialising verifications of one
listing) before inserting the superseding row; the audit event is written last.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Final
from uuid import UUID

from psycopg import sql
from pydantic import BaseModel, ConfigDict, field_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import ClaimStatus, Confidence, ExtractionMethod, Role, Scope
from suv_deals.domain.provenance import MAX_RAW_EXCERPT, FieldProvenance
from suv_deals.errors import Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.observability.logging import redact
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors

_FIELD_PATH_RE: Final = re.compile(r"^[A-Za-z0-9_.\[\]-]{1,200}$")
_VERIFIABLE: Final = frozenset({ClaimStatus.VERIFIED, ClaimStatus.SELLER_DENIED, ClaimStatus.CONFLICTING})
_COLUMNS: Final = (
    "id",
    "workspace_id",
    "listing_id",
    "revision_id",
    "field_path",
    "snapshot_id",
    "document_ref",
    "raw_excerpt",
    "method",
    "transformation",
    "confidence",
    "claim_status",
    "verified_by",
    "verified_at",
    "supersedes_id",
    "observed_at",
    "created_at",
)


def _columns() -> sql.Composable:
    return sql.SQL(", ").join(sql.Identifier(c) for c in _COLUMNS)


class FieldEvidenceRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    listing_id: UUID
    revision_id: UUID | None = None
    field_path: str
    snapshot_id: UUID | None = None
    document_ref: str | None = None
    raw_excerpt: str | None = None
    method: ExtractionMethod
    transformation: str | None = None
    confidence: Confidence
    claim_status: ClaimStatus | None = None
    verified_by: UUID | None = None
    verified_at: datetime | None = None
    supersedes_id: UUID | None = None
    observed_at: datetime
    created_at: datetime

    @field_validator("verified_at", "observed_at", "created_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


def _excerpt(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:MAX_RAW_EXCERPT] or None


_INSERT_SQL: Final = (
    "insert into app.field_evidence (workspace_id, listing_id, revision_id, field_path, snapshot_id,"
    " document_ref, raw_excerpt, method, transformation, confidence, claim_status, verified_by,"
    " verified_at, supersedes_id, observed_at)"
    " values (%(workspace_id)s, %(listing_id)s, %(revision_id)s, %(field_path)s, %(snapshot_id)s,"
    " %(document_ref)s, %(raw_excerpt)s, %(method)s, %(transformation)s, %(confidence)s,"
    " %(claim_status)s, %(verified_by)s, %(verified_at)s, %(supersedes_id)s, %(observed_at)s)"
)


_GET_SQL: Final = sql.SQL(
    "select {columns} from app.field_evidence where workspace_id = %(workspace_id)s and id = %(id)s"
).format(columns=_columns())
_VERIFY_SQL: Final = sql.SQL(
    "insert into app.field_evidence (workspace_id, listing_id, revision_id, field_path, snapshot_id,"
    " document_ref, raw_excerpt, method, transformation, confidence, claim_status, verified_by,"
    " verified_at, supersedes_id, observed_at)"
    " values (%(workspace_id)s, %(listing_id)s, %(revision_id)s, %(field_path)s, %(snapshot_id)s,"
    " %(document_ref)s, %(raw_excerpt)s, %(method)s, %(transformation)s, %(confidence)s,"
    " %(claim_status)s, %(verified_by)s, clock_timestamp(), %(supersedes_id)s, %(observed_at)s)"
    " returning {columns}"
).format(columns=_columns())


async def insert_provenance_evidence(
    conn: Conn,
    actor: ActorContext,
    *,
    listing_id: UUID,
    revision_id: UUID,
    provenance: Mapping[str, FieldProvenance],
    snapshot_id: UUID | None,
) -> int:
    """Append one evidence row per provenance entry of a new revision; returns the row count.

    Entries whose field path is not a plain dotted path are skipped (never interpolated). The
    stored snapshot is the observation's own snapshot: a provenance ``snapshot_id`` that does not
    name it is not trusted (it could reference any row).
    """
    if actor.principal_kind != "system" and not actor.has(Scope.CONFIG_ADMIN):
        raise Forbidden("Only the ingestion pipeline records extraction evidence")
    count = 0
    async with mapped_errors():
        for path in sorted(provenance):
            if not _FIELD_PATH_RE.fullmatch(path):
                continue
            entry = provenance[path]
            await conn.execute(
                _INSERT_SQL,
                {
                    "workspace_id": actor.workspace_id,
                    "listing_id": listing_id,
                    "revision_id": revision_id,
                    "field_path": path,
                    "snapshot_id": snapshot_id,
                    "document_ref": None,
                    "raw_excerpt": _excerpt(entry.raw_text),
                    "method": entry.method.value,
                    "transformation": None if entry.transformation is None else entry.transformation[:200],
                    "confidence": entry.confidence.value,
                    "claim_status": None,
                    "verified_by": None,
                    "verified_at": None,
                    "supersedes_id": None,
                    "observed_at": entry.observed_at,
                },
            )
            count += 1
    return count


async def get_field_evidence(conn: Conn, actor: ActorContext, evidence_id: UUID) -> FieldEvidenceRecord:
    actor.require(Scope.DEALS_READ)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _GET_SQL,
            {"workspace_id": actor.workspace_id, "id": evidence_id},
        )
    if row is None:
        raise NotFound("Evidence not found")
    return FieldEvidenceRecord.model_validate(row)


async def list_field_evidence(
    conn: Conn,
    actor: ActorContext,
    listing_id: UUID,
    *,
    revision_id: UUID | None = None,
    field_path: str | None = None,
    include_superseded: bool = False,
    limit: int = 200,
) -> list[FieldEvidenceRecord]:
    """Evidence of one listing, newest first per field; superseded rows hidden by default."""
    actor.require(Scope.DEALS_READ)
    if not 1 <= limit <= 1000:
        raise ValidationFailed("limit must be between 1 and 1000")
    if field_path is not None and not _FIELD_PATH_RE.fullmatch(field_path):
        raise ValidationFailed("field_path must be a dotted field path")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            sql.SQL(
                "select {columns} from app.field_evidence e"
                " where e.workspace_id = %(workspace_id)s and e.listing_id = %(listing_id)s"
                " and (%(revision_id)s::uuid is null or e.revision_id = %(revision_id)s::uuid)"
                " and (%(field_path)s::text is null or e.field_path = %(field_path)s::text)"
                " and (%(include_superseded)s or not exists ("
                "   select 1 from app.field_evidence n"
                "    where n.workspace_id = e.workspace_id and n.supersedes_id = e.id))"
                " order by e.field_path, e.created_at desc, e.id desc limit %(limit)s"
            ).format(columns=sql.SQL(", ").join(sql.Identifier("e", c) for c in _COLUMNS)),
            {
                "workspace_id": actor.workspace_id,
                "listing_id": listing_id,
                "revision_id": revision_id,
                "field_path": field_path,
                "include_superseded": include_superseded,
                "limit": limit,
            },
        )
    # A verification row copies its original's revision, so revision filters keep whole chains.
    return [FieldEvidenceRecord.model_validate(r) for r in rows]


def _require_verifier(actor: ActorContext) -> None:
    actor.require(Scope.REVIEWS_WRITE)
    if actor.principal_kind != "user" or actor.role not in (Role.OWNER, Role.REVIEWER):
        raise Forbidden("Only a signed-in owner or reviewer may verify evidence")


async def verify_field_evidence(
    conn: Conn,
    actor: ActorContext,
    evidence_id: UUID,
    *,
    claim_status: ClaimStatus = ClaimStatus.VERIFIED,
    document_ref: str | None = None,
    note: str | None = None,
) -> FieldEvidenceRecord:
    """Record an owner/reviewer verification as a superseding evidence row (append-only)."""
    _require_verifier(actor)
    status = ClaimStatus(claim_status)
    if status not in _VERIFIABLE:
        raise ValidationFailed("verification records verified, seller_denied or conflicting")
    ref = None if document_ref is None else redact(document_ref).strip()[:500] or None
    async with mapped_errors():
        original_row = await fetch_one(
            conn,
            _GET_SQL,
            {"workspace_id": actor.workspace_id, "id": evidence_id},
        )
        if original_row is None:
            raise NotFound("Evidence not found")
        original = FieldEvidenceRecord.model_validate(original_row)
        locked = await fetch_one(
            conn,
            "select id from app.listings where workspace_id = %(workspace_id)s and id = %(id)s for update",
            {"workspace_id": actor.workspace_id, "id": original.listing_id},
        )
        if locked is None:  # pragma: no cover - composite FK
            raise NotFound("Evidence not found")
        newer = await fetch_one(
            conn,
            "select id from app.field_evidence where workspace_id = %(workspace_id)s"
            " and supersedes_id = %(id)s limit 1",
            {"workspace_id": actor.workspace_id, "id": original.id},
        )
        if newer is not None:
            raise VersionConflict("This evidence was already superseded; verify the latest row")
        row = await fetch_one(
            conn,
            _VERIFY_SQL,
            {
                "workspace_id": actor.workspace_id,
                "listing_id": original.listing_id,
                "revision_id": original.revision_id,
                "field_path": original.field_path,
                "snapshot_id": original.snapshot_id,
                "document_ref": ref or original.document_ref,
                "raw_excerpt": original.raw_excerpt,
                "method": original.method.value,
                "transformation": original.transformation,
                "confidence": original.confidence.value,
                "claim_status": status.value,
                "verified_by": actor.principal_id,
                "supersedes_id": original.id,
                "observed_at": original.observed_at,
            },
        )
        assert row is not None
        record = FieldEvidenceRecord.model_validate(row)
        await audit.record(
            conn,
            actor,
            "field_evidence.verify",
            "field_evidence",
            record.id,
            reason=note,
            metadata={
                "listing_id": str(original.listing_id),
                "field_path": original.field_path,
                "supersedes_id": str(original.id),
                "claim_status": status.value,
            },
        )
    return record


__all__ = [
    "FieldEvidenceRecord",
    "get_field_evidence",
    "insert_provenance_evidence",
    "list_field_evidence",
    "verify_field_evidence",
]
