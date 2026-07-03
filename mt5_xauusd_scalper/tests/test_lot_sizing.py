"""Lot sizing tests against a realistic XAUUSD contract spec."""
from models import SymbolSpec
from risk_manager import calculate_lot

# Standard XAUUSD: 1 lot = 100 oz, tick 0.01 -> $1 per tick per lot
SPEC = SymbolSpec(
    name="XAUUSD", point=0.01, tick_size=0.01, tick_value=1.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01, digits=2,
)


def test_lot_sizing_respects_risk():
    # SL distance 1.80 -> $180 per lot -> $75 risk -> 0.41 lot (floored)
    result = calculate_lot(75.0, 2364.20, 2366.00, SPEC)
    assert result.ok
    assert result.lot == 0.41
    assert result.loss_at_sl_usd <= 75.0


def test_lot_rounded_down_to_step():
    result = calculate_lot(100.0, 2000.0, 1999.0, SPEC)  # raw = 1.0 exactly
    assert result.ok
    assert result.lot == 1.0
    result2 = calculate_lot(99.0, 2000.0, 1999.0, SPEC)  # raw = 0.99
    assert result2.lot == 0.99


def test_lot_sizing_respects_min_volume():
    # tiny risk with wide SL -> lot below volume_min -> skip with reason
    result = calculate_lot(1.0, 2000.0, 1995.0, SPEC)
    assert not result.ok
    assert "below broker minimum" in result.reason


def test_lot_capped_or_skipped_at_max_volume():
    big_spec = SymbolSpec(
        name="XAUUSD", point=0.01, tick_size=0.01, tick_value=1.0,
        volume_min=0.01, volume_max=0.5, volume_step=0.01, digits=2,
    )
    skipped = calculate_lot(500.0, 2000.0, 1999.0, big_spec)  # raw 5.0 > max 0.5
    assert not skipped.ok
    capped = calculate_lot(500.0, 2000.0, 1999.0, big_spec, cap_to_max=True)
    assert capped.ok
    assert capped.lot == 0.5


def test_no_lot_without_sl():
    result = calculate_lot(75.0, 2000.0, 2000.0, SPEC)
    assert not result.ok
    assert "SL" in result.reason


def test_zero_risk_rejected():
    result = calculate_lot(0.0, 2000.0, 1999.0, SPEC)
    assert not result.ok
