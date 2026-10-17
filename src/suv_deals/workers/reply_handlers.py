"""Seller-reply processing worker (spec 37.6, 37.7, 37.10 U9-U11).

``seller_reply_process`` (`handle_seller_reply_process`) runs after
``persistence.replies_repo.ingest_reply`` stored an inquiry-correlated reply together with its
deterministic claims, the Macedonian summary, the availability evidence, the valuation
invalidation (recalculation queued) and the minimal ``seller.reply.received.v1`` signal. It is
enqueued by the ingest (``ReplyIngestOptions.enqueue_processing_job``), by the uncertain-send
reconciliation (a reply stored while the inquiry was still ``sending`` becomes evidence) or by
`enqueue_reply_process_job`. It decides what, if anything, the OWNER must see:

1. **Decisions** (spec 37.7): a payment request, reservation, identity documents, an
   appointment, a seller commitment or acceptance of a quoted price (plus a withheld sensitive
   attachment or contradictory availability statements) is a consequential decision only the
   owner can make: ONE ``seller_reply.owner_alert`` (``decision_needed``) per inquiry and reason
   set (a seller repeating the same request is not re-alerted). Nothing is accepted or answered.
2. **Opportunity** (spec 37.7 "notify Vasko when the evidence supports a good opportunity"):
   once the recalculation queued by the ingest has run, a valuation that may notify
   (``Valuation.can_notify``: non-fixture, estimated or quote-supported, approved threshold and
   production tax rule) raises ``opportunity_supported`` only when it is material against the
   previous opportunity alert of the inquiry (``domain.notifications.evaluate_materiality``).
   While a recalculation of the listing is still pending the job is released (no attempt
   consumed), at most `ReplyRuntimeOptions.recalculation_wait` after the ingest.
3. **State** (spec 37.5): a seller reply stored before the send was reconciled applies its
   step to the accepted inquiry (``replied``, or ``seller_opted_out`` for an opt-out/complaint;
   ``inquiries_repo.mark_replied``).

Routine replies (receipt, translation, recalculation) stay in the dashboard/audit trail: no alert
per e-mail. No outgoing reply, follow-up, offer or acceptance is ever generated here. Fixture
lineage never alerts. Payloads carry ids, reason codes and the authenticated dashboard link only.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

from pydantic import ValidationError

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import (
    Availability,
    InquiryState,
    JobState,
    JobType,
    ReplyMessageType,
    ValuationState,
)
from suv_deals.domain.notifications import (
    SELLER_REPLY_OWNER_ALERT_EVENT_TYPE,
    AlertState,
    MaterialityDecision,
    SellerReplyAlertKind,
    build_seller_reply_owner_alert,
    evaluate_materiality,
)
from suv_deals.domain.replies import (
    EscalationReason,
    ReplyClaims,
    RequestKind,
    dashboard_reply_url,
)
from suv_deals.errors import NotFound, ValidationFailed
from suv_deals.persistence import inquiries_repo, jobs, outbox, valuation_repo
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.replies_repo import REPLY_PROCESS_PREFIX
from suv_deals.persistence.transactions import job_unit_of_work, retry_transient, unit_of_work
from suv_deals.persistence.valuation_repo import StoredValuation
from suv_deals.workers.runtime import Disposition, JobExecution, JobOutcome, RuntimeContext, apply_disposition

logger = logging.getLogger(__name__)

#: Spec 37.7 consequential requests -> the owner-decision reason (``domain.replies``).
DECISION_REASONS: Final[dict[RequestKind, EscalationReason]] = {
    RequestKind.PAYMENT: EscalationReason.PAYMENT_REQUEST,
    RequestKind.RESERVATION: EscalationReason.RESERVATION_REQUEST,
    RequestKind.IDENTITY_DOCUMENT: EscalationReason.IDENTITY_DOCUMENT_REQUEST,
    RequestKind.APPOINTMENT: EscalationReason.APPOINTMENT_REQUEST,
    RequestKind.COMMITMENT: EscalationReason.COMMITMENT_REQUEST,
    RequestKind.PRICE_ACCEPTANCE: EscalationReason.PRICE_ACCEPTANCE_REQUEST,
}
_FIGURE_STATES: Final = frozenset({ValuationState.ESTIMATED, ValuationState.QUOTE_SUPPORTED})


@dataclass(frozen=True, slots=True)
class ReplyRuntimeOptions:
    """Engineering defaults (PROPOSED)."""

    #: Re-check delay while the recalculation queued by the ingest is still pending.
    recalculation_poll: timedelta = timedelta(seconds=60)
    #: Stop waiting for the recalculation this long after the ingest (the decision alert, if any,
    #: was already raised; the dashboard shows the valuation state).
    recalculation_wait: timedelta = timedelta(hours=2)
    priority: int = 15


async def enqueue_reply_process_job(
    conn: Conn,
    actor: ActorContext,
    *,
    reply_id: UUID,
    inquiry_id: UUID,
    listing_id: UUID | None,
    options: ReplyRuntimeOptions | None = None,
) -> UUID | None:
    """The ONE processing job of a reply (the ingest's key ``seller_reply.process:<reply>``),
    whatever an earlier job of that key ended as. ``None``: it exists already."""
    opts = options or ReplyRuntimeOptions()
    key = f"{REPLY_PROCESS_PREFIX}:{reply_id}"
    async with mapped_errors():
        found = await fetch_one(
            conn,
            "select 1 as found from ops.jobs where workspace_id = %(ws)s and job_type = %(type)s"
            " and dedup_key = %(key)s limit 1",
            {"ws": actor.workspace_id, "type": JobType.SELLER_REPLY_PROCESS.value, "key": key},
        )
    if found is not None:
        return None
    job_id, created = await jobs.enqueue(
        conn,
        actor,
        jobs.JobSpec(
            job_type=JobType.SELLER_REPLY_PROCESS,
            dedup_key=key,
            payload={"reply_id": str(reply_id), "inquiry_id": str(inquiry_id)},
            listing_id=listing_id,
            priority=opts.priority,
        ),
    )
    return job_id if created else None


# =============================================================================================
# Pure decisions
# =============================================================================================


def parse_claims(document: Mapping[str, Any] | None) -> ReplyClaims | None:
    """The stored claims document as ``ReplyClaims`` (``None`` when absent or unreadable)."""
    if not document:
        return None
    try:
        return ReplyClaims.model_validate(dict(document))
    except ValidationError:
        return None


def decision_reasons(claims: ReplyClaims | None, *, withheld_sensitive_attachments: int) -> tuple[str, ...]:
    """Typed owner-decision reasons of one reply (empty for a routine reply)."""
    reasons: list[EscalationReason] = []
    if claims is not None:
        reasons.extend(DECISION_REASONS[kind] for kind in claims.escalation_kinds if kind in DECISION_REASONS)
        if claims.availability_summary == "conflicting":
            reasons.append(EscalationReason.CONTRADICTORY_REPLY)
    if withheld_sensitive_attachments > 0:
        reasons.append(EscalationReason.SENSITIVE_ATTACHMENT_WITHHELD)
    return tuple(dict.fromkeys(r.value for r in reasons))


def alert_state(stored: StoredValuation, availability: Availability) -> AlertState:
    """What an opportunity alert is based on (unknown stays unknown, never zero)."""
    valuation = stored.valuation
    payable = valuation.screening.eur_payable
    conservative = valuation.conservative_contribution
    base = valuation.base_contribution
    threshold = valuation.scenarios.threshold.would_meet if valuation.scenarios is not None else None
    return AlertState(
        price_eur=payable.amount if payable is not None and payable.amount > 0 else None,
        eligibility=valuation.screening.eligibility,
        availability=availability,
        tax_rules_valid=valuation.tax.production_ready if valuation.tax is not None else None,
        evidence_valid=valuation.state not in (ValuationState.STALE, ValuationState.INVALID),
        conservative_contribution_eur=conservative.amount
        if conservative is not None and conservative.currency == "EUR"
        else None,
        base_contribution_eur=base.amount if base is not None and base.currency == "EUR" else None,
        contribution_meets_threshold=threshold,
    )


def opportunity_decision(
    current: StoredValuation, previous: StoredValuation | None, availability: Availability
) -> MaterialityDecision | None:
    """``None`` unless the current valuation may notify; otherwise the materiality decision
    against the previously alerted valuation (``FIRST_ALERT`` without one)."""
    if not current.valuation.can_notify or current.valuation.state not in _FIGURE_STATES:
        return None
    before = None if previous is None else alert_state(previous, availability)
    return evaluate_materiality(before, alert_state(current, availability))


# =============================================================================================
# The handler
# =============================================================================================

_REPLY_SQL: Final = """
select r.id, r.inquiry_id, r.quarantined, r.message_type, r.claims, r.withheld_sensitive_attachments,
       r.ingested_at, i.state as inquiry_state, i.qualification_listing_id, l.is_fixture, l.availability,
       clock_timestamp() as now
  from app.seller_replies r
  join app.seller_inquiries i on i.workspace_id = r.workspace_id and i.id = r.inquiry_id
  join app.listings l on l.workspace_id = i.workspace_id and l.id = i.qualification_listing_id
 where r.workspace_id = %(ws)s and r.id = %(reply)s
"""
_PENDING_RECALC_SQL: Final = """
select count(*) as n from ops.jobs
 where workspace_id = %(ws)s and job_type = 'valuation' and listing_id = %(listing)s
   and state in ('queued', 'running', 'retry_wait')
"""
_LOCK_INQUIRY_SQL: Final = """
select 1 as locked from app.seller_inquiries where workspace_id = %(ws)s and id = %(inquiry)s for update
"""
_DECISION_ALERTS_SQL: Final = """
select o.event_id, o.payload ->> 'reply_id' as reply_id, o.payload -> 'reasons' as reasons
  from ops.outbox o
 where o.workspace_id = %(ws)s and o.event_type = %(type)s and o.aggregate_id = %(inquiry)s
   and o.payload ->> 'kind' = 'decision_needed'
 order by o.event_created_at, o.id
"""
_PREVIOUS_OPPORTUNITY_SQL: Final = """
select payload ->> 'valuation_id' as valuation_id from ops.outbox
 where workspace_id = %(ws)s and event_type = %(type)s and aggregate_id = %(inquiry)s
   and payload ->> 'kind' = 'opportunity_supported'
 order by event_created_at desc, id desc limit 1
"""


@dataclass(frozen=True, slots=True)
class _ReplyFacts:
    reply_id: UUID
    inquiry_id: UUID
    listing_id: UUID
    quarantined: bool
    message_type: ReplyMessageType
    claims: ReplyClaims | None
    withheld: int
    ingested_at: datetime
    inquiry_state: InquiryState
    is_fixture: bool
    availability: Availability
    now: datetime


async def _facts(conn: Conn, actor: ActorContext, reply_id: UUID) -> _ReplyFacts | None:
    async with mapped_errors():
        row = await fetch_one(conn, _REPLY_SQL, {"ws": actor.workspace_id, "reply": reply_id})
    if row is None:
        return None
    return _ReplyFacts(
        reply_id=row["id"],
        inquiry_id=row["inquiry_id"],
        listing_id=row["qualification_listing_id"],
        quarantined=bool(row["quarantined"]),
        message_type=ReplyMessageType(row["message_type"]),
        claims=parse_claims(row["claims"]),
        withheld=int(row["withheld_sensitive_attachments"] or 0),
        ingested_at=ensure_utc(row["ingested_at"]),
        inquiry_state=InquiryState(row["inquiry_state"]),
        is_fixture=bool(row["is_fixture"]),
        availability=Availability(row["availability"]),
        now=ensure_utc(row["now"]),
    )


async def _valuations(
    conn: Conn, actor: ActorContext, facts: _ReplyFacts
) -> tuple[int, StoredValuation | None, StoredValuation | None]:
    """Pending recalculations, the current valuation and the previously alerted one."""
    async with mapped_errors():
        pending = await fetch_one(
            conn, _PENDING_RECALC_SQL, {"ws": actor.workspace_id, "listing": facts.listing_id}
        )
        previous_row = await fetch_one(
            conn,
            _PREVIOUS_OPPORTUNITY_SQL,
            {
                "ws": actor.workspace_id,
                "type": SELLER_REPLY_OWNER_ALERT_EVENT_TYPE,
                "inquiry": facts.inquiry_id,
            },
        )
    try:
        current = await valuation_repo.current_valuation(conn, actor, facts.listing_id)
    except ValidationFailed:
        current = None
    previous: StoredValuation | None = None
    if previous_row is not None and previous_row["valuation_id"]:
        try:
            previous = await valuation_repo.load_valuation(conn, actor, UUID(previous_row["valuation_id"]))
        except (NotFound, ValidationFailed, ValueError):
            previous = None
    return int(pending["n"]) if pending is not None else 0, current, previous


async def _decision_alerts(
    conn: Conn, actor: ActorContext, facts: _ReplyFacts
) -> tuple[list[str], frozenset[str]]:
    """``(this reply's decision alerts, reasons already alerted for OTHER replies)`` of the inquiry.

    Called under the inquiry row lock (``FOR UPDATE``), so concurrent processing jobs of replies to
    the same inquiry serialise here and each sees the alerts the other committed.
    """
    async with mapped_errors():
        await fetch_one(conn, _LOCK_INQUIRY_SQL, {"ws": actor.workspace_id, "inquiry": facts.inquiry_id})
        rows = await fetch_all(
            conn,
            _DECISION_ALERTS_SQL,
            {
                "ws": actor.workspace_id,
                "type": SELLER_REPLY_OWNER_ALERT_EVENT_TYPE,
                "inquiry": facts.inquiry_id,
            },
        )
    own: list[str] = []
    alerted: set[str] = set()
    for row in rows:
        if row["reply_id"] == str(facts.reply_id):
            own.append(str(row["event_id"]))
        elif isinstance(row["reasons"], list):
            alerted.update(str(r) for r in row["reasons"])
    return own, frozenset(alerted)


async def _enqueue_alert(
    conn: Conn,
    ctx: RuntimeContext,
    actor: ActorContext,
    facts: _ReplyFacts,
    *,
    kind: SellerReplyAlertKind,
    reasons: Sequence[str],
    valuation_id: UUID | None = None,
) -> tuple[UUID, bool]:
    """Write one owner alert (business dedup key: one per inquiry and reason set / valuation).

    Returns ``(event_id, raised_for_this_reply)``: an alert that exists already is never written
    again; it counts as this reply's own only when an earlier run of this reply's job wrote it
    (the job may be released while the recalculation is pending), not when an earlier reply of
    the same inquiry raised the same request.
    """
    draft = build_seller_reply_owner_alert(
        event_id=uuid4(),
        kind=kind,
        inquiry_id=facts.inquiry_id,
        reply_id=facts.reply_id,
        listing_id=facts.listing_id,
        dashboard_url=dashboard_reply_url(ctx.dashboard_base_url, facts.inquiry_id, facts.reply_id),
        occurred_at=facts.now,
        reasons=reasons,
        valuation_id=valuation_id,
        is_fixture=facts.is_fixture,
    )
    event_id, created = await outbox.enqueue_event(
        conn,
        actor,
        event_type=draft.event_type,
        event_version=draft.event_version,
        aggregate_type=draft.aggregate_type,
        aggregate_id=draft.aggregate_id,
        aggregate_version=None,
        payload=draft.payload,
        dedup_key=draft.dedup_key,
        is_fixture=draft.is_fixture,
        event_id=draft.event_id,
    )
    if created:
        return event_id, True
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select payload ->> 'reply_id' as reply_id from ops.outbox"
            " where workspace_id = %(ws)s and event_id = %(event)s",
            {"ws": actor.workspace_id, "event": event_id},
        )
    return event_id, row is not None and row["reply_id"] == str(facts.reply_id)


async def handle_seller_reply_process(ctx: RuntimeContext, execution: JobExecution) -> JobOutcome:
    """Owner decisions and supported opportunities of one stored reply (see module docstring)."""
    job, actor = execution.job, execution.actor
    options = ReplyRuntimeOptions()
    raw = job.payload.get("reply_id")
    try:
        reply_id = UUID(str(raw))
    except ValueError as exc:
        raise ValidationFailed("a seller reply process job needs its reply") from exc
    async with unit_of_work(ctx.db, actor) as conn:
        facts = await _facts(conn, actor, reply_id)
        valuations = None if facts is None else await _valuations(conn, actor, facts)
    if facts is None or valuations is None:
        return await _finish(ctx, execution, {"outcome": "reply_missing", "reply_id": str(reply_id)})
    if facts.quarantined or facts.message_type != ReplyMessageType.SELLER_REPLY:
        # Quarantined possible matches, bounces, notices, auto-replies: no owner alert, no effect.
        return await _finish(
            ctx,
            execution,
            {"outcome": "no_action", "reply_id": str(reply_id), "message_type": facts.message_type.value},
        )
    pending, current, previous = valuations
    reasons = decision_reasons(facts.claims, withheld_sensitive_attachments=facts.withheld)
    recalculated = current is not None and (pending == 0 or current.created_at >= facts.ingested_at)
    waiting = pending > 0 and not recalculated and facts.now - facts.ingested_at < options.recalculation_wait
    opportunity = None
    if not waiting and current is not None and pending == 0:
        opportunity = opportunity_decision(current, previous, facts.availability)

    async def commit() -> JobOutcome:
        execution.check_lease()
        async with job_unit_of_work(ctx.db, job) as (conn, _locked):
            result: dict[str, Any] = {
                "outcome": "processed",
                "reply_id": str(reply_id),
                "decisions": list(reasons),
            }
            replied = await inquiries_repo.mark_replied(conn, actor, facts.inquiry_id, reply_id=reply_id)
            if replied is not None:
                result["inquiry_state"] = replied.state.value
            alerts: list[str] = []
            if reasons and not facts.is_fixture:
                # Only a consequential request NOT yet alerted for this inquiry raises a decision
                # alert (with all of this reply's reasons, for context). A seller varying the
                # requests across replies therefore gets at most one alert per distinct reason,
                # never one per combination (2^8 - 1) or per e-mail.
                own, alerted = await _decision_alerts(conn, actor, facts)
                if own:
                    alerts.extend(own)  # an earlier run of this job raised it already
                elif set(reasons) - alerted:
                    event_id, ours = await _enqueue_alert(
                        conn, ctx, actor, facts, kind="decision_needed", reasons=reasons
                    )
                    if ours:
                        alerts.append(str(event_id))
            if (
                opportunity is not None
                and opportunity.realert_allowed
                and current is not None
                and not facts.is_fixture
                and (previous is None or previous.id != current.id)
            ):
                event_id, ours = await _enqueue_alert(
                    conn,
                    ctx,
                    actor,
                    facts,
                    kind="opportunity_supported",
                    reasons=[r.value.lower() for r in opportunity.reasons],
                    valuation_id=current.id,
                )
                if ours:
                    alerts.append(str(event_id))
            # Alerts raised for THIS reply (a seller repeating a request is not re-alerted).
            result["owner_alert_event_ids"] = alerts
            if waiting:
                # The decision alert (if any) is committed now; the opportunity check waits for
                # the recalculation the ingest queued (no attempt consumed). The poll is NOT
                # audited (the reason stays on the job): every seller e-mail would otherwise
                # write up to recalculation_wait / recalculation_poll audit rows.
                await jobs.release(
                    conn,
                    job,
                    available_at=options.recalculation_poll,
                    code="RECALCULATION_PENDING",
                    detail="waiting for the valuation recalculation queued by the reply",
                )
                return JobOutcome(state=JobState.QUEUED, code="RECALCULATION_PENDING", details=result)
            if pending > 0 and not recalculated:
                result["recalculation"] = "pending_timeout"
            await apply_disposition(conn, job, Disposition.complete(result))
            return JobOutcome(state=JobState.SUCCEEDED, code="processed", details=result)

    outcome = await retry_transient(commit)
    logger.info(
        "seller reply processed",
        extra={
            "code": outcome.code,
            "decisions": len(reasons),
            "alerts": len(outcome.details.get("owner_alert_event_ids", [])),
        },
    )
    return outcome


async def _finish(ctx: RuntimeContext, execution: JobExecution, result: dict[str, Any]) -> JobOutcome:
    async def commit() -> None:
        async with job_unit_of_work(ctx.db, execution.job) as (conn, _locked):
            await apply_disposition(conn, execution.job, Disposition.complete(result))

    await retry_transient(commit)
    return JobOutcome(state=JobState.SUCCEEDED, code=str(result.get("outcome")), details=result)


__all__ = [
    "DECISION_REASONS",
    "ReplyRuntimeOptions",
    "alert_state",
    "decision_reasons",
    "enqueue_reply_process_job",
    "handle_seller_reply_process",
    "opportunity_decision",
    "parse_claims",
]
