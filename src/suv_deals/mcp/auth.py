"""MCP authentication: token verifiers, principal mapping and API credentials (spec 20, 24, 31).

The MCP endpoint accepts a bearer token from the ``Authorization`` header only. The SDK's
``BearerAuthBackend`` reads that header (never a query string, cookie, body or tool argument),
calls one of the verifiers below and, with ``AuthSettings.validate_token_resource``, refuses a
token whose ``AccessToken.resource`` is not this server's ``MCP_PUBLIC_URL``. Tokens are never
passed through to Supabase, Slack or any other service. One mode per deployment
(``MCP_AUTH_MODE``):

``oauth`` (`OAuthJwtVerifier`, the preferred production mode)
    A JWT access token from the configured authorization server: signature through the JWKS at
    ``MCP_OAUTH_JWKS_URL`` (``ES256``/``RS256`` only; the JWK's algorithm must equal the header's;
    ``none``, ``HS*`` and ``crit`` headers are refused before any key lookup), issuer
    ``MCP_OAUTH_ISSUER``, audience ``MCP_OAUTH_AUDIENCE`` or the resource ``MCP_PUBLIC_URL``
    (RFC 8707; the dashboard's own issuer + audience pair is a configuration error, so a
    dashboard session token is never an MCP token), ``exp``/``nbf``/``iat`` with a small leeway
    and a bounded lifetime. Scopes come from ``scope`` (space separated) or ``scp`` and are
    mapped to `Scope` values; unknown scope strings are ignored. Principal mapping: a
    canonical-UUID ``sub`` is a user and is mapped to an *active* membership in an *active*
    workspace (``persistence.workspaces``); with several memberships the signed
    ``workspace_id`` claim must name one of them. A machine client is
    mapped through an explicit ``client_principals`` table keyed by the ``client_id``/``azp``
    claim, and only when ``sub`` is exactly the configured client subject (a user token issued
    through that client is never promoted to the machine principal). Nothing in a request can
    choose the workspace.

``static_bearer`` (`CredentialVerifier`)
    An opaque ``suvmcp_<64 hex>`` token looked up by its SHA-256 hash in ``ops.api_credentials``
    (RLS ``credential_lookup`` through the ``app.credential_hash`` GUC): unrevoked, unexpired,
    of kind ``static_bearer``, with the stored workspace, principal, role and scopes. A user
    credential never outlives the user's active membership (or role). This is a scoped private
    credential, **not** OAuth compliance, and no OAuth discovery is advertised for it.

``dev_local`` (`CredentialVerifier`)
    Like ``static_bearer`` but for ``suvdev_`` credentials of kind ``dev_local``, and only when
    ``APP_ENV`` is ``development``/``test`` **and** the MCP URL is a loopback address
    (`dev_local_problem`); the server additionally refuses non-loopback clients.

Effective scopes are always ``token scopes ∩ ROLE_SCOPES[role] ∩ MCP_SCOPES``: a credential
narrows, never widens, the member role, and ``config:admin`` and ``mail:ingest`` are never
effective on MCP. Every rejection is the same ``401`` (the reason is only a metric label and a
log field); an unreachable JWKS endpoint or database is ``DependencyUnavailable`` (``503``),
never a ``401``.

`issue_api_credential` creates a credential for the CLI: it returns the token exactly once and
stores only its hash. `revoke_api_credential` revokes one immediately.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from urllib.parse import urlsplit
from uuid import UUID

import anyio.to_thread
import jwt
from jwt import PyJWK, PyJWKClient
from jwt.exceptions import (
    ExpiredSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    PyJWKClientConnectionError,
    PyJWKError,
    PyJWTError,
)
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from pydantic import BaseModel, ConfigDict, SecretStr

from suv_deals.api.auth import SigningKeyResolver, StaticJwks, supabase_issuer
from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ROLE_SCOPES, ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.errors import AppError, DependencyUnavailable, Forbidden, NotFound, ValidationFailed
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence import audit, workspaces
from suv_deals.persistence.database import Conn, Database, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.settings import Settings

logger = logging.getLogger("suv_deals.mcp.auth")

AuthMode = Literal["oauth", "static_bearer", "dev_local"]
CredentialKind = Literal["static_bearer", "dev_local"]
McpPrincipalKind = Literal["user", "mcp_client"]
DenialReason = Literal[
    "invalid_token", "expired_token", "wrong_issuer", "wrong_audience", "not_member", "revoked"
]

#: Scopes that can be effective on the MCP surface (config:admin and mail:ingest never are).
MCP_SCOPES: Final = frozenset(s for s in Scope if s not in (Scope.CONFIG_ADMIN, Scope.MAIL_INGEST))
#: Scopes advertised in protected-resource metadata and challenges (the spec 20 MCP scopes).
ADVERTISED_SCOPES: Final[tuple[Scope, ...]] = (
    Scope.DEALS_READ,
    Scope.REVIEWS_READ,
    Scope.REVIEWS_WRITE,
    Scope.EVENTS_SUBSCRIBE,
    Scope.RECHECKS_REQUEST,
    Scope.NOTES_WRITE,
    Scope.SOURCES_PAUSE,
)
JWT_ALGORITHMS: Final = ("ES256", "RS256")
DEFAULT_LEEWAY: Final = timedelta(seconds=30)
MAX_LEEWAY: Final = timedelta(minutes=2)
DEFAULT_MAX_TOKEN_LIFETIME: Final = timedelta(days=7)
MAX_TOKEN_LENGTH: Final = 8192
JWKS_CACHE_SECONDS: Final = 600
JWKS_REFRESH_COOLDOWN_SECONDS: Final = 30
JWKS_TIMEOUT_SECONDS: Final = 5.0
DEFAULT_WORKSPACE_CLAIM: Final = "workspace_id"
REQUIRED_JWT_CLAIMS: Final = ("exp", "iat", "iss", "aud", "sub")
TOKEN_PREFIXES: Final[Mapping[CredentialKind, str]] = {"static_bearer": "suvmcp", "dev_local": "suvdev"}
DEFAULT_CREDENTIAL_LIFETIME: Final = timedelta(days=90)
MAX_CREDENTIAL_LIFETIME: Final = timedelta(days=365)
CREDENTIAL_TOUCH_INTERVAL: Final = timedelta(minutes=1)
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})

_COMPACT_JWS: Final = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")
_CREDENTIAL_TOKEN: Final = re.compile(r"^(suvmcp|suvdev)_([0-9a-f]{64})$")
_UUID_RE: Final = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_CLIENT_ID_RE: Final = re.compile(r"^[\x21-\x7e]{1,200}$")
_LABEL_CONTROL_RE: Final = re.compile("[\\x00-\\x1f\\x7f\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069]")
_ROLE_RANK: Final[Mapping[Role, int]] = {Role.VIEWER: 0, Role.REVIEWER: 1, Role.OWNER: 2}
_KIND_BY_PREFIX: Final[Mapping[str, CredentialKind]] = {v: k for k, v in TOKEN_PREFIXES.items()}


class McpAuthConfigError(ValueError):
    """The configured MCP authentication mode cannot work. ``problems`` names settings only."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("MCP authentication is not configured: " + "; ".join(self.problems))


class _Rejected(Exception):
    """Internal: the token is not acceptable (always rendered as the same 401)."""

    def __init__(self, reason: DenialReason) -> None:
        super().__init__(reason)
        self.reason: DenialReason = reason


# --------------------------------------------------------------------------------------------
# Principal and access token
# --------------------------------------------------------------------------------------------


class McpPrincipal(BaseModel):
    """The verified principal behind one MCP request (no token material)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: UUID
    principal_id: UUID
    principal_kind: McpPrincipalKind
    role: Role
    scopes: frozenset[Scope]
    auth_mode: AuthMode
    client_id: str | None = None
    credential_id: UUID | None = None

    def actor(self, request_id: str) -> ActorContext:
        """The request's `ActorContext` (the workspace was resolved server-side)."""
        return ActorContext(
            workspace_id=self.workspace_id,
            principal_id=self.principal_id,
            principal_kind=self.principal_kind,
            role=self.role,
            scopes=self.scopes,
            request_id=request_id,
            client_id=self.client_id,
        )


class McpAccessToken(AccessToken):
    """The SDK ``AccessToken`` plus our resolved principal.

    ``token`` holds a SHA-256 fingerprint, never the presented token.
    """

    principal: McpPrincipal


def current_principal() -> McpPrincipal | None:
    """The verified principal of the current MCP request (``None`` when unauthenticated)."""
    token = get_access_token()
    return token.principal if isinstance(token, McpAccessToken) else None


def hash_token(token: str) -> str:
    """SHA-256 hex of a presented credential (what ``ops.api_credentials.token_hash`` stores)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def effective_scopes(granted: Iterable[Scope], *roles: Role) -> frozenset[Scope]:
    """``granted ∩ ROLE_SCOPES[role] (for every role) ∩ MCP_SCOPES``."""
    scopes = frozenset(granted) & MCP_SCOPES
    for role in roles:
        scopes &= ROLE_SCOPES[role]
    return scopes


def parse_scope_claim(claims: Mapping[str, Any]) -> frozenset[Scope]:
    """Scopes from ``scope`` (space-separated string) or ``scp`` (list or string); unknown strings
    are ignored, never errors."""
    raw = claims.get("scope", claims.get("scp"))
    values: list[object]
    if isinstance(raw, str):
        values = list(raw.split())
    elif isinstance(raw, list):
        values = list(raw)
    else:
        return frozenset()
    known = {s.value: s for s in Scope}
    return frozenset(known[v] for v in values[:64] if isinstance(v, str) and v in known)


def _token(
    principal: McpPrincipal, *, raw: str, expires_at: int | None, resource: str | None
) -> McpAccessToken:
    return McpAccessToken(
        token="sha256:" + hash_token(raw),
        client_id=principal.client_id or f"{principal.auth_mode}:{principal.principal_kind}",
        scopes=[s.value for s in Scope if s in principal.scopes],
        expires_at=expires_at,
        resource=resource,
        subject=str(principal.principal_id),
        claims=None,
        principal=principal,
    )


def _unavailable(error: AppError) -> DependencyUnavailable:
    """Any failure of the identity store while authenticating (lock or statement timeout, an
    aborted transaction, an outage) is a retryable ``503``: never a ``401`` that would make the
    client discard a valid token, and never a ``500`` with a trace."""
    if isinstance(error, DependencyUnavailable):
        return error
    logger.warning("mcp authentication store failed", extra={"code": error.code.value})
    return DependencyUnavailable("The authentication store is temporarily unavailable")


class _DenialRecorder:
    def __init__(self, metrics: AppMetrics | None) -> None:
        self._metrics = metrics

    def __call__(self, reason: DenialReason) -> None:
        if self._metrics is not None:
            self._metrics.record_auth_denial("mcp", reason)
        logger.info("mcp token rejected", extra={"reason": reason})


# --------------------------------------------------------------------------------------------
# OAuth resource-server mode
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClientPrincipal:
    """An OAuth machine client mapped to an explicit workspace, principal and role.

    Only a token whose ``sub`` is exactly ``subject`` (default: the client id itself, as RFC 9068
    client-credentials tokens carry) is mapped; a user token issued through the same client
    (another ``sub``) is never promoted to the machine principal.
    """

    workspace_id: UUID
    principal_id: UUID
    role: Role
    scopes: frozenset[Scope]
    subject: str | None = None


def _url_problem(name: str, value: str | None, settings: Settings, *, required: bool = True) -> str | None:
    if not value:
        return f"{name} is required" if required else None
    try:
        parts = urlsplit(value.strip())
        _ = parts.port  # a malformed port is a configuration problem, never a start-up crash
    except ValueError:
        return f"{name} must be a plain http(s) URL"
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or "*" in parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        return f"{name} must be a plain http(s) URL"
    if parts.scheme == "http" and not (
        settings.app_env in ("development", "test") and parts.hostname in LOOPBACK_HOSTS
    ):
        return f"{name} must use https"
    return None


def _audience_problem(settings: Settings) -> str | None:
    """Refuse the dashboard's (issuer, audience) pair for MCP access tokens.

    RFC 8707 audience binding is only meaningful when no other token shares it: a dashboard
    session token (``<SUPABASE_URL>/auth/v1`` issuer with ``SUPABASE_JWT_AUDIENCE``) must never
    be accepted as an MCP access token (no token passthrough between the two surfaces).
    """
    audience = (settings.mcp_oauth_audience or "").strip()
    if not audience:
        return None
    if settings.supabase_url and audience == settings.supabase_jwt_audience.strip():
        try:
            dashboard_issuer = supabase_issuer(settings.supabase_url)
        except ValueError:
            return None
        if dashboard_issuer.rstrip("/") == (settings.mcp_oauth_issuer or "").strip().rstrip("/"):
            return "MCP_OAUTH_AUDIENCE must not be the dashboard session audience (SUPABASE_JWT_AUDIENCE)"
    return None


class OAuthJwtVerifier:
    """Verifies OAuth JWT access tokens for this resource server (see module docstring)."""

    def __init__(
        self,
        *,
        db: Database,
        issuer: str,
        audiences: Sequence[str],
        resource: str,
        resolver: SigningKeyResolver,
        client_principals: Mapping[str, ClientPrincipal] | None = None,
        workspace_claim: str = DEFAULT_WORKSPACE_CLAIM,
        leeway: timedelta = DEFAULT_LEEWAY,
        max_lifetime: timedelta = DEFAULT_MAX_TOKEN_LIFETIME,
        metrics: AppMetrics | None = None,
    ) -> None:
        if not issuer or not resource or not [a for a in audiences if a]:
            raise ValueError("issuer, audiences and resource are required")
        if not timedelta(0) <= leeway <= MAX_LEEWAY:
            raise ValueError("leeway must be between 0 and 2 minutes")
        self.issuer = issuer
        self.audiences = tuple(dict.fromkeys(a for a in audiences if a))
        self.resource = resource
        self._db = db
        self._resolver = resolver
        self._clients = dict(client_principals or {})
        self._workspace_claim = workspace_claim
        self._leeway = leeway
        self._max_lifetime = max_lifetime
        self._deny = _DenialRecorder(metrics)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        db: Database,
        *,
        resolver: SigningKeyResolver | Mapping[str, Any] | None = None,
        client_principals: Mapping[str, ClientPrincipal] | None = None,
        metrics: AppMetrics | None = None,
    ) -> OAuthJwtVerifier:
        """The verifier for ``MCP_OAUTH_*`` and ``MCP_PUBLIC_URL`` (raises `McpAuthConfigError`).

        ``resolver``: a key resolver or JWK-set document (tests, pinned keys); default: the
        cached ``PyJWKClient`` for ``MCP_OAUTH_JWKS_URL``.
        """
        problems = [
            p
            for p in (
                _url_problem("MCP_OAUTH_ISSUER", settings.mcp_oauth_issuer, settings),
                _url_problem("MCP_PUBLIC_URL", settings.mcp_public_url, settings),
                _url_problem(
                    "MCP_OAUTH_JWKS_URL", settings.mcp_oauth_jwks_url, settings, required=resolver is None
                ),
                _audience_problem(settings),
            )
            if p is not None
        ]
        if problems:
            raise McpAuthConfigError(problems)
        assert settings.mcp_oauth_issuer is not None and settings.mcp_public_url is not None
        if isinstance(resolver, Mapping):
            try:
                key_resolver: SigningKeyResolver = StaticJwks(resolver)
            except (PyJWKError, PyJWTError, KeyError, TypeError, ValueError):
                raise McpAuthConfigError(["the pinned OAuth signing keys are invalid"]) from None
        elif resolver is not None:
            key_resolver = resolver
        else:
            assert settings.mcp_oauth_jwks_url is not None
            key_resolver = PyJWKClient(
                settings.mcp_oauth_jwks_url.strip(),
                cache_jwk_set=True,
                lifespan=JWKS_CACHE_SECONDS,
                timeout=JWKS_TIMEOUT_SECONDS,
                cooldown_duration=JWKS_REFRESH_COOLDOWN_SECONDS,
                headers={"User-Agent": "suv-deals-mcp/jwks"},
            )
        resource = settings.mcp_public_url.strip()
        return cls(
            db=db,
            issuer=settings.mcp_oauth_issuer.strip(),
            audiences=[a for a in ((settings.mcp_oauth_audience or "").strip(), resource) if a],
            resource=resource,
            resolver=key_resolver,
            client_principals=client_principals,
            metrics=metrics,
        )

    async def verify_token(self, token: str) -> McpAccessToken | None:
        """The SDK ``TokenVerifier`` hook: an access token, ``None`` (401) or `DependencyUnavailable`."""
        try:
            return await self._verify(token)
        except _Rejected as exc:
            self._deny(exc.reason)
            return None
        except AppError as exc:
            raise _unavailable(exc) from None

    async def _key(self, token: str, alg: str) -> PyJWK:
        try:
            key = await anyio.to_thread.run_sync(self._resolver.get_signing_key_from_jwt, token)
        except PyJWKClientConnectionError:
            raise DependencyUnavailable("The authorization server's signing keys are unavailable") from None
        except (PyJWKError, PyJWTError):
            raise _Rejected("invalid_token") from None
        if not isinstance(key, PyJWK) or key.algorithm_name != alg:
            raise _Rejected("invalid_token")
        return key

    async def _verify(self, token: str) -> McpAccessToken:
        if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH or not _COMPACT_JWS.fullmatch(token):
            raise _Rejected("invalid_token")
        try:
            header = jwt.get_unverified_header(token)
        except PyJWTError:
            raise _Rejected("invalid_token") from None
        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in JWT_ALGORITHMS or "crit" in header:
            raise _Rejected("invalid_token")
        if not isinstance(header.get("kid"), str):
            raise _Rejected("invalid_token")
        key = await self._key(token, alg)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience=list(self.audiences),
                issuer=self.issuer,
                leeway=self._leeway,
                options={
                    "require": list(REQUIRED_JWT_CLAIMS),
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "enforce_minimum_key_length": True,
                },
            )
        except ExpiredSignatureError:
            raise _Rejected("expired_token") from None
        except InvalidIssuerError:
            raise _Rejected("wrong_issuer") from None
        except InvalidAudienceError:
            raise _Rejected("wrong_audience") from None
        except PyJWTError:
            raise _Rejected("invalid_token") from None
        expires_at = self._lifetime(claims)
        principal = await self._principal(claims)
        return _token(principal, raw=token, expires_at=expires_at, resource=self.resource)

    def _lifetime(self, claims: Mapping[str, Any]) -> int:
        issued, expires = claims.get("iat"), claims.get("exp")
        if isinstance(issued, bool) or isinstance(expires, bool):
            raise _Rejected("invalid_token")
        if not isinstance(issued, int | float) or not isinstance(expires, int | float):
            raise _Rejected("invalid_token")
        if expires <= issued or expires - issued > self._max_lifetime.total_seconds():
            raise _Rejected("invalid_token")
        return int(expires)

    async def _principal(self, claims: Mapping[str, Any]) -> McpPrincipal:
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise _Rejected("invalid_token")
        granted = parse_scope_claim(claims)
        client = claims.get("client_id", claims.get("azp"))
        client_id = client if isinstance(client, str) and _CLIENT_ID_RE.fullmatch(client) else None
        mapped = self._clients.get(client_id) if client_id is not None else None
        if mapped is not None and sub == (mapped.subject or client_id):
            return McpPrincipal(
                workspace_id=mapped.workspace_id,
                principal_id=mapped.principal_id,
                principal_kind="mcp_client",
                role=mapped.role,
                scopes=effective_scopes(granted & mapped.scopes, mapped.role),
                auth_mode="oauth",
                client_id=client_id,
            )
        if not _UUID_RE.fullmatch(sub):
            raise _Rejected("not_member")
        user_id = UUID(sub)
        memberships = await workspaces.resolve_memberships_for_user(self._db, user_id)
        active = [m for m in memberships if m.active and m.workspace_active]
        requested = claims.get(self._workspace_claim)
        if requested is not None:
            if not isinstance(requested, str) or not _UUID_RE.fullmatch(requested):
                raise _Rejected("not_member")
            wanted = UUID(requested)
            chosen = next((m for m in active if m.workspace_id == wanted), None)
        else:
            chosen = active[0] if len(active) == 1 else None
        if chosen is None:
            raise _Rejected("not_member")
        return McpPrincipal(
            workspace_id=chosen.workspace_id,
            principal_id=user_id,
            principal_kind="user",
            role=chosen.role,
            scopes=effective_scopes(granted, chosen.role),
            auth_mode="oauth",
            client_id=client_id,
        )


# --------------------------------------------------------------------------------------------
# Static bearer and local development credentials
# --------------------------------------------------------------------------------------------

_CREDENTIAL_SQL: Final = (
    "select id, workspace_id, principal_id, principal_kind, role, credential_kind, scopes, expires_at,"
    " revoked_at, last_used_at, clock_timestamp() as now from ops.api_credentials"
    " where token_hash = %(hash)s"
)
_WORKSPACE_ACTIVE_SQL: Final = "select active from app.workspaces where id = %(ws)s"
_TOUCH_SQL: Final = (
    "update ops.api_credentials set last_used_at = clock_timestamp()"
    " where workspace_id = %(ws)s and id = %(id)s"
    " and (last_used_at is null or last_used_at < clock_timestamp() - %(interval)s::interval)"
)


def _lower_role(a: Role, b: Role) -> Role:
    return a if _ROLE_RANK[a] <= _ROLE_RANK[b] else b


class CredentialVerifier:
    """Verifies opaque ``ops.api_credentials`` tokens of the allowed kinds (module docstring)."""

    def __init__(
        self,
        *,
        db: Database,
        kinds: Iterable[CredentialKind],
        resource: str | None = None,
        metrics: AppMetrics | None = None,
        touch_interval: timedelta = CREDENTIAL_TOUCH_INTERVAL,
    ) -> None:
        self.kinds: frozenset[CredentialKind] = frozenset(kinds)
        if not self.kinds:
            raise ValueError("at least one credential kind is required")
        self.resource = resource
        self._db = db
        self._touch_interval = touch_interval
        self._deny = _DenialRecorder(metrics)

    @property
    def auth_mode(self) -> AuthMode:
        return "static_bearer" if "static_bearer" in self.kinds else "dev_local"

    async def verify_token(self, token: str) -> McpAccessToken | None:
        try:
            return await self._verify(token)
        except _Rejected as exc:
            self._deny(exc.reason)
            return None
        except AppError as exc:
            raise _unavailable(exc) from None

    async def _verify(self, token: str) -> McpAccessToken:
        match = _CREDENTIAL_TOKEN.fullmatch(token) if isinstance(token, str) else None
        if match is None:
            raise _Rejected("invalid_token")
        kind = _KIND_BY_PREFIX[match.group(1)]
        if kind not in self.kinds:
            raise _Rejected("invalid_token")
        digest = hash_token(token)
        async with mapped_errors(), self._db.transaction() as conn, mapped_errors():
            await conn.execute("select set_config('app.credential_hash', %s, true)", (digest,))
            row = await fetch_one(conn, _CREDENTIAL_SQL, {"hash": digest})
            if row is None or row["credential_kind"] != kind:
                raise _Rejected("invalid_token")
            if row["revoked_at"] is not None:
                raise _Rejected("revoked")
            expires_at = ensure_utc(row["expires_at"])
            if expires_at <= ensure_utc(row["now"]):
                raise _Rejected("expired_token")
            ws: UUID = row["workspace_id"]
            await conn.execute("select set_config('app.workspace_id', %s, true)", (str(ws),))
            principal = await self._principal(conn, row, kind)
            await conn.execute(_TOUCH_SQL, {"ws": ws, "id": row["id"], "interval": self._touch_interval})
        return _token(principal, raw=token, expires_at=int(expires_at.timestamp()), resource=self.resource)

    async def _principal(self, conn: Conn, row: Mapping[str, Any], kind: CredentialKind) -> McpPrincipal:
        ws: UUID = row["workspace_id"]
        principal_id: UUID = row["principal_id"]
        credential_role = Role(row["role"])
        granted = frozenset(Scope(s) for s in row["scopes"] if s in {x.value for x in Scope})
        mode: AuthMode = "static_bearer" if kind == "static_bearer" else "dev_local"
        if row["principal_kind"] == "user":
            membership = await workspaces.get_membership(conn, ws, principal_id)
            if membership is None or not membership.active or not membership.workspace_active:
                raise _Rejected("not_member")
            role = _lower_role(credential_role, membership.role)
            scopes = effective_scopes(granted, credential_role, membership.role)
            principal_kind: McpPrincipalKind = "user"
        else:
            workspace = await fetch_one(conn, _WORKSPACE_ACTIVE_SQL, {"ws": ws})
            if workspace is None or not workspace["active"]:
                raise _Rejected("not_member")
            role = credential_role
            scopes = effective_scopes(granted, credential_role)
            principal_kind = "mcp_client"
        if not scopes:
            raise _Rejected("invalid_token")  # e.g. a mail-ingest-only worker credential
        return McpPrincipal(
            workspace_id=ws,
            principal_id=principal_id,
            principal_kind=principal_kind,
            role=role,
            scopes=scopes,
            auth_mode=mode,
            client_id=f"credential:{row['id']}",
            credential_id=row["id"],
        )


def dev_local_problem(settings: Settings) -> str | None:
    """Why ``dev_local`` must not run here (``None`` when it may): development/test only, and the
    MCP URL (``MCP_PUBLIC_URL``, else ``APP_BASE_URL``) must be a loopback address."""
    if settings.app_env not in ("development", "test"):
        return "MCP_AUTH_MODE=dev_local is only allowed when APP_ENV is development or test"
    url = (settings.mcp_public_url or settings.app_base_url or "").strip()
    try:
        host = urlsplit(url).hostname if url else None
    except ValueError:  # e.g. a broken IPv6 literal: a configuration problem, never a start-up crash
        host = None
    if host not in LOOPBACK_HOSTS:
        return "MCP_AUTH_MODE=dev_local requires a loopback MCP_PUBLIC_URL/APP_BASE_URL"
    return None


# --------------------------------------------------------------------------------------------
# Credential issuing and revocation (for the CLI)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    """A newly created credential. ``token`` is shown exactly once and never stored."""

    credential_id: UUID
    workspace_id: UUID
    principal_id: UUID
    principal_kind: McpPrincipalKind
    role: Role
    scopes: tuple[Scope, ...]
    kind: CredentialKind
    token_prefix: str
    expires_at: datetime
    token: SecretStr


def _require_credential_admin(actor: ActorContext) -> None:
    """Credential administration: the operator CLI (system actor) or a signed-in owner with
    ``config:admin``; never an MCP client."""
    if actor.principal_kind == "system":
        return
    actor.require(Scope.CONFIG_ADMIN)
    if actor.principal_kind != "user" or actor.role != Role.OWNER:
        raise Forbidden("Only the owner may manage API credentials")


def _label(label: str) -> str:
    text = label.strip() if isinstance(label, str) else ""
    if not 1 <= len(text) <= 120 or _LABEL_CONTROL_RE.search(text):
        raise ValidationFailed("label must be 1-120 printable characters", details={"fields": ["label"]})
    return text


async def issue_api_credential(
    conn: Conn,
    actor: ActorContext,
    *,
    principal_id: UUID,
    principal_kind: McpPrincipalKind,
    role: Role,
    scopes: Iterable[Scope],
    label: str,
    kind: CredentialKind = "static_bearer",
    lifetime: timedelta = DEFAULT_CREDENTIAL_LIFETIME,
) -> IssuedCredential:
    """Create one MCP credential in ``actor``'s workspace and return its token ONCE.

    Only the SHA-256 hash is stored. Scopes must be MCP scopes within the role (``mail:ingest``
    and ``config:admin`` are refused); a user credential needs an active membership whose role
    allows the scopes. Run inside ``unit_of_work(db, actor)``.
    """
    _require_credential_admin(actor)
    wanted = frozenset(Scope(s) for s in scopes)
    role = Role(role)
    if not wanted or not wanted <= MCP_SCOPES or not wanted <= ROLE_SCOPES[role]:
        raise ValidationFailed(
            "scopes must be MCP scopes allowed for the role", details={"fields": ["scopes"]}
        )
    if principal_kind not in ("user", "mcp_client"):
        raise ValidationFailed(
            "principal_kind must be user or mcp_client", details={"fields": ["principal_kind"]}
        )
    if kind not in TOKEN_PREFIXES:
        raise ValidationFailed("unknown credential kind", details={"fields": ["kind"]})
    if not timedelta(0) < lifetime <= MAX_CREDENTIAL_LIFETIME:
        raise ValidationFailed(
            "lifetime must be positive and at most 365 days", details={"fields": ["lifetime"]}
        )
    text = _label(label)
    membership = await workspaces.get_membership(conn, actor.workspace_id, principal_id)
    if principal_kind == "user":
        if membership is None or not membership.active or not wanted <= ROLE_SCOPES[membership.role]:
            raise NotFound("No active membership allows these scopes")
    elif membership is not None:
        # A machine credential must never act under a member's identity (claims, idempotency
        # records, notes and subscriptions are keyed by principal id).
        raise ValidationFailed(
            "principal_id of an mcp_client credential must not be a workspace member",
            details={"fields": ["principal_id"]},
        )
    random_part = secrets.token_hex(32)
    prefix = TOKEN_PREFIXES[kind]
    token = f"{prefix}_{random_part}"
    token_prefix = f"{prefix}_{random_part[:6]}"
    ordered = tuple(s for s in Scope if s in wanted)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into ops.api_credentials (workspace_id, principal_id, principal_kind, role,"
            " credential_kind, token_hash, token_prefix, scopes, label, expires_at, created_by)"
            " values (%(ws)s, %(principal)s, %(kind)s, %(role)s, %(credential_kind)s, %(hash)s,"
            " %(prefix)s, %(scopes)s, %(label)s, clock_timestamp() + %(lifetime)s::interval, %(by)s)"
            " returning id, expires_at",
            {
                "ws": actor.workspace_id,
                "principal": principal_id,
                "kind": principal_kind,
                "role": role.value,
                "credential_kind": kind,
                "hash": hash_token(token),
                "prefix": token_prefix,
                "scopes": [s.value for s in ordered],
                "label": text,
                "lifetime": lifetime,
                "by": actor.principal_id,
            },
        )
        assert row is not None
        await audit.record(
            conn,
            actor,
            "credential.create",
            "api_credential",
            row["id"],
            None,
            1,
            metadata={
                "credential_kind": kind,
                "principal_kind": principal_kind,
                "role": role.value,
                "scopes": [s.value for s in ordered],
                "token_prefix": token_prefix,
            },
        )
    return IssuedCredential(
        credential_id=row["id"],
        workspace_id=actor.workspace_id,
        principal_id=principal_id,
        principal_kind=principal_kind,
        role=role,
        scopes=ordered,
        kind=kind,
        token_prefix=token_prefix,
        expires_at=ensure_utc(row["expires_at"]),
        token=SecretStr(token),
    )


async def revoke_api_credential(conn: Conn, actor: ActorContext, credential_id: UUID, *, reason: str) -> bool:
    """Revoke one credential of ``actor``'s workspace now; ``False`` when it was already revoked.

    The next request with that token is ``401``. Unknown or foreign ids are `NotFound`.
    """
    _require_credential_admin(actor)
    text = reason.strip() if isinstance(reason, str) else ""
    if not 3 <= len(text) <= 500 or _LABEL_CONTROL_RE.search(text):
        raise ValidationFailed("reason must be 3-500 printable characters", details={"fields": ["reason"]})
    async with mapped_errors():
        existing = await fetch_one(
            conn,
            "select id, revoked_at from ops.api_credentials where workspace_id = %(ws)s and id = %(id)s"
            " for update",
            {"ws": actor.workspace_id, "id": credential_id},
        )
        if existing is None:
            raise NotFound("Credential not found")
        if existing["revoked_at"] is not None:
            return False
        await conn.execute(
            "update ops.api_credentials set revoked_at = clock_timestamp(), revoked_by = %(by)s,"
            " revoke_reason = %(reason)s where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": credential_id, "by": actor.principal_id, "reason": text},
        )
        await audit.record(conn, actor, "credential.revoke", "api_credential", credential_id, reason=text)
    return True


__all__ = [
    "ADVERTISED_SCOPES",
    "DEFAULT_CREDENTIAL_LIFETIME",
    "JWT_ALGORITHMS",
    "LOOPBACK_HOSTS",
    "MAX_CREDENTIAL_LIFETIME",
    "MCP_SCOPES",
    "TOKEN_PREFIXES",
    "AuthMode",
    "ClientPrincipal",
    "CredentialKind",
    "CredentialVerifier",
    "IssuedCredential",
    "McpAccessToken",
    "McpAuthConfigError",
    "McpPrincipal",
    "OAuthJwtVerifier",
    "SigningKeyResolver",
    "StaticJwks",
    "current_principal",
    "dev_local_problem",
    "effective_scopes",
    "hash_token",
    "issue_api_credential",
    "parse_scope_claim",
    "revoke_api_credential",
]
