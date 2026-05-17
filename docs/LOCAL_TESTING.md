# Local Testing — curl recipes

All examples assume:

- SignalBridge is running on `http://127.0.0.1:8000`.
- `TRADINGVIEW_WEBHOOK_SECRET=change_me_to_a_long_random_secret` (the default).
- `ALLOWED_SYMBOLS=MES1!,MNQ1!`.
- `MAX_CONTRACTS_PER_TRADE=1`.
- `ENABLE_SHORTS=true` initially.

Adjust the secret / symbols if you've changed `.env`.

> **macOS / Linux:** the commands below work as-is.
> **Windows cmd.exe:** replace single quotes with double quotes and escape inner quotes, or use PowerShell with `Invoke-RestMethod`.

---

## Health & status

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/status
```

---

## 1. Valid alert (should be ACCEPTED)

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

## Inspecting the journal

```bash
sqlite3 data/signalbridge.db \
  "select id, received_at, symbol, action, decision, rejection_reason \
   from signals order by id desc limit 20;"
```

## Tailing logs

```bash
tail -f logs/signalbridge.log
```
