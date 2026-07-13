"""Backtester: replays the exact live strategy rules over *real* Upstox
historical data — no synthetic prices anywhere.

Data sources (all authenticated Upstox APIs):
  - NIFTY 50 spot candles  -> v3 historical-candle (1-min / 5-min)
  - Option premiums        -> 1-min candles of the actual contract that the
    selection rules would have bought: expired-instruments API for contracts
    whose expiry has passed, regular historical-candle API otherwise.

Honest approximations (tick data is not available historically):
  - Entries fill at the OPEN of the candle following the signal candle
    (the live engine enters "on the opening tick of the succeeding candle").
  - Within a candle the stop-loss is checked BEFORE the target (conservative:
    if both were touched in the same minute, the backtest books the loss).
  - The delta filter cannot be applied (historical greeks are unavailable);
    contract selection uses the premium band + ATM/ITM rules, which is the
    live engine's primary criterion.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import IST, SPOT_INSTRUMENT_KEY, StrategyConfig
from .market import Candle, ema_series
from .risk import _parse_hhmm
from .upstox_client import UpstoxClient, UpstoxError

TZ = ZoneInfo(IST)
SESSION_START = time(9, 15)


class Backtester:
    def __init__(self, client: UpstoxClient):
        self.client = client
        self.state: dict = {"state": "idle", "pct": 0, "message": "", "result": None}
        self._task: asyncio.Task | None = None
        # per-run caches
        self._expiries: list[str] = []
        self._active_contracts: list[dict] = []
        self._contracts: dict[str, list[dict]] = {}
        self._opt_candles: dict[tuple[str, str], list[Candle]] = {}

    # ---------------- control ----------------

    def start(self, cfg: StrategyConfig, from_date: str, to_date: str,
              timeframes: list[int]) -> None:
        if self.state["state"] == "running":
            raise UpstoxError("A backtest is already running")
        if not self.client.access_token:
            raise UpstoxError("Connect to Upstox before running a backtest")
        frm, to = date.fromisoformat(from_date), date.fromisoformat(to_date)
        yesterday = datetime.now(TZ).date() - timedelta(days=1)
        to = min(to, yesterday)  # completed sessions only
        if frm > to:
            raise UpstoxError("Invalid date range (backtests cover completed sessions, up to yesterday)")
        if (to - frm).days > 95:
            raise UpstoxError("Backtest range is limited to ~3 months")
        tfs = sorted({int(t) for t in timeframes})
        if not tfs or any(t not in (1, 5) for t in tfs):
            raise UpstoxError("timeframes must be a subset of [1, 5]")
        cfg = replace(cfg)  # snapshot so live config edits don't affect the run
        self.state = {"state": "running", "pct": 0, "message": "Starting…", "result": None}
        self._task = asyncio.create_task(self._run(cfg, frm, to, tfs))

    def status(self) -> dict:
        return self.state

    # ---------------- run ----------------

    async def _run(self, cfg: StrategyConfig, frm: date, to: date, tfs: list[int]) -> None:
        try:
            self._expiries, self._active_contracts = [], []
            self._contracts, self._opt_candles = {}, {}
            result = await self._backtest(cfg, frm, to, tfs)
            self.state = {"state": "done", "pct": 100, "message": "Done", "result": result}
        except UpstoxError as e:
            self.state = {"state": "error", "pct": 0, "message": str(e), "result": None}
        except Exception as e:
            self.state = {"state": "error", "pct": 0,
                          "message": f"{type(e).__name__}: {e}", "result": None}

    def _progress(self, pct: float, msg: str) -> None:
        self.state["pct"] = round(min(99, pct), 1)
        self.state["message"] = msg

    async def _backtest(self, cfg: StrategyConfig, frm: date, to: date,
                        tfs: list[int]) -> dict:
        self._progress(1, "Loading option expiries…")
        await self._load_expiries()

        per_tf: dict[str, dict] = {}
        total_steps = len(tfs)
        for n, tf in enumerate(tfs):
            self._progress(2 + n * 96 / total_steps, f"Fetching NIFTY spot {tf}-min candles…")
            spot = await self._fetch_spot(tf, frm - timedelta(days=10), to)
            days = sorted({c.ts.date() for c in spot if frm <= c.ts.date() <= to})
            per_tf[f"{tf}m"] = await self._simulate_tf(
                cfg, tf, spot, days,
                base_pct=2 + n * 96 / total_steps, span_pct=96 / total_steps)

        return {
            "from": frm.isoformat(), "to": to.isoformat(),
            "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "params": {
                "ema_period": cfg.ema_period, "decisive_points": cfg.decisive_points,
                "target_points": cfg.target_points, "stoploss_points": cfg.stoploss_points,
                "trailing_stop": (cfg.trail_mode if cfg.trailing_stop else None),
                "trail_points": [cfg.trail_activate_points, cfg.trail_gap_points],
                "lots": cfg.lots, "premium_band": [cfg.premium_band_low, cfg.premium_band_high],
                "max_trades_per_day": cfg.max_trades_per_day,
                "stop_after_target": cfg.stop_after_target,
                "max_consecutive_losses": cfg.max_consecutive_losses,
                "midday_block": [cfg.midday_block_start, cfg.midday_block_end]
                                 if cfg.midday_block else None,
                "flat_ema_filter": cfg.flat_ema_filter,
                "skip_first_cross": cfg.skip_first_cross,
                "round_trip_charges": cfg.round_trip_charges,
            },
            "assumptions": [
                "Entries fill at the open of the candle after the signal candle",
                "Stop-loss checked before target within each 1-min candle (conservative)",
                "EMA-touch exit is close-confirmed (completed candle close vs EMA, wicks ignored), exiting at the next minute's open",
                "Points-mode trailing steps at candle granularity, armed only by prior candles' highs",
                "Delta filter not applied (historical greeks unavailable); premium-band + ATM/ITM selection",
                f"₹{cfg.round_trip_charges:.0f} charges per round trip",
                "Timeframes are simulated independently (matches per-timeframe position slots; "
                "single-slot cross-timeframe blocking is not modeled)",
            ],
            "timeframes": per_tf,
        }

    # ---------------- data loading ----------------

    async def _load_expiries(self) -> None:
        expiries: set[str] = set()
        try:
            data = await self.client.expired_expiries(SPOT_INSTRUMENT_KEY)
            expiries |= {str(e) for e in data}
        except UpstoxError:
            pass  # expired-instruments access may be disabled on the account
        try:
            self._active_contracts = await self.client.option_contracts(SPOT_INSTRUMENT_KEY)
            expiries |= {c["expiry"] for c in self._active_contracts}
        except UpstoxError:
            self._active_contracts = []
        if not expiries:
            raise UpstoxError("Could not load any NIFTY option expiries from Upstox")
        self._expiries = sorted(expiries)

    async def _fetch_spot(self, tf: int, frm: date, to: date) -> list[Candle]:
        out: dict[datetime, Candle] = {}
        cur = frm
        while cur <= to:
            chunk_end = min(cur + timedelta(days=24), to)
            rows = await self.client.historical_candles(
                SPOT_INSTRUMENT_KEY, tf, cur.isoformat(), chunk_end.isoformat())
            for r in rows:
                c = Candle.from_api(r)
                out[c.ts] = c
            cur = chunk_end + timedelta(days=1)
        candles = sorted(out.values(), key=lambda c: c.ts)
        if not candles:
            raise UpstoxError("Upstox returned no spot candles for the requested range")
        return candles

    async def _contracts_for_expiry(self, expiry: str) -> list[dict]:
        if expiry in self._contracts:
            return self._contracts[expiry]
        today = datetime.now(TZ).date().isoformat()
        if expiry < today:
            rows = await self.client.expired_option_contracts(SPOT_INSTRUMENT_KEY, expiry)
        else:
            rows = [c for c in self._active_contracts if c["expiry"] == expiry]
        self._contracts[expiry] = rows
        return rows

    async def _option_day_candles(self, contract: dict, day: str) -> list[Candle]:
        key = (contract["instrument_key"], day)
        if key in self._opt_candles:
            return self._opt_candles[key]
        today = datetime.now(TZ).date().isoformat()
        try:
            if contract["expiry"] < today:
                rows = await self.client.expired_historical_candles(
                    contract["instrument_key"], "1minute", day, day)
            else:
                rows = await self.client.historical_candles(
                    contract["instrument_key"], 1, day, day)
        except UpstoxError:
            rows = []
        candles = sorted((Candle.from_api(r) for r in rows), key=lambda c: c.ts)
        self._opt_candles[key] = candles
        await asyncio.sleep(0.12)  # stay well inside Upstox rate limits
        return candles

    # ---------------- simulation ----------------

    async def _simulate_tf(self, cfg: StrategyConfig, tf: int, spot: list[Candle],
                           days: list[date], base_pct: float, span_pct: float) -> dict:
        closes = [c.close for c in spot]
        ema = ema_series(closes, cfg.ema_period)
        day_indexes: dict[date, list[int]] = {}
        for i, c in enumerate(spot):
            day_indexes.setdefault(c.ts.date(), []).append(i)

        trades: list[dict] = []
        skipped: list[str] = []
        no_after = _parse_hhmm(cfg.no_entries_after)
        blk_a, blk_b = _parse_hhmm(cfg.midday_block_start), _parse_hhmm(cfg.midday_block_end)

        for dn, day in enumerate(days):
            self._progress(base_pct + span_pct * dn / max(1, len(days)),
                           f"Backtesting {tf}-min — {day.isoformat()}")
            idxs = day_indexes.get(day, [])
            trades_today = consec_sl = 0
            crosses_today = 0
            target_hit = False
            busy_until: datetime | None = None

            for i in idxs:
                if i < 1:
                    continue
                prev_e, cur_e = ema[i - 1], ema[i]
                if prev_e is None or cur_e is None:
                    continue
                prev_c, cur_c = closes[i - 1], closes[i]
                is_cross = ((prev_c <= prev_e and cur_c > cur_e)
                            or (prev_c >= prev_e and cur_c < cur_e))
                prior_crosses = crosses_today
                if is_cross:
                    crosses_today += 1
                if i + 1 >= len(spot) or spot[i + 1].ts.date() != day:
                    continue  # signal on the last candle of the day: no next-candle entry
                side = None
                if prev_c <= prev_e and cur_c > cur_e + cfg.decisive_points:
                    side = "CE"
                elif prev_c >= prev_e and cur_c < cur_e - cfg.decisive_points:
                    side = "PE"
                if side is None:
                    continue
                if (cfg.skip_first_cross and prior_crosses == 0
                        and spot[i].ts.time() < _parse_hhmm(cfg.skip_first_cross_before)):
                    continue  # gap-settling: an early first cross is not traded

                entry_ts = spot[i + 1].ts
                # --- risk gates (identical to the live engine) ---
                if busy_until is not None and entry_ts < busy_until:
                    continue
                if trades_today >= cfg.max_trades_per_day:
                    continue
                if cfg.stop_after_target and target_hit:
                    continue
                if consec_sl >= cfg.max_consecutive_losses:
                    continue
                t = entry_ts.time()
                if t < SESSION_START or t >= no_after:
                    continue
                if cfg.midday_block and blk_a <= t < blk_b:
                    continue
                if cfg.flat_ema_filter:
                    # threshold defined on the 5-min chart, scaled per timeframe
                    ref = ema[i - cfg.flat_ema_lookback] if i - cfg.flat_ema_lookback >= 0 else None
                    if ref is not None and abs(cur_e - ref) < cfg.flat_ema_points * tf / 5.0:
                        continue

                trade = await self._simulate_trade(cfg, tf, side, day, entry_ts, cur_c,
                                                   skipped, spot, ema, i + 1)
                if trade is None:
                    continue
                trades_today += 1
                busy_until = trade.pop("_exit_ts")
                trades.append(trade)
                if trade["points"] < 0:
                    consec_sl += 1
                else:
                    consec_sl = 0
                if trade["reason"] == "target" or trade["points"] >= cfg.target_points - 0.01:
                    target_hit = True

        return self._summarize(trades, skipped, days)

    async def _simulate_trade(self, cfg: StrategyConfig, tf: int, side: str, day: date,
                              entry_ts: datetime, spot_close: float, skipped: list[str],
                              spot: list, ema: list, entry_idx: int) -> dict | None:
        day_s = day.isoformat()
        expiry = next((e for e in self._expiries if e >= day_s), None)
        if expiry is None:
            skipped.append(f"{day_s} {entry_ts.strftime('%H:%M')}: no expiry on record ≥ trade date")
            return None
        contracts = await self._contracts_for_expiry(expiry)
        legs = [c for c in contracts if c.get("instrument_type") == side]
        strikes = sorted({float(c["strike_price"]) for c in legs})
        if not strikes:
            skipped.append(f"{day_s} {entry_ts.strftime('%H:%M')}: no {side} contracts for expiry {expiry}")
            return None
        atm = min(strikes, key=lambda s: abs(s - spot_close))
        step = min((b - a for a, b in zip(strikes, strikes[1:])), default=50.0)
        if side == "CE":
            allowed = [atm - i * step for i in range(cfg.max_itm_strikes + 1)]
        else:
            allowed = [atm + i * step for i in range(cfg.max_itm_strikes + 1)]
        by_strike = {float(c["strike_price"]): c for c in legs}

        # candidate = (contract, its 1-min candles for the day, entry candle index)
        candidates = []
        for strike in allowed:
            contract = by_strike.get(strike)
            if contract is None:
                continue
            candles = await self._option_day_candles(contract, day_s)
            k = next((j for j, c in enumerate(candles)
                      if entry_ts <= c.ts <= entry_ts + timedelta(minutes=3)), None)
            if k is None:
                continue
            candidates.append((contract, candles, k, candles[k].open))
        if not candidates:
            skipped.append(f"{day_s} {entry_ts.strftime('%H:%M')}: no option premium data "
                           f"near ATM {atm:.0f} {side} (expiry {expiry})")
            return None

        mid = (cfg.premium_band_low + cfg.premium_band_high) / 2.0
        in_band = [c for c in candidates
                   if cfg.premium_band_low <= c[3] <= cfg.premium_band_high]
        if in_band:
            contract, candles, k, entry = min(in_band, key=lambda c: abs(c[3] - mid))
        else:  # nearest to ATM, mirroring the live fallback
            contract, candles, k, entry = min(
                candidates, key=lambda c: abs(float(c[0]["strike_price"]) - atm))

        target = entry + cfg.target_points
        init_sl = entry - cfg.stoploss_points
        square_ts = datetime.combine(day, _parse_hhmm(cfg.square_off_at), tzinfo=TZ)

        # In "ema" trail mode the exit fires when a completed spot candle of the
        # signal timeframe CLOSES at/across its 9-EMA (close-confirmed — an
        # intra-candle wick through the EMA does not exit, matching the live
        # engine); the position exits at the open of the next option minute.
        # In "points"
        # mode eff_sl trails the captured high, recomputed only from *prior*
        # candles' highs (intra-candle ordering is unknowable -> conservative).
        ema_trail = cfg.trailing_stop and cfg.trail_mode == "ema"
        points_trail = cfg.trailing_stop and cfg.trail_mode == "points"
        tf_delta = timedelta(minutes=tf)
        spot_j = entry_idx
        ema_touch_from: datetime | None = None
        high = low = entry
        eff_sl = init_sl
        exit_price = exit_ts = None
        reason = "eod"
        for c in candles[k:]:
            if c.ts >= square_ts:
                exit_price, exit_ts, reason = c.open, c.ts, "squareoff"
                break
            if ema_trail and ema_touch_from is None:
                # consider spot candles completed by this option minute
                while (spot_j < len(spot) and spot[spot_j].ts.date() == day
                       and spot[spot_j].ts + tf_delta <= c.ts):
                    se = ema[spot_j]
                    sc = spot[spot_j]
                    if se is not None and ((side == "CE" and sc.close <= se)
                                           or (side == "PE" and sc.close >= se)):
                        ema_touch_from = sc.ts + tf_delta
                        break
                    spot_j += 1
            if ema_touch_from is not None and c.ts >= ema_touch_from:
                exit_price, exit_ts, reason = c.open, c.ts, "ema_touch"
                break
            if c.low <= eff_sl:        # conservative: stop before target in-candle
                exit_price, exit_ts = eff_sl, c.ts
                reason = "trailstop" if eff_sl > init_sl else "stoploss"
                break
            if c.high >= target:
                exit_price, exit_ts, reason = target, c.ts, "target"
                break
            high, low = max(high, c.high), min(low, c.low)
            if points_trail and high >= entry + cfg.trail_activate_points:
                eff_sl = max(eff_sl, round(high - cfg.trail_gap_points, 2))
        if exit_price is None:
            last = candles[-1]
            exit_price, exit_ts = last.close, last.ts
        high, low = max(high, exit_price), min(low, exit_price)

        lot_size = int(contract.get("lot_size") or 75)
        qty = cfg.lots * lot_size
        points = exit_price - entry
        gross = points * qty
        return {
            "_exit_ts": exit_ts,
            "day": day_s, "tf": f"{tf}m", "side": side,
            "symbol": contract.get("trading_symbol", contract["instrument_key"]),
            "strike": float(contract["strike_price"]), "expiry": expiry,
            "qty": qty,
            "entry_time": entry_ts.strftime("%H:%M"),
            "exit_time": exit_ts.strftime("%H:%M"),
            "entry": round(entry, 2), "exit": round(exit_price, 2),
            "high": round(high, 2), "low": round(low, 2),
            "points": round(points, 2),
            "gross_rs": round(gross, 2),
            "charges_rs": round(cfg.round_trip_charges, 2),
            "net_rs": round(gross - cfg.round_trip_charges, 2),
            "reason": reason,
        }

    # ---------------- reporting ----------------

    @staticmethod
    def _summarize(trades: list[dict], skipped: list[str], days: list[date]) -> dict:
        daily: dict[str, dict] = {}
        for t in trades:
            d = daily.setdefault(t["day"], {"day": t["day"], "trades": 0, "net_rs": 0.0})
            d["trades"] += 1
            d["net_rs"] = round(d["net_rs"] + t["net_rs"], 2)
        wins = [t for t in trades if t["net_rs"] > 0]
        reasons = {}
        for t in trades:
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
        daily_list = sorted(daily.values(), key=lambda d: d["day"])
        return {
            "summary": {
                "sessions": len(days),
                "trades": len(trades),
                "wins": len(wins),
                "losses": len(trades) - len(wins),
                "win_rate": round(100 * len(wins) / len(trades), 1) if trades else 0.0,
                "points": round(sum(t["points"] for t in trades), 2),
                "gross_rs": round(sum(t["gross_rs"] for t in trades), 2),
                "charges_rs": round(sum(t["charges_rs"] for t in trades), 2),
                "net_rs": round(sum(t["net_rs"] for t in trades), 2),
                "exit_reasons": reasons,
                "best_day": max(daily_list, key=lambda d: d["net_rs"], default=None),
                "worst_day": min(daily_list, key=lambda d: d["net_rs"], default=None),
            },
            "daily": daily_list,
            "trades": trades,
            "skipped": skipped,
        }
