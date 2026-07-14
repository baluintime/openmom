"""Configuration: environment credentials + runtime strategy parameters.

Strategy defaults implement the "Nifty Intraday Options Scalping" spec:
9-EMA close-confirmation on the NIFTY 50 spot chart, +4 / -3 premium point
exits, 2 lots, max 2 trades/day, mid-day liquidity block, flat-EMA filter.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"
TOKEN_FILE = DATA_DIR / "token.json"

SPOT_INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"
IST = "Asia/Kolkata"

# Strategy parameters that may differ between the 1-min and 5-min engines.
# Everything else (mode, capital, session limits, mid-day block, warm-up,
# square-off, charges) is session-level and shared.
PER_TF_FIELDS = frozenset({
    "ema_period", "decisive_points",
    "flat_ema_filter", "flat_ema_points", "flat_ema_lookback",
    "lots", "premium_band_low", "premium_band_high",
    "delta_low", "delta_high", "max_itm_strikes",
    "target_points", "stoploss_points",
    "trailing_stop", "trail_mode", "trail_activate_points", "trail_gap_points",
    "entry_limit_buffer", "entry_fill_timeout_sec",
})


@dataclass
class EnvSettings:
    api_key: str = os.getenv("UPSTOX_API_KEY", "")
    api_secret: str = os.getenv("UPSTOX_API_SECRET", "")
    redirect_uri: str = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:8000/api/auth/callback")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))


@dataclass
class StrategyConfig:
    # Execution mode: "paper" or "live"
    mode: str = "paper"
    # Timeframes the signal engine runs on (minutes). Both supported.
    timeframes: list[int] = field(default_factory=lambda: [1, 5])
    # Trend indicator
    ema_period: int = 9
    # Upstox's intraday candle API finalizes a just-closed candle a few seconds
    # after the boundary; reading at the exact boundary returns a stale close.
    # A candle counts as completed only this many seconds after it ends.
    candle_grace_sec: int = 6
    # "Decisive" close: candle close must clear the 9-EMA by this many index points
    decisive_points: float = 2.0
    # Opening warm-up: if the session's FIRST price/EMA cross happens before
    # skip_first_cross_before (candle start time), it is gap-settling noise and
    # is not traded — entries then start from the second cross. A first cross
    # at/after the cutoff trades normally.
    skip_first_cross: bool = True
    skip_first_cross_before: str = "09:20"
    # Flat-EMA filter (Institutional Block Filter): skip if the EMA moved less
    # than flat_ema_points over flat_ema_lookback completed candles. The points
    # value is defined on the 5-min chart and scaled by timeframe/5 elsewhere
    # (e.g. 3.0 here means 0.6 pts over 3 candles on the 1-min chart)
    flat_ema_filter: bool = True
    flat_ema_points: float = 3.0
    flat_ema_lookback: int = 3
    # Position slots: True = one concurrent position per timeframe (a 1m and a
    # 5m trade can be open simultaneously; capital for both must be available);
    # False = a single position shared across timeframes
    per_timeframe_positions: bool = True
    # Position sizing (per position)
    lots: int = 2
    capital: float = 30000.0
    max_risk_capital_per_trade: float = 12000.0
    # Contract selection: ATM or slightly ITM within the premium band
    premium_band_low: float = 80.0
    premium_band_high: float = 90.0
    delta_low: float = 0.45
    delta_high: float = 0.65
    max_itm_strikes: int = 2
    # Risk-to-reward (premium points)
    target_points: float = 4.0
    stoploss_points: float = 3.0
    # Trailing exit. Two modes:
    #  "ema"    - exit when the NIFTY spot touches the signal timeframe's 9-EMA
    #             (trend over); target and the initial hard stop still apply
    #  "points" - premium trail: arms once trail_activate_points above entry,
    #             then trails the captured high by trail_gap_points (only ever
    #             rises, never below the initial stop-loss)
    trailing_stop: bool = True
    trail_mode: str = "ema"
    trail_activate_points: float = 2.0
    trail_gap_points: float = 2.0
    # Session overlays
    max_trades_per_day: int = 2
    stop_after_target: bool = True
    max_consecutive_losses: int = 2
    midday_block: bool = True
    midday_block_start: str = "10:30"
    midday_block_end: str = "13:00"
    # Session timing (IST)
    no_entries_after: str = "15:00"
    square_off_at: str = "15:15"
    # Order handling
    entry_limit_buffer: float = 0.5   # limit price = LTP + buffer (still a limit order)
    entry_fill_timeout_sec: int = 30  # cancel unfilled entry after this
    # Cost model (round trip, for net P&L reporting)
    round_trip_charges: float = 56.0
    # Per-timeframe overrides: {"1": {field: value, ...}, "5": {...}} — only
    # PER_TF_FIELDS are honoured; unset fields fall back to the values above
    tf_overrides: dict = field(default_factory=dict)

    def for_tf(self, tf: int) -> "StrategyConfig":
        """Effective config for one timeframe (base + that tf's overrides)."""
        ov = (self.tf_overrides or {}).get(str(tf)) or {}
        if not ov:
            return self
        merged = replace(self)
        for k, v in ov.items():
            if k in PER_TF_FIELDS:
                setattr(merged, k, v)
        return merged

    def validate(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ValueError("mode must be 'paper' or 'live'")
        tfs = sorted({int(t) for t in self.timeframes})
        if not tfs or any(t not in (1, 5) for t in tfs):
            raise ValueError("timeframes must be a non-empty subset of [1, 5]")
        self.timeframes = tfs
        if not isinstance(self.tf_overrides, dict):
            raise ValueError("tf_overrides must be an object")
        clean: dict = {}
        for tf_s, ov in self.tf_overrides.items():
            if str(tf_s) not in ("1", "5") or not isinstance(ov, dict):
                raise ValueError("tf_overrides keys must be '1'/'5' with object values")
            clean[str(tf_s)] = {k: v for k, v in ov.items() if k in PER_TF_FIELDS}
        self.tf_overrides = clean
        for tf in (1, 5):
            m = self.for_tf(tf)
            if m.lots < 1:
                raise ValueError(f"{tf}m: lots must be >= 1")
            if m.target_points <= 0 or m.stoploss_points <= 0:
                raise ValueError(f"{tf}m: target/stoploss points must be positive")
            if m.trail_mode not in ("ema", "points"):
                raise ValueError(f"{tf}m: trail_mode must be 'ema' or 'points'")
            if m.trail_gap_points <= 0 or m.trail_activate_points < 0:
                raise ValueError(f"{tf}m: trail gap must be positive and activation non-negative")


def load_strategy_config() -> StrategyConfig:
    cfg = StrategyConfig()
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            for k, v in saved.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        except Exception:
            pass
    cfg.validate()
    return cfg


def save_strategy_config(cfg: StrategyConfig) -> None:
    CONFIG_FILE.write_text(json.dumps(asdict(cfg), indent=2))
