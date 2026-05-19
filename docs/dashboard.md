# Dashboard

The Dashboard (`/`) is the central control surface for SignalBridge.
It owns the **Execution card** — the single place an operator chooses
the execution mode and confirms (or disables) live order routing.
The Broker Settings page is for connection/account configuration
only; it no longer exposes any execution-mode controls.

## Execution card

A compact card near the top of the Dashboard, titled **Execution**.
It is intentionally minimal — title, status pill, mode dropdown,
Save / Apply button, account line, and two secondary actions.

### What the card shows

* **Title** — `Execution`.
* **Status pill** — one of:
  * `Dry Run` — `EXECUTION_MODE=paper`. No broker orders fired.
  * `Demo` — `EXECUTION_MODE=demo`. Topstep demo/sim execution
    enabled.
  * `Live Locked` — `EXECUTION_MODE=live` but live is not yet
    engaged. Status pill is red but the card is dormant — no orders
    will route until the engagement flow has completed.
  * `Live Engaging` — transient client-side state while the live
    engagement animation + backend verification are running.
  * `Live Armed` — every live gate is satisfied. The card border
    glows red with a subtle pulse.
  * `Kill Switch Active` — the global kill switch is up.
* **Execution mode dropdown** — `dry-run` (`paper`) / `demo` / `live`.
* **Save / Apply** — primary action.
* **Account line** — `Account: <selected_account_id>` and the
  account name when known.
* **Disable Execution** — single-click reset to dry-run.
* **Exit All / Flatten** — `POST /api/broker/flatten-all`.

The old top-right cluster (`broker / mode / order exec / kill switch
/ account`) is gone. That information is either redundant with the
status pill / account line or available on the Broker account card
below.

### Mode behaviour

* **dry-run / paper** — Save / Apply calls `POST
  /api/execution/apply-mode` with `mode=paper`. The endpoint clears
  `ENABLE_TOPSTEP_ORDER_EXECUTION`, `TOPSTEP_EXECUTION_CONFIRM`,
  `ENABLE_LIVE_TRADING`, `LIVE_TRADING_CONFIRM`, and
  `LIVE_TRADING_ACCOUNT_ACK`.

* **demo** — Save / Apply calls the same endpoint with `mode=demo`.
  The backend flips `EXECUTION_MODE=demo`,
  `ENABLE_TOPSTEP_ORDER_EXECUTION=true`, and
  `TOPSTEP_EXECUTION_CONFIRM=DEMO_ONLY` automatically. **No phrase
  entry is required from the operator.** Live trading remains locked
  and every existing demo safety gate (provider, account, kill
  switch, live-lock) still runs.

* **live** — Save / Apply opens the **Live Execution Warning**
  modal. Live is never engaged just by picking it in the dropdown.

### Live engagement flow

1. Operator picks `live` and clicks Save / Apply.
2. The modal opens with the warning copy:
   > You are about to enable live/funded order routing for the
   > selected Topstep account. Orders may route to a real funded
   > account. Confirm only if you understand the risk.
3. The operator must:
   * Type `I_UNDERSTAND_LIVE_ORDERS` exactly.
   * Tick the checkbox: *I acknowledge orders will hit account
     `<selected_account_id>` / `<account_name>`*.
4. On submit:
   * Card switches to the `execution-live-engaging` state. Red
     border segments animate from the top and bottom edges and meet
     at the midpoints over ~3.6 s.
   * The browser calls `POST /api/topstep/live-execution/verify`
     (non-mutating gate preview).
   * If verify is ok, the browser calls `POST
     /api/topstep/live-execution/enable` with the typed phrase + the
     ack flag.
5. Outcomes:
   * If verify returns `failed_gates`, the animation stops, the
     pill returns to `Live Locked`, and the modal output shows the
     failed gates.
   * If enable succeeds, the card moves to `execution-live-armed`
     (solid red border + subtle pulse) and the status pill becomes
     `Live Armed`.

The verify endpoint exists so the UI can render the engagement
animation while the gate check is in flight, and so it can show a
deterministic failure reason without ever flipping settings.

### CSS state classes

The card carries exactly one of:

* `execution-dry-run`
* `execution-demo`
* `execution-live-locked`
* `execution-live-engaging` (transient, JS-applied)
* `execution-live-armed`
* `execution-kill-switch-active`
* `execution-disabled`

`prefers-reduced-motion` users still get the colour change but the
keyframe animations are dropped.

## Broker Settings page

`/settings/broker` is now **account-configuration only**:

* Broker provider selection.
* Topstep username / API key / account / env / base URL / WS URL.
* Selected account dropdown (populated by **Fetch accounts**).
* Tradovate placeholder fields.
* Test connection / Topstep auth / Fetch accounts buttons.
* **Account snapshot** — polling panel for positions/orders. This is
  **not** a realtime price feed; the label was explicitly renamed.

The execution-mode `<select>` is gone; the form preserves the
current `EXECUTION_MODE` value via a hidden input so submitting the
broker form never silently changes the mode. A small relocation
notice points operators back to the Dashboard for execution.

## Endpoints

* `POST /api/execution/apply-mode` — body `{ "mode": "paper" | "demo" }`.
  Live is rejected here on purpose.
* `POST /api/execution/disable` — clears all execution flags;
  returns the app to dry-run.
* `POST /api/topstep/live-execution/verify` — non-mutating live-gate
  preview. Returns `ok`, `failed_gates`, `selected_account_id`,
  `account_name`, `canTrade`, `kill_switch`, `live_allowed_symbols`,
  `live_max_contracts`.
* `POST /api/topstep/live-execution/enable` — unchanged. Still
  requires the exact confirmation phrase + account ack.
* `POST /api/topstep/live-execution/disable` — unchanged.
* `POST /api/topstep/demo-execution/{enable,disable}` — unchanged;
  the dashboard does not call them directly, but they remain for
  scripting / sbctl flows.
* `POST /api/broker/flatten-all` — paper flattens; topstep returns
  `not_implemented` (no live exit orders are submitted from this
  endpoint).

All endpoints require admin auth when `ADMIN_AUTH_ENABLED=true`.

## Ticker Watch (placeholder)

A separate card on the Dashboard scaffolds the future ProjectX
market-data hub:

* Selected ticker dropdown (sourced from configured symbol
  mappings).
* Mapped contract ID (from the symbol map).
* Current price — `Not connected yet`.
* Mode — `polling / SignalR not enabled`.

No live market data is wired yet.

## Safety guarantees preserved

* Live execution is **never** enabled by default.
* `apply-mode` cannot set `EXECUTION_MODE=live` — that path goes
  through verify + enable with the typed confirmation phrase + the
  account acknowledgement.
* The kill switch, allowed symbols, max-contracts cap, and existing
  broker-level safety check (`_live_execution_safety_check`) are
  unchanged.
* Disable Execution does not touch broker credentials or the
  selected account — only the execution flags.
