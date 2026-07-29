@echo off
setlocal
cd /d "%~dp0"

set "WORKSTATION_URL=http://127.0.0.1:8765"
set "BROWSER_PROFILE=%LOCALAPPDATA%\PelosiTracker\DedicatedChromeProfile"

netstat -ano -p tcp | findstr /R /C:":8775 .*LISTENING" >nul
if not errorlevel 1 (
  echo.
  echo A workstation server is already running on port 8775.
  echo Close it before using this launcher so only one execution engine is active.
  echo.
  pause
  exit /b 1
)

netstat -ano -p tcp | findstr /R /C:":8765 .*LISTENING" >nul
if errorlevel 1 (
  echo Starting the PelosiTracker server in its own visible command window...
  start "PelosiTracker Server" cmd /k call "%~dp0start.cmd"
  echo Waiting for the local server...
  for /l %%I in (1,1,30) do (
    curl.exe -s --max-time 1 "%WORKSTATION_URL%/api/health" >nul 2>&1
    if not errorlevel 1 goto server_ready
    timeout /t 1 /nobreak >nul
  )
  echo.
  echo The server did not become ready within 30 seconds.
  echo Review the PelosiTracker Server window for the exact startup error.
  pause
  exit /b 1
)

:server_ready
set "DEDICATED_BROWSER="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
  set "DEDICATED_BROWSER=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
)
if not defined DEDICATED_BROWSER if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" (
  set "DEDICATED_BROWSER=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
)
if not defined DEDICATED_BROWSER if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" (
  set "DEDICATED_BROWSER=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
)
if not defined DEDICATED_BROWSER if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" (
  set "DEDICATED_BROWSER=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
)
if not defined DEDICATED_BROWSER if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" (
  set "DEDICATED_BROWSER=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
)

if not defined DEDICATED_BROWSER (
  echo.
  echo Chrome or Edge could not be found.
  echo Start the server with start.cmd and open %WORKSTATION_URL% manually.
  pause
  exit /b 1
)

echo Opening the trader in a dedicated browser profile...
start "PelosiTracker Dedicated Browser" "%DEDICATED_BROWSER%" ^
  --user-data-dir="%BROWSER_PROFILE%" ^
  --app="%WORKSTATION_URL%" ^
  --no-first-run ^
  --no-default-browser-check

endlocal
