"""Scenario economics tests (spec sections 17, 18 and 31 "Economics" row).

All amounts are SYNTHETIC test values. They are not current costs, legal rates or quotes.
"""

from __future__ import annotations

import decimal
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from suv_deals.domain.costs import (
    CONTRIBUTION_LABEL,
    DISCOUNT_UNKNOWN_LABEL,
    MATERIAL_CATEGORIES,
    REQUIRED_CATEGORIES,
    TERM_BY_CATEGORY,
    CostLine,
    CostProfile,
    CostScope,
    ProceedsEstimate,
    PurchaseInput,
    ScenarioSet,
    Term,
    compute_scenarios,
    load_cost_profile,
    proceeds_supported,
    tax_calculation_evidence_id,
    tax_cost_lines,
)
from suv_deals.domain.enums import (
    Co2Cycle,
    FxPurpose,
    ScenarioName,
    TaxRuleStatus,
)
from suv_deals.domain.enums import (
    CostCategory as C,
)
from suv_deals.domain.enums import (
    CostLineStatus as S,
)
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
    TaxInputs,
    calculate,
    compute_rule_set_sha256,
    load_rule_set_file,
    transition_rule_set,
)
from suv_deals.errors import ValidationFailed

REPO = Path(__file__).resolve().parents[2]
AS_OF = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
CTX40 = decimal.Context(prec=40)  # the precision domain arithmetic uses before display rounding
PROPOSED = ContributionThreshold()  # EUR 1,500.00, unapproved
APPROVED = ContributionThreshold(
    approval_status="approved", approved_by="SYNTHETIC owner", approved_at="2026-10-01"
)
CONS, BASE, UP = ScenarioName.CONSERVATIVE, ScenarioName.BASE, ScenarioName.UPSIDE

# Spec section 18 "Synthetic arithmetic test" -- arithmetic only, not real costs.
SPEC_FIXTURE: dict[str, Any] = json.loads(
    """{
  "fixture": true,
  "currency": "EUR",
  "expected_realized_proceeds": "8000.00",
  "purchase": "2800.00",
  "transport": "700.00",
  "import_components_test_fixture": "1400.00",
  "clearance_and_documents": "250.00",
  "repairs_and_preparation": "800.00",
  "risk_reserve": "600.00",
  "selling_costs": "150.00",
  "total_modelled_cost": "6700.00",
  "contribution_before_business_tax": "1300.00",
  "would_meet_proposed_1500_threshold": false
}"""
)


def eur(value: str) -> Money:
    return Money.of(value, "EUR")


def line(category: C, base: str | None = None, *, status: S = S.ESTIMATED, **kw: Any) -> CostLine:
    values: dict[str, Any] = {
        "category": category,
        "label": kw.pop("label", f"SYNTHETIC {category.value}"),
        "status": status,
        "currency": kw.pop("currency", "EUR"),
    }
    if base is not None:
        values["base"] = Money.of(base, values["currency"])
    for bound in ("low", "high"):
        if bound in kw and isinstance(kw[bound], str):
            kw[bound] = Money.of(kw[bound], values["currency"])
    if status in (S.QUOTED, S.ACTUAL):
        kw.setdefault("evidence_ids", (f"SYNTHETIC-evidence-{category.value}",))
    if status == S.QUOTED:
        kw.setdefault("provider", "SYNTHETIC provider")
    if category == C.REFUNDABLE_DEPOSIT and status in (S.QUOTED, S.ESTIMATED, S.ACTUAL):
        kw.setdefault("refundable", True)
    values.update(kw)
    return CostLine(**values)


def na(category: C) -> CostLine:
    return CostLine(
        category=category,
        label=f"SYNTHETIC {category.value}",
        status=S.NOT_APPLICABLE,
        currency="EUR",
        reason="SYNTHETIC fixture: category not part of this test",
    )


def fill(lines: list[CostLine]) -> list[CostLine]:
    """Add not_applicable lines (with a reason) for every required category not covered."""
    present = {item.category for item in lines}
    return lines + [na(c) for c in sorted(REQUIRED_CATEGORIES - present)]


def spec_lines() -> list[CostLine]:
    return fill(
        [
            line(C.TRANSPORT, SPEC_FIXTURE["transport"]),
            line(C.IMPORT_DUTY, SPEC_FIXTURE["import_components_test_fixture"]),
            line(C.CUSTOMS_BROKER, SPEC_FIXTURE["clearance_and_documents"]),
            line(C.REPAIRS, SPEC_FIXTURE["repairs_and_preparation"]),
            line(C.RISK_RESERVE, SPEC_FIXTURE["risk_reserve"]),
            line(C.SELLING_COSTS, SPEC_FIXTURE["selling_costs"]),
        ]
    )


def purchase(amount: str = "2800.00", status: S = S.ESTIMATED, **kw: Any) -> PurchaseInput:
    if status in (S.QUOTED, S.ACTUAL):
        kw.setdefault("evidence_ids", ("SYNTHETIC-seller-confirmation",))
    return PurchaseInput(status=status, amount=None if status == S.UNKNOWN else eur(amount), **kw)


def proceeds(base: str = "8000.00", **kw: Any) -> ProceedsEstimate:
    kw.setdefault("basis", "owner_estimate")
    for bound in ("low", "high"):
        if bound in kw and isinstance(kw[bound], str):
            kw[bound] = eur(kw[bound])
    return ProceedsEstimate(status=kw.pop("status", S.ESTIMATED), currency="EUR", base=eur(base), **kw)


def run(
    lines: list[CostLine] | None = None,
    *,
    buy: PurchaseInput | None = None,
    sell: ProceedsEstimate | None = None,
    rates: list[FxRate] | None = None,
    threshold: ContributionThreshold = PROPOSED,
    **kw: Any,
) -> ScenarioSet:
    return compute_scenarios(
        buy or purchase(),
        spec_lines() if lines is None else lines,
        sell or proceeds(),
        rates or [],
        threshold,
        as_of=kw.pop("as_of", AS_OF),
        **kw,
    )


def ecb(quote: str, rate: str, *, days_old: int = 1, purpose: FxPurpose = FxPurpose.REFERENCE) -> FxRate:
    return FxRate(
        base="EUR",
        quote=quote,
        rate=Decimal(rate),
        rate_date=AS_OF.date() - timedelta(days=days_old),
        retrieved_at=AS_OF - timedelta(days=days_old),
        provider="SYNTHETIC-ECB-format",
        purpose=purpose,
    )


# --------------------------------------------------------------------------- spec fixture


def test_spec_synthetic_arithmetic_fixture_exact() -> None:
    assert SPEC_FIXTURE["fixture"] is True
    result = run(
        sell=proceeds(SPEC_FIXTURE["expected_realized_proceeds"]), buy=purchase(SPEC_FIXTURE["purchase"])
    )
    base = result.scenario(BASE)
    assert base.total_modelled_cost == eur(SPEC_FIXTURE["total_modelled_cost"])
    assert base.contribution_before_business_tax == eur(SPEC_FIXTURE["contribution_before_business_tax"])
    assert result.threshold.would_meet is SPEC_FIXTURE["would_meet_proposed_1500_threshold"]
    assert base.landed_cost == eur("5150.00")  # 2800 + 700 + 1400 + 250
    assert base.ready_to_sell_cost == eur("6550.00")  # + 800 + 600
    assert base.import_components == eur("1400.00")
    assert base.complete and result.complete
    assert result.threshold.proposed_only
    assert result.threshold.threshold == eur("1500.00")
    assert not result.threshold.alert_eligible
    assert any("PROPOSED/unapproved" in b for b in result.threshold.blockers)
    # point estimates: every scenario equals base
    for name in (CONS, UP):
        assert result.scenario(name).contribution_before_business_tax == eur("1300.00")


def test_terminology_never_says_net_profit() -> None:
    result = run()
    assert result.contribution_label == CONTRIBUTION_LABEL == "estimated contribution before business tax"
    dumped = result.model_dump_json().lower()
    assert "net profit" not in dumped and "net_profit" not in dumped


# --------------------------------------------------------------------------- unknown is never zero


def test_unknown_line_makes_totals_unknown_and_keeps_known_subtotal() -> None:
    lines = [ln if ln.category != C.TRANSPORT else line(C.TRANSPORT, status=S.UNKNOWN) for ln in spec_lines()]
    result = run(lines)
    base = result.scenario(BASE)
    for name in ("transport", "landed_cost", "ready_to_sell_cost", "total_modelled_cost", "cash_required"):
        assert getattr(base, name) is None, name
    assert base.contribution_before_business_tax is None
    assert base.known_subtotal == eur("6000.00")  # 6700 - 700, labelled as a subtotal
    assert [u.item for u in result.unknown_lines] == ["transport"]
    assert not result.complete and result.material_support == "incomplete"
    assert result.threshold.would_meet is None
    assert base.repairs == eur("800.00")  # unaffected terms stay known


def test_missing_required_category_is_unknown_not_zero() -> None:
    lines = [ln for ln in spec_lines() if ln.category != C.STORAGE_HOLDING]
    result = run(lines)
    assert result.scenario(BASE).preparation is None
    assert result.scenario(BASE).total_modelled_cost is None
    assert any(u.item == "storage_holding" and "no line supplied" in u.reason for u in result.unknown_lines)


def test_not_applicable_requires_reason_and_contributes_nothing() -> None:
    with pytest.raises(ValidationError, match="reason"):
        CostLine(category=C.STORAGE_HOLDING, label="x", status=S.NOT_APPLICABLE, currency="EUR")
    result = run()
    assert result.scenario(BASE).acquisition_costs == eur("0")


def test_unknown_purchase_or_proceeds() -> None:
    result = run(buy=purchase(status=S.UNKNOWN))
    assert result.scenario(BASE).purchase_economic_cost is None
    assert result.scenario(BASE).contribution_before_business_tax is None
    assert any(u.item == "purchase" for u in result.unknown_lines)
    unknown_sale = ProceedsEstimate(status=S.UNKNOWN, currency="EUR", basis="mk_asking_prices")
    result = run(sell=unknown_sale)
    base = result.scenario(BASE)
    assert base.expected_realized_proceeds is None and base.contribution_before_business_tax is None
    assert base.total_modelled_cost == eur("6700.00")  # costs are still fully known


# --------------------------------------------------------------------------- line model rules


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"status": S.UNKNOWN, "base": eur("1")}, "no amounts"),
        ({"status": S.QUOTED, "base": eur("1"), "provider": "p"}, "evidence_ids"),
        ({"status": S.QUOTED, "base": eur("1"), "evidence_ids": ("e",)}, "provider"),
        ({"status": S.ACTUAL, "base": eur("1")}, "evidence_ids"),
        ({"status": S.ESTIMATED}, "base amount"),
        ({"status": S.ESTIMATED, "base": eur("5"), "low": eur("6")}, "low <= base"),
        ({"status": S.ESTIMATED, "base": eur("5"), "high": eur("4")}, "low <= base"),
        ({"status": S.ESTIMATED, "base": Money.of("5", "CHF")}, "currency"),
        ({"status": S.ESTIMATED, "base": eur("-5")}, "negative"),
        ({"status": S.ESTIMATED, "base": eur("5"), "refundable": True}, "refundable"),
        ({"status": S.ESTIMATED, "base": eur("5"), "rule_supported": True}, "rule_supported"),
        ({"status": S.ESTIMATED, "base": eur("5"), "expires_at": datetime(2026, 1, 1)}, "naive"),
    ],
)
def test_cost_line_validation(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        CostLine(category=C.TRANSPORT, label="SYNTHETIC", currency="EUR", **kwargs)


def test_cost_line_rejects_purchase_floats_and_unflagged_deposits() -> None:
    with pytest.raises(ValidationError, match="PurchaseInput"):
        CostLine(category=C.PURCHASE, label="x", status=S.UNKNOWN, currency="EUR")
    with pytest.raises(ValidationError):
        CostLine(category=C.TRANSPORT, label="x", status=S.ESTIMATED, currency="EUR", base=700.0)
    with pytest.raises(ValidationError, match="refundable"):
        CostLine(category=C.REFUNDABLE_DEPOSIT, label="x", status=S.ESTIMATED, currency="EUR", base=eur("1"))
    with pytest.raises(ValidationError, match="confirmed refund"):
        line(C.REFUNDABLE_DEPOSIT, "100", refund_confirmed=True)


def test_purchase_and_proceeds_validation() -> None:
    with pytest.raises(ValidationError, match="not_applicable"):
        PurchaseInput(status=S.NOT_APPLICABLE)
    with pytest.raises(ValidationError, match="unknown purchase"):
        PurchaseInput(status=S.UNKNOWN, amount=eur("1"))
    with pytest.raises(ValidationError, match="within the amount"):
        PurchaseInput(status=S.ESTIMATED, amount=eur("100"), included_refundable_deposit=eur("101"))
    with pytest.raises(ValidationError, match="evidence"):
        PurchaseInput(status=S.QUOTED, amount=eur("100"))
    with pytest.raises(ValidationError, match="asking-price"):
        proceeds(negotiation_discount_pct=Decimal("5"))
    with pytest.raises(ValidationError):
        proceeds(basis="mk_asking_prices", negotiation_discount_pct=Decimal("100"))
    with pytest.raises(ValidationError, match="unknown proceeds"):
        ProceedsEstimate(status=S.UNKNOWN, currency="EUR", basis="owner_estimate", base=eur("1"))


# --------------------------------------------------------------------------- quotes, scope, reserve


def test_expired_quote_is_treated_as_estimate() -> None:
    quote = line(C.TRANSPORT, "700.00", status=S.QUOTED, expires_at=AS_OF - timedelta(minutes=1))
    lines = [quote if ln.category == C.TRANSPORT else ln for ln in spec_lines()]
    result = run(lines)
    assert any(w.startswith("QUOTE_EXPIRED:transport") for w in result.warnings)
    assert result.scenario(BASE).transport == eur("700.00")  # still used, as an estimate
    assert any(u.startswith("transport") for u in result.unsupported_material)
    assert result.earliest_expiry == AS_OF - timedelta(minutes=1)


def test_quote_scope_mismatch_is_not_applied() -> None:
    scope = CostScope(origin_city="SYNTHETIC-Munich", vehicle_running="running")
    quote = line(C.TRANSPORT, "700.00", status=S.QUOTED, scope=scope)
    lines = [quote if ln.category == C.TRANSPORT else ln for ln in spec_lines()]
    other = CostScope(origin_city="SYNTHETIC-Milan", vehicle_running="non_running")
    result = run(lines, target_scope=other)
    assert result.scenario(BASE).transport is None
    assert any("scope mismatch" in u.reason for u in result.unknown_lines)
    assert any("COST_SCOPE_MISMATCH" in w for w in result.warnings)
    same = run(lines, target_scope=CostScope(origin_city="synthetic-munich"))
    assert same.scenario(BASE).transport == eur("700.00")
    assert scope.conflicts_with(other) == ["origin_city", "vehicle_running"]


def test_scenarios_use_bounds_and_reserve() -> None:
    lines = fill(
        [
            line(C.TRANSPORT, "700.00", low="650.00", high="900.00"),
            line(C.IMPORT_DUTY, "1400.00"),
            line(C.CUSTOMS_BROKER, "250.00"),
            line(C.REPAIRS, "800.00", low="500.00", high="1500.00"),
            line(C.RISK_RESERVE, "600.00", low="300.00", high="1000.00"),
            line(C.SELLING_COSTS, "150.00"),
        ]
    )
    result = run(lines, sell=proceeds("8000.00", low="7200.00", high="8800.00"))
    cons, base, up = (result.scenario(n) for n in (CONS, BASE, UP))
    assert cons.reserves == eur("1000.00") and base.reserves == eur("600.00") and up.reserves == eur("300.00")
    assert cons.total_modelled_cost == eur("8000.00")  # 2800+900+1400+250+1500+1000+150 (no extrapolation)
    assert up.total_modelled_cost == eur("6050.00")  # 2800+650+1400+250+500+300+150
    assert cons.contribution_before_business_tax == eur("-800.00")  # 7200 - 8000
    assert base.contribution_before_business_tax == eur("1300.00")
    assert up.contribution_before_business_tax == eur("2750.00")  # 8800 - 6050
    assert result.threshold.would_meet_by_scenario == {CONS: False, BASE: False, UP: True}


def test_correlated_lines_do_not_stack_worst_cases() -> None:
    lines = fill(
        [
            line(C.TRANSPORT, "700.00"),
            line(C.IMPORT_DUTY, "1400.00"),
            line(C.CUSTOMS_BROKER, "250.00"),
            line(C.REPAIRS, "800.00", low="500.00", high="1500.00", correlation_group="mechanical"),
            line(C.RISK_RESERVE, "600.00", low="400.00", high="1000.00", correlation_group="mechanical"),
            line(C.SELLING_COSTS, "150.00"),
        ]
    )
    result = run(lines)
    cons, up = result.scenario(CONS), result.scenario(UP)
    assert cons.repairs == eur("1500.00") and cons.reserves == eur("600.00")  # +700 once, not +700 +400
    assert up.repairs == eur("500.00") and up.reserves == eur("600.00")  # -300 once, not -300 -200
    assert any("mechanical" in n and "largest overrun" in n for n in result.correlation_notes)


# --------------------------------------------------------------------------- deposits and cash


def test_included_deposit_split_out_and_counted_once() -> None:
    buy = purchase(
        "3300.00",
        included_refundable_deposit=eur("500.00"),
        deposit_refund_prerequisites=("SYNTHETIC export proof within 30 days",),
    )
    lines = [ln for ln in spec_lines() if ln.category != C.REFUNDABLE_DEPOSIT]
    result = run(lines, buy=buy)
    base, cons = result.scenario(BASE), result.scenario(CONS)
    assert base.purchase_cash_outlay_excluding_deposits == eur("2800.00")
    assert base.refundable_deposits == eur("500.00")
    assert base.cash_required == eur("7200.00")  # 2800 + 3900 + 500, deposit once
    assert base.purchase_economic_cost == eur("2800.00")  # refund assumed (labelled)
    assert base.contribution_before_business_tax == eur("1300.00")
    assert cons.deposits_assumed_not_refunded == eur("500.00")
    assert cons.purchase_economic_cost == eur("3300.00")  # no-refund downside
    assert cons.contribution_before_business_tax == eur("800.00")
    assert cons.cash_required == eur("7200.00")
    assert any("ASSUMED" in a and "SYNTHETIC export proof" in a for a in result.assumptions)
    assert any("no-refund downside" in a for a in cons.assumptions)


def test_confirmed_refund_has_no_downside() -> None:
    deposit = line(C.REFUNDABLE_DEPOSIT, "500.00", refund_confirmed=True, evidence_ids=("SYNTHETIC-refund",))
    lines = [deposit if ln.category == C.REFUNDABLE_DEPOSIT else ln for ln in spec_lines()]
    result = run(lines)
    assert result.scenario(CONS).deposits_assumed_not_refunded == eur("0")
    assert result.scenario(CONS).contribution_before_business_tax == eur("1300.00")
    assert result.scenario(BASE).cash_required == eur("7200.00")
    assert not any("ASSUMED" in a for a in result.assumptions)


def test_unknown_deposit_blocks_cash_and_economics() -> None:
    deposit = line(C.REFUNDABLE_DEPOSIT, status=S.UNKNOWN, reason="seller terms not yet confirmed")
    lines = [deposit if ln.category == C.REFUNDABLE_DEPOSIT else ln for ln in spec_lines()]
    result = run(lines)
    base, cons = result.scenario(BASE), result.scenario(CONS)
    assert base.cash_required is None and base.refundable_deposits is None
    # a refund of an unknown amount cannot be assumed: unknown in every scenario
    assert base.purchase_economic_cost is None and base.contribution_before_business_tax is None
    assert cons.purchase_economic_cost is None and cons.contribution_before_business_tax is None
    assert base.known_subtotal == eur("6700.00")
    assert not any("ASSUMED" in a for a in result.assumptions)  # nothing to assume about an unknown amount


def test_cash_before_sale_flag() -> None:
    selling = line(C.SELLING_COSTS, "150.00", cash_before_sale=False)
    lines = [selling if ln.category == C.SELLING_COSTS else ln for ln in spec_lines()]
    result = run(lines)
    assert result.scenario(BASE).cash_required == eur("6550.00")
    assert result.scenario(BASE).total_modelled_cost == eur("6700.00")
    unknown_after_sale = line(C.SELLING_COSTS, status=S.UNKNOWN, cash_before_sale=False)
    lines = [unknown_after_sale if ln.category == C.SELLING_COSTS else ln for ln in spec_lines()]
    result = run(lines)
    assert result.scenario(BASE).cash_required == eur("6550.00")
    assert result.scenario(BASE).total_modelled_cost is None


def test_no_double_counting_each_category_feeds_one_term() -> None:
    weights = {c: Decimal(2) ** i for i, c in enumerate(sorted(REQUIRED_CATEGORIES - {C.REFUNDABLE_DEPOSIT}))}
    lines = [line(c, str(w)) for c, w in weights.items()]
    lines.append(line(C.REFUNDABLE_DEPOSIT, "0.5", refund_confirmed=True, evidence_ids=("e",)))
    result = run(lines, buy=purchase("0.25"))
    base = result.scenario(BASE)
    assert base.total_modelled_cost is not None
    assert base.total_modelled_cost.amount == Decimal("0.25") + sum(weights.values())
    assert base.cash_required is not None
    assert base.cash_required.amount == Decimal("0.25") + sum(weights.values()) + Decimal("0.5")
    terms = {term: getattr(base, term.value) for term in Term if term != Term.REFUNDABLE_DEPOSITS}
    for term, money in terms.items():
        expected = sum((w for c, w in weights.items() if TERM_BY_CATEGORY[c] == term), Decimal(0))
        assert money is not None and money.amount == expected, term
    assert set(TERM_BY_CATEGORY) == REQUIRED_CATEGORIES


# --------------------------------------------------------------------------- Decimal and FX


def test_threshold_compares_unrounded_values() -> None:
    lines = [ln for ln in spec_lines() if ln.category != C.TRANSPORT] + [line(C.TRANSPORT, "200.00")]
    result = run(lines, buy=purchase("2800.005"))
    base = result.scenario(BASE)
    assert base.contribution_before_business_tax == eur("1799.995")
    edge = run(lines, buy=purchase("3100.005"))
    contribution = edge.scenario(BASE).contribution_before_business_tax
    assert contribution is not None and contribution.amount == Decimal("1499.995")
    assert contribution.display() == "1,500.00 EUR"  # display rounds ...
    assert edge.threshold.would_meet is False  # ... the comparison never does
    exact = run(lines, buy=purchase("3100.00"))
    assert exact.threshold.would_meet is True  # >= threshold


def test_chf_line_converted_by_division() -> None:
    lines = [ln for ln in spec_lines() if ln.category != C.TRANSPORT]
    lines.append(line(C.TRANSPORT, "658.00", currency="CHF"))
    result = run(lines, rates=[ecb("CHF", "0.9400")])
    assert result.scenario(BASE).transport == eur("700")  # 658 / 0.94, not 658 * 0.94
    assert result.fx_rates_used == (ecb("CHF", "0.9400"),)
    assert result.scenario(BASE).contribution_before_business_tax == eur("1300.00")


def test_missing_stale_and_customs_rates() -> None:
    lines = [ln for ln in spec_lines() if ln.category != C.TRANSPORT]
    lines.append(line(C.TRANSPORT, "658.00", currency="CHF"))
    missing = run(lines)
    assert missing.scenario(BASE).transport is None
    assert any(w.startswith("FX_RATE_MISSING:CHF->EUR") for w in missing.warnings)
    customs_only = run(lines, rates=[ecb("CHF", "0.9400", purpose=FxPurpose.CUSTOMS)])
    assert customs_only.scenario(BASE).transport is None
    stale = run(lines, rates=[ecb("CHF", "0.9400", days_old=9)], fx_max_age_days=7)
    assert stale.scenario(BASE).transport == eur("700")
    assert any(w.startswith("FX_RATE_STALE:EUR/CHF") for w in stale.warnings)
    future = run(lines, rates=[ecb("CHF", "0.9400", days_old=-1)])
    assert future.scenario(BASE).transport is None


def test_mkd_line_without_owner_approved_rate_is_unknown() -> None:
    lines = [ln for ln in spec_lines() if ln.category != C.IMPORT_DUTY]
    lines.append(line(C.IMPORT_DUTY, "86100.00", currency="MKD"))
    result = run(lines, rates=[ecb("CHF", "0.94")])
    assert result.scenario(BASE).import_components is None
    assert any("owner-approved source" in w for w in result.warnings)


def test_payment_rate_preferred_on_same_day() -> None:
    lines = [ln for ln in spec_lines() if ln.category != C.TRANSPORT]
    lines.append(line(C.TRANSPORT, "658.00", currency="CHF"))
    payment = ecb("CHF", "0.9200", purpose=FxPurpose.PAYMENT)
    result = run(lines, rates=[ecb("CHF", "0.9400"), payment])
    assert result.scenario(BASE).transport == Money(
        amount=CTX40.divide(Decimal(658), Decimal("0.92")), currency="EUR"
    )


# --------------------------------------------------------------------------- proceeds


def test_discount_unknown_is_labelled_asking_based() -> None:
    sell = proceeds("9000.00", basis="mk_asking_prices", sample_size=4)
    result = run(sell=sell)
    assert result.proceeds_label == DISCOUNT_UNKNOWN_LABEL == "asking-based; discount unknown"
    assert result.scenario(BASE).expected_realized_proceeds == eur("9000.00")
    assert any(DISCOUNT_UNKNOWN_LABEL in a for a in result.assumptions)
    assert not proceeds_supported(sell)


def test_known_discount_is_applied_to_every_bound() -> None:
    sell = proceeds(
        "9000.00",
        low="8000.00",
        high="10000.00",
        basis="mk_asking_prices",
        negotiation_discount_pct=Decimal("10"),
    )
    result = run(sell=sell)
    assert result.scenario(CONS).expected_realized_proceeds == eur("7200.000")
    assert result.scenario(BASE).expected_realized_proceeds == eur("8100.000")
    assert result.scenario(UP).expected_realized_proceeds == eur("9000.000")
    assert "unapproved assumption" in result.proceeds_label


def test_missing_proceeds_bounds_fall_back_with_warning() -> None:
    result = run(sell=proceeds("8000.00"))
    assert any("PROCEEDS_BOUND_MISSING:conservative" in w for w in result.warnings)


# --------------------------------------------------------------------------- support and threshold


def _quoted_lines() -> list[CostLine]:
    return fill(
        [
            line(C.TRANSPORT, "700.00", status=S.QUOTED),
            line(C.IMPORT_DUTY, "1400.00", status=S.ESTIMATED, rule_supported=True, evidence_ids=("tax",)),
            line(C.CUSTOMS_BROKER, "250.00", status=S.QUOTED),
            line(C.REPAIRS, "300.00", status=S.QUOTED),
            line(C.RISK_RESERVE, "300.00", assumption_approved=True),
            line(C.SELLING_COSTS, "150.00", assumption_approved=True),
        ]
    )


def test_material_support_levels() -> None:
    quoted = run(_quoted_lines(), buy=purchase(status=S.QUOTED))
    assert quoted.material_support == "quote_supported"
    assert quoted.unsupported_material == (
        "proceeds: owner estimate (realized-proceeds basis) (not an approved/realized basis)",
    )
    approved = [
        ln if ln.category != C.REPAIRS else line(C.REPAIRS, "300.00", assumption_approved=True)
        for ln in _quoted_lines()
    ]
    assert run(approved, buy=purchase(status=S.QUOTED)).material_support == "approved_assumptions"
    estimated = run(_quoted_lines())  # purchase is only the advertised price
    assert estimated.material_support == "estimated"
    assert run().material_support == "estimated"
    assert {C.TRANSPORT, C.IMPORT_VAT, C.REPAIRS} <= MATERIAL_CATEGORIES


def test_alert_eligibility_requires_approved_threshold_support_and_conservative_margin() -> None:
    sell = proceeds("8000.00", low="7800.00", assumption_approved=True)
    buy = purchase(status=S.QUOTED)
    eligible = run(_quoted_lines(), buy=buy, sell=sell, threshold=APPROVED)
    assert eligible.scenario(CONS).contribution_before_business_tax == eur("1900.00")  # 7800 - 5900
    assert eligible.threshold.alert_eligible and eligible.threshold.blockers == ()
    unapproved = run(_quoted_lines(), buy=buy, sell=sell, threshold=PROPOSED)
    assert not unapproved.threshold.alert_eligible
    weak = run(
        _quoted_lines(),
        buy=buy,
        sell=proceeds("8000.00", low="4000.00", assumption_approved=True),
        threshold=APPROVED,
    )
    assert weak.threshold.would_meet is True and not weak.threshold.alert_eligible
    assert any("conservative" in b for b in weak.threshold.blockers)
    asking = proceeds("8000.00", low="7800.00", basis="mk_asking_prices", assumption_approved=True)
    assert not run(_quoted_lines(), buy=buy, sell=asking, threshold=APPROVED).threshold.alert_eligible
    with pytest.raises(ValidationError, match="approved_by"):
        ContributionThreshold(approval_status="approved")


def test_threshold_only_defined_in_eur() -> None:
    lines = [
        line(c, "1", currency="CHF") if c != C.REFUNDABLE_DEPOSIT else na(c)
        for c in sorted(REQUIRED_CATEGORIES)
    ]
    sell = ProceedsEstimate(
        status=S.ESTIMATED, currency="CHF", base=Money.of("9000", "CHF"), basis="owner_estimate"
    )
    buy = PurchaseInput(status=S.ESTIMATED, amount=Money.of("2800", "CHF"))
    result = compute_scenarios(buy, lines, sell, [], PROPOSED, "CHF", as_of=AS_OF)
    assert result.complete and result.threshold.would_meet is None
    assert any("THRESHOLD_CURRENCY" in w for w in result.warnings)


def test_negative_fx_age_rejected_and_inputs_hash_is_order_independent() -> None:
    with pytest.raises(ValidationFailed):
        run(fx_max_age_days=-1)
    lines = spec_lines()
    assert run(lines).inputs_sha256 == run(list(reversed(lines))).inputs_sha256
    changed = [ln if ln.category != C.TRANSPORT else line(C.TRANSPORT, "701.00") for ln in lines]
    assert run(changed).inputs_sha256 != run(lines).inputs_sha256


# --------------------------------------------------------------------------- tax engine lines


def _tax_inputs() -> TaxInputs:
    return TaxInputs(
        declaration_date=date(2026, 6, 1),
        classification=Classification(
            tariff_code="8703 23",
            evidence_ids=("e",),
            approval_status="approved",
            approved_by="SYNTHETIC owner",
        ),
        origin_proof=OriginProof(proof_type="none", acceptance_status=OriginProofStatus.NOT_AVAILABLE),
        customs_value=Money.of("100000.00", "MKD"),
        customs_value_basis="SYNTHETIC",
        co2_g_km=Decimal("120"),
        co2_cycle="wltp",
        vehicle_age_years=Decimal("12"),
        engine_displacement_cm3=Decimal("1995"),
    )


def test_tax_cost_lines_without_rule_are_unknown() -> None:
    lines = tax_cost_lines(None)
    assert {ln.category for ln in lines} == {
        C.IMPORT_DUTY,
        C.MOTOR_VEHICLE_TAX,
        C.IMPORT_VAT,
        C.OTHER_IMPORT_CHARGES,
    }
    assert all(ln.status == S.UNKNOWN and ln.base is None for ln in lines)
    assert all("no applicable ACTIVE tax rule set" in (ln.reason or "") for ln in lines)


def test_tax_cost_lines_from_fixture_and_incomplete_calculations() -> None:
    rule = load_rule_set_file(REPO / "tests" / "fixtures" / "tax" / "synthetic_rule_set.json")
    calc = calculate(rule, _tax_inputs(), datetime(2026, 6, 1, tzinfo=UTC))
    lines = {ln.category: ln for ln in tax_cost_lines(calc)}
    assert lines[C.IMPORT_DUTY].base == Money.of("10000.00", "MKD")
    assert lines[C.IMPORT_VAT].base == Money.of("22200.00", "MKD")
    assert all(not ln.rule_supported and not ln.assumption_approved for ln in lines.values())
    assert all(ln.evidence_ids[0].startswith("tax_rule_set:SYNTHETIC") for ln in lines.values())
    at = datetime(2026, 6, 1, tzinfo=UTC)
    # Unknown optional input: only the affected category is unknown.
    partial = calculate(rule, _tax_inputs().model_copy(update={"engine_displacement_cm3": None}), at)
    statuses = {ln.category: ln.status for ln in tax_cost_lines(partial)}
    assert statuses[C.OTHER_IMPORT_CHARGES] == S.UNKNOWN
    assert statuses[C.IMPORT_DUTY] == S.ESTIMATED and statuses[C.IMPORT_VAT] == S.ESTIMATED
    # Missing required input: the rule cannot be trusted at all, every category is unknown.
    incomplete = calculate(rule, _tax_inputs().model_copy(update={"co2_cycle": Co2Cycle.UNKNOWN}), at)
    lines_incomplete = tax_cost_lines(incomplete)
    assert all(ln.status == S.UNKNOWN for ln in lines_incomplete)
    assert all("missing:co2_cycle" in (ln.reason or "") for ln in lines_incomplete)
    scenario = run(
        [
            ln
            for ln in spec_lines()
            if ln.category
            not in MATERIAL_CATEGORIES
            - {C.TRANSPORT, C.CUSTOMS_BROKER, C.REPAIRS, C.HOMOLOGATION_REGISTRATION}
        ]
        + list(lines_incomplete)
    )
    assert scenario.scenario(BASE).import_components is None


def _active_rule() -> RuleSet:
    """SYNTHETIC non-fixture rule driven through the real lifecycle (details: test_tax_engine)."""
    rule = load_rule_set_file(REPO / "tests" / "fixtures" / "tax" / "synthetic_rule_set.json")
    source = RuleSource(
        url="https://example.invalid/SYNTHETIC", title="SYNTHETIC", retrieved_at=AS_OF, sha256="0" * 64
    )
    draft = rule.model_copy(update={"is_fixture": False, "status": TaxRuleStatus.DRAFT, "sources": (source,)})
    draft = RuleSet.model_validate({**draft.model_dump(), "sha256": None})
    review = transition_rule_set(draft, TaxRuleStatus.UNDER_REVIEW, at=AS_OF)
    record = ReviewRecord(
        reviewer="SYNTHETIC",
        reviewed_at=AS_OF,
        content_sha256=compute_rule_set_sha256(review),
        scope="SYNTHETIC",
    )
    approved = transition_rule_set(
        review, TaxRuleStatus.APPROVED, at=AS_OF, approved_by="SYNTHETIC owner", review_record=record
    )
    return transition_rule_set(approved, TaxRuleStatus.ACTIVE, at=AS_OF)


def test_tax_cost_lines_from_active_rule_are_rule_supported() -> None:
    calc = calculate(_active_rule(), _tax_inputs(), datetime(2026, 6, 1, tzinfo=UTC))
    assert calc.production_ready
    tax_lines = tax_cost_lines(calc)
    assert all(ln.rule_supported and ln.assumption_approved for ln in tax_lines)
    eur_rates = [
        FxRate(
            base="EUR",
            quote="MKD",
            rate=Decimal("61.5"),
            rate_date=AS_OF.date(),
            retrieved_at=AS_OF,
            provider="SYNTHETIC owner-approved MKD source",
        )
    ]
    others = [
        ln
        for ln in _quoted_lines()
        if ln.category not in {C.IMPORT_DUTY, C.MOTOR_VEHICLE_TAX, C.IMPORT_VAT, C.OTHER_IMPORT_CHARGES}
    ]
    result = run(others + list(tax_lines), buy=purchase(status=S.QUOTED), rates=eur_rates)
    assert result.material_support == "quote_supported"
    import_components = result.scenario(BASE).import_components
    assert import_components is not None
    per_category = ("10000.00", "1000", "22200.00", "1820.95")  # each category converted separately
    with decimal.localcontext(decimal.Context(prec=50)):  # costs sums at 50 digits, never rounds early
        expected = sum((CTX40.divide(Decimal(v), Decimal("61.5")) for v in per_category), Decimal(0))
    assert import_components.amount == expected
    assert abs(import_components.amount - Decimal("35020.95") / Decimal("61.5")) < Decimal("1e-20")


# --------------------------------------------------------------------------- cost profile


def test_default_cost_profile_lists_every_category_unknown() -> None:
    profile = load_cost_profile(REPO / "config" / "cost_profiles" / "default_unapproved.yaml")
    assert profile.approval_status == "unapproved" and profile.approved_by is None
    assert profile.missing_categories() == frozenset()
    assert {a.category for a in profile.assumptions} == REQUIRED_CATEGORIES
    assert all(
        a.status == S.UNKNOWN and a.base is None and a.low is None and a.high is None
        for a in profile.assumptions
    )
    assert all(len(a.note) > 10 for a in profile.assumptions)
    lines = profile.lines()
    assert all(ln.status == S.UNKNOWN and not ln.assumption_approved for ln in lines)
    result = run(list(lines))
    assert not result.complete and result.scenario(BASE).contribution_before_business_tax is None
    assert len(result.unknown_lines) == len(profile.assumptions)  # incl. the CH-scoped lines
    ref = profile.reference()
    assert ref.version == 2 and len(ref.sha256) == 64 and ref.approval_status == "unapproved"


def test_cost_profile_rules(tmp_path: Path) -> None:
    base = {"profile_key": "synthetic", "version": 1, "basis": "SYNTHETIC test profile"}
    with pytest.raises(ValidationError, match="approved_by"):
        CostProfile.model_validate({**base, "approval_status": "approved"})
    with pytest.raises(ValidationError, match="fixture"):
        CostProfile.model_validate(
            {
                **base,
                "approval_status": "approved",
                "approved_by": "o",
                "approved_at": AS_OF,
                "is_fixture": True,
            }
        )
    quoted = {"category": "transport", "label": "x", "status": "quoted", "note": "no quotes in profiles"}
    with pytest.raises(ValidationError, match="profile assumptions"):
        CostProfile.model_validate({**base, "assumptions": [quoted]})
    path = tmp_path / "float.yaml"
    path.write_text(
        "profile_key: synthetic\nversion: 1\nbasis: SYNTHETIC\nassumptions:\n"
        "  - {category: transport, label: x, status: estimated, base: 700.5, note: unquoted float}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="invalid cost profile"):
        load_cost_profile(path)
    path.write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="mapping"):
        load_cost_profile(path)
    with pytest.raises(ValidationFailed, match="cannot read"):
        load_cost_profile(tmp_path / "missing.yaml")
    approved = CostProfile.model_validate(
        {
            **base,
            "approval_status": "approved",
            "approved_by": "SYNTHETIC owner",
            "approved_at": AS_OF,
            "assumptions": [
                {
                    "category": "transport",
                    "label": "x",
                    "status": "estimated",
                    "base": "700.00",
                    "note": "SYNTHETIC",
                },
                {
                    "category": "refundable_deposit",
                    "label": "d",
                    "status": "estimated",
                    "base": "0",
                    "note": "SYNTHETIC",
                },
            ],
        }
    )
    lines = approved.lines()
    assert lines[0].assumption_approved and lines[0].base == eur("700.00")
    assert lines[1].refundable


# --------------------------------------------------------------------------- review regressions


def test_incomplete_calculation_of_a_componentless_rule_is_unknown_not_zero() -> None:
    """The shipped example rule set (no components, every input missing) must never yield
    not_applicable (= zero) import costs."""
    example = load_rule_set_file(REPO / "config" / "tax_rules" / "example_unapproved.json")
    calc = calculate(example, TaxInputs(), AS_OF)
    assert not calc.complete
    lines = tax_cost_lines(calc)
    assert {ln.category for ln in lines} == set(IMPORT_CATEGORIES)
    assert all(ln.status == S.UNKNOWN and ln.base is None for ln in lines)
    result = run(
        [ln for ln in spec_lines() if ln.category not in IMPORT_CATEGORIES] + list(lines),
    )
    assert result.scenario(BASE).import_components is None
    assert result.scenario(BASE).contribution_before_business_tax is None


def _without_other_charges(doc: dict[str, Any]) -> None:
    doc["components"] = [c for c in doc["components"] if c["category"] != "other_import_charges"]


def _synthetic_rule(mutate: Any = None) -> RuleSet:
    doc = json.loads(
        (REPO / "tests" / "fixtures" / "tax" / "synthetic_rule_set.json").read_text(encoding="utf-8"),
        parse_float=Decimal,
    )
    doc["sha256"] = None
    if mutate is not None:
        mutate(doc)
    return RuleSet.model_validate(doc)


def test_category_without_component_is_not_applicable_only_from_complete_calculation() -> None:
    rule = _synthetic_rule(_without_other_charges)
    at = datetime(2026, 6, 1, tzinfo=UTC)
    complete = calculate(rule, _tax_inputs(), at)
    assert complete.complete
    lines = {ln.category: ln for ln in tax_cost_lines(complete)}
    assert lines[C.OTHER_IMPORT_CHARGES].status == S.NOT_APPLICABLE
    assert "defines no other_import_charges" in (lines[C.OTHER_IMPORT_CHARGES].reason or "")

    def drop_wltp(doc: dict[str, Any]) -> None:
        _without_other_charges(doc)
        del next(c for c in doc["components"] if c["id"] == "co2_charge")["tables"]["wltp"]

    incomplete = calculate(_synthetic_rule(drop_wltp), _tax_inputs(), at)  # wltp unsupported -> unknown
    assert not incomplete.complete and incomplete.missing_inputs == ()
    lines = {ln.category: ln for ln in tax_cost_lines(incomplete)}
    assert lines[C.OTHER_IMPORT_CHARGES].status == S.UNKNOWN
    assert lines[C.IMPORT_DUTY].status == S.ESTIMATED  # resolved categories stay usable


def test_tax_lines_carry_the_exact_calculation_evidence_id() -> None:
    rule = load_rule_set_file(REPO / "tests" / "fixtures" / "tax" / "synthetic_rule_set.json")
    calc = calculate(rule, _tax_inputs(), datetime(2026, 6, 1, tzinfo=UTC))
    expected = tax_calculation_evidence_id(calc)
    assert expected == f"tax_calculation:{calc.content_sha256()}"
    assert all(expected in ln.evidence_ids for ln in tax_cost_lines(calc))
    lines = [ln for ln in spec_lines() if ln.category not in IMPORT_CATEGORIES] + list(tax_cost_lines(calc))
    rates = [FxRate(**{**ecb("MKD", "61.5").model_dump(), "provider": "SYNTHETIC owner-approved MKD"})]
    assert run(lines, rates=rates).import_line_sources == (expected,)
    assert run().import_line_sources == tuple(sorted(f"manual:{c.value}" for c in IMPORT_CATEGORIES))


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"basis": "mk_asking_prices", "status": S.QUOTED}, "never quotes"),
        ({"basis": "mk_asking_prices", "status": S.ACTUAL}, "never quotes"),
        ({"basis": "owner_estimate", "status": S.ACTUAL}, "never quotes"),
        ({"basis": "seller_reported_sales", "status": S.QUOTED}, "never quotes"),
        ({"basis": "verified_sales", "status": S.QUOTED}, "never quotes"),
        ({"basis": "verified_sales", "evidence_kind": "asking_price"}, "does not match"),
        ({"basis": "mk_asking_prices", "evidence_kind": "verified_sale"}, "does not match"),
        ({"basis": "dealer_offer", "evidence_kind": "owner_estimate"}, "does not match"),
    ],
)
def test_proceeds_basis_status_and_evidence_kind_must_agree(kwargs: dict[str, Any], match: str) -> None:
    status = kwargs.pop("status", S.ESTIMATED)
    with pytest.raises(ValidationError, match=match):
        ProceedsEstimate(
            status=status, currency="EUR", base=eur("9000.00"), evidence_ids=("SYNTHETIC-e",), **kwargs
        )


def test_asking_price_proceeds_never_supported_without_approved_discount() -> None:
    asking = proceeds(
        "9000.00", basis="mk_asking_prices", evidence_kind="asking_price", assumption_approved=True
    )
    assert not proceeds_supported(asking)
    unapproved_discount = proceeds(
        "9000.00", basis="mk_asking_prices", assumption_approved=True, negotiation_discount_pct=Decimal("8")
    )
    assert not proceeds_supported(unapproved_discount)
    approved_discount = unapproved_discount.model_copy(update={"discount_approved": True})
    assert proceeds_supported(approved_discount)
    offer = ProceedsEstimate(
        status=S.QUOTED,
        currency="EUR",
        base=eur("8500.00"),
        basis="dealer_offer",
        evidence_ids=("SYNTHETIC-dealer-offer",),
    )
    assert proceeds_supported(offer)
    verified = ProceedsEstimate(
        status=S.ACTUAL,
        currency="EUR",
        base=eur("8500.00"),
        basis="verified_sales",
        evidence_kind="verified_sale",
        evidence_ids=("SYNTHETIC-verified-sale",),
    )
    assert proceeds_supported(verified)


def test_unapproved_non_material_estimate_blocks_alert_and_quote_support() -> None:
    sell = proceeds("8000.00", low="7800.00", assumption_approved=True)
    buy = purchase(status=S.QUOTED)
    lines = [
        ln if ln.category != C.RISK_RESERVE else line(C.RISK_RESERVE, "0.00")  # convenient, unapproved
        for ln in _quoted_lines()
    ]
    result = run(lines, buy=buy, sell=sell, threshold=APPROVED)
    assert result.material_support == "estimated"
    assert any(u.startswith("risk_reserve") for u in result.unsupported_material)
    assert not result.threshold.alert_eligible
    assert any("risk_reserve" in b for b in result.threshold.blockers)
    approved = run(_quoted_lines(), buy=buy, sell=sell, threshold=APPROVED)
    assert approved.material_support == "quote_supported" and approved.threshold.alert_eligible


def test_conflicting_same_day_rates_are_never_chosen_silently() -> None:
    lines = [ln for ln in spec_lines() if ln.category != C.TRANSPORT]
    lines.append(line(C.TRANSPORT, "658.00", currency="CHF"))
    other = ecb("CHF", "0.8000").model_copy(update={"provider": "SYNTHETIC other publisher"})
    result = run(lines, rates=[ecb("CHF", "0.9400"), other])
    assert result.scenario(BASE).transport is None
    assert any(w.startswith("FX_RATE_AMBIGUOUS:CHF->EUR") for w in result.warnings)
    same_value = ecb("CHF", "0.940000").model_copy(update={"provider": "SYNTHETIC other publisher"})
    inverse = FxRate(
        base="CHF",
        quote="EUR",
        rate=Decimal("1.25"),
        rate_date=AS_OF.date() - timedelta(days=1),
        retrieved_at=AS_OF,
        provider="SYNTHETIC inverse publisher",
    )
    assert run(lines, rates=[ecb("CHF", "0.9400"), same_value]).scenario(BASE).transport == eur("700")
    consistent = run(lines, rates=[ecb("CHF", "0.8"), inverse])
    assert consistent.scenario(BASE).transport == eur("822.5")  # 658 / 0.8 == 658 * 1.25


def test_deposit_in_price_and_deposit_line_warns_of_double_count() -> None:
    buy = purchase("3300.00", included_refundable_deposit=eur("500.00"))
    lines = [ln for ln in spec_lines() if ln.category != C.REFUNDABLE_DEPOSIT]
    lines.append(line(C.REFUNDABLE_DEPOSIT, "500.00"))
    result = run(lines, buy=buy)
    assert any(w.startswith("DEPOSIT_POSSIBLY_COUNTED_TWICE") for w in result.warnings)
    clean = run([ln for ln in spec_lines() if ln.category != C.REFUNDABLE_DEPOSIT], buy=buy)
    assert not any("DEPOSIT_POSSIBLY" in w for w in clean.warnings)


def test_scoped_quote_without_target_scope_is_not_silently_a_quote() -> None:
    sell = proceeds("8000.00", low="7800.00", assumption_approved=True)
    scoped = line(C.TRANSPORT, "700.00", status=S.QUOTED, scope=CostScope(origin_city="SYNTHETIC-Munich"))
    lines = [scoped if ln.category == C.TRANSPORT else ln for ln in _quoted_lines()]
    result = run(lines, buy=purchase(status=S.QUOTED), sell=sell, threshold=APPROVED)
    assert any(w.startswith("COST_SCOPE_UNVERIFIED:transport") for w in result.warnings)
    assert result.scenario(BASE).transport == eur("700.00")  # amount still modelled ...
    assert not result.threshold.alert_eligible  # ... but it cannot support an alert as a quote
    matched = run(
        lines,
        buy=purchase(status=S.QUOTED),
        sell=sell,
        threshold=APPROVED,
        target_scope=CostScope(origin_city="SYNTHETIC-Munich"),
    )
    assert matched.material_support == "quote_supported" and matched.threshold.alert_eligible


@pytest.mark.parametrize(
    ("assumption", "match"),
    [
        ({"status": "estimated", "low": "900", "base": "700"}, "low <= base"),
        ({"status": "estimated", "base": "-1"}, "negative"),
        ({"status": "estimated"}, "base amount"),
        ({"status": "unknown", "base": "700"}, "no amounts"),
        ({"status": "not_applicable"}, "reason"),
    ],
)
def test_cost_profile_assumptions_are_validated_at_load(assumption: dict[str, Any], match: str) -> None:
    entry = {"category": "transport", "label": "SYNTHETIC", "note": "SYNTHETIC test", **assumption}
    with pytest.raises(ValidationError, match=match):
        CostProfile.model_validate(
            {"profile_key": "synthetic", "version": 1, "basis": "SYNTHETIC", "assumptions": [entry]}
        )


def test_required_categories_cannot_be_dropped_to_hide_missing_lines() -> None:
    for required in (frozenset(), REQUIRED_CATEGORIES - {C.REPAIRS}, REQUIRED_CATEGORIES | {C.PURCHASE}):
        with pytest.raises(ValidationFailed, match="not_applicable line"):
            run([], required_categories=required)
    assert run(required_categories=REQUIRED_CATEGORIES).complete
