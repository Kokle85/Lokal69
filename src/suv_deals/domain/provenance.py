"""Field-level provenance and conflict records (spec section 7).

Confidence measures extraction reliability only. A perfectly parsed seller
statement is still an unverified seller claim.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from suv_deals.clock import ensure_utc
from suv_deals.domain.enums import Confidence, ExtractionMethod

MAX_RAW_EXCERPT = 500


class FieldProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    method: ExtractionMethod
    selector: str | None = Field(default=None, max_length=300)
    raw_text: str | None = Field(default=None, max_length=MAX_RAW_EXCERPT)
    source_url: str | None = Field(default=None, max_length=2048)
    snapshot_id: UUID | None = None
    transformation: str | None = Field(default=None, max_length=200)
    confidence: Confidence
    observed_at: datetime

    @field_validator("raw_text", mode="before")
    @classmethod
    def _truncate(cls, value: object) -> object:
        if isinstance(value, str) and len(value) > MAX_RAW_EXCERPT:
            return value[: MAX_RAW_EXCERPT - 1] + "…"
        return value

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class FieldConflict(BaseModel):
    """Two or more supported values disagree; neither silently wins.

    For numeric fields `values` are plain decimal strings in canonical units (e.g. km:
    ``["187500", "87500"]``; ranges as ``"low-high"``). Descriptive text belongs in `locations`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str = Field(max_length=120)
    values: list[str] = Field(min_length=2, max_length=10)
    locations: list[str] = Field(default_factory=list, max_length=10)
    resolution: str = Field(default="unresolved", max_length=200)
    note: str | None = Field(default=None, max_length=500)
