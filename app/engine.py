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
from .options import hedge_candidates, select_otm
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
        self._ranked: list[dict] = []            # full ranked pool (dispatch fallback)
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
        self._ranked = screen(shortlist, quotes)     # full ranked pool for fallback
        self.candidates = self._ranked[:self.cfg.top_n]
        self.log("refresh", "Re-scan complete — definitive picks: "
                 + ", ".join(f"{c['symbol']} ({c['bias']}, sell {c['side']}, "
                             f"{c['strength']:.2f}%)" for c in self.candidates))

    async def _dispatch(self, now: datetime):
        held = {p["symbol"] for p in self.positions if p["status"] == "open"}
        opened = 0
        # walk the ranked pool; if a top pick can't be traded (illiquid option,
        # un-hedgeable, selection error), fall through to the next-ranked stock
        for c in (getattr(self, "_ranked", None) or self.candidates):
            if opened >= self.cfg.top_n:
                break
            if c["symbol"] in held:
                continue
            try:
                ct = await select_otm(self.client, self.cfg, equity_key=c["equity_key"],
                                      expiry=c["expiry"], side=c["side"],
                                      spot=c["spot"], lot_size=c["lot_size"])
            except UpstoxError as e:
                self.log("dispatch", f"{c['symbol']}: skipped — option selection failed ({e})")
                continue
            if await self._sell(now, c, ct):
                opened += 1
        if opened < self.cfg.top_n:
            self.log("dispatch", f"Opened {opened}/{self.cfg.top_n} positions "
                                 "(remaining picks were un-tradeable/un-hedgeable).")

    def _tick(self, price: float) -> float:
        """Round to the exchange tick (₹0.05) — required for GTT trigger prices."""
        t = self.cfg.tick_size or 0.05
        return round(round(price / t) * t, 2)

    def _label(self, cand_symbol: str, ct) -> str:
        sym = ct.trading_symbol
        if not sym or "|" in sym:
            sym = f"{cand_symbol} {ct.strike:g} {ct.side} {ct.expiry}"
        return sym

    async def _sell(self, now: datetime, cand: dict, ct):
        qty = self.cfg.lots * ct.lot_size
        sell_price = ct.ltp
        ct.trading_symbol = self._label(cand["symbol"], ct)

        # optional hedge: BUY an option of the opposite type. Prefer the ITM
        # strike; if that (or any preferred strike) can't be traded, fall
        # through progressively liquid strikes. Only go naked if none fills.
        hedge = None
        opts: list = []
        if self.cfg.hedge_itm:
            buy_side = "CE" if ct.side == "PE" else "PE"
            try:
                opts = await hedge_candidates(self.client, self.cfg,
                                              equity_key=cand["equity_key"],
                                              expiry=cand["expiry"], side=buy_side,
                                              spot=cand["spot"], lot_size=cand["lot_size"])
            except UpstoxError as e:
                opts = []
                if self.cfg.require_hedge:
                    self.log("dispatch", f"{cand['symbol']}: skipped — no hedge "
                                         f"available ({e}); require-hedge is on.")
                    return False
                self.log("error", f"{cand['symbol']}: no hedge available ({e}) — "
                                  "selling un-hedged (naked).")

        if self.cfg.mode == "live":
            # 1) buy a hedge FIRST — try candidates until one fills
            if opts:
                last_err = None
                for i, hc in enumerate(opts):
                    try:
                        await self.client.place_order(
                            instrument_token=hc.instrument_key,
                            quantity=self.cfg.lots * hc.lot_size,
                            transaction_type="BUY", order_type="MARKET",
                            product="D", tag="eod-hedge")
                        hedge = hc
                        moneyness = "ITM" if i == 0 else "fallback"
                        self.log("trade", f"{cand['symbol']}: hedge BUY filled "
                                          f"{self._label(cand['symbol'], hc)} ({moneyness})")
                        break
                    except UpstoxError as e:
                        last_err = e
                        continue
                if hedge is None:
                    if self.cfg.require_hedge:
                        self.log("dispatch", f"{cand['symbol']}: skipped — no hedge "
                                             f"strike filled ({last_err}); require-hedge is on.")
                        return False
                    self.log("error", f"{cand['symbol']}: no hedge strike filled "
                                      f"({last_err}) — selling un-hedged (naked).")
            # 2) sell the short leg (with or without a hedge)
            try:
                await self.client.place_order(
                    instrument_token=ct.instrument_key, quantity=qty,
                    transaction_type="SELL", order_type="MARKET",
                    product="D", tag="eod-momentum")
            except UpstoxError as e:
                self.log("error", f"{cand['symbol']}: live SELL failed — {e}")
                if hedge:   # roll back the hedge we just bought
                    try:
                        await self.client.place_order(
                            instrument_token=hedge.instrument_key,
                            quantity=self.cfg.lots * hedge.lot_size,
                            transaction_type="SELL", order_type="MARKET",
                            product="D", tag="eod-hedge-rollback")
                        self.log("trade", f"{cand['symbol']}: rolled back the hedge "
                                          "after the short SELL failed")
                    except UpstoxError as e2:
                        self.log("error", f"{cand['symbol']}: hedge rollback failed — {e2}. "
                                          f"You hold a long {hedge.side}; close it manually.")
                return False
        elif opts:
            hedge = opts[0]   # paper: use the best (ITM) candidate
        if hedge:
            hedge.trading_symbol = self._label(cand["symbol"], hedge)
        target = self._tick(sell_price * (1 - self.cfg.tp_pct / 100))
        stop = self._tick(sell_price * (1 + self.cfg.sl_pct / 100))
        pos = {
            "id": f"P{next(_seq)}", "symbol": cand["symbol"], "bias": cand["bias"],
            "side": ct.side, "option_key": ct.instrument_key,
            "option_symbol": ct.trading_symbol, "strike": ct.strike, "expiry": ct.expiry,
            "lot_size": ct.lot_size, "qty": qty, "mode": self.cfg.mode,
            "sell_price": round(sell_price, 2), "target": target, "stop": stop,
            "spot_at_entry": cand["spot"], "strength_at_entry": cand["strength"],
            "entry_time": now.strftime("%H:%M:%S"), "entry_day": now.strftime("%Y-%m-%d"),
            "ltp": round(sell_price, 2), "status": "open", "gtt_ids": [],
            "oi": ct.oi, "delta": ct.delta, "spread_pct": ct.spread_pct,
            "hedge": ({
                "side": hedge.side, "option_key": hedge.instrument_key,
                "option_symbol": hedge.trading_symbol, "strike": hedge.strike,
                "expiry": hedge.expiry, "lot_size": hedge.lot_size,
                "qty": self.cfg.lots * hedge.lot_size,
                "buy_price": round(hedge.ltp, 2), "ltp": round(hedge.ltp, 2),
            } if hedge else None),
        }
        if self.cfg.mode == "live" and self.cfg.use_gtt:
            # OCO via two SINGLE GTTs: BUY-below (target) + BUY-above (stop).
            # Upstox rejects an exit-only MULTIPLE ("one ENTRY strategy required").
            try:
                # store each leg id as it is placed so a partial failure can roll back
                pos["gtt_ids"].append(await self.client.place_gtt(
                    instrument_token=ct.instrument_key, quantity=qty,
                    transaction_type="BUY", trigger_type="BELOW",
                    trigger_price=target, product="D"))
                pos["gtt_ids"].append(await self.client.place_gtt(
                    instrument_token=ct.instrument_key, quantity=qty,
                    transaction_type="BUY", trigger_type="ABOVE",
                    trigger_price=stop, product="D"))
                self.log("trade", f"{cand['symbol']}: GTT exits attached "
                                  f"(target #{pos['gtt_ids'][0]}, stop #{pos['gtt_ids'][1]})")
            except UpstoxError as e:
                await self._cancel_gtts(pos)   # cancel whichever leg was placed
                pos["gtt_ids"] = []
                self.log("error", f"{cand['symbol']}: GTT attach failed — {e}. "
                                  "The app will place the exit order itself while it is "
                                  "running (keep it running, or fix/disable GTT).")
        if self.cfg.mode == "paper":
            pos["exit_mode"] = "paper (monitored locally)"
        elif pos["gtt_ids"]:
            pos["exit_mode"] = "GTT (target + stop)"
        else:
            pos["exit_mode"] = "app-managed"
        self.positions.append(pos)
        self._save_positions()
        hedge_txt = (f" + BOUGHT {pos['hedge']['option_symbol']} @ ₹{pos['hedge']['buy_price']:.2f} (ITM hedge)"
                     if pos["hedge"] else "")
        self.log("trade", f"SOLD {ct.trading_symbol} × {qty} @ ₹{sell_price:.2f} "
                          f"[{self.cfg.mode}] — {cand['symbol']} {cand['bias']} · "
                          f"target ₹{target:.2f} / stop ₹{stop:.2f} · exit: {pos['exit_mode']}"
                          + hedge_txt)
        return True

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
        keys = set()
        for p in opens:
            keys.add(p["option_key"])
            if p.get("hedge"):
                keys.add(p["hedge"]["option_key"])
        try:
            ltp_map = await self.client.ltp(list(keys))
        except UpstoxError:
            return
        for p in opens:
            if p.get("hedge"):
                hlp = ltp_map.get(p["hedge"]["option_key"])
                if hlp is not None:
                    p["hedge"]["ltp"] = round(hlp, 2)
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
                await self._close_hedge_live(p)
                self._close(p, exit_price=price, reason=reason, now=now)
                return
            await self._cancel_gtts(p)   # cancel both OCO legs before flattening
            try:
                await self.client.place_order(
                    instrument_token=p["option_key"], quantity=p["qty"],
                    transaction_type="BUY", order_type="MARKET", product="D",
                    tag="eod-exit")
            except UpstoxError as e:
                self.log("error", f"Live exit order failed for {p['symbol']}: {e} — "
                                  "will retry next tick")
                return
            await self._close_hedge_live(p)   # sell the ITM hedge alongside
        self._close(p, exit_price=price, reason=reason, now=now)

    async def _close_hedge_live(self, p: dict):
        """Live only: sell the bought ITM hedge leg to flatten it with the short."""
        h = p.get("hedge")
        if not h or h.get("closed"):
            return
        try:
            await self.client.place_order(
                instrument_token=h["option_key"], quantity=h["qty"],
                transaction_type="SELL", order_type="MARKET", product="D",
                tag="eod-hedge-exit")
            h["closed"] = True
        except UpstoxError as e:
            self.log("error", f"{p['symbol']}: hedge exit SELL failed — {e}")

    async def _cancel_gtts(self, p: dict):
        """Cancel every resting GTT leg on a position (supports the legacy
        single gtt_id field too)."""
        ids = list(p.get("gtt_ids") or [])
        if p.get("gtt_id"):
            ids.append(p["gtt_id"])
        for gid in ids:
            try:
                await self.client.cancel_gtt(gid)
            except UpstoxError:
                pass

    def _close(self, p: dict, exit_price: float, reason: str, now: datetime):
        p["status"] = "closed"
        p["exit_price"] = round(exit_price, 2)
        p["exit_reason"] = reason
        p["exit_time"] = now.strftime("%H:%M:%S")
        short_pnl = (p["sell_price"] - exit_price) * p["qty"]   # short: credit - buyback
        legs = 1
        h = p.get("hedge")
        hedge_pnl = 0.0
        hedge_fields = {}
        if h:
            legs = 2
            h_exit = 0.0 if reason == "expired" else (h.get("ltp") or h["buy_price"])
            h["exit_price"] = round(h_exit, 2)
            hedge_pnl = (h_exit - h["buy_price"]) * h["qty"]   # long: exit - entry
            hedge_fields = {
                "hedge_symbol": h["option_symbol"], "hedge_side": h["side"],
                "hedge_strike": h["strike"], "hedge_qty": h["qty"],
                "buy_price": h["buy_price"], "hedge_exit": round(h_exit, 2),
                "hedge_net_rs": round(hedge_pnl, 2),
            }
        gross = short_pnl + hedge_pnl
        charges = self.cfg.charges_per_trade * legs
        trade = {
            "entry_day": p["entry_day"], "exit_day": now.strftime("%Y-%m-%d"),
            "symbol": p["symbol"], "bias": p["bias"], "side": p["side"],
            "option_symbol": p["option_symbol"], "strike": p["strike"],
            "expiry": p["expiry"], "qty": p["qty"], "mode": p["mode"],
            "sell_price": p["sell_price"], "exit_price": round(exit_price, 2),
            "short_net_rs": round(short_pnl, 2),
            "spot_at_entry": p.get("spot_at_entry"),
            "entry_time": p["entry_time"], "exit_time": p["exit_time"],
            "reason": reason, "points": round(p["sell_price"] - exit_price, 2),
            "gross_rs": round(gross, 2), "charges_rs": round(charges, 2),
            "net_rs": round(gross - charges, 2), **hedge_fields,
        }
        self._record_trade(trade)
        self._save_positions()
        self.log("trade", f"CLOSED {p['option_symbol']} @ ₹{exit_price:.2f} ({reason})"
                          + (f" + hedge {h['option_symbol']} @ ₹{h['exit_price']:.2f}" if h else "")
                          + f" — net ₹{trade['net_rs']:+,.2f}")

    async def manual_exit(self, pos_id: str) -> bool:
        now = datetime.now(TZ)
        p = next((x for x in self.positions if x["id"] == pos_id and x["status"] == "open"), None)
        if not p:
            return False
        price = p.get("ltp") or p["sell_price"]
        try:
            keys = [p["option_key"]] + ([p["hedge"]["option_key"]] if p.get("hedge") else [])
            m = await self.client.ltp(keys)
            price = m.get(p["option_key"], price)
            if p.get("hedge") and m.get(p["hedge"]["option_key"]) is not None:
                p["hedge"]["ltp"] = round(m[p["hedge"]["option_key"]], 2)
        except UpstoxError:
            pass
        if self.cfg.mode == "live":
            await self._cancel_gtts(p)
            try:
                await self.client.place_order(
                    instrument_token=p["option_key"], quantity=p["qty"],
                    transaction_type="BUY", order_type="MARKET", product="D",
                    tag="eod-manual-exit")
            except UpstoxError as e:
                self.log("error", f"Manual exit failed for {p['symbol']}: {e}")
                return False
            await self._close_hedge_live(p)
        self._close(p, exit_price=price, reason="manual", now=now)
        return True

    # ---------- status ----------
    def status(self) -> dict:
        now = datetime.now(TZ)
        opens = [p for p in self.positions if p.get("status") == "open"]
        for p in opens:
            sell = p.get("sell_price") or 0.0
            up = (sell - (p.get("ltp") or sell)) * (p.get("qty") or 0)   # short leg
            h = p.get("hedge")
            if h:
                up += ((h.get("ltp") or h["buy_price"]) - h["buy_price"]) * h["qty"]  # long leg
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
