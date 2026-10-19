@echo off
chcp 65001 >nul
title Tempo AI Answer - Login
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
setlocal enabledelayedexpansion
rem 1) project venv (most reliable)
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY="%~dp0.venv\Scripts\python.exe""

rem 2) py launcher
if not defined PY (
  py -3 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)

rem 3) python from PATH, skip the Microsoft Store alias (it exits silently)
if not defined PY (
  for /f "delims=" %%p in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do (
    if not defined PY set "PY="%%p""
  )
)

rem 4) common install locations (python.org / store-less installs)
if not defined PY (
  for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if not defined PY if exist "%%d\python.exe" set "PY="%%d\python.exe""
  )
)
if not defined PY (
  for %%d in ("C:\Python313" "C:\Python312" "C:\Python311" "C:\Python310"
              "%ProgramFiles%\Python313" "%ProgramFiles%\Python312" "%ProgramFiles%\Python311") do (
    if not defined PY if exist "%%~d\python.exe" set "PY="%%~d\python.exe""
  )
)

rem verify it actually runs
if defined PY (
  !PY! -c "import sys" >nul 2>nul
  if errorlevel 1 set "PY="
)
if not defined PY goto NOPY

!PY! main.py login
echo.
pause
exit /b 0

:NOPY
echo.
echo [ERROR] No usable Python found.
echo Install Python 3.10+ from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH"), then run install.bat.
echo.
pause
exit /b 1
