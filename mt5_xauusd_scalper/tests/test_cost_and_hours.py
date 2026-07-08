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


# ------------------------------------------------------------------ diagnose funnel

def test_diagnose_funnel_accounts_for_every_bar():
    """The rejection funnel must classify every scanned bar exactly once."""
    import numpy as np
    import pandas as pd
    from backtest import Backtester, M1_WINDOW
    from config import BotConfig

    rng = np.random.default_rng(5)
    n = 3 * 1440
    times = pd.date_range(pd.Timestamp("2026-03-02 00:00", tz="UTC"), periods=n, freq="1min")
    close = 2350 + np.cumsum(rng.normal(0, 0.12, n))
    openp = np.concatenate([[close[0]], close[:-1]])
    pad = np.abs(rng.normal(0.06, 0.03, n))
    m1 = pd.DataFrame({"time": times, "open": openp,
                       "high": np.maximum(openp, close) + pad,
                       "low": np.minimum(openp, close) - pad,
                       "close": close, "tick_volume": rng.integers(50, 300, n)})
    bt = Backtester(BotConfig(), m1)
    funnel, stats = bt.diagnose()
    scanned = funnel.pop("bars_scanned")
    assert scanned == len(m1) - M1_WINDOW
    # every scanned bar lands in exactly one terminal bucket
    assert sum(funnel.values()) == scanned
    # trend-alignment stats are percentages in range
    for key in ("m15_uptrend_pct", "m5_uptrend_pct", "m15_m5_agree_pct"):
        assert 0.0 <= stats[key] <= 100.0


# ------------------------------------------------------------------ adaptive risk

def test_daily_budget_single_trade_risks_whole_budget():
    from config import ORBConfig
    from risk_manager import orb_position_risk
    o = ORBConfig(risk_mode="daily_budget", daily_risk_budget_usd=300,
                  max_trades_per_day=1, max_risk_per_trade_usd=300)
    assert orb_position_risk(o, 0, 0.0) == 300.0


def test_daily_budget_splits_and_consumes():
    from config import ORBConfig
    from risk_manager import orb_position_risk
    o = ORBConfig(risk_mode="daily_budget", daily_risk_budget_usd=300,
                  max_trades_per_day=2, max_risk_per_trade_usd=300)
    assert orb_position_risk(o, 0, 0.0) == 150.0          # 300 / 2
    assert orb_position_risk(o, 1, -150.0) == 150.0       # (300-150) / 1 left
    assert orb_position_risk(o, 1, -300.0) == 0.0         # budget spent


def test_daily_budget_profit_extends_but_capped():
    from config import ORBConfig
    from risk_manager import orb_position_risk
    o = ORBConfig(risk_mode="daily_budget", daily_risk_budget_usd=300,
                  max_trades_per_day=2, profit_extends_budget=True, max_risk_per_trade_usd=300)
    # (300 + 200 banked) / 1 = 500, capped at 300
    assert orb_position_risk(o, 1, 200.0) == 300.0


def test_fixed_mode_returns_fixed_risk():
    from config import ORBConfig
    from risk_manager import orb_position_risk
    o = ORBConfig(risk_mode="fixed", risk_per_trade_usd=75, max_risk_per_trade_usd=300)
    assert orb_position_risk(o, 0, 0.0) == 75.0


def test_lot_adapts_to_sl_at_same_risk():
    from models import SymbolSpec
    from risk_manager import calculate_lot
    spec = SymbolSpec(name="XAUUSD", point=0.01, tick_size=0.01, tick_value=1.0,
                      volume_min=0.01, volume_max=100, volume_step=0.01, digits=2)
    wide = calculate_lot(300, 4700, 4695, spec)   # 5.0 SL
    tight = calculate_lot(300, 4700, 4697, spec)  # 3.0 SL
    assert wide.ok and tight.ok
    assert tight.lot > wide.lot                   # tighter stop -> bigger lot, same $ risk


# ------------------------------------------------------------------ live time base & lot caps

def test_normalize_bar_timeline_removes_server_offset():
    """FundingPips-style UTC+3 labels must be shifted onto real UTC."""
    import pandas as pd
    from datetime import datetime, timedelta, timezone
    from main import _normalize_bar_timeline

    wall = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    # server labels run 3h ahead: last closed bar 14:59 (closes 15:00)
    times = pd.date_range("2026-07-08 14:00", periods=60, freq="1min", tz="UTC")
    m1 = pd.DataFrame({"time": times, "open": 1.0, "high": 1.0, "low": 1.0,
                       "close": 1.0, "tick_volume": 1})
    shifted, offset = _normalize_bar_timeline(m1, wall)
    assert offset == 180
    assert shifted["time"].iloc[-1] == pd.Timestamp("2026-07-08 11:59", tz="UTC")


def test_normalize_bar_timeline_zero_offset_untouched():
    import pandas as pd
    from datetime import datetime, timezone
    from main import _normalize_bar_timeline

    wall = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    times = pd.date_range("2026-07-08 11:00", periods=60, freq="1min", tz="UTC")
    m1 = pd.DataFrame({"time": times, "open": 1.0, "high": 1.0, "low": 1.0,
                       "close": 1.0, "tick_volume": 1})
    shifted, offset = _normalize_bar_timeline(m1, wall)
    assert offset == 0
    assert shifted is m1  # no copy when nothing to fix


def test_size_position_respects_prop_max_lot():
    """FundingPips gold rule: 0.4 lot cap, far below symbol_info's 5.0."""
    from datetime import datetime, timezone
    from config import StrategyTuningConfig, TradingConfig
    from models import Direction, Regime, Signal, StrategyName, SymbolSpec
    from risk_manager import RiskManager

    spec = SymbolSpec(name="XAUUSD", point=0.01, tick_size=0.01, tick_value=1.0,
                      volume_min=0.01, volume_max=5.0, volume_step=0.01, digits=2)
    signal = Signal(symbol="XAUUSD", direction=Direction.BUY,
                    strategy=StrategyName.ORB, regime=Regime.TREND_UP,
                    entry=4000.0, sl=3999.0, tp=4003.0, rr=3.0, score=10,
                    max_score=10, setup_reason="t", spread_points=20.0,
                    created_at=datetime.now(timezone.utc), risk_usd=300.0)
    rm = RiskManager(TradingConfig(max_lot=0.4), StrategyTuningConfig())
    capped = rm.size_position(signal, spec, cap_to_max=True)
    assert capped.ok
    assert capped.lot == 0.4                       # 3.0 lots wanted -> capped
    assert capped.loss_at_sl_usd == 40.0           # true risk at the cap

    rm2 = RiskManager(TradingConfig(max_lot=0.0), StrategyTuningConfig())
    uncapped = rm2.size_position(signal, spec, cap_to_max=True)
    assert uncapped.lot == 3.0                     # broker max 5.0 not binding
