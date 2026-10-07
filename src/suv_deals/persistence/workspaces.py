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
- `bootstrap_owner_membership` is its counterpart for an EXISTING workspace (``suv-deals bootstrap
  owner --workspace``): on the same privileged connection, with no signed-in owner, it adds or
  reactivates an owner membership and records ``membership.bootstrap_owner`` (prior role and
  activity) in one transaction. `find_auth_users`, `get_workspace` and `find_owned_workspace`
  are the read-only privileged lookups the bootstrap needs (it never creates or changes Auth users).

Lock order: membership writes lock the workspace's membership rows (``FOR UPDATE``, ordered by
user id) and write the audit event last; no other chain table is touched.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Final, Literal
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


# --------------------------------------------------------------------------------------------
# Privileged bootstrap helpers (maintenance connection, no signed-in owner)
# --------------------------------------------------------------------------------------------

_EMAIL_RE: Final = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")
BOOTSTRAP_OWNER_REASON: Final = "operator bootstrap of the workspace owner"


class WorkspaceInfo(BaseModel):
    """A workspace as seen by the privileged bootstrap (no member data)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    name: str
    display_timezone: str
    active: bool
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class OwnerBootstrap(BaseModel):
    """Result of `bootstrap_owner_membership`: the owner membership and what it was before."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    membership: Membership
    prior_role: Role | None
    prior_active: bool | None
    outcome: Literal["added", "reactivated", "promoted", "confirmed"]


async def find_auth_users(
    conn: Conn, *, user_id: UUID | None = None, email: str | None = None, limit: int = 2
) -> list[UUID]:
    """PRIVILEGED: ids of existing Supabase Auth users by id or (case-insensitive) e-mail.

    Exactly one of ``user_id`` / ``email`` is required. At most ``limit`` ids are returned so a
    caller can detect an ambiguous e-mail without listing users. Read-only; never creates users.
    """
    if (user_id is None) == (email is None):
        raise ValidationFailed(
            "pass exactly one of user_id or email", details={"fields": ["user_id", "email"]}
        )
    if not 1 <= limit <= 10:
        raise ValidationFailed("limit must be between 1 and 10", details={"fields": ["limit"]})
    async with mapped_errors():
        if user_id is not None:
            rows = await fetch_all(conn, "select id from auth.users where id = %(id)s", {"id": user_id})
        else:
            clean = (email or "").strip()
            if not _EMAIL_RE.fullmatch(clean):
                raise ValidationFailed("email is not an e-mail address", details={"fields": ["email"]})
            rows = await fetch_all(
                conn,
                "select id from auth.users where lower(email) = lower(%(email)s) order by id limit %(limit)s",
                {"email": clean, "limit": limit},
            )
    return [r["id"] for r in rows]


async def get_workspace(conn: Conn, workspace_id: UUID) -> WorkspaceInfo | None:
    """PRIVILEGED: one workspace (active or not) by id, or ``None``."""
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select id, name, display_timezone, active, created_at from app.workspaces where id = %(id)s",
            {"id": workspace_id},
        )
    return None if row is None else WorkspaceInfo.model_validate(row)


async def find_owned_workspace(conn: Conn, *, owner_user_id: UUID, name: str) -> UUID | None:
    """PRIVILEGED: the oldest ACTIVE workspace called ``name`` that ``owner_user_id`` actively owns.

    Lets a re-run of the bootstrap refuse to create a second workspace with the same name.
    """
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select w.id from app.workspaces w join app.memberships m"
            " on m.workspace_id = w.id and m.user_id = %(user_id)s and m.role = 'owner' and m.active"
            " where w.active and w.name = %(name)s order by w.created_at, w.id limit 1",
            {"user_id": owner_user_id, "name": name.strip() if isinstance(name, str) else ""},
        )
    return None if row is None else row["id"]


async def bootstrap_owner_membership(
    conn: Conn,
    *,
    workspace_id: UUID,
    owner_user_id: UUID,
    request_id: str,
    reason: str = BOOTSTRAP_OWNER_REASON,
) -> OwnerBootstrap:
    """PRIVILEGED: add or reactivate ``owner_user_id`` as an owner of an EXISTING active workspace.

    Mirrors `create_workspace` for a workspace that already exists and has no signed-in owner to
    call `add_membership` (lost or never-created owner membership). Runs in its own transaction (a
    savepoint inside one): the workspace row is locked (`NotFound` when unknown or inactive), then
    the membership rows (same order as `add_membership`), the membership is upserted as an active
    owner, and ``membership.bootstrap_owner`` is audited (every call, also a confirmation) with
    the prior role/activity. ``owner_user_id`` must exist in ``auth.users`` (otherwise `NotFound`).
    """
    clean_reason = reason.strip() if isinstance(reason, str) else ""
    if not 1 <= len(clean_reason) <= 500:
        raise ValidationFailed("reason must be 1-500 characters", details={"fields": ["reason"]})
    params = {"workspace_id": workspace_id, "user_id": owner_user_id}
    async with mapped_errors(), conn.transaction():
        workspace = await fetch_one(
            conn,
            "select active from app.workspaces where id = %(workspace_id)s for no key update",
            params,
        )
        if workspace is None or not workspace["active"]:
            raise NotFound("The workspace is unknown or inactive")
        await conn.execute("select set_config('app.workspace_id', %s, true)", (str(workspace_id),))
        members = await fetch_all(conn, _LOCK_MEMBERS_SQL, params)
        prior = next((m for m in members if m["user_id"] == owner_user_id), None)
        await conn.execute(
            "insert into app.memberships (workspace_id, user_id, role, active)"
            " values (%(workspace_id)s, %(user_id)s, 'owner', true)"
            " on conflict (workspace_id, user_id) do update set role = 'owner', active = true",
            params,
        )
        membership = await get_membership(conn, workspace_id, owner_user_id)
        assert membership is not None
        await audit.record(
            conn,
            ActorContext.system(workspace_id, request_id=request_id),
            "membership.bootstrap_owner",
            "membership",
            owner_user_id,
            reason=clean_reason,
            metadata={
                "role": Role.OWNER.value,
                "prior_role": None if prior is None else prior["role"],
                "prior_active": None if prior is None else bool(prior["active"]),
            },
        )
    if prior is None:
        outcome: Literal["added", "reactivated", "promoted", "confirmed"] = "added"
    elif not prior["active"]:
        outcome = "reactivated"
    elif prior["role"] != Role.OWNER.value:
        outcome = "promoted"
    else:
        outcome = "confirmed"
    return OwnerBootstrap(
        membership=membership,
        prior_role=None if prior is None else Role(prior["role"]),
        prior_active=None if prior is None else bool(prior["active"]),
        outcome=outcome,
    )


__all__ = [
    "BOOTSTRAP_OWNER_REASON",
    "Membership",
    "OwnerBootstrap",
    "WorkspaceBootstrap",
    "WorkspaceInfo",
    "add_membership",
    "bootstrap_owner_membership",
    "create_workspace",
    "deactivate_membership",
    "find_auth_users",
    "find_owned_workspace",
    "get_membership",
    "get_workspace",
    "resolve_memberships_for_user",
]
