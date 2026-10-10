"""Microsoft Graph sender adapter for Outlook.com / Microsoft 365 (skeleton; spec 37.8).

REST contract (official Microsoft Learn pages, checked 2026-10-07; no live call was made):

* ``POST /me/sendMail`` (or ``/users/{id | userPrincipalName}/sendMail``). MIME format:
  ``Content-Type: text/plain`` with the MIME message as a base64 string in the body. Success is
  ``202 Accepted`` with an empty body: "the request has been accepted; however, it doesn't
  indicate that the request processing has completed". No message id, conversation id or
  Internet Message-ID is returned, so none is recorded. Malformed MIME returns 400
  ``ErrorMimeContentInvalidBase64String``. Least-privileged permission: ``Mail.Send``
  (delegated work/school, delegated personal Microsoft account, application).
* Send pipeline: Exchange first creates the message in Drafts (only this step precedes the
  202), transport later moves it to Sent Items; failures after the 202 come back as NDRs. A
  missing Sent Items entry is therefore never proof of non-submission.
* ``GET /me/messages`` with ``$filter``/``$select`` (least privileged ``Mail.ReadBasic``;
  ``Mail.Read`` for bodies) and ``Prefer: outlook.body-content-type="text"``; messages expose
  ``internetMessageId``, ``conversationId``, ``isDraft``, ``sentDateTime``,
  ``receivedDateTime`` and ``internetMessageHeaders`` (only with ``$select``). Paging uses
  ``@odata.nextLink`` (followed only on ``https://graph.microsoft.com``).
* Errors: JSON ``{"error": {"code", "message", "innerError"}}``; 401, 403 (insufficient
  permission/licence/conditional access), 404, 413, 429/503 with ``Retry-After``, 5xx.

Account binding: the configured account id is the Graph user ``id`` (GUID) of the mailbox.
Every send first reads ``GET /me`` with the *same* access token and refuses unless that id
matches, so a token for another account can never send. Graph offers no send-as alias listing
for personal accounts, so only the account's own primary address (``mail`` or
``userPrincipalName``) verifies as From/Reply-To; anything else is ``not_verifiable``.

Capabilities that CANNOT be verified offline (listed in ``CAPABILITIES.unverified_offline``):
whether Exchange keeps a client-supplied Message-ID from MIME (Microsoft says transport copies
MIME "more or less intact"), whether ``$filter=internetMessageId eq '...'`` is supported for the
account type, personal-account behaviour of ``/users/{id}`` paths, and reply-header retrieval
on list queries. Activation must verify them with an owner-controlled test message.

Permissions requested (minimum): ``Mail.Send``, ``User.Read`` (``/me`` identity), and
``Mail.ReadBasic`` for reconciliation; ``Mail.Read`` only when provider-API reply ingestion
is enabled (bodies). ``offline_access`` is an OAuth refresh-token scope, not a Graph
permission. ``Mail.ReadWrite``, ``Mail.Send.Shared`` and application ``*.All`` permissions are
broader than needed and reported as warnings.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from suv_deals.clock import Clock, SystemClock, ensure_utc
from suv_deals.domain.enums import EmailProviderKind, Tristate
from suv_deals.domain.inquiries import SenderBinding
from suv_deals.domain.replies import (
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
from suv_deals.integrations.mime_builder import BuiltMessage

GRAPH_ROOT: Final = "https://graph.microsoft.com/v1.0"
GRAPH_HOST: Final = "graph.microsoft.com"
PERMISSION_MAIL_SEND: Final = "Mail.Send"
PERMISSION_MAIL_READBASIC: Final = "Mail.ReadBasic"
PERMISSION_MAIL_READ: Final = "Mail.Read"
PERMISSION_USER_READ: Final = "User.Read"
_BROADER_THAN_NEEDED: Final = frozenset(
    {
        "mail.readwrite",
        "mail.send.shared",
        "mail.readwrite.shared",
        "mail.read.shared",
        "mail.send.all",
        "mail.read.all",
        "mail.readwrite.all",
        "mailboxsettings.readwrite",
    }
)
_READ_PERMISSIONS: Final = frozenset({"mail.read", "mail.readwrite"})
_READBASIC_PERMISSIONS: Final = frozenset({"mail.readbasic", "mail.read", "mail.readwrite"})
_SCOPE_PREFIX: Final = "https://graph.microsoft.com/"
_GUID_RE: Final = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_PERSONAL_ID_RE: Final = re.compile(r"^[0-9a-fA-F]{16}$")  # personal-account /me ids (UNVERIFIED)
_SCOPE_CODES: Final = frozenset({"erroraccessdenied", "authorization_requestdenied", "accessdenied"})
#: Reply headers read for pre-filtering/classification; nothing else is requested.
REPLY_HEADER_NAMES: Final = frozenset(
    {
        "from",
        "to",
        "subject",
        "date",
        "message-id",
        "in-reply-to",
        "references",
        "auto-submitted",
        "x-autoreply",
        "x-autorespond",
        "x-autoresponder",
        "x-auto-reply",
        "precedence",
        "x-spam-flag",
        "x-spam-status",
        "x-ms-exchange-organization-scl",
        "x-forefront-antispam-report",
        "content-type",
        "x-failed-recipients",
    }
)
MAX_FETCH_MESSAGES: Final = 500
REPLY_OVERLAP: Final = timedelta(minutes=5)
_FULL_SELECT: Final = "id,internetMessageId,conversationId,receivedDateTime,internetMessageHeaders,body,from"

CAPABILITIES: Final = ProviderCapabilities(
    kind=EmailProviderKind.MICROSOFT_GRAPH,
    delivery_mode="direct_api",
    returns_provider_message_id=False,
    returns_thread_id=False,
    preserves_client_message_id=Tristate.UNKNOWN,
    documented_send_idempotency=False,
    reconcile_by_rfc_message_id=Tristate.UNKNOWN,
    alias_verification="primary_only",
    reply_retrieval="provider_pull",
    required_scopes=(PERMISSION_MAIL_SEND, PERMISSION_USER_READ, PERMISSION_MAIL_READBASIC),
    unverified_offline=(
        "preserves_client_message_id",
        "reconcile_filter_internet_message_id",
        "personal_account_paths",
        "reply_headers_on_list",
        "sent_items_sync_lag",
    ),
)


class GraphSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    api_root: str = GRAPH_ROOT
    request_timeout_s: float = Field(default=20.0, gt=0, le=120)
    send_timeout_s: float = Field(default=30.0, gt=0, le=120)
    max_response_bytes: int = Field(default=1024 * 1024, ge=4096, le=16 * 1024 * 1024)
    max_list_pages: int = Field(default=5, ge=1, le=50)
    page_size: int = Field(default=50, ge=1, le=100)
    max_full_message_bytes: int = Field(default=8 * 1024 * 1024, ge=64 * 1024, le=32 * 1024 * 1024)
    reply_retrieval_enabled: bool = False  # True only with SELLER_REPLY_INGEST_MODE=provider_api

    @field_validator("api_root")
    @classmethod
    def _root(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.hostname != GRAPH_HOST or parts.port is not None:
            raise ValueError("the Graph root must be https://graph.microsoft.com/...")
        if parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("the Graph root must be a plain URL")
        return value.rstrip("/")


def normalize_permissions(scopes: frozenset[str]) -> frozenset[str]:
    """``https://graph.microsoft.com/Mail.Send`` and ``Mail.Send`` compare equal (lower-case)."""
    normalized: set[str] = set()
    for scope in scopes:
        value = scope[len(_SCOPE_PREFIX) :] if scope.lower().startswith(_SCOPE_PREFIX) else scope
        normalized.add(value.lower())
    return frozenset(normalized)


def _error_code(data: Mapping[str, Any] | None) -> str | None:
    if not data or not isinstance(data.get("error"), Mapping):
        return None
    code = data["error"].get("code")
    return safe_token(code) if isinstance(code, str) else None


def _same_graph_host(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme == "https" and parts.hostname == GRAPH_HOST and parts.port is None and not parts.username
    )


def _parse_graph_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _graph_address(value: Any) -> str | None:
    if isinstance(value, Mapping) and isinstance(value.get("emailAddress"), Mapping):
        address = value["emailAddress"].get("address")
        return address if isinstance(address, str) else None
    return None


class GraphMailProvider:
    """``SenderProvider`` for one bound Microsoft mailbox via Microsoft Graph (skeleton)."""

    def __init__(
        self,
        *,
        binding: SenderBinding,
        token_provider: TokenProvider,
        http: httpx.AsyncClient,
        clock: Clock | None = None,
        settings: GraphSettings | None = None,
    ) -> None:
        if binding.provider != EmailProviderKind.MICROSOFT_GRAPH:
            raise ValidationFailed("binding is not a Microsoft Graph binding")
        if not (_GUID_RE.fullmatch(binding.account_id) or _PERSONAL_ID_RE.fullmatch(binding.account_id)):
            raise ValidationFailed("the Graph account id must be the mailbox user's Graph id")
        self._binding = binding
        self._tokens = token_provider
        self._http = http
        self._clock = clock or SystemClock()
        self._settings = settings or GraphSettings()
        self._me = f"{self._settings.api_root}/me"
        #: The mailbox owner's own addresses: a message from them is never a seller reply.
        self._own_addresses = tuple(
            dict.fromkeys(a for a in (binding.from_address, binding.reply_to_address) if a)
        )

    @property
    def kind(self) -> EmailProviderKind:
        return EmailProviderKind.MICROSOFT_GRAPH

    @property
    def binding(self) -> SenderBinding:
        return self._binding

    @property
    def capabilities(self) -> ProviderCapabilities:
        return CAPABILITIES

    def __repr__(self) -> str:
        return f"GraphMailProvider(binding_id={self._binding.binding_id})"

    # -- helpers --------------------------------------------------------------------------------

    def _scope_gaps(self, token: AccessToken) -> tuple[list[str], list[str]]:
        granted = normalize_permissions(token.scopes)
        missing: list[str] = []
        if "mail.send" not in granted:
            missing.append(PERMISSION_MAIL_SEND)
        if "user.read" not in granted:
            missing.append(PERMISSION_USER_READ)
        if not granted & _READBASIC_PERMISSIONS:
            missing.append(PERMISSION_MAIL_READBASIC)
        if self._settings.reply_retrieval_enabled and not granted & _READ_PERMISSIONS:
            missing.append(PERMISSION_MAIL_READ)
        return missing, sorted(granted & _BROADER_THAN_NEEDED)

    def _headers(self, token: AccessToken, **extra: str) -> dict[str, str]:
        return {"Authorization": token.bearer(), "Accept": "application/json", **extra}

    async def _get_url(
        self, token: AccessToken, url: str, params: Mapping[str, str | int] | None = None, **headers: str
    ) -> HttpResult:
        return await http_call(
            self._http,
            "GET",
            url,
            headers=self._headers(token, **headers),
            params=params,
            timeout_s=self._settings.request_timeout_s,
            max_response_bytes=self._settings.max_response_bytes,
        )

    async def _whoami(self, token: AccessToken) -> HttpResult:
        return await self._get_url(token, self._me, {"$select": "id,mail,userPrincipalName,displayName"})

    def _alias_status(self, address: str, me: Mapping[str, Any]) -> AliasStatus:
        own = [v for v in (me.get("mail"), me.get("userPrincipalName")) if isinstance(v, str)]
        if any(same_account_address(address, value) for value in own):
            return AliasStatus.PRIMARY
        return AliasStatus.NOT_VERIFIABLE

    # -- verification ---------------------------------------------------------------------------

    async def verify_account(self) -> SenderVerification:
        base: dict[str, Any] = {
            "provider": EmailProviderKind.MICROSOFT_GRAPH,
            "checked_at": self._clock.now(),
            "configured_account_id": self._binding.account_id,
            "configured_from": self._binding.from_address,
            "configured_reply_to": self._binding.reply_to_address,
            "configured_display_name": self._binding.display_name,
            "reply_to_status": AliasStatus.NOT_CHECKED if self._binding.reply_to_address else None,
            "capabilities": CAPABILITIES,
        }
        try:
            token = await self._tokens.access_token()
        except TokenUnavailable as exc:
            return SenderVerification(
                **base,
                health=ProviderHealthStatus.CREDENTIALS_REVOKED
                if exc.revoked
                else ProviderHealthStatus.UNAVAILABLE,
                problems=("CREDENTIALS_REVOKED" if exc.revoked else "CREDENTIALS_UNAVAILABLE",),
            )
        missing, excess = self._scope_gaps(token)
        problems: list[str] = ["MISSING_SCOPES"] if missing else []
        warnings: list[str] = ["SCOPES_BROADER_THAN_NEEDED"] if excess else []
        base.update(
            granted_scopes=tuple(sorted(token.scopes)),
            missing_scopes=tuple(missing),
            excess_scopes=tuple(excess),
        )
        me_result = await self._whoami(token)
        if me_result.status_code is None or me_result.status_code >= 500 or me_result.status_code == 429:
            return SenderVerification(
                **base, health=ProviderHealthStatus.UNAVAILABLE, problems=(*problems, "PROVIDER_UNAVAILABLE")
            )
        if me_result.status_code == 401:
            return SenderVerification(
                **base, health=ProviderHealthStatus.UNAVAILABLE, problems=(*problems, "CREDENTIALS_REJECTED")
            )
        me = me_result.json() if me_result.ok else None
        user_id = me.get("id") if me else None
        if not me or not isinstance(user_id, str):
            return SenderVerification(
                **base, health=ProviderHealthStatus.OK, problems=(*problems, "ACCOUNT_ACCESS_DENIED")
            )
        if user_id.lower() != self._binding.account_id.lower():
            problems.append("ACCOUNT_MISMATCH")
        account_email = next(
            (
                canonical_or_none(v)
                for v in (me.get("mail"), me.get("userPrincipalName"))
                if isinstance(v, str)
            ),
            None,
        )
        from_status = self._alias_status(self._binding.from_address, me)
        reply_status = (
            self._alias_status(self._binding.reply_to_address, me) if self._binding.reply_to_address else None
        )
        if from_status not in USABLE_ALIAS_STATUSES:
            problems.append("FROM_ALIAS_NOT_VERIFIABLE")
        if reply_status is not None and reply_status not in USABLE_ALIAS_STATUSES:
            problems.append("REPLY_TO_NOT_VERIFIED")
        display = me.get("displayName") if isinstance(me.get("displayName"), str) else None
        if display is not None and display != self._binding.display_name:
            warnings.append("PROVIDER_DISPLAY_NAME_DIFFERS")
        base["reply_to_status"] = reply_status
        return SenderVerification(
            **base,
            stable_account_id=user_id,
            account_email=account_email,
            provider_display_name=display[:256] if display else None,
            from_status=from_status,
            health=ProviderHealthStatus.OK,
            problems=tuple(dict.fromkeys(problems)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

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
                "provider": EmailProviderKind.MICROSOFT_GRAPH,
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

    async def send(
        self, message: BuiltMessage, *, inquiry_id: UUID, attempt_id: UUID, idempotency_key: str
    ) -> SendAccepted | SendDefiniteFailure | SendUncertain:
        """Submit once via ``sendMail`` (MIME). Graph documents no idempotency for sendMail."""
        problems = send_precondition_problems(
            message,
            self._binding,
            provider=EmailProviderKind.MICROSOFT_GRAPH,
            inquiry_id=inquiry_id,
            idempotency_key=idempotency_key,
        )
        if problems:
            return local_refusal(
                message,
                provider=EmailProviderKind.MICROSOFT_GRAPH,
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
                # Nothing was sent and nothing about the message is wrong: retryable, so the
                # domain retry/preflight path suppresses the inquiry while access is revoked
                # (spec 37.5) instead of failing it permanently.
                retryable=True,
                proof="credentials_rejected_before_submit",
                provider_error=exc.code,
            )
        except Exception:  # the credential store failed before any provider request was made
            return self._failure(
                message,
                inquiry_id,
                attempt_id,
                reason=SendFailureReason.CREDENTIALS_UNAVAILABLE,
                retryable=True,
                proof="credentials_rejected_before_submit",
                provider_error="token_provider_error",
            )
        if token.scopes and "mail.send" not in normalize_permissions(token.scopes):
            return self._failure(
                message,
                inquiry_id,
                attempt_id,
                reason=SendFailureReason.INSUFFICIENT_SCOPE,
                retryable=False,
                proof="local_validation_failed_before_submit",
            )
        me_result = await self._whoami(token)
        me = me_result.json() if me_result.ok else None
        user_id = me.get("id") if me else None
        if me_result.status_code == 401:
            return self._failure(
                message,
                inquiry_id,
                attempt_id,
                reason=SendFailureReason.CREDENTIALS_REJECTED,
                retryable=True,
                proof="credentials_rejected_before_submit",
                http_status=401,
            )
        if not isinstance(user_id, str):
            return self._failure(
                message,
                inquiry_id,
                attempt_id,
                reason=SendFailureReason.PRECHECK_FAILED,
                retryable=me_result.status_code not in (400, 403, 404),
                proof="local_validation_failed_before_submit",
                http_status=me_result.status_code,
            )
        if user_id.lower() != self._binding.account_id.lower():
            return self._failure(
                message,
                inquiry_id,
                attempt_id,
                reason=SendFailureReason.ACCOUNT_MISMATCH,
                retryable=False,
                proof="local_validation_failed_before_submit",
            )
        result = await http_call(
            self._http,
            "POST",
            f"{self._me}/sendMail",
            headers=self._headers(
                token,
                **{
                    "Content-Type": "text/plain",
                    "client-request-id": str(attempt_id),  # diagnostic correlation, not idempotency
                    "return-client-request-id": "true",
                },
            ),
            content=message.raw_base64().encode("ascii"),
            timeout_s=self._settings.send_timeout_s,
            max_response_bytes=64 * 1024,
        )
        if result.status_code is None:
            return uncertain_from_transport(
                message,
                provider=EmailProviderKind.MICROSOFT_GRAPH,
                inquiry_id=inquiry_id,
                attempt_id=attempt_id,
                result=result,
            )
        if 200 <= result.status_code < 300:
            request_id = result.headers.get("request-id")
            return SendAccepted(
                provider=EmailProviderKind.MICROSOFT_GRAPH,
                inquiry_id=inquiry_id,
                attempt_id=attempt_id,
                rfc_message_id=message.rfc_message_id,
                raw_sha256=message.raw_sha256,
                receipt=ProviderReceipt(
                    kind="graph_202_accepted",
                    http_status=result.status_code,
                    provider_request_id=safe_token(request_id, limit=128) if request_id else None,
                    observed_at=self._clock.now(),
                ),
                accepted_at=self._clock.now(),
            )
        return self._classify_rejection(message, inquiry_id, attempt_id, result)

    def _classify_rejection(
        self, message: BuiltMessage, inquiry_id: UUID, attempt_id: UUID, result: HttpResult
    ) -> SendDefiniteFailure | SendUncertain:
        status = result.status_code or 0
        code = _error_code(result.json())
        retry_after = parse_retry_after(result.headers)
        documented = "provider_documented_not_sent"

        def definite(
            reason: SendFailureReason, *, retryable: bool = False, proof: str = documented
        ) -> SendDefiniteFailure:
            return self._failure(
                message,
                inquiry_id,
                attempt_id,
                reason=reason,
                retryable=retryable,
                proof=proof,
                http_status=status,
                provider_error=code,
                retry_after=retry_after,
            )

        if status == 400:
            return definite(SendFailureReason.PROVIDER_REJECTED_INVALID)
        if status == 401:
            return definite(
                SendFailureReason.CREDENTIALS_REJECTED,
                retryable=True,
                proof="credentials_rejected_before_submit",
            )
        if status == 403:
            if code is not None and code.lower() in _SCOPE_CODES:
                return definite(SendFailureReason.INSUFFICIENT_SCOPE)
            return definite(SendFailureReason.ACCOUNT_NOT_PERMITTED)
        if status == 404:
            return definite(SendFailureReason.PROVIDER_NOT_FOUND)
        if status == 413:
            return definite(SendFailureReason.MESSAGE_TOO_LARGE)
        reason = (
            UncertainReason.PROVIDER_THROTTLED
            if status == 429
            else UncertainReason.PROVIDER_SERVER_ERROR
            if status >= 500
            else UncertainReason.UNEXPECTED_STATUS
        )
        return SendUncertain(
            provider=EmailProviderKind.MICROSOFT_GRAPH,
            inquiry_id=inquiry_id,
            attempt_id=attempt_id,
            rfc_message_id=message.rfc_message_id,
            raw_sha256=message.raw_sha256,
            reason=reason,
            http_status=status,
            provider_error=code,
            retry_after_seconds=retry_after,
        )

    # -- reconciliation -------------------------------------------------------------------------

    async def reconcile(
        self,
        *,
        inquiry_id: UUID,
        rfc_message_ids: Sequence[str],
        window: ReconcileWindow,
        provider_message_ids: Sequence[str] = (),
    ) -> ReconcileFoundSent | ReconcileNotFoundYet | ReconcileProviderUnavailable:
        """Search the bound mailbox by Internet Message-ID (UNVERIFIED filter support offline)."""
        try:
            ids = normalize_search_ids(rfc_message_ids)
        except ValueError as exc:
            raise ValidationFailed("invalid reconciliation Message-IDs") from exc
        del window, provider_message_ids  # sendMail returns no provider id to search for

        def unavailable(reason: str, result: HttpResult | None = None) -> ReconcileProviderUnavailable:
            return ReconcileProviderUnavailable(
                provider=EmailProviderKind.MICROSOFT_GRAPH,
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
            result = await self._get_url(
                token,
                f"{self._me}/messages",
                {
                    # Message-IDs are validated (no quotes); OData literal quoting doubles any "'".
                    "$filter": f"internetMessageId eq '{rid.replace(chr(39), chr(39) * 2)}'",
                    "$select": "id,internetMessageId,conversationId,sentDateTime,isDraft",
                    "$top": 10,
                },
            )
            data = result.json() if result.ok else None
            items = data.get("value") if data else None
            if not isinstance(items, list):
                return unavailable("search_failed", result)
            for item in items[:10]:
                if (
                    not isinstance(item, Mapping)
                    or normalize_message_id(item.get("internetMessageId")) != rid
                ):
                    continue
                if item.get("isDraft") is True:
                    draft_seen = True  # created but not (yet) picked up by transport
                    continue
                if item.get("isDraft") is False:
                    return ReconcileFoundSent(
                        provider=EmailProviderKind.MICROSOFT_GRAPH,
                        inquiry_id=inquiry_id,
                        evidence=SentEvidence(
                            matched_by="rfc_message_id",
                            location="graph:mailbox",
                            rfc_message_id=rid,
                            provider_message_id=safe_opaque_id(item.get("id")),
                            provider_thread_id=safe_opaque_id(item.get("conversationId")),
                            sent_at=_parse_graph_datetime(item.get("sentDateTime")),
                        ),
                    )
        return ReconcileNotFoundYet(
            provider=EmailProviderKind.MICROSOFT_GRAPH,
            inquiry_id=inquiry_id,
            searched_message_ids=ids,
            pending_in_drafts_or_outbox=Tristate.YES if draft_seen else Tristate.UNKNOWN,
        )

    # -- correlated replies (skeleton) ----------------------------------------------------------

    async def fetch_correlated_replies(
        self,
        *,
        mailbox_binding_id: UUID,
        bindings: Sequence[InquiryBinding],
        since: datetime,
        cursor: str | None = None,
        max_messages: int | None = None,
    ) -> ReplyFetchResult:
        """Inbox time-window pull: header pre-filter, local correlation, bodies only for matches.

        ``cursor`` is the ISO receivedDateTime of the last fully processed message; the pull
        restarts ``REPLY_OVERLAP`` before it (``since`` is used only without a cursor). A
        candidate whose body cannot be read stops the pull so the cursor never skips it. A
        delta-query/change-notification implementation is future work (UNVERIFIED offline).
        """
        if not self._settings.reply_retrieval_enabled:
            return ReplyFetchResult(
                provider=EmailProviderKind.MICROSOFT_GRAPH,
                retrieval_mode="provider_pull",
                next_cursor=cursor,
                complete=False,
                problems=("REPLY_RETRIEVAL_DISABLED",),
            )
        limit = min(max_messages or MAX_FETCH_MESSAGES, MAX_FETCH_MESSAGES)
        try:
            token = await self._tokens.access_token()
        except TokenUnavailable as exc:
            return ReplyFetchResult(
                provider=EmailProviderKind.MICROSOFT_GRAPH,
                retrieval_mode="provider_pull",
                next_cursor=cursor,
                complete=False,
                problems=("CREDENTIALS_REVOKED" if exc.revoked else "CREDENTIALS_UNAVAILABLE",),
            )
        checkpoint = _parse_graph_datetime(cursor) if cursor else None
        # With a checkpoint, re-read an overlap before it (ingest de-duplicates); else use since.
        # Always UTC: the filter literal carries a "Z" suffix, so a +02:00 value printed as wall
        # time would start the window two hours late and silently skip replies.
        start = ensure_utc(checkpoint - REPLY_OVERLAP if checkpoint is not None else since)
        own = [b for b in bindings if b.mailbox_binding_id == mailbox_binding_id]
        url: str | None = f"{self._me}/mailFolders/inbox/messages"
        params: Mapping[str, str | int] | None = {
            "$filter": f"receivedDateTime ge {start.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "receivedDateTime asc",
            "$select": "id,internetMessageId,conversationId,receivedDateTime,internetMessageHeaders",
            "$top": self._settings.page_size,
        }
        replies: list[CorrelatedReply] = []
        problems: list[str] = []
        scanned = skipped = 0
        complete = True
        last_seen: str | None = cursor if checkpoint is not None else None  # drop an unusable cursor
        for _page in range(self._settings.max_list_pages):
            if url is None:
                break
            result = await self._get_url(token, url, params)
            data = result.json() if result.ok else None
            items = data.get("value") if data else None
            if not isinstance(items, list):
                return ReplyFetchResult(
                    provider=EmailProviderKind.MICROSOFT_GRAPH,
                    retrieval_mode="provider_pull",
                    replies=tuple(replies),
                    next_cursor=last_seen,
                    complete=False,
                    scanned=scanned,
                    skipped_unrelated=skipped,
                    problems=(*problems, "PROVIDER_UNAVAILABLE"),
                )
            for item in items:
                if scanned >= limit:
                    complete = False
                    break
                if not isinstance(item, Mapping):
                    continue
                scanned += 1
                reply = await self._inspect(token, item, mailbox_binding_id, own, bindings)
                if reply is None:
                    # A candidate's body could not be read: stop here so the checkpoint never
                    # moves past it (the next pull re-reads from the last processed message).
                    complete = False
                    problems.append("MESSAGE_FETCH_FAILED")
                    break
                if reply == "skip":
                    skipped += 1
                elif isinstance(reply, CorrelatedReply):
                    replies.append(reply)
                received = item.get("receivedDateTime")
                if isinstance(received, str) and _parse_graph_datetime(received) is not None:
                    last_seen = received
            next_link = data.get("@odata.nextLink") if data else None
            if not complete:
                break
            if isinstance(next_link, str) and next_link:
                if not _same_graph_host(next_link):
                    complete = False
                    break
                url, params = next_link, None
            else:
                url = None
        if url is not None:
            complete = False
        return ReplyFetchResult(
            provider=EmailProviderKind.MICROSOFT_GRAPH,
            retrieval_mode="provider_pull",
            replies=tuple(replies),
            next_cursor=last_seen,
            complete=complete,
            scanned=scanned,
            skipped_unrelated=skipped,
            problems=tuple(problems),
        )

    async def _inspect(
        self,
        token: AccessToken,
        item: Mapping[str, Any],
        mailbox_binding_id: UUID,
        own: Sequence[InquiryBinding],
        bindings: Sequence[InquiryBinding],
    ) -> CorrelatedReply | Literal["skip"] | None:
        """A correlated reply, ``"skip"`` (unrelated/unmatched) or ``None`` (fetch failed)."""
        headers = MessageHeaders.from_raw(_graph_headers(item.get("internetMessageHeaders")))
        conversation = safe_opaque_id(item.get("conversationId"))
        if not _is_candidate(headers, conversation, own):
            return "skip"  # unrelated: body never requested
        message_id = safe_opaque_id(item.get("id"))
        if message_id is None:
            return "skip"
        full = await http_call(
            self._http,
            "GET",
            f"{self._me}/messages/{quote(message_id, safe='')}",
            headers=self._headers(token, Prefer='outlook.body-content-type="text"'),
            params={"$select": _FULL_SELECT},
            timeout_s=self._settings.request_timeout_s,
            max_response_bytes=self._settings.max_full_message_bytes,
        )
        if full.status_code == 404:
            return "skip"
        data = full.json() if full.ok else None
        oversized = data is None and full.ok and not full.body_complete
        if data is None and not oversized:
            return None  # transient failure: the caller stops so the cursor never skips it
        if data is None:
            # Oversized: correlate on the listed headers alone; the body is flagged unavailable.
            data = {**item, "body": {}}
        raw_body = data.get("body")
        body: Mapping[str, Any] = raw_body if isinstance(raw_body, Mapping) else {}
        raw_text, content_type = body.get("content"), body.get("contentType")
        text: str = raw_text if isinstance(raw_text, str) else ""
        html_only = isinstance(content_type, str) and content_type.lower() == "html"
        raw_headers = _graph_headers(data.get("internetMessageHeaders"))
        sender = _graph_address(data.get("from"))
        if sender and not any(k.lower() == "from" for k in raw_headers):
            raw_headers["From"] = [sender]
        full_headers = MessageHeaders.from_raw(raw_headers)
        received = _parse_graph_datetime(data.get("receivedDateTime"))
        inbound = InboundMessage(
            identity=SourceMessageIdentity(
                mailbox_binding_id=mailbox_binding_id,
                provider=EmailProviderKind.MICROSOFT_GRAPH,
                internet_message_id=normalize_message_id(data.get("internetMessageId")),
                provider_message_id=message_id,
                provider_thread_id=safe_opaque_id(data.get("conversationId")),
                received_at=received,
            ),
            headers=full_headers,
            body_text="" if html_only else text[: 256 * 1024],
        )
        correlation = correlate_reply(inbound, bindings, own_addresses=self._own_addresses)
        if correlation.upload_scope == "none":
            return "skip"
        return CorrelatedReply(
            message=inbound,
            correlation=correlation,
            body_truncated=oversized or len(text) > 256 * 1024,
            html_only_body=html_only,
        )

    # -- health ---------------------------------------------------------------------------------

    async def health(self) -> ProviderHealth:
        now = self._clock.now()
        try:
            token = await self._tokens.access_token()
        except TokenUnavailable as exc:
            return ProviderHealth(
                provider=EmailProviderKind.MICROSOFT_GRAPH,
                status=ProviderHealthStatus.CREDENTIALS_REVOKED
                if exc.revoked
                else ProviderHealthStatus.UNAVAILABLE,
                checked_at=now,
                problems=("CREDENTIALS_REVOKED" if exc.revoked else "CREDENTIALS_UNAVAILABLE",),
            )
        missing, _excess = self._scope_gaps(token)
        me_result = await self._whoami(token)
        me = me_result.json() if me_result.ok else None
        user_id = me.get("id") if me else None
        if not isinstance(user_id, str):
            return ProviderHealth(
                provider=EmailProviderKind.MICROSOFT_GRAPH,
                status=ProviderHealthStatus.UNAVAILABLE,
                checked_at=now,
                problems=("PROVIDER_UNAVAILABLE",),
            )
        problems = []
        if user_id.lower() != self._binding.account_id.lower():
            problems.append("ACCOUNT_MISMATCH")
        if missing:
            problems.append("MISSING_SCOPES")
        return ProviderHealth(
            provider=EmailProviderKind.MICROSOFT_GRAPH,
            status=ProviderHealthStatus.DEGRADED if problems else ProviderHealthStatus.OK,
            checked_at=now,
            problems=tuple(problems),
        )


def _graph_headers(value: Any) -> dict[str, list[str]]:
    collected: dict[str, list[str]] = {}
    if not isinstance(value, list):
        return collected
    for item in value[:300]:
        if not isinstance(item, Mapping):
            continue
        name, text = item.get("name"), item.get("value")
        if isinstance(name, str) and isinstance(text, str) and name.lower() in REPLY_HEADER_NAMES:
            collected.setdefault(name, []).append(text)
    return collected


def _is_candidate(
    headers: MessageHeaders, conversation_id: str | None, own: Sequence[InquiryBinding]
) -> bool:
    usable = [b for b in own if b.state != InquiryBindingState.TOMBSTONED]
    message_ids = {m for b in usable for m in b.all_message_ids()}
    refs = set(parse_message_id_list([*headers.get_all("in-reply-to"), *headers.get_all("references")]))
    if refs & message_ids:
        return True
    if conversation_id is not None and any(
        b.provider == EmailProviderKind.MICROSOFT_GRAPH and conversation_id in b.provider_thread_ids
        for b in usable
    ):
        return True
    aliases = {a for b in usable for a in b.verified_seller_aliases}
    senders = {a for v in headers.get_all("from") for a in parse_address_list(v) if a}
    failed = {a for v in headers.get_all("x-failed-recipients") for a in parse_address_list(v) if a}
    return bool((senders | failed) & aliases)


__all__ = [
    "CAPABILITIES",
    "GRAPH_ROOT",
    "PERMISSION_MAIL_READ",
    "PERMISSION_MAIL_READBASIC",
    "PERMISSION_MAIL_SEND",
    "PERMISSION_USER_READ",
    "GraphMailProvider",
    "GraphSettings",
    "normalize_permissions",
]
