"""Signed keyset cursors: tamper/expiry/mismatch rejection and complete, duplicate-free pages
under concurrent inserts and updates (spec 21 "Pagination and errors").

SYNTHETIC data only; every query runs as ``suv_backend`` under RLS.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.read_queries.dataset import (
    CURSOR_SECRET,
    OTHER_SECRET,
    SeededWorkspace,
    make_candidate_listing,
    member,
    run,
    seed_foreign_workspace,
    store_comparables,
    viewer,
)

from suv_deals.api.schemas import OutboxQuery
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Role
from suv_deals.domain.pagination import encode_cursor, filter_hash, keyset_cursor
from suv_deals.errors import ErrorCode, ValidationFailed
from suv_deals.mcp.schemas import (
    DealsListCandidatesInput,
    ReviewsListPendingInput,
)
from suv_deals.persistence import queries
from suv_deals.persistence.database import Database

pytestmark = pytest.mark.db


async def candidates_page(
    db: Database, actor: ActorContext, *, cursor: str | None = None, limit: int = 2, **filters: Any
) -> queries.QueryResult[Any]:
    query = DealsListCandidatesInput.model_validate({"limit": limit, "cursor": cursor, **filters})
    return await run(db, actor, lambda c: queries.list_candidates(c, actor, query, secret=CURSOR_SECRET))


async def rejected(coro: Any) -> str:
    """The ``details.cursor`` problem of a rejected cursor (always VALIDATION_ERROR)."""
    with pytest.raises(ValidationFailed) as error:
        await coro
    assert error.value.code == ErrorCode.VALIDATION_ERROR
    problem = error.value.details.get("cursor")
    assert isinstance(problem, str)
    return problem


def _alter_body(cursor: str, change: dict[str, Any]) -> str:
    body, signature = cursor.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload.update(change)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode() + "." + signature


# --------------------------------------------------------------------------------------------
# Complete, duplicate-free keyset pages
# --------------------------------------------------------------------------------------------


async def test_candidate_pages_cover_every_candidate_once(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    seen: list[UUID] = []
    cursor: str | None = None
    pages = 0
    while True:
        result = await candidates_page(db, actor, cursor=cursor, limit=1)
        seen.extend(i.listing_id for i in result.data.items)
        pages += 1
        if result.next_cursor is None:
            break
        assert len(result.next_cursor) <= 2048
        cursor = result.next_cursor
    assert pages == 4
    assert len(seen) == len(set(seen)) == 4
    assert set(seen) == data.candidate_ids


async def test_keyset_pages_are_complete_and_duplicate_free_under_concurrent_inserts(
    db: Database, seed: Seed, data: SeededWorkspace
) -> None:
    ws = data.workspace_id
    actor = viewer(ws)
    source, key = data.sources["running"], data.source_keys["running"]
    now = datetime.now(UTC)
    # More candidates so the listing needs several pages.
    for days in range(6, 12):
        make_candidate_listing(seed, ws, source, key, created_at=now - timedelta(days=days), now=now)
    first = await candidates_page(db, actor, limit=3)
    initial = await candidates_page(db, actor, limit=100)
    expected = {i.listing_id for i in initial.data.items}
    assert len(expected) == 10

    fresh: list[UUID] = []
    stop = asyncio.Event()

    async def inserter() -> None:
        # New listings committed while the client pages: created after the as-of boundary.
        while not stop.is_set():
            listing, _ = await asyncio.to_thread(
                make_candidate_listing, seed, ws, source, key, created_at=None, now=datetime.now(UTC)
            )
            fresh.append(listing)
            await asyncio.sleep(0.01)

    task = asyncio.create_task(inserter())
    try:
        seen = [i.listing_id for i in first.data.items]
        cursor = first.next_cursor
        while cursor is not None:
            await asyncio.sleep(0.02)
            # Updates between pages change mutable columns, never the immutable sort key.
            await asyncio.to_thread(
                seed.conn.execute,
                "update app.listings set row_version = row_version + 1, last_seen_at = now()"
                " where workspace_id = %s and id = any(%s)",
                (ws, list(expected)),
            )
            result = await candidates_page(db, actor, cursor=cursor, limit=3)
            seen.extend(i.listing_id for i in result.data.items)
            cursor = result.next_cursor
    finally:
        stop.set()
        await task
    assert fresh, "the concurrent inserter committed listings while paging"
    assert len(seen) == len(set(seen)), "no listing appears on two pages"
    assert set(seen) == expected, "every candidate visible at the first page is listed exactly once"
    assert not set(seen) & set(fresh), "listings created after the as-of boundary wait for a re-query"
    requery = await candidates_page(db, actor, limit=100)
    assert set(fresh) <= {i.listing_id for i in requery.data.items}


async def test_filtered_pages_keep_their_filter(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    first = await candidates_page(db, actor, limit=1, profile="primary")
    assert first.next_cursor is not None
    ids = [i.listing_id for i in first.data.items]
    cursor = first.next_cursor
    while cursor is not None:
        page = await candidates_page(db, actor, cursor=cursor, limit=1, profile="primary")
        ids.extend(i.listing_id for i in page.data.items)
        cursor = page.next_cursor
    assert ids == [data.listings[k] for k in ("paused", "incomplete", "priced")]


async def test_comparable_member_pages(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    set_id = data.comparable_set_id
    assert set_id is not None
    ordinals: list[int] = []
    cursor: str | None = None
    while True:
        result = await run(
            db,
            actor,
            lambda c, cur=cursor: queries.get_comparables(
                c, actor, set_id, include_excluded=True, limit=2, cursor=cur, secret=CURSOR_SECRET
            ),
        )
        ordinals.extend(m.ordinal for m in result.data.members)
        if result.next_cursor is None:
            break
        cursor = result.next_cursor
    assert ordinals == list(range(len(ordinals)))
    assert len(ordinals) == result.data.selected_count + result.data.excluded_count


async def test_outbox_pages_exclude_rows_created_after_the_first_page(
    db: Database, seed: Seed, data: SeededWorkspace
) -> None:
    actor = viewer(data.workspace_id)
    first = await run(
        db,
        actor,
        lambda c: queries.outbox_attention_view(c, actor, OutboxQuery(limit=2), secret=CURSOR_SECRET),
    )
    late = seed.outbox(data.workspace_id, state="blocked", blocker_code="NO_ACTIVE_ROUTE")
    seen = [i.outbox_id for i in first.data.items]
    cursor = first.next_cursor
    while cursor is not None:
        page = await run(
            db,
            actor,
            lambda c, cur=cursor: queries.outbox_attention_view(
                c, actor, OutboxQuery(cursor=cur, limit=2), secret=CURSOR_SECRET
            ),
        )
        seen.extend(i.outbox_id for i in page.data.items)
        cursor = page.next_cursor
    expected = {data.outbox[k] for k in ("uncertain", "blocked", "dead_letter", "retry_wait", "fixture")}
    assert len(seen) == len(set(seen)) and set(seen) == expected
    assert late not in seen


async def test_review_queue_cursor_pages_the_frozen_snapshot(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    first = await run(
        db,
        actor,
        lambda c: queries.review_queue(c, actor, ReviewsListPendingInput(limit=1), secret=CURSOR_SECRET),
    )
    assert first.next_cursor is not None and first.data.total == 2
    second = await run(
        db,
        actor,
        lambda c: queries.review_queue(
            c, actor, ReviewsListPendingInput(limit=1, cursor=first.next_cursor), secret=CURSOR_SECRET
        ),
    )
    assert {i.case_id for i in first.data.items + second.data.items} == {
        data.cases["priced"],
        data.cases["incomplete"],
    }
    assert second.next_cursor is None


# --------------------------------------------------------------------------------------------
# Rejected cursors
# --------------------------------------------------------------------------------------------


async def test_altered_and_malformed_cursors_are_rejected(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    first = await candidates_page(db, actor, limit=1)
    cursor = first.next_cursor
    assert cursor is not None
    body, signature = cursor.split(".")
    # Altered position (MAC no longer matches), forged signature, other secret, garbage.
    forged_position = _alter_body(cursor, {"sort": ["2000-01-01T00:00:00Z", str(uuid.uuid4())]})
    assert await rejected(candidates_page(db, actor, cursor=forged_position, limit=1)) == "tampered"
    flipped = signature[:-2] + ("AA" if signature[-2:] != "AA" else "BB")
    assert await rejected(candidates_page(db, actor, cursor=f"{body}.{flipped}", limit=1)) == "tampered"
    foreign_key = await run(
        db,
        actor,
        lambda c: queries.list_candidates(c, actor, DealsListCandidatesInput(limit=1), secret=OTHER_SECRET),
    )
    assert foreign_key.next_cursor is not None
    assert await rejected(candidates_page(db, actor, cursor=foreign_key.next_cursor, limit=1)) == "tampered"
    for garbage in ("not-a-cursor", "a.b.c", f"{body}.", "%%%.###"):
        assert await rejected(candidates_page(db, actor, cursor=garbage, limit=1)) == "malformed"


async def test_validly_signed_cursor_with_a_wrong_sort_tuple_is_malformed(
    db: Database, data: SeededWorkspace
) -> None:
    actor = viewer(data.workspace_id)
    now = datetime.now(UTC)
    payload = keyset_cursor(
        query=queries.CANDIDATES_QUERY,
        workspace_id=actor.workspace_id,
        principal_id=actor.principal_id,
        filters_hash=filter_hash(DealsListCandidatesInput(limit=1).filters()),
        last_sort_key=(42, uuid.uuid4()),  # an integer where the timestamp belongs
        as_of=now,
        now=now,
    )
    token = encode_cursor(payload, CURSOR_SECRET)
    assert await rejected(candidates_page(db, actor, cursor=token, limit=1)) == "malformed"


async def test_expired_cursor_is_rejected(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    issued = datetime.now(UTC) - timedelta(hours=2)
    payload = keyset_cursor(
        query=queries.CANDIDATES_QUERY,
        workspace_id=actor.workspace_id,
        principal_id=actor.principal_id,
        filters_hash=filter_hash(DealsListCandidatesInput(limit=1).filters()),
        last_sort_key=(issued, uuid.uuid4()),
        as_of=issued,
        now=issued,
        ttl=timedelta(minutes=30),
    )
    token = encode_cursor(payload, CURSOR_SECRET)
    assert await rejected(candidates_page(db, actor, cursor=token, limit=1)) == "expired"


async def test_filter_mismatch_is_rejected(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    first = await candidates_page(db, actor, limit=1, profile="primary")
    assert first.next_cursor is not None
    for other in ({"country": "IT"}, {}, {"profile": "manual_4000"}):
        problem = await rejected(candidates_page(db, actor, cursor=first.next_cursor, limit=1, **other))
        assert problem == "mismatch"
    # A different limit is not a filter: the same cursor keeps working.
    page = await candidates_page(db, actor, cursor=first.next_cursor, limit=5, profile="primary")
    assert page.data.items


async def test_workspace_principal_and_query_mismatch_are_rejected(
    db: Database, seed: Seed, data: SeededWorkspace
) -> None:
    actor = viewer(data.workspace_id)
    first = await candidates_page(db, actor, limit=1)
    assert first.next_cursor is not None
    other = await seed_foreign_workspace(db, seed, "cursor")
    # Same principal acting in another workspace.
    elsewhere = member(other.workspace_id, Role.VIEWER, principal_id=actor.principal_id)
    assert await rejected(candidates_page(db, elsewhere, cursor=first.next_cursor, limit=1)) == "mismatch"
    # Another principal of the same workspace.
    colleague = viewer(data.workspace_id)
    assert await rejected(candidates_page(db, colleague, cursor=first.next_cursor, limit=1)) == "mismatch"
    # A cursor of another query (outbox) presented to the candidate listing.
    outbox = await run(
        db,
        actor,
        lambda c: queries.outbox_attention_view(c, actor, OutboxQuery(limit=1), secret=CURSOR_SECRET),
    )
    assert outbox.next_cursor is not None
    assert await rejected(candidates_page(db, actor, cursor=outbox.next_cursor, limit=1)) == "mismatch"


async def test_comparable_cursor_is_bound_to_its_set_and_member_selection(
    db: Database, data: SeededWorkspace
) -> None:
    actor = viewer(data.workspace_id)
    assert data.comparable_set_id is not None
    other_set = await store_comparables(
        db,
        data.workspace_id,
        listing_id=data.listings["incomplete"],
        revision_id=data.revisions["incomplete"][-1],
        source_id=data.sources["mk"],
        source_key=data.source_keys["mk"],
    )
    set_id = data.comparable_set_id
    first = await run(
        db, actor, lambda c: queries.get_comparables(c, actor, set_id, limit=1, secret=CURSOR_SECRET)
    )
    assert first.next_cursor is not None
    for other_id, include_excluded in ((set_id, True), (other_set.id, False)):
        problem = await rejected(
            run(
                db,
                actor,
                lambda c, o=other_id, x=include_excluded: queries.get_comparables(
                    c, actor, o, include_excluded=x, limit=1, cursor=first.next_cursor, secret=CURSOR_SECRET
                ),
            )
        )
        assert problem == "mismatch"


async def test_review_queue_rejects_tampered_cursor(db: Database, data: SeededWorkspace) -> None:
    actor = viewer(data.workspace_id)
    first = await run(
        db,
        actor,
        lambda c: queries.review_queue(c, actor, ReviewsListPendingInput(limit=1), secret=CURSOR_SECRET),
    )
    assert first.next_cursor is not None
    _body, signature = first.next_cursor.split(".")
    altered = _alter_body(first.next_cursor, {"ord": 0})
    query = ReviewsListPendingInput(limit=1, cursor=altered)
    problem = await rejected(
        run(db, actor, lambda c: queries.review_queue(c, actor, query, secret=CURSOR_SECRET))
    )
    assert problem == "tampered"
    assert signature  # the original signature was intact


async def test_outbox_cursor_is_bound_to_its_state_filter_and_signature(
    db: Database, data: SeededWorkspace
) -> None:
    actor = viewer(data.workspace_id)
    first = await run(
        db,
        actor,
        lambda c: queries.outbox_attention_view(
            c, actor, OutboxQuery(state="blocked", limit=1), secret=CURSOR_SECRET
        ),
    )
    cursor = first.next_cursor
    assert cursor is not None  # two blocked rows (one fixture)
    for other in (
        OutboxQuery(cursor=cursor, limit=1),
        OutboxQuery(cursor=cursor, state="uncertain", limit=1),
    ):
        problem = await rejected(
            run(
                db, actor, lambda c, q=other: queries.outbox_attention_view(c, actor, q, secret=CURSOR_SECRET)
            )
        )
        assert problem == "mismatch"
    moved = _alter_body(cursor, {"sort": ["1999-01-01T00:00:00Z", str(uuid.uuid4())]})
    tampered = OutboxQuery(cursor=moved, state="blocked", limit=1)
    problem = await rejected(
        run(db, actor, lambda c: queries.outbox_attention_view(c, actor, tampered, secret=CURSOR_SECRET))
    )
    assert problem == "tampered"
    second = await run(
        db,
        actor,
        lambda c: queries.outbox_attention_view(
            c, actor, OutboxQuery(cursor=cursor, state="blocked", limit=1), secret=CURSOR_SECRET
        ),
    )
    assert {i.outbox_id for i in first.data.items + second.data.items} == {
        data.outbox["blocked"],
        data.outbox["fixture"],
    }
    assert second.next_cursor is None


async def test_comparable_cursor_rejects_tampering_and_previous_keys_still_verify(
    db: Database, data: SeededWorkspace
) -> None:
    actor = viewer(data.workspace_id)
    set_id = data.comparable_set_id
    assert set_id is not None
    first = await run(
        db, actor, lambda c: queries.get_comparables(c, actor, set_id, limit=1, secret=CURSOR_SECRET)
    )
    assert first.next_cursor is not None
    skipped = _alter_body(first.next_cursor, {"sort": [2, str(uuid.uuid4())]})
    problem = await rejected(
        run(
            db,
            actor,
            lambda c: queries.get_comparables(
                c, actor, set_id, limit=1, cursor=skipped, secret=CURSOR_SECRET
            ),
        )
    )
    assert problem == "tampered"
    # Key rotation: a cursor signed with the previous key verifies while that key is still listed.
    rotated = await run(
        db,
        actor,
        lambda c: queries.get_comparables(
            c, actor, set_id, limit=1, cursor=first.next_cursor, secret=(OTHER_SECRET, CURSOR_SECRET)
        ),
    )
    assert [m.ordinal for m in rotated.data.members] == [1]
