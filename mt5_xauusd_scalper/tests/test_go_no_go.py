"""Go/No-Go gate tests. The verdict logic is tested purely (fast); a single
tiny smoke test exercises the heavy orchestration path."""
import numpy as np
import pandas as pd

from config import BotConfig
from go_no_go import CRITERIA, assemble_verdict, evaluate, print_verdict
from validation import monte_carlo


def _mc(prob_neg: float, prob_dd: float):
    # A stand-in MonteCarloResult with just the fields the gate reads.
    class _M:
        prob_negative = prob_neg
        prob_drawdown_exceeds_limit = prob_dd
    return _M()


def test_all_criteria_pass_is_go():
    v = assemble_verdict(
        oos_trades=150, oos_profit_factor=1.4, oos_win_rate_pct=48.0,
        mc=_mc(0.10, 0.05), be_spread=25.0, n_full_trades=300,
    )
    assert v.go is True
    assert all(c.passed for c in v.checks)


def test_low_trade_count_is_no_go():
    v = assemble_verdict(
        oos_trades=40, oos_profit_factor=1.4, oos_win_rate_pct=48.0,
        mc=_mc(0.10, 0.05), be_spread=25.0, n_full_trades=300,
    )
    assert v.go is False
    assert not next(c for c in v.checks if c.name == "OOS trade count").passed


def test_weak_profit_factor_is_no_go():
    v = assemble_verdict(
        oos_trades=150, oos_profit_factor=1.05, oos_win_rate_pct=48.0,
        mc=_mc(0.10, 0.05), be_spread=25.0, n_full_trades=300,
    )
    assert v.go is False


def test_high_ruin_probability_is_no_go():
    v = assemble_verdict(
        oos_trades=150, oos_profit_factor=1.4, oos_win_rate_pct=48.0,
        mc=_mc(0.45, 0.05), be_spread=25.0, n_full_trades=300,
    )
    assert v.go is False
    assert not next(c for c in v.checks if c.name == "MC prob(net<0)").passed


def test_edge_dies_at_low_spread_is_no_go():
    v = assemble_verdict(
        oos_trades=150, oos_profit_factor=1.4, oos_win_rate_pct=48.0,
        mc=_mc(0.10, 0.05), be_spread=15.0, n_full_trades=300,
    )
    assert v.go is False


def test_missing_monte_carlo_is_no_go():
    v = assemble_verdict(
        oos_trades=150, oos_profit_factor=1.4, oos_win_rate_pct=48.0,
        mc=None, be_spread=25.0, n_full_trades=12,
    )
    assert v.go is False


def test_criteria_are_strict():
    assert CRITERIA["min_oos_trades"] >= 100
    assert CRITERIA["min_oos_profit_factor"] >= 1.2
    assert CRITERIA["min_breakeven_spread"] >= 20.0


def test_print_verdict_does_not_raise():
    v = assemble_verdict(150, 1.4, 48.0, _mc(0.1, 0.05), 25.0, 300)
    print_verdict(v)


def test_evaluate_smoke_tiny_data():
    """Heavy path, kept small: 2 days, 1 walk-forward sample, tiny MC.
    Thin data can't clear the gate, so this must be NO-GO and must not crash."""
    rng = np.random.default_rng(3)
    n = 2 * 1440
    times = pd.date_range(pd.Timestamp("2026-03-02 00:00", tz="UTC"), periods=n, freq="1min")
    close = 2350 + np.cumsum(rng.normal(0.0, 0.12, n))
    openp = np.concatenate([[close[0]], close[:-1]])
    pad = np.abs(rng.normal(0.06, 0.03, n))
    m1 = pd.DataFrame({
        "time": times, "open": openp,
        "high": np.maximum(openp, close) + pad, "low": np.minimum(openp, close) - pad,
        "close": close, "tick_volume": rng.integers(50, 300, n),
    })
    verdict = evaluate(BotConfig(), m1, folds=1, iterations=100, wf_sample=1)
    assert verdict.go is False
