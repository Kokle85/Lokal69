"""Dashboard BFF request/response contracts and the route table (docs/api_contract.md).

- Authentication: ``Authorization: Bearer <Supabase user access token>`` only (no cookies, so no
  CSRF surface). The server verifies the JWT, resolves an *active* membership, derives the role's
  scopes (``domain.actor.ROLE_SCOPES``) and checks the route's scope.
- Every response body is a ``ResponseEnvelope`` (except ``/healthz`` and ``/readyz``); errors are
  ``ApiErrorResponse`` with the spec 21 codes mapped through ``errors.HTTP_STATUS``.
- Mutations reuse the MCP tool input models: each body converts to the tool input with the id
  from the path (``to_tool_input``), so validation, idempotency keys and expected versions are
  identical on both surfaces.
- Query models are the lax (string-parsing) counterparts of the MCP list inputs and convert to
  them before use, so the same constraints apply.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from suv_deals.domain.enums import ProfileKey, ReviewOutcome, Scope
from suv_deals.errors import HTTP_STATUS, AppError, ErrorCode, ValidationFailed
from suv_deals.mcp.schemas import (
    CaseVersion,
    ClaimToken,
    Cursor,
    DealsAddNoteInput,
    DealsGetCandidateInput,
    DealsGetComparablesInput,
    DealsListCandidatesInput,
    DealsRequestRecheckInput,
    EvidenceIds,
    Id,
    IdempotencyKey,
    ListingRevisionNumber,
    MissingInformation,
    ModelRunId,
    NoteText,
    Reason,
    ReasonCodes,
    ReviewsClaimInput,
    ReviewsListPendingInput,
    ReviewsReleaseInput,
    ReviewsSubmitInput,
    SourcesPauseInput,
    SourceVersion,
    SummaryText,
    ToolInput,
    ValuationIdRef,
)
from suv_deals.views.candidates import CandidateDetail, CandidateListView
from suv_deals.views.common import (
    SCHEMA_VERSION,
    ErrorPayload,
    ResponseEnvelope,
    UtcDatetime,
    ViewModel,
    envelope_model_for,
)
from suv_deals.views.comparables import ComparableSetView
from suv_deals.views.notes import NoteView, RecheckRequestResult
from suv_deals.views.operations import (
    LivenessView,
    MeView,
    OutboxPage,
    OverviewView,
    ReadinessView,
    SettingsView,
    SourceListView,
    SourcePauseResult,
)
from suv_deals.views.reviews import (
    ClaimResult,
    ReleaseResultView,
    ReviewCaseView,
    ReviewDecisionView,
    ReviewQueuePage,
)
from suv_deals.views.valuations import ValuationView

#: Path parameter type for every ``{..._id}`` segment: canonical UUID string.
PathId = Id

_LaxLimit = Annotated[int, Field(ge=1, le=100)]


def _to_input[InputT: ToolInput](model: type[InputT], data: Mapping[str, Any], name: str) -> InputT:
    try:
        return model.model_validate(dict(data))
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) or "request" for err in exc.errors()})
        raise ValidationFailed(f"Invalid {name} request", details={"fields": fields[:20]}) from None


# --------------------------------------------------------------------------- query models


class ApiQuery(BaseModel):
    """Query-string parameters (strings parsed into types); unknown parameters are refused."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class CandidateListQuery(ApiQuery):
    cursor: Cursor = None
    limit: _LaxLimit = 25
    profile: ProfileKey | None = None
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    status: Literal["pending", "needs_information", "watch", "shortlisted", "rejected"] | None = None
    changed_since: datetime | None = None

    def to_tool_input(self) -> DealsListCandidatesInput:
        data = self.model_dump(exclude_none=True)
        return _to_input(DealsListCandidatesInput, data, "candidate list")


class CandidateDetailQuery(ApiQuery):
    revision: Annotated[int, Field(ge=1)] | None = None

    def to_tool_input(self, listing_id: UUID) -> DealsGetCandidateInput:
        data = {"listing_id": listing_id, **self.model_dump(exclude_none=True)}
        return _to_input(DealsGetCandidateInput, data, "candidate")


class ComparablesQuery(ApiQuery):
    include_excluded: bool = False
    cursor: Cursor = None
    limit: _LaxLimit = 25

    def to_tool_input(self, comparable_set_id: UUID) -> DealsGetComparablesInput:
        data = {"comparable_set_id": comparable_set_id, **self.model_dump()}
        return _to_input(DealsGetComparablesInput, data, "comparables")


class ReviewQueueQuery(ApiQuery):
    cursor: Cursor = None
    limit: _LaxLimit = 25
    include_needs_information: bool = True

    def to_tool_input(self) -> ReviewsListPendingInput:
        return _to_input(ReviewsListPendingInput, self.model_dump(), "review queue")


OutboxAttentionState = Literal["uncertain", "blocked", "dead_letter", "retry_wait"]


class OutboxQuery(ApiQuery):
    """Failed or uncertain deliveries only; delivered/pending rows are not listed here."""

    cursor: Cursor = None
    limit: _LaxLimit = 25
    state: OutboxAttentionState | None = None

    def filters(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"cursor", "limit"}, exclude_none=True)


# --------------------------------------------------------------------------- mutation bodies


class ClaimRequest(ToolInput):
    """Body of ``POST /api/reviews/{case_id}/claim`` (= ``reviews_claim`` minus the path id)."""

    expected_version: CaseVersion
    idempotency_key: IdempotencyKey

    def to_tool_input(self, case_id: UUID) -> ReviewsClaimInput:
        return _to_input(ReviewsClaimInput, {"case_id": case_id, **self.model_dump()}, "claim")


class ReleaseRequest(ToolInput):
    """Body of ``POST /api/reviews/{case_id}/release`` (= ``reviews_release`` minus the path id)."""

    claim_token: ClaimToken = Field(repr=False)
    idempotency_key: IdempotencyKey

    def to_tool_input(self, case_id: UUID) -> ReviewsReleaseInput:
        return _to_input(ReviewsReleaseInput, {"case_id": case_id, **self.model_dump()}, "release")


class SubmitReviewRequest(ToolInput):
    """Body of ``POST /api/reviews/{case_id}/submit`` (= ``reviews_submit`` minus the path id).

    The domain submission rules (reason-code format, unique ids, summary hygiene) run in
    ``to_tool_input``, exactly as for the MCP tool.
    """

    claim_token: ClaimToken = Field(repr=False)
    expected_version: CaseVersion
    listing_revision: ListingRevisionNumber
    valuation_id: ValuationIdRef = None
    outcome: ReviewOutcome
    reason_codes: ReasonCodes
    summary: SummaryText
    evidence_ids: EvidenceIds
    missing_information: MissingInformation = ()
    model_run_id: ModelRunId = None
    idempotency_key: IdempotencyKey

    def to_tool_input(self, case_id: UUID) -> ReviewsSubmitInput:
        return _to_input(ReviewsSubmitInput, {"case_id": case_id, **self.model_dump()}, "submit")


class AddNoteRequest(ToolInput):
    """Body of ``POST /api/listings/{listing_id}/notes`` (= ``deals_add_note`` minus the path id)."""

    note: NoteText
    idempotency_key: IdempotencyKey

    def to_tool_input(self, listing_id: UUID) -> DealsAddNoteInput:
        return _to_input(DealsAddNoteInput, {"listing_id": listing_id, **self.model_dump()}, "note")


class RecheckRequest(ToolInput):
    """Body of ``POST /api/listings/{listing_id}/recheck`` (= ``deals_request_recheck`` minus the path id)."""

    reason: Reason
    idempotency_key: IdempotencyKey

    def to_tool_input(self, listing_id: UUID) -> DealsRequestRecheckInput:
        return _to_input(DealsRequestRecheckInput, {"listing_id": listing_id, **self.model_dump()}, "recheck")


class PauseSourceRequest(ToolInput):
    """Body of ``POST /api/sources/{source_id}/pause`` (= ``sources_pause`` minus the path id)."""

    expected_version: SourceVersion
    reason: Reason
    idempotency_key: IdempotencyKey

    def to_tool_input(self, source_id: UUID) -> SourcesPauseInput:
        return _to_input(SourcesPauseInput, {"source_id": source_id, **self.model_dump()}, "pause")


# --------------------------------------------------------------------------- errors


class ApiErrorResponse(ViewModel):
    """Error body for every failed ``/api`` request (same codes as MCP tool errors)."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    request_id: str = Field(pattern=r"^[\x21-\x7e]{1,200}$")
    as_of: UtcDatetime
    error: ErrorPayload


def api_error(error: AppError, *, request_id: str, as_of: datetime) -> tuple[int, ApiErrorResponse]:
    """HTTP status (``errors.HTTP_STATUS``) and safe body for an ``AppError``."""
    body = ApiErrorResponse(
        request_id=request_id,
        as_of=as_of,
        error=ErrorPayload.from_app_error(error, correlation_id=request_id),
    )
    return HTTP_STATUS[error.code], body


# --------------------------------------------------------------------------- routes

Auth = Literal["none", "user_jwt"]
RequestLocation = Literal["query", "body"]

COMMON_ERRORS: Final[tuple[ErrorCode, ...]] = (
    ErrorCode.UNAUTHENTICATED,
    ErrorCode.FORBIDDEN,
    ErrorCode.VALIDATION_ERROR,
    ErrorCode.RATE_LIMITED,
    ErrorCode.DEPENDENCY_UNAVAILABLE,
    ErrorCode.INTERNAL_ERROR,
)


@dataclass(frozen=True, slots=True)
class ApiRoute:
    method: Literal["GET", "POST"]
    path: str
    auth: Auth
    scope: Scope | None
    response_model: type[BaseModel]
    data_model: type[BaseModel] | None
    request_model: type[BaseModel] | None = None
    request_location: RequestLocation | None = None
    success_status: int = 200
    errors: tuple[ErrorCode, ...] = ()
    paginated: bool = False
    mcp_tool: str | None = None
    summary: str = ""

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def path_params(self) -> tuple[str, ...]:
        return tuple(
            part[1:-1] for part in self.path.split("/") if part.startswith("{") and part.endswith("}")
        )


def _r(
    method: Literal["GET", "POST"],
    path: str,
    data: type[BaseModel],
    *,
    scope: Scope | None,
    summary: str,
    request: type[BaseModel] | None = None,
    location: RequestLocation | None = None,
    status: int = 200,
    errors: tuple[ErrorCode, ...] = (),
    paginated: bool = False,
    tool: str | None = None,
) -> ApiRoute:
    return ApiRoute(
        method=method,
        path=path,
        auth="user_jwt",
        scope=scope,
        response_model=envelope_model_for(data),
        data_model=data,
        request_model=request,
        request_location=location,
        success_status=status,
        errors=(*COMMON_ERRORS, *errors),
        paginated=paginated,
        mcp_tool=tool,
        summary=summary,
    )


_NF = (ErrorCode.NOT_FOUND,)
_WRITE = (ErrorCode.NOT_FOUND, ErrorCode.IDEMPOTENCY_CONFLICT)

ROUTES: Final[tuple[ApiRoute, ...]] = (
    ApiRoute(
        method="GET",
        path="/healthz",
        auth="none",
        scope=None,
        response_model=LivenessView,
        data_model=None,
        errors=(),
        summary="Process liveness only (no dependencies checked).",
    ),
    ApiRoute(
        method="GET",
        path="/readyz",
        auth="none",
        scope=None,
        response_model=ReadinessView,
        data_model=None,
        errors=(),
        summary="Readiness: database, schema compatibility and critical configuration; 503 when not ready.",
    ),
    _r(
        "GET",
        "/api/me",
        MeView,
        scope=None,
        summary="Authenticated principal, workspace, role, scopes, memberships.",
    ),
    _r("GET", "/api/overview", OverviewView, scope=Scope.DEALS_READ, summary="Overview screen."),
    _r(
        "GET",
        "/api/candidates",
        CandidateListView,
        scope=Scope.DEALS_READ,
        summary="Candidate queue with filters and keyset pagination.",
        request=CandidateListQuery,
        location="query",
        paginated=True,
        tool="deals_list_candidates",
    ),
    _r(
        "GET",
        "/api/candidates/{listing_id}",
        CandidateDetail,
        scope=Scope.DEALS_READ,
        summary="Candidate detail at the current or requested revision.",
        request=CandidateDetailQuery,
        location="query",
        errors=_NF,
        tool="deals_get_candidate",
    ),
    _r(
        "GET",
        "/api/comparables/{set_id}",
        ComparableSetView,
        scope=Scope.DEALS_READ,
        summary="Comparable set with paginated members.",
        request=ComparablesQuery,
        location="query",
        errors=_NF,
        paginated=True,
        tool="deals_get_comparables",
    ),
    _r(
        "GET",
        "/api/valuations/{valuation_id}",
        ValuationView,
        scope=Scope.DEALS_READ,
        summary="Versioned valuation breakdown.",
        errors=_NF,
        tool="deals_get_valuation",
    ),
    _r(
        "GET",
        "/api/reviews",
        ReviewQueuePage,
        scope=Scope.REVIEWS_READ,
        summary="Pending review queue (frozen snapshot pagination).",
        request=ReviewQueueQuery,
        location="query",
        paginated=True,
        tool="reviews_list_pending",
    ),
    _r(
        "GET",
        "/api/reviews/{case_id}",
        ReviewCaseView,
        scope=Scope.REVIEWS_READ,
        summary="One review case with candidate, valuation reference and decision history.",
        errors=_NF,
    ),
    _r(
        "POST",
        "/api/reviews/{case_id}/claim",
        ClaimResult,
        scope=Scope.REVIEWS_WRITE,
        summary="Claim the current case version.",
        request=ClaimRequest,
        location="body",
        errors=(*_WRITE, ErrorCode.VERSION_CONFLICT, ErrorCode.ALREADY_CLAIMED),
        tool="reviews_claim",
    ),
    _r(
        "POST",
        "/api/reviews/{case_id}/release",
        ReleaseResultView,
        scope=Scope.REVIEWS_WRITE,
        summary="Release the caller's own claim (idempotent).",
        request=ReleaseRequest,
        location="body",
        errors=(*_WRITE, ErrorCode.CLAIM_EXPIRED),
        tool="reviews_release",
    ),
    _r(
        "POST",
        "/api/reviews/{case_id}/submit",
        ReviewDecisionView,
        scope=Scope.REVIEWS_WRITE,
        summary="Submit a decision against exact versions.",
        request=SubmitReviewRequest,
        location="body",
        status=201,
        errors=(*_WRITE, ErrorCode.VERSION_CONFLICT, ErrorCode.ALREADY_CLAIMED, ErrorCode.CLAIM_EXPIRED),
        tool="reviews_submit",
    ),
    _r(
        "POST",
        "/api/listings/{listing_id}/notes",
        NoteView,
        scope=Scope.NOTES_WRITE,
        summary="Append a private labelled note.",
        request=AddNoteRequest,
        location="body",
        status=201,
        errors=_WRITE,
        tool="deals_add_note",
    ),
    _r(
        "POST",
        "/api/listings/{listing_id}/recheck",
        RecheckRequestResult,
        scope=Scope.RECHECKS_REQUEST,
        summary="Queue a bounded recheck; returns the job id.",
        request=RecheckRequest,
        location="body",
        status=202,
        errors=(*_WRITE, ErrorCode.SOURCE_PAUSED, ErrorCode.ACCESS_BLOCKED),
        tool="deals_request_recheck",
    ),
    _r("GET", "/api/sources", SourceListView, scope=Scope.DEALS_READ, summary="Source status screen."),
    _r(
        "POST",
        "/api/sources/{source_id}/pause",
        SourcePauseResult,
        scope=Scope.SOURCES_PAUSE,
        summary="Pause one source with a reason (never resumes).",
        request=PauseSourceRequest,
        location="body",
        errors=(*_WRITE, ErrorCode.VERSION_CONFLICT),
        tool="sources_pause",
    ),
    _r("GET", "/api/settings", SettingsView, scope=Scope.DEALS_READ, summary="Settings screen (read-only)."),
    _r(
        "GET",
        "/api/outbox",
        OutboxPage,
        scope=Scope.REVIEWS_READ,
        summary="Failed, blocked and uncertain deliveries (no payloads).",
        request=OutboxQuery,
        location="query",
        paginated=True,
    ),
)

ROUTE_INDEX: Final[Mapping[str, ApiRoute]] = MappingProxyType({route.key: route for route in ROUTES})

__all__ = [
    "COMMON_ERRORS",
    "ROUTES",
    "ROUTE_INDEX",
    "AddNoteRequest",
    "ApiErrorResponse",
    "ApiQuery",
    "ApiRoute",
    "CandidateDetailQuery",
    "CandidateListQuery",
    "ClaimRequest",
    "ComparablesQuery",
    "OutboxQuery",
    "PathId",
    "PauseSourceRequest",
    "RecheckRequest",
    "ReleaseRequest",
    "ResponseEnvelope",
    "ReviewQueueQuery",
    "SubmitReviewRequest",
    "api_error",
]
