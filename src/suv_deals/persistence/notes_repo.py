"""Private notes and member watchlists (spec sections 11, 21 ``deals_add_note``, 23).

- **Notes** (``app.owner_notes``) are private annotations, kept separate from extracted seller
  claims. The label comes from the authenticated principal, never from the request: an MCP
  client (or system process) writes ``assistant`` notes, a signed-in owner ``owner`` notes and a
  reviewer ``reviewer`` notes. `add_note` is idempotent through ``ops.idempotency_records``
  (operation ``deals_add_note``, request hash of ``{listing_id, note}``): the same key and text
  return the original note, the same key with other text is ``IDEMPOTENCY_CONFLICT``. Note text
  is untrusted data: bounded (1-4000 characters) and refused when it carries control or
  bidi-override characters.
- **Watchlists** (``app.watchlists``): one active watch per (listing, member) with an optional
  recheck interval (1 hour to 30 days; ``next_recheck_at`` in database time) and expiry.
  Adding again updates the active watch (new ``row_version``); removing is idempotent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.errors import Forbidden, NotFound, ValidationFailed
from suv_deals.persistence import audit, idempotency
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import TransientConflict, mapped_errors
from suv_deals.persistence.reviews_repo import begin_idempotent
from suv_deals.views.notes import NoteLabel, NoteView

NOTE_OPERATION: Final = "deals_add_note"
MIN_RECHECK_INTERVAL: Final = timedelta(hours=1)
MAX_RECHECK_INTERVAL: Final = timedelta(days=30)
_FROZEN = ConfigDict(frozen=True, extra="forbid")
_CONTROL_RE: Final = re.compile(
    "[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069]"
)


def note_label(actor: ActorContext) -> NoteLabel:
    """Label by principal kind (and role for signed-in members)."""
    if actor.principal_kind == "user":
        return "owner" if actor.role == Role.OWNER else "reviewer"
    return "assistant"


def _note_text(note: str) -> str:
    if not isinstance(note, str):
        raise ValidationFailed("note must be text", details={"fields": ["note"]})
    text = note.strip()
    if not 1 <= len(text) <= 4000 or _CONTROL_RE.search(text):
        raise ValidationFailed("note must be 1-4000 characters of plain text", details={"fields": ["note"]})
    return text


_NOTE_COLUMNS: Final = (
    "id, listing_id, case_id, label, author_kind, author_principal_id, body, created_at, updated_at,"
    " row_version"
)


def _note_view(row: Mapping[str, Any]) -> NoteView:
    return NoteView(
        note_id=row["id"],
        listing_id=row["listing_id"],
        case_id=row["case_id"],
        label=row["label"],
        author_kind=row["author_kind"],
        author_principal_id=row["author_principal_id"],
        body=row["body"],
        created_at=ensure_utc(row["created_at"]),
        updated_at=ensure_utc(row["updated_at"]),
        row_version=row["row_version"],
    )


async def add_note(
    conn: Conn,
    actor: ActorContext,
    listing_id: UUID,
    note: str,
    idempotency_key: str,
    *,
    case_id: UUID | None = None,
) -> NoteView:
    """``deals_add_note``: a private, labelled note on a registered listing (idempotent)."""
    actor.require(Scope.NOTES_WRITE)
    request: dict[str, Any] = {"listing_id": str(listing_id), "note": note}
    if case_id is not None:
        request["case_id"] = str(case_id)
    replay = await begin_idempotent(
        conn, actor, NOTE_OPERATION, idempotency_key, idempotency.request_hash_for(NOTE_OPERATION, request)
    )
    if replay is not None:
        return NoteView.model_validate(replay)
    body = _note_text(note)
    async with mapped_errors():
        listing = await fetch_one(
            conn,
            "select id from app.listings where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": listing_id},
        )
        if listing is None:
            raise NotFound("Listing not found")
        row = await fetch_one(
            conn,
            "insert into app.owner_notes (workspace_id, listing_id, case_id, author_principal_id,"  # noqa: S608
            " author_kind,"
            " label, body) values (%(ws)s, %(listing_id)s, %(case_id)s, %(principal)s, %(kind)s, %(label)s,"
            f" %(body)s) returning {_NOTE_COLUMNS}",
            {
                "ws": actor.workspace_id,
                "listing_id": listing_id,
                "case_id": case_id,
                "principal": actor.principal_id,
                "kind": actor.principal_kind,
                "label": note_label(actor),
                "body": body,
            },
        )
        assert row is not None
        await audit.record(
            conn, actor, "note.add", "owner_note", row["id"], None, 1, metadata={"label": row["label"]}
        )
    view = _note_view(row)
    await idempotency.complete(conn, actor, NOTE_OPERATION, idempotency_key, view.model_dump(mode="json"))
    return view


async def list_notes(
    conn: Conn, actor: ActorContext, listing_id: UUID, *, limit: int = 200
) -> list[NoteView]:
    """Notes of one listing, newest first (``deals:read``)."""
    actor.require(Scope.DEALS_READ)
    if not 1 <= limit <= 200:
        raise ValidationFailed("limit must be between 1 and 200")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_NOTE_COLUMNS} from app.owner_notes"  # noqa: S608 - fixed column list
            " where workspace_id = %(ws)s and listing_id = %(listing_id)s"
            " order by created_at desc, id desc limit %(limit)s",
            {"ws": actor.workspace_id, "listing_id": listing_id, "limit": limit},
        )
    return [_note_view(r) for r in rows]


# --------------------------------------------------------------------------------------------
# Watchlists
# --------------------------------------------------------------------------------------------


class WatchlistEntry(BaseModel):
    model_config = _FROZEN

    id: UUID
    listing_id: UUID
    created_by: UUID
    reason: str
    expires_at: datetime | None
    recheck_interval: timedelta | None
    next_recheck_at: datetime | None
    active: bool
    row_version: int
    created_at: datetime
    updated_at: datetime


_WATCH_COLUMNS: Final = (
    "id, listing_id, created_by, reason, expires_at, recheck_interval_seconds, next_recheck_at, active,"
    " row_version, created_at, updated_at"
)


def _watch(row: Mapping[str, Any]) -> WatchlistEntry:
    seconds = row["recheck_interval_seconds"]
    return WatchlistEntry(
        id=row["id"],
        listing_id=row["listing_id"],
        created_by=row["created_by"],
        reason=row["reason"],
        expires_at=None if row["expires_at"] is None else ensure_utc(row["expires_at"]),
        recheck_interval=None if seconds is None else timedelta(seconds=seconds),
        next_recheck_at=None if row["next_recheck_at"] is None else ensure_utc(row["next_recheck_at"]),
        active=row["active"],
        row_version=row["row_version"],
        created_at=ensure_utc(row["created_at"]),
        updated_at=ensure_utc(row["updated_at"]),
    )


def _interval_seconds(interval: timedelta | None) -> int | None:
    if interval is None:
        return None
    if not MIN_RECHECK_INTERVAL <= interval <= MAX_RECHECK_INTERVAL:
        raise ValidationFailed("recheck interval must be between one hour and 30 days")
    return int(interval.total_seconds())


def _reason(reason: str) -> str:
    text = reason.strip() if isinstance(reason, str) else ""
    if not 3 <= len(text) <= 2000 or _CONTROL_RE.search(text):
        raise ValidationFailed("reason must be 3-2000 characters of plain text")
    return text


async def add_watch(
    conn: Conn,
    actor: ActorContext,
    listing_id: UUID,
    *,
    reason: str,
    recheck_interval: timedelta | None = None,
    expires_at: datetime | None = None,
) -> WatchlistEntry:
    """Watch a listing (one active watch per member; adding again updates it)."""
    actor.require(Scope.RECHECKS_REQUEST)
    if actor.principal_kind == "system":
        raise Forbidden("Watches belong to members, not system workers")
    params = {
        "ws": actor.workspace_id,
        "listing_id": listing_id,
        "principal": actor.principal_id,
        "reason": _reason(reason),
        "interval": _interval_seconds(recheck_interval),
        "expires_at": None if expires_at is None else ensure_utc(expires_at),
    }
    unique = {"watchlists_active_uidx": lambda: TransientConflict("The watch changed concurrently; retry")}
    async with mapped_errors(unique=unique):
        existing = await fetch_one(
            conn,
            "select id from app.watchlists where workspace_id = %(ws)s and listing_id = %(listing_id)s"
            " and created_by = %(principal)s and active for update",
            params,
        )
        next_recheck = (
            "case when %(interval)s::integer is null then null"
            " else clock_timestamp() + make_interval(secs => %(interval)s::integer) end"
        )
        if existing is None:
            row = await fetch_one(
                conn,
                "insert into app.watchlists (workspace_id, listing_id, created_by, reason, expires_at,"  # noqa: S608
                " recheck_interval_seconds, next_recheck_at) values (%(ws)s, %(listing_id)s, %(principal)s,"
                f" %(reason)s, %(expires_at)s, %(interval)s, {next_recheck}) returning {_WATCH_COLUMNS}",
                params,
            )
            prior = None
        else:
            row = await fetch_one(
                conn,
                "update app.watchlists set reason = %(reason)s, expires_at = %(expires_at)s,"  # noqa: S608
                f" recheck_interval_seconds = %(interval)s, next_recheck_at = {next_recheck},"
                " row_version = row_version + 1 where workspace_id = %(ws)s and id = %(id)s"
                f" returning {_WATCH_COLUMNS}",
                {**params, "id": existing["id"]},
            )
            prior = "updated"
        assert row is not None
        await audit.record(
            conn,
            actor,
            "watchlist.add",
            "watchlist",
            row["id"],
            None if prior is None else row["row_version"] - 1,
            row["row_version"],
            metadata={"recheck_interval_seconds": params["interval"]},
        )
    return _watch(row)


async def remove_watch(conn: Conn, actor: ActorContext, listing_id: UUID) -> WatchlistEntry | None:
    """End the caller's active watch; ``None`` when there was none (idempotent)."""
    actor.require(Scope.RECHECKS_REQUEST)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "update app.watchlists set active = false, row_version = row_version + 1"  # noqa: S608
            " where workspace_id = %(ws)s and listing_id = %(listing_id)s and created_by = %(principal)s"
            f" and active returning {_WATCH_COLUMNS}",
            {"ws": actor.workspace_id, "listing_id": listing_id, "principal": actor.principal_id},
        )
        if row is not None:
            await audit.record(
                conn,
                actor,
                "watchlist.remove",
                "watchlist",
                row["id"],
                row["row_version"] - 1,
                row["row_version"],
            )
    return None if row is None else _watch(row)


async def list_watches(
    conn: Conn,
    actor: ActorContext,
    *,
    listing_id: UUID | None = None,
    mine_only: bool = False,
    include_inactive: bool = False,
    limit: int = 200,
) -> list[WatchlistEntry]:
    actor.require(Scope.DEALS_READ)
    if not 1 <= limit <= 500:
        raise ValidationFailed("limit must be between 1 and 500")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_WATCH_COLUMNS} from app.watchlists where workspace_id = %(ws)s"  # noqa: S608
            " and (%(listing_id)s::uuid is null or listing_id = %(listing_id)s)"
            " and (not %(mine)s or created_by = %(principal)s)"
            " and (%(inactive)s or active)"
            " order by created_at desc, id desc limit %(limit)s",
            {
                "ws": actor.workspace_id,
                "listing_id": listing_id,
                "mine": mine_only,
                "principal": actor.principal_id,
                "inactive": include_inactive,
                "limit": limit,
            },
        )
    return [_watch(r) for r in rows]


async def due_watch_rechecks(conn: Conn, actor: ActorContext, *, limit: int = 100) -> list[WatchlistEntry]:
    """Active, unexpired watches whose recheck is due (database time); system scheduler only."""
    if actor.principal_kind != "system":
        raise Forbidden("Only the scheduler reads due watch rechecks")
    if not 1 <= limit <= 1000:
        raise ValidationFailed("limit must be between 1 and 1000")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            f"select {_WATCH_COLUMNS} from app.watchlists where workspace_id = %(ws)s and active"  # noqa: S608
            " and next_recheck_at <= clock_timestamp()"
            " and (expires_at is null or expires_at > clock_timestamp())"
            " order by next_recheck_at, id limit %(limit)s",
            {"ws": actor.workspace_id, "limit": limit},
        )
    return [_watch(r) for r in rows]


async def advance_watch_recheck(conn: Conn, actor: ActorContext, watch_id: UUID) -> WatchlistEntry:
    """After queueing the recheck, move ``next_recheck_at`` one interval ahead (system)."""
    if actor.principal_kind != "system":
        raise Forbidden("Only the scheduler advances watch rechecks")
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "update app.watchlists set next_recheck_at = clock_timestamp()"  # noqa: S608
            " + make_interval(secs => recheck_interval_seconds), row_version = row_version + 1"
            " where workspace_id = %(ws)s and id = %(id)s and active and recheck_interval_seconds is not null"
            f" returning {_WATCH_COLUMNS}",
            {"ws": actor.workspace_id, "id": watch_id},
        )
    if row is None:
        raise NotFound("Watch not found")
    return _watch(row)


__all__ = [
    "NOTE_OPERATION",
    "WatchlistEntry",
    "add_note",
    "add_watch",
    "advance_watch_recheck",
    "due_watch_rechecks",
    "list_notes",
    "list_watches",
    "note_label",
    "remove_watch",
]
