"""The selloff rules as a LumiBot strategy. LumiBot owns scheduling, the broker
connection and backtesting; each iteration hands `session.run_session` a
`Broker` view of the strategy, so the decisions come from the tested code in
`rules.py` and `session.py`."""

from __future__ import annotations

import time
from dataclasses import asdict, fields
from datetime import date

from lumibot.strategies import Strategy

from algo_trading.base import ManagedStrategy

from .rules import Bar, Params, parse_universe
from .session import Holding, Order, StateFile, protect, run_session


class LumibotBroker:
    """The `session.Broker` protocol over a running LumiBot strategy."""

    def __init__(self, strategy: Strategy) -> None:
        self.s = strategy
        # Order ids handed to the session, back to LumiBot's own objects.
        self._orders: dict[str, object] = {}

    def equity(self) -> float:
        return float(self.s.portfolio_value)

    def positions(self) -> dict[str, Holding]:
        # LumiBot reports shorts as negative quantities.
        return {
            pos.asset.symbol: Holding(int(float(pos.quantity)), float(pos.avg_fill_price or 0))
            for pos in self.s.get_positions()
            if getattr(pos.asset, "asset_type", "stock") == "stock"
        }

    def orders(self, symbols: list[str]) -> list[Order]:
        wanted = set(symbols)
        out = []
        for o in self.s.get_orders():
            if o.asset.symbol not in wanted or not o.is_active():
                continue
            kind = str(o.order_type or "market")
            price = o.stop_price if kind == "stop" else o.limit_price
            self._orders[o.identifier] = o
            out.append(Order(o.identifier, o.asset.symbol, str(o.side), kind, int(float(o.quantity)),
                             None if price is None else float(price)))
        return out

    def daily_bars(self, symbols: list[str], before: date, sessions: int) -> dict[str, list[Bar]]:
        out: dict[str, list[Bar]] = {}
        for symbol in symbols:
            bars = self.s.get_historical_prices(symbol, sessions + 5, "day")
            frame = bars.df if bars is not None else None
            out[symbol] = [] if frame is None else [
                Bar(ts.date(), float(r.high), float(r.low), float(r.close), float(r.volume))
                for ts, r in frame.iterrows()
                if ts.date() < before
            ]
        return out

    def buy_limit(self, symbol: str, qty: int, limit: float) -> None:
        self.s.submit_order(self.s.create_order(symbol, qty, "buy", limit_price=limit, time_in_force="day"))

    def place_stop(self, symbol: str, qty: int, stop: float) -> None:
        self.s.submit_order(self.s.create_order(symbol, qty, "sell", stop_price=stop, time_in_force="gtc"))

    def cancel(self, order: Order) -> None:
        o = self._orders.pop(order.id, None)
        if o is None:
            return
        self.s.cancel_order(o)
        if self.s.is_backtesting:
            return
        # Alpaca holds a position's shares for its stop until the cancel lands;
        # a close sent before then is rejected for insufficient quantity.
        for _ in range(20):
            if not o.is_active():
                return
            time.sleep(0.5)

    def close(self, symbol: str) -> None:
        self.s.close_position(symbol)


def rules_from(parameters: dict) -> Params:
    """Params from the strategy's parameters, cast to each field's type, so an
    environment override like SELLOFF_MAX_POSITIONS=5 arrives as an int."""
    return Params(**{f.name: type(getattr(Params(), f.name))(parameters[f.name]) for f in fields(Params)})


class SelloffStrategy(ManagedStrategy):
    parameters = {
        **ManagedStrategy.parameters,
        # Comma-separated symbols; empty means the list in universe.txt.
        "universe": "",
        **asdict(Params()),
    }

    @classmethod
    def symbols(cls, parameters: dict) -> set[str]:
        return set(parse_universe(parameters["universe"]))

    def initialize(self) -> None:
        # As in pairs: one day at a time in backtests; live polls so a late
        # restart still trades, and so a fill gets its stop within minutes.
        self.sleeptime = "1D" if self.is_backtesting else "5M"
        self.set_market("NYSE")
        self.universe = parse_universe(self.parameters["universe"])
        self.rules = rules_from(self.parameters)
        self.state = StateFile(self.data_dir / "state.json")
        self.view = LumibotBroker(self)

    def iterate(self) -> None:
        today = self.get_datetime().date()
        if self.state.load().last_session != today:
            if not run_session(self.view, self.universe, self.rules, self.state, today):
                return
        protect(self.view, self.rules, self.state)
