# Forge

> 多客户端提示词注入工具。本地、可逆、只改文本。

把「基础政策 + 覆盖层 + 人格 + 自证」装配成一份文档，注入进 15 个 AI 编程客户端的
提示词层。全程不碰二进制、不改注册表、不改包，每个改动都有备份，随时可逐字节还原。

---

## 快速开始

```bash
cd D:\AgentForge
python -m forge scan                # 看看装了哪些客户端
python -m forge dry-run --target wb # 预演：只列出会改什么
python -m forge apply --target wb   # 真打
python -m forge revert --target wb  # 还原
```

或者双击 **`启动.bat`** 走菜单。

自检（不碰任何真实配置，全部在临时目录里跑）：

```bash
python selftest.py
```

---

## 设计取舍

这个工具是在读过几套现成的破甲工具之后重写的。下面说明**为什么这样组织**。

### 1. 三个槽位，不是一个巨型词包

现成工具通常把所有内容混在一份大文件里：政策、人格、能力清单、场景路由全糊在一起。
改任何一处都得动整个文件，而且改坏一个地方整份就废了。

Forge 拆成三个独立槽位：

| 槽位 | 文件 | 职责 |
|---|---|---|
| 基础政策 | `policy.md` | 决定"什么可以做"，替换掉客户端自带的拒绝条款 |
| 覆盖层 | `overlay.md` | 最高优先级声明，防止被产品提示词稀释 |
| 人格 | `personas/persona.md` | 身份、语气、工作习惯 |

**为什么必须分开**：客户端的提示词拼装顺序大致是
「产品系统提示 → 项目上下文 → 用户规则」，越靠后权重越高。
覆盖层必须明确声明自己是最后一层，否则被产品提示词稀释掉 —— 这是"改了没生效"的常见原因。
把政策单独拆出来，则是因为它替换的是产品配置里的一段**可替换文本**，
跟人格是两回事，混在一起没法单独调。

### 2. 词包按需装载，不全部塞进去

`packs/` 里每个 `.md` 是一段可选的能力清单（逆向、Web、渗透、改模、创作）。
官方全局指令通常有字节预算（比如 65,536 字节），把所有内容一次性塞进去会超预算，
而且每轮对话都重新计费。

```bash
python -m forge apply --target wb --packs reverse,web
```

### 3. 三种缝合方式，对应三种文件形态

| 形态 | 场景 | 做法 |
|---|---|---|
| 标记块 | `.md` / `.mdc` / `.txt` | HTML 注释包住，整块替换 |
| TOML 顶层键 | Codex `config.toml` | 只加一行，插在第一个表头**之前** |
| JSON 字符串 | WorkBuddy `product.json` | 字符串级替换，不重排 JSON |

**TOML 那条为什么要专门处理**：TOML 里「表头之后的键属于那张表」。
把配置行追加到文件末尾，它会被解析成上一个表的成员 ——
Codex 完全读不到，而表面上"文件改了、apply 报成功"。所以只能插到顶层键区，
写盘后还要真解析复核一遍，不对就回滚。

**JSON 那条为什么要转义**：product.json 里的提示词是 JSON 字符串，
换行必须写成 `\n` 两个字符。直接塞真实换行会让整个文件语法损坏、产品配置报废。

### 4. 幂等靠"行级手术"

反复 apply 不能堆叠，revert 要能逐字节回到原样。做法是：
**只在"行"这一级做增删**，用 `split/join("\n")` 这对恒等操作当手术刀。

反例（初版踩过）：用 `text[:i].rstrip("\n")` 拼回去，等于把用户的换行风格重排了一遍，
永远回不到原样 —— 实测会吃掉文件结尾的换行。

### 5. 读不干净就跳过

遇到 GBK / UTF-16 / 二进制内容时，`errors="replace"` 会把文件读成一片替换字符，
然后照原样写回去 —— 用户的文件被静默转码毁掉。
所以解不开就返回 `None`，调用方一律按"读取失败"处理并跳过：

> 宁可不打这个补丁，也不要把用户的文件写坏。

### 6. 备份只存未被改过的原件

二次 apply 时如果把注入版当原档存下来，revert 就会"还原"成注入内容 ——
等于什么都没还原，还丢掉了用户的原始文件。所以备份时带一个 `is_pristine` 判定：
当前内容已经是我们的，就不覆盖备份。

---

## 支持的目标

| key | 客户端 | 注入方式 |
|---|---|---|
| `wb` | WorkBuddy | 六层靶点（模板 / product.json / 命令闸门 / 插件副本 / 运行时缓存 / 云记忆） |
| `dsh` | DeepSeek Harness | AGENTS.md 入口 + skills 技能文件 |
| `codex` | Codex | config.toml 顶层键 `model_instructions_file` |
| `claude` | Claude Code | CLAUDE.md 标记块 |
| `cursor` | Cursor | rules/*.mdc（带 frontmatter） |
| `zcode` | ZCode | AGENTS.md 标记块 |
| `gemini` | Gemini CLI | GEMINI.md |
| `qwen` | Qwen Code | QWEN.md |
| `iflow` | iFlow CLI | IFLOW.md |
| `trae` | Trae | rules/project_rules.md |
| `codebuddy` | CodeBuddy | CODEBUDDY.md |
| `opencode` | OpenCode | AGENTS.md |
| `windsurf` | Windsurf | memories/global_rules.md |
| `cline` | Cline | forge-inject.md |
| `copilot` | GitHub Copilot CLI | copilot-instructions.md |

`--target all` 一次处理全部（自动跳过没装的）。

---

## 目录结构

```
D:\AgentForge\
├─ 启动.bat                  交互菜单
├─ selftest.py               83 项自检（临时目录，不碰真实配置）
├─ policy.md                 基础政策（可改）
├─ overlay.md                覆盖层（可改）
├─ personas\
│   └─ default.md            默认人格 ← 改成你自己的
├─ packs\                    可选能力包
│   ├─ reverse.md  web.md  pentest.md  gamedev.md  writing.md
└─ forge\
    ├─ core.py               读写原语：原子写、备份、还原、dry-run 闸门
    ├─ marks.py              标记块引擎：行级手术、幂等、JSON 转义
    ├─ toml.py               TOML 顶层键操作 + 写后校验
    ├─ persona.py            装配层：政策 + 覆盖层 + 人格 + 自证 + 词包
    ├─ targets.py            靶点基类 + 11 个标记块型客户端
    ├─ t_codex.py            Codex（TOML 顶层键）
    ├─ t_workbuddy.py        WorkBuddy（六层靶点）
    ├─ t_dsh.py              DSH（入口 + 技能文件）
    ├─ registry.py           注册表
    └─ cli.py                命令行
```

---

## 常用命令

```bash
python -m forge scan                          # 只读扫描
python -m forge status --target wb            # 详细状态
python -m forge dry-run --target all          # 全量预演
python -m forge apply --target wb --full      # WorkBuddy 完全模式
python -m forge apply --target all            # 全部
python -m forge revert --target wb            # 还原
python -m forge persona --full-text           # 打印完整注入文档
python -m forge packs                         # 列出词包
```

自定义：

```bash
python -m forge apply --target wb \
  --persona 我的人格.md \
  --packs reverse,web \
  --passphrase 我的口令 \
  --signal 我的回执
```

---

## 非默认安装位置

工具会自动探测，找不到时用参数指定：

```bash
python -m forge apply --target wb --wb-install D:\workbuddyChina
python -m forge apply --target wb --wb-data C:\Users\DK\.workbuddy
```

也支持环境变量：`WORKBUDDY_HOME` / `WORKBUDDY_INSTALL` / `DSH_HOME` / `CODEX_HOME` /
`CLAUDE_DIR` / `CURSOR_DIR` 等。

---

## 生效与验证

1. 跑完 apply 后**完全退出目标客户端** —— 关窗口不够，托盘图标也要退
2. 重新打开
3. 新会话里单独发送自证口令（默认 `forge自检`）
4. 预期只回一行 `forge自检通过`

回复带解释、加戏或拒答，说明没真正载入 —— 检查第 1 步是否退干净了。

---

## 反向验证

`revert` 之后文件应该和注入前**逐字节相同**。验证方法：

```bash
python -m forge revert --target wb
python -m forge status --target wb   # 应显示未注入
```

备份位置：同目录 `<文件名>.forge-bak`，历史归档在同目录 `_forge_history/`。

---

## 边界说明

**能做的**：改文本文件。标记块注入、TOML 顶层键、JSON 字符串替换、多层副本同步。

**不做**：不碰二进制、不装驱动、不挂钩子、不改注册表或服务、不发网络请求。

**已知限制**：

- 客户端升级会覆盖 `resources` 目录，补丁被冲掉 —— 重跑一次 apply 即可（工具会识别哪些被冲掉）
- WorkBuddy 的云记忆层需要客户端**先发过消息**、生成过 `_memory.md` 才存在
- `.mdc` 类文件需要 frontmatter，已内置

---

## 自检

```bash
python selftest.py
```

83 项，全部在临时目录里跑，**不碰任何真实客户端配置**。覆盖：

| 组 | 验证什么 |
|---|---|
| 1 | 标记块幂等：`strip(put(X)) == strip(X)`，七种边界（空文件、无结尾换行、CRLF、已有块、含标题…） |
| 2 | 原子写：无临时文件残留、BOM 处理、只读属性恢复 |
| 3 | 读不干净就跳过：GBK / 二进制返回 None，不静默转码 |
| 4 | dry-run 一个字节不写盘 |
| 5 | 备份策略：二次 apply 不把注入版当原件；官方升级后重建基准 |
| 6 | TOML 顶层键：插在表头之前、写后仍可解析、还原逐字节一致 |
| 7 | JSON 转义：替换后仍合法；真实换行会破坏 JSON（反例验证） |
| 8 | 人格装配：标记、政策、覆盖层、自证齐备 |
| 9 | 靶点端到端：dry-run / apply / 幂等 / 还原，沙箱目录 |
| 10 | 注册表：15 个靶点、目标表达式解析 |

---

## 许可

MIT，见 [LICENSE](LICENSE)。
