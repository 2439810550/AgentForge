# -*- coding: utf-8 -*-
"""靶点注册表。

新增一个客户端的成本 = 写一个类 + 在这里加一行。
"""

from .targets import (
    GeminiTarget, QwenTarget, IflowTarget, TraeTarget, CodeBuddyTarget,
    OpenCodeTarget, WindsurfTarget, ClineTarget, CopilotTarget,
    CursorTarget, ClaudeTarget, ZCodeTarget,
)
from .t_codex import CodexTarget
from .t_workbuddy import WorkBuddyTarget
from .t_dsh import DshTarget


#: key -> 类。key 就是命令行 --target 用的名字。
REGISTRY = {}
for _cls in (
    WorkBuddyTarget,      # wb
    DshTarget,            # dsh
    CodexTarget,          # codex
    ClaudeTarget,         # claude
    CursorTarget,         # cursor
    ZCodeTarget,          # zcode
    GeminiTarget,         # gemini
    QwenTarget,           # qwen
    IflowTarget,          # iflow
    TraeTarget,           # trae
    CodeBuddyTarget,      # codebuddy
    OpenCodeTarget,       # opencode
    WindsurfTarget,       # windsurf
    ClineTarget,          # cline
    CopilotTarget,        # copilot
):
    REGISTRY[_cls.key] = _cls


def keys():
    return list(REGISTRY.keys())


def get(key):
    return REGISTRY.get(key)


def all_classes():
    return [REGISTRY[k] for k in keys()]


def resolve(expr):
    """把 --target 的表达式展开成 key 列表。

    支持：all / wb,dsh / 单个 key。未知 key 会被记下来。
    """
    if not expr or expr == "all":
        return list(keys()), []
    out, bad = [], []
    for part in str(expr).replace(";", ",").split(","):
        k = part.strip().lower()
        if not k:
            continue
        if k == "all":
            for x in keys():
                if x not in out:
                    out.append(x)
            continue
        if k in REGISTRY:
            if k not in out:
                out.append(k)
        else:
            bad.append(k)
    return out, bad


def describe():
    rows = []
    for k in keys():
        c = REGISTRY[k]
        rows.append((k, c.label, c.default_sub or "-"))
    return rows
