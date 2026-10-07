"""API credentials: issue, authenticate, list and revoke (``ops.api_credentials``; spec 20, 24, 37.8).

Every SQL statement against ``ops.api_credentials`` lives here (the MCP verifier in
``mcp/auth.py`` and the ``suv-deals credentials`` commands call these functions).

Kinds and tokens
    ``static_bearer`` (``suvmcp_<64 hex>``) and ``dev_local`` (``suvdev_<64 hex>``) are MCP
    credentials: scopes narrow the member role and never include ``config:admin`` or
    ``mail:ingest`` (`BEARER_SCOPES`). ``mail_worker`` (``suvmail_<64 hex>``) is the narrow
    mailbox-worker identity of spec 37.8: exactly ``mail:ingest``, the owner role and a machine
    principal (migration ``20261007000200`` enforces the same shape). It is bound to ONE mailbox
    through ``ops.mail_worker_bindings.credential_id`` (one active binding per credential, guarded
    by the database); `active_mailbox_binding_id` resolves it after `authenticate`. A token is
    random (256 bits), returned exactly once by `issue_credential` and only its SHA-256 is stored
    (``token_hash``); the listing never returns hashes.

Authentication (`authenticate`)
    Runs inside a transaction WITHOUT a workspace (the token selects it): it sets the
    ``app.credential_hash`` GUC (RLS policy ``credential_lookup`` exposes exactly that row),
    then checks the kind, revocation, expiry (database ``clock_timestamp()``), the required
    scopes and optionally the expected workspace/principal, sets ``app.workspace_id`` and checks
    that the workspace is active. Every refusal is a `CredentialRejected` with a closed
    ``reason`` (metrics/log label only; callers answer every refusal the same way).
    Membership/role checks of user principals stay with the caller (they narrow the role).

Administration
    `issue_credential` / `revoke_credential` / `list_credentials` need the operator CLI (system
    principal) or a signed-in owner with ``config:admin``; never an MCP client. Issuing and
    revoking are audited (``credential.create`` / ``credential.revoke``) without token material.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ROLE_SCOPES, ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.errors import Forbidden, NotFound, ValidationFailed
from suv_deals.persistence import audit, workspaces
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors

CredentialKind = Literal["static_bearer", "dev_local", "mail_worker"]
CredentialPrincipalKind = Literal["user", "mcp_client"]
RejectionReason = Literal[
    "invalid_token",
    "revoked",
    "expired_token",
    "insufficient_scope",
    "wrong_workspace",
    "wrong_principal",
    "workspace_inactive",
]

CREDENTIAL_KINDS: Final[tuple[CredentialKind, ...]] = get_args(CredentialKind)
TOKEN_PREFIXES: Final[Mapping[CredentialKind, str]] = {
    "static_bearer": "suvmcp",
    "dev_local": "suvdev",
    "mail_worker": "suvmail",
}
#: Scopes an MCP (static bearer / dev) credential may carry: config:admin and mail:ingest never.
BEARER_SCOPES: Final = frozenset(s for s in Scope if s not in (Scope.CONFIG_ADMIN, Scope.MAIL_INGEST))
#: The one scope set a mailbox-worker credential carries.
MAIL_WORKER_SCOPES: Final = frozenset({Scope.MAIL_INGEST})
DEFAULT_CREDENTIAL_LIFETIME: Final = timedelta(days=90)
MAX_CREDENTIAL_LIFETIME: Final = timedelta(days=365)
DEFAULT_TOUCH_INTERVAL: Final = timedelta(minutes=1)
MAX_LIST: Final = 200

_TOKEN_RE: Final = re.compile(r"^(suvmcp|suvdev|suvmail)_([0-9a-f]{64})$")
_HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_KIND_BY_PREFIX: Final[Mapping[str, CredentialKind]] = {v: k for k, v in TOKEN_PREFIXES.items()}
_SCOPE_VALUES: Final = frozenset(s.value for s in Scope)
_TEXT_CONTROL_RE: Final = re.compile("[\\x00-\\x1f\\x7f\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069]")
_FROZEN = ConfigDict(frozen=True, extra="forbid")


class CredentialRejected(Exception):
    """The presented credential is not acceptable; ``reason`` is a label, never shown to callers."""

    def __init__(self, reason: RejectionReason) -> None:
        super().__init__(reason)
        self.reason: RejectionReason = reason


# --------------------------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------------------------


def hash_token(token: str) -> str:
    """SHA-256 hex of a presented credential (what ``ops.api_credentials.token_hash`` stores)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_kind(token: object) -> CredentialKind | None:
    """The kind a well-formed token claims by its prefix (``None``: not a credential token)."""
    match = _TOKEN_RE.fullmatch(token) if isinstance(token, str) else None
    return None if match is None else _KIND_BY_PREFIX[match.group(1)]


def allowed_scopes(kind: CredentialKind, role: Role) -> frozenset[Scope]:
    """Scopes a credential of ``kind`` acting as ``role`` may carry."""
    if kind == "mail_worker":
        return MAIL_WORKER_SCOPES if role == Role.OWNER else frozenset()
    return BEARER_SCOPES & ROLE_SCOPES[role]


# --------------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    """A newly created credential. ``token`` is shown exactly once and never stored."""

    credential_id: UUID
    workspace_id: UUID
    principal_id: UUID
    principal_kind: CredentialPrincipalKind
    role: Role
    scopes: tuple[Scope, ...]
    kind: CredentialKind
    token_prefix: str
    expires_at: datetime
    token: SecretStr


class CredentialRecord(BaseModel):
    """Credential metadata for listings (never the token or its hash)."""

    model_config = _FROZEN

    credential_id: UUID
    workspace_id: UUID
    principal_id: UUID
    principal_kind: CredentialPrincipalKind
    role: Role
    credential_kind: CredentialKind
    token_prefix: str | None
    scopes: tuple[str, ...]
    label: str
    expires_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime

    def active(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > ensure_utc(now)


class VerifiedCredential(BaseModel):
    """An authenticated credential (no token material). ``scopes`` are the stored scopes; the
    caller narrows them further (role, surface)."""

    model_config = _FROZEN

    credential_id: UUID
    workspace_id: UUID
    principal_id: UUID
    principal_kind: CredentialPrincipalKind
    role: Role
    credential_kind: CredentialKind
    scopes: frozenset[Scope]
    expires_at: datetime
    last_used_at: datetime | None


# --------------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------------

_LOOKUP_SQL: Final = (
    "select id, workspace_id, principal_id, principal_kind, role, credential_kind, scopes, expires_at,"
    " revoked_at, last_used_at, clock_timestamp() as now from ops.api_credentials"
    " where token_hash = %(hash)s"
)
_WORKSPACE_ACTIVE_SQL: Final = "select active from app.workspaces where id = %(ws)s"
_TOUCH_SQL: Final = (
    "update ops.api_credentials set last_used_at = clock_timestamp()"
    " where workspace_id = %(ws)s and id = %(id)s"
    " and (last_used_at is null or last_used_at < clock_timestamp() - %(interval)s::interval)"
)


async def authenticate(
    conn: Conn,
    token: str,
    *,
    kinds: Collection[CredentialKind],
    required_scopes: Iterable[Scope] = (),
    workspace_id: UUID | None = None,
    principal_id: UUID | None = None,
) -> VerifiedCredential:
    """Look a presented token up by its hash and check it (see module docstring).

    Run in a transaction opened WITHOUT a workspace (``db.transaction()``); on success the
    transaction's ``app.workspace_id`` names the credential's workspace. Raises
    `CredentialRejected`; database failures surface as ``AppError`` (callers map them to a
    retryable dependency error, never to a rejection).
    """
    allowed = frozenset(kinds)
    kind = token_kind(token)
    if kind is None or kind not in allowed:
        raise CredentialRejected("invalid_token")
    digest = hash_token(token)
    async with mapped_errors():
        await conn.execute("select set_config('app.credential_hash', %s, true)", (digest,))
        row = await fetch_one(conn, _LOOKUP_SQL, {"hash": digest})
        if row is None or row["credential_kind"] != kind:
            raise CredentialRejected("invalid_token")
        if row["revoked_at"] is not None:
            raise CredentialRejected("revoked")
        expires_at = ensure_utc(row["expires_at"])
        if expires_at <= ensure_utc(row["now"]):
            raise CredentialRejected("expired_token")
        stored = frozenset(Scope(s) for s in row["scopes"] if s in _SCOPE_VALUES)
        if not frozenset(required_scopes) <= stored:
            raise CredentialRejected("insufficient_scope")
        ws: UUID = row["workspace_id"]
        if workspace_id is not None and ws != workspace_id:
            raise CredentialRejected("wrong_workspace")
        if principal_id is not None and row["principal_id"] != principal_id:
            raise CredentialRejected("wrong_principal")
        await conn.execute("select set_config('app.workspace_id', %s, true)", (str(ws),))
        workspace = await fetch_one(conn, _WORKSPACE_ACTIVE_SQL, {"ws": ws})
        if workspace is None or not workspace["active"]:
            raise CredentialRejected("workspace_inactive")
    return VerifiedCredential(
        credential_id=row["id"],
        workspace_id=ws,
        principal_id=row["principal_id"],
        principal_kind=row["principal_kind"],
        role=Role(row["role"]),
        credential_kind=kind,
        scopes=stored,
        expires_at=expires_at,
        last_used_at=None if row["last_used_at"] is None else ensure_utc(row["last_used_at"]),
    )


async def touch_last_used(
    conn: Conn, credential: VerifiedCredential, *, interval: timedelta = DEFAULT_TOUCH_INTERVAL
) -> None:
    """Record use (at most once per ``interval``, so busy clients do not write on every request)."""
    async with mapped_errors():
        await conn.execute(
            _TOUCH_SQL, {"ws": credential.workspace_id, "id": credential.credential_id, "interval": interval}
        )


async def active_mailbox_binding_id(conn: Conn, credential: VerifiedCredential) -> UUID | None:
    """The ACTIVE ``ops.mail_worker_bindings`` row a mailbox-worker credential is bound to (``None``:
    unbound or revoked). Call after `authenticate` (the workspace GUC is set)."""
    if credential.credential_kind != "mail_worker" and credential.scopes != MAIL_WORKER_SCOPES:
        return None
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select id from ops.mail_worker_bindings where workspace_id = %(ws)s"
            " and credential_id = %(id)s and state = 'active'",
            {"ws": credential.workspace_id, "id": credential.credential_id},
        )
    return None if row is None else row["id"]


# --------------------------------------------------------------------------------------------
# Administration
# --------------------------------------------------------------------------------------------


def require_credential_admin(actor: ActorContext) -> None:
    """Credential administration: the operator CLI (system actor) or a signed-in owner with
    ``config:admin``; never an MCP client."""
    if actor.principal_kind == "system":
        return
    actor.require(Scope.CONFIG_ADMIN)
    if actor.principal_kind != "user" or actor.role != Role.OWNER:
        raise Forbidden("Only the owner may manage API credentials")


def _bounded_text(value: object, field: str, *, minimum: int, maximum: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not minimum <= len(text) <= maximum or _TEXT_CONTROL_RE.search(text):
        raise ValidationFailed(
            f"{field} must be {minimum}-{maximum} printable characters", details={"fields": [field]}
        )
    return text


async def issue_credential(
    conn: Conn,
    actor: ActorContext,
    *,
    principal_id: UUID,
    principal_kind: CredentialPrincipalKind,
    role: Role,
    scopes: Iterable[Scope],
    label: str,
    kind: CredentialKind = "static_bearer",
    lifetime: timedelta = DEFAULT_CREDENTIAL_LIFETIME,
) -> IssuedCredential:
    """Create one credential in ``actor``'s workspace and return its token ONCE (hash stored only).

    Rules (``VALIDATION_ERROR`` naming the field): ``kind`` is a known kind; ``scopes`` are
    non-empty and allowed for the kind and role (`allowed_scopes`: MCP kinds never carry
    ``config:admin``/``mail:ingest``; ``mail_worker`` carries exactly ``mail:ingest`` as owner);
    ``principal_kind`` is ``user`` or ``mcp_client`` (``mail_worker``: ``mcp_client`` only);
    ``0 < lifetime <= 365 days``; ``label`` is 1-120 printable characters. A user credential needs
    an active membership whose role allows the scopes (`NotFound` otherwise); a machine
    credential's ``principal_id`` must not be a workspace member (no identity reuse). Run inside
    ``unit_of_work(db, actor)``; audited as ``credential.create`` (prefix, never the token).
    """
    require_credential_admin(actor)
    if kind not in TOKEN_PREFIXES:
        raise ValidationFailed("unknown credential kind", details={"fields": ["kind"]})
    try:
        wanted = frozenset(Scope(s) for s in scopes)
        role = Role(role)
    except ValueError:
        raise ValidationFailed("unknown scope or role", details={"fields": ["scopes"]}) from None
    permitted = allowed_scopes(kind, role)
    if not wanted or not wanted <= permitted or (kind == "mail_worker" and wanted != MAIL_WORKER_SCOPES):
        raise ValidationFailed(
            "scopes must be allowed for the credential kind and role", details={"fields": ["scopes"]}
        )
    if principal_kind not in ("user", "mcp_client") or (
        kind == "mail_worker" and principal_kind != "mcp_client"
    ):
        raise ValidationFailed(
            "principal_kind is not allowed for this credential", details={"fields": ["principal_kind"]}
        )
    if not timedelta(0) < lifetime <= MAX_CREDENTIAL_LIFETIME:
        raise ValidationFailed(
            "lifetime must be positive and at most 365 days", details={"fields": ["lifetime"]}
        )
    text = _bounded_text(label, "label", minimum=1, maximum=120)
    membership = await workspaces.get_membership(conn, actor.workspace_id, principal_id)
    if principal_kind == "user":
        if membership is None or not membership.active or not wanted <= ROLE_SCOPES[membership.role]:
            raise NotFound("No active membership allows these scopes")
    elif membership is not None:
        # A machine credential must never act under a member's identity (claims, idempotency
        # records, notes and subscriptions are keyed by principal id).
        raise ValidationFailed(
            "principal_id of a machine credential must not be a workspace member",
            details={"fields": ["principal_id"]},
        )
    random_part = secrets.token_hex(32)
    prefix = TOKEN_PREFIXES[kind]
    token = f"{prefix}_{random_part}"
    token_prefix = f"{prefix}_{random_part[:6]}"
    ordered = tuple(s for s in Scope if s in wanted)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into ops.api_credentials (workspace_id, principal_id, principal_kind, role,"
            " credential_kind, token_hash, token_prefix, scopes, label, expires_at, created_by)"
            " values (%(ws)s, %(principal)s, %(kind)s, %(role)s, %(credential_kind)s, %(hash)s,"
            " %(prefix)s, %(scopes)s, %(label)s, clock_timestamp() + %(lifetime)s::interval, %(by)s)"
            " returning id, expires_at",
            {
                "ws": actor.workspace_id,
                "principal": principal_id,
                "kind": principal_kind,
                "role": role.value,
                "credential_kind": kind,
                "hash": hash_token(token),
                "prefix": token_prefix,
                "scopes": [s.value for s in ordered],
                "label": text,
                "lifetime": lifetime,
                "by": actor.principal_id,
            },
        )
        assert row is not None
        await audit.record(
            conn,
            actor,
            "credential.create",
            "api_credential",
            row["id"],
            None,
            1,
            metadata={
                "credential_kind": kind,
                "principal_kind": principal_kind,
                "role": role.value,
                "scopes": [s.value for s in ordered],
                "token_prefix": token_prefix,
            },
        )
    return IssuedCredential(
        credential_id=row["id"],
        workspace_id=actor.workspace_id,
        principal_id=principal_id,
        principal_kind=principal_kind,
        role=role,
        scopes=ordered,
        kind=kind,
        token_prefix=token_prefix,
        expires_at=ensure_utc(row["expires_at"]),
        token=SecretStr(token),
    )


async def revoke_credential(conn: Conn, actor: ActorContext, credential_id: UUID, *, reason: str) -> bool:
    """Revoke one credential of ``actor``'s workspace now; ``False`` when it was already revoked.

    The next request with that token is refused. Unknown or foreign ids are `NotFound`. Audited as
    ``credential.revoke`` with the reason.
    """
    require_credential_admin(actor)
    text = _bounded_text(reason, "reason", minimum=3, maximum=500)
    async with mapped_errors():
        existing = await fetch_one(
            conn,
            "select id, revoked_at from ops.api_credentials where workspace_id = %(ws)s and id = %(id)s"
            " for update",
            {"ws": actor.workspace_id, "id": credential_id},
        )
        if existing is None:
            raise NotFound("Credential not found")
        if existing["revoked_at"] is not None:
            return False
        await conn.execute(
            "update ops.api_credentials set revoked_at = clock_timestamp(), revoked_by = %(by)s,"
            " revoke_reason = %(reason)s where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": credential_id, "by": actor.principal_id, "reason": text},
        )
        await audit.record(conn, actor, "credential.revoke", "api_credential", credential_id, reason=text)
    return True


_LIST_SQL: Final = (
    "select id, workspace_id, principal_id, principal_kind, role, credential_kind, token_prefix, scopes,"
    " label, expires_at, revoked_at, last_used_at, created_at"
    " from ops.api_credentials"
    " where workspace_id = %(ws)s"
    " and (%(all)s or (revoked_at is null and expires_at > clock_timestamp()))"
    " and (%(kind)s::text is null or credential_kind = %(kind)s)"
    " order by created_at desc, id"
    " limit %(limit)s"
)


async def list_credentials(
    conn: Conn,
    actor: ActorContext,
    *,
    include_inactive: bool = False,
    kind: CredentialKind | None = None,
    limit: int = MAX_LIST,
) -> list[CredentialRecord]:
    """Credential metadata of ``actor``'s workspace, newest first (never tokens or hashes).

    Active (unrevoked, unexpired) credentials only unless ``include_inactive``.
    """
    require_credential_admin(actor)
    if kind is not None and kind not in TOKEN_PREFIXES:
        raise ValidationFailed("unknown credential kind", details={"fields": ["kind"]})
    if not 1 <= limit <= MAX_LIST:
        raise ValidationFailed("limit must be between 1 and 200", details={"fields": ["limit"]})
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _LIST_SQL,
            {"ws": actor.workspace_id, "all": include_inactive, "kind": kind, "limit": limit},
        )
    return [_record(r) for r in rows]


def _record(row: Mapping[str, Any]) -> CredentialRecord:
    return CredentialRecord(
        credential_id=row["id"],
        workspace_id=row["workspace_id"],
        principal_id=row["principal_id"],
        principal_kind=row["principal_kind"],
        role=Role(row["role"]),
        credential_kind=row["credential_kind"],
        token_prefix=row["token_prefix"],
        scopes=tuple(row["scopes"]),
        label=row["label"],
        expires_at=ensure_utc(row["expires_at"]),
        revoked_at=None if row["revoked_at"] is None else ensure_utc(row["revoked_at"]),
        last_used_at=None if row["last_used_at"] is None else ensure_utc(row["last_used_at"]),
        created_at=ensure_utc(row["created_at"]),
    )


def is_token_hash(value: object) -> bool:
    """Whether ``value`` has the shape of a stored token hash (64 lower-case hex digits)."""
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


__all__ = [
    "BEARER_SCOPES",
    "CREDENTIAL_KINDS",
    "DEFAULT_CREDENTIAL_LIFETIME",
    "DEFAULT_TOUCH_INTERVAL",
    "MAIL_WORKER_SCOPES",
    "MAX_CREDENTIAL_LIFETIME",
    "TOKEN_PREFIXES",
    "CredentialKind",
    "CredentialPrincipalKind",
    "CredentialRecord",
    "CredentialRejected",
    "IssuedCredential",
    "RejectionReason",
    "VerifiedCredential",
    "active_mailbox_binding_id",
    "allowed_scopes",
    "authenticate",
    "hash_token",
    "is_token_hash",
    "issue_credential",
    "list_credentials",
    "require_credential_admin",
    "revoke_credential",
    "token_kind",
    "touch_last_used",
]
