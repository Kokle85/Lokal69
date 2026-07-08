"""Order execution: the ONLY module that sends orders to MT5.

Hard rules enforced here, regardless of what upstream code says:
- no order without SL and TP,
- no order without a successful lot calculation,
- spread re-checked at send time,
- one open position maximum,
- live sending requires live_trading_enabled AND --mode live (checked upstream,
  re-checked here).
"""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from config_loader import Settings
from models import Direction, Signal, SymbolSpec
from mt5_client import MT5Client, MT5_AVAILABLE

if MT5_AVAILABLE:
    import MetaTrader5 as mt5

ORDER_COMMENT = "accum_breakout"


@dataclass
class ExecutionResult:
    ok: bool
    ticket: int = 0
    price: float = 0.0
    volume: float = 0.0
    comment: str = ""


class TradeExecutor:
    def __init__(self, client: MT5Client, cfg: Settings) -> None:
        self.client = client
        self.cfg = cfg

    @staticmethod
    def _filling(spec: SymbolSpec) -> int:
        """Choose a filling type the broker allows (FOK-only brokers reject
        hardcoded IOC with retcode 10030 - including position closes)."""
        ioc = getattr(mt5, "ORDER_FILLING_IOC", 1)
        if spec.filling_mode == 0 or spec.filling_mode & 2:
            return ioc
        if spec.filling_mode & 1:
            return getattr(mt5, "ORDER_FILLING_FOK", ioc)
        return getattr(mt5, "ORDER_FILLING_RETURN", ioc)

    def execute(self, signal: Signal, lot: float, spec: SymbolSpec) -> ExecutionResult:
        # ---- final hard gates (defense in depth; upstream already checked)
        if not self.cfg.live_trading_enabled:
            return ExecutionResult(False, comment="live_trading_enabled is false")
        if signal.stop_loss <= 0 or signal.take_profit <= 0:
            return ExecutionResult(False, comment="refusing order without SL/TP")
        if lot <= 0:
            return ExecutionResult(False, comment="refusing order with invalid lot")
        spread = self.client.spread_points()
        if spread > self.cfg.max_spread_points:
            return ExecutionResult(
                False, comment=f"spread {spread:.0f}pt above limit at send time"
            )
        if self.client.open_positions():
            return ExecutionResult(False, comment="a position is already open")

        tick = self.client.tick()
        buying = signal.direction is Direction.BUY
        price = tick.ask if buying else tick.bid
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": lot,
            "type": mt5.ORDER_TYPE_BUY if buying else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": round(signal.stop_loss, spec.digits),
            "tp": round(signal.take_profit, spec.digits),
            "deviation": self.cfg.mt5.deviation_points,
            "magic": self.cfg.mt5.magic_number,
            "comment": ORDER_COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(spec),
        }

        check = self.client.order_check(request)
        if check is None or check.retcode != 0:
            code = check.retcode if check else -1
            logger.error("order_check blocked the order: {}", code)
            return ExecutionResult(False, comment=f"order_check failed ({code})")

        result = self.client.order_send(request)
        if result is None:
            return ExecutionResult(False, comment="order_send returned None")
        partial = getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)
        done = getattr(mt5, "TRADE_RETCODE_DONE", 10009)
        placed = getattr(mt5, "TRADE_RETCODE_PLACED", 10008)
        if result.retcode == partial and result.volume > 0:
            logger.warning("Order partially filled: {}/{} lot", result.volume, lot)
        elif result.retcode not in (done, placed):
            logger.error("order_send failed: {} ({})", result.retcode, result.comment)
            return ExecutionResult(False, comment=f"retcode {result.retcode}: {result.comment}")

        logger.info("Order filled: {} {} lot @ {} SL {} TP {} ticket {}",
                    signal.direction.value, result.volume, result.price,
                    request["sl"], request["tp"], result.order)
        return ExecutionResult(True, ticket=result.order, price=result.price,
                               volume=result.volume)
