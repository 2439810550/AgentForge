# -*- coding: utf-8 -*-
"""Codex —— 走官方 config.toml 的 model_instructions_file。

**为什么不用 AGENTS.md**：
AGENTS.md 是「追加的上下文」，会被产品提示词、项目级 AGENTS.md 一起稀释。
而 model_instructions_file 是官方提供的**替换模型指令**入口 —— 直接顶掉
产品自带的指令层，效果比追加彻底得多。这也是社区通行做法。

**只加一行，外面套我们自己的注释标记**，用户其余配置逐字节不动。
"""

import os

from . import core, toml
from .targets import Target


class CodexTarget(Target):
    key, label = "codex", "Codex"
    default_sub, env_vars = ".codex", ["CODEX_HOME", "CODEX_DIR"]
    prompt_files = ["config.toml"]
    bak_suffix = core.DEFAULT_BAK_SUFFIX

    MARK_BEGIN = "# >>> forge begin"
    MARK_END = "# <<< forge end"

    INSTR_REL = "forge-instructions.md"

    @property
    def mark_begin(self):
        return self.MARK_BEGIN

    @property
    def mark_end(self):
        return self.MARK_END

    def instr_path(self):
        return os.path.join(self.config_home(), self.INSTR_REL)

    # ------------------------------------------------------------- 状态
    def status(self):
        rows = []
        home = self.config_home()
        if not os.path.isdir(home):
            rows.append(("info", "配置目录不存在（未安装）", home))
            return rows
        rows.append(("info", "配置目录", home))

        cfg = os.path.join(home, "config.toml")
        if not os.path.exists(cfg):
            rows.append(("info", "config.toml 不存在（apply 时会创建）", ""))
        else:
            t = self._read(cfg)
            if t is None:
                rows.append(("warn", "config.toml 不是 UTF-8，已跳过", cfg))
            elif self.MARK_BEGIN in t:
                rows.append(("ok", "config.toml 已注入 model_instructions_file",
                             core.file_stamp(cfg)))
            else:
                rows.append(("warn", "config.toml 未注入", core.file_stamp(cfg)))

        ip = self.instr_path()
        if os.path.exists(ip):
            rows.append(("ok", "指令文件已存在", ip))
        else:
            rows.append(("info", "指令文件尚未生成", ip))

        if core.has_backup(cfg, suffix=self.bak_suffix):
            rows.append(("info", "  └ 有备份", cfg + self.bak_suffix))
        return rows

    # ------------------------------------------------------------- 应用
    def apply(self, mode="apply"):
        dry = (mode == "dry-run")
        home = self.config_home()
        res = {"key": self.key, "label": self.label, "fixed": 0, "skip": 0,
               "err": 0, "dry": dry, "notes": []}

        if not os.path.isdir(home):
            if dry:
                res["notes"].append("将创建 %s 并写入配置" % home)
                res["fixed"] = 1
                return res
            core.ensure_dir(home)
        res["notes"].append("配置目录 %s" % home)

        cfg = os.path.join(home, "config.toml")
        ip = self.instr_path()
        doc = self.asm.persona_document()

        old = self._read(cfg)
        if old is None and os.path.exists(cfg):
            res["err"] += 1
            res["notes"].append("config.toml 不是 UTF-8 —— 已跳过，未改动")
            return res
        old = old or ""

        before_keys = toml.top_level_keys(old)

        if dry:
            res["notes"].append("[预演] 写入指令文件 %s（%d 字符）" % (self.INSTR_REL, len(doc)))
            res["notes"].append("[预演] 在 config.toml 顶层键区插入 model_instructions_file")
            if self.MARK_BEGIN in old:
                res["notes"].append("[预演] 已有标记块 → 整块替换")
            res["fixed"] = 1
            return res

        # 备份 config.toml（只备份还没被我们改过的）
        if os.path.exists(cfg) and old.strip() and self.MARK_BEGIN not in old:
            core.backup(cfg, suffix=self.bak_suffix, root=home,
                        is_pristine=lambda t: self.MARK_BEGIN not in t)
            res["notes"].append("已备份 config.toml%s" % self.bak_suffix)

        ok = core.write_text(ip, doc, make_dirs=True)
        if not ok:
            res["err"] += 1
            res["notes"].append("写入指令文件失败")
            return res
        res["notes"].append("已写入 %s（%d 字符）" % (self.INSTR_REL, len(doc)))

        new = toml.set_top_key(old, "model_instructions_file", ip,
                               self.MARK_BEGIN, self.MARK_END)

        # 写盘后真解析复核 —— 不对就回滚，绝不让用户拿到 Codex 读不了的配置
        good, why = toml.parse_ok(new)
        if not good:
            res["err"] += 1
            res["notes"].append("写后 TOML 校验失败（%s）—— 已放弃写入，原文件未动" % why)
            return res

        after_keys = toml.top_level_keys(new)
        if "model_instructions_file" not in after_keys:
            res["err"] += 1
            res["notes"].append("键没落在顶层（会被 TOML 表头吞掉）—— 已放弃写入")
            return res

        if core.write_text(cfg, new):
            res["fixed"] += 1
            added = [k for k in after_keys if k not in before_keys]
            res["notes"].append("config.toml 已更新（顶层新增：%s）"
                                % (", ".join(added) or "无"))
        else:
            res["err"] += 1
            res["notes"].append("写入 config.toml 失败")
        return res

    # ------------------------------------------------------------- 还原
    def revert(self, mode="apply"):
        dry = (mode == "dry-run")
        home = self.config_home()
        res = {"key": self.key, "label": self.label, "reverted": 0,
               "skip": 0, "err": 0, "dry": dry, "notes": []}
        cfg = os.path.join(home, "config.toml")
        ip = self.instr_path()

        if os.path.exists(cfg) and core.has_backup(cfg, suffix=self.bak_suffix):
            if dry:
                res["notes"].append("[预演] 从备份还原 config.toml")
            elif core.restore(cfg, suffix=self.bak_suffix):
                res["notes"].append("已从备份还原 config.toml（逐字节）")
            else:
                res["err"] += 1
            res["reverted"] += 1
        elif os.path.exists(cfg):
            t = self._read(cfg) or ""
            if self.MARK_BEGIN in t:
                new = toml.strip_block(t, self.MARK_BEGIN, self.MARK_END)
                if dry:
                    res["notes"].append("[预演] 摘除标记块")
                else:
                    core.write_text(cfg, new)
                    res["notes"].append("已摘除 model_instructions_file 那一行")
                res["reverted"] += 1
            else:
                res["skip"] += 1

        if os.path.exists(ip):
            if dry:
                res["notes"].append("[预演] 删除 %s" % self.INSTR_REL)
            else:
                try:
                    os.remove(ip)
                    res["notes"].append("已删除 %s" % self.INSTR_REL)
                except Exception:
                    res["err"] += 1
        return res
