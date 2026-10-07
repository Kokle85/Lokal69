"""Seller-email provider registry and factory (spec 37.1, 37.3, 37.5; ADR 0002).

``build_sender_provider`` is the only way to obtain a *sending* provider. It refuses unless all
technical prerequisites hold:

* ``SELLER_INQUIRY_MODE=automatic`` (not ``disabled_until_sender_ready`` or ``paused``);
* the inquiry kill switch is off (settings and the live sender status);
* the sender binding is verified: provider, stable account id, From/Reply-To identity, display
  name, verified alias status, healthy and not revoked (``SenderStatus.identity_problems``),
  and it is exactly the binding the settings configure;
* API providers have a server-side secret *reference* (never a raw token) and a token provider;
  the local Outlook route has its worker gateway.

These are technical/safety prerequisites, NOT an approval gate. Vasko's bounded standing
authorization (spec 37.1) means there is no per-message, first-message or first-template
approval anywhere in this module: when ``SELLER_INQUIRY_REQUIRE_MESSAGE_APPROVAL`` is ``false``
(the default) a qualifying send goes straight to the provider. If the owner later sets it to
``true`` the automatic sending path is simply disabled (``OWNER_REQUIRES_MESSAGE_APPROVAL``);
this module never implements an approval queue.

``GatedSenderProvider`` additionally re-reads the live kill switch immediately before every
transmission (failing closed when the probe errors) - a pause stops untransmitted work at once.

``build_account_verifier`` gives the one-time setup/health flow a verify-only facade (no send
method) so the binding can be verified before automatic mode is enabled.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

import httpx

from suv_deals.clock import Clock, SystemClock
from suv_deals.domain.enums import EmailProviderKind
from suv_deals.domain.inquiries import SenderBinding, SenderStatus
from suv_deals.domain.replies import InquiryBinding
from suv_deals.errors import ValidationFailed
from suv_deals.integrations.email_providers.base import (
    ProviderCapabilities,
    ProviderHealth,
    ReconcileFoundSent,
    ReconcileNotFoundYet,
    ReconcileProviderUnavailable,
    ReconcileWindow,
    ReplyFetchResult,
    SendAccepted,
    SendDefiniteFailure,
    SenderProvider,
    SenderVerification,
    SendFailureReason,
    SendUncertain,
    TokenProvider,
    canonical_or_none,
)
from suv_deals.integrations.email_providers.gmail_api import GmailApiProvider, GmailApiSettings
from suv_deals.integrations.email_providers.microsoft_graph import GraphMailProvider, GraphSettings
from suv_deals.integrations.email_providers.outlook_local import (
    DEFAULT_INTENT_TTL,
    OutlookLocalProvider,
    OutlookWorkerGateway,
)
from suv_deals.integrations.mime_builder import BuiltMessage
from suv_deals.settings import Settings

SELLER_EMAIL_REGISTRY_VERSION: Final = "seller-email-registry/1"
API_PROVIDERS: Final = frozenset({EmailProviderKind.GMAIL_API, EmailProviderKind.MICROSOFT_GRAPH})
_SECRET_REFERENCE_RE: Final = re.compile(r"^[a-z][a-z0-9_.-]{1,40}:[A-Za-z0-9_./:-]{1,200}$")
#: Shapes of real credentials that must never be configured where a *reference* is expected.
_SECRET_LOOKALIKES: Final = (
    re.compile(r"^ya29\."),  # Google OAuth access token
    re.compile(r"^1//"),  # Google refresh token
    re.compile(r"^eyJ[A-Za-z0-9_-]+\."),  # JWT
    re.compile(r"^EwB", re.IGNORECASE),  # Microsoft consumer access token
    re.compile(r"^(?:M\.|0\.A)[A-Za-z0-9_-]{20,}"),  # Microsoft refresh tokens
    re.compile(r"^Bearer\s", re.IGNORECASE),
    re.compile(r"[A-Za-z0-9+/_-]{120,}"),  # long opaque blob
)
KillSwitchProbe = Callable[[], Awaitable[bool]]


class SenderSetupError(ValidationFailed):
    """The sending provider cannot be constructed; ``problems`` are stable technical codes."""

    def __init__(self, message: str, problems: Sequence[str]) -> None:
        self.problems: tuple[str, ...] = tuple(dict.fromkeys(problems))
        super().__init__(message, details={"problems": list(self.problems)})


@dataclass(frozen=True)
class ProviderDependencies:
    """Injected collaborators. Tokens come from the server-side secret store, never settings."""

    token_provider: TokenProvider | None = None
    http_client: httpx.AsyncClient | None = None
    outlook_gateway: OutlookWorkerGateway | None = None
    mailbox_binding_id: UUID | None = None
    clock: Clock | None = None
    kill_switch_probe: KillSwitchProbe | None = None
    gmail_settings: GmailApiSettings | None = None
    graph_settings: GraphSettings | None = None
    outlook_intent_ttl: timedelta = DEFAULT_INTENT_TTL


def default_http_client(*, proxy: str | None = None) -> httpx.AsyncClient:
    """Client for the fixed provider hosts: TLS verified, no redirects, no env proxies."""
    return httpx.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        proxy=proxy,
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=2),
        headers={"User-Agent": "suv-deals/0.1 (private seller inquiry)"},
    )


def secret_reference_problems(reference: str | None) -> list[str]:
    """A secret *reference* (e.g. ``vault:email_sender_bindings/<id>``), never a credential."""
    if reference is None or not reference.strip():
        return ["SECRET_REFERENCE_MISSING"]
    if any(pattern.search(reference) for pattern in _SECRET_LOOKALIKES):
        return ["SECRET_REFERENCE_LOOKS_LIKE_CREDENTIAL"]
    if not _SECRET_REFERENCE_RE.fullmatch(reference):
        return ["SECRET_REFERENCE_INVALID"]
    return []


def _provider_kind(settings: Settings) -> EmailProviderKind | None:
    return EmailProviderKind(settings.seller_email_provider) if settings.seller_email_provider else None


def configured_binding_problems(settings: Settings, sender: SenderStatus) -> list[str]:
    """The verified sender status must describe exactly the configured sender identity."""
    problems: list[str] = []
    kind = _provider_kind(settings)
    if kind is None:
        problems.append("PROVIDER_NOT_CONFIGURED")
    if not settings.seller_email_account_id:
        problems.append("ACCOUNT_NOT_CONFIGURED")
    if not settings.seller_email_from or canonical_or_none(settings.seller_email_from) is None:
        problems.append("FROM_NOT_CONFIGURED")
    if settings.seller_email_reply_to and canonical_or_none(settings.seller_email_reply_to) is None:
        problems.append("REPLY_TO_INVALID")
    mismatch = (
        sender.provider != kind
        or sender.account_id != settings.seller_email_account_id
        or canonical_or_none(sender.from_address) != canonical_or_none(settings.seller_email_from)
        or canonical_or_none(sender.reply_to_address) != canonical_or_none(settings.seller_email_reply_to)
    )
    if mismatch:
        problems.append("SENDER_BINDING_MISMATCH")
    return problems


def sending_prerequisite_problems(
    settings: Settings, sender: SenderStatus, deps: ProviderDependencies | None = None
) -> tuple[str, ...]:
    """Every technical reason automatic sending is not possible now (empty = may construct).

    No code here asks for, waits for or records a message approval.
    """
    problems: list[str] = []
    if settings.seller_inquiry_mode != "automatic" or sender.mode != "automatic":
        problems.append("SELLER_INQUIRY_MODE_NOT_AUTOMATIC")
    if settings.seller_inquiry_kill_switch or sender.kill_switch:
        problems.append("KILL_SWITCH_ACTIVE")
    if settings.seller_inquiry_require_message_approval:
        # The owner switched the standing no-approval authorization off: automatic sending is
        # disabled. This module offers no approval queue in its place.
        problems.append("OWNER_REQUIRES_MESSAGE_APPROVAL")
    problems.extend(configured_binding_problems(settings, sender))
    problems.extend(sender.identity_problems())
    kind = _provider_kind(settings)
    if kind in API_PROVIDERS:
        problems.extend(secret_reference_problems(settings.seller_email_oauth_secret_reference))
        if deps is not None and deps.token_provider is None:
            problems.append("TOKEN_PROVIDER_MISSING")
        if deps is not None and deps.http_client is None:
            problems.append("HTTP_CLIENT_MISSING")
    if kind == EmailProviderKind.OUTLOOK_LOCAL and deps is not None:
        if deps.outlook_gateway is None:
            problems.append("OUTLOOK_GATEWAY_MISSING")
        if deps.mailbox_binding_id is None:
            problems.append("MAILBOX_BINDING_MISSING")
    return tuple(dict.fromkeys(problems))


def sender_binding_from_status(sender: SenderStatus) -> SenderBinding:
    """The exact binding of a verified sender status (refused while the identity is incomplete)."""
    problems = list(sender.identity_problems())
    if (
        sender.binding_id is None
        or sender.binding_version is None
        or sender.provider is None
        or not sender.account_id
        or not sender.from_address
        or not sender.display_name
    ):
        problems.append("SENDER_NOT_VERIFIED")
    from_address = canonical_or_none(sender.from_address)
    reply_to = canonical_or_none(sender.reply_to_address)
    if from_address is None:
        problems.append("SENDER_FROM_INVALID")
    if sender.reply_to_address is not None and reply_to is None:
        problems.append("REPLY_TO_INVALID")
    if (
        problems
        or sender.binding_id is None
        or sender.binding_version is None
        or sender.provider is None
        or sender.account_id is None
        or sender.display_name is None
        or from_address is None
    ):
        raise SenderSetupError("the sender binding is not verified", problems)
    return SenderBinding(
        binding_id=sender.binding_id,
        binding_version=sender.binding_version,
        provider=sender.provider,
        account_id=sender.account_id,
        from_address=from_address,
        display_name=sender.display_name,
        reply_to_address=reply_to,
    )


def _construct(binding: SenderBinding, deps: ProviderDependencies) -> SenderProvider:
    clock = deps.clock or SystemClock()
    if binding.provider == EmailProviderKind.GMAIL_API:
        if deps.token_provider is None or deps.http_client is None:
            raise SenderSetupError("Gmail API dependencies missing", ["TOKEN_PROVIDER_MISSING"])
        return GmailApiProvider(
            binding=binding,
            token_provider=deps.token_provider,
            http=deps.http_client,
            clock=clock,
            settings=deps.gmail_settings,
        )
    if binding.provider == EmailProviderKind.MICROSOFT_GRAPH:
        if deps.token_provider is None or deps.http_client is None:
            raise SenderSetupError("Microsoft Graph dependencies missing", ["TOKEN_PROVIDER_MISSING"])
        return GraphMailProvider(
            binding=binding,
            token_provider=deps.token_provider,
            http=deps.http_client,
            clock=clock,
            settings=deps.graph_settings,
        )
    if deps.outlook_gateway is None or deps.mailbox_binding_id is None:
        raise SenderSetupError("Outlook worker dependencies missing", ["OUTLOOK_GATEWAY_MISSING"])
    return OutlookLocalProvider(
        binding=binding,
        mailbox_binding_id=deps.mailbox_binding_id,
        gateway=deps.outlook_gateway,
        clock=clock,
        intent_ttl=deps.outlook_intent_ttl,
    )


def _construct_checked(binding: SenderBinding, deps: ProviderDependencies) -> SenderProvider:
    try:
        return _construct(binding, deps)
    except SenderSetupError:
        raise
    except ValidationFailed as exc:  # e.g. a Gmail account id that is not an e-mail address
        raise SenderSetupError(
            "the sender binding cannot be used by its provider", ["ACCOUNT_ID_INVALID"]
        ) from exc


class GatedSenderProvider:
    """A constructed, verified provider plus a live kill-switch check before every send.

    There is deliberately no approval hook: ``send`` never waits for a human.
    """

    def __init__(self, inner: SenderProvider, *, kill_switch_probe: KillSwitchProbe | None) -> None:
        self._inner = inner
        self._probe = kill_switch_probe

    @property
    def kind(self) -> EmailProviderKind:
        return self._inner.kind

    @property
    def binding(self) -> SenderBinding:
        return self._inner.binding

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._inner.capabilities

    def __repr__(self) -> str:
        return f"GatedSenderProvider({self._inner!r})"

    async def _kill_switch_active(self) -> bool:
        if self._probe is None:
            return False
        try:
            return bool(await self._probe())
        except Exception:
            return True  # fail closed: an unreadable kill switch stops transmission

    async def verify_account(self) -> SenderVerification:
        return await self._inner.verify_account()

    async def send(
        self, message: BuiltMessage, *, inquiry_id: UUID, attempt_id: UUID, idempotency_key: str
    ) -> SendAccepted | SendDefiniteFailure | SendUncertain:
        if await self._kill_switch_active():
            return SendDefiniteFailure(
                provider=self._inner.kind,
                inquiry_id=inquiry_id,
                attempt_id=attempt_id,
                rfc_message_id=message.rfc_message_id,
                raw_sha256=message.raw_sha256,
                pre_submission=True,
                reason=SendFailureReason.KILL_SWITCH_ACTIVE,
                retryable=False,
                proof="local_validation_failed_before_submit",
            )
        return await self._inner.send(
            message, inquiry_id=inquiry_id, attempt_id=attempt_id, idempotency_key=idempotency_key
        )

    async def reconcile(
        self,
        *,
        inquiry_id: UUID,
        rfc_message_ids: Sequence[str],
        window: ReconcileWindow,
        provider_message_ids: Sequence[str] = (),
    ) -> ReconcileFoundSent | ReconcileNotFoundYet | ReconcileProviderUnavailable:
        # Reconciliation only reads; it stays available while the kill switch is on.
        return await self._inner.reconcile(
            inquiry_id=inquiry_id,
            rfc_message_ids=rfc_message_ids,
            window=window,
            provider_message_ids=provider_message_ids,
        )

    async def fetch_correlated_replies(
        self,
        *,
        mailbox_binding_id: UUID,
        bindings: Sequence[InquiryBinding],
        since: datetime,
        cursor: str | None = None,
        max_messages: int | None = None,
    ) -> ReplyFetchResult:
        return await self._inner.fetch_correlated_replies(
            mailbox_binding_id=mailbox_binding_id,
            bindings=bindings,
            since=since,
            cursor=cursor,
            max_messages=max_messages,
        )

    async def health(self) -> ProviderHealth:
        return await self._inner.health()


def build_sender_provider(
    settings: Settings, *, sender: SenderStatus, deps: ProviderDependencies
) -> GatedSenderProvider:
    """The sending provider, or ``SenderSetupError`` listing every unmet technical prerequisite."""
    problems = sending_prerequisite_problems(settings, sender, deps)
    if problems:
        raise SenderSetupError("automatic seller email sending is not available", problems)
    binding = sender_binding_from_status(sender)
    return GatedSenderProvider(_construct_checked(binding, deps), kill_switch_probe=deps.kill_switch_probe)


class AccountVerifier:
    """Verify-only facade for one-time setup and health checks: it has no ``send`` method."""

    def __init__(self, provider: SenderProvider) -> None:
        self.__provider = provider

    @property
    def kind(self) -> EmailProviderKind:
        return self.__provider.kind

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self.__provider.capabilities

    async def verify_account(self) -> SenderVerification:
        return await self.__provider.verify_account()

    async def health(self) -> ProviderHealth:
        return await self.__provider.health()

    def __repr__(self) -> str:
        return f"AccountVerifier(kind={self.kind.value})"


def candidate_binding_from_settings(
    settings: Settings, *, binding_id: UUID, binding_version: int, display_name: str
) -> SenderBinding:
    """The (not yet verified) binding the settings describe, for the one-time verification."""
    kind = _provider_kind(settings)
    from_address = canonical_or_none(settings.seller_email_from)
    reply_to = canonical_or_none(settings.seller_email_reply_to)
    problems: list[str] = []
    if kind is None:
        problems.append("PROVIDER_NOT_CONFIGURED")
    if not settings.seller_email_account_id:
        problems.append("ACCOUNT_NOT_CONFIGURED")
    if from_address is None:
        problems.append("FROM_NOT_CONFIGURED")
    if settings.seller_email_reply_to and reply_to is None:
        problems.append("REPLY_TO_INVALID")
    if not display_name.strip():
        problems.append("SENDER_DISPLAY_NAME_MISSING")
    if kind in API_PROVIDERS:
        problems.extend(secret_reference_problems(settings.seller_email_oauth_secret_reference))
    if problems or kind is None or from_address is None or not settings.seller_email_account_id:
        raise SenderSetupError("the sender is not configured", problems)
    return SenderBinding(
        binding_id=binding_id,
        binding_version=binding_version,
        provider=kind,
        account_id=settings.seller_email_account_id,
        from_address=from_address,
        display_name=display_name,
        reply_to_address=reply_to,
    )


def build_account_verifier(
    settings: Settings, *, binding: SenderBinding, deps: ProviderDependencies
) -> AccountVerifier:
    """Verification works in any inquiry mode (it never sends); the binding must match settings."""
    kind = _provider_kind(settings)
    problems: list[str] = []
    if kind is None or binding.provider != kind:
        problems.append("PROVIDER_MISMATCH")
    if binding.account_id != settings.seller_email_account_id:
        problems.append("ACCOUNT_MISMATCH")
    if binding.from_address != canonical_or_none(settings.seller_email_from):
        problems.append("FROM_MISMATCH")
    if binding.reply_to_address != canonical_or_none(settings.seller_email_reply_to):
        problems.append("REPLY_TO_MISMATCH")
    if kind in API_PROVIDERS:
        problems.extend(secret_reference_problems(settings.seller_email_oauth_secret_reference))
    if problems:
        raise SenderSetupError("verification binding does not match the configuration", problems)
    return AccountVerifier(_construct_checked(binding, deps))


def sender_status_from_verification(
    settings: Settings,
    verification: SenderVerification,
    *,
    binding: SenderBinding,
) -> SenderStatus:
    """``SenderStatus`` for the dispatcher/readiness checks from a verification result."""
    return SenderStatus.from_settings(
        settings,
        binding_id=binding.binding_id,
        binding_version=binding.binding_version,
        display_name=binding.display_name,
        alias_verified=verification.alias_verified,
        verified_at=verification.checked_at if verification.verified else None,
        health_ok=verification.verified,
        credentials_revoked=verification.credentials_revoked,
    )


__all__ = [
    "API_PROVIDERS",
    "SELLER_EMAIL_REGISTRY_VERSION",
    "AccountVerifier",
    "GatedSenderProvider",
    "KillSwitchProbe",
    "ProviderDependencies",
    "SenderSetupError",
    "build_account_verifier",
    "build_sender_provider",
    "candidate_binding_from_settings",
    "configured_binding_problems",
    "default_http_client",
    "secret_reference_problems",
    "sender_binding_from_status",
    "sender_status_from_verification",
    "sending_prerequisite_problems",
]
