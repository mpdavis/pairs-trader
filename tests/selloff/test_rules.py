from dataclasses import replace
from datetime import date

import pytest

from strategies.selloff.rules import (
    Action,
    Bar,
    Candidate,
    Params,
    atr,
    atr_pct,
    average_volume,
    decide_exit,
    drop,
    evaluate,
    is_setup,
    parse_universe,
    passes_filter,
    rank,
    shares,
    stop_price,
)

P = Params()


def test_textbook_selloff_is_a_candidate(make_selloff):
    c = evaluate("AAA", make_selloff(), P)
    assert c is not None
    assert c.drop == pytest.approx(0.15)
    assert c.limit_price(P) == round(c.close * 0.93, 2)


def test_too_little_history_is_not_evaluated(make_selloff):
    assert evaluate("AAA", make_selloff()[-149:], P) is None


# Filter


def test_filter_minimum_price(make_selloff):
    bars = make_selloff()
    assert passes_filter(bars, P)
    assert not passes_filter(bars, replace(P, min_price=bars[-1].close + 0.01))


def test_filter_minimum_average_volume(make_selloff):
    assert not passes_filter(make_selloff(volume=999_999), P)
    assert passes_filter(make_selloff(volume=1_000_000), P)


def test_volume_averages_the_whole_window_not_one_bar(make_series):
    """Reference bug: iloc[-50] read the single bar 50 sessions back. One
    heavy day must not carry 49 thin ones."""
    bars = make_series([10.0] * 60, volume=100_000)
    bars[-50] = replace(bars[-50], volume=40_000_000)
    assert average_volume(bars, 50) == pytest.approx((49 * 100_000 + 40_000_000) / 50)
    assert average_volume(bars, 50) < 1_000_000
    bars = make_series([10.0] * 60, volume=2_000_000)
    bars[-50] = replace(bars[-50], volume=10)
    assert average_volume(bars, 50) > 1_000_000


@pytest.mark.parametrize(("range_pct", "expected"), [(0.049, False), (0.051, True), (0.08, True)])
def test_filter_minimum_atr_percent(make_series, range_pct, expected):
    assert passes_filter(make_series([100.0] * 60, range_pct=range_pct), P) is expected


def test_atr_filter_is_relative_to_the_close():
    """Reference bug: raw ATR was compared with 0.05, so any stock over a few
    dollars passed. A $200 stock moving $2 a day has ATR 2.0 but only 1%."""
    bars = [Bar(date(2026, 1, 1 + i), 201.0, 199.0, 200.0, 2e6) for i in range(20)]
    assert atr(bars, 10) == pytest.approx(2.0)
    assert atr(bars, 10) > 0.05
    assert atr_pct(bars, 10) == pytest.approx(0.01)
    assert not passes_filter(bars, P)


def test_atr_counts_gaps_through_the_previous_close():
    bars = [
        Bar(date(2026, 1, 1), 101, 99, 100, 1),
        Bar(date(2026, 1, 2), 92, 90, 91, 1),  # gapped down: TR = 100 - 90
        Bar(date(2026, 1, 3), 93, 89, 92, 1),  # inside: TR = 93 - 89
    ]
    assert atr(bars, 2) == pytest.approx((10 + 4) / 2)


# Setup


def test_setup_needs_the_close_above_its_150_day_average(make_series):
    # A downtrend that drops another 15%: oversold, but not a pullback in an uptrend.
    closes = [100 - 0.3 * i for i in range(157)]
    closes += [closes[-1] * 0.95, closes[-1] * 0.9, closes[-1] * 0.85]
    bars = make_series(closes)
    assert drop(bars, 3) == pytest.approx(0.15)
    assert not is_setup(bars, P)


@pytest.mark.parametrize(("fall", "expected"), [(0.20, True), (0.125, True), (0.12, False), (0.0, False)])
def test_setup_takes_drops_of_at_least_12_5_percent(make_selloff, fall, expected):
    """Reference bug: the check was inverted (`close < 0.875 * close[-4]`
    rejected), so it threw away exactly the stocks that had sold off."""
    assert is_setup(make_selloff(drop=fall), P) is expected


def test_drop_is_measured_over_three_sessions(make_series):
    bars = make_series([100, 100, 90, 95, 80])
    assert drop(bars, 3) == pytest.approx(0.2)


# Ranking and orders


def cand(symbol, d):
    return Candidate(symbol, close=50.0, drop=d, atr=3.0)


def test_rank_takes_the_largest_drops_up_to_the_open_slots():
    cs = [cand("A", 0.13), cand("B", 0.30), cand("C", 0.20), cand("D", 0.20)]
    assert [c.symbol for c in rank(cs, 3)] == ["B", "C", "D"]
    assert rank(cs, 0) == []
    assert rank(cs, -2) == []


def test_limit_is_seven_percent_under_the_last_close():
    assert Candidate("A", close=50.0, drop=0.2, atr=3.0).limit_price(P) == 46.50


def test_stop_is_two_and_a_half_atrs_under_the_fill():
    assert stop_price(46.50, 3.0, P) == 39.00


def test_shares_risk_two_percent_of_equity():
    # 2% of 100k = 2,000 risked with the stop 7.50 away: 266 shares, $5,320.
    assert shares(100_000, 20.0, 3.0, budget=1e9, p=P) == 266


def test_shares_capped_at_ten_percent_of_equity():
    # Risk alone would allow 2000 / 2.5 = 800 shares of a $46.50 stock = 37k.
    assert shares(100_000, 46.50, 1.0, budget=1e9, p=P) == int(10_000 / 46.50)


def test_shares_capped_by_remaining_budget():
    assert shares(100_000, 46.50, 1.0, budget=930, p=P) == 20
    assert shares(100_000, 46.50, 1.0, budget=-5, p=P) == 0


def test_shares_zero_without_a_stop_distance():
    assert shares(100_000, 46.50, 0.0, budget=1e9, p=P) == 0


# Exits


@pytest.mark.parametrize(
    ("close", "held", "expected"),
    [
        (104.0, 1, Action.EXIT_TARGET),  # exactly 4% up
        (103.9, 1, Action.HOLD),
        (101.0, 2, Action.HOLD),
        (101.0, 3, Action.EXIT_TIME),  # three closes without stop or target
        (110.0, 3, Action.EXIT_TARGET),
        (90.0, 1, Action.EXIT_STOP),  # closed through a stop that never fired
        (90.01, 1, Action.HOLD),
    ],
)
def test_exits(close, held, expected):
    assert decide_exit(100.0, 90.0, close, held, P) is expected


# Universe


def test_default_universe_is_a_scan_not_one_ticker():
    """Reference bug: one hardcoded ticker. The default is a list, and has none
    of the symbols pairs trades by default."""
    u = parse_universe("")
    assert len(u) > 100
    assert len(u) == len(set(u))
    assert not {"JBHT", "KNX", "UPS", "ODFL"} & set(u)


def test_parse_universe_override():
    assert parse_universe("nvda, AMD,,nvda\nTSLA # comment") == ["NVDA", "AMD", "TSLA"]
    with pytest.raises(ValueError):
        parse_universe("# nothing here")
