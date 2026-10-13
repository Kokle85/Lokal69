"""SYNTHETIC helpers for the import-cost calculator tests (service and valuation pipeline).

No real listing, seller or rate: listings are the read-query dataset's synthetic "Example Trail"
documents with a CO2 claim added; the customs rate 61.5337 is the golden-case input of the fact
sheet (a test value, recorded only in throwaway test workspaces). The MK rule set is the shipped
DRAFT file; where a test activates it, that happens in a throwaway SYNTHETIC workspace through the
real owner transitions, never in configuration.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from tests.integration.db.helpers import Seed, unique
from tests.integration.persistence_core.support import member
from tests.integration.read_queries.dataset import _screening, add_revision, normalized

from suv_deals.domain.actor import ActorContext
from suv_deals.domain.enums import Co2Cycle, EligibilityState, Fuel, FxPurpose, Role, TaxRuleStatus
from suv_deals.domain.listings import Co2Info, Documentation, NormalizedListing
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.tax_engine import ReviewRecord, RuleSet, compute_rule_set_sha256, load_rule_set_file
from suv_deals.persistence import valuation_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

REPO = Path(__file__).resolve().parents[3]
CONFIG = REPO / "config"
DRAFT_FILE = CONFIG / "tax_rules" / "drafts" / "mk_passenger_car_import_2026.10-draft.1.json"
AS_OF = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
DECL = date(2026, 10, 13)
CUSTOMS_RATE = "61.5337"


def owner(ws: UUID) -> ActorContext:
    return member(ws, Role.OWNER)


def system(ws: UUID) -> ActorContext:
    return ActorContext.system(ws, request_id=f"test-{uuid.uuid4().hex[:12]}")


def mk_draft() -> RuleSet:
    return load_rule_set_file(DRAFT_FILE)


async def store_rule_set(db: Database, actor: ActorContext, rule_set: RuleSet) -> None:
    async with unit_of_work(db, actor) as conn:
        await valuation_repo.store_rule_set(conn, actor, rule_set)


async def activate(db: Database, actor: ActorContext, rule_set: RuleSet) -> valuation_repo.StoredRuleSet:
    """Store ``rule_set`` as a draft and drive it through review, approval and activation (owner
    acts, SYNTHETIC review record) in this throwaway test workspace only."""
    record = ReviewRecord(
        reviewer="SYNTHETIC reviewer",
        reviewed_at=AS_OF - timedelta(days=1),
        content_sha256=compute_rule_set_sha256(rule_set),
        scope="SYNTHETIC test activation",
    )
    async with unit_of_work(db, actor) as conn:
        await valuation_repo.store_rule_set(conn, actor, rule_set)
        rs, version = rule_set.rule_set_id, rule_set.version
        await valuation_repo.transition_tax_rule_set(conn, actor, rs, version, TaxRuleStatus.UNDER_REVIEW)
        await valuation_repo.transition_tax_rule_set(
            conn,
            actor,
            rs,
            version,
            TaxRuleStatus.APPROVED,
            approved_by="SYNTHETIC owner",
            review_record=record,
        )
        return await valuation_repo.transition_tax_rule_set(conn, actor, rs, version, TaxRuleStatus.ACTIVE)


async def revoke(db: Database, actor: ActorContext, rule_set: RuleSet) -> None:
    async with unit_of_work(db, actor) as conn:
        await valuation_repo.store_rule_set(conn, actor, rule_set)
        await valuation_repo.transition_tax_rule_set(
            conn, actor, rule_set.rule_set_id, rule_set.version, TaxRuleStatus.REVOKED
        )


async def record_customs_rate(
    db: Database,
    ws: UUID,
    *,
    on: date = date(2026, 10, 1),
    value: str = CUSTOMS_RATE,
    is_fixture: bool = False,
) -> None:
    """A SYNTHETIC recorded EUR/MKD customs observation (what `suv-deals fx record` stores)."""
    actor = system(ws)
    rate = FxRate(
        base="EUR",
        quote="MKD",
        rate=Decimal(value),
        rate_date=on,
        retrieved_at=datetime.combine(on, datetime.min.time(), tzinfo=UTC) + timedelta(hours=8),
        provider="SYNTHETIC customs monthly list",
        purpose=FxPurpose.CUSTOMS,
    )
    async with unit_of_work(db, actor) as conn:
        await valuation_repo.upsert_fx_rate(conn, actor, rate, is_fixture=is_fixture, source_ref="SYNTHETIC")


def workspace(seed: Seed, name: str, *, mode: str = "public_html") -> tuple[UUID, UUID, str]:
    ws = seed.workspace(f"{name} {uuid.uuid4().hex[:6]}")
    key = unique("synthetic_src").lower()
    adapter = "fixture_adapter" if mode == "fixture" else "synthetic_adapter"
    source = seed.source(ws, source_key=key, mode=mode, adapter=adapter)
    return ws, source, key


def listing_doc(source_key: str, slid: str, **changes: Any) -> NormalizedListing:
    """The read-query dataset's synthetic diesel "Example Trail" with a listed CO2 claim
    (NEDC 189 g/km) and a Euro 5 statement; ``changes`` replace top-level fields."""
    doc = normalized(
        source_key,
        slid,
        observed_at=AS_OF - timedelta(days=1),
        amount_minor=300000,
        currency="EUR",
        country="DE",
        title="SYNTHETIC Example Trail 2.0 TDI",
    )
    update: dict[str, Any] = {
        "co2": Co2Info(g_per_km=Decimal("189"), cycle=Co2Cycle.NEDC),
        "documentation": Documentation(emissions_class="Euro 5"),
    }
    update.update(changes)
    return NormalizedListing.model_validate({**doc.model_dump(), **update})


def add_listing(
    seed: Seed,
    ws: UUID,
    source_id: UUID,
    source_key: str,
    *,
    payable: Money | None = None,
    screened: bool = True,
    is_fixture: bool = False,
    fuel: Fuel = Fuel.DIESEL,
    **changes: Any,
) -> UUID:
    """One listing with one promoted revision and (by default) an eligible screening whose
    payable amount is EUR 3,000 (fact sheet D1)."""
    slid = unique("IC")
    listing = seed.listing(ws, source_id, source_listing_id=slid, is_fixture=is_fixture)
    doc = listing_doc(source_key, slid, **changes)
    if fuel != doc.vehicle.fuel:
        doc = NormalizedListing.model_validate(
            {**doc.model_dump(), "vehicle": {**doc.vehicle.model_dump(), "fuel": fuel}}
        )
    add_revision(seed, ws, listing, 1, doc)
    if screened:
        amount = payable if payable is not None else Money.of("3000.00", "EUR")
        seed.conn.execute(
            "update app.listings set eligibility_state = 'eligible_primary', eligibility_profile = 'primary',"
            " screening = %s, screening_version = 'screening@synthetic', screened_at = now(),"
            " row_version = row_version + 1 where workspace_id = %s and id = %s",
            (
                Jsonb(_screening(EligibilityState.ELIGIBLE_PRIMARY, eur=str(amount.amount), payable=amount)),
                ws,
                listing,
            ),
        )
    return listing


def counts(seed: Seed, ws: UUID) -> dict[str, int]:
    """Rows a read must never write."""
    tables = (
        "app.valuations",
        "app.fx_rates",
        "app.tax_rule_sets",
        "app.cost_profiles",
        "ops.audit_events",
        "ops.outbox",
        "ops.jobs",
    )
    return {t: int(seed.scalar(f"select count(*) from {t} where workspace_id = %s", (ws,))) for t in tables}
