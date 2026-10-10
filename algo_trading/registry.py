"""Finds the strategies under strategies/. Each subpackage is one strategy,
named by its directory, and exports:

- ``STRATEGY``: a ``ManagedStrategy`` subclass.
- ``status(data_dir) -> Status`` (optional): what the dashboard shows for it.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import strategies

from .base import ManagedStrategy, Status


@dataclass(frozen=True)
class Entry:
    name: str
    strategy: type[ManagedStrategy]
    status: Callable[[Path], Status] | None
    # The package docstring's first paragraph, shown on the dashboard.
    summary: str = ""


def discover() -> dict[str, Entry]:
    found = {}
    for module in pkgutil.iter_modules(strategies.__path__):
        if not module.ispkg:
            continue
        package = importlib.import_module(f"strategies.{module.name}")
        cls = getattr(package, "STRATEGY", None)
        if not (isinstance(cls, type) and issubclass(cls, ManagedStrategy)):
            raise TypeError(f"strategies/{module.name} must export STRATEGY, a ManagedStrategy subclass")
        summary = (package.__doc__ or "").strip().split("\n\n")[0].replace("\n", " ")
        found[module.name] = Entry(module.name, cls, getattr(package, "status", None), summary)
    return found


def select(available: Mapping[str, Entry], wanted: list[str]) -> list[Entry]:
    """`wanted` names, or every strategy when it is empty."""
    unknown = sorted(set(wanted) - set(available))
    if unknown:
        raise SystemExit(f"unknown strategies: {', '.join(unknown)} (have: {', '.join(sorted(available))})")
    return [available[n] for n in (wanted or sorted(available))]


def parameters_from_env(entry: Entry, environ: Mapping[str, str]) -> dict:
    """The strategy's default parameters, each overridable by an environment
    variable named <STRATEGY>_<PARAMETER>, e.g. PAIRS_ENTRY_Z. The default's
    type decides how the value is parsed."""
    params: dict[str, object] = dict(entry.strategy.parameters)
    for key, default in params.items():
        raw = environ.get(f"{entry.name}_{key}".upper())
        if raw is None or key == "data_dir":
            continue
        if isinstance(default, bool):
            params[key] = raw.lower() in ("1", "true", "yes")
        elif isinstance(default, int):
            params[key] = int(raw)
        elif isinstance(default, float):
            params[key] = float(raw)
        else:
            params[key] = raw
    return params


def check_disjoint(entries: list[Entry], environ: Mapping[str, str]) -> None:
    """Exit unless no two strategies may trade the same symbol, each judged on
    the parameters it would run with. Every strategy counts, not only the ones
    being started: another container may be running the rest on this account."""
    claimed: dict[str, str] = {}
    clashes = []
    for entry in entries:
        for symbol in sorted(entry.strategy.symbols(parameters_from_env(entry, environ))):
            if symbol in claimed:
                clashes.append(f"{symbol} ({claimed[symbol]}, {entry.name})")
            claimed.setdefault(symbol, entry.name)
    if clashes:
        raise SystemExit(f"strategies share symbols, which would trade one position twice: {', '.join(clashes)}")
