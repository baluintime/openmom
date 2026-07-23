"""ATM contract selection from the live Upstox option chain.

For a signal side (CE for long, PE for short) this returns the ATM contract.
delta / OI / spread are attached for logging.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import IST, SPOT_INSTRUMENT_KEY, TFConfig
from .upstox_client import UpstoxClient, UpstoxError

TZ = ZoneInfo(IST)


@dataclass
class Contract:
    instrument_key: str
    trading_symbol: str
    side: str            # CE | PE
    strike: float
    expiry: str
    ltp: float
    delta: float | None
    oi: float | None
    spread_pct: float | None
    lot_size: int
    spot: float


class OptionSelector:
    def __init__(self, client: UpstoxClient):
        self.client = client
        self._expiry: str | None = None
        self._lot_size: int | None = None
        self._symbols: dict[str, str] = {}
        self._loaded_day: str | None = None

    async def _load(self) -> None:
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        if self._loaded_day == today and self._expiry and self._expiry >= today:
            return
        contracts = await self.client.option_contracts(SPOT_INSTRUMENT_KEY)
        expiries = sorted({c["expiry"] for c in contracts if c.get("expiry", "") >= today})
        if not expiries:
            raise UpstoxError("No NIFTY option expiries from Upstox")
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

    async def select(self, side: str) -> Contract:
        """Return the ATM contract for the given side."""
        await self._load()
        chain = await self.client.option_chain(SPOT_INSTRUMENT_KEY, self._expiry)
        if not chain:
            raise UpstoxError("Empty option chain from Upstox")
        spot = float(chain[0].get("underlying_spot_price") or 0.0)
        strikes = sorted({float(r["strike_price"]) for r in chain})
        atm_strike = min(strikes, key=lambda s: abs(s - spot))
        leg_key = "call_options" if side == "CE" else "put_options"
        row = next((r for r in chain if float(r["strike_price"]) == atm_strike), None)
        leg = (row or {}).get(leg_key) or {}
        md = leg.get("market_data") or {}
        gk = leg.get("option_greeks") or {}
        ikey = leg.get("instrument_key")
        ltp = float(md.get("ltp") or 0.0)
        if not ikey or ltp <= 0:
            raise UpstoxError(f"No tradeable ATM {side} contract (spot {spot})")
        delta = gk.get("delta")
        delta = abs(float(delta)) if delta is not None else None
        oi = md.get("oi")
        oi = float(oi) if oi is not None else None
        bid = float(md.get("bid_price") or 0.0)
        ask = float(md.get("ask_price") or 0.0)
        spread_pct = (ask - bid) / ltp * 100 if (bid > 0 and ask > 0) else None
        return Contract(instrument_key=ikey,
                        trading_symbol=self._symbols.get(ikey, ikey),
                        side=side, strike=atm_strike, expiry=self._expiry or "",
                        ltp=ltp, delta=delta, oi=oi, spread_pct=spread_pct,
                        lot_size=self._lot_size or 75, spot=spot)
