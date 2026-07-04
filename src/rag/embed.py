"""DashScope /embeddings（OpenAI 兼容端点）批量 embedding，带退避重试。

复用 config 里的千问 key + compatible-mode/v1 端点，不引入新依赖（只用 requests）。
"""
import time

from ..paths import logger

_BATCH = 10          # DashScope text-embedding-v3 单次批量上限（保守取 10）
_RETRIES = 3


def _endpoint(base_url: str) -> str:
    return base_url.rstrip("/") + "/embeddings"


def embed_texts(texts, *, model, base_url, api_key, batch=_BATCH, retries=_RETRIES) -> list[list[float]]:
    """把一批文本转成向量，保持与输入同序。失败重试仍不成则抛 RuntimeError。"""
    if not texts:
        return []
    if not api_key:
        raise RuntimeError("embedding api_key 为空（应复用 qwen_api_key）")
    url = _endpoint(base_url)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        out.extend(_embed_batch(url, headers, model, part, retries))
    return out


def _validate_batch(vecs, expected_n) -> str | None:
    """校验一批 embedding：数量对齐、都是非空数值列表、维度一致、全为有限值。
    返回 None = 通过；str = 问题描述（当次按失败处理）。"""
    import math
    if len(vecs) != expected_n:
        return f"返回 {len(vecs)} 条 embedding，期望 {expected_n} 条"
    dim = None
    for i, v in enumerate(vecs):
        if not isinstance(v, list) or not v:
            return f"第 {i} 条 embedding 为空/非列表"
        if dim is None:
            dim = len(v)
        elif len(v) != dim:
            return f"第 {i} 条维度 {len(v)} ≠ 首条 {dim}"
        for x in v:
            if not isinstance(x, (int, float)) or not math.isfinite(x):
                return f"第 {i} 条含非有限数值"
    return None


def _embed_batch(url, headers, model, texts, retries) -> list[list[float]]:
    import requests as _requests
    last = None
    for attempt in range(retries):
        try:
            resp = _requests.post(
                url, headers=headers,
                json={"model": model, "input": texts, "encoding_format": "float"},
                timeout=60,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                # index 必须唯一、无缺失、无越界、恰为 0..N-1——重复/缺失时排序照样
                # "成功"但向量已错位，长度校验发现不了，必须在这里挡住
                idxs = [d.get("index") for d in data]
                if (any(not isinstance(i, int) for i in idxs)
                        or sorted(idxs) != list(range(len(texts)))):
                    last = f"响应校验失败: index 序列异常（期望 0..{len(texts)-1}，实得 {idxs[:10]}）"
                else:
                    data.sort(key=lambda d: d["index"])   # 保证与输入同序
                    vecs = [d.get("embedding") for d in data]
                    err = _validate_batch(vecs, len(texts))
                    if err is None:
                        return vecs
                    # HTTP 200 但内容不对（空向量 / 维度不一 / NaN）：当失败重试——
                    # 静默收下会让向量和块错位、污染整个索引
                    last = f"响应校验失败: {err}"
            else:
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                # 4xx（鉴权/参数错）重试无意义，直接抛
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    break
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    logger.warning(f"[RAG] embedding 批失败: {last}")
    raise RuntimeError(f"embedding 失败: {last}")


def embed_query(text, **kw) -> list[float]:
    return embed_texts([text], **kw)[0]
