# Topstep / TopstepX integration (scaffolded)

SignalBridge is in the process of growing a Topstep adapter so it can
route alerts to a TopstepX combine / funded account. As of this build
the adapter is **scaffolded only** — credentials persist, the dashboard
and `/api/broker/*` endpoints behave correctly under
`BROKER_PROVIDER=topstep`, and the webhook handler rejects live
executions with a clearly labeled reason. No real Topstep orders are
placed.

## How Topstep exposes its API

Topstep/TopstepX uses [**ProjectX**](https://www.topstepx.com/) for
programmatic access. ProjectX provides:

- REST and WebSocket APIs
- API-key authentication (per-user TopstepX API key)
- Market and historical data feeds
- Account access and order routing

To use it you must:

1. Have **API Access enabled / subscribed** on your TopstepX account.
2. Generate your **API key** from TopstepX/ProjectX settings.
3. Know your **TopstepX username** and the **account ID** you want to
   trade.

The dashboard prepares for that day — none of those values are required
to run SignalBridge with the paper broker.

## Current adapter status

| Capability                | Status                                |
|---------------------------|---------------------------------------|
| `test_connection()`       | scaffolded — reports `missing_credentials` or `scaffolded_not_connected` |
| `authenticate()`          | not implemented (safe envelope)        |
| `refresh_token()`         | not implemented (safe envelope)        |
| `get_accounts()`          | not implemented (safe envelope, empty list) |
| `get_selected_account()`  | not implemented (safe envelope)        |
| `get_positions()`         | not implemented (safe envelope, empty list) |
| `get_orders()`            | not implemented (safe envelope, empty list) |
| `submit_market_order()`   | refused — `Topstep order submission not implemented yet` |
| `flatten_position()`      | not implemented (safe envelope)        |
| `cancel_all_orders()`     | not implemented (safe envelope)        |
| `execute()` (webhook)     | raises `NotImplementedError` — webhook rejects with `broker_not_implemented: topstep_execution_not_implemented…` and journals the rejection |

Live execution mode is intentionally disabled across SignalBridge. The
Topstep adapter only accepts `TOPSTEP_ENV=demo`.

## Configuration

Configure either through the `.env` file or the dashboard
(`/settings/broker`). Persisted values in SQLite override the `.env`
defaults on next start.

| Variable                  | Default                       | Notes                                  |
|---------------------------|-------------------------------|----------------------------------------|
| `BROKER_PROVIDER`         | `paper`                       | set to `topstep` to load this adapter  |
| `TOPSTEP_USERNAME`        | *(empty)*                     | your TopstepX username                 |
| `TOPSTEP_API_KEY`         | *(empty)*                     | API key from TopstepX/ProjectX. **Never displayed in full** in the dashboard. |
| `TOPSTEP_ACCOUNT_ID`      | *(empty)*                     | account id you want to trade           |
| `TOPSTEP_ENV`             | `demo`                        | `live` is blocked in this build        |
| `TOPSTEP_BASE_URL`        | `https://api.topstepx.com`    | ProjectX REST base URL                 |
| `TOPSTEP_WS_URL`          | `https://rtc.topstepx.com`    | ProjectX WebSocket URL                 |
| `TOPSTEP_TOKEN`           | *(empty)*                     | cached auth token (written by the adapter once auth lands; empty for now) |
| `TOPSTEP_TOKEN_EXPIRES_AT`| *(empty)*                     | cached token expiry (ISO-8601)         |

The dashboard masks `TOPSTEP_USERNAME` and `TOPSTEP_API_KEY` — the form
field for the API key shows only the last four characters of the saved
value (or `configured`) and an empty submit preserves the saved key.

## Try it locally

1. Edit `.env`:
   ```
   BROKER_PROVIDER=topstep
   EXECUTION_MODE=demo
   TOPSTEP_USERNAME=<your TopstepX username>
   TOPSTEP_API_KEY=<your API key>
   TOPSTEP_ACCOUNT_ID=<your account id>
   ```
2. Restart the server (`./run.sh` or `python -m uvicorn app.main:app`).
3. Visit `/settings/broker` — the Topstep section should report
   `scaffolded` with masked credentials.
4. Hit **Test connection** — you should see status
   `scaffolded_not_connected` and a clear "not implemented" message.
5. Optionally fire a TradingView alert. The webhook journals it as
   rejected with reason
   `broker_not_implemented: topstep_execution_not_implemented…`.
   No order leaves the building.

## Next phase (not in this build)

When you give the green light, the follow-up work is:

- Wire `authenticate()` / `refresh_token()` to ProjectX.
- Cache the token + expiry in SQLite via the existing settings keys.
- Implement `get_accounts`, `get_positions`, `get_orders` over REST.
- Implement `submit_market_order` and `flatten_position` over REST.
- Add WebSocket market/user streams.
- Switch `EXECUTION_MODE=demo` from a placeholder to actually routing.

`live` mode stays off until those steps are independently verified.
