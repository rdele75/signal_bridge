# SignalBridge Visual Audit — 2026-05-19

Static audit of all dashboard pages against the operator-facing mobile
checklist (`docs/mobile.md`) plus desktop / tablet alignment. No code
was changed by this pass. Findings are organised per page; each issue
carries a severity (`blocker` / `polish` / `nit`) so the operator can
triage which to fix in Phase 2–4.

Audit method: source review only — Jinja templates in `app/templates/`
and the single CSS file at `app/static/styles.css`. The author did not
load the running app in a browser, so any issue noted as "could happen"
should be confirmed visually before being fixed.

Audited viewport classes (per `docs/mobile.md`):
- Desktop: 1440px, 1280px
- Tablet:  1024px, 768px (sidebar shrinks to 200px at ≤1024px)
- Mobile:  414px, 390px, 360px (drawer mode kicks in at ≤768px)

Pages covered (in order): Login, Dashboard, Broker, Risk, Symbols,
TradingView, Metrics, Journal, Profile, Logs, System.

---

## A. Systemic issues (apply to every page)

### A.1 Reload-after-animation flash · `blocker`

Six client-side handlers POST an action and then call
`window.location.reload()` after the visible transition animation
finishes. The reload throws away the freshly-animated DOM and
re-renders the page from scratch — visually a hard white flash, then
the page rebuilds. The user sees the animation play to completion,
then a jarring reset.

Sites (file:line of the `.reload()` call):
1. `app/templates/base.html:149` — header kill-switch toggle. Affects
   **every** page.
2. `app/templates/dashboard.html:693` — apply-mode → paper, end of
   the dry-run-enter animation.
3. `app/templates/dashboard.html:698` — apply-mode → live success path.
4. `app/templates/dashboard.html:759` — disengage button, end of the
   `live-disengaging` fade.
5. `app/templates/dashboard.html:993` — **live engage success**.
   The 3.6s border-travel + 1.2s success-flash + 2.2s hold = ~7s of
   choreographed motion the operator just watched, then the reload
   wipes the card and re-renders. Most visible flash of all six.
6. `app/templates/settings_broker.html:426` — select-account.
7. `app/templates/settings_risk.html:229` — kill-switch activate /
   deactivate buttons.

### A.2 Inline `style="..."` attributes · `polish`

`grep -c 'style=' app/templates/*.html` totals **51** inline-style
attributes across the templates (the original master prompt called
out ~20; the real count is higher). Distribution:

| File                       | Count |
| -------------------------- | ----- |
| `tradingview.html`         | 16    |
| `settings_broker.html`     | 10    |
| `dashboard.html`           | 5     |
| `settings_risk.html`       | 5     |
| `settings_symbols.html`    | 5     |
| `metrics.html`             | 4     |
| `settings_profile.html`    | 3     |
| `system.html`              | 2     |
| `logs.html`                | 1     |
| `base.html` / `login.html` / `journal.html` | 0 |

All are cosmetic — `margin: 4px 0 0`, `margin-top: 8px`,
`margin-left: 6px`, `word-break: break-all`, `width: 80px`. None
encode genuinely one-off rules.

No `<style>` blocks inside templates — only attributes.

### A.3 Buttons lack `:active` state · `polish`

`.btn` defines `:hover` (line 366) and focus, but no `:active`. There
is no tactile press feedback. For a trading-control panel where buttons
arm orders, the lack of a "yes, I pressed it" affordance is felt.

### A.4 Cards mount with no enter animation on page load · `polish`

When any page first renders, all section cards appear simultaneously
without any fade or stagger. Pages with 6–8 cards (Dashboard, Broker,
Risk) feel like a screenful drops in at once.

### A.5 `<details>` collapsibles snap open / closed · `polish`

`details.collapsible > summary::after` rotates with a 0.15s
transition, but the **content** of `<details>` has no height
transition — it slams open. Operator-noted in the master prompt.

Native sidebar `nav-group` collapse is also abrupt, but the content
is short (3–4 nav links) so it's less jarring and the master prompt
explicitly excludes the sidebar from this fix.

Locations of `details.collapsible`:
- `settings_broker.html:151` — "Broker / execution selection"
- `tradingview.html:150, 182, 209` — three collapsibles

### A.6 Chevron affordance inconsistency · `nit`

Sidebar `nav-group` uses `›` (right-pointing chevron, rotates to
down) at `styles.css:716`. Collapsible cards use filled triangles
`▸` / `▾` at `styles.css:802`. Two different shapes for the same
"expand to see more" affordance. Either is fine — pick one.

### A.7 Mode-select has no pre-Apply preview · `polish`

On the Dashboard execution card, the operator picks a mode from the
`<select>` and clicks **Apply**. There is no visual cue between the
select change and the click telling the operator "this is what will
happen." For `live`, the modal pops up immediately on Apply; for
`paper`, the card animates straight into the dry-run transition.
Selecting `live` and pausing the mouse should give the operator a
hint that "Engage Live Execution" is the next gate.

Master prompt 3f already proposes the fix.

### A.8 No keyboard focus ring on modal-trigger / detail summaries · `nit`

`.btn:focus-visible` is not defined explicitly — only the global
`select:focus / input:focus` and `details.collapsible > summary:hover`
get treatment. Keyboard navigation across the page does not draw a
clear ring around active buttons or `<summary>` elements. The
`.mobile-drawer-toggle:focus-visible` rule is the only explicit one.

---

## B. Per-page findings

### B.1 Login (`/login`)

- **Desktop 1280 / 1440:** Card max-width 380px, centered. Fine.
- **Tablet 1024 / 768:** Same — login shell ignores the sidebar.
- **Mobile 414 / 390 / 360:** Login card has its own `@media (max-width:
  480px)` rule that drops padding to 22px 18px. On 360px viewport the
  card is ≈324px wide. Inputs use the global `.form` font-size which
  becomes 16px under mobile rules — no iOS zoom. Pass.
- **Animation:** zero motion. A subtle fade-in on the card (180ms)
  would feel intentional rather than abrupt. `polish`.
- **Severity:** mostly clean. The only nit is no first-paint fade.

### B.2 Dashboard (`/`)

- **Desktop 1440:** Execution card sits above a `grid-3` "Trading
  session / App status / Broker provider" row. The grid-3 partially
  duplicates state already shown in the Execution card (mode, broker
  provider) — visible **info duplication**. `polish`.
- **Desktop 1280:** Same as 1440 — wide enough to keep grid-3 on a row.
- **Execution card border width:** `.execution-card` uses
  `border-width: 1.5px` (line 850 of CSS) while every other `.card`
  uses 1px (default). On a screen with both, the Execution card
  visually stands ½ px proud of its neighbours. Intentional emphasis,
  but feels off in side-by-side layouts. `nit`.
- **Apply button + select on desktop:** the `.execution-mode-form` row
  is left-aligned and shrinks the select to `min-width: 120px`. The
  Apply button then sits flush right of a small dropdown — looks
  cramped versus the wide card. Could either grow the select or
  right-align the Apply. `polish`.
- **Tablet 1024:** Sidebar shrinks to 200px. Grid-3 collapses to grid-2
  via the `@media (max-width: 1100px)` rule. Looks balanced.
- **Tablet 768:** Mobile rules kick in; execution-card-head wraps,
  status text drops to full width. Fine.
- **Mobile 414 / 390 / 360:** All controls stack via `.mobile-actions`,
  `.execution-actions`. Open Orders has a mobile-card-list companion —
  good. Kill-switch indicator inside the actions row centers via
  mobile rule.
- **Live engage flow:** ~7s of choreographed motion ends in a hard
  reload (issue A.1 #5). `blocker`.
- **Apply, Disengage, kill-switch:** same reload problem. `blocker`.
- **Inline styles:** 5 (lines 349, 383, 425, 432, 449) — all
  `margin-top` / `margin-left` on small bits of meta. `polish`.
- **Recent signals / Last rejection cards (lines 414–454):** the
  badge + rejection-reason text uses an inline-styled
  `margin-left: 6px`. Should be a utility class.
- **Smoke-test confusion:** "Smoke Test" (safe dry-run, line 66) and
  "Execute smoke test…" (real BUY/SELL, line 70) are two adjacent
  buttons with near-identical labels. They never appear together
  (toggle on armed-state), but if a user reads the docs they may
  conflate them. Renaming the danger button to "Live smoke test
  (real order)" or similar would help. `polish`.

### B.3 Broker (`/settings/broker`)

- **Desktop 1280 / 1440:** Three top status cards (grid-3),
  Selected-account snapshot card with `dl.kv`, the big collapsible
  Broker form, Test-connection card, then a grid-2 of
  provider-credential summaries. Page is dense but readable.
- **Selected broker provider card:** value is just a single badge with
  no number or extra meta. Visually thin compared to neighbours.
  `nit`.
- **Collapsible summary on the form:** `details.collapsible > summary`
  wraps an `<h3>` plus a `<span class="hint">`. The hint can be long
  ("click to expand · broker provider + credentials · saved to SQLite")
  and the chevron sits after the hint. On 1024–768 the hint wraps to a
  second line, pushing the chevron down. `polish`.
- **Mid-form section break:** line 195 inserts a `<h3 style="margin-
  top: 18px;">Topstep / TopstepX (ProjectX) credentials</h3>` to break
  the form into two parts. Inline spacing — should be a class.
- **Mobile 414 / 390 / 360:** the grid-2 inside the form collapses to
  one column. The `.topstep-account-row` rendered by JS stacks (label
  on top, "Use this account" full-width). Pass per checklist.
- **Reload after select-account:** issue A.1 #6. `blocker`.
- **Inline styles:** 10. `polish`.
- **Animation:** the collapsible snap-opens — issue A.5. `polish`.

### B.4 Risk (`/settings/risk`)

- **Desktop 1440 / 1280:** Two grid-4 rows of summary cards (Sizing,
  Max contracts, Max positions, Max daily loss; Cooldown, Timeframe
  lock, Longs, Shorts). At 1100px and below they collapse to grid-2.
  Fine.
- **Edit risk limits form:** 5 numeric fields in a grid-2 means the
  last field (Duplicate cooldown) sits alone in the left column with
  empty space to its right. Adding a 6th field, or going grid-3, would
  balance it. `polish`.
- **Toggles row:** `.form-row.toggles` uses flex with 18px gap on
  desktop; on mobile it goes `flex-direction: column` (CSS line 1776),
  stacking the three checkboxes. Fine.
- **Allowed-timeframes input:** sits below grid-2, full width — looks
  fine.
- **Sizing mode badge:** "strategy-managed" is a long label inside a
  pill. On the narrowest grid-4 cell (1100→1280 transition window)
  the badge can clip or wrap awkwardly. `nit`.
- **Kill-switch buttons reload:** issue A.1 #7. `blocker`.
- **`<pre id="ks-out" class="output">` height-locked to 360px max:**
  Empty by default ("no action yet…") it consumes 60–70px. Fine.
- **Animation:** none beyond the global kill-switch toggle.

### B.5 Symbols (`/settings/symbols`)

- **Desktop 1280 / 1440:** Mappings table (5 columns) inside
  `.table-wrap.table-scroll`. Contract search row, results pane.
- **`<th style="width: 80px;">Actions`:** inline width. `polish`.
- **Add mapping / Remove:** clicking either fires
  `appendChild` / `row.remove()` instantly. No enter / exit animation
  on rows. Operator may not register that a new blank row appeared
  below if their eyes are on the button. `polish`.
- **Mappings table on mobile:** the master prompt and `docs/mobile.md`
  agree the table should scroll horizontally inside its wrapper — no
  mobile-card-list expected. Pass.
- **Contract search results table:** rendered as innerHTML inside
  `#contract-results`. No mobile-card-list — same expectation as
  mappings.
- **Copy button on contract row:** text swaps from "Copy" → "Copied"
  for 1500ms via JS. No animation, just text swap. Could fade or
  flash briefly. `nit`.
- **Inline styles:** 5.
- **Animation:** no mount or row-add motion. `polish`.

### B.6 TradingView (`/tradingview`)

- **Desktop 1280 / 1440:** Heaviest page by content count. Two summary
  cards, secret edit form, Xiznit two-alert block (4 `dl.kv` rows
  containing `<pre class="code">` blocks), three collapsible cards
  (Webhook URL forms, Generic alert JSON template, Local curl test),
  allowed-symbols chip list, field reference table.
- **Code blocks lack a Copy button:** every `.code-frame` shows a
  `copy-hint` label ("url", "message", "json", "bash") in the top-
  right corner but provides no clickable copy. The secret input has a
  Copy button (good); the surrounding URL / JSON blocks do not. For
  TradingView setup, the operator must hand-copy these into TV. A
  copy button on each `code-frame` would be a real ergonomic win.
  `polish`.
- **Xiznit two-alert block:** Alert 1 and Alert 2 each show the same
  `xiznit_url_tunnel` value in two separate `<pre class="code">`
  blocks. Duplication is intentional (operator pastes once per alert)
  but visually heavy. `nit`.
- **Three abrupt `<details>` opens:** issue A.5. `polish`.
- **Inline styles:** 16 (worst offender). `polish`.
- **Mobile 414 / 390 / 360:** `.copy-row` stacks (input above button)
  via mobile rule. `.code-frame` `<pre class="code">` scrolls
  horizontally inside the box via the mobile pre.code rule (overflow-x:
  auto). Pass.
- **Allowed-symbols chip list:** wraps via `.chips` flex-wrap. Pass.
- **Field reference table:** 3 columns, wrapped in `table-scroll`. Pass.

### B.7 Metrics (`/metrics`)

- **Desktop 1280 / 1440:** Two rows of grid-3 metric cards, profit
  graph SVG card, Past Orders card (with Topstep dynamic refresh on
  topstep provider), Rejection reasons + Trades by symbol grid-2.
- **Profit graph SVG:** uses `preserveAspectRatio="none"` (line 53).
  This stretches the polyline to fit the viewBox, so when card width
  changes the slope visually changes — the same data looks steeper or
  flatter depending on the card. For an actual P&L curve this is
  misleading. `polish`.
- **Past Orders refresh control:** inline styles on the label
  (`margin-right: 6px`) and status span (`margin-left: 10px`). The
  refresh button + lookback select + status text live together in a
  `btn-row mobile-actions`. Looks fine on desktop, stacks on mobile.
- **Refreshed table:** the JS replaces `#past-orders-body` innerHTML on
  refresh. No enter animation on the new rows — the table just swaps.
  `nit`.
- **Mobile 414 / 390 / 360:** Cards stack one per row, two per row on
  768px tablets via `@media (max-width: 1100px)`. Wait — actually on
  768px, `.grid-3 { grid-template-columns: 1fr; }` applies (mobile
  breakpoint), so they go single-column. The `docs/mobile.md`
  checklist says "two per row on 768px tablets" — this expectation may
  no longer match the CSS. `polish` (decide which is correct).
- **Past Orders mobile card list:** present (lines 146–166, 212–232).
  Pass.
- **Inline styles:** 4.

### B.8 Journal (`/journal`)

- **Desktop 1280 / 1440:** Two wide data tables. Recent signals has 9
  columns; Recent closed trades has 8. Both wrapped in `.table-wrap
  .table-scroll`. Pass.
- **Mobile 414 / 390 / 360:** **No mobile-card-list companion on either
  table.** Operator must side-scroll the 9-column table to see
  Rejection reason / Order ID / Broker. Fails `docs/mobile.md` checklist
  for the journal page (the checklist only says "scrolls horizontally
  inside its wrapper," but `metrics.html` already uses the
  hide-on-mobile / show-on-mobile pattern, so journal should match).
  Master prompt 4a is the planned fix. `blocker`.
- **No row-decision affordance in mobile-card-list when added:** when
  the mobile cards arrive, the Decision cell should remain a badge
  (`badge-good` / `badge-bad`) and the rejected-row highlight from
  `tr.row-rejected` is not portable to mobile-card-row. Use the badge
  as the only signal.
- **Animation:** none. Cards / tables mount at once.

### B.9 Profile (`/settings/profile`)

- **Desktop 1280 / 1440:** Two summary cards (grid-2), credentials
  edit form, tip banner. Modest page.
- **Tablet 1024 / 768:** grid-2 collapses at 1100, then mobile rules.
  Fine.
- **Mobile 414 / 390 / 360:** All inputs hit the iOS-safe 16px font.
  New/confirm password grid stacks. Save button full-width. Pass.
- **Inline styles:** 3 — all `margin: 4px 0 0` on muted hints.
  `polish`.
- **Animation:** none.

### B.10 Logs (`/logs`)

- **Desktop 1280 / 1440:** grid-3 status cards (Log file path, Lines
  shown, Stream). Terminal-style log viewer below with three traffic-
  light dots and the log path as a right-aligned label.
- **`style="word-break: break-all;"` on log path card:** line 11 —
  inline. Should be a `mono` utility (or `.break-all`) class.
- **Terminal max-height 640px with `overflow: auto`:** good.
- **Per-line color:** error lines red, warn amber, info blue — good.
- **No manual refresh button:** the "Stream" card sub says "reload page
  to refresh," requiring F5. A Refresh button (and ideally a small
  auto-tail timer) would be more useful. `polish` (out of scope for
  visual pass — note as future work).
- **Mobile:** terminal body wraps via `word-break: break-word` mobile
  rule. Lines stay inside.
- **Animation:** none.

### B.11 System (`/system`)

- **Desktop 1280 / 1440:** grid-4 status cards (App, Runtime, Mode,
  Provider), grid-2 process / storage `dl.kv` blocks, single webhook
  card, "Useful local URLs" table.
- **Useful URLs table wrapper:** wrapped in `.table-wrap` only — **no
  `.table-scroll`**. On a viewport between desktop and mobile (e.g.
  narrow window resize), the table will not scroll inside its
  wrapper; with mobile rules applied, `.table-wrap` does get
  `overflow-x: auto` via line 1745. So actually on phones it's
  okay — but the inconsistency with every other table on the site
  warrants standardising. Master prompt 4b plans this fix. `blocker`
  by convention.
- **Inline `margin-left: 6px` on .env badge:** two occurrences (lines
  66, 68). `polish`.
- **Mobile:** all `dl.kv` blocks switch to a 100px label column
  (line 1731). Long paths wrap thanks to `dl.kv dd { word-break: break-
  all }`. Pass.
- **Animation:** none.

---

## C. Recommended phasing (cross-reference to master prompt)

| Phase | Issues to fix                                                       |
| ----- | ------------------------------------------------------------------- |
| 2     | A.1 (all six reload sites)                                          |
| 3a    | (master prompt) — cross-fade on mode swap                           |
| 3b    | A.5 — smooth `<details>` collapse                                   |
| 3c    | toast polish (master prompt — note exit transition already exists, see below) |
| 3d    | A.4 — first-paint card stagger                                      |
| 3e    | A.3 — button :active state                                          |
| 3f    | A.7 — mode-select pre-Apply preview chip                            |
| 4a    | B.8 — Journal mobile-card-list                                      |
| 4b    | B.11 — System Useful URLs `.table-scroll`                           |
| 4c    | A.2 — inline-style cleanup (utility classes)                        |
| 4d    | Operator-triaged polish items below                                 |

### Notes

- **Toast exit (3c):** master prompt says toast exits via
  `display:none` (harsh). Reviewing the actual code, `showToast` in
  `dashboard.html` already toggles
  `execution-toast-exit` for 360ms before setting `hidden = true`,
  and the CSS class has a `transition: opacity 0.32s ease, transform
  0.32s ease`. So the exit fade is already implemented. Either the
  master prompt is referencing an older state, or there's a subtle
  reason the fade isn't perceptible (e.g. enter / exit fights). On
  visual review, recommend keeping the existing transition and
  verifying it actually plays in the browser before adding more.

- **Card mount animations (3d):** the master prompt is clear that
  this should only fire on first load, not after reload-flash fix
  lands. Worth wiring the `data-first-paint` flag now so future
  in-place refreshes don't re-trigger the entry animation.

- **Audit doc lifespan:** this audit reflects the source tree as of
  `2308a4d` (commit at the start of this branch). Re-walk before
  Phase 4 to catch anything Phase 2 / 3 introduced.

## D. Severity counts

| Severity  | Count |
| --------- | ----- |
| blocker   | 9 (A.1 ×7 sites counted as one issue + B.8 + B.11) |
| polish    | ~20   |
| nit       | ~6    |

---

## E. Out of scope (note-only, no fix planned in this pass)

These were noticed but explicitly outside the visual-pass scope per
the master prompt's "OUT OF SCOPE" section:

- Logs page has no auto-tail or refresh button — operator must F5.
- Metrics page docs/mobile.md says "two per row on 768px tablets"
  but the CSS forces single column at 768px. Decision needed (doc or
  CSS).
- Dashboard duplicates execution-mode + broker-provider info between
  the Execution card and the trailing grid-3 row. Pure
  information-architecture call, not a visual bug.
- Chevron shape inconsistency (A.6) — purely aesthetic.
