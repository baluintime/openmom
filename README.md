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

## Three strategies compared

All three use the same SMA and EMA on the NIFTY 50 spot close (closed-candle
values only, periods configurable per timeframe). Long buys a CE, short buys a
PE — always the **ATM** option. In **paper mode all three run in parallel**, each
with its own position, so the dashboard's scoreboard compares them
apples-to-apples. **Live mode** trades one strategy (`live_strategy`).

**Strategy 1 · MA Zone** (the original)
- Long (CE): close above **both** SMA and EMA; exit when close below **either**.
- Short (PE): close below **both**; exit when close above **either**.

**Strategy 2 · MA Momentum**
- Long (CE): enter when the **EMA crosses above the SMA**; hold while the gap
  `EMA − SMA` keeps **widening** candle-over-candle; exit as soon as it narrows.
- Short (PE): enter when the **EMA crosses below the SMA**; hold while `SMA − EMA`
  widens; exit as soon as it narrows.

**Strategy 3 · Price Crossover**
- Long (CE): enter when the **close crosses above both** the EMA and SMA; exit
  when the **close crosses below the SMA**.
- Short (PE): enter when the **close crosses below both**; exit when the **close
  crosses above the SMA**.

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
   set Paper/Live and the SMA/EMA periods, press **Start**.

Upstox tokens expire daily (~3:30 AM IST); reconnect each morning.

## Per-timeframe settings

Each of the 1m / 5m / 15m pages has its own: mode (paper/live), SMA period, EMA
period, lots, and live strategy (`live_strategy` = 1, 2 or 3). Session settings
(capital, charges, square-off / no-entry times, candle grace) are shared and
stored in `data/config.json`.

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
  (1/2/3), `strategy_name`, `side`, entry/exit, points, net and reason — ready
  for pandas/Excel analysis of the three-strategy comparison.

## Disclaimers

Live mode places **real orders with real money** (requires typing "LIVE" to
enable, per timeframe). Start in paper mode. This software is for educational
purposes and is **not investment advice**; you are responsible for trades placed
through it.
