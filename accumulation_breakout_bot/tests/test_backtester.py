"""Backtester: end-to-end on synthetic data, fill rules, metrics, matrix."""
import numpy as np
import pandas as pd

from backtester import Backtester, load_m5_csv, run_comparison, _metrics
from conftest import flat_zone_candles, make_candles
from models import BacktestTrade, Direction, EntryMode


def _scenario_df(cfg, win: bool) -> pd.DataFrame:
    """Trending warmup (rejected by the slope filter, so no accidental zones)
    + accumulation + direct breakout, then straight to TP or SL."""
    warmup = []
    for i in range(40):  # gentle ramp up to the zone
        base = 1998.6 + 0.03 * i
        warmup.append((base, base + 0.25, base - 0.25, base + 0.05))
    zone = flat_zone_candles()
    breakout = (2000.9, 2001.5, 2000.85, 2001.4)
    follow = []
    if win:  # march to TP (entry+3R ~ 2005.8) without touching SL (~1999.9)
        for i in range(12):
            lo = 2001.6 + 0.5 * i
            follow.append((lo, lo + 0.9, lo - 0.05, lo + 0.8))
    else:    # collapse straight through the SL
        follow.append((2001.3, 2001.35, 1998.0, 1998.2))
    return make_candles(warmup + zone + [breakout] + follow)


def _mk_cfg(cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.entry_mode = EntryMode.DIRECT_BREAKOUT
    cfg.use_ema_filter = False
    cfg.use_session_filter = False
    cfg.backtest.spread_points = 0.0  # deterministic geometry for assertions
    return cfg


def test_backtest_win_hits_tp_at_3r(cfg):
    bt = Backtester(_mk_cfg(cfg), _scenario_df(cfg, win=True))
    report = bt.run("win")
    assert report.total_trades == 1
    trade = report.trades[0]
    assert trade.result == "WIN"
    assert trade.r_result == 3.0
    assert trade.direction is Direction.BUY


def test_backtest_loss_hits_sl_at_minus_1r(cfg):
    bt = Backtester(_mk_cfg(cfg), _scenario_df(cfg, win=False))
    report = bt.run("loss")
    assert report.total_trades == 1
    assert report.trades[0].result == "LOSS"
    assert report.trades[0].r_result == -1.0


def test_stop_first_when_bar_spans_both(cfg):
    """A single bar touching SL and TP must count as a LOSS (conservative)."""
    c = _mk_cfg(cfg)
    warmup = _scenario_df(cfg, win=False).iloc[:-1]  # up to and incl. breakout
    spike = pd.DataFrame({
        "time": [warmup["time"].iloc[-1] + pd.Timedelta(minutes=5)],
        "open": [2001.4], "high": [2010.0], "low": [1998.0],
        "close": [2005.0], "volume": [100],
    })
    df = pd.concat([warmup, spike], ignore_index=True)
    report = Backtester(c, df).run("both")
    assert report.total_trades == 1
    assert report.trades[0].result == "LOSS"


def test_metrics_computation():
    from datetime import datetime

    def t(r):
        return BacktestTrade(
            open_time=datetime(2026, 1, 5), close_time=datetime(2026, 1, 5),
            direction=Direction.BUY, entry=0, stop_loss=0, take_profit=0,
            exit_price=0, r_result=r, result="WIN" if r > 0 else "LOSS",
            session_name="", entry_mode=EntryMode.DIRECT_BREAKOUT,
            zone_high=0, zone_low=0,
        )

    report = _metrics([t(3.0), t(-1.0), t(-1.0), t(3.0), t(-1.0)], "m")
    assert report.total_trades == 5
    assert report.winning_trades == 2
    assert report.losing_trades == 3
    assert report.win_rate_pct == 40.0
    assert report.total_r == 3.0
    assert report.profit_factor == 2.0     # 6R won / 3R lost
    assert report.max_consecutive_losses == 2
    assert report.best_trade_r == 3.0
    assert report.worst_trade_r == -1.0


def test_comparison_matrix_runs_all_8(cfg):
    reports = run_comparison(_mk_cfg(cfg), _scenario_df(cfg, win=True))
    assert len(reports) == 8
    labels = {r.label for r in reports}
    assert "DIRECT_BREAKOUT|ema_off|session_off" in labels
    assert "BREAKOUT_RETEST|ema_on|session_on" in labels


def test_csv_loader_histdata_mt_format(tmp_path):
    p = tmp_path / "gold.csv"
    rows = "\n".join(
        f"2026.01.05,08:{i:02d},2000.1,2000.5,1999.9,2000.3,0" for i in range(10)
    )
    p.write_text(rows + "\n")
    df = load_m5_csv(p)  # M1 input -> resampled to M5
    assert list(df.columns)[:5] == ["time", "open", "high", "low", "close"]
    assert len(df) == 2  # 10 x M1 -> 2 x M5
    assert df["high"].iloc[0] == 2000.5
