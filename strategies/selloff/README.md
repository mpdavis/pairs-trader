# selloff

Laurens Bensdorp's Long Mean Reversion Selloff (System 3 in *Automated Stock
Trading Systems*). It buys liquid, volatile stocks that are in a long-term
uptrend but have just fallen hard, bidding well under the last close so it
only fills on further panic, and sells within days on a small gain, a stop, or
time. Long only.

## Rules

Once per trading day, at the open, using the previous sessions' daily bars:

| Step | Rule |
|---|---|
| Filter | close ≥ `min_price`; 50-day average volume ≥ `min_avg_volume` shares; 10-day ATR ≥ `min_atr_pct` of the close |
| Setup | close above its `sma_window`-day SMA, and down ≥ `min_drop` over the last `drop_window` sessions |
| Rank | largest `drop_window`-session drop first; bid on up to `max_positions` less what is held |
| Entry | limit buy `limit_discount` under the last close, good for the day; unfilled bids are cancelled |
| Stop | `stop_atr` × the 10-day ATR (as of the entry day) under the fill, as a GTC stop order |
| Profit | a close ≥ `profit_target` above the fill exits at the next session's open |
| Time | `max_hold_days` closes (the entry day's included) with neither stop nor target hit exits at the next session's open |
| Size | risk `risk_per_trade` of equity between the fill and the stop, at most `max_position` of equity per position, and all positions together at most `gross_cap` of equity |

ATR is the simple mean of the last ten true ranges. Between sessions the
strategy polls every five minutes and puts a stop under any bid that has
filled, once the bid has finished (Alpaca rejects a sell stop while a buy in
the same symbol is still working). A close that is already below the stop
(the order was missing, or the stock gapped through it) exits at the next
open.

Account positions are the source of truth. `state.json` only keeps each
position's entry date, the last session traded, and the last decision: the
candidates and bids, and each position's stop, target and close (for the
dashboard). Every session reconciles: a fill from yesterday's bid takes
yesterday's date, a long found in a universe symbol is adopted and given a
stop, a short in one is closed, and a position gone since the last session
(stopped out, or closed by hand) is forgotten. A session waits while one of
its exits is still working.

It only ever reads, orders, cancels or closes the symbols in its own
universe, and never cancels or liquidates account-wide. `algo-trading live`
refuses to start if any symbol is in both this universe and another
strategy's (see `ManagedStrategy.symbols`).

### Where the rules come from

The rules above are those in the brief this port was written from. Checked
against what is available without the book:

| Rule | Confirmed by |
|---|---|
| $1 price, 1M shares 50-day volume, ATR(10) ≥ 5% | the reference implementation's docstring; WealthLab's published port |
| Close > SMA(150), 3-day drop ≥ 12.5% | the reference implementation's docstring; WealthLab |
| Limit 7% under the previous close | the reference implementation |
| Stop 2.5 × ATR(10) under the fill | the reference implementation; WealthLab |
| 4% profit target, 3-day time exit | WealthLab's port and discussion |

**Not confirmed** from a source available here (the book was not to hand):

- **Ranking by the largest three-day drop**, the **2% risk / 10% maximum**
  sizing and **ten positions**. They match how Bensdorp sizes his other
  systems, but no source checked states them for this one.
- **When the exits fill.** This port exits at the next session's open, as the
  brief asks. WealthLab's port sells the time exit market-on-close, and the
  book is commonly quoted as "sell next day market on close". Selling at the
  close gives the bounce a full extra day; selling at the open is what a
  once-a-day runner on daily bars can do.
- **The stop's starting day.** Here it goes in as soon as the bid fills (live)
  or at the next session (backtests, which step a day at a time). The book's
  end-of-day process places it for the day after entry.

The reference implementation (Miltiadis-Kon/Automated-Stock-Trading-Systems-in-Python)
was used for ideas only. Its bugs, each covered by a regression test in
`tests/selloff/test_rules.py`: the volume filter read one bar (`iloc[-50]`)
instead of fifty, the ATR filter compared raw ATR with 0.05 instead of ATR
divided by the close, the three-day drop check was inverted so it rejected
exactly the stocks it should take, and it traded one hardcoded ticker.

## Parameters

| Parameter | Default | Environment |
|---|---|---|
| `universe` | `universe.txt` | `SELLOFF_UNIVERSE` (comma-separated; empty for the file) |
| `max_positions` | `10` | `SELLOFF_MAX_POSITIONS` |
| `min_price` / `min_avg_volume` / `min_atr_pct` | `1.0` / `1000000` / `0.05` | `SELLOFF_MIN_PRICE`, … |
| `volume_window` / `atr_window` / `sma_window` | `50` / `10` / `150` | `SELLOFF_VOLUME_WINDOW`, … (sessions) |
| `drop_window` / `min_drop` | `3` / `0.125` | `SELLOFF_DROP_WINDOW`, `SELLOFF_MIN_DROP` |
| `limit_discount` | `0.07` | `SELLOFF_LIMIT_DISCOUNT` |
| `stop_atr` / `profit_target` / `max_hold_days` | `2.5` / `0.04` / `3` | `SELLOFF_STOP_ATR`, … |
| `risk_per_trade` / `max_position` | `0.02` / `0.10` | `SELLOFF_RISK_PER_TRADE`, `SELLOFF_MAX_POSITION` (share of equity) |
| `gross_cap` | `0.5` | `SELLOFF_GROSS_CAP` (share of equity across all its positions) |

### The universe

`universe.txt` is a static list of liquid US large and mid caps, leaning to
the volatile names that can pass a 5% ATR filter. Bensdorp scans every listed
US stock; a static list of today's survivors is a **known survivorship-bias
limitation**: a backtest never meets the stocks that sold off and kept falling
until they were delisted, which is the failure this system is most exposed to.
It also leaves out the small caps where most 12.5% three-day drops happen, so
it trades far less often than the book's version. It excludes every symbol
pairs trades by default.

## Layout

- `rules.py`: the rules, with no broker or data source.
- `session.py`: one trading day against a `Broker` protocol: cancel stale
  bids, reconcile positions, exit what is due, keep a stop under every
  position, bid for the day's candidates, save state. `protect` is the
  between-sessions step that stops new fills.
- `strategy.py`: the LumiBot strategy. `LumibotBroker` adapts it to the
  `Broker` protocol, so backtests and live trading run the same session code.
- `universe.txt`: the default universe.

## Backtest

`algo-trading backtest selloff --start 2024-10-09 --end 2026-10-09` on the
default universe and parameters, $100,000, Yahoo daily bars:

| | |
|---|---|
| Total return / CAGR | +17.6% / 8.5% |
| Max drawdown | 5.9% |
| Sharpe | 0.40 |
| Trades (round trips) | 47, none open at the end |
| Win rate | 77% (average win +8.7%, average loss −5.5%) |
| Average hold | 2.4 sessions from entry day to exit day (10 one-session, 9 two, 28 three) |
| Limit bids filled | 47 of 282 (17%), on 156 of 501 sessions with a bid |
| Stops triggered | none; every exit was a market sell at the next open |

Read it with care:

- **Small sample.** 47 trades, half of them in two clusters: the
  February–April 2025 selloff and semiconductors in June–July 2026. Five
  July 2026 chip trades made 46% of the profit.
- **Mostly in cash.** Positions averaged $7,300 and held for 112
  position-sessions in total over 501 sessions, so the return is on capital
  that sat idle most of the time; a 5.9% drawdown says more about low exposure
  than about a safe strategy.
- **Survivorship bias** in the universe (above) flatters every number here.
- **Fills are optimistic.** The backtest fills a bid whenever the day's low
  reaches it, at the limit (or the open, if it opened lower). Live, a bid at
  the low of a panic day often goes unfilled, and the opening auction after a
  gap is not always available at the printed open.
- No commissions or slippage are charged. Alpaca charges no commission on
  stocks.
