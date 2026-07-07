"""Thin, safe wrapper around the MetaTrader5 package.

The package only exists on Windows; everything import-sensitive is isolated
here so the rest of the codebase (and the test suite) never touches it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import pandas as pd
from loguru import logger

from config import MT5Config, TradingConfig
from models import SymbolSpec

try:  # pragma: no cover - exercised only on Windows with the terminal installed
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None  # type: ignore[assignment]

TIMEFRAME_MAP = {"M1": 1, "M5": 5, "M15": 15}


class MT5Error(Exception):
    pass


class MT5Connector:
    def __init__(self, mt5_cfg: MT5Config, trading_cfg: TradingConfig) -> None:
        self.cfg = mt5_cfg
        self.trading = trading_cfg
        self.connected = False
        self.symbol: str = ""

    # ------------------------------------------------------------ lifecycle

    def connect(self) -> None:
        if mt5 is None:
            raise MT5Error(
                "The MetaTrader5 Python package is not installed (Windows only). "
                "Install the MT5 terminal and `pip install MetaTrader5`, then retry."
            )
        kwargs: dict[str, Any] = {}
        if self.cfg.path:
            kwargs["path"] = self.cfg.path
        if not mt5.initialize(**kwargs):
            code, msg = mt5.last_error()
            raise MT5Error(
                f"MT5 initialize failed ({code}: {msg}). "
                "Is the MetaTrader 5 terminal installed and able to start?"
            )
        if self.cfg.login and self.cfg.password and self.cfg.server:
            if not mt5.login(self.cfg.login, password=self.cfg.password, server=self.cfg.server):
                code, msg = mt5.last_error()
                mt5.shutdown()
                raise MT5Error(f"MT5 login failed for account {self.cfg.login} ({code}: {msg})")
        self.connected = True
        info = mt5.account_info()
        if info:
            logger.info(
                "Connected to MT5 | account {} | server {} | balance {} | demo={}",
                info.login, info.server, info.balance, self.is_demo_account(),
            )

    def shutdown(self) -> None:
        if mt5 is not None and self.connected:
            mt5.shutdown()
            self.connected = False
            logger.info("MT5 connection closed")

    def is_alive(self) -> bool:
        if mt5 is None or not self.connected:
            return False
        return mt5.terminal_info() is not None

    def reconnect(self) -> bool:
        logger.warning("MT5 connection lost - attempting reconnect")
        try:
            self.shutdown()
            self.connect()
            if self.symbol:
                self.resolve_symbol()
            return True
        except MT5Error as exc:
            logger.error("Reconnect failed: {}", exc)
            return False

    # ------------------------------------------------------------ account

    def account_info(self) -> Any:
        info = mt5.account_info()
        if info is None:
            raise MT5Error("account_info() returned None - terminal not connected?")
        return info

    def is_demo_account(self) -> bool:
        return self.account_info().trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO

    # ------------------------------------------------------------ symbol

    def resolve_symbol(self) -> str:
        """Find the broker's name for the configured instrument among its aliases.

        Only the instrument's own symbol + aliases are tried — never an unrelated
        fallback — so a missing index CFD fails loudly instead of silently
        trading gold.
        """
        aliases = list(dict.fromkeys([self.trading.symbol, *self.trading.allowed_symbol_aliases]))
        for candidate in aliases:
            info = mt5.symbol_info(candidate)
            if info is None:
                continue
            if not mt5.symbol_select(candidate, True):
                logger.warning("symbol_select failed for {}", candidate)
                continue
            self.symbol = candidate
            logger.info("Symbol validated: {} (spread {} pt)", candidate, info.spread)
            return candidate
        raise MT5Error(
            f"Instrument '{self.trading.symbol}' not found in MT5. Tried: {aliases}. "
            "Open the symbol in Market Watch and set the exact broker name in "
            "config.yaml (instruments[].symbol / aliases)."
        )

    def symbol_spec(self) -> SymbolSpec:
        info = mt5.symbol_info(self.symbol)
        if info is None:
            raise MT5Error(f"symbol_info({self.symbol}) returned None")
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

    def tick(self) -> Any:
        t = mt5.symbol_info_tick(self.symbol)
        if t is None:
            raise MT5Error(f"symbol_info_tick({self.symbol}) returned None")
        return t

    def spread_points(self) -> float:
        t = self.tick()
        spec = self.symbol_spec()
        return spec.spread_points(t.bid, t.ask)

    # ------------------------------------------------------------ data

    def rates(self, timeframe: str, count: int, start_pos: int = 0) -> pd.DataFrame:
        tf_map = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
        }
        if timeframe not in tf_map:
            raise MT5Error(f"Unsupported timeframe {timeframe}")
        raw = mt5.copy_rates_from_pos(self.symbol, tf_map[timeframe], start_pos, count)
        if raw is None or len(raw) == 0:
            raise MT5Error(f"copy_rates_from_pos returned no data for {self.symbol} {timeframe}")
        df = pd.DataFrame(raw)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df

    def rates_range(self, timeframe: str, days: int) -> pd.DataFrame:
        """Pull history by DATE RANGE (copy_rates_range) - the reliable way to
        fetch long spans. Unlike copy_rates_from_pos it is not bound by a bar
        count, so 360+ days come back in one call if the terminal has cached the
        history (scroll the M1 chart back / raise 'Max bars in chart')."""
        from datetime import datetime, timedelta, timezone

        tf_map = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
        }
        if timeframe not in tf_map:
            raise MT5Error(f"Unsupported timeframe {timeframe}")
        utc_to = datetime.now(timezone.utc) + timedelta(days=1)  # pad so the last bar is included
        utc_from = utc_to - timedelta(days=days + 1)
        raw = mt5.copy_rates_range(self.symbol, tf_map[timeframe], utc_from, utc_to)
        if raw is None or len(raw) == 0:
            raise MT5Error(f"copy_rates_range returned no data for {self.symbol} {timeframe}")
        df = pd.DataFrame(raw)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df

    # ------------------------------------------------------------ trading state

    def open_positions(self) -> list[Any]:
        positions = mt5.positions_get(symbol=self.symbol)
        if positions is None:
            # None = API error, NOT "no positions". Treating it as empty makes
            # the bot believe its open trade closed and abandon management.
            raise MT5Error(f"positions_get failed: {mt5.last_error()}")
        return list(positions)

    def pending_orders(self) -> list[Any]:
        orders = mt5.orders_get(symbol=self.symbol)
        if orders is None:
            raise MT5Error(f"orders_get failed: {mt5.last_error()}")
        return list(orders)

    def today_deals(self, day_start: datetime) -> list[Any]:
        # Deal stamps are broker-server time; pad the window generously on both
        # sides (naive/local bounds used to miss just-closed deals entirely).
        from datetime import timedelta, timezone as _tz

        start = day_start if day_start.tzinfo else day_start.replace(tzinfo=_tz.utc)
        deals = mt5.history_deals_get(
            start - timedelta(days=1), datetime.now(_tz.utc) + timedelta(days=2)
        )
        if deals is None:
            raise MT5Error(f"history_deals_get failed: {mt5.last_error()}")
        return [d for d in deals if d.symbol == self.symbol and d.magic == self.trading.magic_number]

    # ------------------------------------------------------------ orders

    def order_check(self, request: dict[str, Any]) -> Any:
        return mt5.order_check(request)

    def order_send(self, request: dict[str, Any]) -> Any:
        return mt5.order_send(request)
