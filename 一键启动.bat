@echo off
rem ================================================================
rem  Forge - one-click launcher (ASCII only; all UI text is printed
rem  by Python so it renders correctly under any console code page)
rem ================================================================
chcp 65001 >nul 2>nul
setlocal
title Forge
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem ---- locate Python 3.8 or newer --------------------------------
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )
if not defined PY goto nopython
%PY% -c "import sys;raise SystemExit(0 if sys.version_info>=(3,8) else 1)" >nul 2>nul
if errorlevel 1 goto oldpython

rem ---- routing ---------------------------------------------------
if "%~1"=="" goto launch_wait
if /i "%~1"=="help"    goto help
if /i "%~1"=="-h"      goto help
if /i "%~1"=="--help"  goto help
if /i "%~1"=="selftest" goto selftest
if /i "%~1"=="start"   goto launch
if /i "%~1"=="launch"  goto launch
if /i "%~1"=="ui"      goto launch
if /i "%~1"=="menu"    goto passthru
if /i "%~1"=="scan"    goto passthru
if /i "%~1"=="status"  goto passthru
if /i "%~1"=="dry-run" goto passthru
if /i "%~1"=="apply"   goto passthru
if /i "%~1"=="revert"  goto passthru
if /i "%~1"=="persona" goto passthru
if /i "%~1"=="packs"   goto passthru

rem anything else (flags, ports) belongs to the launcher
%PY% run.py launch %*
if errorlevel 1 goto failed
exit /b 0

:launch_wait
rem double-clicked from Explorer: run, show the result, then hold 3s
%PY% run.py launch
if errorlevel 1 goto failed
%PY% -c "import time;time.sleep(3)"
exit /b 0

:launch
%PY% run.py launch
if errorlevel 1 goto failed
exit /b 0

:passthru
%PY% run.py %*
exit /b %errorlevel%

:selftest
%PY% selftest.py
exit /b %errorlevel%

:help
%PY% run.py launch --help
echo   Commands: scan / status / dry-run / apply / revert / persona
echo             packs / ui / menu / launch / selftest
echo   Examples: %~nx0                    one-click start
echo             %~nx0 menu             interactive menu
echo             %~nx0 scan             list clients
echo             %~nx0 apply --target all --full
echo             %~nx0 --port 9000 --no-browser
echo             %~nx0 selftest
exit /b 0

:failed
echo.
echo   [X] Start failed. To see the real error run:
echo         python run.py ui
echo.
pause
exit /b 1

:nopython
echo.
echo   [X] Python not found. Install Python 3.8+ from
echo       https://www.python.org/downloads/
echo       and tick "Add python.exe to PATH" during setup.
echo.
pause
exit /b 1

:oldpython
echo.
echo   [X] Python is too old. Forge needs 3.8 or newer.
echo.
pause
exit /b 1
