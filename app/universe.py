"""NSE F&O stock universe + the EOD 'Strength' screener.

The universe (which stocks have options, their equity spot key, lot size and
nearest expiry) is parsed once per day from the Upstox instruments master and
cached. The screener ranks stocks by how close spot sits to the day's high/low.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import IST, UNIVERSE_FILE
from .upstox_client import UpstoxClient, UpstoxError

TZ = ZoneInfo(IST)

# Index underlyings are not "stocks" — exclude from the equity screen.
_INDEX_HINTS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX",
                "NIFTYNXT50")


@dataclass
class Stock:
    symbol: str
    equity_key: str      # NSE_EQ|... spot key for quotes + option chain
    lot_size: int
    expiry: str          # nearest expiry, YYYY-MM-DD


def _expiry_to_date(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):   # epoch millis
        try:
            return datetime.fromtimestamp(v / 1000, tz=timezone.utc).astimezone(TZ).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v)
    return s[:10] if len(s) >= 10 else s


def _field(rec: dict, *names):
    for n in names:
        if rec.get(n) not in (None, ""):
            return rec[n]
    return None


def build_universe(records: list[dict]) -> list[Stock]:
    """Group option instruments by underlying stock; attach the equity spot key,
    lot size and nearest (>= today) expiry."""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    # equity spot keys by trading symbol
    eq_by_symbol: dict[str, str] = {}
    for r in records:
        seg = _field(r, "segment")
        itype = _field(r, "instrument_type")
        if seg == "NSE_EQ" and itype in ("EQ", "EQUITY", None):
            sym = _field(r, "trading_symbol", "name")
            key = _field(r, "instrument_key")
            if sym and key:
                eq_by_symbol.setdefault(str(sym).upper(), key)

    agg: dict[str, dict] = {}
    for r in records:
        if _field(r, "segment") != "NSE_FO":
            continue
        if _field(r, "instrument_type") not in ("CE", "PE"):
            continue
        sym = _field(r, "underlying_symbol", "asset_symbol", "name")
        if not sym:
            continue
        sym = str(sym).upper()
        if any(h in sym for h in _INDEX_HINTS):
            continue
        exp = _expiry_to_date(_field(r, "expiry"))
        if not exp or exp < today:
            continue
        ukey = _field(r, "underlying_key")
        if not (ukey and str(ukey).startswith("NSE_EQ")):
            ukey = eq_by_symbol.get(sym)
        if not ukey:
            continue
        lot = _field(r, "lot_size") or 0
        a = agg.setdefault(sym, {"equity_key": ukey, "lot": int(lot or 0), "expiries": set()})
        a["expiries"].add(exp)
        if int(lot or 0) > 0:
            a["lot"] = int(lot)

    out = []
    for sym, a in agg.items():
        if not a["expiries"] or a["lot"] < 1:
            continue
        out.append(Stock(symbol=sym, equity_key=a["equity_key"],
                         lot_size=a["lot"], expiry=min(a["expiries"])))
    out.sort(key=lambda s: s.symbol)
    return out


def save_universe(stocks: list[Stock]) -> None:
    UNIVERSE_FILE.write_text(json.dumps(
        {"day": datetime.now(TZ).strftime("%Y-%m-%d"),
         "stocks": [asdict(s) for s in stocks]}, indent=0))


def load_cached_universe() -> list[Stock] | None:
    if not UNIVERSE_FILE.exists():
        return None
    try:
        data = json.loads(UNIVERSE_FILE.read_text())
    except Exception:
        return None
    if data.get("day") != datetime.now(TZ).strftime("%Y-%m-%d"):
        return None
    return [Stock(**s) for s in data.get("stocks", [])]


async def get_universe(client: UpstoxClient, force: bool = False) -> list[Stock]:
    if not force:
        cached = load_cached_universe()
        if cached:
            return cached
    records = await client.instruments_nse()
    stocks = build_universe(records)
    if not stocks:
        raise UpstoxError("No F&O stocks parsed from the instruments master")
    save_universe(stocks)
    return stocks


def screen(stocks: list[Stock], quotes: dict[str, dict]) -> list[dict]:
    """Rank stocks by proximity of spot to the day extreme.

    Bullish strength = (high - spot)/spot*100  (small => near the day HIGH)
    Bearish strength = (spot - low)/spot*100   (small => near the day LOW)
    Each stock takes its smaller strength; the corresponding bias decides the
    option to sell (near high => bullish => sell PUT; near low => sell CALL).
    Ranked ascending by strength (closest to its extreme first).
    """
    ranked = []
    for s in stocks:
        q = quotes.get(s.equity_key)
        if not q:
            continue
        spot, hi, lo = q.get("ltp"), q.get("high"), q.get("low")
        if not spot or not hi or not lo or spot <= 0:
            continue
        bull = (hi - spot) / spot * 100.0
        bear = (spot - lo) / spot * 100.0
        if bull <= bear:
            bias, side, strength = "bullish", "PE", bull
        else:
            bias, side, strength = "bearish", "CE", bear
        ranked.append({
            "symbol": s.symbol, "equity_key": s.equity_key, "lot_size": s.lot_size,
            "expiry": s.expiry, "spot": round(spot, 2), "high": round(hi, 2),
            "low": round(lo, 2), "bias": bias, "side": side,
            "strength": round(strength, 3)})
    ranked.sort(key=lambda r: r["strength"])
    return ranked
