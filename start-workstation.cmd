@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo The workstation has not been set up yet. Running setup now...
  call setup-workstation.cmd
  if errorlevel 1 exit /b 1
)

if not exist ".env" (
  copy /y "env.example" ".env" >nul
  echo Created .env from the workstation template.
)

if not exist "workstation-data" mkdir "workstation-data"

powershell -NoProfile -Command "$found=$false; foreach($c in (Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)){if($c.LocalPort -eq 8765 -or $c.LocalPort -eq 8775){Write-Host ('Port ' + $c.LocalPort + ' is already owned by process ' + $c.OwningProcess); $found=$true}}; if($found){exit 1}"
if errorlevel 1 (
  echo.
  echo Another PelosiTracker server is already running.
  echo Close its command window before starting the workstation so two
  echo execution engines cannot use different in-memory trading modes.
  echo.
  pause
  exit /b 1
)

set "WORKSTATION_MODE=true"
set "ENABLE_PAPER_BOTS=false"
set "ENABLE_POLYMARKET_US_TRADING=true"
set "LEDGER_DB=workstation-data\ledger.db"
set "HISTORY_DB=workstation-data\history.db"
set "STATE_DB=workstation-data\state.db"
set "POLYMARKET_US_TRADING_DB=workstation-data\polymarket-us-trading.db"
set "POLYMARKET_US_DRY_RUN_DB=workstation-data\polymarket-us-dry-run.db"
set "MODEL_LAB_DB=workstation-data\model-lab.db"
set "ENABLE_MLB_LIVE_FEED=true"
set "MLB_LIVE_POLL_SECONDS=5"
set "HISTORY_QUOTE_MIN_SECONDS=5"
set "HISTORY_QUOTE_HEARTBEAT_SECONDS=15"
set "DECISION_MARK_MIN_SECONDS=5"
set "DECISION_MARK_HEARTBEAT_SECONDS=15"

echo.
echo PelosiTracker US Research Workstation
echo Local URL: http://127.0.0.1:8775
echo Login: admin / admin
echo Live trading data: workstation-data\polymarket-us-trading.db
echo Dry-run data: workstation-data\polymarket-us-dry-run.db
echo Press Ctrl+C in this window to stop it.
echo Restart this server after local Python code changes.
echo.

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8775 --workers 1
