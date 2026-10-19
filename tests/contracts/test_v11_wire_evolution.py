"""Contract: the B2b wire evolution and the dashboard view mirrors (no database).

- (a) ``not_now`` is a claim refusal on both sides, retryable, and never a local refusal;
- (c) intents carry the listing-only ``expired`` flag, which is not part of the intent identity;
- (e) a coverage gap never ends before it starts (backend ``422``; the worker clamps);
- (f) ``OutlookSendIntent`` carries the desktop wire limits and the ``inquiry_ref`` check, and
  ``OutlookAccountReport.security_settings_unchanged`` is ``Literal[True]``;
- the dashboard view mirrors (``views.lifecycle``, ``views.mail_workers``) have exactly the fields
  of the read-service models they mirror, so ``model_validate(result.model_dump())`` never drops
  or invents a field.

(b) ``returned_message_ids`` and (d) observed Message-IDs are covered end to end in
``tests/api/test_mail_worker_routes.py``.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

from suv_deals.api.schemas import MailWorkerAccountReport, MailWorkerGapReport, MailWorkerSendIntent
from suv_deals.domain.lifecycle import LagMeasurement
from suv_deals.integrations.email_providers.outlook_local import (
    MAX_WIRE_ADDRESS_CHARS,
    OutlookAccountReport,
    OutlookRefusalReason,
    OutlookSendIntent,
)
from suv_deals.persistence import mail_workers_repo
from suv_deals.persistence.queries import inquiries as inquiry_queries
from suv_deals.persistence.queries import lifecycle as lifecycle_queries
from suv_deals.views import lifecycle as lifecycle_views
from suv_deals.views import mail_workers as mail_views

DESKTOP = Path(__file__).resolve().parents[2] / "desktop" / "outlook-bridge"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

from outlook_bridge import wire  # noqa: E402
from outlook_bridge.testing import make_intent  # noqa: E402

MAILBOX = UUID("77777777-7777-4777-8777-777777777777")
INQUIRY = UUID("66666666-6666-4666-8666-666666666666")
NOW = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)


def _intent_data() -> dict[str, Any]:
    intent = make_intent(
        inquiry_id=INQUIRY,
        mailbox_binding_id=MAILBOX,
        from_address="owner@example.invalid",
        created_at=NOW - timedelta(minutes=5),
    )
    return intent.model_dump(mode="json", exclude={"expired"})


def test_not_now_is_a_retryable_claim_refusal_on_both_sides() -> None:
    assert OutlookRefusalReason.NOT_NOW.value == wire.RefusalReason.NOT_NOW.value == "not_now"
    assert {r.value for r in OutlookRefusalReason} == {r.value for r in wire.RefusalReason}
    assert OutlookRefusalReason.KILL_SWITCH.value == "kill_switch"  # the kill switch stays distinct


def test_expired_flag_is_listing_state_not_intent_identity() -> None:
    data = _intent_data()
    listed = MailWorkerSendIntent.model_validate({**data, "expired": True})
    assert listed.expired is True
    desktop = wire.WorkerSendIntent.model_validate({**data, "expired": True})
    fresh = wire.WorkerSendIntent.model_validate(data)
    assert desktop.is_expired(NOW) and not fresh.is_expired(NOW)
    assert desktop.same_intent(fresh)
    plain = OutlookSendIntent.model_validate(data)
    assert not plain.expired(NOW)  # the provider model keeps its validity check by time


def test_gap_reports_never_end_before_they_start() -> None:
    later = NOW + timedelta(hours=1)
    with pytest.raises(ValidationError):
        MailWorkerGapReport(kind="worker_offline", started_at=later, ended_at=NOW)
    with pytest.raises(ValidationError):
        wire.GapReport(kind="worker_offline", started_at=later, ended_at=NOW)
    clamped = wire.GapReport.of("worker_offline", later, NOW)  # a local clock step
    assert clamped.ended_at == later
    assert MailWorkerGapReport(kind="worker_offline", started_at=NOW, ended_at=None).ended_at is None


def test_send_intent_carries_the_wire_limits_and_the_inquiry_ref_check() -> None:
    data = _intent_data()
    with pytest.raises(ValidationError):
        OutlookSendIntent.model_validate({**data, "inquiry_ref": "inquiry-" + str(UUID(int=7))})
    long_local = "x" * (MAX_WIRE_ADDRESS_CHARS + 1)
    with pytest.raises(ValidationError):
        OutlookSendIntent.model_validate({**data, "to_address": f"{long_local}@synthetic-dealer.example"})
    assert OutlookSendIntent.model_validate(data).inquiry_ref == f"inquiry-{INQUIRY}"


def test_account_reports_cannot_claim_weakened_security() -> None:
    report = {
        "mailbox_binding_id": str(MAILBOX),
        "worker_id": "desktop-1",
        "reported_at": NOW.isoformat(),
        "outlook_flavour": "classic",
        "stable_account_key": "synthetic-account-key-0001",
        "account_smtp_address": "owner@example.invalid",
    }
    for model in (OutlookAccountReport, MailWorkerAccountReport, wire.WorkerAccountReport):
        assert model.model_validate(report).security_settings_unchanged is True
        with pytest.raises(ValidationError):
            model.model_validate({**report, "security_settings_unchanged": False})


MIRRORS: list[tuple[type[BaseModel], type[BaseModel]]] = [
    (lifecycle_views.CoverageLagsView, lifecycle_queries.CoverageLagsView),
    (lifecycle_views.SourceLagView, lifecycle_queries.SourceLagView),
    (lifecycle_views.ListingLifecycleView, lifecycle_queries.ListingLifecycleView),
    (lifecycle_views.V11TableState, lifecycle_queries.V11TableState),
    (mail_views.MailWorkerHealthView, inquiry_queries.MailWorkerHealthView),
    (mail_views.MailboxHealthView, mail_workers_repo.MailboxHealth),
    (mail_views.FolderCheckpointView, mail_workers_repo.FolderCheckpointHealth),
    (mail_views.MailCoverageGap, mail_workers_repo.ReportedGap),
    (lifecycle_views.LagView, LagMeasurement),
]


@pytest.mark.parametrize(("view", "source"), MIRRORS, ids=lambda m: m.__name__)
def test_dashboard_views_mirror_the_read_models_exactly(
    view: type[BaseModel], source: type[BaseModel]
) -> None:
    assert set(view.model_fields) == set(source.model_fields), view.__name__
