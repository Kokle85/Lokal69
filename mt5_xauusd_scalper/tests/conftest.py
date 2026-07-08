"""Shared test fixtures: synthetic candle builders (no MT5 required)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from config import (  # noqa: E402
    DailyGoalsConfig,
    HighPrecisionConfig,
    MomentumConfig,
    PositionManagementConfig,
    RegimeConfig,
    StrategyTuningConfig,
)

START = pd.Timestamp("2026-01-05 08:00", tz="UTC")  # a Monday


def make_candles(
    closes,
    start: pd.Timestamp = START,
    freq_min: int = 1,
    pad: float = 0.05,
    opens=None,
    highs=None,
    lows=None,
    volume: float = 100.0,
) -> pd.DataFrame:
    """Build an OHLC frame from close prices. open = previous close by default."""
    closes = list(map(float, closes))
    if opens is None:
        opens = [closes[0]] + closes[:-1]
    n = len(closes)
    high = [
        highs[i] if highs is not None and highs[i] is not None else max(opens[i], closes[i]) + pad
        for i in range(n)
    ]
    low = [
        lows[i] if lows is not None and lows[i] is not None else min(opens[i], closes[i]) - pad
        for i in range(n)
    ]
    times = [start + pd.Timedelta(minutes=freq_min * i) for i in range(n)]
    return pd.DataFrame(
        {
            "time": times,
            "open": opens,
            "high": high,
            "low": low,
            "close": closes,
            "tick_volume": [volume] * n,
        }
    )


def zigzag_closes(
    n: int, start_price: float, up: float = 0.25, down: float = 0.15,
    up_run: int = 3, down_run: int = 1, rising: bool = True,
) -> list[float]:
    """Rising (or falling) zigzag close series with clear swing points."""
    closes = [start_price]
    i = 0
    while len(closes) < n:
        for _ in range(up_run):
            step = up if rising else -up
            closes.append(closes[-1] + step)
            if len(closes) >= n:
                break
        for _ in range(down_run):
            step = -down if rising else down
            closes.append(closes[-1] + step)
            if len(closes) >= n:
                break
        i += 1
    return closes[:n]


def uptrend_m15(n: int = 120, start_price: float = 1990.0) -> pd.DataFrame:
    return make_candles(
        zigzag_closes(n, start_price, up=0.8, down=0.5), start=START - pd.Timedelta(minutes=15 * n),
        freq_min=15, pad=0.2,
    )


def downtrend_m15(n: int = 120, start_price: float = 2050.0) -> pd.DataFrame:
    return make_candles(
        zigzag_closes(n, start_price, up=0.8, down=0.5, rising=False),
        start=START - pd.Timedelta(minutes=15 * n), freq_min=15, pad=0.2,
    )


def uptrend_m5(n: int = 150, start_price: float = 2000.0) -> pd.DataFrame:
    return make_candles(
        zigzag_closes(n, start_price, up=0.5, down=0.3, up_run=4, down_run=2),
        start=START - pd.Timedelta(minutes=5 * n), freq_min=5, pad=0.15,
    )


def downtrend_m5(n: int = 150, start_price: float = 2040.0) -> pd.DataFrame:
    return make_candles(
        zigzag_closes(n, start_price, up=0.5, down=0.3, up_run=4, down_run=2, rising=False),
        start=START - pd.Timedelta(minutes=5 * n), freq_min=5, pad=0.15,
    )


@pytest.fixture
def tuning() -> StrategyTuningConfig:
    # Strategy entry-logic tests use hand-crafted M1 candle sequences whose SL
    # swings are sized for M1 ATR; pin M1 geometry here. M5 geometry (the
    # production default) is covered in test_cost_and_hours.py.
    return StrategyTuningConfig(sl_timeframe="M1")


@pytest.fixture
def regime_cfg() -> RegimeConfig:
    return RegimeConfig()


@pytest.fixture
def daily_cfg() -> DailyGoalsConfig:
    return DailyGoalsConfig()


@pytest.fixture
def pm_cfg() -> PositionManagementConfig:
    return PositionManagementConfig()


@pytest.fixture
def hp_cfg() -> HighPrecisionConfig:
    return HighPrecisionConfig()


@pytest.fixture
def mo_cfg() -> MomentumConfig:
    return MomentumConfig()
