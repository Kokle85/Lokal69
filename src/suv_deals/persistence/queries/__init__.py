"""Read-query service: builds the shared views (``suv_deals.views``) from the database for the
dashboard API and the MCP tools (spec 21, 23, 30, 32, 37.8-37.10).

Every function is read-only (the review queue's first page stores its frozen snapshot), takes an
`ActorContext`, checks its scope, and filters every statement by the actor's workspace (RLS is
defence in depth). Missing and foreign-workspace ids raise the same ``NotFound``. Results are
`QueryResult` objects; ``QueryResult.envelope(request_id)`` produces the shared
``ResponseEnvelope`` with ``as_of`` in database time, typed warnings and the next signed cursor.

Run the functions inside ``transactions.unit_of_work(db, actor)`` (short transactions, no network
I/O); `health_view` takes the ``Database`` itself so it can report an outage instead of failing.
The cursor signing key comes from ``Settings.mcp_cursor_signing_secret`` (`cursor_secret`).
"""

from __future__ import annotations

from suv_deals.persistence.queries._common import QueryResult, cursor_secret
from suv_deals.persistence.queries.candidates import (
    CANDIDATES_QUERY,
    get_candidate,
    list_candidates,
)
from suv_deals.persistence.queries.economics import (
    COMPARABLES_QUERY,
    get_comparables,
    get_valuation,
)
from suv_deals.persistence.queries.inquiries import (
    INQUIRIES_QUERY,
    REPLIES_QUERY,
    EvaluationInputs,
    MailWorkerHealthView,
    evaluation_inputs,
    evaluation_report,
    get_inquiry,
    get_reply,
    list_inquiries,
    list_replies,
    mail_worker_health_view,
)
from suv_deals.persistence.queries.lifecycle import (
    V11_TABLES,
    CoverageLagsView,
    ListingLifecycleView,
    SourceLagView,
    coverage_lags_view,
    listing_lifecycle_view,
)
from suv_deals.persistence.queries.operations import (
    OUTBOX_QUERY,
    SCHEMA_MARKERS,
    SchemaMarker,
    health_view,
    outbox_attention_view,
    overview_view,
    settings_view,
    sources_view,
)
from suv_deals.persistence.queries.reviews import get_review_case, review_queue

__all__ = [
    "CANDIDATES_QUERY",
    "COMPARABLES_QUERY",
    "INQUIRIES_QUERY",
    "OUTBOX_QUERY",
    "REPLIES_QUERY",
    "SCHEMA_MARKERS",
    "V11_TABLES",
    "CoverageLagsView",
    "EvaluationInputs",
    "ListingLifecycleView",
    "MailWorkerHealthView",
    "QueryResult",
    "SchemaMarker",
    "SourceLagView",
    "coverage_lags_view",
    "cursor_secret",
    "evaluation_inputs",
    "evaluation_report",
    "get_candidate",
    "get_comparables",
    "get_inquiry",
    "get_reply",
    "get_review_case",
    "get_valuation",
    "health_view",
    "list_candidates",
    "list_inquiries",
    "list_replies",
    "listing_lifecycle_view",
    "mail_worker_health_view",
    "outbox_attention_view",
    "overview_view",
    "review_queue",
    "settings_view",
    "sources_view",
]
