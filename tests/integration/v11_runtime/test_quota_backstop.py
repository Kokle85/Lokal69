"""Migration 20261008000100: the database cap backstop counts like the repository (spec 37.5).

``ops.inquiry_quota_usage`` counts every unreleased debit at the LATEST moment its message may
have been handed over: the later of the reservation, the send attempt, the end of every finished
attempt and -- while an attempt still runs (an ``outlook_local`` intent the desktop worker may
claim until it expires) -- now. So intents committed while the laptop was offline can never leave
together with later ones above the caps, also when the repository's own check is bypassed. The
guard semantics (checked on every ledger insert and before every transmission, under the
controls lock; released debits never count) are unchanged.

Arrangement uses the superuser connection (``db_conn``) and the raw-SQL builders of the v1.1
database tests; elapsed time is arranged only with triggers bypassed for one statement.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from tests.integration.db.helpers import Seed, backend
from tests.integration.v11_db.support import (
    SV_TRANSITION,
    InquiryWorld,
    arrange_inquiry_history,
    dispatch,
    expect_sqlstate,
    finish_attempt,
    inquiry_world,
    insert_inquiry,
    queue,
    reserve,
    reserve_with_debit_at,
    seller_entity,
    state_of,
    update_inquiry,
    with_vehicle,
)

from suv_deals.domain.actor import ActorContext
from suv_deals.persistence import inquiries_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db

CAP_AT_DISPATCH = "cap reached at dispatch"


@pytest.fixture
def iw(seed: Seed) -> InquiryWorld:
    return inquiry_world(seed, "B2a quota backstop")


@pytest.fixture
async def db(db_url: str) -> AsyncIterator[Database]:
    database = Database(db_url, set_role="suv_backend", min_size=1, max_size=2)
    await database.open()
    try:
        yield database
    finally:
        await database.close()


def _usage(conn: psycopg.Connection, workspace_id: uuid.UUID) -> tuple[int, int]:
    row = conn.execute("select * from ops.inquiry_quota_usage(%s, null)", (workspace_id,)).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def _other_seller(conn: psycopg.Connection, iw: InquiryWorld) -> tuple[InquiryWorld, uuid.UUID]:
    world = with_vehicle(iw, seller=seller_entity(iw.seed, iw.workspace_id))
    return world, insert_inquiry(conn, world)


def _age_attempts(conn: psycopg.Connection, inquiry_id: uuid.UUID, at: datetime) -> None:
    """TEST ARRANGEMENT: the inquiry's attempts were committed at ``at``."""
    with conn.transaction():
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "update ops.email_delivery_attempts set send_intent_committed_at = %s where inquiry_id = %s",
            (at, inquiry_id),
        )


def _dispatched_days_ago(
    conn: psycopg.Connection, iw: InquiryWorld, days: int, *, outcome: str | None
) -> uuid.UUID:
    """An inquiry reserved and dispatched ``days`` ago. ``outcome=None``: its attempt is still
    running (an intent not yet claimed); ``"uncertain"``: the attempt ended NOW."""
    then = datetime.now(UTC) - timedelta(days=days)
    world, inquiry = _other_seller(conn, iw)
    reserve_with_debit_at(conn, world, inquiry, then)
    queue(conn, world, inquiry)
    attempt = dispatch(conn, world, inquiry)
    if outcome is not None:
        with backend(conn, iw.workspace_id):
            finish_attempt(conn, attempt, outcome)
            assert update_inquiry(conn, inquiry, state=outcome) == 1
    arrange_inquiry_history(conn, inquiry, reserved_at=then, queued_at=then, send_attempted_at=then)
    _age_attempts(conn, inquiry, then)
    return inquiry


def test_function_keeps_signature_grants_and_search_path(db_conn: psycopg.Connection) -> None:
    row = db_conn.execute(
        "select p.provolatile, p.prosecdef, p.proconfig, pg_catalog.pg_get_function_identity_arguments(p.oid)"
        " from pg_catalog.pg_proc p join pg_catalog.pg_namespace n on n.oid = p.pronamespace"
        " where n.nspname = 'ops' and p.proname = 'inquiry_quota_usage'"
    ).fetchall()
    assert len(row) == 1  # replaced in place, never overloaded
    volatility, definer, config, args = row[0]
    assert volatility == "v" and definer is False
    assert config == ['search_path=""'] or config == ['search_path=""']
    assert args == (
        "p_workspace_id uuid, p_exclude_inquiry_id uuid, OUT count_24h integer, OUT count_15d integer"
    )
    for role, allowed in (("suv_backend", True), ("anon", False), ("authenticated", False)):
        granted = db_conn.execute(
            "select pg_catalog.has_function_privilege(%s, 'ops.inquiry_quota_usage(uuid, uuid)', 'EXECUTE')",
            (role,),
        ).fetchone()
        assert granted == (allowed,), role


def test_a_running_attempt_counts_now(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    """Reserved and dispatched three days ago, but the intent is still unclaimed: the message may
    leave at any moment, so it occupies today's window."""
    _dispatched_days_ago(db_conn, iw, 3, outcome=None)
    assert _usage(db_conn, iw.workspace_id) == (1, 1)


def test_an_attempt_that_ended_now_counts_now(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    """The worker handed the message over only now (late claim / guarded retry): it counts now,
    not at the reservation or the first send attempt three days ago."""
    _dispatched_days_ago(db_conn, iw, 3, outcome="uncertain")
    assert _usage(db_conn, iw.workspace_id) == (1, 1)


def test_old_finished_attempts_leave_the_daily_window(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    inquiry = _dispatched_days_ago(db_conn, iw, 3, outcome="uncertain")
    then = datetime.now(UTC) - timedelta(days=3)
    with db_conn.transaction():
        db_conn.execute("set local session_replication_role = replica")
        db_conn.execute(
            "update ops.email_delivery_attempts set finished_at = %s where inquiry_id = %s", (then, inquiry)
        )
    assert _usage(db_conn, iw.workspace_id) == (0, 1)


def test_released_debits_still_never_count(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    world, inquiry = _other_seller(db_conn, iw)
    reserve(db_conn, world, inquiry)
    assert _usage(db_conn, iw.workspace_id) == (1, 1)
    with backend(db_conn, iw.workspace_id):
        update_inquiry(db_conn, inquiry, state="cancelled")
        db_conn.execute(
            "update ops.inquiry_quota_ledger set released_at = now(), release_reason = 'CANCELLED_UNSENT'"
            " where inquiry_id = %s",
            (inquiry,),
        )
    assert _usage(db_conn, iw.workspace_id) == (0, 0)


def test_the_guard_refuses_a_transmission_over_the_cap(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    """Two intents committed three days ago were handed over only now; a third queued inquiry
    must not leave within the same rolling 24 hours (the backstop under the controls lock)."""
    world, third = _other_seller(db_conn, iw)
    reserve_with_debit_at(db_conn, world, third, datetime.now(UTC) - timedelta(days=2))
    queue(db_conn, world, third)
    _dispatched_days_ago(db_conn, iw, 3, outcome="uncertain")
    _dispatched_days_ago(db_conn, iw, 3, outcome=None)
    assert _usage(db_conn, iw.workspace_id) == (2, 3)
    with expect_sqlstate(SV_TRANSITION, CAP_AT_DISPATCH):
        dispatch(db_conn, world, third)
    assert state_of(db_conn, third) == "queued"  # held, never lost
    # A new reservation is refused as well (the ledger-insert check).
    other, fourth = _other_seller(db_conn, iw)
    with expect_sqlstate(SV_TRANSITION, "2 per rolling 24 hours"):
        reserve(db_conn, other, fourth)


async def test_backstop_and_repository_count_alike(
    db: Database, db_conn: psycopg.Connection, iw: InquiryWorld
) -> None:
    world, reserved = _other_seller(db_conn, iw)
    reserve_with_debit_at(db_conn, world, reserved, datetime.now(UTC) - timedelta(days=5))
    _dispatched_days_ago(db_conn, iw, 3, outcome="uncertain")
    _dispatched_days_ago(db_conn, iw, 4, outcome=None)
    actor = ActorContext.system(iw.workspace_id, request_id="b2a-backstop")
    async with unit_of_work(db, actor) as conn:
        repository = await inquiries_repo.quota_usage(conn, actor)
    assert (repository.count_24h, repository.count_15d) == _usage(db_conn, iw.workspace_id) == (2, 3)
