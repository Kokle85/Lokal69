"""Dashboard data-layer tests: SQLite aggregations, CSV ledger, JSON payload."""
import json
from datetime import datetime, timezone

import pytest

from dashboard import CsvData, SqliteData, _polarity_stats
from journal import Journal
from models import BotMode, Direction, Regime, Signal, SignalStatus, StrategyName


def make_signal(direction=Direction.BUY, score=12) -> Signal:
    return Signal(
        symbol="XAUUSD", direction=direction, strategy=StrategyName.HIGH_PRECISION,
        regime=Regime.TREND_UP, entry=2000.0, sl=1999.0, tp=2001.5, rr=1.5,
        score=score, max_score=13, setup_reason="test", spread_points=12.0,
        created_at=datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc),
        risk_usd=100.0, lot=0.5, mode=BotMode.SEMI_AUTO,
    )


@pytest.fixture
def seeded_db(tmp_path):
    """Journal with 3 closed trades (+80, -50, +120), one expired signal."""
    journal = Journal(tmp_path / "dash.db")
    for pnl, score, status in ((80.0, 12, SignalStatus.EXECUTED),
                               (-50.0, 11, SignalStatus.EXECUTED),
                               (120.0, 13, SignalStatus.EXECUTED)):
        sig = make_signal(score=score)
        sig.signal_id = journal.record_signal(sig, status)
        trade_id = journal.record_trade(sig, ticket=1000 + score, fill_price=2000.0, lot=0.5)
        journal.close_trade(trade_id, close_price=2001.0, profit=pnl,
                            close_reason="TAKE_PROFIT" if pnl > 0 else "STOP_LOSS",
                            duration_minutes=10.0)
    expired = make_signal(score=10)
    expired.signal_id = journal.record_signal(expired, SignalStatus.SENT)
    journal.update_signal_status(expired.signal_id, SignalStatus.EXPIRED)
    journal.upsert_daily_stats(
        datetime.now(timezone.utc).date(), 25000, 25150, 150.0, 3, 2, 1, 0,
        daily_locked=False, daily_target_hit=False, daily_max_loss_hit=False,
    )
    journal.close()
    return tmp_path / "dash.db"


def test_polarity_stats():
    s = _polarity_stats([80.0, -50.0, 120.0])
    assert s["trades"] == 3
    assert s["wins"] == 2 and s["losses"] == 1
    assert s["net"] == 150.0
    assert s["profit_factor"] == 4.0  # 200 won / 50 lost
    assert s["expectancy"] == 50.0


def test_sqlite_summary_and_equity(seeded_db):
    data = SqliteData(seeded_db, tz="UTC")
    s = data.summary()
    assert s["trades"] == 3
    assert s["net"] == 150.0
    assert s["win_rate"] == pytest.approx(66.7, abs=0.1)
    curve = data.equity_curve()
    assert [p["equity"] for p in curve] == [80.0, 30.0, 150.0]


def test_sqlite_funnel_and_groupings(seeded_db):
    data = SqliteData(seeded_db, tz="UTC")
    funnel = data.signals_funnel()
    assert funnel.get("EXECUTED") == 3
    assert funnel.get("EXPIRED") == 1
    by_score = {r["key"]: r for r in data.pnl_by_score()}
    assert by_score[12]["net"] == 80.0
    assert by_score[11]["net"] == -50.0
    by_strategy = data.pnl_by_strategy()
    assert by_strategy[0]["key"] == "HIGH_PRECISION_SCALP"
    assert by_strategy[0]["net"] == 150.0
    by_regime = data.pnl_by_regime()
    assert by_regime[0]["key"] == "TREND_UP"


def test_payload_is_json_serializable(seeded_db):
    payload = SqliteData(seeded_db, tz="UTC").payload()
    text = json.dumps(payload)
    assert "summary" in payload and "equity" in payload and "funnel" in payload
    assert len(text) > 100
    assert len(payload["trades"]) == 3
    assert payload["daily"][0]["pnl"] == 150.0  # from daily_stats


def test_csv_ledger_source(tmp_path):
    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        "trade_index,open_time,close_time,direction,strategy,regime,trade_no,score,"
        "entry,sl,tp,lot,close_reason,commission_usd,pnl,equity\n"
        "1,2026-03-02 09:01:00+00:00,2026-03-02 09:15:00+00:00,BUY,HIGH_PRECISION_SCALP,"
        "TREND_UP,1,12,2350.0,2349.0,2351.5,0.5,TAKE_PROFIT,3.5,75.0,75.0\n"
        "2,2026-03-02 15:01:00+00:00,2026-03-02 15:20:00+00:00,SELL,MOMENTUM_SCALP,"
        "TREND_DOWN,2,11,2352.0,2353.0,2350.5,0.5,STOP_LOSS,3.5,-100.0,-25.0\n"
    )
    data = CsvData(csv_path, tz="UTC")
    s = data.summary()
    assert s["trades"] == 2
    assert s["net"] == -25.0
    by_no = {r["key"]: r["net"] for r in data.pnl_by_trade_no()}
    assert by_no == {1: 75.0, 2: -100.0}
    assert data.signals_funnel() == {}  # ledger has no signals table
    payload = data.payload()
    json.dumps(payload)  # must serialize


def test_hour_grouping_respects_timezone(tmp_path):
    csv_path = tmp_path / "t.csv"
    csv_path.write_text(
        "trade_index,open_time,close_time,direction,strategy,regime,trade_no,score,"
        "entry,sl,tp,lot,close_reason,commission_usd,pnl,equity\n"
        "1,2026-03-02 09:00:00+00:00,2026-03-02 09:10:00+00:00,BUY,S,R,1,12,"
        "1,1,1,0.1,TP,0,10.0,10.0\n"
    )
    utc_hours = [r["key"] for r in CsvData(csv_path, tz="UTC").pnl_by_hour()]
    sk_hours = [r["key"] for r in CsvData(csv_path, tz="Europe/Skopje").pnl_by_hour()]
    assert utc_hours == [9]
    assert sk_hours == [10]  # UTC+1 in March (before DST)
