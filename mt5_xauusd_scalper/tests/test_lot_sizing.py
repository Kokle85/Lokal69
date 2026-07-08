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


# ---------------------------------------------------------- contract-size basis

def test_contract_size_overrides_bogus_tick_value():
    """FundingPips live: trade_tick_value reported 0.01 on 100oz gold, which
    mislabels a ~$719 stop-out as $7. Contract size must win."""
    from models import SymbolSpec
    from risk_manager import calculate_lot

    bogus = SymbolSpec(name="XAUUSD", point=0.01, tick_size=0.01, tick_value=0.01,
                       volume_min=0.01, volume_max=5.0, volume_step=0.01, digits=2,
                       contract_size=100.0)
    r = calculate_lot(300.0, 4062.69, 4080.67, bogus)
    # loss per lot = 17.98 x 100 = $1798 -> lot 0.16, real risk ~$287
    assert r.ok
    assert r.lot == 0.16
    assert abs(r.loss_at_sl_usd - 287.68) < 0.5


def test_tick_math_still_used_without_contract_size():
    from models import SymbolSpec
    from risk_manager import calculate_lot

    spec = SymbolSpec(name="XAUUSD", point=0.01, tick_size=0.01, tick_value=1.0,
                      volume_min=0.01, volume_max=100.0, volume_step=0.01, digits=2)
    r = calculate_lot(300.0, 4000.0, 3997.0, spec)
    assert r.ok and r.lot == 1.0  # 300 / (300 ticks x $1)
