"""Append-only audit trail in ``ops.audit_events`` (spec sections 11, 12, 24).

`record` writes one row inside the caller's transaction, so the audit entry commits or rolls
back together with the change it describes. Who acted always comes from the verified
`ActorContext` (principal, kind, role, request id), never from request bodies. Redaction uses
the observability helpers: ``reason`` passes `observability.logging.redact` (bearer tokens,
JWTs, keys, credentials in URLs, e-mail addresses, phone numbers) and ``metadata`` passes
`observability.audit.redact_metadata` (secret-named keys lose their values, strings are
redacted, size bounded to 16 KiB). Secrets are therefore never stored.

Recording needs no scope: denials are audited too (``access.denied``). Reading the trail is an
owner/system operation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, field_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.errors import ValidationFailed
from suv_deals.observability.audit import AuditAction, redact_metadata
from suv_deals.observability.logging import redact
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors

AuditOutcome = Literal["succeeded", "denied", "failed"]

_ACTION_RE: Final = re.compile(r"^[a-z][a-z0-9_.:]{2,99}$")
_TARGET_TYPE_RE: Final = re.compile(r"^[a-z][a-z0-9_.]{2,59}$")
MAX_REASON_CHARS: Final = 1000

_LIST_SQL: Final = (
    "select id, workspace_id, actor_principal_id, actor_kind, actor_role, action, target_type,"
    " target_id, prior_version, new_version, reason, request_id, metadata, occurred_at"
    " from ops.audit_events"
    " where workspace_id = %(workspace_id)s and target_type = %(target_type)s"
    " and target_id = %(target_id)s order by occurred_at desc, id desc limit %(limit)s"
)


class AuditRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    workspace_id: UUID
    actor_principal_id: UUID
    actor_kind: Literal["user", "mcp_client", "system"]
    actor_role: Role | None = None
    action: str
    target_type: str
    target_id: UUID | None = None
    prior_version: int | None = None
    new_version: int | None = None
    reason: str | None = None
    request_id: str | None = None
    metadata: dict[str, Any]
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


def _version(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationFailed(f"{name} must be a non-negative integer")
    return value


def safe_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    cleaned = redact(str(reason)).strip()
    return cleaned[:MAX_REASON_CHARS] or None


def safe_metadata(
    actor: ActorContext, metadata: Mapping[str, Any] | None, outcome: AuditOutcome
) -> dict[str, Any]:
    data = redact_metadata(metadata)
    data["outcome"] = outcome
    if actor.client_id:
        data["actor_client_id"] = redact(actor.client_id)[:200]
    return data


async def record(  # noqa: PLR0917 - positional public contract (WP7a API)
    conn: Conn,
    actor: ActorContext,
    action: AuditAction | str,
    target_type: str,
    target_id: UUID | None,
    prior_version: int | None = None,
    new_version: int | None = None,
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    *,
    outcome: AuditOutcome = "succeeded",
) -> UUID:
    """Append one redacted audit event in the caller's transaction; returns its id."""
    action_text = str(getattr(action, "value", action))
    if not _ACTION_RE.fullmatch(action_text):
        raise ValidationFailed("audit action must be a dotted lower-case name")
    if not _TARGET_TYPE_RE.fullmatch(target_type):
        raise ValidationFailed("audit target_type must be a lower-case name")
    if outcome not in ("succeeded", "denied", "failed"):
        raise ValidationFailed("unknown audit outcome")
    params = {
        "workspace_id": actor.workspace_id,
        "principal": actor.principal_id,
        "kind": actor.principal_kind,
        "role": actor.role.value,
        "action": action_text,
        "target_type": target_type,
        "target_id": target_id,
        "prior": _version(prior_version, "prior_version"),
        "new": _version(new_version, "new_version"),
        "reason": safe_reason(reason),
        "request_id": redact(actor.request_id)[:200] or None,
        "metadata": Jsonb(safe_metadata(actor, metadata, outcome)),
    }
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into ops.audit_events (workspace_id, actor_principal_id, actor_kind, actor_role,"
            " action, target_type, target_id, prior_version, new_version, reason, request_id, metadata)"
            " values (%(workspace_id)s, %(principal)s, %(kind)s, %(role)s, %(action)s, %(target_type)s,"
            " %(target_id)s, %(prior)s, %(new)s, %(reason)s, %(request_id)s, %(metadata)s)"
            " returning id",
            params,
        )
    assert row is not None
    result: UUID = row["id"]
    return result


async def list_for_target(
    conn: Conn, actor: ActorContext, target_type: str, target_id: UUID, *, limit: int = 50
) -> list[AuditRecord]:
    """Most recent audit events for one target (owner or system only)."""
    if actor.principal_kind != "system":
        actor.require(Scope.CONFIG_ADMIN)
    if not _TARGET_TYPE_RE.fullmatch(target_type) or not 1 <= limit <= 500:
        raise ValidationFailed("invalid audit query")
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _LIST_SQL,
            {
                "workspace_id": actor.workspace_id,
                "target_type": target_type,
                "target_id": target_id,
                "limit": limit,
            },
        )
    return [AuditRecord.model_validate(r) for r in rows]
