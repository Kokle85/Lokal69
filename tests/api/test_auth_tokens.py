"""Supabase access-token verification and workspace selection (pure; no database, no network).

Every key is generated locally; the JWKS is in memory (``StaticJwks``) or served by a
``PyJWKClient`` whose HTTP fetch is replaced by a local function (rotation/refresh behaviour).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError
from pydantic import SecretStr
from tests.api.conftest import ES_KID, ISSUER, RS_KID, SUPABASE_URL, SigningKeys, TokenFactory, make_settings

from suv_deals.api.auth import (
    AuthFailure,
    MembershipDenied,
    StaticJwks,
    SupabaseJwtVerifier,
    bearer_token,
    select_membership,
    supabase_issuer,
)
from suv_deals.domain.enums import Role
from suv_deals.errors import DependencyUnavailable, ErrorCode, ValidationFailed
from suv_deals.persistence.workspaces import Membership

USER = uuid.UUID("11111111-1111-4111-8111-111111111111")


def verifier(keys: SigningKeys, **kwargs: Any) -> SupabaseJwtVerifier:
    return SupabaseJwtVerifier(
        issuer=ISSUER, audience="authenticated", resolver=StaticJwks(keys.jwks), **kwargs
    )


async def reason_of(v: SupabaseJwtVerifier, token: str) -> str:
    with pytest.raises(AuthFailure) as caught:
        await v.verify(token)
    assert caught.value.code == ErrorCode.UNAUTHENTICATED
    assert caught.value.message == "The access token is invalid or expired"
    return caught.value.reason


# --------------------------------------------------------------------------------------------
# Accepted tokens
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("alg", ["ES256", "RS256"])
async def test_valid_user_tokens_verify(keys: SigningKeys, tokens: TokenFactory, alg: str) -> None:
    user = await verifier(keys).verify(tokens.mint(USER, alg=alg))
    assert user.user_id == USER
    assert user.aal == "aal1"
    assert user.expires_at > datetime.now(UTC)


async def test_small_clock_skew_is_tolerated(keys: SigningKeys, tokens: TokenFactory) -> None:
    v = verifier(keys)
    assert (await v.verify(tokens.mint(USER, expires_in=3600, issued_offset=-3610))).user_id == USER
    assert (await v.verify(tokens.mint(USER, issued_offset=10))).user_id == USER


async def test_audience_list_containing_authenticated_is_accepted(
    keys: SigningKeys, tokens: TokenFactory
) -> None:
    token = tokens.mint(USER, extra={"aud": ["authenticated", "other"]})
    assert (await verifier(keys).verify(token)).user_id == USER


# --------------------------------------------------------------------------------------------
# Refused tokens
# --------------------------------------------------------------------------------------------


async def test_wrong_issuer_and_audience_are_refused(keys: SigningKeys, tokens: TokenFactory) -> None:
    v = verifier(keys)
    assert (
        await reason_of(v, tokens.mint(USER, extra={"iss": "https://evil.example/auth/v1"})) == "wrong_issuer"
    )
    assert await reason_of(v, tokens.mint(USER, extra={"iss": "supabase"})) == "wrong_issuer"
    assert await reason_of(v, tokens.mint(USER, extra={"aud": "anon"})) == "wrong_audience"
    assert await reason_of(v, tokens.mint(USER, drop=["aud"])) == "invalid_token"


async def test_expired_future_and_not_yet_valid_tokens_are_refused(
    keys: SigningKeys, tokens: TokenFactory
) -> None:
    v = verifier(keys)
    assert await reason_of(v, tokens.mint(USER, expires_in=60, issued_offset=-200)) == "expired_token"
    assert await reason_of(v, tokens.mint(USER, issued_offset=600)) == "invalid_token"  # iat in the future
    assert await reason_of(v, tokens.mint(USER, extra={"nbf": int(datetime.now(UTC).timestamp()) + 600})) == (
        "invalid_token"
    )
    assert await reason_of(v, tokens.mint(USER, drop=["exp"])) == "invalid_token"
    assert await reason_of(v, tokens.mint(USER, drop=["iat"])) == "invalid_token"
    assert (
        await reason_of(v, tokens.mint(USER, expires_in=8 * 24 * 3600)) == "invalid_token"
    )  # lifetime bound


async def test_alg_none_and_unlisted_algorithms_are_refused(keys: SigningKeys, tokens: TokenFactory) -> None:
    v = verifier(keys)
    assert await reason_of(v, tokens.unsigned(USER)) == "invalid_token"
    assert await reason_of(v, tokens.unsigned(USER, alg="None")) == "invalid_token"
    token = tokens.mint(USER, alg="ES384", key=ec.generate_private_key(ec.SECP384R1()), kid=ES_KID)
    assert await reason_of(v, token) == "invalid_token"


async def test_hs256_forgeries_are_refused(keys: SigningKeys, tokens: TokenFactory) -> None:
    v = verifier(keys)
    # A shared-secret token with any secret, and the classic alg-confusion attack that uses the
    # published RSA public key as the HMAC secret.
    assert (
        await reason_of(v, tokens.hmac_signed(USER, b"guessable-secret-but-still-not-allowed!"))
        == "invalid_token"
    )
    assert await reason_of(v, tokens.hmac_signed(USER, keys.rs_public_pem, kid=RS_KID)) == "invalid_token"


async def test_hs256_only_with_an_explicitly_configured_legacy_secret(
    tokens: TokenFactory, keys: SigningKeys
) -> None:
    secret = "SYNTHETIC-legacy-jwt-secret-with-at-least-32-chars"
    legacy = verifier(keys, legacy_hs256_secret=SecretStr(secret))
    assert (await legacy.verify(tokens.hmac_signed(USER, secret.encode()))).user_id == USER
    assert await reason_of(legacy, tokens.hmac_signed(USER, b"another-secret-of-sufficient-length!!")) == (
        "invalid_token"
    )
    # Still never the RSA public key as an HMAC secret, and never alg=none.
    assert await reason_of(legacy, tokens.hmac_signed(USER, keys.rs_public_pem)) == "invalid_token"
    assert await reason_of(legacy, tokens.unsigned(USER)) == "invalid_token"
    with pytest.raises(ValueError, match="32 bytes"):
        verifier(keys, legacy_hs256_secret=SecretStr("short"))


async def test_service_role_and_anon_keys_are_refused(keys: SigningKeys, tokens: TokenFactory) -> None:
    v = verifier(keys)
    # Legacy API-key JWT shape: iss "supabase", a role, no aud/sub; even with a valid signature.
    service_key = jwt.encode(
        {"iss": "supabase", "ref": "synthetic", "role": "service_role", "iat": 1, "exp": 4_102_444_800},
        keys.es_private,
        algorithm="ES256",
        headers={"kid": ES_KID},
    )
    assert await reason_of(v, service_key) in {"wrong_issuer", "invalid_token"}
    assert await reason_of(v, tokens.mint(USER, extra={"role": "service_role"})) == "invalid_token"
    assert await reason_of(v, tokens.mint(USER, extra={"role": "anon"})) == "invalid_token"
    assert await reason_of(v, tokens.mint(USER, extra={"is_anonymous": True})) == "invalid_token"
    # New-format secret/publishable keys are not JWTs at all.
    assert await reason_of(v, "sb_secret_SYNTHETICabcdefghijklmnop") == "invalid_token"


async def test_bad_subject_signature_kid_and_headers_are_refused(
    keys: SigningKeys, tokens: TokenFactory
) -> None:
    v = verifier(keys)
    assert await reason_of(v, tokens.mint("not-a-uuid")) == "invalid_token"
    assert await reason_of(v, tokens.mint(USER, drop=["sub"])) == "invalid_token"
    # Signed by an unpublished key that claims a published kid.
    assert await reason_of(v, tokens.mint(USER, key=keys.stranger_private, kid=ES_KID)) == "invalid_token"
    # Unknown kid, missing kid, and an RS256 token that names the ES256 key.
    assert await reason_of(v, tokens.mint(USER, kid="unknown-kid")) == "invalid_token"
    no_kid = jwt.encode(tokens.claims(USER), keys.es_private, algorithm="ES256")
    assert await reason_of(v, no_kid) == "invalid_token"
    assert await reason_of(v, tokens.mint(USER, alg="RS256", kid=ES_KID)) == "invalid_token"
    assert await reason_of(v, tokens.mint(USER, headers={"crit": ["exp"]})) == "invalid_token"
    # Malformed and oversized input.
    assert await reason_of(v, "not.a.jwt.at.all") == "invalid_token"
    assert await reason_of(v, "a" * 9000) == "invalid_token"
    token = tokens.mint(USER)
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert await reason_of(v, tampered) == "invalid_token"


# --------------------------------------------------------------------------------------------
# JWKS fetching: PyJWKClient caching and bounded refresh (fetch replaced locally)
# --------------------------------------------------------------------------------------------


class LocalJwksClient(PyJWKClient):
    """``PyJWKClient`` whose HTTP fetch returns a local JWK set (no network)."""

    def __init__(self, jwks: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(f"{ISSUER}/.well-known/jwks.json", **kwargs)
        self.jwks = jwks
        self.fetches = 0
        self.fail = False

    def fetch_data(self) -> Any:
        self.fetches += 1
        if self.fail:
            raise PyJWKClientConnectionError("synthetic outage")
        if self.jwk_set_cache is not None:
            self.jwk_set_cache.put(self.jwks)
        self._last_successful_fetch = time.monotonic()
        return self.jwks


async def test_jwks_client_caches_and_refreshes_for_a_rotated_key(
    keys: SigningKeys, tokens: TokenFactory
) -> None:
    client = LocalJwksClient({"keys": [keys.jwks["keys"][0]]}, lifespan=600, cooldown_duration=0)
    v = SupabaseJwtVerifier(issuer=ISSUER, audience="authenticated", resolver=client)
    await v.verify(tokens.mint(USER))
    await v.verify(tokens.mint(USER))
    assert client.fetches == 1  # cached
    client.jwks = keys.jwks  # the project rotated in the RS256 key
    assert (await v.verify(tokens.mint(USER, alg="RS256"))).user_id == USER
    assert client.fetches == 2  # one refresh for the unknown kid


async def test_unknown_kid_refresh_is_rate_limited(keys: SigningKeys, tokens: TokenFactory) -> None:
    client = LocalJwksClient(keys.jwks, lifespan=600, cooldown_duration=30)
    v = SupabaseJwtVerifier(issuer=ISSUER, audience="authenticated", resolver=client)
    await v.verify(tokens.mint(USER))
    for _ in range(5):
        assert await reason_of(v, tokens.mint(USER, kid=f"attacker-{uuid.uuid4()}")) == "invalid_token"
    assert client.fetches == 1  # random kids never force a fetch storm


async def test_jwks_outage_is_dependency_unavailable_not_401(keys: SigningKeys, tokens: TokenFactory) -> None:
    client = LocalJwksClient(keys.jwks, cache_jwk_set=False)
    client.fail = True
    v = SupabaseJwtVerifier(issuer=ISSUER, audience="authenticated", resolver=client)
    with pytest.raises(DependencyUnavailable):
        await v.verify(tokens.mint(USER))


def test_verifier_from_settings_uses_the_project_issuer_and_audience() -> None:
    v = SupabaseJwtVerifier.from_settings(make_settings())
    assert v is not None
    assert v.issuer == ISSUER
    assert v.audience == "authenticated"
    assert v.algorithms == ("ES256", "RS256")
    assert SupabaseJwtVerifier.from_settings(make_settings(supabase_url=None)) is None


@pytest.mark.parametrize(
    "url",
    [
        "ftp://synthetic.example",
        "https://user:pw@synthetic.example",
        "https://synthetic.example?x=1",
        "http://synthetic-project.supabase.example",
    ],
)
def test_supabase_issuer_refuses_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError):
        supabase_issuer(url)
    assert supabase_issuer(SUPABASE_URL + "/") == ISSUER
    assert supabase_issuer("http://127.0.0.1:54321") == "http://127.0.0.1:54321/auth/v1"


# --------------------------------------------------------------------------------------------
# Authorization header parsing
# --------------------------------------------------------------------------------------------


def test_bearer_token_parsing() -> None:
    assert bearer_token(["Bearer abc.def.ghi"]) == "abc.def.ghi"
    assert bearer_token(["bearer   abc.def.ghi  "]) == "abc.def.ghi"
    with pytest.raises(AuthFailure) as missing:
        bearer_token([])
    assert missing.value.reason == "missing_token"
    assert missing.value.response_headers["WWW-Authenticate"] == 'Bearer realm="suv-deals"'
    for header in (
        ["Basic dXNlcjpwYXNz"],
        ["Bearer"],
        ["Bearer a b"],
        ["Bearer x.y.z", "Bearer x.y.z"],
        ["Token x"],
    ):
        with pytest.raises(AuthFailure) as caught:
            bearer_token(header)
        assert caught.value.reason == "invalid_token"
        assert 'error="invalid_token"' in caught.value.response_headers["WWW-Authenticate"]


# --------------------------------------------------------------------------------------------
# Workspace selection
# --------------------------------------------------------------------------------------------


def membership(
    workspace: UUID, *, role: Role = Role.REVIEWER, active: bool = True, ws_active: bool = True
) -> Membership:
    now = datetime(2026, 10, 6, tzinfo=UTC)
    return Membership(
        workspace_id=workspace,
        user_id=USER,
        role=role,
        active=active,
        workspace_name=f"SYNTHETIC {workspace}",
        display_timezone="Europe/Skopje",
        workspace_active=ws_active,
        created_at=now,
        updated_at=now - timedelta(0),
    )


def test_sole_membership_is_the_default_and_header_must_match() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    only = [membership(a)]
    assert select_membership(only, None).workspace_id == a
    assert select_membership(only, str(a)).workspace_id == a
    assert select_membership(only, str(a).upper()).workspace_id == a
    with pytest.raises(MembershipDenied):
        select_membership(only, str(b))  # foreign or unknown: same 403
    with pytest.raises(ValidationFailed) as malformed:
        select_membership(only, "not-a-uuid")
    assert malformed.value.details == {"fields": ["X-Workspace-Id"]}


def test_several_memberships_need_the_header_except_for_bootstrap() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    both = [membership(a), membership(b, role=Role.OWNER)]
    with pytest.raises(ValidationFailed):
        select_membership(both, None)
    assert select_membership(both, None, allow_default=True).workspace_id == a
    assert select_membership(both, str(b)).role == Role.OWNER


def test_inactive_memberships_and_workspaces_never_authorize() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(MembershipDenied):
        select_membership([], None)
    with pytest.raises(MembershipDenied):
        select_membership([membership(a, active=False)], None)
    with pytest.raises(MembershipDenied):
        select_membership([membership(a, ws_active=False)], str(a))
    assert select_membership([membership(a, active=False), membership(b)], None).workspace_id == b
