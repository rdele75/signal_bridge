# Topstep / TopstepX integration

SignalBridge talks to Topstep through the
[**ProjectX**](https://www.topstepx.com/) REST API. Four layers are
wired up — only the first two are on by default:

| Layer                                  | Default | Notes |
|----------------------------------------|---------|-------|
| Read-only account / position / order data | **on** | `Auth/loginKey`, `Account/search`, `Position/searchOpen`, `Order/searchOpen`, `Order/search` |
| Dry-run market-order *previews* (no submission) | **on** | builds `/api/Order/place` payload, journals it, never POSTs |
| Demo/sim market-order *execution* (real POST `/api/Order/place`) | **off** | gated by four safety switches |
| Live/funded market-order *execution* (real POST `/api/Order/place`) | **off** | gated by every demo switch + four additional live-only gates |

Flatten and cancel are wired through REST too: the Dashboard's
*Flatten All Positions* button calls `flatten_position()` which posts
to `/api/Position/closeContract` per leg, and the order-cancel helper
posts to `/api/Order/cancel`. Bracket orders (OCO / bracketed
stop+target) and the SignalR user hub remain on the TODO list — see
the **SignalR user hub (TODO)** section below.

## Credentials

- `TOPSTEP_USERNAME` is your **TopstepX login email address** — not the
  friendly account label (e.g. `PRACTICEDEC1100146-21434541`). The
  label is the account `name` returned by `/api/Account/search`; the
  auth call wants the email tied to the user.
- `TOPSTEP_API_KEY` comes from **TopstepX → API tab** (the API generator
  page; ProjectX is a paid add-on, make sure your account has it
  enabled). The UI shows the key once — copy it somewhere safe.
- **Do not share or commit API keys or tokens.** `.env` is gitignored.
  `TOPSTEP_API_KEY` and `TOPSTEP_TOKEN` are masked everywhere they
  appear in the dashboard / API (last 4 characters only). Short values
  show as `configured`.

## Read-only data (Phase 1)

The adapter calls these endpoints. Each one returns a structured
envelope (`ok`, `status`, `provider`, the response payload, masked
credential summary) and never crashes.

| Method                                 | Endpoint                      | Body                              |
|----------------------------------------|-------------------------------|-----------------------------------|
| `authenticate()`                       | `POST /api/Auth/loginKey`     | `{userName, apiKey}`              |
| `get_accounts()`                       | `POST /api/Account/search`    | `{onlyActiveAccounts: true}`      |
| `get_selected_account()`               | (in-memory match on accounts) | compares ids as **trimmed strings** so numeric ProjectX ids and the user-saved string form match |
| `get_positions()`                      | `POST /api/Position/searchOpen` | `{accountId: <int>}`            |
| `get_orders()`                         | `POST /api/Order/searchOpen`  | `{accountId: <int>}`              |
| `search_orders(start, end)`            | `POST /api/Order/search`      | `{accountId, startTimestamp?, endTimestamp?}` |
| `test_connection()`                    | auth + accounts               | reports `selected_account`, `accounts_count`, masked token cache |

`TOPSTEP_ACCOUNT_ID` must be the **numeric ProjectX account id** (the
`id` field returned by `/api/Account/search` — e.g. `5001`). The value
is stored as a string but compared as a trimmed string against the
numeric returned by ProjectX, so both shapes match cleanly.

If `TOPSTEP_ACCOUNT_ID` is set to a non-numeric value, the read-only
endpoints return a `non_numeric_account_id` envelope and **never hit
the wire**.

## Dry-run order builder (Phase 2)

The dry-run path is the default for any TradingView webhook routed
through Topstep. It runs the normal risk checks, builds a
`/api/Order/place` payload via
`app/execution/topstep_order_builder.py`, journals it, and **does not
submit**.

ProjectX market-order payload shape:

```
POST /api/Order/place
{
  "accountId":  <int>,                # numeric ProjectX account id
  "contractId": "<str>",              # ProjectX contract id (NOT a TV ticker)
  "type":       2,                    # 1 limit · 2 market · 4 stop · 5 trail · 6 join-bid · 7 join-ask
  "side":       0 | 1,                # 0 bid/buy · 1 ask/sell
  "size":       <int>,                # contracts
  "limitPrice": null,
  "stopPrice":  null,
  "trailPrice": null,
  "customTag":  "<str|null>"          # SignalBridge order_id / comment, truncated to 64 chars
}
```

### Action → side mapping

| Internal action | ProjectX side | Notes |
|-----------------|---------------|-------|
| `BUY`           | `0` (bid)     | open or add long |
| `COVER`         | `0` (bid)     | close short |
| `SELL`          | `1` (ask)     | close long or open short |
| `SHORT`         | `1` (ask)     | open short |
| `EXIT`          | refused       | the builder doesn't know which side closes — returns `unsupported_exit_without_position` |

### Symbol mapping (required)

SignalBridge maps TradingView tickers to broker-specific symbols.
ProjectX expects real contract ids (e.g. `CON.F.US.MES.M26`), not
TradingView tickers. The builder refuses to guess: if the Topstep
contract id for a ticker is missing **or blank**, it returns
`symbol_mapping_missing` with the message
`Topstep contract id missing for <ticker>. Add it in Configuration > Symbols.`,
and the dry-run is journaled as a build failure.

**Default mappings** shipped in `config/symbols.example.json`:

| TradingView ticker | Topstep contract id         |
|--------------------|-----------------------------|
| `MNQ1!`            | `CON.F.US.MNQ.M26`          |
| `MES1!`            | `CON.F.US.MES.M26`          |
| `NQ1!`             | *(blank — fill via search)* |
| `ES1!`             | *(blank — fill via search)* |

`NQ1!` / `ES1!` ship blank because the full-size E-mini contract ids
must be picked from the live ProjectX catalog (and they change every
quarter).

**Edit mappings** at **Configuration → Symbols** (`/settings/symbols`).
The page edits `config/symbols.json` directly. You can add, edit, and
remove rows, then **Save mappings**. The same page hosts a **Topstep
contract search** tool: type a search term (e.g. `NQ` or `ES`), click
**Search Topstep Contracts**, and use the **Copy** button to paste the
active contract id into the Topstep column above.

The symbol allowlist (`ALLOWED_SYMBOLS`) is **separate** from the
mapping table — symbols may be allowlisted without a Topstep mapping
(they will simply be rejected at the order builder if Topstep is the
active provider).

**Contract ids roll with futures expiration**, so review and update
them each contract quarter.

### `/api/topstep/contracts/search`

Proxy to ProjectX `POST /api/Contract/search` for the Symbols page.
Requires admin auth and Topstep credentials.

```
POST /api/topstep/contracts/search
Content-Type: application/json

{ "searchText": "NQ", "live": false }
```

Response (envelope):

```
{
  "ok": true,
  "status": "ok",
  "searchText": "NQ",
  "live": false,
  "contracts": [
    {
      "id": "CON.F.US.ENQ.M26",
      "name": "ENQM26",
      "description": "E-mini Nasdaq-100",
      "tickSize": 0.25,
      "tickValue": 5,
      "activeContract": true,
      "symbolId": "F.US.ENQ"
    }
  ]
}
```

Missing credentials, auth failure, or a ProjectX rejection all surface
as an `ok: false` envelope rather than a 5xx — the UI renders the
`message` field directly.

### `/api/topstep/build-order-preview`

```
POST /api/topstep/build-order-preview
Content-Type: application/json

# Optional body: a TradingViewAlert. If omitted (or empty), the most
# recent journaled signal is reused.
{ "secret": "...", "symbol": "MES1!", "action": "buy", "contracts": 1 }
```

Response includes the normalized signal, account id, contract id,
side, size, full order payload, the safety state, and **always**
`would_submit: false`.

## Order history & open data

Three ProjectX endpoints back the dashboard's account visibility:

| Purpose            | Endpoint                        | Body                                      |
|--------------------|---------------------------------|-------------------------------------------|
| Open orders        | `POST /api/Order/searchOpen`    | `{accountId: <int>}`                      |
| Order history      | `POST /api/Order/search`        | `{accountId: <int>, startTimestamp?, endTimestamp?}` |
| Open positions     | `POST /api/Position/searchOpen` | `{accountId: <int>}`                      |

Order history is exposed at:

```
GET /api/broker/order-history?lookback_days=<int>&limit=<int>
```

Defaults come from `ORDER_HISTORY_LOOKBACK_DAYS` (7) and
`ORDER_HISTORY_LIMIT` (100). The response shape:

```
{
  "ok": true,
  "provider": "topstep",
  "status": "ok",
  "lookback_days": 7,
  "limit": 100,
  "start_timestamp": "...",
  "end_timestamp": "...",
  "count": 12,
  "orders": [
    {
      "orderId": "999111",
      "accountId": 5001,
      "contractId": "CON.F.US.MES.M26",
      "creationTimestamp": "...",
      "updateTimestamp": "...",
      "status": "Filled",
      "type": 2,
      "side": 0,
      "side_label": "BUY",
      "size": 1,
      "limitPrice": null,
      "stopPrice": null,
      "filledPrice": 5000.25,
      "customTag": "..."
    }
  ]
}
```

The endpoint never crashes if ProjectX returns an unexpected shape — it
falls back to an empty `orders` list with the failure surfaced via
`ok=false` + `message`. No tokens or API keys ever appear in the JSON.

### Metrics → Past Orders UI

`/metrics` shows a **Past Orders** card. For Topstep it pulls the live
order history via `/api/broker/order-history` and adds:

- a **Refresh** button
- a **Lookback** dropdown (1 day / 7 days / 30 days)
- a clean empty state: *No Topstep orders found for this lookback window.*
- a clean error state when the endpoint returns `ok: false`

When the broker is paper or the journal, the card keeps the original
server-rendered table.

Columns (Topstep mode): Time · Symbol/Contract · Side · Size · Type ·
Status · Limit · Stop · Filled · Order ID · Tag.

## Realtime account/order/position data

Two modes are envisioned. Polling is the default; SignalR is documented
as a future TODO.

| Setting                          | Default     | Notes |
|----------------------------------|-------------|-------|
| `ENABLE_TOPSTEP_REALTIME`        | `false`     | Master switch for the auto-refresh polling loop in the dashboard. |
| `TOPSTEP_REALTIME_MODE`          | `polling`   | `polling` is implemented; `signalr` is reserved for the SignalR client. |
| `TOPSTEP_REALTIME_POLL_SECONDS`  | `5`         | Interval used by the dashboard auto-refresh. |

### Polling (implemented)

`/api/realtime/state` returns positions + open orders + a refreshed-at
timestamp in one call. The broker page's Realtime card calls it
manually (Refresh button) and, when `ENABLE_TOPSTEP_REALTIME=true`,
auto-refreshes every `TOPSTEP_REALTIME_POLL_SECONDS` seconds via JS.

Polling never places orders. The only ProjectX paths it is allowed to
hit are `/api/Position/searchOpen`, `/api/Order/searchOpen`, and
`/api/Order/search`. The `app.execution.topstep_realtime.RealtimePoller`
helper wraps `broker.get_positions()` + `broker.get_orders()` so future
server-side polling jobs share the same surface.

### SignalR user hub (TODO)

ProjectX exposes a SignalR user hub at `TOPSTEP_WS_URL` for push
updates (accounts/orders/positions/balances). Wiring it requires the
`signalrcore` Python package; once that lands the
`SignalRClientPlaceholder` in `app/execution/topstep_realtime.py`
becomes a real client. The dashboard UI already shows the disclaimer:

> **Realtime mode:** Polling every Ns ·
> **WebSocket SignalR:** not enabled yet

Until SignalR is implemented, switching `TOPSTEP_REALTIME_MODE` to
`signalr` falls back to the polling implementation.

## Demo/sim execution (Phase 3)

Disabled by default. **All five** of the following must be true to
allow a demo POST to `/api/Order/place`:

| Setting                            | Required value | Default |
|------------------------------------|---------------:|---------|
| `BROKER_PROVIDER`                  | `topstep`      | `paper` |
| `EXECUTION_MODE`                   | `demo`         | `paper` |
| `ENABLE_TOPSTEP_ORDER_EXECUTION`   | `true`         | `false` |
| `TOPSTEP_EXECUTION_CONFIRM`        | `DEMO_ONLY`    | `disabled` |
| `ENABLE_LIVE_TRADING`              | `false`        | `false` (locked) |

`EXECUTION_MODE=live` is accepted by the settings layer but only the
`/api/topstep/live-execution/enable` flow can flip every live gate
together (see the **Live execution** section below). The broker form
on `/settings/broker` rejects `EXECUTION_MODE=live` so accidental
re-selection through the dropdown can never arm live.

Behavior under each combination:

| Provider | Mode | Exec on? | Confirm | Outcome |
|----------|------|----------|---------|---------|
| paper    | any  | n/a      | n/a     | paper fills as before |
| topstep  | any  | false    | any     | **dry-run preview**, journaled, no POST |
| topstep  | demo | true     | `DEMO_ONLY` | **POST `/api/Order/place`** with the demo account |
| topstep  | demo | true     | `disabled`  | refused (`topstep_execution_confirm_missing`) |
| topstep  | paper | true    | any     | refused (`execution_mode_not_demo`) |
| topstep  | live | any      | any     | refused (`live_execution_locked`) |
| any      | any  | any      | any     | `ENABLE_LIVE_TRADING=true` → refused (`live_execution_locked`) |

### Arming demo execution from the dashboard

The **Dashboard Execution card** (`/`) owns mode selection and the
arming flow. The card surfaces every safety switch
(`BROKER_PROVIDER`, `EXECUTION_MODE`, `ENABLE_TOPSTEP_ORDER_EXECUTION`,
`TOPSTEP_EXECUTION_CONFIRM`, `ENABLE_LIVE_TRADING`, selected account
id, account name, `canTrade`) and labels the state as one of:

- **Dry Run Active** / **Execution Test** — default; webhooks build
  previews and never POST.
- **Demo Execution Armed** — all preconditions met; demo signals POST
  to `/api/Order/place`.
- **Live Armed** — full live gate stack satisfied; the Execution card
  shows a warning border.
- **Live Locked** — `EXECUTION_MODE=live` is set but at least one
  live-only gate is failing; execution is blocked.

Apply the **demo** mode via the dropdown + Apply button on the
Dashboard. The handler at `/api/execution/apply-mode` flips three
settings together:

```
ENABLE_TOPSTEP_ORDER_EXECUTION = true
TOPSTEP_EXECUTION_CONFIRM      = DEMO_ONLY
EXECUTION_MODE                 = demo
```

It **never** sets `ENABLE_LIVE_TRADING` or `EXECUTION_MODE=live`.

The **Disengage** button returns to dry-run by setting
`ENABLE_TOPSTEP_ORDER_EXECUTION=false` and
`TOPSTEP_EXECUTION_CONFIRM=disabled`. Provider and selected account
stay where they are.

### `/api/topstep/demo-execution/enable`

Admin-only JSON endpoint backing the Enable button. Body:

```
{ "confirm": "DEMO_ONLY" }
```

Rejected (HTTP 400, `ok: false`) when any of these is true:

- `confirm` is not exactly `DEMO_ONLY` (`invalid_confirmation`)
- `BROKER_PROVIDER` is not `topstep` (`broker_provider_not_topstep`)
- no Topstep account is selected (`no_selected_account`)
- `EXECUTION_MODE` is already `live` (`execution_mode_live_blocked`)
- `ENABLE_LIVE_TRADING` is true (`live_trading_locked`)
- kill switch is active (`kill_switch_active`)

On success the response is `status: demo_execution_armed` and the
post-flip values of every safety switch.

### `/api/topstep/demo-execution/disable`

Admin-only JSON endpoint backing the Disable button. No body. Sets
`ENABLE_TOPSTEP_ORDER_EXECUTION=false` and
`TOPSTEP_EXECUTION_CONFIRM=disabled`. Response:
`status: demo_execution_disabled`.

### `/api/topstep/submit-test-order`

A manual demo-order helper. Requires admin auth and obeys every
safety switch above. Body is optional:

```
POST /api/topstep/submit-test-order
Content-Type: application/json

{ "symbol": "MES1!", "action": "BUY", "contracts": 1 }
```

Additional input rules (HTTP 400 with stable status labels):

- `action` must be `BUY` or `SELL` (`unsupported_action`).
- `contracts` must be a positive integer ≤ `MAX_CONTRACTS_PER_TRADE`
  (`invalid_contracts` / `contracts_above_max`).
- `symbol` must have a Topstep contract id configured in the Symbols
  map (`symbol_mapping_missing`).
- `EXECUTION_MODE=live` or `ENABLE_LIVE_TRADING=true` short-circuits
  with `live_execution_locked` — the helper never works in live mode.

If any other safety gate is open the response is an `ok: false`
envelope labeled with the failing gate (e.g.
`topstep_execution_disabled`, `execution_mode_not_demo`,
`live_execution_locked`).

**Recommended first demo test:** 1 contract of MES (`MES1!`).

## Live/funded execution (Phase 4)

**Live execution is real money.** It is disabled by default and locked
behind a stricter gate set than demo. Every one of these must be true
before `/api/Order/place` is called against the funded account:

| Setting                            | Required value               | Default |
|------------------------------------|------------------------------|---------|
| `BROKER_PROVIDER`                  | `topstep`                    | `paper` |
| `EXECUTION_MODE`                   | `live`                       | `paper` |
| `ENABLE_TOPSTEP_ORDER_EXECUTION`   | `true`                       | `false` |
| `TOPSTEP_EXECUTION_CONFIRM`        | `LIVE_CONFIRMED`             | `disabled` |
| `ENABLE_LIVE_TRADING`              | `true`                       | `false` |
| `LIVE_TRADING_CONFIRM`             | `I_UNDERSTAND_LIVE_ORDERS`   | `disabled` |
| `LIVE_TRADING_ACCOUNT_ACK`         | `true`                       | `false` |
| Selected account `canTrade`        | `true` (when reported)       | n/a     |
| `LIVE_REQUIRE_KILL_SWITCH_OFF`     | kill switch must be off      | `true`  |
| Signal symbol                      | in `LIVE_ALLOWED_SYMBOLS`    | `MES1!,MNQ1!` |
| Signal contracts                   | ≤ `LIVE_MAX_CONTRACTS_PER_TRADE` AND ≤ `MAX_CONTRACTS_PER_TRADE` | both `1` |

A failing live gate returns `live_execution_locked` with a `gate`
label identifying the specific failure.

### Arming live execution from the dashboard

The **Dashboard Execution card** (`/`) owns live arming through the
live-engagement modal. Selecting `live` in the mode dropdown and
clicking Apply opens the modal. To arm:

1. Confirm the selected Topstep account is the funded account
   (the modal shows the masked account id and name).
2. Type `engage` into the confirmation field (case-insensitive on the
   UI side; the server still stores the long
   `I_UNDERSTAND_LIVE_ORDERS` token).
3. Tick the account-acknowledgement checkbox.
4. Click **Engage Live Execution**.

The Execution card then runs an engagement animation while
`/api/topstep/live-execution/verify` + `/api/topstep/live-execution/enable`
settle. On success the card flips to **Live Armed** (warning border,
red glow). The arm action flips:

```
EXECUTION_MODE                 = live
ENABLE_TOPSTEP_ORDER_EXECUTION = true
TOPSTEP_EXECUTION_CONFIRM      = LIVE_CONFIRMED
ENABLE_LIVE_TRADING            = true
LIVE_TRADING_CONFIRM           = I_UNDERSTAND_LIVE_ORDERS
LIVE_TRADING_ACCOUNT_ACK       = true
```

Disable returns every flag to the safe default. Both events are
journaled and logged at `WARNING` level (no secrets).

### `/api/topstep/live-execution/enable`

Admin-only JSON endpoint. Body:

```
{ "confirm": "I_UNDERSTAND_LIVE_ORDERS", "account_ack": true }
```

Rejected (HTTP 400, `ok: false`) when any of these is true:

- `confirm` is not exactly `I_UNDERSTAND_LIVE_ORDERS`
  (`invalid_confirmation`)
- `account_ack` is not truthy (`account_ack_missing`)
- `BROKER_PROVIDER` is not `topstep` (`broker_provider_not_topstep`)
- no Topstep account is selected (`no_selected_account`)
- `LIVE_REQUIRE_KILL_SWITCH_OFF=true` and kill switch is on
  (`kill_switch_active`)

On success: `status: live_execution_armed` with the full set of
post-flip flags.

### `/api/topstep/live-execution/disable`

Admin-only JSON endpoint. No body. Resets every live-relevant flag:

```
ENABLE_LIVE_TRADING            = false
LIVE_TRADING_CONFIRM           = disabled
LIVE_TRADING_ACCOUNT_ACK       = false
TOPSTEP_EXECUTION_CONFIRM      = disabled
ENABLE_TOPSTEP_ORDER_EXECUTION = false
```

Response: `status: live_execution_disabled`.

### `/api/topstep/submit-live-test-order`

Manual live-order helper. Admin auth + every live gate enforced.
Submits 1 contract by default. Body:

```
{ "symbol": "MES1!", "action": "buy", "contracts": 1 }
```

Rejections use stable status labels:

- `unsupported_action` (must be `buy` / `sell`)
- `invalid_contracts` (must be ≥ 1)
- `live_contracts_above_max` (> `LIVE_MAX_CONTRACTS_PER_TRADE`)
- `contracts_above_max` (> `MAX_CONTRACTS_PER_TRADE`)
- `live_symbol_not_allowed` (not in `LIVE_ALLOWED_SYMBOLS`)
- `symbol_mapping_missing` (no Topstep contract id mapped)
- `live_execution_locked` with a `gate` label for any other failed gate

This endpoint is effectively unavailable unless live is fully armed —
all gates must already be satisfied.

### Emergency stop

In order, fastest first:

```
1. POST /api/topstep/live-execution/disable    # or the dashboard button
2. Activate the kill switch (LIVE_REQUIRE_KILL_SWITCH_OFF=true blocks live)
3. pdctl stop                                  # or sbctl stop
4. tailscale funnel off                        # cut public webhook ingress
```

## Webhook behavior summary

| `BROKER_PROVIDER` | `ENABLE_TOPSTEP_ORDER_EXECUTION` | `EXECUTION_MODE` | Webhook outcome |
|-------------------|----------------------------------|------------------|----------------|
| topstep           | false                            | any non-`live`   | risk checks → dry-run preview → journal `topstep_dry_run_order_built` |
| topstep           | true                             | `demo`           | risk checks → builder → POST `/api/Order/place` |
| topstep           | true                             | `live`           | live gate check → if all pass, POST `/api/Order/place`; else `live_execution_locked` with `gate` label |
| paper             | n/a                              | any              | paper fills (unchanged) |

In all Topstep paths the journal records:

- raw payload
- normalized signal
- risk decision
- dry-run payload **or** broker response (`success`, `orderId`,
  `errorCode`, `errorMessage`)
- `broker_order_id` (the ProjectX `orderId`) when the order was placed
- `execution_mode`
- `broker_provider=topstep`
- **never** API keys, JWTs, or any other secret material

## `/api/broker/status` payload (Topstep adapter)

For the active Topstep adapter, `/api/broker/status` exposes:

- `provider`, `broker_provider`, `active_broker_provider`,
  `execution_mode`
- `broker_connected`, `status` (`ok` / `missing_credentials` /
  `auth_failed` / `account_not_found` / `non_numeric_account_id` /
  …), `auth_status`, `broker_message`
- `selected_account_id` (string), `selected_account_name`,
  `selected_account` (`{id, account_id, id_str, name, balance,
  can_trade, is_visible}` — `None` when no account is selected /
  found)
- `balance` / `account_balance`, `can_trade`, `is_visible` — flat
  mirrors of the selected account snapshot
- `token_cached` (bool) and `token_expires_at` (ISO prefix, never the
  raw JWT)
- `positions_status`, `positions_count`, `positions_message`
- `orders_status`, `orders_count`, `open_orders_count` (alias),
  `orders_message`
- `accounts_count`, `restart_required`
- `enable_topstep_order_dry_run`, `enable_topstep_order_execution`,
  `topstep_execution_confirm`, `enable_live_trading` — safety state

Secrets are never returned in full.

## Configuration reference

| Variable                          | Default                       | Notes |
|-----------------------------------|-------------------------------|-------|
| `BROKER_PROVIDER`                 | `paper`                       | set to `topstep` to load this adapter |
| `EXECUTION_MODE`                  | `paper`                       | `live` is blocked |
| `TOPSTEP_USERNAME`                | *(empty)*                     | **TopstepX login email** |
| `TOPSTEP_API_KEY`                 | *(empty)*                     | from TopstepX/ProjectX API tab |
| `TOPSTEP_ACCOUNT_ID`              | *(empty)*                     | numeric ProjectX account id (e.g. `5001`); stored as a string |
| `SELECTED_ACCOUNT_ID`             | *(empty)*                     | global override for the active account |
| `TOPSTEP_ENV`                     | `demo`                        | `live` is blocked |
| `TOPSTEP_BASE_URL`                | `https://api.topstepx.com`    | REST base URL |
| `TOPSTEP_WS_URL`                  | `https://rtc.topstepx.com`    | SignalR user hub URL; client not wired yet (see SignalR TODO) |
| `TOPSTEP_TOKEN`                   | *(empty, written by adapter)* | cached JWT; masked everywhere |
| `TOPSTEP_TOKEN_EXPIRES_AT`        | *(empty, written by adapter)* | ISO-8601 expiry |
| `ENABLE_TOPSTEP_ORDER_DRY_RUN`    | `true`                        | builds previews, never submits |
| `ENABLE_TOPSTEP_ORDER_EXECUTION`  | `false`                       | required for demo or live submission |
| `TOPSTEP_EXECUTION_CONFIRM`       | `disabled`                    | `DEMO_ONLY` for demo, `LIVE_CONFIRMED` for live — flipped by the arming endpoints |
| `ENABLE_LIVE_TRADING`             | `false`                       | live master switch; flipped to `true` only by `POST /api/topstep/live-execution/enable` |
| `LIVE_TRADING_CONFIRM`            | `disabled`                    | live arming token (`I_UNDERSTAND_LIVE_ORDERS`); flipped by the live-arming endpoint |
| `LIVE_TRADING_ACCOUNT_ACK`        | `false`                       | operator-acknowledged ownership of the funded account |
| `LIVE_MAX_CONTRACTS_PER_TRADE`    | `1`                           | per-live-trade cap; **no UI surface today** — verify the SQLite value before arming live |
| `LIVE_ALLOWED_SYMBOLS`            | `MES1!,MNQ1!`                 | symbols accepted for live submissions; **no UI surface today** |
| `LIVE_REQUIRE_KILL_SWITCH_OFF`    | `true`                        | when true, kill switch must be off before live submits; **no UI surface today** |

## Try it locally

1. Set `TOPSTEP_USERNAME` (email) + `TOPSTEP_API_KEY` in `/settings/broker`.
2. Click **Test Topstep auth** → expect `status: authenticated`.
3. Click **Fetch accounts** → pick **Use this account** on the row you
   trade from. That writes both `SELECTED_ACCOUNT_ID` and
   `TOPSTEP_ACCOUNT_ID` to the numeric ProjectX id.
4. Flip `BROKER_PROVIDER=topstep` and restart. Webhooks now dry-run.
   The dashboard shows the built payload but nothing leaves the
   building.
5. To arm demo/sim execution later, open the Dashboard, change the
   execution mode dropdown to `demo`, and click **Apply**. That flips:
   - `EXECUTION_MODE=demo`
   - `ENABLE_TOPSTEP_ORDER_EXECUTION=true`
   - `TOPSTEP_EXECUTION_CONFIRM=DEMO_ONLY`

   `ENABLE_LIVE_TRADING` stays false (locked). Click **Disengage** to
   return to dry-run.
6. Recommended first demo test: 1 contract of MES (`MES1!`).
7. To arm live/funded execution, select `live` in the Execution card
   mode dropdown and complete the live-engagement modal — type
   `engage`, tick the account acknowledgement, click **Engage Live
   Execution**. The card flips to **Live Armed** and webhooks now
   route to the funded account.

## Secrets / safety reminders

- **Never share or commit API keys or tokens.** `.env` is gitignored.
  `TOPSTEP_API_KEY` and `TOPSTEP_TOKEN` are masked in the dashboard
  and in API responses.
- **Dry-run is the default** Topstep behavior. Demo and live execution
  both require explicit arming through the Dashboard.
- **Live/funded execution is implemented and is real.** Once the
  live-engagement flow succeeds, webhooks route to your real Topstep
  funded account through `/api/Order/place`. Verify
  `LIVE_MAX_CONTRACTS_PER_TRADE`, `LIVE_ALLOWED_SYMBOLS`, and
  `LIVE_REQUIRE_KILL_SWITCH_OFF` directly in SQLite before arming —
  none has a UI edit surface today (see
  [`docs/operational_audit_2026-05-21.md`](operational_audit_2026-05-21.md)
  Section 1 critical findings 1–3).
- The copier, MCP server, bracket orders, and the SignalR user hub
  are explicitly out of scope here.
