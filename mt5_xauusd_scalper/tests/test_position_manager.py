"""Position manager tests: breakeven, partial close, time exits."""
from datetime import datetime, timedelta, timezone

import pytest

from models import Direction, ManagedPosition, PositionActionType, StrategyName
from position_manager import apply_action_to_state, evaluate_position

NOW = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)


def make_position(direction=Direction.BUY, entry=2000.0, sl=1999.5, opened_minutes_ago=1.0) -> ManagedPosition:
    return ManagedPosition(
        ticket=1, symbol="XAUUSD", direction=direction, entry=entry, sl=sl,
        tp=entry + (entry - sl) if direction is Direction.BUY else entry - (sl - entry),
        lot=0.4, initial_lot=0.4, initial_sl=sl, risk_usd=75.0,
        opened_at=NOW - timedelta(minutes=opened_minutes_ago),
        strategy=StrategyName.HIGH_PRECISION,
    )


def test_breakeven_trigger(pm_cfg):
    pos = make_position()  # risk distance 0.5, breakeven at 0.7R = +0.35
    actions = evaluate_position(pos, 2000.36, NOW, pm_cfg)
    kinds = [a.action for a in actions]
    assert PositionActionType.MOVE_BREAKEVEN in kinds
    be = next(a for a in actions if a.action is PositionActionType.MOVE_BREAKEVEN)
    assert be.new_sl == pos.entry


def test_breakeven_not_triggered_early(pm_cfg):
    pos = make_position()
    actions = evaluate_position(pos, 2000.30, NOW, pm_cfg)  # 0.6R < 0.7R
    assert all(a.action is not PositionActionType.MOVE_BREAKEVEN for a in actions)


def test_partial_close_trigger(pm_cfg):
    pos = make_position()  # partial at 1.0R = +0.50
    actions = evaluate_position(pos, 2000.50, NOW, pm_cfg)
    partial = next(a for a in actions if a.action is PositionActionType.PARTIAL_CLOSE)
    assert partial.close_fraction == 0.5


def test_partial_close_disabled(pm_cfg):
    pm_cfg.partial_close_enabled = False
    pos = make_position()
    actions = evaluate_position(pos, 2000.50, NOW, pm_cfg)
    assert all(a.action is not PositionActionType.PARTIAL_CLOSE for a in actions)


def test_partial_close_only_once(pm_cfg):
    pos = make_position()
    pos.partial_done = True
    actions = evaluate_position(pos, 2000.60, NOW, pm_cfg)
    assert all(a.action is not PositionActionType.PARTIAL_CLOSE for a in actions)


def test_time_exit_trigger(pm_cfg):
    # open 9 minutes (> 8) with profit below 0.25R -> close
    pos = make_position(opened_minutes_ago=9.0)
    actions = evaluate_position(pos, 2000.05, NOW, pm_cfg)  # 0.1R
    assert actions[0].action is PositionActionType.TIME_EXIT


def test_no_time_exit_when_trade_working(pm_cfg):
    pos = make_position(opened_minutes_ago=9.0)
    actions = evaluate_position(pos, 2000.20, NOW, pm_cfg)  # 0.4R >= 0.25R
    assert all(a.action is not PositionActionType.TIME_EXIT for a in actions)


def test_max_duration_exit(pm_cfg):
    pos = make_position(opened_minutes_ago=13.0)
    actions = evaluate_position(pos, 2000.20, NOW, pm_cfg)
    assert actions[0].action is PositionActionType.MAX_DURATION_EXIT


def test_sell_position_r_multiple(pm_cfg):
    pos = make_position(direction=Direction.SELL, entry=2000.0, sl=2000.5)
    actions = evaluate_position(pos, 1999.64, NOW, pm_cfg)  # +0.72R for a sell
    assert any(a.action is PositionActionType.MOVE_BREAKEVEN for a in actions)


def test_apply_actions_updates_state(pm_cfg):
    pos = make_position()
    actions = evaluate_position(pos, 2000.55, NOW, pm_cfg)
    for action in actions:
        apply_action_to_state(pos, action)
    assert pos.breakeven_done
    assert pos.sl == pos.entry
    assert pos.partial_done
    assert pos.lot == pytest.approx(0.2)
