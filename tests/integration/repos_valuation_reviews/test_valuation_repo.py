"""Valuation, FX, tax-rule, cost and reverse-invalidation persistence (spec 16-18).

Everything runs as ``suv_backend`` under RLS with SYNTHETIC data only.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError
from tests.integration.db.helpers import Seed
from tests.integration.repos_valuation_reviews.builders import (
    T0,
    RealWorld,
    add_revision,
    complete_valuation,
    cost_profile,
    make_listing,
    mkd_rate,
    owner,
    review_record,
    reviewer,
    run,
    store_simple_valuation,
    synthetic_rule_set,
    system,
    viewer,
)

from suv_deals.domain.costs import CostScope
from suv_deals.domain.enums import CostCategory, FxPurpose, JobState, TaxRuleStatus, ValuationState
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.valuation import InvalidationReason
from suv_deals.errors import Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.persistence import valuation_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.valuation_repo import CostEvidenceInput, DependencyChange
from suv_deals.views.valuations import ValuationView

pytestmark = pytest.mark.db


# --------------------------------------------------------------------------------------------
# Valuation round trip
# --------------------------------------------------------------------------------------------


async def test_complete_valuation_round_trip_reconstructs_the_view(db: Database, world: RealWorld) -> None:
    bundle = await complete_valuation(db, world)
    assert bundle.valuation.state == ValuationState.ESTIMATED
    sys_actor = system(world.workspace_id)
    stored = await run(
        db,
        sys_actor,
        lambda c: valuation_repo.persist_valuation(
            c, sys_actor, bundle.valuation, bundle.refs, bundle.inputs
        ),
    )
    assert stored.valuation == bundle.valuation
    expected = ValuationView.of(
        bundle.valuation,
        valuation_id=stored.id,
        listing_id=world.listing_id,
        listing_revision=1,
        cost_lines=bundle.inputs.cost_lines,
        purchase=bundle.inputs.purchase,
        proceeds=bundle.inputs.proceeds,
    )
    reader = viewer(world.workspace_id)
    view = await run(db, reader, lambda c: valuation_repo.get_valuation_view(c, reader, stored.id))
    assert view == expected
    assert view.dependency_fingerprint == bundle.valuation.dependency_fingerprint
    assert view.tax is not None and view.tax.production_ready
    current = await run(db, reader, lambda c: valuation_repo.current_valuation(c, reader, world.listing_id))
    assert current is not None and current.id == stored.id


async def test_persist_refuses_references_that_differ_from_the_fingerprint(
    db: Database, world: RealWorld
) -> None:
    bundle = await complete_valuation(db, world)
    sys_actor = system(world.workspace_id)
    wrong_config = bundle.refs.model_copy(update={"config_revision_id": uuid.uuid4()})
    with pytest.raises(ValidationFailed):
        await run(
            db,
            sys_actor,
            lambda c: valuation_repo.persist_valuation(
                c, sys_actor, bundle.valuation, wrong_config, bundle.inputs
            ),
        )
    no_fx = bundle.refs.model_copy(update={"fx_rate_ids": ()})
    with pytest.raises(ValidationFailed):
        await run(
            db,
            sys_actor,
            lambda c: valuation_repo.persist_valuation(c, sys_actor, bundle.valuation, no_fx, bundle.inputs),
        )
    bad_inputs = bundle.inputs.model_copy(update={"import_line_sources": ()})
    with pytest.raises(ValidationFailed):
        await run(
            db,
            sys_actor,
            lambda c: valuation_repo.persist_valuation(
                c, sys_actor, bundle.valuation, bundle.refs, bad_inputs
            ),
        )


async def test_foreign_workspace_valuation_is_not_found(
    db: Database, world: RealWorld, other_world: RealWorld
) -> None:
    stored = await store_simple_valuation(db, world)
    foreign = viewer(other_world.workspace_id)
    with pytest.raises(NotFound):
        await run(db, foreign, lambda c: valuation_repo.load_valuation(c, foreign, stored.id))


async def test_reviewer_cannot_write_valuations(db: Database, world: RealWorld) -> None:
    actor = reviewer(world.workspace_id)
    with pytest.raises(Forbidden):
        await run(db, actor, lambda c: valuation_repo.upsert_fx_rate(c, actor, mkd_rate()))


# --------------------------------------------------------------------------------------------
# mark_stale and reverse invalidation
# --------------------------------------------------------------------------------------------


async def test_mark_stale_is_the_only_transition_and_idempotent(db: Database, world: RealWorld) -> None:
    stored = await store_simple_valuation(db, world)
    sys_actor = system(world.workspace_id)
    first = await run(
        db, sys_actor, lambda c: valuation_repo.mark_stale(c, sys_actor, stored.id, InvalidationReason.FX)
    )
    assert first.changed and first.state == ValuationState.STALE and first.stale_reason == "fx"
    again = await run(
        db, sys_actor, lambda c: valuation_repo.mark_stale(c, sys_actor, stored.id, InvalidationReason.CONFIG)
    )
    assert not again.changed and again.stale_reason == "fx"
    loaded = await run(db, sys_actor, lambda c: valuation_repo.load_valuation(c, sys_actor, stored.id))
    assert loaded.valuation.state == ValuationState.STALE and not loaded.valuation.alert_eligible
    view = loaded.view()
    assert view.state == ValuationState.STALE and view.stale_at is not None


async def test_listing_revision_change_marks_stale_and_queues_one_job_per_listing(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    first = await store_simple_valuation(db, world)
    second = await store_simple_valuation(db, world)
    new_rev = add_revision(seed, world.workspace_id, world.listing_id, 2, make="Example", model="Trail")
    sys_actor = system(world.workspace_id)
    change = DependencyChange(
        reason=InvalidationReason.LISTING_REVISION, listing_id=world.listing_id, current_revision_id=new_rev
    )
    result = await run(db, sys_actor, lambda c: valuation_repo.invalidate_dependents(c, sys_actor, change))
    assert set(result.stale_valuation_ids) == {first.id, second.id}
    assert result.jobs_created == 1 and set(result.recompute_jobs) == {world.listing_id}
    job_id = result.recompute_jobs[world.listing_id]
    job = seed.conn.execute(
        "select job_type, state, listing_id, dedup_key from ops.jobs where id = %s", (job_id,)
    ).fetchone()
    assert job == (
        "valuation",
        JobState.QUEUED.value,
        world.listing_id,
        f"valuation.recompute:{world.listing_id}",
    )
    # A repeated invalidation finds nothing open and queues nothing new (deduplicated).
    again = await run(db, sys_actor, lambda c: valuation_repo.invalidate_dependents(c, sys_actor, change))
    assert again.stale_valuation_ids == () and again.jobs_created == 0
    states = {
        r[0]
        for r in seed.conn.execute(
            "select state from app.valuations where listing_id = %s", (world.listing_id,)
        )
    }
    assert states == {"stale"}


async def test_config_change_queues_one_recompute_job_per_stale_valuation(
    db: Database, seed: Seed, world: RealWorld, other_world: RealWorld
) -> None:
    listing2, rev2 = make_listing(seed, world.workspace_id, world.source_id)
    second_world = dataclasses.replace(world, listing_id=listing2, revision_id=rev2)
    first = await store_simple_valuation(db, world)
    second = await store_simple_valuation(db, second_world)
    foreign = await store_simple_valuation(db, other_world)
    sys_actor = system(world.workspace_id)
    change = DependencyChange(
        reason=InvalidationReason.CONFIG, current_config_revision_id=seed.config_revision(world.workspace_id)
    )
    result = await run(db, sys_actor, lambda c: valuation_repo.invalidate_dependents(c, sys_actor, change))
    assert set(result.stale_valuation_ids) == {first.id, second.id} and result.jobs_created == 2
    payloads = {
        row[0]: row[1]
        for row in seed.conn.execute(
            "select listing_id, payload from ops.jobs where workspace_id = %s and job_type = 'valuation'",
            (world.workspace_id,),
        )
    }
    assert payloads[world.listing_id]["stale_valuation_ids"] == [str(first.id)]
    assert payloads[listing2]["stale_valuation_ids"] == [str(second.id)]
    again = await run(db, sys_actor, lambda c: valuation_repo.invalidate_dependents(c, sys_actor, change))
    assert again.jobs_created == 0 and again.stale_valuation_ids == ()
    # RLS: another workspace's valuations are never touched.
    untouched = await run(
        db,
        system(other_world.workspace_id),
        lambda c: valuation_repo.load_valuation(c, system(other_world.workspace_id), foreign.id),
    )
    assert untouched.valuation.state == ValuationState.NOT_STARTED
    with pytest.raises(NotFound):
        await run(
            db,
            sys_actor,
            lambda c: valuation_repo.mark_stale(c, sys_actor, foreign.id, InvalidationReason.FX),
        )


async def test_reverse_invalidation_by_each_dependency_kind(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    bundle = await complete_valuation(db, world)
    sys_actor = system(world.workspace_id)
    stored = await run(
        db,
        sys_actor,
        lambda c: valuation_repo.persist_valuation(
            c, sys_actor, bundle.valuation, bundle.refs, bundle.inputs
        ),
    )
    unrelated = await store_simple_valuation(db, world)
    changes = [
        DependencyChange(reason=InvalidationReason.FX, fx_rate_ids=(bundle.fx.id,)),
        DependencyChange(reason=InvalidationReason.COMPARABLES, comparable_set_ids=(bundle.comparable.id,)),
        DependencyChange(
            reason=InvalidationReason.COMPARABLES,
            market_observation_ids=(bundle.comparable.result.selected[0].observation_id,),
        ),
        DependencyChange(reason=InvalidationReason.COST_PROFILE, cost_profile_ids=(bundle.profile.id,)),
        DependencyChange(
            reason=InvalidationReason.TAX_RULE, tax_rule_set_ids=tuple(r.id for r in bundle.rule_set.rows)
        ),
    ]
    for change in changes:
        found = await run(
            db, sys_actor, lambda c, ch=change: valuation_repo.find_dependents(c, sys_actor, ch)
        )
        assert found == [stored.id], change.reason
    newer = mkd_rate().model_copy(update={"rate_date": date(2026, 10, 6), "rate": Decimal("61.6")})
    fx2, created = await run(db, sys_actor, lambda c: valuation_repo.upsert_fx_rate(c, sys_actor, newer))
    assert created
    result = await run(
        db,
        sys_actor,
        lambda c: valuation_repo.invalidate_dependents(c, sys_actor, DependencyChange.new_fx_rate(fx2)),
    )
    assert result.stale_valuation_ids == (stored.id,)
    remaining = await run(db, sys_actor, lambda c: valuation_repo.load_valuation(c, sys_actor, unrelated.id))
    assert remaining.valuation.state == ValuationState.NOT_STARTED
    # A new business configuration invalidates everything computed under another one.
    new_config = seed.config_revision(world.workspace_id)
    config_change = DependencyChange(reason=InvalidationReason.CONFIG, current_config_revision_id=new_config)
    config_result = await run(
        db, sys_actor, lambda c: valuation_repo.invalidate_dependents(c, sys_actor, config_change)
    )
    assert config_result.stale_valuation_ids == (unrelated.id,)


async def test_revoking_a_tax_rule_invalidates_dependents(db: Database, world: RealWorld) -> None:
    bundle = await complete_valuation(db, world)
    sys_actor = system(world.workspace_id)
    stored = await run(
        db,
        sys_actor,
        lambda c: valuation_repo.persist_valuation(
            c, sys_actor, bundle.valuation, bundle.refs, bundle.inputs
        ),
    )
    own = owner(world.workspace_id)
    rs = bundle.rule_set.rule_set
    revoked = await run(
        db,
        own,
        lambda c: valuation_repo.transition_tax_rule_set(
            c, own, rs.rule_set_id, rs.version, TaxRuleStatus.REVOKED
        ),
    )
    assert revoked.rule_set.status == TaxRuleStatus.REVOKED
    loaded = await run(db, sys_actor, lambda c: valuation_repo.load_valuation(c, sys_actor, stored.id))
    assert loaded.valuation.state == ValuationState.STALE
    assert loaded.valuation.stale_reason is not None and loaded.valuation.stale_reason.startswith("tax_rule")


# --------------------------------------------------------------------------------------------
# FX
# --------------------------------------------------------------------------------------------


async def test_fx_upsert_is_insert_or_return_and_never_overwrites(db: Database, world: RealWorld) -> None:
    sys_actor = system(world.workspace_id)
    rate = mkd_rate().model_copy(update={"rate": Decimal("61.5000")})
    stored, created = await run(db, sys_actor, lambda c: valuation_repo.upsert_fx_rate(c, sys_actor, rate))
    assert created and stored.rate.rate == Decimal("61.5") and str(stored.rate.rate) == "61.5"
    assert (stored.rate.base, stored.rate.quote, stored.rate.purpose) == ("EUR", "MKD", FxPurpose.REFERENCE)
    same, created_again = await run(
        db, sys_actor, lambda c: valuation_repo.upsert_fx_rate(c, sys_actor, rate)
    )
    assert not created_again and same.id == stored.id
    different = rate.model_copy(update={"rate": Decimal("62")})
    with pytest.raises(VersionConflict):
        await run(db, sys_actor, lambda c: valuation_repo.upsert_fx_rate(c, sys_actor, different))
    with pytest.raises(VersionConflict):
        await run(db, sys_actor, lambda c: valuation_repo.upsert_fx_rate(c, sys_actor, rate, is_fixture=True))
    # Direction is explicit: the inverse pair is a different observation.
    inverse = FxRate(
        base="MKD",
        quote="EUR",
        rate=Decimal("0.0162601626"),
        rate_date=rate.rate_date,
        retrieved_at=rate.retrieved_at,
        provider=rate.provider,
    )
    inv, inv_created = await run(
        db, sys_actor, lambda c: valuation_repo.upsert_fx_rate(c, sys_actor, inverse)
    )
    assert inv_created and inv.id != stored.id
    too_precise = rate.model_copy(update={"rate": Decimal("1.00000000001"), "provider": "SYNTHETIC precise"})
    with pytest.raises(ValidationFailed):
        await run(db, sys_actor, lambda c: valuation_repo.upsert_fx_rate(c, sys_actor, too_precise))
    latest = await run(
        db,
        sys_actor,
        lambda c: valuation_repo.latest_fx_rates(
            c,
            sys_actor,
            base="EUR",
            quote="MKD",
            purpose=FxPurpose.REFERENCE,
            on_or_before=date(2026, 12, 31),
        ),
    )
    assert [r.id for r in latest] == [stored.id]


# --------------------------------------------------------------------------------------------
# Tax rule sets
# --------------------------------------------------------------------------------------------


async def test_tax_rule_set_lifecycle_and_mapping(db: Database, seed: Seed, world: RealWorld) -> None:
    own = owner(world.workspace_id)
    draft = synthetic_rule_set()
    draft = draft.model_copy(update={"vehicle_categories": ("passenger_car", "off_road")})
    stored = await run(db, own, lambda c: valuation_repo.store_rule_set(c, own, draft))
    assert {r.vehicle_category for r in stored.rows} == {"passenger_car", "off_road"}
    assert stored.rule_set == draft
    keys = {
        r[0]
        for r in seed.conn.execute(
            "select rule_set_id from app.tax_rule_sets where workspace_id = %s", (world.workspace_id,)
        )
    }
    assert keys == {f"{draft.rule_set_id}::passenger_car", f"{draft.rule_set_id}::off_road"}
    rs, version = draft.rule_set_id, draft.version
    # Never auto-approved: approval needs the owner, an approver name and a bound review record.
    sys_actor = system(world.workspace_id)
    with pytest.raises(Forbidden):
        await run(
            db,
            sys_actor,
            lambda c: valuation_repo.transition_tax_rule_set(
                c, sys_actor, rs, version, TaxRuleStatus.UNDER_REVIEW
            ),
        )
    with pytest.raises(ValidationFailed):
        await run(
            db,
            own,
            lambda c: valuation_repo.transition_tax_rule_set(c, own, rs, version, TaxRuleStatus.APPROVED),
        )
    review = await run(
        db,
        own,
        lambda c: valuation_repo.transition_tax_rule_set(c, own, rs, version, TaxRuleStatus.UNDER_REVIEW),
    )
    assert review.rule_set.sha256 is not None
    with pytest.raises(ValidationFailed):
        await run(
            db,
            own,
            lambda c: valuation_repo.transition_tax_rule_set(c, own, rs, version, TaxRuleStatus.APPROVED),
        )
    approved = await run(
        db,
        own,
        lambda c: valuation_repo.transition_tax_rule_set(
            c,
            own,
            rs,
            version,
            TaxRuleStatus.APPROVED,
            approved_by="SYNTHETIC owner",
            review_record=review_record(draft),
        ),
    )
    assert approved.rule_set.status == TaxRuleStatus.APPROVED
    assert approved.rule_set.approved_by == "SYNTHETIC owner"
    assert approved.approved_by_principal == own.principal_id
    assert approved.rule_set.review_record == review_record(draft)
    row = seed.conn.execute(
        "select approved_by, approval_reference, status from app.tax_rule_sets where rule_set_id = %s",
        (f"{rs}::passenger_car",),
    ).fetchone()
    assert row is not None and row[0] == own.principal_id and '"approver":"SYNTHETIC owner"' in row[1]
    # Approval never activates; activation is a separate explicit step.
    assert approved.rule_set.status != TaxRuleStatus.ACTIVE
    with pytest.raises(ValidationFailed):  # domain + trigger: approved -> draft is not a transition
        await run(
            db,
            own,
            lambda c: valuation_repo.transition_tax_rule_set(c, own, rs, version, TaxRuleStatus.DRAFT),
        )
    active = await run(
        db, own, lambda c: valuation_repo.transition_tax_rule_set(c, own, rs, version, TaxRuleStatus.ACTIVE)
    )
    assert active.rule_set.status == TaxRuleStatus.ACTIVE
    listed = await run(
        db, own, lambda c: valuation_repo.list_rule_sets(c, own, statuses=(TaxRuleStatus.ACTIVE,))
    )
    assert [s.rule_set.label() for s in listed] == [draft.label()]


async def test_only_drafts_are_stored_and_fixture_rule_sets_never_approve(
    db: Database, world: RealWorld
) -> None:
    own = owner(world.workspace_id)
    approved_shape = synthetic_rule_set().model_copy(update={"status": TaxRuleStatus.APPROVED})
    with pytest.raises(ValidationFailed):
        await run(db, own, lambda c: valuation_repo.store_rule_set(c, own, approved_shape))
    fixture = synthetic_rule_set().model_copy(update={"is_fixture": True})
    await run(db, own, lambda c: valuation_repo.store_rule_set(c, own, fixture))
    with pytest.raises(ValidationFailed):
        await run(
            db,
            own,
            lambda c: valuation_repo.transition_tax_rule_set(
                c, own, fixture.rule_set_id, fixture.version, TaxRuleStatus.UNDER_REVIEW
            ),
        )
    reviewer_actor = reviewer(world.workspace_id)
    with pytest.raises(Forbidden):
        await run(
            db,
            reviewer_actor,
            lambda c: valuation_repo.store_rule_set(c, reviewer_actor, synthetic_rule_set()),
        )


# --------------------------------------------------------------------------------------------
# Cost profiles and evidence
# --------------------------------------------------------------------------------------------


async def test_cost_profile_round_trip_and_owner_approval(db: Database, world: RealWorld) -> None:
    sys_actor = system(world.workspace_id)
    profile = cost_profile()
    stored = await run(db, sys_actor, lambda c: valuation_repo.store_cost_profile(c, sys_actor, profile))
    assert stored.profile == profile and stored.profile.reference() == profile.reference()
    with pytest.raises(Forbidden):
        await run(db, sys_actor, lambda c: valuation_repo.approve_cost_profile(c, sys_actor, stored.id))
    own = owner(world.workspace_id)
    approved = await run(db, own, lambda c: valuation_repo.approve_cost_profile(c, own, stored.id))
    assert approved.profile.approval_status == "approved"
    assert approved.profile.approved_by == f"principal:{own.principal_id}"
    reloaded = await run(db, own, lambda c: valuation_repo.load_cost_profile(c, own, stored.id))
    assert reloaded.profile.reference() == approved.profile.reference()
    with pytest.raises(VersionConflict):
        await run(db, own, lambda c: valuation_repo.approve_cost_profile(c, own, stored.id))
    unapproved = profile.model_copy(
        update={"approval_status": "approved", "approved_by": "x", "approved_at": T0}
    )
    with pytest.raises(ValidationFailed):
        await run(db, sys_actor, lambda c: valuation_repo.store_cost_profile(c, sys_actor, unapproved))


async def test_cost_evidence_keeps_hash_and_supersede_invalidates(db: Database, world: RealWorld) -> None:
    sys_actor = system(world.workspace_id)
    quote = CostEvidenceInput(
        kind="quote",
        category=CostCategory.TRANSPORT,
        provider="SYNTHETIC transport provider",
        base=Money.of("700.00", "EUR"),
        high=Money.of("750.00", "EUR"),
        obtained_at=T0,
        expires_at=T0 + timedelta(days=14),
        scope=CostScope(origin_country="DE", destination_city="Skopje", vehicle_running="running"),
        document_sha256="ab" * 32,
        details={"reference": "SYNTHETIC-Q-1"},
    )
    stored = await run(db, sys_actor, lambda c: valuation_repo.insert_cost_evidence(c, sys_actor, quote))
    assert stored.document_sha256 == "ab" * 32 and stored.base == Money.of("700.00", "EUR")
    loaded = await run(db, sys_actor, lambda c: valuation_repo.get_cost_evidence(c, sys_actor, [stored.id]))
    assert loaded == [stored]
    line = valuation_repo.cost_line_from_evidence(stored)
    assert line.evidence_ids == (str(stored.id),) and line.provider == "SYNTHETIC transport provider"
    # A valuation that cites the quote becomes stale when a superseding quote arrives; an
    # unrelated valuation does not.
    bundle = await complete_valuation(db, world, transport=stored)
    assert str(stored.id) in bundle.valuation.dependencies.cost_evidence_ids
    citing = await run(
        db,
        sys_actor,
        lambda c: valuation_repo.persist_valuation(
            c, sys_actor, bundle.valuation, bundle.refs, bundle.inputs
        ),
    )
    unrelated = await store_simple_valuation(db, world)
    newer = quote.model_copy(update={"base": Money.of("720.00", "EUR"), "supersedes_id": stored.id})
    await run(db, sys_actor, lambda c: valuation_repo.insert_cost_evidence(c, sys_actor, newer))
    stale = await run(db, sys_actor, lambda c: valuation_repo.load_valuation(c, sys_actor, citing.id))
    assert stale.valuation.state == ValuationState.STALE
    assert stale.valuation.stale_reason is not None and stale.valuation.stale_reason.startswith("cost_quote")
    untouched = await run(db, sys_actor, lambda c: valuation_repo.load_valuation(c, sys_actor, unrelated.id))
    assert untouched.valuation.state == ValuationState.NOT_STARTED
    # A valuation may only list evidence its scenarios cite.
    foreign_ref = bundle.refs.model_copy(update={"cost_evidence_ids": (uuid.uuid4(),)})
    with pytest.raises(ValidationFailed):
        await run(
            db,
            sys_actor,
            lambda c: valuation_repo.persist_valuation(
                c, sys_actor, bundle.valuation, foreign_ref, bundle.inputs
            ),
        )
    with pytest.raises(PydanticValidationError):
        CostEvidenceInput(
            kind="quote",
            category=CostCategory.TRANSPORT,
            base=Money.of("1", "EUR"),
            obtained_at=T0,
            scope=CostScope(),
        )
