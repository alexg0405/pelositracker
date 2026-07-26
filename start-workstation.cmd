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

set "WORKSTATION_MODE=true"
set "ENABLE_PAPER_BOTS=false"
set "ENABLE_POLYMARKET_US_TRADING=true"
set "LEDGER_DB=workstation-data\ledger.db"
set "HISTORY_DB=workstation-data\history.db"
set "STATE_DB=workstation-data\state.db"
set "POLYMARKET_US_TRADING_DB=workstation-data\polymarket-us-trading.db"

echo.
echo PelosiTracker US Research Workstation
echo Local URL: http://127.0.0.1:8775
echo Login: admin / admin
echo Press Ctrl+C in this window to stop it.
echo.

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8775 --workers 1
