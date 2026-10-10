"""Mailbox-worker heartbeats, checkpoints and health; dashboard/MCP reads (spec 37.6, 37.8-37.10).

- heartbeats store worker health and folder checkpoints; times are clamped to the database
  clock and the last complete scan never moves backwards; coverage gaps are never hidden (a
  closed gap stays listed and audited; a silent worker is a server-detected gap);
- the account report is checked against the bound mailbox and audited without the address;
- ``seller_inquiries_get`` / ``seller_replies_get`` and the frozen dashboard lists show only the
  caller's workspace, never the owner's mailbox, and addresses only to ``config:admin``;
- the 15-day evaluation reports zero suitable deals as zero.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from tests.integration.db.helpers import Seed, backend
from tests.integration.v11_db.support import SENDER_ADDRESS, InquiryWorld, update_inquiry
from tests.integration.v11_replies.support import (
    CURSOR_SECRET,
    MailWorld,
    another,
    ingest,
    issue,
    owner,
    request,
    reviewer,
    rows,
    sent,
)

from suv_deals.api.schemas import (
    InquiryListQuery,
    MailWorkerAccountReport,
    MailWorkerHeartbeatRequest,
    ReplyListQuery,
)
from suv_deals.domain.actor import ROLE_SCOPES, ActorContext
from suv_deals.domain.enums import InquiryState, Role, ValuationState
from suv_deals.domain.evaluation import EvaluationOutcome, SourceActivation
from suv_deals.domain.money import Money
from suv_deals.errors import Forbidden, NotFound, VersionConflict
from suv_deals.persistence import mail_workers_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.queries import inquiries as queries
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.views.common import WarningCode

pytestmark = pytest.mark.db

STORE = "a" * 64
INBOX = "b" * 64


def viewer(workspace_id: uuid.UUID) -> ActorContext:
    return ActorContext(
        workspace_id=workspace_id,
        principal_id=uuid.uuid4(),
        principal_kind="user",
        role=Role.VIEWER,
        scopes=ROLE_SCOPES[Role.VIEWER],
        request_id="req-viewer",
    )


def beat(mw: MailWorld, **fields: object) -> MailWorkerHeartbeatRequest:
    now = datetime.now(UTC)
    document: dict[str, object] = {
        "schema_version": "1.0",
        "heartbeat": {
            "mailbox_binding_id": str(mw.mailbox_id),
            "worker_id": "synthetic-worker-1",
            "at": now.isoformat(),
            "outlook_running": True,
            "mailbox_connected": True,
        },
        "last_successful_reconciliation_at": now.isoformat(),
        "mailbox_last_sync_at": now.isoformat(),
    }
    document.update(fields)
    return MailWorkerHeartbeatRequest.model_validate(document)


async def heartbeat(db: Database, mw: MailWorld, body: MailWorkerHeartbeatRequest) -> None:
    async with unit_of_work(db, mw.worker.actor("req-beat")) as conn:
        ack = await mail_workers_repo.record_heartbeat(conn, mw.worker, body, request_id="req-beat")
    assert ack.schema_version == "1.0" and ack.received_at is not None
    assert ack.downstream["backend"] == "ok"


async def health(db: Database, mw: MailWorld) -> mail_workers_repo.MailboxHealth:
    async with unit_of_work(db, mw.worker.actor("req-health")) as conn:
        return await mail_workers_repo.mailbox_health(conn, mw.worker)


# ---------------------------------------------------------------------------------------------
# Heartbeats, checkpoints, gaps, account report
# ---------------------------------------------------------------------------------------------


async def test_a_silent_worker_is_never_reported_as_monitoring(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    mw = await issue(db, iw)
    fresh = await health(db, mw)
    assert fresh.last_heartbeat_at is None and not fresh.monitoring_active
    # TEST ARRANGEMENT ONLY: the worker was bound ten minutes ago and has been silent since.
    with seed.conn.transaction():
        seed.conn.execute("set local session_replication_role = replica")
        seed.conn.execute(
            "update ops.mail_worker_bindings set created_at = now() - interval '10 minutes' where id = %s",
            (mw.mailbox_id,),
        )
    report = await health(db, mw)
    assert report.last_heartbeat_at is None and not report.monitoring_active
    assert report.heartbeat_status in ("down", "unknown")
    assert any(g.detected_by == "server" and g.open for g in report.coverage_gaps)


async def test_heartbeat_checkpoints_and_gaps(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    mw = await issue(db, iw)
    now = datetime.now(UTC)
    gap_start = now - timedelta(minutes=30)
    await heartbeat(
        db,
        mw,
        beat(
            mw,
            backlog_count=2,
            backlog_oldest_age_seconds=120,
            unresolved_matching_gaps=1,
            checkpoints=[
                {
                    "store_id_hash": STORE,
                    "folder_id_hash": INBOX,
                    "folder_role": "inbox",
                    "last_complete_scan_at": now.isoformat(),
                    "acknowledged_watermark": now.isoformat(),
                    "backlog_count": 2,
                    "backlog_oldest_at": (now - timedelta(minutes=2)).isoformat(),
                    "gap_reasons": ["synthetic folder note"],
                }
            ],
            gaps=[{"kind": "outlook_closed", "started_at": gap_start.isoformat()}],
        ),
    )
    report = await health(db, mw)
    assert report.heartbeat_status == "healthy" and report.outlook_status == "healthy"
    assert report.backlog_count == 2 and report.unresolved_matching_gaps == 1
    (open_gap,) = [g for g in report.coverage_gaps if g.kind == "outlook_closed"]
    assert open_gap.open and open_gap.detected_by == "worker"
    assert not report.monitoring_active  # an open gap is never hidden behind "active"
    (folder,) = report.folders
    assert folder.folder_role == "inbox" and folder.backlog_count == 2
    # The gap closes: still listed (closed), audited twice (opened, closed).
    await heartbeat(
        db,
        mw,
        beat(
            mw,
            gaps=[
                {"kind": "outlook_closed", "started_at": gap_start.isoformat(), "ended_at": now.isoformat()}
            ],
        ),
    )
    report = await health(db, mw)
    (closed,) = [g for g in report.coverage_gaps if g.kind == "outlook_closed"]
    assert not closed.open and closed.ended_at is not None
    assert report.unresolved_matching_gaps == 0 and report.backlog_count == 0
    audits = rows(
        seed,
        "select metadata->>'ended_at' as ended from ops.audit_events"
        " where action = 'mail_worker.coverage_gap' and workspace_id = %s order by occurred_at, id",
        iw.workspace_id,
    )
    assert [a["ended"] is None for a in audits] == [True, False]
    # Re-reporting the same closed gap is not audited again.
    await heartbeat(
        db,
        mw,
        beat(
            mw,
            gaps=[
                {"kind": "outlook_closed", "started_at": gap_start.isoformat(), "ended_at": now.isoformat()}
            ],
        ),
    )
    assert (
        len(
            rows(
                seed,
                "select id from ops.audit_events where action = 'mail_worker.coverage_gap'"
                " and workspace_id = %s",
                iw.workspace_id,
            )
        )
        == 2
    )


async def test_reported_times_are_clamped_and_scans_never_move_backwards(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    mw = await issue(db, iw)
    future = datetime.now(UTC) + timedelta(hours=6)
    await heartbeat(db, mw, beat(mw, last_successful_reconciliation_at=future.isoformat()))
    stored = seed.scalar(
        "select last_complete_scan_at from ops.mail_worker_checkpoints where mailbox_binding_id = %s"
        " and folder_id_hash = %s",
        (mw.mailbox_id, mail_workers_repo.HEALTH_ROW_HASH),
    )
    assert stored <= datetime.now(UTC)
    older = stored - timedelta(hours=1)
    await heartbeat(db, mw, beat(mw, last_successful_reconciliation_at=older.isoformat()))
    again = seed.scalar(
        "select last_complete_scan_at from ops.mail_worker_checkpoints where mailbox_binding_id = %s"
        " and folder_id_hash = %s",
        (mw.mailbox_id, mail_workers_repo.HEALTH_ROW_HASH),
    )
    assert again == stored


async def test_a_heartbeat_for_another_mailbox_is_refused(
    db: Database, iw: InquiryWorld, iw_b: InquiryWorld
) -> None:
    mw = await issue(db, iw)
    other = await issue(db, iw_b)
    body = beat(other)
    async with unit_of_work(db, mw.worker.actor("req")) as conn:
        with pytest.raises(Forbidden) as refused:
            await mail_workers_repo.record_heartbeat(conn, mw.worker, body, request_id="req")
    assert refused.value.details == {"reason": "mailbox_binding_mismatch"}


def account(mw: MailWorld, address: str, flavour: str = "classic") -> MailWorkerAccountReport:
    return MailWorkerAccountReport.model_validate(
        {
            "mailbox_binding_id": str(mw.mailbox_id),
            "worker_id": "synthetic-worker-1",
            "reported_at": datetime.now(UTC).isoformat(),
            "outlook_flavour": flavour,
            "outlook_version": "16.0.synthetic",
            "stable_account_key": "synthetic-account-key-1",
            "account_smtp_address": address,
            "account_type": "exchange",
        }
    )


async def test_the_account_report_is_checked_and_audited_without_the_address(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    mw = await issue(db, iw)
    async with unit_of_work(db, mw.worker.actor("req")) as conn:
        ok = await mail_workers_repo.record_account_report(
            conn,
            mw.worker,
            account(mw, SENDER_ADDRESS.replace("synthetic-mail.example", "SYNTHETIC-MAIL.EXAMPLE")),
            request_id="req",
        )
    assert ok.accepted and ok.status == "verified"
    async with unit_of_work(db, mw.worker.actor("req")) as conn:
        bad = await mail_workers_repo.record_account_report(
            conn, mw.worker, account(mw, "someone@other-mail.example", "new"), request_id="req"
        )
    assert not bad.accepted and bad.problems == ("ACCOUNT_MISMATCH", "OUTLOOK_NOT_CLASSIC")
    with pytest.raises(VersionConflict):
        bad.raise_for_problems()
    report = await health(db, mw)
    assert report.account_status == "mismatch" and not report.monitoring_active
    texts = [
        r["metadata"]
        for r in rows(
            seed,
            "select metadata::text as metadata from ops.audit_events"
            " where action = 'mail_worker.account_report' and workspace_id = %s",
            iw.workspace_id,
        )
    ]
    assert len(texts) == 2 and all("@" not in t for t in texts)


async def test_health_view_scopes_and_warnings(db: Database, iw: InquiryWorld) -> None:
    mw = await issue(db, iw)
    async with unit_of_work(db, viewer(iw.workspace_id)) as conn:
        with pytest.raises(Forbidden):
            await queries.mail_worker_health_view(conn, viewer(iw.workspace_id))
    async with unit_of_work(db, reviewer(iw.workspace_id)) as conn:
        result = await queries.mail_worker_health_view(conn, reviewer(iw.workspace_id))
    assert [m.mailbox_binding_id for m in result.data.mailboxes] == [mw.mailbox_id]
    assert not result.data.any_monitoring_active
    assert WarningCode.COVERAGE_GAP in {w.code for w in result.warnings}
    assert SENDER_ADDRESS not in result.data.model_dump_json()


# ---------------------------------------------------------------------------------------------
# Inquiry and reply reads
# ---------------------------------------------------------------------------------------------


async def test_inquiry_and_reply_reads(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    seed.valuation(iw.workspace_id, iw.listing_id, iw.vehicle.revision_id, state="incomplete")
    outcome = await ingest(
        db, mw, request(mw, inquiry, body="Leider schon verkauft. Letzter Preis 2.450 Euro.")
    )
    async with unit_of_work(db, owner(iw.workspace_id)) as conn:
        as_owner = await queries.get_inquiry(conn, owner(iw.workspace_id), inquiry)
        reply = await queries.get_reply(conn, owner(iw.workspace_id), outcome.reply_id)
    async with unit_of_work(db, reviewer(iw.workspace_id)) as conn:
        as_reviewer = await queries.get_inquiry(conn, reviewer(iw.workspace_id), inquiry)
        reply_for_reviewer = await queries.get_reply(conn, reviewer(iw.workspace_id), outcome.reply_id)
    view = as_owner.data
    assert view.state == InquiryState.REPLIED and view.reply_count == 1
    assert view.latest_reply_id == outcome.reply_id and view.send_attempts.count == 1
    assert view.recipient.address == iw.contact_address
    assert as_reviewer.data.recipient.address is None
    assert as_reviewer.data.recipient.address_domain == iw.contact_address.split("@")[1]
    assert as_reviewer.data.recipient.address_redacted
    # The shared synthetic world has REAL lineage (C1: fixture lineage never reserves or sends).
    assert WarningCode.FIXTURE_DATA not in {w.code for w in as_owner.warnings}
    data = reply.data
    assert data.inquiry_id == inquiry and data.sender.matches_verified_recipient
    assert data.claims is not None and data.claims.price_quotes and not data.claims.price_quotes[0].accepted
    assert data.valuation.state == ValuationState.STALE and data.valuation.recalculation_pending
    codes = {w.code for w in reply.warnings}
    assert {WarningCode.SELLER_CLAIMS_UNVERIFIED, WarningCode.VALUATION_STALE} <= codes
    assert reply_for_reviewer.data.sender.address is None
    for dumped in (view.model_dump_json(), data.model_dump_json(), reply_for_reviewer.data.model_dump_json()):
        assert SENDER_ADDRESS not in dumped  # the owner's mailbox never appears
    assert iw.contact_address not in reply_for_reviewer.data.model_dump_json()


async def test_reads_are_workspace_isolated(
    db: Database, seed: Seed, iw: InquiryWorld, iw_b: InquiryWorld
) -> None:
    inquiry = sent(seed.conn, iw)
    mw = await issue(db, iw)
    outcome = await ingest(db, mw, request(mw, inquiry))
    other = owner(iw_b.workspace_id)
    async with unit_of_work(db, other) as conn:
        with pytest.raises(NotFound):
            await queries.get_inquiry(conn, other, inquiry)
        with pytest.raises(NotFound):
            await queries.get_reply(conn, other, outcome.reply_id)
        page = await queries.list_replies(conn, other, ReplyListQuery(), secret=CURSOR_SECRET)
        inquiries_page = await queries.list_inquiries(conn, other, InquiryListQuery(), secret=CURSOR_SECRET)
    assert page.data.items == () and inquiries_page.data.items == ()
    async with unit_of_work(db, viewer(iw.workspace_id)) as conn:
        with pytest.raises(Forbidden):
            await queries.get_inquiry(conn, viewer(iw.workspace_id), inquiry)


async def test_lists_are_frozen_snapshots_without_message_text(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    first = sent(seed.conn, iw)
    second = sent(seed.conn, another(iw))
    mw = await issue(db, iw)
    reply = await ingest(db, mw, request(mw, first, body="Leider schon verkauft."))
    actor = owner(iw.workspace_id)
    async with unit_of_work(db, actor) as conn:
        page1 = await queries.list_inquiries(conn, actor, InquiryListQuery(limit=1), secret=CURSOR_SECRET)
    assert len(page1.data.items) == 1 and page1.next_cursor is not None
    assert WarningCode.FROZEN_QUEUE_PROJECTION in {w.code for w in page1.warnings}
    # A state change between pages neither shuffles nor duplicates the frozen membership.
    with backend(seed.conn, iw.workspace_id):
        update_inquiry(seed.conn, second, state="no_reply_yet")
    async with unit_of_work(db, actor) as conn:
        page2 = await queries.list_inquiries(
            conn, actor, InquiryListQuery(limit=1, cursor=page1.next_cursor), secret=CURSOR_SECRET
        )
    ids = [i.inquiry_id for i in (*page1.data.items, *page2.data.items)]
    assert sorted(ids, key=str) == sorted([first, second], key=str) and page2.next_cursor is None
    async with unit_of_work(db, actor) as conn:
        replied = await queries.list_inquiries(
            conn, actor, InquiryListQuery(state=InquiryState.REPLIED), secret=CURSOR_SECRET
        )
        attention = await queries.list_inquiries(
            conn, actor, InquiryListQuery(), secret=CURSOR_SECRET, attention_only=True
        )
        replies = await queries.list_replies(
            conn, actor, ReplyListQuery(inquiry_id=first), secret=CURSOR_SECRET
        )
        quarantined = await queries.list_replies(
            conn, actor, ReplyListQuery(quarantined_only=True), secret=CURSOR_SECRET
        )
    assert [i.inquiry_id for i in replied.data.items] == [first]
    assert attention.data.items == ()
    assert [r.reply_id for r in replies.data.items] == [reply.reply_id]
    assert replies.data.items[0].availability == "sold"
    assert quarantined.data.items == ()
    for page in (page1, page2, replied):
        dumped = page.data.model_dump_json()
        assert "@" not in dumped
    assert "verkauft" not in replies.data.model_dump_json()


# ---------------------------------------------------------------------------------------------
# 15-day evaluation
# ---------------------------------------------------------------------------------------------


async def test_a_fifteen_day_evaluation_with_zero_deals_reports_zero(db: Database, seed: Seed) -> None:
    ws = seed.workspace("B1b evaluation")
    seed.profile(ws, "primary")
    source = seed.source(ws, mode="public_html", enabled=False, source_key="eval_live_src")
    start = datetime.now(UTC) - timedelta(days=16)
    for day in range(17):
        at = start + timedelta(days=day)
        seed.insert_id(
            "ops.crawl_runs",
            workspace_id=ws,
            source_id=source,
            adapter_version="synthetic@1.0.0",
            coverage_mode="rolling_pages",
            started_at=at,
            finished_at=at + timedelta(minutes=5),
            outcome="complete",
        )
    listing = seed.listing(
        ws,
        source,
        availability="available",
        eligibility_state="eligible_primary",
        eligibility_profile="primary",
        screening={"synthetic": True},
        screening_version="screening@synthetic",
        screened_at=start + timedelta(days=1),
        first_seen_at=start + timedelta(days=1),
    )
    activation = SourceActivation(source_key="eval_live_src", activated_at=start, is_fixture=False)
    actor = owner(ws)
    async with unit_of_work(db, actor) as conn:
        result = await queries.evaluation_report(conn, actor, activations=[activation])
        threshold = await queries.evaluation_report(
            conn, actor, activations=[activation], approved_threshold=Money.of("1500", "EUR")
        )
    report = result.data
    assert report.window_status == "complete"
    assert report.qualifying_deal_ids == () and report.owner_judgement_candidate_ids == ()
    assert report.eligible_vehicles == 1 and report.well_matched_vehicles == 0
    assert report.best_supported_economics is None
    assert report.inquiries.attempted == 0 and report.seller_replies == 0
    # The window opened and closed (complete), so the honest outcome is exactly "no suitable deal".
    assert report.outcome == EvaluationOutcome.NO_SUITABLE_DEAL
    assert threshold.data.qualifying_deal_ids == ()
    lines = report.summary_lines()
    assert any("Seller replies: 0" in line for line in lines)
    assert WarningCode.THRESHOLD_PROPOSED in {w.code for w in result.warnings}
    assert WarningCode.THRESHOLD_PROPOSED not in {w.code for w in threshold.warnings}
    assert listing is not None
    # Without explicit activations an enabled live source would count; this one is disabled.
    async with unit_of_work(db, actor) as conn:
        default = await queries.evaluation_report(conn, actor)
    assert default.data.window_status == "not_started" and default.data.qualifying_deal_ids == ()
    assert WarningCode.COVERAGE_GAP in {w.code for w in default.warnings}
    json.dumps(report.model_dump(mode="json"))  # serialisable for the dashboard


# ---------------------------------------------------------------------------------------------
# Review regressions (independent review of B1b)
# ---------------------------------------------------------------------------------------------


def health_codes(seed: Seed, mw: MailWorld) -> list[str]:
    return list(
        seed.scalar(
            "select gap_reasons from ops.mail_worker_checkpoints where mailbox_binding_id = %s"
            " and store_id_hash = %s and folder_id_hash = %s",
            (mw.mailbox_id, mail_workers_repo.HEALTH_ROW_HASH, mail_workers_repo.HEALTH_ROW_HASH),
        )
    )


async def test_a_healthy_worker_is_reported_as_monitoring(db: Database, iw: InquiryWorld) -> None:
    """The positive case: a fresh heartbeat, a connected Outlook and a fresh reconciliation with
    no gap is ``monitoring_active`` (the other tests only ever showed it false)."""
    mw = await issue(db, iw)
    await heartbeat(db, mw, beat(mw))
    report = await health(db, mw)
    assert report.monitoring_active and report.open_gap_count == 0 and report.reasons == ()
    assert report.heartbeat_status == "healthy" and report.reconciliation_status == "healthy"


@pytest.mark.parametrize("dead", ["expired", "revoked"])
async def test_a_dead_worker_credential_ends_monitoring_while_the_heartbeat_is_fresh(
    db: Database, seed: Seed, iw: InquiryWorld, dead: str
) -> None:
    """D1 item 2 (spec 37.10: never imply monitoring that is not happening): the worker's last
    heartbeat is still fresh, but its credential has expired (or was revoked underneath the still
    active binding): it can no longer upload a reply, so the mailbox is NOT monitoring, in the
    repository health, the dashboard/MCP health view and everything derived from them (``doctor``,
    ``inquiries status``, ``mail-worker list``)."""
    mw = await issue(db, iw)
    await heartbeat(db, mw, beat(mw))
    assert (await health(db, mw)).monitoring_active
    # TEST ARRANGEMENT ONLY: the bound credential dies right after the (still fresh) heartbeat.
    with seed.conn.transaction():
        seed.conn.execute("set local session_replication_role = replica")
        if dead == "expired":
            seed.conn.execute(
                "update ops.api_credentials set created_at = now() - interval '2 days',"
                " expires_at = now() - interval '1 second' where id = %s",
                (mw.worker.credential_id,),
            )
        else:
            seed.conn.execute(
                "update ops.api_credentials set revoked_at = now(), revoked_by = created_by,"
                " revoke_reason = 'synthetic revocation' where id = %s",
                (mw.worker.credential_id,),
            )
    report = await health(db, mw)
    assert report.binding_state == "active" and report.heartbeat_status == "healthy"
    assert not report.monitoring_active
    assert mail_workers_repo.CREDENTIAL_NOT_LIVE in report.reasons
    async with unit_of_work(db, reviewer(iw.workspace_id)) as conn:
        view = await queries.mail_worker_health_view(conn, reviewer(iw.workspace_id))
    (box,) = view.data.mailboxes
    assert not box.monitoring_active and not view.data.any_monitoring_active
    assert WarningCode.COVERAGE_GAP in {w.code for w in view.warnings}


async def test_an_open_gap_is_never_evicted_by_newer_closed_gaps(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    """REGRESSION: the bounded gap window kept the newest gaps by start time, so an older gap
    that is still OPEN fell out behind newer closed ones: health reported ``monitoring_active``
    while the worker said coverage was missing, and every heartbeat re-audited the gap as new."""
    mw = await issue(db, iw)
    now = datetime.now(UTC)
    still_open = {"kind": "retention_incomplete", "started_at": (now - timedelta(days=2)).isoformat()}
    closed = [
        {
            "kind": "outlook_closed",
            "started_at": (now - timedelta(hours=40 - i)).isoformat(),
            "ended_at": (now - timedelta(hours=40 - i) + timedelta(minutes=5)).isoformat(),
        }
        for i in range(35)
    ]
    for _ in range(3):  # the desktop worker re-sends its open gaps with every heartbeat
        await heartbeat(db, mw, beat(mw, gaps=[still_open, *closed]))
    report = await health(db, mw)
    assert [g.kind for g in report.coverage_gaps if g.open] == ["retention_incomplete"]
    assert not report.monitoring_active and report.open_gap_count == 1
    codes = health_codes(seed, mw)
    assert len(codes) <= 30 and sum(c.startswith("gap:") for c in codes) == 28
    audits = rows(
        seed,
        "select id from ops.audit_events where action = 'mail_worker.coverage_gap'"
        " and workspace_id = %s and metadata->>'kind' = 'retention_incomplete'",
        iw.workspace_id,
    )
    assert len(audits) == 1


async def test_odd_gap_times_never_hide_a_gap(db: Database, seed: Seed, iw: InquiryWorld) -> None:
    """REGRESSION: a gap reported with its end before its start (a start in the future is clamped
    to the server clock) or with an ancient start was stored as an unreadable code and silently
    dropped from health, which then claimed ``monitoring_active``."""
    mw = await issue(db, iw)
    now = datetime.now(UTC)
    # A gap that ends before it starts is refused at the wire (422), so it can never be stored.
    with pytest.raises(ValidationError):
        beat(
            mw,
            gaps=[
                {
                    "kind": "outlook_closed",
                    "started_at": (now + timedelta(hours=1)).isoformat(),
                    "ended_at": (now - timedelta(minutes=10)).isoformat(),
                }
            ],
        )
    # Future times (a worker clock ahead of the server) are clamped to the server clock; the
    # clamped gap still never ends before it starts.
    await heartbeat(
        db,
        mw,
        beat(
            mw,
            gaps=[
                {
                    "kind": "outlook_closed",
                    "started_at": (now + timedelta(hours=1)).isoformat(),
                    "ended_at": (now + timedelta(hours=2)).isoformat(),
                },
                {"kind": "suspend", "started_at": "0001-01-01T00:00:00+00:00"},
            ],
        ),
    )
    report = await health(db, mw)
    kinds = {(g.kind, g.open) for g in report.coverage_gaps if g.detected_by == "worker"}
    assert kinds == {("outlook_closed", False), ("suspend", True)}
    (closed,) = [g for g in report.coverage_gaps if g.kind == "outlook_closed"]
    assert closed.ended_at is not None and closed.ended_at >= closed.started_at
    (ancient,) = [g for g in report.coverage_gaps if g.kind == "suspend"]
    assert ancient.started_at >= now - mail_workers_repo.MAX_REPORTED_AGE - timedelta(minutes=1)
    assert not report.monitoring_active
    assert len(mail_workers_repo.parse_gap_codes(health_codes(seed, mw))) == 2


async def test_absurd_worker_counters_are_bounded_not_fatal(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    """REGRESSION: schema-valid but absurd numbers crashed the heartbeat (an ``OverflowError``
    for a huge backlog age, a CHECK violation for a huge matching-gap count), so the worker's
    health and gaps were lost."""
    mw = await issue(db, iw)
    huge = 10**30
    await heartbeat(
        db,
        mw,
        beat(
            mw,
            backlog_count=huge,
            backlog_oldest_age_seconds=huge,
            unresolved_matching_gaps=10**200,
            checkpoints=[
                {
                    "store_id_hash": STORE,
                    "folder_id_hash": INBOX,
                    "folder_role": "inbox",
                    "backlog_count": huge,
                }
            ],
        ),
    )
    report = await health(db, mw)
    assert report.backlog_count == mail_workers_repo.MAX_REPORTED_COUNT
    assert report.unresolved_matching_gaps == mail_workers_repo.MAX_REPORTED_COUNT
    (folder,) = report.folders
    assert folder.backlog_count == mail_workers_repo.MAX_REPORTED_COUNT


async def test_an_account_report_racing_the_first_heartbeat(
    db: Database, seed: Seed, iw: InquiryWorld
) -> None:
    """REGRESSION: both create the worker health row. The account report inserted it without
    conflict handling, so racing the first heartbeat it failed with a spurious ``409`` (the
    status the worker reads as an account mismatch) - and a heartbeat could drop its code."""
    mw = await issue(db, iw)

    async def report_account() -> mail_workers_repo.AccountReportOutcome:
        async with unit_of_work(db, mw.worker.actor("req-account")) as conn:
            return await mail_workers_repo.record_account_report(
                conn, mw.worker, account(mw, SENDER_ADDRESS), request_id="req-account"
            )

    async with unit_of_work(db, mw.worker.actor("req-beat")) as conn:
        await mail_workers_repo.record_heartbeat(conn, mw.worker, beat(mw), request_id="req-beat")
        racing = asyncio.create_task(report_account())
        await asyncio.sleep(0.3)  # the report now waits for the heartbeat's uncommitted row
    outcome = await racing
    assert outcome.accepted and outcome.status == "verified"
    report = await health(db, mw)
    assert report.account_status == "verified" and report.last_heartbeat_at is not None
    codes = health_codes(seed, mw)
    assert "account:verified" in codes and "matching_gaps:0" in codes
