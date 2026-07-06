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
from .risk import RiskManager
from .upstox_client import UpstoxClient, UpstoxError

TZ = ZoneInfo(IST)
TRADES_FILE = DATA_DIR / "trades.jsonl"


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
        self.pending: dict | None = None      # entry order in flight
        self.position: dict | None = None     # open long option position
        self._manual_squareoff = False
        self._handled_candle: dict[int, str] = {}

        self.events: deque[dict] = deque(maxlen=200)
        self.trades: list[dict] = self._load_today_trades()
        self._last_error: str = ""
        self._task: asyncio.Task | None = None

    # ---------------- lifecycle ----------------

    def broker(self):
        return LiveBroker(self.client) if self.cfg.mode == "live" else self.paper_broker

    def _sync_feeds(self) -> None:
        for tf in self.cfg.timeframes:
            if tf not in self.feeds:
                self.feeds[tf] = SpotFeed(self.client, tf, self.cfg.ema_period)
            self.feeds[tf].ema_period = self.cfg.ema_period
        for tf in list(self.feeds):
            if tf not in self.cfg.timeframes:
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
        if self.position or self.pending:
            raise UpstoxError("Cannot switch mode with an open or pending position")
        self.cfg.mode = mode
        self.cfg.validate()
        save_strategy_config(self.cfg)
        self.log("engine", f"Mode switched to {mode.upper()}")

    def ensure_loop(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    # ---------------- logging / persistence ----------------

    def log(self, kind: str, msg: str) -> None:
        now = datetime.now(TZ)
        self.events.appendleft({"ts": now.strftime("%H:%M:%S"), "kind": kind, "msg": msg})
        try:  # persistent copy, one file per day
            with (DATA_DIR / f"events-{now:%Y-%m-%d}.log").open("a", encoding="utf-8") as f:
                f.write(f"{now:%H:%M:%S} [{kind}] {msg}\n")
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
        if msg != self._last_error:
            self._last_error = msg
            self.log("error", msg)

    async def _tick(self) -> None:
        now = datetime.now(TZ)
        self.risk.roll_day()
        self._sync_feeds()
        if not self.client.access_token:
            return

        # --- live prices (real Upstox LTP; one call for spot + active option) ---
        keys = [SPOT_INSTRUMENT_KEY]
        active = self.position or self.pending
        if active:
            keys.append(active["option"]["instrument_key"])
        ltp_map = await self.client.ltp(keys)
        self.spot_ltp = ltp_map.get(SPOT_INSTRUMENT_KEY, self.spot_ltp)
        opt_ltp = ltp_map.get(active["option"]["instrument_key"]) if active else None
        if self.position is not None and opt_ltp:
            self.position["ltp"] = opt_ltp

        # --- manage in-flight entry order ---
        if self.pending:
            await self._manage_pending(now, opt_ltp)

        # --- manage open position (runs even when engine is stopped) ---
        if self.position:
            await self._manage_position(now, opt_ltp)

        # --- refresh candles + look for signals ---
        if self.risk.market_hours(now) or not self.feeds[self.cfg.timeframes[0]].candles:
            for tf in list(self.cfg.timeframes):
                feed = self.feeds[tf]
                new = await feed.refresh()
                if new and self.running and not self.position and not self.pending:
                    await self._check_signal(now, feed, tf)

    async def _check_signal(self, now: datetime, feed: SpotFeed, tf: int) -> None:
        signal, flat = feed.evaluate_signal(self.cfg)
        if signal is None:
            return
        key = signal.candle_ts.isoformat()
        if self._handled_candle.get(tf) == key:
            return
        self._handled_candle[tf] = key

        label = (f"{tf}m candle {signal.candle_ts.strftime('%H:%M')} closed "
                 f"{'above' if signal.side == 'CE' else 'below'} 9-EMA "
                 f"(close {signal.close:.2f} / EMA {signal.ema:.2f})")
        if flat:
            self.log("signal", f"{label} — SKIPPED: flat 9-EMA (institutional block filter)")
            return
        reason = self.risk.entry_gate(now, self.cfg)
        if reason:
            self.log("signal", f"{label} — SKIPPED: {reason}")
            return

        self.log("signal", f"{label} — {signal.side} entry trigger")
        await self._enter(signal.side, tf, signal.candle_ts)

    # ---------------- entries ----------------

    async def _enter(self, side: str, tf: int, candle_ts: datetime) -> None:
        opt = await self.selector.select(side, self.cfg)
        qty = self.cfg.lots * opt.lot_size
        cost = qty * opt.ltp
        if cost > self.cfg.max_risk_capital_per_trade:
            self.log("risk", f"Note: entry cost ₹{cost:,.0f} exceeds the "
                             f"₹{self.cfg.max_risk_capital_per_trade:,.0f} risk allocation "
                             f"(premium {opt.ltp:.2f} × {qty})")
        limit = round(opt.ltp + self.cfg.entry_limit_buffer, 2)
        order = await self.broker().place(
            instrument_key=opt.instrument_key, side="BUY",
            order_type="LIMIT", qty=qty, price=limit)
        self.pending = {
            "option": asdict(opt), "order": order, "qty": qty, "tf": tf,
            "signal_candle": candle_ts.strftime("%H:%M"),
            "deadline": time.time() + self.cfg.entry_fill_timeout_sec,
        }
        self.log("order", f"BUY LIMIT {qty} × {opt.trading_symbol} @ ₹{limit:.2f} "
                          f"(LTP {opt.ltp:.2f}, strike {opt.strike:.0f}, "
                          f"delta {opt.delta if opt.delta is not None else 'n/a'}, "
                          f"expiry {opt.expiry}) [{self.cfg.mode.upper()}]")

    async def _manage_pending(self, now: datetime, opt_ltp: float | None) -> None:
        p = self.pending
        order: BrokerOrder = await self.broker().poll(p["order"], opt_ltp)
        if order.status == FILLED:
            self.pending = None
            self._open_position(p, order.avg_price, order.qty, now)
        elif order.status == REJECTED:
            self.pending = None
            self.log("order", f"Entry rejected by broker: {order.order_id}")
        elif order.status == OPEN and time.time() > p["deadline"]:
            try:
                await self.broker().cancel(order)
            except UpstoxError as e:
                self.log("error", f"Cancel failed ({e}); rechecking order")
                return
            # a live order may have partially filled before the cancel landed
            filled = order.filled_qty
            self.pending = None
            if filled > 0 and order.avg_price > 0:
                self.log("order", f"Entry partially filled ({filled}/{order.qty}) before "
                                  f"timeout cancel — managing the filled quantity")
                self._open_position(p, order.avg_price, filled, now)
            else:
                self.log("order", "Entry not filled within timeout — cancelled (setup skipped)")

    def _open_position(self, p: dict, entry: float, qty: int, now: datetime) -> None:
        self.risk.record_entry()
        self.position = {
            "option": p["option"], "qty": qty, "tf": p["tf"],
            "signal_candle": p["signal_candle"],
            "entry_price": entry, "entry_time": now.strftime("%H:%M:%S"),
            "target": round(entry + self.cfg.target_points, 2),
            "stoploss": round(entry - self.cfg.stoploss_points, 2),
            "high": entry, "low": entry, "trail_stop": None,
            "ltp": entry, "exit_order": None, "exit_reason": None,
        }
        self._manual_squareoff = False
        self.log("trade", f"ENTERED {p['option']['trading_symbol']} — {qty} qty @ ₹{entry:.2f} "
                          f"| target ₹{self.position['target']:.2f} "
                          f"| stop ₹{self.position['stoploss']:.2f} "
                          f"(trade {self.risk.trades_taken}/{self.cfg.max_trades_per_day})")

    # ---------------- exits ----------------

    def _effective_stop(self, pos: dict) -> tuple[float, str]:
        """Initial stop, or the trailed stop off the captured high once armed.
        Returns (stop_price, reason_if_hit)."""
        stop, reason = pos["stoploss"], "stoploss"
        if (self.cfg.trailing_stop
                and pos["high"] >= pos["entry_price"] + self.cfg.trail_activate_points):
            trail = round(pos["high"] - self.cfg.trail_gap_points, 2)
            if trail > stop:
                stop, reason = trail, "trailstop"
        pos["trail_stop"] = stop if reason == "trailstop" else None
        return stop, reason

    async def _manage_position(self, now: datetime, ltp: float | None) -> None:
        pos = self.position
        if ltp is not None and ltp > 0:  # capture the excursion since entry
            pos["high"] = max(pos["high"], ltp)
            pos["low"] = min(pos["low"], ltp)
        stop_price, stop_reason = self._effective_stop(pos)
        exit_order: BrokerOrder | None = pos.get("exit_order")

        if exit_order is not None:
            exit_order = await self.broker().poll(exit_order, ltp)
            if exit_order.status == FILLED:
                self._finalize_trade(exit_order.avg_price, now)
                return
            if exit_order.status in (REJECTED, CANCELLED):
                pos["exit_order"] = None  # will re-trigger below
            elif (ltp is not None and exit_order.order_type == "LIMIT"
                  and ltp <= stop_price):
                # resting target order while price collapsed to the stop: flip to MARKET
                try:
                    await self.broker().cancel(exit_order)
                except UpstoxError:
                    return
                pos["exit_order"] = None
                pos["exit_reason"] = stop_reason

        if pos.get("exit_order") is None:
            reason, order_type, price = pos.get("exit_reason"), None, 0.0
            if reason in ("stoploss", "trailstop"):
                order_type = "MARKET"
            elif self._manual_squareoff:
                reason, order_type = "manual", "MARKET"
            elif self.risk.square_off_due(now, self.cfg) or not self.risk.market_hours(now):
                reason, order_type = "squareoff", "MARKET"
            elif ltp is not None and ltp <= stop_price:
                reason, order_type = stop_reason, "MARKET"
            elif ltp is not None and ltp >= pos["target"]:
                reason, order_type, price = "target", "LIMIT", pos["target"]
            if order_type is None:
                return
            order = await self.broker().place(
                instrument_key=pos["option"]["instrument_key"], side="SELL",
                order_type=order_type, qty=pos["qty"], price=price)
            pos["exit_order"] = order
            pos["exit_reason"] = reason
            self.log("order", f"EXIT ({reason}) SELL {order_type} {pos['qty']} × "
                              f"{pos['option']['trading_symbol']}"
                              + (f" @ ₹{price:.2f}" if order_type == "LIMIT" else ""))

    def _finalize_trade(self, exit_price: float, now: datetime) -> None:
        pos = self.position
        self.position = None
        self._manual_squareoff = False
        pnl_points = exit_price - pos["entry_price"]
        gross = pnl_points * pos["qty"]
        charges = self.cfg.round_trip_charges
        reason = pos.get("exit_reason") or "exit"
        hit_target = reason == "target" or pnl_points >= self.cfg.target_points - 0.01
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
        self.log("trade", f"EXITED {trade['symbol']} @ ₹{exit_price:.2f} ({reason}) — "
                          f"{pnl_points:+.2f} pts, net ₹{trade['net_rs']:+,.2f}")
        if hit_target and self.cfg.stop_after_target:
            self.log("risk", "Target achieved — terminal deactivated for the day")
        if self.risk.consecutive_losses >= self.cfg.max_consecutive_losses:
            self.log("risk", "Two consecutive stop-losses — operations terminated for the day")

    # ---------------- status ----------------

    def status(self) -> dict:
        now = datetime.now(TZ)
        pos = None
        if self.position:
            p = self.position
            unreal_pts = (p["ltp"] - p["entry_price"]) if p.get("ltp") else 0.0
            pos = {
                "symbol": p["option"]["trading_symbol"], "side": p["option"]["side"],
                "strike": p["option"]["strike"], "expiry": p["option"]["expiry"],
                "qty": p["qty"], "tf": f"{p['tf']}m",
                "entry": round(p["entry_price"], 2), "ltp": round(p.get("ltp") or 0, 2),
                "target": p["target"], "stoploss": p["stoploss"],
                "high": round(p["high"], 2), "low": round(p["low"], 2),
                "trail_stop": p.get("trail_stop"),
                "entry_time": p["entry_time"],
                "unrealized_points": round(unreal_pts, 2),
                "unrealized_rs": round(unreal_pts * p["qty"], 2),
                "exiting": p.get("exit_reason"),
            }
        pending = None
        if self.pending:
            q = self.pending
            pending = {
                "symbol": q["option"]["trading_symbol"], "qty": q["qty"],
                "limit": q["order"].limit_price, "tf": f"{q['tf']}m",
                "seconds_left": max(0, int(q["deadline"] - time.time())),
            }
        realized_net = sum(t["net_rs"] for t in self.trades)
        return {
            "now_ist": now.strftime("%Y-%m-%d %H:%M:%S"),
            "running": self.running,
            "mode": self.cfg.mode,
            "spot": self.spot_ltp,
            "expiry": self.selector.expiry,
            "lot_size": self.selector.lot_size,
            "engines": {f"{tf}m": self.feeds[tf].snapshot() for tf in self.cfg.timeframes},
            "position": pos,
            "pending_entry": pending,
            "risk": self.risk.snapshot(now, self.cfg),
            "pnl": {"realized_net_rs": round(realized_net, 2),
                    "capital": self.cfg.capital,
                    "equity": round(self.cfg.capital + realized_net, 2)},
            "trades": list(reversed(self.trades)),
            "events": list(self.events)[:60],
            "config": asdict(self.cfg),
        }
