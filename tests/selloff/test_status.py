from datetime import date

from strategies.selloff import status
from strategies.selloff.session import Decision, Held, Pick, State, StateFile


def test_status_before_first_session(tmp_path):
    s = status(tmp_path)
    assert s.facts == [("Last session", "never")]
    assert s.rows == []


def test_status_shows_positions_then_candidates(tmp_path):
    StateFile(tmp_path / "state.json").save(State(
        last_session=date(2026, 10, 9),
        entered={"AAA": date(2026, 10, 7)},
        decision=Decision(
            session=date(2026, 10, 9),
            picks=[Pick("BBB", 50.0, 0.182, 3.1, 46.5, 120), Pick("CCC", 30.0, 0.131, 2.0, 27.9, 0)],
            held=[Held("AAA", "2026-10-07", 100, 40.0, 33.5, 41.6, 41.0, 2, "hold")],
        ),
    ))
    s = status(tmp_path)
    assert s.facts == [("Last session", "2026-10-09"), ("Positions", "1"), ("Candidates", "2 (1 bid)")]
    assert s.rows == [
        ["AAA", "held", "2026-10-07", "2", "33.50", "41.60", "+100.00 (+2.5%)"],
        ["BBB", "bid 120 at 46.50, down 18.2%", "—", "—", "—", "—", "—"],
        ["CCC", "candidate, no slot, down 13.1%", "—", "—", "—", "—", "—"],
    ]
