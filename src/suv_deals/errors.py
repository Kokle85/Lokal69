"""Typed application errors shared by the domain, API, MCP and workers.

Every error carries a stable machine code (spec section 21), a safe
user-readable message, a retryable flag and an optional retry-after hint.
Messages must never contain SQL, tokens, secrets, cookies or stack traces.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    CLAIM_EXPIRED = "CLAIM_EXPIRED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    SOURCE_PAUSED = "SOURCE_PAUSED"
    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_RETRYABLE_BY_DEFAULT = frozenset(
    {ErrorCode.RATE_LIMITED, ErrorCode.DEPENDENCY_UNAVAILABLE, ErrorCode.INTERNAL_ERROR}
)

# HTTP status used by the dashboard API for each code. MCP tool errors use the
# same codes inside a tool-error result instead of HTTP statuses.
HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.ALREADY_CLAIMED: 409,
    ErrorCode.CLAIM_EXPIRED: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.SOURCE_PAUSED: 409,
    ErrorCode.ACCESS_BLOCKED: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INSUFFICIENT_DATA: 422,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


class AppError(Exception):
    """Base class for all expected, safely reportable failures."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool | None = None,
        retry_after_seconds: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = code in _RETRYABLE_BY_DEFAULT if retryable is None else retryable
        self.retry_after_seconds = retry_after_seconds
        self.details = details or {}

    def to_payload(self, correlation_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "correlation_id": correlation_id,
        }
        if self.retry_after_seconds is not None:
            payload["retry_after_seconds"] = self.retry_after_seconds
        if self.details:
            payload["details"] = self.details
        return payload

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code.value}: {self.message})"


class ValidationFailed(AppError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.VALIDATION_ERROR, message, details=details)


class NotFound(AppError):
    """Also used for foreign-workspace objects so existence is never leaked."""

    def __init__(self, message: str = "Not found") -> None:
        super().__init__(ErrorCode.NOT_FOUND, message)


class Forbidden(AppError):
    def __init__(self, message: str = "Forbidden") -> None:
        super().__init__(ErrorCode.FORBIDDEN, message)


class Unauthenticated(AppError):
    def __init__(self, message: str = "Authentication required") -> None:
        super().__init__(ErrorCode.UNAUTHENTICATED, message)


class VersionConflict(AppError):
    def __init__(self, message: str = "The object changed; reload and retry", **details: Any) -> None:
        super().__init__(ErrorCode.VERSION_CONFLICT, message, details=details or None)


class AlreadyClaimed(AppError):
    def __init__(self, message: str = "The review case is claimed by another reviewer") -> None:
        super().__init__(ErrorCode.ALREADY_CLAIMED, message)


class ClaimExpired(AppError):
    def __init__(self, message: str = "The review claim expired or is no longer held") -> None:
        super().__init__(ErrorCode.CLAIM_EXPIRED, message)


class IdempotencyConflict(AppError):
    def __init__(self, message: str = "Idempotency key reused with a different request") -> None:
        super().__init__(ErrorCode.IDEMPOTENCY_CONFLICT, message)


class SourcePaused(AppError):
    def __init__(self, message: str = "Source is paused or not enabled") -> None:
        super().__init__(ErrorCode.SOURCE_PAUSED, message)


class AccessBlocked(AppError):
    def __init__(self, message: str = "Source access is blocked; request path stopped") -> None:
        super().__init__(ErrorCode.ACCESS_BLOCKED, message, retryable=False)


class RateLimited(AppError):
    def __init__(self, message: str = "Rate limited", retry_after_seconds: int | None = None) -> None:
        super().__init__(ErrorCode.RATE_LIMITED, message, retry_after_seconds=retry_after_seconds)


class InsufficientData(AppError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.INSUFFICIENT_DATA, message, details=details)


class DependencyUnavailable(AppError):
    def __init__(self, message: str = "A required dependency is unavailable") -> None:
        super().__init__(ErrorCode.DEPENDENCY_UNAVAILABLE, message)
