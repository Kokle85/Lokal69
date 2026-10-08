"""Persistence side of the ``outlook_local`` send route (spec 37.5, 37.6; ADR 0002 addendum).

Implements ``integrations.email_providers.outlook_local.OutlookWorkerGateway`` and the three
mail-worker send-intent routes the desktop worker calls (``outlook_bridge/api_client.py``):

``GET  /v1/mail-workers/send-intents``          `list_pending`  (plus ``kill_switch_active``)
``POST /v1/mail-workers/send-intents/{id}/claim``  `claim`        (fresh revalidation before ``.Send``)
``POST /v1/mail-workers/send-intents/{id}/report`` `report`       (``map_outlook_report`` + outcome)

A send intent IS the committed send attempt: ``dispatch_outlook`` commits ``queued -> sending``
plus the running ``ops.email_delivery_attempts`` row (``attempt_id`` == ``intent_id``, lease owner
``outlook_local:<mailbox_binding_id>``, lease token == ``intent_id``, lease expiry == the intent's
``not_after``) in ONE transaction, before anything leaves the backend. The ``OutlookSendIntent`` the
worker pulls is derived deterministically from that row and the inquiry's immutable binding (same
subject/body/addresses, the attempt's Message-ID, MIME built with ``Date`` = the commit time), so
there is no second copy that could drift and no window in which an attempt exists without its
intent (or the reverse). ``intents_for`` therefore returns every intent that ever existed: one per
``outlook_local`` attempt. Nothing new is stored for an intent; the worker's reports are kept
(sanitized: no addresses) in the attempt's ``provider_response`` / ``reconciliation_evidence``.

Claim = revalidation immediately before ``.Send``: the attempt is still running and unexpired, the
kill switch is off and the mode automatic, the bound sender binding version is unchanged and
usable, the standing authorization is the bound unrevoked version, no suppression applies to the
seller's SURVIVING (merged) family, no (possibly) transmitted inquiry of that family concerns the
same or a plausibly same vehicle, the seller cooldown holds, the listing facts are still the
qualification snapshot, the recipient is still the verified contact and the rolling caps (counted
at the latest possible hand-over) still hold. A business refusal is ``proceed: false`` with a
wire ``refusal_reason`` (never an error): the kill switch answers ``kill_switch`` (retryable;
also when the serving process's own ``SELLER_INQUIRY_MODE``/``SELLER_INQUIRY_KILL_SWITCH`` forbid
sending, ``claim(process_gate=...)``), a final refusal ``intent_invalid``; rolling caps, the
seller cooldown and a source pause answer ``not_now`` (the worker waits and claims again). Claims
change no state.

Report = ``map_outlook_report`` then ``inquiries_repo.record_outcome`` (a running attempt is
finalised once: ``sent_items_confirmed`` -> accepted, ``submitted_to_outbox``/``send_call_failed``
-> uncertain, ``refused_before_send`` -> proven pre-submission failure while the lease holds,
``transport_rejected`` -> definite rejection). A report for an attempt that is already finalised
never overwrites it: Sent Items evidence reconciles an uncertain attempt to ``accepted``; a
definitive refusal reconciles it to ``failed_definite`` only through
``reconcile_from_reports`` (every intent of the inquiry definitively refused by its own worker).

Scopes: the worker routes take the authenticated ``WorkerIdentity`` (mailbox-bound, ``mail:ingest``
only); dispatch and the gateway run as the workspace's system principal. No address, body or
token is logged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import EmailProviderKind, InquiryState, Tristate
from suv_deals.domain.inquiries import (
    RateCapPolicy,
    ReconciliationEvidence,
    SendAttemptOutcome,
    SenderBinding,
)
from suv_deals.domain.replies import normalize_message_id
from suv_deals.errors import NotFound, ValidationFailed
from suv_deals.integrations.email_providers.base import ReconcileFoundSent, ReconcileProvenNotSubmitted
from suv_deals.integrations.email_providers.outlook_local import (
    DEFAULT_INTENT_TTL,
    MAX_INTENT_TTL,
    IntentNotStored,
    OutlookAccountReport,
    OutlookHeartbeat,
    OutlookRefusalReason,
    OutlookSendIntent,
    OutlookSendReport,
    build_send_intent,
    map_outlook_report,
    reconcile_from_reports,
)
from suv_deals.integrations.mime_builder import MimeBuildError, build_inquiry_message, mailbox
from suv_deals.persistence import audit, inquiries_repo
from suv_deals.persistence.database import Conn, Database, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.inquiries_repo import (
    AttemptLease,
    AttemptRecord,
    DispatchResult,
    InquiryRecord,
    OutcomeResult,
)
from suv_deals.persistence.mail_workers_repo import (
    HEALTH_ROW_HASH,
    WorkerIdentity,
    mailbox_mismatch,
    require_active_mailbox,
)
from suv_deals.persistence.sellers_repo import SellerContactRecord, get_contact, require_inquiry_writer
from suv_deals.persistence.sender_bindings_repo import get_binding

LEASE_OWNER_PREFIX: Final = "outlook_local:"
MAX_INTENTS_PAGE: Final = 50
_FROZEN = ConfigDict(frozen=True, extra="forbid")

AccountReportReader = Callable[[Conn, ActorContext, UUID], Awaitable[OutlookAccountReport | None]]


def lease_owner_for(mailbox_binding_id: UUID) -> str:
    """The attempt lease owner of an intent handed to a mailbox's desktop worker."""
    return f"{LEASE_OWNER_PREFIX}{mailbox_binding_id}"


class SendIntentBatch(BaseModel):
    """``GET /v1/mail-workers/send-intents`` data (``api.schemas.MailWorkerSendIntentBatch``)."""

    model_config = _FROZEN

    intents: tuple[OutlookSendIntent, ...] = ()
    kill_switch_active: bool
    #: Reaped, never-claimed, unreported intents of the mailbox (listed with ``expired: true`` so
    #: the worker reports them ``intent_expired`` and the inquiry can be reconciled).
    expired: tuple[OutlookSendIntent, ...] = ()


class ClaimResult(BaseModel):
    """The claim answer (``api.schemas.MailWorkerClaimDecision`` / ``wire.ClaimDecision``)."""

    model_config = _FROZEN

    intent_id: UUID
    proceed: bool
    refusal_reason: OutlookRefusalReason | None = None
    #: Internal reason code (audited; not part of the wire answer).
    detail: str | None = None


class ReportResult(BaseModel):
    model_config = _FROZEN

    intent_id: UUID
    applied: bool
    inquiry_state: InquiryState
    attempt_outcome: SendAttemptOutcome
    reconciled: bool = False


class OutlookDispatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    result: DispatchResult
    intent: OutlookSendIntent | None = None


# =============================================================================================
# Deriving the intent from the committed attempt
# =============================================================================================


def derive_intent(
    record: InquiryRecord,
    attempt: AttemptRecord,
    *,
    mailbox_binding_id: UUID,
    contact: SellerContactRecord,
) -> OutlookSendIntent:
    """The exact ``OutlookSendIntent`` of an ``outlook_local`` attempt (deterministic)."""
    message = inquiries_repo.rebuild_message(record, contact)
    if (
        message is None
        or record.sender_from_address is None
        or record.sender_display_name is None
        or record.sender_account_id is None
        or record.recipient_address is None
    ):
        raise ValidationFailed("the inquiry binding cannot be rebuilt into a send intent")
    created = attempt.send_intent_committed_at
    built = build_inquiry_message(
        message,
        inquiry_id=record.id,
        attempt_number=attempt.attempt_number,
        sender=mailbox(record.sender_from_address, record.sender_display_name),
        recipient_address=record.recipient_address,
        reply_to_address=record.sender_reply_to_address,
        date=created,
    )
    if built.rfc_message_id != attempt.rfc_message_id:
        raise ValidationFailed("the attempt Message-ID does not match its rebuilt message")
    return build_send_intent(
        built,
        attempt_id=attempt.attempt_id,
        idempotency_key=f"send-{attempt.attempt_id}",
        mailbox_binding_id=mailbox_binding_id,
        binding=SenderBinding(
            binding_id=attempt.sender_binding_id,
            binding_version=attempt.sender_binding_version,
            provider=EmailProviderKind.OUTLOOK_LOCAL,
            account_id=record.sender_account_id,
            from_address=record.sender_from_address,
            display_name=record.sender_display_name,
            reply_to_address=record.sender_reply_to_address,
        ),
        created_at=created,
        ttl=attempt.lease_expires_at - created,
    )


def _mailbox_of(attempt: AttemptRecord) -> UUID | None:
    if attempt.provider != EmailProviderKind.OUTLOOK_LOCAL:
        return None
    if not attempt.lease_owner.startswith(LEASE_OWNER_PREFIX):
        return None
    try:
        return UUID(attempt.lease_owner.removeprefix(LEASE_OWNER_PREFIX))
    except ValueError:
        return None


async def _intent_of(
    conn: Conn, actor: ActorContext, record: InquiryRecord, attempt: AttemptRecord
) -> OutlookSendIntent | None:
    box = _mailbox_of(attempt)
    if box is None or record.recipient_contact_id is None:
        return None
    contact = await get_contact(conn, actor, record.recipient_contact_id)
    try:
        return derive_intent(record, attempt, mailbox_binding_id=box, contact=contact)
    except (ValidationFailed, MimeBuildError, ValidationError, ValueError):
        return None


# =============================================================================================
# Dispatch (system): commit the attempt == intent
# =============================================================================================


async def dispatch_outlook(
    conn: Conn,
    actor: ActorContext,
    inquiry_id: UUID,
    *,
    mailbox_binding_id: UUID,
    message_approval_required: bool,
    job_id: UUID | None = None,
    ttl: timedelta = DEFAULT_INTENT_TTL,
) -> OutlookDispatch:
    """``inquiries_repo.dispatch`` for the local Outlook route: the committed attempt is the intent.

    Run inside the send job's unit of work. The mailbox must be the active worker binding of the
    inquiry's bound sender. The attempt lease (the intent's validity) is ``ttl`` from the commit.
    """
    require_inquiry_writer(actor)
    if not timedelta(minutes=5) <= ttl <= MAX_INTENT_TTL:
        raise ValidationFailed("intent TTL out of range")
    ws = actor.workspace_id
    async with mapped_errors():
        box = await fetch_one(
            conn,
            "select sender_binding_id, provider, state from ops.mail_worker_bindings"
            " where workspace_id = %(ws)s and id = %(id)s",
            {"ws": ws, "id": mailbox_binding_id},
        )
        inquiry = await fetch_one(
            conn,
            "select sender_binding_id, sender_provider from app.seller_inquiries where workspace_id = %(ws)s"
            " and id = %(id)s",
            {"ws": ws, "id": inquiry_id},
        )
        now_row = await fetch_one(conn, "select now() as tx")
    if box is None or inquiry is None:
        raise NotFound("Mailbox or inquiry not found")
    if (
        box["state"] != "active"
        or box["provider"] != EmailProviderKind.OUTLOOK_LOCAL.value
        or inquiry["sender_provider"] != EmailProviderKind.OUTLOOK_LOCAL.value
        or box["sender_binding_id"] != inquiry["sender_binding_id"]
    ):
        raise ValidationFailed("the inquiry is not bound to this mailbox's outlook_local sender")
    assert now_row is not None
    attempt_id = uuid4()
    lease = AttemptLease(
        owner=lease_owner_for(mailbox_binding_id),
        token=attempt_id,
        expires_at=ensure_utc(now_row["tx"]) + ttl,
    )
    result = await inquiries_repo.dispatch(
        conn,
        actor,
        inquiry_id,
        lease=lease,
        message_approval_required=message_approval_required,
        job_id=job_id,
        attempt_id=attempt_id,
    )
    if result.outcome != "proceed" or result.attempt is None:
        return OutlookDispatch(result=result)
    record = await inquiries_repo.get_inquiry(conn, actor, inquiry_id)
    intent = await _intent_of(conn, actor, record, result.attempt)
    if intent is None:  # pragma: no cover - dispatch built the same message a moment ago
        raise ValidationFailed("the committed attempt cannot be expressed as a send intent")
    return OutlookDispatch(result=result, intent=intent)


# =============================================================================================
# Worker routes
# =============================================================================================

_PENDING_SQL: Final = (
    "select a.attempt_id from ops.email_delivery_attempts a"
    " join app.seller_inquiries i on i.workspace_id = a.workspace_id and i.id = a.inquiry_id"
    " where a.workspace_id = %(ws)s and a.provider = 'outlook_local' and a.outcome = 'running'"
    " and a.lease_owner = %(owner)s and a.sender_binding_id = %(sender)s"
    " and a.lease_expires_at > clock_timestamp() and i.state = 'sending'"
    " order by a.send_intent_committed_at, a.id limit %(limit)s"
)


#: Intents whose lease the reaper expired (``LEASE_EXPIRED``, inquiry ``uncertain``) that no
#: worker ever claimed (no granted ``send_intent.claim`` audit) and that carry no worker report: the
#: worker only calls ``.Send`` after a granted claim, so its ``intent_expired`` report proves
#: non-submission. Only recent ones (``EXPIRED_INTENT_WINDOW``) are offered again.
_EXPIRED_SQL: Final = (
    "select a.attempt_id from ops.email_delivery_attempts a"
    " join app.seller_inquiries i on i.workspace_id = a.workspace_id and i.id = a.inquiry_id"
    " where a.workspace_id = %(ws)s and a.provider = 'outlook_local' and a.outcome = 'uncertain'"
    " and a.error_code = %(code)s and a.lease_owner = %(owner)s and a.sender_binding_id = %(sender)s"
    " and a.lease_expires_at > clock_timestamp() - %(window)s::interval and i.state = 'uncertain'"
    " and not coalesce(a.provider_response, '{}'::jsonb) ? 'worker_report'"
    " and not coalesce(a.reconciliation_evidence, '{}'::jsonb) ? 'worker_report'"
    " and not exists (select 1 from ops.audit_events e"
    "   where e.workspace_id = a.workspace_id and e.target_type = 'seller_inquiry'"
    "     and e.target_id = a.inquiry_id and e.action = 'send_intent.claim'"
    "     and e.metadata ->> 'intent_id' = a.attempt_id::text"
    "     and coalesce(e.metadata ->> 'proceed', 'true') <> 'false')"
    " order by a.lease_expires_at, a.id limit %(limit)s"
)
EXPIRED_INTENT_WINDOW: Final = timedelta(days=7)


async def _kill_switch_active(conn: Conn, workspace_id: UUID) -> bool:
    row = await fetch_one(
        conn,
        "select kill_switch, mode from app.seller_inquiry_controls where workspace_id = %(ws)s",
        {"ws": workspace_id},
    )
    return row is None or bool(row["kill_switch"]) or row["mode"] != "automatic"


async def list_pending(
    conn: Conn, worker: WorkerIdentity, *, request_id: str, limit: int = 10
) -> SendIntentBatch:
    """Pending intents of the worker's own mailbox (never another mailbox's), plus the kill switch.

    While the kill switch is on the intents are still listed so the worker refuses and reports
    them (``refused_before_send``/``kill_switch``); the claim refuses them as well.
    """
    await require_active_mailbox(conn, worker)
    system = worker.system_actor(request_id)
    async with mapped_errors():
        rows = await fetch_all(
            conn,
            _PENDING_SQL,
            {
                "ws": worker.workspace_id,
                "owner": lease_owner_for(worker.mailbox_binding_id),
                "sender": worker.sender_binding_id,
                "limit": max(1, min(limit, MAX_INTENTS_PAGE)),
            },
        )
        kill = await _kill_switch_active(conn, worker.workspace_id)
    intents = await _intents(conn, system, [r["attempt_id"] for r in rows])
    room = max(1, min(limit, MAX_INTENTS_PAGE)) - len(rows)
    expired: list[OutlookSendIntent] = []
    if room > 0:
        async with mapped_errors():
            stale = await fetch_all(
                conn,
                _EXPIRED_SQL,
                {
                    "ws": worker.workspace_id,
                    "owner": lease_owner_for(worker.mailbox_binding_id),
                    "sender": worker.sender_binding_id,
                    "code": inquiries_repo.LEASE_EXPIRED,
                    "window": EXPIRED_INTENT_WINDOW,
                    "limit": room,
                },
            )
        expired = await _intents(conn, system, [r["attempt_id"] for r in stale])
    return SendIntentBatch(intents=tuple(intents), kill_switch_active=kill, expired=tuple(expired))


async def _intents(conn: Conn, system: ActorContext, attempt_ids: Sequence[UUID]) -> list[OutlookSendIntent]:
    intents: list[OutlookSendIntent] = []
    for attempt_id in attempt_ids:
        record, attempt = await _load(conn, system, attempt_id)
        intent = await _intent_of(conn, system, record, attempt)
        if intent is not None:
            intents.append(intent)
    return intents


async def _load(conn: Conn, actor: ActorContext, attempt_id: UUID) -> tuple[InquiryRecord, AttemptRecord]:
    attempt = await inquiries_repo.get_attempt(conn, actor, attempt_id)
    record = await inquiries_repo.get_inquiry(conn, actor, attempt.inquiry_id)
    return record, attempt


async def _worker_attempt(
    conn: Conn, worker: WorkerIdentity, intent_id: UUID, request_id: str
) -> tuple[InquiryRecord, AttemptRecord]:
    """The worker's own intent; anything else (another mailbox, unknown) is the same refusal."""
    system = worker.system_actor(request_id)
    try:
        record, attempt = await _load(conn, system, intent_id)
    except NotFound:
        raise mailbox_mismatch() from None
    if (
        _mailbox_of(attempt) != worker.mailbox_binding_id
        or attempt.sender_binding_id != worker.sender_binding_id
    ):
        raise mailbox_mismatch()
    return record, attempt


#: The claim's facts, read after the controls row is locked. The seller is the bound entity's
#: SURVIVING root: a merge after the dispatch must not hide a suppression recorded for the
#: survivor (e.g. its opt-out) nor a (possibly) transmitted inquiry of the merged family about the
#: same vehicle (``app.seller_inquiry_vehicle_conflict`` evaluated for the root) or within the
#: seller cooldown.
_CLAIM_FACTS_SQL: Final = """
with x as (
  select i.*, coalesce(e.merged_into_id, e.id) as root
    from app.seller_inquiries i
    join app.seller_entities e on e.workspace_id = i.workspace_id and e.id = i.seller_entity_id
   where i.workspace_id = %(ws)s and i.id = %(inquiry)s
)
select l.current_revision_id, l.availability, l.quarantined, l.identity_conflict,
       r.asking_minor, r.currency, s.source_key, s.enabled, s.paused,
       (select c.status from app.seller_contacts c
         where c.workspace_id = l.workspace_id and c.id = x.recipient_contact_id) as contact_status,
       ops.seller_inquiry_active_suppressions(x.workspace_id, x.root, x.recipient_address, x.vehicle_key,
         l.id, x.vehicle_cluster_id, s.source_key, x.sender_binding_id) as hits,
       app.seller_inquiry_vehicle_conflict(
         pg_catalog.jsonb_populate_record(null::app.seller_inquiries,
           pg_catalog.to_jsonb(x) || pg_catalog.jsonb_build_object('seller_entity_id', x.root)),
         'dispatch') as duplicate_of,
       exists (
         select 1 from app.seller_inquiries o
           join app.seller_entities oe on oe.workspace_id = o.workspace_id and oe.id = o.seller_entity_id
          where o.workspace_id = x.workspace_id and o.id <> x.id
            and coalesce(oe.merged_into_id, oe.id) = x.root
            and o.send_attempted_at > pg_catalog.clock_timestamp() - %(cooldown)s::interval) as cooling
  from x
  join app.listings l on l.workspace_id = x.workspace_id and l.id = x.qualification_listing_id
  join app.sources s on s.workspace_id = l.workspace_id and s.id = l.source_id
  left join app.listing_revisions r on r.workspace_id = l.workspace_id and r.id = l.current_revision_id
"""

#: Refusals that lift on their own without an owner action (rolling caps, seller cooldown, source
#: resume) answer ``not_now``: the worker keeps the intent and claims it again later while it is
#: valid (it reports ``intent_expired`` once its validity ends). The kill switch / mode answers
#: ``kill_switch`` (the worker reports a RETRYABLE proven pre-submission failure; the next dispatch
#: preflight decides when). ``intent_invalid`` is final for this message: it never leaves.
_WAIT: Final = OutlookRefusalReason.NOT_NOW


async def _claim_refusal(
    conn: Conn, worker: WorkerIdentity, record: InquiryRecord, attempt: AttemptRecord, request_id: str
) -> tuple[OutlookRefusalReason, str] | None:
    ws = worker.workspace_id
    system = worker.system_actor(request_id)
    async with mapped_errors():
        controls = await fetch_one(
            conn,
            "select kill_switch, mode, max_per_24h, max_per_15d, seller_cooldown"
            " from app.seller_inquiry_controls where workspace_id = %(ws)s for update",
            {"ws": ws},
        )
        now_row = await fetch_one(conn, "select clock_timestamp() as now")
    assert now_row is not None
    now: datetime = ensure_utc(now_row["now"])
    if controls is None or controls["kill_switch"] or controls["mode"] != "automatic":
        return OutlookRefusalReason.KILL_SWITCH, "KILL_SWITCH_ACTIVE"
    if attempt.outcome != SendAttemptOutcome.RUNNING or record.state != InquiryState.SENDING:
        return OutlookRefusalReason.INTENT_INVALID, "INTENT_CLOSED"
    if now >= attempt.lease_expires_at:
        return OutlookRefusalReason.INTENT_EXPIRED, "INTENT_EXPIRED"
    sender = await get_binding(conn, system, attempt.sender_binding_id)
    if sender.version != attempt.sender_binding_version or not sender.usable:
        return OutlookRefusalReason.BINDING_MISMATCH, "SENDER_BINDING_CHANGED"
    authorization = await inquiries_repo.current_authorization(conn, system)
    if (
        authorization is None
        or authorization.version != record.authorization_version
        or authorization.authorization.problems_at(now)
    ):
        return OutlookRefusalReason.INTENT_INVALID, "AUTHORIZATION_CHANGED"
    async with mapped_errors():
        stale = await fetch_one(
            conn,
            _CLAIM_FACTS_SQL,
            {"ws": ws, "inquiry": record.id, "cooldown": controls["seller_cooldown"]},
        )
    assert stale is not None
    if list(stale["hits"] or ()):
        return OutlookRefusalReason.INTENT_INVALID, "SUPPRESSED"
    if stale["duplicate_of"] is not None:
        return OutlookRefusalReason.INTENT_INVALID, "DUPLICATE_INQUIRY"
    if not stale["enabled"] or stale["paused"]:
        return _WAIT, "SOURCE_NOT_ACTIVE"
    if (
        stale["current_revision_id"] != record.qualification_revision_id
        or stale["asking_minor"] != record.qualified_price_minor
        or stale["currency"] != record.qualified_currency
        or record.qualified_availability is None
        or stale["availability"] != record.qualified_availability.value
        or stale["availability"] in ("sold_claimed", "removed", "reserved")
        or stale["quarantined"]
        or stale["identity_conflict"]
    ):
        return OutlookRefusalReason.INTENT_INVALID, "LISTING_CHANGED"
    if stale["contact_status"] != "verified":
        return OutlookRefusalReason.INTENT_INVALID, "RECIPIENT_CHANGED"
    if stale["cooling"]:
        return _WAIT, "SELLER_COOLDOWN"
    caps = await inquiries_repo.rate_cap_decision(
        conn,
        system,
        policy=RateCapPolicy(
            max_per_24h=int(controls["max_per_24h"]),
            max_per_15d=int(controls["max_per_15d"]),
            seller_cooldown=controls["seller_cooldown"],
        ),
        exclude_inquiry_id=record.id,
    )
    if not caps.allowed:
        return _WAIT, "RATE_CAP_REACHED"
    return None


async def claim(
    conn: Conn,
    worker: WorkerIdentity,
    *,
    intent_id: UUID,
    claim_attempt_id: str,
    worker_id: str,
    request_id: str,
    process_gate: str | None = None,
) -> ClaimResult:
    """Fresh server revalidation immediately before ``.Send`` (never replayed from an earlier claim).

    Lock order: controls (``FOR UPDATE``, so a concurrent pause is either seen or waits) ->
    inquiry -> attempt reads. Business refusals are ``proceed: false`` with a refusal reason.

    ``process_gate``: the detail code when the serving process's own settings forbid sending
    (``SELLER_INQUIRY_MODE`` not ``automatic`` or ``SELLER_INQUIRY_KILL_SWITCH`` on). The claim
    is then refused exactly like the database kill switch (``kill_switch``, audited with that
    detail) once the intent is known to be the worker's own (no existence leak).
    """
    await require_active_mailbox(conn, worker)
    record, attempt = await _worker_attempt(conn, worker, intent_id, request_id)
    refusal = (
        (OutlookRefusalReason.KILL_SWITCH, process_gate)
        if process_gate is not None
        else await _claim_refusal(conn, worker, record, attempt, request_id)
    )
    actor = worker.actor(request_id)
    await audit.record(
        conn,
        actor,
        "send_intent.claim",
        "seller_inquiry",
        record.id,
        reason="claim refused" if refusal else "claim granted",
        metadata={
            "intent_id": str(intent_id),
            "proceed": refusal is None,
            "detail": refusal[1] if refusal else None,
            "claim_attempt_id": claim_attempt_id[:64],
            "worker_id": worker_id[:128],
        },
        outcome="denied" if refusal else "succeeded",
    )
    if refusal is None:
        return ClaimResult(intent_id=intent_id, proceed=True)
    return ClaimResult(intent_id=intent_id, proceed=False, refusal_reason=refusal[0], detail=refusal[1])


def report_summary(report: OutlookSendReport, *, account_matches: bool | None) -> dict[str, Any]:
    """A worker report without any address (``account_matches`` instead of the address used)."""
    return {
        "intent_id": str(report.intent_id),
        "mailbox_binding_id": str(report.mailbox_binding_id),
        "worker_id": report.worker_id,
        "state": report.state.value,
        "refusal_reason": report.refusal_reason.value if report.refusal_reason else None,
        "observed_internet_message_id": normalize_message_id(report.observed_internet_message_id),
        "outbox_pending": report.outbox_pending.value,
        "sent_items_present": report.sent_items_present,
        "error_code": report.error_code,
        "reported_at": report.reported_at.isoformat(),
        "sent_at": report.sent_at.isoformat() if report.sent_at else None,
        "account_matches": account_matches,
    }


def _stored_reports(attempts: Sequence[AttemptRecord], inquiry_id: UUID) -> list[OutlookSendReport]:
    """Reports kept with the attempts (finalising report and late reconciliation report)."""
    reports: list[OutlookSendReport] = []
    for attempt in attempts:
        for holder in (attempt.provider_response, attempt.reconciliation_evidence):
            data = (holder or {}).get("worker_report")
            if not isinstance(data, Mapping):
                continue
            try:
                reports.append(
                    OutlookSendReport.model_validate(
                        {
                            "intent_id": data["intent_id"],
                            "inquiry_id": inquiry_id,
                            "mailbox_binding_id": data["mailbox_binding_id"],
                            "worker_id": data["worker_id"],
                            "state": data["state"],
                            "refusal_reason": data.get("refusal_reason"),
                            "observed_internet_message_id": data.get("observed_internet_message_id"),
                            "outbox_pending": data.get("outbox_pending", "unknown"),
                            "sent_items_present": bool(data.get("sent_items_present")),
                            "error_code": data.get("error_code"),
                            "reported_at": data["reported_at"],
                            "sent_at": data.get("sent_at"),
                        }
                    )
                )
            except (KeyError, ValidationError):
                continue
    return reports


async def report(
    conn: Conn, worker: WorkerIdentity, *, report: OutlookSendReport, request_id: str
) -> ReportResult:
    """Record the worker's submission/evidence report for one of its own intents (idempotent).

    ``map_outlook_report`` maps it; a running attempt is finalised by
    ``inquiries_repo.record_outcome``. Whenever the inquiry is (or just became) ``uncertain``:
    Sent Items evidence reconciles it to ``accepted``; a definitive refusal reconciles it to
    ``failed_definite`` only when ``reconcile_from_reports`` proves non-submission for every
    intent of the inquiry (a refusal reported after the intent's validity is first recorded as
    ``uncertain``: a late proof is a reconciliation); everything else (a repeated report
    included) changes nothing.
    """
    await require_active_mailbox(conn, worker)
    worker.require_mailbox(report.mailbox_binding_id)
    record, attempt = await _worker_attempt(conn, worker, report.intent_id, request_id)
    if report.inquiry_id != record.id:
        raise mailbox_mismatch()
    system = worker.system_actor(request_id)
    intent = await _intent_of(conn, system, record, attempt)
    if intent is None:
        raise ValidationFailed("the intent of this report cannot be rebuilt")
    async with mapped_errors():
        now_row = await fetch_one(conn, "select clock_timestamp() as now")
    assert now_row is not None
    outcome = map_outlook_report(intent, report, observed_at=ensure_utc(now_row["now"]))
    used = report.account_smtp_address_used
    matches = None if used is None else used.strip().casefold() == intent.from_address.casefold()
    summary = report_summary(report, account_matches=matches)
    result: OutcomeResult = await inquiries_repo.record_outcome(
        conn,
        system,
        attempt_id=attempt.attempt_id,
        lease_token=attempt.lease_token,
        outcome=outcome,
        report_summary=summary,
    )
    if result.inquiry_state != InquiryState.UNCERTAIN:
        return ReportResult(
            intent_id=report.intent_id,
            applied=result.applied,
            inquiry_state=result.inquiry_state,
            attempt_outcome=result.attempt_outcome,
            reconciled=result.reconciled,
        )
    # Uncertain (finalised before this report, or just now: e.g. the worker's own refusal arrived
    # after the intent's validity, which the attempt guard records as uncertain because "a late
    # proof is a reconciliation"): positive evidence from the stored reports only.
    attempts = await inquiries_repo.list_attempts(conn, system, record.id)
    intents = [i for i in [await _intent_of(conn, system, record, a) for a in attempts] if i is not None]
    reports = [*_stored_reports(attempts, record.id), report]
    decision = reconcile_from_reports(
        record.id,
        [a.rfc_message_id for a in attempts if a.rfc_message_id],
        reports,
        worker_online=False,
        intents=intents,
    )
    evidence: ReconciliationEvidence | None = None
    if isinstance(decision, ReconcileFoundSent):
        evidence = ReconciliationEvidence(sent_items="found")
    elif isinstance(decision, ReconcileProvenNotSubmitted):
        evidence = ReconciliationEvidence(
            sent_items="not_searched",
            outbox_pending=Tristate.NO,
            proven_not_submitted=decision.proof,
            worker_alive=Tristate.NO,
        )
    if evidence is None:
        return ReportResult(
            intent_id=report.intent_id,
            applied=result.applied,
            inquiry_state=result.inquiry_state,
            attempt_outcome=result.attempt_outcome,
        )
    reconciled = await inquiries_repo.reconcile(conn, system, record.id, evidence=evidence, extra=summary)
    return ReportResult(
        intent_id=report.intent_id,
        applied=True,
        inquiry_state=reconciled.inquiry_state,
        attempt_outcome=result.attempt_outcome,
        reconciled=True,
    )


# =============================================================================================
# OutlookWorkerGateway (server-side provider facade)
# =============================================================================================


def _same_intent(a: OutlookSendIntent, b: OutlookSendIntent) -> bool:
    ignored = {"idempotency_key", "created_at", "not_after"}
    return a.model_dump(exclude=ignored) == b.model_dump(exclude=ignored)


class PersistentOutlookGateway:
    """``OutlookWorkerGateway`` over the attempts table (one workspace, one mailbox)."""

    def __init__(
        self,
        db: Database,
        workspace_id: UUID,
        *,
        mailbox_binding_id: UUID,
        account_report_reader: AccountReportReader | None = None,
        request_id: str = "outlook-gateway",
    ) -> None:
        self._db = db
        self._mailbox = mailbox_binding_id
        self._actor = ActorContext.system(workspace_id, request_id=request_id)
        self._account_reader = account_report_reader

    def __repr__(self) -> str:
        return f"PersistentOutlookGateway(mailbox_binding_id={self._mailbox})"

    async def publish_intent(self, intent: OutlookSendIntent) -> None:
        """Confirm that ``intent`` is the committed attempt's intent (``dispatch_outlook``).

        ``IntentNotStored`` only when no running attempt exists for it (the worker can then never
        be offered it); a running attempt whose derived intent differs raises a generic error
        (the caller treats that as an uncertain hand-over, because the worker may see the
        committed intent).
        """
        async with self._db.transaction(self._actor) as conn:
            try:
                record, attempt = await _load(conn, self._actor, intent.intent_id)
            except NotFound:
                raise IntentNotStored() from None
            if (
                attempt.outcome != SendAttemptOutcome.RUNNING
                or _mailbox_of(attempt) != intent.mailbox_binding_id
            ):
                raise IntentNotStored()
            stored = await _intent_of(conn, self._actor, record, attempt)
        if stored is None or not _same_intent(stored, intent):
            raise ValidationFailed("the committed intent differs from the one handed over")

    async def latest_account_report(self, mailbox_binding_id: UUID) -> OutlookAccountReport | None:
        if mailbox_binding_id != self._mailbox or self._account_reader is None:
            return None
        async with self._db.transaction(self._actor) as conn:
            return await self._account_reader(conn, self._actor, mailbox_binding_id)

    async def latest_heartbeat(self, mailbox_binding_id: UUID) -> OutlookHeartbeat | None:
        """The worker-level health row written by ``mail_workers_repo.record_heartbeat``."""
        if mailbox_binding_id != self._mailbox:
            return None
        async with self._db.transaction(self._actor) as conn, mapped_errors():
            row = await fetch_one(
                conn,
                "select c.heartbeat_at, c.outlook_connected, c.mailbox_sync_ok, c.backlog_count"
                " from ops.mail_worker_checkpoints c"
                " join ops.mail_worker_bindings m on m.workspace_id = c.workspace_id"
                "  and m.id = c.mailbox_binding_id and m.state = 'active'"
                " where c.workspace_id = %(ws)s and c.mailbox_binding_id = %(box)s"
                " and c.store_id_hash = %(hash)s and c.folder_id_hash = %(hash)s",
                {"ws": self._actor.workspace_id, "box": mailbox_binding_id, "hash": HEALTH_ROW_HASH},
            )
            pending = await fetch_one(
                conn,
                "select count(*) as n from ops.email_delivery_attempts where workspace_id = %(ws)s"
                " and lease_owner = %(owner)s and outcome = 'running'",
                {"ws": self._actor.workspace_id, "owner": lease_owner_for(mailbox_binding_id)},
            )
        if row is None or row["heartbeat_at"] is None:
            return None
        return OutlookHeartbeat(
            mailbox_binding_id=mailbox_binding_id,
            worker_id=f"mailbox-{mailbox_binding_id.hex[:12]}",
            at=row["heartbeat_at"],
            outlook_running=bool(row["outlook_connected"]),
            mailbox_connected=bool(row["mailbox_sync_ok"]),
            pending_intents=int(pending["n"]) if pending is not None else 0,
        )

    async def reports_for(self, inquiry_id: UUID) -> Sequence[OutlookSendReport]:
        async with self._db.transaction(self._actor) as conn:
            attempts = await inquiries_repo.list_attempts(conn, self._actor, inquiry_id)
        return _stored_reports(attempts, inquiry_id)

    async def intents_for(self, inquiry_id: UUID) -> Sequence[OutlookSendIntent]:
        """Every intent that ever existed for the inquiry (one per ``outlook_local`` attempt)."""
        async with self._db.transaction(self._actor) as conn:
            record = await inquiries_repo.get_inquiry(conn, self._actor, inquiry_id)
            attempts = await inquiries_repo.list_attempts(conn, self._actor, inquiry_id)
            intents = [await _intent_of(conn, self._actor, record, a) for a in attempts]
        return [i for i in intents if i is not None]


__all__ = [
    "LEASE_OWNER_PREFIX",
    "MAX_INTENTS_PAGE",
    "ClaimResult",
    "OutlookDispatch",
    "PersistentOutlookGateway",
    "ReportResult",
    "SendIntentBatch",
    "claim",
    "derive_intent",
    "dispatch_outlook",
    "lease_owner_for",
    "list_pending",
    "report",
    "report_summary",
]
