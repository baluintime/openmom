"""FastAPI app: REST API + the dashboard (3 timeframe pages)."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from .config import (EnvSettings, TIMEFRAMES, TFConfig, load_config, save_config)
from .engine import Engine
from .upstox_client import UpstoxClient, UpstoxError

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"

env = EnvSettings()
client = UpstoxClient(env.api_key, env.api_secret, env.redirect_uri)
engine = Engine(client, load_config())

app = FastAPI(title="NIFTY SMA+EMA Options Scalper (Upstox)")


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
    for t in TIMEFRAMES:
        engine.engines[t].running = False
    engine.log("auth", "Disconnected from Upstox")
    return {"ok": True}


@app.get("/api/auth/status")
async def auth_status():
    info = {"configured": bool(env.api_key and env.api_secret),
            "token_present": bool(client.access_token), "profile": None}
    if client.access_token:
        try:
            p = await client.profile()
            info["profile"] = {"name": p.get("user_name"), "user_id": p.get("user_id"),
                               "broker": p.get("broker")}
        except UpstoxError as e:
            info["error"] = str(e)
    return info


# ---------------- status / control ----------------
@app.get("/api/status")
async def status():
    return engine.status()


@app.post("/api/start")
async def start(body: dict = Body(...)):
    tf = int(body.get("tf", 0))
    if tf not in TIMEFRAMES:
        raise HTTPException(400, "tf must be 1, 5 or 15")
    try:
        engine.start(tf)
    except UpstoxError as e:
        raise HTTPException(400, str(e))
    return {"tf": tf, "running": True}


@app.post("/api/stop")
async def stop(body: dict = Body(...)):
    tf = int(body.get("tf", 0))
    if tf not in TIMEFRAMES:
        raise HTTPException(400, "tf must be 1, 5 or 15")
    engine.stop(tf)
    return {"tf": tf, "running": False}


@app.get("/api/config")
async def get_config():
    return asdict(engine.cfg)


@app.post("/api/config")
async def set_config(body: dict = Body(...)):
    cfg = engine.cfg
    for k in ("capital", "round_trip_charges", "no_entries_after",
              "square_off_at", "candle_grace_sec"):
        if k in body:
            setattr(cfg, k, body[k])
    if "tf" in body and isinstance(body["tf"], dict):
        for tk, tv in body["tf"].items():
            if tk in cfg.tf and isinstance(tv, dict):
                merged = dict(cfg.tf[tk])
                merged.update({k: v for k, v in tv.items() if k in merged})
                try:
                    TFConfig(**merged).validate()
                except (ValueError, TypeError) as e:
                    raise HTTPException(400, f"{tk}m: {e}")
                cfg.tf[tk] = merged
    try:
        cfg.validate()
    except ValueError as e:
        raise HTTPException(400, str(e))
    save_config(cfg)
    engine.log("config", "Settings updated")
    return asdict(cfg)


@app.get("/api/trades")
async def trades():
    return engine.trades


def main() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host=env.host, port=env.port, reload=False)


if __name__ == "__main__":
    main()
