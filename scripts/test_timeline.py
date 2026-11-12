"""对话时间轴后端核心测试：truncate_history / rewind_session / fork_session。

契约：只动对话与运行态，绝不撤销磁盘文件改动（file_history 记录原样保留）。
"""
import json
import os
import sys

import pytest
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_session():
    from src import session as sm
    s = sm.Session()
    s.chat_history = [
        SystemMessage(content="sys"),                       # 0
        HumanMessage(content="q1"),                          # 1
        AIMessage(content="", tool_calls=[                   # 2
            {"name": "t", "args": {}, "id": "call_1", "type": "tool_call"}]),
        ToolMessage(content="r1", tool_call_id="call_1"),    # 3
        AIMessage(content="a1"),                             # 4
        HumanMessage(content="q2"),                          # 5
        AIMessage(content="a2"),                             # 6
    ]
    return s


# ── truncate_history ─────────────────────────────────────
class TestTruncate:
    def test_keeps_prefix_returns_dropped(self):
        from src.memory import truncate_history
        s = _make_session()
        dropped = truncate_history(s, 5)
        assert dropped == 2
        assert len(s.chat_history) == 5
        assert s.chat_history[1].content == "q1"
        assert s.chat_history[4].content == "a1"

    def test_resets_compaction(self):
        from src.memory import truncate_history
        s = _make_session()
        s.compaction = {"summary": "old summary", "covered_upto": 6}
        truncate_history(s, 3)
        assert s.compaction == {"summary": "", "covered_upto": 0}

    def test_filters_evicted_to_alive(self):
        from src.memory import truncate_history
        s = _make_session()
        s.evicted_tool_results = ["call_1", "call_gone"]
        truncate_history(s, 5)                      # call_1 仍在
        assert s.evicted_tool_results == ["call_1"]

    def test_filters_evicted_drops_deleted(self):
        from src.memory import truncate_history
        s = _make_session()
        s.evicted_tool_results = ["call_1"]
        truncate_history(s, 2)                      # call_1 被删
        assert s.evicted_tool_results == []

    def test_clears_run_state(self):
        from src.memory import truncate_history
        s = _make_session()
        s.last_run = {"id": "run-x"}
        s.last_result = {"text": "done"}
        s.active_run_id = "run-x"
        s.current_task = {"id": "task-1"}
        s.current_plan = [{"step": 1}]
        s.recent_operations = [{"operation_id": "op"}]
        truncate_history(s, 5)
        assert s.last_run is None
        assert s.last_result is None
        assert s.active_run_id is None
        assert s.current_task is None
        assert s.current_plan == []
        assert s.recent_operations == []

    def test_noop_at_full_length(self):
        from src.memory import truncate_history
        s = _make_session()
        s.last_run = {"id": "keep"}
        assert truncate_history(s, 7) == 0
        assert len(s.chat_history) == 7
        assert s.last_run == {"id": "keep"}         # 没动就不重置

    def test_to_system_only(self):
        from src.memory import truncate_history
        s = _make_session()
        assert truncate_history(s, 1) == 6
        assert len(s.chat_history) == 1
        assert isinstance(s.chat_history[0], SystemMessage)

    @pytest.mark.parametrize("bad", [0, -1, 8, "x", 1.5])
    def test_invalid_cut_raises(self, bad):
        from src.memory import truncate_history
        s = _make_session()
        with pytest.raises(ValueError):
            truncate_history(s, bad)

    def test_does_not_touch_file_history(self, isolated_memory):
        from src.memory import truncate_history
        from src import file_history
        rec_dir = file_history.records_dir()
        os.makedirs(rec_dir, exist_ok=True)
        rec = os.path.join(rec_dir, "checkpoint-abc.json")
        with open(rec, "w", encoding="utf-8") as f:
            json.dump({"checkpoint_id": "checkpoint-abc", "path": "x.py"}, f)
        s = _make_session()
        truncate_history(s, 2)
        assert os.path.exists(rec)                  # 撤销材料原样保留
        with open(rec, encoding="utf-8") as f:
            assert json.load(f)["checkpoint_id"] == "checkpoint-abc"


# ── rewind_session（落盘）─────────────────────────────────
class TestRewind:
    def test_persists_prefix(self, isolated_memory, project_dir):
        from src import memory
        s = _make_session()
        memory.save_session(session=s)
        sid = s.current_session_id
        assert sid
        dropped = memory.rewind_session(s, 5)
        assert dropped == 2
        body = isolated_memory / f"{sid}.json"
        data = json.loads(body.read_text(encoding="utf-8"))
        assert len(data["messages"]) == 5
        index = json.loads((isolated_memory / "index.json").read_text(encoding="utf-8"))
        assert any(i["id"] == sid for i in index)   # 会话仍在侧栏

    def test_reload_restores_prefix(self, isolated_memory, project_dir):
        from src import memory, session as sm
        s = _make_session()
        memory.save_session(session=s)
        sid = s.current_session_id
        memory.rewind_session(s, 3)
        fresh = sm.Session()
        memory.load_session(sid, session=fresh)
        assert len(fresh.chat_history) == 3
        assert fresh.chat_history[1].content == "q1"

    def test_to_empty_deletes_body_but_keeps_id(self, isolated_memory, project_dir):
        from src import memory
        s = _make_session()
        memory.save_session(session=s)
        sid = s.current_session_id
        dropped = memory.rewind_session(s, 1)
        assert dropped == 6
        assert not (isolated_memory / f"{sid}.json").exists()
        index = json.loads((isolated_memory / "index.json").read_text(encoding="utf-8"))
        assert not any(i["id"] == sid for i in index)
        # 正文退出侧栏，但会话 id 保留：顶栏撤销靠它定位恢复材料（见 test_tools_fs）
        assert s.current_session_id == sid
        assert len(s.chat_history) == 1
        # 下次发消息复用同一 id 重新落盘、重进侧栏，撤销链才接得上
        s.chat_history.append(HumanMessage(content="new"))
        memory.save_session(session=s)
        assert s.current_session_id == sid
        assert (isolated_memory / f"{sid}.json").exists()

    def test_noop_does_not_save(self, isolated_memory, project_dir):
        from src import memory
        s = _make_session()
        memory.save_session(session=s)
        sid = s.current_session_id
        body = isolated_memory / f"{sid}.json"
        before = body.read_text(encoding="utf-8")
        assert memory.rewind_session(s, 7) == 0
        assert body.read_text(encoding="utf-8") == before


# ── fork_session ─────────────────────────────────────────
class TestFork:
    def test_copies_prefix_fresh_state(self, isolated_memory):
        from src import memory, session as sm
        s = _make_session()
        s.current_model_index = 0
        s.agent_mode = "plan"
        s.last_run = {"id": "run-x"}
        s.current_task = {"id": "task-1"}
        new = memory.fork_session(s, 5)
        assert len(new.chat_history) == 5
        assert new.chat_history[1].content == "q1"
        assert new.current_session_id is None       # 新 id 待首次存盘铸造
        assert new.last_run is None                 # 运行态从零
        assert new.current_task is None
        assert new.agent_mode == "plan"             # 继承设置
        assert sm.get(new.key) is new               # 已注册
        assert len(s.chat_history) == 7             # 源会话不受影响
        assert s.last_run == {"id": "run-x"}

    def test_fork_is_independent_copy(self, isolated_memory):
        from src import memory
        s = _make_session()
        new = memory.fork_session(s, 5)
        new.chat_history.append(HumanMessage(content="branch"))
        assert len(s.chat_history) == 7             # 改分叉不影响源

    def test_new_id_on_save(self, isolated_memory, project_dir):
        from src import memory
        s = _make_session()
        memory.save_session(session=s)
        sid = s.current_session_id
        new = memory.fork_session(s, 5)
        memory.save_session(session=new)
        assert new.current_session_id
        assert new.current_session_id != sid
        assert (isolated_memory / f"{sid}.json").exists()
        assert (isolated_memory / f"{new.current_session_id}.json").exists()

    @pytest.mark.parametrize("bad", [0, 8, "x"])
    def test_invalid_cut_raises(self, isolated_memory, bad):
        from src import memory
        s = _make_session()
        with pytest.raises(ValueError):
            memory.fork_session(s, bad)