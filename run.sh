#!/usr/bin/env bash
# SignalBridge launcher for Linux / macOS.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if [ ! -d ".venv" ]; then
    echo "[signalbridge] creating virtualenv .venv"
    "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[signalbridge] installing requirements"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if [ -f ".env" ]; then
    # Export non-comment lines so APP_HOST / APP_PORT propagate.
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

HOST="${APP_HOST:-127.0.0.1}"
PORT="${APP_PORT:-8000}"

echo "[signalbridge] starting uvicorn on $HOST:$PORT"
exec python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
