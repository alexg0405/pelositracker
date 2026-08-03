@echo off
setlocal
cd /d "%~dp0"

REM Opens the port-8775 research workstation as a standalone browser app using
REM its own profile. Protected actions (clear, remove, sell, stop, reset) are
REM guarded by window.confirm(). A browser that has been told to "prevent this
REM page from creating additional dialogs" makes confirm() return false
REM silently, so those buttons appear dead with no error anywhere. A dedicated
REM profile starts clean and keeps that state away from your normal browsing.

set "WORKSTATION_URL=http://127.0.0.1:8775"
REM Deliberately separate from DedicatedChromeProfile: cookies are not scoped by
REM port, so sharing one profile would let the 8765 trader and this workstation
REM overwrite each other's login session.
set "BROWSER_PROFILE=%LOCALAPPDATA%\PelosiTracker\WorkstationChromeProfile"

netstat -ano -p tcp | findstr /R /C:":8765 .*LISTENING" >nul
if not errorlevel 1 (
  echo.
  echo The dedicated trader server is already running on port 8765.
  echo Close it before using this launcher so only one execution engine is active.
  echo.
  pause
  exit /b 1
)

netstat -ano -p tcp | findstr /R /C:":8775 .*LISTENING" >nul
if errorlevel 1 (
  echo Starting the research workstation in its own visible command window...
  start "PelosiTracker Workstation" cmd /k call "%~dp0start-workstation.cmd"
  echo Waiting for the local server...
  for /l %%I in (1,1,30) do (
    curl.exe -s --max-time 1 "%WORKSTATION_URL%/api/health" >nul 2>&1
    if not errorlevel 1 goto server_ready
    timeout /t 1 /nobreak >nul
  )
  echo.
  echo The server did not become ready within 30 seconds.
  echo Review the PelosiTracker Workstation window for the exact startup error.
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
  echo Start the server with start-workstation.cmd and open %WORKSTATION_URL% manually.
  pause
  exit /b 1
)

echo Opening the workstation in a dedicated browser profile...
echo Login: admin / admin
start "PelosiTracker Workstation Browser" "%DEDICATED_BROWSER%" ^
  --user-data-dir="%BROWSER_PROFILE%" ^
  --app="%WORKSTATION_URL%" ^
  --no-first-run ^
  --no-default-browser-check

endlocal
