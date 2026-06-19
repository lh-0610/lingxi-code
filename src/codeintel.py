"""src/codeintel.py – Code Intelligence: tree-sitter based symbol & import extraction.

Provides:
  - extract_symbols(content, path) → [{kind, name, line, end_line, depth}]
  - extract_imports(content, path) → [source_string, ...]

Import resolution lives in tools._resolve_import_to_file (it carries the
commonpath 越界 check related_files needs); this module stays parse-only.

Falls back gracefully when tree-sitter language packs are not installed.
Currently supports: Python, JavaScript, JSX, TypeScript, TSX.
"""
from __future__ import annotations

import os
import logging
from typing import Optional

log = logging.getLogger("lingxi.codeintel")

# ---------------------------------------------------------------------------
# tree-sitter availability probe
# ---------------------------------------------------------------------------
_ts_languages: dict[str, object] = {}  # lang_name → tree_sitter.Language

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

_AVAILABLE = bool(_ts_languages)

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
    ".tsx": "tsx",
}

# ===========================================================================
# Public API
# ===========================================================================

def is_available() -> bool:
    """Return True if at least one tree-sitter language is loaded."""
    return _AVAILABLE


def supported_extensions() -> list[str]:
    """Return file extensions that tree-sitter can handle."""
    return [ext for ext, lang in _EXT_TO_LANG.items() if lang in _ts_languages]


def get_language_for_path(path: str) -> Optional[str]:
    """Return tree-sitter language name for *path*, or None."""
    ext = os.path.splitext(path)[1].lower()
    lang = _EXT_TO_LANG.get(ext)
    if lang and lang in _ts_languages:
        return lang
    return None


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
        else:
            return _symbols_js(tree.root_node, source)
    except Exception:
        log.debug("extract_symbols failed for %s", path, exc_info=True)
        return []


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
    if lang_name not in _ts_languages:
        return None
    if lang_name not in _parser_cache:
        p = _TSParser()
        p.language = _ts_languages[lang_name]  # type: ignore[assignment]
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

# File extensions that tree-sitter can parse
_TS_EXTENSIONS = tuple(_EXT_TO_LANG.keys())

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
            if ext not in _TS_EXTENSIONS:
                continue
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, base)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError:
                continue

            symbols = extract_symbols(content, fpath)
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
