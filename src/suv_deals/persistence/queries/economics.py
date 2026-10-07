"""Valuation and MK comparable reads (spec 15-18, 21 ``deals_get_valuation``/``deals_get_comparables``,
spec 23 screen 4).

- `get_valuation` reconstructs the stored valuation from its persisted inputs
  (``valuation_repo.load_valuation`` re-validates the domain ``Valuation`` and its dependency
  fingerprint) and renders ``ValuationView``: scenario lines with statuses, totals only for
  complete scenarios (``known_subtotal`` otherwise), unknown figures as ``unknown`` (never 0),
  the threshold labelled ``PROPOSED`` until approved, versions, expiry and the fixture label.
- `get_comparables` pages the members of one immutable comparable set by their stable ordinal
  (selected first, then excluded). The signed keyset cursor carries ``(ordinal, observation_id)``
  and is bound to the set id and ``include_excluded`` through the filter hash, so it cannot be
  replayed against another set or the other member selection.

Scope ``deals:read``; missing and foreign-workspace ids raise the same `NotFound`. A stored record
that cannot be reconstructed is a server-side ``INTERNAL_ERROR`` (never a client validation error).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Scope, ValuationState
from suv_deals.domain.pagination import filter_hash, validate_limit
from suv_deals.errors import AppError, ErrorCode, ValidationFailed
from suv_deals.mcp.schemas import DealsGetComparablesInput
from suv_deals.persistence import market_repo, valuation_repo
from suv_deals.persistence.database import Conn
from suv_deals.persistence.queries._common import (
    FIGURE_STATES,
    CursorSecret,
    QueryResult,
    db_now,
    decode_keyset,
    encode_keyset,
    parse_ordinal,
    parse_uuid,
    rendering,
    require_secret,
)
from suv_deals.views.common import ResponseWarning, WarningCode, warning
from suv_deals.views.comparables import ComparableSetView
from suv_deals.views.valuations import ValuationView

COMPARABLES_QUERY: Final = "deals_get_comparables"


def _unreadable(what: str) -> AppError:
    return AppError(
        ErrorCode.INTERNAL_ERROR, f"The stored {what} could not be reconstructed", retryable=False
    )


def valuation_warnings(view: ValuationView) -> list[ResponseWarning]:
    """Typed warnings for one valuation view (stale, incomplete, unknown costs, approval labels)."""
    warnings: list[ResponseWarning] = []
    if view.state == ValuationState.STALE:
        warnings.append(warning(WarningCode.VALUATION_STALE))
    elif view.state not in FIGURE_STATES:
        warnings.append(warning(WarningCode.VALUATION_INCOMPLETE))
    if view.research_candidate:
        warnings.append(warning(WarningCode.RESEARCH_CANDIDATE))
    if view.is_fixture:
        warnings.append(warning(WarningCode.FIXTURE_DATA))
    if view.threshold is None or view.threshold.label == "PROPOSED":
        warnings.append(warning(WarningCode.THRESHOLD_PROPOSED))
    if view.tax is None or not view.tax.production_ready:
        warnings.append(warning(WarningCode.TAX_RULES_UNAPPROVED))
    if any(not s.complete for s in view.scenarios) or any(
        line.status.value == "unknown" for line in view.cost_lines
    ):
        warnings.append(warning(WarningCode.UNKNOWN_COSTS))
    if view.proceeds is not None and view.proceeds.basis == "mk_asking_prices":
        warnings.append(warning(WarningCode.ASKING_NOT_SALE))
    return warnings


async def get_valuation(conn: Conn, actor: ActorContext, valuation_id: UUID) -> QueryResult[ValuationView]:
    """``deals_get_valuation``: the versioned scenario breakdown of one stored valuation."""
    actor.require(Scope.DEALS_READ)
    now = await db_now(conn)
    try:
        stored = await valuation_repo.load_valuation(conn, actor, valuation_id)
        with rendering("valuation"):
            view = stored.view()
    except ValidationFailed:
        raise _unreadable("valuation") from None
    return QueryResult(data=view, as_of=now, warnings=tuple(valuation_warnings(view)))


def comparable_warnings(view: ComparableSetView) -> list[ResponseWarning]:
    warnings = [warning(WarningCode.ASKING_NOT_SALE)]
    if view.sample_quality == "insufficient":
        warnings.append(warning(WarningCode.INSUFFICIENT_COMPARABLES))
    elif view.sample_quality == "small":
        warnings.append(warning(WarningCode.SMALL_COMPARABLE_SAMPLE))
    if view.is_fixture:
        warnings.append(warning(WarningCode.FIXTURE_DATA))
    return warnings


async def get_comparables(
    conn: Conn,
    actor: ActorContext,
    set_id: UUID,
    *,
    include_excluded: bool = False,
    cursor: str | None = None,
    limit: int | None = None,
    secret: CursorSecret,
) -> QueryResult[ComparableSetView]:
    """``deals_get_comparables``: one page of members of an immutable comparable set.

    The cursor's filter hash is that of ``DealsGetComparablesInput.filters()`` (set id and
    ``include_excluded``), so it cannot be replayed against another set or member selection."""
    actor.require(Scope.DEALS_READ)
    keys = require_secret(secret)
    size = validate_limit(limit)
    filters_hash = filter_hash(
        DealsGetComparablesInput(comparable_set_id=set_id, include_excluded=bool(include_excluded)).filters()
    )
    now = await db_now(conn)
    ordinal_start = 0
    as_of = now
    if cursor is not None:
        position = decode_keyset(
            cursor,
            actor,
            query=COMPARABLES_QUERY,
            filters_hash=filters_hash,
            now=now,
            secret=keys,
            parsers=(parse_ordinal, parse_uuid),
        )
        ordinal_start = int(position.sort[0]) + 1
        as_of = position.as_of
    try:
        with rendering("comparable set"):
            view, next_ordinal = await market_repo.get_comparable_set_view(
                conn,
                actor,
                set_id,
                include_excluded=bool(include_excluded),
                ordinal_start=ordinal_start,
                limit=size,
            )
    except ValidationFailed:
        raise _unreadable("comparable set") from None
    next_cursor = None
    if next_ordinal is not None and view.members:
        last = view.members[-1]
        next_cursor = encode_keyset(
            actor,
            query=COMPARABLES_QUERY,
            filters_hash=filters_hash,
            last_sort_key=(last.ordinal, last.observation_id),
            as_of=as_of,
            now=now,
            secret=keys,
        )
    return QueryResult(
        data=view, as_of=now, warnings=tuple(comparable_warnings(view)), next_cursor=next_cursor
    )


__all__ = [
    "COMPARABLES_QUERY",
    "comparable_warnings",
    "get_comparables",
    "get_valuation",
    "valuation_warnings",
]
