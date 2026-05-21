# Functional Audit — 2026-05-20

Read-only static + dynamic audit of SignalBridge against the safety
claims in `docs/audit.md` and the inline docstrings. No source files
were modified during this pass; the only deliverable is this document.
Every finding cites `file:line` for the operator to verify directly.

## Summary

- Tests passing/failing: **532 passed, 0 failed, 0 skipped** (50.52s).
- Critical findings: **2**
- High findings: **5**
- Medium findings: **6**
- Low findings: **4**

### Top 3 things the operator should fix before any UI work

1. **C1** — `SessionMiddleware` falls back to a hardcoded session key when
   `SESSION_SECRET` is unset (`app/main.py:309`). With auth enabled and
   no real secret, sessions are forgeable from anyone who reads the
   source. Combined with H2, the app starts in this state without
   refusing.
2. **C2** — Default `TRADINGVIEW_WEBHOOK_SECRET` is the public string
   `"change_me_to_a_long_random_secret"` (`app/config.py:71`). Nothing
   refuses startup or warns on it; if exposed over Tailscale Funnel
   without changing the value, anyone can submit valid trade signals.
3. **H1** — Duplicate-order cooldown has a read-then-write race
   (`app/risk_engine.py:189-195` → `app/webhook.py:533-637`): the
   journal lock is per-operation, so two concurrent webhooks with the
   same `order_id` can both pass the duplicate check before either
   records as accepted. Real TradingView strategies fire twice on a
   single bar close routinely.

---

## Findings

### CRITICAL

#### C1 — Hardcoded session-secret fallback enables forged admin sessions
- **Location**: `app/main.py:306-313` (specifically the literal
  `"signalbridge-fallback-secret"` on line 309).
- **What**: When admin auth is enabled, `SessionMiddleware` is
  initialised with
  `secret_key=settings.session_secret or "signalbridge-fallback-secret"`.
  If `SESSION_SECRET` is unset or empty in the environment, the
  middleware silently uses a deterministic, public string baked into
  the source. Anyone with read access to this repository (or this
  audit doc) can forge a valid admin cookie.
- **Why it matters**: Once SignalBridge is reachable over Tailscale
  Funnel (the documented trajectory), an attacker who recovered the
  fallback string from public Git history or any prior `git clone`
  can mint an admin session and arm live trading on the operator's
  account.
- **Reproducible by**: Code reading. `warn_if_default_secrets` at
  `app/auth.py:181-188` only **logs** a warning for the
  `DEFAULT_SESSION_SECRET` (`"generate_or_require_secret"`) — it does
  not even cover the case where `session_secret` is the empty string,
  which is the exact case the `or "signalbridge-fallback-secret"`
  fallback in `main.py:309` targets.
- **Recommendation**: Refuse to start when `admin_auth_enabled=True`
  and `settings.session_secret` is falsy or equals
  `DEFAULT_SESSION_SECRET`. Remove the `or "..."` fallback so the
  refusal is explicit, not silent.

#### C2 — Default `TRADINGVIEW_WEBHOOK_SECRET` is a public placeholder
- **Location**: `app/config.py:69-73`; detection only at the dashboard
  level in `app/main.py:3419`.
- **What**: If the operator doesn't override
  `TRADINGVIEW_WEBHOOK_SECRET` in env or via the `/tradingview` page,
  the webhook secret is the literal
  `"change_me_to_a_long_random_secret"`. The webhook handler at
  `app/webhook.py:129` compares against this default the same way it
  would compare against a real secret — `hmac.compare_digest` is used
  correctly, but the secret being checked is publicly known.
- **Why it matters**: SignalBridge's documented future is
  `tailscale funnel` for real TradingView alerts (`docs/audit.md:142`).
  Any attacker who reaches the public webhook URL with the default
  secret submits valid trade signals. Every risk gate downstream
  (symbol allow-list, contracts cap, duplicate cooldown) still
  applies, so the blast radius is bounded — but if the operator also
  has live execution armed and the attacker picks an allowed symbol
  inside the contract cap, the attacker can fire real orders.
- **Reproducible by**: Code reading. The only detection lives in the
  `/tradingview` template render path (`app/main.py:3419`) to colour
  a badge — startup neither warns nor refuses.
- **Recommendation**: At app startup, refuse to bind to a non-
  localhost interface (`APP_HOST` not in {`127.0.0.1`, `localhost`,
  `::1`}) while the webhook secret equals the default. Inside-only
  installs stay friction-free; public exposure forces a real secret.

---

### HIGH

#### H1 — Duplicate-order cooldown is racy under concurrent webhooks
- **Location**: `app/risk_engine.py:189-195` (the `find_recent_order_id`
  read) and `app/webhook.py:533, 619-637` (the `record_signal` write
  that lands after the broker call).
- **What**: `RiskEngine.evaluate` queries
  `journal.find_recent_order_id(...)` to detect duplicates. The
  journal lock (`app/journal.py:74-83`) is held per-call only — read
  and write are separate locked operations. The webhook handler then
  calls the broker (which can take hundreds of ms over the network)
  and only writes the new signal as `accepted` after the broker
  returns (`app/webhook.py:619`). Two TradingView webhooks with the
  same `order_id` arriving inside that read→write window both see no
  recent duplicate, both pass risk, and both can hit the broker.
- **Why it matters**: TradingView strategies routinely fire two
  alerts on a single bar close — both with the same alert id or
  comment used as `order_id`. The result is a duplicated trade, which
  in live mode is real money. Anecdotally TradingView's own
  `{{strategy.order.alert_message}}` and `{{strategy.alert_message}}`
  alerts can land within tens of milliseconds.
- **Reproducible by**: Code reading; no existing test exercises two
  concurrent calls to `WebhookHandler.handle`. Easy to demonstrate
  with `pytest` + `threading.Thread` or `concurrent.futures`.
- **Recommendation**: Either narrow the window (atomic
  reserve-then-execute pattern using a unique index on `order_id` +
  inserting a `decision='pending'` row before the broker call), or
  serialize the webhook hot path behind a single lock. The latter is
  acceptable given the single-operator scope.

#### H2 — Daily PnL boundary is UTC, not operator-local
- **Location**: `app/journal.py:222-226, 233-236` (`get_daily_pnl`,
  `add_daily_pnl`); `app/journal.py:347` (`count_today` via
  SQLite `date('now')` which is UTC); risk gate at
  `app/risk_engine.py:181-186`.
- **What**: Daily PnL is keyed on `datetime.now(timezone.utc).date()`.
  The day-rollover boundary therefore happens at 00:00 UTC, regardless
  of the operator's local timezone. For a US Eastern operator that's
  19:00 EST / 20:00 EDT — well before local midnight, while the
  operator may still be actively trading. The risk engine's
  `max_daily_loss` check (`app/risk_engine.py:181-186`) reads the
  same UTC bucket, so a loss limit that triggered at 14:00 local
  resets at 19:00–20:00 local on the same calendar day.
- **Why it matters**: Operators outside UTC will see two daily-loss
  windows per calendar day with no clear announcement. A trader who
  was halted by the loss limit can find themselves silently re-armed
  during the same session. This contradicts the implicit safety
  claim of `MAX_DAILY_LOSS`.
- **Reproducible by**: Code reading. Test would freeze
  `datetime.now(timezone.utc)` across the UTC boundary while local
  time is mid-afternoon.
- **Recommendation**: Make the day-bucket configurable
  (`DAILY_PNL_TZ` env var) or document the UTC convention loudly in
  `docs/audit.md`. Either is acceptable; silent UTC roll is not.

#### H3 — `max_open_positions` only counts paper positions
- **Location**: `app/risk_engine.py:198-207` (the cap check);
  `app/journal.py:191-196` (`count_open_positions`); position
  upserts only fire from `app/execution/paper.py:297-302`.
- **What**: `RiskEngine.evaluate` calls
  `journal.count_open_positions()` to enforce `MAX_OPEN_POSITIONS`.
  But the `positions` table is only written by the paper broker —
  `topstep.py` never calls `journal.upsert_position`, and the
  webhook handler doesn't touch the table from the Topstep path.
  For an operator running the Topstep adapter (demo or live), the
  count is permanently 0 and the cap never trips.
- **Why it matters**: The dashboard, README, and `docs/audit.md`
  imply `MAX_OPEN_POSITIONS` is enforced uniformly. It isn't. A
  Topstep operator who relies on the cap to prevent over-trading is
  exposed.
- **Reproducible by**: Inspect the journal `positions` table after a
  Topstep order — empty even after `submit_market_order` returns
  `accepted=true`.
- **Recommendation**: Either (a) write position state to the journal
  from the Topstep submission path (mirrors paper), or (b) treat the
  cap as paper-only and document accordingly. Mixing the two
  silently is the failure mode.

#### H4 — `/api/broker/flatten-all` is a no-op on Topstep
- **Location**: `app/main.py:2721-2751`;
  `app/execution/topstep.py:1298-1306` (`flatten_position` and
  `cancel_all_orders` return a `not_implemented` envelope unchanged).
- **What**: The Dashboard "Exit All / Flatten" button (visible in
  the execution-actions row at every breakpoint) posts to
  `/api/broker/flatten-all`. With Topstep as the active broker, the
  handler returns `{"ok": false, "status": "not_implemented",
  "not_implemented": true, ...}` without touching any open Topstep
  positions. The dashboard JS surfaces a yellow toast saying
  "topstep: flatten not implemented yet" — but the button itself
  looks identical to a working control.
- **Why it matters**: This is the operator's emergency-exit muscle
  memory. Disengage stops new orders; Flatten / Exit All is what an
  operator reaches for to close existing open positions. In a
  funded-account context, an operator under pressure who clicks the
  button and watches it run will assume their position is being
  closed. It isn't.
- **Reproducible by**: With `BROKER_PROVIDER=topstep`, `curl -X POST`
  to `/api/broker/flatten-all` (with admin session) — the JSON
  response confirms.
- **Recommendation**: Hide / disable the button on the dashboard
  while the Topstep adapter is active, or rename it to make the
  scope explicit ("Flatten paper positions"). The "Emergency stop"
  section of `docs/audit.md` should explicitly state that closing
  open Topstep positions must be done from Topstep's own UI.

#### H5 — Topstep token can expire mid-call with no retry
- **Location**: `app/execution/topstep.py:64` (`TOKEN_TTL_HOURS = 23`);
  `app/execution/topstep.py:246-257` (`_is_token_valid`);
  `app/execution/topstep.py:1130-1296` (`submit_market_order`'s single
  POST, no retry on auth failure).
- **What**: The Topstep adapter reuses a cached JWT for up to 23h.
  `_is_token_valid` checks the cached `token_expires_at` locally —
  but that timestamp is the local mint time + 23h, not what the
  Topstep server actually enforces. If a trade fires at 22h59m, the
  local check passes; if the token has actually expired server-side
  by the time the `POST /api/Order/place` lands, ProjectX returns an
  auth-rejected envelope. `submit_market_order` (line 1207) makes
  exactly one POST; on `submit_rejected` it returns up the stack and
  the webhook handler journals the rejection — the trade is dropped.
- **Why it matters**: A live-trading window that straddles the 23h
  re-auth boundary loses trades silently to "submit_rejected" with no
  automatic retry. The operator only finds out post-hoc in the
  journal.
- **Reproducible by**: Force `token_expires_at` to a value 30 seconds
  in the future, then exercise `submit_market_order`. The local
  check passes; the broker rejects. The handler does not re-auth.
- **Recommendation**: On `submit_market_order` rejection with an
  `errorCode` shape suggesting expired-auth, re-`authenticate()` once
  and retry the POST. Constrain to one retry to avoid loops. Note
  this is a documented HTTP-client hardening in `docs/audit.md:79-81`
  but the current code doesn't implement it.

---

### MEDIUM

#### M1 — Documented `canTrade` gate is not enforced at submission
- **Location**: `app/execution/topstep.py:1039-1105`
  (`_demo_execution_safety_check` and `_live_execution_safety_check`);
  contrast with the docstring claim at
  `app/execution/topstep.py:25,33` and `docs/audit.md:47, 63`.
- **What**: Both docstrings claim "Selected account must be numeric
  and (if known) ``canTrade``." In practice the safety checks only
  test `self._numeric_account_id() is None`. `can_trade` is parsed
  from the broker response at `topstep.py:435` for display only —
  the value is never consulted in the gate evaluation. The
  `/api/topstep/live-execution/enable` endpoint
  (`app/main.py:1121-1320`) also doesn't verify `canTrade` before
  flipping all six gate flags together.
- **Why it matters**: The doc-vs-code mismatch can give the operator
  false confidence that the broker's own `canTrade=false` flag will
  stop submission. It will not — orders will be built and POSTed; the
  broker will reject them downstream. That's still safe (no money
  moves) but the failure surfaces as `submit_rejected` rather than a
  clean gate refusal.
- **Reproducible by**: Mock a broker probe returning `canTrade=false`;
  run `_live_execution_safety_check(signal)` — returns `None`
  (all gates pass).
- **Recommendation**: Either implement the `canTrade` gate, or
  remove the docstring/`docs/audit.md` claims that it's enforced.

#### M2 — `ENABLE_KILL_SWITCH=false` silently disables the entire kill switch
- **Location**: `app/kill_switch.py:13-23` (the `enabled` flag);
  `app/main.py:285-288` (construction); `app/config.py:102-104`
  (`ENABLE_KILL_SWITCH` env default `True`).
- **What**: `KillSwitch(enabled=False).is_active()` always returns
  `False` regardless of the sentinel file. The flag is driven by
  `settings.enable_kill_switch` which reads
  `_bool("ENABLE_KILL_SWITCH", True)`. Default is safe (True); but if
  the operator (or a test config) sets `ENABLE_KILL_SWITCH=false`,
  the kill switch becomes a no-op, the dashboard toggle no longer
  blocks trades, and the live-trading "kill switch must be off"
  gate (`app/execution/topstep.py:1083`) trivially passes because
  `kill_switch_active=false` is mirrored from `is_active()`.
- **Why it matters**: A misconfigured `.env` silently disables the
  most important safety surface. There is no startup warning when
  the flag is `False`.
- **Reproducible by**: Set `ENABLE_KILL_SWITCH=false` in env, start
  the app, click the topbar kill switch — `is_active()` still
  returns False, signals still flow.
- **Recommendation**: Log a loud startup warning if
  `enable_kill_switch=False`. Consider refusing to start in live
  mode while the switch is disabled.

#### M3 — App starts with auth disabled on any interface, no guard
- **Location**: `app/auth.py:171-200` (only warns, never refuses);
  `app/config.py:55` (no bind-host guard at startup).
- **What**: `warn_if_default_secrets` is the only startup posture
  check. It logs warnings for: auth disabled, default
  `SESSION_SECRET`, default `ADMIN_PASSWORD`. None of these refuse
  the boot. The operator can bind to `0.0.0.0:8000` with
  `ADMIN_AUTH_ENABLED=false` and the app will start, exposing the
  dashboard wide open on whatever network the host is on.
- **Why it matters**: Local-only deployment is the safe path; the
  trajectory is Tailscale Funnel. If the operator mis-binds during
  setup, there is no safety net at the process layer.
- **Reproducible by**: `APP_HOST=0.0.0.0 ADMIN_AUTH_ENABLED=false
  python -m uvicorn app.main:create_app --factory` — boots cleanly
  with only a warning in logs.
- **Recommendation**: Refuse to start when `admin_auth_enabled=False`
  and `app_host` is not `127.0.0.1` / `localhost` / `::1`.

#### M4 — Position-state divergence between journal and Topstep is unreconciled
- **Location**: `app/risk_engine.py:198-207` reads journal;
  `app/execution/topstep.py` never writes to journal positions; no
  reconciliation loop exists.
- **What**: If the operator manually closes a Topstep position from
  Topstep's own UI, or Topstep auto-flattens at EOD / for a drawdown
  rule, the SignalBridge journal still believes the position is open
  (for paper). For Topstep specifically, the journal believes nothing
  ever opened, so this is moot for that adapter — but a future
  hybrid scenario where the journal is updated from a Topstep
  realtime feed would still drift on manual operator action.
- **Why it matters**: The dashboard's "Open positions" widget and
  the risk engine's `max_open_positions` would mislead the operator
  about real exposure.
- **Reproducible by**: Code reading. No reconciliation code exists.
- **Recommendation**: Document the divergence in `docs/audit.md` as
  a known limitation, OR poll `broker.get_positions()` periodically
  and reconcile against the journal. The reconciliation is a Phase 2
  effort, not a quick fix.

#### M5 — No rate limit on `/webhooks/tradingview`
- **Location**: `app/main.py:2753-2770`.
- **What**: The webhook endpoint accepts unlimited requests per
  unit time. The shared secret guards admission, but a misconfigured
  TradingView alert template or a malicious party with the secret
  can overwhelm the broker integration. Each request hits the
  journal (writes), the risk engine (reads), and on accept, the
  broker (network POST). `docs/audit.md:88-89` acknowledges this is
  absent.
- **Why it matters**: A 100/s webhook storm would saturate the
  Topstep auth + order endpoints, eat the daily-loss limit
  immediately if signals are accepted, and inflate the journal.
- **Reproducible by**: Code reading. No middleware enforces any
  cap. Default uvicorn body size is 1 MiB, which is plenty of
  headroom for abuse with valid payloads.
- **Recommendation**: Add a per-process token-bucket rate limiter
  (no new deps required; `time.monotonic`-driven dict is fine for
  single-host single-operator). Cap to e.g. 5 webhooks/sec/IP.

#### M6 — Test-coverage gaps on the touchy paths above
- **Location**: `tests/` (no new tests added in this pass).
- **What**: The following code paths flagged above have no direct
  test coverage:
  - Duplicate cooldown under concurrent calls (H1).
  - Daily PnL behaviour across UTC boundary (H2).
  - `max_open_positions` enforcement on Topstep (H3).
  - `submit_market_order` retry on token expiry (H5; the current
    code has no retry to test).
  - `canTrade` enforcement (M1; nothing to test — currently
    unenforced).
  - `ENABLE_KILL_SWITCH=false` silently disabling everything
    (M2).
  - Bind-host vs auth-disabled startup refusal (M3; currently
    permissive).
  - Paper broker partial-fill modelling (paper assumes full-fill,
    no test covers a partial).
- **Why it matters**: Each of these is exactly the kind of regression
  that would slip a future change into the repo unnoticed.
- **Recommendation**: As a follow-up pass (not this one), add a
  small targeted test per item. None requires adapter changes
  beyond what the finding already proposes.

---

### LOW

#### L1 — Bare `except Exception` swallows a defensive error path
- **Location**: `app/main.py:1314-1315` (in
  `/api/topstep/live-execution/enable`), `app/execution/topstep.py:265-269`
  (`_store_token` persistence sink).
- **What**: Both catches log nothing useful (the live-arm one has
  `pragma: no cover` and the token-sink one logs the class name
  only). On any persistence failure during the live-arming write the
  exception is silently swallowed; the operator's UI shows success
  but the SQLite row may not be there.
- **Why it matters**: The next process restart would forget the
  arming, but the dashboard reported success. Confusing audit trail.
- **Reproducible by**: Inject a write-fail into the journal during
  the live-arm endpoint.
- **Recommendation**: Log at WARNING with the exception class name
  in both sites. Do not swallow silently.

#### L2 — `webhook_parser.detect_payload_type` returns generic for
  truncated-but-secret-bearing bodies
- **Location**: `app/webhook_parser.py:56-74`.
- **What**: A body with `secret` present but missing one of
  `symbol`/`action`/`contracts`/`price` is routed to the generic
  branch (line 67-71). The handler then surfaces a clear
  `missing_required_field` rejection. Intended behaviour, but worth
  noting: the dispatch cannot be tricked into Xiznit branch by
  setting a body `secret` *and* an `action` field — generic wins
  because `secret` is checked first (line 67).
- **Why it matters**: Confirms the dispatch can't be tricked across
  branches. Not a bug; documenting the static analysis result so the
  next auditor doesn't redo it.
- **Reproducible by**: Inspection.
- **Recommendation**: None — note for the record.

#### L3 — `flatten_position(symbol)` argument unused on paper outside
  the loop label
- **Location**: `app/execution/paper.py:170-214`.
- **What**: The `event` label is set to `paper_flatten_symbol` when
  `symbol` is passed, but the inner `flattened` list is empty when
  the symbol is already flat (line 180-182). The returned message
  says `"flattened 0 position(s)"` in that case, which is
  defensible. Edge case: passing a symbol that doesn't exist in
  `_positions` returns `"flattened 0 position(s)"` without any
  indication the symbol wasn't found. Operator might think the
  flatten succeeded when there was nothing to flatten.
- **Why it matters**: Minor UX confusion in paper-mode debugging.
- **Reproducible by**: `broker.flatten_position("DOES_NOT_EXIST")` —
  returns `ok=True` with empty flattened list.
- **Recommendation**: When `symbol` is non-None and not in
  `_positions`, return a clearer message ("no such symbol").

#### L4 — Default `MAX_DAILY_LOSS=250.0` is points, not dollars,
  unclear in env doc
- **Location**: `app/config.py:95` (default `250.0`);
  `app/risk_engine.py:181-186`.
- **What**: `MAX_DAILY_LOSS` is compared against
  `journal.get_daily_pnl()`, which sums
  `closed_trade.realized_pnl_points` (see `app/execution/paper.py:308-313`).
  The unit is futures **points**, not dollars. For MNQ at $5/point
  that's $1250/day; for MES at $5/point that's $1250/day; for ES at
  $50/point that's $12,500/day. The default value isn't a dollar
  figure but reads like one.
- **Why it matters**: Documentation/UI confusion, not a safety bug.
- **Reproducible by**: Inspection. The dashboard's Risk card shows
  "Max daily loss: 250.00 pts" — already qualifies the unit, good.
- **Recommendation**: Tighten `.env.example` to spell out "points
  (not dollars)".

---

## Test suite notes

- **532 passed, 0 failed, 0 skipped** in 50.52s (`pytest -q`).
- **Slowest tests**: not collected this pass. To enumerate, run
  `pytest --durations=10` — recommend doing so as a one-off and
  recording the top slow tests for future runs.
- **Skipped tests**: zero.
- **Areas with thin coverage** (overlap with M6): concurrent webhook
  duplicate, UTC-boundary daily-loss, Topstep position-count
  enforcement, token expiry mid-trade, kill-switch disabled flag,
  startup-refusal under risky config, paper partial-fill modelling.

## Out of scope / not investigated

- `app/execution/topstep_realtime.py` — the SignalR / polling realtime
  module. Touched only briefly via the route inventory; the realtime
  feed is documented as Phase 2.
- `app/execution/topstep_order_builder.py` — order-payload builder.
  Trusted because every order goes through the safety gates first.
- `app/execution/tradovate.py` — placeholder per docs.
- `app/dashboard.py` template-context computations — visual surface
  only, out of this audit's functional scope.
- `pdctl` / `sbctl` CLI scripts — operational shell wrappers, not in
  the trade hot path.
- Detailed perf / load characteristics. The journal is SQLite-backed
  with a single-process lock; load-testing is a separate effort.

## Confirmation of existing `docs/audit.md` claims

§ "Confirmed capabilities":

- ✓ TradingView webhook + `hmac.compare_digest` — `app/webhook.py:129`.
- ✓ Xiznit native alerts with envelope secret — `app/webhook.py:233-252`.
- ✓ Strategy-managed risk sizing capped by `MAX_CONTRACTS_PER_TRADE`
  — `app/risk_engine.py:169-173`.
- ✓ Timeframe lock — `app/risk_engine.py:154-164`.
- ✓ Symbol mapping via `config/symbols.json` —
  `app/webhook.py:149-153`.
- ✓ Paper broker simulated fills + flatten/reset —
  `app/execution/paper.py:30-264`.
- ✓ Topstep auth + JWT 23h TTL —
  `app/execution/topstep.py:310-407, 64`.
- ✓ Topstep account discovery — `app/execution/topstep.py:440-499`.
- ✓ Topstep demo/sim execution at `/api/Order/place` —
  `app/execution/topstep.py:1130-1296`.
- ✓ Topstep live execution gated behind the arm flow —
  `app/main.py:1121-1320`.
- ✓ Admin auth via PBKDF2-SHA256 — `app/auth.py:75-87, 90-112`.
- ✓ Plaintext fallback + auto-migrate to hash — `app/auth.py:139-144`
  and the login handler around `app/main.py:3606-3607`.
- ✓ Kill switch file-backed and checked by the risk engine —
  `app/kill_switch.py:19-23`, `app/risk_engine.py:144-146`.
- ? `pdctl`/`sbctl` start/stop/status — out of scope for this pass.

§ "Current safety gates" — demo execution:

- ✓ `BROKER_PROVIDER=topstep` enforced in
  `/api/topstep/live-execution/enable` (`app/main.py:1175-1186`); for
  ordinary trades the broker instance itself is the gate.
- ✓ `EXECUTION_MODE=demo` — `app/execution/topstep.py:1051-1052`.
- ✓ `ENABLE_TOPSTEP_ORDER_EXECUTION=true` —
  `app/execution/topstep.py:1049-1050`.
- ✓ `TOPSTEP_EXECUTION_CONFIRM=DEMO_ONLY` —
  `app/execution/topstep.py:1053-1054`.
- ✓ `ENABLE_LIVE_TRADING=false` enforced via
  `_demo_execution_safety_check` line 1047-1048
  (`enable_live_trading` true → `live_execution_locked`).
- ✗ "Numeric Topstep account selected and (when reported)
  `canTrade=true`" — only the numeric part is enforced
  (`topstep.py:1057-1058`); `canTrade` is **not** consulted. See M1.
- ✓ Kill switch off — checked via the risk engine for every signal
  (`app/risk_engine.py:144-146`).
- ✓ Risk engine accepts the signal — `app/risk_engine.py:141-209`.

§ "Current safety gates" — live execution:

- ✓ `BROKER_PROVIDER=topstep` — see above.
- ✓ `EXECUTION_MODE=live` set only via the arm endpoint
  (`app/main.py:3003-3009` refuses live in `/settings/broker`).
- ✓ `ENABLE_TOPSTEP_ORDER_EXECUTION=true` —
  `app/execution/topstep.py:1075-1076`.
- ✓ `TOPSTEP_EXECUTION_CONFIRM=LIVE_CONFIRMED` —
  `app/execution/topstep.py:1077-1078`.
- ✓ `ENABLE_LIVE_TRADING=true` — `app/execution/topstep.py:1073-1074`.
- ✓ `LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_ORDERS` —
  `app/execution/topstep.py:1079-1080`.
- ✓ `LIVE_TRADING_ACCOUNT_ACK=true` —
  `app/execution/topstep.py:1081-1082`.
- ✗ "Selected Topstep account exists and (when reported)
  `canTrade=true`" — partially: numeric account ID enforced
  (`topstep.py:1087-1088`), `canTrade` not. See M1.
- ✓ Kill switch off (when required) —
  `app/execution/topstep.py:1083-1084`.
- ✓ Signal symbol in `LIVE_ALLOWED_SYMBOLS` —
  `app/execution/topstep.py:1089-1094`.
- ✓ Signal contracts ≤ `LIVE_MAX_CONTRACTS_PER_TRADE` —
  `app/execution/topstep.py:1095-1098`.
- ✓ Signal contracts ≤ `MAX_CONTRACTS_PER_TRADE` —
  `app/execution/topstep.py:1100-1104`.
- ? "Valid Topstep contract mapping exists for the symbol" — gates
  the order build (`build_order_preview` calls
  `build_market_order_payload`, which depends on the symbol map),
  but the failure mode is a `topstep_dry_run_build_failed:...`
  envelope, not a labelled live-gate refusal. Behaviourally
  equivalent; nomenclature differs from the doc.
- ✓ Timeframe lock passes if enabled — `app/risk_engine.py:154-164`.

§ "Known limitations" — all match code:

- ✓ Tradovate scaffolded but not connected —
  `app/execution/tradovate.py:125` placeholder.
- ✓ Topstep order history via journal fallback — `app/main.py`
  metrics rendering path.
- ✓ Live skips daily-loss cross-check at submission — the risk
  engine enforces it pre-broker (`app/risk_engine.py:181-186`);
  `_live_execution_safety_check` does not re-check.
- ✓ Kill switch file-backed, not replicated.
- ✓ No rate limiter on the webhook — see M5.

§ "What remains risky" — accurate.

§ "How to verify DEMO/LIVE" instructions — accurate to the current
implementation. `sbctl` CLI not exercised in this pass.

§ "Emergency stop" — steps 1-4 work as documented for **stopping new
orders**. They do **not** close existing open Topstep positions —
see H4. The doc should add a fifth step or a footnote: "to close
existing Topstep positions, log into Topstep's own UI."

§ "Secret handling" — verified:

- Webhook secret stored in SQLite via `TRADINGVIEW_WEBHOOK_SECRET`,
  masked in summary views, regeneration does not log the value
  (`app/main.py:3508-3510`).
- Topstep API key masked to `…<last-4>` — `app/execution/topstep.py:75-82`.
- Topstep JWT persisted, masked in logs to presence flag —
  `app/execution/topstep.py:85-92`.
- Admin password PBKDF2-SHA256 with auto-migrate —
  `app/auth.py:75-87, 139-144`.
- Logs never contain raw secret material — confirmed by grep across
  `log.info/warning/error/debug` and `logger.*` calls (sub-agent
  result, Task 4): no occurrences leak `api_key`/`token`/`password`
  values, only masked previews or boolean presence.
