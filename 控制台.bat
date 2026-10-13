@echo off
chcp 65001 >nul
title Forge 控制台
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [X] 找不到 python。请先装 Python 3 并勾选 Add to PATH。
  echo.
  pause & exit /b 1
)

echo.
echo   正在启动 Forge 控制台...
echo   浏览器会自动打开，若没弹出手动访问 http://127.0.0.1:8317/
echo.
echo   关闭本窗口 = 关闭控制台。
echo.

set PYTHONIOENCODING=utf-8
python -m forge ui
pause
