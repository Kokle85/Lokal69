"""Listing-level action results: private notes and recheck requests (spec 21)."""

from __future__ import annotations

from typing import Final, Literal
from uuid import UUID

from pydantic import Field, model_validator

from suv_deals.domain.enums import JobState
from suv_deals.views.common import UtcDatetime, ViewModel

NOTE_NOTICE: Final = "Private annotation by a workspace member or assistant; not an extracted listing claim."
RECHECK_NOTICE: Final = (
    "Queued, budget-controlled recheck of the registered listing; no arbitrary URL is fetched."
)

NoteLabel = Literal["owner", "reviewer", "assistant"]
AuthorKind = Literal["user", "mcp_client", "system"]


class NoteView(ViewModel):
    """One ``app.owner_notes`` row. Labelled by author, kept separate from seller claims."""

    note_id: UUID
    listing_id: UUID
    case_id: UUID | None
    label: NoteLabel
    author_kind: AuthorKind
    author_principal_id: UUID
    body: str = Field(min_length=1, max_length=4000)
    created_at: UtcDatetime
    updated_at: UtcDatetime
    row_version: int = Field(ge=1)
    notice: str = NOTE_NOTICE

    @model_validator(mode="after")
    def _times(self) -> NoteView:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.author_kind == "mcp_client" and self.label != "assistant":
            raise ValueError("notes written through MCP are labelled assistant")
        return self


class RecheckRequestResult(ViewModel):
    """Result of ``deals_request_recheck``: the queued (or deduplicated) job."""

    job_id: UUID
    listing_id: UUID
    job_type: Literal["recheck"] = "recheck"
    state: JobState
    deduplicated: bool
    available_at: UtcDatetime | None
    notice: str = RECHECK_NOTICE

    @model_validator(mode="after")
    def _open_state(self) -> RecheckRequestResult:
        if not self.deduplicated and self.state not in (
            JobState.QUEUED,
            JobState.RETRY_WAIT,
            JobState.BLOCKED,
        ):
            raise ValueError("a newly requested recheck is queued (or blocked with a typed blocker)")
        return self
