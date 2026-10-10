"""Idempotency records for MCP/API mutations (spec 21 "Claim and optimistic concurrency").

Keys are scoped by the authenticated principal and the operation, inside the actor's workspace
(``ops.idempotency_records``; the record is never taken from request-body actor fields).

Usage, all inside ONE transaction with the domain write (so a timeout retry can never submit
twice: a concurrent duplicate blocks on the unique key until the first transaction finishes,
then sees its committed result)::

    request = SubmitRequest.model_validate(body)                    # validated model first
    request_hash = request_hash_for("reviews_submit", request)      # domain.reviews helper
    async with unit_of_work(db, actor) as conn:
        started = await idempotency.begin(conn, actor, "reviews_submit", key, request_hash)
        if isinstance(started, Replay):
            return started.result                                   # original result
        if isinstance(started, (InProgress, ReplayError)):
            ...                                                     # retryable / stored error code
        result = ...domain write (case lock, decision, outbox)...
        await idempotency.complete(conn, actor, "reviews_submit", key, redacted_result)

- Same key + same canonical hash -> `Replay` (or `ReplayError` for a recorded failure).
- Same key + different hash -> `IdempotencyConflict` (also when the key is already used by
  this principal in another workspace: the unique key is principal-wide).
- A record committed while still ``in_progress`` (a multi-transaction operation, or a crash
  between its transactions) -> `InProgress` until it completes, fails or expires.
- ``request_hash`` MUST be computed from the validated request model with
  `domain.reviews.canonical_request_hash` (exposed here as `request_hash_for`), which drops the
  idempotency key and hashes any plaintext claim token first. Stored results must already be
  redacted (e.g. `ClaimGrant.redacted_result()`): never store a plaintext claim token.
- Expired records are treated as absent and replaced; `delete_expired` purges them.
"""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel

from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Scope
from suv_deals.domain.reviews import canonical_request_hash, validate_idempotency_key
from suv_deals.errors import ErrorCode, Forbidden, IdempotencyConflict, ValidationFailed, VersionConflict
from suv_deals.persistence.database import Conn, fetch_one
from suv_deals.persistence.errors_map import mapped_errors

DEFAULT_TTL: Final = timedelta(hours=24)
MAX_TTL: Final = timedelta(days=7)
MAX_RESULT_BYTES: Final = 64 * 1024
_OPERATION_RE: Final = re.compile(r"^[a-z][a-z0-9_.]{2,79}$")
_HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE_RE: Final = re.compile(r"^[A-Z_]{3,40}$")
_ATTEMPTS: Final = 3


@dataclass(frozen=True, slots=True)
class NewRequest:
    """No usable record existed: run the operation, then `complete` in the same transaction."""

    record_id: UUID


@dataclass(frozen=True, slots=True)
class Replay:
    """Same key, same request, completed: return this original result."""

    result: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReplayError:
    """Same key, same request, recorded as failed: return this error code again."""

    error_code: str


@dataclass(frozen=True, slots=True)
class InProgress:
    """Same key, same request, still running elsewhere: retry later, never run it twice."""

    record_id: UUID


BeginResult = NewRequest | Replay | ReplayError | InProgress


def request_hash_for(operation: str, request: Mapping[str, Any] | BaseModel) -> str:
    """Canonical request hash (domain.reviews); pass the VALIDATED request model."""
    return canonical_request_hash(operation, request)


def _operation(operation: str) -> str:
    if not isinstance(operation, str) or not _OPERATION_RE.fullmatch(operation):
        raise ValidationFailed("operation must be a lower-case name")
    return operation


def _key(key: str) -> str:
    try:
        return validate_idempotency_key(key)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def _ttl(ttl: timedelta) -> timedelta:
    if not timedelta(seconds=1) <= ttl <= MAX_TTL:
        raise ValidationFailed("idempotency ttl must be between one second and seven days")
    return ttl


_INSERT_SQL: Final = (
    "insert into ops.idempotency_records"
    " (workspace_id, principal_id, operation, idempotency_key, request_hash, state, expires_at)"
    " values (%(workspace_id)s, %(principal)s, %(operation)s, %(key)s, %(hash)s, 'in_progress',"
    " now() + %(ttl)s::interval)"
    " on conflict (principal_id, operation, idempotency_key) do nothing"
    " returning id"
)
_SELECT_SQL: Final = (
    "select id, request_hash, state, result, error_code, expires_at <= now() as expired"
    " from ops.idempotency_records"
    " where workspace_id = %(workspace_id)s and principal_id = %(principal)s"
    " and operation = %(operation)s and idempotency_key = %(key)s"
    " for update"
)
_DELETE_EXPIRED_ONE_SQL: Final = (
    "delete from ops.idempotency_records"
    " where workspace_id = %(workspace_id)s and id = %(id)s and expires_at <= now()"
)


async def begin(
    conn: Conn,
    actor: ActorContext,
    operation: str,
    key: str,
    request_hash: str,
    *,
    ttl: timedelta = DEFAULT_TTL,
) -> BeginResult:
    """Start (or replay) an idempotent operation for the authenticated principal."""
    if not isinstance(request_hash, str) or not _HASH_RE.fullmatch(request_hash):
        raise ValidationFailed("request_hash must be a SHA-256 hex digest")
    params = {
        "workspace_id": actor.workspace_id,
        "principal": actor.principal_id,
        "operation": _operation(operation),
        "key": _key(key),
        "hash": request_hash,
        "ttl": _ttl(ttl),
    }
    async with mapped_errors():
        for _ in range(_ATTEMPTS):
            inserted = await fetch_one(conn, _INSERT_SQL, params)
            if inserted is not None:
                return NewRequest(record_id=inserted["id"])
            row = await fetch_one(conn, _SELECT_SQL, params)
            if row is None:
                # Used by this principal in another workspace (invisible here), or purged
                # concurrently; retry the insert, then refuse.
                continue
            if row["expired"]:
                await conn.execute(_DELETE_EXPIRED_ONE_SQL, {**params, "id": row["id"]})
                continue
            if not hmac.compare_digest(str(row["request_hash"]), request_hash):
                raise IdempotencyConflict()
            if row["state"] == "completed":
                return Replay(result=dict(row["result"]))
            if row["state"] == "failed":
                return ReplayError(error_code=str(row["error_code"]))
            return InProgress(record_id=row["id"])
    raise IdempotencyConflict("Idempotency key is already in use for a different request scope")


async def complete(
    conn: Conn, actor: ActorContext, operation: str, key: str, result: Mapping[str, Any]
) -> None:
    """Store the (redacted) result in the same transaction as the domain write."""
    data = _result(result)
    await _finish(conn, actor, operation, key, state="completed", result=data, error_code=None)


async def fail(
    conn: Conn, actor: ActorContext, operation: str, key: str, error_code: ErrorCode | str
) -> None:
    """Record a definitive failure so an identical retry returns the same error code."""
    code = str(getattr(error_code, "value", error_code))
    if not _ERROR_CODE_RE.fullmatch(code):
        raise ValidationFailed("error_code must be an upper-case error code")
    await _finish(conn, actor, operation, key, state="failed", result=None, error_code=code)


async def _finish(
    conn: Conn,
    actor: ActorContext,
    operation: str,
    key: str,
    *,
    state: str,
    result: dict[str, Any] | None,
    error_code: str | None,
) -> None:
    params = {
        "workspace_id": actor.workspace_id,
        "principal": actor.principal_id,
        "operation": _operation(operation),
        "key": _key(key),
        "state": state,
        "result": None if result is None else Jsonb(result),
        "error_code": error_code,
    }
    async with mapped_errors():
        cur = await conn.execute(
            "update ops.idempotency_records set state = %(state)s, result = %(result)s,"
            " error_code = %(error_code)s, completed_at = now()"
            " where workspace_id = %(workspace_id)s and principal_id = %(principal)s"
            " and operation = %(operation)s and idempotency_key = %(key)s and state = 'in_progress'",
            params,
        )
    if cur.rowcount != 1:
        raise VersionConflict("The idempotent request is no longer in progress")


async def delete_expired(conn: Conn, actor: ActorContext, *, limit: int = 1000) -> int:
    """Purge expired records of the actor's workspace (system/owner maintenance)."""
    if actor.principal_kind != "system" and not actor.has(Scope.CONFIG_ADMIN):
        raise Forbidden("Only system maintenance or an owner may purge idempotency records")
    if not 1 <= limit <= 10_000:
        raise ValidationFailed("invalid purge limit")
    async with mapped_errors():
        cur = await conn.execute(
            "delete from ops.idempotency_records where id in ("
            " select id from ops.idempotency_records"
            " where workspace_id = %(workspace_id)s and expires_at <= now()"
            " order by expires_at limit %(limit)s for update skip locked)"
            " and workspace_id = %(workspace_id)s",
            {"workspace_id": actor.workspace_id, "limit": limit},
        )
    return cur.rowcount


def _result(result: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(result)
    try:
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("idempotent result must be plain JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValidationFailed("idempotent result is too large")
    if isinstance(data.get("claim_token"), str) and data["claim_token"]:
        raise ValidationFailed("store the redacted result; never a plaintext claim token")
    return data


__all__ = [
    "BeginResult",
    "InProgress",
    "NewRequest",
    "Replay",
    "ReplayError",
    "begin",
    "complete",
    "delete_expired",
    "fail",
    "request_hash_for",
]
