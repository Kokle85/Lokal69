"""Candle fetching and new-closed-candle detection."""
from __future__ import annotations

from typing import Optional

import pandas as pd
from loguru import logger

from mt5_connector import MT5Connector

# Bars fetched per timeframe: enough for EMA50/ATR warm-up plus structure lookbacks.
BAR_COUNTS = {"M1": 400, "M5": 200, "M15": 150}


class MarketData:
    def __init__(self, connector: MT5Connector) -> None:
        self.connector = connector
        self._last_closed_m1: Optional[pd.Timestamp] = None

    def fetch_closed(self, timeframe: str) -> pd.DataFrame:
        """Fetch bars and drop the still-forming candle so only closed bars remain."""
        count = BAR_COUNTS.get(timeframe, 200)
        df = self.connector.rates(timeframe, count + 1)
        return df.iloc[:-1].reset_index(drop=True)

    def new_m1_candle(self, m1: pd.DataFrame) -> bool:
        """True exactly once per newly closed M1 candle (prevents duplicate signals)."""
        if m1.empty:
            return False
        latest = m1["time"].iloc[-1]
        if self._last_closed_m1 is None or latest > self._last_closed_m1:
            self._last_closed_m1 = latest
            logger.debug("New closed M1 candle: {}", latest)
            return True
        return False
