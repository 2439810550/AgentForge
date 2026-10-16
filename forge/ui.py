# -*- coding: utf-8 -*-
"""Forge Web 控制台 —— 零依赖单文件 HTTP 服务。

只用标准库：http.server + json + threading。
前端是一个自包含的 HTML 页面（内嵌在 ui.py 里），不需要 npm、不需要构建。

设计要点：

  · **只读与写操作分离。** `/api/state` 纯读，`/api/action` 才写。
    所有写操作走 `Gate`，dry-run 在写入层兜底（见 core.py）。

  · **动作异步。** apply 要跑十几秒，同步会卡住浏览器。
    动作扔进后台线程，前端轮询 `/api/state` 拿进度。

  · **日志环形缓冲。** 核心的每次 log 都进缓冲区，前端增量拉取渲染。
"""

import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import core, persona, registry

# ════════════════════════════════════════════════════════════════════════
#  状态
# ════════════════════════════════════════════════════════════════════════

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.busy = False
        self.busy_label = ""
        self.last_action = ""
        self.last_done = ""
        self.logs = []          # 环形缓冲
        self.log_seq = 0
        self.build_state = {}
        self.persona_cache = None
        self.port = 0

    def log(self, msg):
        with self.lock:
            self.log_seq += 1
            self.logs.append({"seq": self.log_seq, "t": time.strftime("%H:%M:%S"),
                              "m": str(msg)})
            if len(self.logs) > 2000:
                del self.logs[:1000]

    def logs_since(self, seq):
        with self.lock:
            return [x for x in self.logs if x["seq"] > seq]


S = State()

# ════════════════════════════════════════════════════════════════════════
#  采集
# ════════════════════════════════════════════════════════════════════════

class _Args:
    """靶点类需要的一个最小 args 容器。"""
    target = "all"
    dir = None
    persona = None
    policy = None
    overlay = None
    packs = None
    passphrase = None
    signal = None
    full = False

    def __getattr__(self, name):
        return None


def build_assembly(payload=None):
    payload = payload or {}
    # 未显式传入的项，从 prefs.json 取（控制台上保存过的设置）
    prefs = load_prefs()
    packs = payload.get("packs")
    if packs is None:
        packs = prefs.get("packs")
    if packs is not None and not isinstance(packs, list):
        packs = [x.strip() for x in str(packs).split(",") if x.strip()]
    asm = persona.build(
        persona_path=payload.get("persona"),
        policy_path=payload.get("policy"),
        overlay_path=payload.get("overlay"),
        packs=packs,
        passphrase=payload.get("passphrase") or prefs.get("passphrase"),
        signal=payload.get("signal") or prefs.get("signal"),
    )
    return asm


def collect_state(payload=None):
    """采集所有靶点状态（只读）。"""
    asm = build_assembly(payload)
    args = _Args()
    out = {"version": getattr(__import__("forge"), "__version__", "0.1.0"),
           "targets": [], "loading": False,
           "passphrase": asm.passphrase, "signal": asm.signal,
           "core_file": "本地核心",
           "summary": asm.summary(),
           "sources": getattr(asm, "sources", {}),
           "busy": S.busy, "busy_label": S.busy_label,
           "last_action": S.last_action, "last_done": S.last_done,
           "log_seq": S.log_seq}

    tot = det = inj = 0
    for k in registry.keys():
        cls = registry.get(k)
        t = cls(args, asm)
        try:
            installed, rows, injected = t.detect()
        except Exception as e:
            installed, rows, injected = False, [("err", "检测异常：%s" % e, "")], False
        tot += 1
        det += 1 if installed else 0
        inj += 1 if injected else 0
        out["targets"].append({
            "key": k,
            "label": cls.label,
            "home": t.config_home(),
            "installed": bool(installed),
            "injected": bool(injected),
            "rows": [{"level": a, "note": b, "detail": c} for a, b, c in rows],
        })
    out["stats"] = {"total": tot, "detected": det, "injected": inj}
    out["pack_list"] = persona.list_packs()
    return out


def run_action(payload):
    """在后台线程里执行 apply / revert / dry-run。"""
    action = payload.get("action")
    targets = payload.get("targets") or []
    full = bool(payload.get("full"))

    # 空选不得静默变成 all。
    # 前端有 `if(!ts.length) toast(...)` 拦着，但 API 层没拦 —— 旧代码里
    # `targets: []` 会被 registry.resolve 展开成全部 15 个靶点。
    # 直接调 API（或前端出 bug）时会误伤全部客户端，revert 场景下尤其危险。
    if not targets:
        S.log("[!] 未指定目标 —— 已拒绝（空选不会变成 all）")
        return

    asm = build_assembly(payload)

    with S.lock:
        S.busy = True
        S.busy_label = "%s %s" % (action, ",".join(targets) or "all")
        S.last_action = S.busy_label
    S.log("═══ 开始 %s：%s ═══" % (action, ", ".join(targets) or "all"))

    try:
        keys_, bad = registry.resolve(",".join(targets) if targets else "all")
        if bad:
            S.log("[!] 未知目标：%s" % ", ".join(bad))
        # 全部目标都无效时直接收工，不能报“完成”并提示重启客户端 ——
        # 那会让人以为真打了，实际一个字节都没动。
        if not keys_:
            S.log("[!] 没有可处理的目标 —— 未执行任何写入")
            return

        dry = (action == "dry-run")
        if dry:
            core.Gate.enter_dry()

        args = _Args()
        args.full = full

        total_fixed = total_rev = total_err = 0
        for k in keys_:
            cls = registry.get(k)
            t = cls(args, asm)
            installed, _rows, _inj = t.detect()

            if action == "revert":
                if not installed:
                    continue
                res = t.revert("dry-run" if dry else "apply")
            else:
                # 未安装的也尝试（可能是"配置目录不存在但可创建"型靶点）
                t2 = cls(args, asm)
                if k == "wb":
                    res = t2.apply("dry-run" if dry else "apply", full=full)
                else:
                    res = t2.apply("dry-run" if dry else "apply")

            if res.get("skip") and not res.get("fixed") and not res.get("reverted"):
                continue

            S.log("── %s (%s)" % (cls.label, k))
            for n in res.get("notes", []):
                S.log("   " + str(n))
            for ln, lv in (res.get("layers") or {}).items():
                S.log("   · %s：%s" % (ln, lv))
            if action == "revert":
                S.log("   → 还原 %d，跳过 %d，错误 %d"
                      % (res.get("reverted", 0), res.get("skip", 0), res.get("err", 0)))
                total_rev += res.get("reverted", 0)
            else:
                S.log("   → 处理 %d，错误 %d" % (res.get("fixed", 0), res.get("err", 0)))
                total_fixed += res.get("fixed", 0)
            total_err += res.get("err", 0)

        if action == "revert":
            S.log("═══ 完成：共还原 %d 处，错误 %d ═══" % (total_rev, total_err))
        elif dry:
            S.log("═══ 预演结束：共 %d 处待处理（未写盘）═══" % total_fixed)
        else:
            S.log("═══ 完成：共处理 %d 处，错误 %d ═══" % (total_fixed, total_err))
            S.log("")
            S.log("接下来必须做这三步：")
            S.log("  1. 完全退出目标客户端（含托盘图标）")
            S.log("  2. 重新打开客户端")
            S.log("  3. 新会话里单独发送：%s" % asm.passphrase)
            S.log("     预期只回一行：%s" % asm.signal)

    except Exception as e:
        import traceback
        S.log("[!] 执行异常：%s" % e)
        for line in traceback.format_exc().splitlines():
            S.log("    " + line)
    finally:
        if payload.get("action") == "dry-run":
            core.Gate.exit_dry()
        else:
            core.Gate.reset()
        with S.lock:
            S.busy = False
            S.busy_label = ""
            S.last_done = time.strftime("%H:%M:%S")


# ════════════════════════════════════════════════════════════════════════
#  前端
# ════════════════════════════════════════════════════════════════════════

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Forge 控制台</title>
<style>
:root{
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2230; --line:#2d3441;
  --tx:#e6edf3; --tx2:#8b949e; --tx3:#6e7681;
  --ok:#3fb950; --warn:#d29922; --err:#f85149; --acc:#58a6ff; --acc2:#1f6feb;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);
  font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
  padding:0 0 40px}
a{color:var(--acc)}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px}

header{border-bottom:1px solid var(--line);background:var(--panel);
  position:sticky;top:0;z-index:20}
.hd{display:flex;align-items:center;gap:14px;padding:14px 0}
.logo{font-size:19px;font-weight:650;letter-spacing:.4px}
.logo b{color:var(--acc)}
.badge{font-size:11px;color:var(--tx3);border:1px solid var(--line);
  padding:2px 7px;border-radius:20px}
.spacer{flex:1}

.tabs{display:flex;gap:2px;padding:0 0 0 0}
.tab{padding:9px 16px;cursor:pointer;color:var(--tx2);font-size:13.5px;
  border-bottom:2px solid transparent;user-select:none}
.tab:hover{color:var(--tx)}
.tab.on{color:var(--acc);border-bottom-color:var(--acc);font-weight:550}

.bar{display:flex;flex-wrap:wrap;gap:9px;align-items:center;padding:16px 0 8px}
button{background:var(--panel2);color:var(--tx);border:1px solid var(--line);
  padding:8px 15px;border-radius:6px;cursor:pointer;font-size:13.5px;
  font-family:inherit;transition:.12s}
button:hover:not(:disabled){background:#252c3a;border-color:#3d4657}
button:disabled{opacity:.4;cursor:not-allowed}
button.pri{background:var(--acc2);border-color:var(--acc2);font-weight:550}
button.pri:hover:not(:disabled){background:#2a7de8}
button.danger{color:var(--err);border-color:#4a2a2a}
button.danger:hover:not(:disabled){background:#2a1a1a}
button.g{color:var(--ok);border-color:#24402a}
button.g:hover:not(:disabled){background:#16281a}

.stats{display:flex;gap:22px;padding:6px 0 14px;color:var(--tx2);font-size:13px;
  flex-wrap:wrap}
.stats b{color:var(--tx);font-weight:600}
.stats .hl{color:var(--ok)}

table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;color:var(--tx3);font-weight:500;font-size:12px;
  padding:8px 10px;border-bottom:1px solid var(--line);text-transform:uppercase;
  letter-spacing:.5px}
td{padding:10px;border-bottom:1px solid #1c2128;vertical-align:middle}
tr:hover td{background:#12171f}
tr.sel td{background:#132030}
td .k{color:var(--tx3);font-size:12px}
.pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11.5px;
  font-weight:550;white-space:nowrap}
.p-inj{background:#12301c;color:var(--ok);border:1px solid #1f5130}
.p-ins{background:#2e2610;color:var(--warn);border:1px solid #4d3f18}
.p-no{background:#1a1a1a;color:var(--tx3);border:1px solid #2a2a2a}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:7px}
.d-ok{background:var(--ok)}.d-warn{background:var(--warn)}
.d-err{background:var(--err)}.d-info{background:var(--tx3)}

input[type=checkbox]{width:15px;height:15px;accent-color:var(--acc);cursor:pointer}
.mono{font-family:ui-monospace,Consolas,"Courier New",monospace;font-size:12px;
  color:var(--tx2)}

.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));
  gap:12px;margin-top:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:14px;cursor:pointer}
.card:hover{border-color:#39414f}
.card.on{border-color:var(--acc);background:#111b2b}
.ch{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.ct{font-weight:600;font-size:14px}
.cpath{font-size:11.5px;color:var(--tx3);word-break:break-all;margin-bottom:9px;
  font-family:ui-monospace,Consolas,monospace}
.crow{font-size:12.5px;color:var(--tx2);padding:2px 0;display:flex;gap:7px}
.crow .lv{font-size:10px;padding:1px 5px;border-radius:3px;flex:0 0 auto;
  background:#1a1f28;color:var(--tx3);margin-top:2px}

#log{background:#0a0d12;border:1px solid var(--line);border-radius:8px;
  padding:13px;height:520px;overflow-y:auto;margin-top:14px;
  font-family:ui-monospace,Consolas,monospace;font-size:12.5px;line-height:1.65}
#log div{white-space:pre-wrap;word-break:break-word}
#log .t{color:var(--tx3);margin-right:9px}
#log .ok{color:var(--ok)}#log .wn{color:var(--warn)}#log .er{color:var(--err)}
#log .ac{color:var(--acc);font-weight:600}

.edit{margin-top:14px}
.edit textarea{width:100%;min-height:340px;background:#0a0d12;color:var(--tx);
  border:1px solid var(--line);border-radius:8px;padding:13px;
  font-family:ui-monospace,Consolas,monospace;font-size:12.5px;line-height:1.6;
  resize:vertical;outline:none}
.edit textarea:focus{border-color:var(--acc)}
.hint{color:var(--tx2);font-size:12.5px;margin:8px 0 12px}
.field{display:flex;gap:9px;align-items:center;margin:9px 0;flex-wrap:wrap}
.field label{color:var(--tx2);font-size:13px;min-width:74px}
.field input[type=text]{background:#0a0d12;color:var(--tx);border:1px solid var(--line);
  border-radius:6px;padding:7px 11px;font-size:13px;font-family:inherit;
  flex:1;min-width:190px;outline:none}
.field input[type=text]:focus{border-color:var(--acc)}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);
  background:var(--panel2);border:1px solid var(--line);padding:11px 20px;
  border-radius:8px;font-size:13.5px;opacity:0;transition:.22s;pointer-events:none;
  z-index:99;box-shadow:0 8px 26px rgba(0,0,0,.55)}
.toast.on{opacity:1}
.hide{display:none}
.warnbox{background:#2e2610;border:1px solid #4d3f18;color:#e8c46a;
  padding:11px 14px;border-radius:7px;font-size:12.5px;margin:12px 0;line-height:1.6}
</style>
</head>
<body>

<header><div class="wrap">
  <div class="hd">
    <div class="logo">Forge <b>控制台</b></div>
    <div class="badge" id="ver">v-</div>
    <div class="spacer"></div>
    <div class="badge" id="busy">空闲</div>
  </div>
  <div class="tabs">
    <div class="tab on" data-t="targets">客户端</div>
    <div class="tab" data-t="persona">人格与政策</div>
    <div class="tab" data-t="log">运行日志</div>
  </div>
</div></header>

<div class="wrap">

<!-- 客户端 -->
<div id="t-targets">
  <div class="bar">
    <button class="pri" id="b-dry">预演（不改盘）</button>
    <button class="pri" id="b-apply">执行注入</button>
    <button class="danger" id="b-revert">还原</button>
    <button id="b-refresh">刷新</button>
    <button id="b-all">全选</button>
    <button id="b-none">清空</button>
    <label class="mono" style="margin-left:6px;display:flex;align-items:center;gap:6px">
      <input type="checkbox" id="full"> 完全模式
    </label>
  </div>
  <div class="stats" id="stats"></div>
  <div class="warnbox">
    改的是磁盘文件，<b>运行中的客户端不会重新读取</b>。
    执行后必须完全退出目标客户端（含托盘图标），再重新打开才会生效。
  </div>
  <div id="list"></div>
</div>

<!-- 人格 -->
<div id="t-persona" class="hide">
  <div class="hint">
    三段分开维护：<b>基础政策</b>决定"什么可以做"，<b>覆盖层</b>声明最高优先级，
    <b>人格</b>决定身份与语气。改完点保存，再回客户端页执行一次注入。
  </div>
  <div class="field"><label>自证口令</label>
    <input type="text" id="pp" placeholder="forge自检"></div>
  <div class="field"><label>自证回执</label>
    <input type="text" id="sg" placeholder="forge自检通过"></div>
  <div class="field"><label>启用词包</label>
    <input type="text" id="pk" placeholder="reverse,web,pentest（逗号分隔，留空=不用）"></div>
  <div class="bar">
    <button class="pri" id="b-psave">保存设置</button>
    <button id="b-ppreview">预览装配结果</button>
  </div>
  <div id="pinfo" class="hint"></div>
  <div class="edit">
    <div class="hint" style="margin-bottom:6px"><b>基础政策</b>（policy.md）</div>
    <textarea id="ta-policy"></textarea>
  </div>
  <div class="edit">
    <div class="hint" style="margin-bottom:6px"><b>覆盖层</b>（overlay.md）</div>
    <textarea id="ta-overlay"></textarea>
  </div>
  <div class="edit">
    <div class="hint" style="margin-bottom:6px"><b>人格</b>（personas/persona.md）</div>
    <textarea id="ta-persona"></textarea>
  </div>
  <div class="bar">
    <button class="pri" id="b-save3">保存三份文件</button>
    <button id="b-reload">重新载入</button>
  </div>
</div>

<!-- 日志 -->
<div id="t-log" class="hide">
  <div class="bar">
    <button id="b-lclear">清屏</button>
    <button id="b-ltop">滚到顶部</button>
    <button id="b-lbot">滚到底部</button>
  </div>
  <div id="log"></div>
</div>

</div>

<div class="toast" id="toast"></div>

<script>
var ST = {targets:[], sel:{}, seq:0, persona:null};

function $(s){return document.querySelector(s)}
function $$(s){return Array.from(document.querySelectorAll(s))}
function toast(m){var t=$("#toast");t.textContent=m;t.classList.add("on");
  clearTimeout(t._x);t._x=setTimeout(function(){t.classList.remove("on")},2200)}

function api(path, body){
  return fetch(path, body?{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)}:{}).then(function(r){return r.json()});
}

/* ---------- 标签 ---------- */
$$(".tab").forEach(function(el){
  el.onclick=function(){
    $$(".tab").forEach(function(x){x.classList.remove("on")});
    el.classList.add("on");
    ["targets","persona","log"].forEach(function(t){
      $("#t-"+t).classList.toggle("hide", t!==el.dataset.t)});
  };
});

/* ---------- 渲染 ---------- */
function pill(t){
  if(t.injected) return '<span class="pill p-inj">已注入</span>';
  if(t.installed) return '<span class="pill p-ins">已安装</span>';
  return '<span class="pill p-no">未检测到</span>';
}

function render(){
  var s=ST.stats||{total:0,detected:0,injected:0};
  $("#ver").textContent="v"+(ST.version||"?");
  $("#stats").innerHTML =
    '共 <b>'+s.total+'</b> 个目标　·　已安装 <b>'+s.detected+
    '</b>　·　已注入 <b class="hl">'+s.injected+'</b>';

  var h='<table><thead><tr><th style="width:34px"></th><th>客户端</th>'+
        '<th style="width:110px">状态</th><th>配置目录</th><th style="width:60px">详情</th>'+
        '</tr></thead><tbody>';
  ST.targets.forEach(function(t){
    var sel=ST.sel[t.key]?"checked":"";
    h+='<tr class="'+(ST.sel[t.key]?"sel":"")+'" data-k="'+t.key+'">'+
       '<td><input type="checkbox" data-k="'+t.key+'" '+sel+'></td>'+
       '<td><b>'+t.label+'</b> <span class="k">'+t.key+'</span></td>'+
       '<td>'+pill(t)+'</td>'+
       '<td class="mono">'+(t.home||"-")+'</td>'+
       '<td><a href="#" data-exp="'+t.key+'">查看</a></td></tr>';
    if(t._open){
      h+='<tr><td></td><td colspan="4" style="padding:4px 10px 14px">';
      (t.rows||[]).forEach(function(r){
        var d=r.level==="ok"?"d-ok":r.level==="warn"?"d-warn":
              r.level==="err"?"d-err":"d-info";
        h+='<div class="crow"><span class="dot '+d+'"></span>'+
           '<span>'+r.note+'</span>'+
           (r.detail?'<span class="mono">'+r.detail+'</span>':'')+'</div>';
      });
      h+='</td></tr>';
    }
  });
  h+='</tbody></table>';
  $("#list").innerHTML=h;

  $$('#list input[type=checkbox]').forEach(function(cb){
    cb.onchange=function(){ ST.sel[cb.dataset.k]=cb.checked; render(); };
  });
  $$('#list a[data-exp]').forEach(function(a){
    a.onclick=function(e){e.preventDefault();
      var k=a.dataset.exp;
      ST.targets.forEach(function(t){ if(t.key===k) t._open=!t._open; });
      render();};
  });
  $$('#list tr[data-k]').forEach(function(tr){
    tr.onclick=function(e){ if(e.target.tagName==="INPUT"||e.target.tagName==="A")return;
      var k=tr.dataset.k; ST.sel[k]=!ST.sel[k]; render(); };
  });
}

function refresh(){
  return api("/api/state").then(function(d){
    ST.targets=(d.targets||[]).map(function(t){
      var old=ST.targets.find(function(x){return x.key===t.key});
      if(old&&old._open) t._open=true;
      return t;
    });
    ST.stats=d.stats; ST.version=d.version;
    ST.passphrase=d.passphrase; ST.signal=d.signal;
    ST.summary=d.summary; ST.sources=d.sources;
    $("#busy").textContent = d.busy ? ("运行中："+d.busy_label) : "空闲";
    $("#busy").style.color = d.busy ? "var(--warn)" : "var(--tx3)";
    var dis=d.busy;
    ["#b-dry","#b-apply","#b-revert"].forEach(function(x){$(x).disabled=dis});
    render(); pullLogs(d.log_seq);
    if(ST.persona===null && d.passphrase!==undefined){
      $("#pp").value=d.passphrase||""; $("#sg").value=d.signal||"";
      loadPersonaFiles();
    }
  }).catch(function(e){ toast("状态获取失败："+e) });
}

/* ---------- 日志 ---------- */
function colorize(m){
  var c="";
  if(/^═══/.test(m)) c="ac";
  else if(/\[\!\]|错误|异常|失败/.test(m)) c="er";
  else if(/\[--\]|跳过|警告/.test(m)) c="wn";
  else if(/\[ok\]|已完成|→ 处理/.test(m)) c="ok";
  return c?('<span class="'+c+'">'+esc(m)+'</span>'):esc(m);
}
function esc(s){return String(s).replace(/[&<>]/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;"}[c]})}

function pullLogs(seq){
  if(seq===undefined||seq<=ST.seq) return;
  var el=$("#log");
  // 首次拉全量
  var from = (ST.seq===0)?0:ST.seq;
  api("/api/logs?since="+from).then(function(d){
    (d.logs||[]).forEach(function(l){
      var div=document.createElement("div");
      div.innerHTML='<span class="t">'+l.t+'</span>'+colorize(l.m);
      el.appendChild(div);
    });
    if((d.logs||[]).length){ el.scrollTop=el.scrollHeight; }
    ST.seq=d.seq||seq;
  });
}

/* ---------- 动作 ---------- */
function selected(){
  return Object.keys(ST.sel).filter(function(k){return ST.sel[k]});
}
function act(action){
  var ts=selected();
  if(!ts.length){ toast("先勾选至少一个客户端"); return; }
  var full=$("#full").checked;
  if(action==="apply" && !confirm(
      "将对 "+ts.length+" 个客户端执行注入：\n  "+ts.join(", ")+
      "\n\n改的是磁盘文件，会有备份，可随时还原。\n\n继续？")) return;
  if(action==="revert" && !confirm(
      "将还原 "+ts.length+" 个客户端到注入前状态：\n  "+ts.join(", ")+"\n\n继续？")) return;
  var body={action:action,targets:ts,full:full};
  if($("#pp").value) body.passphrase=$("#pp").value;
  if($("#sg").value) body.signal=$("#sg").value;
  if($("#pk").value) body.packs=$("#pk").value;
  api("/api/action",body).then(function(d){
    if(!d.ok){ toast(d.err||"执行失败"); return; }
    toast((action==="dry-run"?"预演":action==="apply"?"注入":"还原")+"已开始");
    $$(".tab").forEach(function(x){x.classList.remove("on")});
    $$(".tab").forEach(function(x){ if(x.dataset.t==="log") x.classList.add("on") });
    ["targets","persona","log"].forEach(function(t){
      $("#t-"+t).classList.toggle("hide", t!=="log")});
    ST.seq = 0;
    $("#log").innerHTML = "";
    // 主动跟到动作结束，不依赖 1500ms 的全局轮询。
    // 旧版只 setTimeout(refresh,400) 一次 —— 如果动作在那之后才结束，
    // 界面会停在中间态（状态灯、统计数字都不对）。
    followAction();
  });
}

// 盯着 busy 直到结束，然后刷新状态与日志。
// 为什么需要它：act() 提交后服务端在后台线程里跑，前端不知道何时结束。
// 只靠全局轮询会在忙→闲的跳变上重建界面，但如果动作在两次轮询之间
// 就跑完了（比如单靶点 apply 只需几百毫秒），跳变会被漏掉。
function followAction(){
  var ticks = 0;
  var timer = setInterval(function(){
    ticks++;
    api("/api/busy").then(function(b){
      if(b.busy){
        if(ticks > 600) clearInterval(timer);   // 最多盯 5 分钟
        return;
      }
      clearInterval(timer);
      _wasBusy = false;     // 已处理过，别让全局轮询再刷一次
      refresh();
    }).catch(function(){});
  }, 400);
}

$("#b-dry").onclick=function(){act("dry-run")};
$("#b-apply").onclick=function(){act("apply")};
$("#b-revert").onclick=function(){act("revert")};
$("#b-refresh").onclick=function(){refresh(); toast("已刷新")};
$("#b-all").onclick=function(){ST.targets.forEach(function(t){ST.sel[t.key]=true});render()};
$("#b-none").onclick=function(){ST.sel={};render()};
$("#b-lclear").onclick=function(){$("#log").innerHTML=""};
$("#b-ltop").onclick=function(){$("#log").scrollTop=0};
$("#b-lbot").onclick=function(){$("#log").scrollTop=$("#log").scrollHeight};

/* ---------- 人格 ---------- */
function loadPersonaFiles(){
  api("/api/persona").then(function(d){
    $("#ta-policy").value=d.policy||"";
    $("#ta-overlay").value=d.overlay||"";
    $("#ta-persona").value=d.persona||"";
    $("#pinfo").innerHTML="政策 "+ (d.policy||"").length + " 字　·　覆盖层 "+
      (d.overlay||"").length + " 字　·　人格 "+(d.persona||"").length+" 字　·　词包："+
      ((d.packs||[]).join(", ")||"无");
  });
}
$("#b-reload").onclick=function(){loadPersonaFiles();toast("已重新载入")};
$("#b-psave").onclick=function(){
  var b={passphrase:$("#pp").value,signal:$("#sg").value,packs:$("#pk").value};
  api("/api/prefs",b).then(function(){toast("设置已保存");refresh()});
};
$("#b-save3").onclick=function(){
  api("/api/persona",{policy:$("#ta-policy").value,overlay:$("#ta-overlay").value,
    persona:$("#ta-persona").value}).then(function(d){
      if(d.ok){toast("三份文件已保存");loadPersonaFiles();refresh()}
      else toast(d.err||"保存失败");
    });
};
$("#b-ppreview").onclick=function(){
  api("/api/preview",{passphrase:$("#pp").value,signal:$("#sg").value,
    packs:$("#pk").value}).then(function(d){
    if(d.err){toast(d.err);return}
    var old=document.getElementById("pvwrap");
    if(old)old.remove();
    var w=document.createElement("div");
    w.id="pvwrap"; w.className="edit";
    w.innerHTML='<div class="hint" style="margin-bottom:6px"><b>装配结果预览</b>　'+
      '共 '+d.chars+' 字符</div>'+
      '<textarea readonly style="min-height:420px">'+esc(d.text)+'</textarea>';
    $("#pinfo").after(w);
    toast("预览已生成");
  });
};

/* ---------- 轮询 ---------- */
// 关键：必须记住「上一次是否忙碌」。
// 旧版用一个从未赋值的局部变量 wasBusy，判断写成 `d.log_seq > ST.seq`；
// 但 pullLogs() 结尾会把 ST.seq 抬到服务端当前值 —— 动作跑完那一刻两者相等，
// 条件恒为假 → refresh() 从不调用 → 「已注入」灯和统计数字卡在中间态。
// 这就是「执行注入后状态不改变」的原因。改成按忙→闲跳变刷新。
var _wasBusy = false;
setInterval(function(){
  api("/api/busy").then(function(b){
    $("#busy").textContent = b.busy ? ("运行中："+b.label) : "空闲";
    $("#busy").style.color = b.busy ? "var(--warn)" : "var(--tx3)";
    ["#b-dry","#b-apply","#b-revert"].forEach(function(x){$(x).disabled=b.busy});
    if(b.busy){
      _wasBusy = true;
      api("/api/state").then(function(d){ pullLogs(d.log_seq); }).catch(function(){});
      return;
    }
    if(_wasBusy){
      // 忙 → 闲：动作刚结束，强制拉全量日志并重建界面。
      // refresh() 内部会 pullLogs，不必重复调。
      _wasBusy = false;
      refresh();
      return;
    }
    // 空闲且没刚跑完：仅在服务端确实有新日志时跟上（比如别处触发的动作）
    api("/api/state").then(function(d){
      if(d.log_seq > ST.seq){ pullLogs(d.log_seq); refresh(); }
    }).catch(function(){});
  }).catch(function(){});
},1500);

refresh();
</script>
</body>
</html>
"""


# ════════════════════════════════════════════════════════════════════════
#  服务
# ════════════════════════════════════════════════════════════════════════

PREFS_FILE = None


def prefs_path():
    global PREFS_FILE
    if PREFS_FILE is None:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_state")
        os.makedirs(base, exist_ok=True)
        PREFS_FILE = os.path.join(base, "prefs.json")
    return PREFS_FILE


def load_prefs():
    try:
        with open(prefs_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(p):
    try:
        with open(prefs_path(), "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def persona_file_paths():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return {
        "policy": os.path.join(root, "policy.md"),
        "overlay": os.path.join(root, "overlay.md"),
        "persona": os.path.join(root, "personas", "persona.md"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ForgeConsole"

    def log_message(self, *a):
        pass    # 静音，日志走我们自己的缓冲

    # ---------------------------------------------------------------- 工具
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _html(self, text, code=200):
        body = text.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # 必须禁缓存：整个界面（含全部 JS）就靠这一个响应下发。
            # 不设的话浏览器会拿旧页面，于是改了前端逻辑但用户看到的还是旧的 ——
            # 实测症状：修复已部署、服务端发的页面含新逻辑，
            # 但浏览器里跑的还是旧 JS，多选还原仍然只刷新第一次。
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path

        if path in ("/", "/index.html"):
            return self._html(PAGE)

        if path == "/api/state":
            prefs = load_prefs()
            return self._json(collect_state(prefs) if prefs else collect_state())

        if path == "/api/busy":
            with S.lock:
                return self._json({"busy": S.busy, "label": S.busy_label})

        if path == "/api/logs":
            q = u.query
            since = 0
            for kv in q.split("&"):
                if kv.startswith("since="):
                    try:
                        since = int(kv[6:])
                    except Exception:
                        since = 0
            return self._json({"logs": S.logs_since(since), "seq": S.log_seq})

        if path == "/api/persona":
            fp = persona_file_paths()
            prefs = load_prefs()
            out = {"packs": (prefs.get("packs") or [])}
            for k, p in fp.items():
                try:
                    with open(p, encoding="utf-8") as f:
                        out[k] = f.read()
                except Exception:
                    out[k] = ""
            return self._json(out)

        return self._json({"err": "not found"}, 404)

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        req = self._read_body()

        if path == "/api/action":
            if S.busy:
                return self._json({"ok": False, "err": "有动作正在运行，等它跑完"})
            act = req.get("action")
            if act not in ("apply", "revert", "dry-run"):
                return self._json({"ok": False, "err": "未知动作：%s" % act})
            # 在边界就把空拦截掉，给前端一个明确的错误响应，
            # 而不是让它在后台变成一个“静默什么都不做”的空转。
            tgs = req.get("targets") or []
            if not tgs:
                return self._json({"ok": False, "err": "未选任何客户端"})
            t = threading.Thread(target=run_action, args=(req,), daemon=True,
                                 name="forge-action")
            t.start()
            return self._json({"ok": True, "action": act})

        if path == "/api/prefs":
            p = load_prefs()
            if "passphrase" in req:
                p["passphrase"] = req["passphrase"]
            if "signal" in req:
                p["signal"] = req["signal"]
            if "packs" in req:
                p["packs"] = [x.strip() for x in str(req["packs"]).split(",") if x.strip()]
            save_prefs(p)
            return self._json({"ok": True})

        if path == "/api/persona":
            fp = persona_file_paths()
            written = []
            for k in ("policy", "overlay", "persona"):
                if k in req:
                    try:
                        core.write_text(fp[k], req[k], make_dirs=True)
                        written.append(k)
                    except Exception as e:
                        return self._json({"ok": False, "err": "写 %s 失败：%s" % (k, e)})
            S.log("人格文件已更新：%s" % ", ".join(written))
            return self._json({"ok": True, "written": written})

        if path == "/api/preview":
            try:
                asm = build_assembly(req)
                txt = asm.persona_document()
                return self._json({"text": txt, "chars": len(txt)})
            except Exception as e:
                return self._json({"err": str(e)})

        return self._json({"err": "not found"}, 404)


def serve(port=8317, open_browser=True, host="127.0.0.1"):
    """启动控制台。端口被占时向后顺延。"""
    srv = None
    p = port
    for p in range(port, port + 25):
        try:
            srv = ThreadingHTTPServer((host, p), Handler)
            break
        except OSError:
            continue
    if srv is None:
        print("端口 %d-%d 都被占用，起不来。" % (port, port + 24))
        return 1

    S.port = p
    url = "http://%s:%d/" % (host, p)
    S.log("Forge 控制台已启动：%s" % url)
    S.log("工作目录：%s" % os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()

    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()

    print()
    print("  Forge 控制台")
    print("  " + "─" * 56)
    print("  地址：%s" % url)
    print("  按 Ctrl+C 退出")
    print()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n  已退出。")
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
    return 0
