"""SEC-1 (wave D2): one running ``outlook_local`` intent is granted to ONE desktop worker only.

Before D2 the server granted every claim of a running intent inside its lease, whatever
``worker_id`` asked, so the "never two ``.Send``" invariant rested on the desktop's per-data_dir
SQLite store alone (a second installation with the same credential, a reinstall or a wiped store
while the submission report was still queued locally re-claimed and re-sent). Now:

- the first granted claim pins the intent to its ``worker_id``; a claim by another worker id is
  refused ``intent_invalid`` / ``ALREADY_CLAIMED`` (final for that worker);
- the same worker id is granted again (a lost claim answer still works);
- refused claims do not pin anything (only GRANTED claim audits count);
- an unreadable granted-claim audit (intent id or worker id missing / rewritten) counts as a claim
  by another worker: fail safe, never "unclaimed";
- the refused second worker's ``refused_before_send`` report keeps the attempt ``uncertain``
  (a granted claim exists, so the refusal is no proof of non-submission): never resent.

Everything is synthetic; nothing is sent.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from tests.integration.v11_inquiries.support import World, scalar, system
from tests.integration.v11_inquiries.test_send_intents import REQ, _intent, _report, _send_report, _worker

from suv_deals.domain.enums import InquiryState
from suv_deals.domain.inquiries import SendAttemptOutcome
from suv_deals.integrations.email_providers.outlook_local import OutlookRefusalReason, OutlookSubmissionState
from suv_deals.persistence import audit, inquiries_repo, send_intents_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.mail_workers_repo import WorkerIdentity
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db

WORKER_A = "synthetic-desktop.sa1b2c3d4e5f60718"
WORKER_B = "synthetic-desktop.sb9f8e7d6c5b4a3a2"


async def _claim_as(
    db: Database, worker: WorkerIdentity, intent_id: UUID, worker_id: str, attempt: str
) -> send_intents_repo.ClaimResult:
    async with unit_of_work(db, worker.system_actor(REQ)) as conn:
        return await send_intents_repo.claim(
            conn,
            worker,
            intent_id=intent_id,
            claim_attempt_id=attempt,
            worker_id=worker_id,
            request_id=REQ,
        )


async def test_second_worker_is_refused_and_the_first_is_granted_again(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)

    first = await _claim_as(db, worker, intent.intent_id, WORKER_A, "c1")
    assert first.proceed

    second = await _claim_as(db, worker, intent.intent_id, WORKER_B, "c2")
    assert not second.proceed
    assert second.refusal_reason == OutlookRefusalReason.INTENT_INVALID
    assert second.detail == "ALREADY_CLAIMED"

    # The holder's lost claim answer: claiming again with the same worker id is granted.
    again = await _claim_as(db, worker, intent.intent_id, WORKER_A, "c3")
    assert again.proceed

    # Claims change no state; the intent is still listed for its worker until a report arrives.
    assert await _state(db, world, inquiry_id) == "sending"
    holders = await _holders(db, world, inquiry_id, intent.intent_id)
    assert holders == frozenset({WORKER_A})


async def test_refused_claims_do_not_pin_the_intent(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    _, intent = await _intent(db, world, worker)
    # Worker B's claim is refused by the serving process's own gate (kill switch semantics).
    async with unit_of_work(db, worker.system_actor(REQ)) as conn:
        refused = await send_intents_repo.claim(
            conn,
            worker,
            intent_id=intent.intent_id,
            claim_attempt_id="c1",
            worker_id=WORKER_B,
            request_id=REQ,
            process_gate="KILL_SWITCH_ACTIVE",
        )
    assert not refused.proceed and refused.refusal_reason == OutlookRefusalReason.KILL_SWITCH
    # Worker B's refused claim pinned nothing: worker A is granted.
    granted = await _claim_as(db, worker, intent.intent_id, WORKER_A, "c2")
    assert granted.proceed, granted.detail


async def test_unreadable_granted_claim_audit_fails_safe(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    # A granted claim audit whose intent id and worker id cannot be read (e.g. rewritten).
    async with unit_of_work(db, worker.system_actor(REQ)) as conn:
        await audit.record(
            conn,
            worker.actor(REQ),
            inquiries_repo.CLAIM_AUDIT_ACTION,
            "seller_inquiry",
            inquiry_id,
            reason="claim granted",
            metadata={"intent_id": "not-a-uuid", "proceed": True, "claim_attempt_id": "x"},
            outcome="succeeded",
        )
    refused = await _claim_as(db, worker, intent.intent_id, WORKER_A, "c1")
    assert not refused.proceed and refused.detail == "ALREADY_CLAIMED"


async def test_refused_second_worker_report_keeps_the_attempt_uncertain(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    assert (await _claim_as(db, worker, intent.intent_id, WORKER_A, "c1")).proceed
    refused = await _claim_as(db, worker, intent.intent_id, WORKER_B, "c2")
    assert not refused.proceed and refused.detail == "ALREADY_CLAIMED"
    # Worker B reports the refusal it received: worker A may have called .Send, so this is no
    # proof of non-submission - the attempt is held uncertain and never resent.
    result = await _send_report(
        db,
        worker,
        _report(
            intent, OutlookSubmissionState.REFUSED_BEFORE_SEND, refusal=OutlookRefusalReason.INTENT_INVALID
        ),
    )
    assert result.inquiry_state == InquiryState.UNCERTAIN
    assert result.attempt_outcome == SendAttemptOutcome.UNCERTAIN and not result.reconciled
    assert await _state(db, world, inquiry_id) == "uncertain"


async def _holders(db: Database, world: World, inquiry_id: UUID, intent_id: UUID) -> frozenset[str | None]:
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        return await inquiries_repo.granted_claim_workers(conn, actor, inquiry_id, intent_id)


async def _state(db: Database, world: World, inquiry_id: UUID) -> object:
    sql = "select state from app.seller_inquiries where id = %(id)s"
    return await scalar(db, world, sql, {"id": inquiry_id})
