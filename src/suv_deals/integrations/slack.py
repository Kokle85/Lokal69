"""Optional, disabled-by-default Slack fallback provider (spec section 22).

Posting a Slack message does NOT prove that dot reads the channel or that a
bot-authored message triggers any automation. This adapter therefore only
covers the verifiable parts:

- outbound `chat.postMessage` (JSON body, bot token in the Authorization
  header) carrying message metadata `{event_type, event_payload: {event_ref,
  dedup_key}}` and a stable short event reference in the text so duplicates can
  be detected (spec 13);
- timeouts after sending, Slack 5xx and `internal_error`/`fatal_error` are
  UNCERTAIN (Slack: "it's possible some aspect of the operation succeeded");
  they are reconciled with `conversations.history?include_all_metadata=true`
  instead of being blindly resent;
- inbound request verification: v0 HMAC-SHA256 over `v0:<ts>:<raw body>` with
  the signing secret as a UTF-8 key, 5-minute freshness window, constant-time
  compare, all before parsing; then team/app/channel/event-type binding,
  provider event-id dedup and own-message loop prevention.

Verified facts: docs.slack.dev chat.postMessage, conversations.history,
message metadata, Events API and request-verification pages (2026-10-06).
Sending refuses unless ALLOW_EXTERNAL_NOTIFICATIONS=true,
NOTIFICATION_PROVIDER=slack and an owner destination approval reference exist.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol
from urllib.parse import urlencode
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt, ValidationError, field_validator

from suv_deals.clock import Clock, ensure_utc
from suv_deals.errors import AppError, ErrorCode, Forbidden, ValidationFailed
from suv_deals.integrations.event_bridge import (
    ActivationRoute,
    dashboard_url_problem,
    parse_retry_after,
    select_activation_route,
)
from suv_deals.integrations.safe_http import SafeHttp, SafeHttpError, SafeHttpFailure, SafeResponse
from suv_deals.integrations.webhook_signing import canonical_json
from suv_deals.settings import Settings

SLACK_API_BASE: Final = "https://slack.com/api"
POST_MESSAGE_URL: Final = f"{SLACK_API_BASE}/chat.postMessage"
HISTORY_URL: Final = f"{SLACK_API_BASE}/conversations.history"
METADATA_EVENT_TYPE: Final = "suv_deals.review_pending"  # app-namespaced custom metadata type
SIGNATURE_VERSION: Final = "v0"
REPLAY_WINDOW: Final = timedelta(minutes=5)
MAX_INBOUND_BODY_BYTES: Final = 512 * 1024
MAX_TEXT_CHARS: Final = 3000
EVENT_REF_PREFIX: Final = "SDR-"

_CHANNEL_RE = re.compile(r"^[CG][A-Z0-9]{8,20}$")
_TEAM_RE = re.compile(r"^T[A-Z0-9]{8,20}$")
_APP_RE = re.compile(r"^A[A-Z0-9]{8,20}$")
_BOT_RE = re.compile(r"^B[A-Z0-9]{8,20}$")
_USER_RE = re.compile(r"^[UW][A-Z0-9]{8,20}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
_TS_HEADER_RE = re.compile(r"^[0-9]{1,12}$")
_SIGNATURE_RE = re.compile(r"^v0=[0-9a-f]{64}$")
_EVENT_REF_RE = re.compile(r"\bSDR-[0-9A-F]{12}\b")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Slack error strings (chat.postMessage reference) that are safe to retry later.
_RETRYABLE_ERRORS: Final = frozenset(
    {"ratelimited", "rate_limited", "service_unavailable", "request_timeout"}
)
# "It's possible some aspect of the operation succeeded before the error was raised."
_UNCERTAIN_ERRORS: Final = frozenset({"internal_error", "fatal_error"})
_KNOWN_ERRORS: Final = (
    _RETRYABLE_ERRORS
    | _UNCERTAIN_ERRORS
    | frozenset(
        {
            "channel_not_found",
            "not_in_channel",
            "is_archived",
            "invalid_auth",
            "not_authed",
            "account_inactive",
            "token_revoked",
            "token_expired",
            "missing_scope",
            "no_permission",
            "restricted_action",
            "msg_too_long",
            "no_text",
            "invalid_metadata_format",
            "invalid_metadata_schema",
            "metadata_too_large",
            "metadata_must_be_sent_from_app",
            "message_limit_exceeded",
            "team_access_not_granted",
        }
    )
)


# --------------------------------------------------------------------------- configuration


class SlackNotConfigured(ValidationFailed):
    def __init__(self, missing: list[str]) -> None:
        super().__init__("Slack provider is not configured", details={"missing": missing})


class SlackSendBlocked(Forbidden):
    """External delivery is not authorized by configuration/approval."""

    def __init__(self, blockers: list[str]) -> None:
        super().__init__("Slack delivery is not activated")
        self.details = {"blockers": blockers}


class SlackConfig(BaseModel):
    """One explicitly approved private destination. Secrets stay SecretStr."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bot_token: SecretStr
    signing_secret: SecretStr
    channel_id: str
    team_id: str | None = None
    app_id: str | None = None
    bot_id: str | None = None
    bot_user_id: str | None = None
    destination_approval_ref: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("bot_token")
    @classmethod
    def _bot_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if not token.startswith("xoxb-") or len(token) > 512 or any(c.isspace() for c in token):
            raise ValueError("a Slack bot token (xoxb-) is required")
        return value

    @field_validator("signing_secret")
    @classmethod
    def _signing_secret(cls, value: SecretStr) -> SecretStr:
        if not 8 <= len(value.get_secret_value()) <= 256:
            raise ValueError("signing secret has an unexpected length")
        return value

    @field_validator("channel_id")
    @classmethod
    def _channel(cls, value: str) -> str:
        if not _CHANNEL_RE.fullmatch(value):
            raise ValueError("channel_id must be a Slack channel ID")
        return value

    @field_validator("team_id", "app_id", "bot_id", "bot_user_id")
    @classmethod
    def _ids(cls, value: str | None, info: Any) -> str | None:
        patterns = {"team_id": _TEAM_RE, "app_id": _APP_RE, "bot_id": _BOT_RE, "bot_user_id": _USER_RE}
        if value is not None and not patterns[info.field_name].fullmatch(value):
            raise ValueError(f"{info.field_name} has an unexpected format")
        return value

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        destination_approval_ref: str | None = None,
        team_id: str | None = None,
        app_id: str | None = None,
        bot_id: str | None = None,
        bot_user_id: str | None = None,
    ) -> SlackConfig:
        missing = [
            name
            for name, value in (
                ("SLACK_BOT_TOKEN", settings.slack_bot_token),
                ("SLACK_SIGNING_SECRET", settings.slack_signing_secret),
                ("SLACK_CHANNEL_ID", settings.slack_channel_id),
            )
            if value is None or (isinstance(value, SecretStr) and not value.get_secret_value())
        ]
        if missing:
            raise SlackNotConfigured(missing)
        assert settings.slack_bot_token is not None
        assert settings.slack_signing_secret is not None
        assert settings.slack_channel_id is not None
        try:
            return cls(
                bot_token=settings.slack_bot_token,
                signing_secret=settings.slack_signing_secret,
                channel_id=settings.slack_channel_id,
                team_id=team_id,
                app_id=app_id,
                bot_id=bot_id,
                bot_user_id=bot_user_id,
                destination_approval_ref=destination_approval_ref,
            )
        except ValidationError as exc:
            fields = sorted({str(err["loc"][0]) for err in exc.errors() if err["loc"]})
            raise ValidationFailed("Invalid Slack configuration", details={"fields": fields}) from None


def send_blockers(settings: Settings, config: SlackConfig | None) -> list[str]:
    """Every reason external Slack delivery must not happen (empty list = allowed)."""
    blockers: list[str] = []
    if not settings.allow_external_notifications:
        blockers.append("allow_external_notifications is false")
    if settings.notification_provider != "slack":
        blockers.append("notification_provider is not slack")
    if config is None:
        blockers.append("slack is not configured")
    else:
        if not config.destination_approval_ref:
            blockers.append("no destination binding approval reference")
        if settings.slack_channel_id and settings.slack_channel_id != config.channel_id:
            blockers.append("channel does not match the configured binding")
    try:
        selection = select_activation_route(settings)
    except AppError as exc:
        blockers.append(exc.message)
    else:
        if selection.route is ActivationRoute.MCP_EVENTS:
            blockers.append("native MCP Events is the selected activation route")
    return blockers


def ensure_send_allowed(settings: Settings, config: SlackConfig | None) -> SlackConfig:
    blockers = send_blockers(settings, config)
    if blockers or config is None:
        raise SlackSendBlocked(blockers)
    return config


# --------------------------------------------------------------------------- outbound


def event_reference(event_id: UUID | str) -> str:
    """Stable short reference shown in the message text and metadata (dedup/lookup key)."""
    digest = hashlib.sha256(f"suv_deals/slack-event-ref\x00{event_id}".encode()).hexdigest()
    return EVENT_REF_PREFIX + digest[:12].upper()


def escape_mrkdwn(text: str) -> str:
    """Slack control characters must be escaped in message text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackReviewNotice(BaseModel):
    """Read view of the internal `review.pending` outbox payload for the Slack adapter."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    event_id: UUID
    case_id: UUID
    case_version: StrictInt = Field(ge=1)
    dashboard_url: str = Field(max_length=2048)
    summary: str = Field(default="", max_length=1000)
    deduplication_key: str = Field(min_length=1, max_length=200)

    @field_validator("dashboard_url")
    @classmethod
    def _https_dashboard(cls, value: str) -> str:
        # Same rule as the MCP Events payload (no tokens/credentials in links), https only.
        problem = dashboard_url_problem(value, allow_local_http=False)
        if problem is not None:
            raise ValueError(problem)
        return value


def build_post_body(config: SlackConfig, notice: SlackReviewNotice) -> dict[str, Any]:
    """Message body: minimal owner wording, no seller contact data, no financial secrets."""
    ref = event_reference(notice.event_id)
    summary = _CONTROL_CHARS.sub(" ", notice.summary).strip()[:280]
    parts = [f"Research candidate pending review (case version {notice.case_version})."]
    if summary:
        parts.append(escape_mrkdwn(summary))
    parts.append(f"Dashboard (sign-in required): {escape_mrkdwn(notice.dashboard_url)}")
    parts.append(f"[ref {ref}]")
    text = " ".join(parts)[:MAX_TEXT_CHARS]
    return {
        "channel": config.channel_id,
        "text": text,
        "unfurl_links": False,
        "unfurl_media": False,
        "metadata": {
            "event_type": METADATA_EVENT_TYPE,
            "event_payload": {"event_ref": ref, "dedup_key": notice.deduplication_key},
        },
    }


class SlackOutcomeKind(StrEnum):
    POSTED = "posted"  # provider accepted (receipt), not "dot processed it"
    RETRY = "retry"
    FAILED = "failed"
    UNCERTAIN = "uncertain"  # reconcile via conversations.history before any resend


@dataclass(frozen=True, slots=True)
class SlackPostOutcome:
    kind: SlackOutcomeKind
    event_ref: str
    status_code: int | None = None
    slack_error: str | None = None  # known Slack error string, or "other"
    message_ts: str | None = None
    channel_id: str | None = None
    retry_after: timedelta | None = None
    send_attempted_at: datetime | None = None
    provider_accepted_at: datetime | None = None


def _auth_headers(config: SlackConfig, *, json_body: bool) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {config.bot_token.get_secret_value()}"}
    if json_body:
        headers["Content-Type"] = "application/json; charset=utf-8"
    return headers


def _parse_slack_json(response: SafeResponse) -> dict[str, Any] | None:
    if response.truncated or not response.body:
        return None
    try:
        parsed = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _known_error(value: object) -> str:
    return value if isinstance(value, str) and value in _KNOWN_ERRORS else "other"


async def post_review_message(
    notice: SlackReviewNotice | Mapping[str, Any],
    *,
    config: SlackConfig | None,
    settings: Settings,
    http: SafeHttp,
    clock: Clock,
) -> SlackPostOutcome:
    """Post one pending-review message. Raises SlackSendBlocked unless fully activated."""
    approved = ensure_send_allowed(settings, config)
    parsed = notice if isinstance(notice, SlackReviewNotice) else _parse_notice(notice)
    ref = event_reference(parsed.event_id)
    body = canonical_json(build_post_body(approved, parsed)).encode("utf-8")
    attempted = ensure_utc(clock.now())
    try:
        response = await http.post(
            POST_MESSAGE_URL,
            content=body,
            headers=_auth_headers(approved, json_body=True),
            timeout_s=10.0,
            max_response_bytes=64 * 1024,
        )
    except SafeHttpError as exc:
        if exc.failure in {SafeHttpFailure.DESTINATION_REJECTED, SafeHttpFailure.REDIRECT_REFUSED}:
            kind = SlackOutcomeKind.FAILED
        elif exc.possibly_delivered:
            kind = SlackOutcomeKind.UNCERTAIN
        else:
            kind = SlackOutcomeKind.RETRY
        return SlackPostOutcome(
            kind=kind, event_ref=ref, status_code=exc.status_code, send_attempted_at=attempted
        )
    status = response.status_code
    now = ensure_utc(clock.now())
    if status == 429:
        return SlackPostOutcome(
            kind=SlackOutcomeKind.RETRY,
            event_ref=ref,
            status_code=status,
            slack_error="ratelimited",
            retry_after=parse_retry_after(response.headers.get("retry-after"), now),
            send_attempted_at=attempted,
        )
    if status >= 500:
        return SlackPostOutcome(
            kind=SlackOutcomeKind.UNCERTAIN, event_ref=ref, status_code=status, send_attempted_at=attempted
        )
    if not 200 <= status < 300:
        return SlackPostOutcome(
            kind=SlackOutcomeKind.FAILED, event_ref=ref, status_code=status, send_attempted_at=attempted
        )
    payload = _parse_slack_json(response)
    if payload is None:
        return SlackPostOutcome(
            kind=SlackOutcomeKind.UNCERTAIN, event_ref=ref, status_code=status, send_attempted_at=attempted
        )
    if payload.get("ok") is True:
        ts = payload.get("ts")
        channel = payload.get("channel")
        return SlackPostOutcome(
            kind=SlackOutcomeKind.POSTED,
            event_ref=ref,
            status_code=status,
            message_ts=ts if isinstance(ts, str) else None,
            channel_id=channel if isinstance(channel, str) else None,
            send_attempted_at=attempted,
            provider_accepted_at=now,
        )
    error = _known_error(payload.get("error"))
    if error in _UNCERTAIN_ERRORS:
        kind = SlackOutcomeKind.UNCERTAIN
    elif error in _RETRYABLE_ERRORS:
        kind = SlackOutcomeKind.RETRY
    else:
        kind = SlackOutcomeKind.FAILED
    return SlackPostOutcome(
        kind=kind,
        event_ref=ref,
        status_code=status,
        slack_error=error,
        retry_after=parse_retry_after(response.headers.get("retry-after"), now),
        send_attempted_at=attempted,
    )


def _parse_notice(raw: Mapping[str, Any]) -> SlackReviewNotice:
    try:
        return SlackReviewNotice.model_validate(dict(raw))
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
        raise ValidationFailed(
            "Invalid review.pending payload for Slack", details={"fields": fields}
        ) from None


class ReconcileState(StrEnum):
    FOUND = "found"  # the message exists: mark delivered with this receipt
    NOT_FOUND = "not_found"  # searched the bounded window; safe to schedule one resend
    UNKNOWN = "unknown"  # lookup failed: keep `uncertain` visible, do not resend


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    state: ReconcileState
    message_ts: str | None = None
    pages_read: int = 0


def _message_is_ours(message: Mapping[str, Any], config: SlackConfig) -> bool:
    """Only a message authored by our app/bot can be the receipt of an uncertain post.

    A human (or another bot) quoting the reference must never mark the delivery done.
    """
    bot_id = message.get("bot_id")
    app_id = message.get("app_id")
    if config.bot_id is not None or config.app_id is not None:
        return (config.bot_id is not None and bot_id == config.bot_id) or (
            config.app_id is not None and app_id == config.app_id
        )
    # Without a configured identity, require at least an app/bot author.
    return (isinstance(bot_id, str) and bool(bot_id)) or (isinstance(app_id, str) and bool(app_id))


def _message_has_ref(message: Mapping[str, Any], ref: str, config: SlackConfig) -> bool:
    if not _message_is_ours(message, config):
        return False
    metadata = message.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("event_type") == METADATA_EVENT_TYPE:
        payload = metadata.get("event_payload")
        if isinstance(payload, Mapping) and payload.get("event_ref") == ref:
            return True
    text = message.get("text")
    return isinstance(text, str) and ref in _EVENT_REF_RE.findall(text)


async def reconcile_uncertain_post(
    event_ref: str,
    *,
    posted_after: datetime,
    config: SlackConfig | None,
    settings: Settings,
    http: SafeHttp,
    max_pages: int = 3,
) -> ReconcileResult:
    """Look up recent channel history for our event reference (needs channels/groups:history).

    Only usable once a real token with history scope exists; any lookup failure
    returns UNKNOWN so the uncertainty stays visible (spec 13).
    """
    approved = ensure_send_allowed(settings, config)
    if not re.fullmatch(r"SDR-[0-9A-F]{12}", event_ref):
        raise ValueError("invalid event reference")
    oldest = ensure_utc(posted_after) - timedelta(minutes=1)
    cursor: str | None = None
    pages = 0
    while pages < max_pages:
        query: dict[str, str] = {
            "channel": approved.channel_id,
            "oldest": f"{oldest.timestamp():.6f}",
            "include_all_metadata": "true",
            "limit": "200",
        }
        if cursor:
            query["cursor"] = cursor
        try:
            response = await http.get(
                f"{HISTORY_URL}?{urlencode(query)}",
                headers=_auth_headers(approved, json_body=False),
                timeout_s=10.0,
                max_response_bytes=1024 * 1024,
            )
        except SafeHttpError:
            return ReconcileResult(ReconcileState.UNKNOWN, pages_read=pages)
        pages += 1
        payload = _parse_slack_json(response) if response.is_success else None
        if payload is None or payload.get("ok") is not True:
            return ReconcileResult(ReconcileState.UNKNOWN, pages_read=pages)
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return ReconcileResult(ReconcileState.UNKNOWN, pages_read=pages)
        for message in messages:
            if isinstance(message, Mapping) and _message_has_ref(message, event_ref, approved):
                ts = message.get("ts")
                return ReconcileResult(
                    ReconcileState.FOUND, message_ts=ts if isinstance(ts, str) else None, pages_read=pages
                )
        meta = payload.get("response_metadata")
        next_cursor = meta.get("next_cursor") if isinstance(meta, Mapping) else None
        if not payload.get("has_more") or not isinstance(next_cursor, str) or not next_cursor:
            return ReconcileResult(ReconcileState.NOT_FOUND, pages_read=pages)
        cursor = next_cursor[:512]
    # More history than the bounded window allows: we cannot claim it is absent.
    return ReconcileResult(ReconcileState.UNKNOWN, pages_read=pages)


# --------------------------------------------------------------------------- inbound


class SlackRejectReason(StrEnum):
    MISSING_HEADERS = "missing_headers"
    MALFORMED_TIMESTAMP = "malformed_timestamp"
    STALE_TIMESTAMP = "stale_timestamp"
    MALFORMED_SIGNATURE = "malformed_signature"
    BAD_SIGNATURE = "bad_signature"
    BODY_TOO_LARGE = "body_too_large"
    INVALID_BODY = "invalid_body"
    UNSUPPORTED_ENVELOPE = "unsupported_envelope"
    WRONG_TEAM = "wrong_team"
    WRONG_APP = "wrong_app"
    WRONG_CHANNEL = "wrong_channel"
    EVENT_TYPE_NOT_ALLOWED = "event_type_not_allowed"
    INVALID_EVENT_ID = "invalid_event_id"


_SIGNATURE_REASONS: Final = frozenset(
    {
        SlackRejectReason.MISSING_HEADERS,
        SlackRejectReason.MALFORMED_TIMESTAMP,
        SlackRejectReason.STALE_TIMESTAMP,
        SlackRejectReason.MALFORMED_SIGNATURE,
        SlackRejectReason.BAD_SIGNATURE,
        SlackRejectReason.BODY_TOO_LARGE,
    }
)


class SlackRequestRejected(AppError):
    def __init__(self, reason: SlackRejectReason) -> None:
        code = ErrorCode.UNAUTHENTICATED if reason in _SIGNATURE_REASONS else ErrorCode.FORBIDDEN
        super().__init__(code, "Slack request rejected", retryable=False)
        self.reason = reason


def compute_slack_signature(signing_secret: str, timestamp: str, raw_body: bytes) -> str:
    base = b"v0:" + timestamp.encode("ascii") + b":" + raw_body
    digest = hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def verify_slack_signature(
    signing_secret: SecretStr | str,
    raw_body: bytes,
    headers: Mapping[str, str],
    *,
    now: datetime,
    max_body_bytes: int = MAX_INBOUND_BODY_BYTES,
) -> int:
    """Verify `X-Slack-Signature` over the raw body bytes. Returns the request timestamp."""
    if not isinstance(raw_body, bytes | bytearray):
        raise TypeError("raw body must be the exact bytes received")
    if len(raw_body) > max_body_bytes:
        raise SlackRequestRejected(SlackRejectReason.BODY_TOO_LARGE)
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    timestamp = lowered.get("x-slack-request-timestamp", "")
    signature = lowered.get("x-slack-signature", "")
    if not timestamp or not signature:
        raise SlackRequestRejected(SlackRejectReason.MISSING_HEADERS)
    if not _TS_HEADER_RE.fullmatch(timestamp):
        raise SlackRequestRejected(SlackRejectReason.MALFORMED_TIMESTAMP)
    ts = int(timestamp)
    if abs(ensure_utc(now).timestamp() - ts) > REPLAY_WINDOW.total_seconds():
        raise SlackRequestRejected(SlackRejectReason.STALE_TIMESTAMP)
    if not _SIGNATURE_RE.fullmatch(signature):
        raise SlackRequestRejected(SlackRejectReason.MALFORMED_SIGNATURE)
    secret = signing_secret.get_secret_value() if isinstance(signing_secret, SecretStr) else signing_secret
    expected = compute_slack_signature(secret, timestamp, bytes(raw_body))
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii")):
        raise SlackRequestRejected(SlackRejectReason.BAD_SIGNATURE)
    return ts


@dataclass(frozen=True, slots=True)
class SlackBinding:
    """The single workspace/app/channel and event types this system accepts."""

    team_id: str
    app_id: str
    channel_id: str
    allowed_event_types: frozenset[str]
    own_bot_id: str | None = None
    own_bot_user_id: str | None = None

    @classmethod
    def from_config(cls, config: SlackConfig, *, allowed_event_types: frozenset[str]) -> SlackBinding:
        if config.team_id is None or config.app_id is None:
            raise ValidationFailed("Slack binding requires team_id and app_id")
        return cls(
            team_id=config.team_id,
            app_id=config.app_id,
            channel_id=config.channel_id,
            allowed_event_types=allowed_event_types,
            own_bot_id=config.bot_id,
            own_bot_user_id=config.bot_user_id,
        )


class ProviderEventDedup(Protocol):
    """Durable implementations insert (provider, event_id) with ON CONFLICT DO NOTHING."""

    async def first_seen(self, provider: str, event_id: str, *, now: datetime) -> bool: ...


@dataclass
class InMemoryEventDedup:
    """Bounded process-local dedup for tests and single-process development."""

    ttl: timedelta = timedelta(hours=1)
    max_entries: int = 10_000
    _seen: OrderedDict[tuple[str, str], datetime] = field(default_factory=OrderedDict)

    async def first_seen(self, provider: str, event_id: str, *, now: datetime) -> bool:
        now = ensure_utc(now)
        while self._seen:
            key, at = next(iter(self._seen.items()))
            if now - at <= self.ttl:
                break
            del self._seen[key]
        key = (provider, event_id)
        if key in self._seen:
            return False
        self._seen[key] = now
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)
        return True


class InboundAction(StrEnum):
    URL_VERIFICATION = "url_verification"  # respond with the challenge
    ACCEPT = "accept"  # persist durably, then acknowledge and process asynchronously
    IGNORE_DUPLICATE = "ignore_duplicate"  # acknowledge, do nothing
    IGNORE_OWN_MESSAGE = "ignore_own_message"  # acknowledge, never react to our own alerts


@dataclass(frozen=True, slots=True)
class InboundDecision:
    action: InboundAction
    event_id: str | None = None
    event_type: str | None = None
    channel_id: str | None = None
    challenge: str | None = None
    event_refs: tuple[str, ...] = ()
    payload: Mapping[str, Any] | None = None


def _event_channel(event: Mapping[str, Any]) -> object:
    if "channel" in event:
        return event.get("channel")
    item = event.get("item")
    return item.get("channel") if isinstance(item, Mapping) else None


def _is_own_message(event: Mapping[str, Any], binding: SlackBinding) -> tuple[bool, tuple[str, ...]]:
    text = event.get("text")
    refs = tuple(_EVENT_REF_RE.findall(text)) if isinstance(text, str) else ()
    metadata = event.get("metadata")
    own_metadata = isinstance(metadata, Mapping) and metadata.get("event_type") == METADATA_EVENT_TYPE
    bot_authored = bool(event.get("bot_id")) or event.get("subtype") == "bot_message"
    own = (
        (binding.own_bot_id is not None and event.get("bot_id") == binding.own_bot_id)
        or (binding.own_bot_user_id is not None and event.get("user") == binding.own_bot_user_id)
        or event.get("app_id") == binding.app_id
        or own_metadata
        # A bot re-posting/forwarding our alert is a loop; a human quoting a reference is not.
        or (bot_authored and bool(refs))
    )
    return own, refs


async def handle_inbound_event(
    raw_body: bytes,
    headers: Mapping[str, str],
    *,
    signing_secret: SecretStr | str,
    binding: SlackBinding,
    dedup: ProviderEventDedup,
    now: datetime,
) -> InboundDecision:
    """Verify, bind, loop-check and dedup one Events API request (raises SlackRequestRejected)."""
    verify_slack_signature(signing_secret, raw_body, headers, now=now)
    try:
        payload = json.loads(bytes(raw_body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SlackRequestRejected(SlackRejectReason.INVALID_BODY) from None
    if not isinstance(payload, dict):
        raise SlackRequestRejected(SlackRejectReason.INVALID_BODY)
    envelope_type = payload.get("type")
    if envelope_type == "url_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str) or not 1 <= len(challenge) <= 256:
            raise SlackRequestRejected(SlackRejectReason.INVALID_BODY)
        return InboundDecision(action=InboundAction.URL_VERIFICATION, challenge=challenge)
    if envelope_type != "event_callback":
        raise SlackRequestRejected(SlackRejectReason.UNSUPPORTED_ENVELOPE)
    if payload.get("team_id") != binding.team_id:
        raise SlackRequestRejected(SlackRejectReason.WRONG_TEAM)
    if payload.get("api_app_id") != binding.app_id:
        raise SlackRequestRejected(SlackRejectReason.WRONG_APP)
    event = payload.get("event")
    if not isinstance(event, Mapping):
        raise SlackRequestRejected(SlackRejectReason.INVALID_BODY)
    event_type = event.get("type")
    if not isinstance(event_type, str) or event_type not in binding.allowed_event_types:
        raise SlackRequestRejected(SlackRejectReason.EVENT_TYPE_NOT_ALLOWED)
    if _event_channel(event) != binding.channel_id:
        raise SlackRequestRejected(SlackRejectReason.WRONG_CHANNEL)
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not _EVENT_ID_RE.fullmatch(event_id):
        raise SlackRequestRejected(SlackRejectReason.INVALID_EVENT_ID)
    own, refs = _is_own_message(event, binding)
    if own:
        return InboundDecision(
            action=InboundAction.IGNORE_OWN_MESSAGE,
            event_id=event_id,
            event_type=event_type,
            channel_id=binding.channel_id,
            event_refs=refs,
        )
    if not await dedup.first_seen("slack", event_id, now=now):
        return InboundDecision(
            action=InboundAction.IGNORE_DUPLICATE,
            event_id=event_id,
            event_type=event_type,
            channel_id=binding.channel_id,
        )
    return InboundDecision(
        action=InboundAction.ACCEPT,
        event_id=event_id,
        event_type=event_type,
        channel_id=binding.channel_id,
        event_refs=refs,
        payload=payload,
    )
