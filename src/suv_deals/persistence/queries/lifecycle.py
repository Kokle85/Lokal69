"""Lifecycle and coverage-lag reads (spec 30, 37.9): what the current schema can show today.

Spec 37.9 asks for separate lags: source scan lag, detection delay (only with a trustworthy source
publication time), detail freshness, mail-reply detection lag and notification processing lag.
Each is a ``domain.lifecycle.LagMeasurement``: ``unknown`` or ``inconsistent`` carry no value, never
zero, and a configured interval (15-minute scheduler) is context only, never an observed latency.

Today's sources:

- source scan lag: the last complete crawl run per source (``ops.crawl_runs``);
- detail freshness: ``app.listings.last_detail_success_at``;
- detection delay: ALWAYS ``unknown`` for now: adapters do not yet persist a validated statement
  that a source publication time is trustworthy, so first-seen is never presented as "new today";
- notification processing lag: the newest provider-accepted, non-fixture ``ops.outbox`` row
  (event creation to provider acceptance; says nothing about dot's processing);
- mail-reply detection lag: ``unknown``: the v1.1 reply tables (spec 37.8) belong to another work
  package. `V11_TABLES` lists the tables whose presence is reported, so the views show the hook
  honestly until those readers are added (``app.availability_events`` then becomes the availability
  history source instead of the ``listing.availability`` audit events).

These models are local to the read service until a shared view is added to ``views.operations``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Availability, Scope
from suv_deals.domain.lifecycle import (
    LagMeasurement,
    LagStatus,
    TrustedSourceTime,
    detail_freshness,
    detection_delay,
    notification_processing_lag,
    source_scan_lag,
)
from suv_deals.domain.listings import SourceTimestamp
from suv_deals.errors import NotFound
from suv_deals.persistence.database import Conn, fetch_all, fetch_one
from suv_deals.persistence.errors_map import mapped_errors
from suv_deals.persistence.queries._common import QueryResult, db_now, enum_or, rendering, utc_or_none
from suv_deals.persistence.queries.operations import source_rows, source_state
from suv_deals.views.common import ResponseWarning, WarningCode, warning
from suv_deals.views.operations import SourceRunState

#: Spec 37.8 tables owned by the v1.1 work packages; their presence is reported, not assumed.
V11_TABLES: Final[tuple[str, ...]] = (
    "app.availability_events",
    "app.seller_entities",
    "app.seller_contacts",
    "app.seller_inquiries",
    "app.seller_replies",
    "ops.mail_worker_checkpoints",
)
DETECTION_NOTE: Final = (
    "Detection delay needs a trustworthy source publication time; none is recorded, so it is unknown. "
    "First seen by this system never makes an older ad new."
)
MAIL_LAG_REASON: Final = "seller reply ingest is not wired into this read service yet"
AVAILABILITY_SOURCE_NOTE: Final = (
    "Availability history comes from listing.availability audit events until app.availability_events "
    "is read here."
)

_MODEL = ConfigDict(frozen=True, extra="forbid")


class V11TableState(BaseModel):
    model_config = _MODEL

    relation: str = Field(max_length=80)
    present: bool


class SourceLagView(BaseModel):
    """Per-source scan lag (time since the last complete, finished crawl run)."""

    model_config = _MODEL

    source_id: UUID
    source_key: str = Field(max_length=80)
    state: SourceRunState
    last_successful_scan_at: datetime | None
    last_complete_scan_at: datetime | None
    source_scan_lag: LagMeasurement


class CoverageLagsView(BaseModel):
    model_config = _MODEL

    sources: tuple[SourceLagView, ...] = Field(max_length=200)
    notification_processing_lag: LagMeasurement
    mail_reply_detection_lag: LagMeasurement
    v11_tables: tuple[V11TableState, ...]
    notes: tuple[str, ...]


class ListingLifecycleView(BaseModel):
    """One source listing's own lifecycle evidence (never merged with another source's)."""

    model_config = _MODEL

    listing_id: UUID
    source_id: UUID
    source_key: str = Field(max_length=80)
    source_state: SourceRunState
    first_seen_at: datetime
    last_seen_on_search_at: datetime | None
    last_detail_success_at: datetime | None
    last_availability_check_at: datetime | None
    last_complete_source_scan_at: datetime | None
    source_published_at: datetime | None
    source_published_trusted: bool
    availability: Availability
    detail_freshness: LagMeasurement
    detection_delay: LagMeasurement
    availability_history_source: Literal["audit_events", "availability_events"]
    notes: tuple[str, ...]


_TABLES_SQL: Final = """
select m.rel, pg_catalog.to_regclass(m.rel) is not null as present
  from unnest(%(rels)s::text[]) with ordinality as m(rel, ord)
 order by m.ord
"""
_LISTING_SQL: Final = """
select l.id, l.source_id, l.first_seen_at, l.last_detail_success_at, l.last_availability_check_at,
       l.availability, r.normalized -> 'source_published_at' as source_published,
       (select max(o.observed_at) from app.listing_observations o
         where o.workspace_id = l.workspace_id and o.listing_id = l.id) as last_seen_on_search_at
  from app.listings l
  left join app.listing_revisions r
    on r.workspace_id = l.workspace_id and r.listing_id = l.id and r.id = l.current_revision_id
 where l.workspace_id = %(ws)s and l.id = %(listing_id)s
"""
_LAST_DELIVERY_SQL: Final = """
select o.event_created_at, o.provider_accepted_at
  from ops.outbox o
 where o.workspace_id = %(ws)s and o.provider_accepted_at is not null and not o.is_fixture
 order by o.provider_accepted_at desc, o.id desc
 limit 1
"""


async def v11_table_states(conn: Conn) -> tuple[V11TableState, ...]:
    """Which spec 37.8 tables exist in this database (catalog lookup only)."""
    async with mapped_errors():
        rows = await fetch_all(conn, _TABLES_SQL, {"rels": list(V11_TABLES)})
    return tuple(V11TableState(relation=r["rel"], present=bool(r["present"])) for r in rows)


def _interval(row: Mapping[str, object]) -> timedelta | None:
    seconds = row["interval_seconds"]
    return timedelta(seconds=seconds) if isinstance(seconds, int) and seconds > 0 else None


async def coverage_lags_view(conn: Conn, actor: ActorContext) -> QueryResult[CoverageLagsView]:
    """Per-source scan lag plus the notification and mail-reply lags (spec 37.9)."""
    actor.require(Scope.DEALS_READ)
    now = await db_now(conn)
    rows = await source_rows(conn, actor)
    async with mapped_errors():
        delivery = await fetch_one(conn, _LAST_DELIVERY_SQL, {"ws": actor.workspace_id})
    tables = await v11_table_states(conn)
    with rendering("coverage lags"):
        sources = tuple(
            SourceLagView(
                source_id=r["id"],
                source_key=r["source_key"],
                state=source_state(r),
                last_successful_scan_at=utc_or_none(r["last_successful_scan_at"]),
                last_complete_scan_at=utc_or_none(r["last_complete_scan_at"]),
                source_scan_lag=source_scan_lag(
                    utc_or_none(r["last_complete_scan_at"]), now=now, configured_interval=_interval(r)
                ),
            )
            for r in rows
        )
        view = CoverageLagsView(
            sources=sources,
            notification_processing_lag=notification_processing_lag(
                None if delivery is None else utc_or_none(delivery["event_created_at"]),
                None if delivery is None else utc_or_none(delivery["provider_accepted_at"]),
            ),
            mail_reply_detection_lag=LagMeasurement(
                name="mail_reply_detection_lag", status=LagStatus.UNKNOWN, reason=MAIL_LAG_REASON
            ),
            v11_tables=tables,
            notes=(DETECTION_NOTE, AVAILABILITY_SOURCE_NOTE),
        )
    warnings: list[ResponseWarning] = []
    if any(
        s.source_scan_lag.status != LagStatus.MEASURED for s in sources if s.state != SourceRunState.DISABLED
    ):
        warnings.append(warning(WarningCode.COVERAGE_GAP))
    return QueryResult(data=view, as_of=now, warnings=tuple(warnings))


async def listing_lifecycle_view(
    conn: Conn, actor: ActorContext, listing_id: UUID
) -> QueryResult[ListingLifecycleView]:
    """First/last seen, detail freshness and detection delay of one source listing."""
    actor.require(Scope.DEALS_READ)
    now = await db_now(conn)
    async with mapped_errors():
        row = await fetch_one(conn, _LISTING_SQL, {"ws": actor.workspace_id, "listing_id": listing_id})
    if row is None:
        raise NotFound("Listing not found")
    sources = await source_rows(conn, actor, source_id=row["source_id"])
    if not sources:  # pragma: no cover - the composite FK guarantees the source
        raise NotFound("Listing not found")
    source = sources[0]
    try:
        stamp = SourceTimestamp.model_validate(row["source_published"] or {})
    except ValidationError:
        stamp = SourceTimestamp()
    published = TrustedSourceTime.from_source_timestamp(stamp, trustworthy=False)
    first_seen = utc_or_none(row["first_seen_at"])
    last_detail = utc_or_none(row["last_detail_success_at"])
    assert first_seen is not None
    with rendering("listing lifecycle"):
        view = ListingLifecycleView(
            listing_id=row["id"],
            source_id=row["source_id"],
            source_key=source["source_key"],
            source_state=source_state(source),
            first_seen_at=first_seen,
            last_seen_on_search_at=utc_or_none(row["last_seen_on_search_at"]),
            last_detail_success_at=last_detail,
            last_availability_check_at=utc_or_none(row["last_availability_check_at"]),
            last_complete_source_scan_at=utc_or_none(source["last_complete_scan_at"]),
            source_published_at=published.value,
            source_published_trusted=published.usable,
            availability=enum_or(Availability, row["availability"], Availability.UNKNOWN),
            detail_freshness=detail_freshness(last_detail, now=now),
            detection_delay=detection_delay(first_seen, published),
            availability_history_source="audit_events",
            notes=(DETECTION_NOTE, AVAILABILITY_SOURCE_NOTE),
        )
    warnings: list[ResponseWarning] = []
    if view.detail_freshness.status != LagStatus.MEASURED:
        warnings.append(warning(WarningCode.STALE_DATA))
    return QueryResult(data=view, as_of=now, warnings=tuple(warnings))


__all__ = [
    "V11_TABLES",
    "CoverageLagsView",
    "ListingLifecycleView",
    "SourceLagView",
    "V11TableState",
    "coverage_lags_view",
    "listing_lifecycle_view",
    "v11_table_states",
]
