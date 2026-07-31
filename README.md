# NIFTY Renko vs Fast Ichimoku Options Scalper (Upstox)

Automated intraday options system on the **Upstox API** comparing two scalping
strategies — **Renko** and **Fast Ichimoku** — on the NIFTY 50 spot chart, with
a web dashboard. Three independent engines run on the **1-, 5- and 15-minute**
charts, each on its own page.

- **Real market data only** — every price (spot candles, option chain, LTP)
  comes live from Upstox. No synthetic or dummy data anywhere.
- **Chart = execution.** All timeframes' candles are built locally from
  1-minute data (Upstox's native 5m/15m aggregates finalize late and caused
  chart-vs-execution mismatches). The chart plots the engine's own candles and
  indicators, so what you see is exactly what it trades on.
- **Paper or Live**, configurable independently per timeframe.

## Two strategies compared (Renko vs Fast Ichimoku)

Both run on the NIFTY 50 spot candles (built locally from 1-minute data, closed
candles only). Long buys a CE, short buys a PE — always the **ATM** option. In
**paper mode both run in parallel**, each with its own position, so the
dashboard scoreboard compares them apples-to-apples on identical candles.
**Live mode** trades one (`live_strategy`).

**Strategy 1 · Renko** (primary — pure momentum, **tick-driven**)
- Bricks are built **tick-by-tick from the live index price** (the NIFTY spot
  LTP, sampled every second — the index itself only re-computes ~once a second,
  so this is effectively tick-level for the underlying), using classic **2-box
  reversal** and tracking each brick's **wick**. Brick size = **ATR(14)**
  (dynamic) or a **fixed point** value (5–10 for NIFTY), configurable.
- **2-brick rule**: enter only after **two consecutive bricks** in the new
  direction — long (CE) when up **and** price is above the EMA-20 overlay;
  short (PE) when down and below EMA-20.
- **Exits (all evaluated per tick)**: a **reversal brick**, the **stop-loss at
  the wick/base of the run's first brick**, or the **take-profit at 1.5–2.0
  bricks** (`renko_target_bricks`) from entry.
- Brick completion is evaluated the instant price crosses a threshold — not on
  candle close — matching the spec's "triggers immediately upon brick
  threshold completion". The dashboard shows the live brick strip, size, and
  the active SL/target. (Sub-second true-tick precision would need the Upstox
  websocket feed; 1-second LTP sampling of the index is equivalent in practice.)

**Strategy 2 · Fast Ichimoku** (accelerated 9-22-44-22)
- Tenkan 9, Kijun 22, Senkou-B 44, displacement 22 — all configurable.
- **Long**: price breaks **above the Kumo cloud** with **Tenkan > Kijun**;
  **short**: breaks **below** with Tenkan < Kijun. No entry while price is
  **inside the cloud** (low-volatility filter).
- **Exit**: a candle **closes on the wrong side of the Tenkan**.

Entries and exits are MARKET orders. Forced square-off at 15:15, no new entries
after 15:00 (IST). The scoreboard (net ₹, win rate, points, leader highlighted)
aggregates all paper trades across all timeframes by strategy.

## Setup

1. Create an Upstox API app at <https://account.upstox.com/developer/apps>
   with redirect URI `http://localhost:8000/api/auth/callback`.
2. `cp .env.example .env` and fill in `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`,
   `UPSTOX_REDIRECT_URI`.
3. `./run.sh` (creates a venv, installs deps, starts the server) or
   `pip install -r requirements.txt && python -m app.main`.
4. Open <http://localhost:8000>, **Connect Upstox**, pick a timeframe page,
   set Paper/Live and the strategy parameters, press **Start**.

Upstox tokens expire daily (~3:30 AM IST); reconnect each morning.

## Per-timeframe settings

Each of the 1m / 5m / 15m pages has its own: mode (paper/live), lots, live
strategy (`live_strategy` = 1 Renko / 2 Ichimoku), Renko params (`renko_mode`,
`renko_points`, `atr_period`, `ema_filter_period`) and Ichimoku params
(`tenkan`, `kijun`, `senkou_b`, `displacement`). Session settings (capital,
charges, square-off / no-entry times, candle grace) are shared and stored in
`data/config.json`.

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
- Every trade is written to `data/trades.jsonl` with `tf`, `mode`, `strategy`
  (1 Renko / 2 Ichimoku), `strategy_name`, `side`, entry/exit, index @ entry/exit,
  points, net and reason. Download as Excel from the dashboard (today / all
  history) for side-by-side analysis of the two strategies.

## Disclaimers

Live mode places **real orders with real money** (requires typing "LIVE" to
enable, per timeframe). Start in paper mode. This software is for educational
purposes and is **not investment advice**; you are responsible for trades placed
through it.
