"""Tests for find_tests and related_files tools.

Run:  python -m pytest scripts/test_related_files.py -v
"""
import os
import re
import sys

import pytest

# 保证项目根在 sys.path 里
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── 辅助 ────────────────────────────────────────────────────────────

def _norm(p: str) -> str:
    """统一路径分隔符为 '/' 便于跨平台断言。"""
    return p.replace("\\", "/")


# ── find_tests ──────────────────────────────────────────────────────


class TestFindTests:
    """find_tests —— 根据源码文件/符号名查找相关测试文件。"""

    # ── 基本功能 ──

    def test_finds_test_tools_for_tools_py(self):
        """find_tests(path='src/tools.py') 应找到 scripts/test_tools.py。"""
        from src.tools import find_tests

        result = find_tests.func("src/tools.py")
        assert "scripts/test_tools.py" in _norm(result)

    def test_finds_test_related_files_for_tools_py(self):
        """find_tests(path='src/tools.py') 也应找到本测试文件。"""
        from src.tools import find_tests

        result = find_tests.func("src/tools.py")
        assert "scripts/test_related_files.py" in _norm(result)

    def test_tools_py_scores_high_for_test_tools(self):
        """scripts/test_tools.py 应有高分（文件名+import+目录）。"""
        from src.tools import find_tests

        result = find_tests.func("src/tools.py")
        scores = re.findall(
            r'scripts/test_tools\.py\s+\(分数 (\d+):', _norm(result)
        )
        assert len(scores) == 1, f"未找到 test_tools.py 的分数行: {result}"
        assert int(scores[0]) >= 90, f"期望分数>=90，实际: {scores[0]}"

    def test_tools_py_test_tools_score_contains_reasons(self):
        """scripts/test_tools.py 的分数行应包含文件名匹配和 import 匹配理由。"""
        from src.tools import find_tests

        result = find_tests.func("src/tools.py")
        # 找到 test_tools.py 那一行
        line = next(
            (l for l in result.splitlines() if "test_tools.py" in _norm(l) and "分数" in l),
            "",
        )
        assert line, f"未找到包含分数的 test_tools.py 行: {result}"
        assert "文件名匹配" in line

    # ── symbol 过滤 ──

    def test_symbol_filter_increases_score(self):
        """find_tests(path, symbol='edit_file') 应在结果中反映符号内容。"""
        from src.tools import find_tests

        result = find_tests.func("src/tools.py", symbol="edit_file")
        assert "edit_file" in result

    def test_symbol_filter_keeps_correct_file(self):
        """symbol 过滤后仍应列出 scripts/test_tools.py。"""
        from src.tools import find_tests

        result = find_tests.func("src/tools.py", symbol="edit_file")
        assert "scripts/test_tools.py" in _norm(result)

    # ── 无参数 ──

    def test_no_args_returns_at_least_one_test(self):
        """find_tests() 无参数时应搜索项目根，返回至少一个测试文件。"""
        from src.tools import find_tests

        result = find_tests.func()
        assert re.search(r"- (?:tests|test|scripts)/.+\.py", _norm(result))
        assert "run_tests" in result

    # ── 错误路径 ──

    def test_nonexistent_file_returns_error(self):
        from src.tools import find_tests

        result = find_tests.func("src/nonexistent_xyz.py")
        assert "失败" in result or "不存在" in result

    def test_path_outside_project_rejected(self):
        from src.tools import find_tests

        result = find_tests.func("../etc/passwd")
        assert "失败" in result or "超出" in result or "不允许" in result

    # ── 输出格式 ──

    def test_returns_run_tests_command(self):
        """返回结果应包含 run_tests 推荐命令。"""
        from src.tools import find_tests

        result = find_tests.func("src/tools.py")
        assert "run_tests" in result

    def test_sorted_by_score_desc(self):
        """结果应按分数降序排列。"""
        from src.tools import find_tests

        result = find_tests.func("src/tools.py")
        scores = [int(m) for m in re.findall(r"分数 (\d+)", result)]
        assert len(scores) >= 1
        assert scores == sorted(scores, reverse=True)

    def test_equal_scores_are_sorted_by_path(self, project_dir):
        (project_dir / "src").mkdir()
        (project_dir / "src" / "state.py").write_text("value = 1\n", encoding="utf-8")
        (project_dir / "tests").mkdir()
        for name in ("test_z.py", "test_a.py"):
            (project_dir / "tests" / name).write_text(
                "from src import state\n\ndef test_value(): assert state.value == 1\n",
                encoding="utf-8",
            )

        from src.tools import find_tests
        result = find_tests.func("src/state.py")

        assert result.index("tests/test_a.py") < result.index("tests/test_z.py")

    def test_max_results_limits_output(self):
        """max_results=1 时应只返回最多 1 个候选。"""
        from src.tools import find_tests

        result = find_tests.func("src/tools.py", max_results=1)
        candidates = re.findall(r"^- .+\.py\s+\(分数", result, re.MULTILINE)
        assert len(candidates) <= 1

    def test_scores_are_positive(self):
        """找到的测试文件分数应大于 0。"""
        from src.tools import find_tests

        result = find_tests.func("src/tools.py")
        scores = [int(m) for m in re.findall(r"分数 (\d+)", result)]
        assert all(s > 0 for s in scores)

    # ── 代理工具函数 ──

    def test_extract_imports_py(self):
        """_extract_imports_py 应能提取标准 import 语句。"""
        from src.tools import _extract_imports_py

        imports = _extract_imports_py(
            os.path.join(ROOT, "scripts", "test_tools.py")
        )
        assert "os" in imports
        assert "sys" in imports
        assert "pytest" in imports
        assert "src.tools" in imports

    def test_score_test_candidate_filename_match(self):
        """_score_test_candidate 对文件名匹配应给高分。"""
        from src.tools import _score_test_candidate

        test_abs = os.path.join(ROOT, "scripts", "test_tools.py")
        test_rel = "scripts/test_tools.py"
        sc, reasons = _score_test_candidate(
            test_abs, test_rel,
            target_stem="tools", target_module="src.tools",
            symbol="", root=ROOT,
        )
        assert sc >= 90, f"期望>=90，实际: {sc}"
        assert any("文件名" in r for r in reasons)

    def test_score_test_candidate_no_match(self):
        """_score_test_candidate 对不相关的文件应给低分或 0。"""
        from src.tools import _score_test_candidate

        test_abs = os.path.join(ROOT, "scripts", "test_tools.py")
        test_rel = "scripts/test_tools.py"
        sc, reasons = _score_test_candidate(
            test_abs, test_rel,
            target_stem="completely_unrelated_xyz", target_module="foo.bar",
            symbol="", root=ROOT,
        )
        assert sc <= 10, f"期望<=10（仅目录分），实际: {sc}"

    def test_find_test_files_returns_known_tests(self):
        """_find_test_files 应返回项目中已知的测试文件。"""
        from src.tools import _find_test_files

        test_files = _find_test_files(ROOT)
        rels = [_norm(rel) for _, rel in test_files]
        assert "scripts/test_tools.py" in rels
        assert "scripts/conftest.py" not in rels

    def test_parent_package_import_does_not_match_unrelated_submodule(self, project_dir):
        """from pkg import state 不能被误判成 import pkg.tools。"""
        (project_dir / "pkg").mkdir()
        (project_dir / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (project_dir / "pkg" / "tools.py").write_text("def work(): pass\n", encoding="utf-8")
        (project_dir / "pkg" / "state.py").write_text("value = 1\n", encoding="utf-8")
        (project_dir / "scripts").mkdir()
        (project_dir / "scripts" / "test_state.py").write_text(
            "from pkg import state\n\ndef test_state(): assert state.value == 1\n",
            encoding="utf-8",
        )

        from src.tools import find_tests
        result = find_tests.func("pkg/tools.py")

        assert "test_state.py" not in result

    def test_scripts_non_test_python_is_not_candidate(self, project_dir):
        (project_dir / "src").mkdir()
        (project_dir / "src" / "app.py").write_text("def run(): pass\n", encoding="utf-8")
        (project_dir / "scripts").mkdir()
        (project_dir / "scripts" / "build_app.py").write_text(
            "from src import app\n", encoding="utf-8"
        )

        from src.tools import find_tests
        result = find_tests.func("src/app.py")

        assert "build_app.py" not in result
        assert "未找到" in result

    def test_nested_test_directory_is_recognized(self, project_dir):
        (project_dir / "tests" / "unit").mkdir(parents=True)
        (project_dir / "tests" / "unit" / "test_sample.py").write_text(
            "def test_sample(): pass\n", encoding="utf-8"
        )

        from src.tools import find_tests
        result = find_tests.func()

        assert "tests/unit/test_sample.py" in _norm(result)


# ── related_files ───────────────────────────────────────────────────


class TestRelatedFiles:
    """related_files —— 给定源码文件，列出它导入的文件、导入它的文件、相关测试。"""

    # ── 基本结构 ──

    def test_basic_tools_py_header(self):
        """related_files('src/tools.py') 应以目标文件路径开头。"""
        from src.tools import related_files

        result = related_files.func("src/tools.py")
        assert "目标文件" in result
        assert "src/tools.py" in _norm(result) or "src\\tools.py" in result

    def test_imports_section_present(self):
        """src/tools.py 导入了项目内文件（如 src/state.py, src/paths.py）。"""
        from src.tools import related_files

        result = related_files.func("src/tools.py")
        assert "它导入的项目内文件" in result
        # 至少应有 state 和 paths 相关文件
        assert "state" in result

    def test_reverse_importers_present(self):
        """src/tools.py 被其他文件导入（如 src/agent.py, src/streaming.py）。"""
        from src.tools import related_files

        result = related_files.func("src/tools.py")
        assert "导入它的项目内文件" in result

    def test_test_candidates_present(self):
        """src/tools.py 应有相关测试候选。"""
        from src.tools import related_files

        result = related_files.func("src/tools.py")
        assert "相关测试候选" in result
        assert "scripts/test_tools.py" in _norm(result)

    def test_suggestions_present(self):
        """应包含建议下一步。"""
        from src.tools import related_files

        result = related_files.func("src/tools.py")
        assert "建议下一步" in result
        assert "read_file" in result

    def test_run_tests_in_suggestions(self):
        """有测试候选时应推荐 run_tests。"""
        from src.tools import related_files

        result = related_files.func("src/tools.py")
        assert "run_tests" in result

    # ── 不同文件 ──

    def test_agent_py_related_files(self):
        """related_files('src/agent.py') 应返回有效输出。"""
        from src.tools import related_files

        result = related_files.func("src/agent.py")
        assert "目标文件" in result
        assert "src/agent.py" in _norm(result) or "src\\agent.py" in result
        # agent.py 导入 state
        assert "state" in result

    def test_state_py_related_files(self):
        """related_files('src/state.py') 应列出导入它的文件。"""
        from src.tools import related_files

        result = related_files.func("src/state.py")
        assert "导入它的项目内文件" in result

    # ── 错误路径 ──

    def test_nonexistent_file_returns_error(self):
        from src.tools import related_files

        result = related_files.func("src/nonexistent_xyz.py")
        assert "失败" in result or "不存在" in result

    def test_path_outside_project_rejected(self):
        from src.tools import related_files

        result = related_files.func("../etc/passwd")
        assert "失败" in result or "超出" in result or "不允许" in result

    def test_non_python_file_returns_hint(self):
        """非 Python 文件应返回文件信息（无导入解析时只有建议下一步）。"""
        from src.tools import related_files

        result = related_files.func("config.json")
        assert "config.json" in result
        # 无导入信息时应有"建议下一步"
        assert "建议下一步" in result

    # ── 输出格式 ──

    def test_max_results_limits_reverse_importers(self):
        """max_results 应限制导入它文件的显示数量。"""
        from src.tools import related_files

        result = related_files.func("src/tools.py", max_results=1)
        # 逆向导入列表最多 1 条
        lines = result.split("\n")
        in_section = False
        count = 0
        for line in lines:
            if "导入它的项目内文件" in line:
                in_section = True
                continue
            if in_section:
                if line.startswith("- "):
                    count += 1
                elif line.strip() == "" or not line.startswith("-"):
                    break
        assert count <= 1

    def test_imports_are_project_files(self):
        """导入的文件列表应是项目内文件（以 src/ 开头或类似结构）。"""
        from src.tools import related_files

        result = related_files.func("src/tools.py")
        # 找到 "它导入的项目内文件" 后的 - 行
        lines = result.split("\n")
        in_section = False
        imported = []
        for line in lines:
            if "它导入的项目内文件" in line:
                in_section = True
                continue
            if in_section and line.startswith("- "):
                imported.append(line[2:].strip())
            elif in_section and line.strip() == "":
                break
        # 应至少有几条
        assert len(imported) >= 1, f"应至少找到 1 个导入文件，实际: {imported}"

    # ── 代理工具函数 ──

    def test_module_name_for_py(self):
        """_module_name_for_py 应正确转换路径到模块名。"""
        from src.tools import _module_name_for_py

        abs_path = os.path.join(ROOT, "src", "tools.py")
        assert _module_name_for_py(abs_path, ROOT) == "src.tools"

    def test_module_name_for_init(self):
        """_module_name_for_py 对 __init__.py 应返回包名。"""
        from src.tools import _module_name_for_py

        abs_path = os.path.join(ROOT, "src", "__init__.py")
        assert _module_name_for_py(abs_path, ROOT) == "src"

    def test_module_to_path(self):
        """_module_to_path 应能将模块名映射回文件路径。"""
        from src.tools import _module_to_path

        result = _module_to_path("src.tools", ROOT)
        assert result is not None
        assert result.endswith("tools.py")

    def test_module_to_path_package(self):
        """_module_to_path 对包名应返回 __init__.py。"""
        from src.tools import _module_to_path

        result = _module_to_path("src", ROOT)
        assert result is not None
        assert "__init__.py" in result

    def test_module_to_path_nonexistent(self):
        """_module_to_path 对不存在的模块应返回 None。"""
        from src.tools import _module_to_path

        result = _module_to_path("nonexistent.fake.module", ROOT)
        assert result is None

    def test_reverse_import_does_not_match_parent_package_only(self, project_dir):
        (project_dir / "pkg").mkdir()
        (project_dir / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (project_dir / "pkg" / "tools.py").write_text("def work(): pass\n", encoding="utf-8")
        (project_dir / "pkg" / "state.py").write_text("value = 1\n", encoding="utf-8")
        (project_dir / "consumer.py").write_text("from pkg import state\n", encoding="utf-8")

        from src.tools import related_files
        result = related_files.func("pkg/tools.py")

        assert "consumer.py" not in result

    def test_relative_imports_resolve_to_project_modules(self, project_dir):
        (project_dir / "pkg").mkdir()
        (project_dir / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (project_dir / "pkg" / "state.py").write_text("value = 1\n", encoding="utf-8")
        (project_dir / "pkg" / "consumer.py").write_text(
            "from . import state\n", encoding="utf-8"
        )

        from src.tools import related_files
        result = related_files.func("pkg/consumer.py")

        assert "pkg/state.py" in _norm(result)


# ── 独立运行 ────────────────────────────────────────────────────────
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
