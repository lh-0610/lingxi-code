"""src/codeintel.py 测试：tree-sitter 多语言符号提取、import 解析、code_map_ts。

测试范围：
  - is_available / supported_extensions / get_language_for_path
  - extract_symbols（Python / JS / TS interface/type/enum / TSX）
  - extract_imports（Python import/from + JS import/require）
  - code_map_ts（目录级符号地图）
"""
import pytest

from src import codeintel


# ---------------------------------------------------------------------------
# 辅助：跳过 tree-sitter 不可用的环境
# ---------------------------------------------------------------------------
requires_ts = pytest.mark.skipif(
    not codeintel.is_available(),
    reason="tree-sitter language packs not installed",
)


# ---------------------------------------------------------------------------
# is_available / supported_extensions
# ---------------------------------------------------------------------------

class TestAvailability:
    @requires_ts
    def test_is_available(self):
        assert codeintel.is_available() is True

    @requires_ts
    def test_supported_extensions_non_empty(self):
        exts = codeintel.supported_extensions()
        assert len(exts) >= 2  # 至少 .py + 一种 JS/TS
        assert ".py" in exts

    @requires_ts
    def test_py_extension(self):
        assert ".py" in codeintel.supported_extensions()


# ---------------------------------------------------------------------------
# get_language_for_path
# ---------------------------------------------------------------------------

class TestGetLanguage:
    @requires_ts
    @pytest.mark.parametrize("path,expected", [
        ("foo.py", "python"),
        ("foo.pyw", "python"),
        ("foo.js", "javascript"),
        ("foo.jsx", "javascript"),
        ("foo.mjs", "javascript"),
        ("foo.cjs", "javascript"),
        ("foo.ts", "typescript"),
        ("foo.tsx", "tsx"),
    ])
    def test_returns_correct_language(self, path, expected):
        assert codeintel.get_language_for_path(path) == expected

    def test_unknown_ext_returns_none(self):
        assert codeintel.get_language_for_path("foo.rb") is None

    def test_no_ext_returns_none(self):
        assert codeintel.get_language_for_path("Makefile") is None


# ---------------------------------------------------------------------------
# extract_symbols – Python
# ---------------------------------------------------------------------------

class TestExtractSymbolsPython:
    @requires_ts
    def test_class_and_methods(self):
        code = (
            "class Bar:\n"
            "    def baz(self):\n"
            "        pass\n"
        )
        syms = codeintel.extract_symbols(code, "foo.py")
        names = {s["name"] for s in syms}
        assert "Bar" in names
        assert "baz" in names
        # Bar → kind=class, baz → kind=method
        bar = [s for s in syms if s["name"] == "Bar"][0]
        assert bar["kind"] == "class"
        baz = [s for s in syms if s["name"] == "baz"][0]
        assert baz["kind"] == "method"

    @requires_ts
    def test_top_level_function(self):
        code = "def hello():\n    pass\n"
        syms = codeintel.extract_symbols(code, "f.py")
        assert len(syms) == 1
        assert syms[0]["name"] == "hello"
        assert syms[0]["kind"] == "function"
        assert syms[0]["line"] == 1

    @requires_ts
    def test_async_function(self):
        code = "async def fetch():\n    pass\n"
        syms = codeintel.extract_symbols(code, "f.py")
        assert any(s["name"] == "fetch" and s["kind"] == "function" for s in syms)

    @requires_ts
    def test_nested_class(self):
        code = (
            "class Outer:\n"
            "    class Inner:\n"
            "        pass\n"
        )
        syms = codeintel.extract_symbols(code, "f.py")
        names = {s["name"] for s in syms}
        assert "Outer" in names
        assert "Inner" in names

    @requires_ts
    def test_end_line(self):
        code = "class Multi:\n    def a(self):\n        pass\n    def b(self):\n        pass\n"
        syms = codeintel.extract_symbols(code, "f.py")
        multi = [s for s in syms if s["name"] == "Multi"][0]
        assert multi["end_line"] == 5  # 5 lines total

    @requires_ts
    def test_line_numbers_1_indexed(self):
        code = "def first():\n    pass\n\ndef second():\n    pass\n"
        syms = codeintel.extract_symbols(code, "f.py")
        first = [s for s in syms if s["name"] == "first"][0]
        second = [s for s in syms if s["name"] == "second"][0]
        assert first["line"] == 1
        assert second["line"] == 4


# ---------------------------------------------------------------------------
# extract_symbols – JS
# ---------------------------------------------------------------------------

class TestExtractSymbolsJS:
    @requires_ts
    def test_js_class_and_function(self):
        code = "class Foo {}\nfunction bar() {}\n"
        syms = codeintel.extract_symbols(code, "a.js")
        names = {s["name"] for s in syms}
        assert "Foo" in names
        assert "bar" in names
        bar = [s for s in syms if s["name"] == "bar"][0]
        assert bar["kind"] == "function"

    @requires_ts
    def test_js_async_function(self):
        code = "export async function baz() {}\n"
        syms = codeintel.extract_symbols(code, "a.js")
        assert any(s["name"] == "baz" and s["kind"] == "function" for s in syms)

    @requires_ts
    def test_js_arrow_export(self):
        code = "export const helper = () => 42\n"
        syms = codeintel.extract_symbols(code, "a.js")
        assert any(s["name"] == "helper" and s["kind"] == "function" for s in syms)

    @requires_ts
    def test_js_variable(self):
        code = "const x = 123\n"
        syms = codeintel.extract_symbols(code, "a.js")
        assert any(s["name"] == "x" and s["kind"] == "variable" for s in syms)

    @requires_ts
    def test_jsx(self):
        code = "function App() { return <div /> }\n"
        syms = codeintel.extract_symbols(code, "a.jsx")
        assert any(s["name"] == "App" for s in syms)


# ---------------------------------------------------------------------------
# extract_symbols – TypeScript / TSX
# ---------------------------------------------------------------------------

class TestExtractSymbolsTS:
    @requires_ts
    def test_ts_interface(self):
        code = "interface Config { port: number }\n"
        syms = codeintel.extract_symbols(code, "a.ts")
        assert any(s["name"] == "Config" and s["kind"] == "interface" for s in syms)

    @requires_ts
    def test_ts_type(self):
        code = "type Result = { ok: boolean }\n"
        syms = codeintel.extract_symbols(code, "a.ts")
        assert any(s["name"] == "Result" and s["kind"] == "type" for s in syms)

    @requires_ts
    def test_ts_enum(self):
        code = "export enum Status { Active, Inactive }\n"
        syms = codeintel.extract_symbols(code, "a.ts")
        assert any(s["name"] == "Status" and s["kind"] == "enum" for s in syms)

    @requires_ts
    def test_ts_class(self):
        code = "class Component { render() {} }\n"
        syms = codeintel.extract_symbols(code, "a.ts")
        names = {s["name"] for s in syms}
        assert "Component" in names
        assert "render" in names

    @requires_ts
    def test_tsx_function(self):
        code = "function Widget() { return <div /> }\n"
        syms = codeintel.extract_symbols(code, "a.tsx")
        assert any(s["name"] == "Widget" for s in syms)


# ---------------------------------------------------------------------------
# extract_imports – Python
# ---------------------------------------------------------------------------

class TestExtractImportsPython:
    @requires_ts
    def test_basic_import(self):
        code = "import os\nimport json\n"
        imps = codeintel.extract_imports(code, "f.py")
        assert "os" in imps
        assert "json" in imps

    @requires_ts
    def test_from_import(self):
        code = "from pathlib import Path\n"
        imps = codeintel.extract_imports(code, "f.py")
        assert "pathlib" in imps

    @requires_ts
    def test_relative_import_dot(self):
        code = "from .models import User\n"
        imps = codeintel.extract_imports(code, "f.py")
        assert ".models" in imps

    @requires_ts
    def test_relative_import_dots(self):
        code = "from ..utils import helper\n"
        imps = codeintel.extract_imports(code, "f.py")
        assert "..utils" in imps

    @requires_ts
    def test_dotted_import(self):
        code = "import os.path\n"
        imps = codeintel.extract_imports(code, "f.py")
        assert "os.path" in imps


# ---------------------------------------------------------------------------
# extract_imports – JS/TS
# ---------------------------------------------------------------------------

class TestExtractImportsJS:
    @requires_ts
    def test_es6_import(self):
        code = "import React from 'react'\nimport { useState } from './hooks'\n"
        imps = codeintel.extract_imports(code, "a.ts")
        assert "react" in imps
        assert "./hooks" in imps

    @requires_ts
    def test_require(self):
        code = "const fs = require('fs')\n"
        imps = codeintel.extract_imports(code, "a.js")
        assert "fs" in imps

    @requires_ts
    def test_import_default(self):
        code = "import axios from 'axios'\n"
        imps = codeintel.extract_imports(code, "a.js")
        assert "axios" in imps


# ---------------------------------------------------------------------------
# code_map_ts – directory-level
# ---------------------------------------------------------------------------

class TestCodeMapTS:
    @requires_ts
    def test_py_directory(self, tmp_path):
        (tmp_path / "a.py").write_text("def foo(): pass\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("class C: pass\n", encoding="utf-8")
        result = codeintel.code_map_ts(str(tmp_path))
        assert "a.py" in result
        assert "foo" in result
        assert "b.py" in result
        assert "C" in result

    @requires_ts
    def test_ts_directory(self, tmp_path):
        (tmp_path / "app.ts").write_text(
            "interface Props { x: number }\nfunction render() {}\n",
            encoding="utf-8",
        )
        result = codeintel.code_map_ts(str(tmp_path))
        assert "app.ts" in result
        assert "Props" in result
        assert "render" in result

    @requires_ts
    def test_skips_noise(self, tmp_path):
        (tmp_path / "real.py").write_text("def real(): pass\n", encoding="utf-8")
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "lib.js").write_text("function fake() {}", encoding="utf-8")
        result = codeintel.code_map_ts(str(tmp_path))
        assert "real" in result
        assert "fake" not in result

    @requires_ts
    def test_empty_dir(self, tmp_path):
        result = codeintel.code_map_ts(str(tmp_path))
        # 没有源文件时返回 None
        assert result is None or result == ""

    @requires_ts
    def test_max_chars(self, tmp_path):
        # 创建足够多的文件以触发截断
        for i in range(20):
            (tmp_path / f"f{i}.py").write_text(f"def fn{i}(): pass\n", encoding="utf-8")
        result = codeintel.code_map_ts(str(tmp_path), max_chars=100)
        assert len(result) <= 200  # 给一点余量（截断在循环内）


# ---------------------------------------------------------------------------
# extract_imports_from_content（别名）
# ---------------------------------------------------------------------------

class TestAlias:
    @requires_ts
    def test_alias_equals_main(self):
        code = "import os\n"
        assert codeintel.extract_imports_from_content(code, "f.py") == codeintel.extract_imports(code, "f.py")


# ---------------------------------------------------------------------------
# 无 tree-sitter 时的降级行为
# ---------------------------------------------------------------------------

class TestNoTreeSitterFallback:
    """即使 tree-sitter 未安装，extract_symbols/imports 应返回空列表，不抛异常。"""

    def test_extract_symbols_unsupported_ext(self):
        assert codeintel.extract_symbols("fn main() {}", "main.rs") == []

    def test_extract_imports_unsupported_ext(self):
        assert codeintel.extract_imports("use std::io;", "main.rs") == []
