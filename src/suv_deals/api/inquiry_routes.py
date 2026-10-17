"""Dashboard routes of spec 37 (``api.schemas.V11_DASHBOARD_ROUTES``; Supabase user JWT).

Same handler shape as ``api.routes``: authenticate (membership, workspace, rate limit), check the
route's scope, parse closed query/body models, run ONE short workspace-scoped transaction and
answer with the shared ``ResponseEnvelope``.

- Inquiries and replies (``inquiries:read``): frozen-snapshot list pages and single records. The
  recipient/sender ADDRESS is shown only to the owner (``views.inquiries.recipient_address_visible``,
  applied by the read service), and so is the text of a QUARANTINED reply (an unverified possible
  match that may be unrelated personal mail; ``reply_content_visible``); bodies are never part of
  a list.
- Inquiry control: ``GET`` shows kill switch, mode, caps, usage and how many kill-switch /
  authorization-revoked suppressions a resume could remove. ``pause`` (``inquiries:pause``) is
  the ``seller_inquiries_pause`` tool's rule set (``mcp.tools.pause_inquiries``: expected version,
  reason, idempotency key). ``resume`` is owner-only (``config:admin``), dashboard-only, needs the
  expected version, a reason and an idempotency key, and optionally removes (each one audited) the
  active ``kill_switch`` suppressions and, while the current standing authorization is effective,
  the ``authorization_revoked`` ones. Other suppressions (opt-out, bounce, complaint, ...) are never
  removed here. ``expected_removable_suppressions`` (the count the owner saw in the control view;
  REQUIRED with ``remove_suppressions``, ``422`` otherwise) is compared under the controls lock: a
  different count refuses the whole resume (``409 VERSION_CONFLICT``, ``details.reason =
  suppressions_changed``), so only the suppressions the owner saw are removed. The control view's
  sender readiness describes the CONFIGURED sending identity, and its process gate (D1 item 7) the
  serving backend's ``SELLER_INQUIRY_MODE``, kill switch and message-approval setting:
  ``automatic_inquiries_possible`` only while every gate is open.
- Activation canary evidence (owner only, ``config:admin``; read-only, D1 item 6): rows 4-6 of
  the activation checklist for the configured sender and the newest canaries (never the target).
- Mail-worker health and coverage gaps (``inquiries:read``): separate health dimensions per
  mailbox worker; a gap is never hidden.
- Lifecycle and lags (``deals:read``): separate scan/notification/mail lags; unknown is never zero.
- 15-day evaluation (``inquiries:read``; the read also needs ``deals:read``): built from stored
  evidence; zero suitable deals is reported as zero.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Final

from fastapi import APIRouter, Request
from starlette.responses import Response

from suv_deals.api.deps import (
    AuthContext,
    Authenticated,
    api_state,
    body_model,
    check_idempotency_header,
    in_transaction,
    no_query,
    path_id,
    query_model,
    require_scope,
)
from suv_deals.api.errors import JSON_MEDIA_TYPE
from suv_deals.api.schemas import (
    MAX_REMOVABLE_SUPPRESSIONS,
    V11_ROUTE_INDEX,
    ApiRoute,
    EvaluationQuery,
    InquiryListQuery,
    InquiryPauseRequest,
    InquiryResumeRequest,
    MailWorkerHealthQuery,
    ReplyListQuery,
)
from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import SuppressionReason
from suv_deals.domain.inquiries import requires_message_approval
from suv_deals.domain.money import Money
from suv_deals.errors import AppError, ErrorCode, ValidationFailed, VersionConflict
from suv_deals.mcp.tools import pause_inquiries
from suv_deals.persistence import canaries_repo, idempotency, inquiries_repo, queries
from suv_deals.persistence.database import Conn, db_now
from suv_deals.persistence.errors_map import TransientConflict
from suv_deals.persistence.inquiries_repo import SuppressionRow
from suv_deals.persistence.queries import QueryResult
from suv_deals.settings import Settings
from suv_deals.views.common import ResponseEnvelope, envelope
from suv_deals.views.inquiries import (
    ActivationCanaryView,
    CanaryEvidenceView,
    InquiryControlView,
    InquiryResumeResult,
)
from suv_deals.views.lifecycle import CoverageLagsView, ListingLifecycleView
from suv_deals.views.mail_workers import MailCoverageGapListView, MailWorkerHealthView
from suv_deals.workers.inquiry_handlers import (
    automatic_sending_enabled,
    configured_sender_binding,
    configured_sender_problems,
)

router = APIRouter()

RESUME_OPERATION: Final = "inquiry_control_resume"
#: ``details.reason`` of a resume refused because the removable suppressions changed.
SUPPRESSIONS_CHANGED: Final = "suppressions_changed"
#: Suppressions a resume may remove (the authorization one only while the authorization is
#: effective again); every other reason needs its own explicit owner decision.
RESUMABLE_REASONS: Final = frozenset({SuppressionReason.KILL_SWITCH, SuppressionReason.AUTHORIZATION_REVOKED})


def _route(key: str) -> ApiRoute:
    return V11_ROUTE_INDEX[key]


INQUIRIES: Final = _route("GET /api/inquiries")
INQUIRY: Final = _route("GET /api/inquiries/{inquiry_id}")
REPLIES: Final = _route("GET /api/replies")
REPLY: Final = _route("GET /api/replies/{reply_id}")
CONTROL: Final = _route("GET /api/inquiry-control")
PAUSE: Final = _route("POST /api/inquiry-control/pause")
RESUME: Final = _route("POST /api/inquiry-control/resume")
CANARY_EVIDENCE: Final = _route("GET /api/activation/canary-evidence")
#: Canaries listed by the canary-evidence view (newest first).
CANARY_EVIDENCE_LIMIT: Final = 20
HEALTH: Final = _route("GET /api/mail-workers/health")
GAPS: Final = _route("GET /api/mail-workers/coverage-gaps")
LAGS: Final = _route("GET /api/lifecycle/lags")
LIFECYCLE: Final = _route("GET /api/listings/{listing_id}/lifecycle")
EVALUATION: Final = _route("GET /api/evaluation")


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _respond(route: ApiRoute, body: ResponseEnvelope[Any]) -> Response:
    return Response(
        content=body.model_dump_json(), status_code=route.success_status, media_type=JSON_MEDIA_TYPE
    )


async def _read[T](
    request: Request, auth: AuthContext, work: Callable[[Conn, ActorContext], Awaitable[T]]
) -> T:
    return await in_transaction(api_state(request), auth.actor, lambda conn: work(conn, auth.actor))


async def _now(conn: Conn) -> datetime:
    return ensure_utc(await db_now(conn))


def _replay_error(code: str) -> AppError:
    try:
        error_code = ErrorCode(code)
    except ValueError:
        error_code = ErrorCode.INTERNAL_ERROR
    return AppError(error_code, "The original request with this idempotency key failed")


async def removable_suppressions(conn: Conn, actor: ActorContext, now: datetime) -> list[SuppressionRow]:
    """Active suppressions a resume may remove: ``kill_switch`` ones, and ``authorization_revoked``
    ones while the current standing authorization is effective (``inquiries:read``).

    Listed up to ``MAX_REMOVABLE_SUPPRESSIONS`` (the repository's bound), so the count the control
    view shows, the count a resume compares and the rows it removes are the same set."""
    rows = await inquiries_repo.active_suppressions(conn, actor, limit=MAX_REMOVABLE_SUPPRESSIONS)
    authorization = await inquiries_repo.current_authorization(conn, actor)
    authorized = authorization is not None and not authorization.authorization.problems_at(now)
    return [
        row
        for row in rows
        if row.reason == SuppressionReason.KILL_SWITCH
        or (row.reason == SuppressionReason.AUTHORIZATION_REVOKED and authorized)
    ]


# --------------------------------------------------------------------------------------------
# Inquiries and replies
# --------------------------------------------------------------------------------------------


@router.get("/api/inquiries")
async def get_inquiries(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, INQUIRIES.scope)
    query = query_model(request, InquiryListQuery)
    secret = api_state(request).cursor_secret()
    result = await _read(
        request,
        auth,
        lambda conn, actor: queries.list_inquiries(
            conn, actor, query, secret=secret, attention_only=query.attention_only
        ),
    )
    return _respond(INQUIRIES, result.envelope(auth.request_id))


@router.get("/api/inquiries/{inquiry_id}")
async def get_inquiry(request: Request, inquiry_id: str, auth: Authenticated) -> Response:
    require_scope(request, auth, INQUIRY.scope)
    no_query(request)
    target = path_id(inquiry_id, "inquiry_id")
    result = await _read(request, auth, lambda conn, actor: queries.get_inquiry(conn, actor, target))
    return _respond(INQUIRY, result.envelope(auth.request_id))


@router.get("/api/replies")
async def get_replies(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, REPLIES.scope)
    query = query_model(request, ReplyListQuery)
    secret = api_state(request).cursor_secret()
    result = await _read(
        request, auth, lambda conn, actor: queries.list_replies(conn, actor, query, secret=secret)
    )
    return _respond(REPLIES, result.envelope(auth.request_id))


@router.get("/api/replies/{reply_id}")
async def get_reply(request: Request, reply_id: str, auth: Authenticated) -> Response:
    require_scope(request, auth, REPLY.scope)
    no_query(request)
    target = path_id(reply_id, "reply_id")
    # A quarantined reply's text is for the owner only (``views.inquiries.reply_content_visible``);
    # the read query withholds it for every other caller (the same rule as MCP and the CLI).
    result = await _read(request, auth, lambda conn, actor: queries.get_reply(conn, actor, target))
    return _respond(REPLY, result.envelope(auth.request_id))


# --------------------------------------------------------------------------------------------
# Inquiry control
# --------------------------------------------------------------------------------------------


async def configured_control_view(conn: Conn, actor: ActorContext, settings: Settings) -> InquiryControlView:
    """``inquiries_repo.control_view`` for the CONFIGURED sending identity (``inquiries:read``).

    The sender readiness describes the binding the runtime would actually send from
    (`workers.inquiry_handlers.configured_sender_binding`: ``SELLER_EMAIL_PROVIDER`` /
    ``_ACCOUNT_ID`` / ``_FROM`` / ``_REPLY_TO``), never merely the newest binding. Without one, or
    when the provider's newest binding is not exactly the configured identity, the readiness is
    ``missing`` and ``sender_problems`` names why (``sender_identity_*`` codes, no values); the
    runtime never sends from such a binding. Also adds ``removable_suppressions``.
    """
    sender = await configured_sender_binding(conn, actor, settings)
    view = await inquiries_repo.control_view(
        conn, actor, sender_binding_id=None if sender is None else sender.id
    )
    update: dict[str, Any] = {}
    if sender is None:
        update = {
            "sender_readiness": "missing",
            "sender_provider": None,
            "sender_binding_version": None,
            "sender_problems": ("sender_binding_missing",),
            "activation_canary_complete": False,
        }
    else:
        identity = [
            f"sender_identity_{code.lower()}"
            for code in configured_sender_problems(settings, sender)
            if code != "SENDER_BINDING_MISSING"
        ]
        if identity:
            problems = tuple(dict.fromkeys([*identity, *view.sender_problems]))[:10]
            update = {
                "sender_readiness": "missing",
                "sender_problems": problems,
                "activation_canary_complete": False,
            }
    removable = await removable_suppressions(conn, actor, await _now(conn))
    gated = view.model_copy(update={**update, "removable_suppressions": len(removable)})
    return gated.model_copy(update=process_gate(settings, gated))


def caps_leave_room(view: InquiryControlView) -> bool:
    """Whether the owner's rolling caps let an inquiry go out NOW: both caps above 0 (a cap of 0
    holds every real seller inquiry, e.g. during the activation canary step,
    docs/seller_email_activation.md section 8) and neither rolling window used up."""
    return view.used_24h < view.max_per_24h and view.used_15d < view.max_per_15d


def process_gate(settings: Settings, view: InquiryControlView) -> dict[str, Any]:
    """The PROCESS-level gate of the serving backend next to the database controls (D1 item 7).

    ``SELLER_INQUIRY_MODE``, ``SELLER_INQUIRY_KILL_SWITCH`` and the owner's
    ``SELLER_INQUIRY_REQUIRE_MESSAGE_APPROVAL`` setting (which disables automatic sending); the
    blocker codes are those of ``suv-deals canary send``. ``automatic_inquiries_possible`` only when
    every gate is open (process, database mode and kill switch, authorization, configured sender
    and its completed activation canary) and the owner's rolling caps leave room now
    (`caps_leave_room`), so the dashboard never claims automatic inquiries while any gate is
    closed or the owner holds them with a cap of 0.
    """
    blockers: list[str] = []
    if settings.seller_inquiry_mode != "automatic":
        blockers.append("SELLER_INQUIRY_MODE_NOT_AUTOMATIC")
    if settings.seller_inquiry_kill_switch:
        blockers.append("SELLER_INQUIRY_KILL_SWITCH_ON")
    if requires_message_approval(settings):
        blockers.append("MESSAGE_APPROVAL_SETTING_ON")
    possible = (
        not blockers
        and automatic_sending_enabled(settings)
        and view.mode == "automatic"
        and not view.kill_switch
        and view.authorization_status == "active"
        and view.sender_readiness == "ready"
        # F3/OPS-04 (wave D2): `inquiries_repo.reserve` refuses every real inquiry
        # (activation_canary_incomplete) until the sender's current version has a correlated canary.
        and view.activation_canary_complete
        and caps_leave_room(view)
    )
    return {
        "process_mode": settings.seller_inquiry_mode,
        "process_kill_switch": settings.seller_inquiry_kill_switch,
        "process_message_approval_required": requires_message_approval(settings),
        "process_blockers": tuple(blockers),
        "automatic_inquiries_possible": possible,
    }


@router.get("/api/inquiry-control")
async def get_inquiry_control(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, CONTROL.scope)
    no_query(request)
    settings = api_state(request).settings

    async def work(conn: Conn, actor: ActorContext) -> tuple[Any, datetime]:
        view = await configured_control_view(conn, actor, settings)
        return view, await _now(conn)

    view, as_of = await _read(request, auth, work)
    return _respond(CONTROL, envelope(view, request_id=auth.request_id, as_of=as_of))


@router.post("/api/inquiry-control/pause")
async def post_inquiry_pause(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, PAUSE.scope)
    no_query(request)
    body = await body_model(request, InquiryPauseRequest)
    check_idempotency_header(request, body.idempotency_key)
    tool = body.to_tool_input()

    async def work(conn: Conn, actor: ActorContext) -> tuple[Any, datetime]:
        result = await pause_inquiries(conn, actor, tool)
        return result, await _now(conn)

    view, as_of = await _read(request, auth, work)
    return _respond(PAUSE, envelope(view, request_id=auth.request_id, as_of=as_of))


class SuppressionsChanged(VersionConflict):
    """The removable suppressions differ from the count the owner saw (``409 VERSION_CONFLICT``,
    ``details.reason = suppressions_changed``); nothing was changed. Reload and decide again."""

    def __init__(self, *, expected: int, current: int) -> None:
        super().__init__(
            "The suppressions a resume would remove changed; reload the inquiry controls and retry",
            reason=SUPPRESSIONS_CHANGED,
            expected_removable_suppressions=expected,
            current_removable_suppressions=current,
        )


async def resume_inquiries(
    conn: Conn,
    actor: ActorContext,
    *,
    expected_version: int,
    reason: str,
    remove_suppressions: bool,
    expected_removable: int | None,
) -> InquiryResumeResult:
    """The owner's resume (dashboard route and ``suv-deals inquiries resume``), in ONE transaction.

    ``inquiries_repo.resume`` locks the controls row first and checks ``expected_version``; the
    removable suppressions are listed after that lock (``add_suppression`` takes the same lock, so
    the set cannot grow underneath) and a count different from ``expected_removable`` refuses the
    WHOLE resume (`SuppressionsChanged`; the caller's transaction rolls back, nothing changes).
    Only then is each one removed (one audited removal each). A removal without
    ``expected_removable`` is refused before anything changes (``VALIDATION_ERROR``): only the set
    the owner saw is ever removed. Nothing is sent by a resume.
    """
    if remove_suppressions and expected_removable is None:
        raise ValidationFailed(
            "A resume that removes suppressions must name the removable count the owner saw",
            details={"fields": ["expected_removable_suppressions"]},
        )
    result = await inquiries_repo.resume(conn, actor, expected_version=expected_version, reason=reason)
    removed = 0
    if remove_suppressions and expected_removable is not None:
        rows = await removable_suppressions(conn, actor, await _now(conn))
        if len(rows) != expected_removable:
            raise SuppressionsChanged(expected=expected_removable, current=len(rows))
        for row in rows:
            await inquiries_repo.remove_suppression(conn, actor, row.id, reason=f"resume: {reason}")
            removed += 1
    return result.model_copy(update={"suppressions_removed": removed})


async def _resume(conn: Conn, actor: ActorContext, body: InquiryResumeRequest) -> InquiryResumeResult:
    request_hash = idempotency.request_hash_for(RESUME_OPERATION, body)
    started = await idempotency.begin(conn, actor, RESUME_OPERATION, body.idempotency_key, request_hash)
    if isinstance(started, idempotency.Replay):
        return InquiryResumeResult.model_validate(started.result)
    if isinstance(started, idempotency.ReplayError):
        raise _replay_error(started.error_code)
    if isinstance(started, idempotency.InProgress):
        raise TransientConflict.in_progress()
    final = await resume_inquiries(
        conn,
        actor,
        expected_version=body.expected_version,
        reason=body.reason,
        remove_suppressions=body.remove_suppressions,
        expected_removable=body.expected_removable_suppressions,
    )
    await idempotency.complete(
        conn, actor, RESUME_OPERATION, body.idempotency_key, final.model_dump(mode="json")
    )
    return final


@router.post("/api/inquiry-control/resume")
async def post_inquiry_resume(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, RESUME.scope)
    no_query(request)
    body = await body_model(request, InquiryResumeRequest)
    check_idempotency_header(request, body.idempotency_key)

    async def work(conn: Conn, actor: ActorContext) -> tuple[Any, datetime]:
        result = await _resume(conn, actor, body)
        return result, await _now(conn)

    view, as_of = await _read(request, auth, work)
    return _respond(RESUME, envelope(view, request_id=auth.request_id, as_of=as_of))


# --------------------------------------------------------------------------------------------
# Activation canary evidence (owner only, read-only)
# --------------------------------------------------------------------------------------------


async def canary_evidence_view(conn: Conn, actor: ActorContext, settings: Settings) -> CanaryEvidenceView:
    """Rows 4-6 of the activation evidence for the CONFIGURED sender (D1 item 6).

    The same state as ``suv-deals canary status`` / ``doctor`` (`canaries_repo.evidence_state`);
    without a usable configured identity (`configured_sender_problems`) it is ``no_sender``. Lists
    the newest canaries with ids, states and times only (never the target, its hash, the purpose or
    evidence text). Reads only: nothing is prepared, claimed or sent.
    """
    records = await canaries_repo.list_canaries(conn, actor, limit=CANARY_EVIDENCE_LIMIT)
    sender = await configured_sender_binding(conn, actor, settings)
    usable = sender if sender is not None and not configured_sender_problems(settings, sender) else None
    state, detail = canaries_repo.evidence_state(records, usable)
    return CanaryEvidenceView(
        evidence=state,
        detail=detail[:300],
        sender_provider=None if usable is None else usable.provider,
        sender_binding_version=None if usable is None else usable.version,
        canaries=tuple(
            ActivationCanaryView(
                id=r.id,
                provider=r.provider,
                sender_binding_version=r.sender_binding_version,
                current_sender_version=usable is not None
                and r.sender_binding_id == usable.id
                and r.sender_binding_version == usable.version,
                state=r.state,
                created_at=r.created_at,
                outcome_recorded_at=r.outcome_recorded_at,
                accepted_at=r.accepted_at,
                reply_recorded_at=r.reply_recorded_at,
            )
            for r in records
        ),
    )


@router.get("/api/activation/canary-evidence")
async def get_canary_evidence(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, CANARY_EVIDENCE.scope)
    no_query(request)
    settings = api_state(request).settings

    async def work(conn: Conn, actor: ActorContext) -> tuple[Any, datetime]:
        view = await canary_evidence_view(conn, actor, settings)
        return view, await _now(conn)

    view, as_of = await _read(request, auth, work)
    return _respond(CANARY_EVIDENCE, envelope(view, request_id=auth.request_id, as_of=as_of))


# --------------------------------------------------------------------------------------------
# Mail-worker health
# --------------------------------------------------------------------------------------------


async def _health(request: Request, auth: AuthContext) -> QueryResult[MailWorkerHealthView]:
    query = query_model(request, MailWorkerHealthQuery)
    settings = api_state(request).settings
    reconcile = timedelta(seconds=settings.mail_reconcile_interval_seconds)
    result = await _read(
        request,
        auth,
        lambda conn, actor: queries.mail_worker_health_view(
            conn, actor, include_revoked=query.include_revoked, reconcile_interval=reconcile
        ),
    )
    view = MailWorkerHealthView.model_validate(result.data.model_dump())
    return QueryResult(
        data=view, as_of=result.as_of, warnings=result.warnings, next_cursor=result.next_cursor
    )


@router.get("/api/mail-workers/health")
async def get_mail_worker_health(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, HEALTH.scope)
    result = await _health(request, auth)
    return _respond(HEALTH, result.envelope(auth.request_id))


@router.get("/api/mail-workers/coverage-gaps")
async def get_mail_coverage_gaps(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, GAPS.scope)
    result = await _health(request, auth)
    gaps = MailCoverageGapListView.of(result.data)
    return _respond(
        GAPS, envelope(gaps, request_id=auth.request_id, as_of=result.as_of, warnings=result.warnings)
    )


# --------------------------------------------------------------------------------------------
# Lifecycle, lags and evaluation
# --------------------------------------------------------------------------------------------


@router.get("/api/lifecycle/lags")
async def get_lifecycle_lags(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, LAGS.scope)
    no_query(request)
    result = await _read(request, auth, queries.coverage_lags_view)
    view = CoverageLagsView.model_validate(result.data.model_dump())
    return _respond(
        LAGS,
        envelope(view, request_id=auth.request_id, as_of=result.as_of, warnings=result.warnings),
    )


@router.get("/api/listings/{listing_id}/lifecycle")
async def get_listing_lifecycle(request: Request, listing_id: str, auth: Authenticated) -> Response:
    require_scope(request, auth, LIFECYCLE.scope)
    no_query(request)
    target = path_id(listing_id, "listing_id")
    result = await _read(
        request, auth, lambda conn, actor: queries.listing_lifecycle_view(conn, actor, target)
    )
    view = ListingLifecycleView.model_validate(result.data.model_dump())
    return _respond(
        LIFECYCLE,
        envelope(view, request_id=auth.request_id, as_of=result.as_of, warnings=result.warnings),
    )


@router.get("/api/evaluation")
async def get_evaluation(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, EVALUATION.scope)
    query_model(request, EvaluationQuery)  # the window is fixed at 15 days; refuses anything else
    settings = api_state(request).settings
    threshold = (
        Money.of(settings.proposed_min_contribution_eur, "EUR")
        if settings.contribution_threshold_approved
        else None
    )
    result = await _read(
        request,
        auth,
        lambda conn, actor: queries.evaluation_report(conn, actor, approved_threshold=threshold),
    )
    return _respond(EVALUATION, result.envelope(auth.request_id))


__all__ = [
    "CANARY_EVIDENCE_LIMIT",
    "RESUMABLE_REASONS",
    "RESUME_OPERATION",
    "SUPPRESSIONS_CHANGED",
    "SuppressionsChanged",
    "canary_evidence_view",
    "caps_leave_room",
    "configured_control_view",
    "process_gate",
    "removable_suppressions",
    "resume_inquiries",
    "router",
]
