"""Owner-authorized sending identities (``ops.email_sender_bindings``; spec 37.3, 37.8; ADR 0002).

A binding is the verified sender: provider (``outlook_local`` | ``gmail_api`` |
``microsoft_graph``), stable account id, From address, display name, optional Reply-To alias,
alias verification and health. Provider, account and From are the identity: a different account
is a NEW binding (the database freezes them), never a silent switch. Every change that matters
for an inquiry (display name, Reply-To, alias status, verification, credential) advances
``version``; reservations bind ``(binding_id, version)`` and the dispatch guard refuses a changed
binding.

Credentials are never stored in clear and never returned: the OAuth grant of an API provider is
sealed with ``integrations.secret_box`` (AES-256-GCM; associated data binds it to this workspace,
binding, provider and account) into ``secret_envelope``, or the binding names an external
``secret_reference``. Records expose only ``has_secret``. ``SELLER_EMAIL_OAUTH_SECRET_REFERENCE``
points at a sealed envelope as ``secretbox:ops.email_sender_bindings/<binding id>``;
``BindingTokenProvider`` (the server-side ``TokenProvider`` of ``gmail_api``) resolves it, opens
the envelope and exchanges the refresh token at Google's fixed OAuth 2.0 token endpoint
(``grant_type=refresh_token``, RFC 6749 section 6) through an injected HTTP client - outside any
database transaction. A revoked grant (``invalid_grant``) raises ``TokenUnavailable(revoked=True)``
and marks the binding ``unhealthy``, so the dispatch preflight holds/suppresses instead of sending.

Scopes: writes need ``config:admin`` (or a system principal); reads need ``inquiries:read``.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from suv_deals.clock import Clock, SystemClock, ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import EmailProviderKind
from suv_deals.domain.inquiries import SenderBinding, SenderMode, SenderStatus
from suv_deals.domain.seller_contacts import AddressError, canonicalize_address
from suv_deals.errors import AppError, NotFound, ValidationFailed, VersionConflict
from suv_deals.integrations.email_providers.base import AccessToken, TokenUnavailable
from suv_deals.integrations.secret_box import SecretBox, SecretBoxError
from suv_deals.persistence import audit
from suv_deals.persistence.database import Conn, Database, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.sellers_repo import require_inquiry_reader, require_inquiry_writer

Health = Literal["unknown", "healthy", "degraded", "unhealthy"]

SECRET_REFERENCE_SCHEME: Final = "secretbox"  # noqa: S105 - a reference scheme, not a secret
SECRET_REFERENCE_TABLE: Final = "ops.email_sender_bindings"  # noqa: S105 - a table name
#: Google's OAuth 2.0 token endpoint (fixed; never configurable, so a sealed grant can never be
#: sent anywhere else).
GOOGLE_TOKEN_URL: Final = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint URL
TOKEN_REFRESH_MARGIN: Final = timedelta(seconds=60)
_REFERENCE_RE: Final = re.compile(
    r"^secretbox:ops\.email_sender_bindings/(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
_EXTERNAL_REFERENCE_RE: Final = re.compile(r"^[a-z][a-z0-9+.-]{1,30}:[A-Za-z0-9._/-]{1,200}$")
_DETAIL_RE: Final = re.compile(r"[^A-Za-z0-9_.:, -]")
_MAX_TOKEN_RESPONSE_BYTES: Final = 64 * 1024

_COLUMNS: Final = (
    "id, provider, account_id, from_address, display_name, reply_to_address, alias_verified,"
    " alias_verified_at, secret_envelope is not null as has_secret_envelope, secret_reference, health,"
    " health_checked_at, health_detail, verified_at, verified_by, revoked_at, revoke_reason, version,"
    " created_at, updated_at"
)
_SELECT: Final = f"select {_COLUMNS} from ops.email_sender_bindings"  # noqa: S608 - fixed columns
_SELECT_WITH_SECRET: Final = f"select secret_envelope, {_COLUMNS} from ops.email_sender_bindings"  # noqa: S608


class SenderBindingRecord(BaseModel):
    """A sender binding WITHOUT any secret material (``has_secret`` only)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: UUID
    provider: EmailProviderKind
    account_id: str
    from_address: str
    display_name: str
    reply_to_address: str | None = None
    alias_verified: bool
    alias_verified_at: datetime | None = None
    has_secret_envelope: bool
    secret_reference: str | None = None
    health: Health
    health_checked_at: datetime | None = None
    health_detail: str | None = None
    verified_at: datetime | None = None
    verified_by: UUID | None = None
    revoked_at: datetime | None = None
    revoke_reason: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "alias_verified_at", "health_checked_at", "verified_at", "revoked_at", "created_at", "updated_at"
    )
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @property
    def has_secret(self) -> bool:
        return self.has_secret_envelope or self.secret_reference is not None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def usable(self) -> bool:
        """Verified, alias-verified, healthy and not revoked (the dispatch guard's conditions)."""
        return (
            not self.revoked
            and self.verified_at is not None
            and self.alias_verified
            and self.health == "healthy"
        )

    def sender_binding(self) -> SenderBinding:
        """The exact sender snapshot a reservation binds (``domain.inquiries.SenderBinding``)."""
        return SenderBinding(
            binding_id=self.id,
            binding_version=self.version,
            provider=self.provider,
            account_id=self.account_id,
            from_address=self.from_address,
            display_name=self.display_name,
            reply_to_address=self.reply_to_address,
        )


def sender_status(
    binding: SenderBindingRecord | None, *, mode: SenderMode, kill_switch: bool
) -> SenderStatus:
    """``domain.inquiries.SenderStatus`` from the binding and the workspace controls."""
    if binding is None:
        return SenderStatus(mode=mode, kill_switch=kill_switch)
    return SenderStatus(
        mode=mode,
        kill_switch=kill_switch,
        provider=binding.provider,
        binding_id=binding.id,
        binding_version=binding.version,
        account_id=binding.account_id,
        from_address=binding.from_address,
        display_name=binding.display_name,
        reply_to_address=binding.reply_to_address,
        alias_verified=binding.alias_verified,
        verified_at=binding.verified_at,
        health_ok=binding.health == "healthy",
        credentials_revoked=binding.revoked,
    )


# =============================================================================================
# Helpers
# =============================================================================================


def secret_reference_for(binding_id: UUID) -> str:
    """``secretbox:ops.email_sender_bindings/<id>``: the value of SELLER_EMAIL_OAUTH_SECRET_REFERENCE."""
    return f"{SECRET_REFERENCE_SCHEME}:{SECRET_REFERENCE_TABLE}/{binding_id}"


def parse_secret_reference(reference: str) -> UUID:
    """The binding id of a ``secretbox:`` reference (``ValidationFailed`` for anything else)."""
    match = _REFERENCE_RE.fullmatch(reference.strip()) if isinstance(reference, str) else None
    if match is None:
        raise ValidationFailed(
            "the secret reference is not a secretbox:ops.email_sender_bindings/<id> reference",
            details={"problems": ["SECRET_REFERENCE_UNSUPPORTED"]},
        )
    return UUID(match.group("id"))


def binding_secret_aad(
    *, workspace_id: UUID, binding_id: UUID, provider: EmailProviderKind, account_id: str
) -> bytes:
    """Associated data: the envelope opens only for exactly this workspace/binding/account."""
    return (
        f"email_sender_binding\x00{workspace_id}\x00{binding_id}\x00{provider.value}\x00{account_id}".encode()
    )


def _canonical(address: str | None, what: str) -> str | None:
    if address is None:
        return None
    try:
        return canonicalize_address(address).canonical
    except AddressError as exc:
        raise ValidationFailed(f"the {what} address is not a canonical e-mail address") from exc


def _reason(reason: str) -> str:
    text = " ".join(str(reason).split())[:500]
    if len(text) < 3:
        raise ValidationFailed("a reason of at least 3 characters is required")
    return text


def _detail(detail: str | None) -> str | None:
    if detail is None:
        return None
    cleaned = _DETAIL_RE.sub("_", detail)[:500].strip()
    return cleaned or None


async def _locked(conn: Conn, actor: ActorContext, binding_id: UUID) -> SenderBindingRecord:
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _SELECT + " where workspace_id = %(ws)s and id = %(id)s for update",
            {"ws": actor.workspace_id, "id": binding_id},
        )
    if row is None:
        raise NotFound("Sender binding not found")
    return SenderBindingRecord.model_validate(row)


def _expect(record: SenderBindingRecord, expected_version: int | None) -> None:
    if record.revoked:
        raise VersionConflict("The sender binding is revoked", reason="sender_binding_revoked")
    if expected_version is not None and record.version != expected_version:
        raise VersionConflict("The sender binding changed; reload and retry", current_version=record.version)


# =============================================================================================
# Reads
# =============================================================================================


async def get_binding(conn: Conn, actor: ActorContext, binding_id: UUID) -> SenderBindingRecord:
    require_inquiry_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _SELECT + " where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": binding_id},
        )
    if row is None:
        raise NotFound("Sender binding not found")
    return SenderBindingRecord.model_validate(row)


async def list_bindings(
    conn: Conn, actor: ActorContext, *, include_revoked: bool = False
) -> list[SenderBindingRecord]:
    require_inquiry_reader(actor)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _SELECT + " where workspace_id = %(ws)s and (%(all)s or revoked_at is null)"
            " order by created_at desc, id desc limit 100",
            {"ws": actor.workspace_id, "all": include_revoked},
        )
    return [SenderBindingRecord.model_validate(r) for r in rows]


async def active_binding(
    conn: Conn, actor: ActorContext, *, provider: EmailProviderKind | None = None
) -> SenderBindingRecord | None:
    """The newest unrevoked binding (optionally of one provider)."""
    require_inquiry_reader(actor)
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _SELECT + " where workspace_id = %(ws)s and revoked_at is null"
            " and (%(provider)s::text is null or provider = %(provider)s)"
            " order by created_at desc, id desc limit 1",
            {"ws": actor.workspace_id, "provider": provider.value if provider else None},
        )
    return None if row is None else SenderBindingRecord.model_validate(row)


# =============================================================================================
# Writes (owner setup flow; never a message approval)
# =============================================================================================


async def create_binding(
    conn: Conn,
    actor: ActorContext,
    *,
    provider: EmailProviderKind,
    account_id: str,
    from_address: str,
    display_name: str,
    reply_to_address: str | None = None,
    reason: str,
) -> SenderBindingRecord:
    """Register an owner-authorized sending identity (unverified, health ``unknown``)."""
    require_inquiry_writer(actor)
    text = _reason(reason)
    sender = _canonical(from_address, "From")
    reply_to = _canonical(reply_to_address, "Reply-To")
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "insert into ops.email_sender_bindings (workspace_id, provider, account_id, from_address,"
            " display_name, reply_to_address, created_by)"
            " values (%(ws)s, %(provider)s, %(account)s, %(from)s, %(name)s, %(reply_to)s, %(by)s)"
            " returning id",
            {
                "ws": actor.workspace_id,
                "provider": EmailProviderKind(provider).value,
                "account": account_id,
                "from": sender,
                "name": display_name,
                "reply_to": reply_to,
                "by": actor.principal_id,
            },
        )
    assert row is not None
    binding_id: UUID = row["id"]
    await audit.record(
        conn,
        actor,
        "sender_binding.create",
        "email_sender_binding",
        binding_id,
        new_version=1,
        reason=text,
        metadata={"provider": EmailProviderKind(provider).value},
    )
    return await get_binding(conn, actor, binding_id)


async def update_identity(
    conn: Conn,
    actor: ActorContext,
    binding_id: UUID,
    *,
    expected_version: int,
    display_name: str,
    reply_to_address: str | None,
    reason: str,
) -> SenderBindingRecord:
    """Change the display name / Reply-To alias. Verification is reset (re-verify first)."""
    require_inquiry_writer(actor)
    text = _reason(reason)
    record = await _locked(conn, actor, binding_id)
    _expect(record, expected_version)
    async with mapped_errors():
        await conn.execute(
            "update ops.email_sender_bindings set display_name = %(name)s, reply_to_address = %(reply_to)s,"
            " alias_verified = false, alias_verified_at = null, verified_at = null, verified_by = null,"
            " version = version + 1 where workspace_id = %(ws)s and id = %(id)s",
            {
                "ws": actor.workspace_id,
                "id": binding_id,
                "name": display_name,
                "reply_to": _canonical(reply_to_address, "Reply-To"),
            },
        )
    await audit.record(
        conn,
        actor,
        "sender_binding.update",
        "email_sender_binding",
        binding_id,
        prior_version=record.version,
        new_version=record.version + 1,
        reason=text,
    )
    return await get_binding(conn, actor, binding_id)


async def record_verification(
    conn: Conn,
    actor: ActorContext,
    binding_id: UUID,
    *,
    expected_version: int,
    verified: bool,
    alias_verified: bool,
    health: Health,
    health_detail: str | None = None,
    reason: str,
) -> SenderBindingRecord:
    """Record the technical verification result (``SenderVerification``); advances the version.

    A technical prerequisite of automatic sending, not a message approval.
    """
    require_inquiry_writer(actor)
    text = _reason(reason)
    record = await _locked(conn, actor, binding_id)
    _expect(record, expected_version)
    async with mapped_errors():
        await conn.execute(
            "update ops.email_sender_bindings set"
            " alias_verified = %(alias)s,"
            " alias_verified_at = case when %(alias)s then now() else null end,"
            " verified_at = case when %(verified)s then now() else null end,"
            " verified_by = case when %(verified)s then %(by)s::uuid else null end,"
            " health = %(health)s, health_checked_at = now(), health_detail = %(detail)s,"
            " version = version + 1 where workspace_id = %(ws)s and id = %(id)s",
            {
                "ws": actor.workspace_id,
                "id": binding_id,
                "alias": bool(alias_verified),
                "verified": bool(verified),
                "by": actor.principal_id,
                "health": health,
                "detail": _detail(health_detail),
            },
        )
    await audit.record(
        conn,
        actor,
        "sender_binding.verify",
        "email_sender_binding",
        binding_id,
        prior_version=record.version,
        new_version=record.version + 1,
        reason=text,
        metadata={"verified": verified, "alias_verified": alias_verified, "health": health},
    )
    return await get_binding(conn, actor, binding_id)


async def record_health(
    conn: Conn, actor: ActorContext, binding_id: UUID, *, health: Health, detail: str | None = None
) -> SenderBindingRecord:
    """Health observation (no version change; the dispatch guard requires ``healthy``)."""
    require_inquiry_writer(actor)
    record = await _locked(conn, actor, binding_id)
    if record.revoked:
        return record
    async with mapped_errors():
        await conn.execute(
            "update ops.email_sender_bindings set health = %(health)s, health_checked_at = now(),"
            " health_detail = %(detail)s where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": binding_id, "health": health, "detail": _detail(detail)},
        )
    return await get_binding(conn, actor, binding_id)


class OAuthRefreshGrant(BaseModel):
    """The sealed OAuth material of an API binding (never logged, never returned)."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    client_id: str = Field(min_length=1, max_length=512, pattern=r"^[\x21-\x7e]+$")
    client_secret: SecretStr
    refresh_token: SecretStr

    @field_validator("client_secret", "refresh_token")
    @classmethod
    def _printable(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw or len(raw) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in raw):
            raise ValueError("OAuth secrets are printable ASCII without whitespace")
        return value

    def sealed_bytes(self) -> bytes:
        return json.dumps(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret.get_secret_value(),
                "refresh_token": self.refresh_token.get_secret_value(),
            },
            separators=(",", ":"),
        ).encode("utf-8")


async def store_secret(
    conn: Conn,
    actor: ActorContext,
    binding_id: UUID,
    *,
    grant: OAuthRefreshGrant,
    box: SecretBox,
    expected_version: int,
    reason: str,
) -> SenderBindingRecord:
    """Seal the OAuth grant into ``secret_envelope`` (AAD-bound); advances the version."""
    require_inquiry_writer(actor)
    text = _reason(reason)
    record = await _locked(conn, actor, binding_id)
    _expect(record, expected_version)
    if record.provider == EmailProviderKind.OUTLOOK_LOCAL:
        raise ValidationFailed("the local Outlook route stores no credential on the server")
    envelope = box.seal(
        grant.sealed_bytes(),
        aad=binding_secret_aad(
            workspace_id=actor.workspace_id,
            binding_id=binding_id,
            provider=record.provider,
            account_id=record.account_id,
        ),
    )
    async with mapped_errors():
        await conn.execute(
            "update ops.email_sender_bindings set secret_envelope = %(envelope)s, secret_reference = null,"
            " version = version + 1 where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": binding_id, "envelope": envelope},
        )
    await audit.record(
        conn,
        actor,
        "sender_binding.secret_store",
        "email_sender_binding",
        binding_id,
        prior_version=record.version,
        new_version=record.version + 1,
        reason=text,
        metadata={"key_id": box.current_key_id},
    )
    return await get_binding(conn, actor, binding_id)


async def set_secret_reference(
    conn: Conn,
    actor: ActorContext,
    binding_id: UUID,
    *,
    reference: str,
    expected_version: int,
    reason: str,
) -> SenderBindingRecord:
    """Point the binding at an external secret store (``scheme:path``; never a raw token)."""
    require_inquiry_writer(actor)
    text = _reason(reason)
    if not _EXTERNAL_REFERENCE_RE.fullmatch(reference) or reference.startswith(f"{SECRET_REFERENCE_SCHEME}:"):
        raise ValidationFailed("the secret reference must be an external scheme:path reference")
    record = await _locked(conn, actor, binding_id)
    _expect(record, expected_version)
    async with mapped_errors():
        await conn.execute(
            "update ops.email_sender_bindings set secret_reference = %(ref)s, secret_envelope = null,"
            " version = version + 1 where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": binding_id, "ref": reference},
        )
    await audit.record(
        conn,
        actor,
        "sender_binding.secret_reference",
        "email_sender_binding",
        binding_id,
        prior_version=record.version,
        new_version=record.version + 1,
        reason=text,
    )
    return await get_binding(conn, actor, binding_id)


async def revoke_binding(
    conn: Conn, actor: ActorContext, binding_id: UUID, *, reason: str
) -> SenderBindingRecord:
    """Permanently revoke (the database freezes the row); queued inquiries then fail the guard."""
    require_inquiry_writer(actor)
    text = _reason(reason)
    record = await _locked(conn, actor, binding_id)
    if record.revoked:
        return record
    async with mapped_errors():
        await conn.execute(
            "update ops.email_sender_bindings set revoked_at = now(), revoked_by = %(by)s,"
            " revoke_reason = %(reason)s, version = version + 1 where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": binding_id, "by": actor.principal_id, "reason": text},
        )
    await audit.record(
        conn,
        actor,
        "sender_binding.revoke",
        "email_sender_binding",
        binding_id,
        prior_version=record.version,
        new_version=record.version + 1,
        reason=text,
    )
    return await get_binding(conn, actor, binding_id)


async def open_sealed_grant(
    conn: Conn, actor: ActorContext, binding_id: UUID, *, box: SecretBox
) -> tuple[SenderBindingRecord, OAuthRefreshGrant]:
    """Server-side only: the binding and its opened OAuth grant (system principals only)."""
    if actor.principal_kind != "system":
        raise ValidationFailed("sealed credentials are opened only by system processes")
    async with mapped_errors():
        row = await fetch_one(
            conn,
            _SELECT.replace("select ", "select secret_envelope, ", 1)
            + " where workspace_id = %(ws)s and id = %(id)s",
            {"ws": actor.workspace_id, "id": binding_id},
        )
    if row is None:
        raise NotFound("Sender binding not found")
    data = dict(row)
    envelope = data.pop("secret_envelope")
    record = SenderBindingRecord.model_validate(data)
    if record.revoked:
        raise TokenUnavailable(revoked=True, code="binding_revoked")
    if envelope is None:
        raise TokenUnavailable(revoked=False, code="secret_missing")
    try:
        plaintext = box.open(
            bytes(envelope),
            aad=binding_secret_aad(
                workspace_id=actor.workspace_id,
                binding_id=binding_id,
                provider=record.provider,
                account_id=record.account_id,
            ),
        )
        grant = OAuthRefreshGrant.model_validate(json.loads(plaintext.decode("utf-8")))
    except (SecretBoxError, ValidationError, ValueError, UnicodeDecodeError):
        raise TokenUnavailable(revoked=False, code="secret_unreadable") from None
    return record, grant


# =============================================================================================
# Server-side TokenProvider for gmail_api
# =============================================================================================


class BindingTokenProvider:
    """``TokenProvider`` resolving ``SELLER_EMAIL_OAUTH_SECRET_REFERENCE`` (``secretbox:``).

    Reads and opens the sealed grant in a short system transaction, then (outside it) exchanges
    the refresh token at ``GOOGLE_TOKEN_URL`` with the injected client. Tokens are cached in
    memory until ``TOKEN_REFRESH_MARGIN`` before expiry; nothing is logged or persisted.
    """

    def __init__(
        self,
        *,
        db: Database,
        workspace_id: UUID,
        reference: str,
        box: SecretBox,
        http: httpx.AsyncClient,
        clock: Clock | None = None,
        system_actor: ActorContext | None = None,
    ) -> None:
        self._db = db
        self._binding_id = parse_secret_reference(reference)
        self._box = box
        self._http = http
        self._clock = clock or SystemClock()
        self._actor = system_actor or ActorContext.system(workspace_id, request_id="token-provider")
        if self._actor.workspace_id != workspace_id or self._actor.principal_kind != "system":
            raise ValidationFailed("the token provider acts as the workspace's system principal")
        self._cached: AccessToken | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"BindingTokenProvider(binding_id={self._binding_id})"

    @property
    def binding_id(self) -> UUID:
        return self._binding_id

    async def access_token(self, *, force_refresh: bool = False) -> AccessToken:
        async with self._lock:
            now = self._clock.now()
            cached = self._cached
            if (
                not force_refresh
                and cached is not None
                and cached.expires_at is not None
                and cached.expires_at - TOKEN_REFRESH_MARGIN > now
            ):
                return cached
            try:
                async with self._db.transaction(self._actor) as conn:
                    record, grant = await open_sealed_grant(
                        conn, self._actor, self._binding_id, box=self._box
                    )
            except AppError as exc:
                raise TokenUnavailable(revoked=False, code="secret_store_unavailable") from exc
            if record.provider != EmailProviderKind.GMAIL_API:
                raise TokenUnavailable(revoked=False, code="provider_not_supported")
            token = await self._exchange(grant)
            self._cached = token
            return token

    async def _exchange(self, grant: OAuthRefreshGrant) -> AccessToken:
        form = {
            "grant_type": "refresh_token",
            "client_id": grant.client_id,
            "client_secret": grant.client_secret.get_secret_value(),
            "refresh_token": grant.refresh_token.get_secret_value(),
        }
        try:
            response = await self._http.post(
                GOOGLE_TOKEN_URL,
                data=form,
                headers={"Accept": "application/json"},
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise TokenUnavailable(revoked=False, code="token_endpoint_unreachable") from None
        body: Mapping[str, Any] = {}
        if len(response.content) <= _MAX_TOKEN_RESPONSE_BYTES:
            try:
                parsed = response.json()
            except ValueError:
                parsed = {}
            if isinstance(parsed, Mapping):
                body = parsed
        if response.status_code != 200:
            error = body.get("error")
            if response.status_code in (400, 401) and error == "invalid_grant":
                await self._mark_unhealthy()
                raise TokenUnavailable(revoked=True, code="invalid_grant")
            code = error if isinstance(error, str) and re.fullmatch(r"[a-z_]{1,40}", error) else "error"
            raise TokenUnavailable(revoked=False, code=f"token_endpoint_{response.status_code}_{code}")
        token = body.get("access_token")
        expires_in = body.get("expires_in")
        scope = body.get("scope")
        if not isinstance(token, str) or not isinstance(expires_in, int) or isinstance(expires_in, bool):
            raise TokenUnavailable(revoked=False, code="token_response_invalid")
        try:
            return AccessToken(
                token=SecretStr(token),
                scopes=frozenset(scope.split()) if isinstance(scope, str) else frozenset(),
                expires_at=self._clock.now() + timedelta(seconds=max(0, min(expires_in, 86_400))),
            )
        except ValidationError:
            raise TokenUnavailable(revoked=False, code="token_response_invalid") from None

    async def _mark_unhealthy(self) -> None:
        try:
            async with self._db.transaction(self._actor) as conn:
                await record_health(
                    conn, self._actor, self._binding_id, health="unhealthy", detail="CREDENTIALS_REVOKED"
                )
        except AppError:
            return  # best effort: the token refusal itself already stops sending


__all__ = [
    "GOOGLE_TOKEN_URL",
    "SECRET_REFERENCE_SCHEME",
    "BindingTokenProvider",
    "Health",
    "OAuthRefreshGrant",
    "SenderBindingRecord",
    "active_binding",
    "binding_secret_aad",
    "create_binding",
    "get_binding",
    "list_bindings",
    "open_sealed_grant",
    "parse_secret_reference",
    "record_health",
    "record_verification",
    "revoke_binding",
    "secret_reference_for",
    "sender_status",
    "set_secret_reference",
    "store_secret",
    "update_identity",
]
