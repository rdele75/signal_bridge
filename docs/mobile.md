# Mobile manual-verification checklist

SignalBridge's UI is primarily operated from a desktop, but the site
must remain usable on a phone for monitoring + emergency stops. This
file is the hand-test checklist used after any responsive change.

## Viewport widths to test

| Device-class             | CSS width |
| ------------------------ | --------- |
| Small Android            | 360 px    |
| iPhone 15 / 14 / 13      | 390 px    |
| iPhone 11 / XR           | 414 px    |
| iPhone 16 Pro Max        | 430 px    |
| Tablet portrait          | 768 px    |

Browser devtools → toggle device mode is enough; no need for real
devices for the layout pass.

## Pages to walk

Walk every page at each of the four phone widths above and at 768 px:

- `/`
- `/settings/broker`
- `/settings/risk`
- `/settings/symbols`
- `/tradingview`
- `/metrics`
- `/journal`
- `/settings/profile`

## Checklist

For each page:

- [ ] No horizontal page scroll. The whole `<html>` should not move
      sideways. Tables may scroll *inside their wrapper*; that's fine.
- [ ] Hamburger button (top-left) opens the drawer.
- [ ] Drawer overlay dims the page; tapping it closes the drawer.
- [ ] Drawer close button (×) closes the drawer.
- [ ] Escape key closes the drawer.
- [ ] Tapping any nav link closes the drawer and navigates.
- [ ] Configuration / Activity / System groups still collapse + expand.
- [ ] Selects, text inputs, password inputs all render dark (no white
      OS-default styling).
- [ ] Inputs focus without iOS zooming in (font-size on inputs is
      16 px under the mobile breakpoint).
- [ ] Buttons are at least ~44 px tall.
- [ ] Long URLs / secrets wrap or scroll inside their box — not the
      page.

### Dashboard (`/`)

- [ ] Execution card title + status sit on one line; status drops
      below the title only on narrow widths.
- [ ] Mode dropdown spans the card width.
- [ ] Apply button is full-width below the dropdown.
- [ ] Account line wraps cleanly if the account name is long.
- [ ] Disengage / Exit All / Smoke Test buttons stack and are
      thumb-tappable.
- [ ] Toggling the kill switch (header) reloads the page without
      horizontal scroll.
- [ ] Live warning modal fits the viewport — close button + Engage
      button both reachable without horizontal scroll.
- [ ] Typing the confirmation phrase doesn't cause iOS zoom.
- [ ] Live engaging animation runs on the card border without the
      page layout shifting.
- [ ] Toast notifications appear above the bottom safe-area, span the
      width, and don't cover critical buttons.
- [ ] Open orders block shows the mobile card list (not the wide
      table) on phones; symbols and IDs wrap.

### Broker (`/settings/broker`)

- [ ] Broker credentials grid collapses to one column.
- [ ] TopstepX API key input doesn't overflow the page.
- [ ] Selected account dropdown is full-width.
- [ ] Test connection / Test Topstep auth / Fetch accounts buttons
      stack and remain readable.
- [ ] Fetched account rows stack: label on top, "Use this account"
      button full-width below.
- [ ] Selected-account snapshot dl pairs stack to one column under
      480 px.

### Risk (`/settings/risk`)

- [ ] All four summary cards stack into one column.
- [ ] Strategy-managed checkbox + Fixed contracts input read well.
- [ ] Kill switch activate/deactivate buttons are full-width.
- [ ] Output `<pre>` (`#ks-out`) scrolls horizontally inside its box.

### Symbols (`/settings/symbols`)

- [ ] Mapping table scrolls horizontally inside its wrapper.
- [ ] Add mapping / Save mappings buttons stack.
- [ ] Contract search row stacks (input → live=true checkbox → Search
      button), all full-width.
- [ ] Search-result table scrolls inside its wrapper.

### TradingView (`/tradingview`)

- [ ] Current secret input + Copy button stack on phones; the input
      stays readable.
- [ ] Webhook URL/code blocks scroll horizontally without forcing the
      page to scroll.
- [ ] Xiznit two-alert setup blocks remain readable.
- [ ] Collapsible sections still expand on tap.
- [ ] Allowed-symbols chip list wraps.
- [ ] Field-reference table scrolls inside its wrapper.

### Metrics (`/metrics`)

- [ ] Top metric cards stack one per row on phones; two per row on
      768 px tablets.
- [ ] Profit graph SVG fits the card width without overflow.
- [ ] Past Orders card surfaces the mobile card-list view on phones —
      not the wide table.
- [ ] Refresh / Lookback controls stack.
- [ ] Empty state text is centred and readable.

### Journal (`/journal`)

- [ ] Recent signals table scrolls horizontally inside its wrapper.
- [ ] Recent closed trades table scrolls horizontally inside its
      wrapper.

### Profile (`/settings/profile`)

- [ ] Both summary cards stack into one column.
- [ ] Current password / new username / new password / confirm
      password inputs are full-width.
- [ ] New / confirm password share a grid on tablet but stack on
      phones.
- [ ] Save profile button is full-width on phones.

## Implementation pointers

- All responsive rules live in `app/static/styles.css` inside the
  `@media (max-width: 768px)` and `@media (max-width: 480px)`
  sections — there are no per-template hacks.
- Reusable helper classes the templates may add for responsiveness:
  - `mobile-stack`, `mobile-actions`, `mobile-full` — for button rows
    and stacked controls
  - `responsive-grid` — for auto-fit card grids
  - `table-scroll` — wraps wide tables so they scroll inside, not the
    page
  - `mobile-card-list` + `mobile-card-row` — for "row as labelled
    card" alternatives to wide tables
  - `copy-row` — input + button on desktop, stacked on mobile
  - `hide-on-mobile` / `show-on-mobile` — swap between desktop and
    mobile renders of the same data
- The drawer is wired in `app/templates/base.html`. The JS toggles
  `body.sidebar-open` and `.is-open` on the sidebar.
