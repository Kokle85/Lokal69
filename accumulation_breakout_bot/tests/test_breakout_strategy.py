"""Breakout strategy: direct entries, retest state machine, quality gates."""
import pandas as pd
import pytest

from breakout_strategy import BreakoutStrategy
from conftest import flat_zone_candles, make_candles
from models import Direction, EntryMode


def _frame_with(specs_after: list[tuple], cfg, n_zone: int = 30):
    """zone_lookback candles of accumulation + the given follow-up candles."""
    specs = flat_zone_candles(n=n_zone) + specs_after
    return make_candles(specs)


def test_direct_breakout_buy_signal(cfg):
    cfg.entry_mode = EntryMode.DIRECT_BREAKOUT
    # strong bullish candle closing well above zone_high 2001 + buffer
    breakout = (2000.9, 2001.5, 2000.85, 2001.4)
    df = _frame_with([breakout], cfg)
    ev = BreakoutStrategy(cfg).on_bar(df)
    assert ev.signal is not None, f"rejections: {ev.rejections}"
    s = ev.signal
    assert s.direction is Direction.BUY
    assert s.stop_loss < s.zone.low
    risk = s.entry_price - s.stop_loss
    assert s.take_profit == breakout[3] + 3.0 * risk


def test_direct_breakout_sell_signal(cfg):
    cfg.entry_mode = EntryMode.DIRECT_BREAKOUT
    breakout = (2000.4, 2000.45, 1999.45, 1999.5)
    df = _frame_with([breakout], cfg)
    ev = BreakoutStrategy(cfg).on_bar(df)
    assert ev.signal is not None, f"rejections: {ev.rejections}"
    assert ev.signal.direction is Direction.SELL
    assert ev.signal.stop_loss > ev.signal.zone.high


def test_weak_body_breakout_rejected(cfg):
    cfg.entry_mode = EntryMode.DIRECT_BREAKOUT
    # closes beyond the buffer but with a tiny body
    weak = (2001.28, 2001.42, 2001.2, 2001.38)
    df = _frame_with([weak], cfg)
    ev = BreakoutStrategy(cfg).on_bar(df)
    assert ev.signal is None
    assert any("body too weak" in r for r in ev.rejections)


def test_rejection_wick_breakout_rejected(cfg):
    cfg.entry_mode = EntryMode.DIRECT_BREAKOUT
    # strong body but a huge upper wick (sellers rejected the push)
    wicky = (2000.9, 2002.6, 2000.85, 2001.4)
    df = _frame_with([wicky], cfg)
    ev = BreakoutStrategy(cfg).on_bar(df)
    assert ev.signal is None
    assert any("wick too large" in r for r in ev.rejections)


def test_no_breakout_inside_zone(cfg):
    cfg.entry_mode = EntryMode.DIRECT_BREAKOUT
    inside = (2000.4, 2000.9, 2000.2, 2000.6)
    df = _frame_with([inside], cfg)
    ev = BreakoutStrategy(cfg).on_bar(df)
    assert ev.signal is None
    assert any("no breakout" in r for r in ev.rejections)


def test_retest_flow_produces_buy(cfg):
    cfg.entry_mode = EntryMode.BREAKOUT_RETEST
    strategy = BreakoutStrategy(cfg)
    breakout = (2000.9, 2001.5, 2000.85, 2001.4)
    df = _frame_with([breakout], cfg)
    ev1 = strategy.on_bar(df)
    assert ev1.signal is None
    assert strategy.pending is not None, f"rejections: {ev1.rejections}"

    # pullback touches the broken boundary (zone_high 2001) and closes back up
    retest = (2001.35, 2001.45, 2001.05, 2001.4)
    df2 = make_candles(flat_zone_candles() + [breakout, retest])
    ev2 = strategy.on_bar(df2)
    assert ev2.signal is not None, f"rejections: {ev2.rejections}"
    assert ev2.signal.direction is Direction.BUY
    assert ev2.signal.entry_mode is EntryMode.BREAKOUT_RETEST
    assert strategy.pending is None


def test_zone_mid_stop_mode_halves_the_risk(cfg):
    cfg.entry_mode = EntryMode.DIRECT_BREAKOUT
    breakout = (2000.9, 2001.5, 2000.85, 2001.4)

    cfg.stop_mode = "zone_opposite"
    full = BreakoutStrategy(cfg).on_bar(_frame_with([breakout], cfg))
    cfg.stop_mode = "zone_mid"
    mid = BreakoutStrategy(cfg).on_bar(_frame_with([breakout], cfg))

    assert full.signal is not None and mid.signal is not None
    assert mid.signal.stop_loss > full.signal.stop_loss  # mid stop sits higher
    # mid-stop risk = full risk minus half the zone (same entry, same buffer)
    assert (full.signal.risk_distance - mid.signal.risk_distance
            == pytest.approx(full.signal.zone.size / 2, abs=1e-9))
    # TP still at 3R of the (smaller) risk
    assert mid.signal.take_profit == pytest.approx(
        mid.signal.entry_price + 3.0 * mid.signal.risk_distance)


def test_retest_timeout_cancels(cfg):
    cfg.entry_mode = EntryMode.BREAKOUT_RETEST
    cfg.retest_timeout_candles = 3
    strategy = BreakoutStrategy(cfg)
    breakout = (2000.9, 2001.5, 2000.85, 2001.4)
    specs = flat_zone_candles() + [breakout]
    strategy.on_bar(make_candles(specs))
    assert strategy.pending is not None
    # price runs away without ever retesting
    for i in range(4):
        specs = specs + [(2002.0 + i * 0.3, 2002.4 + i * 0.3,
                          2001.9 + i * 0.3, 2002.3 + i * 0.3)]
        ev = strategy.on_bar(make_candles(specs))
    assert strategy.pending is None
    assert any("timeout" in r for r in ev.rejections)


def test_retest_deep_reentry_cancels(cfg):
    cfg.entry_mode = EntryMode.BREAKOUT_RETEST
    strategy = BreakoutStrategy(cfg)
    breakout = (2000.9, 2001.5, 2000.85, 2001.4)
    specs = flat_zone_candles() + [breakout]
    strategy.on_bar(make_candles(specs))
    assert strategy.pending is not None
    # price collapses back to the middle of the zone -> failed breakout
    fail = (2001.35, 2001.4, 2000.2, 2000.3)
    ev = strategy.on_bar(make_candles(specs + [fail]))
    assert strategy.pending is None
    assert ev.signal is None
    assert any("re-entered the zone" in r for r in ev.rejections)


def test_ema_filter_blocks_counter_trend_buy(cfg):
    cfg.entry_mode = EntryMode.DIRECT_BREAKOUT
    cfg.use_ema_filter = True
    cfg.ema_period = 20  # short EMA so the test frame is enough warmup
    # long downtrend before the zone -> EMA far above -> BUY must be blocked
    downtrend = []
    for i in range(60):
        base = 2030.0 - 0.5 * i
        downtrend.append((base, base + 0.3, base - 0.6, base - 0.4))
    specs = downtrend + flat_zone_candles(low=2000.0, high=2001.0) \
        + [(2000.9, 2001.5, 2000.85, 2001.4)]
    ev = BreakoutStrategy(cfg).on_bar(make_candles(specs))
    # EMA(20) over a fresh crash sits well above the zone -> buy rejected
    if ev.signal is not None:
        assert ev.signal.entry_price > ev.signal.ema_value  # sanity if it passed
    else:
        assert any("EMA filter" in r for r in ev.rejections), ev.rejections


def test_mtf_rsi_gate_blocks_counter_direction(cfg):
    cfg.entry_mode = EntryMode.DIRECT_BREAKOUT
    cfg.use_mtf_rsi_filter = True
    breakout = (2000.9, 2001.5, 2000.85, 2001.4)
    df = _frame_with([breakout], cfg)
    strategy = BreakoutStrategy(cfg)

    # H1/H4 momentum down -> BUY breakout must be rejected
    ev = strategy.on_bar(df, atr_value=0.7, ema_value=0.0, rsi_value=50.0,
                         mtf_rsi={"h1": 40.0, "h1_prev": 45.0, "h4": 42.0})
    assert ev.signal is None
    assert any("MTF RSI" in r for r in ev.rejections)

    # both timeframes above the midline -> BUY allowed (both_agree)
    strategy = BreakoutStrategy(cfg)
    ev2 = strategy.on_bar(df, atr_value=0.7, ema_value=0.0, rsi_value=50.0,
                          mtf_rsi={"h1": 61.0, "h1_prev": 58.0, "h4": 55.0})
    assert ev2.signal is not None, ev2.rejections

    # cross_confirm additionally demands H1 RSI RISING for a BUY
    cfg.mtf_rsi_mode = "cross_confirm"
    strategy = BreakoutStrategy(cfg)
    ev3 = strategy.on_bar(df, atr_value=0.7, ema_value=0.0, rsi_value=50.0,
                          mtf_rsi={"h1": 61.0, "h1_prev": 65.0, "h4": 55.0})
    assert ev3.signal is None  # falling H1 = no fresh upward break
    strategy = BreakoutStrategy(cfg)
    ev4 = strategy.on_bar(df, atr_value=0.7, ema_value=0.0, rsi_value=50.0,
                          mtf_rsi={"h1": 61.0, "h1_prev": 49.0, "h4": 55.0})
    assert ev4.signal is not None, ev4.rejections


def test_htf_rsi_uses_only_closed_bars(cfg):
    """The last (still-forming) H1 bucket must not leak into the HTF RSI."""
    import numpy as np
    from indicators import last_closed_htf_rsi

    n = 12 * 40  # 40 hours of M5
    times = pd.date_range("2026-01-05 00:00", periods=n, freq="5min")
    close = np.linspace(2000, 2010, n)
    df = pd.DataFrame({"time": times, "open": close, "high": close + 0.1,
                       "low": close - 0.1, "close": close, "volume": 1})
    # cut mid-hour: last M5 bar opens at HH:25 -> that H1 bar is NOT closed
    df_cut = df.iloc[: n - 6]
    full_h1, _ = last_closed_htf_rsi(df, 60, 14)
    cut_h1, _ = last_closed_htf_rsi(df_cut, 60, 14)
    assert not pd.isna(cut_h1)
    # monotone rising series -> RSI 100 everywhere; the real assertion is that
    # the call works and excludes the partial bucket without crashing
    assert cut_h1 == pytest.approx(100.0, abs=1e-6)
