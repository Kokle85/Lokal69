"""MK market evidence and reproducible comparable sets (spec 11, 15).

Everything under test runs as ``suv_backend`` under RLS; data is SYNTHETIC.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.repos_valuation_reviews.builders import (
    T0,
    RealWorld,
    business_config,
    observation,
    owner,
    reviewer,
    run,
    system,
    target,
    viewer,
)

from suv_deals.domain.comparables import ExclusionReason, MarketObservation, select_comparables
from suv_deals.domain.enums import EvidenceKind, Fuel
from suv_deals.domain.listings import PartialDate, VehicleSpec
from suv_deals.domain.money import Money
from suv_deals.errors import AppError, ErrorCode, Forbidden, NotFound, ValidationFailed
from suv_deals.persistence import market_repo
from suv_deals.persistence.database import Conn, Database

pytestmark = pytest.mark.db


def _vehicle(**overrides: object) -> VehicleSpec:
    values: dict[str, object] = {
        "make": "Example",
        "model": "Trail",
        "fuel": Fuel.DIESEL,
        "gearbox": "automatic",
        "drive": "awd",
        "first_registration": PartialDate(value="2012", precision="year"),  # type: ignore[arg-type]
        "mileage_km": Decimal("180000"),
    }
    values.update(overrides)
    return VehicleSpec(**values)  # type: ignore[arg-type]


async def test_evidence_kinds_stay_distinct_and_round_trip(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    ws = world.workspace_id
    sys_actor, own = system(ws), owner(ws)
    asking = observation(world.source_key, "9000.00")
    reported = observation(world.source_key, evidence_kind=EvidenceKind.SELLER_REPORTED_SALE, amount=None)
    verified = observation(world.source_key, "8600.00", evidence_kind=EvidenceKind.VERIFIED_SALE)
    estimate = observation(world.source_key, "8800.00", evidence_kind=EvidenceKind.OWNER_ESTIMATE, url=None)

    async def record(conn: Conn) -> None:
        await market_repo.insert_market_observation(
            conn,
            sys_actor,
            asking,
            confidence="medium",
            source_id=world.source_id,
        )
        await market_repo.insert_market_observation(
            conn,
            sys_actor,
            reported,
            confidence="low",
            source_id=world.source_id,
        )
        await market_repo.insert_market_observation(
            conn,
            sys_actor,
            verified,
            confidence="high",
            source_id=world.source_id,
            evidence={"document_ref": "SYNTHETIC-contract-1", "sha256": "cd" * 32},
        )

    await run(db, sys_actor, record)
    await run(db, own, lambda c: market_repo.insert_market_observation(c, own, estimate, confidence="medium"))
    loaded = await run(
        db,
        viewer(ws),
        lambda c: market_repo.get_market_observations(
            c, viewer(ws), [asking.id, reported.id, verified.id, estimate.id]
        ),
    )
    assert {k: v.observation.evidence_kind for k, v in loaded.items()} == {
        asking.id: EvidenceKind.ASKING_PRICE,
        reported.id: EvidenceKind.SELLER_REPORTED_SALE,
        verified.id: EvidenceKind.VERIFIED_SALE,
        estimate.id: EvidenceKind.OWNER_ESTIMATE,
    }
    assert loaded[asking.id].observation == asking  # lossless document
    assert loaded[reported.id].observation.amount is None
    assert loaded[estimate.id].recorded_by == own.principal_id  # from authentication only
    assert loaded[asking.id].recorded_by is None
    stored_kinds = dict(
        seed.conn.execute(
            "select id, evidence_kind from app.market_observations where workspace_id = %s", (ws,)
        ).fetchall()
    )
    assert stored_kinds[asking.id] == "asking_price" and stored_kinds[verified.id] == "verified_sale"


async def test_market_evidence_rules(db: Database, world: RealWorld) -> None:
    ws = world.workspace_id
    sys_actor = system(ws)
    estimate = observation(world.source_key, evidence_kind=EvidenceKind.OWNER_ESTIMATE)
    with pytest.raises(Forbidden):  # an owner estimate is never recorded by a system worker
        await run(
            db,
            sys_actor,
            lambda c: market_repo.insert_market_observation(c, sys_actor, estimate, confidence="low"),
        )
    rev = reviewer(ws)
    with pytest.raises(Forbidden):  # reviewers do not write market evidence
        await run(
            db,
            rev,
            lambda c: market_repo.insert_market_observation(
                c, rev, observation(world.source_key), confidence="low", source_id=world.source_id
            ),
        )
    verified = observation(world.source_key, evidence_kind=EvidenceKind.VERIFIED_SALE)
    with pytest.raises(ValidationFailed):  # a verified sale needs its evidence
        await run(
            db,
            sys_actor,
            lambda c: market_repo.insert_market_observation(
                c, sys_actor, verified, confidence="high", source_id=world.source_id
            ),
        )
    no_price = observation(world.source_key, amount=None)
    with pytest.raises(ValidationFailed):  # only a seller-reported sale may lack an amount
        await run(
            db,
            sys_actor,
            lambda c: market_repo.insert_market_observation(
                c, sys_actor, no_price, confidence="low", source_id=world.source_id
            ),
        )
    wrong_source = observation("another_synthetic_source")
    with pytest.raises(ValidationFailed):
        await run(
            db,
            sys_actor,
            lambda c: market_repo.insert_market_observation(
                c, sys_actor, wrong_source, confidence="low", source_id=world.source_id
            ),
        )


async def test_observation_replay_is_idempotent_and_never_rewrites(db: Database, world: RealWorld) -> None:
    sys_actor = system(world.workspace_id)
    item = observation(world.source_key)

    def insert(obs: MarketObservation) -> Callable[[Conn], Awaitable[tuple[UUID, bool]]]:
        return lambda c: market_repo.insert_market_observation(
            c, sys_actor, obs, confidence="medium", source_id=world.source_id
        )

    first = await run(db, sys_actor, insert(item))
    again = await run(db, sys_actor, insert(item))
    assert first == (item.id, True) and again == (item.id, False)
    changed = item.model_copy(update={"amount": Money.of("1.00", "EUR")})
    with pytest.raises(AppError) as info:
        await run(db, sys_actor, insert(changed))
    assert info.value.code == ErrorCode.VERSION_CONFLICT


async def test_candidate_lookup_by_indexed_dimensions(db: Database, world: RealWorld) -> None:
    ws = world.workspace_id
    sys_actor = system(ws)
    inside = observation(world.source_key, "9000.00")
    unknown_year = observation(
        world.source_key, "9100.00", vehicle=_vehicle(first_registration=PartialDate())
    )
    petrol = observation(world.source_key, "9200.00", vehicle=_vehicle(fuel=Fuel.PETROL))
    other_model = observation(world.source_key, "9300.00", vehicle=_vehicle(model="Other"))
    out_of_window = observation(
        world.source_key,
        "9400.00",
        vehicle=_vehicle(first_registration=PartialDate(value="2005", precision="year")),  # type: ignore[arg-type]
    )
    too_old = observation(world.source_key, "9500.00", observed_at=T0 - timedelta(days=400))
    after_as_of = observation(world.source_key, "9600.00", observed_at=T0 + timedelta(days=1))
    items = [inside, unknown_year, petrol, other_model, out_of_window, too_old, after_as_of]

    async def record(conn: Conn) -> None:
        for item in items:
            await market_repo.insert_market_observation(
                conn,
                sys_actor,
                item,
                confidence="medium",
                source_id=world.source_id,
            )

    await run(db, sys_actor, record)
    found = await run(
        db,
        sys_actor,
        lambda c: market_repo.list_candidate_comparables(
            c, sys_actor, target(world.listing_id), as_of=T0, max_age_days=180, year_window=2
        ),
    )
    # Fuel/gearbox/drive mismatches are returned so the domain records them as exclusions.
    assert {o.id for o in found} == {inside.id, unknown_year.id, petrol.id}
    with pytest.raises(ValidationFailed):
        await run(
            db,
            sys_actor,
            lambda c: market_repo.list_candidate_comparables(
                c, sys_actor, target(world.listing_id), as_of=T0, max_age_days=0, year_window=2
            ),
        )


async def test_comparable_set_round_trip_with_selected_and_excluded(
    db: Database, seed: Seed, world: RealWorld
) -> None:
    ws = world.workspace_id
    sys_actor = system(ws)
    selected = [observation(world.source_key, a) for a in ("8800.00", "9000.00", "9200.00", "9400.00")]
    petrol = observation(world.source_key, "9900.00", vehicle=_vehicle(fuel=Fuel.PETROL))
    stale = observation(world.source_key, "7000.00", observed_at=T0 - timedelta(days=900))
    candidates = [*selected, petrol, stale]

    async def go(conn: Conn) -> market_repo.StoredComparableSet:
        for item in candidates:
            await market_repo.insert_market_observation(
                conn,
                sys_actor,
                item,
                confidence="medium",
                source_id=world.source_id,
            )
        result = select_comparables(target(world.listing_id), candidates, business_config(), as_of=T0)
        return await market_repo.persist_comparable_set(
            conn,
            sys_actor,
            result,
            listing_id=world.listing_id,
            target_revision_id=world.revision_id,
        )

    stored = await run(db, sys_actor, go)
    result = stored.result
    assert {s.observation_id for s in result.selected} == {o.id for o in selected}
    reasons = {e.observation_id: set(e.reasons) for e in result.excluded}
    assert ExclusionReason.FUEL_MISMATCH in reasons[petrol.id]
    assert ExclusionReason.STALE in reasons[stale.id]
    reader = viewer(ws)
    loaded = await run(db, reader, lambda c: market_repo.load_comparable_set(c, reader, stored.id))
    assert loaded.result == result
    assert loaded.content_sha256 == market_repo.comparable_content_sha256(result)
    assert loaded.reference() == stored.reference()
    assert loaded.sample_quality == result.sample_quality
    assert set(loaded.excluded_observations) == {petrol.id, stale.id}
    # Member rows: excluded rows carry reasons and no weight; differences is a jsonb object.
    rows = seed.conn.execute(
        "select market_observation_id, disposition, reasons, jsonb_typeof(differences), weight"
        " from app.comparable_set_members where comparable_set_id = %s",
        (stored.id,),
    ).fetchall()
    assert len(rows) == len(candidates)
    for obs_id, disposition, row_reasons, diff_type, weight in rows:
        assert diff_type == "object"
        if disposition == "excluded":
            assert row_reasons and weight is None and obs_id in {petrol.id, stale.id}
        else:
            assert row_reasons == [] and weight is not None and obs_id in {o.id for o in selected}
    quality = seed.scalar("select sample_quality from app.comparable_sets where id = %s", (stored.id,))
    assert quality == result.sample_quality
    # Paged view by stable ordinal (selected first, then excluded).
    page, next_ordinal = await run(
        db,
        reader,
        lambda c: market_repo.get_comparable_set_view(c, reader, stored.id, include_excluded=True, limit=4),
    )
    assert next_ordinal == 4 and [m.role for m in page.members] == ["selected"] * 4
    rest, last = await run(
        db,
        reader,
        lambda c: market_repo.get_comparable_set_view(
            c, reader, stored.id, include_excluded=True, ordinal_start=4, limit=4
        ),
    )
    assert last is None and [m.role for m in rest.members] == ["excluded", "excluded"]
    containing = await run(
        db, reader, lambda c: market_repo.comparable_sets_with_observation(c, reader, [petrol.id])
    )
    assert containing == [stored.id]


async def test_comparable_sets_and_observations_are_workspace_isolated(
    db: Database, world: RealWorld, other_world: RealWorld
) -> None:
    sys_actor = system(world.workspace_id)
    items = [observation(world.source_key, a) for a in ("8800.00", "9000.00", "9200.00")]

    async def go(conn: Conn) -> market_repo.StoredComparableSet:
        for item in items:
            await market_repo.insert_market_observation(
                conn,
                sys_actor,
                item,
                confidence="medium",
                source_id=world.source_id,
            )
        result = select_comparables(target(world.listing_id), items, business_config(), as_of=T0)
        return await market_repo.persist_comparable_set(
            conn,
            sys_actor,
            result,
            listing_id=world.listing_id,
            target_revision_id=world.revision_id,
        )

    stored = await run(db, sys_actor, go)
    foreign = viewer(other_world.workspace_id)
    with pytest.raises(NotFound):
        await run(db, foreign, lambda c: market_repo.load_comparable_set(c, foreign, stored.id))
    seen = await run(
        db, foreign, lambda c: market_repo.get_market_observations(c, foreign, [i.id for i in items])
    )
    assert seen == {}
    # A set for a listing of another workspace is refused (composite FK -> NotFound).
    other_sys = system(other_world.workspace_id)
    with pytest.raises(NotFound):
        await run(
            db,
            other_sys,
            lambda c: market_repo.persist_comparable_set(
                c,
                other_sys,
                stored.result.model_copy(update={"target": target(other_world.listing_id)}),
                listing_id=other_world.listing_id,
                target_revision_id=world.revision_id,
            ),
        )
    # Re-using an observation id from another workspace never reveals or overwrites it.
    copied = items[0].model_copy(update={"source_key": other_world.source_key})
    with pytest.raises(ValidationFailed):
        await run(
            db,
            other_sys,
            lambda c: market_repo.insert_market_observation(
                c, other_sys, copied, confidence="low", source_id=other_world.source_id
            ),
        )
