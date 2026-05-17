# TradingView Alert Setup

This doc shows how to wire a TradingView alert to SignalBridge.

## 1. Get a public URL

TradingView cannot send to `127.0.0.1`. Pick one of:

- **ngrok** — fast and free, ephemeral URL. See `app/tunnel/ngrok_notes.py`.
- **Cloudflare Tunnel** — stable URL on a domain you own. See `app/tunnel/cloudflare_notes.py`.

Whichever you pick, your final webhook URL will look like:

```
https://<your-tunnel-host>/webhooks/tradingview
```

## 2. Create the alert in TradingView

1. Open the chart and strategy/script you trade.
2. Click the **Alerts** panel → **Create Alert** (or press `Alt+A`).
3. **Condition** — pick your strategy/indicator and the condition that should fire.
4. **Notifications** tab → enable **Webhook URL** and paste your public URL.
5. **Message** — paste the JSON body below.

## 3. Alert JSON body

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

## 4. Placeholder cheat sheet

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

## 5. Action normalization

TradingView strategies typically send `buy` or `sell`. SignalBridge also understands:

| Inbound | Normalized internally |
| --- | --- |
| `buy`, `long` | `BUY` |
| `sell` | `SELL` |
| `short` | `SHORT` |
| `cover` | `COVER` |
| `exit`, `close` | `EXIT` |

Anything else is rejected with `unknown_action`.

## 6. Symbols

Only tickers in `ALLOWED_SYMBOLS` will be accepted. Edit `.env` if you want to add more.

```
ALLOWED_SYMBOLS=MES1!,MNQ1!
```

The ticker SignalBridge sees is exactly what `{{ticker}}` resolves to on the TradingView chart.

## 7. Test the alert from TradingView

After saving the alert, TradingView's **"Send test"** button on the alert will fire the webhook once. Watch `logs/signalbridge.log` and `data/signalbridge.db` for the resulting row.

## 8. Common rejection reasons

| Reason | Fix |
| --- | --- |
| `invalid_secret` | Make sure the `secret` in the alert body matches `.env`. |
| `symbol_not_allowed: ...` | Add the ticker to `ALLOWED_SYMBOLS` and restart. |
| `contracts_above_max ...` | Raise `MAX_CONTRACTS_PER_TRADE` or lower order size. |
| `duplicate_order_id` | Strategy fired the same `order.id` inside the cooldown window. |
| `kill_switch_active` | Delete `data/kill_switch.active` to resume. |
| `longs_disabled` / `shorts_disabled` | Toggle the corresponding flag in `.env`. |
| `max_open_positions_reached ...` | Close existing positions or raise the cap. |
| `missing_or_invalid_price` | The paper broker needs a numeric price — fix the alert JSON. |
| `broker_not_implemented: topstep_...` | `BROKER_PROVIDER=topstep` selected, but the Topstep adapter is a placeholder — switch to `paper` until the adapter ships. |
| `broker_not_implemented: tradovate_...` | Same as above for `tradovate`. |
| `malformed_payload: ...` | Non-numeric `contracts` or `price`, or otherwise invalid JSON shape. |
