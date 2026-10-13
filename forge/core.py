# -*- coding: utf-8 -*-
"""Forge 核心原语 —— 读写、原子写、备份、归档、全局闸门。

这个模块只做一件事：**在别人的磁盘上，安全地改文件**。
所有上层的靶点逻辑都建立在它上面。

四条设计原则，每一条都是踩过坑之后定下来的：

  1. **读不干净就跳过。**
     遇到 GBK / UTF-16 / 二进制内容时，`errors="replace"` 会把文件读成一片
     U+FFFD 然后照原样写回去 —— 用户的文件被静默转码毁掉。所以解不开就返回
     None，调用方一律按"读取失败"处理。宁可不打这个补丁，也不写坏文件。

  2. **写必须原子。**
     先写同目录临时文件、fsync、再 os.replace。中途断电或异常不会把原文件
     截成半截。旧实现直接 open(path,"wb") 打开就写，写一半失败文件就废了。

  3. **dry-run 是写入层自己的事。**
     不依赖调用方"记得判断"。全局闸门在 write/backup 里拦，这样将来加新命令
     即使漏判也改不坏东西。

  4. **备份只存未被我们改过的原件。**
     二次 apply 时如果把注入版当原档存下来，revert 就会"还原"成注入内容 ——
     等于什么都没还原，还丢掉了用户的原始文件。
"""

import os
import io
import json
import shutil
import hashlib
import stat
import datetime as _dt


# ════════════════════════════════════════════════════════════════════════
#  全局闸门
# ════════════════════════════════════════════════════════════════════════

class Gate:
    """写入闸门。

    dry_run  —— 预演：所有写操作变成空操作，返回"本应发生的"描述。
    readonly —— 只读入口（status/check）：连日志都不落盘。
    """
    dry_run = False
    readonly = False

    @classmethod
    def enter_dry(cls):
        cls.dry_run = True

    @classmethod
    def exit_dry(cls):
        cls.dry_run = False

    @classmethod
    def enter_readonly(cls):
        cls.readonly = True

    @classmethod
    def reset(cls):
        cls.dry_run = False
        cls.readonly = False


# ════════════════════════════════════════════════════════════════════════
#  读
# ════════════════════════════════════════════════════════════════════════

def read_bytes(path):
    """读原始字节。失败返回 None。"""
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


def read_text(path):
    """严格读 UTF-8 文本。解不开返回 None（原则 1）。

    返回的文本带一个 BOM 标记，由调用方决定写回时是否保留。
    这里不做任何替换、不吞异常 —— 解不开就是解不开。
    """
    raw = read_bytes(path)
    if raw is None:
        return None
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_text_bom(path):
    """返回 (文本, 是否有 BOM)。二进制读入，行尾原样保留。"""
    raw = read_bytes(path)
    if raw is None:
        return None, False
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    if has_bom:
        raw = raw[3:]
    try:
        return raw.decode("utf-8"), has_bom
    except UnicodeDecodeError:
        return None, has_bom


def read_json(path):
    t = read_text(path)
    if t is None:
        return None
    try:
        return json.loads(t)
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════════
#  写
# ════════════════════════════════════════════════════════════════════════

def atomic_write(path, data):
    """先写同目录临时文件，fsync，再 os.replace 顶替（原则 2）。

    返回 True 表示成功。任何一步失败都会清掉临时文件并返回 False ——
    绝不留下半截文件，也绝不动原文件。

    **只读属性处理**：Windows 上 `os.replace` 对一个带 ReadOnly 属性的目标会报
    PermissionError。这在实际场景里很常见 —— 别的工具（如 pojia）会把云记忆文件
    设为只读来防客户端清空。所以这里先清属性、写完再恢复，保持原属性不变。
    注意：必须先删临时文件再 os.replace，否则 ReadOnly 文件删不掉。
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    tmp = os.path.join(d, ".%s.forge-tmp" % os.path.basename(path))
    was_readonly = False
    try:
        if os.path.exists(path):
            was_readonly = not os.access(path, os.W_OK)
            if was_readonly:
                try:
                    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                except Exception:
                    pass
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if was_readonly:
            try:
                os.chmod(path, stat.S_IREAD)
            except Exception:
                pass
        return True
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        # 写失败时也要把只读属性恢复回去，不给用户留异常状态
        if was_readonly:
            try:
                os.chmod(path, stat.S_IREAD)
            except Exception:
                pass
        return False


def write_text(path, text, bom=False, make_dirs=False):
    """写 UTF-8 文本。dry-run 时直接返回 False（原则 3）。

    返回 True = 真写了；False = 被闸门拦下或写入失败。
    """
    if Gate.dry_run:
        return False
    if make_dirs:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    return atomic_write(path, data)


def write_bytes(path, data, make_dirs=False):
    if Gate.dry_run:
        return False
    if make_dirs:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
    return atomic_write(path, data)


# ════════════════════════════════════════════════════════════════════════
#  签名 / 判定
# ════════════════════════════════════════════════════════════════════════

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    raw = read_bytes(path)
    return sha256_bytes(raw) if raw is not None else ""


def short_hash(data, n=12):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return sha256_bytes(data)[:n]


# ════════════════════════════════════════════════════════════════════════
#  备份 / 归档 / 还原
# ════════════════════════════════════════════════════════════════════════

DEFAULT_BAK_SUFFIX = ".forge-bak"

HIST_DIRNAME = "_forge_history"


def hist_dir(root):
    """历史备份目录（按时间戳归档，防止 bak 被覆盖后丢掉原版）。"""
    return os.path.join(root, HIST_DIRNAME)


def archive(src, root, reason=""):
    """把 src 按时间戳归档一份到 root/_forge_history/。"""
    if Gate.dry_run or Gate.readonly:
        return None
    if not os.path.isfile(src):
        return None
    d = hist_dir(root)
    try:
        os.makedirs(d, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = os.path.basename(src)
        if reason:
            stem = "%s.%s" % (stem, reason)
        dst = os.path.join(d, "%s.%s" % (ts, stem))
        n = 1
        while os.path.exists(dst):
            dst = os.path.join(d, "%s.%s.%d" % (ts, stem, n))
            n += 1
        shutil.copy2(src, dst)
        return dst
    except Exception:
        return None


def backup(path, bak_path=None, suffix=None, is_pristine=None, root=None):
    """确保"未打补丁的原件"有一份备份。

    is_pristine(text) -> bool 用来判断当前内容是否还是官方原版。
    三种情形（原则 4）：
      · bak 不存在            -> 备份当前文件
      · bak 存在且当前已注入   -> 什么都不做（保住原始基准）
      · bak 存在但当前是干净的官方版（官方升级覆盖过）-> 归档旧 bak，重建基准

    返回备份路径；被闸门拦下时返回 None。
    """
    if Gate.dry_run:
        return None
    bak = bak_path or (path + (suffix or DEFAULT_BAK_SUFFIX))
    try:
        if bak_path:
            d = os.path.dirname(bak)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)

        if not os.path.exists(bak):
            shutil.copy2(path, bak)
            return bak

        if is_pristine is not None:
            cur = read_text(path)
            if cur is not None and is_pristine(cur):
                old = read_text(bak)
                if old is not None and old != cur:
                    archive(bak, root or os.path.dirname(path), "superseded")
                    shutil.copy2(path, bak)
        return bak
    except Exception:
        return bak


def restore(path, bak_path=None, suffix=None):
    """从备份还原。原样复制回去，不重排、不重编码。"""
    if Gate.dry_run:
        return False
    bak = bak_path or (path + (suffix or DEFAULT_BAK_SUFFIX))
    if not os.path.exists(bak):
        return False
    raw = read_bytes(bak)
    if raw is None:
        return False
    return atomic_write(path, raw)


def has_backup(path, bak_path=None, suffix=None):
    bak = bak_path or (path + (suffix or DEFAULT_BAK_SUFFIX))
    return os.path.exists(bak)


# ════════════════════════════════════════════════════════════════════════
#  行尾风格
# ════════════════════════════════════════════════════════════════════════

def detect_nl(text):
    """探测换行风格：返回 '\\r\\n' 或 '\\n'。"""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def match_nl(text, target_nl):
    """把 text 的换行统一成 target_nl。"""
    body = text.replace("\r\n", "\n")
    return body if target_nl == "\n" else body.replace("\n", target_nl)


# ════════════════════════════════════════════════════════════════════════
#  小工具
# ════════════════════════════════════════════════════════════════════════

def expand(p):
    return os.path.expanduser(os.path.expandvars(p))


def ensure_dir(p):
    if Gate.dry_run:
        return
    if p and not os.path.isdir(p):
        os.makedirs(p, exist_ok=True)


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % n)
        n /= 1024.0
    return str(n)


def file_stamp(path):
    try:
        st = os.stat(path)
        return "%s  %s" % (
            _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            human_size(st.st_size))
    except Exception:
        return "-"
