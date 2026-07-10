"""Candle handling and EMA/signal math for the NIFTY 50 spot chart.

The indicator is computed on the underlying spot index only (never on the
option premium chart, per spec) and signals fire strictly on *completed*
candles — the "Close" confirmation filter.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import IST, SPOT_INSTRUMENT_KEY, StrategyConfig
from .upstox_client import UpstoxClient

TZ = ZoneInfo(IST)


@dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float

    @classmethod
    def from_api(cls, row: list) -> "Candle":
        return cls(
            ts=datetime.fromisoformat(row[0]).astimezone(TZ),
            open=float(row[1]), high=float(row[2]),
            low=float(row[3]), close=float(row[4]),
        )


def ema_series(closes: list[float], period: int) -> list[float | None]:
    """EMA seeded with the SMA of the first `period` closes."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(closes)):
        prev = closes[i] * k + prev * (1 - k)
        out[i] = prev
    return out


@dataclass
class Signal:
    side: str          # "CE" or "PE"
    timeframe: int
    candle_ts: datetime
    close: float
    ema: float
    confirmed_from: datetime | None = None   # crossing candle, when this is a
                                             # confirmation (not the cross itself)


def find_confirmed_cross(closes: list[float], ema: list[float | None],
                         i: int, side: str, window: int) -> int | None:
    """Confirmation-entry search: candle i already closed beyond the EMA in
    `side`'s direction. Return the index of a raw EMA cross into that direction
    within the previous `window` candles, provided every close since stayed
    beyond the EMA (a close back across cancels the setup). None otherwise."""
    for back in range(1, window + 1):
        j = i - back
        if j < 1 or ema[j] is None or ema[j - 1] is None:
            return None
        if side == "CE":
            if closes[j] <= ema[j]:
                return None            # closed back across: setup dead
            if closes[j - 1] <= ema[j - 1]:
                return j               # this was the crossing candle
        else:
            if closes[j] >= ema[j]:
                return None
            if closes[j - 1] >= ema[j - 1]:
                return j
    return None


class SpotFeed:
    """Fetches real NIFTY 50 spot candles from Upstox for one timeframe and
    detects 9-EMA decisive-close breakouts on newly completed candles."""

    def __init__(self, client: UpstoxClient, timeframe_min: int, ema_period: int = 9,
                 grace_sec: int = 6):
        self.client = client
        self.tf = timeframe_min
        self.ema_period = ema_period
        self.grace_sec = grace_sec
        self.candles: list[Candle] = []
        self.ema: list[float | None] = []
        self.last_processed_ts: datetime | None = None
        self._seed: list[Candle] = []   # previous sessions, for EMA warm-up
        self._seed_day: str | None = None

    async def _load_seed(self, now: datetime) -> None:
        """Previous-session candles so the EMA is valid from the first bars of today."""
        day = now.strftime("%Y-%m-%d")
        if self._seed_day == day:
            return
        frm = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        to = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            rows = await self.client.historical_candles(SPOT_INSTRUMENT_KEY, self.tf, frm, to)
            self._seed = [Candle.from_api(r) for r in rows][-60:]
            self._seed_day = day
        except Exception:
            self._seed = []  # EMA will simply need `period` candles of today

    async def refresh(self) -> list[Candle]:
        """Fetch today's candles; returns newly *completed* candles since last call."""
        now = datetime.now(TZ)
        await self._load_seed(now)
        rows = await self.client.intraday_candles(SPOT_INSTRUMENT_KEY, self.tf)
        today = [Candle.from_api(r) for r in rows]
        # A candle stamped ts covers [ts, ts + tf). Upstox finalizes the row a
        # few seconds AFTER the boundary — reading at the exact boundary can
        # return a stale close (observed ~7 pts off on 5m) — so a candle only
        # counts as complete grace_sec after it ends.
        cutoff = timedelta(minutes=self.tf, seconds=self.grace_sec)
        completed_today = [c for c in today if now >= c.ts + cutoff]
        self.candles = self._seed + completed_today
        self.ema = ema_series([c.close for c in self.candles], self.ema_period)
        new = [c for c in completed_today
               if self.last_processed_ts is None or c.ts > self.last_processed_ts]
        if completed_today:
            self.last_processed_ts = completed_today[-1].ts
        return new

    def flat_threshold(self, cfg: StrategyConfig) -> float:
        """flat_ema_points is defined on the 5-min chart (per the spec) and
        scaled linearly for other timeframes — an EMA's slope per candle is
        proportional to the candle duration, so an unscaled threshold would
        veto nearly every 1-min crossover."""
        return cfg.flat_ema_points * self.tf / 5.0

    def evaluate_signal(self, cfg: StrategyConfig) -> tuple[Signal | None, dict]:
        """Signal on the latest completed candle, plus flat-EMA measurements
        {flat, move, needed}.

        CE: previous close at/below its EMA, latest close decisively above.
        PE: mirror image below.
        """
        flat_info = {"flat": False, "move": None, "needed": self.flat_threshold(cfg),
                     "raw_side": None, "margin": None}
        closes = [c.close for c in self.candles]
        ema = ema_series(closes, cfg.ema_period)
        if len(closes) < cfg.ema_period + max(2, cfg.flat_ema_lookback + 1):
            return None, flat_info
        prev_c, cur_c = closes[-2], closes[-1]
        prev_e, cur_e = ema[-2], ema[-1]
        if prev_e is None or cur_e is None:
            return None, flat_info

        if cfg.flat_ema_filter:
            ref = ema[-1 - cfg.flat_ema_lookback]
            if ref is not None:
                flat_info["move"] = abs(cur_e - ref)
                flat_info["flat"] = flat_info["move"] < flat_info["needed"]

        # raw cross first, then the decisive-margin qualification — a cross
        # that fails the margin is surfaced via flat_info so it can be logged
        side = None
        confirmed_from = None
        if prev_c <= prev_e and cur_c > cur_e:
            flat_info["raw_side"] = "CE"
            flat_info["margin"] = cur_c - cur_e
            if cur_c > cur_e + cfg.decisive_points:
                side = "CE"
        elif prev_c >= prev_e and cur_c < cur_e:
            flat_info["raw_side"] = "PE"
            flat_info["margin"] = cur_e - cur_c
            if cur_c < cur_e - cfg.decisive_points:
                side = "PE"
        elif cfg.confirm_window_candles > 0:
            # no fresh cross on this candle: confirmation-entry check for a
            # recent non-decisive cross that this candle's close now validates
            i = len(closes) - 1
            if cur_c > cur_e + cfg.decisive_points:
                j = find_confirmed_cross(closes, ema, i, "CE", cfg.confirm_window_candles)
                if j is not None:
                    side, confirmed_from = "CE", self.candles[j].ts
            elif cur_c < cur_e - cfg.decisive_points:
                j = find_confirmed_cross(closes, ema, i, "PE", cfg.confirm_window_candles)
                if j is not None:
                    side, confirmed_from = "PE", self.candles[j].ts
        if side is None:
            return None, flat_info

        return Signal(side=side, timeframe=self.tf, candle_ts=self.candles[-1].ts,
                      close=cur_c, ema=cur_e, confirmed_from=confirmed_from), flat_info

    def snapshot(self, points: int = 75) -> dict:
        cs = self.candles[-points:]
        es = self.ema[-points:]
        return {
            "timeframe": self.tf,
            "last_candle_ts": self.candles[-1].ts.isoformat() if self.candles else None,
            "last_close": self.candles[-1].close if self.candles else None,
            "ema": es[-1] if es and es[-1] is not None else None,
            "series": [
                {"t": c.ts.strftime("%H:%M"), "c": round(c.close, 2),
                 "e": round(e, 2) if e is not None else None}
                for c, e in zip(cs, es)
            ],
        }
