"""SSRF for MCP Events callbacks: subscribe-time URL policy and connection-time DNS/IP checks.

Covers private IPs, DNS rebinding with mixed answers, rebinding between
verification and delivery, IPv6 forms, cloud metadata, redirects and http://.
Destinations are SYNTHETIC; nothing here touches the network.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest

from suv_deals.clock import FrozenClock
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.integrations import event_bridge as eb
from suv_deals.integrations.event_bridge import (
    CallbackErrorReason,
    DeliveryFailureReason,
    DeliveryOutcomeKind,
    DeliveryTarget,
    EventsProtocolError,
    JsonRpcCode,
    SubscriptionStatus,
)
from suv_deals.integrations.safe_http import SafeHttpClient, SafeHttpError, SafeHttpFailure
from suv_deals.integrations.webhook_signing import parse_whsec

NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)
WS = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PRINCIPAL = UUID("99999999-9999-4999-8999-999999999999")
SECRET_TEXT = "whsec_" + base64.b64encode(b"s" * 32).decode()
SECRET = parse_whsec(SECRET_TEXT)
CALLBACK = "https://receiver.example.com/mcp-events/cb"
PUBLIC = "93.184.216.34"

ACTOR = ActorContext(
    workspace_id=WS,
    principal_id=PRINCIPAL,
    principal_kind="mcp_client",
    role=Role.REVIEWER,
    scopes=frozenset({Scope.REVIEWS_READ, Scope.EVENTS_SUBSCRIBE}),
    request_id="req-ssrf",
)

BLOCKED_CALLBACK_URLS = [
    "http://receiver.example.com/cb",  # https only
    "ftp://receiver.example.com/cb",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "https://127.0.0.1/cb",
    "https://127.1/cb",
    "https://2130706433/cb",
    "https://0x7f000001/cb",
    "https://0177.0.0.1/cb",
    "https://localhost/cb",
    "https://LOCALHOST./cb",
    "https://10.0.0.5/cb",
    "https://172.16.0.1/cb",
    "https://192.168.1.10/cb",
    "https://100.64.0.1/cb",
    "https://169.254.169.254/latest/meta-data/",
    "https://metadata.google.internal/computeMetadata/v1/",
    "https://instance-data/latest/",
    "https://[::1]/cb",
    "https://[::ffff:127.0.0.1]/cb",
    "https://[::ffff:a9fe:a9fe]/cb",  # IPv4-mapped 169.254.169.254
    "https://[fd00:ec2::254]/cb",  # AWS IMDS over IPv6
    "https://[fe80::1]/cb",
    "https://[fc00::1]/cb",
    "https://[64:ff9b::a00:1]/cb",  # NAT64 of 10.0.0.1
    "https://[2002:a00:1::]/cb",  # 6to4 of 10.0.0.1
    "https://user:pass@receiver.example.com/cb",
    "https://receiver.example.com@10.0.0.1/cb",
    "https://receiver.example.com:8443/cb",  # callbacks use 443 only
    "https://receiver.example.com:80/cb",
    "https://receiver.example.com/cb#fragment",
    "https://intranet/cb",
    "https://svc.internal/cb",
    "https://printer.local/cb",
    "https://receiver.example.com/cb\r\nX-Injected: 1",
    "https://" + "a" * 2050 + ".example.com/",
    "",
]


@pytest.mark.parametrize("url", BLOCKED_CALLBACK_URLS)
def test_subscribe_rejects_unsafe_callback_urls(url: str) -> None:
    params = {
        "name": eb.EVENT_NAME,
        "arguments": {"profile": "primary"},
        "delivery": {"mode": "webhook", "url": url, "secret": SECRET_TEXT},
    }
    with pytest.raises(EventsProtocolError) as info:
        eb.validate_subscribe_params(params, ACTOR, clock=FrozenClock(NOW))
    assert info.value.rpc_code is JsonRpcCode.INVALID_PARAMS
    assert url not in json.dumps(info.value.to_jsonrpc_error()) or not url


def test_public_https_callback_accepted() -> None:
    params = {
        "name": eb.EVENT_NAME,
        "arguments": {"profile": "primary"},
        "delivery": {"mode": "webhook", "url": CALLBACK, "secret": SECRET_TEXT},
    }
    assert eb.validate_subscribe_params(params, ACTOR, clock=FrozenClock(NOW)).callback_url == CALLBACK


# --------------------------------------------------------------------------- connection-time DNS


class Recorder:
    def __init__(self, status: int = 200, echo: bool = True, location: str | None = None) -> None:
        self.status = status
        self.echo = echo
        self.location = location
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {"location": self.location} if self.location else {}
        if self.echo and request.content:
            body = json.loads(request.content)
            if "challenge" in body:
                return httpx.Response(self.status, json={"challenge": body["challenge"]}, headers=headers)
        return httpx.Response(self.status, headers=headers)


def resolver(*answers: str) -> Callable[[str, int], Awaitable[list[str]]]:
    async def resolve(host: str, port: int) -> list[str]:
        return list(answers)

    return resolve


def http(recorder: Recorder, resolve: Callable[[str, int], Awaitable[list[str]]]) -> SafeHttpClient:
    return SafeHttpClient(resolver=resolve, transport=httpx.MockTransport(recorder))


PRIVATE_ANSWERS = [
    ["10.0.0.1"],
    ["127.0.0.1"],
    ["169.254.169.254"],
    ["100.100.100.200"],  # Alibaba metadata sits in CGNAT space
    ["0.0.0.0"],
    ["::1"],
    ["fd00:ec2::254"],
    ["fe80::1%eth0"],
    ["::ffff:10.0.0.1"],
    ["64:ff9b::7f00:1"],
    [PUBLIC, "10.0.0.1"],  # mixed answers: rebinding attempt
    ["10.0.0.1", PUBLIC],
    [PUBLIC, "::1"],
    ["not-an-ip"],
    [],
]


@pytest.mark.parametrize("answers", PRIVATE_ANSWERS)
async def test_verification_refuses_private_dns_answers(answers: list[str]) -> None:
    recorder = Recorder()
    async with http(recorder, resolver(*answers)) as client:
        result = await eb.run_verification_challenge(
            CALLBACK, SECRET, "sub_ssrf", client, clock=FrozenClock(NOW)
        )
    assert not result.ok
    assert result.reason is CallbackErrorReason.CONNECTION_REFUSED
    assert result.detail == SafeHttpFailure.DESTINATION_REJECTED.value
    assert recorder.requests == []


async def test_dns_failure_is_refused() -> None:
    async def failing(host: str, port: int) -> list[str]:
        raise OSError("NXDOMAIN")

    recorder = Recorder()
    async with http(recorder, failing) as client:
        with pytest.raises(SafeHttpError) as info:
            await client.post(CALLBACK, content=b"{}", headers={})
    assert info.value.failure is SafeHttpFailure.DESTINATION_REJECTED
    assert recorder.requests == []


def _target(url: str = CALLBACK) -> DeliveryTarget:
    return DeliveryTarget(
        subscription_id="sub_ssrf",
        workspace_id=WS,
        principal_id=PRINCIPAL,
        event_name=eb.EVENT_NAME,
        arguments={"profile": "primary"},
        callback_url=url,
        secrets=(SECRET,),
        status=SubscriptionStatus.ACTIVE,
        expires_at=NOW + timedelta(hours=1),
        verified_at=NOW,
    )


OCCURRENCE: dict[str, Any] = {
    "eventId": "55555555-5555-4555-8555-555555555555",
    "name": eb.EVENT_NAME,
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


async def _allow(_: DeliveryTarget) -> bool:
    return True


async def test_rebinding_between_verification_and_delivery_is_caught_at_connection_time() -> None:
    answers = [[PUBLIC], ["10.0.0.7"]]

    async def rebinding(host: str, port: int) -> list[str]:
        return answers.pop(0)

    recorder = Recorder()
    async with http(recorder, rebinding) as client:
        verified = await eb.run_verification_challenge(
            CALLBACK, SECRET, "sub_ssrf", client, clock=FrozenClock(NOW)
        )
        assert verified.ok
        outcome = await eb.deliver(
            OCCURRENCE, _target(), client, attempt=1, clock=FrozenClock(NOW), access_check=_allow
        )
    assert outcome.kind is DeliveryOutcomeKind.FAILED
    assert outcome.reason is DeliveryFailureReason.DESTINATION_REJECTED
    assert len(recorder.requests) == 1  # only the verification reached the (public) receiver


@pytest.mark.parametrize("url", ["http://receiver.example.com/cb", "https://10.0.0.1/cb", "https://[::1]/cb"])
async def test_delivery_to_stored_unsafe_url_is_refused(url: str) -> None:
    recorder = Recorder()
    async with http(recorder, resolver(PUBLIC)) as client:
        outcome = await eb.deliver(
            OCCURRENCE, _target(url), client, attempt=1, clock=FrozenClock(NOW), access_check=_allow
        )
    assert outcome.kind is DeliveryOutcomeKind.FAILED
    assert outcome.reason is DeliveryFailureReason.DESTINATION_REJECTED
    assert recorder.requests == []


@pytest.mark.parametrize(
    "location",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "https://10.0.0.1/admin",
        "https://receiver.example.com/other",
    ],
)
async def test_redirects_are_never_followed(location: str) -> None:
    recorder = Recorder(status=307, echo=False, location=location)
    async with http(recorder, resolver(PUBLIC)) as client:
        verification = await eb.run_verification_challenge(
            CALLBACK, SECRET, "sub_ssrf", client, clock=FrozenClock(NOW)
        )
        outcome = await eb.deliver(
            OCCURRENCE, _target(), client, attempt=1, clock=FrozenClock(NOW), access_check=_allow
        )
    assert verification.reason is CallbackErrorReason.CHALLENGE_FAILED
    assert outcome.kind is DeliveryOutcomeKind.FAILED
    assert outcome.reason is DeliveryFailureReason.HTTP_REDIRECT_REFUSED
    assert len(recorder.requests) == 2
    assert all(r.url.host == PUBLIC for r in recorder.requests)


async def test_connection_goes_to_validated_ip_with_original_host() -> None:
    recorder = Recorder()
    async with http(recorder, resolver(PUBLIC)) as client:
        outcome = await eb.deliver(
            OCCURRENCE, _target(), client, attempt=1, clock=FrozenClock(NOW), access_check=_allow
        )
    assert outcome.kind is DeliveryOutcomeKind.DELIVERED
    request = recorder.requests[0]
    assert request.url.host == PUBLIC
    assert request.headers["host"] == "receiver.example.com"
    assert request.extensions["sni_hostname"] == "receiver.example.com"


async def test_safe_client_rejects_http_and_odd_ports_for_callbacks() -> None:
    recorder = Recorder()
    async with http(recorder, resolver(PUBLIC)) as client:
        for url in ("http://receiver.example.com/cb", "https://receiver.example.com:8443/cb"):
            with pytest.raises(SafeHttpError) as info:
                await client.post(url, content=b"{}", headers={})
            assert info.value.failure is SafeHttpFailure.DESTINATION_REJECTED
    assert recorder.requests == []
