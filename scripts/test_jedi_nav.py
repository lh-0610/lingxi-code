"""Jedi 代码导航工具测试

覆盖：
- find_definition / find_references 正确注册到 ALL_TOOLS + TOOL_DISPLAY_NAMES
- find_definition 在已知符号上返回合理结果
- find_references 在已知符号上返回合理结果
- 错误场景（文件不存在、符号找不到）
- run_tests 对 jedi 工具的放行
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── 注册检查 ─────────────────────────────────────────────
class TestRegistration:
    """确认两个工具在 ALL_TOOLS 和 TOOL_DISPLAY_NAMES 中注册。"""

    def test_all_tools_contains(self):
        from src.tools import ALL_TOOLS
        tool_names = [t.name if hasattr(t, "name") else str(t) for t in ALL_TOOLS]
        assert "find_definition" in tool_names
        assert "find_references" in tool_names

    def test_display_names(self):
        from src.tools import TOOL_DISPLAY_NAMES
        assert "find_definition" in TOOL_DISPLAY_NAMES
        assert "find_references" in TOOL_DISPLAY_NAMES

    def test_in_readonly_tools(self):
        from src.streaming import PLAN_MODE_READONLY_TOOLS
        assert "find_definition" in PLAN_MODE_READONLY_TOOLS
        assert "find_references" in PLAN_MODE_READONLY_TOOLS


# ── find_definition ─────────────────────────────────────
class TestFindDefinition:
    """在临时项目中验证 find_definition 的核心逻辑。"""

    def test_definition_found(self, project_dir):
        """在临时项目里写一个简单的 Python 文件，跳转到函数定义应有结果。"""
        src = project_dir / "sample.py"
        src.write_text(
            "def greet(name):\n"
            "    return f'hello {name}'\n"
            "\n"
            "result = greet('world')\n",
            encoding="utf-8",
        )
        from src.tools import find_definition

        result = find_definition.func("greet", "sample.py")
        assert "错误" not in result
        assert "greet" in result

    def test_definition_not_found(self, project_dir):
        """符号不存在时应返回友好提示。"""
        src = project_dir / "sample.py"
        src.write_text("x = 1\n", encoding="utf-8")
        from src.tools import find_definition

        result = find_definition.func("nonexistent_symbol_xyz")
        assert "未找到" in result or "找不到" in result or "无结果" in result or "没找到" in result

    def test_file_not_found(self, project_dir):
        from src.tools import find_definition

        result = find_definition.func("x", str(project_dir / "nope.py"))
        assert "错误" in result or "失败" in result or "不存在" in result or "没找到" in result


# ── find_references ─────────────────────────────────────
class TestFindReferences:
    """在临时项目中验证 find_references 的核心逻辑。"""

    def test_references_found(self, project_dir):
        """同一项目中多处调用同一函数，references 应全部找到。"""
        src = project_dir / "multi.py"
        src.write_text(
            "def helper():\n"
            "    return 42\n"
            "\n"
            "a = helper()\n"
            "b = helper()\n",
            encoding="utf-8",
        )
        from src.tools import find_references

        result = find_references.func("helper", "multi.py")
        assert "错误" not in result
        assert "helper" in result

    def test_references_not_found(self, project_dir):
        src = project_dir / "sample.py"
        src.write_text("x = 1\n", encoding="utf-8")
        from src.tools import find_references

        result = find_references.func("ghost_func_abc")
        assert "未找到" in result or "找不到" in result or "无结果" in result or "引用" in result


# ── run_tests 放行 ──────────────────────────────────────
class TestRunTestsPassthrough:
    """确认 run_tests 工具允许 find_definition/find_references。"""

    def test_in_plan_mode_readonly(self):
        from src.streaming import PLAN_MODE_READONLY_TOOLS
        assert "find_definition" in PLAN_MODE_READONLY_TOOLS
        assert "find_references" in PLAN_MODE_READONLY_TOOLS

    def test_in_parallel_safe(self):
        from src.streaming import PARALLEL_SAFE_TOOLS
        assert "find_definition" in PARALLEL_SAFE_TOOLS
        assert "find_references" in PARALLEL_SAFE_TOOLS


# ── path 给定时严格限定本文件（不跨文件） + 未装降级 ──────────
class TestFindDefinitionPathScoped:
    """path 给定即「找这个文件里的符号」：跳过注释找到同文件的 def；
    纯注释提到时绝不退到全项目返回别的文件的定义（守护 Codex review 2 抓的越界 bug）。"""

    def test_skips_comment_finds_def_in_same_file(self, project_dir):
        """符号首处在注释里、真实 def 在同文件 → 遍历到 def 行解析成功。"""
        src = project_dir / "c.py"
        src.write_text(
            "# target_fn 是个工具函数\n"
            "def target_fn():\n"
            "    return 1\n",
            encoding="utf-8",
        )
        from src.tools import find_definition

        result = find_definition.func("target_fn", "c.py")
        assert "c.py:2" in result   # 命中 def 那行（2），不是注释行（1）

    def test_comment_only_does_not_cross_file(self, project_dir):
        """指定文件里只是注释提到符号、真实定义在【别的文件】→ 绝不返回别的文件的定义。"""
        (project_dir / "comment_only.py").write_text(
            "# target_fn mentioned here\nx = 1\n", encoding="utf-8")
        (project_dir / "real.py").write_text(
            "def target_fn():\n    return 1\n", encoding="utf-8")
        from src.tools import find_definition

        result = find_definition.func("target_fn", "comment_only.py")
        assert "real.py" not in result            # ← 关键：不跨文件误导
        assert "可解析定义" in result


class TestFindReferencesPathScoped:
    def test_comment_only_does_not_cross_file(self, project_dir):
        """find_references 同样：注释提到 → 不退到全项目返回和 path 无关的引用。"""
        (project_dir / "comment_only.py").write_text(
            "# target_fn mentioned here\nx = 1\n", encoding="utf-8")
        (project_dir / "real.py").write_text(
            "def target_fn():\n    return 1\n\nresult = target_fn()\n", encoding="utf-8")
        from src.tools import find_references

        result = find_references.func("target_fn", "comment_only.py")
        assert "real.py" not in result            # ← 关键：不跨文件误导
        assert "可解析引用" in result


def test_degrades_without_jedi(project_dir, monkeypatch):
    """没装 jedi 时给降级提示、不抛异常。"""
    import builtins
    real_import = builtins.__import__

    def _fake(name, *a, **k):
        if name == "jedi":
            raise ImportError("simulated: no jedi")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fake)
    from src.tools import find_definition

    out = find_definition.func("x", "y.py")
    assert "jedi" in out and "search_files" in out
