"""Pure checks of the seller-reply signal and owner-alert builders (spec 37.6/37.7; no database).

The Slack post carries the event id, inquiry id, reply id, listing/vehicle reference, a fixed
status and the authenticated https dashboard link -- never a body, address, attachment, amount or
credential. The owner alert names typed reasons in fixed owner wording.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from suv_deals.domain.notifications import (
    build_seller_reply_owner_alert,
    render_seller_reply_alert_text,
    seller_reply_alert_dedup_key,
)
from suv_deals.domain.replies import ReplySignalStatus, build_seller_reply_signal
from suv_deals.errors import ValidationFailed
from suv_deals.integrations import slack
from suv_deals.settings import Settings

DASHBOARD = "https://dashboard.synthetic.example"
CHANNEL = "C0SYNTHETIC1"


def _config() -> slack.SlackConfig:
    return slack.SlackConfig.model_validate(
        {
            "bot_token": "xoxb-SYNTHETIC-test-token-not-real",
            "signing_secret": "synthetic-signing-secret",
            "channel_id": CHANNEL,
            "destination_approval_ref": "synthetic-approval",
        }
    )


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "allow_external_notifications": True,
        "seller_reply_signal_provider": "slack",
        "slack_channel_id": CHANNEL,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def test_seller_reply_post_is_minimal() -> None:
    inquiry, reply, listing = uuid4(), uuid4(), uuid4()
    draft = build_seller_reply_signal(
        event_id=uuid4(),
        inquiry_id=inquiry,
        reply_id=reply,
        listing_id=listing,
        dashboard_base_url=DASHBOARD,
        occurred_at=datetime.now(UTC),
        status=ReplySignalStatus.DECISION_NEEDED,
    )
    notice = slack.SlackSellerReplyNotice.model_validate(draft.payload)
    body = slack.build_seller_reply_post_body(_config(), notice)
    text = body["text"]
    assert text.startswith("Seller inquiry update: seller reply received; owner decision needed.")
    assert f"Event {draft.event_id}; inquiry {inquiry}; reply {reply}; listing {listing}." in text
    assert f"{DASHBOARD}/inquiries/{inquiry}/replies/{reply}" in text
    assert body["channel"] == CHANNEL and body["unfurl_links"] is False and body["unfurl_media"] is False
    assert set(body["metadata"]["event_payload"]) == {
        "event_ref",
        "dedup_key",
        "event_id",
        "inquiry_id",
        "reply_id",
        "listing_id",
    }
    assert "xoxb" not in json.dumps(body)


def test_seller_reply_notice_refuses_free_text_status_and_plain_http_links() -> None:
    draft = build_seller_reply_signal(
        event_id=uuid4(),
        inquiry_id=uuid4(),
        reply_id=uuid4(),
        listing_id=None,
        dashboard_base_url=DASHBOARD,
        occurred_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="status"):
        slack.SlackSellerReplyNotice.model_validate({**draft.payload, "status": "Seller says: call me"})
    with pytest.raises(ValueError, match="dashboard"):
        slack.SlackSellerReplyNotice.model_validate(
            {**draft.payload, "dashboard_url": "http://dashboard.synthetic.example/inquiries/x"}
        )


def test_signal_blockers_do_not_depend_on_the_candidate_route() -> None:
    config = _config()
    assert slack.signal_send_blockers(_settings(), config, category="seller_reply") == []
    assert slack.signal_send_blockers(
        _settings(allow_external_notifications=False), config, category="seller_reply"
    ) == ["allow_external_notifications is false"]
    assert slack.signal_send_blockers(
        _settings(seller_reply_signal_provider="disabled"), config, category="seller_reply"
    ) == ["seller_reply_signal_provider is not slack"]
    assert slack.signal_send_blockers(_settings(), config, category="candidate_discovery") == [
        "category is not routed through a Slack signal"
    ]


def test_owner_alert_is_typed_deduplicated_and_free_of_seller_text() -> None:
    inquiry, reply, listing = uuid4(), uuid4(), uuid4()
    draft = build_seller_reply_owner_alert(
        event_id=uuid4(),
        kind="decision_needed",
        inquiry_id=inquiry,
        reply_id=reply,
        listing_id=listing,
        dashboard_url=f"{DASHBOARD}/inquiries/{inquiry}/replies/{reply}",
        occurred_at=datetime.now(UTC),
        reasons=["payment_request", "payment_request", "appointment_request"],
    )
    assert draft.payload["reasons"] == ["payment_request", "appointment_request"]
    assert draft.priority == "high" and draft.aggregate_id == inquiry
    # The same reason set (any order) is the same business key: no re-alert per repeated request.
    assert draft.dedup_key == seller_reply_alert_dedup_key(
        "decision_needed", inquiry_id=inquiry, reasons=["appointment_request", "payment_request"]
    )
    text = render_seller_reply_alert_text("decision_needed", draft.payload["reasons"])
    assert text == (
        "Seller reply needs your decision: payment request, appointment request. Nothing was accepted"
        " or answered; no reply is sent automatically."
    )
    notice = slack.SlackOwnerAlertNotice.model_validate(draft.payload)
    body = slack.build_owner_alert_post_body(_config(), notice)
    assert body["text"].startswith(text)
    with pytest.raises(ValidationFailed):
        build_seller_reply_owner_alert(
            event_id=uuid4(),
            kind="decision_needed",
            inquiry_id=inquiry,
            reply_id=reply,
            listing_id=listing,
            dashboard_url=f"{DASHBOARD}/inquiries/{inquiry}/replies/{reply}",
            occurred_at=datetime.now(UTC),
            reasons=["Bitte überweisen"],  # free text is never a reason code
        )
    with pytest.raises(ValidationFailed):
        build_seller_reply_owner_alert(
            event_id=uuid4(),
            kind="opportunity_supported",
            inquiry_id=inquiry,
            reply_id=reply,
            listing_id=listing,
            dashboard_url=f"{DASHBOARD}/inquiries/{inquiry}/replies/{reply}",
            occurred_at=datetime.now(UTC),
            reasons=["first_alert"],  # an opportunity alert must cite its valuation
        )
