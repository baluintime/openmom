# NIFTY SMA + EMA Options Scalper (Upstox)

Automated intraday options system on the **Upstox API** driven by a dual
moving-average (SMA + EMA) rule on the NIFTY 50 spot chart, with a web
dashboard. Three independent engines run on the **1-, 5- and 15-minute**
charts, each on its own page.

- **Real market data only** — every price (spot candles, option chain, LTP)
  comes live from Upstox. No synthetic or dummy data anywhere.
- **Chart = execution.** All timeframes' candles are built locally from
  1-minute data (Upstox's native 5m/15m aggregates finalize late and caused
  chart-vs-execution mismatches). The chart plots the engine's own candles and
  averages, so what you see is exactly what it trades on.
- **Paper or Live**, configurable independently per timeframe.

## Strategy (closed-candle values only)

Two moving averages on the NIFTY 50 spot close — an SMA and an EMA, periods
configurable per timeframe:

| | Rule |
|---|---|
| **Long entry** (buy CE) | a completed candle **closes above BOTH** the SMA and the EMA |
| **Long exit** | a completed candle **closes below EITHER** the SMA or the EMA |
| **Short entry** (buy PE) | a completed candle **closes below BOTH** |
| **Short exit** | a completed candle **closes above EITHER** |

Only close values are used — no intrabar/tick triggers. Entries and exits are
MARKET orders. Forced square-off at 15:15, no new entries after 15:00 (IST).

## ATM vs ITM comparison (paper mode)

In **paper mode** every signal opens **two legs** — one **ATM** contract and one
**ITM** contract — that enter and exit on the *same spot signal*, so their
timing is identical and only the instrument differs. The dashboard shows a live
**ATM vs ITM** scoreboard (net ₹, win rate, points) so you can see which
performs better. The ITM leg is chosen from strikes within `itm_max_depth` of
ATM, ranked by liquidity: highest OI, tightest bid/ask spread, and a delta at or
above `itm_min_delta`.

In **live mode** each timeframe trades exactly one contract — ATM or ITM, set by
`live_contract`.

## Setup

1. Create an Upstox API app at <https://account.upstox.com/developer/apps>
   with redirect URI `http://localhost:8000/api/auth/callback`.
2. `cp .env.example .env` and fill in `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`,
   `UPSTOX_REDIRECT_URI`.
3. `./run.sh` (creates a venv, installs deps, starts the server) or
   `pip install -r requirements.txt && python -m app.main`.
4. Open <http://localhost:8000>, **Connect Upstox**, pick a timeframe page,
   set Paper/Live and the SMA/EMA periods, press **Start**.

Upstox tokens expire daily (~3:30 AM IST); reconnect each morning.

## Per-timeframe settings

Each of the 1m / 5m / 15m pages has its own: mode (paper/live), SMA period, EMA
period, lots, live contract (ATM/ITM), and ITM selection controls
(`itm_max_depth`, `itm_min_delta`, `itm_min_oi`, `itm_max_spread_pct`). Session
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
- Every trade is written to `data/trades.jsonl` with `tf`, `mode`, `kind`
  (ATM/ITM), `side`, entry/exit, points, net, delta, oi, spread_pct — ready for
  pandas/Excel analysis of ATM-vs-ITM performance.

## Disclaimers

Live mode places **real orders with real money** (requires typing "LIVE" to
enable, per timeframe). Start in paper mode. This software is for educational
purposes and is **not investment advice**; you are responsible for trades placed
through it.
