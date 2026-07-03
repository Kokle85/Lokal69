"""Validation tooling tests: Monte Carlo, spread sensitivity, break-even spread."""
import numpy as np

from validation import _slice_metrics, breakeven_spread, monte_carlo


def test_monte_carlo_needs_minimum_trades():
    assert monte_carlo([1.0, -1.0, 2.0], iterations=100) is None


def test_monte_carlo_positive_edge():
    # strongly positive expectancy -> low probability of a losing run
    rng = np.random.default_rng(1)
    pnls = list(rng.normal(20.0, 10.0, 200))  # mean +20, modest noise
    mc = monte_carlo(pnls, iterations=2000, drawdown_limit_usd=150)
    assert mc is not None
    assert mc.net_pnl_p50 > 0
    assert mc.prob_negative < 0.10
    assert mc.net_pnl_p05 < mc.net_pnl_p95


def test_monte_carlo_negative_edge_flagged():
    rng = np.random.default_rng(2)
    pnls = list(rng.normal(-5.0, 30.0, 200))  # losing system
    mc = monte_carlo(pnls, iterations=2000)
    assert mc.prob_negative > 0.6  # mostly-losing outcomes


def test_monte_carlo_is_deterministic():
    pnls = list(np.linspace(-10, 10, 60))
    a = monte_carlo(pnls, iterations=500, seed=99)
    b = monte_carlo(pnls, iterations=500, seed=99)
    assert a.net_pnl_p50 == b.net_pnl_p50
    assert a.prob_negative == b.prob_negative


def test_breakeven_spread_found():
    rows = [
        {"spread_points": 10, "profit_factor": 1.6},
        {"spread_points": 20, "profit_factor": 1.2},
        {"spread_points": 30, "profit_factor": 0.9},
    ]
    assert breakeven_spread(rows) == 20


def test_breakeven_spread_none_when_no_edge():
    rows = [
        {"spread_points": 10, "profit_factor": 0.8},
        {"spread_points": 20, "profit_factor": 0.5},
    ]
    assert breakeven_spread(rows) is None


def test_slice_metrics_basic():
    m = _slice_metrics([10.0, -5.0, 20.0, -5.0])
    assert m["trades"] == 4
    assert m["net"] == 20.0
    assert m["pf"] == 3.0  # 30 won / 10 lost
    assert m["win_rate"] == 50.0


def test_slice_metrics_empty():
    m = _slice_metrics([])
    assert m["trades"] == 0
    assert m["net"] == 0.0
