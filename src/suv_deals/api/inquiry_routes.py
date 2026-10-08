"""Dashboard routes of spec 37 (``api.schemas.V11_DASHBOARD_ROUTES``; Supabase user JWT).

Same handler shape as ``api.routes``: authenticate (membership, workspace, rate limit), check the
route's scope, parse closed query/body models, run ONE short workspace-scoped transaction and
answer with the shared ``ResponseEnvelope``.

- Inquiries and replies (``inquiries:read``): frozen-snapshot list pages and single records. The
  recipient/sender ADDRESS is shown only to the owner (``views.inquiries.recipient_address_visible``,
  applied by the read service); bodies are never part of a list.
- Inquiry control: ``GET`` shows kill switch, mode, caps, usage and how many kill-switch /
  authorization-revoked suppressions a resume could remove. ``pause`` (``inquiries:pause``) is
  the ``seller_inquiries_pause`` tool's rule set (``mcp.tools.pause_inquiries``: expected version,
  reason, idempotency key). ``resume`` is owner-only (``config:admin``), dashboard-only, needs the
  expected version, a reason and an idempotency key, and optionally removes (each one audited) the
  active ``kill_switch`` suppressions and, while the current standing authorization is effective,
  the ``authorization_revoked`` ones. Other suppressions (opt-out, bounce, complaint, ...) are never
  removed here.
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
from suv_deals.domain.money import Money
from suv_deals.errors import AppError, ErrorCode
from suv_deals.mcp.tools import pause_inquiries
from suv_deals.persistence import idempotency, inquiries_repo, queries
from suv_deals.persistence.database import Conn, db_now
from suv_deals.persistence.errors_map import TransientConflict
from suv_deals.persistence.inquiries_repo import SuppressionRow
from suv_deals.persistence.queries import QueryResult
from suv_deals.views.common import ResponseEnvelope, envelope
from suv_deals.views.inquiries import InquiryResumeResult
from suv_deals.views.lifecycle import CoverageLagsView, ListingLifecycleView
from suv_deals.views.mail_workers import MailCoverageGapListView, MailWorkerHealthView

router = APIRouter()

RESUME_OPERATION: Final = "inquiry_control_resume"
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
    ones while the current standing authorization is effective (``inquiries:read``)."""
    rows = await inquiries_repo.active_suppressions(conn, actor)
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
    result = await _read(request, auth, lambda conn, actor: queries.get_reply(conn, actor, target))
    return _respond(REPLY, result.envelope(auth.request_id))


# --------------------------------------------------------------------------------------------
# Inquiry control
# --------------------------------------------------------------------------------------------


@router.get("/api/inquiry-control")
async def get_inquiry_control(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, CONTROL.scope)
    no_query(request)

    async def work(conn: Conn, actor: ActorContext) -> tuple[Any, datetime]:
        now = await _now(conn)
        view = await inquiries_repo.control_view(conn, actor)
        removable = await removable_suppressions(conn, actor, now)
        return view.model_copy(update={"removable_suppressions": len(removable)}), now

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


async def _resume(conn: Conn, actor: ActorContext, body: InquiryResumeRequest) -> InquiryResumeResult:
    request_hash = idempotency.request_hash_for(RESUME_OPERATION, body)
    started = await idempotency.begin(conn, actor, RESUME_OPERATION, body.idempotency_key, request_hash)
    if isinstance(started, idempotency.Replay):
        return InquiryResumeResult.model_validate(started.result)
    if isinstance(started, idempotency.ReplayError):
        raise _replay_error(started.error_code)
    if isinstance(started, idempotency.InProgress):
        raise TransientConflict("The same request is still in progress; retry shortly")
    result = await inquiries_repo.resume(
        conn, actor, expected_version=body.expected_version, reason=body.reason
    )
    removed = 0
    if body.remove_suppressions:
        for row in await removable_suppressions(conn, actor, await _now(conn)):
            await inquiries_repo.remove_suppression(conn, actor, row.id, reason=f"resume: {body.reason}")
            removed += 1
    final = result.model_copy(update={"suppressions_removed": removed})
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
    "RESUMABLE_REASONS",
    "RESUME_OPERATION",
    "removable_suppressions",
    "router",
]
