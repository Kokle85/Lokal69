"""SNIPER_MODE tests: config, overrides, sweep scoring, governor scenarios A-E."""
from datetime import date, datetime, timezone

import pytest

import indicators as ind
from config import BotConfig, RiskConfig, SniperModeConfig
from conftest import make_candles, zigzag_closes
from models import BotMode, Direction, Regime, Signal, StrategyName
from sniper_mode import SNIPER_MAX_SCORE, SniperGovernor, apply_sniper_overrides
from telegram_bot import format_signal_message

DAY = date(2026, 1, 5)


def make_governor(**overrides) -> SniperGovernor:
    sniper = SniperModeConfig(**{k: v for k, v in overrides.items() if k in SniperModeConfig.model_fields})
    risk = RiskConfig(**{k: v for k, v in overrides.items() if k in RiskConfig.model_fields})
    return SniperGovernor(risk, sniper, DAY, starting_equity=25000.0)


# ------------------------------------------------------------------ config

def test_sniper_config_defaults():
    cfg = SniperModeConfig()
    assert cfg.enabled
    assert cfg.max_trades_per_day == 2
    assert not cfg.allow_second_trade_after_loss
    assert cfg.second_trade_min_score == 11
    assert cfg.second_trade_after_loss_min_score == 12


def test_sniper_config_rejects_more_than_two_trades():
    with pytest.raises(Exception):
        SniperModeConfig(max_trades_per_day=3)


def test_sniper_config_rejects_looser_after_loss_score():
    with pytest.raises(Exception):
        SniperModeConfig(second_trade_min_score=11, second_trade_after_loss_min_score=10)


def test_apply_sniper_overrides():
    cfg = apply_sniper_overrides(BotConfig())
    assert cfg.strategies.high_precision_scalp.rr == 0.5
    assert cfg.strategies.momentum_scalp.rr == 0.5
    assert cfg.position_management.move_to_breakeven_at_r == 0.25
    assert cfg.position_management.partial_close_at_r == 0.35
    assert cfg.position_management.time_exit_minutes == 5
    assert cfg.position_management.max_trade_duration_minutes == 8
    assert cfg.strategy.min_sl_atr == 0.7
    assert cfg.strategy.max_sl_atr == 1.8


# ------------------------------------------------------------------ sweep

def _sweep_frame():
    """Frame where the detector's reference window low is known."""
    df = make_candles(zigzag_closes(40, 2000.0, up=0.2, down=0.1))
    # detector reference window for lookback=20, recent=5 is iloc[-25:-5]
    ref_low = float(df["low"].iloc[-25:-5].min())
    return df, ref_low


def test_liquidity_sweep_detected():
    df, ref_low = _sweep_frame()
    # dip below the prior range low, then reclaim it
    df.loc[df.index[-2], "low"] = ref_low - 0.2
    df.loc[df.index[-1], "close"] = ref_low + 0.5
    assert ind.detect_liquidity_sweep(df, bullish=True, atr_now=0.35, lookback=20, max_sweep_atr=1.2)


def test_liquidity_sweep_too_deep_rejected():
    df, ref_low = _sweep_frame()
    df.loc[df.index[-2], "low"] = ref_low - 5.0  # way beyond max_sweep_atr * ATR
    df.loc[df.index[-1], "close"] = ref_low + 0.5
    assert not ind.detect_liquidity_sweep(df, bullish=True, atr_now=0.35, lookback=20, max_sweep_atr=1.2)


def test_no_sweep_without_reclaim():
    df, ref_low = _sweep_frame()
    df.loc[df.index[-2], "low"] = ref_low - 0.2
    df.loc[df.index[-1], "close"] = ref_low - 0.1  # still below the level
    assert not ind.detect_liquidity_sweep(df, bullish=True, atr_now=0.35, lookback=20, max_sweep_atr=1.2)


# ------------------------------------------------------------------ governor

def test_first_trade_requires_execution_score():
    gov = make_governor()
    assert not gov.evaluate_new_trade(signal_score=10).allowed
    decision = gov.evaluate_new_trade(signal_score=11)
    assert decision.allowed
    assert decision.risk_usd == 100  # sniper default risk


def test_scenario_a_two_wins_then_stop():
    gov = make_governor()
    gov.on_trade_closed(50.0)
    # second trade allowed only with score >= 11
    assert not gov.evaluate_new_trade(signal_score=10).allowed
    decision = gov.evaluate_new_trade(signal_score=11)
    assert decision.allowed
    assert decision.risk_usd == 100  # risk_after_win
    gov.on_trade_closed(50.0)
    assert gov.state.locked  # 2 trades taken -> stop for the day
    assert not gov.evaluate_new_trade(signal_score=13).allowed


def test_scenario_b_target_stops_immediately():
    gov = make_governor()
    gov.on_trade_closed(100.0)
    decision = gov.evaluate_new_trade(signal_score=11)
    assert decision.allowed
    gov.on_trade_closed(100.0)  # realized 200 -> target
    assert gov.state.locked
    assert gov.state.daily_target_hit


def test_scenario_c_first_loss_locks_by_default():
    gov = make_governor()
    gov.on_trade_closed(-100.0)
    assert gov.state.locked
    assert gov.state.lock_reason == "loss_lock"
    assert not gov.evaluate_new_trade(signal_score=13).allowed


def test_scenario_d_aggressive_second_after_loss():
    gov = make_governor(allow_second_trade_after_loss=True)
    gov.on_trade_closed(-100.0)
    assert not gov.state.locked  # aggressive opt-in keeps the day open
    # score 11 is not enough after a loss
    assert not gov.evaluate_new_trade(signal_score=11).allowed
    decision = gov.evaluate_new_trade(signal_score=12)
    assert decision.allowed
    assert decision.risk_usd == 50  # reduced risk only
    gov.on_trade_closed(-50.0)  # second loss -> realized -150
    assert gov.state.locked
    assert gov.state.daily_max_loss_hit or gov.state.lock_reason in ("max_trades", "loss_lock")
    assert not gov.evaluate_new_trade(signal_score=13).allowed


def test_scenario_e_score_10_second_trade_rejected():
    gov = make_governor()
    gov.on_trade_closed(60.0)
    decision = gov.evaluate_new_trade(signal_score=10)
    assert not decision.allowed
    assert "score" in decision.reason
    assert not gov.state.locked  # rejection only - a better setup may still come


def test_daily_loss_150_locks():
    gov = make_governor(allow_second_trade_after_loss=True)
    gov.on_trade_closed(-150.0)
    assert gov.state.locked
    assert gov.state.daily_max_loss_hit


def test_open_loss_blocks_second_trade():
    gov = make_governor()
    gov.on_trade_closed(60.0)
    decision = gov.evaluate_new_trade(signal_score=12, open_loss_usd=160.0)
    assert not decision.allowed
    assert "open loss" in decision.reason


def test_green_day_protection_caps_risk():
    # realized >= profit lock (100): risk never exceeds banked profit
    gov = make_governor()
    gov.on_trade_closed(120.0)
    decision = gov.evaluate_new_trade(signal_score=12)
    assert decision.allowed
    assert decision.risk_usd <= 120.0


def test_second_trade_status_reporting():
    gov = make_governor()
    allowed, reason = gov.second_trade_status()
    assert allowed and "trade 1" in reason
    gov.on_trade_closed(-100.0)
    allowed, reason = gov.second_trade_status()
    assert not allowed
    assert gov.first_trade_result().startswith("LOSS")

    gov2 = make_governor()
    gov2.on_trade_closed(80.0)
    allowed, reason = gov2.second_trade_status()
    assert allowed
    assert "11" in reason
    assert gov2.first_trade_result().startswith("WIN")


# ------------------------------------------------------------------ telegram

def test_signal_message_shows_trade_number():
    signal = Signal(
        symbol="XAUUSD", direction=Direction.BUY, strategy=StrategyName.HIGH_PRECISION,
        regime=Regime.TREND_UP, entry=2364.20, sl=2363.20, tp=2364.70, rr=0.5,
        score=12, max_score=SNIPER_MAX_SCORE, setup_reason="test",
        spread_points=10.0, created_at=datetime.now(timezone.utc),
        risk_usd=100.0, lot=0.1, trade_number=1, max_trades_today=2,
    )
    text = format_signal_message(signal, 0.0, BotMode.SIGNAL_ONLY)
    assert "Trade number today: 1/2" in text
    assert "Score: 12/13" in text
