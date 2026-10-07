"""MCP Events over the authenticated endpoint (spec 22; docs/research/mcp_events_and_webhooks.md).

``server/discover`` advertises ``capabilities.events``; ``events/list`` returns the
``review.pending.v1`` descriptor to principals holding ``reviews:read`` + ``events:subscribe``;
``events/subscribe`` verifies the callback with a signed single-use challenge (`FakeCallback`
echoes or refuses it; no real network) and stores the subscription with the secret sealed;
failures use the draft JSON-RPC codes; ``events/unsubscribe`` is idempotent. SYNTHETIC only.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from suv_deals.api.middleware import RateLimit
from suv_deals.domain.enums import Role, Scope
from suv_deals.integrations.event_bridge import DEFAULT_POLICY, SubscriptionPolicy
from suv_deals.mcp.auth import ClientPrincipal, McpAccessToken, McpPrincipal, issue_api_credential
from suv_deals.mcp.events import EXTENSION_ID
from suv_deals.mcp.server import McpOptions
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.settings import Settings
from tests.integration.db.helpers import Seed
from tests.integration.persistence_core.support import system
from tests.mcp.conftest import (
    CALLBACK_URL,
    MCP_URL,
    FakeCallback,
    McpClient,
    SigningKeys,
    TokenFactory,
    Users,
    add_members,
    events_settings,
    make_settings,
    mcp_client,
    offline_db,
    whsec,
)

pytestmark = pytest.mark.db

EVENT = "review.pending.v1"


def subscribe_params(secret: str, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "name": EVENT,
        "arguments": {"profile": "primary"},
        "delivery": {"mode": "webhook", "url": CALLBACK_URL, "secret": secret},
        "cursor": None,
    }
    params.update(overrides)
    return params


def unsubscribe_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "name": EVENT,
        "arguments": {"profile": "primary"},
        "delivery": {"mode": "webhook", "url": CALLBACK_URL},
    }
    params.update(overrides)
    return params


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class EventsHarness:
    client: McpClient
    callback: FakeCallback
    users: Users
    workspace_id: UUID
    tokens: TokenFactory
    seed: Seed

    def token(self, user: UUID, role: Role) -> str:
        return self.tokens.for_role(user, role)

    @property
    def reviewer(self) -> str:
        return self.token(self.users.reviewer, Role.REVIEWER)

    def subscription_row(self, wire_id: str) -> dict[str, Any]:
        rows = self.seed.conn.execute(
            "select id, principal_id, canonical_filter, callback_url, encrypted_secret, secret_version,"
            " verification_state, verified_at, expires_at, revoked_at, revoke_reason, version"
            " from ops.event_subscriptions where workspace_id = %s order by created_at",
            (self.workspace_id,),
        ).fetchall()
        names = [
            "id",
            "principal_id",
            "canonical_filter",
            "callback_url",
            "encrypted_secret",
            "secret_version",
            "verification_state",
            "verified_at",
            "expires_at",
            "revoked_at",
            "revoke_reason",
            "version",
        ]
        records = [dict(zip(names, row, strict=True)) for row in rows]
        assert records, "no subscription stored"
        if len(records) == 1:
            return records[0]
        raise AssertionError(f"{len(records)} subscriptions; select by filter in the test ({wire_id})")


@asynccontextmanager
async def events_harness(
    db: Database,
    seed: Seed,
    keys: SigningKeys,
    tokens: TokenFactory,
    *,
    settings: Settings | None = None,
    callback: FakeCallback | None = None,
    options: McpOptions | None = None,
) -> AsyncIterator[EventsHarness]:
    workspace = seed.workspace("MCP events")
    users = add_members(seed, workspace)
    fake = callback or FakeCallback()
    async with mcp_client(
        settings or events_settings(), db, keys, events_http=fake, options=options
    ) as client:
        yield EventsHarness(
            client=client, callback=fake, users=users, workspace_id=workspace, tokens=tokens, seed=seed
        )


# --------------------------------------------------------------------------------------------
# Capability and discovery
# --------------------------------------------------------------------------------------------


class SubscriberVerifier:
    """Accepts one opaque token as a reviewer subscriber (no database needed)."""

    async def verify_token(self, token: str) -> McpAccessToken | None:
        if token != "synthetic-subscriber":
            return None
        scopes = frozenset({Scope.REVIEWS_READ, Scope.EVENTS_SUBSCRIBE})
        principal = McpPrincipal(
            workspace_id=uuid.uuid4(),
            principal_id=uuid.uuid4(),
            principal_kind="user",
            role=Role.REVIEWER,
            scopes=scopes,
            auth_mode="oauth",
        )
        return McpAccessToken(
            token="sha256:test",
            client_id="synthetic",
            scopes=[s.value for s in scopes],
            resource=MCP_URL,
            subject=str(principal.principal_id),
            principal=principal,
        )


async def test_discover_advertises_events_only_when_enabled(keys: SigningKeys) -> None:
    for settings, enabled in ((events_settings(), True), (make_settings(), False)):
        async with mcp_client(
            settings, offline_db(), keys, events_http=FakeCallback(), token_verifier=SubscriberVerifier()
        ) as client:
            assert (client.app.events is not None) is enabled
            discover = await client.result("server/discover", token="synthetic-subscriber")
            assert ("events" in discover["capabilities"]) is enabled
            assert (EXTENSION_ID in (discover["capabilities"].get("extensions") or {})) is enabled
            listed = await client.rpc("events/list", token="synthetic-subscriber")
            if enabled:
                assert [e["name"] for e in listed.json()["result"]["events"]] == [EVENT]
            else:
                assert listed.status_code == 404 and listed.json()["error"]["code"] == -32601
            unauthenticated = await client.rpc("server/discover")
            assert unauthenticated.status_code == 401


async def test_discover_and_list_with_a_mapped_principal(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    async with events_harness(db, seed, keys, tokens) as h:
        discover = await h.client.result("server/discover", token=h.reviewer)
        assert discover["capabilities"]["events"] == {}
        assert discover["capabilities"]["tools"] == {"listChanged": False}
        assert EXTENSION_ID in discover["capabilities"]["extensions"]
        listed = await h.client.result("events/list", token=h.reviewer)
        assert [e["name"] for e in listed["events"]] == [EVENT]
        descriptor = listed["events"][0]
        assert descriptor["delivery"] == ["webhook"]
        assert descriptor["inputSchema"]["additionalProperties"] is False
        assert descriptor["inputSchema"]["properties"]["profile"]["enum"] == ["primary"]
        assert set(descriptor["payloadSchema"]["required"]) == {
            "case_id",
            "case_version",
            "listing_id",
            "listing_revision",
            "readiness",
            "dashboard_url",
        }
        assert listed["resultType"] == "complete"
        viewer = await h.client.result("events/list", token=h.token(h.users.viewer, Role.VIEWER))
        assert viewer["events"] == []
        no_subscribe = h.tokens.mint(h.users.reviewer, scopes=[Scope.REVIEWS_READ])
        assert (await h.client.result("events/list", token=no_subscribe))["events"] == []


async def test_events_disabled_means_method_not_found(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    async with events_harness(db, seed, keys, tokens, settings=make_settings()) as h:
        discover = await h.client.result("server/discover", token=h.reviewer)
        assert "events" not in discover["capabilities"]
        response = await h.client.rpc("events/list", token=h.reviewer)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == -32601


async def test_enabled_optional_profiles_widen_the_input_schema(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    settings = events_settings(manual_4000_profile_enabled=True)
    async with events_harness(db, seed, keys, tokens, settings=settings) as h:
        listed = await h.client.result("events/list", token=h.reviewer)
        assert listed["events"][0]["inputSchema"]["properties"]["profile"]["enum"] == [
            "primary",
            "manual_4000",
        ]


# --------------------------------------------------------------------------------------------
# Subscribe
# --------------------------------------------------------------------------------------------


async def test_subscribe_verifies_the_callback_and_seals_the_secret(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    async with events_harness(db, seed, keys, tokens) as h:
        secret = h.callback.secret
        before = datetime.now(UTC)
        result = await h.client.result("events/subscribe", subscribe_params(secret), token=h.reviewer)
        assert result["id"].startswith("sub_") and len(result["id"]) == 36
        assert result["cursor"] is None and result["truncated"] is False
        refresh_before = parse_time(result["refreshBefore"])
        assert before + DEFAULT_POLICY.default_ttl - timedelta(minutes=1) < refresh_before
        assert refresh_before < datetime.now(UTC) + DEFAULT_POLICY.default_ttl + timedelta(minutes=1)
        assert secret not in str(result)

        assert len(h.callback.requests) == 1
        challenge = h.callback.requests[0]
        assert challenge["url"] == CALLBACK_URL
        assert challenge["payload"]["type"] == "verification" and len(challenge["payload"]["challenge"]) >= 32
        assert challenge["headers"]["webhook-id"].startswith("msg_verification_")
        assert challenge["headers"]["X-MCP-Subscription-Id"] == result["id"]

        row = h.subscription_row(result["id"])
        assert row["principal_id"] == h.users.reviewer
        assert row["verification_state"] == "verified" and row["verified_at"] is not None
        assert row["canonical_filter"] == {"profile": "primary"}
        secret_bytes = base64.b64decode(secret.removeprefix("whsec_"))
        assert secret.encode() not in bytes(row["encrypted_secret"])
        assert secret_bytes not in bytes(row["encrypted_secret"])

        # Refresh: same identity, no new challenge, new lifetime (ttl clamped to the server range).
        refreshed = await h.client.result(
            "events/subscribe", subscribe_params(secret, ttlMs=600_000), token=h.reviewer
        )
        assert refreshed["id"] == result["id"]
        assert parse_time(refreshed["refreshBefore"]) < datetime.now(UTC) + timedelta(minutes=11)
        assert len(h.callback.requests) == 1
        short = await h.client.result(
            "events/subscribe", subscribe_params(secret, ttlMs=1000), token=h.reviewer
        )
        assert parse_time(short["refreshBefore"]) > datetime.now(UTC) + DEFAULT_POLICY.min_ttl - timedelta(
            seconds=30
        )
        forever = await h.client.result(
            "events/subscribe", subscribe_params(secret, ttlMs=None), token=h.reviewer
        )
        assert parse_time(forever["refreshBefore"]) <= datetime.now(UTC) + DEFAULT_POLICY.max_ttl
        row = h.subscription_row(result["id"])
        assert row["version"] > 1 and row["revoked_at"] is None

        # Another filter for the same principal and URL reuses the cached verification.
        queue = await h.client.result(
            "events/subscribe",
            subscribe_params(secret, arguments={"profile": "primary", "queue": "fast-lane"}),
            token=h.reviewer,
        )
        assert queue["id"] != result["id"]
        assert len(h.callback.requests) == 1

        # A new secret rotates it and requires a new challenge signed with the new secret.
        h.callback.secret = whsec(b"r")
        rotated = await h.client.result(
            "events/subscribe", subscribe_params(h.callback.secret), token=h.reviewer
        )
        assert rotated["id"] == result["id"]
        assert len(h.callback.requests) == 2

        verifications = h.client.metrics.registry.get_sample_value(
            "suv_deals_callback_verifications_total", {"result": "ok"}
        )
        assert verifications == 2


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("wrong_echo", "challenge_failed"),
        ("http_500", "http_5xx"),
        ("timeout", "timeout"),
        ("refused", "connection_refused"),
    ],
)
async def test_failed_challenge_is_callback_endpoint_error(
    mode: str, reason: str, db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    async with events_harness(db, seed, keys, tokens, callback=FakeCallback(mode=mode)) as h:
        response = await h.client.rpc(
            "events/subscribe", subscribe_params(h.callback.secret), token=h.reviewer
        )
        assert response.status_code == 200  # an application-range JSON-RPC error, not a transport error
        error = response.json()["error"]
        assert error == {"code": -32015, "message": "CallbackEndpointError", "data": {"reason": reason}}
        assert h.callback.secret not in response.text and CALLBACK_URL not in response.text
        assert len(h.callback.requests) == 1
        row = h.subscription_row("failed")
        assert row["verification_state"] == "failed" and row["verified_at"] is None


async def test_subscribe_refusals_use_the_draft_error_codes(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    async with events_harness(db, seed, keys, tokens) as h:
        secret = h.callback.secret
        cases: list[tuple[dict[str, Any], int, dict[str, Any] | None]] = [
            (subscribe_params("whsec_c2hvcnQ="), -32602, {"field": "delivery.secret"}),
            (subscribe_params("plain-secret"), -32602, {"field": "delivery.secret"}),
            (
                subscribe_params(
                    secret,
                    delivery={
                        "mode": "webhook",
                        "url": "http://receiver.synthetic.example/cb",
                        "secret": secret,
                    },
                ),
                -32602,
                {"field": "url"},
            ),
            (
                subscribe_params(
                    secret, delivery={"mode": "webhook", "url": "https://10.0.0.7/cb", "secret": secret}
                ),
                -32602,
                {"field": "url"},
            ),
            (
                subscribe_params(secret, arguments={"profile": "primary", "extra": "x"}),
                -32602,
                {"field": "arguments"},
            ),
            (
                subscribe_params(secret, arguments={"profile": "manual_4000"}),
                -32602,
                {"field": "arguments.profile"},
            ),
            (subscribe_params(secret, ttlMs=-5), -32602, {"field": "ttlMs"}),
            (subscribe_params(secret, name="listing.sold.v1"), -32011, {"kind": "event"}),
            (
                subscribe_params(secret, delivery={"mode": "push", "url": CALLBACK_URL, "secret": secret}),
                -32014,
                {"feature": "deliveryMode", "value": "push"},
            ),
            ({"name": EVENT}, -32602, {"field": "arguments.profile"}),
        ]
        for params, code, data in cases:
            response = await h.client.rpc("events/subscribe", params, token=h.reviewer)
            error = response.json()["error"]
            assert error["code"] == code, (params, error)
            if data is not None:
                assert {k: error["data"][k] for k in data} == data, error
            assert secret not in response.text
        viewer = await h.client.rpc(
            "events/subscribe", subscribe_params(secret), token=h.token(h.users.viewer, Role.VIEWER)
        )
        assert viewer.json()["error"]["code"] == -32012
        assert h.callback.requests == []  # nothing was contacted for any refused request
        assert (
            h.seed.scalar(
                "select count(*) from ops.event_subscriptions where workspace_id = %s", (h.workspace_id,)
            )
            == 0
        )


async def test_external_notifications_disabled_refuses_without_contacting_anyone(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    settings = events_settings(allow_external_notifications=False)
    async with events_harness(db, seed, keys, tokens, settings=settings) as h:
        error = await h.client.error(
            "events/subscribe", subscribe_params(h.callback.secret), token=h.reviewer
        )
        assert error["code"] == -32014 and error["data"] == {"feature": "deliveryMode", "value": "webhook"}
        # Authorization is answered first: a principal without the event scopes is Forbidden.
        viewer = await h.client.error(
            "events/subscribe",
            subscribe_params(h.callback.secret),
            token=h.token(h.users.viewer, Role.VIEWER),
        )
        assert viewer["code"] == -32012
        assert h.callback.requests == []
        assert (
            h.seed.scalar(
                "select count(*) from ops.event_subscriptions where workspace_id = %s", (h.workspace_id,)
            )
            == 0
        )


async def test_unsubscribe_is_rate_limited_per_principal(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    options = McpOptions(expensive_limit=RateLimit(capacity=2, per_seconds=60.0))
    async with events_harness(db, seed, keys, tokens, options=options) as h:
        for _ in range(2):
            await h.client.result("events/unsubscribe", unsubscribe_params(), token=h.reviewer)
        limited = await h.client.error("events/unsubscribe", unsubscribe_params(), token=h.reviewer)
        assert limited["code"] == -32013 and limited["data"]["limit"] == "requests"


async def test_subscription_quota_is_resource_exhausted(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    options = McpOptions(subscription_policy=SubscriptionPolicy(max_subscriptions_per_principal=1))
    async with events_harness(db, seed, keys, tokens, options=options) as h:
        secret = h.callback.secret
        await h.client.result("events/subscribe", subscribe_params(secret), token=h.reviewer)
        error = await h.client.error(
            "events/subscribe",
            subscribe_params(secret, arguments={"profile": "primary", "queue": "second"}),
            token=h.reviewer,
        )
        assert error["code"] == -32013 and error["data"]["limit"] == "subscriptions"
        # A refresh of the existing subscription is never blocked by the quota.
        await h.client.result("events/subscribe", subscribe_params(secret), token=h.reviewer)


async def test_subscription_grants_no_review_write_access(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    async with events_harness(db, seed, keys, tokens) as h:
        subscriber = h.tokens.mint(h.users.reviewer, scopes=[Scope.REVIEWS_READ, Scope.EVENTS_SUBSCRIBE])
        await h.client.result("events/subscribe", subscribe_params(h.callback.secret), token=subscriber)
        names = [t["name"] for t in await h.client.tools(subscriber)]
        assert names == ["reviews_list_pending"]


# --------------------------------------------------------------------------------------------
# Unsubscribe
# --------------------------------------------------------------------------------------------


async def test_unsubscribe_is_idempotent_and_stops_the_subscription(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    async with events_harness(db, seed, keys, tokens) as h:
        secret = h.callback.secret
        result = await h.client.result("events/subscribe", subscribe_params(secret), token=h.reviewer)
        first = await h.client.result("events/unsubscribe", unsubscribe_params(), token=h.reviewer)
        assert {k: v for k, v in first.items() if k not in ("resultType", "_meta")} == {}
        row = h.subscription_row(result["id"])
        assert row["revoked_at"] is not None and row["revoke_reason"] == "unsubscribed"
        again = await h.client.result("events/unsubscribe", unsubscribe_params(), token=h.reviewer)
        assert {k: v for k, v in again.items() if k not in ("resultType", "_meta")} == {}
        unknown = await h.client.result(
            "events/unsubscribe",
            unsubscribe_params(delivery={"mode": "webhook", "url": "https://other.synthetic.example/cb"}),
            token=h.reviewer,
        )
        assert {k: v for k, v in unknown.items() if k not in ("resultType", "_meta")} == {}
        # Another principal cannot unsubscribe it (identity comes from the token, never params).
        other = h.token(h.users.second_reviewer, Role.REVIEWER)
        await h.client.result("events/subscribe", subscribe_params(secret), token=h.reviewer)  # reactivate
        await h.client.result("events/unsubscribe", unsubscribe_params(), token=other)
        rows = h.seed.conn.execute(
            "select revoked_at from ops.event_subscriptions where workspace_id = %s and principal_id = %s",
            (h.workspace_id, h.users.reviewer),
        ).fetchall()
        assert rows and all(r[0] is None for r in rows)
        viewer = await h.client.rpc(
            "events/unsubscribe", unsubscribe_params(), token=h.token(h.users.viewer, Role.VIEWER)
        )
        assert viewer.json()["error"]["code"] == -32012
        bad = await h.client.rpc("events/unsubscribe", {"name": EVENT}, token=h.reviewer)
        assert bad.json()["error"]["code"] == -32602


async def test_events_methods_require_authentication(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    async with events_harness(db, seed, keys, tokens) as h:
        for method in ("events/list", "events/subscribe", "events/unsubscribe"):
            response = await h.client.rpc(method, subscribe_params(h.callback.secret))
            assert response.status_code == 401
        assert h.callback.requests == []


async def test_only_principals_the_dispatcher_can_recheck_may_subscribe(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    """Before every delivery the dispatcher rechecks a membership or a stored credential. An
    OAuth machine client mapped only in configuration has neither: it is not offered the event
    and cannot subscribe (its subscription could never deliver); a machine principal behind a
    stored API credential can."""
    workspace = seed.workspace("MCP events machine")
    subscriber_scopes = frozenset({Scope.REVIEWS_READ, Scope.EVENTS_SUBSCRIBE})
    clients = {
        "synthetic-machine": ClientPrincipal(
            workspace_id=workspace, principal_id=uuid.uuid4(), role=Role.REVIEWER, scopes=subscriber_scopes
        )
    }
    callback = FakeCallback()
    async with mcp_client(
        events_settings(), db, keys, events_http=callback, client_principals=clients
    ) as client:
        machine = tokens.mint(
            "synthetic-machine", scopes=list(subscriber_scopes), extra={"client_id": "synthetic-machine"}
        )
        assert (await client.result("events/list", token=machine))["events"] == []
        refused = await client.error("events/subscribe", subscribe_params(callback.secret), token=machine)
        assert refused["code"] == -32012
        await client.result("events/unsubscribe", unsubscribe_params(), token=machine)  # still idempotent
    assert callback.requests == []
    assert (
        seed.scalar("select count(*) from ops.event_subscriptions where workspace_id = %s", (workspace,)) == 0
    )

    actor = system(workspace)
    async with unit_of_work(db, actor) as conn:
        issued = await issue_api_credential(
            conn,
            actor,
            principal_id=uuid.uuid4(),
            principal_kind="mcp_client",
            role=Role.REVIEWER,
            scopes=sorted(subscriber_scopes),
            label="SYNTHETIC event subscriber",
        )
    static = events_settings(mcp_auth_mode="static_bearer")
    async with mcp_client(static, db, keys, events_http=callback) as client:
        token = issued.token.get_secret_value()
        assert [e["name"] for e in (await client.result("events/list", token=token))["events"]] == [EVENT]
        result = await client.result("events/subscribe", subscribe_params(callback.secret), token=token)
        assert result["id"].startswith("sub_") and len(callback.requests) == 1
    stored = seed.conn.execute(
        "select credential_id, verification_state from ops.event_subscriptions where workspace_id = %s",
        (workspace,),
    ).fetchall()
    assert stored == [(issued.credential_id, "verified")]
