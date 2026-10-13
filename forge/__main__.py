# -*- coding: utf-8 -*-
"""让 `python -m forge` 可用。"""

import sys

from .cli import main

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
