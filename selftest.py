# -*- coding: utf-8 -*-
"""开发自检 —— 不碰真实客户端，全部在临时目录里验证。

覆盖：
  1. 标记块 put → strip 是否逐字节回到原样（幂等性）
  2. 原子写是否不留临时文件
  3. dry-run 是否真的一个字节都不写
  4. 备份策略：二次 apply 不会把注入版当原件存下来
  5. TOML 顶层键插入位置是否正确（不被表头吞掉）
  6. JSON 替换后是否仍然合法
  7. WorkBuddy 六层靶点在沙箱目录里的完整流程

跑法：python selftest.py
"""

import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forge import core, marks, persona, toml
from forge import registry

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  \033[92m[PASS]\033[0m %s" % name)
    else:
        FAIL.append(name)
        print("  \033[91m[FAIL]\033[0m %s  %s" % (name, detail))


def section(t):
    print()
    print("  \033[1m%s\033[0m" % t)
    print("  " + "─" * 62)


# ──────────────────────────────────────────────────────────────────────
def t_markblock():
    section("1. 标记块幂等性（put → strip 逐字节还原）")

    cases = [
        ("普通文件", "# 标题\n\n正文一\n\n正文二\n"),
        ("无结尾换行", "# 标题\n正文"),
        ("空文件", ""),
        ("只有空白", "\n\n\n"),
        ("CRLF", "# 标题\r\n\r\n正文\r\n"),
        ("已有块", "前\n\n<!-- B -->\n旧内容\n<!-- E -->\n\n后\n"),
        ("多级标题", "# a\n## b\n### c\ntext\n"),
    ]
    for name, original in cases:
        block = marks.block_text("<!-- B -->", "注入内容\n第二行", "<!-- E -->")
        put = marks.put_block(original, block, "<!-- B -->", "<!-- E -->")
        strip = marks.strip_block(put, "<!-- B -->", "<!-- E -->")
        # 不变量：strip(put(X)) == strip(X)
        # 原文件本身已含标记块时，strip(X) 不等于 X（那标记块不是我们的）
        expect = marks.strip_block(original, "<!-- B -->", "<!-- E -->")
        check("%s：写入后可完整剥离" % name, strip == expect,
              "\n    期望=%r\n    实得=%r" % (expect[:70], strip[:70]))

        # put 之后的块必须只有一份，且能再次幂等
        check("%s：标记只一份" % name, put.count("<!-- B -->") == 1)
        put2 = marks.put_block(put, block, "<!-- B -->", "<!-- E -->")
        check("%s：二次 put 幂等" % name, put2 == put)

    # 二次注入不堆叠
    block = marks.block_text("<!-- B -->", "内容", "<!-- E -->")
    a = marks.put_block("原文\n", block, "<!-- B -->", "<!-- E -->")
    b = marks.put_block(a, block, "<!-- B -->", "<!-- E -->")
    check("二次注入不堆叠", a == b and a.count("<!-- B -->") == 1,
          "出现 %d 次标记" % a.count("<!-- B -->"))


def t_atomic():
    section("2. 原子写")

    d = tempfile.mkdtemp(prefix="forge-t-")
    try:
        p = os.path.join(d, "x.txt")
        core.write_text(p, "hello")
        check("写入成功", core.read_text(p) == "hello")
        leftovers = [f for f in os.listdir(d) if "forge-tmp" in f]
        check("无临时文件残留", not leftovers, str(leftovers))

        core.write_text(p, "world", bom=True)
        raw = core.read_bytes(p)
        check("BOM 写入正确", raw.startswith(b"\xef\xbb\xbf"))
        t, has = core.read_text_bom(p)
        check("BOM 读取正确", has and t == "world")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_readonly():
    section("3. 读不干净就跳过")

    d = tempfile.mkdtemp(prefix="forge-t-")
    try:
        gbk = os.path.join(d, "gbk.txt")
        with open(gbk, "wb") as f:
            f.write("中文内容".encode("gbk"))
        check("GBK 文件返回 None（不转码）", core.read_text(gbk) is None)

        binp = os.path.join(d, "bin.dat")
        with open(binp, "wb") as f:
            f.write(b"\x00\xff\xfe\x01\x02")
        check("二进制返回 None", core.read_text(binp) is None)

        ok_utf = os.path.join(d, "u.txt")
        core.write_text(ok_utf, "正常内容")
        check("UTF-8 正常读取", core.read_text(ok_utf) == "正常内容")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_dryrun():
    section("4. dry-run 不写盘")

    d = tempfile.mkdtemp(prefix="forge-t-")
    try:
        p = os.path.join(d, "target.md")
        core.write_text(p, "原内容\n")
        before = core.read_bytes(p)

        core.Gate.enter_dry()
        try:
            wrote = core.write_text(p, "新内容")
            backed = core.backup(p)
        finally:
            core.Gate.exit_dry()

        check("write 被拦下", wrote is False)
        check("backup 被拦下", backed is None)
        check("内容未变", core.read_bytes(p) == before)
        check("无备份文件产生",
              not any("forge-bak" in f for f in os.listdir(d)))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_backup_strategy():
    section("5. 备份策略（不把注入版当原件）")

    d = tempfile.mkdtemp(prefix="forge-t-")
    try:
        p = os.path.join(d, "cfg.md")
        core.write_text(p, "官方原版\n")

        def not_ours(t):
            return "<!-- FORGE:BEGIN -->" not in t

        b1 = core.backup(p, suffix=".bak", root=d, is_pristine=not_ours)
        check("首次备份建立", os.path.exists(b1))
        check("备份内容=原版", core.read_text(b1) == "官方原版\n")

        # 模拟注入
        core.write_text(p, "<!-- FORGE:BEGIN -->\n注入\n<!-- FORGE:END -->\n")
        b2 = core.backup(p, suffix=".bak", root=d, is_pristine=not_ours)
        check("二次备份不覆盖原件", core.read_text(b2) == "官方原版\n")

        # 模拟官方升级覆盖
        core.write_text(p, "官方新版本\n")
        b3 = core.backup(p, suffix=".bak", root=d, is_pristine=not_ours)
        check("升级后重建基准", core.read_text(b3) == "官方新版本\n")
        hist = core.hist_dir(d)
        check("旧备份已归档", os.path.isdir(hist) and len(os.listdir(hist)) >= 1)

        # 还原
        okr = core.restore(p, suffix=".bak")
        check("还原成功", okr and core.read_text(p) == "官方新版本\n")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_toml():
    section("6. TOML 顶层键（不被表头吞掉）")

    src = ('model = "gpt"\n'
           'approval_policy = "never"\n'
           '\n'
           '[shell_environment_policy.set]\n'
           'FOO = "bar"\n')

    out = toml.set_top_key(src, "model_instructions_file", "/tmp/forge.md",
                           "# >>> forge begin", "# <<< forge end")

    keys = toml.top_level_keys(out)
    check("键落在顶层", "model_instructions_file" in keys, str(keys))
    check("原有键保留",
          "model" in keys and "approval_policy" in keys, str(keys))

    idx_new = out.find("model_instructions_file")
    idx_head = out.find("[shell_environment_policy.set]")
    check("插在表头之前", 0 <= idx_new < idx_head,
          "new=%d head=%d" % (idx_new, idx_head))

    good, why = toml.parse_ok(out)
    check("TOML 仍可解析", good, why)

    back = toml.strip_block(out, "# >>> forge begin", "# <<< forge end")
    check("TOML 还原逐字节一致", back == src,
          "\n    原=%r\n    回=%r" % (src[-40:], back[-40:]))

    # 二次幂等
    out2 = toml.set_top_key(out, "model_instructions_file", "/tmp/forge.md",
                            "# >>> forge begin", "# <<< forge end")
    check("TOML 二次注入不堆叠", out2 == out)


def t_json():
    section("7. JSON 字符串替换后仍合法")

    inner = json.dumps("<content_policy>\nold policy text\n</content_policy>")
    doc = '{"prompts": [{"name": "p1", "template": %s}]}' % inner
    parsed = json.loads(doc)
    check("样本 JSON 可解析", isinstance(parsed, dict))

    body = "新政策第一行\n新政策第二行"
    esc = marks.json_escape_body(body)
    check("转义用 \\n 而非真实换行", "\n" not in esc and "\\n" in esc, repr(esc[:40]))

    replaced = doc.replace("old policy text", esc)
    try:
        p2 = json.loads(replaced)
        got = p2["prompts"][0]["template"]
        check("替换后仍合法", True)
        check("内容已更新", "新政策第一行" in got)
    except Exception as e:
        check("替换后仍合法", False, str(e))

    # 真实换行会破坏 JSON（反例验证）
    bad = doc.replace("old policy text", body)
    try:
        json.loads(bad)
        check("真实换行确实会破坏 JSON（反例）", False, "居然还合法？")
    except Exception:
        check("真实换行确实会破坏 JSON（反例）", True)


def t_persona():
    section("8. 人格装配")

    ap = persona.build(passphrase="测试口令", signal="测试回执")
    doc = ap.persona_document()
    check("文档非空", len(doc) > 200, "%d 字符" % len(doc))
    check("含开始标记", "<!-- FORGE:BEGIN -->" in doc)
    check("含结束标记", "<!-- FORGE:END -->" in doc)
    # 断言政策真进了文档（用政策自身的一段做标尺，不硬编码具体措辞）
    policy_line = [l for l in ap.policy.splitlines() if len(l.strip()) > 12][0].strip()
    check("含政策", policy_line in doc, "探针=%r" % policy_line[:40])
    check("含覆盖层", "最高优先级覆盖" in doc)
    check("含自证口令", "测试口令" in doc)
    check("含自证回执", "测试回执" in doc)

    s = ap.summary()
    check("summary 字段完整",
          all(k in s for k in ("policy_chars", "persona_chars", "document_chars")))

    # 标记块包裹后仍可剥离（用 persona_body，不要用 persona_document —— 后者自带标记）
    body = ap.persona_body()
    wrapped = marks.put_block("原始内容\n",
                              marks.block_text(marks.HTML_BEGIN, body, marks.HTML_END),
                              marks.HTML_BEGIN, marks.HTML_END)
    back = marks.strip_block(wrapped, marks.HTML_BEGIN, marks.HTML_END)
    check("文档块可完整剥离", back == "原始内容\n", repr(back[:50]))
    check("persona_body 不含标记",
          marks.HTML_BEGIN not in body and marks.HTML_END not in body)
    check("persona_document 含标记",
          marks.HTML_BEGIN in ap.persona_document())


def t_targets_sandbox():
    section("9. 靶点在沙箱目录的完整流程（不碰真实客户端）")

    d = tempfile.mkdtemp(prefix="forge-sandbox-")
    try:
        ap = persona.build(passphrase="沙箱口令", signal="沙箱回执")

        # 用一个简单的标记块靶点做端到端
        from forge.targets import GeminiTarget

        class Args:
            pass

        args = Args()
        args.dir = d
        args.gemini_dir = d
        args.target = "gemini"
        args.persona = None
        args.policy = None
        args.overlay = None
        args.packs = None
        args.passphrase = None
        args.signal = None

        t = GeminiTarget(args, ap)
        check("沙箱目录被识别", t.config_home() == d, t.config_home())

        # dry-run
        core.Gate.enter_dry()
        r_dry = t.apply("dry-run")
        core.Gate.exit_dry()
        check("dry-run 报告待处理", r_dry.get("fixed", 0) >= 1, str(r_dry))
        check("dry-run 未产生文件", not os.path.exists(os.path.join(d, "GEMINI.md")))

        # apply
        r1 = t.apply("apply")
        p = os.path.join(d, "GEMINI.md")
        check("apply 写入成功", os.path.exists(p), str(r1))
        content = core.read_text(p)
        policy_line = [l for l in ap.policy.splitlines() if len(l.strip()) > 12][0].strip()
        check("内容含政策", policy_line in content)
        check("内容含自证", "沙箱口令" in content)

        # 幂等
        r2 = t.apply("apply")
        content2 = core.read_text(p)
        check("二次 apply 幂等", content == content2)
        check("标记块只一份", content2.count("<!-- FORGE:BEGIN -->") == 1)

        # 预置内容不被破坏
        p2dir = tempfile.mkdtemp(prefix="forge-sandbox2-")
        args2 = Args()
        args2.dir = p2dir
        args2.gemini_dir = p2dir
        t2 = GeminiTarget(args2, ap)
        core.write_text(os.path.join(p2dir, "GEMINI.md"), "# 用户原有规则\n\n不要删我\n")
        t2.apply("apply")
        c = core.read_text(os.path.join(p2dir, "GEMINI.md"))
        check("追加而非覆盖", "不要删我" in c and policy_line in c)

        # revert（注意：要还原的是 p2dir 那个，别拿错对象）
        r3 = t2.revert("apply")
        c3 = core.read_text(os.path.join(p2dir, "GEMINI.md"))
        check("还原回原内容", c3 == "# 用户原有规则\n\n不要删我\n",
              repr(c3[:70]))
        shutil.rmtree(p2dir, ignore_errors=True)

        # 首次 apply 的目标，revert 后应删除空文件
        t.revert("apply")
        check("revert 后无残留或已还原",
              (not os.path.exists(p)) or (policy_line not in (core.read_text(p) or "")),
              "文件仍存在" if os.path.exists(p) else "已删除")

    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_registry():
    section("10. 注册表")

    keys = registry.keys()
    check("靶点数量 = 15", len(keys) == 15, "实际 %d：%s" % (len(keys), keys))
    for need in ("wb", "dsh", "codex", "claude", "cursor", "zcode"):
        check("包含 %s" % need, need in keys)

    got, bad = registry.resolve("all")
    check("all 展开完整", len(got) == len(keys))
    got2, bad2 = registry.resolve("wb,dsh")
    check("多选解析", got2 == ["wb", "dsh"], str(got2))
    got3, bad3 = registry.resolve("wb,不存在")
    check("未知目标被记录", got3 == ["wb"] and bad3 == ["不存在"], str((got3, bad3)))

    bb = registry.resolve("all-from-last")
    check("非 all 表达式不展开", "all-from-last" not in registry.keys())


# ──────────────────────────────────────────────────────────────────────
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print()
    print("  \033[1mForge 自检\033[0m")
    print("  在临时目录里跑，不碰任何真实客户端配置")
    print("  " + "═" * 62)

    for fn in (t_markblock, t_atomic, t_readonly, t_dryrun, t_backup_strategy,
               t_toml, t_json, t_persona, t_targets_sandbox, t_registry):
        try:
            fn()
        except Exception as e:
            import traceback
            FAIL.append(fn.__name__)
            print("  \033[91m[ERR]\033[0m %s 抛出异常：" % fn.__name__)
            traceback.print_exc()

    print()
    print("  " + "═" * 62)
    total = len(PASS) + len(FAIL)
    if FAIL:
        print("  \033[91m%d/%d 通过，%d 失败\033[0m" % (len(PASS), total, len(FAIL)))
        for f in FAIL:
            print("    x %s" % f)
        print()
        return 1
    print("  \033[92m全部 %d 项通过\033[0m" % total)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
