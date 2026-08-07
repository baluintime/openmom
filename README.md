# NIFTY Options Scalper (Upstox)

Intraday NIFTY options trading scaffold on the **Upstox API** with a web
dashboard. Three independent engines run on the **1-, 5- and 15-minute** charts,
each on its own page. **No trading strategy is currently configured** — see
"No strategy configured (scaffold)" below.

- **Real market data only** — every price (spot candles, option chain, LTP)
  comes live from Upstox. No synthetic or dummy data anywhere.
- **Chart = execution.** All timeframes' candles are built locally from
  1-minute data (Upstox's native 5m/15m aggregates finalize late and caused
  chart-vs-execution mismatches). The chart plots the engine's own candles.
- **Paper or Live**, configurable independently per timeframe.

## No strategy configured (scaffold)

The trading strategies have been removed. The app currently runs as a **clean
scaffold**: it authenticates with Upstox, streams the live NIFTY 50 spot,
builds per-timeframe candles, manages any open position and forced square-off,
and serves the dashboard, reports and Excel export — but **opens no trades**
because no signal logic is wired in.

Adding a strategy is the one seam left open (`app/strategy.py`):

1. add its id + metadata to `STRATEGIES`,
2. return its id from `TFEngine.active_strats()`,
3. implement its decision in the tick loop and call `self._enter(...)` /
   drive exits — the position/broker/reporting plumbing
   (`_enter`, `_settle`, `_finalize`, trades + Excel + logs) is already in place.

Everything else — login, live feed & chart, the 1m/5m/15m pages, paper/live
toggle, lot sizing, square-off, trade reporting with index-price logging, and
the Excel download — is retained and working.

## Setup

1. Create an Upstox API app at <https://account.upstox.com/developer/apps>
   with redirect URI `http://localhost:8000/api/auth/callback`.
2. `cp .env.example .env` and fill in `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`,
   `UPSTOX_REDIRECT_URI`.
3. `./run.sh` (creates a venv, installs deps, starts the server) or
   `pip install -r requirements.txt && python -m app.main`.
4. Open <http://localhost:8000>, **Connect Upstox**, pick a timeframe page,
   set Paper/Live and lots, press **Start**. (With no strategy configured,
   Start simply runs the feed/position manager — no trades are opened.)

Upstox tokens expire daily (~3:30 AM IST); reconnect each morning.

## Per-timeframe settings

Each of the 1m / 5m / 15m pages has its own mode (paper/live) and lots. Session
settings (capital, charges, square-off / no-entry times, candle grace) are
shared and stored in `data/config.json`.

## API

| Endpoint | Purpose |
|---|---|
| `GET /` | Dashboard |
| `GET /api/status` | Full snapshot (spot, per-tf engines, comparison, trades, events) |
| `POST /api/start` / `POST /api/stop` `{"tf":1\|5\|15}` | Start/stop one timeframe |
| `GET/POST /api/config` | Read/update settings (session or `{"tf":{"5":{...}}}`) |
| `GET /api/trades` | Today's trades |
| Auth: `GET /api/auth/login` → `/api/auth/callback`, `POST /api/auth/token` | Upstox OAuth / manual token |

## Logs & analysis

- Activity log is timeframe-tagged with filter tabs (All / 1m / 5m / 15m /
  System) and captured per day to `data/events-YYYY-MM-DD.log` (readable) and
  `data/events-YYYY-MM-DD.jsonl` (structured).
- Every trade is written to `data/trades.jsonl` with `tf`, `mode`, `strategy`,
  `strategy_name`, `side`, entry/exit, index @ entry/exit, points, net and
  reason. Download as Excel from the dashboard (today / all history). (No
  strategy is configured yet, so no trades are produced until one is added.)

## Disclaimers

Live mode places **real orders with real money** (requires typing "LIVE" to
enable, per timeframe). Start in paper mode. This software is for educational
purposes and is **not investment advice**; you are responsible for trades placed
through it.
