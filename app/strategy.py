"""The three strategies being compared. Each is a pure decision function over
the NIFTY spot close, SMA and EMA series (closed-candle values only).

Each returns the DESIRED side after the latest completed candle:
  "long"  -> should be long  (hold a CE)
  "short" -> should be short (hold a PE)
  None    -> should be flat
The engine compares this to the strategy's current side and acts (exit an open
position, enter a new one, or flip).
"""
from __future__ import annotations

STRATEGIES = {
    1: {"name": "S1 · MA Zone",
        "desc": "Long while close is above BOTH SMA & EMA; short while below both."},
    2: {"name": "S2 · MA Momentum",
        "desc": "Enter on EMA/SMA cross; hold while the gap widens, exit when it narrows."},
    3: {"name": "S3 · Price Crossover",
        "desc": "Enter when close crosses above/below both MAs; exit on close crossing the SMA."},
}


def decide(strat: int, closes: list[float], sma: list[float | None],
           ema: list[float | None], cur_side: str | None) -> str | None:
    """Desired side after the latest completed candle. cur_side is the
    strategy's current side (None = flat)."""
    i = len(closes) - 1
    if i < 1 or None in (sma[i], ema[i], sma[i - 1], ema[i - 1]):
        return cur_side  # not enough data — hold whatever we have
    c, cp = closes[i], closes[i - 1]
    s, sp = sma[i], sma[i - 1]
    e, ep = ema[i], ema[i - 1]

    if strat == 1:
        # long exactly when above both, short when below both, else flat
        if c > s and c > e:
            return "long"
        if c < s and c < e:
            return "short"
        return None

    if strat == 2:
        cross_up = e > s and ep <= sp        # EMA crosses above SMA
        cross_dn = e < s and ep >= sp        # EMA crosses below SMA
        if cur_side == "long":
            # hold while (EMA-SMA) widens; exit when it narrows
            if (e - s) < (ep - sp):
                return "short" if cross_dn else None
            return "long"
        if cur_side == "short":
            if (s - e) < (sp - ep):
                return "long" if cross_up else None
            return "short"
        if cross_up:
            return "long"
        if cross_dn:
            return "short"
        return None

    if strat == 3:
        above_both, below_both = c > s and c > e, c < s and c < e
        ab_prev, bb_prev = cp > sp and cp > ep, cp < sp and cp < ep
        if cur_side == "long":
            if c < s:                        # close crossed below SMA -> exit
                return "short" if (below_both and not bb_prev) else None
            return "long"
        if cur_side == "short":
            if c > s:                        # close crossed above SMA -> exit
                return "long" if (above_both and not ab_prev) else None
            return "short"
        if above_both and not ab_prev:
            return "long"
        if below_both and not bb_prev:
            return "short"
        return None

    return None
