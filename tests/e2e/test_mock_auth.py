"""Offline checks of the E2E mock Supabase Auth server (no network, no database).

The tokens it issues must be accepted by the backend's real `SupabaseJwtVerifier` (same issuer,
audience, algorithm, kid and claims), and it must behave like Supabase where the dashboard relies
on it: apikey required, refresh-token rotation, PKCE magic links, logout revocation.
"""

from __future__ import annotations

import base64
import hashlib
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from suv_deals.api.auth import AuthFailure, StaticJwks, SupabaseJwtVerifier
from tests.e2e.mock_supabase_auth import create_mock_auth_app
from tests.e2e.users import DEFAULT_TTL, PASSWORD, PUBLISHABLE_KEY, USERS

BASE = "http://127.0.0.1:54399"
ORIGIN = "http://127.0.0.1:4173"
HEADERS = {"apikey": PUBLISHABLE_KEY}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_mock_auth_app(base_url=BASE, allow_origins=[ORIGIN]))


def _verifier(client: TestClient) -> SupabaseJwtVerifier:
    jwks = client.get("/auth/v1/.well-known/jwks.json").json()
    return SupabaseJwtVerifier(issuer=f"{BASE}/auth/v1", audience="authenticated", resolver=StaticJwks(jwks))


def _password(client: TestClient, user: str = "reviewer") -> dict[str, object]:
    response = client.post(
        "/auth/v1/token?grant_type=password",
        json={"email": USERS[user].email, "password": PASSWORD},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


async def test_issued_tokens_pass_the_backend_verifier(client: TestClient) -> None:
    session = _password(client)
    verified = await _verifier(client).verify(str(session["access_token"]))
    assert verified.user_id == USERS["reviewer"].user_id
    assert session["expires_in"] == DEFAULT_TTL
    assert session["token_type"] == "bearer"


async def test_short_lived_user_is_refused_after_expiry_while_advertising_the_normal_lifetime(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    issued_at = time.time() - 600  # issue the token "10 minutes ago": its real 6 s lifetime is over
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: issued_at)
        session = _password(client, "expiring")
    assert session["expires_in"] == DEFAULT_TTL  # what supabase-js sees
    verifier = SupabaseJwtVerifier(
        issuer=f"{BASE}/auth/v1",
        audience="authenticated",
        resolver=StaticJwks(client.get("/auth/v1/.well-known/jwks.json").json()),
        leeway=timedelta(0),
    )
    with pytest.raises(AuthFailure):
        await verifier.verify(str(session["access_token"]))


def test_wrong_password_and_missing_apikey_are_refused(client: TestClient) -> None:
    wrong = client.post(
        "/auth/v1/token?grant_type=password",
        json={"email": USERS["reviewer"].email, "password": "nope"},
        headers=HEADERS,
    )
    assert wrong.status_code == 400
    assert wrong.json()["code"] == "invalid_credentials"
    no_key = client.post(
        "/auth/v1/token?grant_type=password", json={"email": USERS["reviewer"].email, "password": PASSWORD}
    )
    assert no_key.status_code == 401


def test_refresh_tokens_rotate_and_logout_revokes(client: TestClient) -> None:
    session = _password(client)
    refreshed = client.post(
        "/auth/v1/token?grant_type=refresh_token",
        json={"refresh_token": session["refresh_token"]},
        headers=HEADERS,
    )
    assert refreshed.status_code == 200
    reused = client.post(
        "/auth/v1/token?grant_type=refresh_token",
        json={"refresh_token": session["refresh_token"]},
        headers=HEADERS,
    )
    assert reused.status_code == 400
    current = refreshed.json()
    out = client.post(
        "/auth/v1/logout?scope=global",
        headers={**HEADERS, "Authorization": f"Bearer {current['access_token']}"},
    )
    assert out.status_code == 204
    after = client.post(
        "/auth/v1/token?grant_type=refresh_token",
        json={"refresh_token": current["refresh_token"]},
        headers=HEADERS,
    )
    assert after.status_code == 400


def test_pkce_magic_link_round_trip(client: TestClient) -> None:
    verifier = "synthetic-code-verifier-0123456789abcdefghijklmnopqrstuvwxyz"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    email = USERS["owner"].email
    sent = client.post(
        f"/auth/v1/otp?redirect_to={ORIGIN}/auth/callback",
        json={"email": email, "code_challenge": challenge, "code_challenge_method": "s256"},
        headers=HEADERS,
    )
    assert sent.status_code == 200
    link = client.get(f"/__e2e/magic-link?email={email}").json()["url"]
    redirect = client.get(urlsplit(link).path + "?" + urlsplit(link).query, follow_redirects=False)
    assert redirect.status_code == 303
    target = urlsplit(redirect.headers["location"])
    assert f"{target.scheme}://{target.netloc}" == ORIGIN
    code = parse_qs(target.query)["code"][0]
    wrong = client.post(
        "/auth/v1/token?grant_type=pkce", json={"auth_code": code, "code_verifier": "wrong"}, headers=HEADERS
    )
    assert wrong.status_code == 400  # and the code is now consumed
    sent_again = client.post(
        f"/auth/v1/otp?redirect_to={ORIGIN}/auth/callback",
        json={"email": email, "code_challenge": challenge, "code_challenge_method": "s256"},
        headers=HEADERS,
    )
    assert sent_again.status_code == 200
    link = client.get(f"/__e2e/magic-link?email={email}").json()["url"]
    code = parse_qs(
        urlsplit(client.get(link.replace(BASE, ""), follow_redirects=False).headers["location"]).query
    )["code"][0]
    ok = client.post(
        "/auth/v1/token?grant_type=pkce", json={"auth_code": code, "code_verifier": verifier}, headers=HEADERS
    )
    assert ok.status_code == 200
    assert ok.json()["user"]["email"] == email
    reused = client.get(link.replace(BASE, ""), follow_redirects=False)
    assert "error=access_denied" in reused.headers["location"]


def test_unknown_email_gets_the_same_otp_answer_and_foreign_redirects_are_ignored(client: TestClient) -> None:
    response = client.post(
        "/auth/v1/otp?redirect_to=https://evil.example/steal",
        json={"email": "nobody@e2e.invalid"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert client.get("/__e2e/magic-link?email=nobody@e2e.invalid").status_code == 404
    client.post(
        "/auth/v1/otp?redirect_to=https://evil.example/steal",
        json={"email": USERS["viewer"].email},
        headers=HEADERS,
    )
    link = client.get(f"/__e2e/magic-link?email={USERS['viewer'].email}").json()["url"]
    assert "evil.example" not in parse_qs(urlsplit(link).query)["redirect_to"][0]
