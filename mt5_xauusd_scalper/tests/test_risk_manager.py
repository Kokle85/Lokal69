"""Risk manager hard-rule tests."""
from datetime import datetime, timezone

import pytest

from config import StrategyTuningConfig, TradingConfig
from models import Direction, Regime, Signal, StrategyName
from risk_manager import RiskManager, validate_sl_distance


@pytest.fixture
def risk_manager(tuning) -> RiskManager:
    return RiskManager(TradingConfig(), tuning)


def make_signal(entry=2000.0, sl=1999.6, tp=2000.4) -> Signal:
    return Signal(
        symbol="XAUUSD",
        direction=Direction.BUY,
        strategy=StrategyName.HIGH_PRECISION,
        regime=Regime.TREND_UP,
        entry=entry,
        sl=sl,
        tp=tp,
        rr=1.0,
        score=9,
        max_score=11,
        setup_reason="test",
        spread_points=10.0,
        created_at=datetime.now(timezone.utc),
        risk_usd=75.0,
    )


ATR = 0.35


def test_valid_signal_passes(risk_manager):
    check = risk_manager.validate_signal(
        make_signal(), atr_value=ATR, spread_points=10, open_positions=0, symbol_tradeable=True
    )
    assert check.ok


def test_no_trade_without_sl(risk_manager):
    signal = make_signal(sl=0.0)
    check = risk_manager.validate_signal(
        signal, atr_value=ATR, spread_points=10, open_positions=0, symbol_tradeable=True
    )
    assert not check.ok
    assert "stop loss" in check.reason


def test_no_trade_when_spread_high(risk_manager):
    check = risk_manager.validate_signal(
        make_signal(), atr_value=ATR, spread_points=40, open_positions=0, symbol_tradeable=True
    )
    assert not check.ok
    assert "spread" in check.reason


def test_no_trade_when_position_open(risk_manager):
    check = risk_manager.validate_signal(
        make_signal(), atr_value=ATR, spread_points=10, open_positions=1, symbol_tradeable=True
    )
    assert not check.ok
    assert "positions" in check.reason


def test_no_trade_when_symbol_not_tradeable(risk_manager):
    check = risk_manager.validate_signal(
        make_signal(), atr_value=ATR, spread_points=10, open_positions=0, symbol_tradeable=False
    )
    assert not check.ok


def test_sl_too_small_rejected(tuning):
    # 0.1 ATR distance < min_sl_atr 0.8
    check = validate_sl_distance(2000.0, 2000.0 - 0.1 * ATR, ATR, tuning)
    assert not check.ok
    assert "below minimum" in check.reason


def test_sl_too_large_rejected(tuning):
    # 3 ATR distance > max_sl_atr 2.2
    check = validate_sl_distance(2000.0, 2000.0 - 3.0 * ATR, ATR, tuning)
    assert not check.ok
    assert "above maximum" in check.reason


def test_sl_within_bounds_accepted(tuning):
    check = validate_sl_distance(2000.0, 2000.0 - 1.2 * ATR, ATR, tuning)
    assert check.ok


def test_config_forbids_disabling_stop_loss():
    with pytest.raises(Exception):
        TradingConfig(require_stop_loss=False)
    with pytest.raises(Exception):
        TradingConfig(allow_trade_without_sl=True)
