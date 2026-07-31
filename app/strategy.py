"""The two strategies being compared (per the Scalp Trading spec):

  1 · Renko            — ATR/fixed bricks, 2-brick confirmation, EMA-20 overlay.
  2 · Fast Ichimoku    — accelerated Tenkan/Kijun/Kumo, breakout + TK cross.

Each is a pure decision function over the NIFTY spot candle series (closed
candles only). It returns the DESIRED side after the latest completed candle:
  "long" -> hold a CE,  "short" -> hold a PE,  None -> flat.
The engine compares this to the strategy's current side and acts.
"""
from __future__ import annotations

from .market import ema_series

STRATEGIES = {
    1: {"name": "Renko", "desc": "Tick-driven bricks (2-box reversal): 2 bricks in "
                                 "trend + EMA-20; exit on reversal brick / wick SL / target."},
    2: {"name": "Fast Ichimoku", "desc": "Price breaks the Kumo with Tenkan/Kijun "
                                         "aligned; exit on close across Tenkan."},
}


# ---------------- indicators ----------------

def atr(candles, period: int) -> float | None:
    """Wilder ATR of the last `period` candles (needs period+1 candles)."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atrv = sum(trs[:period]) / period
    for tr in trs[period:]:
        atrv = (atrv * (period - 1) + tr) / period
    return atrv


def renko_dirs(prices: list[float], brick: float) -> list[int]:
    """Brick directions (+1 up, -1 down) built from closes. Simple 1-box
    construction: each full `brick` move prints a brick in that direction."""
    bricks: list[int] = []
    if not prices or brick <= 0:
        return bricks
    last = prices[0]
    for p in prices[1:]:
        diff = p - last
        if diff >= brick:
            n = int(diff // brick)
            bricks += [1] * n
            last += n * brick
        elif diff <= -brick:
            n = int((-diff) // brick)
            bricks += [-1] * n
            last -= n * brick
    return bricks


def _rolling_mid(highs, lows, period, i):
    if i < period - 1:
        return None
    hi = max(highs[i - period + 1:i + 1])
    lo = min(lows[i - period + 1:i + 1])
    return (hi + lo) / 2.0


def ichimoku(candles, tenkan_p, kijun_p, senkoub_p):
    """Returns (tenkan, kijun, spanA, spanB) arrays (unshifted)."""
    n = len(candles)
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    tenkan = [None] * n
    kijun = [None] * n
    spanA = [None] * n
    spanB = [None] * n
    for i in range(n):
        tenkan[i] = _rolling_mid(highs, lows, tenkan_p, i)
        kijun[i] = _rolling_mid(highs, lows, kijun_p, i)
        if tenkan[i] is not None and kijun[i] is not None:
            spanA[i] = (tenkan[i] + kijun[i]) / 2.0
        spanB[i] = _rolling_mid(highs, lows, senkoub_p, i)
    return tenkan, kijun, spanA, spanB


# ---------------- brick sizing ----------------

def brick_size(candles, cfg) -> float:
    if getattr(cfg, "renko_mode", "atr") == "fixed":
        return max(0.05, float(cfg.renko_points))
    a = atr(candles, cfg.atr_period)
    if a is None or a <= 0:
        return max(0.05, float(cfg.renko_points))  # fallback until ATR is warm
    return round(a, 2)


# ---------------- decisions ----------------

def renko_decide(candles, cfg, cur_side):
    closes = [c.close for c in candles]
    brick = brick_size(candles, cfg)
    bricks = renko_dirs(closes, brick)
    ema = ema_series(closes, cfg.ema_filter_period)
    if len(bricks) < 2 or ema[-1] is None:
        return cur_side if cur_side else None
    close, e = closes[-1], ema[-1]
    if cur_side == "long":
        return None if bricks[-1] == -1 else "long"   # reversal brick -> exit
    if cur_side == "short":
        return None if bricks[-1] == 1 else "short"
    if bricks[-1] == 1 and bricks[-2] == 1 and close > e:
        return "long"
    if bricks[-1] == -1 and bricks[-2] == -1 and close < e:
        return "short"
    return None


def ichimoku_decide(candles, cfg, cur_side):
    n = len(candles)
    disp = cfg.displacement
    if n < 2:
        return cur_side
    t, k, sa, sb = ichimoku(candles, cfg.tenkan, cfg.kijun, cfg.senkou_b)
    i = n - 1
    close, close_p = candles[i].close, candles[i - 1].close
    if None in (t[i], k[i]):
        return cur_side
    # exits: close on the wrong side of Tenkan
    if cur_side == "long":
        return None if close < t[i] else "long"
    if cur_side == "short":
        return None if close > t[i] else "short"
    # entries need the cloud (spanA/spanB from `disp` candles ago)
    j = i - disp
    if j < 0 or None in (sa[j], sb[j]):
        return None
    ct, cb = max(sa[j], sb[j]), min(sa[j], sb[j])
    if cb <= close <= ct:
        return None                       # inside the Kumo -> no trade
    long_now = close > ct and t[i] > k[i]
    short_now = close < cb and t[i] < k[i]
    # require this to be a fresh transition (breakout + alignment just formed)
    jp = j - 1
    long_prev = short_prev = False
    if jp >= 0 and None not in (sa[jp], sb[jp], t[i - 1], k[i - 1]):
        ctp, cbp = max(sa[jp], sb[jp]), min(sa[jp], sb[jp])
        long_prev = close_p > ctp and t[i - 1] > k[i - 1]
        short_prev = close_p < cbp and t[i - 1] < k[i - 1]
    if long_now and not long_prev:
        return "long"
    if short_now and not short_prev:
        return "short"
    return None


def decide(strat: int, candles, cfg, cur_side):
    if strat == 1:
        return renko_decide(candles, cfg, cur_side)
    if strat == 2:
        return ichimoku_decide(candles, cfg, cur_side)
    return None


def context(strat: int, candles, cfg) -> str:
    """One-line indicator context for logging an entry/exit."""
    closes = [c.close for c in candles]
    if strat == 1:
        brick = brick_size(candles, cfg)
        bricks = renko_dirs(closes, brick)
        ema = ema_series(closes, cfg.ema_filter_period)
        last2 = "".join("G" if b == 1 else "R" for b in bricks[-2:]) or "—"
        ev = f"{ema[-1]:.2f}" if ema and ema[-1] is not None else "—"
        return f"brick {brick:.2f} · last2 {last2} · EMA20 {ev} · close {closes[-1]:.2f}"
    t, k, sa, sb = ichimoku(candles, cfg.tenkan, cfg.kijun, cfg.senkou_b)
    i = len(candles) - 1
    j = i - cfg.displacement
    cloud = "—"
    if j >= 0 and None not in (sa[j], sb[j]):
        ct, cb = max(sa[j], sb[j]), min(sa[j], sb[j])
        c = closes[-1]
        cloud = "above" if c > ct else "below" if c < cb else "inside"
    tv = f"{t[i]:.2f}" if t[i] is not None else "—"
    kv = f"{k[i]:.2f}" if k[i] is not None else "—"
    return f"cloud {cloud} · Tenkan {tv} · Kijun {kv} · close {closes[-1]:.2f}"
