"""Execution layer: identical strategy path for paper and live trading.

PaperBroker simulates *fills only* — every price it uses is the real Upstox
LTP at that moment (no synthetic data). LiveBroker places real orders via
the Upstox order API. Entries are LIMIT orders per spec; stop-loss and
square-off exits go out as MARKET to guarantee the position is flattened,
while target exits go out as LIMIT at the target price.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

from .upstox_client import UpstoxClient

_paper_seq = itertools.count(1)

# Order status vocabulary (superset of Upstox's)
OPEN = "open"
FILLED = "complete"
CANCELLED = "cancelled"
REJECTED = "rejected"


@dataclass
class BrokerOrder:
    order_id: str
    instrument_key: str
    side: str                # BUY / SELL
    order_type: str          # LIMIT / MARKET
    qty: int
    limit_price: float
    status: str = OPEN
    avg_price: float = 0.0
    filled_qty: int = 0
    placed_at: float = field(default_factory=time.time)


class PaperBroker:
    """Simulated execution against live market prices."""

    name = "paper"

    def __init__(self):
        self.orders: dict[str, BrokerOrder] = {}

    async def place(self, *, instrument_key: str, side: str, order_type: str,
                    qty: int, price: float = 0.0) -> BrokerOrder:
        order = BrokerOrder(
            order_id=f"PAPER-{next(_paper_seq)}",
            instrument_key=instrument_key, side=side,
            order_type=order_type, qty=qty, limit_price=price,
        )
        self.orders[order.order_id] = order
        return order

    async def poll(self, order: BrokerOrder, ltp: float | None) -> BrokerOrder:
        """Fill rules against the real LTP: market fills at LTP; a buy limit
        fills when LTP <= limit; a sell limit fills when LTP >= limit."""
        if order.status != OPEN or ltp is None or ltp <= 0:
            return order
        fill = None
        if order.order_type == "MARKET":
            fill = ltp
        elif order.side == "BUY" and ltp <= order.limit_price:
            fill = min(ltp, order.limit_price)
        elif order.side == "SELL" and ltp >= order.limit_price:
            fill = max(ltp, order.limit_price)
        if fill is not None:
            order.status = FILLED
            order.avg_price = fill
            order.filled_qty = order.qty
        return order

    async def cancel(self, order: BrokerOrder) -> None:
        if order.status == OPEN:
            order.status = CANCELLED


class LiveBroker:
    """Real order placement through the Upstox API."""

    name = "live"

    def __init__(self, client: UpstoxClient):
        self.client = client

    async def place(self, *, instrument_key: str, side: str, order_type: str,
                    qty: int, price: float = 0.0) -> BrokerOrder:
        order_id = await self.client.place_order(
            instrument_token=instrument_key, quantity=qty,
            transaction_type=side, order_type=order_type, price=price,
        )
        return BrokerOrder(order_id=order_id, instrument_key=instrument_key,
                           side=side, order_type=order_type, qty=qty, limit_price=price)

    async def poll(self, order: BrokerOrder, ltp: float | None = None) -> BrokerOrder:
        d = await self.client.order_details(order.order_id)
        status = (d.get("status") or "").lower()
        order.filled_qty = int(d.get("filled_quantity") or 0)
        order.avg_price = float(d.get("average_price") or 0.0)
        if status == "complete":
            order.status = FILLED
        elif status in ("cancelled", "cancelled after market order"):
            order.status = CANCELLED
        elif status == "rejected":
            order.status = REJECTED
        return order

    async def cancel(self, order: BrokerOrder) -> None:
        await self.client.cancel_order(order.order_id)
        order.status = CANCELLED
