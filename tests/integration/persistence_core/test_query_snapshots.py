"""Frozen query snapshots + signed cursors on real PostgreSQL (spec 21 "Pagination and errors").

The review queue is paged from a frozen snapshot: reprioritisation, status changes,
insertions and deletions between pages do not reorder, duplicate or drop results.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed, World, sha, unique
from tests.integration.persistence_core.support import member, system

from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role
from suv_deals.domain.pagination import filter_hash
from suv_deals.errors import Forbidden, ValidationFailed
from suv_deals.persistence import query_snapshots
from suv_deals.persistence.database import Conn, Database
from suv_deals.persistence.query_snapshots import CursorPage

pytestmark = pytest.mark.db

SECRET = b"synthetic-cursor-secret-for-tests-0123456789"
QUERY = "reviews_list_pending"
FILTERS = {"profile": "primary", "state": ["pending", "claimed"]}


def _case(seed: Seed, world: World, priority: int) -> UUID:
    listing = seed.listing(world.workspace_id, world.source_id)
    revision = seed.revision(world.workspace_id, listing, 1)
    return seed.review_case(world.workspace_id, listing, revision, priority=priority)


async def _queue(conn: Conn, actor: ActorContext) -> tuple[list[UUID], list[dict[str, Any]]]:
    cur = await conn.execute(
        "select id, priority, state, row_version from app.review_cases"
        " where workspace_id = %s and state in ('pending', 'claimed')"
        " order by priority desc, created_at, id",
        (actor.workspace_id,),
    )
    rows = await cur.fetchall()
    ids = [r["id"] for r in rows]
    projections = [
        {"case_id": str(r["id"]), "priority": r["priority"], "state": r["state"], "row_version": r["row_version"]}
        for r in rows
    ]
    return ids, projections


async def _start(db: Database, actor: ActorContext, limit: int = 3) -> CursorPage:
    async with db.transaction(actor) as conn:
        ids, projections = await _queue(conn, actor)
        return await query_snapshots.start_listing(
            conn,
            actor,
            query_name=QUERY,
            filter_hash=filter_hash(FILTERS),
            ordered_ids=ids,
            projections=projections,
            limit=limit,
            secret=SECRET,
        )


async def _next(db: Database, actor: ActorContext, cursor: str, *, filters: dict[str, Any] | None = None) -> CursorPage:
    async with db.transaction(actor) as conn:
        return await query_snapshots.next_page(
            conn,
            actor,
            cursor=cursor,
            query_name=QUERY,
            filter_hash=filter_hash(filters or FILTERS),
            limit=3,
            secret=SECRET,
        )


async def test_pages_are_stable_under_reprioritisation_status_changes_inserts_and_deletes(
    db: Database, world_a: World, seed: Seed
) -> None:
    ws = world_a.workspace_id
    cases = {p: _case(seed, world_a, p) for p in (70, 60, 50, 40, 30, 20, 10)}
    reviewer = member(ws, Role.REVIEWER)
    first = await _start(db, reviewer)
    assert list(first.page.ids) == [cases[70], cases[60], cases[50]]
    assert first.page.total == 7 and first.page.next_ordinal == 3 and first.next_cursor is not None
    # Between pages: reprioritise, change status, insert and delete.
    seed.conn.execute("update app.review_cases set priority = 1000, row_version = row_version + 1 where id = %s", (cases[10],))
    seed.conn.execute(
        "update app.review_cases set state = 'claimed', claim_holder = %s, claim_token_hash = %s,"
        " claimed_at = now(), claim_expires_at = now() + interval '5 minutes', row_version = row_version + 1"
        " where id = %s",
        (uuid.uuid4(), sha(unique("claim")), cases[40]),
    )
    inserted = _case(seed, world_a, 65)
    seed.conn.execute("delete from app.review_cases where id = %s", (cases[30],))
    second = await _next(db, reviewer, first.next_cursor)
    assert list(second.page.ids) == [cases[40], cases[30], cases[20]]
    # Projections are the frozen display values; claim/submit revalidate the live row.
    assert [p["state"] for p in second.page.projections] == ["pending", "pending", "pending"]
    assert second.next_cursor is not None
    third = await _next(db, reviewer, second.next_cursor)
    assert list(third.page.ids) == [cases[10]] and third.next_cursor is None
    assert third.page.projections[0]["priority"] == 10
    seen = list(first.page.ids) + list(second.page.ids) + list(third.page.ids)
    assert seen == [cases[p] for p in (70, 60, 50, 40, 30, 20, 10)]
    assert inserted not in seen
    # Re-querying shows the changes.
    fresh = await _start(db, reviewer, limit=10)
    assert list(fresh.page.ids)[:3] == [cases[10], cases[70], inserted]
    assert cases[30] not in fresh.page.ids and cases[40] in fresh.page.ids


async def test_snapshot_is_bound_to_principal_workspace_query_and_filters(
    db: Database, world_a: World, world_b: World, seed: Seed
) -> None:
    for p in (5, 4, 3, 2):
        _case(seed, world_a, p)
    principal = uuid.uuid4()
    reviewer = member(world_a.workspace_id, Role.REVIEWER, principal_id=principal)
    first = await _start(db, reviewer, limit=2)
    assert first.next_cursor is not None
    cursor = first.next_cursor
    other_principal = member(world_a.workspace_id, Role.REVIEWER)
    same_principal_other_workspace = member(world_b.workspace_id, Role.REVIEWER, principal_id=principal)
    for actor in (other_principal, same_principal_other_workspace):
        with pytest.raises(ValidationFailed):
            await _next(db, actor, cursor)
    with pytest.raises(ValidationFailed):
        await _next(db, reviewer, cursor, filters={"profile": "manual_4000"})
    tampered = cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB")
    with pytest.raises(ValidationFailed):
        await _next(db, reviewer, tampered)
    # The raw page API applies the same binding (no cursor involved).
    snapshot_id = first.page.snapshot_id
    for actor in (other_principal, same_principal_other_workspace):
        async with db.transaction(actor) as conn:
            with pytest.raises(ValidationFailed):
                await query_snapshots.page(
                    conn, actor, snapshot_id, 2, 2, query_name=QUERY, filter_hash=filter_hash(FILTERS)
                )
    async with db.transaction(reviewer) as conn:
        with pytest.raises(ValidationFailed):
            await query_snapshots.page(
                conn, reviewer, snapshot_id, 2, 2, query_name="deals_list_candidates", filter_hash=filter_hash(FILTERS)
            )
        ok = await query_snapshots.page(
            conn, reviewer, snapshot_id, 2, 2, query_name=QUERY, filter_hash=filter_hash(FILTERS)
        )
    assert ok.total == 4 and len(ok.ids) == 2 and ok.next_ordinal is None
    # The legitimate owner of the cursor still gets the next page.
    assert len((await _next(db, reviewer, cursor)).page.ids) == 2


async def test_snapshots_expire_and_are_purged(db: Database, world_a: World, seed: Seed) -> None:
    ws = world_a.workspace_id
    reviewer = member(ws, Role.REVIEWER)
    ids = [uuid.uuid4() for _ in range(3)]
    async with db.transaction(reviewer) as conn:
        snapshot_id = await query_snapshots.create_snapshot(
            conn, reviewer, filter_hash(FILTERS), ids, [], timedelta(minutes=5), query_name=QUERY
        )
        page = await query_snapshots.page(
            conn, reviewer, snapshot_id, 0, 10, query_name=QUERY, filter_hash=filter_hash(FILTERS)
        )
    assert list(page.ids) == ids and page.projections == () and page.next_ordinal is None
    seed.conn.execute(
        "update ops.query_snapshots set created_at = now() - interval '2 hours',"
        " expires_at = now() - interval '1 hour' where id = %s",
        (snapshot_id,),
    )
    async with db.transaction(reviewer) as conn:
        with pytest.raises(ValidationFailed):
            await query_snapshots.page(
                conn, reviewer, snapshot_id, 0, 10, query_name=QUERY, filter_hash=filter_hash(FILTERS)
            )
        with pytest.raises(Forbidden):
            await query_snapshots.delete_expired(conn, reviewer)
    async with db.transaction(system(ws)) as conn:
        assert await query_snapshots.delete_expired(conn, system(ws)) == 1
    assert seed.scalar("select count(*) from ops.query_snapshots where id = %s", (snapshot_id,)) == 0


async def test_snapshot_input_validation(db: Database, world_a: World) -> None:
    reviewer = member(world_a.workspace_id, Role.REVIEWER)
    duplicate = uuid.uuid4()
    async with db.transaction(reviewer) as conn:
        with pytest.raises(ValidationFailed):
            await query_snapshots.create_snapshot(
                conn, reviewer, filter_hash(FILTERS), [duplicate, duplicate], [], query_name=QUERY
            )
        with pytest.raises(ValidationFailed):
            await query_snapshots.create_snapshot(
                conn, reviewer, filter_hash(FILTERS), [uuid.uuid4()], [{}, {}], query_name=QUERY
            )
        with pytest.raises(ValidationFailed):
            await query_snapshots.create_snapshot(
                conn, reviewer, filter_hash(FILTERS), [], [], timedelta(days=2), query_name=QUERY
            )
        with pytest.raises(ValidationFailed):
            await query_snapshots.create_snapshot(conn, reviewer, "nothex", [], [], query_name=QUERY)
        empty = await query_snapshots.create_snapshot(conn, reviewer, filter_hash(FILTERS), [], [], query_name=QUERY)
        page = await query_snapshots.page(
            conn, reviewer, empty, 0, None, query_name=QUERY, filter_hash=filter_hash(FILTERS)
        )
        assert page.total == 0 and page.ids == () and page.next_ordinal is None
        with pytest.raises(ValidationFailed):
            await query_snapshots.page(
                conn, reviewer, empty, 0, 101, query_name=QUERY, filter_hash=filter_hash(FILTERS)
            )
