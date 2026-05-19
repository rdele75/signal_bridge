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

## 8. `sbctl` — control script

`scripts/sbctl` wraps `uvicorn` so you don't retype the full command.
It runs SignalBridge on `127.0.0.1:8000` by default (override with
`APP_HOST` / `APP_PORT` in `.env`). Tailscale Funnel — or any other
public proxy — should forward to `127.0.0.1:8000`.

### Install

```bash
chmod +x scripts/sbctl scripts/install-sbctl.sh
./scripts/install-sbctl.sh             # symlinks ~/.local/bin/sbctl
./scripts/install-sbctl.sh --with-pdctl   # optional: also link pdctl
```

The installer skips with an error if `~/.local/bin/sbctl` already
exists as a real file (so a hand-written script in `PATH` is never
overwritten). Make sure `~/.local/bin` is on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Usage

```bash
sbctl start        # background uvicorn, pid in runtime/signalbridge.pid
sbctl stop         # SIGTERM the recorded pid (falls back to port owner)
sbctl restart      # stop then start
sbctl status       # pid / port owner / /health response
sbctl logs         # tail -F logs/server.out + logs/signalbridge.log
sbctl health       # curl http://127.0.0.1:8000/health
```

Use `sbctl restart` after any settings change that needs a restart
(e.g. switching `BROKER_PROVIDER`) or after pulling code changes.

State files:

- `runtime/signalbridge.pid` — recorded pid of the running uvicorn
- `logs/server.out` — uvicorn stdout/stderr capture
- `logs/signalbridge.log` — rotated application log (existing file)

Both `runtime/*.pid` and `logs/server.out` are gitignored.

## 9. Running in the background (alternative)

If you'd rather not use `sbctl`:

```bash
nohup ./run.sh > logs/run.out 2>&1 &
```

For a long-lived deployment, a systemd unit is the right answer. That is documented in `docs/PACKAGING.md` but not implemented in this build.
