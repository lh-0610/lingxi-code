"""numpy 持久化向量库：暴力 cosine 检索 + 按 text-hash 缓存复用未变块的向量。

为什么不上 chromadb/faiss：本机 Python 3.14 太新，重型向量库（及其 onnxruntime 等
二进制依赖）多半没有 3.14 wheel、硬装就崩；而单用户 md 知识库规模（几百~几千块）下，
numpy 暴力 cosine 亚毫秒级，零重依赖、保证能跑。store 抽象后端可换。

事务性（generation 方案）：每次重建写一代【带版本号】的数据文件，最后**原子替换
manifest** 作为提交点——load 只认 manifest 指向的那一代，所以进程中途退出最多留下
一堆没被引用的新代文件（下次重建清理），绝不会出现"新向量 + 旧元数据"的混搭
（分文件逐个替换的方案在块数恰好相同时无法靠长度校验发现错位）。

落盘（rag_index/<name>/）：
  manifest.json          —— {generation, kb_dir, embed_model, embed_base_url, chunks, dim}
                            索引锚 + 提交点：原子写、最后写；load 先读它
  embeddings.<gen>.npy   —— float32 [N, dim]，已 L2 归一化（检索时点积即 cosine）
  meta.<gen>.jsonl       —— N 行，每行 {text, heading, source, chunk_id, hash}
  vec_cache.<gen>.jsonl  —— {h: text_hash, v: 归一化向量}，重建时复用未变块、免重复 embed

旧版（无 generation 的散文件）索引不兼容：视为未建索引、要求重建（一次性成本，
换来确定的一致性）。读写/重建全程持模块级 RLock（后台重建线程 vs agent 检索线程）。
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

_GEN_FILE_RE = re.compile(r"^(embeddings|meta|vec_cache)\.(\d+)\.(npy|jsonl)$")


def text_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _valid_vec(v, dim=None) -> bool:
    """判定一个（缓存/待提交）向量是否可用：非空一维数字序列、值全有限（无 nan/inf）、
    维度与期望 dim 一致（dim 给定时）。挡住污染索引的坏缓存/坏 embedding。"""
    if not isinstance(v, (list, tuple)) or not v:
        return False
    if isinstance(dim, int) and len(v) != dim:
        return False
    try:
        arr = np.asarray(v, dtype=np.float32)
    except (TypeError, ValueError):
        return False
    return arr.ndim == 1 and bool(np.isfinite(arr).all())


class VectorStore:
    def __init__(self, name: str = "default"):
        self.name = name
        self.dir = os.path.join(rag_index_dir(), name)
        self.vecs = None            # np.ndarray [N, dim] float32（已归一化）
        self.metas: list[dict] = []
        self.manifest: dict = {}    # 索引锚（generation / kb_dir / embed_model / ...）
        self._cache: dict[str, list[float]] = {}   # text_hash -> 归一化向量
        self._loaded = False

    # ── 路径 ──

    def _manifest_path(self):
        return os.path.join(self.dir, "manifest.json")

    def _vec_path(self, gen: int):
        return os.path.join(self.dir, f"embeddings.{gen}.npy")

    def _meta_path(self, gen: int):
        return os.path.join(self.dir, f"meta.{gen}.jsonl")

    def _cache_path(self, gen: int):
        return os.path.join(self.dir, f"vec_cache.{gen}.jsonl")

    # ── 加载 ──

    def load(self, for_write: bool = False) -> "VectorStore":
        """加载 manifest 指向的那一代。

        错误分流（同 projects._load 的原则）：
          瞬时 I/O 错（PermissionError/占用等 OSError）→ for_write=True 时**抛出**——
            绝不能把 generation 当 0 继续，否则 rebuild 会提交新 manifest 覆盖活动代；
            纯读路径按空返回但**不缓存**（_loaded 不置 True），下次访问重试。
          损坏（JSON 错/结构错）→ 按空处理（重建可修复），缓存结果。
        """
        with _LOCK:
            if self._loaded:
                return self
            try:
                if not os.path.exists(self._manifest_path()):
                    # 无 manifest：全新目录，或旧版散文件格式（不兼容，按未建索引处理）
                    if os.path.exists(os.path.join(self.dir, "meta.jsonl")):
                        logger.warning("[RAG] 检测到旧版索引格式（无 generation），请重建索引")
                    self._loaded = True
                    return self
                with open(self._manifest_path(), encoding="utf-8") as f:
                    manifest = json.load(f) or {}
                gen = manifest.get("generation")
                if not isinstance(gen, int):
                    logger.warning("[RAG] manifest 缺 generation（旧版索引），请重建索引")
                    self._loaded = True
                    return self

                metas: list[dict] = []
                if os.path.exists(self._meta_path(gen)):
                    with open(self._meta_path(gen), encoding="utf-8") as f:
                        metas = [json.loads(ln) for ln in f if ln.strip()]
                vecs = None
                if os.path.exists(self._vec_path(gen)):
                    vecs = np.load(self._vec_path(gen))
                cache: dict[str, list[float]] = {}
                _cache_dim = manifest.get("dim")
                _dropped = 0
                if os.path.exists(self._cache_path(gen)):
                    # vec_cache 只是省 embedding 的优化层，不是可检索主数据。它无论是 I/O
                    # 失败、JSON 截断还是结构损坏，都只丢缓存；绝不能把完整 vec/meta 一起判空。
                    try:
                        with open(self._cache_path(gen), encoding="utf-8") as f:
                            for ln in f:
                                if not ln.strip():
                                    continue
                                try:
                                    o = json.loads(ln)
                                    h = o.get("h") if isinstance(o, dict) else None
                                    v = o.get("v") if isinstance(o, dict) else None
                                    if isinstance(h, str) and h and _valid_vec(v, _cache_dim):
                                        cache[h] = v
                                    else:
                                        _dropped += 1
                                except (json.JSONDecodeError, TypeError, ValueError):
                                    _dropped += 1
                    except OSError as e:
                        logger.warning(f"[RAG] 向量缓存读取失败，忽略缓存并重新 embedding: {e}")
                if _dropped:
                    logger.warning(f"[RAG] 丢弃 {_dropped} 条损坏的缓存向量（将重新 embedding）")

                # 一致性校验（防御纵深；generation 方案下正常不会走到）。数量、形状、维度、
                # 数值都要过——尤其防「一维 .npy 恰好长度==meta 数」蒙混过数量校验，
                # 之后 search 里 self.vecs @ q 退化成标量、len(sims) 抛 TypeError。
                n_vec = 0 if vecs is None else (vecs.shape[0] if vecs.size else 0)
                bad = ""
                if n_vec != len(metas) or len(metas) != manifest.get("chunks", len(metas)):
                    bad = (f"数量不一致（向量 {n_vec} / meta {len(metas)} / "
                           f"manifest {manifest.get('chunks')}）")
                elif vecs is not None and vecs.size:
                    dim = manifest.get("dim")
                    if vecs.ndim != 2:
                        bad = f"向量不是二维（ndim={vecs.ndim}）"
                    elif isinstance(dim, int) and vecs.shape[1] != dim:
                        bad = f"向量维度与 manifest 不符（{vecs.shape[1]} != {dim}）"
                    elif not np.isfinite(vecs).all():
                        bad = "向量含 nan/inf"
                if bad:
                    logger.warning(f"[RAG] 索引第 {gen} 代损坏（{bad}），按损坏处理，请重建索引")
                    self._loaded = True
                    return self
                self.vecs, self.metas, self.manifest, self._cache = vecs, metas, manifest, cache
                self._loaded = True
            except OSError as e:
                logger.warning(f"[RAG] 索引读取失败（瞬时 I/O 错）: {e}")
                if for_write:
                    raise   # 写路径中止：不能在读不到活动代的情况下重建/覆盖
                self.vecs, self.metas, self.manifest, self._cache = None, [], {}, {}
                # _loaded 保持 False → 纯读下次重试
            except Exception as e:
                logger.warning(f"[RAG] 索引损坏，按空处理: {e}")
                self.vecs, self.metas, self.manifest, self._cache = None, [], {}, {}
                self._loaded = True
            return self

    def _max_gen_on_disk(self) -> int:
        """目录里现存数据文件的最大代号（含未被 manifest 引用的残留），无文件返回 0。"""
        best = 0
        try:
            for fn in os.listdir(self.dir):
                m = _GEN_FILE_RE.match(fn)
                if m:
                    best = max(best, int(m.group(2)))
        except OSError:
            pass
        return best

    def cached_vec(self, h: str):
        """已缓存的（归一化）向量，用于重建时复用未变块；没有返回 None。"""
        with _LOCK:
            return self._cache.get(h)

    # ── 重建（generation 提交）──

    def rebuild(self, metas: list[dict], vectors: list[list[float]], manifest: dict | None = None) -> None:
        """用全套 (metas, vectors) 重建索引并落盘。vectors 会被 L2 归一化。

        写盘顺序 = 事务：① 完整写出【新一代】的三个数据文件 → ② 原子替换 manifest
        指向新代（提交点）→ ③ 清理旧代文件。任何一步中途崩溃，manifest 仍指向完整
        的旧代，load 读到的永远是一致快照。
        """
        if len(metas) != len(vectors):
            raise ValueError(f"metas({len(metas)}) 与 vectors({len(vectors)}) 数量不一致")
        with _LOCK:
            self.load(for_write=True)   # 瞬时 I/O 错 → 抛出中止，绝不覆盖活动代
            os.makedirs(self.dir, exist_ok=True)
            # 新代号取 max(manifest 代, 磁盘现存最大代)+1：即使 manifest 读出异常/有
            # 上次崩溃残留的更高代文件，也保证唯一、绝不与任何现存代冲突
            gen = max(int(self.manifest.get("generation", 0)), self._max_gen_on_disk()) + 1

            arr = np.asarray(vectors, dtype=np.float32) if vectors else np.zeros((0, 0), dtype=np.float32)
            if arr.size:
                # 提交前把关整块矩阵：形状必须二维、数值必须全有限。任何坏向量（embedding 返回
                # nan/坏缓存漏网）在这里就中止——**绝不落盘、绝不清理旧代**，否则会用 0 块索引
                # 顶替掉有效旧代（重建"报成功"、检索却查不到）。抛出后 caller 保留旧索引。
                if arr.ndim != 2:
                    raise ValueError(f"向量矩阵必须二维，实际 ndim={arr.ndim}，拒绝提交（保留旧索引）")
                if not np.isfinite(arr).all():
                    raise ValueError("向量矩阵含 nan/inf，拒绝提交（保留旧索引）")
                norms = np.linalg.norm(arr, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                arr = arr / norms
            cache = {m["hash"]: arr[i].tolist() for i, m in enumerate(metas)} if arr.size else {}

            # ① 新一代数据文件（此刻 manifest 还指向旧代，写一半崩了也不影响读）
            if arr.size:
                np.save(self._vec_path(gen), arr)   # 路径以 .npy 结尾，np.save 不再追加后缀
            with open(self._meta_path(gen), "w", encoding="utf-8") as f:
                for m in metas:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
            with open(self._cache_path(gen), "w", encoding="utf-8") as f:
                for h, v in cache.items():
                    f.write(json.dumps({"h": h, "v": v}) + "\n")

            # ② 提交点：原子替换 manifest
            new_manifest = dict(manifest or {})
            new_manifest["generation"] = gen
            new_manifest["chunks"] = len(metas)
            if arr.size:
                new_manifest["dim"] = int(arr.shape[1])
            tmp = self._manifest_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(new_manifest, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._manifest_path())

            self.vecs, self.metas, self.manifest, self._cache = arr, list(metas), new_manifest, cache
            self._loaded = True

            # ③ 清理非当前代的数据文件（含旧版散文件），失败不影响正确性
            self._cleanup_other_generations(gen)

    def _cleanup_other_generations(self, keep_gen: int) -> None:
        try:
            for fn in os.listdir(self.dir):
                m = _GEN_FILE_RE.match(fn)
                if m and int(m.group(2)) != keep_gen:
                    os.remove(os.path.join(self.dir, fn))
                elif fn in ("embeddings.npy", "meta.jsonl", "vec_cache.jsonl"):
                    os.remove(os.path.join(self.dir, fn))   # 旧版散文件
        except OSError as e:
            logger.debug(f"[RAG] 清理旧代索引文件失败（不影响使用）: {e}")

    # ── 检索 ──

    def search(self, query_vec: list[float], top_k: int = 5) -> list[tuple[float, dict]]:
        """返回 [(cosine 相似度, meta), ...]，按相似度降序，至多 top_k 条。"""
        with _LOCK:
            self.load()
            if self.vecs is None or not len(self.metas) or not self.vecs.size:
                return []
            q = np.asarray(query_vec, dtype=np.float32)
            n = float(np.linalg.norm(q))
            if n == 0:
                return []
            q = q / n
            sims = self.vecs @ q                      # 两边都归一化 → 点积即 cosine
            k = min(top_k, len(sims))
            top = np.argpartition(-sims, k - 1)[:k]
            top = top[np.argsort(-sims[top])]
            return [(float(sims[i]), self.metas[i]) for i in top]

    def count(self) -> int:
        with _LOCK:
            self.load()
            return len(self.metas)
