"""FastAPI application: REST API + the dashboard frontend page."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from .backtest import Backtester
from .config import EnvSettings, load_strategy_config, save_strategy_config
from .engine import Engine
from .upstox_client import UpstoxClient, UpstoxError

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"

env = EnvSettings()
client = UpstoxClient(env.api_key, env.api_secret, env.redirect_uri)
engine = Engine(client, load_strategy_config())
backtester = Backtester(client)

app = FastAPI(title="NIFTY Intraday Options Scalper (Upstox)")


@app.on_event("startup")
async def _startup():
    engine.ensure_loop()


@app.get("/")
async def index():
    return FileResponse(FRONTEND)


# ---------------- auth ----------------

@app.get("/api/auth/login")
async def auth_login():
    if not env.api_key or not env.api_secret:
        raise HTTPException(400, "Set UPSTOX_API_KEY and UPSTOX_API_SECRET in .env first")
    return RedirectResponse(client.login_url())


@app.get("/api/auth/callback")
async def auth_callback(code: str = ""):
    if not code:
        raise HTTPException(400, "Missing authorization code")
    try:
        await client.exchange_code(code)
    except UpstoxError as e:
        raise HTTPException(400, str(e))
    engine.log("auth", "Connected to Upstox")
    return RedirectResponse("/")


@app.post("/api/auth/token")
async def auth_token(body: dict = Body(...)):
    token = (body.get("access_token") or "").strip()
    if not token:
        raise HTTPException(400, "access_token required")
    client.set_token(token)
    engine.log("auth", "Access token set manually")
    return await auth_status()


@app.post("/api/auth/logout")
async def auth_logout():
    client.clear_token()
    engine.running = False
    engine.log("auth", "Disconnected from Upstox")
    return {"ok": True}


@app.get("/api/auth/status")
async def auth_status():
    info = {"configured": bool(env.api_key and env.api_secret),
            "token_present": bool(client.access_token),
            "profile": None}
    if client.access_token:
        try:
            p = await client.profile()
            info["profile"] = {"name": p.get("user_name"), "user_id": p.get("user_id"),
                               "broker": p.get("broker")}
        except UpstoxError as e:
            info["token_present"] = bool(client.access_token)  # cleared on 401
            info["error"] = str(e)
    return info


# ---------------- engine control ----------------

@app.get("/api/status")
async def status():
    return engine.status()


@app.post("/api/start")
async def start():
    try:
        engine.start()
    except UpstoxError as e:
        raise HTTPException(400, str(e))
    return {"running": True}


@app.post("/api/stop")
async def stop():
    engine.stop()
    return {"running": False}


@app.post("/api/squareoff")
async def squareoff():
    if not engine.positions:
        raise HTTPException(400, "No open position")
    engine.request_squareoff()
    return {"ok": True}


@app.post("/api/mode")
async def set_mode(body: dict = Body(...)):
    mode = body.get("mode", "")
    if mode not in ("paper", "live"):
        raise HTTPException(400, "mode must be 'paper' or 'live'")
    try:
        engine.set_mode(mode)
    except (UpstoxError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"mode": engine.cfg.mode}


@app.get("/api/config")
async def get_config():
    return asdict(engine.cfg)


@app.post("/api/config")
async def set_config(body: dict = Body(...)):
    cfg = engine.cfg
    editable = {
        "timeframes", "per_timeframe_positions", "ema_period", "candle_grace_sec",
        "decisive_points", "flat_ema_filter",
        "flat_ema_points", "flat_ema_lookback", "lots", "capital",
        "max_risk_capital_per_trade", "premium_band_low", "premium_band_high",
        "delta_low", "delta_high", "max_itm_strikes", "target_points",
        "stoploss_points", "trailing_stop", "trail_mode", "trail_activate_points",
        "trail_gap_points", "max_trades_per_day", "stop_after_target",
        "max_consecutive_losses", "midday_block", "midday_block_start",
        "midday_block_end", "no_entries_after", "square_off_at",
        "entry_limit_buffer", "entry_fill_timeout_sec", "round_trip_charges",
    }
    old = asdict(cfg)
    for k, v in body.items():
        if k in editable:
            setattr(cfg, k, v)
    try:
        cfg.validate()
    except ValueError as e:
        for k, v in old.items():
            setattr(cfg, k, v)
        raise HTTPException(400, str(e))
    save_strategy_config(cfg)
    engine.log("config", "Strategy parameters updated")
    return asdict(cfg)


@app.get("/api/trades")
async def trades():
    return engine.trades


# ---------------- backtesting (real historical data) ----------------

@app.post("/api/backtest/run")
async def backtest_run(body: dict = Body(default={})):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    yesterday = (datetime.now(ZoneInfo("Asia/Kolkata")).date() - timedelta(days=1))
    to_date = body.get("to_date") or yesterday.isoformat()
    from_date = body.get("from_date") or (yesterday - timedelta(days=30)).isoformat()
    tfs = body.get("timeframes") or engine.cfg.timeframes
    try:
        backtester.start(engine.cfg, from_date, to_date, tfs)
    except (UpstoxError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"started": True, "from": from_date, "to": to_date, "timeframes": tfs}


@app.get("/api/backtest/status")
async def backtest_status():
    return backtester.status()


def main() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host=env.host, port=env.port, reload=False)


if __name__ == "__main__":
    main()
