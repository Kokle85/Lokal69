"""Private notes and watchlists (spec 11, 21 ``deals_add_note``). SYNTHETIC data, ``suv_backend``."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.repos_valuation_reviews.builders import (
    RealWorld,
    idem_key,
    make_listing,
    open_case,
    owner,
    reviewer,
    run,
    system,
    viewer,
)

from suv_deals.errors import Forbidden, IdempotencyConflict, NotFound, ValidationFailed
from suv_deals.persistence import notes_repo
from suv_deals.persistence.database import Database

pytestmark = pytest.mark.db

# Columns that satisfy ``sources_enable_gate_ck`` (SYNTHETIC host and paths).
_FETCHABLE: dict[str, Any] = {
    "adapter_version": "1.0.0",
    "technical_status": "fixture_tested",
    "terms_status": "permitted",
    "terms_decision": "proceed_permitted",
    "terms_decision_actor": "owner (synthetic)",
    "allowed_hosts": ["synthetic-dealer.example"],
    "allowed_search_paths": ["^/search$"],
    "allowed_detail_paths": ["^/vehicles/[A-Z0-9-]+$"],
}


def _make_fetchable(seed: Seed, ws: UUID, source_id: UUID) -> None:
    """Enable an existing SYNTHETIC source so it may make requests (every activation gate met)."""
    sets = ", ".join(f"{column} = %({column})s" for column in _FETCHABLE)
    seed.conn.execute(
        f"update app.sources set {sets}, enabled = true where workspace_id = %(ws)s and id = %(id)s",
        {**_FETCHABLE, "ws": ws, "id": source_id},
    )


async def test_notes_are_labelled_by_the_authenticated_principal(db: Database, world: RealWorld) -> None:
    ws = world.workspace_id
    assistant = reviewer(ws)  # an MCP client
    member = reviewer(ws, kind="user")
    own = owner(ws)
    labels = []
    for actor in (assistant, member, own):
        note = await run(
            db,
            actor,
            lambda c, a=actor: notes_repo.add_note(
                c, a, world.listing_id, "  SYNTHETIC note text  ", idem_key()
            ),
        )
        labels.append((note.label, note.author_kind, note.author_principal_id, note.body))
    assert labels == [
        ("assistant", "mcp_client", assistant.principal_id, "SYNTHETIC note text"),
        ("reviewer", "user", member.principal_id, "SYNTHETIC note text"),
        ("owner", "user", own.principal_id, "SYNTHETIC note text"),
    ]
    listed = await run(db, viewer(ws), lambda c: notes_repo.list_notes(c, viewer(ws), world.listing_id))
    assert [n.label for n in listed] == ["owner", "reviewer", "assistant"]  # newest first


async def test_add_note_is_idempotent_and_validated(db: Database, seed: Seed, world: RealWorld) -> None:
    ws = world.workspace_id
    actor = reviewer(ws)
    key = idem_key()
    first = await run(
        db, actor, lambda c: notes_repo.add_note(c, actor, world.listing_id, "SYNTHETIC first", key)
    )
    again = await run(
        db, actor, lambda c: notes_repo.add_note(c, actor, world.listing_id, "SYNTHETIC first", key)
    )
    assert again == first
    assert seed.scalar("select count(*) from app.owner_notes where listing_id = %s", (world.listing_id,)) == 1
    with pytest.raises(IdempotencyConflict):
        await run(
            db, actor, lambda c: notes_repo.add_note(c, actor, world.listing_id, "SYNTHETIC other", key)
        )
    for bad in ("   ", "x" * 4001, "SYNTHETIC" + chr(0x202E) + "override", "bell\x07"):
        with pytest.raises(ValidationFailed):
            await run(
                db, actor, lambda c, b=bad: notes_repo.add_note(c, actor, world.listing_id, b, idem_key())
            )
    reader = viewer(ws)
    with pytest.raises(Forbidden):
        await run(
            db, reader, lambda c: notes_repo.add_note(c, reader, world.listing_id, "SYNTHETIC", idem_key())
        )
    with pytest.raises(NotFound):
        await run(db, actor, lambda c: notes_repo.add_note(c, actor, uuid.uuid4(), "SYNTHETIC", idem_key()))
    # A case of ANOTHER listing cannot be attached (composite reference).
    other_listing, other_rev = make_listing(seed, ws, world.source_id)
    case = await open_case(db, world, listing_id=other_listing, revision_id=other_rev)
    with pytest.raises(NotFound):
        await run(
            db,
            actor,
            lambda c: notes_repo.add_note(
                c, actor, world.listing_id, "SYNTHETIC", idem_key(), case_id=case.case_id
            ),
        )
    attached = await run(
        db,
        actor,
        lambda c: notes_repo.add_note(
            c, actor, other_listing, "SYNTHETIC on case", idem_key(), case_id=case.case_id
        ),
    )
    assert attached.case_id == case.case_id


async def test_notes_are_workspace_isolated(db: Database, world: RealWorld, other_world: RealWorld) -> None:
    actor = reviewer(world.workspace_id)
    await run(db, actor, lambda c: notes_repo.add_note(c, actor, world.listing_id, "SYNTHETIC", idem_key()))
    foreign = reviewer(other_world.workspace_id)
    assert await run(db, foreign, lambda c: notes_repo.list_notes(c, foreign, world.listing_id)) == []
    with pytest.raises(NotFound):
        await run(
            db, foreign, lambda c: notes_repo.add_note(c, foreign, world.listing_id, "SYNTHETIC", idem_key())
        )


async def test_watchlist_lifecycle_and_recheck_frequency(db: Database, seed: Seed, world: RealWorld) -> None:
    ws = world.workspace_id
    actor = reviewer(ws, kind="user")
    watch = await run(
        db,
        actor,
        lambda c: notes_repo.add_watch(
            c, actor, world.listing_id, reason="SYNTHETIC watch", recheck_interval=timedelta(hours=6)
        ),
    )
    assert watch.active and watch.row_version == 1 and watch.recheck_interval == timedelta(hours=6)
    assert watch.next_recheck_at is not None
    assert (
        timedelta(hours=5, minutes=59)
        < watch.next_recheck_at - watch.created_at
        < timedelta(hours=6, minutes=1)
    )
    updated = await run(
        db,
        actor,
        lambda c: notes_repo.add_watch(
            c, actor, world.listing_id, reason="SYNTHETIC weekly", recheck_interval=timedelta(days=7)
        ),
    )
    assert updated.id == watch.id and updated.row_version == 2 and updated.reason == "SYNTHETIC weekly"
    for interval in (timedelta(minutes=30), timedelta(days=31)):
        with pytest.raises(ValidationFailed):
            await run(
                db,
                actor,
                lambda c, i=interval: notes_repo.add_watch(
                    c, actor, world.listing_id, reason="SYNTHETIC", recheck_interval=i
                ),
            )
    other = reviewer(ws, kind="user")
    await run(
        db, other, lambda c: notes_repo.add_watch(c, other, world.listing_id, reason="SYNTHETIC other member")
    )
    mine = await run(db, actor, lambda c: notes_repo.list_watches(c, actor, mine_only=True))
    assert [w.id for w in mine] == [watch.id]
    everyone = await run(db, actor, lambda c: notes_repo.list_watches(c, actor, listing_id=world.listing_id))
    assert len(everyone) == 2
    # The scheduler sees due watches by database time and advances them (only on a source that
    # may make requests: the synthetic source starts disabled).
    _make_fetchable(seed, ws, world.source_id)
    seed.conn.execute(
        "update app.watchlists set next_recheck_at = clock_timestamp() - interval '1 minute' where id = %s",
        (watch.id,),
    )
    sys_actor = system(ws)
    due = await run(db, sys_actor, lambda c: notes_repo.due_watch_rechecks(c, sys_actor))
    assert [w.id for w in due] == [watch.id]
    advanced = await run(db, sys_actor, lambda c: notes_repo.advance_watch_recheck(c, sys_actor, watch.id))
    assert advanced.next_recheck_at is not None and advanced.next_recheck_at > advanced.updated_at
    assert await run(db, sys_actor, lambda c: notes_repo.due_watch_rechecks(c, sys_actor)) == []
    removed = await run(db, actor, lambda c: notes_repo.remove_watch(c, actor, world.listing_id))
    assert removed is not None and not removed.active
    assert await run(db, actor, lambda c: notes_repo.remove_watch(c, actor, world.listing_id)) is None
    with pytest.raises(Forbidden):
        await run(
            db, sys_actor, lambda c: notes_repo.add_watch(c, sys_actor, world.listing_id, reason="SYNTHETIC")
        )
    reader = viewer(ws)
    with pytest.raises(Forbidden):
        await run(db, reader, lambda c: notes_repo.add_watch(c, reader, world.listing_id, reason="SYNTHETIC"))
    with pytest.raises(Forbidden):
        await run(db, actor, lambda c: notes_repo.due_watch_rechecks(c, actor))


async def test_due_watch_rechecks_only_fill_the_window_with_fetchable_sources(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    """More overdue watches on sources that may not fetch than the limit never hide a fetchable one."""
    ws = world.workspace_id
    _make_fetchable(seed, ws, world.source_id)
    blocked: list[dict[str, Any]] = [
        {},  # registered but disabled
        {**_FETCHABLE, "enabled": True, "paused": True, "pause_reason": "SYNTHETIC", "paused_at": "now()"},
        {**_FETCHABLE, "technical_status": "access_blocked"},  # an access-blocked source is never enabled
        {**_FETCHABLE, "enabled": True, "detail_mode": "card_only"},
    ]
    actor = reviewer(ws, kind="user")
    stuck: list[UUID] = []
    for cols in blocked:
        source = seed.source(ws, mode="public_html", adapter="synthetic_adapter", **cols)
        listing, _ = make_listing(seed, ws, source, eligible=False)
        watch = await run(
            db,
            actor,
            lambda c, li=listing: notes_repo.add_watch(
                c, actor, li, reason="SYNTHETIC stuck", recheck_interval=timedelta(hours=6)
            ),
        )
        stuck.append(watch.id)
    eligible = await run(
        db,
        actor,
        lambda c: notes_repo.add_watch(
            c, actor, world.listing_id, reason="SYNTHETIC due", recheck_interval=timedelta(hours=6)
        ),
    )
    # The stuck watches are the most overdue: ordered by due time alone they would fill the window.
    seed.conn.execute(
        "update app.watchlists set next_recheck_at = clock_timestamp() - interval '2 hours'"
        " where id = any(%s)",
        (stuck,),
    )
    seed.conn.execute(
        "update app.watchlists set next_recheck_at = clock_timestamp() - interval '1 minute' where id = %s",
        (eligible.id,),
    )
    sys_actor = system(ws)
    due = await run(db, sys_actor, lambda c: notes_repo.due_watch_rechecks(c, sys_actor, limit=2))
    assert [w.id for w in due] == [eligible.id]
    # ``source_ids`` narrows further to what the caller may fetch from now; empty means nothing.
    only = await run(
        db,
        sys_actor,
        lambda c: notes_repo.due_watch_rechecks(c, sys_actor, limit=2, source_ids=[world.source_id]),
    )
    assert [w.id for w in only] == [eligible.id]
    assert (
        await run(db, sys_actor, lambda c: notes_repo.due_watch_rechecks(c, sys_actor, source_ids=[])) == []
    )
    assert (
        await run(
            db, sys_actor, lambda c: notes_repo.due_watch_rechecks(c, sys_actor, source_ids=[uuid.uuid4()])
        )
        == []
    )
    # Once the source may fetch again, its watches are due again (none was advanced meanwhile).
    seed.conn.execute(
        "update app.sources set paused = false, pause_reason = null, paused_at = null"
        " where workspace_id = %s and paused",
        (ws,),
    )
    again = await run(db, sys_actor, lambda c: notes_repo.due_watch_rechecks(c, sys_actor, limit=10))
    assert len(again) == 2 and again[-1].id == eligible.id and again[0].id in stuck


async def test_watch_on_a_foreign_listing_is_not_found(
    db: Database, world: RealWorld, other_world: RealWorld
) -> None:
    foreign = reviewer(other_world.workspace_id, kind="user")
    with pytest.raises(NotFound):
        await run(
            db, foreign, lambda c: notes_repo.add_watch(c, foreign, world.listing_id, reason="SYNTHETIC")
        )
