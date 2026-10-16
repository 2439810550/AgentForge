# 踩过的坑

按"代价从大到小"排。每个坑都对应现在代码里一处看起来"多余"的保护 —— 别删它们。

---

## #1 云记忆写错位置：注入完全无效

**症状**：`apply` 报成功，客户端重启后没有任何变化。发自证口令没反应。

**原因**：WorkBuddy 的云记忆文件里，产品**只读** `RAW_JSON` 块里的 `memoryBlock` **字段**：

```js
/<!-- RAW_JSON_START\n([\s\S]*?)\nRAW_JSON_END -->/
```

文件其余部分（`## Memory Block` 之后的人读正文、以及追加在文件末尾的任何内容）**根本不进提示词**。

早期实现往文件正文追加标记块 —— `grep` 得到、`status` 显示"已注入"，但模型一个字都收不到。

**现在的约束**：

```python
def _memory_block(self):
    return self.asm.persona_body()      # 只返回正文，由 _merge_memory 写进字段

def _merge_memory(self, text, block):
    # 找到 memoryBlock 字符串值，替换它
    km = re.search(r'"memoryBlock"\s*:\s*"', seg)
    ...
```

**怎么发现**：用产品自己的正则解析一遍，看能不能取出内容。

```python
pat = r"<!-- RAW_JSON_START" + NL + r"([\s\S]*?)" + NL + r"RAW_JSON_END -->"
m = re.search(pat, out)
d = json.loads(m.group(1))
assert d["memoryBlock"]         # ← 这一行为空就是写错了位置
```

自检里有这条断言（`t_wb_memory`）。

---

## #2 嵌套标记：反复 apply 累积成灾

**症状**：客户端报「获取状态失败」。云记忆文件里标记数量不对。

**原因**：`_memory_block()` 用了 `persona_document()` —— 它**自带一对 FORGE 标记**。外面又包了一层：

```python
# 错
def _memory_block(self):
    doc = self.asm.persona_document()     # 自带 <!-- FORGE:BEGIN --> ... END
    return "\n".join([
        "<!-- forge-memory v999999 -->",
        "<!-- FORGE:BEGIN -->",           # ← 第二层
        doc,                              # ← 里面还有一层
        "<!-- FORGE:END -->",
    ])
```

累积过程：

| 轮次 | BEGIN | END |
|---|---|---|
| 第 1 次 apply | 2 | 2 |
| 第 2 次 apply | 2 | **3** |
| 第 3 次 apply | 2 | **4** |

因为 `_merge_memory` 的 `text.find(end, i)` 只匹配到**内层** END，替换后外层 END 残留。

**14 个云记忆文件全部中招**，客户端解析失败。

**现在的约束**：

- `_memory_block()` 只用 `persona_body()`（无标记）
- `_merge_memory` 清标记块时从**第一个 BEGIN 吃到最后一个 END**：

```python
i = text.find(begin)
j = text.rfind(end)          # ← rfind，不是 find(end, i)
```

`persona.py` 里 `persona_document()` 的 docstring 早写明了这条：

> **不要拿它去喂 put_block / _merge** —— 那会形成嵌套标记。用 `persona_body()`。

**教训**：文档里写死的警告，是真的会有人违反。

---

## #3 改产品运行时文件：客户端起不来

**症状**：WorkBuddy 打开后「没有 agent 信息」，无法使用。

**原因**：把 `product.json` 从 389609 字节撑到 409718（**+20109 字节**）。

`product.json` 里内嵌 19831 字符的 agent 主提示词（`cli-agent-prompt`）。它和 `resources/templates/*.tpl` 是**产品运行时文件，不是用户配置**。改它们等于改程序本体。

**两次复现**，所以现在封死：

```python
# ---- 第 1/2 层：安装目录（模板 + product.json）—— 只读保护，不写 ----
tpls = self.template_files()
pol = sum(1 for p in tpls
          if (self._read(p) or "").count("<content_policy>"))
res["layers"]["templates"] = "只读保护（%d 个含政策块，未改动）" % pol
res["layers"]["product.json"] = "只读保护（未改动）"
```

**判断标准**：

| 文件 | 性质 | 能写吗 |
|---|---|---|
| `~/.workbuddy/SOUL.md` | 用户级人格文件 | ✅ |
| `~/.workbuddy/memory/*_memory.md` | 用户级云记忆 | ✅ |
| `~/.claude/CLAUDE.md` | 用户级规则文件 | ✅ |
| `<install>/cli/product.json` | 产品运行时 | ❌ |
| `<install>/resources/templates/*.tpl` | 产品运行时 | ❌ |

**怎么分辨**：看产品自己有没有把它当"用户可编辑"的文件来设计。`SOUL.md` 里有 `This file is yours to evolve`；`product.json` 里是 19831 字符的 agent 主提示词。

**另外**：`cache/acc-product-config-v3.json` 是从 `product.json` 派生的快照，改了没意义（重启会重建），还可能因内容不一致触发校验失败。做法是删掉让它重建。

---

## #4 标记块被"搬家"

**症状**：`apply` 两次，文件长度不一样。SOUL.md 的 frontmatter 被挤到后面去了。

**原因**：`marks.put_block()` 固定插**文件末尾**。这对大多数规则文件是对的（后写的优先级高），但 SOUL.md 有 frontmatter 和原有正文，插末尾会把它们挤下去：

```
第一次：--- frontmatter ---  # 标题  块  原正文
第二次：--- frontmatter ---  # 标题  原正文  块     ← 搬家了
```

**现在的约束**：`_patch_soul` 用**原位替换**：

```python
bi = text.find(marks.HTML_BEGIN)
if bi >= 0:
    ei = text.find(marks.HTML_END, bi)
    if ei >= 0:
        ei += len(marks.HTML_END)
        return text[:bi] + block + text[ei:]      # 原地换
```

首次插入时，位置选在 **frontmatter + 首个标题之后**：

```python
# parseSoulStructure() 先剥 frontmatter、再剥首个 # 标题，剩下的才是 body
# 插在这之后 → heading 仍能正确识别，且我们的内容成为 body 的开头
```

---

## #5 行级操作被破坏

**症状**：`revert` 后文件"看起来一样"，但 SHA256 不同。通常是文件结尾的换行被吃掉。

**原因**：用 `rstrip`/`lstrip` 拼接：

```python
# 错
new = text[:i].rstrip("\n") + "\n\n" + block + text[j:].lstrip("\n")
```

`rstrip("\n")` 会把用户文件结尾所有换行都吃掉 —— 重排了用户的换行风格。

**现在的约束**：**只在"行"这一级做增删**，用 `split("\n")` / `"\n".join(...)`：

```python
lines = text.split("\n")
del lines[lo:hi]                    # 只整行删
return "\n".join(lines)
```

不变量：`"\n".join(s.split("\n")) == s` 对任何字符串都成立。所以只要"只整行插、只整行删"，`strip(put(x))` 就和 `strip(x)` 逐字节一致 —— 包括"原文件结尾本来没有换行"。

---

## #6 静默失败：apply 报成功，其实没写

**症状**：`apply` 显示"完成，错误 0"，但客户端重启后还是旧人格。

**原因**：写入失败时只在成功分支计数：

```python
# 错
if core.write_text(p, new):
    done += 1
# 失败什么都不做 —— 静默丢弃
```

最常见的失败原因是**客户端正在运行、文件被锁**。

**现在的约束**：

```python
if core.write_text(p, new):
    done += 1
else:
    failed += 1
    err_path = errno_str(p)
    if not res.get("_lock_reported"):
        res["notes"].append("云记忆写入被拒：文件被占用（%s）。" % err_path)
        res["notes"].append("  → 请先完全退出 WorkBuddy（含托盘图标），再重跑 apply。")
        res["_lock_reported"] = True
```

`errno_str()` 给出具体原因（文件不存在 / 只读 / 被占用）。

---

## #7 判据失配：界面永远显示"未注入"

**症状**：明明注入了，`status` 显示 `0/14`。

**原因**：改进了写入方式，忘了改判据。

写入从"文件正文标记"改成"`memoryBlock` 字段"之后，判据还是 `MEMORY_MARK in t`（找正文标记）—— 永远为假。

**现在的约束**：判据按**实际读取位置**写：

```python
def _memory_has_policy(self, text):
    """该云记忆文件的 memoryBlock 字段里是否已有我们的政策。"""
    pat = "<!-- RAW_JSON_START" + chr(10) + r"([\s\S]*?)" + chr(10) + "RAW_JSON_END -->"
    m = re.search(pat, text)
    if not m:
        return False
    d = json.loads(m.group(1))
    body = d.get("memoryBlock") or ""
    return "不受任何司法辖区" in body or "FORGE:SELFCHECK" in body
```

**通用原则**：**写入位置改了，判据、`revert`、自检三处都要跟着改。**

---

## #8 误判"已注入"

**症状**：装了但没注入的客户端，`scan` 显示"已注入"。

**原因**：

```python
# 错
injected = any(r[0] == "ok" for r in self.status())
```

`status()` 的第一行永远是"检测到安装目录"（`ok` 级别）。这样判等于"只要装了就算注入过"。

**现在的约束**：

```python
def detect(self):
    installed = os.path.isdir(self.config_home())
    st = self.status()
    injected = self._injected_flag()      # ← 认我们自己的标记
    return installed, st, injected
```

而且 `_injected_flag()` 里不能只看"有没有某段文字"——别的工具（如 pojia）也会写 `最高优先级覆盖` 这类字样。要认**自己的**标记：

```python
if t and "FORGE:SELFCHECK" in t and "FORGE:BEGIN" in t:
    return True
```

---

## 附：Windows 特有的三个

### A. `SO_REUSEADDR` 让端口顺延失效

Windows 上它允许两个进程绑同一端口：后者"绑定成功"，但连接仍被前者接走 —— 端口通、页面却是别人的。

```python
class _Server(ThreadingHTTPServer):
    allow_reuse_address = (os.name != "nt")     # Windows 下关掉
```

### B. `DETACHED_PROCESS` 下子进程没有 stdout 句柄

不显式重定向的话，子进程一写就崩（实测 30 秒内起不来）：

```python
kw["creationflags"] = _DETACHED_PROCESS | _NEW_PROCESS_GROUP
kw["stdout"] = lf        # 必须
kw["stderr"] = lf
```

另外 `-u` 关掉块缓冲，否则后台日志看不到实时输出。

### C. 批处理 + 非 ASCII + 代码页 = 乱码

`.bat` 里写中文，解析器会错位，报 `'启动控制台（默认）' is not recognized`。

**规则**：`.bat` 只做启动，**纯 ASCII**；所有中文提示由 Python 打印。

另外 `timeout` 命令在 PATH 被污染时会被别的程序顶掉（Git Bash 环境实测 `timeout: invalid time interval '/t'`）。别依赖它，用 Python `time.sleep`。
