"""The valuation pipeline computes import tax through the SHARED builder (`import_estimate`).

Once an ACTIVE rule set exists, a valuation gets the duty, motor-vehicle tax and import VAT the
calculator shows for the same listing: the advertised payable amount as the transaction value, the
cost profile's EUR 700 transport in the customs value, no proof of origin, the customs monthly
rate. Lines computed from such claims are never rule- or quote-supported, and the builder's
assumptions and the Euro import gate are recorded as valuation warnings. Production selection is
unchanged: only an ACTIVE rule set is ever used (no preview). Everything is SYNTHETIC: the
fixture dealer pages, the jurisdiction-XX rule set, and the MK draft activated only inside this
throwaway workspace.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from tests.integration.db.helpers import Seed
from tests.integration.import_costs.support import CONFIG, activate, mk_draft, record_customs_rate
from tests.integration.pipeline.support import PipelineEnv, build_env, by_slid, run, seed_comparables, seed_fx

from suv_deals.clock import SystemClock
from suv_deals.crawling.scheduler import run_scheduler_tick
from suv_deals.domain.costs import CostLine
from suv_deals.domain.enums import (
    CostCategory,
    CostLineStatus,
    FxPurpose,
    JobState,
    TaxRuleStatus,
    ValuationState,
)
from suv_deals.domain.money import Money
from suv_deals.domain.tax_engine import IMPORT_CATEGORIES, ComponentStatus, RuleSet, RuleSource
from suv_deals.persistence import valuation_repo
from suv_deals.persistence.database import Conn
from suv_deals.persistence.queries import ImportCostRequest, ImportCostResult, estimate_import_costs_for
from suv_deals.workers.reconciliation import ReconcileOptions, Reconciler
from suv_deals.workers.runner import Worker
from suv_deals.workers.runtime import RuntimeOptions

pytestmark = pytest.mark.db

CUSTOMS = "61.5"


def synthetic_xx_rules() -> RuleSet:
    """SYNTHETIC jurisdiction XX: every rate and amount is invented. It needs only inputs the
    shared builder supplies from a listing (customs value, origin, the customs rate)."""
    origin_list = ["AT", "DE", "IT"]
    return RuleSet.model_validate(
        {
            "rule_set_id": "SYNTHETIC-TEST-ONLY-xx-builder",
            "jurisdiction": "XX",
            "version": "synthetic-1",
            "status": "draft",
            "valid_from": "2026-01-01",
            "valid_to": None,
            "currency": "MKD",
            "vehicle_categories": ["passenger_car"],
            "description": "SYNTHETIC TEST RULES under code XX; not a tax of any country.",
            "sources": [
                RuleSource(
                    url="https://tax-authority.example/SYNTHETIC.pdf",
                    title="SYNTHETIC source",
                    retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
                    sha256="0" * 64,
                )
            ],
            "required_inputs": [
                "declaration_date",
                "customs_value",
                "customs_value_basis",
                "customs_fx_rate",
                "preferential_origin_country",
            ],
            "components": [
                {
                    "id": "duty_listed",
                    "kind": "percentage",
                    "label": "SYNTHETIC duty, listed origin",
                    "category": "import_duty",
                    "currency": "MKD",
                    "alternative_group": "duty",
                    "applies_when": [
                        {"input": "preferential_origin_country", "op": "in", "value": origin_list}
                    ],
                    "rate": "0.02",
                    "base": ["customs_value"],
                },
                {
                    "id": "duty_standard",
                    "kind": "percentage",
                    "label": "SYNTHETIC duty, standard",
                    "category": "import_duty",
                    "currency": "MKD",
                    "alternative_group": "duty",
                    "applies_when": [
                        {"input": "preferential_origin_country", "op": "not_in", "value": origin_list}
                    ],
                    "rate": "0.10",
                    "base": ["customs_value"],
                },
                {
                    "id": "vehicle_charge",
                    "kind": "fixed",
                    "label": "SYNTHETIC flat vehicle charge",
                    "category": "motor_vehicle_tax",
                    "currency": "MKD",
                    "amount": "1000",
                },
                {
                    "id": "processing_fee",
                    "kind": "fixed",
                    "label": "SYNTHETIC processing fee",
                    "category": "other_import_charges",
                    "currency": "MKD",
                    "amount": "123.45",
                },
                {
                    "id": "vat",
                    "kind": "percentage",
                    "label": "SYNTHETIC VAT",
                    "category": "import_vat",
                    "currency": "MKD",
                    "depends_on": ["duty_listed", "duty_standard", "vehicle_charge"],
                    "rate": "0.20",
                    "base": ["customs_value", "duty_listed", "duty_standard", "vehicle_charge"],
                },
            ],
            "rounding_rules": {
                "component_default": {"quantum": "0.01", "mode": "half_up", "stage": "before_dependents"},
                "total": {"quantum": "0.01", "mode": "half_up", "stage": "before_dependents"},
            },
            "sha256": None,
            "is_fixture": False,
        }
    )


def _month_start() -> date:
    return datetime.now(UTC).date().replace(day=1)


def _previous_month_start(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


async def _value_fixture_listings(
    env: PipelineEnv, *, customs_on: date | None = None, customs: bool = True
) -> None:
    await seed_comparables(env)
    await seed_fx(env)  # reference EUR/MKD for the scenarios
    if customs:
        await record_customs_rate(
            env.ctx.db, env.workspace_id, on=customs_on or _month_start(), value=CUSTOMS
        )
    tick = await run_scheduler_tick(
        env.ctx.db, env.ctx.settings, SystemClock(), workspace_ids=[env.workspace_id]
    )
    assert any(w.workspace_id == env.workspace_id and w.enqueued for w in tick.workspaces)
    await _work(env)


async def _work(env: PipelineEnv) -> None:
    reports = await Worker(env.ctx, workspace_ids=[env.workspace_id], worker_id="worker-tax").run_until_idle()
    assert all(r.state == JobState.SUCCEEDED for r in reports), [(r.job_type, r.code) for r in reports]


def _xx_env_options() -> RuntimeOptions:
    return RuntimeOptions(job_lease_seconds=120, heartbeat_seconds=40, tax_jurisdiction="XX")


async def _valuation(env: PipelineEnv, slid: str) -> valuation_repo.StoredValuation:
    listing_id = by_slid(env)[slid]["id"]

    async def go(conn: Conn) -> valuation_repo.StoredValuation:
        stored = await valuation_repo.current_valuation(conn, env.system, listing_id)
        assert stored is not None
        return stored

    return await run(env.ctx, env.system, go)


def _import_lines(stored: valuation_repo.StoredValuation) -> dict[CostCategory, CostLine]:
    return {ln.category: ln for ln in stored.inputs.cost_lines if ln.category in IMPORT_CATEGORIES}


async def test_valuation_tax_comes_from_the_shared_builder(db_url: str, seed: Seed) -> None:
    env = await build_env(
        db_url,
        seed,
        options=_xx_env_options(),
        name="Tax builder",
    )
    try:
        stored_rule = await activate(env.ctx.db, env.owner, synthetic_xx_rules())
        await _value_fixture_listings(env)
        stored = await _valuation(env, "TEST-204")
        valuation = stored.valuation
        tax = valuation.tax
        assert tax is not None and tax.rule_status == TaxRuleStatus.ACTIVE
        assert stored.refs.tax_rule_set_row_id == stored_rule.row_for("passenger_car")
        # Customs value = advertised EUR 2,750 + the profile's EUR 700 transport, at the customs rate.
        assert tax.complete and tax.production_ready
        amounts = {c.component_id: c.amount for c in tax.components if c.status == ComponentStatus.RESOLVED}
        assert amounts == {
            "duty_standard": Money.of("21217.50", "MKD"),  # 10% of 3,450 x 61.5 = 212,175.00 (no proof)
            "vehicle_charge": Money.of("1000.00", "MKD"),
            "processing_fee": Money.of("123.45", "MKD"),
            "vat": Money.of("46878.50", "MKD"),  # 20% of (212,175.00 + 21,217.50 + 1,000)
        }
        assert tax.total_import_cost == Money.of("69219.45", "MKD")
        assert tax.customs_fx_rate is not None and tax.customs_fx_rate.rate == Decimal(CUSTOMS)
        # A customs rate is a monthly list rate: the valuation expires when its month ends.
        next_month = (_month_start() + timedelta(days=32)).replace(day=1)
        assert valuation.expires_at is not None
        assert valuation.expires_at <= datetime.combine(next_month, datetime.min.time(), tzinfo=UTC)
        used = next(c for c in tax.components if c.component_id == "duty_standard").inputs_used
        assert "3450" in used["customs_value"]
        # Estimated from claims (the advertised price): never rule- or quote-supported.
        lines = _import_lines(stored)
        assert {c: ln.status for c, ln in lines.items()} == dict.fromkeys(
            IMPORT_CATEGORIES, CostLineStatus.ESTIMATED
        )
        assert all(not ln.rule_supported and not ln.assumption_approved for ln in lines.values())
        assert all(ln.reason is not None and "advertised price" in ln.reason for ln in lines.values())
        assert valuation.scenarios is not None and valuation.scenarios.material_support != "quote_supported"
        transport = [ln for ln in stored.inputs.cost_lines if ln.category == CostCategory.TRANSPORT]
        assert [ln.base for ln in transport] == [Money.of("700.00", "EUR")]
        warnings = valuation.warnings
        assert any(
            w.startswith("TAX_INPUT_ASSUMPTION:ADVERTISED_PRICE_AS_TRANSACTION_VALUE") for w in warnings
        )
        assert any(w.startswith("TAX_INPUT_ASSUMPTION:NO_ORIGIN_PROOF_STANDARD_DUTY") for w in warnings)
        assert any(w.startswith("TAX_INPUT_UNVERIFIED:price: advertised price") for w in warnings)
        assert any(w.startswith("IMPORT_GATE:") for w in warnings)
        assert not valuation.alert_eligible
        view = stored.view()
        assert view.tax is not None and not any(c.intermediate for c in view.tax.components)
    finally:
        await env.close()


async def test_valuation_and_calculator_agree_once_the_mk_rules_are_active(db_url: str, seed: Seed) -> None:
    env = await build_env(db_url, seed, name="Tax parity")
    try:
        await activate(env.ctx.db, env.owner, mk_draft())
        await _value_fixture_listings(env)
        stored = await _valuation(env, "TEST-204")
        tax = stored.valuation.tax
        assert tax is not None and tax.rule_set_id == "mk-passenger-car-import"
        # The fixture listing states no CO2 document: duty resolves, ДМВ and VAT stay unknown.
        status = {c.component_id: c.status for c in tax.components}
        assert status["duty_standard"] == ComponentStatus.RESOLVED
        assert status["dmv_full"] == ComponentStatus.UNKNOWN and not tax.complete
        lines = _import_lines(stored)
        assert lines[CostCategory.IMPORT_DUTY].status == CostLineStatus.ESTIMATED
        assert not lines[CostCategory.IMPORT_DUTY].rule_supported
        assert lines[CostCategory.MOTOR_VEHICLE_TAX].status == CostLineStatus.UNKNOWN
        view = stored.view()
        assert view.tax is not None
        helpers = {c.component_id for c in view.tax.components if c.intermediate}
        assert {"customs_value_mkd", "vmv_mkd", "vat_base_mkd"} <= helpers
        # The calculator, on the same listing and declaration date, gives the same components.
        declared: date = tax.effective_date
        request = ImportCostRequest.model_validate(
            {"listing_id": str(stored.listing_id), "vehicle": {"declaration_date": declared.isoformat()}}
        )

        async def calculator(conn: Conn) -> ImportCostResult:
            return await estimate_import_costs_for(
                conn, env.system, request, config_dir=CONFIG, as_of=stored.valuation.created_at
            )

        result = await run(env.ctx, env.system, calculator)
        estimate = result.estimate
        assert estimate.mode == "active"
        legal = estimate.scenarios[0]
        assert [(c.component_id, c.status, c.amount) for c in legal.components] == [
            (c.component_id, c.status, c.amount) for c in tax.components
        ]
    finally:
        await env.close()


async def test_a_new_months_customs_rate_revalues_what_last_months_rate_left_incomplete(
    db_url: str, seed: Seed
) -> None:
    """Regression: a valuation built with last month's customs rate (not effective) recorded no FX
    dependency, and recording this month's rate never invalidated it, so it stayed incomplete."""
    env = await build_env(db_url, seed, options=_xx_env_options(), name="Tax customs month")
    try:
        await activate(env.ctx.db, env.owner, synthetic_xx_rules())
        last_month = _previous_month_start(_month_start())
        await _value_fixture_listings(env, customs_on=last_month)
        before = await _valuation(env, "TEST-204")
        tax = before.valuation.tax
        assert tax is not None and not tax.complete and tax.customs_fx_rate is None
        assert before.valuation.state == ValuationState.INCOMPLETE  # no deadline from a past month
        customs = [d for d in before.valuation.dependencies.fx if d.purpose == FxPurpose.CUSTOMS]
        assert [d.rate_date for d in customs] == [last_month]
        await record_customs_rate(env.ctx.db, env.workspace_id, on=_month_start(), value=CUSTOMS)
        report = await Reconciler(env.ctx, ReconcileOptions()).reconcile_workspace(env.workspace_id)
        assert report.valuations_invalidated >= 1 and report.recompute_jobs >= 1
        await _work(env)
        after = await _valuation(env, "TEST-204")
        assert after.valuation.tax is not None and after.valuation.tax.complete
        used = after.valuation.tax.customs_fx_rate
        assert used is not None and used.rate_date == _month_start()
        again = await Reconciler(env.ctx, ReconcileOptions()).reconcile_workspace(env.workspace_id)
        assert again.valuations_invalidated == 0  # the current rate invalidates nothing more
    finally:
        await env.close()


async def test_the_first_customs_rate_revalues_valuations_computed_without_one(
    db_url: str, seed: Seed
) -> None:
    """Regression: valuations made under ACTIVE rules before any customs rate was recorded had no
    FX dependency to invalidate, so recording the first rate never re-valued them."""
    env = await build_env(db_url, seed, options=_xx_env_options(), name="Tax customs first")
    try:
        await activate(env.ctx.db, env.owner, synthetic_xx_rules())
        await _value_fixture_listings(env, customs=False)
        before = await _valuation(env, "TEST-204")
        assert before.valuation.tax is not None and not before.valuation.tax.complete
        assert not [d for d in before.valuation.dependencies.fx if d.purpose == FxPurpose.CUSTOMS]
        quiet = await Reconciler(env.ctx, ReconcileOptions()).reconcile_workspace(env.workspace_id)
        assert quiet.valuations_invalidated == 0  # no customs rate yet: nothing to re-value
        await record_customs_rate(env.ctx.db, env.workspace_id, on=_month_start(), value=CUSTOMS)
        report = await Reconciler(env.ctx, ReconcileOptions()).reconcile_workspace(env.workspace_id)
        assert report.valuations_invalidated >= 1
        await _work(env)
        after = await _valuation(env, "TEST-204")
        assert after.valuation.tax is not None and after.valuation.tax.complete
        settled = await Reconciler(env.ctx, ReconcileOptions()).reconcile_workspace(env.workspace_id)
        assert settled.valuations_invalidated == 0
    finally:
        await env.close()
