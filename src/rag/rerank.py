"""DashScope gte-rerank：两阶段检索的第二阶段（cross-encoder 精排）。

向量召回是双塔(query/doc 分开编码)，快但粗；rerank 把 (query, 每个候选doc) 拼在一起
交叉编码打分，排序准得多。用同一个千问 key，走 DashScope 原生 rerank 端点。
"""
import math
import time

from ..paths import logger

_RETRIES = 3


def _parse_results(results, n_docs):
    """解析并校验 rerank 响应。返回 [(index, score), ...] 降序；格式异常返回 None。

    校验：非空候选必须有非空结果；index 唯一、int、0<=idx<n_docs；分数为有限数值。
    重复/越界 index 或 NaN 分数意味着响应不可信，静默收下会用错误顺序污染检索结果。
    """
    if not isinstance(results, list) or not results:
        return None   # 非空候选收到空/非列表结果 → 异常
    out = []
    seen = set()
    for r in results:
        if not isinstance(r, dict) or "index" not in r:
            return None
        idx = r["index"]
        if not isinstance(idx, int) or not (0 <= idx < n_docs) or idx in seen:
            return None
        seen.add(idx)
        score = r.get("relevance_score", 0.0)
        if not isinstance(score, (int, float)) or not math.isfinite(score):
            return None
        out.append((idx, float(score)))
    out.sort(key=lambda x: -x[1])
    return out


def rerank(query: str, documents: list[str], *, model, url, api_key, top_n, retries=_RETRIES):
    """把 documents 按与 query 的相关性重排。

    返回 [(原始下标, 相关性分数), ...]（降序，长度 <= top_n）；
    失败（重试仍不成）返回 None，让调用方回退到向量顺序。
    """
    if not documents:
        return []
    if not api_key:
        logger.warning("[RAG] rerank 缺 api_key，跳过")
        return None
    import requests as _requests
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "input": {"query": query, "documents": documents},
        "parameters": {"return_documents": False, "top_n": min(top_n, len(documents))},
    }
    last = None
    for attempt in range(retries):
        try:
            resp = _requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                results = resp.json().get("output", {}).get("results", [])
                out = _parse_results(results, len(documents))
                if out is not None:
                    return out
                # 200 但结果不可信（空/重复/越界 index/非有限分数）→ 按失败重试，
                # 最终 None 让 retriever 回退到向量顺序
                last = "响应校验失败: results 为空/重复/越界 index 或非有限分数"
            else:
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    break   # 鉴权/参数错重试无意义
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    logger.warning(f"[RAG] rerank 失败，回退向量顺序: {last}")
    return None
