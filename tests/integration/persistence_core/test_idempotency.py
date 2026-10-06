"""Idempotency records on real PostgreSQL (spec 21: principal + operation scoped keys)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from tests.integration.db.helpers import Seed, World, unique
from tests.integration.persistence_core.support import member, system

from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role
from suv_deals.domain.reviews import SubmitRequest
from suv_deals.errors import ErrorCode, Forbidden, IdempotencyConflict, ValidationFailed, VersionConflict
from suv_deals.persistence import idempotency
from suv_deals.persistence.database import Database
from suv_deals.persistence.idempotency import InProgress, NewRequest, Replay, ReplayError

pytestmark = pytest.mark.db

OPERATION = "reviews_submit"


def _hash(**request: object) -> str:
    return idempotency.request_hash_for(OPERATION, {"case_id": str(uuid.uuid4()), **request})


async def test_same_key_same_hash_replays_and_different_hash_conflicts(db: Database, world_a: World) -> None:
    actor = member(world_a.workspace_id, Role.REVIEWER)
    key = unique("idem-key")
    request_hash = _hash(outcome="watch")
    async with db.transaction(actor) as conn:
        started = await idempotency.begin(conn, actor, OPERATION, key, request_hash)
        assert isinstance(started, NewRequest)
        await idempotency.complete(conn, actor, OPERATION, key, {"decision_id": "d-1", "case_version": 3})
    async with db.transaction(actor) as conn:
        replay = await idempotency.begin(conn, actor, OPERATION, key, request_hash)
    assert replay == Replay(result={"decision_id": "d-1", "case_version": 3})
    async with db.transaction(actor) as conn:
        with pytest.raises(IdempotencyConflict):
            await idempotency.begin(conn, actor, OPERATION, key, _hash(outcome="rejected"))


async def test_keys_are_scoped_by_principal_and_operation(db: Database, world_a: World) -> None:
    ws = world_a.workspace_id
    alice = member(ws, Role.REVIEWER)
    bob = member(ws, Role.REVIEWER)
    key = unique("idem-key")
    request_hash = _hash()
    async with db.transaction(alice) as conn:
        assert isinstance(await idempotency.begin(conn, alice, OPERATION, key, request_hash), NewRequest)
        await idempotency.complete(conn, alice, OPERATION, key, {"ok": True})
    async with db.transaction(bob) as conn:
        assert isinstance(await idempotency.begin(conn, bob, OPERATION, key, request_hash), NewRequest)
    async with db.transaction(alice) as conn:
        other_operation = await idempotency.begin(conn, alice, "reviews_claim", key, request_hash)
    assert isinstance(other_operation, NewRequest)


async def test_rolled_back_operation_leaves_no_record(db: Database, world_a: World) -> None:
    actor = member(world_a.workspace_id, Role.REVIEWER)
    key = unique("idem-key")
    request_hash = _hash()

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with db.transaction(actor) as conn:
            await idempotency.begin(conn, actor, OPERATION, key, request_hash)
            raise Boom
    async with db.transaction(actor) as conn:
        assert isinstance(await idempotency.begin(conn, actor, OPERATION, key, request_hash), NewRequest)


async def test_in_progress_record_is_reported_and_can_fail(db: Database, world_a: World) -> None:
    actor = member(world_a.workspace_id, Role.REVIEWER)
    key = unique("idem-key")
    request_hash = _hash()
    async with db.transaction(actor) as conn:  # multi-transaction operation: step 1 committed
        assert isinstance(await idempotency.begin(conn, actor, OPERATION, key, request_hash), NewRequest)
    async with db.transaction(actor) as conn:
        assert isinstance(await idempotency.begin(conn, actor, OPERATION, key, request_hash), InProgress)
    async with db.transaction(actor) as conn:
        await idempotency.fail(conn, actor, OPERATION, key, ErrorCode.CLAIM_EXPIRED)
    async with db.transaction(actor) as conn:
        assert await idempotency.begin(conn, actor, OPERATION, key, request_hash) == ReplayError(
            error_code="CLAIM_EXPIRED"
        )
        with pytest.raises(VersionConflict):
            await idempotency.complete(conn, actor, OPERATION, key, {"late": True})


async def test_concurrent_duplicate_waits_and_replays_instead_of_double_submitting(
    db: Database, world_a: World, seed: Seed
) -> None:
    """A timeout retry arrives while the first submission is still in flight (spec 21)."""
    ws = world_a.workspace_id
    actor = member(ws, Role.REVIEWER)
    key = unique("idem-key")
    request_hash = _hash(outcome="shortlisted")
    first_started = asyncio.Event()
    effects: list[str] = []

    async def submit(name: str, *, hold: bool) -> object:
        async with db.transaction(actor) as conn:
            started = await idempotency.begin(conn, actor, OPERATION, key, request_hash)
            if hold:
                first_started.set()
            if isinstance(started, NewRequest):
                effects.append(name)
                if hold:
                    await asyncio.sleep(0.3)  # the client times out and retries meanwhile
                await idempotency.complete(conn, actor, OPERATION, key, {"decision": name})
            return started

    async def retry() -> object:
        await first_started.wait()
        return await submit("retry", hold=False)

    first, second = await asyncio.gather(submit("first", hold=True), retry())
    assert isinstance(first, NewRequest)
    assert second == Replay(result={"decision": "first"})
    assert effects == ["first"]
    assert seed.scalar(
        "select count(*) from ops.idempotency_records where idempotency_key = %s", (key,)
    ) == 1


def _expired_record(seed: Seed, actor: ActorContext, key: str, request_hash: str) -> None:
    seed.conn.execute(
        "insert into ops.idempotency_records (workspace_id, principal_id, operation, idempotency_key,"
        " request_hash, state, result, completed_at, created_at, expires_at)"
        " values (%s, %s, %s, %s, %s, 'completed', %s::jsonb, now() - interval '1 hour',"
        " now() - interval '1 hour', now() - interval '1 minute')",
        (actor.workspace_id, actor.principal_id, OPERATION, key, request_hash, '{"old": true}'),
    )


async def test_expired_records_are_replaced_and_purged(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    actor = member(ws, Role.REVIEWER)
    key = unique("idem-key")
    _expired_record(seed, actor, key, _hash())
    async with db.transaction(actor) as conn:
        # Expired: treated as absent, even with a different request hash.
        assert isinstance(await idempotency.begin(conn, actor, OPERATION, key, _hash()), NewRequest)
    stale_key = unique("idem-key")
    _expired_record(seed, actor, stale_key, _hash())
    reviewer = member(ws, Role.REVIEWER)
    async with db.transaction(reviewer) as conn:
        with pytest.raises(Forbidden):
            await idempotency.delete_expired(conn, reviewer)
    async with db.transaction(system(ws)) as conn:
        assert await idempotency.delete_expired(conn, system(ws)) == 1
    assert (
        seed.scalar("select count(*) from ops.idempotency_records where idempotency_key = %s", (stale_key,))
        == 0
    )
    assert seed.scalar("select count(*) from ops.idempotency_records where idempotency_key = %s", (key,)) == 1


async def test_same_key_in_another_workspace_is_refused_without_leaking(
    db: Database, world_a: World, world_b: World
) -> None:
    principal = uuid.uuid4()
    in_a = member(world_a.workspace_id, Role.REVIEWER, principal_id=principal)
    in_b = member(world_b.workspace_id, Role.REVIEWER, principal_id=principal)
    key = unique("idem-key")
    async with db.transaction(in_a) as conn:
        await idempotency.begin(conn, in_a, OPERATION, key, _hash())
        await idempotency.complete(conn, in_a, OPERATION, key, {"secret_of_a": True})
    async with db.transaction(in_b) as conn:
        with pytest.raises(IdempotencyConflict) as excinfo:
            await idempotency.begin(conn, in_b, OPERATION, key, _hash())
    assert "secret_of_a" not in str(excinfo.value.to_payload())


async def test_inputs_are_validated_and_hashes_come_from_the_validated_model(db: Database, world_a: World) -> None:
    actor = member(world_a.workspace_id, Role.REVIEWER)
    async with db.transaction(actor) as conn:
        with pytest.raises(ValidationFailed):
            await idempotency.begin(conn, actor, OPERATION, "short", _hash())
        with pytest.raises(ValidationFailed):
            await idempotency.begin(conn, actor, OPERATION, unique("idem-key"), "not-a-hash")
        with pytest.raises(ValidationFailed):
            await idempotency.begin(conn, actor, "Bad Operation", unique("idem-key"), _hash())
    key = unique("idem-key")
    async with db.transaction(actor) as conn:
        await idempotency.begin(conn, actor, OPERATION, key, _hash())
        with pytest.raises(ValidationFailed):
            await idempotency.complete(conn, actor, OPERATION, key, {"claim_token": "plaintext-token"})
    # The canonical hash comes from the VALIDATED model, ignores the idempotency key and never
    # contains the plaintext claim token.
    body = {
        "case_id": str(uuid.uuid4()),
        "claim_token": "t" * 43,
        "expected_version": 2,
        "listing_revision": 3,
        "outcome": "watch",
        "reason_codes": ["SYNTHETIC"],
        "summary": "Synthetic rationale for an idempotency test.",
        "idempotency_key": unique("idem-key"),
    }
    first = idempotency.request_hash_for(OPERATION, SubmitRequest.model_validate(body))
    retried = SubmitRequest.model_validate({**body, "idempotency_key": unique("idem-key")})
    assert idempotency.request_hash_for(OPERATION, retried) == first
    changed = SubmitRequest.model_validate({**body, "outcome": "rejected"})
    assert idempotency.request_hash_for(OPERATION, changed) != first
