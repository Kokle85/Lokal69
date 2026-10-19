"""Dispatch-time re-checks and identity merges (spec 37.1, 37.5; 37.10 delta tests).

- The rolling caps limit TRANSMISSIONS: every debit counts at the later of its reservation and
  its send attempt, and the caps are re-checked before every transmission, so a backlog that
  queued up while the sender was offline can never leave in a burst (two dispatches racing for
  the last slot: exactly one wins).
- The seller cooldown is re-checked at dispatch against (possibly) transmitted inquiries.
- One initial inquiry per ACTUAL vehicle/seller pair: neither a cluster confirmed after a
  listing-identity send, nor a seller merge, nor a plausible-but-unresolved cross-site
  duplicate can produce a second e-mail to the same seller about the same car.
- A seller merge is serialised with a reservation of the surviving entity.
- Lifecycle timestamps (cooldown, caps, "possibly transmitted") are database-owned.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from tests.integration.db.helpers import Seed, backend
from tests.integration.v11_db.support import (
    SV_FROZEN,
    SV_TRANSITION,
    InquiryWorld,
    arrange_inquiry_history,
    attempt_values,
    binding_values,
    confirmed_cluster,
    contact,
    debit,
    dispatch,
    expect_sqlstate,
    finish_attempt,
    insert_attempt,
    insert_inquiry,
    queue,
    race,
    reserve,
    reserve_with_debit_at,
    seller_entity,
    sent_inquiry,
    single_success,
    state_of,
    update_inquiry,
    vehicle,
    with_vehicle,
)

pytestmark = pytest.mark.db

CAP_AT_DISPATCH = "cap reached at dispatch"
ONE_PER_PAIR = "one initial inquiry per vehicle/seller pair"


def _other_seller(db_conn: psycopg.Connection, iw: InquiryWorld) -> tuple[InquiryWorld, uuid.UUID]:
    """Another car of another seller (no cooldown or identity interaction with ``iw``)."""
    world = with_vehicle(iw, seller=seller_entity(iw.seed, iw.workspace_id))
    return world, insert_inquiry(db_conn, world)


def _attempts(db_conn: psycopg.Connection, workspace_id: uuid.UUID) -> int:
    row = db_conn.execute(
        "select count(*) from ops.email_delivery_attempts where workspace_id = %s", (workspace_id,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def _world_for(
    iw: InquiryWorld, veh_world: InquiryWorld, seller: uuid.UUID, contact_id: uuid.UUID, address: str
) -> InquiryWorld:
    return InquiryWorld(
        iw.workspace_id,
        iw.seed,
        veh_world.vehicle,
        seller,
        contact_id,
        address,
        iw.authorization_id,
        iw.sender_binding_id,
        iw.controls_id,
    )


# ---------------------------------------------------------------------------------------------
# Rolling caps limit transmissions, not only reservations
# ---------------------------------------------------------------------------------------------


def test_queued_backlog_never_leaves_in_a_burst(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    """Two inquiries reserved two days ago (sender offline since) and two reserved today: when
    the sender comes back, only two leave within the rolling 24 hours."""
    backlog: list[tuple[InquiryWorld, uuid.UUID]] = []
    for _ in range(2):
        world, inquiry = _other_seller(db_conn, iw)
        reserve_with_debit_at(db_conn, world, inquiry, datetime.now(UTC) - timedelta(days=2))
        queue(db_conn, world, inquiry)
        backlog.append((world, inquiry))
    for _ in range(2):
        world, inquiry = _other_seller(db_conn, iw)
        reserve(db_conn, world, inquiry)
        queue(db_conn, world, inquiry)
        backlog.append((world, inquiry))
    sent = 0
    for world, inquiry in backlog:
        try:
            dispatch(db_conn, world, inquiry)
            sent += 1
        except psycopg.Error as exc:
            assert exc.sqlstate == SV_TRANSITION, str(exc)
            assert CAP_AT_DISPATCH in str(exc) and "24 hours" in str(exc), str(exc)
            assert state_of(db_conn, inquiry) == "queued"  # held, never lost or cancelled
    assert sent == 2
    assert _attempts(db_conn, iw.workspace_id) == 2


def test_fifteen_day_cap_is_rechecked_at_dispatch(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    first_world, first = _other_seller(db_conn, iw)
    reserve(db_conn, first_world, first)
    queue(db_conn, first_world, first)
    second_world, second = _other_seller(db_conn, iw)
    reserve(db_conn, second_world, second)
    # The owner reduces the 15-day ceiling below the reserved backlog: nothing leaves.
    db_conn.execute(
        "update app.seller_inquiry_controls set max_per_15d = 1, version = version + 1"
        " where workspace_id = %s",
        (iw.workspace_id,),
    )
    with expect_sqlstate(SV_TRANSITION, f"{CAP_AT_DISPATCH}: 1 per rolling 15 days"):
        dispatch(db_conn, first_world, first)
    # Cancelling the other (never transmitted) reservation releases its debit and the slot.
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, second, state="cancelled", state_reasons=["OWNER_REDUCED_CAP"])
        db_conn.execute(
            "update ops.inquiry_quota_ledger set released_at = now(), release_reason = 'CANCELLED_UNSENT'"
            " where inquiry_id = %s and released_at is null",
            (second,),
        )
    dispatch(db_conn, first_world, first)
    assert state_of(db_conn, first) == "sending"


def test_zero_caps_pause_dispatch_of_queued_work(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    db_conn.execute(
        "update app.seller_inquiry_controls set max_per_24h = 0, version = version + 1"
        " where workspace_id = %s",
        (iw.workspace_id,),
    )
    with expect_sqlstate(SV_TRANSITION, CAP_AT_DISPATCH):
        dispatch(db_conn, iw, inquiry)
    assert state_of(db_conn, inquiry) == "queued"


def test_a_debit_counts_again_from_its_send_attempt(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    """Reserved three days ago, transmitted now: it occupies today's window for new reservations."""
    old_world, old = _other_seller(db_conn, iw)
    reserve_with_debit_at(db_conn, old_world, old, datetime.now(UTC) - timedelta(days=3))
    queue(db_conn, old_world, old)
    dispatch(db_conn, old_world, old)
    second_world, second = _other_seller(db_conn, iw)
    reserve(db_conn, second_world, second)
    third_world, third = _other_seller(db_conn, iw)
    with expect_sqlstate(SV_TRANSITION, "2 per rolling 24 hours"):
        reserve(db_conn, third_world, third)
    usage = db_conn.execute("select * from ops.inquiry_quota_usage(%s, null)", (iw.workspace_id,)).fetchone()
    assert usage == (2, 2)


def test_concurrent_dispatches_cannot_both_take_the_last_slot(
    db_url: str, db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    sent_world, sent = _other_seller(db_conn, iw)
    reserve(db_conn, sent_world, sent)
    queue(db_conn, sent_world, sent)
    dispatch(db_conn, sent_world, sent)  # one of the two daily slots is used
    racers = []
    for _ in range(2):
        world, inquiry = _other_seller(db_conn, iw)
        reserve_with_debit_at(db_conn, world, inquiry, datetime.now(UTC) - timedelta(days=2))
        queue(db_conn, world, inquiry)
        racers.append((world, inquiry))

    def job(world: InquiryWorld, inquiry: uuid.UUID) -> Callable[[psycopg.Connection], object]:
        return lambda conn: dispatch(conn, world, inquiry)

    results = race(db_url, *(job(w, i) for w, i in racers))
    single_success(results, SV_TRANSITION)
    assert CAP_AT_DISPATCH in str(next(r for r in results if r is not None))
    assert _attempts(db_conn, iw.workspace_id) == 2


def test_dispatch_rechecks_the_seller_cooldown(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    """Two cars of one dealer reserved more than a cooldown apart (sender offline in between)
    never leave together."""
    first = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, first)
    queue(db_conn, iw, first)
    arrange_inquiry_history(
        db_conn,
        first,
        reserved_at=datetime.now(UTC) - timedelta(days=8),
        queued_at=datetime.now(UTC) - timedelta(days=8),
    )
    second_car = with_vehicle(iw)  # same seller entity
    second = insert_inquiry(db_conn, second_car)
    reserve(db_conn, second_car, second)  # the first reservation is older than the cooldown
    queue(db_conn, second_car, second)
    dispatch(db_conn, iw, first)
    with expect_sqlstate(SV_TRANSITION, "transmitted recently"):
        dispatch(db_conn, second_car, second)
    assert state_of(db_conn, second) == "queued"


# ---------------------------------------------------------------------------------------------
# One inquiry per actual vehicle/seller pair across identity and seller merges
# ---------------------------------------------------------------------------------------------


def _twin_of(seed: Seed, iw: InquiryWorld, seller: uuid.UUID | None = None) -> InquiryWorld:
    """The same synthetic car on another site, with its own verified contact."""
    twin = vehicle(seed, iw.workspace_id)
    seller_id = seller or iw.seller_entity_id
    contact_id, address = contact(
        seed, iw.workspace_id, twin, seller_id, evidence_kind="marketplace_relay_for_listing"
    )
    return InquiryWorld(
        iw.workspace_id,
        seed,
        twin,
        seller_id,
        contact_id,
        address,
        iw.authorization_id,
        iw.sender_binding_id,
        iw.controls_id,
    )


def _cooldown_elapsed(db_conn: psycopg.Connection, inquiry: uuid.UUID) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=30)
    arrange_inquiry_history(
        db_conn,
        inquiry,
        reserved_at=long_ago,
        queued_at=long_ago,
        send_attempted_at=long_ago,
        accepted_at=long_ago,
    )


def test_cluster_confirmed_after_a_send_cannot_reach_the_seller_again(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    first, _ = sent_inquiry(db_conn, iw)  # listing identity of site A, accepted
    _cooldown_elapsed(db_conn, first)
    twin = _twin_of(seed, iw)
    cluster = confirmed_cluster(seed, iw.workspace_id, [iw.listing_id, twin.listing_id])
    second = insert_inquiry(db_conn, twin, vehicle_kind="vehicle_cluster", cluster_id=cluster)
    with expect_sqlstate(SV_TRANSITION, ONE_PER_PAIR):
        reserve(db_conn, twin, second)
    assert state_of(db_conn, second) == "qualifying"


def test_unsent_listing_identity_must_be_cancelled_before_the_cluster_identity(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    first = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, first)
    queue(db_conn, iw, first)
    twin = _twin_of(seed, iw)
    cluster = confirmed_cluster(seed, iw.workspace_id, [iw.listing_id, twin.listing_id])
    merged = insert_inquiry(db_conn, twin, vehicle_kind="vehicle_cluster", cluster_id=cluster)
    with expect_sqlstate(SV_TRANSITION, ONE_PER_PAIR):
        reserve(db_conn, twin, merged)
    # The superseded listing-identity record can no longer be sent either.
    with expect_sqlstate("SV003", "must use the cluster"):
        dispatch(db_conn, iw, first)
    # Merge reconciliation: cancel the never-transmitted record (its debit is released).
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, first, state="cancelled", state_reasons=["IDENTITY_MERGED"])
        db_conn.execute(
            "update ops.inquiry_quota_ledger set released_at = now(), release_reason = 'IDENTITY_MERGED'"
            " where inquiry_id = %s and released_at is null",
            (first,),
        )
    reserve(db_conn, twin, merged)
    assert state_of(db_conn, merged) == "reserved"


def test_seller_merge_cannot_produce_a_second_inquiry_about_the_same_car(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    first, _ = sent_inquiry(db_conn, iw)
    _cooldown_elapsed(db_conn, first)
    survivor = seller_entity(seed, iw.workspace_id)
    db_conn.execute(
        "update app.seller_entities set merged_into_id = %s, merged_at = now(), merge_reason = 'same VAT id'"
        " where id = %s",
        (survivor, iw.seller_entity_id),
    )
    db_conn.execute(
        "update app.seller_contacts set status = 'changed', changed_at = now() where id = %s",
        (iw.contact_id,),
    )
    contact_id, address = contact(seed, iw.workspace_id, iw.vehicle, survivor)
    world = _world_for(iw, iw, survivor, contact_id, address)
    second = insert_inquiry(db_conn, world)
    with expect_sqlstate(SV_TRANSITION, ONE_PER_PAIR):
        reserve(db_conn, world, second)


def test_seller_merge_after_reservation_blocks_the_dispatch(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    """Two entities each reserved an inquiry about the same car; once they are known to be the
    same seller, the second one never leaves."""
    first, _ = sent_inquiry(db_conn, iw)
    _cooldown_elapsed(db_conn, first)
    other = seller_entity(seed, iw.workspace_id)
    db_conn.execute(
        "update app.seller_contacts set status = 'changed', changed_at = now() where id = %s",
        (iw.contact_id,),
    )
    contact_id, address = contact(seed, iw.workspace_id, iw.vehicle, other)
    world = _world_for(iw, iw, other, contact_id, address)
    second = insert_inquiry(db_conn, world)
    reserve(db_conn, world, second)  # a different seller entity at this point
    queue(db_conn, world, second)
    db_conn.execute(
        "update app.seller_entities set merged_into_id = %s, merged_at = now(),"
        " merge_reason = 'same legal entity'"
        " where id = %s",
        (other, iw.seller_entity_id),
    )
    with expect_sqlstate(SV_TRANSITION, ONE_PER_PAIR):
        dispatch(db_conn, world, second)
    assert _attempts(db_conn, iw.workspace_id) == 1


def test_plausible_cross_site_duplicate_is_held_until_resolved(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    first, _ = sent_inquiry(db_conn, iw)
    _cooldown_elapsed(db_conn, first)
    twin = _twin_of(seed, iw)
    unreviewed = seed.insert_id(
        "app.vehicle_clusters",
        workspace_id=iw.workspace_id,
        confidence="medium",
        match_basis={"synthetic": True},
    )
    for listing in (iw.listing_id, twin.listing_id):
        seed.insert(
            "app.vehicle_cluster_members",
            workspace_id=iw.workspace_id,
            cluster_id=unreviewed,
            listing_id=listing,
            confidence="medium",
        )
    second = insert_inquiry(db_conn, twin)  # listing identity: the cluster is not confirmed
    with expect_sqlstate(SV_TRANSITION, ONE_PER_PAIR):
        reserve(db_conn, twin, second)
    # Resolved as a different car: the second listing may be asked about.
    db_conn.execute(
        "update app.vehicle_clusters set review_status = 'rejected', reviewed_by = %s, reviewed_at = now(),"
        " row_version = row_version + 1 where id = %s",
        (uuid.uuid4(), unreviewed),
    )
    reserve(db_conn, twin, second)
    assert state_of(db_conn, second) == "reserved"


def test_other_cars_and_other_sellers_are_not_conflicts(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    """The one-per-pair rule is narrow: a different seller of the same car is another pair."""
    first, _ = sent_inquiry(db_conn, iw)
    _cooldown_elapsed(db_conn, first)
    other_seller = seller_entity(seed, iw.workspace_id)
    twin = _twin_of(seed, iw, seller=other_seller)
    cluster = confirmed_cluster(seed, iw.workspace_id, [iw.listing_id, twin.listing_id])
    pair = insert_inquiry(db_conn, twin, vehicle_kind="vehicle_cluster", cluster_id=cluster)
    reserve(db_conn, twin, pair)
    assert state_of(db_conn, pair) == "reserved"


def test_seller_merge_waits_for_a_reservation_of_the_survivor(
    db_url: str, db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    absorbed = seller_entity(seed, iw.workspace_id)
    inquiry = insert_inquiry(db_conn, iw)
    with psycopg.connect(db_url, autocommit=True) as other, other.transaction():
        reserve(other, iw, inquiry)  # uncommitted: holds the surviving seller row
        db_conn.execute("set lock_timeout = '300ms'")
        try:
            with expect_sqlstate("55P03"):
                db_conn.execute(
                    "update app.seller_entities set merged_into_id = %s, merged_at = now(),"
                    " merge_reason = 'same VAT id' where id = %s",
                    (iw.seller_entity_id, absorbed),
                )
        finally:
            db_conn.execute("reset lock_timeout")
    assert state_of(db_conn, inquiry) == "reserved"


# ---------------------------------------------------------------------------------------------
# Lifecycle timestamps are database-owned
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("column", ["reserved_at", "queued_at", "send_attempted_at"])
def test_backend_cannot_rewrite_lifecycle_timestamps(
    db_conn: psycopg.Connection, iw: InquiryWorld, column: str
) -> None:
    inquiry, _ = sent_inquiry(db_conn, iw)
    backdated = datetime.now(UTC) - timedelta(days=30)
    with expect_sqlstate("42501"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, **{column: backdated})
    with expect_sqlstate(SV_FROZEN, "maintained by the database"):
        update_inquiry(db_conn, inquiry, **{column: backdated})
    with expect_sqlstate(SV_FROZEN, "maintained by the database"):
        update_inquiry(db_conn, inquiry, **{column: None})


def test_lifecycle_timestamps_cannot_be_inserted(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    for column in ("reserved_at", "queued_at", "send_attempted_at", "accepted_at", "replied_at"):
        values: dict[str, Any] = {column: datetime.now(UTC)}
        with expect_sqlstate(SV_FROZEN, "maintained by the database"):
            insert_inquiry(db_conn, iw, **values)


def test_acceptance_time_is_set_only_by_its_transition(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    with expect_sqlstate(SV_FROZEN, "maintained by the database"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, accepted_at=datetime.now(UTC))
    attempt = dispatch(db_conn, iw, inquiry)
    provider_time = datetime.now(UTC) - timedelta(seconds=5)
    with backend(db_conn, iw.workspace_id):
        finish_attempt(db_conn, attempt, "accepted")
        update_inquiry(db_conn, inquiry, state="accepted", accepted_at=provider_time)
    row = db_conn.execute("select accepted_at from app.seller_inquiries where id = %s", (inquiry,)).fetchone()
    assert row == (provider_time,)
    with expect_sqlstate(SV_FROZEN, "maintained by the database"), backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, accepted_at=provider_time - timedelta(days=1))


def test_reservation_debit_and_attempt_helpers_stay_consistent(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    """Guard against fixture drift: the arranged send intent carries the inquiry's stable id."""
    inquiry = insert_inquiry(db_conn, iw)
    with backend(db_conn, iw.workspace_id):
        debit(db_conn, iw, inquiry)
        update_inquiry(db_conn, inquiry, state="reserved", **binding_values(iw))
    queue(db_conn, iw, inquiry)
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="sending")
        insert_attempt(db_conn, attempt_values(iw, inquiry))
    row = db_conn.execute(
        "select a.rfc_message_id like '<inquiry-%%@synthetic-mail.example>'"
        " from ops.email_delivery_attempts a"
        " where a.inquiry_id = %s",
        (inquiry,),
    ).fetchone()
    assert row == (True,)
