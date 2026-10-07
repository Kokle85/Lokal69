"""MCP authentication (spec 20, 24, 31 "API/MCP auth"): OAuth resource-server tokens, static
bearer and local development credentials, protected-resource metadata, transport checks.

Every invalid, expired, wrongly issued, wrong-audience or revoked token is the same ``401``;
an unreachable identity provider is ``503``, never ``401``. SYNTHETIC keys and data only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from jwt.exceptions import PyJWKClientConnectionError
from pydantic import SecretStr

from suv_deals.domain.enums import Role, Scope
from suv_deals.errors import Forbidden, NotFound, ValidationFailed
from suv_deals.mcp.auth import (
    ADVERTISED_SCOPES,
    ClientPrincipal,
    CredentialVerifier,
    McpAccessToken,
    McpAuthConfigError,
    McpPrincipal,
    OAuthJwtVerifier,
    hash_token,
    issue_api_credential,
    parse_scope_claim,
    revoke_api_credential,
)
from suv_deals.mcp.server import build_mcp
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work
from tests.integration.db.helpers import Seed
from tests.integration.persistence_core.support import member, system
from tests.mcp.conftest import (
    API_HOST,
    BASE_URL,
    ISSUER,
    MCP_URL,
    METADATA_URL,
    McpClient,
    SigningKeys,
    TokenFactory,
    add_members,
    build_app,
    envelope,
    make_settings,
    mcp_client,
    offline_db,
    rpc_headers,
    running,
)

SCOPE_TEXT = " ".join(s.value for s in ADVERTISED_SCOPES)
DENIALS = "suv_deals_authorization_denials_total"


def denials(metrics: AppMetrics, reason: str) -> float:
    value = metrics.registry.get_sample_value(DENIALS, {"surface": "mcp", "reason": reason})
    return value or 0.0


class StubVerifier:
    """Accepts exactly one opaque token (tests that must not reach a database)."""

    def __init__(self, scopes: frozenset[Scope] = frozenset({Scope.DEALS_READ})) -> None:
        self.scopes = scopes

    async def verify_token(self, token: str) -> McpAccessToken | None:
        if token != "synthetic-good-token":
            return None
        principal = McpPrincipal(
            workspace_id=uuid.uuid4(),
            principal_id=uuid.uuid4(),
            principal_kind="mcp_client",
            role=Role.VIEWER,
            scopes=self.scopes,
            auth_mode="oauth",
        )
        return McpAccessToken(
            token="sha256:test",
            client_id="synthetic",
            scopes=[s.value for s in self.scopes],
            resource=MCP_URL,
            subject=str(principal.principal_id),
            principal=principal,
        )


# --------------------------------------------------------------------------------------------
# OAuth mode without a database (rejected before any membership lookup)
# --------------------------------------------------------------------------------------------


async def test_unauthenticated_request_is_401_with_resource_metadata(keys: SigningKeys) -> None:
    async with mcp_client(make_settings(), offline_db(), keys) as client:
        response = await client.rpc("tools/list")
        assert response.status_code == 401
        challenge = response.headers["www-authenticate"]
        assert challenge.startswith("Bearer ")
        assert 'error="invalid_token"' in challenge
        assert f'resource_metadata="{METADATA_URL}"' in challenge
        assert f'scope="{SCOPE_TEXT}"' in challenge
        assert response.json() == {"error": "invalid_token", "error_description": "Authentication required"}
        assert response.headers["x-request-id"]
        assert response.headers["cache-control"] == "no-store"
        assert denials(client.metrics, "missing_token") == 1


async def test_protected_resource_metadata_names_the_issuer_and_scopes(keys: SigningKeys) -> None:
    async with mcp_client(make_settings(), offline_db(), keys) as client:
        response = await client.http.get("/.well-known/oauth-protected-resource/mcp")
        assert response.status_code == 200
        body = response.json()
        assert body["resource"] == MCP_URL
        assert body["authorization_servers"] == [ISSUER]  # exact string, no trailing slash
        assert body["scopes_supported"] == [s.value for s in ADVERTISED_SCOPES]
        assert "config:admin" not in body["scopes_supported"]
        assert body["bearer_methods_supported"] == ["header"]


def _bad_tokens(tokens: TokenFactory, keys: SigningKeys) -> dict[str, tuple[str, str]]:
    user = uuid.uuid4()
    scopes = [Scope.DEALS_READ]
    return {
        "garbage": ("not-a-jwt", "invalid_token"),
        "alg_none": (tokens.unsigned(user, scopes=scopes), "invalid_token"),
        "unknown_kid": (tokens.mint(user, scopes=scopes, kid="synthetic-unknown"), "invalid_token"),
        "stranger_key": (tokens.mint(user, scopes=scopes, key=keys.stranger_private), "invalid_token"),
        "expired": (tokens.mint(user, scopes=scopes, expires_in=60, issued_offset=-600), "expired_token"),
        "wrong_issuer": (
            tokens.mint(user, scopes=scopes, issuer="https://other.synthetic.example"),
            "wrong_issuer",
        ),
        "wrong_audience": (
            tokens.mint(user, scopes=scopes, audience="https://other.synthetic.example/mcp"),
            "wrong_audience",
        ),
        "dashboard_audience": (tokens.mint(user, scopes=scopes, audience="authenticated"), "wrong_audience"),
        "missing_sub": (tokens.mint(user, scopes=scopes, drop=["sub"]), "invalid_token"),
        "missing_exp": (tokens.mint(user, scopes=scopes, drop=["exp"]), "invalid_token"),
        "too_long_lived": (tokens.mint(user, scopes=scopes, expires_in=30 * 86400), "invalid_token"),
        "crit_header": (tokens.mint(user, scopes=scopes, headers={"crit": ["exp"]}), "invalid_token"),
        "non_uuid_unmapped_client": (tokens.mint("machine-client", scopes=scopes), "not_member"),
    }


@pytest.mark.parametrize(
    "case",
    [
        "garbage",
        "alg_none",
        "unknown_kid",
        "stranger_key",
        "expired",
        "wrong_issuer",
        "wrong_audience",
        "dashboard_audience",
        "missing_sub",
        "missing_exp",
        "too_long_lived",
        "crit_header",
        "non_uuid_unmapped_client",
    ],
)
async def test_invalid_tokens_are_the_same_401(case: str, keys: SigningKeys, tokens: TokenFactory) -> None:
    token, reason = _bad_tokens(tokens, keys)[case]
    async with mcp_client(make_settings(), offline_db(), keys) as client:
        response = await client.rpc("tools/list", token=token)
        assert response.status_code == 401
        assert response.json() == {"error": "invalid_token", "error_description": "Authentication required"}
        assert f'resource_metadata="{METADATA_URL}"' in response.headers["www-authenticate"]
        assert token not in response.text
        assert denials(client.metrics, reason) == 1


async def test_token_in_query_string_or_body_is_never_accepted(keys: SigningKeys) -> None:
    app = build_app(make_settings(), offline_db(), keys, token_verifier=StubVerifier())
    async with running(app) as http:
        query = await http.post(
            "/mcp?access_token=synthetic-good-token",
            json=envelope("tools/list"),
            headers=rpc_headers("tools/list"),
        )
        assert query.status_code == 401
        body = envelope("tools/list", {"access_token": "synthetic-good-token"})
        in_body = await http.post("/mcp", json=body, headers=rpc_headers("tools/list"))
        assert in_body.status_code == 401
        good = await http.post(
            "/mcp",
            json=envelope("tools/list"),
            headers=rpc_headers("tools/list", token="synthetic-good-token"),
        )
        assert good.status_code == 200


async def test_two_authorization_headers_are_refused(keys: SigningKeys) -> None:
    app = build_app(make_settings(), offline_db(), keys, token_verifier=StubVerifier())
    async with running(app) as http:
        headers = [
            *rpc_headers("tools/list").items(),
            ("Authorization", "Bearer synthetic-good-token"),
            ("Authorization", "Bearer synthetic-good-token"),
        ]
        response = await http.post("/mcp", json=envelope("tools/list"), headers=headers)
        assert response.status_code == 401
        assert f'scope="{SCOPE_TEXT}"' in response.headers["www-authenticate"]


async def test_jwks_outage_is_503_not_401(keys: SigningKeys, tokens: TokenFactory) -> None:
    class Down:
        def get_signing_key_from_jwt(self, token: str) -> Any:
            raise PyJWKClientConnectionError("synthetic outage")

    async with mcp_client(make_settings(), offline_db(), keys, jwks=Down()) as client:
        token = tokens.mint(uuid.uuid4(), scopes=[Scope.DEALS_READ])
        response = await client.rpc("tools/list", token=token)
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"
        assert response.json()["error"] == "temporarily_unavailable"


async def test_database_outage_during_membership_mapping_is_503(
    keys: SigningKeys, tokens: TokenFactory
) -> None:
    async with mcp_client(make_settings(), offline_db(), keys) as client:
        token = tokens.mint(uuid.uuid4(), scopes=[Scope.DEALS_READ])  # valid; mapping needs the DB
        response = await client.rpc("tools/list", token=token)
        assert response.status_code == 503


async def test_protocol_header_mismatch_is_400_like_the_sdk(keys: SigningKeys) -> None:
    app = build_app(make_settings(), offline_db(), keys, token_verifier=StubVerifier())
    async with running(app) as http:
        token = "synthetic-good-token"
        body = envelope("tools/call", {"name": "deals_health", "arguments": {}})
        wrong_name = await http.post(
            "/mcp", json=body, headers=rpc_headers("tools/call", "deals_get_candidate", token)
        )
        assert wrong_name.status_code == 400
        assert wrong_name.json()["error"]["code"] == -32020
        wrong_method = await http.post(
            "/mcp", json=body, headers=rpc_headers("tools/list", "deals_health", token)
        )
        assert wrong_method.status_code == 400
        assert wrong_method.json()["error"]["code"] == -32020
        headers = {**rpc_headers("tools/list", token=token), "MCP-Protocol-Version": "2099-01-01"}
        wrong_version = await http.post("/mcp", json=envelope("tools/list"), headers=headers)
        assert wrong_version.status_code == 400
        assert wrong_version.json()["error"]["code"] == -32020


async def test_host_and_origin_are_validated(keys: SigningKeys) -> None:
    app = build_app(make_settings(), offline_db(), keys, token_verifier=StubVerifier())
    async with running(app) as http:
        headers = rpc_headers("tools/list", token="synthetic-good-token")
        allowed = await http.post(
            "/mcp",
            json=envelope("tools/list"),
            headers={**headers, "Origin": "https://dash.synthetic.example"},
        )
        assert allowed.status_code == 200
        foreign = await http.post(
            "/mcp", json=envelope("tools/list"), headers={**headers, "Origin": "https://evil.example"}
        )
        assert foreign.status_code == 403
        rebinding = await http.post(
            "/mcp", json=envelope("tools/list"), headers={**headers, "Host": "attacker.example"}
        )
        assert rebinding.status_code == 421
    assert API_HOST in BASE_URL


async def test_unknown_method_is_404_method_not_found(keys: SigningKeys) -> None:
    app = build_app(make_settings(), offline_db(), keys, token_verifier=StubVerifier())
    async with running(app) as http:
        response = await http.post(
            "/mcp",
            json=envelope("tools/execute_sql"),
            headers=rpc_headers("tools/execute_sql", token="synthetic-good-token"),
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == -32601


# --------------------------------------------------------------------------------------------
# Configuration that cannot authenticate safely never opens the endpoint
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"mcp_oauth_issuer": None}, "MCP_OAUTH_ISSUER is required"),
        ({"mcp_public_url": None}, "MCP_PUBLIC_URL is required"),
        ({"mcp_public_url": "https://api.synthetic.example/other"}, "MCP_PUBLIC_URL must end with /mcp"),
        (
            {"mcp_auth_mode": "dev_local"},
            "MCP_AUTH_MODE=dev_local requires a loopback MCP_PUBLIC_URL/APP_BASE_URL",
        ),
        (
            {
                "mcp_auth_mode": "dev_local",
                "app_env": "production",
                "mcp_public_url": "http://127.0.0.1:8000/mcp",
            },
            "MCP_AUTH_MODE=dev_local is only allowed when APP_ENV is development or test",
        ),
        (
            {
                "mcp_auth_mode": "static_bearer",
                "app_env": "production",
                "mcp_public_url": None,
                "app_base_url": "http://dash.synthetic.example",
            },
            "MCP_PUBLIC_URL must use https in production",
        ),
        (
            {"mcp_events_enabled": True, "mcp_event_subscription_secret_encryption_key": None},
            "MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY is missing or invalid",
        ),
    ],
)
async def test_unsafe_configuration_disables_the_endpoint(
    overrides: dict[str, Any], problem: str, keys: SigningKeys
) -> None:
    settings = make_settings(**overrides)
    app = build_mcp(settings, offline_db(), jwks=keys.jwks, metrics=AppMetrics(process_metrics=False))
    assert not app.configured and app.problems == (problem,)
    async with running(app) as http:
        response = await http.post("/mcp", json=envelope("tools/list"), headers=rpc_headers("tools/list"))
        assert response.status_code == 503
        assert response.json()["error"] == "temporarily_unavailable"


async def test_oauth_without_jwks_url_or_resolver_is_disabled() -> None:
    app = build_mcp(make_settings(), offline_db(), metrics=AppMetrics(process_metrics=False))
    assert app.problems == ("MCP_OAUTH_JWKS_URL is required",)
    pinned = build_mcp(
        make_settings(), offline_db(), jwks={"keys": []}, metrics=AppMetrics(process_metrics=False)
    )
    assert pinned.problems == ("the pinned OAuth signing keys are invalid",)


def test_scope_claim_parsing_ignores_unknown_values() -> None:
    assert parse_scope_claim({"scope": "openid deals:read  bogus reviews:read"}) == {
        Scope.DEALS_READ,
        Scope.REVIEWS_READ,
    }
    assert parse_scope_claim({"scp": ["notes:write", 7, None]}) == {Scope.NOTES_WRITE}
    assert parse_scope_claim({"scope": 12}) == frozenset()
    assert parse_scope_claim({}) == frozenset()


# --------------------------------------------------------------------------------------------
# OAuth principal mapping (database)
# --------------------------------------------------------------------------------------------


async def _tool_names(client: McpClient, token: str) -> list[str]:
    return [t["name"] for t in await client.tools(token)]


@pytest.mark.db
async def test_oauth_user_maps_to_membership_and_token_scopes_narrow_the_role(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    workspace = seed.workspace("MCP auth mapping")
    users = add_members(seed, workspace)
    async with mcp_client(make_settings(), db, keys) as client:
        # A viewer token asking for writes only gets what the viewer role allows.
        viewer = tokens.mint(
            users.viewer, scopes=[Scope.DEALS_READ, Scope.REVIEWS_WRITE, Scope.SOURCES_PAUSE]
        )
        assert await _tool_names(client, viewer) == [
            "deals_health",
            "deals_list_candidates",
            "deals_get_candidate",
            "deals_get_comparables",
            "deals_get_valuation",
        ]
        # Unknown, admin and mail-ingest scopes are never effective on MCP.
        owner = tokens.mint(users.owner, scopes=["openid", "config:admin", "mail:ingest", "reviews:read"])
        assert await _tool_names(client, owner) == ["reviews_list_pending"]
        # No scope claim: authenticated but nothing is discoverable.
        bare = tokens.mint(users.reviewer, scopes=[])
        assert await _tool_names(client, bare) == []
        # A user without a membership is not a principal here.
        stranger = tokens.mint(users.stranger, scopes=[Scope.DEALS_READ])
        assert (await client.rpc("tools/list", token=stranger)).status_code == 401
        assert denials(client.metrics, "not_member") == 1


@pytest.mark.db
async def test_audience_may_be_the_configured_audience_or_the_resource(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    workspace = seed.workspace("MCP audience")
    users = add_members(seed, workspace)
    settings = make_settings(mcp_oauth_audience="api://suv-deals-synthetic")
    async with mcp_client(settings, db, keys) as client:
        for audience in ("api://suv-deals-synthetic", MCP_URL, ["other", MCP_URL]):
            token = tokens.mint(users.viewer, scopes=[Scope.DEALS_READ], audience=audience)
            assert (await client.rpc("tools/list", token=token)).status_code == 200
        wrong = tokens.mint(users.viewer, scopes=[Scope.DEALS_READ], audience="api://another")
        assert (await client.rpc("tools/list", token=wrong)).status_code == 401


@pytest.mark.db
async def test_several_memberships_need_a_signed_workspace_claim(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    first = seed.workspace("MCP first")
    second = seed.workspace("MCP second")
    foreign = seed.workspace("MCP foreign")
    user = seed.user()
    seed.membership(first, user, "viewer")
    seed.membership(second, user, "reviewer")
    seed.membership(foreign, seed.user(), "owner")
    async with mcp_client(make_settings(), db, keys) as client:
        scopes = [Scope.DEALS_READ, Scope.REVIEWS_WRITE]
        ambiguous = tokens.mint(user, scopes=scopes)
        assert (await client.rpc("tools/list", token=ambiguous)).status_code == 401
        chosen = tokens.mint(user, scopes=scopes, extra={"workspace_id": str(second)})
        names = await _tool_names(client, chosen)
        assert "reviews_claim" in names  # the reviewer membership was selected
        not_mine = tokens.mint(user, scopes=scopes, extra={"workspace_id": str(foreign)})
        assert (await client.rpc("tools/list", token=not_mine)).status_code == 401
        malformed = tokens.mint(user, scopes=scopes, extra={"workspace_id": "not-a-uuid"})
        assert (await client.rpc("tools/list", token=malformed)).status_code == 401
    seed.conn.execute(
        "update app.memberships set active = false where workspace_id = %s and user_id = %s", (first, user)
    )
    async with mcp_client(make_settings(), db, keys) as client:
        sole = tokens.mint(user, scopes=scopes)  # only one active membership is left
        assert "reviews_claim" in await _tool_names(client, sole)


@pytest.mark.db
async def test_mapped_machine_client_uses_its_explicit_principal(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> None:
    workspace = seed.workspace("MCP machine client")
    principal = uuid.uuid4()
    clients = {
        "synthetic-machine": ClientPrincipal(
            workspace_id=workspace,
            principal_id=principal,
            role=Role.VIEWER,
            scopes=frozenset({Scope.DEALS_READ}),
        )
    }
    async with mcp_client(make_settings(), db, keys, client_principals=clients) as client:
        token = tokens.mint(
            "synthetic-machine",
            scopes=[Scope.DEALS_READ, Scope.REVIEWS_READ],
            extra={"client_id": "synthetic-machine"},
        )
        assert await _tool_names(client, token) == [
            "deals_health",
            "deals_list_candidates",
            "deals_get_candidate",
            "deals_get_comparables",
            "deals_get_valuation",
        ]
        health = await client.ok("deals_health", token=token)
        assert health["data"]["ready"] in (True, False)


# --------------------------------------------------------------------------------------------
# Static bearer and dev_local credentials (database)
# --------------------------------------------------------------------------------------------


async def _issue(
    db: Database,
    workspace: UUID,
    *,
    principal: UUID,
    kind: str = "static_bearer",
    principal_kind: str = "user",
    role: Role = Role.REVIEWER,
    scopes: tuple[Scope, ...] = (Scope.DEALS_READ, Scope.REVIEWS_READ, Scope.REVIEWS_WRITE),
) -> Any:
    actor = system(workspace)
    async with unit_of_work(db, actor) as conn:
        return await issue_api_credential(
            conn,
            actor,
            principal_id=principal,
            principal_kind=principal_kind,  # type: ignore[arg-type]
            role=role,
            scopes=scopes,
            label="SYNTHETIC test credential",
            kind=kind,  # type: ignore[arg-type]
        )


@pytest.mark.db
async def test_static_bearer_credential_lifecycle(db: Database, seed: Seed, keys: SigningKeys) -> None:
    workspace = seed.workspace("MCP static bearer")
    users = add_members(seed, workspace)
    issued = await _issue(db, workspace, principal=users.reviewer)
    token = issued.token.get_secret_value()
    assert token.startswith("suvmcp_") and issued.token_prefix == token[:13]
    assert token not in repr(issued)
    row = seed.conn.execute(
        "select token_hash, token_prefix, credential_kind, last_used_at from ops.api_credentials"
        " where id = %s",
        (issued.credential_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == hash_token(token) and row[1] == issued.token_prefix and row[2] == "static_bearer"
    assert row[3] is None
    stored = seed.conn.execute(
        "select row_to_json(c)::text from ops.api_credentials c where id = %s", (issued.credential_id,)
    ).fetchone()
    assert stored is not None and token not in stored[0]  # only the hash is stored

    settings = make_settings(mcp_auth_mode="static_bearer")
    metrics = AppMetrics(process_metrics=False)
    async with mcp_client(settings, db, keys, metrics=metrics) as client:
        assert client.app.auth_mode == "static_bearer"
        unauthenticated = await client.rpc("tools/list")
        assert unauthenticated.status_code == 401
        assert "resource_metadata" not in unauthenticated.headers["www-authenticate"]  # not OAuth
        no_metadata = await client.http.get("/.well-known/oauth-protected-resource/mcp")
        assert no_metadata.status_code == 404
        names = await _tool_names(client, token)
        assert "reviews_claim" in names and "sources_pause" not in names
        touched = seed.scalar(
            "select last_used_at from ops.api_credentials where id = %s", (issued.credential_id,)
        )
        assert touched is not None
        # OAuth JWTs and dev credentials are not accepted in static_bearer mode.
        dev = await _issue(db, workspace, principal=users.reviewer, kind="dev_local")
        assert (await client.rpc("tools/list", token=dev.token.get_secret_value())).status_code == 401
        assert (await client.rpc("tools/list", token="suvmcp_" + "0" * 64)).status_code == 401
        # Revocation takes effect on the next request.
        actor = system(workspace)
        async with unit_of_work(db, actor) as conn:
            assert await revoke_api_credential(conn, actor, issued.credential_id, reason="SYNTHETIC rotation")
        async with unit_of_work(db, actor) as conn:
            assert not await revoke_api_credential(
                conn, actor, issued.credential_id, reason="SYNTHETIC again"
            )
        revoked = await client.rpc("tools/list", token=token)
        assert revoked.status_code == 401
        assert denials(metrics, "revoked") == 1


@pytest.mark.db
async def test_static_bearer_rejects_expired_inactive_and_mail_worker_credentials(
    db: Database, seed: Seed, keys: SigningKeys
) -> None:
    workspace = seed.workspace("MCP static negatives")
    users = add_members(seed, workspace)
    now = datetime.now(UTC)
    expired_token = "suvmcp_" + "e" * 64
    seed.insert(
        "ops.api_credentials",
        workspace_id=workspace,
        principal_id=users.reviewer,
        principal_kind="user",
        role="reviewer",
        credential_kind="static_bearer",
        token_hash=hash_token(expired_token),
        scopes=["deals:read"],
        label="SYNTHETIC expired",
        created_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )
    mail_token = "suvmcp_" + "a" * 64
    seed.insert(
        "ops.api_credentials",
        workspace_id=workspace,
        principal_id=uuid.uuid4(),
        principal_kind="mcp_client",
        role="owner",
        credential_kind="static_bearer",
        token_hash=hash_token(mail_token),
        scopes=["mail:ingest"],
        label="SYNTHETIC mail worker",
        expires_at=now + timedelta(days=1),
    )
    member_cred = await _issue(db, workspace, principal=users.second_reviewer)
    client_cred = await _issue(
        db,
        workspace,
        principal=uuid.uuid4(),
        principal_kind="mcp_client",
        role=Role.VIEWER,
        scopes=(Scope.DEALS_READ,),
    )
    metrics = AppMetrics(process_metrics=False)
    async with mcp_client(make_settings(mcp_auth_mode="static_bearer"), db, keys, metrics=metrics) as client:
        assert (await client.rpc("tools/list", token=expired_token)).status_code == 401
        assert denials(metrics, "expired_token") == 1
        assert (await client.rpc("tools/list", token=mail_token)).status_code == 401
        assert len(await client.tools(client_cred.token.get_secret_value())) == 5
        member_token = member_cred.token.get_secret_value()
        assert "reviews_claim" in await _tool_names(client, member_token)
        # A demoted member's credential is cut to the new role; a removed member loses access.
        seed.conn.execute(
            "update app.memberships set role = 'viewer' where workspace_id = %s and user_id = %s",
            (workspace, users.second_reviewer),
        )
        assert "reviews_claim" not in await _tool_names(client, member_token)
        seed.conn.execute(
            "update app.memberships set active = false where workspace_id = %s and user_id = %s",
            (workspace, users.second_reviewer),
        )
        assert (await client.rpc("tools/list", token=member_token)).status_code == 401
        assert denials(metrics, "not_member") == 1


@pytest.mark.db
async def test_issuing_credentials_is_restricted_and_bounded(db: Database, seed: Seed) -> None:
    workspace = seed.workspace("MCP issuing")
    users = add_members(seed, workspace)
    reviewer = member(workspace, Role.REVIEWER, principal_id=users.reviewer)
    async with unit_of_work(db, reviewer) as conn:
        with pytest.raises(Forbidden):
            await issue_api_credential(
                conn,
                reviewer,
                principal_id=users.reviewer,
                principal_kind="user",
                role=Role.REVIEWER,
                scopes=[Scope.DEALS_READ],
                label="SYNTHETIC",
            )
    actor = system(workspace)
    bad: list[dict[str, Any]] = [
        {"scopes": [Scope.MAIL_INGEST], "role": Role.OWNER},
        {"scopes": [Scope.CONFIG_ADMIN], "role": Role.OWNER},
        {"scopes": [Scope.SOURCES_PAUSE], "role": Role.REVIEWER},
        {"scopes": [], "role": Role.VIEWER},
        {"scopes": [Scope.DEALS_READ], "role": Role.VIEWER, "lifetime": timedelta(days=400)},
        {"scopes": [Scope.DEALS_READ], "role": Role.VIEWER, "label": "  "},
    ]
    for case in bad:
        async with unit_of_work(db, actor) as conn:
            with pytest.raises(ValidationFailed):
                await issue_api_credential(
                    conn,
                    actor,
                    principal_id=users.owner,
                    principal_kind="mcp_client",
                    role=case["role"],
                    scopes=case["scopes"],
                    label=case.get("label", "SYNTHETIC"),
                    lifetime=case.get("lifetime", timedelta(days=1)),
                )
    async with unit_of_work(db, actor) as conn:
        with pytest.raises(NotFound):  # a user credential needs an active membership allowing the scopes
            await issue_api_credential(
                conn,
                actor,
                principal_id=users.viewer,
                principal_kind="user",
                role=Role.REVIEWER,
                scopes=[Scope.REVIEWS_WRITE],
                label="SYNTHETIC",
            )
    async with unit_of_work(db, actor) as conn:
        with pytest.raises(NotFound):
            await revoke_api_credential(conn, actor, uuid.uuid4(), reason="SYNTHETIC unknown")
    issued = await _issue(db, workspace, principal=users.owner, role=Role.OWNER, scopes=(Scope.DEALS_READ,))
    audit = seed.scalar(
        "select count(*) from ops.audit_events where workspace_id = %s and action = 'credential.create'"
        " and target_id = %s",
        (workspace, issued.credential_id),
    )
    assert audit == 1


@pytest.mark.db
async def test_dev_local_mode_accepts_only_dev_credentials_from_loopback(
    db: Database, seed: Seed, keys: SigningKeys
) -> None:
    workspace = seed.workspace("MCP dev local")
    users = add_members(seed, workspace)
    dev = (
        await _issue(
            db,
            workspace,
            principal=users.viewer,
            kind="dev_local",
            role=Role.VIEWER,
            scopes=(Scope.DEALS_READ,),
        )
    ).token.get_secret_value()
    static = (
        await _issue(db, workspace, principal=users.viewer, role=Role.VIEWER, scopes=(Scope.DEALS_READ,))
    ).token.get_secret_value()
    settings = make_settings(
        mcp_auth_mode="dev_local",
        mcp_public_url="http://127.0.0.1:8000/mcp",
        app_base_url="http://127.0.0.1:8000",
        mcp_allowed_origins="",
    )
    local = "http://127.0.0.1:8000"
    async with mcp_client(settings, db, keys, base_url=local) as client:
        assert client.app.auth_mode == "dev_local"
        assert len(await client.tools(dev)) == 5
        assert (await client.rpc("tools/list", token=static)).status_code == 401
    async with mcp_client(settings, db, keys, base_url=local, client=("203.0.113.9", 4000)) as remote:
        response = await remote.rpc("tools/list", token=dev)
        assert response.status_code == 403
        assert response.json()["error"] == "forbidden"


def test_credential_verifier_requires_a_kind() -> None:
    with pytest.raises(ValueError, match="credential kind"):
        CredentialVerifier(db=offline_db(), kinds=[])


def test_oauth_verifier_from_settings_reports_missing_settings_by_name() -> None:
    settings = make_settings(mcp_oauth_issuer="http://issuer.synthetic.example", mcp_oauth_jwks_url=None)
    with pytest.raises(McpAuthConfigError) as caught:
        OAuthJwtVerifier.from_settings(settings, offline_db())
    assert caught.value.problems == ("MCP_OAUTH_ISSUER must use https", "MCP_OAUTH_JWKS_URL is required")
    secret = SecretStr("never-printed")
    assert "never-printed" not in str(caught.value) and secret.get_secret_value() == "never-printed"
