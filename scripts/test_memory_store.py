"""长期记忆存储 单元测试

覆盖：add / list / delete / search / render / 去重 / 损坏恢复 / 空文本
全部操作在 isolated_memory fixture 提供的临时目录中进行，不污染真实数据。
"""
import os
import sys
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import memory_store as ms


# ── add_memory ──────────────────────────────────────────
class TestAddMemory:
    def test_basic_add(self, isolated_memory):
        result = ms.add_memory("用户喜欢 Python", scope="global")
        assert result is not None
        assert result["text"] == "用户喜欢 Python"
        assert result["scope"] == "global"
        assert "id" in result
        assert "created" in result

    def test_add_dedup(self, isolated_memory):
        ms.add_memory("用户喜欢 Python")
        result = ms.add_memory("用户喜欢 Python")
        assert result is None  # 重复，跳过

    def test_add_dedup_case_insensitive(self, isolated_memory):
        ms.add_memory("User Likes Python")
        result = ms.add_memory("user likes python")
        assert result is None  # 大小写不同也应去重

    def test_add_empty_text(self, isolated_memory):
        result = ms.add_memory("")
        assert result is None

    def test_add_whitespace_only(self, isolated_memory):
        result = ms.add_memory("   ")
        assert result is None

    def test_add_truncates_long_text(self, isolated_memory):
        from src.limits import MEMORY_FACT_MAX_LENGTH

        long_text = "x" * (MEMORY_FACT_MAX_LENGTH + 100)
        result = ms.add_memory(long_text)
        assert result is not None
        assert len(result["text"]) <= MEMORY_FACT_MAX_LENGTH + 3  # +3 for "..."

    def test_add_persists_to_file(self, isolated_memory):
        ms.add_memory("持久化测试")
        with open(os.path.join(str(isolated_memory), "long_term_memory.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["memories"]) == 1
        assert data["memories"][0]["text"] == "持久化测试"


# ── list_memories ───────────────────────────────────────
class TestListMemories:
    def test_list_empty(self, isolated_memory):
        result = ms.list_memories()
        assert result == []

    def test_list_returns_all_in_scope(self, isolated_memory):
        ms.add_memory("事实 A", scope="global")
        ms.add_memory("事实 B", scope="global")
        ms.add_memory("事实 C", scope="project:demo")
        result = ms.list_memories(scope="global")
        assert len(result) == 2
        texts = [m["text"] for m in result]
        assert "事实 A" in texts
        assert "事实 B" in texts

    def test_list_scope_isolation(self, isolated_memory):
        ms.add_memory("only in project", scope="project:x")
        result = ms.list_memories(scope="global")
        assert len(result) == 0


# ── delete_memory ───────────────────────────────────────
class TestDeleteMemory:
    def test_delete_existing(self, isolated_memory):
        mem = ms.add_memory("要删除的")
        assert mem is not None
        ok = ms.delete_memory(mem["id"])
        assert ok is True
        assert ms.list_memories() == []

    def test_delete_nonexistent(self, isolated_memory):
        ok = ms.delete_memory("nonexistent_id")
        assert ok is False


# ── search_memories ─────────────────────────────────────
class TestSearchMemories:
    def test_search_basic(self, isolated_memory):
        ms.add_memory("用户喜欢 Python 编程")
        ms.add_memory("用户在 Windows 上开发")
        results = ms.search_memories("python")
        assert len(results) == 1
        assert "Python" in results[0]["text"]

    def test_search_partial_match(self, isolated_memory):
        ms.add_memory("用户使用 PyTorch 深度学习框架")
        results = ms.search_memories("pytorch")
        assert len(results) == 1

    def test_search_no_match(self, isolated_memory):
        ms.add_memory("事实 X")
        results = ms.search_memories("不存在的关键词")
        assert len(results) == 0

    def test_search_empty_query(self, isolated_memory):
        ms.add_memory("事实 Y")
        results = ms.search_memories("")
        assert len(results) == 0

    def test_search_scope_filter(self, isolated_memory):
        ms.add_memory("全局事实", scope="global")
        ms.add_memory("项目事实", scope="project:demo")
        results = ms.search_memories("事实", scope="global")
        assert len(results) == 1
        assert results[0]["scope"] == "global"


# ── render_memories_for_prompt ──────────────────────────
class TestRenderMemories:
    def test_render_empty(self, isolated_memory):
        result = ms.render_memories_for_prompt()
        assert result == ""

    def test_render_basic(self, isolated_memory):
        ms.add_memory("用户偏好深色主题")
        result = ms.render_memories_for_prompt()
        assert "关于用户的长期记忆" in result
        assert "用户偏好深色主题" in result

    def test_render_respects_max_chars(self, isolated_memory):
        for i in range(50):
            ms.add_memory(f"记忆条目 {i}: " + "x" * 100)
        result = ms.render_memories_for_prompt(max_chars=200)
        assert len(result) <= 200 + 100  # 最后一行可能略超

    def test_render_newest_first(self, isolated_memory):
        ms.add_memory("旧记忆")
        ms.add_memory("新记忆")
        result = ms.render_memories_for_prompt()
        pos_new = result.index("新记忆")
        pos_old = result.index("旧记忆")
        assert pos_new < pos_old  # 新的在前


# ── 损坏恢复 ────────────────────────────────────────────
class TestCorruptionRecovery:
    def test_corrupted_json_resets(self, isolated_memory):
        """JSON 损坏时 add_memory 应正常工作（重置为空再写入）。"""
        mem_file = str(isolated_memory / "long_term_memory.json")
        with open(mem_file, "w", encoding="utf-8") as f:
            f.write("{invalid json!!!")

        result = ms.add_memory("恢复后的新记忆")
        assert result is not None
        assert result["text"] == "恢复后的新记忆"

        all_mems = ms.list_memories()
        assert len(all_mems) == 1

    def test_missing_memories_key_resets(self, isolated_memory):
        """JSON 合法但缺少 memories 键时应重置。"""
        mem_file = str(isolated_memory / "long_term_memory.json")
        with open(mem_file, "w", encoding="utf-8") as f:
            json.dump({"wrong_key": []}, f)

        result = ms.add_memory("结构恢复")
        assert result is not None


# ── 独立运行 ────────────────────────────────────────────
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
