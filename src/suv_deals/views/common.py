"""Shared building blocks for every read model (spec sections 7, 21, 23).

- ``ResponseEnvelope[T]`` is the shape of every MCP tool result and dashboard API response:
  ``schema_version``, ``request_id``, ``as_of`` (RFC 3339 UTC), ``data``, typed ``warnings`` and
  ``next_cursor``.
- Financial decimals are strings (``DecimalStr``), never JSON numbers. Unknown money is
  ``AmountView(status="unknown", amount=None)``: a null amount with an explicit label, never 0.
- Every datetime is timezone-aware UTC (naive values are rejected).
- Views are frozen, reject unknown fields and list every field as required in their
  serialization schema, so clients can rely on each key being present.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from functools import cache
from typing import Annotated, Any, Final, Literal, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from suv_deals.clock import ensure_utc
from suv_deals.domain.money import CURRENCY_EXPONENTS, CurrencyCode, FxRate, Money
from suv_deals.errors import AppError, ErrorCode

SCHEMA_VERSION: Final = "1.0"
MAX_CURSOR_LENGTH: Final = 2048
MAX_WARNINGS: Final = 50
MAX_LIST_ITEMS: Final = 500

VIEW_CONFIG: Final = ConfigDict(
    frozen=True,
    extra="forbid",
    json_schema_serialization_defaults_required=True,
    hide_input_in_errors=True,
)


class ViewModel(BaseModel):
    """Base for every read model: frozen, closed, all keys always present in output."""

    model_config = VIEW_CONFIG


# --------------------------------------------------------------------------- scalar types

_DECIMAL_RE: Final = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
DECIMAL_PATTERN: Final = _DECIMAL_RE.pattern


def decimal_str(value: Decimal | int | str) -> str:
    """Canonical fixed-point string for a finite decimal (``-0`` becomes ``0``); floats refused."""
    if isinstance(value, bool | float):
        raise ValueError("binary floats (and booleans) are not decimal amounts; use Decimal or str")
    if isinstance(value, str):
        value = Decimal(value) if value.strip() else Decimal("NaN")
    dec = Decimal(value)
    if not dec.is_finite():
        raise ValueError("decimal must be finite")
    text = format(dec, "f")
    if text.startswith("-") and dec == 0:
        text = text[1:]
    return text


def _coerce_decimal_str(value: object) -> object:
    if isinstance(value, Decimal | int | str) and not isinstance(value, bool):
        try:
            return decimal_str(value)
        except (ValueError, ArithmeticError) as exc:
            raise ValueError("not a finite decimal amount") from exc
    if isinstance(value, float):
        raise ValueError("binary floats are not allowed for decimal amounts; use a decimal string")
    return value


#: A finite decimal serialised as a JSON string (``"2750.00"``); never a JSON number.
DecimalStr = Annotated[
    str,
    BeforeValidator(_coerce_decimal_str),
    Field(pattern=DECIMAL_PATTERN, max_length=60),
]


def _utc(value: datetime) -> datetime:
    return ensure_utc(value)


#: Timezone-aware UTC datetime, serialised as RFC 3339 (``2026-10-06T10:00:00Z``).
UtcDatetime = Annotated[datetime, AfterValidator(_utc)]

_HEX64_PATTERN: Final = r"^[0-9a-f]{64}$"
Sha256Hex = Annotated[str, Field(pattern=_HEX64_PATTERN)]

#: Short machine label such as a readiness value (``needs_import_costs``).
MachineLabel = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")]

_REQUEST_ID_PATTERN: Final = r"^[\x21-\x7e]{1,200}$"
_REQUEST_ID_RE: Final = re.compile(_REQUEST_ID_PATTERN)
MAX_RETRY_AFTER_SECONDS: Final = 86_400


def is_valid_request_id(value: object) -> bool:
    """Printable ASCII without spaces, 1-200 characters (request and correlation ids)."""
    return isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) is not None


def money_display(money: Money) -> Decimal:
    """Round to the currency's minor unit (half-up) for display; comparisons happen upstream."""
    return money.quantized()


# --------------------------------------------------------------------------- amounts

AmountStatus = Literal["known", "unknown", "not_applicable"]


class AmountView(ViewModel):
    """A money amount for display. Unknown is ``amount: null`` with ``status: unknown``, never 0.

    ``amount`` is rounded half-up to the currency's minor unit; the exact arithmetic happened
    on the backend before rounding (spec 3, 18).
    """

    status: AmountStatus
    amount: DecimalStr | None
    currency: CurrencyCode | None
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _consistent(self) -> AmountView:
        if self.status == "known":
            if self.amount is None or self.currency is None:
                raise ValueError("a known amount needs amount and currency")
        elif self.amount is not None:
            raise ValueError(f"an {self.status} amount carries no figure (unknown is never zero)")
        if self.status == "not_applicable" and not self.reason:
            raise ValueError("not_applicable needs a reason")
        if self.currency is not None and self.currency not in CURRENCY_EXPONENTS:
            raise ValueError(f"unsupported currency {self.currency}")
        return self

    @classmethod
    def of(
        cls, money: Money | None, *, unknown_reason: str | None = None, currency: str | None = None
    ) -> AmountView:
        """Known amount from ``Money`` (rounded for display) or an explicit unknown."""
        if money is None:
            return cls.unknown(unknown_reason, currency=currency)
        return cls(status="known", amount=decimal_str(money_display(money)), currency=money.currency)

    @classmethod
    def unknown(cls, reason: str | None = None, *, currency: str | None = None) -> AmountView:
        return cls(status="unknown", amount=None, currency=currency, reason=reason)

    @classmethod
    def not_applicable(cls, reason: str, *, currency: str | None = None) -> AmountView:
        return cls(status="not_applicable", amount=None, currency=currency, reason=reason)

    @classmethod
    def from_minor(
        cls, minor: int | None, currency: str | None, *, unknown_reason: str | None = None
    ) -> AmountView:
        if minor is None or currency is None:
            return cls.unknown(unknown_reason, currency=currency)
        return cls.of(Money.from_minor(minor, currency))


class FxRateView(ViewModel):
    """A recorded FX observation with its explicit direction: 1 ``base`` = ``rate`` ``quote``."""

    base: CurrencyCode
    quote: CurrencyCode
    rate: DecimalStr
    rate_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    provider: str = Field(max_length=100)
    purpose: Literal["reference", "customs", "payment"]
    direction: str = Field(max_length=120)

    @classmethod
    def of(cls, rate: FxRate) -> FxRateView:
        return cls(
            base=rate.base,
            quote=rate.quote,
            rate=decimal_str(rate.rate),
            rate_date=rate.rate_date.isoformat(),
            provider=rate.provider,
            purpose=rate.purpose.value,
            direction=f"1 {rate.base} = {decimal_str(rate.rate)} {rate.quote}",
        )


# --------------------------------------------------------------------------- warnings


class WarningCode(StrEnum):
    """Typed warning codes. Each warning also carries a user-readable explanation."""

    STALE_DATA = "STALE_DATA"
    SOURCE_PAUSED = "SOURCE_PAUSED"
    SOURCE_BLOCKED = "SOURCE_BLOCKED"
    COVERAGE_GAP = "COVERAGE_GAP"
    FIXTURE_DATA = "FIXTURE_DATA"
    RESEARCH_CANDIDATE = "RESEARCH_CANDIDATE"
    VALUATION_INCOMPLETE = "VALUATION_INCOMPLETE"
    VALUATION_STALE = "VALUATION_STALE"
    UNKNOWN_COSTS = "UNKNOWN_COSTS"
    INSUFFICIENT_COMPARABLES = "INSUFFICIENT_COMPARABLES"
    SMALL_COMPARABLE_SAMPLE = "SMALL_COMPARABLE_SAMPLE"
    ASKING_NOT_SALE = "ASKING_NOT_SALE"
    THRESHOLD_PROPOSED = "THRESHOLD_PROPOSED"
    TAX_RULES_UNAPPROVED = "TAX_RULES_UNAPPROVED"
    FX_STALE = "FX_STALE"
    SELLER_CLAIMS_UNVERIFIED = "SELLER_CLAIMS_UNVERIFIED"
    SCORE_NOT_PROBABILITY = "SCORE_NOT_PROBABILITY"
    FROZEN_QUEUE_PROJECTION = "FROZEN_QUEUE_PROJECTION"
    REVISION_NOT_CURRENT = "REVISION_NOT_CURRENT"
    NEW_REVISION_AVAILABLE = "NEW_REVISION_AVAILABLE"
    CLAIM_EXPIRING = "CLAIM_EXPIRING"
    ACTIVATION_BLOCKED = "ACTIVATION_BLOCKED"
    PARTIAL_RESULTS = "PARTIAL_RESULTS"
    DEPENDENCY_DEGRADED = "DEPENDENCY_DEGRADED"


DEFAULT_WARNING_MESSAGES: Final[dict[WarningCode, str]] = {
    WarningCode.STALE_DATA: "Some data is older than its freshness window; recheck before acting.",
    WarningCode.SOURCE_PAUSED: "A source is paused; no new network work runs for it.",
    WarningCode.SOURCE_BLOCKED: "A source reported an access block; its request path is stopped.",
    WarningCode.COVERAGE_GAP: "Coverage is incomplete; search results are not all available inventory.",
    WarningCode.FIXTURE_DATA: "Synthetic fixture data; never a real opportunity and never notified.",
    WarningCode.RESEARCH_CANDIDATE: "Research candidate with unknown costs; not a quantified opportunity.",
    WarningCode.VALUATION_INCOMPLETE: "The valuation is incomplete; unknown items are listed, never zero.",
    WarningCode.VALUATION_STALE: "The valuation is stale; a recalculation is queued.",
    WarningCode.UNKNOWN_COSTS: "Some cost lines are unknown; totals are not shown.",
    WarningCode.INSUFFICIENT_COMPARABLES: "Insufficient MK comparables; targeted research is needed.",
    WarningCode.SMALL_COMPARABLE_SAMPLE: "Small comparable sample; statistics are indicative only.",
    WarningCode.ASKING_NOT_SALE: "Asking prices are advertised amounts, not realized sale prices.",
    WarningCode.THRESHOLD_PROPOSED: "The EUR 1,500 contribution threshold is PROPOSED, not owner-approved.",
    WarningCode.TAX_RULES_UNAPPROVED: (
        "No approved, active import-tax rule set applies; import costs are unknown."
    ),
    WarningCode.FX_STALE: "An FX rate is older than its allowed age.",
    WarningCode.SELLER_CLAIMS_UNVERIFIED: "Condition and document statements are unverified seller claims.",
    WarningCode.SCORE_NOT_PROBABILITY: (
        "The ranking score orders the queue; it is not a probability of profit."
    ),
    WarningCode.FROZEN_QUEUE_PROJECTION: (
        "Queue pages are a frozen projection; claim and submit revalidate the current versions."
    ),
    WarningCode.REVISION_NOT_CURRENT: "The requested revision is not the listing's current revision.",
    WarningCode.NEW_REVISION_AVAILABLE: (
        "A newer listing revision exists; decisions must cite the current one."
    ),
    WarningCode.CLAIM_EXPIRING: "The review claim expires soon.",
    WarningCode.ACTIVATION_BLOCKED: "One or more activation gates are blocked.",
    WarningCode.PARTIAL_RESULTS: "Some results could not be loaded; the page is partial.",
    WarningCode.DEPENDENCY_DEGRADED: "A dependency is degraded; some data may be missing.",
}


class ResponseWarning(ViewModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_serialization_defaults_required=True,
        hide_input_in_errors=True,
        title="Warning",
    )

    code: WarningCode
    message: str = Field(min_length=1, max_length=500)


def warning(code: WarningCode, message: str | None = None) -> ResponseWarning:
    """Warning with the default explanation for ``code`` unless a specific one is given."""
    return ResponseWarning(code=code, message=message or DEFAULT_WARNING_MESSAGES[code])


# --------------------------------------------------------------------------- envelope


class ResponseEnvelope[DataT](ViewModel):
    """Every MCP tool result and dashboard API response (spec 21 "Shared schema rules")."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    request_id: str = Field(pattern=_REQUEST_ID_PATTERN)
    as_of: UtcDatetime
    data: DataT
    warnings: tuple[ResponseWarning, ...] = Field(default=(), max_length=MAX_WARNINGS)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=MAX_CURSOR_LENGTH)

    def to_text(self) -> str:
        """Compact, deterministic JSON text for clients that need a text representation."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )


@cache
def envelope_model_for(model: type[BaseModel]) -> type[BaseModel]:
    """``ResponseEnvelope[model]`` as a class (cached), e.g. for an ``outputSchema``."""
    return cast(type[BaseModel], ResponseEnvelope.__class_getitem__(model))


def envelope[DataT](
    data: DataT,
    *,
    request_id: str,
    as_of: datetime,
    warnings: Sequence[ResponseWarning] = (),
    next_cursor: str | None = None,
) -> ResponseEnvelope[DataT]:
    """Build the response envelope; duplicate warnings (same code and message) are collapsed.

    More than ``MAX_WARNINGS`` distinct warnings are cut to the first ``MAX_WARNINGS - 1`` plus
    one ``PARTIAL_RESULTS`` warning saying how many were omitted, so a long warning list can
    never make the response itself fail.
    """
    unique = tuple(dict.fromkeys(warnings))
    if len(unique) > MAX_WARNINGS:
        omitted = len(unique) - (MAX_WARNINGS - 1)
        unique = (
            *unique[: MAX_WARNINGS - 1],
            warning(WarningCode.PARTIAL_RESULTS, f"{omitted} further warnings were omitted."),
        )
    return ResponseEnvelope[DataT](
        request_id=request_id,
        as_of=as_of,
        data=data,
        warnings=unique,
        next_cursor=next_cursor,
    )


# --------------------------------------------------------------------------- errors

ErrorDetailValue = str | int | bool | None | list[str]


class ErrorPayload(ViewModel):
    """Typed error body shared by MCP tool errors and the dashboard API (spec 21 error codes).

    ``message`` and ``details`` are safe: no SQL, tokens, cookies, URLs with credentials or
    stack traces. A foreign-workspace object is reported exactly like a missing one.
    """

    code: ErrorCode
    message: str = Field(min_length=1, max_length=500)
    retryable: bool
    retry_after_seconds: int | None = Field(default=None, ge=0, le=MAX_RETRY_AFTER_SECONDS)
    correlation_id: str | None = Field(default=None, pattern=_REQUEST_ID_PATTERN)
    details: dict[str, ErrorDetailValue] | None = None

    @classmethod
    def from_app_error(cls, error: AppError, correlation_id: str | None = None) -> ErrorPayload:
        """Safe payload for any ``AppError``. Rendering an error never fails itself.

        ``retry_after_seconds`` is clamped to 0-86,400 (a negative or non-integer hint is
        dropped) and a malformed ``correlation_id`` is omitted rather than raising.
        """
        return cls(
            code=error.code,
            message=error.message[:500] or error.code.value,
            retryable=error.retryable,
            retry_after_seconds=_safe_retry_after(error.retry_after_seconds),
            correlation_id=correlation_id if is_valid_request_id(correlation_id) else None,
            details=_safe_details(error.details),
        )


def _safe_retry_after(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, MAX_RETRY_AFTER_SECONDS)


def _safe_details(details: dict[str, Any]) -> dict[str, ErrorDetailValue] | None:
    """Keep only JSON-scalar or string-list details with short keys; drop everything else."""
    safe: dict[str, ErrorDetailValue] = {}
    for key, value in sorted(details.items()):
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            continue
        if value is None or isinstance(value, bool | int):
            safe[key] = value
        elif isinstance(value, str):
            safe[key] = value[:300]
        elif isinstance(value, list | tuple) and all(isinstance(v, str) for v in value):
            safe[key] = [v[:200] for v in value[:50]]
    return safe or None


def quantize_eur(value: Decimal) -> Decimal:
    """Round a EUR figure to cents (half-up) for display only."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
