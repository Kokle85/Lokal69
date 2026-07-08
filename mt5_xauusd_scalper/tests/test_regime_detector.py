"""Regime detector tests: TREND_UP, TREND_DOWN, CHOPPY, SPIKE, DEAD_MARKET."""
import pandas as pd

from conftest import downtrend_m15, downtrend_m5, make_candles, uptrend_m15, uptrend_m5, zigzag_closes
from models import Regime
from regime_detector import RegimeDetector, build_snapshot


def make_detector(regime_cfg):
    return RegimeDetector(regime_cfg, max_spread_points=25)


def rising_m1(n=200, start=2020.0):
    return make_candles(zigzag_closes(n, start, up=0.25, down=0.15, up_run=3, down_run=1), pad=0.03)


def falling_m1(n=200, start=2020.0):
    return make_candles(
        zigzag_closes(n, start, up=0.25, down=0.15, up_run=3, down_run=1, rising=False), pad=0.03
    )


def test_regime_trend_up(tuning, regime_cfg):
    snap = build_snapshot(uptrend_m15(), uptrend_m5(), rising_m1(), tuning, spread_points=10, point=0.01)
    result = make_detector(regime_cfg).detect(snap)
    assert result.regime is Regime.TREND_UP
    assert result.tradeable


def test_regime_trend_down(tuning, regime_cfg):
    snap = build_snapshot(
        downtrend_m15(), downtrend_m5(), falling_m1(), tuning, spread_points=10, point=0.01
    )
    result = make_detector(regime_cfg).detect(snap)
    assert result.regime is Regime.TREND_DOWN
    assert result.tradeable


def test_regime_choppy_on_timeframe_conflict(tuning, regime_cfg):
    # M15 up but M5 down -> conflict -> CHOPPY
    snap = build_snapshot(
        uptrend_m15(), downtrend_m5(start_price=2100.0), rising_m1(), tuning,
        spread_points=10, point=0.01,
    )
    result = make_detector(regime_cfg).detect(snap)
    assert result.regime is Regime.CHOPPY
    assert not result.tradeable


def test_regime_spike(tuning, regime_cfg):
    m1 = rising_m1()
    m1.loc[m1.index[-1], "high"] = m1["close"].iloc[-1] + 8.0  # massive range candle
    snap = build_snapshot(uptrend_m15(), uptrend_m5(), m1, tuning, spread_points=10, point=0.01)
    result = make_detector(regime_cfg).detect(snap)
    assert result.regime is Regime.SPIKE
    assert not result.tradeable


def test_regime_spike_on_sudden_spread(tuning, regime_cfg):
    snap = build_snapshot(
        uptrend_m15(), uptrend_m5(), rising_m1(), tuning, spread_points=80, point=0.01
    )
    result = make_detector(regime_cfg).detect(snap)
    assert result.regime is Regime.SPIKE


def test_regime_dead_market(tuning, regime_cfg):
    flat = make_candles([2000.0 + 0.01 * (i % 2) for i in range(200)], pad=0.005)
    snap = build_snapshot(uptrend_m15(), uptrend_m5(), flat, tuning, spread_points=10, point=0.01)
    result = make_detector(regime_cfg).detect(snap)
    assert result.regime is Regime.DEAD_MARKET
    assert not result.tradeable


def test_m15_m5_trend_alignment_required(tuning, regime_cfg):
    # aligned timeframes produce a trend regime; the alignment itself is what we assert
    snap = build_snapshot(uptrend_m15(), uptrend_m5(), rising_m1(), tuning, spread_points=10, point=0.01)
    m15_up = snap.m15_ema_fast.iloc[-1] > snap.m15_ema_slow.iloc[-1]
    m5_up = snap.m5_ema_fast.iloc[-1] > snap.m5_ema_slow.iloc[-1]
    assert m15_up and m5_up
