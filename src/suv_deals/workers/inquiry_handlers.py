"""Runtime workers of the bounded automatic seller inquiry (spec 37.1-37.5, 37.10 U3/U6).

Three job types, registered in `workers.handlers.default_registry`:

``seller_inquiry_plan`` (`handle_seller_inquiry_plan`)
    Enqueued ONCE per listing revision by the valuation pipeline (`enqueue_plan_job`, dedup key
    ``seller_inquiry.plan:<listing>:<revision>`` checked over every job state) for an inquiry
    candidate (eligible screening; inquiry readiness is separate from profit readiness, so a
    candidate without CoC or price confirmation still qualifies while the hard price/mileage
    rules still apply), and by the bounded reconciliation sweep. It reads every input with
    ``inquiries_repo.read_readiness_inputs``, decides with ``domain.inquiries
    .evaluate_inquiry_readiness`` and records the decision (``record_readiness``). It reserves
    (quota debit + immutable binding), queues and enqueues the send job ONLY when
    ``SELLER_INQUIRY_MODE=automatic``, the settings kill switch is off, the domain decision is
    ``inquiry_ready`` and may reserve now (controls row automatic and unpaused, the standing
    authorization effective, the sender binding verified, recipient and language verified, no
    duplicate or suppression), the sender binding is the configured provider's and the listing
    is NOT fixture lineage. Otherwise the readiness stays recorded (visible, informational) and
    the job finishes with a typed reason. Waiting conditions (rolling caps, seller cooldown) are
    not failures: the job is released (no attempt consumed) until
    ``inquiries_repo.next_window_at`` says the window frees. There is no approval state and no
    approval wait anywhere: the bounded standing authorization is the only authority.

``seller_inquiry_send`` (`handle_seller_inquiry_send`)
    Revalidates immediately before transmission through ``inquiries_repo.dispatch`` (the
    domain preflight plus the database guards) and commits the send intent BEFORE any I/O:

    - ``outlook_local`` (default route): ``send_intents_repo.dispatch_outlook`` publishes the
      intent (the committed running attempt) for the bound mailbox's desktop worker, only while
      that worker's heartbeat is fresh; a stale or missing worker holds the job (released with
      a bounded delay and a visible reason). The job completes in the SAME transaction as the
      intent, so it can never retry into a second intent.
    - ``gmail_api`` / ``microsoft_graph``: the attempt is committed, the message (rebuilt by
      ``mime_builder`` from the stored, immutable rendering) is handed to the provider OUTSIDE
      any transaction (`InquiryRuntime.api_provider`: the gated provider with the
      ``BindingTokenProvider`` and a live kill-switch probe), then ``record_outcome`` stores it.
      A timeout or any ambiguity is ``uncertain``: the job is blocked with
      ``EMAIL_DELIVERY_UNCERTAIN`` and the inquiry waits for reconciliation; it is never resent.
      A proven pre-submission failure follows ``domain.inquiries.should_retry`` (the next run
      calls the guarded ``inquiries_repo.retry`` first); a definite rejection is final.

    A paused workspace (kill switch, mode) holds a queued inquiry without consuming attempts; after
    `InquiryRuntimeOptions.max_send_hold` the dispatch preflight records the suppression (its quota
    debit is released; ``resume`` re-qualifies it). Fixture lineage is never dispatched.

``seller_inquiry_reconcile`` (`handle_seller_inquiry_reconcile`)
    Resolves an ``uncertain`` attempt with positive evidence only: a Message-ID-linked,
    unquarantined inbound message stored for the inquiry (``correlated_inbound``; a reply that
    arrived while the inquiry was still ``sending`` counts), the local worker's stored reports
    (``outlook_local``: Sent Items, or a definitive refusal of every intent) or the provider
    search by Message-ID (``gmail_api``). An empty Sent Items or an empty search never releases
    the reservation: the inquiry stays ``uncertain`` and is never resent. A proven
    non-submission (``failed_definite``) gets a new send job, whose guarded retry decides.
    The reconciliation pass schedules these jobs for uncertain attempts older than a bounded age.

Nothing here logs an address, a message body or a token; job results carry ids and codes only.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict

from suv_deals.clock import Clock, SystemClock, ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import (
    EligibilityState,
    EmailProviderKind,
    InquiryReadiness,
    InquiryState,
    JobState,
    JobType,
)
from suv_deals.domain.inquiries import (
    PRE_RESERVATION_STATES,
    InquiryReadinessDecision,
    ReconciliationEvidence,
    SendAttemptOutcome,
    SenderBinding,
    evaluate_inquiry_readiness,
    requires_message_approval,
)
from suv_deals.errors import (
    AppError,
    EmailDeliveryUncertain,
    Forbidden,
    InsufficientData,
    NotFound,
    RateLimited,
    ValidationFailed,
    VersionConflict,
)
from suv_deals.integrations.email_providers.base import (
    ReconcileOutcome,
    ReconcileProviderUnavailable,
    ReconcileWindow,
    SendAccepted,
    SendDefiniteFailure,
    SenderProvider,
    SendUncertain,
    TokenProvider,
    UncertainReason,
    reconciliation_evidence,
)
from suv_deals.integrations.email_providers.gmail_api import GmailApiProvider, GmailApiSettings
from suv_deals.integrations.email_providers.microsoft_graph import GraphMailProvider, GraphSettings
from suv_deals.integrations.email_providers.outlook_local import (
    DEFAULT_INTENT_TTL,
    HEARTBEAT_STALE_AFTER,
    OutlookLocalProvider,
)
from suv_deals.integrations.secret_box import SecretBox
from suv_deals.integrations.seller_email import (
    API_PROVIDERS,
    GatedSenderProvider,
    ProviderDependencies,
    SenderSetupError,
    build_sender_provider,
    default_http_client,
    secret_reference_problems,
)
from suv_deals.persistence import (
    inquiries_repo,
    jobs,
    listings_repo,
    sellers_repo,
    send_intents_repo,
    sender_bindings_repo,
)
from suv_deals.persistence.database import Conn, Database, fetch_all, fetch_one
from suv_deals.persistence.errors_map import LeaseLost, mapped_errors
from suv_deals.persistence.inquiries_repo import (
    AttemptLease,
    AttemptRecord,
    DispatchResult,
    InquiryControls,
    InquiryRecord,
    ReadinessSnapshot,
)
from suv_deals.persistence.listings_repo import ListingRecord
from suv_deals.persistence.mail_workers_repo import HEALTH_ROW_HASH
from suv_deals.persistence.sender_bindings_repo import BindingTokenProvider, SenderBindingRecord
from suv_deals.persistence.transactions import job_unit_of_work, retry_transient, unit_of_work
from suv_deals.settings import Settings
from suv_deals.workers.runtime import (
    Disposition,
    JobExecution,
    JobOutcome,
    RuntimeContext,
    apply_disposition,
    backoff_delay,
)

logger = logging.getLogger(__name__)

PLAN_PREFIX: Final = "seller_inquiry.plan"
SEND_PREFIX: Final = "seller_inquiry.send"
RECONCILE_PREFIX: Final = "seller_inquiry.reconcile"
#: Screening states that make a listing an inquiry candidate (the hard price/mileage/SUV rules
#: hold). ``needs_facts`` (e.g. an ambiguous price basis) is not a candidate: it blocks dispatch.
CANDIDATE_STATES: Final = frozenset(
    {EligibilityState.ELIGIBLE_PRIMARY, EligibilityState.ELIGIBLE_MANUAL_PROFILE}
)
#: Listing availability that can never be asked about.
_UNAVAILABLE: Final = frozenset({"sold_claimed", "removed"})
#: Typed job-result outcomes (``result_reference.outcome``).
Outcome = Literal[
    "reserved",
    "readiness_recorded",
    "held",
    "waiting",
    "inquiry_exists",
    "seller_not_linked",
    "not_an_inquiry_candidate",
    "revision_superseded",
    "readiness_inputs_missing",
    "reservation_refused",
    "intent_published",
    "accepted",
    "uncertain",
    "failed_definite",
    "retry_refused",
    "cancelled",
    "suppressed",
    "not_queued",
    "fixture_lineage",
    "still_uncertain",
    "proven_not_submitted",
    "not_uncertain",
]


# =============================================================================================
# Runtime options and the provider factory
# =============================================================================================


@dataclass(frozen=True, slots=True)
class InquiryRuntimeOptions:
    """Engineering defaults of the inquiry workers (PROPOSED; not owner- or provider-approved)."""

    #: A queued inquiry of a paused workspace is re-checked this often (no attempt consumed).
    send_hold_delay: timedelta = timedelta(minutes=15)
    #: After this long on hold the dispatch preflight records the kill-switch suppression (the
    #: quota debit is released; the owner's ``resume`` re-qualifies the inquiry).
    max_send_hold: timedelta = timedelta(hours=24)
    #: Re-check delay while the bound desktop worker is offline (stale/missing heartbeat).
    worker_offline_delay: timedelta = timedelta(minutes=5)
    #: Validity of an ``outlook_local`` send intent (the attempt lease); the worker refuses later.
    intent_ttl: timedelta = DEFAULT_INTENT_TTL
    #: The heartbeat must be at most this old for an intent to be published.
    heartbeat_max_age: timedelta = HEARTBEAT_STALE_AFTER
    #: Reconciliation search window around the attempt.
    reconcile_lookback: timedelta = timedelta(hours=1)
    reconcile_lookahead: timedelta = timedelta(days=2)
    #: Priorities: plans below crawling, sends/reconciles above it.
    plan_priority: int = -5
    send_priority: int = 20
    reconcile_priority: int = 10
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if not timedelta(minutes=5) <= self.intent_ttl <= timedelta(hours=48):
            raise ValueError("intent_ttl must be between 5 minutes and 48 hours")
        if self.send_hold_delay <= timedelta(0) or self.worker_offline_delay <= timedelta(0):
            raise ValueError("hold delays must be positive")


KillSwitchProbe = Callable[[], Awaitable[bool]]
TokenProviderFactory = Callable[[UUID, SenderBindingRecord], TokenProvider]


def configured_provider(settings: Settings) -> EmailProviderKind:
    """``SELLER_EMAIL_PROVIDER``; unset means the owner's default route ``outlook_local``."""
    value = settings.seller_email_provider
    return EmailProviderKind(value) if value else EmailProviderKind.OUTLOOK_LOCAL


def automatic_sending_enabled(settings: Settings) -> bool:
    """Process-level switches: ``SELLER_INQUIRY_MODE=automatic`` and the settings kill switch off.

    The workspace controls row (mode, kill switch, caps) is checked separately by the domain and
    the database guards; both must allow sending.
    """
    return settings.seller_inquiry_mode == "automatic" and not settings.seller_inquiry_kill_switch


@dataclass(slots=True)
class InquiryRuntime:
    """Provider wiring of the inquiry workers (`workers.runtime.build_runtime` attaches one).

    Nothing network-related is constructed eagerly: the HTTP client of an API provider is created
    on first use, and only when ``SELLER_EMAIL_PROVIDER`` names that provider (a send also needs
    automatic mode; `build_sender_provider` refuses otherwise). ``outlook_local`` needs no client:
    the backend only commits intents for the desktop worker.
    """

    settings: Settings
    db: Database
    clock: Clock = field(default_factory=SystemClock)
    options: InquiryRuntimeOptions = field(default_factory=InquiryRuntimeOptions)
    #: Injected HTTP client for API providers (tests: httpx.MockTransport). ``None``: lazily built.
    http_client: httpx.AsyncClient | None = None
    #: Server-side secret box for sealed OAuth grants (``None``: built from settings on demand).
    secret_box: SecretBox | None = None
    #: Optional token provider factory (tests, other secret stores). Default: BindingTokenProvider.
    token_provider_factory: TokenProviderFactory | None = None
    _owned_http: httpx.AsyncClient | None = None
    _tokens: dict[tuple[UUID, UUID], TokenProvider] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Settings, *, db: Database, clock: Clock | None = None) -> InquiryRuntime:
        return cls(settings=settings, db=db, clock=clock or SystemClock())

    @property
    def provider(self) -> EmailProviderKind:
        return configured_provider(self.settings)

    def _http(self) -> httpx.AsyncClient:
        if self.http_client is not None:
            return self.http_client
        if self._owned_http is None:
            self._owned_http = default_http_client()
        return self._owned_http

    async def aclose(self) -> None:
        owned, self._owned_http = self._owned_http, None
        if owned is not None:
            await owned.aclose()

    def kill_switch_probe(self, workspace_id: UUID) -> KillSwitchProbe:
        """Live re-read of the kill switch right before a provider call (fails closed)."""

        async def probe() -> bool:
            if not automatic_sending_enabled(self.settings):
                return True
            async with self.db.transaction(workspace_id=workspace_id) as conn:
                row = await fetch_one(
                    conn,
                    "select kill_switch, mode from app.seller_inquiry_controls where workspace_id = %(ws)s",
                    {"ws": workspace_id},
                )
            return row is None or bool(row["kill_switch"]) or row["mode"] != "automatic"

        return probe

    def token_provider(self, workspace_id: UUID, binding: SenderBindingRecord) -> TokenProvider:
        """The server-side token provider of an API binding (never logs or returns the token)."""
        key = (workspace_id, binding.id)
        cached = self._tokens.get(key)
        if cached is not None:
            return cached
        if self.token_provider_factory is not None:
            provider = self.token_provider_factory(workspace_id, binding)
        else:
            reference = self.settings.seller_email_oauth_secret_reference
            problems = secret_reference_problems(reference)
            if problems or reference is None:
                raise SenderSetupError("the sender secret reference is not configured", problems)
            box = self.secret_box or SecretBox.from_settings(self.settings)
            provider = BindingTokenProvider(
                db=self.db,
                workspace_id=workspace_id,
                reference=reference,
                box=box,
                http=self._http(),
                clock=self.clock,
            )
        self._tokens[key] = provider
        return provider

    def api_provider(
        self, workspace_id: UUID, binding: SenderBindingRecord, controls: InquiryControls
    ) -> GatedSenderProvider:
        """The verified, gated sending provider for an API binding (`SenderSetupError` lists every
        unmet technical prerequisite; there is no approval prerequisite)."""
        if binding.provider not in API_PROVIDERS:
            raise SenderSetupError("not an API provider binding", ["PROVIDER_NOT_API"])
        sender = sender_bindings_repo.sender_status(
            binding, mode=controls.mode, kill_switch=controls.kill_switch
        )
        deps = ProviderDependencies(
            token_provider=self.token_provider(workspace_id, binding),
            http_client=self._http(),
            clock=self.clock,
            kill_switch_probe=self.kill_switch_probe(workspace_id),
        )
        return build_sender_provider(self.settings, sender=sender, deps=deps)

    def reconcile_provider(self, workspace_id: UUID, binding: SenderBindingRecord) -> SenderProvider:
        """A read-only provider for reconciliation (works while paused: it never sends)."""
        if binding.provider != configured_provider(self.settings):
            raise SenderSetupError("the bound provider is not the configured one", ["PROVIDER_MISMATCH"])
        tokens = self.token_provider(workspace_id, binding)
        if binding.provider == EmailProviderKind.GMAIL_API:
            return GmailApiProvider(
                binding=binding.sender_binding(),
                token_provider=tokens,
                http=self._http(),
                clock=self.clock,
                settings=GmailApiSettings(),
            )
        if binding.provider == EmailProviderKind.MICROSOFT_GRAPH:
            return GraphMailProvider(
                binding=binding.sender_binding(),
                token_provider=tokens,
                http=self._http(),
                clock=self.clock,
                settings=GraphSettings(),
            )
        raise SenderSetupError("not an API provider binding", ["PROVIDER_NOT_API"])


def inquiry_runtime(ctx: RuntimeContext) -> InquiryRuntime:
    """The context's inquiry runtime (attached once by `build_runtime`; built lazily otherwise)."""
    attached = ctx.inquiry_runtime
    if attached is None:
        attached = InquiryRuntime.from_settings(ctx.settings, db=ctx.db, clock=ctx.clock)
        ctx.inquiry_runtime = attached
        ctx.add_closer(attached.aclose)
    return attached


# =============================================================================================
# Enqueue helpers (one plan per listing revision; one send per attempt; bounded reconciles)
# =============================================================================================


def plan_dedup_key(listing_id: UUID, revision_id: UUID, suffix: str | None = None) -> str:
    base = f"{PLAN_PREFIX}:{listing_id}:{revision_id}"
    return base if suffix is None else f"{base}:{suffix}"


def send_dedup_key(inquiry_id: UUID, attempt_number: int) -> str:
    return f"{SEND_PREFIX}:{inquiry_id}:{attempt_number}"


def reconcile_dedup_key(inquiry_id: UUID, bucket: str) -> str:
    return f"{RECONCILE_PREFIX}:{inquiry_id}:{bucket}"


async def _job_exists(
    conn: Conn, actor: ActorContext, job_type: JobType, dedup_key: str, listing_id: UUID | None
) -> bool:
    """Whether ANY job (whatever its state) already carries this business key."""
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select 1 as found from ops.jobs where workspace_id = %(ws)s and job_type = %(type)s"
            " and dedup_key = %(key)s and (%(listing)s::uuid is null or listing_id = %(listing)s) limit 1",
            {"ws": actor.workspace_id, "type": job_type.value, "key": dedup_key, "listing": listing_id},
        )
    return row is not None


async def enqueue_plan_job(
    conn: Conn,
    actor: ActorContext,
    *,
    listing_id: UUID,
    revision_id: UUID,
    reason: str,
    suffix: str | None = None,
    options: InquiryRuntimeOptions | None = None,
) -> UUID | None:
    """ONE ``seller_inquiry_plan`` job per listing revision (and ``suffix``), whatever earlier
    jobs of that key ended as. Returns the new job id, or ``None`` when it was planned already.
    Runs in the caller's transaction (e.g. the valuation commit)."""
    opts = options or InquiryRuntimeOptions()
    key = plan_dedup_key(listing_id, revision_id, suffix)
    if await _job_exists(conn, actor, JobType.SELLER_INQUIRY_PLAN, key, listing_id):
        return None
    job_id, created = await jobs.enqueue(
        conn,
        actor,
        jobs.JobSpec(
            job_type=JobType.SELLER_INQUIRY_PLAN,
            dedup_key=key,
            payload={"listing_id": str(listing_id), "revision_id": str(revision_id), "reason": reason[:60]},
            listing_id=listing_id,
            priority=opts.plan_priority,
            max_attempts=opts.max_attempts,
        ),
    )
    return job_id if created else None


async def enqueue_send_job(
    conn: Conn,
    actor: ActorContext,
    *,
    inquiry_id: UUID,
    listing_id: UUID,
    attempt_number: int,
    options: InquiryRuntimeOptions | None = None,
) -> UUID | None:
    """The send job of one transmission attempt (``attempt_number`` 1..3)."""
    opts = options or InquiryRuntimeOptions()
    key = send_dedup_key(inquiry_id, attempt_number)
    if await _job_exists(conn, actor, JobType.SELLER_INQUIRY_SEND, key, listing_id):
        return None
    job_id, created = await jobs.enqueue(
        conn,
        actor,
        jobs.JobSpec(
            job_type=JobType.SELLER_INQUIRY_SEND,
            dedup_key=key,
            payload={"inquiry_id": str(inquiry_id), "attempt_number": attempt_number},
            listing_id=listing_id,
            priority=opts.send_priority,
            max_attempts=opts.max_attempts,
        ),
    )
    return job_id if created else None


async def enqueue_reconcile_job(
    conn: Conn,
    actor: ActorContext,
    *,
    inquiry_id: UUID,
    listing_id: UUID,
    bucket: str,
    options: InquiryRuntimeOptions | None = None,
) -> UUID | None:
    """One reconcile job per inquiry and time bucket (the reconciliation pass picks the bucket)."""
    opts = options or InquiryRuntimeOptions()
    key = reconcile_dedup_key(inquiry_id, bucket)
    if await _job_exists(conn, actor, JobType.SELLER_INQUIRY_RECONCILE, key, listing_id):
        return None
    job_id, created = await jobs.enqueue(
        conn,
        actor,
        jobs.JobSpec(
            job_type=JobType.SELLER_INQUIRY_RECONCILE,
            dedup_key=key,
            payload={"inquiry_id": str(inquiry_id)},
            listing_id=listing_id,
            priority=opts.reconcile_priority,
            max_attempts=opts.max_attempts,
        ),
    )
    return job_id if created else None


def is_inquiry_candidate_state(state: EligibilityState | str | None) -> bool:
    """A screening state that makes a listing an inquiry candidate (`CANDIDATE_STATES`)."""
    return state is not None and str(state) in CANDIDATE_STATES


def is_inquiry_candidate(listing: ListingRecord) -> bool:
    """Eligible screening of an available, unquarantined listing with a current revision."""
    return (
        listing.eligibility_state in CANDIDATE_STATES
        and not listing.quarantined
        and not listing.identity_conflict
        and listing.current_revision_id is not None
        and listing.availability.value not in _UNAVAILABLE
    )


# =============================================================================================
# Shared job helpers
# =============================================================================================


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    listing_id: UUID | None = None
    revision_id: UUID | None = None
    inquiry_id: UUID | None = None
    reply_id: UUID | None = None
    attempt_number: int | None = None


def _result(outcome: Outcome, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"outcome": outcome}
    for key, value in extra.items():
        if value is None:
            continue
        if isinstance(value, UUID):
            data[key] = str(value)
        elif isinstance(value, InquiryState | InquiryReadiness):
            data[key] = value.value
        elif isinstance(value, list | tuple):
            data[key] = [str(v) for v in value][:20]
        else:
            data[key] = value
    return data


async def _complete(ctx: RuntimeContext, execution: JobExecution, result: dict[str, Any]) -> JobOutcome:
    """Finish the job with a typed result (nothing else is written)."""

    async def commit() -> None:
        async with job_unit_of_work(ctx.db, execution.job) as (conn, _locked):
            await apply_disposition(conn, execution.job, Disposition.complete(result))

    await retry_transient(commit)
    return JobOutcome(state=JobState.SUCCEEDED, code=str(result.get("outcome")), details=result)


async def _release_in(
    conn: Conn,
    execution: JobExecution,
    *,
    available_at: datetime | timedelta,
    code: str,
    detail: str | None = None,
) -> None:
    """Return the job to ``queued`` WITHOUT consuming an attempt (a waiting condition)."""
    await jobs.release(
        conn, execution.job, available_at=available_at, code=code, detail=detail, actor=execution.actor
    )


async def _release(
    ctx: RuntimeContext,
    execution: JobExecution,
    *,
    available_at: datetime | timedelta,
    code: str,
    detail: str | None = None,
) -> JobOutcome:
    async def commit() -> None:
        async with job_unit_of_work(ctx.db, execution.job) as (conn, _locked):
            await _release_in(conn, execution, available_at=available_at, code=code, detail=detail)

    await retry_transient(commit)
    logger.info("inquiry job held", extra={"job_type": execution.job.job_type.value, "code": code})
    return JobOutcome(state=JobState.QUEUED, code=code)


def _payload(execution: JobExecution) -> _Payload:
    return _Payload.model_validate(execution.job.payload)


def _codes(decision: InquiryReadinessDecision) -> list[str]:
    from suv_deals.domain.inquiries import ReadinessSeverity  # noqa: PLC0415 - local enum use

    blocking = [r.code.value for r in decision.reasons if r.severity != ReadinessSeverity.INFO]
    return list(dict.fromkeys(blocking))[:20]


def _hold_code(reasons: Sequence[str], default: str) -> str:
    for code in reasons:
        if code in ("RATE_CAP_REACHED", "SELLER_COOLDOWN", "KILL_SWITCH_ACTIVE", "INQUIRIES_PAUSED"):
            return f"INQUIRY_WAIT_{code}"[:80]
    return default


# =============================================================================================
# seller_inquiry_plan
# =============================================================================================


def reservation_refusal(
    settings: Settings,
    listing: ListingRecord,
    snapshot: ReadinessSnapshot,
    decision: InquiryReadinessDecision,
) -> str | None:
    """Why this run records readiness only (``None``: the reservation may be attempted).

    Process-level prerequisites (mode, kill switch, the owner's explicit approval setting that
    DISABLES automatic sending instead of adding an approval wait), the configured send route and
    fixture lineage. The domain decision covers controls, authorization, sender verification,
    recipient, language, duplicates, suppressions, caps and cooldown.
    """
    if listing.is_fixture:
        return "fixture_lineage"
    if settings.seller_inquiry_mode != "automatic":
        return "seller_inquiry_mode_not_automatic"
    if settings.seller_inquiry_kill_switch:
        return "settings_kill_switch_active"
    if requires_message_approval(settings):
        return "automatic_sending_disabled_by_owner_setting"
    binding = snapshot.sender_binding
    if binding is None or binding.provider != configured_provider(settings):
        return "sender_provider_not_configured"
    if decision.readiness != InquiryReadiness.INQUIRY_READY:
        return f"readiness_{decision.readiness.value}"
    return None


async def _read_plan(
    ctx: RuntimeContext, actor: ActorContext, payload: _Payload, listing_id: UUID
) -> tuple[ListingRecord, ReadinessSnapshot | None, dict[str, Any] | None]:
    async with unit_of_work(ctx.db, actor) as conn:
        listing = await listings_repo.get_listing(conn, actor, listing_id)
        if payload.revision_id is not None and listing.current_revision_id != payload.revision_id:
            return listing, None, _result("revision_superseded", listing_id=listing_id)
        if not is_inquiry_candidate(listing):
            return (
                listing,
                None,
                _result(
                    "not_an_inquiry_candidate",
                    listing_id=listing_id,
                    eligibility=None
                    if listing.eligibility_state is None
                    else listing.eligibility_state.value,
                ),
            )
        contact = await sellers_repo.current_contact(conn, actor, listing_id)
        if contact is None:
            # No exact-listing seller/contact evidence yet: there is nobody to ask (spec 37.3).
            return listing, None, _result("seller_not_linked", listing_id=listing_id)
        binding = await sender_bindings_repo.active_binding(
            conn, actor, provider=configured_provider(ctx.settings)
        )
        try:
            snapshot = await inquiries_repo.read_readiness_inputs(
                conn,
                actor,
                listing_id=listing_id,
                seller_entity_id=contact.seller_entity_id,
                sender_binding_id=None if binding is None else binding.id,
            )
        except (InsufficientData, NotFound) as exc:
            return (
                listing,
                None,
                _result("readiness_inputs_missing", listing_id=listing_id, code=exc.code.value),
            )
    return listing, snapshot, None


async def handle_seller_inquiry_plan(ctx: RuntimeContext, execution: JobExecution) -> JobOutcome:
    """Readiness, then (only when every prerequisite holds) reservation + queue + send job."""
    job, actor = execution.job, execution.actor
    payload = _payload(execution)
    listing_id = job.listing_id or payload.listing_id
    if listing_id is None:
        raise ValidationFailed("a seller inquiry plan job needs its listing")
    listing, snapshot, finished = await _read_plan(ctx, actor, payload, listing_id)
    if finished is not None or snapshot is None:
        return await _complete(ctx, execution, finished or _result("readiness_inputs_missing"))
    decision = evaluate_inquiry_readiness(snapshot.inputs)
    refusal = reservation_refusal(ctx.settings, listing, snapshot, decision)
    rt = inquiry_runtime(ctx)

    async def commit() -> JobOutcome:
        execution.check_lease()
        async with job_unit_of_work(ctx.db, job) as (conn, _locked):
            return await _plan_commit(
                conn,
                execution,
                rt=rt,
                listing=listing,
                snapshot=snapshot,
                decision=decision,
                refusal=refusal,
            )

    outcome = await retry_transient(commit)
    logger.info(
        "seller inquiry planned",
        extra={"outcome": outcome.code, "readiness": decision.readiness.value},
    )
    return outcome


async def _plan_commit(
    conn: Conn,
    execution: JobExecution,
    *,
    rt: InquiryRuntime,
    listing: ListingRecord,
    snapshot: ReadinessSnapshot,
    decision: InquiryReadinessDecision,
    refusal: str | None,
) -> JobOutcome:
    job, actor = execution.job, execution.actor

    async def done(result: dict[str, Any]) -> JobOutcome:
        await apply_disposition(conn, job, Disposition.complete(result))
        return JobOutcome(state=JobState.SUCCEEDED, code=str(result["outcome"]), details=result)

    try:
        record = await inquiries_repo.open_inquiry(
            conn, actor, snapshot.identity, qualification_listing_id=listing.id
        )
    except VersionConflict as exc:
        return await done(
            _result("inquiry_exists", listing_id=listing.id, reason=(exc.details or {}).get("reason"))
        )
    if record.state not in PRE_RESERVATION_STATES and record.state != InquiryState.CANCELLED:
        # Reserved, queued, (possibly) sent, suppressed...: the one inquiry of this pair exists.
        return await done(_result("inquiry_exists", inquiry_id=record.id, state=record.state))
    record = await inquiries_repo.record_readiness(conn, actor, record.id, decision)
    codes = _codes(decision)
    base = {"inquiry_id": record.id, "readiness": decision.readiness, "state": record.state, "reasons": codes}
    if refusal is not None:
        return await done(_result("readiness_recorded", reservation=refusal, **base))
    if not decision.can_reserve_now:
        return await _hold_plan(conn, execution, record, codes, base)
    try:
        prepared = inquiries_repo.prepare_binding(snapshot, decision, inquiry_id=record.id)
    except ValidationFailed as exc:
        return await done(
            _result("reservation_refused", reservation="binding_refused", problems=_problems(exc), **base)
        )
    try:
        async with conn.transaction():  # savepoint: a refused reservation keeps the readiness
            await inquiries_repo.reserve(conn, actor, record.id, decision=decision, prepared=prepared)
            queued = await inquiries_repo.queue(conn, actor, record.id)
    except RateLimited:
        return await _hold_plan(conn, execution, record, codes, base)
    except (VersionConflict, ValidationFailed, Forbidden) as exc:
        details = exc.details or {}
        reason = details.get("reason") or (_problems(exc) or [exc.code.value])[0]
        return await done(_result("reservation_refused", reservation=str(reason)[:80], **base))
    send_job = await enqueue_send_job(
        conn, actor, inquiry_id=queued.id, listing_id=listing.id, attempt_number=1, options=rt.options
    )
    return await done(
        _result(
            "reserved",
            inquiry_id=queued.id,
            readiness=decision.readiness,
            state=queued.state,
            send_job_id=send_job,
            language=None if queued.language is None else queued.language.value,
        )
    )


def _problems(exc: AppError) -> list[str]:
    problems = (exc.details or {}).get("problems")
    return [str(p)[:80] for p in problems][:10] if isinstance(problems, list) else []


async def _hold_plan(
    conn: Conn,
    execution: JobExecution,
    record: InquiryRecord,
    codes: Sequence[str],
    base: Mapping[str, Any],
) -> JobOutcome:
    """A qualified inquiry that must wait: release until the window frees (no attempt consumed),
    or finish visibly when no end is known (owner pause / kill switch / a cap set to 0)."""
    actor = execution.actor
    window = await inquiries_repo.next_window_at(
        conn, actor, seller_entity_id=record.seller_entity_id, exclude_inquiry_id=record.id
    )
    if window.next_at is not None:
        code = _hold_code(codes, "INQUIRY_WAIT_WINDOW")
        await _release_in(
            conn, execution, available_at=window.next_at, code=code, detail="waiting for the rolling window"
        )
        return JobOutcome(state=JobState.QUEUED, code=code, details={"next_at": window.next_at.isoformat()})
    result = _result("held", **base)
    await apply_disposition(conn, execution.job, Disposition.complete(result))
    return JobOutcome(state=JobState.SUCCEEDED, code="held", details=result)


# =============================================================================================
# seller_inquiry_send
# =============================================================================================


@dataclass(frozen=True, slots=True)
class _SendReads:
    record: InquiryRecord
    listing: ListingRecord
    controls: InquiryControls | None
    binding: SenderBindingRecord | None
    now: datetime


async def _read_send(ctx: RuntimeContext, actor: ActorContext, inquiry_id: UUID) -> _SendReads:
    async with unit_of_work(ctx.db, actor) as conn:
        record = await inquiries_repo.get_inquiry(conn, actor, inquiry_id)
        listing = await listings_repo.get_listing(conn, actor, record.qualification_listing_id)
        controls = await inquiries_repo.get_controls(conn, actor)
        binding = (
            None
            if record.sender_binding_id is None
            else await sender_bindings_repo.get_binding(conn, actor, record.sender_binding_id)
        )
        async with mapped_errors():
            row = await fetch_one(conn, "select clock_timestamp() as now")
    assert row is not None
    return _SendReads(record, listing, controls, binding, ensure_utc(row["now"]))


@dataclass(frozen=True, slots=True)
class MailboxState:
    """The bound desktop worker of a sender (``outlook_local``) and its heartbeat."""

    mailbox_binding_id: UUID
    heartbeat_at: datetime | None
    outlook_connected: bool
    mailbox_sync_ok: bool
    now: datetime

    def fresh(self, max_age: timedelta) -> bool:
        return (
            self.heartbeat_at is not None
            and self.now - self.heartbeat_at <= max_age
            and self.outlook_connected
            and self.mailbox_sync_ok
        )


async def mailbox_state(conn: Conn, actor: ActorContext, sender_binding_id: UUID) -> MailboxState | None:
    """The active mailbox-worker binding of the sender and its worker-level health row."""
    async with mapped_errors():
        row = await fetch_one(
            conn,
            "select m.id, c.heartbeat_at, coalesce(c.outlook_connected, false) as outlook_connected,"
            " coalesce(c.mailbox_sync_ok, false) as mailbox_sync_ok, clock_timestamp() as now"
            " from ops.mail_worker_bindings m"
            " left join ops.mail_worker_checkpoints c on c.workspace_id = m.workspace_id"
            "  and c.mailbox_binding_id = m.id and c.store_id_hash = %(hash)s and c.folder_id_hash = %(hash)s"
            " where m.workspace_id = %(ws)s and m.sender_binding_id = %(sender)s and m.state = 'active'"
            " order by m.created_at desc, m.id limit 1",
            {"ws": actor.workspace_id, "sender": sender_binding_id, "hash": HEALTH_ROW_HASH},
        )
    if row is None:
        return None
    beat = row["heartbeat_at"]
    return MailboxState(
        mailbox_binding_id=row["id"],
        heartbeat_at=None if beat is None else ensure_utc(beat),
        outlook_connected=bool(row["outlook_connected"]),
        mailbox_sync_ok=bool(row["mailbox_sync_ok"]),
        now=ensure_utc(row["now"]),
    )


def _paused(settings: Settings, controls: InquiryControls | None) -> tuple[bool, bool]:
    """``(held, database_paused)``: any switch stops untransmitted work; only a database-level
    pause may later be recorded as a suppression by the dispatch preflight."""
    database = controls is None or controls.kill_switch or controls.mode != "automatic"
    return database or not automatic_sending_enabled(settings), database


async def handle_seller_inquiry_send(ctx: RuntimeContext, execution: JobExecution) -> JobOutcome:
    actor = execution.actor
    rt = inquiry_runtime(ctx)
    payload = _payload(execution)
    if payload.inquiry_id is None:
        raise ValidationFailed("a seller inquiry send job needs its inquiry")
    inquiry_id = payload.inquiry_id
    reads = await _read_send(ctx, actor, inquiry_id)
    record = reads.record
    if record.state not in (InquiryState.QUEUED, InquiryState.FAILED_DEFINITE):
        # Sending, uncertain, accepted, cancelled...: never a second intent or transmission.
        return await _complete(
            ctx, execution, _result("not_queued", inquiry_id=inquiry_id, state=record.state)
        )
    if reads.listing.is_fixture:
        return await _close_fixture(ctx, execution, record)
    held, database_paused = _paused(ctx.settings, reads.controls)
    waited = reads.now - (record.queued_at or record.reserved_at or reads.now)
    if held and (not database_paused or waited < rt.options.max_send_hold):
        return await _release(
            ctx,
            execution,
            available_at=rt.options.send_hold_delay,
            code="SEND_HELD_PAUSED",
            detail="seller inquiries are paused or not in automatic mode; nothing was transmitted",
        )
    if record.state == InquiryState.FAILED_DEFINITE:
        retried = await _guarded_retry(ctx, execution, record)
        if retried is not None:
            return retried
    provider = record.sender_provider
    if provider is None or reads.binding is None:
        return await _dispatch_only(ctx, execution, inquiry_id)  # the preflight cancels it as stale
    if provider == EmailProviderKind.OUTLOOK_LOCAL:
        return await _send_outlook(ctx, execution, rt, record)
    if provider in API_PROVIDERS:
        return await _send_api(ctx, execution, rt, reads)
    raise ValidationFailed("unknown sender provider")  # pragma: no cover - enum is exhaustive


async def _close_fixture(ctx: RuntimeContext, execution: JobExecution, record: InquiryRecord) -> JobOutcome:
    """Fixture lineage is never transmitted: an untransmitted queued inquiry is cancelled."""

    async def commit() -> JobOutcome:
        async with job_unit_of_work(ctx.db, execution.job) as (conn, _locked):
            state = record.state
            if record.state in sellers_repo.CANCELLABLE_STATES:
                try:
                    async with conn.transaction():
                        state = (
                            await inquiries_repo.cancel_inquiry(
                                conn, execution.actor, record.id, reasons=["FIXTURE_LINEAGE"]
                            )
                        ).state
                except VersionConflict:
                    state = record.state
            result = _result("fixture_lineage", inquiry_id=record.id, state=state)
            await apply_disposition(conn, execution.job, Disposition.complete(result))
            return JobOutcome(state=JobState.SUCCEEDED, code="fixture_lineage", details=result)

    return await retry_transient(commit)


async def _guarded_retry(
    ctx: RuntimeContext, execution: JobExecution, record: InquiryRecord
) -> JobOutcome | None:
    """``failed_definite -> queued`` only through ``domain.inquiries.should_retry`` (the guarded
    repository retry: proven pre-submission failure, same account, attempts remaining).
    ``None``: re-queued, continue with the dispatch."""

    async def commit() -> JobOutcome | None:
        async with job_unit_of_work(ctx.db, execution.job) as (conn, _locked):
            try:
                async with conn.transaction():
                    await inquiries_repo.retry(conn, execution.actor, record.id)
            except (EmailDeliveryUncertain, VersionConflict) as exc:
                result = _result(
                    "retry_refused",
                    inquiry_id=record.id,
                    reasons=(exc.details or {}).get("reasons") or [exc.code.value],
                )
                await apply_disposition(conn, execution.job, Disposition.complete(result))
                return JobOutcome(state=JobState.SUCCEEDED, code="retry_refused", details=result)
            return None

    return await retry_transient(commit)


def _dispatch_disposition(result: DispatchResult, options: InquiryRuntimeOptions) -> Disposition | None:
    """How a non-proceed dispatch finishes the job (``None``: hold, released by the caller)."""
    if result.outcome in ("cancelled", "suppressed"):
        return Disposition.complete(
            _result(
                "cancelled" if result.outcome == "cancelled" else "suppressed",
                inquiry_id=result.inquiry_id,
                reasons=list(result.decision.reasons),
            )
        )
    del options
    return None


async def _apply_hold(
    conn: Conn, execution: JobExecution, result: DispatchResult, options: InquiryRuntimeOptions
) -> JobOutcome:
    reasons = list(result.decision.reasons)
    if "NOT_QUEUED" in reasons:
        done = _result("not_queued", inquiry_id=result.inquiry_id)
        await apply_disposition(conn, execution.job, Disposition.complete(done))
        return JobOutcome(state=JobState.SUCCEEDED, code="not_queued", details=done)
    at: datetime | timedelta = result.next_attempt_at or options.send_hold_delay
    code = _hold_code(reasons, "SEND_HELD_PREFLIGHT")
    await _release_in(conn, execution, available_at=at, code=code, detail=", ".join(reasons)[:300] or None)
    return JobOutcome(state=JobState.QUEUED, code=code, details={"reasons": reasons[:20]})


async def _dispatch_only(ctx: RuntimeContext, execution: JobExecution, inquiry_id: UUID) -> JobOutcome:
    """An inquiry without a usable bound sender: the preflight records why (never transmits)."""
    job = execution.job
    lease = AttemptLease(owner=job.lease_owner, token=job.lease_token, expires_at=job.lease_expires_at)

    async def commit() -> JobOutcome:
        async with job_unit_of_work(ctx.db, job) as (conn, _locked):
            result = await inquiries_repo.dispatch(
                conn,
                execution.actor,
                inquiry_id,
                lease=lease,
                message_approval_required=requires_message_approval(ctx.settings),
                job_id=job.id,
            )
            if result.outcome == "proceed":  # pragma: no cover - an incomplete binding never proceeds
                raise ValidationFailed("an inquiry without a bound sender cannot be dispatched")
            disposition = _dispatch_disposition(result, inquiry_runtime(ctx).options)
            if disposition is None:
                return await _apply_hold(conn, execution, result, inquiry_runtime(ctx).options)
            await apply_disposition(conn, job, disposition)
            return JobOutcome(state=JobState.SUCCEEDED, code=result.outcome)

    return await retry_transient(commit)


async def _send_outlook(
    ctx: RuntimeContext, execution: JobExecution, rt: InquiryRuntime, record: InquiryRecord
) -> JobOutcome:
    """Publish the committed intent for the bound desktop worker (only while it is online)."""
    job, actor = execution.job, execution.actor
    assert record.sender_binding_id is not None
    async with unit_of_work(ctx.db, actor) as conn:
        box = await mailbox_state(conn, actor, record.sender_binding_id)
    if box is None:
        return await _release(
            ctx,
            execution,
            available_at=rt.options.worker_offline_delay,
            code="MAILBOX_WORKER_MISSING",
            detail="no active desktop mailbox worker is bound to the sender; nothing was published",
        )
    if not box.fresh(rt.options.heartbeat_max_age):
        return await _release(
            ctx,
            execution,
            available_at=rt.options.worker_offline_delay,
            code="WORKER_HEARTBEAT_STALE",
            detail="the desktop mailbox worker is offline (stale heartbeat); nothing was published",
        )

    async def commit() -> JobOutcome:
        execution.check_lease()
        async with job_unit_of_work(ctx.db, job) as (conn, _locked):
            dispatched = await send_intents_repo.dispatch_outlook(
                conn,
                actor,
                record.id,
                mailbox_binding_id=box.mailbox_binding_id,
                message_approval_required=requires_message_approval(ctx.settings),
                job_id=job.id,
                ttl=rt.options.intent_ttl,
            )
            result = dispatched.result
            if result.outcome == "proceed" and result.attempt is not None:
                # Completed in the SAME transaction as the intent: this job can never publish a
                # second intent. The worker's report (or the reaper) decides the outcome.
                done = _result(
                    "intent_published",
                    inquiry_id=record.id,
                    attempt_id=result.attempt.attempt_id,
                    mailbox_binding_id=box.mailbox_binding_id,
                )
                await apply_disposition(conn, job, Disposition.complete(done))
                return JobOutcome(state=JobState.SUCCEEDED, code="intent_published", details=done)
            disposition = _dispatch_disposition(result, rt.options)
            if disposition is None:
                return await _apply_hold(conn, execution, result, rt.options)
            await apply_disposition(conn, job, disposition)
            return JobOutcome(state=JobState.SUCCEEDED, code=result.outcome)

    return await retry_transient(commit)


async def _send_api(
    ctx: RuntimeContext, execution: JobExecution, rt: InquiryRuntime, reads: _SendReads
) -> JobOutcome:
    """Commit the attempt, call the provider outside any transaction, record the outcome."""
    job, actor = execution.job, execution.actor
    record, binding, controls = reads.record, reads.binding, reads.controls
    assert binding is not None and controls is not None
    if rt.provider != binding.provider:
        return await _block(
            ctx, execution, "SENDER_PROVIDER_NOT_CONFIGURED", "the bound provider is not configured"
        )
    try:
        provider = rt.api_provider(actor.workspace_id, binding, controls)
    except SenderSetupError as exc:
        return await _block(ctx, execution, "SENDER_SETUP_INCOMPLETE", ", ".join(exc.problems)[:300])
    lease = AttemptLease(owner=job.lease_owner, token=job.lease_token, expires_at=job.lease_expires_at)

    async def commit_intent() -> DispatchResult | JobOutcome:
        execution.check_lease()
        async with job_unit_of_work(ctx.db, job) as (conn, _locked):
            result = await inquiries_repo.dispatch(
                conn,
                actor,
                record.id,
                lease=lease,
                message_approval_required=requires_message_approval(ctx.settings),
                job_id=job.id,
            )
            if result.outcome == "proceed":
                return result  # the running attempt (send intent) commits with this transaction
            disposition = _dispatch_disposition(result, rt.options)
            if disposition is None:
                return await _apply_hold(conn, execution, result, rt.options)
            await apply_disposition(conn, job, disposition)
            return JobOutcome(state=JobState.SUCCEEDED, code=result.outcome)

    committed = await retry_transient(commit_intent)
    if isinstance(committed, JobOutcome):
        return committed
    assert committed.attempt is not None and committed.message is not None
    attempt, message = committed.attempt, committed.message
    # External I/O: outside every transaction, after the intent is durable.
    try:
        outcome: SendAccepted | SendDefiniteFailure | SendUncertain = await provider.send(
            message,
            inquiry_id=record.id,
            attempt_id=attempt.attempt_id,
            idempotency_key=f"send-{attempt.attempt_id}",
        )
    except Exception:  # an unexpected provider failure may have happened after the hand-over
        logger.warning("provider send raised; recorded as uncertain", extra={"inquiry_id": str(record.id)})
        outcome = SendUncertain(
            provider=attempt.provider,
            inquiry_id=record.id,
            attempt_id=attempt.attempt_id,
            rfc_message_id=message.rfc_message_id,
            raw_sha256=message.raw_sha256,
            reason=UncertainReason.UNEXPECTED_ERROR,
        )
    return await _record_send(ctx, execution, attempt, outcome)


async def _record_send(
    ctx: RuntimeContext,
    execution: JobExecution,
    attempt: AttemptRecord,
    outcome: SendAccepted | SendDefiniteFailure | SendUncertain,
) -> JobOutcome:
    job, actor = execution.job, execution.actor

    async def commit() -> JobOutcome:
        async with job_unit_of_work(ctx.db, job) as (conn, _locked):
            recorded = await inquiries_repo.record_outcome(
                conn, actor, attempt_id=attempt.attempt_id, lease_token=attempt.lease_token, outcome=outcome
            )
            state = recorded.inquiry_state
            if state == InquiryState.ACCEPTED or recorded.attempt_outcome == SendAttemptOutcome.ACCEPTED:
                done = _result("accepted", inquiry_id=attempt.inquiry_id, attempt_id=attempt.attempt_id)
                await apply_disposition(conn, job, Disposition.complete(done))
                return JobOutcome(state=JobState.SUCCEEDED, code="accepted", details=done)
            if recorded.attempt_outcome == SendAttemptOutcome.PRE_SUBMISSION_FAILURE:
                # Proven never submitted: the next run applies the guarded retry (should_retry).
                retry_after = (
                    outcome.retry_after_seconds if isinstance(outcome, SendDefiniteFailure) else None
                )
                wait: timedelta = (
                    timedelta(seconds=retry_after) if retry_after else backoff_delay(job.attempts)
                )
                await apply_disposition(
                    conn, job, Disposition.retry("PRE_SUBMISSION_FAILURE", wait, "proven not submitted")
                )
                return JobOutcome(state=JobState.RETRY_WAIT, code="PRE_SUBMISSION_FAILURE")
            if recorded.attempt_outcome == SendAttemptOutcome.DEFINITE_REJECTION:
                done = _result(
                    "failed_definite", inquiry_id=attempt.inquiry_id, attempt_id=attempt.attempt_id
                )
                await apply_disposition(conn, job, Disposition.complete(done))
                return JobOutcome(state=JobState.SUCCEEDED, code="failed_definite", details=done)
            # Uncertain: the message may have left. Never resent; reconciliation decides.
            await apply_disposition(
                conn,
                job,
                Disposition.blocked(
                    jobs.EMAIL_DELIVERY_UNCERTAIN,
                    "the provider outcome is uncertain; reconcile, never resend",
                ),
            )
            return JobOutcome(state=JobState.BLOCKED, code=jobs.EMAIL_DELIVERY_UNCERTAIN)

    try:
        return await retry_transient(commit)
    except LeaseLost:
        # The job lease is gone, but the provider outcome is evidence and must never be lost: it
        # is recorded under the attempt's own fence; the reaper blocks the job.
        async with unit_of_work(ctx.db, actor) as conn:
            await inquiries_repo.record_outcome(
                conn, actor, attempt_id=attempt.attempt_id, lease_token=attempt.lease_token, outcome=outcome
            )
        raise


async def _block(ctx: RuntimeContext, execution: JobExecution, code: str, detail: str) -> JobOutcome:
    """A configuration problem that needs the operator (nothing was committed or sent)."""

    async def commit() -> None:
        async with job_unit_of_work(ctx.db, execution.job) as (conn, _locked):
            await apply_disposition(conn, execution.job, Disposition.blocked(code, detail))

    await retry_transient(commit)
    return JobOutcome(state=JobState.BLOCKED, code=code)


# =============================================================================================
# seller_inquiry_reconcile
# =============================================================================================

_INBOUND_SQL: Final = """
select r.id from app.seller_replies r
 where r.workspace_id = %(ws)s and r.inquiry_id = %(inquiry)s and not r.quarantined and r.header_linked
 order by r.ingested_at, r.id
"""


async def correlated_inbound_replies(conn: Conn, actor: ActorContext, inquiry_id: UUID) -> list[UUID]:
    """Unquarantined inbound messages linked by Message-ID to this inquiry (positive evidence that
    it was submitted, also when the reply arrived while the inquiry was still ``sending``)."""
    async with mapped_errors():
        rows = await fetch_all(conn, _INBOUND_SQL, {"ws": actor.workspace_id, "inquiry": inquiry_id})
    return [r["id"] for r in rows]


def _unresolved(attempts: Sequence[AttemptRecord]) -> AttemptRecord | None:
    pending = [
        a for a in attempts if a.outcome == SendAttemptOutcome.UNCERTAIN and a.reconciled_outcome is None
    ]
    return pending[-1] if pending else None


def _mailbox_of(attempt: AttemptRecord) -> UUID | None:
    owner = attempt.lease_owner
    if attempt.provider != EmailProviderKind.OUTLOOK_LOCAL or not owner.startswith(
        send_intents_repo.LEASE_OWNER_PREFIX
    ):
        return None
    try:
        return UUID(owner.removeprefix(send_intents_repo.LEASE_OWNER_PREFIX))
    except ValueError:
        return None


def _sender_of(record: InquiryRecord) -> SenderBinding:
    assert (
        record.sender_binding_id is not None
        and record.sender_binding_version is not None
        and record.sender_provider is not None
        and record.sender_account_id is not None
        and record.sender_from_address is not None
        and record.sender_display_name is not None
    )
    return SenderBinding(
        binding_id=record.sender_binding_id,
        binding_version=record.sender_binding_version,
        provider=record.sender_provider,
        account_id=record.sender_account_id,
        from_address=record.sender_from_address,
        display_name=record.sender_display_name,
        reply_to_address=record.sender_reply_to_address,
    )


async def handle_seller_inquiry_reconcile(ctx: RuntimeContext, execution: JobExecution) -> JobOutcome:
    job, actor = execution.job, execution.actor
    rt = inquiry_runtime(ctx)
    payload = _payload(execution)
    if payload.inquiry_id is None:
        raise ValidationFailed("a seller inquiry reconcile job needs its inquiry")
    inquiry_id = payload.inquiry_id
    async with unit_of_work(ctx.db, actor) as conn:
        record = await inquiries_repo.get_inquiry(conn, actor, inquiry_id)
        attempts = await inquiries_repo.list_attempts(conn, actor, inquiry_id)
        inbound = await correlated_inbound_replies(conn, actor, inquiry_id)
        binding = (
            None
            if record.sender_binding_id is None
            else await sender_bindings_repo.get_binding(conn, actor, record.sender_binding_id)
        )
        async with mapped_errors():
            now_row = await fetch_one(conn, "select clock_timestamp() as now")
    assert now_row is not None
    now = ensure_utc(now_row["now"])
    attempt = _unresolved(attempts)
    if record.state != InquiryState.UNCERTAIN or attempt is None:
        return await _complete(
            ctx, execution, _result("not_uncertain", inquiry_id=inquiry_id, state=record.state)
        )
    evidence: ReconciliationEvidence
    if inbound:
        evidence = ReconciliationEvidence(correlated_inbound=True)
    else:
        message_ids = [a.rfc_message_id for a in attempts if a.rfc_message_id][-20:]
        if not message_ids:
            return await _complete(
                ctx, execution, _result("still_uncertain", inquiry_id=inquiry_id, reasons=["NO_MESSAGE_ID"])
            )
        window = _window(attempts, now, rt.options)
        try:
            provider = _reconcile_provider(
                ctx, rt, actor.workspace_id, attempt=attempt, record=record, binding=binding
            )
        except SenderSetupError as exc:
            return await _block(
                ctx, execution, "RECONCILE_PROVIDER_UNAVAILABLE", ", ".join(exc.problems)[:300]
            )
        found: ReconcileOutcome = await provider.reconcile(
            inquiry_id=inquiry_id,
            rfc_message_ids=message_ids,
            window=window,
            provider_message_ids=[a.provider_message_id for a in attempts if a.provider_message_id][-20:],
        )
        if isinstance(found, ReconcileProviderUnavailable):
            wait = (
                timedelta(seconds=found.retry_after_seconds)
                if found.retry_after_seconds
                else backoff_delay(job.attempts)
            )

            async def retry_later() -> None:
                async with job_unit_of_work(ctx.db, job) as (conn, _locked):
                    await apply_disposition(
                        conn, job, Disposition.retry("RECONCILE_PROVIDER_UNAVAILABLE", wait)
                    )

            await retry_transient(retry_later)
            return JobOutcome(state=JobState.RETRY_WAIT, code="RECONCILE_PROVIDER_UNAVAILABLE")
        evidence = reconciliation_evidence(found)

    async def commit() -> JobOutcome:
        async with job_unit_of_work(ctx.db, job) as (conn, _locked):
            try:
                async with conn.transaction():
                    reconciled = await inquiries_repo.reconcile(conn, actor, inquiry_id, evidence=evidence)
            except VersionConflict:
                done = _result("not_uncertain", inquiry_id=inquiry_id)
                await apply_disposition(conn, job, Disposition.complete(done))
                return JobOutcome(state=JobState.SUCCEEDED, code="not_uncertain", details=done)
            state = reconciled.inquiry_state
            if state == InquiryState.ACCEPTED:
                outcome: Outcome = "accepted"
                if inbound:
                    from suv_deals.workers.reply_handlers import enqueue_reply_process_job  # noqa: PLC0415

                    for reply_id in inbound[:5]:
                        await enqueue_reply_process_job(
                            conn,
                            actor,
                            reply_id=reply_id,
                            inquiry_id=inquiry_id,
                            listing_id=record.qualification_listing_id,
                        )
            elif state == InquiryState.FAILED_DEFINITE:
                outcome = "proven_not_submitted"
                await enqueue_send_job(
                    conn,
                    actor,
                    inquiry_id=inquiry_id,
                    listing_id=record.qualification_listing_id,
                    attempt_number=len(attempts) + 1,
                    options=rt.options,
                )
            else:
                outcome = "still_uncertain"
            done = _result(outcome, inquiry_id=inquiry_id, reasons=list(reconciled.decision.reasons))
            await apply_disposition(conn, job, Disposition.complete(done))
            return JobOutcome(state=JobState.SUCCEEDED, code=outcome, details=done)

    return await retry_transient(commit)


def _window(
    attempts: Sequence[AttemptRecord], now: datetime, options: InquiryRuntimeOptions
) -> ReconcileWindow:
    start = min(a.send_intent_committed_at for a in attempts) - options.reconcile_lookback
    end = max(now, max(a.send_intent_committed_at for a in attempts) + options.reconcile_lookahead)
    if end - start > timedelta(days=59):
        start = end - timedelta(days=59)
    return ReconcileWindow(start=start, end=end)


def _reconcile_provider(
    ctx: RuntimeContext,
    rt: InquiryRuntime,
    workspace_id: UUID,
    *,
    attempt: AttemptRecord,
    record: InquiryRecord,
    binding: SenderBindingRecord | None,
) -> SenderProvider:
    """The read-only reconciliation source of the attempt's route (never a sending path)."""
    if attempt.provider == EmailProviderKind.OUTLOOK_LOCAL:
        box = _mailbox_of(attempt)
        if box is None:
            raise SenderSetupError("the send intent names no mailbox worker", ["MAILBOX_UNKNOWN"])
        gateway = send_intents_repo.PersistentOutlookGateway(
            ctx.db, workspace_id, mailbox_binding_id=box, request_id="inquiry-reconcile"
        )
        return OutlookLocalProvider(
            binding=_sender_of(record), mailbox_binding_id=box, gateway=gateway, clock=ctx.clock
        )
    if binding is None:
        raise SenderSetupError("the inquiry has no sender binding", ["SENDER_BINDING_MISSING"])
    return rt.reconcile_provider(workspace_id, binding)


__all__ = [
    "CANDIDATE_STATES",
    "PLAN_PREFIX",
    "RECONCILE_PREFIX",
    "SEND_PREFIX",
    "InquiryRuntime",
    "InquiryRuntimeOptions",
    "MailboxState",
    "automatic_sending_enabled",
    "configured_provider",
    "correlated_inbound_replies",
    "enqueue_plan_job",
    "enqueue_reconcile_job",
    "enqueue_send_job",
    "handle_seller_inquiry_plan",
    "handle_seller_inquiry_reconcile",
    "handle_seller_inquiry_send",
    "inquiry_runtime",
    "is_inquiry_candidate",
    "is_inquiry_candidate_state",
    "mailbox_state",
    "plan_dedup_key",
    "reconcile_dedup_key",
    "reservation_refusal",
    "send_dedup_key",
]
