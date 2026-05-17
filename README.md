# SignalBridge

**SignalBridge** is a private, local trading dashboard and webhook bridge.

It runs on your own machine, exposes a small web UI you open in a browser, accepts TradingView alerts at a webhook endpoint, applies your risk rules, and executes them through a broker adapter. Today the only functional adapter is **paper trading**; **Topstep / TopstepX** and **Tradovate** are stubbed out for the future.

> Not SaaS. Not multi-user. Not packaged for distribution. Not live yet.

```
TradingView alert
   │
   ▼
SignalBridge webhook (POST /webhooks/tradingview)
   │  validate secret → parse → normalize action
   ▼
Risk engine (allow-list, caps, kill switch, dupes, daily loss…)
   │
   ▼
Broker adapter (paper today; topstep / tradovate planned)
   │
   ▼
Journal / metrics / logs   ←—   visible in the local dashboard
```

---

## What you see when you open it

`http://127.0.0.1:8000/` — local dashboard with:

| Page | What it shows |
| --- | --- |
| `/`                | app status, broker, kill switch, allowed symbols, open positions, today's accepted/rejected counts, last signal, last rejection, paper P&L |
| `/settings/broker` | active provider, configured Topstep / Tradovate fields, "Test connection" button |
| `/settings/risk`   | read-only view of risk limits + kill-switch toggle |
| `/tradingview`     | your webhook URL, secret status, alert JSON template, allowed symbols |
| `/journal`         | recent signals (accepted/rejected) + recent closed paper trades |
| `/metrics`         | accepted/rejected counts, rejection reasons, trades by symbol, basic paper P&L, win rate |
| `/logs`            | tail of `logs/signalbridge.log` |

All pages share a top bar showing **execution mode**, **broker provider**, and a **live / halted** pill driven by the kill switch.

---

## Quick start

```bash
git clone <local copy> signalbridge
cd signalbridge
cp .env.example .env
# edit .env — set a long random TRADINGVIEW_WEBHOOK_SECRET

# Linux / macOS
./run.sh

# Windows
run.bat
```

Server boots on `http://127.0.0.1:8000`. Open it in a browser.

Smoke checks:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/status
```

---

## How a signal flows through

1. **Receive** — TradingView posts JSON to `/webhooks/tradingview`.
2. **Authenticate** — `secret` field is compared (constant-time) against `TRADINGVIEW_WEBHOOK_SECRET`.
3. **Parse** — Pydantic schema accepts numeric fields as **either** strings (TradingView's normal output) **or** unquoted numbers.
4. **Normalize action** — `buy`/`long` → `BUY`, `sell` → `SELL`, `short` → `SHORT`, `cover` → `COVER`, `exit`/`close` → `EXIT`.
5. **Risk engine** runs every check:
   - kill switch active?
   - symbol on allow-list?
   - contracts above cap?
   - direction allowed (longs/shorts toggles)?
   - daily loss limit reached?
   - duplicate `order_id` inside cooldown window?
   - already at max open positions?
6. **Broker adapter** executes:
   - `paper` — simulates a fill at the alert's `price`, updates the position, records realized PnL (in price-points) when a fill closes / reduces the position.
   - `topstep` — rejects with `broker_not_implemented`. (Adapter loads so the app can boot, but `execute()` raises `NotImplementedError` — caught by the webhook handler and turned into a labeled rejection.)
   - `tradovate` — same placeholder behavior.
7. **Journal** writes one row per signal (accepted or rejected) to SQLite.
8. **Dashboard** picks up the new row on the next page load.

A rejection at any step returns `{"accepted": false, "decision": "rejected", "rejection_reason": "..."}` and is still written to the journal.

---

## Configuration

All config lives in `.env` (see `.env.example` for the full list).

| Variable | Purpose |
| --- | --- |
| `APP_HOST`, `APP_PORT`              | bind address (default `127.0.0.1:8000`) |
| `EXECUTION_MODE`                    | `paper` today; `demo` / `live` reserved for the future |
| `BROKER_PROVIDER`                   | `paper` (default), `topstep`, `tradovate` |
| `BROKER`                            | legacy alias for `BROKER_PROVIDER` |
| `TRADINGVIEW_WEBHOOK_SECRET`        | shared secret in the alert body |
| `ALLOWED_SYMBOLS`                   | comma-separated allow-list |
| `MAX_CONTRACTS_PER_TRADE`           | hard cap per signal |
| `MAX_DAILY_LOSS`                    | daily realized PnL floor |
| `MAX_OPEN_POSITIONS`                | concurrent open positions cap |
| `ENABLE_LONGS`, `ENABLE_SHORTS`     | direction toggles |
| `ENABLE_KILL_SWITCH`                | turn the kill switch feature on/off |
| `DUPLICATE_ORDER_COOLDOWN_SECONDS`  | reject re-sent `order_id`s inside this window |
| `DATABASE_PATH`, `LOG_PATH`         | storage paths |
| `TOPSTEP_*`                         | placeholders — not used until the adapter ships |
| `TRADOVATE_*`                       | placeholders — not used until the adapter ships |

---

## TradingView setup

In the dashboard, open **TradingView** to see the alert JSON template prefilled with your webhook URL and a status indicator for the secret. The minimum body shape is:

```json
{
  "secret": "<your TRADINGVIEW_WEBHOOK_SECRET>",
  "source": "tradingview",
  "strategy": "orb_200ema_confluence",
  "symbol": "{{ticker}}",
  "exchange": "{{exchange}}",
  "action": "{{strategy.order.action}}",
  "contracts": "{{strategy.order.contracts}}",
  "price": "{{strategy.order.price}}",
  "order_id": "{{strategy.order.id}}"
}
```

To make `127.0.0.1` reachable from TradingView's servers, expose it with **ngrok** or **Cloudflare Tunnel**. See `app/tunnel/ngrok_notes.py` and `app/tunnel/cloudflare_notes.py` for notes, and [`docs/TRADINGVIEW_ALERTS.md`](docs/TRADINGVIEW_ALERTS.md) for the full alert template.

---

## REST API (used by the dashboard JS, also fine to curl)

| Method + path | Purpose |
| --- | --- |
| `GET  /health`                        | liveness probe |
| `GET  /status`                        | top-level status (same shape as `/api/status`) |
| `GET  /api/status`                    | app + broker + open positions |
| `GET  /api/metrics`                   | accepted/rejected counts, rejection reasons, by-symbol, win rate |
| `GET  /api/journal/recent?limit=50`   | recent signals + closed trades |
| `GET  /api/positions`                 | open positions only |
| `POST /api/kill-switch/enable`        | activate the kill switch (halts execution) |
| `POST /api/kill-switch/disable`       | deactivate the kill switch |
| `POST /api/broker/test-connection`    | probe the active broker adapter |
| `POST /webhooks/tradingview`          | the inbound alert endpoint |

`POST /api/broker/test-connection` returns `200` with `ok: true` for paper and `501` with `ok: false` and `status: "not_implemented"` for topstep / tradovate.

---

## Local testing

See [`docs/LOCAL_TESTING.md`](docs/LOCAL_TESTING.md) for ready-to-paste `curl` recipes covering valid alerts, bad secrets, unknown symbols, oversized orders, invalid prices, disabled shorts, and duplicate `order_id`.

Run the test suite:

```bash
pip install -r requirements.txt
pytest -q
```

---

## Logs and database

- Logs: `logs/signalbridge.log` (rotating, 5 MB × 3 backups). Tail in the dashboard at `/logs`.
- Database: `data/signalbridge.db` (SQLite). Tables: `signals`, `positions`, `daily_pnl`, `closed_trades`.

Inspect from the CLI:

```bash
sqlite3 data/signalbridge.db \
  "select id, received_at, broker_provider, symbol, action, decision, rejection_reason
   from signals order by id desc limit 20;"

sqlite3 data/signalbridge.db \
  "select id, closed_at, symbol, side, contracts, entry_price, exit_price,
          realized_pnl_points from closed_trades order by id desc limit 20;"
```

---

## Safety notes

- Live execution is **not** implemented. Topstep + Tradovate adapters raise `NotImplementedError` on `execute()`.
- Paper mode is the default and cannot place real orders.
- The kill switch is on by default — create `data/kill_switch.active` (or click the button on `/settings/risk`) to halt all execution. Delete the file (or click again) to resume.
- The webhook secret is the only authentication. Use a long random string and never commit it.
- This is a single-user local app. There is no auth in front of the dashboard — bind to `127.0.0.1` and don't expose the dashboard port publicly.
