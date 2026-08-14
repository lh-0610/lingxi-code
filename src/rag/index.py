"""摄取 kb_dir 下所有 .md / .pdf → 切块 → embed → 建索引。

- 块级增量：按「标题路径 + 正文」的 hash 复用上次已 embed 的向量，只对新增/改动的块
  调 embedding API（省钱、快）。删掉的块在 rebuild 时自然从索引里消失。
- 缓存失效：换 embedding 模型/端点后旧向量不可比 → manifest 里的 embed_model/base_url
  与当前不一致时**整库重新 embed**，绝不复用异空间向量。
- 安全：任一文件读取失败 → **中止重建、保留旧索引**（否则唯一文件读失败会把索引清空）。
- manifest：随索引写入 {kb_dir, embed_model, embed_base_url, chunk_size, chunk_overlap}，
  检索/状态检查时校验（换目录/换模型/改切块参数都必须先重建，防"切了新目录还检索出
  旧库内容"或"改了切块粒度却还用旧切片"）。

分域（category）：**顶层子目录名**即分域名，直接放在 kb_dir 根下的文件归入 "default"。

    knowledge_base/
      project/   → category="project"
      learning/  → category="learning"
      x.md       → category="default"

分域随每块写进 Chroma metadata，检索时按域配额取（retriever.retrieve 的 scope/quota）。
这是为了解决「同一个库里放了两批高度同题材的资料」时的相互挤占：比如项目实现文档与
通用学习资料都在讲 MCP/RAG/Agent 主循环，标题几乎一样（而标题会拼进 embedding 文本），
向量空间里天然难分——不分域的话 top-k 会被其中一批占满，模型据此把别人的做法当成你的。
"""
import os

from .chunk import iter_markdown_chunks, iter_plain_chunks
from .embed import embed_texts
from .store import VectorStore, text_hash
from ..limits import (
    RAG_MAX_SOURCE_FILE_BYTES,
    RAG_MAX_TOTAL_CHUNKS,
    RAG_MAX_TOTAL_SOURCE_BYTES,
    RAG_MIN_CHUNK_SIZE,
)
from ..paths import logger

MD_EXTS = (".md", ".markdown")
PDF_EXTS = (".pdf",)
SUPPORTED_EXTS = MD_EXTS + PDF_EXTS

DEFAULT_CATEGORY = "default"


def norm_kb_dir(kb_dir: str) -> str:
    """kb_dir 的规范化形态（manifest 存储/比较用）：绝对路径 + normcase。"""
    return os.path.normcase(os.path.normpath(os.path.abspath(kb_dir)))


def norm_url(u: str) -> str:
    """embedding/rerank 端点的规范化（manifest 存储/比较用）：去首尾空白 + 去尾斜杠。
    https://x/v1 与 https://x/v1/ 是同一端点，不做规范化会被误判"端点变了"→ 整库重刷。"""
    return (u or "").strip().rstrip("/")


def _iter_sources(kb_dir: str):
    for root, dirs, files in os.walk(kb_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]   # 跳过 .git 等
        for fn in files:
            if fn.lower().endswith(SUPPORTED_EXTS):
                yield os.path.join(root, fn)


def category_of(rel: str) -> str:
    """相对路径（正斜杠）→ 分域名：顶层子目录名，根目录下的文件归 DEFAULT_CATEGORY。"""
    head, _, tail = rel.partition("/")
    return head if tail else DEFAULT_CATEGORY


def _read_pdf_pages(path: str) -> list[str]:
    """PDF → 每页一段文本。pypdf 缺失/解析失败都抛异常，由调用方并入 read_errors 中止重建
    ——静默跳过会让「放进来却没被索引」无声发生，比报错更难发现。"""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError(
            f"知识库含 PDF（{os.path.basename(path)}）但未安装 pypdf，"
            "请先 pip install pypdf（或把 PDF 移出知识库目录）") from e
    reader = PdfReader(path)
    return [(page.extract_text() or "") for page in reader.pages]


def _chunks_of(path: str, rel: str, chunk_size: int, chunk_overlap: int):
    """按后缀分派切块。Markdown 走标题分层切；PDF 按页切、heading 填「第 N 页」
    （引用时能定位到页码），且**不做 Markdown 解析**——正文里的 # 和 ``` 只是普通字符。"""
    if path.lower().endswith(PDF_EXTS):
        cid = 0
        for i, page_text in enumerate(_read_pdf_pages(path), 1):
            for ch in iter_plain_chunks(page_text, rel, f"第 {i} 页",
                                        chunk_size, chunk_overlap, start_id=cid):
                cid = ch["chunk_id"] + 1
                yield ch
        return
    with open(path, encoding="utf-8") as f:
        text = f.read()
    yield from iter_markdown_chunks(text, rel, chunk_size, chunk_overlap)


def reindex(kb_dir: str, *, embed_model, embed_base_url, embed_api_key,
            chunk_size, chunk_overlap, name="default") -> dict:
    """重建 name 索引。返回统计 {files, chunks, embedded, reused}。

    抛异常（读文件失败 / embedding 失败）时**不落盘**，旧索引保持原样。
    """
    if not kb_dir or not os.path.isdir(kb_dir):
        raise ValueError(f"知识库目录无效: {kb_dir!r}")
    if chunk_size < RAG_MIN_CHUNK_SIZE or not (0 <= chunk_overlap < chunk_size):
        raise ValueError(f"非法切块参数: chunk_size={chunk_size}, chunk_overlap={chunk_overlap}"
                         f"（要求 chunk_size>={RAG_MIN_CHUNK_SIZE} 且 0<=overlap<chunk_size；"
                         "过小的 chunk_size 会产生海量切片、天量 embedding 请求）")

    # 写路径加载：瞬时 I/O 错在这里就中止（还没花 embedding 钱），旧索引原样保留
    store = VectorStore(name).load(for_write=True)

    # 换模型/端点 → 旧缓存向量在异空间，不可复用（否则"切模型后 embedded:0"却比不对）。
    # 端点用规范化比较：/v1 与 /v1/ 是同一端点，不能触发整库重刷。
    _mf = store.manifest
    cache_ok = (_mf.get("embed_model") == embed_model
                and norm_url(_mf.get("embed_base_url", "")) == norm_url(embed_base_url))
    if not cache_ok and store.count():
        logger.info(f"[RAG] embedding 模型/端点变更（{_mf.get('embed_model')} → {embed_model}），"
                    "整库重新 embed")

    # 1. 切块（读失败即中止——只跳过失败文件会把"读不出来"当"文件没了"，清掉它的索引）
    all_chunks: list[dict] = []
    read_errors: list[str] = []
    total_source_bytes = 0
    for path in _iter_sources(kb_dir):
        try:
            source_bytes = os.path.getsize(path)
        except Exception as e:
            read_errors.append(f"{path}: {e}")
            continue
        if source_bytes > RAG_MAX_SOURCE_FILE_BYTES:
            raise RuntimeError(
                f"知识库文件 {path} 大小 {source_bytes} 字节，超过单文件上限 "
                f"{RAG_MAX_SOURCE_FILE_BYTES}，已中止（旧索引保留）")
        total_source_bytes += source_bytes
        if total_source_bytes > RAG_MAX_TOTAL_SOURCE_BYTES:
            raise RuntimeError(
                f"知识库源文件总大小超过上限 {RAG_MAX_TOTAL_SOURCE_BYTES} 字节，已中止（旧索引保留）")
        rel = os.path.relpath(path, kb_dir).replace("\\", "/")
        category = category_of(rel)
        # 手动驱动切块迭代器：读文件/解析 PDF 的失败要并进 read_errors（中止重建），而
        # 我们自己抛的「切片数超上限」必须原样冒泡——所以上限检查放在 try 之外，不会被误吞。
        chunks = _chunks_of(path, rel, chunk_size, chunk_overlap)
        while True:
            try:
                ch = next(chunks)
            except StopIteration:
                break
            except Exception as e:
                read_errors.append(f"{path}: {e}")
                break
            # 即时卡上限：不能等全部切完再 len()，否则保护触发前 all_chunks 自己就可能耗尽内存。
            if len(all_chunks) >= RAG_MAX_TOTAL_CHUNKS:
                raise RuntimeError(
                    f"知识库切片数超过上限 {RAG_MAX_TOTAL_CHUNKS}，已中止（旧索引保留）——"
                    "请增大 chunk_size 或缩减知识库目录后重试，以免产生天量 embedding 请求。")
            embed_text = f"{ch['heading']}\n\n{ch['text']}" if ch["heading"] else ch["text"]
            ch["hash"] = text_hash(embed_text)
            ch["_embed_text"] = embed_text
            ch["category"] = category
            all_chunks.append(ch)
    if read_errors:
        raise RuntimeError(
            f"{len(read_errors)} 个文件读取失败，中止重建（旧索引保留）：\n" + "\n".join(read_errors[:5]))

    manifest = {"kb_dir": norm_kb_dir(kb_dir), "embed_model": embed_model,
                "embed_base_url": norm_url(embed_base_url),
                "chunk_size": chunk_size, "chunk_overlap": chunk_overlap}

    if not all_chunks:
        store.rebuild([], [], manifest=manifest)
        return {"files": 0, "chunks": 0, "embedded": 0, "reused": 0}

    # 2. 只 embed 缓存里没有的块（新增/改动）；换模型后 cache_ok=False → 全部重 embed
    fresh: dict[str, list[float]] = {}
    need = [i for i, c in enumerate(all_chunks)
            if not cache_ok or store.cached_vec(c["hash"]) is None]
    if need:
        texts = [all_chunks[i]["_embed_text"] for i in need]
        vecs = embed_texts(texts, model=embed_model, base_url=embed_base_url, api_key=embed_api_key)
        for j, i in enumerate(need):
            fresh[all_chunks[i]["hash"]] = vecs[j]

    # 3. 组装完整 (metas, vectors)，重建索引（rebuild 统一归一化 + 重置缓存 + 写 manifest）
    metas, vectors = [], []
    for c in all_chunks:
        metas.append({"text": c["text"], "heading": c["heading"], "source": c["source"],
                      "chunk_id": c["chunk_id"], "hash": c["hash"],
                      "category": c.get("category", DEFAULT_CATEGORY)})
        v = fresh.get(c["hash"])
        if v is None:
            v = store.cached_vec(c["hash"])
        if v is None:   # 不应发生（need 已覆盖），防御性中止防错位
            raise RuntimeError(f"内部错误：块 {c['source']}#{c['chunk_id']} 缺向量，中止重建")
        vectors.append(v)
    store.rebuild(metas, vectors, manifest=manifest)

    files = len({c["source"] for c in all_chunks})
    # chunks 报**实际入库**数（store.rebuild 会按 hash 去重），不报切出来的原始块数——
    # 否则「切了 100 块、库里只有 80 块」会被当成索引缺失去排查。
    indexed = int(store.manifest.get("chunks", len(all_chunks)))
    cats: dict[str, int] = {}
    for c in all_chunks:
        cats[c.get("category", DEFAULT_CATEGORY)] = cats.get(c.get("category", DEFAULT_CATEGORY), 0) + 1
    stats = {"files": files, "chunks": indexed, "embedded": len(need),
             "reused": len(all_chunks) - len(need),
             "duplicates": len(all_chunks) - indexed, "categories": cats}
    logger.info(f"[RAG] 索引重建完成: {stats}")
    return stats
