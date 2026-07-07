"""Shared typed models used across the bot."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class BotMode(str, Enum):
    SIGNAL_ONLY = "SIGNAL_ONLY"
    SEMI_AUTO = "SEMI_AUTO"
    DEMO_AUTO = "DEMO_AUTO"
    LIVE_AUTO = "LIVE_AUTO"


class Regime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    CHOPPY = "CHOPPY"
    SPIKE = "SPIKE"
    DEAD_MARKET = "DEAD_MARKET"


NO_TRADE_REGIMES = frozenset({Regime.CHOPPY, Regime.SPIKE, Regime.DEAD_MARKET})


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class StrategyName(str, Enum):
    HIGH_PRECISION = "HIGH_PRECISION_SCALP"
    MOMENTUM = "MOMENTUM_SCALP"
    ORB = "OPENING_RANGE_BREAKOUT"


class SignalStatus(str, Enum):
    NEW = "NEW"
    SENT = "SENT"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class CloseReason(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TIME_EXIT = "TIME_EXIT"
    MAX_DURATION = "MAX_DURATION"
    PARTIAL = "PARTIAL"
    KILL = "KILL"
    MANUAL = "MANUAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SymbolSpec:
    """Broker properties needed for lot sizing and price math."""

    name: str
    point: float
    tick_size: float
    tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    digits: int
    trade_allowed: bool = True
    # SYMBOL_FILLING_* bitmask from the broker (1=FOK, 2=IOC); 0 = unknown.
    filling_mode: int = 0

    def spread_points(self, bid: float, ask: float) -> float:
        return (ask - bid) / self.point if self.point > 0 else 0.0


@dataclass
class RegimeResult:
    regime: Regime
    reasons: list[str] = field(default_factory=list)

    @property
    def tradeable(self) -> bool:
        return self.regime not in NO_TRADE_REGIMES


@dataclass
class Signal:
    symbol: str
    direction: Direction
    strategy: StrategyName
    regime: Regime
    entry: float
    sl: float
    tp: float
    rr: float
    score: int
    max_score: int
    setup_reason: str
    spread_points: float
    created_at: datetime
    risk_usd: float = 0.0
    lot: float = 0.0
    mode: BotMode = BotMode.SIGNAL_ONLY
    signal_id: Optional[int] = None
    candle_time: Optional[datetime] = None
    trade_number: Optional[int] = None  # 1-based number of this trade today (sniper mode)
    max_trades_today: int = 0
    note: str = ""

    @property
    def sl_distance(self) -> float:
        return abs(self.entry - self.sl)

    @property
    def expected_r_score(self) -> float:
        """Score used by the strategy selector: reward weighted by setup quality."""
        if self.max_score <= 0:
            return 0.0
        return self.rr * (self.score / self.max_score)


@dataclass
class GovernorDecision:
    allowed: bool
    risk_usd: float
    reason: str
    lock_triggered: bool = False


@dataclass
class LockEvent:
    reason: str
    message: str


class PositionActionType(str, Enum):
    MOVE_BREAKEVEN = "MOVE_BREAKEVEN"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    TIME_EXIT = "TIME_EXIT"
    MAX_DURATION_EXIT = "MAX_DURATION_EXIT"


@dataclass
class PositionAction:
    action: PositionActionType
    reason: str
    close_fraction: float = 0.0  # for PARTIAL_CLOSE
    new_sl: Optional[float] = None  # for MOVE_BREAKEVEN


@dataclass
class ManagedPosition:
    """Bot-side view of the single open position."""

    ticket: int
    symbol: str
    direction: Direction
    entry: float
    sl: float
    tp: float
    lot: float
    initial_lot: float
    initial_sl: float
    risk_usd: float
    opened_at: datetime
    strategy: StrategyName
    trade_id: Optional[int] = None
    breakeven_done: bool = False
    partial_done: bool = False

    @property
    def risk_distance(self) -> float:
        """Distance of the original stop, used as the 1R reference even after SL moves."""
        return abs(self.entry - self.initial_sl)

    def r_multiple(self, current_price: float) -> float:
        if self.risk_distance <= 0:
            return 0.0
        move = current_price - self.entry if self.direction is Direction.BUY else self.entry - current_price
        return move / self.risk_distance


@dataclass
class OrderResult:
    ok: bool
    retcode: int
    comment: str
    ticket: int = 0
    price: float = 0.0
    volume: float = 0.0
