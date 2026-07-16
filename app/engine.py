"""The automated trading engine.

Flow per completed spot candle (1-min and/or 5-min):
  decisive 9-EMA close-break on NIFTY spot  ->  risk gates  ->  pick ATM/ITM
  option from live chain  ->  BUY 2 lots via LIMIT order  ->  monitor real
  premium LTP  ->  exit at +target (LIMIT) / -stoploss (MARKET) / square-off.

The same path runs in paper and live mode; only the execution layer differs.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .broker import CANCELLED, FILLED, OPEN, REJECTED, BrokerOrder, LiveBroker, PaperBroker
from .config import DATA_DIR, IST, SPOT_INSTRUMENT_KEY, StrategyConfig, save_strategy_config
from .market import SpotFeed
from .options import OptionSelector, SelectedOption
from .risk import RiskManager, _parse_hhmm
from .upstox_client import UpstoxClient, UpstoxError

TZ = ZoneInfo(IST)
TRADES_FILE = DATA_DIR / "trades.jsonl"
POSITIONS_FILE = DATA_DIR / "positions.json"


class Engine:
    def __init__(self, client: UpstoxClient, cfg: StrategyConfig):
        self.client = client
        self.cfg = cfg
        self.risk = RiskManager()
        self.selector = OptionSelector(client)
        self.paper_broker = PaperBroker()
        self.running = False
        self.feeds: dict[int, SpotFeed] = {}
        self._sync_feeds()

        self.spot_ltp: float | None = None
        self.pendings: dict[int, dict] = {}   # entry orders in flight, keyed by timeframe
        self.positions: dict[int, dict] = {}  # open long positions, keyed by timeframe
        self._manual_squareoff = False
        self._handled_candle: dict[int, str] = {}
        self._deferred: dict[int, dict] = {}  # signals waiting for a freed slot

        self.events: deque[dict] = deque(maxlen=200)
        self.trades: list[dict] = self._load_today_trades()
        self._last_error: str = ""
        self._last_error_ts: float = 0.0
        self._task: asyncio.Task | None = None
        self._load_positions()

    # ---------------- lifecycle ----------------

    def broker(self):
        return LiveBroker(self.client) if self.cfg.mode == "live" else self.paper_broker

    def _sync_feeds(self) -> None:
        for tf in self.cfg.timeframes:
            if tf not in self.feeds:
                self.feeds[tf] = SpotFeed(self.client, tf, self.cfg.ema_period,
                                          self.cfg.candle_grace_sec)
            self.feeds[tf].ema_period = self.cfg.for_tf(tf).ema_period
            self.feeds[tf].grace_sec = self.cfg.candle_grace_sec
        for tf in list(self.feeds):
            # keep the feed while a position/order of this timeframe is active —
            # the EMA-touch exit needs its candles even if the tf was disabled
            if tf not in self.cfg.timeframes and tf not in self.positions \
                    and tf not in self.pendings:
                del self.feeds[tf]

    def start(self) -> None:
        if not self.client.access_token:
            raise UpstoxError("Connect to Upstox before starting the engine")
        self.running = True
        self.log("engine", f"Engine started — mode={self.cfg.mode.upper()}, "
                           f"timeframes={[f'{t}m' for t in self.cfg.timeframes]}")

    def stop(self) -> None:
        self.running = False
        self.log("engine", "Engine stopped (open position, if any, is still managed)")

    def request_squareoff(self) -> None:
        self._manual_squareoff = True
        self.log("engine", "Manual square-off requested")

    def set_mode(self, mode: str) -> None:
        if self.positions or self.pendings:
            raise UpstoxError("Cannot switch mode with an open or pending position")
        self.cfg.mode = mode
        self.cfg.validate()
        save_strategy_config(self.cfg)
        self.log("engine", f"Mode switched to {mode.upper()}")

    def ensure_loop(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    # ---------------- logging / persistence ----------------

    def log(self, kind: str, msg: str, tf: int | None = None) -> None:
        now = datetime.now(TZ)
        tf_tag = f"{tf}m" if tf else None
        self.events.appendleft({"ts": now.strftime("%H:%M:%S"), "kind": kind,
                                "msg": msg, "tf": tf_tag})
        try:  # persistent copies, one pair of files per day:
            # .log  — human-readable;  .jsonl — structured, for analysis
            with (DATA_DIR / f"events-{now:%Y-%m-%d}.log").open("a", encoding="utf-8") as f:
                f.write(f"{now:%H:%M:%S} [{tf_tag or '--'}] [{kind}] {msg}\n")
            with (DATA_DIR / f"events-{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now.isoformat(), "tf": tf_tag,
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

    def _append_trade(self, trade: dict) -> None:
        self.trades.append(trade)
        with TRADES_FILE.open("a") as f:
            f.write(json.dumps(trade) + "\n")

    def _save_positions(self) -> None:
        """Persist open positions so an app restart mid-trade cannot orphan them."""
        def ser(pos: dict) -> dict:
            d = {k: v for k, v in pos.items() if k != "exit_order"}
            o = pos.get("exit_order")
            if o is not None:
                d["exit_order_state"] = {
                    "order_id": o.order_id, "instrument_key": o.instrument_key,
                    "side": o.side, "order_type": o.order_type,
                    "qty": o.qty, "limit_price": o.limit_price}
            return d
        try:
            POSITIONS_FILE.write_text(json.dumps({
                "day": datetime.now(TZ).strftime("%Y-%m-%d"), "mode": self.cfg.mode,
                "positions": {str(tf): ser(p) for tf, p in self.positions.items()}}))
        except OSError:
            pass

    def _load_positions(self) -> None:
        if not POSITIONS_FILE.exists():
            return
        try:
            d = json.loads(POSITIONS_FILE.read_text())
        except Exception:
            return
        if (d.get("day") != datetime.now(TZ).strftime("%Y-%m-%d")
                or d.get("mode") != self.cfg.mode):
            return
        for tf_s, p in d.get("positions", {}).items():
            st = p.pop("exit_order_state", None)
            p["exit_order"] = None
            if st and self.cfg.mode == "live":
                # a live exit order survives the restart — resume polling it by id
                p["exit_order"] = BrokerOrder(
                    order_id=st["order_id"], instrument_key=st["instrument_key"],
                    side=st["side"], order_type=st["order_type"],
                    qty=st["qty"], limit_price=st["limit_price"])
            self.positions[int(tf_s)] = p
            self.log("engine", f"Restored open position "
                               f"{p['option']['trading_symbol']} after restart"
                               + (" (resuming exit order)" if p["exit_order"] else ""),
                     tf=int(tf_s))
        if self.positions:
            self._sync_feeds()

    # ---------------- main loop ----------------

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except UpstoxError as e:
                self._note_error(str(e))
            except Exception as e:  # keep the loop alive no matter what
                self._note_error(f"{type(e).__name__}: {e}")
            in_market = self.risk.market_hours(datetime.now(TZ))
            await asyncio.sleep(2 if (in_market and self.client.access_token) else 15)

    def _note_error(self, msg: str) -> None:
        # dedupe repeats, but resurface a persisting error every 2 minutes so a
        # stream of identical failures cannot go silently unnoticed
        now_t = time.time()
        if msg != self._last_error or now_t - self._last_error_ts > 120:
            self._last_error = msg
            self._last_error_ts = now_t
            self.log("error", msg)

    async def _tick(self) -> None:
        now = datetime.now(TZ)
        self.risk.roll_day()
        self._sync_feeds()
        if not self.client.access_token:
            return

        # --- live prices (real Upstox LTP; one call for spot + active options) ---
        keys = [SPOT_INSTRUMENT_KEY]
        for a in list(self.pendings.values()) + list(self.positions.values()):
            k = a["option"]["instrument_key"]
            if k not in keys:
                keys.append(k)
        ltp_map = await self.client.ltp(keys)
        self.spot_ltp = ltp_map.get(SPOT_INSTRUMENT_KEY, self.spot_ltp)

        # --- refresh candles FIRST so exits act on the freshest close ---
        new_by_tf: dict[int, list] = {}
        if self.risk.market_hours(now) or not self.feeds[self.cfg.timeframes[0]].candles:
            for tf in sorted(self.cfg.timeframes, reverse=True):
                feed = self.feeds[tf]
                new_by_tf[tf] = await feed.refresh()
                if feed.note:   # observed data revision after candle completion
                    self.log("data", feed.note, tf=tf)
                    feed.note = None

        # --- manage in-flight entry orders ---
        for tf, p in list(self.pendings.items()):
            await self._manage_pending(tf, p, now, ltp_map.get(p["option"]["instrument_key"]))

        # --- manage open positions (runs even when engine is stopped) ---
        for pos in list(self.positions.values()):
            opt_ltp = ltp_map.get(pos["option"]["instrument_key"])
            if opt_ltp:
                pos["ltp"] = opt_ltp
            await self._manage_position(pos, now, opt_ltp)

        # --- signals: higher timeframe first (the spec's primary chart gets
        # first claim on a position slot at shared candle boundaries) ---
        for tf in sorted(self.cfg.timeframes, reverse=True):
            new = new_by_tf.get(tf) or []
            if new and self.running:
                feed = self.feeds[tf]
                if len(new) > 1:
                    self.log("data", f"{len(new)} candles completed in one "
                                     f"poll (data delay) — evaluating each", tf=tf)
                base = len(feed.candles) - len(new)
                for k in range(len(new)):
                    # only the newest candle may trigger an entry; older
                    # ones are evaluated for the log so nothing vanishes
                    await self._check_signal(now, feed, tf, at=base + k,
                                             actionable=(k == len(new) - 1))

        # --- deferred signals: enter as soon as the exiting position frees the slot ---
        await self._try_deferred(now)

    async def _check_signal(self, now: datetime, feed: SpotFeed, tf: int,
                            at: int | None = None, actionable: bool = True) -> None:
        tfcfg = self.cfg.for_tf(tf)
        signal, flat_info = feed.evaluate_signal(tfcfg, at)
        if signal is None:
            if flat_info.get("raw_side") and feed.candles:
                ts = feed.candles[at if at is not None else -1].ts
                key = ts.isoformat()
                if self._handled_candle.get(tf) != key:
                    self._handled_candle[tf] = key
                    self.log("signal",
                             f"{tf}m candle {ts.strftime('%H:%M')} crossed "
                             f"{'above' if flat_info['raw_side'] == 'CE' else 'below'} 9-EMA "
                             f"but closed only {flat_info['margin']:.2f} pts beyond "
                             f"(need > {tfcfg.decisive_points}) — not decisive, no entry", tf=tf)
            return
        key = signal.candle_ts.isoformat()
        if self._handled_candle.get(tf) == key:
            return
        self._handled_candle[tf] = key

        label = (f"{tf}m candle {signal.candle_ts.strftime('%H:%M')} closed "
                 f"{'above' if signal.side == 'CE' else 'below'} 9-EMA "
                 f"(close {signal.close:.2f} / EMA {signal.ema:.2f})")
        if not actionable:
            self.log("signal", f"{label} — {signal.side} signal EXPIRED "
                               f"(a newer candle arrived in the same poll) — no entry", tf=tf)
            return
        reason = self.risk.entry_gate(now, self.cfg)
        if reason:
            self.log("signal", f"{label} — SKIPPED: {reason}", tf=tf)
            return
        if (self.cfg.skip_first_cross and flat_info.get("first_cross")
                and signal.candle_ts.time() < _parse_hhmm(self.cfg.skip_first_cross_before)):
            self.log("signal", f"{label} — SKIPPED: first EMA cross of the session "
                               f"before {self.cfg.skip_first_cross_before} (opening "
                               f"gap-settling — entries start from the second cross)", tf=tf)
            return
        if flat_info["flat"]:
            self.log("signal", f"{label} — SKIPPED: flat 9-EMA "
                     f"(moved {flat_info['move']:.2f} pts over "
                     f"{tfcfg.flat_ema_lookback} candles, need ≥ {flat_info['needed']:.2f})",
                     tf=tf)
            return
        holder = self._slot_holder(tf)
        if holder is not None:
            # a reversal candle exits the old position and signals the new one
            # simultaneously — don't discard the signal, wait for the slot
            # through the succeeding candle (the spec's entry window)
            expires = signal.candle_ts + timedelta(minutes=2 * tf)
            self._deferred[tf] = {"signal": signal, "label": label, "expires": expires}
            self.log("signal", f"{label} — slot held by a {holder['tf']}m position; "
                               f"deferred (enters if the slot frees before "
                               f"{expires.strftime('%H:%M')})", tf=tf)
            return

        self.log("signal", f"{label} — {signal.side} entry trigger", tf=tf)
        self._deferred.pop(tf, None)
        await self._enter(signal.side, tf, signal.candle_ts)

    def _slot_holder(self, tf: int) -> dict | None:
        """The position/pending currently occupying tf's entry slot, if any."""
        if self.cfg.per_timeframe_positions:
            return self.positions.get(tf) or self.pendings.get(tf)
        held = list(self.positions.values()) + list(self.pendings.values())
        return held[0] if held else None

    async def _try_deferred(self, now: datetime) -> None:
        for tf, d in list(self._deferred.items()):
            if tf not in self.cfg.timeframes or not self.running:
                del self._deferred[tf]
                continue
            if now >= d["expires"]:
                del self._deferred[tf]
                self.log("signal", f"{d['label']} — deferred entry expired "
                                   f"(slot never freed)", tf=tf)
                continue
            if self._slot_holder(tf) is not None:
                continue
            del self._deferred[tf]
            reason = self.risk.entry_gate(now, self.cfg)
            if reason:
                self.log("signal", f"{d['label']} — deferred entry blocked: {reason}", tf=tf)
                continue
            self.log("signal", f"{d['label']} — slot freed, entering now", tf=tf)
            await self._enter(d["signal"].side, tf, d["signal"].candle_ts)

    # ---------------- entries ----------------

    async def _enter(self, side: str, tf: int, candle_ts: datetime) -> None:
        tfcfg = self.cfg.for_tf(tf)
        opt = await self.selector.select(side, tfcfg)
        qty = tfcfg.lots * opt.lot_size
        cost = qty * opt.ltp
        if cost > self.cfg.max_risk_capital_per_trade:
            self.log("risk", f"Note: entry cost ₹{cost:,.0f} exceeds the "
                             f"₹{self.cfg.max_risk_capital_per_trade:,.0f} risk allocation "
                             f"(premium {opt.ltp:.2f} × {qty})", tf=tf)
        limit = round(opt.ltp + tfcfg.entry_limit_buffer, 2)
        order = await self.broker().place(
            instrument_key=opt.instrument_key, side="BUY",
            order_type="LIMIT", qty=qty, price=limit)
        self.pendings[tf] = {
            "option": asdict(opt), "order": order, "qty": qty, "tf": tf,
            "signal_candle": candle_ts.strftime("%H:%M"),
            "deadline": time.time() + tfcfg.entry_fill_timeout_sec,
        }
        self.log("order", f"BUY LIMIT {qty} × {opt.trading_symbol} @ ₹{limit:.2f} "
                          f"(LTP {opt.ltp:.2f}, strike {opt.strike:.0f}, "
                          f"delta {opt.delta if opt.delta is not None else 'n/a'}, "
                          f"expiry {opt.expiry}) [{self.cfg.mode.upper()}]", tf=tf)

    async def _manage_pending(self, tf: int, p: dict, now: datetime,
                              opt_ltp: float | None) -> None:
        order: BrokerOrder = await self.broker().poll(p["order"], opt_ltp)
        if order.status == FILLED:
            self.pendings.pop(tf, None)
            self._open_position(p, order.avg_price, order.qty, now)
        elif order.status == REJECTED:
            self.pendings.pop(tf, None)
            self.log("order", f"Entry rejected by broker: {order.order_id}", tf=tf)
        elif order.status == OPEN and time.time() > p["deadline"]:
            try:
                await self.broker().cancel(order)
            except UpstoxError as e:
                self.log("error", f"Cancel failed ({e}); rechecking order", tf=tf)
                return
            # a live order may have partially filled before the cancel landed
            filled = order.filled_qty
            self.pendings.pop(tf, None)
            if filled > 0 and order.avg_price > 0:
                self.log("order", f"Entry partially filled ({filled}/{order.qty}) before "
                                  f"timeout cancel — managing the filled quantity", tf=tf)
                self._open_position(p, order.avg_price, filled, now)
            else:
                self.log("order", "Entry not filled within timeout — cancelled "
                                  "(setup skipped)", tf=tf)

    def _open_position(self, p: dict, entry: float, qty: int, now: datetime) -> None:
        self.risk.record_entry()
        tfcfg = self.cfg.for_tf(p["tf"])
        self.positions[p["tf"]] = pos = {
            "option": p["option"], "qty": qty, "tf": p["tf"],
            "signal_candle": p["signal_candle"],
            "entry_price": entry, "entry_time": now.strftime("%H:%M:%S"),
            "target": round(entry + tfcfg.target_points, 2),
            "stoploss": round(entry - tfcfg.stoploss_points, 2),
            "high": entry, "low": entry, "trail_stop": None,
            "ltp": entry, "exit_order": None, "exit_reason": None,
        }
        self._save_positions()
        self.log("trade", f"ENTERED {p['option']['trading_symbol']} — {qty} qty "
                          f"@ ₹{entry:.2f} | target ₹{pos['target']:.2f} "
                          f"| stop ₹{pos['stoploss']:.2f} "
                          f"(trade {self.risk.trades_taken}/{self.cfg.max_trades_per_day})",
                 tf=p["tf"])

    # ---------------- exits ----------------

    def _effective_stop(self, pos: dict) -> tuple[float, str]:
        """Initial stop, or (in points mode) the trailed stop off the captured
        high once armed. Returns (stop_price, reason_if_hit)."""
        tfcfg = self.cfg.for_tf(pos["tf"])
        stop, reason = pos["stoploss"], "stoploss"
        if (tfcfg.trailing_stop and tfcfg.trail_mode == "points"
                and pos["high"] >= pos["entry_price"] + tfcfg.trail_activate_points):
            trail = round(pos["high"] - tfcfg.trail_gap_points, 2)
            if trail > stop:
                stop, reason = trail, "trailstop"
        pos["trail_stop"] = stop if reason == "trailstop" else None
        return stop, reason

    def _ema_touched(self, pos: dict) -> bool:
        """EMA-touch exit, close-confirmed: true only when the latest
        *completed* candle of the trade's timeframe CLOSED at/across its
        9-EMA (CE: close at/below, PE: close at/above). Ticks that pierce
        the EMA while a candle is still painting do not exit."""
        pos["ema_level"] = None
        tfcfg = self.cfg.for_tf(pos["tf"])
        if not (tfcfg.trailing_stop and tfcfg.trail_mode == "ema"):
            return False
        feed = self.feeds.get(pos["tf"])
        if feed is None or not feed.candles or not feed.ema or feed.ema[-1] is None:
            return False
        ema = feed.ema[-1]
        close = feed.candles[-1].close
        pos["ema_level"] = round(ema, 2)
        if pos["option"]["side"] == "CE":
            return close <= ema
        return close >= ema

    async def _manage_position(self, pos: dict, now: datetime, ltp: float | None) -> None:
        if ltp is not None and ltp > 0:  # capture the excursion since entry
            pos["high"] = max(pos["high"], ltp)
            pos["low"] = min(pos["low"], ltp)
        stop_price, stop_reason = self._effective_stop(pos)
        ema_touched = self._ema_touched(pos)
        exit_order: BrokerOrder | None = pos.get("exit_order")

        if exit_order is not None:
            exit_order = await self.broker().poll(exit_order, ltp)
            if exit_order.status == FILLED:
                self._finalize_trade(pos, exit_order.avg_price, now)
                return
            if exit_order.status in (REJECTED, CANCELLED):
                pos["exit_order"] = None  # will re-trigger below
            elif (exit_order.order_type == "LIMIT"
                  and ((ltp is not None and ltp <= stop_price) or ema_touched)):
                # resting target order while the exit condition hit: flip to MARKET
                try:
                    await self.broker().cancel(exit_order)
                except UpstoxError:
                    return
                pos["exit_order"] = None
                pos["exit_reason"] = ("ema_touch" if ema_touched
                                      and not (ltp is not None and ltp <= stop_price)
                                      else stop_reason)

        if pos.get("exit_order") is None:
            reason, order_type, price = pos.get("exit_reason"), None, 0.0
            if reason in ("stoploss", "trailstop", "ema_touch"):
                order_type = "MARKET"
            elif self._manual_squareoff:
                reason, order_type = "manual", "MARKET"
            elif self.risk.square_off_due(now, self.cfg) or not self.risk.market_hours(now):
                if not self.risk.market_hours(now) and self.cfg.mode == "live":
                    # a live MARKET order outside market hours only gets rejected
                    # over and over — warn once and stand down (Upstox RMS
                    # auto-squares intraday product at end of day)
                    if not pos.get("orphan_warned"):
                        pos["orphan_warned"] = True
                        self._save_positions()
                        self.log("error", f"Market closed with open live position "
                                          f"{pos['option']['trading_symbol']} — verify at the "
                                          f"broker (intraday product is RMS-squared)",
                                 tf=pos["tf"])
                    return
                reason, order_type = "squareoff", "MARKET"
            elif ltp is not None and ltp <= stop_price:
                reason, order_type = stop_reason, "MARKET"
            elif ema_touched:
                reason, order_type = "ema_touch", "MARKET"
            elif ltp is not None and ltp >= pos["target"]:
                reason, order_type, price = "target", "LIMIT", pos["target"]
            if order_type is None:
                return
            order = await self.broker().place(
                instrument_key=pos["option"]["instrument_key"], side="SELL",
                order_type=order_type, qty=pos["qty"], price=price)
            pos["exit_order"] = order
            pos["exit_reason"] = reason
            self._save_positions()
            self.log("order", f"EXIT ({reason}) SELL {order_type} {pos['qty']} × "
                              f"{pos['option']['trading_symbol']}"
                              + (f" @ ₹{price:.2f}" if order_type == "LIMIT" else ""),
                     tf=pos["tf"])
            # settle immediately when possible (paper MARKET fills at the
            # current LTP) so a reversal signal can take the slot this tick
            order = await self.broker().poll(order, ltp)
            if order.status == FILLED:
                self._finalize_trade(pos, order.avg_price, now)

    def _finalize_trade(self, pos: dict, exit_price: float, now: datetime) -> None:
        self.positions.pop(pos["tf"], None)
        self._save_positions()
        if not self.positions and not self.pendings:
            self._manual_squareoff = False
        pnl_points = exit_price - pos["entry_price"]
        gross = pnl_points * pos["qty"]
        charges = self.cfg.round_trip_charges
        reason = pos.get("exit_reason") or "exit"
        tfcfg = self.cfg.for_tf(pos["tf"])
        hit_target = reason == "target" or pnl_points >= tfcfg.target_points - 0.01
        self.risk.record_exit(pnl_points, gross, charges, hit_target)
        trade = {
            "day": self.risk.day, "mode": self.cfg.mode, "tf": f"{pos['tf']}m",
            "symbol": pos["option"]["trading_symbol"],
            "side": pos["option"]["side"], "strike": pos["option"]["strike"],
            "qty": pos["qty"], "entry_time": pos["entry_time"],
            "exit_time": now.strftime("%H:%M:%S"),
            "entry": round(pos["entry_price"], 2), "exit": round(exit_price, 2),
            "high": round(max(pos["high"], exit_price), 2),
            "low": round(min(pos["low"], exit_price), 2),
            "points": round(pnl_points, 2), "gross_rs": round(gross, 2),
            "charges_rs": round(charges, 2), "net_rs": round(gross - charges, 2),
            "reason": reason,
        }
        self._append_trade(trade)
        self.log("trade", f"EXITED {trade['symbol']} @ ₹{exit_price:.2f} "
                          f"({reason}) — {pnl_points:+.2f} pts, net ₹{trade['net_rs']:+,.2f}",
                 tf=pos["tf"])
        if hit_target and self.cfg.stop_after_target:
            self.log("risk", "Target achieved — terminal deactivated for the day")
        if self.risk.consecutive_losses >= self.cfg.max_consecutive_losses:
            self.log("risk", f"{self.risk.consecutive_losses} consecutive losing trades "
                             f"(limit {self.cfg.max_consecutive_losses}) — no further "
                             f"entries today")

    # ---------------- status ----------------

    def status(self) -> dict:
        now = datetime.now(TZ)
        positions = []
        for p in self.positions.values():
            unreal_pts = (p["ltp"] - p["entry_price"]) if p.get("ltp") else 0.0
            positions.append({
                "symbol": p["option"]["trading_symbol"], "side": p["option"]["side"],
                "strike": p["option"]["strike"], "expiry": p["option"]["expiry"],
                "qty": p["qty"], "tf": f"{p['tf']}m",
                "entry": round(p["entry_price"], 2), "ltp": round(p.get("ltp") or 0, 2),
                "target": p["target"], "stoploss": p["stoploss"],
                "high": round(p["high"], 2), "low": round(p["low"], 2),
                "trail_stop": p.get("trail_stop"),
                "ema_level": p.get("ema_level"),
                "entry_time": p["entry_time"],
                "unrealized_points": round(unreal_pts, 2),
                "unrealized_rs": round(unreal_pts * p["qty"], 2),
                "exiting": p.get("exit_reason"),
            })
        pendings = [{
            "symbol": q["option"]["trading_symbol"], "qty": q["qty"],
            "limit": q["order"].limit_price, "tf": f"{q['tf']}m",
            "seconds_left": max(0, int(q["deadline"] - time.time())),
        } for q in self.pendings.values()]
        realized_net = sum(t["net_rs"] for t in self.trades)
        return {
            "now_ist": now.strftime("%Y-%m-%d %H:%M:%S"),
            "running": self.running,
            "mode": self.cfg.mode,
            "spot": self.spot_ltp,
            "expiry": self.selector.expiry,
            "lot_size": self.selector.lot_size,
            "engines": {f"{tf}m": self.feeds[tf].snapshot()
                        for tf in self.cfg.timeframes if tf in self.feeds},
            "positions": positions,
            "pending_entries": pendings,
            "risk": self.risk.snapshot(now, self.cfg),
            "pnl": {"realized_net_rs": round(realized_net, 2),
                    "unrealized_rs": round(sum(p["unrealized_rs"] for p in positions), 2),
                    "capital": self.cfg.capital,
                    "equity": round(self.cfg.capital + realized_net, 2)},
            "trades": list(reversed(self.trades)),
            "events": list(self.events)[:60],
            "config": asdict(self.cfg),
        }
