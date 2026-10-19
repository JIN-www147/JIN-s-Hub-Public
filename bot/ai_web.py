"""
网页版 DeepSeek 驱动。

要点：
  * 一次运行只使用同一个对话（打开页面时新建，之后连续追问），
    不会每道题都开新会话。
  * 发送前记录上一轮回答，用来判断"新回答真的来了"，避免读到旧内容。
  * 支持 on_delta 回调，把流式文本实时推给监控面板。
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

from playwright.sync_api import BrowserContext, Page

PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "url": "https://chat.deepseek.com/",
        "input": [
            "textarea",
            "div[contenteditable='true']",
        ],
        "send": [
            "div[role='button'][aria-label*='发送']",
            "button[aria-label*='发送']",
            "button:has-text('发送')",
        ],
        "answer": [
            ".ds-markdown",
            "div[class*='ds-markdown']",
            "div[class*='markdown']",
        ],
        "busy": ["button[aria-label*='停止']", "div[role='button'][aria-label*='停止']"],
    },
}

JS_READ_ANSWER = """
(sels) => {
  for (const s of sels) {
    const els = [...document.querySelectorAll(s)]
      .filter(e => ((e.innerText || e.textContent || '')).trim().length > 0);
    if (!els.length) continue;

    // 克隆一份，去掉代码块顶部的「java / 复制 / 下载」工具栏，
    // 否则这些按钮文字会被当成代码第一行，导致编译失败。
    const clone = els[els.length - 1].cloneNode(true);
    clone.querySelectorAll(
      '[class*="code-block-banner"], [class*="banner-wrap"], [class*="toolbar"]'
    ).forEach(n => n.remove());

    const holder = document.createElement('div');
    holder.style.cssText = 'position:absolute;left:-99999px;top:0;width:900px;';
    holder.appendChild(clone);
    document.body.appendChild(holder);

    // 标签页在后台时 Chrome 会丢弃部分渲染内容，innerText 会变短，
    // 这时改用 textContent（代码块的换行仍然保留）。
    const inner = (holder.innerText || '').trim();
    const tc = (holder.textContent || '').trim();
    holder.remove();

    const out = inner.length >= tc.length * 0.9 ? inner : tc;
    if (out) return out;
  }
  return '';
}
"""

JS_MSG_COUNT = """
(sels) => {
  for (const s of sels) {
    const n = document.querySelectorAll(s).length;
    if (n > 0) return n;
  }
  return 0;
}
"""


class WebAI:
    def __init__(self, ctx: BrowserContext, provider: str = "deepseek",
                 timeout: int = 180, log=print,
                 on_delta: Optional[Callable[[str], None]] = None,
                 per_chat: int = 3):
        if provider not in PROVIDERS:
            raise ValueError(f"不支持的 AI：{provider}，可选 {list(PROVIDERS)}")
        self.cfg = PROVIDERS[provider]
        self.key = provider
        self.ctx = ctx
        self.timeout = timeout
        self.log = log
        self.on_delta = on_delta
        self.per_chat = max(1, int(per_chat or 0))   # 一个对话最多做几题
        self.page: Optional[Page] = None
        self._started = False
        self._q_count = 0          # 当前对话里已经做了几题
        self._recycled = 0         # 已回收的对话数

    # ------------------------------------------------------------------ 页面
    def open(self) -> Page:
        """首次调用时打开对话页并新建对话；之后复用，不再重开。"""
        if self.page and not self.page.is_closed():
            return self.page
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(30000)
        self.page.goto(self.cfg["url"], wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(4000)
        self._started = True
        self.log(f"{self.cfg['name']} 对话已建立（本次运行只用一个对话）")
        return self.page

    def new_chat(self):
        """手动重开对话（长时间运行后需要清上下文时才用）"""
        page = self.open()
        page.goto(self.cfg["url"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)
        self.log("已重开一个对话")

    # ------------------------------------------------------------------ 对话清理
    def list_chats(self) -> set[str]:
        """侧栏里已有的对话链接集合，用来识别本次新产生的对话"""
        page = self.open()
        try:
            return set(page.evaluate(
                """() => [...document.querySelectorAll('a[href*="/a/chat/s/"]')]
                        .map(a => a.getAttribute('href'))"""))
        except Exception:
            return set()

    def _delete_one(self, href: str) -> bool:
        page = self.open()
        item = page.locator(f'a[href="{href}"]')
        if item.count() == 0:
            return False
        it = item.first
        it.scroll_into_view_if_needed(timeout=5000)
        it.hover()
        page.wait_for_timeout(500)
        more = it.locator("div[role=button]").first
        if more.count() == 0:
            return False
        more.click(timeout=5000)
        page.wait_for_timeout(600)

        menu = page.get_by_text("删除", exact=True).last
        if menu.count() == 0:
            return False
        menu.click(timeout=5000)
        page.wait_for_timeout(800)

        confirm = page.get_by_text("删除该对话", exact=True).last
        if confirm.count() > 0 and confirm.is_visible():
            confirm.click(timeout=5000)
        page.wait_for_timeout(1200)
        return True

    def cleanup_since(self, before: set[str]) -> int:
        """删掉本次运行新产生的对话，返回删除条数"""
        now = self.list_chats()
        extra = [h for h in now if h not in before]
        if not extra:
            return 0
        deleted = 0
        for href in extra:
            try:
                if self._delete_one(href):
                    deleted += 1
            except Exception as exc:
                self.log(f"删除对话失败：{exc}")
        if deleted:
            self.log(f"已清理本次产生的 {deleted} 个 DeepSeek 对话")
        return deleted

    # ------------------------------------------------------------------ 对话轮换
    def begin_question(self) -> int:
        """每道新题开始时调一次。

        当前对话已经做满 per_chat 题时，先把它删掉、再开一个全新的对话，
        避免上下文越滚越长导致模型响应变慢。
        返回本题在这个对话里的序号（从 1 开始）。
        """
        if self._q_count >= self.per_chat:
            self.recycle_chat()
        self._q_count += 1
        return self._q_count

    def delete_current_chat(self) -> bool:
        """删掉当前正在用的那个对话"""
        page = self.open()
        m = re.search(r"/a/chat/s/([\w-]+)", page.url)
        if not m:
            return False
        return self._delete_one(f"/a/chat/s/{m.group(1)}")

    def recycle_chat(self) -> bool:
        """回收当前对话 + 开新对话"""
        ok = False
        try:
            ok = self.delete_current_chat()
        except Exception as exc:
            self.log(f"   回收对话失败：{exc}")

        page = self.open()
        page.goto(self.cfg["url"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        self._q_count = 0
        self._recycled += 1
        if ok:
            self.log(f"   已回收对话并新开一个（每 {self.per_chat} 题换一次）")
        else:
            self.log("   已新开一个对话（旧对话没删掉，结束时会统一清理）")
        return ok

    def check_login(self) -> bool:
        page = self.open()
        if "sign_in" in page.url or "/login" in page.url.lower():
            return False
        try:
            has_login_btn = page.evaluate("""() => {
                return [...document.querySelectorAll('button,[role=button],a,span,div')].some(e => {
                    const t = (e.innerText || '').trim();
                    if (!['登录', '立即登录', '去登录', '密码登录'].includes(t)) return false;
                    if (e.children.length > 0) return false;
                    const r = e.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
            }""")
            if has_login_btn:
                return False
        except Exception:
            pass
        return any(page.locator(s).count() > 0 for s in self.cfg["input"])

    def _locate_input(self):
        page = self.open()
        for sel in self.cfg["input"]:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.last.is_visible():
                return loc.last
        raise RuntimeError(f"{self.cfg['name']} 页面上找不到输入框（可能未登录或页面改版）")

    def _click_send(self) -> bool:
        page = self.open()
        for sel in self.cfg["send"]:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click()
                return True
        return False

    def _is_busy(self) -> bool:
        """是否还在生成。
        注意：必须判断「可见」，DOM 里存在但隐藏的按钮不算在生成，
        否则会永远卡在等待回答上。
        """
        page = self.open()
        for sel in self.cfg["busy"]:
            try:
                loc = page.locator(sel)
                n = min(loc.count(), 4)
                for i in range(n):
                    if loc.nth(i).is_visible():
                        return True
            except Exception:
                pass
        return False

    def _read(self) -> str:
        return (self.open().evaluate(JS_READ_ANSWER, self.cfg["answer"]) or "").strip()

    # ------------------------------------------------------------------ 提问
    def ask(self, prompt: str, quiet: bool = False, expect_files: int = 1) -> str:
        page = self.open()

        # 等上一轮彻底结束，避免插话
        for _ in range(20):
            if not self._is_busy():
                break
            page.wait_for_timeout(1000)

        prev_text = self._read()
        prev_count = page.evaluate(JS_MSG_COUNT, self.cfg["answer"])

        box = self._locate_input()
        box.click()
        page.wait_for_timeout(250)
        try:
            box.fill(prompt)
        except Exception:
            page.keyboard.insert_text(prompt)
        page.wait_for_timeout(500)

        if not self._click_send():
            page.keyboard.press("Enter")

        if not quiet:
            self.log(f"已发送到 {self.cfg['name']}，等待回答 …")
        try:
            return self._wait_answer(prev_text, prev_count, expect_files)
        except TimeoutError:
            # 偶尔页面会没收到消息，清空输入框再发一次
            self.log("没有收到回复，重新发送一次 …")
            page.wait_for_timeout(1500)
            try:
                box = self._locate_input()
                box.click()
                try:
                    box.fill("")
                except Exception:
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Delete")
                page.wait_for_timeout(300)
                box.fill(prompt)
                page.wait_for_timeout(500)
                if not self._click_send():
                    page.keyboard.press("Enter")
            except Exception as exc:
                self.log(f"重发失败：{exc}")
            return self._wait_answer(prev_text, prev_count, expect_files)

    @staticmethod
    def _looks_complete(text: str, expect_files: int) -> bool:
        """判断回答是否已经写完整，避免读到写了一半的代码。

        DeepSeek 页面上没有可靠的「生成中」标志（停止按钮根本不存在），
        所以只能靠内容特征 + 文本稳定时长来判断。
        """
        t = text or ""
        if expect_files > 1:
            if len(re.findall(r"===\s*FILE:", t)) < expect_files:
                return False
            return "}" in t.rstrip()[-300:]
        # 单文件：代码必须闭合
        tail = t.rstrip()
        if "Begin" in t and "End" not in t:
            return False
        if tail.endswith("}") or tail.endswith("```"):
            return True
        # End 之后还应该有类/方法的收尾大括号
        if "End" in t and "}" in t[t.rfind("End"):]:
            return True
        return False

    def _wait_answer(self, prev_text: str, prev_count: int, expect_files: int = 1) -> str:
        page = self.open()
        deadline = time.time() + self.timeout
        last = ""
        stable = 0
        started = False
        start_ts = time.time()

        while time.time() < deadline:
            page.wait_for_timeout(1000)
            text = self._read()
            count = page.evaluate(JS_MSG_COUNT, self.cfg["answer"])
            # 出现新文本，或回答条数变多，都算 AI 开始作答
            if text and (text != prev_text or count > prev_count):
                started = True
                if text != last:
                    last = text
                    stable = 0
                    if self.on_delta:
                        try:
                            self.on_delta(text)
                        except Exception:
                            pass
                else:
                    stable += 1
                    # 5 秒文本没有任何变化，且内容看起来完整，才认为生成结束
                    if stable >= 5 and self._looks_complete(text, expect_files):
                        return text
            elif not started and time.time() - start_ts > 60:
                # 35 秒还没有任何新内容，基本可以判定发送失败
                raise TimeoutError(
                    f"{self.cfg['name']} 60 秒内没有产生新的回答，"
                    "可能是未登录、页面卡住或输入框改版。请运行 python main.py ai-test 排查。")
        if last:
            return last
        raise TimeoutError(f"{self.cfg['name']} 等待回答超时（{self.timeout}s）")


# ---------------------------------------------------------------------- 工具函数
NOISE_LINES = {"java", "python", "c", "cpp", "js", "sql", "bash", "shell",
               "复制", "下载", "copy", "download", "展开", "收起", "代码"}


def extract_code(answer: str, language: str = "java") -> str:
    """从 AI 回答里提取代码，优先取 ``` 代码块，并清理代码块工具栏残留。"""
    if not answer:
        return ""

    blocks = re.findall(r"```(?:\w+)?\s*\n(.*?)```", answer, re.S)
    if blocks:
        # 有时 AI 会先给片段再给完整代码，拼接会导致内容重复，取最长的一段
        code = max(blocks, key=len).rstrip()
    else:
        lines = []
        for ln in answer.splitlines():
            if ln.strip().lower() in NOISE_LINES:
                continue
            if re.match(r"^\s*(以上|这段|说明|解释|运行结果|输出如下|注意[:：])", ln):
                continue
            lines.append(ln)
        code = "\n".join(lines).strip()

    # 去掉代码前面可能残留的解释文字，从文件开头切起
    m = re.search(r"^\s*(?:package\s|import\s|public\s+class\s|class\s|/\*{2,}\s*Begin)", code, re.M)
    if m and m.start() > 0:
        code = code[m.start():]
    return code.strip()
