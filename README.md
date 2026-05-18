# SignalBridge

**SignalBridge** is a private, local trading dashboard and webhook bridge.

It runs on your own machine, exposes a small web UI you open in a browser, accepts TradingView alerts at a webhook endpoint, applies your risk rules, and executes them through a broker adapter.

**Broker status today:**

- **Paper** — fully functional. Simulates fills, tracks positions, and computes realized PnL in price-points. Default account id is `PAPER-001` (configurable via `SELECTED_ACCOUNT_ID`).
- **Topstep / TopstepX** — adapter is scaffolded but **not connected** to a real API yet. `execute()` rejects with `broker_not_implemented`; the read-only methods (`get_accounts`, `get_positions`, `get_orders`, …) return a structured `not_implemented` envelope so the dashboard never crashes.
- **Tradovate** — same: scaffolded placeholder, not connected.
- Broker credentials are **env-only** (`TOPSTEP_*` / `TRADOVATE_*` in `.env`). The dashboard never echoes them back; it only shows whether each one is configured.
- **No live orders are placed by this build.**

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
| `/settings/broker` | pick provider + execution mode (form), Topstep / Tradovate placeholder fields, "Test connection" button |
| `/settings/risk`   | edit allow-list, contracts cap, daily loss, open-positions cap, longs/shorts toggles, dup cooldown · kill-switch toggle |
| `/tradingview`     | webhook URL, alert JSON template, edit / regenerate the webhook secret, allowed symbols |
| `/journal`         | recent signals (accepted/rejected) + recent closed paper trades |
| `/metrics`         | accepted/rejected counts, rejection reasons, trades by symbol, basic paper P&L, win rate |
| `/logs`            | tail of `logs/signalbridge.log` |
| `/system`          | app name/version, host/port, db & log paths, cwd, broker, mode, `.env` loaded?, runtime status, useful local URLs |

All pages share a top bar showing **execution mode**, **broker provider**, and a **live / halted** pill driven by the kill switch.

---

## Quick start

```bash
git clone <local copy> signalbridge
cd signalbridge
cp .env.example .env
# edit .env — at minimum set:
#   TRADINGVIEW_WEBHOOK_SECRET  (long random string, never commit)
#   ADMIN_PASSWORD              (strong password before exposing the UI)
#   SESSION_SECRET              (long random string before exposing the UI)

# Linux / macOS
./run.sh

# Windows
run.bat
```

Server boots on `http://127.0.0.1:8000`. Open it in a browser — you'll
land on `/login` and need the admin credentials from `.env`.

Smoke checks (no login required for `/health`):

```bash
curl http://127.0.0.1:8000/health
```

Webhook smoke check — bypasses dashboard auth, validates the shared secret
in the JSON body:

```bash
curl -X POST http://127.0.0.1:8000/webhooks/tradingview \
  -H 'Content-Type: application/json' \
  -d '{"secret":"<TRADINGVIEW_WEBHOOK_SECRET>","source":"tradingview",
       "strategy":"manual","symbol":"MES1!","action":"buy",
       "contracts":"1","price":"5000.25","order_id":"smoke_1"}'
```

The admin-only endpoints (`/api/status`, `/api/system`, `/api/metrics`,
…) return `401` until you log in — `curl` them with the session cookie
from your browser, or just open the dashboard.

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

`.env` provides **defaults** at first boot. The dashboard then persists any
changes you make to a `settings` table in `data/signalbridge.db`, and those
stored values **override the `.env` defaults** at runtime. Resetting a value
back to its `.env` default means editing the `settings` row in SQLite (or
deleting it and restarting).

Dashboard-editable keys: `APP_HOST`, `APP_PORT`, `EXECUTION_MODE`,
`BROKER_PROVIDER`, `SELECTED_ACCOUNT_ID`, `TRADINGVIEW_WEBHOOK_SECRET`,
`ALLOWED_SYMBOLS`, `MAX_CONTRACTS_PER_TRADE`, `MAX_DAILY_LOSS`,
`MAX_OPEN_POSITIONS`, `ENABLE_LONGS`, `ENABLE_SHORTS`,
`DUPLICATE_ORDER_COOLDOWN_SECONDS`.

Runtime-applied immediately: webhook secret, execution mode, all risk
limits, allow-list, longs/shorts toggles, duplicate cooldown.
Restart-required: `APP_HOST`, `APP_PORT`, `BROKER_PROVIDER` (the broker
adapter is built once at startup).

**Broker credentials** (Topstep / Tradovate `USERNAME`, `PASSWORD`,
`API_KEY`, etc.) are intentionally **not** editable from the UI yet —
those still come from `.env` only.

**Execution adapters today.** Only `paper` is functional. `topstep` and
`tradovate` load so the app can boot, but `execute()` raises
`NotImplementedError` (turned into a labeled rejection by the webhook
handler). `live` execution mode is rejected at the settings layer.

All env defaults (see `.env.example` for the full list):

| Variable | Purpose |
| --- | --- |
| `APP_HOST`, `APP_PORT`              | bind address (default `127.0.0.1:8000`) |
| `ADMIN_AUTH_ENABLED`                | enable dashboard login (default `true`) |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD`  | admin credentials for `/login` |
| `SESSION_SECRET`                    | signing key for the session cookie |
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
| `GET  /api/status`                    | app + broker + open positions + `selected_account_id`, `broker_connected`, `broker_message` |
| `GET  /api/metrics`                   | accepted/rejected counts, rejection reasons, by-symbol, win rate |
| `GET  /api/journal/recent?limit=50`   | recent signals + closed trades |
| `GET  /api/positions`                 | open positions only |
| `POST /api/kill-switch/enable`        | activate the kill switch (halts execution) |
| `POST /api/kill-switch/disable`       | deactivate the kill switch |
| `GET  /api/broker/status`             | active provider, selected account, connection status |
| `POST /api/broker/test-connection`    | probe the active broker adapter |
| `GET  /api/broker/accounts`           | accounts visible to the active adapter |
| `GET  /api/broker/positions`          | open positions from the active adapter |
| `GET  /api/broker/orders`             | recent orders from the active adapter |
| `GET  /api/system`                    | host/port, paths, runtime status, useful local URLs |
| `POST /webhooks/tradingview`          | the inbound alert endpoint |

`POST /api/broker/test-connection` returns `200` with `ok: true` for paper and `501` with `ok: false` and `status: "not_implemented"` for topstep / tradovate. The `GET /api/broker/*` query endpoints always return `200` with a JSON envelope — when the active provider hasn't implemented an operation, the envelope includes `"not_implemented": true` so the dashboard can render safely.

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
- Database: `data/signalbridge.db` (SQLite). Tables: `settings`, `signals`, `positions`, `daily_pnl`, `closed_trades`.

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

## Dashboard authentication

SignalBridge protects the dashboard, settings pages, and admin JSON
endpoints behind a single admin password so the app can be exposed
through Tailscale Funnel (or any public tunnel) without leaking control
over your trading bridge.

| Setting                | Default                              | What it does |
| ---------------------- | ------------------------------------ | --- |
| `ADMIN_AUTH_ENABLED`   | `true`                               | turn auth on/off (set `false` only for purely local dev) |
| `ADMIN_USERNAME`       | `admin`                              | username posted to `/login` |
| `ADMIN_PASSWORD`       | `change_me_admin_password`           | password posted to `/login` — **change before exposing the UI** |
| `SESSION_SECRET`       | `generate_or_require_secret`         | signs the session cookie — **set a long random value before exposing the UI** |

**What's protected:**
- All HTML pages (`/`, `/settings/broker`, `/settings/risk`,
  `/tradingview`, `/journal`, `/metrics`, `/logs`, `/system`) — anonymous
  visitors get a 303 redirect to `/login`.
- All admin JSON endpoints (`/api/status`, `/api/system`, `/api/metrics`,
  `/api/journal/recent`, `/api/positions`, `/api/kill-switch/*`,
  `/api/broker/*`) — anonymous callers get `401`.
- All settings POST endpoints (`/settings/broker`, `/settings/risk`,
  `/tradingview/secret`, `/tradingview/secret/regenerate`).

**What's intentionally public:**
- `GET /health` — needed for tunnel liveness checks.
- `POST /webhooks/tradingview` — TradingView's servers don't have a
  dashboard session. The endpoint stays open but rejects any payload
  whose `secret` field doesn't match `TRADINGVIEW_WEBHOOK_SECRET`. **The
  webhook secret is the only thing standing between TradingView (or
  anyone who finds the tunnel URL) and your risk engine — rotate it
  through the dashboard before going public.**

**Before turning on Tailscale Funnel:**

1. `ADMIN_PASSWORD` — change from the default to a strong password.
2. `SESSION_SECRET` — change from the default to a long random string
   (e.g. `python -c 'import secrets; print(secrets.token_urlsafe(48))'`).
   Rotating this signs out everyone.
3. `TRADINGVIEW_WEBHOOK_SECRET` — change from the default. Either edit
   `.env` or use the **TradingView** page in the dashboard (it can
   regenerate a fresh secret for you).

If `SESSION_SECRET` or `ADMIN_PASSWORD` are still on the default at
startup, the app logs a `WARNING` so you notice.

---

## Safety notes

- Live execution is **not** implemented. Topstep + Tradovate adapters raise `NotImplementedError` on `execute()`, and every read-only method (`get_accounts`, `get_positions`, `get_orders`, …) returns `not_implemented: true` instead of hitting a real API.
- Paper mode is the default and cannot place real orders.
- Broker credentials live in `.env` only. The UI never echoes raw values back; it only reports whether each one is configured.
- The kill switch is on by default — create `data/kill_switch.active` (or click the button on `/settings/risk`) to halt all execution. Delete the file (or click again) to resume.
- The webhook secret is the only check on `/webhooks/tradingview` — use a long random string and never commit it.
- This is a single-user local app. Dashboard auth (above) gates the UI and admin API; the webhook stays open by design but is shared-secret protected.
