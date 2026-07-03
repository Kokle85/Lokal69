"""Realistic trading-cost model for the backtester.

Turns the transparent-but-unrealistic "fixed spread only" assumption into a
defensible one: variable spread, per-side commission, adverse slippage on every
fill, occasional requotes, and a configurable intrabar fill rule for the case
where a single M1 bar spans both the stop and the target.

Everything is deterministic given the config seed and the trade index, so runs
are reproducible (the workflow/journal can replay them).
"""
from __future__ import annotations

from dataclasses import dataclass

from config import BacktestConfig
from models import Direction, SymbolSpec


@dataclass
class Fill:
    price: float
    commission_usd: float


class CostModel:
    def __init__(self, cfg: BacktestConfig, spec: SymbolSpec) -> None:
        self.cfg = cfg
        self.spec = spec
        self.point = spec.point

    # ------------------------------------------------------------- spread

    def _rng_unit(self, key: int) -> float:
        """Deterministic pseudo-random in [0,1) from seed+key (no global RNG)."""
        x = (self.cfg.seed * 2654435761 + key * 40503) & 0xFFFFFFFF
        x ^= (x >> 13)
        x = (x * 1274126177) & 0xFFFFFFFF
        x ^= (x >> 16)
        return (x & 0xFFFFFF) / float(0x1000000)

    def spread_points(self, trade_index: int) -> float:
        """Variable spread around the base, clamped to [0, max]. Seeded per trade."""
        if self.cfg.spread_std_points <= 0:
            return self.cfg.base_spread_points
        # Two-sided variation via averaged uniforms (approx-normal, bounded).
        u = (self._rng_unit(trade_index) + self._rng_unit(trade_index + 7)) - 1.0
        spread = self.cfg.base_spread_points + u * self.cfg.spread_std_points * 2.0
        return max(0.0, min(spread, self.cfg.max_spread_points))

    # ------------------------------------------------------------- fills

    def entry_fill(self, reference_price: float, direction: Direction, trade_index: int) -> Fill:
        """Actual entry price after crossing half the spread + slippage (+ maybe a requote).

        Buy fills higher, sell fills lower — always adverse to the trader.
        """
        spread_pts = self.spread_points(trade_index)
        adverse_pts = spread_pts / 2.0 + self.cfg.entry_slippage_points
        if self._rng_unit(trade_index + 101) < self.cfg.requote_probability:
            adverse_pts += self.cfg.requote_extra_points
        adverse = adverse_pts * self.point
        price = reference_price + adverse if direction is Direction.BUY else reference_price - adverse
        return Fill(price, self._commission(trade_index_lot=None))

    def exit_fill(self, level_price: float, direction: Direction, trade_index: int) -> float:
        """Actual exit price for a market exit / stop / target after adverse costs.

        A buy exits at bid (below the level), a sell exits at ask (above it).
        """
        spread_pts = self.spread_points(trade_index)
        adverse = (spread_pts / 2.0 + self.cfg.exit_slippage_points) * self.point
        return level_price - adverse if direction is Direction.BUY else level_price + adverse

    def commission(self, lot: float) -> float:
        return self.cfg.commission_per_lot_per_side_usd * lot

    def _commission(self, trade_index_lot) -> float:  # placeholder kept for symmetry
        return 0.0

    # ------------------------------------------------------------- intrabar

    def stop_hit_first(self, trade_index: int) -> bool:
        """When one bar spans both SL and TP, decide which fills first.

        conservative -> stop first (worst case), optimistic -> target first,
        random -> seeded coin flip.
        """
        mode = self.cfg.intrabar_fill
        if mode == "conservative":
            return True
        if mode == "optimistic":
            return False
        return self._rng_unit(trade_index + 991) < 0.5
