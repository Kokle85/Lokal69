"""Regression tests from the independent SECURITY review (r2) of work package C1.

- A granted worker claim is evidence that ``.Send`` may have been called; it lives in the audit
  trail (``send_intent.claim``). The audit layer redacts free-text strings, and its phone-number
  rule rewrote about 0.07 % of UUID strings (``00123456-...`` -> ``[REDACTED_PHONE]-...``): such a
  granted claim was invisible to ``inquiries_repo.granted_claims`` and to the expired-intent
  listing, so a forged ``refused_before_send`` report (a stolen worker credential) became a proven
  pre-submission failure -> guarded retry -> a SECOND e-mail to the same seller. The claim now
  stores the intent id as a UUID value (never rewritten) and an unreadable claim audit fails safe
  (it counts for every local intent of the inquiry).
- A claim that waited for the row locks of a concurrent mailbox revocation was granted right after
  the revocation committed (the mailbox was only checked before the locks): the revoked worker
  credential could still be told to call ``.Send``. The claim (and the report) re-check the
  mailbox under the locks.
- Activation canaries are an e-mail path outside the seller caps: they are refused while the kill
  switch is on, bounded per rolling 24 hours, and their free text can never carry an address; the
  target-address hash is shown to the owner only.

Everything is synthetic; nothing is sent.
"""

from __future__ import annotations

import asyncio
import random
import uuid

import pytest
from tests.integration.v11_inquiries.support import World, owner, scalar, system
from tests.integration.v11_inquiries.test_canaries import TARGET, _canary, _reviewer
from tests.integration.v11_inquiries.test_send_intents import (
    REQ,
    _claim,
    _intent,
    _pending,
    _report,
    _send_report,
    _worker,
)

from suv_deals.domain.enums import InquiryState
from suv_deals.domain.inquiries import SendAttemptOutcome
from suv_deals.errors import Forbidden, RateLimited, ValidationFailed
from suv_deals.integrations.email_providers.outlook_local import (
    OutlookRefusalReason,
    OutlookSendIntent,
    OutlookSendReport,
    OutlookSubmissionState,
)
from suv_deals.observability.audit import redact_metadata
from suv_deals.persistence import audit, canaries_repo, inquiries_repo, mail_workers_repo, send_intents_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


def _mangled_uuid() -> uuid.UUID:
    """A random UUID whose canonical text the audit redaction rewrites (phone-number rule)."""
    rng = random.Random()
    digits = "".join(rng.choice("0123456789") for _ in range(5))
    tail = "".join(rng.choice("0123456789abcdef") for _ in range(12))
    value = uuid.UUID(f"001{digits}-a{rng.randrange(0x1000):03x}-4{rng.randrange(0x1000):03x}-8abc-{tail}")
    assert redact_metadata({"intent_id": str(value)})["intent_id"] != str(value)  # precondition
    return value


@pytest.fixture
def mangled_attempt_id(monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """The next dispatched attempt (== intent) id is one the audit redaction would rewrite."""
    value = _mangled_uuid()
    monkeypatch.setattr(send_intents_repo, "uuid4", lambda: value)
    return value


def _refusal(
    intent: OutlookSendIntent, reason: OutlookRefusalReason = OutlookRefusalReason.INTENT_EXPIRED
) -> OutlookSendReport:
    return _report(intent, OutlookSubmissionState.REFUSED_BEFORE_SEND, refusal=reason)


# ---------------------------------------------------------------------------------------------
# A granted claim must survive the audit redaction
# ---------------------------------------------------------------------------------------------


async def test_a_granted_claim_is_never_lost_to_the_audit_redaction(
    db: Database, world: World, mangled_attempt_id: uuid.UUID
) -> None:
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    assert intent.intent_id == mangled_attempt_id
    assert (await _claim(db, worker, intent.intent_id)).proceed  # the real worker may now .Send
    # A stolen credential reports a pre-send refusal for the claimed intent: no proof (before the
    # fix this became failed_definite / pre_submission_failure, i.e. eligible for a second e-mail).
    forged = await _send_report(db, worker, _refusal(intent))
    assert forged.inquiry_state == InquiryState.UNCERTAIN
    assert forged.attempt_outcome == SendAttemptOutcome.UNCERTAIN
    state = await scalar(
        db, world, "select state from app.seller_inquiries where id = %(id)s", {"id": inquiry_id}
    )
    assert state == "uncertain"
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        assert intent.intent_id in await inquiries_repo.granted_claims(conn, actor, inquiry_id)


async def test_an_expired_claimed_intent_is_never_offered_for_a_refusal(
    db: Database, world: World, mangled_attempt_id: uuid.UUID
) -> None:
    """The expired-intent listing offers only intents NO worker ever claimed; a claimed one whose
    lease ran out must never come back as ``expired`` (its refusal would be taken as proof)."""
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    assert (await _claim(db, worker, intent.intent_id)).proceed
    seed = world.seed
    with seed.conn.transaction():  # TEST ARRANGEMENT ONLY: the intent's lease ran out
        seed.conn.execute("set local session_replication_role = replica")
        seed.conn.execute(
            "update ops.email_delivery_attempts"
            " set lease_expires_at = send_intent_committed_at + interval '1 millisecond'"
            " where attempt_id = %s",
            (intent.intent_id,),
        )
    assert await inquiries_repo.reap_expired_attempts(db, world.workspace_id) == (intent.intent_id,)
    batch = await _pending(db, worker)
    assert intent.intent_id not in [i.intent_id for i in batch.expired]
    late = await _send_report(db, worker, _refusal(intent))
    assert late.inquiry_state == InquiryState.UNCERTAIN and not late.reconciled
    state = await scalar(
        db, world, "select state from app.seller_inquiries where id = %(id)s", {"id": inquiry_id}
    )
    assert state == "uncertain"


async def test_an_unreadable_claim_audit_fails_safe(db: Database, world: World) -> None:
    """A claim audit written before this fix may carry a rewritten intent id: it counts as a
    granted claim for every local intent of the inquiry (never as 'no claim')."""
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    rewritten = redact_metadata({"intent_id": str(_mangled_uuid())})["intent_id"]
    async with unit_of_work(db, worker.system_actor(REQ)) as conn:
        await audit.record(
            conn,
            worker.actor(REQ),
            inquiries_repo.CLAIM_AUDIT_ACTION,
            "seller_inquiry",
            inquiry_id,
            reason="claim granted",
            metadata={"intent_id": rewritten, "proceed": True, "detail": None},
        )
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        assert await inquiries_repo.granted_claims(conn, actor, inquiry_id) == frozenset({intent.intent_id})
    forged = await _send_report(db, worker, _refusal(intent))
    assert forged.inquiry_state == InquiryState.UNCERTAIN


# ---------------------------------------------------------------------------------------------
# A claim racing a mailbox revocation
# ---------------------------------------------------------------------------------------------


async def test_a_claim_waiting_for_a_revocation_is_never_granted(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    published = await scalar(
        db,
        world,
        "select count(*) from ops.mail_binding_sync where workspace_id = %(ws)s and inquiry_id = %(id)s",
        {"id": inquiry_id},
    )
    assert published >= 1  # the revocation locks (and tombstones) this inquiry
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        assert await mail_workers_repo.revoke_mail_worker(
            conn, boss, worker.mailbox_binding_id, reason="PC stolen (synthetic)"
        )
        # The claim authenticated before the revocation commits and waits for its row locks.
        racing = asyncio.create_task(_claim(db, worker, intent.intent_id))
        await asyncio.sleep(0.4)
        blocked = not racing.done()
    assert blocked, "the claim must wait for the revocation's row locks"
    with pytest.raises(Forbidden) as refused:
        await racing
    assert refused.value.details["reason"] == "mailbox_binding_revoked"
    granted = await scalar(
        db,
        world,
        "select count(*) from ops.audit_events where workspace_id = %(ws)s and target_id = %(id)s"
        " and action = 'send_intent.claim'",
        {"id": inquiry_id},
    )
    assert granted == 0


async def test_a_report_waiting_for_a_revocation_is_not_applied(db: Database, world: World) -> None:
    worker = await _worker(db, world)
    _, intent = await _intent(db, world, worker)
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        await mail_workers_repo.revoke_mail_worker(
            conn, boss, worker.mailbox_binding_id, reason="credential stolen (synthetic)"
        )
        racing = asyncio.create_task(_send_report(db, worker, _refusal(intent)))
        await asyncio.sleep(0.4)
        blocked = not racing.done()
    assert blocked, "the report must wait for the revocation's row locks"
    with pytest.raises(Forbidden):
        await racing
    outcome = await scalar(
        db,
        world,
        "select outcome from ops.email_delivery_attempts where attempt_id = %(id)s",
        {"id": intent.intent_id},
    )
    assert outcome == "running"  # nothing from the revoked credential was recorded


# ---------------------------------------------------------------------------------------------
# Activation canaries: kill switch, rolling bound, no address in free text, owner-only hash
# ---------------------------------------------------------------------------------------------


async def test_no_canary_while_the_kill_switch_is_on(db: Database, world: World) -> None:
    await _worker(db, world)
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        controls = await inquiries_repo.get_controls(conn, boss)
        assert controls is not None
        await inquiries_repo.pause(conn, boss, expected_version=controls.version, reason="stop (synthetic)")
    with pytest.raises(ValidationFailed) as exc:
        await _canary(db, world)
    assert exc.value.details["reason"] == "kill_switch_active"


async def test_canaries_are_bounded_per_rolling_day(db: Database, world: World) -> None:
    await _worker(db, world)
    boss = owner(world.workspace_id)
    for n in range(canaries_repo.MAX_CANARIES_PER_24H):
        record = await _canary(db, world, purpose=f"activation evidence {n} (synthetic)")
        if n % 2:  # a cancelled canary still counts (it may have been sent before the cancel)
            async with unit_of_work(db, boss) as conn:
                await canaries_repo.cancel_canary(conn, boss, record.id, reason="not needed (synthetic)")
    with pytest.raises(RateLimited) as exc:
        await _canary(db, world)
    assert exc.value.details["reason"] == "activation_canary_volume"
    count = await scalar(
        db, world, "select count(*) from ops.inquiry_activation_canaries where workspace_id = %(ws)s", {}
    )
    assert count == canaries_repo.MAX_CANARIES_PER_24H


async def test_canary_free_text_never_carries_an_address(db: Database, world: World) -> None:
    await _worker(db, world)
    with pytest.raises(ValidationFailed) as exc:
        await _canary(db, world, purpose=f"test to {TARGET}")
    assert exc.value.details["reason"] == "purpose"
    record = await _canary(db, world)
    boss = owner(world.workspace_id)
    with pytest.raises(ValidationFailed):
        async with unit_of_work(db, boss) as conn:
            await canaries_repo.cancel_canary(conn, boss, record.id, reason=f"wrong target {TARGET}")


async def test_the_target_hash_is_for_the_owner_only(db: Database, world: World) -> None:
    await _worker(db, world)
    record = await _canary(db, world)
    assert record.target_address_hash  # the owner (writer) sees it
    viewer = _reviewer(world.workspace_id)
    async with unit_of_work(db, viewer) as conn:
        listed = await canaries_repo.list_canaries(conn, viewer)
        single = await canaries_repo.get_canary(conn, viewer, record.id)
    assert [c.id for c in listed] == [record.id]
    assert listed[0].target_address_hash is None and single.target_address_hash is None
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        assert (await canaries_repo.get_canary(conn, boss, record.id)).target_address_hash == (
            record.target_address_hash
        )


async def test_send_intents_claim_audit_keeps_the_intent_id_verbatim(
    db: Database, world: World, mangled_attempt_id: uuid.UUID
) -> None:
    worker = await _worker(db, world)
    inquiry_id, intent = await _intent(db, world, worker)
    assert (await _claim(db, worker, intent.intent_id)).proceed
    stored = await scalar(
        db,
        world,
        "select metadata ->> 'intent_id' from ops.audit_events where workspace_id = %(ws)s"
        " and target_id = %(id)s and action = 'send_intent.claim'",
        {"id": inquiry_id},
    )
    assert stored == str(mangled_attempt_id)


async def test_only_a_writer_takes_the_send_path_row_locks(db: Database, world: World) -> None:
    """``lock_attempt`` takes the inquiry and attempt rows FOR UPDATE: an ``inquiries:read``
    principal (e.g. a reviewer) must never be able to hold the send path's locks."""
    worker = await _worker(db, world)
    _, intent = await _intent(db, world, worker)
    viewer = _reviewer(world.workspace_id)
    with pytest.raises(Forbidden):
        async with unit_of_work(db, viewer) as conn:
            await inquiries_repo.lock_attempt(conn, viewer, intent.intent_id)
    actor = system(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        record, attempt = await inquiries_repo.lock_attempt(conn, actor, intent.intent_id)
    assert attempt.attempt_id == intent.intent_id and record.id == attempt.inquiry_id
