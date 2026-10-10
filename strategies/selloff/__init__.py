"""Long mean reversion after a selloff: buy liquid, volatile stocks in a
long-term uptrend that have fallen 12.5% in three sessions, with a limit bid
7% under the last close, and sell on a 4% gain, a 2.5 ATR stop, or after three
days. Laurens Bensdorp's System 3 from Automated Stock Trading Systems."""

from __future__ import annotations

from pathlib import Path

from algo_trading.base import Status

from .session import StateFile
from .strategy import SelloffStrategy

STRATEGY = SelloffStrategy


def status(data_dir: Path) -> Status:
    state = StateFile(data_dir / "state.json").load()
    d = state.decision
    facts = [("Last session", state.last_session.isoformat() if state.last_session else "never")]
    rows = []
    if d is not None:
        facts += [
            ("Positions", str(len(d.held))),
            ("Candidates", f"{len(d.picks)} ({sum(1 for p in d.picks if p.qty)} bid)"),
        ]
        for h in d.held:
            pl = (h.last_close - h.entry) * h.qty
            rows.append([
                h.symbol,
                "held" if h.action == "hold" else h.action.replace("_", " "),
                h.entered,
                str(h.held_closes),
                f"{h.stop:.2f}",
                f"{h.target:.2f}",
                f"{pl:+,.2f} ({(h.last_close / h.entry - 1) * 100:+.1f}%)",
            ])
        for p in d.picks:
            what = f"bid {p.qty} at {p.limit:.2f}" if p.qty else "candidate, no slot"
            rows.append([p.symbol, f"{what}, down {p.drop * 100:.1f}%", "—", "—", "—", "—", "—"])
    return Status(
        facts=facts,
        columns=["Symbol", "Status", "Entered", "Days held", "Stop", "Target", "P&L at last close"],
        rows=rows,
    )
