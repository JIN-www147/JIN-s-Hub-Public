"""
美林 Tempo Talents 学习平台接口封装。

原理：
  1. 用 Playwright 启动一个持久化浏览器（保存 cookie，只需登录一次）。
  2. 登录通过页面表单完成（平台对账号密码做了加密，走页面最稳）。
  3. 抓题、提交、评测全部在页面上下文里用 fetch 发请求，
     自动带上 cookie 与平台要求的 X-Requested-With 头，比纯 requests 稳定。
"""
from __future__ import annotations

import json
import time
from typing import Any

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

BASE_DEFAULT = "http://YOUR_PLATFORM_HOST/tempo-talents/"
GATEWAY = "/gateway/tempo-talents/api"

# 在页面里发请求，返回 {status, json, text}
JS_FETCH = """
async ({url, method, data}) => {
  const opt = {
    method: method || 'GET',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    credentials: 'include'
  };
  if (method && method !== 'GET' && data) {
    opt.headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8';
    opt.body = new URLSearchParams(data).toString();
  }
  try {
    const r = await fetch(url, opt);
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) {}
    return { status: r.status, json: json, text: text.slice(0, 800), url: r.url };
  } catch (e) {
    return { status: -1, json: null, text: String(e), url: url };
  }
}
"""


class PlatformError(RuntimeError):
    pass


class Platform:
    def __init__(self, base_url: str, username: str, password: str,
                 classroom_id: str, profile_dir: str = "data/profile", headless: bool = False,
                 log=print):
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.classroom_id = classroom_id
        self.profile_dir = profile_dir
        self.headless = headless
        self.log = log
        self._pw: Playwright | None = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None

    # ------------------------------------------------------------------ 生命周期
    def start(self):
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            channel="msedge",
            headless=self.headless,
            viewport={"width": 1440, "height": 950},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(30000)

    def close(self):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()
            self.ctx = None
            self._pw = None

    # ------------------------------------------------------------------ 登录
    def is_logged_in(self) -> bool:
        r = self._fetch(f"{GATEWAY}/tt-common/user/current")
        return bool(r.get("json") and r["json"].get("success") and r["json"].get("data"))

    def whoami(self) -> dict:
        r = self._fetch(f"{GATEWAY}/tt-common/user/current")
        return (r.get("json") or {}).get("data") or {}

    def login(self, interactive_wait: int = 120) -> dict:
        """打开平台并登录；如果出现图形验证码，会等用户在窗口里手动输入。"""
        self.page.goto(self.base_url, wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(2500)

        if self.is_logged_in():
            me = self.whoami()
            self.log(f"已处于登录状态：{me.get('name')}（{me.get('code')}）")
            return me

        self.log("正在登录平台 …")
        try:
            self.page.wait_for_selector("input.el-input__inner", timeout=15000)
            inputs = self.page.query_selector_all("input.el-input__inner")
            if len(inputs) < 2:
                raise PlatformError("没有找到账号/密码输入框，页面结构可能变了")
            inputs[0].fill(self.username)
            inputs[1].fill(self.password)
            if len(inputs) >= 3:
                self.log("检测到图形验证码，请在浏览器窗口里手动填写验证码后点登录。")
                self.page.click("button.login-button")
                for _ in range(interactive_wait):
                    self.page.wait_for_timeout(1000)
                    if self.is_logged_in():
                        break
            else:
                self.page.click("button.login-button")
        except PlatformError:
            raise
        except Exception as exc:
            raise PlatformError(f"登录表单操作失败：{exc}") from exc

        for _ in range(30):
            self.page.wait_for_timeout(1000)
            if self.is_logged_in():
                me = self.whoami()
                self.log(f"登录成功：{me.get('name')}（{me.get('code')}）")
                return me
        raise PlatformError("登录失败：账号密码错误，或平台需要验证码/单点登录")

    # ------------------------------------------------------------------ 底层请求
    def _fetch(self, path: str, method: str = "GET", data: dict | None = None) -> dict:
        assert self.page is not None
        url = path if path.startswith("http") else self.base_url.rstrip("/").rsplit("/tempo-talents", 1)[0] + path
        if method == "GET" and data:
            from urllib.parse import urlencode
            url += ("&" if "?" in url else "?") + urlencode(data)
            data = None
        res = self.page.evaluate(JS_FETCH, {"url": url, "method": method, "data": data})
        return res

    def api(self, path: str, method: str = "POST", data: dict | None = None,
            retries: int = 2) -> Any:
        """调用 /gateway/tempo-talents/api 下的接口，返回 data 字段。"""
        full = f"{GATEWAY}{path}"
        last = None
        relogin_done = False
        for attempt in range(retries + 1):
            res = self._fetch(full, method, data)
            last = res
            if res["status"] == 200 and res.get("json") and res["json"].get("success"):
                return res["json"].get("data")

            body = res.get("text") or ""

            # 真的掉登录了：重登一次再试
            if res["status"] in (302, 401) and not relogin_done:
                self.log("会话已失效，重新登录 …")
                self.login()
                relogin_done = True
                continue

            # 返回的是网页而不是 JSON —— 说明路径不对（被前端兜底了），重试没有意义
            if body.startswith("<!DOCTYPE") or body.startswith("<html"):
                raise PlatformError(
                    f"接口 {path} 返回了网页而不是数据，路径可能写错了（检查是否有拼写/多余的层级）")

            # 业务报错（比如「实践环境尚未初始化」）也直接抛出，不要空转
            if res.get("json"):
                msg = res["json"].get("message") or res["json"].get("status")
                raise PlatformError(f"{path} -> {msg}")

            time.sleep(1)
        raise PlatformError(f"接口调用失败 {path} -> {json.dumps(last, ensure_ascii=False)[:300]}")

    # ------------------------------------------------------------------ 业务数据
    def list_classrooms(self) -> list[dict]:
        return self.api("/tt-classroom/list", "POST",
                        {"classroomStatus": "started", "page": 1, "rows": 50, "schoolYear": ""}) \
            .get("classroomListInfo", {}).get("rows", [])

    def list_courses(self, course_type: str = "practice") -> list[dict]:
        """课程实验列表（practice=课程实验，training=应用实训，combat=项目实战）

        平台返回的是「章节 -> 实验」两层结构，这里拍平成实验列表，
        并给每个实验补上所属章节名。
        """
        chapters = self.api("/tt-classroom/classroomCourseListDetail", "POST",
                            {"classroomId": self.classroom_id, "coursesName": "",
                             "coursesState": "allStudent", "coursesType": course_type}) or []
        out = []
        for i, ch in enumerate(chapters):
            ch_no = ch.get("sort") or (i + 1)
            for c in (ch.get("lastList") or []):
                c["_chapter"] = ch.get("coursesName")
                c["chapter_no"] = ch_no
                c["course_no"] = c.get("sort")
                c["course_code"] = f"{ch_no}-{c.get('sort')}"
                c["courseId"] = c.get("coursesId") or c.get("id")
                out.append(c)
        # 有些情况下接口直接返回实验列表
        if not out:
            out = [c for c in chapters if c.get("coursesType") != "chapter"]
        return out

    def list_steps(self, course_id: str, retries: int = 2) -> list[dict]:
        """某个实验下的关卡列表"""
        data = self.api("/tt-course-challenge/challenge/list", "GET",
                        {"courseId": course_id, "classroomId": self.classroom_id,
                         "page": 1, "rows": 1000}, retries=retries)
        return (data or {}).get("rows", [])

    def prepare_course(self, course_id: str, timeout: int = 150) -> list[dict]:
        """初始化该实验的实践环境（容器），等待就绪后返回关卡列表。

        平台的实验环境是按需启动的，没初始化时读关卡会报
        「实践环境尚未初始化或已关闭」。
        """
        try:
            return self.list_steps(course_id, retries=0)
        except PlatformError:
            pass

        self.log("   正在启动实践环境（容器），通常 10~40 秒 …")
        self.api("/tt-course-challenge/environment/initialize", "POST",
                 {"classroomId": self.classroom_id, "courseId": course_id})

        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            time.sleep(3)
            try:
                steps = self.list_steps(course_id, retries=0)
                if steps:
                    self.log("   环境就绪")
                    return steps
            except PlatformError as exc:
                last = exc
        raise PlatformError(f"实践环境初始化超时（{timeout}s）：{last}")

    def get_step(self, step_id: str) -> dict:
        """关卡详情：题目描述、代码框架、测试集等"""
        return self.api("/tt-course-challenge/challenge", "GET",
                        {"courseStepId": step_id, "classroomId": self.classroom_id})

    # ------------------------------------------------------------------ 提交评测
    def save_code(self, step_id: str, code_list: list[dict], elapsed: int = 5) -> None:
        payload = [{"codePath": c["codePath"], "code": c["code"], "name": c["name"]} for c in code_list]
        self.api("/tt-course-challenge/challenge/save", "POST", {
            "courseStepId": step_id,
            "newElapsedTime": str(elapsed),
            "classroomId": self.classroom_id,
            "codeJson": json.dumps(payload, ensure_ascii=False),
        })

    def start_verify(self, step_id: str) -> str:
        ticket = self.api("/tt-course-challenge/challenge/code/verification/start", "POST",
                          {"courseStepId": step_id, "classroomId": self.classroom_id})
        if not ticket:
            raise PlatformError("评测启动失败，未拿到 ticket")
        return ticket

    def poll_verify(self, step_id: str, ticket: str, timeout: int = 180) -> dict:
        """轮询评测结果，返回 {passFlag, trialSetList, ...}"""
        start = time.time()
        last = {}
        while time.time() - start < timeout:
            data = self.api("/tt-course-challenge/challenge/code/verification", "GET", {
                "classroomId": self.classroom_id, "courseStepId": step_id, "ticket": ticket})
            if isinstance(data, dict):
                last = data
                phase = (data.get("containerPhase") or "").lower()
                if phase in ("succeeded", "failed", "error") or data.get("trialSetList"):
                    return data
            time.sleep(2)
        return last

    def evaluate(self, step_id: str, code_list: list[dict], elapsed: int = 5) -> dict:
        """保存代码 + 评测 + 轮询，一步到位"""
        self.save_code(step_id, code_list, elapsed)
        # 平台把代码同步到实训练容器需要一点时间，立刻评测可能编译到旧代码
        time.sleep(1.5)
        ticket = self.start_verify(step_id)
        return self.poll_verify(step_id, ticket)

    # ------------------------------------------------------------------ 选择题
    def save_select(self, step_id: str, select_list: list[dict], elapsed: int = 5) -> None:
        self.api("/tt-course-challenge/challenge/save", "POST", {
            "courseStepId": step_id,
            "newElapsedTime": str(elapsed),
            "classroomId": self.classroom_id,
            "selectOrJudgeJson": json.dumps(select_list, ensure_ascii=False),
        })

    def verify_select(self, step_id: str) -> dict:
        """选择题评测是同步返回的，不需要轮询"""
        data = self.api("/tt-course-challenge/challenge/select-and-judge/verification", "POST",
                        {"classroomId": self.classroom_id, "courseStepId": step_id})
        return data if isinstance(data, dict) else {}

    def evaluate_select(self, step_id: str, select_list: list[dict], elapsed: int = 5) -> dict:
        self.save_select(step_id, select_list, elapsed)
        time.sleep(1.0)
        return self.verify_select(step_id)
