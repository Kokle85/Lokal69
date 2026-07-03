"""Momentum Scalp strategy tests."""
from models import Direction, Regime
from setup_builders import momentum_buy_snapshot, momentum_sell_snapshot
from strategy_momentum import MomentumStrategy


def make_strategy(mo_cfg, tuning, max_spread=25.0):
    return MomentumStrategy(mo_cfg, tuning, max_spread, 15.0, 350.0)


def test_momentum_buy_setup(mo_cfg, tuning):
    snap = momentum_buy_snapshot(tuning)
    result = make_strategy(mo_cfg, tuning).evaluate(snap, Regime.TREND_UP)
    assert result.signal is not None, f"rejections: {result.rejections}"
    sig = result.signal
    assert sig.direction is Direction.BUY
    assert sig.sl < sig.entry < sig.tp
    assert sig.rr == mo_cfg.rr
    assert sig.score >= mo_cfg.min_quality_score


def test_momentum_sell_setup(mo_cfg, tuning):
    snap = momentum_sell_snapshot(tuning)
    result = make_strategy(mo_cfg, tuning).evaluate(snap, Regime.TREND_DOWN)
    assert result.signal is not None, f"rejections: {result.rejections}"
    sig = result.signal
    assert sig.direction is Direction.SELL
    assert sig.tp < sig.entry < sig.sl
    assert sig.score >= mo_cfg.min_quality_score


def test_momentum_tp_is_1_6_r(mo_cfg, tuning):
    snap = momentum_buy_snapshot(tuning)
    result = make_strategy(mo_cfg, tuning).evaluate(snap, Regime.TREND_UP)
    assert result.signal is not None
    sig = result.signal
    assert abs((sig.tp - sig.entry) - 1.6 * (sig.entry - sig.sl)) < 1e-6


def test_momentum_blocked_by_spread(mo_cfg, tuning):
    snap = momentum_buy_snapshot(tuning, spread_points=60.0)
    result = make_strategy(mo_cfg, tuning).evaluate(snap, Regime.TREND_UP)
    assert result.signal is None
    assert any("spread" in r for r in result.rejections)


def test_momentum_no_trade_in_range(mo_cfg, tuning):
    snap = momentum_buy_snapshot(tuning)
    result = make_strategy(mo_cfg, tuning).evaluate(snap, Regime.RANGE)
    assert result.signal is None
