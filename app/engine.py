"""NSE EOD Momentum engine.

Daily pipeline (IST): scan all F&O stocks at 3:15, re-scan the shortlist at
3:24:50, then at 3:25 SELL an OTM option on each of the top-N stocks with a
+TP% / -SL% exit. Positions are carried overnight and closed only when the exit
triggers (or on manual exit / expiry).

Paper mode simulates fills at the real option LTP and monitors TP/SL locally.
Live mode places real SELL orders (product D, carry-forward) and an exchange
GTT for the exit.
"""
from __future__ import annotations

import asyncio
import itertools
import json
from collections import deque
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import (DATA_DIR, IST, POSITIONS_FILE, EODConfig, parse_hms)
from .options import select_otm
from .universe import get_universe, screen
from .upstox_client import UpstoxClient, UpstoxError

TZ = ZoneInfo(IST)
TRADES_FILE = DATA_DIR / "trades.jsonl"
_seq = itertools.count(1)


class EODEngine:
    def __init__(self, client: UpstoxClient, cfg: EODConfig):
        self.client = client
        self.cfg = cfg
        self.running = False
        self.events: deque[dict] = deque(maxlen=400)
        self.positions: list[dict] = self._load_positions()
        self.trades: list[dict] = self._load_today_trades()
        self.scan_result: list[dict] = []       # shortlist from the 3:15 scan
        self.candidates: list[dict] = []         # definitive top-N from 3:24:50
        self.universe_count = 0
        self.last_scan_at: str | None = None
        self.phase = {"scan": None, "refresh": None, "dispatch": None}  # date guards
        self._last_error = ""
        self._task: asyncio.Task | None = None

    # ---------- lifecycle ----------
    def start(self):
        if not self.client.access_token:
            raise UpstoxError("Connect to Upstox before starting")
        self.running = True
        self.log("engine", f"Strategy armed ({self.cfg.mode}) — scan {self.cfg.scan_time}, "
                           f"dispatch {self.cfg.dispatch_time}")

    def stop(self):
        self.running = False
        self.log("engine", "Strategy disarmed (open positions still tracked)")

    def ensure_loop(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    @staticmethod
    def market_hours(now: datetime) -> bool:
        from datetime import time as _t
        return now.weekday() < 5 and _t(9, 15) <= now.time() < _t(15, 30)

    # ---------- logging / persistence ----------
    def log(self, kind: str, msg: str):
        now = datetime.now(TZ)
        self.events.appendleft({"ts": now.strftime("%H:%M:%S"), "kind": kind, "msg": msg})
        try:
            with (DATA_DIR / f"events-{now:%Y-%m-%d}.log").open("a", encoding="utf-8") as f:
                f.write(f"{now:%H:%M:%S} [{kind}] {msg}\n")
            with (DATA_DIR / f"events-{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now.isoformat(), "kind": kind, "msg": msg}) + "\n")
        except OSError:
            pass

    def _load_positions(self) -> list[dict]:
        """Load carried positions. Tolerates a stale/foreign file (e.g. an old
        version's dict) by keeping only well-formed position records."""
        if not POSITIONS_FILE.exists():
            return []
        try:
            data = json.loads(POSITIONS_FILE.read_text())
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [p for p in data if isinstance(p, dict) and "status" in p and "option_key" in p]

    def _save_positions(self):
        try:
            POSITIONS_FILE.write_text(json.dumps(self.positions, indent=0))
        except OSError:
            pass

    def _load_today_trades(self) -> list[dict]:
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        out = []
        if TRADES_FILE.exists():
            for line in TRADES_FILE.read_text().splitlines():
                try:
                    t = json.loads(line)
                    if isinstance(t, dict) and t.get("exit_day") == today:
                        out.append(t)
                except Exception:
                    continue
        return out

    def _record_trade(self, trade: dict):
        self.trades.append(trade)
        with TRADES_FILE.open("a") as f:
            f.write(json.dumps(trade) + "\n")

    # ---------- main loop ----------
    async def _run(self):
        while True:
            try:
                await self._tick()
            except UpstoxError as e:
                self._note_error(str(e))
            except Exception as e:
                self._note_error(f"{type(e).__name__}: {e}")
            in_mkt = self.market_hours(datetime.now(TZ))
            await asyncio.sleep(3 if (in_mkt and self.client.access_token) else 20)

    def _note_error(self, msg):
        if msg != self._last_error:
            self._last_error = msg
            self.log("error", msg)

    async def _tick(self):
        now = datetime.now(TZ)
        if not self.client.access_token:
            return
        await self._schedule(now)
        await self._monitor(now)

    async def _schedule(self, now: datetime):
        if not (self.running and self.market_hours(now)):
            return
        day = now.strftime("%Y-%m-%d")
        t = now.time()
        if self.phase["scan"] != day and t >= parse_hms(self.cfg.scan_time):
            self.phase["scan"] = day
            await self._scan()
        if (self.phase["refresh"] != day and self.scan_result
                and t >= parse_hms(self.cfg.refresh_time)):
            self.phase["refresh"] = day
            await self._refresh()
        if (self.phase["dispatch"] != day and self.candidates
                and t >= parse_hms(self.cfg.dispatch_time)):
            self.phase["dispatch"] = day
            await self._dispatch(now)

    # ---------- pipeline ----------
    async def scan_now(self) -> list[dict]:
        """Manual scan+refresh (does not dispatch) — for inspection."""
        await self._scan()
        await self._refresh()
        return self.candidates

    async def _scan(self):
        universe = await get_universe(self.client)
        self.universe_count = len(universe)
        if self.cfg.universe_limit > 0:
            universe = universe[:self.cfg.universe_limit]
        quotes = await self.client.full_quote([s.equity_key for s in universe])
        ranked = screen(universe, quotes)
        self.scan_result = ranked[:self.cfg.shortlist_size]
        self.last_scan_at = datetime.now(TZ).strftime("%H:%M:%S")
        self.log("scan", f"Scanned {len(universe)} F&O stocks — shortlisted "
                         f"{len(self.scan_result)} (top: "
                         + ", ".join(f"{r['symbol']} {r['strength']:.2f}%"
                                     for r in self.scan_result[:3]) + ")")

    async def _refresh(self):
        # rebuild Stock records from the shortlist and re-screen on fresh quotes
        from .universe import Stock
        shortlist = [Stock(symbol=r["symbol"], equity_key=r["equity_key"],
                           lot_size=r["lot_size"], expiry=r["expiry"])
                     for r in self.scan_result]
        quotes = await self.client.full_quote([s.equity_key for s in shortlist])
        ranked = screen(shortlist, quotes)
        self.candidates = ranked[:self.cfg.top_n]
        self.log("refresh", "Re-scan complete — definitive picks: "
                 + ", ".join(f"{c['symbol']} ({c['bias']}, sell {c['side']}, "
                             f"{c['strength']:.2f}%)" for c in self.candidates))

    async def _dispatch(self, now: datetime):
        held = {p["symbol"] for p in self.positions if p["status"] == "open"}
        for c in self.candidates[:self.cfg.top_n]:
            if c["symbol"] in held:
                self.log("dispatch", f"{c['symbol']}: already holding a position — skipped")
                continue
            try:
                ct = await select_otm(self.client, self.cfg, equity_key=c["equity_key"],
                                      expiry=c["expiry"], side=c["side"],
                                      spot=c["spot"], lot_size=c["lot_size"])
            except UpstoxError as e:
                self.log("error", f"{c['symbol']}: option selection failed — {e}")
                continue
            await self._sell(now, c, ct)

    async def _sell(self, now: datetime, cand: dict, ct):
        qty = self.cfg.lots * ct.lot_size
        sell_price = ct.ltp
        # readable contract label if the chain didn't provide a trading symbol
        symbol = ct.trading_symbol
        if not symbol or "|" in symbol:
            symbol = f"{cand['symbol']} {ct.strike:g} {ct.side} {ct.expiry}"
        ct.trading_symbol = symbol
        gtt_id = None
        if self.cfg.mode == "live":
            try:
                await self.client.place_order(
                    instrument_token=ct.instrument_key, quantity=qty,
                    transaction_type="SELL", order_type="MARKET",
                    product="D", tag="eod-momentum")
            except UpstoxError as e:
                self.log("error", f"{cand['symbol']}: live SELL failed — {e}")
                return
        target = round(sell_price * (1 - self.cfg.tp_pct / 100), 2)
        stop = round(sell_price * (1 + self.cfg.sl_pct / 100), 2)
        pos = {
            "id": f"P{next(_seq)}", "symbol": cand["symbol"], "bias": cand["bias"],
            "side": ct.side, "option_key": ct.instrument_key,
            "option_symbol": ct.trading_symbol, "strike": ct.strike, "expiry": ct.expiry,
            "lot_size": ct.lot_size, "qty": qty, "mode": self.cfg.mode,
            "sell_price": round(sell_price, 2), "target": target, "stop": stop,
            "spot_at_entry": cand["spot"], "strength_at_entry": cand["strength"],
            "entry_time": now.strftime("%H:%M:%S"), "entry_day": now.strftime("%Y-%m-%d"),
            "ltp": round(sell_price, 2), "status": "open", "gtt_id": gtt_id,
            "oi": ct.oi, "delta": ct.delta, "spread_pct": ct.spread_pct,
        }
        if self.cfg.mode == "live" and self.cfg.use_gtt:
            try:
                pos["gtt_id"] = await self.client.place_gtt(
                    instrument_token=ct.instrument_key, quantity=qty,
                    transaction_type="BUY", target_price=target, stop_price=stop,
                    product="D")
                self.log("trade", f"{cand['symbol']}: GTT exit attached #{pos['gtt_id']}")
            except UpstoxError as e:
                self.log("error", f"{cand['symbol']}: GTT attach failed — {e}. "
                                  "The app will place the exit order itself while it is "
                                  "running (keep it running, or fix/disable GTT).")
        if self.cfg.mode == "paper":
            pos["exit_mode"] = "paper (monitored locally)"
        elif pos["gtt_id"]:
            pos["exit_mode"] = f"GTT #{pos['gtt_id']}"
        else:
            pos["exit_mode"] = "app-managed"
        self.positions.append(pos)
        self._save_positions()
        self.log("trade", f"SOLD {ct.trading_symbol} × {qty} @ ₹{sell_price:.2f} "
                          f"[{self.cfg.mode}] — {cand['symbol']} {cand['bias']} · "
                          f"target ₹{target:.2f} / stop ₹{stop:.2f} · exit: {pos['exit_mode']}")

    async def _monitor(self, now: datetime):
        opens = [p for p in self.positions if p["status"] == "open"]
        if not opens:
            return
        today = now.strftime("%Y-%m-%d")
        # expired OTM options that were carried: seller keeps the full premium
        for p in list(opens):
            if p["expiry"] < today:
                self._close(p, exit_price=0.0, reason="expired", now=now)
        opens = [p for p in self.positions if p["status"] == "open"]
        if not opens:
            return
        keys = list({p["option_key"] for p in opens})
        try:
            ltp_map = await self.client.ltp(keys)
        except UpstoxError:
            return
        for p in opens:
            lp = ltp_map.get(p["option_key"])
            if lp is None:
                continue
            p["ltp"] = round(lp, 2)
            if not self.market_hours(now):
                continue   # exits only trigger during market hours
            # short option: profit when price falls to target, loss when it rises to stop
            if lp <= p["target"]:
                await self._exit_now(p, lp, "target", now)
            elif lp >= p["stop"]:
                await self._exit_now(p, lp, "stoploss", now)
        self._save_positions()

    async def _exit_now(self, p: dict, price: float, reason: str, now: datetime):
        """Close a position. Paper simulates the buy-back at `price`. Live places
        a real market BUY to flatten — but first verifies the short is still open
        at the broker (a GTT may have already fired while the app was off), so it
        never fires a stray order."""
        if p["mode"] == "live":
            try:
                held = await self.client.positions()
                netq = held.get(p["option_key"])
            except UpstoxError:
                netq = None
            if netq == 0:
                self.log("trade", f"{p['symbol']}: broker already flat "
                                  "(exchange GTT closed it) — recording exit")
                self._close(p, exit_price=price, reason=reason, now=now)
                return
            try:
                if p.get("gtt_id"):
                    try:
                        await self.client.cancel_gtt(p["gtt_id"])
                    except UpstoxError:
                        pass
                await self.client.place_order(
                    instrument_token=p["option_key"], quantity=p["qty"],
                    transaction_type="BUY", order_type="MARKET", product="D",
                    tag="eod-exit")
            except UpstoxError as e:
                self.log("error", f"Live exit order failed for {p['symbol']}: {e} — "
                                  "will retry next tick")
                return
        self._close(p, exit_price=price, reason=reason, now=now)

    def _close(self, p: dict, exit_price: float, reason: str, now: datetime):
        p["status"] = "closed"
        p["exit_price"] = round(exit_price, 2)
        p["exit_reason"] = reason
        p["exit_time"] = now.strftime("%H:%M:%S")
        pnl = (p["sell_price"] - exit_price) * p["qty"]      # short: credit - buyback
        charges = self.cfg.charges_per_trade
        trade = {
            "entry_day": p["entry_day"], "exit_day": now.strftime("%Y-%m-%d"),
            "symbol": p["symbol"], "bias": p["bias"], "side": p["side"],
            "option_symbol": p["option_symbol"], "strike": p["strike"],
            "expiry": p["expiry"], "qty": p["qty"], "mode": p["mode"],
            "sell_price": p["sell_price"], "exit_price": round(exit_price, 2),
            "spot_at_entry": p.get("spot_at_entry"),
            "entry_time": p["entry_time"], "exit_time": p["exit_time"],
            "reason": reason, "points": round(p["sell_price"] - exit_price, 2),
            "gross_rs": round(pnl, 2), "charges_rs": round(charges, 2),
            "net_rs": round(pnl - charges, 2),
        }
        self._record_trade(trade)
        self._save_positions()
        self.log("trade", f"CLOSED {p['option_symbol']} @ ₹{exit_price:.2f} ({reason}) "
                          f"— net ₹{trade['net_rs']:+,.2f}")

    async def manual_exit(self, pos_id: str) -> bool:
        now = datetime.now(TZ)
        p = next((x for x in self.positions if x["id"] == pos_id and x["status"] == "open"), None)
        if not p:
            return False
        price = p.get("ltp") or p["sell_price"]
        try:
            m = await self.client.ltp([p["option_key"]])
            price = m.get(p["option_key"], price)
        except UpstoxError:
            pass
        if self.cfg.mode == "live":
            try:
                if p.get("gtt_id"):
                    await self.client.cancel_gtt(p["gtt_id"])
                await self.client.place_order(
                    instrument_token=p["option_key"], quantity=p["qty"],
                    transaction_type="BUY", order_type="MARKET", product="D",
                    tag="eod-manual-exit")
            except UpstoxError as e:
                self.log("error", f"Manual exit failed for {p['symbol']}: {e}")
                return False
        self._close(p, exit_price=price, reason="manual", now=now)
        return True

    # ---------- status ----------
    def status(self) -> dict:
        now = datetime.now(TZ)
        opens = [p for p in self.positions if p.get("status") == "open"]
        for p in opens:
            sell = p.get("sell_price") or 0.0
            up = (sell - (p.get("ltp") or sell)) * (p.get("qty") or 0)
            p["unreal_rs"] = round(up, 2)
        return {
            "now_ist": now.strftime("%Y-%m-%d %H:%M:%S"),
            "market_open": self.market_hours(now),
            "running": self.running, "config": asdict(self.cfg),
            "universe_count": self.universe_count, "last_scan_at": self.last_scan_at,
            "phase": self.phase, "scan_result": self.scan_result,
            "candidates": self.candidates,
            "positions": opens,
            "trades": list(reversed(self.trades)),
            "events": list(self.events)[:150],
        }
