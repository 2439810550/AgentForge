@echo off
rem Forge - legacy entry point, kept for muscle memory.
rem Real launcher: 一键启动.bat (bare name = one-click start)
chcp 65001 >nul 2>nul
cd /d "%~dp0"
call "%~dp0一键启动.bat" %*
exit /b %errorlevel%
