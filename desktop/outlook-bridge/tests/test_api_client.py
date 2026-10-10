"""Mailbox-worker API client: typed failures, transport rules and client-side guards (spec 37.8)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from bridge_support import MAILBOX_ID, OTHER_MAILBOX_ID, START, TOKEN
from outlook_bridge.api_client import (
    BINDINGS_PATH,
    MAX_RESPONSE_BYTES,
    REPLIES_PATH,
    ApiErrorKind,
    BridgeApiClient,
    BridgeApiError,
    ClientIdentity,
)
from outlook_bridge.credentials import credential_fingerprint
from outlook_bridge.errors import CredentialUnusable, MailboxMismatch
from outlook_bridge.testing import make_intent

Handler = Callable[[httpx.Request], httpx.Response]
#: A synthetic local-store instance id (``LocalStore.store_instance_id``).
STORE_INSTANCE = "s00000000000000a1"


def _client(
    handler: Handler, token: Callable[[], str] = lambda: TOKEN
) -> tuple[BridgeApiClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = BridgeApiClient(
        "https://api.example.invalid",
        token_provider=token,
        identity=ClientIdentity(MAILBOX_ID, "desktop-test-1", STORE_INSTANCE),
        transport=httpx.MockTransport(record),
    )
    return client, seen


def _error(status: int, code: str = "X", **extra: Any) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"schema_version": "1.0", "request_id": "req-1", "error": {"code": code, **extra}}
        headers = {"Retry-After": str(extra["retry_after_seconds"])} if "retry_after_seconds" in extra else {}
        return httpx.Response(status, json=body, headers=headers)

    return handler


def _reply_body(inquiry: UUID, mailbox: UUID = MAILBOX_ID) -> bytes:
    return json.dumps(
        {"schema_version": "1.0", "inquiry_id": str(inquiry), "mailbox_binding_id": str(mailbox)}
    ).encode()


def _ack(inquiry: UUID, **overrides: Any) -> dict[str, Any]:
    body = {
        "schema_version": "1.0",
        "reply_id": str(uuid4()),
        "inquiry_id": str(inquiry),
        "ingest_status": "stored",
        "duplicate": False,
        "request_id": "req-1",
        "ingested_at": START.isoformat(),
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    ("status", "code", "kind", "transient"),
    [
        (400, "VALIDATION_ERROR", ApiErrorKind.VALIDATION, False),
        (422, "VALIDATION_ERROR", ApiErrorKind.VALIDATION, False),
        (401, "UNAUTHENTICATED", ApiErrorKind.UNAUTHENTICATED, False),
        (403, "FORBIDDEN", ApiErrorKind.FORBIDDEN, False),
        (404, "NOT_FOUND", ApiErrorKind.NOT_FOUND, False),
        (409, "IDEMPOTENCY_CONFLICT", ApiErrorKind.IDEMPOTENCY_CONFLICT, False),
        (409, "VERSION_CONFLICT", ApiErrorKind.CONFLICT, False),
        (413, "TOO_LARGE", ApiErrorKind.REQUEST_TOO_LARGE, False),
        (429, "RATE_LIMITED", ApiErrorKind.RATE_LIMITED, True),
        (500, "INTERNAL", ApiErrorKind.UNAVAILABLE, True),
        (503, "UNAVAILABLE", ApiErrorKind.UNAVAILABLE, True),
        (418, "TEAPOT", ApiErrorKind.PROTOCOL, True),
    ],
)
def test_typed_failures(status: int, code: str, kind: ApiErrorKind, transient: bool) -> None:
    client, _ = _client(_error(status, code))
    with pytest.raises(BridgeApiError) as info:
        client.fetch_binding_page(None, limit=100)
    assert info.value.kind == kind
    assert info.value.status == status
    assert info.value.transient is transient
    assert info.value.code == code
    assert info.value.credential_fingerprint == credential_fingerprint(TOKEN)
    assert TOKEN not in str(info.value) and TOKEN not in repr(info.value)


def test_retry_after_is_honoured_and_bounded() -> None:
    client, _ = _client(_error(429, "RATE_LIMITED", retry_after_seconds=120))
    with pytest.raises(BridgeApiError) as info:
        client.fetch_binding_page(None, limit=10)
    assert info.value.retry_after_seconds == 120
    client, _ = _client(_error(429, "RATE_LIMITED", retry_after_seconds=10**9))
    with pytest.raises(BridgeApiError) as info:
        client.fetch_binding_page(None, limit=10)
    assert info.value.retry_after_seconds == 3600


def test_transport_failures_and_timeouts_are_transient() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    for handler in (refuse, slow):
        client, _ = _client(handler)
        with pytest.raises(BridgeApiError) as info:
            client.fetch_binding_page(None, limit=10)
        assert info.value.kind == ApiErrorKind.TRANSPORT and info.value.transient


def test_redirects_are_never_followed() -> None:
    client, seen = _client(
        lambda r: httpx.Response(302, headers={"Location": "https://evil.example.invalid/x"})
    )
    with pytest.raises(BridgeApiError) as info:
        client.fetch_binding_page(None, limit=10)
    assert info.value.kind == ApiErrorKind.PROTOCOL
    assert [str(r.url.host) for r in seen] == ["api.example.invalid"]  # the bearer token never travelled


def test_oversized_and_non_json_responses_are_protocol_errors() -> None:
    client, _ = _client(lambda r: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 10)))
    with pytest.raises(BridgeApiError) as info:
        client.fetch_binding_page(None, limit=10)
    assert info.value.kind == ApiErrorKind.PROTOCOL
    client, _ = _client(lambda r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(BridgeApiError):
        client.fetch_binding_page(None, limit=10)
    client, _ = _client(lambda r: httpx.Response(200, json=[1, 2]))
    with pytest.raises(BridgeApiError):
        client.fetch_binding_page(None, limit=10)
    client, _ = _client(lambda r: httpx.Response(200, json={"schema_version": "2.0", "items": []}))
    with pytest.raises(BridgeApiError):
        client.fetch_binding_page(None, limit=10)


def test_binding_page_request_shape_and_validation() -> None:
    inquiry = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == BINDINGS_PATH
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.params["cursor"] == "opaque-1"
        assert request.url.params["limit"] == "100"
        assert "workspace" not in str(request.url)  # the worker never selects a workspace or mailbox
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "items": [
                    {
                        "inquiry_id": str(inquiry),
                        "binding_version": 2,
                        "mailbox_binding_id": str(MAILBOX_ID),
                        "state": "tombstoned",
                    },
                ],
                "next_cursor": "opaque-2",
                "has_more": False,
            },
        )

    client, _ = _client(handler)
    page = client.fetch_binding_page("opaque-1", limit=500)
    assert page.next_cursor == "opaque-2"
    assert page.items[0].to_record().binding is None


@pytest.mark.parametrize(
    "item",
    [
        {
            "state": "tombstoned",
            "verified_seller_aliases": ["seller@dealer.example.invalid"],
        },  # payload on tombstone
        {"state": "active"},  # no provider
        {"state": "active", "provider": "outlook_local", "outbound_message_ids": ["not-a-message-id"]},
        {
            "state": "active",
            "provider": "outlook_local",
            "verified_seller_aliases": ["two@a.example.invalid, b@c"],
        },
        {"state": "active", "provider": "outlook_local", "workspace_id": str(uuid4())},  # unknown field
    ],
)
def test_invalid_binding_items_are_protocol_errors(item: dict[str, Any]) -> None:
    payload = {
        "inquiry_id": str(uuid4()),
        "binding_version": 1,
        "mailbox_binding_id": str(MAILBOX_ID),
        **item,
    }
    client, _ = _client(lambda r: httpx.Response(200, json={"schema_version": "1.0", "items": [payload]}))
    with pytest.raises(BridgeApiError) as info:
        client.fetch_binding_page(None, limit=10)
    assert info.value.kind == ApiErrorKind.PROTOCOL


def test_reply_upload_carries_idempotency_key_and_checks_the_acknowledgement() -> None:
    inquiry = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == REPLIES_PATH
        assert request.headers["Idempotency-Key"] == "mwr1-abcdef0123"
        assert request.headers["Content-Type"] == "application/json"
        return httpx.Response(200, json=_ack(inquiry, duplicate=True))

    client, _ = _client(handler)
    ack = client.post_reply(
        _reply_body(inquiry), idempotency_key="mwr1-abcdef0123", expected_inquiry_id=inquiry
    )
    assert ack.duplicate is True and ack.ingest_status == "stored"
    client, _ = _client(lambda r: httpx.Response(200, json=_ack(uuid4())))
    with pytest.raises(BridgeApiError) as info:
        client.post_reply(
            _reply_body(inquiry), idempotency_key="mwr1-abcdef0123", expected_inquiry_id=inquiry
        )
    assert info.value.kind == ApiErrorKind.PROTOCOL  # an acknowledgement for another inquiry is not ours


def test_reply_upload_guards_run_before_any_request() -> None:
    inquiry = uuid4()
    client, seen = _client(lambda r: httpx.Response(200, json=_ack(inquiry)))
    with pytest.raises(MailboxMismatch):  # cross-mailbox injection refused client-side
        client.post_reply(
            _reply_body(inquiry, OTHER_MAILBOX_ID),
            idempotency_key="mwr1-abcdef0123",
            expected_inquiry_id=inquiry,
        )
    with pytest.raises(MailboxMismatch):
        client.post_reply(
            _reply_body(uuid4()), idempotency_key="mwr1-abcdef0123", expected_inquiry_id=inquiry
        )
    with pytest.raises(BridgeApiError) as info:
        client.post_reply(
            b"x" * (128 * 1024 + 1), idempotency_key="mwr1-abcdef0123", expected_inquiry_id=inquiry
        )
    assert info.value.kind == ApiErrorKind.REQUEST_TOO_LARGE
    with pytest.raises(BridgeApiError):
        client.post_reply(_reply_body(inquiry), idempotency_key="short", expected_inquiry_id=inquiry)
    with pytest.raises(BridgeApiError):
        client.post_reply(b"not json", idempotency_key="mwr1-abcdef0123", expected_inquiry_id=inquiry)
    with pytest.raises(BridgeApiError):
        client.post_reply(
            b'{"schema_version":"2.0"}', idempotency_key="mwr1-abcdef0123", expected_inquiry_id=inquiry
        )
    assert seen == []


def test_unusable_credential_stops_before_the_network() -> None:
    def refuse() -> str:
        raise CredentialUnusable("expired")

    client, seen = _client(lambda r: httpx.Response(200, json={}), token=refuse)
    with pytest.raises(CredentialUnusable):
        client.fetch_binding_page(None, limit=10)
    assert seen == []


def test_send_intents_for_another_mailbox_are_refused() -> None:
    intent = make_intent(
        inquiry_id=uuid4(),
        mailbox_binding_id=OTHER_MAILBOX_ID,
        from_address="owner@example.invalid",
        created_at=START,
    )
    body = {
        "schema_version": "1.0",
        "intents": [json.loads(intent.model_dump_json())],
        "kill_switch_active": False,
    }
    client, _ = _client(lambda r: httpx.Response(200, json=body))
    with pytest.raises(BridgeApiError) as info:
        client.fetch_send_intents()
    assert info.value.kind == ApiErrorKind.PROTOCOL


def test_claim_answer_must_name_the_claimed_intent() -> None:
    intent_id = uuid4()
    client, seen = _client(
        lambda r: httpx.Response(
            200, json={"schema_version": "1.0", "intent_id": str(uuid4()), "proceed": True}
        )
    )
    with pytest.raises(BridgeApiError):
        client.claim_send_intent(intent_id)
    sent = json.loads(seen[0].content)
    attempt = sent.pop("claim_attempt_id")
    assert sent == {
        "schema_version": "1.0",
        "intent_id": str(intent_id),
        "mailbox_binding_id": str(MAILBOX_ID),
        # The claim names this local store as well (SEC-1, wave D2).
        "worker_id": f"desktop-test-1.{STORE_INSTANCE}",
    }
    assert seen[0].headers["Idempotency-Key"] == f"claim-{intent_id}-{attempt}"


def test_every_claim_is_a_fresh_revalidation_never_an_idempotent_replay() -> None:
    """A lost ``proceed=true`` must never be replayed after the kill switch/suppression changed."""
    intent_id = uuid4()
    client, seen = _client(
        lambda r: httpx.Response(
            200, json={"schema_version": "1.0", "intent_id": str(intent_id), "proceed": True}
        )
    )
    client.claim_send_intent(intent_id)
    client.claim_send_intent(intent_id)
    keys = [request.headers["Idempotency-Key"] for request in seen]
    attempts = [json.loads(request.content)["claim_attempt_id"] for request in seen]
    assert len(set(keys)) == 2 and len(set(attempts)) == 2
    assert all(len(key) <= 128 and key.startswith(f"claim-{intent_id}-") for key in keys)
