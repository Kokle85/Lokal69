"""Uncertain-send reconciliation on the ``outlook_local`` route and the reconciliation sweeps
(spec 37.5-37.7; work package B2a items 1 and 4).

- a seller reply stored while the inquiry was still ``sending`` is ``correlated_inbound``
  evidence: after the attempt reaper marked the send ``uncertain``, the reconcile job accepts it
  and the inquiry becomes ``replied`` -- the reservation and debit stay, nothing is resent;
- without positive evidence (no worker report, an empty Sent Items) the inquiry stays
  ``uncertain``: the reservation is never released and no second intent is ever published;
- a late worker report after a stored reply: the reconciliation pass moves ``accepted`` to
  ``replied``;
- re-decision sweep: readiness recorded while ``disabled_until_sender_ready`` is decided again
  once the owner switches the workspace to automatic mode (one plan job per fingerprint).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from tests.integration.pipeline.support import PipelineEnv, run
from tests.integration.v11_runtime.support import (
    WORKER_REQ,
    age_uncertainty,
    attempts_of,
    debits_of,
    eligible_live_listing,
    inquiries_of,
    jobs_of,
    link_seller,
    no_approval_state,
    now_utc,
    pending_intents,
    prepare_sender,
    runtime_settings,
    seller_replies,
    with_settings,
    work,
)

from suv_deals.domain.enums import InquiryState, JobType
from suv_deals.integrations.email_providers.outlook_local import OutlookSendReport, OutlookSubmissionState
from suv_deals.persistence import inquiries_repo, send_intents_repo
from suv_deals.persistence.database import Conn
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.workers.reconciliation import Reconciler

pytestmark = pytest.mark.db

REPLY_BODY = "Guten Tag, das Fahrzeug ist noch verfügbar. Synthetic fixture reply."


def _db_url(env: PipelineEnv) -> str:
    url = env.ctx.settings.database_url
    assert url is not None
    return url.get_secret_value()


def _expire_intent(env: PipelineEnv, inquiry_id: UUID) -> None:
    """TEST ARRANGEMENT (superuser, triggers bypassed for one statement): the running attempt's
    intent expired an hour ago without any worker report (laptop crashed after ``.Send``)."""
    with env.seed.conn.transaction():
        env.seed.conn.execute("set local session_replication_role = replica")
        env.seed.conn.execute(
            "update ops.email_delivery_attempts set send_intent_committed_at = now() - interval '2 hours',"
            " lease_expires_at = now() - interval '1 hour'"
            " where workspace_id = %s and inquiry_id = %s and outcome = 'running'",
            (env.workspace_id, inquiry_id),
        )


async def _published(env: PipelineEnv) -> tuple[Any, Any, dict[str, Any]]:
    sender = await prepare_sender(env)
    assert sender.worker is not None
    listing = await eligible_live_listing(env)
    seller = await link_seller(env, listing)
    await work(env)
    [intent] = (await pending_intents(env, sender.worker)).intents
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.SENDING
    return sender, seller, {"intent": intent, "inquiry": inquiry}


async def _claim(env: PipelineEnv, worker: Any, intent: Any) -> None:
    async with unit_of_work(env.ctx.db, worker.system_actor(WORKER_REQ)) as conn:
        claim = await send_intents_repo.claim(
            conn,
            worker,
            intent_id=intent.intent_id,
            claim_attempt_id="claim-1",
            worker_id="synthetic-desktop-1",
            request_id=WORKER_REQ,
        )
    assert claim.proceed, claim


async def test_reply_stored_while_sending_resolves_the_reaped_uncertain_send(env: PipelineEnv) -> None:
    sender, seller, arranged = await _published(env)
    intent, inquiry = arranged["intent"], arranged["inquiry"]
    await _claim(env, sender.worker, intent)  # the worker called .Send, then crashed before reporting

    # The seller answers while the backend still sees ``sending``: stored, no state step yet.
    reply = await seller_replies(
        env,
        sender.worker,
        inquiry["id"],
        in_reply_to=intent.rfc_message_id,
        from_address=seller.address,
        body=REPLY_BODY,
    )
    assert reply.ingest_status == "stored"
    await work(env)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.SENDING

    # The intent expires: the attempt reaper marks the send uncertain (reservation + debit stay).
    _expire_intent(env, inquiry["id"])
    reconciler = Reconciler(env.ctx)
    report = await reconciler.reconcile_workspace(env.workspace_id)
    assert report.send_attempts_uncertain == 1
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.UNCERTAIN and debits_of(env) == 1

    # Old enough: the pass schedules ONE reconcile job, which cites the correlated reply.
    age_uncertainty(env, inquiry["id"], minutes=30)
    report = await reconciler.reconcile_workspace(env.workspace_id)
    assert report.inquiry_reconcile_jobs == 1
    await work(env)
    [job] = jobs_of(env, JobType.SELLER_INQUIRY_RECONCILE)
    assert job["state"] == "succeeded" and job["result_reference"]["outcome"] == "accepted", job
    assert job["result_reference"]["state"] == InquiryState.REPLIED
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.REPLIED
    [attempt] = attempts_of(env, inquiry["id"])
    assert attempt["outcome"] == "uncertain" and attempt["reconciled_outcome"] == "accepted"
    # Never a second intent or transmission; the debit stays.
    assert (await pending_intents(env, sender.worker)).intents == ()
    assert len(jobs_of(env, JobType.SELLER_INQUIRY_SEND)) == 1 and debits_of(env) == 1
    no_approval_state(env)


async def test_uncertain_without_evidence_stays_uncertain_and_is_never_resent(env: PipelineEnv) -> None:
    sender, _seller, arranged = await _published(env)
    intent, inquiry = arranged["intent"], arranged["inquiry"]
    await _claim(env, sender.worker, intent)
    _expire_intent(env, inquiry["id"])
    reconciler = Reconciler(env.ctx)
    await reconciler.reconcile_workspace(env.workspace_id)
    age_uncertainty(env, inquiry["id"], minutes=30)
    assert (await reconciler.reconcile_workspace(env.workspace_id)).inquiry_reconcile_jobs == 1
    await work(env)
    [job] = jobs_of(env, JobType.SELLER_INQUIRY_RECONCILE)
    assert job["state"] == "succeeded" and job["result_reference"]["outcome"] == "still_uncertain", job
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.UNCERTAIN
    assert debits_of(env) == 1  # an empty Sent Items never releases the reservation
    assert len(attempts_of(env, inquiry["id"])) == 1
    assert (await pending_intents(env, sender.worker)).intents == ()
    [send] = jobs_of(env, JobType.SELLER_INQUIRY_SEND)
    assert send["result_reference"]["outcome"] == "intent_published"  # never a second send job
    # The same hour: no second reconcile job.
    assert (await reconciler.reconcile_workspace(env.workspace_id)).inquiry_reconcile_jobs == 0


async def test_late_worker_report_after_a_stored_reply_is_marked_replied(env: PipelineEnv) -> None:
    sender, seller, arranged = await _published(env)
    intent, inquiry = arranged["intent"], arranged["inquiry"]
    await _claim(env, sender.worker, intent)
    await seller_replies(
        env,
        sender.worker,
        inquiry["id"],
        in_reply_to=intent.rfc_message_id,
        from_address=seller.address,
        body=REPLY_BODY,
    )
    await work(env)
    # The worker's Sent Items report arrives only now.
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
    assert sender.worker is not None
    async with unit_of_work(env.ctx.db, sender.worker.system_actor(WORKER_REQ)) as conn:
        await send_intents_repo.report(conn, sender.worker, report=report, request_id=WORKER_REQ)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.ACCEPTED
    reconciler = Reconciler(env.ctx)
    dry = await reconciler.reconcile_workspace(env.workspace_id, dry_run=True)
    assert dry.inquiries_marked_replied == 1
    assert inquiries_of(env)[0]["state"] == InquiryState.ACCEPTED  # the dry run changed nothing
    report_live = await reconciler.reconcile_workspace(env.workspace_id)
    assert report_live.inquiries_marked_replied == 1 and report_live.errors == []
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.REPLIED
    assert (await reconciler.reconcile_workspace(env.workspace_id)).inquiries_marked_replied == 0


async def test_readiness_recorded_while_disabled_is_decided_again_after_activation(env: PipelineEnv) -> None:
    disabled = with_settings(
        env, runtime_settings(_db_url(env), seller_inquiry_mode="disabled_until_sender_ready")
    )
    sender = await prepare_sender(disabled, automatic=False)
    assert sender.worker is not None
    listing = await eligible_live_listing(disabled)
    await link_seller(disabled, listing)
    await work(disabled)
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.HELD_FACTS and debits_of(env) == 0

    # Nothing changed: the sweep plans nothing new in the same day for the same prerequisites.
    automatic = with_settings(env, runtime_settings(_db_url(env)))
    reconciler = Reconciler(automatic.ctx)
    first = await reconciler.reconcile_workspace(env.workspace_id)
    assert first.inquiry_replan_jobs == 1  # first fingerprint of this revision
    await work(automatic, JobType.SELLER_INQUIRY_PLAN)
    assert (await reconciler.reconcile_workspace(env.workspace_id)).inquiry_replan_jobs == 0
    assert debits_of(env) == 0  # the workspace controls are still disabled

    # The owner verifies the sender and switches the workspace to automatic mode.
    actor = env.system

    async def activate(conn: Conn) -> None:
        controls = await inquiries_repo.get_controls(conn, actor)
        assert controls is not None
        await inquiries_repo.set_mode(
            conn, actor, expected_version=controls.version, mode="automatic", reason="sender verified"
        )

    await run(env.ctx, actor, activate)
    report = await reconciler.reconcile_workspace(env.workspace_id)
    assert report.inquiry_replan_jobs == 1 and report.errors == []
    await work(automatic)
    outcomes = [j["result_reference"]["outcome"] for j in jobs_of(env, JobType.SELLER_INQUIRY_PLAN)]
    assert outcomes[-1] == "reserved", outcomes
    [inquiry] = inquiries_of(env)
    assert inquiry["state"] == InquiryState.SENDING and debits_of(env) == 1
    [intent] = (await pending_intents(env, sender.worker)).intents
    assert intent.inquiry_id == inquiry["id"]
    # Reserved: no further re-decisions.
    assert (await reconciler.reconcile_workspace(env.workspace_id)).inquiry_replan_jobs == 0
    no_approval_state(env)
