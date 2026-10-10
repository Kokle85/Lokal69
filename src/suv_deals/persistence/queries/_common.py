"""Shared plumbing of the read-query service: results, signed keyset cursors and row helpers.

Every query function returns a `QueryResult`: the view (``data``), the database time the view was
built at (``as_of``), typed warnings and the next signed cursor. The API/MCP layer turns it into
the shared ``ResponseEnvelope`` with `QueryResult.envelope`.

Keyset cursors (spec 21 "Pagination and errors") come from ``domain.pagination``: the signed body
binds the query name, workspace, principal, the canonical filter hash, the last row's complete
sort tuple (ending in the unique id tie-breaker), an as-of boundary and an expiry. Decoding
verifies the MAC before anything else and rejects altered, expired and mismatched cursors with
``VALIDATION_ERROR`` (``details.cursor`` = ``malformed``/``tampered``/``expired``/``mismatch``).

Server-side faults are never reported as client errors: a missing or short signing key
(`require_secret`) and a stored row that cannot be rendered into its view model (`rendering`)
raise ``INTERNAL_ERROR`` (not retryable, no internal detail in the message).

Review status of a case (`CASE_STATUS_SQL`, `CASE_STATE_SQL`): a claim is a lock, not a review
outcome. A claimed case's review status is its restore state (the prior decision while it still
cites the case's revision, else ``pending``, exactly as release/expiry restore it); the displayed
state is ``claimed`` only while the claim is active, so an expired claim never reads as claimed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from pydantic import SecretStr
from pydantic import ValidationError as ModelValidationError

from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import ValuationState
from suv_deals.domain.pagination import (
    DEFAULT_CURSOR_TTL,
    MIN_SECRET_BYTES,
    CursorPayload,
    decode_cursor,
    encode_cursor,
    keyset_cursor,
)
from suv_deals.errors import AppError, ErrorCode, ValidationFailed
from suv_deals.persistence.database import Conn, fetch_one
from suv_deals.views.common import ResponseEnvelope, ResponseWarning, envelope

#: Valuation states that carry contribution figures (everything else is a research candidate).
FIGURE_STATES: Final = frozenset({ValuationState.ESTIMATED, ValuationState.QUOTE_SUPPORTED})

CursorSecret = bytes | Sequence[bytes]


@dataclass(frozen=True, slots=True)
class QueryResult[DataT]:
    """One read result: the view, its database as-of time, warnings and the next cursor."""

    data: DataT
    as_of: datetime
    warnings: tuple[ResponseWarning, ...] = ()
    next_cursor: str | None = None

    def envelope(self, request_id: str) -> ResponseEnvelope[DataT]:
        """The shared ``ResponseEnvelope`` (duplicate warnings collapsed, at most 50)."""
        return envelope(
            self.data,
            request_id=request_id,
            as_of=self.as_of,
            warnings=self.warnings,
            next_cursor=self.next_cursor,
        )


def _unconfigured() -> AppError:
    return AppError(ErrorCode.INTERNAL_ERROR, "Pagination cursor signing is not configured", retryable=False)


def cursor_secret(secret: SecretStr | str | bytes | None) -> bytes:
    """The cursor signing key from ``Settings.mcp_cursor_signing_secret`` (at least 32 bytes).

    A missing or short secret is a server configuration problem, reported without the value.
    """
    if isinstance(secret, SecretStr):
        secret = secret.get_secret_value()
    raw = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not raw or len(raw) < MIN_SECRET_BYTES:
        raise _unconfigured()
    return raw


def require_secret(secret: object) -> CursorSecret:
    """Check the signing key(s) a paginated read was given BEFORE any work: one key, or the current
    key followed by previous keys, each at least 32 bytes. A bad key is ``INTERNAL_ERROR`` on
    every call (not only when a page happens to need a cursor), never a client validation error."""
    if isinstance(secret, bytes | bytearray):
        keys: tuple[object, ...] = (secret,)
    elif isinstance(secret, Sequence) and not isinstance(secret, str):
        keys = tuple(secret)
    else:
        raise _unconfigured()
    if not keys or any(not isinstance(k, bytes | bytearray) or len(k) < MIN_SECRET_BYTES for k in keys):
        raise _unconfigured()
    return tuple(bytes(k) for k in keys if isinstance(k, bytes | bytearray))


@contextmanager
def rendering(what: str) -> Iterator[None]:
    """Turn a view-model validation failure on STORED data into a typed server error.

    Rows that violate a view invariant (an inconsistent or unreadable stored record) must not
    surface as a raw model error, which an API layer could mistake for a client validation error.
    """
    try:
        yield
    except ModelValidationError:
        raise AppError(
            ErrorCode.INTERNAL_ERROR, f"The stored {what} could not be rendered", retryable=False
        ) from None


async def db_now(conn: Conn) -> datetime:
    """Database time (``clock_timestamp()``), timezone-aware UTC."""
    row = await fetch_one(conn, "select clock_timestamp() as now")
    assert row is not None
    return ensure_utc(row["now"])


# --------------------------------------------------------------------------------------------
# Keyset cursors
# --------------------------------------------------------------------------------------------


def _malformed() -> ValidationFailed:
    return ValidationFailed("Invalid or expired cursor; restart the listing", details={"cursor": "malformed"})


def encode_keyset(
    actor: ActorContext,
    *,
    query: str,
    filters_hash: str,
    last_sort_key: Sequence[object],
    as_of: datetime,
    now: datetime,
    secret: CursorSecret,
    ttl: timedelta = DEFAULT_CURSOR_TTL,
) -> str:
    """Signed cursor positioned after the row whose complete sort tuple is ``last_sort_key``."""
    keys = require_secret(secret)
    payload = keyset_cursor(
        query=query,
        workspace_id=actor.workspace_id,
        principal_id=actor.principal_id,
        filters_hash=filters_hash,
        last_sort_key=last_sort_key,
        as_of=as_of,
        now=now,
        ttl=ttl,
    )
    return encode_cursor(payload, keys)


@dataclass(frozen=True, slots=True)
class KeysetPosition:
    """A verified keyset cursor: the decoded sort tuple and its as-of boundary."""

    sort: tuple[Any, ...]
    as_of: datetime


def decode_keyset(
    cursor: str,
    actor: ActorContext,
    *,
    query: str,
    filters_hash: str,
    now: datetime,
    secret: CursorSecret,
    parsers: Sequence[Callable[[object], Any]],
) -> KeysetPosition:
    """Verify a client cursor (MAC, expiry, query/workspace/principal/filter binding) and parse
    its sort tuple with ``parsers`` (one per sort column). Snapshot cursors are refused."""
    keys = require_secret(secret)
    payload: CursorPayload = decode_cursor(
        cursor,
        keys,
        query=query,
        workspace_id=actor.workspace_id,
        principal_id=actor.principal_id,
        filters_hash=filters_hash,
        now=now,
    )
    if payload.mode != "keyset" or payload.asof is None or len(payload.sort) != len(parsers):
        raise _malformed()
    try:
        values = tuple(parse(value) for parse, value in zip(parsers, payload.sort, strict=True))
    except (TypeError, ValueError):
        raise _malformed() from None
    return KeysetPosition(sort=values, as_of=payload.asof)


def parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("expected an RFC 3339 timestamp")
    return ensure_utc(datetime.fromisoformat(value))


def parse_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise TypeError("expected a UUID string")
    return UUID(value)


def parse_ordinal(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected a non-negative ordinal")
    return value


# --------------------------------------------------------------------------------------------
# Review status of a case (claim-aware)
# --------------------------------------------------------------------------------------------


#: SQL expression over a case aliased ``c``: its review status ignoring any claim (the restore
#: state while claimed: the latest decision while it cites the case's revision, else ``pending``).
CASE_STATUS_SQL: Final = """(case when c.state <> 'claimed' then c.state
       else coalesce((select d.outcome from app.review_decisions d
                       where d.workspace_id = c.workspace_id and d.id = c.latest_decision_id
                         and d.listing_revision_id = c.revision_id), 'pending') end)"""

#: SQL expression over a case aliased ``c``: the state to display, ``claimed`` only while the claim
#: is active at the bound ``%(now)s`` (database time); an expired claim shows its restore state.
CASE_STATE_SQL: Final = (
    "(case when c.state = 'claimed' and c.claim_expires_at > %(now)s::timestamptz then 'claimed'"
    f" else {CASE_STATUS_SQL} end)"
)


# --------------------------------------------------------------------------------------------
# Row helpers
# --------------------------------------------------------------------------------------------


def utc_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


def enum_or[E: StrEnum](enum: type[E], value: object, default: E) -> E:
    """``enum(value)`` for a known value, else ``default`` (stored JSON is never trusted blindly)."""
    if not isinstance(value, str):
        return default
    try:
        return enum(value)
    except ValueError:
        return default


def enum_or_none[E: StrEnum](enum: type[E], value: object) -> E | None:
    if not isinstance(value, str):
        return None
    try:
        return enum(value)
    except ValueError:
        return None


def plain_decimal(value: Decimal | None) -> str | None:
    """Canonical decimal text without exponent or trailing zeros (``187500.000000`` -> ``187500``)."""
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def decimal_or_none(value: object) -> Decimal | None:
    """A finite ``Decimal`` from stored JSON text/number, else ``None`` (never a float)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int | str):
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError):
            return None
        return parsed if parsed.is_finite() else None
    return None


def json_field(document: object, *path: str) -> object:
    """Nested value of a stored JSON document, or ``None`` when any step is missing."""
    value = document
    for part in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def text_list(value: object, *, limit: int, max_chars: int) -> tuple[str, ...]:
    """Bounded tuple of strings from a stored JSON array (non-strings are skipped)."""
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(item)[:max_chars] for item in value if isinstance(item, str))[:limit]


__all__ = [
    "CASE_STATE_SQL",
    "CASE_STATUS_SQL",
    "FIGURE_STATES",
    "CursorSecret",
    "KeysetPosition",
    "QueryResult",
    "cursor_secret",
    "db_now",
    "decimal_or_none",
    "decode_keyset",
    "encode_keyset",
    "enum_or",
    "enum_or_none",
    "json_field",
    "parse_datetime",
    "parse_ordinal",
    "parse_uuid",
    "plain_decimal",
    "rendering",
    "require_secret",
    "text_list",
    "utc_or_none",
]
