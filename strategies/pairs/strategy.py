"""The pairs rules as a LumiBot strategy. LumiBot owns scheduling, the broker
connection and backtesting; each iteration hands `session.run_session` a
`Broker` view of the strategy, so the decisions come from the tested code in
`rules.py` and `session.py`."""

from __future__ import annotations

from datetime import date

from lumibot.strategies import Strategy

from algo_trading.base import ManagedStrategy

from .rules import Params, parse_pairs
from .session import StateFile, run_session


class LumibotBroker:
    """The `session.Broker` protocol over a running LumiBot strategy."""

    def __init__(self, strategy: Strategy) -> None:
        self.s = strategy

    def equity(self) -> float:
        return float(self.s.portfolio_value)

    def positions(self) -> dict[str, int]:
        # LumiBot reports shorts as negative quantities.
        return {
            pos.asset.symbol: int(float(pos.quantity))
            for pos in self.s.get_positions()
            if getattr(pos.asset, "asset_type", "stock") == "stock"
        }

    def open_orders(self, symbols: list[str]) -> bool:
        return any(o.asset.symbol in symbols and o.is_active() for o in self.s.get_orders())

    def daily_closes(self, symbols: list[str], before: date, sessions: int) -> dict[str, dict[date, float]]:
        out: dict[str, dict[date, float]] = {}
        for symbol in symbols:
            bars = self.s.get_historical_prices(symbol, sessions + 5, "day")
            frame = bars.df if bars is not None else None
            out[symbol] = {} if frame is None else {
                ts.date(): float(close) for ts, close in frame["close"].items() if ts.date() < before
            }
        return out

    def shortable(self, symbol: str) -> bool:
        # Backtests have no borrow constraints; live Alpaca exposes its client.
        api = getattr(self.s.broker, "api", None)
        if self.s.is_backtesting or api is None:
            return True
        asset = api.get_asset(symbol)
        return bool(asset.shortable and asset.easy_to_borrow)

    def submit(self, symbol: str, qty: int) -> None:
        side = "buy" if qty > 0 else "sell_short"
        self.s.submit_order(self.s.create_order(symbol, abs(qty), side))

    def close(self, symbol: str) -> None:
        self.s.close_position(symbol)


class PairsStrategy(ManagedStrategy):
    parameters = {
        **ManagedStrategy.parameters,
        "pairs": "JBHT/KNX,UPS/ODFL",
        "lookback": 60,
        "entry_z": 2.0,
        "exit_z": 0.5,
        "stop_z": 3.5,
        "max_hold_days": 20,
        "pair_gross": 0.5,
    }

    @classmethod
    def symbols(cls, parameters: dict) -> set[str]:
        return {s for p in parse_pairs(parameters["pairs"]) for s in (p.y, p.x)}

    def initialize(self) -> None:
        # Backtests step one day at a time. Live polls instead of using "1D":
        # LumiBot schedules a daily strategy started mid-session at the start
        # time, not the open, so a late restart could skip trading for good.
        # The state file keeps it to one session per day.
        self.sleeptime = "1D" if self.is_backtesting else "5M"
        self.set_market("NYSE")
        p = self.parameters
        self.pairs = parse_pairs(p["pairs"])
        self.rules = Params(
            lookback=int(p["lookback"]),
            entry_z=float(p["entry_z"]),
            exit_z=float(p["exit_z"]),
            stop_z=float(p["stop_z"]),
            max_hold_days=int(p["max_hold_days"]),
            pair_gross=float(p["pair_gross"]),
        )
        self.state = StateFile(self.data_dir / "state.json")
        self.view = LumibotBroker(self)

    def iterate(self) -> None:
        today = self.get_datetime().date()
        last, _ = self.state.load()
        if last != today:
            run_session(self.view, self.pairs, self.rules, self.state, today)
