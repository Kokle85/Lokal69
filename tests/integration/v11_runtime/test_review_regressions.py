"""Regression tests of the B2a review (spec 37.1-37.5, 37.7, 37.10 U3/U6; both PG versions).

Each test fails on the code before the review:

- a price change while the inquiry was reserved/queued (never transmitted): the plan job of the
  NEW revision cancels the stale reservation and reserves the current facts; before, it finished
  as ``inquiry_exists``, the dispatch preflight then cancelled the stale work and nothing ever
  inquired about the current revision;
- a routine sender re-verification (binding version + 1) cancels a queued inquiry at dispatch
  (``SENDER_CHANGED``): the never-transmitted cancellation is decided again by the next pass;
- a restart with another ``SELLER_INQUIRY_MODE`` re-decides a not-yet-reserved inquiry at the
  next pass (not only on the next UTC day);
- a revoked API sender is suppressed by the dispatch preflight (debit released) instead of a
  blocked send job holding a queued inquiry and its quota debit; only a setup problem the
  preflight cannot see blocks the job;
- a blocked reconcile job is not multiplied every hour;
- a send job reaped before it committed any send intent is returned to the queue (audited
  acknowledgement), while one whose intent exists stays blocked;
- an opt-out stored while the send was still ``sending`` ends as ``seller_opted_out`` (not
  ``replied``) once the send is accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from tests.integration.pipeline.support import PipelineEnv, run
from tests.integration.v11_inquiries.support import complete_activation_canary
from tests.integration.v11_runtime.support import (
    LIVE_KEY,
    WORKER_REQ,
    GmailApi,
    age_uncertainty,
    attach_gmail,
    attempts_of,
    box,
    debits_of,
    eligible_live_listing,
    gmail_settings,
    inquiries_of,
    jobs_of,
    link_seller,
    no_approval_state,
    now_utc,
    pending_intents,
    prepare_sender,
    runtime_settings,
    seller_replies,
    show_seller_email,
    with_settings,
    work,
)

from suv_deals.domain.enums import EmailProviderKind, InquiryState, JobType
from suv_deals.integrations.email_providers.outlook_local import OutlookSendReport, OutlookSubmissionState
from suv_deals.persistence import inquiries_repo, jobs, listings_repo, send_intents_repo, sender_bindings_repo
from suv_deals.persistence.database import Conn
from suv_deals.persistence.inquiries_repo import AttemptLease
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.workers.reconciliation import Reconciler

pytestmark = pytest.mark.db


def _db_url(env: PipelineEnv) -> str:
    url = env.ctx.settings.database_url
    assert url is not None
    return url.get_secret_value()


def _row(env: PipelineEnv, query: str, *params: Any) -> dict[str, Any]:
    rows = env.rows(query, *params)
    assert len(rows) == 1, rows
    return rows[0]


async def _queued(
    env: PipelineEnv, *, page_contact: bool = False, **sender_options: Any
) -> tuple[Any, dict[str, Any]]:
    """A verified sender, the eligible real-lineage listing with its seller, valuation + plan
    only: one reserved and queued inquiry whose send job waits. ``page_contact``: the dealer's
    page itself shows the e-mail address, so the detail pipeline records the verified contact
    (and every re-fetch re-verifies it) instead of the test arrangement `link_seller`."""
    sender = await prepare_sender(env, **sender_options)
    listing = await eligible_live_listing(env)
    if not page_contact:
        await link_seller(env, listing)
    await work(env, JobType.VALUATION, JobType.SELLER_INQUIRY_PLAN)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED and debits_of(env) == 1
    return sender, listing


# --------------------------------------------------------------------------------------------
# A new revision while the inquiry is queued
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Recheck:
    listing_id: UUID
    reason: str
    idempotency_key: str


async def test_price_change_while_queued_reserves_the_current_revision(
    env: PipelineEnv, tmp_path: Path
) -> None:
    # The page shows the seller's address: the recheck's re-fetch re-verifies the same contact
    # (wave D2; a contact-form page would now record the seller's e-mail as unavailable).
    show_seller_email(tmp_path)
    await _queued(env, page_contact=True)
    [listing] = env.rows(
        "select id, current_revision_id from app.listings where workspace_id = %s"
        " and source_listing_id = 'TEST-204'",
        env.workspace_id,
    )
    first_revision = listing["current_revision_id"]

    # The dealer lowers the price (SYNTHETIC page); a recheck stores a NEW revision.
    page = tmp_path / LIVE_KEY / "detail_normal.html"
    text = page.read_text(encoding="utf-8")
    page.write_text(text.replace("2750.00", "2700.00").replace("2.750", "2.700"), encoding="utf-8")
    owner = env.owner

    async def recheck(conn: Conn) -> object:
        return await listings_repo.request_recheck(
            conn, owner, _Recheck(listing["id"], "synthetic price change", "b2a-review-recheck-1")
        )

    await run(env.ctx, owner, recheck)
    reports = await work(env, JobType.RECHECK, JobType.VALUATION, JobType.SELLER_INQUIRY_PLAN)
    current = env.scalar("select current_revision_id from app.listings where id = %s", listing["id"])
    assert current != first_revision
    plans = [(r.state, r.code) for r in reports if r.job_type == JobType.SELLER_INQUIRY_PLAN]
    # The new revision's plan cancels the stale (never transmitted) reservation, re-runs at once
    # without consuming an attempt, and reserves the current facts.
    assert [code for _state, code in plans] == ["INQUIRY_STALE_CANCELLED", "reserved"], plans
    inquiry = _row(
        env,
        "select id, state, qualification_revision_id, qualified_price_minor from app.seller_inquiries"
        " where workspace_id = %s",
        env.workspace_id,
    )
    assert inquiry["state"] == InquiryState.QUEUED
    assert inquiry["qualification_revision_id"] == current and inquiry["qualified_price_minor"] == 270000
    assert debits_of(env) == 1  # the stale debit was released, the new reservation took one

    # Exactly one intent for the CURRENT reservation; the stale send job publishes nothing more.
    reports = await work(env, JobType.SELLER_INQUIRY_SEND)
    assert sorted(r.code or "" for r in reports) == ["intent_published", "not_queued"]
    [attempt] = attempts_of(env, inquiry["id"])
    assert attempt["outcome"] == "running"
    no_approval_state(env)


# --------------------------------------------------------------------------------------------
# Never-transmitted cancellations and process switches are decided again
# --------------------------------------------------------------------------------------------


async def test_sender_reverification_cancellation_is_decided_again(env: PipelineEnv) -> None:
    sender, _listing = await _queued(env)
    system = env.system

    async def reverify(conn: Conn) -> int:
        binding = await sender_bindings_repo.get_binding(conn, system, sender.binding_id)
        updated = await sender_bindings_repo.record_verification(
            conn,
            system,
            binding.id,
            expected_version=binding.version,
            verified=True,
            alias_verified=True,
            health="healthy",
            reason="periodic re-verification (synthetic)",
        )
        return updated.version

    version = await run(env.ctx, system, reverify)
    reports = await work(env, JobType.SELLER_INQUIRY_SEND)
    assert [r.code for r in reports] == ["cancelled"]  # SENDER_CHANGED: the binding is immutable
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.CANCELLED and debits_of(env) == 0

    # The new binding version has no activation evidence yet (F3/OPS-04, wave D2): decided again,
    # nothing reserved, the named reason recorded.
    report = await Reconciler(env.ctx).reconcile_workspace(env.workspace_id)
    assert report.inquiry_replan_jobs == 1 and report.errors == []
    reports = await work(env)
    plans = [r for r in reports if r.job_type == JobType.SELLER_INQUIRY_PLAN]
    assert [r.code for r in plans] == ["readiness_recorded"], plans
    assert plans[0].details["reservation"] == "activation_canary_incomplete"
    assert debits_of(env) == 0
    # The owner's canary for the new version completes: the next pass reserves.
    await complete_activation_canary(env.ctx.db, env.workspace_id, sender.binding_id)
    report = await Reconciler(env.ctx).reconcile_workspace(env.workspace_id)
    assert report.inquiry_replan_jobs == 1 and report.errors == []
    reports = await work(env)
    assert [r.code for r in reports if r.job_type == JobType.SELLER_INQUIRY_PLAN] == ["reserved"]
    row = _row(
        env,
        "select state, sender_binding_version from app.seller_inquiries where workspace_id = %s",
        env.workspace_id,
    )
    assert row["state"] == InquiryState.SENDING and row["sender_binding_version"] == version
    assert sender.worker is not None
    [intent] = (await pending_intents(env, sender.worker)).intents
    assert intent.inquiry_id == inquiry["id"]
    # Reserved: nothing more to decide.
    assert (await Reconciler(env.ctx).reconcile_workspace(env.workspace_id)).inquiry_replan_jobs == 0


async def test_process_mode_change_is_decided_at_the_next_pass(env: PipelineEnv) -> None:
    # The workspace controls are automatic and the sender verified, but the PROCESS still runs
    # with SELLER_INQUIRY_MODE=disabled_until_sender_ready: readiness only.
    disabled = with_settings(
        env, runtime_settings(_db_url(env), seller_inquiry_mode="disabled_until_sender_ready")
    )
    await prepare_sender(disabled)
    listing = await eligible_live_listing(disabled)
    await link_seller(disabled, listing)
    await work(disabled)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUALIFYING and debits_of(env) == 0
    first = await Reconciler(disabled.ctx).reconcile_workspace(env.workspace_id)
    assert first.inquiry_replan_jobs == 1
    await work(disabled, JobType.SELLER_INQUIRY_PLAN)
    assert (await Reconciler(disabled.ctx).reconcile_workspace(env.workspace_id)).inquiry_replan_jobs == 0

    # The owner restarts the process in automatic mode: the very next pass decides again.
    automatic = with_settings(env, runtime_settings(_db_url(env)))
    report = await Reconciler(automatic.ctx).reconcile_workspace(env.workspace_id)
    assert report.inquiry_replan_jobs == 1
    await work(automatic, JobType.SELLER_INQUIRY_PLAN)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED and debits_of(env) == 1


# --------------------------------------------------------------------------------------------
# API route: a provider that cannot be built
# --------------------------------------------------------------------------------------------


async def _gmail_queued(env: PipelineEnv, api: GmailApi) -> tuple[Any, PipelineEnv]:
    sealed = box()
    sender = await prepare_sender(env, provider=EmailProviderKind.GMAIL_API, box=sealed)
    live = attach_gmail(with_settings(env, gmail_settings(_db_url(env), sender.binding_id)), api, sealed)
    listing = await eligible_live_listing(live)
    await link_seller(live, listing)
    await work(live, JobType.VALUATION, JobType.SELLER_INQUIRY_PLAN)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED and debits_of(env) == 1
    return sender, live


async def test_revoked_api_sender_is_suppressed_not_blocked(env: PipelineEnv) -> None:
    api = GmailApi()
    sender, live = await _gmail_queued(env, api)
    system = env.system

    async def revoke(conn: Conn) -> None:
        await sender_bindings_repo.revoke_binding(
            conn, system, sender.binding_id, reason="owner revoked access"
        )

    await run(env.ctx, system, revoke)
    reports = await work(live, JobType.SELLER_INQUIRY_SEND)
    assert [r.code for r in reports] == ["suppressed"]
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "succeeded" and send["blocker_code"] is None
    assert "SENDER_REVOKED" in send["result_reference"]["reasons"]
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.SUPPRESSED
    assert debits_of(env) == 0 and attempts_of(env, inquiry["id"]) == []  # never transmitted
    assert api.sent == [] and not [c for c in api.calls if "oauth2" in c or "gmail" in c]


async def test_setup_problem_the_preflight_cannot_see_blocks_without_an_intent(env: PipelineEnv) -> None:
    api = GmailApi()
    sender, _live = await _gmail_queued(env, api)
    # The process lost its secret reference: the provider cannot be built, the preflight would
    # proceed. Blocked for the operator; the intent of the preflight is rolled back.
    broken = attach_gmail(
        with_settings(
            env, gmail_settings(_db_url(env), sender.binding_id, seller_email_oauth_secret_reference=None)
        ),
        api,
        box(),
    )
    reports = await work(broken, JobType.SELLER_INQUIRY_SEND)
    assert [r.code for r in reports] == ["SENDER_SETUP_INCOMPLETE"]
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "blocked" and send["blocker_code"] == "SENDER_SETUP_INCOMPLETE"
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.QUEUED and attempts_of(env, inquiry["id"]) == []
    assert debits_of(env) == 1 and api.sent == []


# --------------------------------------------------------------------------------------------
# Reconciliation sweeps
# --------------------------------------------------------------------------------------------


async def test_a_blocked_reconcile_job_is_not_multiplied(env: PipelineEnv) -> None:
    api = GmailApi(send_mode="timeout")
    _sender, live = await _gmail_queued(env, api)
    await work(live, JobType.SELLER_INQUIRY_SEND)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.UNCERTAIN
    age_uncertainty(env, inquiry["id"], minutes=60)
    assert (await Reconciler(live.ctx).reconcile_workspace(env.workspace_id)).inquiry_reconcile_jobs == 1

    # The process now runs another send route: the gmail reconciliation source is unavailable.
    other = with_settings(env, runtime_settings(_db_url(env)))
    await work(other, JobType.SELLER_INQUIRY_RECONCILE)
    [job] = jobs_of(env, JobType.SELLER_INQUIRY_RECONCILE)
    assert job["state"] == "blocked" and job["blocker_code"] == "RECONCILE_PROVIDER_UNAVAILABLE"
    # TEST ARRANGEMENT: the blocked job belongs to an earlier hour bucket.
    env.seed.conn.execute(
        "update ops.jobs set dedup_key = %s where id = %s",
        (f"seller_inquiry.reconcile:{inquiry['id']}:2026010100", job["id"]),
    )
    report = await Reconciler(live.ctx).reconcile_workspace(env.workspace_id)
    assert report.inquiry_reconcile_jobs == 0 and report.errors == []
    assert len(jobs_of(env, JobType.SELLER_INQUIRY_RECONCILE)) == 1
    assert len(api.sent) == 1  # and never resent


def _expire_job_lease(env: PipelineEnv, job_id: UUID) -> None:
    """TEST ARRANGEMENT: the worker holding the job crashed; its lease ran out."""
    env.seed.conn.execute(
        "update ops.jobs set lease_expires_at = now() - interval '1 second' where id = %s", (job_id,)
    )


async def test_send_job_reaped_before_any_intent_is_requeued(env: PipelineEnv) -> None:
    sender, _listing = await _queued(env)
    claimed = await jobs.claim(
        env.ctx.db, env.workspace_id, "b2a-crashed-worker", [JobType.SELLER_INQUIRY_SEND], 60
    )
    assert claimed is not None
    _expire_job_lease(env, claimed.id)  # crashed before the dispatch transaction committed

    report = await Reconciler(env.ctx).reconcile_workspace(env.workspace_id)
    assert report.errors == [] and report.jobs_blocked_uncertain == 1
    assert report.inquiry_send_jobs_unblocked == 1
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "queued" and send["blocker_code"] is None
    [audit] = env.rows(
        "select metadata from ops.audit_events where workspace_id = %s and action = 'job.unblock'",
        env.workspace_id,
    )
    assert audit["metadata"]["acknowledged_uncertain_delivery"] is True
    reports = await work(env, JobType.SELLER_INQUIRY_SEND)
    assert [r.code for r in reports] == ["intent_published"]
    [inquiry] = inquiries_of(env)
    assert len(attempts_of(env, inquiry["id"])) == 1
    assert sender.worker is not None
    assert len((await pending_intents(env, sender.worker)).intents) == 1


async def test_send_job_reaped_after_its_intent_stays_blocked(env: PipelineEnv) -> None:
    await _queued(env)
    claimed = await jobs.claim(
        env.ctx.db, env.workspace_id, "b2a-crashed-worker", [JobType.SELLER_INQUIRY_SEND], 60
    )
    assert claimed is not None
    [inquiry] = inquiries_of(env)
    system = env.system

    async def commit_intent(conn: Conn) -> None:
        result = await inquiries_repo.dispatch(
            conn,
            system,
            inquiry["id"],
            lease=AttemptLease(
                owner=claimed.lease_owner,
                token=claimed.lease_token,
                expires_at=now_utc() + timedelta(minutes=5),
            ),
            message_approval_required=False,
            job_id=claimed.id,
        )
        assert result.outcome == "proceed"

    await run(env.ctx, system, commit_intent)  # the intent names the job; then the worker died
    _expire_job_lease(env, claimed.id)
    report = await Reconciler(env.ctx).reconcile_workspace(env.workspace_id)
    assert report.jobs_blocked_uncertain == 1 and report.inquiry_send_jobs_unblocked == 0
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["state"] == "blocked" and send["blocker_code"] == jobs.EMAIL_DELIVERY_UNCERTAIN
    assert len(attempts_of(env, inquiry["id"])) == 1


# --------------------------------------------------------------------------------------------
# A reply stored while the send was unresolved keeps its own state step
# --------------------------------------------------------------------------------------------


async def test_opt_out_stored_while_sending_ends_seller_opted_out(env: PipelineEnv) -> None:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    listing = await eligible_live_listing(env)
    seller = await link_seller(env, listing)
    await work(env)
    [intent] = (await pending_intents(env, sender.worker)).intents
    worker = sender.worker
    async with unit_of_work(env.ctx.db, worker.system_actor(WORKER_REQ)) as conn:
        claim = await send_intents_repo.claim(
            conn,
            worker,
            intent_id=intent.intent_id,
            claim_attempt_id="claim-1",
            worker_id="synthetic-desktop-1",
            request_id=WORKER_REQ,
        )
    assert claim.proceed
    # The seller opts out before the worker's Sent Items report arrives (inquiry still sending).
    stored = await seller_replies(
        env,
        worker,
        intent.inquiry_id,
        in_reply_to=intent.rfc_message_id,
        from_address=seller.address,
        body="Bitte keine weiteren Anfragen. Synthetic fixture reply.",
    )
    assert stored.ingest_status == "stored"
    await work(env)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.SENDING
    report = OutlookSendReport(
        intent_id=intent.intent_id,
        inquiry_id=intent.inquiry_id,
        mailbox_binding_id=intent.mailbox_binding_id,
        worker_id="synthetic-desktop-1",
        state=OutlookSubmissionState.SENT_ITEMS_CONFIRMED,
        account_smtp_address_used=intent.from_address,
        observed_internet_message_id=intent.rfc_message_id,
        sent_items_present=True,
        reported_at=now_utc(),
        sent_at=now_utc(),
    )
    async with unit_of_work(env.ctx.db, worker.system_actor(WORKER_REQ)) as conn:
        await send_intents_repo.report(conn, worker, report=report, request_id=WORKER_REQ)
    assert inquiries_of(env)[0]["state"] == InquiryState.ACCEPTED
    passed = await Reconciler(env.ctx).reconcile_workspace(env.workspace_id)
    assert passed.inquiries_marked_replied == 1 and passed.errors == []
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.SELLER_OPTED_OUT
    assert (await Reconciler(env.ctx).reconcile_workspace(env.workspace_id)).inquiries_marked_replied == 0
