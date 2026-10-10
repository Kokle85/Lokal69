"""Audit events: verified actor, versions, UTC time, and metadata that never contains secrets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from suv_deals.clock import FrozenClock
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.observability.audit import (
    MAX_METADATA_BYTES,
    AuditAction,
    AuditEvent,
    build_audit_event,
    redact_metadata,
)
from suv_deals.observability.logging import REDACTED

NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)
WS = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ACTOR = ActorContext(
    workspace_id=WS,
    principal_id=UUID("99999999-9999-4999-8999-999999999999"),
    principal_kind="mcp_client",
    role=Role.REVIEWER,
    scopes=frozenset({Scope.REVIEWS_READ, Scope.EVENTS_SUBSCRIBE}),
    request_id="req-audit-1",
    display_name="Synthetic Reviewer",
    client_id="client-synthetic",
)


def test_build_from_verified_actor() -> None:
    event = build_audit_event(
        ACTOR,
        action=AuditAction.EVENT_SUBSCRIPTION_CREATE,
        target_type="event_subscription",
        target_id="sub_0123456789abcdef0123456789abcdef",
        clock=FrozenClock(NOW),
        new_version=1,
        reason="dot subscribed to the primary queue",
        metadata={
            "event": "review.pending.v1",
            "callback_host": "receiver.example.com",
            "secret": "whsec_abc",
            "delivery": {"url": "https://receiver.example.com/cb?token=t1", "secret": "whsec_def"},
            "ttl_ms": 3_600_000,
        },
    )
    assert event.workspace_id == WS
    assert event.actor_principal_id == ACTOR.principal_id
    assert event.actor_kind == "mcp_client"
    assert event.actor_role is Role.REVIEWER
    assert event.actor_client_id == "client-synthetic"
    assert event.request_id == "req-audit-1"
    assert event.occurred_at == NOW
    assert event.action == "event_subscription.create"
    assert event.metadata["secret"] == REDACTED
    assert event.metadata["delivery"]["secret"] == REDACTED
    assert "t1" not in event.metadata["delivery"]["url"]
    assert event.metadata["ttl_ms"] == 3_600_000
    record = event.to_record()
    text = str(record)
    assert "whsec_abc" not in text and "whsec_def" not in text
    assert "Synthetic Reviewer" not in text  # no display names (PII) in audit rows
    assert record["occurred_at"] == "2026-10-06T10:00:00Z"


def test_reason_is_redacted_and_bounded() -> None:
    event = build_audit_event(
        ACTOR,
        action="review.submit",
        target_type="review_case",
        target_id=UUID("44444444-4444-4444-8444-444444444444"),
        clock=FrozenClock(NOW),
        prior_version=3,
        new_version=4,
        reason="  seller asked to call +49 151 23456789 or mail seller@example.com  ",
    )
    assert event.reason is not None
    assert "23456789" not in event.reason
    assert "seller@example.com" not in event.reason
    assert event.target_id == "44444444-4444-4444-8444-444444444444"
    assert event.prior_version == 3 and event.new_version == 4


def test_metadata_size_is_bounded() -> None:
    big = {f"k{i}": "x" * 1000 for i in range(40)}
    out = redact_metadata(big)
    assert out["truncated"] is True
    assert len(str(out)) < MAX_METADATA_BYTES


def test_metadata_handles_secret_objects_and_empty() -> None:
    assert redact_metadata(None) == {}
    assert redact_metadata({"token": SecretStr("x"), "n": 1}) == {"token": REDACTED, "n": 1}


@pytest.mark.parametrize(
    "change",
    [
        {"action": "Review Submit"},
        {"action": "review"},
        {"target_type": "Review-Case"},
        {"target_id": "has space"},
        {"target_id": ""},
        {"prior_version": -1},
        {"request_id": ""},
        {"occurred_at": datetime(2026, 10, 6, 10, 0)},
        {"unexpected": "field"},
        {"actor_kind": "root"},
    ],
)
def test_invalid_events_rejected(change: dict[str, Any]) -> None:
    base: dict[str, Any] = {
        "audit_id": UUID("12121212-1212-4121-8121-121212121212"),
        "workspace_id": WS,
        "actor_principal_id": ACTOR.principal_id,
        "actor_kind": "user",
        "actor_role": Role.OWNER,
        "action": "config.change",
        "target_type": "config_revision",
        "target_id": "rev-1",
        "request_id": "req-1",
        "occurred_at": NOW,
    }
    with pytest.raises((ValidationError, ValueError)):
        AuditEvent(**{**base, **change})


def test_occurred_at_is_normalized_to_utc_and_frozen() -> None:
    skopje = timezone(timedelta(hours=2))
    event = build_audit_event(
        ACTOR,
        action=AuditAction.SOURCE_PAUSE,
        target_type="source",
        target_id="mobile_de",
        clock=FrozenClock(datetime(2026, 10, 6, 12, 0, tzinfo=skopje)),
        outcome="denied",
    )
    assert event.occurred_at == NOW
    assert event.occurred_at.tzinfo is UTC
    assert event.outcome == "denied"
    with pytest.raises(ValidationError):
        event.action = "x.y"  # type: ignore[misc]
