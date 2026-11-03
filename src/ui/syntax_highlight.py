"""文件面板的语法高亮：把一行行差异文本交给 tree-sitter 划出「哪一段属于哪一类」，界面只管上色。

和 codeintel 的分工：语言表（_EXT_TO_LANG）、解析器缓存、定义（函数 / 类名）的识别都在
codeintel；这里只做分类，返回**字符下标范围**，不碰 Qt、不碰主题——颜色由 file_panel 按
当前主题取，切主题不用重新解析。

两条取类途径：
- Python / JS / TS / TSX：用单语言包自带的 queries/highlights.scm。TS / TSX 额外叠加 JS 那份
  ——它对着 TS 语法同样能编译，覆盖（函数 / 类名、字符串、注释）比 TS 自带那份全；
- 其余语言（tree-sitter-language-pack 0.13 不带 query 文件）：按节点类型名分类，注释 / 字符串
  / 数字认类型名出现的关键词，匿名 token 里的单词当关键字，函数 / 类名交给 codeintel 的定义识别
  （和代码地图同一套判断，不在别处再写一遍）。

认不出语言、没装语法库、行数超过 MAX_HIGHLIGHT_LINES、解析出错，一律返回空字典：界面按
原来的样子显示，不报错。
"""
from __future__ import annotations

import bisect
import importlib
import logging
import os
import re

from .. import codeintel

log = logging.getLogger("lingxi.ui.syntax")

# 超过这个规模就不高亮：tree-sitter 很快，但把上千行拼起来再逐段写字符格式会拖慢面板打开。
# 文件面板本来就是「看一眼差异」的地方，真要看几千行的文件有编辑器。
MAX_HIGHLIGHT_LINES = 2000
MAX_HIGHLIGHT_CHARS = 400_000

# 会参与高亮的行种类：diff 里的新增 / 删除 / 上下文行。hunk 头、diff 头、说明行不碰。
_CODE_KINDS = ("add", "del", "ctx")

# 类别 → 主题 token（file_panel 按当前主题取色；亮暗两套都有同名的键）
TOKEN_BY_CATEGORY = {
    "keyword": "syn_keyword",
    "string": "syn_string",
    "comment": "syn_comment",
    "number": "syn_number",
    "function": "syn_function",
    "type": "syn_type",
    "constant": "syn_constant",
    "operator": "syn_operator",
    "punct": "syn_punct",
}

_CATEGORIES = tuple(TOKEN_BY_CATEGORY)          # 用作 bytearray 里的编号，0 表示不上色
_CATEGORY_ID = {name: i + 1 for i, name in enumerate(_CATEGORIES)}
_ID_CATEGORY = {i + 1: name for i, name in enumerate(_CATEGORIES)}

# 重叠时谁盖住谁：数字大的后写、盖在前面。注释最高（它不会再被拆开），标点最低。
_PRIORITY = {
    "punct": 1,
    "operator": 2,
    "constant": 3,
    "type": 4,
    "function": 5,
    "number": 6,
    "keyword": 7,
    "string": 8,
    "comment": 9,
}

# 有官方 query 的语言 → 用哪几个包的 queries/highlights.scm。TS / TSX 先 JS 后 TS（后写的按
# 优先级覆盖，两份都能编译通过）。
_QUERY_MODULES = {
    "python": ("tree_sitter_python",),
    "javascript": ("tree_sitter_javascript",),
    "typescript": ("tree_sitter_javascript", "tree_sitter_typescript"),
    "tsx": ("tree_sitter_javascript", "tree_sitter_typescript"),
}

# 官方 query 里的捕获名 → (类别, 优先级)。没收进来的捕获（variable、property……）保持正文色：
# 全给上色反而看不出重点。
_CAPTURE_RULES = {
    "keyword": ("keyword", _PRIORITY["keyword"]),
    "string": ("string", _PRIORITY["string"]),
    "string.special": ("string", _PRIORITY["string"]),
    "escape": ("string", _PRIORITY["string"]),
    "embedded": ("string", _PRIORITY["string"]),
    "comment": ("comment", _PRIORITY["comment"]),
    "number": ("number", _PRIORITY["number"]),
    "function": ("function", _PRIORITY["function"]),
    "function.method": ("function", _PRIORITY["function"]),
    "function.builtin": ("function", _PRIORITY["function"]),
    "constructor": ("type", _PRIORITY["type"]),
    "type": ("type", _PRIORITY["type"]),
    "type.builtin": ("type", _PRIORITY["type"]),
    "constant": ("constant", _PRIORITY["constant"]),
    "constant.builtin": ("constant", _PRIORITY["constant"]),
    "variable.builtin": ("constant", _PRIORITY["constant"]),
    "operator": ("operator", _PRIORITY["operator"]),
    "punctuation.bracket": ("punct", _PRIORITY["punct"]),
    "punctuation.delimiter": ("punct", _PRIORITY["punct"]),
    "punctuation.special": ("punct", _PRIORITY["punct"]),
}

_query_cache: dict[str, list] = {}

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
# 通用遍历里当「常量」看的字面量关键字
_CONST_WORDS = {
    "true", "false", "null", "nil", "none", "undefined", "nullptr", "this", "self", "super",
}
_PUNCT_TOKENS = set("(){}[];,.:")

# 通用遍历的分类：节点类型名里出现这些词就算（小写比较）
_NUMBER_WORDS = ("integer", "float", "number", "numeric", "decimal", "hex", "octal", "binary")
_STRING_WORDS = ("string", "char", "rune", "heredoc")


def token_for(category: str) -> str:
    """类别对应的主题 token；不认识的类别退回正文色。"""
    return TOKEN_BY_CATEGORY.get(category, "mv_text")


def highlight_rows(rows, path: str, max_lines: int = MAX_HIGHLIGHT_LINES,
                   max_chars: int = MAX_HIGHLIGHT_CHARS) -> dict[int, list[tuple[int, int, str]]]:
    """rows（[(行号, 种类, 文本)]，和 tools_git.file_diff_lines 同格式）→ 高亮范围。

    返回 {行下标: [(起始字符, 结束字符, 类别)]}，下标相对**面板里那一行的原始文本**（diff 的
    "+" / "-" / 空格前缀已经算进去），file_panel 直接按它切文本上色。没有可高亮的行（语言认
    不出 / 没装语法库 / 太大）时返回空字典。
    """
    try:
        return _highlight(list(rows or []), path, max_lines, max_chars)
    except Exception:
        # 语法库版本打架、解析器崩了……都不能把面板弄挂：退回纯文本显示。
        log.debug("syntax highlight failed for %s", path, exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# 拼文本 + 解析
# ---------------------------------------------------------------------------

def _prefix_len(kind: str, text: str, unified: bool) -> int:
    """unified diff 的行首 "+" / "-" / " " 不算代码，要去掉再交给语法树。

    不是 unified diff 的行（新文件、和上次提交无差异时给的当前全文）本来就是纯内容，不能剥——
    剥掉缩进会改变语法结构。
    """
    if unified and kind in _CODE_KINDS and text[:1] in ("+", "-", " "):
        return 1
    return 0


def _highlight(rows, path, max_lines, max_chars):
    parser, lang = codeintel.parser_for_path(path)
    if parser is None:
        return {}

    # 只有真正的 unified diff 才有行首 +/-/空格；新文件、无差异时给的是纯内容
    unified = any(kind in ("hunk", "meta") for _no, kind, _text in rows)
    entries: list[tuple[int, int, str]] = []       # (行下标, 前缀长度, 代码)
    for index, (_no, kind, text) in enumerate(rows):
        if kind not in _CODE_KINDS:
            continue
        text = str(text)
        prefix = _prefix_len(kind, text, unified)
        entries.append((index, prefix, text[prefix:]))
    if not entries or len(entries) > max_lines:
        return {}
    if sum(len(code) for _i, _p, code in entries) > max_chars:
        return {}

    source = "\n".join(code for _i, _p, code in entries).encode("utf-8")
    tree = parser.parse(source)
    root = tree.root_node

    raw = _query_spans(parser, lang, root)
    if not raw:
        raw = _generic_spans(root, source)
    # 函数 / 类名统一再标一遍：官方 query 对「小写类名」这类不管，通用遍历也认得出定义
    for start, end, category in codeintel.definition_marks(root, source):
        raw.append((start, end, category, _PRIORITY[category]))

    return _to_rows(raw, entries)


def _query_spans(parser, lang: str, root) -> list[tuple]:
    """官方 highlights.scm 的捕获 → [(起, 止, 类别, 优先级)]；这个语言没有可用的 query 时空。"""
    out: list[tuple] = []
    for query in _queries_for(parser, lang):
        try:
            cursor = _query_cursor(query)
            captures = cursor.captures(root)
        except Exception:
            log.debug("query capture failed for %s", lang, exc_info=True)
            continue
        for name, nodes in captures.items():
            rule = _CAPTURE_RULES.get(name)
            if rule is None:
                continue
            category, priority = rule
            for node in nodes:
                out.append((node.start_byte, node.end_byte, category, priority))
    return out


def _queries_for(parser, lang: str) -> list:
    """这个语言的 highlights.scm 编译成的 Query 列表（按语言缓存）。"""
    if lang in _query_cache:
        return _query_cache[lang]
    queries = []
    for module_name in _QUERY_MODULES.get(lang, ()):
        try:
            module = importlib.import_module(module_name)
            query_path = os.path.join(os.path.dirname(module.__file__), "queries", "highlights.scm")
            with open(query_path, encoding="utf-8") as handle:
                text = handle.read()
            queries.append(_make_query(parser, text))
        except Exception:
            # 这个包没带 query、或和别的语法版本对不上：少一份而已，不影响其它
            log.debug("no highlights.scm from %s for %s", module_name, lang, exc_info=True)
    _query_cache[lang] = queries
    return queries


def _make_query(parser, text: str):
    from tree_sitter import Query
    return Query(parser.language, text)


def _query_cursor(query):
    from tree_sitter import QueryCursor
    return QueryCursor(query)


# ---------------------------------------------------------------------------
# 没有 query 的语言：按节点类型名分类
# ---------------------------------------------------------------------------

def _named_category(node_type: str) -> str:
    """具名节点按类型名认：注释 / 字符串 / 数字。认不出返回空串（保持正文色）。"""
    name = node_type.lower()
    if "comment" in name:
        return "comment"
    if "escape" in name:
        return "string"        # 转义序列是字符串的一部分
    if any(word in name for word in _STRING_WORDS):
        return "string"
    if any(word in name for word in _NUMBER_WORDS):
        return "number"
    if "keyword" in name:
        return "keyword"
    return ""


def _anonymous_category(token: str) -> str:
    """匿名 token（语法里写死的字面量）：单词当关键字，括号分号当标点，其余当运算符。"""
    if _WORD_RE.match(token):
        return "constant" if token.lower() in _CONST_WORDS else "keyword"
    if token in _PUNCT_TOKENS:
        return "punct"
    return "operator"


def _generic_spans(root, source: bytes) -> list[tuple]:
    """显式栈遍历（深层嵌套的语法树会撞 Python 递归上限），字符串 / 注释内部不再进去拆。"""
    out: list[tuple] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.is_named:
            category = _named_category(node.type)
            if category:
                out.append((node.start_byte, node.end_byte, category, _PRIORITY[category]))
                continue
            stack.extend(node.children)
            continue
        category = _anonymous_category(node.type)
        if category and node.end_byte > node.start_byte:
            out.append((node.start_byte, node.end_byte, category, _PRIORITY[category]))
    return out


# ---------------------------------------------------------------------------
# 字节范围 → 每行的字符范围
# ---------------------------------------------------------------------------

def _byte_to_char(code: str, size: int) -> list[int]:
    """非 ASCII 行的字节下标 → 字符下标（长 size + 1）。

    落在多字节字符中间的字节算到这个字符之后，和原来「解码行首这一截、坏掉的半个字符记一个
    替换符」数出来的一样——语法树给的范围本来也都在字符边界上。"""
    table = [0] * (size + 1)
    pos = 0
    for char_index, char in enumerate(code):
        width = len(char.encode("utf-8"))
        for k in range(1, width):
            table[pos + k] = char_index + 1
        pos += width
        table[pos] = char_index + 1
    return table


def _to_rows(raw: list[tuple], entries: list[tuple[int, int, str]]) -> dict:
    """把整段文本上的字节范围摊回每一行，返回 {行下标: [(起, 止, 类别)]}。

    按优先级从低到高逐段写入，后写的盖住前面的——同一段文字只留最后那一类。
    """
    starts: list[int] = []
    sizes: list[int] = []                 # 每行的字节数
    char_maps: list[list[int] | None] = []  # 字节下标 → 字符下标；纯 ASCII 行两者相同，记 None
    prefixes: dict[int, int] = {}
    offset = 0
    for index, prefix, code in entries:
        starts.append(offset)
        prefixes[index] = prefix
        size = len(code.encode("utf-8"))
        sizes.append(size)
        char_maps.append(None if size == len(code) else _byte_to_char(code, size))
        offset += size + 1

    # 每段都把整行重新编码、再解码行首到这一段，耗时随行长平方涨：压缩过的 JS / JSON 一行
    # 二十几万字符时，纯 ASCII 打开要卡 2 秒，带中文的卡 23 秒。每行只编码一次、表只建一次。
    painted: dict[int, bytearray] = {}
    for start, end, category, _priority in sorted(raw, key=lambda span: span[3]):
        if end <= start:
            continue
        line = bisect.bisect_right(starts, start) - 1
        while line < len(entries) and starts[line] < end:
            index, _prefix, code = entries[line]
            base = starts[line]
            begin = max(0, start - base)
            finish = min(sizes[line], end - base)
            if finish > begin:
                char_map = char_maps[line]
                if char_map is None:
                    begin_char, end_char = begin, finish
                else:
                    begin_char, end_char = char_map[begin], char_map[finish]
                if end_char > begin_char:
                    row = painted.get(index)
                    if row is None:
                        row = painted[index] = bytearray(len(code))
                    row[begin_char:end_char] = bytes([_CATEGORY_ID[category]]) * (
                        end_char - begin_char)
            line += 1

    out: dict[int, list[tuple[int, int, str]]] = {}
    for index, row in painted.items():
        prefix = prefixes[index]
        runs: list[tuple[int, int, str]] = []
        i = 0
        while i < len(row):
            value = row[i]
            if not value:
                i += 1
                continue
            j = i
            while j < len(row) and row[j] == value:
                j += 1
            runs.append((i + prefix, j + prefix, _ID_CATEGORY[value]))
            i = j
        if runs:
            out[index] = runs
    return out
