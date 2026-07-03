"""Tests for the shared decide_actions() core used by both live and backtest."""
from models import PositionActionType
from position_manager import decide_actions


def test_max_duration_wins_over_everything(pm_cfg):
    actions = decide_actions(
        favorable_r=2.0, current_r=2.0, minutes_open=pm_cfg.max_trade_duration_minutes,
        breakeven_done=False, partial_done=False, entry_price=2000.0, cfg=pm_cfg,
    )
    assert len(actions) == 1
    assert actions[0].action is PositionActionType.MAX_DURATION_EXIT


def test_time_exit_uses_current_r_not_favorable(pm_cfg):
    # Favorable R was high, but current R is low and time is up -> time exit.
    actions = decide_actions(
        favorable_r=1.0, current_r=0.1, minutes_open=pm_cfg.time_exit_minutes,
        breakeven_done=False, partial_done=False, entry_price=2000.0, cfg=pm_cfg,
    )
    assert actions[0].action is PositionActionType.TIME_EXIT


def test_no_time_exit_when_current_r_healthy(pm_cfg):
    actions = decide_actions(
        favorable_r=0.8, current_r=0.5, minutes_open=pm_cfg.time_exit_minutes,
        breakeven_done=False, partial_done=False, entry_price=2000.0, cfg=pm_cfg,
    )
    assert all(a.action is not PositionActionType.TIME_EXIT for a in actions)


def test_breakeven_and_partial_use_favorable_r(pm_cfg):
    actions = decide_actions(
        favorable_r=1.0, current_r=0.4, minutes_open=1.0,
        breakeven_done=False, partial_done=False, entry_price=2000.0, cfg=pm_cfg,
    )
    kinds = {a.action for a in actions}
    assert PositionActionType.MOVE_BREAKEVEN in kinds
    assert PositionActionType.PARTIAL_CLOSE in kinds
    be = next(a for a in actions if a.action is PositionActionType.MOVE_BREAKEVEN)
    assert be.new_sl == 2000.0


def test_already_done_flags_suppress_actions(pm_cfg):
    actions = decide_actions(
        favorable_r=1.5, current_r=1.5, minutes_open=1.0,
        breakeven_done=True, partial_done=True, entry_price=2000.0, cfg=pm_cfg,
    )
    assert actions == []


def test_live_wrapper_matches_core(pm_cfg):
    """evaluate_position must produce the same decision as decide_actions."""
    from datetime import datetime, timedelta, timezone

    from models import Direction, ManagedPosition, StrategyName
    from position_manager import evaluate_position

    now = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
    pos = ManagedPosition(
        ticket=1, symbol="XAUUSD", direction=Direction.BUY, entry=2000.0, sl=1999.5, tp=2000.5,
        lot=0.4, initial_lot=0.4, initial_sl=1999.5, risk_usd=75.0,
        opened_at=now - timedelta(minutes=1), strategy=StrategyName.HIGH_PRECISION,
    )
    # price at +0.36 -> r = 0.72, past breakeven 0.7
    live = evaluate_position(pos, 2000.36, now, pm_cfg)
    r = pos.r_multiple(2000.36)
    core = decide_actions(r, r, 1.0, False, False, pos.entry, pm_cfg)
    assert [a.action for a in live] == [a.action for a in core]
