"""Wire evolution v1.1 (B2b): ``not_now`` claim answers, server-flagged expired intents and the
returned-original Message-IDs of delivery reports.

- ``not_now`` (rolling caps, seller cooldown, source pause) keeps the intent waiting: nothing is
  sent and nothing is reported; it is claimed again after ``NOT_NOW_RETRY`` and reported
  ``intent_expired`` once its validity ends.
- An intent the server lists with ``expired: true`` is refused before any claim, whatever the
  local clock says.
- A bounce's returned original is read from the RAW body (the sanitiser removes it) and uploaded
  as ``returned_message_ids``; any other message type never carries them.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from bridge_support import MAILBOX_ID, OWNER, START, Harness
from outlook_bridge import matching
from outlook_bridge.sending import NOT_NOW_RETRY
from outlook_bridge.testing import inquiry_message_id, make_intent
from outlook_bridge.wire import ReplyUpload, WorkerSendIntent

from suv_deals.domain.enums import ReplyMessageType


def _intent(h: Harness, **overrides: Any) -> WorkerSendIntent:
    kwargs: dict[str, Any] = {
        "inquiry_id": uuid4(),
        "mailbox_binding_id": MAILBOX_ID,
        "from_address": OWNER,
        "created_at": h.clock.now() - timedelta(minutes=1),
    }
    kwargs.update(overrides)
    return make_intent(**kwargs)


def _reports(h: Harness, intent_id: UUID) -> list[dict[str, Any]]:
    return [r for r in h.backend.reports if r["intent_id"] == str(intent_id)]


def test_not_now_keeps_the_intent_waiting_without_a_report(harness: Harness) -> None:
    intent = _intent(harness, ttl=timedelta(hours=4))
    harness.backend.add_intent(intent)
    harness.backend.claim_refusal = "not_now"
    worker = harness.worker()
    assert worker.start().sends["deferred_not_now"] == 1
    assert _reports(harness, intent.intent_id) == [] and harness.outlook.send_calls == []
    claims = len(harness.backend.claims)
    harness.advance(timedelta(minutes=2))
    assert worker.tick().sends["deferred_not_now"] == 1
    assert len(harness.backend.claims) == claims  # not claimed again before the retry time
    harness.backend.claim_refusal = None
    harness.advance(NOT_NOW_RETRY)
    worker.tick()
    assert harness.outlook.send_calls == [intent.subject]  # sent once the wait lifted
    assert _reports(harness, intent.intent_id)[0]["state"] == "submitted_to_outbox"


def test_not_now_that_outlasts_the_intent_is_reported_expired_never_sent(harness: Harness) -> None:
    intent = _intent(harness, ttl=timedelta(minutes=30))
    harness.backend.add_intent(intent)
    harness.backend.claim_refusal = "not_now"
    worker = harness.worker()
    worker.start()
    for _ in range(4):
        harness.advance(NOT_NOW_RETRY + timedelta(seconds=1))
        worker.tick()
    reports = _reports(harness, intent.intent_id)
    assert [r["refusal_reason"] for r in reports] == ["intent_expired"]
    assert reports[0]["state"] == "refused_before_send"
    assert harness.outlook.send_calls == []


def test_server_flagged_expired_intent_is_refused_before_any_claim(harness: Harness) -> None:
    intent = _intent(harness, ttl=timedelta(hours=4))  # still valid by the local clock
    harness.backend.add_intent(intent)
    harness.backend.expire_intent(intent.intent_id)
    harness.worker().start()
    report = _reports(harness, intent.intent_id)[0]
    assert report["state"] == "refused_before_send" and report["refusal_reason"] == "intent_expired"
    assert intent.intent_id not in harness.backend.claims
    assert harness.outlook.send_calls == []


def test_expired_flag_does_not_change_the_intent_identity() -> None:
    intent = make_intent(
        inquiry_id=uuid4(), mailbox_binding_id=MAILBOX_ID, from_address=OWNER, created_at=START
    )
    flagged = WorkerSendIntent.model_validate({**intent.model_dump(), "expired": True})
    assert flagged.same_intent(intent) and intent.same_intent(flagged)
    assert flagged.is_expired(intent.created_at) and not intent.is_expired(intent.created_at)


def _bounce_body(*message_ids: str) -> str:
    originals = "".join(f"Original-Message-ID: {m}\n" for m in message_ids)
    return (
        "This is the mail system at host mx.dealer.example.invalid.\n\n"
        "Your message could not be delivered to one or more recipients.\n\n"
        "Reporting-MTA: dns; mx.dealer.example.invalid\n"
        "Final-Recipient: rfc822; gone@dealer.example.invalid\n"
        "Action: failed\nStatus: 5.1.1\n\n"
        "--- returned message ---\n"
        f"{originals}"
        "Subject: Anfrage zu Synthetic SUV\n\nGuten Tag, ist das Fahrzeug noch verfuegbar?\n"
    )


def test_bounce_upload_carries_the_returned_original_from_the_raw_body(harness: Harness) -> None:
    inquiry = uuid4()
    outbound = inquiry_message_id(inquiry)
    harness.bind(inquiry)
    harness.deliver_reply(
        inquiry,
        sender="mailer-daemon@mx.dealer.example.invalid",
        subject="Undelivered Mail Returned to Sender",
        body=_bounce_body(outbound),
        references=(),
    )
    harness.worker().start()
    posts = [p for p in harness.reply_posts() if p["inquiry_id"] == str(inquiry)]
    assert posts, "the correlated bounce is uploaded"
    assert posts[0]["message_type"] in ("bounce", "delivery_notice")
    assert posts[0]["returned_message_ids"] == [outbound]


def test_returned_ids_only_for_delivery_reports(harness: Harness) -> None:
    inquiry = uuid4()
    harness.bind(inquiry)
    harness.deliver_reply(inquiry)
    harness.worker().start()
    post = next(p for p in harness.reply_posts() if p["inquiry_id"] == str(inquiry))
    # A matched seller reply goes out in exactly the spec v1.0 shape: no extension keys.
    assert "message_type" not in post and "returned_message_ids" not in post
    with pytest.raises(ValueError, match="bounce or delivery notice"):
        ReplyUpload.model_validate({**post, "returned_message_ids": ["<a@example.invalid>"]})
    upload = ReplyUpload.model_validate(
        {
            **post,
            "message_type": ReplyMessageType.BOUNCE,
            "returned_message_ids": ["<A@Example.invalid>", "<A@example.invalid>"],
        }
    )
    assert upload.returned_message_ids == ("<A@example.invalid>",)
    assert matching.REQUEST_EXTENSION_DEFAULTS["returned_message_ids"] == []
