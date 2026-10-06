"""Translate PostgreSQL errors into typed, safely reportable `AppError`s (spec 21, docs/schema.md).

Every persistence function runs its SQL inside `mapped_errors()`, so callers only ever see
`AppError` subclasses. Messages are fixed, safe strings: they never contain SQL, parameter
values, row contents or server details. The stable constraint name (asserted by the schema
tests) is the only database identifier exposed, in `details`, for check/not-null violations.

| SQLSTATE | Meaning | AppError |
|---|---|---|
| `SV001` | append-only history UPDATE/DELETE | `ValidationFailed` (guard `append_only`) |
| `SV002` | state transition not permitted | `VersionConflict` |
| `SV003` | dangling / cross-workspace / fixture reference | `ValidationFailed` (one text: no leak) |
| `SV004` | frozen column modified | `ValidationFailed` (guard `frozen`) |
| `SV005` | monotonic version would decrease | `VersionConflict` |
| `SV006` | detail generation never allocated | `ValidationFailed` (guard `generation`) |
| `23505` unique_violation | concurrent duplicate | `VersionConflict` (override per constraint) |
| `23503` foreign_key_violation | referenced row missing or in another workspace | `NotFound` |
| `23514` check / `23502` not null / `22xxx` data | invalid value | `ValidationFailed` |
| `42501` insufficient_privilege (RLS/grants) | row outside the workspace or missing grant | `Forbidden` |
| `40001` / `40P01` serialization / deadlock | retry the whole transaction | `TransientConflict` (retryable) |
| `55P03` lock_not_available (lock_timeout) | row busy | `TransientConflict` (retryable) |
| `57014` query_canceled (statement_timeout) | too slow | `DependencyUnavailable` (retryable) |
| `08xxx` / OperationalError | connection lost | `DependencyUnavailable` (retryable) |
| anything else | unexpected | `AppError(INTERNAL_ERROR)` |
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Final

import psycopg

from suv_deals.errors import (
    AppError,
    DependencyUnavailable,
    ErrorCode,
    Forbidden,
    NotFound,
    ValidationFailed,
    VersionConflict,
)

SV_APPEND_ONLY: Final = "SV001"
SV_TRANSITION: Final = "SV002"
SV_REFERENCE: Final = "SV003"
SV_FROZEN: Final = "SV004"
SV_MONOTONIC: Final = "SV005"
SV_GENERATION: Final = "SV006"

UNIQUE_VIOLATION: Final = "23505"
FOREIGN_KEY_VIOLATION: Final = "23503"
CHECK_VIOLATION: Final = "23514"
NOT_NULL_VIOLATION: Final = "23502"
EXCLUSION_VIOLATION: Final = "23P01"
INSUFFICIENT_PRIVILEGE: Final = "42501"
SERIALIZATION_FAILURE: Final = "40001"
DEADLOCK_DETECTED: Final = "40P01"
LOCK_NOT_AVAILABLE: Final = "55P03"
QUERY_CANCELED: Final = "57014"

_TRANSIENT: Final = frozenset({SERIALIZATION_FAILURE, DEADLOCK_DETECTED, LOCK_NOT_AVAILABLE})

ErrorFactory = Callable[[], AppError]


class LeaseLost(AppError):
    """The worker no longer holds the job/event lease: roll back the WHOLE transaction.

    Raised by fenced updates (heartbeat, completion, failure, outbox lifecycle) that matched
    zero rows, and by `transactions.lock_job` when revalidation fails. A newer lease holder may
    already own the work; a late worker must never commit domain writes (spec 13 fencing).
    """

    def __init__(self, message: str = "The work lease was lost; nothing was committed") -> None:
        super().__init__(ErrorCode.VERSION_CONFLICT, message, retryable=False)


class TransientConflict(AppError):
    """Serialization failure, deadlock or lock timeout: retry the whole transaction."""

    def __init__(self, message: str = "Concurrent update; retry the request") -> None:
        super().__init__(ErrorCode.VERSION_CONFLICT, message, retryable=True, retry_after_seconds=1)


def sqlstate_of(exc: BaseException) -> str | None:
    return exc.sqlstate if isinstance(exc, psycopg.Error) else None


def constraint_of(exc: BaseException) -> str | None:
    if not isinstance(exc, psycopg.Error):
        return None
    name = exc.diag.constraint_name
    return name if isinstance(name, str) and name else None


def is_retryable_db_error(exc: BaseException) -> bool:
    if isinstance(exc, AppError):
        return exc.retryable
    if isinstance(exc, psycopg.OperationalError):
        return True
    state = sqlstate_of(exc)
    return state is not None and (state in _TRANSIENT or state == QUERY_CANCELED)


def map_db_error(
    exc: BaseException,
    *,
    unique: Mapping[str, ErrorFactory] | None = None,
    foreign_key: Mapping[str, ErrorFactory] | None = None,
) -> AppError:
    """Typed AppError for a database exception. `AppError`s pass through unchanged.

    `unique` / `foreign_key` map constraint names to caller-specific errors (for example an
    idempotency key -> `IdempotencyConflict`); unlisted constraints use the defaults above.
    """
    if isinstance(exc, AppError):
        return exc
    state = sqlstate_of(exc)
    constraint = constraint_of(exc)
    if state is None:
        if isinstance(exc, psycopg.OperationalError | psycopg.InterfaceError):
            return DependencyUnavailable("The database is unavailable")
        return AppError(ErrorCode.INTERNAL_ERROR, "Database operation failed")
    if state == SV_APPEND_ONLY:
        return ValidationFailed(
            "History records are append-only; write a superseding record", details={"guard": "append_only"}
        )
    if state == SV_TRANSITION:
        return VersionConflict("The state transition is not permitted from the current state")
    if state == SV_REFERENCE:
        return ValidationFailed(
            "A referenced record is missing or not eligible for this record", details={"guard": "reference"}
        )
    if state == SV_FROZEN:
        return ValidationFailed("These fields are immutable once written", details={"guard": "frozen"})
    if state == SV_MONOTONIC:
        return VersionConflict("A newer version was already recorded; reload and retry")
    if state == SV_GENERATION:
        return ValidationFailed(
            "The observation generation was never allocated", details={"guard": "generation"}
        )
    if state == UNIQUE_VIOLATION:
        if unique and constraint is not None and constraint in unique:
            return unique[constraint]()
        return VersionConflict("A conflicting record was written concurrently; reload and retry")
    if state == FOREIGN_KEY_VIOLATION:
        if foreign_key and constraint is not None and constraint in foreign_key:
            return foreign_key[constraint]()
        # Missing and foreign-workspace references are indistinguishable by design.
        return NotFound("A referenced record was not found")
    if state in (CHECK_VIOLATION, NOT_NULL_VIOLATION, EXCLUSION_VIOLATION):
        details = {"constraint": constraint} if constraint else None
        return ValidationFailed("The value violates a data constraint", details=details)
    if state.startswith("22"):
        return ValidationFailed("A value has an invalid format or size")
    if state == INSUFFICIENT_PRIVILEGE:
        return Forbidden("The operation is not permitted for this workspace")
    if state in _TRANSIENT:
        if state == LOCK_NOT_AVAILABLE:
            return TransientConflict("The record is busy; retry shortly")
        return TransientConflict()
    if state == QUERY_CANCELED:
        return AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "The database did not answer in time",
            retryable=True,
            retry_after_seconds=2,
        )
    if state.startswith("08") or isinstance(exc, psycopg.OperationalError):
        return DependencyUnavailable("The database is unavailable")
    return AppError(ErrorCode.INTERNAL_ERROR, "Database operation failed")


@asynccontextmanager
async def mapped_errors(
    *,
    unique: Mapping[str, ErrorFactory] | None = None,
    foreign_key: Mapping[str, ErrorFactory] | None = None,
) -> AsyncIterator[None]:
    """Convert psycopg errors raised in the block (including a deferred-constraint failure at
    COMMIT when it wraps `Database.transaction`) into typed `AppError`s."""
    try:
        yield
    except psycopg.Error as exc:
        raise map_db_error(exc, unique=unique, foreign_key=foreign_key) from exc
