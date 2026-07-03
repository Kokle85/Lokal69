"""Open-position management: breakeven, partial close, time exits.

`evaluate_position` is pure decision logic (unit-testable); the live bot
applies the returned actions through the execution engine.
"""
from __future__ import annotations

from datetime import datetime

from loguru import logger

from config import PositionManagementConfig
from models import (
    Direction,
    ManagedPosition,
    PositionAction,
    PositionActionType,
)


def decide_actions(
    favorable_r: float,
    current_r: float,
    minutes_open: float,
    breakeven_done: bool,
    partial_done: bool,
    entry_price: float,
    cfg: PositionManagementConfig,
) -> list[PositionAction]:
    """Single source of truth for position-management decisions.

    Both the live bot and the backtester call this with the SAME config so the
    two paths cannot drift apart. `favorable_r` is the best R reached so far
    (drives breakeven/partial); `current_r` is the R at the current price
    (drives the time exit). Live passes favorable_r == current_r == current R;
    the backtester passes the bar's best-excursion R and its close R.
    """
    actions: list[PositionAction] = []

    if minutes_open >= cfg.max_trade_duration_minutes:
        return [
            PositionAction(
                PositionActionType.MAX_DURATION_EXIT,
                f"open {minutes_open:.1f}min >= max {cfg.max_trade_duration_minutes}min",
            )
        ]

    if minutes_open >= cfg.time_exit_minutes and current_r < cfg.time_exit_min_r:
        return [
            PositionAction(
                PositionActionType.TIME_EXIT,
                f"open {minutes_open:.1f}min with only {current_r:.2f}R (< {cfg.time_exit_min_r}R)",
            )
        ]

    if not breakeven_done and favorable_r >= cfg.move_to_breakeven_at_r:
        actions.append(
            PositionAction(
                PositionActionType.MOVE_BREAKEVEN,
                f"reached {favorable_r:.2f}R >= {cfg.move_to_breakeven_at_r}R",
                new_sl=entry_price,
            )
        )

    if cfg.partial_close_enabled and not partial_done and favorable_r >= cfg.partial_close_at_r:
        actions.append(
            PositionAction(
                PositionActionType.PARTIAL_CLOSE,
                f"reached {favorable_r:.2f}R >= {cfg.partial_close_at_r}R",
                close_fraction=cfg.partial_close_percent / 100.0,
            )
        )

    return actions


def evaluate_position(
    pos: ManagedPosition,
    current_price: float,
    now: datetime,
    cfg: PositionManagementConfig,
) -> list[PositionAction]:
    """Live wrapper: derive R and age from the position, then apply shared rules."""
    r = pos.r_multiple(current_price)
    age_minutes = (now - pos.opened_at).total_seconds() / 60.0
    return decide_actions(
        favorable_r=r,
        current_r=r,
        minutes_open=age_minutes,
        breakeven_done=pos.breakeven_done,
        partial_done=pos.partial_done,
        entry_price=pos.entry,
        cfg=cfg,
    )


def apply_action_to_state(pos: ManagedPosition, action: PositionAction) -> None:
    """Update the bot-side position state after an action succeeded on the broker."""
    if action.action is PositionActionType.MOVE_BREAKEVEN and action.new_sl is not None:
        pos.sl = action.new_sl
        pos.breakeven_done = True
        logger.info("Position {}: SL moved to breakeven {}", pos.ticket, pos.sl)
    elif action.action is PositionActionType.PARTIAL_CLOSE:
        pos.lot = round(pos.lot * (1.0 - action.close_fraction), 8)
        pos.partial_done = True
        logger.info("Position {}: partial close, remaining lot {}", pos.ticket, pos.lot)
