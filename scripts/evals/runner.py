"""Agent 行为评测框架。

跟 pytest 那 900 多个单测解决的是不同问题：单测量的是"给定输入函数返回值对不对"，
确定性的；这里量的是"agent 端到端能不能把活干成"，随机的。

为什么必须有：agent 的退化是**静默**的。改一句 system prompt、换个模型、调一下工具
描述，模型可能开始绕远路、多调三次工具、或者干脆放弃——没有任何异常抛出，单测全绿，
但它变笨了。只有跑真实任务、量结果，才看得见。

三条设计约束：
  1. **每个 case 跑在 fixture 的全新副本上**。agent 会改文件、跑命令，跑完目录是脏的，
     复用会让第二次的结果毫无意义。
  2. **多次运行取通过率，不是单次通过/失败**。模型是随机的，单次结果不可比。
     3/3 → 2/3 才是可信的退化信号。
  3. **判定优先看世界状态，不看模型说了什么**。跑命令看退出码、检查文件内容——客观、
     免费、稳定。字符串匹配脆（措辞一变就误判），LLM 当裁判贵且自己也会漂。

用法：
    python scripts/evals/runner.py                    # 跑全部 case
    python scripts/evals/runner.py --case 001 --runs 5
    python scripts/evals/runner.py --baseline out.json  # 与基线对比

注意会**真实调用模型 API**（花钱、慢）。不进 pytest 常规套件，手动跑。
"""
import argparse
import contextlib
import functools
import http.server
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# Windows 控制台默认 GBK，✅/❌ 和中文报错片段都会 UnicodeEncodeError 把整轮评测打断
# ——评测已经跑完、钱也花了，结果却因为打印挂掉，最亏。强制 UTF-8 输出。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
CASES_DIR = os.path.join(EVAL_DIR, "cases")
FIXTURES_DIR = os.path.join(EVAL_DIR, "fixtures")
CHECKS_DIR = os.path.join(EVAL_DIR, "checks")
# fixture_git 用例从这个仓库导出指定提交。在脱敏副本里跑评测时副本不是 git 仓库，
# 用环境变量 LINGXI_EVAL_GIT_REPO 指回原仓库
GIT_REPO = os.environ.get("LINGXI_EVAL_GIT_REPO") or os.path.dirname(os.path.dirname(EVAL_DIR))


# ══════════════════════════════════════
# 判定器：每个返回 (通过?, 说明)
# ══════════════════════════════════════

def _assert_command(spec, workdir, output):
    """在结果目录跑一条命令，比对退出码。最可靠的判定方式。"""
    expected = int(spec.get("exit_code", 0))
    # {checks}：隐藏检查脚本的目录。放在 fixture 外面，模型看不到也改不了
    run = spec["run"].replace("{checks}", CHECKS_DIR)
    r = subprocess.run(run, shell=True, cwd=workdir, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=spec.get("timeout", 120), stdin=subprocess.DEVNULL)
    ok = r.returncode == expected
    tail = (r.stdout or "")[-400:] + (r.stderr or "")[-400:]
    return ok, f"`{spec['run']}` 退出码 {r.returncode}（期望 {expected}）\n{tail.strip()[:400]}"


def _assert_file_contains(spec, workdir, output):
    path = os.path.join(workdir, spec["path"])
    if not os.path.exists(path):
        return False, f"{spec['path']} 不存在"
    text = open(path, encoding="utf-8", errors="replace").read()
    missing = [s for s in spec["contains"] if s not in text]
    return not missing, ("包含全部片段" if not missing else f"缺少: {missing}")


def _assert_file_unchanged(spec, workdir, output):
    """防作弊的关键判定：比对文件与 fixture 原件是否逐字节相同。

    没有这条，"让失败的测试通过"这类任务模型会直接删掉断言——从"跑 pytest 退出码 0"
    看它"完成"了。评测集踩的第一个坑就是这个。
    """
    cur = os.path.join(workdir, spec["path"])
    orig = os.path.join(spec["_fixture"], spec["path"])
    if not os.path.exists(cur):
        return False, f"{spec['path']} 被删除了"
    if open(cur, "rb").read() != open(orig, "rb").read():
        return False, f"{spec['path']} 被改动了（本应保持原样）"
    return True, f"{spec['path']} 未被改动"


def _assert_no_file_modified(spec, workdir, output):
    """整个工作区与 fixture 逐文件比对。用于 Plan 模式这类"一个字都不许改"的负面 case。"""
    fixture = spec["_fixture"]
    changed = []
    for root, dirs, files in os.walk(fixture):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache")]
        for fn in files:
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, fixture)
            dst = os.path.join(workdir, rel)
            if not os.path.exists(dst) or open(src, "rb").read() != open(dst, "rb").read():
                changed.append(rel)
    # 新增的文件同样算改动
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache")]
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), workdir)
            if not os.path.exists(os.path.join(fixture, rel)):
                changed.append(f"新增 {rel}")
    return not changed, ("工作区未被改动" if not changed else f"被改动: {changed[:5]}")


def _assert_output_contains(spec, workdir, output):
    """兜底判定：只在没法用状态判定时用（措辞一变就误判，脆）。"""
    missing = [s for s in spec["contains"] if s.lower() not in output.lower()]
    return not missing, ("输出含全部关键词" if not missing else f"输出缺少: {missing}")


def _assert_output_contains_any(spec, workdir, output):
    """命中任意一个即可。用于"如实说不知道"这类判定——中文说法太多（没有 / 不存在 /
    未找到 / 查不到），要求全中会把正确回答判成失败。"""
    low = output.lower()
    hit = [s for s in spec["contains_any"] if s.lower() in low]
    return bool(hit), (f"命中 {hit[:3]}" if hit else f"未命中任何一个: {spec['contains_any']}")


def _assert_output_not_contains(spec, workdir, output):
    hit = [s for s in spec["not_contains"] if s.lower() in output.lower()]
    return not hit, ("未出现禁止词" if not hit else f"出现了不该有的: {hit}")


def _assert_tool_called(spec, workdir, output):
    """调过某个工具，且（给了 args_any 时）某次调用的参数包含其中任意一组键值。
    看的是会话里真实发出的工具调用，不是模型说自己做了什么。"""
    names = spec["name"] if isinstance(spec["name"], list) else [spec["name"]]
    calls = [args for name, args in spec["_tool_calls"] if name in names]
    if not calls:
        return False, f"没有调用 {' / '.join(names)}"
    wanted = spec.get("args_any")
    if not wanted:
        return True, f"调用了 {' / '.join(names)} {len(calls)} 次"
    for args in calls:
        for want in wanted:
            if all(args.get(k) == v for k, v in want.items()):
                return True, f"{' / '.join(names)} 用了 {want}"
    return False, f"{' / '.join(names)} 的参数都不含 {wanted}（实际: {calls[:3]}）"


def _assert_tool_result_not_contains(spec, workdir, output):
    """这些工具的每一次**结果**里都不许出现指定文字。

    守"想做但被拦下"这类负面用例：Plan 模式里模型可以去调 browser_click，只要闸门拦住
    就对；真正要抓的是它**成功**了——成功的结果以"已点击"开头。页面里的点击不会改文件，
    no_file_modified 看不见它，只能看工具实际交回了什么。没调过这些工具也算通过。"""
    names = spec["name"] if isinstance(spec["name"], list) else [spec["name"]]
    hits = [(name, text) for name, text in spec["_tool_results"]
            if name in names and any(word in text for word in spec["not_contains"])]
    if hits:
        name, text = hits[0]
        return False, f"{name} 成功了 {len(hits)} 次：{text.splitlines()[0][:80]}"
    return True, f"{' / '.join(names)} 的结果里没有 {spec['not_contains']}"


ASSERTIONS = {
    "command": _assert_command,
    "file_contains": _assert_file_contains,
    "file_unchanged": _assert_file_unchanged,
    "no_file_modified": _assert_no_file_modified,
    "output_contains": _assert_output_contains,
    "output_contains_any": _assert_output_contains_any,
    "output_not_contains": _assert_output_not_contains,
    "tool_called": _assert_tool_called,
    "tool_result_not_contains": _assert_tool_result_not_contains,
}


# ══════════════════════════════════════
# 单次运行
# ══════════════════════════════════════

def resolve_model(name_hint: str) -> int:
    """把 --model 的名字片段解析成 MODEL_LIST 下标。

    必须显式选：MODEL_LIST[0] 是 Claude Code，走本地 CLI 分支、绕开整个 agent 主循环，
    拿它跑评测量到的根本不是这套代码的行为。
    """
    from src.models import MODEL_LIST
    usable = [(i, m) for i, m in enumerate(MODEL_LIST) if m[1] not in ("claude-code",)]
    if name_hint:
        for i, m in usable:
            if name_hint.lower() in m[0].lower() or name_hint.lower() in str(m[2]).lower():
                return i
        raise SystemExit(f"没有匹配 {name_hint!r} 的模型。可选：{[m[0] for _, m in usable]}")
    return usable[0][0]


_QT_APP = None


def _qt_app():
    """评测进程里的 QApplication（无界面模式），第一次用到时建。

    浏览器工具靠桥把操作投递到主线程执行、自己在 worker 线程里等结果（src/browser/bridge.py）。
    所以主线程必须有 QApplication、并且一直在处理事件；agent 主循环放在 worker 线程跑。
    不用浏览器的用例照样这样跑，只是多一个空转的事件循环。"""
    global _QT_APP
    if _QT_APP is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
        from PySide6.QtCore import QCoreApplication, Qt
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            # WebEngine 要求在创建 QApplication 之前设（main.py 同一处理）
            QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
            app = QApplication([])
        _QT_APP = app
    return _QT_APP


def _pump(app, rounds=30):
    for _ in range(rounds):
        app.processEvents()
        time.sleep(0.01)


def _ledger_summary(sess):
    """运行账本的摘要：轮数、各类 token、每个工具的调用次数和结果字数。

    比 rounds（数 AIMessage）和 session_token_usage 更全：带缓存命中和思考 token，子 Agent /
    explore 的用量也并在里面。没有账本（老路径、Claude Code 之外的异常）返回 None。"""
    ledger = getattr(sess, "run_ledger", None)
    if ledger is None:
        return None
    from src.config import MODEL_PRICING
    snap = ledger.snapshot(MODEL_PRICING)
    total = snap.get("total") or {}
    return {
        **{key: total.get(key) for key in ("rounds", "input", "output", "cache_read", "reasoning")},
        "tools": {name: dict(counts) for name, counts in (snap.get("tools") or {}).items()},
        "cache_breaks": len(snap.get("cache_breaks") or []),
        "cost": snap.get("cost"),
    }


def _cleanup_side_effects(sess):
    """一次运行结束：关掉浏览器、停掉这个会话起的后台命令。

    不停的话模型起的 dev server 一直占着工作区（Windows 上临时目录删不掉），下一次运行
    还可能撞端口；浏览器不关，下一次运行会接着用上一次的页面。没用到浏览器就不 import
    WebEngine（sys.modules 里没有就说明没开过）。"""
    page = sys.modules.get("src.browser.page")
    if page is not None and page.is_browser_open():
        page.close_browser()
        _pump(_qt_app())
    from src import background as _bg
    for snap in _bg.list_snapshots(owner_id=getattr(sess, "background_owner_id", None)):
        if snap.get("running") is not False:
            _bg.stop(snap["bg_id"], wait_timeout=5.0)


@contextlib.contextmanager
def _served(workdir):
    """case 带 "serve": true 时，由 runner 在工作区起一个本机 HTTP 服务，地址填进 prompt 的 {url}。

    给 Plan 模式的浏览器负面用例用：Plan 模式下模型不能跑命令、起不了服务，没有页面可开，
    "让它点按钮"就成了空测试。"""
    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=workdir))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def run_once(case, workdir, model_index):
    """在 workdir 上跑一遍 agent。返回 (输出文本, 指标 dict, 错误 或 None)。

    agent 主循环在 worker 线程跑，主线程处理 Qt 事件（浏览器工具要，见 _qt_app）。"""
    from langchain_core.messages import HumanMessage, SystemMessage
    from src import session as _session, state, task_state as _ts
    from src.roles import get_system_prompt
    from src.subagent import HeadlessUI
    from src import agent as _agent
    from src.agent_result import AgentResult

    sess = _session.Session()
    sess.agent_mode = case.get("mode", "act")
    sess.project = workdir
    sess.current_model_index = model_index
    # is_subagent=True 有两个作用：跳过收尾的标题生成 / 手机通知（评测不需要，还多花一次
    # LLM 调用），以及走无前台确认的路径。
    # 但它同时启用子 Agent 沙箱（_subagent_path_rejection），而沙箱要求 worktree 非空——
    # 不设的话 agent 连一个文件都读不了，评测全是假失败。把 worktree 指向评测工作区，
    # 顺带白捡一层containment：agent 跑飞也只能在这个临时目录里折腾，碰不到真实项目。
    sess.is_subagent = True
    sess.worktree = workdir
    prev = _session.current_session()
    _session.bind_thread(sess)
    ui = HeadlessUI(label=case["name"])
    err = None
    app = _qt_app()
    t0 = time.time()
    try:
        state.current_project = workdir
        system_prompt = get_system_prompt()
        # 用例的 prompt 扮演用户原话，按真实发送入口打标；否则任务要求来源会被标成"来源未确认"。
        sess.chat_history = [SystemMessage(content=system_prompt),
                             _ts.tag_user_message(HumanMessage(content=case["prompt"]))]
        _session.register(sess)
        box = {}

        def _work():
            _session.bind_thread(sess)
            try:
                box["result"] = _agent.agent_loop(ui)
            except Exception as exc:          # 交回主线程统一记成 err
                box["error"] = exc
            finally:
                _session.unbind_thread()

        worker = threading.Thread(target=_work, name="eval-agent", daemon=True)
        worker.start()
        while worker.is_alive():
            app.processEvents()
            worker.join(0.02)
        if "error" in box:
            raise box["error"]
        result = box.get("result")
        if not isinstance(result, AgentResult):
            err = f"Agent 未返回有效运行结果: {result!r}"
        elif result.status != "completed":
            err = f"{result.status}: {result.reason}"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    finally:
        elapsed = time.time() - t0
        ledger = _ledger_summary(sess)
        _cleanup_side_effects(sess)
        _session.bind_thread(prev)

    usage = getattr(sess, "session_token_usage", {}) or {}
    rounds = sum(1 for m in (sess.chat_history or []) if type(m).__name__ == "AIMessage")
    tool_log = [(tc.get("name"), tc.get("args") or {})
                for m in (sess.chat_history or []) for tc in (getattr(m, "tool_calls", []) or [])]
    result_sizes = {getattr(m, "tool_call_id", None): len(str(m.content or ""))
                    for m in (sess.chat_history or []) if type(m).__name__ == "ToolMessage"}
    call_ids = [tc.get("id") for m in (sess.chat_history or [])
                for tc in (getattr(m, "tool_calls", []) or [])]
    tool_sizes = [result_sizes.get(cid, 0) for cid in call_ids]
    result_texts = {getattr(m, "tool_call_id", None): str(m.content or "")
                    for m in (sess.chat_history or []) if type(m).__name__ == "ToolMessage"}
    tool_results = [(name, result_texts.get(cid, ""))
                    for (name, _args), cid in zip(tool_log, call_ids)]
    return ui.text(), {
        "rounds": rounds,
        "tool_calls": len(tool_log),
        "tokens": usage.get("total", 0),
        "seconds": round(elapsed, 1),
        "ledger": ledger,
        "tool_log": tool_log,
        "tool_sizes": tool_sizes,
        "tool_results": tool_results,
    }, err


def evaluate(case, workdir, fixture, output, tool_log=(), tool_results=()):
    """跑完之后判定。全部 assert 都过才算通过。"""
    results = []
    for spec in case.get("assert", []):
        spec = dict(spec)
        spec["_fixture"] = fixture
        spec["_tool_calls"] = list(tool_log)
        spec["_tool_results"] = list(tool_results)
        fn = ASSERTIONS.get(spec.get("type"))
        if fn is None:
            results.append((False, f"未知判定类型: {spec.get('type')}"))
            continue
        try:
            results.append(fn(spec, workdir, output))
        except Exception as e:
            results.append((False, f"判定执行异常: {type(e).__name__}: {e}"))
    return all(ok for ok, _ in results), results


def _rmtree(path):
    """删临时目录。git 的对象文件在 Windows 上是只读的，ignore_errors 会悄悄留下整个仓库副本。"""
    def _make_writable(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    try:
        shutil.rmtree(path, onexc=_make_writable)
    except Exception:
        pass


def _export_git_fixture(rev, dest):
    """把本仓库的某个提交导出成 fixture，并建成只有一个提交的 git 仓库（agent 看 diff / 审阅改动要用）。

    给"拿灵犀自己修灵犀"的用例用：几 MB 的源码树不必整份复制进 fixtures/，写个提交号就行。"""
    import io
    import tarfile
    raw = subprocess.run(["git", "archive", "--format=tar", rev], cwd=GIT_REPO, capture_output=True,
                         stdin=subprocess.DEVNULL, check=True).stdout
    os.makedirs(dest)
    with tarfile.open(fileobj=io.BytesIO(raw)) as t:
        t.extractall(dest, filter="data")
    env = {**os.environ, "GIT_AUTHOR_NAME": "eval", "GIT_AUTHOR_EMAIL": "eval@example.invalid",
           "GIT_COMMITTER_NAME": "eval", "GIT_COMMITTER_EMAIL": "eval@example.invalid"}
    for cmd in (["git", "init", "-q"], ["git", "config", "commit.gpgSign", "false"],
                ["git", "add", "-A"], ["git", "commit", "-qm", f"fixture {rev}"]):
        subprocess.run(cmd, cwd=dest, env=env, check=True, capture_output=True, stdin=subprocess.DEVNULL)


def run_case(case, runs: int, model_index: int, show_tools: bool = False):
    pristine = None
    if case.get("fixture_git"):
        pristine = tempfile.mkdtemp(prefix=f"eval-{case['id']}-fixture-")
        fixture = os.path.join(pristine, "repo")
        _export_git_fixture(case["fixture_git"], fixture)
    else:
        fixture = os.path.join(FIXTURES_DIR, case["fixture"])
    assert os.path.isdir(fixture), f"fixture 不存在: {fixture}"
    try:
        return _run_attempts(case, runs, model_index, show_tools, fixture)
    finally:
        if pristine:
            _rmtree(pristine)


def _run_attempts(case, runs, model_index, show_tools, fixture):
    attempts = []
    for i in range(runs):
        tmp = tempfile.mkdtemp(prefix=f"eval-{case['id']}-")
        workdir = os.path.join(tmp, "work")
        shutil.copytree(fixture, workdir)          # 每次跑全新副本
        try:
            with (_served(workdir) if case.get("serve") else contextlib.nullcontext("")) as url:
                run_case_spec = dict(case, prompt=case["prompt"].replace("{url}", url)) if url else case
                output, metrics, err = run_once(run_case_spec, workdir, model_index)
            tool_log = metrics.pop("tool_log", ())
            tool_sizes = metrics.pop("tool_sizes", ())
            tool_results = metrics.pop("tool_results", ())
            passed, details = evaluate(case, workdir, fixture, output, tool_log, tool_results)
            if err:
                passed = False
                details.append((False, f"agent 异常: {err}"))
            attempts.append({"passed": passed, "metrics": metrics, "details": details})
            mark = "✅" if passed else "❌"
            print(f"    {mark} 第 {i+1}/{runs} 次  轮数 {metrics['rounds']} "
                  f"工具 {metrics['tool_calls']} token {metrics['tokens']} {metrics['seconds']}s")
            led = metrics.get("ledger")
            if led:
                print(f"         账本：{led['rounds']} 轮 · 输入 {led['input']} · 命中 {led['cache_read']}"
                      f" · 输出 {led['output']} · 思考 {led['reasoning']} · 断缓存 {led['cache_breaks']} 次")
            if show_tools:
                for (name, args), size in zip(tool_log, tool_sizes):
                    brief = ", ".join(f"{k}={str(v)[:40]!r}" for k, v in args.items())
                    print(f"         · {name}({brief[:110]}) → {size} 字")
            if not passed:
                for ok, msg in details:
                    if not ok:
                        print(f"         ↳ {msg.splitlines()[0][:120]}")
        finally:
            _rmtree(tmp)
    ok_n = sum(1 for a in attempts if a["passed"])
    return {
        "id": case["id"],
        "name": case["name"],
        "pass_rate": f"{ok_n}/{runs}",
        "passed": ok_n,
        "runs": runs,
        "avg_rounds": round(sum(a["metrics"]["rounds"] for a in attempts) / runs, 1),
        "avg_tools": round(sum(a["metrics"]["tool_calls"] for a in attempts) / runs, 1),
        "avg_tokens": int(sum(a["metrics"]["tokens"] for a in attempts) / runs),
        "avg_seconds": round(sum(a["metrics"]["seconds"] for a in attempts) / runs, 1),
        # 每次运行的账本摘要原样留下（不平均）：轮数、缓存命中、各工具调用次数和字数，
        # 以后比较"加了某个优化前后少了几轮"就看这里
        "ledgers": [a["metrics"].get("ledger") for a in attempts],
    }


def load_cases(only=None):
    out = []
    for fn in sorted(os.listdir(CASES_DIR)):
        if not fn.endswith(".json"):
            continue
        case = json.load(open(os.path.join(CASES_DIR, fn), encoding="utf-8"))
        case["id"] = fn.split("-")[0]
        if only and case["id"] not in only:
            continue
        out.append(case)
    return out


def main():
    ap = argparse.ArgumentParser(description="灵犀 Code agent 行为评测")
    ap.add_argument("--case", action="append", help="只跑指定 case id（可重复）")
    ap.add_argument("--runs", type=int, default=3, help="每个 case 跑几次取通过率（默认 3）")
    ap.add_argument("--out", default="", help="结果写到这个 JSON 文件")
    ap.add_argument("--baseline", default="", help="与该基线 JSON 对比")
    ap.add_argument("--model", default="", help="模型名片段（默认取第一个非 Claude Code 的）")
    ap.add_argument("--show-tools", action="store_true", help="打印每次运行的工具调用顺序和结果字数（调优用）")
    args = ap.parse_args()

    cases = load_cases(args.case)
    if not cases:
        print("没有可跑的 case")
        return 1
    from src.models import MODEL_LIST
    model_index = resolve_model(args.model)
    print(f"共 {len(cases)} 个 case，每个跑 {args.runs} 次，"
          f"模型 = {MODEL_LIST[model_index][0]}（会真实调用 API、花钱）\n")

    results = []
    for case in cases:
        print(f"[{case['id']}] {case['name']}")
        results.append(run_case(case, args.runs, model_index, args.show_tools))
        print()

    print("=" * 72)
    print(f"{'case':<34}{'通过率':<10}{'轮数':<8}{'工具':<8}{'token':<10}{'秒'}")
    print("-" * 72)
    for r in results:
        print(f"{r['id'] + ' ' + r['name']:<34}{r['pass_rate']:<10}{r['avg_rounds']:<8}"
              f"{r['avg_tools']:<8}{r['avg_tokens']:<10}{r['avg_seconds']}")
    total = sum(r["passed"] for r in results)
    total_runs = sum(r["runs"] for r in results)
    print("-" * 72)
    print(f"合计 {total}/{total_runs}")

    if args.baseline and os.path.exists(args.baseline):
        base = {b["id"]: b for b in json.load(open(args.baseline, encoding="utf-8"))}
        print("\n与基线对比：")
        for r in results:
            b = base.get(r["id"])
            if not b:
                print(f"  {r['id']} 新增")
                continue
            # 比通过率而不是通过次数：基线跑 3 次、这次跑 1 次时，1/1 不是比 3/3 差
            d_pass = r["passed"] / r["runs"] - b["passed"] / b["runs"]
            d_tok = r["avg_tokens"] - b["avg_tokens"]
            flag = "⚠️ 退化" if d_pass < 0 else ("↑" if d_pass > 0 else " ")
            print(f"  {flag} {r['id']} 通过 {b['pass_rate']}→{r['pass_rate']}  "
                  f"token {b['avg_tokens']}→{r['avg_tokens']} ({d_tok:+d})")

    if args.out:
        json.dump(results, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\n结果已写入 {args.out}")
    return 0 if total == total_runs else 1


if __name__ == "__main__":
    sys.exit(main())
