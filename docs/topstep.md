# Topstep / TopstepX integration

SignalBridge talks to Topstep through the
[**ProjectX**](https://www.topstepx.com/) REST API. Three layers are
wired up — only the first is on by default:

| Layer                                  | Default | Notes |
|----------------------------------------|---------|-------|
| Read-only account / position / order data | **on** | `Auth/loginKey`, `Account/search`, `Position/searchOpen`, `Order/searchOpen`, `Order/search` |
| Dry-run market-order *previews* (no submission) | **on** | builds `/api/Order/place` payload, journals it, never POSTs |
| Demo/sim market-order *execution* (real POST `/api/Order/place`) | **off** | gated by four safety switches; live/funded execution stays locked |

Bracket orders, flatten/cancel via REST, WebSocket streams, and
live/funded execution are **not implemented** and not planned for this
build.

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

| TradingView ticker | Topstep contract id        | Tradovate symbol |
|--------------------|----------------------------|------------------|
| `MNQ1!`            | `CON.F.US.MNQ.M26`         | `MNQ`            |
| `MES1!`            | `CON.F.US.MES.M26`         | `MES`            |
| `NQ1!`             | *(blank — fill via search)* | `NQ`             |
| `ES1!`             | *(blank — fill via search)* | `ES`             |

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

`EXECUTION_MODE=live` is blocked by the settings layer and by the
adapter itself. `ENABLE_LIVE_TRADING=true` is rejected by the
settings layer — the live/funded path stays locked in this build.

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

### `/api/topstep/submit-test-order`

A manual demo-order helper. Requires admin auth and obeys every
safety switch above. Body is optional:

```
POST /api/topstep/submit-test-order
Content-Type: application/json

{ "symbol": "MES1!", "action": "BUY", "contracts": 1 }
```

If any safety gate is open the response is an `ok: false` envelope
labeled with the failing gate (e.g. `topstep_execution_disabled`,
`execution_mode_not_demo`, `live_execution_locked`).

## Webhook behavior summary

| `BROKER_PROVIDER` | `ENABLE_TOPSTEP_ORDER_EXECUTION` | `EXECUTION_MODE` | Webhook outcome |
|-------------------|----------------------------------|------------------|----------------|
| topstep           | false                            | any non-`live`   | risk checks → dry-run preview → journal `topstep_dry_run_order_built` |
| topstep           | true                             | `demo`           | risk checks → builder → POST `/api/Order/place` |
| topstep           | true                             | `live`           | refused (`live_execution_locked`) |
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
| `TOPSTEP_WS_URL`                  | `https://rtc.topstepx.com`    | reserved for a future phase |
| `TOPSTEP_TOKEN`                   | *(empty, written by adapter)* | cached JWT; masked everywhere |
| `TOPSTEP_TOKEN_EXPIRES_AT`        | *(empty, written by adapter)* | ISO-8601 expiry |
| `ENABLE_TOPSTEP_ORDER_DRY_RUN`    | `true`                        | builds previews, never submits |
| `ENABLE_TOPSTEP_ORDER_EXECUTION`  | `false`                       | required for demo submission |
| `TOPSTEP_EXECUTION_CONFIRM`       | `disabled`                    | must be `DEMO_ONLY` to submit |
| `ENABLE_LIVE_TRADING`             | `false`                       | locked; setting `true` is rejected |

## Try it locally

1. Set `TOPSTEP_USERNAME` (email) + `TOPSTEP_API_KEY` in `/settings/broker`.
2. Click **Test Topstep auth** → expect `status: authenticated`.
3. Click **Fetch accounts** → pick **Use this account** on the row you
   trade from. That writes both `SELECTED_ACCOUNT_ID` and
   `TOPSTEP_ACCOUNT_ID` to the numeric ProjectX id.
4. Flip `BROKER_PROVIDER=topstep` and restart. Webhooks now dry-run.
   The dashboard shows the built payload but nothing leaves the
   building.
5. To enable demo/sim execution later: flip `EXECUTION_MODE=demo`,
   `ENABLE_TOPSTEP_ORDER_EXECUTION=true`,
   `TOPSTEP_EXECUTION_CONFIRM=DEMO_ONLY`, and confirm
   `ENABLE_LIVE_TRADING=false`.

## Secrets / safety reminders

- **Never share or commit API keys or tokens.** `.env` is gitignored.
  `TOPSTEP_API_KEY` and `TOPSTEP_TOKEN` are masked in the dashboard
  and in API responses.
- Live/funded execution stays locked until a future phase. There is
  no path through the dashboard to enable it in this build.
- The copier, MCP server, bracket orders, and the dashboard overhaul
  are explicitly out of scope here.
