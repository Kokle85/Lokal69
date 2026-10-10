"""MCP tool surface: ``tools/list`` and ``tools/call`` for the twelve spec 21 tools and the three
spec 37.8 inquiry tools (``seller_inquiries_get``, ``seller_replies_get`` under ``inquiries:read``;
``seller_inquiries_pause`` under ``inquiries:pause``).

``tools/list`` returns only the tools whose scope the caller holds, in the deterministic spec 21
order, each with its exact resolved ``inputSchema`` (``additionalProperties: false`` everywhere),
``outputSchema`` (``ResponseEnvelope[<view>]``) and annotations from ``mcp.schemas.TOOLS``.
Results are ``cacheScope: private`` with ``ttlMs: 0`` because the list depends on authorization.

``tools/call`` (`ToolDispatcher.call_tool`):

1. An unknown tool is a JSON-RPC protocol error ``-32602`` ("Unknown tool"), never a tool result.
2. The principal comes from the verified access token (``auth.current_principal``); the
   workspace was resolved server-side and is never taken from arguments.
3. The tool's scope is checked (``FORBIDDEN`` tool error, denial metric) BEFORE the arguments
   are looked at; then the arguments are validated against the closed pydantic input model
   (``VALIDATION_ERROR`` naming fields only, never values).
4. A per-principal token bucket limits calls (expensive tools -- paginated reads, health and
   every write -- have a smaller bucket than single-object reads): ``RATE_LIMITED`` with
   ``retry_after_seconds``.
5. The handler runs the shared read-query service (``persistence.queries``) or the repository
   mutation inside ONE short workspace-scoped transaction (``transactions.unit_of_work``;
   failures that prove a rollback are re-run). Mutations are idempotent per principal and
   operation through their ``idempotency_key`` (same key + same request replays the original
   result; a different request is ``IDEMPOTENCY_CONFLICT``). ``deals_request_recheck`` queues a
   bounded job for a registered listing only (``listings_repo.request_recheck``, shared with the
   dashboard API); no tool accepts a URL. ``reviews_submit`` refuses the spec 19 dashboard-action
   reason codes with any outcome but ``needs_information`` (``reviews_repo.submit``).
6. Success: ``CallToolResult(structuredContent=<envelope JSON>, content=[TextContent(
   envelope.to_text())])``. Any ``AppError``: ``isError: true`` with the typed ``ToolError``
   payload (same codes as the dashboard API) as structured content and text, with the request's
   correlation id. Anything unexpected is logged server-side and returned as ``INTERNAL_ERROR``
   without details: no SQL, traces, tokens or submitted values.

There is no purchase, seller-contact/send, payment, tax-approval, SQL or crawl tool: the inquiry
tools read one inquiry/reply of the caller's workspace or activate the kill switch (never resume,
never send). Later packages add tools through `ToolRegistry.with_tools`; the forbidden names are
refused there too.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import uuid4

from mcp import types
from mcp.server import ServerRequestContext
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from suv_deals.api.middleware import PrincipalRateLimiter, RateLimit
from suv_deals.clock import Clock, ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Scope
from suv_deals.errors import HTTP_STATUS, AppError, ErrorCode, Forbidden, Unauthenticated, ValidationFailed
from suv_deals.mcp.auth import current_principal
from suv_deals.mcp.schemas import (
    FORBIDDEN_TOOL_NAMES,
    TOOLS,
    V11_TOOLS,
    DealsAddNoteInput,
    DealsGetCandidateInput,
    DealsGetComparablesInput,
    DealsGetValuationInput,
    DealsHealthInput,
    DealsListCandidatesInput,
    DealsRequestRecheckInput,
    ReviewsClaimInput,
    ReviewsListPendingInput,
    ReviewsReleaseInput,
    ReviewsSubmitInput,
    SellerInquiriesGetInput,
    SellerInquiriesPauseInput,
    SellerRepliesGetInput,
    SourcesPauseInput,
    SubmitRuleError,
    ToolInput,
    ToolSpec,
    tool_error,
    tool_input_schema,
    tool_output_schema,
)
from suv_deals.observability.logging import log_context
from suv_deals.observability.metrics import AppMetrics
from suv_deals.persistence import (
    idempotency,
    inquiries_repo,
    listings_repo,
    notes_repo,
    queries,
    reviews_repo,
    sources_repo,
)
from suv_deals.persistence.database import Conn, Database, db_now
from suv_deals.persistence.errors_map import TransientConflict
from suv_deals.persistence.transactions import retry_transient, unit_of_work
from suv_deals.settings import Settings
from suv_deals.views.common import (
    ResponseEnvelope,
    ResponseWarning,
    WarningCode,
    envelope,
    is_valid_request_id,
    warning,
)
from suv_deals.views.inquiries import InquiryPauseResult
from suv_deals.views.jsonschema import model_schema

logger = logging.getLogger("suv_deals.mcp.tools")

TRANSACTION_ATTEMPTS: Final = 3
TOOL_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
#: Single-object reads; every other tool (paginated reads, health, writes) is "expensive".
CHEAP_TOOLS: Final = frozenset(
    {"deals_get_candidate", "deals_get_valuation", "seller_inquiries_get", "seller_replies_get"}
)
DEFAULT_EXPENSIVE_LIMIT: Final = RateLimit(capacity=30, per_seconds=2.0)  # 30 burst, 30/minute
DEFAULT_CHEAP_LIMIT: Final = RateLimit(capacity=120, per_seconds=0.25)  # 120 burst, 240/minute
_ARGUMENT_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
#: Idempotency operation of ``seller_inquiries_pause`` (shared with ``POST /api/inquiry-control/pause``).
PAUSE_OPERATION: Final = "seller_inquiries_pause"


# --------------------------------------------------------------------------------------------
# Services and calls
# --------------------------------------------------------------------------------------------


class ToolRateLimiter:
    """Per-principal token buckets: a smaller one for expensive tools, a larger one for cheap reads."""

    def __init__(
        self,
        *,
        expensive: RateLimit = DEFAULT_EXPENSIVE_LIMIT,
        cheap: RateLimit = DEFAULT_CHEAP_LIMIT,
        cheap_tools: Iterable[str] = CHEAP_TOOLS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._buckets = PrincipalRateLimiter(mutations=expensive, reads=cheap, monotonic=monotonic)
        self._cheap = frozenset(cheap_tools)

    def check(self, actor: ActorContext, tool: str) -> None:
        """Take one token or raise ``RATE_LIMITED`` (with ``retry_after_seconds``)."""
        self._buckets.check(actor.principal_id, mutation=tool not in self._cheap)


@dataclass(slots=True)
class ToolServices:
    """What tool handlers need; one instance per MCP server."""

    settings: Settings
    db: Database
    clock: Clock
    metrics: AppMetrics
    limiter: ToolRateLimiter

    def cursor_secret(self) -> bytes:
        """The pagination signing key (``INTERNAL_ERROR`` when it is not configured)."""
        return queries.cursor_secret(self.settings.mcp_cursor_signing_secret)


@dataclass(frozen=True, slots=True)
class ToolCall[I: ToolInput]:
    """One authorized, validated tool invocation."""

    services: ToolServices
    actor: ActorContext
    arguments: I

    @property
    def request_id(self) -> str:
        return self.actor.request_id

    async def in_transaction[T](self, work: Callable[[Conn], Awaitable[T]]) -> T:
        """One short workspace-scoped unit of work; re-run only after a proven rollback."""

        async def attempt() -> T:
            async with unit_of_work(self.services.db, self.actor) as conn:
                return await work(conn)

        return await retry_transient(attempt, attempts=TRANSACTION_ATTEMPTS)


ToolHandler = Callable[[ToolCall[Any]], Awaitable[ResponseEnvelope[Any]]]


async def _mutation[T](call: ToolCall[Any], work: Callable[[Conn], Awaitable[T]]) -> tuple[T, datetime]:
    async def run(conn: Conn) -> tuple[T, datetime]:
        result = await work(conn)
        return result, ensure_utc(await db_now(conn))

    return await call.in_transaction(run)


# --------------------------------------------------------------------------------------------
# Handlers (one per spec 21 tool)
# --------------------------------------------------------------------------------------------


def _replay_error(code: str) -> AppError:
    try:
        error_code = ErrorCode(code)
    except ValueError:
        error_code = ErrorCode.INTERNAL_ERROR
    return AppError(error_code, "The original request with this idempotency key failed")


async def pause_inquiries(
    conn: Conn, actor: ActorContext, arguments: SellerInquiriesPauseInput
) -> InquiryPauseResult:
    """``seller_inquiries_pause`` (also the dashboard's pause route): idempotent per principal.

    The same key and request replay the original result; another request under the key is
    ``IDEMPOTENCY_CONFLICT``. ``inquiries_repo.pause`` checks ``inquiries:pause`` and the expected
    control version; it never resumes anything.
    """
    request_hash = idempotency.request_hash_for(PAUSE_OPERATION, arguments)
    started = await idempotency.begin(conn, actor, PAUSE_OPERATION, arguments.idempotency_key, request_hash)
    if isinstance(started, idempotency.Replay):
        return InquiryPauseResult.model_validate(started.result)
    if isinstance(started, idempotency.ReplayError):
        raise _replay_error(started.error_code)
    if isinstance(started, idempotency.InProgress):
        raise TransientConflict.in_progress()
    result = await inquiries_repo.pause(
        conn, actor, expected_version=arguments.expected_version, reason=arguments.reason
    )
    await idempotency.complete(
        conn, actor, PAUSE_OPERATION, arguments.idempotency_key, result.model_dump(mode="json")
    )
    return result


async def deals_health(call: ToolCall[DealsHealthInput]) -> ResponseEnvelope[Any]:
    services = call.services
    result = await queries.health_view(services.db, call.actor, services.settings, clock=services.clock)
    return result.envelope(call.request_id)


async def deals_list_candidates(call: ToolCall[DealsListCandidatesInput]) -> ResponseEnvelope[Any]:
    secret = call.services.cursor_secret()
    result = await call.in_transaction(
        lambda conn: queries.list_candidates(conn, call.actor, call.arguments, secret=secret)
    )
    return result.envelope(call.request_id)


async def deals_get_candidate(call: ToolCall[DealsGetCandidateInput]) -> ResponseEnvelope[Any]:
    args = call.arguments
    result = await call.in_transaction(
        lambda conn: queries.get_candidate(conn, call.actor, args.listing_id, args.revision)
    )
    return result.envelope(call.request_id)


async def deals_get_comparables(call: ToolCall[DealsGetComparablesInput]) -> ResponseEnvelope[Any]:
    args = call.arguments
    secret = call.services.cursor_secret()
    result = await call.in_transaction(
        lambda conn: queries.get_comparables(
            conn,
            call.actor,
            args.comparable_set_id,
            include_excluded=args.include_excluded,
            cursor=args.cursor,
            limit=args.limit,
            secret=secret,
        )
    )
    return result.envelope(call.request_id)


async def deals_get_valuation(call: ToolCall[DealsGetValuationInput]) -> ResponseEnvelope[Any]:
    valuation_id = call.arguments.valuation_id
    result = await call.in_transaction(lambda conn: queries.get_valuation(conn, call.actor, valuation_id))
    return result.envelope(call.request_id)


async def reviews_list_pending(call: ToolCall[ReviewsListPendingInput]) -> ResponseEnvelope[Any]:
    secret = call.services.cursor_secret()
    result = await call.in_transaction(
        lambda conn: queries.review_queue(conn, call.actor, call.arguments, secret=secret)
    )
    return result.envelope(call.request_id)


async def reviews_claim(call: ToolCall[ReviewsClaimInput]) -> ResponseEnvelope[Any]:
    args = call.arguments
    view, as_of = await _mutation(
        call,
        lambda conn: reviews_repo.claim(
            conn,
            call.actor,
            args.case_id,
            args.expected_version,
            args.idempotency_key,
            duration=timedelta(seconds=call.services.settings.review_claim_duration_seconds),
        ),
    )
    return envelope(view, request_id=call.request_id, as_of=as_of)


async def reviews_release(call: ToolCall[ReviewsReleaseInput]) -> ResponseEnvelope[Any]:
    args = call.arguments
    view, as_of = await _mutation(
        call,
        lambda conn: reviews_repo.release(
            conn, call.actor, args.case_id, args.claim_token, args.idempotency_key
        ),
    )
    return envelope(view, request_id=call.request_id, as_of=as_of)


async def reviews_submit(call: ToolCall[ReviewsSubmitInput]) -> ResponseEnvelope[Any]:
    submission = call.arguments.to_submit_request()
    base_url = call.services.settings.app_base_url
    view, as_of = await _mutation(
        call, lambda conn: reviews_repo.submit(conn, call.actor, submission, dashboard_base_url=base_url)
    )
    warnings: list[ResponseWarning] = [warning(WarningCode.FIXTURE_DATA)] if view.is_fixture else []
    mine = view.for_caller(call.actor.principal_id)
    return envelope(mine, request_id=call.request_id, as_of=as_of, warnings=warnings)


async def deals_request_recheck(call: ToolCall[DealsRequestRecheckInput]) -> ResponseEnvelope[Any]:
    view, as_of = await _mutation(
        call, lambda conn: listings_repo.request_recheck(conn, call.actor, call.arguments)
    )
    return envelope(view, request_id=call.request_id, as_of=as_of)


async def deals_add_note(call: ToolCall[DealsAddNoteInput]) -> ResponseEnvelope[Any]:
    args = call.arguments
    view, as_of = await _mutation(
        call,
        lambda conn: notes_repo.add_note(conn, call.actor, args.listing_id, args.note, args.idempotency_key),
    )
    return envelope(view, request_id=call.request_id, as_of=as_of)


async def sources_pause(call: ToolCall[SourcesPauseInput]) -> ResponseEnvelope[Any]:
    args = call.arguments
    view, as_of = await _mutation(
        call,
        lambda conn: sources_repo.pause_source(
            conn, call.actor, args.source_id, args.expected_version, args.reason, args.idempotency_key
        ),
    )
    return envelope(
        view, request_id=call.request_id, as_of=as_of, warnings=[warning(WarningCode.SOURCE_PAUSED)]
    )


async def seller_inquiries_get(call: ToolCall[SellerInquiriesGetInput]) -> ResponseEnvelope[Any]:
    inquiry_id = call.arguments.inquiry_id
    result = await call.in_transaction(lambda conn: queries.get_inquiry(conn, call.actor, inquiry_id))
    return result.envelope(call.request_id)


async def seller_replies_get(call: ToolCall[SellerRepliesGetInput]) -> ResponseEnvelope[Any]:
    """One correlated reply (spec 37.8). A QUARANTINED reply is an unverified possible match that
    may be unrelated personal mail: only its metadata leaves over MCP (``content_withheld``;
    ``views.inquiries.reply_content_visible`` needs ``config:admin``, never effective on MCP), so
    its text never reaches dot or a model provider before the owner verified it on the dashboard.
    The read query applies that rule itself (``queries.get_reply``: the view and its warnings are
    built for the caller's scopes), so every surface inherits it from one place."""
    reply_id = call.arguments.reply_id
    result = await call.in_transaction(lambda conn: queries.get_reply(conn, call.actor, reply_id))
    return result.envelope(call.request_id)


async def seller_inquiries_pause(call: ToolCall[SellerInquiriesPauseInput]) -> ResponseEnvelope[Any]:
    view, as_of = await _mutation(call, lambda conn: pause_inquiries(conn, call.actor, call.arguments))
    return envelope(view, request_id=call.request_id, as_of=as_of)


#: The spec 37.8 inquiry tools (``mcp.schemas.V11_TOOLS``); served by default after the twelve.
V11_HANDLERS: Final[Mapping[str, ToolHandler]] = {
    "seller_inquiries_get": seller_inquiries_get,
    "seller_replies_get": seller_replies_get,
    "seller_inquiries_pause": seller_inquiries_pause,
}


CORE_HANDLERS: Final[Mapping[str, ToolHandler]] = {
    "deals_health": deals_health,
    "deals_list_candidates": deals_list_candidates,
    "deals_get_candidate": deals_get_candidate,
    "deals_get_comparables": deals_get_comparables,
    "deals_get_valuation": deals_get_valuation,
    "reviews_list_pending": reviews_list_pending,
    "reviews_claim": reviews_claim,
    "reviews_release": reviews_release,
    "reviews_submit": reviews_submit,
    "deals_request_recheck": deals_request_recheck,
    "deals_add_note": deals_add_note,
    "sources_pause": sources_pause,
}


# --------------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------------


def _annotations(spec: ToolSpec) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        title=spec.annotations.title,
        read_only_hint=spec.annotations.read_only,
        destructive_hint=spec.annotations.destructive,
        idempotent_hint=spec.annotations.idempotent,
        open_world_hint=spec.annotations.open_world,
    )


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler
    definition: types.Tool
    input_schema: dict[str, Any]

    @property
    def name(self) -> str:
        return self.spec.name

    def validate(self, arguments: Mapping[str, Any] | None) -> ToolInput:
        """Closed-model validation (the same models and rules as ``schemas.validate_tool_input``);
        ``VALIDATION_ERROR`` names the failing argument fields, never their values."""
        if arguments is not None and not isinstance(arguments, Mapping):
            raise ValidationFailed(f"Invalid arguments for {self.name}", details={"fields": ["arguments"]})
        try:
            return self.spec.input_model.model_validate(dict(arguments or {}))
        except ValidationError as exc:
            raise ValidationFailed(
                f"Invalid arguments for {self.name}", details={"fields": argument_error_fields(exc)}
            ) from None


def argument_error_fields(exc: ValidationError) -> list[str]:
    """Top-level argument names (plus a list index) of a validation failure, sorted and bounded.

    Tool inputs are flat, so the first location segment is the argument; pydantic's union-member
    tags (``changed_since.none``) are dropped. Domain-rule failures name the fields they concern
    (``schemas.SubmitRuleError``); names that are not plain identifiers become
    ``<unrecognised field>``. Submitted values never appear.
    """
    fields: set[str] = set()
    for err in exc.errors():
        loc = err["loc"]
        if not loc:
            cause = (err.get("ctx") or {}).get("error")
            fields.update(cause.fields if isinstance(cause, SubmitRuleError) else ("arguments",))
            continue
        head = loc[0]
        if not isinstance(head, str) or not _ARGUMENT_RE.fullmatch(head):
            fields.add("<unrecognised field>")
            continue
        index = loc[1] if len(loc) > 1 and isinstance(loc[1], int) else None
        fields.add(head if index is None else f"{head}.{index}")
    return sorted(fields)[:20]


def _register(spec: ToolSpec, handler: ToolHandler) -> RegisteredTool:
    if not TOOL_NAME_PATTERN.fullmatch(spec.name):
        raise ValueError("tool names must be 1-128 characters of [A-Za-z0-9_.-]")
    if spec.name in FORBIDDEN_TOOL_NAMES:
        raise ValueError(f"tool {spec.name!r} must never exist (spec 21)")
    core = (spec.name in TOOLS and TOOLS[spec.name] is spec) or (
        spec.name in V11_TOOLS and V11_TOOLS[spec.name] is spec
    )
    if core:
        input_schema = tool_input_schema(spec.name)
        output_schema = tool_output_schema(spec.name)
    else:
        input_schema = model_schema(
            spec.input_model, mode="validation", title=spec.name, keep_object_titles=False
        )
        output_schema = model_schema(spec.envelope_model, mode="serialization", title=f"{spec.name}_result")
    definition = types.Tool(
        name=spec.name,
        title=spec.annotations.title,
        description=spec.description,
        input_schema=input_schema,
        output_schema=output_schema,
        annotations=_annotations(spec),
    )
    return RegisteredTool(spec=spec, handler=handler, definition=definition, input_schema=input_schema)


class ToolRegistry:
    """The tools this server exposes, in a deterministic order (spec 21 order, then extensions)."""

    def __init__(self, tools: Iterable[tuple[ToolSpec, ToolHandler]]) -> None:
        registered: dict[str, RegisteredTool] = {}
        for spec, handler in tools:
            if spec.name in registered:
                raise ValueError(f"tool {spec.name!r} is registered twice")
            registered[spec.name] = _register(spec, handler)
        self._tools = registered

    @classmethod
    def default(cls) -> ToolRegistry:
        """The twelve spec 21 tools, then the three spec 37.8 inquiry tools (no send tool)."""
        return cls(
            [
                *((TOOLS[name], CORE_HANDLERS[name]) for name in TOOLS),
                *((V11_TOOLS[name], V11_HANDLERS[name]) for name in V11_TOOLS),
            ]
        )

    def with_tools(self, extra: Iterable[tuple[ToolSpec, ToolHandler]]) -> ToolRegistry:
        """A registry with additional (extension) tools appended after the existing ones."""
        return ToolRegistry([*((t.spec, t.handler) for t in self._tools.values()), *extra])

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def visible(self, scopes: Iterable[Scope]) -> list[types.Tool]:
        """Tool definitions the caller may discover (unauthorized tools are hidden)."""
        granted = frozenset(scopes)
        return [t.definition.model_copy(deep=True) for t in self._tools.values() if t.spec.scope in granted]

    def input_schema(self, name: str) -> Mapping[str, Any] | None:
        """For the SDK's ``Mcp-Param-*`` header validation (no ``x-mcp-header`` params exist)."""
        tool = self._tools.get(name)
        return None if tool is None else tool.input_schema


# --------------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------------


def _compact(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def success_result(body: ResponseEnvelope[Any]) -> types.CallToolResult:
    """Structured envelope plus the same envelope as compact text."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=body.to_text())],
        structured_content=body.model_dump(mode="json"),
    )


def error_result(error: AppError, correlation_id: str | None) -> types.CallToolResult:
    """``isError: true`` with the typed ``ToolError`` payload (structured and as text)."""
    payload = tool_error(error, correlation_id).model_dump(mode="json")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_compact(payload))],
        structured_content=payload,
        is_error=True,
    )


def request_id_for(ctx: ServerRequestContext[Any, Any]) -> str:
    """The HTTP request's id (set by the request-context/guard middleware), else a fresh one."""
    request = ctx.request
    state = getattr(request, "state", None)
    value = getattr(state, "request_id", None) if state is not None else None
    if is_valid_request_id(value):
        assert isinstance(value, str)
        return value
    return f"mcp-{uuid4().hex}"


def _safe_tool_name(name: object) -> str | None:
    return name if isinstance(name, str) and TOOL_NAME_PATTERN.fullmatch(name) else None


# --------------------------------------------------------------------------------------------
# Dispatcher (the low-level Server handlers)
# --------------------------------------------------------------------------------------------


class ToolDispatcher:
    """``on_list_tools`` / ``on_call_tool`` for the low-level ``mcp.server.Server``."""

    def __init__(self, services: ToolServices, registry: ToolRegistry | None = None) -> None:
        self.services = services
        self.registry = registry or ToolRegistry.default()

    async def list_tools(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        principal = current_principal()
        scopes = principal.scopes if principal is not None else frozenset[Scope]()
        return types.ListToolsResult(tools=self.registry.visible(scopes), ttl_ms=0, cache_scope="private")

    async def call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        tool = self.registry.get(params.name)
        if tool is None:
            safe = _safe_tool_name(params.name)
            message = f"Unknown tool: {safe}" if safe else "Unknown tool"
            raise MCPError(code=types.INVALID_PARAMS, message=message)
        request_id = request_id_for(ctx)
        started = time.perf_counter()
        metrics = self.services.metrics
        with log_context(request_id=request_id):
            try:
                body = await self._run(tool, params.arguments, request_id)
                result = success_result(body)
                status = 200
            except AppError as exc:
                result = error_result(exc, request_id)
                status = HTTP_STATUS.get(exc.code, 500)
                metrics.record_error("mcp", exc.code)
                if exc.code is ErrorCode.INTERNAL_ERROR:
                    logger.warning("mcp tool failed", extra={"tool": tool.name, "code": exc.code.value})
            except Exception:
                logger.exception("mcp tool raised an unexpected error", extra={"tool": tool.name})
                error = AppError(ErrorCode.INTERNAL_ERROR, "Internal error", retryable=True)
                result = error_result(error, request_id)
                status = 500
                metrics.record_error("mcp", ErrorCode.INTERNAL_ERROR)
            elapsed = time.perf_counter() - started
            metrics.observe_request("mcp", tool.name, status, elapsed)
            logger.info(
                "mcp tool call",
                extra={"tool": tool.name, "status": status, "ms": round(elapsed * 1000)},
            )
        return result

    async def _run(
        self, tool: RegisteredTool, arguments: Mapping[str, Any] | None, request_id: str
    ) -> ResponseEnvelope[Any]:
        principal = current_principal()
        if principal is None:
            raise Unauthenticated()
        actor = principal.actor(request_id)
        try:
            actor.require(tool.spec.scope)
        except Forbidden:
            self.services.metrics.record_auth_denial("mcp", "insufficient_scope")
            raise
        validated = tool.validate(arguments)
        self.services.limiter.check(actor, tool.name)
        return await tool.handler(ToolCall(services=self.services, actor=actor, arguments=validated))


def handler_for(name: str) -> ToolHandler:
    """The built-in handler of one spec 21 or spec 37.8 tool (for composing registries)."""
    return CORE_HANDLERS.get(name) or V11_HANDLERS[name]


def tool_names(registry: ToolRegistry | None = None) -> Sequence[str]:
    return (registry or ToolRegistry.default()).names


__all__ = [
    "CHEAP_TOOLS",
    "CORE_HANDLERS",
    "DEFAULT_CHEAP_LIMIT",
    "DEFAULT_EXPENSIVE_LIMIT",
    "PAUSE_OPERATION",
    "V11_HANDLERS",
    "RegisteredTool",
    "ToolCall",
    "ToolDispatcher",
    "ToolHandler",
    "ToolRateLimiter",
    "ToolRegistry",
    "ToolServices",
    "error_result",
    "handler_for",
    "pause_inquiries",
    "request_id_for",
    "success_result",
    "tool_names",
]
