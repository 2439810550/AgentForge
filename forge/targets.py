# -*- coding: utf-8 -*-
"""靶点基类与标记块型靶点。

15 个客户端的注入差异，最后归结为三件事：
  · 配置目录在哪（探测 + 环境变量覆盖 + 手动指定）
  · 提示词文件叫什么（有的一个文件，有的多个）
  · 往里面塞什么形态（Markdown 标记块 / TOML 顶层键 / JSON 字符串 / 多层文件）

所以基类只管生命周期：detect → status → apply → revert。
每个子类只声明自己的差异，不重写流程。

**为什么反转必须精确到字节**：
apply 之后的 revert 要能让文件回到"和注入前一模一样"。
如果只是"把标记块删掉"，用户的换行风格、结尾有无换行都会被重排。
所以剥离逻辑必须在"行"这一级做增删（见 marks.py 的说明）。
"""

import os
from . import core, marks, persona

DEFAULT_BAK = core.DEFAULT_BAK_SUFFIX


class Target:
    """靶点基类。子类声明类属性，流程由基类提供。"""

    key = ""
    label = ""
    #: 可覆盖配置目录的环境变量名（按优先级）
    env_vars = []
    #: 默认配置目录（相对用户主目录）
    default_sub = ""
    #: 提示词文件名（相对配置目录）
    prompt_files = []
    #: 备份后缀
    bak_suffix = DEFAULT_BAK
    #: 是否需要 frontmatter（.mdc 类）
    frontmatter = ""
    #: 标记块（子类可覆盖；Codex 用 # 注释，其余用 HTML 注释）
    mark_begin = "<!-- FORGE:BEGIN -->"
    mark_end = "<!-- FORGE:END -->"

    def __init__(self, args=None, asm=None):
        self.args = args
        self.asm = asm or persona.build()
        self.rows = []

    # ---------------------------------------------------------------- 探测
    def override_dir(self):
        """命令行显式指定 > 环境变量 > 默认。"""
        if self.args is not None:
            for a in ("dir", self.key + "_dir"):
                v = getattr(self.args, a, None)
                if v:
                    return core.expand(v)
        for ev in self.env_vars:
            v = os.environ.get(ev)
            if v:
                return core.expand(v)
        return ""

    def config_home(self):
        ov = self.override_dir()
        if ov:
            return ov
        return os.path.join(os.path.expanduser("~"), self.default_sub)

    def exists(self):
        return os.path.isdir(self.config_home())

    # ---------------------------------------------------------------- 状态
    def _read(self, path):
        return core.read_text(path)

    def _ours(self, text):
        return marks.has_block(text, self.mark_begin)

    def status(self):
        """返回 [(级别, 说明, 详情)] 的行列表。级别：ok / warn / info / err"""
        rows = []
        home = self.config_home()
        if not os.path.isdir(home):
            rows.append(("info", "配置目录不存在（未安装或未初始化）", home))
            return rows
        rows.append(("info", "配置目录", home))

        found_any = False
        for rel in self.prompt_files:
            p = os.path.join(home, rel)
            if not os.path.exists(p):
                rows.append(("info", "%s 不存在" % rel, ""))
                continue
            found_any = True
            t = self._read(p)
            if t is None:
                rows.append(("warn", "%s 不是 UTF-8 文本，已跳过" % rel, p))
                continue
            if self._ours(t):
                rows.append(("ok", "%s 已注入" % rel, core.file_stamp(p)))
            else:
                rows.append(("warn", "%s 未注入" % rel, core.file_stamp(p)))
            if core.has_backup(p, suffix=self.bak_suffix):
                rows.append(("info", "  └ 有备份", p + self.bak_suffix))

        if not found_any:
            rows.append(("info", "尚无提示词文件（apply 时会创建）", ""))
        return rows

    def detect(self):
        """返回 (installed, status_rows, injected)。

        injected 必须严格基于「我们的标记」。
        不能用 `any(r[0] == "ok")` —— 那样“检测到安装目录”也会被算成已注入，
        结果是别的工具（如 pojia）注入过的机器被误报成 Forge 注入过。
        """
        installed = os.path.isdir(self.config_home())
        st = self.status()
        injected = self._injected_flag()
        return installed, st, injected

    def _injected_flag(self):
        """默认：任一提示词文件含我们的标记块。"""
        home = self.config_home()
        for rel in self.prompt_files:
            p = os.path.join(home, rel)
            t = core.read_text(p)
            if t and marks.has_block(t, self.mark_begin):
                return True
        return False

    # ---------------------------------------------------------------- 应用
    def _frontmatter(self):
        if not self.frontmatter:
            return ""
        return self.frontmatter.rstrip("\n") + "\n"

    def _merge(self, home, old, doc):
        """默认缝合方式：标记块追加/替换（幂等）。

        ⚠ 只用 persona_body()，不能用 persona_document() —— 后者自带 FORGE 标记，
        外面再包一层就嵌套了，strip 只能剥最外层、留下半截。
        """
        block = marks.block_text(self.mark_begin, self.asm.persona_body(), self.mark_end)
        return marks.put_block(old, block, self.mark_begin, self.mark_end)

    def apply(self, mode="apply"):
        """mode: apply / dry-run"""
        dry = (mode == "dry-run")
        home = self.config_home()
        res = {"key": self.key, "label": self.label, "fixed": 0, "skip": 0,
               "err": 0, "dry": dry, "notes": []}

        if not os.path.isdir(home):
            # 配置目录不存在时：只处理"本工具自己的落点"，不替用户建产品目录
            target = os.path.join(home, self.prompt_files[0]) if self.prompt_files else ""
            if not target:
                res["skip"] = 1
                return res
            if dry:
                res["notes"].append("将创建目录并写入 %s" % target)
                res["fixed"] = 1
                return res
            core.ensure_dir(home)
        else:
            res["notes"].append("配置目录 %s" % home)

        doc = self.asm.persona_document()
        for rel in self.prompt_files:
            p = os.path.join(home, rel)
            old = self._read(p)
            if old is None and os.path.exists(p):
                res["err"] += 1
                res["notes"].append("读不了 %s（非 UTF-8）—— 已跳过，未改动" % rel)
                continue
            old = old or ""
            existed = os.path.exists(p)

            if dry:
                verb = "整块替换" if self._ours(old) else ("追加" if existed else "新建")
                res["notes"].append("[预演] %s → %s（%d 字符）"
                                    % (rel, verb, len(doc)))
                res["fixed"] += 1
                continue

            # 备份：只在"还不是我们写的"且文件已有实际内容时留原件
            if existed and old.strip() and not self._ours(old):
                core.backup(p, suffix=self.bak_suffix, root=home,
                            is_pristine=lambda t: not self._ours(t))
                res["notes"].append("已备份 %s%s" % (rel, self.bak_suffix))

            new = self._merge(home, old, doc)
            if self._frontmatter() and not new.lstrip().startswith("---"):
                new = self._frontmatter() + new
            if core.write_text(p, new, make_dirs=True):
                res["fixed"] += 1
                res["notes"].append("已写入 %s（%d 字符）" % (rel, len(new)))
            else:
                res["err"] += 1
                res["notes"].append("写入失败 %s" % rel)
        return res

    # ---------------------------------------------------------------- 还原
    def revert(self, mode="apply"):
        dry = (mode == "dry-run")
        home = self.config_home()
        res = {"key": self.key, "label": self.label, "reverted": 0,
               "skip": 0, "err": 0, "dry": dry, "notes": []}

        for rel in self.prompt_files:
            p = os.path.join(home, rel)
            if not os.path.exists(p):
                continue
            if core.has_backup(p, suffix=self.bak_suffix):
                if dry:
                    res["notes"].append("[预演] 从备份还原 %s" % rel)
                    res["reverted"] += 1
                    continue
                if core.restore(p, suffix=self.bak_suffix):
                    res["reverted"] += 1
                    res["notes"].append("已从备份还原 %s" % rel)
                else:
                    res["err"] += 1
                continue

            # 没备份：只能把标记块摘掉（文件是我们新建的情形）
            t = self._read(p)
            if t is None:
                res["err"] += 1
                continue
            if not self._ours(t):
                res["skip"] += 1
                continue
            new = marks.strip_block(t, self.mark_begin, self.mark_end)
            if dry:
                res["notes"].append("[预演] 摘除标记块 %s" % rel)
                res["reverted"] += 1
                continue
            if not new.strip():
                try:
                    os.remove(p)
                    res["notes"].append("已删除（内容全是我们写的）%s" % rel)
                except Exception:
                    res["err"] += 1
                    continue
            else:
                core.write_text(p, new)
                res["notes"].append("已摘除标记块 %s" % rel)
            res["reverted"] += 1
        return res


# ════════════════════════════════════════════════════════════════════════
#  声明式靶点：差异只有"目录 + 文件名"
# ════════════════════════════════════════════════════════════════════════

CURSOR_FM = """---
description: 操作者配置（Forge 注入）
globs:
alwaysApply: true
---"""


class GeminiTarget(Target):
    key, label = "gemini", "Gemini CLI"
    default_sub, env_vars = ".gemini", ["GEMINI_DIR", "GEMINI_HOME"]
    prompt_files = ["GEMINI.md"]


class QwenTarget(Target):
    key, label = "qwen", "Qwen Code"
    default_sub, env_vars = ".qwen", ["QWEN_DIR", "QWEN_HOME"]
    prompt_files = ["QWEN.md"]


class IflowTarget(Target):
    key, label = "iflow", "iFlow CLI"
    default_sub, env_vars = ".iflow", ["IFLOW_DIR", "IFLOW_HOME"]
    prompt_files = ["IFLOW.md"]


class TraeTarget(Target):
    key, label = "trae", "Trae"
    default_sub, env_vars = ".trae", ["TRAE_DIR", "TRAE_HOME"]
    prompt_files = ["rules/project_rules.md"]


class CodeBuddyTarget(Target):
    key, label = "codebuddy", "CodeBuddy"
    default_sub, env_vars = ".codebuddy", ["CODEBUDDY_DIR", "CODEBUDDY_HOME"]
    prompt_files = ["CODEBUDDY.md"]


class OpenCodeTarget(Target):
    key, label = "opencode", "OpenCode"
    default_sub, env_vars = ".config/opencode", ["OPENCODE_DIR"]
    prompt_files = ["AGENTS.md"]


class WindsurfTarget(Target):
    key, label = "windsurf", "Windsurf"
    default_sub, env_vars = ".codeium", ["WINDSURF_DIR", "CODEIUM_DIR"]
    prompt_files = ["windsurf/memories/global_rules.md"]


class ClineTarget(Target):
    key, label = "cline", "Cline"
    default_sub, env_vars = "Documents/Cline/Rules", ["CLINE_DIR"]
    prompt_files = ["forge-inject.md"]


class CopilotTarget(Target):
    key, label = "copilot", "GitHub Copilot CLI"
    default_sub, env_vars = ".copilot", ["COPILOT_DIR", "COPILOT_HOME"]
    prompt_files = ["copilot-instructions.md"]


class CursorTarget(Target):
    key, label = "cursor", "Cursor"
    default_sub, env_vars = ".cursor", ["CURSOR_DIR", "CURSOR_HOME"]
    prompt_files = ["rules/forge-inject.mdc"]
    frontmatter = CURSOR_FM


class ClaudeTarget(Target):
    key, label = "claude", "Claude Code"
    default_sub, env_vars = ".claude", ["CLAUDE_DIR", "CLAUDE_CONFIG_DIR"]
    prompt_files = ["CLAUDE.md"]


MARK_TARGETS = [
    GeminiTarget, QwenTarget, IflowTarget, TraeTarget, CodeBuddyTarget,
    OpenCodeTarget, WindsurfTarget, ClineTarget, CopilotTarget,
    CursorTarget, ClaudeTarget,
]


class ZCodeTarget(Target):
    """ZCode（智谱）—— AGENTS.md + Memory 文件双落点。"""
    key, label = "zcode", "ZCode"
    default_sub, env_vars = ".zcode", ["ZCODE_DIR", "ZCODE_HOME"]
    prompt_files = ["AGENTS.md", "ZCODE.md"]


MARK_TARGETS.append(ZCodeTarget)
