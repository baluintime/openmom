"""Session risk overlays from the spec:

- Max 2 trade setups per day
- Target cutoff: stop for the day after the first target hit
- Double-loss cutoff: stop after 2 consecutive stop-losses
- Mid-day institutional block: no entries 10:30-13:00 IST
- Entry window: 09:15 to `no_entries_after`; forced square-off at `square_off_at`

State persists to disk so a restart mid-session cannot reset the day's limits.
"""
from __future__ import annotations

import json
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .config import DATA_DIR, IST, StrategyConfig

TZ = ZoneInfo(IST)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


class RiskManager:
    def __init__(self):
        self.day: str = ""
        self.trades_taken: int = 0
        self.consecutive_losses: int = 0
        self.target_hit: bool = False
        self.day_gross_rs: float = 0.0
        self.day_charges_rs: float = 0.0
        self._load()

    # ---------- persistence ----------

    def _file(self):
        return DATA_DIR / "risk_state.json"

    def _load(self) -> None:
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        if self._file().exists():
            try:
                d = json.loads(self._file().read_text())
                if d.get("day") == today:
                    self.day = d["day"]
                    self.trades_taken = d.get("trades_taken", 0)
                    self.consecutive_losses = d.get("consecutive_losses", 0)
                    self.target_hit = d.get("target_hit", False)
                    self.day_gross_rs = d.get("day_gross_rs", 0.0)
                    self.day_charges_rs = d.get("day_charges_rs", 0.0)
                    return
            except Exception:
                pass
        self.day = today

    def _save(self) -> None:
        self._file().write_text(json.dumps({
            "day": self.day, "trades_taken": self.trades_taken,
            "consecutive_losses": self.consecutive_losses, "target_hit": self.target_hit,
            "day_gross_rs": self.day_gross_rs, "day_charges_rs": self.day_charges_rs,
        }))

    def roll_day(self) -> None:
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        if self.day != today:
            self.day = today
            self.trades_taken = 0
            self.consecutive_losses = 0
            self.target_hit = False
            self.day_gross_rs = 0.0
            self.day_charges_rs = 0.0
            self._save()

    # ---------- gates ----------

    @staticmethod
    def market_hours(now: datetime) -> bool:
        return now.weekday() < 5 and MARKET_OPEN <= now.time() < MARKET_CLOSE

    def midday_blocked(self, now: datetime, cfg: StrategyConfig) -> bool:
        if not cfg.midday_block:
            return False
        return _parse_hhmm(cfg.midday_block_start) <= now.time() < _parse_hhmm(cfg.midday_block_end)

    def entry_gate(self, now: datetime, cfg: StrategyConfig) -> str | None:
        """None if a new entry is allowed, else a human-readable block reason."""
        self.roll_day()
        if not self.market_hours(now):
            return "Market closed"
        if now.time() >= _parse_hhmm(cfg.no_entries_after):
            return f"No new entries after {cfg.no_entries_after}"
        if self.midday_blocked(now, cfg):
            return f"Mid-day liquidity block ({cfg.midday_block_start}-{cfg.midday_block_end})"
        if self.trades_taken >= cfg.max_trades_per_day:
            return f"Daily trade limit reached ({cfg.max_trades_per_day})"
        if cfg.stop_after_target and self.target_hit:
            return "Target hit — terminal deactivated for the day"
        if self.consecutive_losses >= cfg.max_consecutive_losses:
            return f"{cfg.max_consecutive_losses} consecutive stop-losses — done for the day"
        return None

    def square_off_due(self, now: datetime, cfg: StrategyConfig) -> bool:
        return now.time() >= _parse_hhmm(cfg.square_off_at)

    # ---------- results ----------

    def record_entry(self) -> None:
        self.roll_day()
        self.trades_taken += 1
        self._save()

    def record_exit(self, pnl_points: float, gross_rs: float, charges_rs: float,
                    hit_target: bool) -> None:
        self.day_gross_rs += gross_rs
        self.day_charges_rs += charges_rs
        if pnl_points < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        if hit_target:
            self.target_hit = True
        self._save()

    def snapshot(self, now: datetime, cfg: StrategyConfig) -> dict:
        self.roll_day()
        return {
            "day": self.day,
            "trades_taken": self.trades_taken,
            "max_trades": cfg.max_trades_per_day,
            "consecutive_losses": self.consecutive_losses,
            "target_hit": self.target_hit,
            "midday_block_active": self.midday_blocked(now, cfg),
            "market_open": self.market_hours(now),
            "entry_block_reason": self.entry_gate(now, cfg),
            "day_gross_rs": round(self.day_gross_rs, 2),
            "day_charges_rs": round(self.day_charges_rs, 2),
            "day_net_rs": round(self.day_gross_rs - self.day_charges_rs, 2),
        }
