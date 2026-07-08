"""Strategy selector tests using stub strategies."""
from datetime import datetime, timezone

from models import Direction, Regime, Signal, StrategyName
from strategy_high_precision import EvaluationResult
from strategy_selector import StrategySelector


class StubStrategy:
    def __init__(self, result: EvaluationResult) -> None:
        self.result = result

    def evaluate(self, snap, regime) -> EvaluationResult:
        return self.result


def make_signal(strategy: StrategyName, rr: float, score: int, spread: float = 10.0, sl_dist: float = 0.4) -> Signal:
    entry = 2000.0
    return Signal(
        symbol="XAUUSD",
        direction=Direction.BUY,
        strategy=strategy,
        regime=Regime.TREND_UP,
        entry=entry,
        sl=entry - sl_dist,
        tp=entry + rr * sl_dist,
        rr=rr,
        score=score,
        max_score=11,
        setup_reason="test",
        spread_points=spread,
        created_at=datetime.now(timezone.utc),
    )


def selector_with(hp_signal, mo_signal) -> StrategySelector:
    return StrategySelector(
        StubStrategy(EvaluationResult(hp_signal, [] if hp_signal else ["hp rejected"])),
        StubStrategy(EvaluationResult(mo_signal, [] if mo_signal else ["mo rejected"])),
    )


def test_no_trade_in_choppy_spike_dead_market():
    hp = make_signal(StrategyName.HIGH_PRECISION, 1.0, 11)
    sel = selector_with(hp, None)
    for regime in (Regime.CHOPPY, Regime.SPIKE, Regime.DEAD_MARKET):
        result = sel.select(None, regime)
        assert result.signal is None
        assert regime.value in result.reason


def test_only_high_precision_signal():
    hp = make_signal(StrategyName.HIGH_PRECISION, 1.0, 9)
    result = selector_with(hp, None).select(None, Regime.TREND_UP)
    assert result.signal is hp


def test_only_momentum_signal():
    mo = make_signal(StrategyName.MOMENTUM, 1.6, 9)
    result = selector_with(None, mo).select(None, Regime.TREND_UP)
    assert result.signal is mo


def test_both_signals_higher_expected_r_wins():
    hp = make_signal(StrategyName.HIGH_PRECISION, 1.0, 11)   # expected R = 1.0
    mo = make_signal(StrategyName.MOMENTUM, 1.6, 9)          # expected R = 1.6*9/11 = 1.309
    result = selector_with(hp, mo).select(None, Regime.TREND_UP)
    assert result.signal is mo


def test_tie_broken_by_spread():
    # identical expected R: rr 1.0 score 11 vs rr 2.0 score 5.5 -> use same values instead
    hp = make_signal(StrategyName.HIGH_PRECISION, 1.0, 11, spread=8.0)
    mo = make_signal(StrategyName.MOMENTUM, 1.0, 11, spread=15.0)
    result = selector_with(hp, mo).select(None, Regime.TREND_UP)
    assert result.signal is hp
    assert "spread" in result.reason


def test_tie_broken_by_cleaner_sl():
    hp = make_signal(StrategyName.HIGH_PRECISION, 1.0, 11, spread=10.0, sl_dist=0.3)
    mo = make_signal(StrategyName.MOMENTUM, 1.0, 11, spread=10.0, sl_dist=0.6)
    result = selector_with(hp, mo).select(None, Regime.TREND_UP)
    assert result.signal is hp
    assert "stop loss" in result.reason


def test_rejections_are_reported():
    result = selector_with(None, None).select(None, Regime.TREND_UP)
    assert result.signal is None
    assert any("hp rejected" in r for r in result.rejections)
    assert any("mo rejected" in r for r in result.rejections)
