"""MCP Events subscriptions and delivery records (spec 22). SYNTHETIC data, ``suv_backend``."""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.repos_valuation_reviews.builders import (
    RealWorld,
    make_listing,
    open_case,
    reviewer,
    run,
    system,
)

from suv_deals.clock import FrozenClock
from suv_deals.domain.actor import ActorContext
from suv_deals.errors import Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.integrations import event_bridge as eb
from suv_deals.integrations.secret_box import SecretBox
from suv_deals.integrations.webhook_signing import parse_whsec
from suv_deals.persistence import subscriptions_repo as subs
from suv_deals.persistence.database import Conn, Database
from suv_deals.persistence.errors_map import LeaseLost

pytestmark = pytest.mark.db

CALLBACK = "https://callback.synthetic.example/hooks/review"


@pytest.fixture
def box() -> SecretBox:
    return SecretBox({1: os.urandom(32)}, 1)


def new_secret() -> str:
    return "whsec_" + base64.b64encode(os.urandom(32)).decode("ascii")


def subscribe_request(
    actor: ActorContext, secret: str, *, url: str = CALLBACK, profile: str = "primary"
) -> eb.SubscriptionRequest:
    params = {
        "name": eb.EVENT_NAME,
        "arguments": {"profile": profile},
        "delivery": {"mode": "webhook", "url": url, "secret": secret},
        "ttlMs": 3_600_000,
    }
    return eb.validate_subscribe_params(params, actor, clock=FrozenClock(datetime.now(UTC)))


def verified(secret: str, *, ok: bool = True) -> eb.VerificationResult:
    now = datetime.now(UTC)
    return eb.VerificationResult(
        ok=ok,
        reason=None if ok else eb.CallbackErrorReason.CHALLENGE_FAILED,
        detail=None,
        status_code=200 if ok else 400,
        webhook_id="msg_SYNTHETIC",
        attempted_at=now,
        verified_at=now if ok else None,
        secret_fingerprint=parse_whsec(secret).fingerprint,
    )


def member_actor(seed: Seed, ws: UUID, *, active: bool = True) -> ActorContext:
    """A signed-in reviewer with a real (synthetic) membership row."""
    user = seed.user()
    seed.membership(ws, user, "reviewer", active=active)
    return reviewer(ws, user, kind="user")


async def subscribe(
    db: Database, actor: ActorContext, box: SecretBox, secret: str, **kw: Any
) -> subs.SubscribeOutcome:
    request = subscribe_request(actor, secret, **kw)
    return await run(db, actor, lambda c: subs.create_or_refresh_subscription(c, actor, request, box))


async def verify(db: Database, actor: ActorContext, box: SecretBox, row_id: UUID, secret: str) -> Any:
    return await run(db, actor, lambda c: subs.record_verification(c, actor, row_id, verified(secret), box))


def outcome(kind: eb.DeliveryOutcomeKind, delivery: subs.ClaimedDelivery, **kw: Any) -> eb.DeliveryOutcome:
    return eb.DeliveryOutcome(
        kind=kind,
        subscription_id="sub_SYNTHETIC",
        webhook_id="msg_SYNTHETIC",
        attempt=delivery.attempts,
        **kw,
    )


async def new_event(db: Database, seed: Seed, world: RealWorld) -> UUID:
    listing, rev = make_listing(seed, world.workspace_id, world.source_id)
    created = await open_case(db, world, listing_id=listing, revision_id=rev)
    assert created.event_id is not None
    return created.event_id


async def test_subscription_identity_refresh_and_secret_storage(
    db: Database, seed: Seed, world: RealWorld, box: SecretBox
) -> None:
    actor = reviewer(world.workspace_id)
    secret = new_secret()
    created = await subscribe(db, actor, box, secret)
    record = created.record
    assert created.created and record.status is eb.SubscriptionStatus.PENDING_VERIFICATION
    assert record.subscription_id == subscribe_request(actor, secret).subscription_id
    assert record.filter_hash == subs.filter_hash_for(world.workspace_id, {"profile": "primary"})
    assert record.principal_id == actor.principal_id and record.secret_version == 1
    stored = seed.scalar("select encrypted_secret from ops.event_subscriptions where id = %s", (record.id,))
    assert secret.encode() not in bytes(stored) and parse_whsec(secret).key not in bytes(stored)
    refreshed = await subscribe(db, actor, box, secret)
    assert refreshed.refreshed and not refreshed.secret_rotated and refreshed.record.id == record.id
    assert refreshed.record.version == 2 and refreshed.record.expires_at >= record.expires_at
    assert (
        seed.scalar(
            "select count(*) from ops.event_subscriptions where principal_id = %s", (actor.principal_id,)
        )
        == 1
    )
    # Other arguments or another principal are different identities.
    other_profile = await subscribe(db, actor, box, secret, profile="manual_4000")
    assert other_profile.created and other_profile.record.subscription_id != record.subscription_id
    other_actor = reviewer(world.workspace_id)
    theirs = await subscribe(db, other_actor, box, secret)
    assert theirs.created and theirs.record.subscription_id != record.subscription_id
    # Ownership comes from authentication: a request built for another principal is refused.
    forged = subscribe_request(other_actor, secret)
    with pytest.raises(Forbidden):
        await run(db, actor, lambda c: subs.create_or_refresh_subscription(c, actor, forged, box))
    with pytest.raises(Forbidden):  # the dispatcher never owns subscriptions
        sys_actor = system(world.workspace_id)
        await run(db, sys_actor, lambda c: subs.create_or_refresh_subscription(c, sys_actor, forged, box))


async def test_secret_rotation_window_and_verification(
    db: Database, seed: Seed, world: RealWorld, box: SecretBox
) -> None:
    actor = member_actor(seed, world.workspace_id)
    old_secret, new = new_secret(), new_secret()
    created = await subscribe(db, actor, box, old_secret)
    active = await verify(db, actor, box, created.record.id, old_secret)
    assert active.status is eb.SubscriptionStatus.ACTIVE and active.verified_at is not None
    rotated = await subscribe(db, actor, box, new)
    record = rotated.record
    assert rotated.secret_rotated and record.secret_version == 2 and record.has_previous_secret
    assert record.status is eb.SubscriptionStatus.PENDING_VERIFICATION  # rotation restarts verification
    assert record.previous_secret_valid_until is not None
    assert (
        timedelta(minutes=55) < record.previous_secret_valid_until - datetime.now(UTC) <= timedelta(hours=1)
    )
    with pytest.raises(VersionConflict):  # a verification of the OLD secret no longer counts
        await verify(db, actor, box, record.id, old_secret)
    await verify(db, actor, box, record.id, new)
    sys_actor = system(world.workspace_id)
    found = await run(db, sys_actor, lambda c: subs.list_active_for_event(c, sys_actor, eb.EVENT_NAME, box))
    [target] = found.targets
    assert target.subscription_id == record.subscription_id and len(target.secrets) == 2
    assert target.secrets[0].matches(parse_whsec(new)) and target.secrets[1].matches(parse_whsec(old_secret))
    # After the window closes, a refresh drops the previous ciphertext.
    seed.conn.execute(
        "update ops.event_subscriptions"
        " set previous_secret_valid_until = clock_timestamp() - interval '1 second' where id = %s",
        (record.id,),
    )
    later = await subscribe(db, actor, box, new)
    assert not later.secret_rotated and not later.record.has_previous_secret
    # A box without the sealing key cannot open the secret: the subscription is reported, not used.
    other_box = SecretBox({2: os.urandom(32)}, 2)
    broken = await run(
        db, sys_actor, lambda c: subs.list_active_for_event(c, sys_actor, eb.EVENT_NAME, other_box)
    )
    assert broken.targets == () and broken.undecryptable == (record.id,)


async def test_unsubscribe_is_idempotent_and_stops_deliveries(
    db: Database, seed: Seed, world: RealWorld, box: SecretBox
) -> None:
    actor = member_actor(seed, world.workspace_id)
    secret = new_secret()
    created = await subscribe(db, actor, box, secret)
    await verify(db, actor, box, created.record.id, secret)
    event_id = await new_event(db, seed, world)
    sys_actor = system(world.workspace_id)
    [(delivery_id, _)] = await run(
        db, sys_actor, lambda c: subs.create_deliveries(c, sys_actor, event_id, [created.record.id])
    )
    request = eb.UnsubscribeRequest(
        subscription_id=created.record.subscription_id,
        workspace_id=world.workspace_id,
        principal_id=actor.principal_id,
    )
    first = await run(db, actor, lambda c: subs.unsubscribe(c, actor, request))
    assert first.matched and first.changed and first.subscription_id == created.record.subscription_id
    second = await run(db, actor, lambda c: subs.unsubscribe(c, actor, request))
    assert second.matched and not second.changed
    unknown = eb.UnsubscribeRequest(
        subscription_id="sub_" + "0" * 32, workspace_id=world.workspace_id, principal_id=actor.principal_id
    )
    nothing = await run(db, actor, lambda c: subs.unsubscribe(c, actor, unknown))
    assert not nothing.matched and nothing.subscription_id is None
    state = seed.scalar("select state from ops.event_deliveries where id = %s", (delivery_id,))
    assert state == "cancelled"
    found = await run(db, sys_actor, lambda c: subs.list_active_for_event(c, sys_actor, eb.EVENT_NAME, box))
    assert found.targets == ()
    assert await run(db, sys_actor, lambda c: subs.claim_due_deliveries(c, sys_actor, "dispatcher-1")) == []
    # Subscribing again reactivates the same identity and requires a new verification.
    again = await subscribe(db, actor, box, secret)
    assert again.reactivated and again.record.id == created.record.id and again.needs_verification


async def test_dispatch_rechecks_membership_and_scope(
    db: Database, seed: Seed, world: RealWorld, box: SecretBox
) -> None:
    ws = world.workspace_id
    member = member_actor(seed, ws)
    stranger = reviewer(ws)  # an MCP client principal without a membership or credential row
    unverified = member_actor(seed, ws)
    rows: dict[str, UUID] = {}
    for name, actor in (("member", member), ("stranger", stranger), ("unverified", unverified)):
        secret = new_secret()
        created = await subscribe(db, actor, box, secret)
        rows[name] = created.record.id
        if name != "unverified":
            await verify(db, actor, box, created.record.id, secret)
    sys_actor = system(ws)
    found = await run(db, sys_actor, lambda c: subs.list_active_for_event(c, sys_actor, eb.EVENT_NAME, box))
    assert [r.id for r in found.records] == [rows["member"]]
    assert found.revoked == (rows["stranger"],)
    revoked = await run(db, sys_actor, lambda c: subs.get_subscription(c, sys_actor, rows["stranger"]))
    assert (
        revoked.status is eb.SubscriptionStatus.REVOKED
        and revoked.revoke_reason == subs.ACCESS_REVOKED_REASON
    )
    # A membership that is deactivated later loses the subscription at the next dispatch.
    seed.conn.execute(
        "update app.memberships set active = false where workspace_id = %s and user_id = %s",
        (ws, member.principal_id),
    )
    later = await run(db, sys_actor, lambda c: subs.list_active_for_event(c, sys_actor, eb.EVENT_NAME, box))
    assert later.targets == () and later.revoked == (rows["member"],)
    with pytest.raises(Forbidden):
        await run(db, member, lambda c: subs.list_active_for_event(c, member, eb.EVENT_NAME, box))


async def test_delivery_records_are_unique_leased_and_fenced(
    db: Database, seed: Seed, world: RealWorld, box: SecretBox
) -> None:
    actor = member_actor(seed, world.workspace_id)
    secret = new_secret()
    sub = (await subscribe(db, actor, box, secret)).record
    await verify(db, actor, box, sub.id, secret)
    sys_actor = system(world.workspace_id)
    events = [await new_event(db, seed, world) for _ in range(4)]

    async def create(conn: Conn) -> list[tuple[UUID, bool]]:
        made: list[tuple[UUID, bool]] = []
        for event in events:
            made += await subs.create_deliveries(conn, sys_actor, event, [sub.id, sub.id])
        return made

    made = await run(db, sys_actor, create)
    assert [created for _, created in made] == [True] * 4
    again = await run(db, sys_actor, lambda c: subs.create_deliveries(c, sys_actor, events[0], [sub.id]))
    assert again == [(made[0][0], False)]
    assert seed.scalar("select count(*) from ops.event_deliveries where subscription_id = %s", (sub.id,)) == 4
    claimed = await run(
        db, sys_actor, lambda c: subs.claim_due_deliveries(c, sys_actor, "dispatcher-1", limit=10)
    )
    assert len(claimed) == 4 and all(d.state == "sending" and d.attempts == 1 for d in claimed)
    assert await run(db, sys_actor, lambda c: subs.claim_due_deliveries(c, sys_actor, "dispatcher-2")) == []
    by_event = {d.event_id: d for d in claimed}
    now = datetime.now(UTC)
    accepted = by_event[events[0]]
    done = await run(
        db,
        sys_actor,
        lambda c: subs.record_delivery_outcome(
            c,
            sys_actor,
            accepted,
            outcome(eb.DeliveryOutcomeKind.DELIVERED, accepted, status_code=204, provider_accepted_at=now),
        ),
    )
    assert done.state == "accepted" and done.accepted_at is not None and done.last_response_code == 204
    with pytest.raises(LeaseLost):  # a late duplicate outcome cannot overwrite the result
        await run(
            db,
            sys_actor,
            lambda c: subs.record_delivery_outcome(
                c, sys_actor, accepted, outcome(eb.DeliveryOutcomeKind.FAILED, accepted, status_code=500)
            ),
        )
    gone = by_event[events[1]]
    terminal = await run(
        db,
        sys_actor,
        lambda c: subs.record_delivery_outcome(
            c,
            sys_actor,
            gone,
            outcome(
                eb.DeliveryOutcomeKind.FAILED,
                gone,
                status_code=410,
                reason=eb.DeliveryFailureReason.HTTP_410_GONE,
            ),
        ),
    )
    assert terminal.state == "failed" and terminal.safe_error == "http_410_gone"
    busy = by_event[events[2]]
    retry = await run(
        db,
        sys_actor,
        lambda c: subs.record_delivery_outcome(
            c,
            sys_actor,
            busy,
            outcome(
                eb.DeliveryOutcomeKind.RETRY,
                busy,
                status_code=503,
                reason=eb.DeliveryFailureReason.HTTP_5XX,
                next_attempt_at=now + timedelta(minutes=5),
            ),
        ),
    )
    assert retry.state == "retry_wait" and retry.next_attempt_at > now and retry.lease_token is None
    timeout = by_event[events[3]]
    unsure = await run(
        db,
        sys_actor,
        lambda c: subs.record_delivery_outcome(
            c,
            sys_actor,
            timeout,
            outcome(
                eb.DeliveryOutcomeKind.UNCERTAIN, timeout, reason=eb.DeliveryFailureReason.TIMEOUT_AFTER_SEND
            ),
        ),
    )
    assert unsure.state == "uncertain"
    requeued = await run(db, sys_actor, lambda c: subs.requeue_uncertain(c, sys_actor, timeout.id))
    assert requeued.state == "retry_wait"
    with pytest.raises(VersionConflict):
        await run(db, sys_actor, lambda c: subs.requeue_uncertain(c, sys_actor, timeout.id))
    # A dispatcher that lost its lease may have sent the request: the reaper marks it uncertain.
    leased = await run(db, sys_actor, lambda c: subs.claim_due_deliveries(c, sys_actor, "dispatcher-3"))
    assert [d.id for d in leased] == [timeout.id] and leased[0].attempts == 2
    seed.conn.execute(
        "update ops.event_deliveries set lease_expires_at = clock_timestamp() - interval '1 second'"
        " where id = %s",
        (timeout.id,),
    )
    reaped = await run(db, sys_actor, lambda c: subs.reap_expired_deliveries(c, sys_actor))
    assert reaped == [timeout.id]
    with pytest.raises(LeaseLost):
        await run(
            db,
            sys_actor,
            lambda c: subs.record_delivery_outcome(
                c, sys_actor, leased[0], outcome(eb.DeliveryOutcomeKind.DELIVERED, leased[0], status_code=200)
            ),
        )
    with pytest.raises(Forbidden):
        await run(db, actor, lambda c: subs.claim_due_deliveries(c, actor, "dispatcher-x"))


async def test_revocation_and_workspace_isolation(
    db: Database, seed: Seed, world: RealWorld, other_world: RealWorld, box: SecretBox
) -> None:
    actor = member_actor(seed, world.workspace_id)
    secret = new_secret()
    sub = (await subscribe(db, actor, box, secret)).record
    await verify(db, actor, box, sub.id, secret)
    event_id = await new_event(db, seed, world)
    sys_actor = system(world.workspace_id)
    [(delivery_id, _)] = await run(
        db, sys_actor, lambda c: subs.create_deliveries(c, sys_actor, event_id, [sub.id])
    )
    foreign = reviewer(other_world.workspace_id)
    with pytest.raises(NotFound):
        await run(db, foreign, lambda c: subs.get_subscription(c, foreign, sub.id))
    assert await run(db, foreign, lambda c: subs.list_subscriptions(c, foreign, include_revoked=True)) == []
    with pytest.raises(NotFound):
        await run(db, foreign, lambda c: subs.revoke_subscription(c, foreign, sub.id, "SYNTHETIC test"))
    other_member = reviewer(world.workspace_id)
    with pytest.raises(NotFound):  # another member cannot see or stop someone else's subscription
        await run(
            db, other_member, lambda c: subs.revoke_subscription(c, other_member, sub.id, "SYNTHETIC test")
        )
    with pytest.raises(ValidationFailed):
        await run(db, actor, lambda c: subs.revoke_subscription(c, actor, sub.id, "x"))
    revoked = await run(
        db, actor, lambda c: subs.revoke_subscription(c, actor, sub.id, "SYNTHETIC rotation test")
    )
    assert revoked.status is eb.SubscriptionStatus.REVOKED
    assert seed.scalar("select state from ops.event_deliveries where id = %s", (delivery_id,)) == "cancelled"
    same = await run(db, actor, lambda c: subs.revoke_subscription(c, actor, sub.id, "SYNTHETIC again"))
    assert same.revoked_at == revoked.revoked_at  # idempotent
    with pytest.raises(VersionConflict):  # a revoked subscription is not re-verified
        await verify(db, actor, box, sub.id, secret)
