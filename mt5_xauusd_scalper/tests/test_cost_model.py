"""Cost model tests: spread variation, adverse fills, commission, intrabar rule."""
import pytest

from config import BacktestConfig
from cost_model import CostModel
from models import Direction, SymbolSpec

SPEC = SymbolSpec(
    name="XAUUSD", point=0.01, tick_size=0.01, tick_value=1.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01, digits=2,
)


def model(**overrides) -> CostModel:
    return CostModel(BacktestConfig(**overrides), SPEC)


def test_entry_fill_is_adverse_both_directions():
    cm = model(base_spread_points=20, entry_slippage_points=3, requote_probability=0.0, spread_std_points=0)
    buy = cm.entry_fill(2000.0, Direction.BUY, 1)
    sell = cm.entry_fill(2000.0, Direction.SELL, 1)
    # half spread (10pt=0.10) + slippage (3pt=0.03) = 0.13 adverse
    assert buy.price == round(2000.0 + 0.13, 10)
    assert sell.price == round(2000.0 - 0.13, 10)


def test_exit_fill_is_adverse_both_directions():
    cm = model(base_spread_points=20, exit_slippage_points=3, spread_std_points=0)
    buy_exit = cm.exit_fill(2005.0, Direction.BUY, 1)   # buy exits at bid -> lower
    sell_exit = cm.exit_fill(2005.0, Direction.SELL, 1)  # sell exits at ask -> higher
    assert buy_exit < 2005.0
    assert sell_exit > 2005.0


def test_commission_scales_with_lot():
    cm = model(commission_per_lot_per_side_usd=3.5)
    assert cm.commission(1.0) == 3.5
    assert cm.commission(0.4) == pytest.approx(1.4)


def test_spread_is_deterministic_and_bounded():
    cm = model(base_spread_points=18, spread_std_points=6, max_spread_points=60)
    a = cm.spread_points(42)
    b = cm.spread_points(42)
    assert a == b  # deterministic for a given trade index + seed
    for i in range(200):
        assert 0.0 <= cm.spread_points(i) <= 60.0


def test_spread_varies_across_trades():
    cm = model(base_spread_points=18, spread_std_points=6)
    values = {cm.spread_points(i) for i in range(50)}
    assert len(values) > 5  # not a constant


def test_intrabar_conservative_stops_first():
    assert model(intrabar_fill="conservative").stop_hit_first(1) is True


def test_intrabar_optimistic_targets_first():
    assert model(intrabar_fill="optimistic").stop_hit_first(1) is False


def test_intrabar_random_is_deterministic():
    cm = model(intrabar_fill="random")
    assert cm.stop_hit_first(7) == cm.stop_hit_first(7)


def test_requote_adds_extra_slippage():
    # requote_probability 1.0 -> always adds the extra points
    base = model(base_spread_points=20, entry_slippage_points=3, requote_probability=0.0, spread_std_points=0)
    rq = model(base_spread_points=20, entry_slippage_points=3, requote_probability=1.0,
               requote_extra_points=5, spread_std_points=0)
    b0 = base.entry_fill(2000.0, Direction.BUY, 1).price
    b1 = rq.entry_fill(2000.0, Direction.BUY, 1).price
    assert b1 > b0  # requote makes the buy fill even worse
