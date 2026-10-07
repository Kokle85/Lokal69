"""Field evidence verification and possible-same-vehicle clusters (spec 7, 10, 37.9)."""

from __future__ import annotations

from uuid import UUID

import pytest
from tests.integration.db.helpers import T0 as SEED_T0
from tests.integration.db.helpers import Seed, unique
from tests.integration.persistence_core.support import member
from tests.integration.repos_sources_listings.support import (
    T0,
    Env,
    claim_detail,
    discover,
    later,
    run_detail,
    vehicle,
)

from suv_deals.domain.enums import ClaimStatus, Confidence, Role
from suv_deals.domain.identity import SameVehicleSuggestion, possible_same_vehicle
from suv_deals.errors import Forbidden, NotFound, ValidationFailed, VersionConflict
from suv_deals.persistence import clusters_repo, evidence_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db

SYNTHETIC_VIN = "WVGZZZ5NZCW000001"  # format-valid, synthetic


async def _evidence(db: Database, env: Env) -> tuple[UUID, UUID]:
    slid = unique("SYN")
    listing_id, _ = await discover(db, env, slid)
    await run_detail(db, env, await claim_detail(db, env), listing_id, vehicle(slid))
    async with unit_of_work(db, env.owner) as conn:
        rows = await evidence_repo.list_field_evidence(
            conn, env.owner, listing_id, field_path="price.amount_minor"
        )
    assert len(rows) == 1 and rows[0].claim_status is None and rows[0].confidence == Confidence.HIGH
    return listing_id, rows[0].id


async def test_owner_verification_is_a_superseding_row(db: Database, env: Env, seed: Seed) -> None:
    listing_id, evidence_id = await _evidence(db, env)
    async with unit_of_work(db, env.owner) as conn:
        verified = await evidence_repo.verify_field_evidence(
            conn,
            env.owner,
            evidence_id,
            document_ref="owner-call-2026-10-06",
            note="seller confirmed by phone",
        )
    assert verified.supersedes_id == evidence_id and verified.claim_status == ClaimStatus.VERIFIED
    assert verified.verified_by == env.owner_user_id and verified.verified_at is not None
    assert verified.raw_excerpt is not None and verified.field_path == "price.amount_minor"
    async with unit_of_work(db, env.owner) as conn:
        current = await evidence_repo.list_field_evidence(
            conn, env.owner, listing_id, field_path="price.amount_minor"
        )
        history = await evidence_repo.list_field_evidence(
            conn, env.owner, listing_id, field_path="price.amount_minor", include_superseded=True
        )
    assert [e.id for e in current] == [verified.id]
    assert {e.id for e in history} == {evidence_id, verified.id}
    # The original row is untouched (append-only) and cannot be superseded twice.
    assert seed.scalar("select claim_status from app.field_evidence where id = %s", (evidence_id,)) is None
    async with unit_of_work(db, env.owner) as conn:
        with pytest.raises(VersionConflict):
            await evidence_repo.verify_field_evidence(conn, env.owner, evidence_id)
        with pytest.raises(ValidationFailed):
            await evidence_repo.verify_field_evidence(
                conn, env.owner, verified.id, claim_status=ClaimStatus.UNKNOWN
            )
    reviewer_user = seed.user()
    seed.membership(env.workspace_id, reviewer_user, "reviewer")
    reviewer = member(env.workspace_id, Role.REVIEWER, principal_id=reviewer_user)
    async with unit_of_work(db, reviewer) as conn:
        conflicting = await evidence_repo.verify_field_evidence(
            conn, reviewer, verified.id, claim_status=ClaimStatus.CONFLICTING
        )
    assert conflicting.supersedes_id == verified.id and conflicting.verified_by == reviewer_user


async def test_only_signed_in_owner_or_reviewer_verifies(db: Database, env: Env, env_b: Env) -> None:
    _, evidence_id = await _evidence(db, env)
    for actor in (
        member(env.workspace_id, Role.VIEWER),
        member(env.workspace_id, Role.OWNER, kind="mcp_client"),
        env.system,
    ):
        async with unit_of_work(db, actor) as conn:
            with pytest.raises(Forbidden):
                await evidence_repo.verify_field_evidence(conn, actor, evidence_id)
    async with unit_of_work(db, env_b.owner) as conn:
        with pytest.raises(NotFound):
            await evidence_repo.verify_field_evidence(conn, env_b.owner, evidence_id)
        with pytest.raises(NotFound):
            await evidence_repo.get_field_evidence(conn, env_b.owner, evidence_id)


# --------------------------------------------------------------------------------------------
# Clusters
# --------------------------------------------------------------------------------------------


async def _pair(db: Database, env: Env, seed: Seed) -> tuple[UUID, UUID]:
    """One discovered listing and one listing of a second synthetic source."""
    first, _ = await discover(db, env, unique("SYN"))
    other_source = seed.source(env.workspace_id)
    second = seed.listing(env.workspace_id, other_source, first_seen_at=SEED_T0, last_seen_at=later(600))
    return first, second


def _suggestion() -> SameVehicleSuggestion:
    return possible_same_vehicle(vehicle("A-1", vin=SYNTHETIC_VIN), vehicle("B-1", vin=SYNTHETIC_VIN))


async def test_suggestion_never_merges_and_respects_false_positive_review(
    db: Database, env: Env, seed: Seed
) -> None:
    a, b = await _pair(db, env, seed)
    suggestion = possible_same_vehicle(vehicle("A-1", vin=SYNTHETIC_VIN), vehicle("B-1", vin=SYNTHETIC_VIN))
    assert suggestion.suggest and suggestion.confidence == Confidence.HIGH
    listings_before = seed.scalar(
        "select count(*) from app.listings where workspace_id = %s", (env.workspace_id,)
    )
    async with unit_of_work(db, env.system) as conn:
        created = await clusters_repo.suggest_possible_same_vehicle(conn, env.system, a, b, suggestion)
        again = await clusters_repo.suggest_possible_same_vehicle(conn, env.system, b, a, suggestion)
    assert created.outcome == "created_cluster" and len(created.member_ids) == 2
    assert again.outcome == "already_clustered" and again.cluster_id == created.cluster_id
    assert seed.scalar("select count(*) from app.listings where workspace_id = %s", (env.workspace_id,)) == (
        listings_before
    )
    assert created.cluster_id is not None
    basis = seed.scalar("select match_basis from app.vehicle_clusters where id = %s", (created.cluster_id,))
    assert basis["kind"] == "possible_same_vehicle" and "VIN" not in str(basis).replace("VIN_MATCH", "")
    reviewer = member(env.workspace_id, Role.REVIEWER, principal_id=env.owner_user_id)
    async with unit_of_work(db, reviewer) as conn:
        unlinked = await clusters_repo.unlink_member(
            conn, reviewer, created.member_ids[1], reason="different car, photos differ"
        )
        repeat = await clusters_repo.unlink_member(conn, reviewer, created.member_ids[1], reason="again")
    assert (
        unlinked.unlinked_at is not None and unlinked.unlinked_by == env.owner_user_id and repeat == unlinked
    )
    assert (
        seed.scalar(
            "select count(*) from app.vehicle_cluster_members where cluster_id = %s", (created.cluster_id,)
        )
        == 2
    )  # the row is kept with who/when/why
    async with unit_of_work(db, env.system) as conn:
        refused = await clusters_repo.suggest_possible_same_vehicle(conn, env.system, a, b, suggestion)
    assert refused.outcome == "previously_unlinked" and refused.cluster_id == created.cluster_id


async def test_confirm_review_and_lifecycle(db: Database, env: Env, seed: Seed) -> None:
    a, b = await _pair(db, env, seed)
    async with unit_of_work(db, env.system) as conn:
        created = await clusters_repo.suggest_possible_same_vehicle(conn, env.system, a, b, _suggestion())
        with pytest.raises(Forbidden):
            await clusters_repo.confirm_member(
                conn, env.system, created.member_ids[0], reason="system may not"
            )
    assert created.cluster_id is not None
    async with unit_of_work(db, env.owner) as conn:
        confirmed = await clusters_repo.confirm_member(
            conn, env.owner, created.member_ids[0], reason="same VIN seen"
        )
        view = await clusters_repo.get_cluster(conn, env.owner, created.cluster_id)
        with pytest.raises(VersionConflict):
            await clusters_repo.review_cluster(
                conn, env.owner, created.cluster_id, "confirmed", expected_version=1, reason="stale version"
            )
        reviewed = await clusters_repo.review_cluster(
            conn,
            env.owner,
            created.cluster_id,
            "confirmed",
            expected_version=view.cluster.row_version,
            reason="owner reviewed both ads",
        )
        lifecycle = await clusters_repo.cluster_lifecycle(conn, env.owner, created.cluster_id)
    assert confirmed.manually_confirmed and confirmed.confirmed_by == env.owner_user_id
    assert reviewed.review_status == "confirmed" and reviewed.reviewed_by == env.owner_user_id
    assert {s.listing_id for s in lifecycle.sources} == {a, b}
    # The discovered listing was first seen at T0 (08:00), the seeded one at 10:00: the earliest
    # sighting by THIS system, not a publication date.
    assert lifecycle.earliest_observed_at == T0 < SEED_T0
    assert lifecycle.latest_source_presence_at == later(600)


async def test_rejected_pair_is_not_resuggested_and_weak_signals_are_ignored(
    db: Database, env: Env, seed: Seed
) -> None:
    a, b = await _pair(db, env, seed)
    weak = possible_same_vehicle(vehicle("A-2", make="Toyota", model="RAV4"), vehicle("B-2"))
    async with unit_of_work(db, env.system) as conn:
        assert (await clusters_repo.suggest_possible_same_vehicle(conn, env.system, a, b, weak)).outcome == (
            "not_suggested"
        )
        created = await clusters_repo.suggest_possible_same_vehicle(conn, env.system, a, b, _suggestion())
        with pytest.raises(ValidationFailed):
            await clusters_repo.suggest_possible_same_vehicle(conn, env.system, a, a, _suggestion())
    assert created.cluster_id is not None
    async with unit_of_work(db, env.owner) as conn:
        await clusters_repo.review_cluster(
            conn, env.owner, created.cluster_id, "rejected", expected_version=1, reason="false positive"
        )
    async with unit_of_work(db, env.system) as conn:
        again = await clusters_repo.suggest_possible_same_vehicle(conn, env.system, a, b, _suggestion())
    assert again.outcome == "previously_unlinked" and again.cluster_id == created.cluster_id
    assert (
        seed.scalar("select count(*) from app.vehicle_clusters where workspace_id = %s", (env.workspace_id,))
        == 1
    )
