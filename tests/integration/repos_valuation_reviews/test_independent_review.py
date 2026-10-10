"""Regression tests from the independent review of WP7b2 (races, late results, access rechecks).

Each test pins one defect found in review; SYNTHETIC data only, everything under test runs as
``suv_backend`` under RLS.
"""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb
from tests.integration.db.helpers import Seed, sha, unique
from tests.integration.repos_valuation_reviews.builders import (
    T0,
    RealWorld,
    add_revision,
    business_config,
    case_row,
    complete_valuation,
    make_listing,
    observation,
    open_case,
    owner,
    reviewer,
    run,
    screening,
    set_eligibility,
    store_simple_valuation,
    system,
    target,
    viewer,
)

from suv_deals.clock import FrozenClock
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.comparables import select_comparables
from suv_deals.domain.costs import CostScope
from suv_deals.domain.enums import CostCategory, EligibilityState, JobState, TaxRuleStatus, ValuationState
from suv_deals.domain.money import Money
from suv_deals.domain.valuation import InvalidationReason
from suv_deals.errors import NotFound, ValidationFailed, VersionConflict
from suv_deals.integrations import event_bridge as eb
from suv_deals.integrations.secret_box import SecretBox
from suv_deals.integrations.webhook_signing import parse_whsec
from suv_deals.persistence import market_repo, reviews_repo, subscriptions_repo, valuation_repo
from suv_deals.persistence.database import Conn, Database
from suv_deals.persistence.valuation_repo import CostEvidenceInput, DependencyChange

pytestmark = pytest.mark.db

CALLBACK = "https://callback.synthetic.example/hooks/review-independent"


# --------------------------------------------------------------------------------------------
# Review cases: late results never regress current facts
# --------------------------------------------------------------------------------------------


async def test_late_older_or_missing_valuation_never_regresses_the_case(
    db: Database, world: RealWorld
) -> None:
    older = await store_simple_valuation(db, world)
    newer = await store_simple_valuation(db, world)
    created = await open_case(db, world, valuation_id=older.id)
    assert created.case_id is not None
    updated = await open_case(db, world, valuation_id=newer.id)
    assert updated.action == "updated" and updated.case_version == 2
    # A late (retried) job offers the OLDER valuation of the same revision: nothing changes.
    late = await open_case(db, world, valuation_id=older.id)
    assert late.action == "stale_valuation" and late.case_version == 2
    # A caller without a valuation (e.g. a re-screening) never clears the current one.
    without = await open_case(db, world, valuation_id=None)
    assert without.action == "unchanged" and without.case_version == 2
    view = await run(
        db,
        viewer(world.workspace_id),
        lambda c: reviews_repo.get_case(c, viewer(world.workspace_id), created.case_id),  # type: ignore[arg-type]
    )
    assert view.valuation is not None and view.valuation.valuation_id == newer.id
    assert view.case_version == 2


async def test_a_late_screening_never_reopens_or_closes_a_case(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    ws = world.workspace_id
    created = await open_case(db, world)
    assert created.action == "created"
    # Ingest re-screens the current revision (e.g. a new configuration) and rejects it.
    set_eligibility(seed, ws, world.listing_id, "rejected")
    closed = await open_case(db, world, screening_result=screening(EligibilityState.REJECTED))
    assert closed.action == "superseded"
    # A late retry carrying the EARLIER eligible screening must not open a new case.
    late = await open_case(db, world)
    assert late.action == "stale_screening" and late.case_id is None
    open_cases = seed.scalar(
        "select count(*) from app.review_cases where listing_id = %s and state <> 'superseded'",
        (world.listing_id,),
    )
    assert open_cases == 0
    # The reverse: the committed screening is eligible again; a late rejection never closes it.
    set_eligibility(seed, ws, world.listing_id, "eligible_primary", "primary")
    reopened = await open_case(db, world)
    assert reopened.action == "created" and reopened.case_id is not None
    late_reject = await open_case(db, world, screening_result=screening(EligibilityState.REJECTED))
    assert late_reject.action == "stale_screening"
    assert case_row(seed, reopened.case_id)["state"] == "pending"


async def test_upsert_without_a_rank_keeps_the_current_priority(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    created = await open_case(db, world)
    assert created.case_id is not None
    seed.conn.execute(
        "update app.review_cases set priority = 750, ranking = %s, ranking_version = 'ranking@test'"
        " where id = %s",
        (_jsonb({"total": "7.5", "scoring_version": "ranking@test"}), created.case_id),
    )
    again = await open_case(db, world)
    assert again.action == "unchanged"
    row = case_row(seed, created.case_id)
    assert (row["priority"], row["ranking_version"]) == (750, "ranking@test")
    # A valuation update without a rank keeps the priority too.
    stored = await store_simple_valuation(db, world)
    valued = await open_case(db, world, valuation_id=stored.id)
    assert valued.action == "updated"
    row = case_row(seed, created.case_id)
    assert (row["priority"], row["ranking"]["total"]) == (750, "7.5")


def _jsonb(value: dict[str, Any]) -> Jsonb:
    return Jsonb(value)


async def test_case_view_marks_a_stale_valuation_as_research_only(db: Database, world: RealWorld) -> None:
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
    assert created.case_id is not None
    reader = viewer(world.workspace_id)
    fresh = await run(db, reader, lambda c: reviews_repo.get_case(c, reader, created.case_id))  # type: ignore[arg-type]
    assert fresh.valuation is not None and not fresh.valuation.research_candidate
    await run(
        db, sys_actor, lambda c: valuation_repo.mark_stale(c, sys_actor, stored.id, InvalidationReason.FX)
    )
    stale = await run(db, reader, lambda c: reviews_repo.get_case(c, reader, created.case_id))  # type: ignore[arg-type]
    assert stale.valuation is not None and stale.valuation.state == ValuationState.STALE
    assert stale.valuation.research_candidate and stale.candidate.research_candidate


# --------------------------------------------------------------------------------------------
# Valuations: no lost invalidation
# --------------------------------------------------------------------------------------------


async def test_mark_stale_queues_one_deduplicated_recomputation(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    sys_actor = system(world.workspace_id)
    first, second = await store_simple_valuation(db, world), await store_simple_valuation(db, world)
    marked = await run(
        db,
        sys_actor,
        lambda c: valuation_repo.mark_stale(c, sys_actor, first.id, InvalidationReason.FRESHNESS_DEADLINE),
    )
    assert marked.changed and marked.recompute_job_id is not None
    job = seed.conn.execute(
        "select job_type, state, dedup_key, listing_id from ops.jobs where id = %s",
        (marked.recompute_job_id,),
    ).fetchone()
    assert job == ("valuation", "queued", f"valuation.recompute:{world.listing_id}", world.listing_id)
    again = await run(
        db, sys_actor, lambda c: valuation_repo.mark_stale(c, sys_actor, first.id, InvalidationReason.FX)
    )
    assert not again.changed and again.recompute_job_id is None
    other = await run(
        db, sys_actor, lambda c: valuation_repo.mark_stale(c, sys_actor, second.id, InvalidationReason.FX)
    )
    assert other.recompute_job_id == marked.recompute_job_id  # one open job per listing


async def test_concurrent_tax_revoke_cannot_miss_a_valuation_being_persisted(
    db: Database, world: RealWorld
) -> None:
    bundle = await complete_valuation(db, world)
    ws = world.workspace_id
    sys_actor, own = system(ws), owner(ws)
    rule = bundle.rule_set.rule_set
    persisted = asyncio.Event()

    async def slow_persist(conn: Conn) -> valuation_repo.StoredValuation:
        stored = await valuation_repo.persist_valuation(
            conn, sys_actor, bundle.valuation, bundle.refs, bundle.inputs
        )
        persisted.set()
        await asyncio.sleep(0.4)  # the owner revokes the rule set before this commits
        return stored

    async def revoke() -> Any:
        await persisted.wait()
        return await run(
            db,
            own,
            lambda c: valuation_repo.transition_tax_rule_set(
                c, own, rule.rule_set_id, rule.version, TaxRuleStatus.REVOKED
            ),
        )

    stored, revoked = await asyncio.gather(run(db, sys_actor, slow_persist), revoke())
    assert revoked.rule_set.status == TaxRuleStatus.REVOKED
    loaded = await run(db, sys_actor, lambda c: valuation_repo.load_valuation(c, sys_actor, stored.id))
    assert loaded.valuation.state == ValuationState.STALE  # never an estimated valuation on revoked rules


async def test_concurrent_cost_profile_approval_cannot_miss_a_valuation_being_persisted(
    db: Database, world: RealWorld
) -> None:
    bundle = await complete_valuation(db, world)  # its cost profile is still unapproved
    ws = world.workspace_id
    sys_actor, own = system(ws), owner(ws)
    persisted = asyncio.Event()

    async def slow_persist(conn: Conn) -> valuation_repo.StoredValuation:
        stored = await valuation_repo.persist_valuation(
            conn, sys_actor, bundle.valuation, bundle.refs, bundle.inputs
        )
        persisted.set()
        await asyncio.sleep(0.4)  # the owner approves the profile before this commits
        return stored

    async def approve() -> Any:
        await persisted.wait()
        return await run(db, own, lambda c: valuation_repo.approve_cost_profile(c, own, bundle.profile.id))

    stored, approved = await asyncio.gather(run(db, sys_actor, slow_persist), approve())
    assert approved.profile.approval_status == "approved"
    loaded = await run(db, sys_actor, lambda c: valuation_repo.load_valuation(c, sys_actor, stored.id))
    assert loaded.valuation.state == ValuationState.STALE  # its profile reference is outdated


async def test_superseded_or_untracked_cost_evidence_is_refused(db: Database, world: RealWorld) -> None:
    sys_actor = system(world.workspace_id)
    quote = CostEvidenceInput(
        kind="quote",
        category=CostCategory.TRANSPORT,
        provider="SYNTHETIC transport provider",
        base=Money.of("700.00", "EUR"),
        obtained_at=T0,
        expires_at=T0 + timedelta(days=14),
        scope=CostScope(origin_country="DE", destination_city="Skopje", vehicle_running="running"),
    )
    stored = await run(db, sys_actor, lambda c: valuation_repo.insert_cost_evidence(c, sys_actor, quote))
    bundle = await complete_valuation(db, world, transport=stored)
    # The scenarios cite the quote, so the typed reference list must track it (otherwise a
    # superseding quote could never find this valuation).
    untracked = bundle.refs.model_copy(update={"cost_evidence_ids": ()})
    with pytest.raises(ValidationFailed):
        await run(
            db,
            sys_actor,
            lambda c: valuation_repo.persist_valuation(
                c, sys_actor, bundle.valuation, untracked, bundle.inputs
            ),
        )
    newer = quote.model_copy(update={"base": Money.of("720.00", "EUR"), "supersedes_id": stored.id})
    await run(db, sys_actor, lambda c: valuation_repo.insert_cost_evidence(c, sys_actor, newer))
    # A calculation that read the quote before it was superseded is born outdated: refused.
    with pytest.raises(VersionConflict):
        await run(
            db,
            sys_actor,
            lambda c: valuation_repo.persist_valuation(
                c, sys_actor, bundle.valuation, bundle.refs, bundle.inputs
            ),
        )


async def test_invalidation_during_a_running_recompute_queues_a_follow_up(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    ws = world.workspace_id
    sys_actor = system(ws)
    await store_simple_valuation(db, world)
    config_change = DependencyChange(
        reason=InvalidationReason.CONFIG, current_config_revision_id=seed.config_revision(ws)
    )
    first = await run(
        db, sys_actor, lambda c: valuation_repo.invalidate_dependents(c, sys_actor, config_change)
    )
    running = first.recompute_jobs[world.listing_id]
    # The recompute job is running (it read its inputs BEFORE the next change).
    seed.conn.execute(
        "update ops.jobs set state = 'running', lease_owner = 'SYNTHETIC-worker',"
        " lease_token = gen_random_uuid(), lease_expires_at = clock_timestamp() + interval '5 minutes',"
        " last_heartbeat_at = clock_timestamp(), attempts = 1 where id = %s",
        (running,),
    )
    open_valuation = await store_simple_valuation(db, world)
    rev2 = add_revision(seed, ws, world.listing_id, 2, make="Example", model="Trail")
    change = DependencyChange(
        reason=InvalidationReason.LISTING_REVISION, listing_id=world.listing_id, current_revision_id=rev2
    )
    second = await run(db, sys_actor, lambda c: valuation_repo.invalidate_dependents(c, sys_actor, change))
    assert second.stale_valuation_ids == (open_valuation.id,)
    follow_up = second.recompute_jobs[world.listing_id]
    assert follow_up != running and second.jobs_created == 1
    row = seed.conn.execute("select state, dedup_key from ops.jobs where id = %s", (follow_up,)).fetchone()
    assert row == (JobState.QUEUED.value, f"valuation.recompute:{world.listing_id}:after:{running}")
    # Further invalidations while the same job runs reuse the queued follow-up.
    another = await store_simple_valuation(db, world)
    third = await run(db, sys_actor, lambda c: valuation_repo.invalidate_dependents(c, sys_actor, change))
    assert third.stale_valuation_ids == (another.id,) and third.jobs_created == 0
    assert third.recompute_jobs[world.listing_id] == follow_up


# --------------------------------------------------------------------------------------------
# Event subscriptions: access rechecks and fixture isolation
# --------------------------------------------------------------------------------------------


@pytest.fixture
def box() -> SecretBox:
    return SecretBox({1: os.urandom(32)}, 1)


def _secret() -> str:
    return "whsec_" + base64.b64encode(os.urandom(32)).decode("ascii")


def _member(seed: Seed, ws: UUID) -> ActorContext:
    user = seed.user()
    seed.membership(ws, user, "reviewer")
    return reviewer(ws, user, kind="user")


def _credential(seed: Seed, ws: UUID, principal: UUID, kind: str = "user") -> UUID:
    return seed.insert_id(
        "ops.api_credentials",
        workspace_id=ws,
        principal_id=principal,
        principal_kind=kind,
        role="reviewer",
        credential_kind="static_bearer",
        token_hash=sha(unique("token")),
        scopes=["reviews:read", "events:subscribe"],
        label="SYNTHETIC credential",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )


async def _active_subscription(
    db: Database, actor: ActorContext, box: SecretBox, *, credential_id: UUID | None = None
) -> subscriptions_repo.SubscriptionRecord:
    secret = _secret()
    params = {
        "name": eb.EVENT_NAME,
        "arguments": {"profile": "primary"},
        "delivery": {"mode": "webhook", "url": CALLBACK, "secret": secret},
        "ttlMs": 3_600_000,
    }
    request = eb.validate_subscribe_params(params, actor, clock=FrozenClock(datetime.now(UTC)))
    created = await run(
        db,
        actor,
        lambda c: subscriptions_repo.create_or_refresh_subscription(
            c, actor, request, box, credential_id=credential_id
        ),
    )
    now = datetime.now(UTC)
    result = eb.VerificationResult(
        ok=True,
        reason=None,
        detail=None,
        status_code=200,
        webhook_id="msg_SYNTHETIC",
        attempted_at=now,
        verified_at=now,
        secret_fingerprint=parse_whsec(secret).fingerprint,
    )
    return await run(
        db, actor, lambda c: subscriptions_repo.record_verification(c, actor, created.record.id, result, box)
    )


async def test_user_credential_subscription_needs_an_active_membership(
    db: Database, seed: Seed, world: RealWorld, box: SecretBox
) -> None:
    ws = world.workspace_id
    member = _member(seed, ws)
    credential = _credential(seed, ws, member.principal_id)
    record = await _active_subscription(db, member, box, credential_id=credential)
    sys_actor = system(ws)
    found = await run(
        db, sys_actor, lambda c: subscriptions_repo.list_active_for_event(c, sys_actor, eb.EVENT_NAME, box)
    )
    assert [r.id for r in found.records] == [record.id]
    # The member is removed from the workspace; the still-valid static credential must not
    # keep delivering workspace events to them.
    seed.conn.execute(
        "update app.memberships set active = false where workspace_id = %s and user_id = %s",
        (ws, member.principal_id),
    )
    later = await run(
        db, sys_actor, lambda c: subscriptions_repo.list_active_for_event(c, sys_actor, eb.EVENT_NAME, box)
    )
    assert later.targets == () and later.revoked == (record.id,)


async def test_waiting_deliveries_are_not_leased_after_access_was_lost(
    db: Database, seed: Seed, world: RealWorld, box: SecretBox
) -> None:
    ws = world.workspace_id
    member = _member(seed, ws)
    record = await _active_subscription(db, member, box)
    listing, rev = make_listing(seed, ws, world.source_id)
    created = await open_case(db, world, listing_id=listing, revision_id=rev)
    assert created.event_id is not None
    sys_actor = system(ws)
    await run(
        db,
        sys_actor,
        lambda c: subscriptions_repo.create_deliveries(c, sys_actor, created.event_id, [record.id]),  # type: ignore[arg-type]
    )
    seed.conn.execute(
        "update app.memberships set active = false where workspace_id = %s and user_id = %s",
        (ws, member.principal_id),
    )
    claimed = await run(
        db, sys_actor, lambda c: subscriptions_repo.claim_due_deliveries(c, sys_actor, "dispatcher-1")
    )
    assert claimed == []
    # Access restored: the waiting delivery is leased again (nothing was lost).
    seed.conn.execute(
        "update app.memberships set active = true where workspace_id = %s and user_id = %s",
        (ws, member.principal_id),
    )
    again = await run(
        db, sys_actor, lambda c: subscriptions_repo.claim_due_deliveries(c, sys_actor, "dispatcher-1")
    )
    assert [d.subscription_id for d in again] == [record.id]


async def test_fixture_events_never_get_delivery_records(
    db: Database, seed: Seed, world: RealWorld, box: SecretBox
) -> None:
    ws = world.workspace_id
    member = _member(seed, ws)
    record = await _active_subscription(db, member, box)
    fixture_source = seed.source(ws)  # mode 'fixture'
    listing, rev = make_listing(seed, ws, fixture_source)
    created = await open_case(db, world, listing_id=listing, revision_id=rev)
    assert created.event_id is not None
    sys_actor = system(ws)
    with pytest.raises(ValidationFailed):
        await run(
            db,
            sys_actor,
            lambda c: subscriptions_repo.create_deliveries(c, sys_actor, created.event_id, [record.id]),  # type: ignore[arg-type]
        )
    with pytest.raises(NotFound):  # an unknown (or foreign) event id
        await run(
            db,
            sys_actor,
            lambda c: subscriptions_repo.create_deliveries(c, sys_actor, uuid.uuid4(), [record.id]),
        )
    assert (
        seed.scalar("select count(*) from ops.event_deliveries where subscription_id = %s", (record.id,)) == 0
    )


# --------------------------------------------------------------------------------------------
# Comparable sets: selected evidence is exactly what is stored, never fixture-contaminated
# --------------------------------------------------------------------------------------------


async def test_comparable_selection_must_match_stored_real_observations(
    db: Database, world: RealWorld
) -> None:
    ws = world.workspace_id
    sys_actor = system(ws)
    real = [observation(world.source_key, a) for a in ("8800.00", "9000.00", "9200.00")]
    fixture_obs = observation(world.source_key, "9100.00", is_fixture=True)

    async def setup(conn: Conn) -> Any:
        for item in [*real, fixture_obs]:
            await market_repo.insert_market_observation(
                conn, sys_actor, item, confidence="medium", source_id=world.source_id
            )
        return select_comparables(target(world.listing_id), real, business_config(), as_of=T0)

    result = await run(db, sys_actor, setup)
    first = result.selected[0]

    async def persist(candidate: Any) -> Any:
        return await run(
            db,
            sys_actor,
            lambda c: market_repo.persist_comparable_set(
                c, sys_actor, candidate, listing_id=world.listing_id, target_revision_id=world.revision_id
            ),
        )

    # A hand-built result that selects FIXTURE evidence for a real target is refused.
    smuggled = first.model_copy(update={"observation_id": fixture_obs.id, "observation": fixture_obs})
    with pytest.raises(ValidationFailed):
        await persist(result.model_copy(update={"selected": (smuggled, *result.selected[1:])}))
    # A selected comparable whose embedded data differs from the stored row would never reload.
    altered = first.observation.model_copy(update={"amount": Money.of("1.00", "EUR")})
    with pytest.raises(ValidationFailed):
        await persist(
            result.model_copy(
                update={"selected": (first.model_copy(update={"observation": altered}), *result.selected[1:])}
            )
        )
    # An unknown observation id is NotFound.
    ghost = first.observation.model_copy(update={"id": uuid.uuid4()})
    with pytest.raises(NotFound):
        await persist(
            result.model_copy(
                update={
                    "selected": (
                        first.model_copy(update={"observation_id": ghost.id, "observation": ghost}),
                        *result.selected[1:],
                    )
                }
            )
        )
    stored = await persist(result)
    assert {s.observation_id for s in stored.result.selected} == {o.id for o in real}


async def test_a_late_verification_failure_never_overrides_a_newer_success(
    db: Database, seed: Seed, world: RealWorld, box: SecretBox
) -> None:
    ws = world.workspace_id
    member = _member(seed, ws)
    secret = _secret()
    params = {
        "name": eb.EVENT_NAME,
        "arguments": {"profile": "primary"},
        "delivery": {"mode": "webhook", "url": CALLBACK, "secret": secret},
        "ttlMs": 3_600_000,
    }
    request = eb.validate_subscribe_params(params, member, clock=FrozenClock(datetime.now(UTC)))
    created = await run(
        db, member, lambda c: subscriptions_repo.create_or_refresh_subscription(c, member, request, box)
    )
    started = datetime.now(UTC)
    fingerprint = parse_whsec(secret).fingerprint

    def result(ok: bool, attempted_at: datetime) -> eb.VerificationResult:
        return eb.VerificationResult(
            ok=ok,
            reason=None if ok else eb.CallbackErrorReason.TIMEOUT,
            detail=None,
            status_code=200 if ok else None,
            webhook_id="msg_SYNTHETIC",
            attempted_at=attempted_at,
            verified_at=attempted_at + timedelta(seconds=1) if ok else None,
            secret_fingerprint=fingerprint,
        )

    record_id = created.record.id
    newer = await run(
        db,
        member,
        lambda c: subscriptions_repo.record_verification(c, member, record_id, result(True, started), box),
    )
    assert newer.status is eb.SubscriptionStatus.ACTIVE
    # The outcome of an EARLIER attempt (it timed out) arrives last.
    late = await run(
        db,
        member,
        lambda c: subscriptions_repo.record_verification(
            c, member, record_id, result(False, started - timedelta(minutes=1)), box
        ),
    )
    assert late.status is eb.SubscriptionStatus.ACTIVE and late.version == newer.version
    # A genuinely newer failure is recorded.
    failed = await run(
        db,
        member,
        lambda c: subscriptions_repo.record_verification(
            c, member, record_id, result(False, started + timedelta(minutes=1)), box
        ),
    )
    assert (
        failed.status is eb.SubscriptionStatus.PENDING_VERIFICATION and failed.verification_state == "failed"
    )
