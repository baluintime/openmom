# NIFTY Intraday Options Scalper (Upstox)

Fully automated implementation of the *NIFTY Intraday Options Scalping System*
requirements (₹30,000 capital, 9-EMA momentum scalping) on the **Upstox API**,
with a web dashboard frontend.

- **Real market data only** — every price (spot candles, option chain, premium
  LTP) comes live from Upstox. There is no synthetic, dummy or simulated data
  anywhere in the system.
- **Paper or Real trading** — selectable from the dashboard. Paper mode
  simulates *fills only*, always at real market prices; live mode places real
  orders on your Upstox account.
- **Both 1-minute and 5-minute** signal engines, individually switchable and
  able to run simultaneously.
- **No candlestick chart** — the dashboard shows a spot-close vs 9-EMA line
  chart plus live tickers, position, risk gates, trades and an activity log.

## Strategy (per the requirements document)

| Rule | Implementation |
|---|---|
| Indicator | 9-EMA on **NIFTY 50 spot** closes (never on the option premium) |
| CE entry | Completed candle closes *decisively* above the 9-EMA (≥ 2 pts, configurable) → buy ATM/slightly-ITM Call at the open of the next candle |
| PE entry | Mirror: decisive close below the 9-EMA → buy Put |
| "Close" confirmation | Signals fire **only on completed candles** — never while a candle is painting |
| Opening warm-up | If the session's **first** price/EMA cross (per timeframe) happens before 09:20 it is treated as gap-settling and not traded — entries then start from the second cross. A first cross at/after 09:20 trades normally (cutoff configurable) |
| Contract selection | Nearest expiry, ATM or ≤ 2 strikes ITM, premium band ₹80–90, delta 0.45–0.65 (from the live Upstox option chain; lot size read from the API) |
| Position size | 2 lots (configurable) per position |
| Position slots | One per timeframe by default — a 1-min and a 5-min trade can be open simultaneously (≈ double the capital deployed while both run). Configurable to a single shared slot. Daily limits (max trades, cutoffs) are shared across timeframes |
| Entry orders | **LIMIT** (LTP + small buffer); auto-cancelled if unfilled in 30 s |
| Target | +4 premium points (LIMIT exit) |
| Stop-loss | −3 premium points (MARKET exit) |
| Trailing exit | Default **EMA-touch (close-confirmed)**: exit when a *completed* candle of the signal timeframe closes at/across the 9-EMA — intra-candle wicks through the EMA do not exit; the +4 target and −3 hard stop still apply. Alternative **points** mode: premium trails the captured high by 2 pts once +2 pts in profit. Both exit as MARKET |
| High/Low capture | The premium's high and low since entry are tracked live, shown on the position card and recorded per trade (live and backtest) |
| Max trades/day | 2 setups |
| Target cutoff | First target hit → terminal deactivated for the day |
| Double-loss cutoff | 2 consecutive stop-losses → done for the day |
| Mid-day block | No entries 10:30–13:00 IST |
| Flat-EMA filter | Signal skipped if the 9-EMA is flat: < 3 pts move over 3 candles on the 5-min chart, scaled by timeframe (< 0.6 pts on 1-min) |
| Session guard | No entries after 15:00; forced square-off at 15:15 IST |
| Cost model | ₹56 round-trip charges applied to net P&L reporting |

Every threshold above is editable in the dashboard's **Strategy settings**
panel and persists across restarts (as does the day's risk state, so a restart
cannot reset the daily limits). Settings are split into three tabs: **Session**
(shared: capital, daily limits, mid-day block, warm-up, square-off, charges)
and **1 min / 5 min strategy** — each timeframe has its own EMA period,
decisive margin, flat filter, target/stop, trailing exit, lots and contract
selection, stored as overrides in `tf_overrides` and honoured by both the live
engine and the backtester.

The activity log is timeframe-tagged (filter tabs: All / 1 min / 5 min /
System) and captured per day to two files for analysis: human-readable
`data/events-YYYY-MM-DD.log` and structured `data/events-YYYY-MM-DD.jsonl`
(one JSON object per line: ts, tf, kind, msg).

## Setup

1. **Create an Upstox API app** at <https://account.upstox.com/developer/apps>
   with redirect URI `http://localhost:8000/api/auth/callback`.
2. Configure credentials:

   ```bash
   cp .env.example .env
   # edit .env → UPSTOX_API_KEY, UPSTOX_API_SECRET, UPSTOX_REDIRECT_URI
   ```

3. Run:

   ```bash
   ./run.sh          # creates a venv, installs deps, starts the server
   ```

   or manually:

   ```bash
   pip install -r requirements.txt
   python -m app.main
   ```

4. Open <http://localhost:8000>, click **Connect Upstox** (OAuth) — or paste
   an access token generated elsewhere — pick **Paper trade** or **Real
   (live) trade**, choose 1-min / 5-min, and press **Start engine**.

Upstox access tokens expire daily (~3:30 AM IST); reconnect each morning.

## API

| Endpoint | Purpose |
|---|---|
| `GET /` | Dashboard page |
| `GET /api/status` | Full engine snapshot (spot, EMA series, position, risk, trades, events) |
| `POST /api/start` / `POST /api/stop` | Start/stop the signal engine (an open position keeps being managed after stop) |
| `POST /api/squareoff` | Flatten the open position at market |
| `POST /api/mode` `{"mode":"paper"\|"live"}` | Switch execution mode (only while flat) |
| `GET/POST /api/config` | Read/update strategy parameters |
| `GET /api/trades` | Today's trade log |
| `GET /api/auth/login` → `/api/auth/callback` | Upstox OAuth flow |
| `POST /api/auth/token` | Set an access token manually |
| `POST /api/backtest/run` `{"from_date","to_date","timeframes":[1,5]}` | Start a backtest (defaults: last ~1 month, both timeframes) |
| `GET /api/backtest/status` | Backtest progress and, when done, the full result |

## Backtesting

The dashboard's **Backtest** card replays the exact engine rules over a chosen
date range (default: the last month, up to yesterday) using only real Upstox
historical data:

- NIFTY 50 spot 1-min/5-min candles → signals, flat-EMA filter, all risk gates
- 1-min premium candles of the actual contract the selection rules pick —
  via the *expired-instruments* API for past expiries, the regular
  historical-candle API for still-active ones

It reports, per timeframe: final net P&L (after ₹56/round-trip charges),
trade count, win rate, exit-reason breakdown, best/worst day and a
trade-by-trade table.

Honest approximations (tick data is not available historically): entries fill
at the open of the candle after the signal candle; within a candle the
stop-loss is evaluated before the target (conservative); the delta filter is
not applied because historical option greeks are unavailable. Treat results
as a realistic estimate, not an exact replay.

## Notes & disclaimers

- Live mode places **real orders with real money**. The dashboard requires a
  typed confirmation before enabling it. Start in paper mode.
- Exits are monitor-driven: the engine watches the real premium LTP every ~2 s
  and sends the exit order when target/stop/square-off conditions trigger, in
  both paper and live mode, so behaviour is identical across modes.
- A zero-loss system is impossible (spread, theta, slippage — see the
  requirements doc). This software is provided for educational purposes and is
  **not investment advice**; you are responsible for trades placed through it.
