# -*- coding: utf-8 -*-
"""TOML 顶层键操作。

Codex 的 `config.toml` 有一个致命细节：**表头之后的键属于那张表**。

    [shell_environment_policy.set]
    FOO = "bar"

如果把我们那行追加到文件末尾，它会被解析成 `FOO` 那张表里的一个键 ——
Codex 完全读不到配置，而表面上"文件改了、grep 得到、apply 报成功"。
这是最难发现的一类坏，因为它不报错。

所以规则是：**只能插到顶层键区**，也就是第一个表头之前。

另外：TOML 没有注释块语法，不能像 Markdown 那样用 <!-- --> 包住。
Codex 官方认 `#` 行注释，所以用成对的 `#` 注释行当标记。
"""

import re

from . import core


def _top_insert_index(lines):
    """找第一个表头/数组表头的位置。返回可插入下标。"""
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and not s.startswith("[["):
            return i
        if s.startswith("[["):
            return i
    return len(lines)


def strip_block(text, begin, end):
    """删掉标记块。与 put_block 严格互逆（含"结尾有无换行"）。

    整件事只在行这一级做增删，用 split/join("\\n") 这对恒等操作当手术刀。
    这样 put → strip 能逐字节回到原样，包括"原文件结尾本来没有换行"。
    """
    if not text or begin not in text:
        return text or ""
    lines = text.split("\n")
    b, e = begin.strip(), end.strip()
    bi = next((n for n, l in enumerate(lines) if l.strip() == b), -1)
    if bi < 0:
        return text
    ei = next((n for n in range(bi, len(lines)) if lines[n].strip() == e), -1)
    if ei < 0:
        # 只有前半截（被人手删过）：保守起见不动
        return text
    lo, hi = bi, ei + 1
    # 收掉我们插在块前后的那两个空行（只各收一个，不碰用户的）
    if lo > 0 and lines[lo - 1] == "":
        lo -= 1
    if hi < len(lines) and lines[hi] == "":
        hi += 1
    del lines[lo:hi]
    return "\n".join(lines)


def put_top(text, block, begin, end):
    """把标记块插到顶层键区；已有则先删后插（幂等）。

    只整行插入 ``空行 + 块 + 空行``，绝不重排、不改写用户已有的任何一行。
    """
    base = strip_block(text or "", begin, end)
    blk = block.rstrip("\n").split("\n")
    if not base.strip():
        return "\n".join(blk) + "\n"
    lines = base.split("\n")
    idx = _top_insert_index(lines)
    lines[idx:idx] = [""] + blk + [""]
    return "\n".join(lines)


def set_top_key(text, key, value, begin, end):
    """在顶层键区写 `key = value`，包在标记里。"""
    val = value.replace("\\", "\\\\").replace("'", "\\'")
    block = "%s\n%s = '%s'\n%s" % (begin, key, val, end)
    return put_top(text, block, begin, end)


def parse_ok(text):
    """尽力验证 TOML 是否仍可解析。没有 tomllib 时退化成括号配对检查。

    用途：写盘后自检 —— 写坏了立刻回滚，不要让用户拿到一个 Codex 读不了的配置。
    """
    try:
        import tomllib
        tomllib.loads(text)
        return True, ""
    except ModuleNotFoundError:
        pass
    except Exception as e:
        return False, str(e)

    # 退化检查：表头后不能出现悬空的引号/括号
    try:
        depth = 0
        for line in text.split("\n"):
            s = line.split("#", 1)[0]
            depth += s.count("[") - s.count("]")
        if depth != 0:
            return False, "方括号不配对（depth=%d）" % depth
        if text.count('"') % 2 and text.count("'") % 2:
            return False, "引号数量异常"
        return True, ""
    except Exception as e:
        return False, str(e)


def top_level_keys(text):
    """列出顶层键名（第一个表头之前）。"""
    out = []
    for line in (text or "").split("\n"):
        s = line.strip()
        if s.startswith("["):
            break
        m = re.match(r"^([A-Za-z_][\w.-]*)\s*=", s)
        if m:
            out.append(m.group(1))
    return out
