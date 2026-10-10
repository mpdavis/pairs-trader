from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path


def data_root() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def wanted(names: list[str]) -> list[str]:
    """Names from the command line, else STRATEGIES (comma-separated), else all."""
    if names:
        return names
    return [n.strip() for n in os.environ.get("STRATEGIES", "").split(",") if n.strip()]


def alpaca_credentials() -> tuple[str, str, bool]:
    paper = os.environ.get("ALPACA_IS_PAPER", "true").lower() != "false"
    if not paper and os.environ.get("ALLOW_LIVE_TRADING") != "yes":
        sys.exit("ALPACA_IS_PAPER=false trades real money; also set ALLOW_LIVE_TRADING=yes to confirm")
    return os.environ["ALPACA_API_KEY"], os.environ["ALPACA_API_SECRET"], paper


def show_strategy_logs() -> None:
    # LumiBot configures only its own loggers, so without a handler here the
    # strategies' decisions and orders never reach the container log.
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s: %(message)s"))
    for name in ("strategies", "algo_trading"):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


def cmd_list() -> None:
    from .registry import discover

    for name, entry in sorted(discover().items()):
        print(f"{name:16} {entry.summary}")


def cmd_live(names: list[str]) -> None:
    from lumibot.brokers import Alpaca
    from lumibot.traders import Trader

    from .heartbeat import heartbeat
    from .registry import check_disjoint, discover, parameters_from_env, select

    key, secret, paper = alpaca_credentials()
    available = discover()
    check_disjoint(list(available.values()), os.environ)
    entries = select(available, wanted(names))
    broker = Alpaca({"API_KEY": key, "API_SECRET": secret, "PAPER": paper})
    show_strategy_logs()
    trader = Trader()
    for entry in entries:
        data_dir = data_root() / entry.name
        data_dir.mkdir(parents=True, exist_ok=True)
        # A fresh start counts as alive, so a deploy outside market hours is
        # healthy at once; from then on only completed iterations advance it.
        heartbeat(data_dir).touch()
        params = parameters_from_env(entry, os.environ)
        params["data_dir"] = str(data_dir)
        trader.add_strategy(entry.strategy(broker=broker, name=entry.name, parameters=params))
    trader.run_all()


def cmd_backtest(name: str, start: str, end: str, budget: float) -> None:
    from lumibot.backtesting import YahooDataBacktesting

    from .registry import discover, parameters_from_env, select

    (entry,) = select(discover(), [name])
    out = entry.strategy.run_backtest(
        YahooDataBacktesting,
        datetime.fromisoformat(start),
        datetime.fromisoformat(end),
        budget=budget,
        benchmark_asset="SPY",
        name=entry.name,
        parameters=parameters_from_env(entry, os.environ),
        show_plot=False,
        show_tearsheet=False,
        save_tearsheet=False,
        show_indicators=False,
        show_progress_bar=False,
    )
    results = out[0] if isinstance(out, tuple) else out
    for key in ("total_return", "cagr", "volatility", "sharpe", "max_drawdown", "romad"):
        value = (results or {}).get(key)
        if isinstance(value, dict):
            value = value.get("drawdown")
        if value is not None:
            print(f"{key:13} {float(value):.4f}")


def cmd_dashboard(port: int) -> None:
    from alpaca.trading.client import TradingClient
    from waitress import serve

    from .dashboard import create_app
    from .registry import discover, select

    key, secret, paper = alpaca_credentials()
    client = TradingClient(key, secret, paper=paper)
    app = create_app(lambda: client, select(discover(), wanted([])), data_root(), os.environ)
    serve(app, host="0.0.0.0", port=port)


def cmd_healthcheck(names: list[str], max_age_hours: float) -> None:
    """Exit non-zero unless every selected strategy's heartbeat is fresh. Reads
    only files, so it stays cheap enough for a frequent container healthcheck."""
    from .heartbeat import heartbeat

    selected = wanted(names)
    if not selected:
        sys.exit("healthcheck needs strategy names or STRATEGIES")
    stale = []
    for name in selected:
        beat = heartbeat(data_root() / name)
        if not beat.exists() or time.time() - beat.stat().st_mtime > max_age_hours * 3600:
            stale.append(name)
    if stale:
        sys.exit(f"no recent iteration: {', '.join(stale)}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="algo-trading")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list the strategies in strategies/")
    live = sub.add_parser("live", help="trade the Alpaca account with the named strategies (default: STRATEGIES, else all)")
    live.add_argument("names", nargs="*")
    bt = sub.add_parser("backtest", help="replay one strategy in LumiBot on Yahoo daily bars")
    bt.add_argument("name")
    bt.add_argument("--start", required=True)
    bt.add_argument("--end", default=datetime.now().date().isoformat())
    bt.add_argument("--budget", type=float, default=100_000.0)
    dash = sub.add_parser("dashboard", help="serve the account and strategy dashboard")
    dash.add_argument("--port", type=int, default=8080)
    hc = sub.add_parser("healthcheck", help="fail unless each strategy iterated recently")
    hc.add_argument("names", nargs="*")
    # Iterations run only in market hours; four days covers a holiday weekend.
    hc.add_argument("--max-age-hours", type=float, default=96)
    args = ap.parse_args()

    if args.cmd == "list":
        cmd_list()
    elif args.cmd == "live":
        cmd_live(args.names)
    elif args.cmd == "backtest":
        cmd_backtest(args.name, args.start, args.end, args.budget)
    elif args.cmd == "dashboard":
        cmd_dashboard(args.port)
    else:
        cmd_healthcheck(args.names, args.max_age_hours)


if __name__ == "__main__":
    main()
