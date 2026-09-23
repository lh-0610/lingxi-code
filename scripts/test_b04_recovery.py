"""恢复检查、继续入口与后台会话归属（B04）。

这一批回答的是"重开之后，能不能安全地接着干"：

1. **现场还是那个现场吗？** 锚点记下涉及文件的指纹、HEAD、分支、根提交；继续前逐项比对。
   两句话不能说反：指纹相同**不等于**测试通过，HEAD 相同**不等于**工作区相同。
2. **中断时那个写操作做了没有？** 不知道。结果未知的操作不重放、不说成功也不说没执行，
   而且**一律**要求补验证——它可能写了记录之外的路径。用户没点继续、直接发新消息也一样。
3. **继续的是不是同一件事？** 以会话 + run_id 核对归属；旧卡、后台会话、还在收尾的旧 worker
   都不能把两轮搅在一起。模型按稳定标识找回，找不到就要求重选；Plan 仍是 Plan。
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src import memory, recovery, run_records, session, state, verification, workspace_anchor
from src.agent_result import AgentResult


# ══════════════════════════════════════════════════════════════
# 夹具与造数
# ══════════════════════════════════════════════════════════════

def _rmtree(path):
    """删 .git 这类带只读文件的目录（Windows 上 git 对象文件是只读的）。"""
    def _retry(func, target, _exc):
        os.chmod(target, 0o700)
        func(target)
    shutil.rmtree(path, onexc=_retry)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout.strip()


def _make_repo(root):
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Tester")
    # 不 ignore 的话，测试自己留下的缓存会被当成"新出现的文件"
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "util.py").write_text("y = 2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture()
def repo(tmp_path):
    return _make_repo(tmp_path / "proj")


@pytest.fixture(autouse=True)
def _no_real_models(monkeypatch):
    """本文件任何用例都不许碰真实模型或本地 Claude CLI：误走到就当场失败，而不是悄悄发请求。

    新建的 Session 默认模型下标可能落在 Claude Code 上（CLAUDE.md backlog 里的已知问题），
    漏设一次模型就会真的起一个 claude 进程——实测踩到过一次，所以这里兜底阻断。
    """
    from src import agent as _agent
    from src import claude_code, models

    def _forbidden(*a, **k):
        raise AssertionError("测试不许调用真实模型或 Claude CLI")

    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(_agent, "_claude_code_loop", _forbidden)
    monkeypatch.setattr(claude_code, "claude_code_loop", _forbidden)


@pytest.fixture()
def no_config_issues(monkeypatch):
    """隔离副本里没有 config.json，所有 API 模型都会报"缺 key"。预检的其它判断要单独测。"""
    from src import models
    monkeypatch.setattr(models, "get_model_config_issues", lambda index=None: [])


def _api_models():
    from src.models import MODEL_LIST
    return [i for i, m in enumerate(MODEL_LIST) if m[1] not in ("claude-code", "ollama")]


class _UI:
    def __init__(self):
        self.messages = []

    def show_message(self, text, tag="ai_msg"):
        self.messages.append(str(text))

    def render_final_markdown(self, text, speak=True):
        self.messages.append(str(text))

    def show_retry(self, text):
        self.messages.append(str(text))

    def show_token_usage(self, *a, **k):
        pass

    def remove_thinking_indicator(self):
        pass

    def text(self):
        return "".join(self.messages)


def _code_session(root, *, text="修一下登录"):
    sess = session.Session()
    sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content=text),
                         AIMessage(content="好")]
    sess.agent_mode = "act"
    sess.current_model_index = _api_models()[0]
    session.register(sess)
    memory.save_session(session=sess)
    # 首次保存会把项目锚成全局 current_project（测试里是 None），所以之后再设、再存一次
    sess.project = str(root)
    memory.save_session(session=sess)
    return sess


def _disk(mem_dir, sid):
    with open(os.path.join(str(mem_dir), f"{sid}.json"), encoding="utf-8") as stream:
        return json.load(stream)


def _sidecar(mem_dir, sid):
    path = os.path.join(str(mem_dir), f"{sid}.inflight.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def _interrupted(root):
    """造一个"append_file 已调度、进程死在拿到结果之前"的会话。返回 (session_id, 崩前 run_id)。

    磁盘上留下的正是 B03 窗口①/②的样子：running 记录；带三个调用的 AIMessage，
    第一个（read_file）已应答、第二个（append_file）在 sidecar 里有记录但没有回执、
    第三个（run_command）根本没开始；append 的副作用**确实发生了**。
    """
    sess = _code_session(root)
    run = run_records.begin_run(sess)
    sess.chat_history.append(AIMessage(content="", tool_calls=[
        {"name": "read_file", "args": {"path": "app.py"}, "id": "c1"},
        {"name": "append_file", "args": {"path": "notes.txt", "content": "x\n"}, "id": "c2"},
        {"name": "run_command", "args": {"command": "echo hi"}, "id": "c3"},
    ]))
    sess.chat_history.append(ToolMessage(content="x = 1", tool_call_id="c1"))
    memory.save_session(session=sess)
    run_records.begin_operation(sess, tool="append_file", tool_call_id="c2",
                                args={"path": "notes.txt", "content": "x\n"})
    (root / "notes.txt").write_text("x\n", encoding="utf-8")
    return sess.current_session_id, run["id"]


def _reopen(sid):
    """重开程序：全新的 Session 对象，从磁盘读。"""
    sess = session.Session()
    sess.current_model_index = _api_models()[0]
    assert memory.load_session(sid, session=sess) is True
    session.register(sess)
    return sess


# ══════════════════════════════════════════════════════════════
# 现场锚点
# ══════════════════════════════════════════════════════════════

class TestWorkspaceAnchor:
    def test_capture_records_repository_identity_and_fingerprints(self, repo):
        anchor = workspace_anchor.capture(str(repo), ["app.py", "missing.py"])
        assert anchor["is_git"] is True
        assert anchor["git_head"] and anchor["git_branch"]
        assert anchor["git_roots"], "根提交是判断'还是不是同一个仓库'的依据"
        assert "sha256" in anchor["fingerprints"]["app.py"]
        assert anchor["fingerprints"]["missing.py"] == {"missing": True}

    def test_changed_deleted_and_appeared_files_are_reported(self, repo):
        (repo / "gone.py").write_text("z\n", encoding="utf-8")
        anchor = workspace_anchor.capture(str(repo), ["app.py", "gone.py", "new.py"])
        (repo / "app.py").write_text("x = 999\n", encoding="utf-8")
        (repo / "gone.py").unlink()
        (repo / "new.py").write_text("n\n", encoding="utf-8")

        report = workspace_anchor.compare(anchor, str(repo))
        assert report["changed"] == ["app.py"]
        assert report["deleted"] == ["gone.py"]
        assert report["appeared"] == ["new.py"]
        assert report["checked"] == 3
        assert report["blocking"] == []

    def test_same_head_is_not_the_same_workspace(self, repo):
        """HEAD 没变、文件变了：必须照样报出来，不能拿 HEAD 相同推出现场没变。"""
        anchor = workspace_anchor.capture(str(repo), ["app.py"])
        (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
        report = workspace_anchor.compare(anchor, str(repo))
        assert report["head_change"] is None
        assert report["changed"] == ["app.py"]

    def test_new_commit_and_branch_switch_are_reported(self, repo):
        anchor = workspace_anchor.capture(str(repo), [])
        (repo / "later.py").write_text("l\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "later")
        _git(repo, "checkout", "-q", "-b", "other-branch")
        report = workspace_anchor.compare(anchor, str(repo))
        assert report["head_change"] and report["head_change"][0] != report["head_change"][1]
        assert report["branch_change"][1] == "other-branch"
        assert report["blocking"] == [], "同一个仓库里换分支不是阻断，是要报告的变化"

    def test_a_different_repository_at_the_same_path_blocks(self, repo):
        anchor = workspace_anchor.capture(str(repo), ["app.py"])
        _rmtree(repo / ".git")
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "other@example.com")
        _git(repo, "config", "user.name", "Other")
        (repo / "other.txt").write_text("another project\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "someone else's history")
        report = workspace_anchor.compare(anchor, str(repo))
        assert any("另一个仓库" in b for b in report["blocking"])

    def test_no_longer_a_repository_blocks(self, repo):
        anchor = workspace_anchor.capture(str(repo), ["app.py"])
        _rmtree(repo / ".git")
        report = workspace_anchor.compare(anchor, str(repo))
        assert any("现在不是" in b for b in report["blocking"])

    def test_missing_directory_blocks(self, repo, tmp_path):
        anchor = workspace_anchor.capture(str(repo), ["app.py"])
        repo.rename(tmp_path / "moved")
        report = workspace_anchor.compare(anchor, str(repo))
        assert any("不存在" in b for b in report["blocking"])

    def test_plain_directory_still_gets_file_level_comparison(self, tmp_path):
        root = tmp_path / "plain"
        root.mkdir()
        (root / "a.txt").write_text("1", encoding="utf-8")
        anchor = workspace_anchor.capture(str(root), ["a.txt"])
        assert anchor["is_git"] is False and anchor["errors"] == [], "不是 git 仓库不是错误"
        (root / "a.txt").write_text("2", encoding="utf-8")
        assert workspace_anchor.compare(anchor, str(root))["changed"] == ["a.txt"]

    def test_legacy_run_without_anchor_is_reported_not_guessed(self, repo):
        report = workspace_anchor.compare(None, str(repo))
        assert report["incomplete"] and "没有记录现场锚点" in report["incomplete"][0]
        assert report["changed"] == [] and report["blocking"] == []

    def test_large_file_is_compared_by_size_and_says_so(self, repo, monkeypatch):
        monkeypatch.setattr(workspace_anchor, "MAX_HASH_BYTES", 3)
        anchor = workspace_anchor.capture(str(repo), ["app.py"])
        assert "too_large" in anchor["fingerprints"]["app.py"]
        same_size = workspace_anchor.compare(anchor, str(repo))
        assert same_size["changed"] == [] and any("只比对了大小" in r
                                                  for r in same_size["incomplete"])
        (repo / "app.py").write_text("x = 12345\n", encoding="utf-8")
        assert workspace_anchor.compare(anchor, str(repo))["changed"] == ["app.py"]

    def test_normalize_drops_garbage_instead_of_trusting_it(self):
        assert workspace_anchor.normalize("nope") is None
        assert workspace_anchor.normalize({"root": 5}) is None
        clean = workspace_anchor.normalize({"root": "D:/p", "is_git": "yes",
                                            "fingerprints": {"a": {"sha256": "x", "evil": 1},
                                                             "b": "not-a-dict"}})
        assert clean["is_git"] is False, "只认真正的 True"
        assert clean["fingerprints"] == {"a": {"sha256": "x"}}

    def test_same_repository_check(self, repo, tmp_path):
        anchor = workspace_anchor.capture(str(repo), [])
        copy = tmp_path / "copy"
        _git(tmp_path, "clone", "-q", str(repo), str(copy))
        assert workspace_anchor.same_repository(anchor, str(copy))[0] is True
        # 内容、作者、时间都相同的两个 init 提交哈希会撞上，所以另起一个内容不同的仓库
        third = tmp_path / "third"
        third.mkdir()
        _git(third, "init", "-q")
        _git(third, "config", "user.email", "z@example.com")
        _git(third, "config", "user.name", "Z")
        (third / "t.txt").write_text("t", encoding="utf-8")
        _git(third, "add", "-A")
        _git(third, "commit", "-q", "-m", "unrelated")
        assert workspace_anchor.same_repository(anchor, str(third))[0] is False
        plain = tmp_path / "plain"
        plain.mkdir()
        assert workspace_anchor.same_repository(anchor, str(plain))[0] is False
        assert workspace_anchor.same_repository({"is_git": False}, str(copy))[0] is None


# ══════════════════════════════════════════════════════════════
# 运行记录里的模型与锚点
# ══════════════════════════════════════════════════════════════

class TestRunRecordAnchor:
    def test_model_is_a_stable_identity_without_index_or_key(self, isolated_memory, repo):
        from src.models import MODEL_LIST
        sess = _code_session(repo)
        run = run_records.begin_run(sess)
        name, mtype, model_id, _ = MODEL_LIST[sess.current_model_index]
        assert run["model"] == {"id": model_id, "name": name, "type": mtype}
        stored = _disk(isolated_memory, sess.current_session_id)["progress"]["last_run"]
        assert set(stored["model"]) == {"id", "name", "type"}, "不存下标、不存密钥"
        assert "key" not in json.dumps(stored["model"]).lower()

    def test_anchor_and_model_survive_reload_and_bad_values_degrade(self, isolated_memory, repo):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        sid = sess.current_session_id
        again = _reopen(sid)
        assert again.last_run["workspace"]["is_git"] is True
        assert again.last_run["model"]["id"]

        data = _disk(isolated_memory, sid)
        data["progress"]["last_run"]["model"] = "gpt-by-index-3"
        data["progress"]["last_run"]["workspace"] = ["garbage"]
        with open(os.path.join(str(isolated_memory), f"{sid}.json"), "w",
                  encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
        broken = _reopen(sid)
        assert broken.progress_error == "", "坏掉的锚点不能拖垮整份进度"
        assert broken.last_run["id"] == data["progress"]["last_run"]["id"]
        assert broken.last_run["model"] is None and broken.last_run["workspace"] is None

    def test_tool_boundary_refreshes_the_anchor_so_own_edits_are_not_external(
            self, isolated_memory, repo, monkeypatch):
        """工具边界不刷新锚点的话，崩在这之后的恢复会把本轮自己刚改的文件报成"被外部修改"。"""
        from src import streaming
        sess = _code_session(repo)
        session.bind_thread(sess)
        session.set_active(sess)
        run_records.begin_run(sess)

        class _Write:
            def invoke(self, args):
                (repo / "app.py").write_text("x = 42\n", encoding="utf-8")
                verification.mark_dirty(sess.verification, "app.py")
                return "已写入 app.py"

        monkeypatch.setattr(streaming, "get_tool_map", lambda: {"write_file": _Write()})
        try:
            streaming._execute_tool({"name": "write_file", "args": {"path": "app.py"},
                                     "id": "w1"}, _UI())
        finally:
            session.unbind_thread()
        again = _reopen(sess.current_session_id)
        report = workspace_anchor.compare(again.last_run["workspace"], str(repo))
        assert "app.py" in again.last_run["workspace"]["fingerprints"]
        assert report["changed"] == [], "自己刚写的内容不是外部修改"

    def test_file_only_tools_skip_the_git_subprocess_but_commands_do_not(
            self, isolated_memory, repo, monkeypatch):
        """一次 git 子进程几十毫秒，是整套工具边界提交的好几倍；只写文件的工具改不了 HEAD，
        不必为它起。命令可能切分支 / 提交，照常刷新。"""
        from src import streaming
        sess = _code_session(repo)
        session.bind_thread(sess)
        session.set_active(sess)
        run_records.begin_run(sess)
        git_calls = []
        real_git = workspace_anchor._git
        monkeypatch.setattr(workspace_anchor, "_git",
                            lambda root, *args: (git_calls.append(args), real_git(root, *args))[1])

        class _Write:
            def invoke(self, args):
                (repo / "app.py").write_text("x = 7\n", encoding="utf-8")
                verification.mark_dirty(sess.verification, "app.py")
                return "已写入"

        class _Command:
            def invoke(self, args):
                return "ok"

        monkeypatch.setattr(streaming, "get_tool_map",
                            lambda: {"write_file": _Write(), "run_command": _Command()})
        try:
            streaming._execute_tool({"name": "write_file", "args": {"path": "app.py"},
                                     "id": "w1"}, _UI())
            assert git_calls == [], "只写文件的工具边界不该起 git 子进程"
            anchor = sess.last_run["workspace"]
            assert anchor["git_head"] and anchor["is_git"], "沿用上一次的 git 信息，不是清空"
            assert "app.py" in anchor["fingerprints"], "指纹照样重算"
            streaming._execute_tool({"name": "run_command", "args": {"command": "echo hi"},
                                     "id": "r1"}, _UI())
            assert any(args[0] == "rev-parse" for args in git_calls), "命令之后要重读 HEAD / 分支"
        finally:
            session.unbind_thread()


# ══════════════════════════════════════════════════════════════
# 主线程预检
# ══════════════════════════════════════════════════════════════

class TestPrecheck:
    def test_missing_project_directory_blocks(self, isolated_memory, repo, tmp_path,
                                              no_config_issues):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        repo.rename(tmp_path / "moved-away")
        pre = recovery.precheck(sess)
        assert pre.project_missing == str(repo)
        assert any("项目目录已不存在" in b for b in pre.blocking)

    def test_recorded_model_is_restored_by_stable_id(self, isolated_memory, repo,
                                                     no_config_issues):
        from src.models import MODEL_LIST
        sess = _code_session(repo)
        first, second = _api_models()[:2]
        sess.current_model_index = first
        run_records.begin_run(sess)
        sess.current_model_index = second     # 重开后继承了别的会话的模型
        pre = recovery.precheck(sess)
        assert pre.blocking == []
        assert pre.model_index == first
        assert any(MODEL_LIST[first][0] in n for n in pre.notes)

    def test_removed_model_requires_an_explicit_choice(self, isolated_memory, repo,
                                                       no_config_issues):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        sess.last_run["model"] = {"id": "model-that-was-removed", "name": "旧模型", "type": "custom"}
        pre = recovery.precheck(sess)
        assert pre.model_missing is True
        assert any("旧模型" in b and "不可用" in b for b in pre.blocking)

        accepted = recovery.precheck(sess, accept_current_model=True)
        assert accepted.blocking == []
        assert accepted.model_index == sess.current_model_index
        assert any("按你的选择改用" in n for n in accepted.notes)

    def test_a_model_the_user_picked_in_the_header_is_respected(self, isolated_memory, repo,
                                                                no_config_issues):
        """重开的会话模型是继承来的，才按记录找回；用户特意换过的不能又悄悄改回去。"""
        from src.models import MODEL_LIST
        sess = _code_session(repo)
        first, second = _api_models()[:2]
        sess.current_model_index = first
        run_records.begin_run(sess)
        sess.current_model_index = second
        sess.model_user_choice = True
        pre = recovery.precheck(sess)
        assert pre.blocking == [] and pre.model_index == second
        assert any("按你在顶栏的选择" in n and MODEL_LIST[second][0] in n for n in pre.notes)

    def test_a_header_choice_also_answers_a_removed_model(self, isolated_memory, repo,
                                                          no_config_issues):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        sess.last_run["model"] = {"id": "model-that-was-removed", "name": "旧模型", "type": "custom"}
        sess.model_user_choice = True
        pre = recovery.precheck(sess)
        assert pre.blocking == [] and pre.model_index == sess.current_model_index

    def test_header_model_switch_marks_the_choice(self, qt_app, monkeypatch):
        from src import agent as _agent
        from src.ui.header import HeaderMixin

        class _Btn:
            def setEnabled(self, *_):
                pass

            def setChecked(self, *_):
                pass

        class _Header:
            _on_model_changed = HeaderMixin._on_model_changed

            def __init__(self):
                self.think_btn = _Btn()

            def _show_current_model_config_warning(self):
                pass

            def _force_stop_generation(self):
                pass

        monkeypatch.setattr(_agent, "switch_model", lambda i: None)
        monkeypatch.setattr(_agent, "set_reasoning", lambda enabled: None)
        active = session.get_active()
        assert active.model_user_choice is False, "新会话 / 重开的会话还没人选过"
        _Header()._on_model_changed(_api_models()[1])
        assert active.model_user_choice is True

    def test_same_id_on_another_backend_counts_as_missing(self, isolated_memory, repo,
                                                          no_config_issues):
        """同一个 model_id 换了类型就是另一种执行后端——不能静默落过去。"""
        from src.models import MODEL_LIST
        sess = _code_session(repo)
        run_records.begin_run(sess)
        _, mtype, model_id, _ = MODEL_LIST[sess.current_model_index]
        sess.last_run["model"] = {"id": model_id, "name": "x", "type": "claude-code"}
        assert recovery.find_model(sess.last_run["model"]) is None
        assert recovery.precheck(sess).model_missing is True

    def test_configuration_problems_block(self, isolated_memory, repo, monkeypatch):
        from src import models
        monkeypatch.setattr(models, "get_model_config_issues",
                            lambda index=None: ["某模型 需要先在 ⚙ 设置里填 api_key。"])
        sess = _code_session(repo)
        run_records.begin_run(sess)
        assert any("api_key" in b for b in recovery.precheck(sess).blocking)

    def test_plan_mode_is_kept_and_said(self, isolated_memory, repo, no_config_issues):
        sess = _code_session(repo)
        sess.agent_mode = "plan"
        run_records.begin_run(sess)
        pre = recovery.precheck(sess)
        assert sess.agent_mode == "plan"
        assert any("Plan" in n for n in pre.notes)

    def test_legacy_run_without_model_uses_current_and_says_so(self, isolated_memory, repo,
                                                               no_config_issues):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        sess.last_run["model"] = None
        pre = recovery.precheck(sess)
        assert pre.blocking == [] and pre.model_index == sess.current_model_index
        assert any("没有记录所用模型" in n for n in pre.notes)


# ══════════════════════════════════════════════════════════════
# 恢复处置（worker 侧）
# ══════════════════════════════════════════════════════════════

def _messages_for(history, call_id):
    return [m for m in history if isinstance(m, ToolMessage) and m.tool_call_id == call_id]


class TestResumeApplication:
    def test_placeholders_distinguish_unknown_from_never_started(self, isolated_memory, repo):
        sid, _old = _interrupted(repo)
        sess = _reopen(sid)
        check = recovery.inspect_site(sess)
        recovery.apply_resume(sess, check)

        unknown = _messages_for(sess.chat_history, "c2")
        never = _messages_for(sess.chat_history, "c3")
        assert len(unknown) == 1 and len(never) == 1
        assert unknown[0].content.startswith("应用恢复提示：未取得执行结果")
        assert "可能已经执行" in unknown[0].content and "不要直接重复" in unknown[0].content
        assert "没有这次 `run_command` 调用的执行记录" in never[0].content
        assert "按未执行处理" in never[0].content
        for msg in unknown + never:
            assert msg.additional_kwargs.get("lingxi_internal") is True
            assert msg.additional_kwargs.get("lingxi_kind") == recovery.RESUME_KIND
            assert "成功" not in msg.content, "不能冒充工具返回了结果"
        # 协议：占位紧跟在同一个 AIMessage 的结果区里
        ai_index = next(i for i, m in enumerate(sess.chat_history)
                        if isinstance(m, AIMessage) and m.tool_calls)
        following = [m.tool_call_id for m in sess.chat_history[ai_index + 1:ai_index + 4]]
        assert following == ["c1", "c2", "c3"]

    def test_summary_is_program_text_listing_unknown_operations(self, isolated_memory, repo):
        sid, _old = _interrupted(repo)
        sess = _reopen(sid)
        recovery.apply_resume(sess, recovery.inspect_site(sess))
        summary = sess.chat_history[-1]
        assert isinstance(summary, HumanMessage)
        assert summary.additional_kwargs["lingxi_internal"] is True
        assert summary.additional_kwargs["lingxi_kind"] == recovery.RESUME_KIND
        assert "不是用户的新要求" in summary.content
        assert "append_file" in summary.content and "notes.txt" in summary.content
        assert "不要直接重复" in summary.content
        assert "运行被中断" in summary.content

    def test_unknown_write_forces_tests_and_diff_in_the_next_run(self, isolated_memory, repo):
        """最关键的一条：结果未知的写操作从未进入 dirty 文件，新一轮的基线又建立在它之后——
        不并入义务的话，完成闸门会拿"没有已知改动"推出"工作区没变"。"""
        sid, _old = _interrupted(repo)
        sess = _reopen(sid)
        recovery.apply_resume(sess, recovery.inspect_site(sess))
        run_records.begin_run(sess)

        v = sess.verification
        assert "notes.txt" in v["dirty_files"]
        assert v.get("unknown_changes"), "结果未知的写操作必须打开盲区"
        assert v["tests_passed"] is None
        gaps = verification.get_verification_gaps(v)
        joined = "\n".join(gaps)
        assert "run_tests" in joined and "git_diff" in joined

    def test_continued_run_is_marked_continue_but_keeps_the_real_request(self, isolated_memory,
                                                                         repo):
        sid, _old = _interrupted(repo)
        sess = _reopen(sid)
        recovery.apply_resume(sess, recovery.inspect_site(sess))
        run = run_records.begin_run(sess)
        assert run["source"]["kind"] == "continue"
        assert run["source"]["text"] == "修一下登录", "任务仍是用户那条真实消息提的"

    def test_inflight_is_kept_when_the_resume_note_cannot_be_saved(self, isolated_memory, repo,
                                                                   monkeypatch):
        sid, _old = _interrupted(repo)
        sess = _reopen(sid)
        monkeypatch.setattr(memory, "save_session_report",
                            lambda **k: run_records.SaveOutcome(error=OSError("磁盘满了")))
        ui = _UI()
        assert recovery.prepare_and_apply(sess, ui=ui, mode="continue",
                                          expected_run_id=sess.last_run["id"]) is True
        ops = _sidecar(isolated_memory, sid)["operations"]
        assert [op["tool"] for op in ops] == ["append_file"], "说明没存上就不能摘掉唯一线索"
        assert "未能保存" in ui.text()

    def test_inflight_is_handed_over_after_save_with_the_diagnosis_kept(self, isolated_memory,
                                                                        repo):
        sid, _old = _interrupted(repo)
        sess = _reopen(sid)
        op_id = _sidecar(isolated_memory, sid)["operations"][0]["operation_id"]
        assert recovery.prepare_and_apply(sess, ui=_UI(), mode="continue",
                                          expected_run_id=sess.last_run["id"]) is True
        assert _sidecar(isolated_memory, sid) is None, "交接后 sidecar 里不再留这条"

        stored = _disk(isolated_memory, sid)["messages"][-1]
        assert stored["lingxi_kind"] == "resume"
        ops = stored["lingxi_recovery"]["operations"]
        assert ops[0]["operation_id"] == op_id and ops[0]["tool"] == "append_file"
        assert ops[0]["paths"] == ["notes.txt"]
        again = _reopen(sid)
        kwargs = again.chat_history[-1].additional_kwargs
        assert kwargs["lingxi_recovery"]["operations"][0]["operation_id"] == op_id

    def test_second_reopen_does_not_report_the_same_operation_again(self, isolated_memory, repo):
        sid, _old = _interrupted(repo)
        sess = _reopen(sid)
        recovery.prepare_and_apply(sess, ui=_UI(), mode="continue",
                                   expected_run_id=sess.last_run["id"])
        again = _reopen(sid)
        assert recovery.inspect_site(again).unknown_ops == []
        assert recovery.inspect_site(again).dangling == [], "占位已落盘，不再悬空"

    def test_unreadable_sidecar_is_treated_as_unknown(self, isolated_memory, repo):
        sid, _old = _interrupted(repo)
        with open(os.path.join(str(isolated_memory), f"{sid}.inflight.json"), "w",
                  encoding="utf-8") as stream:
            stream.write("{ not json")
        sess = _reopen(sid)
        check = recovery.inspect_site(sess)
        assert check.inflight_unreadable
        recovery.apply_resume(sess, check)
        c2 = _messages_for(sess.chat_history, "c2")[0].content
        assert "可能已经执行" in c2, "读不出记录就不能说成'没有执行记录'"
        run_records.begin_run(sess)
        assert sess.verification.get("unknown_changes")

    def test_blocking_site_changes_nothing(self, isolated_memory, repo):
        sid, _old = _interrupted(repo)
        _rmtree(repo / ".git")
        sess = _reopen(sid)
        before_disk = _disk(isolated_memory, sid)
        before_len = len(sess.chat_history)
        ui = _UI()
        assert recovery.prepare_and_apply(sess, ui=ui, mode="continue",
                                          expected_run_id=sess.last_run["id"]) is False
        assert "无法继续" in ui.text()
        assert len(sess.chat_history) == before_len
        assert _disk(isolated_memory, sid) == before_disk
        assert _sidecar(isolated_memory, sid)["operations"], "被拦下时执行前记录原样保留"

    def test_stale_card_changes_nothing(self, isolated_memory, repo):
        sid, _old = _interrupted(repo)
        sess = _reopen(sid)
        before_len = len(sess.chat_history)
        assert recovery.prepare_and_apply(sess, ui=_UI(), mode="continue",
                                          expected_run_id="run-from-an-older-card") is False
        assert len(sess.chat_history) == before_len

    def test_external_edits_are_detected_and_become_obligations(self, isolated_memory, repo):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        (repo / "app.py").write_text("x = 3\n", encoding="utf-8")
        verification.mark_dirty(sess.verification, "app.py")
        verification.mark_dirty(sess.verification, "util.py")
        run_records.finalize_run(sess, sess.last_run, AgentResult("cancelled", "用户停止"))
        sid = sess.current_session_id

        (repo / "app.py").write_text("x = 'someone else'\n", encoding="utf-8")   # 外部改动
        (repo / "util.py").unlink()                                             # 外部删除
        again = _reopen(sid)
        check = recovery.inspect_site(again)
        assert check.site["changed"] == ["app.py"] and check.site["deleted"] == ["util.py"]
        recovery.apply_resume(again, check)
        summary = again.chat_history[-1].content
        assert "app.py：内容与上次记录不同" in summary and "util.py：已被删除" in summary
        run_records.begin_run(again)
        assert {"app.py", "util.py"} <= set(again.verification["dirty_files"])

    def test_long_change_lists_are_capped_but_every_file_is_an_obligation(self, isolated_memory,
                                                                          repo):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        names = [f"m{i:02d}.txt" for i in range(35)]
        for name in names:
            (repo / name).write_text("a", encoding="utf-8")
            verification.mark_dirty(sess.verification, name)
        run_records.finalize_run(sess, sess.last_run, AgentResult("cancelled", "停"))
        for name in names:
            (repo / name).write_text("b", encoding="utf-8")       # 全部被外部改过
        again = _reopen(sess.current_session_id)
        recovery.apply_resume(again, recovery.inspect_site(again))
        summary = again.chat_history[-1].content
        assert summary.count("内容与上次记录不同") == 30
        assert "另有 5 处变化未列出" in summary
        run_records.begin_run(again)
        assert set(names) <= set(again.verification["dirty_files"]), "没列出的照样要验证"

    def test_head_change_without_anything_at_stake_is_reported_not_required(
            self, isolated_memory, repo):
        """已结束的纯问答轮次：HEAD 变了照实说，但不能因此把新一轮拖进测试要求。"""
        sess = _code_session(repo)
        run_records.begin_run(sess)
        run_records.finalize_run(sess, sess.last_run, AgentResult("cancelled", "用户停止"))
        (repo / "later.py").write_text("l\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "someone committed")
        again = _reopen(sess.current_session_id)
        check = recovery.inspect_site(again)
        assert check.at_stake is False and check.site["head_change"]
        recovery.apply_resume(again, check)
        assert "Git HEAD" in again.chat_history[-1].content
        run_records.begin_run(again)
        assert verification.get_verification_gaps(again.verification) == []

    def test_head_change_with_pending_work_becomes_an_obligation(self, isolated_memory, repo):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        (repo / "app.py").write_text("x = 5\n", encoding="utf-8")
        verification.mark_dirty(sess.verification, "app.py")
        run_records.finalize_run(sess, sess.last_run, AgentResult("unverified", "没测"))
        _git(repo, "checkout", "-q", "-b", "elsewhere")
        again = _reopen(sess.current_session_id)
        check = recovery.inspect_site(again)
        assert check.at_stake is True
        recovery.apply_resume(again, check)
        run_records.begin_run(again)
        blind = again.verification.get("unknown_changes") or {}
        assert any("分支已变化" in reason for reason in blind.values())

    def test_record_without_anchor_is_reported_as_unverifiable(self, isolated_memory, repo):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        run_records.finalize_run(sess, sess.last_run, AgentResult("failed", "连接失败"))
        sess.last_run["workspace"] = None        # 本功能之前的旧会话
        check = recovery.inspect_site(sess)
        recovery.apply_resume(sess, check)
        assert "没有记录现场锚点" in sess.chat_history[-1].content
        assert "内容与现在一致" not in sess.chat_history[-1].content


# ══════════════════════════════════════════════════════════════
# 真实 agent_loop：继续 / 中断后直接发消息
# ══════════════════════════════════════════════════════════════

class _Spy:
    def __init__(self):
        self.calls = []

    def invoke(self, args):
        self.calls.append(args)
        return "不该被调用"


@pytest.fixture()
def loop_env(monkeypatch, isolated_memory, repo):
    """真走 agent_loop；只把模型换成桩、把真实工具换成间谍（任何重放都会被记下）。"""
    from src import agent as _agent
    from src import streaming
    spies = {name: _Spy() for name in ("append_file", "run_command", "write_file", "read_file")}
    monkeypatch.setattr(streaming, "get_tool_map", lambda: dict(spies))
    monkeypatch.setattr(_agent, "maybe_generate_session_title", lambda *a, **k: None)
    yield _agent, spies, isolated_memory, repo
    session.unbind_thread()


def _bind(sess):
    session.bind_thread(sess)
    session.set_active(sess)


class _Stream:
    """模型桩：记录每次调用时**真正要发出去的历史**（走真实的 _prepare_stream_history）。"""

    def __init__(self, replies=("我先核对一下现场。",)):
        self.replies = list(replies)
        self.sent = []
        self.modes = []

    def __call__(self, ui):
        from src import streaming
        history, _rec = streaming._prepare_stream_history(ui)
        self.sent.append(history)
        self.modes.append(state.agent_mode)
        text = self.replies[min(len(self.sent) - 1, len(self.replies) - 1)]
        return text, [], {"input": 1, "output": 1, "total": 2}, None


class TestAgentLoopContinue:
    def test_continue_never_replays_and_cannot_end_as_completed(self, loop_env, monkeypatch):
        _agent, spies, mem, repo = loop_env
        sid, old_run = _interrupted(repo)
        sess = _reopen(sid)
        _bind(sess)
        stream = _Stream()
        monkeypatch.setattr(_agent, "_stream_with_tools", stream)

        result = _agent.agent_loop(_UI(), resume={"expected_run_id": old_run, "notes": []})

        assert all(not spy.calls for spy in spies.values()), "结果未知的操作绝不自动重放"
        assert (repo / "notes.txt").read_text(encoding="utf-8") == "x\n"
        assert result.status == "unverified", "没补验证就不能按 completed 收尾"
        first = stream.sent[0]
        c2 = [m for m in first if isinstance(m, ToolMessage) and m.tool_call_id == "c2"]
        assert c2 and c2[0].content.startswith("应用恢复提示：未取得执行结果")
        assert any(isinstance(m, HumanMessage) and "[继续任务" in str(m.content) for m in first)
        assert sess.last_run["id"] != old_run
        assert sess.last_run["source"]["kind"] == "continue"
        assert _sidecar(mem, sid) is None

    def test_blocked_continue_starts_no_run_and_calls_no_model(self, loop_env, monkeypatch):
        _agent, spies, mem, repo = loop_env
        sid, old_run = _interrupted(repo)
        _rmtree(repo / ".git")
        sess = _reopen(sid)
        _bind(sess)
        stream = _Stream()
        monkeypatch.setattr(_agent, "_stream_with_tools", stream)
        ui = _UI()

        result = _agent.agent_loop(ui, resume={"expected_run_id": old_run, "notes": []})

        assert result.status == "failed" and "恢复检查未通过" in result.reason
        assert stream.sent == [], "被拦下就不调模型"
        assert sess.last_run["id"] == old_run
        assert _disk(mem, sid)["progress"]["last_run"]["phase"] == "running", \
            "磁盘上仍是上一轮的事实，不因为一次被拦下的继续而改写"
        assert "无法继续" in ui.text()

    def test_plan_mode_survives_continue(self, loop_env, monkeypatch):
        _agent, spies, mem, repo = loop_env
        sid, old_run = _interrupted(repo)
        data = _disk(mem, sid)
        data["agent_mode"] = "plan"
        with open(os.path.join(str(mem), f"{sid}.json"), "w", encoding="utf-8") as stream_:
            json.dump(data, stream_, ensure_ascii=False)
        sess = _reopen(sid)
        _bind(sess)
        stream = _Stream()
        monkeypatch.setattr(_agent, "_stream_with_tools", stream)

        _agent.agent_loop(_UI(), resume={"expected_run_id": old_run, "notes": []})
        assert stream.modes and set(stream.modes) == {"plan"}, "恢复不暗中切到 Act"
        assert sess.agent_mode == "plan"

    def test_old_green_tests_are_not_a_pass_for_the_continued_run(self, loop_env, monkeypatch):
        _agent, spies, mem, repo = loop_env
        sess = _code_session(repo)
        run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "app.py")
        verification.mark_tests(sess.verification, True, "3 passed")
        run_records.finalize_run(sess, sess.last_run, AgentResult("unverified", "diff 没看"))
        again = _reopen(sess.current_session_id)
        _bind(again)
        seen = {}

        def _peek(ui):
            seen["tests_passed"] = again.verification["tests_passed"]
            seen["dirty"] = list(again.verification["dirty_files"])
            return "好", [], {"input": 0, "output": 0, "total": 0}, None

        monkeypatch.setattr(_agent, "_stream_with_tools", _peek)
        _agent.agent_loop(_UI(), resume={"expected_run_id": again.last_run["id"], "notes": []})
        assert seen["tests_passed"] is None, "历史测试成功只是历史证据"
        assert "app.py" in seen["dirty"]


class TestTypedMessageAfterCrash:
    def test_a_plain_message_still_gets_the_recovery_handling(self, loop_env, monkeypatch):
        """用户没点继续、直接打字：不阻断新请求，但结果未知的写入照样要求补验证。"""
        _agent, spies, mem, repo = loop_env
        sid, old_run = _interrupted(repo)
        sess = _reopen(sid)
        _bind(sess)
        sess.chat_history.append(HumanMessage(content="继续"))
        stream = _Stream()
        monkeypatch.setattr(_agent, "_stream_with_tools", stream)

        result = _agent.agent_loop(_UI())

        assert all(not spy.calls for spy in spies.values())
        assert result.status == "unverified"
        user_at = next(i for i, m in enumerate(sess.chat_history)
                       if isinstance(m, HumanMessage) and m.content == "继续")
        note = sess.chat_history[user_at + 1]
        assert note.additional_kwargs.get("lingxi_kind") == recovery.RECOVERY_KIND
        assert "append_file" in note.content and "不要直接重复" in note.content
        assert sess.last_run["source"]["kind"] == "user_message"
        assert sess.last_run["source"]["text"] == "继续"
        assert _sidecar(mem, sid) is None
        assert _messages_for(sess.chat_history, "c2"), "悬空调用在历史里补上了占位"

    def test_a_normally_finished_session_gets_no_recovery_note(self, loop_env, monkeypatch):
        _agent, spies, mem, repo = loop_env
        sess = _code_session(repo)
        run_records.begin_run(sess)
        run_records.finalize_run(sess, sess.last_run, AgentResult("completed"))
        again = _reopen(sess.current_session_id)
        _bind(again)
        again.chat_history.append(HumanMessage(content="再问一句"))
        monkeypatch.setattr(_agent, "_stream_with_tools", _Stream(("答",)))
        assert recovery.needs_adoption(again) is False
        _agent.agent_loop(_UI())
        kinds = [m.additional_kwargs.get("lingxi_kind") for m in again.chat_history
                 if isinstance(m, HumanMessage)]
        assert recovery.RECOVERY_KIND not in kinds and recovery.RESUME_KIND not in kinds


class TestNothingIsResurrected:
    def test_allowlists_worktree_and_threads_are_not_persisted(self, isolated_memory, repo,
                                                               no_config_issues):
        sess = _code_session(repo)
        sess.command_allowlist.add("deploy --token-remembered-once")
        sess.command_prefix_allowlist.add("deploy-prefix-remembered")
        sess.edit_path_allowlist.add("edit-path-remembered.py")
        sess.worktree = str(repo / ".lingxi-worktrees" / "wt1")
        run_records.begin_run(sess)
        run_records.finalize_run(sess, sess.last_run, AgentResult("cancelled", "停"))
        data = _disk(isolated_memory, sess.current_session_id)
        raw = json.dumps(data, ensure_ascii=False)
        for remembered in ("deploy --token-remembered-once", "deploy-prefix-remembered",
                           "edit-path-remembered.py"):
            assert remembered not in raw, f"确认许可 {remembered!r} 不该落盘"
        assert not any("allowlist" in key for key in data), "会话 JSON 里没有许可字段"
        assert "pid" not in data["progress"]["last_run"]

        again = _reopen(sess.current_session_id)
        assert not again.command_allowlist and not again.command_prefix_allowlist
        assert not again.edit_path_allowlist
        assert again.worktree is None and again.thread is None
        assert again.last_worker is None and again.resume_pending is False
        pre = recovery.precheck(again)
        assert any("不会被自动合并" in n for n in pre.notes), "隔离区使用权不跨进程恢复"


# ══════════════════════════════════════════════════════════════
# 结果卡上的继续入口（纯判定）
# ══════════════════════════════════════════════════════════════

def _snapshot(**over):
    base = {"session_id": "s1", "run_id": "r1", "phase": "ended", "outcome": "cancelled",
            "evidence": {}, "pending_verification": {},
            "plan": {"done": 1, "total": 3, "next": "修复并验证"},
            "model": {"id": "m", "name": "某模型", "type": "custom"}, "agent_mode": "act"}
    base.update(over)
    return base


class TestResumableView:
    @pytest.mark.parametrize("outcome", ["cancelled", "failed", "limit_reached", "unverified"])
    def test_unfinished_outcomes_offer_continue(self, outcome):
        from src import result_view
        assert result_view.describe(_snapshot(outcome=outcome))["resumable"] is True

    def test_completed_does_not_offer_continue(self):
        from src import result_view
        assert result_view.describe(_snapshot(outcome="completed"))["resumable"] is False

    def test_interrupted_offers_continue_but_the_live_run_does_not(self):
        from src import result_view
        running = _snapshot(phase="running", outcome=None)
        assert result_view.describe(running)["resumable"] is True
        assert result_view.describe(running, live_run_id="r1")["resumable"] is False

    def test_card_without_identity_offers_nothing(self):
        from src import result_view
        assert result_view.describe(_snapshot(session_id=""))["resumable"] is False
        assert result_view.describe(_snapshot(run_id=""))["resumable"] is False

    def test_lines_label_the_plan_as_model_reported(self):
        from src import result_view
        view = result_view.describe(_snapshot())
        lines = result_view.resume_lines(view)
        assert "上次停止：用户停止" in lines
        assert "计划进度：模型记录为 1 / 3" in lines
        assert "下一步：检查现场后继续「修复并验证」" in lines
        assert "上次使用：某模型 · Act 模式" in lines
        plan_view = result_view.describe(_snapshot(agent_mode="plan"))
        assert any("Plan 模式" in line for line in result_view.resume_lines(plan_view))

    def test_snapshot_carries_model_plan_and_mode(self, isolated_memory, repo):
        sess = _code_session(repo)
        sess.current_plan = [{"text": "复现", "status": "done"},
                             {"text": "修复并验证", "status": "in_progress"}]
        sess.agent_mode = "plan"
        run_records.begin_run(sess)
        report = run_records.finalize_run(sess, sess.last_run, AgentResult("limit_reached", "触顶"))
        snap = report.snapshot
        assert snap["plan"] == {"done": 1, "total": 2, "next": "修复并验证"}
        assert snap["agent_mode"] == "plan" and snap["model"]["id"]
        restored = run_records.snapshot_from_loaded(_reopen(sess.current_session_id))
        assert restored["plan"]["total"] == 2 and restored["model"] == snap["model"]


# ══════════════════════════════════════════════════════════════
# 主线程：归属 / 屏障 / 连点（真实 Qt 事件循环）
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(app, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


@pytest.fixture()
def host(qt_app, monkeypatch, isolated_memory, no_config_issues):
    """借 ChatUI 的真实方法组一个宿主；agent_loop 换成可控的假实现。"""
    from PySide6.QtCore import QObject, Qt
    from src import agent as _agent
    from src.ui.chat_window import ChatUI
    from src.ui.widgets import SignalBridge

    calls = []
    gate = threading.Event()
    gate.set()

    def _fake_loop(ui, *, resume=None):
        calls.append({"resume": resume, "thread": threading.current_thread(),
                      "session": session.current_session()})
        gate.wait(10)
        return AgentResult("completed")

    monkeypatch.setattr(_agent, "agent_loop", _fake_loop)

    class Host(QObject):
        _RESUME_WAIT_MS = ChatUI._RESUME_WAIT_MS
        _RESUME_WAIT_LIMIT = ChatUI._RESUME_WAIT_LIMIT
        # 静态方法要连同描述符一起借，否则挂到别的类上会被绑成实例方法
        _resume_ownership_problem = ChatUI.__dict__["_resume_ownership_problem"]
        _on_result_continue = ChatUI._on_result_continue
        _resume_wait = ChatUI._resume_wait
        _resume_abort = ChatUI._resume_abort
        _start_resume = ChatUI._start_resume
        _settle_resume_card = ChatUI._settle_resume_card
        _resume_cards_by_session = ChatUI._resume_cards_by_session
        _run_agent = ChatUI._run_agent
        _on_finished_sess = ChatUI._on_finished_sess
        _on_remote_submit = ChatUI._on_remote_submit

        def __init__(self):
            super().__init__()
            self.toasts, self.btn_states, self.appended, self.blocked = [], [], [], []
            self.header_syncs, self.sent = 0, []
            self._has_input = False
            self._ai_reply_start = None
            self.bridge = SignalBridge()
            self.bridge.finished.connect(self._on_finished_sess, Qt.QueuedConnection)

        def _show_toast(self, text, duration=1500):
            self.toasts.append(text)

        def _update_btn_state(self, s):
            self.btn_states.append(s)

        def _append_html(self, text, tag):
            self.appended.append(text)

        def _sync_header_from_session(self):
            self.header_syncs += 1

        def _refresh_session_list(self):
            pass

        def _show_resume_blocked(self, sess, view, card, pre):
            self.blocked.append(pre)

        def _do_send(self, text, images=None):
            self.sent.append(text)

        def show_message(self, text, tag):
            pass

    h = Host()
    h.calls, h.gate = calls, gate
    yield h
    gate.set()
    _pump(qt_app, 0.1)


def _stopped_session(root):
    sess = _code_session(root)
    run_records.begin_run(sess)
    report = run_records.finalize_run(sess, sess.last_run, AgentResult("cancelled", "用户停止"))
    session.set_active(sess)
    from src import result_view
    return sess, result_view.describe(report.snapshot)


def _card(view):
    from src.ui.result_card import ResultCard
    return ResultCard(view)


def _wait_calls(app, host, n, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and len(host.calls) < n:
        app.processEvents()
        time.sleep(0.01)


class TestContinueButton:
    def test_double_click_starts_exactly_one_worker(self, qt_app, host, repo, monkeypatch):
        from src import agent as _agent
        sess, view = _stopped_session(repo)
        card = _card(view)
        release = threading.Event()

        def _real_shaped_loop(ui, *, resume=None):
            # 和真的一样：一开跑就有了新的运行记录
            host.calls.append({"resume": resume, "session": session.current_session()})
            run = run_records.begin_run(session.current_session())
            release.wait(10)
            run_records.finalize_run(session.current_session(), run, AgentResult("completed"))
            return AgentResult("completed")

        monkeypatch.setattr(_agent, "agent_loop", _real_shaped_loop)
        host._on_result_continue(view, card=card)
        host._on_result_continue(view, card=card)       # 程序化的第二次（绕过已禁用的按钮）
        card._continue_btn.click()                      # 真按钮此刻已禁用，点不出信号
        _wait_calls(qt_app, host, 1)
        _pump(qt_app, 0.3)
        host._on_result_continue(view, card=card)       # 第一轮还在跑时再点
        _pump(qt_app, 0.3)
        assert len(host.calls) == 1
        assert host.calls[0]["resume"]["expected_run_id"] == view["run_id"]
        assert host.calls[0]["session"] is sess, "worker 绑的是卡片所属的会话"
        release.set()
        _pump(qt_app, 0.8)
        assert len(host.calls) == 1, "排队等着的那次点击要被归属核对拒掉"
        assert any("更新的一轮" in t for t in host.toasts)

    def test_waits_for_the_old_worker_to_really_exit(self, qt_app, host, repo):
        """强制停止后 is_generating 已是 False，但旧线程还活着——必须等它退出。"""
        sess, view = _stopped_session(repo)
        release = threading.Event()
        old = threading.Thread(target=release.wait, args=(10,), daemon=True)
        old.start()
        sess.last_worker = old
        sess.is_generating = False
        host._on_result_continue(view, card=_card(view))
        _pump(qt_app, 0.4)
        assert host.calls == [], "旧 worker 没退出之前不能起新的"
        assert sess.resume_pending is True
        release.set()
        old.join(2)
        _wait_calls(qt_app, host, 1)
        assert len(host.calls) == 1
        assert sess.resume_pending is False

    def test_sending_is_blocked_while_continue_is_pending(self, qt_app, host, repo):
        sess, view = _stopped_session(repo)
        release = threading.Event()
        old = threading.Thread(target=release.wait, args=(10,), daemon=True)
        old.start()
        sess.last_worker = old
        host._on_result_continue(view, card=_card(view))
        host._on_remote_submit("趁机再发一条")
        assert host.sent == [], "等待期间再发一条就是两个 worker 抢同一个会话"
        release.set()
        old.join(2)
        _wait_calls(qt_app, host, 1)

    def test_old_card_is_rejected(self, qt_app, host, repo):
        sess, view = _stopped_session(repo)
        run_records.begin_run(sess)          # 之后又开过一轮
        run_records.finalize_run(sess, sess.last_run, AgentResult("completed"))
        host._on_result_continue(view, card=_card(view))
        _pump(qt_app, 0.2)
        assert host.calls == [] and any("更新的一轮" in t for t in host.toasts)
        assert sess.resume_pending is False

    def test_card_from_another_session_is_rejected(self, qt_app, host, repo, tmp_path):
        sess, view = _stopped_session(repo)
        other = _code_session(_make_repo(tmp_path / "other"))
        session.set_active(other)
        host._on_result_continue(view, card=_card(view))
        _pump(qt_app, 0.2)
        assert host.calls == [] and any("不属于当前会话" in t for t in host.toasts)

    def test_switching_away_while_waiting_cancels(self, qt_app, host, repo, tmp_path):
        sess, view = _stopped_session(repo)
        release = threading.Event()
        old = threading.Thread(target=release.wait, args=(10,), daemon=True)
        old.start()
        sess.last_worker = old
        card = _card(view)
        host._on_result_continue(view, card=card)
        session.set_active(_code_session(_make_repo(tmp_path / "other")))
        release.set()
        old.join(2)
        _pump(qt_app, 0.5)
        assert host.calls == [], "不替一个看不见的会话在后台悄悄开跑"
        assert any("已切换" in t for t in host.toasts)
        assert sess.resume_pending is False
        assert card._continue_btn.isEnabled(), "取消后按钮要能再点"

    def test_blocked_precheck_frees_the_button_and_explains(self, qt_app, host, repo):
        sess, view = _stopped_session(repo)
        sess.last_run["model"] = {"id": "gone-model", "name": "旧模型", "type": "custom"}
        card = _card(view)
        host._on_result_continue(view, card=card)
        _pump(qt_app, 0.2)
        assert host.calls == []
        assert host.blocked and host.blocked[0].model_missing
        assert card._continue_btn.isEnabled() and sess.resume_pending is False

    def test_recorded_model_is_restored_before_the_worker_starts(self, qt_app, host, repo):
        sess, view = _stopped_session(repo)
        first, second = _api_models()[:2]
        recorded = sess.last_run["model"]["id"]
        from src.models import MODEL_LIST
        assert MODEL_LIST[first][2] == recorded
        sess.current_model_index = second
        host._on_result_continue(view, card=_card(view))
        _wait_calls(qt_app, host, 1)
        assert sess.current_model_index == first
        assert host.header_syncs >= 1, "顶栏要显示实际使用的模型"
        assert any("继续任务" in a and MODEL_LIST[first][0] in a for a in host.appended)

    def test_two_sessions_each_settle_their_own_card(self, qt_app, host, repo, tmp_path,
                                                     monkeypatch):
        """A 在后台继续着、用户又在 B 点了继续：两张卡各自收尾，互不覆盖。"""
        from src import agent as _agent
        first, view_a = _stopped_session(repo)
        card_a = _card(view_a)
        release = threading.Event()

        def _loop(ui, *, resume=None):
            current = session.current_session()
            host.calls.append({"session": current})
            run = run_records.begin_run(current)
            release.wait(10)
            run_records.finalize_run(current, run, AgentResult("completed"))
            return AgentResult("completed")

        monkeypatch.setattr(_agent, "agent_loop", _loop)
        host._on_result_continue(view_a, card=card_a)
        _wait_calls(qt_app, host, 1)
        second, view_b = _stopped_session(_make_repo(tmp_path / "second"))   # 切到 B
        card_b = _card(view_b)
        host._on_result_continue(view_b, card=card_b)
        _wait_calls(qt_app, host, 2)
        assert [c["session"] for c in host.calls] == [first, second]
        release.set()
        _pump(qt_app, 1.0)
        assert card_a._continue_btn.text() == "已继续", "后台会话结束也要收尾它自己的卡"
        assert card_b._continue_btn.text() == "已继续"

    def test_card_settles_after_the_run(self, qt_app, host, repo, monkeypatch):
        from src import agent as _agent
        sess, view = _stopped_session(repo)
        card = _card(view)

        def _new_run_then_finish(ui, *, resume=None):
            host.calls.append({"resume": resume})
            run_records.begin_run(sess)
            run_records.finalize_run(sess, sess.last_run, AgentResult("completed"))
            return AgentResult("completed")

        monkeypatch.setattr(_agent, "agent_loop", _new_run_then_finish)
        host._on_result_continue(view, card=card)
        assert card._continue_btn.text() == "正在继续…" and not card._continue_btn.isEnabled()
        _wait_calls(qt_app, host, 1)
        _pump(qt_app, 0.5)
        assert card._continue_btn.text() == "已继续" and not card._continue_btn.isEnabled()


def _unrelated_repo(root):
    """内容、作者都和 _make_repo 不同的仓库：两个一模一样的 init 提交哈希会撞上。"""
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "someone-else@example.com")
    _git(root, "config", "user.name", "Someone Else")
    (root / "theirs.txt").write_text("not your project\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "their history")
    return root


class TestRelocateProject:
    """项目被挪走了：重新指到新位置时，判据是仓库身份，不是目录名。"""

    @staticmethod
    def _relocate(sess, picked, confirm):
        from src.ui.chat_window import ChatUI
        return ChatUI._relocate_session_project(None, sess, picked, confirm=confirm)

    def _moved_session(self, repo, tmp_path):
        sess, _view = _stopped_session(repo)
        moved = tmp_path / "moved-here"
        repo.rename(moved)
        assert recovery.precheck(sess).project_missing
        return sess, moved

    def test_same_repository_is_accepted_and_persisted(self, isolated_memory, repo, tmp_path,
                                                       no_config_issues):
        from src import projects
        sess, moved = self._moved_session(repo, tmp_path)
        ok, _note = self._relocate(sess, str(moved),
                                   confirm=lambda *a: pytest.fail("身份明确时不该再问用户"))
        expected = os.path.normpath(str(moved)).replace("\\", "/")
        assert ok and sess.project == expected
        assert _disk(isolated_memory, sess.current_session_id)["project"] == expected
        assert expected in [p["path"] for p in projects.list_projects()]
        assert recovery.precheck(sess).blocking == []
        check = recovery.inspect_site(sess)
        assert check.blocking == []
        assert any("上次记录的目录是" in r for r in check.site["incomplete"]), \
            "换了位置要如实说明，并据此要求补验证"

    def test_another_repository_is_refused(self, isolated_memory, repo, tmp_path,
                                           no_config_issues):
        sess, _moved = self._moved_session(repo, tmp_path)
        other = _unrelated_repo(tmp_path / "someone-elses")
        before = sess.project
        ok, message = self._relocate(sess, str(other),
                                     confirm=lambda *a: pytest.fail("确定不是同一个仓库就不该问"))
        assert ok is False and "另一个仓库" in message
        assert sess.project == before

    def test_unknown_identity_asks_and_respects_the_answer(self, isolated_memory, repo, tmp_path,
                                                           no_config_issues):
        sess, moved = self._moved_session(repo, tmp_path)
        sess.last_run["workspace"] = None          # 旧会话：没有记录仓库身份
        asked = []
        ok, _ = self._relocate(sess, str(moved), confirm=lambda why, p: asked.append(why) or False)
        assert ok is False and asked and sess.project == str(repo)
        ok, _ = self._relocate(sess, str(moved), confirm=lambda why, p: True)
        assert ok is True


# ══════════════════════════════════════════════════════════════
# 重绘：程序写的不是"你"说的
# ══════════════════════════════════════════════════════════════

class TestSendCopyPlaceholder:
    def test_placeholder_does_not_pretend_a_result(self):
        """发送副本的兜底占位：只知道没拿到结果，不能写成"无结果"让模型以为执行过了。"""
        from src import streaming
        out = streaming._sanitize_tool_pairs([
            HumanMessage(content="q"),
            AIMessage(content="", tool_calls=[{"name": "append_file", "args": {}, "id": "t1"}]),
        ])
        text = out[-1].content
        assert text.startswith("应用恢复提示：未取得执行结果")
        assert "不代表执行成功或失败" in text and "不要直接重复" in text
        assert "无结果" not in text


class TestRedraw:
    def test_internal_prompts_are_not_drawn_as_the_user(self, qt_app):
        from src.ui.chat_window import ChatUI
        gate = HumanMessage(content="[内部验证要求]\n你刚才试图结束任务",
                            additional_kwargs={"lingxi_internal": True})
        resume = HumanMessage(content="[继续任务 · 程序生成的恢复说明…]",
                              additional_kwargs={"lingxi_internal": True,
                                                 "lingxi_kind": "resume",
                                                 "lingxi_recovery": {"note": "🔄 继续任务"}})
        real = HumanMessage(content="修一下登录")
        hidden = ChatUI._is_hidden_bridge_message
        assert hidden(None, gate) is True, "闸门提示只给模型看"
        assert hidden(None, resume) is False and hidden(None, real) is False
        assert ChatUI._recovery_note_text(resume) == "🔄 继续任务"
        assert ChatUI._recovery_note_text(real) is None
        assert ChatUI._recovery_note_text(gate) is None


# ══════════════════════════════════════════════════════════════
# 真实 Qt 渲染
# ══════════════════════════════════════════════════════════════

class TestCardRendering:
    def test_continue_button_and_lines_render_and_fit(self, qt_app, isolated_memory, repo,
                                                      tmp_path):
        from PySide6.QtWidgets import QLabel, QPushButton
        from src.ui.message_view import MessageView
        from src.ui.result_card import ResultCard
        from src import result_view
        sess = _code_session(repo)
        sess.current_plan = [{"text": "复现问题", "status": "done"},
                             {"text": "修复并验证" + "，并且" * 30, "status": "in_progress"}]
        run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "app.py")
        report = run_records.finalize_run(sess, sess.last_run,
                                          AgentResult("limit_reached", "已达单次交互轮次上限"))
        view = result_view.describe(report.snapshot)

        for width in (420, 760):
            mv = MessageView()
            mv.resize(width, 900)
            card = mv.add_result_card(ResultCard(view))
            mv.show()
            for _ in range(3):
                qt_app.processEvents()
            buttons = {b.text(): b for b in card.findChildren(QPushButton)}
            assert "继续任务" in buttons
            btn = buttons["继续任务"]
            bottom = btn.mapTo(card, btn.rect().bottomLeft()).y()
            assert btn.isVisible() and bottom <= card.height(), "按钮不能被卡片边框切掉"
            text = "\n".join(w.text() for w in card.findChildren(QLabel))
            assert "计划进度：模型记录为 1 / 2" in text
            assert "上次停止：达到轮数上限" in text
            widest = max(lab.minimumSizeHint().width() for lab in card.findChildren(QLabel))
            assert widest <= 520
            card.grab().save(str(tmp_path / f"resume_card_{width}.png"))
            mv.close()

    def test_completed_card_has_no_continue_button(self, qt_app, isolated_memory, repo):
        from PySide6.QtWidgets import QPushButton
        from src.ui.result_card import ResultCard
        from src import result_view
        sess = _code_session(repo)
        run_records.begin_run(sess)
        report = run_records.finalize_run(sess, sess.last_run, AgentResult("completed"))
        card = ResultCard(result_view.describe(report.snapshot))
        assert "继续任务" not in [b.text() for b in card.findChildren(QPushButton)]
        card.close()


# ══════════════════════════════════════════════════════════════
# 真实进程：崩在 append 中途 → 新进程重开 → 继续
# ══════════════════════════════════════════════════════════════

_CRASH_MID_APPEND = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from src import paths
paths.set_data_dir(sys.argv[2])
from src import agent, memory, session, streaming
from langchain_core.messages import HumanMessage

sid, root = sys.argv[3], sys.argv[4]

# 硬性阻断一切真实模型 / Claude CLI：新建的 Session 默认模型下标可能落在 Claude Code 上，
# 不显式选模型、不阻断的话，这个子进程会真的去调本地 claude。
from src import models
from src.models import MODEL_LIST

def _forbidden(*a, **k):
    raise AssertionError("测试子进程不许调用真实模型或 Claude CLI")

models._create_llm = _forbidden
agent._claude_code_loop = _forbidden
agent._post_run_notify = lambda *a, **k: None
agent.maybe_generate_session_title = lambda *a, **k: None

sess = session.Session()
assert memory.load_session(sid, session=sess) is True
sess.current_model_index = next(i for i, m in enumerate(MODEL_LIST)
                                if m[1] not in ("claude-code", "ollama"))
session.register(sess)
session.bind_thread(sess)
session.set_active(sess)
sess.chat_history.append(HumanMessage(content="把 notes.txt 补一行"))
memory.save_session(session=sess)

class _Write:
    def invoke(self, args):
        from src import verification
        with open(os.path.join(root, "scratch.txt"), "w", encoding="utf-8") as f:
            f.write("scratch\n")
        verification.mark_dirty(session.get_verification(), "scratch.txt")
        return "已写入 scratch.txt"

class _AppendThenDie:
    def invoke(self, args):
        with open(os.path.join(root, "notes.txt"), "a", encoding="utf-8") as f:
            f.write("appended once\n")
            f.flush()
            os.fsync(f.fileno())
        os._exit(31)            # 副作用已发生、结果还没交回去

streaming.get_tool_map = lambda: {"write_file": _Write(), "append_file": _AppendThenDie()}
# 先有一个正常提交的写操作：它的提交把这条 AIMessage 连同结果存上盘。
# 只读工具不存盘——只有它的话，崩溃后磁盘上根本没有这条 AIMessage，只剩 sidecar。
calls = [{"name": "write_file", "args": {"path": "scratch.txt", "content": "scratch\n"},
          "id": "w1"},
         {"name": "append_file", "args": {"path": "notes.txt", "content": "appended once\n"},
          "id": "a1"}]
agent._stream_with_tools = lambda ui: ("", calls, {"input": 1, "output": 1, "total": 2}, None)

class _UI:
    def __getattr__(self, name):
        return lambda *a, **k: None

agent.agent_loop(_UI())
'''


class TestRealProcessCrash:
    def test_crash_mid_append_then_continue_in_a_new_process(self, isolated_memory, repo,
                                                             tmp_path, monkeypatch):
        from src import agent as _agent
        from src import streaming
        sess = _code_session(repo)
        sid = sess.current_session_id
        script = tmp_path / "crash_mid_append.py"
        script.write_text(_CRASH_MID_APPEND, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, str(script),
             os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
             str(isolated_memory.parent), sid, str(repo)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, env=env)
        assert proc.returncode == 31, f"子进程该以 os._exit(31) 死掉: {proc.stderr[-2000:]}"
        assert (repo / "notes.txt").read_text(encoding="utf-8") == "appended once\n"

        disk = _disk(isolated_memory, sid)
        crashed_run = disk["progress"]["last_run"]
        assert crashed_run["phase"] == "running"
        assert [op["tool"] for op in _sidecar(isolated_memory, sid)["operations"]] \
            == ["append_file"]

        # ── 新进程（本测试进程）重开：结果卡显示中断、可继续 ──
        from src import result_view
        again = _reopen(sid)
        view = result_view.describe(run_records.snapshot_from_loaded(again))
        assert view["title"] == "上次运行被中断" and view["resumable"] is True

        spy = _Spy()
        monkeypatch.setattr(streaming, "get_tool_map", lambda: {"append_file": spy,
                                                                "write_file": spy})
        monkeypatch.setattr(_agent, "maybe_generate_session_title", lambda *a, **k: None)
        stream = _Stream()
        monkeypatch.setattr(_agent, "_stream_with_tools", stream)
        session.bind_thread(again)
        session.set_active(again)
        try:
            result = _agent.agent_loop(_UI(), resume={"expected_run_id": crashed_run["id"],
                                                      "notes": []})
        finally:
            session.unbind_thread()

        assert spy.calls == [], "没有自动重放"
        assert (repo / "notes.txt").read_text(encoding="utf-8") == "appended once\n"
        assert result.status == "unverified"
        a1 = [m for m in stream.sent[0] if isinstance(m, ToolMessage) and m.tool_call_id == "a1"]
        assert a1 and "可能已经执行" in a1[0].content
        assert _sidecar(isolated_memory, sid) is None
        final = _disk(isolated_memory, sid)
        placeholders = [m for m in final["messages"]
                        if m.get("lingxi_kind") == "resume" and m["type"] == "ToolMessage"]
        assert [m["tool_call_id"] for m in placeholders] == ["a1"], "悬空调用的占位已落盘"
        notes = [m for m in final["messages"]
                 if m.get("lingxi_kind") == "resume" and m["type"] == "HumanMessage"]
        assert len(notes) == 1
        assert notes[0]["lingxi_recovery"]["operations"][0]["tool"] == "append_file"
        assert notes[0]["lingxi_recovery"]["from_run_id"] == crashed_run["id"]
        assert final["progress"]["last_run"]["source"]["kind"] == "continue"
        assert final["progress"]["last_run"]["phase"] == "ended"
