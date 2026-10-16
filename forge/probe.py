# -*- coding: utf-8 -*-
"""端口探活 —— 供一键启动脚本判断"控制台是不是已经在跑"。

只做只读探测，用的端点是纯内存的 `/api/busy`（不触发靶点扫描，毫秒级返回）。

    python -m forge.probe          # 打印在跑的控制台端口，没有则返回 1
    python -m forge.probe 8317 25  # 自定义起始端口与顺延范围
"""

import json
import sys
import urllib.request

BASE_PORT = 8317
SPAN = 25


def alive(port, base="127.0.0.1", timeout=0.6):
    """该端口上是否有活着的 Forge 控制台。

    两道校验：`Server` 响应头必须是 ForgeConsole，响应体必须是 `/api/busy`
    的合法 JSON。别的服务占着这个端口时不会被误认成"已经跑着"。
    """
    try:
        url = "http://%s:%d/api/busy" % (base, port)
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200:
                return False
            # BaseHTTPRequestHandler 会在 server_version 后拼上 " Python/3.x"
            if not (r.headers.get("Server") or "").startswith("ForgeConsole"):
                return False
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        return "busy" in data and "label" in data
    except Exception:
        return False


def find(base=BASE_PORT, span=SPAN):
    """返回第一个活着的控制台端口，找不到返回 0。"""
    for p in range(base, base + span):
        if alive(p):
            return p
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    base = int(argv[0]) if len(argv) > 0 else BASE_PORT
    span = int(argv[1]) if len(argv) > 1 else SPAN
    port = find(base, span)
    print(port)
    return 0 if port else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
