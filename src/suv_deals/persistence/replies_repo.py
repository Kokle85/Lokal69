"""Correlated seller-reply ingest (spec 37.7, 37.8 ``POST /v1/mail-workers/replies``).

`ingest_reply` stores ONE inquiry-correlated message uploaded by the mailbox worker and applies
its consequences in ONE transaction (the caller's ``unit_of_work`` of the worker's workspace):

1. **Scope** - the body's mailbox must be the worker's own; the inquiry must exist in the worker's
   workspace AND have been sent from the worker's mailbox (its sender binding). Anything else is
   the same ``FORBIDDEN`` (``mailbox_binding_mismatch``): another mailbox's or another workspace's
   inquiry is indistinguishable from an unknown one (no existence leak). The inquiry row is
   locked first (it serialises replies of one inquiry and binding publication).
2. **Idempotency** - both the request ``Idempotency-Key`` and the stable source identity
   (``ReplyIngestRequest.dedup_key``: Internet Message-ID, else provider id, else a content hash)
   are checked in ``ops.mail_ingest_dedup`` (``domain.replies.decide_ingest``); neither is
   trusted alone. Same identity + same immutable fingerprint -> the original ``reply_id`` with
   ``duplicate: true`` (also with another key, after a folder move or a later scan; a new Outlook
   locator goes to ``app.seller_reply_locators``). The same key for another message, or the same
   identity with different content (or claimed for another inquiry), is an
   ``IDEMPOTENCY_CONFLICT``: the conflicting upload is kept as a quarantined
   ``idempotency_conflict`` reply next to (never over) the original and the dedup counters move;
   the outcome carries the conflict and `ReplyIngestOutcome.raise_for_conflict` raises it AFTER
   the commit (`ingest` does that), so the quarantine record survives the ``409``.
3. **Binding** - the named ``binding_version`` must be published to this mailbox and not be a
   tombstone, and the inquiry's newest binding must not be a tombstone (``409``
   ``binding_version_unpublished`` / ``403`` ``inquiry_binding_tombstoned``); the database
   re-checks all of it.
4. **Correlation is recomputed here** - ``domain.replies.correlate_reply`` runs on the uploaded
   From/In-Reply-To/References/subject/body against the binding the worker named (plus the newest
   bindings of the mailbox's other inquiries for the conflicting-reference check). The worker's
   ``correlation_status``/``correlation_reasons`` are never trusted: a message the server cannot
   link to this inquiry is refused (``VALIDATION_ERROR``, ``reply_not_correlated``; nothing is
   stored), a possible match is stored quarantined, and a worker's own quarantine is kept. The
   worker's message type is accepted only when it is not "upgrading" to ``seller_reply`` against
   the server's classification of the uploaded fields. A bounce/delivery notice whose returned
   original the worker's sanitiser removed (no In-Reply-To/References link) is stored
   quarantined (``dsn_link_unverified``) for verification and has no effect.
5. **Store** - the reply (sanitised body and subject as uploaded, original language, safe
   attachment metadata with the server's policy decision, Macedonian structured summary, claims),
   the dedup record and the locator.
6. **Effects** (matched, unquarantined messages only; ``domain.replies.decide_reply_processing``):
   inquiry transitions (``uncertain -> accepted`` first, with the attempt's reconciliation
   recorded as a correlated inbound message, then ``replied``/``bounced``/``seller_opted_out``);
   suppressions (hard bounce -> the address; opt-out/complaint -> the seller and the address; never
   an acknowledgement); availability evidence through ``persistence.availability_repo`` (a "sold"
   statement is ``sold_claimed`` + ``seller_reported_sold``; a contradiction suppresses outreach
   for the vehicle); valuation invalidation of the vehicle's listings (open valuations marked
   stale and a deduplicated recomputation queued); a quoted price stays an unaccepted seller quote
   in the claims and never touches the advertised price; the binding is re-published when the
   inquiry state changed; and the minimal ``seller.reply.received.v1`` outbox signal (event id,
   inquiry id, reply id, listing/cluster id, safe dashboard URL, brief status: no body, address,
   name or price; a fixture listing's signal is stored ``blocked``). Optionally a
   ``seller_reply_process`` job for follow-up processing (``ReplyIngestOptions``).

Lock order: ``app.seller_inquiries`` -> ``ops.mail_ingest_dedup`` -> ``app.seller_replies`` ->
``ops.email_delivery_attempts`` -> ``app.listings`` -> ``app.valuations`` -> ``ops.jobs`` ->
``ops.mail_worker_bindings`` (binding sequence) -> ``ops.outbox``. A concurrent duplicate that loses
a unique race raises ``TransientConflict`` (the whole unit of work is re-run by `ingest`). No
network I/O; message text, addresses and tokens are never logged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from suv_deals.api.schemas import MailWorkerBindingItem, MailWorkerReplyAck
from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import InquiryState, JobType, ReplyMessageType, SuppressionReason
from suv_deals.domain.inquiries import ReconciliationEvidence
from suv_deals.domain.listings import canonical_json
from suv_deals.domain.replies import (
    FINGERPRINT_VERSION,
    MK_SUMMARY_VERSION,
    SANITIZER_VERSION,
    AttachmentAction,
    AttachmentDecision,
    AttachmentMeta,
    CorrelationOutcome,
    CorrelationReason,
    CorrelationResult,
    InboundMessage,
    IngestDecision,
    IngestDecisionKind,
    InquiryBindingState,
    ReplyClaims,
    ReplyIngestRequest,
    ReplyProcessingDecision,
    ReplySourceContent,
    SourceMessageIdentity,
    StoredReplyIngest,
    build_mk_summary,
    build_seller_reply_signal,
    classify_message,
    correlate_reply,
    decide_ingest,
    decide_reply_processing,
    evaluate_attachments,
    extract_reply_claims,
    parse_delivery_report,
    source_content_fingerprint,
)
from suv_deals.domain.valuation import InvalidationReason
from suv_deals.errors import AppError, Forbidden, IdempotencyConflict, ValidationFailed, VersionConflict
from suv_deals.persistence import audit, availability_repo, jobs, mail_workers_repo, outbox, valuation_repo
from suv_deals.persistence.database import Conn, Database, fetch_all, fetch_one
from suv_deals.persistence.errors_map import TransientConflict, mapped_errors
from suv_deals.persistence.mail_workers_repo import WorkerIdentity, mailbox_mismatch
from suv_deals.persistence.transactions import retry_transient, unit_of_work

SCHEMA_VERSION: Final = "1.0"
DEFAULT_DASHBOARD_BASE_URL: Final = "http://127.0.0.1:8000"
MAX_CLAIM_ITEMS: Final = 20
MAX_CLAIMS_BYTES: Final = 60_000
MAX_SUMMARY_BYTES: Final = 32_000
REPLY_PROCESS_PREFIX: Final = "seller_reply.process"

IngestStatus = Literal["stored", "quarantined"]

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_UPGRADE_TYPES: Final = frozenset({ReplyMessageType.SELLER_REPLY})
_DSN_TYPES: Final = frozenset({ReplyMessageType.BOUNCE, ReplyMessageType.DELIVERY_NOTICE})
_ALWAYS_QUARANTINED: Final = frozenset({ReplyMessageType.SPAM, ReplyMessageType.AMBIGUOUS})
_STRONG_LINKS: Final = frozenset(
    {CorrelationReason.HEADER_REFERENCE_MATCH, CorrelationReason.RETURNED_ORIGINAL_MATCH}
)
#: Correlation reasons that quarantine a possible match, in reporting priority.
_QUARANTINE_REASONS: Final[tuple[tuple[CorrelationReason, str], ...]] = (
    (CorrelationReason.SPAM_MESSAGE, "spam_message"),
    (CorrelationReason.AMBIGUOUS_MESSAGE, "ambiguous_message"),
    (CorrelationReason.MULTIPLE_INQUIRIES, "multiple_inquiries"),
    (CorrelationReason.CONFLICTING_REFERENCE, "conflicting_reference"),
    (CorrelationReason.FORWARDED, "forwarded_message"),
    (CorrelationReason.CHANGED_ADDRESS, "changed_address"),
    (CorrelationReason.AMBIGUOUS_SENDER, "ambiguous_sender"),
    (CorrelationReason.DSN_RECIPIENT_MISMATCH, "dsn_recipient_mismatch"),
    (CorrelationReason.THREAD_ONLY_UNCORROBORATED, "thread_only_link"),
    (CorrelationReason.SENDER_ONLY_NO_THREAD, "possible_match_without_link"),
)
_RACE_CONSTRAINTS: Final = (
    "mail_ingest_dedup_key_uk",
    "mail_ingest_dedup_idempotency_uk",
    "mail_ingest_dedup_reply_uk",
    "seller_replies_message_uidx",
    "seller_replies_provider_message_uidx",
    "seller_replies_fingerprint_uidx",
    "seller_replies_inquiry_message_uidx",
    "seller_replies_conflict_uidx",
)
_IDEMPOTENCY_KEY_CHARS: Final = range(0x21, 0x7F)


@dataclass(frozen=True, slots=True)
class ReplyIngestOptions:
    """Deployment options (the API passes ``Settings.app_base_url`` as the dashboard base)."""

    dashboard_base_url: str = DEFAULT_DASHBOARD_BASE_URL
    #: Write the ``seller.reply.received.v1`` outbox row for a matched seller reply.
    emit_signal: bool = True
    #: Queue a ``seller_reply_process`` job for follow-up processing (e.g. a full translation once
    #: an approved provider exists). The deterministic claims and summary are computed at ingest.
    enqueue_processing_job: bool = False


DEFAULT_INGEST_OPTIONS: Final = ReplyIngestOptions()


class ReplyIngestOutcome(BaseModel):
    """The ingest result: the 37.8 acknowledgement fields plus what happened (for tests/audit)."""

    model_config = _FROZEN

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    reply_id: UUID
    inquiry_id: UUID
    ingest_status: IngestStatus
    duplicate: bool
    request_id: str
    ingested_at: datetime
    conflict: bool = False
    correlation_outcome: CorrelationOutcome | None = None
    message_type: ReplyMessageType | None = None
    quarantine_reason: str | None = None
    transitions: tuple[InquiryState, ...] = ()
    suppression_ids: tuple[UUID, ...] = ()
    availability_event_id: UUID | None = None
    availability_conflict: bool = False
    stale_valuation_ids: tuple[UUID, ...] = ()
    recompute_job_ids: tuple[UUID, ...] = ()
    outbox_event_id: UUID | None = None
    processing_job_id: UUID | None = None

    def ack(self) -> MailWorkerReplyAck:
        """The wire acknowledgement (``POST /v1/mail-workers/replies`` 200 body)."""
        return MailWorkerReplyAck(
            schema_version="1.0",
            reply_id=self.reply_id,
            inquiry_id=self.inquiry_id,
            ingest_status=self.ingest_status,
            duplicate=self.duplicate,
            request_id=self.request_id,
            ingested_at=self.ingested_at,
        )

    def raise_for_conflict(self) -> None:
        """``409 IDEMPOTENCY_CONFLICT`` for a conflicting upload (call after the commit)."""
        if self.conflict:
            raise IdempotencyConflict("The source message conflicts with an already ingested message")


# --------------------------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------------------------


async def ingest(
    db: Database,
    worker: WorkerIdentity,
    request: ReplyIngestRequest,
    idempotency_key: str,
    *,
    request_id: str,
    now: datetime | None = None,
    options: ReplyIngestOptions = DEFAULT_INGEST_OPTIONS,
) -> MailWorkerReplyAck:
    """Run `ingest_reply` in its own unit of work (re-run on a lost unique race), then raise a
    conflict AFTER the commit so its quarantine record is kept. Returns the wire acknowledgement."""

    async def run() -> ReplyIngestOutcome:
        async with unit_of_work(db, worker.actor(request_id)) as conn:
            return await ingest_reply(
                conn,
                worker,
                request,
                idempotency_key,
                now or datetime.now(UTC),
                request_id=request_id,
                options=options,
            )

    outcome = await retry_transient(run)
    outcome.raise_for_conflict()
    return outcome.ack()


async def ingest_reply(
    conn: Conn,
    worker: WorkerIdentity,
    request: ReplyIngestRequest,
    idempotency_key: str,
    now: datetime,
    *,
    request_id: str,
    options: ReplyIngestOptions = DEFAULT_INGEST_OPTIONS,
) -> ReplyIngestOutcome:
    """Store one correlated reply and apply its effects (see module docstring).

    Run inside ``unit_of_work(db, worker.actor(request_id))``. A conflict is RETURNED
    (``outcome.conflict``), not raised, so the caller commits the quarantine record first.
    """
    _check_idempotency_key(idempotency_key)
    current = ensure_utc(now)
    worker.require_mailbox(request.mailbox_binding_id)
    await mail_workers_repo.require_active_mailbox(conn, worker)
    inquiry = await _lock_inquiry(conn, worker, request.inquiry_id)
    async with mapped_errors(unique=_races()):
        return await _ingest_locked(
            conn,
            worker,
            request,
            key=idempotency_key,
            now=current,
            inquiry=inquiry,
            request_id=request_id,
            options=options,
        )


def _races() -> dict[str, Any]:
    return dict.fromkeys(_RACE_CONSTRAINTS, _race)


def _race() -> AppError:
    return TransientConflict("A concurrent upload of the same message won; retry")


def _check_idempotency_key(key: object) -> None:
    if (
        not isinstance(key, str)
        or not 8 <= len(key) <= 128
        or any(ord(c) not in _IDEMPOTENCY_KEY_CHARS for c in key)
    ):
        raise ValidationFailed(
            "Idempotency-Key must be 8-128 printable ASCII characters",
            details={"fields": ["Idempotency-Key"]},
        )


_INQUIRY_SQL: Final = """
select i.id, i.state, i.sender_binding_id, i.sender_from_address, i.sender_reply_to_address,
       i.recipient_address, i.seller_entity_id, i.qualification_listing_id, i.vehicle_cluster_id,
       i.vehicle_key, i.send_attempted_at, l.is_fixture
  from app.seller_inquiries i
  join app.listings l on l.workspace_id = i.workspace_id and l.id = i.qualification_listing_id
 where i.workspace_id = %(ws)s and i.id = %(id)s
   for update of i
"""


async def _lock_inquiry(conn: Conn, worker: WorkerIdentity, inquiry_id: UUID) -> Mapping[str, Any]:
    async with mapped_errors():
        row = await fetch_one(conn, _INQUIRY_SQL, {"ws": worker.workspace_id, "id": inquiry_id})
    if row is None or row["sender_binding_id"] != worker.sender_binding_id:
        raise mailbox_mismatch()
    if row["send_attempted_at"] is None:
        raise VersionConflict("The inquiry was never transmitted", reason="inquiry_not_transmitted")
    return row


# --------------------------------------------------------------------------------------------
# Dedup
# --------------------------------------------------------------------------------------------

_DEDUP_SQL: Final = """
select d.id, d.dedup_key, d.idempotency_key, d.fingerprint, d.fingerprint_version, d.inquiry_id,
       d.reply_id, d.ingest_result
  from ops.mail_ingest_dedup d
 where d.workspace_id = %(ws)s and d.mailbox_binding_id = %(box)s
   and (d.dedup_key = %(key)s or d.idempotency_key = %(idem)s)
 order by d.created_at
   for update
"""
_STORED_REPLY_SQL: Final = """
select id, inquiry_id, internet_message_id, provider_message_id, from_address, in_reply_to, reference_ids,
       subject, sanitized_body, attachments, source_fingerprint, quarantined, ingested_at
  from app.seller_replies
 where workspace_id = %(ws)s and id = %(id)s
"""


def _stored(
    row: Mapping[str, Any], fingerprint: str | None = None, version: str | None = None
) -> StoredReplyIngest:
    return StoredReplyIngest(
        reply_id=row["reply_id"],
        dedup_key=row["dedup_key"],
        idempotency_key=row["idempotency_key"],
        fingerprint=fingerprint or row["fingerprint"],
        fingerprint_version=version or row["fingerprint_version"],
    )


def stored_source_content(reply: Mapping[str, Any]) -> ReplySourceContent:
    """The immutable source content of a stored reply (to recompute an old fingerprint version)."""
    attachments = tuple(
        AttachmentMeta(
            filename=a["filename"],
            mime_type=a["mime_type"],
            byte_size=a["byte_size"],
            sha256=a["sha256"],
            local_ref=a.get("local_ref"),
        )
        for a in (reply["attachments"] or [])
        if isinstance(a, Mapping)
    )
    return ReplySourceContent(
        internet_message_id=reply["internet_message_id"],
        provider_message_id=reply["provider_message_id"],
        from_address=reply["from_address"],
        in_reply_to=reply["in_reply_to"],
        references=tuple(reply["reference_ids"] or ()),
        subject=reply["subject"],
        body_text=reply["sanitized_body"],
        attachments=attachments,
    )


async def _reply_row(conn: Conn, worker: WorkerIdentity, reply_id: UUID) -> Mapping[str, Any]:
    row = await fetch_one(conn, _STORED_REPLY_SQL, {"ws": worker.workspace_id, "id": reply_id})
    assert row is not None  # the dedup row's foreign key guarantees it
    return row


async def _decide(
    conn: Conn,
    worker: WorkerIdentity,
    request: ReplyIngestRequest,
    *,
    key: str,
    dedup_key: str,
    fingerprint: str,
) -> tuple[IngestDecision, Mapping[str, Any] | None, Mapping[str, Any] | None]:
    rows = await fetch_all(
        conn,
        _DEDUP_SQL,
        {"ws": worker.workspace_id, "box": worker.mailbox_binding_id, "key": dedup_key, "idem": key},
    )
    by_key = next((r for r in rows if r["dedup_key"] == dedup_key), None)
    by_idem = next((r for r in rows if r["idempotency_key"] == key), None)
    decision = decide_ingest(
        dedup_key=dedup_key,
        idempotency_key=key,
        fingerprint=fingerprint,
        locator=request.locator(),
        existing_by_dedup_key=None if by_key is None else _stored(by_key),
        existing_by_idempotency_key=None if by_idem is None else _stored(by_idem),
    )
    if decision.kind == IngestDecisionKind.FINGERPRINT_VERSION_MISMATCH:
        # Recompute the stored message's fingerprint with the current algorithm and decide again.
        recomputed = {}
        for row in (by_key, by_idem):
            if row is not None and row["id"] not in recomputed:
                reply = await _reply_row(conn, worker, row["reply_id"])
                recomputed[row["id"]] = _stored(
                    row, source_content_fingerprint(stored_source_content(reply)), FINGERPRINT_VERSION
                )
        decision = decide_ingest(
            dedup_key=dedup_key,
            idempotency_key=key,
            fingerprint=fingerprint,
            locator=request.locator(),
            existing_by_dedup_key=None if by_key is None else recomputed[by_key["id"]],
            existing_by_idempotency_key=None if by_idem is None else recomputed[by_idem["id"]],
        )
    return decision, by_key, by_idem


# --------------------------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------------------------


async def _ingest_locked(
    conn: Conn,
    worker: WorkerIdentity,
    request: ReplyIngestRequest,
    *,
    key: str,
    now: datetime,
    inquiry: Mapping[str, Any],
    request_id: str,
    options: ReplyIngestOptions,
) -> ReplyIngestOutcome:
    fingerprint = request.fingerprint()
    dedup = request.dedup_key()
    dedup_key = dedup.as_string()
    decision, by_key, by_idem = await _decide(
        conn, worker, request, key=key, dedup_key=dedup_key, fingerprint=fingerprint
    )
    if decision.kind == IngestDecisionKind.DUPLICATE:
        assert decision.reply_id is not None
        original = by_key if by_key is not None else by_idem
        assert original is not None
        if original["inquiry_id"] == request.inquiry_id:
            return await _duplicate(conn, worker, request, original, request_id)
        # The same message claimed for another inquiry: never re-pointed, never overwritten.
        return await _conflict(
            conn,
            worker,
            request,
            target=original,
            fingerprint=fingerprint,
            dedup_kind=dedup.kind,
            now=now,
            request_id=request_id,
        )
    if decision.kind == IngestDecisionKind.CONFLICT:
        target = by_idem if by_idem is not None and by_idem["dedup_key"] != dedup_key else by_key
        assert target is not None
        return await _conflict(
            conn,
            worker,
            request,
            target=target,
            fingerprint=fingerprint,
            dedup_kind=dedup.kind,
            now=now,
            request_id=request_id,
        )
    if request.source_message.internet_message_id is not None:
        # Same message stored for this inquiry through an earlier (rotated) mailbox binding.
        earlier = await fetch_one(
            conn,
            "select id, source_fingerprint, quarantined, ingested_at from app.seller_replies"
            " where workspace_id = %(ws)s and inquiry_id = %(inquiry)s and internet_message_id = %(mid)s"
            " and conflict_of_reply_id is null",
            {
                "ws": worker.workspace_id,
                "inquiry": request.inquiry_id,
                "mid": request.source_message.internet_message_id,
            },
        )
        if earlier is not None:
            if earlier["source_fingerprint"] != fingerprint:
                return _outcome(request, earlier["id"], request_id, earlier, duplicate=False, conflict=True)
            return _outcome(request, earlier["id"], request_id, earlier, duplicate=True)
    return await _new(
        conn,
        worker,
        request,
        key=key,
        dedup_key=dedup_key,
        dedup_kind=dedup.kind,
        fingerprint=fingerprint,
        now=now,
        inquiry=inquiry,
        request_id=request_id,
        options=options,
    )


def _outcome(
    request: ReplyIngestRequest,
    reply_id: UUID,
    request_id: str,
    reply: Mapping[str, Any],
    *,
    duplicate: bool,
    conflict: bool = False,
    **extra: Any,
) -> ReplyIngestOutcome:
    return ReplyIngestOutcome(
        reply_id=reply_id,
        inquiry_id=request.inquiry_id,
        ingest_status="quarantined" if reply["quarantined"] or conflict else "stored",
        duplicate=duplicate,
        request_id=request_id,
        ingested_at=ensure_utc(reply["ingested_at"]),
        conflict=conflict,
        **extra,
    )


async def _record_locator(
    conn: Conn, worker: WorkerIdentity, request: ReplyIngestRequest, reply_id: UUID
) -> None:
    locator = request.locator()
    if locator is None:
        return
    await conn.execute(
        "insert into app.seller_reply_locators (workspace_id, reply_id, mailbox_binding_id,"
        " outlook_entry_id, outlook_store_id, seen_at)"
        " values (%(ws)s, %(reply)s, %(box)s, %(entry)s, %(store)s, clock_timestamp())"
        " on conflict do nothing",
        {
            "ws": worker.workspace_id,
            "reply": reply_id,
            "box": worker.mailbox_binding_id,
            "entry": locator.outlook_entry_id,
            "store": locator.outlook_store_id,
        },
    )


async def _duplicate(
    conn: Conn,
    worker: WorkerIdentity,
    request: ReplyIngestRequest,
    original: Mapping[str, Any],
    request_id: str,
) -> ReplyIngestOutcome:
    await conn.execute(
        "update ops.mail_ingest_dedup set last_seen_at = greatest(last_seen_at, clock_timestamp()),"
        " duplicate_count = duplicate_count + 1 where workspace_id = %(ws)s and id = %(id)s",
        {"ws": worker.workspace_id, "id": original["id"]},
    )
    await _record_locator(conn, worker, request, original["reply_id"])
    reply = await _reply_row(conn, worker, original["reply_id"])
    return _outcome(request, original["reply_id"], request_id, reply, duplicate=True)


async def _validated_binding(
    conn: Conn, worker: WorkerIdentity, request: ReplyIngestRequest
) -> MailWorkerBindingItem:
    requested, latest = await mail_workers_repo.binding_for(
        conn, worker, request.inquiry_id, request.binding_version
    )
    if latest is not None and latest.state == InquiryBindingState.TOMBSTONED:
        raise Forbidden(
            "The inquiry binding was revoked for this mailbox",
            details={"reason": "inquiry_binding_tombstoned"},
        )
    if requested is None or requested.state == InquiryBindingState.TOMBSTONED:
        raise VersionConflict(
            "The binding version was never published to this mailbox", reason="binding_version_unpublished"
        )
    return requested


async def _conflict(
    conn: Conn,
    worker: WorkerIdentity,
    request: ReplyIngestRequest,
    *,
    target: Mapping[str, Any],
    fingerprint: str,
    dedup_kind: str,
    now: datetime,
    request_id: str,
) -> ReplyIngestOutcome:
    """Keep the conflicting upload quarantined beside the original and count the conflict."""
    await _validated_binding(conn, worker, request)
    original_id: UUID = target["reply_id"]
    existing: Mapping[str, Any] | None = await fetch_one(
        conn,
        "select id, quarantined, ingested_at from app.seller_replies where workspace_id = %(ws)s"
        " and conflict_of_reply_id = %(original)s and source_fingerprint = %(fp)s",
        {"ws": worker.workspace_id, "original": original_id, "fp": fingerprint},
    )
    if existing is None:
        mtype = request.message_type
        existing = await _insert_reply(
            conn,
            worker,
            request,
            fingerprint=fingerprint,
            message_type=mtype,
            quarantined=True,
            quarantine_reason="idempotency_conflict",
            correlation_status="quarantined",
            correlation_reasons=("IDEMPOTENCY_CONFLICT",),
            returned_ids=(),
            attachments=_attachment_documents(request.attachments, evaluate_attachments(request.attachments)),
            withheld=request.withheld_sensitive_attachments,
            processed=None,
            conflict_of=original_id,
        )
    await conn.execute(
        "update ops.mail_ingest_dedup set last_seen_at = greatest(last_seen_at, clock_timestamp()),"
        " conflict_count = conflict_count + 1, last_conflict_at = clock_timestamp(),"
        " last_conflict_fingerprint = %(fp)s, last_conflict_reply_id = %(reply)s"
        " where workspace_id = %(ws)s and id = %(id)s",
        {"ws": worker.workspace_id, "id": target["id"], "fp": fingerprint, "reply": existing["id"]},
    )
    await _record_locator(conn, worker, request, existing["id"])
    await audit.record(
        conn,
        worker.actor(request_id),
        "seller_reply.idempotency_conflict",
        "seller_reply",
        existing["id"],
        metadata={
            "inquiry_id": str(request.inquiry_id),
            "conflict_of_reply_id": str(original_id),
            "dedup_kind": dedup_kind,
            "occurred_at": now.isoformat(),
        },
        outcome="denied",
    )
    return _outcome(request, existing["id"], request_id, existing, duplicate=False, conflict=True)


def _message(
    worker: WorkerIdentity, request: ReplyIngestRequest, binding: MailWorkerBindingItem
) -> InboundMessage:
    headers: dict[str, Any] = {"From": request.headers.from_address, "Subject": request.subject}
    if request.headers.in_reply_to is not None:
        headers["In-Reply-To"] = request.headers.in_reply_to
    if request.headers.references:
        headers["References"] = list(request.headers.references)
    assert binding.provider is not None
    return InboundMessage(
        identity=SourceMessageIdentity(
            mailbox_binding_id=worker.mailbox_binding_id,
            provider=binding.provider,
            internet_message_id=request.source_message.internet_message_id,
            provider_message_id=request.source_message.provider_message_id,
            received_at=request.source_message.received_at,
        ),
        headers=headers,
        body_text=request.sanitized_body_text,
        attachments=request.attachments,
    )


def _message_type(request: ReplyIngestRequest, message: InboundMessage) -> ReplyMessageType:
    """The worker's type, except that it never upgrades the server's view to ``seller_reply``."""
    server = classify_message(message.headers, message.body_text, attachments=message.attachments)
    if request.message_type in _UPGRADE_TYPES and server != ReplyMessageType.SELLER_REPLY:
        return server
    return request.message_type


def _quarantine_reason(correlation: CorrelationResult, mtype: ReplyMessageType) -> str | None:
    if mtype == ReplyMessageType.SPAM:
        return "spam_message"
    if mtype == ReplyMessageType.AMBIGUOUS:
        return "ambiguous_message"
    if correlation.outcome == CorrelationOutcome.MATCHED:
        return None
    for reason, label in _QUARANTINE_REASONS:
        if reason in correlation.reasons:
            return label
    return "possible_match_unverified"


def _attachment_documents(
    attachments: Sequence[AttachmentMeta], decisions: Sequence[AttachmentDecision]
) -> list[dict[str, Any]]:
    """Safe metadata of the uploaded attachments with the server's policy decision. A file the
    server judges sensitive is withheld here too (counted, never described)."""
    by_index = {d.index: d for d in decisions}
    documents: list[dict[str, Any]] = []
    for index, meta in enumerate(attachments):
        decision = by_index.get(index)
        if decision is not None and decision.action == AttachmentAction.QUARANTINE_SENSITIVE:
            continue
        document: dict[str, Any] = {
            "filename": meta.filename,
            "mime_type": meta.mime_type,
            "byte_size": meta.byte_size,
            "sha256": meta.sha256,
        }
        if meta.local_ref is not None:
            document["local_ref"] = meta.local_ref
        if decision is not None:
            document["action"] = decision.action.value
            if decision.document_kind is not None:
                document["document_kind"] = decision.document_kind.value
            document["reasons"] = list(decision.reasons[:20])
        documents.append(document)
    return documents


def _claims_document(claims: ReplyClaims) -> dict[str, Any]:
    bounded = claims.model_copy(
        update={
            name: getattr(claims, name)[:MAX_CLAIM_ITEMS]
            for name in ("availability", "prices", "other_amounts", "documents", "requests")
        }
    )
    document: dict[str, Any] = bounded.model_dump(mode="json")
    if len(canonical_json(document).encode("utf-8")) > MAX_CLAIMS_BYTES:
        document = ReplyClaims(language=claims.language, warnings=("CLAIMS_TRUNCATED",)).model_dump(
            mode="json"
        )
    return document


def _bounded_text(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


@dataclass(frozen=True, slots=True)
class _Processed:
    claims: dict[str, Any]
    summary: str


_INSERT_REPLY_SQL: Final = """
insert into app.seller_replies (workspace_id, inquiry_id, mailbox_binding_id, binding_version,
  internet_message_id, provider_message_id, from_address, in_reply_to, reference_ids, returned_message_ids,
  subject, sanitized_body, body_sanitizer_version, source_fingerprint, fingerprint_version, received_at,
  observed_at, message_type, correlation_status, correlation_reasons, detected_language, attachments,
  withheld_sensitive_attachments, mk_summary, mk_summary_version, mk_summary_generated_at, claims,
  claims_version, processing_state, processed_at, quarantined, quarantine_reason, conflict_of_reply_id)
values (%(ws)s, %(inquiry)s, %(box)s, %(version)s, %(mid)s, %(pmid)s, %(from)s, %(irt)s, %(refs)s,
  %(returned)s, %(subject)s, %(body)s, %(sanitizer)s, %(fp)s, %(fp_version)s, %(received)s, %(observed)s,
  %(type)s, %(status)s, %(reasons)s, %(language)s, %(attachments)s, %(withheld)s, %(summary)s,
  %(summary_version)s, case when %(summary)s::text is null then null else clock_timestamp() end,
  %(claims)s, %(claims_version)s, %(processing)s,
  case when %(processing)s = 'stored' then null else clock_timestamp() end,
  %(quarantined)s, %(quarantine)s, %(conflict_of)s)
returning id, quarantined, ingested_at, header_linked, thread_linked
"""


async def _insert_reply(
    conn: Conn,
    worker: WorkerIdentity,
    request: ReplyIngestRequest,
    *,
    fingerprint: str,
    message_type: ReplyMessageType,
    quarantined: bool,
    quarantine_reason: str | None,
    correlation_status: str,
    correlation_reasons: Sequence[str],
    returned_ids: Sequence[str],
    attachments: list[dict[str, Any]],
    withheld: int,
    processed: _Processed | None,
    conflict_of: UUID | None = None,
) -> Mapping[str, Any]:
    source = request.source_message
    row = await fetch_one(
        conn,
        _INSERT_REPLY_SQL,
        {
            "ws": worker.workspace_id,
            "inquiry": request.inquiry_id,
            "box": worker.mailbox_binding_id,
            "version": request.binding_version,
            "mid": source.internet_message_id,
            "pmid": source.provider_message_id,
            "from": request.headers.from_address,
            "irt": request.headers.in_reply_to,
            "refs": list(request.headers.references),
            "returned": list(returned_ids)[:20],
            "subject": request.subject,
            "body": request.sanitized_body_text,
            "sanitizer": SANITIZER_VERSION,
            "fp": fingerprint,
            "fp_version": FINGERPRINT_VERSION,
            "received": source.received_at,
            "observed": request.observed_at,
            "type": message_type.value,
            "status": correlation_status,
            "reasons": [r[:80] for r in correlation_reasons][:30],
            "language": None if request.detected_language is None else request.detected_language.value,
            "attachments": Jsonb(attachments[:20]),
            "withheld": min(withheld, 200),
            "summary": None if processed is None else processed.summary,
            "summary_version": None if processed is None else MK_SUMMARY_VERSION,
            "claims": Jsonb({} if processed is None else processed.claims),
            "claims_version": None
            if processed is None
            else str(processed.claims.get("version") or "")[:80] or None,
            "processing": "stored" if processed is None else "processed",
            "quarantined": quarantined,
            "quarantine": quarantine_reason,
            "conflict_of": conflict_of,
        },
    )
    assert row is not None
    return row


async def _new(
    conn: Conn,
    worker: WorkerIdentity,
    request: ReplyIngestRequest,
    *,
    key: str,
    dedup_key: str,
    dedup_kind: str,
    fingerprint: str,
    now: datetime,
    inquiry: Mapping[str, Any],
    request_id: str,
    options: ReplyIngestOptions,
) -> ReplyIngestOutcome:
    binding_item = await _validated_binding(conn, worker, request)
    binding = mail_workers_repo.domain_binding(binding_item)
    others = [
        b
        for b in await mail_workers_repo.latest_mailbox_bindings(conn, worker)
        if b.inquiry_id != request.inquiry_id
    ]
    message = _message(worker, request, binding_item)
    mtype = _message_type(request, message)
    own = [
        a
        for a in (worker.account_address, inquiry["sender_from_address"], inquiry["sender_reply_to_address"])
        if a
    ]
    correlation = correlate_reply(
        message,
        [binding, *others],
        message_type=mtype,
        all_bindings_for_reference_check=[binding, *others],
        own_addresses=own,
    )
    for_this = correlation.inquiry_id == request.inquiry_id or (
        correlation.inquiry_id is None and request.inquiry_id in correlation.candidate_inquiry_ids
    )
    dsn_unverified = False
    if correlation.outcome == CorrelationOutcome.UNMATCHED or not for_this:
        if mtype in _DSN_TYPES and correlation.outcome == CorrelationOutcome.UNMATCHED:
            dsn_unverified = True
        else:
            raise ValidationFailed(
                "The message is not correlated to this inquiry; it stays in the local mailbox",
                details={"reason": "reply_not_correlated"},
            )
    worker_quarantined = request.correlation_status == "quarantined"
    quarantined = (
        dsn_unverified
        or worker_quarantined
        or correlation.outcome != CorrelationOutcome.MATCHED
        or mtype in _ALWAYS_QUARANTINED
    )
    reason = (
        "dsn_link_unverified"
        if dsn_unverified
        else _quarantine_reason(correlation, mtype) or ("worker_quarantined" if worker_quarantined else None)
    )
    if not quarantined:
        reason = None
    elif reason is None:
        reason = "possible_match_unverified"
    reasons = [r.value for r in correlation.reasons]
    if worker_quarantined:
        reasons.append("WORKER_QUARANTINED")
    body = request.sanitized_body_text
    decisions = evaluate_attachments(request.attachments, body_text=body)
    withheld = request.withheld_sensitive_attachments + sum(
        1 for d in decisions if d.action == AttachmentAction.QUARANTINE_SENSITIVE
    )
    dsn = parse_delivery_report(body) if mtype in _DSN_TYPES else None
    claims: ReplyClaims | None = None
    processed: _Processed | None = None
    if mtype in (ReplyMessageType.SELLER_REPLY, ReplyMessageType.AMBIGUOUS):
        claims = extract_reply_claims(
            body,
            request.detected_language,
            quoted_at=request.source_message.received_at,
            attachment_count=len(request.attachments),
        )
        summary = build_mk_summary(claims, request.detected_language, attachments=decisions)
        processed = _Processed(
            claims=_claims_document(claims), summary=_bounded_text(summary.text, MAX_SUMMARY_BYTES)
        )
    reply = await _insert_reply(
        conn,
        worker,
        request,
        fingerprint=fingerprint,
        message_type=mtype,
        quarantined=quarantined,
        quarantine_reason=reason,
        correlation_status="quarantined" if quarantined else "matched",
        correlation_reasons=reasons,
        returned_ids=() if dsn is None else dsn.original_message_ids,
        attachments=_attachment_documents(request.attachments, decisions),
        withheld=withheld,
        processed=processed,
    )
    reply_id: UUID = reply["id"]
    await conn.execute(
        "insert into ops.mail_ingest_dedup (workspace_id, mailbox_binding_id, credential_id, dedup_kind,"
        " dedup_key, idempotency_key, fingerprint, fingerprint_version, inquiry_id, reply_id, ingest_result)"
        " values (%(ws)s, %(box)s, %(credential)s, %(kind)s, %(key)s, %(idem)s, %(fp)s, %(fp_version)s,"
        " %(inquiry)s, %(reply)s, %(result)s)",
        {
            "ws": worker.workspace_id,
            "box": worker.mailbox_binding_id,
            "credential": worker.credential_id,
            "kind": dedup_kind,
            "key": dedup_key,
            "idem": key,
            "fp": fingerprint,
            "fp_version": FINGERPRINT_VERSION,
            "inquiry": request.inquiry_id,
            "reply": reply_id,
            "result": "quarantined" if quarantined else "stored",
        },
    )
    await _record_locator(conn, worker, request, reply_id)
    effects: dict[str, Any] = {}
    if not quarantined:
        decision = decide_reply_processing(correlation, claims, attachments=decisions, bounce=dsn)
        effects = await _apply_effects(
            conn,
            worker,
            request,
            inquiry=inquiry,
            reply_id=reply_id,
            correlation=correlation,
            decision=decision,
            now=now,
            request_id=request_id,
            options=options,
        )
    await audit.record(
        conn,
        worker.actor(request_id),
        "seller_reply.ingest",
        "seller_reply",
        reply_id,
        metadata={
            "inquiry_id": str(request.inquiry_id),
            "message_type": mtype.value,
            "correlation_outcome": correlation.outcome.value,
            "quarantined": quarantined,
            "quarantine_reason": reason,
            "transitions": [s.value for s in effects.get("transitions", ())],
        },
    )
    return _outcome(
        request,
        reply_id,
        request_id,
        reply,
        duplicate=False,
        correlation_outcome=correlation.outcome,
        message_type=mtype,
        quarantine_reason=reason,
        **effects,
    )


# --------------------------------------------------------------------------------------------
# Effects of a matched reply
# --------------------------------------------------------------------------------------------


async def _apply_effects(
    conn: Conn,
    worker: WorkerIdentity,
    request: ReplyIngestRequest,
    *,
    inquiry: Mapping[str, Any],
    reply_id: UUID,
    correlation: CorrelationResult,
    decision: ReplyProcessingDecision,
    now: datetime,
    request_id: str,
    options: ReplyIngestOptions,
) -> dict[str, Any]:
    system = worker.system_actor(request_id)
    current = InquiryState(inquiry["state"])
    strong = bool(_STRONG_LINKS & set(correlation.reasons))
    if current == InquiryState.UNCERTAIN and strong and not decision.resolves_uncertain_send:
        decision = decision.model_copy(update={"resolves_uncertain_send": True})
    path = decision.transition_path(current)
    transitions = await _transition(conn, worker, inquiry, path)
    suppressions = await _suppress(conn, system, inquiry, reply_id, decision)
    result: dict[str, Any] = {"transitions": transitions, "suppression_ids": suppressions}
    received = request.source_message.received_at
    evidence = decision.availability_evidence
    if evidence is not None:
        outcome = await availability_repo.record_seller_statement(
            conn,
            system,
            listing_id=inquiry["qualification_listing_id"],
            reply_id=reply_id,
            status=evidence.availability,
            stated_at=received,
            observed_at=max(now, received),
            vehicle_cluster_id=inquiry["vehicle_cluster_id"],
        )
        if outcome.event is not None:
            result["availability_event_id"] = outcome.event.id
        if outcome.conflict:
            result["availability_conflict"] = True
            extra = await _insert_suppression(
                conn,
                system,
                scope="vehicle",
                key=str(inquiry["vehicle_key"]),
                reason=SuppressionReason.CONTRADICTORY_AVAILABILITY,
                inquiry_id=inquiry["id"],
                reply_id=reply_id,
            )
            result["suppression_ids"] = (*suppressions, extra)
    if decision.invalidate_valuation or decision.queue_recalculation:
        stale, recompute = await _invalidate_valuations(conn, system, inquiry)
        result["stale_valuation_ids"] = stale
        result["recompute_job_ids"] = recompute
    if transitions:
        await mail_workers_repo.publish_inquiry_binding(conn, system, inquiry["id"])
    if decision.emit_signal and options.emit_signal and decision.signal_status is not None:
        draft = build_seller_reply_signal(
            event_id=uuid4(),
            inquiry_id=inquiry["id"],
            reply_id=reply_id,
            listing_id=inquiry["qualification_listing_id"],
            dashboard_base_url=options.dashboard_base_url,
            occurred_at=now,
            status=decision.signal_status,
            vehicle_cluster_id=inquiry["vehicle_cluster_id"],
            is_fixture=bool(inquiry["is_fixture"]),
            is_canary=correlation.is_canary,
        )
        event_id, _created = await outbox.enqueue_event(
            conn,
            system,
            event_type=draft.event_type,
            event_version=1,
            aggregate_type=draft.aggregate_type,
            aggregate_id=reply_id,
            aggregate_version=None,
            payload=draft.payload,
            dedup_key=draft.dedup_key,
            is_fixture=draft.is_fixture,
            event_id=draft.event_id,
        )
        result["outbox_event_id"] = event_id
    if options.enqueue_processing_job:
        job_id, _ = await jobs.enqueue(
            conn,
            system,
            jobs.JobSpec(
                job_type=JobType.SELLER_REPLY_PROCESS,
                dedup_key=f"{REPLY_PROCESS_PREFIX}:{reply_id}",
                payload={"reply_id": str(reply_id), "inquiry_id": str(inquiry["id"])},
                listing_id=inquiry["qualification_listing_id"],
            ),
        )
        result["processing_job_id"] = job_id
    return result


async def _transition(
    conn: Conn, worker: WorkerIdentity, inquiry: Mapping[str, Any], path: Sequence[InquiryState]
) -> tuple[InquiryState, ...]:
    """Walk the legal reply-driven steps; ``uncertain -> accepted`` records the attempt's
    reconciliation (a Message-ID-linked inbound message is positive evidence of submission)."""
    state = InquiryState(inquiry["state"])
    done: list[InquiryState] = []
    for step in path:
        if state == InquiryState.UNCERTAIN and step == InquiryState.ACCEPTED:
            evidence = ReconciliationEvidence(correlated_inbound=True).model_dump(mode="json")
            await conn.execute(
                "update ops.email_delivery_attempts set reconciled_outcome = 'accepted',"
                " reconciled_at = clock_timestamp(), reconciliation_evidence = %(evidence)s"
                " where workspace_id = %(ws)s and inquiry_id = %(inquiry)s and outcome = 'uncertain'"
                " and reconciled_outcome is null",
                {"ws": worker.workspace_id, "inquiry": inquiry["id"], "evidence": Jsonb(evidence)},
            )
        updated = await fetch_one(
            conn,
            "update app.seller_inquiries set state = %(to)s, row_version = row_version + 1"
            " where workspace_id = %(ws)s and id = %(id)s and state = %(from)s returning state",
            {"ws": worker.workspace_id, "id": inquiry["id"], "to": step.value, "from": state.value},
        )
        if updated is None:  # pragma: no cover - the inquiry row is locked by this transaction
            break
        state = step
        done.append(step)
    return tuple(done)


async def _insert_suppression(
    conn: Conn,
    actor: ActorContext,
    *,
    scope: str,
    key: str,
    reason: SuppressionReason,
    inquiry_id: UUID,
    reply_id: UUID,
) -> UUID:
    params = {
        "ws": actor.workspace_id,
        "scope": scope,
        "key": key,
        "reason": reason.value,
        "evidence": Jsonb(
            {"source": "seller_reply", "reply_id": str(reply_id), "inquiry_id": str(inquiry_id)}
        ),
        "inquiry": inquiry_id,
        "reply": reply_id,
    }
    row = await fetch_one(
        conn,
        "insert into ops.email_suppressions (workspace_id, scope, scope_key, reason, evidence, inquiry_id,"
        " reply_id, created_by_kind) values (%(ws)s, %(scope)s, %(key)s, %(reason)s, %(evidence)s,"
        " %(inquiry)s, %(reply)s, 'system')"
        " on conflict (workspace_id, scope, match_key, reason) where removed_at is null do nothing"
        " returning id",
        params,
    )
    if row is None:
        row = await fetch_one(
            conn,
            "select id from ops.email_suppressions where workspace_id = %(ws)s and scope = %(scope)s"
            " and match_key = (case when %(scope)s = 'address' then lower(%(key)s) else %(key)s end)"
            " and reason = %(reason)s and removed_at is null",
            params,
        )
    assert row is not None
    result: UUID = row["id"]
    return result


async def _suppress(
    conn: Conn,
    actor: ActorContext,
    inquiry: Mapping[str, Any],
    reply_id: UUID,
    decision: ReplyProcessingDecision,
) -> tuple[UUID, ...]:
    """Hard bounce -> the address; opt-out/complaint -> the seller (and its merged family through
    the guard) plus the address. Removal is never automatic; no acknowledgement is ever sent."""
    ids: list[UUID] = []
    address = inquiry["recipient_address"]
    for reason in decision.suppressions:
        targets: list[tuple[str, str]] = []
        if reason in (SuppressionReason.SELLER_OPT_OUT, SuppressionReason.COMPLAINT):
            targets.append(("seller", f"seller_entity:{inquiry['seller_entity_id']}"))
        if address:
            targets.append(("address", str(address)))
        for scope, key in targets:
            ids.append(
                await _insert_suppression(
                    conn,
                    actor,
                    scope=scope,
                    key=key,
                    reason=reason,
                    inquiry_id=inquiry["id"],
                    reply_id=reply_id,
                )
            )
    return tuple(dict.fromkeys(ids))


async def _invalidate_valuations(
    conn: Conn, actor: ActorContext, inquiry: Mapping[str, Any]
) -> tuple[tuple[UUID, ...], tuple[UUID, ...]]:
    """Mark the vehicle's open valuations stale (seller evidence changed) and queue recomputation;
    a listing without an open valuation still gets its deduplicated recomputation job."""
    listings = [inquiry["qualification_listing_id"]]
    if inquiry["vehicle_cluster_id"] is not None:
        members = await fetch_all(
            conn,
            "select listing_id from app.vehicle_cluster_members where workspace_id = %(ws)s"
            " and cluster_id = %(cluster)s and unlinked_at is null order by listing_id",
            {"ws": actor.workspace_id, "cluster": inquiry["vehicle_cluster_id"]},
        )
        listings.extend(m["listing_id"] for m in members if m["listing_id"] not in listings)
    rows = await fetch_all(
        conn,
        "select id, listing_id from app.valuations where workspace_id = %(ws)s"
        " and listing_id = any(%(listings)s::uuid[]) and state = any(%(open)s::text[]) order by id",
        {
            "ws": actor.workspace_id,
            "listings": listings,
            "open": [s.value for s in valuation_repo.OPEN_VALUATION_STATES],
        },
    )
    stale: list[UUID] = []
    jobs_queued: list[UUID] = []
    covered: set[UUID] = set()
    for row in rows:
        change = await valuation_repo.mark_stale(
            conn, actor, row["id"], InvalidationReason.EVIDENCE, detail="seller reply evidence"
        )
        if change.changed:
            stale.append(row["id"])
        if change.recompute_job_id is not None:
            jobs_queued.append(change.recompute_job_id)
        covered.add(row["listing_id"])
    for listing_id in listings:
        if listing_id in covered:
            continue
        job_id, _ = await jobs.enqueue(
            conn,
            actor,
            jobs.JobSpec(
                job_type=JobType.VALUATION,
                dedup_key=f"valuation.recompute:{listing_id}",
                payload={"listing_id": str(listing_id), "stale_valuation_ids": [], "reason": "evidence"},
                listing_id=listing_id,
            ),
        )
        jobs_queued.append(job_id)
    return tuple(stale), tuple(dict.fromkeys(jobs_queued))


def signal_payload_is_minimal(payload: Mapping[str, Any]) -> bool:
    """True when a stored signal payload carries only ids, the safe URL and a fixed status
    (used by tests and the doctor; no body, address, name or price may ever appear)."""
    allowed = {
        "schema_version",
        "event_id",
        "type",
        "event_name",
        "occurred_at",
        "inquiry_id",
        "reply_id",
        "dashboard_url",
        "status",
        "deduplication_key",
        "route",
        "listing_id",
        "vehicle_cluster_id",
        "canary",
        "fixture",
    }
    text = json.dumps(payload, ensure_ascii=True)
    return set(payload) <= allowed and "@" not in text


__all__ = [
    "DEFAULT_DASHBOARD_BASE_URL",
    "DEFAULT_INGEST_OPTIONS",
    "REPLY_PROCESS_PREFIX",
    "IngestStatus",
    "ReplyIngestOptions",
    "ReplyIngestOutcome",
    "ingest",
    "ingest_reply",
    "signal_payload_is_minimal",
    "stored_source_content",
]
