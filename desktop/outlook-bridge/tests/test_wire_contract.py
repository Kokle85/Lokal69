"""Contract: the worker's wire models mirror the backend ``outlook_local`` models field for field.

The desktop package duplicates these models (it must not load the backend settings/provider stack),
so this test round-trips worker objects through the backend models and back, and checks the shared
hashing/Message-ID helpers against the backend implementations.
"""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from bridge_support import MAILBOX_ID, OWNER, START
from outlook_bridge import wire
from outlook_bridge.testing import inquiry_message_id, make_intent

from suv_deals.domain import seller_templates
from suv_deals.integrations import mime_builder
from suv_deals.integrations.email_providers import outlook_local

PAIRS = [
    (wire.WorkerSendIntent, outlook_local.OutlookSendIntent),
    (wire.WorkerSendReport, outlook_local.OutlookSendReport),
    (wire.WorkerAccountReport, outlook_local.OutlookAccountReport),
    (wire.WorkerHeartbeat, outlook_local.OutlookHeartbeat),
]


#: Worker fields that are listing/transport state on the wire (``api.schemas`` models), not part of
#: the backend provider model: ``expired`` is set by ``GET /send-intents`` for reaped intents.
WIRE_ONLY_FIELDS = {wire.WorkerSendIntent: {"expired"}}


@pytest.mark.parametrize(("worker_model", "backend_model"), PAIRS)
def test_models_have_identical_fields(worker_model: type, backend_model: type) -> None:
    extra = WIRE_ONLY_FIELDS.get(worker_model, set())
    assert set(worker_model.model_fields) - extra == set(backend_model.model_fields)  # type: ignore[attr-defined]


def test_enumerations_match() -> None:
    assert {s.value for s in wire.SubmissionState} == {s.value for s in outlook_local.OutlookSubmissionState}
    assert {r.value for r in wire.RefusalReason} == {r.value for r in outlook_local.OutlookRefusalReason}


def test_intent_round_trips_through_the_backend_model() -> None:
    intent = make_intent(
        inquiry_id=uuid4(), mailbox_binding_id=MAILBOX_ID, from_address=OWNER, created_at=START
    )
    backend = outlook_local.OutlookSendIntent.model_validate_json(intent.model_dump_json(exclude={"expired"}))
    again = wire.WorkerSendIntent.model_validate_json(backend.model_dump_json())
    assert again == intent
    assert wire.intent_integrity_problems(again) == ()
    flagged = wire.WorkerSendIntent.model_validate({**intent.model_dump(), "expired": True})
    assert flagged.same_intent(intent) and flagged.is_expired(START) and not intent.is_expired(START)


def test_backend_intent_enforces_the_wire_limits() -> None:
    intent = make_intent(
        inquiry_id=uuid4(), mailbox_binding_id=MAILBOX_ID, from_address=OWNER, created_at=START
    )
    data = intent.model_dump(exclude={"expired"})
    with pytest.raises(ValueError, match="inquiry reference"):
        outlook_local.OutlookSendIntent.model_validate({**data, "inquiry_ref": f"inquiry-{uuid4()}"})
    for field, value in (("inquiry_ref", "x" * 65), ("rfc_message_id", "<" + "a" * 998 + ">")):
        with pytest.raises(ValueError, match=field):
            outlook_local.OutlookSendIntent.model_validate({**data, field: value})


def test_account_report_cannot_claim_weakened_security() -> None:
    values = {
        "mailbox_binding_id": str(MAILBOX_ID),
        "worker_id": "desktop-1",
        "reported_at": START.isoformat(),
        "outlook_flavour": "classic",
        "stable_account_key": "acct:9f2c1e7a",
        "account_smtp_address": OWNER,
    }
    for model in (wire.WorkerAccountReport, outlook_local.OutlookAccountReport):
        assert model.model_validate(values).security_settings_unchanged is True  # type: ignore[attr-defined]
        with pytest.raises(ValueError, match="security_settings_unchanged"):
            model.model_validate({**values, "security_settings_unchanged": False})  # type: ignore[attr-defined]


def test_gap_reports_never_end_before_they_start() -> None:
    with pytest.raises(ValueError, match="before it starts"):
        wire.GapReport(kind="worker_offline", started_at=START, ended_at=START - timedelta(seconds=1))
    clamped = wire.GapReport.of("worker_offline", START, START - timedelta(minutes=5))
    assert clamped.ended_at == START
    assert wire.GapReport.of("worker_offline", START, None).ended_at is None


def test_not_now_refusal_is_a_retryable_pre_submission_failure() -> None:
    intent = make_intent(
        inquiry_id=uuid4(), mailbox_binding_id=MAILBOX_ID, from_address=OWNER, created_at=START
    )
    report = wire.WorkerSendReport(
        intent_id=intent.intent_id,
        inquiry_id=intent.inquiry_id,
        mailbox_binding_id=MAILBOX_ID,
        worker_id="desktop-1",
        state=wire.SubmissionState.REFUSED_BEFORE_SEND,
        refusal_reason=wire.RefusalReason.NOT_NOW,
        reported_at=START,
    )
    outcome = outlook_local.map_outlook_report(
        outlook_local.OutlookSendIntent.model_validate_json(intent.model_dump_json(exclude={"expired"})),
        outlook_local.OutlookSendReport.model_validate_json(report.model_dump_json()),
        observed_at=START,
    )
    assert type(outcome).__name__ == "SendDefiniteFailure"
    assert outcome.pre_submission and outcome.retryable  # type: ignore[union-attr]


def test_report_and_heartbeat_round_trip() -> None:
    report = wire.WorkerSendReport(
        intent_id=uuid4(),
        inquiry_id=uuid4(),
        mailbox_binding_id=MAILBOX_ID,
        worker_id="desktop-1",
        state=wire.SubmissionState.SENT_ITEMS_CONFIRMED,
        account_smtp_address_used=OWNER,
        observed_internet_message_id=inquiry_message_id(uuid4()),
        outbox_pending=wire.Tristate.NO,
        sent_items_present=True,
        reported_at=START,
        sent_at=START,
    )
    backend = outlook_local.OutlookSendReport.model_validate(json.loads(report.model_dump_json()))
    assert json.loads(backend.model_dump_json()) == json.loads(report.model_dump_json())
    beat = wire.WorkerHeartbeat(
        mailbox_binding_id=MAILBOX_ID,
        worker_id="desktop-1",
        at=START,
        outlook_running=True,
        mailbox_connected=True,
    )
    assert outlook_local.OutlookHeartbeat.model_validate_json(beat.model_dump_json()).at == START


def test_refused_report_maps_to_a_pre_submission_failure_in_the_backend() -> None:
    intent = make_intent(
        inquiry_id=uuid4(), mailbox_binding_id=MAILBOX_ID, from_address=OWNER, created_at=START
    )
    report = wire.WorkerSendReport(
        intent_id=intent.intent_id,
        inquiry_id=intent.inquiry_id,
        mailbox_binding_id=MAILBOX_ID,
        worker_id="desktop-1",
        state=wire.SubmissionState.SEND_CALL_FAILED,
        error_code="WORKER_INTERRUPTED",
        reported_at=START,
    )
    outcome = outlook_local.map_outlook_report(
        outlook_local.OutlookSendIntent.model_validate_json(intent.model_dump_json(exclude={"expired"})),
        outlook_local.OutlookSendReport.model_validate_json(report.model_dump_json()),
        observed_at=START,
    )
    assert type(outcome).__name__ == "SendUncertain"  # an interrupted .Send is never a definite failure


def test_hash_and_message_id_helpers_match_the_backend() -> None:
    subject, body = "Anfrage zu SUV \u2013 REF-1", "Guten Tag,\r\nist das Fahrzeug verfügbar?\r\n"
    assert wire.message_body_hash(subject, body) == seller_templates.message_body_hash(subject, body)
    assert wire.canonical_message(subject, body) == seller_templates.canonical_message(subject, body)
    inquiry = uuid4()
    message_id = inquiry_message_id(inquiry, 3)
    parsed = mime_builder.parse_inquiry_message_id(message_id)
    assert parsed is not None and wire.parse_inquiry_message_id(message_id) == (parsed.inquiry_id, 3)
    assert wire.parse_inquiry_message_id("<other@example.invalid>") is None
    assert wire.is_own_message_id_format(message_id)
