from dataclasses import replace
from datetime import date, timedelta

import pytest

from strategies.selloff.rules import Params, atr
from strategies.selloff.session import Holding, Order, StateFile, State, protect, run_session

LAST = date(2026, 10, 8)  # the last bar in conftest's series
TODAY = LAST + timedelta(days=1)
PARAMS = Params()
UNIVERSE = ["AAA", "BBB", "CCC", "HHH"]


@pytest.fixture
def market(make_series, make_selloff):
    def build(**overrides):
        """Bars for the universe: AAA and BBB sold off (BBB harder), CCC and
        HHH sit flat at 100 and are not candidates."""
        bars = {
            "AAA": make_selloff(drop=0.15),
            "BBB": make_selloff(drop=0.20),
            "CCC": make_series([100.0] * 160),
            "HHH": make_series([100.0] * 160),
        }
        bars.update(overrides)
        return bars

    return build


class FakeBroker:
    def __init__(self, bars, positions=None, orders=(), equity=100_000.0):
        self.bars = bars
        self.pos = dict(positions or {})
        self.working = list(orders)
        self.eq = equity
        self.bids = []
        self.stops = []
        self.cancelled = []
        self.closed = []
        self.asked = []

    def equity(self):
        return self.eq

    def positions(self):
        return dict(self.pos)

    def orders(self, symbols):
        self.asked.append(sorted(symbols))
        return [o for o in self.working if o.symbol in symbols]

    def daily_bars(self, symbols, before, sessions):
        return {s: [b for b in self.bars.get(s, []) if b.day < before][-sessions:] for s in symbols}

    def buy_limit(self, symbol, qty, limit):
        self.bids.append((symbol, qty, limit))

    def place_stop(self, symbol, qty, stop):
        self.stops.append((symbol, qty, stop))

    def cancel(self, order):
        self.cancelled.append(order.id)
        self.working.remove(order)

    def close(self, symbol):
        self.closed.append(symbol)


@pytest.fixture
def state(tmp_path):
    return StateFile(tmp_path / "state.json")


def stop_order(symbol, qty, price, id="s1"):
    return Order(id, symbol, "sell", "stop", qty, price)


def test_scans_the_universe_and_bids_on_the_biggest_selloffs_first(state, market):
    broker = FakeBroker(market())
    assert run_session(broker, UNIVERSE, PARAMS, state, TODAY)
    assert [b[0] for b in broker.bids] == ["BBB", "AAA"]
    for symbol, qty, limit in broker.bids:
        assert limit == round(market()[symbol][-1].close * 0.93, 2)
        assert qty * limit <= 10_000
    picks = state.load().decision.picks
    assert [(p.symbol, p.qty > 0) for p in picks] == [("BBB", True), ("AAA", True)]


def test_max_positions_limits_the_bids(state, market):
    broker = FakeBroker(market())
    run_session(broker, UNIVERSE, replace(PARAMS, max_positions=1), state, TODAY)
    assert [b[0] for b in broker.bids] == ["BBB"]
    assert [(p.symbol, p.qty) for p in state.load().decision.picks][1] == ("AAA", 0)


def test_gross_cap_limits_what_the_bids_commit(state, market):
    broker = FakeBroker(market())
    run_session(broker, UNIVERSE, replace(PARAMS, gross_cap=0.12), state, TODAY)
    committed = sum(q * px for _, q, px in broker.bids)
    assert committed <= 12_000
    assert len(broker.bids) == 2  # the second gets what is left of the cap


def test_only_its_own_symbols_are_read_or_touched(state, market):
    # KNX belongs to pairs: its position and its order are none of our business.
    knx_stop = stop_order("KNX", 50, 30.0, id="k")
    broker = FakeBroker(market(), positions={"KNX": Holding(-50, 40.0)}, orders=[knx_stop])
    run_session(broker, UNIVERSE, PARAMS, state, TODAY)
    assert broker.asked == [sorted(UNIVERSE)]
    assert broker.closed == [] and broker.cancelled == []
    assert "KNX" not in state.load().entered


def test_unfilled_bids_from_yesterday_are_cancelled(state, market):
    stale = Order("b1", "AAA", "buy", "limit", 100, 80.0)
    broker = FakeBroker(market(), orders=[stale])
    run_session(broker, UNIVERSE, PARAMS, state, TODAY)
    assert broker.cancelled == ["b1"]


def test_working_exit_defers_the_session(state, market):
    exit_order = Order("x1", "HHH", "sell", "market", 10)
    broker = FakeBroker(market(), positions={"HHH": Holding(10, 100.0)}, orders=[exit_order])
    assert not run_session(broker, UNIVERSE, PARAMS, state, TODAY)
    assert broker.bids == [] and broker.closed == []
    assert not state.path.exists()


def test_yesterdays_fill_gets_its_stop_and_keeps_its_entry_date(state, market):
    bars = market()
    entry_day = LAST
    state.save(State(last_session=entry_day, entered={}, decision=None))
    run_session(FakeBroker(bars), UNIVERSE, PARAMS, state, entry_day)  # bids BBB and AAA
    # Filled near the last close, so neither the target nor the stop is due.
    broker = FakeBroker(bars, positions={"BBB": Holding(50, 79.0)})
    run_session(broker, UNIVERSE, PARAMS, state, TODAY)
    expected = round(79.0 - 2.5 * atr([b for b in bars["BBB"] if b.day < entry_day], 10), 2)
    assert broker.stops == [("BBB", 50, expected)]
    assert state.load().entered["BBB"] == entry_day
    # Held, so not bid on again.
    assert "BBB" not in [b[0] for b in broker.bids]


def test_existing_matching_stop_is_left_alone(state, market):
    bars = market()
    entered = LAST - timedelta(days=1)
    stop = round(100.0 - 2.5 * atr([b for b in bars["HHH"] if b.day < entered], 10), 2)
    state.save(State(last_session=LAST, entered={"HHH": entered}))
    broker = FakeBroker(bars, positions={"HHH": Holding(10, 100.0)}, orders=[stop_order("HHH", 10, stop)])
    run_session(broker, UNIVERSE, PARAMS, state, TODAY)
    assert broker.stops == [] and broker.cancelled == []


def test_stop_with_the_wrong_quantity_is_replaced(state, market):
    state.save(State(last_session=LAST, entered={"HHH": LAST}))
    broker = FakeBroker(market(), positions={"HHH": Holding(10, 100.0)}, orders=[stop_order("HHH", 4, 85.0)])
    run_session(broker, UNIVERSE, PARAMS, state, TODAY)
    assert broker.cancelled == ["s1"]
    assert [(s, q) for s, q, _ in broker.stops] == [("HHH", 10)]


def test_profit_target_exits_after_cancelling_the_stop(state, market, make_series):
    bars = market(HHH=make_series([100.0] * 159 + [105.0]))
    state.save(State(last_session=LAST, entered={"HHH": LAST}))
    broker = FakeBroker(bars, positions={"HHH": Holding(10, 100.0)}, orders=[stop_order("HHH", 10, 85.0)])
    run_session(broker, UNIVERSE, PARAMS, state, TODAY)
    assert broker.cancelled == ["s1"]
    assert broker.closed == ["HHH"]
    assert state.load().decision.held[0].action == "exit_target"


def test_time_exit_after_three_closes(state, market):
    entered = LAST - timedelta(days=2)  # closes on LAST-2, LAST-1, LAST
    state.save(State(last_session=LAST, entered={"HHH": entered}))
    broker = FakeBroker(market(), positions={"HHH": Holding(10, 100.0)})
    run_session(broker, UNIVERSE, PARAMS, state, TODAY)
    assert broker.closed == ["HHH"]


def test_two_closes_is_not_yet_a_time_exit(state, market):
    state.save(State(last_session=LAST, entered={"HHH": LAST - timedelta(days=1)}))
    broker = FakeBroker(market(), positions={"HHH": Holding(10, 100.0)})
    run_session(broker, UNIVERSE, PARAMS, state, TODAY)
    assert broker.closed == []
    assert len(broker.stops) == 1


def test_exits_free_their_slot_for_todays_bids(state, market):
    state.save(State(last_session=LAST, entered={"HHH": LAST - timedelta(days=5)}))
    broker = FakeBroker(market(), positions={"HHH": Holding(10, 100.0)})
    run_session(broker, UNIVERSE, replace(PARAMS, max_positions=1), state, TODAY)
    assert broker.closed == ["HHH"]
    assert [b[0] for b in broker.bids] == ["BBB"]


def test_held_positions_take_slots(state, market):
    state.save(State(last_session=LAST, entered={"HHH": LAST}))
    broker = FakeBroker(market(), positions={"HHH": Holding(10, 100.0)})
    run_session(broker, UNIVERSE, replace(PARAMS, max_positions=1), state, TODAY)
    assert broker.bids == []


def test_orphan_long_is_adopted_and_protected(state, market):
    broker = FakeBroker(market(), positions={"CCC": Holding(20, 100.0)})
    run_session(broker, UNIVERSE, PARAMS, state, TODAY)
    assert state.load().entered["CCC"] == LAST
    assert [(s, q) for s, q, _ in broker.stops] == [("CCC", 20)]
    assert broker.closed == []


def test_orphan_short_is_closed(state, market):
    broker = FakeBroker(market(), positions={"CCC": Holding(-20, 100.0)})
    run_session(broker, UNIVERSE, PARAMS, state, TODAY)
    assert broker.closed == ["CCC"]
    assert "CCC" not in state.load().entered


def test_position_gone_since_last_session_is_forgotten(state, market):
    state.save(State(last_session=LAST, entered={"HHH": LAST - timedelta(days=1)}))
    run_session(FakeBroker(market()), UNIVERSE, PARAMS, state, TODAY)
    assert state.load().entered == {}


def test_symbol_without_history_is_skipped(state, market, make_selloff):
    bars = market(AAA=make_selloff()[-20:])
    broker = FakeBroker(bars)
    run_session(broker, UNIVERSE, PARAMS, state, TODAY)
    assert [b[0] for b in broker.bids] == ["BBB"]


def test_state_round_trips(state, market):
    run_session(FakeBroker(market()), UNIVERSE, PARAMS, state, TODAY)
    loaded = state.load()
    state.save(loaded)
    assert state.load() == loaded
    assert loaded.last_session == TODAY


# protect: between sessions


def test_protect_stops_a_fill_once_its_bid_is_done(state, market):
    run_session(FakeBroker(market()), UNIVERSE, PARAMS, state, TODAY)
    pick = state.load().decision.picks[0]
    filled = FakeBroker(market(), positions={pick.symbol: Holding(pick.qty, pick.limit)})
    protect(filled, PARAMS, state)
    assert filled.stops == [(pick.symbol, pick.qty, round(pick.limit - 2.5 * pick.atr, 2))]


def test_protect_waits_while_the_bid_is_still_working(state, market):
    run_session(FakeBroker(market()), UNIVERSE, PARAMS, state, TODAY)
    pick = state.load().decision.picks[0]
    rest = Order("b1", pick.symbol, "buy", "limit", pick.qty - 5, pick.limit)
    partial = FakeBroker(market(), positions={pick.symbol: Holding(5, pick.limit)}, orders=[rest])
    protect(partial, PARAMS, state)
    assert partial.stops == []


def test_protect_ignores_positions_it_did_not_bid_for(state, market):
    run_session(FakeBroker(market()), UNIVERSE, PARAMS, state, TODAY)
    broker = FakeBroker(market(), positions={"KNX": Holding(10, 40.0), "CCC": Holding(5, 100.0)})
    protect(broker, PARAMS, state)
    assert broker.stops == []


def test_protect_before_any_session_does_nothing(state, market):
    broker = FakeBroker(market(), positions={"AAA": Holding(10, 80.0)})
    protect(broker, PARAMS, state)
    assert broker.stops == [] and broker.asked == []


def test_started_without_state(state):
    assert state.load() == State()
