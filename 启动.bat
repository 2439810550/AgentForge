@echo off
chcp 65001 >nul
title Forge - 提示词注入工具
cd /d "%~dp0"
color 0A

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [X] 找不到 python。请先装 Python 3 并勾选 Add to PATH。
  echo.
  pause & exit /b 1
)

set "PY=python"
set "PYTHONIOENCODING=utf-8"

echo.
echo   ================================================================
echo      Forge  -  多客户端提示词注入工具
echo   ================================================================
echo.
echo      1  扫描本机客户端          只读
echo      2  查看详细状态            只读
echo      3  预演（不改盘）          只读
echo      4  执行注入
echo      5  还原
echo      6  查看人格装配结果        只读
echo      7  列出可用词包            只读
echo      8  跑自检（不碰真实配置）
echo      0  退出
echo.
set "C="
set /p C=  选择:

if "%C%"=="1" goto scan
if "%C%"=="2" goto status
if "%C%"=="3" goto dry
if "%C%"=="4" goto apply
if "%C%"=="5" goto revert
if "%C%"=="6" goto persona
if "%C%"=="7" goto packs
if "%C%"=="8" goto selftest
goto end

:scan
%PY% -m forge scan
pause & goto menu

:status
set "T="
set /p T=  目标 [all / wb / dsh / codex ...，回车=wb]:
if "%T%"=="" set "T=wb"
%PY% -m forge status --target %T%
pause & goto menu

:dry
set "T="
set /p T=  目标 [all / wb / dsh / codex ...，回车=wb]:
if "%T%"=="" set "T=wb"
%PY% -m forge dry-run --target %T%
pause & goto menu

:apply
set "T="
set /p T=  目标 [all / wb / dsh / codex ...，回车=wb]:
if "%T%"=="" set "T=wb"
set "F="
set /p F=  是否完全模式（WorkBuddy 命令闸门/文件保护）[y/N]:
if /i "%F%"=="y" ( %PY% -m forge apply --target %T% --full ) else ( %PY% -m forge apply --target %T% )
pause & goto menu

:revert
set "T="
set /p T=  目标 [all / wb / dsh / codex ...，回车=wb]:
if "%T%"=="" set "T=wb"
%PY% -m forge revert --target %T%
pause & goto menu

:persona
%PY% -m forge persona
pause & goto menu

:packs
%PY% -m forge packs
pause & goto menu

:selftest
%PY% selftest.py
pause & goto menu

:menu
cls
goto :eof

:end
exit /b 0
