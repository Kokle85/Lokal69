"""Dashboard user authentication: Supabase Auth access tokens and workspace membership (spec 12,
20 "Authentication and authorization", 24; docs/api_contract.md section 1; ADR 0001).

Token verification (`SupabaseJwtVerifier`):

- The token comes from ``Authorization: Bearer`` only (the FastAPI dependency in ``api.deps``
  never reads cookies, query strings or bodies).
- Signature: the project's JWKS at ``<SUPABASE_URL>/auth/v1/.well-known/jwks.json`` through
  PyJWT's ``PyJWKClient`` (`SupabaseJwksClient`: JWK-set cache of 10 minutes, unknown ``kid``
  refresh at most once per 30 seconds, 5 second timeout, no redirects, and no new network attempt
  for 5 seconds after a failed fetch). The fetch is blocking urllib, so it runs in a worker
  thread. Tests and pinned-key deployments inject a `SigningKeyResolver` (`StaticJwks`).
- Algorithms: ``ES256`` and ``RS256`` only; the JWK's own algorithm must equal the header's.
  ``HS256`` is accepted only when a legacy shared secret is configured explicitly; ``none`` and
  every other algorithm are refused before any key lookup.
- Claims: issuer ``<SUPABASE_URL>/auth/v1``, audience ``Settings.supabase_jwt_audience``
  (``authenticated``), ``exp``/``nbf``/``iat`` with a small leeway (``iat`` in the future is
  refused), a canonical UUID ``sub``, ``role == "authenticated"`` (so ``anon`` and
  ``service_role`` API-key JWTs, which also lack ``aud``/``sub``, are refused) and no anonymous
  sign-in session. Token lifetime is bounded (7 days, the Supabase maximum).
- Failures are ``401 UNAUTHENTICATED`` with one generic message (the reason is only a metric
  label). An unreachable JWKS endpoint is ``503 DEPENDENCY_UNAVAILABLE``, never a 401.

Membership (`resolve_principal`): after verification the user's *active* memberships in *active*
workspaces are resolved with the ``app.user_id`` GUC only (``persistence.workspaces``). No
membership is ``403 FORBIDDEN``. ``X-Workspace-Id`` is validated against those memberships and
never trusted on its own: an unknown or foreign workspace is the same ``403`` as no membership,
so the existence of other workspaces is never revealed. With one membership it is the default;
with several the header is required (``GET /api/me`` defaults to the first so the client can
bootstrap). The actor is ``ActorContext(principal_kind="user", role=<membership role>,
scopes=ROLE_SCOPES[role] - {mail:ingest})``: ``mail:ingest`` belongs to the mailbox-bound local
reply worker credential only (spec 37.8), so a browser session never carries it, exactly as the
MCP, CLI and system actors never do (a credential may narrow, never widen, the role).
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import anyio.to_thread
import jwt
from jwt import PyJWK, PyJWKClient, PyJWKSet
from jwt.exceptions import (
    ExpiredSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    PyJWKClientConnectionError,
    PyJWKError,
    PyJWTError,
)
from pydantic import SecretStr

from suv_deals.domain.actor import ROLE_SCOPES, ActorContext
from suv_deals.domain.enums import Role, Scope
from suv_deals.errors import DependencyUnavailable, Forbidden, Unauthenticated, ValidationFailed
from suv_deals.persistence import workspaces
from suv_deals.persistence.database import Database
from suv_deals.persistence.workspaces import Membership
from suv_deals.settings import Settings

AuthDenialReason = Literal[
    "missing_token",
    "invalid_token",
    "expired_token",
    "wrong_issuer",
    "wrong_audience",
    "insufficient_scope",
    "not_member",
]

ASYMMETRIC_ALGORITHMS: Final = ("ES256", "RS256")
LEGACY_SYMMETRIC_ALGORITHM: Final = "HS256"
AUTHENTICATED_ROLE: Final = "authenticated"
DEFAULT_LEEWAY: Final = timedelta(seconds=30)
MAX_LEEWAY: Final = timedelta(minutes=2)
MAX_TOKEN_LIFETIME: Final = timedelta(days=7)
MAX_TOKEN_LENGTH: Final = 8192
JWKS_CACHE_SECONDS: Final = 600
JWKS_REFRESH_COOLDOWN_SECONDS: Final = 30
JWKS_TIMEOUT_SECONDS: Final = 5.0
JWKS_FAILURE_BACKOFF_SECONDS: Final = 5.0
MIN_LEGACY_SECRET_BYTES: Final = 32
WORKSPACE_HEADER: Final = "X-Workspace-Id"
REQUIRED_CLAIMS: Final = ("exp", "iat", "sub", "aud", "iss", "role")
#: Scopes a dashboard (browser) session never carries, whatever the member role.
DASHBOARD_EXCLUDED_SCOPES: Final = frozenset({Scope.MAIL_INGEST})

_COMPACT_JWS: Final = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")
_UUID_RE: Final = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_INVALID_MESSAGE: Final = "The access token is invalid or expired"
_MEMBERSHIP_MESSAGE: Final = "No active membership for the requested workspace"


# --------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------


class AuthFailure(Unauthenticated):
    """``401``: missing, malformed, wrongly signed/issued or expired token (one generic message)."""

    def __init__(self, reason: AuthDenialReason, *, presented: bool = True) -> None:
        super().__init__(_INVALID_MESSAGE if presented else "Authentication required")
        self.reason: AuthDenialReason = reason
        challenge = 'Bearer realm="suv-deals"'
        if presented:
            challenge += ', error="invalid_token"'
        self.response_headers = {"WWW-Authenticate": challenge}


class MembershipDenied(Forbidden):
    """``403``: no active membership, or the requested workspace is not one of them.

    Foreign and non-existent workspaces produce exactly the same error.
    """

    def __init__(self) -> None:
        super().__init__(_MEMBERSHIP_MESSAGE)
        self.reason: AuthDenialReason = "not_member"


# --------------------------------------------------------------------------------------------
# Signing keys
# --------------------------------------------------------------------------------------------


class SigningKeyResolver(Protocol):
    """Resolves the verification key for a token's ``kid`` (``PyJWKClient`` satisfies this)."""

    def get_signing_key_from_jwt(self, token: str) -> PyJWK: ...


class StaticJwks:
    """An in-memory JWK set (tests with locally generated keys, or pinned production keys).

    Only signature keys with a ``kid`` are used; an unknown ``kid`` is an invalid token.
    """

    def __init__(self, jwks: Mapping[str, Any]) -> None:
        key_set = PyJWKSet.from_dict(dict(jwks))
        self._keys: dict[str, PyJWK] = {
            key.key_id: key
            for key in key_set.keys
            if key.key_id is not None and key.public_key_use in ("sig", None)
        }

    def get_signing_key_from_jwt(self, token: str) -> PyJWK:
        kid = jwt.get_unverified_header(token).get("kid")
        key = self._keys.get(kid) if isinstance(kid, str) else None
        if key is None:
            raise PyJWKError("Unable to find a signing key that matches the token")
        return key


def supabase_issuer(supabase_url: str) -> str:
    """``<SUPABASE_URL>/auth/v1`` for a plain http(s) project URL (no credentials, query or fragment)."""
    parts = urlsplit(supabase_url.strip())
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError("SUPABASE_URL must be a plain http(s) URL")
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("SUPABASE_URL must use https outside local development")
    return f"{supabase_url.strip().rstrip('/')}/auth/v1"


class SupabaseJwksClient(PyJWKClient):
    """``PyJWKClient`` that fails fast for a short backoff after a failed JWKS fetch.

    ``PyJWKClient`` holds one lock across its blocking fetch, so without a backoff an outage (or
    an empty/invalid key set) makes every verification queue behind its own network attempt of up
    to ``timeout`` seconds in a worker thread. After a failure the next ``failure_backoff``
    seconds re-raise the same error class without touching the network (an unreachable endpoint
    stays ``503``, an unusable key set stays ``401``); the first attempt after the backoff
    retries. Verification stays fail-closed: no expired key set is ever reused.
    """

    def __init__(
        self,
        uri: str,
        *,
        failure_backoff: float = JWKS_FAILURE_BACKOFF_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        super().__init__(uri, **kwargs)
        if not (failure_backoff >= 0 and math.isfinite(failure_backoff)):
            raise ValueError("failure_backoff must be a finite, non-negative number of seconds")
        self._failure_backoff = failure_backoff
        self._monotonic = monotonic
        self._failure: tuple[float, type[PyJWTError]] | None = None

    def fetch_data(self) -> Any:
        failure = self._failure
        if failure is not None and self._monotonic() - failure[0] < self._failure_backoff:
            raise failure[1]("The JWKS endpoint failed recently; retrying after a short backoff")
        try:
            data = super().fetch_data()
        except PyJWTError as exc:
            self._failure = (self._monotonic(), type(exc))
            raise
        self._failure = None
        return data


def supabase_jwks_client(issuer: str) -> SupabaseJwksClient:
    """Cached, bounded-refresh JWKS client for ``<issuer>/.well-known/jwks.json``."""
    return SupabaseJwksClient(
        f"{issuer}/.well-known/jwks.json",
        cache_jwk_set=True,
        lifespan=JWKS_CACHE_SECONDS,
        timeout=JWKS_TIMEOUT_SECONDS,
        cooldown_duration=JWKS_REFRESH_COOLDOWN_SECONDS,
        headers={"User-Agent": "suv-deals-api/jwks"},
    )


# --------------------------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerifiedUser:
    """The verified identity of a signed-in Supabase user (no token material is kept)."""

    user_id: UUID
    issued_at: datetime
    expires_at: datetime
    session_id: str | None
    aal: str | None


def _timestamp(claims: Mapping[str, Any], name: str) -> datetime:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AuthFailure("invalid_token")
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise AuthFailure("invalid_token") from None


def _short_text(value: object) -> str | None:
    return value[:200] if isinstance(value, str) else None


class SupabaseJwtVerifier:
    """Verifies Supabase Auth user access tokens (see module docstring)."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        resolver: SigningKeyResolver,
        leeway: timedelta = DEFAULT_LEEWAY,
        legacy_hs256_secret: SecretStr | None = None,
    ) -> None:
        if not issuer or not audience:
            raise ValueError("issuer and audience are required")
        if not timedelta(0) <= leeway <= MAX_LEEWAY:
            raise ValueError("leeway must be between 0 and 2 minutes")
        self.issuer = issuer
        self.audience = audience
        self._resolver = resolver
        self._leeway = leeway
        self._legacy_secret: bytes | None = None
        if legacy_hs256_secret is not None:
            secret = legacy_hs256_secret.get_secret_value().encode("utf-8")
            if len(secret) < MIN_LEGACY_SECRET_BYTES:
                raise ValueError("the legacy HS256 secret must be at least 32 bytes")
            self._legacy_secret = secret

    @property
    def algorithms(self) -> tuple[str, ...]:
        if self._legacy_secret is None:
            return ASYMMETRIC_ALGORITHMS
        return (*ASYMMETRIC_ALGORITHMS, LEGACY_SYMMETRIC_ALGORITHM)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        resolver: SigningKeyResolver | None = None,
        leeway: timedelta = DEFAULT_LEEWAY,
        legacy_hs256_secret: SecretStr | None = None,
    ) -> SupabaseJwtVerifier | None:
        """The verifier for the configured project, or ``None`` when ``SUPABASE_URL`` is unset."""
        if not settings.supabase_url:
            return None
        issuer = supabase_issuer(settings.supabase_url)
        return cls(
            issuer=issuer,
            audience=settings.supabase_jwt_audience,
            resolver=resolver if resolver is not None else supabase_jwks_client(issuer),
            leeway=leeway,
            legacy_hs256_secret=legacy_hs256_secret,
        )

    async def _key_for(self, token: str, alg: str) -> PyJWK | bytes:
        if alg == LEGACY_SYMMETRIC_ALGORITHM:
            assert self._legacy_secret is not None  # alg is only allowed when configured
            return self._legacy_secret
        try:
            key = await anyio.to_thread.run_sync(self._resolver.get_signing_key_from_jwt, token)
        except PyJWKClientConnectionError:
            raise DependencyUnavailable("The identity provider's signing keys are unavailable") from None
        except (PyJWKError, PyJWTError):
            raise AuthFailure("invalid_token") from None
        if not isinstance(key, PyJWK) or key.algorithm_name != alg:
            raise AuthFailure("invalid_token")
        return key

    async def verify(self, token: str) -> VerifiedUser:
        """Verify one access token; raises `AuthFailure` (401) or `DependencyUnavailable` (503)."""
        if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH or not _COMPACT_JWS.fullmatch(token):
            raise AuthFailure("invalid_token")
        try:
            header = jwt.get_unverified_header(token)
        except PyJWTError:
            raise AuthFailure("invalid_token") from None
        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in self.algorithms or "crit" in header:
            raise AuthFailure("invalid_token")
        if alg != LEGACY_SYMMETRIC_ALGORITHM and not isinstance(header.get("kid"), str):
            raise AuthFailure("invalid_token")
        key = await self._key_for(token, alg)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience=self.audience,
                issuer=self.issuer,
                leeway=self._leeway,
                options={
                    "require": list(REQUIRED_CLAIMS),
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
            raise AuthFailure("expired_token") from None
        except InvalidIssuerError:
            raise AuthFailure("wrong_issuer") from None
        except InvalidAudienceError:
            raise AuthFailure("wrong_audience") from None
        except PyJWTError:
            raise AuthFailure("invalid_token") from None
        return self._user(claims)

    def _user(self, claims: Mapping[str, Any]) -> VerifiedUser:
        if claims.get("role") != AUTHENTICATED_ROLE or claims.get("is_anonymous") is True:
            raise AuthFailure("invalid_token")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not _UUID_RE.fullmatch(sub):
            raise AuthFailure("invalid_token")
        issued_at = _timestamp(claims, "iat")
        expires_at = _timestamp(claims, "exp")
        if expires_at <= issued_at or expires_at - issued_at > MAX_TOKEN_LIFETIME:
            raise AuthFailure("invalid_token")
        return VerifiedUser(
            user_id=UUID(sub),
            issued_at=issued_at,
            expires_at=expires_at,
            session_id=_short_text(claims.get("session_id")),
            aal=_short_text(claims.get("aal")),
        )


def bearer_token(authorization: Sequence[str]) -> str:
    """The token of exactly one ``Authorization: Bearer <token>`` header.

    No header is ``missing_token``; several headers, another scheme or an empty token is
    ``invalid_token``.
    """
    if not authorization:
        raise AuthFailure("missing_token", presented=False)
    if len(authorization) != 1:
        raise AuthFailure("invalid_token")
    scheme, _, token = authorization[0].strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        raise AuthFailure("invalid_token")
    return token


# --------------------------------------------------------------------------------------------
# Membership and workspace selection
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Principal:
    """A verified user, their active memberships, the selected one and the request's actor."""

    user: VerifiedUser
    memberships: tuple[Membership, ...]
    membership: Membership
    actor: ActorContext


def select_membership(
    memberships: Sequence[Membership], requested: str | None, *, allow_default: bool = False
) -> Membership:
    """The membership for the requested workspace (see module docstring for the rules)."""
    active = [m for m in memberships if m.active and m.workspace_active]
    if not active:
        raise MembershipDenied()
    if requested is None:
        if len(active) == 1 or allow_default:
            return active[0]
        raise ValidationFailed(
            "Several workspaces are available; select one with X-Workspace-Id",
            details={"fields": [WORKSPACE_HEADER]},
        )
    value = requested.strip()
    if not _UUID_RE.fullmatch(value):
        raise ValidationFailed("X-Workspace-Id must be a UUID", details={"fields": [WORKSPACE_HEADER]})
    workspace_id = UUID(value)
    for membership in active:
        if membership.workspace_id == workspace_id:
            return membership
    raise MembershipDenied()


def dashboard_scopes(role: Role) -> frozenset[Scope]:
    """The scopes of a dashboard session for ``role``: the role's scopes minus worker-only ones."""
    return ROLE_SCOPES[Role(role)] - DASHBOARD_EXCLUDED_SCOPES


def actor_for(user: VerifiedUser, membership: Membership, request_id: str) -> ActorContext:
    return ActorContext(
        workspace_id=membership.workspace_id,
        principal_id=user.user_id,
        principal_kind="user",
        role=membership.role,
        scopes=dashboard_scopes(membership.role),
        request_id=request_id,
    )


async def resolve_principal(
    db: Database,
    user: VerifiedUser,
    *,
    requested_workspace: str | None,
    request_id: str,
    allow_default_workspace: bool = False,
) -> Principal:
    """Resolve the verified user's active memberships and the request's actor."""
    memberships = tuple(await workspaces.resolve_memberships_for_user(db, user.user_id))
    membership = select_membership(memberships, requested_workspace, allow_default=allow_default_workspace)
    return Principal(
        user=user,
        memberships=tuple(m for m in memberships if m.active and m.workspace_active),
        membership=membership,
        actor=actor_for(user, membership, request_id),
    )


__all__ = [
    "ASYMMETRIC_ALGORITHMS",
    "DASHBOARD_EXCLUDED_SCOPES",
    "WORKSPACE_HEADER",
    "AuthDenialReason",
    "AuthFailure",
    "MembershipDenied",
    "Principal",
    "SigningKeyResolver",
    "StaticJwks",
    "SupabaseJwksClient",
    "SupabaseJwtVerifier",
    "VerifiedUser",
    "actor_for",
    "bearer_token",
    "dashboard_scopes",
    "resolve_principal",
    "select_membership",
    "supabase_issuer",
    "supabase_jwks_client",
]
