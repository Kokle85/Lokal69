"""Signed opaque pagination cursors (spec section 21 "Pagination and errors").

No offset pagination. A cursor is ``base64url(body) "." base64url(HMAC-SHA256(secret, body))``
where the canonical JSON body binds:

- the query name and the filter hash (sha256 of the canonical filters, without cursor/limit),
- the workspace id and the authenticated principal id,
- either a frozen snapshot (``ops.query_snapshots`` id + next ordinal; used for review queues,
  because an as-of timestamp alone does not freeze mutable priority/status), or a keyset
  position (the last row's full sort tuple ending in its unique id tie-breaker + an as-of
  boundary),
- issue and expiry times (lifetime at most one day).

Decoding verifies the MAC in constant time against the current and any previous server secrets
*before* parsing the body, then rejects expired, future-issued and mismatched cursors (other
workspace, principal, query or filters) with ``VALIDATION_ERROR``. Cursors are at most 2,048
characters (spec 21 schema). Claim/submit never trust a cursor or snapshot projection; they
revalidate current versions.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.listings import canonical_json, sha256_json
from suv_deals.errors import ValidationFailed

CURSOR_FORMAT_VERSION: Final = 1
MAX_CURSOR_LENGTH: Final = 2048
MAX_CURSOR_LIFETIME: Final = timedelta(days=1)
DEFAULT_CURSOR_TTL: Final = timedelta(minutes=30)
ISSUED_AT_SKEW: Final = timedelta(seconds=60)
MIN_SECRET_BYTES: Final = 32
DEFAULT_LIMIT: Final = 25
MAX_LIMIT: Final = 100
MAX_SNAPSHOT_ORDINAL: Final = 10_000
MAX_SORT_VALUES: Final = 8
_MAC_CONTEXT: Final = b"suv-deals/cursor/v1\x00"
_B64_RE: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_UUID_RE: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PAGINATION_KEYS: Final = frozenset({"cursor", "limit"})

SortValue = str | int | bool | None
CursorProblem = Literal["malformed", "tampered", "expired", "mismatch"]


class CursorPayload(BaseModel):
    """The signed cursor body. Short keys keep cursors well below 2,048 characters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    v: Literal[1] = CURSOR_FORMAT_VERSION
    q: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")  # query name, e.g. reviews_list_pending
    ws: UUID
    pr: UUID
    fh: str = Field(pattern=r"^[0-9a-f]{64}$")
    sort: tuple[SortValue, ...] = Field(default=(), max_length=MAX_SORT_VALUES)
    snap: UUID | None = None
    ord: int | None = Field(default=None, ge=0, le=MAX_SNAPSHOT_ORDINAL)
    asof: datetime | None = None
    iat: datetime
    exp: datetime

    @field_validator("sort")
    @classmethod
    def _bounded_values(cls, value: tuple[SortValue, ...]) -> tuple[SortValue, ...]:
        for item in value:
            if isinstance(item, str) and len(item) > 200:
                raise ValueError("sort values are at most 200 characters")
        return value

    @field_validator("asof", "iat", "exp")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _mode(self) -> CursorPayload:
        snapshot_mode = self.snap is not None or self.ord is not None
        if snapshot_mode:
            if self.snap is None or self.ord is None:
                raise ValueError("snapshot cursors need both snapshot id and ordinal")
            if self.sort or self.asof is not None:
                raise ValueError("snapshot cursors carry no keyset position")
        else:
            if not self.sort or self.asof is None:
                raise ValueError("keyset cursors need a sort tuple and an as-of boundary")
            last = self.sort[-1]
            if not (isinstance(last, str) and _UUID_RE.fullmatch(last)):
                raise ValueError("the sort tuple must end with the unique id tie-breaker")
        if self.exp <= self.iat:
            raise ValueError("cursor expiry must be after issue time")
        if self.exp - self.iat > MAX_CURSOR_LIFETIME:
            raise ValueError("cursor lifetime exceeds one day")
        return self

    @property
    def mode(self) -> Literal["snapshot", "keyset"]:
        return "snapshot" if self.snap is not None else "keyset"


# ---------------------------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------------------------


def filter_hash(filters: Mapping[str, Any]) -> str:
    """Canonical hash of the query filters. ``cursor``/``limit`` and ``None`` values are ignored,
    so an omitted filter and an explicit ``null`` are the same query."""
    canonical = {
        k: sort_value_of(v) if not isinstance(v, list | tuple | dict) else v
        for k, v in filters.items()
        if k not in _PAGINATION_KEYS and v is not None
    }
    return sha256_json(canonical)


def sort_value_of(value: object) -> SortValue:
    """Normalise one sort-key value to a JSON scalar (Decimal and times as strings, never floats)."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Enum):
        return sort_value_of(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    raise ValidationFailed(f"unsupported sort value type {type(value).__name__}")


def snapshot_cursor(
    *,
    query: str,
    workspace_id: UUID,
    principal_id: UUID,
    filters_hash: str,
    snapshot_id: UUID,
    next_ordinal: int,
    now: datetime,
    snapshot_expires_at: datetime,
    ttl: timedelta = DEFAULT_CURSOR_TTL,
) -> CursorPayload:
    """Cursor for the next page of a frozen ``ops.query_snapshots`` result (never outlives it)."""
    now = _aware(now)
    expires = min(now + _ttl(ttl), _aware(snapshot_expires_at))
    if expires <= now:
        raise ValidationFailed("the query snapshot has expired; re-query")
    return _payload(
        q=query,
        ws=workspace_id,
        pr=principal_id,
        fh=filters_hash,
        snap=snapshot_id,
        ord=next_ordinal,
        iat=now,
        exp=expires,
    )


def keyset_cursor(
    *,
    query: str,
    workspace_id: UUID,
    principal_id: UUID,
    filters_hash: str,
    last_sort_key: Sequence[object],
    as_of: datetime,
    now: datetime,
    ttl: timedelta = DEFAULT_CURSOR_TTL,
) -> CursorPayload:
    """Cursor after the row whose complete sort tuple (ending in its unique id) is ``last_sort_key``."""
    now = _aware(now)
    return _payload(
        q=query,
        ws=workspace_id,
        pr=principal_id,
        fh=filters_hash,
        sort=tuple(sort_value_of(v) for v in last_sort_key),
        asof=_aware(as_of),
        iat=now,
        exp=now + _ttl(ttl),
    )


def _payload(**data: Any) -> CursorPayload:
    try:
        return CursorPayload(**data)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) or "cursor" for err in exc.errors()})
        raise ValidationFailed("invalid cursor parameters", details={"fields": fields}) from None


def _ttl(ttl: timedelta) -> timedelta:
    if not timedelta(0) < ttl <= MAX_CURSOR_LIFETIME:
        raise ValidationFailed("cursor ttl must be positive and at most one day")
    return ttl


# ---------------------------------------------------------------------------------------------
# Encode / decode
# ---------------------------------------------------------------------------------------------


def encode_cursor(payload: CursorPayload, secret: bytes | Sequence[bytes]) -> str:
    """Sign with the current (first) secret. Raises if the token would exceed 2,048 characters."""
    key = _secrets(secret)[0]
    body = _b64encode(canonical_json(payload.model_dump(mode="json")).encode("utf-8"))
    token = f"{body}.{_b64encode(_mac(key, body))}"
    if len(token) > MAX_CURSOR_LENGTH:
        raise ValidationFailed("cursor would exceed the maximum length")
    return token


def decode_cursor(
    token: object,
    secret: bytes | Sequence[bytes],
    *,
    query: str,
    workspace_id: UUID,
    principal_id: UUID,
    filters_hash: str,
    now: datetime,
) -> CursorPayload:
    """Verify and decode a client cursor bound to this query, workspace, principal and filters."""
    keys = _secrets(secret)
    now = _aware(now)
    if not isinstance(token, str) or not 0 < len(token) <= MAX_CURSOR_LENGTH:
        raise _invalid("malformed")
    parts = token.split(".")
    if len(parts) != 2 or not all(_B64_RE.fullmatch(p) for p in parts):
        raise _invalid("malformed")
    body, signature = parts
    try:
        presented = _b64decode(signature)
    except (binascii.Error, ValueError):
        raise _invalid("malformed") from None
    # Check every key without early exit so timing does not reveal which key matched.
    matched = False
    for key in keys:
        matched |= hmac.compare_digest(_mac(key, body), presented)
    if not matched:
        raise _invalid("tampered")
    try:
        raw = json.loads(_b64decode(body).decode("utf-8"))
        payload = CursorPayload.model_validate(raw)
    except (ValueError, ValidationError, UnicodeDecodeError, binascii.Error):
        raise _invalid("malformed") from None
    if payload.exp <= now:
        raise _invalid("expired")
    if payload.iat > now + ISSUED_AT_SKEW:
        raise _invalid("malformed")
    same = (
        payload.q == query
        and payload.ws == workspace_id
        and payload.pr == principal_id
        and hmac.compare_digest(payload.fh, filters_hash)
    )
    if not same:
        raise _invalid("mismatch")
    return payload


def validate_limit(limit: int | None) -> int:
    """Page size: default 25, 1..100 (spec 21 ``$defs.limit``)."""
    if limit is None:
        return DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValidationFailed("limit must be an integer between 1 and 100")
    return limit


def next_snapshot_ordinal(ordinal: int, returned: int, total: int) -> int | None:
    """Ordinal for the next page of a frozen snapshot, or ``None`` when the snapshot is exhausted."""
    if ordinal < 0 or returned < 0 or total < 0 or ordinal + returned > total:
        raise ValidationFailed("inconsistent snapshot page bounds")
    following = ordinal + returned
    return following if following < total else None


def _secrets(secret: bytes | Sequence[bytes]) -> tuple[bytes, ...]:
    keys = (secret,) if isinstance(secret, bytes | bytearray) else tuple(secret)
    if not keys:
        raise ValidationFailed("cursor signing secret is not configured")
    for key in keys:
        if not isinstance(key, bytes | bytearray) or len(key) < MIN_SECRET_BYTES:
            raise ValidationFailed("cursor signing secret must be at least 32 bytes")
    return tuple(bytes(k) for k in keys)


def _mac(key: bytes, body: str) -> bytes:
    return hmac.new(key, _MAC_CONTEXT + body.encode("ascii"), hashlib.sha256).digest()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _invalid(problem: CursorProblem) -> ValidationFailed:
    return ValidationFailed("Invalid or expired cursor; restart the listing", details={"cursor": problem})


def _aware(value: datetime) -> datetime:
    try:
        return ensure_utc(value)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
