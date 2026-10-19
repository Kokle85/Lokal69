"""End to end: pipeline -> plan -> reservation -> outlook_local intent -> reply -> Slack signal.

Spec 37.1-37.7, 37.10 U3/U6/U9 (work package B2a, test (a) and (j)):

- the valuation pipeline plans ONE inquiry per listing revision for a REAL-lineage eligible
  candidate (inquiry readiness is separate from profit readiness: the valuation is incomplete);
- automatic mode + verified ``outlook_local`` sender + the standing authorization reserve, queue
  and publish ONE send intent; the desktop worker (simulated through ``send_intents_repo``)
  claims it and reports Sent Items -> ``accepted``;
- a correlated reply makes the valuation stale and queues its recomputation, writes ONE
  ``seller.reply.received`` outbox row and is processed by its job (no outgoing reply);
- the dispatcher posts exactly one minimal Slack message to the route selected for the
  ``seller_reply`` category and never uses native MCP Events for it; a second pass sends nothing;
- no approval state or approval wait exists anywhere.
"""

from __future__ import annotations

import json

import pytest
from tests.integration.pipeline.support import PipelineEnv
from tests.integration.v11_runtime.support import (
    DASHBOARD,
    SENDER_ADDRESS,
    SlackApi,
    approve_slack_category,
    attempts_of,
    debits_of,
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

from suv_deals.domain.enums import InquiryState, JobState, JobType, OutboxState
from suv_deals.workers.dispatcher import CATEGORY_BY_EVENT, Dispatcher

pytestmark = pytest.mark.db

REPLY_BODY = (
    "Guten Tag, das Fahrzeug ist noch verfügbar. Der Preis bleibt bei 2.750 EUR. "
    "Die Unterlagen liegen vor. Synthetic fixture reply."
)


def _db_url(env: PipelineEnv) -> str:
    url = env.ctx.settings.database_url
    assert url is not None
    return url.get_secret_value()


async def test_automatic_inquiry_end_to_end(env: PipelineEnv) -> None:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    listing = await eligible_live_listing(env)
    seller = await link_seller(env, listing)

    # 1. Valuation -> ONE plan job for the revision -> reservation + queue -> ONE send job.
    reports = await work(env)
    by_type: dict[JobType, list[object]] = {}
    for report in reports:
        by_type.setdefault(report.job_type, []).append(report)
    assert len(by_type[JobType.SELLER_INQUIRY_PLAN]) == 1
    assert len(by_type[JobType.SELLER_INQUIRY_SEND]) == 1
    assert all(r.state == JobState.SUCCEEDED for r in reports), [(r.job_type, r.code) for r in reports]
    [plan] = jobs_of(env, JobType.SELLER_INQUIRY_PLAN)
    assert plan["dedup_key"] == f"seller_inquiry.plan:{listing['id']}:{listing['current_revision_id']}"
    assert plan["result_reference"]["outcome"] == "reserved"
    [inquiry] = inquiries_of(env)
    assert inquiry["readiness"] == "inquiry_ready" and inquiry["qualification_listing_id"] == listing["id"]
    # Profit readiness is NOT required: the valuation stays incomplete (no approved tax rule).
    assert env.scalar(
        "select state from app.valuations where workspace_id = %s and listing_id = %s"
        " order by created_at desc limit 1",
        env.workspace_id,
        listing["id"],
    ) in ("incomplete", "estimated")
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["result_reference"]["outcome"] == "intent_published"
    assert inquiry["state"] == InquiryState.SENDING
    assert debits_of(env) == 1

    # 2. The desktop worker pulls exactly that intent, claims it (fresh revalidation) and reports.
    batch = await pending_intents(env, sender.worker)
    assert not batch.kill_switch_active
    [intent] = batch.intents
    assert intent.inquiry_id == inquiry["id"] and intent.from_address == SENDER_ADDRESS
    assert intent.to_address == seller.address
    await worker_sends(env, sender.worker, intent)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.ACCEPTED
    [attempt] = attempts_of(env, inquiry["id"])
    assert attempt["outcome"] == "accepted" and attempt["provider"] == "outlook_local"

    # A second worker pass never creates a second intent (the send job is done).
    assert (await work(env)) == []
    assert len(attempts_of(env, inquiry["id"])) == 1

    # 3. The seller replies (correlated by In-Reply-To).
    ingested = await seller_replies(
        env,
        sender.worker,
        inquiry["id"],
        in_reply_to=intent.rfc_message_id,
        from_address=seller.address,
        body=REPLY_BODY,
    )
    assert ingested.stale_valuation_ids and ingested.recompute_job_ids
    assert ingested.outbox_event_id is not None and ingested.processing_job_id is not None
    reports = await work(env)
    # The reply job may wait (released, no attempt burned) until the recomputation finished.
    process = jobs_of(env, JobType.SELLER_REPLY_PROCESS)
    if process[0]["state"] == "queued":
        env.seed.conn.execute("update ops.jobs set available_at = now() where id = %s", (process[0]["id"],))
        reports += await work(env)
    [process_job] = jobs_of(env, JobType.SELLER_REPLY_PROCESS)
    assert process_job["state"] == "succeeded" and process_job["attempts"] >= 1
    assert process_job["result_reference"]["outcome"] == "processed"
    assert process_job["result_reference"]["owner_alert_event_ids"] == []  # routine reply
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.REPLIED
    # Recomputed: the newest valuation of the listing is current again (not stale).
    assert (
        env.scalar(
            "select state from app.valuations where workspace_id = %s and listing_id = %s"
            " order by created_at desc limit 1",
            env.workspace_id,
            listing["id"],
        )
        != "stale"
    )
    # Never an outgoing reply or follow-up: still exactly one attempt and one plan job.
    assert len(attempts_of(env, inquiry["id"])) == 1
    assert len(jobs_of(env, JobType.SELLER_INQUIRY_PLAN)) == 1
    assert len(jobs_of(env, JobType.SELLER_INQUIRY_SEND)) == 1

    [signal] = outbox_of(env, "seller.reply.received")
    assert signal["state"] == OutboxState.PENDING and not signal["is_fixture"]
    assert CATEGORY_BY_EVENT["seller.reply.received"] == "seller_reply"

    # 4. Dispatcher: external notifications allowed, a verified Slack route for seller replies.
    live = with_settings(env, slack_signal_settings(_db_url(env)))
    await approve_slack_category(live, "seller_reply")
    api = SlackApi()
    report = await Dispatcher(live.ctx, http=api.client(), dispatcher_id="dispatcher-b2a").run_workspace(
        env.workspace_id
    )
    delivered = [e for e in report.events if e.event_type == "seller.reply.received"]
    assert [(e.event_id, e.state, e.provider) for e in delivered] == [
        (signal["event_id"], OutboxState.DELIVERED, "slack")
    ]
    assert len(api.posts) == 1 and api.other == []
    post = api.posts[0]
    text = post["text"]
    assert f"Event {signal['event_id']}" in text and str(inquiry["id"]) in text
    assert str(ingested.reply_id) in text and "seller reply received" in text
    assert f"{DASHBOARD}/inquiries/{inquiry['id']}/replies/{ingested.reply_id}" in text
    rendered = json.dumps(post)
    for secret in (seller.address, SENDER_ADDRESS, "Guten Tag", "2.750", "xoxb-", intent.rfc_message_id):
        assert secret not in rendered  # no body, address, Message-ID or credential
    assert post["metadata"]["event_type"] == "suv_deals.seller_reply_received"
    # Never native MCP Events for seller replies (no deliveries exist at all).
    assert (
        env.scalar("select count(*) from ops.event_deliveries where workspace_id = %s", env.workspace_id) == 0
    )
    # A 2xx is a receipt only: nothing marks the signal as seen/processed by dot.
    [row] = outbox_of(env, "seller.reply.received")
    assert row["state"] == OutboxState.DELIVERED
    assert (
        env.scalar(
            "select owner_seen_at from ops.outbox where workspace_id = %s and event_id = %s",
            env.workspace_id,
            signal["event_id"],
        )
        is None
    )

    # The same reply never activates dot twice: another pass posts nothing.
    again = await Dispatcher(live.ctx, http=api.client(), dispatcher_id="dispatcher-b2a").run_workspace(
        env.workspace_id
    )
    assert [e for e in again.events if e.event_type == "seller.reply.received"] == []
    assert len(api.posts) == 1

    # (j) No approval state or approval wait anywhere.
    no_approval_state(env)


async def test_signal_without_a_seller_reply_route_is_blocked_visibly(env: PipelineEnv) -> None:
    """External notifications allowed but no approved route for the category: visible blocker,
    no post, and the candidate route (MCP Events) is never used as a fallback."""
    sender = await prepare_sender(env)
    assert sender.worker is not None
    listing = await eligible_live_listing(env)
    seller = await link_seller(env, listing)
    await work(env)
    [intent] = (await pending_intents(env, sender.worker)).intents
    await worker_sends(env, sender.worker, intent)
    [inquiry] = inquiries_of(env)
    await seller_replies(
        env,
        sender.worker,
        inquiry["id"],
        in_reply_to=intent.rfc_message_id,
        from_address=seller.address,
        body=REPLY_BODY,
    )
    live = with_settings(env, slack_signal_settings(_db_url(env)))
    await approve_slack_category(live, "candidate_discovery")  # a route, but not for seller replies
    api = SlackApi()
    await Dispatcher(live.ctx, http=api.client(), dispatcher_id="dispatcher-b2a").run_workspace(
        env.workspace_id
    )
    [signal] = outbox_of(env, "seller.reply.received")
    assert signal["state"] == OutboxState.BLOCKED and signal["blocker_code"] == "NO_ACTIVE_ROUTE"
    assert api.posts == []

    # External notifications OFF: blocked before anything else, nothing posted.
    other = await approve_slack_category(live, "seller_reply")
    assert other is not None
    # TEST ARRANGEMENT: an operator re-queued the blocked signal after approving the route.
    env.seed.conn.execute(
        "update ops.outbox set state = 'pending', blocker_code = null"
        " where workspace_id = %s and event_id = %s",
        (env.workspace_id, signal["event_id"]),
    )
    off = with_settings(env, slack_signal_settings(_db_url(env), allow_external_notifications=False))
    await Dispatcher(off.ctx, http=api.client(), dispatcher_id="dispatcher-b2a").run_workspace(
        env.workspace_id
    )
    [signal] = outbox_of(env, "seller.reply.received")
    assert (
        signal["state"] == OutboxState.BLOCKED and signal["blocker_code"] == "EXTERNAL_NOTIFICATIONS_DISABLED"
    )
    assert api.posts == []
