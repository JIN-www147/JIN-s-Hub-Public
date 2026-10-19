# -*- coding: utf-8 -*-
"""美林平台 AI 自动答题工具 —— 命令行入口"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from bot import dashboard
    from bot.platform import Platform, PlatformError
    from bot.ai_web import WebAI
    from bot.solver import Solver
except ModuleNotFoundError as _exc:
    _name = str(_exc).split("'")[1] if "'" in str(_exc) else str(_exc)
    print()
    print("=" * 58)
    print(f"  缺少依赖：{_name}")
    print("=" * 58)
    print("请先安装依赖，任选一种：")
    print("  1. 双击 install.bat（推荐，自动建虚拟环境并安装）")
    print("  2. 命令行执行：pip install -r requirements.txt")
    print()
    try:
        input("按回车退出 …")
    except EOFError:
        pass
    sys.exit(1)

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")

cfg_skip_passed = True


def load_config() -> dict:
    """读取配置；如果没有 config.json，就从模板复制一份并提示用户填写。"""
    if not os.path.exists(CONFIG_PATH):
        example = os.path.join(ROOT, "config.example.json")
        print(f"没有找到 config.json，正在从模板生成 …")
        if os.path.exists(example):
            shutil.copyfile(example, CONFIG_PATH)
            print(f"已生成：{CONFIG_PATH}")
            print("请用记事本打开它，填写：")
            print("  base_url    —— 平台地址，例如 http://你的平台地址/tempo-talents/")
            print("  username    —— 你的学号/账号")
            print("  password    —— 你的密码")
            print("  classroom_id —— 留空即可，程序会自动选你加入的第一个课堂")
            sys.exit(0)
        raise SystemExit("缺少 config.example.json，无法生成配置")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def hr(title=""):
    print()
    print("=" * 66)
    if title:
        print(f"  {title}")
        print("=" * 66)


def parse_range(text: str) -> list[int]:
    """'1-5,8' -> [1,2,3,4,5,8]"""
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


RE_CHAPTER = re.compile(r"第?\s*([一二三四五六七八九十\d]+)\s*章")
RE_FULL_CODE = re.compile(r"^\s*(\d+)\s*[-－]\s*(\d+)\s*$")
RE_RANGE = re.compile(r"(\d+)\s*[-－~～到至]\s*(\d+)")
RE_ONE_NUM = re.compile(r"^\s*(\d+)\s*$")

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _normalize_chapter(text: str) -> str:
    """把「第三章」这类写法统一成「第3章」"""
    def rep(m):
        s = m.group(1)
        if s.isdigit():
            return f"第{s}章"
        if s in CN_NUM:
            return f"第{CN_NUM[s]}章"
        if s.startswith("十"):
            return f"第{10 + CN_NUM.get(s[1:], 0)}章"
        if s.endswith("十"):
            return f"第{CN_NUM.get(s[0], 1) * 10}章"
        return m.group(0)
    return RE_CHAPTER.sub(rep, text)


def parse_scope(text: str) -> dict:
    """
    解析一条范围指令，支持这些写法：
        （空）           -> 全部
        第1章 / 第三章    -> 该章全部实验
        第1章 1-5        -> 第一章 1-1 到 1-5 号实验
        1-3             -> 只做 1-3 号实验
        第2章 2          -> 第二章 2 号实验
        Java高级特性     -> 按名字模糊匹配
    返回 {chapter, from, to} 或 {keyword}
    """
    text = _normalize_chapter((text or "").strip())
    if not text:
        return {}

    m = RE_CHAPTER.search(text)
    chapter = int(m.group(1)) if (m and m.group(1).isdigit()) else None
    rest = text
    if m:
        rest = (text[:m.start()] + " " + text[m.end():]).strip()

    # 没有「第N章」时，'1-3' 这种整体就是一个实验编号
    if chapter is None:
        m2 = RE_FULL_CODE.match(text)
        if m2:
            return {"chapter": int(m2.group(1)), "from": int(m2.group(2)), "to": int(m2.group(2))}

    # 「第N章」后面没跟数字：整章
    if chapter is not None and not rest:
        return {"chapter": chapter}

    # 「第N章 1-5」里的 1-5 是实验序号范围
    r = RE_RANGE.search(rest)
    if r:
        return {"chapter": chapter, "from": int(r.group(1)), "to": int(r.group(2))}

    # 单个数字：有章号 -> 该章第 N 个实验；无章号 -> 第 N 章
    m3 = RE_ONE_NUM.match(rest)
    if m3:
        n = int(m3.group(1))
        if chapter is not None:
            return {"chapter": chapter, "from": n, "to": n}
        return {"chapter": n}

    # 其余按章节名 / 实验名关键字匹配
    return {"keyword": text}


def describe_scope(scope: dict) -> str:
    if not scope:
        return "全部题目"
    if scope.get("keyword"):
        return f"名称包含「{scope['keyword']}」"
    ch = scope.get("chapter")
    a, b = scope.get("from"), scope.get("to")
    if ch is not None and a is not None:
        if a == b:
            return f"第{ch}章 第{a}个实验"
        return f"第{ch}章 第{a}~{b}个实验"
    if ch is not None:
        return f"第{ch}章 全部实验"
    if a is not None:
        return f"第{a}~{b}个实验（所有章）"
    return "全部题目"


def match_course(c: dict, scope: dict) -> bool:
    if not scope:
        return True
    if scope.get("keyword"):
        kw = scope["keyword"]
        return kw in (c.get("coursesName") or "") or kw in (c.get("_chapter") or "")
    ch = scope.get("chapter")
    if ch is not None and c.get("chapter_no") != ch:
        return False
    a, b = scope.get("from"), scope.get("to")
    if a is not None:
        n = c.get("course_no") or 0
        if not (a <= n <= b):
            return False
    return True


def ensure_classroom(plat: Platform, cfg: dict) -> str:
    """配置里没写 classroom_id 时，自动取第一个课堂并写回配置。"""
    cid = (plat.classroom_id or "").strip()
    if cid and not cid.startswith("YOUR_"):
        return cid
    classes = plat.list_classrooms()
    if not classes:
        raise PlatformError("没有找到你加入的课堂，请先在平台上加入课堂")
    first = classes[0]
    plat.classroom_id = first["id"]
    print(f"已自动选择课堂：{first.get('classroomName')}（{first.get('belongToClass')}）")
    try:
        cfg["platform"]["classroom_id"] = first["id"]
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return first["id"]


def make_platform(cfg: dict, headless: bool = False) -> Platform:
    p = cfg["platform"]
    return Platform(
        base_url=p["base_url"], username=p["username"], password=p["password"],
        classroom_id=p["classroom_id"], profile_dir=os.path.join(ROOT, "data", "profile"),
        headless=headless,
    )


class DemoAI:
    """演示用的假 AI：不联网，用来预览监控面板和整个流程。"""

    key = "demo"
    name = "演示模式"
    per_chat = 3

    def __init__(self, log=print, on_delta=None):
        self.log = log
        self.on_delta = on_delta

    def open(self):
        return None

    def check_login(self):
        return True

    def new_chat(self):
        pass

    def begin_question(self):
        return 1

    def ask(self, prompt, quiet=False, expect_files=1):
        self.log("演示模式：不调用真实 AI，直接返回示例代码")
        time.sleep(0.6)
        chunks = [
            "先看题目要求：需要在 Begin/End 之间补全实现。\n",
            "注意输出要逐字符一致，中文冒号不能写成英文冒号。\n",
            "```java\n",
            'System.out.println("演示模式生成的示例代码");\n',
            "```\n",
            "以上就是本题的代码。",
        ]
        text = ""
        for c in chunks:
            text += c
            if self.on_delta:
                self.on_delta(text)
            time.sleep(0.3)
        return text

    def list_chats(self):
        return set()

    def cleanup_since(self, before):
        return 0


def open_dashboard(plat, headless: bool) -> str:
    """启动监控面板；非后台模式时在浏览器里开标签页并切到前台。"""
    dashboard.start()
    url = dashboard.url()
    if headless:
        return url
    try:
        page = plat.ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.bring_to_front()
    except Exception:
        pass
    return url


# ---------------------------------------------------------------------- 命令
def cmd_login(cfg, args):
    hr("首次登录 / 检查登录状态")
    plat = make_platform(cfg, headless=False)
    plat.start()
    try:
        me = plat.login()
        print(f"平台：已登录 {me.get('name')}（{me.get('code')}）")

        ai = WebAI(plat.ctx, "deepseek", log=print)
        ai.open()
        print("\n浏览器里已打开 DeepSeek 对话页。")
        print("请在页面里完成登录（扫码或手机号密码都行）。")
        try:
            input("\n登录完成后，回到这里按回车 …")
        except EOFError:
            pass

        print()
        print(f"DeepSeek：{'已就绪 ✓' if ai.check_login() else '未检测到登录 ✗，请重跑本命令'}")
    finally:
        plat.close()
    print("\n登录状态已保存，下次直接运行 启动.bat 选 3 答题。")


def cmd_check(cfg, args):
    hr("环境自检")
    plat = make_platform(cfg, headless=True)
    plat.start()
    try:
        me = plat.login()
        print(f"[OK] 平台登录：{me.get('name')}（{me.get('code')}）")
        ensure_classroom(plat, cfg)
        classes = plat.list_classrooms()
        print(f"[OK] 我的课堂：{len(classes)} 个")
        for c in classes:
            print(f"      · {c.get('classroomName')}（{c.get('belongToClass')}）")
        courses = plat.list_courses("practice")
        print(f"[OK] 课程实验：{len(courses)} 个")
        ai = WebAI(plat.ctx, "deepseek", log=print)
        ai.open()
        print(f"[{'OK' if ai.check_login() else '--'}] DeepSeek 登录状态")
    finally:
        plat.close()


def cmd_list(cfg, args):
    hr("课程与关卡清单")
    plat = make_platform(cfg, headless=True)
    plat.start()
    try:
        plat.login()
        ensure_classroom(plat, cfg)
        for ctype, label in [("practice", "课程实验"), ("training", "应用实训"), ("combat", "项目实战")]:
            courses = plat.list_courses(ctype)
            if not courses:
                continue
            print(f"\n【{label}】共 {len(courses)} 个")
            for c in courses:
                name = c.get("coursesName") or c.get("name")
                print(f"  · {name}")
                try:
                    steps = plat.list_steps(c.get("courseId") or c["id"], retries=0)
                except Exception:
                    print("      （实践环境未启动，运行自动答题时会自动初始化）")
                    continue
                for s in steps:
                    flag = "✓" if s.get("finished") else "·"
                    print(f"      {flag} 第{s.get('sequence')}关 {s.get('name')}  [{s.get('stepId')}]")
    finally:
        plat.close()


def _keep(step: dict, pick: list[int] | None) -> bool:
    if cfg_skip_passed and step.get("finished"):
        return False
    if pick and step.get("sequence") not in pick:
        return False
    return True


def collect_plan(plat: Platform, args) -> list[tuple]:
    """返回 [(实验名, 课程id, 编号如 1-3)]"""
    courses = plat.list_courses("practice")

    scope = parse_scope(getattr(args, "scope", "") or "")
    if scope:
        courses = [c for c in courses if match_course(c, scope)]

    kw = (getattr(args, "course", None) or "").strip()
    if kw:
        courses = [c for c in courses if kw in (c.get("coursesName") or "")]

    if not courses:
        return []
    return [(c.get("coursesName") or c.get("name"),
             c.get("courseId") or c.get("id"),
             c.get("course_code") or "") for c in courses]


def cmd_run(cfg, args):
    hr("自动答题")
    demo = bool(getattr(args, "demo", False))
    # 演示模式一律不提交，避免把示例代码真的交上去
    dry = bool(getattr(args, "dry_run", False)) or demo
    headless = bool(args.headless)

    plat = make_platform(cfg, headless=headless)
    plat.start()
    try:
        me = plat.login()
        ensure_classroom(plat, cfg)
        url = open_dashboard(plat, headless)
        dashboard.update(account=f"{me.get('name')} / {me.get('orgName') or ''}")
        print(f"监控面板：{url}    （已在浏览器新标签页打开）")

        if demo:
            ai = DemoAI(log=print, on_delta=lambda t: dashboard.update(ai_status="answering", ai_answer=t))
        else:
            ai = WebAI(plat.ctx, "deepseek", timeout=cfg["ai"]["answer_timeout"], log=print,
                       on_delta=lambda t: dashboard.update(ai_status="answering", ai_answer=t),
                       per_chat=cfg["run"].get("questions_per_chat", 3))
            ai.open()
            if not ai.check_login():
                if headless:
                    print("[!] DeepSeek 尚未登录，请先运行 首次登录.bat")
                    return
                print("\n[!] DeepSeek 尚未登录，已在浏览器里打开登录页。")
                print("    请完成扫码/密码登录，然后回到这个窗口按回车继续。")
                try:
                    input("    登录完成后按回车 …")
                except EOFError:
                    return
                if not ai.check_login():
                    print("    仍未检测到登录状态，退出。请运行 首次登录.bat")
                    return

        # 记下运行前已有的对话，结束时把本次新产生的删掉，保持账号干净
        before_chats = set()
        if not demo:
            try:
                before_chats = ai.list_chats()
            except Exception:
                before_chats = set()

        solver = Solver(plat, ai, max_retry=cfg["run"]["max_retry"], log=print)
        solver.dry_run = dry

        plan = collect_plan(plat, args)
        if not plan:
            print("没有匹配到实验，请检查输入的范围。")
            return

        pick = parse_range(args.steps) if args.steps else None
        limit = args.limit or 0

        print(f"\n范围：{describe_scope(parse_scope(getattr(args, 'scope', '') or ''))}")
        print(f"将处理 {len(plan)} 个实验：")
        for _cname, _cid, _code in plan:
            print(f"   {_code:>5}  {_cname}")
        print()

        # 预扫一次，让面板进度条有意义
        total = 0
        for cname, cid, code in plan:
            try:
                steps = plat.list_steps(cid, retries=0)
            except Exception:
                steps = []
            total += len([s for s in steps if _keep(s, pick)])
        if limit:
            total = min(total, limit) if total else limit
        dashboard.update(total=total)
        print(f"计划处理 {total} 关\n")

        idx = ok_cnt = 0
        for cname, course_id, code in plan:
            if limit and idx >= limit:
                break
            print(f"\n>>> 实验：{code} {cname}")
            base = cfg["platform"]["base_url"].rstrip("/")
            cid_class = cfg["platform"]["classroom_id"]
            dashboard.update(
                course=f"{code} {cname}",
                practice_url=f"{base}/#/practicum/practiceDetail?id={course_id}"
                             f"&classroomId={cid_class}&tenancyId={cid_class}",
            )
            try:
                steps = plat.prepare_course(course_id)
            except Exception as exc:
                print(f"    环境启动失败，跳过：{exc}")
                dashboard.log(f"环境启动失败：{exc}", "err")
                continue

            steps.sort(key=lambda s: s.get("sequence") or 0)
            for s in steps:
                if limit and idx >= limit:
                    break
                if not _keep(s, pick):
                    continue
                idx += 1
                seq = s.get("sequence") or 0
                tag = f"{idx}/{total}" if total else str(idx)
                print(f"\n[{tag}] {cname} / 第{seq}关 {s.get('name')}")
                dashboard.update(index=idx, course=f"{code} {cname}")
                try:
                    if solver.solve_step(s["stepId"], s.get("name"), course=cname, seq=seq):
                        ok_cnt += 1
                except Exception as exc:
                    print(f"   处理失败：{exc}")
                    dashboard.log(f"处理失败：{exc}", "err")
                time.sleep(cfg["run"]["delay_between_steps"])

        hr("完成")
        msg = f"处理 {idx} 关，通过 {ok_cnt} 关"
        if dry:
            msg += "（演示/试运行模式，未真正提交评测）"
        print(msg)
        dashboard.log(msg, "ok")

        # 收尾：删掉本次新建的 DeepSeek 对话
        if before_chats and cfg["run"].get("cleanup_chat", True):
            print("\n正在清理本次产生的 DeepSeek 对话 …")
            try:
                n = ai.cleanup_since(before_chats)
                print(f"清理完成：删除 {n} 个对话")
                dashboard.log(f"清理 DeepSeek 对话：{n} 个", "ok")
            except Exception as exc:
                print(f"清理失败（不影响答题结果）：{exc}")
    finally:
        plat.close()


def cmd_ai_test(cfg, args):
    hr("测试 DeepSeek 通道")
    plat = make_platform(cfg, headless=False)
    plat.start()
    try:
        ai = WebAI(plat.ctx, "deepseek", timeout=cfg["ai"]["answer_timeout"], log=print)
        ai.open()
        if not ai.check_login():
            print("尚未登录，请先运行 首次登录.bat")
            return
        print("发送测试问题：用 Java 输出 Hello World，只给代码块。")
        ans = ai.ask("请用 Java 输出 Hello World，只输出一个 ```java 代码块，不要解释。")
        print("\n--- AI 回答 ---")
        print(ans[:1200])
    finally:
        plat.close()


MENU = """
╔══════════════════════════════════════════════════════════╗
║        美林 Tempo 实训平台 · AI 自动答题工具              ║
║        解答引擎：DeepSeek 网页版（单对话连续作答）        ║
╠══════════════════════════════════════════════════════════╣
║  1  首次登录（平台 + DeepSeek）                           ║
║  2  查看课程与关卡                                        ║
║  3  自动答题（带实时监控面板）                            ║
║  4  演示模式（不登录 AI，只看界面和流程）                 ║
║  5  测试 DeepSeek 通道                                    ║
║  6  环境自检                                              ║
║  0  退出                                                  ║
╚══════════════════════════════════════════════════════════╝"""


def interactive(cfg):
    from types import SimpleNamespace
    while True:
        print(MENU)
        choice = input("请选择：").strip()
        if choice == "0":
            break
        try:
            if choice == "1":
                cmd_login(cfg, None)
            elif choice == "2":
                cmd_list(cfg, None)
            elif choice in ("3", "4"):
                print("\n请输入答题范围，一条指令搞定（直接回车 = 全部题目）：")
                print("   第1章            → 第一章全部实验")
                print("   第1章 1-5        → 第一章 1-1 到 1-5 号实验")
                print("   1-3              → 只做 1-3 号实验")
                print("   第2章 2          → 第二章 2 号实验")
                scope = input("范围 > ").strip()
                args = SimpleNamespace(scope=scope, course=None, steps=None, limit=0,
                                       headless=False, demo=(choice == "4"), dry_run=(choice == "4"))
                cmd_run(cfg, args)
            elif choice == "5":
                cmd_ai_test(cfg, None)
            elif choice == "6":
                cmd_check(cfg, None)
            else:
                print("无效选项")
        except PlatformError as exc:
            print(f"\n[平台错误] {exc}")
        except KeyboardInterrupt:
            print("\n已中断")
        print()
        input("按回车返回菜单 …")


# ---------------------------------------------------------------------- 入口
def main():
    global cfg_skip_passed
    cfg = load_config()
    cfg_skip_passed = cfg["run"].get("skip_passed", True)

    ap = argparse.ArgumentParser(description="美林平台 AI 自动答题工具")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("login", help="首次登录 / 检查登录状态")
    sub.add_parser("check", help="环境自检")
    sub.add_parser("list", help="列出课程与关卡")
    sub.add_parser("ai-test", help="测试 DeepSeek 是否可用")

    r = sub.add_parser("run", help="自动答题")
    r.add_argument("--scope", help="答题范围，如 '第1章' / '第1章 1-5' / '1-3'，不填=全部")
    r.add_argument("--course", help="（旧参数）实验名模糊匹配")
    r.add_argument("--steps", help="（可选）只做实验里的第几关，如 1-3")
    r.add_argument("--limit", type=int, default=0, help="最多做几关")
    r.add_argument("--headless", action="store_true", help="后台运行（不显示浏览器）")
    r.add_argument("--demo", action="store_true", help="演示模式：假 AI + 不提交，只看流程")
    r.add_argument("--dry-run", action="store_true", help="只生成代码，不提交评测")

    args = ap.parse_args()
    if not args.cmd:
        interactive(cfg)
        return

    handlers = {"login": cmd_login, "check": cmd_check, "list": cmd_list,
                "run": cmd_run, "ai-test": cmd_ai_test}
    try:
        handlers[args.cmd](cfg, args)
    except PlatformError as exc:
        print(f"\n[平台错误] {exc}")
    except KeyboardInterrupt:
        print("\n已中断")


if __name__ == "__main__":
    main()
