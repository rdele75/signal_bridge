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
* **Status text** — small, low-key label next to the title. Reflects
  the current state (`Execution Test`, `Live Locked`, `Live Engaging`,
  `Live armed`, `Kill Switch Active`). Not a colored bubble — the
  selected mode in the dropdown is the canonical state.
* **Execution mode dropdown** — `Execution Test` and `live` only. Topstep
  does not expose a freely controllable demo/paper surface, so the
  UI no longer offers a "demo" choice in this dropdown. The backend
  still accepts demo for scripted / sbctl flows.
* **Apply** — primary action. While the request is in flight a
  small spinner sits next to the button label; the card never
  dumps raw JSON, just a short human-readable status message.
* **Account line** — `Account: <selected_account_id>` and the
  account name when known.
* **Smoke Test** — visible in Execution Test mode. Runs a Topstep
  smoke test against the selected account + the contract mapping.
  Clicking this button **always** runs the dry-run preview only —
  it builds a BUY entry + SELL exit payload pair and never calls
  `/api/Order/place`. Helper text reads:
  *Runs an Execution Test enter/exit check. No broker order is sent.*
* **Execute smoke test…** — secondary button that appears only when
  execution is already armed (demo or live). Opens a dedicated
  confirmation modal that requires:
  * Typing `smoke` exactly in the confirmation field.
  * Checking the acknowledgement box:
    *I understand this will place and exit 1 MES on the selected
    Topstep account.*
  When both gates pass, the browser POSTs `execute=true` with
  `confirmation="smoke"`. The backend then re-runs every safety gate
  (provider, account, mapping, kill switch, contract cap, armed
  state) before submitting the BUY entry + SELL exit pair.
* **Disengage** — single-click reset to Execution Test. Disarms live
  + demo flags, returns the app to a safe state, and never disconnects
  broker credentials. Visible only when execution is armed; hidden in
  the Execution Test state where there is nothing to disengage.
* **Exit All / Flatten** — `POST /api/broker/flatten-all`.

The old top-right meta cluster (`broker / mode / order exec / kill
switch / account`) is gone, and so is the colored status pill —
they were redundant with the dropdown + account line.

### Mode behaviour

* **Execution Test / paper** — Apply calls `POST
  /api/execution/apply-mode` with `mode=paper`. The endpoint clears
  `ENABLE_TOPSTEP_ORDER_EXECUTION`, `TOPSTEP_EXECUTION_CONFIRM`,
  `ENABLE_LIVE_TRADING`, `LIVE_TRADING_CONFIRM`, and
  `LIVE_TRADING_ACCOUNT_ACK`.

* **live** — Apply opens the **Live Execution Warning** modal.
  Live is never engaged just by picking it in the dropdown.

* **demo** (scripted only) — the apply-mode endpoint still accepts
  `mode=demo` for sbctl / scripting flows. It flips
  `EXECUTION_MODE=demo`, `ENABLE_TOPSTEP_ORDER_EXECUTION=true`, and
  `TOPSTEP_EXECUTION_CONFIRM=DEMO_ONLY` automatically. Live trading
  remains locked, and every existing demo safety gate still runs.
  The dashboard does not expose this option in the dropdown.

### Live engagement flow

1. Operator picks `live` and clicks Apply.
2. The modal opens with the warning copy:
   > You are about to enable live/funded order routing for the
   > selected Topstep account. Orders may route to a real funded
   > account. Confirm only if you understand the risk.
3. The operator must:
   * Type `engage` exactly (the short typed phrase replaces the
     legacy `I_UNDERSTAND_LIVE_ORDERS` UX wording — the long token
     still lives in `LIVE_TRADING_CONFIRM` as the persisted broker
     safety check, but the operator never sees it).
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
     (solid red border + subtle pulse), the status text becomes
     **Live armed** with a persistent green check, a one-shot green
     success-flash shimmer plays around the border, and a toast
     notification slides in from the bottom-right with the message
     *Live execution armed*. The toast fades out after a couple of
     seconds; the checkmark stays visible for as long as live remains
     armed.

The verify endpoint exists so the UI can render the engagement
animation while the gate check is in flight, and so it can show a
deterministic failure reason without ever flipping settings.

### Live → Execution Test transition

Selecting `Execution Test` (or clicking **Disengage**) while live is armed
no longer snaps the card from red to green. Instead:

1. The status text fades to *Disengaging live…*.
2. The card adds `execution-live-disengaging`. The red border bars
   retreat from the edges toward the centre and the red glow fades
   over ~1.5 s.
3. A toast slides in with *Live execution disengaged*.
4. The card lands on `execution-dry-run` with the one-shot
   `execution-dryrun-enter` cue, then settles into the slow
   `execution-dry-run-pulse` breathing animation.
5. The status text becomes *Execution Test* and the page reloads to
   pick up the persisted state.

### CSS state classes

The card carries exactly one of:

* `execution-dry-run`
* `execution-demo`
* `execution-live-locked`
* `execution-live-engaging` (transient, JS-applied)
* `execution-live-armed`
* `execution-live-disengaging` (transient, JS-applied)
* `execution-kill-switch-active`
* `execution-disabled`

Execution Test runs a deliberately slow safe-state breathing animation
(`execution-dry-run-pulse`, ~6.5 s cycle) — gentle blue/green
glow that signals "alive but idle" without competing with the
live indicators. Live armed pulses faster (~1.6 s) so the two
states are visually distinct at a glance.

When the live engagement flow finishes successfully, the status
text fades through these CSS classes:

* `execution-status-transitioning` — used by JS to fade the old
  text out before swapping the label.
* `execution-live-armed-enter` — a one-shot animation that flashes
  a green checkmark + colour swing as the label settles into
  "Live armed" red.
* `execution-status-check-visible` — persistent class added once
  the entry animation finishes. Keeps the green checkmark next to
  the status text for as long as live remains armed.
* `execution-live-success-flash` — one-shot green shimmer/glow
  overlay applied to the card immediately after engagement.

The reverse path uses `execution-live-disengaging` (red bars
retreat toward centre + fade) followed by `execution-dryrun-enter`
(one-shot dry-run lead-in) before the steady
`execution-dry-run-pulse` resumes.

Toast notifications use `execution-toast`, `execution-toast-enter`,
and `execution-toast-exit` for the slide/fade-in and slide/fade-out
transitions. The toast container is fixed-positioned at the
bottom-right corner so it never causes layout shift.

`prefers-reduced-motion` users get the colour change without the
keyframe animations.

## Broker Settings page

`/settings/broker` is now **account-configuration only**:

* Broker provider selection.
* Topstep username / API key / account / env / base URL / WS URL.
* Selected account dropdown (populated by **Fetch accounts**).
* Test connection / Topstep auth / Fetch accounts buttons.

The execution-mode `<select>` is gone; the form preserves the
current `EXECUTION_MODE` value via a hidden input so submitting the
broker form never silently changes the mode. A small relocation
notice points operators back to the Dashboard for execution.

### Account snapshot UI removed

The bulky **Account snapshot** / **Realtime account data** polling
panel was removed from both the dashboard and the broker page. It
was redundant with the per-account information surfaced elsewhere
(Metrics → Past orders for order history; broker credentials on
this page; at-a-glance broker provider on the dashboard).

The backend endpoints are unchanged and remain available for
tooling and tests:

* `GET /api/realtime/state` — combined positions + orders snapshot.
* `GET /api/broker/positions`.
* `GET /api/broker/orders`.

## Endpoints

* `POST /api/execution/apply-mode` — body `{ "mode": "paper" | "demo" }`.
  Live is rejected here on purpose.
* `POST /api/execution/disable` — clears all execution flags;
  returns the app to Execution Test. The dashboard calls this from
  the **Disengage** button.
* `POST /api/topstep/smoke-test` — dual-mode. Body:
  `{ "symbol": "MES1!", "contracts": 1, "execute": false,
  "confirmation": "" }`. With `execute=false` (default) it builds
  BUY entry + SELL exit previews and never calls
  `/api/Order/place` (returns `would_submit=false`). With
  `execute=true` it requires `confirmation="smoke"` exactly, plus
  every armed-execution prerequisite (provider topstep, selected
  account, valid mapping, kill switch off, `contracts ≤
  MAX_CONTRACTS_PER_TRADE`, `ENABLE_TOPSTEP_ORDER_EXECUTION=true`,
  and — if `EXECUTION_MODE=live` — the full live gate stack via
  `submit_market_order`). When everything passes, it places the
  entry, then the exit, journals both, and returns both broker
  responses.
* `POST /api/topstep/live-execution/verify` — non-mutating live-gate
  preview. Returns `ok`, `failed_gates`, `selected_account_id`,
  `account_name`, `canTrade`, `kill_switch`, `live_allowed_symbols`,
  `live_max_contracts`.
* `POST /api/topstep/live-execution/enable` — unchanged contract.
  The typed `confirm` body field must equal the short phrase
  `engage`; the endpoint persists the long-form token
  (`I_UNDERSTAND_LIVE_ORDERS`) into `LIVE_TRADING_CONFIRM`, so the
  broker safety check sees the same value it always has.
* `POST /api/topstep/live-execution/disable` — unchanged.
* `POST /api/topstep/demo-execution/{enable,disable}` — unchanged;
  the dashboard does not call them directly, but they remain for
  scripting / sbctl flows.
* `POST /api/broker/flatten-all` — paper flattens; topstep returns
  `not_implemented` (no live exit orders are submitted from this
  endpoint).

All endpoints require admin auth when `ADMIN_AUTH_ENABLED=true`.

## Ticker Watch (placeholder)

A separate card on the Dashboard is an honest placeholder until the
ProjectX market-data hub lands. It shows:

* *Ticker Watch is not connected yet.*
* *Realtime price feed will be added through ProjectX market data
  later.*

No broken controls, no dropdowns that imply a connection that does
not exist yet. The card will gain a real symbol selector + last
price once the ProjectX market hub is wired in — until then the
placeholder copy is deliberately blunt so the operator never
mistakes a stale value for a live feed.

## Safety guarantees preserved

* Live execution is **never** enabled by default.
* `apply-mode` cannot set `EXECUTION_MODE=live` — that path goes
  through verify + enable with the typed phrase + the account
  acknowledgement.
* The kill switch, allowed symbols, max-contracts cap, and existing
  broker-level safety check (`_live_execution_safety_check`) are
  unchanged. The stored `LIVE_TRADING_CONFIRM` token is the same
  long-form value as before — only the user-facing typed phrase
  changed (to `engage`).
* Disengage (the dashboard's reset action) does not touch broker
  credentials or the selected account — only the execution flags.
* The Smoke Test endpoint is dry-run only. It refuses to run while
  `EXECUTION_MODE=live` (the `dry_run_mode` check fails), and never
  hits `/api/Order/place` regardless of state.
