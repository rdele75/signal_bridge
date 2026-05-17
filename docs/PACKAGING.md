# Packaging — Future Options

SignalBridge is currently a plain Python project that runs from source via `run.sh` / `run.bat`. That's the right shape while it's still small.

Once it stabilizes, there are several distribution options. None of them are implemented yet — this document records the trade-offs so the choice can be made deliberately later.

## Option A — Zip folder (lowest effort)

Ship the entire repo as a `.zip`. The user unzips and runs `run.bat` / `run.sh`.

**Pros**
- No build step.
- Easy to update — re-extract.
- The user still gets the full source for inspection.

**Cons**
- Requires Python preinstalled on the user's machine.
- First-run installs dependencies into `.venv/`, which means an internet connection.

**When to pick:** for technically-comfortable users (the initial audience for SignalBridge).

## Option B — PyInstaller `.exe` (Windows)

Bundle Python + dependencies + app into a single `signalbridge.exe`.

**Pros**
- Zero Python install required.
- Double-click to launch.

**Cons**
- Antivirus false positives are common.
- Each build is platform-specific (Win/Mac/Linux all need separate builds).
- The `.exe` is ~50 MB.
- Code-signing matters if you want users not to see SmartScreen warnings, which costs money.

**When to pick:** for non-technical users on Windows who shouldn't have to know what "pip" is.

Sketch:

```
pip install pyinstaller
pyinstaller --name signalbridge --onefile --console -p . app/main.py
```

The launcher target would actually be a small shim that calls `uvicorn` programmatically, since `app.main:app` is an ASGI factory not a CLI.

## Option C — Windows service

Wrap the uvicorn launch in a Windows service so it starts at boot and survives logout. NSSM is the usual route:

```
nssm install SignalBridge "C:\path\to\python.exe" "-m" "uvicorn" "app.main:app" "--host" "127.0.0.1" "--port" "8000"
nssm set SignalBridge AppDirectory "C:\path\to\signalbridge"
nssm start SignalBridge
```

**Pros**
- Survives reboots, runs without a logged-in user.

**Cons**
- Logs go to Windows Event Log unless redirected (NSSM can redirect to files).
- Updating the binary requires `nssm stop` first.

## Option D — Linux systemd service

A unit file at `/etc/systemd/system/signalbridge.service`:

```ini
[Unit]
Description=SignalBridge TradingView webhook bridge
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/signalbridge
ExecStart=/home/YOUR_USER/signalbridge/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```
sudo systemctl daemon-reload
sudo systemctl enable --now signalbridge
journalctl -u signalbridge -f
```

**Pros**
- Auto-restart on crash.
- Native log capture via `journalctl`.

**Cons**
- Requires root once for setup.
- Path/user must be edited by hand.

## Decision deferred

For now, ship from source. Revisit once SignalBridge has stable users and a stable feature set.
