"""End-to-end integration tests driving the real order lifecycle through a
fake MT5 terminal: connect, resolve, execute, manage, close, plus failure paths.
"""
import asyncio
from datetime import datetime, timezone

import pytest

import execution as execution_mod
import mt5_connector as connector_mod
from config import BotConfig
from fake_mt5 import FakeMT5
from models import BotMode, Direction, Regime, Signal, StrategyName
from mt5_connector import MT5Connector


@pytest.fixture
def fake(monkeypatch):
    fk = FakeMT5(demo=True, bid=2000.00, ask=2000.20)
    monkeypatch.setattr(connector_mod, "mt5", fk)
    monkeypatch.setattr(execution_mod, "mt5", fk)
    return fk


def make_connector(fake) -> MT5Connector:
    cfg = BotConfig()
    conn = MT5Connector(cfg.mt5, cfg.trading)
    conn.connect()
    conn.resolve_symbol()
    return conn


def make_signal(direction=Direction.BUY, entry=2000.20, sl=1999.20, tp=2001.20) -> Signal:
    return Signal(
        symbol="XAUUSD", direction=direction, strategy=StrategyName.HIGH_PRECISION,
        regime=Regime.TREND_UP, entry=entry, sl=sl, tp=tp, rr=1.0, score=11, max_score=13,
        setup_reason="integration", spread_points=20.0, created_at=datetime.now(timezone.utc),
        risk_usd=100.0, lot=0.10, mode=BotMode.DEMO_AUTO,
    )


# ------------------------------------------------------------ connector layer

def test_connect_and_resolve_symbol(fake):
    conn = make_connector(fake)
    assert conn.connected
    assert conn.symbol == "XAUUSD"
    assert conn.is_demo_account()
    spec = conn.symbol_spec()
    assert spec.volume_min == 0.01
    assert conn.is_alive()


def test_terminal_down_detected(fake):
    conn = make_connector(fake)
    fake.terminal_down = True
    assert not conn.is_alive()


def test_place_market_order_opens_position(fake):
    conn = make_connector(fake)
    engine = execution_mod.ExecutionEngine(conn, BotConfig().trading)
    spec = conn.symbol_spec()
    result = engine.place_market_order(make_signal(), spec, lot=0.10)
    assert result.ok
    assert result.ticket >= 1000
    assert len(conn.open_positions()) == 1


def test_order_check_failure_blocks_order(fake):
    conn = make_connector(fake)
    fake.fail_order_check = True
    engine = execution_mod.ExecutionEngine(conn, BotConfig().trading)
    result = engine.place_market_order(make_signal(), conn.symbol_spec(), lot=0.10)
    assert not result.ok
    assert "order_check" in result.comment
    assert len(conn.open_positions()) == 0


def test_order_send_failure_reported(fake):
    conn = make_connector(fake)
    fake.fail_order_send = True
    notes = []
    engine = execution_mod.ExecutionEngine(conn, BotConfig().trading, notify=notes.append)
    result = engine.place_market_order(make_signal(), conn.symbol_spec(), lot=0.10)
    assert not result.ok
    assert notes  # Telegram notified on failure


def test_pre_execution_check_rejects_price_drift(fake):
    conn = make_connector(fake)
    engine = execution_mod.ExecutionEngine(conn, BotConfig().trading)
    # signal entry far from current ask -> drift beyond max_entry_slippage_points
    stale = make_signal(entry=1990.0, sl=1989.0, tp=1992.0)
    ok, reason = engine.pre_execution_check(stale, conn.symbol_spec())
    assert not ok
    assert "moved" in reason


def test_reconnect_after_drop(fake):
    conn = make_connector(fake)
    fake.terminal_down = True
    assert not conn.is_alive()
    fake.terminal_down = False
    assert conn.reconnect()
    assert conn.symbol == "XAUUSD"


def test_partial_then_full_close(fake):
    conn = make_connector(fake)
    engine = execution_mod.ExecutionEngine(conn, BotConfig().trading)
    spec = conn.symbol_spec()
    engine.place_market_order(make_signal(), spec, lot=0.10)
    ticket = conn.open_positions()[0].ticket
    # partial close 0.04
    r1 = engine.close_position(ticket, Direction.BUY, 0.04, spec, "partial")
    assert r1.ok
    assert abs(conn.open_positions()[0].volume - 0.06) < 1e-9
    # close remainder
    r2 = engine.close_position(ticket, Direction.BUY, 0.06, spec, "time_exit")
    assert r2.ok
    assert len(conn.open_positions()) == 0


# ------------------------------------------------------------ full bot lifecycle

def _make_bot(fake, tmp_path):
    from journal import Journal
    from main import ScalperBot

    cfg = BotConfig(mode=BotMode.DEMO_AUTO)
    cfg.telegram.enabled = False
    bot = ScalperBot(cfg)
    bot.journal.close()
    bot.journal = Journal(tmp_path / "test.db")
    bot.connector.connect()
    bot.connector.resolve_symbol()
    bot.auto_trading_allowed = True
    return bot


def test_full_execute_and_close_updates_governor(fake, tmp_path):
    bot = _make_bot(fake, tmp_path)
    signal = make_signal()
    signal.signal_id = bot.journal.record_signal(signal)

    asyncio.run(bot._execute_signal(signal))
    assert bot.position is not None
    assert len(bot.connector.open_positions()) == 1

    # price runs to target; broker closes the position
    fake.move_price(bid=2001.40, ask=2001.60)
    for pos in list(fake._positions.values()):
        fake._close_deal({"position": pos.ticket, "volume": pos.volume,
                          "type": fake.ORDER_TYPE_SELL, "symbol": "XAUUSD"})

    bot._sync_closed_position()
    assert bot.position is None
    assert bot.governor.state.trades_today == 1
    assert bot.governor.state.realized_pnl > 0  # winning trade booked
    bot.journal.close()


def test_governor_blocks_execution_when_locked(fake, tmp_path):
    bot = _make_bot(fake, tmp_path)
    # lock the day via two losses (default daily_goals path uses sniper by default)
    bot.governor.state.locked = True
    bot.governor.state.lock_reason = "test_lock"
    signal = make_signal()
    signal.signal_id = bot.journal.record_signal(signal)
    asyncio.run(bot._execute_signal(signal))
    assert bot.position is None  # locked -> no order
    assert len(bot.connector.open_positions()) == 0
    bot.journal.close()
