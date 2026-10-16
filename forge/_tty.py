# -*- coding: utf-8 -*-
"""控制台编码兼容 —— 让中文在任何 Windows 代码页下都不乱码。

双击 .bat 起的窗口默认是 936(GBK)，Python 输出 UTF-8 就会花屏。
这里在进程启动时把控制台输出代码页切到 65001，并把 stdio 重设成 UTF-8。

不影响写盘编码（那部分由各模块自己 `encoding="utf-8"` 控制）。
"""

import os
import sys


def setup():
    """幂等，可在任何入口安全重复调用。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if os.name != "nt":
        return

    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.SetConsoleOutputCP(65001)
        # 输入代码页保持默认：65001 的输入在部分系统上会让 input() 丢字
    except Exception:
        pass
