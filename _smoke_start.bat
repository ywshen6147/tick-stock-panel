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
REM  - Skips starting a service whose port is already in use.
REM  - Backend is started WITHOUT --reload on purpose: closing the
REM    window then shuts the process down cleanly (no orphan workers
REM    that would later block startup with the data-dir lock).
REM
REM  Need hot reload while editing code?  Use dev.ps1 instead.
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
set "STARTED_FE=0"

netstat -an | findstr ":3018" | findstr "LISTENING" >nul 2>nul
if errorlevel 1 goto START_BACKEND
echo     - backend  already running on :3018  ^(not started again^)
goto CHECK_FE

:START_BACKEND
start "TSP-Backend :3018" /D "%ROOT%\backend" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --app-dir . --host 0.0.0.0 --port 3018"
echo     - backend  started  [window: TSP-Backend :3018]

:CHECK_FE
netstat -an | findstr ":3011" | findstr "LISTENING" >nul 2>nul
if errorlevel 1 goto START_FRONTEND
echo     - frontend already running on :3011  ^(not started again^)
goto DONE

:START_FRONTEND
start "TSP-Frontend :3011" /D "%ROOT%\frontend" cmd /k "%PNPM% dev --host 0.0.0.0 --port 3011"
echo     - frontend started  [window: TSP-Frontend :3011]
set "STARTED_FE=1"
echo.
echo Waiting for the dev server...
ping -n 9 127.0.0.1 >nul
start "" "http://127.0.0.1:3011/"

:DONE
echo.
echo ================================================================
echo   Done.
echo   Open  : http://127.0.0.1:3011/
echo   Stop  : run TSP-停止.bat   (or close the two black windows)
echo   Note  : first startup may sync/verify data and take a while.
echo ================================================================
echo.
pause
