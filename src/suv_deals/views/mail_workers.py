"""Mailbox-worker health and coverage-gap read models (spec 37.6, 37.9) for the dashboard API.

Mirrors of ``persistence.mail_workers_repo.MailboxHealth`` and
``persistence.queries.inquiries.MailWorkerHealthView`` (which import ``api.schemas`` and so cannot
be imported by it). The API converts with ``model_validate(result.model_dump())``; a contract test
keeps the field sets identical.

Health dimensions are separate (heartbeat, Outlook, reconciliation, backlog, account): monitoring
is reported only while all of them are fresh, and coverage gaps are never hidden. Store and folder
ids appear only as hashes; no address, subject or body is part of these views.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field

from suv_deals.domain.enums import EmailProviderKind
from suv_deals.views.common import UtcDatetime, ViewModel
from suv_deals.views.lifecycle import LagView

ComponentStatus = Literal["healthy", "stale", "down", "unknown"]
AccountStatus = Literal["unknown", "verified", "mismatch", "not_classic"]

MAX_MAILBOXES = 50


class MailCoverageGap(ViewModel):
    """A monitored coverage gap of a mailbox worker (reported by it or detected by the server)."""

    kind: str = Field(max_length=64)
    started_at: UtcDatetime
    ended_at: UtcDatetime | None
    open: bool
    detected_by: Literal["worker", "server"]


class FolderCheckpointView(ViewModel):
    """One hashed store/folder checkpoint as reported by the worker."""

    store_id_hash: str = Field(max_length=128)
    folder_id_hash: str = Field(max_length=128)
    folder_role: str = Field(max_length=64)
    last_complete_scan_at: UtcDatetime | None
    last_scan_started_at: UtcDatetime | None
    overlap_watermark: UtcDatetime | None
    heartbeat_at: UtcDatetime | None
    backlog_count: int | None = Field(ge=0)
    backlog_oldest_at: UtcDatetime | None
    gap_reasons: tuple[str, ...] = Field(max_length=50)


class MailboxHealthView(ViewModel):
    """Separate health dimensions of one mailbox worker."""

    mailbox_binding_id: UUID
    sender_binding_id: UUID
    provider: EmailProviderKind
    worker_label: str = Field(max_length=120)
    binding_state: Literal["active", "revoked"]
    generated_at: UtcDatetime
    last_heartbeat_at: UtcDatetime | None
    heartbeat_age_seconds: int | None = Field(ge=0)
    heartbeat_status: ComponentStatus
    outlook_status: ComponentStatus
    mailbox_sync_ok: bool | None
    mailbox_sync_lag: LagView
    last_successful_reconciliation_at: UtcDatetime | None
    reconciliation_status: ComponentStatus
    backlog_count: int | None = Field(ge=0)
    backlog_age: LagView
    unresolved_matching_gaps: int | None = Field(ge=0)
    account_status: AccountStatus
    coverage_gaps: tuple[MailCoverageGap, ...] = Field(max_length=100)
    open_gap_count: int = Field(ge=0)
    folders: tuple[FolderCheckpointView, ...] = Field(max_length=100)
    monitoring_active: bool
    reasons: tuple[str, ...] = Field(max_length=50)


class MailWorkerHealthView(ViewModel):
    """``GET /api/mail-workers/health``: every mailbox worker's separate health dimensions."""

    generated_at: UtcDatetime
    mailboxes: tuple[MailboxHealthView, ...] = Field(max_length=MAX_MAILBOXES)
    any_monitoring_active: bool
    open_gap_count: int = Field(ge=0)
    notes: tuple[str, ...] = Field(max_length=20)


class MailCoverageGapItem(ViewModel):
    """One coverage gap with the mailbox it belongs to."""

    mailbox_binding_id: UUID
    worker_label: str = Field(max_length=120)
    binding_state: Literal["active", "revoked"]
    gap: MailCoverageGap


class MailCoverageGapListView(ViewModel):
    """``GET /api/mail-workers/coverage-gaps``: open gaps first, then the newest closed ones."""

    generated_at: UtcDatetime
    open_gap_count: int = Field(ge=0)
    items: tuple[MailCoverageGapItem, ...] = Field(max_length=MAX_MAILBOXES * 100)

    @classmethod
    def of(cls, health: MailWorkerHealthView) -> MailCoverageGapListView:
        items = [
            MailCoverageGapItem(
                mailbox_binding_id=box.mailbox_binding_id,
                worker_label=box.worker_label,
                binding_state=box.binding_state,
                gap=gap,
            )
            for box in health.mailboxes
            for gap in box.coverage_gaps
        ]
        items.sort(key=lambda i: (not i.gap.open, -i.gap.started_at.timestamp(), str(i.mailbox_binding_id)))
        return cls(generated_at=health.generated_at, open_gap_count=health.open_gap_count, items=tuple(items))


__all__ = [
    "AccountStatus",
    "ComponentStatus",
    "FolderCheckpointView",
    "MailCoverageGap",
    "MailCoverageGapItem",
    "MailCoverageGapListView",
    "MailWorkerHealthView",
    "MailboxHealthView",
]
