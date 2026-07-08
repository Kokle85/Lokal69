"""Position sizing and hard pre-trade validation. Deny by default."""
from __future__ import annotations

import math

from loguru import logger

from config_loader import Settings
from models import LotResult, Signal, SymbolSpec


def calculate_lot(
    balance: float,
    risk_percent: float,
    entry: float,
    stop_loss: float,
    spec: SymbolSpec,
) -> LotResult:
    """Size the lot so a stop-out loses ~risk_percent of balance.

    Never rounds UP: the floored lot risks at most the requested amount.
    """
    if balance <= 0:
        return LotResult(False, 0.0, "account balance unavailable or non-positive")
    risk_usd = balance * risk_percent / 100.0
    if risk_usd <= 0:
        return LotResult(False, 0.0, "risk per trade computes to zero")
    sl_distance = abs(entry - stop_loss)
    if sl_distance <= 0:
        return LotResult(False, 0.0, "stop loss distance is zero - trade forbidden")
    if spec.tick_size <= 0 or spec.tick_value <= 0:
        return LotResult(False, 0.0, "invalid symbol tick size/value from broker")

    loss_per_lot = (sl_distance / spec.tick_size) * spec.tick_value
    raw_lot = risk_usd / loss_per_lot
    step = spec.volume_step if spec.volume_step > 0 else 0.01
    lot = math.floor(raw_lot / step + 1e-9) * step
    lot = round(lot, 8)

    if lot < spec.volume_min:
        return LotResult(
            False, 0.0,
            f"lot {lot} below broker minimum {spec.volume_min} "
            f"(risk ${risk_usd:.2f} too small for SL distance {sl_distance:.2f})",
        )
    if lot > spec.volume_max:
        return LotResult(False, 0.0, f"lot {lot} above broker maximum {spec.volume_max}")

    actual_loss = loss_per_lot * lot
    return LotResult(True, lot, "ok", actual_loss)


def validate_signal(signal: Signal, cfg: Settings) -> tuple[bool, str]:
    """Structural checks every signal must pass before sizing/execution."""
    if signal.stop_loss <= 0 or signal.take_profit <= 0:
        return False, "signal missing SL or TP"
    risk = signal.risk_distance
    if risk <= 0:
        return False, "risk distance is zero"
    if signal.atr <= 0:
        return False, "ATR unavailable for validation"
    ratio = risk / signal.atr
    if ratio < cfg.min_stop_distance_atr:
        return False, f"stop distance {ratio:.2f} ATR below minimum {cfg.min_stop_distance_atr}"
    if ratio > cfg.max_stop_distance_atr:
        return False, f"stop distance {ratio:.2f} ATR above maximum {cfg.max_stop_distance_atr}"
    # TP must actually sit rr x risk away (guards against construction bugs)
    expected_tp_dist = cfg.risk_reward_ratio * risk
    tp_dist = abs(signal.take_profit - signal.entry_price)
    if abs(tp_dist - expected_tp_dist) > 0.01 * expected_tp_dist + 1e-9:
        return False, "TP is not at the configured risk_reward_ratio"
    return True, "ok"


def size_signal(signal: Signal, balance: float, spec: SymbolSpec,
                cfg: Settings) -> LotResult:
    ok, reason = validate_signal(signal, cfg)
    if not ok:
        logger.warning("Signal rejected by risk manager: {}", reason)
        return LotResult(False, 0.0, reason)
    result = calculate_lot(balance, cfg.risk_per_trade_percent,
                           signal.entry_price, signal.stop_loss, spec)
    if not result.ok:
        logger.warning("Lot sizing failed: {}", result.reason)
    return result
