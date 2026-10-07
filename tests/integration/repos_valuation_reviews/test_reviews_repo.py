"""Review cases, claims, decisions, outbox events and the stable queue (spec 14, 18, 21, 22).

Every operation under test runs as ``suv_backend`` under RLS; data is SYNTHETIC.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError
from tests.integration.db.helpers import Seed
from tests.integration.repos_valuation_reviews.builders import (
    CURSOR_SECRET,
    RealWorld,
    add_revision,
    case_row,
    claim_case,
    complete_valuation,
    decision_count,
    expire_claim,
    idem_key,
    make_listing,
    open_case,
    outbox_rows,
    owner,
    primary_profile,
    reviewer,
    run,
    screening,
    set_eligibility,
    submit_request,
    system,
    viewer,
)

from suv_deals.domain.enums import EligibilityState, ReviewOutcome, ReviewState
from suv_deals.domain.valuation import InvalidationReason
from suv_deals.errors import (
    AlreadyClaimed,
    ClaimExpired,
    Forbidden,
    IdempotencyConflict,
    NotFound,
    ValidationFailed,
    VersionConflict,
)
from suv_deals.persistence import bindings_repo, reviews_repo, valuation_repo
from suv_deals.persistence.database import Conn, Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db


# --------------------------------------------------------------------------------------------
# Case creation and the same-transaction review.pending event
# --------------------------------------------------------------------------------------------


async def test_qualifying_revision_creates_pending_case_and_event(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    created = await open_case(db, world)
    assert created.action == "created" and created.case_version == 1 and created.state == ReviewState.PENDING
    assert created.readiness == "not_valued" and created.event_created
    row = case_row(seed, created.case_id)  # type: ignore[arg-type]
    assert (row["state"], row["row_version"], row["revision_id"], row["is_fixture"]) == (
        "pending",
        1,
        world.revision_id,
        False,
    )
    events = outbox_rows(seed, world.workspace_id)
    assert len(events) == 1
    event = events[0]
    assert event["dedup_key"] == f"review.pending:{created.case_id}:1"
    assert event["event_version"] == 1 and event["payload"]["schema_version"] == "1.0"
    assert event["event_id"] == created.event_id and event["state"] == "pending"
    assert not event["is_fixture"] and event["destination_binding_id"] is None  # no active route
    assert event["payload"]["case_version"] == 1 and event["payload"]["listing_revision"] == 1
    # The same facts again change nothing and create no second event.
    again = await open_case(db, world)
    assert again.action == "unchanged" and again.case_version == 1
    assert len(outbox_rows(seed, world.workspace_id)) == 1
    # Not qualifying for the profile: no case at all.
    listing, rev = make_listing(seed, world.workspace_id, world.source_id, eligible=False)
    set_eligibility(seed, world.workspace_id, listing, "rejected")
    rejected = await open_case(
        db,
        world,
        listing_id=listing,
        revision_id=rev,
        screening_result=screening(EligibilityState.REJECTED),
    )
    assert rejected.action == "not_qualifying" and rejected.case_id is None


async def test_rollback_removes_case_and_its_event(db: Database, seed: Seed, world: RealWorld) -> None:
    actor = system(world.workspace_id)

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with unit_of_work(db, actor) as conn:
            result = await reviews_repo.upsert_review_case(
                conn,
                actor,
                world.listing_id,
                world.revision_id,
                screening(),
                None,
                primary_profile(),
                dashboard_base_url="https://dashboard.synthetic.example",
            )
            assert result.event_created
            raise Boom
    assert (
        seed.scalar("select count(*) from app.review_cases where workspace_id = %s", (world.workspace_id,))
        == 0
    )
    assert outbox_rows(seed, world.workspace_id) == []


async def test_fixture_case_event_is_blocked_and_never_routed(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    fixture_source = seed.source(world.workspace_id)  # mode 'fixture'
    listing, rev = make_listing(seed, world.workspace_id, fixture_source)
    created = await open_case(db, world, listing_id=listing, revision_id=rev)
    assert created.action == "created"
    assert case_row(seed, created.case_id)["is_fixture"] is True  # type: ignore[arg-type]
    [event] = outbox_rows(seed, world.workspace_id)
    assert event["is_fixture"] and event["state"] == "blocked" and event["payload"]["fixture"] is True
    assert event["destination_binding_id"] is None


# --------------------------------------------------------------------------------------------
# Claims
# --------------------------------------------------------------------------------------------


async def test_concurrent_claims_one_wins_the_other_is_already_claimed(
    db: Database, world: RealWorld
) -> None:
    created = await open_case(db, world)
    case_id = created.case_id
    assert case_id is not None
    first, second = reviewer(world.workspace_id), reviewer(world.workspace_id)
    claimed = asyncio.Event()

    async def hold_claim(conn: Conn) -> Any:
        result = await reviews_repo.claim(conn, first, case_id, 1, idem_key())
        claimed.set()
        await asyncio.sleep(0.3)  # keep the row lock while the competitor arrives
        return result

    async def compete() -> Any:
        await claimed.wait()
        return await run(db, second, lambda c: reviews_repo.claim(c, second, case_id, 1, idem_key()))

    outcomes = await asyncio.gather(run(db, first, hold_claim), compete(), return_exceptions=True)
    assert outcomes[0].claim_token is not None and outcomes[0].case_version == 2  # type: ignore[union-attr]
    assert isinstance(outcomes[1], AlreadyClaimed)
    # The loser with the CURRENT version is still refused: never a silent override.
    with pytest.raises(AlreadyClaimed):
        await claim_case(db, second, case_id, 2)


async def test_claim_token_is_hashed_and_replay_is_redacted(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    created = await open_case(db, world)
    assert created.case_id is not None
    actor = reviewer(world.workspace_id)
    key = idem_key()
    grant = await run(db, actor, lambda c: reviews_repo.claim(c, actor, created.case_id, 1, key))  # type: ignore[arg-type]
    assert grant.claim_token is not None and not grant.claim_token_redacted
    row = case_row(seed, created.case_id)
    assert row["claim_token_hash"] != grant.claim_token and len(row["claim_token_hash"]) == 64
    assert row["claim_holder"] == actor.principal_id
    replay = await run(db, actor, lambda c: reviews_repo.claim(c, actor, created.case_id, 1, key))  # type: ignore[arg-type]
    assert replay.claim_token is None and replay.claim_token_redacted and replay.case_version == 2
    stored = seed.scalar(
        "select result::text from ops.idempotency_records where idempotency_key = %s", (key,)
    )
    assert grant.claim_token not in stored
    with pytest.raises(AlreadyClaimed):  # another reviewer, whatever version it sends
        await claim_case(db, reviewer(world.workspace_id), created.case_id, 2)
    with pytest.raises(VersionConflict):  # the holder with a stale expected version
        await claim_case(db, actor, created.case_id, 1)
    viewer_actor = viewer(world.workspace_id)
    with pytest.raises(Forbidden):
        await claim_case(db, viewer_actor, created.case_id, 2)


async def test_claim_expiry_uses_database_time(db: Database, seed: Seed, world: RealWorld) -> None:
    created = await open_case(db, world)
    case_id = created.case_id
    assert case_id is not None
    first, second = reviewer(world.workspace_id), reviewer(world.workspace_id)
    grant = await claim_case(db, first, case_id, 1)
    expire_claim(seed, case_id)
    with pytest.raises(ClaimExpired):
        await run(db, first, lambda c: reviews_repo.submit(c, first, submit_request(grant)))
    taken = await claim_case(db, second, case_id, 2)
    assert taken.took_over_expired and taken.case_version == 3
    # The previous holder's release is a no-op now (not its claim any more).
    released = await run(
        db, first, lambda c: reviews_repo.release(c, first, case_id, grant.claim_token or "", idem_key())
    )
    assert not released.released and released.reason == "not_held"
    # The reaper returns expired claims to their restore state by database time.
    expire_claim(seed, case_id)
    sys_actor = system(world.workspace_id)
    expired = await run(db, sys_actor, lambda c: reviews_repo.expire_claims(c, sys_actor))
    assert [e.case_id for e in expired] == [case_id] and expired[0].state == ReviewState.PENDING
    row = case_row(seed, case_id)
    assert (row["state"], row["row_version"], row["claim_token_hash"]) == ("pending", 4, None)


async def test_release_only_the_callers_current_claim(db: Database, seed: Seed, world: RealWorld) -> None:
    created = await open_case(db, world)
    case_id = created.case_id
    assert case_id is not None
    holder, other = reviewer(world.workspace_id), reviewer(world.workspace_id)
    grant = await claim_case(db, holder, case_id, 1)
    token = grant.claim_token or ""
    noop = await run(db, other, lambda c: reviews_repo.release(c, other, case_id, token, idem_key()))
    assert not noop.released and noop.reason == "not_held" and case_row(seed, case_id)["state"] == "claimed"
    with pytest.raises(ClaimExpired):  # the holder presenting a stale/wrong token
        await run(db, holder, lambda c: reviews_repo.release(c, holder, case_id, "w" * 43, idem_key()))
    key = idem_key()
    released = await run(db, holder, lambda c: reviews_repo.release(c, holder, case_id, token, key))
    assert released.released and released.state == ReviewState.PENDING and released.case_version == 3
    replay = await run(db, holder, lambda c: reviews_repo.release(c, holder, case_id, token, key))
    assert replay == released
    again = await run(db, holder, lambda c: reviews_repo.release(c, holder, case_id, token, idem_key()))
    assert not again.released and again.reason == "not_claimed"
    assert case_row(seed, case_id)["row_version"] == 3


# --------------------------------------------------------------------------------------------
# Submit
# --------------------------------------------------------------------------------------------


async def test_submit_appends_immutable_decision_and_updates_case(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    created = await open_case(db, world)
    case_id = created.case_id
    assert case_id is not None
    actor = reviewer(world.workspace_id)
    grant = await claim_case(db, actor, case_id, 1)
    request = submit_request(grant, outcome=ReviewOutcome.NEEDS_INFORMATION, model_run_id="SYNTHETIC-run-1")
    decision = await run(
        db, actor, lambda c: reviews_repo.submit(c, actor, request, model_name="SYNTHETIC-model")
    )
    assert decision.outcome == ReviewOutcome.NEEDS_INFORMATION and decision.case_version == 2
    assert decision.new_case_version == 3 and decision.case_state == ReviewState.NEEDS_INFORMATION
    assert decision.actor.principal_id == actor.principal_id and decision.actor.principal_kind == "mcp_client"
    assert decision.tool_request_id == actor.request_id and decision.model_name == "SYNTHETIC-model"
    row = case_row(seed, case_id)
    assert (row["state"], row["row_version"], row["latest_decision_id"], row["claim_holder"]) == (
        "needs_information",
        3,
        decision.decision_id,
        None,
    )
    stored = seed.conn.execute(
        "select actor_principal_id, actor_kind, actor_role, case_version, listing_revision_id, input_hash,"
        " model_run_id from app.review_decisions where id = %s",
        (decision.decision_id,),
    ).fetchone()
    assert stored == (
        actor.principal_id,
        "mcp_client",
        "reviewer",
        2,
        world.revision_id,
        decision.input_hash,
        "SYNTHETIC-run-1",
    )
    audit = seed.scalar(
        "select count(*) from ops.audit_events where target_id = %s and action = 'review.submit'", (case_id,)
    )
    assert audit == 1
    # Request bodies cannot carry actor fields (no impersonation).
    with pytest.raises(PydanticValidationError):
        submit_request(grant, actor_principal_id=uuid.uuid4())
    view = await run(
        db,
        viewer(world.workspace_id),
        lambda c: reviews_repo.get_case(c, viewer(world.workspace_id), case_id),
    )
    assert view.state == ReviewState.NEEDS_INFORMATION and [d.decision_id for d in view.decisions] == [
        decision.decision_id
    ]
    assert view.candidate.listing_id == world.listing_id and not view.claim.claimed


async def test_submit_rejects_stale_versions_revisions_claims_and_foreign_cases(
    db: Database, seed: Seed, world: RealWorld, other_world: RealWorld
) -> None:
    created = await open_case(db, world)
    case_id = created.case_id
    assert case_id is not None
    actor = reviewer(world.workspace_id)
    grant = await claim_case(db, actor, case_id, 1)

    async def attempt(**overrides: Any) -> None:
        request = submit_request(grant, **overrides)
        await run(db, actor, lambda c: reviews_repo.submit(c, actor, request))

    with pytest.raises(VersionConflict):
        await attempt(expected_version=1)
    with pytest.raises(VersionConflict):
        await attempt(listing_revision=2)
    with pytest.raises(ClaimExpired):
        await attempt(claim_token="t" * 43)
    with pytest.raises(VersionConflict):  # cites a valuation the case does not reference
        await attempt(valuation_id=uuid.uuid4())
    foreign = reviewer(other_world.workspace_id)
    with pytest.raises(NotFound):
        request = submit_request(grant)
        await run(db, foreign, lambda c: reviews_repo.submit(c, foreign, request))
    # A newer revision is promoted but not yet applied to the case: no decision on old facts.
    add_revision(
        seed, world.workspace_id, world.listing_id, 2, make="Example", model="Trail", seller_country="DE"
    )
    with pytest.raises(VersionConflict):
        await attempt()
    expire_claim(seed, case_id)
    with pytest.raises(ClaimExpired):
        await attempt()
    assert decision_count(seed, case_id) == 0


async def test_idempotent_submit_replays_and_conflicts(db: Database, seed: Seed, world: RealWorld) -> None:
    created = await open_case(db, world)
    case_id = created.case_id
    assert case_id is not None
    actor = reviewer(world.workspace_id)
    grant = await claim_case(db, actor, case_id, 1)
    request = submit_request(grant)
    first = await run(db, actor, lambda c: reviews_repo.submit(c, actor, request))
    replay = await run(db, actor, lambda c: reviews_repo.submit(c, actor, request))
    assert replay == first and decision_count(seed, case_id) == 1
    different = request.model_copy(update={"summary": "SYNTHETIC different rationale for the same key."})
    with pytest.raises(IdempotencyConflict):
        await run(db, actor, lambda c: reviews_repo.submit(c, actor, different))
    # The key is scoped to the principal: another reviewer's identical key is independent.
    other = reviewer(world.workspace_id)
    with pytest.raises(ClaimExpired):
        await run(db, other, lambda c: reviews_repo.submit(c, other, request))


async def test_timeout_retry_cannot_double_submit(db: Database, seed: Seed, world: RealWorld) -> None:
    created = await open_case(db, world)
    case_id = created.case_id
    assert case_id is not None
    actor = reviewer(world.workspace_id)
    grant = await claim_case(db, actor, case_id, 1)
    request = submit_request(grant)
    submitted = asyncio.Event()

    async def slow_commit(conn: Conn) -> Any:
        result = await reviews_repo.submit(conn, actor, request)
        submitted.set()
        await asyncio.sleep(0.3)  # the client times out and retries before this commits
        return result

    async def retry() -> Any:
        await submitted.wait()
        return await run(db, actor, lambda c: reviews_repo.submit(c, actor, request))

    original, retried = await asyncio.gather(run(db, actor, slow_commit), retry())
    assert retried == original and decision_count(seed, case_id) == 1
    # A second, different request against the same (now decided) version cannot add a decision.
    with pytest.raises(ClaimExpired):
        await run(
            db,
            actor,
            lambda c: reviews_repo.submit(c, actor, submit_request(grant, outcome=ReviewOutcome.REJECTED)),
        )
    assert decision_count(seed, case_id) == 1


# --------------------------------------------------------------------------------------------
# New material information
# --------------------------------------------------------------------------------------------


async def test_new_revision_during_review_creates_new_case_version(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    created = await open_case(db, world)
    case_id = created.case_id
    assert case_id is not None
    actor = reviewer(world.workspace_id)
    grant = await claim_case(db, actor, case_id, 1)
    rev2 = add_revision(
        seed, world.workspace_id, world.listing_id, 2, make="Example", model="Trail", seller_country="DE"
    )
    updated = await open_case(db, world, revision_id=rev2)
    assert updated.action == "updated" and updated.case_version == 3 and updated.state == ReviewState.CLAIMED
    assert not updated.event_created  # a claimed case is not re-signalled
    with pytest.raises(VersionConflict):  # the claim cannot decide on the old version
        await run(db, actor, lambda c: reviews_repo.submit(c, actor, submit_request(grant)))
    regrant = await claim_case(db, actor, case_id, 3)
    assert regrant.rotated and regrant.listing_revision == 2 and regrant.revision_id == rev2
    decision = await run(db, actor, lambda c: reviews_repo.submit(c, actor, submit_request(regrant)))
    assert decision.listing_revision == 2 and decision.listing_revision_id == rev2
    # A newer revision of a decided case returns it to pending with a new signal; the
    # previous decision stays in history.
    rev3 = add_revision(
        seed, world.workspace_id, world.listing_id, 3, make="Example", model="Trail", seller_country="DE"
    )
    reopened = await open_case(db, world, revision_id=rev3)
    assert reopened.state == ReviewState.PENDING and reopened.event_created
    assert reopened.case_version == 6
    dedups = [e["dedup_key"] for e in outbox_rows(seed, world.workspace_id)]
    assert dedups == [f"review.pending:{case_id}:1", f"review.pending:{case_id}:6"]
    assert decision_count(seed, case_id) == 1
    # A late, older revision changes nothing.
    stale = await open_case(db, world, revision_id=rev2)
    assert stale.action == "stale_revision"
    assert case_row(seed, case_id)["row_version"] == 6


async def test_revision_that_no_longer_qualifies_supersedes_the_case(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    created = await open_case(db, world)
    case_id = created.case_id
    assert case_id is not None
    actor = reviewer(world.workspace_id)
    grant = await claim_case(db, actor, case_id, 1)
    rev2 = add_revision(
        seed, world.workspace_id, world.listing_id, 2, make="Example", model="Trail", seller_country="DE"
    )
    set_eligibility(seed, world.workspace_id, world.listing_id, "rejected")  # ingest screened rev 2
    closed = await open_case(
        db, world, revision_id=rev2, screening_result=screening(EligibilityState.REJECTED)
    )
    assert closed.action == "superseded" and closed.state == ReviewState.SUPERSEDED
    row = case_row(seed, case_id)
    assert row["state"] == "superseded" and row["claim_holder"] is None and row["reason"]
    with pytest.raises(VersionConflict):
        await run(db, actor, lambda c: reviews_repo.submit(c, actor, submit_request(grant)))
    with pytest.raises(VersionConflict):
        await claim_case(db, actor, case_id, 3)
    # A later qualifying revision opens a NEW case for the same (listing, profile).
    rev3 = add_revision(
        seed, world.workspace_id, world.listing_id, 3, make="Example", model="Trail", seller_country="DE"
    )
    set_eligibility(seed, world.workspace_id, world.listing_id, "eligible_primary", "primary")
    fresh = await open_case(db, world, revision_id=rev3)
    assert fresh.action == "created" and fresh.case_id != case_id


# --------------------------------------------------------------------------------------------
# Stable queue pagination
# --------------------------------------------------------------------------------------------


async def test_queue_pages_are_stable_under_concurrent_changes(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    ws = world.workspace_id
    case_ids: list[uuid.UUID] = []
    first = await open_case(db, world)
    assert first.case_id is not None
    case_ids.append(first.case_id)
    for _ in range(4):
        listing, rev = make_listing(seed, ws, world.source_id)
        result = await open_case(db, world, listing_id=listing, revision_id=rev)
        assert result.case_id is not None
        case_ids.append(result.case_id)
    for priority, case_id in zip((50, 40, 30, 20, 10), case_ids, strict=True):
        seed.conn.execute("update app.review_cases set priority = %s where id = %s", (priority, case_id))
    actor = reviewer(ws)
    filters = reviews_repo.ReviewQueueFilters()

    async def page(cursor: str | None, who: Any = actor) -> reviews_repo.ReviewQueueResult:
        return await run(
            db,
            who,
            lambda c: reviews_repo.list_pending_queue(c, who, filters, cursor, secret=CURSOR_SECRET, limit=2),
        )

    page1 = await page(None)
    assert [i.case_id for i in page1.page.items] == case_ids[:2] and page1.page.total == 5
    assert page1.next_cursor is not None
    # Between pages: reprioritise, change a status, delete one case and insert a new one.
    seed.conn.execute("update app.review_cases set priority = 1000 where id = %s", (case_ids[4],))
    await claim_case(db, reviewer(ws), case_ids[2], 1)
    seed.conn.execute("delete from app.review_cases where id = %s", (case_ids[3],))
    listing, rev = make_listing(seed, ws, world.source_id)
    inserted = await open_case(db, world, listing_id=listing, revision_id=rev)
    page2 = await page(page1.next_cursor)
    assert [i.case_id for i in page2.page.items] == case_ids[2:4]
    assert page2.page.items[0].state == ReviewState.PENDING  # the frozen projection
    page3 = await page(page2.next_cursor)
    assert [i.case_id for i in page3.page.items] == case_ids[4:] and page3.next_cursor is None
    assert page3.page.items[0].priority == 10
    # The cursor is bound to its principal.
    with pytest.raises(ValidationFailed):
        await page(page1.next_cursor, reviewer(ws))
    # A fresh query sees the changes.
    fresh = await run(
        db,
        actor,
        lambda c: reviews_repo.list_pending_queue(c, actor, filters, None, secret=CURSOR_SECRET, limit=10),
    )
    ids = [i.case_id for i in fresh.page.items]
    assert ids[0] == case_ids[4] and case_ids[3] not in ids and inserted.case_id in ids
    claimed = next(i for i in fresh.page.items if i.case_id == case_ids[2])
    assert claimed.state == ReviewState.CLAIMED and claimed.claim.claimed and not claimed.claim.held_by_caller


async def test_reviews_are_workspace_isolated(db: Database, world: RealWorld, other_world: RealWorld) -> None:
    created = await open_case(db, world)
    case_id = created.case_id
    assert case_id is not None
    foreign = reviewer(other_world.workspace_id)
    with pytest.raises(NotFound):
        await run(db, foreign, lambda c: reviews_repo.get_case(c, foreign, case_id))
    with pytest.raises(NotFound):
        await claim_case(db, foreign, case_id, 1)
    queue = await run(
        db,
        foreign,
        lambda c: reviews_repo.list_pending_queue(
            c, foreign, reviews_repo.ReviewQueueFilters(), None, secret=CURSOR_SECRET
        ),
    )
    assert queue.page.items == () and queue.page.total == 0
    foreign_system = system(other_world.workspace_id)
    with pytest.raises(NotFound):  # a listing of another workspace cannot get a case here
        await run(
            db,
            foreign_system,
            lambda c: reviews_repo.upsert_review_case(
                c,
                foreign_system,
                world.listing_id,
                world.revision_id,
                screening(),
                None,
                primary_profile(),
                dashboard_base_url="https://dashboard.synthetic.example",
            ),
        )
    with pytest.raises(Forbidden):  # reviewers never create cases
        await run(
            db,
            reviewer(world.workspace_id),
            lambda c: reviews_repo.upsert_review_case(
                c,
                reviewer(world.workspace_id),
                world.listing_id,
                world.revision_id,
                screening(),
                None,
                primary_profile(),
                dashboard_base_url="https://dashboard.synthetic.example",
            ),
        )


# --------------------------------------------------------------------------------------------
# Valued cases, shortlist guard and the authorized owner alert
# --------------------------------------------------------------------------------------------


async def _approved_route(db: Database, world: RealWorld, category: bindings_repo.EventCategory) -> Any:
    own = owner(world.workspace_id)

    async def go(conn: Conn) -> Any:
        binding = await bindings_repo.create_binding(
            conn,
            own,
            provider="slack",
            label="SYNTHETIC private channel",
            external_workspace_id="T0SYNTH",
            external_channel_id="C0SYNTH",
        )
        binding = await bindings_repo.approve_binding(
            conn, own, binding.id, approval_reference="SYNTHETIC owner approval", expected_version=1
        )
        binding = await bindings_repo.set_binding_enabled(conn, own, binding.id, True, expected_version=2)
        prefs = await bindings_repo.upsert_preferences(conn, own, binding.id, event_categories=[category])
        prefs = await bindings_repo.approve_preferences(
            conn, own, prefs.id, approval_reference="SYNTHETIC owner approval", expected_version=1
        )
        await bindings_repo.set_preferences_enabled(conn, own, prefs.id, True, expected_version=2)
        return binding

    return await run(db, own, go)


async def test_shortlist_needs_current_valuation_and_alerts_only_through_an_active_route(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    bundle = await complete_valuation(db, world, as_of=now)
    sys_actor = system(world.workspace_id)
    stored = await run(
        db,
        sys_actor,
        lambda c: valuation_repo.persist_valuation(
            c, sys_actor, bundle.valuation, bundle.refs, bundle.inputs
        ),
    )
    created = await open_case(db, world, valuation_id=stored.id)
    case_id = created.case_id
    assert case_id is not None and created.readiness not in ("not_valued", "valuation_stale")
    actor = reviewer(world.workspace_id)
    evidence = (bundle.comparable.result.selected[0].observation_id,)
    grant = await claim_case(db, actor, case_id, 1)
    with pytest.raises(ValidationFailed):  # a shortlist cites evidence
        await run(
            db,
            actor,
            lambda c: reviews_repo.submit(c, actor, submit_request(grant, outcome=ReviewOutcome.SHORTLISTED)),
        )
    first = await run(
        db,
        actor,
        lambda c: reviews_repo.submit(
            c,
            actor,
            submit_request(grant, outcome=ReviewOutcome.SHORTLISTED, evidence_ids=evidence),
            dashboard_base_url="https://dashboard.synthetic.example",
        ),
    )
    assert first.outcome == ReviewOutcome.SHORTLISTED and first.valuation_id == stored.id
    assert outbox_rows(seed, world.workspace_id, "review.shortlisted") == []  # no approved route
    binding = await _approved_route(db, world, "owner_alert")
    regrant = await claim_case(db, actor, case_id, 3)
    second = await run(
        db,
        actor,
        lambda c: reviews_repo.submit(
            c,
            actor,
            submit_request(regrant, outcome=ReviewOutcome.SHORTLISTED, evidence_ids=evidence),
            dashboard_base_url="https://dashboard.synthetic.example",
        ),
    )
    [alert] = outbox_rows(seed, world.workspace_id, "review.shortlisted")
    assert alert["destination_binding_id"] == binding.id and alert["state"] == "pending"
    assert alert["dedup_key"] == f"review.shortlisted:{second.decision_id}" and not alert["is_fixture"]
    assert alert["payload"]["schema_version"] == "1.0" and alert["event_version"] == 1
    # A stale valuation blocks a further shortlist (spec 18 pre-decision validation).
    await run(
        db,
        sys_actor,
        lambda c: valuation_repo.mark_stale(c, sys_actor, stored.id, InvalidationReason.FX),
    )
    third = await claim_case(db, actor, case_id, 5)
    with pytest.raises(VersionConflict):
        await run(
            db,
            actor,
            lambda c: reviews_repo.submit(
                c, actor, submit_request(third, outcome=ReviewOutcome.SHORTLISTED, evidence_ids=evidence)
            ),
        )


async def test_non_fixture_case_cannot_cite_a_fixture_valuation(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    fixture_valuation = seed.valuation(world.workspace_id, world.listing_id, world.revision_id)
    with pytest.raises(NotFound):  # composite lookup: the valuation must belong to the listing revision
        await open_case(db, world, valuation_id=uuid.uuid4())
    with pytest.raises(ValidationFailed):  # fixture lineage guard (SV003) or fixture mismatch
        await open_case(db, world, valuation_id=fixture_valuation)
    assert (
        seed.scalar("select count(*) from app.review_cases where listing_id = %s", (world.listing_id,)) == 0
    )
