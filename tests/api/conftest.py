"""Fixtures for the dashboard API tests (SYNTHETIC data, locally generated keys, no network).

- Signing keys are generated locally per session (ES256 P-256 and RS256 2048-bit) and published
  through an in-memory JWKS (``StaticJwks``); Supabase is never called.
- The app runs in-process over ``httpx.ASGITransport`` with its lifespan entered; database tests
  use the migrated test database through ``Database(db_url, set_role="suv_backend")`` so RLS and
  grants apply as in production. Arrangement uses the superuser ``Seed`` and the read-query
  dataset builders (read-only reuse).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from pydantic import SecretStr
from tests.integration.db.helpers import Seed
from tests.integration.read_queries.dataset import CURSOR_SECRET, SeededWorkspace, seed_workspace

from suv_deals.api.app import create_app
from suv_deals.api.auth import StaticJwks
from suv_deals.api.deps import ApiOptions
from suv_deals.api.middleware import PrincipalRateLimiter
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence.database import Database
from suv_deals.settings import Settings

SUPABASE_URL = "https://synthetic-project.supabase.example"
ISSUER = f"{SUPABASE_URL}/auth/v1"
BASE_URL = "https://dashboard.synthetic.example"
DASHBOARD_ORIGIN = BASE_URL
ES_KID = "synthetic-es256-key-1"
RS_KID = "synthetic-rs256-key-1"
UNREACHABLE_DB = "postgresql://suv:suv@127.0.0.1:1/synthetic_unreachable"


@dataclass(frozen=True)
class SigningKeys:
    es_private: ec.EllipticCurvePrivateKey
    rs_private: rsa.RSAPrivateKey
    stranger_private: ec.EllipticCurvePrivateKey  # never published
    jwks: dict[str, Any]

    @property
    def rs_public_pem(self) -> bytes:
        return self.rs_private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )


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


@dataclass
class TokenFactory:
    """Mints SYNTHETIC Supabase-shaped access tokens with the local keys."""

    keys: SigningKeys

    def claims(
        self,
        user_id: UUID | str,
        *,
        expires_in: int = 3600,
        issued_offset: int = 0,
        extra: Mapping[str, Any] | None = None,
        drop: Sequence[str] = (),
    ) -> dict[str, Any]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": "authenticated",
            "sub": str(user_id),
            "role": "authenticated",
            "iat": now + issued_offset,
            "exp": now + issued_offset + expires_in,
            "aal": "aal1",
            "session_id": str(uuid.uuid4()),
            "email": "synthetic-user@example.invalid",
            "phone": "",
            "is_anonymous": False,
        }
        claims.update(extra or {})
        for name in drop:
            claims.pop(name, None)
        return claims

    def mint(
        self,
        user_id: UUID | str,
        *,
        alg: str = "ES256",
        kid: str | None = None,
        key: Any = None,
        headers: Mapping[str, Any] | None = None,
        **claim_options: Any,
    ) -> str:
        payload = self.claims(user_id, **claim_options)
        signing_key = key
        if signing_key is None:
            signing_key = self.keys.es_private if alg == "ES256" else self.keys.rs_private
        header: dict[str, Any] = {"kid": kid or (ES_KID if alg == "ES256" else RS_KID)}
        header.update(headers or {})
        return jwt.encode(payload, signing_key, algorithm=alg, headers=header)

    def unsigned(self, user_id: UUID | str, *, alg: str = "none", **claim_options: Any) -> str:
        header = {"alg": alg, "typ": "JWT"}
        payload = self.claims(user_id, **claim_options)
        return f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(payload).encode())}."

    def hmac_signed(
        self, user_id: UUID | str, secret: bytes, *, kid: str | None = None, **claim_options: Any
    ) -> str:
        """An HS256 token signed with ``secret`` (for example the RSA public key PEM)."""
        header: dict[str, Any] = {"alg": "HS256", "typ": "JWT"}
        if kid is not None:
            header["kid"] = kid
        payload = self.claims(user_id, **claim_options)
        signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(payload).encode())}"
        signature = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
        return f"{signing_input}.{b64url(signature)}"


@pytest.fixture(scope="session")
def tokens(keys: SigningKeys) -> TokenFactory:
    return TokenFactory(keys)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "app_base_url": BASE_URL,
        "supabase_url": SUPABASE_URL,
        "build_id": "synthetic-api.1",
        "mcp_cursor_signing_secret": SecretStr(CURSOR_SECRET.decode()),
        "event_bridge_enabled": False,
        "allow_external_notifications": False,
        "source_network_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@asynccontextmanager
async def running_client(app: Any, base_url: str = BASE_URL) -> AsyncIterator[httpx.AsyncClient]:
    """The app with its lifespan entered, behind an in-process ASGI transport."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            yield client


def build_test_app(
    settings: Settings,
    keys: SigningKeys,
    db: Database,
    *,
    options: ApiOptions | None = None,
    limiter: PrincipalRateLimiter | None = None,
    metrics: AppMetrics | None = None,
    **kwargs: Any,
) -> Any:
    return create_app(
        settings,
        db=db,
        jwks=StaticJwks(keys.jwks),
        metrics=metrics or AppMetrics(process_metrics=False),
        limiter=limiter,
        options=options,
        **kwargs,
    )


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
    extra: dict[str, UUID] = field(default_factory=dict)


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
class ApiHarness:
    """An in-process client plus helpers that authenticate as one synthetic user."""

    client: httpx.AsyncClient
    tokens: TokenFactory
    users: Users
    workspace_id: UUID
    metrics: AppMetrics

    def auth(
        self, user: UUID, *, workspace: UUID | str | None = None, **token_options: Any
    ) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.tokens.mint(user, **token_options)}"}
        if workspace is not None:
            headers["X-Workspace-Id"] = str(workspace)
        return headers

    async def get(self, path: str, user: UUID, **kwargs: Any) -> httpx.Response:
        headers = {**self.auth(user), **kwargs.pop("headers", {})}
        return await self.client.get(path, headers=headers, **kwargs)

    async def post(self, path: str, user: UUID, body: Mapping[str, Any], **kwargs: Any) -> httpx.Response:
        headers = {**self.auth(user), **kwargs.pop("headers", {})}
        return await self.client.post(path, headers=headers, json=dict(body), **kwargs)


@dataclass
class DataHarness(ApiHarness):
    data: SeededWorkspace = field(default=None)  # type: ignore[assignment]


@pytest.fixture
async def data_api(
    db: Database, seed: Seed, settings: Settings, keys: SigningKeys, tokens: TokenFactory
) -> AsyncIterator[DataHarness]:
    """The full SYNTHETIC read-query dataset plus owner/reviewer/viewer members."""
    data = await seed_workspace(db, seed, "API")
    users = add_members(seed, data.workspace_id)
    metrics = AppMetrics(process_metrics=False)
    app = build_test_app(settings, keys, db, metrics=metrics)
    async with running_client(app) as client:
        yield DataHarness(
            client=client,
            tokens=tokens,
            users=users,
            workspace_id=data.workspace_id,
            metrics=metrics,
            data=data,
        )


@pytest.fixture
async def bare_api(
    db: Database, seed: Seed, settings: Settings, keys: SigningKeys, tokens: TokenFactory
) -> AsyncIterator[ApiHarness]:
    """One empty workspace with owner/reviewer/viewer members (fast; for auth tests)."""
    workspace_id = seed.workspace("API auth")
    users = add_members(seed, workspace_id)
    metrics = AppMetrics(process_metrics=False)
    app = build_test_app(settings, keys, db, metrics=metrics)
    async with running_client(app) as client:
        yield ApiHarness(
            client=client, tokens=tokens, users=users, workspace_id=workspace_id, metrics=metrics
        )


def error_of(response: httpx.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    assert set(body) == {"schema_version", "request_id", "as_of", "error"}, body
    assert body["error"]["correlation_id"] == body["request_id"] == response.headers["x-request-id"]
    error: dict[str, Any] = body["error"]
    return error


def comparable_error(response: httpx.Response) -> dict[str, Any]:
    """The error body without the per-request fields (request id, time, correlation id)."""
    body = dict(response.json())
    error = dict(body.pop("error"))
    body.pop("request_id")
    body.pop("as_of")
    error.pop("correlation_id")
    return {**body, "error": error, "status": response.status_code}
