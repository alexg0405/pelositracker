@echo off
setlocal
cd /d "%~dp0"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH.
  echo Install Python 3.10 through 3.15, then run this file again.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating the local Python environment...
  python -m venv .venv
  if errorlevel 1 exit /b 1
)

echo Installing the workstation dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 exit /b 1

echo Building the unchanged Rust calculation engine...
call build-rust.cmd
if errorlevel 1 exit /b 1

if not exist ".env" (
  copy /y "env.example" ".env" >nul
  echo Created the private local configuration file: .env
)

if not exist "workstation-data" mkdir "workstation-data"

echo.
echo Workstation setup is complete.
echo Run start-workstation.cmd and open http://127.0.0.1:8775
