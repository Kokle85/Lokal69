"""Accumulation Breakout Bot - entry point.

Modes:
    python src/main.py --mode signal                       # detect + log, no orders
    python src/main.py --mode live                         # orders (double-gated)
    python src/main.py --mode backtest --csv data/historical/xau_m5.csv
    python src/main.py --mode backtest --symbol XAUUSD --from 2025-01-01 --to 2026-01-01
    python src/main.py --mode optimize --csv data/historical/xau_m5.csv
"""
from __future__ import annotations

import argparse
import itertools
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent))

from backtester import Backtester, load_m5_csv, run_comparison
from config_loader import ConfigError, Settings, load_settings
from breakout_strategy import BreakoutStrategy
from journal import Journal
from logger import setup_logging
from models import Signal
from news_filter import NewsFilter
from report_generator import print_table, write_reports
from risk_manager import size_signal
from session_filter import active_session

SCAN_INTERVAL_SECONDS = 10


# ================================================================== live/signal

class LiveBot:
    """One loop for both signal-only and live mode; the only difference is
    whether TradeExecutor is allowed to send (double gate)."""

    def __init__(self, cfg: Settings, live: bool) -> None:
        from mt5_client import MT5Client
        from trade_executor import TradeExecutor

        self.cfg = cfg
        self.live = live and cfg.live_trading_enabled
        if live and not cfg.live_trading_enabled:
            logger.warning(
                "--mode live requested but live_trading_enabled is false in "
                "config - running SIGNAL-ONLY. Set live_trading_enabled: true "
                "to actually trade."
            )
        from telegram_notifier import TelegramNotifier

        self.client = MT5Client(cfg.mt5, cfg.symbol)
        self.executor = TradeExecutor(self.client, cfg)
        self.strategy = BreakoutStrategy(cfg)
        self.journal = Journal()
        self.news = NewsFilter(cfg)
        self.telegram = TelegramNotifier(cfg)
        self.last_bar_time: Optional[pd.Timestamp] = None
        self.trades_today = 0
        self.current_day = None
        self.cooldown_until: Optional[pd.Timestamp] = None

    def run(self) -> None:
        self.client.connect()
        self.client.resolve_symbol()
        spec = self.client.spec()
        if not spec.trade_allowed:
            raise SystemExit(f"{spec.name} is not tradeable on this account")
        mode_label = "LIVE" if self.live else "SIGNAL-ONLY"
        logger.info("Running in {} mode on {}", mode_label, self.cfg.symbol)
        self.telegram.send_startup(self.cfg.symbol, mode_label)
        try:
            while True:
                try:
                    self.tick(spec)
                except Exception as exc:  # noqa: BLE001 - the loop must survive
                    logger.exception("Error in scan loop: {}", exc)
                time.sleep(SCAN_INTERVAL_SECONDS)
        finally:
            self.client.shutdown()

    def tick(self, spec) -> None:
        if not self.client.is_alive():
            logger.error("MT5 terminal connection lost; retrying")
            return
        # MTF RSI needs deep M5 history: H4 RSI(14) converges after ~40 H4
        # bars = ~2000 M5 candles.
        need = max(600, self.cfg.ema_period + 50,
                   2400 if self.cfg.use_mtf_rsi_filter else 0)
        m5 = self.client.m5_candles(count=need)
        bar_time = m5["time"].iloc[-1]
        if self.last_bar_time is not None and bar_time == self.last_bar_time:
            return  # act once per closed M5 candle
        self.last_bar_time = bar_time

        # broker-time day rollover
        if bar_time.date() != self.current_day:
            self.current_day = bar_time.date()
            self.trades_today = 0

        now = bar_time.to_pydatetime()  # broker clock, same base as sessions
        session = active_session(self.cfg.sessions, now)

        # ---- gates (each rejection is journaled so no-trade days are explainable)
        if self.trades_today >= self.cfg.max_trades_per_day:
            return
        if self.cooldown_until is not None and bar_time < self.cooldown_until:
            return
        if self.cfg.use_session_filter and session is None:
            return
        blocked, why = self.news.is_blocked(now)
        if blocked:
            self.journal.log_rejection(now, self.cfg.symbol, "M5", why, session or "")
            return
        if self.client.open_positions():
            return  # one position at a time

        ev = self.strategy.on_bar(m5)
        if ev.signal is None:
            if ev.rejections and ev.zone is not None:
                # only journal decisions made against a real zone - not warmup noise
                self.journal.log_rejection(now, self.cfg.symbol, "M5",
                                           ev.rejections[0], session or "")
            return

        signal: Signal = ev.signal
        signal.session_name = session or ""
        spread = self.client.spread_points()
        signal.spread_points = spread
        if spread > self.cfg.max_spread_points:
            self.journal.log_rejection(now, self.cfg.symbol, "M5",
                                       f"spread {spread:.0f}pt above "
                                       f"{self.cfg.max_spread_points:.0f}pt",
                                       session or "", signal)
            return

        lot = size_signal(signal, self.client.balance(), spec, self.cfg)
        if not lot.ok:
            self.journal.log_rejection(now, self.cfg.symbol, "M5",
                                       lot.reason, session or "", signal)
            return
        signal.lot_size = lot.lot

        self.journal.log_signal(signal)
        logger.info("SIGNAL {} {} | lot {} | entry {:.2f} SL {:.2f} TP {:.2f} | {}",
                    signal.direction.value, signal.symbol, signal.lot_size,
                    signal.entry_price, signal.stop_loss, signal.take_profit,
                    signal.reason_for_entry)
        self.telegram.send_signal(signal, lot.loss_at_sl_usd,
                                  "LIVE" if self.live else "SIGNAL-ONLY")

        if not self.live:
            return

        results = self.executor.execute(signal, lot.lot, spec)
        filled = [r for r in results if r.ok]
        failed = [r for r in results if not r.ok]
        if filled:
            # the TP ladder counts as ONE trade for daily-limit purposes
            self.trades_today += 1
            self.cooldown_until = bar_time + pd.Timedelta(minutes=self.cfg.cooldown_minutes)
            for r in filled:
                self.journal.log_trade_open(signal, r.ticket)
            total_vol = sum(r.volume for r in filled)
            self.telegram.send_trade_open(signal, filled[0].ticket,
                                          filled[0].price, total_vol)
            if failed:
                self.telegram.send_error(
                    f"{len(failed)} of {len(results)} TP-ladder orders failed: "
                    f"{failed[0].comment}"
                )
        else:
            reason = failed[0].comment if failed else "no orders attempted"
            self.journal.log_rejection(now, self.cfg.symbol, "M5",
                                       f"execution failed: {reason}",
                                       session or "", signal)
            self.telegram.send_error(f"Order failed: {reason}")


# ================================================================== backtest

def _load_data(cfg: Settings, args) -> pd.DataFrame:
    if args.csv:
        return load_m5_csv(args.csv)
    # no CSV: pull from MT5 (Windows terminal required)
    from mt5_client import MT5Client

    client = MT5Client(cfg.mt5, args.symbol or cfg.symbol)
    client.connect()
    try:
        client.resolve_symbol()
        days = 365
        if args.date_from and args.date_to:
            days = (pd.Timestamp(args.date_to) - pd.Timestamp(args.date_from)).days + 1
        df = client.m5_range(days)
    finally:
        client.shutdown()
    if args.date_from:
        df = df[df["time"] >= pd.Timestamp(args.date_from)]
    if args.date_to:
        df = df[df["time"] < pd.Timestamp(args.date_to) + pd.Timedelta(days=1)]
    return df.reset_index(drop=True)


def run_backtest(cfg: Settings, args) -> int:
    m5 = _load_data(cfg, args)
    if args.date_from:
        m5 = m5[m5["time"] >= pd.Timestamp(args.date_from)].reset_index(drop=True)
    if args.date_to:
        m5 = m5[m5["time"] < pd.Timestamp(args.date_to) + pd.Timedelta(days=1)].reset_index(drop=True)
    reports = run_comparison(cfg, m5)
    print_table(reports)
    write_reports(reports)
    return 0


def run_optimize(cfg: Settings, args) -> int:
    """Small grid over the zone/breakout levers, run in the configured entry
    mode with all filters as configured. Reports sorted by total R."""
    m5 = _load_data(cfg, args)
    grid = {
        "zone_lookback_candles": [24, 30, 40],
        "touch_tolerance_atr_multiplier": [0.10, 0.15, 0.25],
        "breakout_buffer_atr_multiplier": [0.10, 0.15, 0.25],
        "min_breakout_body_atr_multiplier": [0.3, 0.4, 0.6],
    }
    keys = list(grid)
    reports = []
    combos = list(itertools.product(*grid.values()))
    logger.info("Optimizing over {} combinations...", len(combos))
    for combo in combos:
        variant = cfg.model_copy(deep=True)
        for key, value in zip(keys, combo):
            setattr(variant, key, value)
        label = ",".join(f"{k.split('_')[0]}={v}" for k, v in zip(keys, combo))
        reports.append(Backtester(variant, m5).run(label))
    reports.sort(key=lambda r: r.total_r, reverse=True)
    print_table(reports[:15])
    write_reports(reports)
    return 0


# ================================================================== cli

def main() -> int:
    parser = argparse.ArgumentParser(description="Accumulation Breakout Bot")
    parser.add_argument("--mode", required=True,
                        choices=["live", "signal", "backtest", "optimize"])
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--csv", default=None, help="M5 (or M1) history CSV for backtest")
    parser.add_argument("--from", dest="date_from", default=None)
    parser.add_argument("--to", dest="date_to", default=None)
    args = parser.parse_args()

    setup_logging()
    try:
        cfg = load_settings(args.config)
    except ConfigError as exc:
        logger.error("{}", exc)
        return 2
    if args.symbol:
        cfg.symbol = args.symbol

    if args.mode == "backtest":
        return run_backtest(cfg, args)
    if args.mode == "optimize":
        return run_optimize(cfg, args)

    bot = LiveBot(cfg, live=(args.mode == "live"))
    try:
        bot.run()
    except KeyboardInterrupt:
        logger.info("Stopped by user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
