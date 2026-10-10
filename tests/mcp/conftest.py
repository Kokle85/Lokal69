"""Fixtures for the MCP server tests (SYNTHETIC data, locally generated keys, no network).

- OAuth access tokens are minted with locally generated ES256/RS256 keys and verified through an
  in-memory JWKS (``build_mcp(..., jwks=...)``); no authorization server, Supabase or Slack is
  ever contacted.
- The MCP app runs in-process behind ``httpx2.ASGITransport`` with its lifespan entered (the
  SDK session manager must run), exactly as research section 8 describes. Raw JSON-RPC requests
  carry the 2026-07-28 headers (``MCP-Protocol-Version``, ``Mcp-Method``, ``Mcp-Name``) and the
  ``_meta`` envelope.
- Database tests use the migrated test database through ``Database(db_url,
  set_role="suv_backend")`` (RLS and grants as in production) and the read-query dataset
  builders (read-only reuse).
- Callback challenges go to `FakeCallback`, an in-memory ``SafeHttp`` that verifies the Standard
  Webhooks signature and echoes (or refuses) the challenge.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx2
import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jsonschema import Draft202012Validator
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from pydantic import SecretStr

from suv_deals.domain.actor import ROLE_SCOPES
from suv_deals.domain.enums import Role, Scope
from suv_deals.integrations.safe_http import SafeHttpError, SafeHttpFailure, SafeResponse
from suv_deals.integrations.webhook_signing import parse_whsec, verify_inbound
from suv_deals.mcp.auth import MCP_SCOPES
from suv_deals.mcp.schemas import tool_error_schema
from suv_deals.mcp.server import McpApp, build_mcp
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence.database import Database
from suv_deals.settings import Settings
from tests.integration.db.helpers import Seed
from tests.integration.read_queries.dataset import CURSOR_SECRET, SeededWorkspace, seed_workspace

PROTOCOL = "2026-07-28"
API_HOST = "api.synthetic.example"
BASE_URL = f"https://{API_HOST}"
MCP_URL = f"{BASE_URL}/mcp"
ISSUER = "https://issuer.synthetic.example"
DASHBOARD = "https://dash.synthetic.example"
METADATA_URL = f"{BASE_URL}/.well-known/oauth-protected-resource/mcp"
ES_KID = "synthetic-mcp-es256-1"
RS_KID = "synthetic-mcp-rs256-1"
CALLBACK_URL = "https://receiver.synthetic.example/mcp-events/callback_1"
EVENT_KEY = base64.b64encode(b"S" * 32).decode()
UNREACHABLE_DB = "postgresql://suv:suv@127.0.0.1:1/synthetic_unreachable"


def whsec(fill: bytes = b"w") -> str:
    """A SYNTHETIC Standard Webhooks secret (32 bytes)."""
    return "whsec_" + base64.b64encode(fill * 32).decode()


# --------------------------------------------------------------------------------------------
# Keys and tokens
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SigningKeys:
    es_private: ec.EllipticCurvePrivateKey
    rs_private: rsa.RSAPrivateKey
    stranger_private: ec.EllipticCurvePrivateKey  # never published
    jwks: dict[str, Any]


def _jwk(public_json: str, kid: str, alg: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(public_json)
    data.update(kid=kid, alg=alg, use="sig", key_ops=["verify"])
    return data


@pytest.fixture(scope="session")
def keys() -> SigningKeys:
    es = ec.generate_private_key(ec.SECP256R1())
    rs = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    stranger = ec.generate_private_key(ec.SECP256R1())
    jwks = {
        "keys": [
            _jwk(ECAlgorithm.to_jwk(es.public_key()), ES_KID, "ES256"),
            _jwk(RSAAlgorithm.to_jwk(rs.public_key()), RS_KID, "RS256"),
        ]
    }
    return SigningKeys(es_private=es, rs_private=rs, stranger_private=stranger, jwks=jwks)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def scope_text(scopes: Sequence[Scope | str]) -> str:
    return " ".join(s.value if isinstance(s, Scope) else s for s in scopes)


def role_scopes(role: Role) -> list[Scope]:
    return [s for s in Scope if s in ROLE_SCOPES[role] and s in MCP_SCOPES]


@dataclass
class TokenFactory:
    """Mints SYNTHETIC OAuth access tokens for this resource server."""

    keys: SigningKeys

    def claims(
        self,
        sub: UUID | str,
        *,
        scopes: Sequence[Scope | str] = (),
        expires_in: int = 3600,
        issued_offset: int = 0,
        issuer: str = ISSUER,
        audience: str | list[str] = MCP_URL,
        extra: Mapping[str, Any] | None = None,
        drop: Sequence[str] = (),
    ) -> dict[str, Any]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": issuer,
            "aud": audience,
            "sub": str(sub),
            "iat": now + issued_offset,
            "exp": now + issued_offset + expires_in,
            "scope": scope_text(scopes),
            "client_id": "synthetic-chatgpt-client",
            "jti": uuid.uuid4().hex,
        }
        claims.update(extra or {})
        for name in drop:
            claims.pop(name, None)
        return claims

    def mint(
        self,
        sub: UUID | str,
        *,
        alg: str = "ES256",
        kid: str | None = None,
        key: Any = None,
        headers: Mapping[str, Any] | None = None,
        **claim_options: Any,
    ) -> str:
        payload = self.claims(sub, **claim_options)
        signing_key = key
        if signing_key is None:
            signing_key = self.keys.es_private if alg == "ES256" else self.keys.rs_private
        header: dict[str, Any] = {"kid": kid or (ES_KID if alg == "ES256" else RS_KID)}
        header.update(headers or {})
        return jwt.encode(payload, signing_key, algorithm=alg, headers=header)

    def unsigned(self, sub: UUID | str, **claim_options: Any) -> str:
        header = {"alg": "none", "typ": "JWT", "kid": ES_KID}
        payload = self.claims(sub, **claim_options)
        return f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(payload).encode())}."

    def for_role(self, sub: UUID, role: Role, **options: Any) -> str:
        return self.mint(sub, scopes=role_scopes(role), **options)


@pytest.fixture(scope="session")
def tokens(keys: SigningKeys) -> TokenFactory:
    return TokenFactory(keys)


# --------------------------------------------------------------------------------------------
# Settings and app construction
# --------------------------------------------------------------------------------------------


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "app_base_url": DASHBOARD,
        "build_id": "synthetic-mcp.1",
        "mcp_public_url": MCP_URL,
        "mcp_auth_mode": "oauth",
        "mcp_oauth_issuer": ISSUER,
        "mcp_oauth_audience": None,
        "mcp_oauth_jwks_url": None,
        "mcp_allowed_origins": DASHBOARD,
        "mcp_cursor_signing_secret": SecretStr(CURSOR_SECRET.decode()),
        "source_network_enabled": False,
        "allow_external_notifications": False,
        "event_bridge_enabled": False,
        "mcp_events_enabled": False,
        "manual_4000_profile_enabled": False,
        "below_target_watch_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def events_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "mcp_events_enabled": True,
        "allow_external_notifications": True,
        "mcp_event_subscription_secret_encryption_key": SecretStr(EVENT_KEY),
    }
    values.update(overrides)
    return make_settings(**values)


def offline_db() -> Database:
    """A pool that is never opened (tests that must not reach any database)."""
    return Database(UNREACHABLE_DB, min_size=1, max_size=1)


# --------------------------------------------------------------------------------------------
# Fake callback receiver (SafeHttp)
# --------------------------------------------------------------------------------------------


@dataclass
class FakeCallback:
    """In-memory ``SafeHttp``: verifies the signed challenge and answers per ``mode``.

    Modes: ``echo`` (2xx + exact echo), ``wrong_echo``, ``http_500``, ``timeout``, ``refused``.
    """

    secret: str = field(default_factory=whsec)
    mode: str = "echo"
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def post(
        self,
        url: str,
        *,
        content: bytes,
        headers: Mapping[str, str],
        timeout_s: float | None = None,
        max_response_bytes: int | None = None,
    ) -> SafeResponse:
        verified = verify_inbound(parse_whsec(self.secret), content, headers)
        self.requests.append({"url": url, "headers": dict(headers), "payload": verified.payload})
        if self.mode == "timeout":
            raise SafeHttpError(SafeHttpFailure.TIMEOUT_AFTER_SEND, possibly_delivered=True)
        if self.mode == "refused":
            raise SafeHttpError(SafeHttpFailure.CONNECT_FAILED)
        if self.mode == "http_500":
            return self._response(500, b'{"error":"synthetic"}')
        challenge = verified.payload["challenge"] if self.mode == "echo" else "not-the-challenge"
        return self._response(200, json.dumps({"challenge": challenge}).encode())

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        max_response_bytes: int | None = None,
    ) -> SafeResponse:  # pragma: no cover - the events code never GETs
        raise SafeHttpError(SafeHttpFailure.DESTINATION_REJECTED)

    @staticmethod
    def _response(status: int, body: bytes) -> SafeResponse:
        return SafeResponse(
            status_code=status,
            headers={"content-type": "application/json"},
            body=body,
            truncated=False,
            elapsed=timedelta(milliseconds=5),
            pinned_ip="203.0.113.10",
        )


# --------------------------------------------------------------------------------------------
# JSON-RPC client over the in-process transport
# --------------------------------------------------------------------------------------------


def envelope(method: str, params: Mapping[str, Any] | None = None, *, request_id: int = 1) -> dict[str, Any]:
    body = dict(params or {})
    body["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "synthetic-test-client", "version": "0"},
    }
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": body}


def rpc_headers(method: str, name: str | None = None, token: str | None = None) -> dict[str, str]:
    headers = {
        "MCP-Protocol-Version": PROTOCOL,
        "Mcp-Method": method,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if name is not None:
        headers["Mcp-Name"] = name
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


@dataclass
class McpClient:
    """Raw JSON-RPC over HTTP against one running `McpApp`."""

    app: McpApp
    http: httpx2.AsyncClient
    metrics: AppMetrics

    async def rpc(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        token: str | None = None,
        name: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        merged = {**rpc_headers(method, name, token), **(headers or {})}
        return await self.http.post("/mcp", json=envelope(method, params), headers=merged)

    async def result(self, method: str, params: Mapping[str, Any] | None = None, *, token: str) -> Any:
        response = await self.rpc(method, params, token=token)
        assert response.status_code == 200, response.text
        body = response.json()
        assert "error" not in body, body
        return body["result"]

    async def error(
        self, method: str, params: Mapping[str, Any] | None = None, *, token: str
    ) -> dict[str, Any]:
        response = await self.rpc(method, params, token=token)
        body = response.json()
        assert "error" in body, body
        error: dict[str, Any] = body["error"]
        error["http_status"] = response.status_code
        return error

    async def tools(self, token: str) -> list[dict[str, Any]]:
        result = await self.result("tools/list", token=token)
        tools: list[dict[str, Any]] = result["tools"]
        return tools

    async def call(
        self, tool: str, arguments: Mapping[str, Any] | None = None, *, token: str
    ) -> dict[str, Any]:
        response = await self.rpc(
            "tools/call", {"name": tool, "arguments": dict(arguments or {})}, token=token, name=tool
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "error" not in body, body
        result: dict[str, Any] = body["result"]
        return result

    async def ok(
        self, tool: str, arguments: Mapping[str, Any] | None = None, *, token: str
    ) -> dict[str, Any]:
        """A successful call: returns the structured envelope (text content must match it)."""
        result = await self.call(tool, arguments, token=token)
        assert result["isError"] is False, result
        structured: dict[str, Any] = result["structuredContent"]
        assert json.loads(result["content"][0]["text"]) == structured
        return structured

    async def fails(
        self, tool: str, arguments: Mapping[str, Any] | None = None, *, token: str, code: str
    ) -> dict[str, Any]:
        """A tool error with ``code``: returns the validated ``ToolError`` payload."""
        result = await self.call(tool, arguments, token=token)
        assert result["isError"] is True, result
        payload: dict[str, Any] = result["structuredContent"]
        assert json.loads(result["content"][0]["text"]) == payload
        errors = list(ERROR_VALIDATOR.iter_errors(payload))
        assert not errors, [e.message for e in errors]
        assert payload["code"] == code, payload
        assert payload["correlation_id"], payload
        return payload


ERROR_VALIDATOR = Draft202012Validator(tool_error_schema())


@asynccontextmanager
async def running(
    app: McpApp, *, client: tuple[str, int] = ("127.0.0.1", 123), base_url: str = BASE_URL
) -> AsyncIterator[httpx2.AsyncClient]:
    """The app with its lifespan entered, behind an in-process transport."""
    async with lifespan_in_task(app):
        transport = httpx2.ASGITransport(app=app, client=client)
        async with httpx2.AsyncClient(transport=transport, base_url=base_url) as http:
            yield http


@asynccontextmanager
async def lifespan_in_task(app: McpApp) -> AsyncIterator[None]:
    """Enter and exit the app lifespan in ONE owner task.

    The SDK session manager runs an anyio task group, which must be exited by the task that
    entered it; async-generator fixtures set up and tear down in different tasks.
    """
    started = asyncio.Event()
    stop = asyncio.Event()
    failures: list[BaseException] = []

    async def owner() -> None:
        try:
            async with app.lifespan():
                started.set()
                await stop.wait()
        except BaseException as exc:
            failures.append(exc)
            started.set()
            raise

    task = asyncio.create_task(owner())
    await started.wait()
    if failures:
        await asyncio.gather(task, return_exceptions=True)
        raise failures[0]
    try:
        yield
    finally:
        stop.set()
        await task


def build_app(
    settings: Settings,
    db: Database,
    keys: SigningKeys,
    *,
    metrics: AppMetrics | None = None,
    **kwargs: Any,
) -> McpApp:
    kwargs.setdefault("jwks", keys.jwks)
    return build_mcp(settings, db, metrics=metrics or AppMetrics(process_metrics=False), **kwargs)


@asynccontextmanager
async def mcp_client(
    settings: Settings, db: Database, keys: SigningKeys, **kwargs: Any
) -> AsyncIterator[McpClient]:
    metrics = kwargs.pop("metrics", None) or AppMetrics(process_metrics=False)
    client_addr = kwargs.pop("client", ("127.0.0.1", 123))
    base_url = kwargs.pop("base_url", BASE_URL)
    app = build_app(settings, db, keys, metrics=metrics, **kwargs)
    async with running(app, client=client_addr, base_url=base_url) as http:
        yield McpClient(app=app, http=http, metrics=metrics)


# --------------------------------------------------------------------------------------------
# Database-backed fixtures
# --------------------------------------------------------------------------------------------


@pytest.fixture
def seed(db_conn: psycopg.Connection) -> Seed:
    return Seed(db_conn)


@pytest.fixture
async def db(db_url: str) -> AsyncIterator[Database]:
    database = Database(db_url, set_role="suv_backend", min_size=1, max_size=8, lock_timeout_ms=5_000)
    await database.open()
    try:
        yield database
    finally:
        await database.close()


@dataclass
class Users:
    owner: UUID
    reviewer: UUID
    viewer: UUID
    second_reviewer: UUID
    stranger: UUID


def add_members(seed: Seed, workspace_id: UUID) -> Users:
    users = Users(
        owner=seed.user(),
        reviewer=seed.user(),
        viewer=seed.user(),
        second_reviewer=seed.user(),
        stranger=seed.user(),
    )
    seed.membership(workspace_id, users.owner, "owner")
    seed.membership(workspace_id, users.reviewer, "reviewer")
    seed.membership(workspace_id, users.viewer, "viewer")
    seed.membership(workspace_id, users.second_reviewer, "reviewer")
    return users


@dataclass
class DataHarness:
    """The SYNTHETIC read-query dataset, its members and a running MCP client."""

    client: McpClient
    tokens: TokenFactory
    users: Users
    data: SeededWorkspace
    seed: Seed

    def token(self, user: UUID, role: Role, **options: Any) -> str:
        return self.tokens.for_role(user, role, **options)

    @property
    def owner(self) -> str:
        return self.token(self.users.owner, Role.OWNER)

    @property
    def reviewer(self) -> str:
        return self.token(self.users.reviewer, Role.REVIEWER)

    @property
    def second_reviewer(self) -> str:
        return self.token(self.users.second_reviewer, Role.REVIEWER)

    @property
    def viewer(self) -> str:
        return self.token(self.users.viewer, Role.VIEWER)


@pytest.fixture
async def data_mcp(
    db: Database, seed: Seed, keys: SigningKeys, tokens: TokenFactory
) -> AsyncIterator[DataHarness]:
    data = await seed_workspace(db, seed, "MCP")
    users = add_members(seed, data.workspace_id)
    async with mcp_client(make_settings(), db, keys) as client:
        yield DataHarness(client=client, tokens=tokens, users=users, data=data, seed=seed)
