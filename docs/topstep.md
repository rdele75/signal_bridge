# Topstep / TopstepX integration (read-only account data)

SignalBridge currently supports the following with Topstep:

- **Authentication** against TopstepX/ProjectX (`/api/Auth/loginKey`)
  using your **TopstepX email address** as `TOPSTEP_USERNAME` and a
  ProjectX API key.
- **Account discovery** (`/api/Account/search`) — returns the
  **numeric ProjectX account id**, account name, balance, `canTrade`,
  and `isVisible` for each active account.
- **Read-only account status** surfaced on the dashboard and
  `/api/broker/status` (selected account id/name, balance, canTrade,
  isVisible, cached-token state).

Open-positions and working-orders read endpoints are **scaffolded** —
they return a structured `not_implemented` envelope until the
ProjectX response shapes are pinned down. The endpoints stay
read-only and never hit the wire while scaffolded.

**Order placement is still disabled** — the webhook handler rejects
Topstep-routed signals with a clearly labeled reason
(`broker_not_implemented: topstep_order_submission_disabled:
topstep_execution_not_enabled: …`) so no real orders leave the
building, and never silently falls back to paper.

**Do not share or commit API keys or tokens.** The `TOPSTEP_API_KEY`
and `TOPSTEP_TOKEN` are masked in the dashboard and in API responses
(last 4 characters only). `.env` is gitignored.

## How Topstep exposes its API

Topstep/TopstepX uses [**ProjectX**](https://www.topstepx.com/) for
programmatic access. ProjectX provides:

- REST and WebSocket APIs
- API-key authentication (per-user TopstepX API key)
- Market and historical data feeds
- Account access and order routing

### Get TopstepX API access

1. Sign in to your TopstepX account.
2. Make sure your account has **API Access enabled / subscribed** —
   ProjectX is a paid add-on.
3. Open **Settings → API** (or whichever tab TopstepX surfaces the API
   key generator under).
4. **Generate an API key**. The UI shows it once; copy it somewhere safe.
5. `TOPSTEP_USERNAME` is your **TopstepX login email**, not the friendly
   account label (e.g. `"PRACTICEDEC1100146-21434541"`). The label
   shows up on `/api/Account/search` as the account `name`; the auth
   call wants the user account's email.

> Never share or commit your API key. Treat it like a password.

## What this phase implements

| Capability                  | Status                                |
|-----------------------------|---------------------------------------|
| `authenticate()`            | **real** — `POST /api/Auth/loginKey` with username + apiKey |
| `get_auth_headers()`        | **real** — returns `Authorization: Bearer <token>`, auths on demand |
| `refresh_token()`           | re-auths via `loginKey` (ProjectX doesn't issue a separate refresh token) |
| `get_accounts()`            | **real** — `POST /api/Account/search` with `onlyActiveAccounts=true`; returns numeric ProjectX account ids plus `balance`, `canTrade`, `isVisible` |
| `get_selected_account()`    | **real** — matches `TOPSTEP_ACCOUNT_ID` / `SELECTED_ACCOUNT_ID` against the active list. Compares ids as **trimmed strings**, so a numeric ProjectX id (e.g. `5001`) and the stored string form (`"5001"`) match without int/string surprises |
| `get_positions()`           | **scaffolded** — returns a safe `status=not_implemented` envelope with the selected account id; never crashes, never hits the wire |
| `get_orders()`              | **scaffolded** — same shape as `get_positions()` |
| `test_connection()`         | **real** — auths, fetches accounts, reports `accounts_count`, `selected_account_id`, and the parsed `selected_account` snapshot |
| `submit_market_order()`     | refused — `status=topstep_execution_not_enabled` |
| `flatten_position()`        | refused — `status=topstep_execution_not_enabled` |
| `cancel_all_orders()`       | refused — `status=topstep_execution_not_enabled` |
| `execute()` (webhook path)  | raises `NotImplementedError` — webhook rejects with `broker_not_implemented: topstep_execution_not_enabled…` and journals the rejection |

Tokens are cached in the `settings` table (`TOPSTEP_TOKEN`,
`TOPSTEP_TOKEN_EXPIRES_AT`) with a conservative 23-hour expiry, so the
adapter doesn't burn an auth call on every request.

`EXECUTION_MODE=live` and `TOPSTEP_ENV=live` are still blocked across
SignalBridge.

### `/api/broker/status` payload

For the active Topstep adapter, the status endpoint exposes:

- `provider`, `broker_provider`, `active_broker_provider`,
  `execution_mode`
- `broker_connected`, `status` (`ok` / `missing_credentials` /
  `auth_failed` / `account_not_found` / …), `auth_status` (mirror of
  `status` for templates), `broker_message`
- `selected_account_id` (string), `selected_account_name`,
  `selected_account` (`{id, account_id, id_str, name, balance,
  can_trade, is_visible}` — `None` when no account is selected /
  found)
- `balance` / `account_balance`, `can_trade`, `is_visible` — flat
  mirrors of the selected account snapshot for compact card rendering
- `token_cached` (bool) and `token_expires_at` (ISO prefix, never the
  raw JWT)
- `positions_status` (`not_implemented` in this phase),
  `positions_count`, `positions_not_implemented`, `positions_message`
- `orders_status` (`not_implemented` in this phase), `orders_count`,
  `orders_not_implemented`, `orders_message`
- `accounts_count` (total active accounts), `restart_required`

Tokens and API keys are **never** returned in full — only the
last-four preview and the masked `token_cached` / `token_expires_at`
state appear in the payload.

## Configuration

Configure either through the `.env` file or the dashboard
(`/settings/broker`). Persisted values in SQLite override `.env` on
the next start.

| Variable                  | Default                       | Notes                                  |
|---------------------------|-------------------------------|----------------------------------------|
| `BROKER_PROVIDER`         | `paper`                       | set to `topstep` to load this adapter at startup |
| `EXECUTION_MODE`          | `paper`                       | stays `paper` or `demo`; live is blocked |
| `TOPSTEP_USERNAME`        | *(empty)*                     | your TopstepX **login email** — not the account label/name |
| `TOPSTEP_API_KEY`         | *(empty)*                     | API key from TopstepX/ProjectX. **Never displayed in full** — last 4 chars only |
| `TOPSTEP_ACCOUNT_ID`      | *(empty)*                     | numeric ProjectX account id returned by `/api/Account/search` (e.g. `5001`); stored as a string |
| `SELECTED_ACCOUNT_ID`     | *(empty)*                     | global override for the active account; the **Use this account** button writes both keys |
| `TOPSTEP_ENV`             | `demo`                        | `live` is blocked                       |
| `TOPSTEP_BASE_URL`        | `https://api.topstepx.com`    | ProjectX REST base URL                  |
| `TOPSTEP_WS_URL`          | `https://rtc.topstepx.com`    | ProjectX WebSocket URL (not used yet)   |
| `TOPSTEP_TOKEN`           | *(empty, written by adapter)* | cached JWT — never displayed in full    |
| `TOPSTEP_TOKEN_EXPIRES_AT`| *(empty, written by adapter)* | ISO-8601 token expiry                   |

The dashboard masks `TOPSTEP_USERNAME` and `TOPSTEP_API_KEY`. The API
key form input keeps the saved value when submitted blank.

## Try it locally

1. In `/settings/broker`, paste your TopstepX **username** and
   **API key**, save.
2. Click **Test Topstep auth** under the Topstep card. You should see
   ``status: authenticated``. The token is cached in SQLite for 23h.
3. Click **Fetch accounts**. SignalBridge will call
   `POST /api/Account/search` with `{"onlyActiveAccounts": true}` and
   render the returned accounts.
4. Click **Use this account** on the account row you want to trade
   from. That writes the id into both `SELECTED_ACCOUNT_ID` and
   `TOPSTEP_ACCOUNT_ID`.
5. (Optional) Flip `BROKER_PROVIDER` to `topstep` and restart. **No
   orders are placed** — the webhook keeps rejecting Topstep-routed
   signals with `broker_not_implemented: topstep_execution_not_enabled…`.

Equivalent API calls (admin auth required):

```
POST /api/topstep/authenticate
GET  /api/topstep/accounts
POST /api/topstep/select-account     # form field: account_id
GET  /api/broker/status
POST /api/broker/test-connection
GET  /api/broker/accounts
```

## Secrets / safety reminders

- Never share your TopstepX API key. Anyone holding it can issue
  authenticated ProjectX calls on your behalf.
- Never commit `.env` or any file containing credentials. `.env` is
  already in `.gitignore`.
- `TOPSTEP_API_KEY` and `TOPSTEP_TOKEN` are **never** echoed back in
  full from the dashboard or API — only the last 4 characters appear,
  and short values are reported as `configured`.

## Next phase (not in this build)

Still off-limits until explicitly green-lit:

- Wiring `get_positions()` to `POST /api/Position/searchOpen` and
  `get_orders()` to `POST /api/Order/searchOpen` once the response
  schemas are confirmed against a real ProjectX account. Today both
  return a structured `not_implemented` envelope and never hit the
  wire.
- Routing `submit_market_order` over `POST /api/Order/place` (or the
  TopstepX equivalent) — currently refuses.
- `flatten_position` / `cancel_all_orders` over REST.
- WebSocket market and user streams (`TOPSTEP_WS_URL`).
- Switching `EXECUTION_MODE=live` from a blocked value to a routed one.
