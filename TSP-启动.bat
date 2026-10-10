@echo off
title TSP - A-share Quant Workbench (portable launcher)
setlocal EnableExtensions

REM ================================================================
REM  TSP portable one-click launcher  (copy the whole folder anywhere)
REM
REM  - Uses %~dp0 (this script's own folder) as the project root,
REM    so it works on any machine / any drive / any folder name.
REM  - First run: installs backend deps (uv sync) + frontend deps
REM    (pnpm install) automatically. Later runs: just start.
REM  - Starts backend + frontend as children of THIS console window
REM    (see TSP-run.ps1), therefore:
REM
REM        *** CLOSE THIS WINDOW  ==  STOP TSP ***
REM
REM    No need for the separate stop script during normal use (it is only a
REM    cleanup tool for leftovers after a force-kill).
REM  - Refuses to start a second copy when :3018/:3011 are already in
REM    use: two stacks sharing one data dir race for the data-dir lock
REM    (MiningProcessLockError) and one of them hangs.
REM  - Backend runs WITHOUT --reload on purpose: a reloader re-execs a
REM    new worker and the two can race for the data-dir lock at startup.
REM ================================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

REM uv installs here by default -> make sure it is on PATH
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
REM vite dev-server proxy target (defaults are the same; kept explicit)
set "BACKEND_HOST=127.0.0.1"
set "BACKEND_PORT=3018"

echo ================================================================
echo   TSP - A-share Quant Workbench   (portable one-click start)
echo   Project : %ROOT%
echo   Backend : http://127.0.0.1:3018
echo   Frontend: http://127.0.0.1:3011
echo   Stop    : CLOSE THIS WINDOW
echo ================================================================
echo.

REM ---------- 1) locate uv ----------
set "UV="
where uv >nul 2>nul && set "UV=uv"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV (
  echo [X] 'uv' not found. Install it first, then run this again:
  echo     powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
  echo.
  pause
  exit /b 1
)

REM ---------- 2) locate pnpm ----------
set "PNPM="
where pnpm >nul 2>nul && set "PNPM=pnpm"
if not defined PNPM where corepack >nul 2>nul && set "PNPM=corepack pnpm"
if not defined PNPM (
  echo [X] 'pnpm' not found. Install Node.js, then run:  corepack enable
  echo     winget install OpenJS.NodeJS.LTS
  echo.
  pause
  exit /b 1
)

REM ---------- 3) first run: install dependencies ----------
if not exist "%ROOT%\backend\.venv\Scripts\python.exe" goto INSTALL_BACKEND
echo [1/3] Backend dependencies OK
goto CHECK_FRONTEND

:INSTALL_BACKEND
echo [1/3] Installing backend dependencies ^(uv sync^) - first run only, please wait...
pushd "%ROOT%\backend"
"%UV%" sync
if errorlevel 1 goto FAIL_BACKEND
popd
goto CHECK_FRONTEND

:FAIL_BACKEND
popd
echo [X] uv sync failed. See messages above.
pause
exit /b 1

:CHECK_FRONTEND
if not exist "%ROOT%\frontend\node_modules" goto INSTALL_FRONTEND
echo [2/3] Frontend dependencies OK
goto START_SERVICES

:INSTALL_FRONTEND
echo [2/3] Installing frontend dependencies ^(pnpm install^) - first run only, please wait...
pushd "%ROOT%\frontend"
%PNPM% install
if errorlevel 1 goto FAIL_FRONTEND
popd
goto START_SERVICES

:FAIL_FRONTEND
popd
echo [X] pnpm install failed. See messages above.
pause
exit /b 1

:START_SERVICES
echo [3/3] Starting services...
netstat -an | findstr ":3018" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 goto ALREADY_RUNNING
netstat -an | findstr ":3011" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 goto ALREADY_RUNNING

title TSP RUNNING - close this window to stop
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\TSP-run.ps1" -Root "%ROOT%" -Uv "%UV%" -Pnpm "%PNPM%"
echo.
echo TSP stopped.  Press any key to close this window.
pause >nul
exit /b 0

:ALREADY_RUNNING
echo [X] Port 3018 or 3011 is already LISTENING - TSP looks ALREADY RUNNING.
echo     Close that TSP window to stop it, then run this launcher again.
echo     (If no TSP window is open, use the separate stop script to clean leftovers.)
echo.
pause
exit /b 1
