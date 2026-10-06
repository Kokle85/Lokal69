"""Property tests for scenario economics (spec 31 "Economics": unit/property tests).

Generated amounts are SYNTHETIC. Properties:
- complete totals equal the exact sum of their parts (no double counting, no rounding);
- conservative <= base <= upside contribution when bounds are independent;
- adding an unknown line always makes the dependent totals None (unknown is never zero);
- explicit rounding lands on a multiple of the quantum within one step.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from suv_deals.domain.costs import (
    REQUIRED_CATEGORIES,
    TERM_BY_CATEGORY,
    CostLine,
    ProceedsEstimate,
    PurchaseInput,
    Term,
    compute_scenarios,
)
from suv_deals.domain.enums import CostCategory, CostLineStatus, ScenarioName
from suv_deals.domain.money import Money
from suv_deals.domain.profiles import ContributionThreshold
from suv_deals.domain.tax_engine import RoundingSpec, apply_rounding

AS_OF = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
CATEGORIES = sorted(REQUIRED_CATEGORIES - {CostCategory.REFUNDABLE_DEPOSIT})
PICK = {ScenarioName.CONSERVATIVE: 2, ScenarioName.BASE: 1, ScenarioName.UPSIDE: 0}

cents = st.integers(min_value=0, max_value=5_000_000).map(lambda c: Decimal(c).scaleb(-2))
bounds = st.lists(cents, min_size=3, max_size=3).map(sorted)


def eur(value: Decimal) -> Money:
    return Money(amount=value, currency="EUR")


@st.composite
def cost_lines(draw: st.DrawFn) -> dict[CostCategory, list[Decimal] | None]:
    """Per category: [low, base, high] for an estimated line, or None for not_applicable."""
    return {c: draw(st.one_of(st.none(), bounds)) for c in CATEGORIES}


def build(spec: dict[CostCategory, list[Decimal] | None]) -> list[CostLine]:
    lines: list[CostLine] = []
    for category, values in spec.items():
        if values is None:
            lines.append(
                CostLine(
                    category=category,
                    label=f"SYNTHETIC {category.value}",
                    status=CostLineStatus.NOT_APPLICABLE,
                    currency="EUR",
                    reason="SYNTHETIC property test",
                )
            )
        else:
            low, base, high = values
            lines.append(
                CostLine(
                    category=category,
                    label=f"SYNTHETIC {category.value}",
                    status=CostLineStatus.ESTIMATED,
                    currency="EUR",
                    low=eur(low),
                    base=eur(base),
                    high=eur(high),
                )
            )
    lines.append(
        CostLine(
            category=CostCategory.REFUNDABLE_DEPOSIT,
            label="SYNTHETIC no deposit",
            status=CostLineStatus.NOT_APPLICABLE,
            currency="EUR",
            reason="SYNTHETIC property test",
        )
    )
    return lines


def run(lines: list[CostLine], buy: Decimal, sale: list[Decimal]):  # type: ignore[no-untyped-def]
    low, base, high = sale
    return compute_scenarios(
        PurchaseInput(status=CostLineStatus.ESTIMATED, amount=eur(buy)),
        lines,
        ProceedsEstimate(
            status=CostLineStatus.ESTIMATED,
            currency="EUR",
            low=eur(low),
            base=eur(base),
            high=eur(high),
            basis="owner_estimate",
        ),
        [],
        ContributionThreshold(),
        as_of=AS_OF,
    )


@settings(max_examples=80, deadline=None)
@given(spec=cost_lines(), buy=cents, sale=bounds)
def test_complete_totals_equal_sum_of_parts(
    spec: dict[CostCategory, list[Decimal] | None], buy: Decimal, sale: list[Decimal]
) -> None:
    result = run(build(spec), buy, sale)
    assert result.complete
    for name, index in PICK.items():
        scenario = result.scenario(name)
        parts = {c: (v[index] if v is not None else Decimal(0)) for c, v in spec.items()}
        expected_total = buy + sum(parts.values(), Decimal(0))
        assert scenario.total_modelled_cost == eur(expected_total)
        assert scenario.known_subtotal == eur(expected_total)
        proceeds = sale[0 if name == ScenarioName.CONSERVATIVE else 2 if name == ScenarioName.UPSIDE else 1]
        assert scenario.contribution_before_business_tax == eur(proceeds - expected_total)

        def term(t: Term, values: dict[CostCategory, Decimal] = parts) -> Decimal:
            return sum((v for c, v in values.items() if TERM_BY_CATEGORY[c] == t), Decimal(0))

        economic = buy + term(Term.ACQUISITION_COSTS)
        landed = economic + term(Term.TRANSPORT) + term(Term.IMPORT_COMPONENTS) + term(Term.CLEARANCE)
        ready = (
            landed
            + term(Term.REPAIRS)
            + term(Term.PREPARATION)
            + term(Term.INCLUDED_REGISTRATION)
            + term(Term.RESERVES)
        )
        assert scenario.purchase_economic_cost == eur(economic)
        assert scenario.landed_cost == eur(landed)
        assert scenario.ready_to_sell_cost == eur(ready)
        assert scenario.cash_required == eur(expected_total)  # every line paid before sale, no deposits
        assert scenario.refundable_deposits == eur(Decimal(0))


@settings(max_examples=60, deadline=None)
@given(spec=cost_lines(), buy=cents, sale=bounds)
def test_scenarios_are_ordered_when_bounds_are_independent(
    spec: dict[CostCategory, list[Decimal] | None], buy: Decimal, sale: list[Decimal]
) -> None:
    result = run(build(spec), buy, sale)
    cons, base, up = (
        result.scenario(n).contribution_before_business_tax
        for n in (ScenarioName.CONSERVATIVE, ScenarioName.BASE, ScenarioName.UPSIDE)
    )
    assert cons is not None and base is not None and up is not None
    assert cons <= base <= up


@settings(max_examples=60, deadline=None)
@given(
    spec=cost_lines(), buy=cents, sale=bounds, unknown_category=st.sampled_from(sorted(REQUIRED_CATEGORIES))
)
def test_adding_an_unknown_line_always_makes_totals_unknown(
    spec: dict[CostCategory, list[Decimal] | None],
    buy: Decimal,
    sale: list[Decimal],
    unknown_category: CostCategory,
) -> None:
    lines = build(spec)
    before = run(lines, buy, sale)
    unknown = CostLine(
        category=unknown_category,
        label="SYNTHETIC unknown line",
        status=CostLineStatus.UNKNOWN,
        currency="EUR",
    )
    after = run([*lines, unknown], buy, sale)
    assert not after.complete
    assert len(after.unknown_lines) == len(before.unknown_lines) + 1
    for name in PICK:
        scenario = after.scenario(name)
        assert scenario.contribution_before_business_tax is None
        assert scenario.cash_required is None
        if unknown_category != CostCategory.REFUNDABLE_DEPOSIT:
            assert scenario.total_modelled_cost is None
            assert scenario.known_subtotal == before.scenario(name).known_subtotal
        term = TERM_BY_CATEGORY[unknown_category]
        assert getattr(scenario, term.value) is None


@settings(max_examples=60, deadline=None)
@given(spec=cost_lines(), buy=cents, sale=bounds, dropped=st.sampled_from(CATEGORIES))
def test_a_missing_category_is_never_treated_as_zero(
    spec: dict[CostCategory, list[Decimal] | None],
    buy: Decimal,
    sale: list[Decimal],
    dropped: CostCategory,
) -> None:
    lines = [ln for ln in build(spec) if ln.category != dropped]
    result = run(lines, buy, sale)
    assert any(u.item == dropped.value for u in result.unknown_lines)
    assert all(result.scenario(n).total_modelled_cost is None for n in PICK)


@settings(max_examples=150, deadline=None)
@given(
    value=st.decimals(min_value=0, max_value=10**9, places=4, allow_nan=False, allow_infinity=False),
    quantum=st.sampled_from([Decimal("0.01"), Decimal("0.05"), Decimal("1"), Decimal("10"), Decimal("100")]),
    mode=st.sampled_from(["half_up", "half_even", "down", "up"]),
)
def test_rounding_lands_on_quantum_within_one_step(value: Decimal, quantum: Decimal, mode: str) -> None:
    rounded = apply_rounding(value, RoundingSpec(quantum=quantum, mode=mode))  # type: ignore[arg-type]
    assert (rounded / quantum) == (rounded / quantum).to_integral_value()
    diff = abs(rounded - value)
    if mode in ("half_up", "half_even"):
        assert diff <= quantum / 2
    else:
        assert diff < quantum
        assert (rounded <= value) if mode == "down" else (rounded >= value)


@settings(max_examples=60, deadline=None)
@given(asking=cents, pct=st.integers(min_value=0, max_value=9999).map(lambda p: Decimal(p).scaleb(-2)))
def test_negotiation_discount_is_exact(asking: Decimal, pct: Decimal) -> None:
    sale = ProceedsEstimate(
        status=CostLineStatus.ESTIMATED,
        currency="EUR",
        base=eur(asking),
        basis="mk_asking_prices",
        negotiation_discount_pct=pct,
    )
    result = compute_scenarios(
        PurchaseInput(status=CostLineStatus.ESTIMATED, amount=eur(Decimal(0))),
        build(dict.fromkeys(CATEGORIES)),
        sale,
        [],
        ContributionThreshold(),
        as_of=AS_OF,
    )
    realized = result.scenario(ScenarioName.BASE).expected_realized_proceeds
    assert realized is not None
    assert realized.amount == asking * (1 - pct / 100)
