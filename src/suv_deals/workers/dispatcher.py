"""Outbox dispatcher: gated external delivery of committed events (spec 13, 18, 22, 24, 30).

The internal review queue (pending cases in the dashboard/MCP tools) never depends on this
process. The dispatcher only decides whether a committed outbox event may LEAVE the system and
records what happened. A 2xx is a receipt, never a completed review; ``owner_seen_at`` is never
inferred.

Per workspace (``ops.active_workspace_ids()``, ADR 0001) and per claimed event (one lease at a time,
``FOR UPDATE SKIP LOCKED``; fixture-looking rows are never leased):

1. **Global gates**: the event type must map to a known category (``review.pending`` ->
   ``candidate_discovery``, ``review.shortlisted`` -> ``owner_alert``); ``ALLOW_EXTERNAL_NOTIFICATIONS``
   must be true and `event_bridge.select_activation_route` must select a route without a conflict.
   Otherwise the event is ``blocked`` with a typed code (visible; never silently discarded, never
   retried forever). Fixture events were blocked at enqueue time and are never claimed.
2. **Revalidation immediately before dispatch** (spec 18, one short transaction): the review case
   must still exist in the state and version the event names, the listing's current revision must be
   the case revision, the listing must be available, eligible and not quarantined, the valuation must
   not be stale/invalid/expired, and the source must not have opportunity alerts paused (paused,
   suspected parser drift ``degraded``, parser unhealthy or access blocked;
   `sources_repo.alert_pause_reason`). Fixture lineage is the LISTING's frozen ``is_fixture`` (set at
   ingest), never the source's current mode: an event about a fixture listing is blocked even after
   its source was switched to a real mode. A stale event is cancelled with the reason
   (`outbox.cancel_stale`, audited); an expired valuation is marked stale (which queues its
   recomputation).
3. **Route**: exactly one approved, enabled route for the category (`bindings_repo.selected_route`);
   its provider must be the one selected by the settings, the destination binding must be verified,
   an event bound to an older destination is not re-routed, and approved quiet hours defer delivery.
   Owner-facing opportunity alerts (``review.shortlisted``) are blocked with
   ``OWNER_ALERT_MESSAGE_UNAVAILABLE`` until a provider message builder that meets the spec 22
   content rules exists (the pending-review wording is never reused for them).
4. **Delivery**:

   - ``mcp_events``: the matching verified subscriptions (server-side profile/queue filters, a
     membership/scope recheck that revokes subscribers who lost access) get one delivery row per
     ``(subscription, event)``. Due deliveries are leased in ``next_attempt_at`` order and EACH one
     passes the dispatch guard again immediately before its request (`Dispatcher._delivery_gate`:
     revalidation of the event, the route/binding checks of step 3 and approved quiet hours), so a
     retry or resend from an earlier cycle is never sent to a destination the owner has since
     disabled or re-routed, nor for an event that became stale. They are sent with Standard Webhooks
     signatures (`event_bridge.deliver`): the same ``eventId``/``webhook-id`` on every attempt,
     410/413 terminal for that delivery, retryable statuses back off (``Retry-After`` honoured), a
     timeout after the request was sent is ``uncertain``. ``begin_send`` is committed before the
     first request, so a crash leaves the event ``uncertain`` for the reaper instead of a blind
     resend. While an event's lease is held, ITS due deliveries are claimed directly
     (`subscriptions_repo.claim_due_deliveries(event_ids=...)`), so a backlog of other events'
     retries can neither delay it nor burn its attempts; the end-of-cycle sweep then sends the other
     due deliveries in ``next_attempt_at`` order. A delivery refused by the guard is cancelled with
     the guard's truthful stale/route code (`subscriptions_repo.cancel_delivery`). The outbox row then
     aggregates its deliveries: any receipt -> ``delivered``; otherwise any uncertain -> ``uncertain``;
     otherwise pending retries -> ``retry_wait``; otherwise -> ``dead_letter`` (visible).
   - ``slack`` (only when it is the selected route): `slack.post_review_message`, refused by the
     adapter itself unless every activation gate passes.

Spec v1.1 category signals (section 37.6/37.7, ADR 0002) take their OWN path (`_handle_signal`):
``seller.reply.received`` (category ``seller_reply``) and ``seller_reply.owner_alert`` (category
``owner_alert``) are posted ONLY to the Slack route selected for their category in
`bindings_repo` -- never to native MCP Events (candidate discovery only) and independent of the
candidate activation route. External delivery needs ``ALLOW_EXTERNAL_NOTIFICATIONS`` (and
``SELLER_REPLY_SIGNAL_PROVIDER=slack`` for seller replies) plus an enabled, approved and verified
Slack destination binding; fixture lineage is never delivered. Each event is revalidated right
before the post (`revalidate_signal`: the reply still exists and is not quarantined; an
opportunity alert still cites the current, notifiable valuation). The post is minimal
(`slack.build_seller_reply_post_body` / `build_owner_alert_post_body`: ids, a fixed-vocabulary
status and the authenticated dashboard link; never a body, address, attachment or credential). A
Slack 2xx is a provider receipt, never proof that dot processed the signal; retries and the
uncertain follow-up keep the event id (and its ``SDR-`` reference), and an uncertain post is
looked up in the channel history before any resend, so one reply never activates dot twice.

5. **Uncertain follow-up** (never a blind resend): MCP Events has no lookup API, so the documented
   conservative rule applies (`event_bridge.decide_uncertain_followup`: hold, then at most one resend
   with the SAME event id, then stay visibly uncertain); Slack posts are looked up in the channel
   history (`slack.reconcile_uncertain_post`) and only a ``not_found`` schedules one resend.

Logs carry ``event_id`` and outcome codes; metrics count deliveries per provider and outcome. No callback
URL, secret or payload is ever logged.
"""

from __future__ import annotations

import logging
import os
import random
import signal
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID, uuid4

import anyio
from psycopg import sql
from pydantic import ValidationError

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Availability, EligibilityState, OutboxState, ReviewState, ValuationState
from suv_deals.domain.notifications import (
    FIXTURE_SUMMARY_PREFIX,
    SELLER_REPLY_OWNER_ALERT_EVENT_TYPE,
    QuietHours,
    Urgency,
    evaluate_quiet_hours,
)
from suv_deals.domain.replies import SELLER_REPLY_EVENT_TYPE
from suv_deals.domain.valuation import InvalidationReason
from suv_deals.errors import AppError, NotFound, ValidationFailed
from suv_deals.integrations import event_bridge as eb
from suv_deals.integrations import slack
from suv_deals.integrations.safe_http import SafeHttp, SafeHttpClient
from suv_deals.integrations.secret_box import SecretBox
from suv_deals.observability.logging import log_context
from suv_deals.persistence import (
    bindings_repo,
    listings_repo,
    outbox,
    reviews_repo,
    sources_repo,
    subscriptions_repo,
    valuation_repo,
)
from suv_deals.persistence.bindings_repo import ActivationRouteSelection, DestinationBinding, EventCategory
from suv_deals.persistence.database import Conn, Database, db_now, fetch_all, fetch_one
from suv_deals.persistence.errors_map import LeaseLost, mapped_errors
from suv_deals.persistence.outbox import OUTBOX_COLUMNS, ClaimedEvent, DeliveryOutcome, OutboxRecord
from suv_deals.persistence.subscriptions_repo import ClaimedDelivery, DeliveryRecord, SubscriptionRecord
from suv_deals.persistence.transactions import retry_transient, unit_of_work
from suv_deals.settings import Settings
from suv_deals.workers.runtime import RuntimeContext, active_workspace_ids, build_runtime, system_actor

logger = logging.getLogger(__name__)

PENDING_EVENT: Final = "review.pending"
SHORTLIST_EVENT: Final = "review.shortlisted"
SELLER_REPLY_EVENT: Final = SELLER_REPLY_EVENT_TYPE
SELLER_REPLY_ALERT_EVENT: Final = SELLER_REPLY_OWNER_ALERT_EVENT_TYPE
CATEGORY_BY_EVENT: Final[dict[str, EventCategory]] = {
    PENDING_EVENT: "candidate_discovery",
    SHORTLIST_EVENT: "owner_alert",
    SELLER_REPLY_EVENT: "seller_reply",
    SELLER_REPLY_ALERT_EVENT: "owner_alert",
}
#: Category signals posted only to the Slack route of their category (never MCP Events).
SIGNAL_EVENTS: Final = frozenset({SELLER_REPLY_EVENT, SELLER_REPLY_ALERT_EVENT})
#: Outbox states whose event may still be delivered (deliveries of other events are rechecked).
_DELIVERABLE_EVENT_STATES: Final = frozenset(
    {
        OutboxState.PENDING,
        OutboxState.RETRY_WAIT,
        OutboxState.SENDING,
        OutboxState.DELIVERED,
        OutboxState.UNCERTAIN,
    }
)
_UNAVAILABLE: Final = frozenset({Availability.REMOVED, Availability.SOLD_CLAIMED})
_CLOSED_VALUATIONS: Final = frozenset({ValuationState.STALE, ValuationState.INVALID})
_IN_PROGRESS: Final = frozenset({"pending", "retry_wait", "sending"})
#: Minimum re-queue delay of an event none of whose deliveries was sent in the current cycle.
_IDLE_REQUEUE: Final = timedelta(seconds=30)

# Typed blocker / reason codes (``^[A-Za-z0-9_.:-]{1,80}$``).
UNROUTABLE_EVENT_TYPE: Final = "UNROUTABLE_EVENT_TYPE"
EXTERNAL_DISABLED: Final = "EXTERNAL_NOTIFICATIONS_DISABLED"
BRIDGE_UNAVAILABLE: Final = "EVENT_BRIDGE_UNAVAILABLE"
ROUTE_CONFLICT: Final = "ACTIVATION_ROUTE_CONFLICT"
NO_ACTIVE_ROUTE: Final = "NO_ACTIVE_ROUTE"
ROUTE_PROVIDER_MISMATCH: Final = "ROUTE_PROVIDER_MISMATCH"
ROUTE_CHANGED: Final = "DESTINATION_ROUTE_CHANGED"
DESTINATION_NOT_VERIFIED: Final = "DESTINATION_NOT_VERIFIED"
SECRETS_UNAVAILABLE: Final = "SUBSCRIPTION_SECRETS_UNAVAILABLE"
NO_SUBSCRIPTION: Final = "NO_MATCHING_SUBSCRIPTION"
SLACK_BLOCKED: Final = "SLACK_SEND_BLOCKED"
SOURCE_ALERTS_PAUSED: Final = "SOURCE_ALERTS_PAUSED"
FIXTURE_CASE: Final = "FIXTURE_EVENT"
INVALID_PAYLOAD: Final = "INVALID_EVENT_PAYLOAD"
OWNER_ALERT_UNSUPPORTED: Final = "OWNER_ALERT_MESSAGE_UNAVAILABLE"
SIGNAL_DISABLED: Final = "SELLER_REPLY_SIGNAL_DISABLED"
SIGNAL_ROUTE_NOT_SLACK: Final = "SIGNAL_ROUTE_NOT_SLACK"
#: Refusals that block an event (visible, needs an owner/operator change); every other refusal
#: means the event is stale and is cancelled with that reason.
_BLOCKING_REFUSALS: Final = frozenset(
    {
        SOURCE_ALERTS_PAUSED,
        FIXTURE_CASE,
        UNROUTABLE_EVENT_TYPE,
        INVALID_PAYLOAD,
        NO_ACTIVE_ROUTE,
        ROUTE_PROVIDER_MISMATCH,
        ROUTE_CHANGED,
        DESTINATION_NOT_VERIFIED,
        SECRETS_UNAVAILABLE,
        OWNER_ALERT_UNSUPPORTED,
        SIGNAL_DISABLED,
        SIGNAL_ROUTE_NOT_SLACK,
    }
)


class StaleEvent(Exception):
    """The event no longer describes the current committed state (spec 18 dispatch guard)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class DispatcherOptions:
    """Engineering defaults (not provider-published values)."""

    outbox_lease_seconds: float = 300.0
    events_per_cycle: int = 20
    delivery_lease_seconds: float = 120.0
    #: Deliveries leased at once; their total request deadline must fit the delivery lease.
    deliveries_per_claim: int = 5
    #: Delivery batches per claim loop: the held event's own deliveries (an event with many
    #: subscribers) and the end-of-cycle sweep of other due deliveries. The held event's batches
    #: must fit its lease.
    delivery_batches: int = 4
    delivery_max_attempts: int = 5
    idle_poll_seconds: float = 10.0
    reconcile_limit: int = 50

    def __post_init__(self) -> None:
        batch_seconds = self.deliveries_per_claim * eb.DEFAULT_RETRY_POLICY.request_timeout_s
        if batch_seconds >= self.delivery_lease_seconds:
            raise ValueError("deliveries_per_claim x request timeout must fit the delivery lease")
        if self.delivery_batches < 1:
            raise ValueError("delivery_batches must be at least 1")
        if self.delivery_batches * batch_seconds >= self.outbox_lease_seconds:
            raise ValueError("delivery_batches x batch deadline must fit the event lease")


@dataclass(frozen=True, slots=True)
class EventResult:
    event_id: UUID
    event_type: str
    state: OutboxState | None  # None: the lease was lost (the reaper recovers the event)
    code: str | None = None
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    delivery_id: UUID
    event_id: UUID
    kind: str
    reason: str | None = None


@dataclass(slots=True)
class DispatchReport:
    workspace_id: UUID
    events: list[EventResult] = field(default_factory=list)
    deliveries: list[DeliveryResult] = field(default_factory=list)
    reconciled: list[tuple[UUID, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Send:
    provider: Literal["mcp_events", "slack"]
    binding: DestinationBinding


@dataclass(frozen=True, slots=True)
class _Done:
    state: OutboxState
    code: str | None


@dataclass(slots=True)
class _Targets:
    by_row: dict[UUID, tuple[eb.DeliveryTarget, SubscriptionRecord]]


@dataclass(frozen=True, slots=True)
class _Gate:
    """`Dispatcher._delivery_gate`: send ``payload``, or nothing (``refusal`` / ``defer_until``)."""

    payload: dict[str, Any] | None = None
    refusal: str | None = None
    defer_until: datetime | None = None


@dataclass(slots=True)
class _OwnerRun:
    """What happened to the deliveries of the event whose lease this cycle holds."""

    owner_id: UUID | None
    lease: ClaimedEvent | None  # None once the event lease is known lost
    reached: bool = False  # one of its deliveries was claimed in this cycle
    began: bool = False  # ``begin_send`` was attempted before its first request
    refusal: str | None = None  # its delivery was refused by the gate (stale / route)


def default_dispatcher_id() -> str:
    return f"dispatcher:{socket.gethostname()[:60]}:{os.getpid()}:{uuid4().hex[:8]}"


def _payload_uuid(payload: Mapping[str, Any], key: str) -> UUID:
    try:
        return UUID(str(payload[key]))
    except (KeyError, ValueError) as exc:
        raise StaleEvent(INVALID_PAYLOAD) from exc


def _payload_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise StaleEvent(INVALID_PAYLOAD)
    return value


async def revalidate(
    conn: Conn,
    actor: ActorContext,
    event: OutboxRecord,
    *,
    now: datetime,
) -> None:
    """Raise `StaleEvent` when the event no longer matches the committed current versions.

    An expired valuation is marked stale here (its recomputation is queued in the same transaction;
    the caller commits it together with the event cancellation).
    """
    if event.aggregate_type != "review_case":
        raise StaleEvent(UNROUTABLE_EVENT_TYPE)
    payload = event.payload
    case_id = _payload_uuid(payload, "case_id")
    try:
        case = await reviews_repo.get_case(conn, actor, case_id)
    except NotFound:
        raise StaleEvent("STALE_CASE_MISSING") from None
    if case.is_fixture:
        raise StaleEvent(FIXTURE_CASE)
    expected_state = ReviewState.SHORTLISTED if event.event_type == SHORTLIST_EVENT else ReviewState.PENDING
    if case.state != expected_state:
        raise StaleEvent(f"STALE_CASE_STATE_{case.state.value}".upper())
    if case.case_version != _payload_int(payload, "case_version"):
        raise StaleEvent("STALE_CASE_VERSION")
    if case.listing_revision != _payload_int(payload, "listing_revision"):
        raise StaleEvent("STALE_LISTING_REVISION")
    listing = await listings_repo.get_listing(conn, actor, case.listing_id)
    if listing.is_fixture:
        # Lineage frozen at ingest: never re-derived from the source's current mode.
        raise StaleEvent(FIXTURE_CASE)
    if listing.current_revision_id != case.revision_id:
        raise StaleEvent("STALE_LISTING_REVISION")
    if listing.quarantined:
        raise StaleEvent("LISTING_QUARANTINED")
    if listing.availability in _UNAVAILABLE:
        raise StaleEvent(f"LISTING_{listing.availability.value}".upper())
    if listing.eligibility_state in (None, EligibilityState.REJECTED):
        raise StaleEvent("LISTING_NOT_ELIGIBLE")
    valuation = case.valuation
    if event.event_type == SHORTLIST_EVENT:
        cited = payload.get("valuation_id")
        if (valuation is None) != (cited is None) or (
            valuation is not None and str(valuation.valuation_id) != str(cited)
        ):
            raise StaleEvent("STALE_VALUATION")
    if valuation is not None:
        if valuation.state in _CLOSED_VALUATIONS:
            raise StaleEvent("STALE_VALUATION")
        if valuation.expires_at is not None and valuation.expires_at <= now:
            await valuation_repo.mark_stale(
                conn,
                actor,
                valuation.valuation_id,
                InvalidationReason.FRESHNESS_DEADLINE,
                detail="expired before dispatch",
            )
            raise StaleEvent("VALUATION_EXPIRED")
    paused = await sources_repo.alert_pause_reason(conn, actor, listing.source_id)
    if paused is not None:
        raise StaleEvent(SOURCE_ALERTS_PAUSED)


_SIGNAL_REPLY_SQL: Final = """
select r.id, r.inquiry_id, r.quarantined, r.message_type, i.qualification_listing_id as listing_id,
       coalesce(l.is_fixture, true) as is_fixture
  from app.seller_replies r
  join app.seller_inquiries i on i.workspace_id = r.workspace_id and i.id = r.inquiry_id
  left join app.listings l on l.workspace_id = i.workspace_id and l.id = i.qualification_listing_id
 where r.workspace_id = %(ws)s and r.id = %(reply)s
"""


async def revalidate_signal(
    conn: Conn,
    actor: ActorContext,
    event: OutboxRecord,
    *,
    now: datetime,
) -> None:
    """Raise `StaleEvent` unless a category signal still describes the committed state.

    ``seller.reply.received``: the reply exists, belongs to the named inquiry and is not
    quarantined; fixture lineage (the inquiry's listing, frozen at ingest) is never delivered.
    ``seller_reply.owner_alert``: the same, and an ``opportunity_supported`` alert must still cite
    the listing's current valuation, which must still be notifiable (not stale, invalid or
    expired) for an available, eligible, unquarantined listing whose source alerts are not paused.
    """
    payload = event.payload
    if event.is_fixture or payload.get("fixture") is True:
        raise StaleEvent(FIXTURE_CASE)
    expected_aggregate = "seller_reply" if event.event_type == SELLER_REPLY_EVENT else "seller_inquiry"
    if event.event_type not in SIGNAL_EVENTS or event.aggregate_type != expected_aggregate:
        raise StaleEvent(UNROUTABLE_EVENT_TYPE)
    reply_id = _payload_uuid(payload, "reply_id")
    inquiry_id = _payload_uuid(payload, "inquiry_id")
    row = await fetch_one(conn, _SIGNAL_REPLY_SQL, {"ws": actor.workspace_id, "reply": reply_id})
    if row is None:
        raise StaleEvent("STALE_REPLY_MISSING")
    if row["inquiry_id"] != inquiry_id:
        raise StaleEvent(INVALID_PAYLOAD)
    if row["is_fixture"]:
        raise StaleEvent(FIXTURE_CASE)
    if row["quarantined"]:
        raise StaleEvent("REPLY_QUARANTINED")
    if event.event_type != SELLER_REPLY_ALERT_EVENT or payload.get("kind") != "opportunity_supported":
        return
    listing = await listings_repo.get_listing(conn, actor, row["listing_id"])
    if listing.quarantined:
        raise StaleEvent("LISTING_QUARANTINED")
    if listing.availability in _UNAVAILABLE:
        raise StaleEvent(f"LISTING_{listing.availability.value}".upper())
    if listing.eligibility_state in (None, EligibilityState.REJECTED):
        raise StaleEvent("LISTING_NOT_ELIGIBLE")
    cited = _payload_uuid(payload, "valuation_id")
    try:
        current = await valuation_repo.current_valuation(conn, actor, listing.id)
    except (NotFound, ValidationFailed):
        current = None
    if current is None or current.id != cited:
        raise StaleEvent("STALE_VALUATION")
    valuation = current.valuation
    if valuation.state in _CLOSED_VALUATIONS or not valuation.can_notify:
        raise StaleEvent("STALE_VALUATION")
    if valuation.expires_at is not None and ensure_utc(valuation.expires_at) <= now:
        await valuation_repo.mark_stale(
            conn, actor, current.id, InvalidationReason.FRESHNESS_DEADLINE, detail="expired before dispatch"
        )
        raise StaleEvent("VALUATION_EXPIRED")
    if await sources_repo.alert_pause_reason(conn, actor, listing.source_id) is not None:
        raise StaleEvent(SOURCE_ALERTS_PAUSED)


# The lease of `outbox.claim_events` for the category signals, with a NULL-safe fixture test.
# `outbox.claim_events` evaluates its "looks like a fixture" predicate to NULL for a payload
# without a ``summary`` (``jsonb_typeof(NULL) = 'string'`` is NULL), so a signal payload -- which
# never carries a summary -- is neither leased nor refused there (see the foundation change
# request). Same lease columns and semantics; fixture rows (flag, ``fixture`` marker, fixture
# summary) are never leased here either.
_SIGNAL_CLAIM_SQL: Final = sql.SQL(
    """
with picked as (
  select o.id
    from ops.outbox o
   where o.workspace_id = %(workspace_id)s
     and o.event_type = any(%(types)s)
     and o.state in ('pending', 'retry_wait')
     and not o.is_fixture
     and o.attempts < o.max_attempts
     and o.available_at <= now()
     and coalesce(o.payload -> 'fixture', 'false'::jsonb) in ('false'::jsonb, 'null'::jsonb)
     and not coalesce(pg_catalog.jsonb_typeof(o.payload -> 'summary') = 'string'
                      and pg_catalog.starts_with(pg_catalog.upper(pg_catalog.regexp_replace(
                            o.payload ->> 'summary', '^[[:space:]]+', '')), %(fixture_prefix)s), false)
   order by o.available_at, o.id
   for update of o skip locked
   limit %(limit)s
)
update ops.outbox o
   set state = 'sending',
       lease_owner = %(owner)s,
       lease_token = gen_random_uuid(),
       lease_expires_at = now() + %(lease)s::interval,
       last_heartbeat_at = now(),
       attempts = o.attempts + 1
  from picked
 where o.id = picked.id
   and o.workspace_id = %(workspace_id)s
returning {columns}
"""
).format(columns=sql.SQL(", ").join(sql.Identifier("o", c) for c in OUTBOX_COLUMNS))


async def claim_signal_events(
    db: Database, workspace_id: UUID, dispatcher_id: str, lease_seconds: float, limit: int
) -> list[ClaimedEvent]:
    """Lease up to ``limit`` due category-signal events (one short transaction, SKIP LOCKED)."""
    async with mapped_errors(), db.transaction(workspace_id=workspace_id) as conn, mapped_errors():
        rows = await fetch_all(
            conn,
            _SIGNAL_CLAIM_SQL,
            {
                "workspace_id": workspace_id,
                "types": sorted(SIGNAL_EVENTS),
                "owner": dispatcher_id,
                "lease": timedelta(seconds=float(lease_seconds)),
                "limit": limit,
                "fixture_prefix": FIXTURE_SUMMARY_PREFIX.upper(),
            },
        )
    return sorted((ClaimedEvent.model_validate(r) for r in rows), key=lambda e: (e.available_at, e.id))


class Dispatcher:
    """One dispatcher process. ``run_workspace`` handles one workspace once (tests, CLI)."""

    def __init__(
        self,
        ctx: RuntimeContext,
        *,
        http: SafeHttp | None = None,
        dispatcher_id: str | None = None,
        options: DispatcherOptions | None = None,
        box: SecretBox | None = None,
        rng: random.Random | None = None,
        retry_policy: eb.RetryPolicy = eb.DEFAULT_RETRY_POLICY,
        uncertain_policy: eb.UncertainPolicy | None = None,
    ) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.dispatcher_id = dispatcher_id or default_dispatcher_id()
        self.options = options or DispatcherOptions()
        self.retry_policy = retry_policy
        self.uncertain_policy = uncertain_policy or eb.UncertainPolicy()
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security
        self._http = http
        self._box = box

    # ------------------------------------------------------------------ settings-level selection

    def selection(self) -> tuple[eb.ActivationSelection | None, str | None]:
        """The settings-level route, or ``(None, code)`` when external delivery is refused."""
        if not self.settings.allow_external_notifications:
            return None, EXTERNAL_DISABLED
        try:
            selected = eb.select_activation_route(self.settings, clock=self.ctx.clock)
        except eb.ActivationRouteConflict:
            return None, ROUTE_CONFLICT
        if selected.route is eb.ActivationRoute.NONE:
            return None, BRIDGE_UNAVAILABLE
        return selected, None

    def _secret_box(self) -> SecretBox | None:
        if self._box is None:
            try:
                self._box = SecretBox.from_settings(self.settings)
            except AppError:
                return None
        return self._box

    def http(self) -> SafeHttp:
        if self._http is None:
            self._http = SafeHttpClient(
                resolver=self.ctx.resolver, proxy=self.settings.callback_egress_proxy_url
            )
        return self._http

    # ------------------------------------------------------------------ cycle

    async def run_once(self) -> list[DispatchReport]:
        reports: list[DispatchReport] = []
        for workspace_id in await active_workspace_ids(self.ctx.db):
            try:
                reports.append(await self.run_workspace(workspace_id))
            except AppError as exc:
                logger.warning("dispatch failed for a workspace", extra={"error_code": exc.code.value})
        return reports

    async def run(self, stop: anyio.Event) -> None:
        while not stop.is_set():
            try:
                reports = await self.run_once()
            except AppError as exc:
                logger.warning("dispatcher cycle failed", extra={"error_code": exc.code.value})
                reports = []
            busy = any(r.events or r.deliveries for r in reports)
            if not busy:
                with anyio.move_on_after(self.options.idle_poll_seconds):
                    await stop.wait()

    async def run_workspace(self, workspace_id: UUID) -> DispatchReport:
        actor = system_actor(workspace_id, "dispatcher")
        report = DispatchReport(workspace_id=workspace_id)
        selected, refusal = self.selection()
        for _ in range(self.options.events_per_cycle):
            # Category signals first (a seller reply is time-critical and rare: at most a few per
            # day under the inquiry caps); the generic claim cannot lease them (see
            # `claim_signal_events`) and also refuses fixture rows into ``blocked``.
            claimed = await claim_signal_events(
                self.ctx.db, workspace_id, self.dispatcher_id, self.options.outbox_lease_seconds, 1
            )
            if not claimed:
                claimed = await outbox.claim_events(
                    self.ctx.db, workspace_id, self.dispatcher_id, self.options.outbox_lease_seconds, 1
                )
            if not claimed:
                break
            event = claimed[0]
            with log_context(event_id=event.event_id, request_id=actor.request_id):
                report.events.append(await self._handle_event(actor, event, selected, refusal, report))
        if selected is not None and selected.route is eb.ActivationRoute.MCP_EVENTS:
            await self._deliver_due(actor, report, None, selected)
        await self._follow_up_uncertain(actor, report, selected)
        await self._record_stats(actor)
        return report

    # ------------------------------------------------------------------ one event

    async def _transition(
        self, actor: ActorContext, event: ClaimedEvent, state: OutboxState, code: str
    ) -> EventResult:
        async def once() -> None:
            async with unit_of_work(self.ctx.db, actor) as conn:
                if state == OutboxState.BLOCKED:
                    await outbox.mark_blocked(conn, event, code)
                elif state == OutboxState.CANCELLED:
                    await outbox.cancel_stale(conn, actor, event.event_id, code, lease=event)
                else:  # pragma: no cover - only blocked/cancelled are applied here
                    raise ValueError(state)

        try:
            await retry_transient(once)
        except LeaseLost:
            return EventResult(event.event_id, event.event_type, None, code)
        logger.info("event not dispatched", extra={"state": state.value, "code": code})
        return EventResult(event.event_id, event.event_type, state, code)

    async def _handle_event(
        self,
        actor: ActorContext,
        event: ClaimedEvent,
        selected: eb.ActivationSelection | None,
        refusal: str | None,
        report: DispatchReport,
    ) -> EventResult:
        category = CATEGORY_BY_EVENT.get(event.event_type)
        if category is None:
            return await self._transition(actor, event, OutboxState.BLOCKED, UNROUTABLE_EVENT_TYPE)
        if event.event_type in SIGNAL_EVENTS:
            return await self._handle_signal(actor, event, category)
        if selected is None:
            return await self._transition(actor, event, OutboxState.BLOCKED, refusal or EXTERNAL_DISABLED)
        try:
            decision = await retry_transient(lambda: self._prepare(actor, event, category, selected))
        except LeaseLost:
            return EventResult(event.event_id, event.event_type, None)
        except ValidationFailed:
            # A payload that does not parse can never be delivered: visible, never retried.
            return await self._transition(actor, event, OutboxState.BLOCKED, INVALID_PAYLOAD)
        if isinstance(decision, _Done):
            logger.info("event not dispatched", extra={"state": decision.state.value, "code": decision.code})
            return EventResult(event.event_id, event.event_type, decision.state, decision.code)
        if decision.provider == "slack":
            return await self._send_slack(actor, event, decision.binding)
        run = await self._deliver_due(actor, report, event, selected)
        return await self._finish_mcp_event(actor, event, sent=run.began, refusal=run.refusal)

    async def _prepare(
        self,
        actor: ActorContext,
        event: ClaimedEvent,
        category: EventCategory,
        selected: eb.ActivationSelection,
    ) -> _Send | _Done:
        """Revalidation, route checks and the MCP Events fan-out in ONE short transaction."""
        async with unit_of_work(self.ctx.db, actor) as conn:
            now = ensure_utc(await db_now(conn))
            try:
                await revalidate(conn, actor, event, now=now)
            except StaleEvent as stale:
                if stale.code in _BLOCKING_REFUSALS:
                    await outbox.mark_blocked(conn, event, stale.code)
                    return _Done(OutboxState.BLOCKED, stale.code)
                await outbox.cancel_stale(conn, actor, event.event_id, stale.code, lease=event)
                return _Done(OutboxState.CANCELLED, stale.code)
            route = await bindings_repo.selected_route(conn, actor, category)
            blocked, binding = await self._route_problem(conn, actor, event, route, selected)
            if blocked is not None or route is None or binding is None:
                code = blocked or NO_ACTIVE_ROUTE
                await outbox.mark_blocked(conn, event, code)
                return _Done(OutboxState.BLOCKED, code)
            deferred = self._quiet_hours(now, route.quiet_hours, event)
            if deferred is not None:
                await outbox.mark_retry(conn, event, deferred, "QUIET_HOURS")
                return _Done(OutboxState.RETRY_WAIT, "QUIET_HOURS")
            if category == "owner_alert":
                # No provider message builder exists yet for owner-facing opportunity alerts
                # (spec 22 content rules); the pending-review wording must never be reused for
                # them. Visible typed blocker until that builder (and its pre-alert freshness
                # recheck) is implemented.
                await outbox.mark_blocked(conn, event, OWNER_ALERT_UNSUPPORTED)
                return _Done(OutboxState.BLOCKED, OWNER_ALERT_UNSUPPORTED)
            if route.provider == "slack":
                return _Send("slack", binding)
            fanned = await self._fan_out(conn, actor, event)
            if fanned is not None:
                await outbox.mark_blocked(conn, event, fanned)
                return _Done(OutboxState.BLOCKED, fanned)
            return _Send("mcp_events", binding)

    async def _route_problem(
        self,
        conn: Conn,
        actor: ActorContext,
        event: OutboxRecord,
        route: ActivationRouteSelection | None,
        selected: eb.ActivationSelection,
    ) -> tuple[str | None, DestinationBinding | None]:
        if route is None:
            return NO_ACTIVE_ROUTE, None
        if event.destination_binding_id is not None and event.destination_binding_id != route.binding_id:
            return ROUTE_CHANGED, None  # never re-routed to a destination not approved for it
        if route.provider != selected.route.value:
            return ROUTE_PROVIDER_MISMATCH, None
        binding = await bindings_repo.get_binding(conn, actor, route.binding_id)
        if not binding.enabled or binding.approved_at is None or binding.verified_at is None:
            return DESTINATION_NOT_VERIFIED, binding
        if route.provider == "mcp_events" and self._secret_box() is None:
            return SECRETS_UNAVAILABLE, binding
        return None, binding

    def _quiet_hours(self, now: datetime, quiet: QuietHours | None, event: OutboxRecord) -> datetime | None:
        urgency: Urgency = "urgent" if event.payload.get("priority") == "urgent" else "normal"
        decision = evaluate_quiet_hours(now, quiet, urgency)
        return None if decision.deliver_now else decision.deliver_at

    async def _fan_out(self, conn: Conn, actor: ActorContext, event: ClaimedEvent) -> str | None:
        """Delivery rows for the matching verified subscriptions (``None`` = something to deliver)."""
        box = self._secret_box()
        assert box is not None
        signal_ = eb.parse_signal(event.payload)
        active = await subscriptions_repo.list_active_for_event(conn, actor, eb.EVENT_NAME, box)
        matching = [
            record.id
            for target, record in zip(active.targets, active.records, strict=True)
            if eb.matches_filters(target.arguments, signal_)
        ]
        if matching:
            await subscriptions_repo.create_deliveries(
                conn, actor, event.event_id, matching, max_attempts=self.options.delivery_max_attempts
            )
        existing = await subscriptions_repo.list_deliveries(conn, actor, event_id=event.event_id)
        return None if existing else NO_SUBSCRIPTION

    # ------------------------------------------------------------------ MCP Events deliveries

    async def _targets(self, actor: ActorContext) -> _Targets:
        box = self._secret_box()
        if box is None:
            return _Targets(by_row={})
        async with unit_of_work(self.ctx.db, actor) as conn:
            active = await subscriptions_repo.list_active_for_event(conn, actor, eb.EVENT_NAME, box)
        return _Targets(by_row={r.id: (t, r) for t, r in zip(active.targets, active.records, strict=True)})

    async def _deliver_due(
        self,
        actor: ActorContext,
        report: DispatchReport,
        owner: ClaimedEvent | None,
        selected: eb.ActivationSelection,
    ) -> _OwnerRun:
        """Lease and send due deliveries in ``next_attempt_at`` order (``owner``: the event whose
        lease this cycle holds -- only ITS deliveries are claimed; ``None`` for the end-of-cycle
        sweep of every other due delivery). At most ``delivery_batches`` batches either way.

        Every delivery passes `_delivery_gate` immediately before it is sent.
        """
        run = _OwnerRun(owner_id=None if owner is None else owner.event_id, lease=owner)
        event_ids = None if owner is None else [owner.event_id]
        for _ in range(self.options.delivery_batches):
            async with unit_of_work(self.ctx.db, actor) as conn:
                claimed = await subscriptions_repo.claim_due_deliveries(
                    conn,
                    actor,
                    self.dispatcher_id,
                    lease_seconds=self.options.delivery_lease_seconds,
                    limit=self.options.deliveries_per_claim,
                    event_ids=event_ids,
                )
            if not claimed:
                break
            targets = await self._targets(actor)
            for delivery in claimed:
                report.deliveries.append(await self._send_delivery(actor, delivery, targets, selected, run))
            if len(claimed) < self.options.deliveries_per_claim:
                break
        return run

    def _deferred(
        self, delivery: ClaimedDelivery, subscription_id: str, retry_at: datetime
    ) -> eb.DeliveryOutcome:
        """Nothing is sent now: retried at ``retry_at`` (approved quiet hours)."""
        return eb.DeliveryOutcome(
            kind=eb.DeliveryOutcomeKind.SKIPPED,
            subscription_id=subscription_id,
            webhook_id=None,
            attempt=delivery.attempts,
            next_attempt_at=retry_at,
        )

    async def _cancel_delivery(
        self, actor: ActorContext, delivery: ClaimedDelivery, reason: str
    ) -> DeliveryResult:
        """Nothing is sent: the guard refused the delivery (stale event / withdrawn route)."""

        async def once() -> DeliveryRecord:
            async with unit_of_work(self.ctx.db, actor) as conn:
                return await subscriptions_repo.cancel_delivery(conn, actor, delivery, reason)

        try:
            stored = await retry_transient(once)
        except LeaseLost:
            logger.warning("delivery lease lost; the reaper marks it uncertain")
            return DeliveryResult(delivery.id, delivery.event_id, "lease_lost", reason)
        logger.info("delivery cancelled", extra={"code": reason})
        return DeliveryResult(delivery.id, delivery.event_id, stored.state, reason)

    async def _send_delivery(
        self,
        actor: ActorContext,
        delivery: ClaimedDelivery,
        targets: _Targets,
        selected: eb.ActivationSelection,
        run: _OwnerRun,
    ) -> DeliveryResult:
        is_owner = run.owner_id is not None and delivery.event_id == run.owner_id
        if is_owner:
            run.reached = True
        found = targets.by_row.get(delivery.subscription_id)
        if found is None:
            outcome = eb.DeliveryOutcome(
                kind=eb.DeliveryOutcomeKind.SKIPPED,
                subscription_id="inactive",
                webhook_id=None,
                attempt=delivery.attempts,
                reason=eb.DeliveryFailureReason.SUBSCRIPTION_INACTIVE,
                block_reason=eb.BlockReason.REVOKED,
            )
            return await self._record_delivery(actor, delivery, outcome)
        target, record = found
        gate = await self._delivery_gate(actor, delivery.event_id, selected)
        if gate.refusal is not None:
            if is_owner:
                run.refusal = gate.refusal
            return await self._cancel_delivery(actor, delivery, gate.refusal)
        if gate.payload is None:  # deferred by approved quiet hours: nothing is sent now
            assert gate.defer_until is not None
            outcome = self._deferred(delivery, target.subscription_id, gate.defer_until)
            return await self._record_delivery(actor, delivery, outcome)
        try:
            occurrence = eb.build_occurrence(gate.payload)
        except AppError:
            outcome = eb.DeliveryOutcome(
                kind=eb.DeliveryOutcomeKind.FAILED,
                subscription_id=target.subscription_id,
                webhook_id=None,
                attempt=delivery.attempts,
                reason=eb.DeliveryFailureReason.INVALID_OCCURRENCE,
            )
            return await self._record_delivery(actor, delivery, outcome)
        if is_owner and not run.began:
            run.began = True
            if run.lease is not None:
                try:
                    async with unit_of_work(self.ctx.db, actor) as conn:
                        await outbox.begin_send(conn, run.lease)
                except LeaseLost:
                    # Our event lease is gone: its deliveries are still sent under their own leases
                    # (each passed the gate just now); the reaper settles the outbox row.
                    logger.warning("event lease lost before sending")
                    run.lease = None

        async def access_check(_target: eb.DeliveryTarget) -> bool:
            async with unit_of_work(self.ctx.db, actor) as conn:
                return await subscriptions_repo.check_subscriber_access(conn, actor, record)

        with log_context(event_id=delivery.event_id):
            outcome = await eb.deliver(
                occurrence,
                target,
                self.http(),
                attempt=delivery.attempts,
                clock=self.ctx.clock,
                access_check=access_check,
                rng=self._rng,
                retry_policy=self.retry_policy,
                first_attempt_at=delivery.created_at,
            )
        self.ctx.metrics.record_delivery("mcp_events", _metric_outcome(outcome.kind), latency=outcome.elapsed)
        return await self._record_delivery(actor, delivery, outcome)

    async def _delivery_gate(
        self, actor: ActorContext, event_id: UUID, selected: eb.ActivationSelection
    ) -> _Gate:
        """The dispatch guard of ONE delivery, immediately before it is sent (spec 18, 22).

        The event must still be current (`revalidate`) and its category's route must still be the
        approved, enabled and verified one selected by the settings (an owner who disables or
        re-routes the destination stops every delivery at once, including retries and the
        uncertain resend); approved quiet hours defer it.
        """
        try:
            async with unit_of_work(self.ctx.db, actor) as conn:
                event = await outbox.get_event(conn, actor, event_id)
                if event.is_fixture:
                    return _Gate(refusal=FIXTURE_CASE)
                if event.state not in _DELIVERABLE_EVENT_STATES:
                    return _Gate(refusal=f"EVENT_{event.state.value}".upper())
                category = CATEGORY_BY_EVENT.get(event.event_type)
                if category is None:
                    return _Gate(refusal=UNROUTABLE_EVENT_TYPE)
                now = ensure_utc(await db_now(conn))
                await revalidate(conn, actor, event, now=now)
                route = await bindings_repo.selected_route(conn, actor, category)
                problem, _binding = await self._route_problem(conn, actor, event, route, selected)
                if problem is not None or route is None:
                    return _Gate(refusal=problem or NO_ACTIVE_ROUTE)
                if category == "owner_alert":
                    return _Gate(refusal=OWNER_ALERT_UNSUPPORTED)
                deferred = self._quiet_hours(now, route.quiet_hours, event)
                if deferred is not None:
                    return _Gate(defer_until=deferred)
                return _Gate(payload=dict(event.payload))
        except StaleEvent as stale:  # rolled back: nothing of the read transaction is kept
            return _Gate(refusal=stale.code)
        except NotFound:
            return _Gate(refusal="STALE_EVENT_MISSING")

    async def _record_delivery(
        self, actor: ActorContext, delivery: ClaimedDelivery, outcome: eb.DeliveryOutcome
    ) -> DeliveryResult:
        async def once() -> DeliveryRecord:
            async with unit_of_work(self.ctx.db, actor) as conn:
                return await subscriptions_repo.record_delivery_outcome(conn, actor, delivery, outcome)

        reason = None if outcome.reason is None else outcome.reason.value
        try:
            stored = await retry_transient(once)
        except LeaseLost:
            logger.warning("delivery lease lost; the reaper marks it uncertain")
            return DeliveryResult(delivery.id, delivery.event_id, "lease_lost", reason)
        logger.info("delivery recorded", extra={"outcome": stored.state, "reason": reason})
        return DeliveryResult(delivery.id, delivery.event_id, stored.state, reason)

    async def _finish_mcp_event(
        self, actor: ActorContext, event: ClaimedEvent, *, sent: bool, refusal: str | None = None
    ) -> EventResult:
        """Aggregate the event's deliveries into the outbox row (see module docstring)."""

        async def once() -> tuple[OutboxState, str | None]:
            async with unit_of_work(self.ctx.db, actor) as conn:
                deliveries = await subscriptions_repo.list_deliveries(conn, actor, event_id=event.event_id)
                return await self._aggregate(conn, actor, event, deliveries, sent_now=sent, refusal=refusal)

        try:
            state, code = await retry_transient(once)
        except LeaseLost:
            return EventResult(event.event_id, event.event_type, None, provider="mcp_events")
        return EventResult(event.event_id, event.event_type, state, code, "mcp_events")

    async def _aggregate(
        self,
        conn: Conn,
        actor: ActorContext,
        event: ClaimedEvent,
        deliveries: Sequence[DeliveryRecord],
        *,
        sent_now: bool,
        refusal: str | None = None,
    ) -> tuple[OutboxState, str | None]:
        """One outbox attempt row per cycle in which a delivery of the event was attempted.

        ``refusal``: the dispatch guard refused the event's delivery right before sending (the event
        became stale, or its route was disabled / changed); with nothing delivered, uncertain or
        waiting, the event is cancelled (stale) or blocked (route) with that reason.
        """
        accepted = [d for d in deliveries if d.state == "accepted" and d.accepted_at is not None]
        uncertain = [d for d in deliveries if d.state == "uncertain"]
        waiting = [d for d in deliveries if d.state in _IN_PROGRESS]
        if accepted:
            first = min(d.accepted_at for d in accepted if d.accepted_at is not None)
            if sent_now:
                await outbox.record_attempt(
                    conn,
                    event,
                    uuid4(),
                    DeliveryOutcome.ACCEPTED,
                    receipt=f"mcp_events:{len(accepted)}/{len(deliveries)} accepted",
                    provider="mcp_events",
                )
            await outbox.mark_delivered(conn, event, provider_accepted_at=first)
            return OutboxState.DELIVERED, None
        if uncertain:
            if sent_now:
                await outbox.record_attempt(
                    conn,
                    event,
                    uuid4(),
                    DeliveryOutcome.UNCERTAIN,
                    error="DELIVERY_UNCERTAIN",
                    provider="mcp_events",
                )
            await outbox.mark_uncertain(conn, event, "DELIVERY_UNCERTAIN")
            return OutboxState.UNCERTAIN, "DELIVERY_UNCERTAIN"
        if waiting:
            due = min(d.next_attempt_at for d in waiting)
            if not sent_now:
                # Nothing of this event was sent in this cycle (its deliveries are sent by the
                # sweep): never re-queue it for an immediate re-claim that would only burn attempts.
                due = max(due, ensure_utc(await db_now(conn)) + _IDLE_REQUEUE)
            if sent_now:
                await outbox.record_attempt(
                    conn,
                    event,
                    uuid4(),
                    DeliveryOutcome.RETRYABLE,
                    error="DELIVERY_RETRY",
                    provider="mcp_events",
                )
            state = await outbox.mark_retry(conn, event, due, "DELIVERY_RETRY")
            return state, "DELIVERY_RETRY"
        if refusal is not None:
            if refusal in _BLOCKING_REFUSALS:
                await outbox.mark_blocked(conn, event, refusal)
                return OutboxState.BLOCKED, refusal
            await outbox.cancel_stale(conn, actor, event.event_id, refusal, lease=event)
            return OutboxState.CANCELLED, refusal
        codes = sorted({d.safe_error or d.state for d in deliveries})
        if sent_now:
            await outbox.record_attempt(
                conn,
                event,
                uuid4(),
                DeliveryOutcome.TERMINAL,
                error="DELIVERY_FAILED",
                provider="mcp_events",
                error_detail=", ".join(codes)[:300],
            )
        await outbox.mark_dead_letter(conn, event, "DELIVERY_FAILED")
        return OutboxState.DEAD_LETTER, "DELIVERY_FAILED"

    # ------------------------------------------------------------------ Slack (selected route only)

    def _slack_config(self, binding: DestinationBinding) -> slack.SlackConfig | None:
        try:
            config = slack.SlackConfig.from_settings(
                self.settings,
                destination_approval_ref=binding.approval_reference,
                team_id=self.settings.slack_team_id or binding.external_workspace_id,
                app_id=self.settings.slack_app_id,
                bot_id=self.settings.slack_bot_id,
                bot_user_id=self.settings.slack_bot_user_id,
            )
        except AppError:
            return None
        if binding.external_channel_id != config.channel_id:
            return None
        return config

    async def _send_slack(
        self, actor: ActorContext, event: ClaimedEvent, binding: DestinationBinding
    ) -> EventResult:
        config = self._slack_config(binding)
        if config is None or slack.send_blockers(self.settings, config):
            return await self._transition(actor, event, OutboxState.BLOCKED, SLACK_BLOCKED)
        try:
            async with unit_of_work(self.ctx.db, actor) as conn:
                await outbox.begin_send(conn, event)
        except LeaseLost:
            return EventResult(event.event_id, event.event_type, None, provider="slack")
        try:
            posted = await slack.post_review_message(
                event.payload, config=config, settings=self.settings, http=self.http(), clock=self.ctx.clock
            )
        except AppError:
            # Refused before any request (blocked or an invalid payload): nothing was sent.
            return await self._transition(actor, event, OutboxState.BLOCKED, SLACK_BLOCKED)
        self.ctx.metrics.record_delivery("slack", _slack_metric(posted.kind))

        async def once() -> OutboxState:
            async with unit_of_work(self.ctx.db, actor) as conn:
                return await self._apply_slack(conn, event, posted)

        try:
            state = await retry_transient(once)
        except LeaseLost:
            return EventResult(event.event_id, event.event_type, None, provider="slack")
        return EventResult(event.event_id, event.event_type, state, posted.slack_error, "slack")

    async def _apply_slack(
        self, conn: Conn, event: ClaimedEvent, posted: slack.SlackPostOutcome
    ) -> OutboxState:
        kind = posted.kind
        if kind is slack.SlackOutcomeKind.POSTED:
            receipt = f"{posted.channel_id}:{posted.message_ts}" if posted.message_ts else posted.event_ref
            await outbox.record_attempt(
                conn,
                event,
                uuid4(),
                DeliveryOutcome.ACCEPTED,
                receipt=receipt[:500],
                response_code=posted.status_code,
                provider="slack",
                sent_at=posted.send_attempted_at,
            )
            await outbox.mark_delivered(conn, event, provider_accepted_at=posted.provider_accepted_at)
            return OutboxState.DELIVERED
        if kind is slack.SlackOutcomeKind.UNCERTAIN:
            await outbox.record_attempt(
                conn,
                event,
                uuid4(),
                DeliveryOutcome.UNCERTAIN,
                response_code=posted.status_code,
                error="SLACK_UNCERTAIN",
                provider="slack",
                sent_at=posted.send_attempted_at,
            )
            await outbox.mark_uncertain(conn, event, "SLACK_UNCERTAIN")
            return OutboxState.UNCERTAIN
        if kind is slack.SlackOutcomeKind.RETRY:
            await outbox.record_attempt(
                conn,
                event,
                uuid4(),
                DeliveryOutcome.RETRYABLE,
                response_code=posted.status_code,
                error="SLACK_RETRY",
                provider="slack",
                sent_at=posted.send_attempted_at,
            )
            delay = eb.backoff_delay(event.attempts, self.retry_policy, self._rng)
            if posted.retry_after is not None:
                delay = max(delay, posted.retry_after)
            return await outbox.mark_retry(conn, event, delay, "SLACK_RETRY")
        await outbox.record_attempt(
            conn,
            event,
            uuid4(),
            DeliveryOutcome.TERMINAL,
            response_code=posted.status_code,
            error="SLACK_FAILED",
            provider="slack",
            error_detail=posted.slack_error,
            sent_at=posted.send_attempted_at,
        )
        await outbox.mark_dead_letter(conn, event, "SLACK_FAILED")
        return OutboxState.DEAD_LETTER

    # ------------------------------------------------------------------ category signals (v1.1)

    async def _handle_signal(
        self, actor: ActorContext, event: ClaimedEvent, category: EventCategory
    ) -> EventResult:
        """A seller-reply signal or a seller-reply owner alert: Slack route of its category only."""
        if not self.settings.allow_external_notifications:
            return await self._transition(actor, event, OutboxState.BLOCKED, EXTERNAL_DISABLED)
        if category == "seller_reply" and self.settings.seller_reply_signal_provider != "slack":
            return await self._transition(actor, event, OutboxState.BLOCKED, SIGNAL_DISABLED)
        try:
            decision = await retry_transient(lambda: self._prepare_signal(actor, event, category))
        except LeaseLost:
            return EventResult(event.event_id, event.event_type, None)
        except ValidationFailed:
            return await self._transition(actor, event, OutboxState.BLOCKED, INVALID_PAYLOAD)
        if isinstance(decision, _Done):
            logger.info("event not dispatched", extra={"state": decision.state.value, "code": decision.code})
            return EventResult(event.event_id, event.event_type, decision.state, decision.code)
        return await self._send_signal(actor, event, category, decision.binding)

    async def _signal_route(
        self, conn: Conn, actor: ActorContext, event: OutboxRecord, category: EventCategory
    ) -> tuple[str | None, ActivationRouteSelection | None, DestinationBinding | None]:
        """The approved, enabled and verified Slack route of ``category`` (or a blocker code)."""
        route = await bindings_repo.selected_route(conn, actor, category)
        if route is None:
            return NO_ACTIVE_ROUTE, None, None
        if event.destination_binding_id is not None and event.destination_binding_id != route.binding_id:
            return ROUTE_CHANGED, route, None
        if route.provider != "slack":
            return SIGNAL_ROUTE_NOT_SLACK, route, None  # never MCP Events (candidate discovery only)
        binding = await bindings_repo.get_binding(conn, actor, route.binding_id)
        if (
            binding.provider != "slack"
            or not binding.enabled
            or binding.approved_at is None
            or binding.verified_at is None
        ):
            return DESTINATION_NOT_VERIFIED, route, binding
        return None, route, binding

    async def _prepare_signal(
        self, actor: ActorContext, event: ClaimedEvent, category: EventCategory
    ) -> _Send | _Done:
        """Revalidation and the route checks of one signal in ONE short transaction."""
        async with unit_of_work(self.ctx.db, actor) as conn:
            now = ensure_utc(await db_now(conn))
            try:
                await revalidate_signal(conn, actor, event, now=now)
            except StaleEvent as stale:
                if stale.code in _BLOCKING_REFUSALS:
                    await outbox.mark_blocked(conn, event, stale.code)
                    return _Done(OutboxState.BLOCKED, stale.code)
                await outbox.cancel_stale(conn, actor, event.event_id, stale.code, lease=event)
                return _Done(OutboxState.CANCELLED, stale.code)
            problem, route, binding = await self._signal_route(conn, actor, event, category)
            if problem is not None or route is None or binding is None:
                code = problem or NO_ACTIVE_ROUTE
                await outbox.mark_blocked(conn, event, code)
                return _Done(OutboxState.BLOCKED, code)
            deferred = self._quiet_hours(now, route.quiet_hours, event)
            if deferred is not None:
                await outbox.mark_retry(conn, event, deferred, "QUIET_HOURS")
                return _Done(OutboxState.RETRY_WAIT, "QUIET_HOURS")
            return _Send("slack", binding)

    async def _send_signal(
        self, actor: ActorContext, event: ClaimedEvent, category: EventCategory, binding: DestinationBinding
    ) -> EventResult:
        config = self._slack_config(binding)
        if config is None or slack.signal_send_blockers(self.settings, config, category=category):
            return await self._transition(actor, event, OutboxState.BLOCKED, SLACK_BLOCKED)
        # Parse the stored payload BEFORE the send is marked as attempted: a payload that does not
        # parse (e.g. a non-https dashboard link) is visible and never retried; nothing was sent.
        notice: slack.SlackSellerReplyNotice | slack.SlackOwnerAlertNotice
        try:
            if event.event_type == SELLER_REPLY_EVENT:
                notice = slack.SlackSellerReplyNotice.model_validate(dict(event.payload))
            else:
                notice = slack.SlackOwnerAlertNotice.model_validate(dict(event.payload))
        except ValidationError:
            return await self._transition(actor, event, OutboxState.BLOCKED, INVALID_PAYLOAD)
        if notice.event_id != event.event_id:
            return await self._transition(actor, event, OutboxState.BLOCKED, INVALID_PAYLOAD)
        try:
            async with unit_of_work(self.ctx.db, actor) as conn:
                await outbox.begin_send(conn, event)
        except LeaseLost:
            return EventResult(event.event_id, event.event_type, None, provider="slack")
        try:
            if isinstance(notice, slack.SlackSellerReplyNotice):
                posted = await slack.post_seller_reply_signal(
                    notice, config=config, settings=self.settings, http=self.http(), clock=self.ctx.clock
                )
            else:
                posted = await slack.post_owner_alert(
                    notice, config=config, settings=self.settings, http=self.http(), clock=self.ctx.clock
                )
        except AppError:
            # Refused before any request (activation gates): nothing was sent.
            return await self._transition(actor, event, OutboxState.BLOCKED, SLACK_BLOCKED)
        self.ctx.metrics.record_delivery("slack", _slack_metric(posted.kind))

        async def once() -> OutboxState:
            async with unit_of_work(self.ctx.db, actor) as conn:
                return await self._apply_slack(conn, event, posted)

        try:
            state = await retry_transient(once)
        except LeaseLost:
            return EventResult(event.event_id, event.event_type, None, provider="slack")
        return EventResult(event.event_id, event.event_type, state, posted.slack_error, "slack")

    async def _follow_up_signal(self, actor: ActorContext, event: OutboxRecord, now: datetime) -> str | None:
        """An uncertain signal post: channel-history lookup first; only ``not_found`` re-queues
        the SAME event (same id and ``SDR-`` reference) for one more post."""
        category = CATEGORY_BY_EVENT.get(event.event_type)
        if category is None or event.send_attempted_at is None:
            return None
        if now - event.updated_at < self.uncertain_policy.hold_for:
            return None
        if not self.settings.allow_external_notifications:
            return None
        async with unit_of_work(self.ctx.db, actor) as conn:
            problem, _route, binding = await self._signal_route(conn, actor, event, category)
        if problem is not None or binding is None:
            return None  # a disabled/unapproved destination is not contacted, not even to look up
        config = self._slack_config(binding)
        if config is None or slack.signal_send_blockers(self.settings, config, category=category):
            return None
        found = await slack.reconcile_uncertain_signal_post(
            slack.event_reference(event.event_id),
            posted_after=event.send_attempted_at,
            config=config,
            settings=self.settings,
            http=self.http(),
            category=category,
        )
        if found.state is slack.ReconcileState.UNKNOWN:
            return None
        accepted = found.state is slack.ReconcileState.FOUND
        async with unit_of_work(self.ctx.db, actor) as conn:
            await outbox.reconcile_uncertain(
                conn,
                actor,
                event.event_id,
                provider="slack",
                accepted=accepted,
                receipt=f"{config.channel_id}:{found.message_ts}" if accepted and found.message_ts else None,
                note="channel history lookup",
            )
        return "delivered" if accepted else "resend_scheduled"

    # ------------------------------------------------------------------ uncertain follow-up

    async def _follow_up_uncertain(
        self, actor: ActorContext, report: DispatchReport, selected: eb.ActivationSelection | None
    ) -> None:
        async with unit_of_work(self.ctx.db, actor) as conn:
            attention = await outbox.list_attention(conn, actor, limit=self.options.reconcile_limit)
            now = ensure_utc(await db_now(conn))
        for event in attention:
            if event.state != OutboxState.UNCERTAIN or event.is_fixture:
                continue
            is_signal = event.event_type in SIGNAL_EVENTS
            if selected is None and not is_signal:
                continue
            with log_context(event_id=event.event_id):
                try:
                    if is_signal:
                        outcome = await self._follow_up_signal(actor, event, now)
                    elif selected is not None and selected.route is eb.ActivationRoute.MCP_EVENTS:
                        outcome = await self._follow_up_mcp(actor, event, now)
                    else:
                        outcome = await self._follow_up_slack(actor, event, now)
                except AppError as exc:
                    logger.warning("uncertain follow-up failed", extra={"error_code": exc.code.value})
                    continue
            if outcome is not None:
                report.reconciled.append((event.event_id, outcome))

    async def _follow_up_mcp(self, actor: ActorContext, event: OutboxRecord, now: datetime) -> str | None:
        """MCP Events has no lookup API: hold, then at most one resend with the same event id."""

        async def once() -> str | None:
            async with unit_of_work(self.ctx.db, actor) as conn:
                deliveries = await subscriptions_repo.list_deliveries(conn, actor, event_id=event.event_id)
                accepted = [d for d in deliveries if d.state == "accepted"]
                if accepted:
                    await outbox.reconcile_uncertain(
                        conn,
                        actor,
                        event.event_id,
                        provider="mcp_events",
                        accepted=True,
                        receipt=f"mcp_events:{len(accepted)}/{len(deliveries)} accepted",
                        note="a subscriber delivery has a recorded receipt",
                    )
                    return "delivered"
                requeued = 0
                for delivery in deliveries:
                    if delivery.state != "uncertain":
                        continue
                    decision = eb.decide_uncertain_followup(
                        uncertain_since=delivery.updated_at,
                        # Conservative: only a delivery that became uncertain on its first attempt
                        # is re-sent (once); anything else stays visibly uncertain.
                        resends_done=0 if delivery.attempts <= 1 else self.uncertain_policy.max_resends,
                        now=now,
                        policy=self.uncertain_policy,
                    )
                    if decision is eb.UncertainDecision.RESEND_SAME_EVENT_ID:
                        await subscriptions_repo.requeue_uncertain(conn, actor, delivery.id)
                        requeued += 1
                if requeued:
                    await outbox.reconcile_uncertain(
                        conn,
                        actor,
                        event.event_id,
                        provider="mcp_events",
                        accepted=False,
                        note="no lookup API; one resend with the same eventId after the hold period",
                    )
                    return "resend_scheduled"
                return None

        return await retry_transient(once)

    async def _follow_up_slack(self, actor: ActorContext, event: OutboxRecord, now: datetime) -> str | None:
        if event.destination_binding_id is None or event.send_attempted_at is None:
            return None
        if now - event.updated_at < self.uncertain_policy.hold_for:
            return None
        async with unit_of_work(self.ctx.db, actor) as conn:
            binding = await bindings_repo.get_binding(conn, actor, event.destination_binding_id)
        if binding.provider != "slack" or not binding.enabled or binding.approved_at is None:
            return None  # a disabled/unapproved destination is not contacted, not even to look up
        config = self._slack_config(binding)
        if config is None or slack.send_blockers(self.settings, config):
            return None
        found = await slack.reconcile_uncertain_post(
            slack.event_reference(event.event_id),
            posted_after=event.send_attempted_at,
            config=config,
            settings=self.settings,
            http=self.http(),
        )
        if found.state is slack.ReconcileState.UNKNOWN:
            return None
        accepted = found.state is slack.ReconcileState.FOUND
        async with unit_of_work(self.ctx.db, actor) as conn:
            await outbox.reconcile_uncertain(
                conn,
                actor,
                event.event_id,
                provider="slack",
                accepted=accepted,
                receipt=f"{config.channel_id}:{found.message_ts}" if accepted and found.message_ts else None,
                note="channel history lookup",
            )
        return "delivered" if accepted else "resend_scheduled"

    # ------------------------------------------------------------------ metrics

    async def _record_stats(self, actor: ActorContext) -> None:
        try:
            async with unit_of_work(self.ctx.db, actor) as conn:
                stats = await outbox.outbox_stats(conn, actor)
        except AppError:
            return
        provider = (
            self.settings.event_bridge_provider
            if self.settings.event_bridge_provider != "disabled"
            else "mcp_events"
        )
        counts: dict[OutboxState | str, int] = {state: n for state, n in stats.counts.items()}
        self.ctx.metrics.set_outbox_counts(provider, counts)


def _metric_outcome(kind: eb.DeliveryOutcomeKind) -> str:
    return kind.value


def _slack_metric(kind: slack.SlackOutcomeKind) -> str:
    return {
        slack.SlackOutcomeKind.POSTED: "delivered",
        slack.SlackOutcomeKind.RETRY: "retry",
        slack.SlackOutcomeKind.FAILED: "failed",
        slack.SlackOutcomeKind.UNCERTAIN: "uncertain",
    }[kind]


async def run_dispatcher(
    settings: Settings,
    *,
    dispatcher_id: str | None = None,
    stop: anyio.Event | None = None,
    http: SafeHttp | None = None,
) -> None:
    """Process entry point (``suv-deals dispatcher``): run until SIGTERM/SIGINT."""
    ctx = await build_runtime(settings, application_name="suv-deals-dispatcher", configure_logs=True)
    stop = stop or anyio.Event()
    owned_http: SafeHttpClient | None = None
    if http is None:
        owned_http = SafeHttpClient(resolver=ctx.resolver, proxy=settings.callback_egress_proxy_url)
        http = owned_http
    dispatcher = Dispatcher(ctx, http=http, dispatcher_id=dispatcher_id)
    try:
        async with anyio.create_task_group() as tg:

            async def watch_signals() -> None:
                with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                    async for _signum in signals:
                        logger.info("shutdown requested; finishing the current event")
                        stop.set()
                        return

            async def work() -> None:
                await dispatcher.run(stop)
                tg.cancel_scope.cancel()

            tg.start_soon(watch_signals)
            tg.start_soon(work)
    finally:
        if owned_http is not None:
            await owned_http.aclose()
        await ctx.aclose()


__all__ = [
    "CATEGORY_BY_EVENT",
    "SIGNAL_EVENTS",
    "DeliveryResult",
    "DispatchReport",
    "Dispatcher",
    "DispatcherOptions",
    "EventResult",
    "StaleEvent",
    "claim_signal_events",
    "default_dispatcher_id",
    "revalidate",
    "revalidate_signal",
    "run_dispatcher",
]
