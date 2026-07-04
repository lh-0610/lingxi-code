"""知识库检索工具：search_knowledge（只读、Plan 放行、不弹确认）。

对 config.rag.kb_dir 下的 md 资料库做语义检索，返回带出处的片段，供 agent 据实回答。
索引由 rag.index.reindex 预先建好（知识库 tab 的「重建索引」或首次检索时提示）。
"""
from langchain_core.tools import tool

from .paths import logger


@tool
def search_knowledge(query: str) -> str:
    """检索外部资料库（知识库）里与问题最相关的片段，用于「据实回答」。

    返回带编号 [1][2] 和出处的片段。你应**只依据这些内容作答并标注来源**；
    若检索不到相关内容，就如实说明「知识库中没有」，不要编造。
    query: 要检索的问题或关键信息（用自然语言，越贴近问题越好）。
    """
    from . import config
    if not config.RAG_KB_DIR:
        return "知识库未配置：请在 config.json 的 rag.kb_dir 指向一个 md 目录，并先重建索引。"
    if not config.RAG_EMBED_API_KEY:
        return "知识库检索需要 embedding key（默认复用 qwen_api_key），当前为空，请在 config.json 配置。"
    # 会话锚点强校验（工具层是所有发送路径的最后一道闸）：本会话锚定的知识库必须与
    # 当前配置目录一致，避免历史会话（锚定 A）在配置切到 B 后静默检索到 B 的内容。
    try:
        from . import session as _session
        from .rag.index import norm_kb_dir
        _sess = _session.current_session()
    except Exception:
        _sess = None
    _bind_target = None
    if _sess is not None and getattr(_sess, "session_kind", "code") == "rag":
        _cur = norm_kb_dir(config.RAG_KB_DIR)
        _anchor = getattr(_sess, "rag_kb_dir", "") or ""
        if not _anchor:
            _bind_target = _cur   # 空锚点：等检索成功再绑定（失败/不一致的检索不该钉死空会话）
        elif _anchor != _cur:
            return ("本对话锚定的知识库与当前配置目录不同，已停止检索以免答错库——"
                    "请切回该对话原本的知识库目录，或新建一个知识库对话。")
    try:
        from .rag.retriever import retrieve, format_context, IndexMismatchError
        hits = retrieve(
            query,
            embed_model=config.RAG_EMBED_MODEL,
            embed_base_url=config.RAG_EMBED_BASE_URL,
            embed_api_key=config.RAG_EMBED_API_KEY,
            top_k=config.RAG_TOP_K,
            min_score=config.RAG_MIN_SCORE,
            kb_dir=config.RAG_KB_DIR,   # 索引锚校验：换目录/换模型/改切块未重建 → 明确拒绝
            chunk_size=config.RAG_CHUNK_SIZE,
            chunk_overlap=config.RAG_CHUNK_OVERLAP,
            rerank=config.RAG_RERANK,
            rerank_model=config.RAG_RERANK_MODEL,
            rerank_url=config.RAG_RERANK_URL,
            rerank_top_n=config.RAG_RERANK_TOP_N,
        )
    except IndexMismatchError as e:
        # 索引不存在/未建 或 与配置不一致——都不算检索成功，不绑定会话锚点
        return f"知识库索引不可用：{e}"
    except Exception as e:
        logger.warning(f"[RAG] 检索失败: {e}")
        return f"知识库检索出错: {e}"
    # 走到这里说明检索确实跑在一个有效、已建的索引上（未建会在上面抛错）——
    # 此时才把空锚点绑定到当前库（"有效索引未命中"也算成功用过这个库）
    if _bind_target is not None:
        _sess.rag_kb_dir = _bind_target
    if not hits:
        return "知识库中没有与该问题相关的内容（该问题可能不在资料范围内）。"
    return format_context(hits)
