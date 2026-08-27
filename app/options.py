"""OTM option selection for a screened stock.

For a bullish stock (sell PUT) pick a strike below spot; for a bearish stock
(sell CALL) pick a strike above spot — both within `otm_strikes` of ATM. Among
the in-range strikes the most liquid (highest OI, tightest bid/ask spread) that
clears the OI/spread floors is chosen.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import EODConfig
from .upstox_client import UpstoxClient, UpstoxError


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


async def select_otm(client: UpstoxClient, cfg: EODConfig, *, equity_key: str,
                     expiry: str, side: str, spot: float, lot_size: int) -> Contract:
    """Choose the OTM contract to SELL for a screened stock."""
    chain = await client.option_chain(equity_key, expiry)
    if not chain:
        raise UpstoxError(f"Empty option chain for {equity_key} {expiry}")
    ref_spot = float(chain[0].get("underlying_spot_price") or spot or 0.0)
    strikes = sorted({float(r["strike_price"]) for r in chain})
    if not strikes:
        raise UpstoxError("No strikes in chain")
    atm = min(strikes, key=lambda s: abs(s - ref_spot))
    ai = strikes.index(atm)
    # OTM direction: PUT below spot, CALL above spot
    if side == "PE":
        cand_strikes = [s for s in strikes[max(0, ai - cfg.otm_strikes):ai] if s < ref_spot]
    else:
        cand_strikes = [s for s in strikes[ai + 1:ai + 1 + cfg.otm_strikes] if s > ref_spot]
    if not cand_strikes:
        raise UpstoxError(f"No OTM {side} strikes within {cfg.otm_strikes} of {ref_spot}")

    leg_key = "put_options" if side == "PE" else "call_options"
    by_strike = {float(r["strike_price"]): r for r in chain}

    def build(strike: float) -> Contract | None:
        leg = (by_strike.get(strike) or {}).get(leg_key) or {}
        md = leg.get("market_data") or {}
        gk = leg.get("option_greeks") or {}
        ikey = leg.get("instrument_key")
        ltp = float(md.get("ltp") or 0.0)
        if not ikey or ltp <= 0:
            return None
        delta = gk.get("delta")
        delta = float(delta) if delta is not None else None
        oi = md.get("oi")
        oi = float(oi) if oi is not None else None
        bid = float(md.get("bid_price") or 0.0)
        ask = float(md.get("ask_price") or 0.0)
        spread_pct = (ask - bid) / ltp * 100 if (bid > 0 and ask > 0) else None
        return Contract(instrument_key=ikey,
                        trading_symbol=leg.get("trading_symbol") or ikey,
                        side=side, strike=strike, expiry=expiry, ltp=ltp,
                        delta=delta, oi=oi, spread_pct=spread_pct,
                        lot_size=lot_size, spot=ref_spot)

    cands = [c for c in (build(s) for s in cand_strikes) if c]
    if not cands:
        raise UpstoxError(f"No quotable OTM {side} contract for {equity_key}")

    def passes(c: Contract) -> bool:
        if cfg.min_oi and c.oi is not None and c.oi < cfg.min_oi:
            return False
        if cfg.max_spread_pct and c.spread_pct is not None and c.spread_pct > cfg.max_spread_pct:
            return False
        return True

    filtered = [c for c in cands if passes(c)] or cands   # fall back if all filtered out

    def score(c: Contract):
        oi = c.oi or 0.0
        sp = c.spread_pct if c.spread_pct is not None else 99.0
        return (oi, -sp)
    return max(filtered, key=score)


async def select_itm(client: UpstoxClient, cfg: EODConfig, *, equity_key: str,
                     expiry: str, side: str, spot: float, lot_size: int) -> Contract:
    """Choose the ITM contract to BUY (the hedge leg). `side` is the option
    type to buy: CE is ITM below spot, PE is ITM above spot."""
    chain = await client.option_chain(equity_key, expiry)
    if not chain:
        raise UpstoxError(f"Empty option chain for {equity_key} {expiry}")
    ref_spot = float(chain[0].get("underlying_spot_price") or spot or 0.0)
    strikes = sorted({float(r["strike_price"]) for r in chain})
    if not strikes:
        raise UpstoxError("No strikes in chain")
    atm = min(strikes, key=lambda s: abs(s - ref_spot))
    ai = strikes.index(atm)
    depth = cfg.hedge_itm_strikes
    # ITM candidates, nearest-in first
    if side == "CE":
        cand_strikes = [s for s in reversed(strikes[:ai]) if s < ref_spot][:depth]
    else:
        cand_strikes = [s for s in strikes[ai + 1:] if s > ref_spot][:depth]
    if not cand_strikes:
        raise UpstoxError(f"No ITM {side} strike within {depth} of {ref_spot}")

    leg_key = "call_options" if side == "CE" else "put_options"
    by_strike = {float(r["strike_price"]): r for r in chain}

    def build(strike: float) -> Contract | None:
        leg = (by_strike.get(strike) or {}).get(leg_key) or {}
        md = leg.get("market_data") or {}
        gk = leg.get("option_greeks") or {}
        ikey = leg.get("instrument_key")
        ltp = float(md.get("ltp") or 0.0)
        if not ikey or ltp <= 0:
            return None
        delta = gk.get("delta")
        bid = float(md.get("bid_price") or 0.0)
        ask = float(md.get("ask_price") or 0.0)
        return Contract(instrument_key=ikey,
                        trading_symbol=leg.get("trading_symbol") or ikey,
                        side=side, strike=strike, expiry=expiry, ltp=ltp,
                        delta=float(delta) if delta is not None else None,
                        oi=float(md["oi"]) if md.get("oi") is not None else None,
                        spread_pct=(ask - bid) / ltp * 100 if (bid > 0 and ask > 0) else None,
                        lot_size=lot_size, spot=ref_spot)

    # pick the deepest requested ITM strike that has a quote; else the nearest one
    for strike in reversed(cand_strikes):
        c = build(strike)
        if c:
            return c
    for strike in cand_strikes:
        c = build(strike)
        if c:
            return c
    raise UpstoxError(f"No quotable ITM {side} contract for {equity_key}")


async def hedge_candidates(client: UpstoxClient, cfg: EODConfig, *, equity_key: str,
                           expiry: str, side: str, spot: float,
                           lot_size: int) -> list[Contract]:
    """Ordered list of hedge contracts to try, best-first: the ITM strike(s)
    (the intended hedge, nearest-to-money first for liquidity), then ATM, then
    OTM as a last-resort partial hedge. The engine buys the first one that
    fills. `side` is the option type to buy (CE below spot / PE above spot)."""
    chain = await client.option_chain(equity_key, expiry)
    if not chain:
        raise UpstoxError(f"Empty option chain for {equity_key} {expiry}")
    ref_spot = float(chain[0].get("underlying_spot_price") or spot or 0.0)
    strikes = sorted({float(r["strike_price"]) for r in chain})
    if not strikes:
        raise UpstoxError("No strikes in chain")
    atm = min(strikes, key=lambda s: abs(s - ref_spot))
    ai = strikes.index(atm)
    leg_key = "call_options" if side == "CE" else "put_options"
    by_strike = {float(r["strike_price"]): r for r in chain}

    def build(strike: float) -> Contract | None:
        leg = (by_strike.get(strike) or {}).get(leg_key) or {}
        md = leg.get("market_data") or {}
        gk = leg.get("option_greeks") or {}
        ikey = leg.get("instrument_key")
        ltp = float(md.get("ltp") or 0.0)
        if not ikey or ltp <= 0:
            return None
        delta = gk.get("delta")
        bid = float(md.get("bid_price") or 0.0)
        ask = float(md.get("ask_price") or 0.0)
        return Contract(instrument_key=ikey,
                        trading_symbol=leg.get("trading_symbol") or ikey,
                        side=side, strike=strike, expiry=expiry, ltp=ltp,
                        delta=float(delta) if delta is not None else None,
                        oi=float(md["oi"]) if md.get("oi") is not None else None,
                        spread_pct=(ask - bid) / ltp * 100 if (bid > 0 and ask > 0) else None,
                        lot_size=lot_size, spot=ref_spot)

    # ordered strikes: ITM nearest-first (most liquid ITM), then ATM, then OTM
    if side == "CE":   # ITM call = below spot; OTM = above
        itm = [s for s in reversed(strikes[:ai]) if s < ref_spot]
        otm = [s for s in strikes[ai + 1:] if s > ref_spot]
    else:              # ITM put = above spot; OTM = below
        itm = [s for s in strikes[ai + 1:] if s > ref_spot]
        otm = [s for s in reversed(strikes[:ai]) if s < ref_spot]
    ordered = itm + [atm] + otm

    out: list[Contract] = []
    seen = set()
    for s in ordered:
        if s in seen:
            continue
        seen.add(s)
        c = build(s)
        if c:
            out.append(c)
    if not out:
        raise UpstoxError(f"No quotable {side} hedge contract for {equity_key}")
    return out
