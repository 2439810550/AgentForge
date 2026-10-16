# 加一个靶点

从零加一个客户端的完整流程。**真实样例走一遍**，不是伪代码。

---

## 一分钟版

三步：

1. 在 `forge/targets.py` 加一个类（或单独文件，如果逻辑复杂）
2. 在 `forge/registry.py` 的 `REGISTRY` 元组里加一行
3. 在 `selftest.py` 加断言

---

## 第一步：确定形态

先回答三个问题：

| 问题 | 怎么查 |
|---|---|
| 配置目录在哪？ | 装一遍客户端，看它读哪个文件；或查官方文档 |
| 提示词文件叫什么？ | 同上 |
| 什么形态？ | 是纯文本 → 标记块；是 TOML → 顶层键；是 JSON 内嵌字符串 → 字符串替换 |

绝大多数情况是**标记块**（纯文本 `.md` / `.mdc`）。这时只需要继承基类、声明差异。

---

## 第二步：声明式靶点（最常见）

打开 `forge/targets.py`，在文件末尾附近加：

```python
class MyClientTarget(Target):
    key = "myclient"                    # 命令行 --target 用的名字
    label = "My Client"                 # 界面显示名
    default_sub = ".myclient"           # 配置目录（相对用户主目录）
    env_vars = ["MYCLIENT_DIR", "MYCLIENT_HOME"]   # 环境变量覆盖（按优先级）
    prompt_files = ["MYCLIENT.md"]      # 提示词文件（相对配置目录）
```

**就这五行**。`detect` / `status` / `apply` / `revert` 全部由基类提供。

如果文件是 `.mdc`（Cursor 那类带 frontmatter 的），再加一行：

```python
    frontmatter = """---
description: 操作者配置（Forge 注入）
globs:
alwaysApply: true
---"""
```

`apply` 时会在内容前自动补上，`revert` 时连它一起收掉。

### 真实样例：Gemini CLI

```python
class GeminiTarget(Target):
    key, label = "gemini", "Gemini CLI"
    default_sub, env_vars = ".gemini", ["GEMINI_DIR", "GEMINI_HOME"]
    prompt_files = ["GEMINI.md"]
```

### 多文件的情况

有的客户端读多个文件（比如同时读 `AGENTS.md` 和 `ZCODE.md`）：

```python
class ZCodeTarget(Target):
    key, label = "zcode", "ZCode"
    default_sub = ".zcode"
    env_vars = ["ZCODE_DIR", "ZCODE_HOME"]
    prompt_files = ["AGENTS.md", "ZCODE.md"]     # 两个都写
```

`apply` / `revert` / `status` 会自动遍历。

---

## 第三步：注册

打开 `forge/registry.py`：

```python
from .targets import (
    GeminiTarget, QwenTarget, ..., MyClientTarget,   # ← 加导入
)

REGISTRY = {}
for _cls in (
    WorkBuddyTarget,
    ...
    CopilotTarget,
    MyClientTarget,     # ← 加一行
):
    REGISTRY[_cls.key] = _cls
```

顺序决定 `scan` / `status` 的显示顺序。把常用的放前面。

---

## 第四步：测

```bash
# 1. 注册成功了吗
python run.py scan

# 2. 状态能读吗（只读，不写盘）
python run.py status --target myclient

# 3. 预演
python run.py dry-run --target myclient

# 4. 真打
python run.py apply --target myclient

# 5. 幂等：再打一次，长度不该变
python run.py apply --target myclient

# 6. 还原：文件该和注入前逐字节相同
python run.py revert --target myclient
```

第 5、6 步是**必做**的。只测第 4 步会漏掉幂等和可逆性问题。

---

## 第五步：加断言

在 `selftest.py` 里加一组。参考 `t_targets_sandbox()`：

```python
def t_myclient():
    section("N. MyClient 靶点（沙箱）")

    d = tempfile.mkdtemp(prefix="forge-myclient-")
    try:
        home = os.path.join(d, ".myclient")
        os.makedirs(home)

        class A: pass
        a = A()
        for k in ("target","persona","policy","overlay","packs","passphrase",
                  "signal","full"):
            setattr(a, k, None)
        a.dir = home                      # 显式指定，不碰真实配置

        t = MyClientTarget(a, persona.build())

        # dry-run 不写盘
        before = os.listdir(home)
        t.apply(mode="dry-run")
        check("dry-run 不写盘", os.listdir(home) == before)

        # apply 写入
        t.apply(mode="apply")
        p = os.path.join(home, "MYCLIENT.md")
        check("apply 创建文件", os.path.isfile(p))
        check("含标记块", "<!-- FORGE:BEGIN -->" in open(p, encoding="utf-8").read())

        # 幂等
        n1 = os.path.getsize(p)
        t.apply(mode="apply")
        check("重复 apply 幂等", os.path.getsize(p) == n1)

        # 还原
        t.revert()
        check("revert 后文件被清掉或还原", ...)
    finally:
        shutil.rmtree(d, ignore_errors=True)
```

然后在 `main()` 的元组里加上函数名。

---

## 复杂情况

### 情况 A：注入点不是标准文件

参考 `t_codex.py`（TOML 顶层键）或 `t_dsh.py`（入口 + 技能文件）。这两个都重写了 `apply` / `revert`。

要点：

- 重写 `apply` 时**必须**处理 `dry` 分支，不能只在最后判断
- 重写 `revert` 时要覆盖所有写入过的路径
- `_injected_flag()` 也要重写，判据要认自己的标记

### 情况 B：写入失败要报错，不能静默

```python
if core.write_text(p, new):
    res["fixed"] += 1
else:
    res["err"] += 1
    res["notes"].append("写入失败（%s）" % errno_str(p))
```

**实测教训**：早期版本只在成功时 `done += 1`，失败静默丢弃 —— 结果"apply 说成功、其实一个字没写"，客户端重启后还是旧人格。

### 情况 C：目标目录不存在

基类的做法是**只创建本工具自己的落点**，不替用户建产品目录：

```python
if not os.path.isdir(home):
    core.ensure_dir(home)
```

### 情况 D：只有部分文件需要处理

比如"只动含 `<content_policy>` 的文件"：

```python
if n == 0:
    skipped += 1
    continue
```

不这样做会把用户的磁盘弄脏一大片（几十个无关模板都被追加内容），回滚时还得逐个判断。

---

## 检查清单

加完靶点，逐条确认：

- [ ] `python run.py scan` 能看到它
- [ ] `status` / `dry-run` 不改盘（用 `git status` 或目录快照验证）
- [ ] `apply` 两次，第二次字节数不变（幂等）
- [ ] `revert` 后与注入前逐字节相同（不是"看起来一样"）
- [ ] `revert` 在**没注入过**的文件上不出错
- [ ] 非 UTF-8 文件被跳过而不是写坏
- [ ] 写失败时 `res["err"]` 有计数、`notes` 有说明
- [ ] 加了自检断言
- [ ] README 的靶点表加了行

---

## 别做的事

| 反模式 | 为什么 |
|---|---|
| 改产品的安装目录 | 见 `pitfalls.md` #7。会把客户端搞到起不来 |
| 用 `persona_document()` 喂 `_merge` | 嵌套标记，累积成灾 |
| 用 `text[:i].rstrip("\n")` 拼回 | 破坏逐字节可逆性 |
| 靠 `any(r[0] == "ok")` 判注入 | "检测到安装目录"也是 ok |
| 写失败了不报错 | 假成功 |
