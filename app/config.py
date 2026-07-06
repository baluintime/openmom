"""Configuration: environment credentials + runtime strategy parameters.

Strategy defaults implement the "Nifty Intraday Options Scalping" spec:
9-EMA close-confirmation on the NIFTY 50 spot chart, +4 / -3 premium point
exits, 2 lots, max 2 trades/day, mid-day liquidity block, flat-EMA filter.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"
TOKEN_FILE = DATA_DIR / "token.json"

SPOT_INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"
IST = "Asia/Kolkata"


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
    # "Decisive" close: candle close must clear the 9-EMA by this many index points
    decisive_points: float = 2.0
    # Flat-EMA filter (Institutional Block Filter): skip if the EMA moved less
    # than flat_ema_points over flat_ema_lookback completed candles. The points
    # value is defined on the 5-min chart and scaled by timeframe/5 elsewhere
    # (e.g. 3.0 here means 0.6 pts over 3 candles on the 1-min chart)
    flat_ema_filter: bool = True
    flat_ema_points: float = 3.0
    flat_ema_lookback: int = 3
    # Position sizing
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
    # Trailing stop: once the premium is trail_activate_points above entry,
    # the stop trails the captured high by trail_gap_points (only ever rises,
    # never below the initial stop-loss)
    trailing_stop: bool = True
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

    def validate(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ValueError("mode must be 'paper' or 'live'")
        tfs = sorted({int(t) for t in self.timeframes})
        if not tfs or any(t not in (1, 5) for t in tfs):
            raise ValueError("timeframes must be a non-empty subset of [1, 5]")
        self.timeframes = tfs
        if self.lots < 1:
            raise ValueError("lots must be >= 1")
        if self.target_points <= 0 or self.stoploss_points <= 0:
            raise ValueError("target/stoploss points must be positive")
        if self.trail_gap_points <= 0 or self.trail_activate_points < 0:
            raise ValueError("trail gap must be positive and activation non-negative")


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
