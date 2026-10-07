"""HTTP authentication and authorization of the dashboard API (real PostgreSQL, marker ``db``).

- Unauthenticated and every kind of bad token -> ``401`` with one generic message and a bearer
  challenge; tokens in query strings or cookies are never accepted.
- An authenticated user without an active membership -> ``403`` that does not reveal whether the
  requested workspace exists; ``X-Workspace-Id`` is validated against the memberships.
- Role scopes: a viewer cannot mutate, a reviewer cannot pause sources.
"""

from __future__ import annotations

import uuid

import jwt
import pytest
from tests.api.conftest import ES_KID, ApiHarness, comparable_error, error_of
from tests.integration.db.helpers import Seed

pytestmark = pytest.mark.db

INVALID = "The access token is invalid or expired"


async def test_unauthenticated_requests_are_401_with_a_bearer_challenge(bare_api: ApiHarness) -> None:
    for path in ("/api/me", "/api/overview", "/api/candidates", "/api/reviews", "/api/settings"):
        response = await bare_api.client.get(path)
        assert response.status_code == 401, path
        assert error_of(response)["code"] == "UNAUTHENTICATED"
        assert response.headers["www-authenticate"] == 'Bearer realm="suv-deals"'
        assert response.headers["cache-control"] == "no-store"
    response = await bare_api.client.post("/api/reviews/" + str(uuid.uuid4()) + "/claim", json={})
    assert response.status_code == 401
    assert (
        bare_api.metrics.authorization_denials_total.labels(
            surface="api", reason="missing_token"
        )._value.get()
        >= 6
    )


async def test_bad_tokens_are_401_with_one_generic_message(bare_api: ApiHarness) -> None:
    user = bare_api.users.owner
    t = bare_api.tokens
    bad = {
        "wrong_issuer": t.mint(user, extra={"iss": "https://evil.example/auth/v1"}),
        "wrong_audience": t.mint(user, extra={"aud": "anon"}),
        "expired": t.mint(user, expires_in=60, issued_offset=-600),
        "alg_none": t.unsigned(user),
        "hs256_forgery": t.hmac_signed(user, b"SYNTHETIC-attacker-chosen-secret-0123456789"),
        "hs256_alg_confusion": t.hmac_signed(user, t.keys.rs_public_pem, kid="synthetic-rs256-key-1"),
        "service_role": jwt.encode(
            {"iss": "supabase", "role": "service_role", "iat": 1, "exp": 4_102_444_800},
            t.keys.es_private,
            algorithm="ES256",
            headers={"kid": ES_KID},
        ),
        "service_role_claims": t.mint(user, extra={"role": "service_role"}),
        "anon_role": t.mint(user, extra={"role": "anon"}),
        "unpublished_key": t.mint(user, key=t.keys.stranger_private),
        "garbage": "not-a-token",
    }
    for name, token in bad.items():
        response = await bare_api.client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401, name
        error = error_of(response)
        assert error["code"] == "UNAUTHENTICATED"
        assert error["message"] == INVALID, name
        assert error["details"] is None
        assert 'error="invalid_token"' in response.headers["www-authenticate"]
        assert token not in response.text
    reasons = {"wrong_issuer", "wrong_audience", "expired_token", "invalid_token"}
    counted = {
        r: bare_api.metrics.authorization_denials_total.labels(surface="api", reason=r)._value.get()
        for r in reasons
    }
    assert all(value >= 1 for value in counted.values()), counted


async def test_tokens_are_only_accepted_from_the_authorization_header(bare_api: ApiHarness) -> None:
    token = bare_api.tokens.mint(bare_api.users.owner)
    for kwargs in (
        {"params": {"access_token": token}},
        {"headers": {"Cookie": f"sb-access-token={token}"}},
        {"headers": {"X-Access-Token": token}},
        {"headers": {"Authorization": f"Basic {token}"}},
        {"headers": {"Authorization": token}},
    ):
        response = await bare_api.client.get("/api/me", **kwargs)  # type: ignore[arg-type]
        assert response.status_code == 401, kwargs
    # The same token in the header works.
    ok = await bare_api.client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200


async def test_valid_member_gets_me_with_role_scopes_and_memberships(bare_api: ApiHarness) -> None:
    response = await bare_api.get("/api/me", bare_api.users.reviewer)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"schema_version", "request_id", "as_of", "data", "warnings", "next_cursor"}
    me = body["data"]
    assert me["principal_id"] == str(bare_api.users.reviewer)
    assert me["principal_kind"] == "user"
    assert me["role"] == "reviewer"
    assert set(me["scopes"]) == {
        "deals:read",
        "reviews:read",
        "reviews:write",
        "events:subscribe",
        "rechecks:request",
        "notes:write",
        "inquiries:read",
    }
    assert me["workspace"]["workspace_id"] == str(bare_api.workspace_id)
    assert [m["workspace_id"] for m in me["memberships"]] == [str(bare_api.workspace_id)]
    assert me["display_name"] is None


async def test_authenticated_non_member_is_403_without_leaking_existence(
    bare_api: ApiHarness, seed: Seed
) -> None:
    stranger = bare_api.users.stranger
    plain = await bare_api.get("/api/overview", stranger)
    existing = await bare_api.get(
        "/api/overview", stranger, headers={"X-Workspace-Id": str(bare_api.workspace_id)}
    )
    missing = await bare_api.get("/api/overview", stranger, headers={"X-Workspace-Id": str(uuid.uuid4())})
    for response in (plain, existing, missing):
        assert response.status_code == 403
        assert error_of(response)["code"] == "FORBIDDEN"
    assert comparable_error(existing) == comparable_error(missing) == comparable_error(plain)
    assert str(bare_api.workspace_id) not in existing.text
    me = await bare_api.get("/api/me", stranger)
    assert me.status_code == 403
    # Inactive membership and inactive workspace authorize nothing either.
    inactive_user = seed.user()
    seed.membership(bare_api.workspace_id, inactive_user, "owner", active=False)
    assert (await bare_api.get("/api/me", inactive_user)).status_code == 403
    closed = seed.workspace("API closed", active=False)
    closed_user = seed.user()
    seed.membership(closed, closed_user, "owner")
    assert (await bare_api.get("/api/me", closed_user)).status_code == 403
    assert (
        bare_api.metrics.authorization_denials_total.labels(surface="api", reason="not_member")._value.get()
        >= 5
    )


async def test_workspace_header_spoofing_is_rejected(bare_api: ApiHarness, seed: Seed) -> None:
    foreign = seed.workspace("API foreign")
    foreign_owner = seed.user()
    seed.membership(foreign, foreign_owner, "owner")
    reviewer = bare_api.users.reviewer
    spoofed = await bare_api.get("/api/overview", reviewer, headers={"X-Workspace-Id": str(foreign)})
    unknown = await bare_api.get("/api/overview", reviewer, headers={"X-Workspace-Id": str(uuid.uuid4())})
    assert spoofed.status_code == unknown.status_code == 403
    assert comparable_error(spoofed) == comparable_error(unknown)
    malformed = await bare_api.get("/api/overview", reviewer, headers={"X-Workspace-Id": "workspace-b"})
    assert malformed.status_code == 422
    assert error_of(malformed)["details"] == {"fields": ["X-Workspace-Id"]}
    twice = await bare_api.client.get(
        "/api/overview",
        headers=[
            ("Authorization", f"Bearer {bare_api.tokens.mint(reviewer)}"),
            ("X-Workspace-Id", str(bare_api.workspace_id)),
            ("X-Workspace-Id", str(foreign)),
        ],
    )
    assert twice.status_code == 422
    own = await bare_api.get(
        "/api/overview", reviewer, headers={"X-Workspace-Id": str(bare_api.workspace_id)}
    )
    assert own.status_code == 200


async def test_several_memberships_require_an_explicit_workspace(bare_api: ApiHarness, seed: Seed) -> None:
    second = seed.workspace("API second")
    user = bare_api.users.reviewer
    seed.membership(second, user, "owner")
    missing = await bare_api.get("/api/overview", user)
    assert missing.status_code == 422
    assert error_of(missing)["details"] == {"fields": ["X-Workspace-Id"]}
    me = await bare_api.get("/api/me", user)  # bootstrap: lists every membership
    assert me.status_code == 200
    listed = {m["workspace_id"]: m["role"] for m in me.json()["data"]["memberships"]}
    assert listed == {str(bare_api.workspace_id): "reviewer", str(second): "owner"}
    chosen = await bare_api.get("/api/me", user, headers={"X-Workspace-Id": str(second)})
    assert chosen.json()["data"]["role"] == "owner"
    assert "config:admin" in chosen.json()["data"]["scopes"]
    as_owner = await bare_api.get("/api/settings", user, headers={"X-Workspace-Id": str(second)})
    assert as_owner.status_code in (200, 422)  # an empty workspace may have no configuration yet


async def test_viewer_cannot_mutate_and_reviewer_cannot_pause(bare_api: ApiHarness) -> None:
    some_id = str(uuid.uuid4())
    mutations = {
        f"/api/reviews/{some_id}/claim": {"expected_version": 1, "idempotency_key": "viewer-claim-0001"},
        f"/api/reviews/{some_id}/release": {"claim_token": "x" * 32, "idempotency_key": "viewer-release-01"},
        f"/api/listings/{some_id}/notes": {"note": "viewer note", "idempotency_key": "viewer-note-0001"},
        f"/api/listings/{some_id}/recheck": {
            "reason": "viewer recheck",
            "idempotency_key": "viewer-recheck-1",
        },
        f"/api/sources/{some_id}/pause": {
            "expected_version": 1,
            "reason": "viewer pause",
            "idempotency_key": "viewer-pause-001",
        },
    }
    for path, body in mutations.items():
        response = await bare_api.post(path, bare_api.users.viewer, body)
        assert response.status_code == 403, path
        assert error_of(response)["code"] == "FORBIDDEN"
    submit = await bare_api.post(f"/api/reviews/{some_id}/submit", bare_api.users.viewer, {})
    assert submit.status_code == 403  # scope is checked before the body is even parsed
    pause = await bare_api.post(
        f"/api/sources/{some_id}/pause",
        bare_api.users.reviewer,
        {"expected_version": 1, "reason": "reviewer pause", "idempotency_key": "reviewer-pause-01"},
    )
    assert pause.status_code == 403
    denials = bare_api.metrics.authorization_denials_total.labels(surface="api", reason="insufficient_scope")
    assert denials._value.get() >= 7
