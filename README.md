# NSE EOD Momentum Options Strategy (Upstox)

Automated **end-of-day** options-selling strategy for NSE F&O stocks, on the
**Upstox API**, with a web dashboard. It ranks all F&O stocks by how close their
spot sits to the day's high/low near the close, then sells an OTM option on the
top-ranked names and carries the position overnight.

- **Real market data only** — stock quotes, option chain, LTP all come live from
  Upstox. Paper mode simulates only *execution*, at real prices.
- **Paper or Live**, switchable in the dashboard (Live requires typing "LIVE").

## Strategy (per the spec)

**Screening — "Strength" (3:15 PM IST):** across every NSE F&O-eligible stock,
on the underlying *cash* price:

- Bullish strength = `(Day High − Spot) / Spot × 100`  → small ⇒ near the high
- Bearish strength = `(Spot − Day Low) / Spot × 100`   → small ⇒ near the low

Each stock takes its smaller strength; stocks closest to **0%** (hugging an
extreme) rank highest.

**Pipeline:**

| Time (IST) | Phase | Action |
|---|---|---|
| 3:15:00 | Scan | Quote all F&O stocks, compute Strength, shortlist the top |
| 3:24:50 | Refresh | Re-quote the shortlist on live prices, pick the definitive **top N** |
| 3:25:00 | Dispatch | For each pick: select an OTM option and place a **market SELL**, attach the exit |

**Trade rules:**

- Stock near its **high** (bullish) → **sell an OTM PUT** within `otm_strikes` below spot.
- Stock near its **low** (bearish) → **sell an OTM CALL** within `otm_strikes` above spot.
- Among in-range strikes the most liquid (highest OI, tightest bid/ask spread,
  passing the OI/spread floors) is chosen.

**Exit & lifecycle:**

- **Take-profit**: option price falls to `sell × (1 − TP%)` (default 5% → seller gain).
- **Stop-loss**: option price rises to `sell × (1 + SL%)` (default 20% → seller loss).
- **Overnight carry** — no intraday square-off. A position closes only when its
  exit triggers, on manual exit, or when the option expires (expired OTM ⇒ the
  seller keeps the full premium).
- **Live**: the SELL uses product **D** (carry-forward) and the exit is an
  exchange **GTT** (OCO target + stop). **Paper**: the exit is monitored locally
  against the live option LTP and simulated.

## Setup

1. Create an Upstox API app (redirect URI `http://localhost:8000/api/auth/callback`).
2. `cp .env.example .env`, fill `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`, `UPSTOX_REDIRECT_URI`.
3. `pip install -r requirements.txt && python -m app.main`.
4. Open <http://localhost:8000>, **Connect Upstox**, set Paper/Live and the
   parameters, press **Arm strategy**. Use **Run scan now** to preview the
   screener any time, and **Dispatch now** to place the sells manually.

Live selling requires F&O margin in the account. Upstox tokens expire ~3:30 AM
IST — reconnect each morning (positions persist in `data/positions.json`).

## Settings

Top stocks (N), lots, OTM strike range, TP% / SL%, shortlist size, min OI, max
spread %, universe limit (cap stocks scanned — 0 = all), scan / refresh /
dispatch times, capital, per-trade charges. Stored in `data/config.json`.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | Full snapshot (config, pipeline phase, shortlist, candidates, positions, trades, events) |
| `POST /api/start` / `POST /api/stop` | Arm / disarm the strategy |
| `POST /api/scan` | Run scan + refresh now (no orders) |
| `POST /api/dispatch` | Place the sells for the current candidates now |
| `POST /api/exit` `{"id":...}` | Buy back and close one position |
| `GET/POST /api/config` | Read / update settings |
| `GET /api/trades` · `GET /api/trades.xlsx?scope=today\|all` | Trades JSON / Excel |
| Auth: `GET /api/auth/login` → `/api/auth/callback`, `POST /api/auth/token` | Upstox OAuth / manual token |

## Logs & analysis

- Activity log captured per day to `data/events-YYYY-MM-DD.log` and `.jsonl`.
- Every closed trade is written to `data/trades.jsonl` (stock, bias, contract,
  strike, expiry, sell/exit prices, spot at entry, reason, gross/charges/net,
  overnight flag) — downloadable as Excel from the dashboard.
- Open (carried) positions persist in `data/positions.json` across restarts.

## Notes on live use

Live SELL + GTT paths are implemented against Upstox's documented order/GTT v3
endpoints but have not been exercised against a live account here — verify with
one stock and small size first. Screening quotes are batched (F&O universe can
be ~180+ stocks); the instruments master is cached daily in `data/universe.json`.

## Disclaimer

Options *selling* carries unlimited-risk characteristics and requires margin.
Live mode places **real orders**. This software is educational and **not
investment advice**; you are responsible for every order it places.
