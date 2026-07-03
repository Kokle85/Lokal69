"""Daily Risk Governor tests, including the spec's scenario A-D behaviors."""
from datetime import date

import pytest

from daily_risk_governor import DailyRiskGovernor

DAY = date(2026, 1, 5)


@pytest.fixture
def governor(daily_cfg) -> DailyRiskGovernor:
    return DailyRiskGovernor(daily_cfg, DAY, starting_equity=25000.0)


def test_first_trade_uses_default_risk(governor):
    decision = governor.evaluate_new_trade(signal_score=9)
    assert decision.allowed
    assert decision.risk_usd == 75


def test_daily_target_lock(governor):
    governor.on_trade_closed(120.0)
    events = governor.on_trade_closed(120.0)  # realized 240 >= 200 -> lock (scenario B)
    assert governor.state.locked
    assert governor.state.daily_target_hit
    assert any("target" in e.message.lower() for e in events)
    decision = governor.evaluate_new_trade(signal_score=11)
    assert not decision.allowed


def test_daily_loss_lock(governor):
    governor.on_trade_closed(-150.0)
    governor.on_trade_closed(60.0)   # break the consecutive-loss chain
    governor.on_trade_closed(-170.0)  # realized -260 <= -250
    assert governor.state.locked
    assert governor.state.daily_max_loss_hit
    assert not governor.evaluate_new_trade(signal_score=11).allowed


def test_consecutive_loss_lock(governor):
    # Scenario C: -75 then -50 -> two consecutive losses -> locked
    governor.on_trade_closed(-75.0)
    governor.on_trade_closed(-50.0)
    assert governor.state.locked
    assert governor.state.lock_reason == "consecutive_losses"
    assert not governor.evaluate_new_trade(signal_score=11).allowed


def test_max_trades_lock(governor):
    for pnl in (10.0, -10.0, 10.0, -10.0):
        governor.on_trade_closed(pnl)
    assert governor.state.locked
    assert governor.state.lock_reason == "max_trades"


def test_risk_reduced_after_loss(governor):
    governor.on_trade_closed(-75.0)
    decision = governor.evaluate_new_trade(signal_score=11)
    assert decision.allowed
    assert decision.risk_usd == 50  # never increase risk after a loss


def test_risk_after_win_requires_score(governor):
    governor.on_trade_closed(90.0)
    # score >= 8 -> risk_after_win
    assert governor.evaluate_new_trade(signal_score=8).risk_usd == 100
    # low score -> default risk only
    assert governor.evaluate_new_trade(signal_score=7).risk_usd == 75


def test_open_loss_blocks_new_trades(governor):
    decision = governor.evaluate_new_trade(signal_score=9, open_loss_usd=160.0)
    assert not decision.allowed
    assert "open loss" in decision.reason
    assert not governor.state.locked  # blocks, but does not lock the day


def test_scenario_a_profit_lock_zone(governor):
    # Trade 1 +90, trade 2 +100 -> realized 190, in the profit-lock zone (>=150)
    governor.on_trade_closed(90.0)
    governor.on_trade_closed(100.0)
    assert not governor.state.locked

    # final trade only with score >= 9 and halved risk (<= 50)
    blocked = governor.evaluate_new_trade(signal_score=8)
    assert not blocked.allowed

    allowed = governor.evaluate_new_trade(signal_score=9)
    assert allowed.allowed
    assert allowed.risk_usd <= 50

    # if profit then reaches +200 -> day locks
    governor.on_trade_closed(15.0)
    assert governor.state.locked
    assert governor.state.daily_target_hit


def test_scenario_d_recovery_day(governor):
    governor.on_trade_closed(-75.0)
    governor.on_trade_closed(60.0)
    governor.on_trade_closed(90.0)
    assert governor.state.realized_pnl == 75.0
    assert not governor.state.locked
    # below the 150 lock zone -> normal rules apply, one trade left before max_trades
    decision = governor.evaluate_new_trade(signal_score=9)
    assert decision.allowed


def test_equity_drawdown_lock(governor):
    governor.check_equity_drawdown(current_equity=25000.0 - 260.0)
    assert governor.state.locked
    assert governor.state.lock_reason == "equity_drawdown"


def test_reset_for_new_day(governor):
    governor.on_trade_closed(-75.0)
    governor.on_trade_closed(-50.0)
    assert governor.state.locked
    governor.reset_for_day(date(2026, 1, 6), 24875.0)
    assert not governor.state.locked
    assert governor.state.trades_today == 0
    assert governor.evaluate_new_trade(signal_score=9).allowed
