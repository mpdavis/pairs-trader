import os
import time

import pytest

from algo_trading.__main__ import cmd_healthcheck
from algo_trading.base import ManagedStrategy
from algo_trading.heartbeat import heartbeat
from algo_trading.registry import Entry, check_disjoint, discover, parameters_from_env, select
from strategies.pairs import PairsStrategy
from strategies.selloff import SelloffStrategy


def test_discovers_pairs():
    entries = discover()
    assert entries["pairs"].strategy is PairsStrategy
    assert entries["pairs"].status is not None
    assert entries["pairs"].summary.startswith("Market-neutral pairs trading")


def test_discovers_selloff():
    entry = discover()["selloff"]
    assert entry.strategy is SelloffStrategy
    assert entry.status is not None
    assert entry.summary.startswith("Long mean reversion after a selloff")


def test_installed_strategies_trade_disjoint_symbols_by_default():
    check_disjoint(list(discover().values()), {})


def test_overlapping_symbols_refuse_to_start():
    entries = list(discover().values())
    with pytest.raises(SystemExit, match=r"KNX \(pairs, selloff\)"):
        check_disjoint(entries, {"SELLOFF_UNIVERSE": "NVDA,KNX"})
    with pytest.raises(SystemExit, match=r"NVDA \(pairs, selloff\)"):
        check_disjoint(entries, {"PAIRS_PAIRS": "NVDA/AMD"})


def test_select_defaults_to_everything_and_rejects_unknown_names():
    available = discover()
    assert [e.name for e in select(available, [])] == sorted(available)
    with pytest.raises(SystemExit, match="unknown strategies: nope"):
        select(available, ["pairs", "nope"])


class Example(ManagedStrategy):
    parameters = {**ManagedStrategy.parameters, "symbol": "SPY", "window": 20, "threshold": 1.5, "enabled": True}


def test_parameters_from_env_casts_by_default_type():
    entry = Entry("example", Example, None)
    params = parameters_from_env(entry, {
        "EXAMPLE_SYMBOL": "QQQ",
        "EXAMPLE_WINDOW": "30",
        "EXAMPLE_THRESHOLD": "2.25",
        "EXAMPLE_ENABLED": "false",
        # The runner owns data_dir; the environment cannot redirect it.
        "EXAMPLE_DATA_DIR": "/elsewhere",
        "OTHER_WINDOW": "99",
    })
    assert params == {"data_dir": None, "symbol": "QQQ", "window": 30, "threshold": 2.25, "enabled": False}
    assert Example.parameters["window"] == 20


def test_healthcheck(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="no recent iteration: pairs"):
        cmd_healthcheck(["pairs"], max_age_hours=96)
    beat = heartbeat(tmp_path / "pairs")
    beat.parent.mkdir()
    beat.touch()
    cmd_healthcheck(["pairs"], max_age_hours=96)
    old = time.time() - 5 * 86400
    os.utime(beat, (old, old))
    with pytest.raises(SystemExit):
        cmd_healthcheck(["pairs"], max_age_hours=96)


def test_healthcheck_reads_strategies_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STRATEGIES", "a, b")
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        heartbeat(tmp_path / name).touch()
    cmd_healthcheck([], max_age_hours=1)


def test_healthcheck_does_not_import_lumibot(tmp_path):
    import subprocess
    import sys

    (tmp_path / "pairs").mkdir()
    (tmp_path / "pairs" / "heartbeat").touch()
    code = (
        "import sys; from algo_trading.__main__ import cmd_healthcheck; "
        "cmd_healthcheck(['pairs'], 96); print('lumibot' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={"DATA_DIR": str(tmp_path), "PATH": ""})
    assert out.stdout.strip() == "False"
