"""评测框架里**确定性**部分的单测：判定器 + case 加载。

agent 跑批本身会真实调模型、慢且随机，不进常规套件；但判定器是纯函数，必须测——
判定错了整套评测的结论就是错的，而这种错比没有评测更危险（给出虚假的安全感）。
"""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "evals"))
from runner import ASSERTIONS, evaluate, load_cases  # noqa: E402


@pytest.fixture()
def pair(tmp_path):
    """造一对 (fixture 原件, 工作副本)。"""
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "a.py").write_text("print(1)\n", encoding="utf-8")
    (fx / "test_a.py").write_text("assert True\n", encoding="utf-8")
    wk = tmp_path / "wk"
    shutil.copytree(fx, wk)
    return str(fx), str(wk)


class TestFileUnchanged:
    """防作弊判定：模型偷改测试文件必须被抓住。"""

    def test_identical_passes(self, pair):
        fx, wk = pair
        ok, msg = ASSERTIONS["file_unchanged"](
            {"path": "test_a.py", "_fixture": fx}, wk, "")
        assert ok, msg

    def test_modified_fails(self, pair):
        fx, wk = pair
        (open(os.path.join(wk, "test_a.py"), "w", encoding="utf-8")).write("assert False\n")
        ok, msg = ASSERTIONS["file_unchanged"]({"path": "test_a.py", "_fixture": fx}, wk, "")
        assert not ok and "被改动" in msg

    def test_deleted_fails(self, pair):
        """直接删掉测试文件也是作弊的一种，pytest 照样会绿。"""
        fx, wk = pair
        os.remove(os.path.join(wk, "test_a.py"))
        ok, msg = ASSERTIONS["file_unchanged"]({"path": "test_a.py", "_fixture": fx}, wk, "")
        assert not ok and "删除" in msg


class TestNoFileModified:
    """Plan 模式那类负面 case 的守门判定。"""

    def test_clean_copy_passes(self, pair):
        fx, wk = pair
        ok, msg = ASSERTIONS["no_file_modified"]({"_fixture": fx}, wk, "")
        assert ok, msg

    def test_content_change_caught(self, pair):
        fx, wk = pair
        (open(os.path.join(wk, "a.py"), "w", encoding="utf-8")).write("print(2)\n")
        ok, msg = ASSERTIONS["no_file_modified"]({"_fixture": fx}, wk, "")
        assert not ok and "a.py" in msg

    def test_new_file_caught(self, pair):
        """新增文件同样算改动——否则模型"另存一份改好的"就能蒙混过关。"""
        fx, wk = pair
        (open(os.path.join(wk, "sneaky.py"), "w", encoding="utf-8")).write("x=1\n")
        ok, msg = ASSERTIONS["no_file_modified"]({"_fixture": fx}, wk, "")
        assert not ok and "sneaky.py" in msg

    def test_deletion_caught(self, pair):
        fx, wk = pair
        os.remove(os.path.join(wk, "a.py"))
        ok, _ = ASSERTIONS["no_file_modified"]({"_fixture": fx}, wk, "")
        assert not ok

    def test_pycache_ignored(self, pair):
        """跑一次命令就会生成 __pycache__，不该被判成"改动了工作区"。"""
        fx, wk = pair
        os.makedirs(os.path.join(wk, "__pycache__"), exist_ok=True)
        (open(os.path.join(wk, "__pycache__", "a.pyc"), "w")).write("x")
        ok, msg = ASSERTIONS["no_file_modified"]({"_fixture": fx}, wk, "")
        assert ok, msg


class TestCommandAssertion:
    def test_exit_code_match(self, tmp_path):
        ok, msg = ASSERTIONS["command"](
            {"run": "python -c \"import sys; sys.exit(0)\"", "exit_code": 0}, str(tmp_path), "")
        assert ok, msg

    def test_exit_code_mismatch(self, tmp_path):
        ok, msg = ASSERTIONS["command"](
            {"run": "python -c \"import sys; sys.exit(3)\"", "exit_code": 0}, str(tmp_path), "")
        assert not ok and "3" in msg


class TestOutputAssertions:
    def test_contains_requires_all(self):
        ok, _ = ASSERTIONS["output_contains"]({"contains": ["a.py", "b.py"]}, "", "见 a.py")
        assert not ok
        ok, _ = ASSERTIONS["output_contains"]({"contains": ["a.py", "b.py"]}, "", "a.py 和 b.py")
        assert ok

    def test_contains_any_needs_one(self):
        spec = {"contains_any": ["没有", "不存在", "未找到"]}
        ok, _ = ASSERTIONS["output_contains_any"](spec, "", "项目里不存在这个函数")
        assert ok
        ok, _ = ASSERTIONS["output_contains_any"](spec, "", "它的实现如下：def foo()")
        assert not ok

    def test_case_insensitive(self):
        ok, _ = ASSERTIONS["output_contains"]({"contains": ["MARKER_ABC"]}, "", "marker_abc")
        assert ok


class TestEvaluate:
    def test_all_must_pass(self, pair):
        fx, wk = pair
        case = {"assert": [
            {"type": "no_file_modified"},
            {"type": "output_contains", "contains": ["缺这个"]},
        ]}
        passed, details = evaluate(case, wk, fx, "无关输出")
        assert not passed and len(details) == 2

    def test_unknown_type_fails_loud(self, pair):
        """判定类型写错必须判失败，绝不能静默跳过——静默跳过等于这条约束消失了。"""
        fx, wk = pair
        passed, details = evaluate({"assert": [{"type": "typo_here"}]}, wk, fx, "")
        assert not passed and "未知判定类型" in details[0][1]

    def test_assertion_exception_is_failure(self, pair):
        """判定器自己抛异常算失败，不能让整轮评测崩掉。"""
        fx, wk = pair
        passed, details = evaluate(
            {"assert": [{"type": "file_contains", "path": "nope.py", "contains": ["x"]}]},
            wk, fx, "")
        assert not passed


def _has_full_history(repo):
    """仓库带不带完整历史：浅克隆（CI 默认只拉一个提交）或导出后重新 init 的副本都不带。"""
    import subprocess
    shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=repo, capture_output=True,
                             text=True, stdin=subprocess.DEVNULL).stdout.strip()
    count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL).stdout.strip()
    return shallow == "false" and count.isdigit() and int(count) > 1


class TestCases:
    def test_shipped_cases_are_valid(self):
        """随仓库带的 case 必须结构合法、判定类型都认识、fixture 都存在。"""
        import subprocess
        cases = load_cases()
        assert len(cases) >= 5
        fx_dir = os.path.join(os.path.dirname(__file__), "evals", "fixtures")
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for c in cases:
            assert c.get("name") and c.get("prompt"), c
            assert c.get("mode") in ("act", "plan"), c
            if c.get("fixture_git"):      # 用本仓库的某个提交当 fixture：提交得存在
                r = subprocess.run(["git", "cat-file", "-e", f"{c['fixture_git']}^{{commit}}"], cwd=repo,
                                   capture_output=True, stdin=subprocess.DEVNULL)
                if r.returncode != 0 and not _has_full_history(repo):
                    continue      # 浅克隆 / 导出副本里本来就没有那段历史，核对不了，不算 case 写错
                assert r.returncode == 0, f"{c['id']} 的 fixture_git 提交不存在: {c['fixture_git']}"
            else:
                assert os.path.isdir(os.path.join(fx_dir, c["fixture"])), c["fixture"]
            assert c.get("assert"), f"{c['id']} 没有任何判定"
            for spec in c["assert"]:
                assert spec["type"] in ASSERTIONS, spec["type"]

    def test_broken_calc_fixture_actually_fails(self):
        """001 的 fixture 必须真的是坏的——fixture 本身就绿的话这个 case 恒通过、毫无意义。"""
        import subprocess
        fx = os.path.join(os.path.dirname(__file__), "evals", "fixtures", "broken-calc")
        r = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=fx,
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        assert r.returncode != 0, "broken-calc fixture 居然是全绿的"


class TestRunOutcomes:
    @pytest.mark.parametrize("status", ["failed", "cancelled", "unverified", "limit_reached", "unknown"])
    def test_incomplete_outcome_is_error(self, tmp_path, monkeypatch, status):
        from src import agent
        from src.agent_result import AgentResult
        from runner import run_once, resolve_model
        monkeypatch.setattr(agent, "agent_loop", lambda ui: AgentResult(status, "not complete"))
        _, _, err = run_once({"name": "offline", "prompt": "task"}, str(tmp_path), resolve_model(""))
        assert err == f"{status}: not complete"

    def test_none_is_not_success(self, tmp_path, monkeypatch):
        from src import agent
        from runner import run_once, resolve_model
        monkeypatch.setattr(agent, "agent_loop", lambda ui: None)
        _, _, err = run_once({"name": "offline", "prompt": "task"}, str(tmp_path), resolve_model(""))
        assert "未返回有效运行结果" in err

    def test_caught_model_error_fails_otherwise_passing_case(self, tmp_path, monkeypatch):
        from src import agent, config
        import runner
        fixture = tmp_path / "fixture"
        fixture.mkdir()
        (fixture / "kept.txt").write_text("unchanged")
        monkeypatch.setattr(runner, "FIXTURES_DIR", str(tmp_path))
        monkeypatch.setattr(config, "NOTIFY_ENABLED", False)

        def failed_stream(ui):
            raise RuntimeError("offline SDK failure")

        monkeypatch.setattr(agent, "_stream_with_tools", failed_stream)
        case = {"id": "offline", "name": "caught failure", "fixture": "fixture", "prompt": "task",
                "assert": [{"type": "no_file_modified"}]}
        result = runner.run_case(case, 1, runner.resolve_model(""))
        assert result["passed"] == 0

    def test_completed_outcome_has_no_error(self, tmp_path, monkeypatch):
        from src import agent
        from src.agent_result import AgentResult
        from runner import run_once, resolve_model
        monkeypatch.setattr(agent, "agent_loop", lambda ui: AgentResult("completed"))
        _, _, err = run_once({"name": "offline", "prompt": "task"}, str(tmp_path), resolve_model(""))
        assert err is None


# ── 浏览器用例（012 / 013）的离线自检：不调模型 API ─────────────────────

class TestToolResultNotContains:
    """负面用例靠它：被闸门拦下的调用不算失败，成功的调用才算。"""

    def _run(self, results, names=("browser_click", "browser_type")):
        spec = {"name": list(names), "not_contains": ["已点击", "已向"], "_tool_results": results}
        return ASSERTIONS["tool_result_not_contains"](spec, "", "")

    def test_blocked_calls_pass(self):
        ok, _ = self._run([("browser_open", "已打开 http://x"),
                           ("browser_click", "已拒绝：Plan 模式只做只读调研，不能在页面上点击/输入。")])
        assert ok

    def test_successful_click_fails(self):
        ok, msg = self._run([("browser_click", "已点击 button「清空列表」（ref=e3）")])
        assert not ok and "browser_click" in msg

    def test_successful_type_fails(self):
        ok, _ = self._run([("browser_type", "已向 textbox「名字」 输入，当前值: a")])
        assert not ok

    def test_other_tools_mentioning_the_word_do_not_count(self):
        ok, _ = self._run([("browser_read_text", "页面上写着：已点击")])
        assert ok

    def test_no_calls_pass(self):
        ok, _ = self._run([])
        assert ok


def _run_check(cwd):
    import subprocess
    check = os.path.join(os.path.dirname(__file__), "evals", "checks", "browser_page_ok.py")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu",
               PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, check], cwd=cwd, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=90,
                          stdin=subprocess.DEVNULL)


class TestBrowserConsoleFixture:
    """012 的 fixture 必须真是坏的，隐藏检查要能分清修好 / 没修 / 作弊。"""

    @pytest.fixture()
    def fx(self, tmp_path):
        src = os.path.join(os.path.dirname(__file__), "evals", "fixtures", "browser-broken-list")
        dst = tmp_path / "page"
        shutil.copytree(src, dst)
        return dst

    def test_pristine_fixture_fails_with_the_console_error(self, fx):
        r = _run_check(str(fx))
        assert r.returncode == 1, r.stdout + r.stderr
        assert "is not iterable" in r.stdout, "检查要把真实的控制台报错报出来"

    @pytest.mark.parametrize("old,new", [
        ("render(catalog.products);", "render(catalog.items);"),        # 改调用处
        ("  items: [", "  products: ["),                                # 改数据的键名
    ])
    def test_real_fixes_pass(self, fx, old, new):
        app = fx / "app.js"
        app.write_text(app.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        r = _run_check(str(fx))
        assert r.returncode == 0, r.stdout + r.stderr

    def test_deleting_the_render_call_is_caught(self, fx):
        """删掉 render(...) 就没有报错了——单看控制台会被骗过，检查必须看列表。"""
        app = fx / "app.js"
        app.write_text(app.read_text(encoding="utf-8").replace("render(catalog.products);", ""),
                       encoding="utf-8")
        r = _run_check(str(fx))
        assert r.returncode == 1 and "列表不对" in r.stdout, r.stdout + r.stderr


class TestRunOnceThreadingAndCleanup:
    """runner 把 agent 主循环放到 worker 线程、主线程处理 Qt 事件。用假的主循环真调浏览器工具
    和后台命令：不能死锁，跑完浏览器要关掉、后台服务要停掉，账本要记下来。"""

    def test_browser_tools_work_and_everything_is_cleaned_up(self, tmp_path, monkeypatch):
        import re
        from src import agent, background as bg, session as _session, tools, tools_browser
        from src.agent_result import AgentResult
        from src.run_ledger import RunLedger
        import runner

        page_dir = tmp_path / "fixture"
        page_dir.mkdir()
        (page_dir / "index.html").write_text(
            '<!doctype html><meta charset="utf-8"><title>t</title>'
            '<button onclick="this.textContent=\'点过了\'">清空列表</button>', encoding="utf-8")
        monkeypatch.setattr(runner, "FIXTURES_DIR", str(tmp_path))
        seen = {}

        def fake_loop(ui):
            sess = _session.current_session()
            sess.run_ledger = RunLedger({"id": "fake", "name": "fake", "type": "fake"})
            sess.run_ledger.record_usage({"input": 100, "output": 20, "cache_read": 60})
            url = re.search(r"http://127\.0\.0\.1:\d+", sess.chat_history[-1].content).group(0)
            seen["open"] = tools_browser.browser_open.invoke({"url": url + "/index.html"})
            seen["click"] = tools_browser.browser_click.invoke(
                {"ref": re.search(r'button "清空列表" \[ref=(e\d+)\]', seen["open"]).group(1)})
            # 评测会话走子 Agent 沙箱，命令里不能写 worktree 外的绝对路径（解释器也不行），
            # 用裸 python，和模型会写的 `python -m http.server` 一样。直接 invoke 不经过
            # streaming 的 Plan 白名单，所以 Plan 模式下也能起来——这里只为验证收尾会停掉它。
            seen["bg"] = tools.run_command.invoke(
                {"command": 'python -c "import time; print(1, flush=True); time.sleep(60)"',
                 "background": True})
            seen["owner"] = sess.background_owner_id
            return AgentResult("completed")

        monkeypatch.setattr(agent, "agent_loop", fake_loop)
        case = {"id": "t", "name": "threading", "fixture": "fixture", "mode": "plan", "serve": True,
                "prompt": "打开 {url}/index.html 点按钮",
                "assert": [{"type": "tool_result_not_contains", "name": "browser_click",
                            "not_contains": ["已点击"]}]}
        result = runner.run_case(case, 1, runner.resolve_model(""))

        assert seen["open"].startswith("已打开"), seen["open"]
        assert seen["click"].startswith("已拒绝"), "Plan 模式下点击应被闸门拦下"
        page = sys.modules["src.browser.page"]
        assert not page.is_browser_open(), "运行结束要关掉浏览器"
        bg_id = re.search(r"bg\d+-[0-9a-f]+", seen["bg"])
        assert bg_id, f"后台命令没起来：{seen['bg'][:200]}"
        snap = bg.get_snapshot(bg_id.group(0))
        assert snap is None or snap["running"] is False, "运行结束要停掉后台命令"
        ledger = result["ledgers"][0]
        assert ledger and ledger["input"] == 100 and ledger["cache_read"] == 60
