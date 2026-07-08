import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np
import pandas as pd
import pytest

from config_loader import Settings


@pytest.fixture
def cfg() -> Settings:
    """Baseline test settings: small warmup, filters off by default so each
    test enables exactly what it exercises."""
    return Settings(
        use_ema_filter=False,
        use_session_filter=False,
        zone_lookback_candles=30,
        atr_period=14,
    )


def make_candles(specs: list[tuple[float, float, float, float]],
                 start: str = "2026-01-05 08:00",
                 freq_minutes: int = 5) -> pd.DataFrame:
    """Build an OHLC frame from (open, high, low, close) tuples."""
    times = pd.date_range(pd.Timestamp(start), periods=len(specs),
                          freq=f"{freq_minutes}min")
    o, h, l, c = zip(*specs)
    return pd.DataFrame({"time": times, "open": o, "high": h, "low": l,
                         "close": c, "volume": np.full(len(specs), 100)})


def flat_zone_candles(n: int = 30, low: float = 2000.0, high: float = 2001.0,
                      upper_touches: int = 4, lower_touches: int = 4,
                      start: str = "2026-01-05 08:00") -> list[tuple]:
    """A clean sideways range with wick rejections at both boundaries.

    Regular candles have bodies around the middle with modest range (so ATR
    stays comparable to the zone size). Touch candles poke a wick at the
    boundary but close back inside.
    """
    mid = (low + high) / 2
    specs = []
    upper_slots = set(range(2, 2 + upper_touches * 3, 3))
    lower_slots = set(range(3, 3 + lower_touches * 3, 3))
    for i in range(n):
        if i in upper_slots:
            # upper wick touch: high at the boundary, close below it
            specs.append((mid, high, mid - 0.1, mid + 0.15))
        elif i in lower_slots:
            # lower wick touch: low at the boundary, close above it
            specs.append((mid, mid + 0.1, low, mid - 0.15))
        else:
            # regular candle: range wide enough that ATR stays comparable to
            # the zone size (so stop-distance/ATR bands behave realistically),
            # but highs/lows stay clear of the touch tolerance at the borders
            specs.append((mid - 0.1, mid + 0.35, mid - 0.35, mid + 0.1))
    return specs
