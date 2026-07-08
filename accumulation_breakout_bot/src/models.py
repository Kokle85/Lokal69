"""Core data types for the Accumulation Breakout Bot."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class EntryMode(str, Enum):
    DIRECT_BREAKOUT = "DIRECT_BREAKOUT"
    BREAKOUT_RETEST = "BREAKOUT_RETEST"


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Zone:
    """A validated accumulation zone."""

    high: float
    low: float
    upper_touches: int
    lower_touches: int
    start_time: datetime
    end_time: datetime
    atr: float

    @property
    def size(self) -> float:
        return self.high - self.low

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0


@dataclass
class ZoneResult:
    """Outcome of a zone scan: either a zone or the reasons none qualified."""

    zone: Optional[Zone] = None
    rejections: list[str] = field(default_factory=list)


@dataclass
class Signal:
    """A fully specified trade the operator (or executor) can act on."""

    timestamp: datetime
    symbol: str
    timeframe: str
    entry_mode: EntryMode
    direction: Direction
    zone: Zone
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_reward_ratio: float
    atr: float
    ema_value: float = 0.0
    session_name: str = ""
    spread_points: float = 0.0
    lot_size: float = 0.0
    reason_for_entry: str = ""

    @property
    def risk_distance(self) -> float:
        return abs(self.entry_price - self.stop_loss)


@dataclass(frozen=True)
class SymbolSpec:
    """Broker contract details needed for lot sizing and order math."""

    name: str
    point: float
    tick_size: float
    tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    digits: int
    trade_allowed: bool = True
    filling_mode: int = 0  # SYMBOL_FILLING_* bitmask (1=FOK, 2=IOC); 0=unknown


@dataclass
class LotResult:
    ok: bool
    lot: float
    reason: str
    loss_at_sl_usd: float = 0.0


@dataclass
class BacktestTrade:
    open_time: datetime
    close_time: datetime
    direction: Direction
    entry: float
    stop_loss: float
    take_profit: float
    exit_price: float
    r_result: float          # -1.0 on SL, +rr on TP
    result: str              # WIN | LOSS | TIMEOUT
    session_name: str
    entry_mode: EntryMode
    zone_high: float
    zone_low: float


@dataclass
class BacktestReport:
    label: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate_pct: float = 0.0
    average_r: float = 0.0
    total_r: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_r: float = 0.0
    long_trades: int = 0
    short_trades: int = 0
    best_trade_r: float = 0.0
    worst_trade_r: float = 0.0
    max_consecutive_losses: int = 0
    max_consecutive_wins: int = 0
    trades: list[BacktestTrade] = field(default_factory=list)

    def as_row(self) -> dict:
        return {
            "label": self.label,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate_pct": round(self.win_rate_pct, 2),
            "average_r": round(self.average_r, 3),
            "total_r": round(self.total_r, 2),
            "profit_factor": round(self.profit_factor, 3),
            "max_drawdown_r": round(self.max_drawdown_r, 2),
            "long_trades": self.long_trades,
            "short_trades": self.short_trades,
            "best_trade_r": round(self.best_trade_r, 2),
            "worst_trade_r": round(self.worst_trade_r, 2),
            "max_consecutive_losses": self.max_consecutive_losses,
            "max_consecutive_wins": self.max_consecutive_wins,
        }
