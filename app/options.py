"""Selection of the tradeable NIFTY option contract from the live option chain.

Per spec: nearest expiry, ATM or slightly ITM, premium band ~Rs.80-90,
delta ~0.5-0.6. Falls back to the nearest-to-band ATM/ITM contract when
nothing sits exactly inside the band (e.g. far from expiry).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import IST, SPOT_INSTRUMENT_KEY, StrategyConfig
from .upstox_client import UpstoxClient, UpstoxError

TZ = ZoneInfo(IST)


@dataclass
class SelectedOption:
    instrument_key: str
    trading_symbol: str
    side: str            # CE / PE
    strike: float
    expiry: str
    ltp: float
    delta: float | None
    lot_size: int
    spot: float


class OptionSelector:
    def __init__(self, client: UpstoxClient):
        self.client = client
        self._expiry: str | None = None
        self._lot_size: int | None = None
        self._symbols: dict[str, str] = {}
        self._loaded_day: str | None = None

    async def _load_contracts(self) -> None:
        """Nearest expiry + lot size + trading symbols, refreshed daily from Upstox."""
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        if self._loaded_day == today and self._expiry and self._expiry >= today:
            return
        contracts = await self.client.option_contracts(SPOT_INSTRUMENT_KEY)
        expiries = sorted({c["expiry"] for c in contracts if c.get("expiry", "") >= today})
        if not expiries:
            raise UpstoxError("No NIFTY option expiries returned by Upstox")
        self._expiry = expiries[0]
        near = [c for c in contracts if c["expiry"] == self._expiry]
        self._lot_size = int(near[0].get("lot_size") or 75)
        self._symbols = {c["instrument_key"]: c.get("trading_symbol", c["instrument_key"])
                         for c in near}
        self._loaded_day = today

    @property
    def expiry(self) -> str | None:
        return self._expiry

    @property
    def lot_size(self) -> int | None:
        return self._lot_size

    async def select(self, side: str, cfg: StrategyConfig) -> SelectedOption:
        await self._load_contracts()
        chain = await self.client.option_chain(SPOT_INSTRUMENT_KEY, self._expiry)
        if not chain:
            raise UpstoxError("Empty option chain from Upstox")
        spot = float(chain[0].get("underlying_spot_price") or 0.0)
        strikes = sorted({float(r["strike_price"]) for r in chain})
        atm = min(strikes, key=lambda s: abs(s - spot))
        step = min((b - a for a, b in zip(strikes, strikes[1:])), default=50.0)

        # ATM or slightly ITM only: for CE that's strikes at/below spot, PE at/above
        if side == "CE":
            allowed = {atm} | {atm - i * step for i in range(1, cfg.max_itm_strikes + 1)}
        else:
            allowed = {atm} | {atm + i * step for i in range(1, cfg.max_itm_strikes + 1)}

        leg_key = "call_options" if side == "CE" else "put_options"
        candidates = []
        for row in chain:
            strike = float(row["strike_price"])
            if strike not in allowed:
                continue
            leg = row.get(leg_key) or {}
            md = leg.get("market_data") or {}
            greeks = leg.get("option_greeks") or {}
            ltp = float(md.get("ltp") or 0.0)
            if ltp <= 0 or not leg.get("instrument_key"):
                continue
            delta = greeks.get("delta")
            delta = abs(float(delta)) if delta is not None else None
            candidates.append((strike, leg["instrument_key"], ltp, delta))

        if not candidates:
            raise UpstoxError(f"No tradeable {side} contract near ATM (spot {spot})")

        band_mid = (cfg.premium_band_low + cfg.premium_band_high) / 2.0

        def in_premium_band(c):
            return cfg.premium_band_low <= c[2] <= cfg.premium_band_high

        def in_delta_band(c):
            return c[3] is not None and cfg.delta_low <= c[3] <= cfg.delta_high

        # Preference: premium band + delta band > premium band > delta band > nearest ATM
        for pool in (
            [c for c in candidates if in_premium_band(c) and in_delta_band(c)],
            [c for c in candidates if in_premium_band(c)],
            [c for c in candidates if in_delta_band(c)],
        ):
            if pool:
                best = min(pool, key=lambda c: abs(c[2] - band_mid))
                break
        else:
            best = min(candidates, key=lambda c: abs(c[0] - atm))

        strike, ikey, ltp, delta = best
        return SelectedOption(
            instrument_key=ikey,
            trading_symbol=self._symbols.get(ikey, ikey),
            side=side, strike=strike, expiry=self._expiry or "",
            ltp=ltp, delta=delta, lot_size=self._lot_size or 75, spot=spot,
        )


def days_to_expiry(expiry: str) -> int:
    try:
        return (date.fromisoformat(expiry) - datetime.now(TZ).date()).days
    except Exception:
        return -1
