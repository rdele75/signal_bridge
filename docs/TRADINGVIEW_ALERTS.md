# TradingView Alert Setup

This doc shows how to wire TradingView alerts to SignalBridge. SignalBridge
accepts **two** alert body shapes today:

1. **Generic SignalBridge envelope** — historical schema where you write the
   alert message yourself, including the secret. Use this when you control
   the alert template (custom strategies, hand-rolled clients).
2. **Xiznit Universal ORB native** — the strategy controls the JSON body
   via `{{strategy.order.alert_message}}` and `{{strategy.alert_message}}`.
   For this shape the secret must be passed via the URL or a header instead.

## 1. Get a public URL

TradingView cannot send to `127.0.0.1`. Pick one of:

- **ngrok** — fast and free, ephemeral URL. See `app/tunnel/ngrok_notes.py`.
- **Cloudflare Tunnel** — stable URL on a domain you own. See `app/tunnel/cloudflare_notes.py`.
- **Tailscale Funnel** — what this deploy uses (`https://rtvm2.tail350a2.ts.net`).

Whichever you pick, the path is always:

```
POST /webhooks/tradingview
```

---

## 2. Xiznit Universal ORB (recommended for ORB users)

The Xiznit strategy emits two distinct messages. Create **two** TradingView
alerts, both pointing at the same SignalBridge webhook URL — do **not**
create a third "built-in notifications" alert; everything you need is
already in these two.

### Alert 1 — Entries & TP exits

- **Condition:** `Xiznit Universal ORB` → **Order fills**
- **Message:** `{{strategy.order.alert_message}}`
- **Webhook URL:**

```
https://rtvm2.tail350a2.ts.net/webhooks/tradingview?secret=YOUR_SECRET&symbol={{ticker}}
```

### Alert 2 — SL moves & force-closes

- **Condition:** `Xiznit Universal ORB` → **alert() function calls**
- **Message:** `{{strategy.alert_message}}`
- **Webhook URL:**

```
https://rtvm2.tail350a2.ts.net/webhooks/tradingview?secret=YOUR_SECRET&symbol={{ticker}}
```

### Why the secret goes in the URL

Xiznit owns the JSON body — its `alert_message` strings are formed inside
the strategy and you can't wedge an extra `"secret": "..."` into them.
SignalBridge therefore accepts the secret from any of:

- **Query string** (preferred): `?secret=YOUR_SECRET`
- **Header**: `X-SignalBridge-Secret: YOUR_SECRET`
- **Body** (legacy generic envelope only): `"secret": "YOUR_SECRET"`

Rejection rules:

- If none of the three carry a secret → `missing_secret`
- If a secret is present but doesn't match `TRADINGVIEW_WEBHOOK_SECRET` →
  `invalid_secret`

The secret is never logged or echoed back. Use a long random value —
the **`/tradingview`** page in the dashboard lets you regenerate one and
copy it from a read-only field. After regenerating, update **both**
TradingView alert webhook URLs (Alert 1 and Alert 2) so the
`?secret=…` query parameter matches the new value.

### Why `&symbol={{ticker}}` is helpful

Some Xiznit `alert()` messages omit the symbol — passing `{{ticker}}` via
the query string is the fallback so SignalBridge always knows which symbol
the alert refers to. When the body itself carries `symbol`/`ticker`, the
URL value is ignored.

### Xiznit fields SignalBridge understands

These are the keys the Xiznit native JSON commonly includes. Anything else
is preserved verbatim in the journal's `raw_payload` column.

| Xiznit field | Used as |
| --- | --- |
| `action` | `buy`/`sell` → ENTRY; `exit`/`close` → EXIT; `update_sl` → STOP_UPDATE |
| `qty` or `contracts` | order size (mapped to `contracts`) |
| `symbol` / `ticker` | symbol (URL `?symbol=` is the fallback) |
| `price` / `entry` / `fill_price` | fill price |
| `sl` / `stop` / `stop_loss` / `new_sl` | stop level |
| `tp1` / `tp2` / `tp3` | numeric take-profit levels |
| `tp` | numeric TP on entries; label `TP1`/`TP2`/`TP3` on exits |
| `reason` | `sl`, `eod_flatten`, `weekend_gap`, `max_duration`, `blackout` |
| `order_id` / `id` | dedup key (within `DUPLICATE_ORDER_COOLDOWN_SECONDS`) |
| `comment` | free-form note |

### Branching behavior

- **Entry** (`buy`/`sell`): required = secret + symbol + qty. Price is
  optional — without it the alert is journaled as an accepted dry-run
  (`xiznit_entry_dry_run_no_price`) so you can see what the strategy
  wanted to do without forcing a fill out of thin air.
- **TP exit** (`action=exit` with `tp=TP1/TP2/TP3`): required = qty. Missing
  qty → `missing_exit_qty`.
- **SL / force-close** (`action=exit` with `reason=sl/eod_flatten/...`):
  qty optional. If missing, `close_all=true` is recorded in
  `execution_result` and the alert is journaled as a close-all dry-run.
- **Stop update** (`action=update_sl`): never submits an order. Recorded
  as `decision=accepted` with `execution_result.event=stop_update_received`
  and the new stop level. No price/qty needed.

### Sample curl tests

After exporting your real secret:

```bash
export SB="YOUR_SECRET"
export URL="https://rtvm2.tail350a2.ts.net/webhooks/tradingview"
```

**Xiznit entry (query secret + query symbol fallback):**

```bash
curl -s -X POST "$URL?secret=$SB&symbol=MES1!" \
  -H 'Content-Type: application/json' \
  -d '{"action":"buy","qty":1,"entry":5000.25,"sl":4995.0,"tp1":5005.0,"tp2":5010.0,"order_id":"orb_long_1"}'
```

**Xiznit update_sl (informational, no order placed):**

```bash
curl -s -X POST "$URL?secret=$SB&symbol=MES1!" \
  -H 'Content-Type: application/json' \
  -d '{"action":"update_sl","symbol":"MES1!","sl":5002.5,"order_id":"orb_long_1"}'
```

---

## 3. Generic SignalBridge envelope (still supported)

When you own the alert message, the legacy shape is unchanged:

1. Open the chart and strategy/script you trade.
2. Click the **Alerts** panel → **Create Alert** (or press `Alt+A`).
3. **Condition** — pick your strategy/indicator and the condition that should fire.
4. **Notifications** tab → enable **Webhook URL** and paste your public URL.
5. **Message** — paste the JSON body below.

```json
{
  "secret": "PASTE_YOUR_LONG_RANDOM_SECRET_HERE",
  "source": "tradingview",
  "strategy": "orb_200ema_confluence",
  "symbol": "{{ticker}}",
  "exchange": "{{exchange}}",
  "action": "{{strategy.order.action}}",
  "contracts": "{{strategy.order.contracts}}",
  "price": "{{strategy.order.price}}",
  "position_size": "{{strategy.position_size}}",
  "market_position": "{{strategy.market_position}}",
  "order_id": "{{strategy.order.id}}",
  "comment": "{{strategy.order.comment}}",
  "bar_time": "{{time}}",
  "fire_time": "{{timenow}}"
}
```

Replace the `secret` value with the same value you put in `TRADINGVIEW_WEBHOOK_SECRET` in `.env`.

### Placeholder cheat sheet

| Placeholder | Substituted with |
| --- | --- |
| `{{ticker}}` | symbol of the chart, e.g. `MES1!` |
| `{{exchange}}` | exchange of the chart, e.g. `CME_MINI` |
| `{{strategy.order.action}}` | `buy` or `sell` (TradingView strategies emit these) |
| `{{strategy.order.contracts}}` | order size |
| `{{strategy.order.price}}` | fill price |
| `{{strategy.position_size}}` | net position size after this order |
| `{{strategy.market_position}}` | `long`, `short`, or `flat` |
| `{{strategy.order.id}}` | strategy-defined ID (e.g. `long_entry`) — used for dedup |
| `{{strategy.order.comment}}` | free-form comment from the strategy |
| `{{time}}` | bar open time (ISO 8601) |
| `{{timenow}}` | fire time (ISO 8601) |

---

## 4. Action normalization

TradingView strategies typically send `buy` or `sell`. SignalBridge also understands:

| Inbound | Normalized internally |
| --- | --- |
| `buy`, `long` | `BUY` |
| `sell` | `SELL` |
| `short` | `SHORT` |
| `cover` | `COVER` |
| `exit`, `close` | `EXIT` |
| `update_sl`, `move_sl`, `sl_update` | `UPDATE_SL` (Xiznit only, informational) |

Anything else is rejected with `unknown_action`.

---

## 5. Symbols

Only tickers in `ALLOWED_SYMBOLS` will be accepted for entry signals. Edit
`.env` (or the dashboard) if you want to add more.

```
ALLOWED_SYMBOLS=MES1!,MNQ1!
```

The ticker SignalBridge sees is exactly what `{{ticker}}` resolves to on the
TradingView chart — or the `?symbol=` query parameter when the Xiznit body
doesn't carry one.

---

## 6. Test the alert from TradingView

After saving each alert, TradingView's **"Send test"** button on the alert
will fire the webhook once. Watch `logs/signalbridge.log` and
`data/signalbridge.db` for the resulting row.

---

## 7. Timeframe field

When `ENABLE_TIMEFRAME_LOCK=true` (set on the
[Risk page](risk.md) — *off by default*), every alert must carry a
timeframe value that matches one of the entries in
`ALLOWED_TIMEFRAMES`. Otherwise the alert is rejected as
`missing_timeframe` (or `timeframe_not_allowed`).

**SignalBridge accepts three key names**, in priority order:

| Key | Why it exists | Example |
| --- | --- | --- |
| `timeframe` | Historical SignalBridge key. | `"timeframe": "5"` |
| `interval` | TradingView's native placeholder is `{{interval}}`. **Recommended.** | `"interval": "{{interval}}"` |
| `tf` | Common shorthand in hand-rolled templates. | `"tf": "5"` |

If multiple keys are present, the leftmost non-empty value wins.
The recommended spelling for new alerts is **`"interval": "{{interval}}"`**
— TradingView substitutes the placeholder automatically, and the
key name matches its own template variable.

If `ENABLE_TIMEFRAME_LOCK=false`, the timeframe field is optional and
SignalBridge does not enforce a value. Older alerts that predate the
lock continue to work unchanged.

---

## 8. Common rejection reasons

| Reason | Fix |
| --- | --- |
| `missing_secret` | No secret in body, URL, or header. Add `?secret=…` or `X-SignalBridge-Secret`. |
| `invalid_secret` | Secret present but doesn't match `TRADINGVIEW_WEBHOOK_SECRET`. |
| `missing_symbol` | Xiznit body had no `symbol`/`ticker` and no `?symbol=` query param. |
| `missing_or_invalid_qty` | Xiznit entry payload had no usable `qty`/`contracts`. |
| `missing_exit_qty` | Xiznit TP exit with no qty — SignalBridge will not guess. |
| `symbol_not_allowed: ...` | Add the ticker to `ALLOWED_SYMBOLS` and restart. |
| `contracts_above_max ...` | Raise `MAX_CONTRACTS_PER_TRADE` or lower order size. |
| `duplicate_order_id` | Strategy fired the same `order.id` inside the cooldown window. |
| `kill_switch_active` | Delete `data/kill_switch.active` to resume. |
| `longs_disabled` / `shorts_disabled` | Toggle the corresponding flag in `.env`. |
| `max_open_positions_reached ...` | Close existing positions or raise the cap. |
| `missing_or_invalid_price` | The paper broker needs a numeric price — fix the alert JSON. |
| `broker_not_implemented: topstep_...` | `BROKER_PROVIDER=topstep` selected, but the Topstep adapter is a placeholder — switch to `paper` until the adapter ships. |
| `malformed_payload: ...` | Non-numeric `contracts` or `price`, or otherwise invalid JSON shape. |
