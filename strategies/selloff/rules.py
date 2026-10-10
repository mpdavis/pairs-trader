"""The trading rules, free of any broker or data source so the backtest and the
live runner make identical decisions from identical bars."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path

DEFAULT_UNIVERSE = Path(__file__).with_name("universe.txt")


@dataclass(frozen=True)
class Bar:
    day: date
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Params:
    max_positions: int = 10
    min_price: float = 1.0
    min_avg_volume: int = 1_000_000
    volume_window: int = 50
    atr_window: int = 10
    # ATR as a fraction of the close: the stock must move enough for a 7%
    # discount to be an ordinary day's range rather than a collapse.
    min_atr_pct: float = 0.05
    sma_window: int = 150
    drop_window: int = 3
    min_drop: float = 0.125
    limit_discount: float = 0.07
    stop_atr: float = 2.5
    profit_target: float = 0.04
    max_hold_days: int = 3
    risk_per_trade: float = 0.02
    max_position: float = 0.10
    # Share of account equity this strategy may hold, so it leaves room for
    # the others trading the same account.
    gross_cap: float = 0.5

    @property
    def history(self) -> int:
        """Bars a symbol needs before every rule can be evaluated."""
        return max(self.sma_window, self.volume_window, self.atr_window + 1, self.drop_window + 1)


def parse_universe(raw: str) -> list[str]:
    """Comma- or newline-separated symbols; `#` starts a comment. An empty
    string means the default list in universe.txt."""
    if not raw.strip():
        raw = DEFAULT_UNIVERSE.read_text()
    symbols = []
    for line in raw.splitlines():
        for item in line.split("#", 1)[0].split(","):
            symbol = item.strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    if not symbols:
        raise ValueError("the universe is empty")
    return symbols


def average_volume(bars: list[Bar], window: int) -> float:
    # Every one of the last `window` bars, not the single bar `window` back.
    return statistics.fmean(b.volume for b in bars[-window:])


def atr(bars: list[Bar], window: int) -> float:
    """Average true range over the last `window` bars, as a simple mean. Each
    true range needs the bar before it, so this reads `window + 1` bars."""
    recent = bars[-(window + 1):]
    ranges = [
        max(cur.high, prev.close) - min(cur.low, prev.close)
        for prev, cur in zip(recent, recent[1:])
    ]
    return statistics.fmean(ranges)


def atr_pct(bars: list[Bar], window: int) -> float:
    return atr(bars, window) / bars[-1].close


def sma(bars: list[Bar], window: int) -> float:
    return statistics.fmean(b.close for b in bars[-window:])


def drop(bars: list[Bar], window: int) -> float:
    """How far the last close is below the close `window` sessions earlier, as
    a positive fraction: 0.15 is a 15% fall."""
    return 1 - bars[-1].close / bars[-1 - window].close


def passes_filter(bars: list[Bar], p: Params) -> bool:
    return (
        bars[-1].close >= p.min_price
        and average_volume(bars, p.volume_window) >= p.min_avg_volume
        and atr_pct(bars, p.atr_window) >= p.min_atr_pct
    )


def is_setup(bars: list[Bar], p: Params) -> bool:
    return bars[-1].close > sma(bars, p.sma_window) and drop(bars, p.drop_window) >= p.min_drop


@dataclass(frozen=True)
class Candidate:
    symbol: str
    close: float
    drop: float
    atr: float

    def limit_price(self, p: Params) -> float:
        return round(self.close * (1 - p.limit_discount), 2)


def evaluate(symbol: str, bars: list[Bar], p: Params) -> Candidate | None:
    """A candidate when `bars` (oldest first, ending with the last close)
    pass the filter and the setup."""
    if len(bars) < p.history or not passes_filter(bars, p) or not is_setup(bars, p):
        return None
    return Candidate(symbol, bars[-1].close, drop(bars, p.drop_window), atr(bars, p.atr_window))


def rank(candidates: list[Candidate], slots: int) -> list[Candidate]:
    """The largest three-day drops first; ties by symbol so reruns agree."""
    ordered = sorted(candidates, key=lambda c: (-c.drop, c.symbol))
    return ordered[: max(slots, 0)]


def stop_price(fill: float, entry_atr: float, p: Params) -> float:
    return round(fill - p.stop_atr * entry_atr, 2)


def shares(equity: float, entry: float, entry_atr: float, budget: float, p: Params) -> int:
    """Whole shares risking `risk_per_trade` of equity between `entry` and the
    stop, no more than `max_position` of equity, and no more than `budget`."""
    risk_per_share = p.stop_atr * entry_atr
    if entry <= 0 or risk_per_share <= 0:
        return 0
    by_risk = equity * p.risk_per_trade / risk_per_share
    by_size = min(equity * p.max_position, budget) / entry
    return max(int(min(by_risk, by_size)), 0)


class Action(str, Enum):
    HOLD = "hold"
    EXIT_TARGET = "exit_target"
    EXIT_TIME = "exit_time"
    EXIT_STOP = "exit_stop"


EXITS = {Action.EXIT_TARGET, Action.EXIT_TIME, Action.EXIT_STOP}


def decide_exit(entry: float, stop: float, last_close: float, held_closes: int, p: Params) -> Action:
    """What to do at the next session with a position bought at `entry`.
    `held_closes` counts the closes since the fill, the entry day's included.
    The stop order normally exits intraday; a close already below it (the
    order was missing, or the stock gapped through) exits at the next session."""
    if last_close <= stop:
        return Action.EXIT_STOP
    if last_close >= entry * (1 + p.profit_target):
        return Action.EXIT_TARGET
    if held_closes >= p.max_hold_days:
        return Action.EXIT_TIME
    return Action.HOLD
