"""Import-cost calculator service (``persistence.queries.import_costs``) on a real database.

Modes (active / indicative_preview / unavailable), listing facts and request overrides, the
customs-rate sources (recorded, caller-supplied, none), workspace isolation, scope enforcement and
that the read persists nothing. The MK amounts are the fact sheet's golden cases G01/G03 of the
shipped DRAFT rule set. Every workspace, listing and rate is SYNTHETIC.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.import_costs.support import (
    AS_OF,
    CONFIG,
    CUSTOMS_RATE,
    DRAFT_FILE,
    activate,
    add_listing,
    counts,
    mk_draft,
    owner,
    record_customs_rate,
    revoke,
    store_rule_set,
    system,
    workspace,
)
from tests.integration.persistence_core.support import member

from suv_deals.domain.costs import load_cost_profile
from suv_deals.domain.enums import Co2Cycle, CostCategory, Role, Scope, TaxRuleStatus
from suv_deals.domain.listings import Co2Info
from suv_deals.domain.money import Money
from suv_deals.domain.tax_engine import RuleSet
from suv_deals.errors import Forbidden, NotFound
from suv_deals.persistence import valuation_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.queries import (
    ImportCostRequest,
    estimate_import_costs_for,
    get_import_cost_estimate,
)
from suv_deals.persistence.transactions import unit_of_work
from suv_deals.views.import_costs import ImportCostEstimateView

pytestmark = pytest.mark.db

G01 = "168154.45"  # D1: diesel, NEDC 189, EUR 3,000 + EUR 700 transport, no proof of origin
G03 = "217629.97"  # D3: petrol, NEDC 245, EUR 2,500 + EUR 700


def request(**kw: Any) -> ImportCostRequest:
    vehicle = kw.pop("vehicle", None)
    data: dict[str, Any] = {**kw}
    if vehicle is not None:
        data["vehicle"] = vehicle
    return ImportCostRequest.model_validate(data)


async def estimate(
    db: Database, ws: UUID, req: ImportCostRequest, *, config_dir: Path = CONFIG, actor: Any = None
) -> ImportCostEstimateView:
    who = actor or member(ws, Role.VIEWER)
    async with unit_of_work(db, who) as conn:
        result = await get_import_cost_estimate(conn, who, req, config_dir=config_dir, as_of=AS_OF)
    view = result.data
    assert ImportCostEstimateView.model_validate(view.model_dump(mode="json")) == view
    return view


def legal_total(view: ImportCostEstimateView) -> str | None:
    legal = view.scenarios[0]
    total = legal.total if view.mode == "active" else legal.indicative_total
    assert total is not None
    return total.amount


def config_without_drafts(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    (target / "cost_profiles").mkdir(parents=True)
    shutil.copy(CONFIG / "cost_profiles" / "default_unapproved.yaml", target / "cost_profiles")
    return target


# --------------------------------------------------------------------------- modes


async def test_preview_on_the_shipped_draft_from_listing_facts(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import costs preview")
    listing = add_listing(seed, ws, source, key)
    await record_customs_rate(db, ws)
    before = counts(seed, ws)
    view = await estimate(
        db,
        ws,
        request(listing_id=str(listing), vehicle={"co2_document": "coc", "declaration_date": "2026-10-13"}),
    )
    assert counts(seed, ws) == before  # a read: no valuation, cost line, rate, audit row or job
    assert view.mode == "indicative_preview" and not view.production_ready
    assert view.rule is not None and view.rule.rule_set_id == "mk-passenger-car-import"
    assert view.rule.status == TaxRuleStatus.DRAFT and view.banner is not None
    assert legal_total(view) == G01 and view.scenarios[0].total is None
    assert view.customs_fx_source == "recorded" and view.customs_fx_rate is not None
    assert view.customs_fx_rate.rate == CUSTOMS_RATE and view.customs_fx_rate.purpose == "customs"
    assert view.listing is not None and view.listing.listing_id == listing and not view.listing.is_fixture
    sources = {s.fact: s.source for s in view.fact_sources}
    assert sources["co2"] == "listing" and sources["co2_document"] == "request"
    assert sources["price"] == "listing" and sources["origin_proof"] == "default"
    assert view.facts.price.amount == "3000.00" and view.facts.co2_document == "coc"
    assert any(a.startswith("TRANSPORT_IN_CUSTOMS_VALUE:700.00 EUR") for a in view.assumptions)
    assert any(a.startswith("NO_ORIGIN_PROOF_STANDARD_DUTY") for a in view.assumptions)
    assert "COST_PROFILE:default@v3 (unapproved; not yet stored in this workspace)" in view.notes
    assert view.unverified_inputs == ("price: advertised price, not an invoice or contract price",)
    async with unit_of_work(db, member(ws, Role.VIEWER)) as conn:
        result = await get_import_cost_estimate(
            conn,
            member(ws, Role.VIEWER),
            request(listing_id=str(listing), vehicle={"declaration_date": "2026-10-13"}),
            config_dir=CONFIG,
            as_of=AS_OF,
        )
    codes = {w.code.value: w.message for w in result.warnings}
    assert codes["TAX_RULES_UNAPPROVED"].startswith("INDICATIVE PREVIEW")
    assert "unapproved draft rules" in codes["TAX_RULES_UNAPPROVED"]
    assert "co2: listing claim" in codes["SELLER_CLAIMS_UNVERIFIED"]
    assert result.data.facts.co2_document == "listing_claim_unverified"
    assert legal_total(result.data) == G01  # the draft accepts a listing claim, labelled as such


async def test_request_facts_override_the_listing_and_stand_alone(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import costs overrides")
    listing = add_listing(seed, ws, source, key)
    await record_customs_rate(db, ws)
    petrol = {
        "price": {"amount": "2500", "currency": "EUR"},
        "price_basis": "invoice",
        "fuel": "petrol",
        "co2": [{"cycle": "nedc", "g_km": "245"}],
        "co2_document": "coc",
        "declaration_date": "2026-10-13",
    }
    overridden = await estimate(db, ws, request(listing_id=str(listing), vehicle=petrol))
    assert legal_total(overridden) == G03 and overridden.unverified_inputs == ()
    sources = {s.fact: s.source for s in overridden.fact_sources}
    assert {sources[f] for f in petrol} == {"request"}
    assert sources["first_registration"] == "listing" and sources["seller_country"] == "listing"
    alone = await estimate(db, ws, request(vehicle=petrol))
    assert alone.listing is None and legal_total(alone) == G03
    assert "listing" not in {s.source for s in alone.fact_sources}
    # Caller readings without a stated document never inherit the listing's claim label.
    readings = await estimate(
        db,
        ws,
        request(listing_id=str(listing), vehicle={"co2": [{"cycle": "wltp", "g_km": "160"}]}),
    )
    assert readings.facts.co2_document is None and not readings.scenarios[0].complete
    assert "co2_document" in readings.missing_facts
    assert any(w.startswith("CO2_DOCUMENT_UNKNOWN") for w in readings.warnings)
    # A claimed proof of origin: 0% duty in the legal base, the standard duty shown beside it.
    proof = await estimate(
        db,
        ws,
        request(
            listing_id=str(listing),
            vehicle={"co2_document": "coc", "origin_proof": {"kind": "eu", "country": "DE"}},
        ),
    )
    assert [s.name for s in proof.scenarios] == ["legal_base", "standard_duty_if_proof_rejected"]
    assert legal_total(proof) == "154721.64"  # G02
    duty = {c.category: c for c in proof.scenarios[1].categories}[CostCategory.IMPORT_DUTY]
    assert duty.amount.amount == "11383.73"


async def test_active_rule_set_wins_and_the_preview_is_only_the_fallback(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import costs active")
    listing = add_listing(seed, ws, source, key)
    await record_customs_rate(db, ws)
    stored = await activate(db, owner(ws), mk_draft())
    view = await estimate(
        db, ws, request(listing_id=str(listing), vehicle={"co2_document": "coc", "price_basis": "invoice"})
    )
    assert view.mode == "active" and view.banner is None and view.production_ready
    assert view.rule is not None and view.rule.status == TaxRuleStatus.ACTIVE and view.rule.sha256_recorded
    assert view.rule.content_sha256 == stored.rule_set.sha256
    legal = view.scenarios[0]
    assert legal.total is not None and legal.total.amount == G01 and legal.indicative_total is None
    assert legal.production_ready
    assert seed.scalar("select count(*) from app.valuations where workspace_id = %s", (ws,)) == 0


async def test_unavailable_without_any_rule_set(db: Database, seed: Seed, tmp_path: Path) -> None:
    ws, source, key = workspace(seed, "Import costs unavailable")
    listing = add_listing(seed, ws, source, key)
    await record_customs_rate(db, ws)
    async with unit_of_work(db, member(ws, Role.VIEWER)) as conn:
        result = await get_import_cost_estimate(
            conn,
            member(ws, Role.VIEWER),
            request(listing_id=str(listing)),
            config_dir=config_without_drafts(tmp_path),
            as_of=AS_OF,
        )
    view = result.data
    assert view.mode == "unavailable" and view.scenarios == () and view.rule is None
    assert view.missing_facts[0] == "tax_rule_set" and view.banner is not None
    assert view.facts.price.amount == "3000.00"  # the facts are still shown
    assert [w.code.value for w in result.warnings][:2] == ["TAX_RULES_UNAPPROVED", "UNKNOWN_COSTS"]


async def test_conflicting_or_revoked_stored_copies_leave_it_unavailable(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import costs conflict")
    listing = add_listing(seed, ws, source, key)
    await record_customs_rate(db, ws)
    doc = json.loads(DRAFT_FILE.read_text(encoding="utf-8"), parse_float=Decimal)
    doc["description"] = "SYNTHETIC edited copy of the draft with the same id and version"
    await store_rule_set(db, owner(ws), RuleSet.model_validate(doc))
    view = await estimate(db, ws, request(listing_id=str(listing)))
    assert view.mode == "unavailable" and "CONFLICTING_RULE_SET_COPIES" in view.selection_reason
    other_ws, other_source, other_key = workspace(seed, "Import costs revoked")
    other_listing = add_listing(seed, other_ws, other_source, other_key)
    await revoke(db, owner(other_ws), mk_draft())
    revoked = await estimate(db, other_ws, request(listing_id=str(other_listing)))
    assert revoked.mode == "unavailable"
    assert any("revoked" in c for c in revoked.considered)
    # An identical stored draft merges with the file.
    third_ws, third_source, third_key = workspace(seed, "Import costs same draft")
    third_listing = add_listing(seed, third_ws, third_source, third_key)
    await record_customs_rate(db, third_ws)
    await store_rule_set(db, owner(third_ws), mk_draft())
    merged = await estimate(db, third_ws, request(listing_id=str(third_listing)))
    assert merged.mode == "indicative_preview" and legal_total(merged) == G01


# --------------------------------------------------------------------------- customs FX


async def test_missing_customs_rate_names_the_fx_record_command(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import costs no FX")
    listing = add_listing(seed, ws, source, key)
    view = await estimate(db, ws, request(listing_id=str(listing)))
    assert view.customs_fx_source == "none" and view.customs_fx_rate is None
    legal = view.scenarios[0]
    # The customs value is known in EUR; without a customs rate it never reaches MKD.
    assert not legal.complete and legal.customs_value.amount == "3700.00"
    cv_mkd = next(c for c in legal.components if c.component_id == "customs_value_mkd")
    assert cv_mkd.status == "unknown" and "CUSTOMS_FX_RATE_MISSING:customs_value" in cv_mkd.warnings
    assert legal.indicative_total is not None and legal.indicative_total.amount is None
    assert "customs_fx_rate" in view.missing_facts
    hint = next(w for w in view.warnings if w.startswith("CUSTOMS_FX_RATE_NOT_RECORDED"))
    assert "suv-deals fx record --base EUR --quote MKD" in hint and "--purpose customs" in hint
    assert "--date 2026-10-01" in hint and "customs_eur_mkd_rate" in hint


async def test_caller_supplied_rate_is_labelled_and_never_stored(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import costs caller FX")
    listing = add_listing(seed, ws, source, key)
    before = counts(seed, ws)
    view = await estimate(db, ws, request(listing_id=str(listing), customs_eur_mkd_rate=CUSTOMS_RATE))
    assert counts(seed, ws) == before
    assert view.customs_fx_source == "caller_supplied" and legal_total(view) == G01
    assert (
        view.customs_fx_rate is not None and view.customs_fx_rate.provider == "caller-supplied (not recorded)"
    )
    assert view.customs_fx_rate.rate_date == "2026-10-01"
    assert any(a.startswith("CALLER_SUPPLIED_CUSTOMS_RATE:1 EUR = 61.5337 MKD") for a in view.assumptions)
    # A rate recorded for the declaration month wins over the caller's.
    await record_customs_rate(db, ws, value="61.5")
    recorded = await estimate(db, ws, request(listing_id=str(listing), customs_eur_mkd_rate=CUSTOMS_RATE))
    assert recorded.customs_fx_source == "recorded" and recorded.customs_fx_rate is not None
    assert recorded.customs_fx_rate.rate == "61.5"
    assert any(w.startswith("CALLER_CUSTOMS_RATE_IGNORED") for w in recorded.warnings)


async def test_a_previous_month_rate_is_never_silently_used(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import costs old FX")
    listing = add_listing(seed, ws, source, key)
    await record_customs_rate(db, ws, on=date(2026, 9, 1), value="61.6")
    old = await estimate(db, ws, request(listing_id=str(listing)))
    assert not old.scenarios[0].complete
    # Regression: the stale rate was shown as the rate used ("recorded", eur_basis naming it) and
    # missing_facts was empty, so the envelope said "needed: see the unknown components".
    assert old.customs_fx_source == "recorded_not_effective" and old.customs_fx_rate is None
    assert "customs_fx_rate" in old.missing_facts
    assert old.eur_basis.startswith("no customs rate effective for 2026-10") and "not used" in old.eur_basis
    assert any(w.startswith("CUSTOMS_FX_RATE_NOT_FOR_DECLARATION_MONTH") for w in old.warnings)
    async with unit_of_work(db, member(ws, Role.VIEWER)) as conn:
        result = await get_import_cost_estimate(
            conn, member(ws, Role.VIEWER), request(listing_id=str(listing)), config_dir=CONFIG, as_of=AS_OF
        )
    unknown = {w.code.value: w.message for w in result.warnings}["UNKNOWN_COSTS"]
    assert "customs_fx_rate" in unknown and "see the unknown components" not in unknown
    cv_mkd = next(c for c in old.scenarios[0].components if c.component_id == "customs_value_mkd")
    assert cv_mkd.status == "unknown" and "CUSTOMS_FX_RATE_NOT_EFFECTIVE" in cv_mkd.warnings
    accepted = await estimate(db, ws, request(listing_id=str(listing), accept_previous_month_fx=True))
    assert accepted.scenarios[0].complete
    assert accepted.customs_fx_source == "recorded" and accepted.customs_fx_rate is not None
    assert "customs_fx_rate" not in accepted.missing_facts
    assert any(a.startswith("CUSTOMS_FX_PREVIOUS_MONTH_ACCEPTED") for a in accepted.assumptions)
    caller = await estimate(db, ws, request(listing_id=str(listing), customs_eur_mkd_rate=CUSTOMS_RATE))
    assert caller.customs_fx_source == "caller_supplied" and legal_total(caller) == G01
    assert any(w.startswith("CUSTOMS_FX_RATE_NOT_FOR_DECLARATION_MONTH") for w in caller.warnings)


async def test_the_cost_profile_note_follows_the_stored_approval(db: Database, seed: Seed) -> None:
    """Regression: the note always repeated the configured file's "unapproved", although the
    owner approves the STORED version in Settings and valuations use that approved row."""
    ws, source, key = workspace(seed, "Import costs profile note")
    listing = add_listing(seed, ws, source, key)
    configured = load_cost_profile(CONFIG / "cost_profiles" / "default_unapproved.yaml")
    tag = f"COST_PROFILE:{configured.profile_key}@v{configured.version}"
    first = await estimate(db, ws, request(listing_id=str(listing)))
    assert f"{tag} (unapproved; not yet stored in this workspace)" in first.notes
    async with unit_of_work(db, owner(ws)) as conn:
        stored = await valuation_repo.store_cost_profile(conn, owner(ws), configured)
    unapproved = await estimate(db, ws, request(listing_id=str(listing)))
    assert f"{tag} (unapproved)" in unapproved.notes
    async with unit_of_work(db, owner(ws)) as conn:
        await valuation_repo.approve_cost_profile(conn, owner(ws), stored.id)
    approved = await estimate(db, ws, request(listing_id=str(listing)))
    assert f"{tag} (approved)" in approved.notes
    assert not any("unapproved" in n for n in approved.notes if n.startswith("COST_PROFILE:"))


async def test_a_listing_co2_of_zero_for_a_diesel_is_no_fact(db: Database, seed: Seed) -> None:
    """Regression: a listing's CO2 0 g/km for a diesel gave a complete estimate with ДМВ 0."""
    ws, source, key = workspace(seed, "Import costs zero CO2")
    listing = add_listing(seed, ws, source, key, co2=Co2Info(g_per_km=Decimal(0), cycle=Co2Cycle.WLTP))
    await record_customs_rate(db, ws)
    view = await estimate(
        db, ws, request(listing_id=str(listing), vehicle={"declaration_date": "2026-10-13"})
    )
    assert not view.scenarios[0].complete and view.facts.co2 == ()
    assert {"co2", "co2_document"} <= set(view.missing_facts)
    assert any(w.startswith("LISTING_CO2_NOT_A_FACT:0") for w in view.warnings), view.warnings


# --------------------------------------------------------------------------- listings, isolation, scope


async def test_missing_foreign_and_quarantined_listings_are_not_found(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import costs mine")
    other_ws, other_source, other_key = workspace(seed, "Import costs theirs")
    foreign = add_listing(seed, other_ws, other_source, other_key)
    for listing_id in (foreign, uuid.uuid4()):
        with pytest.raises(NotFound):
            await estimate(db, ws, request(listing_id=str(listing_id)))
    quarantined = add_listing(seed, ws, source, key)
    seed.conn.execute(
        "update app.listings set quarantined = true, quarantine_reason = 'identity_hash_collision',"
        " row_version = row_version + 1 where workspace_id = %s and id = %s",
        (ws, quarantined),
    )
    with pytest.raises(NotFound):
        await estimate(db, ws, request(listing_id=str(quarantined)))
    unrevised = seed.listing(ws, source)
    with pytest.raises(NotFound):
        await estimate(db, ws, request(listing_id=str(unrevised)))


async def test_unscreened_listing_leaves_the_price_unknown(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import costs unscreened")
    listing = add_listing(seed, ws, source, key, screened=False)
    await record_customs_rate(db, ws)
    view = await estimate(db, ws, request(listing_id=str(listing)))
    assert view.listing is not None and not view.listing.screened
    assert view.facts.price.status == "unknown" and "price" in view.missing_facts
    assert any(w.startswith("LISTING_NOT_SCREENED") for w in view.warnings)
    priced = await estimate(
        db,
        ws,
        request(listing_id=str(listing), vehicle={"price": {"amount": "3000", "currency": "EUR"}}),
    )
    assert legal_total(priced) == G01


async def test_scope_is_enforced(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import costs scope")
    listing = add_listing(seed, ws, source, key)
    nobody = member(ws, Role.VIEWER, scopes=frozenset({Scope.REVIEWS_READ}))
    with pytest.raises(Forbidden):
        await estimate(db, ws, request(listing_id=str(listing)), actor=nobody)
    # The system actor (CLI operator) and every role with deals:read may estimate.
    for actor in (system(ws), member(ws, Role.REVIEWER), owner(ws)):
        async with unit_of_work(db, actor) as conn:
            result = await estimate_import_costs_for(
                conn, actor, request(listing_id=str(listing)), config_dir=CONFIG, as_of=AS_OF
            )
        assert result.estimate.mode == "indicative_preview"


async def test_fixture_lineage_listing_is_labelled_and_uses_fixture_rates(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import costs fixture", mode="fixture")
    listing = add_listing(seed, ws, source, key, is_fixture=True)
    await record_customs_rate(db, ws, is_fixture=True)
    async with unit_of_work(db, member(ws, Role.VIEWER)) as conn:
        result = await get_import_cost_estimate(
            conn, member(ws, Role.VIEWER), request(listing_id=str(listing)), config_dir=CONFIG, as_of=AS_OF
        )
    view = result.data
    assert view.listing is not None and view.listing.is_fixture and view.listing.fixture_label
    assert "FIXTURE_DATA" in {w.code.value for w in result.warnings}
    assert view.customs_fx_source == "recorded" and legal_total(view) == G01
    # A fixture rate never serves a real (non-listing) request.
    real = await estimate(db, ws, request(vehicle={"price": {"amount": "3000", "currency": "EUR"}}))
    assert real.customs_fx_source == "none"


async def test_chf_listing_price_leaves_the_customs_value_unknown(db: Database, seed: Seed) -> None:
    ws, source, key = workspace(seed, "Import costs CHF")
    listing = add_listing(seed, ws, source, key, payable=Money.of("3200.00", "CHF"))
    await record_customs_rate(db, ws)
    view = await estimate(db, ws, request(listing_id=str(listing)))
    assert view.facts.price.currency == "CHF" and view.scenarios[0].customs_value.status == "unknown"
    assert any(w.startswith("CUSTOMS_VALUE_CHF_UNSUPPORTED") for w in view.warnings)
