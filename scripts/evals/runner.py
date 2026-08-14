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
import json
import os
import shutil
import subprocess
import sys
import tempfile
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


# ══════════════════════════════════════
# 判定器：每个返回 (通过?, 说明)
# ══════════════════════════════════════

def _assert_command(spec, workdir, output):
    """在结果目录跑一条命令，比对退出码。最可靠的判定方式。"""
    expected = int(spec.get("exit_code", 0))
    r = subprocess.run(spec["run"], shell=True, cwd=workdir, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=spec.get("timeout", 120))
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


ASSERTIONS = {
    "command": _assert_command,
    "file_contains": _assert_file_contains,
    "file_unchanged": _assert_file_unchanged,
    "no_file_modified": _assert_no_file_modified,
    "output_contains": _assert_output_contains,
    "output_contains_any": _assert_output_contains_any,
    "output_not_contains": _assert_output_not_contains,
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


def run_once(case, workdir, model_index):
    """在 workdir 上跑一遍 agent。返回 (输出文本, 指标 dict, 错误 或 None)。"""
    from langchain_core.messages import HumanMessage, SystemMessage
    from src import session as _session, state
    from src.roles import get_system_prompt
    from src.subagent import HeadlessUI
    from src import agent as _agent

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
    t0 = time.time()
    try:
        state.current_project = workdir
        system_prompt = get_system_prompt()
        sess.chat_history = [SystemMessage(content=system_prompt),
                             HumanMessage(content=case["prompt"])]
        _session.register(sess)
        _agent.agent_loop(ui)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    finally:
        elapsed = time.time() - t0
        _session.bind_thread(prev)

    usage = getattr(sess, "session_token_usage", {}) or {}
    rounds = sum(1 for m in (sess.chat_history or []) if type(m).__name__ == "AIMessage")
    tool_calls = sum(len(getattr(m, "tool_calls", []) or [])
                     for m in (sess.chat_history or []))
    return ui.text(), {
        "rounds": rounds,
        "tool_calls": tool_calls,
        "tokens": usage.get("total", 0),
        "seconds": round(elapsed, 1),
    }, err


def evaluate(case, workdir, fixture, output):
    """跑完之后判定。全部 assert 都过才算通过。"""
    results = []
    for spec in case.get("assert", []):
        spec = dict(spec)
        spec["_fixture"] = fixture
        fn = ASSERTIONS.get(spec.get("type"))
        if fn is None:
            results.append((False, f"未知判定类型: {spec.get('type')}"))
            continue
        try:
            results.append(fn(spec, workdir, output))
        except Exception as e:
            results.append((False, f"判定执行异常: {type(e).__name__}: {e}"))
    return all(ok for ok, _ in results), results


def run_case(case, runs: int, model_index: int):
    fixture = os.path.join(FIXTURES_DIR, case["fixture"])
    assert os.path.isdir(fixture), f"fixture 不存在: {fixture}"
    attempts = []
    for i in range(runs):
        tmp = tempfile.mkdtemp(prefix=f"eval-{case['id']}-")
        workdir = os.path.join(tmp, "work")
        shutil.copytree(fixture, workdir)          # 每次跑全新副本
        try:
            output, metrics, err = run_once(case, workdir, model_index)
            passed, details = evaluate(case, workdir, fixture, output)
            if err:
                passed = False
                details.append((False, f"agent 异常: {err}"))
            attempts.append({"passed": passed, "metrics": metrics, "details": details})
            mark = "✅" if passed else "❌"
            print(f"    {mark} 第 {i+1}/{runs} 次  轮数 {metrics['rounds']} "
                  f"工具 {metrics['tool_calls']} token {metrics['tokens']} {metrics['seconds']}s")
            if not passed:
                for ok, msg in details:
                    if not ok:
                        print(f"         ↳ {msg.splitlines()[0][:120]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
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
        results.append(run_case(case, args.runs, model_index))
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
            d_pass = r["passed"] - b["passed"]
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
