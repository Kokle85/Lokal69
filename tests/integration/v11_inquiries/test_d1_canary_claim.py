"""D1 item 3: the atomic canary claim (``canaries_repo.claim_for_send``).

``prepared -> uncertain`` in ONE guarded statement after the controls lock: the kill switch, the
mode, the standing authorization, the bound sender binding (unrevoked, verified, the canary's
version) and the desktop mailbox (active, of that sender, with a live credential) are re-checked
in the same statement that moves the canary, with the sender binding and the mailbox rows held
``FOR SHARE``. A revocation racing the claim is therefore either seen (the claim waits for it and
refuses) or happens after the claim committed; it can never slip in between a check and the
transition. A refused claim changes nothing and names every reason (codes only).

Nothing is sent: the target is a reserved ``example.invalid`` address and the repository only
records evidence.
"""

from __future__ import annotations

import asyncio

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import World, owner, system
from tests.integration.v11_inquiries.test_canaries import _canary, _reviewer
from tests.integration.v11_inquiries.test_send_intents import _worker
from tests.integration.v11_inquiries.test_suppression_and_authorization import _revoked

from suv_deals.errors import Forbidden
from suv_deals.persistence import canaries_repo, inquiries_repo, mail_workers_repo, sender_bindings_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


async def _claim(
    db: Database, world: World, record: canaries_repo.CanaryRecord, token: str = "a" * 32, **kwargs: object
) -> canaries_repo.CanaryRecord:
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        return await canaries_repo.claim_for_send(
            conn,
            boss,
            record.id,
            expected_version=int(kwargs.pop("expected_version", record.version)),  # type: ignore[call-overload]
            send_token=token,
        )


async def _refused(
    db: Database, world: World, record: canaries_repo.CanaryRecord, **kwargs: object
) -> set[str]:
    with pytest.raises(canaries_repo.CanaryClaimRefused) as caught:
        await _claim(db, world, record, **kwargs)  # type: ignore[arg-type]
    assert caught.value.details is not None and caught.value.details["reason"] == "canary_claim_refused"
    assert list(caught.value.problems) == caught.value.details["problems"]
    # Nothing changed: the canary is still prepared at its version.
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        fresh = await canaries_repo.get_canary(conn, boss, record.id)
    assert fresh.state == "prepared" and fresh.version == record.version
    return set(caught.value.problems)


async def test_a_claim_moves_a_prepared_canary_to_uncertain_exactly_once(db: Database, world: World) -> None:
    await _worker(db, world)
    record = await _canary(db, world)
    claimed = await _claim(db, world, record, token="b" * 32)
    assert claimed.state == "uncertain" and claimed.version == record.version + 1
    assert claimed.outcome_evidence["send_token"] == "b" * 32
    assert claimed.outcome_evidence["phase"] == "transport_started"
    # The same token is an idempotent replay; any other claimer is refused (never a second send).
    again = await _claim(db, world, record, token="b" * 32)
    assert again.id == claimed.id and again.version == claimed.version
    with pytest.raises(canaries_repo.CanaryClaimRefused) as caught:
        await _claim(db, world, record, token="c" * 32)
    assert "CANARY_NOT_PREPARED" in caught.value.problems
    # The transport's outcome follows the claim as before.
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        accepted = await canaries_repo.record_canary_outcome(
            conn, boss, record.id, outcome="accepted", evidence={"provider_code": "ok"}
        )
    assert accepted.state == "accepted"


async def test_a_claim_needs_a_writer(db: Database, world: World) -> None:
    await _worker(db, world)
    record = await _canary(db, world)
    reader = _reviewer(world.workspace_id)  # reads inquiries, no config:admin
    async with unit_of_work(db, reader) as conn:
        with pytest.raises(Forbidden):
            await canaries_repo.claim_for_send(
                conn, reader, record.id, expected_version=1, send_token="d" * 32
            )


async def test_the_kill_switch_and_the_mode_refuse_the_claim(db: Database, world: World) -> None:
    await _worker(db, world)
    record = await _canary(db, world)
    boss = owner(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        controls = await inquiries_repo.get_controls(conn, boss)
        assert controls is not None
        await inquiries_repo.pause(
            conn, boss, expected_version=controls.version, reason="Owner pause (synthetic)"
        )
    assert "KILL_SWITCH_ACTIVE" in await _refused(db, world, record)
    async with unit_of_work(db, boss) as conn:
        controls = await inquiries_repo.get_controls(conn, boss)
        assert controls is not None
        await inquiries_repo.resume(
            conn, boss, expected_version=controls.version, reason="Owner resume (synthetic)"
        )
        controls = await inquiries_repo.get_controls(conn, boss)
        assert controls is not None
        await inquiries_repo.set_mode(
            conn,
            boss,
            expected_version=controls.version,
            mode="paused",
            reason="Owner holds sending (synthetic)",
        )
    assert "CONTROLS_MODE_NOT_AUTOMATIC" in await _refused(db, world, record)


async def test_a_revoked_authorization_refuses_the_claim(db: Database, world: World) -> None:
    await _worker(db, world)
    record = await _canary(db, world)
    boss = system(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        await inquiries_repo.record_authorization(conn, boss, _revoked(2), reason="revocation v2 (synthetic)")
    assert "STANDING_AUTHORIZATION_REVOKED" in await _refused(db, world, record)


async def test_a_revoked_sender_or_mailbox_or_a_changed_version_refuses_the_claim(
    db: Database, seed: Seed, world: World
) -> None:
    worker = await _worker(db, world)
    record = await _canary(db, world)
    assert "CANARY_VERSION_CHANGED" in await _refused(db, world, record, expected_version=record.version + 7)
    boss = system(world.workspace_id)
    async with unit_of_work(db, boss) as conn:
        await mail_workers_repo.revoke_mail_worker(
            conn, boss, worker.mailbox_binding_id, reason="Laptop replaced (synthetic)"
        )
    assert "CANARY_MAILBOX_NOT_ACTIVE" in await _refused(db, world, record)
    async with unit_of_work(db, boss) as conn:
        await sender_bindings_repo.revoke_binding(
            conn, boss, world.sender_binding_id, reason="Identity retired"
        )
    problems = await _refused(db, world, record)
    assert {"SENDER_BINDING_REVOKED", "CANARY_MAILBOX_NOT_ACTIVE"} <= problems
    del seed


async def test_a_sender_revocation_racing_the_claim_is_seen_by_it(db: Database, world: World) -> None:
    """The race the atomic claim closes: the sender binding is revoked by a transaction that has
    not committed yet when the claim runs. The claim's ``FOR SHARE`` on the binding row waits for
    it, then sees the revocation and refuses; the canary stays ``prepared`` (nothing sent)."""
    await _worker(db, world)
    record = await _canary(db, world)
    boss = system(world.workspace_id)
    revoker_ready = asyncio.Event()
    release = asyncio.Event()

    async def revoke() -> None:
        async with unit_of_work(db, boss) as conn:
            await sender_bindings_repo.revoke_binding(
                conn, boss, world.sender_binding_id, reason="Identity retired"
            )
            revoker_ready.set()
            await release.wait()

    revoker = asyncio.create_task(revoke())
    await asyncio.wait_for(revoker_ready.wait(), timeout=10)
    claim = asyncio.create_task(_claim(db, world, record, token="e" * 32))
    await asyncio.sleep(0.3)
    assert not claim.done()  # waiting for the revocation's row lock, not deciding on stale rows
    release.set()
    await revoker
    with pytest.raises(canaries_repo.CanaryClaimRefused) as caught:
        await claim
    assert "SENDER_BINDING_REVOKED" in caught.value.problems
    owner_actor = owner(world.workspace_id)
    async with unit_of_work(db, owner_actor) as conn:
        fresh = await canaries_repo.get_canary(conn, owner_actor, record.id)
    assert fresh.state == "prepared"
