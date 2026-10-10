"""The shared activation-canary domain (F3, wave D2): fixed text, Message-ID, hashes, wire models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from suv_deals.domain.canary import (
    CANARY_INTENT_TTL,
    CanaryIntent,
    CanaryReplyReport,
    CanaryReport,
    canary_body,
    canary_body_hash,
    canary_message_id,
    canary_subject,
    canary_target_hash,
    parse_canary_message_id,
)
from suv_deals.integrations.mime_builder import parse_inquiry_message_id

CID = uuid.UUID("12345678-1234-4234-8234-123456789abc")
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
SENDER = "owner-inquiries@example.invalid"


def _intent(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "canary_id": str(CID),
        "mailbox_binding_id": str(uuid.uuid4()),
        "binding_id": str(uuid.uuid4()),
        "binding_version": 1,
        "account_id": "synthetic-account",
        "from_address": SENDER,
        "from_display_name": "Synthetic Owner",
        "target_address_hash": canary_target_hash("owner-test@example.invalid"),
        "subject": canary_subject(CID),
        "body_text": canary_body(CID),
        "rfc_message_id": canary_message_id(CID, SENDER),
        "body_hash": canary_body_hash(CID),
        "created_at": NOW.isoformat(),
        "not_after": (NOW + timedelta(hours=12)).isoformat(),
    }
    data.update(overrides)
    return data


def test_the_canary_text_is_fixed_neutral_and_names_its_reference() -> None:
    body = canary_body(CID)
    assert f"canary-{CID}" in body and f"canary-{CID}" in canary_subject(CID)
    lowered = body.lower()
    for word in ("vehicle", "price", "fahrzeug", "@", "http"):
        assert word not in lowered
    assert canary_body(uuid.uuid4()) != body  # bound to its id


def test_message_id_round_trips_and_is_never_an_inquiry_message_id() -> None:
    value = canary_message_id(CID, SENDER)
    assert value == f"<canary-{CID}@example.invalid>"
    assert parse_canary_message_id(value) == CID
    assert parse_inquiry_message_id(value) is None
    assert parse_canary_message_id(f"<inquiry-{CID}.1@example.invalid>") is None
    assert parse_canary_message_id(f"<canary-{CID}@EXAMPLE.invalid>") is None
    with pytest.raises(ValueError, match="domain"):
        canary_message_id(CID, "no-domain")


def test_target_hash_is_case_insensitive_and_canonical() -> None:
    assert canary_target_hash(" Owner-Test@Example.Invalid ") == canary_target_hash(
        "owner-test@example.invalid"
    )
    assert canary_target_hash("a@example.invalid") != canary_target_hash("b@example.invalid")


@pytest.mark.parametrize(
    "override",
    [
        {"subject": "Mailbox activation test - please wire money"},
        {"body_text": canary_body(CID) + "Also, what is your lowest price?\n"},
        {"body_hash": "0" * 64},
        {"rfc_message_id": canary_message_id(uuid.uuid4(), SENDER)},
        {"not_after": (NOW + CANARY_INTENT_TTL + timedelta(seconds=1)).isoformat()},
        {"not_after": NOW.isoformat()},
        {"target_address_hash": "not-a-hash"},
        {"unexpected": True},
    ],
)
def test_a_canary_intent_is_exactly_the_fixed_rendering(override: dict[str, Any]) -> None:
    CanaryIntent.model_validate(_intent())
    with pytest.raises(ValidationError):
        CanaryIntent.model_validate(_intent(**override))


def test_expiry_uses_the_window_or_the_server_flag() -> None:
    intent = CanaryIntent.model_validate(_intent())
    assert not intent.is_expired(NOW)
    assert intent.is_expired(NOW + timedelta(hours=12))
    assert CanaryIntent.model_validate(_intent(expired=True)).is_expired(NOW)


def test_reports_are_consistent() -> None:
    base = {
        "canary_id": str(CID),
        "mailbox_binding_id": str(uuid.uuid4()),
        "worker_id": "desktop-1.s0000000000000001",
        "reported_at": NOW.isoformat(),
    }
    CanaryReport.model_validate({**base, "state": "refused_before_send", "refusal_reason": "intent_expired"})
    CanaryReport.model_validate({**base, "state": "sent_items_confirmed", "sent_items_present": True})
    for bad in (
        {"state": "refused_before_send"},
        {"state": "submitted_to_outbox", "refusal_reason": "kill_switch"},
        {"state": "submitted_to_outbox", "sent_items_present": True},
        {"state": "sent_items_confirmed"},
    ):
        with pytest.raises(ValidationError):
            CanaryReport.model_validate({**base, **bad})


def test_a_reply_report_must_reference_its_canary() -> None:
    base = {
        "canary_id": str(CID),
        "mailbox_binding_id": str(uuid.uuid4()),
        "worker_id": "desktop-1",
        "internet_message_id": "<owner-reply@example.invalid>",
        "from_address_hash": "a" * 64,
        "received_at": NOW.isoformat(),
    }
    CanaryReplyReport.model_validate({**base, "references": [canary_message_id(CID, SENDER)]})
    with pytest.raises(ValidationError, match="does not reference"):
        CanaryReplyReport.model_validate({**base, "in_reply_to": [canary_message_id(uuid.uuid4(), SENDER)]})
    with pytest.raises(ValidationError):
        CanaryReplyReport.model_validate(
            {**base, "in_reply_to": [canary_message_id(CID, SENDER)], "body": "x"}
        )
