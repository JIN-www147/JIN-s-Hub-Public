@echo off
chcp 65001 >nul
title Tempo AI Answer - Install
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
setlocal enabledelayedexpansion

echo ============================================
echo   Installing dependencies (run once)
echo ============================================
echo.

rem find a usable base python (skip the Microsoft Store alias)
set "BASE="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "BASE=py -3"

if not defined BASE (
  for /f "delims=" %%p in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do (
    if not defined BASE set "BASE="%%p""
  )
)
if not defined BASE (
  for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if not defined BASE if exist "%%d\python.exe" set "BASE="%%d\python.exe""
  )
)
if not defined BASE (
  for %%d in ("C:\Python313" "C:\Python312" "C:\Python311" "C:\Python310"
              "%ProgramFiles%\Python313" "%ProgramFiles%\Python312" "%ProgramFiles%\Python311") do (
    if not defined BASE if exist "%%~d\python.exe" set "BASE="%%~d\python.exe""
  )
)

if defined BASE (
  !BASE! -c "import sys" >nul 2>nul
  if errorlevel 1 set "BASE="
)

if not defined BASE goto NOPY

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment .venv ...
  !BASE! -m venv .venv
)

if not exist ".venv\Scripts\python.exe" goto VENVFAIL

echo Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet

echo Installing dependencies ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo.
echo Done. Next: run login.bat (first time only), then run.bat.
echo.
pause
exit /b 0

:NOPY
echo.
echo ============================================================
echo   [ERROR] No usable Python found
echo ============================================================
echo.
echo Your "python" is probably the Microsoft Store alias, which
echo does nothing when executed.
echo.
echo Fix:
echo   1. Install Python 3.10+ from https://www.python.org/downloads/
echo   2. During setup TICK "Add python.exe to PATH"
echo   3. Open a NEW cmd window and run: python --version
echo   4. Then run install.bat again
echo.
pause
exit /b 1

:VENVFAIL
echo.
echo [ERROR] Failed to create virtual environment.
echo.
pause
exit /b 1
