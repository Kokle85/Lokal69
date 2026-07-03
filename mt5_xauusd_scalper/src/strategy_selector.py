"""Picks which strategy signal (if any) to act on for the current candle."""
from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from models import NO_TRADE_REGIMES, Regime, Signal
from regime_detector import MarketSnapshot
from strategy_high_precision import EvaluationResult, HighPrecisionStrategy
from strategy_momentum import MomentumStrategy


@dataclass
class SelectionResult:
    signal: Signal | None
    reason: str
    rejections: list[str] = field(default_factory=list)


class StrategySelector:
    def __init__(
        self,
        high_precision: HighPrecisionStrategy,
        momentum: MomentumStrategy,
    ) -> None:
        self.high_precision = high_precision
        self.momentum = momentum

    def select(self, snap: MarketSnapshot, regime: Regime) -> SelectionResult:
        if regime in NO_TRADE_REGIMES:
            reason = f"no trade: regime is {regime.value}"
            logger.debug(reason)
            return SelectionResult(None, reason)

        hp: EvaluationResult = self.high_precision.evaluate(snap, regime)
        mo: EvaluationResult = self.momentum.evaluate(snap, regime)
        rejections = [f"high_precision: {r}" for r in hp.rejections] + [
            f"momentum: {r}" for r in mo.rejections
        ]

        if hp.signal is None and mo.signal is None:
            return SelectionResult(None, "no strategy produced a signal", rejections)
        if hp.signal is not None and mo.signal is None:
            return SelectionResult(hp.signal, "only high precision signal available", rejections)
        if mo.signal is not None and hp.signal is None:
            return SelectionResult(mo.signal, "only momentum signal available", rejections)

        assert hp.signal is not None and mo.signal is not None
        if hp.signal.expected_r_score > mo.signal.expected_r_score:
            return SelectionResult(hp.signal, "high precision has higher expected R score", rejections)
        if mo.signal.expected_r_score > hp.signal.expected_r_score:
            return SelectionResult(mo.signal, "momentum has higher expected R score", rejections)

        # Tie-break: better spread, then the cleaner (tighter) stop.
        if hp.signal.spread_points != mo.signal.spread_points:
            chosen = hp.signal if hp.signal.spread_points < mo.signal.spread_points else mo.signal
            return SelectionResult(chosen, "tie broken by better spread", rejections)
        chosen = hp.signal if hp.signal.sl_distance <= mo.signal.sl_distance else mo.signal
        return SelectionResult(chosen, "tie broken by cleaner stop loss", rejections)
