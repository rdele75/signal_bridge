# SignalBridge

**SignalBridge** is a private, local trading dashboard and webhook bridge.

It runs on your own machine, exposes a small web UI you open in a browser, accepts TradingView alerts at a webhook endpoint, applies your risk rules, and executes them through a broker adapter.

**Broker status today:**

- **Paper** — fully functional. Simulates fills, tracks positions, and computes realized PnL in price-points. Default account id is `PAPER-001` (configurable via `SELECTED_ACCOUNT_ID`).
- **Topstep / TopstepX** — fully wired to ProjectX. `test_connection()` runs real `/api/Auth/loginKey` + `/api/Account/search`, `submit_market_order` posts to `/api/Order/place`, `flatten_position` calls `/api/Position/closeContract`, and the read-only methods (`get_accounts`, `get_positions`, `get_orders`, `get_order_history`) hit the real ProjectX endpoints. See [`docs/topstep.md`](docs/topstep.md) for the integration details.
- Topstep credentials (`TOPSTEP_USERNAME`, `TOPSTEP_API_KEY`, `TOPSTEP_ACCOUNT_ID`, `TOPSTEP_ENV`) can be set in `.env` or persisted via `/settings/broker`. The dashboard masks the API key (last four characters only). `TOPSTEP_PASSWORD` stays env-only.
- Execution modes: `paper` (default, safe), `demo` (Topstep simulated account via real ProjectX endpoints), `live` (real funded account). Live mode is gated behind a multi-step arming flow on the Dashboard — see [`docs/topstep.md`](docs/topstep.md) for the full gate stack.

> [!WARNING]
> **Live execution is implemented and is real.** Arming live mode through the Dashboard's live-engagement flow will route signed TradingView signals through `submit_market_order` to your real Topstep funded account. Verify `LIVE_MAX_CONTRACTS_PER_TRADE`, `LIVE_ALLOWED_SYMBOLS`, and the kill-switch state before you arm. The full operator-experience audit including known invisible-settings gaps is in [`docs/operational_audit_2026-05-21.md`](docs/operational_audit_2026-05-21.md).

> Not SaaS. Not multi-user. Not packaged for distribution.

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
Broker adapter (paper or topstep live; tradovate removed)
   │
   ▼
Journal / metrics / logs   ←—   visible in the local dashboard
```

---

## What you see when you open it

`http://127.0.0.1:8000/` — local dashboard with:

| Page | What it shows |
| --- | --- |
| `/`                | **Execution card** (mode toggle, demo/live arming, Exit-All), trading session, broker status, account snapshot card, **Ticker Watch** placeholder, today's accepted/rejected counts, last signal, last rejection, P&L |
| `/settings/broker` | account configuration: broker provider, Topstep credentials, selected-account dropdown, account snapshot polling. Execution controls live on the Dashboard. |
| `/settings/risk`   | edit contracts cap, daily loss, open-positions cap, longs/shorts toggles, timeframe lock, dup cooldown · kill-switch activate/deactivate buttons (runtime state only — the feature flag `ENABLE_KILL_SWITCH` stays env-only) |
| `/tradingview`     | current webhook secret (copyable) + regenerate button, Test webhook button, Xiznit Universal ORB alert recipe |
| `/journal`         | recent signals (accepted/rejected) + recent closed paper trades |
| `/metrics`         | accepted/rejected counts, rejection reasons, trades by symbol, basic paper P&L, win rate |
| `/logs`            | tail of `logs/signalbridge.log` |
| `/system`          | app name/version, host/port, db & log paths, cwd, broker, mode, `.env` loaded?, runtime status, useful local URLs |
| `/settings/profile`| change dashboard admin username + password (PBKDF2-SHA256 hash stored in SQLite) |

Every page has a top-bar kill-switch button. Execution state (mode, broker provider, armed/locked) lives on the Dashboard execution card.

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
   - `topstep` — in dry-run mode (default), builds the `/api/Order/place` payload and journals it without submitting. With demo execution armed, submits to ProjectX against a simulated account. With live execution armed and every gate satisfied, submits to ProjectX against the funded account. The handler in `app/webhook.py:_execute_topstep` picks the path based on `EXECUTION_MODE` and the safety flags.
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

> [!IMPORTANT]
> Several settings in `MANAGED_KEYS` do **not** have UI edit surfaces yet —
> including the live-execution caps (`LIVE_MAX_CONTRACTS_PER_TRADE`,
> `LIVE_ALLOWED_SYMBOLS`, `LIVE_REQUIRE_KILL_SWITCH_OFF`). For those keys,
> editing `.env` post-bootstrap does **nothing**: SignalBridge reads the
> SQLite-stored value at startup and ignores the env default. Today the only
> fix path is editing the SQLite `settings` row directly. Phase 2 of the
> consolidation pass will land an "Advanced settings" page that exposes
> every managed key. See
> [`docs/operational_audit_2026-05-21.md`](docs/operational_audit_2026-05-21.md)
> Section 1 for the full inventory and rationale.

Dashboard-editable keys today: `APP_HOST` (read-only on `/system`),
`APP_PORT` (read-only on `/system`), `EXECUTION_MODE`, `BROKER_PROVIDER`,
`SELECTED_ACCOUNT_ID`, `TRADINGVIEW_WEBHOOK_SECRET`,
`MAX_CONTRACTS_PER_TRADE`, `STRATEGY_MANAGED_RISK`,
`FIXED_CONTRACTS_PER_TRADE`, `MAX_DAILY_LOSS`, `MAX_OPEN_POSITIONS`,
`ENABLE_LONGS`, `ENABLE_SHORTS`, `DUPLICATE_ORDER_COOLDOWN_SECONDS`,
`ENABLE_TIMEFRAME_LOCK`, `ALLOWED_TIMEFRAMES`, `TOPSTEP_USERNAME`,
`TOPSTEP_API_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH` (set via
`/settings/profile`).

Live-execution gates (`ENABLE_LIVE_TRADING`, `LIVE_TRADING_CONFIRM`,
`LIVE_TRADING_ACCOUNT_ACK`, plus `TOPSTEP_EXECUTION_CONFIRM` and
`ENABLE_TOPSTEP_ORDER_EXECUTION` when arming live) are flipped together
through `POST /api/topstep/live-execution/enable` — the Dashboard's
live-engagement modal is the canonical surface. The settings form will
refuse to set `EXECUTION_MODE=live` directly so the arming flow stays
the only path.

Runtime-applied immediately: webhook secret, execution mode, all risk
limits, allow-list, longs/shorts toggles, duplicate cooldown, Topstep
credentials, live-execution arming.
Restart-required: `APP_HOST`, `APP_PORT`, `BROKER_PROVIDER` (the broker
adapter is built once at startup).

**Broker credentials.** `TOPSTEP_USERNAME` and `TOPSTEP_API_KEY` can be
persisted via `/settings/broker`. `TOPSTEP_PASSWORD` stays env-only —
ProjectX authenticates via the API key, the password is unused once
the key is configured.

**Execution adapters today.** Paper is the safe default and never
places real orders. Topstep is fully wired to ProjectX (`/api/Auth`,
`/api/Order/place`, `/api/Position/closeContract`,
`/api/Account/search`, `/api/Order/search`). Demo and live execution
both run through the same `submit_market_order` code path; the
difference is which gate stack is required and which Topstep account
the order hits. See [`docs/topstep.md`](docs/topstep.md) for the full
gate stack and the integration map.

All env defaults (see `.env.example` for the full list):

| Variable | Purpose |
| --- | --- |
| `APP_HOST`, `APP_PORT`              | bind address (default `127.0.0.1:8000`) |
| `ADMIN_AUTH_ENABLED`                | enable dashboard login (default `true`) |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD`  | admin credentials for `/login` |
| `SESSION_SECRET`                    | signing key for the session cookie |
| `EXECUTION_MODE`                    | `paper` (default), `demo`, or `live`. `live` cannot be set from the broker form — use the Dashboard live-arming flow |
| `BROKER_PROVIDER`                   | `paper` (default), `topstep` |
| `BROKER`                            | legacy alias for `BROKER_PROVIDER` |
| `TRADINGVIEW_WEBHOOK_SECRET`        | shared secret in the alert body |
| `ALLOWED_SYMBOLS`                   | comma-separated allow-list |
| `MAX_CONTRACTS_PER_TRADE`           | hard cap per signal (always enforced) |
| `STRATEGY_MANAGED_RISK`             | `true` (default) → trade sizing comes from the alert's `contracts`; `false` → use `FIXED_CONTRACTS_PER_TRADE`. See [docs/risk.md](docs/risk.md). |
| `FIXED_CONTRACTS_PER_TRADE`         | quantity used when `STRATEGY_MANAGED_RISK=false` (must be ≤ `MAX_CONTRACTS_PER_TRADE`) |
| `MAX_DAILY_LOSS`                    | daily realized PnL floor |
| `MAX_OPEN_POSITIONS`                | concurrent open positions cap |
| `ENABLE_LONGS`, `ENABLE_SHORTS`     | direction toggles |
| `ENABLE_KILL_SWITCH`                | turn the kill switch feature on/off (env-only, requires restart) |
| `DUPLICATE_ORDER_COOLDOWN_SECONDS`  | reject re-sent `order_id`s inside this window |
| `ENABLE_TIMEFRAME_LOCK`, `ALLOWED_TIMEFRAMES` | optional chart-timeframe allow-list |
| `WEBHOOK_RATE_LIMIT_PER_SECOND`, `WEBHOOK_RATE_BURST` | token-bucket rate limit for `/webhooks/tradingview` (env-only, requires restart) |
| `TRADING_DAY_TIMEZONE`              | timezone for the daily-PnL boundary (env-only, requires restart) |
| `DATABASE_PATH`, `LOG_PATH`, `LOG_LEVEL` | storage paths and log verbosity |
| `TOPSTEP_USERNAME`, `TOPSTEP_API_KEY`, `TOPSTEP_ACCOUNT_ID`, `TOPSTEP_ENV` | ProjectX credentials. `TOPSTEP_USERNAME` and `TOPSTEP_API_KEY` are editable from `/settings/broker`. `TOPSTEP_ACCOUNT_ID` is mirrored from `SELECTED_ACCOUNT_ID`. `TOPSTEP_ENV` stays `demo` |
| `TOPSTEP_PASSWORD`                  | env-only; unused once `TOPSTEP_API_KEY` is configured |
| `TOPSTEP_BASE_URL`, `TOPSTEP_WS_URL` | ProjectX endpoints. Persisted in SQLite on first boot; no UI edit surface today |
| `TOPSTEP_TOKEN`, `TOPSTEP_TOKEN_EXPIRES_AT` | adapter-managed auth cache — never user-editable |
| `ENABLE_TOPSTEP_ORDER_DRY_RUN`      | when true (default), Topstep builds order payloads without submitting them (managed key, awaiting Phase 2 UI surface) |
| `ENABLE_TOPSTEP_ORDER_EXECUTION`    | gate that allows Topstep `/api/Order/place` calls — flipped via the demo / live arming endpoints |
| `TOPSTEP_EXECUTION_CONFIRM`         | confirmation token (`disabled` / `DEMO_ONLY` / `LIVE_CONFIRMED`) — flipped by the arming endpoints |
| `ENABLE_LIVE_TRADING`               | live/funded execution master switch — flipped via `POST /api/topstep/live-execution/enable`, never `.env` after first boot |
| `LIVE_TRADING_CONFIRM`              | live arming token (`I_UNDERSTAND_LIVE_ORDERS`) — flipped by the live-arming endpoint |
| `LIVE_TRADING_ACCOUNT_ACK`          | operator-acknowledged ownership of the live account — flipped by the live-arming endpoint |
| `LIVE_MAX_CONTRACTS_PER_TRADE`      | per-trade cap independent of `MAX_CONTRACTS_PER_TRADE`. **No UI surface today — verify the SQLite-stored value before arming live (audit Section 1 critical 1)** |
| `LIVE_ALLOWED_SYMBOLS`              | symbols allowed for live execution. **No UI surface today — verify the SQLite-stored list before arming live (audit Section 1 critical 2)** |
| `LIVE_REQUIRE_KILL_SWITCH_OFF`      | when true (default), the kill switch must be off before live orders submit. **No UI surface today (audit Section 1 critical 3)** |
| `ORDER_HISTORY_LOOKBACK_DAYS`, `ORDER_HISTORY_LIMIT` | defaults for the `/metrics` order-history widget (no UI edit today) |
| `ENABLE_TOPSTEP_REALTIME`, `TOPSTEP_REALTIME_MODE`, `TOPSTEP_REALTIME_POLL_SECONDS` | account/position/order polling. `signalr` mode is documented but not wired |

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
| `POST /api/paper/flatten`             | zero every open paper position (paper provider only) |
| `POST /api/paper/flatten/{symbol}`    | zero one paper position by symbol |
| `POST /api/paper/reset`               | clear paper position/order state (keeps signal journal) |
| `GET  /api/system`                    | host/port, paths, runtime status, useful local URLs |
| `POST /webhooks/tradingview`          | the inbound alert endpoint |

`POST /api/broker/test-connection` returns `200` with `ok: true` for paper, and for topstep when the API key authenticates against ProjectX and at least one account is visible. Topstep failure envelopes carry a documented `status` field (`missing_credentials`, `auth_failed`, `network_error`, etc.) at HTTP 200 with `ok: false`; genuine server errors come back as `500`. The `GET /api/broker/*` query endpoints always return `200` with a JSON envelope so the dashboard can render safely.

---

## Paper position cleanup (flatten / reset)

Paper mode accumulates simulated positions during testing — every accepted
TradingView alert adds, reduces, or flips a position the same way a live
broker would. To return the simulated account to flat without restarting:

- **Flatten Paper Positions** — closes every open paper position (or one
  symbol via `/api/paper/flatten/{symbol}`).
- **Reset Paper State** — additionally clears the paper adapter's
  in-memory order state. Useful when you've been firing test alerts and
  want a clean slate.

Neither action deletes the signal journal, closed-trade history, or daily
PnL — those remain as your testing record. Each action logs a
`paper_flatten_all` / `paper_flatten_symbol` / `paper_reset_state` event to
`logs/signalbridge.log`.

Buttons live on the **Dashboard** (under Open positions) and on
**Broker** (`/settings/broker`, under "Paper controls"). Both prompt for
confirmation before firing.

If the active provider is `topstep`, these endpoints return a safe
`{"ok": false, "not_implemented": true, "status":
"not_available_for_provider"}` envelope — they only operate on the paper
adapter.

```bash
# Flatten all paper positions
curl -X POST http://127.0.0.1:8000/api/paper/flatten \
  -b "$(cat dashboard_cookies.txt)"

# Flatten one symbol
curl -X POST http://127.0.0.1:8000/api/paper/flatten/MES1! \
  -b "$(cat dashboard_cookies.txt)"

# Reset paper position/order state (keeps signal journal)
curl -X POST http://127.0.0.1:8000/api/paper/reset \
  -b "$(cat dashboard_cookies.txt)"
```

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
| `ADMIN_USERNAME`       | `admin`                              | username posted to `/login` (also editable from `/settings/profile`) |
| `ADMIN_PASSWORD`       | `change_me_admin_password`           | plaintext fallback used until you save a new password from `/settings/profile` — **change before exposing the UI** |
| `ADMIN_PASSWORD_HASH`  | _(empty)_                            | PBKDF2-SHA256 hash written by `/settings/profile`; takes precedence over `ADMIN_PASSWORD` once set |
| `SESSION_SECRET`       | `generate_or_require_secret`         | signs the session cookie — **set a long random value before exposing the UI** |

**What's protected:**
- All HTML pages (`/`, `/settings/broker`, `/settings/risk`,
  `/tradingview`, `/settings/profile`, `/journal`, `/metrics`, `/logs`,
  `/system`) — anonymous visitors get a 303 redirect to `/login`.
- All admin JSON endpoints (`/api/status`, `/api/system`, `/api/metrics`,
  `/api/journal/recent`, `/api/positions`, `/api/kill-switch/*`,
  `/api/broker/*`) — anonymous callers get `401`.
- All settings POST endpoints (`/settings/broker`, `/settings/risk`,
  `/settings/profile`, `/tradingview/secret`,
  `/tradingview/secret/regenerate`).

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
   After first login, visit **System → Profile** (`/settings/profile`)
   to rotate the username/password — the page stores a PBKDF2-SHA256
   hash in SQLite so the plaintext value no longer matters.
2. `SESSION_SECRET` — change from the default to a long random string
   (e.g. `python -c 'import secrets; print(secrets.token_urlsafe(48))'`).
   Rotating this signs out everyone.
3. `TRADINGVIEW_WEBHOOK_SECRET` — change from the default. Either edit
   `.env` or use the **TradingView** page in the dashboard (it can
   regenerate a fresh secret for you). After regenerating, update
   **both** TradingView alert webhook URLs (Xiznit setup uses
   `?secret=…&symbol={{ticker}}` rather than the JSON body).

If `SESSION_SECRET` or `ADMIN_PASSWORD` are still on the default at
startup (and no `ADMIN_PASSWORD_HASH` has been saved), the app logs a
`WARNING` so you notice.

---

## Safety notes

- **Live execution is implemented.** Arming live mode through the Dashboard's live-engagement flow flips every gate together (`ENABLE_LIVE_TRADING`, `LIVE_TRADING_CONFIRM`, `LIVE_TRADING_ACCOUNT_ACK`, `TOPSTEP_EXECUTION_CONFIRM=LIVE_CONFIRMED`, `EXECUTION_MODE=live`). Subsequent webhooks route through `submit_market_order` to your real Topstep funded account.
- **Before arming live, verify the safety knobs.** `LIVE_MAX_CONTRACTS_PER_TRADE` (per-trade hard cap, independent of `MAX_CONTRACTS_PER_TRADE`), `LIVE_ALLOWED_SYMBOLS` (live-symbol allow-list — defaults to micros only), and `LIVE_REQUIRE_KILL_SWITCH_OFF` (defaults to enforced) all gate live submissions. None has a UI edit surface today — read the values directly from SQLite or `.env.example` before you arm. See [`docs/operational_audit_2026-05-21.md`](docs/operational_audit_2026-05-21.md) Section 1 for the full inventory.
- Paper mode is the default and cannot place real orders.
- `TOPSTEP_USERNAME` and `TOPSTEP_API_KEY` can be saved from the UI. `TOPSTEP_PASSWORD` stays env-only. The UI never echoes raw API-key values back; it only shows the last four characters.
- The kill switch is on by default — create `data/kill_switch.active` (or click the toggle in the top bar / the activate button on `/settings/risk`) to halt all execution. Delete the file (or click again) to resume. Set `ENABLE_KILL_SWITCH=false` in `.env` and restart to disable the feature entirely (not recommended).
- The webhook secret is the only check on `/webhooks/tradingview` — use a long random string and never commit it.
- This is a single-user local app. Dashboard auth (above) gates the UI and admin API; the webhook stays open by design but is shared-secret protected.
