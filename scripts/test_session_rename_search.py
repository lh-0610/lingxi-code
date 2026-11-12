"""会话重命名 / 跨会话搜索后端核心测试：rename_session / search_sessions。

契约：
  - rename 三处一起改（内存锚点 + index + 正文），不刷新 updated（不打乱历史顺序），
    改名后再 save 不被旧内存标题覆盖回去。
  - search 只搜真实用户输入 + 助手可见正文，跳过系统/内部/思考/工具结果；大小写不敏感；
    单个会话正文读不出不中断整次搜索。
"""
import json
import os
import sys

import pytest
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _save(title, messages, *, kind="code", project=None):
    """造一个会话并存盘，返回 Session（已注册、有 id）。messages 不含 SystemMessage。

    project 通过临时改 state.current_project 生效：首次落盘锚定的是当时的全局项目，
    直接预设 s.project 会被 save 覆盖。rag 会话的 project 恒 None（save 内部强制）。
    """
    from src import memory, session as sm, state
    s = sm.Session()
    s.session_kind = kind
    s.current_session_title = title
    s.chat_history = [SystemMessage(content="sys")] + messages
    if project is not None:
        old = state.current_project
        state.current_project = project
        try:
            memory.save_session(session=s)
        finally:
            state.current_project = old
    else:
        memory.save_session(session=s)
    return s


def _index(isolated_memory):
    return json.loads((isolated_memory / "index.json").read_text(encoding="utf-8"))


def _body(isolated_memory, sid):
    return json.loads((isolated_memory / f"{sid}.json").read_text(encoding="utf-8"))


# ── rename_session ───────────────────────────────────────
class TestRename:
    def test_updates_index_and_body(self, isolated_memory, project_dir):
        from src import memory
        s = _save("旧标题", [HumanMessage(content="你好"), AIMessage(content="在")])
        assert memory.rename_session(s.current_session_id, "新标题") is True
        item = next(i for i in _index(isolated_memory) if i["id"] == s.current_session_id)
        assert item["title"] == "新标题"
        assert _body(isolated_memory, s.current_session_id)["title"] == "新标题"

    def test_preserves_updated_no_reorder(self, isolated_memory, project_dir):
        from src import memory
        s = _save("旧", [HumanMessage(content="hi"), AIMessage(content="yo")])
        sid = s.current_session_id
        before_idx = next(i for i in _index(isolated_memory) if i["id"] == sid)["updated"]
        before_body = _body(isolated_memory, sid)["updated"]
        memory.rename_session(sid, "改名")
        after_idx = next(i for i in _index(isolated_memory) if i["id"] == sid)["updated"]
        after_body = _body(isolated_memory, sid)["updated"]
        assert after_idx == before_idx          # 不刷新 updated → 不顶到分组最前
        assert after_body == before_body

    def test_syncs_live_session_title(self, isolated_memory, project_dir):
        from src import memory
        s = _save("旧", [HumanMessage(content="hi"), AIMessage(content="yo")])
        memory.rename_session(s.current_session_id, "内存也改")
        assert s.current_session_title == "内存也改"

    def test_rename_then_save_no_regression(self, isolated_memory, project_dir):
        """关键回归：改名后再 save_session，盘上标题不被旧内存标题覆盖回去。"""
        from src import memory
        s = _save("旧", [HumanMessage(content="hi"), AIMessage(content="yo")])
        sid = s.current_session_id
        memory.rename_session(sid, "坚持住")
        s.chat_history.append(HumanMessage(content="再聊一句"))
        memory.save_session(session=s)
        assert _body(isolated_memory, sid)["title"] == "坚持住"
        item = next(i for i in _index(isolated_memory) if i["id"] == sid)
        assert item["title"] == "坚持住"

    def test_only_target_session_changes(self, isolated_memory, project_dir):
        from src import memory
        a = _save("A", [HumanMessage(content="a"), AIMessage(content="a")])
        b = _save("B", [HumanMessage(content="b"), AIMessage(content="b")])
        memory.rename_session(a.current_session_id, "A2")
        assert a.current_session_title == "A2"
        assert b.current_session_title == "B"     # 别的会话不受影响
        assert _body(isolated_memory, b.current_session_id)["title"] == "B"

    def test_reload_restores_new_title(self, isolated_memory, project_dir):
        from src import memory, session as sm
        s = _save("旧", [HumanMessage(content="hi"), AIMessage(content="yo")])
        sid = s.current_session_id
        memory.rename_session(sid, "重启后还在")
        fresh = sm.Session()
        memory.load_session(sid, session=fresh)
        assert fresh.current_session_title == "重启后还在"

    @pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
    def test_empty_title_raises(self, isolated_memory, project_dir, bad):
        from src import memory
        s = _save("旧", [HumanMessage(content="hi"), AIMessage(content="yo")])
        with pytest.raises(ValueError):
            memory.rename_session(s.current_session_id, bad)

    def test_missing_id_returns_false(self, isolated_memory, project_dir):
        from src import memory
        assert memory.rename_session("不存在的id", "x") is False

    def test_sanitizes_whitespace_and_caps(self, isolated_memory, project_dir):
        from src import memory
        s = _save("旧", [HumanMessage(content="hi"), AIMessage(content="yo")])
        long = "字" * 200
        memory.rename_session(s.current_session_id, "  多\n行\t标题  " + long)
        title = _body(isolated_memory, s.current_session_id)["title"]
        assert "\n" not in title and "\t" not in title
        assert title.startswith("多 行 标题")
        assert len(title) == 100

    def test_keeps_user_punctuation(self, isolated_memory, project_dir):
        from src import memory
        s = _save("旧", [HumanMessage(content="hi"), AIMessage(content="yo")])
        memory.rename_session(s.current_session_id, "「重构」：登录流程")
        assert _body(isolated_memory, s.current_session_id)["title"] == "「重构」：登录流程"


# ── search_sessions ──────────────────────────────────────
class TestSearch:
    def test_empty_query_returns_empty(self, isolated_memory, project_dir):
        from src import memory
        _save("标题", [HumanMessage(content="内容"), AIMessage(content="回复")])
        assert memory.search_sessions("") == []
        assert memory.search_sessions("   ") == []

    def test_matches_title_only(self, isolated_memory, project_dir):
        from src import memory
        s = _save("登录流程重构", [HumanMessage(content="你好"), AIMessage(content="在")])
        hits = memory.search_sessions("登录")
        assert len(hits) == 1
        assert hits[0]["id"] == s.current_session_id
        assert hits[0]["match_in_title"] is True
        assert hits[0]["snippet"] == ""           # 仅标题命中，无正文预览
        assert hits[0]["match_count"] == 0

    def test_matches_user_body(self, isolated_memory, project_dir):
        from src import memory
        s = _save("无关标题", [HumanMessage(content="帮我修复支付超时"), AIMessage(content="好的")])
        hits = memory.search_sessions("支付")
        assert len(hits) == 1
        assert hits[0]["id"] == s.current_session_id
        assert hits[0]["match_in_title"] is False
        assert "支付" in hits[0]["snippet"]
        assert hits[0]["match_count"] == 1

    def test_matches_ai_body(self, isolated_memory, project_dir):
        from src import memory
        _save("标题", [HumanMessage(content="问题"), AIMessage(content="这是关于缓存的回答")])
        hits = memory.search_sessions("缓存")
        assert len(hits) == 1
        assert hits[0]["match_count"] == 1

    def test_case_insensitive(self, isolated_memory, project_dir):
        from src import memory
        _save("Title", [HumanMessage(content="Fix the WebSocket bug"), AIMessage(content="ok")])
        assert len(memory.search_sessions("websocket")) == 1
        assert len(memory.search_sessions("WEBSOCKET")) == 1

    def test_skips_system_message(self, isolated_memory, project_dir):
        from src import memory
        s = _save("标题", [HumanMessage(content="你好"), AIMessage(content="在")])
        # 把关键词只放进 SystemMessage
        s.chat_history[0] = SystemMessage(content="SECRETWORD 系统提示词")
        from src import memory as m
        m.save_session(session=s)
        assert memory.search_sessions("SECRETWORD") == []

    def test_skips_internal_messages(self, isolated_memory, project_dir):
        from src import memory
        internal = HumanMessage(content="继续任务 SECRETWORD",
                                additional_kwargs={"lingxi_internal": True, "lingxi_kind": "resume"})
        _save("标题", [HumanMessage(content="真实问题"), internal, AIMessage(content="好")])
        assert memory.search_sessions("SECRETWORD") == []

    def test_skips_tool_results(self, isolated_memory, project_dir):
        from src import memory
        call = AIMessage(content="", tool_calls=[
            {"name": "read_file", "args": {}, "id": "c1", "type": "tool_call"}])
        tool = ToolMessage(content="SECRETWORD 出现在工具结果里", tool_call_id="c1")
        _save("标题", [HumanMessage(content="读文件"), call, tool, AIMessage(content="读完了")])
        assert memory.search_sessions("SECRETWORD") == []

    def test_skips_thinking_blocks(self, isolated_memory, project_dir):
        from src import memory
        ai = AIMessage(content=[
            {"type": "thinking", "thinking": "SECRETWORD 只在思考里"},
            {"type": "text", "text": "可见的回答"},
        ])
        _save("标题", [HumanMessage(content="问题"), ai])
        assert memory.search_sessions("SECRETWORD") == []
        assert len(memory.search_sessions("可见")) == 1   # 正文块仍可搜

    def test_match_count_multiple_messages(self, isolated_memory, project_dir):
        from src import memory
        _save("标题", [
            HumanMessage(content="缓存问题"),
            AIMessage(content="缓存方案"),
            HumanMessage(content="缓存还有问题"),
        ])
        hits = memory.search_sessions("缓存")
        assert hits[0]["match_count"] == 3

    def test_snippet_has_context_and_ellipsis(self, isolated_memory, project_dir):
        from src import memory
        long = "前面" * 40 + "关键命中词" + "后面" * 40
        _save("标题", [HumanMessage(content=long), AIMessage(content="ok")])
        snippet = memory.search_sessions("关键命中词")[0]["snippet"]
        assert "关键命中词" in snippet
        assert snippet.startswith("…") and snippet.endswith("…")
        assert len(snippet) < len(long)

    def test_no_match_returns_empty(self, isolated_memory, project_dir):
        from src import memory
        _save("标题", [HumanMessage(content="你好"), AIMessage(content="在")])
        assert memory.search_sessions("不存在的词") == []

    def test_results_follow_index_order(self, isolated_memory, project_dir):
        from src import memory
        first = _save("甲", [HumanMessage(content="共同词"), AIMessage(content="a")])
        second = _save("乙", [HumanMessage(content="共同词"), AIMessage(content="b")])
        hits = memory.search_sessions("共同词")
        ids = [h["id"] for h in hits]
        index_ids = [i["id"] for i in _index(isolated_memory)]
        # 结果顺序与索引一致（更新时间倒序）：后存的 second 在前
        assert ids == [i for i in index_ids if i in ids]
        assert ids[0] == second.current_session_id

    def test_kind_filter(self, isolated_memory, project_dir):
        from src import memory
        code = _save("代码会话", [HumanMessage(content="共同词"), AIMessage(content="a")], kind="code")
        rag = _save("知识会话", [HumanMessage(content="共同词"), AIMessage(content="b")], kind="rag")
        code_hits = memory.search_sessions("共同词", kind="code")
        rag_hits = memory.search_sessions("共同词", kind="rag")
        assert [h["id"] for h in code_hits] == [code.current_session_id]
        assert [h["id"] for h in rag_hits] == [rag.current_session_id]

    def test_project_filter(self, isolated_memory, project_dir):
        from src import memory
        here = _save("本项目", [HumanMessage(content="共同词"), AIMessage(content="a")],
                     project=str(project_dir))
        other = _save("别的项目", [HumanMessage(content="共同词"), AIMessage(content="b")],
                      project="/some/other/project")
        hits = memory.search_sessions("共同词", project=str(project_dir))
        assert [h["id"] for h in hits] == [here.current_session_id]
        all_hits = memory.search_sessions("共同词", project="__all__")
        assert {h["id"] for h in all_hits} == {here.current_session_id, other.current_session_id}

    def test_corrupt_body_skipped_title_still_matches(self, isolated_memory, project_dir):
        from src import memory
        s = _save("可搜标题", [HumanMessage(content="正文"), AIMessage(content="回复")])
        sid = s.current_session_id
        # 正文写坏（非法 JSON），标题仍在索引里
        (isolated_memory / f"{sid}.json").write_text("{ 坏掉的 json", encoding="utf-8")
        hits = memory.search_sessions("可搜标题")
        assert len(hits) == 1
        assert hits[0]["id"] == sid
        assert hits[0]["match_in_title"] is True
        assert hits[0]["snippet"] == ""           # 正文读不出 → 无预览，但不崩