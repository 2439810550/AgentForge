# -*- coding: utf-8 -*-
"""等价入口 —— `python run.py <命令>` 与 `python -m forge <命令>` 完全一致。

存在的理由：有些环境里 `python -m forge` 会因为当前目录不在 sys.path
而找不到包，直接跑这个文件则永远能找到。

    python run.py scan
    python run.py apply --target all
    python run.py ui
    python run.py launch
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forge import _tty
from forge.cli import main

if __name__ == "__main__":
    _tty.setup()
    sys.exit(main())
