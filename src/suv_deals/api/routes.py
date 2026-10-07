"""Dashboard BFF routes (docs/api_contract.md section 6; spec 19, 21, 23, 30).

Every route of ``api.schemas.ROUTES`` is implemented here; the route table is the single source of
each route's scope and success status. Handlers are thin:

1. authenticate (``deps.authenticate``: bearer token, active membership, workspace, rate limit),
2. check the route's scope (``403`` + denial metric),
3. parse the path ids, query string or JSON body into the shared closed models,
4. run the shared read-query service (``persistence.queries``) or repository mutation inside ONE
   short workspace-scoped transaction (``transactions.unit_of_work``; transient conflicts that
   prove a rollback are re-run),
5. answer with the shared ``ResponseEnvelope`` (``as_of`` in database time).

Mutations are idempotent through the body's ``idempotency_key`` (scoped by principal and
operation; same key + same request replays the original result, a different request is
``IDEMPOTENCY_CONFLICT``) and use ``expected_version`` optimistic concurrency. Cursors are the
HMAC-signed cursors of ``domain.pagination`` keyed with ``Settings.mcp_cursor_signing_secret``;
an altered, expired or mismatched cursor is ``422`` with ``details.cursor``.

Spec 19 dashboard actions. "Needs inspection", "needs documents" and "price confirmation needed"
are submitted through ``POST /api/reviews/{case_id}/submit`` as a ``needs_information`` decision
whose ``reason_codes`` include ``needs_inspection``, ``needs_documents`` or
``price_confirmation_needed`` (the ``domain.due_diligence.DashboardAction`` values) and whose
``missing_information`` lists the open items. `dashboard_action_request` builds that body. These
reason codes (in any case or separator spelling) are refused with any other outcome, so an action
can never shortlist, watch or reject a case by accident.

Recheck (``POST /api/listings/{listing_id}/recheck``) queues a budget-controlled ``recheck`` job
for a registered listing only (the stored canonical URL; the request carries no URL). A paused,
disabled or parser-unhealthy source is ``SOURCE_PAUSED``; an access-blocked source is
``ACCESS_BLOCKED``; a detail job already waiting for the listing is returned as
``deduplicated: true``.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Final
from uuid import UUID

import anyio
from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.responses import Response

from suv_deals.api.deps import (
    ApiState,
    AuthContext,
    Authenticated,
    AuthenticatedBootstrap,
    api_state,
    body_model,
    check_idempotency_header,
    in_transaction,
    no_query,
    path_id,
    query_model,
    require_scope,
)
from suv_deals.api.errors import JSON_MEDIA_TYPE, method_not_allowed
from suv_deals.api.schemas import (
    ROUTE_INDEX,
    ROUTES,
    AddNoteRequest,
    ApiRoute,
    CandidateDetailQuery,
    CandidateListQuery,
    ClaimRequest,
    ComparablesQuery,
    OutboxQuery,
    PauseSourceRequest,
    RecheckRequest,
    ReleaseRequest,
    ReviewQueueQuery,
    SubmitReviewRequest,
)
from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.due_diligence import DashboardAction
from suv_deals.domain.enums import JobState, JobType, ReviewOutcome, Scope, TechnicalStatus
from suv_deals.errors import AccessBlocked, AppError, ErrorCode, SourcePaused, ValidationFailed
from suv_deals.mcp.schemas import DealsRequestRecheckInput, ReviewsSubmitInput
from suv_deals.persistence import (
    audit,
    idempotency,
    jobs,
    listings_repo,
    notes_repo,
    queries,
    reviews_repo,
    sources_repo,
)
from suv_deals.persistence.database import Conn, db_now, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.queries.operations import SCHEMA_MARKERS, build_info, schema_markers_present
from suv_deals.persistence.transactions import lock_source
from suv_deals.views.common import ResponseEnvelope, ResponseWarning, WarningCode, envelope, warning
from suv_deals.views.notes import RecheckRequestResult
from suv_deals.views.operations import (
    LivenessView,
    MembershipView,
    MeView,
    ReadinessCheck,
    ReadinessView,
    WorkspaceView,
)

router = APIRouter()

RECHECK_OPERATION: Final = "deals_request_recheck"
DASHBOARD_ACTION_REASON_CODES: Final = frozenset(action.value for action in DashboardAction)
DASHBOARD_ACTION_SUMMARIES: Final[dict[DashboardAction, str]] = {
    DashboardAction.NEEDS_INSPECTION: (
        "Needs inspection: an independent physical inspection is required before any decision."
    ),
    DashboardAction.NEEDS_DOCUMENTS: (
        "Needs documents: the vehicle documents must be obtained and checked before any decision."
    ),
    DashboardAction.PRICE_CONFIRMATION_NEEDED: (
        "Price confirmation needed: the payable price and availability must be confirmed before any decision."
    ),
}
READINESS_TIMEOUT_SECONDS: Final = 3.0
_MAX_MEMBERSHIPS: Final = 50
_CODE_SEPARATORS: Final = re.compile(r"[-.:]")

_WAITING_JOB_SQL: Final = (
    "select id, state, available_at from ops.jobs"
    " where workspace_id = %(ws)s and listing_id = %(listing_id)s"
    " and job_type in ('detail', 'recheck') and state in ('queued', 'retry_wait')"
    " order by created_at, id limit 1"
)


def _route(key: str) -> ApiRoute:
    return ROUTE_INDEX[key]


ME: Final = _route("GET /api/me")
OVERVIEW: Final = _route("GET /api/overview")
CANDIDATES: Final = _route("GET /api/candidates")
CANDIDATE: Final = _route("GET /api/candidates/{listing_id}")
COMPARABLES: Final = _route("GET /api/comparables/{set_id}")
VALUATION: Final = _route("GET /api/valuations/{valuation_id}")
REVIEWS: Final = _route("GET /api/reviews")
REVIEW: Final = _route("GET /api/reviews/{case_id}")
CLAIM: Final = _route("POST /api/reviews/{case_id}/claim")
RELEASE: Final = _route("POST /api/reviews/{case_id}/release")
SUBMIT: Final = _route("POST /api/reviews/{case_id}/submit")
NOTES: Final = _route("POST /api/listings/{listing_id}/notes")
RECHECK: Final = _route("POST /api/listings/{listing_id}/recheck")
SOURCES: Final = _route("GET /api/sources")
PAUSE: Final = _route("POST /api/sources/{source_id}/pause")
SETTINGS: Final = _route("GET /api/settings")
OUTBOX: Final = _route("GET /api/outbox")


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def json_response(model: BaseModel, status: int) -> Response:
    return Response(content=model.model_dump_json(), status_code=status, media_type=JSON_MEDIA_TYPE)


def _respond(route: ApiRoute, body: ResponseEnvelope[Any]) -> Response:
    return json_response(body, route.success_status)


async def _read[T](
    request: Request, auth: AuthContext, work: Callable[[Conn, ActorContext], Awaitable[T]]
) -> T:
    state = api_state(request)
    return await in_transaction(state, auth.actor, lambda conn: work(conn, auth.actor))


async def _now(conn: Conn) -> datetime:
    return ensure_utc(await db_now(conn))


def _mutation_envelope[T](
    data: T, auth: AuthContext, as_of: datetime, warnings: Sequence[ResponseWarning] = ()
) -> ResponseEnvelope[T]:
    return envelope(data, request_id=auth.request_id, as_of=as_of, warnings=warnings)


# --------------------------------------------------------------------------------------------
# Health and readiness (public, not enveloped)
# --------------------------------------------------------------------------------------------


PROBE_METHODS: Final = ["GET", "HEAD"]


@router.api_route("/healthz", methods=PROBE_METHODS)
async def get_healthz(request: Request) -> Response:
    """Process liveness only; never touches dependencies (``HEAD`` too, for load balancers)."""
    state = api_state(request)
    return json_response(LivenessView(build_id=build_info(state.settings).build_id), 200)


async def _database_checks(state: ApiState) -> tuple[ReadinessCheck, ReadinessCheck]:
    try:
        with anyio.fail_after(READINESS_TIMEOUT_SECONDS):
            async with mapped_errors(), state.db.transaction() as conn, mapped_errors():
                present = await schema_markers_present(conn, SCHEMA_MARKERS)
    except (AppError, TimeoutError, OSError):
        return (
            ReadinessCheck(name="database", status="unavailable", detail="database not reachable"),
            ReadinessCheck(name="schema", status="unknown", detail="database not reachable"),
        )
    database = ReadinessCheck(name="database", status="ok", detail=None)
    missing = sum(1 for ok in present if not ok)
    if missing:
        return database, ReadinessCheck(
            name="schema", status="unavailable", detail=f"{missing} required schema objects are missing"
        )
    return database, ReadinessCheck(name="schema", status="ok", detail=None)


def _config_check(state: ApiState) -> ReadinessCheck:
    settings = state.settings
    try:
        state.cursor_secret()
    except AppError:
        return ReadinessCheck(
            name="config",
            status="not_configured",
            detail="pagination cursor signing secret is not configured",
        )
    if state.verifier is None:
        return ReadinessCheck(
            name="config", status="not_configured", detail="dashboard authentication is not configured"
        )
    if settings.app_env == "production":
        if settings.mcp_auth_mode == "dev_local":
            return ReadinessCheck(
                name="config", status="degraded", detail="development MCP authentication mode in production"
            )
        if not settings.app_base_url.startswith("https://"):
            return ReadinessCheck(
                name="config",
                status="degraded",
                detail="the dashboard base address must use https in production",
            )
    return ReadinessCheck(name="config", status="ok", detail=None)


async def readiness_view(state: ApiState) -> ReadinessView:
    """Database reachable, required schema objects present and critical configuration set.

    Cached for ``ApiOptions.readiness_cache_seconds`` so an unauthenticated caller cannot turn the
    endpoint into database load; refreshes are serialized, so a burst of concurrent probes shares
    ONE database check (and at most one pooled connection) instead of one each. No hostnames,
    URLs, versions or secrets are reported.
    """
    cached = _fresh_readiness(state)
    if cached is not None:
        return cached
    async with state.readiness_lock:
        cached = _fresh_readiness(state)  # another probe refreshed it while this one waited
        if cached is not None:
            return cached
        database, schema = await _database_checks(state)
        config = _config_check(state)
        checks = (database, schema, config)
        view = ReadinessView(
            ready=all(check.status == "ok" for check in checks),
            checks=checks,
            build_id=build_info(state.settings).build_id,
        )
        state.readiness_cache = (anyio.current_time(), view)
        return view


def _fresh_readiness(state: ApiState) -> ReadinessView | None:
    cached = state.readiness_cache
    if cached is not None and anyio.current_time() - cached[0] < state.options.readiness_cache_seconds:
        return cached[1]
    return None


@router.api_route("/readyz", methods=PROBE_METHODS)
async def get_readyz(request: Request) -> Response:
    view = await readiness_view(api_state(request))
    return json_response(view, 200 if view.ready else 503)


# --------------------------------------------------------------------------------------------
# Identity and overview
# --------------------------------------------------------------------------------------------


@router.get("/api/me")
async def get_me(request: Request, auth: AuthenticatedBootstrap) -> Response:
    no_query(request)
    state = api_state(request)
    principal = auth.principal
    selected = principal.membership
    listed = list(principal.memberships)
    if len(listed) > _MAX_MEMBERSHIPS:
        others = [m for m in listed if m.workspace_id != selected.workspace_id]
        listed = [selected, *others[: _MAX_MEMBERSHIPS - 1]]
    view = MeView(
        principal_id=principal.user.user_id,
        principal_kind="user",
        display_name=None,
        role=selected.role,
        scopes=tuple(scope for scope in Scope if scope in auth.actor.scopes),
        workspace=WorkspaceView(
            workspace_id=selected.workspace_id,
            name=selected.workspace_name[:200],
            display_timezone=selected.display_timezone[:64],
        ),
        memberships=tuple(
            MembershipView(
                workspace_id=m.workspace_id,
                workspace_name=m.workspace_name[:200],
                role=m.role,
                active=m.active,
            )
            for m in listed
        ),
    )
    return _respond(ME, envelope(view, request_id=auth.request_id, as_of=state.clock.now()))


@router.get("/api/overview")
async def get_overview(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, OVERVIEW.scope)
    no_query(request)
    settings = api_state(request).settings
    result = await _read(request, auth, lambda conn, actor: queries.overview_view(conn, actor, settings))
    return _respond(OVERVIEW, result.envelope(auth.request_id))


# --------------------------------------------------------------------------------------------
# Candidates, comparables, valuations
# --------------------------------------------------------------------------------------------


@router.get("/api/candidates")
async def get_candidates(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, CANDIDATES.scope)
    query = query_model(request, CandidateListQuery).to_tool_input()
    secret = api_state(request).cursor_secret()
    result = await _read(
        request, auth, lambda conn, actor: queries.list_candidates(conn, actor, query, secret=secret)
    )
    return _respond(CANDIDATES, result.envelope(auth.request_id))


@router.get("/api/candidates/{listing_id}")
async def get_candidate(request: Request, listing_id: str, auth: Authenticated) -> Response:
    require_scope(request, auth, CANDIDATE.scope)
    tool = query_model(request, CandidateDetailQuery).to_tool_input(path_id(listing_id, "listing_id"))
    result = await _read(
        request,
        auth,
        lambda conn, actor: queries.get_candidate(conn, actor, tool.listing_id, tool.revision),
    )
    return _respond(CANDIDATE, result.envelope(auth.request_id))


@router.get("/api/comparables/{set_id}")
async def get_comparables(request: Request, set_id: str, auth: Authenticated) -> Response:
    require_scope(request, auth, COMPARABLES.scope)
    tool = query_model(request, ComparablesQuery).to_tool_input(path_id(set_id, "set_id"))
    secret = api_state(request).cursor_secret()
    result = await _read(
        request,
        auth,
        lambda conn, actor: queries.get_comparables(
            conn,
            actor,
            tool.comparable_set_id,
            include_excluded=tool.include_excluded,
            cursor=tool.cursor,
            limit=tool.limit,
            secret=secret,
        ),
    )
    return _respond(COMPARABLES, result.envelope(auth.request_id))


@router.get("/api/valuations/{valuation_id}")
async def get_valuation(request: Request, valuation_id: str, auth: Authenticated) -> Response:
    require_scope(request, auth, VALUATION.scope)
    no_query(request)
    target = path_id(valuation_id, "valuation_id")
    result = await _read(request, auth, lambda conn, actor: queries.get_valuation(conn, actor, target))
    return _respond(VALUATION, result.envelope(auth.request_id))


# --------------------------------------------------------------------------------------------
# Reviews
# --------------------------------------------------------------------------------------------


@router.get("/api/reviews")
async def get_reviews(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, REVIEWS.scope)
    query = query_model(request, ReviewQueueQuery).to_tool_input()
    secret = api_state(request).cursor_secret()
    result = await _read(
        request, auth, lambda conn, actor: queries.review_queue(conn, actor, query, secret=secret)
    )
    return _respond(REVIEWS, result.envelope(auth.request_id))


@router.get("/api/reviews/{case_id}")
async def get_review(request: Request, case_id: str, auth: Authenticated) -> Response:
    require_scope(request, auth, REVIEW.scope)
    no_query(request)
    target = path_id(case_id, "case_id")
    result = await _read(request, auth, lambda conn, actor: queries.get_review_case(conn, actor, target))
    return _respond(REVIEW, result.envelope(auth.request_id))


@router.post("/api/reviews/{case_id}/claim")
async def post_claim(request: Request, case_id: str, auth: Authenticated) -> Response:
    require_scope(request, auth, CLAIM.scope)
    no_query(request)
    target = path_id(case_id, "case_id")
    body = await body_model(request, ClaimRequest)
    check_idempotency_header(request, body.idempotency_key)
    tool = body.to_tool_input(target)

    async def work(conn: Conn, actor: ActorContext) -> tuple[Any, datetime]:
        view = await reviews_repo.claim(
            conn, actor, tool.case_id, tool.expected_version, tool.idempotency_key
        )
        return view, await _now(conn)

    view, as_of = await _read(request, auth, work)
    return _respond(CLAIM, _mutation_envelope(view, auth, as_of))


@router.post("/api/reviews/{case_id}/release")
async def post_release(request: Request, case_id: str, auth: Authenticated) -> Response:
    require_scope(request, auth, RELEASE.scope)
    no_query(request)
    target = path_id(case_id, "case_id")
    body = await body_model(request, ReleaseRequest)
    check_idempotency_header(request, body.idempotency_key)
    tool = body.to_tool_input(target)

    async def work(conn: Conn, actor: ActorContext) -> tuple[Any, datetime]:
        view = await reviews_repo.release(conn, actor, tool.case_id, tool.claim_token, tool.idempotency_key)
        return view, await _now(conn)

    view, as_of = await _read(request, auth, work)
    return _respond(RELEASE, _mutation_envelope(view, auth, as_of))


def _action_code(code: str) -> str:
    """Reason codes compared as the dashboard action they spell (``NEEDS-DOCUMENTS`` included)."""
    return _CODE_SEPARATORS.sub("_", code.lower())


def check_dashboard_actions(tool: ReviewsSubmitInput) -> None:
    """Spec 19 dashboard actions are ``needs_information`` decisions only (module docstring).

    The codes are matched case- and separator-insensitively, so a variant spelling such as
    ``NEEDS_INSPECTION`` or ``needs-documents`` cannot attach an action to another outcome.
    """
    if tool.outcome != ReviewOutcome.NEEDS_INFORMATION and any(
        _action_code(code) in DASHBOARD_ACTION_REASON_CODES for code in tool.reason_codes
    ):
        raise ValidationFailed(
            "Inspection, document and price-confirmation actions are needs_information decisions",
            details={"fields": ["outcome", "reason_codes"]},
        )


def dashboard_action_request(
    action: DashboardAction,
    *,
    claim_token: str,
    expected_version: int,
    listing_revision: int,
    missing_information: Sequence[str],
    idempotency_key: str,
    valuation_id: UUID | None = None,
    evidence_ids: Sequence[UUID] = (),
    summary: str | None = None,
    extra_reason_codes: Sequence[str] = (),
) -> SubmitReviewRequest:
    """The submit body of one spec 19 dashboard action (what the dashboard sends)."""
    codes = (DashboardAction(action).value, *(c for c in extra_reason_codes if c != action.value))
    return SubmitReviewRequest(
        claim_token=claim_token,
        expected_version=expected_version,
        listing_revision=listing_revision,
        valuation_id=valuation_id,
        outcome=ReviewOutcome.NEEDS_INFORMATION,
        reason_codes=codes,
        summary=summary or DASHBOARD_ACTION_SUMMARIES[DashboardAction(action)],
        evidence_ids=tuple(evidence_ids),
        missing_information=tuple(missing_information),
        idempotency_key=idempotency_key,
    )


@router.post("/api/reviews/{case_id}/submit")
async def post_submit(request: Request, case_id: str, auth: Authenticated) -> Response:
    require_scope(request, auth, SUBMIT.scope)
    no_query(request)
    target = path_id(case_id, "case_id")
    body = await body_model(request, SubmitReviewRequest)
    check_idempotency_header(request, body.idempotency_key)
    tool = body.to_tool_input(target)
    check_dashboard_actions(tool)
    submission = tool.to_submit_request()
    base_url = api_state(request).settings.app_base_url

    async def work(conn: Conn, actor: ActorContext) -> tuple[Any, datetime]:
        view = await reviews_repo.submit(conn, actor, submission, dashboard_base_url=base_url)
        return view, await _now(conn)

    view, as_of = await _read(request, auth, work)
    warnings = [warning(WarningCode.FIXTURE_DATA)] if view.is_fixture else []
    return _respond(SUBMIT, _mutation_envelope(view, auth, as_of, warnings))


# --------------------------------------------------------------------------------------------
# Listing actions
# --------------------------------------------------------------------------------------------


@router.post("/api/listings/{listing_id}/notes")
async def post_note(request: Request, listing_id: str, auth: Authenticated) -> Response:
    require_scope(request, auth, NOTES.scope)
    no_query(request)
    target = path_id(listing_id, "listing_id")
    body = await body_model(request, AddNoteRequest)
    check_idempotency_header(request, body.idempotency_key)
    tool = body.to_tool_input(target)

    async def work(conn: Conn, actor: ActorContext) -> tuple[Any, datetime]:
        view = await notes_repo.add_note(conn, actor, tool.listing_id, tool.note, tool.idempotency_key)
        return view, await _now(conn)

    view, as_of = await _read(request, auth, work)
    return _respond(NOTES, _mutation_envelope(view, auth, as_of))


async def request_recheck(
    conn: Conn, actor: ActorContext, tool: DealsRequestRecheckInput
) -> RecheckRequestResult:
    """``deals_request_recheck`` in the caller's transaction (see module docstring).

    Lock order: idempotency record -> ``app.sources`` (share) -> ``app.listings`` -> new job row ->
    audit (insert-only, last).
    """
    actor.require(Scope.RECHECKS_REQUEST)
    request_hash = idempotency.request_hash_for(RECHECK_OPERATION, tool)
    replay = await reviews_repo.begin_idempotent(
        conn, actor, RECHECK_OPERATION, tool.idempotency_key, request_hash
    )
    if replay is not None:
        return RecheckRequestResult.model_validate(replay)
    listing = await listings_repo.get_listing(conn, actor, tool.listing_id)
    source = await lock_source(conn, actor.workspace_id, listing.source_id, for_network=False)
    if source.technical_status == TechnicalStatus.ACCESS_BLOCKED:
        raise AccessBlocked()
    if not source.enabled or source.paused or source.technical_status == TechnicalStatus.PARSER_UNHEALTHY:
        raise SourcePaused()
    ref = await listings_repo.request_detail_refresh(
        conn, actor, tool.listing_id, reason=f"recheck: {tool.reason}", job_type=JobType.RECHECK
    )
    if ref is not None:
        job = await jobs.get_job(conn, actor, ref.job_id)
        result = RecheckRequestResult(
            job_id=job.id,
            listing_id=tool.listing_id,
            state=job.state,
            deduplicated=not ref.created,
            available_at=job.available_at,
        )
    else:
        async with mapped_errors():
            waiting = await fetch_one(
                conn, _WAITING_JOB_SQL, {"ws": actor.workspace_id, "listing_id": tool.listing_id}
            )
        if waiting is None:  # pragma: no cover - request_detail_refresh only dedupes a waiting job
            raise AppError(ErrorCode.INTERNAL_ERROR, "The waiting recheck could not be read", retryable=True)
        result = RecheckRequestResult(
            job_id=waiting["id"],
            listing_id=tool.listing_id,
            state=JobState(waiting["state"]),
            deduplicated=True,
            available_at=ensure_utc(waiting["available_at"]),
        )
    async with mapped_errors():
        await audit.record(
            conn,
            actor,
            "recheck.request",
            "listing",
            tool.listing_id,
            reason=tool.reason,
            metadata={"job_id": str(result.job_id), "deduplicated": result.deduplicated},
        )
    await idempotency.complete(
        conn, actor, RECHECK_OPERATION, tool.idempotency_key, result.model_dump(mode="json")
    )
    return result


@router.post("/api/listings/{listing_id}/recheck")
async def post_recheck(request: Request, listing_id: str, auth: Authenticated) -> Response:
    require_scope(request, auth, RECHECK.scope)
    no_query(request)
    target = path_id(listing_id, "listing_id")
    body = await body_model(request, RecheckRequest)
    check_idempotency_header(request, body.idempotency_key)
    tool = body.to_tool_input(target)

    async def work(conn: Conn, actor: ActorContext) -> tuple[RecheckRequestResult, datetime]:
        result = await request_recheck(conn, actor, tool)
        return result, await _now(conn)

    view, as_of = await _read(request, auth, work)
    return _respond(RECHECK, _mutation_envelope(view, auth, as_of))


# --------------------------------------------------------------------------------------------
# Sources, settings, outbox
# --------------------------------------------------------------------------------------------


@router.get("/api/sources")
async def get_sources(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, SOURCES.scope)
    no_query(request)
    result = await _read(request, auth, queries.sources_view)
    return _respond(SOURCES, result.envelope(auth.request_id))


@router.post("/api/sources/{source_id}/pause")
async def post_pause(request: Request, source_id: str, auth: Authenticated) -> Response:
    require_scope(request, auth, PAUSE.scope)
    no_query(request)
    target = path_id(source_id, "source_id")
    body = await body_model(request, PauseSourceRequest)
    check_idempotency_header(request, body.idempotency_key)
    tool = body.to_tool_input(target)

    async def work(conn: Conn, actor: ActorContext) -> tuple[Any, datetime]:
        view = await sources_repo.pause_source(
            conn, actor, tool.source_id, tool.expected_version, tool.reason, tool.idempotency_key
        )
        return view, await _now(conn)

    view, as_of = await _read(request, auth, work)
    warnings = [warning(WarningCode.SOURCE_PAUSED)]
    return _respond(PAUSE, _mutation_envelope(view, auth, as_of, warnings))


@router.get("/api/settings")
async def get_settings_view(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, SETTINGS.scope)
    no_query(request)
    fallback = api_state(request).fallback_config
    result = await _read(
        request, auth, lambda conn, actor: queries.settings_view(conn, actor, fallback_config=fallback)
    )
    return _respond(SETTINGS, result.envelope(auth.request_id))


@router.get("/api/outbox")
async def get_outbox(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, OUTBOX.scope)
    query = query_model(request, OutboxQuery)
    secret = api_state(request).cursor_secret()
    result = await _read(
        request, auth, lambda conn, actor: queries.outbox_attention_view(conn, actor, query, secret=secret)
    )
    return _respond(OUTBOX, result.envelope(auth.request_id))


# --------------------------------------------------------------------------------------------
# Fallback for unknown /api paths (included after any extension routers, before the MCP mount)
# --------------------------------------------------------------------------------------------

fallback_router = APIRouter()
_FALLBACK_METHODS: Final = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_ROUTE_PATTERNS: Final = tuple(
    (re.compile("^" + re.sub(r"\{[^/]+\}", "[^/]+", route.path) + "$"), route.method) for route in ROUTES
)


async def api_fallback(request: Request) -> Response:
    """``404 NOT_FOUND`` (or ``405`` for a known path with another method) as an API error body,
    so an unknown ``/api`` path never falls through to the MCP mount at ``/``."""
    path = request.url.path
    allowed = sorted({method for pattern, method in _ROUTE_PATTERNS if pattern.fullmatch(path)})
    if allowed:
        raise method_not_allowed(allowed)
    raise AppError(ErrorCode.NOT_FOUND, "Not found")


fallback_router.add_api_route("/api", api_fallback, methods=_FALLBACK_METHODS, include_in_schema=False)
fallback_router.add_api_route(
    "/api/{rest:path}", api_fallback, methods=_FALLBACK_METHODS, include_in_schema=False
)


__all__ = [
    "DASHBOARD_ACTION_REASON_CODES",
    "DASHBOARD_ACTION_SUMMARIES",
    "RECHECK_OPERATION",
    "check_dashboard_actions",
    "dashboard_action_request",
    "fallback_router",
    "json_response",
    "readiness_view",
    "request_recheck",
    "router",
]
