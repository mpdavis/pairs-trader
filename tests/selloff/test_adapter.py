from datetime import date
from types import SimpleNamespace

import pandas as pd

from strategies.selloff.rules import Bar, Params
from strategies.selloff.session import Holding
from strategies.selloff.strategy import LumibotBroker, SelloffStrategy, rules_from


class FakeStrategy:
    is_backtesting = True
    portfolio_value = 100_000.0

    def __init__(self, positions=(), orders=(), frames=None):
        self._positions = positions
        self._orders = orders
        self._frames = frames or {}
        self.created = []
        self.submitted = []
        self.cancelled = []
        self.closed = []

    def get_positions(self):
        return list(self._positions)

    def get_orders(self):
        return list(self._orders)

    def get_historical_prices(self, symbol, length, timestep):
        assert timestep == "day"
        return SimpleNamespace(df=self._frames.get(symbol))

    def create_order(self, symbol, quantity, side, **kw):
        return (symbol, quantity, side, kw)

    def submit_order(self, order):
        self.submitted.append(order)

    def cancel_order(self, order):
        self.cancelled.append(order)

    def close_position(self, symbol):
        self.closed.append(symbol)


def position(symbol, qty, price, asset_type="stock"):
    return SimpleNamespace(asset=SimpleNamespace(symbol=symbol, asset_type=asset_type), quantity=qty,
                           avg_fill_price=price)


def order(symbol, side, kind, qty, stop=None, limit=None, active=True, id="o1"):
    return SimpleNamespace(asset=SimpleNamespace(symbol=symbol), side=side, order_type=kind, quantity=qty,
                           stop_price=stop, limit_price=limit, identifier=id, is_active=lambda: active)


def test_positions_carry_quantity_and_entry_price():
    s = FakeStrategy(positions=[position("AAA", 10, 46.5), position("BBB", -7.0, 20), position("USD", 500, 1, "forex")])
    assert LumibotBroker(s).positions() == {"AAA": Holding(10, 46.5), "BBB": Holding(-7, 20.0)}


def test_orders_lists_active_orders_for_the_given_symbols_only():
    s = FakeStrategy(orders=[
        order("AAA", "sell", "stop", 10, stop=39.0, id="a"),
        order("AAA", "buy", "limit", 5, limit=46.5, active=False, id="b"),
        order("KNX", "sell", "stop", 3, stop=30.0, id="c"),
    ])
    (o,) = LumibotBroker(s).orders(["AAA", "BBB"])
    assert (o.id, o.symbol, o.side, o.kind, o.qty, o.price) == ("a", "AAA", "sell", "stop", 10, 39.0)


def test_cancel_hands_back_lumibots_own_order():
    lumi = order("AAA", "sell", "stop", 10, stop=39.0, id="a")
    s = FakeStrategy(orders=[lumi])
    broker = LumibotBroker(s)
    (o,) = broker.orders(["AAA"])
    broker.cancel(o)
    assert s.cancelled == [lumi]


def test_daily_bars_drop_the_current_session():
    index = pd.to_datetime(["2026-10-07 16:00", "2026-10-08 16:00", "2026-10-09 16:00"]).tz_localize("America/New_York")
    frame = pd.DataFrame({"high": [11, 12, 13.0], "low": [9, 10, 11.0], "close": [10, 11, 12.0],
                          "volume": [1e6, 2e6, 3e6]}, index=index)
    bars = LumibotBroker(FakeStrategy(frames={"AAA": frame})).daily_bars(["AAA", "BBB"], before=date(2026, 10, 9), sessions=2)
    assert bars == {
        "AAA": [Bar(date(2026, 10, 7), 11, 9, 10, 1e6), Bar(date(2026, 10, 8), 12, 10, 11, 2e6)],
        "BBB": [],
    }


def test_entry_is_a_day_limit_and_the_stop_is_good_till_cancelled():
    s = FakeStrategy()
    broker = LumibotBroker(s)
    broker.buy_limit("AAA", 10, 46.5)
    broker.place_stop("AAA", 10, 39.0)
    assert s.submitted == [
        ("AAA", 10, "buy", {"limit_price": 46.5, "time_in_force": "day"}),
        ("AAA", 10, "sell", {"stop_price": 39.0, "time_in_force": "gtc"}),
    ]


def test_close_closes_only_the_named_symbol():
    s = FakeStrategy()
    LumibotBroker(s).close("AAA")
    assert s.closed == ["AAA"]


def test_every_rule_parameter_is_a_strategy_parameter():
    p = SelloffStrategy.parameters
    assert rules_from(p) == Params()
    assert p["universe"] == ""


def test_rules_from_casts_overrides():
    p = {**SelloffStrategy.parameters, "max_positions": "5", "min_drop": "0.1", "min_avg_volume": "500000"}
    r = rules_from(p)
    assert (r.max_positions, r.min_drop, r.min_avg_volume) == (5, 0.1, 500_000)


def test_symbols_follow_the_universe_parameter():
    assert SelloffStrategy.symbols({"universe": "aaa,bbb"}) == {"AAA", "BBB"}
    assert len(SelloffStrategy.symbols({"universe": ""})) > 100


def test_never_sells_everything():
    # Shared account: a strategy-wide liquidation would close pairs' positions too.
    import inspect

    import strategies.selloff.session as session
    import strategies.selloff.strategy as strategy

    for module in (session, strategy):
        assert "sell_all" not in inspect.getsource(module)
        assert "cancel_open_orders" not in inspect.getsource(module)

