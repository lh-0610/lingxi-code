"""RAG 引擎单元测试（不打网络）：markdown 切块 + numpy 向量库检索/持久化/缓存。

embedding 是真实网络调用，不进单测；这里覆盖确定性的切块逻辑和向量库行为。
"""
import pytest

from src.rag.chunk import chunk_markdown
from src.rag.store import VectorStore, text_hash


class TestChunk:
    def test_headings_and_paths(self):
        md = "# Doc\n\n## A\nalpha text\n\n## B\nbeta text\n"
        chunks = chunk_markdown(md, "d.md", chunk_size=800, overlap=100)
        assert len(chunks) == 2
        headings = {c["heading"] for c in chunks}
        assert "Doc › A" in headings and "Doc › B" in headings
        assert all(c["source"] == "d.md" for c in chunks)

    def test_code_fence_hash_not_treated_as_header(self):
        md = "# T\n\n```\n# not a header\ncode\n```\n\nbody\n"
        chunks = chunk_markdown(md, "x.md", 800, 100)
        assert chunks and all(c["heading"] == "T" for c in chunks)   # 代码里的 # 不算标题

    def test_large_section_split_with_overlap(self):
        big = "para. " * 400   # 单段 ~2400 字符，无空行
        chunks = chunk_markdown("# H\n\n" + big, "b.md", chunk_size=800, overlap=120)
        assert len(chunks) >= 3
        assert all(len(c["text"]) <= 800 for c in chunks)

    def test_overlap_between_adjacent_packed_blocks(self):
        """P2#4 回归：多个短段落打包切块时，相邻块必须共享 overlap 字符上下文
        （旧实现只在硬切超长单段时才有重叠，短段拼块前后完全无重叠）。"""
        paras = [f"seg{i}-" + "x" * 40 for i in range(6)]   # 6 段，每段 43 字符
        chunks = chunk_markdown("# H\n\n" + "\n\n".join(paras), "m.md",
                                chunk_size=100, overlap=30)
        texts = [c["text"] for c in chunks]
        assert len(texts) >= 3 and all(len(t) <= 100 for t in texts)
        for a, b in zip(texts, texts[1:]):
            assert b.startswith(a[-30:]), f"相邻块无重叠:\n{a!r}\n{b!r}"

    def test_zero_overlap_has_no_shared_context(self):
        """overlap=0：退化为纯打包切分，相邻块不共享上下文。"""
        paras = [f"seg{i}-" + "x" * 40 for i in range(6)]
        chunks = chunk_markdown("# H\n\n" + "\n\n".join(paras), "m.md",
                                chunk_size=100, overlap=0)
        texts = [c["text"] for c in chunks]
        assert len(texts) >= 3
        for a, b in zip(texts, texts[1:]):
            assert not b.startswith(a[-10:]), "overlap=0 不应有重叠"


class TestVectorStore:
    def _rebuild(self, store):
        metas = [
            {"text": "a", "heading": "", "source": "a.md", "chunk_id": 0, "hash": text_hash("a")},
            {"text": "b", "heading": "", "source": "b.md", "chunk_id": 0, "hash": text_hash("b")},
            {"text": "c", "heading": "", "source": "c.md", "chunk_id": 0, "hash": text_hash("c")},
        ]
        store.rebuild(metas, [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        return metas

    def test_search_returns_nearest_descending(self, isolated_memory):
        s = VectorStore("t1")
        self._rebuild(s)
        hits = s.search([0.9, 0.1, 0.0], top_k=2)
        assert hits[0][1]["source"] == "a.md"     # 最接近 x 轴
        assert len(hits) == 2 and hits[0][0] > hits[1][0]   # 按相似度降序

    def test_persist_and_reload(self, isolated_memory):
        self._rebuild(VectorStore("t2"))
        s2 = VectorStore("t2")                     # 新实例从磁盘加载
        assert s2.count() == 3
        assert s2.search([0.0, 0.0, 1.0], top_k=1)[0][1]["source"] == "c.md"

    def test_cache_reuse_and_prune(self, isolated_memory):
        s = VectorStore("t3")
        self._rebuild(s)
        assert s.cached_vec(text_hash("a")) is not None       # 供增量复用
        assert s.cached_vec(text_hash("nonexistent")) is None

    def test_empty_store_search(self, isolated_memory):
        assert VectorStore("empty").search([1.0, 0.0], top_k=3) == []


class TestRerank:
    def test_parses_and_sorts_by_score(self, monkeypatch):
        from src.rag import rerank as rr

        class _Resp:
            status_code = 200

            def json(self):
                # 故意乱序，验证按 relevance_score 降序排
                return {"output": {"results": [
                    {"index": 0, "relevance_score": 0.30},
                    {"index": 2, "relevance_score": 0.90},
                ]}}
        monkeypatch.setattr("requests.post", lambda *a, **k: _Resp())
        out = rr.rerank("q", ["d0", "d1", "d2"], model="gte-rerank",
                        url="http://x", api_key="k", top_n=2)
        assert out == [(2, 0.90), (0, 0.30)]

    def test_returns_none_on_server_error(self, monkeypatch):
        from src.rag import rerank as rr

        class _Resp:
            status_code = 500
            text = "boom"
        monkeypatch.setattr("requests.post", lambda *a, **k: _Resp())
        assert rr.rerank("q", ["d0"], model="m", url="http://x",
                         api_key="k", top_n=1, retries=1) is None

    def test_empty_docs(self):
        from src.rag import rerank as rr
        assert rr.rerank("q", [], model="m", url="u", api_key="k", top_n=5) == []

    def _resp(self, results):
        class _R:
            status_code = 200

            def json(self):
                return {"output": {"results": results}}
        return _R()

    def test_empty_results_for_nonempty_docs_is_failure(self, monkeypatch):
        """非空候选收到空 results → None（回退向量顺序），不当成'全都不相关'。"""
        from src.rag import rerank as rr
        monkeypatch.setattr("requests.post", lambda *a, **k: self._resp([]))
        assert rr.rerank("q", ["d0", "d1"], model="m", url="u", api_key="k",
                         top_n=2, retries=1) is None

    def test_duplicate_index_is_failure(self, monkeypatch):
        from src.rag import rerank as rr
        monkeypatch.setattr("requests.post", lambda *a, **k: self._resp(
            [{"index": 0, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.5}]))
        assert rr.rerank("q", ["d0", "d1"], model="m", url="u", api_key="k",
                         top_n=2, retries=1) is None

    def test_out_of_range_index_is_failure(self, monkeypatch):
        from src.rag import rerank as rr
        monkeypatch.setattr("requests.post", lambda *a, **k: self._resp(
            [{"index": 7, "relevance_score": 0.9}]))
        assert rr.rerank("q", ["d0", "d1"], model="m", url="u", api_key="k",
                         top_n=2, retries=1) is None

    def test_nonfinite_score_is_failure(self, monkeypatch):
        from src.rag import rerank as rr
        monkeypatch.setattr("requests.post", lambda *a, **k: self._resp(
            [{"index": 0, "relevance_score": float("nan")}]))
        assert rr.rerank("q", ["d0"], model="m", url="u", api_key="k",
                         top_n=1, retries=1) is None


class TestRagMode:
    """知识库模式的收口：系统提示词切换 + 工具集只剩 search_knowledge。"""

    def test_prompt_switches_with_rag_mode(self):
        from src import state, roles
        state.rag_mode = True
        try:
            assert "知识库问答助手" in roles.get_system_prompt()
        finally:
            state.rag_mode = False
        assert "知识库问答助手" not in roles.get_system_prompt()

    def test_rag_prompt_excludes_longterm_memory(self, monkeypatch):
        """RAG 模式不注入长期记忆——严格'只依据检索资料回答'（记忆会破坏接地/来源标注）。"""
        from src import state, roles, memory_store
        monkeypatch.setattr(memory_store, "render_memories_for_prompt",
                            lambda max_chars=0: "# 关于用户的长期记忆\nSECRET_MEM_MARKER")
        state.rag_mode = True
        try:
            assert "SECRET_MEM_MARKER" not in roles.get_system_prompt()
        finally:
            state.rag_mode = False
        assert "SECRET_MEM_MARKER" in roles.get_system_prompt()   # 编码模式仍注入

    def test_cfg_num_safe_parse(self):
        """配置数值安全解析：非数字回退默认不抛（程序能起）；合法 0 不被 or 吞掉。"""
        from src.config import _cfg_num
        assert _cfg_num({"top_k": "five"}, "top_k", 5) == 5        # 非数字 → 默认，不炸
        assert _cfg_num({"chunk_overlap": 0}, "chunk_overlap", 120) == 0   # 合法 0 保留
        assert _cfg_num({"x": None}, "x", 7) == 7                  # None → 默认
        assert _cfg_num({}, "missing", 9) == 9
        assert _cfg_num({"s": "0.5"}, "s", 0.0, cast=float) == 0.5

    def test_cfg_num_rejects_bool_inf_nan(self):
        """bool（int 子类）/ Infinity（转 int 抛 OverflowError）/ NaN 都必须回退默认。"""
        from src.config import _cfg_num
        assert _cfg_num({"x": True}, "x", 800) == 800              # true ≠ 1
        assert _cfg_num({"x": False}, "x", 800) == 800
        assert _cfg_num({"x": float("inf")}, "x", 800) == 800      # OverflowError 兜住
        assert _cfg_num({"x": float("inf")}, "x", 0.0, cast=float) == 0.0   # isfinite 兜住
        assert _cfg_num({"x": float("nan")}, "x", 0.0, cast=float) == 0.0

    def test_cfg_dict_rejects_non_dict_section(self):
        """rag 段写成字符串/数组 → 按未配置处理（不 AttributeError 炸启动）。"""
        from src.config import _cfg_dict
        assert _cfg_dict({"rag": "oops"}, "rag") == {}
        assert _cfg_dict({"rag": [1, 2]}, "rag") == {}
        assert _cfg_dict({"rag": None}, "rag") == {}
        assert _cfg_dict({"rag": {"a": 1}}, "rag") == {"a": 1}
        assert _cfg_dict({}, "rag") == {}

    def test_cfg_bool_strict(self):
        """严格布尔：bool("false")==True 的坑——字符串 "false" 绝不能开启付费重排。"""
        from src.config import _cfg_bool
        assert _cfg_bool({"rerank": True}, "rerank", False) is True
        assert _cfg_bool({"rerank": False}, "rerank", True) is False
        assert _cfg_bool({"rerank": "false"}, "rerank", True) is False   # 关键：不当 True
        assert _cfg_bool({"rerank": "true"}, "rerank", False) is True
        assert _cfg_bool({"rerank": "0"}, "rerank", True) is False
        assert _cfg_bool({"rerank": "1"}, "rerank", False) is True
        assert _cfg_bool({"rerank": "maybe"}, "rerank", False) is False  # 乱写 → 默认
        assert _cfg_bool({"rerank": ""}, "rerank", True) is False        # 空串 → False
        assert _cfg_bool({}, "rerank", True) is True                     # 缺省 → 默认

    def test_reindex_rejects_bad_chunk_params(self, isolated_memory, tmp_path):
        """chunk_size<=0 / overlap>=chunk_size → 入口 ValueError，不进切块/embedding。"""
        from src.rag.index import reindex
        kb = tmp_path / "kb"
        kb.mkdir()
        (kb / "a.md").write_text("# A\n\nx", encoding="utf-8")
        kw = dict(embed_model="m", embed_base_url="u", embed_api_key="k")
        with pytest.raises(ValueError, match="切块参数"):
            reindex(str(kb), chunk_size=0, chunk_overlap=0, **kw)
        with pytest.raises(ValueError, match="切块参数"):
            reindex(str(kb), chunk_size=100, chunk_overlap=100, **kw)

    def test_build_rag_tools_only_search_knowledge(self):
        from src.tools import build_rag_tools
        names = [t.name for t in build_rag_tools()]
        assert names == ["search_knowledge"]

    def test_execute_tool_gate_rejects_non_rag_tool(self):
        """rag_mode 下 _execute_tool 拒绝非 search_knowledge 工具，拒绝信息进 chat_history。"""
        from src import state
        from src.streaming import _execute_tool

        class _NoopUI:
            def show_message(self, *a, **k):
                pass

        state.chat_history.clear()
        state.rag_mode = True
        try:
            _execute_tool({"name": "run_command", "args": {"command": "echo hi"}, "id": "t1"},
                          _NoopUI())
        finally:
            state.rag_mode = False
        assert state.chat_history, "应有拒绝的 ToolMessage"
        assert "知识库模式" in state.chat_history[-1].content

    def test_rag_mode_is_session_level(self):
        """rag_mode 会话级：前台会话开知识库，不影响别的会话（后台编码会话不被污染）。"""
        from src import session as _session, state
        state.rag_mode = True                       # 落到当前 active 会话
        other = _session.Session()                  # 新会话（如后台编码会话）
        assert other.rag_mode is False              # 不被前台切换影响
        assert _session.get_active().rag_mode is True
        state.rag_mode = False

    def test_resolve_bound_llm_reads_target_session(self):
        """resolve_bound_llm 读【目标会话】的 rag_mode，不读全局/当前线程。"""
        import inspect
        from src.agent import resolve_bound_llm
        src = inspect.getsource(resolve_bound_llm)
        assert 'getattr(session, "rag_mode"' in src   # 锁死实现：从 session 参数读

    def test_can_parallel_disabled_in_rag_mode(self):
        """知识库模式下并行预取关闭（防在 RAG gate 前预执行旧工具）。"""
        from src import state
        from src.streaming import _can_parallel
        calls = [{"name": "read_file", "args": {"path": "a"}},
                 {"name": "search_files", "args": {"regex": "x"}}]
        assert _can_parallel(calls) is True
        state.rag_mode = True
        try:
            assert _can_parallel(calls) is False
        finally:
            state.rag_mode = False


def _stub_embed(texts, **kw):
    """确定性假 embedding：按文本长度造 3 维向量（免网络）。"""
    return [[1.0 + len(t) % 3, 2.0, 3.0] for t in texts]


class TestIndexHardening:
    def _kb(self, tmp_path, files=None):
        kb = tmp_path / "kb"
        kb.mkdir(exist_ok=True)
        for name, text in (files or {"a.md": "# A\n\nalpha content"}).items():
            (kb / name).write_text(text, encoding="utf-8")
        return str(kb)

    def test_read_error_aborts_and_preserves_index(self, isolated_memory, tmp_path, monkeypatch):
        """任一 md 读取失败 → 中止重建、旧索引原样保留（不被清空）。"""
        from src.rag.index import reindex
        from src.rag.store import VectorStore
        import src.rag.index as idx_mod
        monkeypatch.setattr(idx_mod, "embed_texts", _stub_embed)
        kb = self._kb(tmp_path)
        kw = dict(embed_model="m", embed_base_url="u", embed_api_key="k",
                  chunk_size=800, chunk_overlap=100)
        assert reindex(kb, **kw)["chunks"] == 1
        old_count = VectorStore().count()

        import builtins
        _real_open = builtins.open

        def _open(path, *a, **k):
            if str(path).endswith(".md"):
                raise OSError("locked")
            return _real_open(path, *a, **k)
        monkeypatch.setattr(builtins, "open", _open)
        with pytest.raises(RuntimeError, match="中止重建"):
            reindex(kb, **kw)
        monkeypatch.setattr(builtins, "open", _real_open)
        assert VectorStore().count() == old_count     # 旧索引未被清空

    def test_equivalent_base_url_reuses_cache(self, isolated_memory, tmp_path, monkeypatch):
        """https://x/v1 与 https://x/v1/ 是同一端点：不得触发整库重刷（浪费 API 费用）。"""
        from src.rag.index import reindex
        import src.rag.index as idx_mod
        monkeypatch.setattr(idx_mod, "embed_texts", _stub_embed)
        kb = self._kb(tmp_path)
        kw = dict(embed_model="m", embed_api_key="k", chunk_size=800, chunk_overlap=0)
        s1 = reindex(kb, embed_base_url="https://x/v1", **kw)
        assert s1["embedded"] == 1
        s2 = reindex(kb, embed_base_url="https://x/v1/", **kw)   # 仅差尾斜杠
        assert s2["embedded"] == 0 and s2["reused"] == 1         # 等价端点：复用

    def test_model_switch_invalidates_cache(self, isolated_memory, tmp_path, monkeypatch):
        """换 embedding 模型后旧缓存失效：整库重 embed（不复用异空间向量）。"""
        from src.rag.index import reindex
        import src.rag.index as idx_mod
        calls = {"n": 0}

        def _counting_embed(texts, **kw):
            calls["n"] += len(texts)
            return _stub_embed(texts)
        monkeypatch.setattr(idx_mod, "embed_texts", _counting_embed)
        kb = self._kb(tmp_path)
        kw = dict(embed_base_url="u", embed_api_key="k", chunk_size=800, chunk_overlap=100)
        s1 = reindex(kb, embed_model="model-a", **kw)
        assert s1["embedded"] == 1
        s2 = reindex(kb, embed_model="model-a", **kw)
        assert s2["embedded"] == 0 and s2["reused"] == 1   # 同模型：复用
        s3 = reindex(kb, embed_model="model-b", **kw)
        assert s3["embedded"] == 1 and s3["reused"] == 0   # 换模型：全部重 embed

    def test_manifest_records_chunk_params_and_status_flags_change(
            self, isolated_memory, tmp_path, monkeypatch):
        """P2#3：manifest 记录 chunk_size/overlap；改切块参数未重建 → index_status 判需重建。"""
        from src.rag.index import reindex
        from src.rag.retriever import index_status
        from src.rag.store import VectorStore
        import src.rag.index as idx_mod
        monkeypatch.setattr(idx_mod, "embed_texts", _stub_embed)
        kb = self._kb(tmp_path)
        kw = dict(embed_model="m", embed_base_url="u", embed_api_key="k")
        reindex(kb, chunk_size=800, chunk_overlap=100, **kw)
        mf = VectorStore().load().manifest
        assert mf["chunk_size"] == 800 and mf["chunk_overlap"] == 100
        base = dict(kb_dir=kb, embed_model="m", embed_base_url="u")
        assert index_status(chunk_size=800, chunk_overlap=100, **base)["ok"] is True
        st_sz = index_status(chunk_size=500, chunk_overlap=100, **base)
        assert st_sz["ok"] is False and "切块" in st_sz["reason"]
        st_ov = index_status(chunk_size=800, chunk_overlap=50, **base)
        assert st_ov["ok"] is False and "切块" in st_ov["reason"]

    def test_retrieve_refuses_on_chunk_param_change(
            self, isolated_memory, tmp_path, monkeypatch):
        """P2#3：改切块参数未重建 → retrieve 拒绝（不拿旧粒度切片答题；校验在 embed 之前）。"""
        from src.rag.index import reindex
        from src.rag.retriever import retrieve, IndexMismatchError
        import src.rag.index as idx_mod
        monkeypatch.setattr(idx_mod, "embed_texts", _stub_embed)
        kb = self._kb(tmp_path)
        reindex(kb, embed_model="m", embed_base_url="u", embed_api_key="k",
                chunk_size=800, chunk_overlap=100)
        with pytest.raises(IndexMismatchError, match="切块"):
            retrieve("q", embed_model="m", embed_base_url="u", embed_api_key="k",
                     kb_dir=kb, chunk_size=500, chunk_overlap=100)

    def test_reindex_reembeds_when_cache_corrupt(self, isolated_memory, tmp_path, monkeypatch):
        """P1 端到端：坏缓存（nan）被丢 → 重新 embedding → 索引有效可加载（不再"报成功却 0 块"）。"""
        import json
        import numpy as np
        from src.rag.index import reindex
        from src.rag.store import VectorStore
        import src.rag.index as idx_mod
        monkeypatch.setattr(idx_mod, "embed_texts", _stub_embed)
        kb = self._kb(tmp_path)
        kw = dict(embed_model="m", embed_base_url="u", embed_api_key="k",
                  chunk_size=800, chunk_overlap=100)
        reindex(kb, **kw)
        st = VectorStore().load()
        gen, dim = st.manifest["generation"], st.manifest["dim"]
        with open(st._cache_path(gen), "w", encoding="utf-8") as f:      # 污染缓存为 nan
            for m in st.metas:
                f.write(json.dumps({"h": m["hash"], "v": [float("nan")] * dim}) + "\n")
        stats = reindex(kb, **kw)                                        # 同参数再重建
        assert stats["chunks"] >= 1 and stats["embedded"] == stats["chunks"]   # 全部重新 embed
        s2 = VectorStore()
        assert s2.count() == stats["chunks"]                            # 索引真可加载（非 0）
        assert np.isfinite(s2.vecs).all()                               # 落盘向量全有限

    def test_reindex_rejects_too_small_chunk_size(self, isolated_memory, tmp_path):
        """P2：chunk_size 低于下限 → ValueError（防海量切片/天量请求）。"""
        from src.rag.index import reindex
        kb = self._kb(tmp_path)
        kw = dict(embed_model="m", embed_base_url="u", embed_api_key="k")
        with pytest.raises(ValueError, match="切块参数"):
            reindex(kb, chunk_size=1, chunk_overlap=0, **kw)

    def test_reindex_caps_total_chunks(self, isolated_memory, tmp_path, monkeypatch):
        """P2：切片总数超上限 → embedding 前中止（不发请求、旧索引不动）。"""
        from src.rag.index import reindex
        import src.rag.index as idx_mod
        calls = {"n": 0}

        def _spy(texts, **kw):
            calls["n"] += len(texts)
            return _stub_embed(texts)
        monkeypatch.setattr(idx_mod, "embed_texts", _spy)
        monkeypatch.setattr(idx_mod, "RAG_MAX_TOTAL_CHUNKS", 2)
        kb = tmp_path / "big"; kb.mkdir()
        (kb / "a.md").write_text("# A\n\n" + "x" * 400 + "\n\n# B\n\n" + "y" * 400, encoding="utf-8")
        kw = dict(embed_model="m", embed_base_url="u", embed_api_key="k",
                  chunk_size=100, chunk_overlap=0)
        with pytest.raises(RuntimeError, match="超过上限"):
            reindex(str(kb), **kw)
        assert calls["n"] == 0                                          # 没发 embedding 请求

    def test_chunk_cap_stops_lazy_iterator_immediately(self, isolated_memory, tmp_path, monkeypatch):
        """总块数保护必须边产出边检查，不能先构造完整巨型列表再判断。"""
        from src.rag.index import reindex
        import src.rag.index as idx_mod

        seen = {"n": 0}

        def _many_chunks(text, source, chunk_size, overlap):
            for i in range(100):
                seen["n"] += 1
                yield {"text": f"x{i}", "heading": "", "source": source, "chunk_id": i}

        monkeypatch.setattr(idx_mod, "iter_markdown_chunks", _many_chunks)
        monkeypatch.setattr(idx_mod, "RAG_MAX_TOTAL_CHUNKS", 2)
        kb = tmp_path / "lazy"; kb.mkdir()
        (kb / "a.md").write_text("x", encoding="utf-8")
        with pytest.raises(RuntimeError, match="超过上限"):
            reindex(str(kb), embed_model="m", embed_base_url="u", embed_api_key="k",
                    chunk_size=100, chunk_overlap=0)
        assert seen["n"] == 3          # 允许 2 块；看到第 3 块立即停，未继续枚举剩余 97 块


class TestStoreHardening:
    def _mk(self, name, texts=("a", "b")):
        from src.rag.store import VectorStore, text_hash
        s = VectorStore(name)
        metas = [{"text": t, "heading": "", "source": f"{t}.md", "chunk_id": 0,
                  "hash": text_hash(t)} for t in texts]
        vecs = [[1.0, 0.0], [0.0, 1.0]][:len(texts)]
        s.rebuild(metas, vecs)
        return s

    def test_corrupt_length_mismatch_treated_as_empty(self, isolated_memory):
        """向量行数 ≠ meta 条数（当前代文件被外力破坏）→ 按损坏清空，不再 IndexError。"""
        from src.rag.store import VectorStore
        s = self._mk("c1")
        gen = s.manifest["generation"]
        with open(s._meta_path(gen), encoding="utf-8") as f:
            lines = f.readlines()
        with open(s._meta_path(gen), "w", encoding="utf-8") as f:
            f.write(lines[0])                               # meta 少一行
        s2 = VectorStore("c1")
        assert s2.count() == 0                              # 按损坏清空
        assert s2.search([1.0, 0.0], top_k=2) == []         # 不崩

    def test_1d_vec_file_treated_as_corrupt(self, isolated_memory):
        """P2#2：一维 .npy（长度恰等于 meta 数）会蒙混过数量校验，须由 ndim 校验拦下，
        否则 search 里 vecs@q 退化成标量、len(sims) 抛 TypeError。"""
        import numpy as np
        from src.rag.store import VectorStore
        s = self._mk("c1d")                                 # 2 条 meta / 2 维向量 / manifest dim=2
        gen = s.manifest["generation"]
        np.save(s._vec_path(gen), np.array([1.0, 2.0], dtype=np.float32))   # 覆盖成一维长度 2
        s2 = VectorStore("c1d")
        assert s2.count() == 0                              # ndim!=2 → 按损坏处理
        assert s2.search([1.0, 0.0], top_k=2) == []         # 不抛 TypeError

    def test_corrupt_cache_vec_dropped_on_load(self, isolated_memory):
        """P1：缓存里的坏向量（nan / 维度不符）加载时丢弃，不会被复用污染新索引。"""
        import json
        from src.rag.store import VectorStore, text_hash
        s = self._mk("cc")                                  # dim=2；写了 vec_cache
        gen = s.manifest["generation"]
        with open(s._cache_path(gen), "w", encoding="utf-8") as f:
            f.write(json.dumps({"h": text_hash("a"), "v": [float("nan"), 0.0]}) + "\n")
            f.write(json.dumps({"h": text_hash("b"), "v": [1.0]}) + "\n")   # 维度不符
        s2 = VectorStore("cc")
        s2.load()
        assert s2.cached_vec(text_hash("a")) is None        # nan → 丢
        assert s2.cached_vec(text_hash("b")) is None        # 维度不符 → 丢
        assert s2.count() == 2                              # 主索引（.npy）未受影响，仍可用

    def test_malformed_cache_json_does_not_invalidate_main_index(self, isolated_memory):
        """vec_cache 是可丢优化层：JSON 截断只能清缓存，不能拖累完整 vec/meta 主索引。"""
        from src.rag.store import VectorStore
        s = self._mk("bad-cache-json")
        gen = s.manifest["generation"]
        with open(s._cache_path(gen), "w", encoding="utf-8") as f:
            f.write('{"h": "truncated"\n')
        s2 = VectorStore("bad-cache-json").load()
        assert s2.count() == 2
        assert s2._cache == {}
        assert s2.search([1.0, 0.0], top_k=1)[0][1]["source"] == "a.md"

    def test_corrupt_cache_json_does_not_wipe_index(self, isolated_memory):
        """P2：缓存文件语法损坏 / 行缺 h / 非 dict → 只丢缓存，完整主索引（vec+meta）照常可用。"""
        from src.rag.store import VectorStore
        s = self._mk("cj")                                  # 2 块有效主索引
        gen = s.manifest["generation"]
        with open(s._cache_path(gen), "w", encoding="utf-8") as f:
            f.write("{ this is not valid json\n")           # 语法错误
            f.write('{"v": [1.0, 0.0]}\n')                   # 缺 h
            f.write("42\n")                                  # 非 dict
        s2 = VectorStore("cj")
        assert s2.count() == 2                              # 主索引未被连累清空
        assert s2.search([1.0, 0.0], top_k=2)               # 仍可检索
        assert s2._cache == {}                              # 坏缓存整体丢弃

    def test_rebuild_refuses_nan_matrix_keeps_old_index(self, isolated_memory):
        """P1：提交前校验拦下含 nan 的向量矩阵 → 抛错、不落盘、不清旧代（旧索引保留）。"""
        from src.rag.store import VectorStore, text_hash
        s = self._mk("keep")                                # 有效旧代：2 块
        old_gen = s.manifest["generation"]
        bad_meta = [{"text": "z", "heading": "", "source": "z.md", "chunk_id": 0,
                     "hash": text_hash("z")}]
        with pytest.raises(ValueError, match="nan|拒绝提交"):
            s.rebuild(bad_meta, [[float("nan"), 0.0]])
        s2 = VectorStore("keep")
        assert s2.count() == 2                              # 旧代仍在（未被 0 块顶替）
        assert s2.manifest["generation"] == old_gen         # manifest 未提交新代

    def test_partial_new_generation_not_visible(self, isolated_memory):
        """事务性：新一代数据文件写了、manifest 没提交（中途崩溃）→ 读到的仍是完整旧代。"""
        import numpy as np
        from src.rag.store import VectorStore
        s = self._mk("c3")
        gen = s.manifest["generation"]
        # 模拟崩溃现场：下一代的向量 + meta 写了一半（块数与旧代相同，长度校验发现不了）
        np.save(s._vec_path(gen + 1), np.asarray([[9.0, 9.0], [8.0, 8.0]], dtype=np.float32))
        with open(s._meta_path(gen + 1), "w", encoding="utf-8") as f:
            f.write('{"text":"EVIL","heading":"","source":"evil.md","chunk_id":0,"hash":"x"}\n'
                    '{"text":"EVIL2","heading":"","source":"evil2.md","chunk_id":0,"hash":"y"}\n')
        s2 = VectorStore("c3").load()
        assert s2.manifest["generation"] == gen             # manifest 仍指向旧代
        hits = s2.search([1.0, 0.0], top_k=1)
        assert hits[0][1]["source"] == "a.md"               # 读到的是旧代内容，不是 EVIL

    def test_legacy_flat_files_require_rebuild(self, isolated_memory):
        """旧版散文件格式（无 generation manifest）→ 按未建索引处理，不误读。"""
        import os
        from src.rag.store import VectorStore
        s = VectorStore("legacy")
        os.makedirs(s.dir, exist_ok=True)
        with open(os.path.join(s.dir, "meta.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"text":"old","heading":"","source":"o.md","chunk_id":0,"hash":"h"}\n')
        assert VectorStore("legacy").count() == 0

    def test_manifest_roundtrip(self, isolated_memory):
        from src.rag.store import VectorStore, text_hash
        s = VectorStore("c2")
        s.rebuild([{"text": "a", "heading": "", "source": "a.md", "chunk_id": 0,
                    "hash": text_hash("a")}], [[1.0, 0.0]],
                  manifest={"kb_dir": "X", "embed_model": "m"})
        s2 = VectorStore("c2").load()
        assert s2.manifest["kb_dir"] == "X" and s2.manifest["embed_model"] == "m"
        assert s2.manifest["chunks"] == 1 and s2.manifest["dim"] == 2
        assert isinstance(s2.manifest["generation"], int)

    def test_transient_read_error_aborts_rebuild(self, isolated_memory, monkeypatch):
        """manifest 读取遇瞬时 I/O 错（PermissionError）→ rebuild 中止，活动代不被覆盖。"""
        from src.rag.store import VectorStore, text_hash
        s = self._mk("c4")
        old_gen = s.manifest["generation"]

        import builtins
        _real_open = builtins.open

        def _open(path, *a, **k):
            if str(path).endswith("manifest.json"):
                raise PermissionError("locked by AV")
            return _real_open(path, *a, **k)
        monkeypatch.setattr(builtins, "open", _open)
        s2 = VectorStore("c4")   # 未加载的新实例（模拟另一次进程/调用）
        with pytest.raises(OSError):
            s2.rebuild([{"text": "z", "heading": "", "source": "z.md", "chunk_id": 0,
                         "hash": text_hash("z")}], [[0.5, 0.5]])
        monkeypatch.setattr(builtins, "open", _real_open)
        s3 = VectorStore("c4").load()
        assert s3.manifest["generation"] == old_gen      # 活动代原样
        assert s3.search([1.0, 0.0], top_k=1)[0][1]["source"] == "a.md"

    def test_generation_never_collides_with_stray_files(self, isolated_memory):
        """磁盘残留了更高代号的文件（上次崩溃遗留）→ 新代号必须跳过它，绝不覆盖写。"""
        import numpy as np
        from src.rag.store import text_hash
        s = self._mk("c5")                                # gen 1
        np.save(s._vec_path(5), np.asarray([[9.0, 9.0]], dtype=np.float32))  # 残留 gen5
        s.rebuild([{"text": "n", "heading": "", "source": "n.md", "chunk_id": 0,
                    "hash": text_hash("n")}], [[1.0, 0.0]])
        assert s.manifest["generation"] == 6              # max(1, 5) + 1，不与残留冲突


class TestRetrieverAnchor:
    def _build(self, kb_dir, model="m"):
        from src.rag.store import VectorStore, text_hash
        from src.rag.index import norm_kb_dir
        s = VectorStore()
        s.rebuild([{"text": "alpha", "heading": "", "source": "a.md", "chunk_id": 0,
                    "hash": text_hash("alpha")}], [[1.0, 0.0, 0.0]],
                  manifest={"kb_dir": norm_kb_dir(kb_dir), "embed_model": model,
                            "embed_base_url": "u"})

    def test_dir_switch_refuses_stale_index(self, isolated_memory, tmp_path, monkeypatch):
        """切换 kb_dir 未重建 → 拒绝检索（不再静默返回旧库内容）。"""
        from src.rag.retriever import retrieve, IndexMismatchError
        a, b = tmp_path / "A", tmp_path / "B"
        a.mkdir(); b.mkdir()
        self._build(str(a))
        with pytest.raises(IndexMismatchError, match="重建索引"):
            retrieve("q", embed_model="m", embed_base_url="u", embed_api_key="k",
                     kb_dir=str(b))

    def test_base_url_switch_refuses_stale_index(self, isolated_memory, tmp_path):
        """换 embedding 端点未重建 → 拒绝检索（新端点向量空间与旧索引不可比）。"""
        from src.rag.retriever import retrieve, IndexMismatchError
        a = tmp_path / "A"; a.mkdir()
        self._build(str(a))                                   # manifest 端点 = "u"
        with pytest.raises(IndexMismatchError, match="重建索引"):
            retrieve("q", embed_model="m", embed_base_url="http://other", embed_api_key="k",
                     kb_dir=str(a))

    def test_index_status_matches_and_flags(self, isolated_memory, tmp_path):
        """index_status：锚一致 → ok+块数；目录切换 → 需重建（UI 不再误报旧块数为可用）。"""
        from src.rag.retriever import index_status
        a, b = tmp_path / "A", tmp_path / "B"
        a.mkdir(); b.mkdir()
        self._build(str(a))
        st = index_status(kb_dir=str(a), embed_model="m", embed_base_url="u")
        assert st["ok"] is True and st["chunks"] == 1
        st2 = index_status(kb_dir=str(b), embed_model="m", embed_base_url="u")
        assert st2["ok"] is False and "重建" in st2["reason"]

    def test_model_switch_refuses_stale_index(self, isolated_memory, tmp_path, monkeypatch):
        from src.rag.retriever import retrieve, IndexMismatchError
        a = tmp_path / "A"; a.mkdir()
        self._build(str(a), model="model-a")
        with pytest.raises(IndexMismatchError, match="重建索引"):
            retrieve("q", embed_model="model-b", embed_base_url="u", embed_api_key="k",
                     kb_dir=str(a))

    def test_matching_anchor_retrieves(self, isolated_memory, tmp_path, monkeypatch):
        from src.rag import retriever as rmod
        a = tmp_path / "A"; a.mkdir()
        self._build(str(a))
        monkeypatch.setattr(rmod, "embed_query", lambda q, **kw: [1.0, 0.0, 0.0])
        hits = rmod.retrieve("q", embed_model="m", embed_base_url="u", embed_api_key="k",
                             kb_dir=str(a))
        assert hits and hits[0]["source"] == "a.md"

    def test_retrieve_fail_closed_on_missing_chunk_meta(self, isolated_memory, tmp_path):
        """P2#1：旧索引（manifest 无切块元数据）+ 调用方传了 chunk 参数 → retrieve 拒绝
        （fail-closed）。绕过 GUI 直接调工具也不放行——与 index_status 同一判据。"""
        from src.rag.retriever import retrieve, IndexMismatchError
        a = tmp_path / "A"; a.mkdir()
        self._build(str(a))     # manifest 只有 kb_dir/embed_model/embed_base_url，无切块字段
        with pytest.raises(IndexMismatchError, match="切块"):
            retrieve("q", embed_model="m", embed_base_url="u", embed_api_key="k",
                     kb_dir=str(a), chunk_size=800, chunk_overlap=100)

    def test_index_status_fail_closed_on_missing_chunk_meta(self, isolated_memory, tmp_path):
        """P2#1：index_status 对缺切块元数据的旧索引同样判需重建（两处判据一致）。"""
        from src.rag.retriever import index_status
        a = tmp_path / "A"; a.mkdir()
        self._build(str(a))
        st = index_status(kb_dir=str(a), embed_model="m", embed_base_url="u",
                          chunk_size=800, chunk_overlap=100)
        assert st["ok"] is False and "切块" in st["reason"]


class TestSearchKnowledgeAnchor:
    """P1#1：工具层强制校验会话锚点——历史会话不得静默检索到已切换后的别的库。"""

    def _rag_session(self, kb_dir):
        from src import session as _session
        s = _session.Session()
        s.session_kind = "rag"
        s.rag_kb_dir = kb_dir
        _session.set_active(s)
        return s

    def test_tool_refuses_when_session_anchor_differs(self, isolated_memory, tmp_path, monkeypatch):
        from src import config
        import src.tools_rag as tr
        from src.rag.index import norm_kb_dir
        kb_b = tmp_path / "B"; kb_b.mkdir()
        monkeypatch.setattr(config, "RAG_KB_DIR", str(kb_b))
        monkeypatch.setattr(config, "RAG_EMBED_API_KEY", "k")
        self._rag_session(norm_kb_dir(str(tmp_path / "A")))   # 锚定 A，配置切到 B
        out = tr.search_knowledge.invoke({"query": "x"})
        assert "锚定的知识库与当前配置目录不同" in out

    def test_tool_binds_empty_anchor_on_valid_index_miss(self, isolated_memory, tmp_path, monkeypatch):
        """空锚点 + 有效索引但未命中（retrieve 返回 []）→ 绑定当前库（算成功用过）。"""
        from src import config
        import src.tools_rag as tr
        from src.rag import retriever as rmod
        from src.rag.index import norm_kb_dir
        kb = tmp_path / "kb"; kb.mkdir()
        monkeypatch.setattr(config, "RAG_KB_DIR", str(kb))
        monkeypatch.setattr(config, "RAG_EMBED_API_KEY", "k")
        monkeypatch.setattr(rmod, "retrieve", lambda *a, **k: [])   # 有效索引未命中（免真检索）
        s = self._rag_session("")                                   # 空锚点
        tr.search_knowledge.invoke({"query": "x"})
        assert s.rag_kb_dir == norm_kb_dir(str(kb))                 # 首次使用绑定当前库

    def test_tool_does_not_bind_when_index_absent(self, isolated_memory, tmp_path, monkeypatch):
        """P2#1：索引不存在（未建，count==0）→ 工具明确提示重建且**不绑定**会话锚点，
        区别于"有效索引未命中"。走真 retrieve（isolated_memory 下无任何索引）。"""
        from src import config
        import src.tools_rag as tr
        kb = tmp_path / "kb"; kb.mkdir()
        monkeypatch.setattr(config, "RAG_KB_DIR", str(kb))
        monkeypatch.setattr(config, "RAG_EMBED_API_KEY", "k")
        s = self._rag_session("")                                   # 空锚点、从未建索引
        out = tr.search_knowledge.invoke({"query": "x"})
        assert "重建" in out                                        # 提示先重建
        assert s.rag_kb_dir == ""                                   # 未建索引 → 不绑定


class TestEmbedValidation:
    def test_200_with_empty_data_fails(self, monkeypatch):
        """HTTP 200 但 data 为空 → 按失败处理（不静默返回 []，防向量错位）。"""
        from src.rag.embed import embed_texts

        class _Resp:
            status_code = 200

            def json(self):
                return {"data": []}
        monkeypatch.setattr("requests.post", lambda *a, **k: _Resp())
        with pytest.raises(RuntimeError, match="校验失败"):
            embed_texts(["a", "b"], model="m", base_url="http://x", api_key="k", retries=1)

    def test_dim_mismatch_fails(self, monkeypatch):
        from src.rag.embed import embed_texts

        class _Resp:
            status_code = 200

            def json(self):
                return {"data": [{"index": 0, "embedding": [1.0, 2.0]},
                                 {"index": 1, "embedding": [1.0]}]}
        monkeypatch.setattr("requests.post", lambda *a, **k: _Resp())
        with pytest.raises(RuntimeError, match="校验失败"):
            embed_texts(["a", "b"], model="m", base_url="http://x", api_key="k", retries=1)

    def test_duplicate_index_fails(self, monkeypatch):
        """index 重复（[0,0]）：数量/维度都对但向量已错位，必须按失败处理。"""
        from src.rag.embed import embed_texts

        class _Resp:
            status_code = 200

            def json(self):
                return {"data": [{"index": 0, "embedding": [1.0, 2.0]},
                                 {"index": 0, "embedding": [3.0, 4.0]}]}
        monkeypatch.setattr("requests.post", lambda *a, **k: _Resp())
        with pytest.raises(RuntimeError, match="index 序列异常"):
            embed_texts(["a", "b"], model="m", base_url="http://x", api_key="k", retries=1)

    def test_missing_index_key_fails(self, monkeypatch):
        from src.rag.embed import embed_texts

        class _Resp:
            status_code = 200

            def json(self):
                return {"data": [{"embedding": [1.0, 2.0]}, {"embedding": [3.0, 4.0]}]}
        monkeypatch.setattr("requests.post", lambda *a, **k: _Resp())
        with pytest.raises(RuntimeError, match="index 序列异常"):
            embed_texts(["a", "b"], model="m", base_url="http://x", api_key="k", retries=1)
