"""Three independent SMA+EMA engines (1m, 5m, 15m) on the NIFTY spot chart.

Entry/exit is driven purely by the spot close vs its SMA and EMA (closed
candles only). The option is the instrument that expresses the view:
  long  -> buy CE,  short -> buy PE.

Paper mode opens BOTH an ATM and an ITM leg per signal (same entry/exit
timing, driven by the spot signal) so their P&L can be compared directly.
Live mode places one real order (ATM or ITM per config).
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from .broker import FILLED, REJECTED, BrokerOrder, LiveBroker, PaperBroker
from .config import (DATA_DIR, IST, SPOT_INSTRUMENT_KEY, TIMEFRAMES, AppConfig,
                     parse_hhmm)
from .market import SpotFeed
from .options import OptionSelector
from .upstox_client import UpstoxClient, UpstoxError

TZ = ZoneInfo(IST)
TRADES_FILE = DATA_DIR / "trades.jsonl"


class TFEngine:
    """One timeframe's engine: feed + signal + position (one or two legs)."""

    def __init__(self, parent: "Engine", timeframe: int):
        self.parent = parent
        self.tf = timeframe
        self.feed = SpotFeed(parent.client, timeframe, parent.cfg.candle_grace_sec)
        self.running = False
        self.position: dict | None = None   # {"side","entry_ts","legs":{kind:leg}}
        self._handled: str | None = None

    def cfg(self):
        return self.parent.cfg.tfcfg(self.tf)

    def broker(self):
        c = self.cfg()
        return LiveBroker(self.parent.client) if c.mode == "live" else self.parent.paper_broker

    def log(self, kind: str, msg: str):
        self.parent.log(kind, msg, tf=self.tf)

    async def tick(self, now: datetime, ltp_map: dict):
        c = self.cfg()
        self.feed.set_periods(c.sma_period, c.ema_period)
        new = await self.feed.refresh()
        if self.feed.note:
            self.log("data", self.feed.note)
            self.feed.note = None

        # update leg LTPs
        if self.position:
            for leg in self.position["legs"].values():
                lp = ltp_map.get(leg["option"]["instrument_key"])
                if lp:
                    leg["ltp"] = lp

        state = self.feed.signal_state()
        if state is None:
            return

        # forced square-off / no new entries windows
        square = now.time() >= parse_hhmm(self.parent.cfg.square_off_at)
        can_enter = (self.running and self.parent.market_hours(now)
                     and now.time() < parse_hhmm(self.parent.cfg.no_entries_after))

        if self.position:
            self._manage_exit(now, state, square)
            await self._settle(now, ltp_map)
        elif can_enter and new:
            # act only once per completed candle
            key = state["ts"].isoformat()
            if self._handled != key:
                self._handled = key
                if state["above_both"]:
                    await self._enter(now, "long", "CE", state)
                elif state["below_both"]:
                    await self._enter(now, "short", "PE", state)

    async def _enter(self, now: datetime, side: str, opt_side: str, state: dict):
        c = self.cfg()
        try:
            picks = await self.parent.selector.select(opt_side, c)
        except UpstoxError as e:
            self.log("error", f"Contract selection failed: {e}")
            return
        if c.mode == "live":
            want = c.live_contract
            picks = {want: picks[want]} if want in picks else {}
        if not picks:
            self.log("signal", f"{side.upper()} signal but no tradeable "
                               f"{opt_side} contract found — skipped")
            return
        legs = {}
        for kind, ct in picks.items():
            qty = c.lots * ct.lot_size
            order = await self.broker().place(
                instrument_key=ct.instrument_key, side="BUY",
                order_type="MARKET", qty=qty)
            legs[kind] = {"option": asdict(ct), "qty": qty, "order": order,
                          "entry_price": None, "ltp": ct.ltp,
                          "exit_order": None, "exit_reason": None}
        self.position = {"side": side, "opt_side": opt_side,
                         "entry_ts": now.strftime("%H:%M:%S"),
                         "signal_candle": state["ts"].strftime("%H:%M"),
                         "legs": legs, "exiting": None}
        detail = ", ".join(f"{k} {legs[k]['option']['trading_symbol']}" for k in legs)
        self.log("trade", f"{side.upper()} entry [{c.mode}] — close {state['close']:.2f} "
                          f"{'>' if side == 'long' else '<'} SMA {state['sma']:.2f} & "
                          f"EMA {state['ema']:.2f} | buying {detail}")

    def _manage_exit(self, now: datetime, state: dict, square: bool):
        pos = self.position
        if pos["exiting"]:
            return
        if square or not self.parent.market_hours(now):
            pos["exiting"] = "squareoff"
        elif pos["side"] == "long" and state["below_either"]:
            pos["exiting"] = "close<SMA/EMA"
        elif pos["side"] == "short" and state["above_either"]:
            pos["exiting"] = "close>SMA/EMA"
        if pos["exiting"]:
            self.log("trade", f"EXIT ({pos['exiting']}) — candle {state['ts']:%H:%M} "
                              f"close {state['close']:.2f} vs SMA {state['sma']:.2f} / "
                              f"EMA {state['ema']:.2f}")

    async def _settle(self, now: datetime, ltp_map: dict):
        pos = self.position
        if not pos["exiting"]:
            return
        done = True
        for kind, leg in pos["legs"].items():
            lp = ltp_map.get(leg["option"]["instrument_key"])
            if leg["entry_price"] is None:
                o = await self.broker().poll(leg["order"], lp)
                if o.status == FILLED:
                    leg["entry_price"] = o.avg_price
                elif o.status == REJECTED:
                    leg["entry_price"] = 0.0
                else:
                    done = False
                    continue
            if leg.get("exit_done"):
                continue
            if leg["exit_order"] is None:
                leg["exit_order"] = await self.broker().place(
                    instrument_key=leg["option"]["instrument_key"], side="SELL",
                    order_type="MARKET", qty=leg["qty"])
            o = await self.broker().poll(leg["exit_order"], lp)
            if o.status == FILLED:
                leg["exit_price"] = o.avg_price
                leg["exit_done"] = True
            else:
                done = False
        if done:
            self._finalize(now)

    async def ensure_entry_fills(self, ltp_map: dict):
        """Fill pending entry MARKET orders (called each tick before exit checks)."""
        if not self.position:
            return
        for leg in self.position["legs"].values():
            if leg["entry_price"] is None:
                lp = ltp_map.get(leg["option"]["instrument_key"])
                o = await self.broker().poll(leg["order"], lp)
                if o.status == FILLED:
                    leg["entry_price"] = o.avg_price
                elif o.status == REJECTED:
                    leg["entry_price"] = 0.0

    def _finalize(self, now: datetime):
        pos = self.position
        self.position = None
        c = self.cfg()
        for kind, leg in pos["legs"].items():
            entry = leg.get("entry_price") or 0.0
            exit_p = leg.get("exit_price") or 0.0
            pts = exit_p - entry
            gross = pts * leg["qty"]
            charges = self.parent.cfg.round_trip_charges
            trade = {
                "day": now.strftime("%Y-%m-%d"), "tf": f"{self.tf}m",
                "mode": c.mode, "kind": kind, "side": pos["side"],
                "symbol": leg["option"]["trading_symbol"],
                "strike": leg["option"]["strike"], "qty": leg["qty"],
                "entry_time": pos["entry_ts"], "exit_time": now.strftime("%H:%M:%S"),
                "entry": round(entry, 2), "exit": round(exit_p, 2),
                "points": round(pts, 2), "gross_rs": round(gross, 2),
                "charges_rs": round(charges, 2), "net_rs": round(gross - charges, 2),
                "reason": pos["exiting"],
                "delta": leg["option"].get("delta"), "oi": leg["option"].get("oi"),
                "spread_pct": leg["option"].get("spread_pct"),
            }
            self.parent.record_trade(trade)
            self.log("trade", f"EXITED {kind} {trade['symbol']} @ ₹{exit_p:.2f} "
                              f"({pos['exiting']}) — {pts:+.2f} pts, net ₹{trade['net_rs']:+,.2f}")

    def status(self) -> dict:
        c = self.cfg()
        pos = None
        if self.position:
            legs = []
            for kind, leg in self.position["legs"].items():
                entry = leg.get("entry_price")
                ltp = leg.get("ltp") or 0
                upts = (ltp - entry) if entry else 0
                legs.append({
                    "kind": kind, "symbol": leg["option"]["trading_symbol"],
                    "strike": leg["option"]["strike"], "qty": leg["qty"],
                    "entry": round(entry, 2) if entry else None, "ltp": round(ltp, 2),
                    "delta": leg["option"].get("delta"), "oi": leg["option"].get("oi"),
                    "spread_pct": leg["option"].get("spread_pct"),
                    "unreal_pts": round(upts, 2), "unreal_rs": round(upts * leg["qty"], 2),
                })
            pos = {"side": self.position["side"], "opt_side": self.position["opt_side"],
                   "entry_time": self.position["entry_ts"],
                   "signal_candle": self.position["signal_candle"],
                   "exiting": self.position["exiting"], "legs": legs}
        return {"tf": self.tf, "running": self.running, "config": asdict(c),
                "feed": self.feed.snapshot(), "position": pos}


class Engine:
    def __init__(self, client: UpstoxClient, cfg: AppConfig):
        self.client = client
        self.cfg = cfg
        self.selector = OptionSelector(client)
        self.paper_broker = PaperBroker()
        self.engines = {t: TFEngine(self, t) for t in TIMEFRAMES}
        self.spot_ltp: float | None = None
        self.events: deque[dict] = deque(maxlen=300)
        self.trades: list[dict] = self._load_today_trades()
        self._last_error = ""
        self._task: asyncio.Task | None = None

    # ---- lifecycle ----
    def start(self, tf: int):
        if not self.client.access_token:
            raise UpstoxError("Connect to Upstox before starting")
        self.engines[tf].running = True
        self.log("engine", f"Engine started ({self.cfg.tfcfg(tf).mode})", tf=tf)

    def stop(self, tf: int):
        self.engines[tf].running = False
        self.log("engine", "Engine stopped (open position still managed)", tf=tf)

    def ensure_loop(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    @staticmethod
    def market_hours(now: datetime) -> bool:
        from datetime import time as _t
        return now.weekday() < 5 and _t(9, 15) <= now.time() < _t(15, 30)

    # ---- logging / persistence ----
    def log(self, kind: str, msg: str, tf: int | None = None):
        now = datetime.now(TZ)
        tag = f"{tf}m" if tf else None
        self.events.appendleft({"ts": now.strftime("%H:%M:%S"), "kind": kind,
                                "msg": msg, "tf": tag})
        try:
            with (DATA_DIR / f"events-{now:%Y-%m-%d}.log").open("a", encoding="utf-8") as f:
                f.write(f"{now:%H:%M:%S} [{tag or '--'}] [{kind}] {msg}\n")
            with (DATA_DIR / f"events-{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now.isoformat(), "tf": tag,
                                    "kind": kind, "msg": msg}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _load_today_trades(self) -> list[dict]:
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        out = []
        if TRADES_FILE.exists():
            for line in TRADES_FILE.read_text().splitlines():
                try:
                    t = json.loads(line)
                    if t.get("day") == today:
                        out.append(t)
                except Exception:
                    continue
        return out

    def record_trade(self, trade: dict):
        self.trades.append(trade)
        with TRADES_FILE.open("a") as f:
            f.write(json.dumps(trade) + "\n")

    # ---- main loop ----
    async def _run(self):
        while True:
            try:
                await self._tick()
            except UpstoxError as e:
                self._note_error(str(e))
            except Exception as e:
                self._note_error(f"{type(e).__name__}: {e}")
            in_mkt = self.market_hours(datetime.now(TZ))
            await asyncio.sleep(2 if (in_mkt and self.client.access_token) else 15)

    def _note_error(self, msg: str):
        if msg != self._last_error:
            self._last_error = msg
            self.log("error", msg)

    async def _tick(self):
        now = datetime.now(TZ)
        if not self.client.access_token:
            return
        keys = [SPOT_INSTRUMENT_KEY]
        for eng in self.engines.values():
            if eng.position:
                for leg in eng.position["legs"].values():
                    k = leg["option"]["instrument_key"]
                    if k not in keys:
                        keys.append(k)
        ltp_map = await self.client.ltp(keys)
        self.spot_ltp = ltp_map.get(SPOT_INSTRUMENT_KEY, self.spot_ltp)
        for eng in self.engines.values():
            await eng.ensure_entry_fills(ltp_map)
            await eng.tick(now, ltp_map)

    # ---- comparison + status ----
    def comparison(self) -> dict:
        agg = {"ATM": {"trades": 0, "wins": 0, "net": 0.0, "points": 0.0},
               "ITM": {"trades": 0, "wins": 0, "net": 0.0, "points": 0.0}}
        for t in self.trades:
            k = t.get("kind")
            if k in agg:
                a = agg[k]
                a["trades"] += 1
                a["wins"] += 1 if t["net_rs"] > 0 else 0
                a["net"] = round(a["net"] + t["net_rs"], 2)
                a["points"] = round(a["points"] + t["points"], 2)
        for k in agg:
            n = agg[k]["trades"]
            agg[k]["win_rate"] = round(100 * agg[k]["wins"] / n, 1) if n else 0.0
        lead = None
        if agg["ATM"]["trades"] or agg["ITM"]["trades"]:
            lead = "ATM" if agg["ATM"]["net"] >= agg["ITM"]["net"] else "ITM"
        return {"ATM": agg["ATM"], "ITM": agg["ITM"], "leader": lead}

    def status(self) -> dict:
        now = datetime.now(TZ)
        return {
            "now_ist": now.strftime("%Y-%m-%d %H:%M:%S"),
            "market_open": self.market_hours(now),
            "spot": self.spot_ltp,
            "expiry": self.selector.expiry,
            "lot_size": self.selector.lot_size,
            "capital": self.cfg.capital,
            "engines": {f"{t}m": self.engines[t].status() for t in TIMEFRAMES},
            "comparison": self.comparison(),
            "trades": list(reversed(self.trades)),
            "events": list(self.events)[:120],
        }
