"""Seller inquiry, seller reply, mailbox-worker health and 15-day evaluation reads (spec 37.8-37.10).

Read models are the shared views of ``views.inquiries`` (the exact output of the MCP tools
``seller_inquiries_get`` / ``seller_replies_get`` and the dashboard routes of
``api.schemas.V11_DASHBOARD_ROUTES``):

- `get_inquiry` / `get_reply`: one record of the caller's workspace (``inquiries:read``); a missing
  or foreign id is the same ``NOT_FOUND``. A seller's e-mail address (the verified recipient, a
  reply's From) is shown only to ``config:admin`` holders (`views.inquiries.recipient_address_
  visible`); everyone else sees the domain. The owner's mailbox and the sender account id never
  appear; replies carry the sanitised original, the Macedonian structured summary, claims (a
  price is an unaccepted seller quote), attachment metadata without local references, and the
  vehicle's current valuation status (stale + recalculation pending after a reply).
- `list_inquiries` / `list_replies`: frozen pagination through ``ops.query_snapshots`` (the first
  page stores the ordered membership and the summary projections, bound to principal, workspace,
  query and filters; later pages read the snapshot only, so state changes between pages never
  shuffle or duplicate rows). Filters: one state, ``uncertain_only`` and the dashboard's
  ``attention_only`` (uncertain, held for facts, suppressed, failed, stuck sending); replies of one
  inquiry and ``quarantined_only``. Lists never contain message text or addresses. The first page
  WRITES its snapshot (run inside ``transactions.unit_of_work``); the snapshot layer lets
  ``inquiries:read`` alone page these two queries (``query_snapshots.INQUIRY_QUERY_NAMES``),
  every other snapshot query still needs ``deals:read`` or ``reviews:read``.
- `mail_worker_health_view`: ``persistence.mail_workers_repo.list_mailbox_health`` (heartbeat age,
  sync lag, backlog age, unresolved matching gaps, account check, every coverage gap) for the
  dashboard and ``doctor``; ``monitoring_active`` is never claimed without fresh evidence.
- `evaluation_inputs` / `evaluation_report`: the spec 37.9/37.10 15-day evaluation from stored
  evidence (``domain.evaluation.build_evaluation_report``): crawl runs as scan records, activated
  sources (an enabled, non-fixture acquisition source counts as activated at its creation unless
  the caller passes explicit activations), candidates deduplicated by confirmed vehicle cluster with
  their latest valuation and comparable quality, inquiries, matched seller replies, and documents
  a seller reply resolved (a document claim of ``available``/``attached`` or an allowed vehicle
  document attachment). Zero suitable deals is reported as zero; synthetic records never count.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from suv_deals.api.schemas import InquiryListQuery, ReplyListQuery
from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import (
    Availability,
    Completeness,
    EligibilityState,
    EmailProviderKind,
    InquiryReadiness,
    InquiryState,
    MessageLanguage,
    ReplyMessageType,
    Scope,
    SuppressionReason,
    ValuationState,
)
from suv_deals.domain.evaluation import (
    DocumentResolution,
    EvaluationCandidate,
    EvaluationInquiry,
    EvaluationReply,
    EvaluationReport,
    SourceActivation,
    build_evaluation_report,
)
from suv_deals.domain.lifecycle import ScanRecord, SourceHealth
from suv_deals.domain.listings import sha256_json
from suv_deals.domain.money import Money
from suv_deals.domain.replies import DocumentClaimStatus, ReplyClaims
from suv_deals.errors import NotFound, ValidationFailed
from suv_deals.persistence import mail_workers_repo, query_snapshots
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.mail_workers_repo import MailboxHealth
from suv_deals.persistence.queries._common import (
    CursorSecret,
    QueryResult,
    db_now,
    enum_or,
    enum_or_none,
    rendering,
    require_secret,
    text_list,
    utc_or_none,
)
from suv_deals.views.common import AmountView, ResponseWarning, WarningCode, warning
from suv_deals.views.inquiries import (
    InquiryAuthorizationRef,
    InquiryListView,
    InquiryMessageView,
    InquiryQualificationView,
    InquirySummaryView,
    InquiryTemplateView,
    InquiryTimestamps,
    InquiryVehicleRef,
    InquiryView,
    RecipientView,
    ReplyAttachmentView,
    ReplyClaimsView,
    ReplyListView,
    ReplySenderView,
    ReplySummaryView,
    ReplyView,
    SendAttemptSummary,
    SendAttemptsView,
    SenderRef,
    ValuationStatusView,
    recipient_address_visible,
)

INQUIRIES_QUERY: Final = "seller_inquiries"
REPLIES_QUERY: Final = "seller_replies"
#: Snapshot membership bound (projections must stay under the 4 MiB snapshot limit).
MAX_SNAPSHOT_ROWS: Final = 2000
ATTENTION_STATES: Final = (
    InquiryState.UNCERTAIN,
    InquiryState.HELD_FACTS,
    InquiryState.SUPPRESSED,
    InquiryState.FAILED_DEFINITE,
    InquiryState.SENDING,
)
DEFAULT_EVALUATION_LOOKBACK: Final = timedelta(days=90)
MAX_EVALUATION_ROWS: Final = 20_000

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_OPEN_JOB_STATES: Final = ("queued", "running", "retry_wait")
_RESOLVED_DOCUMENT_STATUSES: Final = frozenset({DocumentClaimStatus.AVAILABLE, DocumentClaimStatus.ATTACHED})
_COMPLETENESS: Final[Mapping[str, Completeness]] = {
    "complete": Completeness.COMPLETE,
    "budget_limited": Completeness.BUDGET_LIMITED,
    "partial": Completeness.PARTIAL,
    "failed": Completeness.FAILED,
    "cancelled": Completeness.FAILED,
    "blocked": Completeness.BLOCKED,
    "running": Completeness.PARTIAL,
}
_COMPARABLE_STATUS: Final[Mapping[str, str]] = {
    "adequate": "adequate",
    "small": "small_sample",
    "insufficient": "insufficient_comparables",
}


def _require_inquiries(actor: ActorContext) -> None:
    actor.require(Scope.INQUIRIES_READ)


# --------------------------------------------------------------------------------------------
# Inquiry detail
# --------------------------------------------------------------------------------------------

_INQUIRY_SELECT: Final = """
select i.id, i.identity_key, i.seller_entity_id, i.vehicle_kind, i.vehicle_cluster_id,
       i.qualification_listing_id, i.qualification_revision_id, i.qualification_revision_number,
       i.qualified_semantic_hash, i.qualified_price_minor, i.qualified_currency, i.qualified_availability,
       i.readiness, i.readiness_reasons, i.readiness_rules_version, i.readiness_evaluated_at,
       i.authorization_id, i.authorization_version, i.authorization_fingerprint, i.template_id,
       i.template_version, i.template_set_version, i.language, i.scope_hash, i.body_hash, i.original_subject,
       i.original_body, i.mk_preview_subject, i.mk_preview_body, i.sender_binding_id,
       i.sender_binding_version,
       i.sender_provider, i.sender_display_name, i.recipient_contact_id, i.recipient_address, i.state,
       i.state_reasons, i.suppression_reason, i.rfc_message_id, i.reserved_at, i.queued_at,
       i.send_attempted_at, i.accepted_at, i.replied_at, i.state_changed_at, i.row_version, i.created_at,
       i.updated_at,
       s.source_key, l.source_listing_id, l.canonical_url, l.is_fixture as listing_fixture,
       c.status as contact_status, c.contact_kind, c.address as contact_address, c.language_code,
       c.language_status as contact_language_status, c.verified_at as contact_verified_at,
       c.listing_reference as contact_reference, c.listing_url as contact_url,
       (select count(*) from app.seller_replies r
         where r.workspace_id = i.workspace_id and r.inquiry_id = i.id
           and r.conflict_of_reply_id is null) as reply_count,
       (select r.id from app.seller_replies r
         where r.workspace_id = i.workspace_id and r.inquiry_id = i.id and r.conflict_of_reply_id is null
         order by r.received_at desc, r.id desc limit 1) as latest_reply_id,
       exists (select 1 from ops.email_delivery_attempts a
                where a.workspace_id = i.workspace_id and a.inquiry_id = i.id
                  and a.submission_uncertain) as attempt_uncertain
  from app.seller_inquiries i
  join app.listings l on l.workspace_id = i.workspace_id and l.id = i.qualification_listing_id
  join app.sources s on s.workspace_id = l.workspace_id and s.id = l.source_id
  left join app.seller_contacts c on c.workspace_id = i.workspace_id and c.id = i.recipient_contact_id
 where i.workspace_id = %(ws)s
"""
_ATTEMPTS_SQL: Final = """
select attempt_number, provider, outcome, send_intent_committed_at, finished_at, reconciled_outcome,
       reconciled_at, submission_uncertain, error_code
  from ops.email_delivery_attempts
 where workspace_id = %(ws)s and inquiry_id = %(id)s
 order by attempt_number
"""


def _vehicle(row: Mapping[str, Any]) -> InquiryVehicleRef:
    reference = row["contact_reference"] or row["source_listing_id"]
    url = row["contact_url"] or row["canonical_url"]
    return InquiryVehicleRef(
        vehicle_kind=row["vehicle_kind"],
        vehicle_cluster_id=row["vehicle_cluster_id"],
        listing_id=row["qualification_listing_id"],
        source_key=row["source_key"],
        listing_reference=None if reference is None else str(reference)[:200],
        listing_url=None if url is None or len(str(url)) > 2048 else str(url),
    )


def _recipient(row: Mapping[str, Any], actor: ActorContext) -> RecipientView:
    address = row["recipient_address"] or row["contact_address"]
    return RecipientView.build(
        address=address,
        show_address=recipient_address_visible(actor.scopes),
        contact_id=row["recipient_contact_id"],
        verification_status=row["contact_status"] or "unknown",
        contact_kind=row["contact_kind"],
        language=row["language_code"],
        language_status=row["contact_language_status"] or "unknown",
        verified_at=utc_or_none(row["contact_verified_at"]),
    )


def _attempts(rows: Sequence[Mapping[str, Any]]) -> SendAttemptsView:
    attempts = tuple(
        SendAttemptSummary(
            attempt_number=r["attempt_number"],
            provider=EmailProviderKind(r["provider"]),
            outcome=r["outcome"],
            send_intent_committed_at=ensure_utc(r["send_intent_committed_at"]),
            finished_at=utc_or_none(r["finished_at"]),
            reconciled_outcome=r["reconciled_outcome"],
            reconciled_at=utc_or_none(r["reconciled_at"]),
            submission_uncertain=bool(r["submission_uncertain"]),
            error_code=r["error_code"],
        )
        for r in rows
    )
    return SendAttemptsView(
        count=len(attempts),
        last_outcome=attempts[-1].outcome if attempts else None,
        uncertain=any(a.submission_uncertain for a in attempts),
        attempts=attempts,
    )


def _inquiry_view(
    row: Mapping[str, Any], attempts: Sequence[Mapping[str, Any]], actor: ActorContext
) -> InquiryView:
    state = InquiryState(row["state"])
    attempts_view = _attempts(attempts)
    message = (
        None
        if row["original_subject"] is None
        else InquiryMessageView(
            original_subject=row["original_subject"],
            original_body=row["original_body"],
            mk_preview_subject=row["mk_preview_subject"],
            mk_preview_body=row["mk_preview_body"],
        )
    )
    return InquiryView(
        inquiry_id=row["id"],
        identity_key=row["identity_key"],
        seller_entity_id=row["seller_entity_id"],
        vehicle=_vehicle(row),
        state=state,
        state_reasons=tuple(row["state_reasons"] or ()),
        qualification=InquiryQualificationView(
            listing_revision_id=row["qualification_revision_id"],
            revision_number=row["qualification_revision_number"],
            semantic_hash=row["qualified_semantic_hash"],
            asking_price=AmountView.from_minor(
                row["qualified_price_minor"],
                None if row["qualified_currency"] is None else str(row["qualified_currency"]),
                unknown_reason="no qualified asking price recorded",
            ),
            availability=enum_or_none(Availability, row["qualified_availability"]),
            readiness=InquiryReadiness(row["readiness"]),
            readiness_reasons=tuple(row["readiness_reasons"] or ()),
            rules_version=row["readiness_rules_version"],
            evaluated_at=utc_or_none(row["readiness_evaluated_at"]),
        ),
        authorization=InquiryAuthorizationRef(
            authorization_id=row["authorization_id"],
            version=row["authorization_version"],
            fingerprint=row["authorization_fingerprint"],
        ),
        template=InquiryTemplateView(
            template_id=row["template_id"],
            template_version=row["template_version"],
            template_set_version=row["template_set_version"],
            scope_hash=row["scope_hash"],
            body_hash=row["body_hash"],
        ),
        language=enum_or_none(MessageLanguage, row["language"]),
        recipient=_recipient(row, actor),
        sender=SenderRef(
            sender_binding_id=row["sender_binding_id"],
            binding_version=row["sender_binding_version"],
            provider=enum_or_none(EmailProviderKind, row["sender_provider"]),
            display_name=row["sender_display_name"],
        ),
        rfc_message_id=row["rfc_message_id"],
        send_attempts=attempts_view,
        delivery_uncertain=state == InquiryState.UNCERTAIN or bool(row["attempt_uncertain"]),
        suppression_reason=enum_or_none(SuppressionReason, row["suppression_reason"]),
        message=message,
        reply_count=int(row["reply_count"]),
        latest_reply_id=row["latest_reply_id"],
        timestamps=InquiryTimestamps(
            created_at=ensure_utc(row["created_at"]),
            state_changed_at=ensure_utc(row["state_changed_at"]),
            reserved_at=utc_or_none(row["reserved_at"]),
            queued_at=utc_or_none(row["queued_at"]),
            send_attempted_at=utc_or_none(row["send_attempted_at"]),
            accepted_at=utc_or_none(row["accepted_at"]),
            replied_at=utc_or_none(row["replied_at"]),
            updated_at=ensure_utc(row["updated_at"]),
        ),
        row_version=int(row["row_version"]),
    )


async def get_inquiry(conn: Conn, actor: ActorContext, inquiry_id: Any) -> QueryResult[InquiryView]:
    """``seller_inquiries_get`` / ``GET /api/inquiries/{inquiry_id}``."""
    _require_inquiries(actor)
    params = {"ws": actor.workspace_id, "id": inquiry_id}
    async with mapped_errors():
        row = await fetch_one(conn, _INQUIRY_SELECT + " and i.id = %(id)s", params)
        if row is None:
            raise NotFound("Inquiry not found")
        attempts = await fetch_all(conn, _ATTEMPTS_SQL, params)
    now = await db_now(conn)
    with rendering("seller inquiry"):
        view = _inquiry_view(row, attempts, actor)
    warnings: list[ResponseWarning] = []
    if row["listing_fixture"]:
        warnings.append(warning(WarningCode.FIXTURE_DATA))
    return QueryResult(data=view, as_of=now, warnings=tuple(warnings))


# --------------------------------------------------------------------------------------------
# Reply detail
# --------------------------------------------------------------------------------------------

_REPLY_SELECT: Final = """
select r.id, r.inquiry_id, r.message_type, r.detected_language, r.subject, r.sanitized_body, r.mk_summary,
       r.mk_summary_version, r.mk_summary_generated_at, r.from_address, r.correlation_status,
       r.correlation_reasons, r.header_linked, r.thread_linked, r.received_at, r.observed_at, r.ingested_at,
       r.processing_state, r.processed_at, r.claims, r.quarantined, r.quarantine_reason, r.attachments,
       r.withheld_sensitive_attachments,
       i.seller_entity_id, i.vehicle_kind, i.vehicle_cluster_id, i.qualification_listing_id,
       i.recipient_address, s.source_key, l.source_listing_id, l.canonical_url,
       l.is_fixture as listing_fixture,
       c.listing_reference as contact_reference, c.listing_url as contact_url
  from app.seller_replies r
  join app.seller_inquiries i on i.workspace_id = r.workspace_id and i.id = r.inquiry_id
  join app.listings l on l.workspace_id = i.workspace_id and l.id = i.qualification_listing_id
  join app.sources s on s.workspace_id = l.workspace_id and s.id = l.source_id
  left join app.seller_contacts c on c.workspace_id = i.workspace_id and c.id = i.recipient_contact_id
 where r.workspace_id = %(ws)s
"""
_SELLER_ADDRESS_SQL: Final = """
select exists (
  select 1 from app.seller_contacts c
    join app.seller_entities e on e.workspace_id = c.workspace_id and e.id = c.seller_entity_id
   where c.workspace_id = %(ws)s and c.status = 'verified' and lower(c.address) = lower(%(address)s)
     and (e.id = %(seller)s or e.merged_into_id = %(seller)s)) as verified
"""
_VALUATION_SQL: Final = """
select v.id, v.state, v.stale_reason,
       exists (select 1 from ops.jobs j where j.workspace_id = l.workspace_id and j.listing_id = l.id
                 and j.job_type = 'valuation' and j.state = any(%(open)s::text[])) as pending
  from app.listings l
  left join lateral (
    select v.id, v.state, v.stale_reason from app.valuations v
     where v.workspace_id = l.workspace_id and v.listing_id = l.id
       and v.listing_revision_id = l.current_revision_id
     order by v.created_at desc, v.id desc limit 1) v on true
 where l.workspace_id = %(ws)s and l.id = %(listing)s
"""


def _claims(document: object) -> ReplyClaims | None:
    if not isinstance(document, Mapping) or not document:
        return None
    try:
        return ReplyClaims.model_validate(dict(document))
    except ValidationError:
        return None


def _attachments(document: object) -> tuple[ReplyAttachmentView, ...]:
    if not isinstance(document, list):
        return ()
    views: list[ReplyAttachmentView] = []
    for item in document[:20]:
        if not isinstance(item, Mapping):
            continue
        views.append(
            ReplyAttachmentView(
                filename=item["filename"],
                mime_type=item["mime_type"],
                byte_size=item["byte_size"],
                sha256=item["sha256"],
                action=item.get("action"),
                document_kind=item.get("document_kind"),
            )
        )
    return tuple(views)


def _vehicle_documents(document: object) -> int:
    if not isinstance(document, list):
        return 0
    return sum(
        1 for item in document if isinstance(item, Mapping) and item.get("action") == "allow_vehicle_document"
    )


async def get_reply(conn: Conn, actor: ActorContext, reply_id: Any) -> QueryResult[ReplyView]:
    """``seller_replies_get`` / ``GET /api/replies/{reply_id}``."""
    _require_inquiries(actor)
    params = {"ws": actor.workspace_id, "id": reply_id}
    async with mapped_errors():
        row = await fetch_one(conn, _REPLY_SELECT + " and r.id = %(id)s", params)
        if row is None:
            raise NotFound("Reply not found")
        verified = await fetch_one(
            conn,
            _SELLER_ADDRESS_SQL,
            {"ws": actor.workspace_id, "address": row["from_address"], "seller": row["seller_entity_id"]},
        )
        valuation = await fetch_one(
            conn,
            _VALUATION_SQL,
            {
                "ws": actor.workspace_id,
                "listing": row["qualification_listing_id"],
                "open": list(_OPEN_JOB_STATES),
            },
        )
    now = await db_now(conn)
    recipient = row["recipient_address"]
    matches = (recipient is not None and str(recipient).lower() == str(row["from_address"]).lower()) or bool(
        verified and verified["verified"]
    )
    claims = _claims(row["claims"])
    with rendering("seller reply"):
        view = ReplyView(
            reply_id=row["id"],
            inquiry_id=row["inquiry_id"],
            seller_entity_id=row["seller_entity_id"],
            vehicle=_vehicle(row),
            message_type=ReplyMessageType(row["message_type"]),
            original_language=row["detected_language"],
            subject=row["subject"],
            sanitized_body=row["sanitized_body"],
            mk_summary=row["mk_summary"],
            mk_summary_version=row["mk_summary_version"],
            mk_summary_generated_at=utc_or_none(row["mk_summary_generated_at"]),
            sender=ReplySenderView.build(
                address=row["from_address"],
                show_address=recipient_address_visible(actor.scopes),
                matches_verified_recipient=matches,
                correlation_status=row["correlation_status"],
                correlation_reasons=tuple(row["correlation_reasons"] or ()),
                header_linked=bool(row["header_linked"]),
                thread_linked=bool(row["thread_linked"]),
            ),
            received_at=ensure_utc(row["received_at"]),
            observed_at=ensure_utc(row["observed_at"]),
            ingested_at=ensure_utc(row["ingested_at"]),
            processing_state=row["processing_state"],
            processed_at=utc_or_none(row["processed_at"]),
            claims=None
            if claims is None
            else ReplyClaimsView.from_claims(
                claims, vehicle_document_attachments=_vehicle_documents(row["attachments"])
            ),
            quarantined=bool(row["quarantined"]),
            quarantine_reason=row["quarantine_reason"],
            attachments=_attachments(row["attachments"]),
            withheld_sensitive_attachments=int(row["withheld_sensitive_attachments"]),
            valuation=ValuationStatusView(
                valuation_id=None if valuation is None else valuation["id"],
                state=None if valuation is None else enum_or_none(ValuationState, valuation["state"]),
                stale_reason=None
                if valuation is None or valuation["stale_reason"] is None
                else str(valuation["stale_reason"])[:200],
                recalculation_pending=bool(valuation and valuation["pending"]),
            ),
        )
    warnings: list[ResponseWarning] = []
    if claims is not None:
        warnings.append(warning(WarningCode.SELLER_CLAIMS_UNVERIFIED))
    if view.valuation.state == ValuationState.STALE:
        warnings.append(warning(WarningCode.VALUATION_STALE))
    if row["listing_fixture"]:
        warnings.append(warning(WarningCode.FIXTURE_DATA))
    return QueryResult(data=view, as_of=now, warnings=tuple(warnings))


# --------------------------------------------------------------------------------------------
# Lists (frozen snapshots)
# --------------------------------------------------------------------------------------------

_INQUIRY_LIST_FILTERS: Final = """
   and (%(state)s::text is null or i.state = %(state)s::text)
   and (not %(uncertain)s or i.state = 'uncertain' or exists (
          select 1 from ops.email_delivery_attempts a
           where a.workspace_id = i.workspace_id and a.inquiry_id = i.id and a.submission_uncertain))
   and (not %(attention)s or i.state = any(%(attention_states)s::text[]))
 order by i.state_changed_at desc, i.id desc
 limit %(limit)s
"""
_REPLY_LIST_FILTERS: Final = """
   and (%(inquiry)s::uuid is null or r.inquiry_id = %(inquiry)s::uuid)
   and (not %(quarantined)s or r.quarantined)
 order by r.received_at desc, r.id desc
 limit %(limit)s
"""


def _inquiry_summary(row: Mapping[str, Any]) -> InquirySummaryView:
    state = InquiryState(row["state"])
    return InquirySummaryView(
        inquiry_id=row["id"],
        seller_entity_id=row["seller_entity_id"],
        vehicle=_vehicle(row),
        state=state,
        language=enum_or_none(MessageLanguage, row["language"]),
        recipient_status=row["contact_status"] or "unknown",
        delivery_uncertain=state == InquiryState.UNCERTAIN or bool(row["attempt_uncertain"]),
        suppression_reason=enum_or_none(SuppressionReason, row["suppression_reason"]),
        reply_count=int(row["reply_count"]),
        state_changed_at=ensure_utc(row["state_changed_at"]),
        reserved_at=utc_or_none(row["reserved_at"]),
        send_attempted_at=utc_or_none(row["send_attempted_at"]),
        accepted_at=utc_or_none(row["accepted_at"]),
        row_version=int(row["row_version"]),
    )


def _reply_summary(row: Mapping[str, Any]) -> ReplySummaryView:
    claims = _claims(row["claims"])
    return ReplySummaryView(
        reply_id=row["id"],
        inquiry_id=row["inquiry_id"],
        vehicle=_vehicle(row),
        message_type=ReplyMessageType(row["message_type"]),
        original_language=row["detected_language"],
        availability=None if claims is None else claims.availability_summary,
        quarantined=bool(row["quarantined"]),
        processing_state=row["processing_state"],
        received_at=ensure_utc(row["received_at"]),
        ingested_at=ensure_utc(row["ingested_at"]),
    )


async def _snapshot_page(
    conn: Conn,
    actor: ActorContext,
    *,
    query_name: str,
    filters: Mapping[str, Any],
    cursor: str | None,
    limit: int,
    secret: CursorSecret,
    load: Any,
) -> query_snapshots.CursorPage:
    keys = require_secret(secret)
    filter_hash = sha256_json({"query": query_name, **filters})
    if cursor is not None:
        return await query_snapshots.next_page(
            conn,
            actor,
            cursor=cursor,
            query_name=query_name,
            filter_hash=filter_hash,
            limit=limit,
            secret=keys,
        )
    ids, projections = await load()
    return await query_snapshots.start_listing(
        conn,
        actor,
        query_name=query_name,
        filter_hash=filter_hash,
        ordered_ids=ids,
        projections=projections,
        limit=limit,
        secret=keys,
    )


async def list_inquiries(
    conn: Conn,
    actor: ActorContext,
    query: InquiryListQuery,
    *,
    secret: CursorSecret,
    attention_only: bool = False,
) -> QueryResult[InquiryListView]:
    """``GET /api/inquiries``: one frozen page of inquiry summaries (newest state change first)."""
    _require_inquiries(actor)
    filters = {**query.filters(), "attention_only": attention_only}

    async def load() -> tuple[list[Any], list[dict[str, Any]]]:
        async with mapped_errors():
            rows = await fetch_all(
                conn,
                _INQUIRY_SELECT + _INQUIRY_LIST_FILTERS,
                {
                    "ws": actor.workspace_id,
                    "state": None if query.state is None else query.state.value,
                    "uncertain": query.uncertain_only,
                    "attention": attention_only,
                    "attention_states": [s.value for s in ATTENTION_STATES],
                    "limit": MAX_SNAPSHOT_ROWS,
                },
            )
        with rendering("seller inquiry list"):
            summaries = [_inquiry_summary(r) for r in rows]
        return [s.inquiry_id for s in summaries], [s.model_dump(mode="json") for s in summaries]

    page = await _snapshot_page(
        conn,
        actor,
        query_name=INQUIRIES_QUERY,
        filters=filters,
        cursor=query.cursor,
        limit=query.limit,
        secret=secret,
        load=load,
    )
    now = await db_now(conn)
    with rendering("seller inquiry list"):
        items = tuple(InquirySummaryView.model_validate(p) for p in page.page.projections)
    warnings = [warning(WarningCode.FROZEN_QUEUE_PROJECTION)]
    if page.page.total >= MAX_SNAPSHOT_ROWS:
        warnings.append(warning(WarningCode.PARTIAL_RESULTS))
    return QueryResult(
        data=InquiryListView(items=items), as_of=now, warnings=tuple(warnings), next_cursor=page.next_cursor
    )


async def list_replies(
    conn: Conn, actor: ActorContext, query: ReplyListQuery, *, secret: CursorSecret
) -> QueryResult[ReplyListView]:
    """``GET /api/replies``: one frozen page of reply summaries (newest received first; no bodies)."""
    _require_inquiries(actor)
    filters = query.filters()

    async def load() -> tuple[list[Any], list[dict[str, Any]]]:
        async with mapped_errors():
            rows = await fetch_all(
                conn,
                _REPLY_SELECT + " and r.conflict_of_reply_id is null" + _REPLY_LIST_FILTERS,
                {
                    "ws": actor.workspace_id,
                    "inquiry": query.inquiry_id,
                    "quarantined": query.quarantined_only,
                    "limit": MAX_SNAPSHOT_ROWS,
                },
            )
        with rendering("seller reply list"):
            summaries = [_reply_summary(r) for r in rows]
        return [s.reply_id for s in summaries], [s.model_dump(mode="json") for s in summaries]

    page = await _snapshot_page(
        conn,
        actor,
        query_name=REPLIES_QUERY,
        filters=filters,
        cursor=query.cursor,
        limit=query.limit,
        secret=secret,
        load=load,
    )
    now = await db_now(conn)
    with rendering("seller reply list"):
        items = tuple(ReplySummaryView.model_validate(p) for p in page.page.projections)
    warnings = [warning(WarningCode.FROZEN_QUEUE_PROJECTION)]
    if page.page.total >= MAX_SNAPSHOT_ROWS:
        warnings.append(warning(WarningCode.PARTIAL_RESULTS))
    return QueryResult(
        data=ReplyListView(items=items), as_of=now, warnings=tuple(warnings), next_cursor=page.next_cursor
    )


# --------------------------------------------------------------------------------------------
# Mail-worker health
# --------------------------------------------------------------------------------------------


class MailWorkerHealthView(BaseModel):
    """Every mailbox worker's separate health dimensions (``GET`` dashboard/doctor read)."""

    model_config = _FROZEN

    generated_at: datetime
    mailboxes: tuple[MailboxHealth, ...]
    any_monitoring_active: bool
    open_gap_count: int
    notes: tuple[str, ...]


MAIL_HEALTH_NOTES: Final = (
    "A configured reconciliation interval is context only, never an observed latency guarantee.",
    "Monitoring is reported only while the worker heartbeat, Outlook connection and "
    "reconciliation are fresh.",
)


async def mail_worker_health_view(
    conn: Conn,
    actor: ActorContext,
    *,
    include_revoked: bool = False,
    heartbeat_interval: timedelta = mail_workers_repo.DEFAULT_HEARTBEAT_INTERVAL,
    reconcile_interval: timedelta = mail_workers_repo.DEFAULT_RECONCILE_INTERVAL,
) -> QueryResult[MailWorkerHealthView]:
    """Mailbox-worker health for the dashboard and ``doctor`` (``inquiries:read``)."""
    _require_inquiries(actor)
    mailboxes = await mail_workers_repo.list_mailbox_health(
        conn,
        actor.workspace_id,
        include_revoked=include_revoked,
        heartbeat_interval=heartbeat_interval,
        reconcile_interval=reconcile_interval,
    )
    now = await db_now(conn)
    view = MailWorkerHealthView(
        generated_at=now,
        mailboxes=tuple(mailboxes),
        any_monitoring_active=any(m.monitoring_active for m in mailboxes),
        open_gap_count=sum(m.open_gap_count for m in mailboxes),
        notes=MAIL_HEALTH_NOTES,
    )
    warnings: list[ResponseWarning] = []
    if not mailboxes or any(not m.monitoring_active for m in mailboxes):
        warnings.append(
            warning(WarningCode.COVERAGE_GAP, "The mailbox worker is not demonstrably monitoring.")
        )
    return QueryResult(data=view, as_of=now, warnings=tuple(warnings))


# --------------------------------------------------------------------------------------------
# 15-day evaluation
# --------------------------------------------------------------------------------------------


class EvaluationInputs(BaseModel):
    """Everything the 15-day evaluation reads, built from stored evidence."""

    model_config = _FROZEN

    now: datetime
    activations: tuple[SourceActivation, ...]
    scans: tuple[ScanRecord, ...]
    candidates: tuple[EvaluationCandidate, ...]
    inquiries: tuple[EvaluationInquiry, ...]
    replies: tuple[EvaluationReply, ...]
    document_resolutions: tuple[DocumentResolution, ...]


_SOURCES_SQL: Final = """
select source_key, role, mode, enabled, created_at
  from app.sources where workspace_id = %(ws)s order by source_key
"""
_SCANS_SQL: Final = """
select r.id, s.source_key, s.mode, r.partition_key, r.profile_id, r.started_at, r.finished_at, r.outcome,
       r.gap_reasons, r.access_state
  from ops.crawl_runs r
  join app.sources s on s.workspace_id = r.workspace_id and s.id = r.source_id
 where r.workspace_id = %(ws)s and r.started_at >= %(since)s
 order by r.started_at, r.id
 limit %(limit)s
"""
_CANDIDATES_SQL: Final = """
select l.id, l.first_seen_at, l.eligibility_state, l.is_fixture,
       (select m.cluster_id from app.vehicle_cluster_members m
          join app.vehicle_clusters c on c.workspace_id = m.workspace_id and c.id = m.cluster_id
         where m.workspace_id = l.workspace_id and m.listing_id = l.id and m.unlinked_at is null
           and c.review_status = 'confirmed'
         order by m.created_at desc limit 1) as cluster_id,
       v.state as valuation_state, v.currency, v.conservative_contribution_minor, v.base_contribution_minor,
       v.unknowns, cs.sample_quality
  from app.listings l
  left join lateral (
    select v.state, v.currency, v.conservative_contribution_minor, v.base_contribution_minor, v.unknowns,
           v.comparable_set_id
      from app.valuations v
     where v.workspace_id = l.workspace_id and v.listing_id = l.id
       and v.listing_revision_id = l.current_revision_id
     order by v.created_at desc, v.id desc limit 1) v on true
  left join app.comparable_sets cs
    on cs.workspace_id = l.workspace_id and cs.listing_id = l.id and cs.id = v.comparable_set_id
 where l.workspace_id = %(ws)s and l.eligibility_state is not null and l.first_seen_at >= %(since)s
 order by l.first_seen_at, l.id
 limit %(limit)s
"""
_EVAL_INQUIRIES_SQL: Final = """
select id, state, created_at, vehicle_cluster_id, qualification_listing_id, suppression_reason
  from app.seller_inquiries
 where workspace_id = %(ws)s and created_at >= %(since)s
 order by created_at, id
 limit %(limit)s
"""
_EVAL_REPLIES_SQL: Final = """
select r.id, r.inquiry_id, r.message_type, r.received_at, r.quarantined, r.claims, r.attachments,
       i.qualification_listing_id
  from app.seller_replies r
  join app.seller_inquiries i on i.workspace_id = r.workspace_id and i.id = r.inquiry_id
 where r.workspace_id = %(ws)s and r.conflict_of_reply_id is null and r.received_at >= %(since)s
 order by r.received_at, r.id
 limit %(limit)s
"""


def _scan(row: Mapping[str, Any]) -> ScanRecord:
    blocked = row["outcome"] == "blocked" or row["access_state"] in ("access_blocked", "rate_limited")
    reasons = row["gap_reasons"] if isinstance(row["gap_reasons"], list) else []
    incident = any("parser" in str(r).lower() for r in reasons)
    started = ensure_utc(row["started_at"])
    finished = utc_or_none(row["finished_at"])
    return ScanRecord(
        scan_id=row["id"],
        source_key=row["source_key"],
        partition_key=str(row["partition_key"])[:200],
        started_at=started,
        finished_at=None if finished is None else max(finished, started),
        completeness=_COMPLETENESS.get(row["outcome"], Completeness.PARTIAL),
        filter_fingerprint=f"{row['profile_id'] or 'none'}:{row['partition_key']}"[:128],
        parser_incident=incident,
        health=SourceHealth.BLOCKED if blocked else SourceHealth.HEALTHY,
        is_fixture=row["mode"] == "fixture",
    )


def _candidate(row: Mapping[str, Any], threshold: Money | None) -> EvaluationCandidate | None:
    eligibility = enum_or_none(EligibilityState, row["eligibility_state"])
    if eligibility is None:
        return None
    state = enum_or_none(ValuationState, row["valuation_state"])
    complete = state in (ValuationState.ESTIMATED, ValuationState.QUOTE_SUPPORTED)
    currency = row["currency"]
    conservative = (
        Money.from_minor(row["conservative_contribution_minor"], currency)
        if complete and row["conservative_contribution_minor"] is not None
        else None
    )
    base = (
        Money.from_minor(row["base_contribution_minor"], currency)
        if complete and row["base_contribution_minor"] is not None
        else None
    )
    comparable = _COMPARABLE_STATUS.get(row["sample_quality"]) if row["sample_quality"] else None
    return EvaluationCandidate.model_validate(
        {
            "candidate_id": row["id"],
            "vehicle_cluster_id": row["cluster_id"],
            "first_seen_at": ensure_utc(row["first_seen_at"]),
            "eligibility": eligibility,
            "comparable_status": comparable,
            "valuation_state": state,
            "conservative_contribution": conservative,
            "base_contribution": base,
            "unknowns": text_list(row["unknowns"], limit=100, max_chars=200),
            "approved_contribution_threshold": threshold
            if eligibility == EligibilityState.ELIGIBLE_PRIMARY
            else None,
            "is_fixture": bool(row["is_fixture"]),
        }
    )


def _documents(row: Mapping[str, Any]) -> list[DocumentResolution]:
    if row["quarantined"] or row["message_type"] != ReplyMessageType.SELLER_REPLY.value:
        return []
    received = ensure_utc(row["received_at"])
    kinds: list[str] = []
    claims = _claims(row["claims"])
    if claims is not None:
        kinds.extend(d.kind.value for d in claims.documents if d.status in _RESOLVED_DOCUMENT_STATUSES)
    if isinstance(row["attachments"], list):
        kinds.extend(
            str(a["document_kind"])
            for a in row["attachments"]
            if isinstance(a, Mapping)
            and a.get("action") == "allow_vehicle_document"
            and a.get("document_kind")
        )
    return [
        DocumentResolution(
            candidate_id=row["qualification_listing_id"], document=kind[:60], resolved_at=received
        )
        for kind in dict.fromkeys(kinds)
    ]


async def evaluation_inputs(
    conn: Conn,
    actor: ActorContext,
    *,
    approved_threshold: Money | None = None,
    activations: Sequence[SourceActivation] | None = None,
    lookback: timedelta = DEFAULT_EVALUATION_LOOKBACK,
) -> EvaluationInputs:
    """The 15-day evaluation inputs of the actor's workspace (``deals:read`` and ``inquiries:read``).

    ``approved_threshold`` is the OWNER-APPROVED contribution threshold only (``None`` when the
    proposed EUR 1,500 is not approved: then no candidate can be a "deal found").
    """
    actor.require(Scope.DEALS_READ, Scope.INQUIRIES_READ)
    if not timedelta(days=1) <= lookback <= timedelta(days=365):
        raise ValidationFailed("lookback must be between 1 and 365 days")
    now = await db_now(conn)
    params = {"ws": actor.workspace_id, "since": now - lookback, "limit": MAX_EVALUATION_ROWS}
    async with mapped_errors():
        sources = await fetch_all(conn, _SOURCES_SQL, params)
        scans = await fetch_all(conn, _SCANS_SQL, params)
        candidates = await fetch_all(conn, _CANDIDATES_SQL, params)
        inquiries = await fetch_all(conn, _EVAL_INQUIRIES_SQL, params)
        replies = await fetch_all(conn, _EVAL_REPLIES_SQL, params)
    if activations is None:
        activations = tuple(
            SourceActivation(
                source_key=s["source_key"],
                activated_at=ensure_utc(s["created_at"])
                if s["enabled"] and s["role"] == "acquisition" and s["mode"] != "fixture"
                else None,
                is_fixture=s["mode"] == "fixture",
            )
            for s in sources
        )
    with rendering("evaluation inputs"):
        built = [_candidate(r, approved_threshold) for r in candidates]
        documents = [d for r in replies for d in _documents(r)]
        return EvaluationInputs(
            now=now,
            activations=tuple(activations),
            scans=tuple(_scan(r) for r in scans),
            candidates=tuple(c for c in built if c is not None),
            inquiries=tuple(
                EvaluationInquiry(
                    inquiry_id=r["id"],
                    state=InquiryState(r["state"]),
                    created_at=ensure_utc(r["created_at"]),
                    vehicle_cluster_id=r["vehicle_cluster_id"],
                    candidate_id=r["qualification_listing_id"],
                    suppression_reason=enum_or_none(SuppressionReason, r["suppression_reason"]),
                )
                for r in inquiries
            ),
            replies=tuple(
                EvaluationReply(
                    reply_id=r["id"],
                    inquiry_id=r["inquiry_id"],
                    message_type=enum_or(ReplyMessageType, r["message_type"], ReplyMessageType.AMBIGUOUS),
                    received_at=ensure_utc(r["received_at"]),
                    matched=not r["quarantined"],
                )
                for r in replies
            ),
            document_resolutions=tuple(documents),
        )


async def evaluation_report(
    conn: Conn,
    actor: ActorContext,
    *,
    approved_threshold: Money | None = None,
    activations: Sequence[SourceActivation] | None = None,
    lookback: timedelta = DEFAULT_EVALUATION_LOOKBACK,
) -> QueryResult[EvaluationReport]:
    """The 15-day quality evaluation (zero suitable deals is reported as zero, never invented)."""
    inputs = await evaluation_inputs(
        conn, actor, approved_threshold=approved_threshold, activations=activations, lookback=lookback
    )
    with rendering("evaluation report"):
        report = build_evaluation_report(
            now=inputs.now,
            activations=inputs.activations,
            scans=inputs.scans,
            candidates=inputs.candidates,
            inquiries=inputs.inquiries,
            replies=inputs.replies,
            document_resolutions=inputs.document_resolutions,
        )
    warnings: list[ResponseWarning] = []
    if any(cov.gaps for cov in report.coverage) or report.window_start is None:
        warnings.append(warning(WarningCode.COVERAGE_GAP))
    if approved_threshold is None:
        warnings.append(warning(WarningCode.THRESHOLD_PROPOSED))
    return QueryResult(data=report, as_of=inputs.now, warnings=tuple(warnings))


__all__ = [
    "ATTENTION_STATES",
    "INQUIRIES_QUERY",
    "MAX_SNAPSHOT_ROWS",
    "REPLIES_QUERY",
    "EvaluationInputs",
    "MailWorkerHealthView",
    "evaluation_inputs",
    "evaluation_report",
    "get_inquiry",
    "get_reply",
    "list_inquiries",
    "list_replies",
    "mail_worker_health_view",
]
