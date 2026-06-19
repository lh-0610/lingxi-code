"""code_map 工具测试：符号提取（py/js）/ 跳噪声目录 / 边界。

code_map 用 state.current_project 作项目根（project_dir fixture 已设），用 .func 直调。
"""
import pytest

from src.tools import code_map
from src import codeintel

# TS/TSX 用例需要 tree-sitter 的 JS/TS 语法；没装这些可选包时正则回退抓不到
# 箭头函数 / interface / type，应跳过而非误报失败。
requires_ts = pytest.mark.skipif(
    codeintel.get_language_for_path("x.ts") is None,
    reason="tree-sitter typescript 未安装（可选依赖）",
)


class TestCodeMap:
    def test_extracts_py_symbols(self, project_dir):
        (project_dir / "foo.py").write_text(
            "class Bar:\n"
            "    def baz(self):\n"
            "        pass\n"
            "\n"
            "def top():\n"
            "    pass\n",
            encoding="utf-8",
        )
        out = code_map.func("")
        assert "foo.py" in out
        assert "Bar" in out
        assert "baz" in out              # 类内方法也提取
        assert "top" in out

    def test_has_line_numbers(self, project_dir):
        (project_dir / "f.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
        out = code_map.func("")
        assert "alpha" in out
        # tree-sitter 输出格式为 "L   1: ..."（宽域对齐），正则回退格式为 "L1: ..."
        assert "L" in out and "1" in out

    def test_skips_noise_dirs(self, project_dir):
        (project_dir / ".git").mkdir()
        (project_dir / ".git" / "hook.py").write_text("def hooked(): pass", encoding="utf-8")
        (project_dir / "real.py").write_text("def real_fn(): pass", encoding="utf-8")
        out = code_map.func("")
        assert "real_fn" in out
        assert "hooked" not in out       # .git 下的不扫

    def test_js_ts_symbols(self, project_dir):
        (project_dir / "a.ts").write_text(
            "export class Foo {}\n"
            "function bar() {}\n"
            "export async function baz() {}\n",
            encoding="utf-8",
        )
        out = code_map.func("")
        assert "Foo" in out
        assert "bar" in out
        assert "baz" in out
        # tree-sitter 格式 "L   1: class Foo"；正则回退格式 "L1: class Foo"
        assert "L" in out
        assert "1" in out  # Foo 在 L1
        assert "2" in out  # bar 在 L2
        assert "3" in out  # baz 在 L3

    @requires_ts
    def test_ts_interfaces_and_types(self, project_dir):
        """TypeScript interface 和 type 提取。"""
        (project_dir / "types.ts").write_text(
            "interface Config { port: number }\n"
            "type Result = { ok: boolean }\n"
            "export enum Status { Active, Inactive }\n",
            encoding="utf-8",
        )
        out = code_map.func("")
        assert "Config" in out
        assert "Result" in out
        assert "Status" in out

    @requires_ts
    def test_ts_arrow_export(self, project_dir):
        """导出的 const 箭头函数应提取。"""
        (project_dir / "utils.ts").write_text(
            "export const helper = () => 42\n",
            encoding="utf-8",
        )
        out = code_map.func("")
        assert "helper" in out

    def test_nonexistent_path(self, project_dir):
        assert "不存在" in code_map.func("nope_dir")

    def test_no_source_files(self, project_dir):
        (project_dir / "readme.txt").write_text("hi", encoding="utf-8")
        out = code_map.func("")
        assert "未找到" in out

    def test_subdir_scope(self, project_dir):
        (project_dir / "src").mkdir()
        (project_dir / "src" / "mod.py").write_text("def in_src(): pass", encoding="utf-8")
        (project_dir / "other.py").write_text("def in_root(): pass", encoding="utf-8")
        out = code_map.func("src")
        assert "in_src" in out
        assert "in_root" not in out      # 限定 src 子目录，不含项目根的

    def test_rejects_path_escape(self, project_dir):
        # 安全：.. 不能逃出项目根
        out = code_map.func("../")
        assert "不允许" in out or "项目范围" in out or "不存在" in out

    def test_skips_node_modules(self, project_dir):
        (project_dir / "node_modules").mkdir()
        (project_dir / "node_modules" / "bad.ts").write_text("function noisy() {}", encoding="utf-8")
        (project_dir / "app.ts").write_text("function real() {}", encoding="utf-8")
        out = code_map.func("")
        assert "real" in out
        assert "noisy" not in out

    def test_extracts_async_py_functions(self, project_dir):
        (project_dir / "async_mod.py").write_text("async def fetch_data():\n    pass\n", encoding="utf-8")
        out = code_map.func("")
        assert "fetch_data" in out
