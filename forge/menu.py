# -*- coding: utf-8 -*-
"""交互菜单 —— 纯 Python 实现，等价于原来的 `启动.bat` 菜单。

    1 启动控制台   2 扫描   3 状态   4 预演   5 注入
    6 还原        7 人格    8 词包    9 自检   0 退出

不用 .bat 跑菜单的理由：批处理的代码页解析对中文不稳，
菜单文字交给 Python 输出，任何 shell 下都一样。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MENU = """
  ================================================================
     Forge  ·  多客户端提示词注入工具
  ================================================================

     1  启动控制台（Web UI）    默认
     2  扫描本机客户端          只读
     3  查看详细状态            只读
     4  预演（不改盘）          只读
     5  执行注入
     6  还原
     7  查看人格装配结果        只读
     8  列出可用词包            只读
     9  跑自检（不碰真实配置）
     0  退出
"""


def _ask(prompt, default=""):
    try:
        v = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return v or default


def _target():
    t = _ask("  目标 [all / wb / dsh / codex ...，回车=all]: ", "all")
    return t


def _dispatch(argv):
    """借道 cli.main 执行，保证和命令行行为完全一致。"""
    from . import cli
    try:
        return cli.main(argv) or 0
    except KeyboardInterrupt:
        print("\n  已中断。")
        return 130


def run():
    while True:
        print(MENU)
        c = _ask("  选择 [回车=1]: ", "1")
        if c is None:
            return 0

        if c == "0":
            return 0
        elif c == "1":
            from . import launch
            launch.launch()
            _pause()
        elif c == "2":
            _dispatch(["scan"])
            _pause()
        elif c == "3":
            _dispatch(["status", "--target", _target()])
            _pause()
        elif c == "4":
            _dispatch(["dry-run", "--target", _target()])
            _pause()
        elif c == "5":
            t = _target()
            f = _ask("  完全模式（WorkBuddy 命令闸门 / 文件保护）[y/N]: ", "n")
            argv = ["apply", "--target", t]
            if f.lower() == "y":
                argv.append("--full")
            _dispatch(argv)
            _pause()
        elif c == "6":
            _dispatch(["revert", "--target", _target()])
            _pause()
        elif c == "7":
            _dispatch(["persona"])
            _pause()
        elif c == "8":
            _dispatch(["packs"])
            _pause()
        elif c == "9":
            _selftest()
            _pause()
        else:
            continue


def _pause():
    try:
        input("\n  按回车继续…")
    except (EOFError, KeyboardInterrupt):
        print()


def _selftest():
    import subprocess
    st = os.path.join(ROOT, "selftest.py")
    if not os.path.isfile(st):
        print("  找不到 selftest.py")
        return
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.call([sys.executable, st], cwd=ROOT, env=env)


def cmd_menu(args, asm):
    return run()
