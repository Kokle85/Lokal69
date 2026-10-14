"""A listing's recorded import values (owner decision 2026-10-13: two separate values per vehicle).

Routes (``api.schemas.V11_DASHBOARD_ROUTES``; Supabase user JWT; never an MCP tool). Same handler
shape as ``api.owner_config_routes``: authenticate, check the scope, parse the closed model, run
ONE short workspace-scoped transaction and answer with the shared ``ResponseEnvelope``.

- ``GET /api/listings/{listing_id}/import-values`` (``deals:read``): the listing's CURRENT recorded
  real purchase price and estimated customs value plus a short history
  (``import_values_repo.current_import_values``). A missing or foreign-workspace listing is
  ``404``.
- ``POST /api/listings/{listing_id}/import-values`` (``config:admin``: the workspace OWNER only;
  reviewers and viewers get ``403``; the repository also refuses anything but a signed-in user):
  record the real purchase price (``purchase_price``: amount in EUR, kind actual/quote/estimate,
  source, optional document reference) and/or the estimated customs value
  (``estimated_customs_value``: ``amount_eur`` WITHOUT transport, source, optional document
  reference); at least one. Each supersedes the current value of its kind; the listing's
  valuations are marked stale and its recomputation is queued
  (``import_values_repo.record_import_values``, audited ``import_values.record``). The
  ``idempotency_key`` is in the body (an ``Idempotency-Key`` header, when sent, must equal it) and
  is bound to the listing: a retry with the same key replays the original result, a failure
  consumes nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ValidationError
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
    require_scope,
)
from suv_deals.api.errors import JSON_MEDIA_TYPE
from suv_deals.api.schemas import V11_ROUTE_INDEX, ApiRoute, ImportValuesRecordRequest
from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.errors import AppError, ErrorCode, ValidationFailed
from suv_deals.mcp.schemas import validation_error_fields
from suv_deals.persistence import idempotency, import_values_repo
from suv_deals.persistence.database import Conn, db_now
from suv_deals.persistence.errors_map import TransientConflict
from suv_deals.persistence.import_values_repo import (
    ImportValueRecord,
    ImportValues,
    ImportValuesRecorded,
    RecordedCustomsValue,
    RecordedPurchase,
)
from suv_deals.views.common import AmountView, ResponseEnvelope, decimal_str, envelope
from suv_deals.views.import_values import ImportValueRecordView, ImportValuesRecordResult, ImportValuesView

router = APIRouter()

RECORD_OPERATION: Final = "owner_import_values_record"
IMPORT_VALUES: Final[ApiRoute] = V11_ROUTE_INDEX["GET /api/listings/{listing_id}/import-values"]
RECORD: Final[ApiRoute] = V11_ROUTE_INDEX["POST /api/listings/{listing_id}/import-values"]


# --------------------------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------------------------


def record_view(record: ImportValueRecord) -> ImportValueRecordView:
    """One recorded value: ``amount`` in whole euros (half-up), ``amount_exact`` as recorded."""
    return ImportValueRecordView(
        id=record.id,
        category=record.category,
        kind=record.kind,
        amount=AmountView.of(record.amount, whole=True),
        amount_exact=decimal_str(record.amount.quantized()),
        source=record.source[:120],
        document_ref=record.document_ref,
        supersedes_id=record.supersedes_id,
        current=record.current,
        recorded_by_principal=record.recorded_by,
        recorded_at=record.recorded_at,
        is_fixture=record.is_fixture,
    )


def values_view(values: ImportValues) -> ImportValuesView:
    return ImportValuesView(
        listing_id=values.listing_id,
        purchase_price=None if values.purchase is None else record_view(values.purchase),
        estimated_customs_value=None if values.customs_value is None else record_view(values.customs_value),
        history=tuple(record_view(r) for r in values.history[:50]),
    )


def record_result(result: ImportValuesRecorded) -> ImportValuesRecordResult:
    return ImportValuesRecordResult(
        values=values_view(result.values),
        recorded_ids=result.recorded_ids,
        superseded_ids=result.superseded_ids,
        stale_valuation_ids=result.stale_valuation_ids[:1000],
        recompute_job_id=result.recompute_job_id,
    )


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _respond(route: ApiRoute, body: ResponseEnvelope[Any]) -> Response:
    return Response(
        content=body.model_dump_json(), status_code=route.success_status, media_type=JSON_MEDIA_TYPE
    )


def _replay_error(code: str) -> AppError:
    try:
        error_code = ErrorCode(code)
    except ValueError:
        error_code = ErrorCode.INTERNAL_ERROR
    return AppError(error_code, "The original request with this idempotency key failed")


async def _run[T](request: Request, auth: AuthContext, work: Callable[[Conn], Awaitable[T]]) -> T:
    return await in_transaction(api_state(request), auth.actor, work)


def _fields(exc: ValidationError, part: str, model: type[BaseModel]) -> list[str]:
    """Body field paths of a repository-input failure (``purchase_price.amount``), never values."""
    paths = validation_error_fields(exc, root=part, model=model)
    return [p if p == part else f"{part}.{p}" for p in paths] or [part]


def repository_inputs(
    body: ImportValuesRecordRequest,
) -> tuple[RecordedPurchase | None, RecordedCustomsValue | None]:
    """The request's values as repository inputs (amounts must be more than zero and within the
    engine's input bound; failures name the body fields)."""
    price, estimate = body.purchase_price, body.estimated_customs_value
    try:
        purchase = (
            None
            if price is None
            else RecordedPurchase(
                amount=Decimal(price.amount),
                currency=price.currency,
                kind=price.kind,
                source=price.source,
                document_ref=price.document_ref,
            )
        )
    except ValidationError as exc:
        fields = _fields(exc, "purchase_price", RecordedPurchase)
        raise ValidationFailed("Invalid purchase price", details={"fields": fields}) from None
    try:
        customs = (
            None
            if estimate is None
            else RecordedCustomsValue(
                amount_eur=Decimal(estimate.amount_eur),
                source=estimate.source,
                document_ref=estimate.document_ref,
            )
        )
    except ValidationError as exc:
        fields = _fields(exc, "estimated_customs_value", RecordedCustomsValue)
        raise ValidationFailed("Invalid estimated customs value", details={"fields": fields}) from None
    return purchase, customs


async def record_values(
    conn: Conn, actor: ActorContext, listing_id: UUID, body: ImportValuesRecordRequest
) -> ImportValuesRecordResult:
    """Record in the transaction of the idempotency record (same key + same listing and values
    replays the stored result; a failure rolls the record back with everything else)."""
    request_hash = idempotency.request_hash_for(
        RECORD_OPERATION, {"listing_id": str(listing_id), "body": body.model_dump(mode="json")}
    )
    started = await idempotency.begin(conn, actor, RECORD_OPERATION, body.idempotency_key, request_hash)
    if isinstance(started, idempotency.Replay):
        return ImportValuesRecordResult.model_validate(started.result)
    if isinstance(started, idempotency.ReplayError):
        raise _replay_error(started.error_code)
    if isinstance(started, idempotency.InProgress):
        raise TransientConflict.in_progress()
    purchase, customs = repository_inputs(body)
    recorded = await import_values_repo.record_import_values(
        conn, actor, listing_id, purchase=purchase, customs_value=customs
    )
    result = record_result(recorded)
    await idempotency.complete(
        conn, actor, RECORD_OPERATION, body.idempotency_key, result.model_dump(mode="json")
    )
    return result


async def _now(conn: Conn) -> datetime:
    return ensure_utc(await db_now(conn))


# --------------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------------


@router.get("/api/listings/{listing_id}/import-values")
async def get_import_values(request: Request, auth: Authenticated, listing_id: str) -> Response:
    require_scope(request, auth, IMPORT_VALUES.scope)
    no_query(request)
    target = path_id(listing_id, "listing_id")

    async def work(conn: Conn) -> tuple[ImportValuesView, datetime]:
        values = await import_values_repo.current_import_values(conn, auth.actor, target)
        return values_view(values), await _now(conn)

    view, as_of = await _run(request, auth, work)
    return _respond(IMPORT_VALUES, envelope(view, request_id=auth.request_id, as_of=as_of))


@router.post("/api/listings/{listing_id}/import-values")
async def post_import_values(request: Request, auth: Authenticated, listing_id: str) -> Response:
    require_scope(request, auth, RECORD.scope)
    no_query(request)
    target = path_id(listing_id, "listing_id")
    body = await body_model(request, ImportValuesRecordRequest)
    check_idempotency_header(request, body.idempotency_key)

    async def work(conn: Conn) -> tuple[ImportValuesRecordResult, datetime]:
        result = await record_values(conn, auth.actor, target, body)
        return result, await _now(conn)

    view, as_of = await _run(request, auth, work)
    return _respond(RECORD, envelope(view, request_id=auth.request_id, as_of=as_of))


__all__ = [
    "IMPORT_VALUES",
    "RECORD",
    "RECORD_OPERATION",
    "record_result",
    "record_values",
    "record_view",
    "repository_inputs",
    "router",
    "values_view",
]
