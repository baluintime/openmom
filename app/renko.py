"""Tick-driven Renko brick engine.

Bricks are built from a live price stream (the NIFTY spot LTP sampled every
second), not from candle closes — so brick completion is evaluated the moment
price crosses a threshold, matching the spec's "triggers immediately upon
precise brick threshold completion". Uses classic 2-box reversal and tracks the
wick (extreme against the brick's direction) for the wick-based stop-loss.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Brick:
    dir: int          # +1 up, -1 down
    open: float
    close: float
    wick_hi: float
    wick_lo: float


class RenkoState:
    """Feed prices via update(price); it returns the list of bricks that just
    completed. Deterministic for a given price stream and brick size."""

    def __init__(self, brick: float, cap: int = 1000):
        self.brick = max(float(brick), 0.01)
        self.cap = cap
        self.bricks: list[Brick] = []
        self.anchor: float | None = None   # close of the last brick
        self.dir = 0
        self.hi: float | None = None        # extremes since the last brick
        self.lo: float | None = None

    def update(self, price: float) -> list[Brick]:
        out: list[Brick] = []
        if price is None or price <= 0:
            return out
        if self.anchor is None:
            self.anchor = price
            self.hi = self.lo = price
            return out
        self.hi = max(self.hi, price)
        self.lo = min(self.lo, price)
        b = self.brick
        while True:
            if self.dir >= 0 and price >= self.anchor + b:
                top = self.anchor + b
                out.append(Brick(1, self.anchor, top, top, self.lo))
                self.anchor = top; self.dir = 1; self.hi = self.lo = price
            elif self.dir <= 0 and price <= self.anchor - b:
                bot = self.anchor - b
                out.append(Brick(-1, self.anchor, bot, self.hi, bot))
                self.anchor = bot; self.dir = -1; self.hi = self.lo = price
            elif self.dir > 0 and price <= self.anchor - 2 * b:   # reversal down
                bot = self.anchor - 2 * b
                out.append(Brick(-1, self.anchor, bot, self.hi, bot))
                self.anchor = bot; self.dir = -1; self.hi = self.lo = price
            elif self.dir < 0 and price >= self.anchor + 2 * b:   # reversal up
                top = self.anchor + 2 * b
                out.append(Brick(1, self.anchor, top, top, self.lo))
                self.anchor = top; self.dir = 1; self.hi = self.lo = price
            else:
                break
        if out:
            self.bricks.extend(out)
            if len(self.bricks) > self.cap:
                self.bricks = self.bricks[-self.cap:]
        return out

    def run_start(self) -> Brick | None:
        """First brick of the current same-direction run (for the SL wick)."""
        if not self.bricks:
            return None
        d = self.bricks[-1].dir
        i = len(self.bricks) - 1
        while i > 0 and self.bricks[i - 1].dir == d:
            i -= 1
        return self.bricks[i]


def seed_from_candles(state: RenkoState, candles) -> None:
    """Warm up the brick trail from historical OHLC candles, walking each
    candle low→high or high→low by its direction to approximate the path."""
    for c in candles:
        if c.close >= c.open:
            for p in (c.open, c.low, c.high, c.close):
                state.update(p)
        else:
            for p in (c.open, c.high, c.low, c.close):
                state.update(p)
