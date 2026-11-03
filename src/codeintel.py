"""src/codeintel.py – Code Intelligence: tree-sitter based symbol & import extraction.

Provides:
  - extract_symbols(content, path) → [{kind, name, line, end_line, depth}]
  - file_symbols(content, path) → same, but falls back to a generic regex for
    languages without a grammar, so every source file gets an outline
  - extract_imports(content, path) → [source_string, ...]  (Python / JS / TS)

Import resolution lives in tools._resolve_import_to_file (it carries the
commonpath 越界 check related_files needs); this module stays parse-only.

语言：Python / JS / TS 用各自的官方语法包（tree-sitter-python 等）和专门的遍历；其余语言
（Java、Kotlin、Go、Rust、C/C++、C#、PHP、Ruby、Swift、Dart、Scala、Lua、Bash……）用
tree-sitter-language-pack 0.x 里预编译好的语法 + 通用遍历：各家语法给定义节点起名的习惯一致
（class_declaration / function_item / struct_specifier / method ……），按节点类型名认定义，
不用给每种语言写查询。1.x 改成运行时联网下载语法，桌面程序不用它。
没装、或打包时没带某个语言，就退回通用正则（class / def / func / fn … 开头的行）。
Vue / Svelte 单文件组件只解析里面的 <script>。
"""
from __future__ import annotations

import os
import re
import logging
from typing import Optional

log = logging.getLogger("lingxi.codeintel")

# ---------------------------------------------------------------------------
# tree-sitter availability probe
# ---------------------------------------------------------------------------
_ts_languages: dict[str, object] = {}  # lang_name → tree_sitter.Language（官方单语言包导入时加载，其余按需加载）
_missing_languages: set[str] = set()   # 试过、加载不了的语言（没装 / 打包时没带）

try:
    from tree_sitter import Language as _TSLanguage, Parser as _TSParser

    # Python
    try:
        import tree_sitter_python as _tspy
        _ts_languages["python"] = _TSLanguage(_tspy.language())
    except ImportError:
        pass

    # JavaScript (+ JSX: same grammar)
    try:
        import tree_sitter_javascript as _tsjs
        _ts_languages["javascript"] = _TSLanguage(_tsjs.language())
    except ImportError:
        pass

    # TypeScript + TSX (single package, two grammars)
    try:
        import tree_sitter_typescript as _tsts
        _ts_languages["typescript"] = _TSLanguage(_tsts.language_typescript())
        _ts_languages["tsx"] = _TSLanguage(_tsts.language_tsx())
    except ImportError:
        pass
except ImportError:
    pass

try:
    import tree_sitter_language_pack as _ts_pack
except Exception:  # 没装；或 tree-sitter 版本对不上
    _ts_pack = None

_AVAILABLE = bool(_ts_languages) or _ts_pack is not None

# Parser cache (one per language, reused across calls)
_parser_cache: dict[str, _TSParser] = {}

# Extension → tree-sitter language name
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".pyw": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    # 以下来自 tree-sitter-language-pack
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".groovy": "groovy",
    ".go": "go", ".rs": "rust", ".c": "c", ".h": "cpp",   # .h 也可能是 C++，C++ 语法能读 C 头文件
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".cs": "csharp", ".php": "php", ".rb": "ruby", ".swift": "swift", ".dart": "dart",
    ".lua": "lua", ".sh": "bash", ".bash": "bash", ".ps1": "powershell",
    ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".hs": "haskell", ".ml": "ocaml",
    ".r": "r", ".jl": "julia", ".zig": "zig", ".nim": "nim", ".sol": "solidity",
    ".pl": "perl", ".pm": "perl", ".clj": "clojure", ".fs": "fsharp", ".elm": "elm",
}
_JS_LANGS = ("javascript", "typescript", "tsx")
# 单文件组件：只解析 <script> 里的 JS / TS
_SFC_EXTS = (".vue", ".svelte")

# 当源码看的扩展名（代码地图、引用图都按这个收文件）；有没有语法库都算，没有的走正则
SOURCE_EXTENSIONS: tuple[str, ...] = tuple(_EXT_TO_LANG) + _SFC_EXTS

# ===========================================================================
# Public API
# ===========================================================================

def is_available() -> bool:
    """Return True if at least one tree-sitter language is loaded."""
    return _AVAILABLE


def supported_extensions() -> list[str]:
    """Return file extensions that tree-sitter can handle."""
    exts = [ext for ext, lang in _EXT_TO_LANG.items() if _load_language(lang) is not None]
    if _load_language("javascript") is not None:
        exts += list(_SFC_EXTS)
    return exts


def get_language_for_path(path: str) -> Optional[str]:
    """Return tree-sitter language name for *path*, or None."""
    ext = os.path.splitext(path)[1].lower()
    lang = _EXT_TO_LANG.get(ext)
    if lang and _load_language(lang) is not None:
        return lang
    return None


def parser_for_path(path: str):
    """路径对应的 (解析器, 语言名)；认不出扩展名或没装语法库时返回 (None, None)。

    给只需要「解析这棵语法树」的调用方用（文件面板的语法高亮）：解析器按语言缓存复用，
    语言对象的加载规则和 extract_symbols 完全一致，不重复实现一遍语言表。
    """
    try:
        lang = get_language_for_path(path)
        if not lang:
            return None, None
        parser = _get_parser(lang)
    except Exception:  # tree-sitter 装了一半（有 language pack 没有核心包）之类
        log.debug("parser_for_path failed for %s", path, exc_info=True)
        return None, None
    return (parser, lang) if parser is not None else (None, None)


def _load_language(name: str):
    """语言对象；官方单语言包已在导入时加载，其余从 language pack 按需取。加载不了返回 None。"""
    lang = _ts_languages.get(name)
    if lang is not None or name in _missing_languages:
        return lang
    try:
        lang = _ts_pack.get_language(name) if _ts_pack is not None else None
    except Exception:  # LookupError：这个语言的二进制没带进包
        lang = None
    if lang is None:
        _missing_languages.add(name)
    else:
        _ts_languages[name] = lang
    return lang


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------

def extract_symbols(content: str, path: str) -> list[dict]:
    """Extract top-level symbols from *content* (source code).

    Returns a list of dicts, each with keys:
        kind    – "function" | "class" | "method" | "variable" |
                  "interface" | "type" | "enum" | "namespace"
        name    – symbol name (str)
        line    – 1-indexed start line
        end_line – 1-indexed end line

    Returns [] when tree-sitter cannot parse the language or on error.
    """
    if os.path.splitext(path)[1].lower() in _SFC_EXTS:
        return _symbols_sfc(content)
    lang = get_language_for_path(path)
    if not lang:
        return []
    parser = _get_parser(lang)
    if not parser:
        return []
    try:
        source = content.encode("utf-8")
        tree = parser.parse(source)
        if lang == "python":
            return _symbols_python(tree.root_node, source)
        if lang in _JS_LANGS:
            return _symbols_js(tree.root_node, source)
        return _symbols_generic(tree.root_node, source)
    except Exception:
        log.debug("extract_symbols failed for %s", path, exc_info=True)
        return []


def file_symbols(content: str, path: str) -> list[dict]:
    """extract_symbols，但没有语法库的语言（没装 / 打包时没带）、或通用遍历认不出定义的语言
    （Elixir 的 def 在语法树里只是普通调用）退回通用正则，所有源码文件都能给出大纲。
    Python / JS / TS 有语法库时信语法树：正则会把文档字符串里 "class of …" 这种话也当成定义。"""
    syms = extract_symbols(content, path)
    if syms:
        return syms
    ext = os.path.splitext(path)[1].lower()
    lang = _EXT_TO_LANG.get(ext)
    if ext not in SOURCE_EXTENSIONS:
        return []
    if ((lang == "python" or lang in _JS_LANGS or ext in _SFC_EXTS)
            and _load_language(lang or "javascript") is not None):
        return []
    return regex_symbols(content)


# ---------------------------------------------------------------------------
# Import extraction
# ---------------------------------------------------------------------------

def extract_imports(content: str, path: str) -> list[str]:
    """Extract import source strings from *content*.

    For Python: ["os", "pathlib", ".models", "..utils"]
    For JS/TS:  ["react", "./hooks", "fs"]

    Returns [] on failure or unsupported language.
    """
    lang = get_language_for_path(path)
    if not lang:
        return []
    parser = _get_parser(lang)
    if not parser:
        return []
    if lang != "python" and lang not in _JS_LANGS:
        return []   # 其余语言的模块路径各有各的规则，related_files 改看引用图（code_graph.py）
    try:
        source = content.encode("utf-8")
        tree = parser.parse(source)
        if lang == "python":
            return _imports_python(tree.root_node, source)
        else:
            return _imports_js(tree.root_node, source)
    except Exception:
        log.debug("extract_imports failed for %s", path, exc_info=True)
        return []


# ===========================================================================
# Internal helpers
# ===========================================================================

def _get_parser(lang_name: str) -> Optional[_TSParser]:
    """Get or create a cached tree-sitter parser for *lang_name*."""
    language = _load_language(lang_name)
    if language is None:
        return None
    if lang_name not in _parser_cache:
        p = _TSParser()
        p.language = language  # type: ignore[assignment]
        _parser_cache[lang_name] = p
    return _parser_cache[lang_name]


def _text(node, source: bytes) -> str:
    """Extract text of a tree-sitter node."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Python symbol extraction
# ---------------------------------------------------------------------------

def _symbols_python(root, source: bytes) -> list[dict]:
    out: list[dict] = []

    def _walk(node, depth: int = 0):
        ntype = node.type
        if ntype in ("function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            name = _text(name_node, source) if name_node else "?"
            in_class = depth > 0
            kind = "method" if (in_class and ntype == "function_definition") else (
                "function" if ntype == "function_definition" else "class"
            )
            out.append({
                "kind": kind,
                "name": name,
                "line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "depth": depth,
            })
            # Recurse into class body for methods / nested classes (depth+1 → 缩进)
            if ntype == "class_definition":
                for child in node.children:
                    _walk(child, depth + 1)
            return

        for child in node.children:
            _walk(child, depth)

    for child in root.children:
        _walk(child)
    return out


# ---------------------------------------------------------------------------
# JS/TS symbol extraction
# ---------------------------------------------------------------------------

_TS_EXTRA_TYPES = {
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
    "abstract_class_declaration": "class",
    "internal_module": "namespace",  # TS namespace
}


def _symbols_js(root, source: bytes) -> list[dict]:
    out: list[dict] = []

    def _walk(node, depth: int = 0):
        ntype = node.type
        in_class = depth > 0

        # --- function_declaration ---
        if ntype == "function_declaration":
            name_node = node.child_by_field_name("name")
            out.append({
                "kind": "method" if in_class else "function",
                "name": _text(name_node, source) if name_node else "?",
                "line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "depth": depth,
            })
            return

        # --- class_declaration ---
        if ntype in ("class_declaration", "abstract_class_declaration"):
            name_node = node.child_by_field_name("name")
            out.append({
                "kind": "class",
                "name": _text(name_node, source) if name_node else "?",
                "line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "depth": depth,
            })
            for child in node.children:
                _walk(child, depth + 1)
            return

        # --- method_definition (inside class) ---
        if ntype == "method_definition" and in_class:
            name_node = node.child_by_field_name("name")
            out.append({
                "kind": "method",
                "name": _text(name_node, source) if name_node else "?",
                "line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "depth": depth,
            })
            return

        # --- lexical_declaration / variable_declaration (const/let/var) ---
        if ntype in ("lexical_declaration", "variable_declaration"):
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                vd_name = declarator.child_by_field_name("name")
                vd_value = declarator.child_by_field_name("value")
                name = _text(vd_name, source) if vd_name else "?"
                if vd_value and vd_value.type in ("arrow_function", "function", "class"):
                    kind = "class" if vd_value.type == "class" else "function"
                    out.append({
                        "kind": kind,
                        "name": name,
                        "line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                        "depth": depth,
                    })
                else:
                    out.append({
                        "kind": "variable",
                        "name": name,
                        "line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                        "depth": depth,
                    })
            return

        # --- TS-specific: interface / type / enum / namespace ---
        if ntype in _TS_EXTRA_TYPES:
            name_node = node.child_by_field_name("name")
            out.append({
                "kind": _TS_EXTRA_TYPES[ntype],
                "name": _text(name_node, source) if name_node else "?",
                "line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "depth": depth,
            })
            # Recurse into namespace body
            if ntype == "internal_module":
                for child in node.children:
                    _walk(child, depth + 1)
            return

        for child in node.children:
            _walk(child, depth)

    for child in root.children:
        _walk(child)
    return out


# ---------------------------------------------------------------------------
# Generic symbol extraction (every other tree-sitter grammar)
# ---------------------------------------------------------------------------

# 节点类型名里含这个词 → 定义的种类，按顺序先匹配先得。constructor 得在 struct 前面
# （con-struct-or），Swift 协议里的方法（protocol_function_declaration）得在 protocol 前面
_GENERIC_KINDS = (
    ("constructor", "method"), ("protocol_function", "method"),
    ("interface", "interface"), ("protocol", "interface"), ("trait", "trait"),
    ("struct", "struct"), ("enum", "enum"), ("record", "class"), ("class", "class"),
    ("object", "class"), ("impl", "impl"), ("namespace", "namespace"), ("module", "namespace"),
    ("mod_item", "namespace"), ("method", "method"),
    ("function", "function"), ("func", "function"),
    ("type_alias", "type"), ("type_item", "type"), ("type_spec", "type"), ("type_definition", "type"),
)
# 定义节点的类型名以这些结尾（class_body / function_call / method_invocation 都不是定义）
_DEF_SUFFIXES = ("_declaration", "_definition", "_item", "_specifier", "_spec", "_signature")
# Ruby / Haskell 的定义节点就叫这几个词本身
_BARE_DEFS = {"class": "class", "module": "namespace", "method": "method",
              "singleton_method": "method", "function": "function"}
# 带关键词、但不是定义：PHP 的 use、C# 的枚举成员、各种调用 / 表达式
_NOT_DEF_WORDS = ("call", "expression", "invocation", "import", "using", "namespace_use",
                  "use_declaration", "namespace_alias", "member")
_TYPE_KINDS = {"class", "interface", "struct", "enum", "trait", "impl"}
_CONTAINER_KINDS = _TYPE_KINDS | {"namespace"}
# Swift 的 struct / enum / extension、Kotlin 的 interface 都是 class_declaration，靠关键字区分
_CLASS_KEYWORDS = {"interface": "interface", "struct": "struct", "enum": "enum",
                   "extension": "impl", "protocol": "interface"}
_NAME_NODE_TYPES = {
    "identifier", "type_identifier", "field_identifier", "constant", "simple_identifier", "name",
    "property_identifier", "qualified_identifier", "scoped_identifier", "word",
    "namespace_identifier", "scoped_type_identifier", "destructor_name", "operator_name",
}
_MAX_SYMBOLS_PER_FILE = 3000   # 生成的大文件别撑爆地图


def _generic_kind(node) -> str:
    ntype = node.type
    if ntype in _BARE_DEFS:
        return _BARE_DEFS[ntype]
    if not ntype.endswith(_DEF_SUFFIXES) or any(w in ntype for w in _NOT_DEF_WORDS):
        return ""
    for word, kind in _GENERIC_KINDS:
        if word in ntype:
            # C / C++ 的 struct_specifier 不带花括号体就是在用这个类型（struct P p;），不是定义
            if ntype.endswith("_specifier") and node.child_by_field_name("body") is None:
                return ""
            if kind == "class":
                for child in node.children:
                    if not child.is_named and child.type in _CLASS_KEYWORDS:
                        return _CLASS_KEYWORDS[child.type]
            if ntype == "type_spec":   # Go：type User struct {...} / type Repo interface {...}
                body = node.child_by_field_name("type")
                if body is not None and body.type in ("struct_type", "interface_type"):
                    return body.type[:-len("_type")]
            return kind
    return ""


def _def_name_node(node):
    """定义节点里的名字节点：name 字段；Rust impl 用 type 字段；C / C++ 函数顺着 declarator 往里找；
    Kotlin / Dart 这类没有字段名的语法取第一个标识符子节点。"""
    found = node.child_by_field_name("name")
    if found is None and node.type == "impl_item":
        found = node.child_by_field_name("type")
    if found is None:
        found = node.child_by_field_name("declarator")
        while found is not None and found.child_by_field_name("declarator") is not None:
            found = found.child_by_field_name("declarator")
    if found is None:
        found = next((c for c in node.named_children if c.type in _NAME_NODE_TYPES), None)
    return found


def _symbols_generic(root, source: bytes) -> list[dict]:
    """按节点类型名认定义（见 _GENERIC_KINDS）。类 / 命名空间往里走一层找方法，函数体不进去。
    用显式栈不用递归：深层嵌套的语法树会撞 Python 的递归上限。"""
    out: list[dict] = []
    stack = [(child, 0, False) for child in reversed(root.children)]
    while stack and len(out) < _MAX_SYMBOLS_PER_FILE:
        node, depth, in_type = stack.pop()
        kind = _generic_kind(node)
        name_node = _def_name_node(node) if kind else None
        name = _text(name_node, source).strip() if name_node is not None else ""
        if kind and name and "\n" not in name and len(name) <= 80:
            if kind == "function" and in_type:
                kind = "method"
            out.append({
                "kind": kind,
                "name": name,
                "line": name_node.start_point[0] + 1,   # 名字所在行：Java 注解、C 返回类型可能在上一行
                "end_line": node.end_point[0] + 1,
                "depth": depth,
            })
            if kind in _CONTAINER_KINDS:
                stack.extend((child, depth + 1, kind in _TYPE_KINDS) for child in reversed(node.children))
            continue
        stack.extend((child, depth, in_type) for child in reversed(node.children))
    return out


def definition_marks(root, source: bytes, max_marks: int = 5000) -> list[tuple]:
    """定义节点的**名字**范围：[(start_byte, end_byte, "function" | "type")]。

    给语法高亮用（文件面板没有 highlights.scm 的语言靠它给函数 / 类名上色）。节点类型的判断
    直接用 _generic_kind / _def_name_node——定义长什么样只在这里定一次，别处不再重复。
    """
    out: list[tuple] = []
    stack = list(root.children)
    while stack and len(out) < max_marks:
        node = stack.pop()
        kind = _generic_kind(node)
        if kind:
            name_node = _def_name_node(node)
            if name_node is not None and name_node.end_byte > name_node.start_byte:
                out.append((name_node.start_byte, name_node.end_byte,
                            "type" if kind in _CONTAINER_KINDS | {"struct", "enum", "trait", "type"}
                            else "function"))
        stack.extend(node.children)
    return out


# ---------------------------------------------------------------------------
# Vue / Svelte single-file components: parse the <script> blocks
# ---------------------------------------------------------------------------

_SCRIPT_BLOCK_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)
_SCRIPT_LANG_RE = re.compile(r"""\blang\s*=\s*["']?(tsx|ts|typescript)\b""", re.I)


def script_blocks(content: str) -> list[tuple[str, int, str]]:
    """单文件组件里的 <script> 块：[(语言, 块之前的行数, 代码)]，语言按 lang="ts" / "tsx" 认，默认 JS。"""
    out = []
    for m in _SCRIPT_BLOCK_RE.finditer(content):
        lang_m = _SCRIPT_LANG_RE.search(m.group(1))
        lang = "javascript" if not lang_m else ("tsx" if lang_m.group(1).lower() == "tsx" else "typescript")
        out.append((lang, content.count("\n", 0, m.start(2)), m.group(2)))
    return out


def _symbols_sfc(content: str) -> list[dict]:
    out: list[dict] = []
    for lang, offset, code in script_blocks(content):
        parser = _get_parser(lang)
        if parser is None:
            continue
        source = code.encode("utf-8")
        try:
            syms = _symbols_js(parser.parse(source).root_node, source)
        except Exception:
            log.debug("extract_symbols failed for a <script> block", exc_info=True)
            continue
        for sym in syms:
            sym["line"] += offset
            sym["end_line"] += offset
        out.extend(syms)
    return out


# ---------------------------------------------------------------------------
# Regex fallback (languages without a grammar)
# ---------------------------------------------------------------------------

_REGEX_DEF_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?:(?:export|default|public|private|protected|internal|static|final|abstract|sealed|open|override|"
    r"async|unsafe|inline|virtual|extern|data|partial|readonly|local|pub(?:\([^)]*\))?)\s+)*"
    r"(?P<kw>class|interface|struct|enum|trait|impl|object|protocol|module|namespace|defmodule|defp|def|"
    r"fn|func|fun|function|sub|proc|type)\s+(?:\([^)]*\)\s*)?(?P<name>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)")
_REGEX_KINDS = {"def": "function", "defp": "function", "fn": "function", "func": "function",
                "fun": "function", "function": "function", "sub": "function", "proc": "function",
                "defmodule": "namespace", "module": "namespace", "protocol": "interface", "object": "class"}


def regex_symbols(content: str) -> list[dict]:
    """通用正则：一行以 class / def / func / fn … 开头（前面可带 public、export 之类修饰）就算定义。
    Go 的 func (r *T) Name 跳过接收者。层级按缩进（每 4 格一层）。"""
    out: list[dict] = []
    for i, line in enumerate(content.splitlines(), 1):
        m = _REGEX_DEF_RE.match(line)
        if not m:
            continue
        kw = m.group("kw")
        out.append({"kind": _REGEX_KINDS.get(kw, kw), "name": m.group("name"), "line": i, "end_line": i,
                    "depth": len(m.group("indent").expandtabs(4)) // 4})
        if len(out) >= _MAX_SYMBOLS_PER_FILE:
            break
    return out


# ---------------------------------------------------------------------------
# Python import extraction
# ---------------------------------------------------------------------------

def _imports_python(root, source: bytes) -> list[str]:
    out: list[str] = []

    def _walk(node):
        ntype = node.type

        if ntype == "import_statement":
            # `import os` / `import json`
            # children: "import" keyword + dotted_name
            for child in node.named_children:
                if child.type == "dotted_name":
                    out.append(_text(child, source))
                    break
            return

        if ntype == "import_from_statement":
            # `from pathlib import Path` → source is the node between "from" and "import"
            # children: "from" + dotted_name/relative_import + "import" + names
            found_from = False
            for child in node.children:
                if child.type == "from":
                    found_from = True
                    continue
                if found_from and child.type == "import":
                    break
                if found_from and child.type in ("dotted_name", "relative_import"):
                    out.append(_text(child, source))
                    break
            return

        for child in node.children:
            _walk(child)

    _walk(root)
    return out


# ---------------------------------------------------------------------------
# JS/TS import extraction
# ---------------------------------------------------------------------------

def _imports_js(root, source: bytes) -> list[str]:
    out: list[str] = []

    def _walk(node):
        ntype = node.type

        if ntype == "import_statement":
            # Find the string node (module source)
            for child in node.children:
                if child.type == "string":
                    raw = _text(child, source)
                    if len(raw) >= 2:
                        out.append(raw[1:-1])  # strip quotes
                    break
            return

        if ntype in ("lexical_declaration", "variable_declaration"):
            # Check for require('...') calls
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                val = declarator.child_by_field_name("value")
                if val and val.type == "call_expression":
                    fn = val.child_by_field_name("function")
                    if fn and _text(fn, source) == "require":
                        args = val.child_by_field_name("arguments")
                        if args:
                            for arg in args.children:
                                if arg.type == "string":
                                    raw = _text(arg, source)
                                    if len(raw) >= 2:
                                        out.append(raw[1:-1])
                                    break
            return

        for child in node.children:
            _walk(child)

    _walk(root)
    return out


# ===========================================================================
# Directory-level code map (for tools.code_map integration)
# ===========================================================================

# Directories to skip when walking
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "build", "dist", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
    ".next", ".nuxt", "coverage", ".turbo",
}


def code_map_ts(base: str, max_chars: int = 8000) -> Optional[str]:
    """Walk *base* directory and build a symbol map using tree-sitter.

    Returns a formatted string like:
        path/to/file.py
          L  10: function foo
          L  25: class Bar
            L  26: method baz

    Returns None when tree-sitter is unavailable OR no symbols are found, so the
    caller (tools.code_map) falls back to its regex extractor. Output truncated
    to *max_chars*.
    """
    if not _AVAILABLE:
        return None

    lines: list[str] = []
    chars = 0

    for dirpath, dirnames, filenames in os.walk(base):
        # prune skip dirs in-place
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SOURCE_EXTENSIONS:
                continue
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, base)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError:
                continue

            symbols = file_symbols(content, fpath)
            if not symbols:
                continue

            header = rel.replace("\\", "/")
            lines.append(header)
            chars += len(header) + 1
            if chars >= max_chars:
                break

            for sym in symbols:
                kind = sym["kind"]
                name = sym["name"]
                line_no = sym["line"]
                indent = "  " * sym.get("depth", 0)
                entry = f"  L{line_no:>4d}: {indent}{kind} {name}"
                lines.append(entry)
                chars += len(entry) + 1
                if chars >= max_chars:
                    break
            if chars >= max_chars:
                break
        if chars >= max_chars:
            break

    return "\n".join(lines) if lines else None


def extract_imports_from_content(content: str, path: str) -> list[str]:
    """Alias for extract_imports() -- kept for backward compatibility
    with tools.py which imports this name."""
    return extract_imports(content, path)
