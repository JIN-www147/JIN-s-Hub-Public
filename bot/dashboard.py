"""
实时监控面板：一个本地网页，把「平台题目 / AI 对话过程 / 最终代码与评测结果」
放在同一屏里，答题时自动刷新，方便判断程序是否真的在正常工作。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LOCK = threading.Lock()

STATE: dict = {
    "account": "",
    "course": "",
    "practice_url": "",
    "chat_round": "",
    "step_no": 0,
    "step_name": "",
    "qtype": "",
    "index": 0,
    "total": 0,
    "desc": "",
    "framework": "",
    "ai_status": "idle",      # idle | asking | answering | done | error
    "ai_answer": "",
    "prompt_len": 0,
    "attempt": 0,
    "final_code": "",
    "eval_passed": None,
    "eval_detail": "",
    "logs": [],
    "started_at": time.time(),
    "updated_at": time.time(),
}


def update(**kw):
    with _LOCK:
        STATE.update(kw)
        STATE["updated_at"] = time.time()


def reset_step(**kw):
    with _LOCK:
        STATE.update({
            "step_no": 0, "step_name": "", "qtype": "", "desc": "", "framework": "",
            "ai_status": "idle", "ai_answer": "", "prompt_len": 0, "attempt": 0,
            "final_code": "", "eval_passed": None, "eval_detail": "",
        })
        STATE.update(kw)
        STATE["updated_at"] = time.time()


def log(text: str, kind: str = "info"):
    with _LOCK:
        STATE["logs"].append({"t": time.strftime("%H:%M:%S"), "text": text, "kind": kind})
        STATE["logs"] = STATE["logs"][-200:]
        STATE["updated_at"] = time.time()


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>AI 答题监控台</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; background: #0d1117; color: #c9d1d9;
    font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
    height: 100vh; display: flex; flex-direction: column; font-size: 13px;
  }
  header {
    display: flex; align-items: center; gap: 18px; padding: 10px 18px;
    background: linear-gradient(90deg,#161b22,#1c2430); border-bottom: 1px solid #30363d;
    flex-wrap: wrap;
  }
  header .brand { font-weight: 600; color: #58a6ff; letter-spacing: .5px; }
  .chip {
    background: #21262d; border: 1px solid #30363d; border-radius: 999px;
    padding: 3px 11px; font-size: 12px; color: #8b949e;
  }
  .chip b { color: #c9d1d9; font-weight: 600; }
  .chip.live { border-color: #238636; color: #3fb950; }
  .chip.wait { border-color: #9e6a03; color: #d29922; }
  .chip.bad  { border-color: #8b2c2c; color: #f85149; }
  #progress { flex: 1; min-width: 120px; height: 6px; background: #21262d; border-radius: 4px; overflow: hidden; }
  #progress > i { display: block; height: 100%; width: 0%; background: #3fb950; transition: width .3s; }

  main { flex: 1; display: grid; grid-template-columns: 1.15fr 1fr 1.05fr; gap: 1px; background: #30363d; min-height: 0; }
  section { background: #0d1117; display: flex; flex-direction: column; min-height: 0; }
  .title {
    padding: 8px 14px; font-size: 12px; letter-spacing: .6px; color: #8b949e;
    background: #161b22; border-bottom: 1px solid #21262d; display: flex; justify-content: space-between;
  }
  .title span:last-child { color: #484f58; }
  .body { flex: 1; overflow: auto; padding: 14px 16px; line-height: 1.75; }
  .body::-webkit-scrollbar { width: 8px; }
  .body::-webkit-scrollbar-thumb { background: #30363d; border-radius: 4px; }

  h4 { color: #58a6ff; font-size: 13px; margin: 14px 0 6px; padding-left: 8px; border-left: 3px solid #1f6feb; }
  p { margin: 6px 0; color: #adbac7; }
  li { margin: 3px 0 3px 6px; color: #adbac7; }
  pre {
    background: #161b22; border: 1px solid #21262d; border-radius: 6px;
    padding: 10px 12px; overflow: auto; font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 12px; line-height: 1.55; color: #a5d6ff; margin: 8px 0;
  }
  code { font-family: Consolas, monospace; background: #1c2128; padding: 1px 5px; border-radius: 4px; color: #79c0ff; }

  .ai-msg { white-space: pre-wrap; word-break: break-word; font-size: 12.5px; line-height: 1.7; color: #c9d1d9; }
  .status { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #484f58; }
  .dot.asking { background: #d29922; }
  .dot.answering { background: #3fb950; box-shadow: 0 0 8px #3fb950; animation: pulse 1.1s infinite; }
  .dot.done { background: #58a6ff; }
  .dot.error { background: #f85149; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }

  .pass { color: #3fb950; font-weight: 600; }
  .fail { color: #f85149; font-weight: 600; }
  .result-box { border: 1px solid #21262d; border-radius: 8px; padding: 10px 12px; margin: 8px 0; background: #10151c; }
  .result-box.pass { border-color: #238636; background: #0d1a10; }
  .result-box.fail { border-color: #8b2c2c; background: #1a0d0d; }
  .result-box.neutral { border-color: #30363d; background: #12171e; }
  .neutral { color: #8b949e; font-weight: 600; }
  .mono { font-family: Consolas, monospace; font-size: 12px; white-space: pre-wrap; }

  #logbox { font-family: Consolas, monospace; font-size: 11.5px; }
  #logbox div { padding: 1px 0; color: #7d8590; }
  #logbox div.ok { color: #3fb950; }
  #logbox div.err { color: #f85149; }
  #logbox div.ai { color: #58a6ff; }
  .empty { color: #484f58; font-style: italic; }
  .logs { border-top: 1px solid #21262d; background: #0b0f14; height: 148px; overflow: auto; padding: 8px 14px; }
</style>
</head>
<body>
<header>
  <div class="brand">AI 答题监控台</div>
  <div class="chip" id="c-acc">账号 <b>-</b></div>
  <div class="chip" id="c-course">实验 <b>-</b></div>
  <div class="chip" id="c-step">关卡 <b>-</b></div>
  <div class="chip" id="c-index">进度 <b>0 / 0</b></div>
  <div class="chip" id="c-round">对话 <b>-</b></div>
  <div id="progress"><i></i></div>
  <a class="chip" id="c-open" target="_blank" rel="noopener"
     style="display:none;text-decoration:none;cursor:pointer">↗ 打开平台实验页</a>
  <div class="chip" id="c-ai"><span class="dot"></span><span>空闲</span></div>
  <div class="chip" id="c-clock">00:00</div>
</header>

<main>
  <section>
    <div class="title"><span>平台题目原文</span><span id="t-qtype"></span></div>
    <div class="body" id="p-desc"><span class="empty">等待开始 …</span></div>
  </section>

  <section>
    <div class="title"><span>AI 对话过程（DeepSeek）</span><span id="t-len"></span></div>
    <div class="body" id="p-ai"><span class="empty">等待提问 …</span></div>
  </section>

  <section>
    <div class="title"><span>提交代码与评测结果</span><span id="t-attempt"></span></div>
    <div class="body" id="p-code"><span class="empty">暂无</span></div>
  </section>
</main>

<div class="logs"><div id="logbox"></div></div>

<script>
function esc(s) {
  return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function md(t) {
  let s = esc(t || "");
  s = s.replace(/```[a-zA-Z]*\n([\s\S]*?)```/g, (m, c) => "<pre>" + c.replace(/\n+$/, "") + "</pre>");
  s = s.replace(/!\[[^\]]*\]\([^)]*\)(\{\{[^}]*\}\})?/g, "");
  s = s.replace(/^#{1,6}\s+(.+)$/gm, "<h4>$1</h4>");
  s = s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/^\s*[-*]\s+(.+)$/gm, "<li>$1</li>");
  s = s.replace(/^&gt;\s?(.*)$/gm, "<p style='color:#8b949e'>$1</p>");
  s = s.replace(/\n{2,}/g, "</p><p>");
  return "<p>" + s + "</p>";
}
let last = {};
function setIfChanged(id, key, html) {
  if (last[key] === html) return;
  last[key] = html;
  document.getElementById(id).innerHTML = html;
}
function render(s) {
  document.getElementById("c-acc").innerHTML = "账号 <b>" + esc(s.account || "-") + "</b>";
  document.getElementById("c-course").innerHTML = "实验 <b>" + esc(s.course || "-") + "</b>";
  document.getElementById("c-step").innerHTML = "关卡 <b>第" + (s.step_no || "-") + "关 " + esc(s.step_name || "") + "</b>";
  document.getElementById("c-index").innerHTML = "进度 <b>" + (s.index || 0) + " / " + (s.total || 0) + "</b>";
  document.getElementById("c-round").innerHTML = "对话 <b>" + (s.chat_round || "-") + "</b>";
  document.querySelector("#progress > i").style.width =
    (s.total ? Math.min(100, (s.index / s.total) * 100) : 0) + "%";

  const map = {idle: ["", "空闲"], asking: ["asking", "已发送，等待回复"], answering: ["answering", "AI 正在输出"], done: ["done", "本关完成"], error: ["error", "出错"]};
  const [cls, txt] = map[s.ai_status] || ["", s.ai_status];
  document.getElementById("c-ai").innerHTML = '<span class="dot ' + cls + '"></span><span>' + txt + "</span>";

  const secs = Math.max(0, Math.floor((Date.now() / 1000) - (s.started_at || Date.now() / 1000)));
  document.getElementById("c-clock").textContent =
    String(Math.floor(secs / 60)).padStart(2, "0") + ":" + String(secs % 60).padStart(2, "0");

  document.getElementById("t-qtype").textContent = s.qtype || "";
  setIfChanged("p-desc", "desc", s.desc ? md(s.desc) : '<span class="empty">等待开始 …</span>');

  let ai = "";
  if (s.ai_answer) {
    ai = '<div class="ai-msg">' + md(s.ai_answer) + "</div>";
    const box = document.getElementById("p-ai");
    if (last.aiScroll !== s.ai_answer.length) {
      last.aiScroll = s.ai_answer.length;
      setTimeout(() => { box.scrollTop = box.scrollHeight; }, 10);
    }
  } else if (s.ai_status === "asking") {
    ai = '<span class="empty">已把题目发给 AI，等待首字返回 …</span>';
  } else if (s.ai_status === "error") {
    ai = '<span class="fail">' + esc(s.eval_detail || "AI 调用失败") + "</span>";
  } else {
    ai = '<span class="empty">等待提问 …</span>';
  }
  setIfChanged("p-ai", "ai", ai);
  document.getElementById("t-len").textContent = s.prompt_len ? ("prompt " + s.prompt_len + " 字") : "";

  let code = "";
  if (s.final_code) code = "<pre>" + esc(s.final_code) + "</pre>";
  if (s.eval_detail) {
    const st = s.eval_passed === true ? "pass" : (s.eval_passed === false ? "fail" : "neutral");
    const head = s.eval_passed === true ? "✓ 通过" : (s.eval_passed === false ? "✗ 未通过" : "— 未提交评测");
    code += '<div class="result-box ' + st + '">' +
            '<div class="' + st + '">' + head + "</div>" +
            '<div class="mono">' + esc(s.eval_detail) + "</div></div>";
  }
  setIfChanged("p-code", "code", code || '<span class="empty">暂无</span>');
  document.getElementById("t-attempt").textContent = s.attempt ? ("第 " + s.attempt + " 次作答") : "";

  const link = document.getElementById("c-open");
  if (s.practice_url) {
    link.href = s.practice_url;
    link.style.display = "";
  } else {
    link.style.display = "none";
  }

  const lb = document.getElementById("logbox");
  const html = (s.logs || []).slice(-60).map(l =>
    '<div class="' + (l.kind || "") + '">' + l.t + "  " + esc(l.text) + "</div>").join("");
  if (last.logs !== html) { last.logs = html; lb.innerHTML = html; lb.scrollTop = lb.scrollHeight; }
}
async function tick() {
  try {
    const r = await fetch("/state", {cache: "no-store"});
    render(await r.json());
  } catch (e) {}
}
tick();
setInterval(tick, 700);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/state"):
            with _LOCK:
                payload = json.dumps(STATE, ensure_ascii=False)
            body = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


_server: ThreadingHTTPServer | None = None
PORT = 0


def start(preferred: int = 8877) -> int:
    """启动面板服务，返回可用端口"""
    global _server, PORT
    if _server:
        return PORT
    for port in range(preferred, preferred + 12):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            _server = srv
            PORT = port
            return port
        except OSError:
            continue
    raise RuntimeError("监控面板端口被占用")


def url() -> str:
    return f"http://127.0.0.1:{PORT}/"
