"""End-to-end test of the LIVE ORB path: ScalperBot + fake MT5 terminal.

Drives the real bot loop (tick -> scan -> risk -> lot sizing -> order ->
position management -> broker close -> governor lock) with a synthetic
opening-range breakout. This is the test that would have caught the
scalp-position-management bug (8-min time exit / 0.7R breakeven strangling
a 3R ORB runner) - the backtester has its own correct path and never sees it.
"""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

import execution as execution_mod
import main as main_mod
import mt5_connector as connector_mod
from config import BotConfig
from fake_mt5 import FakeMT5
from journal import Journal
from models import BotMode
from utils import now_in_tz


class _FakeDatetime:
    """Replaces main.datetime so the bot's wall clock is test-controlled."""

    current: datetime = None  # type: ignore[assignment]

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.astimezone(tz)


def _orb_cfg() -> BotConfig:
    cfg = BotConfig()
    cfg.trading_style = "orb"
    cfg.mode = BotMode.DEMO_AUTO
    cfg.telegram.enabled = False
    o = cfg.orb
    o.enabled = True
    o.entry_mode = "breakout"
    o.trend_filter = "none"   # the full-2025-validated system: no EMA filter
    o.tp_r = 3.0
    o.min_range_atr = 0.05
    o.move_to_breakeven_at_r = 99.0
    o.partial_close_enabled = False
    o.max_trade_minutes = 360
    o.max_trades_per_day = 1
    o.risk_mode = "daily_budget"
    o.daily_risk_budget_usd = 300.0
    o.max_risk_per_trade_usd = 300.0
    o.profit_extends_budget = True
    return cfg


def _session_frames(open_utc: datetime):
    """M1 with a clean 15-min range then a breakout bar, plus an M5 for ATR."""
    rng = np.random.default_rng(3)
    # M1: 45 pre-open bars + 15 range bars + 1 breakout bar
    start = open_utc - timedelta(minutes=45)
    n = 45 + 15 + 1
    times = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    close = np.full(n, 2000.25) + rng.normal(0, 0.02, n)
    high = close + 0.05
    low = close - 0.05
    # opening range bars [open, open+15): high 2000.50, low 2000.00
    for i in range(45, 60):
        close[i] = 2000.25
        high[i] = 2000.50
        low[i] = 2000.00
    # breakout bar closes above range_high + buffer
    close[-1] = 2000.60
    high[-1] = 2000.62
    low[-1] = 2000.30
    openp = np.concatenate([[close[0]], close[:-1]])
    m1 = pd.DataFrame({"time": times, "open": openp, "high": high, "low": low,
                       "close": close, "tick_volume": np.full(n, 100)})

    m5_times = pd.date_range(open_utc - timedelta(minutes=5 * 60), periods=60, freq="5min", tz="UTC")
    m5_close = np.full(60, 2000.2) + rng.normal(0, 0.05, 60)
    m5 = pd.DataFrame({
        "time": m5_times, "open": m5_close, "high": m5_close + 0.5,
        "low": m5_close, "close": m5_close + 0.25, "tick_volume": np.full(60, 500),
    })  # true range ~0.5 -> M5 ATR ~0.5
    return m1, m5


@pytest.fixture
def env(monkeypatch, tmp_path):
    fake = FakeMT5(demo=True, bid=2000.42, ask=2000.60)  # 18pt spread, ask = breakout close
    monkeypatch.setattr(connector_mod, "mt5", fake)
    monkeypatch.setattr(execution_mod, "mt5", fake)
    monkeypatch.setattr(main_mod, "Journal", lambda: Journal(tmp_path / "e2e.db"))
    monkeypatch.setattr(main_mod, "datetime", _FakeDatetime)

    cfg = _orb_cfg()
    # today's London open (09:00 session tz) in UTC - DST-proof
    tz = ZoneInfo(cfg.session_opens.timezone)
    today_local = now_in_tz(cfg.session_opens.timezone).date()
    open_local = datetime(today_local.year, today_local.month, today_local.day, 9, 0, tzinfo=tz)
    open_utc = open_local.astimezone(timezone.utc)
    _FakeDatetime.current = open_utc + timedelta(minutes=16, seconds=30)

    m1, m5 = _session_frames(open_utc)
    bot = main_mod.ScalperBot(cfg)
    state = {"new_candle": True}
    bot.market_data.fetch_closed = lambda tf: m1 if tf == "M1" else m5
    bot.market_data.new_m1_candle = lambda df: state["new_candle"]
    bot.startup()
    return bot, fake, state, tmp_path


def _tick(bot):
    asyncio.run(bot.tick())


def _signal_count(db):
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    finally:
        con.close()


def test_e2e_orb_demo_auto_full_lifecycle(env):
    bot, fake, state, tmp = env

    # ---- tick 1: breakout bar -> signal -> sized -> executed
    _tick(bot)
    assert bot.position is not None, "breakout must produce an executed trade in DEMO_AUTO"
    pos = bot.position
    # geometry: SL below the range low, TP = entry + 3R
    assert pos.sl == pytest.approx(1999.975, abs=0.02)
    risk = pos.entry - pos.sl
    assert pos.tp == pytest.approx(pos.entry + 3.0 * risk, abs=0.02)
    # lot sized from the $300 daily budget: 300 / (risk * $100 per lot)
    expected_lot = np.floor(300.0 / (risk * 100.0) / 0.01) * 0.01
    assert pos.lot == pytest.approx(expected_lot, abs=0.011)
    broker_pos = fake._positions[pos.ticket]
    # the order carries SL/TP rounded to the symbol's digits (2 for XAUUSD)
    assert broker_pos.sl == pytest.approx(round(pos.sl, 2), abs=1e-9), "order must carry the SL"
    assert broker_pos.tp == pytest.approx(round(pos.tp, 2), abs=1e-9)

    # ---- tick 2: +20 min, price at ~+1R: ORB management must NOT touch it
    # (the scalp rules would have time-exited at 8 min and BE-moved at 0.7R)
    _FakeDatetime.current += timedelta(minutes=20)
    fake.move_price(pos.entry + risk, pos.entry + risk + 0.18)
    state["new_candle"] = False
    _tick(bot)
    assert bot.position is not None, "ORB trade must not be time-exited after 20 min"
    assert fake._positions[pos.ticket].sl == pytest.approx(round(pos.sl, 2), abs=1e-9), (
        "breakeven must stay OFF (move_to_breakeven_at_r=99)"
    )

    # ---- tick 3: broker fills the TP; bot must reconcile and lock the day
    _FakeDatetime.current += timedelta(minutes=30)
    tp = pos.tp
    fake.move_price(tp + 0.005, tp + 0.19)
    fake.order_send({
        "action": fake.TRADE_ACTION_DEAL, "position": pos.ticket,
        "symbol": "XAUUSD", "volume": pos.lot, "type": fake.ORDER_TYPE_SELL,
    })
    _tick(bot)
    assert bot.position is None
    s = bot.governor.state
    assert s.trades_today == 1
    assert s.realized_pnl == pytest.approx(3 * 300.0, rel=0.05), "a 3R win risks $300 -> ~$900"
    assert s.locked, "day must lock after the single allowed ORB trade wins"

    # ---- tick 4: same session cannot signal/trade again
    state["new_candle"] = True
    _tick(bot)
    assert bot.position is None
    assert len(fake._positions) == 0
    assert _signal_count(tmp / "e2e.db") == 1


def test_e2e_restart_restores_daily_state_and_adopts_position(env):
    """A restart must NOT re-arm a fresh daily budget (N restarts = N x $300
    was possible before), and an open broker position must be adopted."""
    bot, fake, state, tmp = env

    # winning day: open, broker fills TP, day locks
    _tick(bot)
    pos = bot.position
    assert pos is not None
    fake.move_price(pos.tp + 0.005, pos.tp + 0.19)
    fake.order_send({
        "action": fake.TRADE_ACTION_DEAL, "position": pos.ticket,
        "symbol": "XAUUSD", "volume": pos.lot, "type": fake.ORDER_TYPE_SELL,
    })
    _FakeDatetime.current += timedelta(minutes=30)
    state["new_candle"] = False
    _tick(bot)
    assert bot.governor.state.locked

    # ---- restart #1: same journal, no open position
    bot2 = main_mod.ScalperBot(bot.cfg)
    bot2.market_data.fetch_closed = bot.market_data.fetch_closed
    bot2.market_data.new_m1_candle = lambda df: True
    bot2.startup()
    s = bot2.governor.state
    assert s.trades_today == 1, "trades_today must survive the restart"
    assert s.realized_pnl == pytest.approx(bot.governor.state.realized_pnl, abs=0.01)
    assert s.locked, "the daily lock must re-arm from the journal"
    _tick(bot2)
    assert bot2.position is None and len(fake._positions) == 0, (
        "restart must not allow a second trade on a locked day"
    )

    # ---- restart #2: an orphaned broker position must be adopted
    fake.order_send({
        "action": fake.TRADE_ACTION_DEAL, "symbol": "XAUUSD", "volume": 0.5,
        "type": fake.ORDER_TYPE_BUY, "sl": 1999.0, "tp": 2006.0,
        "magic": bot.cfg.trading.magic_number,
    })
    bot3 = main_mod.ScalperBot(bot.cfg)
    bot3.market_data.fetch_closed = bot.market_data.fetch_closed
    bot3.market_data.new_m1_candle = lambda df: False
    bot3.startup()
    assert bot3.position is not None, "orphaned position must be adopted on startup"
    assert bot3.position.ticket in fake._positions
    assert bot3.position.sl == pytest.approx(1999.0)


def test_e2e_signal_only_no_duplicates_and_no_orders(env):
    bot, fake, state, tmp = env
    bot.cfg.mode = BotMode.SIGNAL_ONLY
    bot.auto_trading_allowed = False

    _tick(bot)
    assert bot.position is None and len(fake._positions) == 0, "SIGNAL_ONLY must never trade"
    assert _signal_count(tmp / "e2e.db") == 1

    # next M1 candle with the breakout still valid must NOT re-emit the signal
    _FakeDatetime.current += timedelta(minutes=1)
    _tick(bot)
    assert _signal_count(tmp / "e2e.db") == 1, "one signal per session, no Telegram spam"
