"""HTTP client for the mailbox-worker API (spec 37.8; client side).

Endpoints (the worker can never select a workspace or another mailbox; the server derives both
from the credential):

``GET  /v1/mail-workers/inquiry-bindings?cursor=&limit=``   binding changes incl. tombstones
``POST /v1/mail-workers/replies``                            one correlated reply (37.8 v1.0 shape)
``GET  /v1/mail-workers/send-intents?limit=``               pending ``outlook_local`` send intents
``POST /v1/mail-workers/send-intents/{id}/claim``           revalidation immediately before ``.Send``
``POST /v1/mail-workers/send-intents/{id}/report``          submission/evidence report
``POST /v1/mail-workers/heartbeat``                         health, checkpoints and coverage gaps
``POST /v1/mail-workers/account-report``                    classic-Outlook account verification

The two spec paths (bindings, replies) are normative; the send-intent, heartbeat and
account-report paths are this package's proposal for the backend contract.

Transport rules: HTTPS only (plain HTTP only for an explicitly allowed loopback development
backend), redirects are never followed (the bearer token must not travel to another origin),
responses are size-capped, request bodies of ``POST /replies`` are capped at 128 KiB and carry an
``Idempotency-Key`` header. Failures are typed: ``400/422`` validation, ``401`` unauthenticated
(credential rejected: transmission stops, backlog kept), ``403`` forbidden, ``409`` conflict
(``IDEMPOTENCY_CONFLICT`` vs other conflicts), ``429`` rate limited (Retry-After honoured),
``5xx``/``503`` unavailable and transport errors - the last two are transient.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import EmailProviderKind
from suv_deals.domain.replies import MAX_REQUEST_BYTES, InquiryBinding, InquiryBindingState

from outlook_bridge import WORKER_SOFTWARE
from outlook_bridge.local_queue import BindingRecord
from outlook_bridge.wire import (
    ClaimDecision,
    HeartbeatAck,
    HeartbeatEnvelope,
    SendIntentBatch,
    WorkerAccountReport,
    WorkerSendReport,
)

BINDINGS_PATH: Final = "/v1/mail-workers/inquiry-bindings"
REPLIES_PATH: Final = "/v1/mail-workers/replies"
SEND_INTENTS_PATH: Final = "/v1/mail-workers/send-intents"
HEARTBEAT_PATH: Final = "/v1/mail-workers/heartbeat"
ACCOUNT_REPORT_PATH: Final = "/v1/mail-workers/account-report"
MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024
MAX_RETRY_AFTER_SECONDS: Final = 3600
_CURSOR_RE: Final = re.compile(r"^[\x21-\x7e]{1,1024}$")
_REQUEST_ID_RE: Final = re.compile(r"^[\x21-\x7e]{1,200}$")


class ApiErrorKind(StrEnum):
    VALIDATION = "validation"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    TRANSPORT = "transport"
    PROTOCOL = "protocol"
    REQUEST_TOO_LARGE = "request_too_large"


TRANSIENT_KINDS: Final = frozenset(
    {ApiErrorKind.RATE_LIMITED, ApiErrorKind.UNAVAILABLE, ApiErrorKind.TRANSPORT, ApiErrorKind.PROTOCOL}
)


class BridgeApiError(Exception):
    """Typed API failure; the message never contains tokens or mail content."""

    def __init__(
        self,
        kind: ApiErrorKind,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        retry_after_seconds: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.request_id = request_id

    @property
    def transient(self) -> bool:
        return self.kind in TRANSIENT_KINDS

    def __repr__(self) -> str:
        return f"BridgeApiError({self.kind.value}, status={self.status}, code={self.code})"


# =============================================================================================
# Response models
# =============================================================================================


class BindingSyncItem(BaseModel):
    """One binding change. Tombstones carry identity, version and state only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inquiry_id: UUID
    binding_version: int = Field(ge=1)
    mailbox_binding_id: UUID
    state: InquiryBindingState
    provider: EmailProviderKind | None = None
    outbound_message_ids: tuple[str, ...] = ()
    send_intent_message_ids: tuple[str, ...] = ()
    provider_message_ids: tuple[str, ...] = ()
    provider_thread_ids: tuple[str, ...] = ()
    verified_seller_aliases: tuple[str, ...] = ()
    listing_references: tuple[str, ...] = ()
    listing_urls: tuple[str, ...] = ()
    listing_id: UUID | None = None
    vehicle_cluster_id: UUID | None = None
    is_canary: bool = False

    @model_validator(mode="after")
    def _tombstone_shape(self) -> BindingSyncItem:
        if self.state == InquiryBindingState.TOMBSTONED:
            payload = (
                self.outbound_message_ids,
                self.send_intent_message_ids,
                self.provider_message_ids,
                self.provider_thread_ids,
                self.verified_seller_aliases,
                self.listing_references,
                self.listing_urls,
            )
            if any(payload) or self.listing_id or self.vehicle_cluster_id:
                raise ValueError("a tombstone carries no binding payload")
        elif self.provider is None:
            raise ValueError("an active binding names its sending provider")
        else:
            self._binding()  # the payload must satisfy the shared domain binding rules
        return self

    def _binding(self) -> InquiryBinding:
        return InquiryBinding.model_validate(self.model_dump())

    def to_record(self) -> BindingRecord:
        binding = None if self.state == InquiryBindingState.TOMBSTONED else self._binding()
        return BindingRecord(
            inquiry_id=self.inquiry_id,
            binding_version=self.binding_version,
            mailbox_binding_id=self.mailbox_binding_id,
            state=self.state,
            binding=binding,
        )


class BindingPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: Literal["1.0"]
    items: tuple[BindingSyncItem, ...] = Field(default=(), max_length=1000)
    next_cursor: str | None = None
    has_more: bool = False

    @field_validator("next_cursor")
    @classmethod
    def _cursor(cls, value: str | None) -> str | None:
        if value is not None and not _CURSOR_RE.fullmatch(value):
            raise ValueError("cursor must be an opaque printable token")
        return value


class IngestAck(BaseModel):
    """Successful ``POST /replies`` answer (spec 37.8)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: Literal["1.0"]
    reply_id: UUID
    inquiry_id: UUID
    ingest_status: Literal["stored", "quarantined"]
    duplicate: bool
    request_id: str = Field(min_length=1, max_length=200)
    ingested_at: datetime

    @field_validator("ingested_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    mailbox_binding_id: UUID
    worker_id: str


# =============================================================================================
# Client
# =============================================================================================


class BridgeApiClient:
    """Synchronous client (the worker loop is single-threaded)."""

    def __init__(
        self,
        base_url: str,
        *,
        token_provider: Callable[[], str],
        identity: ClientIdentity,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token_provider = token_provider
        self._identity = identity
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": WORKER_SOFTWARE, "Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ plumbing

    def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: bytes | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        token = self._token_provider()  # raises when the credential is missing/unusable
        request_id = f"mw-{uuid4().hex}"
        headers = {"Authorization": f"Bearer {token}", "X-Request-Id": request_id}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            with self._client.stream(
                method, self._base + path, params=dict(params or {}), content=body, headers=headers
            ) as response:
                raw = self._read_capped(response)
                status = response.status_code
                retry_after_header = response.headers.get("Retry-After")
        except httpx.TimeoutException:
            raise BridgeApiError(ApiErrorKind.TRANSPORT, "request timed out", request_id=request_id) from None
        except httpx.HTTPError as exc:
            raise BridgeApiError(
                ApiErrorKind.TRANSPORT, f"transport error ({type(exc).__name__})", request_id=request_id
            ) from None
        if 300 <= status < 400:
            raise BridgeApiError(
                ApiErrorKind.PROTOCOL, "redirects are not followed", status=status, request_id=request_id
            )
        data = self._json(raw, status, request_id)
        if status >= 400:
            raise self._error(status, data, retry_after_header, request_id)
        return data, request_id

    @staticmethod
    def _read_capped(response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise BridgeApiError(ApiErrorKind.PROTOCOL, "response too large", status=response.status_code)
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _json(raw: bytes, status: int, request_id: str) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if status >= 400:
                return {}
            raise BridgeApiError(
                ApiErrorKind.PROTOCOL, "response is not JSON", status=status, request_id=request_id
            ) from None
        if not isinstance(data, dict):
            raise BridgeApiError(ApiErrorKind.PROTOCOL, "response is not an object", status=status, request_id=request_id)
        return data

    @staticmethod
    def _retry_after(header: str | None, error: Mapping[str, Any]) -> int | None:
        candidates: list[Any] = [error.get("retry_after_seconds"), header]
        for value in candidates:
            try:
                seconds = int(str(value).strip()) if value is not None else None
            except ValueError:
                continue
            if seconds is not None and seconds >= 0:
                return max(1, min(seconds, MAX_RETRY_AFTER_SECONDS))
        return None

    def _error(self, status: int, data: Mapping[str, Any], retry_after: str | None, request_id: str) -> BridgeApiError:
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        assert isinstance(error, dict)
        code = error.get("code") if isinstance(error.get("code"), str) else None
        code = code[:64] if code else None
        server_request = data.get("request_id")
        rid = server_request if isinstance(server_request, str) and _REQUEST_ID_RE.fullmatch(server_request) else request_id
        if status in (400, 422):
            kind = ApiErrorKind.VALIDATION
        elif status == 401:
            kind = ApiErrorKind.UNAUTHENTICATED
        elif status == 403:
            kind = ApiErrorKind.FORBIDDEN
        elif status == 404:
            kind = ApiErrorKind.NOT_FOUND
        elif status == 409:
            kind = ApiErrorKind.IDEMPOTENCY_CONFLICT if code == "IDEMPOTENCY_CONFLICT" else ApiErrorKind.CONFLICT
        elif status == 413:
            kind = ApiErrorKind.REQUEST_TOO_LARGE
        elif status == 429:
            kind = ApiErrorKind.RATE_LIMITED
        elif status >= 500:
            kind = ApiErrorKind.UNAVAILABLE
        else:
            kind = ApiErrorKind.PROTOCOL
        return BridgeApiError(
            kind,
            f"mail-worker API answered {status}",
            status=status,
            code=code,
            retry_after_seconds=self._retry_after(retry_after, error) if kind == ApiErrorKind.RATE_LIMITED else None,
            request_id=rid,
        )

    @staticmethod
    def _parse[M: BaseModel](model: type[M], data: Mapping[str, Any], request_id: str | None) -> M:
        try:
            return model.model_validate(dict(data))
        except ValidationError:
            raise BridgeApiError(
                ApiErrorKind.PROTOCOL, f"unexpected {model.__name__} response shape", request_id=request_id
            ) from None

    # ------------------------------------------------------------------ bindings

    def fetch_binding_page(self, cursor: str | None, *, limit: int) -> BindingPage:
        params = {"limit": str(max(1, min(limit, 100)))}
        if cursor is not None:
            params["cursor"] = cursor
        data, request_id = self._request("GET", BINDINGS_PATH, params=params)
        return self._parse(BindingPage, data, request_id)

    # ------------------------------------------------------------------ replies

    def post_reply(self, body: bytes, *, idempotency_key: str, expected_inquiry_id: UUID) -> IngestAck:
        if len(body) > MAX_REQUEST_BYTES:
            raise BridgeApiError(ApiErrorKind.REQUEST_TOO_LARGE, "reply upload exceeds 128 KiB")
        data, request_id = self._request("POST", REPLIES_PATH, body=body, idempotency_key=idempotency_key)
        ack = self._parse(IngestAck, data, request_id)
        if ack.inquiry_id != expected_inquiry_id:
            raise BridgeApiError(ApiErrorKind.PROTOCOL, "acknowledgement names another inquiry", request_id=request_id)
        return ack

    # ------------------------------------------------------------------ send intents

    def fetch_send_intents(self, *, limit: int = 10) -> SendIntentBatch:
        data, request_id = self._request("GET", SEND_INTENTS_PATH, params={"limit": str(max(1, min(limit, 50)))})
        batch = self._parse(SendIntentBatch, data, request_id)
        for intent in batch.intents:
            if intent.mailbox_binding_id != self._identity.mailbox_binding_id:
                raise BridgeApiError(ApiErrorKind.PROTOCOL, "send intent for another mailbox", request_id=request_id)
        return batch

    def claim_send_intent(self, intent_id: UUID) -> ClaimDecision:
        path = f"{SEND_INTENTS_PATH}/{quote(str(intent_id))}/claim"
        body = json.dumps(
            {
                "schema_version": "1.0",
                "intent_id": str(intent_id),
                "mailbox_binding_id": str(self._identity.mailbox_binding_id),
                "worker_id": self._identity.worker_id,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        data, request_id = self._request("POST", path, body=body, idempotency_key=f"claim-{intent_id}")
        decision = self._parse(ClaimDecision, data, request_id)
        if decision.intent_id != intent_id:
            raise BridgeApiError(ApiErrorKind.PROTOCOL, "claim answer names another intent", request_id=request_id)
        return decision

    def post_send_report(self, report: WorkerSendReport) -> None:
        path = f"{SEND_INTENTS_PATH}/{quote(str(report.intent_id))}/report"
        body = report.model_dump_json().encode("utf-8")
        key = f"report-{report.intent_id}-{report.state.value}"
        self._request("POST", path, body=body, idempotency_key=key[:128])

    # ------------------------------------------------------------------ health

    def post_heartbeat(self, envelope: HeartbeatEnvelope) -> HeartbeatAck:
        data, request_id = self._request("POST", HEARTBEAT_PATH, body=envelope.model_dump_json().encode("utf-8"))
        return self._parse(HeartbeatAck, data, request_id)

    def post_account_report(self, report: WorkerAccountReport) -> None:
        self._request("POST", ACCOUNT_REPORT_PATH, body=report.model_dump_json().encode("utf-8"))


__all__ = [
    "ACCOUNT_REPORT_PATH",
    "BINDINGS_PATH",
    "HEARTBEAT_PATH",
    "MAX_RESPONSE_BYTES",
    "REPLIES_PATH",
    "SEND_INTENTS_PATH",
    "TRANSIENT_KINDS",
    "ApiErrorKind",
    "BindingPage",
    "BindingSyncItem",
    "BridgeApiClient",
    "BridgeApiError",
    "ClientIdentity",
    "IngestAck",
]
