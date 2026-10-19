"""Recorded import values (``persistence.import_values_repo``) and the calculator's listing mode.

Owner decision 2026-10-13: each vehicle keeps its REAL purchase price (profit, landed cost) apart
from its ESTIMATED customs value (import charges). The owner records both per listing; a new value
supersedes the current one; recording invalidates the listing's valuations and queues their
recomputation; the calculator's listing mode uses the current values unless the request overrides
them. The amounts are the owner's own example (bought for EUR 2,800, SchwackeNet EUR 4,200) on the
fact sheet's D1 car (diesel, NEDC 189, no proof of origin), customs rate 61.5337. Every workspace,
listing and value is SYNTHETIC.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row
from pydantic import ValidationError
from tests.integration.db.helpers import Seed
from tests.integration.import_costs.support import (
    AS_OF,
    CONFIG,
    add_listing,
    owner,
    record_customs_rate,
    system,
    workspace,
)
from tests.integration.persistence_core.support import member

from suv_deals.domain.enums import Role, Scope
from suv_deals.domain.money import Money
from suv_deals.errors import Forbidden, NotFound, ValidationFailed
from suv_deals.persistence import import_values_repo, valuation_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.import_values_repo import (
    ImportValues,
    ImportValuesRecorded,
    RecordedCustomsValue,
    RecordedPurchase,
)
from suv_deals.persistence.queries import ImportCostRequest, get_import_cost_estimate
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.views.import_costs import ImportCostEstimateView

pytestmark = pytest.mark.db

PAID = RecordedPurchase(
    amount=Decimal("2800.00"), kind="actual", source="seller invoice", document_ref="INV-SYNTHETIC-1"
)
SCHWACKE = RecordedCustomsValue(amount_eur=Decimal("4200"), source="SchwackeNet catalogue")


async def record(
    db: Database,
    ws: UUID,
    listing: UUID,
    *,
    purchase: RecordedPurchase | None = None,
    customs_value: RecordedCustomsValue | None = None,
    actor: Any = None,
) -> ImportValuesRecorded:
    who = actor or owner(ws)
    async with unit_of_work(db, who) as conn:
        return await import_values_repo.record_import_values(
            conn, who, listing, purchase=purchase, customs_value=customs_value
        )


async def current(db: Database, ws: UUID, listing: UUID, *, actor: Any = None) -> ImportValues:
    who = actor or member(ws, Role.VIEWER)
    async with unit_of_work(db, who) as conn:
        return await import_values_repo.current_import_values(conn, who, listing)


def _job_rows(seed: Seed, ws: UUID, listing: UUID) -> list[dict[str, Any]]:
    return (
        _dicts(seed)
        .execute(
            "select id, state, dedup_key, payload from ops.jobs where workspace_id = %s and listing_id = %s"
            " and job_type = 'valuation' order by created_at, id",
            (ws, listing),
        )
        .fetchall()
    )


def _dicts(seed: Seed) -> psycopg.Cursor[dict[str, Any]]:
    return seed.conn.cursor(row_factory=dict_row)


def _revision(seed: Seed, ws: UUID, listing: UUID) -> UUID:
    rev = seed.scalar(
        "select current_revision_id from app.listings where workspace_id = %s and id = %s", (ws, listing)
    )
    assert isinstance(rev, UUID)
    return rev


# --------------------------------------------------------------------------- repository


async def test_owner_records_both_values_and_reads_them_back(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import values record")
    listing = add_listing(seed, ws, source, key)
    result = await record(db, ws, listing, purchase=PAID, customs_value=SCHWACKE)
    assert len(result.recorded_ids) == 2 and result.superseded_ids == ()
    values = await current(db, ws, listing)
    assert values == result.values
    purchase, customs = values.purchase, values.customs_value
    assert purchase is not None and customs is not None
    assert (purchase.category, purchase.kind, purchase.amount) == (
        "purchase",
        "actual",
        Money.of("2800", "EUR"),
    )
    assert (purchase.source, purchase.document_ref) == ("seller invoice", "INV-SYNTHETIC-1")
    assert purchase.price_basis == "recorded_actual" and purchase.current and not purchase.is_fixture
    assert (customs.category, customs.kind, customs.amount) == (
        "customs_value",
        "estimate",
        Money.of("4200", "EUR"),
    )
    assert customs.source == "SchwackeNet catalogue" and customs.document_ref is None
    assert {r.id for r in values.history} == set(result.recorded_ids)
    stored = (
        _dicts(seed)
        .execute(
            "select category, kind, base_minor, currency, provider, listing_id, recorded_by, scope"
            " from app.cost_evidence where workspace_id = %s order by category",
            (ws,),
        )
        .fetchall()
    )
    assert [(r["category"], r["kind"], r["base_minor"], r["currency"]) for r in stored] == [
        ("customs_value", "estimate", 420000, "EUR"),
        ("purchase", "actual", 280000, "EUR"),
    ]
    assert {r["listing_id"] for r in stored} == {listing}
    assert all(r["scope"]["listing_id"] == str(listing) for r in stored)
    audit = (
        _dicts(seed)
        .execute(
            "select action, target_type, target_id, metadata from ops.audit_events where workspace_id = %s"
            " and action = 'import_values.record'",
            (ws,),
        )
        .fetchall()
    )
    assert len(audit) == 1 and audit[0]["target_id"] == listing and audit[0]["target_type"] == "listing"
    assert {r["category"] for r in audit[0]["metadata"]["recorded"]} == {"purchase", "customs_value"}


async def test_a_new_value_supersedes_the_current_one_in_a_linear_chain(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import values chain")
    listing = add_listing(seed, ws, source, key)
    first = await record(db, ws, listing, purchase=PAID)
    quote = RecordedPurchase(amount=Decimal("2750"), kind="quote", source="agreed with the seller")
    second = await record(db, ws, listing, purchase=quote)
    assert second.superseded_ids == first.recorded_ids
    third = await record(db, ws, listing, purchase=PAID, customs_value=SCHWACKE)
    assert third.superseded_ids == second.recorded_ids  # the customs value had no predecessor
    values = await current(db, ws, listing)
    assert values.purchase is not None and values.purchase.id == third.recorded_ids[0]
    assert values.purchase.supersedes_id == second.recorded_ids[0]
    purchases = [r for r in values.history if r.category == "purchase"]
    assert [r.id for r in purchases] == [third.recorded_ids[0], second.recorded_ids[0], first.recorded_ids[0]]
    assert [r.current for r in purchases] == [True, False, False]
    assert [r.kind for r in purchases] == ["actual", "quote", "actual"]
    # The rows are append-only: the superseded value is still there.
    assert seed.scalar("select count(*) from app.cost_evidence where workspace_id = %s", (ws,)) == 4


async def test_only_the_signed_in_owner_records_values(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import values roles")
    listing = add_listing(seed, ws, source, key)
    refused = [
        member(ws, Role.REVIEWER),
        member(ws, Role.VIEWER),
        system(ws),
        member(ws, Role.OWNER, kind="mcp_client"),
        member(ws, Role.OWNER, scopes=frozenset({Scope.DEALS_READ})),
    ]
    for actor in refused:
        with pytest.raises(Forbidden):
            await record(db, ws, listing, purchase=PAID, actor=actor)
    assert seed.scalar("select count(*) from app.cost_evidence where workspace_id = %s", (ws,)) == 0
    # A system worker and a viewer may read them.
    assert (await current(db, ws, listing, actor=system(ws))).purchase is None
    assert (await current(db, ws, listing)).customs_value is None


async def test_a_foreign_or_missing_listing_is_not_found(db: Database, seed: Seed) -> None:
    ws_a, source_a, key_a = workspace(seed, "Import values A")
    ws_b, source_b, key_b = workspace(seed, "Import values B")
    add_listing(seed, ws_a, source_a, key_a)
    foreign = add_listing(seed, ws_b, source_b, key_b)
    await record(db, ws_b, foreign, purchase=PAID)
    with pytest.raises(NotFound):
        await record(db, ws_a, foreign, purchase=PAID)
    with pytest.raises(NotFound):
        await record(db, ws_a, UUID(int=7), customs_value=SCHWACKE)
    with pytest.raises(NotFound):
        await current(db, ws_a, foreign)
    assert seed.scalar("select count(*) from app.cost_evidence where workspace_id = %s", (ws_a,)) == 0


async def test_recording_invalidates_the_listing_and_queues_one_recomputation(
    db: Database, seed: Seed
) -> None:
    ws, source, key = workspace(seed, "Import values invalidation")
    listing = add_listing(seed, ws, source, key)
    other = add_listing(seed, ws, source, key)
    rev = _revision(seed, ws, listing)
    open_valuation = seed.valuation(ws, listing, rev)
    untouched = seed.valuation(ws, other, _revision(seed, ws, other))
    result = await record(db, ws, listing, purchase=PAID)
    assert result.stale_valuation_ids == (open_valuation,)
    row = (
        _dicts(seed)
        .execute("select state, stale_reason from app.valuations where id = %s", (open_valuation,))
        .fetchone()
    )
    assert row is not None and row["state"] == "stale"
    assert row["stale_reason"].startswith("cost_quote: recorded import values changed (purchase price)")
    assert seed.scalar("select state from app.valuations where id = %s", (untouched,)) == "incomplete"
    jobs = _job_rows(seed, ws, listing)
    assert [(j["id"], j["state"], j["dedup_key"]) for j in jobs] == [
        (result.recompute_job_id, "queued", f"valuation.recompute:{listing}")
    ]
    assert jobs[0]["payload"]["stale_valuation_ids"] == [str(open_valuation)]
    # A second value while that job waits: nothing open is left, the same job absorbs it.
    again = await record(db, ws, listing, customs_value=SCHWACKE)
    assert again.stale_valuation_ids == () and again.recompute_job_id == result.recompute_job_id
    assert len(_job_rows(seed, ws, listing)) == 1
    assert _job_rows(seed, ws, other) == []


async def test_a_listing_without_a_valuation_still_gets_its_recomputation(db: Database, seed: Seed) -> None:
    """A recomputation that is already running read the old values: one follow-up job runs after it."""
    ws, source, key = workspace(seed, "Import values requeue")
    listing = add_listing(seed, ws, source, key)
    first = await record(db, ws, listing, purchase=PAID)
    assert first.stale_valuation_ids == ()
    assert [j["id"] for j in _job_rows(seed, ws, listing)] == [first.recompute_job_id]
    seed.conn.execute(
        "update ops.jobs set state = 'running', lease_owner = 'SYNTHETIC-worker',"
        " lease_token = gen_random_uuid(), lease_expires_at = clock_timestamp() + interval '5 minutes',"
        " last_heartbeat_at = clock_timestamp(), attempts = 1 where id = %s",
        (first.recompute_job_id,),
    )
    second = await record(db, ws, listing, customs_value=SCHWACKE)
    assert second.recompute_job_id != first.recompute_job_id
    keys = [j["dedup_key"] for j in _job_rows(seed, ws, listing)]
    assert keys == [
        f"valuation.recompute:{listing}",
        f"valuation.recompute:{listing}:after:{first.recompute_job_id}",
    ]


async def test_fixture_lineage_and_cost_evidence_separation(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import values fixture", mode="fixture")
    listing = add_listing(seed, ws, source, key, is_fixture=True)
    result = await record(db, ws, listing, purchase=PAID, customs_value=SCHWACKE)
    values = result.values
    assert values.purchase is not None and values.purchase.is_fixture
    assert values.customs_value is not None and values.customs_value.is_fixture
    # Recorded import values are not cost evidence (the cost profile never sees them).
    async with unit_of_work(db, system(ws)) as conn:
        with pytest.raises(NotFound):
            await valuation_repo.get_cost_evidence(conn, system(ws), list(result.recorded_ids))


def test_recorded_values_are_validated_before_anything_is_written() -> None:
    for amount in ("0", "-1", "2800.001", "1000000000000.01", "NaN", "Infinity"):
        with pytest.raises(ValidationError):
            RecordedPurchase(amount=Decimal(amount), kind="actual", source="seller invoice")
        with pytest.raises(ValidationError):
            RecordedCustomsValue(amount_eur=Decimal(amount), source="SchwackeNet catalogue")
    with pytest.raises(ValidationError):
        RecordedPurchase.model_validate({"amount": 2800.0, "kind": "actual", "source": "seller invoice"})
    with pytest.raises(ValidationError):
        RecordedPurchase.model_validate(
            {"amount": "2800", "currency": "MKD", "kind": "actual", "source": "invoice"}
        )
    with pytest.raises(ValidationError):
        RecordedPurchase.model_validate({"amount": "2800", "kind": "paid", "source": "seller invoice"})
    for source in ("ab", "   ", "seller\ninvoice", "x" * 121, "seller" + chr(0x202E) + "invoice"):
        with pytest.raises(ValidationError):
            RecordedCustomsValue(amount_eur=Decimal("4200"), source=source)
    for ref in ("line\nbreak", "INV" + chr(0x202E) + "1", "   "):
        with pytest.raises(ValidationError):
            RecordedCustomsValue(amount_eur=Decimal("4200"), source="SchwackeNet", document_ref=ref)
    assert RecordedCustomsValue(amount_eur=Decimal("4200"), source="  SchwackeNet  ").source == "SchwackeNet"


async def test_recording_nothing_is_refused(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import values nothing")
    listing = add_listing(seed, ws, source, key)
    with pytest.raises(ValidationFailed) as exc:
        await record(db, ws, listing)
    assert exc.value.details["fields"] == ["purchase_price", "estimated_customs_value"]


# --------------------------------------------------------------------------- calculator listing mode


def request(listing: UUID, **vehicle: Any) -> ImportCostRequest:
    facts = {"co2_document": "coc", "declaration_date": "2026-10-13", **vehicle}
    return ImportCostRequest.model_validate({"listing_id": str(listing), "vehicle": facts})


async def estimate(db: Database, ws: UUID, req: ImportCostRequest) -> ImportCostEstimateView:
    who = member(ws, Role.VIEWER)
    async with unit_of_work(db, who) as conn:
        result = await get_import_cost_estimate(conn, who, req, config_dir=CONFIG, as_of=AS_OF)
    return result.data


def _totals(view: ImportCostEstimateView) -> dict[str, tuple[str | None, str | None]]:
    out: dict[str, tuple[str | None, str | None]] = {}
    for s in view.scenarios:
        total, landed = s.indicative_total, s.indicative_landed_cost_eur
        assert total is not None and landed is not None
        out[s.name] = (total.amount, landed.amount)
    return out


async def test_listing_mode_uses_the_recorded_values(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import values calculator")
    listing = add_listing(seed, ws, source, key)  # advertised EUR 3,000
    await record_customs_rate(db, ws)
    recorded = await record(db, ws, listing, purchase=PAID, customs_value=SCHWACKE)
    purchase_id, customs_id = recorded.recorded_ids
    view = await estimate(db, ws, request(listing))
    # The owner's example: duties on EUR 4,200 + 700, landed cost on the EUR 2,800 actually paid.
    assert _totals(view) == {"primary": ("185803", "6520"), "transaction_value": ("165213", "6185")}
    assert view.purchase_price.amount.amount == "2800" and view.purchase_price.basis == "recorded_actual"
    assert (
        view.purchase_price.source
        == "recorded actual purchase price EUR 2800 (seller invoice, "
        + (view.purchase_price.source.rsplit(", ", 1)[1])
    )
    assert view.customs_value_for_duties.amount.amount == "4900"
    assert view.customs_value_for_duties.basis == "estimated"
    assert view.customs_value_for_duties.source.startswith(
        "recorded estimated customs value EUR 4200 (SchwackeNet catalogue, "
    )
    sources = {s.fact: s.source for s in view.fact_sources}
    assert {sources[f] for f in ("price", "price_basis")} == {"recorded"}
    assert {sources[f] for f in ("estimated_customs_value_eur", "estimated_customs_value_source")} == {
        "recorded"
    }
    assert sources["co2"] == "listing" and sources["co2_document"] == "request"
    assert view.facts.price_basis == "recorded_actual" and view.facts.price.amount == "2800.00"
    assert view.facts.estimated_customs_value_source == "SchwackeNet catalogue"
    assert view.listing is not None
    assert view.listing.recorded_purchase_price_id == purchase_id
    assert view.listing.recorded_customs_value_id == customs_id
    assert any(n.startswith(f"RECORDED_PURCHASE_PRICE_USED:{purchase_id}:") for n in view.notes)
    assert any(n.startswith(f"RECORDED_CUSTOMS_VALUE_USED:{customs_id}:") for n in view.notes)
    # A paid price is evidence; the catalogue estimate is not.
    assert all(not u.startswith("price:") for u in view.unverified_inputs)
    assert any(
        u.startswith("customs_value: estimated customs value (SchwackeNet catalogue)")
        for u in view.unverified_inputs
    )


async def test_request_values_override_the_recorded_ones(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import values override")
    listing = add_listing(seed, ws, source, key)
    await record_customs_rate(db, ws)
    recorded = await record(db, ws, listing, purchase=PAID, customs_value=SCHWACKE)
    purchase_id, customs_id = recorded.recorded_ids
    # The request's price replaces the recorded one (with its own basis); the estimate stays.
    priced = await estimate(
        db, ws, request(listing, price={"amount": "3000", "currency": "EUR"}, price_basis="invoice")
    )
    sources = {s.fact: s.source for s in priced.fact_sources}
    assert sources["price"] == "request" and sources["estimated_customs_value_eur"] == "recorded"
    assert priced.purchase_price.amount.amount == "3000" and priced.purchase_price.basis == "invoice"
    assert priced.purchase_price.source == "invoice or contract price"
    assert priced.customs_value_for_duties.amount.amount == "4900"
    assert any(n.startswith(f"RECORDED_PURCHASE_PRICE_OVERRIDDEN:{purchase_id}:") for n in priced.notes)
    assert any(n.startswith(f"RECORDED_CUSTOMS_VALUE_USED:{customs_id}:") for n in priced.notes)
    assert _totals(priced)["transaction_value"][0] == "168155"  # G01 on the EUR 3,000 invoice
    # The request's estimate replaces the recorded one; the recorded purchase price stays.
    estimated = await estimate(
        db,
        ws,
        request(listing, estimated_customs_value_eur="3500", estimated_customs_value_source="owner estimate"),
    )
    sources = {s.fact: s.source for s in estimated.fact_sources}
    assert sources["estimated_customs_value_eur"] == "request" and sources["price"] == "recorded"
    assert estimated.customs_value_for_duties.source == "owner estimate"
    assert estimated.customs_value_for_duties.amount.amount == "4200"
    assert estimated.purchase_price.amount.amount == "2800"
    assert any(n.startswith(f"RECORDED_CUSTOMS_VALUE_OVERRIDDEN:{customs_id}:") for n in estimated.notes)
    # A purchase price recorded alone: the transaction value on it, no estimate, no alternative.
    only_price = add_listing(seed, ws, source, key)
    await record(
        db,
        ws,
        only_price,
        purchase=RecordedPurchase(amount=Decimal("2800"), kind="estimate", source="my guess"),
    )
    view = await estimate(db, ws, request(only_price))
    assert [s.name for s in view.scenarios] == ["primary"]
    assert view.customs_value_for_duties.basis == "transaction_value"
    assert view.customs_value_for_duties.amount.amount == "3500"
    assert view.customs_value_for_duties.source.startswith(
        "recorded estimate purchase price EUR 2800 (my guess"
    )
    assert view.purchase_price.basis == "recorded_estimate"
    assert "price: recorded estimate, not an invoice or contract price" in view.unverified_inputs
    assert _totals(view)["primary"] == ("165213", "6185")


async def test_a_lone_price_basis_cannot_relabel_the_recorded_purchase_price(
    db: Database, seed: Seed
) -> None:
    """Regression: a request ``price_basis`` without a ``price`` kept the owner's recorded amount
    but replaced its recorded kind (an unpaid estimate read as an invoice, without its unverified
    flag). The recorded price keeps its kind, as in the valuation; only a request price can carry
    another basis."""
    ws, source, key = workspace(seed, "Import values lone basis")
    listing = add_listing(seed, ws, source, key)  # advertised EUR 3,000
    await record_customs_rate(db, ws)
    guess = RecordedPurchase(amount=Decimal("2800"), kind="estimate", source="owner guess")
    await record(db, ws, listing, purchase=guess)
    for basis in ("invoice", "advertised"):
        with pytest.raises(ValidationFailed) as refused:
            await estimate(db, ws, request(listing, price_basis=basis))
        assert refused.value.details == {"fields": ["price_basis"]}
        assert "recorded purchase price keeps its recorded kind" in refused.value.message
    # Without the lone basis the recorded estimate stays labelled as one ...
    view = await estimate(db, ws, request(listing))
    assert view.purchase_price.basis == "recorded_estimate"
    assert "price: recorded estimate, not an invoice or contract price" in view.unverified_inputs
    # ... and a price given WITH its basis still replaces it.
    priced = await estimate(
        db, ws, request(listing, price={"amount": "2800", "currency": "EUR"}, price_basis="invoice")
    )
    assert (
        priced.purchase_price.basis == "invoice"
        and priced.purchase_price.source == "invoice or contract price"
    )
    # Without a recorded purchase price a lone basis still restates the listing's advertised price.
    plain = add_listing(seed, ws, source, key)
    stated = await estimate(db, ws, request(plain, price_basis="invoice"))
    assert stated.purchase_price.basis == "invoice" and stated.purchase_price.amount.amount == "3000"


async def test_without_recorded_values_listing_mode_is_unchanged(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import values none")
    listing = add_listing(seed, ws, source, key)
    await record_customs_rate(db, ws)
    view = await estimate(db, ws, request(listing))
    assert view.listing is not None and view.listing.recorded_purchase_price_id is None
    assert view.listing.recorded_customs_value_id is None
    assert view.purchase_price.basis == "advertised" and view.purchase_price.amount.amount == "3000"
    assert not any(n.startswith("RECORDED_") for n in view.notes)
    assert "recorded" not in {s.source for s in view.fact_sources}
    assert _totals(view) == {"primary": ("168155", "6433")}
