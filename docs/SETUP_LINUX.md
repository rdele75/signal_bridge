# SignalBridge — Linux / macOS Setup

## 1. Prerequisites

- Python 3.10 or newer.
- `python3-venv` (Debian/Ubuntu: `sudo apt install python3-venv`).
- `curl` for local testing.

Verify Python:

```bash
python3 --version
```

## 2. Download SignalBridge

```bash
cd ~/projects
# extract or clone signalbridge here
cd signalbridge
```

## 3. Configure

```bash
cp .env.example .env
$EDITOR .env
```

At minimum, set a long random `TRADINGVIEW_WEBHOOK_SECRET`. Keep the defaults (`EXECUTION_MODE=paper`, `BROKER=paper`) for your first run.

## 4. First run

```bash
chmod +x run.sh
./run.sh
```

`run.sh` will:

1. Create `.venv/` if missing.
2. Install dependencies from `requirements.txt`.
3. Launch `uvicorn app.main:app` on the host/port from `.env`.

In another terminal:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/status
```

## 5. Expose to TradingView

- **ngrok** — see `app/tunnel/ngrok_notes.py`.
- **Cloudflare Tunnel** — see `app/tunnel/cloudflare_notes.py`.

## 6. Troubleshooting

- **`python3-venv` missing** — `sudo apt install python3-venv` (or your distro's equivalent).
- **Port already in use** — change `APP_PORT` in `.env` or stop the other process.
- **Permission denied on `run.sh`** — `chmod +x run.sh`.
- **macOS Gatekeeper blocks `cloudflared`** — `xattr -d com.apple.quarantine /path/to/cloudflared`.

## 7. Stopping

Press `Ctrl+C` in the run terminal.

## 8. Running in the background

For a quick background run during testing:

```bash
nohup ./run.sh > logs/run.out 2>&1 &
```

For a long-lived deployment, a systemd unit is the right answer. That is documented in `docs/PACKAGING.md` but not implemented in this build.
