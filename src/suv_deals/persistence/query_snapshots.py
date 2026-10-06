"""Frozen query snapshots for stable pagination (spec 21 "Pagination and errors").

An as-of timestamp alone does not freeze mutable priority or status, so review-queue listings
persist the ordered result membership plus display projections in ``ops.query_snapshots`` and
paginate by snapshot id + ordinal. Each snapshot is bound to the principal, the workspace, the
query name and the canonical filter hash; any mismatch, expiry or foreign snapshot yields the
same ``VALIDATION_ERROR`` ("re-query"), so nothing about other snapshots leaks.

Pages come from the frozen membership only: reprioritisation, status changes, insertions and
deletions between pages change nothing until the client re-queries. Claim/submit never trust a
projection; they revalidate current versions.

`start_listing` / `next_page` combine snapshots with the signed opaque cursors of
`domain.pagination` (HMAC, bound to query/workspace/principal/filters, never outliving the
snapshot). Snapshots expire within one day (CHECK); `delete_expired` purges them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Scope
from suv_deals.domain.pagination import (
    DEFAULT_CURSOR_TTL,
    MAX_SNAPSHOT_ORDINAL,
    decode_cursor,
    encode_cursor,
    next_snapshot_ordinal,
    snapshot_cursor,
    validate_limit,
)
from suv_deals.errors import Forbidden, ValidationFailed
from suv_deals.persistence.database import Conn, db_now, fetch_one
from suv_deals.persistence.errors_map import mapped_errors

MAX_SNAPSHOT_IDS: Final = 10_000
MAX_SNAPSHOT_TTL: Final = timedelta(days=1)
DEFAULT_SNAPSHOT_TTL: Final = timedelta(minutes=30)
MAX_PROJECTIONS_BYTES: Final = 4 * 1024 * 1024
_QUERY_RE: Final = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")


class SnapshotPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: UUID
    ordinal_start: int
    ids: tuple[UUID, ...]
    projections: tuple[dict[str, Any], ...]
    total: int
    next_ordinal: int | None
    expires_at: datetime


class CursorPage(BaseModel):
    """A page plus the signed cursor for the next one (``None`` at the end)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: SnapshotPage
    next_cursor: str | None


def _require_read(actor: ActorContext) -> None:
    if not (actor.has(Scope.REVIEWS_READ) or actor.has(Scope.DEALS_READ)):
        raise Forbidden("Missing scope: reviews:read or deals:read")


def _query(query_name: str) -> str:
    if not isinstance(query_name, str) or not _QUERY_RE.fullmatch(query_name):
        raise ValidationFailed("query_name must be a lower-case name")
    return query_name


def _filter_hash(value: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise ValidationFailed("filter_hash must be a SHA-256 hex digest")
    return value


def _invalid_snapshot() -> ValidationFailed:
    return ValidationFailed(
        "The result snapshot expired or does not match this query; re-query", details={"cursor": "expired"}
    )


async def create_snapshot(  # noqa: PLR0917 - positional public contract (WP7a API)
    conn: Conn,
    actor: ActorContext,
    filter_hash: str,
    ordered_ids: Sequence[UUID],
    projections: Sequence[Mapping[str, Any]],
    ttl: timedelta = DEFAULT_SNAPSHOT_TTL,
    *,
    query_name: str,
) -> UUID:
    """Freeze an ordered result (unique ids, one projection per id or none) for the principal."""
    _require_read(actor)
    ids = list(ordered_ids)
    if len(ids) > MAX_SNAPSHOT_IDS:
        raise ValidationFailed("a snapshot holds at most 10,000 results; narrow the filters")
    if len(set(ids)) != len(ids):
        raise ValidationFailed("snapshot ids must be unique")
    rows = [dict(p) for p in projections]
    if rows and len(rows) != len(ids):
        raise ValidationFailed("provide exactly one projection per id")
    if not timedelta(seconds=1) <= ttl <= MAX_SNAPSHOT_TTL:
        raise ValidationFailed("snapshot ttl must be between one second and one day")
    try:
        encoded = json.dumps(rows, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("projections must be plain JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_PROJECTIONS_BYTES:
        raise ValidationFailed("projections are too large")
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into ops.query_snapshots"
            " (workspace_id, principal_id, query_name, filter_hash, result_ids, projections, expires_at)"
            " values (%(workspace_id)s, %(principal)s, %(query)s, %(filter_hash)s, %(ids)s::uuid[],"
            " %(projections)s, now() + %(ttl)s::interval)"
            " returning id",
            {
                "workspace_id": actor.workspace_id,
                "principal": actor.principal_id,
                "query": _query(query_name),
                "filter_hash": _filter_hash(filter_hash),
                "ids": ids,
                "projections": Jsonb(rows),
                "ttl": ttl,
            },
        )
    assert row is not None
    snapshot_id: UUID = row["id"]
    return snapshot_id


_PAGE_SQL: Final = """
select s.id,
       s.expires_at,
       pg_catalog.cardinality(s.result_ids) as total,
       s.result_ids[%(lo)s:%(hi)s] as ids,
       pg_catalog.jsonb_array_length(s.projections) as projection_count,
       coalesce((
         select pg_catalog.jsonb_agg(e.value order by e.ordinality)
           from pg_catalog.jsonb_array_elements(s.projections) with ordinality as e(value, ordinality)
          where e.ordinality between %(lo)s and %(hi)s), '[]'::jsonb) as projections
  from ops.query_snapshots s
 where s.workspace_id = %(workspace_id)s
   and s.id = %(snapshot_id)s
   and s.principal_id = %(principal)s
   and s.query_name = %(query)s
   and s.filter_hash = %(filter_hash)s
   and s.expires_at > now()
"""


async def page(
    conn: Conn,
    actor: ActorContext,
    snapshot_id: UUID,
    ordinal_start: int,
    limit: int | None,
    *,
    query_name: str,
    filter_hash: str,
) -> SnapshotPage:
    """One page of a frozen snapshot, bound to principal + workspace + query + filter hash."""
    _require_read(actor)
    size = validate_limit(limit)
    if isinstance(ordinal_start, bool) or not 0 <= ordinal_start <= MAX_SNAPSHOT_ORDINAL:
        raise ValidationFailed("ordinal must be between 0 and 10,000")
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _PAGE_SQL,
            {
                "workspace_id": actor.workspace_id,
                "snapshot_id": snapshot_id,
                "principal": actor.principal_id,
                "query": _query(query_name),
                "filter_hash": _filter_hash(filter_hash),
                "lo": ordinal_start + 1,
                "hi": ordinal_start + size,
            },
        )
    if row is None:
        raise _invalid_snapshot()
    total = int(row["total"] or 0)
    if ordinal_start > total:
        raise _invalid_snapshot()
    ids = tuple(row["ids"] or ())
    projections = tuple(row["projections"] or ()) if int(row["projection_count"] or 0) else ()
    return SnapshotPage(
        snapshot_id=row["id"],
        ordinal_start=ordinal_start,
        ids=ids,
        projections=projections,
        total=total,
        next_ordinal=next_snapshot_ordinal(ordinal_start, len(ids), total),
        expires_at=ensure_utc(row["expires_at"]),
    )


async def delete_expired(conn: Conn, actor: ActorContext, *, limit: int = 1000) -> int:
    """Purge expired snapshots of the actor's workspace (system/owner maintenance)."""
    if actor.principal_kind != "system" and not actor.has(Scope.CONFIG_ADMIN):
        raise Forbidden("Only system maintenance or an owner may purge snapshots")
    if not 1 <= limit <= 10_000:
        raise ValidationFailed("invalid purge limit")
    async with mapped_errors():
        cur = await conn.execute(
            "delete from ops.query_snapshots where id in ("
            " select id from ops.query_snapshots"
            " where workspace_id = %(workspace_id)s and expires_at <= now()"
            " order by expires_at limit %(limit)s)"
            " and workspace_id = %(workspace_id)s",
            {"workspace_id": actor.workspace_id, "limit": limit},
        )
    return cur.rowcount


# --------------------------------------------------------------------------------------------
# Signed-cursor integration (domain.pagination)
# --------------------------------------------------------------------------------------------


async def _cursor_for(
    conn: Conn,
    actor: ActorContext,
    result: SnapshotPage,
    *,
    query_name: str,
    filter_hash: str,
    secret: bytes | Sequence[bytes],
    cursor_ttl: timedelta,
) -> str | None:
    if result.next_ordinal is None:
        return None
    now = ensure_utc(await db_now(conn))
    payload = snapshot_cursor(
        query=query_name,
        workspace_id=actor.workspace_id,
        principal_id=actor.principal_id,
        filters_hash=filter_hash,
        snapshot_id=result.snapshot_id,
        next_ordinal=result.next_ordinal,
        now=now,
        snapshot_expires_at=result.expires_at,
        ttl=cursor_ttl,
    )
    return encode_cursor(payload, secret)


async def start_listing(
    conn: Conn,
    actor: ActorContext,
    *,
    query_name: str,
    filter_hash: str,
    ordered_ids: Sequence[UUID],
    projections: Sequence[Mapping[str, Any]],
    limit: int | None,
    secret: bytes | Sequence[bytes],
    ttl: timedelta = DEFAULT_SNAPSHOT_TTL,
    cursor_ttl: timedelta = DEFAULT_CURSOR_TTL,
) -> CursorPage:
    """Freeze a fresh result and return its first page plus the signed next-page cursor."""
    snapshot_id = await create_snapshot(
        conn, actor, filter_hash, ordered_ids, projections, ttl, query_name=query_name
    )
    first = await page(conn, actor, snapshot_id, 0, limit, query_name=query_name, filter_hash=filter_hash)
    cursor = await _cursor_for(
        conn,
        actor,
        first,
        query_name=query_name,
        filter_hash=filter_hash,
        secret=secret,
        cursor_ttl=cursor_ttl,
    )
    return CursorPage(page=first, next_cursor=cursor)


async def next_page(
    conn: Conn,
    actor: ActorContext,
    *,
    cursor: str,
    query_name: str,
    filter_hash: str,
    limit: int | None,
    secret: bytes | Sequence[bytes],
    cursor_ttl: timedelta = DEFAULT_CURSOR_TTL,
) -> CursorPage:
    """Verify a signed snapshot cursor (MAC, expiry, query/workspace/principal/filter binding)
    and return the next frozen page. Keyset cursors are rejected here."""
    _require_read(actor)
    now = ensure_utc(await db_now(conn))
    payload = decode_cursor(
        cursor,
        secret,
        query=_query(query_name),
        workspace_id=actor.workspace_id,
        principal_id=actor.principal_id,
        filters_hash=_filter_hash(filter_hash),
        now=now,
    )
    if payload.snap is None or payload.ord is None:
        raise ValidationFailed(
            "Invalid or expired cursor; restart the listing", details={"cursor": "mismatch"}
        )
    result = await page(
        conn, actor, payload.snap, payload.ord, limit, query_name=query_name, filter_hash=filter_hash
    )
    next_cursor = await _cursor_for(
        conn,
        actor,
        result,
        query_name=query_name,
        filter_hash=filter_hash,
        secret=secret,
        cursor_ttl=cursor_ttl,
    )
    return CursorPage(page=result, next_cursor=next_cursor)
