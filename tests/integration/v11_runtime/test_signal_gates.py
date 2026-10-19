"""External-delivery gates of the seller-reply signal (spec 37.6/37.7, 22; safety review).

Nothing leaves the process unless EVERY gate holds: ``ALLOW_EXTERNAL_NOTIFICATIONS``, the
seller-reply signal provider, and an owner-approved, enabled AND verified Slack destination for the
``seller_reply`` category. A refused signal is visible (``blocked`` with a typed code), makes no
HTTP request at all (not even a lookup) and never falls back to native MCP Events.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from tests.integration.pipeline.support import SLACK_CHANNEL, PipelineEnv, run
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
from suv_deals.persistence import bindings_repo
from suv_deals.persistence.database import Conn
from suv_deals.workers.dispatcher import Dispatcher

pytestmark = pytest.mark.db


def _db_url(env: PipelineEnv) -> str:
    url = env.ctx.settings.database_url
    assert url is not None
    return url.get_secret_value()


class _NoNetwork:
    """Any request at all is a test failure (recorded, answered 599)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(599)

    def client(self) -> SafeHttpClient:
        return SafeHttpClient(resolver=public_resolver, transport=httpx.MockTransport(self))


async def _stored_reply(env: PipelineEnv) -> None:
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
    assert signal["state"] == OutboxState.PENDING


async def _dispatch(env: PipelineEnv, **overrides: Any) -> _NoNetwork:
    live = with_settings(env, slack_signal_settings(_db_url(env), **overrides))
    network = _NoNetwork()
    await Dispatcher(live.ctx, http=network.client(), dispatcher_id="dispatcher-gates").run_workspace(
        env.workspace_id
    )
    return network


def _no_mcp_delivery(env: PipelineEnv) -> None:
    deliveries = env.scalar(
        "select count(*) from ops.event_deliveries where workspace_id = %s", env.workspace_id
    )
    assert deliveries == 0


async def test_external_notifications_off_blocks_without_any_request(env: PipelineEnv) -> None:
    await _stored_reply(env)
    await approve_slack_category(env, "seller_reply")
    network = await _dispatch(env, allow_external_notifications=False)
    [signal] = outbox_of(env, "seller.reply.received")
    assert signal["state"] == OutboxState.BLOCKED
    assert signal["blocker_code"] == "EXTERNAL_NOTIFICATIONS_DISABLED"
    assert network.requests == []
    _no_mcp_delivery(env)


async def test_disabled_seller_reply_signal_provider_blocks_without_any_request(env: PipelineEnv) -> None:
    await _stored_reply(env)
    await approve_slack_category(env, "seller_reply")
    network = await _dispatch(env, seller_reply_signal_provider="disabled")
    [signal] = outbox_of(env, "seller.reply.received")
    assert signal["state"] == OutboxState.BLOCKED and signal["blocker_code"] == "SELLER_REPLY_SIGNAL_DISABLED"
    assert network.requests == []
    _no_mcp_delivery(env)


async def test_unverified_slack_destination_is_never_posted_to(env: PipelineEnv) -> None:
    await _stored_reply(env)
    owner = env.owner

    async def approved_not_verified(conn: Conn) -> None:
        binding = await bindings_repo.create_binding(
            conn,
            owner,
            provider="slack",
            label="SYNTHETIC unverified channel",
            external_workspace_id="T0SYNTHETIC1",
            external_channel_id=SLACK_CHANNEL,
        )
        await bindings_repo.approve_binding(
            conn, owner, binding.id, approval_reference="SYNTHETIC owner approval", expected_version=1
        )
        preference = await bindings_repo.upsert_preferences(
            conn, owner, binding.id, event_categories=["seller_reply"]
        )
        preference = await bindings_repo.approve_preferences(
            conn, owner, preference.id, approval_reference="SYNTHETIC approval", expected_version=1
        )
        await bindings_repo.set_preferences_enabled(
            conn, owner, preference.id, True, expected_version=preference.row_version
        )

    await run(env.ctx, owner, approved_not_verified)
    network = await _dispatch(env)
    [signal] = outbox_of(env, "seller.reply.received")
    assert signal["state"] == OutboxState.BLOCKED
    assert signal["blocker_code"] in ("DESTINATION_NOT_VERIFIED", "NO_ACTIVE_ROUTE"), signal
    assert network.requests == []
    _no_mcp_delivery(env)
