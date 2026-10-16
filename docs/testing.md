# 自检体系

`selftest.py` —— 128 项断言，**全部在临时目录里跑，不碰任何真实客户端配置**。

```bash
python selftest.py
```

---

## 为什么这么设计

这个工具改的是别人的配置文件。一个 bug 的代价可能是用户的工作环境坏掉。所以：

- **全部在临时目录**：`tempfile.mkdtemp()`，跑完删掉。任何时候跑都不会影响本机。
- **断言"不变量"，不是"预期输出"**：比如 `strip(put(X)) == strip(X)`，而不是"结果应该是某某字符串"。前者能覆盖无数边界，后者只能覆盖你想到的那一个。
- **测幂等和可逆**：不只是"改成功了"，还要"再改一次不变"和"能还原回去"。

---

## 12 组，覆盖什么

| 组 | 验证什么 | 关键断言 |
|---|---|---|
| 1 | 标记块幂等 | `strip(put(X)) == strip(X)`，七种边界（空文件、无结尾换行、CRLF、已有块、含标题…） |
| 2 | 原子写 | 无临时文件残留、BOM 处理、只读属性恢复 |
| 3 | 读不干净就跳过 | GBK / 二进制返回 `None`，不静默转码 |
| 4 | dry-run 不写盘 | 一个字节都不写 |
| 5 | 备份策略 | 二次 apply 不把注入版当原件；官方升级后重建基准 |
| 6 | TOML 顶层键 | 插在表头之前、写后仍可解析、还原逐字节一致 |
| 7 | JSON 转义 | 替换后仍合法；真实换行会破坏 JSON（反例验证） |
| 8 | 人格装配 | 标记、政策、覆盖层、自证齐备；`persona_body` 不带标记 |
| 9 | 靶点端到端 | dry-run / apply / 幂等 / revert，沙箱目录 |
| 10 | 注册表 | 15 个靶点、目标表达式解析 |
| 11 | WorkBuddy 云记忆 | 防三处已修 bug 回归 |
| 12 | 控制台状态刷新 | 注入/还原后 detect().injected 能正确翻转；前端轮询不漏刷新 |

---

## 第 11 组详解（新增）

这一组是**回归防护**，对应 `pitfalls.md` 的 #1 / #2 / #7。三个都是真实事故：

```
[PASS] _memory_block 返回 persona_body()
[PASS] _memory_block 不返回 persona_document()
[PASS] 正文里没有插入标记块
[PASS] RAW_JSON 结构保留
[PASS] 产品正则能解析出 JSON 块
[PASS] memoryBlock 字段被写入
[PASS] memoryBlock 含政策
[PASS] memoryBlock 含自证
[PASS] uid 未被破坏
[PASS] 连续 merge 幂等（长度稳定）
[PASS] 损坏文件一次修好（标记块清零）
[PASS] 旧注入内容被清除
[PASS] 修复后判据认得出来
[PASS] 存在 _memory_has_policy 判据
[PASS] 判据认得出已注入的文件
[PASS] 判据对空文件为假
[PASS] 定义了 MEMORY_MAX_CHARS
[PASS] 上限为 10000
[PASS] apply 不再写 product.json / 运行时缓存
[PASS] apply 不再写模板
[PASS] apply 含「只读保护」说明
[PASS] apply 调用 _patch_soul（SOUL.md 是写入点）
[PASS] apply 调用 memory_files（云记忆是写入点）
[PASS] revert 覆盖 soul_files()
[PASS] dry-run 不写 SOUL.md
[PASS] apply 写入 SOUL.md
[PASS] SOUL.md 含政策
[PASS] frontmatter 未被破坏
[PASS] 原正文未被删
[PASS] SOUL.md 重复 apply 幂等
[PASS] SOUL.md revert 逐字节还原
```

### 用产品自己的正则验证

不自己造一个"差不多的"正则，而是**逐字复制产品源码里的那个**：

```python
# 与 app.asar 里 parseMarkdown 的 /<!-- RAW_JSON_START\n([\s\S]*?)\nRAW_JSON_END -->/ 一致
pat = r"<!-- RAW_JSON_START" + NL + r"([\s\S]*?)" + NL + r"RAW_JSON_END -->"
m = _re.search(pat, out)
check("产品正则能解析出 JSON 块", bool(m))
```

**为什么**：自己写的宽松正则能过，产品解析不了 —— 那就是假绿。

### 从"已损坏"状态开始测

不只测干净输入，还测**能不能修好已经坏掉的**：

```python
broken = NL.join([
    "# User Memory Profile", ..., "## Memory Block", "",
    "<!-- RAW_JSON_START", '{"uid":"x","memoryBlock":""}', "RAW_JSON_END -->", "",
    "<!-- forge-memory v999999 -->", "<!-- forge-memory v999999 -->",
    "<!-- FORGE:BEGIN -->", "<!-- FORGE:BEGIN -->", "旧内容",
    "<!-- FORGE:END -->", "<!-- FORGE:END -->", "<!-- FORGE:END -->", "",
])
fixed = t._merge_memory(broken, t._memory_block())
check("损坏文件一次修好（标记块清零）",
      fixed.count("<!-- FORGE:BEGIN -->") == 0)
```

这是从真实事故里学到的：用户机器上已经有损坏文件，光修代码不够，得能**清干净**。

### 只读"代码行"，不读注释

第一版断言栽在这里：

```python
# 错：注释里提到 persona_document() 是为了说明为什么不能用它
check("_memory_block 不再用 persona_document()",
      "persona_document" not in inspect.getsource(WorkBuddyTarget._memory_block))
```

注释里出现该字符串 → 断言假失败。

**正确做法**：看函数实际返回什么。

```python
returns = [ln.strip() for ln in src.splitlines()
           if ln.strip().startswith("return ")]
check("_memory_block 返回 persona_body()",
      any("persona_body()" in r for r in returns))
check("_memory_block 不返回 persona_document()",
      not any("persona_document()" in r for r in returns))
```

## 第 12 组详解

防「执行注入后状态不改变」回归。真实事故，两个独立原因：

```
[PASS] _injected_flag 检查 soul_files()
[PASS] _injected_flag 调用 _memory_has_policy()
[PASS] _injected_flag 按字段判云记忆（不查原始文本的 FORGE:BEGIN）
[PASS] 注入前 detect().injected 为假
[PASS] 注入后 detect().injected 为真（控制台状态灯依据）
[PASS] 还原后 detect().injected 回到假
[PASS] 二次注入后仍为真
[PASS] SOUL.md 汇报含 s_ok 计数器
[PASS] soul 层不再只报本次写入数
[PASS] 存在 _wasBusy 状态变量
[PASS] 存在 followAction（主动跟到动作结束）
[PASS] 按忙→闲跳变刷新
[PASS] 删掉了从未赋值的 wasBusy 死变量
[PASS] act() 调用 followAction
```

### 测状态翻转的完整循环

不只测"注入后是真"，还要测**还原后回到假**：

```python
_ins, _rows, inj_before = t.detect()
check("注入前 detect().injected 为假", inj_before is False)

t.apply(mode="apply")
_ins, _rows, inj_after = t.detect()
check("注入后 detect().injected 为真", inj_after is True)

t.revert()
_ins, _rows, inj_rev = t.detect()
check("还原后 detect().injected 回到假", inj_rev is False)   # ← 关键
```

这正是用户报的第二个症状（"恢复文件之后注入状态也不改变"）。

### 前端逻辑用静态检查 + node 对照实验

JS 嵌在 Python 字符串里，Python 语法检查覆盖不到。两道防线：

**一、静态检查关键词**（`selftest.py`）：

```python
page = _read_page_js()      # 抽出 ui.py 里 <script> 段
check("存在 _wasBusy 状态变量", "_wasBusy" in page)
check("按忙→闲跳变刷新", "if(_wasBusy)" in page)
check("删掉了从未赋值的 wasBusy 死变量", "var wasBusy=false" not in page)
```

**二、node 语法检查**（开发时手动跑）：

```bash
python -c "..." > /tmp/page.js    # 抽出 JS
node --check /tmp/page.js
```

**三、对照实验**：复刻新旧两版轮询逻辑，同场景跑，证明旧版确实不刷新 —— 这是
"我修的到底是不是真 bug"的验证方式，比读代码可靠。

```
旧逻辑： refresh 次数: 0      ← 状态永不刷新
新逻辑： refresh 次数: 1      ← 正常
```

### 静态断言：锁住"不该写的东西"

`apply` 不该再调用 `_patch_json` / `_patch_plain`：

```python
ap = inspect.getsource(WorkBuddyTarget.apply)
ap_code = [ln for ln in ap.splitlines()
           if ln.strip() and not ln.strip().startswith("#")]
check("apply 不再写 product.json / 运行时缓存（无 _patch_json 调用）",
      not any("_patch_json(" in ln for ln in ap_code))
```

这是**防止有人把危险改动加回来**。

---

## 写断言的方式

用 `check(name, cond, detail)`：

```python
check("消息", 条件, "不通过时显示的细节")
```

输出：

```
[PASS] 标记块可完整剥离
[FAIL] apply 幂等  期望=1234 实得=1240
```

### 对比时打印实际值

```python
# 好
check("幂等", len(s3) == len(after), "%d vs %d" % (len(s3), len(after)))

# 差：失败了不知道差多少
check("幂等", len(s3) == len(after))
```

### 断言要能"说人话"

```python
# 好
check("产品正则能解析出 JSON 块", bool(m))

# 差
check("test_parse", bool(m))
```

---

## 加一组断言

1. 写一个 `t_xxx()` 函数
2. 函数体开头 `section("N. 标题")`
3. 在 `main()` 的元组里加函数名

```python
def t_my_thing():
    section("12. 我的验证")

    # 用 tempfile，不碰真实配置
    d = tempfile.mkdtemp(prefix="forge-my-")
    try:
        ...
        check("某个不变量", cond, "细节")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    ...
    for fn in (t_markblock, ..., t_wb_memory, t_my_thing):
        try:
            fn()
        except Exception as e:
            import traceback
            FAIL.append(fn.__name__)
            print("  [ERR] %s 抛出异常：" % fn.__name__)
            traceback.print_exc()
```

**注意**：函数抛异常会被捕获并记为失败，不会中断整轮 —— 所以断言里的代码可以不那么防御。

---

## 跑之后看什么

```bash
python selftest.py; echo "exit=$?"
```

- `exit=0` = 全过
- 有失败会打印 `N/M 通过，K 失败` 和失败名单

**在改代码之后必跑**。特别是改了 `marks.py` / `core.py` / `t_workbuddy.py` 这三个。

---

## 没覆盖的

诚实说明边界：

| 没测的 | 为什么 |
|---|---|
| 真实客户端能否正确读取 | 需要装客户端 + 重启 + 发自证口令。日志里可查 `[UserMemoryEvent] memory_injection_success` |
| 各客户端的**版本差异** | 15 个客户端，版本各异。探测逻辑按路径/内容匹配，不按版本 |
| 并发写入 | 本工具是单进程 CLI，不做并发假设 |
| 大文件性能 | 最大的 `product.json` 是 613KB，现在只读不写；实际写入的文件都在 100KB 以内 |
