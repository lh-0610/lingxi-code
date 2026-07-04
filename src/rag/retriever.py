"""检索：query → embed → 向量库 top-k（+ 相似度阈值）→ 拼成带编号引用的上下文。"""
from .embed import embed_query
from .store import VectorStore


class IndexMismatchError(RuntimeError):
    """索引与当前配置不一致（换了知识库目录 / embedding 模型/端点 / 切块参数但没重建），拒绝检索。"""


def anchor_mismatch(mf: dict, *, kb_dir, embed_model, embed_base_url,
                    chunk_size=None, chunk_overlap=None) -> str:
    """索引 manifest 与当前配置的锚点校验：返回不一致原因（需重建）或 ""（一致）。

    index_status（UI 状态）与 retrieve（实际检索）**共用同一判据**，避免两处漂移——
    尤其是「缺字段」一律 fail-closed（旧版索引缺 kb_dir / embed / 切块元数据都要求重建），
    杜绝绕过 GUI 直接调用工具时旧索引被静默放行。
    kb_dir 为空时跳过目录比较（retrieve 的可选锚语义）；chunk_size/chunk_overlap
    为 None（调用方不关心）时跳过对应比较。
    """
    from .index import norm_kb_dir, norm_url as _norm_url
    if kb_dir and mf.get("kb_dir") != norm_kb_dir(kb_dir):
        return "知识库目录已切换（或索引缺目录信息），需重建索引"
    if mf.get("embed_model") != embed_model:
        return "embedding 模型已变（或索引缺模型信息），需重建索引"
    if _norm_url(mf.get("embed_base_url", "")) != _norm_url(embed_base_url):
        return "embedding 端点已变，需重建索引"
    if chunk_size is not None and mf.get("chunk_size") != chunk_size:
        return "切块参数已变（或索引缺切块元数据），需重建索引"
    if chunk_overlap is not None and mf.get("chunk_overlap") != chunk_overlap:
        return "切块参数已变（或索引缺切块元数据），需重建索引"
    return ""


def index_status(*, kb_dir, embed_model, embed_base_url,
                 chunk_size=None, chunk_overlap=None, name="default") -> dict:
    """给 UI 的索引状态：{chunks, ok, reason}。ok=False 时 reason 说明为何需重建。
    与 retrieve 的锚校验同一套判据（anchor_mismatch）——UI 显示的"已索引 N 块"必须是
    当前配置下真正可检索的。"""
    store = VectorStore(name).load()
    n = store.count()
    if n == 0:
        return {"chunks": 0, "ok": False, "reason": "未建索引"}
    if not kb_dir:
        return {"chunks": n, "ok": False, "reason": "未配置知识库目录"}
    reason = anchor_mismatch(store.manifest, kb_dir=kb_dir, embed_model=embed_model,
                             embed_base_url=embed_base_url,
                             chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    return {"chunks": n, "ok": not reason, "reason": reason}


def retrieve(query: str, *, embed_model, embed_base_url, embed_api_key,
             top_k=5, min_score=0.0, name="default", kb_dir="",
             chunk_size=None, chunk_overlap=None,
             rerank=False, rerank_model="gte-rerank", rerank_url="", rerank_top_n=20) -> list[dict]:
    """返回命中片段列表 [{score, text, heading, source, chunk_id, hash, [rerank_score]}, ...]。

    rerank=True 时走两阶段：向量先粗召回 rerank_top_n 条，再用 cross-encoder 精排成 top_k；
    rerank 调用失败则回退到向量顺序，保证"总能出结果"。

    索引锚校验走 anchor_mismatch（与 index_status 同一判据、fail-closed）：manifest 里的
    kb_dir / embed 模型/端点 / 切块参数与当前配置不一致（或旧索引缺这些字段）→
    抛 IndexMismatchError（否则换了目录还会检索出旧库内容、换了模型比出乱序结果）。
    索引为空（未建）同样抛 IndexMismatchError；有效索引但无匹配才返回 []。
    """
    store = VectorStore(name).load()
    # 「索引不存在/未建」与「有效索引未命中」是两回事：前者抛错（调用方据此不绑定会话、
    # 提示重建），后者返回 []（正常的"没有相关内容"）。绝不把 count==0 当检索成功。
    if store.count() == 0:
        raise IndexMismatchError("知识库索引尚未建立（无任何片段），请先「重建索引」。")
    # 索引锚校验：与 index_status 共用 anchor_mismatch（fail-closed，缺字段也拒绝），
    # 换目录/换模型/换端点/改切块参数但没重建 → 抛错，绝不拿旧索引答题。
    reason = anchor_mismatch(store.manifest, kb_dir=kb_dir, embed_model=embed_model,
                             embed_base_url=embed_base_url,
                             chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    if reason:
        raise IndexMismatchError(reason + "——请先「重建索引」。")
    qv = embed_query(query, model=embed_model, base_url=embed_base_url, api_key=embed_api_key)
    # 开 rerank 时召回更大的候选池，给精排留空间
    pool_k = max(top_k, rerank_top_n) if rerank else top_k
    hits = store.search(qv, pool_k)
    if min_score > 0:
        hits = [(s, m) for s, m in hits if s >= min_score]
    cand = [{"score": round(s, 4), **m} for s, m in hits]

    if rerank and len(cand) > 1:
        from .rerank import rerank as _rerank
        docs = [(f"{c['heading']}\n{c['text']}" if c.get("heading") else c["text"]) for c in cand]
        order = _rerank(query, docs, model=rerank_model, url=rerank_url,
                        api_key=embed_api_key, top_n=top_k)
        if order is not None:
            out = []
            for idx, score in order:
                if 0 <= idx < len(cand):
                    item = dict(cand[idx])
                    item["rerank_score"] = round(score, 4)
                    out.append(item)
            return out[:top_k]
        # rerank 失败 → 回退向量顺序（下面截断）

    return cand[:top_k]


def format_context(hits: list[dict]) -> str:
    """把命中片段拼成给模型的上下文：带 [n] 编号 + 出处，便于据实回答并标注来源。"""
    if not hits:
        return "（知识库中未检索到相关内容）"
    blocks = []
    for i, h in enumerate(hits, 1):
        loc = h.get("source", "?")
        if h.get("heading"):
            loc += f" › {h['heading']}"
        score = h.get("rerank_score", h.get("score", 0))   # 有重排分优先显示重排分
        blocks.append(f"[{i}] 来源: {loc}（相关度 {score}）\n{h.get('text', '')}")
    return "\n\n".join(blocks)
