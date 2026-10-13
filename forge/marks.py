# -*- coding: utf-8 -*-
"""标记块引擎 —— 可追加、可整块替换、可精确剥离的注入原语。

所有"往别人的配置文件里塞东西"的需求，最后都归结为同一个问题：
**怎么保证反复执行不堆叠、还能一个字节不差地还原回去。**

三种缝合方式，对应三种文件形态：

  1. `MarkBlock`  —— 通用文本文件（.md / .mdc / .txt / .js）
                     用注释标记包住，整块替换。

  2. `TopLevelKey` —— TOML / INI 这类"表头之后键属于那张表"的格式
                     （见 toml.py，这里只放接口约定）

  3. `JsonString`  —— JSON 里内嵌的大段提示词文本
                     字符串级替换，不重排 JSON、不改缩进。

**幂等的实现要点**：整件事只在"行"这一级做增删，用 split/join("\n") 这对
恒等操作当手术刀（`"\n".join(s.split("\n")) == s` 对任何字符串都成立）。
只要保证"只整行插、只整行删"，put → strip 就能逐字节回到原样 ——
包括"原文件结尾本来没有换行"这种边角。

反例（初版踩过的坑）：用 `text[:i].rstrip("\n")` + `"\n\n".join(parts)`
+ `.rstrip()+"\n"` 去拼，等于把用户的换行风格重排了一遍，永远回不到原样。
"""

from . import core


# 通用标记常量（HTML 注释型，适用于 .md / .mdc / .txt）
HTML_BEGIN = "<!-- FORGE:BEGIN -->"
HTML_END = "<!-- FORGE:END -->"


# ════════════════════════════════════════════════════════════════════════
#  通用文本文件：注释标记块
# ════════════════════════════════════════════════════════════════════════

def block_text(begin, body, end):
    """把 body 包成标记块。body 尾部空白先收干净。"""
    return "%s\n%s\n%s" % (begin, body.rstrip("\n"), end)


def strip_block(text, begin, end):
    """删掉标记块及其前后多余空行。块不存在时原样返回。

    **行级手术**（关键）：整件事只在“行”这一级做增删，
    用 split/join("\\n") 这对恒等操作当手术刀。
    只要保证“只整行删”，strip(put(x)) 就能与 strip(x) 逐字节一致 ——
    包括“原文件结尾本来没有换行”这种边角。

    早期用 ``text[:i].rstrip("\\n")`` + ``"\\n\\n".join(parts)`` 拼的版本
    会把用户的换行风格重排一遍，实测会吃掉文件结尾的换行。
    """
    if not text or begin not in text:
        return text or ""

    lines = text.split("\n")
    b, e = begin.strip(), end.strip()

    bi = next((n for n, l in enumerate(lines) if l.strip() == b), -1)
    if bi < 0:
        # 标记不在独立行上（与内容同行）：退回字符级删法，保守处理
        i = text.find(begin)
        j = text.find(end, i)
        if j < 0:
            return text[:i].rstrip("\n") + "\n"
        return text[:i] + text[j + len(end):]

    ei = next((n for n in range(bi, len(lines)) if lines[n].strip() == e), -1)
    if ei < 0:
        # 只有前半截（被人手删过）：保守起见，不动
        return text

    lo, hi = bi, ei + 1
    # 收掉我们插在块前/后的那一个空行（只各收一个）
    if lo > 0 and lines[lo - 1] == "":
        lo -= 1
    if hi < len(lines) and lines[hi] == "":
        hi += 1
    del lines[lo:hi]
    return "\n".join(lines)


def has_block(text, begin):
    return bool(text) and begin in text


def put_block(text, block, begin, end):
    """插标记块；已有则先删后插（幂等）。

    插入位置：文件末尾（大多数 Markdown 规则文件都该这样 —— 后写的规则
    在多数客户端里优先级更高，且不会把用户原有内容挤下去）。

    分隔用 ``\\n\\n``（固定两个换行），不依赖 base 的结尾形态 ——
    这样 strip 时那两个空行里“属于分隔”的那一个能被准确收掉，
    用户原有的结尾换行不会受牵连。
    """
    base = strip_block(text or "", begin, end)
    if not base:
        return block.rstrip("\n") + "\n"
    return base + "\n\n" + block.rstrip("\n") + "\n"


# ════════════════════════════════════════════════════════════════════════
#  JSON 内嵌文本
# ════════════════════════════════════════════════════════════════════════

def json_escape_body(s):
    """把多行文本转成能安全塞进 JSON 字符串字面量的形式。

    关键：换行必须是**两字符的转义序列** \\n，不是真实换行符。
    直接塞真实换行进去，JSON 语法当场损坏、整个配置文件报废 ——
    这是老版本实际踩过的坑（.tpl 和 product.json 用了同一份替换文本）。
    """
    out = s.replace("\\", "\\\\")
    out = out.replace("\"", "\\\"")
    out = out.replace("\r\n", "\\n").replace("\n", "\\n")
    out = out.replace("\r", "\\n")
    out = out.replace("\t", "\\t")
    return out


def json_unescape_body(s):
    """反向：仅供调试/校验用。"""
    return (s.replace("\\t", "\t")
             .replace("\\n", "\n")
             .replace('\\"', '"')
             .replace("\\\\", "\\"))


def replace_tagged(text, tag, new_inner, count_all=True):
    """把 <tag>...</tag> 包裹的内容整体换掉。

    new_inner 需要调用方自己决定转义形式（见 json_escape_body）。
    返回 (新文本, 替换处数)。
    """
    op, cl = "<%s>" % tag, "</%s>" % tag
    out, pos, n = [], 0, 0
    while True:
        i = text.find(op, pos)
        if i < 0:
            break
        j = text.find(cl, i)
        if j < 0:
            break
        out.append(text[pos:i + len(op)])
        out.append(new_inner)
        out.append(cl)
        pos = j + len(cl)
        n += 1
        if not count_all:
            break
    if n == 0:
        return text, 0
    out.append(text[pos:])
    return "".join(out), n


def find_json_string_end(text, start):
    """从 start 位置开始，找它所在 JSON 字符串的结束位置（未转义的引号之后）。

    用于"在某个字符串值内部继续定位"的场景。
    返回结束引号后一位的索引；找不到返回 -1。
    """
    i = start
    n = len(text)
    if i >= n or text[i] != '"':
        return -1
    i += 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    return -1


# ════════════════════════════════════════════════════════════════════════
#  可选段（存在则替换、不存在则追加）
# ════════════════════════════════════════════════════════════════════════

def upsert_section(text, marker, body, nl=None):
    """按 marker 定位一个段，有则整段替换，无则追加到末尾。

    与 put_block 的区别：put_block 管的是 begin/end 成对标记的大块；
    这个管的是单行标记开头的段落（更适合往现有提示词里补一小节）。
    """
    nl = nl or core.detect_nl(text)
    body = core.match_nl(body, nl)
    idx = text.find(marker)
    if idx < 0:
        return text.rstrip() + nl + body.rstrip() + nl

    # 找该段的结束：下一个同级标题或文件末尾
    after = idx + len(marker)
    nxt = text.find("\n#", after)
    if nxt < 0:
        return text[:idx] + body.rstrip() + nl
    return text[:idx] + body.rstrip() + nl + text[nxt:].lstrip("\n")
