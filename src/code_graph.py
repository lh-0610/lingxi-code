"""项目级代码结构：每个源码文件定义了哪些名字，又用到了别的文件定义的哪些名字，据此建文件之间的引用图。

不看 import 语句、不解析各语言的模块路径：A 文件里出现了 B 文件定义的类 / 函数名，就记一条 A→B
（学 Aider 的 repo map），所以对所有语言一样管用。名字撞车的噪声靠权重压：定义在很多文件里的
名字（render、update）不算，同名定义有几处就平分，越长越具体的名字越算数。

用途：代码地图给任务相关文件的上下游加分、任务词对不上时按"被引用多少"挑核心文件（PageRank）；
related_files 对没有 import 解析的语言（Java、Go、Rust……）给出上下游。
"""
import math
import os
import re
from collections import Counter

from . import codeintel
from .tools_common import _SEARCH_MAX_FILE_SIZE
from .tools_codemap import _is_noise_dir, _iter_project_files
from .workspace_anchor import _git

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_MAX_DEFINERS = 6           # 一个名字定义在超过这么多文件里就太泛了，不当引用
_SCAN_MAX_FILES = 5000      # 超大仓库只看前这么多个源码文件，别让每条消息都卡好几秒
_MEMBER_WEIGHT = 0.2        # 方法名的引用权重（见 build_graph）
_MIN_EDGE_WEIGHT = 0.3      # 比这弱的边不要：一个方法名用一次、只有一处定义是 0.2~0.4
# 到处都有的方法名：哪个文件定义了它们都说明不了谁依赖谁
_GENERIC_NAMES = {
    "main", "init", "__init__", "setup", "teardown", "run", "get", "set", "new", "add", "remove",
    "update", "render", "build", "create", "delete", "start", "stop", "close", "open", "load", "save",
    "read", "write", "parse", "handle", "process", "execute", "apply", "call", "test", "value", "data",
    "name", "type", "self", "this", "list", "dict", "map", "str", "len", "toString", "equals", "hashCode",
}

_symbol_cache: dict[str, tuple] = {}   # 绝对路径 → ((mtime_ns, 大小), 符号)
_SYMBOL_CACHE_MAX = 20000


def file_symbols(abs_path: str, text: str) -> list[dict]:
    """codeintel.file_symbols，按 mtime + 大小缓存：没改过的文件不重复解析。"""
    try:
        st = os.stat(abs_path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    hit = _symbol_cache.get(abs_path)
    if key is not None and hit and hit[0] == key:
        return hit[1]
    syms = codeintel.file_symbols(text, abs_path)
    if key is not None:
        if len(_symbol_cache) >= _SYMBOL_CACHE_MAX:
            _symbol_cache.clear()
        _symbol_cache[abs_path] = (key, syms)
    return syms


def project_files(root: str, exts):
    """项目里的源码文件 (绝对路径, 相对路径)。git 仓库按 git ls-files 列（跳过 .gitignore 里的
    tmp/、构建产物这类目录——灵犀自己的仓库里 tmp/ 下的测试临时文件曾经把概览占满），
    不是 git 仓库就遍历目录、跳过常见噪声目录。"""
    out, _err = _git(root, "ls-files", "-co", "--exclude-standard", "-z")
    if out is None:
        yield from _iter_project_files(root, exts)
        return
    for rel in sorted(set(out.split("\0"))):
        if not rel or not rel.lower().endswith(exts):
            continue
        parts = rel.split("/")
        if any(_is_noise_dir(p) for p in parts[:-1]):
            continue
        abs_path = os.path.join(root, *parts)
        try:
            if os.path.getsize(abs_path) > _SEARCH_MAX_FILE_SIZE:
                continue
        except OSError:          # 删了但还在索引里
            continue
        yield abs_path, rel


def scan(root: str):
    """读项目里的源码文件。返回 ({相对路径: 正文}, {相对路径: 符号})。"""
    texts, symbols = {}, {}
    for abs_path, rel in project_files(root, codeintel.SOURCE_EXTENSIONS):
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        texts[rel] = text
        symbols[rel] = file_symbols(abs_path, text)
        if len(texts) >= _SCAN_MAX_FILES:
            break
    return texts, symbols


def _short_name(name: str) -> str:
    """Foo::bar / M.b / ns.Foo → 最后一段，引用处一般只写这一段。"""
    return re.split(r"::|\.", name)[-1]


def _specificity(name: str) -> float:
    """loadUserProfile、parse_config_file 这种长名字几乎不会撞车，比 load 可信得多。"""
    if len(name) >= 8 or "_" in name.strip("_") or re.search(r"[a-z][A-Z]", name):
        return 2.0
    return 1.0


def build_graph(texts: dict, defs: dict) -> dict:
    """{A: {B: 权重}}：A 用到了 B 定义的名字。defs 是 {相对路径: [(名字, 种类), ...]}。

    方法名打两折：Java / C# 每个 service、controller 都有 getById、list 这类方法，同名到处撞；
    类型名、顶层函数才是"A 依赖 B"的硬信号。总权重太弱的边（只沾了一个常见方法名）丢掉。"""
    definers: dict[str, dict] = {}
    for rel, file_defs in defs.items():
        for raw, kind in file_defs:
            name = _short_name(raw)
            if len(name) < 3 or name in _GENERIC_NAMES:
                continue
            owners = definers.setdefault(name, {})
            owners[rel] = max(owners.get(rel, 0.0), _MEMBER_WEIGHT if kind == "method" else 1.0)
    definers = {n: owners for n, owners in definers.items() if len(owners) <= _MAX_DEFINERS}
    edges = {}
    for rel, text in texts.items():
        counts = Counter(t for t in _IDENT_RE.findall(text) if t in definers)
        out: dict[str, float] = {}
        for name, count in counts.items():
            owners = definers[name]
            if rel in owners:   # 自己也定义了这个名字：多半是在用自己的，不算到别人头上
                continue
            base = math.sqrt(count) * _specificity(name) / len(owners)
            for owner, kind_weight in owners.items():
                out[owner] = out.get(owner, 0.0) + base * kind_weight
        out = {b: w for b, w in out.items() if w >= _MIN_EDGE_WEIGHT}
        if out:
            edges[rel] = out
    return edges


def defs_of(symbols: dict) -> dict:
    """codeintel 的符号（dict）→ build_graph 要的 {相对路径: [(名字, 种类)]}。"""
    return {rel: [(s["name"], s["kind"]) for s in syms] for rel, syms in symbols.items()}


def pagerank(edges: dict, nodes, personalization: dict | None = None,
             damping: float = 0.85, iterations: int = 30) -> dict:
    """带权 PageRank：被引用得多、被重要文件引用的文件分高。personalization 给了就偏向这些文件。"""
    nodes = list(nodes)
    if not nodes:
        return {}
    if personalization:
        total = sum(max(0.0, personalization.get(v, 0.0)) for v in nodes)
    else:
        total = 0.0
    if total > 0:
        p = {v: max(0.0, personalization.get(v, 0.0)) / total for v in nodes}
    else:
        p = {v: 1.0 / len(nodes) for v in nodes}
    out_sum = {a: sum(ws.values()) for a, ws in edges.items()}
    rank = dict(p)
    for _ in range(iterations):
        dangling = sum(rank[v] for v in nodes if not out_sum.get(v))
        new = {v: (1 - damping) * p[v] + damping * dangling * p[v] for v in nodes}
        for a, ws in edges.items():
            ra = rank.get(a, 0.0)
            if not ra:
                continue
            share = damping * ra / out_sum[a]
            for b, w in ws.items():
                if b in new:
                    new[b] += share * w
        delta = sum(abs(new[v] - rank[v]) for v in nodes)
        rank = new
        if delta < 1e-6:
            break
    return rank


def neighbors(edges: dict, rel: str):
    """(它用到的文件, 用到它的文件)，各自按引用强度从高到低。"""
    uses = sorted(edges.get(rel, {}).items(), key=lambda x: (-x[1], x[0]))
    used_by = sorted(((a, ws[rel]) for a, ws in edges.items() if rel in ws), key=lambda x: (-x[1], x[0]))
    return [r for r, _ in uses], [r for r, _ in used_by]
