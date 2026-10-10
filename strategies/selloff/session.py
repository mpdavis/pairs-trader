"""One trading session: read yesterday's bars and the account, exit what is
due, keep a stop under everything held, and bid for the day's candidates.
Account positions are the source of truth for what is held; the state file
only remembers what positions cannot (each entry's date), and the last
session's decision."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

from .rules import (
    EXITS,
    Action,
    Bar,
    Candidate,
    Params,
    atr,
    decide_exit,
    evaluate,
    rank,
    shares,
    stop_price,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Holding:
    qty: int
    avg_price: float


@dataclass(frozen=True)
class Order:
    id: str
    symbol: str
    side: str  # "buy" or "sell"
    kind: str  # "market", "limit", "stop", ...
    qty: int
    price: float | None = None


class Broker(Protocol):
    def equity(self) -> float: ...
    def positions(self) -> dict[str, Holding]: ...
    def orders(self, symbols: list[str]) -> list[Order]: ...
    def daily_bars(self, symbols: list[str], before: date, sessions: int) -> dict[str, list[Bar]]: ...
    def buy_limit(self, symbol: str, qty: int, limit: float) -> None: ...
    def place_stop(self, symbol: str, qty: int, stop: float) -> None: ...
    def cancel(self, order: Order) -> None: ...
    def close(self, symbol: str) -> None: ...


@dataclass(frozen=True)
class Pick:
    """A candidate as the session saw it, and the order it placed (qty 0 when
    there was no slot or budget left)."""

    symbol: str
    close: float
    drop: float
    atr: float
    limit: float
    qty: int


@dataclass(frozen=True)
class Held:
    """A position as the session saw it, for the dashboard."""

    symbol: str
    entered: str
    qty: int
    entry: float
    stop: float
    target: float
    last_close: float
    held_closes: int
    action: str


@dataclass
class Decision:
    session: date
    picks: list[Pick] = field(default_factory=list)
    held: list[Held] = field(default_factory=list)


@dataclass
class State:
    last_session: date | None = None
    entered: dict[str, date] = field(default_factory=dict)
    decision: Decision | None = None


class StateFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> State:
        if not self.path.exists():
            return State()
        raw = json.loads(self.path.read_text())
        d = raw.get("decision")
        decision = None
        if d:
            decision = Decision(
                session=date.fromisoformat(d["session"]),
                picks=[Pick(**p) for p in d.get("picks", [])],
                held=[Held(**h) for h in d.get("held", [])],
            )
        last = raw.get("last_session")
        return State(
            last_session=date.fromisoformat(last) if last else None,
            entered={s: date.fromisoformat(v) for s, v in raw.get("entered", {}).items()},
            decision=decision,
        )

    def save(self, state: State) -> None:
        d = state.decision
        raw = {
            "last_session": state.last_session.isoformat() if state.last_session else None,
            "entered": {s: v.isoformat() for s, v in sorted(state.entered.items())},
            "decision": None if d is None else {
                "session": d.session.isoformat(),
                "picks": [asdict(p) for p in d.picks],
                "held": [asdict(h) for h in d.held],
            },
        }
        # Write-then-rename, so a crash mid-write cannot leave a truncated file.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2))
        os.replace(tmp, self.path)


def entry_atr(bars: list[Bar], entered: date, p: Params) -> float:
    """The ATR the entry was sized on: from the bars before the entry day. An
    adopted position may predate the history fetched; it falls back to the
    latest ATR rather than leaving the position without a stop."""
    before = [b for b in bars if b.day < entered]
    return atr(before if len(before) > p.atr_window else bars, p.atr_window)


def run_session(broker: Broker, universe: list[str], params: Params, state: StateFile, today: date) -> bool:
    """Returns False when the session must be retried later."""
    prev = state.load()
    ours = set(universe)

    orders = broker.orders(universe)
    # Entry bids are good for their day only. Live, Alpaca has already expired
    # them; anything still working is cancelled so it cannot fill unwatched.
    for o in orders:
        if o.side == "buy":
            log.info("%s: cancelling unfilled entry bid for %d", o.symbol, o.qty)
            broker.cancel(o)
    # An exit still working means the account is about to change under us.
    if any(o.side == "sell" and o.kind != "stop" for o in orders):
        log.warning("exit orders still open; deferring the session")
        return False
    stops = [o for o in orders if o.side == "sell" and o.kind == "stop"]

    positions = {s: h for s, h in broker.positions().items() if s in ours and h.qty}
    bars = broker.daily_bars(universe, before=today, sessions=params.history + 10)
    equity = broker.equity()
    log.info("session %s equity=%.2f positions=%s", today, equity, {s: h.qty for s, h in positions.items()})

    # Reconcile what the account holds with what the last session left.
    bid_last = {p.symbol for p in prev.decision.picks if p.qty} if prev.decision else set()
    entered: dict[str, date] = {}
    for symbol, h in sorted(positions.items()):
        if h.qty < 0:
            # This strategy only buys; a short in one of its symbols is not its own.
            log.error("%s: short %d found in the account; closing it", symbol, h.qty)
            _close(broker, symbol, stops)
            continue
        if symbol in prev.entered:
            entered[symbol] = prev.entered[symbol]
        elif symbol in bid_last:
            entered[symbol] = prev.last_session
        else:
            first = bars.get(symbol)[-1].day if bars.get(symbol) else today
            log.warning("%s: adopting %d shares found in the account", symbol, h.qty)
            entered[symbol] = first
    for symbol in sorted(set(prev.entered) - set(entered)):
        log.info("%s: no longer held (stopped out or closed by hand)", symbol)
    positions = {s: h for s, h in positions.items() if s in entered}

    held_views: list[Held] = []
    exiting: set[str] = set()
    staying_value = 0.0
    for symbol in sorted(entered):
        h, b = positions[symbol], bars.get(symbol) or []
        if len(b) <= params.atr_window:
            log.error("%s: only %d bars, cannot price a stop; closing", symbol, len(b))
            _close(broker, symbol, stops)
            exiting.add(symbol)
            continue
        stop = stop_price(h.avg_price, entry_atr(b, entered[symbol], params), params)
        target = round(h.avg_price * (1 + params.profit_target), 2)
        held_closes = sum(1 for x in b if x.day >= entered[symbol])
        action = decide_exit(h.avg_price, stop, b[-1].close, held_closes, params)
        log.info("%s: entry=%.2f stop=%.2f target=%.2f close=%.2f held=%d -> %s",
                 symbol, h.avg_price, stop, target, b[-1].close, held_closes, action.value)
        if action in EXITS:
            _close(broker, symbol, stops)
            exiting.add(symbol)
        else:
            _ensure_stop(broker, symbol, h.qty, stop, stops)
            staying_value += h.qty * b[-1].close
        held_views.append(Held(symbol, entered[symbol].isoformat(), h.qty, h.avg_price, stop, target,
                               b[-1].close, held_closes, action.value))

    # Entries: the day's selloffs, biggest first, into the slots left.
    slots = params.max_positions - (len(entered) - len(exiting))
    budget = params.gross_cap * equity - staying_value
    found: list[Candidate] = []
    for symbol in universe:
        if symbol in positions:
            continue
        c = evaluate(symbol, bars.get(symbol) or [], params)
        if c is not None:
            found.append(c)
    chosen = {c.symbol for c in rank(found, slots)}
    picks: list[Pick] = []
    for c in sorted(found, key=lambda c: (-c.drop, c.symbol)):
        limit = c.limit_price(params)
        qty = 0
        if c.symbol in chosen:
            qty = shares(equity, limit, c.atr, budget, params)
            if qty > 0:
                log.info("%s: down %.1f%% in %d sessions; BUY %d limit %.2f", c.symbol, c.drop * 100,
                         params.drop_window, qty, limit)
                broker.buy_limit(c.symbol, qty, limit)
                budget -= qty * limit
            else:
                log.info("%s: no budget left for an entry", c.symbol)
        picks.append(Pick(c.symbol, round(c.close, 2), round(c.drop, 4), round(c.atr, 4), limit, qty))
    log.info("%d candidates, %d bids, %d held, %d exiting", len(found), sum(1 for p in picks if p.qty),
             len(entered), len(exiting))

    state.save(State(last_session=today, entered=entered, decision=Decision(today, picks, held_views)))
    return True


def protect(broker: Broker, params: Params, state: StateFile) -> None:
    """Put a stop under any of today's bids that has filled. Between sessions
    this is all that runs, so a fill is not left unprotected until tomorrow.
    A stop waits until the bid is done: Alpaca rejects a sell stop while a buy
    in the same symbol is still working, as a potential wash trade."""
    s = state.load()
    if s.decision is None or s.decision.session != s.last_session:
        return
    bids = {p.symbol: p for p in s.decision.picks if p.qty}
    if not bids:
        return
    orders = broker.orders(sorted(bids))
    working = {o.symbol for o in orders if o.side == "buy"}
    stops = [o for o in orders if o.side == "sell" and o.kind == "stop"]
    for symbol, h in broker.positions().items():
        if symbol not in bids or h.qty <= 0 or symbol in working:
            continue
        _ensure_stop(broker, symbol, h.qty, stop_price(h.avg_price, bids[symbol].atr, params), stops)


def _ensure_stop(broker: Broker, symbol: str, qty: int, stop: float, stops: list[Order]) -> None:
    mine = [o for o in stops if o.symbol == symbol]
    if len(mine) == 1 and mine[0].qty == qty and mine[0].price is not None and abs(mine[0].price - stop) < 0.005:
        return
    for o in mine:
        broker.cancel(o)
    log.info("%s: stop %d at %.2f", symbol, qty, stop)
    broker.place_stop(symbol, qty, stop)


def _close(broker: Broker, symbol: str, stops: list[Order]) -> None:
    # The stop holds the shares; it has to go first, or the close is
    # rejected live and in a backtest leaves a stop that would open a short.
    for o in stops:
        if o.symbol == symbol:
            broker.cancel(o)
    log.info("%s: closing", symbol)
    broker.close(symbol)
