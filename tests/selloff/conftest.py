from datetime import date, timedelta

import pytest

from strategies.selloff.rules import Bar

LAST = date(2026, 10, 8)


def series(closes, range_pct=0.06, volume=2_000_000):
    """Daily bars ending on LAST, each spanning `range_pct` of its close (so
    ATR as a percent of close is about `range_pct`)."""
    n = len(closes)
    return [
        Bar(LAST - timedelta(days=n - 1 - i), c * (1 + range_pct / 2), c * (1 - range_pct / 2), c, volume)
        for i, c in enumerate(closes)
    ]


def selloff(drop=0.15, sessions=160, **kw):
    """A steady uptrend from 50 to 100 that then falls `drop` over the last
    three sessions: a textbook setup."""
    closes = [50 + 50 * i / (sessions - 4) for i in range(sessions - 3)]
    top = closes[-1]
    closes += [top * (1 - drop / 3), top * (1 - 2 * drop / 3), top * (1 - drop)]
    return series(closes, **kw)


@pytest.fixture
def make_series():
    return series


@pytest.fixture
def make_selloff():
    return selloff
