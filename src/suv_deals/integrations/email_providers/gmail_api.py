"""Gmail API sender adapter (optional alternative to the local Outlook route; ADR 0002 addendum).

REST contract (official Google documentation, checked 2026-10-07; no live call was made):

* ``POST https://gmail.googleapis.com/gmail/v1/users/{userId}/messages/send`` with a JSON
  ``Message`` whose ``raw`` field is the RFC 2822 message encoded as base64url; the response is
  a ``Message`` resource (``id``, ``threadId``, ``labelIds``). ``userId`` is "the user's email
  address. The special value ``me`` can be used to indicate the authenticated user." This
  adapter always uses the *configured account address* as ``userId`` (never ``me``), so a token
  issued for any other account is refused by Google instead of silently sending from it, and
  additionally pre-checks ``users.getProfile`` with the same token before every send.
* Threading: Google adds a message to an existing thread only when ``threadId`` is set, the
  ``References``/``In-Reply-To`` headers follow RFC 2822 and the ``Subject`` matches. A seller
  inquiry is always a *new* initial message (follow-ups and replies are not authorized), so no
  ``threadId`` is sent and the MIME builder forbids ``In-Reply-To``/``References``. The
  ``threadId`` Gmail returns is recorded for reply correlation; a local Outlook conversation id
  is never used as a Gmail thread id.
* ``GET .../users/{userId}/settings/sendAs`` lists send-as aliases (``sendAsEmail``,
  ``displayName``, ``replyToAddress``, ``isPrimary``, ``verificationStatus`` = ``accepted`` |
  ``pending``; verification applies to custom "from" aliases only, the primary address is
  always usable).
* ``GET .../users/{userId}/messages?q=rfc822msgid:<id>`` and ``messages.get`` with
  ``format=metadata`` reconcile an uncertain send (``q`` is not available with the
  ``gmail.metadata`` scope). ``GET .../users/{userId}/history`` (``startHistoryId``;
  HTTP 404 for an expired id -> full re-sync) catches up on new messages for reply retrieval.
* Errors: 400 badRequest, 401 authError (refresh the token), 403 dailyLimitExceeded /
  rateLimitExceeded / userRateLimitExceeded / domainPolicy / insufficient permissions, 429
  (per-user mail sending, bandwidth and concurrency limits), 500-504 backendError.

Minimum OAuth scopes (justification):

* ``https://www.googleapis.com/auth/gmail.send`` - send only; cannot read the mailbox.
* ``https://www.googleapis.com/auth/gmail.readonly`` - read-only access needed for
  ``settings.sendAs.list`` (alias verification), ``users.getProfile`` (account binding),
  ``rfc822msgid:`` search (reconciliation; ``gmail.metadata`` does not support ``q``) and,
  only when ``SELLER_REPLY_INGEST_MODE=provider_api``, reading correlated reply bodies. It
  cannot send, modify, delete or change settings.
* Not requested: ``gmail.modify``, ``gmail.compose``, ``https://mail.google.com/`` (broader
  than needed; reported as a verification warning if granted).

Google classifies ``gmail.readonly`` as a restricted scope; consent-screen publishing status
and refresh-token lifetime for a personal project are UNVERIFIED here and must be checked at
activation (docs/seller_email_activation.md).

Send classification (spec 37.5): 2xx -> accepted (ids only as returned); 400/401/403
(non-rate-limit)/404/413 -> definite, nothing sent; 401 is retryable after a token refresh;
connection failures before any request byte -> definite pre-submission, retryable;
429, rate-limit 403s, 5xx, 3xx, other statuses, read timeouts, write interruptions and
connection resets after transmission may have started -> uncertain (hold and reconcile).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from suv_deals.clock import Clock, SystemClock
from suv_deals.domain.enums import EmailProviderKind, Tristate
from suv_deals.domain.inquiries import SenderBinding
from suv_deals.domain.replies import (
    MAX_RAW_BODY_CHARS,
    InboundMessage,
    InquiryBinding,
    InquiryBindingState,
    MessageHeaders,
    SourceMessageIdentity,
    correlate_reply,
    normalize_message_id,
    parse_address_list,
    parse_message_id_list,
)
from suv_deals.errors import ValidationFailed
from suv_deals.integrations.email_providers.base import (
    USABLE_ALIAS_STATUSES,
    AccessToken,
    AliasStatus,
    CorrelatedReply,
    HttpResult,
    ProviderAttachmentRef,
    ProviderCapabilities,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderReceipt,
    ReconcileFoundSent,
    ReconcileNotFoundYet,
    ReconcileProviderUnavailable,
    ReconcileWindow,
    ReplyFetchResult,
    SendAccepted,
    SendDefiniteFailure,
    SenderVerification,
    SendFailureReason,
    SendUncertain,
    SentEvidence,
    TokenProvider,
    TokenUnavailable,
    UncertainReason,
    canonical_or_none,
    http_call,
    local_refusal,
    normalize_search_ids,
    parse_retry_after,
    safe_opaque_id,
    safe_token,
    same_account_address,
    send_precondition_problems,
    uncertain_from_transport,
)
from suv_deals.integrations.mime_builder import BuiltMessage, parse_inquiry_message_id

GMAIL_API_ROOT: Final = "https://gmail.googleapis.com/gmail/v1"
GMAIL_HOST: Final = "gmail.googleapis.com"
SCOPE_GMAIL_SEND: Final = "https://www.googleapis.com/auth/gmail.send"
SCOPE_GMAIL_READONLY: Final = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_GMAIL_METADATA: Final = "https://www.googleapis.com/auth/gmail.metadata"
SCOPE_GMAIL_COMPOSE: Final = "https://www.googleapis.com/auth/gmail.compose"
SCOPE_GMAIL_MODIFY: Final = "https://www.googleapis.com/auth/gmail.modify"
SCOPE_GMAIL_INSERT: Final = "https://www.googleapis.com/auth/gmail.insert"
SCOPE_GMAIL_SETTINGS_SHARING: Final = "https://www.googleapis.com/auth/gmail.settings.sharing"
SCOPE_MAIL_FULL: Final = "https://mail.google.com/"
REQUIRED_GMAIL_SCOPES: Final = (SCOPE_GMAIL_SEND, SCOPE_GMAIL_READONLY)
_SEND_SCOPES: Final = frozenset({SCOPE_GMAIL_SEND, SCOPE_GMAIL_COMPOSE, SCOPE_GMAIL_MODIFY, SCOPE_MAIL_FULL})
_READ_SCOPES: Final = frozenset({SCOPE_GMAIL_READONLY, SCOPE_GMAIL_MODIFY, SCOPE_MAIL_FULL})
_BROADER_THAN_NEEDED: Final = frozenset(
    {
        SCOPE_GMAIL_COMPOSE,
        SCOPE_GMAIL_MODIFY,
        SCOPE_MAIL_FULL,
        SCOPE_GMAIL_INSERT,
        SCOPE_GMAIL_SETTINGS_SHARING,
    }
)
_RATE_LIMIT_REASONS: Final = frozenset(
    {
        "ratelimitexceeded",
        "userratelimitexceeded",
        "dailylimitexceeded",
        "quotaexceeded",
        "resource_exhausted",
        "rate_limit_exceeded",
    }
)
_SCOPE_REASONS: Final = frozenset({"insufficientpermissions", "access_token_scope_insufficient"})
#: Headers read for reply pre-filtering/classification (everything else is never fetched).
REPLY_HEADER_NAMES: Final = (
    "From",
    "To",
    "Subject",
    "Date",
    "Message-ID",
    "In-Reply-To",
    "References",
    "Auto-Submitted",
    "X-Autoreply",
    "X-Autorespond",
    "X-Autoresponder",
    "X-Auto-Reply",
    "Precedence",
    "X-Spam-Flag",
    "X-Spam-Status",
    "Content-Type",
    "X-Failed-Recipients",
)
_REPLY_HEADER_KEYS: Final = frozenset(name.lower() for name in REPLY_HEADER_NAMES)
_HISTORY_ID_RE: Final = re.compile(r"^[0-9]{1,20}$")
_LABEL_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_FETCH_MESSAGES: Final = 500
MAX_PART_DEPTH: Final = 12
MAX_PARTS: Final = 200

CAPABILITIES: Final = ProviderCapabilities(
    kind=EmailProviderKind.GMAIL_API,
    delivery_mode="direct_api",
    returns_provider_message_id=True,
    returns_thread_id=True,
    preserves_client_message_id=Tristate.UNKNOWN,
    documented_send_idempotency=False,
    reconcile_by_rfc_message_id=Tristate.YES,
    alias_verification="api",
    reply_retrieval="provider_pull",
    required_scopes=REQUIRED_GMAIL_SCOPES,
    unverified_offline=(
        "preserves_client_message_id",
        "search_index_lag",
        "oauth_consent_publishing_status",
        "refresh_token_lifetime",
    ),
)


class GmailApiSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    api_root: str = GMAIL_API_ROOT
    request_timeout_s: float = Field(default=20.0, gt=0, le=120)
    send_timeout_s: float = Field(default=30.0, gt=0, le=120)
    max_response_bytes: int = Field(default=1024 * 1024, ge=4096, le=16 * 1024 * 1024)
    max_body_bytes: int = Field(default=256 * 1024, ge=1024, le=MAX_RAW_BODY_CHARS)
    max_full_message_bytes: int = Field(default=8 * 1024 * 1024, ge=64 * 1024, le=32 * 1024 * 1024)
    max_history_pages: int = Field(default=10, ge=1, le=100)
    max_list_pages: int = Field(default=5, ge=1, le=50)
    default_max_messages: int = Field(default=200, ge=1, le=MAX_FETCH_MESSAGES)

    @field_validator("api_root")
    @classmethod
    def _root(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.hostname != GMAIL_HOST or parts.port is not None:
            raise ValueError("the Gmail API root must be https://gmail.googleapis.com/...")
        if parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("the Gmail API root must be a plain URL")
        return value.rstrip("/")


def _error_reasons(data: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Machine reasons from a Google error body (``errors[].reason``, ``status``, ``details``)."""
    if not data:
        return ()
    error = data.get("error")
    if not isinstance(error, Mapping):
        return ()
    found: list[str] = []
    for item in error.get("errors") or ():
        if isinstance(item, Mapping) and isinstance(item.get("reason"), str):
            found.append(item["reason"])
    for item in error.get("details") or ():
        if isinstance(item, Mapping) and isinstance(item.get("reason"), str):
            found.append(item["reason"])
    if isinstance(error.get("status"), str):
        found.append(error["status"])
    return tuple(safe_token(reason) for reason in found[:8])


def _header_map(payload: Mapping[str, Any] | None, *, allowed: frozenset[str]) -> dict[str, list[str]]:
    collected: dict[str, list[str]] = {}
    if not isinstance(payload, Mapping):
        return collected
    for item in payload.get("headers") or ():
        if not isinstance(item, Mapping):
            continue
        name, value = item.get("name"), item.get("value")
        if isinstance(name, str) and isinstance(value, str) and name.lower() in allowed:
            collected.setdefault(name, []).append(value)
    return collected


def _labels(data: Mapping[str, Any]) -> tuple[str, ...]:
    raw = data.get("labelIds")
    if not isinstance(raw, list):
        return ()
    return tuple(label for label in raw[:50] if isinstance(label, str) and _LABEL_RE.fullmatch(label))


def _internal_date(data: Mapping[str, Any]) -> datetime | None:
    raw = data.get("internalDate")
    if isinstance(raw, str) and raw.isdigit() and len(raw) <= 16:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
    return None


def _b64url_decode(data: str, limit: int) -> tuple[bytes, bool]:
    padded = data + "=" * (-len(data) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return b"", False
    return decoded[:limit], len(decoded) > limit


def _walk_parts(part: Mapping[str, Any], depth: int = 0) -> Iterator[Mapping[str, Any]]:
    if depth > MAX_PART_DEPTH:
        return
    yield part
    children = part.get("parts")
    if isinstance(children, list):
        for child in children[:MAX_PARTS]:
            if isinstance(child, Mapping):
                yield from _walk_parts(child, depth + 1)


def _charset(part: Mapping[str, Any]) -> str:
    for values in _header_map(part, allowed=frozenset({"content-type"})).values():
        match = re.search(r'charset="?([A-Za-z0-9._-]{1,40})"?', values[0])
        if match:
            return match.group(1)
    return "utf-8"


class _ParsedBody(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = ""
    truncated: bool = False
    html_only: bool = False
    attachments: tuple[ProviderAttachmentRef, ...] = ()


def _parse_full_payload(payload: Mapping[str, Any], *, limit: int) -> _ParsedBody:
    text: str | None = None
    truncated = False
    saw_html = False
    attachments: list[ProviderAttachmentRef] = []
    count = 0
    for part in _walk_parts(payload):
        count += 1
        if count > MAX_PARTS:
            break
        raw_mime, raw_body, raw_name = part.get("mimeType"), part.get("body"), part.get("filename")
        mime: str = raw_mime if isinstance(raw_mime, str) else ""
        body: Mapping[str, Any] = raw_body if isinstance(raw_body, Mapping) else {}
        filename: str = raw_name if isinstance(raw_name, str) else ""
        if filename or body.get("attachmentId"):
            size = body.get("size")
            attachments.append(
                ProviderAttachmentRef(
                    filename=filename or "attachment",
                    mime_type=mime or "application/octet-stream",
                    byte_size=size if isinstance(size, int) and size >= 0 else None,
                    provider_attachment_id=safe_opaque_id(body.get("attachmentId")),
                )
            )
            continue
        if mime == "text/html":
            saw_html = True
        if mime == "text/plain" and text is None and isinstance(body.get("data"), str):
            raw, cut = _b64url_decode(body["data"], limit)
            try:
                text = raw.decode(_charset(part), errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            truncated = cut
    return _ParsedBody(
        text=text or "",
        truncated=truncated,
        html_only=text is None and saw_html,
        attachments=tuple(attachments[:50]),
    )


class GmailApiProvider:
    """``SenderProvider`` for one bound Gmail account (see module docstring)."""

    def __init__(
        self,
        *,
        binding: SenderBinding,
        token_provider: TokenProvider,
        http: httpx.AsyncClient,
        clock: Clock | None = None,
        settings: GmailApiSettings | None = None,
    ) -> None:
        if binding.provider != EmailProviderKind.GMAIL_API:
            raise ValidationFailed("binding is not a Gmail API binding")
        account = canonical_or_none(binding.account_id)
        if account is None:
            raise ValidationFailed("the Gmail account id must be the account's email address")
        self._binding = binding
        self._account = account
        self._tokens = token_provider
        self._http = http
        self._clock = clock or SystemClock()
        self._settings = settings or GmailApiSettings()
        self._user_root = f"{self._settings.api_root}/users/{quote(account, safe='@.+-_')}"

    # -- protocol properties --------------------------------------------------------------------

    @property
    def kind(self) -> EmailProviderKind:
        return EmailProviderKind.GMAIL_API

    @property
    def binding(self) -> SenderBinding:
        return self._binding

    @property
    def capabilities(self) -> ProviderCapabilities:
        return CAPABILITIES

    def __repr__(self) -> str:
        return f"GmailApiProvider(binding_id={self._binding.binding_id})"

    # -- helpers --------------------------------------------------------------------------------

    def _headers(self, token: AccessToken) -> dict[str, str]:
        return {"Authorization": token.bearer(), "Accept": "application/json"}

    async def _get(
        self, token: AccessToken, path: str, params: Mapping[str, str | int | Sequence[str]] | None = None
    ) -> HttpResult:
        return await http_call(
            self._http,
            "GET",
            f"{self._user_root}{path}",
            headers=self._headers(token),
            params=params,
            timeout_s=self._settings.request_timeout_s,
            max_response_bytes=self._settings.max_response_bytes,
        )

    @staticmethod
    def _scope_gaps(token: AccessToken) -> tuple[list[str], list[str]]:
        granted = set(token.scopes)
        missing: list[str] = []
        if not granted & _SEND_SCOPES:
            missing.append(SCOPE_GMAIL_SEND)
        if not granted & _READ_SCOPES:
            missing.append(SCOPE_GMAIL_READONLY)
        return missing, sorted(granted & _BROADER_THAN_NEEDED)

    def _verification(self, **values: Any) -> SenderVerification:
        base: dict[str, Any] = {
            "provider": EmailProviderKind.GMAIL_API,
            "checked_at": self._clock.now(),
            "configured_account_id": self._binding.account_id,
            "configured_from": self._binding.from_address,
            "configured_reply_to": self._binding.reply_to_address,
            "configured_display_name": self._binding.display_name,
            "reply_to_status": AliasStatus.NOT_CHECKED if self._binding.reply_to_address else None,
            "capabilities": CAPABILITIES,
        }
        base.update(values)
        return SenderVerification(**base)

    # -- verification ---------------------------------------------------------------------------

    async def verify_account(self) -> SenderVerification:
        try:
            token = await self._tokens.access_token()
        except TokenUnavailable as exc:
            return self._verification(
                health=ProviderHealthStatus.CREDENTIALS_REVOKED
                if exc.revoked
                else ProviderHealthStatus.UNAVAILABLE,
                problems=("CREDENTIALS_REVOKED" if exc.revoked else "CREDENTIALS_UNAVAILABLE",),
            )
        problems: list[str] = []
        warnings: list[str] = []
        missing, excess = self._scope_gaps(token)
        if missing:
            problems.append("MISSING_SCOPES")
        if excess:
            warnings.append("SCOPES_BROADER_THAN_NEEDED")
        common: dict[str, Any] = {
            "granted_scopes": tuple(sorted(token.scopes)),
            "missing_scopes": tuple(missing),
            "excess_scopes": tuple(excess),
        }
        profile = await self._get(token, "/profile")
        if profile.status_code is None or (profile.status_code >= 500 or profile.status_code == 429):
            return self._verification(
                **common,
                health=ProviderHealthStatus.UNAVAILABLE,
                problems=(*problems, "PROVIDER_UNAVAILABLE"),
                warnings=tuple(warnings),
            )
        if profile.status_code == 401:
            return self._verification(
                **common,
                health=ProviderHealthStatus.UNAVAILABLE,
                problems=(*problems, "CREDENTIALS_REJECTED"),
                warnings=tuple(warnings),
            )
        data = profile.json() if profile.ok else None
        email = data.get("emailAddress") if data else None
        account_email = canonical_or_none(email) if isinstance(email, str) else None
        if account_email is None:
            problems.append(
                "ACCOUNT_ACCESS_DENIED" if profile.status_code in (403, 404) else "PROVIDER_RESPONSE_INVALID"
            )
        elif not same_account_address(account_email, self._account):
            problems.append("ACCOUNT_MISMATCH")

        from_status = AliasStatus.NOT_CHECKED
        reply_status: AliasStatus | None = AliasStatus.NOT_CHECKED if self._binding.reply_to_address else None
        provider_display: str | None = None
        if account_email is not None and "ACCOUNT_MISMATCH" not in problems:
            send_as = await self._get(token, "/settings/sendAs")
            entries = send_as.json() if send_as.ok else None
            aliases = entries.get("sendAs") if entries else None
            if not isinstance(aliases, list):
                problems.append("ALIAS_CHECK_FAILED")
            else:
                from_status, provider_display, smtp_relay = self._alias_status(
                    aliases, self._binding.from_address
                )
                if smtp_relay:
                    warnings.append("ALIAS_USES_EXTERNAL_SMTP_RELAY")
                if self._binding.reply_to_address:
                    reply_status, _name, _relay = self._alias_status(aliases, self._binding.reply_to_address)
                if provider_display is not None and provider_display != self._binding.display_name:
                    warnings.append("PROVIDER_DISPLAY_NAME_DIFFERS")
        if from_status not in USABLE_ALIAS_STATUSES:
            problems.append(
                "FROM_ALIAS_PENDING" if from_status == AliasStatus.PENDING else "FROM_NOT_VERIFIED"
            )
        if reply_status is not None and reply_status not in USABLE_ALIAS_STATUSES:
            problems.append("REPLY_TO_NOT_VERIFIED")
        return self._verification(
            **common,
            stable_account_id=account_email,
            account_email=account_email,
            provider_display_name=provider_display,
            from_status=from_status,
            reply_to_status=reply_status,
            health=ProviderHealthStatus.OK,
            problems=tuple(dict.fromkeys(problems)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    @staticmethod
    def _alias_status(aliases: list[Any], address: str) -> tuple[AliasStatus, str | None, bool]:
        for entry in aliases[:200]:
            if not isinstance(entry, Mapping):
                continue
            send_as = entry.get("sendAsEmail")
            if not isinstance(send_as, str) or not same_account_address(send_as, address):
                continue
            name = entry.get("displayName") if isinstance(entry.get("displayName"), str) else None
            relay = isinstance(entry.get("smtpMsa"), Mapping)
            if entry.get("isPrimary") is True:
                return AliasStatus.PRIMARY, name, relay
            status = entry.get("verificationStatus")
            if status == "accepted":
                return AliasStatus.VERIFIED_ALIAS, name, relay
            if status == "pending":
                return AliasStatus.PENDING, name, relay
            return AliasStatus.NOT_VERIFIABLE, name, relay
        return AliasStatus.NOT_FOUND, None, False

    # -- send ----------------------------------------------------------------------------------

    def _failure(
        self,
        message: BuiltMessage,
        inquiry_id: UUID,
        attempt_id: UUID,
        *,
        reason: SendFailureReason,
        retryable: bool,
        proof: str,
        http_status: int | None = None,
        provider_error: str | None = None,
        retry_after: int | None = None,
    ) -> SendDefiniteFailure:
        return SendDefiniteFailure.model_validate(
            {
                "provider": EmailProviderKind.GMAIL_API,
                "inquiry_id": inquiry_id,
                "attempt_id": attempt_id,
                "rfc_message_id": message.rfc_message_id,
                "raw_sha256": message.raw_sha256,
                "pre_submission": True,
                "reason": reason,
                "retryable": retryable,
                "proof": proof,
                "http_status": http_status,
                "provider_error": provider_error,
                "retry_after_seconds": retry_after,
            }
        )

    def _uncertain(
        self,
        message: BuiltMessage,
        inquiry_id: UUID,
        attempt_id: UUID,
        *,
        reason: UncertainReason,
        http_status: int | None,
        provider_error: str | None,
        retry_after: int | None,
    ) -> SendUncertain:
        return SendUncertain(
            provider=EmailProviderKind.GMAIL_API,
            inquiry_id=inquiry_id,
            attempt_id=attempt_id,
            rfc_message_id=message.rfc_message_id,
            raw_sha256=message.raw_sha256,
            reason=reason,
            http_status=http_status,
            provider_error=provider_error,
            retry_after_seconds=retry_after,
        )

    async def _precheck_account(
        self, token: AccessToken, message: BuiltMessage, inquiry_id: UUID, attempt_id: UUID
    ) -> SendDefiniteFailure | None:
        """Same-token identity check; any failure here means the send was never issued."""
        profile = await self._get(token, "/profile")
        if profile.status_code == 401:
            return self._failure(
                message,
                inquiry_id,
                attempt_id,
                reason=SendFailureReason.CREDENTIALS_REJECTED,
                retryable=True,
                proof="credentials_rejected_before_submit",
                http_status=401,
            )
        data = profile.json() if profile.ok else None
        email = data.get("emailAddress") if data else None
        if not profile.ok or not isinstance(email, str):
            return self._failure(
                message,
                inquiry_id,
                attempt_id,
                reason=SendFailureReason.PRECHECK_FAILED,
                retryable=profile.status_code not in (400, 403, 404),
                proof="local_validation_failed_before_submit",
                http_status=profile.status_code,
                provider_error=safe_token(profile.failure_detail) if profile.failure_detail else None,
            )
        if not same_account_address(email, self._account):
            return self._failure(
                message,
                inquiry_id,
                attempt_id,
                reason=SendFailureReason.ACCOUNT_MISMATCH,
                retryable=False,
                proof="local_validation_failed_before_submit",
            )
        return None

    async def send(
        self, message: BuiltMessage, *, inquiry_id: UUID, attempt_id: UUID, idempotency_key: str
    ) -> SendAccepted | SendDefiniteFailure | SendUncertain:
        """Submit once. Gmail documents no idempotency key: the key is recorded, never relied on."""
        problems = send_precondition_problems(
            message,
            self._binding,
            provider=EmailProviderKind.GMAIL_API,
            inquiry_id=inquiry_id,
            idempotency_key=idempotency_key,
        )
        if problems:
            return local_refusal(
                message,
                provider=EmailProviderKind.GMAIL_API,
                inquiry_id=inquiry_id,
                attempt_id=attempt_id,
                problems=problems,
            )
        try:
            token = await self._tokens.access_token()
        except TokenUnavailable as exc:
            return self._failure(
                message,
                inquiry_id,
                attempt_id,
                reason=SendFailureReason.CREDENTIALS_REVOKED
                if exc.revoked
                else SendFailureReason.CREDENTIALS_UNAVAILABLE,
                retryable=not exc.revoked,
                proof="credentials_rejected_before_submit",
                provider_error=exc.code,
            )
        if token.scopes and not set(token.scopes) & _SEND_SCOPES:
            return self._failure(
                message,
                inquiry_id,
                attempt_id,
                reason=SendFailureReason.INSUFFICIENT_SCOPE,
                retryable=False,
                proof="local_validation_failed_before_submit",
            )
        refused = await self._precheck_account(token, message, inquiry_id, attempt_id)
        if refused is not None:
            return refused
        result = await http_call(
            self._http,
            "POST",
            f"{self._user_root}/messages/send",
            headers={**self._headers(token), "Content-Type": "application/json"},
            content=_json_bytes({"raw": message.raw_base64url()}),
            timeout_s=self._settings.send_timeout_s,
            max_response_bytes=64 * 1024,
        )
        if result.status_code is None:
            return uncertain_from_transport(
                message,
                provider=EmailProviderKind.GMAIL_API,
                inquiry_id=inquiry_id,
                attempt_id=attempt_id,
                result=result,
            )
        if 200 <= result.status_code < 300:
            return await self._accepted(token, message, inquiry_id, attempt_id, result)
        return self._classify_rejection(message, inquiry_id, attempt_id, result)

    async def _accepted(
        self,
        token: AccessToken,
        message: BuiltMessage,
        inquiry_id: UUID,
        attempt_id: UUID,
        result: HttpResult,
    ) -> SendAccepted:
        data = result.json()
        message_id = safe_opaque_id(data.get("id")) if data else None
        complete = data is not None and message_id is not None
        thread_id = safe_opaque_id(data.get("threadId")) if data and complete else None
        receipt = ProviderReceipt(
            kind="gmail_message_resource",
            http_status=result.status_code,
            label_ids=_labels(data) if data else (),
            response_complete=complete,
            observed_at=self._clock.now(),
        )
        observed: str | None = None
        if message_id is not None:
            observed = await self._read_message_id(token, message_id)
        return SendAccepted(
            provider=EmailProviderKind.GMAIL_API,
            inquiry_id=inquiry_id,
            attempt_id=attempt_id,
            rfc_message_id=message.rfc_message_id,
            raw_sha256=message.raw_sha256,
            observed_rfc_message_id=observed,
            provider_message_id=message_id if complete else None,
            provider_thread_id=thread_id,
            receipt=receipt,
            accepted_at=self._clock.now(),
        )

    async def _read_message_id(self, token: AccessToken, message_id: str) -> str | None:
        """Best effort: the Message-ID Gmail actually stored (it may differ from the client one)."""
        meta = await self._get(
            token,
            f"/messages/{quote(message_id, safe='')}",
            {"format": "metadata", "metadataHeaders": ["Message-ID"]},
        )
        data = meta.json() if meta.ok else None
        if not data:
            return None
        values = _header_map(data.get("payload"), allowed=frozenset({"message-id"}))
        return next((normalize_message_id(found[0]) for found in values.values()), None)

    def _classify_rejection(
        self, message: BuiltMessage, inquiry_id: UUID, attempt_id: UUID, result: HttpResult
    ) -> SendDefiniteFailure | SendUncertain:
        status = result.status_code or 0
        reasons = _error_reasons(result.json())
        lowered = {r.lower() for r in reasons}
        error = reasons[0] if reasons else None
        retry_after = parse_retry_after(result.headers)

        def definite(
            reason: SendFailureReason, *, retryable: bool = False, proof: str
        ) -> SendDefiniteFailure:
            return self._failure(
                message,
                inquiry_id,
                attempt_id,
                reason=reason,
                retryable=retryable,
                proof=proof,
                http_status=status,
                provider_error=error,
                retry_after=retry_after,
            )

        def uncertain(reason: UncertainReason) -> SendUncertain:
            return self._uncertain(
                message,
                inquiry_id,
                attempt_id,
                reason=reason,
                http_status=status,
                provider_error=error,
                retry_after=retry_after,
            )

        documented = "provider_documented_not_sent"
        if status == 400:
            return definite(SendFailureReason.PROVIDER_REJECTED_INVALID, proof=documented)
        if status == 401:
            return definite(
                SendFailureReason.CREDENTIALS_REJECTED,
                retryable=True,
                proof="credentials_rejected_before_submit",
            )
        if status == 403:
            if lowered & _RATE_LIMIT_REASONS:
                return uncertain(UncertainReason.PROVIDER_THROTTLED)
            if lowered & _SCOPE_REASONS:
                return definite(SendFailureReason.INSUFFICIENT_SCOPE, proof=documented)
            return definite(SendFailureReason.ACCOUNT_NOT_PERMITTED, proof=documented)
        if status == 404:
            return definite(SendFailureReason.PROVIDER_NOT_FOUND, proof=documented)
        if status == 413:
            return definite(SendFailureReason.MESSAGE_TOO_LARGE, proof=documented)
        if status == 429:
            return uncertain(UncertainReason.PROVIDER_THROTTLED)
        if status >= 500:
            return uncertain(UncertainReason.PROVIDER_SERVER_ERROR)
        return uncertain(UncertainReason.UNEXPECTED_STATUS)

    # -- reconciliation -------------------------------------------------------------------------

    async def reconcile(
        self,
        *,
        inquiry_id: UUID,
        rfc_message_ids: Sequence[str],
        window: ReconcileWindow,
        provider_message_ids: Sequence[str] = (),
    ) -> ReconcileFoundSent | ReconcileNotFoundYet | ReconcileProviderUnavailable:
        """Search the configured account only. Not finding the message proves nothing."""
        try:
            ids = normalize_search_ids(rfc_message_ids)
        except ValueError as exc:
            raise ValidationFailed("invalid reconciliation Message-IDs") from exc
        pids = tuple(dict.fromkeys(p for p in provider_message_ids if safe_opaque_id(p)))
        if len(pids) != len(set(provider_message_ids)):
            raise ValidationFailed("invalid provider message ids")
        del window  # rfc822msgid search is exact; the window is recorded by the caller

        def unavailable(reason: str, result: HttpResult | None = None) -> ReconcileProviderUnavailable:
            return ReconcileProviderUnavailable(
                provider=EmailProviderKind.GMAIL_API,
                inquiry_id=inquiry_id,
                reason=safe_token(reason),
                http_status=result.status_code if result else None,
                retry_after_seconds=parse_retry_after(result.headers) if result else None,
            )

        try:
            token = await self._tokens.access_token()
        except TokenUnavailable as exc:
            return unavailable("credentials_revoked" if exc.revoked else "credentials_unavailable")
        draft_seen = False
        for rid in ids:
            listing = await self._get(
                token,
                "/messages",
                {"q": f"rfc822msgid:{rid[1:-1]}", "includeSpamTrash": "true", "maxResults": 10},
            )
            data = listing.json() if listing.ok else None
            if data is None:
                return unavailable("search_failed", listing)
            for item in (data.get("messages") or [])[:10]:
                mid = safe_opaque_id(item.get("id")) if isinstance(item, Mapping) else None
                if mid is None:
                    continue
                found, draft, failure = await self._sent_copy(token, mid, expected=rid)
                if failure is not None:
                    return unavailable("metadata_failed", failure)
                draft_seen = draft_seen or draft
                if found is not None:
                    return ReconcileFoundSent(
                        provider=EmailProviderKind.GMAIL_API, inquiry_id=inquiry_id, evidence=found
                    )
        for pid in pids:
            found, draft, failure = await self._sent_copy(token, pid, expected=None)
            if failure is not None:
                return unavailable("metadata_failed", failure)
            draft_seen = draft_seen or draft
            if found is not None:
                return ReconcileFoundSent(
                    provider=EmailProviderKind.GMAIL_API, inquiry_id=inquiry_id, evidence=found
                )
        return ReconcileNotFoundYet(
            provider=EmailProviderKind.GMAIL_API,
            inquiry_id=inquiry_id,
            searched_message_ids=ids,
            searched_provider_ids=pids,
            pending_in_drafts_or_outbox=Tristate.YES if draft_seen else Tristate.UNKNOWN,
        )

    async def _sent_copy(
        self, token: AccessToken, message_id: str, *, expected: str | None
    ) -> tuple[SentEvidence | None, bool, HttpResult | None]:
        meta = await self._get(
            token,
            f"/messages/{quote(message_id, safe='')}",
            {"format": "metadata", "metadataHeaders": ["Message-ID"]},
        )
        if meta.status_code == 404:
            return None, False, None
        data = meta.json() if meta.ok else None
        if data is None:
            return None, False, meta
        labels = _labels(data)
        header_values = _header_map(data.get("payload"), allowed=frozenset({"message-id"}))
        stored = next((normalize_message_id(v[0]) for v in header_values.values()), None)
        if expected is not None and stored != expected:
            return None, False, None
        if "SENT" not in labels:
            return None, "DRAFT" in labels, None
        return (
            SentEvidence(
                matched_by="rfc_message_id" if expected is not None else "provider_message_id",
                location="gmail:SENT",
                rfc_message_id=stored,
                provider_message_id=safe_opaque_id(data.get("id")) or message_id,
                provider_thread_id=safe_opaque_id(data.get("threadId")),
                sent_at=_internal_date(data),
            ),
            False,
            None,
        )

    # -- correlated replies ---------------------------------------------------------------------

    async def fetch_correlated_replies(
        self,
        *,
        mailbox_binding_id: UUID,
        bindings: Sequence[InquiryBinding],
        since: datetime,
        cursor: str | None = None,
        max_messages: int | None = None,
    ) -> ReplyFetchResult:
        """Provider-API reply ingestion: metadata pre-filter, local correlation, minimal bodies.

        ``cursor`` is a Gmail ``historyId``. An expired/unusable cursor (HTTP 404) is a reported
        gap followed by a full re-sync from ``since`` (the caller passes an overlapping window).
        Any incomplete pull keeps the previous cursor (re-read; ingest de-duplicates). More than
        ``max_history_pages`` of history in one pull is reported as incomplete every time and
        needs a larger page budget or a window re-sync.
        """
        limit = min(max_messages or self._settings.default_max_messages, MAX_FETCH_MESSAGES)
        try:
            token = await self._tokens.access_token()
        except TokenUnavailable as exc:
            return ReplyFetchResult(
                provider=EmailProviderKind.GMAIL_API,
                retrieval_mode="provider_pull",
                next_cursor=cursor,
                complete=False,
                problems=("CREDENTIALS_REVOKED" if exc.revoked else "CREDENTIALS_UNAVAILABLE",),
            )
        candidate_ids: list[str] = []
        gap = False
        advance_to: str | None = None
        complete = True
        if cursor is not None and _HISTORY_ID_RE.fullmatch(cursor):
            history = await self._history_ids(token, cursor)
            if history is None:
                return self._unavailable_fetch(cursor)
            ids, latest, complete, expired = history
            if expired:
                gap = True  # history id too old (HTTP 404): full re-sync from the time window
            else:
                candidate_ids = ids
                advance_to = latest
        elif cursor is not None:
            gap = True  # unusable cursor: re-sync from the time window
        if cursor is None or gap:
            window_ids = await self._window_ids(token, since)
            if window_ids is None:
                return self._unavailable_fetch(cursor, gap=gap)
            candidate_ids, advance_to, complete = window_ids
        unique = list(dict.fromkeys(candidate_ids))
        if len(unique) > limit:
            unique = unique[:limit]
            complete = False
        replies: list[CorrelatedReply] = []
        pending: list[str] = []
        skipped = 0
        for message_id in unique:
            item = await self._inspect(token, message_id, mailbox_binding_id, bindings)
            if item is None:
                complete = False
            elif isinstance(item, CorrelatedReply):
                replies.append(item)
            elif item == "pending":
                pending.append(message_id)
            else:
                skipped += 1
        if complete and advance_to is not None:
            next_cursor: str | None = advance_to
        else:
            # Incomplete: keep the old cursor (re-read; ingest de-duplicates) or, after a gap or
            # without a cursor, start again from the caller's overlapping time window.
            next_cursor = cursor if cursor is not None and not gap else None
        return ReplyFetchResult(
            provider=EmailProviderKind.GMAIL_API,
            retrieval_mode="provider_pull",
            replies=tuple(replies),
            pending_retry_locators=tuple(pending[:200]),
            next_cursor=next_cursor,
            complete=complete,
            gap_detected=gap,
            scanned=len(unique),
            skipped_unrelated=skipped,
        )

    def _unavailable_fetch(self, cursor: str | None, *, gap: bool = False) -> ReplyFetchResult:
        return ReplyFetchResult(
            provider=EmailProviderKind.GMAIL_API,
            retrieval_mode="provider_pull",
            next_cursor=cursor,
            complete=False,
            gap_detected=gap,
            problems=("PROVIDER_UNAVAILABLE",),
        )

    async def _history_ids(
        self, token: AccessToken, cursor: str
    ) -> tuple[list[str], str | None, bool, bool] | None:
        """(ids, latest history id, complete, expired) or None when the provider is unavailable."""
        ids: list[str] = []
        latest: str | None = None
        page_token: str | None = None
        for _page in range(self._settings.max_history_pages):
            params: dict[str, str | int | Sequence[str]] = {
                "startHistoryId": cursor,
                "historyTypes": "messageAdded",
                "maxResults": 500,
            }
            if page_token:
                params["pageToken"] = page_token
            result = await self._get(token, "/history", params)
            if result.status_code == 404:
                return [], None, False, True
            data = result.json() if result.ok else None
            if data is None:
                return None
            for record in data.get("history") or []:
                if not isinstance(record, Mapping):
                    continue
                for added in record.get("messagesAdded") or []:
                    message = added.get("message") if isinstance(added, Mapping) else None
                    if not isinstance(message, Mapping):
                        continue
                    labels = _labels(message)
                    mid = safe_opaque_id(message.get("id"))
                    if mid and "SENT" not in labels and "DRAFT" not in labels:
                        ids.append(mid)
            history_id = data.get("historyId")
            if isinstance(history_id, str) and _HISTORY_ID_RE.fullmatch(history_id):
                latest = history_id
            next_token = data.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                return ids, latest, True, False
            page_token = next_token
        return ids, latest, False, False

    async def _window_ids(
        self, token: AccessToken, since: datetime
    ) -> tuple[list[str], str | None, bool] | None:
        profile = await self._get(token, "/profile")  # baseline first: nothing arriving later is lost
        data = profile.json() if profile.ok else None
        if data is None:
            return None
        baseline = data.get("historyId")
        baseline_id = baseline if isinstance(baseline, str) and _HISTORY_ID_RE.fullmatch(baseline) else None
        query = f"after:{int(since.timestamp())} -in:sent -in:drafts"
        ids: list[str] = []
        page_token: str | None = None
        for _page in range(self._settings.max_list_pages):
            params: dict[str, str | int | Sequence[str]] = {
                "q": query,
                "includeSpamTrash": "true",
                "maxResults": 100,
            }
            if page_token:
                params["pageToken"] = page_token
            result = await self._get(token, "/messages", params)
            listing = result.json() if result.ok else None
            if listing is None:
                return None
            for item in listing.get("messages") or []:
                mid = safe_opaque_id(item.get("id")) if isinstance(item, Mapping) else None
                if mid:
                    ids.append(mid)
            next_token = listing.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                return ids, baseline_id, True
            page_token = next_token
        return ids, baseline_id, False

    async def _inspect(
        self,
        token: AccessToken,
        message_id: str,
        mailbox_binding_id: UUID,
        bindings: Sequence[InquiryBinding],
    ) -> CorrelatedReply | str | None:
        """A correlated reply, ``"pending"``, ``"skip"`` or ``None`` (provider unavailable)."""
        path = f"/messages/{quote(message_id, safe='')}"
        meta = await self._get(
            token, path, {"format": "metadata", "metadataHeaders": list(REPLY_HEADER_NAMES)}
        )
        if meta.status_code == 404:
            return "skip"
        data = meta.json() if meta.ok else None
        if data is None:
            return None
        labels = _labels(data)
        if "SENT" in labels or "DRAFT" in labels:
            return "skip"
        headers = MessageHeaders.from_raw(_header_map(data.get("payload"), allowed=_REPLY_HEADER_KEYS))
        own = [b for b in bindings if b.mailbox_binding_id == mailbox_binding_id]
        decision = _prefilter(headers, safe_opaque_id(data.get("threadId")), own)
        if decision != "candidate":
            return decision  # metadata discarded; the body of unrelated mail is never fetched
        full = await http_call(
            self._http,
            "GET",
            f"{self._user_root}{path}",
            headers=self._headers(token),
            params={"format": "full"},
            timeout_s=self._settings.request_timeout_s,
            max_response_bytes=self._settings.max_full_message_bytes,
        )
        if full.status_code == 404:
            return "skip"
        full_data = full.json() if full.ok else None
        if full_data is None:
            if not (full.ok and not full.body_complete):
                return None  # transient: retried by the next pull (cursor not advanced)
            # Oversized message: correlate on the metadata headers alone instead of failing the
            # whole pull forever; the body is flagged as unavailable/truncated for follow-up.
            full_data = {**data, "payload": {"headers": (data.get("payload") or {}).get("headers", [])}}
            oversized = True
        else:
            oversized = False
        raw_payload = full_data.get("payload")
        payload: Mapping[str, Any] = raw_payload if isinstance(raw_payload, Mapping) else {}
        body = _parse_full_payload(payload, limit=self._settings.max_body_bytes)
        full_headers = MessageHeaders.from_raw(_header_map(payload, allowed=_REPLY_HEADER_KEYS))
        inbound = InboundMessage(
            identity=SourceMessageIdentity(
                mailbox_binding_id=mailbox_binding_id,
                provider=EmailProviderKind.GMAIL_API,
                internet_message_id=normalize_message_id(full_headers.get("message-id")),
                provider_message_id=safe_opaque_id(full_data.get("id")) or message_id,
                provider_thread_id=safe_opaque_id(full_data.get("threadId")),
                received_at=_internal_date(full_data),
            ),
            headers=full_headers,
            body_text=body.text,
            in_junk_folder="SPAM" in _labels(full_data),
        )
        correlation = correlate_reply(inbound, bindings)
        if correlation.upload_scope == "none":
            return "skip"  # fetched as a candidate but unrelated after correlation: never returned
        return CorrelatedReply(
            message=inbound,
            correlation=correlation,
            attachment_refs=body.attachments,
            body_truncated=body.truncated or oversized,
            html_only_body=body.html_only,
        )

    async def recheck_locators(
        self,
        *,
        mailbox_binding_id: UUID,
        bindings: Sequence[InquiryBinding],
        locators: Sequence[str],
    ) -> ReplyFetchResult:
        """Re-inspect ``pending_retry_locators`` after a binding sync (reply-before-binding race).

        Same metadata pre-filter and local correlation as a pull; nothing else is read. Locators
        that are still unmatched stay pending (the caller bounds the retry window).
        """
        ids = [lid for lid in dict.fromkeys(locators) if safe_opaque_id(lid)][:MAX_FETCH_MESSAGES]
        try:
            token = await self._tokens.access_token()
        except TokenUnavailable as exc:
            return ReplyFetchResult(
                provider=EmailProviderKind.GMAIL_API,
                retrieval_mode="provider_pull",
                pending_retry_locators=tuple(ids),
                complete=False,
                problems=("CREDENTIALS_REVOKED" if exc.revoked else "CREDENTIALS_UNAVAILABLE",),
            )
        replies: list[CorrelatedReply] = []
        pending: list[str] = []
        skipped = 0
        complete = True
        for message_id in ids:
            item = await self._inspect(token, message_id, mailbox_binding_id, bindings)
            if item is None:
                complete = False
                pending.append(message_id)
            elif isinstance(item, CorrelatedReply):
                replies.append(item)
            elif item == "pending":
                pending.append(message_id)
            else:
                skipped += 1
        return ReplyFetchResult(
            provider=EmailProviderKind.GMAIL_API,
            retrieval_mode="provider_pull",
            replies=tuple(replies),
            pending_retry_locators=tuple(pending),
            complete=complete,
            scanned=len(ids),
            skipped_unrelated=skipped,
        )

    # -- health ---------------------------------------------------------------------------------

    async def health(self) -> ProviderHealth:
        now = self._clock.now()
        try:
            token = await self._tokens.access_token()
        except TokenUnavailable as exc:
            return ProviderHealth(
                provider=EmailProviderKind.GMAIL_API,
                status=ProviderHealthStatus.CREDENTIALS_REVOKED
                if exc.revoked
                else ProviderHealthStatus.UNAVAILABLE,
                checked_at=now,
                problems=("CREDENTIALS_REVOKED" if exc.revoked else "CREDENTIALS_UNAVAILABLE",),
            )
        missing, _excess = self._scope_gaps(token)
        profile = await self._get(token, "/profile")
        data = profile.json() if profile.ok else None
        email = data.get("emailAddress") if data else None
        if not isinstance(email, str):
            return ProviderHealth(
                provider=EmailProviderKind.GMAIL_API,
                status=ProviderHealthStatus.UNAVAILABLE,
                checked_at=now,
                problems=("PROVIDER_UNAVAILABLE",),
            )
        problems = []
        if not same_account_address(email, self._account):
            problems.append("ACCOUNT_MISMATCH")
        if missing:
            problems.append("MISSING_SCOPES")
        return ProviderHealth(
            provider=EmailProviderKind.GMAIL_API,
            status=ProviderHealthStatus.DEGRADED if problems else ProviderHealthStatus.OK,
            checked_at=now,
            problems=tuple(problems),
        )


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _prefilter(headers: MessageHeaders, thread_id: str | None, own: Sequence[InquiryBinding]) -> str:
    """Metadata-only decision: ``candidate`` (fetch the body), ``pending`` or ``unrelated``.

    A body is fetched only when the message references one of this system's outbound/send-intent
    Message-IDs, sits in a bound Gmail thread, comes from (or reports a failure for) a verified
    seller alias. A reference to one of *our* Message-ID shapes without a known binding is a
    reply-before-binding-sync race: only the provider locator is kept for a later re-read.
    """
    usable = [b for b in own if b.state != InquiryBindingState.TOMBSTONED]
    message_ids: set[str] = {m for b in usable for m in b.all_message_ids()}
    revoked_ids: set[str] = {
        m for b in own if b.state == InquiryBindingState.TOMBSTONED for m in b.all_message_ids()
    }
    refs = set(parse_message_id_list([*headers.get_all("in-reply-to"), *headers.get_all("references")]))
    if refs & message_ids:
        return "candidate"
    if refs & revoked_ids:
        return "unrelated"  # a revoked binding never grants access to mailbox content
    if thread_id is not None and any(
        b.provider == EmailProviderKind.GMAIL_API and thread_id in b.provider_thread_ids for b in usable
    ):
        return "candidate"
    aliases = {a for b in usable for a in b.verified_seller_aliases}
    senders = {a for value in headers.get_all("from") for a in parse_address_list(value) if a}
    failed = {a for value in headers.get_all("x-failed-recipients") for a in parse_address_list(value) if a}
    if (senders | failed) & aliases:
        return "candidate"
    if any(parse_inquiry_message_id(ref) is not None for ref in refs):
        return "pending"
    return "unrelated"


__all__ = [
    "CAPABILITIES",
    "GMAIL_API_ROOT",
    "REPLY_HEADER_NAMES",
    "REQUIRED_GMAIL_SCOPES",
    "SCOPE_GMAIL_READONLY",
    "SCOPE_GMAIL_SEND",
    "GmailApiProvider",
    "GmailApiSettings",
]
