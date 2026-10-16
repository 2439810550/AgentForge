# Forge 开发文档

给要读代码、改代码、加靶点的人。README 讲"怎么用"，这里讲"为什么这样写、改哪里会坏"。

---

## 目录

| 文档 | 内容 |
|---|---|
| 本文 | 架构总览、模块职责、数据流 |
| [`adding-a-target.md`](adding-a-target.md) | 加一个新客户端靶点（含真实样例） |
| [`pitfalls.md`](pitfalls.md) | 踩过的坑与对应的代码约束 |
| [`testing.md`](testing.md) | 自检体系与怎么加断言 |

---

## 一、这个东西在做什么

把一份装配好的提示词文档，注入进 15 个 AI 编程客户端的提示词层。

三个硬约束，所有设计都是被它们逼出来的：

1. **只能改文本。** 不碰二进制、不装驱动、不改注册表、不发网络请求。
2. **必须可逆。** `revert` 之后文件要和注入前**逐字节相同**——不是"看起来一样"，是 SHA256 一致。
3. **必须幂等。** 反复 `apply` 不堆叠，不膨胀。

---

## 二、模块地图

```
forge/
├─ core.py        读写原语：原子写、备份、还原、dry-run 闸门
├─ marks.py       标记块引擎：行级手术、幂等、JSON 转义
├─ toml.py        TOML 顶层键操作（Codex 专用）
├─ persona.py     装配层：政策 + 覆盖层 + 人格 + 自证 + 词包
├─ targets.py     靶点基类 + 12 个声明式靶点
├─ t_codex.py     Codex（TOML 顶层键）
├─ t_workbuddy.py WorkBuddy（SOUL.md + 云记忆两条通道）
├─ t_dsh.py       DSH（AGENTS.md 入口 + 技能文件）
├─ registry.py    注册表：key → 类
├─ cli.py         命令行
├─ launch.py      一键启动：探活 → 拉起 → 等上线 → 开浏览器
├─ probe.py       端口探活（只读）
├─ menu.py        交互菜单（纯 Python）
├─ _tty.py        控制台编码兼容
└─ ui.py          Web 控制台
```

依赖方向是单向的，不循环：

```
core  ←  marks  ←  targets  ←  t_*  ←  registry  ←  cli / ui
                    ↑
                 persona
```

`core` 不认识 `marks`；`t_*` 不认识 `cli`。加靶点不需要改 `core`。

---

## 三、核心抽象

### 3.1 闸门（`core.Gate`）

三种模式，**在写入层拦截**，不依赖调用方记得判断：

| 模式 | 效果 |
|---|---|
| 正常 | 正常写 |
| `dry_run` | `write_text` / `backup` / `restore` 全部返回 False，一个字节不落盘 |
| `readonly` | 连日志都不写 |

**为什么放在写入层**：早期靠调用方自觉判断 `if not dry:`，结果一处漏判就把预演变成了真写。放在 `core` 里，将来加新命令即使忘了判断也改不坏东西。

`cli.py` 里：

```python
if args.cmd in ("status", "scan", "persona", "packs"):
    core.Gate.enter_readonly()
try:
    return fn(args, asm) or 0
finally:
    core.Gate.reset()
```

### 3.2 原子写（`core.atomic_write`）

先写同目录临时文件 → `fsync` → `os.replace` 顶替。

三个细节都是踩出来的：

- **临时文件必须同目录**：跨盘 `os.replace` 会失败。
- **只读属性要先清**：别的工具（如 pojia）会把云记忆设成只读防客户端清空。Windows 上 `os.replace` 到只读目标会 `PermissionError`。做法是写前清属性、写后恢复。
- **失败必须重启临时文件**：否则在用户目录里留一堆 `.xxx.forge-tmp`。

### 3.3 备份策略（`core.backup`）

**只存"未被我们改过的原件"**。三种情形：

| 状态 | 行为 |
|---|---|
| bak 不存在 | 备份当前文件 |
| bak 存在，当前**已注入** | 什么都不做（保住原始基准） |
| bak 存在，当前是**干净的官方版** | 归档旧 bak → 重建基准 |

第三种是"官方升级自愈"：客户端升级会覆盖配置文件，此时当前内容反而是干净的原版，而 bak 是更老的原版。不重建基准的话，`revert` 会把文件降级。

判据由调用方通过 `is_pristine` 传入，比如：

```python
core.backup(p, suffix=".forge-bak", root=home,
            is_pristine=lambda t: not self._ours(t))
```

---

## 四、三种缝合方式

15 个客户端的注入差异，最后归结为**三件事**：

1. 配置目录在哪
2. 提示词文件叫什么
3. **往里面塞什么形态**

第三种决定了用哪个引擎：

### 4.1 标记块（`marks.py`）—— .md / .mdc / .txt / .js

```
<!-- FORGE:BEGIN -->
...内容...
<!-- FORGE:END -->
```

### 4.2 TOML 顶层键（`toml.py`）—— Codex `config.toml`

这里有个**致命细节**：TOML 里"表头之后的键属于那张表"。

```toml
[shell_environment_policy.set]
FOO = "bar"

model_instructions_file = '...'   # ← 被解析成 FOO 那张表的成员，Codex 读不到
```

所以只能插到**第一个表头之前**的顶层键区。写盘后还要用 `tomllib` 真解析复核一遍。

### 4.3 JSON 字符串（`marks.json_escape_body`）—— WorkBuddy `product.json`

提示词是 JSON 字符串，换行必须写成**两字符的转义序列** `\n`。直接塞真实换行会让整个文件语法损坏。

```python
def json_escape_body(s):
    out = s.replace("\\", "\\\\")
    out = out.replace("\"", "\\\"")
    out = out.replace("\r\n", "\\n").replace("\n", "\\n")
    ...
```

写前用 `json.loads()` 验证，不合法就放弃。

---

## 五、幂等是怎么做到的

### 5.1 行级手术

**整件事只在"行"这一级做增删**，用 `split("\n")` / `"\n".join(...)` 当手术刀。

不变量：`"\n".join(s.split("\n")) == s` 对任何字符串都成立。所以只要保证"只整行插、只整行删"，`strip(put(x))` 就能和 `strip(x)` 逐字节一致。

**反例**（初版踩的坑）：

```python
# 错：把用户的换行风格重排了一遍，永远回不到原样
new = text[:i].rstrip("\n") + "\n\n" + block + text[j:].lstrip("\n")
```

`rstrip`/`lstrip` 会吃掉用户文件结尾的换行，实测 `strip(put(x)) != strip(x)`。

### 5.2 先剥后放（`marks.put_block`）

```python
def put_block(text, block, begin, end):
    base = strip_block(text or "", begin, end)   # 先剥旧块
    ...
    return base + "\n\n" + block.rstrip("\n") + "\n"
```

不管之前堆过几份，先剥干净再放一份。

### 5.3 原位替换 vs 搬家

`put_block` 固定插文件末尾——对大多数规则文件是对的（后写的优先级高）。

但 **SOUL.md 不行**：它是用户自己的文件，有 frontmatter 和原有正文，插到末尾会把它们挤下去。所以 `_patch_soul` 用**原位替换**：

```python
bi = text.find(marks.HTML_BEGIN)
if bi >= 0:
    ei = text.find(marks.HTML_END, bi) + len(marks.HTML_END)
    return text[:bi] + block + text[ei:]     # 原地换，不搬家
```

**实测**：用 `put_block` 会每次 apply 重排一次，第二次起长度就变了（不幂等）。

---

## 六、数据流

### 6.1 装配（`persona.py`）

```
policy.md  ─┐
overlay.md ─┤
persona.md ─┼→ Assembly ─→ persona_body()     （无标记，用于再包一层）
packs/*.md ─┤              persona_document() （带标记，用于独立文件）
自证口令   ─┘
```

**这两个方法必须分清**，混用会出嵌套标记：

| 方法 | 何时用 |
|---|---|
| `persona_body()` | 外面还要包标记块时（`_merge` / `_merge_memory` / `_patch_soul`） |
| `persona_document()` | 文件本身就是我们的（Codex 指令文件、DSH 技能文件） |

`persona_document()` 的 docstring 里写死了这条警告，`targets.py` 的 `_merge()` 也注释了。**违反它就是 bug**——WorkBuddy 的云记忆栽过两次。

### 6.2 注入（`Target.apply`）

```
detect() → status() → apply() → revert()
```

基类只管生命周期，子类只声明差异：

```python
class GeminiTarget(Target):
    key, label = "gemini", "Gemini CLI"
    default_sub, env_vars = ".gemini", ["GEMINI_DIR", "GEMINI_HOME"]
    prompt_files = ["GEMINI.md"]
```

四行就是一个靶点。

### 6.3 目录探测优先级

```
命令行 --xxx-dir  >  环境变量  >  默认路径/扫盘
```

```python
def override_dir(self):
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
```

---

## 七、`_injected_flag` 的判据

判断"这个客户端是不是被**我们**注入过"，**必须严格基于我们的标记**。

```python
def detect(self):
    installed = os.path.isdir(self.config_home())
    injected = self._injected_flag()      # ← 不是 any(r[0] == "ok")
    return installed, st, injected
```

**为什么**：`status()` 的第一行永远是"检测到安装目录"（`ok` 级别）。如果用 `any(r[0] == "ok")` 判注入，装了但没注入的机器会被误报成已注入。

同理，WorkBuddy 的云记忆判据不能只看"文件里有没有某段文字"——必须**按字段判定**（`_memory_has_policy`），因为产品读的是 `memoryBlock` 字段，不是文件正文。

---

## 八、WorkBuddy 的特殊处理

它是唯一一个多通道靶点，也是唯一一个**只读保护安装目录**的。

### 8.1 两条写入通道

| 通道 | 位置 | 特点 |
|---|---|---|
| SOUL.md | `~/.workbuddy/SOUL.md` | 产品设计给用户写；有 frontmatter，需原位替换 |
| 云记忆 | `~/.workbuddy/memory/<uid>_memory.md` | 每轮自动注入，抗升级；写 `memoryBlock` **字段** |

### 8.2 三条只读的层

| 层 | 为什么不写 |
|---|---|
| `resources/templates/*.tpl` | 产品运行时模板 |
| `cli/product.json` | 内嵌 19831 字符的 agent 主提示词 |
| `cache/acc-product-config-v3.json` | 从 product.json 派生的快照，重启会重建 |

**实测事故**：改 `product.json` 把它从 389609 撑到 409718（+20109 字节）后，WorkBuddy 出现「没有 agent 信息」起不来。两次复现，因此封死。

### 8.3 云记忆的两个坑

**坑一：写字段不是写正文。** 产品只读 `RAW_JSON` 块里的 `memoryBlock` 字段：

```js
/<!-- RAW_JSON_START\n([\s\S]*?)\nRAW_JSON_END -->/
```

文件其余部分（`## Memory Block` 之后的人读正文）根本不进提示词。往正文追加标记块——看着改了，模型一个字收不到。

**坑二：`localMemoryEnabled` 默认关闭。**

```js
localMemoryEnabled: r === true    // 只有显式为 true 才开
```

`settings.json` 里没这个键 → 本地云记忆强制关闭 → 产品只从远端拉。

**另外**：`memoryBlock` 上限 10000 字符（超了被产品截断），落盘后设只读（挡住空档案回写）。

---

## 九、加一个靶点

见 [`adding-a-target.md`](adding-a-target.md)。

最短路径：写一个类 + 在 `registry.py` 加一行 + 在 `selftest.py` 加断言。

---

## 十、踩过的坑

见 [`pitfalls.md`](pitfalls.md)。**建议先读那个再改代码**——里面有 8 个坑，每个都对应一处现在看起来"多余"的代码。

---

## 十一、约定

- 文件写入一律 UTF-8，不写 BOM（除非原件有）。
- 换行风格跟随原件（`core.match_nl`）。
- 注释写**为什么**，不写**是什么**。代码本身说明是什么。
- 每个非显然的设计决定，注释里要留事故记录。例："实测把 X 撑到 Y 后客户端起不来"。
- 中文回复，技术名词保留标准英文。
