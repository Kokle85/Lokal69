"""A small in-memory fake of the MetaTrader5 module for integration tests.

It implements just enough of the real API surface (constants + functions the bot
calls) to exercise the full order lifecycle: connect, resolve symbol, place a
market order with SL/TP, modify SL, partial/full close, and read back deals.
No network, no terminal, deterministic.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace


class FakeMT5:
    # --- constants (values don't matter, only identity/consistency) ---
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    SYMBOL_TRADE_MODE_FULL = 4
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 2
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_PLACED = 10008

    USD_PER_UNIT = 100.0  # 1 lot XAUUSD = $100 per 1.0 price move

    def __init__(self, demo: bool = True, bid: float = 2000.0, ask: float = 2000.20) -> None:
        self.demo = demo
        self.bid = bid
        self.ask = ask
        self._positions: dict[int, SimpleNamespace] = {}
        self._deals: list[SimpleNamespace] = []
        self._next_ticket = 1000
        self._connected = False
        self._error = (0, "ok")
        # test hooks:
        self.fail_order_check = False
        self.fail_order_send = False
        self.order_send_retcode = self.TRADE_RETCODE_DONE
        self.terminal_down = False

    # ------------------------------------------------------------ lifecycle
    def initialize(self, **kwargs) -> bool:
        self._connected = True
        return True

    def login(self, login, password=None, server=None) -> bool:
        return True

    def shutdown(self) -> None:
        self._connected = False

    def last_error(self):
        return self._error

    def terminal_info(self):
        if self.terminal_down or not self._connected:
            return None
        return SimpleNamespace(connected=True)

    def account_info(self):
        return SimpleNamespace(
            login=123456,
            server="FakeBroker-Demo",
            balance=25000.0,
            equity=25000.0,
            margin_free=25000.0,
            trade_mode=self.ACCOUNT_TRADE_MODE_DEMO if self.demo else self.ACCOUNT_TRADE_MODE_REAL,
        )

    # ------------------------------------------------------------ symbol
    def symbol_info(self, name):
        if name != "XAUUSD":
            return None
        return SimpleNamespace(
            name="XAUUSD", point=0.01, trade_tick_size=0.01, trade_tick_value=1.0,
            volume_min=0.01, volume_max=100.0, volume_step=0.01, digits=2,
            trade_mode=self.SYMBOL_TRADE_MODE_FULL, spread=20, visible=True,
        )

    def symbol_select(self, name, enable) -> bool:
        return name == "XAUUSD"

    def symbol_info_tick(self, name):
        return SimpleNamespace(bid=self.bid, ask=self.ask, time=0)

    # ------------------------------------------------------------ data
    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        return None  # integration tests drive execution directly, not scanning

    # ------------------------------------------------------------ state
    def positions_get(self, symbol=None):
        return tuple(self._positions.values())

    def orders_get(self, symbol=None):
        return tuple()

    def history_deals_get(self, from_date, to_date):
        return tuple(self._deals)

    # ------------------------------------------------------------ trading
    def order_check(self, request):
        if self.fail_order_check:
            return SimpleNamespace(retcode=10019, comment="Not enough money", margin=1e9)
        return SimpleNamespace(retcode=0, comment="ok", margin=100.0, balance=25000.0)

    def order_send(self, request):
        if self.fail_order_send:
            return SimpleNamespace(
                retcode=10013, comment="Invalid request", order=0, price=0.0, volume=0.0
            )
        action = request["action"]
        if action == self.TRADE_ACTION_SLTP:
            pos = self._positions.get(request["position"])
            if pos:
                pos.sl = request["sl"]
                pos.tp = request["tp"]
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="sltp",
                                   order=request["position"], price=0.0, volume=0.0)

        # DEAL: open new, or close/reduce an existing position
        if "position" in request:
            return self._close_deal(request)
        return self._open_deal(request)

    def _open_deal(self, request):
        ticket = self._next_ticket
        self._next_ticket += 1
        is_buy = request["type"] == self.ORDER_TYPE_BUY
        price = self.ask if is_buy else self.bid
        pos = SimpleNamespace(
            ticket=ticket, symbol=request["symbol"], type=request["type"],
            volume=request["volume"], price_open=price, sl=request["sl"], tp=request["tp"],
            profit=0.0, magic=request.get("magic", 0), position_id=ticket,
        )
        self._positions[ticket] = pos
        self._deals.append(SimpleNamespace(
            ticket=ticket, position_id=ticket, symbol=request["symbol"], entry=0,  # IN
            type=request["type"], volume=request["volume"], price=price, profit=0.0,
            commission=0.0, swap=0.0, magic=request.get("magic", 0),
            time=int(datetime.now(timezone.utc).timestamp()),
        ))
        return SimpleNamespace(retcode=self.order_send_retcode, comment="done",
                               order=ticket, price=price, volume=request["volume"])

    def _close_deal(self, request):
        ticket = request["position"]
        pos = self._positions.get(ticket)
        if pos is None:
            return SimpleNamespace(retcode=10013, comment="no position", order=0, price=0.0, volume=0.0)
        closing_buy = pos.type == self.ORDER_TYPE_BUY
        exit_price = self.bid if closing_buy else self.ask
        lot = request["volume"]
        move = (exit_price - pos.price_open) if closing_buy else (pos.price_open - exit_price)
        profit = move * self.USD_PER_UNIT * lot
        self._deals.append(SimpleNamespace(
            ticket=ticket, position_id=ticket, symbol=pos.symbol, entry=1,  # OUT
            type=request["type"], volume=lot, price=exit_price, profit=profit,
            commission=0.0, swap=0.0, magic=pos.magic,
            time=int(datetime.now(timezone.utc).timestamp()),
        ))
        pos.volume = round(pos.volume - lot, 8)
        if pos.volume <= 1e-9:
            del self._positions[ticket]
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="closed",
                               order=ticket, price=exit_price, volume=lot)

    # ------------------------------------------------------------ test helpers
    def move_price(self, bid: float, ask: float) -> None:
        self.bid, self.ask = bid, ask
        for pos in self._positions.values():
            closing_buy = pos.type == self.ORDER_TYPE_BUY
            exit_price = bid if closing_buy else ask
            move = (exit_price - pos.price_open) if closing_buy else (pos.price_open - exit_price)
            pos.profit = move * self.USD_PER_UNIT * pos.volume
