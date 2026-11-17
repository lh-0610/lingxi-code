"""项目没有测试时，浏览器检查能不能算验证（评测 012：模型修好了前端脚本、页面也确认好了，
却因为"没有收集到任何测试用例"一直过不了完成闸门，白绕 5~8 轮）。

四个条件缺一不可，这里逐条给反例：
1. 改的代码全是浏览器直接跑的脚本（.js / .mjs / .cjs）；
2. 页面是在最后一次改动**之后**重新加载的，读的就是这次加载的控制台、没有报错；
3. 项目确实没有测试——不带筛选跑过 run_tests、一个用例都没收集到；有测试就必须照跑；
4. 结果卡照实写证据是浏览器，不写"测试通过"。
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src import memory, result_view, run_records, session, tools, verification
from src.agent_result import AgentResult


def _v(*files, none_found=True, tests_run=True):
    v = verification.new_verification()
    for f in files:
        verification.mark_dirty(v, f)
    if tests_run:
        verification.mark_tests(v, None, "没有收集到任何测试用例", no_tests=none_found)
    verification.mark_diff_reviewed(v)
    return v


def _browser(v, errors=0, revision=None):
    rev = v["evidence_revision"] if revision is None else revision
    verification.mark_browser_check(v, errors=errors, url="http://127.0.0.1:8765/index.html",
                                    load_revision=rev)


def _test_gaps(v):
    return [g for g in verification.gaps_from_state(v) if "git_diff" not in g]


class TestGate:
    def test_clean_reload_after_the_last_edit_counts_when_the_project_has_no_tests(self):
        v = _v("app.js", "index.html")
        _browser(v)
        assert _test_gaps(v) == []

    def test_without_a_browser_check_the_gate_says_how_to_verify(self):
        v = _v("app.js")
        gaps = _test_gaps(v)
        assert len(gaps) == 1
        assert "browser_open" in gaps[0] and "browser_console" in gaps[0]

    def test_a_check_made_before_the_last_edit_does_not_count(self):
        v = _v("app.js")
        _browser(v)
        verification.mark_dirty(v, "app.js")          # 检查之后又改了
        verification.mark_tests(v, None, "没有收集到任何测试用例", no_tests=True)
        gaps = _test_gaps(v)
        assert gaps and "又改了文件" in gaps[0]

    def test_errors_in_the_reloaded_page_block_completion(self):
        v = _v("app.js")
        _browser(v, errors=1)
        gaps = _test_gaps(v)
        assert gaps and "还有报错" in gaps[0]

    def test_a_project_with_tests_still_has_to_run_them(self):
        """测试没跑起来（比如超时 / 没装）不是"项目没有测试"，浏览器顶替不了。"""
        v = _v("app.js", none_found=False)
        _browser(v)
        gaps = _test_gaps(v)
        assert gaps and "测试尚未验证" in gaps[0]

    def test_run_tests_must_have_confirmed_there_are_no_tests(self):
        v = _v("app.js", tests_run=False)
        _browser(v)
        gaps = _test_gaps(v)
        assert gaps and "尚未运行测试" in gaps[0]
        assert "浏览器检查" in gaps[0], "要告诉模型确认没测试之后还有浏览器这条路"

    @pytest.mark.parametrize("other", ["server.py", "app.ts", "App.jsx", "main.vue"])
    def test_only_scripts_the_browser_runs_directly(self, other):
        """改了 .py 浏览器测不到；.ts / .jsx / .vue 要先编译，页面上跑的未必是改后的代码。"""
        v = _v("app.js", other)
        _browser(v)
        gaps = _test_gaps(v)
        assert gaps and "测试尚未验证" in gaps[0]

    def test_blind_spots_are_not_covered_by_a_browser_check(self, tmp_path):
        v = _v("app.js")
        v["unknown_changes"] = {str(tmp_path): "有一段时间读不到目录"}
        _browser(v)
        assert any("测试尚未验证" in g for g in _test_gaps(v))

    def test_no_tests_flag_needs_an_inconclusive_result(self):
        v = verification.new_verification()
        verification.mark_tests(v, True, no_tests=True)
        assert v["tests_none_found"] is False
        verification.mark_tests(v, None, "没有收集到任何测试用例", no_tests=True)
        assert v["tests_none_found"] is True
        verification.mark_tests(v, None, "测试超时")
        assert v["tests_none_found"] is False, "后一次结论要覆盖前一次"

    def test_a_new_turn_forgets_the_browser_check_and_the_last_load(self):
        v = _v("app.js")
        _browser(v)
        v["browser_last_load"] = {"nav": 1, "evidence_revision": 1}
        verification.reset_verification(v)
        assert v["browser_check"] is None and "browser_last_load" not in v
        assert v["tests_none_found"] is False


# ── run_tests 只在"不带筛选、一个都没收集到"时认定项目没有测试 ──

def _new_session(tmp_path):
    sess = session.Session()
    sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="修一下页面"),
                         AIMessage(content="好")]
    session.register(sess)
    memory.save_session(session=sess)
    sess.project = str(tmp_path)
    session.bind_thread(sess)
    session.set_active(sess)
    run_records.begin_run(sess)
    return sess


@pytest.fixture()
def web_project(isolated_memory, tmp_path):
    (tmp_path / "index.html").write_text("<script src='app.js'></script>", encoding="utf-8")
    (tmp_path / "app.js").write_text("console.log(1)\n", encoding="utf-8")
    sess = _new_session(tmp_path)
    try:
        yield sess, tmp_path
    finally:
        session.unbind_thread()


class TestRunTestsNoTests:
    def test_unfiltered_run_with_nothing_collected_marks_no_tests(self, web_project):
        sess, _ = web_project
        out = tools.run_tests.func()
        assert "没有收集到" in out or "no tests ran" in out.lower(), out
        assert sess.verification["tests_none_found"] is True
        assert sess.verification["evidence"][-1].get("no_tests") is True

    def test_a_filter_that_selects_nothing_is_not_no_tests(self, web_project):
        sess, _ = web_project
        tools.run_tests.func(k="does_not_exist")
        assert sess.verification["tests_passed"] is None
        assert sess.verification["tests_none_found"] is False


# ── 结果卡：证据是浏览器就说浏览器 ──

class TestResultCard:
    def _finish(self, sess, status="completed"):
        return run_records.finalize_run(sess, sess.last_run, AgentResult(status, "")).snapshot

    def _browser_evidence(self, sess, status="passed"):
        verification.record_evidence(sess.verification, kind="browser", checker="浏览器控制台",
                                     path="http://127.0.0.1:8765/index.html", exit_code=None,
                                     status=status, summary="本次加载控制台无报错")

    def test_title_names_the_browser_and_says_there_are_no_tests(self, web_project):
        sess, _ = web_project
        verification.mark_dirty(sess.verification, "app.js")
        tools.run_tests.func()
        self._browser_evidence(sess)
        view = result_view.describe(self._finish(sess))
        assert view["title"] == "本轮执行结束，浏览器检查通过（项目没有测试）"
        assert "测试通过" not in view["title"]
        text = result_view.plain_text(view)
        assert "项目里没有测试用例" in text
        assert "浏览器检查" in text and "本次页面加载的控制台" in text
        assert "无退出码" not in text.split("浏览器检查")[1].splitlines()[0]

    def test_no_tests_without_a_browser_check_is_not_a_pass(self, web_project):
        sess, _ = web_project
        verification.mark_dirty(sess.verification, "app.js")
        tools.run_tests.func()
        assert result_view.summarize_checks(self._finish(sess)) == "unresolved"

    def test_a_failed_static_check_still_wins(self, web_project):
        sess, _ = web_project
        verification.mark_dirty(sess.verification, "app.js")
        tools.run_tests.func()
        self._browser_evidence(sess)
        verification.record_evidence(sess.verification, kind="check", checker="node",
                                     path="app.js", exit_code=1, status="failed")
        assert result_view.summarize_checks(self._finish(sess)) == "unresolved"

    def test_browser_kind_and_no_tests_survive_a_save_and_reload(self):
        raw = {"validation_runs": [
            {"kind": "browser", "status": "passed", "checker": "浏览器控制台"},
            {"kind": "tests", "status": "not_run", "no_tests": True}]}
        runs = run_records._normalize_evidence(raw)["validation_runs"]
        assert runs[0]["kind"] == "browser"
        assert runs[1].get("no_tests") is True
