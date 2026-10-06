"""Valuation assembly, fingerprint, state and invalidation (spec sections 14 and 18).

All inputs are SYNTHETIC test data (rule sets, quotes, comparables, rates).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from suv_deals.domain.costs import (
    REQUIRED_CATEGORIES,
    CostLine,
    CostProfileRef,
    ProceedsEstimate,
    PurchaseInput,
    ScenarioSet,
    compute_scenarios,
    tax_cost_lines,
)
from suv_deals.domain.enums import (
    Co2Cycle,
    EligibilityState,
    FxPurpose,
    ProfileKey,
    ScenarioName,
    TaxRuleStatus,
    ValuationState,
)
from suv_deals.domain.enums import CostCategory as C
from suv_deals.domain.enums import CostLineStatus as S
from suv_deals.domain.money import FxRate, Money
from suv_deals.domain.profiles import ContributionThreshold
from suv_deals.domain.tax_engine import (
    IMPORT_CATEGORIES,
    Classification,
    OriginProof,
    OriginProofStatus,
    ReviewRecord,
    RuleSet,
    RuleSource,
    TaxCalculation,
    TaxInputs,
    calculate,
    compute_rule_set_sha256,
    load_rule_set_file,
    transition_rule_set,
)
from suv_deals.domain.valuation import (
    CALCULATION_VERSION,
    ComparableReference,
    InvalidationReason,
    ScreeningInput,
    TaxRuleDependency,
    Valuation,
    ValuationDependencies,
    assemble_valuation,
    build_dependencies,
    is_stale,
    mark_stale,
)
from suv_deals.errors import ValidationFailed

REPO = Path(__file__).resolve().parents[2]
TAX_FIXTURE = REPO / "tests" / "fixtures" / "tax" / "synthetic_rule_set.json"
AS_OF = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
DECL = date(2026, 11, 2)
PROPOSED = ContributionThreshold()
APPROVED = ContributionThreshold(
    approval_status="approved", approved_by="SYNTHETIC owner", approved_at="2026-10-01"
)
REV = "SYNTHETIC-listing-revision-1"
CONFIG = "SYNTHETIC-config-revision-1"
PROFILE = CostProfileRef(
    profile_key="default", version=1, sha256="b" * 64, approval_status="unapproved", is_fixture=False
)
MKD_RATE = FxRate(
    base="EUR",
    quote="MKD",
    rate=Decimal("61.5"),
    rate_date=date(2026, 10, 5),
    retrieved_at=datetime(2026, 10, 5, 16, tzinfo=UTC),
    provider="SYNTHETIC owner-approved MKD source",
)


# --------------------------------------------------------------------------- builders


def eur(value: str) -> Money:
    return Money.of(value, "EUR")


def _rule(status: TaxRuleStatus) -> RuleSet:
    """SYNTHETIC non-fixture rule set driven through the real lifecycle to ``status``."""
    fixture = load_rule_set_file(TAX_FIXTURE)
    source = RuleSource(
        url="https://example.invalid/SYNTHETIC", title="SYNTHETIC", retrieved_at=AS_OF, sha256="0" * 64
    )
    draft = RuleSet.model_validate(
        {**fixture.model_dump(), "status": "draft", "is_fixture": False, "sha256": None, "sources": [source]}
    )
    under_review = transition_rule_set(draft, TaxRuleStatus.UNDER_REVIEW, at=AS_OF)
    record = ReviewRecord(
        reviewer="SYNTHETIC reviewer",
        reviewed_at=AS_OF,
        content_sha256=compute_rule_set_sha256(under_review),
        scope="SYNTHETIC",
    )
    approved = transition_rule_set(
        under_review, TaxRuleStatus.APPROVED, at=AS_OF, approved_by="SYNTHETIC owner", review_record=record
    )
    return approved if status == TaxRuleStatus.APPROVED else transition_rule_set(approved, status, at=AS_OF)


def tax_inputs(**kw: Any) -> TaxInputs:
    values: dict[str, Any] = {
        "declaration_date": DECL,
        "classification": Classification(
            tariff_code="8703 23",
            evidence_ids=("e",),
            approval_status="approved",
            approved_by="SYNTHETIC owner",
        ),
        "origin_proof": OriginProof(proof_type="none", acceptance_status=OriginProofStatus.NOT_AVAILABLE),
        "customs_value": Money.of("100000.00", "MKD"),
        "customs_value_basis": "SYNTHETIC",
        "co2_g_km": Decimal("120"),
        "co2_cycle": Co2Cycle.WLTP,
        "vehicle_age_years": Decimal("12"),
        "engine_displacement_cm3": Decimal("1995"),
    }
    values.update(kw)
    return TaxInputs(**values)


def active_calc() -> TaxCalculation:
    calc = calculate(_rule(TaxRuleStatus.ACTIVE), tax_inputs(), AS_OF)
    assert calc.production_ready
    return calc


def fixture_calc() -> TaxCalculation:
    return calculate(load_rule_set_file(TAX_FIXTURE), tax_inputs(), AS_OF)


def other_lines(*, quoted: bool = False, expires_at: datetime | None = None) -> list[CostLine]:
    status = S.QUOTED if quoted else S.ESTIMATED
    extra: dict[str, Any] = (
        {"evidence_ids": ("SYNTHETIC-quote",), "provider": "SYNTHETIC provider"} if quoted else {}
    )
    lines = [
        CostLine(
            category=c,
            label=f"SYNTHETIC {c.value}",
            status=status,
            currency="EUR",
            base=eur(v),
            expires_at=expires_at,
            **extra,
        )
        for c, v in ((C.TRANSPORT, "700.00"), (C.CUSTOMS_BROKER, "250.00"), (C.REPAIRS, "800.00"))
    ]
    lines += [
        CostLine(
            category=c,
            label=f"SYNTHETIC {c.value}",
            status=S.ESTIMATED,
            currency="EUR",
            base=eur(v),
            assumption_approved=True,
        )
        for c, v in ((C.RISK_RESERVE, "600.00"), (C.SELLING_COSTS, "150.00"))
    ]
    covered = {ln.category for ln in lines} | IMPORT_CATEGORIES
    lines += [
        CostLine(
            category=c,
            label="SYNTHETIC n/a",
            status=S.NOT_APPLICABLE,
            currency="EUR",
            reason="SYNTHETIC test",
        )
        for c in sorted(REQUIRED_CATEGORIES - covered)
    ]
    return lines


def scenarios(
    calc: TaxCalculation | None,
    *,
    quoted: bool = False,
    threshold: ContributionThreshold = PROPOSED,
    sell: ProceedsEstimate | None = None,
    expires_at: datetime | None = None,
    rates: list[FxRate] | None = None,
) -> ScenarioSet:
    buy = PurchaseInput(
        status=S.QUOTED if quoted else S.ESTIMATED,
        amount=eur("2800.00"),
        evidence_ids=("SYNTHETIC-seller-confirmation",) if quoted else (),
    )
    sell = sell or ProceedsEstimate(
        status=S.ESTIMATED, currency="EUR", base=eur("8000.00"), basis="owner_estimate"
    )
    lines = other_lines(quoted=quoted, expires_at=expires_at) + list(tax_cost_lines(calc))
    return compute_scenarios(buy, lines, sell, rates or [MKD_RATE], threshold, as_of=AS_OF)


def screening(state: EligibilityState = EligibilityState.ELIGIBLE_PRIMARY, **kw: Any) -> ScreeningInput:
    kw.setdefault("eur_payable", eur("2800.00"))
    return ScreeningInput(eligibility=state, profile_key=ProfileKey.PRIMARY, **kw)


def comparable(**kw: Any) -> ComparableReference:
    values: dict[str, Any] = {
        "comparable_set_id": "SYNTHETIC-comparable-set-1",
        "content_hash": "a" * 64,
        "sample_size": 6,
        "quality": "adequate",
        "fresh_until": AS_OF + timedelta(days=3),
    }
    values.update(kw)
    return ComparableReference(**values)


def assemble(**kw: Any) -> Valuation:
    calc = kw.pop("tax", None) if "tax" in kw else active_calc()
    values: dict[str, Any] = {
        "listing_revision_id": REV,
        "screening": screening(),
        "comparable": comparable(),
        "tax": calc,
        "scenarios": kw.pop("scenario_set", None) or scenarios(calc),
        "fx_rates": [MKD_RATE],
        "cost_profile": PROFILE,
        "config_revision_id": CONFIG,
        "as_of": AS_OF,
    }
    values.update(kw)
    return assemble_valuation(**values)


# --------------------------------------------------------------------------- states


def test_not_started_without_scenarios() -> None:
    valuation = assemble(scenario_set=None, scenarios=None)
    assert valuation.state == ValuationState.NOT_STARTED
    assert valuation.base_contribution is None and valuation.expires_at is None
    assert not valuation.research_candidate and not valuation.can_notify


def test_rejected_listing_is_invalid_and_terminal() -> None:
    valuation = assemble(screening=screening(EligibilityState.REJECTED))
    assert valuation.state == ValuationState.INVALID
    assert valuation.base_contribution is None and valuation.conservative_contribution is None
    with pytest.raises(ValidationFailed, match="terminal"):
        mark_stale(valuation, [InvalidationReason.CONFIG], AS_OF)


def test_missing_tax_rule_is_incomplete_research_candidate() -> None:
    valuation = assemble(
        tax=None, scenario_set=scenarios(None), tax_unavailable_reason="SYNTHETIC: no ACTIVE MK rule"
    )
    assert valuation.state == ValuationState.INCOMPLETE
    assert valuation.research_candidate
    assert any("SYNTHETIC: no ACTIVE MK rule" in u for u in valuation.unknowns)
    assert sum("no applicable ACTIVE tax rule set" in u for u in valuation.unknowns) == 4
    assert valuation.base_contribution is None and not valuation.alert_eligible


def test_complete_with_active_rule_is_estimated() -> None:
    valuation = assemble()
    assert valuation.state == ValuationState.ESTIMATED
    assert not valuation.research_candidate and not valuation.is_fixture
    base = valuation.scenarios.scenario(ScenarioName.BASE)  # type: ignore[union-attr]
    assert valuation.base_contribution == base.contribution_before_business_tax
    assert valuation.base_contribution is not None
    expected = Decimal("8000.00") - Decimal("5300.00") - _imports()  # 5300 = purchase + other lines
    assert abs(valuation.base_contribution.amount - expected) < Decimal("1e-20")
    assert valuation.contribution_label == "estimated contribution before business tax"
    assert valuation.calculation_version == CALCULATION_VERSION
    assert not valuation.alert_eligible  # threshold proposed only
    assert any("CONTRIBUTION_THRESHOLD_PROPOSED" in w for w in valuation.warnings)


def _imports() -> Decimal:
    calc = active_calc()
    import_line_total = Decimal(0)
    for ln in tax_cost_lines(calc):
        if ln.base is not None:
            import_line_total += MKD_RATE.convert(ln.base, "EUR").amount
    return import_line_total


def test_quotes_and_active_rule_are_quote_supported() -> None:
    calc = active_calc()
    valuation = assemble(tax=calc, scenario_set=scenarios(calc, quoted=True))
    assert valuation.state == ValuationState.QUOTE_SUPPORTED


def test_approved_but_inactive_rule_keeps_import_costs_unsupported() -> None:
    calc = calculate(_rule(TaxRuleStatus.APPROVED), tax_inputs(), AS_OF)
    assert calc.complete and not calc.production_ready
    valuation = assemble(tax=calc, scenario_set=scenarios(calc))
    assert valuation.state == ValuationState.INCOMPLETE
    assert any("not ACTIVE" in u for u in valuation.unknowns)


def test_incomplete_tax_calculation_is_incomplete() -> None:
    calc = calculate(_rule(TaxRuleStatus.ACTIVE), tax_inputs(co2_cycle=Co2Cycle.UNKNOWN), AS_OF)
    valuation = assemble(tax=calc, scenario_set=scenarios(calc))
    assert valuation.state == ValuationState.INCOMPLETE
    assert any("import tax incomplete" in u and "missing input co2_cycle" in u for u in valuation.unknowns)


@pytest.mark.parametrize(
    "kw",
    [
        {"screening": screening(EligibilityState.NEEDS_FACTS)},
        {"screening": screening(eur_payable=None)},
    ],
)
def test_missing_listing_facts_are_incomplete(kw: dict[str, Any]) -> None:
    valuation = assemble(**kw)
    assert valuation.state == ValuationState.INCOMPLETE and valuation.research_candidate


def test_comparable_quality_rules() -> None:
    asking = ProceedsEstimate(
        status=S.ESTIMATED, currency="EUR", base=eur("9000.00"), basis="mk_asking_prices"
    )
    calc = active_calc()
    poor = assemble(
        tax=calc,
        scenario_set=scenarios(calc, sell=asking),
        comparable=comparable(quality="insufficient_comparables"),
    )
    assert poor.state == ValuationState.INCOMPLETE
    assert any("insufficient_comparables" in u for u in poor.unknowns)
    owner = assemble(comparable=None)
    assert owner.state == ValuationState.ESTIMATED
    assert any("INSUFFICIENT_COMPARABLES" in w for w in owner.warnings)
    small = assemble(comparable=comparable(quality="small", sample_size=2))
    assert any("SMALL_COMPARABLE_SAMPLE: n=2" in w for w in small.warnings)


def test_purchase_above_screened_payable_is_flagged() -> None:
    valuation = assemble(screening=screening(eur_payable=eur("2500.00")))
    assert any("PURCHASE_ABOVE_SCREENED_PAYABLE" in w for w in valuation.warnings)


# --------------------------------------------------------------------------- fixtures and alerts


def _alert_ready(calc: TaxCalculation) -> ScenarioSet:
    sell = ProceedsEstimate(
        status=S.ESTIMATED,
        currency="EUR",
        base=eur("8000.00"),
        low=eur("7800.00"),
        basis="owner_estimate",
        assumption_approved=True,
    )
    return scenarios(calc, quoted=True, threshold=APPROVED, sell=sell)


def test_alert_eligible_only_when_every_gate_passes() -> None:
    calc = active_calc()
    valuation = assemble(tax=calc, scenario_set=_alert_ready(calc))
    assert valuation.state == ValuationState.QUOTE_SUPPORTED
    assert valuation.alert_eligible and valuation.can_notify


def test_fixture_tax_rule_makes_a_fixture_valuation_that_never_notifies() -> None:
    calc = fixture_calc()
    valuation = assemble(tax=calc, scenario_set=_alert_ready(calc))
    assert valuation.is_fixture
    assert valuation.state == ValuationState.ESTIMATED  # computed, but labelled synthetic
    assert not valuation.alert_eligible and not valuation.can_notify
    assert any("FIXTURE_VALUATION" in w for w in valuation.warnings)


@pytest.mark.parametrize(
    "kw",
    [
        {"fixture_inputs": True},
        {"comparable": comparable(is_fixture=True)},
        {"screening": screening(is_fixture=True)},
        {
            "cost_profile": CostProfileRef(
                profile_key="synthetic",
                version=1,
                sha256="c" * 64,
                approval_status="unapproved",
                is_fixture=True,
            )
        },
    ],
)
def test_any_fixture_input_propagates(kw: dict[str, Any]) -> None:
    calc = active_calc()
    valuation = assemble(tax=calc, scenario_set=_alert_ready(calc), **kw)
    assert valuation.is_fixture and not valuation.can_notify


# --------------------------------------------------------------------------- fingerprint


def test_fingerprint_is_deterministic_and_order_independent() -> None:
    other = FxRate(
        base="EUR",
        quote="CHF",
        rate=Decimal("0.94"),
        rate_date=date(2026, 10, 5),
        retrieved_at=AS_OF,
        provider="ECB",
    )
    calc = active_calc()
    sset = scenarios(calc)
    a = assemble(tax=calc, scenario_set=sset, fx_rates=[MKD_RATE, other], evidence_ids=["e2", "e1", "e1"])
    b = assemble(tax=calc, scenario_set=sset, fx_rates=[other, MKD_RATE], evidence_ids=["e1", "e2"])
    assert a.dependency_fingerprint == b.dependency_fingerprint == a.dependencies.fingerprint()
    assert len(a.dependency_fingerprint) == 64
    assert a.dependencies.evidence_ids == ("e1", "e2")
    assert a.dependencies.calculation_version == CALCULATION_VERSION


def test_dependencies_capture_customs_rate_and_scenario_fx() -> None:
    customs = FxRate(
        base="EUR",
        quote="MKD",
        rate=Decimal("61.4"),
        rate_date=date(2026, 10, 1),
        retrieved_at=AS_OF,
        provider="SYNTHETIC customs authority",
        purpose=FxPurpose.CUSTOMS,
    )
    calc = calculate(
        _rule(TaxRuleStatus.ACTIVE),
        tax_inputs(customs_value=eur("1626.00"), customs_fx_rate=customs),
        AS_OF,
    )
    deps = build_dependencies(
        listing_revision_id=REV, config_revision_id=CONFIG, tax=calc, scenarios=scenarios(calc), fx_rates=[]
    )
    purposes = {(d.quote, d.purpose) for d in deps.fx}
    assert purposes == {("MKD", FxPurpose.CUSTOMS), ("MKD", FxPurpose.REFERENCE)}
    assert deps.tax_rule is not None and deps.tax_rule.status == TaxRuleStatus.ACTIVE


def _current(valuation: Valuation, **changes: Any) -> ValuationDependencies:
    return valuation.dependencies.model_copy(update=changes)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"listing_revision_id": "SYNTHETIC-listing-revision-2"}, InvalidationReason.LISTING_REVISION),
        ({"comparable_hash": "d" * 64}, InvalidationReason.COMPARABLES),
        ({"comparable_set_id": None, "comparable_hash": None}, InvalidationReason.COMPARABLES),
        ({"fx": ()}, InvalidationReason.FX),
        ({"cost_profile": PROFILE.model_copy(update={"version": 2})}, InvalidationReason.COST_PROFILE),
        ({"cost_evidence_ids": ("SYNTHETIC-new-quote",)}, InvalidationReason.COST_QUOTE),
        ({"cost_inputs_sha256": "e" * 64}, InvalidationReason.COST_QUOTE),
        ({"config_revision_id": "SYNTHETIC-config-revision-2"}, InvalidationReason.CONFIG),
        ({"evidence_ids": ("SYNTHETIC-inspection",)}, InvalidationReason.EVIDENCE),
        ({"calculation_version": "valuation/9.9.9"}, InvalidationReason.CALCULATION_VERSION),
    ],
)
def test_each_dependency_change_invalidates(change: dict[str, Any], reason: InvalidationReason) -> None:
    valuation = assemble()
    current = _current(valuation, **change)
    assert current.fingerprint() != valuation.dependency_fingerprint
    check = is_stale(valuation, current, AS_OF)
    assert check.stale and check.reasons == (reason,)


@pytest.mark.parametrize(
    "tax_change",
    [
        {"status": TaxRuleStatus.REVOKED},
        {"status": TaxRuleStatus.EXPIRED},
        {"valid_to": date(2026, 12, 1)},
        {"sha256": "f" * 64},
        {"version": "synthetic-2"},
    ],
)
def test_tax_rule_approval_or_effective_date_change_invalidates(tax_change: dict[str, Any]) -> None:
    valuation = assemble()
    assert valuation.dependencies.tax_rule is not None
    current = _current(valuation, tax_rule=valuation.dependencies.tax_rule.model_copy(update=tax_change))
    check = is_stale(valuation, current, AS_OF)
    assert check.stale and InvalidationReason.TAX_RULE in check.reasons


def test_closed_current_tax_rule_invalidates_even_if_recorded_identically() -> None:
    revoked = TaxRuleDependency.from_rule_set(
        transition_rule_set(_rule(TaxRuleStatus.ACTIVE), TaxRuleStatus.REVOKED, at=AS_OF)
    )
    deps = build_dependencies(listing_revision_id=REV, config_revision_id=CONFIG, tax=revoked)
    valuation = assemble(scenarios=None, scenario_set=None)
    check = is_stale(valuation.model_copy(update={"dependencies": deps}), deps, AS_OF)
    assert check.reasons == (InvalidationReason.TAX_RULE,)


def test_unchanged_dependencies_are_not_stale_until_deadline() -> None:
    valuation = assemble()
    assert valuation.expires_at == AS_OF + timedelta(days=3)  # comparable freshness is earliest
    fresh = is_stale(valuation, valuation.dependencies, AS_OF + timedelta(days=1))
    assert not fresh.stale and fresh.reasons == ()
    late = is_stale(valuation, valuation.dependencies, AS_OF + timedelta(days=3))
    assert late.stale and late.reasons == (InvalidationReason.FRESHNESS_DEADLINE,)


# --------------------------------------------------------------------------- expiry


@pytest.mark.parametrize(
    ("kw", "expected", "why"),
    [
        ({}, AS_OF + timedelta(days=3), "comparable freshness"),
        ({"quote_expiry": AS_OF + timedelta(days=1)}, AS_OF + timedelta(days=1), "cost quote"),
        ({"comparable": comparable(fresh_until=None)}, datetime(2026, 10, 13, tzinfo=UTC), "FX EUR/MKD"),
        (
            {"comparable": comparable(fresh_until=None), "fx_max_age_days": 400},
            datetime(2027, 1, 1, tzinfo=UTC),
            "tax rule",
        ),
    ],
)
def test_expiry_is_earliest_deadline(kw: dict[str, Any], expected: datetime, why: str) -> None:
    quote_expiry = kw.pop("quote_expiry", None)
    calc = active_calc()
    valuation = assemble(tax=calc, scenario_set=scenarios(calc, quoted=True, expires_at=quote_expiry), **kw)
    assert valuation.expires_at == expected, why


def test_already_expired_inputs_produce_a_stale_valuation() -> None:
    calc = active_calc()
    old_rate = MKD_RATE.model_copy(update={"rate_date": date(2026, 9, 1)})
    valuation = assemble(tax=calc, scenario_set=scenarios(calc, rates=[old_rate]), fx_rates=[old_rate])
    assert valuation.state == ValuationState.STALE
    assert valuation.stale_at == AS_OF
    assert valuation.stale_reason is not None and valuation.stale_reason.startswith("freshness_deadline")
    assert valuation.base_contribution is not None  # figures kept for audit
    assert not valuation.alert_eligible and not valuation.can_notify


# --------------------------------------------------------------------------- mark_stale and invariants


def test_mark_stale_moves_state_only() -> None:
    calc = active_calc()
    valuation = assemble(tax=calc, scenario_set=_alert_ready(calc))
    assert valuation.can_notify
    stale = mark_stale(
        valuation, [InvalidationReason.FX, InvalidationReason.CONFIG], AS_OF + timedelta(hours=1)
    )
    assert stale.state == ValuationState.STALE and stale.stale_reason == "fx, config"
    assert stale.dependency_fingerprint == valuation.dependency_fingerprint
    assert stale.base_contribution == valuation.base_contribution
    assert not stale.can_notify and not stale.alert_eligible
    assert mark_stale(stale, [InvalidationReason.EVIDENCE], AS_OF) is stale
    with pytest.raises(ValidationFailed, match="at least one reason"):
        mark_stale(valuation, [], AS_OF)
    assert is_stale(stale, stale.dependencies, AS_OF).stale


def test_valuation_invariants() -> None:
    valuation = assemble(tax=None, scenario_set=scenarios(None))
    data = valuation.model_dump()
    with pytest.raises(ValidationError, match="no contribution figures"):
        Valuation.model_validate({**data, "base_contribution": eur("1")})
    with pytest.raises(ValidationError, match="fingerprint"):
        Valuation.model_validate({**data, "dependency_fingerprint": "0" * 64})
    with pytest.raises(ValidationError, match="stale"):
        Valuation.model_validate({**data, "state": ValuationState.STALE})
    estimated = assemble().model_dump()
    with pytest.raises(ValidationError, match="needs base and conservative"):
        Valuation.model_validate({**estimated, "base_contribution": None})
    fixture = assemble(tax=fixture_calc(), scenario_set=scenarios(fixture_calc())).model_dump()
    with pytest.raises(ValidationError, match="alert eligible"):
        Valuation.model_validate({**fixture, "alert_eligible": True})


def test_dependency_and_screening_models() -> None:
    with pytest.raises(ValidationError, match="sorted"):
        ValuationDependencies(listing_revision_id=REV, config_revision_id=CONFIG, evidence_ids=("b", "a"))
    with pytest.raises(ValidationError, match="EUR"):
        ScreeningInput(eligibility=EligibilityState.ELIGIBLE_PRIMARY, eur_payable=Money.of("2800", "CHF"))
    with pytest.raises(ValidationError):
        ComparableReference(comparable_set_id="x", content_hash="nothex", sample_size=1, quality="adequate")
