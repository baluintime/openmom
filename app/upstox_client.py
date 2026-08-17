"""Async wrapper over the Upstox REST API (v2 + v3).

Only real Upstox endpoints are used — no synthetic or dummy market data
anywhere. All market data (candles, LTP, option chain) comes live from
Upstox; paper mode only simulates *execution*, always at real market prices.
"""
from __future__ import annotations

import gzip
import io
import json
import time
import urllib.parse
from typing import Any

import httpx

from .config import INSTRUMENTS_URL, TOKEN_FILE

V2 = "https://api.upstox.com/v2"
V3 = "https://api.upstox.com/v3"


class UpstoxError(Exception):
    pass


class UpstoxClient:
    def __init__(self, api_key: str, api_secret: str, redirect_uri: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.redirect_uri = redirect_uri
        self.access_token: str | None = None
        self._http = httpx.AsyncClient(timeout=15.0)
        self._load_token()

    # ---------------- auth ----------------

    def _load_token(self) -> None:
        if TOKEN_FILE.exists():
            try:
                data = json.loads(TOKEN_FILE.read_text())
                self.access_token = data.get("access_token") or None
            except Exception:
                self.access_token = None

    def set_token(self, token: str) -> None:
        self.access_token = token.strip()
        TOKEN_FILE.write_text(json.dumps({"access_token": self.access_token, "saved_at": time.time()}))

    def clear_token(self) -> None:
        self.access_token = None
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()

    def login_url(self) -> str:
        params = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self.api_key,
            "redirect_uri": self.redirect_uri,
        })
        return f"{V2}/login/authorization/dialog?{params}"

    async def exchange_code(self, code: str) -> None:
        resp = await self._http.post(
            f"{V2}/login/authorization/token",
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "code": code,
                "client_id": self.api_key,
                "client_secret": self.api_secret,
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        data = resp.json()
        token = data.get("access_token")
        if resp.status_code != 200 or not token:
            raise UpstoxError(f"Token exchange failed: {data}")
        self.set_token(token)

    # ---------------- request helpers ----------------

    def _headers(self) -> dict[str, str]:
        if not self.access_token:
            raise UpstoxError("Not authenticated with Upstox — connect first")
        return {"Accept": "application/json", "Authorization": f"Bearer {self.access_token}"}

    async def _get(self, url: str, params: dict | None = None) -> Any:
        resp = await self._http.get(url, headers=self._headers(), params=params)
        return self._unwrap(resp)

    def _unwrap(self, resp: httpx.Response) -> Any:
        try:
            body = resp.json()
        except Exception:
            raise UpstoxError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 401:
            # token expired (Upstox tokens die at ~3:30 AM IST daily)
            self.access_token = None
            raise UpstoxError("Upstox token expired or invalid — reconnect")
        if resp.status_code >= 400 or body.get("status") == "error":
            errs = body.get("errors") or [body]
            msg = "; ".join(str(e.get("message", e)) for e in errs if isinstance(e, dict)) or str(body)
            raise UpstoxError(msg)
        return body.get("data", body)

    # ---------------- account ----------------

    async def profile(self) -> dict:
        return await self._get(f"{V2}/user/profile")

    async def funds(self) -> dict:
        return await self._get(f"{V2}/user/get-funds-and-margin", params={"segment": "SEC"})

    # ---------------- market data ----------------

    async def intraday_candles(self, instrument_key: str, interval_min: int) -> list[list]:
        """Today's candles, ascending by time. [[ts, o, h, l, c, vol, oi], ...]"""
        key = urllib.parse.quote(instrument_key, safe="")
        data = await self._get(f"{V3}/historical-candle/intraday/{key}/minutes/{interval_min}")
        candles = data.get("candles", [])
        return sorted(candles, key=lambda c: c[0])

    async def historical_candles(self, instrument_key: str, interval_min: int,
                                 from_date: str, to_date: str) -> list[list]:
        """Past-session candles (YYYY-MM-DD dates), ascending by time."""
        key = urllib.parse.quote(instrument_key, safe="")
        data = await self._get(
            f"{V3}/historical-candle/{key}/minutes/{interval_min}/{to_date}/{from_date}")
        candles = data.get("candles", [])
        return sorted(candles, key=lambda c: c[0])

    async def ltp(self, instrument_keys: list[str]) -> dict[str, float]:
        """Last traded prices keyed by instrument_key."""
        out: dict[str, float] = {}
        for batch in _chunks(instrument_keys, 400):
            data = await self._get(f"{V3}/market-quote/ltp",
                                   params={"instrument_key": ",".join(batch)})
            for item in data.values():
                token = item.get("instrument_token")
                if token is not None and item.get("last_price") is not None:
                    out[token] = float(item["last_price"])
        return out

    async def full_quote(self, instrument_keys: list[str]) -> dict[str, dict]:
        """Full quote per instrument: {key: {ltp, open, high, low, close, oi}}.
        `high`/`low` are the intraday day extremes used by the strength screen."""
        out: dict[str, dict] = {}
        for batch in _chunks(instrument_keys, 200):
            data = await self._get(f"{V2}/market-quote/quotes",
                                   params={"instrument_key": ",".join(batch)})
            for item in data.values():
                token = item.get("instrument_token")
                ohlc = item.get("ohlc") or {}
                if token is None:
                    continue
                out[token] = {
                    "ltp": _f(item.get("last_price")),
                    "open": _f(ohlc.get("open")), "high": _f(ohlc.get("high")),
                    "low": _f(ohlc.get("low")), "close": _f(ohlc.get("close")),
                    "oi": _f(item.get("oi")), "volume": _f(item.get("volume")),
                }
        return out

    # ---------------- F&O universe (instruments master) ----------------

    async def instruments_nse(self) -> list[dict]:
        """Download + parse the public NSE instruments master (gzipped JSON).
        No auth required. Returns the raw instrument records."""
        resp = await self._http.get(INSTRUMENTS_URL, timeout=60.0)
        if resp.status_code != 200:
            raise UpstoxError(f"Instruments master HTTP {resp.status_code}")
        raw = resp.content
        try:
            raw = gzip.decompress(raw)
        except (OSError, gzip.BadGzipFile):
            pass  # already-decompressed (some CDNs auto-gunzip)
        return json.loads(raw.decode("utf-8"))

    # ---------------- options ----------------

    async def option_contracts(self, underlying_key: str) -> list[dict]:
        return await self._get(f"{V2}/option/contract", params={"instrument_key": underlying_key})

    async def option_chain(self, underlying_key: str, expiry_date: str) -> list[dict]:
        return await self._get(f"{V2}/option/chain",
                               params={"instrument_key": underlying_key, "expiry_date": expiry_date})

    # ---------------- expired instruments (for backtesting) ----------------

    async def expired_expiries(self, underlying_key: str) -> list[str]:
        data = await self._get(f"{V2}/expired-instruments/expiries",
                               params={"instrument_key": underlying_key})
        if isinstance(data, dict):
            data = data.get("expiries", [])
        return [str(e) for e in data]

    async def expired_option_contracts(self, underlying_key: str, expiry_date: str) -> list[dict]:
        return await self._get(f"{V2}/expired-instruments/option/contract",
                               params={"instrument_key": underlying_key,
                                       "expiry_date": expiry_date})

    async def expired_historical_candles(self, expired_instrument_key: str, interval: str,
                                         from_date: str, to_date: str) -> list[list]:
        key = urllib.parse.quote(expired_instrument_key, safe="")
        data = await self._get(
            f"{V2}/expired-instruments/historical-candle/{key}/{interval}/{to_date}/{from_date}")
        return sorted(data.get("candles", []), key=lambda c: c[0])

    # ---------------- orders ----------------

    async def place_order(self, *, instrument_token: str, quantity: int,
                          transaction_type: str, order_type: str,
                          price: float = 0.0, product: str = "I",
                          tag: str = "nifty-scalper") -> str:
        payload = {
            "quantity": quantity,
            "product": product,
            "validity": "DAY",
            "price": round(price, 2) if order_type == "LIMIT" else 0,
            "tag": tag,
            "instrument_token": instrument_token,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
            "slice": True,
        }
        resp = await self._http.post(f"{V3}/order/place", headers={
            **self._headers(), "Content-Type": "application/json"}, json=payload)
        data = self._unwrap(resp)
        order_ids = data.get("order_ids") or ([data["order_id"]] if data.get("order_id") else [])
        if not order_ids:
            raise UpstoxError(f"Order placement returned no order id: {data}")
        return order_ids[0]

    async def order_details(self, order_id: str) -> dict:
        return await self._get(f"{V2}/order/details", params={"order_id": order_id})

    async def positions(self) -> dict[str, int]:
        """Net quantity per instrument_key from the day's positions (short = negative)."""
        data = await self._get(f"{V2}/portfolio/short-term-positions")
        out: dict[str, int] = {}
        for p in data or []:
            k = p.get("instrument_token")
            q = p.get("quantity")
            if k is not None and q is not None:
                out[k] = int(q)
        return out

    async def cancel_order(self, order_id: str) -> None:
        resp = await self._http.delete(f"{V3}/order/cancel",
                                       headers=self._headers(), params={"order_id": order_id})
        self._unwrap(resp)

    # ---------------- GTT (exchange-side exits) ----------------

    async def place_gtt(self, *, instrument_token: str, quantity: int,
                        transaction_type: str, target_price: float,
                        stop_price: float, product: str = "D") -> str:
        """Create an OCO GTT with a TARGET rule and a STOPLOSS rule on one
        instrument (Upstox GTT v3). Returns the gtt order id."""
        payload = {
            "type": "MULTIPLE", "quantity": quantity, "product": product,
            "instrument_token": instrument_token,
            "transaction_type": transaction_type,
            "rules": [
                {"strategy": "TARGET", "trigger_type": "IMMEDIATE",
                 "trigger_price": round(target_price, 2)},
                {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE",
                 "trigger_price": round(stop_price, 2)},
            ],
        }
        resp = await self._http.post(f"{V3}/order/gtt/place", headers={
            **self._headers(), "Content-Type": "application/json"}, json=payload)
        data = self._unwrap(resp)
        gid = (data.get("gtt_order_ids") or [data.get("gtt_order_id")])
        gid = [g for g in gid if g]
        if not gid:
            raise UpstoxError(f"GTT placement returned no id: {data}")
        return gid[0]

    async def gtt_details(self, gtt_order_id: str) -> dict:
        data = await self._get(f"{V3}/order/gtt", params={"gtt_order_id": gtt_order_id})
        if isinstance(data, list):
            return data[0] if data else {}
        return data

    async def cancel_gtt(self, gtt_order_id: str) -> None:
        resp = await self._http.request(
            "DELETE", f"{V3}/order/gtt/cancel",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"gtt_order_id": gtt_order_id})
        self._unwrap(resp)

    async def close(self) -> None:
        await self._http.aclose()


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
