"""Delivery semantics of the seller-reply Slack signal (spec 37.6/37.7, 37.10 U10).

- an uncertain post (Slack 5xx) is never blindly resent: the channel history is searched for
  the event reference first; a hit marks it delivered without a second post (one reply never
  activates dot twice);
- only when the bounded lookup finds nothing is the SAME event (same id and ``SDR-`` reference)
  posted once more;
- a Slack 2xx/``ok`` is recorded as a provider receipt only (nothing claims dot processed it);
- fixture lineage is never delivered.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import psycopg
import pytest
from tests.integration.pipeline.support import SLACK_CHANNEL, PipelineEnv
from tests.integration.v11_runtime.support import (
    approve_slack_category,
    eligible_live_listing,
    inquiries_of,
    link_seller,
    outbox_of,
    pending_intents,
    prepare_sender,
    public_resolver,
    seller_replies,
    slack_signal_settings,
    with_settings,
    work,
    worker_sends,
)

from suv_deals.domain.enums import OutboxState
from suv_deals.integrations import event_bridge as eb
from suv_deals.integrations.safe_http import SafeHttpClient
from suv_deals.workers.dispatcher import Dispatcher, claim_signal_events

pytestmark = pytest.mark.db


def _db_url(env: PipelineEnv) -> str:
    url = env.ctx.settings.database_url
    assert url is not None
    return url.get_secret_value()


class FlakySlack:
    """SYNTHETIC Slack: the first ``fail_posts`` posts answer 500 (possibly delivered); the
    channel history contains the earlier post only when ``history_has_post``."""

    def __init__(self, *, fail_posts: int, history_has_post: bool) -> None:
        self.fail_posts = fail_posts
        self.history_has_post = history_has_post
        self.posts: list[dict[str, Any]] = []
        self.history_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat.postMessage"):
            self.posts.append(json.loads(request.content))
            if self.fail_posts > 0:
                self.fail_posts -= 1
                return httpx.Response(500, text="synthetic upstream failure")
            return httpx.Response(200, json={"ok": True, "ts": "1700000000.000200", "channel": SLACK_CHANNEL})
        if request.url.path.endswith("/conversations.history"):
            self.history_calls += 1
            messages: list[dict[str, Any]] = []
            if self.history_has_post and self.posts:
                first = self.posts[0]
                messages.append(
                    {
                        "type": "message",
                        "bot_id": "B0SYNTHETIC1",
                        "ts": "1700000000.000100",
                        "text": first["text"],
                        "metadata": first["metadata"],
                    }
                )
            return httpx.Response(200, json={"ok": True, "messages": messages, "has_more": False})
        return httpx.Response(404, json={"ok": False, "error": "unexpected_synthetic_route"})

    def client(self) -> SafeHttpClient:
        return SafeHttpClient(resolver=public_resolver, transport=httpx.MockTransport(self))


async def _signal(env: PipelineEnv) -> dict[str, Any]:
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
        body="Guten Tag, das Fahrzeug ist noch verfügbar. Synthetic fixture reply.",
    )
    [signal] = outbox_of(env, "seller.reply.received")
    return signal


def _dispatcher(env: PipelineEnv, api: FlakySlack) -> Dispatcher:
    return Dispatcher(
        env.ctx,
        http=api.client(),
        dispatcher_id="dispatcher-b2a",
        uncertain_policy=eb.UncertainPolicy(hold_for=timedelta(0)),
    )


async def test_uncertain_post_found_in_history_is_never_posted_again(env: PipelineEnv) -> None:
    signal = await _signal(env)
    live = with_settings(env, slack_signal_settings(_db_url(env)))
    await approve_slack_category(live, "seller_reply")
    api = FlakySlack(fail_posts=1, history_has_post=True)
    await _dispatcher(live, api).run_workspace(env.workspace_id)
    assert len(api.posts) == 1 and api.history_calls == 1
    [row] = outbox_of(env, "seller.reply.received")
    assert row["state"] == OutboxState.DELIVERED and row["event_id"] == signal["event_id"]
    # Later passes never post it again.
    await _dispatcher(live, api).run_workspace(env.workspace_id)
    assert len(api.posts) == 1


async def test_uncertain_post_not_found_is_resent_once_with_the_same_reference(env: PipelineEnv) -> None:
    signal = await _signal(env)
    live = with_settings(env, slack_signal_settings(_db_url(env)))
    await approve_slack_category(live, "seller_reply")
    api = FlakySlack(fail_posts=1, history_has_post=False)
    await _dispatcher(live, api).run_workspace(env.workspace_id)
    assert len(api.posts) == 1 and api.history_calls == 1
    env.seed.conn.execute(
        "update ops.outbox set available_at = now() where workspace_id = %s and event_id = %s",
        (env.workspace_id, signal["event_id"]),
    )
    await _dispatcher(live, api).run_workspace(env.workspace_id)
    assert len(api.posts) == 2
    first, second = api.posts
    assert first["metadata"]["event_payload"]["event_ref"] == second["metadata"]["event_payload"]["event_ref"]
    assert first["metadata"]["event_payload"]["event_id"] == str(signal["event_id"])
    assert second["metadata"]["event_payload"]["event_id"] == str(signal["event_id"])
    [row] = outbox_of(env, "seller.reply.received")
    assert row["state"] == OutboxState.DELIVERED
    attempts = env.rows(
        "select uncertain, external_receipt from ops.delivery_attempts where workspace_id = %s"
        " and outbox_id = (select id from ops.outbox where workspace_id = %s and event_id = %s)"
        " order by attempt_number",
        env.workspace_id,
        env.workspace_id,
        signal["event_id"],
    )
    assert attempts[0]["uncertain"] is True and attempts[0]["external_receipt"] is None
    # The final post is recorded as the provider's receipt (channel:ts), nothing more.
    assert attempts[-1]["uncertain"] is False
    assert attempts[-1]["external_receipt"] == f"{SLACK_CHANNEL}:1700000000.000200"


async def test_fixture_marked_signals_are_never_claimed(env: PipelineEnv) -> None:
    """Fixture lineage is stored ``blocked`` at insert (``outbox_fixture_ck`` forbids a pending
    fixture row); a pending row whose payload carries the fixture marker (TEST ARRANGEMENT,
    triggers bypassed) is never leased or posted either."""
    signal = await _signal(env)
    with pytest.raises(psycopg.errors.CheckViolation), env.seed.conn.transaction():
        env.seed.conn.execute("set local session_replication_role = replica")
        env.seed.conn.execute(
            "update ops.outbox set is_fixture = true where workspace_id = %s and event_id = %s",
            (env.workspace_id, signal["event_id"]),
        )
    with env.seed.conn.transaction():
        env.seed.conn.execute("set local session_replication_role = replica")
        env.seed.conn.execute(
            "update ops.outbox set payload = payload || '{\"fixture\": true}'::jsonb"
            " where workspace_id = %s and event_id = %s",
            (env.workspace_id, signal["event_id"]),
        )
    assert await claim_signal_events(env.ctx.db, env.workspace_id, "dispatcher-b2a", 30, 5) == []
    live = with_settings(env, slack_signal_settings(_db_url(env)))
    await approve_slack_category(live, "seller_reply")
    api = FlakySlack(fail_posts=0, history_has_post=False)
    await _dispatcher(live, api).run_workspace(env.workspace_id)
    assert api.posts == []
    [row] = outbox_of(env, "seller.reply.received")
    assert row["state"] != OutboxState.DELIVERED
