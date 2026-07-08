"""Thin, defensive wrapper around the MetaTrader5 package (Windows-only).

Every None returned by the MT5 API is converted into a raised MT5ClientError:
None from positions_get means "API error", never "no positions" - treating
those the same is how bots abandon live trades.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from loguru import logger

from config_loader import MT5Config
from models import SymbolSpec

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:  # non-Windows: backtest/signal-from-CSV still work
    mt5 = None
    MT5_AVAILABLE = False


class MT5ClientError(Exception):
    pass


class MT5Client:
    def __init__(self, cfg: MT5Config, symbol: str) -> None:
        if not MT5_AVAILABLE:
            raise MT5ClientError(
                "MetaTrader5 package is not available. Live/signal mode needs "
                "Windows with MT5 installed; backtest mode works anywhere."
            )
        self.cfg = cfg
        self.symbol = symbol
        self.connected = False

    # ------------------------------------------------------------ lifecycle

    def connect(self) -> None:
        kwargs: dict[str, Any] = {}
        if self.cfg.path:
            kwargs["path"] = self.cfg.path
        if not mt5.initialize(**kwargs):
            raise MT5ClientError(f"MT5 initialize failed: {mt5.last_error()}")
        if self.cfg.login and self.cfg.password and self.cfg.server:
            if not mt5.login(self.cfg.login, password=self.cfg.password,
                             server=self.cfg.server):
                mt5.shutdown()
                raise MT5ClientError(f"MT5 login failed: {mt5.last_error()}")
        info = mt5.account_info()
        if info is None:
            mt5.shutdown()
            raise MT5ClientError("account_info returned None after connect")
        self.connected = True
        logger.info("Connected to MT5 | account {} | server {} | balance {} | demo={}",
                    info.login, info.server, info.balance,
                    info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO)

    def shutdown(self) -> None:
        if self.connected:
            mt5.shutdown()
            self.connected = False

    def is_alive(self) -> bool:
        return mt5.terminal_info() is not None

    def is_demo(self) -> bool:
        info = mt5.account_info()
        if info is None:
            raise MT5ClientError("account_info returned None")
        return info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO

    def balance(self) -> float:
        info = mt5.account_info()
        if info is None:
            raise MT5ClientError("account_info returned None")
        return float(info.balance)

    # ------------------------------------------------------------ symbol

    def resolve_symbol(self) -> None:
        info = mt5.symbol_info(self.symbol)
        if info is None or not mt5.symbol_select(self.symbol, True):
            raise MT5ClientError(
                f"symbol {self.symbol} not found in Market Watch - check the "
                f"broker's exact name (GOLD/XAUUSDm/...) and set it in config."
            )

    def spec(self) -> SymbolSpec:
        info = mt5.symbol_info(self.symbol)
        if info is None:
            raise MT5ClientError(f"symbol_info({self.symbol}) returned None")
        return SymbolSpec(
            name=self.symbol,
            point=info.point,
            tick_size=info.trade_tick_size or info.point,
            tick_value=info.trade_tick_value,
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
            digits=info.digits,
            trade_allowed=info.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL,
            filling_mode=getattr(info, "filling_mode", 0),
        )

    def spread_points(self) -> float:
        tick = mt5.symbol_info_tick(self.symbol)
        info = mt5.symbol_info(self.symbol)
        if tick is None or info is None or info.point <= 0:
            raise MT5ClientError("tick/symbol info unavailable for spread")
        return (tick.ask - tick.bid) / info.point

    def tick(self) -> Any:
        t = mt5.symbol_info_tick(self.symbol)
        if t is None:
            raise MT5ClientError(f"symbol_info_tick({self.symbol}) returned None")
        return t

    # ------------------------------------------------------------ data

    def m5_candles(self, count: int = 600) -> pd.DataFrame:
        """Last `count` CLOSED M5 candles. Bar labels are broker-server time -
        keep every time comparison (sessions!) on that same clock."""
        raw = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M5, 1, count)
        if raw is None or len(raw) == 0:
            raise MT5ClientError(f"copy_rates_from_pos returned no M5 data: {mt5.last_error()}")
        df = pd.DataFrame(raw)
        df["time"] = pd.to_datetime(df["time"], unit="s")  # naive = broker time
        df = df.rename(columns={"tick_volume": "volume"})
        return df[["time", "open", "high", "low", "close", "volume"]]

    def m5_range(self, days: int) -> pd.DataFrame:
        utc_to = datetime.now(timezone.utc) + timedelta(days=1)
        raw = mt5.copy_rates_range(self.symbol, mt5.TIMEFRAME_M5,
                                   utc_to - timedelta(days=days + 1), utc_to)
        if raw is None or len(raw) == 0:
            raise MT5ClientError(f"copy_rates_range returned no data: {mt5.last_error()}")
        df = pd.DataFrame(raw)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df.rename(columns={"tick_volume": "volume"})
        return df[["time", "open", "high", "low", "close", "volume"]]

    # ------------------------------------------------------------ state

    def open_positions(self) -> list[Any]:
        positions = mt5.positions_get(symbol=self.symbol)
        if positions is None:
            raise MT5ClientError(f"positions_get failed: {mt5.last_error()}")
        return [p for p in positions if p.magic == self.cfg.magic_number]

    # ------------------------------------------------------------ orders

    def order_check(self, request: dict[str, Any]) -> Any:
        return mt5.order_check(request)

    def order_send(self, request: dict[str, Any]) -> Any:
        return mt5.order_send(request)
