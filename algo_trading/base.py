"""What every strategy under strategies/ builds on."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from lumibot.strategies import Strategy

from .heartbeat import heartbeat

log = logging.getLogger(__name__)


@dataclass
class Status:
    """What a strategy shows on the dashboard: a few labelled facts and an
    optional table."""

    facts: list[tuple[str, str]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


class ManagedStrategy(Strategy):
    """A LumiBot strategy run by `algo-trading live`. The runner passes each one
    its own `data_dir` for state; after every iteration that completes, the
    heartbeat file there is touched, which is what the container healthcheck
    and the dashboard read. Subclasses implement `iterate` instead of
    `on_trading_iteration`."""

    parameters: dict = {"data_dir": None}

    @classmethod
    def symbols(cls, parameters: dict) -> set[str]:
        """The symbols this strategy may trade under `parameters`. Strategies
        share one account, so `live` refuses to start when two overlap."""
        return set()

    @property
    def data_dir(self) -> Path:
        if not hasattr(self, "_data_dir"):
            configured = self.parameters.get("data_dir")
            # Backtests get a throwaway directory so they never touch live state.
            self._data_dir = Path(configured) if configured else Path(tempfile.mkdtemp(prefix=f"{self.name}-"))
            self._data_dir.mkdir(parents=True, exist_ok=True)
        return self._data_dir

    def iterate(self) -> None:
        raise NotImplementedError

    def on_trading_iteration(self) -> None:
        try:
            self.iterate()
        except Exception:
            if self.is_backtesting:
                raise
            # The next iteration retries. A heartbeat that stops advancing marks
            # the container unhealthy and shows on the dashboard.
            log.exception("%s: iteration failed", self.name)
            return
        heartbeat(self.data_dir).touch()
