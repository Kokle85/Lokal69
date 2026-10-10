"""Lifecycle and coverage-lag read models (spec 30, 37.9) for the dashboard API.

Mirrors of the read-service models in ``persistence.queries.lifecycle`` (which cannot be imported
by ``api.schemas`` without an import cycle). The API converts the read service's result with
``model_validate(result.model_dump())``; a contract test keeps the field sets identical.

A lag is a `LagView`: ``unknown`` and ``inconsistent`` carry no value - never zero - and a
configured interval is context only, never an observed latency.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from suv_deals.domain.enums import Availability
from suv_deals.domain.lifecycle import LagName, LagStatus
from suv_deals.views.common import UtcDatetime, ViewModel
from suv_deals.views.operations import SourceRunState


class LagView(ViewModel):
    """One observed lag (``domain.lifecycle.LagMeasurement``)."""

    name: LagName
    status: LagStatus
    value_seconds: int | None = Field(
        ge=0, description="Only a measured lag has a value; never zero for unknown."
    )
    reason: str | None = Field(max_length=500)
    configured_interval_seconds: int | None = Field(
        ge=0, description="Context only; never an observed latency guarantee."
    )
    note: str = Field(max_length=500)

    @model_validator(mode="after")
    def _shape(self) -> LagView:
        if (self.status == LagStatus.MEASURED) != (self.value_seconds is not None):
            raise ValueError("only a measured lag has a value")
        return self


class V11TableState(ViewModel):
    relation: str = Field(max_length=80)
    present: bool


class SourceLagView(ViewModel):
    """Per-source scan lag (time since the last complete, finished crawl run)."""

    source_id: UUID
    source_key: str = Field(max_length=80)
    state: SourceRunState
    last_successful_scan_at: UtcDatetime | None
    last_complete_scan_at: UtcDatetime | None
    source_scan_lag: LagView


class CoverageLagsView(ViewModel):
    """``GET /api/lifecycle/lags``: separate lags per spec 37.9, unknown never shown as zero."""

    sources: tuple[SourceLagView, ...] = Field(max_length=200)
    notification_processing_lag: LagView
    mail_reply_detection_lag: LagView
    v11_tables: tuple[V11TableState, ...] = Field(max_length=50)
    notes: tuple[str, ...] = Field(max_length=50)


class ListingLifecycleView(ViewModel):
    """``GET /api/listings/{listing_id}/lifecycle``: one source listing's own lifecycle evidence."""

    listing_id: UUID
    source_id: UUID
    source_key: str = Field(max_length=80)
    source_state: SourceRunState
    first_seen_at: UtcDatetime
    last_seen_on_search_at: UtcDatetime | None
    last_detail_success_at: UtcDatetime | None
    last_availability_check_at: UtcDatetime | None
    last_complete_source_scan_at: UtcDatetime | None
    source_published_at: UtcDatetime | None
    source_published_trusted: bool
    availability: Availability
    detail_freshness: LagView
    detection_delay: LagView
    availability_history_source: Literal["audit_events", "availability_events"]
    notes: tuple[str, ...] = Field(max_length=50)


__all__ = [
    "CoverageLagsView",
    "LagView",
    "ListingLifecycleView",
    "SourceLagView",
    "V11TableState",
]
