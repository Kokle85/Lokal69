"""Tests for the profitability levers: cost-to-TP gate, data-driven hour
filter, M5 stop geometry, and the sniper optimizer grid."""
from datetime import datetime
from zoneinfo import ZoneInfo

from config import BotConfig, SessionsConfig, StrategyTuningConfig, TradingConfig
from conftest import uptrend_m5, uptrend_m15
from models import Regime
from optimizer import SNIPER_GRID, apply_sniper_params
from regime_detector import build_snapshot, geometry_atr
from risk_manager import check_cost_to_tp
from setup_builders import hp_buy_m1
from utils import in_session

TRADING = TradingConfig()  # max_cost_to_tp_pct 25, cost_buffer_points 12


# ------------------------------------------------------------------ cost gate

def test_cost_gate_blocks_structurally_unprofitable_trade():
    # The old sniper geometry: TP 20pt, spread 18 -> cost 30pt = 150% of target.
    check = check_cost_to_tp(entry=2000.0, tp=2000.20, spread_points=18, point=0.01, trading=TRADING)
    assert not check.ok
    assert "cost" in check.reason


def test_cost_gate_passes_wide_geometry():
    # M5 geometry: TP 180pt, cost 30pt = ~17% of target.
    check = check_cost_to_tp(entry=2000.0, tp=2001.80, spread_points=18, point=0.01, trading=TRADING)
    assert check.ok


def test_cost_gate_boundary():
    # cost 30pt at exactly 25% requires TP of 120pt; 119pt must fail, 121pt pass.
    fail = check_cost_to_tp(2000.0, 2001.19, 18, 0.01, TRADING)
    ok = check_cost_to_tp(2000.0, 2001.21, 18, 0.01, TRADING)
    assert not fail.ok
    assert ok.ok


def test_cost_gate_rejects_zero_tp():
    assert not check_cost_to_tp(2000.0, 2000.0, 18, 0.01, TRADING).ok


# ------------------------------------------------------------------ hour filter

def _monday_at(hour: int) -> datetime:
    return datetime(2026, 1, 5, hour, 30, tzinfo=ZoneInfo("Europe/Skopje"))


def test_allowed_hours_filter_blocks_other_hours():
    cfg = SessionsConfig(allowed_hours=[15, 16, 17])
    allowed, reason = in_session(cfg, _monday_at(10))  # inside 09-12 window but hour not allowed
    assert not allowed
    assert "allowed_hours" in reason


def test_allowed_hours_filter_passes_listed_hour():
    cfg = SessionsConfig(allowed_hours=[15, 16, 17])
    allowed, _ = in_session(cfg, _monday_at(16))  # inside 15-18 window and allowed
    assert allowed


def test_empty_allowed_hours_means_off():
    cfg = SessionsConfig()  # default []
    allowed, _ = in_session(cfg, _monday_at(10))
    assert allowed


def test_allowed_hours_applies_even_without_windows():
    cfg = SessionsConfig(enabled=False, allowed_hours=[15])
    allowed, _ = in_session(cfg, _monday_at(10))
    assert not allowed


# ------------------------------------------------------------------ geometry

def test_geometry_atr_selects_m5_by_default():
    tuning = StrategyTuningConfig()  # production default: sl_timeframe M5
    snap = build_snapshot(uptrend_m15(), uptrend_m5(), hp_buy_m1(), tuning, 10.0, 0.01)
    assert tuning.sl_timeframe == "M5"
    assert geometry_atr(snap, tuning) == float(snap.m5_atr.iloc[-1])
    # M5 ATR is materially larger than M1 ATR -> wider stops, lower cost ratio
    assert geometry_atr(snap, tuning) > snap.atr_now


def test_geometry_atr_m1_mode():
    tuning = StrategyTuningConfig(sl_timeframe="M1")
    snap = build_snapshot(uptrend_m15(), uptrend_m5(), hp_buy_m1(), tuning, 10.0, 0.01)
    assert geometry_atr(snap, tuning) == snap.atr_now


def test_m5_geometry_changes_hp_stop_selection():
    """In M5 mode the stop must come from M5 swings (or be rejected on M5 ATR
    bounds) - never from the tiny M1 swing the M1-mode test relies on."""
    from config import HighPrecisionConfig
    from strategy_high_precision import HighPrecisionStrategy

    m1_tuning = StrategyTuningConfig(sl_timeframe="M1")
    m5_tuning = StrategyTuningConfig(sl_timeframe="M5")
    snap = build_snapshot(uptrend_m15(), uptrend_m5(), hp_buy_m1(), m1_tuning, 10.0, 0.01)

    m1_result = HighPrecisionStrategy(HighPrecisionConfig(), m1_tuning, 25.0, 15.0, 350.0).evaluate(
        snap, Regime.TREND_UP
    )
    m5_result = HighPrecisionStrategy(HighPrecisionConfig(), m5_tuning, 25.0, 15.0, 350.0).evaluate(
        snap, Regime.TREND_UP
    )
    assert m1_result.signal is not None  # baseline sanity
    if m5_result.signal is not None:
        assert m5_result.signal.sl != m1_result.signal.sl
        assert m5_result.signal.sl_distance > m1_result.signal.sl_distance
    else:
        assert any("SL" in r or "swing" in r for r in m5_result.rejections)


# ------------------------------------------------------------------ sniper grid

def test_sniper_grid_covers_the_cost_levers():
    assert set(SNIPER_GRID) >= {
        "tp_r", "min_sl_atr", "max_sl_atr", "move_to_breakeven_at_r",
        "partial_close_at_r", "time_exit_minutes", "max_cost_to_tp_pct",
    }
    assert all(v >= 1.2 for v in SNIPER_GRID["tp_r"])  # no cost-doomed 0.5R targets


def test_apply_sniper_params():
    params = {k: v[0] for k, v in SNIPER_GRID.items()}
    cfg = apply_sniper_params(BotConfig(), params)
    assert cfg.sniper_mode.enabled
    assert cfg.sniper_mode.tp_r == params["tp_r"]
    assert cfg.sniper_mode.time_exit_minutes == params["time_exit_minutes"]
    assert cfg.trading.max_cost_to_tp_pct == params["max_cost_to_tp_pct"]
    # base config untouched (deep copy)
    assert BotConfig().sniper_mode.tp_r == 1.5
