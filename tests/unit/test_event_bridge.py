"""MCP Events provider logic for review.pending.v1 (spec 13, 20, 22; research doc sections 3-8).

All data here is SYNTHETIC (UUIDs from the spec's synthetic examples, example.com hosts).
"""

from __future__ import annotations

import base64
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import mcp
import pytest

from suv_deals.clock import FrozenClock
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import OutboxState, ReviewState, Role, Scope
from suv_deals.errors import ErrorCode, ValidationFailed
from suv_deals.integrations import event_bridge as eb
from suv_deals.integrations.event_bridge import (
    EVENT_NAME,
    ActivationRoute,
    ActivationRouteConflict,
    BlockReason,
    BridgeStatus,
    CallbackErrorReason,
    ChallengeCheck,
    ChallengeLedger,
    DeliveryFailureReason,
    DeliveryOutcomeKind,
    DeliveryTarget,
    DispatchDecision,
    EventsProtocolError,
    JsonRpcCode,
    RetryPolicy,
    SubscriptionPolicy,
    SubscriptionStatus,
    UncertainDecision,
    VerificationCacheEntry,
    VerificationRateLimiter,
)
from suv_deals.integrations.safe_http import SafeHttpClient, SafeHttpError, SafeHttpFailure, SafeResponse
from suv_deals.integrations.webhook_signing import (
    HEADER_ID,
    HEADER_SIGNATURE,
    HEADER_SUBSCRIPTION,
    HEADER_TIMESTAMP,
    canonical_json,
    parse_whsec,
    sign,
)
from suv_deals.settings import Settings

NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)
WS = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PRINCIPAL = UUID("99999999-9999-4999-8999-999999999999")
OTHER_PRINCIPAL = UUID("88888888-8888-4888-8888-888888888888")
CALLBACK = "https://receiver.example.com/mcp-events/callback_123"
PUBLIC_IP = "93.184.216.34"
SECRET_TEXT = "whsec_" + base64.b64encode(bytes(range(32))).decode()
SECRET = parse_whsec(SECRET_TEXT)
SECRET_NEW = parse_whsec("whsec_" + base64.b64encode(bytes(range(40, 72))).decode())

# Spec section 22 internal outbox payload (synthetic) plus readiness/profile.
OUTBOX_EVENT: dict[str, Any] = {
    "schema_version": "1.0",
    "event_id": "55555555-5555-4555-8555-555555555555",
    "type": "review.pending",
    "occurred_at": "2026-10-06T10:05:00Z",
    "case_id": "44444444-4444-4444-8444-444444444444",
    "case_version": 1,
    "listing_id": "11111111-1111-4111-8111-111111111111",
    "listing_revision": 3,
    "priority": "normal",
    "dashboard_url": "https://app.example/reviews/44444444-4444-4444-8444-444444444444",
    "summary": "New research candidate; import costs need verification.",
    "deduplication_key": "review.pending:44444444-4444-4444-8444-444444444444:1",
    "readiness": "needs_import_costs",
    "profile": "primary",
}

# Spec section 22 occurrence example.
EXPECTED_OCCURRENCE = {
    "eventId": "55555555-5555-4555-8555-555555555555",
    "name": "review.pending.v1",
    "timestamp": "2026-10-06T10:05:00Z",
    "data": {
        "case_id": "44444444-4444-4444-8444-444444444444",
        "case_version": 1,
        "listing_id": "11111111-1111-4111-8111-111111111111",
        "listing_revision": 3,
        "readiness": "needs_import_costs",
        "dashboard_url": "https://app.example/reviews/44444444-4444-4444-8444-444444444444",
    },
    "cursor": None,
}


def actor(
    role: Role = Role.REVIEWER,
    scopes: frozenset[Scope] | None = None,
    principal: UUID = PRINCIPAL,
    workspace: UUID = WS,
) -> ActorContext:
    return ActorContext(
        workspace_id=workspace,
        principal_id=principal,
        principal_kind="mcp_client",
        role=role,
        scopes=scopes if scopes is not None else frozenset({Scope.REVIEWS_READ, Scope.EVENTS_SUBSCRIBE}),
        request_id="req-test",
    )


def params(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": EVENT_NAME,
        "arguments": {"profile": "primary"},
        "delivery": {"mode": "webhook", "url": CALLBACK, "secret": SECRET_TEXT},
        "cursor": None,
    }
    base.update(overrides)
    return base


def rpc_error(call: Callable[[], object]) -> EventsProtocolError:
    with pytest.raises(EventsProtocolError) as info:
        call()
    return info.value


Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def http_with(handler: Handler, *answers: str) -> SafeHttpClient:
    async def resolve(host: str, port: int) -> list[str]:
        return list(answers or (PUBLIC_IP,))

    return SafeHttpClient(resolver=resolve, transport=httpx.MockTransport(handler))


def target(**overrides: Any) -> DeliveryTarget:
    values: dict[str, Any] = {
        "subscription_id": eb.subscription_identity(
            PRINCIPAL, CALLBACK, EVENT_NAME, {"profile": "primary"}, workspace_id=WS
        ),
        "workspace_id": WS,
        "principal_id": PRINCIPAL,
        "event_name": EVENT_NAME,
        "arguments": {"profile": "primary"},
        "callback_url": CALLBACK,
        "secrets": (SECRET,),
        "status": SubscriptionStatus.ACTIVE,
        "expires_at": NOW + timedelta(hours=12),
        "verified_at": NOW - timedelta(minutes=1),
    }
    values.update(overrides)
    return DeliveryTarget(**values)


async def allow(_: DeliveryTarget) -> bool:
    return True


async def deny(_: DeliveryTarget) -> bool:
    return False


# =========================================================================== events/list


def test_descriptor_shape_and_schemas() -> None:
    descriptor = eb.event_descriptor()
    assert descriptor["name"] == "review.pending.v1"
    assert descriptor["delivery"] == ["webhook"]
    schema = descriptor["inputSchema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["profile"]
    assert set(schema["properties"]) == {"profile", "queue"}
    assert schema["properties"]["profile"]["enum"] == ["primary", "manual_4000", "below_target_watch"]
    payload = descriptor["payloadSchema"]
    assert payload["additionalProperties"] is False
    assert set(payload["required"]) == {
        "case_id",
        "case_version",
        "listing_id",
        "listing_revision",
        "readiness",
        "dashboard_url",
    }
    json.dumps(descriptor)  # JSON-serializable


def test_list_events_respects_scopes_and_permitted_profiles() -> None:
    assert eb.list_events(actor())["events"][0]["name"] == EVENT_NAME
    viewer = actor(role=Role.VIEWER, scopes=frozenset({Scope.REVIEWS_READ, Scope.DEALS_READ}))
    assert eb.list_events(viewer) == {"events": []}
    no_read = actor(scopes=frozenset({Scope.EVENTS_SUBSCRIBE}))
    assert eb.list_events(no_read) == {"events": []}
    narrowed = eb.list_events(actor(), permitted_profiles=("primary",))
    assert narrowed["events"][0]["inputSchema"]["properties"]["profile"]["enum"] == ["primary"]
    assert eb.list_events(actor(), permitted_profiles=()) == {"events": []}
    system = ActorContext.system(WS, "req-sys")
    assert eb.list_events(system) == {"events": []}


def test_list_events_cursor_validation() -> None:
    assert eb.list_events(actor(), {"cursor": None})["events"]
    err = rpc_error(lambda: eb.list_events(actor(), {"cursor": 5}))
    assert err.rpc_code is JsonRpcCode.INVALID_PARAMS


# =========================================================================== events/subscribe


def test_subscribe_happy_path_and_result_shape() -> None:
    clock = FrozenClock(NOW)
    request = eb.validate_subscribe_params(params(), actor(), clock=clock)
    assert request.subscription_id.startswith("sub_") and len(request.subscription_id) == 36
    assert request.callback_host == "receiver.example.com"
    assert request.ttl_kind == "default"
    assert request.granted_ttl == timedelta(hours=12)
    assert request.secret.matches(SECRET)
    assert request.result() == {
        "id": request.subscription_id,
        "refreshBefore": "2026-10-06T22:00:00Z",
        "cursor": None,
        "truncated": False,
    }


def test_identity_is_deterministic_and_order_independent() -> None:
    clock = FrozenClock(NOW)
    first = eb.validate_subscribe_params(
        params(arguments={"profile": "primary", "queue": "q1"}), actor(), clock=clock
    )
    reordered = eb.validate_subscribe_params(
        params(arguments={"queue": "q1", "profile": "primary"}), actor(), clock=clock
    )
    assert first.subscription_id == reordered.subscription_id
    assert first.canonical_arguments == '{"profile":"primary","queue":"q1"}'


@pytest.mark.parametrize(
    "change",
    [
        {"arguments": {"profile": "manual_4000"}},
        {"arguments": {"profile": "primary", "queue": "q2"}},
        {"delivery": {"mode": "webhook", "url": CALLBACK + "x", "secret": SECRET_TEXT}},
    ],
)
def test_identity_changes_with_url_or_arguments(change: dict[str, Any]) -> None:
    clock = FrozenClock(NOW)
    base = eb.validate_subscribe_params(params(), actor(), clock=clock).subscription_id
    assert eb.validate_subscribe_params(params(**change), actor(), clock=clock).subscription_id != base


def test_identity_is_bound_to_authenticated_principal_not_body_fields() -> None:
    clock = FrozenClock(NOW)
    mine = eb.validate_subscribe_params(params(), actor(), clock=clock)
    spoof = eb.validate_subscribe_params(
        params(principal_id=str(OTHER_PRINCIPAL), workspace_id="x"), actor(), clock=clock
    )
    assert spoof.subscription_id == mine.subscription_id
    assert spoof.principal_id == PRINCIPAL
    other = eb.validate_subscribe_params(params(), actor(principal=OTHER_PRINCIPAL), clock=clock)
    assert other.subscription_id != mine.subscription_id
    other_ws = eb.validate_subscribe_params(
        params(), actor(workspace=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")), clock=clock
    )
    assert other_ws.subscription_id != mine.subscription_id


def test_secret_rotation_keeps_identity() -> None:
    clock = FrozenClock(NOW)
    first = eb.validate_subscribe_params(params(), actor(), clock=clock)
    rotated = eb.validate_subscribe_params(
        params(delivery={"mode": "webhook", "url": CALLBACK, "secret": SECRET_NEW.to_whsec()}),
        actor(),
        clock=clock,
    )
    assert rotated.subscription_id == first.subscription_id
    assert not rotated.secret.matches(first.secret)


@pytest.mark.parametrize(
    ("ttl", "kind", "granted"),
    [
        (3_600_000, "value", timedelta(hours=1)),
        (3_600_000.0, "value", timedelta(hours=1)),
        (1_000, "value", timedelta(minutes=5)),  # clamped up to the server minimum
        (0, "value", timedelta(minutes=5)),
        (10**12, "value", timedelta(hours=24)),  # grant <= n but never above the server maximum
        (10**30, "value", timedelta(hours=24)),  # no timedelta overflow
        (1e300, "value", timedelta(hours=24)),
        (None, "no_expiry", timedelta(hours=24)),  # MVP: finite grant even for null
    ],
)
def test_ttl_semantics(ttl: object, kind: str, granted: timedelta) -> None:
    request = eb.validate_subscribe_params(params(ttlMs=ttl), actor(), clock=FrozenClock(NOW))
    assert request.ttl_kind == kind
    assert request.granted_ttl == granted
    assert request.result()["refreshBefore"] is not None
    assert request.expires_at == NOW + granted


@pytest.mark.parametrize("ttl", [-1, "3600000", True, 1.5, [], {}])
def test_invalid_ttl_rejected(ttl: object) -> None:
    err = rpc_error(lambda: eb.validate_subscribe_params(params(ttlMs=ttl), actor(), clock=FrozenClock(NOW)))
    assert err.rpc_code is JsonRpcCode.INVALID_PARAMS


def test_max_age_accepted_and_ignored() -> None:
    clock = FrozenClock(NOW)
    with_age = eb.validate_subscribe_params(params(maxAgeMs=60_000), actor(), clock=clock)
    assert with_age.result() == eb.validate_subscribe_params(params(), actor(), clock=clock).result()
    assert eb.validate_subscribe_params(params(maxAgeMs=None), actor(), clock=clock)
    err = rpc_error(lambda: eb.validate_subscribe_params(params(maxAgeMs="1"), actor(), clock=clock))
    assert err.rpc_code is JsonRpcCode.INVALID_PARAMS


def test_custom_policy_bounds() -> None:
    policy = SubscriptionPolicy(
        default_ttl=timedelta(hours=1), min_ttl=timedelta(minutes=10), max_ttl=timedelta(hours=2)
    )
    request = eb.validate_subscribe_params(params(), actor(), clock=FrozenClock(NOW), policy=policy)
    assert request.granted_ttl == timedelta(hours=1)
    with pytest.raises(ValueError):
        SubscriptionPolicy(min_ttl=timedelta(hours=2), default_ttl=timedelta(hours=1))


def test_supplied_cursor_is_ignored_and_result_is_null_cursor_not_truncated() -> None:
    # Research doc sections 4 and 13: an event type without replay answers
    # `cursor: null, truncated: false` (the pending-review tool is the catch-up path).
    request = eb.validate_subscribe_params(params(cursor="opaque"), actor(), clock=FrozenClock(NOW))
    assert request.cursor_supplied
    assert request.result()["cursor"] is None
    assert request.result()["truncated"] is False
    err = rpc_error(lambda: eb.validate_subscribe_params(params(cursor=7), actor(), clock=FrozenClock(NOW)))
    assert err.rpc_code is JsonRpcCode.INVALID_PARAMS


def test_unknown_event_name_is_not_found() -> None:
    err = rpc_error(
        lambda: eb.validate_subscribe_params(params(name="comment.created"), actor(), clock=FrozenClock(NOW))
    )
    assert err.rpc_code is JsonRpcCode.NOT_FOUND
    assert err.to_jsonrpc_error() == {"code": -32011, "message": "NotFound", "data": {"kind": "event"}}


@pytest.mark.parametrize(
    "bad",
    [
        {"name": None},
        {"name": ""},
        {"arguments": []},
        {"arguments": {}},
        {"arguments": {"profile": "primary", "extra": 1}},
        {"arguments": {"profile": "secret_profile"}},
        {"arguments": {"profile": 1}},
        {"arguments": {"profile": "primary", "queue": "UPPER"}},
        {"arguments": {"profile": "primary", "queue": "x" * 65}},
        {"arguments": {"profile": "primary", "queue": 3}},
        {"delivery": None},
        {"delivery": {"mode": 5, "url": CALLBACK, "secret": SECRET_TEXT}},
        {"delivery": {"mode": "webhook", "url": CALLBACK}},
        {"delivery": {"mode": "webhook", "url": CALLBACK, "secret": "whsec_short"}},
        {"delivery": {"mode": "webhook", "url": CALLBACK, "secret": "not-a-whsec"}},
        {"delivery": {"mode": "webhook", "secret": SECRET_TEXT}},
        {"delivery": {"mode": "webhook", "url": "http://receiver.example.com/x", "secret": SECRET_TEXT}},
    ],
)
def test_invalid_params_rejected(bad: dict[str, Any]) -> None:
    err = rpc_error(lambda: eb.validate_subscribe_params(params(**bad), actor(), clock=FrozenClock(NOW)))
    assert err.rpc_code is JsonRpcCode.INVALID_PARAMS
    assert err.code is ErrorCode.VALIDATION_ERROR
    assert SECRET_TEXT not in json.dumps(err.to_jsonrpc_error())


def test_disabled_profile_rejected_by_workspace_policy() -> None:
    err = rpc_error(
        lambda: eb.validate_subscribe_params(
            params(arguments={"profile": "manual_4000"}),
            actor(),
            clock=FrozenClock(NOW),
            permitted_profiles=("primary",),
        )
    )
    assert err.rpc_code is JsonRpcCode.INVALID_PARAMS


def test_params_must_be_object() -> None:
    err = rpc_error(lambda: eb.validate_subscribe_params([1], actor(), clock=FrozenClock(NOW)))
    assert err.rpc_code is JsonRpcCode.INVALID_PARAMS


@pytest.mark.parametrize("mode", ["push", "poll", "stream", "sse"])
def test_non_webhook_delivery_mode_unsupported(mode: str) -> None:
    err = rpc_error(
        lambda: eb.validate_subscribe_params(
            params(delivery={"mode": mode, "url": CALLBACK, "secret": SECRET_TEXT}),
            actor(),
            clock=FrozenClock(NOW),
        )
    )
    assert err.rpc_code is JsonRpcCode.UNSUPPORTED
    assert err.rpc_data == {"feature": "deliveryMode", "value": mode}


@pytest.mark.parametrize(
    "who",
    [
        actor(role=Role.VIEWER, scopes=frozenset({Scope.REVIEWS_READ})),
        actor(scopes=frozenset({Scope.EVENTS_SUBSCRIBE})),
        actor(scopes=frozenset({Scope.REVIEWS_READ, Scope.REVIEWS_WRITE})),
        ActorContext.system(WS, "req-sys"),
    ],
)
def test_forbidden_without_both_scopes_or_for_system(who: ActorContext) -> None:
    err = rpc_error(lambda: eb.validate_subscribe_params(params(), who, clock=FrozenClock(NOW)))
    assert err.rpc_code is JsonRpcCode.FORBIDDEN
    assert err.code is ErrorCode.FORBIDDEN


def test_subscription_does_not_require_or_grant_review_write() -> None:
    reader = actor(scopes=frozenset({Scope.REVIEWS_READ, Scope.EVENTS_SUBSCRIBE}))
    request = eb.validate_subscribe_params(params(), reader, clock=FrozenClock(NOW))
    assert request.principal_id == PRINCIPAL
    assert not reader.has(Scope.REVIEWS_WRITE)


def test_quota() -> None:
    eb.check_subscription_quota(9, is_refresh=False)
    eb.check_subscription_quota(10, is_refresh=True)
    err = rpc_error(lambda: eb.check_subscription_quota(10, is_refresh=False))
    assert err.rpc_code is JsonRpcCode.RESOURCE_EXHAUSTED
    assert err.rpc_data == {"limit": "subscriptions", "max": 10}


def test_delivery_status_payload() -> None:
    status = eb.delivery_status_payload(
        active=True, last_delivery_at=NOW, last_error=CallbackErrorReason.TIMEOUT, failed_since=NOW
    )
    assert status == {
        "active": True,
        "lastDeliveryAt": "2026-10-06T10:00:00Z",
        "lastError": "timeout",
        "failedSince": "2026-10-06T10:00:00Z",
    }
    request = eb.validate_subscribe_params(params(), actor(), clock=FrozenClock(NOW))
    assert eb.subscribe_result(request, delivery_status=status)["deliveryStatus"] == status


# =========================================================================== JSON-RPC errors


@pytest.mark.parametrize(
    ("err", "code", "data"),
    [
        (eb.invalid_params("bad", field_name="url"), -32602, {"detail": "bad", "field": "url"}),
        (eb.not_found("subscription"), -32011, {"kind": "subscription"}),
        (eb.forbidden("nope"), -32012, {"detail": "nope"}),
        (eb.resource_exhausted("subscriptions", 3), -32013, {"limit": "subscriptions", "max": 3}),
        (eb.unsupported("deliveryMode", "push"), -32014, {"feature": "deliveryMode", "value": "push"}),
        (
            eb.callback_endpoint_error(CallbackErrorReason.CHALLENGE_FAILED),
            -32015,
            {"reason": "challenge_failed"},
        ),
    ],
)
def test_jsonrpc_error_helpers(err: EventsProtocolError, code: int, data: dict[str, Any]) -> None:
    payload = err.to_jsonrpc_error()
    assert payload["code"] == code
    assert payload["data"] == data
    converted = err.to_mcp_error()
    assert isinstance(converted, mcp.MCPError)


def test_callback_error_message_matches_research_probe() -> None:
    payload = eb.callback_endpoint_error(CallbackErrorReason.TIMEOUT).to_jsonrpc_error()
    assert payload == {"code": -32015, "message": "CallbackEndpointError", "data": {"reason": "timeout"}}


def test_unsupported_hides_non_scalar_values() -> None:
    assert eb.unsupported("deliveryMode", {"x": 1}).rpc_data == {"feature": "deliveryMode", "value": "dict"}


# =========================================================================== events/unsubscribe


def test_unsubscribe_resolves_same_identity_and_is_idempotent() -> None:
    sub = eb.validate_subscribe_params(
        params(arguments={"profile": "primary", "queue": "q1"}), actor(), clock=FrozenClock(NOW)
    )
    unsub = eb.validate_unsubscribe_params(
        {
            "name": EVENT_NAME,
            "arguments": {"queue": "q1", "profile": "primary"},
            "delivery": {"mode": "webhook", "url": CALLBACK},
        },
        actor(),
    )
    assert unsub.subscription_id == sub.subscription_id
    assert eb.unsubscribe_result() == {}


def test_unsubscribe_unknown_subscription_still_returns_empty() -> None:
    unsub = eb.validate_unsubscribe_params(
        {"name": "nothing.v9", "arguments": {"x": 1}, "delivery": {"url": "https://nowhere.example.org/x"}},
        actor(),
    )
    assert unsub.subscription_id.startswith("sub_")
    assert eb.unsubscribe_result() == {}


def test_unsubscribe_does_not_contact_or_ssrf_check_the_url() -> None:
    unsub = eb.validate_unsubscribe_params(
        {"name": EVENT_NAME, "arguments": {}, "delivery": {"url": "http://10.0.0.1/x"}}, actor()
    )
    assert unsub.principal_id == PRINCIPAL


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {"name": EVENT_NAME},
        {"name": EVENT_NAME, "arguments": [], "delivery": {"url": CALLBACK}},
        {"name": EVENT_NAME, "arguments": {}, "delivery": {"url": ""}},
        {"name": 3, "arguments": {}, "delivery": {"url": CALLBACK}},
    ],
)
def test_unsubscribe_invalid_params(bad: object) -> None:
    err = rpc_error(lambda: eb.validate_unsubscribe_params(bad, actor()))
    assert err.rpc_code is JsonRpcCode.INVALID_PARAMS


def test_unsubscribe_requires_events_scope() -> None:
    err = rpc_error(
        lambda: eb.validate_unsubscribe_params(
            {"name": EVENT_NAME, "arguments": {}, "delivery": {"url": CALLBACK}},
            actor(role=Role.VIEWER, scopes=frozenset({Scope.REVIEWS_READ})),
        )
    )
    assert err.rpc_code is JsonRpcCode.FORBIDDEN


# =========================================================================== verification challenge


class Receiver:
    """Synthetic callback receiver used by the challenge tests."""

    def __init__(
        self,
        *,
        status: int = 200,
        echo: Callable[[str], object] | None = None,
        raw_body: bytes | None = None,
        exc: Exception | None = None,
        on_request: Callable[[], None] | None = None,
    ) -> None:
        self.status = status
        self.echo = echo or (lambda challenge: challenge)
        self.raw_body = raw_body
        self.exc = exc
        self.on_request = on_request
        self.requests: list[httpx.Request] = []
        self.challenges: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        self.challenges.append(body["challenge"])
        if self.on_request:
            self.on_request()
        if self.exc is not None:
            raise self.exc
        if self.raw_body is not None:
            return httpx.Response(self.status, content=self.raw_body)
        return httpx.Response(self.status, json={"challenge": self.echo(body["challenge"])})


SUB_ID = eb.subscription_identity(PRINCIPAL, CALLBACK, EVENT_NAME, {"profile": "primary"}, workspace_id=WS)


async def challenge(receiver: Receiver, **kwargs: Any) -> eb.VerificationResult:
    clock = kwargs.pop("clock", FrozenClock(NOW))
    async with http_with(receiver) as http:
        return await eb.run_verification_challenge(CALLBACK, SECRET, SUB_ID, http, clock=clock, **kwargs)


async def test_challenge_success_signed_and_shaped() -> None:
    receiver = Receiver()
    result = await challenge(receiver)
    assert result.ok
    assert result.reason is None
    assert result.verified_at == NOW
    assert result.secret_fingerprint == SECRET.fingerprint
    request = receiver.requests[0]
    body = json.loads(request.content)
    assert set(body) == {"type", "challenge"}
    assert body["type"] == "verification"
    assert len(body["challenge"]) >= 43  # 32 random bytes, urlsafe base64
    assert request.headers[HEADER_ID].startswith("msg_verification_")
    assert "." not in request.headers[HEADER_ID]
    assert request.headers[HEADER_SUBSCRIPTION] == SUB_ID
    assert request.headers[HEADER_TIMESTAMP] == str(int(NOW.timestamp()))
    expected = sign(SECRET, request.headers[HEADER_ID], NOW, request.content.decode())
    assert request.headers[HEADER_SIGNATURE] == expected
    assert request.headers["content-type"] == "application/json"


async def test_each_challenge_is_fresh() -> None:
    receiver = Receiver()
    await challenge(receiver)
    await challenge(receiver)
    assert receiver.challenges[0] != receiver.challenges[1]
    assert receiver.requests[0].headers[HEADER_ID] != receiver.requests[1].headers[HEADER_ID]


async def test_challenge_mismatch_fails() -> None:
    result = await challenge(Receiver(echo=lambda c: c[:-1] + ("A" if c[-1] != "A" else "B")))
    assert not result.ok
    assert result.reason is CallbackErrorReason.CHALLENGE_FAILED
    assert result.detail == ChallengeCheck.MISMATCH.value
    with pytest.raises(EventsProtocolError) as info:
        result.raise_for_failure()
    assert info.value.to_jsonrpc_error()["data"] == {"reason": "challenge_failed"}


async def test_reused_old_challenge_echo_fails() -> None:
    first = Receiver()
    assert (await challenge(first)).ok
    stale = first.challenges[0]
    result = await challenge(Receiver(echo=lambda _: stale))
    assert result.reason is CallbackErrorReason.CHALLENGE_FAILED


@pytest.mark.parametrize(
    "raw", [b"", b"not json", b"[]", b'{"challenge": 5}', b'{"other": "x"}', b"\xff\xfe"]
)
async def test_bad_echo_bodies_fail(raw: bytes) -> None:
    result = await challenge(Receiver(raw_body=raw))
    assert result.reason is CallbackErrorReason.CHALLENGE_FAILED


async def test_oversized_echo_body_fails() -> None:
    result = await challenge(Receiver(raw_body=b'{"challenge":"' + b"x" * 10_000 + b'"}'))
    assert result.reason is CallbackErrorReason.CHALLENGE_FAILED


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (400, CallbackErrorReason.HTTP_4XX),
        (401, CallbackErrorReason.HTTP_4XX),
        (404, CallbackErrorReason.HTTP_4XX),
        (410, CallbackErrorReason.HTTP_4XX),
        (500, CallbackErrorReason.HTTP_5XX),
        (503, CallbackErrorReason.HTTP_5XX),
    ],
)
async def test_non_2xx_challenge_fails_even_with_correct_echo(
    status: int, reason: CallbackErrorReason
) -> None:
    result = await challenge(Receiver(status=status))
    assert not result.ok
    assert result.reason is reason
    assert result.status_code == status


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_redirect_during_challenge_fails_without_following(status: int) -> None:
    receiver = Receiver(status=status)
    result = await challenge(receiver)
    assert result.reason is CallbackErrorReason.CHALLENGE_FAILED
    assert len(receiver.requests) == 1


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (httpx.ReadTimeout("slow"), CallbackErrorReason.TIMEOUT),
        (httpx.ConnectTimeout("slow"), CallbackErrorReason.TIMEOUT),
        (httpx.ConnectError("refused"), CallbackErrorReason.CONNECTION_REFUSED),
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] bad cert"), CallbackErrorReason.TLS_ERROR),
        (httpx.ReadError("reset"), CallbackErrorReason.CONNECTION_REFUSED),
    ],
)
async def test_transport_failures_map_to_draft_reasons(exc: Exception, reason: CallbackErrorReason) -> None:
    result = await challenge(Receiver(exc=exc))
    assert result.reason is reason


async def test_late_echo_beyond_60_seconds_fails() -> None:
    clock = FrozenClock(NOW)
    receiver = Receiver(on_request=lambda: clock.advance(timedelta(seconds=61)))
    result = await challenge(receiver, clock=clock)
    assert result.reason is CallbackErrorReason.CHALLENGE_FAILED
    assert result.detail == ChallengeCheck.EXPIRED.value


async def test_echo_within_60_seconds_succeeds() -> None:
    clock = FrozenClock(NOW)
    receiver = Receiver(on_request=lambda: clock.advance(timedelta(seconds=59)))
    assert (await challenge(receiver, clock=clock)).ok


def test_ledger_single_use_and_expiry() -> None:
    ledger = ChallengeLedger()
    token = ledger.issue(NOW, lambda: "t" * 43)
    assert ledger.consume(token, token, NOW) is ChallengeCheck.OK
    assert ledger.consume(token, token, NOW) is ChallengeCheck.UNKNOWN_OR_REUSED
    token2 = ledger.issue(NOW, lambda: "u" * 43)
    assert ledger.consume(token2, token2, NOW + timedelta(seconds=61)) is ChallengeCheck.EXPIRED
    token3 = ledger.issue(NOW, lambda: "v" * 43)
    assert ledger.consume(token3, None, NOW) is ChallengeCheck.MISMATCH
    assert ledger.consume(token3, token3, NOW) is ChallengeCheck.UNKNOWN_OR_REUSED  # burned on mismatch
    assert len(ledger) == 0


def test_ledger_rejects_weak_or_duplicate_tokens_and_bounds_outstanding() -> None:
    ledger = ChallengeLedger(max_outstanding=2)
    with pytest.raises(ValueError):
        ledger.issue(NOW, lambda: "short")
    ledger.issue(NOW, lambda: "a" * 43)
    with pytest.raises(ValueError):
        ledger.issue(NOW, lambda: "a" * 43)
    ledger.issue(NOW, lambda: "b" * 43)
    err = rpc_error(lambda: ledger.issue(NOW, lambda: "c" * 43))
    assert err.rpc_code is JsonRpcCode.RESOURCE_EXHAUSTED
    # expired entries are pruned on issue
    ledger.issue(NOW + timedelta(seconds=61), lambda: "d" * 43)


async def test_verification_rate_limited_per_host() -> None:
    limiter = VerificationRateLimiter(max_attempts=2, window=timedelta(minutes=1))
    receiver = Receiver()
    assert (await challenge(receiver, rate_limiter=limiter)).ok
    assert (await challenge(receiver, rate_limiter=limiter)).ok
    with pytest.raises(EventsProtocolError) as info:
        await challenge(receiver, rate_limiter=limiter)
    assert info.value.rpc_code is JsonRpcCode.RESOURCE_EXHAUSTED
    assert len(receiver.requests) == 2
    assert limiter.allow("receiver.example.com", NOW + timedelta(minutes=2))
    assert limiter.allow("other.example.com", NOW)


def test_rate_limiter_bounds_host_count() -> None:
    limiter = VerificationRateLimiter(max_hosts=3)
    for i in range(10):
        assert limiter.allow(f"h{i}.example.com", NOW + timedelta(minutes=i * 2))
    assert len(limiter._attempts) <= 3


def test_needs_verification_cache_rules() -> None:
    key = eb.verification_cache_key(PRINCIPAL, CALLBACK, workspace_id=WS)
    entry = VerificationCacheEntry(cache_key=key, verified_at=NOW, secret_fingerprint=SECRET.fingerprint)
    kwargs: dict[str, Any] = {"principal_id": PRINCIPAL, "callback_url": CALLBACK, "workspace_id": WS}
    assert eb.needs_verification(None, secret=SECRET, now=NOW, **kwargs)
    assert not eb.needs_verification(entry, secret=SECRET, now=NOW + timedelta(hours=1), **kwargs)
    assert eb.needs_verification(entry, secret=SECRET, now=NOW + timedelta(hours=24), **kwargs)
    assert eb.needs_verification(entry, secret=SECRET_NEW, now=NOW, **kwargs)  # rotation
    assert eb.needs_verification(
        entry, secret=SECRET, now=NOW, principal_id=PRINCIPAL, callback_url=CALLBACK + "2", workspace_id=WS
    )
    assert eb.needs_verification(
        entry, secret=SECRET, now=NOW, principal_id=OTHER_PRINCIPAL, callback_url=CALLBACK, workspace_id=WS
    )
    future = VerificationCacheEntry(key, NOW + timedelta(hours=1), SECRET.fingerprint)
    assert eb.needs_verification(future, secret=SECRET, now=NOW, **kwargs)


def test_verification_cache_covers_all_arguments_for_principal_and_url() -> None:
    clock = FrozenClock(NOW)
    a = eb.validate_subscribe_params(params(), actor(), clock=clock)
    b = eb.validate_subscribe_params(params(arguments={"profile": "manual_4000"}), actor(), clock=clock)
    assert a.subscription_id != b.subscription_id
    assert a.verification_cache_key == b.verification_cache_key


# =========================================================================== lifecycle


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"status": SubscriptionStatus.UNSUBSCRIBED}, BlockReason.UNSUBSCRIBED),
        ({"status": SubscriptionStatus.REVOKED}, BlockReason.REVOKED),
        ({"status": SubscriptionStatus.PENDING_VERIFICATION}, BlockReason.UNVERIFIED),
        ({"verified_at": None}, BlockReason.UNVERIFIED),
        ({"expires_at": NOW}, BlockReason.EXPIRED),
        ({"secrets": ()}, BlockReason.NO_SECRET),
        ({"event_name": "other.v1"}, BlockReason.WRONG_EVENT),
        ({}, None),
    ],
)
def test_delivery_block_reasons(overrides: dict[str, Any], reason: BlockReason | None) -> None:
    assert eb.delivery_block_reason(target(**overrides), NOW) is reason


def test_signing_secrets_rotation_window() -> None:
    until = NOW + timedelta(hours=1)
    assert eb.signing_secrets(SECRET_NEW, SECRET, until, now=NOW) == (SECRET_NEW, SECRET)
    assert eb.signing_secrets(SECRET_NEW, SECRET, until, now=until) == (SECRET_NEW,)
    assert eb.signing_secrets(SECRET_NEW, None, None, now=NOW) == (SECRET_NEW,)
    assert eb.signing_secrets(SECRET, SECRET, until, now=NOW) == (SECRET,)


def test_target_repr_hides_secrets_and_url() -> None:
    text = repr(target())
    assert "receiver.example.com" not in text
    assert SECRET_TEXT not in text


# =========================================================================== occurrence


def test_build_occurrence_matches_spec_example_exactly() -> None:
    occurrence = eb.build_occurrence(OUTBOX_EVENT)
    assert occurrence == EXPECTED_OCCURRENCE
    assert "summary" not in json.dumps(occurrence)
    assert "priority" not in occurrence["data"]


def test_occurrence_timestamp_converted_to_utc_z() -> None:
    event = {**OUTBOX_EVENT, "occurred_at": "2026-10-06T12:05:00.250+02:00"}
    assert eb.build_occurrence(event)["timestamp"] == "2026-10-06T10:05:00.250Z"


def test_missing_readiness_stays_unknown() -> None:
    event = {k: v for k, v in OUTBOX_EVENT.items() if k != "readiness"}
    assert eb.build_occurrence(event)["data"]["readiness"] == "unknown"


@pytest.mark.parametrize(
    "change",
    [
        {"occurred_at": "2026-10-06T10:05:00"},  # naive
        {"case_version": "1"},
        {"case_version": True},
        {"case_version": 0},
        {"listing_revision": -1},
        {"event_id": "not-a-uuid"},
        {"type": "review.decided"},
        {"readiness": "Needs Costs!"},
        {"dashboard_url": "https://app.example/reviews/x?token=abc"},
        {"dashboard_url": "https://app.example/reviews/x?access_token=abc"},
        {"dashboard_url": "https://user:pw@app.example/reviews/x"},
        {"dashboard_url": "http://app.example/reviews/x"},
        {"dashboard_url": "javascript:alert(1)"},
        {"dashboard_url": "https://app.example/reviews/x#frag"},
        {"queue": "Bad Queue"},
        {"profile": "unknown_profile"},
    ],
)
def test_invalid_outbox_payload_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationFailed) as info:
        eb.build_occurrence({**OUTBOX_EVENT, **change})
    assert "abc" not in info.value.message


@pytest.mark.parametrize(
    ("url", "local_ok", "problem"),
    [
        ("https://app.example/reviews/1", False, None),
        ("http://127.0.0.1:8000/reviews/1", True, None),
        ("http://127.0.0.1:8000/reviews/1", False, "https"),
        ("https://app.example/reviews/1?sig=abc", False, "tokens"),
        ("https://app.example/reviews/1?X-Amz-Signature=abc", False, "tokens"),
        ("https://app.example/re views/1", False, "whitespace"),
        ("https://app.example/reviews/1\r\nX: y", False, "whitespace"),
        ("https://[::1/reviews", False, "malformed"),
        ("", False, "2048"),
        ("https://app.example/" + "a" * 2049, False, "2048"),
    ],
)
def test_dashboard_url_problem(url: str, local_ok: bool, problem: str | None) -> None:
    found = eb.dashboard_url_problem(url, allow_local_http=local_ok)
    if problem is None:
        assert found is None
    else:
        assert found is not None and problem in found
        assert "abc" not in found


def test_local_dev_dashboard_over_http_is_allowed() -> None:
    event = {**OUTBOX_EVENT, "dashboard_url": "http://127.0.0.1:8000/reviews/1"}
    assert eb.build_occurrence(event)["data"]["dashboard_url"] == "http://127.0.0.1:8000/reviews/1"


def test_filters_are_applied_server_side() -> None:
    signal = eb.parse_signal({**OUTBOX_EVENT, "queue": "q1"})
    assert eb.matches_filters({"profile": "primary"}, signal)
    assert eb.matches_filters({"profile": "primary", "queue": "q1"}, signal)
    assert not eb.matches_filters({"profile": "primary", "queue": "q2"}, signal)
    assert not eb.matches_filters({"profile": "manual_4000"}, signal)
    no_profile = eb.parse_signal({k: v for k, v in OUTBOX_EVENT.items() if k != "profile"})
    assert not eb.matches_filters({"profile": "primary"}, no_profile)
    no_queue = eb.parse_signal(OUTBOX_EVENT)
    assert not eb.matches_filters({"profile": "primary", "queue": "q1"}, no_queue)


# =========================================================================== dispatch decisions


SIGNAL = eb.parse_signal(OUTBOX_EVENT)


@pytest.mark.parametrize(
    ("existing", "version", "state", "expected"),
    [
        (None, 1, ReviewState.PENDING, DispatchDecision.SEND),
        (OutboxState.PENDING, 1, ReviewState.PENDING, DispatchDecision.SEND),
        (OutboxState.RETRY_WAIT, 1, ReviewState.PENDING, DispatchDecision.SEND),
        (OutboxState.DELIVERED, 1, ReviewState.PENDING, DispatchDecision.SKIP_DUPLICATE),
        (OutboxState.SENDING, 1, ReviewState.PENDING, DispatchDecision.SKIP_IN_FLIGHT),
        (OutboxState.UNCERTAIN, 1, ReviewState.PENDING, DispatchDecision.HOLD_UNCERTAIN),
        (OutboxState.DEAD_LETTER, 1, ReviewState.PENDING, DispatchDecision.SKIP_FINAL),
        (OutboxState.CANCELLED, 1, ReviewState.PENDING, DispatchDecision.SKIP_FINAL),
        (OutboxState.BLOCKED, 1, ReviewState.PENDING, DispatchDecision.SKIP_FINAL),
        (None, 2, ReviewState.PENDING, DispatchDecision.SUPPRESS_STALE),  # out of order: newer version exists
        (None, 1, ReviewState.CLAIMED, DispatchDecision.SUPPRESS_STALE),
        (None, 1, ReviewState.SHORTLISTED, DispatchDecision.SUPPRESS_STALE),
        (None, 1, ReviewState.SUPERSEDED, DispatchDecision.SUPPRESS_STALE),
    ],
)
def test_decide_dispatch(
    existing: OutboxState | None, version: int, state: ReviewState, expected: DispatchDecision
) -> None:
    decision = eb.decide_dispatch(
        SIGNAL,
        target(),
        existing_state=existing,
        current_case_version=version,
        current_case_state=state,
        now=NOW,
    )
    assert decision is expected


def test_decide_dispatch_inactive_and_filter_mismatch() -> None:
    kwargs: dict[str, Any] = {
        "existing_state": None,
        "current_case_version": 1,
        "current_case_state": ReviewState.PENDING,
        "now": NOW,
    }
    revoked = target(status=SubscriptionStatus.REVOKED)
    assert eb.decide_dispatch(SIGNAL, revoked, **kwargs) is DispatchDecision.SKIP_INACTIVE
    other_profile = target(arguments={"profile": "manual_4000"})
    assert eb.decide_dispatch(SIGNAL, other_profile, **kwargs) is DispatchDecision.SKIP_FILTER_MISMATCH


# =========================================================================== delivery


class Callback:
    """Synthetic webhook receiver with a scripted list of responses or exceptions."""

    def __init__(self, *script: int | Exception | httpx.Response) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.script.pop(0) if self.script else 200
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(item)


async def run_deliver(
    receiver: Callback,
    *,
    occurrence: Mapping[str, Any] | None = None,
    tgt: DeliveryTarget | None = None,
    attempt: int = 1,
    clock: FrozenClock | None = None,
    access: Callable[[DeliveryTarget], Awaitable[bool]] = allow,
    policy: RetryPolicy | None = None,
    first_attempt_at: datetime | None = None,
) -> eb.DeliveryOutcome:
    async with http_with(receiver) as http:
        return await eb.deliver(
            occurrence if occurrence is not None else EXPECTED_OCCURRENCE,
            tgt or target(),
            http,
            attempt=attempt,
            clock=clock or FrozenClock(NOW),
            access_check=access,
            rng=random.Random(7),
            retry_policy=policy or RetryPolicy(),
            first_attempt_at=first_attempt_at,
        )


async def test_2xx_is_a_receipt_with_exact_wire_format() -> None:
    receiver = Callback(202)
    outcome = await run_deliver(receiver)
    assert outcome.kind is DeliveryOutcomeKind.DELIVERED
    assert outcome.provider_accepted_at == NOW
    assert outcome.webhook_id == EXPECTED_OCCURRENCE["eventId"]
    request = receiver.requests[0]
    assert request.content == canonical_json(EXPECTED_OCCURRENCE).encode()
    assert json.loads(request.content) == EXPECTED_OCCURRENCE
    assert request.headers[HEADER_ID] == EXPECTED_OCCURRENCE["eventId"]
    assert request.headers[HEADER_SUBSCRIPTION] == target().subscription_id
    assert request.headers[HEADER_SIGNATURE] == sign(
        SECRET, request.headers[HEADER_ID], NOW, request.content.decode()
    )
    assert request.headers["host"] == "receiver.example.com"


async def test_rotation_window_sends_two_signatures() -> None:
    receiver = Callback(200)
    await run_deliver(receiver, tgt=target(secrets=(SECRET_NEW, SECRET)))
    entries = receiver.requests[0].headers[HEADER_SIGNATURE].split(" ")
    assert len(entries) == 2


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (410, DeliveryFailureReason.HTTP_410_GONE),
        (413, DeliveryFailureReason.HTTP_413_TOO_LARGE),
        (400, DeliveryFailureReason.HTTP_4XX),
        (401, DeliveryFailureReason.HTTP_4XX),
        (403, DeliveryFailureReason.HTTP_4XX),
        (404, DeliveryFailureReason.HTTP_4XX),
        (422, DeliveryFailureReason.HTTP_4XX),
    ],
)
async def test_terminal_statuses_fail_this_delivery_only(status: int, reason: DeliveryFailureReason) -> None:
    receiver = Callback(status)
    outcome = await run_deliver(receiver)
    assert outcome.kind is DeliveryOutcomeKind.FAILED
    assert outcome.reason is reason
    assert outcome.wire_error is CallbackErrorReason.HTTP_4XX
    assert not outcome.revoke_subscription  # the subscription stays active
    assert outcome.next_attempt_at is None
    assert len(receiver.requests) == 1


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_3xx_is_refused_and_not_followed(status: int) -> None:
    receiver = Callback(httpx.Response(status, headers={"location": "https://elsewhere.example.org/"}))
    outcome = await run_deliver(receiver)
    assert outcome.kind is DeliveryOutcomeKind.FAILED
    assert outcome.reason is DeliveryFailureReason.HTTP_REDIRECT_REFUSED
    assert len(receiver.requests) == 1


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
async def test_transient_statuses_are_retried_with_backoff(status: int) -> None:
    outcome = await run_deliver(Callback(status))
    assert outcome.kind is DeliveryOutcomeKind.RETRY
    assert outcome.retryable
    assert outcome.next_attempt_at is not None
    delay = outcome.next_attempt_at - NOW
    assert timedelta(seconds=15) <= delay <= timedelta(seconds=30)


async def test_retry_after_seconds_is_respected() -> None:
    outcome = await run_deliver(Callback(httpx.Response(429, headers={"retry-after": "120"})))
    assert outcome.kind is DeliveryOutcomeKind.RETRY
    assert outcome.retry_after == timedelta(seconds=120)
    assert outcome.next_attempt_at == NOW + timedelta(seconds=120)


async def test_retry_after_http_date_is_respected() -> None:
    header = "Tue, 06 Oct 2026 10:03:00 GMT"
    outcome = await run_deliver(Callback(httpx.Response(503, headers={"retry-after": header})))
    assert outcome.retry_after == timedelta(minutes=3)
    assert outcome.next_attempt_at == NOW + timedelta(minutes=3)


async def test_retry_after_beyond_window_dead_letters() -> None:
    outcome = await run_deliver(Callback(httpx.Response(503, headers={"retry-after": "3600"})))
    assert outcome.kind is DeliveryOutcomeKind.DEAD_LETTER


async def test_attempts_exhausted_dead_letters() -> None:
    outcome = await run_deliver(Callback(500), attempt=5)
    assert outcome.kind is DeliveryOutcomeKind.DEAD_LETTER
    assert outcome.reason is DeliveryFailureReason.HTTP_5XX


async def test_total_retry_window_bounded() -> None:
    outcome = await run_deliver(
        Callback(500), attempt=2, first_attempt_at=NOW - timedelta(minutes=14, seconds=50)
    )
    assert outcome.kind is DeliveryOutcomeKind.DEAD_LETTER


async def test_retries_keep_event_id_and_regenerate_timestamp_and_signature() -> None:
    clock = FrozenClock(NOW)
    receiver = Callback(500, 200)
    first = await run_deliver(receiver, clock=clock, attempt=1)
    assert first.kind is DeliveryOutcomeKind.RETRY
    assert first.next_attempt_at is not None
    clock.advance(first.next_attempt_at - NOW)
    second = await run_deliver(receiver, clock=clock, attempt=2, first_attempt_at=NOW)
    assert second.kind is DeliveryOutcomeKind.DELIVERED
    one, two = receiver.requests
    assert one.headers[HEADER_ID] == two.headers[HEADER_ID] == EXPECTED_OCCURRENCE["eventId"]
    assert json.loads(one.content)["eventId"] == json.loads(two.content)["eventId"]
    assert one.content == two.content  # occurrence timestamp is the event time, not the send time
    assert one.headers[HEADER_TIMESTAMP] != two.headers[HEADER_TIMESTAMP]
    assert one.headers[HEADER_SIGNATURE] != two.headers[HEADER_SIGNATURE]


async def test_timeout_after_send_is_uncertain_not_blindly_retried() -> None:
    outcome = await run_deliver(Callback(httpx.ReadTimeout("no response")))
    assert outcome.kind is DeliveryOutcomeKind.UNCERTAIN
    assert outcome.reason is DeliveryFailureReason.TIMEOUT_AFTER_SEND
    assert outcome.wire_error is CallbackErrorReason.TIMEOUT
    assert outcome.next_attempt_at is None


async def test_connection_lost_after_send_is_uncertain() -> None:
    outcome = await run_deliver(Callback(httpx.ReadError("reset")))
    assert outcome.kind is DeliveryOutcomeKind.UNCERTAIN


class _RaisingHttp:
    """SafeHttp double that fails after the receiver's status line was already read."""

    def __init__(self, exc: SafeHttpError) -> None:
        self.exc = exc
        self.calls = 0

    async def post(
        self,
        url: str,
        *,
        content: bytes,
        headers: Mapping[str, str],
        timeout_s: float | None = None,
        max_response_bytes: int | None = None,
    ) -> SafeResponse:
        self.calls += 1
        raise self.exc

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        max_response_bytes: int | None = None,
    ) -> SafeResponse:  # pragma: no cover - not used by deliver()
        raise self.exc


@pytest.mark.parametrize(
    ("failure", "status", "kind", "reason"),
    [
        # Regression: these used to become UNCERTAIN, and the uncertain follow-up rule would
        # then re-send a delivery the receiver had explicitly refused with 410/413.
        (
            SafeHttpFailure.PROTOCOL_ERROR,
            410,
            DeliveryOutcomeKind.FAILED,
            DeliveryFailureReason.HTTP_410_GONE,
        ),
        (
            SafeHttpFailure.CONNECTION_LOST,
            413,
            DeliveryOutcomeKind.FAILED,
            DeliveryFailureReason.HTTP_413_TOO_LARGE,
        ),
        (SafeHttpFailure.TIMEOUT_AFTER_SEND, 400, DeliveryOutcomeKind.FAILED, DeliveryFailureReason.HTTP_4XX),
        (SafeHttpFailure.CONNECTION_LOST, 503, DeliveryOutcomeKind.RETRY, DeliveryFailureReason.HTTP_5XX),
        (
            SafeHttpFailure.TIMEOUT_AFTER_SEND,
            429,
            DeliveryOutcomeKind.RETRY,
            DeliveryFailureReason.HTTP_RETRYABLE_4XX,
        ),
        (SafeHttpFailure.TIMEOUT_AFTER_SEND, 200, DeliveryOutcomeKind.DELIVERED, None),
    ],
)
async def test_transport_failure_after_status_line_is_classified_by_status(
    failure: SafeHttpFailure, status: int, kind: DeliveryOutcomeKind, reason: DeliveryFailureReason | None
) -> None:
    http = _RaisingHttp(SafeHttpError(failure, possibly_delivered=True, status_code=status))
    outcome = await eb.deliver(
        EXPECTED_OCCURRENCE,
        target(),
        http,
        attempt=1,
        clock=FrozenClock(NOW),
        access_check=allow,
        rng=random.Random(1),
    )
    assert http.calls == 1
    assert outcome.kind is kind
    assert outcome.reason is reason
    assert outcome.status_code == status
    assert outcome.send_attempted_at == NOW
    if kind is DeliveryOutcomeKind.DELIVERED:
        assert outcome.provider_accepted_at == NOW


async def test_transport_failure_without_status_after_send_stays_uncertain() -> None:
    http = _RaisingHttp(SafeHttpError(SafeHttpFailure.CONNECTION_LOST, possibly_delivered=True))
    outcome = await eb.deliver(
        EXPECTED_OCCURRENCE, target(), http, attempt=1, clock=FrozenClock(NOW), access_check=allow
    )
    assert outcome.kind is DeliveryOutcomeKind.UNCERTAIN
    assert outcome.next_attempt_at is None


@pytest.mark.parametrize(
    ("exc", "reason", "wire"),
    [
        (httpx.ConnectTimeout("t"), DeliveryFailureReason.TIMEOUT, CallbackErrorReason.TIMEOUT),
        (
            httpx.ConnectError("refused"),
            DeliveryFailureReason.CONNECTION_REFUSED,
            CallbackErrorReason.CONNECTION_REFUSED,
        ),
        (
            httpx.ConnectError("[SSL] handshake"),
            DeliveryFailureReason.TLS_ERROR,
            CallbackErrorReason.TLS_ERROR,
        ),
    ],
)
async def test_failures_before_sending_are_retryable(
    exc: Exception, reason: DeliveryFailureReason, wire: CallbackErrorReason
) -> None:
    outcome = await run_deliver(Callback(exc))
    assert outcome.kind is DeliveryOutcomeKind.RETRY
    assert outcome.reason is reason
    assert outcome.wire_error is wire


@pytest.mark.parametrize(
    "status",
    [SubscriptionStatus.REVOKED, SubscriptionStatus.UNSUBSCRIBED, SubscriptionStatus.PENDING_VERIFICATION],
)
async def test_inactive_subscriptions_receive_nothing(status: SubscriptionStatus) -> None:
    receiver = Callback(200)
    outcome = await run_deliver(receiver, tgt=target(status=status))
    assert outcome.kind is DeliveryOutcomeKind.SKIPPED
    assert receiver.requests == []


async def test_expired_subscription_receives_nothing() -> None:
    receiver = Callback(200)
    outcome = await run_deliver(receiver, tgt=target(expires_at=NOW - timedelta(seconds=1)))
    assert outcome.block_reason is BlockReason.EXPIRED
    assert receiver.requests == []


async def test_membership_recheck_failure_stops_delivery_and_signals_revocation() -> None:
    receiver = Callback(200)
    outcome = await run_deliver(receiver, access=deny)
    assert outcome.kind is DeliveryOutcomeKind.SKIPPED
    assert outcome.reason is DeliveryFailureReason.ACCESS_REVOKED
    assert outcome.revoke_subscription
    assert receiver.requests == []


async def test_access_check_error_fails_closed() -> None:
    async def broken(_: DeliveryTarget) -> bool:
        raise RuntimeError("db down")

    receiver = Callback(200)
    outcome = await run_deliver(receiver, access=broken)
    assert outcome.kind is DeliveryOutcomeKind.RETRY
    assert outcome.reason is DeliveryFailureReason.ACCESS_CHECK_UNAVAILABLE
    assert receiver.requests == []


@pytest.mark.parametrize(
    "occurrence",
    [
        {**EXPECTED_OCCURRENCE, "type": "verification"},
        {**EXPECTED_OCCURRENCE, "name": "other.v1"},
        {**EXPECTED_OCCURRENCE, "eventId": "evt.with.dot"},
        {**EXPECTED_OCCURRENCE, "eventId": 5},
        {**EXPECTED_OCCURRENCE, "cursor": "c1"},
        {**EXPECTED_OCCURRENCE, "data": {**EXPECTED_OCCURRENCE["data"], "summary": "x"}},
        {k: v for k, v in EXPECTED_OCCURRENCE.items() if k != "timestamp"},
    ],
)
async def test_invalid_occurrences_are_never_sent(occurrence: dict[str, Any]) -> None:
    receiver = Callback(200)
    outcome = await run_deliver(receiver, occurrence=occurrence)
    assert outcome.kind is DeliveryOutcomeKind.FAILED
    assert outcome.reason is DeliveryFailureReason.INVALID_OCCURRENCE
    assert receiver.requests == []


async def test_oversized_occurrence_is_never_sent() -> None:
    huge = {**EXPECTED_OCCURRENCE, "data": {**EXPECTED_OCCURRENCE["data"], "readiness": "r" * 270_000}}
    receiver = Callback(200)
    outcome = await run_deliver(receiver, occurrence=huge)
    assert outcome.kind is DeliveryOutcomeKind.FAILED
    assert outcome.reason is DeliveryFailureReason.PAYLOAD_TOO_LARGE
    assert receiver.requests == []


async def test_attempt_must_be_positive() -> None:
    with pytest.raises(ValueError):
        await run_deliver(Callback(200), attempt=0)


# =========================================================================== backoff and Retry-After


def test_backoff_is_exponential_with_jitter_and_capped() -> None:
    policy = RetryPolicy()
    rng = random.Random(1)
    for attempt, (low, high) in {
        1: (15, 30),
        2: (30, 60),
        3: (60, 120),
        4: (120, 240),
        9: (150, 300),
    }.items():
        for _ in range(50):
            seconds = eb.backoff_delay(attempt, policy, rng).total_seconds()
            assert low <= seconds <= high


def test_retry_policy_validation() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay=timedelta(minutes=10), max_delay=timedelta(minutes=1))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", timedelta(0)),
        ("30", timedelta(seconds=30)),
        (" 45 ", timedelta(seconds=45)),
        ("Tue, 06 Oct 2026 10:01:00 GMT", timedelta(minutes=1)),
        ("Tue, 06 Oct 2026 09:00:00 GMT", timedelta(0)),
        ("-5", None),
        ("soon", None),
        ("", None),
        (None, None),
        ("9" * 70, None),
        (str(8 * 24 * 3600), None),
    ],
)
def test_parse_retry_after(value: str | None, expected: timedelta | None) -> None:
    assert eb.parse_retry_after(value, NOW) == expected


# =========================================================================== uncertain follow-up


def test_uncertain_followup_rule() -> None:
    assert (
        eb.decide_uncertain_followup(uncertain_since=NOW, resends_done=0, now=NOW) is UncertainDecision.HOLD
    )
    later = NOW + timedelta(minutes=2)
    assert (
        eb.decide_uncertain_followup(uncertain_since=NOW, resends_done=0, now=later)
        is UncertainDecision.RESEND_SAME_EVENT_ID
    )
    assert (
        eb.decide_uncertain_followup(uncertain_since=NOW, resends_done=1, now=later)
        is UncertainDecision.KEEP_UNCERTAIN
    )


# =========================================================================== activation route


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_default_route_is_unavailable() -> None:
    selection = eb.select_activation_route(settings())
    assert selection.route is ActivationRoute.NONE
    assert selection.bridge_status is BridgeStatus.UNAVAILABLE
    assert selection.blockers


@pytest.mark.parametrize(
    "overrides",
    [
        {"mcp_events_enabled": True, "notification_provider": "slack"},
        {"mcp_events_enabled": True, "event_bridge_provider": "slack"},
        {"notification_provider": "mcp_events", "event_bridge_provider": "slack"},
        {"event_bridge_enabled": True, "event_bridge_provider": "disabled"},
        {"event_bridge_provider": "mcp_events", "mcp_events_enabled": False},
    ],
)
def test_conflicting_routes_raise(overrides: dict[str, Any]) -> None:
    with pytest.raises(ActivationRouteConflict) as info:
        eb.select_activation_route(settings(**overrides))
    assert info.value.code is ErrorCode.VALIDATION_ERROR


SYNTHETIC_KEY = base64.b64encode(bytes(range(32))).decode()  # synthetic 32-byte encryption key


def test_native_route_configured_then_verified() -> None:
    base = {
        "allow_external_notifications": True,
        "event_bridge_enabled": True,
        "event_bridge_provider": "mcp_events",
        "mcp_events_enabled": True,
        "mcp_event_subscription_secret_encryption_key": SYNTHETIC_KEY,
    }
    clock = FrozenClock(NOW)
    configured = eb.select_activation_route(settings(**base), clock=clock)
    assert configured.route is ActivationRoute.MCP_EVENTS
    assert configured.bridge_status is BridgeStatus.CONFIGURED
    verified = eb.select_activation_route(
        settings(**base, event_bridge_verified_at="2026-10-06T09:00:00Z"), clock=clock
    )
    assert verified.bridge_status is BridgeStatus.VERIFIED
    assert verified.blockers == ()
    garbage = eb.select_activation_route(settings(**base, event_bridge_verified_at="yesterday"), clock=clock)
    assert garbage.bridge_status is BridgeStatus.CONFIGURED
    assert any("not an aware" in b for b in garbage.blockers)
    naive = eb.select_activation_route(
        settings(**base, event_bridge_verified_at="2026-10-06T09:00:00"), clock=clock
    )
    assert naive.bridge_status is BridgeStatus.CONFIGURED


def test_future_canary_timestamp_is_not_verification() -> None:
    selection = eb.select_activation_route(
        settings(
            allow_external_notifications=True,
            event_bridge_enabled=True,
            event_bridge_provider="mcp_events",
            mcp_events_enabled=True,
            mcp_event_subscription_secret_encryption_key=SYNTHETIC_KEY,
            event_bridge_verified_at="2099-01-01T00:00:00Z",
        ),
        clock=FrozenClock(NOW),
    )
    assert selection.bridge_status is BridgeStatus.CONFIGURED
    assert "event_bridge_verified_at is in the future" in selection.blockers


@pytest.mark.parametrize(
    ("key", "blocker"),
    [
        (None, "mcp_event_subscription_secret_encryption_key is not configured"),
        ("   ", "mcp_event_subscription_secret_encryption_key is not configured"),
        (base64.b64encode(b"short").decode(), "mcp_event_subscription_secret_encryption_key is invalid"),
    ],
)
def test_native_route_unavailable_without_valid_encryption_key(key: str | None, blocker: str) -> None:
    # Subscriptions cannot be stored without the at-rest key, so even a recorded canary
    # must not report the route as configured/verified.
    overrides: dict[str, Any] = {
        "allow_external_notifications": True,
        "event_bridge_enabled": True,
        "event_bridge_provider": "mcp_events",
        "mcp_events_enabled": True,
        "event_bridge_verified_at": "2026-10-06T09:00:00Z",
    }
    if key is not None:
        overrides["mcp_event_subscription_secret_encryption_key"] = key
    selection = eb.select_activation_route(settings(**overrides), clock=FrozenClock(NOW))
    assert selection.route is ActivationRoute.NONE
    assert selection.bridge_status is BridgeStatus.UNAVAILABLE
    assert selection.blockers == (blocker,)
    if key:
        assert key not in " ".join(selection.blockers)


def test_external_notifications_gate() -> None:
    selection = eb.select_activation_route(
        settings(event_bridge_enabled=True, event_bridge_provider="mcp_events", mcp_events_enabled=True)
    )
    assert selection.route is ActivationRoute.NONE
    assert "allow_external_notifications is false" in selection.blockers


SLACK_SETTINGS: dict[str, Any] = {
    "allow_external_notifications": True,
    "event_bridge_enabled": True,
    "event_bridge_provider": "slack",
    "notification_provider": "slack",
    "slack_bot_token": "xoxb-000000000-synthetic-test-token",
    "slack_signing_secret": "synthetic-signing-secret-0123456789",
    "slack_channel_id": "C0SYNTHETIC1",
}


def test_slack_route_selected_when_only_slack_enabled() -> None:
    selection = eb.select_activation_route(settings(**SLACK_SETTINGS), clock=FrozenClock(NOW))
    assert selection.route is ActivationRoute.SLACK
    assert selection.bridge_status is BridgeStatus.CONFIGURED


@pytest.mark.parametrize(
    ("drop", "blocker"),
    [
        ("slack_bot_token", "slack_bot_token is not configured"),
        ("slack_signing_secret", "slack_signing_secret is not configured"),
        ("slack_channel_id", "slack_channel_id is not configured"),
    ],
)
def test_slack_route_unavailable_without_credentials(drop: str, blocker: str) -> None:
    values = {k: v for k, v in SLACK_SETTINGS.items() if k != drop}
    selection = eb.select_activation_route(settings(**values), clock=FrozenClock(NOW))
    assert selection.route is ActivationRoute.NONE
    assert selection.bridge_status is BridgeStatus.UNAVAILABLE
    assert selection.blockers == (blocker,)


def test_slack_activation_route_requires_slack_notification_provider() -> None:
    values = {**SLACK_SETTINGS, "notification_provider": "disabled"}
    selection = eb.select_activation_route(settings(**values), clock=FrozenClock(NOW))
    assert selection.route is ActivationRoute.NONE
    assert "notification_provider is not slack" in selection.blockers


def test_rfc3339() -> None:
    assert eb.rfc3339(NOW) == "2026-10-06T10:00:00Z"
    assert eb.rfc3339(NOW.replace(microsecond=123456)) == "2026-10-06T10:00:00.123Z"
    with pytest.raises(ValueError):
        eb.rfc3339(datetime(2026, 1, 1))
