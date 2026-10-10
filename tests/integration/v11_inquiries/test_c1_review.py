"""Regression tests from the independent C1 review (work package c1-persistence-db-hardening).

- The worker claim and a concurrent worker report are serialised on the inquiry/attempt rows: a
  ``refused_before_send`` report racing a claim that is being GRANTED can never be recorded as a
  proven pre-submission failure (``failed_definite``, eligible for the guarded retry) while the
  real worker goes on to call ``.Send`` - the double-send that C1 item 4 closes; and a claim
  racing a committing refusal decides on the closed attempt, never on a stale read.
- Waiting reasons read from the stored controls: the initial mode ``disabled_until_sender_ready``
  is ``SENDER_SETUP_INCOMPLETE`` (the owner paused nothing) and a rolling cap set to 0 is a
  visible ``RATE_CAP_REACHED`` wait (both are attention items).

Everything is synthetic; nothing is sent.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from tests.integration.v11_inquiries.support import World, owner, reserve_and_queue, scalar
from tests.integration.v11_inquiries.test_controls_hardening import _controls, _summaries
from tests.integration.v11_inquiries.test_send_intents import REQ, _intent, _report, _send_report, _worker

from suv_deals.domain.enums import InquiryState
from suv_deals.domain.inquiries import SendAttemptOutcome
from suv_deals.integrations.email_providers.outlook_local import OutlookRefusalReason, OutlookSubmissionState
from suv_deals.persistence import inquiries_repo, send_intents_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


async def test_a_report_racing_a_granted_claim_is_never_proof(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    forged = _report(
        intent, OutlookSubmissionState.REFUSED_BEFORE_SEND, refusal=OutlookRefusalReason.INTENT_EXPIRED
    )
    async with unit_of_work(db, worker.system_actor(REQ)) as conn:
        claim = await send_intents_repo.claim(
            conn,
            worker,
            intent_id=intent.intent_id,
            claim_attempt_id="claim-race",
            worker_id="synthetic-desktop-1",
            request_id=REQ,
        )
        assert claim.proceed  # granted, not yet committed: the real worker is about to .Send
        # A (stolen) credential reports a pre-send refusal for the same intent meanwhile.
        racing = asyncio.create_task(_send_report(db, worker, forged))
        await asyncio.sleep(0.4)
        blocked = not racing.done()
    result = await racing
    assert blocked, "the report must wait for the claim's row locks"
    assert result.inquiry_state == InquiryState.UNCERTAIN
    assert result.attempt_outcome == SendAttemptOutcome.UNCERTAIN
    state = await scalar(
        db, world, "select state from app.seller_inquiries where id = %(id)s", {"id": inquiry_id}
    )
    assert state == "uncertain"


async def test_a_claim_racing_a_committing_refusal_report_is_refused(db: Database, world: World) -> None:
    """The reverse order: the refusal report is recorded first (not yet committed); the claim
    waits for its row locks and then decides on the CLOSED attempt instead of a stale read."""
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    refusal = _report(
        intent, OutlookSubmissionState.REFUSED_BEFORE_SEND, refusal=OutlookRefusalReason.KILL_SWITCH
    )

    async def claim() -> send_intents_repo.ClaimResult:
        async with unit_of_work(db, worker.system_actor(REQ)) as conn:
            return await send_intents_repo.claim(
                conn,
                worker,
                intent_id=intent.intent_id,
                claim_attempt_id="claim-late",
                worker_id="synthetic-desktop-1",
                request_id=REQ,
            )

    async with unit_of_work(db, worker.system_actor(REQ)) as conn:
        reported = await send_intents_repo.report(conn, worker, report=refusal, request_id=REQ)
        assert reported.inquiry_state == InquiryState.FAILED_DEFINITE  # proven: no claim granted
        racing = asyncio.create_task(claim())
        await asyncio.sleep(0.4)
        blocked = not racing.done()
    decision = await racing
    assert blocked, "the claim must wait for the report's row locks"
    assert not decision.proceed
    assert decision.refusal_reason == OutlookRefusalReason.INTENT_INVALID
    assert decision.detail == "INTENT_CLOSED"
    state = await scalar(
        db, world, "select state from app.seller_inquiries where id = %(id)s", {"id": inquiry_id}
    )
    assert state == "failed_definite"


async def test_initial_mode_waits_for_the_sender_setup_not_a_pause(db: Database, world: World) -> None:
    queued = await reserve_and_queue(db, world)
    boss = owner(world.workspace_id)
    controls = await _controls(db, world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        await inquiries_repo.set_mode(
            conn,
            boss,
            expected_version=controls.version,
            mode="disabled_until_sender_ready",
            reason="sender re-setup (synthetic)",
        )
    attention = await _summaries(db, world, attention_only=True)
    assert attention[queued.id] == "SENDER_SETUP_INCOMPLETE"


async def test_a_cap_set_to_zero_is_an_attention_wait(db: Database, world: World) -> None:
    queued = await reserve_and_queue(db, world)
    assert (await _summaries(db, world))[queued.id] is None
    boss = owner(world.workspace_id)
    controls = await _controls(db, world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        await inquiries_repo.set_limits(
            conn,
            boss,
            expected_version=controls.version,
            max_per_24h=0,
            max_per_15d=5,
            seller_cooldown=timedelta(days=7),
            reason="hold every new inquiry (synthetic)",
        )
    attention = await _summaries(db, world, attention_only=True)
    assert attention[queued.id] == "RATE_CAP_REACHED"
