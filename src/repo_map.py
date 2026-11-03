"""开工前的代码地图（学 Aider 的 repo map）：按这次任务挑出最相关的文件，列出里面的类 / 函数和行号。

为什么要有：DeepSeek 在大仓库里一轮只读一个文件，任务二（模型按显示名匹配）40 轮里 read_file 占了 25 次；
提示词要它批量读、派 explore 子代理，它都不用。这张地图不靠模型自觉——每次新任务开始时自动生成、
放进系统提示词，模型一开始就知道该看哪几个文件、哪几行。只做本地文本分析，不调模型、不花钱。

怎么挑：任务里的词（英文标识符拆开 + 中文连续 2~4 字）去匹配每个文件的路径、类 / 函数名和正文；
一个词在越少的文件里出现，命中它越值钱（IDF），所以"任务""self"这种到处都有的词几乎不加分，
中文描述也能对上代码里的中文注释。再沿引用图（code_graph.py：A 用到了 B 定义的名字）给高分文件的
上下游加一点分。类 / 函数清单来自 codeintel（tree-sitter，各语言通用；没有语法库的退回正则）。

任务词在代码里对不上（中文任务、英文代码又没中文注释；问"这个项目是做什么的"），就退一步给项目概览：
精简目录树 + 被引用最多的几个核心文件（PageRank），像 Gemini CLI 开局给目录结构一样。
"""
import math
import os
import re

from . import code_graph
from .codeintel import SOURCE_EXTENSIONS

SOURCE_EXTS = SOURCE_EXTENSIONS
MIN_SOURCE_FILES = 30      # 源码文件比这少就不生成：小项目模型自己翻得过来
MAP_MAX_FILES = 12
MAP_MAX_CHARS = 6000       # 约 2k token
SYMBOLS_PER_FILE = 20
# 最相关的文件都不到这个分：任务和代码对不上（闲聊、问概念），不给按任务挑的地图（改给项目概览）。
# 在灵犀仓库上实测：闲聊 / 仓库里没有的功能最高 8~9 分，真实任务 18~200 分
_MIN_TOP_SCORE = 15.0
_TEST_FILE_WEIGHT = 0.6    # 测试文件常常原样抄着任务里的话，打个折，别挤掉真正要改的源码
# 比这短的消息（"你好""继续"）不给项目概览：多半不是在交代任务
_OVERVIEW_MIN_CHARS = 6
_OVERVIEW_CORE_FILES = 8
_OVERVIEW_SYMBOLS = 8
_OVERVIEW_TOP_DIRS = 15
_OVERVIEW_SUB_DIRS = 6
_OVERVIEW_TREE_LINES = 45
# 地图里不列的符号：JS / TS 的顶层常量太多，挤掉真正的类和函数
_HIDDEN_KINDS = {"variable"}

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[一-鿿]{2,}")
_STOP = {"the", "and", "for", "with", "this", "that", "from", "into", "not", "are", "you", "has", "have",
         "def", "class", "self", "return", "import", "true", "false", "none", "py", "src", "test", "tests"}


def _split_ident(word: str):
    """snake_case / camelCase 拆成小写的词，连同整个标识符本身。"""
    parts = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", word.replace("_", " "))
    return {word.lower(), *(p.lower() for p in parts if len(p) >= 3)}


def task_terms(text: str):
    """任务描述里拿来匹配代码的词。返回 (英文词集合, [每段中文的 2~4 字子串集合, ...])。

    中文没法分词，就把每段连续中文切成 2~4 字的子串；但跨词的子串（"其次才""模型带不"）只会出现在
    原样抄了任务原话的地方，IDF 又高，叠加起来会把抄原话的测试文件顶到最前——所以每段中文
    只按它命中得最好的那一个子串计分（见 rank_files）。"""
    words = set()
    for word in _IDENT_RE.findall(text or ""):
        words |= _split_ident(word)
    runs = []
    for run in _CJK_RUN_RE.findall(text or ""):
        grams = {run[i:i + n] for n in (2, 3, 4) for i in range(len(run) - n + 1)}
        if grams:
            runs.append(grams)
    return {w for w in words if w not in _STOP}, runs


_TEST_NAME_RE = re.compile(r"(^test_|_test\.\w+$|Tests?\.\w+$|\.(test|spec)\.\w+$)")


def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return bool(_TEST_NAME_RE.search(name)) or "/tests/" in f"/{rel}" or "/src/test/" in f"/{rel}"


def _load(project_root: str):
    """({相对路径: 正文}, {相对路径: [(行号, 层级, 种类, 名字)]})。"""
    texts, raw = code_graph.scan(project_root)
    symbols = {rel: [(s["line"], s.get("depth", 0), s["kind"], s["name"]) for s in syms
                     if s["kind"] not in _HIDDEN_KINDS]
               for rel, syms in raw.items()}
    return texts, symbols


def _graph(texts, symbols):
    return code_graph.build_graph(texts, {rel: [(s[3], s[2]) for s in syms] for rel, syms in symbols.items()})


def rank_files(project_root: str, task_text: str):
    """给项目里每个源码文件按这次任务打分。返回 (按分数排好的 [(相对路径, 分数)], {相对路径: 符号}, 文件总数)；
    项目太小或任务里没有能匹配的词时返回 ([], {}, 文件数)。"""
    if not project_root or not os.path.isdir(project_root):
        return [], {}, 0
    texts, symbols = _load(project_root)
    ranked = _rank(texts, symbols, _graph(texts, symbols), task_text)
    return (ranked, symbols, len(texts)) if ranked is not None else ([], {}, len(texts))


def _rank(texts, symbols, edges, task_text):
    """[(相对路径, 分数)]；项目太小或任务里没有能匹配的词时返回 None。"""
    words, runs = task_terms(task_text)
    if len(texts) < MIN_SOURCE_FILES or not (words or runs):
        return None
    terms = words | set().union(*runs) if runs else set(words)

    # 每个词在多少个文件里出现过（路径、正文都算），算 IDF
    lowered = {rel: (rel.lower(), text.lower()) for rel, text in texts.items()}
    hits = {rel: {t for t in terms if t in path or t in body} for rel, (path, body) in lowered.items()}
    df = {}
    for found in hits.values():
        for t in found:
            df[t] = df.get(t, 0) + 1
    n = len(texts)
    idf = {t: math.log(1 + n / c) for t, c in df.items()}

    score = {}
    for rel in texts:
        syms = symbols[rel]
        path_l = lowered[rel][0]
        sym_words = set()
        for _ln, _lv, _kw, name in syms:
            sym_words |= _split_ident(name)
        s = 0.0
        for t in words & hits[rel]:
            w = idf[t]
            s += w                      # 正文里出现
            if t in path_l:
                s += 2 * w              # 文件名 / 目录名里就有
            if t in sym_words:
                s += 1.5 * w            # 是某个类 / 函数名的一部分
        for grams in runs:              # 每段中文只算命中得最好的那个子串，越长越罕见越值钱
            found = grams & hits[rel]
            if found:
                s += max(idf[g] * len(g) / 2 for g in found)
        if _is_test_file(rel):
            s *= _TEST_FILE_WEIGHT
        score[rel] = s

    # 沿引用图传一点分：高分文件用到的、用到高分文件的，通常也在这条链上。
    # 引用图比 import 密（同名就连），按这个文件最强的那条边归一，弱的顺带引用只沾一点
    boost = {}
    for a, outs in edges.items():
        strongest = max(outs.values())
        for b, w in outs.items():
            strength = w / strongest
            if score.get(a, 0) > 0:
                boost[b] = max(boost.get(b, 0.0), 0.25 * score[a] * strength)
            if score.get(b, 0) > 0:
                boost[a] = max(boost.get(a, 0.0), 0.1 * score[b] * strength)
    final = {rel: score[rel] + boost.get(rel, 0.0) for rel in texts}
    return sorted(((r, final[r]) for r in final if final[r] > 0), key=lambda x: (-x[1], x[0]))


def listed_files(map_text: str) -> int:
    """地图里列了几个文件（文件路径独占一行、不缩进；标题和说明行都带中文标点）。"""
    return sum(1 for ln in map_text.splitlines()
               if ln and not ln[0].isspace() and not ln.startswith("#")
               and not any(ch in ln for ch in "，。：（"))


def is_overview(map_text: str) -> bool:
    return map_text.startswith("# 代码地图（项目概览")


def build_task_map(project_root: str, task_text: str) -> str:
    """生成这次任务的代码地图；项目太小时返回空字符串。任务和代码对不上时给项目概览，
    消息太短（"你好""继续"）时什么都不给。"""
    if not project_root or not os.path.isdir(project_root):
        return ""
    texts, symbols = _load(project_root)
    if len(texts) < MIN_SOURCE_FILES:
        return ""
    edges = _graph(texts, symbols)
    ranked = _rank(texts, symbols, edges, task_text)
    if not ranked or ranked[0][1] < _MIN_TOP_SCORE:
        if len((task_text or "").strip()) < _OVERVIEW_MIN_CHARS:
            return ""
        return _overview(texts, symbols, edges)
    n = len(texts)
    top = ranked[0][1]
    words, _runs = task_terms(task_text)
    floor = 0.2 * top
    chosen = [r for r, s in ranked if s >= floor][:MAP_MAX_FILES]

    lines = ["# 代码地图（按这次任务自动挑的相关文件，供参考）",
             "按任务里的词匹配路径、类 / 函数名和注释挑出来的，越靠前越相关；先读这些文件，"
             "动手前核对要改的位置。这里没列的文件不代表无关。"]
    used = sum(len(x) + 1 for x in lines)
    shown = 0
    for rel in chosen:
        syms = symbols[rel]
        relevant = [s for s in syms if _split_ident(s[3]) & words]
        top_level = [s for s in syms if s[1] == 0]
        picked = sorted(set(relevant) | set(top_level[:SYMBOLS_PER_FILE]), key=lambda s: s[0])[:SYMBOLS_PER_FILE]
        block = [rel] + [f"  L{ln:<5d} {'  ' * min(level, 3)}{kw} {name}" for ln, level, kw, name in picked]
        if len(syms) > len(picked):
            block.append(f"  …（另有 {len(syms) - len(picked)} 个类 / 函数没列）")
        text = "\n".join(block)
        if used + len(text) > MAP_MAX_CHARS and shown:
            break
        lines.append(text)
        used += len(text) + 1
        shown += 1
    rest = len(ranked) - shown
    lines.append(f"（项目共 {n} 个源码文件；另有 {rest} 个文件也沾到了任务里的词，相关度更低没列出）"
                 if rest > 0 else f"（项目共 {n} 个源码文件）")
    return "\n".join(lines)


def _dir_tree(rels) -> list:
    """三层目录（总共不超过 _OVERVIEW_TREE_LINES 行），括号里是下面（含子目录）的源码文件数，文件多的在前。
    只有一个子目录、自己没文件的一串目录压成一行（Java 的 src/main/java/com/xx/），不然层数全耗在包路径上。"""
    root = {"count": 0, "files": 0, "dirs": {}}
    for rel in rels:
        parts = rel.split("/")
        node = root
        node["count"] += 1
        for part in parts[:-1]:
            node = node["dirs"].setdefault(part, {"count": 0, "files": 0, "dirs": {}})
            node["count"] += 1
        node["files"] += 1

    def collapse(name, node):
        while len(node["dirs"]) == 1 and not node["files"]:
            (child, node), = node["dirs"].items()
            name = f"{name}/{child}"
        return name, node

    def by_size(dirs):
        return sorted(dirs.items(), key=lambda x: (-x[1]["count"], x[0]))

    lines = []

    def render(dirs, level, limit):
        items = by_size(dirs)
        for name, node in items[:limit]:
            if len(lines) >= _OVERVIEW_TREE_LINES:
                return
            name, node = collapse(name, node)
            lines.append(f"{'  ' * (level + 1)}{name}/ ({node['count']})")
            if level < 2:
                render(node["dirs"], level + 1, _OVERVIEW_SUB_DIRS)
        if len(items) > limit and len(lines) < _OVERVIEW_TREE_LINES:
            lines.append(f"{'  ' * (level + 1)}…（另有 {len(items) - limit} 个目录）")

    render(root["dirs"], 0, _OVERVIEW_TOP_DIRS)
    if root["files"]:
        lines.append(f"  （根目录下 {root['files']} 个）")
    return lines


def _overview(texts, symbols, edges) -> str:
    """任务词对不上时的项目概览：目录结构 + 被引用最多的核心文件（PageRank）和它们的顶层类 / 函数。"""
    rank = code_graph.pagerank(edges, texts)
    core = [r for r in sorted(texts, key=lambda r: (-rank.get(r, 0.0), r))
            if not _is_test_file(r) and symbols.get(r)][:_OVERVIEW_CORE_FILES]
    lines = ["# 代码地图（项目概览：任务里的词在代码里没对上，先给目录结构和被引用最多的文件，供参考）",
             "按文件之间的引用关系挑的核心文件；要找具体功能，用 search_files 搜关键词、code_map 看某个目录。",
             "目录（括号里是源码文件数）：", *_dir_tree(texts), "核心文件（被别的文件引用得最多）："]
    used = sum(len(x) + 1 for x in lines)
    for rel in core:
        syms = symbols[rel]
        # 先列公开的（不以 _ 开头），再按行号排回去
        top_level = sorted(sorted((s for s in syms if s[1] == 0),
                                  key=lambda s: (s[3].startswith("_"), s[0]))[:_OVERVIEW_SYMBOLS])
        block = [rel] + [f"  L{ln:<5d} {kw} {name}" for ln, _lv, kw, name in top_level]
        if len(syms) > len(top_level):
            block.append(f"  …（另有 {len(syms) - len(top_level)} 个类 / 函数没列）")
        text = "\n".join(block)
        if used + len(text) > MAP_MAX_CHARS:
            break
        lines.append(text)
        used += len(text) + 1
    lines.append(f"（项目共 {len(texts)} 个源码文件）")
    return "\n".join(lines)
