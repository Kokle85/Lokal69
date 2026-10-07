"""Rolling caps, the kill switch, pause and resume (spec 37.4, 37.5, 37.10).

- at most 2 inquiries per rolling 24 hours and 5 per rolling 15 days per workspace;
- a backlog reserved long ago that is dispatched in a burst is counted at the send attempt, so
  the burst still respects the 24-hour cap (``hold`` with the time the window frees);
- the kill switch and the paused mode block reservation, queueing and dispatch; resume (owner,
  ``expected_version``) re-qualifies what the kill switch suppressed, with one audit event each.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.v11_inquiries.support import (
    World,
    add_vehicle,
    backdate,
    dispatch,
    now_utc,
    owner,
    qualify,
    queue,
    readiness,
    reserve,
    reserve_and_queue,
    reserve_existing,
    scalar,
    send,
    system,
)

from suv_deals.domain.enums import InquiryReadiness, InquiryState, SuppressionReason
from suv_deals.errors import Forbidden, RateLimited, VersionConflict
from suv_deals.persistence import inquiries_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


async def _controls_version(db: Database, world: World) -> int:
    async with unit_of_work(db, system(world.workspace_id)) as conn:
        controls = await inquiries_repo.get_controls(conn, system(world.workspace_id))
    assert controls is not None
    return controls.version


async def _pause(db: Database, world: World) -> None:
    actor = owner(world.workspace_id)
    async with unit_of_work(db, actor) as conn:
        result = await inquiries_repo.pause(
            conn, actor, expected_version=await _controls_version(db, world), reason="owner pause (synthetic)"
        )
    assert not result.already_paused


async def _usage(db: Database, world: World) -> inquiries_repo.QuotaUsage:
    async with unit_of_work(db, system(world.workspace_id)) as conn:
        return await inquiries_repo.quota_usage(conn, system(world.workspace_id))


async def test_two_per_rolling_24_hours(db: Database, seed: Seed, world: World) -> None:
    ws = world.workspace_id
    await send(db, world)
    third = world.with_vehicle(await add_vehicle(db, seed, ws))
    # A decision taken while the cap still had room ...
    early, early_decision, early_snapshot = await qualify(db, third)
    assert early_decision.can_reserve_now
    await send(db, world.with_vehicle(await add_vehicle(db, seed, ws)))
    assert (await _usage(db, world)).count_24h == 2
    # ... is refused under the controls lock once the cap filled (typed, with the retry time).
    with pytest.raises(RateLimited) as exc:
        await reserve_existing(db, third, early, early_decision, early_snapshot)
    assert exc.value.details.get("reason") == "inquiry_cap_reached"
    assert exc.value.details.get("next_allowed_at") is not None

    _, decision = await readiness(db, third)
    assert decision.readiness == InquiryReadiness.INQUIRY_READY and not decision.can_reserve_now
    assert "RATE_CAP_REACHED" in decision.codes() and decision.next_attempt_at is not None
    record, _, _ = await qualify(db, third)
    assert record.state == InquiryState.QUALIFYING  # waits for the window; not an approval queue
    async with unit_of_work(db, system(ws)) as conn:
        window = await inquiries_repo.next_window_at(conn, system(ws))
    assert window.blocked and window.next_at is not None
    assert timedelta(hours=23) < window.next_at - now_utc() <= timedelta(hours=24)


async def test_five_per_rolling_15_days(db: Database, seed: Seed, world: World) -> None:
    ws = world.workspace_id
    sent_ids = []
    for days_ago in (12, 9, 6, 4, 2):
        vehicle = await add_vehicle(db, seed, ws)
        record, _ = await send(db, world.with_vehicle(vehicle))
        backdate(seed.conn, record.id, to=now_utc() - timedelta(days=days_ago))
        sent_ids.append(record.id)
    usage = await _usage(db, world)
    assert usage.count_24h == 0 and usage.count_15d == 5

    sixth = world.with_vehicle(await add_vehicle(db, seed, ws))
    _, decision = await readiness(db, sixth)
    assert "RATE_CAP_REACHED" in decision.codes() and not decision.can_reserve_now
    async with unit_of_work(db, system(ws)) as conn:
        window = await inquiries_repo.next_window_at(conn, system(ws))
    assert window.next_at is not None
    # The oldest send (12 days ago) leaves the 15-day window in about 3 days.
    assert timedelta(days=2, hours=23) < window.next_at - now_utc() <= timedelta(days=3, minutes=1)


async def test_backlog_burst_is_counted_at_the_send_attempt(db: Database, seed: Seed, world: World) -> None:
    ws = world.workspace_id
    backlog = []
    for _ in range(3):
        vehicle = await add_vehicle(db, seed, ws)
        record = await reserve_and_queue(db, world.with_vehicle(vehicle))
        # Reserved three days ago and never sent (e.g. the sender was offline).
        backdate(seed.conn, record.id, to=now_utc() - timedelta(days=3))
        backlog.append(record)
    assert (await _usage(db, world)).count_24h == 0

    first = await dispatch(db, world, backlog[0].id)
    second = await dispatch(db, world, backlog[1].id)
    assert first.outcome == "proceed" and second.outcome == "proceed"
    third = await dispatch(db, world, backlog[2].id)
    assert third.outcome == "hold", third.decision
    assert third.next_attempt_at is not None and third.next_attempt_at > now_utc() + timedelta(hours=23)
    assert (await _usage(db, world)).count_24h == 2
    assert (
        await scalar(
            db, world, "select state from app.seller_inquiries where id = %(id)s", {"id": backlog[2].id}
        )
    ) == "queued"


async def test_kill_switch_blocks_reserve_queue_and_dispatch_and_resume_requalifies(
    db: Database, seed: Seed, world: World
) -> None:
    ws = world.workspace_id
    queued = await reserve_and_queue(db, world)
    reserved_only = await reserve(db, world.with_vehicle(await add_vehicle(db, seed, ws)))
    for record_id in (queued.id, reserved_only.id):  # free the 24-hour window for a third one
        backdate(seed.conn, record_id, to=now_utc() - timedelta(days=2))
    later = world.with_vehicle(await add_vehicle(db, seed, ws))
    record, decision, snapshot = await qualify(db, later)

    await _pause(db, world)
    # Reservation: refused under the controls lock even with a decision taken before the pause.
    with pytest.raises(VersionConflict) as exc:
        await reserve_existing(db, later, record, decision, snapshot)
    assert exc.value.details.get("reason") == "inquiry_kill_switch"
    # Queueing: refused.
    with pytest.raises(VersionConflict):
        await queue(db, world, reserved_only.id)
    # Dispatch: the queued inquiry is suppressed (kill_switch); nothing was transmitted.
    stopped = await dispatch(db, world, queued.id)
    assert stopped.outcome == "suppressed" and stopped.attempt is None
    state = await scalar(
        db,
        world,
        "select state || ':' || suppression_reason from app.seller_inquiries where id = %(id)s",
        {"id": queued.id},
    )
    assert state == f"suppressed:{SuppressionReason.KILL_SWITCH.value}"
    _, held = await readiness(db, later)
    assert "KILL_SWITCH_ACTIVE" in held.codes() and not held.can_reserve_now
    # Pausing again is a no-op; the version guards concurrent changes.
    actor = owner(ws)
    version = await _controls_version(db, world)
    async with unit_of_work(db, actor) as conn:
        again = await inquiries_repo.pause(conn, actor, expected_version=version, reason="again")
    assert again.already_paused and again.version == version
    with pytest.raises(VersionConflict):
        async with unit_of_work(db, actor) as conn:
            await inquiries_repo.resume(conn, actor, expected_version=version - 1, reason="stale view")

    # Resume: owner only; the kill-switch suppression is re-qualified with an audit event.
    with pytest.raises(Forbidden):
        async with unit_of_work(db, system(ws)) as conn:
            await inquiries_repo.resume(conn, system(ws), expected_version=version, reason="system resume")
    async with unit_of_work(db, actor) as conn:
        resumed = await inquiries_repo.resume(conn, actor, expected_version=version, reason="owner resume")
    assert resumed.version == version + 1
    after = await scalar(
        db, world, "select state from app.seller_inquiries where id = %(id)s", {"id": queued.id}
    )
    assert after == "qualifying"
    audits = await scalar(
        db,
        world,
        "select count(*) from ops.audit_events where workspace_id = %(ws)s and target_id = %(id)s"
        " and action = 'seller_inquiry.requalify'",
        {"id": queued.id},
    )
    assert audits == 1


async def test_paused_mode_holds_reservation_and_dispatch(db: Database, world: World) -> None:
    ws = world.workspace_id
    queued = await reserve_and_queue(db, world)
    actor = system(ws)
    async with unit_of_work(db, actor) as conn:
        await inquiries_repo.set_mode(
            conn,
            actor,
            expected_version=await _controls_version(db, world),
            mode="paused",
            reason="owner pause",
        )
    result = await dispatch(db, world, queued.id)
    assert result.outcome in ("hold", "suppressed") and result.attempt is None
    attempts = await scalar(
        db, world, "select count(*) from ops.email_delivery_attempts where workspace_id = %(ws)s", {}
    )
    assert attempts == 0
    async with unit_of_work(db, actor) as conn:
        window = await inquiries_repo.next_window_at(conn, actor)
    assert window.blocked and window.next_at is None  # waits for the owner, never a timer
