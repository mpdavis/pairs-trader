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

`algo-trading backtest selloff` on the default universe and parameters,
$100,000, Yahoo daily bars. The two-year run is the period live trading
would have started from; the 18-year run is the one to judge the strategy by.

| | 2008-01-01 → 2026-10-09 | 2024-10-09 → 2026-10-09 |
|---|---|---|
| Total return / CAGR | +54.3% / 2.3% | +17.6% / 8.5% |
| Max drawdown | 9.3% | 5.9% |
| Sharpe (LumiBot, vs T-bills) | −0.26 | 0.40 |
| SPY price return, same period | +434% (max drawdown 53%) | +34% |
| Trades (round trips) | 193 | 47 |
| Win rate | 69% (average win +7.8%, average loss −7.9%) | 77% (+8.7% / −5.5%) |
| Average hold | 2.3 sessions | 2.4 sessions |
| Limit bids filled | 193 of 1,097 (18%) | 47 of 282 (17%) |
| Share of equity invested | 0.7% on average; any position on 6% of sessions | 1.6%; 11% of sessions |
| Stops triggered | 3, each a gap through the stop at the open | none |

What the long run shows:

- **The last two years were the best stretch, not a typical one.** Most years
  returned −1% to +3%, and 2014–2017 lost money four years running. The
  whole result rests on 2021 (+16.3%), 2025 (+5.8%) and 2026 (+10.2%).
- **It rarely trades.** About ten trades a year, with long droughts (one trade
  in each of 2012 and 2013). In 2008 only 80 of the universe's 141 symbols
  traded at all, and large caps seldom fall 12.5% in three days.
- **The stop does not cap the worst losses.** All three stops filled on a gap
  far below them: RUN and ENPH in March 2020 (−32%, −35%), UPST in August 2023
  (−30%). Average wins and losses are the same size, so the strategy lives on
  its win rate.
- **Fill optimism is small.** 182 of the 193 fills (94%) either opened below
  the bid or traded at least 0.5% through it, so they did not depend on the
  low merely touching the limit. Dropping the other 11 cuts the profit from
  $54k to $45k, because those 11 happened to be big winners.
- **Volatility regime (Nagel 2012).** Reversal profits are said to grow with
  market stress. By the VIX close before entry:

  | VIX | Trades | Win rate | Average return | Worst |
  |---|---|---|---|---|
  | < 15 | 18 | 56% | +0.5% | −13.6% |
  | 15–20 | 63 | 67% | +2.3% | −30.5% |
  | 20–30 | 66 | 86% | +5.6% | −7.4% |
  | 30 + | 46 | 54% | +1.2% | −35.0% |

  The 20–30 band was the best in both halves of the period (2008–2016: 15
  trades, 80% wins; 2017–2026: 51 trades, 88%), but the effect is not the
  monotonic one the paper predicts: panics above 30 were mediocre and held the
  worst losses. VIX ≥ 20 against below 20 differs by +1.9 points of average
  return with t = 1.30, which is not significant. The band was chosen after
  looking at four buckets, so it is a hypothesis to watch in paper trading,
  not a rule.

Read every number with care:

- **Survivorship bias** in the universe (above) flatters all of them.
- **Fills at the printed open.** Exits and gap fills assume the open was
  available; after a gap the opening auction can be worse.
- No commissions or slippage are charged. Alpaca charges no commission on
  stocks.
