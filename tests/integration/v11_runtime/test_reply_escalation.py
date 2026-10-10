"""Seller-reply processing: owner decisions vs routine replies (spec 37.7, 37.10 U11; test (i)).

- a reply asking for a payment (a consequential decision only the owner may take) raises ONE
  ``seller_reply.owner_alert`` (``decision_needed``, reason ``payment_request``); nothing is accepted
  or answered and no outgoing message exists;
- the seller repeating the same request is not re-alerted (business dedup key per inquiry and
  reason set), and a routine reply raises no alert at all;
- the dispatcher posts the owner alert only to the Slack route selected for ``owner_alert``, with
  the fixed owner wording, ids and the dashboard link -- never the reply body or an address -- and
  never through native MCP Events.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

import pytest
from tests.integration.pipeline.support import PipelineEnv
from tests.integration.v11_runtime.support import (
    SENDER_ADDRESS,
    SlackApi,
    approve_slack_category,
    attempts_of,
    eligible_live_listing,
    inquiries_of,
    jobs_of,
    link_seller,
    no_approval_state,
    outbox_of,
    pending_intents,
    prepare_sender,
    seller_replies,
    slack_signal_settings,
    with_settings,
    work,
    worker_sends,
)

from suv_deals.domain.enums import InquiryState, JobType, OutboxState
from suv_deals.domain.notifications import SELLER_REPLY_OWNER_ALERT_EVENT_TYPE
from suv_deals.workers.dispatcher import CATEGORY_BY_EVENT, Dispatcher

pytestmark = pytest.mark.db

PAYMENT_BODY = (
    "Guten Tag, das Fahrzeug ist noch verfügbar. Bitte überweisen Sie vorab eine Anzahlung von 500 EUR. "
    "Synthetic fixture reply."
)
ROUTINE_BODY = (
    "Guten Tag, das Fahrzeug ist noch verfügbar. Die Unterlagen liegen vor. Synthetic fixture reply."
)


#: Random ids and times inside an alert or a Slack post: canonical UUIDs (event, inquiry, reply and
#: listing ids, also inside the dashboard link and the dedup key) and RFC 3339 timestamps.
_RANDOM_IDS_AND_TIMES = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
    re.IGNORECASE,
)


def _seller_text_view(value: Any) -> str:
    """``value`` as JSON text with non-ASCII kept literal and the random ids/times blanked.

    A plain substring test against ``json.dumps`` output is wrong both ways: a short token such as
    the deposit amount ``500`` occurs by chance inside a random UUID (``...fa50025ae``) or a
    millisecond timestamp (a flaky failure), and ``ensure_ascii`` turns ``überweisen`` into
    ``\\u00fcberweisen`` (a check that can never fail). The ids are asserted separately; what is
    left must not carry any seller-supplied text.
    """
    return _RANDOM_IDS_AND_TIMES.sub("<id>", json.dumps(value, ensure_ascii=False))


def test_seller_text_view_ignores_random_ids_but_keeps_seller_text() -> None:
    reply_id = "1b0c2d3e-4f50-4a6b-8c7d-9e0fa50025ae"
    payload = {
        "reply_id": reply_id,
        "occurred_at": "2026-10-10T10:00:00.500Z",
        "dashboard_url": f"https://dashboard.example/inquiries/x/replies/{reply_id}",
    }
    # The former plain check `"500" not in json.dumps(payload)` failed on such a clean payload.
    assert "500" in json.dumps(payload)
    assert "500" not in _seller_text_view(payload)
    # ... and could never catch a leaked non-ASCII word.
    leaked = {"status": "Bitte überweisen Sie eine Anzahlung von 500 EUR"}
    assert "überweisen" not in json.dumps(leaked)
    view = _seller_text_view(leaked)
    assert "überweisen" in view and "500" in view and "Anzahlung" in view


def _db_url(env: PipelineEnv) -> str:
    url = env.ctx.settings.database_url
    assert url is not None
    return url.get_secret_value()


async def _process(env: PipelineEnv, reply_id: UUID) -> dict[str, Any]:
    """Run the worker until the reply's processing job finished (a pending recalculation only
    releases it, without consuming an attempt)."""
    for _ in range(4):
        await work(env)
        [job] = [
            j
            for j in jobs_of(env, JobType.SELLER_REPLY_PROCESS)
            if j["payload"].get("reply_id") == str(reply_id)
        ]
        if job["state"] == "succeeded":
            return job
        assert job["state"] == "queued", job
        env.seed.conn.execute("update ops.jobs set available_at = now() where id = %s", (job["id"],))
    raise AssertionError("the reply processing job did not finish")


async def test_payment_request_alerts_once_and_routine_replies_never(env: PipelineEnv) -> None:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    listing = await eligible_live_listing(env)
    seller = await link_seller(env, listing)
    await work(env)
    [intent] = (await pending_intents(env, sender.worker)).intents
    await worker_sends(env, sender.worker, intent)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.ACCEPTED

    # 1. The seller asks for a deposit: ONE owner-decision alert.
    first = await seller_replies(
        env,
        sender.worker,
        inquiry["id"],
        in_reply_to=intent.rfc_message_id,
        from_address=seller.address,
        body=PAYMENT_BODY,
    )
    job = await _process(env, first.reply_id)
    result = job["result_reference"]
    assert result["outcome"] == "processed"
    assert result["decisions"] == ["payment_request"]
    assert len(result["owner_alert_event_ids"]) == 1
    [alert] = outbox_of(env, SELLER_REPLY_OWNER_ALERT_EVENT_TYPE)
    assert str(alert["event_id"]) == result["owner_alert_event_ids"][0]
    assert alert["state"] == OutboxState.PENDING and not alert["is_fixture"]
    payload = alert["payload"]
    assert payload["kind"] == "decision_needed" and payload["reasons"] == ["payment_request"]
    assert payload["inquiry_id"] == str(inquiry["id"]) and payload["reply_id"] == str(first.reply_id)
    rendered = _seller_text_view(payload)
    for private in ("Anzahlung", "500", "überweisen", seller.address, SENDER_ADDRESS):
        assert private not in rendered  # ids, codes and the dashboard link only
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.REPLIED  # nothing accepted, nothing answered

    # 2. The same request again: no second alert (one per inquiry and reason set).
    second = await seller_replies(
        env,
        sender.worker,
        inquiry["id"],
        in_reply_to=intent.rfc_message_id,
        from_address=seller.address,
        body=PAYMENT_BODY.replace("500", "400"),
    )
    job = await _process(env, second.reply_id)
    assert job["result_reference"]["decisions"] == ["payment_request"]
    assert job["result_reference"]["owner_alert_event_ids"] == []
    assert len(outbox_of(env, SELLER_REPLY_OWNER_ALERT_EVENT_TYPE)) == 1

    # 3. A routine reply: no alert at all (receipt and recalculation stay in the dashboard).
    third = await seller_replies(
        env,
        sender.worker,
        inquiry["id"],
        in_reply_to=intent.rfc_message_id,
        from_address=seller.address,
        body=ROUTINE_BODY,
    )
    job = await _process(env, third.reply_id)
    assert job["result_reference"]["decisions"] == []
    assert job["result_reference"]["owner_alert_event_ids"] == []
    assert len(outbox_of(env, SELLER_REPLY_OWNER_ALERT_EVENT_TYPE)) == 1
    # ONE undelivered receipt signal for the inquiry (C1 flood control: the later replies are
    # coalesced into it; dot reads every reply) and never an outgoing reply or follow-up.
    assert len(outbox_of(env, "seller.reply.received")) == 1
    assert len(attempts_of(env, inquiry["id"])) == 1
    assert len(jobs_of(env, JobType.SELLER_INQUIRY_SEND)) == 1

    # 4. Dispatch: the owner alert goes ONLY to the Slack route of the owner_alert category.
    assert CATEGORY_BY_EVENT[SELLER_REPLY_OWNER_ALERT_EVENT_TYPE] == "owner_alert"
    live = with_settings(env, slack_signal_settings(_db_url(env)))
    await approve_slack_category(live, "seller_reply")
    await approve_slack_category(live, "owner_alert")
    api = SlackApi()
    report = await Dispatcher(live.ctx, http=api.client(), dispatcher_id="dispatcher-b2a").run_workspace(
        env.workspace_id
    )
    # (review.pending candidate events have no candidate route here: blocked, never posted)
    delivered = {(e.event_type, e.state) for e in report.events if e.event_type != "review.pending"}
    assert delivered == {
        ("seller.reply.received", OutboxState.DELIVERED),
        (SELLER_REPLY_OWNER_ALERT_EVENT_TYPE, OutboxState.DELIVERED),
    }
    assert len(api.posts) == 2 and api.other == []  # one coalesced receipt signal + the alert
    owner_posts = [p for p in api.posts if p["metadata"]["event_type"] == "suv_deals.owner_alert"]
    [post] = owner_posts
    assert post["text"].startswith("Seller reply needs your decision: payment request.")
    assert "Nothing was accepted or answered; no reply is sent automatically." in post["text"]
    assert f"Inquiry {inquiry['id']}; reply {first.reply_id}" in post["text"]
    posted = _seller_text_view(api.posts)
    for private in ("Anzahlung", "überweisen", "Guten Tag", seller.address, SENDER_ADDRESS, "xoxb-"):
        assert private not in posted
    assert (
        env.scalar("select count(*) from ops.event_deliveries where workspace_id = %s", env.workspace_id) == 0
    )
    [alert] = outbox_of(env, SELLER_REPLY_OWNER_ALERT_EVENT_TYPE)
    assert alert["state"] == OutboxState.DELIVERED
    no_approval_state(env)


async def test_owner_alert_route_on_mcp_events_is_refused_visibly(env: PipelineEnv) -> None:
    """MCP Events stays for candidate discovery: a seller-reply owner alert is never delivered
    there; without an approved Slack route for its category it is blocked visibly."""
    sender = await prepare_sender(env)
    assert sender.worker is not None
    listing = await eligible_live_listing(env)
    seller = await link_seller(env, listing)
    await work(env)
    [intent] = (await pending_intents(env, sender.worker)).intents
    await worker_sends(env, sender.worker, intent)
    [inquiry] = inquiries_of(env)
    reply = await seller_replies(
        env,
        sender.worker,
        inquiry["id"],
        in_reply_to=intent.rfc_message_id,
        from_address=seller.address,
        body=PAYMENT_BODY,
    )
    await _process(env, reply.reply_id)
    live = with_settings(env, slack_signal_settings(_db_url(env)))
    await approve_slack_category(live, "seller_reply")  # but nothing for owner_alert
    api = SlackApi()
    await Dispatcher(live.ctx, http=api.client(), dispatcher_id="dispatcher-b2a").run_workspace(
        env.workspace_id
    )
    [alert] = outbox_of(env, SELLER_REPLY_OWNER_ALERT_EVENT_TYPE)
    assert alert["state"] == OutboxState.BLOCKED and alert["blocker_code"] == "NO_ACTIVE_ROUTE"
    assert [p["metadata"]["event_type"] for p in api.posts] == ["suv_deals.seller_reply_received"]
    assert (
        env.scalar("select count(*) from ops.event_deliveries where workspace_id = %s", env.workspace_id) == 0
    )
