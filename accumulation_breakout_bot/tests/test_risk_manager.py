"""Risk manager: lot math, floor rounding, hard rejections."""
from datetime import datetime, timezone

from models import Direction, EntryMode, LotResult, Signal, SymbolSpec, Zone
from risk_manager import calculate_lot, size_signal, validate_signal

SPEC = SymbolSpec(name="XAUUSD", point=0.01, tick_size=0.01, tick_value=1.0,
                  volume_min=0.01, volume_max=100.0, volume_step=0.01, digits=2)


def _zone() -> Zone:
    return Zone(high=2001.0, low=2000.0, upper_touches=3, lower_touches=3,
                start_time=datetime.now(timezone.utc),
                end_time=datetime.now(timezone.utc), atr=1.0)


def _signal(entry=2001.5, sl=2000.4, rr=3.0) -> Signal:
    risk = abs(entry - sl)
    return Signal(
        timestamp=datetime.now(timezone.utc), symbol="XAUUSD", timeframe="M5",
        entry_mode=EntryMode.DIRECT_BREAKOUT, direction=Direction.BUY,
        zone=_zone(), entry_price=entry, stop_loss=sl,
        take_profit=entry + rr * risk, risk_reward_ratio=rr, atr=1.0,
    )


def test_lot_sizes_to_risk_percent():
    # balance 25000, 0.5% = $125 risk; SL distance 1.0 -> $100/lot -> 1.25 lots
    result = calculate_lot(25000.0, 0.5, 2001.5, 2000.5, SPEC)
    assert result.ok
    assert result.lot == 1.25
    assert result.loss_at_sl_usd <= 125.0 + 1e-9


def test_lot_floors_to_step_never_rounds_up():
    # $125 risk over 1.13 distance -> 1.10619 lots -> floors to 1.10
    result = calculate_lot(25000.0, 0.5, 2001.63, 2000.50, SPEC)
    assert result.ok
    assert result.lot == 1.10
    assert result.loss_at_sl_usd <= 125.0


def test_zero_sl_distance_refused():
    result = calculate_lot(25000.0, 0.5, 2001.0, 2001.0, SPEC)
    assert not result.ok
    assert "forbidden" in result.reason


def test_tiny_risk_below_min_lot_refused():
    result = calculate_lot(100.0, 0.1, 2001.5, 1995.0, SPEC)  # $0.10 risk
    assert not result.ok
    assert "below broker minimum" in result.reason


def test_missing_tp_refused(cfg):
    s = _signal()
    s.take_profit = 0.0
    ok, reason = validate_signal(s, cfg)
    assert not ok
    assert "missing SL or TP" in reason


def test_stop_distance_bounds_enforced(cfg):
    too_tight = _signal(entry=2001.5, sl=2001.3)   # 0.2 < 0.5 x ATR(1.0)
    ok, reason = validate_signal(too_tight, cfg)
    assert not ok and "below minimum" in reason

    too_wide = _signal(entry=2001.5, sl=1998.5)    # 3.0 > 2.5 x ATR(1.0)
    ok, reason = validate_signal(too_wide, cfg)
    assert not ok and "above maximum" in reason


def test_tp_must_match_rr(cfg):
    s = _signal()
    s.take_profit = s.entry_price + 1.0  # not 3R away
    ok, reason = validate_signal(s, cfg)
    assert not ok
    assert "risk_reward_ratio" in reason


def test_size_signal_end_to_end(cfg):
    result = size_signal(_signal(), 25000.0, SPEC, cfg)
    assert isinstance(result, LotResult)
    assert result.ok
    assert result.lot > 0
