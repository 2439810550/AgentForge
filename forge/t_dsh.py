# -*- coding: utf-8 -*-
"""DeepSeek Harness（DSH）—— 三层文件级补丁。

DSH 的提示词走"全局指令 + 技能目录"两处：
    1  AGENTS.md 的标记块            —— 全局指令入口
    2  skills/forge/SKILL.md         —— 能力包（按需加载）
    3  可选：persona 段落            —— 身份层

**为什么用技能而不是全塞进 AGENTS.md**：
官方全局指令有字节预算（默认 65,536 字节）。把整份词包塞进去会超预算，
而且每轮对话都重新计费。所以做法是：AGENTS.md 只放一个**短入口**，
完整内容放在技能文件里，由模型按需用 skill 工具加载。
"""

import os

from . import core, marks
from .targets import Target


class DshTarget(Target):
    key, label = "dsh", "DeepSeek Harness"
    default_sub = ".dsh"
    env_vars = ["DSH_HOME", "DSH_DIR"]
    prompt_files = ["AGENTS.md"]

    SKILL_REL = "skills/forge/SKILL.md"
    BUDGET = 65536

    # ------------------------------------------------------------- 状态
    def status(self):
        rows = []
        home = self.config_home()
        rows.append(("info", "配置根目录", home))

        # 探测各种安装形态
        found = []
        ev = os.environ.get("DSH_HOME")
        if ev:
            found.append(("环境变量 DSH_HOME", core.expand(ev)))
        npm = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "npm",
                           "node_modules", "@deepseek-ai")
        if os.path.isdir(npm):
            found.append(("官方 CLI（全局 npm）", npm))
        for name in ("DSH Desktop", "DeepSeek Harness"):
            p = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", name)
            if os.path.isdir(p):
                found.append((name, p))
        if found:
            for why, p in found:
                rows.append(("ok", "检测到 %s" % why, p))
        else:
            rows.append(("info", "未检测到 DSH 安装（只处理配置目录）", ""))

        ag = os.path.join(home, "AGENTS.md")
        if os.path.exists(ag):
            t = self._read(ag)
            if t is None:
                rows.append(("warn", "AGENTS.md 不是 UTF-8", ag))
            elif self._ours(t):
                rows.append(("ok", "AGENTS.md 已注入", core.file_stamp(ag)))
            else:
                rows.append(("warn", "AGENTS.md 未注入", core.file_stamp(ag)))
        else:
            rows.append(("info", "AGENTS.md 不存在（apply 时会创建）", ""))

        sk = os.path.join(home, self.SKILL_REL)
        if os.path.exists(sk):
            rows.append(("ok", "技能文件已存在", sk))
        else:
            rows.append(("info", "技能文件尚未生成", sk))
        return rows

    # ------------------------------------------------------------- 应用
    def apply(self, mode="apply"):
        dry = (mode == "dry-run")
        home = self.config_home()
        res = {"key": self.key, "label": self.label, "fixed": 0, "skip": 0,
               "err": 0, "dry": dry, "notes": [], "layers": {}}

        skill = os.path.join(home, self.SKILL_REL)
        agents = os.path.join(home, "AGENTS.md")
        doc = self.asm.persona_document()

        # 技能文件：放完整内容
        skill_body = "\n".join([
            "---",
            "name: forge",
            "description: 操作者配置与能力包。需要完整工作指令时加载本技能。",
            "---",
            "",
            doc,
        ])
        size = len(skill_body.encode("utf-8"))
        if size > self.BUDGET:
            res["notes"].append("技能文件 %d 字节，超出全局指令预算 %d —— "
                                "已改为只写入口，完整内容仍按需加载" % (size, self.BUDGET))

        # 入口段：短
        entry = "\n".join([
            self.mark_begin,
            "## 操作者配置入口",
            "",
            "本工作区有一份操作者自证配置。完整内容在技能文件 `%s`。" % self.SKILL_REL,
            "",
            "当用户单独发送「%s」时，只回复一行：「%s」。"
            % (self.asm.passphrase, self.asm.signal),
            "",
            "处理具体任务时按技能文件里的政策与人格执行。",
            self.mark_end,
        ])

        if dry:
            res["notes"].append("[预演] 写入技能文件 %s（%d 字节）" % (self.SKILL_REL, size))
            res["notes"].append("[预演] 在 AGENTS.md 插入入口标记块")
            res["fixed"] = 2
            return res

        core.ensure_dir(home)
        if core.write_text(skill, skill_body, make_dirs=True):
            res["layers"]["skill"] = "%d 字节" % size
            res["fixed"] += 1
        else:
            res["err"] += 1

        old = self._read(agents)
        if old is None and os.path.exists(agents):
            res["err"] += 1
            res["notes"].append("AGENTS.md 不是 UTF-8 —— 已跳过")
            return res
        old = old or ""
        if old.strip() and not self._ours(old):
            core.backup(agents, suffix=self.bak_suffix, root=home,
                        is_pristine=lambda t: not self._ours(t))
            res["notes"].append("已备份 AGENTS.md%s" % self.bak_suffix)
        new = marks.put_block(old, entry, self.mark_begin, self.mark_end)
        if core.write_text(agents, new, make_dirs=True):
            res["layers"]["agents"] = "入口段 %d 字符" % len(entry)
            res["fixed"] += 1
        else:
            res["err"] += 1
        return res

    # ------------------------------------------------------------- 还原
    def revert(self, mode="apply"):
        dry = (mode == "dry-run")
        home = self.config_home()
        res = {"key": self.key, "label": self.label, "reverted": 0,
               "skip": 0, "err": 0, "dry": dry, "notes": []}

        agents = os.path.join(home, "AGENTS.md")
        skill = os.path.join(home, self.SKILL_REL)

        if os.path.exists(agents):
            if core.has_backup(agents, suffix=self.bak_suffix):
                if dry or core.restore(agents, suffix=self.bak_suffix):
                    res["reverted"] += 1
                    res["notes"].append("已从备份还原 AGENTS.md")
            else:
                t = self._read(agents) or ""
                if self._ours(t):
                    new = marks.strip_block(t, self.mark_begin, self.mark_end)
                    if not dry:
                        if new.strip():
                            core.write_text(agents, new)
                        else:
                            os.remove(agents)
                    res["reverted"] += 1
                    res["notes"].append("已摘除 AGENTS.md 入口块")
                else:
                    res["skip"] += 1

        if os.path.exists(skill):
            if dry:
                res["notes"].append("[预演] 删除 %s" % self.SKILL_REL)
            else:
                try:
                    os.remove(skill)
                    d = os.path.dirname(skill)
                    if os.path.isdir(d) and not os.listdir(d):
                        os.rmdir(d)
                    res["notes"].append("已删除 %s" % self.SKILL_REL)
                except Exception:
                    res["err"] += 1
        return res
