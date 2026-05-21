# SignalBridge Operational Audit — 2026-05-21

Scope: operator-experience audit. Read-only pass. Finds places where a
non-developer running SignalBridge as a shipped product would get stuck,
silently disagree with the system, or lack a UI path to a setting that
controls real money.

## Executive Summary

The functional pass is in good shape — code paths are correct, the
secret-handling is solid, and live-execution arming has a coherent
multi-gate flow. The **operator-facing** layer is not. The dominant
finding is a class of "ghost settings": values in `MANAGED_KEYS` (i.e.
persisted to SQLite, no longer readable from `.env` after first boot)
that have **no UI display, no UI edit form, and no admin API for the
operator to inspect or change**. The canonical example, the bug that
prompted this audit, is `LIVE_MAX_CONTRACTS_PER_TRADE`: it controls how
many contracts can be sent on a real funded order, it's persisted on
first boot, and the only way to change it once stored is editing
SQLite. There are at least five other settings in the same shape, three
of which directly gate live trading.

Severity distribution: **6 critical, 7 high, 4 medium, ~20 low**. Top
critical findings are documented in Section 1.

Dashboard reactivity is also weak: outside of the kill-switch toggle
and the dashboard-execution-card refresh-after-action, every page is a
"reload to see new state" surface. Polls do not exist; the only
periodic-ish refresh is the `_fetchOrderHistory()` on `/metrics` page
load.

Path to shippable: blocked. Two of the gaps under Section 5 are
non-negotiable for a non-developer user — every live-execution gate
must be visible+editable in the UI, and the README must stop claiming
"no live orders" when live execution code exists and works.

## Section 1: Settings / Configuration Architecture

### Definitions

- **Pydantic field**: the attribute name on `Settings`
  (`app/config.py:84-324`).
- **MANAGED_KEYS**: persisted to SQLite after first boot
  (`app/settings_store.py:23-78`). Once in SQLite, `.env` edits to the
  same key have no effect — `initialize_settings_from_env`
  (`app/settings_store.py:617-634`) overlays the stored value on top
  of the env default. This is the source of the
  `LIVE_MAX_CONTRACTS_PER_TRADE` bug class.
- **RUNTIME_APPLICABLE**: takes effect immediately on the in-memory
  `Settings` object (`app/settings_store.py:84-131`).
- **RESTART_REQUIRED**: persisted but only honored after restart
  (`app/settings_store.py:134-136`).

### The big table

(Severities defined in the audit brief.)

| Key | Pydantic field | Default (.env.example) | MANAGED | RUNTIME | RESTART | Dashboard edit | Dashboard read | API write | API read | Severity |
|---|---|---|---|---|---|---|---|---|---|---|
| `APP_HOST` | `app_host` | `127.0.0.1` | yes | no | yes | NONE | `/system` | NONE | `/api/system` | medium |
| `APP_PORT` | `app_port` | `8000` | yes | no | yes | NONE | `/system` | NONE | `/api/system` | medium |
| `APP_NAME` | `app_name` | `SignalBridge` | no | n/a | n/a | NONE | `/system`, base header | NONE | `/api/system` | low |
| `EXECUTION_MODE` | `execution_mode` | `paper` | yes | yes | no | `/` dashboard mode select; arm endpoints | `/`, `/settings/broker`, `/system` | `POST /api/execution/apply-mode`, `/api/topstep/{demo,live}-execution/enable` | `/api/status` | low |
| `BROKER_PROVIDER` | `broker_provider` | `paper` | yes | no | yes | `/settings/broker` form | `/`, `/settings/broker`, `/system` | `POST /settings/broker` | `/api/status` | low |
| `BROKER` (legacy alias) | `broker` | `paper` | no | n/a | n/a | NONE | NONE | NONE | NONE | low |
| `SELECTED_ACCOUNT_ID` | `selected_account_id` | (empty) | yes | yes | no | `/settings/broker` dropdown | `/`, `/settings/broker` | `POST /api/topstep/select-account`, `/settings/broker` | `/api/status`, `/api/broker/status` | low |
| `TRADINGVIEW_WEBHOOK_SECRET` | `webhook_secret` | placeholder | yes | yes | no | `/tradingview` (set + regenerate) | `/tradingview` (full value), `/system` (set/unset) | `POST /tradingview/secret`, `/tradingview/secret/regenerate` | NONE | low |
| `ALLOWED_SYMBOLS` | `allowed_symbols` | `MNQ1!,MES1!,NQ1!,ES1!` | yes | yes | no | `/settings/risk` (legacy hidden field) | `/` exec card via state, `/settings/risk` cards | `POST /settings/risk` | `/api/status` | **medium** |
| `MAX_CONTRACTS_PER_TRADE` | `max_contracts_per_trade` | `1` | yes | yes | no | `/settings/risk` | `/settings/risk` | `POST /settings/risk` | `/api/status` | low |
| `STRATEGY_MANAGED_RISK` | `strategy_managed_risk` | `true` | yes | yes | no | `/settings/risk` | `/settings/risk` | `POST /settings/risk` | `/api/status` | low |
| `FIXED_CONTRACTS_PER_TRADE` | `fixed_contracts_per_trade` | `1` | yes | yes | no | `/settings/risk` | `/settings/risk` | `POST /settings/risk` | `/api/status` | low |
| `MAX_DAILY_LOSS` | `max_daily_loss` | `250` | yes | yes | no | `/settings/risk` | `/settings/risk` | `POST /settings/risk` | NONE | low |
| `MAX_OPEN_POSITIONS` | `max_open_positions` | `1` | yes | yes | no | `/settings/risk` | `/settings/risk` | `POST /settings/risk` | NONE | low |
| `ENABLE_LONGS` | `enable_longs` | `true` | yes | yes | no | `/settings/risk` | `/settings/risk` | `POST /settings/risk` | NONE | low |
| `ENABLE_SHORTS` | `enable_shorts` | `true` | yes | yes | no | `/settings/risk` | `/settings/risk` | `POST /settings/risk` | NONE | low |
| `ENABLE_KILL_SWITCH` | `enable_kill_switch` | `true` | **no** | n/a | n/a (boot-time) | NONE | `/settings/risk` (badge in card), `/system` (indirect), exec-card "disabled (config)" badge | NONE | NONE | **high** |
| `DUPLICATE_ORDER_COOLDOWN_SECONDS` | `duplicate_order_cooldown_seconds` | `300` | yes | yes | no | `/settings/risk` | `/settings/risk` | `POST /settings/risk` | NONE | low |
| `ENABLE_TIMEFRAME_LOCK` | `enable_timeframe_lock` | `false` | yes | yes | no | `/settings/risk` | `/settings/risk` | `POST /settings/risk` | NONE | low |
| `ALLOWED_TIMEFRAMES` | `allowed_timeframes` | `1` | yes | yes | no | `/settings/risk` | `/settings/risk` | `POST /settings/risk` | NONE | low |
| `TRADING_DAY_TIMEZONE` | `trading_day_timezone` | `UTC` | no | n/a | yes (read once at boot by `Journal`, `app/main.py:289-292`) | NONE | NONE | NONE | NONE | **high** |
| `DATABASE_PATH` | `database_path` | `data/signalbridge.db` | no | n/a | yes | NONE | `/system` | NONE | `/api/system` | medium |
| `LOG_PATH` | `log_path` | `logs/signalbridge.log` | no | n/a | yes | NONE | `/system`, `/logs` | NONE | `/api/system` | low |
| `LOG_LEVEL` | `log_level` | `INFO` | no | n/a | yes | NONE | `/system` | NONE | `/api/system` | medium |
| `SYMBOLS_MAP_PATH` | `symbols_map_path` | `config/symbols.json` | no | n/a | yes | NONE | `/settings/symbols` shows path; not editable | NONE | NONE | low |
| `WEBHOOK_RATE_LIMIT_PER_SECOND` | `webhook_rate_limit_per_second` | `10` | no | n/a | yes (the `TokenBucket` is constructed once at boot, `app/main.py:2911-2914`) | NONE | NONE | NONE | NONE | **high** |
| `WEBHOOK_RATE_BURST` | `webhook_rate_burst` | `30` | no | n/a | yes (see above) | NONE | NONE | NONE | NONE | **high** |
| `ADMIN_AUTH_ENABLED` | `admin_auth_enabled` | `true` | no | n/a | yes (middleware mounted once at `app/main.py:335-346`) | NONE | login form behavior is observable; otherwise NONE | NONE | NONE | medium |
| `ADMIN_USERNAME` | `admin_username` | `admin` | yes | yes | no | `/settings/profile` | `/settings/profile` | `POST /settings/profile` | NONE | low |
| `ADMIN_PASSWORD` (plaintext fallback) | `admin_password` | `change_me_admin_password` | no | n/a | n/a (cleared in-memory on first hash write) | indirect via `/settings/profile` | NONE | indirect | NONE | low |
| `ADMIN_PASSWORD_HASH` | `admin_password_hash` | (empty) | yes | yes | no | `/settings/profile` | indirect (page shows whether hash is set) | `POST /settings/profile` | NONE | low |
| `SESSION_SECRET` | `session_secret` | placeholder | no | n/a | yes (middleware mounts once) | NONE | NONE | NONE | NONE | medium |
| `TOPSTEP_USERNAME` | `topstep_username` | (empty) | yes | yes | no | `/settings/broker` | `/settings/broker` (masked) | `POST /settings/broker` | `/api/broker/status` (indirect) | low |
| `TOPSTEP_PASSWORD` | `topstep_password` | (empty) | **no** | n/a | yes (only env-driven) | NONE | `/settings/broker` shows "password_set" via the read path | NONE | NONE | **high** |
| `TOPSTEP_API_KEY` | `topstep_api_key` | (empty) | yes | yes | no | `/settings/broker` | `/settings/broker` (last-4 only) | `POST /settings/broker` | NONE | low |
| `TOPSTEP_ACCOUNT_ID` | `topstep_account_id` | (empty) | yes | yes | no | indirect (mirrored from `SELECTED_ACCOUNT_ID` for topstep) | `/settings/broker` | `POST /api/topstep/select-account`, `POST /settings/broker` | `/api/broker/status` | low |
| `TOPSTEP_ENV` | `topstep_env` | `demo` | yes | yes | no | `/settings/broker` (hidden — `topstep_env` form field exists at `app/main.py:3231` but is not in the rendered template) | NONE | `POST /settings/broker` | NONE | **medium** |
| `TOPSTEP_BASE_URL` | `topstep_base_url` | `https://api.topstepx.com` | yes | yes | no | NONE (removed in polish pass, `app/main.py:3233-3237` comment) | `/settings/broker` (read-only) | NONE | NONE | medium |
| `TOPSTEP_WS_URL` | `topstep_ws_url` | `https://rtc.topstepx.com` | yes | yes | no | NONE (removed in polish pass) | `/settings/broker` (read-only) | NONE | NONE | medium |
| `TOPSTEP_TOKEN` | `topstep_token` | (empty) | yes | yes | no | NONE (adapter writes) | indirect ("token cached" badge) | NONE | NONE | low |
| `TOPSTEP_TOKEN_EXPIRES_AT` | `topstep_token_expires_at` | (empty) | yes | yes | no | NONE (adapter writes) | `/settings/broker` (truncated) | NONE | NONE | low |
| `ENABLE_TOPSTEP_ORDER_DRY_RUN` | `enable_topstep_order_dry_run` | `true` | yes | yes | no | NONE | NONE (not surfaced in any template) | NONE | NONE | **high** |
| `ENABLE_TOPSTEP_ORDER_EXECUTION` | `enable_topstep_order_execution` | `false` | yes | yes | no | dashboard execution-card mode select; arm endpoints | indirect via exec-card state | `POST /api/execution/apply-mode`, `POST /api/topstep/{demo,live}-execution/enable` | `/api/broker/status` | low |
| `TOPSTEP_EXECUTION_CONFIRM` | `topstep_execution_confirm` | `disabled` | yes | yes | no | arm endpoints only | indirect | arm endpoints | `/api/broker/status` | low |
| `ENABLE_LIVE_TRADING` | `enable_live_trading` | `false` | yes | yes | no | dashboard live-engagement modal | exec card state, `/api/broker/status` | `POST /api/topstep/live-execution/{enable,disable}` | `/api/broker/status` | low |
| `LIVE_TRADING_CONFIRM` | `live_trading_confirm` | `disabled` | yes | yes | no | live arm endpoint only | NONE | live arm endpoints | NONE | low |
| `LIVE_TRADING_ACCOUNT_ACK` | `live_trading_account_ack` | `false` | yes | yes | no | live arm endpoint only (ack checkbox) | NONE | live arm endpoints | NONE | low |
| `LIVE_MAX_CONTRACTS_PER_TRADE` | `live_max_contracts_per_trade` | `1` | yes | yes | no | **NONE** | **NONE** | NONE | NONE | **critical** |
| `LIVE_ALLOWED_SYMBOLS` | `live_allowed_symbols` | `MES1!,MNQ1!` | yes | yes | no | **NONE** | NONE in any page (surfaced only in the live-verify JSON envelope) | NONE | `/api/topstep/live-execution/verify` (read-only) | **critical** |
| `LIVE_REQUIRE_KILL_SWITCH_OFF` | `live_require_kill_switch_off` | `true` | yes | yes | no | **NONE** | **NONE** | NONE | NONE | **critical** |
| `ORDER_HISTORY_LOOKBACK_DAYS` | `order_history_lookback_days` | `7` | yes | yes | no | NONE | `/metrics` (default value in dropdown init) | NONE | passed as query param | medium |
| `ORDER_HISTORY_LIMIT` | `order_history_limit` | `100` | yes | yes | no | NONE | `/metrics` (default) | NONE | passed as query param | medium |
| `ENABLE_TOPSTEP_REALTIME` | `enable_topstep_realtime` | `false` | yes | yes | no | NONE | `/settings/broker` (label string only) | NONE | `/api/realtime/state` | medium |
| `TOPSTEP_REALTIME_MODE` | `topstep_realtime_mode` | `polling` | yes | yes | no | NONE | `/settings/broker` (label) | NONE | `/api/realtime/state` | medium |
| `TOPSTEP_REALTIME_POLL_SECONDS` | `topstep_realtime_poll_seconds` | `5` | yes | yes | no | NONE | `/settings/broker` (label) | NONE | `/api/realtime/state` | medium |
| `SIGNALBRIDGE_ALLOW_INSECURE_BOOT` | (env-only) | unset | no | n/a | yes (boot-time check) | NONE | NONE | NONE | NONE | low |
| `SIGNALBRIDGE_ALLOW_PUBLIC_NO_AUTH` | (env-only) | unset | no | n/a | yes (boot-time check) | NONE | NONE | NONE | NONE | low |

### Critical findings

These are the rows where the operator can be silently wrong about a
setting that controls live-money behavior, with no path through the UI
to inspect or change it.

1. **`LIVE_MAX_CONTRACTS_PER_TRADE` is invisible.** Persisted on first
   boot by `initialize_settings_from_env` (`app/settings_store.py:617-634`).
   Enforced as a hard cap in `submit-live-test-order`
   (`app/main.py:2438-2451`) and in the Topstep adapter safety check.
   Surfaced as a number in the live-verify response
   (`app/main.py:1543-1558`), in the live-execution-armed response
   (`app/main.py:1375-1380`), and in the audit-log line at
   `app/main.py:1306-1315`. **Not displayed on any page; not editable
   from any form.** Operator changes `.env`; nothing happens; cap stays
   at 1; live signals with size >1 are rejected with
   `live_contracts_above_max`; operator has no way to discover why
   without grepping the SQLite `settings` row. This is the canonical
   bug, and it is currently in production.

2. **`LIVE_ALLOWED_SYMBOLS` is invisible.** Same shape: persisted on
   first boot, enforced at `app/main.py:2467-2479`, surfaced only in
   the live-verify JSON (`app/main.py:1557`) and the audit log. **No
   page renders this list.** If the operator wants to trade NQ1!/ES1!
   live, the only path is editing SQLite. The default is micros-only
   (`MES1!,MNQ1!`), which fails closed safely — but the operator can
   never discover or change it.

3. **`LIVE_REQUIRE_KILL_SWITCH_OFF` is invisible.** Defaults true,
   enforced at `app/main.py:1234-1248` in the arm flow and in the live
   safety check. **No UI surface at all.** A reasonable operator could
   want this off (e.g. for testing arm flows) and would have no way to
   change it.

4. **`ENABLE_TOPSTEP_ORDER_DRY_RUN` is invisible.** Default true.
   Persisted to SQLite. Surfaces in `broker_status_payload` at
   `app/dashboard.py:757`. **No template references it.** Toggling
   this off in `.env` post-bootstrap has no effect.

5. **`ENABLE_KILL_SWITCH` is invisible from the UI but is read at
   boot.** `app/config.py:133-135` reads it; not in `MANAGED_KEYS`, so
   the `.env` value is authoritative across restarts. The exec card on
   `/` does show a "disabled (config)" warning badge when it's false
   (`app/templates/dashboard.html:78-82`), but **the operator cannot
   enable or disable the kill switch from the dashboard** — only via
   `.env` + restart. The "Activate kill switch" buttons on `/` and
   `/settings/risk` just toggle the *runtime active flag*, not the
   feature.

6. **`TRADING_DAY_TIMEZONE` is invisible and consumed only at boot.**
   Threaded into the `Journal` constructor (`app/main.py:289-292`),
   never overridable from SQLite, never displayed anywhere. An
   operator who configures their daily-PnL window to `America/New_York`
   in `.env`, then later changes it through the UI thinking there's a
   field — there is no field, so nothing happens. The README mentions
   it; that's the only place.

### High-severity findings

- **`TOPSTEP_PASSWORD` cannot be persisted via the UI.** The
  `/settings/broker` form (`app/main.py:3225-3232`) accepts
  `topstep_username` and `topstep_api_key` but not password. The
  Settings field exists (`app/config.py:153`) but `TOPSTEP_PASSWORD`
  is **not in `MANAGED_KEYS`** (`app/settings_store.py:23-78`) — so
  the only path is `.env`. The settings_broker.html shows
  `topstep.password_set` as a state badge but never offers an edit
  field. The README claims credentials can be persisted via the UI;
  password is an exception.

- **`WEBHOOK_RATE_LIMIT_PER_SECOND` / `WEBHOOK_RATE_BURST` are
  hot-reload broken.** The `TokenBucket` is constructed once at boot
  (`app/main.py:2911-2914`). They are not in `MANAGED_KEYS`, so a
  `.env` edit + restart is the only path. If an operator hits a
  surprise burst and wants to relax the bucket, they cannot do it
  without a restart.

- **`TOPSTEP_BASE_URL` / `TOPSTEP_WS_URL` cannot be edited from the
  UI.** Both are in `MANAGED_KEYS`, but the form fields were removed
  in the polish pass (`app/main.py:3233-3237` comment confirms). If
  ProjectX migrates endpoints, the only path is SQLite or
  `.env`-with-the-stored-value-already-overriding-it.

- **`TOPSTEP_ENV` Form param is accepted but no input renders for it.**
  `app/main.py:3231` declares `topstep_env: str = Form("demo")`, so the
  form always silently rewrites the persisted value to `"demo"` on
  every save. If an operator ever sets it to anything else (the
  coercer rejects `live` anyway, `app/settings_store.py:336-350`), the
  next broker-form save reverts it.

- **`ALLOWED_SYMBOLS` is a hidden legacy field.** The risk-form post
  handler (`app/main.py:3429`) accepts it as `Optional[str] =
  Form(None)`. The risk-form template no longer renders the input
  (`app/templates/settings_risk.html` has no `name="allowed_symbols"`
  field). So the form save silently leaves it unchanged on the
  positive path — but a stale browser tab that did include the input
  would still update it. There is no clean path to change which
  symbols are allowed.

## Section 2: Dashboard Reactivity

### Per-page element table

`/` (Dashboard, `app/templates/dashboard.html`):

| Element | Data source | Update mechanism | Staleness |
|---|---|---|---|
| Execution-card mode | `settings.execution_mode` via `_page_ctx` | server render; in-page `refreshFromStatus(payload)` after Apply / Disengage / Engage Live | up-to-date after explicit action; otherwise as old as the page render |
| Account row in exec card | `settings.resolved_account_id` + broker status | server render only — explicit "do NOT update the execution-account block" comment at `app/templates/dashboard.html:548-550` | until reload |
| Flatten button disabled state | `broker_open_position_count` from dashboard summary | server render; re-evaluated client-side after flatten completes (`setFlattenButtonEnabled`) | until next reload, or until a flatten completes |
| Trading session badge | computed at render in `current_trading_session()` | server render; **no periodic refresh** | as old as the page; an operator who keeps the dashboard open through session boundaries sees the wrong session |
| App status badge | `kill_switch.is_active()` | server render; in-page mirror via `signalbridge:kill-switch-change` event | mirrored — fresh |
| Broker provider badge | `settings.resolved_provider` + `broker.provider` | server render; `refreshFromStatus` updates the badge after Apply | stale until apply |
| Broker connection badge | `broker_connected` | server render; `refreshFromStatus` updates it | stale until apply |
| Trades today / Accepted / Rejected / P&L cards | journal counts | **server render only** | stale until reload |
| Open orders table (last 10) | `broker.get_orders()` via dashboard summary | **server render only** | stale until reload |
| Last signal / Last rejection cards | journal latest | **server render only** | stale until reload |
| Win rate / Total points % | journal closed-trade stats | **server render only** | stale until reload |
| Ticker Watch placeholder | `symbol_map.all_mappings()` | static placeholder card; "not connected yet" | n/a |

`/settings/broker`:

| Element | Data source | Update mechanism | Staleness |
|---|---|---|---|
| Selected provider badge | `configured_provider` | server render only | until reload |
| Broker connection badge | `broker_status` | `_applyConnectionBadge` after Test/Auth/Accounts button | stale until button click |
| Selected account snapshot | `broker_status.selected_account` | `_refreshBrokerSnapshot()` after Use-this-account; explicit click | stale until action |
| Account dropdown | initial from server; live populated from `_renderAccountsList` | only updates after Fetch accounts click | stale until click |
| Test output `<pre>` | last button click | replaced per click | n/a |
| Topstep credential masks | server render | only refreshes on page reload | until reload |

`/settings/risk`:

| Element | Data source | Update mechanism | Staleness |
|---|---|---|---|
| All four header cards | server render | none | until reload |
| Form inputs | server render | none | until reload |
| Kill-switch badge | server render | `_applyKsState(active)` on POST response and on `signalbridge:kill-switch-change` event | fresh |

`/settings/symbols`, `/settings/profile`: pure form pages — no live state.

`/tradingview`:

| Element | Data source | Update mechanism | Staleness |
|---|---|---|---|
| Secret status (set/unset) + preview | server render | only updates after a save/regenerate POST+redirect cycle | until reload |
| Secret input value | server render | until reload | until reload |
| Test webhook result | per-button-click | replaced on click | n/a |

`/journal`, `/metrics`:

| Element | Data source | Update mechanism | Staleness |
|---|---|---|---|
| Journal tables | server render (via `journal_view`) | until reload | until reload |
| Metrics counters / charts | server render via `metrics_summary` | until reload | until reload |
| Past orders / order history table | client-side fetch on page load, plus button + select | refreshes on `/metrics` page mount and on lookback dropdown change; no auto-poll | up-to-date after click |

`/logs`: tail rendered server-side at request time; no auto-poll
(`app/main.py:3769-3778`). Reload to see new lines.

`/system`: every field is static at request time.

### Reactivity gaps

- **No page polls** for new signals, fills, or PnL. The dashboard is
  effectively a snapshot of the journal at the time you opened the
  tab. A signal that fires while you have `/` open does not appear
  until you reload. This is the single biggest operator experience gap
  — a "live status" dashboard that is not live.
- **The trading-session badge is computed at render time.** The
  `data-session-time` element has no clock update. An operator who
  leaves the dashboard open from London open through NY close sees
  "London" in their badge all day.
- **The selected-account row in the execution card is intentionally
  not updated by `refreshFromStatus`**
  (`app/templates/dashboard.html:548-550`). If the operator changes
  the account in another tab and switches back to `/`, the account
  shown is stale until reload.
- **No SSE/WebSocket anywhere.** Confirmed by grep. The
  `signalbridge:kill-switch-change` `CustomEvent` is the only in-page
  pub-sub, and it is single-page.
- **`/api/realtime/state` exists but no template consumes it.** It
  ships open positions and order rows with a `refreshed_at` timestamp,
  but no JS in the repo subscribes to it. Operator-facing realtime is
  zero.
- **The journal-table pages (`/journal`, `/metrics`) have no
  auto-refresh.** A live test fires signals, the operator opens
  `/journal` to watch the trail, has to manually reload after every
  alert.

## Section 3: Operational Gotcha Catalog

### .env / DB / "the value I set in .env isn't taking effect"

- **The class of bug**: any key in `MANAGED_KEYS`
  (`app/settings_store.py:23-78`) is bootstrapped from `.env` exactly
  once, at first boot of a fresh database. Subsequent `.env` edits to
  the same key have no effect — `initialize_settings_from_env`
  (`app/settings_store.py:617-634`) explicitly overlays the stored
  value back onto `Settings`. The README documents this generally
  ("`.env` provides defaults at first boot. The dashboard then
  persists any changes…") but it does not list which keys are
  affected, and it does not warn that some `MANAGED_KEYS` have no UI
  edit path (LIVE_MAX_CONTRACTS, LIVE_ALLOWED_SYMBOLS,
  LIVE_REQUIRE_KILL_SWITCH_OFF, ENABLE_TOPSTEP_ORDER_DRY_RUN,
  TOPSTEP_BASE_URL, TOPSTEP_WS_URL, TOPSTEP_ENV-ish,
  ORDER_HISTORY_LOOKBACK_DAYS, ORDER_HISTORY_LIMIT, ENABLE_TOPSTEP_REALTIME,
  TOPSTEP_REALTIME_MODE, TOPSTEP_REALTIME_POLL_SECONDS,
  TOPSTEP_TOKEN, TOPSTEP_TOKEN_EXPIRES_AT). Operators will hit this
  again. Impact: lost time / safety concern (the live caps are in this
  set).

### "I clicked Apply but the change didn't take effect"

- **Broker provider switch silently requires a restart.** Saving
  `BROKER_PROVIDER=topstep` from `/settings/broker` (`app/main.py:3265-3277`)
  flashes "Restart required to switch the active adapter" but the
  in-memory `broker` instance is unchanged. The dashboard
  `active_broker_provider` and `broker_provider` therefore diverge.
  Symptom: operator switches provider, clicks Test connection,
  paper-status response comes back because paper is still the active
  adapter. They blame their Topstep credentials. Impact: lost time.
  Root cause: `RESTART_REQUIRED` (`app/settings_store.py:134-136`).

- **`WEBHOOK_RATE_LIMIT_PER_SECOND` / `WEBHOOK_RATE_BURST` cannot be
  hot-reloaded.** The `TokenBucket` is constructed at
  `app/main.py:2911-2914` with the boot-time settings and never
  reconstructed. If the values were ever editable from the UI, edits
  would silently no-op. They're env-only, so today this is
  "restart-only" not "broken save," but it will become broken if the
  forms appear without a corresponding bucket rebuild.

- **`SESSION_SECRET` rotation requires a restart.** The
  `SessionMiddleware` is mounted once at `app/main.py:335-346`.

- **`ADMIN_AUTH_ENABLED` is read-once at boot.** Toggling it after
  boot has no effect — `SessionMiddleware` is mounted (or not)
  conditionally at boot.

### "The dashboard shows X, the log says Y" / display divergence

- **`enable_topstep_realtime` shows "Polling every Ns" on
  `/settings/broker`** (`app/templates/settings_broker.html` and the
  realtime_view dict at `app/main.py:3175-3185`) but the polling code
  is not wired up. No template consumes `/api/realtime/state`. The
  operator sees a label that promises polling that doesn't happen.

- **Webhook test result label is correct but the underlying
  short-circuit decision uses an `order_id` of literally
  `"webhook-test-"`** (`app/main.py:2950-2952` — note the trailing
  dash). Not user-visible because the short-circuit returns early, but
  if the test ever stops short-circuiting, the journal row would have
  a malformed order_id.

- **Execution mode dropdown on dashboard relabels `paper` as
  `Execution Test`** (`app/templates/dashboard.html:40`) but the
  underlying value is `paper`. An operator who reads logs that say
  `mode=paper` then looks at the dashboard for "paper" will see
  "Execution Test." Cosmetic, but confusing.

- **`active_broker_provider` vs `broker_provider`** when a restart is
  pending. Dashboard exec card shows the *configured* provider; some
  badges below show the active one. Until restart they disagree.

### Two-databases / stray files

- **Stray root-level `signalbridge.db`** has been created at least
  once. Already in `.gitignore` (`.gitignore` line 30:
  `signalbridge.db`). The commit history (`d13b304: chore: commit
  audit doc, ignore stray root-level signalbridge.db`) confirms this
  is a known recurring issue. Most likely cause: the
  `DATABASE_PATH=data/signalbridge.db` env default is resolved
  relative to `cwd`; if the launcher is invoked from a different
  directory the path is interpreted differently. But `Settings.database_abs_path`
  (`app/config.py:352-355`) resolves to `PROJECT_ROOT / path` for
  relative paths, which is consistent. Probably a one-off from a
  previous boot before the abs-path resolution landed. Impact: minor.

### "I clicked the button twice and weird things happened"

- **`/api/topstep/live-execution/enable` is not idempotent at the
  journal level** but **is** idempotent at the settings level
  (`app/main.py:1250-1296`). A double-submit during the engagement
  animation writes two `LIVE_ARMED` audit rows. The flow's UI prevents
  it (busy state, form submit handler) but the API endpoint accepts
  it.

- **Webhook handler holds per-`order_id` locks but never evicts them**
  (`app/webhook.py:150-178`). Entries are ~100 bytes; cardinality is
  bounded by unique order_ids ever seen. Long-running instance + many
  unique alerts → unbounded growth. The comment acknowledges it
  ("Entries are never evicted — each is ~100 bytes and order_id
  cardinality is bounded"). For a shipped product running months at a
  time, this should at least be a TTL-keyed dict.

### Settings-state misalignment

- **`STRATEGY_MANAGED_RISK=true` ignores `FIXED_CONTRACTS_PER_TRADE`**
  (`app/webhook.py:311-353`). The form (`app/templates/settings_risk.html`)
  disables the input but the value is still saved. An operator who
  saves a fixed value of 10 then turns strategy-managed off later
  immediately starts sending 10-contract orders. The cross-field check
  (`app/main.py:3459-3467`) enforces `fixed ≤ max` but does not warn
  about the disable-on-toggle gotcha.

- **`SELECTED_ACCOUNT_ID` and `TOPSTEP_ACCOUNT_ID` are mirrored** for
  the topstep provider (`app/main.py:3247-3248`). A direct
  `POST /api/topstep/select-account` (`app/main.py:2543-2584`) writes
  both. A direct `POST /settings/broker` for the paper provider does
  not touch `TOPSTEP_ACCOUNT_ID`. Behavior is consistent but
  non-obvious; if an operator switches provider, they're seeing the
  mirrored topstep account in `SELECTED_ACCOUNT_ID` even though
  they're back on paper.

### README / docs stale

- **README claims live execution is not implemented**: "Live execution
  is **not** implemented. The Topstep adapter raises
  `NotImplementedError` on `execute()`." That paragraph is from
  before this build's flatten + live arming were wired up. The Topstep
  adapter `submit_market_order` does real `/api/Order/place` posts
  now; `flatten_position` calls `/api/Position/closeContract`. Boot
  validation does NOT block live mode. The "Broker status today" table
  near the top also still reads "topstep is read-only/dry-run" which
  is no longer true. Impact: safety concern — the operator's mental
  model from the docs is wrong about the most dangerous part of the
  system.

- **README's "Dashboard-editable keys" list is missing every
  topstep/live/order-history/realtime key.** It lists only the legacy
  risk + provider keys. Operator who follows the README has no idea
  most settings are managed.

- **`/tradingview` page section header still says "Generic JSON
  template"** but that section was removed (per the master prompt for
  Pass 2). The collapsible only contains the Xiznit instructions. Not
  a bug but inconsistent with README ("generic JSON template" listed
  among what the page shows).

### Webhook payload edge cases (called out in the prompt)

- **`sl: <price>` vs `reason: "sl"`**: the Xiznit parser
  (`app/webhook_parser.py:162-164`) reads `sl` / `stop` / `stop_loss`
  / `new_sl` as the **stop level** (float), assigning it to
  `parsed.stop`. The `close_all` reasons live in `_CLOSE_ALL_REASONS`
  at `app/webhook_parser.py:47-53` and trigger only when
  `parsed.reason` (read from the explicit `reason` field at
  `app/webhook_parser.py:184`) equals one of `{"sl", "eod_flatten",
  ...}`. A strategy that emits `{"action":"exit","sl":5000.25}`
  without a `reason` field gets:
  - `action_class=EXIT`
  - `stop=5000.25`
  - `reason=None`
  - `tp_label=None`
  - `close_all=False`
  - `qty=None`
  - which fails `_handle_xiznit_exit` at `app/webhook.py:525-549`
    with `missing_exit_context`.
  This is the bug the operator described. The strategy intended "close
  on SL"; the parser saw "exit with a stop level field but no
  context." Fix shape: treat a present `stop`/`sl` numeric field on an
  EXIT action with no `qty` and no `tp_label` as `close_all=True`. But
  this audit is read-only — flagged here.

- **`_invalid_payload: None` rows in the journal.** When the request
  JSON parse fails (`app/main.py:3020-3023`), `payload = None`. The
  handler at `app/webhook.py:198-207` then records the rejection with
  `raw={"_invalid_payload": str(raw_payload)[:500]}` =
  `{"_invalid_payload": "None"}`. Operator sees a journal row whose
  raw_payload is the literal string `"None"` — that's TradingView
  delivering an empty / un-substituted body (template variable that
  resolved to nothing). The rejection log line is correctly
  `malformed_payload`, but the journal row's content is unhelpful.

- **TradingView template substitution can produce empty alerts.** If
  the alert body has unresolved `{{...}}` placeholders, TradingView
  sometimes posts an empty body. The handler treats this as
  `malformed_payload`. The operator has no in-product hint that this
  is a TradingView-side template issue rather than a SignalBridge
  issue.

### Backup / restore / data lifecycle

- **No mechanism to back up or restore SignalBridge state.** The
  whole product state is `data/signalbridge.db`. There's no export, no
  "factory reset," no migration tool. An operator who wants to clone
  their config to a new machine has to copy `.env` + the SQLite DB
  manually.

- **No way to "forget a setting" once in SQLite.** Deleting the row
  manually is the only path. The README documents this in passing
  ("Resetting a value back to its `.env` default means editing the
  `settings` row in SQLite"). For a non-developer this is
  impractical.

## Section 4: Test Coverage of Operational Concerns

### What's covered

- Boot validation: `test_boot_validation.py` covers the placeholder /
  short-secret refusal-to-boot flow + the public-no-auth escape
  hatches. Solid.
- Auth + session: `test_auth.py` covers login, password migration,
  redirect handling.
- Webhook rejections: `test_webhook.py`, `test_xiznit_webhook.py`,
  `test_webhook_rejection_logging.py` cover the rejection paths and
  redacted-preview logging.
- Risk engine: `test_risk_engine.py`, `test_strategy_managed_risk.py`,
  `test_max_open_positions_topstep.py`, `test_timeframe_lock.py`
  cover the per-check behaviors.
- Topstep flatten + live submit: `test_topstep_flatten.py`,
  `test_live_execution.py`.
- Dashboard rendering: `test_dashboard.py`,
  `test_dashboard_execution_card.py`, `test_dashboard_flatten.py`.
- UI layout cleanup: `test_layout_cleanup.py` —
  asserts the absence of removed surfaces.

### What's NOT covered

- **Any "is this MANAGED_KEY visible in the UI?" assertion.** No test
  iterates `MANAGED_KEYS` and checks for either a UI display or a
  documented "intentionally hidden" annotation. The
  `LIVE_MAX_CONTRACTS_PER_TRADE` bug would have been caught instantly
  by a property-style test that loops `MANAGED_KEYS` and asserts each
  is either readable through a template snapshot or listed in a
  curated "env-only" set.
- **The `.env` → SQLite bootstrap once-only behavior.** There is no
  test that mutates a stored value, restarts the app, edits `.env`,
  and asserts the stored value still wins. (The behavior is correct;
  it is undocumented and operator-confusing.)
- **README/docs vs. code drift.** No test reads the README's
  "Dashboard-editable keys" list and checks it against `MANAGED_KEYS`
  + the UI form fields. Could be a one-line glob test.
- **The "Apply mode" UI flow end-to-end.** Existing tests cover the
  `/api/execution/apply-mode` endpoint behavior; they don't assert
  that the JS in `dashboard.html` references actual endpoint names
  and response shape keys (`body.ok`, `body.execute`, etc.). The
  Apply-button bug from the recent debug pass would have been caught
  by a headless-browser test or even a regex test on the JS.

### Top 5 recommended test additions

1. **MANAGED_KEYS visibility property test** (highest impact,
   lowest effort). Iterate `MANAGED_KEYS`; for each key, render the
   relevant page (`/`, `/settings/broker`, `/settings/risk`,
   `/settings/symbols`, `/tradingview`) and assert that either the
   Pydantic-field name appears in the rendered HTML or the key is in
   a curated `EXPECTED_UI_INVISIBLE` set. Update the set deliberately
   when removing fields. This single test would have caught the
   bug-of-the-day.

2. **".env post-bootstrap is ignored" docs test.** Create a fresh
   SQLite, bootstrap from `.env`, mutate `.env`, call
   `initialize_settings_from_env` again, assert the in-memory value
   matches the SQLite-stored one (not the new `.env` one). Pin the
   behavior so a future refactor doesn't silently change it.

3. **README sync test.** Parse the "Dashboard-editable keys" list in
   `README.md`. Assert it equals `set(MANAGED_KEYS) -
   ENV_ONLY_KEYS`. Tiny test, prevents drift.

4. **Webhook empty-body / template-substitution rejection test.**
   Post `b""`, `b"null"`, and a body of literal `"{{...}}"` to
   `/webhooks/tradingview`. Assert each returns `malformed_payload`
   with a journal row that has a *meaningful* raw_payload field — not
   the bare string `"None"`. Forces a real diagnostic for the bug
   described in Section 3.

5. **Xiznit EXIT + sl-level fallback test.** Post
   `{"action":"exit","symbol":"MES1!","sl":"5000.25"}` to the webhook
   and assert the response is currently `missing_exit_context`. Then
   document the expected behavior change ("a numeric `sl` on EXIT
   with no qty should imply close_all") and leave the test marked
   `xfail` until the parser change ships. Pins the bug shape before
   the fix.

## Section 5: Path to Shippable

### Blockers (cannot ship as-is)

- **Make every live-execution gate visible+editable from the UI.**
  `LIVE_MAX_CONTRACTS_PER_TRADE`, `LIVE_ALLOWED_SYMBOLS`,
  `LIVE_REQUIRE_KILL_SWITCH_OFF`. Non-developer users will hit the
  exact bug we just fixed. *Severity: critical.*
- **Fix the README's "live execution not implemented" claim.** A
  non-developer reading the README assumes paper-only safety. The
  code routes real orders. *Severity: critical (safety).*
- **Reconcile `BROKER_PROVIDER` UX with restart requirement.** Either
  rebuild the broker adapter on save (true hot-reload) or add a "your
  changes will take effect after restart" interstitial that explains
  what to do — the current flash banner is not enough. Non-developer
  users will keep clicking Test connection and not understand why it
  still says paper. *Severity: high.*
- **Backup / restore.** Operator needs a one-button "export my config
  + journal" and a corresponding restore path. Otherwise their first
  laptop replacement loses their state. *Severity: high.*

### Polish — needed but not blockers

- **Add periodic refresh (poll every N seconds, configurable, default
  off) to the dashboard's signal counters, Open orders table, P&L
  cards, and Trading session badge.** SSE/WebSocket is overkill;
  `setInterval` calling `fetchStatus` would close most of this gap.
- **Surface the `/api/realtime/state` data somewhere.** Today the
  endpoint exists and the broker promises polling — the operator
  never sees it.
- **Add forms for the medium-severity invisible settings**
  (`ORDER_HISTORY_*`, `ENABLE_TOPSTEP_ORDER_DRY_RUN`,
  `TOPSTEP_BASE_URL`, `TOPSTEP_WS_URL`, `TOPSTEP_ENV`, the realtime
  knobs, the webhook rate-limit knobs). Several of these are
  legitimate "advanced settings" that belong under a single hidden-by-
  default panel rather than scattered across pages.
- **Add a `TOPSTEP_PASSWORD` field to `/settings/broker`**, or
  explicitly document why it stays env-only (e.g. ProjectX doesn't
  use it). Right now it just looks like an oversight.
- **Resolve the Xiznit-exit-no-context bug.** A strategy that exits
  on SL with a price-level field but no `reason` field gets a
  rejection. Either teach the parser to infer `close_all=True` when
  EXIT + numeric `sl` + no qty, or document the required body shape
  in `docs/TRADINGVIEW_ALERTS.md` and add a clear error message.
- **Mode dropdown labels.** Either show `paper` everywhere or
  `Execution Test` everywhere — not both. Right now the operator
  reads `mode=paper` in logs and `Execution Test` in the UI.
- **Per-`order_id` lock dict needs eviction.** TTL-based pruning,
  ~hourly cleanup.
- **Add an "advanced settings" page** that exposes every `MANAGED_KEY`
  as a fallback so a stuck operator can edit anything without
  SQLite.

### Rough dependency order

1. (blocker) UI surfaces for the three critical live-execution
   settings. Cheap; ~half a day. Doesn't depend on anything else.
2. (blocker) README + docs/topstep.md rewrite to match reality.
3. (blocker) Backup/restore (export + import a config bundle).
4. (high) Either real `BROKER_PROVIDER` hot-reload or a forced-restart
   modal in the dashboard.
5. Test additions from Section 4 — particularly the property test
   for `MANAGED_KEYS` visibility. Land this before steps 1-4 so the
   next regression is caught for free.
6. (polish) Periodic refresh on `/`.
7. (polish) Forms for medium-severity invisible settings, ideally
   under an "Advanced" disclosure.
8. (polish) Xiznit-exit close_all-fallback fix.
9. (polish) Mode-label consistency.
10. (polish) `order_id` lock dict eviction.

## Appendix: Methodology

Files read in full (no excerpts):

- `app/config.py`
- `app/settings_store.py`
- `app/main.py` (all 3861 lines, in chunks)
- `app/dashboard.py`
- `app/webhook.py`
- `app/risk_engine.py`
- `app/webhook_parser.py`
- `app/templates/base.html`
- `app/templates/dashboard.html`
- `app/templates/settings_broker.html`
- `app/templates/settings_risk.html`
- `app/templates/system.html`
- `app/templates/tradingview.html`
- `.env.example`
- `README.md`
- `.gitignore`
- `tests/test_webhook_rejection_logging.py`

Files skimmed (line-count + relevant section pulls):

- `app/auth.py`, `app/journal.py`, `app/symbol_map.py`,
  `app/rate_limiter.py`, `app/kill_switch.py`,
  `app/execution/topstep.py` (referenced by line, not read in full
  here — prior audit doc covered it).
- `app/templates/metrics.html` (order-history JS block, lines
  280-339).
- Test directory listing only — no test bodies executed.

Commands run:

- `ls`, `wc -l`, `grep -n` on routes / fetch / setInterval.

No HTTP probes to the live server. No DB queries. No file
modifications outside this audit document.
