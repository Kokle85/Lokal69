"""D1 item 4 through the real dispatcher: a seller-reply signal that Slack refuses for good ends
``dead_letter``; the replies coalesced into it would never reach dot. The same dispatcher pass
re-emits ONE new signal for the inquiry (its newest coalesced reply), which a later pass posts;
nothing is re-emitted again. SYNTHETIC Slack double; nothing leaves the process.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
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
from suv_deals.integrations.safe_http import SafeHttpClient
from suv_deals.workers.dispatcher import Dispatcher

pytestmark = pytest.mark.db

BODY = "Guten Tag, das Fahrzeug ist noch verfügbar. Synthetic fixture reply."


class RefusingSlack:
    """SYNTHETIC Slack: the first ``refuse`` posts are refused for good (HTTP 403), later ones ok."""

    def __init__(self, refuse: int) -> None:
        self.refuse = refuse
        self.posts: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat.postMessage"):
            self.posts.append(json.loads(request.content))
            if self.refuse > 0:
                self.refuse -= 1
                return httpx.Response(403, json={"ok": False, "error": "synthetic_forbidden"})
            return httpx.Response(200, json={"ok": True, "ts": "1700000000.000300", "channel": SLACK_CHANNEL})
        return httpx.Response(404, json={"ok": False, "error": "unexpected_synthetic_route"})

    def client(self) -> SafeHttpClient:
        return SafeHttpClient(resolver=public_resolver, transport=httpx.MockTransport(self))


def _db_url(env: PipelineEnv) -> str:
    url = env.ctx.settings.database_url
    assert url is not None
    return url.get_secret_value()


async def test_a_refused_signal_with_coalesced_replies_is_re_emitted_and_posted_once(
    env: PipelineEnv,
) -> None:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    listing = await eligible_live_listing(env)
    seller = await link_seller(env, listing)
    await work(env)
    [intent] = (await pending_intents(env, sender.worker)).intents
    await worker_sends(env, sender.worker, intent)
    [inquiry] = inquiries_of(env)

    async def reply(n: int) -> Any:
        return await seller_replies(
            env,
            sender.worker,  # type: ignore[arg-type]
            inquiry["id"],
            in_reply_to=intent.rfc_message_id,
            from_address=seller.address,
            body=f"{BODY} Nachricht {n}.",
        )

    first = await reply(1)
    second = await reply(2)
    assert (first.signal_status, second.signal_status) == ("emitted", "coalesced")
    live = with_settings(env, slack_signal_settings(_db_url(env)))
    await approve_slack_category(live, "seller_reply")
    api = RefusingSlack(refuse=1)

    report = await Dispatcher(live.ctx, http=api.client(), dispatcher_id="dispatcher-d1").run_workspace(
        env.workspace_id
    )
    dead, fresh = outbox_of(env, "seller.reply.received")
    assert dead["state"] == OutboxState.DEAD_LETTER and len(api.posts) == 1
    assert report.reemitted_signals == [fresh["event_id"]]
    assert fresh["state"] == OutboxState.PENDING
    assert fresh["payload"]["reply_id"] == str(second.reply_id)
    assert fresh["dedup_key"] == f"seller.reply.received:{second.reply_id}"

    again = await Dispatcher(live.ctx, http=api.client(), dispatcher_id="dispatcher-d1").run_workspace(
        env.workspace_id
    )
    assert again.reemitted_signals == []
    signals = outbox_of(env, "seller.reply.received")
    assert [s["state"] for s in signals] == [OutboxState.DEAD_LETTER, OutboxState.DELIVERED]
    assert len(api.posts) == 2
    assert api.posts[1]["metadata"]["event_payload"]["event_id"] == str(fresh["event_id"])
    # Nothing further: one re-emit, one post.
    await Dispatcher(live.ctx, http=api.client(), dispatcher_id="dispatcher-d1").run_workspace(
        env.workspace_id
    )
    assert len(outbox_of(env, "seller.reply.received")) == 2 and len(api.posts) == 2
