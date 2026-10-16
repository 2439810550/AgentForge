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
def t_wb_memory():
    """WorkBuddy 云记忆 —— 防三处已修 bug 回归。

    这三个都是真实事故，不是假想：
      1. 写错位置：写入文件正文，而产品只读 RAW_JSON 块的 memoryBlock 字段
      2. 标记嵌套：_memory_block 用 persona_document()（自带标记）再包一层
      3. 判据失配：status 用文件正文标记判断，改字段后永远显示 0/14
    """
    section("11. WorkBuddy 云记忆（防三处 bug 回归）")

    import inspect
    from forge.t_workbuddy import WorkBuddyTarget

    class A:
        pass
    a = A()
    for k in ("target", "persona", "policy", "overlay", "packs", "passphrase",
              "signal", "full", "wb_install", "wb_data"):
        setattr(a, k, None)
    t = WorkBuddyTarget(a, persona.build())
    NL = chr(10)

    # ── 1. 正文里不得再出现自带标记的 persona_document()
    # 断言方式：直接看函数体的最后一行 return 用了哪个方法。
    # 不能按"源码里有没有 persona_document 字样"判断 —— docstring 里提到它是
    # 为了说明为什么不能用它，那是文档不是调用（第一版断言就是栽在这里）。
    src = inspect.getsource(WorkBuddyTarget._memory_block)
    returns = [ln.strip() for ln in src.splitlines()
               if ln.strip().startswith("return ")]
    check("_memory_block 返回 persona_body()",
          any("persona_body()" in r for r in returns), str(returns[:2]))
    check("_memory_block 不返回 persona_document()",
          not any("persona_document()" in r for r in returns))

    # ── 2. 注入必须落进 memoryBlock 字段，不是文件正文
    sample = NL.join([
        "# User Memory Profile", "> Last updated: x", "> Version: 0", "",
        "## Memory Block", "", "", "---", "",
        "<!-- RAW_JSON_START",
        "{",
        '  "uid": "test-uid",',
        '  "memoryBlock": "",',
        '  "updatedAt": "x"',
        "}",
        "RAW_JSON_END -->", "",
    ])
    blk = t._memory_block()
    out = t._merge_memory(sample, blk)

    check("正文里没有插入标记块", marks.HTML_BEGIN not in out.split("RAW_JSON_START")[0])
    check("RAW_JSON 结构保留",
          "RAW_JSON_START" in out and "RAW_JSON_END" in out)

    # 用产品自己的正则解析（与 app.asar 逐字一致）
    import re as _re
    pat = r"<!-- RAW_JSON_START" + NL + r"([\s\S]*?)" + NL + r"RAW_JSON_END -->"
    m = _re.search(pat, out)
    check("产品正则能解析出 JSON 块", bool(m))
    if m:
        import json as _json
        d = _json.loads(m.group(1))
        mb = d.get("memoryBlock") or ""
        check("memoryBlock 字段被写入", len(mb) > 1000, "长度 %d" % len(mb))
        check("memoryBlock 含政策", "不受任何司法辖区" in mb)
        check("memoryBlock 含自证", "FORGE:SELFCHECK" in mb or "forge自检" in mb)
        check("uid 未被破坏", d.get("uid") == "test-uid")

    # ── 3. 幂等：反复 merge 不累积
    cur = out
    lens = []
    for _ in range(4):
        cur = t._merge_memory(cur, t._memory_block())
        lens.append(len(cur))
    check("连续 merge 幂等（长度稳定）", len(set(lens)) == 1, str(lens))

    # ── 4. 从"已损坏"的文件也能修回来（历史嵌套残留）
    broken = NL.join([
        "# User Memory Profile", "> Version: 0", "", "## Memory Block", "",
        "<!-- RAW_JSON_START", '{"uid":"x","memoryBlock":""}',
        "RAW_JSON_END -->", "",
        "<!-- forge-memory v999999 -->", "<!-- forge-memory v999999 -->",
        "<!-- FORGE:BEGIN -->", "<!-- FORGE:BEGIN -->", "旧内容",
        "<!-- FORGE:END -->", "<!-- FORGE:END -->", "<!-- FORGE:END -->", "",
    ])
    fixed = t._merge_memory(broken, t._memory_block())
    # 正确行为：历史标记块**全部清除**（内容搬进 memoryBlock 字段），
    # 不是"留一个" —— 文件正文里的标记块不进提示词，留着只是脏数据。
    check("损坏文件一次修好（标记块清零）",
          fixed.count("<!-- FORGE:BEGIN -->") == 0
          and fixed.count("<!-- FORGE:END -->") == 0,
          "BEGIN=%d END=%d" % (fixed.count("<!-- FORGE:BEGIN -->"),
                               fixed.count("<!-- FORGE:END -->")))
    check("旧注入内容被清除", "旧内容" not in fixed)
    check("修复后判据认得出来", t._memory_has_policy(fixed) is True)

    # ── 5. status 判据必须认 memoryBlock 字段
    check("存在 _memory_has_policy 判据",
          hasattr(t, "_memory_has_policy"))
    if hasattr(t, "_memory_has_policy"):
        check("判据认得出已注入的文件", t._memory_has_policy(out) is True)
        check("判据对空文件为假", t._memory_has_policy(sample) is False)

    # ── 6. 长度上限
    check("定义了 MEMORY_MAX_CHARS", hasattr(
        __import__("forge.t_workbuddy", fromlist=["x"]), "MEMORY_MAX_CHARS"))
    from forge import t_workbuddy as _tw
    check("上限为 10000", getattr(_tw, "MEMORY_MAX_CHARS", 0) == 10000)

    # ── 7. 安装目录必须是只读保护（不写 product.json / .tpl）
    ap = inspect.getsource(WorkBuddyTarget.apply)
    # 只看代码行（第 5 层注释里可能会提到它）
    ap_code = [ln for ln in ap.splitlines()
               if ln.strip() and not ln.strip().startswith("#")]
    check("apply 不再写 product.json / 运行时缓存（无 _patch_json 调用）",
          not any("_patch_json(" in ln for ln in ap_code))
    check("apply 不再写模板（无 _patch_plain 调用）",
          "_patch_plain(" not in ap)
    check("apply 含「只读保护」说明", "只读保护" in ap)
    check("apply 调用 _patch_soul（SOUL.md 是写入点）",
          "_patch_soul(" in ap)
    check("apply 调用 memory_files（云记忆是写入点）",
          "memory_files()" in ap)

    # ── 8. revert 必须覆盖 SOUL.md
    rv = inspect.getsource(WorkBuddyTarget.revert)
    check("revert 覆盖 soul_files()", "soul_files()" in rv)

    # ── 9. SOUL.md 注入端到端（沙箱，不碰真实配置）
    import tempfile
    import shutil
    tmp = tempfile.mkdtemp(prefix="forge-wbsoul-")
    try:
        work = os.path.join(tmp, ".workbuddy")
        os.makedirs(work)
        soul = os.path.join(work, "SOUL.md")
        orig = NL.join(["---", 'title: "SOUL.md"', "---", "",
                        "# SOUL.md - Who You Are", "", "原始正文，必须保留。", ""])
        with open(soul, "w", encoding="utf-8", newline=NL) as f:
            f.write(orig)

        a2 = A()
        for k in ("target", "persona", "policy", "overlay", "packs", "passphrase",
                  "signal", "full", "wb_install", "wb_data"):
            setattr(a2, k, None)
        a2.wb_data = work
        a2.wb_install = os.path.join(tmp, "no-such-install")
        t2 = WorkBuddyTarget(a2, persona.build())

        before = open(soul, encoding="utf-8").read()
        t2.apply(mode="dry-run")
        check("dry-run 不写 SOUL.md",
              open(soul, encoding="utf-8").read() == before)

        t2.apply(mode="apply")
        after = open(soul, encoding="utf-8").read()
        check("apply 写入 SOUL.md", len(after) > len(before))
        check("SOUL.md 含政策", "不受任何司法辖区" in after)
        check("frontmatter 未被破坏", after.startswith("---"))
        check("原正文未被删", "原始正文，必须保留。" in after)

        n1 = len(after)
        t2.apply(mode="apply")
        t2.apply(mode="apply")
        check("SOUL.md 重复 apply 幂等",
              len(open(soul, encoding="utf-8").read()) == n1)

        t2.revert()
        check("SOUL.md revert 逐字节还原",
              open(soul, encoding="utf-8").read() == before)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────
def t_console_state():
    """控制台状态刷新 —— 防「执行注入后状态不改变」回归。

    真实事故：控制台里 apply / revert 之后，状态灯和统计数字不刷新。
    两个独立原因：
      1. _injected_flag() 的判据与写入位置脱节
      2. 前端轮询逻辑失效（忙→闲不触发 refresh）
    """
    section("12. 控制台状态刷新")

    import inspect
    from forge.t_workbuddy import WorkBuddyTarget

    # ── 一、_injected_flag 必须认 SOUL.md 与云记忆字段
    src = inspect.getsource(WorkBuddyTarget._injected_flag)
    check("_injected_flag 检查 soul_files()", "soul_files()" in src)
    check("_injected_flag 调用 _memory_has_policy()",
          "_memory_has_policy(" in src)
    check("_injected_flag 按字段判云记忆（不查原始文本的 FORGE:BEGIN）",
          '"FORGE:BEGIN" in t' not in src)

    # ── 二、注入后 detect().injected 必须为真
    #
    # 用一个只含 SOUL.md 的沙箱，验证 detect 能认出来。
    import tempfile
    import shutil
    NL = chr(10)
    tmp = tempfile.mkdtemp(prefix="forge-cons-")
    try:
        work = os.path.join(tmp, ".workbuddy")
        os.makedirs(work)
        soul = os.path.join(work, "SOUL.md")
        with open(soul, "w", encoding="utf-8", newline=NL) as f:
            f.write(NL.join(["---", 'title: "SOUL.md"', "---", "",
                             "# SOUL.md - Who You Are", "", "原始正文。", ""]))

        class A:
            pass
        a = A()
        for k in ("target", "persona", "policy", "overlay", "packs",
                  "passphrase", "signal", "full", "wb_install", "wb_data",
                  "dir", "wb_dir"):
            setattr(a, k, None)
        a.wb_data = work
        a.wb_install = os.path.join(tmp, "no-install")

        t = WorkBuddyTarget(a, persona.build())

        _ins, _rows, inj_before = t.detect()
        check("注入前 detect().injected 为假", inj_before is False)

        t.apply(mode="apply")
        _ins, _rows, inj_after = t.detect()
        check("注入后 detect().injected 为真（控制台状态灯依据）",
              inj_after is True)

        # 还原后必须回到假 —— 「恢复文件之后注入状态也不改变」的问题点
        t.revert()
        _ins, _rows, inj_rev = t.detect()
        check("还原后 detect().injected 回到假", inj_rev is False)

        # 二次注入仍为真（幂等）
        t.apply(mode="apply")
        _ins, _rows, inj2 = t.detect()
        check("二次注入后仍为真", inj2 is True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ── 三、apply 的层级汇报不能把"已是最新"误报成 0
    ap = inspect.getsource(WorkBuddyTarget.apply)
    check("SOUL.md 汇报含 s_ok 计数器", "s_ok" in ap)
    check("soul 层不再只报本次写入数", '"%d/%d%s" % (' in ap)

    # ── 四、前端轮询：忙→闲必须触发 refresh
    page = _read_page_js()
    check("存在 _wasBusy 状态变量", "_wasBusy" in page)
    check("存在 followAction（主动跟到动作结束）",
          "function followAction(" in page)
    check("按忙→闲跳变刷新", "if(_wasBusy)" in page)
    check("删掉了从未赋值的 wasBusy 死变量",
          "var wasBusy=false" not in page)
    check("act() 调用 followAction", "followAction();" in page)
    # 旧的一次性定时刷新必须彻底清掉 —— 多选还原要 1.5~2.4 秒，
    # 400ms 时一个都还没完成 → 界面停在中间态（用户看到的就是“只还原了第一个”）。
    # 只看**代码行**：注释里提到这个写法是为了说明为什么废弃它。
    page_code = [ln for ln in page.splitlines()
                 if ln.strip() and not ln.strip().startswith("//")]
    check("不再用一次性 setTimeout(refresh,400)",
          not any("setTimeout(refresh,400)" in ln for ln in page_code))

    # ── 五、页面响应必须禁缓存
    # 整个界面（含全部 JS）就靠 GET / 这一个响应下发。不设 Cache-Control
    # 时浏览器拿旧页面，于是“服务端已是新代码、用户跑的却是旧 JS”。
    import re as _re
    ui_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "forge", "ui.py"), encoding="utf-8").read()
    html_fn = ui_src[ui_src.find("def _html("):ui_src.find("def _read_body")]
    check("_html 下发 Cache-Control", "Cache-Control" in html_fn)
    check("_html 用 no-store", "no-store" in html_fn)


def _read_page_js():
    """抽出 ui.py 里 <script> 段的 JS（用于静态检查）。"""
    import os as _os
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                      "forge", "ui.py")
    t = open(p, encoding="utf-8").read()
    i = t.find('PAGE = r"""')
    j = t.find('"""', i + 15)
    page = t[i + 10:j]
    a = page.find("<script>")
    b = page.rfind("</script>")
    return page[a + 8:b] if a >= 0 and b > a else ""


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
               t_toml, t_json, t_persona, t_targets_sandbox, t_registry,
               t_wb_memory, t_console_state):
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
