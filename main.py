"""Dip-buy bot for the make.fun cbBTC bars-replay race (agent-fun/bars-replay/v1).

Strategy (long only, at most one position, paper USDC):
  * Watch each 1-minute bar of CBBTC. A buy is considered only when the latest
    close sits at least DIP_BPS below the trailing HIGH_WIN-bar high of closes
    (a real dip, not noise) AND momentum says the fall is exhausting:
      - a bullish RSI divergence: price printed a lower swing low while
        RSI(14) printed a higher low at that swing, with a real bounce
        (DIV_MIN_BOUNCE_BPS) between the two lows; the signal turns tradable
        DIV_CONFIRM bars after the swing low and stays valid DIV_VALID bars;
      - or RSI(14) crossed up through 30 within the last CROSS_VALID bars.
  * And only inside one of two zones, measured on the LONG_WIN-bar window:
      - capitulation: the close has fallen at least CAP_FALL_BPS from the
        long high, sits within CAP_BAND_BPS of the long low (it is printing
        the low), and RSI is at most CAP_RSI_MAX — a washed-out bottom;
      - retest: the long low is at least RETEST_AGE bars old and the close
        is back within RETEST_BAND_BPS above it — divergence entries only.
    Mid-waterfall dips and fresh breakdowns through the low are not bought.
  * Entries are all-in (SIZE_PCT of cash) and capped at MAX_TRIPS entries in
    any trailing TRIP_WIN bars, with a COOLDOWN after every exit, and none
    after LATE_CUTOFF.
  * Insurance trade: if no buy has filled at all by FB_FROM and a modest
    dip is on the table (close at least FB_DIP_BPS below the trailing high
    with RSI at most FB_RSI_MAX), take ONE small position (FB_SIZE_PCT of
    cash) with tight exits (FB_TP_BPS / FB_SL_BPS). It exists so a quiet
    window still produces a trade; while it is open the primary logic
    pauses, and once it closes the primary logic may resume.
  * While holding, sell everything when the close reaches TP_BPS above the
    position's cost basis (fees included) or SL_BPS below it (the FB_*
    levels when the position is the insurance trade).
  * Every order carries a max_slippage_bps measured from the latest close:
    tight for buys (a fill far above the close means the dip already bounced),
    wide for sells (an exit must not be refused when it matters).

Tuned on the kit's dev window with a fill-faithful simulator (see README):
the dev day was a -491 bp grind whose only durable bounce followed the
capitulation low, so the final parameters buy that pattern and little else.

Deterministic: no wall clock, no randomness, no environment reads; the only
inputs are the hello and the batches themselves. Prices are integers at the
symbol's price scale; RSI and ratios are IEEE doubles, identical every run.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars  # noqa: E402

SYMBOL = "CBBTC"

PARAMS = {
    "dip_bps": 70,        # close must be >= this far below the trailing high
    "high_win": 45,       # trailing window (bars) for the reference high
    "rsi_len": 14,        # RSI period
    "rsi_os": 30,         # oversold level for the cross-up signal
    "cross_valid": 10,    # bars a cross-up stays tradable
    "div_confirm": 5,     # bars after a swing low before divergence is tradable
    "div_valid": 30,      # bars a divergence stays valid, from the swing low
    "div_min_bounce_bps": 50,  # min intervening bounce between the two swing lows
    "div_min_gap": 0,     # min bars between the two swing lows of a divergence
    "cross_on": 1,        # 1: the RSI cross-up alone may trigger an entry
    "swing_span": 4,      # bars on each side of a swing low
    "long_win": 240,      # long window (bars) for the capitulation/retest zones
    "cap_fall_bps": 340,  # capitulation: fall from the long high at least this
    "cap_band_bps": 40,   # capitulation: close within this of the long low
    "cap_rsi_max": 28,    # capitulation: RSI at most this (100 = no cap)
    "retest_age": 60,     # retest: the long low is at least this many bars old
    "retest_band_bps": 90,  # retest: close within this above the long low
    "late_cutoff": 1250,  # no entries after this bar index
    "tp_bps": 180,        # take profit vs cost basis
    "sl_bps": 100,        # stop loss vs cost basis
    "max_trips": 3,       # max entries per trailing trip_win bars
    "trip_win": 1440,     # trailing window for the trip cap (bars)
    "cooldown": 5,        # bars to wait after an exit before re-entering
    "size_pct": 100,      # percent of cash per entry
    "buy_slip_bps": 150,  # max_slippage_bps on buys
    "sell_slip_bps": 2000,  # max_slippage_bps on sells
    "min_bars": 60,       # history needed before the first entry
    "fb_from": 1150,      # insurance: first bar index it may fire
    "fb_cutoff": 1400,    # insurance: last bar index it may fire
    "fb_dip_bps": 50,     # insurance: close >= this below the trailing high
    "fb_rsi_max": 45,     # insurance: RSI at most this
    "fb_size_pct": 20,    # insurance: percent of cash per entry
    "fb_tp_bps": 80,      # insurance: take profit vs its cost basis
    "fb_sl_bps": 50,      # insurance: stop loss vs its cost basis
}


class Bot:
    def __init__(self, hello):
        self.p = dict(PARAMS)
        sym = hello["symbols"][0]
        self.qty_scale = sym["qty_scale"]
        self.price_scale = sym["price_scale"]
        self.quote_scale = hello["capital"]["quote_scale"]
        self.min_notional = int(hello["fees"]["min_notional_quote_units"])
        self.closes = []          # close of every bar seen, in order
        self.rsi = []             # RSI value per bar (None until seeded)
        self._avg_gain = None
        self._avg_loss = None
        self.swings = []          # (bar_index, close, rsi) of confirmed swing lows
        self.div_from = 1 << 60   # first bar index a divergence is tradable
        self.div_until = -1       # last bar index a divergence is tradable
        self.cross_until = -1     # last bar index a cross-up is tradable
        self.entries = []         # bar indices of filled buys
        self.pending = None       # "buy" / "sell" while an order awaits its fill
        self.cooldown_until = -1  # first bar index a new entry is allowed
        self.order_n = 0
        self.filled_buys = 0      # filled buys of any kind (primary or insurance)
        self.fb_active = False    # the open position is the insurance trade
        self.pending_fb = False   # the pending order belongs to the insurance trade

    # -- indicators ------------------------------------------------------
    def _push_close(self, close):
        closes = self.closes
        closes.append(close)
        i = len(closes) - 1
        n = self.p["rsi_len"]
        if i == 0:
            self.rsi.append(None)
            return
        ch = closes[i] - closes[i - 1]
        gain = float(ch) if ch > 0 else 0.0
        loss = float(-ch) if ch < 0 else 0.0
        if i <= n:
            # seed with the simple average of the first n changes
            if i == 1:
                self._sg, self._sl = gain, loss
            else:
                self._sg += gain
                self._sl += loss
            if i < n:
                self.rsi.append(None)
                return
            self._avg_gain = self._sg / n
            self._avg_loss = self._sl / n
        else:
            self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
            self._avg_loss = (self._avg_loss * (n - 1) + loss) / n
        if self._avg_loss == 0.0:
            self.rsi.append(100.0 if self._avg_gain > 0 else 50.0)
        else:
            rs = self._avg_gain / self._avg_loss
            self.rsi.append(100.0 - 100.0 / (1.0 + rs))
        # RSI cross-up through the oversold level
        prev = self.rsi[i - 1]
        if prev is not None and prev <= self.p["rsi_os"] < self.rsi[i]:
            self.cross_until = i + self.p["cross_valid"] - 1
        self._check_swing(i)

    def _check_swing(self, i):
        span = self.p["swing_span"]
        s = i - span  # the candidate swing bar, now fully surrounded
        if s < span or self.rsi[s] is None:
            return
        closes = self.closes
        c = closes[s]
        left = closes[s - span:s]
        right = closes[s + 1:s + span + 1]
        if len(right) < span:
            return
        if not (c < min(left) and c <= min(right)):
            return
        if self.swings and self.swings[-1][0] == s:
            return
        prev = self.swings[-1] if self.swings else None
        self.swings.append((s, c, self.rsi[s]))
        if prev is not None and c < prev[1] and self.rsi[s] > prev[2]:
            # bullish divergence: lower price low, higher RSI low. Require a
            # real retest shape: the two lows far enough apart, with a real
            # bounce between them (a grind's micro-bounces do not count).
            peak = max(self.closes[prev[0]:s + 1])
            if s - prev[0] >= self.p["div_min_gap"] and peak * 10000 >= c * (10000 + self.p["div_min_bounce_bps"]):
                self.div_from = s + self.p["div_confirm"]
                self.div_until = s + self.p["div_valid"] - 1

    # -- helpers ---------------------------------------------------------
    def _basis_price(self, holding):
        # price (at price_scale) whose value() equals the position's cost
        return holding.cost * 10 ** (self.qty_scale + self.price_scale) // (holding.qty * 10 ** self.quote_scale)

    def _next_id(self, tag):
        self.order_n += 1
        return "%s%d" % (tag, self.order_n)

    # -- the per-batch decision ------------------------------------------
    def on_batch(self, state, batch):
        for bar in batch["bars"]:
            if bar["symbol"] == SYMBOL:
                self._push_close(int(bar["c"]))
        # fold the fills of the orders decided on the previous batch: any
        # resolution (filled or rejected) clears the pending flag
        for fill in batch["fills"]:
            if self.pending == "buy" and fill["side"] == "buy":
                if fill.get("status") == "filled":
                    self.filled_buys += 1
                    if self.pending_fb:
                        self.fb_active = True
                    else:
                        self.entries.append(len(self.closes) - 1)
                self.pending = None
                self.pending_fb = False
            elif self.pending == "sell" and fill["side"] == "sell":
                if fill.get("status") == "filled":
                    self.cooldown_until = len(self.closes) - 1 + self.p["cooldown"]
                    if self.pending_fb:
                        self.fb_active = False
                self.pending = None
                self.pending_fb = False
        if state.warming_up or not self.closes:
            return None
        i = len(self.closes) - 1
        close = self.closes[i]
        holding = state.positions.get(SYMBOL)

        if holding is not None and self.pending is None:
            basis = self._basis_price(holding)
            if self.fb_active:
                tp = basis * (10000 + self.p["fb_tp_bps"]) // 10000
                sl = basis * (10000 - self.p["fb_sl_bps"]) // 10000
            else:
                tp = basis * (10000 + self.p["tp_bps"]) // 10000
                sl = basis * (10000 - self.p["sl_bps"]) // 10000
            if close >= tp or close <= sl:
                self.pending = "sell"
                self.pending_fb = self.fb_active
                return [{
                    "side": "sell",
                    "client_id": self._next_id("s"),
                    "symbol": SYMBOL,
                    "qty": holding.qty,
                    "max_slippage_bps": self.p["sell_slip_bps"],
                }]
            return None

        if holding is not None or self.pending is not None:
            return None
        if i < self.p["min_bars"] or i < self.cooldown_until:
            return None
        orders = self._try_primary(state, i, close)
        if orders is not None:
            return orders
        return self._try_fallback(state, i, close)

    # -- entries -----------------------------------------------------------
    def _try_primary(self, state, i, close):
        recent = sum(1 for e in self.entries if i - e < self.p["trip_win"])
        if recent >= self.p["max_trips"]:
            return None
        win = self.closes[max(0, i - self.p["high_win"] + 1):i + 1]
        if close * 10000 > max(win) * (10000 - self.p["dip_bps"]):
            return None  # not dipped far enough below the trailing high
        if i > self.p["late_cutoff"]:
            return None
        div_ok = self.div_from <= i <= self.div_until
        cross_ok = self.p["cross_on"] == 1 and i <= self.cross_until
        if not (div_ok or cross_ok):
            return None
        # Zone gate: a dip is only bought at the capitulation low itself
        # (a deep fall from the long high, printing the low now) or on a
        # retest of an old low (divergence only — a falling knife through
        # the level has no divergence and is skipped).
        long = self.closes[max(0, i - self.p["long_win"] + 1):i + 1]
        hi = max(long)
        lo = min(long)
        lo_age = 0
        for j in range(len(long) - 1, -1, -1):
            if long[j] == lo:
                lo_age = len(long) - 1 - j
                break
        fall_bps = (1.0 - close / hi) * 10000
        above_bps = (close / lo - 1.0) * 10000
        rsi_now = self.rsi[i]
        zone_cap = (fall_bps >= self.p["cap_fall_bps"] and above_bps <= self.p["cap_band_bps"]
                    and rsi_now is not None and rsi_now <= self.p["cap_rsi_max"])
        zone_ret = lo_age >= self.p["retest_age"] and above_bps <= self.p["retest_band_bps"]
        if zone_ret and not zone_cap and not div_ok:
            return None
        if not (zone_cap or zone_ret):
            return None
        notional = state.cash * self.p["size_pct"] // 100
        room = state.weight_room(SYMBOL)
        if room < notional:
            notional = room
        if notional < self.min_notional:
            return None
        self.pending = "buy"
        self.pending_fb = False
        return [{
            "side": "buy",
            "client_id": self._next_id("b"),
            "symbol": SYMBOL,
            "notional_quote_units": notional,
            "max_slippage_bps": self.p["buy_slip_bps"],
        }]

    def _try_fallback(self, state, i, close):
        # Insurance trade: only when nothing has filled all window and the
        # window is running out. One small dip buy with tight exits, so a
        # quiet race still produces a trade instead of failing the
        # at-least-one-fill guardrail. Once it has filled, filled_buys > 0
        # keeps it from ever firing again — at most one such round trip.
        if self.filled_buys > 0:
            return None
        if i < self.p["fb_from"] or i > self.p["fb_cutoff"]:
            return None
        win = self.closes[max(0, i - self.p["high_win"] + 1):i + 1]
        if close * 10000 > max(win) * (10000 - self.p["fb_dip_bps"]):
            return None  # no modest dip on the table
        rsi_now = self.rsi[i]
        if rsi_now is None or rsi_now > self.p["fb_rsi_max"]:
            return None
        notional = state.cash * self.p["fb_size_pct"] // 100
        room = state.weight_room(SYMBOL)
        if room < notional:
            notional = room
        if notional < self.min_notional:
            return None
        self.pending = "buy"
        self.pending_fb = True
        return [{
            "side": "buy",
            "client_id": self._next_id("f"),
            "symbol": SYMBOL,
            "notional_quote_units": notional,
            "max_slippage_bps": self.p["buy_slip_bps"],
        }]

def main():
    holder = {}

    def on_batch(state, batch):
        bot = holder.get("bot")
        if bot is None:
            bot = holder["bot"] = Bot(state.hello)
        return bot.on_batch(state, batch)

    makefun_bars.run(on_batch)


if __name__ == "__main__":
    main()
