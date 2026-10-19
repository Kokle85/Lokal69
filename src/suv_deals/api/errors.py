"""Error rendering for the dashboard API (docs/api_contract.md section 3, spec 21 and 24).

Every failed request gets the same ``ApiErrorResponse`` body (``api.schemas.api_error``): a typed
spec 21 code, a safe message, the retryable flag, ``retry_after_seconds`` when known and the
request id as ``correlation_id``. The body never contains SQL, tokens, cookies, credential-bearing
URLs, submitted values or stack traces:

- `AppError`\\s carry messages that are safe by contract; their HTTP status is
  ``errors.HTTP_STATUS[code]`` unless the error is an `ApiHttpError` with an explicit transport
  status (405, 413, 415; the body code stays ``VALIDATION_ERROR``).
- Request-validation failures list the failing field *names* only (``details.fields``).
- Anything else is ``INTERNAL_ERROR`` with a fixed message; the exception is logged server-side
  (through the redacting JSON logger) and never rendered.

``Retry-After`` accompanies every error with a retry hint (clamped to 0-86,400 seconds), and a
``401`` carries a ``WWW-Authenticate: Bearer`` challenge. Every error is ``Cache-Control:
no-store``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from suv_deals.api.schemas import api_error
from suv_deals.clock import Clock, SystemClock
from suv_deals.errors import HTTP_STATUS, AppError, ErrorCode
from suv_deals.observability.metrics import AppMetrics
from suv_deals.views.common import is_valid_request_id

logger = logging.getLogger("suv_deals.api")

JSON_MEDIA_TYPE: Final = "application/json"
NO_STORE: Final = "no-store"
_FIELD_RE: Final = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_UNRECOGNISED: Final = "<unrecognised field>"
_LOCATION_PREFIXES: Final = frozenset({"body", "query", "path", "header", "cookie"})
_MAX_FIELDS: Final = 20


class ApiHttpError(AppError):
    """An `AppError` whose HTTP status is fixed by the transport rather than by its code.

    Used for method-not-allowed (405), payload-too-large (413) and unsupported media type (415):
    the body keeps the spec 21 code ``VALIDATION_ERROR`` so clients that map codes keep working.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        http_status: int,
        details: dict[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(code, message, details=details)
        self.http_status = http_status
        self.response_headers: dict[str, str] = dict(headers or {})


def method_not_allowed(allowed: Iterable[str] = ()) -> ApiHttpError:
    allow = ", ".join(sorted({m.upper() for m in allowed if m}))
    return ApiHttpError(
        ErrorCode.VALIDATION_ERROR,
        "This method is not allowed for the route",
        http_status=405,
        headers={"Allow": allow} if allow else None,
    )


def payload_too_large(limit_bytes: int) -> ApiHttpError:
    return ApiHttpError(
        ErrorCode.VALIDATION_ERROR,
        "The request body is too large",
        http_status=413,
        details={"fields": ["body"], "limit_bytes": limit_bytes},
    )


def unsupported_media_type() -> ApiHttpError:
    return ApiHttpError(
        ErrorCode.VALIDATION_ERROR,
        "Request bodies must be application/json",
        http_status=415,
        details={"fields": ["Content-Type"]},
    )


def internal_error() -> AppError:
    return AppError(ErrorCode.INTERNAL_ERROR, "Internal error; the incident was logged", retryable=True)


def new_request_id() -> str:
    return f"req-{uuid4().hex}"


def request_id_of(request: Request) -> str:
    """The request id the request-context middleware assigned (a fresh one as a fallback)."""
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) and is_valid_request_id(value) else new_request_id()


def http_status_for(error: AppError) -> int:
    status = getattr(error, "http_status", None)
    if isinstance(status, int) and 400 <= status <= 599:
        return status
    return HTTP_STATUS.get(error.code, 500)


def error_response(
    error: AppError,
    *,
    request_id: str | None,
    clock: Clock | None = None,
    metrics: AppMetrics | None = None,
) -> Response:
    """The JSON error response for ``error`` (never raises for a valid `AppError`)."""
    rid = request_id if isinstance(request_id, str) and is_valid_request_id(request_id) else new_request_id()
    _status, body = api_error(error, request_id=rid, as_of=(clock or SystemClock()).now())
    headers: dict[str, str] = {"Cache-Control": NO_STORE}
    if body.error.retry_after_seconds is not None:
        headers["Retry-After"] = str(body.error.retry_after_seconds)
    extra = getattr(error, "response_headers", None)
    if isinstance(extra, Mapping):
        headers.update({str(k): str(v) for k, v in extra.items()})
    if error.code == ErrorCode.UNAUTHENTICATED:
        headers.setdefault("WWW-Authenticate", 'Bearer realm="suv-deals"')
    if metrics is not None:
        metrics.record_error("api", error.code)
    return Response(
        content=body.model_dump_json(),
        status_code=http_status_for(error),
        media_type=JSON_MEDIA_TYPE,
        headers=headers,
    )


def safe_field_names(locations: Iterable[Sequence[object]]) -> list[str]:
    """Bounded, sorted field names of validation-error locations; never the submitted values.

    The leading location kind (``body``/``query``/``path``/``header``) is dropped; anything that is
    not a plain field name (for example an unknown key carrying markup) is reported as
    ``<unrecognised field>``.
    """
    fields: set[str] = set()
    for loc in locations:
        parts = [str(part) for part in loc]
        if parts and parts[0] in _LOCATION_PREFIXES:
            parts = parts[1:]
        name = ".".join(parts) or "request"
        fields.add(name if _FIELD_RE.fullmatch(name) else _UNRECOGNISED)
    return sorted(fields)[:_MAX_FIELDS]


def install_exception_handlers(app: FastAPI, *, clock: Clock, metrics: AppMetrics) -> None:
    """Map `AppError`, request-validation and routing errors to ``ApiErrorResponse`` bodies.

    Unexpected exceptions are handled by the request-context middleware (outermost), which
    renders ``INTERNAL_ERROR`` without details and logs the exception server-side.
    """

    async def on_app_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, AppError)
        return error_response(exc, request_id=request_id_of(request), clock=clock, metrics=metrics)

    async def on_validation_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, RequestValidationError)
        fields = safe_field_names(err.get("loc", ()) for err in exc.errors())
        error = AppError(ErrorCode.VALIDATION_ERROR, "Invalid request", details={"fields": fields})
        return error_response(error, request_id=request_id_of(request), clock=clock, metrics=metrics)

    async def on_http_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, StarletteHTTPException)
        error: AppError
        if exc.status_code == 404:
            error = AppError(ErrorCode.NOT_FOUND, "Not found")
        elif exc.status_code == 405:
            allow = (exc.headers or {}).get("Allow", "")
            error = method_not_allowed(part.strip() for part in allow.split(","))
        elif exc.status_code == 401:
            error = AppError(ErrorCode.UNAUTHENTICATED, "Authentication required")
        elif exc.status_code == 403:
            error = AppError(ErrorCode.FORBIDDEN, "Forbidden")
        elif exc.status_code == 429:
            error = AppError(ErrorCode.RATE_LIMITED, "Rate limited")
        elif 400 <= exc.status_code < 500:
            error = ApiHttpError(ErrorCode.VALIDATION_ERROR, "Invalid request", http_status=exc.status_code)
        else:
            error = internal_error()
        return error_response(error, request_id=request_id_of(request), clock=clock, metrics=metrics)

    app.add_exception_handler(AppError, on_app_error)
    app.add_exception_handler(RequestValidationError, on_validation_error)
    app.add_exception_handler(StarletteHTTPException, on_http_error)


__all__ = [
    "JSON_MEDIA_TYPE",
    "NO_STORE",
    "ApiHttpError",
    "error_response",
    "http_status_for",
    "install_exception_handlers",
    "internal_error",
    "method_not_allowed",
    "new_request_id",
    "payload_too_large",
    "request_id_of",
    "safe_field_names",
    "unsupported_media_type",
]
