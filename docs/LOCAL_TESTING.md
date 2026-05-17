# Local Testing — curl recipes

SignalBridge is a private local dashboard + webhook bridge:

```
TradingView → /webhooks/tradingview → risk engine → broker adapter
                                                    → journal / metrics
```

This page covers the webhook side. The dashboard itself is at
`http://127.0.0.1:8000/` — open it in a browser to view live status,
journal, metrics, and logs.

All examples below assume:

- SignalBridge is running on `http://127.0.0.1:8000`.
- `TRADINGVIEW_WEBHOOK_SECRET=change_me_to_a_long_random_secret` (the default).
- `ALLOWED_SYMBOLS=MES1!,MNQ1!`.
- `MAX_CONTRACTS_PER_TRADE=1`.
- `ENABLE_SHORTS=true` initially.

Adjust the secret / symbols if you've changed `.env`.

## Dashboard pages

```text
GET /                Dashboard
GET /settings/broker Broker settings page
GET /settings/risk   Risk settings page
GET /tradingview     TradingView setup page
GET /journal         Trade journal page
GET /metrics         Metrics page
GET /logs            Recent logs page
```

## REST API

```bash
curl http://127.0.0.1:8000/api/status
curl http://127.0.0.1:8000/api/metrics
curl http://127.0.0.1:8000/api/journal/recent?limit=20
curl http://127.0.0.1:8000/api/positions

# Kill switch
curl -X POST http://127.0.0.1:8000/api/kill-switch/enable
curl -X POST http://127.0.0.1:8000/api/kill-switch/disable

# Broker readiness probe — paper returns 200/ok, others return 501/not_implemented
curl -X POST http://127.0.0.1:8000/api/broker/test-connection
```

> **macOS / Linux:** the commands below work as-is.
> **Windows cmd.exe:** replace single quotes with double quotes and escape inner quotes, or use PowerShell with `Invoke-RestMethod`.

> **Numeric fields:** `contracts`, `price`, and `position_size` accept either
> quoted TradingView-style strings (`"contracts": "1"`, `"price": "5000.25"`)
> **or** raw JSON numbers (`"contracts": 1`, `"price": 5000.25`). Use whichever
> is convenient — both shapes round-trip to the same internal signal.
> Non-numeric strings like `"abc"` or `"not-a-price"` are still rejected
> with `rejection_reason: "malformed_payload: ..."`.

---

## Health & status

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/status
```

---

## 1. Valid alert — **quoted** numeric values (classic TradingView shape)

```bash
curl -X POST http://127.0.0.1:8000/webhooks/tradingview \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "change_me_to_a_long_random_secret",
    "source": "tradingview",
    "strategy": "orb_200ema_confluence",
    "symbol": "MES1!",
    "exchange": "CME_MINI",
    "action": "buy",
    "contracts": "1",
    "price": "5000.25",
    "position_size": "1",
    "market_position": "long",
    "order_id": "test_long_001",
    "comment": "valid test",
    "bar_time": "2026-05-17T13:30:00Z",
    "fire_time": "2026-05-17T13:30:01Z"
  }'
```

Expected response: `{"accepted":true,"decision":"accepted",...}`.

---

## 1b. Valid alert — **unquoted** numeric values (hand-rolled client)

```bash
curl -X POST http://127.0.0.1:8000/webhooks/tradingview \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "change_me_to_a_long_random_secret",
    "source": "tradingview",
    "strategy": "orb_200ema_confluence",
    "symbol": "MES1!",
    "exchange": "CME_MINI",
    "action": "buy",
    "contracts": 1,
    "price": 5000.25,
    "position_size": 1,
    "market_position": "long",
    "order_id": "test_long_002"
  }'
```

Expected response: `{"accepted":true,"decision":"accepted",...}` — same as above.

---

## 2. Bad secret (should be REJECTED)

```bash
curl -X POST http://127.0.0.1:8000/webhooks/tradingview \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "wrong_secret",
    "symbol": "MES1!",
    "action": "buy",
    "contracts": "1",
    "price": "5000.25",
    "order_id": "test_badsecret_001"
  }'
```

Expected: `{"accepted":false,"decision":"rejected","rejection_reason":"invalid_secret"}`.

---

## 3. Unknown symbol (should be REJECTED)

```bash
curl -X POST http://127.0.0.1:8000/webhooks/tradingview \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "change_me_to_a_long_random_secret",
    "symbol": "AAPL",
    "action": "buy",
    "contracts": "1",
    "price": "200.00",
    "order_id": "test_badsym_001"
  }'
```

Expected: `"rejection_reason":"symbol_not_allowed: AAPL"`.

---

## 4. Too many contracts (should be REJECTED)

```bash
curl -X POST http://127.0.0.1:8000/webhooks/tradingview \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "change_me_to_a_long_random_secret",
    "symbol": "MES1!",
    "action": "buy",
    "contracts": "99",
    "price": "5000.25",
    "order_id": "test_bigsize_001"
  }'
```

Expected: `"rejection_reason":"contracts_above_max (99 > 1)"`.

---

## 5. Disabled short (set `ENABLE_SHORTS=false` and restart first)

```bash
curl -X POST http://127.0.0.1:8000/webhooks/tradingview \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "change_me_to_a_long_random_secret",
    "symbol": "MES1!",
    "action": "sell",
    "contracts": "1",
    "price": "5000.25",
    "order_id": "test_noshort_001"
  }'
```

Expected: `"rejection_reason":"shorts_disabled"`.

---

## 5b. Invalid price (non-numeric — should be REJECTED)

```bash
curl -X POST http://127.0.0.1:8000/webhooks/tradingview \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "change_me_to_a_long_random_secret",
    "symbol": "MES1!",
    "action": "buy",
    "contracts": "1",
    "price": "not-a-price",
    "order_id": "test_bad_price_001"
  }'
```

Expected: `"rejection_reason":"malformed_payload: ..."`.

---

## 6. Duplicate `order_id` (run twice within `DUPLICATE_ORDER_COOLDOWN_SECONDS`)

```bash
# First call — accepted
curl -X POST http://127.0.0.1:8000/webhooks/tradingview \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "change_me_to_a_long_random_secret",
    "symbol": "MES1!",
    "action": "buy",
    "contracts": "1",
    "price": "5000.25",
    "order_id": "dup_001"
  }'

# Second call with same order_id — rejected
curl -X POST http://127.0.0.1:8000/webhooks/tradingview \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "change_me_to_a_long_random_secret",
    "symbol": "MES1!",
    "action": "buy",
    "contracts": "1",
    "price": "5000.50",
    "order_id": "dup_001"
  }'
```

Expected second response: `"rejection_reason":"duplicate_order_id"`.

---

## Daily loss limit (placeholder note)

The `daily_loss_limit_reached` risk check is wired up and tested, but the
**paper broker does not auto-accrue realized PnL yet**. So in practice the
limit only fires if something explicitly calls `journal.add_daily_pnl()`.
This is a known partial implementation, not a silent feature claim.

---

## Inspecting the journal

```bash
sqlite3 data/signalbridge.db \
  "select id, received_at, broker_provider, symbol, broker_symbol, action, \
          decision, rejection_reason \
   from signals order by id desc limit 20;"
```

## Tailing logs

```bash
tail -f logs/signalbridge.log
```
