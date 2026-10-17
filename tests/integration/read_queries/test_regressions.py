"""Regression tests for defects found in the independent review of the read-query service.

- Quarantined (parser-unhealthy) and not-yet-promoted revisions never leak into the candidate
  detail: price/availability history stop at the current revision and requesting such a revision
  is ``NOT_FOUND`` (never an unhandled model error).
- An expired claim reads as the case's restore state (prior decision of the same revision, else
  ``pending``) in the overview counts and the candidate queue; the ``status`` filter matches the
  review status of claimed cases too.
- ``changed_since`` notices a new valuation of the current revision.
- A missing/short cursor signing key is a server configuration error (``INTERNAL_ERROR``), never a
  client ``VALIDATION_ERROR``.
- A stored row that cannot be rendered is a typed ``INTERNAL_ERROR``.
- Keyset pages stay duplicate-free when candidates change status between pages.

SYNTHETIC data only; every query runs as ``suv_backend``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed, sha, unique
from tests.integration.read_queries.dataset import (
    CURSOR_SECRET,
    SeededWorkspace,
    make_candidate_listing,
    normalized,
    run,
    store_not_started_valuation,
    viewer,
)

from suv_deals.api.schemas import OutboxQuery
from suv_deals.domain.enums import ReviewState
from suv_deals.errors import AppError, ErrorCode, NotFound, ValidationFailed
from suv_deals.mcp.schemas import DealsListCandidatesInput, ReviewsListPendingInput
from suv_deals.persistence import queries
from suv_deals.persistence.database import Database
from suv_deals.settings import Settings

pytestmark = pytest.mark.db


async def _ids(db: Database, ws: UUID, **filters: Any) -> list[UUID]:
    actor = viewer(ws)
    query = DealsListCandidatesInput.model_validate({"limit": 100, **filters})
    result = await run(db, actor, lambda c: queries.list_candidates(c, actor, query, secret=CURSOR_SECRET))
    return [i.listing_id for i in result.data.items]


# --------------------------------------------------------------------------------------------
# Quarantined / unpromoted revisions
# --------------------------------------------------------------------------------------------


def _quarantined_revision(seed: Seed, data: SeededWorkspace, listing_key: str, number: int) -> UUID:
    """A parser-unhealthy revision: stored as evidence, never promoted to current."""
    ws, listing = data.workspace_id, data.listings[listing_key]
    slid = seed.scalar("select source_listing_id from app.listings where id = %s", (listing,))
    doc = normalized(
        data.source_keys["running"],
        slid,
        observed_at=datetime.now(UTC).replace(microsecond=0),
        amount_minor=100000,
        currency="EUR",
        country="DE",
        title="SYNTHETIC parser drift",
    )
    _, gen, obs = seed.detail_observation(ws, listing, promoted=False)
    return seed.revision(
        ws,
        listing,
        number,
        detail_generation=gen,
        observation_id=obs,
        observed_at=doc.observed_at,
        semantic_hash=doc.semantic_hash(),
        asking_minor=100000,
        currency="EUR",
        availability="sold_claimed",
        normalized=doc.model_dump(mode="json"),
        provenance={},
        parser_version=doc.parser_version,
        quarantined=True,
    )


async def test_quarantined_revision_never_reaches_the_candidate_detail(
    db: Database, seed: Seed, data: SeededWorkspace
) -> None:
    _quarantined_revision(seed, data, "priced", 3)
    actor = viewer(data.workspace_id)
    listing_id = data.listings["priced"]
    result = await run(db, actor, lambda c: queries.get_candidate(c, actor, listing_id))
    detail = result.data
    assert detail.revision.revision_number == 2 and detail.revision.is_current
    assert [p.revision_number for p in detail.price_history] == [1, 2]
    assert [p.payable.amount for p in detail.price_history] == ["2900.00", "2750.00"]
    assert {p.revision_number for p in detail.availability_history} <= {None, 1, 2}
    assert all(p.availability.value != "sold_claimed" for p in detail.availability_history)
    with pytest.raises(NotFound):
        await run(db, actor, lambda c: queries.get_candidate(c, actor, listing_id, 3))


async def test_quarantined_older_revision_is_not_served(
    db: Database, seed: Seed, data: SeededWorkspace
) -> None:
    # Revision 3 quarantined, then the source recovered and revision 4 was promoted.
    _quarantined_revision(seed, data, "priced", 3)
    ws, listing = data.workspace_id, data.listings["priced"]
    _, gen, obs = seed.detail_observation(ws, listing, promoted=True)
    current = data.revisions["priced"][-1]
    doc = seed.scalar("select normalized from app.listing_revisions where id = %s", (current,))
    rev4 = seed.revision(
        ws,
        listing,
        4,
        detail_generation=gen,
        observation_id=obs,
        observed_at=datetime.now(UTC),
        normalized=doc,
        provenance={},
        parser_version="synthetic_parser@1.0.0",
    )
    seed.promote(ws, listing, rev4, gen, obs)
    actor = viewer(ws)
    result = await run(db, actor, lambda c: queries.get_candidate(c, actor, listing))
    assert result.data.revision.revision_number == 4
    assert [p.revision_number for p in result.data.price_history] == [1, 2, 4]
    with pytest.raises(NotFound):
        await run(db, actor, lambda c: queries.get_candidate(c, actor, listing, 3))


# --------------------------------------------------------------------------------------------
# Expired claims and the status filter
# --------------------------------------------------------------------------------------------


def _claim(seed: Seed, ws: UUID, case_id: UUID, *, expired: bool) -> None:
    now = datetime.now(UTC)
    claimed_at, expires = (now - timedelta(minutes=20), now - timedelta(minutes=10))
    if not expired:
        claimed_at, expires = now - timedelta(minutes=1), now + timedelta(minutes=30)
    seed.conn.execute(
        "update app.review_cases set state = 'claimed', claim_holder = %s, claim_token_hash = %s,"
        " claimed_at = %s, claim_expires_at = %s, row_version = row_version + 1"
        " where workspace_id = %s and id = %s",
        (UUID(int=7), sha(unique("claim")), claimed_at, expires, ws, case_id),
    )


def _expire_claim(seed: Seed, ws: UUID, case_id: UUID) -> None:
    now = datetime.now(UTC)
    seed.conn.execute(
        "update app.review_cases set claimed_at = %s, claim_expires_at = %s, row_version = row_version + 1"
        " where workspace_id = %s and id = %s",
        (now - timedelta(minutes=20), now - timedelta(minutes=10), ws, case_id),
    )


async def test_expired_claims_read_as_their_restore_state(
    db: Database, seed: Seed, data: SeededWorkspace, settings: Settings
) -> None:
    ws = data.workspace_id
    # The claimed case without a prior decision expired: it is pending again.
    _expire_claim(seed, ws, data.cases["incomplete"])
    # The watch case (decided on its current revision) was claimed and the claim expired: watch.
    _claim(seed, ws, data.cases["paused"], expired=True)
    actor = viewer(ws)
    overview = await run(db, actor, lambda c: queries.overview_view(c, actor, settings))
    counts = overview.data.pending_reviews
    assert (counts.pending, counts.claimed, counts.watch) == (2, 0, 1)
    listing = await run(
        db,
        actor,
        lambda c: queries.list_candidates(c, actor, DealsListCandidatesInput(), secret=CURSOR_SECRET),
    )
    states = {i.listing_id: i.review_state for i in listing.data.items}
    assert states[data.listings["incomplete"]] == ReviewState.PENDING
    assert states[data.listings["paused"]] == ReviewState.WATCH
    assert await _ids(db, ws, status="watch") == [data.listings["paused"]]
    assert await _ids(db, ws, status="pending") == [data.listings["incomplete"], data.listings["priced"]]
    detail = await run(db, actor, lambda c: queries.get_candidate(c, actor, data.listings["paused"]))
    assert detail.data.review_case is not None and detail.data.review_case.state == ReviewState.WATCH


async def test_status_filter_matches_actively_claimed_candidates(db: Database, data: SeededWorkspace) -> None:
    # The incomplete case is actively claimed by another reviewer and has no prior decision:
    # its review status is still pending, so the pending filter lists it (shown as claimed).
    ws = data.workspace_id
    assert await _ids(db, ws, status="pending") == [data.listings["incomplete"], data.listings["priced"]]
    actor = viewer(ws)
    result = await run(
        db,
        actor,
        lambda c: queries.list_candidates(
            c, actor, DealsListCandidatesInput(status="pending"), secret=CURSOR_SECRET
        ),
    )
    states = {i.listing_id: i.review_state for i in result.data.items}
    assert states[data.listings["incomplete"]] == ReviewState.CLAIMED


# --------------------------------------------------------------------------------------------
# changed_since
# --------------------------------------------------------------------------------------------


async def test_changed_since_notices_a_new_valuation(db: Database, seed: Seed, data: SeededWorkspace) -> None:
    ws = data.workspace_id
    marker = seed.scalar("select clock_timestamp()")
    assert await _ids(db, ws, changed_since=marker.isoformat()) == []
    await store_not_started_valuation(
        db,
        ws,
        data.config_revision_id,
        listing_id=data.listings["needs_facts"],
        revision_id=data.revisions["needs_facts"][-1],
    )
    assert await _ids(db, ws, changed_since=marker.isoformat()) == [data.listings["needs_facts"]]


# --------------------------------------------------------------------------------------------
# Server-side failures are typed INTERNAL errors
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [b"short", b"", (), "a-text-secret-that-is-long-enough-0123456789"])
async def test_misconfigured_cursor_secret_is_an_internal_error(
    db: Database, data: SeededWorkspace, bad: Any
) -> None:
    actor = viewer(data.workspace_id)
    first = await run(
        db,
        actor,
        lambda c: queries.list_candidates(c, actor, DealsListCandidatesInput(limit=1), secret=CURSOR_SECRET),
    )
    calls = [
        lambda c: queries.list_candidates(c, actor, DealsListCandidatesInput(limit=1), secret=bad),
        lambda c: queries.list_candidates(
            c, actor, DealsListCandidatesInput(limit=1, cursor=first.next_cursor), secret=bad
        ),
        lambda c: queries.outbox_attention_view(c, actor, OutboxQuery(limit=100), secret=bad),
        lambda c: queries.get_comparables(c, actor, data.comparable_set_id, secret=bad),
        lambda c: queries.review_queue(c, actor, ReviewsListPendingInput(limit=1), secret=bad),
    ]
    for call in calls:
        with pytest.raises(AppError) as error:
            await run(db, actor, call)
        assert not isinstance(error.value, ValidationFailed)
        assert error.value.code == ErrorCode.INTERNAL_ERROR
        assert "32" not in error.value.message


async def test_unrenderable_stored_row_is_an_internal_error(
    db: Database, seed: Seed, data: SeededWorkspace
) -> None:
    # An eligible listing whose screened profile is missing cannot be rendered consistently.
    seed.conn.execute(
        "update app.listings set eligibility_state = 'eligible_primary', eligibility_profile = null,"
        " row_version = row_version + 1 where workspace_id = %s and id = %s",
        (data.workspace_id, data.listings["needs_facts"]),
    )
    actor = viewer(data.workspace_id)
    for call in (
        lambda c: queries.list_candidates(c, actor, DealsListCandidatesInput(), secret=CURSOR_SECRET),
        lambda c: queries.get_candidate(c, actor, data.listings["needs_facts"]),
    ):
        with pytest.raises(AppError) as error:
            await run(db, actor, call)
        assert error.value.code == ErrorCode.INTERNAL_ERROR and error.value.retryable is False


# --------------------------------------------------------------------------------------------
# Keyset pages under status changes
# --------------------------------------------------------------------------------------------


async def test_keyset_pages_under_status_changes_between_pages(
    db: Database, seed: Seed, data: SeededWorkspace
) -> None:
    ws = data.workspace_id
    source, key = data.sources["running"], data.source_keys["running"]
    now = datetime.now(UTC)
    extra = [
        make_candidate_listing(seed, ws, source, key, created_at=now - timedelta(days=d), now=now)[0]
        for d in range(6, 10)
    ]
    order = [data.listings[k] for k in ("paused", "needs_facts", "incomplete", "priced")] + extra
    actor = viewer(ws)
    first = await run(
        db,
        actor,
        lambda c: queries.list_candidates(c, actor, DealsListCandidatesInput(limit=3), secret=CURSOR_SECRET),
    )
    seen = [i.listing_id for i in first.data.items]
    assert seen == order[:3]
    # Between pages: a listed candidate and a not-yet-listed one are rejected by screening, and
    # the rejected listing becomes a candidate (it sorts before the cursor: it waits for a re-query).
    for listing, state in (
        (order[0], "rejected"),
        (order[5], "rejected"),
        (data.listings["rejected"], "needs_facts"),
    ):
        seed.conn.execute(
            "update app.listings set eligibility_state = %s, eligibility_profile = null,"
            " row_version = row_version + 1 where workspace_id = %s and id = %s",
            (state, ws, listing),
        )
    cursor = first.next_cursor
    while cursor is not None:
        page = await run(
            db,
            actor,
            lambda c, cur=cursor: queries.list_candidates(
                c, actor, DealsListCandidatesInput(limit=3, cursor=cur), secret=CURSOR_SECRET
            ),
        )
        seen.extend(i.listing_id for i in page.data.items)
        cursor = page.next_cursor
    assert len(seen) == len(set(seen)), "no candidate appears twice"
    # Every row that stayed a candidate is listed; the row that left before its page is not.
    stayed = [i for i in order[3:] if i != order[5]]
    assert [i for i in seen if i in stayed] == stayed
    assert order[5] not in seen
    assert data.listings["rejected"] not in seen
    requery = await run(
        db,
        actor,
        lambda c: queries.list_candidates(
            c, actor, DealsListCandidatesInput(limit=100), secret=CURSOR_SECRET
        ),
    )
    assert data.listings["rejected"] in {i.listing_id for i in requery.data.items}
