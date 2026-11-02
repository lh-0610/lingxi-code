"""开工前的代码地图（学 Aider 的 repo map）：按这次任务挑出最相关的文件，列出里面的类 / 函数和行号。

为什么要有：DeepSeek 在大仓库里一轮只读一个文件，任务二（模型按显示名匹配）40 轮里 read_file 占了 25 次；
提示词要它批量读、派 explore 子代理，它都不用。这张地图不靠模型自觉——每次新任务开始时自动生成、
放进系统提示词，模型一开始就知道该看哪几个文件、哪几行。只做本地文本分析，不调模型、不花钱。

怎么挑：任务里的词（英文标识符拆开 + 中文连续 2~4 字）去匹配每个文件的路径、类 / 函数名和正文；
一个词在越少的文件里出现，命中它越值钱（IDF），所以"任务""self"这种到处都有的词几乎不加分，
中文描述也能对上代码里的中文注释。再沿 Python 的 import 关系给被高分文件引用 / 引用它们的文件加一点分。
"""
import math
import os
import re

from .tools_codemap import _extract_imports_py, _iter_project_files, _module_name_for_py, _module_to_path

SOURCE_EXTS = (".py", ".js", ".ts", ".jsx", ".tsx")
MIN_SOURCE_FILES = 40      # 源码文件比这少就不生成：小项目模型自己翻得过来
MAP_MAX_FILES = 12
MAP_MAX_CHARS = 6000       # 约 2k token
SYMBOLS_PER_FILE = 20
# 最相关的文件都不到这个分：任务和代码对不上（闲聊、问概念），不给地图。
# 在灵犀仓库上实测：闲聊 / 仓库里没有的功能最高 8~9 分，真实任务 18~200 分
_MIN_TOP_SCORE = 15.0
_TEST_FILE_WEIGHT = 0.6    # 测试文件常常原样抄着任务里的话，打个折，别挤掉真正要改的源码

_SYMBOL_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:export\s+)?(?:async\s+)?(?P<kw>def|class|function)\s+(?P<name>[A-Za-z_]\w*)")
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


def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return (name.startswith("test_") or name.endswith(("_test.py", ".test.ts", ".test.js", ".spec.ts", ".spec.js"))
            or "/tests/" in f"/{rel}")


def _symbols(lines):
    out = []
    for i, line in enumerate(lines, 1):
        m = _SYMBOL_RE.match(line)
        if m:
            out.append((i, len(m.group("indent").expandtabs(4)) // 4, m.group("kw"), m.group("name")))
    return out


def rank_files(project_root: str, task_text: str):
    """给项目里每个源码文件按这次任务打分。返回 (按分数排好的 [(相对路径, 分数)], {相对路径: 符号}, 文件总数)；
    项目太小或任务里没有能匹配的词时返回 ([], {}, 文件数)。"""
    words, runs = task_terms(task_text)
    if not project_root or not os.path.isdir(project_root) or not (words or runs):
        return [], {}, 0
    terms = words | set().union(*runs) if runs else set(words)
    files = {}
    for abs_path, rel in _iter_project_files(project_root, SOURCE_EXTS):
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        files[rel] = (abs_path, text)
    if len(files) < MIN_SOURCE_FILES:
        return [], {}, len(files)

    # 每个词在多少个文件里出现过（路径、正文都算），算 IDF
    lowered = {rel: (rel.lower(), text.lower()) for rel, (_a, text) in files.items()}
    hits = {rel: {t for t in terms if t in path or t in body} for rel, (path, body) in lowered.items()}
    df = {}
    for found in hits.values():
        for t in found:
            df[t] = df.get(t, 0) + 1
    n = len(files)
    idf = {t: math.log(1 + n / c) for t, c in df.items()}

    symbols, score = {}, {}
    for rel, (abs_path, text) in files.items():
        syms = _symbols(text.splitlines())
        symbols[rel] = syms
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

    # 沿 import 关系传一点分：高分文件引用的、引用高分文件的，通常也在这条链上
    boost = {}
    for rel, (abs_path, _text) in files.items():
        if not rel.endswith(".py") or score[rel] <= 0:
            continue
        try:
            imported = _extract_imports_py(abs_path, _module_name_for_py(abs_path, project_root))
        except Exception:
            continue
        for mod in imported:
            target = _module_to_path(mod, project_root)
            if not target:
                continue
            trel = os.path.relpath(target, project_root).replace(os.sep, "/")
            if trel in score and trel != rel:
                boost[trel] = max(boost.get(trel, 0.0), 0.25 * score[rel])
                boost[rel] = max(boost.get(rel, 0.0), 0.1 * score[trel])
    final = {rel: score[rel] + boost.get(rel, 0.0) for rel in files}
    ranked = sorted(((r, final[r]) for r in final if final[r] > 0), key=lambda x: (-x[1], x[0]))
    return ranked, symbols, len(files)


def build_task_map(project_root: str, task_text: str) -> str:
    """生成这次任务的代码地图；项目太小、任务和代码对不上（闲聊、问概念）时返回空字符串。"""
    ranked, symbols, n = rank_files(project_root, task_text)
    if not ranked:
        return ""
    top = ranked[0][1]
    if top < _MIN_TOP_SCORE:
        return ""
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
