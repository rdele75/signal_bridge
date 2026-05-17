@echo off
REM SignalBridge launcher for Windows.
setlocal ENABLEDELAYEDEXPANSION

cd /d "%~dp0"

if not exist ".venv" (
    echo [signalbridge] creating virtualenv .venv
    python -m venv .venv
    if errorlevel 1 (
        echo [signalbridge] ERROR: failed to create .venv. Make sure Python 3.10+ is installed.
        exit /b 1
    )
)

call .venv\Scripts\activate.bat

echo [signalbridge] installing requirements
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo [signalbridge] ERROR: dependency install failed.
    exit /b 1
)

REM Load APP_HOST / APP_PORT from .env if present.
set APP_HOST=127.0.0.1
set APP_PORT=8000
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        set "_KEY=%%A"
        set "_VAL=%%B"
        if /i "!_KEY!"=="APP_HOST" set "APP_HOST=!_VAL!"
        if /i "!_KEY!"=="APP_PORT" set "APP_PORT=!_VAL!"
    )
)

echo [signalbridge] starting uvicorn on %APP_HOST%:%APP_PORT%
python -m uvicorn app.main:app --host %APP_HOST% --port %APP_PORT%

endlocal
