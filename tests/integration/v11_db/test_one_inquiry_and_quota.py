"""The one-inquiry rule, concurrent reservations and transactional rate caps (spec 37.5; 37.10).

- One car advertised on three sites by the same seller (three listings, three relay addresses)
  gets ONE inquiry: the identity is (workspace, confirmed cluster, seller entity, purpose).
- Two connections reserving concurrently: exactly one succeeds (identity uniqueness, the last
  quota slot, and the seller cooldown are all decided under database locks).
- Caps of 2 per rolling 24 h and 5 per rolling 15 days are ceilings enforced by the ledger;
  the owner can only reduce them; debits of (possibly) transmitted inquiries are retained.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from tests.integration.db.helpers import Seed, backend
from tests.integration.v11_db.support import (
    PURPOSE,
    SV_REFERENCE,
    SV_TRANSITION,
    InquiryWorld,
    binding_values,
    confirmed_cluster,
    contact,
    debit,
    dispatch,
    expect_sqlstate,
    identity_key,
    insert_inquiry,
    queue,
    race,
    reserve,
    seller_entity,
    single_success,
    state_of,
    update_inquiry,
    vehicle,
    with_vehicle,
)

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------------------------
# One inquiry per vehicle/seller across sites
# ---------------------------------------------------------------------------------------------


def _three_site_world(seed: Seed, iw: InquiryWorld) -> tuple[list[InquiryWorld], uuid.UUID]:
    """The same synthetic car on three sources, one seller, three different relay addresses."""
    worlds = [iw]
    for _ in range(2):
        veh = vehicle(seed, iw.workspace_id)
        contact_id, address = contact(
            seed,
            iw.workspace_id,
            veh,
            iw.seller_entity_id,
            evidence_kind="marketplace_relay_for_listing",
            address=f"relay-{uuid.uuid4().hex[:10]}@relay.synthetic-market.example",
        )
        assert address is not None
        worlds.append(
            InquiryWorld(
                iw.workspace_id,
                seed,
                veh,
                iw.seller_entity_id,
                contact_id,
                address,
                iw.authorization_id,
                iw.sender_binding_id,
                iw.controls_id,
            )
        )
    cluster = confirmed_cluster(seed, iw.workspace_id, [w.listing_id for w in worlds])
    return worlds, cluster


def test_three_listings_of_one_vehicle_and_seller_get_one_inquiry(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    worlds, cluster = _three_site_world(seed, iw)
    first = insert_inquiry(db_conn, worlds[0], vehicle_kind="vehicle_cluster", cluster_id=cluster)
    for other in worlds[1:]:
        # The same cluster identity, whichever listing/relay qualifies it: unique.
        with expect_sqlstate("23505", "seller_inquiries_identity"):
            insert_inquiry(db_conn, other, vehicle_kind="vehicle_cluster", cluster_id=cluster)
        # A listing-incarnation identity for a member of a confirmed cluster is refused outright.
        with expect_sqlstate(SV_REFERENCE, "must use the cluster"):
            insert_inquiry(db_conn, other)
    reserve(db_conn, worlds[0], first)
    queue(db_conn, worlds[0], first)
    dispatch(db_conn, worlds[0], first)
    rows = db_conn.execute(
        "select count(*) from app.seller_inquiries where workspace_id = %s and seller_entity_id = %s",
        (iw.workspace_id, iw.seller_entity_id),
    ).fetchone()
    assert rows == (1,)


def test_identity_components_are_unique_even_with_a_forged_hash(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    insert_inquiry(db_conn, iw)
    # A hash that does not match the identity components is refused by the CHECK...
    with expect_sqlstate("23514", "seller_inquiries_identity_key_ck"):
        insert_inquiry(db_conn, iw, identity_key="0" * 64)
    # ...and the components are unique on their own.
    key = identity_key(iw.workspace_id, "listing_incarnation", iw.listing_id, iw.seller_entity_id)
    with expect_sqlstate("23505"):
        insert_inquiry(db_conn, iw, identity_key=key)


def test_identity_key_matches_the_domain_for_both_vehicle_kinds(db_conn: psycopg.Connection) -> None:
    ws, vehicle_id, seller = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for kind in ("vehicle_cluster", "listing_incarnation"):
        row = db_conn.execute(
            "select app.seller_inquiry_identity_key(%s, %s, %s, %s, %s)",
            (ws, kind, vehicle_id, seller, PURPOSE),
        ).fetchone()
        assert row == (identity_key(ws, kind, vehicle_id, seller),)


def test_one_live_inquiry_per_listing_and_seller(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    """Before a cluster is confirmed, the listing identity is used; once confirmed, the old record
    must be cancelled (merge reconciliation) before the cluster identity can exist for the listing."""
    first = insert_inquiry(db_conn, iw)
    cluster = confirmed_cluster(seed, iw.workspace_id, [iw.listing_id])
    with expect_sqlstate("23505", "seller_inquiries_listing_seller_uidx"):
        insert_inquiry(db_conn, iw, vehicle_kind="vehicle_cluster", cluster_id=cluster)
    # The pending listing-identity record can no longer be reserved: its identity is not canonical.
    with expect_sqlstate(SV_REFERENCE, "must use the cluster"):
        reserve(db_conn, iw, first)
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, first, state="cancelled", state_reasons=["IDENTITY_MERGED"])
    merged = insert_inquiry(db_conn, iw, vehicle_kind="vehicle_cluster", cluster_id=cluster)
    reserve(db_conn, iw, merged)
    assert state_of(db_conn, merged) == "reserved"


def test_price_change_relisting_or_sender_change_does_not_reset_the_rule(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    dispatch(db_conn, iw, inquiry)
    # Whatever changes later, the same identity can never get a second record.
    with expect_sqlstate("23505"):
        insert_inquiry(db_conn, iw, state="candidate")


# ---------------------------------------------------------------------------------------------
# Concurrency: two connections, exactly one succeeds
# ---------------------------------------------------------------------------------------------


def test_concurrent_identity_reservations_exactly_one_succeeds(db_url: str, iw: InquiryWorld) -> None:
    def job(conn: psycopg.Connection) -> None:
        inquiry = insert_inquiry(conn, iw)
        reserve(conn, iw, inquiry)

    single_success(race(db_url, job, job), "23505")


def test_concurrent_reservations_cannot_both_take_the_last_quota_slot(
    db_url: str, db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    # One slot of the 24 h ceiling is already used by an earlier inquiry to another seller.
    earlier = with_vehicle(iw, seller=seller_entity(iw.seed, iw.workspace_id))
    used = insert_inquiry(db_conn, earlier)
    reserve(db_conn, earlier, used)
    racers = [with_vehicle(iw, seller=seller_entity(iw.seed, iw.workspace_id)) for _ in range(2)]
    inquiries = [insert_inquiry(db_conn, world) for world in racers]

    def job_for(world: InquiryWorld, inquiry: uuid.UUID) -> Callable[[psycopg.Connection], object]:
        return lambda conn: reserve(conn, world, inquiry)

    results = race(db_url, *(job_for(w, i) for w, i in zip(racers, inquiries, strict=True)))
    single_success(results, SV_TRANSITION)
    assert "cap reached" in str(next(r for r in results if r is not None))
    reserved = db_conn.execute(
        "select count(*) from app.seller_inquiries where workspace_id = %s and state = 'reserved'",
        (iw.workspace_id,),
    ).fetchone()
    assert reserved == (2,)


def test_concurrent_reservations_to_one_seller_respect_the_cooldown(
    db_url: str, db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    second = with_vehicle(iw)  # same seller entity, another car
    first_inquiry = insert_inquiry(db_conn, iw)
    second_inquiry = insert_inquiry(db_conn, second)
    results = race(
        db_url,
        lambda conn: reserve(conn, iw, first_inquiry),
        lambda conn: reserve(conn, second, second_inquiry),
    )
    single_success(results, SV_TRANSITION)
    assert "seller cooldown" in str(next(r for r in results if r is not None))


def test_concurrent_dispatch_of_one_inquiry_sends_once(
    db_url: str, db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)

    def job(conn: psycopg.Connection) -> None:
        dispatch(conn, iw, inquiry)

    results = race(db_url, job, job)
    assert sum(r is None for r in results) == 1, results
    attempts = db_conn.execute(
        "select count(*) from ops.email_delivery_attempts where inquiry_id = %s", (inquiry,)
    ).fetchone()
    assert attempts == (1,)


def test_pause_racing_a_dispatch_is_serialised(
    db_url: str, db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    """A dispatch holds the control row FOR UPDATE: a concurrent pause waits or is seen."""
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)

    def pause(conn: psycopg.Connection) -> None:
        with backend(conn, iw.workspace_id):
            conn.execute(
                "update app.seller_inquiry_controls set kill_switch = true, kill_switch_reason ="
                " 'race pause',"
                " kill_switch_set_at = now(), kill_switch_set_by = %s, version = version + 1"
                " where workspace_id = %s",
                (uuid.uuid4(), iw.workspace_id),
            )

    results = race(db_url, lambda conn: dispatch(conn, iw, inquiry), pause)
    assert results[1] is None
    state = state_of(db_conn, inquiry)
    if results[0] is None:
        assert state == "sending"  # committed its send intent before the pause
    else:
        assert isinstance(results[0], psycopg.Error) and results[0].sqlstate == SV_TRANSITION
        assert state == "queued"


# ---------------------------------------------------------------------------------------------
# Rate caps (rolling windows, ceilings, retained debits)
# ---------------------------------------------------------------------------------------------


def _backdated_debit(db_conn: psycopg.Connection, iw: InquiryWorld, age: timedelta) -> uuid.UUID:
    """Arrange a historical debit (documented owner maintenance path; never available to suv_backend)."""
    world = with_vehicle(iw, seller=seller_entity(iw.seed, iw.workspace_id))
    inquiry = insert_inquiry(db_conn, world)
    with db_conn.transaction():
        db_conn.execute("select set_config('app.history_maintenance', 'on', true)")
        db_conn.execute(
            "insert into ops.inquiry_quota_ledger (workspace_id, inquiry_id, debited_at) values (%s, %s, %s)",
            (iw.workspace_id, inquiry, datetime.now(UTC) - age),
        )
    return inquiry


def _fresh_reservation(db_conn: psycopg.Connection, iw: InquiryWorld) -> tuple[InquiryWorld, uuid.UUID]:
    world = with_vehicle(iw, seller=seller_entity(iw.seed, iw.workspace_id))
    return world, insert_inquiry(db_conn, world)


def test_two_per_rolling_24_hours(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    _backdated_debit(db_conn, iw, timedelta(hours=23))
    _backdated_debit(db_conn, iw, timedelta(hours=1))
    world, inquiry = _fresh_reservation(db_conn, iw)
    with expect_sqlstate(SV_TRANSITION, "2 per rolling 24 hours"):
        reserve(db_conn, world, inquiry)


def test_debit_older_than_24_hours_leaves_the_daily_window(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    _backdated_debit(db_conn, iw, timedelta(hours=24, minutes=1))
    _backdated_debit(db_conn, iw, timedelta(hours=2))
    world, inquiry = _fresh_reservation(db_conn, iw)
    reserve(db_conn, world, inquiry)
    assert state_of(db_conn, inquiry) == "reserved"


def test_five_per_rolling_15_days(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    for days in (14, 10, 6, 3, 2):
        _backdated_debit(db_conn, iw, timedelta(days=days))
    world, inquiry = _fresh_reservation(db_conn, iw)
    with expect_sqlstate(SV_TRANSITION, "5 per rolling 15 days"):
        reserve(db_conn, world, inquiry)


def test_fifteen_day_window_expires(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    for days in (15, 10, 6, 3):
        _backdated_debit(db_conn, iw, timedelta(days=days, minutes=1))
    world, inquiry = _fresh_reservation(db_conn, iw)
    reserve(db_conn, world, inquiry)


def test_future_dated_debits_still_count(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    _backdated_debit(db_conn, iw, timedelta(hours=-3))
    _backdated_debit(db_conn, iw, timedelta(hours=-1))
    world, inquiry = _fresh_reservation(db_conn, iw)
    with expect_sqlstate(SV_TRANSITION, "cap reached"):
        reserve(db_conn, world, inquiry)


def test_released_debits_do_not_count_but_retained_ones_do(
    db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    # Two never-sent reservations cancelled and released: the slots come back.
    for _ in range(2):
        world, inquiry = _fresh_reservation(db_conn, iw)
        reserve(db_conn, world, inquiry)
        with backend(db_conn, iw.workspace_id):
            update_inquiry(db_conn, inquiry, state="cancelled")
            db_conn.execute(
                "update ops.inquiry_quota_ledger set released_at = now(), release_reason = 'CANCELLED_UNSENT'"
                " where inquiry_id = %s",
                (inquiry,),
            )
    world, inquiry = _fresh_reservation(db_conn, iw)
    reserve(db_conn, world, inquiry)
    queue(db_conn, world, inquiry)
    dispatch(db_conn, world, inquiry)
    world2, inquiry2 = _fresh_reservation(db_conn, iw)
    reserve(db_conn, world2, inquiry2)
    # Both slots are now held (one by a possibly transmitted send): the third is refused.
    world3, inquiry3 = _fresh_reservation(db_conn, iw)
    with expect_sqlstate(SV_TRANSITION, "cap reached"):
        reserve(db_conn, world3, inquiry3)


@pytest.mark.parametrize(("max_24h", "max_15d"), [(1, 5), (2, 1), (0, 5), (2, 0)])
def test_owner_can_reduce_or_pause_the_caps(
    db_conn: psycopg.Connection, iw: InquiryWorld, max_24h: int, max_15d: int
) -> None:
    db_conn.execute(
        "update app.seller_inquiry_controls set max_per_24h = %s, max_per_15d = %s, version = version + 1"
        " where workspace_id = %s",
        (max_24h, max_15d, iw.workspace_id),
    )
    if min(max_24h, max_15d) > 0:
        world, inquiry = _fresh_reservation(db_conn, iw)
        reserve(db_conn, world, inquiry)
    world, inquiry = _fresh_reservation(db_conn, iw)
    with expect_sqlstate(SV_TRANSITION, "cap reached"):
        reserve(db_conn, world, inquiry)


def test_backend_cannot_backdate_a_debit(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    world, inquiry = _fresh_reservation(db_conn, iw)
    with expect_sqlstate(SV_TRANSITION, "backdated"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "insert into ops.inquiry_quota_ledger (workspace_id, inquiry_id, debited_at) values (%s, %s, %s)",
            (iw.workspace_id, inquiry, datetime.now(UTC) - timedelta(days=20)),
        )
    # Maintenance mode is owner-only: the GUC does not help suv_backend.
    with expect_sqlstate(SV_TRANSITION, "backdated"), backend(db_conn, iw.workspace_id):
        db_conn.execute("select set_config('app.history_maintenance', 'on', true)")
        db_conn.execute(
            "insert into ops.inquiry_quota_ledger (workspace_id, inquiry_id, debited_at) values (%s, %s, %s)",
            (iw.workspace_id, inquiry, datetime.now(UTC) - timedelta(days=20)),
        )
    assert world.workspace_id == iw.workspace_id


def test_quota_debits_are_retained_for_uncertain_sends(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = insert_inquiry(db_conn, iw)
    reserve(db_conn, iw, inquiry)
    queue(db_conn, iw, inquiry)
    attempt = dispatch(db_conn, iw, inquiry)
    with backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.email_delivery_attempts set outcome = 'uncertain', finished_at = now() where id = %s",
            (attempt,),
        )
        update_inquiry(db_conn, inquiry, state="uncertain")
    with expect_sqlstate(SV_TRANSITION, "never-transmitted"), backend(db_conn, iw.workspace_id):
        db_conn.execute(
            "update ops.inquiry_quota_ledger set released_at = now(), release_reason = 'UNCERTAIN'"
            " where inquiry_id = %s",
            (inquiry,),
        )
    with expect_sqlstate("42501"), backend(db_conn, iw.workspace_id):
        db_conn.execute("delete from ops.inquiry_quota_ledger where inquiry_id = %s", (inquiry,))
    with expect_sqlstate("SV001"):
        db_conn.execute("delete from ops.inquiry_quota_ledger where inquiry_id = %s", (inquiry,))
    held = db_conn.execute(
        "select count(*) from ops.inquiry_quota_ledger where inquiry_id = %s and released_at is null",
        (inquiry,),
    ).fetchone()
    assert held == (1,)


def test_binding_values_bind_any_supported_language(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    """Guard against fixture drift: the arranged binding is a valid pre-reservation update."""
    inquiry = insert_inquiry(db_conn, iw)
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, **binding_values(iw))
    with backend(db_conn, iw.workspace_id):
        debit(db_conn, iw, inquiry)
        update_inquiry(db_conn, inquiry, state="reserved")
    assert state_of(db_conn, inquiry) == "reserved"
