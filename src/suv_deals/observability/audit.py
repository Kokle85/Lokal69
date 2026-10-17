"""Audit records for administrative and review actions (spec sections 12, 24).

`AuditEvent` is the immutable, persistence-ready shape: who (principal, kind,
role, client id), did what (`action` on `target_type`/`target_id`), between
which versions, why, under which request id and when (UTC). Metadata is always
passed through `redact_metadata`: secret-named keys lose their values, strings
are redacted, and the JSON size is bounded. Persistence (an append-only table
with no UPDATE/DELETE grants) is implemented by the persistence package.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from suv_deals.clock import Clock, ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role
from suv_deals.observability.logging import redact, redact_value

MAX_METADATA_BYTES: Final = 16 * 1024
_ACTION_PATTERN: Final = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}$"
_TARGET_TYPE_PATTERN: Final = r"^[a-z][a-z0-9_]{0,63}$"
_TARGET_ID_PATTERN: Final = r"^[A-Za-z0-9_.:-]{1,128}$"


class AuditAction(StrEnum):
    """Known actions; other dotted lower-case actions are accepted for forward compatibility."""

    REVIEW_CLAIM = "review.claim"
    REVIEW_RELEASE = "review.release"
    REVIEW_SUBMIT = "review.submit"
    NOTE_ADD = "note.add"
    RECHECK_REQUEST = "recheck.request"
    SOURCE_PAUSE = "source.pause"
    SOURCE_ACTIVATE = "source.activate"
    CONFIG_CHANGE = "config.change"
    TAX_RULES_APPROVE = "tax_rules.approve"
    EVENT_SUBSCRIPTION_CREATE = "event_subscription.create"
    EVENT_SUBSCRIPTION_REFRESH = "event_subscription.refresh"
    EVENT_SUBSCRIPTION_VERIFY = "event_subscription.verify"
    EVENT_SUBSCRIPTION_UNSUBSCRIBE = "event_subscription.unsubscribe"
    EVENT_SUBSCRIPTION_REVOKE = "event_subscription.revoke"
    SECRET_ROTATE = "secret.rotate"  # noqa: S105 - an action name, not a secret
    NOTIFICATION_ROUTE_CHANGE = "notification.route_change"
    ACCESS_DENIED = "access.denied"


AuditOutcome = Literal["succeeded", "denied", "failed"]


def redact_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Redacted, JSON-safe, size-bounded copy. Never contains secret values."""
    if not metadata:
        return {}
    cleaned = redact_value(dict(metadata))
    if not isinstance(cleaned, dict):  # pragma: no cover - redact_value keeps mappings as dicts
        return {}
    encoded = json.dumps(cleaned, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        return {"truncated": True, "keys": sorted(cleaned)[:50]}
    result: dict[str, Any] = json.loads(encoded)
    return result


class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    audit_id: UUID
    workspace_id: UUID
    actor_principal_id: UUID
    actor_kind: Literal["user", "mcp_client", "system"]
    actor_role: Role
    actor_client_id: str | None = Field(default=None, max_length=200)
    action: str = Field(pattern=_ACTION_PATTERN, max_length=128)
    target_type: str = Field(pattern=_TARGET_TYPE_PATTERN)
    target_id: str = Field(pattern=_TARGET_ID_PATTERN)
    prior_version: int | None = Field(default=None, ge=0)
    new_version: int | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, max_length=1000)
    request_id: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    outcome: AuditOutcome = "succeeded"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("reason")
    @classmethod
    def _redact_reason(cls, value: str | None) -> str | None:
        return None if value is None else redact(value.strip())[:1000] or None

    @field_validator("metadata")
    @classmethod
    def _redact_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return redact_metadata(value)

    def to_record(self) -> dict[str, Any]:
        """JSON-safe mapping for the persistence layer."""
        return self.model_dump(mode="json")


def build_audit_event(
    actor: ActorContext,
    *,
    action: AuditAction | str,
    target_type: str,
    target_id: UUID | str,
    clock: Clock,
    prior_version: int | None = None,
    new_version: int | None = None,
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    outcome: AuditOutcome = "succeeded",
    audit_id: UUID | None = None,
) -> AuditEvent:
    """Build an audit event from the verified actor (never from request-body actor fields)."""
    return AuditEvent(
        audit_id=audit_id or uuid4(),
        workspace_id=actor.workspace_id,
        actor_principal_id=actor.principal_id,
        actor_kind=actor.principal_kind,
        actor_role=actor.role,
        actor_client_id=actor.client_id,
        action=str(getattr(action, "value", action)),
        target_type=target_type,
        target_id=str(target_id),
        prior_version=prior_version,
        new_version=new_version,
        reason=reason,
        request_id=actor.request_id,
        occurred_at=clock.now(),
        outcome=outcome,
        metadata=dict(metadata or {}),
    )
