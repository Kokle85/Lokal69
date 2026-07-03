"""Lot sizing and hard per-trade risk validation."""
from __future__ import annotations

import math
from dataclasses import dataclass

from loguru import logger

from config import StrategyTuningConfig, TradingConfig
from models import Signal, SymbolSpec


@dataclass
class LotResult:
    ok: bool
    lot: float
    reason: str
    loss_at_sl_usd: float = 0.0


@dataclass
class RiskCheck:
    ok: bool
    reason: str


def calculate_lot(
    risk_usd: float,
    entry: float,
    sl: float,
    spec: SymbolSpec,
    cap_to_max: bool = False,
) -> LotResult:
    """Size the position so a stop-out loses ~risk_usd, rounded DOWN to volume_step."""
    if risk_usd <= 0:
        return LotResult(False, 0.0, "risk_usd must be positive")
    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        return LotResult(False, 0.0, "SL distance is zero — trade without SL is forbidden")
    if spec.tick_size <= 0 or spec.tick_value <= 0:
        return LotResult(False, 0.0, "invalid symbol tick size/value")

    loss_per_lot = (sl_distance / spec.tick_size) * spec.tick_value
    raw_lot = risk_usd / loss_per_lot

    step = spec.volume_step if spec.volume_step > 0 else 0.01
    lot = math.floor(raw_lot / step + 1e-9) * step
    lot = round(lot, 8)

    if lot < spec.volume_min:
        return LotResult(
            False,
            0.0,
            f"calculated lot {lot} below broker minimum {spec.volume_min} "
            f"(risk ${risk_usd:.0f} too small for SL distance {sl_distance:.2f})",
        )
    if lot > spec.volume_max:
        if not cap_to_max:
            return LotResult(False, 0.0, f"calculated lot {lot} above broker maximum {spec.volume_max}")
        lot = spec.volume_max

    actual_loss = (sl_distance / spec.tick_size) * spec.tick_value * lot
    if actual_loss > risk_usd * 1.05:
        return LotResult(False, 0.0, f"rounded lot risks ${actual_loss:.2f} > allowed ${risk_usd:.2f}")
    return LotResult(True, lot, "ok", actual_loss)


def validate_sl_distance(
    entry: float, sl: float, atr_value: float, tuning: StrategyTuningConfig
) -> RiskCheck:
    if atr_value <= 0:
        return RiskCheck(False, "ATR unavailable for SL validation")
    distance = abs(entry - sl)
    if distance <= 0:
        return RiskCheck(False, "trade has no stop loss")
    ratio = distance / atr_value
    if ratio < tuning.min_sl_atr:
        return RiskCheck(False, f"SL distance {ratio:.2f} ATR below minimum {tuning.min_sl_atr}")
    if ratio > tuning.max_sl_atr:
        return RiskCheck(False, f"SL distance {ratio:.2f} ATR above maximum {tuning.max_sl_atr}")
    return RiskCheck(True, "ok")


class RiskManager:
    """Final gatekeeper for every order. Never bypass; deny by default."""

    def __init__(self, trading: TradingConfig, tuning: StrategyTuningConfig) -> None:
        self.trading = trading
        self.tuning = tuning

    def validate_signal(
        self,
        signal: Signal,
        atr_value: float,
        spread_points: float,
        open_positions: int,
        symbol_tradeable: bool,
    ) -> RiskCheck:
        if signal.sl <= 0 or signal.sl_distance <= 0:
            return RiskCheck(False, "signal has no stop loss")
        if not symbol_tradeable:
            return RiskCheck(False, "symbol is not tradeable")
        if open_positions >= self.trading.max_positions:
            return RiskCheck(False, f"max positions ({self.trading.max_positions}) already open")
        if spread_points > self.trading.max_spread_points:
            return RiskCheck(
                False,
                f"spread {spread_points:.0f}pt above limit {self.trading.max_spread_points:.0f}pt",
            )
        sl_check = validate_sl_distance(signal.entry, signal.sl, atr_value, self.tuning)
        if not sl_check.ok:
            return sl_check
        return RiskCheck(True, "ok")

    def size_position(self, signal: Signal, spec: SymbolSpec) -> LotResult:
        result = calculate_lot(signal.risk_usd, signal.entry, signal.sl, spec)
        if not result.ok:
            logger.warning("Lot sizing rejected: {}", result.reason)
        return result
