"""Review queue and review case reads (spec 21 ``reviews_list_pending``, ``GET /api/reviews/{case_id}``).

The queue itself is owned by ``persistence.reviews_repo`` (frozen ``ops.query_snapshots``
projections bound to principal, workspace and filter hash; claim/submit always revalidate). This
module only adds the read-service result shape and typed warnings, so the dashboard API and the MCP
tools build the queue in exactly one place.

Note: the first queue page *writes* its snapshot row (``ops.query_snapshots``), so the transaction
must commit (``transactions.unit_of_work``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final
from uuid import UUID

from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Scope
from suv_deals.mcp.schemas import ReviewsListPendingInput
from suv_deals.persistence import reviews_repo
from suv_deals.persistence.database import Conn
from suv_deals.persistence.queries._common import CursorSecret, QueryResult, db_now, rendering, require_secret
from suv_deals.views.common import ResponseWarning, WarningCode, warning
from suv_deals.views.reviews import ReviewCaseView, ReviewQueuePage

CLAIM_EXPIRY_WARNING: Final = timedelta(minutes=1)


async def review_queue(
    conn: Conn,
    actor: ActorContext,
    query: ReviewsListPendingInput,
    *,
    secret: CursorSecret,
) -> QueryResult[ReviewQueuePage]:
    """``reviews_list_pending``: one page of the frozen pending-review projection."""
    actor.require(Scope.REVIEWS_READ)
    keys = require_secret(secret)
    filters = reviews_repo.ReviewQueueFilters(include_needs_information=query.include_needs_information)
    with rendering("review queue"):
        result = await reviews_repo.list_pending_queue(
            conn, actor, filters, query.cursor, secret=keys, limit=query.limit
        )
    now = await db_now(conn)
    page = result.page
    warnings: list[ResponseWarning] = [warning(WarningCode.FROZEN_QUEUE_PROJECTION)]
    if any(item.rank is not None for item in page.items):
        warnings.append(warning(WarningCode.SCORE_NOT_PROBABILITY))
    if any(item.is_fixture for item in page.items):
        warnings.append(warning(WarningCode.FIXTURE_DATA))
    if any(item.research_candidate for item in page.items):
        warnings.append(warning(WarningCode.RESEARCH_CANDIDATE))
    if any(
        item.claim.held_by_caller
        and item.claim.expires_at is not None
        and item.claim.expires_at - now <= CLAIM_EXPIRY_WARNING
        for item in page.items
    ):
        warnings.append(warning(WarningCode.CLAIM_EXPIRING))
    return QueryResult(data=page, as_of=now, warnings=tuple(warnings), next_cursor=result.next_cursor)


async def get_review_case(conn: Conn, actor: ActorContext, case_id: UUID) -> QueryResult[ReviewCaseView]:
    """One review case with its candidate, valuation reference and decision history."""
    actor.require(Scope.REVIEWS_READ)
    with rendering("review case"):
        view = await reviews_repo.get_case(conn, actor, case_id)
    now = await db_now(conn)
    warnings: list[ResponseWarning] = []
    if view.is_fixture:
        warnings.append(warning(WarningCode.FIXTURE_DATA))
    if view.candidate.freshness.stale:
        warnings.append(warning(WarningCode.STALE_DATA))
    return QueryResult(data=view, as_of=now, warnings=tuple(warnings))


__all__ = ["get_review_case", "review_queue"]
