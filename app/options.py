"""Contract selection from the live Upstox option chain.

For a signal side (CE for long, PE for short) this returns an ATM contract and
a scored ITM contract. The ITM pick is chosen from the strikes within
itm_max_depth of ATM, ranked by liquidity: highest OI, tightest bid/ask spread,
and a delta at/above the configured floor.
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
    kind: str            # "ATM" | "ITM"
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

    async def select(self, side: str, cfg: TFConfig) -> dict[str, Contract]:
        """Return {"ATM": Contract, "ITM": Contract} for the given side.
        Either may be missing if no tradeable contract is found."""
        await self._load()
        chain = await self.client.option_chain(SPOT_INSTRUMENT_KEY, self._expiry)
        if not chain:
            raise UpstoxError("Empty option chain from Upstox")
        spot = float(chain[0].get("underlying_spot_price") or 0.0)
        strikes = sorted({float(r["strike_price"]) for r in chain})
        atm_strike = min(strikes, key=lambda s: abs(s - spot))
        step = min((b - a for a, b in zip(strikes, strikes[1:])), default=50.0)
        leg_key = "call_options" if side == "CE" else "put_options"
        by_strike = {float(r["strike_price"]): r for r in chain}

        def build(strike: float, kind: str) -> Contract | None:
            row = by_strike.get(strike)
            if not row:
                return None
            leg = row.get(leg_key) or {}
            md = leg.get("market_data") or {}
            gk = leg.get("option_greeks") or {}
            ikey = leg.get("instrument_key")
            ltp = float(md.get("ltp") or 0.0)
            if not ikey or ltp <= 0:
                return None
            delta = gk.get("delta")
            delta = abs(float(delta)) if delta is not None else None
            oi = md.get("oi")
            oi = float(oi) if oi is not None else None
            bid = float(md.get("bid_price") or 0.0)
            ask = float(md.get("ask_price") or 0.0)
            spread_pct = (ask - bid) / ltp * 100 if (bid > 0 and ask > 0) else None
            return Contract(kind=kind, instrument_key=ikey,
                            trading_symbol=self._symbols.get(ikey, ikey),
                            side=side, strike=strike, expiry=self._expiry or "",
                            ltp=ltp, delta=delta, oi=oi, spread_pct=spread_pct,
                            lot_size=self._lot_size or 75, spot=spot)

        out: dict[str, Contract] = {}
        atm = build(atm_strike, "ATM")
        if atm:
            out["ATM"] = atm

        # ITM candidates: CE -> strikes below spot, PE -> strikes above
        cand = []
        for d in range(1, cfg.itm_max_depth + 1):
            strike = atm_strike - d * step if side == "CE" else atm_strike + d * step
            c = build(strike, "ITM")
            if c is None:
                continue
            if cfg.itm_min_delta and c.delta is not None and c.delta < cfg.itm_min_delta:
                continue
            if cfg.itm_min_oi and c.oi is not None and c.oi < cfg.itm_min_oi:
                continue
            if (cfg.itm_max_spread_pct and c.spread_pct is not None
                    and c.spread_pct > cfg.itm_max_spread_pct):
                continue
            cand.append(c)
        if not cand:  # relax filters: nearest ITM with a valid quote
            for d in range(1, cfg.itm_max_depth + 1):
                strike = atm_strike - d * step if side == "CE" else atm_strike + d * step
                c = build(strike, "ITM")
                if c:
                    cand.append(c)
                    break
        if cand:
            # rank: high OI, tight spread (both optional), then shallow ITM
            def score(c: Contract):
                oi = c.oi or 0.0
                sp = c.spread_pct if c.spread_pct is not None else 5.0
                return (oi, -sp, -abs(c.strike - atm_strike))
            out["ITM"] = max(cand, key=score)
        return out
