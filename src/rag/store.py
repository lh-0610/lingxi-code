"""Chroma 持久化向量库：cosine 检索 + metadata 过滤 + 按 chunk-hash 复用未变块的向量。

为什么从 numpy 换成 Chroma：78 块规模下 numpy 暴力点积本就够快，换库不为性能，而为
**metadata 过滤**（同一库里按 category 分域检索，见 index.py 的子目录分类）和主流选型的
可迁移性。原 numpy 实现的三项保证在这里都做了等价物，一项没丢：

  ① 块级增量复用 —— **chunk hash 直接当 Chroma 的 document ID**。「哪些块已 embed 过」
     退化成「哪些 ID 已存在」，不再需要单独的 vec_cache 文件。附赠内容去重：同 hash 只
     可能有一条记录，同一份资料的多个副本不会各占一个 top-k 名额。
  ② 事务性提交 —— Chroma 没有事务，用**三段式改名**补：新数据先写进 staging collection，
     提交时 main→old、staging→main、删 old。任一步崩溃后 load() 都能从残留状态推断出该
     回滚还是该前滚（见 _recover），绝不会出现「新向量 + 旧元数据」的混搭或索引凭空消失。
  ③ 索引锚 —— manifest 整体 json.dumps 后存进 collection metadata 的 lingxi_manifest 键
     （存字符串而非摊平成多个键：绕开 Chroma 的 metadata 取值类型限制与保留键前缀，
     且能原样往返）。

落盘：rag_index/chroma/（整个 Chroma PersistentClient 的数据目录，多个 store 共用一个
client，按 collection 名区分）。旧版 numpy 散文件索引（rag_index/<name>/*.npy）不兼容，
视为未建索引、要求重建。

读写全程持模块级 RLock（后台重建线程 vs agent 检索线程）。
"""
import hashlib
import json
import os
import re
import threading

import numpy as np

from ..paths import rag_index_dir, logger

# 串行化所有索引读写（UI 后台重建线程 / agent 检索线程 / 多 worker 并发检索）。
# RLock：count/search 内部会再入 load。
_LOCK = threading.RLock()

# Chroma collection 名约束：3-512 个 [a-zA-Z0-9._-]，且首尾必须是字母或数字。
# 固定的 kb_ 前缀 + _v1 后缀让任何 store 名（含 "t1" 这种 2 字符的）都自动合规。
_NAME_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9._-]")

# 每次 add 的批大小：远低于 Chroma 的单批上限，避免大库一次性打爆内存/请求体。
_ADD_BATCH = 1000

# path -> PersistentClient。同一数据目录必须复用同一个 client（重复构造在 Chroma 里
# 会因 settings 冲突报错），且测试用 set_data_dir 换目录时能各自拿到自己的 client。
_CLIENTS: dict[str, object] = {}

# 已提示过「旧版索引需重建」的目录：UI 每次刷新状态行都会新建 VectorStore，不去重的话
# 同一句告警会在一次会话里刷几十遍。重建之前状态不会变，说一次就够。
_LEGACY_WARNED: set[str] = set()


def text_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _valid_vec(v, dim=None) -> bool:
    """判定一个（复用/待提交）向量是否可用：非空一维数字序列、值全有限（无 nan/inf）、
    维度与期望 dim 一致（dim 给定时）。挡住污染索引的坏向量。"""
    if isinstance(v, np.ndarray):
        v = v.tolist()
    if not isinstance(v, (list, tuple)) or not v:
        return False
    if isinstance(dim, int) and len(v) != dim:
        return False
    try:
        arr = np.asarray(v, dtype=np.float32)
    except (TypeError, ValueError):
        return False
    return arr.ndim == 1 and bool(np.isfinite(arr).all())


def _client(path: str):
    """按数据目录缓存的 Chroma PersistentClient。"""
    with _LOCK:
        c = _CLIENTS.get(path)
        if c is None:
            import chromadb
            from chromadb.config import Settings
            os.makedirs(path, exist_ok=True)
            c = chromadb.PersistentClient(
                path=path,
                settings=Settings(anonymized_telemetry=False, allow_reset=False),
            )
            _CLIENTS[path] = c
        return c


class VectorStore:
    def __init__(self, name: str = "default"):
        self.name = name
        self.dir = os.path.join(rag_index_dir(), "chroma")   # Chroma client 数据目录
        self.metas: list[dict] = []      # 兼容字段：Chroma 下不再全量驻留，保持空
        self.manifest: dict = {}         # 索引锚（generation / kb_dir / embed_model / ...）
        self._col = None                 # 当前 collection 句柄（None = 未建索引）
        self._cache: dict[str, list[float]] | None = None   # 懒加载：hash -> 向量
        self._loaded = False

    # ── collection 命名 ──

    @property
    def _cname(self) -> str:
        return f"kb_{_NAME_UNSAFE_RE.sub('_', self.name)}_v1"

    @property
    def _staging_cname(self) -> str:
        return self._cname + "__staging"

    @property
    def _old_cname(self) -> str:
        return self._cname + "__old"

    def _legacy_dir(self) -> str:
        """旧版 numpy 索引目录（仅用于提示用户重建）。"""
        return os.path.join(rag_index_dir(), self.name)

    # ── 加载 ──

    def load(self, for_write: bool = False) -> "VectorStore":
        """加载当前 collection 与它的 manifest。

        错误分流（同 projects._load 的原则）：
          collection 不存在 → 正常的「未建索引」，按空返回并缓存结果。
          其它任何异常（数据目录损坏 / 占用 / Chroma 内部错）→ for_write=True 时**抛出**，
            绝不能把「读不出活动索引」当成空索引继续，否则 rebuild 会拿新数据顶掉一个其实
            完好的索引；纯读路径按空返回但**不缓存**（_loaded 不置 True），下次访问重试。
        """
        with _LOCK:
            if self._loaded:
                return self
            try:
                client = _client(self.dir)
                names = {c.name for c in client.list_collections()}
                names = self._recover(client, names)

                if self._cname not in names:
                    legacy = self._legacy_dir()
                    if (os.path.exists(os.path.join(legacy, "manifest.json"))
                            and legacy not in _LEGACY_WARNED):
                        _LEGACY_WARNED.add(legacy)
                        logger.warning(f"[RAG] 检测到旧版 numpy 索引格式（已换 Chroma 后端），"
                                       f"请重建索引；重建后 {legacy} 可删除")
                    self._loaded = True
                    return self

                col = client.get_collection(self._cname)
                manifest = {}
                raw = (col.metadata or {}).get("lingxi_manifest")
                if isinstance(raw, str) and raw:
                    try:
                        manifest = json.loads(raw) or {}
                    except (json.JSONDecodeError, TypeError, ValueError):
                        # manifest 读不出 → 锚校验会 fail-closed 要求重建（不当作可用索引）
                        logger.warning("[RAG] 索引 manifest 损坏，请重建索引")
                        manifest = {}
                self._col, self.manifest = col, manifest
                self._loaded = True
            except Exception as e:
                logger.warning(f"[RAG] 索引读取失败: {e}")
                if for_write:
                    raise   # 写路径中止：读不到活动索引就不许重建覆盖
                self._col, self.manifest, self._cache = None, {}, None
                # _loaded 保持 False → 纯读下次重试
            return self

    def _recover(self, client, names: set) -> set:
        """从上次 rebuild 中途崩溃的残留状态里恢复，返回恢复后的 collection 名集合。

        提交三段式是 main→old、staging→main、删 old，于是崩溃后只可能是三种状态：
          main 在  → 提交没开始（staging 是废数据）或已完成（old 是废数据）→ 清废数据。
          main 不在、old 在 → 崩在两次改名之间 → **回滚**：old 改回 main，弃掉 staging。
          main 不在、old 不在 → 干净的未建索引。
        """
        try:
            if self._cname in names:
                for garbage in (self._staging_cname, self._old_cname):
                    if garbage in names:
                        logger.info(f"[RAG] 清理上次重建的残留 collection: {garbage}")
                        client.delete_collection(garbage)
                        names = names - {garbage}
                return names
            if self._old_cname in names:
                logger.warning("[RAG] 检测到重建中途崩溃，回滚到上一份完整索引")
                if self._staging_cname in names:
                    client.delete_collection(self._staging_cname)
                    names = names - {self._staging_cname}
                client.get_collection(self._old_cname).modify(name=self._cname)
                names = (names - {self._old_cname}) | {self._cname}
        except Exception as e:
            # 恢复失败不该让检索整个挂掉：回落到「按当前实际状态处理」
            logger.warning(f"[RAG] 索引状态恢复失败: {e}")
        return names

    # ── 增量复用 ──

    def _ensure_cache(self) -> dict:
        """懒加载 hash -> 向量（只在重建时用到，检索路径不会付这个代价）。"""
        with _LOCK:
            if self._cache is not None:
                return self._cache
            cache: dict[str, list[float]] = {}
            self.load()
            if self._col is not None:
                try:
                    got = self._col.get(include=["embeddings"])
                    ids = got.get("ids") or []
                    embs = got.get("embeddings")
                    embs = [] if embs is None else list(embs)
                    dim = self.manifest.get("dim")
                    for i, cid in enumerate(ids):
                        if i >= len(embs):
                            break
                        v = embs[i]
                        if isinstance(v, np.ndarray):
                            v = v.tolist()
                        if _valid_vec(v, dim):
                            cache[cid] = v
                except Exception as e:
                    # 复用只是省 embedding 的优化层，取不到就全部重新 embed，不影响正确性
                    logger.warning(f"[RAG] 读取已有向量失败，将重新 embedding: {e}")
            self._cache = cache
            return cache

    def cached_vec(self, h: str):
        """已存在的（归一化）向量，用于重建时复用未变块；没有返回 None。"""
        with _LOCK:
            return self._ensure_cache().get(h)

    # ── 重建（staging + 改名提交）──

    def rebuild(self, metas: list[dict], vectors: list[list[float]], manifest: dict | None = None) -> None:
        """用全套 (metas, vectors) 重建索引。vectors 会被 L2 归一化。

        提交顺序 = 事务：① 全部数据写进 staging collection → ② main→old、staging→main
        （提交点）→ ③ 删 old。任何一步崩溃，load() 的 _recover 都能推断出一致状态。

        同 hash 的重复块只保留第一条（Chroma 的 ID 唯一性天然去重），避免同一份内容的
        多个副本在检索时挤占多个 top-k 名额。
        """
        if len(metas) != len(vectors):
            raise ValueError(f"metas({len(metas)}) 与 vectors({len(vectors)}) 数量不一致")
        with _LOCK:
            self.load(for_write=True)   # 读不到活动索引 → 抛出中止，绝不覆盖

            arr = np.asarray(vectors, dtype=np.float32) if vectors else np.zeros((0, 0), dtype=np.float32)
            if arr.size:
                # 提交前把关整块矩阵：形状必须二维、数值必须全有限。任何坏向量（embedding 返回
                # nan / 坏缓存漏网）在这里就中止——**绝不落盘**，否则会用坏索引顶掉有效旧索引
                # （重建"报成功"、检索却查不到）。抛出后 caller 保留旧索引。
                if arr.ndim != 2:
                    raise ValueError(f"向量矩阵必须二维，实际 ndim={arr.ndim}，拒绝提交（保留旧索引）")
                if not np.isfinite(arr).all():
                    raise ValueError("向量矩阵含 nan/inf，拒绝提交（保留旧索引）")
                norms = np.linalg.norm(arr, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                arr = arr / norms

            # 按 hash 去重（保留首次出现）：同一 ID 在一次 add 里重复会被 Chroma 拒绝，
            # 且重复内容本就不该各占一个检索名额。
            ids, docs, mds, embs = [], [], [], []
            seen: set[str] = set()
            for i, m in enumerate(metas):
                h = m.get("hash")
                if not h or h in seen:
                    continue
                seen.add(h)
                ids.append(h)
                docs.append(m.get("text", ""))
                mds.append({
                    "heading": m.get("heading", "") or "",
                    "source": m.get("source", "") or "",
                    "chunk_id": int(m.get("chunk_id", 0) or 0),
                    "category": m.get("category", "") or "default",
                    "hash": h,
                })
                embs.append(arr[i].tolist())
            dropped = len(metas) - len(ids)
            if dropped:
                logger.info(f"[RAG] 去重丢弃 {dropped} 个重复块（内容完全相同）")

            new_manifest = dict(manifest or {})
            new_manifest["generation"] = int(self.manifest.get("generation", 0)) + 1
            new_manifest["chunks"] = len(ids)
            if arr.size:
                new_manifest["dim"] = int(arr.shape[1])

            client = _client(self.dir)
            names = {c.name for c in client.list_collections()}
            _LEGACY_WARNED.discard(self._legacy_dir())   # 重建过了，旧格式提示重新生效

            # ① staging：先清掉可能的残留，再整份写进去（此刻 main 还是完整的旧索引）
            if self._staging_cname in names:
                client.delete_collection(self._staging_cname)
            staging = client.create_collection(
                self._staging_cname,
                metadata={"hnsw:space": "cosine", "lingxi_manifest": json.dumps(new_manifest, ensure_ascii=False)},
            )
            try:
                for s in range(0, len(ids), _ADD_BATCH):
                    e = s + _ADD_BATCH
                    staging.add(ids=ids[s:e], embeddings=embs[s:e],
                                documents=docs[s:e], metadatas=mds[s:e])
            except Exception:
                # 写 staging 失败 → 清掉半成品，main 原样保留
                try:
                    client.delete_collection(self._staging_cname)
                except Exception:
                    pass
                raise

            # ② 提交点：main→old、staging→main
            if self._old_cname in names:
                client.delete_collection(self._old_cname)
            had_main = self._cname in names
            if had_main:
                client.get_collection(self._cname).modify(name=self._old_cname)
            staging.modify(name=self._cname)

            # ③ 清理旧索引，失败不影响正确性（下次 load 的 _recover 会收拾）
            if had_main:
                try:
                    client.delete_collection(self._old_cname)
                except Exception as e:
                    logger.debug(f"[RAG] 清理旧 collection 失败（不影响使用）: {e}")

            self._col = client.get_collection(self._cname)
            self.manifest = new_manifest
            self._cache = {i: v for i, v in zip(ids, embs)}
            self._loaded = True

    # ── 检索 ──

    def search(self, query_vec: list[float], top_k: int = 5, where: dict | None = None) -> list[tuple[float, dict]]:
        """返回 [(cosine 相似度, meta), ...]，按相似度降序，至多 top_k 条。

        where: Chroma 的 metadata 过滤（如 {"category": "project"}），None = 不过滤。
        """
        with _LOCK:
            self.load()
            if self._col is None or top_k <= 0:
                return []
            n = self.count()
            if n == 0:
                return []
            q = np.asarray(query_vec, dtype=np.float32)
            norm = float(np.linalg.norm(q))
            if norm == 0 or not np.isfinite(norm):
                return []
            q = q / norm
            try:
                res = self._col.query(
                    query_embeddings=[q.tolist()],
                    n_results=min(top_k, n),
                    where=where or None,
                    include=["documents", "metadatas", "distances"],
                )
            except Exception as e:
                logger.warning(f"[RAG] 检索失败: {e}")
                return []
            ids = (res.get("ids") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]
            mds = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            out: list[tuple[float, dict]] = []
            for i, cid in enumerate(ids):
                md = dict(mds[i] or {}) if i < len(mds) else {}
                md["text"] = docs[i] if i < len(docs) else ""
                md.setdefault("hash", cid)
                # cosine 空间下 Chroma 返回的 distance = 1 - cosine 相似度
                d = float(dists[i]) if i < len(dists) else 1.0
                out.append((max(-1.0, min(1.0, 1.0 - d)), md))
            return out

    def count(self) -> int:
        with _LOCK:
            self.load()
            if self._col is None:
                return 0
            try:
                return int(self._col.count())
            except Exception as e:
                logger.warning(f"[RAG] 读取索引块数失败: {e}")
                return 0

    def categories(self) -> list[str]:
        """索引里出现过的所有分域名（供 UI / scope 参数校验）。"""
        with _LOCK:
            self.load()
            if self._col is None:
                return []
            try:
                got = self._col.get(include=["metadatas"])
                return sorted({(m or {}).get("category") or "default"
                               for m in (got.get("metadatas") or [])})
            except Exception as e:
                logger.warning(f"[RAG] 读取分域列表失败: {e}")
                return []
