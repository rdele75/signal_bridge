# SignalBridge

**SignalBridge** is a private, local trading dashboard and webhook bridge.

It runs on your own machine, exposes a small web UI you open in a browser, accepts TradingView alerts at a webhook endpoint, applies your risk rules, and executes them through a broker adapter.

**Broker:** Topstep / TopstepX (ProjectX) is the only adapter. Every order SignalBridge submits is a real ProjectX order on a real Topstep account. The fact that a Topstep account is an eval (Combine, Practice, Express) vs funded is incidental — both are real money, both go through the same `/api/Order/place` endpoint, both deserve the same treatment. The dashboard labels the selected account with a `Funded` or `Eval` badge so the operator always sees which one is wired up.

**Execution state:** SignalBridge runs in one of three states, picked from the Dashboard's mode dropdown:

- **Off** — execution disengaged. Signals are journaled but no orders submit, no broker round-trip happens, and the kill switch is irrelevant.
- **Test** — orders are built and validated against ProjectX schema but NOT POSTed. Used for smoke-testing plumbing (credentials, symbol map, payload shape) without risking a fill.
- **Armed** — orders submit to `/api/Order/place` against the selected Topstep account. The kill switch, the armed-symbol allowlist, the contracts cap, and the account-canTrade flag all gate each submission.

> [!WARNING]
> **Armed execution is real.** Switching to Armed and clicking Apply flips a single setting; subsequent TradingView alerts route through `submit_market_order` to the selected Topstep account. Verify the selected account, the `ALLOWED_SYMBOLS_ARMED` list, and `MAX_CONTRACTS_PER_TRADE` before you arm.

> Not SaaS. Not multi-user. Not packaged for distribution.

```
TradingView alert
   │
   ▼
SignalBridge webhook (POST /webhooks/tradingview)
   │  validate secret → parse → normalize action
   ▼
Risk engine (allow-list, caps, direction toggles, dupes, daily loss,
             kill switch when armed)
   │
   ▼
Execution state:  off    → journal as accepted, no broker call
                  test   → Topstep adapter builds payload, no POST
                  armed  → Topstep adapter POSTs /api/Order/place
   │
   ▼
Journal / metrics / logs   ←—   visible in the local dashboard
```

---

## What you see when you open it

`http://127.0.0.1:8000/` — local dashboard with:

| Page | What it shows |
| --- | --- |
| `/`                | **Execution card** (Off / Test / Armed dropdown with Apply, Funded/Eval badge next to Armed, Flatten All button, Smoke Test button), trading session, broker connection status, today's **Armed** accepted/rejected counts, last signal, last rejection, P&L |
| `/settings/broker` | Topstep credentials, account-selection dropdown, Test connection / Fetch accounts buttons |
| `/settings/risk`   | edit contracts cap, daily loss, open-positions cap, longs/shorts toggles, timeframe lock, dup cooldown, and BOTH symbol allowlists (general + armed) |
| `/tradingview`     | current webhook secret (copyable) + regenerate, Xiznit Universal ORB alert recipe, Test webhook button |
| `/journal`         | recent signals with a Mode column (off / test / armed) so the operator can tell Test fills from real Armed submissions at a glance |
| `/metrics`         | accepted/rejected counts, rejection reasons, by-symbol breakdown, past orders |
| `/logs`            | tail of `logs/signalbridge.log` |
| `/system`          | app name/version, host/port, db & log paths, cwd, broker, mode, `.env` loaded?, runtime status, useful local URLs |
| `/settings/profile`| change dashboard admin username + password (PBKDF2-SHA256 hash stored in SQLite) |

Every page has a top-bar kill-switch button. Execution state, the funded/eval badge, and the Topstep connection status live on the Dashboard execution card.

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
6. **Execution dispatch** branches on the state set in the Dashboard:
   - **Off** — no broker call. The signal is journaled as accepted with `message="execution_off_no_submission"` and SignalBridge returns.
   - **Test** — Topstep adapter builds the `/api/Order/place` payload, validates the contract id, logs the build, returns `submitted=false, mode="test"`. Never POSTs.
   - **Armed** — Topstep adapter runs its armed gate stack (credentials, numeric account id, canTrade, kill switch, ALLOWED_SYMBOLS_ARMED, MAX_CONTRACTS_PER_TRADE) and POSTs `/api/Order/place`. The ProjectX response is journaled and surfaced in the dashboard.
7. **Journal** writes one row per signal (accepted or rejected) to SQLite. The Mode column on `/journal` distinguishes Off / Test / Armed entries.
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
> The execution-model-collapse (2026-05-21) rework removed every pre-collapse
> `paper` / `demo` / `live` setting and renamed `LIVE_ALLOWED_SYMBOLS` to
> `ALLOWED_SYMBOLS_ARMED`. The boot-time schema check refuses to start
> against a SQLite database that still carries any of the removed keys —
> if you see `pre-collapse SQLite schema detected` in the log, delete
> `data/signalbridge.db` and restart so a fresh database can be
> bootstrapped from `.env`. The operator's journal export from the
> pre-flight checklist is the source of truth for prior history.

Dashboard-editable keys today: `EXECUTION_MODE` (via the Dashboard mode
dropdown), `SELECTED_ACCOUNT_ID`, `TRADINGVIEW_WEBHOOK_SECRET`,
`ALLOWED_SYMBOLS`, `ALLOWED_SYMBOLS_ARMED`, `MAX_CONTRACTS_PER_TRADE`,
`STRATEGY_MANAGED_RISK`, `FIXED_CONTRACTS_PER_TRADE`, `MAX_DAILY_LOSS`,
`MAX_OPEN_POSITIONS`, `ENABLE_LONGS`, `ENABLE_SHORTS`,
`DUPLICATE_ORDER_COOLDOWN_SECONDS`, `ENABLE_TIMEFRAME_LOCK`,
`ALLOWED_TIMEFRAMES`, `TOPSTEP_USERNAME`, `TOPSTEP_API_KEY`,
`ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`.

Runtime-applied immediately: webhook secret, execution mode, all risk
limits, allowlists, longs/shorts toggles, duplicate cooldown, Topstep
credentials.
Restart-required: `APP_HOST`, `APP_PORT`, `BROKER_PROVIDER` (pinned to
topstep post-collapse).

**Broker credentials.** `TOPSTEP_USERNAME` and `TOPSTEP_API_KEY` can be
persisted from `/settings/broker`. `TOPSTEP_PASSWORD` stays env-only —
ProjectX authenticates via the API key, the password is unused once
the key is configured.

All env defaults (see `.env.example` for the full list):

| Variable | Purpose |
| --- | --- |
| `APP_HOST`, `APP_PORT`              | bind address (default `127.0.0.1:8000`) |
| `ADMIN_AUTH_ENABLED`                | enable dashboard login (default `true`) |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD`  | admin credentials for `/login` |
| `SESSION_SECRET`                    | signing key for the session cookie |
| `EXECUTION_MODE`                    | `off` (default), `test`, or `armed` |
| `BROKER_PROVIDER`                   | pinned to `topstep`; other values rejected |
| `TRADINGVIEW_WEBHOOK_SECRET`        | shared secret in the alert body |
| `ALLOWED_SYMBOLS`                   | comma-separated allowlist (applies in every state) |
| `ALLOWED_SYMBOLS_ARMED`             | stricter subset applied only when execution is Armed |
| `MAX_CONTRACTS_PER_TRADE`           | hard cap per trade; applies uniformly in Test and Armed |
| `STRATEGY_MANAGED_RISK`             | `true` (default) → sizing comes from the alert's `contracts`; `false` → use `FIXED_CONTRACTS_PER_TRADE`. See [docs/risk.md](docs/risk.md). |
| `FIXED_CONTRACTS_PER_TRADE`         | quantity used when `STRATEGY_MANAGED_RISK=false` (must be ≤ `MAX_CONTRACTS_PER_TRADE`) |
| `MAX_DAILY_LOSS`                    | daily realized PnL floor |
| `MAX_OPEN_POSITIONS`                | concurrent open positions cap |
| `ENABLE_LONGS`, `ENABLE_SHORTS`     | direction toggles |
| `ENABLE_KILL_SWITCH`                | master switch for the kill switch feature (env-only, restart required) |
| `DUPLICATE_ORDER_COOLDOWN_SECONDS`  | reject re-sent `order_id`s inside this window |
| `TRADING_DAY_TIMEZONE`              | timezone for the daily-PnL boundary (env-only, restart required) |
| `DATABASE_PATH`, `LOG_PATH`, `LOG_LEVEL` | storage paths and log verbosity |
| `TOPSTEP_USERNAME`, `TOPSTEP_API_KEY`, `TOPSTEP_ACCOUNT_ID`, `TOPSTEP_ENV` | ProjectX credentials; username + API key editable from `/settings/broker` |
| `TOPSTEP_PASSWORD`                  | env-only; unused once `TOPSTEP_API_KEY` is configured |
| `TOPSTEP_BASE_URL`, `TOPSTEP_WS_URL` | ProjectX endpoints |
| `TOPSTEP_TOKEN`, `TOPSTEP_TOKEN_EXPIRES_AT` | adapter-managed auth cache — never user-editable |

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
| `POST /api/broker/flatten-all`        | flatten every open Topstep position (Armed only) |
| `POST /api/execution/off`             | set execution state to Off |
| `POST /api/execution/test`            | set execution state to Test |
| `POST /api/execution/arm`             | set execution state to Armed (runs the gate-stack check first) |
| `POST /api/execution/submit-test-order` | build a synthetic 1-contract test order against ProjectX without POSTing |
| `GET  /api/system`                    | host/port, paths, runtime status, useful local URLs |
| `POST /webhooks/tradingview`          | the inbound alert endpoint |

`POST /api/broker/test-connection` runs a real `/api/Auth/loginKey` + `/api/Account/search` round-trip and returns `200` with `ok: true` on success. Topstep failure envelopes carry a documented `status` field (`missing_credentials`, `auth_failed`, `non_numeric_account_id`, `network_error`, ...) at HTTP 200 with `ok: false`; genuine server errors come back as `500`.

---

## Flatten / cancel-all

When execution is **Armed** and Topstep is holding open positions, the
Dashboard's **Flatten All Positions** button POSTs `/api/broker/flatten-all`
which calls `flatten_position()` on the Topstep adapter. The adapter
queries `/api/Position/searchOpen`, then POSTs one
`/api/Position/closeContract` per open leg and reports a per-leg
envelope.

The kill switch is bypassed for flatten — closing existing state
remains available after emergency stop. Off and Test states refuse
flatten with a `not_armed` envelope: positions live on Topstep's side
and must be closed through a real broker call, which only happens in
Armed.

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

- **Armed execution submits real orders to your Topstep account.** Eval (Combine / Practice / Express) accounts behave the same as funded for SignalBridge's purposes — both are real ProjectX orders, both fill against your real account state, both deserve the same care. The Dashboard renders a `Funded` or `Eval` badge next to the Armed state so you always see which one is wired up.
- **Default state is Off.** First boot and every restart land in Off; the operator has to deliberately switch to Test or Armed.
- **Verify the safety knobs before arming.** `MAX_CONTRACTS_PER_TRADE` (hard cap), `ALLOWED_SYMBOLS_ARMED` (which symbols can submit when Armed — defaults to micros only), and the selected Topstep account all gate live submissions. The Dashboard's "Cannot Arm" line surfaces blockers before you click.
- `TOPSTEP_USERNAME` and `TOPSTEP_API_KEY` can be saved from the UI. `TOPSTEP_PASSWORD` stays env-only. The UI never echoes raw API-key values back; it only shows the last four characters.
- The kill switch is on by default — create `data/kill_switch.active`, click the top-bar button, or click Activate on `/settings/risk` to halt new Armed orders. The kill switch is consulted only when execution is Armed (Off and Test ignore it).
- The webhook secret is the only check on `/webhooks/tradingview` — use a long random string and never commit it.
- This is a single-user local app. Dashboard auth (above) gates the UI and admin API; the webhook stays open by design but is shared-secret protected.
