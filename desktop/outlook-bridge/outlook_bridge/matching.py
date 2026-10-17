"""Local reply correlation before any upload (spec 37.7; 37.8 client side).

The decision whether a message may leave the machine uses exactly the shared, pure semantics of
``suv_deals.domain.replies`` (classification, correlation, sanitising, attachment policy,
fingerprint, dedup key and request validation), so the worker and the backend agree byte for
byte. The domain package is installed next to the worker (no-deps; see README). It is imported
here directly; when it is missing the worker refuses to run (``SemanticsUnavailable``) rather
than correlating with a diverging copy.

Outcomes for one mailbox item:

``non_mail``           meeting/sharing/other non-mail item: ignored before reply processing.
``upload``             matched to exactly one inquiry -> full 37.8 upload.
``quarantine_upload``  exactly one candidate inquiry but not verified (forwarded, changed
                       address, thread-only, ...) -> uploaded flagged, never applied.
``pending_binding``    replies to Message-IDs the worker has no binding for yet: only a bounded
                       metadata locator is kept; it is re-read after the next binding sync.
``matching_gap``       an unresolved candidate (retry window over for a reply to this system's
                       Message-ID format, or several candidate inquiries): surfaced as a count.
``unrelated``          everything else: stays in the local mailbox; only a hashed key is kept.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import ValidationError

from outlook_bridge.errors import UploadBuildError
from outlook_bridge.local_queue import message_id_hash, sha256_text
from outlook_bridge.outlook_adapter import AttachmentDigest, MailSnapshot
from outlook_bridge.wire import (
    DELIVERY_REPORT_TYPES,
    MAX_RETURNED_MESSAGE_IDS,
    ReplyUpload,
    is_own_message_id_format,
)
from suv_deals.domain.enums import EmailProviderKind, MessageLanguage
from suv_deals.domain.language import detect_text_language
from suv_deals.domain.listings import sha256_json
from suv_deals.domain.replies import (
    CORRELATION_VERSION,
    FINGERPRINT_VERSION,
    MAX_RAW_BODY_CHARS,
    MAX_REQUEST_BYTES,
    REPLY_SCHEMA_VERSION,
    SANITIZER_VERSION,
    AttachmentAction,
    AttachmentDecision,
    AttachmentMeta,
    CorrelationOutcome,
    CorrelationResult,
    InboundMessage,
    InquiryBinding,
    MessageHeaders,
    ReplyIngestRequest,
    SanitizedReplyBody,
    SourceMessageIdentity,
    build_ingest_request,
    correlate_reply,
    evaluate_attachments,
    is_processable_item,
    normalize_message_id,
    parse_address_list,
    parse_delivery_report,
    safe_filename,
    sanitize_reply_body,
    unmatched_retention,
)
from suv_deals.errors import AppError

#: Placeholder digest for attachments that are never uploaded (classification uses metadata only).
SENTINEL_DIGEST: Final = "0" * 64
#: Optional domain extensions of the 37.8 request and their defaults; omitted when default (see
#: ``wire_payload``) so a matched seller reply is sent in exactly the spec v1.0 shape.
REQUEST_EXTENSION_DEFAULTS: Final[dict[str, Any]] = {
    "message_type": "seller_reply",
    "correlation_status": "matched",
    "correlation_reasons": [],
    "withheld_sensitive_attachments": 0,
    "returned_message_ids": [],
}
_MAX_LOCATOR_CHARS: Final = 1024
_FIT_REFERENCES: Final = 20
_TRUNCATION_MARKER: Final = "\n[truncated]"


class DecisionKind(StrEnum):
    NON_MAIL = "non_mail"
    UPLOAD = "upload"
    QUARANTINE_UPLOAD = "quarantine_upload"
    PENDING_BINDING = "pending_binding"
    MATCHING_GAP = "matching_gap"
    UNRELATED = "unrelated"


UPLOAD_KINDS: Final = frozenset({DecisionKind.UPLOAD, DecisionKind.QUARANTINE_UPLOAD})


@dataclass(frozen=True, slots=True)
class LocalKey:
    """Stable local identity of a message: never includes EntryID/StoreID locators."""

    dedup_key: str
    key_hash: str
    kind: Literal["internet_message_id", "content_hash"]
    internet_message_id: str | None


@dataclass(frozen=True)
class LocalDecision:
    kind: DecisionKind
    key: LocalKey
    correlation: CorrelationResult | None = None
    inbound: InboundMessage | None = None
    reference_hashes: frozenset[str] = frozenset()
    own_reference: bool = False
    retry_until: datetime | None = None
    gap_kind: str | None = None
    digest_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class PreparedUpload:
    request: ReplyUpload
    payload: dict[str, Any]
    body: bytes
    idempotency_key: str
    inquiry_id: UUID
    binding_version: int
    quarantined: bool


def semantics_versions() -> dict[str, str]:
    return {
        "reply_schema": REPLY_SCHEMA_VERSION,
        "fingerprint": FINGERPRINT_VERSION,
        "correlation": CORRELATION_VERSION,
        "sanitizer": SANITIZER_VERSION,
    }


def _safe_locator(value: str | None) -> str | None:
    if value is None or not value or len(value) > _MAX_LOCATOR_CHARS:
        return None
    if any(ord(c) < 33 or ord(c) == 127 for c in value):
        return None
    return value


def wire_payload(request: ReplyIngestRequest) -> dict[str, Any]:
    """The exact JSON body for ``POST /v1/mail-workers/replies`` (spec 37.8 v1.0 shape).

    A matched seller reply is sent in exactly the spec's v1.0 shape. The optional domain
    extensions appear only when they carry information the backend cannot derive itself: a
    message type other than ``seller_reply`` (auto-reply, bounce, delivery notice), a quarantined
    possible match with its reasons, the count of withheld sensitive attachments and, for a
    bounce/delivery notice, the returned original's Message-IDs (``wire.ReplyUpload``). Correlation
    reasons of a *matched* message are omitted: the backend re-derives the match from the
    headers and its own bindings and never trusts worker-supplied reasons.
    """
    data = request.model_dump(mode="json", by_alias=True)
    if data.get("correlation_status") == "matched":
        data.pop("correlation_reasons", None)
    for key, default in REQUEST_EXTENSION_DEFAULTS.items():
        if key in data and data[key] == default:
            data.pop(key)
    return data


def encode_payload(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def returned_message_ids(inbound: InboundMessage, request: ReplyIngestRequest) -> tuple[str, ...]:
    """The returned original's Message-IDs of a bounce/delivery notice, read from the RAW body
    before sanitising (the sanitiser removes the quoted original); empty for anything else."""
    if request.message_type not in DELIVERY_REPORT_TYPES:
        return ()
    found = parse_delivery_report(inbound.body_text).original_message_ids
    return tuple(dict.fromkeys(found))[:MAX_RETURNED_MESSAGE_IDS]


def as_upload(request: ReplyIngestRequest, returned: tuple[str, ...] = ()) -> ReplyUpload:
    """The validated wire upload (``wire.ReplyUpload``) of a domain ingest request."""
    data = request.model_dump(mode="json", by_alias=True)
    data["returned_message_ids"] = list(returned)
    return ReplyUpload.model_validate(data)


def idempotency_key_for(request: ReplyIngestRequest) -> str:
    """Stable per source message (survives restarts, retries and folder moves)."""
    return "mwr1-" + hashlib.sha256(request.dedup_key().as_string().encode("utf-8")).hexdigest()[:56]


class LocalMatcher:
    """Turns mailbox snapshots into local decisions and, for correlated replies, uploads."""

    def __init__(
        self,
        mailbox_binding_id: UUID,
        *,
        retry_window: timedelta,
        own_addresses: Sequence[str] = (),
    ) -> None:
        if retry_window <= timedelta(0):
            raise ValueError("retry window must be positive")
        self._mailbox = mailbox_binding_id
        self._retry_window = retry_window
        # The mailbox owner's own sender addresses: a copy of a manual reply by the owner in a
        # seller thread is never a seller reply and stays local (shared correlation rule).
        self._own_addresses = tuple(own_addresses)

    @property
    def mailbox_binding_id(self) -> UUID:
        return self._mailbox

    # ------------------------------------------------------------------ keys and conversion

    def local_key(self, snapshot: MailSnapshot) -> LocalKey:
        message_id = normalize_message_id(snapshot.internet_message_id)
        if message_id is not None:
            dedup = f"{self._mailbox}:internet_message_id:{message_id}"
            return LocalKey(dedup, sha256_text(dedup), "internet_message_id", message_id)
        senders = [a for value in snapshot.headers.get("from", ()) for a in parse_address_list(value)]
        material = {
            "from": senders[0] if len(senders) == 1 else None,
            "subject": unicodedata.normalize("NFC", snapshot.subject),
            "body": unicodedata.normalize("NFC", snapshot.body_text.replace("\r\n", "\n")),
            "attachments": [(a.filename, a.mime_type, a.byte_size) for a in snapshot.attachments],
            "received_at": snapshot.ref.received_at.isoformat() if snapshot.ref.received_at else None,
        }
        dedup = f"{self._mailbox}:local_content:{sha256_json(material)}"
        return LocalKey(dedup, sha256_text(dedup), "content_hash", None)

    @staticmethod
    def _attachment_meta(
        snapshot: MailSnapshot, digests: Mapping[int, AttachmentDigest]
    ) -> tuple[AttachmentMeta, ...]:
        metas: list[AttachmentMeta] = []
        for position, info in enumerate(snapshot.attachments):
            digest = digests.get(info.index)
            try:
                meta = AttachmentMeta(
                    filename=safe_filename(info.filename),
                    mime_type=info.mime_type,
                    byte_size=min(max(digest.byte_size if digest else info.byte_size, 0), 2**40),
                    sha256=digest.sha256 if digest else SENTINEL_DIGEST,
                    local_ref=f"att-{position}",
                )
            except ValidationError:
                meta = AttachmentMeta(
                    filename="attachment",
                    mime_type="application/octet-stream",
                    byte_size=0,
                    sha256=SENTINEL_DIGEST,
                    local_ref=f"att-{position}",
                )
            metas.append(meta)
        return tuple(metas)

    def to_inbound(
        self,
        snapshot: MailSnapshot,
        *,
        in_junk_folder: bool,
        digests: Mapping[int, AttachmentDigest] | None = None,
    ) -> InboundMessage:
        identity = SourceMessageIdentity(
            mailbox_binding_id=self._mailbox,
            provider=EmailProviderKind.OUTLOOK_LOCAL,
            internet_message_id=normalize_message_id(snapshot.internet_message_id),
            outlook_entry_id=_safe_locator(snapshot.ref.entry_id),
            outlook_store_id=_safe_locator(snapshot.ref.store_id),
            received_at=snapshot.ref.received_at,
        )
        return InboundMessage(
            identity=identity,
            headers=MessageHeaders.from_raw(snapshot.headers),
            body_text=snapshot.body_text[:MAX_RAW_BODY_CHARS],
            attachments=self._attachment_meta(snapshot, digests or {})[:200],
            in_junk_folder=in_junk_folder,
            message_class=snapshot.ref.message_class[:255] if snapshot.ref.message_class else None,
        )

    # ------------------------------------------------------------------ decision

    def evaluate(
        self,
        snapshot: MailSnapshot,
        bindings: Sequence[InquiryBinding],
        *,
        in_junk_folder: bool,
        first_seen_at: datetime,
        now: datetime,
        own_sent_hashes: frozenset[str] = frozenset(),
    ) -> LocalDecision:
        """Local decision for one item; ``own_sent_hashes`` are hashed Message-IDs Outlook actually
        used for this worker's own sent inquiries (``LocalStore.own_sent_message_id_hashes``)."""
        key = self.local_key(snapshot)
        if not is_processable_item(snapshot.ref.message_class):
            return LocalDecision(kind=DecisionKind.NON_MAIL, key=key)
        inbound = self.to_inbound(snapshot, in_junk_folder=in_junk_folder)
        correlation = correlate_reply(inbound, bindings, own_addresses=self._own_addresses)
        references = inbound.reference_ids()
        reference_hashes = frozenset(message_id_hash(r) for r in references)
        own_reference = any(is_own_message_id_format(r) for r in references) or bool(
            reference_hashes & own_sent_hashes
        )
        scope = correlation.upload_scope
        if scope in ("full", "quarantine"):
            allowed = [
                snapshot.attachments[d.index].index
                for d in evaluate_attachments(inbound.attachments, body_text=inbound.body_text)
                if d.action == AttachmentAction.ALLOW_VEHICLE_DOCUMENT and d.index < len(snapshot.attachments)
            ]
            return LocalDecision(
                kind=DecisionKind.UPLOAD if scope == "full" else DecisionKind.QUARANTINE_UPLOAD,
                key=key,
                correlation=correlation,
                inbound=inbound,
                reference_hashes=reference_hashes,
                own_reference=own_reference,
                digest_indices=tuple(allowed),
            )
        if correlation.outcome == CorrelationOutcome.QUARANTINED:
            # Several candidate inquiries: stays local, surfaced as a matching gap (count only).
            return LocalDecision(
                kind=DecisionKind.MATCHING_GAP,
                key=key,
                correlation=correlation,
                reference_hashes=reference_hashes,
                own_reference=own_reference,
                gap_kind="ambiguous_multi_inquiry",
            )
        retention = unmatched_retention(
            correlation, first_seen_locally_at=first_seen_at, now=now, window=self._retry_window
        )
        if retention.retain_locator:
            return LocalDecision(
                kind=DecisionKind.PENDING_BINDING,
                key=key,
                correlation=correlation,
                reference_hashes=reference_hashes,
                own_reference=own_reference,
                retry_until=retention.retry_until,
            )
        if retention.surface_matching_gap and own_reference:
            return LocalDecision(
                kind=DecisionKind.MATCHING_GAP,
                key=key,
                correlation=correlation,
                reference_hashes=reference_hashes,
                own_reference=True,
                gap_kind="unresolved_reply_matching",
            )
        return LocalDecision(kind=DecisionKind.UNRELATED, key=key, correlation=correlation)

    # ------------------------------------------------------------------ upload

    def prepare_upload(
        self,
        snapshot: MailSnapshot,
        decision: LocalDecision,
        digests: Mapping[int, AttachmentDigest],
        *,
        in_junk_folder: bool,
        observed_at: datetime,
    ) -> PreparedUpload:
        """Build the validated 37.8 request; only correlated decisions are accepted."""
        correlation = decision.correlation
        if decision.kind not in UPLOAD_KINDS or correlation is None or correlation.upload_scope == "none":
            raise UploadBuildError("only correlated replies can be uploaded")
        inbound = self.to_inbound(snapshot, in_junk_folder=in_junk_folder, digests=digests)
        hashed = set(digests)
        decisions = tuple(
            self._require_digest(d, snapshot, hashed)
            for d in evaluate_attachments(inbound.attachments, body_text=inbound.body_text)
        )
        sanitized = sanitize_reply_body(inbound.body_text)
        detected = detect_text_language(sanitized.text).language
        language = MessageLanguage(detected) if detected in {m.value for m in MessageLanguage} else None
        request = self._build_fitting(
            inbound,
            correlation,
            sanitized=sanitized,
            decisions=decisions,
            language=language,
            observed_at=observed_at,
        )
        if any(a.sha256 == SENTINEL_DIGEST for a in request.attachments):
            raise UploadBuildError("an attachment without a computed digest cannot be uploaded")
        if request.mailbox_binding_id != self._mailbox or request.inquiry_id != correlation.inquiry_id:
            raise UploadBuildError("upload does not belong to this mailbox/inquiry")
        try:
            upload = as_upload(request, returned_message_ids(inbound, request))
        except ValidationError:
            raise UploadBuildError("correlated reply cannot be represented as a valid upload") from None
        payload = wire_payload(upload)
        body = encode_payload(payload)
        if len(body) > MAX_REQUEST_BYTES:
            raise UploadBuildError("upload exceeds 128 KiB")
        return PreparedUpload(
            request=upload,
            payload=payload,
            body=body,
            idempotency_key=idempotency_key_for(upload),
            inquiry_id=upload.inquiry_id,
            binding_version=upload.binding_version,
            quarantined=upload.correlation_status == "quarantined",
        )

    @staticmethod
    def _require_digest(
        decision: AttachmentDecision, snapshot: MailSnapshot, hashed: set[int]
    ) -> AttachmentDecision:
        if decision.action != AttachmentAction.ALLOW_VEHICLE_DOCUMENT:
            return decision
        if (
            decision.index < len(snapshot.attachments)
            and snapshot.attachments[decision.index].index in hashed
        ):
            return decision
        return AttachmentDecision(
            index=decision.index, action=AttachmentAction.REJECT, reasons=("DIGEST_UNAVAILABLE",)
        )

    def _build_fitting(
        self,
        inbound: InboundMessage,
        correlation: CorrelationResult,
        *,
        sanitized: SanitizedReplyBody,
        decisions: Sequence[AttachmentDecision],
        language: MessageLanguage | None,
        observed_at: datetime,
    ) -> ReplyIngestRequest:
        """Build the request; deterministically trim references, then the body, to fit 128 KiB."""
        message = inbound
        for step in range(8):
            try:
                return build_ingest_request(
                    message,
                    correlation,
                    sanitized=sanitized,
                    attachment_decisions=decisions,
                    detected_language=language,
                    observed_at=observed_at,
                )
            except (ValidationError, AppError) as exc:
                if "128 KiB" not in str(exc) or step == 7:
                    raise UploadBuildError(
                        "correlated reply cannot be represented as a valid upload"
                    ) from None
            if step == 0 and len(message.references) > _FIT_REFERENCES:
                kept = (
                    *message.references[: _FIT_REFERENCES // 2],
                    *message.references[-_FIT_REFERENCES // 2 :],
                )
                values = dict(message.headers.values)
                values["references"] = (" ".join(kept),)
                message = message.model_copy(update={"headers": MessageHeaders(values=values)})
                continue
            text = sanitized.text
            keep = max(0, len(text.encode("utf-8")) // 2)
            cut = text.encode("utf-8")[:keep].decode("utf-8", errors="ignore").rstrip()
            sanitized = sanitized.model_copy(update={"text": cut + _TRUNCATION_MARKER, "truncated": True})
        raise UploadBuildError("correlated reply cannot be represented as a valid upload")


__all__ = [
    "REQUEST_EXTENSION_DEFAULTS",
    "SENTINEL_DIGEST",
    "UPLOAD_KINDS",
    "DecisionKind",
    "LocalDecision",
    "LocalKey",
    "LocalMatcher",
    "PreparedUpload",
    "as_upload",
    "encode_payload",
    "idempotency_key_for",
    "returned_message_ids",
    "semantics_versions",
    "wire_payload",
]
