# SignalBridge

**SignalBridge** is a single-user TradingView webhook execution gateway.

It is **not** a centralized platform, **not** SaaS, and **not** multi-user. You download your own copy, supply your own credentials, point your own TradingView alert at it, and it trades only your account.

The default mode is **paper trading**. Live trading is intentionally disabled in this build — the live broker adapter is a placeholder that raises `NotImplementedError`.

---

## What it does

1. Exposes a FastAPI HTTP endpoint at `POST /webhooks/tradingview`.
2. Validates the alert against a shared secret you control.
3. Normalizes TradingView's action values (`buy`, `sell`, `long`, `short`, `exit`, `close`) into a small internal vocabulary (`BUY`, `SELL`, `SHORT`, `COVER`, `EXIT`).
4. Runs a risk engine (symbol allow-list, contract caps, kill switch, daily loss limit, duplicate-order cooldown, long/short toggles, max open positions).
5. Routes the signal to a broker adapter:
   - `paper` — simulates fills locally and tracks open positions.
   - `tradovate_demo` — placeholder, not yet implemented.
   - `tradovate_live` — disabled placeholder.
6. Records every signal and decision into SQLite at `data/signalbridge.db`.
7. Writes a rotating log file at `logs/signalbridge.log`.

---

## Quick start

```bash
git clone <your fork or local copy> signalbridge
cd signalbridge
cp .env.example .env
# edit .env — at minimum set a long random TRADINGVIEW_WEBHOOK_SECRET

# Linux / macOS
./run.sh

# Windows
run.bat
```

The server starts on `http://127.0.0.1:8000` by default.

Check it:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/status
```

---

## How it works

TradingView fires an alert with a JSON body. SignalBridge:

1. Receives the request at `/webhooks/tradingview`.
2. Compares the `secret` field against `TRADINGVIEW_WEBHOOK_SECRET`.
3. Parses and normalizes the payload.
4. Runs every risk check in `app/risk_engine.py`.
5. If accepted, calls the configured broker adapter from `app/execution/`.
6. Writes a row to the `signals` table with the full decision and result.
7. Returns a JSON response describing what happened.

A rejection at any stage returns `{"accepted": false, "decision": "rejected", "rejection_reason": "..."}` and is still journaled.

---

## Configuration

All config lives in `.env`. See `.env.example` for the full list.

| Variable | Purpose |
| --- | --- |
| `APP_HOST`, `APP_PORT` | bind address |
| `EXECUTION_MODE` | `paper` (default) / `demo` / `live` |
| `BROKER` | `paper` / `tradovate_demo` / `tradovate_live` |
| `TRADINGVIEW_WEBHOOK_SECRET` | shared secret in alert body |
| `ALLOWED_SYMBOLS` | comma-separated allow-list |
| `MAX_CONTRACTS_PER_TRADE` | hard cap per signal |
| `MAX_DAILY_LOSS` | absolute USD floor on daily realized PnL |
| `MAX_OPEN_POSITIONS` | concurrent open positions cap |
| `ENABLE_LONGS`, `ENABLE_SHORTS` | direction toggles |
| `ENABLE_KILL_SWITCH` | turn the kill switch feature on/off |
| `DATABASE_PATH`, `LOG_PATH` | storage paths |

---

## TradingView setup

See [`docs/TRADINGVIEW_ALERTS.md`](docs/TRADINGVIEW_ALERTS.md) for the alert JSON body template and how to wire it up.

The minimum body shape is:

```json
{
  "secret": "<your secret>",
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

To make `127.0.0.1` reachable from TradingView, expose it with **ngrok** or **Cloudflare Tunnel** — see `app/tunnel/ngrok_notes.py` and `app/tunnel/cloudflare_notes.py`.

---

## Local testing

See [`docs/LOCAL_TESTING.md`](docs/LOCAL_TESTING.md) for ready-to-paste `curl` commands covering valid alerts, bad secrets, unknown symbols, too many contracts, disabled shorts, and duplicate `order_id`.

Run the test suite:

```bash
pip install -r requirements.txt
pytest
```

---

## Logs and database

- Logs: `logs/signalbridge.log` (rotating, 5 MB × 3 backups).
- Database: `data/signalbridge.db` (SQLite). Tables: `signals`, `positions`, `daily_pnl`.

You can inspect the journal with any SQLite browser:

```bash
sqlite3 data/signalbridge.db "select id, received_at, symbol, action, decision, rejection_reason from signals order by id desc limit 20;"
```

---

## Future packaging

Packaging is intentionally not implemented yet. See [`docs/PACKAGING.md`](docs/PACKAGING.md) for the options under consideration (zip folder, PyInstaller `.exe`, Windows service, Linux systemd).

---

## Safety notes

- Live execution is disabled in this build.
- Paper mode is the default and cannot place real orders.
- The kill switch is on by default. Create the sentinel file `data/kill_switch.active` to halt all execution immediately. Delete the file to resume.
- The webhook secret is the only authentication. Use a long random string and never commit it.
