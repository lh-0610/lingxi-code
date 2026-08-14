"""检索：query → embed → 向量库 top-k（+ 相似度阈值 + 分域配额）→ 拼成带编号引用的上下文。

分域配额（scope="all" 且索引里有多个 category 时）：不是一把捞 top-k 让所有域混着抢，
而是**每个域各查一遍，再按名次轮转合并**。这样两批同题材资料（如「本项目的实现」与
「通用学习资料」）不会互相挤占——某个域没内容时名额自动让给其它域，不浪费。
"""
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
    return {"chunks": n, "ok": not reason, "reason": reason,
            "categories": store.categories()}


def _balanced_search(store, qv, top_k: int, categories: list[str], pool: int):
    """按名次轮转合并各域的检索结果：先取每域第 1 名，再取每域第 2 名……直到凑够 top_k。

    同一轮内按分数高低决定谁先进（凑不满整轮时优先留给更相关的那条）；某域提前取完，
    剩下的名额自然被其它域用掉（不留空位）。
    """
    ranked = {c: store.search(qv, pool, where={"category": c}) for c in categories}
    out: list[tuple[float, dict]] = []
    rank = 0
    while len(out) < top_k:
        round_hits = [ranked[c][rank] for c in categories if rank < len(ranked[c])]
        if not round_hits:
            break
        round_hits.sort(key=lambda x: -x[0])
        out.extend(round_hits[:top_k - len(out)])
        rank += 1
    out.sort(key=lambda x: -x[0])
    return out


def retrieve(query: str, *, embed_model, embed_base_url, embed_api_key,
             top_k=5, min_score=0.0, name="default", kb_dir="",
             chunk_size=None, chunk_overlap=None, scope="all",
             rerank=False, rerank_model="gte-rerank", rerank_url="", rerank_top_n=20) -> list[dict]:
    """返回命中片段列表 [{score, text, heading, source, chunk_id, hash, category, [rerank_score]}, ...]。

    scope: "all"（默认，多域时走分域配额）或某个具体分域名（只在该域内检索）。
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
    # 分域范围：scope 指定了具体域就只查它；"all" 且索引里确实有多个域才走配额合并
    # （单域库——比如根目录平铺放 md——走原来的单次查询，行为不变）。
    cats = store.categories()
    scope = (scope or "all").strip() or "all"
    qv = embed_query(query, model=embed_model, base_url=embed_base_url, api_key=embed_api_key)
    # 开 rerank 时召回更大的候选池，给精排留空间
    pool_k = max(top_k, rerank_top_n) if rerank else top_k
    if scope != "all":
        if cats and scope not in cats:
            return []       # 指定了不存在的域：如实返回空，不静默退回全库
        hits = store.search(qv, pool_k, where={"category": scope})
    elif len(cats) > 1:
        hits = _balanced_search(store, qv, pool_k, cats, pool_k)
    else:
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
