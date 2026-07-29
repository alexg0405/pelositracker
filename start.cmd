@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found.
  echo Run: python -m venv .venv
  echo Then: .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
  exit /b 1
)
if not exist ".env" (
  if exist "env.example" (
    copy /y "env.example" ".env" >nul
    echo Created .env from env.example.
  ) else (
    echo No .env file found; using the app's built-in defaults.
  )
)
set "ENABLE_MLB_LIVE_FEED=true"
set "MLB_LIVE_POLL_SECONDS=5"
set "HISTORY_QUOTE_MIN_SECONDS=5"
set "HISTORY_QUOTE_HEARTBEAT_SECONDS=15"
set "DECISION_MARK_MIN_SECONDS=5"
set "DECISION_MARK_HEARTBEAT_SECONDS=15"
powershell -NoProfile -Command "$found=$false; foreach($c in (Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)){if($c.LocalPort -eq 8765 -or $c.LocalPort -eq 8775){Write-Host ('Port ' + $c.LocalPort + ' is already owned by process ' + $c.OwningProcess); $found=$true}}; if($found){exit 1}"
if errorlevel 1 (
  echo.
  echo Another PelosiTracker server is already running.
  echo Close its command window before starting this server so two execution
  echo engines cannot use different in-memory trading modes.
  echo.
  pause
  exit /b 1
)
echo.
echo PelosiTracker local server
echo Local URL: http://127.0.0.1:8765
echo Live trading data: polymarket-us-trading.db unless overridden in .env
echo Dry-run data: workstation-data\polymarket-us-dry-run.db unless overridden in .env
echo Press Ctrl+C in this window to stop it.
echo Restart this server after local Python code changes.
echo.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --workers 1
