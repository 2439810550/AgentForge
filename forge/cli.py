# -*- coding: utf-8 -*-
"""Forge —— 命令行入口。

    python -m forge status              只读：列出所有靶点状态
    python -m forge scan                只读：谁装了、谁注入了
    python -m forge dry-run --target wb 预演，不写盘
    python -m forge apply --target wb   真打
    python -m forge revert --target wb  还原
    python -m forge persona             打印装配后的完整文档（用于检查）
    python -m forge packs               列出可用词包
"""

import argparse
import os
import sys

from . import core, persona, registry

# ── 颜色 ────────────────────────────────────────────────────────────────
_USE_COLOR = sys.stdout.isatty() or os.environ.get("FORGE_COLOR") == "1"


def _c(code, s):
    if not _USE_COLOR:
        return s
    return "\033[%sm%s\033[0m" % (code, s)


def ok(s):     return _c("92", s)
def warn(s):   return _c("93", s)
def info(s):   return _c("96", s)
def dim(s):    return _c("90", s)
def err(s):    return _c("91", s)
def bold(s):   return _c("1", s)


def banner():
    print()
    print(bold("  ╔══════════════════════════════════════════════════════════════╗"))
    print(bold("  ║  Forge  ·  多客户端提示词注入工具                            ║"))
    print(bold("  ╚══════════════════════════════════════════════════════════════╝"))


# ── 命令实现 ────────────────────────────────────────────────────────────

def cmd_scan(args, asm):
    banner()
    print("  %s\n" % dim("扫描本机已安装的客户端（只读）"))
    keys_, bad = registry.resolve(args.target)
    if bad:
        print("  %s 未知目标：%s" % (warn("[!]"), ", ".join(bad)))
        print()

    n_inst = n_inj = 0
    for k in keys_:
        cls = registry.get(k)
        t = cls(args, asm)
        installed, rows, injected = t.detect()
        if installed:
            n_inst += 1
        if injected:
            n_inj += 1
        mark = "●" if injected else ("○" if installed else "·")
        col = ok if injected else (warn if installed else dim)
        print("  %s %-12s %-22s %s" % (
            col(mark), k, cls.label,
            ok("已注入") if injected else (warn("已安装·未注入") if installed else dim("未检测到"))))
    print()
    print("  %s" % dim("已安装 %d 个，已注入 %d 个" % (n_inst, n_inj)))
    print()


def cmd_status(args, asm):
    banner()
    keys_, bad = registry.resolve(args.target)
    if bad:
        print("  %s 未知目标：%s\n" % (warn("[!]"), ", ".join(bad)))
    for k in keys_:
        cls = registry.get(k)
        t = cls(args, asm)
        rows = t.status()
        print("  %s %s" % (bold(cls.label), dim("(%s)" % k)))
        print("  " + dim("─" * 66))
        for level, note, detail in rows:
            tag = {"ok": ok("[ok]"), "warn": warn("[--]"),
                   "err": err("[!!]"), "info": dim("[  ]")}.get(level, dim("[  ]"))
            line = "    %s %s" % (tag, note)
            if detail:
                line += "  " + dim(detail)
            print(line)
        print()


def cmd_dry_run(args, asm):
    banner()
    print("  %s\n" % warn("预演模式：只列出将要改什么，一个字节都不写盘"))
    core.Gate.enter_dry()
    try:
        _run_action(args, asm, "dry-run")
    finally:
        core.Gate.exit_dry()


def cmd_apply(args, asm):
    banner()
    _run_action(args, asm, "apply")


def cmd_revert(args, asm):
    banner()
    print("  %s\n" % warn("还原：按备份逐字节回退"))
    _run_action(args, asm, "revert")


def _run_action(args, asm, mode):
    keys_, bad = registry.resolve(args.target)
    if bad:
        print("  %s 未知目标：%s" % (err("[!]"), ", ".join(bad)))
        return 2

    tot = {"fixed": 0, "reverted": 0, "err": 0, "skip": 0}

    for k in keys_:
        cls = registry.get(k)
        t = cls(args, asm)
        installed, _rows, _inj = t.detect()

        if mode == "revert":
            res = t.revert(mode)
        else:
            t2 = cls(args, asm)
            if k == "wb":
                res = t2.apply(mode, full=getattr(args, "full", False))
            else:
                res = t2.apply(mode)

        if res.get("skip") and not res.get("fixed") and not res.get("reverted"):
            continue

        print("  %s %s" % (bold(cls.label), dim("(%s)" % k)))
        print("  " + dim("─" * 66))
        for n in res.get("notes", []):
            print("    %s" % n)
        for lname, lv in (res.get("layers") or {}).items():
            print("    %s %s" % (dim("·"), "%s：%s" % (lname, lv)))
        if mode == "revert":
            print("    %s 还原 %d，跳过 %d，错误 %d" % (
                ok("→") if res.get("reverted") else dim("→"),
                res.get("reverted", 0), res.get("skip", 0), res.get("err", 0)))
            tot["reverted"] += res.get("reverted", 0)
        else:
            print("    %s 处理 %d，错误 %d" % (
                ok("→") if res.get("fixed") else dim("→"),
                res.get("fixed", 0), res.get("err", 0)))
            tot["fixed"] += res.get("fixed", 0)
        tot["err"] += res.get("err", 0)
        tot["skip"] += res.get("skip", 0)
        print()

    print("  " + dim("─" * 66))
    if mode == "revert":
        print("  %s 共还原 %d 处，错误 %d" % (ok("完成"), tot["reverted"], tot["err"]))
    elif mode == "dry-run":
        print("  %s 共 %d 处待处理（未写盘）" % (info("预演结束"), tot["fixed"]))
    else:
        print("  %s 共处理 %d 处，错误 %d" % (ok("完成"), tot["fixed"], tot["err"]))
    if mode != "dry-run":
        print()
        print("  %s 接下来必须做这三步：" % warn("重要"))
        print("    1. 完全退出目标客户端（含托盘图标，关窗口不够）")
        print("    2. 重新打开客户端")
        print("    3. 新会话里单独发送：  %s" % bold(asm.passphrase))
        print("       预期只回一行：%s" % asm.signal)
    print()
    return 0


def cmd_persona(args, asm):
    banner()
    s = asm.summary()
    print("  装配概览")
    print("  " + dim("─" * 66))
    for k, v in s.items():
        print("    %-16s %s" % (k, v))
    print()
    print("  来源")
    print("  " + dim("─" * 66))
    for k, v in getattr(asm, "sources", {}).items():
        print("    %-16s %s" % (k, v))
    print()
    if args.full_text:
        print(dim("─" * 66))
        print(asm.persona_document())
    else:
        print("  %s" % dim("（加 --full-text 打印完整文档）"))
        print()


def cmd_packs(args, asm):
    banner()
    names = persona.list_packs()
    if not names:
        print("  %s 没有词包。把 .md 放进 %s\n" % (dim("[ ]"), persona.packs_dir()))
        return
    print("  可用词包（%s）" % persona.packs_dir())
    print("  " + dim("─" * 66))
    for n in names:
        got = persona.read_pack(n)
        title = got[0] if got else n
        print("    %-24s %s" % (n, dim(title)))
    print()


def cmd_ui(args, asm):
    from . import ui as ui_mod
    return ui_mod.serve(
        port=getattr(args, "port", 8317) or 8317,
        open_browser=not getattr(args, "no_browser", False),
        host=getattr(args, "host", "127.0.0.1") or "127.0.0.1")


def cmd_launch(args, asm):
    """一键启动：已跑就开浏览器，没跑就后台拉起。"""
    from . import launch
    return launch.launch(
        port=getattr(args, "port", None) or 8317,
        open_browser=not getattr(args, "no_browser", False),
        force_new=getattr(args, "new", False),
        new_window=not getattr(args, "headless", False))


def cmd_menu(args, asm):
    """交互菜单（纯 Python，不依赖批处理）。"""
    from . import menu
    return menu.run()


# ── 参数 ────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(
        prog="forge",
        description="多客户端提示词注入工具（本地、可逆、只改文本）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python -m forge scan
  python -m forge status --target wb
  python -m forge dry-run --target wb
  python -m forge apply --target wb --full
  python -m forge apply --target all
  python -m forge revert --target wb
  python -m forge persona --full-text
  python -m forge packs
""")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--target", "-t", default="all",
                        help="目标：all / wb / dsh / codex / claude …（可逗号分隔）")
        sp.add_argument("--persona", help="人格文件路径")
        sp.add_argument("--policy", help="基础政策文件路径")
        sp.add_argument("--overlay", help="覆盖层文件路径")
        sp.add_argument("--packs", help="启用的词包（逗号分隔）")
        sp.add_argument("--passphrase", help="自证口令")
        sp.add_argument("--signal", help="自证回执")
        return sp

    common(sub.add_parser("scan", help="只读：扫描已安装/已注入"))
    common(sub.add_parser("status", help="只读：逐靶点详细状态"))
    common(sub.add_parser("dry-run", help="预演，不写盘"))
    sp = common(sub.add_parser("apply", help="执行注入"))
    sp.add_argument("--full", action="store_true",
                    help="WorkBuddy 完全模式（含命令闸门与文件保护中和）")
    common(sub.add_parser("revert", help="还原"))
    sp = sub.add_parser("persona", help="打印装配结果")
    sp.add_argument("--persona"); sp.add_argument("--policy")
    sp.add_argument("--overlay"); sp.add_argument("--packs")
    sp.add_argument("--passphrase"); sp.add_argument("--signal")
    sp.add_argument("--full-text", action="store_true")
    common(sub.add_parser("packs", help="列出词包"))
    sp = sub.add_parser("ui", help="启动 Web 控制台（前台）")
    sp.add_argument("--port", type=int, default=8317)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--no-browser", action="store_true")

    sp = sub.add_parser("launch", help="一键启动：已跑就开浏览器，没跑就后台拉起")
    sp.add_argument("--port", type=int, default=8317)
    sp.add_argument("--no-browser", action="store_true", help="不开浏览器")
    sp.add_argument("--new", action="store_true", help="忽略已在跑的实例，另起一个")
    sp.add_argument("--headless", action="store_true", help="不留控制台窗口（纯后台）")

    sub.add_parser("menu", help="交互菜单")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.cmd:
        build_parser().print_help()
        return 0

    packs = None
    if getattr(args, "packs", None):
        packs = [x.strip() for x in args.packs.split(",") if x.strip()]

    asm = persona.build(
        persona_path=getattr(args, "persona", None),
        policy_path=getattr(args, "policy", None),
        overlay_path=getattr(args, "overlay", None),
        packs=packs,
        passphrase=getattr(args, "passphrase", None),
        signal=getattr(args, "signal", None),
    )

    table = {
        "scan": cmd_scan, "status": cmd_status, "dry-run": cmd_dry_run,
        "apply": cmd_apply, "revert": cmd_revert, "persona": cmd_persona,
        "packs": cmd_packs, "ui": cmd_ui, "launch": cmd_launch,
        "menu": cmd_menu,
    }
    fn = table.get(args.cmd)
    if not fn:
        build_parser().print_help()
        return 0

    # status / scan / persona / packs 属只读入口
    if args.cmd in ("status", "scan", "persona", "packs"):
        core.Gate.enter_readonly()
    try:
        return fn(args, asm) or 0
    finally:
        core.Gate.reset()
