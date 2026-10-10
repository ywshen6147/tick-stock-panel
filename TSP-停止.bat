@echo off
title TSP - stop services
setlocal EnableExtensions

REM ================================================================
REM  TSP portable stop script  (thin wrapper -> TSP-停止.ps1)
REM
REM  Stops ONLY this project's services; unrelated python/node
REM  processes on the machine are left untouched. The matching logic
REM  and its safety rails live in TSP-停止.ps1 (ASCII, easy to review).
REM ================================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo ================================================================
echo   TSP - stop backend / frontend
echo   Project : %ROOT%
echo ================================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\TSP-停止.ps1"

echo.
echo If a port is still in use, find and kill the owner manually:
echo   netstat -ano ^| findstr ":3018" ^| findstr LISTENING
echo   taskkill /PID ^<pid^> /T /F
echo.
pause
