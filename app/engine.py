"""Three independent SMA/EMA engines (1m, 5m, 15m) on the NIFTY spot chart.

Within each timeframe, three strategies are compared (see strategy.py). In
paper mode all three run in parallel, each with its own position, all trading
the ATM option (long -> CE, short -> PE). In live mode only the configured
strategy trades. Because the three share the same SMA/EMA on the same candles,
the comparison is apples-to-apples.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from .broker import FILLED, REJECTED, LiveBroker, PaperBroker
from .config import (DATA_DIR, IST, SPOT_INSTRUMENT_KEY, TIMEFRAMES, AppConfig,
                     parse_hhmm)
from .market import SpotFeed, ema_series
from .options import OptionSelector
from .strategy import STRATEGIES, brick_size, context, decide, ichimoku, renko_dirs
from .upstox_client import UpstoxClient, UpstoxError

TZ = ZoneInfo(IST)
TRADES_FILE = DATA_DIR / "trades.jsonl"


class TFEngine:
    """One timeframe: a feed plus one slot per active strategy."""

    def __init__(self, parent: "Engine", timeframe: int):
        self.parent = parent
        self.tf = timeframe
        self.feed = SpotFeed(parent.client, timeframe, parent.cfg.candle_grace_sec)
        self.running = False
        # slot per strategy id: {"side","position","handled","pending_entry"}
        self.slots: dict[int, dict] = {}

    def cfg(self):
        return self.parent.cfg.tfcfg(self.tf)

    def broker(self):
        return (LiveBroker(self.parent.client) if self.cfg().mode == "live"
                else self.parent.paper_broker)

    def log(self, kind, msg):
        self.parent.log(kind, msg, tf=self.tf)

    def active_strats(self) -> list[int]:
        c = self.cfg()
        return [c.live_strategy] if c.mode == "live" else [1, 2]

    def _sync_slots(self):
        want = set(self.active_strats())
        for sid in want:
            self.slots.setdefault(sid, {"side": None, "position": None,
                                        "handled": None, "pending_entry": None})
        for sid in list(self.slots):
            # keep a slot with an open position even if it left the active set
            if sid not in want and not self.slots[sid]["position"]:
                del self.slots[sid]

    async def tick(self, now: datetime, ltp_map: dict):
        new = await self.feed.refresh()
        if self.feed.note:
            self.log("data", self.feed.note)
            self.feed.note = None
        self._sync_slots()

        # update LTPs on open legs
        for slot in self.slots.values():
            pos = slot["position"]
            if pos:
                lp = ltp_map.get(pos["option"]["instrument_key"])
                if lp:
                    pos["ltp"] = lp

        if not self.feed.candles:
            return
        candles = self.feed.candles
        last = candles[-1]

        square = (now.time() >= parse_hhmm(self.parent.cfg.square_off_at)
                  or not self.parent.market_hours(now))
        can_enter = (self.running and self.parent.market_hours(now)
                     and now.time() < parse_hhmm(self.parent.cfg.no_entries_after)
                     and not square)

        for sid in list(self.slots):
            await self._run_strategy(sid, now, candles, last, new, ltp_map,
                                     can_enter, square)

    async def _run_strategy(self, sid, now, candles, last, new, ltp_map,
                            can_enter, square):
        slot = self.slots[sid]
        cfg = self.cfg()

        # 1) settle any in-flight exit / entry fills first
        await self._settle(sid, now, ltp_map)

        pos = slot["position"]
        # 2) forced square-off
        if pos and square and not pos.get("exiting"):
            pos["exiting"] = "squareoff"
            await self._place_exit(sid)
            self.log("trade", f"[{STRATEGIES[sid]['name']}] EXIT (squareoff)")
            return

        # 3) act once per completed candle
        key = last.ts.isoformat()
        if slot["handled"] == key or not new:
            return
        slot["handled"] = key

        desired = decide(sid, candles, cfg, slot["side"])
        if desired == slot["side"]:
            return

        ctx = context(sid, candles, cfg)
        if pos and not pos.get("exiting"):
            pos["exiting"] = f"→{desired or 'flat'}"
            slot["pending_entry"] = desired if desired in ("long", "short") else None
            await self._place_exit(sid)
            self.log("trade", f"[{STRATEGIES[sid]['name']}] EXIT ({last.ts:%H:%M} · "
                              f"{ctx}) → {desired or 'flat'}")
        elif not pos and desired in ("long", "short"):
            if can_enter:
                await self._enter(sid, now, desired, last, ctx)

    async def _enter(self, sid, now, side, last, state):
        opt_side = "CE" if side == "long" else "PE"
        try:
            ct = await self.parent.selector.select(opt_side)
        except UpstoxError as e:
            self.log("error", f"[{STRATEGIES[sid]['name']}] contract selection failed: {e}")
            return
        c = self.cfg()
        qty = c.lots * ct.lot_size
        order = await self.broker().place(instrument_key=ct.instrument_key,
                                          side="BUY", order_type="MARKET", qty=qty)
        idx = self.parent.spot_ltp
        self.slots[sid].update({"side": side, "position": {
            "option": asdict(ct), "qty": qty, "order": order,
            "entry_price": None, "ltp": ct.ltp, "entry_ts": now.strftime("%H:%M:%S"),
            "signal_candle": last.ts.strftime("%H:%M"), "exiting": None,
            "exit_order": None, "entry_index": idx}})
        self.log("trade", f"[{STRATEGIES[sid]['name']}] {side.upper()} entry [{c.mode}] "
                          f"— index {idx:.2f} · {state} — buy {ct.trading_symbol}"
                          if idx else
                          f"[{STRATEGIES[sid]['name']}] {side.upper()} entry [{c.mode}] "
                          f"— {state} — buy {ct.trading_symbol}")

    async def _place_exit(self, sid):
        pos = self.slots[sid]["position"]
        if pos and pos["exit_order"] is None:
            pos["exit_order"] = await self.broker().place(
                instrument_key=pos["option"]["instrument_key"], side="SELL",
                order_type="MARKET", qty=pos["qty"])

    async def _settle(self, sid, now, ltp_map):
        slot = self.slots[sid]
        pos = slot["position"]
        if not pos:
            # a pending entry waiting for the slot to free
            if slot["pending_entry"] and self.parent.market_hours(now):
                side = slot["pending_entry"]
                slot["pending_entry"] = None
                last = self.feed.candles[-1]
                ctx = context(sid, self.feed.candles, self.cfg())
                await self._enter(sid, now, side, last, ctx)
            return
        lp = ltp_map.get(pos["option"]["instrument_key"])
        # fill the entry market order
        if pos["entry_price"] is None:
            o = await self.broker().poll(pos["order"], lp)
            if o.status == FILLED:
                pos["entry_price"] = o.avg_price
            elif o.status == REJECTED:
                pos["entry_price"] = 0.0
        # settle the exit market order
        if pos.get("exiting") and pos["exit_order"] is not None and pos["entry_price"] is not None:
            o = await self.broker().poll(pos["exit_order"], lp)
            if o.status == FILLED:
                self._finalize(sid, o.avg_price, now)

    def _finalize(self, sid, exit_price, now):
        slot = self.slots[sid]
        pos = slot["position"]
        slot["position"] = None
        slot["side"] = None
        c = self.cfg()
        entry = pos.get("entry_price") or 0.0
        pts = exit_price - entry
        gross = pts * pos["qty"]
        charges = self.parent.cfg.round_trip_charges
        entry_idx = pos.get("entry_index")
        exit_idx = self.parent.spot_ltp
        trade = {
            "day": now.strftime("%Y-%m-%d"), "tf": f"{self.tf}m", "mode": c.mode,
            "strategy": sid, "strategy_name": STRATEGIES[sid]["name"],
            "side": "long" if pos["option"]["side"] == "CE" else "short",
            "symbol": pos["option"]["trading_symbol"], "strike": pos["option"]["strike"],
            "qty": pos["qty"], "entry_time": pos["entry_ts"],
            "exit_time": now.strftime("%H:%M:%S"),
            "entry": round(entry, 2), "exit": round(exit_price, 2),
            "entry_index": round(entry_idx, 2) if entry_idx else None,
            "exit_index": round(exit_idx, 2) if exit_idx else None,
            "index_points": (round(exit_idx - entry_idx, 2)
                             if (entry_idx and exit_idx) else None),
            "points": round(pts, 2), "gross_rs": round(gross, 2),
            "charges_rs": round(charges, 2), "net_rs": round(gross - charges, 2),
            "reason": pos.get("exiting"),
        }
        self.parent.record_trade(trade)
        self.log("trade", f"[{STRATEGIES[sid]['name']}] EXITED {trade['symbol']} "
                          f"@ ₹{exit_price:.2f}"
                          + (f" · index {exit_idx:.2f}" if exit_idx else "")
                          + f" — {pts:+.2f} pts, net ₹{trade['net_rs']:+,.2f}")

    def status(self):
        c = self.cfg()
        positions = []
        for sid in sorted(self.slots):
            pos = self.slots[sid]["position"]
            if not pos:
                continue
            entry = pos.get("entry_price")
            ltp = pos.get("ltp") or 0
            upts = (ltp - entry) if entry else 0
            positions.append({
                "strategy": sid, "strategy_name": STRATEGIES[sid]["name"],
                "side": self.slots[sid]["side"], "symbol": pos["option"]["trading_symbol"],
                "strike": pos["option"]["strike"], "qty": pos["qty"],
                "entry": round(entry, 2) if entry else None, "ltp": round(ltp, 2),
                "entry_time": pos["entry_ts"], "exiting": pos.get("exiting"),
                "unreal_pts": round(upts, 2), "unreal_rs": round(upts * pos["qty"], 2)})
        return {"tf": self.tf, "running": self.running, "config": asdict(c),
                "feed": self._chart(c), "positions": positions}

    def _chart(self, c, points: int = 90) -> dict:
        """Close + EMA-20 (Renko overlay) + Tenkan/Kijun (Ichimoku) for the chart."""
        cs = self.feed.candles
        closes = [x.close for x in cs]
        ema = ema_series(closes, c.ema_filter_period)
        t, k, _sa, _sb = ichimoku(cs, c.tenkan, c.kijun, c.senkou_b) if cs else ([], [], [], [])
        n = len(cs)
        rng = range(max(0, n - points), n)
        last = cs[-1] if cs else None
        cur = lambda arr: (round(arr[-1], 2) if arr and arr[-1] is not None else None)
        return {
            "timeframe": self.tf,
            "last_ts": last.ts.strftime("%H:%M") if last else None,
            "last_close": round(last.close, 2) if last else None,
            "ema": cur(ema), "tenkan": cur(t), "kijun": cur(k),
            "brick": round(brick_size(cs, c), 2) if cs else None,
            "series": [
                {"t": cs[i].ts.strftime("%H:%M"), "c": round(cs[i].close, 2),
                 "e": round(ema[i], 2) if ema[i] is not None else None,
                 "tk": round(t[i], 2) if t[i] is not None else None,
                 "kj": round(k[i], 2) if k[i] is not None else None}
                for i in rng],
        }


class Engine:
    def __init__(self, client: UpstoxClient, cfg: AppConfig):
        self.client = client
        self.cfg = cfg
        self.selector = OptionSelector(client)
        self.paper_broker = PaperBroker()
        self.engines = {t: TFEngine(self, t) for t in TIMEFRAMES}
        self.spot_ltp = None
        self.events: deque[dict] = deque(maxlen=300)
        self.trades: list[dict] = self._load_today_trades()
        self._last_error = ""
        self._task = None

    def start(self, tf):
        if not self.client.access_token:
            raise UpstoxError("Connect to Upstox before starting")
        self.engines[tf].running = True
        self.log("engine", f"Engine started ({self.cfg.tfcfg(tf).mode})", tf=tf)

    def stop(self, tf):
        self.engines[tf].running = False
        self.log("engine", "Engine stopped (open positions still managed)", tf=tf)

    def ensure_loop(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    @staticmethod
    def market_hours(now):
        from datetime import time as _t
        return now.weekday() < 5 and _t(9, 15) <= now.time() < _t(15, 30)

    def log(self, kind, msg, tf=None):
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

    def _load_today_trades(self):
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

    def record_trade(self, trade):
        self.trades.append(trade)
        with TRADES_FILE.open("a") as f:
            f.write(json.dumps(trade) + "\n")

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

    def _note_error(self, msg):
        if msg != self._last_error:
            self._last_error = msg
            self.log("error", msg)

    async def _tick(self):
        now = datetime.now(TZ)
        if not self.client.access_token:
            return
        keys = [SPOT_INSTRUMENT_KEY]
        for e in self.engines.values():
            for slot in e.slots.values():
                if slot["position"]:
                    k = slot["position"]["option"]["instrument_key"]
                    if k not in keys:
                        keys.append(k)
        ltp_map = await self.client.ltp(keys)
        self.spot_ltp = ltp_map.get(SPOT_INSTRUMENT_KEY, self.spot_ltp)
        for e in self.engines.values():
            await e.tick(now, ltp_map)

    def comparison(self) -> dict:
        agg = {sid: {"trades": 0, "wins": 0, "net": 0.0, "points": 0.0}
               for sid in (1, 2)}
        for t in self.trades:
            sid = t.get("strategy")
            if sid in agg:
                a = agg[sid]
                a["trades"] += 1
                a["wins"] += 1 if t["net_rs"] > 0 else 0
                a["net"] = round(a["net"] + t["net_rs"], 2)
                a["points"] = round(a["points"] + t["points"], 2)
        for sid in agg:
            n = agg[sid]["trades"]
            agg[sid]["win_rate"] = round(100 * agg[sid]["wins"] / n, 1) if n else 0.0
            agg[sid]["name"] = STRATEGIES[sid]["name"]
            agg[sid]["desc"] = STRATEGIES[sid]["desc"]
        traded = {sid: agg[sid] for sid in agg if agg[sid]["trades"]}
        leader = max(traded, key=lambda s: traded[s]["net"]) if traded else None
        return {"strategies": agg, "leader": leader}

    def status(self):
        now = datetime.now(TZ)
        return {
            "now_ist": now.strftime("%Y-%m-%d %H:%M:%S"),
            "market_open": self.market_hours(now),
            "spot": self.spot_ltp, "expiry": self.selector.expiry,
            "lot_size": self.selector.lot_size, "capital": self.cfg.capital,
            "strategies": {str(k): v for k, v in STRATEGIES.items()},
            "engines": {f"{t}m": self.engines[t].status() for t in TIMEFRAMES},
            "comparison": self.comparison(),
            "trades": list(reversed(self.trades)),
            "events": list(self.events)[:120],
        }
