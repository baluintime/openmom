"""Configuration for the SMA+EMA dual-average scalping system.

Strategy (spot NIFTY 50, per timeframe, close values only):
  Long  (buy CE): enter when a completed candle CLOSES above BOTH the SMA and
                  the EMA; exit when a candle CLOSES below EITHER.
  Short (buy PE): enter when a completed candle CLOSES below BOTH; exit when a
                  candle CLOSES above EITHER.

Three independent engines run on the 1-, 5- and 15-minute charts. All candles
are built locally from 1-minute data (Upstox's own 5m/15m aggregates finalize
late), so what the chart shows is exactly what the engine trades on.

Paper mode opens BOTH an ATM and an ITM option per signal and tracks them
separately so the two can be compared. Live mode trades one (configurable).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"
TOKEN_FILE = DATA_DIR / "token.json"

SPOT_INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"
IST = "Asia/Kolkata"
TIMEFRAMES = (1, 5, 15)


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass
class EnvSettings:
    api_key: str = os.getenv("UPSTOX_API_KEY", "")
    api_secret: str = os.getenv("UPSTOX_API_SECRET", "")
    redirect_uri: str = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:8000/api/auth/callback")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))


@dataclass
class TFConfig:
    """Per-timeframe account settings. No strategy is configured — signal logic
    is added on top of this scaffold. Paper/live and lots are retained so a new
    strategy can trade immediately once wired in."""
    timeframe: int
    enabled: bool = False
    mode: str = "paper"          # "paper" | "live"
    lots: int = 1

    def validate(self) -> None:
        if self.timeframe not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {TIMEFRAMES}")
        if self.mode not in ("paper", "live"):
            raise ValueError("mode must be 'paper' or 'live'")
        if self.lots < 1:
            raise ValueError("lots must be >= 1")


@dataclass
class AppConfig:
    capital: float = 30000.0
    round_trip_charges: float = 56.0        # per option round trip, for net P&L
    no_entries_after: str = "15:00"
    square_off_at: str = "15:15"
    candle_grace_sec: int = 6               # wait this long after a candle ends
    tf: dict = field(default_factory=lambda: {
        str(t): asdict(TFConfig(timeframe=t)) for t in TIMEFRAMES})

    def tfcfg(self, timeframe: int) -> TFConfig:
        return TFConfig(**self.tf[str(timeframe)])

    def set_tf(self, timeframe: int, cfg: TFConfig) -> None:
        cfg.validate()
        self.tf[str(timeframe)] = asdict(cfg)

    def validate(self) -> None:
        for t in TIMEFRAMES:
            self.tfcfg(t).validate()


def load_config() -> AppConfig:
    cfg = AppConfig()
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            for k, v in saved.items():
                if k == "tf" and isinstance(v, dict):
                    for tk, tv in v.items():
                        if tk in cfg.tf and isinstance(tv, dict):
                            cfg.tf[tk].update({kk: vv for kk, vv in tv.items()
                                               if kk in cfg.tf[tk]})
                elif hasattr(cfg, k):
                    setattr(cfg, k, v)
        except Exception:
            pass
    # self-heal: a stale/invalid saved value (e.g. an old strategy id) must not
    # crash startup — reset only the offending timeframe to its defaults
    for t in TIMEFRAMES:
        try:
            cfg.tfcfg(t).validate()
        except (ValueError, TypeError):
            cfg.tf[str(t)] = asdict(TFConfig(timeframe=t))
    try:
        cfg.validate()
    except (ValueError, TypeError):
        cfg = AppConfig()
    return cfg


def save_config(cfg: AppConfig) -> None:
    CONFIG_FILE.write_text(json.dumps(asdict(cfg), indent=2))
