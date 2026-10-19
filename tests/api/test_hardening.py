"""API/MCP hardening of work package B2b (wave-3 review leftovers).

- `PreAuthLimiter`: a cheap per-client budget of FAILED authentications, checked before any
  signature check, key fetch or database lookup (``/api``, ``/mcp``, ``/v1/mail-workers``);
- opaque credentials are refused by format, CPU-only, before any database lookup;
- `ThrottledJwksClient`: the failure backoff, and the unknown-``kid`` refresh cooldown also after
  a FAILED fetch (PyJWT starts it only after a successful one);
- ``/readyz`` public details are generic (the specific reason only goes to the server log);
- validation errors name nested fields exactly (``validation_error_fields(model=...)``).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import pytest
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError
from tests.api.conftest import (
    UNREACHABLE_DB,
    ApiHarness,
    SigningKeys,
    TokenFactory,
    build_test_app,
    error_of,
    running_client,
)

from suv_deals.api.auth import ThrottledJwksClient
from suv_deals.api.middleware import DEFAULT_PREAUTH_LIMIT, PreAuthLimiter, RateLimit
from suv_deals.api.routes import GENERIC_READINESS_DETAIL
from suv_deals.errors import RateLimited
from suv_deals.persistence.database import Database
from suv_deals.settings import Settings

# ---------------------------------------------------------------------------------- PreAuthLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_preauth_limiter_refuses_only_after_the_failure_budget_is_spent() -> None:
    clock = Clock()
    limiter = PreAuthLimiter(limit=RateLimit(capacity=3, per_seconds=10.0), monotonic=clock)
    for _ in range(3):
        limiter.check("198.51.100.7")  # successful authentications cost nothing
    for _ in range(3):
        limiter.check("198.51.100.7")
        limiter.failed("198.51.100.7")
    with pytest.raises(RateLimited) as refused:
        limiter.check("198.51.100.7")
    assert refused.value.retry_after_seconds == 10
    limiter.check("203.0.113.9")  # another client is unaffected
    clock.now += 10.0
    limiter.check("198.51.100.7")  # one failure refilled
    clock.now += 30.0
    limiter.check("198.51.100.7")
    assert limiter.tracked_clients() == 0  # fully refilled clients are forgotten


def test_preauth_limiter_is_bounded_in_tracked_clients() -> None:
    limiter = PreAuthLimiter(limit=RateLimit(capacity=1, per_seconds=60.0), max_clients=10)
    for n in range(50):
        limiter.failed(f"192.0.2.{n}")
    assert limiter.tracked_clients() == 10
    limiter.check("192.0.2.0")  # the oldest entries were evicted (bounded memory)
    with pytest.raises(RateLimited):
        limiter.check("192.0.2.49")
    with pytest.raises(ValueError):
        PreAuthLimiter(max_clients=0)


def test_preauth_limiter_groups_ipv6_clients_by_their_64_network() -> None:
    """One IPv6 /64 is one client: a host rotating its interface identifier (privacy addresses,
    or any of the 2**64 addresses a single subscriber gets) must not get a fresh budget each time.
    An IPv4-mapped IPv6 address is the IPv4 client itself."""
    limiter = PreAuthLimiter(limit=RateLimit(capacity=2, per_seconds=60.0))
    limiter.failed("2001:db8:1:2::1")
    limiter.failed("2001:db8:1:2:ffff:ffff:ffff:9")  # same /64, another address
    with pytest.raises(RateLimited):
        limiter.check("2001:db8:1:2:abcd::77")
    with pytest.raises(RateLimited):
        limiter.check("2001:DB8:1:2::1%eth0")  # zone id and case do not escape the bucket
    limiter.check("2001:db8:1:3::1")  # the neighbouring /64 is another client
    limiter.failed("::ffff:198.51.100.7")
    limiter.failed("198.51.100.7")
    with pytest.raises(RateLimited):
        limiter.check("198.51.100.7")
    with pytest.raises(RateLimited):
        limiter.check("::ffff:198.51.100.7")
    limiter.check("198.51.100.8")  # IPv4 clients stay per address
    limiter.failed("testclient")  # a non-IP client label is kept as it is
    assert limiter.tracked_clients() == 3


@pytest.mark.db
async def test_dashboard_requests_are_refused_before_verification_after_failed_tokens(
    bare_api: ApiHarness, tokens: TokenFactory, keys: SigningKeys
) -> None:
    for _ in range(DEFAULT_PREAUTH_LIMIT.capacity):
        bad = await bare_api.client.get(
            "/api/me",
            headers={
                "Authorization": f"Bearer {tokens.mint(bare_api.users.owner, key=keys.stranger_private)}"
            },
        )
        assert bad.status_code == 401
    good = await bare_api.get("/api/me", bare_api.users.owner)
    assert good.status_code == 429  # even a valid token waits: no verification while flooding
    assert error_of(good)["code"] == "RATE_LIMITED" and int(good.headers["retry-after"]) >= 1


@pytest.mark.db
async def test_membership_denials_do_not_spend_the_failed_authentication_budget(bare_api: ApiHarness) -> None:
    for _ in range(DEFAULT_PREAUTH_LIMIT.capacity + 5):
        response = await bare_api.get("/api/me", bare_api.users.stranger)
        assert response.status_code == 403  # a valid token without membership is not an auth failure
    assert (await bare_api.get("/api/me", bare_api.users.owner)).status_code == 200


async def test_opaque_tokens_on_the_dashboard_are_refused_without_a_database(
    settings: Settings, keys: SigningKeys
) -> None:
    db = Database(UNREACHABLE_DB, set_role="suv_backend", min_size=0, max_size=1, pool_timeout_s=0.5)
    app = build_test_app(settings, keys, db)
    async with running_client(app) as client:
        for token in ("suvmail_" + "a" * 64, "suvmcp_" + "b" * 64, "not-a-token"):
            response = await client.get("/api/overview", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401


# ---------------------------------------------------------------------------------- JWKS throttle


def _jwks(keys: SigningKeys) -> dict[str, Any]:
    return json.loads(json.dumps(keys.jwks))


class _Endpoint:
    """Stands in for the network fetch of ``PyJWKClient.fetch_data`` (no network)."""

    def __init__(self, keys: SigningKeys) -> None:
        self.calls = 0
        self.fail = False
        self.keys = keys

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        endpoint = self

        def fetch_data(client: PyJWKClient) -> Any:
            return endpoint.fetch(client)

        monkeypatch.setattr(PyJWKClient, "fetch_data", fetch_data)

    def fetch(self, client: PyJWKClient) -> Any:
        self.calls += 1
        if self.fail:
            raise PyJWKClientConnectionError("synthetic outage")
        data = _jwks(self.keys)
        if client.jwk_set_cache is not None:
            client.jwk_set_cache.put(data)
        client._last_successful_fetch = time.monotonic()
        return data


def test_jwks_failure_backoff_fails_fast(monkeypatch: pytest.MonkeyPatch, keys: SigningKeys) -> None:
    endpoint = _Endpoint(keys)
    endpoint.install(monkeypatch)
    clock = Clock()
    client = ThrottledJwksClient(
        "https://synthetic-project.supabase.example/auth/v1/.well-known/jwks.json",
        cache_jwk_set=True,
        lifespan=600,
        cooldown_duration=30,
        failure_backoff=5.0,
        monotonic=clock,
    )
    endpoint.fail = True
    with pytest.raises(PyJWKClientConnectionError):
        client.get_signing_keys()
    with pytest.raises(PyJWKClientConnectionError):
        client.get_signing_keys()  # within the backoff: no network attempt
    assert endpoint.calls == 1
    clock.now += 6.0
    endpoint.fail = False
    assert client.get_signing_keys()
    assert endpoint.calls == 2


def test_unknown_kid_refreshes_are_throttled_after_a_failed_fetch(
    monkeypatch: pytest.MonkeyPatch, keys: SigningKeys
) -> None:
    endpoint = _Endpoint(keys)
    endpoint.install(monkeypatch)
    client = ThrottledJwksClient(
        "https://synthetic-project.supabase.example/auth/v1/.well-known/jwks.json",
        cache_jwk_set=True,
        lifespan=600,
        cooldown_duration=30,
        failure_backoff=0.0,  # isolate the cooldown from the failure backoff
    )
    assert client.get_signing_key("synthetic-es256-key-1")
    assert endpoint.calls == 1
    client._last_successful_fetch = time.monotonic() - 100  # the success cooldown has elapsed
    endpoint.fail = True
    with pytest.raises(PyJWKClientConnectionError):
        client.get_signing_key(f"random-{uuid.uuid4().hex}")  # one refresh attempt, failed
    assert endpoint.calls == 2
    for _ in range(5):  # random kids during the cooldown: invalid token, no network attempt
        with pytest.raises(PyJWKClientError):
            client.get_signing_key(f"random-{uuid.uuid4().hex}")
    assert endpoint.calls == 2
    assert client.get_signing_key("synthetic-es256-key-1")  # known keys keep verifying (cached)


# ---------------------------------------------------------------------------------- readiness


async def test_readyz_public_details_are_generic_and_the_reason_is_logged(
    settings: Settings, keys: SigningKeys, caplog: pytest.LogCaptureFixture
) -> None:
    db = Database(UNREACHABLE_DB, set_role="suv_backend", min_size=0, max_size=1, pool_timeout_s=0.5)
    app = build_test_app(settings, keys, db)
    caplog.set_level(logging.WARNING, logger="suv_deals.api")
    async with running_client(app) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    failing = [c for c in body["checks"] if c["status"] != "ok"]
    assert failing and all(c["detail"] == GENERIC_READINESS_DETAIL for c in failing)
    assert "127.0.0.1" not in response.text and "synthetic_unreachable" not in response.text
    assert any(getattr(r, "check", None) == "database" for r in caplog.records)


# ---------------------------------------------------------------------------------- validation fields


@pytest.mark.db
async def test_nested_body_validation_errors_name_the_exact_field(bare_api: ApiHarness) -> None:
    response = await bare_api.post(
        "/api/inquiry-control/pause",
        bare_api.users.owner,
        {"expected_version": "1", "reason": "Synthetic pause", "idempotency_key": "pause-fields-01"},
    )
    assert response.status_code == 422
    assert error_of(response)["details"]["fields"] == ["expected_version"]
    assert "Synthetic pause" not in response.text
