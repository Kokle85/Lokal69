"""High Precision Scalp strategy tests."""
from models import Direction, Regime
from setup_builders import hp_buy_snapshot, hp_sell_snapshot
from strategy_high_precision import HighPrecisionStrategy


def make_strategy(hp_cfg, tuning, max_spread=25.0):
    return HighPrecisionStrategy(hp_cfg, tuning, max_spread, 15.0, 350.0)


def test_high_precision_buy_setup(hp_cfg, tuning):
    snap = hp_buy_snapshot(tuning)
    result = make_strategy(hp_cfg, tuning).evaluate(snap, Regime.TREND_UP)
    assert result.signal is not None, f"rejections: {result.rejections}"
    sig = result.signal
    assert sig.direction is Direction.BUY
    assert sig.sl < sig.entry < sig.tp
    assert sig.score >= hp_cfg.min_precision_score
    assert abs((sig.tp - sig.entry) - hp_cfg.rr * (sig.entry - sig.sl)) < 1e-6


def test_high_precision_sell_setup(hp_cfg, tuning):
    snap = hp_sell_snapshot(tuning)
    result = make_strategy(hp_cfg, tuning).evaluate(snap, Regime.TREND_DOWN)
    assert result.signal is not None, f"rejections: {result.rejections}"
    sig = result.signal
    assert sig.direction is Direction.SELL
    assert sig.tp < sig.entry < sig.sl
    assert sig.score >= hp_cfg.min_precision_score


def test_no_trade_when_spread_high(hp_cfg, tuning):
    snap = hp_buy_snapshot(tuning, spread_points=60.0)
    result = make_strategy(hp_cfg, tuning).evaluate(snap, Regime.TREND_UP)
    assert result.signal is None
    assert any("spread" in r for r in result.rejections)


def test_no_trade_in_wrong_regime(hp_cfg, tuning):
    snap = hp_buy_snapshot(tuning)
    result = make_strategy(hp_cfg, tuning).evaluate(snap, Regime.CHOPPY)
    assert result.signal is None


def test_every_rejection_has_a_reason(hp_cfg, tuning):
    snap = hp_buy_snapshot(tuning, spread_points=60.0)
    result = make_strategy(hp_cfg, tuning).evaluate(snap, Regime.TREND_UP)
    assert result.signal is None
    assert len(result.rejections) > 0
