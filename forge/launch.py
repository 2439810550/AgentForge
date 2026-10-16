# -*- coding: utf-8 -*-
"""一键启动 —— 探测已有控制台，没有就后台拉起一个，再开浏览器。

    python -m forge launch            # 一键启动（默认标准入口）
    python -m forge launch --port 9000
    python -m forge menu              # 交互菜单（不用 .bat 也能跑）

不依赖任何 shell 的中文能力：所有提示由 Python 输出，
所以 Windows 批处理、PowerShell、Git Bash 下表现一致。
"""

import os
import subprocess
import sys
import time
import webbrowser

from . import probe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PORT = 8317
SPAN = 25
BOOT_TIMEOUT = 30

# Windows CreateProcess 标志
_CREATE_NEW_CONSOLE = 0x00000010
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _entry():
    """启动子进程用的入口参数（不含解释器）：优先 run.py（永远能找到包）。"""
    rp = os.path.join(ROOT, "run.py")
    if os.path.isfile(rp):
        return [rp]
    return ["-m", "forge"]


def _env():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["FORGE_LAUNCHED"] = "1"
    return env


def log_path():
    """后台（无窗口）模式的日志文件路径。"""
    return os.path.join(ROOT, "_state", "forge-ui.log")


def spawn(port=DEFAULT_PORT, new_window=True):
    """后台拉起控制台，返回 Popen。

    new_window=True  -> 新开一个可见控制台窗口，方便看日志、Ctrl+C 关掉
    new_window=False -> 完全脱离，输出写 _state/forge-ui.log

    注意：DETACHED_PROCESS 下子进程没有控制台句柄，stdout/stderr 必须显式
    重定向，否则子进程一写就崩（实测 30 秒内起不来）。
    """
    # -u：关闭子进程的块缓冲，后台日志才能实时看到
    cmd = [sys.executable, "-u"] + _entry() + ["ui", "--port", str(port)]
    kw = {"cwd": ROOT, "env": _env(), "close_fds": True}

    if os.name == "nt":
        if new_window:
            kw["creationflags"] = _CREATE_NEW_CONSOLE
        else:
            kw["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
            kw["stdin"] = subprocess.DEVNULL
            lp = log_path()
            os.makedirs(os.path.dirname(lp), exist_ok=True)
            lf = open(lp, "ab", buffering=0)
            kw["stdout"] = lf
            kw["stderr"] = lf
    else:
        kw["stdin"] = subprocess.DEVNULL
        kw["stdout"] = subprocess.DEVNULL
        kw["stderr"] = subprocess.DEVNULL
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, **kw)


def wait_up(base=DEFAULT_PORT, span=SPAN, timeout=BOOT_TIMEOUT):
    """等控制台上线，返回端口，超时返回 0。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = probe.find(base, span)
        if p:
            return p
        time.sleep(0.5)
    return 0


def launch(port=DEFAULT_PORT, open_browser=True, force_new=False, new_window=True):
    """一键启动主流程。返回进程退出码。"""
    port = int(port or DEFAULT_PORT)

    if not force_new:
        p = probe.find(port, SPAN)
        if p:
            url = "http://127.0.0.1:%d/" % p
            print()
            print("  Forge 控制台已经在跑（端口 %d）" % p)
            print("  地址：%s" % url)
            print()
            if open_browser:
                webbrowser.open(url)
            return 0

    print()
    print("  正在启动 Forge 控制台…")
    proc = None
    try:
        proc = spawn(port, new_window=new_window)
    except Exception as e:
        print("  [X] 拉不起来：%s" % e)
        print("      手动跑：python run.py ui --port %d" % port)
        return 1

    p = wait_up(port, SPAN, BOOT_TIMEOUT)

    # 启动竞态：另一个实例在这几秒里先占了请求端口。我们刚起的这个就是多余的。
    if p and p != port and probe.alive(port):
        try:
            proc.terminate()
        except Exception:
            pass
        p = port

    if not p:
        print("  [X] %d 秒内没起来（端口 %d-%d 都没响应）。" % (BOOT_TIMEOUT, port, port + SPAN - 1))
        print("      手动跑一次看报错：python run.py ui --port %d" % port)
        if not new_window and os.path.isfile(log_path()):
            print("      后台日志：%s" % log_path())
        return 1

    url = "http://127.0.0.1:%d/" % p
    print()
    print("  Forge 控制台已就绪")
    print("  " + "─" * 52)
    print("  地址：%s" % url)
    if new_window:
        print("  关掉控制台：关掉那个标题为 Forge 的窗口，或窗口里按 Ctrl+C")
    else:
        print("  后台模式，日志：%s" % log_path())
        print("  关掉控制台：taskkill /F /PID <进程号>")
    print()
    if open_browser:
        webbrowser.open(url)
    return 0


def cmd_launch(args, asm):
    return launch(
        port=getattr(args, "port", None) or DEFAULT_PORT,
        open_browser=not getattr(args, "no_browser", False),
        force_new=getattr(args, "new", False),
        new_window=not getattr(args, "headless", False))
