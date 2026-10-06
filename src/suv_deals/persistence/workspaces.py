"""Workspaces and memberships (spec sections 11, 12; ADR 0001).

- `resolve_memberships_for_user` is the membership bootstrap: it runs BEFORE a workspace is
  selected, in its own short transaction with only the ``app.user_id`` GUC set (from a verified
  JWT subject), so RLS (``membership_self_read`` / ``workspace_member_read``) exposes exactly the
  caller's own active memberships. The query also filters on ``user_id`` explicitly.
- `get_membership` reads one membership inside a transaction whose GUC already names the
  workspace or the user (auth layer, before an `ActorContext` exists).
- Membership writes are owner-only (spec 12: "separately guarded backend operation", never an
  MCP tool): a human owner with ``config:admin``. They are audited, and the last active owner of a
  workspace can never be demoted or deactivated.
- `create_workspace` is for setup scripts only. ``suv_backend`` has no INSERT on
  ``app.workspaces`` (workspaces are provisioned by the owner role), so it must run on a
  privileged maintenance connection; it creates the workspace, the owner membership and an audit
  event in one transaction.

Lock order: membership writes lock the workspace's membership rows (``FOR UPDATE``, ordered by
user id) and write the audit event last; no other chain table is touched.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from psycopg import sql
from pydantic import BaseModel, ConfigDict, field_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.errors import Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, Database, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors

_TIMEZONE_RE: Final = re.compile(r"^[A-Za-z0-9_+/-]{1,64}$")
_MEMBERSHIP_COLUMNS: Final = sql.SQL(
    "m.workspace_id, m.user_id, m.role, m.active, m.created_at, m.updated_at,"
    " w.name as workspace_name, w.display_timezone, w.active as workspace_active"
)


class Membership(BaseModel):
    """One membership with the display fields of its workspace."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    workspace_id: UUID
    user_id: UUID
    role: Role
    active: bool
    workspace_name: str
    display_timezone: str
    workspace_active: bool
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class WorkspaceBootstrap(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: UUID
    name: str
    display_timezone: str
    owner: Membership


_RESOLVE_SQL: Final = sql.SQL(
    "select {columns} from app.memberships m"
    " join app.workspaces w on w.id = m.workspace_id"
    " where m.user_id = %(user_id)s and m.active and w.active"
    " order by w.name, m.workspace_id"
).format(columns=_MEMBERSHIP_COLUMNS)

_GET_SQL: Final = sql.SQL(
    "select {columns} from app.memberships m"
    " join app.workspaces w on w.id = m.workspace_id"
    " where m.workspace_id = %(workspace_id)s and m.user_id = %(user_id)s"
).format(columns=_MEMBERSHIP_COLUMNS)

_LOCK_MEMBERS_SQL: Final = (
    "select user_id, role, active from app.memberships"
    " where workspace_id = %(workspace_id)s order by user_id for update"
)


async def resolve_memberships_for_user(db: Database, user_id: UUID) -> list[Membership]:
    """Active memberships of a verified user in active workspaces (bootstrap, no workspace GUC).

    ``user_id`` must come from a verified token (the auth layer), never from a request body.
    """
    if not isinstance(user_id, UUID):
        raise ValidationFailed("user_id must be a UUID")
    async with mapped_errors(), db.transaction(user_id=user_id) as conn, mapped_errors():
        rows = await fetch_all(conn, _RESOLVE_SQL, {"user_id": user_id})
    return [Membership.model_validate(r) for r in rows]


async def get_membership(conn: Conn, workspace_id: UUID, user_id: UUID) -> Membership | None:
    """The membership of ``user_id`` in ``workspace_id`` as visible in the current transaction.

    Returns ``None`` when it does not exist or is not visible (another workspace, or no GUC).
    Inactive memberships are returned with ``active=False``; callers must check it.
    """
    async with mapped_errors():
        row = await fetch_one(conn, _GET_SQL, {"workspace_id": workspace_id, "user_id": user_id})
    return None if row is None else Membership.model_validate(row)


def _require_owner(actor: ActorContext) -> None:
    """Membership administration: a human owner holding ``config:admin`` (never MCP or system)."""
    actor.require(Scope.CONFIG_ADMIN)
    if actor.principal_kind != "user" or actor.role != Role.OWNER:
        raise Forbidden("Only a signed-in owner may change memberships")


def _active_owners(rows: list[Any]) -> set[UUID]:
    return {r["user_id"] for r in rows if r["active"] and r["role"] == Role.OWNER.value}


async def add_membership(
    conn: Conn, actor: ActorContext, user_id: UUID, role: Role, *, reason: str
) -> Membership:
    """Add (or reactivate / change the role of) a member of the actor's workspace; audited.

    Refuses to demote the last active owner. ``user_id`` must exist in ``auth.users``
    (otherwise `NotFound`).
    """
    _require_owner(actor)
    role = Role(role)
    params = {"workspace_id": actor.workspace_id, "user_id": user_id, "role": role.value}
    async with mapped_errors():
        members = await fetch_all(conn, _LOCK_MEMBERS_SQL, params)
        prior = next((m for m in members if m["user_id"] == user_id), None)
        owners = _active_owners(members)
        if prior is not None and owners == {user_id} and role != Role.OWNER:
            raise VersionConflict("The last active owner cannot be demoted")
        if prior is not None and prior["active"] and prior["role"] == role.value:
            current = await get_membership(conn, actor.workspace_id, user_id)
            assert current is not None
            return current
        await conn.execute(
            "insert into app.memberships (workspace_id, user_id, role, active)"
            " values (%(workspace_id)s, %(user_id)s, %(role)s, true)"
            " on conflict (workspace_id, user_id) do update set role = excluded.role, active = true",
            params,
        )
        current = await get_membership(conn, actor.workspace_id, user_id)
        assert current is not None
        await audit.record(
            conn,
            actor,
            "membership.add" if prior is None else "membership.update",
            "membership",
            user_id,
            reason=reason,
            metadata={
                "role": role.value,
                "prior_role": None if prior is None else prior["role"],
                "prior_active": None if prior is None else bool(prior["active"]),
            },
        )
    return current


async def deactivate_membership(conn: Conn, actor: ActorContext, user_id: UUID, *, reason: str) -> Membership:
    """Deactivate a member (the row is kept for audit); the last active owner is refused."""
    _require_owner(actor)
    params = {"workspace_id": actor.workspace_id, "user_id": user_id}
    async with mapped_errors():
        members = await fetch_all(conn, _LOCK_MEMBERS_SQL, params)
        prior = next((m for m in members if m["user_id"] == user_id), None)
        if prior is None:
            raise NotFound("Membership not found")
        if _active_owners(members) == {user_id}:
            raise VersionConflict("The last active owner cannot be deactivated")
        if prior["active"]:
            await conn.execute(
                "update app.memberships set active = false"
                " where workspace_id = %(workspace_id)s and user_id = %(user_id)s",
                params,
            )
            await audit.record(
                conn,
                actor,
                "membership.deactivate",
                "membership",
                user_id,
                reason=reason,
                metadata={"role": prior["role"]},
            )
        current = await get_membership(conn, actor.workspace_id, user_id)
    assert current is not None
    return current


async def create_workspace(
    conn: Conn,
    *,
    name: str,
    owner_user_id: UUID,
    request_id: str,
    display_timezone: str = "Europe/Skopje",
) -> WorkspaceBootstrap:
    """Setup scripts only: create a workspace with its first owner, on a PRIVILEGED connection.

    ``suv_backend`` cannot insert workspaces (it gets `Forbidden`). Runs in its own transaction
    (a savepoint when the connection is already inside one).
    """
    clean_name = name.strip() if isinstance(name, str) else ""
    if not 1 <= len(clean_name) <= 200:
        raise ValidationFailed("workspace name must be 1-200 characters")
    if not _TIMEZONE_RE.fullmatch(display_timezone):
        raise ValidationFailed("display_timezone must be an IANA time zone name")
    async with mapped_errors(), conn.transaction():
        row = await fetch_one(
            conn,
            "insert into app.workspaces (name, display_timezone) values (%(name)s, %(tz)s) returning id",
            {"name": clean_name, "tz": display_timezone},
        )
        assert row is not None
        workspace_id: UUID = row["id"]
        await conn.execute(
            "select set_config('app.workspace_id', %s, true)",
            (str(workspace_id),),
        )
        await conn.execute(
            "insert into app.memberships (workspace_id, user_id, role, active)"
            " values (%(workspace_id)s, %(user_id)s, 'owner', true)",
            {"workspace_id": workspace_id, "user_id": owner_user_id},
        )
        owner = await get_membership(conn, workspace_id, owner_user_id)
        assert owner is not None
        actor = ActorContext.system(workspace_id, request_id=request_id)
        await audit.record(
            conn,
            actor,
            "workspace.create",
            "workspace",
            workspace_id,
            new_version=1,
            metadata={"owner_user_id": str(owner_user_id), "display_timezone": display_timezone},
        )
    return WorkspaceBootstrap(
        workspace_id=workspace_id, name=clean_name, display_timezone=display_timezone, owner=owner
    )


__all__ = [
    "Membership",
    "WorkspaceBootstrap",
    "add_membership",
    "create_workspace",
    "deactivate_membership",
    "get_membership",
    "resolve_memberships_for_user",
]
