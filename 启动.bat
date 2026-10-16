@echo off
rem Forge - legacy entry point, kept for muscle memory.
rem Real launcher: 一键启动.bat   (one-click start / menu / CLI)
rem This file is ASCII-only on purpose: batch + non-ASCII + codepage = mess.
chcp 65001 >nul 2>nul
cd /d "%~dp0"
rem no args -> interactive menu; args -> pass through
if "%~1"=="" ( call "%~dp0一键启动.bat" menu ) else ( call "%~dp0一键启动.bat" %* )
exit /b %errorlevel%
