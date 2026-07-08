"""Indicator unit tests: EMA, RSI, ATR, candle anatomy, structure, filters."""
import numpy as np
import pandas as pd

import indicators as ind
from conftest import make_candles, zigzag_closes


def test_ema_output_exists():
    series = pd.Series(np.linspace(2000, 2010, 100))
    out = ind.ema(series, 20)
    assert len(out) == 100
    assert out.notna().all()
    # EMA of a rising series trails the price
    assert out.iloc[-1] < series.iloc[-1]


def test_rsi_range():
    rng = np.random.default_rng(7)
    series = pd.Series(2000 + np.cumsum(rng.normal(0, 0.5, 300)))
    out = ind.rsi(series, 14)
    assert ((out >= 0) & (out <= 100)).all()
    # strongly rising series -> high RSI; falling -> low RSI
    rising = ind.rsi(pd.Series(np.linspace(2000, 2020, 100)), 14)
    assert rising.iloc[-1] > 70
    falling = ind.rsi(pd.Series(np.linspace(2020, 2000, 100)), 14)
    assert falling.iloc[-1] < 30


def test_atr_positive():
    df = make_candles(zigzag_closes(100, 2000.0))
    out = ind.atr(df, 14)
    assert (out.iloc[5:] > 0).all()


def test_candle_anatomy():
    row = pd.Series({"open": 100.0, "high": 103.0, "low": 98.0, "close": 102.0})
    assert ind.candle_body(row) == 2.0
    assert ind.upper_wick(row) == 1.0
    assert ind.lower_wick(row) == 2.0
    assert ind.candle_range(row) == 5.0
    assert ind.wick_body_ratio(row) == 1.5


def test_bullish_rejection_detection():
    # bullish, lower wick 3x body, close in top 40% of range
    row = pd.Series({"open": 2000.0, "high": 2000.32, "low": 1999.40, "close": 2000.20})
    assert ind.is_bullish_rejection(row, 1.5)
    # bearish candle can never be a bullish rejection
    row2 = pd.Series({"open": 2000.2, "high": 2000.3, "low": 1999.4, "close": 2000.0})
    assert not ind.is_bullish_rejection(row2, 1.5)
    # wick too small
    row3 = pd.Series({"open": 2000.0, "high": 2000.5, "low": 1999.9, "close": 2000.4})
    assert not ind.is_bullish_rejection(row3, 1.5)


def test_bearish_rejection_detection():
    row = pd.Series({"open": 2000.20, "high": 2001.0, "low": 1999.90, "close": 2000.0})
    assert ind.is_bearish_rejection(row, 1.5)
    row2 = pd.Series({"open": 2000.0, "high": 2001.0, "low": 1999.9, "close": 2000.2})
    assert not ind.is_bearish_rejection(row2, 1.5)


def test_swing_detection():
    closes = zigzag_closes(60, 2000.0, up=0.5, down=0.3, up_run=4, down_run=2)
    df = make_candles(closes)
    assert ind.recent_swing_low(df) is not None
    assert ind.recent_swing_high(df) is not None


def test_higher_high_higher_low_structure():
    df_up = make_candles(zigzag_closes(80, 2000.0, up=0.5, down=0.3, up_run=4, down_run=2))
    assert ind.has_higher_highs_higher_lows(df_up)
    assert not ind.has_lower_highs_lower_lows(df_up)


def test_lower_high_lower_low_structure():
    df_down = make_candles(
        zigzag_closes(80, 2040.0, up=0.5, down=0.3, up_run=4, down_run=2, rising=False)
    )
    assert ind.has_lower_highs_lower_lows(df_down)
    assert not ind.has_higher_highs_higher_lows(df_down)


def test_choppy_candle_rejection():
    # alternating direction with huge wicks
    closes, opens, highs, lows = [], [], [], []
    price = 2000.0
    for i in range(20):
        direction = 1 if i % 2 == 0 else -1
        opens.append(price)
        price = price + direction * 0.05
        closes.append(price)
        highs.append(max(opens[-1], price) + 0.40)
        lows.append(min(opens[-1], price) - 0.40)
    df = make_candles(closes, opens=opens, highs=highs, lows=lows)
    ema20 = df["close"].ewm(span=20, adjust=False).mean()
    choppy, reason = ind.is_choppy_market(df, ema20, 1.8)
    assert choppy
    assert reason


def test_clean_trend_not_choppy():
    df = make_candles(zigzag_closes(60, 2000.0, up=0.4, down=0.2, up_run=5, down_run=1), pad=0.02)
    ema20 = df["close"].ewm(span=20, adjust=False).mean()
    choppy, _ = ind.is_choppy_market(df, ema20, 1.8)
    assert not choppy


def test_spike_candle_rejection():
    closes = zigzag_closes(60, 2000.0, up=0.2, down=0.1)
    df = make_candles(closes)
    # blow out the final candle range
    df.loc[df.index[-1], "high"] = df["close"].iloc[-1] + 5.0
    atr_series = ind.atr(df, 14)
    spike, reason = ind.is_spike_candle(df, atr_series, 2.5)
    assert spike
    assert "ATR" in reason


def test_normal_candle_not_spike():
    df = make_candles(zigzag_closes(60, 2000.0, up=0.2, down=0.1))
    atr_series = ind.atr(df, 14)
    spike, _ = ind.is_spike_candle(df, atr_series, 2.5)
    assert not spike
