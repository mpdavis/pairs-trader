# algo-trading

[LumiBot](https://github.com/Lumiwealth/lumibot) strategies trading an Alpaca
account, plus a dashboard over the account and the strategies. One image runs
any or all of the strategies, and the dashboard.

## Strategies

Each directory under `strategies/` is one strategy, named by the directory.

| Strategy | What it does |
|---|---|
| [`pairs`](strategies/pairs/README.md) | Market-neutral pairs trading: long the cheap stock, short the rich one, close both when the spread reverts |
| [`selloff`](strategies/selloff/README.md) | Long mean reversion: bid 7% under stocks in an uptrend that just fell 12.5% in three days, sell on a 4% gain, a stop, or after three days |

`algo-trading list` prints what is installed.

### Adding one

Create `strategies/<name>/__init__.py` that exports:

- `STRATEGY`: a subclass of `algo_trading.base.ManagedStrategy`. Implement
  `initialize` as in any LumiBot strategy, and `iterate` in place of
  `on_trading_iteration`. Keep state under `self.data_dir`, which is
  `/data/<name>` live and a temporary directory in backtests.
- `status(data_dir) -> algo_trading.base.Status` (optional): facts and a table
  for the dashboard, read from the strategy's state files.

The package docstring's first paragraph is its description on the dashboard.
Every entry in the strategy's `parameters` can be overridden by an environment
variable named `<NAME>_<PARAMETER>`, for example `PAIRS_ENTRY_Z=2.5`.

Strategies share one Alpaca account, so two of them must never trade the same
symbol. A strategy declares the symbols it may trade by overriding
`ManagedStrategy.symbols(parameters)`; `algo-trading live` refuses to start
when two strategies' symbols overlap.

## Commands

```sh
algo-trading list
algo-trading backtest pairs --start 2024-10-09 --end 2025-10-08   # Yahoo daily bars, no keys needed
algo-trading live [NAME ...]        # default: $STRATEGIES (comma-separated), else every strategy
algo-trading dashboard [--port 8080]
algo-trading healthcheck [NAME ...] # fails unless each strategy iterated in the last 96 hours
```

## Configuration

| Variable | Default | |
|---|---|---|
| `ALPACA_API_KEY`, `ALPACA_API_SECRET` | required for `live` and `dashboard` | |
| `ALPACA_IS_PAPER` | `true` | `false` also requires `ALLOW_LIVE_TRADING=yes` |
| `STRATEGIES` | every strategy | which ones `live`, `dashboard` and `healthcheck` cover |
| `DATA_DIR` | `/data` | each strategy keeps state in `$DATA_DIR/<name>` |
| `<NAME>_<PARAMETER>` | the strategy's default | e.g. `PAIRS_PAIRS=JBHT/KNX` |

## Dashboard

Read-only. It shows equity and the day's change, cash, buying power, long and
short exposure, a three-month equity curve, every holding with its unrealized
P&L, the 25 most recent orders, and for each strategy when it last iterated
and its own status panel. Mount the data volume read-only into it. `/healthz`
answers without touching Alpaca.
