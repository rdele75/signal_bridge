# Topstep / TopstepX integration

SignalBridge talks to Topstep through the
[**ProjectX**](https://www.topstepx.com/) REST API. Post-collapse
(2026-05-21) Topstep is the only adapter — every order SignalBridge
submits is a real ProjectX order on the selected Topstep account.

| Layer | Default | Notes |
|-------|---------|-------|
| Read-only account / position / order data | **on** | `Auth/loginKey`, `Account/search`, `Position/searchOpen`, `Order/searchOpen`, `Order/search` |
| Order submission (`/api/Order/place`) | **gated** | Off skips the adapter; Test builds the payload without POSTing; Armed POSTs |
| Flatten / cancel-all | **Armed only** | `Position/closeContract`, `Order/cancel` |

Bracket orders (OCO / bracketed stop+target) and the SignalR user hub
remain on the TODO list — see the **SignalR user hub (TODO)** section
below.

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
  appear in the dashboard / API (last 4 characters only).

## Read-only data

The adapter calls these endpoints. Each one returns a structured
envelope (`ok`, `status`, `provider`, the response payload, masked
credential summary) and never crashes.

| Method | Endpoint | Body |
|--------|----------|------|
| `authenticate()` | `POST /api/Auth/loginKey` | `{userName, apiKey}` |
| `get_accounts()` | `POST /api/Account/search` | `{onlyActiveAccounts: true}` |
| `get_selected_account()` | (in-memory match on accounts) | compares ids as **trimmed strings** so numeric ProjectX ids and the user-saved string form match |
| `get_positions()` | `POST /api/Position/searchOpen` | `{accountId: <int>}` |
| `get_orders()` | `POST /api/Order/searchOpen` | `{accountId: <int>}` |
| `search_orders(start, end)` | `POST /api/Order/search` | `{accountId, startTimestamp?, endTimestamp?}` |
| `test_connection()` | auth + accounts | reports `selected_account`, `accounts_count`, masked token cache |

`TOPSTEP_ACCOUNT_ID` must be the **numeric ProjectX account id** (the
`id` field returned by `/api/Account/search` — e.g. `5001`). The value
is stored as a string but compared as a trimmed string against the
numeric returned by ProjectX, so both shapes match cleanly. A
non-numeric value returns a `non_numeric_account_id` envelope without
hitting the wire.

## Execution states

SignalBridge has three execution states. Each TradingView signal goes
through the risk engine first (kill switch, allowlist, contracts cap,
direction toggles, daily loss, open positions, duplicate cooldown);
the state determines what happens after risk passes.

### Off

The webhook handler journals the signal as accepted and returns. The
broker adapter is never asked to execute. The kill switch is
irrelevant in this state.

### Test

`submit_market_order` builds the `/api/Order/place` payload via
`topstep_order_builder.build_market_order_payload`, logs the build,
journals the attempt, and returns
`{ok: True, submitted: False, mode: "test"}`. It never POSTs to
ProjectX. The test path uses the general `ALLOWED_SYMBOLS` list — the
stricter armed-symbol allowlist does not apply.

### Armed

`submit_market_order` runs the armed gate stack:

1. Credentials present (`TOPSTEP_USERNAME` + `TOPSTEP_API_KEY`).
2. Selected account id is numeric.
3. `canTrade` flag for the selected account is true (when known; an
   unknown flag emits a one-shot WARNING and lets the trade through —
   the operator may not have clicked Fetch Accounts yet).
4. Kill switch is off, when `ENABLE_KILL_SWITCH=true`.
5. Signal symbol is in `ALLOWED_SYMBOLS_ARMED`.
6. Signal contracts ≤ `MAX_CONTRACTS_PER_TRADE`.

On pass, the adapter POSTs `/api/Order/place` (with the H5 auth-retry
shim — one re-authenticate + retry on HTTP 401 or a known auth
errorCode). The ProjectX response is journaled in full.

### Flipping state

The Dashboard execution-card dropdown is the canonical surface. The
underlying endpoints:

| Endpoint | Effect |
|----------|--------|
| `POST /api/execution/off`  | sets `EXECUTION_MODE=off`. |
| `POST /api/execution/test` | sets `EXECUTION_MODE=test`. |
| `POST /api/execution/arm`  | runs the gate-stack check, then sets `EXECUTION_MODE=armed`. Refuses with `no_selected_account` / `kill_switch_active` / `no_armed_symbols` when a gate fails. |
| `POST /api/execution/submit-test-order` | builds a synthetic 1-contract MES BUY order against ProjectX without POSTing. Works regardless of the current execution state. Used as a smoke test from the Dashboard. |

No confirmation tokens. No acknowledgement checkboxes. The operator
selects the account and clicks Apply.

## ProjectX market-order payload

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

SignalBridge maps TradingView tickers to ProjectX contract ids (e.g.
`MES1!` → `CON.F.US.MES.M26`). The builder refuses to guess: if the
Topstep contract id for a ticker is missing **or blank**, it returns
`symbol_mapping_missing` with the message
`Topstep contract id missing for <ticker>. Add it in Configuration > Symbols.`

Edit mappings at **Configuration → Symbols** (`/settings/symbols`). The
page edits `config/symbols.json` directly. The same page hosts a
**Topstep contract search** tool: type a search term (e.g. `NQ` or
`ES`), click **Search Topstep Contracts**, and use the **Copy** button
to paste the active contract id into the Topstep column above.

## Flatten / cancel-all

`flatten_position(symbol=None)` and `cancel_all_orders(symbol=None)`
work only in Armed mode. Both:

- Bypass the kill-switch gate. Closing existing state remains
  available after emergency stop.
- Hit `/api/Position/closeContract` (flatten) or `/api/Order/cancel`
  (cancel-all) once per leg.
- Return a structured envelope with one entry per leg. Partial
  failures are reported, not raised.

Off and Test states refuse the call with `status: "not_armed"`.

## Order history

`POST /api/broker/order-history` returns recent submitted orders via
ProjectX's `/api/Order/search` (defaults: 7-day lookback,
100-row limit; overridable via `ORDER_HISTORY_LOOKBACK_DAYS` and
`ORDER_HISTORY_LIMIT`). The `/metrics` page renders this as the Past
Orders table.

## Realtime account/order/position data

### Polling (implemented)

`/api/realtime/state` returns a snapshot built from
`broker.get_positions()` + `broker.get_orders()` so the dashboard JS
can refresh open positions and working orders without each panel
making its own request. Default polling interval is 5s
(`TOPSTEP_REALTIME_POLL_SECONDS`).

### SignalR user hub (TODO)

ProjectX exposes a SignalR user hub at `TOPSTEP_WS_URL` for push
updates. Wiring requires the `signalrcore` Python dependency; until
then `TOPSTEP_REALTIME_MODE=signalr` falls back to polling.

## Emergency stop

In order, fastest first:

1. **Flip to Off via the Dashboard** — the mode dropdown's Off option
   skips the broker on every subsequent signal.
2. **Activate the kill switch** (top-bar button) — blocks new Armed
   orders if you're still in Armed mode.
3. **Stop the server** (`pkill -f uvicorn` or `Ctrl+C`).
4. **Cut the tunnel** (e.g. `tailscale funnel off`) so TradingView
   can't even reach the webhook.

## Configuration reference

| Variable | Default | Notes |
|----------|---------|-------|
| `BROKER_PROVIDER` | `topstep` | Pinned. Other values are rejected. |
| `EXECUTION_MODE` | `off` | `off` / `test` / `armed`. Edit from the Dashboard. |
| `TOPSTEP_USERNAME` | *(empty)* | TopstepX login email. |
| `TOPSTEP_API_KEY` | *(empty)* | TopstepX/ProjectX API key. |
| `TOPSTEP_ACCOUNT_ID` | *(empty)* | numeric ProjectX account id (e.g. `5001`); stored as a string. |
| `SELECTED_ACCOUNT_ID` | *(empty)* | mirror of `TOPSTEP_ACCOUNT_ID` written when the Dashboard's account dropdown saves. |
| `TOPSTEP_ENV` | `demo` | reserved; `live` is rejected (live execution is driven by `EXECUTION_MODE=armed`, not this knob). |
| `TOPSTEP_BASE_URL` | `https://api.topstepx.com` | REST base URL. |
| `TOPSTEP_WS_URL` | `https://rtc.topstepx.com` | SignalR hub URL; client not wired yet. |
| `TOPSTEP_TOKEN` | *(empty, written by adapter)* | cached JWT; masked everywhere. |
| `TOPSTEP_TOKEN_EXPIRES_AT` | *(empty, written by adapter)* | ISO-8601 expiry. |
| `ALLOWED_SYMBOLS` | `MNQ1!,MES1!,NQ1!,ES1!` | general allowlist; applied in every execution state. |
| `ALLOWED_SYMBOLS_ARMED` | `MES1!,MNQ1!` | stricter allowlist applied only when armed. Entries must also appear in `ALLOWED_SYMBOLS`. |
| `MAX_CONTRACTS_PER_TRADE` | `1` | hard cap, applied uniformly in Test and Armed. |
| `ENABLE_KILL_SWITCH` | `true` | when false the kill-switch feature is disabled entirely (the Dashboard button is decorative). |
| `ORDER_HISTORY_LOOKBACK_DAYS` | `7` | default lookback for the Past Orders table. |
| `ORDER_HISTORY_LIMIT` | `100` | default row cap. |
| `ENABLE_TOPSTEP_REALTIME` | `false` | enable the realtime poller (best-effort, see note above). |
| `TOPSTEP_REALTIME_MODE` | `polling` | `polling` or `signalr` (signalr falls back to polling today). |
| `TOPSTEP_REALTIME_POLL_SECONDS` | `5` | polling interval. |

## Try it locally

1. Set `TOPSTEP_USERNAME` (email) + `TOPSTEP_API_KEY` in
   `/settings/broker`.
2. Click **Test Topstep auth** → expect `status: authenticated`.
3. Click **Fetch accounts** → pick **Use this account** on the row
   you trade from. That writes both `SELECTED_ACCOUNT_ID` and
   `TOPSTEP_ACCOUNT_ID` to the numeric ProjectX id.
4. Open `/settings/symbols`, search for your contracts (e.g. `MES`),
   and copy the active contract ids into the Topstep column.
5. From the Dashboard, click **Smoke Test** — confirms the adapter
   can build a payload against ProjectX without submitting.
6. To run real submissions: change the execution-mode dropdown to
   `armed` and click Apply. The Armed gate stack runs first; if any
   gate fails the dropdown reverts and the failure surfaces inline.
7. The funded/eval badge next to the Armed status confirms which
   account class you're about to trade on.

## Secrets / safety reminders

- **Never share or commit API keys or tokens.** `.env` is gitignored.
  `TOPSTEP_API_KEY` and `TOPSTEP_TOKEN` are masked in the dashboard
  and in API responses.
- **Off is the default state.** First boot and every restart land in
  Off; the operator has to deliberately switch to Test or Armed.
- **Armed execution submits real orders to your Topstep account.**
  Verify `MAX_CONTRACTS_PER_TRADE`, `ALLOWED_SYMBOLS_ARMED`, and the
  selected account before arming. The Dashboard surfaces blockers
  inline so a missing knob shows up before you click Apply.
- The copier, MCP server, bracket orders, and the SignalR user hub
  are explicitly out of scope here.
