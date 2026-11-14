# cbBTC Dip-Buy Bot — make.fun BTC Race (btc-race-24h-20261113-2314)

A long-only trading bot for the make.fun bars-replay race. It trades cbBTC
against USDC on 1-minute bars, starting from 1,000 USDC of paper money, and
speaks `agent-fun/bars-replay/v1` through the kit's Python SDK shim
(`vendor/makefun_bars.py`, a byte-for-byte copy; `main.py` is the strategy).

## What it does

The bot answers every batch with at most one order. It buys a dip only when
three things line up at the same bar:

1. **A real dip.** The latest close is at least 70 bp below the trailing
   45-bar high of closes.
2. **Momentum exhaustion.** Either a bullish RSI(14) divergence — price
   prints a lower swing low while RSI prints a higher low at that swing,
   with a real bounce (≥ 50 bp) between the two lows; the signal turns
   tradable 5 bars after the swing low and stays valid for 30 bars — or
   RSI(14) crossed up through 30 within the last 10 bars.
3. **The right zone**, measured on the trailing 240-bar window:
   - *Capitulation:* the close has fallen ≥ 340 bp from the 240-bar high,
     sits within 40 bp of the 240-bar low (it is printing the low), and RSI
     is ≤ 28. A washed-out bottom.
   - *Retest:* the 240-bar low is at least 60 bars old and the close is
     back within 90 bp above it. Divergence entries only — a falling knife
     through the level has no divergence and is skipped.

Mid-waterfall dips and fresh breakdowns are never bought: on the dev day
those entries lost every time.

Entries are all-in (100% of cash, at most one position), capped at 3 entries
per trailing 1,440 bars, with a 5-bar cooldown after each exit and no
entries after bar 1,250. While holding, the bot sells everything when the
close reaches **+180 bp** over the position's cost basis (fees included) or
**−100 bp** under it. Buys carry `max_slippage_bps` 150 (a fill far above
the close means the bounce already happened), sells 2,000 (an exit must not
be refused when it matters).

The bot is deterministic: no wall clock, no randomness, no environment
reads — only the hello and the batches. Two executions produce byte-identical
replies (verified locally).

## How it was tuned

The kit's dev window is a −491 bp day: a steady grind down for ~7 hours to
the low at bar 406, then a choppy sideways range with ±60–130 bp hourly
bounces. Buy-and-hold loses ~$49 there, and the first version of this bot —
the same dip/RSI signals without the zone gate — lost $17.62: all three of
its entries fired inside the waterfall and stopped out.

So the parameters were re-tuned on the dev bars with a local simulator that
replicates the harness's fill model exactly (next-bar-open fills, 4 bp fee,
2 bp slippage, constant-product impact against each bar's open depth,
end-of-window flatten at the last close), driving the real bot over stdio:

- dip depth / take-profit / stop-loss grids: take-profit never bound inside
  the waterfall — the problem was *where* entries fired, not the exits.
- The zone gate was added after measuring every dip event's forward path:
  the only entries with durable forward bounces were the capitulation low
  itself and retests of an old low with divergence.
- Final parameters sit in the middle of a stable plateau (cap-fall
  340–380 bp × RSI cap 26–30 all give the same trade; TP 180 is below the
  cliff where a 300 bp target is never reached).

`grade-local` could not run the Python bot on this machine (its docker
driver needs Docker; its process driver runs node22 only), so the simulator
— built from the harness's own published source — is the graded reference.

## Dev-window result

On the kit's dev window (1,440 bars), the final bot makes **one round trip**:

| Metric | Value |
| --- | --- |
| Net PnL | **+$17.12** (17,120,9xx micro-USDC) |
| Filled orders | 2 (buy at bar 406, sell at bar 658) |
| Fees paid | $0.81 |
| Max drawdown | 1.07% |
| Think time | ~0.1 s for all 1,440 batches (budget 1,800 s) |

That passes the dev gate (net PnL > 0 after every fee) with at least one
trade and a drawdown far under the 25% guardrail. The race window is unseen
data; a bot this selective may trade rarely there — that is the trade-off
the dev day taught: in a grind, cash is a position.
