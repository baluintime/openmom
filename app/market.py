"""Candle building and SMA/EMA signal logic on the NIFTY 50 spot chart.

All timeframes are built from 1-minute candles fetched from Upstox (its native
5m/15m aggregates finalize late and caused chart-vs-execution mismatches).
Signals use CLOSED-candle values only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import IST, SPOT_INSTRUMENT_KEY, TFConfig
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
        return cls(ts=datetime.fromisoformat(row[0]).astimezone(TZ),
                   open=float(row[1]), high=float(row[2]),
                   low=float(row[3]), close=float(row[4]))


def sma_series(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    run = 0.0
    for i, c in enumerate(closes):
        run += c
        if i >= period:
            run -= closes[i - period]
        if i >= period - 1:
            out[i] = run / period
    return out


def ema_series(closes: list[float], period: int) -> list[float | None]:
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


class SpotFeed:
    """Builds tf-minute candles from live 1-minute data and computes SMA/EMA."""

    def __init__(self, client: UpstoxClient, timeframe: int, grace_sec: int = 6):
        self.client = client
        self.tf = timeframe
        self.grace_sec = grace_sec
        self.candles: list[Candle] = []
        self.sma: list[float | None] = []
        self.ema: list[float | None] = []
        self.last_processed_ts: datetime | None = None
        self._seed: list[Candle] = []
        self._seed_day: str | None = None
        self._confirm: dict = {}
        self.note: str | None = None

    async def _load_seed(self, now: datetime) -> None:
        """Previous-session 1-min candles so the averages are valid from the open."""
        day = now.strftime("%Y-%m-%d")
        if self._seed_day == day:
            return
        frm = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        to = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            rows = await self.client.historical_candles(SPOT_INSTRUMENT_KEY, 1, frm, to)
            ones = [Candle.from_api(r) for r in rows]
            self._seed = self._aggregate(ones, self.tf)[-60:]
            self._seed_day = day
        except Exception:
            self._seed = []

    @staticmethod
    def _aggregate(one_min: list[Candle], tf: int) -> list[Candle]:
        """Group 1-minute candles into tf-minute candles. A bucket is emitted
        only once its FINAL minute is present, so a close is never a partial."""
        if tf == 1:
            return list(one_min)
        buckets: dict[datetime, list[Candle]] = {}
        for c in one_min:
            start = c.ts - timedelta(minutes=c.ts.minute % tf, seconds=c.ts.second,
                                     microseconds=c.ts.microsecond)
            buckets.setdefault(start, []).append(c)
        out = []
        for start in sorted(buckets):
            cs = sorted(buckets[start], key=lambda c: c.ts)
            if cs[-1].ts != start + timedelta(minutes=tf - 1):
                continue
            out.append(Candle(ts=start, open=cs[0].open,
                              high=max(c.high for c in cs),
                              low=min(c.low for c in cs), close=cs[-1].close))
        return out

    async def refresh(self) -> list[Candle]:
        """Fetch/build candles; return newly completed candles since last call."""
        now = datetime.now(TZ)
        await self._load_seed(now)
        rows = await self.client.intraday_candles(SPOT_INSTRUMENT_KEY, 1)
        today = self._aggregate([Candle.from_api(r) for r in rows], self.tf)
        cutoff = timedelta(minutes=self.tf, seconds=self.grace_sec)
        completed = [c for c in today if now >= c.ts + cutoff]

        # a just-completed candle is trusted only after two identical reads
        if completed and self.last_processed_ts is not None:
            newest = completed[-1]
            if newest.ts > self.last_processed_ts:
                seen = self._confirm.get(newest.ts)
                self._confirm = {newest.ts: newest.close}
                if seen is None:
                    completed = completed[:-1]
                elif seen != newest.close:
                    self.note = (f"candle {newest.ts:%H:%M} close revised "
                                 f"{seen:.2f} → {newest.close:.2f} — waiting for stable data")
                    completed = completed[:-1]

        self.candles = self._seed + completed
        closes = [c.close for c in self.candles]
        self.sma = sma_series(closes, self._sma_period)
        self.ema = ema_series(closes, self._ema_period)

        if self.last_processed_ts is None:
            new = completed[-1:]
        else:
            new = [c for c in completed if c.ts > self.last_processed_ts]
        if completed:
            self.last_processed_ts = completed[-1].ts
        return new

    # periods are pushed in by the engine before each refresh
    _sma_period = 20
    _ema_period = 9

    def set_periods(self, sma: int, ema: int) -> None:
        self._sma_period, self._ema_period = sma, ema

    def signal_state(self) -> dict | None:
        """State of the latest completed candle: its close, SMA, EMA and the
        above/below flags. None until enough candles exist."""
        if not self.candles or self.sma[-1] is None or self.ema[-1] is None:
            return None
        c = self.candles[-1]
        s, e = self.sma[-1], self.ema[-1]
        return {"ts": c.ts, "close": c.close, "sma": s, "ema": e,
                "above_both": c.close > s and c.close > e,
                "below_both": c.close < s and c.close < e,
                "above_either": c.close > s or c.close > e,
                "below_either": c.close < s or c.close < e}

    def snapshot(self, points: int = 90) -> dict:
        cs = self.candles[-points:]
        ss = self.sma[-points:]
        es = self.ema[-points:]
        last = self.candles[-1] if self.candles else None
        return {
            "timeframe": self.tf,
            "last_ts": last.ts.strftime("%H:%M") if last else None,
            "last_close": round(last.close, 2) if last else None,
            "sma": round(self.sma[-1], 2) if self.sma and self.sma[-1] is not None else None,
            "ema": round(self.ema[-1], 2) if self.ema and self.ema[-1] is not None else None,
            "series": [
                {"t": c.ts.strftime("%H:%M"), "c": round(c.close, 2),
                 "s": round(s, 2) if s is not None else None,
                 "e": round(e, 2) if e is not None else None}
                for c, s, e in zip(cs, ss, es)
            ],
        }
