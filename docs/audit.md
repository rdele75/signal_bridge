# SignalBridge Audit & Safety Review

This document captures the safety posture of the current SignalBridge
build. Treat it as the contract for what the system promises to do and
not do — anything missing from this list is not guaranteed.

## Confirmed capabilities

- **TradingView webhook**: accepts the generic SignalBridge envelope.
  Shared-secret check uses `hmac.compare_digest`.
- **Xiznit native alerts**: parsed when the body matches the Xiznit
  shape. Secret comes from the request envelope (query string or
  `X-SignalBridge-Secret` header); a body `secret` still wins if
  present.
- **Strategy-managed risk sizing**: when enabled, sizing comes from the
  alert. When disabled, the configured `FIXED_CONTRACTS_PER_TRADE` is
  used. Both are capped by `MAX_CONTRACTS_PER_TRADE`.
- **Timeframe lock**: optional. When on, the signal must carry a
  timeframe in `ALLOWED_TIMEFRAMES`.
- **Symbol mapping**: provider-aware mapping in `config/symbols.json`.
- **Paper broker**: simulated fills against the journal; flatten/reset
  endpoints exist.
- **Topstep auth** (`/api/Auth/loginKey`) works and persists a 23h JWT.
- **Topstep account discovery** (`/api/Account/search`) works.
- **Topstep demo/sim execution** (`/api/Order/place`) works once the
  demo gates are armed.
- **Topstep live/funded execution** is supported but disabled by
  default. Requires explicit arm flow described below.
- **Admin auth**: PBKDF2-SHA256 password hash stored in SQLite.
  Plaintext env fallback only when no hash is stored. Successful logins
  migrate the plaintext default to a hash automatically.
- **Kill switch**: file-backed; checked by the risk engine; blocks all
  executions when active.
- **`pdctl`/`sbctl`**: start/stop/restart/status/logs/health/audit.

## Current safety gates

### Boot-time validation

`create_app()` refuses to construct the FastAPI app — no routes are
mounted, no subsystem is initialised — when any of the following is
true:

- `TRADINGVIEW_WEBHOOK_SECRET` is unset or empty.
- `TRADINGVIEW_WEBHOOK_SECRET` equals the public placeholder
  `change_me_to_a_long_random_secret` (also the literal value in
  `.env.example`).
- `TRADINGVIEW_WEBHOOK_SECRET` is shorter than 16 characters.

Failure raises `RuntimeError` listing every offending item and the fix
(`openssl rand -hex 32`). Escape hatch:
`SIGNALBRIDGE_ALLOW_INSECURE_BOOT=1` downgrades the refusal to a loud
`WARNING` and boots anyway. Intended for debug sessions only; never
set in production `.env`.

### Demo execution (`EXECUTION_MODE=demo`)

All of the following must be true before `/api/Order/place` is called:

- `BROKER_PROVIDER=topstep`
- `EXECUTION_MODE=demo`
- `ENABLE_TOPSTEP_ORDER_EXECUTION=true`
- `TOPSTEP_EXECUTION_CONFIRM=DEMO_ONLY`
- `ENABLE_LIVE_TRADING=false`
- A numeric Topstep account is selected and (when reported) `canTrade=true`
- Kill switch is off
- Risk engine accepts the signal (symbol allow-list, contracts cap,
  daily loss, duplicate order, max open positions, timeframe lock)

### Live execution (`EXECUTION_MODE=live`)

Live execution layers stricter gates on top. ALL must be true:

- `BROKER_PROVIDER=topstep`
- `EXECUTION_MODE=live`
- `ENABLE_TOPSTEP_ORDER_EXECUTION=true`
- `TOPSTEP_EXECUTION_CONFIRM=LIVE_CONFIRMED`
- `ENABLE_LIVE_TRADING=true`
- `LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_ORDERS`
- `LIVE_TRADING_ACCOUNT_ACK=true`
- Selected Topstep account exists and (when reported) `canTrade=true`
- Kill switch is off (when `LIVE_REQUIRE_KILL_SWITCH_OFF=true`)
- Signal symbol is in `LIVE_ALLOWED_SYMBOLS`
- Signal contracts ≤ `LIVE_MAX_CONTRACTS_PER_TRADE`
- Signal contracts ≤ `MAX_CONTRACTS_PER_TRADE`
- A valid Topstep contract mapping exists for the symbol
- Timeframe lock passes if enabled

A failing live gate returns `live_execution_locked` with the specific
gate label in the response details. The broker form cannot set
`EXECUTION_MODE=live` — only `/api/topstep/live-execution/enable` can
flip every flag together.

## Known limitations

- Tradovate adapter is scaffolded but not connected.
- Topstep order history is only available via the journal fallback —
  ProjectX search-orders is wired but not yet rendered in the metrics
  page beyond raw rows.
- Live execution skips the daily-loss limit cross-check at submission
  time — the risk engine enforces it before the broker dispatch, so the
  effective behavior matches demo, but a future hardening pass should
  re-check at the broker boundary.
- The kill switch is file-backed; it survives restarts but it is not
  replicated across hosts (single-operator app).
- No rate limiter on the webhook endpoint — the shared-secret check is
  the only inbound throttle.

## What remains risky

- Live execution is real money. The arm flow requires explicit
  acknowledgement, but the operator is still the last line of defense.
- TradingView alerts can carry stale prices; bracket stops/TPs are not
  built yet for Topstep.
- `SESSION_SECRET` must be rotated before exposing the dashboard
  publicly. The startup logger warns when the default is in use.

## How to verify DEMO mode

```bash
# Print the gate snapshot. Should show demo execution armed = no by default.
sbctl audit

# Or via curl with admin auth:
curl -s http://127.0.0.1:8000/api/status | jq

# After arming via the dashboard, sbctl audit should show:
#   execution_mode           : demo
#   ENABLE_TOPSTEP_ORDER_EXECUTION : yes
#   TOPSTEP_EXECUTION_CONFIRM      : DEMO_ONLY
#   demo execution armed           : yes
#   live execution armed           : no
```

## How to verify LIVE is locked unless armed

```bash
sbctl audit
# Expect:
#   ENABLE_LIVE_TRADING            : no
#   LIVE_TRADING_CONFIRM           : disabled
#   LIVE_TRADING_ACCOUNT_ACK       : no
#   live execution armed           : no
```

If `live execution armed : yes` appears unexpectedly, immediately:

1. POST `/api/topstep/live-execution/disable` (or click the disarm
   button on `/settings/broker`).
2. Run `sbctl audit` again and confirm `live execution armed : no`.

## Emergency stop

In order, fastest first:

1. **Disable Live Execution** button on `/settings/broker`
   (or `curl -X POST /api/topstep/live-execution/disable`).
2. **Enable the kill switch** via the dashboard.
   `LIVE_REQUIRE_KILL_SWITCH_OFF` defaults true, so an active kill
   switch alone blocks live submissions.
3. **Stop the server**: `pdctl stop` (or `sbctl stop`).
4. **Turn off the public tunnel**: `tailscale funnel off` so external
   TradingView webhooks can no longer reach the box.

Each step is independent — running them in sequence is belt-and-braces.

## Secret handling

- Webhook secret: stored in SQLite via the managed key
  `TRADINGVIEW_WEBHOOK_SECRET`. The dashboard masks it in summary
  views and only displays the full value on `/tradingview` after the
  operator authenticates. Regeneration is logged as an event without
  the new value.
- Topstep API key: stored in SQLite. Dashboard masks to `…<last-4>`.
- Topstep JWT: persisted across restarts, masked in logs to a boolean
  presence flag.
- Admin password: stored as a PBKDF2-SHA256 hash. Plaintext env value
  is only honored until a hash exists or until a successful login
  migrates it.
- Logs never contain raw secret/token/password material — verified by
  the `test_regenerate_does_not_leak_secret_to_logs` and
  `test_submit_market_order_does_not_log_or_leak_token` tests.
