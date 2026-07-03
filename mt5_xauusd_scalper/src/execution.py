"""Order execution: market orders with mandatory SL/TP, full retcode handling."""
from __future__ import annotations

from typing import Any, Callable, Optional

from loguru import logger

from config import TradingConfig
from models import Direction, OrderResult, Signal, SymbolSpec
from mt5_connector import MT5Connector, mt5

ORDER_COMMENT = "XAUUSD_SCALPER_MVP"

# Human-readable messages for the retcodes we most commonly hit.
RETCODE_MESSAGES: dict[int, str] = {
    10004: "Requote",
    10006: "Request rejected",
    10007: "Request canceled by trader",
    10008: "Order placed",
    10009: "Request completed",
    10010: "Only part of the request was completed",
    10011: "Request processing error",
    10012: "Request canceled by timeout",
    10013: "Invalid request",
    10014: "Invalid volume",
    10015: "Invalid price",
    10016: "Invalid stops (SL/TP)",
    10017: "Trade is disabled",
    10018: "Market is closed",
    10019: "Not enough money/margin",
    10020: "Prices changed",
    10021: "No quotes to process the request",
    10022: "Invalid order expiration",
    10023: "Order state changed",
    10024: "Too frequent requests",
    10025: "No changes in request",
    10026: "Autotrading disabled by server",
    10027: "Autotrading disabled by client terminal",
    10028: "Request locked for processing",
    10029: "Order or position frozen",
    10030: "Invalid order filling type",
    10031: "No connection with the trade server",
    10032: "Operation allowed only for live accounts",
    10033: "Number of pending orders reached the limit",
    10034: "Volume of orders and positions reached the limit",
}


def retcode_message(retcode: int) -> str:
    return RETCODE_MESSAGES.get(retcode, f"Unknown retcode {retcode}")


class ExecutionEngine:
    def __init__(
        self,
        connector: MT5Connector,
        trading: TradingConfig,
        notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.connector = connector
        self.trading = trading
        self._notify = notify or (lambda _msg: None)

    # ------------------------------------------------------------- pre-checks

    def pre_execution_check(self, signal: Signal, spec: SymbolSpec) -> tuple[bool, str]:
        """Recheck market conditions moments before sending the order."""
        tick = self.connector.tick()
        current = tick.ask if signal.direction is Direction.BUY else tick.bid

        spread_pts = spec.spread_points(tick.bid, tick.ask)
        if spread_pts > self.trading.max_spread_points:
            return False, f"spread rose to {spread_pts:.0f}pt before execution"

        drift_pts = abs(current - signal.entry) / spec.point if spec.point > 0 else 0.0
        if drift_pts > self.trading.max_entry_slippage_points:
            return False, f"price moved {drift_pts:.0f}pt from signal entry"

        if signal.sl <= 0:
            return False, "signal lost its stop loss"
        if signal.direction is Direction.BUY and signal.sl >= current:
            return False, "SL is above current price for a BUY"
        if signal.direction is Direction.SELL and signal.sl <= current:
            return False, "SL is below current price for a SELL"

        if len(self.connector.open_positions()) >= self.trading.max_positions:
            return False, "a position opened before execution"
        return True, "ok"

    # ------------------------------------------------------------- orders

    def build_market_request(self, signal: Signal, spec: SymbolSpec, lot: float) -> dict[str, Any]:
        tick = self.connector.tick()
        buying = signal.direction is Direction.BUY
        price = tick.ask if buying else tick.bid  # buy at ask, sell at bid
        return {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": lot,
            "type": mt5.ORDER_TYPE_BUY if buying else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": round(signal.sl, spec.digits),
            "tp": round(signal.tp, spec.digits),
            "deviation": self.trading.deviation_points,
            "magic": self.trading.magic_number,
            "comment": ORDER_COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

    def place_market_order(self, signal: Signal, spec: SymbolSpec, lot: float) -> OrderResult:
        ok, reason = self.pre_execution_check(signal, spec)
        if not ok:
            logger.warning("Order cancelled before send: {}", reason)
            return OrderResult(False, 0, f"pre-check failed: {reason}")

        request = self.build_market_request(signal, spec, lot)

        check = self.connector.order_check(request)
        if check is None or check.retcode != 0:
            code = check.retcode if check else -1
            msg = f"order_check failed: {code} {retcode_message(code)}"
            logger.error(msg)
            self._notify(f"⚠️ Order blocked by order_check: {retcode_message(code)}")
            return OrderResult(False, code, msg)

        result = self.connector.order_send(request)
        if result is None:
            msg = "order_send returned None"
            logger.error(msg)
            self._notify("❌ order_send failed: no response from terminal")
            return OrderResult(False, -1, msg)

        if result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            msg = f"order_send failed: {result.retcode} {retcode_message(result.retcode)} ({result.comment})"
            logger.error(msg)
            self._notify(f"❌ Order failed: {retcode_message(result.retcode)}")
            return OrderResult(False, result.retcode, msg)

        logger.info(
            "Order filled: {} {} lot {} @ {} SL {} TP {} ticket {}",
            signal.direction.value, spec.name, result.volume, result.price,
            request["sl"], request["tp"], result.order,
        )
        return OrderResult(True, result.retcode, "filled", result.order, result.price, result.volume)

    # ------------------------------------------------------------- management

    def modify_sl(self, ticket: int, new_sl: float, tp: float, spec: SymbolSpec) -> OrderResult:
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": spec.name,
            "position": ticket,
            "sl": round(new_sl, spec.digits),
            "tp": round(tp, spec.digits),
            "magic": self.trading.magic_number,
        }
        result = self.connector.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else -1
            msg = f"SL modify failed: {code} {retcode_message(code)}"
            logger.error(msg)
            return OrderResult(False, code, msg)
        return OrderResult(True, result.retcode, "sl modified", ticket)

    def close_position(
        self, ticket: int, direction: Direction, lot: float, spec: SymbolSpec, reason: str
    ) -> OrderResult:
        tick = self.connector.tick()
        closing_buy = direction is Direction.BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "position": ticket,
            "volume": lot,
            "type": mt5.ORDER_TYPE_SELL if closing_buy else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if closing_buy else tick.ask,
            "deviation": self.trading.deviation_points,
            "magic": self.trading.magic_number,
            "comment": f"{ORDER_COMMENT}_{reason}"[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = self.connector.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else -1
            msg = f"close failed: {code} {retcode_message(code)}"
            logger.error(msg)
            self._notify(f"❌ Position close failed: {retcode_message(code)}")
            return OrderResult(False, code, msg)
        logger.info("Position {} closed ({}): {} lot @ {}", ticket, reason, lot, result.price)
        return OrderResult(True, result.retcode, reason, ticket, result.price, lot)
