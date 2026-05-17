# SignalBridge — Windows Setup

## 1. Prerequisites

- Windows 10 or 11.
- Python 3.10 or newer from <https://www.python.org/downloads/windows/>.
  - During install, check **"Add python.exe to PATH"**.
- A terminal: `cmd.exe`, PowerShell, or Windows Terminal.

Verify Python:

```cmd
python --version
```

You should see `Python 3.10.x` or higher.

## 2. Download SignalBridge

Drop the SignalBridge folder anywhere convenient, e.g.:

```
C:\Users\<you>\signalbridge\
```

## 3. Configure

Copy the example env file and edit it:

```cmd
copy .env.example .env
notepad .env
```

At minimum, change `TRADINGVIEW_WEBHOOK_SECRET` to a long random string.
The defaults keep `EXECUTION_MODE=paper` and `BROKER=paper`, which is correct for a first run.

## 4. First run

From the SignalBridge folder:

```cmd
run.bat
```

`run.bat` will:

1. Create `.venv\` if missing.
2. Install dependencies from `requirements.txt`.
3. Launch `uvicorn app.main:app` on the host/port from `.env`.

Leave the window open. Open another terminal and test:

```cmd
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/status
```

## 5. Expose to TradingView

TradingView cannot reach `127.0.0.1`. Use a tunnel:

- **ngrok** — see `app\tunnel\ngrok_notes.py`.
- **Cloudflare Tunnel** — see `app\tunnel\cloudflare_notes.py`.

## 6. Troubleshooting

- **`python` not recognized** — re-run the Python installer and tick "Add to PATH".
- **`pip install` fails behind a corporate proxy** — set `HTTPS_PROXY` in `.env` or your shell.
- **Port already in use** — change `APP_PORT` in `.env`.
- **Antivirus quarantines `.venv\`** — add a folder exception for `signalbridge\.venv\`.

## 7. Stopping

Press `Ctrl+C` in the run window.

## 8. Daily loop

You can re-run `run.bat` any time — it reuses the existing `.venv` and just starts the server.
