"""A small, loopback-only emulation of the Supabase Auth (GoTrue) endpoints supabase-js uses.

TEST-ONLY. It exists so the dashboard's browser E2E tests can sign in for real (supabase-js in the
browser, real HTTP, real ES256 JWTs verified by the backend through a real JWKS fetch) without any
Supabase project. All users are SYNTHETIC (``tests/e2e/users.py``).

Endpoints (``/auth/v1`` prefix, as on a Supabase project):

- ``POST /token?grant_type=password``       email + password sign-in
- ``POST /token?grant_type=refresh_token``  refresh-token rotation (a used refresh token is revoked)
- ``POST /token?grant_type=pkce``            magic-link code exchange (S256 verifier checked)
- ``GET  /user``                             the user of a valid access token
- ``POST /logout?scope=global|local|others`` revokes refresh tokens
- ``POST /otp``                              "sends" a magic link (kept in memory, never mailed)
- ``GET  /verify``                           magic-link landing: redirects with ``?code=`` or ``?error=``
- ``GET  /.well-known/jwks.json``            the public ES256 signing key
- ``GET  /health``                           liveness (Playwright waits for it)

Test helpers (NOT part of Supabase): ``GET /__e2e/magic-link?email=`` returns the last link sent to
that address.

Access tokens carry the claims Supabase documents (``iss=<url>/auth/v1``, ``aud=authenticated``,
``sub``, ``role=authenticated``, ``aal``, ``session_id``, ``email``, ``phone``, ``is_anonymous``,
``amr``). For a user with a short ``access_ttl`` the JWT ``exp`` is short while the response still
advertises the normal lifetime, so supabase-js does not refresh proactively and the backend rejects
the token mid-session: the dashboard must refresh and retry (spec 23 "token expiry mid-review").

Run: ``uv run python tests/e2e/mock_supabase_auth.py --port 54399 --allow-origin http://127.0.0.1:4173``
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import secrets
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlencode, urlsplit

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from jwt.algorithms import ECAlgorithm

if __package__ in (None, ""):  # executed as a script: make `tests.e2e` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.e2e.users import DEFAULT_TTL, PASSWORD, PUBLISHABLE_KEY, USERS, USERS_BY_EMAIL, E2EUser

API_VERSION: Final = "2024-01-01"
LOOPBACK: Final = ("127.0.0.1", "localhost")


@dataclass
class SessionRecord:
    session_id: str
    user: E2EUser
    refresh_token: str
    revoked: bool = False


@dataclass
class MagicLink:
    email: str
    token: str
    code_challenge: str | None
    redirect_to: str
    created_at: float
    used: bool = False


@dataclass
class AuthState:
    base_url: str
    publishable_key: str
    private_key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    kid: str = field(default_factory=lambda: f"synthetic-e2e-{uuid.uuid4().hex[:12]}")
    sessions: dict[str, SessionRecord] = field(default_factory=dict)  # refresh token -> session
    links: dict[str, MagicLink] = field(default_factory=dict)  # one-time token -> link
    codes: dict[str, tuple[E2EUser, str | None]] = field(
        default_factory=dict
    )  # auth code -> (user, challenge)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def issuer(self) -> str:
        return f"{self.base_url}/auth/v1"

    def jwks(self) -> dict[str, Any]:
        jwk = ECAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        jwk.update(kid=self.kid, alg="ES256", use="sig", key_ops=["verify"])
        return {"keys": [jwk]}

    def access_token(self, user: E2EUser, session_id: str) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": "authenticated",
            "sub": str(user.user_id),
            "role": "authenticated",
            "iat": now,
            "exp": now + user.access_ttl,
            "aal": "aal1",
            "amr": [{"method": "password", "timestamp": now}],
            "session_id": session_id,
            "email": user.email,
            "phone": "",
            "is_anonymous": False,
            "app_metadata": {"provider": "email", "providers": ["email"]},
            "user_metadata": {},
        }
        return jwt.encode(
            claims, self.private_key, algorithm="ES256", headers={"kid": self.kid, "typ": "JWT"}
        )

    def verify(self, token: str) -> dict[str, Any] | None:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self.private_key.public_key(),
                algorithms=["ES256"],
                audience="authenticated",
                issuer=self.issuer,
            )
        except jwt.PyJWTError:
            return None
        return claims


def user_json(user: E2EUser) -> dict[str, Any]:
    stamp = "2026-10-01T00:00:00Z"
    return {
        "id": str(user.user_id),
        "aud": "authenticated",
        "role": "authenticated",
        "email": user.email,
        "email_confirmed_at": stamp,
        "phone": "",
        "confirmed_at": stamp,
        "last_sign_in_at": stamp,
        "app_metadata": {"provider": "email", "providers": ["email"]},
        "user_metadata": {},
        "identities": [],
        "created_at": stamp,
        "updated_at": stamp,
        "is_anonymous": False,
    }


def auth_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"code": code, "error_code": code, "msg": message, "error_description": message},
        status_code=status,
        headers={"X-Supabase-Api-Version": API_VERSION},
    )


def _challenge_ok(verifier: str, challenge: str | None) -> bool:
    if challenge is None:
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, challenge)


def _safe_redirect(value: str | None, allowed_origins: list[str]) -> str | None:
    """Only same-origin redirects to an allowed (dashboard) origin, like Supabase's allow-list."""
    if not value:
        return None
    parts = urlsplit(value)
    origin = f"{parts.scheme}://{parts.netloc}"
    return value if origin in allowed_origins else None


def create_mock_auth_app(
    *, base_url: str, allow_origins: list[str], publishable_key: str = PUBLISHABLE_KEY
) -> FastAPI:
    state = AuthState(base_url=base_url.rstrip("/"), publishable_key=publishable_key)
    app = FastAPI(title="SYNTHETIC mock Supabase Auth", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = state
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["apikey", "authorization", "content-type", "x-client-info", "x-supabase-api-version"],
        expose_headers=["x-supabase-api-version"],
        allow_credentials=False,
        max_age=600,
    )

    def require_apikey(request: Request) -> JSONResponse | None:
        if request.headers.get("apikey") != state.publishable_key:
            return auth_error(401, "no_authorization", "No API key found in request")
        return None

    def session_response(user: E2EUser, session_id: str | None = None) -> JSONResponse:
        sid = session_id or str(uuid.uuid4())
        refresh = secrets.token_urlsafe(24)
        with state.lock:
            state.sessions[refresh] = SessionRecord(session_id=sid, user=user, refresh_token=refresh)
        now = int(time.time())
        body = {
            "access_token": state.access_token(user, sid),
            "token_type": "bearer",
            # Advertised lifetime: always the normal one (see module docstring).
            "expires_in": DEFAULT_TTL,
            "expires_at": now + DEFAULT_TTL,
            "refresh_token": refresh,
            "user": user_json(user),
        }
        return JSONResponse(
            body, headers={"X-Supabase-Api-Version": API_VERSION, "Cache-Control": "no-store"}
        )

    @app.get("/auth/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "name": "SYNTHETIC mock Supabase Auth"}

    @app.get("/auth/v1/.well-known/jwks.json")
    async def jwks() -> JSONResponse:
        return JSONResponse(state.jwks(), headers={"Cache-Control": "public, max-age=600"})

    @app.post("/auth/v1/token")
    async def token(request: Request, grant_type: str = "") -> Response:
        if (refused := require_apikey(request)) is not None:
            return refused
        try:
            body = await request.json()
        except ValueError:
            return auth_error(400, "validation_failed", "Invalid JSON body")
        if not isinstance(body, dict):
            return auth_error(400, "validation_failed", "Invalid JSON body")
        if grant_type == "password":
            user = USERS_BY_EMAIL.get(str(body.get("email", "")).strip().lower())
            if user is None or not secrets.compare_digest(str(body.get("password", "")), PASSWORD):
                return auth_error(400, "invalid_credentials", "Invalid login credentials")
            return session_response(user)
        if grant_type == "refresh_token":
            presented = str(body.get("refresh_token", ""))
            with state.lock:
                record = state.sessions.get(presented)
                if record is None or record.revoked:
                    return auth_error(
                        400, "refresh_token_not_found", "Invalid Refresh Token: Refresh Token Not Found"
                    )
                record.revoked = True  # rotation: a refresh token is single-use
            return session_response(record.user, record.session_id)
        if grant_type == "pkce":
            code = str(body.get("auth_code", ""))
            verifier = str(body.get("code_verifier", ""))
            with state.lock:
                entry = state.codes.pop(code, None)
            if entry is None or not _challenge_ok(verifier, entry[1]):
                return auth_error(
                    400, "flow_state_not_found", "invalid flow state, no valid flow state found"
                )
            return session_response(entry[0])
        return auth_error(400, "unsupported_grant_type", "unsupported_grant_type")

    @app.get("/auth/v1/user")
    async def get_user(request: Request) -> Response:
        if (refused := require_apikey(request)) is not None:
            return refused
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        claims = state.verify(presented) if scheme.lower() == "bearer" else None
        if claims is None:
            return auth_error(403, "bad_jwt", "invalid JWT: unable to parse or verify signature")
        user = next((u for u in USERS.values() if str(u.user_id) == claims.get("sub")), None)
        if user is None:
            return auth_error(404, "user_not_found", "User from sub claim in JWT does not exist")
        return JSONResponse(user_json(user), headers={"X-Supabase-Api-Version": API_VERSION})

    @app.post("/auth/v1/logout")
    async def logout(request: Request, scope: str = "global") -> Response:
        if (refused := require_apikey(request)) is not None:
            return refused
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        claims = state.verify(presented) if scheme.lower() == "bearer" else None
        if claims is not None:
            with state.lock:
                for record in state.sessions.values():
                    same_user = str(record.user.user_id) == claims.get("sub")
                    same_session = record.session_id == claims.get("session_id")
                    if same_user and (scope == "global" or (scope == "local") == same_session):
                        record.revoked = True
        return Response(status_code=204)

    @app.post("/auth/v1/otp")
    async def otp(request: Request, redirect_to: str | None = None) -> Response:
        if (refused := require_apikey(request)) is not None:
            return refused
        body = await request.json()
        email = str(body.get("email", "")).strip().lower()
        target = _safe_redirect(redirect_to, allow_origins) or (
            allow_origins[0] if allow_origins else base_url
        )
        if email in USERS_BY_EMAIL:  # unknown addresses get the same answer (no enumeration)
            link = MagicLink(
                email=email,
                token=secrets.token_urlsafe(24),
                code_challenge=body.get("code_challenge"),
                redirect_to=target,
                created_at=time.time(),
            )
            with state.lock:
                state.links[link.token] = link
        return JSONResponse({}, headers={"X-Supabase-Api-Version": API_VERSION})

    @app.get("/auth/v1/verify")
    async def verify(token: str = "", type: str = "magiclink", redirect_to: str | None = None) -> Response:
        with state.lock:
            link = state.links.get(token)
            valid = (
                link is not None
                and not link.used
                and type in ("magiclink", "email")
                and time.time() - link.created_at < 600
            )
            if valid and link is not None:
                link.used = True
        target = (
            (link.redirect_to if link else None)
            or _safe_redirect(redirect_to, allow_origins)
            or allow_origins[0]
        )
        joiner = "&" if "?" in target else "?"
        if not valid or link is None:
            query = urlencode(
                {
                    "error": "access_denied",
                    "error_code": "otp_expired",
                    "error_description": "Email link is invalid or has expired",
                }
            )
            return RedirectResponse(f"{target}{joiner}{query}", status_code=303)
        code = secrets.token_urlsafe(24)
        with state.lock:
            state.codes[code] = (USERS_BY_EMAIL[link.email], link.code_challenge)
        return RedirectResponse(f"{target}{joiner}{urlencode({'code': code})}", status_code=303)

    @app.get("/__e2e/magic-link")
    async def last_magic_link(email: str) -> Response:
        with state.lock:
            links = [link for link in state.links.values() if link.email == email.strip().lower()]
        if not links:
            return JSONResponse({"detail": "no link"}, status_code=404)
        latest = max(links, key=lambda link: link.created_at)
        query = urlencode({"token": latest.token, "type": "magiclink", "redirect_to": latest.redirect_to})
        return JSONResponse({"url": f"{state.issuer}/verify?{query}"})

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SYNTHETIC mock Supabase Auth (test-only, loopback)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=54399)
    parser.add_argument(
        "--allow-origin", action="append", default=[], help="dashboard origin(s) for CORS/redirects"
    )
    args = parser.parse_args(argv)
    if args.host not in LOOPBACK:
        parser.error("the mock auth server binds to loopback only")
    origins = args.allow_origin or ["http://127.0.0.1:4173"]
    base_url = f"http://{args.host}:{args.port}"
    app = create_mock_auth_app(base_url=base_url, allow_origins=origins)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
