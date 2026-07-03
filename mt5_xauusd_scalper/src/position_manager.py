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


def evaluate_position(
    pos: ManagedPosition,
    current_price: float,
    now: datetime,
    cfg: PositionManagementConfig,
) -> list[PositionAction]:
    """Return the list of actions due for this position, highest priority first."""
    actions: list[PositionAction] = []
    r = pos.r_multiple(current_price)
    age_minutes = (now - pos.opened_at).total_seconds() / 60.0

    # Hard cap: never hold a scalp past max duration.
    if age_minutes >= cfg.max_trade_duration_minutes:
        actions.append(
            PositionAction(
                PositionActionType.MAX_DURATION_EXIT,
                f"open {age_minutes:.1f}min >= max {cfg.max_trade_duration_minutes}min",
            )
        )
        return actions

    # Stale trade: not working after time_exit_minutes.
    if age_minutes >= cfg.time_exit_minutes and r < cfg.time_exit_min_r:
        actions.append(
            PositionAction(
                PositionActionType.TIME_EXIT,
                f"open {age_minutes:.1f}min with only {r:.2f}R (< {cfg.time_exit_min_r}R)",
            )
        )
        return actions

    if not pos.breakeven_done and r >= cfg.move_to_breakeven_at_r:
        actions.append(
            PositionAction(
                PositionActionType.MOVE_BREAKEVEN,
                f"reached {r:.2f}R >= {cfg.move_to_breakeven_at_r}R",
                new_sl=pos.entry,
            )
        )

    if (
        cfg.partial_close_enabled
        and not pos.partial_done
        and r >= cfg.partial_close_at_r
    ):
        actions.append(
            PositionAction(
                PositionActionType.PARTIAL_CLOSE,
                f"reached {r:.2f}R >= {cfg.partial_close_at_r}R",
                close_fraction=cfg.partial_close_percent / 100.0,
            )
        )

    return actions


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
