"""Async wrapper over the Upstox REST API (v2 + v3).

Only real Upstox endpoints are used — no synthetic or dummy market data
anywhere. All market data (candles, LTP, option chain) comes live from
Upstox; paper mode only simulates *execution*, always at real market prices.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

import httpx

from .config import TOKEN_FILE

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
        data = await self._get(f"{V3}/market-quote/ltp",
                               params={"instrument_key": ",".join(instrument_keys)})
        out: dict[str, float] = {}
        for item in data.values():
            token = item.get("instrument_token")
            if token is not None and item.get("last_price") is not None:
                out[token] = float(item["last_price"])
        return out

    # ---------------- options ----------------

    async def option_contracts(self, underlying_key: str) -> list[dict]:
        return await self._get(f"{V2}/option/contract", params={"instrument_key": underlying_key})

    async def option_chain(self, underlying_key: str, expiry_date: str) -> list[dict]:
        return await self._get(f"{V2}/option/chain",
                               params={"instrument_key": underlying_key, "expiry_date": expiry_date})

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

    async def cancel_order(self, order_id: str) -> None:
        resp = await self._http.delete(f"{V3}/order/cancel",
                                       headers=self._headers(), params={"order_id": order_id})
        self._unwrap(resp)

    async def close(self) -> None:
        await self._http.aclose()
