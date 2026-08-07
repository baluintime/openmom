"""Configuration for the NSE EOD Momentum options-selling strategy.

At 3:15 PM the system scans all NSE F&O-eligible stocks and ranks them by how
close spot is to the day's high/low. At 3:24:50 it re-scans the shortlist on
live prices and picks the definitive top N. At 3:25 it SELLS an OTM option on
each (PUT for a stock near its high / CALL for a stock near its low), and
attaches a +TP% / -SL% exit. Positions are carried overnight.
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
POSITIONS_FILE = DATA_DIR / "positions.json"
UNIVERSE_FILE = DATA_DIR / "universe.json"

IST = "Asia/Kolkata"
# Upstox NSE instruments master (public, gzipped JSON)
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


def parse_hms(s: str) -> time:
    """Parse 'HH:MM' or 'HH:MM:SS'."""
    parts = [int(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return time(parts[0], parts[1], parts[2])


@dataclass
class EnvSettings:
    api_key: str = os.getenv("UPSTOX_API_KEY", "")
    api_secret: str = os.getenv("UPSTOX_API_SECRET", "")
    redirect_uri: str = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:8000/api/auth/callback")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))


@dataclass
class EODConfig:
    mode: str = "paper"            # "paper" | "live"
    top_n: int = 1                 # how many top-ranked stocks to trade
    lots: int = 1                  # option lots per stock
    otm_strikes: int = 5           # max OTM distance from spot (strikes)
    tp_pct: float = 5.0            # take-profit: option price drops this % (seller gain)
    sl_pct: float = 20.0           # stop-loss: option price rises this % (seller loss)
    scan_time: str = "15:15"       # initial stock scan (IST)
    refresh_time: str = "15:24:50" # pre-exec rescan of the shortlist
    dispatch_time: str = "15:25"   # place the sell orders + exits
    shortlist_size: int = 15       # how many to carry from scan -> refresh
    min_oi: float = 0.0            # skip option strikes below this OI (0 = off)
    max_spread_pct: float = 5.0    # skip strikes with bid/ask spread% above this (0 = off)
    universe_limit: int = 0        # cap stocks scanned (0 = all F&O; >0 for speed/testing)
    capital: float = 100000.0
    charges_per_trade: float = 40.0  # round-trip cost per option leg, for net P&L

    def validate(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ValueError("mode must be 'paper' or 'live'")
        for k in ("top_n", "lots", "otm_strikes", "shortlist_size"):
            if int(getattr(self, k)) < 1:
                raise ValueError(f"{k} must be >= 1")
        if self.tp_pct <= 0 or self.tp_pct >= 100:
            raise ValueError("tp_pct must be between 0 and 100")
        if self.sl_pct <= 0:
            raise ValueError("sl_pct must be > 0")
        for k in ("scan_time", "refresh_time", "dispatch_time"):
            parse_hms(getattr(self, k))  # raises on bad format
        if self.top_n > self.shortlist_size:
            raise ValueError("top_n cannot exceed shortlist_size")


def load_config() -> EODConfig:
    cfg = EODConfig()
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            for k, v in saved.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        except Exception:
            pass
    try:
        cfg.validate()
    except (ValueError, TypeError):
        cfg = EODConfig()   # self-heal a bad/stale saved config
    return cfg


def save_config(cfg: EODConfig) -> None:
    CONFIG_FILE.write_text(json.dumps(asdict(cfg), indent=2))
