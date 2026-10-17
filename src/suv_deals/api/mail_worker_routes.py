"""The mailbox-worker API under ``/v1/mail-workers`` (spec 37.8; ``api.schemas.MAIL_WORKER_ROUTES``).

Used by the Windows desktop worker (``desktop/outlook-bridge``). Bodies and responses are the
desktop wire models exactly (top-level JSON objects, no ``ResponseEnvelope``); errors are the
shared ``ApiErrorResponse`` bodies.

Authentication (`authenticate_worker`):

1. The client's failed-authentication budget (``deps.ApiState.preauth``) is checked first: a
   client flooding bad credentials is ``429`` before any hashing or database work.
2. ``Authorization: Bearer suvmail_<64 hex>`` only (exactly one header; never a query string,
   cookie or body). The token FORMAT is checked CPU-only (``credentials_repo.token_kind``): any
   other token kind (a dashboard JWT, an MCP ``suvmcp_`` credential) is ``401`` without touching
   the database.
3. The per-credential rate limit is keyed by the token's SHA-256 (also before the database).
4. ``mail_workers_repo.resolve_worker`` runs in a transaction opened WITHOUT a workspace: the
   credential alone names workspace and mailbox (a request can never select either). Revoked or
   expired: ``401`` ``mail_worker_credential_revoked`` (the worker stops transmitting and keeps
   its backlog); an unknown token: ``401``; a revoked mailbox binding: ``403``.

Routes (each runs ONE short transaction of the worker's workspace unless noted):

- ``GET /inquiry-bindings``: the worker's own mailbox binding changes after an opaque signed
  cursor (``limit`` <= 100; tombstones included; uncertain sends publish their intent Message-IDs).
- ``POST /replies``: ``Idempotency-Key`` REQUIRED (``400`` without), checked together with the
  stable source identity by ``replies_repo.ingest`` (its own unit of work). An unknown or foreign
  inquiry is ``403`` ``mailbox_binding_mismatch``; the same message again is ``200`` with
  ``duplicate: true``; a conflicting upload is ``409``. A per-credential reply bucket tighter than
  the mutation bucket (``deps.MAIL_WORKER_REPLY_LIMIT``, PROPOSED 30 in a burst then 10 per
  minute) and the repository's hourly cap of new replies per mailbox (``details.reason =
  mail_worker_ingest_volume``) answer ``429`` with ``Retry-After``.
- ``GET /send-intents``: pending intents of the worker's mailbox plus reaped, never-claimed,
  unreported ones flagged ``expired: true`` (the worker reports them ``intent_expired``).
  ``kill_switch_active`` is also ``true`` while this process's settings forbid sending
  (`process_gate`).
- ``POST /send-intents/{intent_id}/claim``: ``Idempotency-Key`` required, but never stored or
  replayed: a claim is ALWAYS evaluated fresh (``send_intents_repo.claim``). It is the last guard
  before ``.Send``: while ``SELLER_INQUIRY_MODE`` is not ``automatic`` or
  ``SELLER_INQUIRY_KILL_SWITCH`` is on, every claim is refused ``kill_switch`` (audited), even for
  an intent committed earlier (owner decision: nothing is sent unless the mode is automatic and
  the kill switch is off).
- ``POST /send-intents/{intent_id}/report``: ``Idempotency-Key`` required; the same key and report
  replay the acknowledgement (``persistence.idempotency``), another report under the same key is
  ``409``. The path id must equal the body's ``intent_id``.
- ``GET /canary-intents``, ``POST /canary-intents/{canary_id}/claim|report|reply`` (F3, wave
  D2): the ``outlook_local`` activation canary (``canaries_repo``): the published canaries of the
  worker's mailbox, a fresh claim (never stored or replayed; refused ``kill_switch`` while this
  process's settings forbid sending), the Sent Items evidence and the owner's correlated test
  reply (headers and the sender's hash only). ``Idempotency-Key`` is required on all three
  POSTs; report and reply replay their acknowledgement like the send report.
- ``POST /heartbeat`` and ``POST /account-report``: no ``Idempotency-Key``; the account report has
  no ``schema_version`` (wire shape). A refused account report is recorded, then ``409``.

Every body names the worker's own mailbox (``403`` otherwise). No route logs or returns a token,
address, subject or body.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from starlette.responses import Response

from suv_deals.api.auth import AuthFailure, bearer_token
from suv_deals.api.deps import api_state, body_model, in_transaction, no_query, path_id, query_model
from suv_deals.api.errors import JSON_MEDIA_TYPE, ApiHttpError, request_id_of
from suv_deals.api.middleware import client_address
from suv_deals.api.schemas import (
    IDEMPOTENCY_HEADER,
    MAIL_WORKER_PREFIX,
    MailWorkerAccepted,
    MailWorkerAccountReport,
    MailWorkerBindingsQuery,
    MailWorkerCanaryClaimRequest,
    MailWorkerCanaryIntentBatch,
    MailWorkerCanaryReply,
    MailWorkerCanaryReport,
    MailWorkerClaimDecision,
    MailWorkerClaimRequest,
    MailWorkerHeartbeatRequest,
    MailWorkerReplyRequest,
    MailWorkerSendIntent,
    MailWorkerSendIntentBatch,
    MailWorkerSendIntentsQuery,
    MailWorkerSendReport,
)
from suv_deals.domain.reviews import validate_idempotency_key
from suv_deals.errors import AppError, ErrorCode, Unauthenticated, ValidationFailed
from suv_deals.integrations.email_providers.outlook_local import OutlookSendIntent
from suv_deals.persistence import (
    canaries_repo,
    credentials_repo,
    idempotency,
    mail_workers_repo,
    replies_repo,
    send_intents_repo,
)
from suv_deals.persistence.database import Conn
from suv_deals.persistence.errors_map import TransientConflict, mapped_errors
from suv_deals.persistence.mail_workers_repo import WorkerIdentity
from suv_deals.persistence.replies_repo import ReplyIngestOptions
from suv_deals.settings import Settings

router = APIRouter()

REPORT_OPERATION: Final = "mail_worker.send_report"
CANARY_REPORT_OPERATION: Final = "mail_worker.canary_report"
CANARY_REPLY_OPERATION: Final = "mail_worker.canary_reply"
_WORKER_KIND: Final = "mail_worker"


# --------------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkerContext:
    """The authenticated mailbox worker of one request."""

    worker: WorkerIdentity
    request_id: str


def _rate_key(token: str) -> UUID:
    """A stable, non-reversible limiter key for a presented credential (no database needed)."""
    return UUID(bytes=hashlib.sha256(token.encode("utf-8")).digest()[:16])


async def authenticate_worker(request: Request) -> WorkerContext:
    """Dependency of every mail-worker route (see module docstring)."""
    state = api_state(request)
    request_id = request_id_of(request)
    client = client_address(request.scope)
    state.preauth.check(client)
    try:
        token = bearer_token(request.headers.getlist("authorization"))
        if credentials_repo.token_kind(token) != _WORKER_KIND:
            raise AuthFailure("invalid_token")  # CPU-only: never a database lookup
    except AuthFailure as exc:
        state.preauth.failed(client)
        state.metrics.record_auth_denial("api", exc.reason)
        raise
    state.mail_worker_limiter.check(_rate_key(token), mutation=request.method not in ("GET", "HEAD"))
    try:
        async with mapped_errors(), state.db.transaction() as conn, mapped_errors():
            worker = await mail_workers_repo.resolve_worker(conn, token)
    except Unauthenticated as exc:
        state.preauth.failed(client)
        revoked = (exc.details or {}).get("reason") == "mail_worker_credential_revoked"
        state.metrics.record_auth_denial("api", "revoked" if revoked else "invalid_token")
        raise
    return WorkerContext(worker=worker, request_id=request_id)


Worker = Annotated[WorkerContext, Depends(authenticate_worker)]


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _wire(model: BaseModel, status: int = 200) -> Response:
    """A wire model as the top-level JSON body (no envelope)."""
    return Response(content=model.model_dump_json(), status_code=status, media_type=JSON_MEDIA_TYPE)


def required_idempotency_key(request: Request) -> str:
    """Exactly one well-formed ``Idempotency-Key`` header (``400`` when missing, ``422`` when
    malformed or repeated)."""
    values = request.headers.getlist(IDEMPOTENCY_HEADER)
    if not values:
        raise ApiHttpError(
            ErrorCode.VALIDATION_ERROR,
            "This route requires an Idempotency-Key header",
            http_status=400,
            details={"fields": [IDEMPOTENCY_HEADER]},
        )
    if len(values) > 1:
        raise ValidationFailed("Send Idempotency-Key exactly once", details={"fields": [IDEMPOTENCY_HEADER]})
    try:
        return validate_idempotency_key(values[0].strip())
    except ValueError:
        raise ValidationFailed(
            "Idempotency-Key must be 8-128 characters of A-Z a-z 0-9 . _ : -",
            details={"fields": [IDEMPOTENCY_HEADER]},
        ) from None


def _same_intent(path_value: UUID, body_value: UUID) -> None:
    if path_value != body_value:
        raise ValidationFailed(
            "The body's intent_id must equal the path's intent_id", details={"fields": ["intent_id"]}
        )


async def _work[T](request: Request, ctx: WorkerContext, work: Callable[[Conn], Awaitable[T]]) -> T:
    return await in_transaction(api_state(request), ctx.worker.actor(ctx.request_id), work)


#: ``process_gate`` detail codes (audited on the refused claim; never a value).
GATE_KILL_SWITCH: Final = "SETTINGS_KILL_SWITCH_ACTIVE"
GATE_MODE: Final = "SETTINGS_MODE_NOT_AUTOMATIC"


def process_gate(settings: Settings) -> str | None:
    """Why this process's settings forbid sending (``None`` when they allow it).

    ``SELLER_INQUIRY_KILL_SWITCH`` and ``SELLER_INQUIRY_MODE`` are process-level switches next to
    the workspace controls in the database; both must allow sending, so a switch flipped on the
    API process stops an intent that was committed before (at the worker's claim).
    """
    if settings.seller_inquiry_kill_switch:
        return GATE_KILL_SWITCH
    if settings.seller_inquiry_mode != "automatic":
        return GATE_MODE
    return None


def _wire_intent(intent: OutlookSendIntent, *, expired: bool) -> MailWorkerSendIntent:
    return MailWorkerSendIntent.model_validate({**intent.model_dump(), "expired": expired})


# --------------------------------------------------------------------------------------------
# Bindings and replies
# --------------------------------------------------------------------------------------------


@router.get(MAIL_WORKER_PREFIX + "/inquiry-bindings")
async def get_inquiry_bindings(request: Request, ctx: Worker) -> Response:
    query = query_model(request, MailWorkerBindingsQuery)
    secret = api_state(request).cursor_secret()
    page = await _work(
        request,
        ctx,
        lambda conn: mail_workers_repo.list_binding_changes(
            conn, ctx.worker, cursor=query.cursor, limit=query.limit, secret=secret
        ),
    )
    return _wire(page)


@router.post(MAIL_WORKER_PREFIX + "/replies")
async def post_reply(request: Request, ctx: Worker) -> Response:
    no_query(request)
    state = api_state(request)
    # The reply bucket is tighter than the generic mutation bucket (``deps.MAIL_WORKER_REPLY_LIMIT``);
    # checked before the body is parsed, so a flood never reaches correlation or the database.
    state.mail_worker_reply_limiter.check(ctx.worker.credential_id, mutation=True)
    key = required_idempotency_key(request)
    body = await body_model(request, MailWorkerReplyRequest)
    ack = await replies_repo.ingest(
        state.db,
        ctx.worker,
        body,
        key,
        request_id=ctx.request_id,
        now=state.clock.now(),
        options=ReplyIngestOptions(
            dashboard_base_url=state.settings.app_base_url,
            # The worker's ``seller_reply_process`` job (owner decisions, opportunity check) is
            # queued in the same transaction; the reconciliation pass is only the backstop.
            enqueue_processing_job=True,
        ),
    )
    return _wire(ack)


# --------------------------------------------------------------------------------------------
# Send intents
# --------------------------------------------------------------------------------------------


@router.get(MAIL_WORKER_PREFIX + "/send-intents")
async def get_send_intents(request: Request, ctx: Worker) -> Response:
    query = query_model(request, MailWorkerSendIntentsQuery)
    batch = await _work(
        request,
        ctx,
        lambda conn: send_intents_repo.list_pending(
            conn, ctx.worker, request_id=ctx.request_id, limit=query.limit
        ),
    )
    intents = (
        *(_wire_intent(i, expired=False) for i in batch.intents),
        *(_wire_intent(i, expired=True) for i in batch.expired),
    )
    closed = process_gate(api_state(request).settings) is not None
    return _wire(
        MailWorkerSendIntentBatch(
            intents=intents[: query.limit], kill_switch_active=batch.kill_switch_active or closed
        )
    )


@router.post(MAIL_WORKER_PREFIX + "/send-intents/{intent_id}/claim")
async def post_claim(request: Request, intent_id: str, ctx: Worker) -> Response:
    no_query(request)
    target = path_id(intent_id, "intent_id")
    required_idempotency_key(request)  # never stored or replayed: a claim is always fresh
    body = await body_model(request, MailWorkerClaimRequest)
    _same_intent(target, body.intent_id)
    ctx.worker.require_mailbox(body.mailbox_binding_id)
    gate = process_gate(api_state(request).settings)
    result = await _work(
        request,
        ctx,
        lambda conn: send_intents_repo.claim(
            conn,
            ctx.worker,
            intent_id=target,
            claim_attempt_id=body.claim_attempt_id,
            worker_id=body.worker_id,
            request_id=ctx.request_id,
            process_gate=gate,
        ),
    )
    return _wire(
        MailWorkerClaimDecision(
            intent_id=result.intent_id, proceed=result.proceed, refusal_reason=result.refusal_reason
        )
    )


def _replay_error(code: str) -> AppError:
    try:
        error_code = ErrorCode(code)
    except ValueError:
        error_code = ErrorCode.INTERNAL_ERROR
    return AppError(error_code, "The original request with this Idempotency-Key failed")


@router.post(MAIL_WORKER_PREFIX + "/send-intents/{intent_id}/report")
async def post_report(request: Request, intent_id: str, ctx: Worker) -> Response:
    no_query(request)
    target = path_id(intent_id, "intent_id")
    key = required_idempotency_key(request)
    body = await body_model(request, MailWorkerSendReport)
    _same_intent(target, body.intent_id)
    ctx.worker.require_mailbox(body.mailbox_binding_id)
    request_hash = idempotency.request_hash_for(REPORT_OPERATION, body)
    actor = ctx.worker.actor(ctx.request_id)

    async def work(conn: Conn) -> None:
        started = await idempotency.begin(conn, actor, REPORT_OPERATION, key, request_hash)
        if isinstance(started, idempotency.Replay):
            return
        if isinstance(started, idempotency.ReplayError):
            raise _replay_error(started.error_code)
        if isinstance(started, idempotency.InProgress):
            raise TransientConflict.in_progress("The same report is still being recorded; retry shortly")
        await send_intents_repo.report(conn, ctx.worker, report=body, request_id=ctx.request_id)
        await idempotency.complete(conn, actor, REPORT_OPERATION, key, {"accepted": True})

    await _work(request, ctx, work)
    return _wire(MailWorkerAccepted())


# --------------------------------------------------------------------------------------------
# Activation canaries (outlook_local transport; F3, wave D2; `canaries_repo`)
# --------------------------------------------------------------------------------------------


def _same_canary(path_value: UUID, body_value: UUID) -> None:
    if path_value != body_value:
        raise ValidationFailed(
            "The body's canary_id must equal the path's canary_id", details={"fields": ["canary_id"]}
        )


@router.get(MAIL_WORKER_PREFIX + "/canary-intents")
async def get_canary_intents(request: Request, ctx: Worker) -> Response:
    no_query(request)
    closed = process_gate(api_state(request).settings) is not None
    batch = await _work(
        request,
        ctx,
        lambda conn: canaries_repo.desktop_canary_intents(
            conn, ctx.worker, request_id=ctx.request_id, process_closed=closed
        ),
    )
    return _wire(MailWorkerCanaryIntentBatch.model_validate(batch.model_dump()))


@router.post(MAIL_WORKER_PREFIX + "/canary-intents/{canary_id}/claim")
async def post_canary_claim(request: Request, canary_id: str, ctx: Worker) -> Response:
    no_query(request)
    target = path_id(canary_id, "canary_id")
    required_idempotency_key(request)  # never stored or replayed: a claim is always fresh
    body = await body_model(request, MailWorkerCanaryClaimRequest)
    _same_canary(target, body.canary_id)
    ctx.worker.require_mailbox(body.mailbox_binding_id)
    gate = process_gate(api_state(request).settings)
    decision = await _work(
        request,
        ctx,
        lambda conn: canaries_repo.claim_desktop_canary(
            conn,
            ctx.worker,
            canary_id=target,
            claim_attempt_id=body.claim_attempt_id,
            worker_id=body.worker_id,
            request_id=ctx.request_id,
            process_gate=gate,
        ),
    )
    return _wire(decision)


async def _idempotent_canary_step(
    request: Request,
    ctx: WorkerContext,
    *,
    operation: str,
    key: str,
    body: BaseModel,
    step: Callable[[Conn], Awaitable[object]],
) -> None:
    request_hash = idempotency.request_hash_for(operation, body)
    actor = ctx.worker.actor(ctx.request_id)

    async def work(conn: Conn) -> None:
        started = await idempotency.begin(conn, actor, operation, key, request_hash)
        if isinstance(started, idempotency.Replay):
            return
        if isinstance(started, idempotency.ReplayError):
            raise _replay_error(started.error_code)
        if isinstance(started, idempotency.InProgress):
            raise TransientConflict.in_progress("The same request is still being recorded; retry shortly")
        await step(conn)
        await idempotency.complete(conn, actor, operation, key, {"accepted": True})

    await _work(request, ctx, work)


@router.post(MAIL_WORKER_PREFIX + "/canary-intents/{canary_id}/report")
async def post_canary_report(request: Request, canary_id: str, ctx: Worker) -> Response:
    no_query(request)
    target = path_id(canary_id, "canary_id")
    key = required_idempotency_key(request)
    body = await body_model(request, MailWorkerCanaryReport)
    _same_canary(target, body.canary_id)
    ctx.worker.require_mailbox(body.mailbox_binding_id)
    await _idempotent_canary_step(
        request,
        ctx,
        operation=CANARY_REPORT_OPERATION,
        key=key,
        body=body,
        step=lambda conn: canaries_repo.report_desktop_canary(
            conn, ctx.worker, report=body, request_id=ctx.request_id
        ),
    )
    return _wire(MailWorkerAccepted())


@router.post(MAIL_WORKER_PREFIX + "/canary-intents/{canary_id}/reply")
async def post_canary_reply(request: Request, canary_id: str, ctx: Worker) -> Response:
    no_query(request)
    target = path_id(canary_id, "canary_id")
    key = required_idempotency_key(request)
    body = await body_model(request, MailWorkerCanaryReply)
    _same_canary(target, body.canary_id)
    ctx.worker.require_mailbox(body.mailbox_binding_id)
    await _idempotent_canary_step(
        request,
        ctx,
        operation=CANARY_REPLY_OPERATION,
        key=key,
        body=body,
        step=lambda conn: canaries_repo.record_desktop_canary_reply(
            conn, ctx.worker, reply=body, request_id=ctx.request_id
        ),
    )
    return _wire(MailWorkerAccepted())


# --------------------------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------------------------


@router.post(MAIL_WORKER_PREFIX + "/heartbeat")
async def post_heartbeat(request: Request, ctx: Worker) -> Response:
    no_query(request)
    body = await body_model(request, MailWorkerHeartbeatRequest)
    ack = await _work(
        request,
        ctx,
        lambda conn: mail_workers_repo.record_heartbeat(conn, ctx.worker, body, request_id=ctx.request_id),
    )
    return _wire(ack)


@router.post(MAIL_WORKER_PREFIX + "/account-report")
async def post_account_report(request: Request, ctx: Worker) -> Response:
    no_query(request)
    body = await body_model(request, MailWorkerAccountReport)
    outcome = await _work(
        request,
        ctx,
        lambda conn: mail_workers_repo.record_account_report(
            conn, ctx.worker, body, request_id=ctx.request_id
        ),
    )
    outcome.raise_for_problems()  # AFTER the commit: the refusal stays visible on the dashboard
    return _wire(MailWorkerAccepted())


__all__ = [
    "CANARY_REPLY_OPERATION",
    "CANARY_REPORT_OPERATION",
    "GATE_KILL_SWITCH",
    "GATE_MODE",
    "REPORT_OPERATION",
    "Worker",
    "WorkerContext",
    "authenticate_worker",
    "process_gate",
    "required_idempotency_key",
    "router",
]
