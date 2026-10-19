"""
答题流程：抓题 -> 交给网页版 DeepSeek -> 取回代码 -> 写回平台 -> 评测 -> 失败带反馈重答。
所有中间状态都会同步推送到监控面板（bot/dashboard.py）。
"""
from __future__ import annotations

import html as html_lib
import re
import time

from . import dashboard
from .ai_web import extract_code

BEGIN_RE = re.compile(r"/\*{2,}\s*Begin\s*\*{2,}/")
END_RE = re.compile(r"/\*{2,}\s*End\s*\*{2,}/")

# 这些扩展名才是真正能改代码的题；只有 README.md 之类的属于「命令行操作题」
CODE_EXT = (".java", ".py", ".c", ".cpp", ".h", ".js", ".ts", ".sql", ".sh", ".go", ".php")


def is_codable(codes: list[dict]) -> bool:
    return any((c.get("codePath") or "").lower().endswith(CODE_EXT) for c in codes)


def strip_markdown_images(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)\{\{[^}]*\}\}", "", text or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    return text


# ---------------------------------------------------------------------- 选择题
def html_to_text(s: str) -> str:
    """把题干 HTML 转成可读纯文本，保留代码换行。"""
    if not s:
        return ""
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p\s*>", "\n", s)
    s = re.sub(r"(?i)</div\s*>", "\n", s)
    s = re.sub(r"(?i)</pre\s*>", "\n", s)
    s = re.sub(r"(?i)</(code|span|font)\s*>", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_lib.unescape(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


TYPE_CN = {
    "single_choice": "单选题",
    "multiple_choice": "多选题",
    "judge": "判断题",
}


def build_select_prompt(items: list[dict], feedback: str | None = None) -> str:
    parts = ["下面是一组实训选择题，请作答。", ""]
    for i, q in enumerate(items, 1):
        qtype = TYPE_CN.get(q.get("type"), "选择题")
        parts.append(f"【第{i}题】（{qtype}）")
        parts.append(html_to_text(q.get("title")))
        opts = q.get("content") or []
        for j, opt in enumerate(opts):
            parts.append(f"{chr(65 + j)}. {html_to_text(str(opt))}")
        parts.append("")

    parts += [
        "【作答要求】",
        "1. 严格按下面的格式输出，每题一行，不要解释、不要复述题目：",
        "   1. B",
        "   2. AC",
        "2. 多选题写多个字母（如 AC），单选题只写一个字母，判断题 A=正确、B=错误。",
        "3. 题号必须和上面的题号一致，每题都要作答，不能漏。",
    ]
    if feedback:
        parts += ["", "【上次作答有错，请注意】", feedback]
    return "\n".join(parts)


def parse_choice_answers(text: str, n: int) -> list[str]:
    """从 AI 回答里解析出每题的字母答案，返回长度 n 的列表。"""
    answers: dict[int, str] = {}
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(?:第)?\s*(\d+)\s*[\.、:：)）]?\s*([A-Fa-f][A-Fa-f,\s]*)$", line.strip())
        if m:
            no = int(m.group(1))
            letters = re.sub(r"[^A-Fa-f]", "", m.group(2)).upper()
            if 1 <= no <= n and letters:
                answers[no] = "".join(sorted(set(letters)))

    if len(answers) < n:
        # 兜底：整段里按顺序抓独立的字母行
        pool = re.findall(r"^\s*([A-F]{1,4})\s*$", text or "", re.M)
        idx = 1
        for no in range(1, n + 1):
            if no in answers:
                continue
            while idx - 1 < len(pool):
                cand = pool[idx - 1]
                idx += 1
                answers[no] = cand
                break
    return [answers.get(i, "") for i in range(1, n + 1)]


def letters_to_answer(letters: str, qtype: str) -> str:
    """把字母答案转成平台要的格式（字符串索引）。"""
    idxs = [ord(ch) - 65 for ch in (letters or "").upper() if "A" <= ch <= "F"]
    if not idxs:
        return ""
    if qtype == "multiple_choice":
        return ",".join(str(i) for i in idxs)
    return str(idxs[0])


FILE_MARK = re.compile(r"^\s*===\s*FILE:\s*(\S+?)\s*===\s*$", re.M)


def split_multi(answer: str) -> dict:
    """解析 ===FILE: 路径=== 形式的多文件输出"""
    out = {}
    marks = list(FILE_MARK.finditer(answer or ""))
    for i, m in enumerate(marks):
        path = m.group(1).strip()
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(answer)
        body = answer[start:end]
        body = re.sub(r"^\s*```\w*\s*$", "", body, flags=re.M)
        body = re.sub(r"^\s*```\s*$", "", body, flags=re.M)
        body = body.strip()
        if body:
            out[path] = body
    return out


def match_path(ai_files: dict, path: str):
    name = path.split("/")[-1]
    for k, v in ai_files.items():
        kk = k.strip().lstrip("./")
        if kk == path or kk == name or kk.endswith("/" + name) or path.endswith("/" + kk):
            return v
    return None


def strip_file_marks(text: str) -> str:
    """清掉可能残留的 ===FILE: xxx=== 标记行，避免混进代码里。"""
    if not text:
        return text
    text = re.sub(r"^\s*===\s*FILE:.*?===\s*$", "", text, flags=re.M)
    return text.strip()


def fix_public_class(code: str, path: str) -> str:
    """Java 要求 public class 名与文件名一致，AI 有时会写错，这里强制对齐。"""
    if not code:
        return code
    fname = path.split("/")[-1].rsplit(".", 1)[0]
    m = re.search(r"public\s+(?:final\s+|abstract\s+)?class\s+(\w+)", code)
    if m and m.group(1) != fname:
        code = code[:m.start(1)] + fname + code[m.end(1):]
    return code


def build_prompt(step: dict, feedback: str | None = None, seq_no: int = 0) -> str:
    """把平台题目拼成给 AI 的指令"""
    title = step.get("title") or ""
    course = step.get("courseName") or ""
    desc = strip_markdown_images(step.get("description") or "")
    codes = (step.get("questionInfo") or {}).get("questionCodeList") or []
    trials = step.get("trialSetList") or []

    head = "下面是一道新的实训编程题，请只针对这一题作答，忽略之前对话里的其它题目。"
    if seq_no:
        head = f"（本次连续对话的第 {seq_no} 题）" + head

    parts = [
        head,
        "",
        f"【课程】{course}",
        f"【关卡】{title}",
        "",
        "【题目说明】",
        desc.strip()[:6000],
        "",
    ]
    for c in codes:
        parts += [f"【待补全代码：{c.get('codePath')}】", "```java", c.get("code", ""), "```", ""]

    visible = [t for t in trials if t.get("expectedOutput")]
    if visible:
        parts.append("【测试数据】")
        for i, t in enumerate(visible, 1):
            parts.append(f"  用例{i}: 输入={t.get('input') or '（无）'} | 预期输出={t['expectedOutput']}")
        exp0 = visible[0]["expectedOutput"]
        if exp0.endswith("\n"):
            parts.append("  注意：预期输出的**最后一行也有换行**，请所有行都用 System.out.println，"
                         "不要用 System.out.print。")
        else:
            parts.append("  注意：预期输出的**最后一行没有换行**，最后一行请用 System.out.print。")
        parts.append("")

    shared = [
        "2. 必须原样保留 /********* Begin *********/ 和 /********* End *********/ 两行注释，",
        "   把你的实现写在它们之间；Begin/End 之外的 package、import、类与方法签名不要改动。",
        "3. 输出必须与预期逐字符一致：注意中英文标点、每行缩进的空格数、以及末尾的换行数量。",
    ]
    if len(codes) > 1:
        parts.append("【输出要求】本关有 %d 个文件，请分别输出每个文件的完整内容。" % len(codes))
        parts.append("格式严格如下（每个文件一段，路径必须与下面完全一致，不要用 ``` 包裹）：")
        for c in codes:
            parts.append(f"===FILE: {c.get('codePath')}===")
            parts.append("（这里放该文件的完整代码）")
            parts.append("")
        parts += shared
        parts.append("4. 每个文件的 public class 名必须与文件名一致。")
    else:
        parts.append("【输出要求】")
        parts += ["1. 直接输出完整的、可编译运行的 Java 代码，不要解释、不要客套话。"] + shared
        parts.append("4. 只用一个 ```java 代码块输出最终代码。")
    if feedback:
        parts += ["", "【上次提交未通过，请修正】", feedback]
    return "\n".join(parts)


def merge_into_framework(framework: str, ai_code: str) -> str:
    """把 AI 给的代码合进原始框架。

    - AI 给的是完整文件（含 package / public class）-> 整体替换，避免嵌套错乱
    - AI 只给了片段 -> 塞进原框架的 Begin/End 之间
    """
    ai_code = (ai_code or "").strip()
    if not ai_code:
        return framework

    is_full_file = bool(
        re.search(r"^\s*package\s", ai_code, re.M)
        or re.search(r"^\s*(?:public\s+|final\s+|abstract\s+)*class\s+\w+", ai_code, re.M)
    )
    if is_full_file:
        return ai_code

    if not (BEGIN_RE.search(framework) and END_RE.search(framework)):
        return ai_code

    b = BEGIN_RE.search(framework)
    e = END_RE.search(framework)
    body = ai_code
    b2 = BEGIN_RE.search(body)
    if b2:
        body = body[b2.end():]
    e2 = END_RE.search(body)
    if e2:
        body = body[:e2.start()]
    body = "\n".join("    " + ln if ln.strip() else ln for ln in body.splitlines())
    return framework[:b.end()] + "\n" + body + "\n" + framework[e.start():]


def diff_hint(expected: str, actual: str) -> str:
    """定位第一个不同的字符，肉眼看着一样时特别有用。"""
    e, a = expected or "", actual or ""
    if e == a:
        return ""
    for i in range(min(len(e), len(a))):
        if e[i] != a[i]:
            ctx = e[max(0, i - 8):i + 8]
            return (f"第 {i} 个字符开始不同：预期 {e[i]!r}，实际 {a[i]!r}"
                    f"（预期片段 {ctx!r}）")
    if len(e) > len(a):
        return f"实际输出少了结尾：{e[len(a):]!r}"
    return f"实际输出多了结尾：{a[len(e):]!r}"


def summarize_result(result: dict) -> tuple[bool, str]:
    """返回 (是否通过, 可读反馈)"""
    if not isinstance(result, dict):
        return False, "评测无返回"
    passed = bool(result.get("passFlag"))
    lines = []
    for t in result.get("trialSetList") or []:
        if not t.get("isTrailSetVisible", True):
            continue
        ok = "✓" if t.get("passed") else "✗"
        # 不要 strip：平台是逐字符比对，首尾换行/空格本身就是差异点
        exp = t.get("expectedOutput") or ""
        real = t.get("realOutput") or ""
        lines.append(f"{ok} 预期: {exp!r}\n  实际: {real!r}")
        if not t.get("passed"):
            hint = diff_hint(exp, real)
            if hint:
                lines.append(f"  → {hint}")
    if not passed and not lines:
        msg = result.get("message") or result.get("containerPhase") or ""
        lines.append(str(msg))
    return passed, "\n".join(lines)


class Solver:
    def __init__(self, platform, ai, max_retry: int = 2, log=print):
        self.platform = platform
        self.ai = ai
        self.max_retry = max_retry
        self.log = log
        self.dry_run = False
        self._seq = 0

    def _say(self, text: str, kind: str = "info"):
        self.log(text)
        dashboard.log(text, kind)

    def _begin_round(self) -> int:
        """开始一道新题：必要时先回收旧对话、再开新的"""
        round_no = 1
        try:
            round_no = self.ai.begin_question()
        except Exception:
            pass
        n_per = getattr(self.ai, "per_chat", 0) or 0
        if n_per:
            dashboard.update(chat_round=f"{round_no}/{n_per}")
            if round_no == 1 and getattr(self.ai, "_recycled", 0) > 0:
                self._say("（本关使用新对话）")
        return round_no

    # ------------------------------------------------------------------ 选择题
    def solve_select(self, step: dict, name: str) -> bool:
        step_id = step.get("courseStepId") or ""
        items = (step.get("questionInfo") or {}).get("selectOrJudgeList") or []
        if not items or not step_id:
            self._say("跳过：没有读到选择题内容", "err")
            dashboard.update(ai_status="done")
            return False

        preview = []
        for i, q in enumerate(items, 1):
            preview.append(f"【第{i}题】{TYPE_CN.get(q.get('type'), '选择题')}\n" + html_to_text(q.get("title")))
            for j, opt in enumerate(q.get("content") or []):
                preview.append(f"{chr(65 + j)}. {html_to_text(str(opt))}")
            preview.append("")
        dashboard.update(desc="\n".join(preview), qtype="选择题")

        feedback = None
        for attempt in range(self.max_retry + 1):
            prompt = build_select_prompt(items, feedback)
            dashboard.update(ai_status="asking", ai_answer="", prompt_len=len(prompt),
                             attempt=attempt + 1, eval_detail="", eval_passed=None)
            self._say(f"发送 {len(items)} 道选择题给 DeepSeek（第 {attempt + 1} 次）")

            try:
                answer = self.ai.ask(prompt, expect_files=1)
            except Exception as exc:
                self._say(f"AI 调用失败：{exc}", "err")
                if attempt < self.max_retry:
                    time.sleep(3)
                    continue
                dashboard.update(ai_status="error", eval_detail=str(exc))
                return False

            letters = parse_choice_answers(answer, len(items))
            dashboard.update(final_code="\n".join(
                f"第{i}题 → {lt or '（未作答）'}" for i, lt in enumerate(letters, 1)))

            if not all(letters):
                self._say("有题目没给出答案，重试", "err")
                feedback = "上次有题目没有作答，请每题都给一个选项字母。"
                continue

            payload = [{
                "questionId": q.get("questionId"),
                "type": q.get("type"),
                "title": q.get("title"),
                "content": q.get("content"),
                "answer": letters_to_answer(lt, q.get("type")),
                "passed": False,
            } for q, lt in zip(items, letters)]

            result = self.platform.evaluate_select(step_id, payload)
            if result.get("passFlag"):
                self._say("✓ 通过", "ok")
                dashboard.update(eval_passed=True, eval_detail="全部答对",
                                 ai_status="done")
                return True

            rmap = {t.get("questionId"): t for t in (result.get("trialSetList") or [])}
            wrong = [i for i, q in enumerate(items, 1)
                     if not (rmap.get(q.get("questionId")) or {}).get("passed", False)]
            detail = f"答错：第 {', '.join(map(str, wrong))} 题" if wrong else "未全部答对"
            self._say("✗ " + detail, "err")
            dashboard.update(eval_passed=False, eval_detail=detail, ai_status="done")
            feedback = f"上次第 {', '.join(map(str, wrong))} 题答错了，请重新仔细判断这些题目。"
        return False

    def _answer_ok(self, ans: str, n_files: int) -> bool:
        """回答是否已经写完整"""
        ans = ans or ""
        if n_files > 1:
            files = split_multi(ans)
            return len(files) >= n_files and all(v.rstrip().endswith("}") for v in files.values())
        tail = ans.rstrip()
        if "Begin" in ans and "End" not in ans:
            return False
        if tail.endswith("}") or tail.endswith("```"):
            return True
        if "End" in ans and "}" in ans[ans.rfind("End"):]:
            return True
        return False

    def _ensure_complete(self, answer: str, n_files: int) -> str:
        """确认回答已经写完整，否则继续读（最多约 30 秒）。"""
        read = getattr(self.ai, "_read", None)
        if read is None:
            return answer
        for _ in range(15):
            if self._answer_ok(answer, n_files):
                return answer
            time.sleep(2)
            try:
                more = read()
            except Exception:
                break
            if more and len(more) > len(answer):
                answer = more
        return answer

    def solve_step(self, step_id: str, step_name: str = "", course: str = "", seq: int = 0) -> bool:
        step = self.platform.get_step(step_id)
        qtype = step.get("questionType")
        name = step.get("title") or step_name
        codes = (step.get("questionInfo") or {}).get("questionCodeList") or []
        framework = codes[0].get("code", "") if codes else ""

        dashboard.reset_step(
            course=course or step.get("courseName") or "",
            step_no=seq, step_name=name,
            qtype=step.get("questionTypeName") or qtype or "",
            desc=step.get("description") or "",
            framework=framework,
        )
        self._say(f"── 关卡：{name}（{step.get('questionTypeName') or qtype}）")

        if qtype == "select":
            round_no = self._begin_round()
            return self.solve_select(step, name)

        if qtype != "code":
            self._say(f"跳过：不支持的题型（{qtype}）", "err")
            dashboard.update(ai_status="done")
            return False

        if not codes:
            self._say("跳过：题目没有代码文件", "err")
            dashboard.update(ai_status="done")
            return False

        if not is_codable(codes):
            files = "、".join(c.get("codePath") or "?" for c in codes)
            self._say(f"跳过：这是命令行/终端类关卡（文件：{files}），"
                      "需要在实验终端里手动敲命令，不是改代码能过的", "err")
            dashboard.update(ai_status="done", eval_detail=f"命令行类关卡，已跳过（{files}）")
            return False

        self._seq += 1
        round_no = self._begin_round()

        feedback = None
        for attempt in range(self.max_retry + 1):
            prompt = build_prompt(step, feedback, seq_no=self._seq)
            dashboard.update(ai_status="asking", ai_answer="", prompt_len=len(prompt),
                             attempt=attempt + 1, eval_detail="", eval_passed=None)
            self._say(f"发送题目给 DeepSeek（第 {attempt + 1} 次）")

            try:
                answer = self.ai.ask(prompt, expect_files=len(codes))
            except Exception as exc:
                self._say(f"AI 调用失败：{exc}", "err")
                if attempt < self.max_retry:
                    self._say("换一次再试 …", "err")
                    time.sleep(3)
                    continue
                dashboard.update(ai_status="error", eval_detail=str(exc))
                return False

            # DeepSeek 的回复是流式的，偶尔会读到还没写完的内容，
            # 这里做一次完整性校验，不够完整就再多读几次。
            answer = self._ensure_complete(answer, len(codes))

            ai_code = extract_code(answer)
            if not ai_code:
                self._say("AI 没有返回可用代码，重试", "err")
                feedback = "上次没有按格式输出代码，请按要求输出完整代码。"
                continue

            if len(codes) == 1:
                codes[0]["code"] = merge_into_framework(codes[0].get("code", ""), ai_code)
            else:
                ai_files = split_multi(answer)
                hit = 0
                for c in codes:
                    body = match_path(ai_files, c.get("codePath", ""))
                    if body:
                        c["code"] = merge_into_framework(c.get("code", ""), body)
                        hit += 1
                if hit == 0:
                    # AI 没按多文件格式输出：只改第一个文件，并清掉可能混入的标记
                    idx = next((i for i, c in enumerate(codes) if BEGIN_RE.search(c.get("code", ""))), 0)
                    codes[idx]["code"] = merge_into_framework(
                        codes[idx].get("code", ""), strip_file_marks(ai_code))
                    self._say(f"AI 没按多文件格式输出，只更新了 {codes[idx].get('codePath')}", "err")

            # 兜底：清掉残留标记 + 保证 public class 名与文件名一致
            for c in codes:
                c["code"] = fix_public_class(strip_file_marks(c["code"]), c.get("codePath", ""))

            dashboard.update(final_code=codes[0]["code"], ai_status="done")

            if self.dry_run:
                self._say("演示/试运行模式：代码已生成，未提交评测", "ok")
                dashboard.update(eval_passed=None, eval_detail="演示模式：未提交评测")
                return True

            self._say("提交代码并触发评测 …")
            result = self.platform.evaluate(step_id, codes)
            ok, detail = summarize_result(result)
            dashboard.update(eval_passed=ok, eval_detail=detail, attempt=attempt + 1)

            if ok:
                self._say("✓ 通过", "ok")
                return True
            if detail:
                self._say("   差异：\n" + "\n".join("     " + ln for ln in detail.splitlines()), "err")
            self._say("✗ 未通过，把差异回传给 AI 重试" if attempt < self.max_retry else "✗ 未通过（已用完重试）", "err")
            feedback = detail or "输出与预期不一致，请检查输出格式。"
        return False
