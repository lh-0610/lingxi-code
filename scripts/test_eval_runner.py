"""评测框架里**确定性**部分的单测：判定器 + case 加载。

agent 跑批本身会真实调模型、慢且随机，不进常规套件；但判定器是纯函数，必须测——
判定错了整套评测的结论就是错的，而这种错比没有评测更危险（给出虚假的安全感）。
"""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "evals"))
from runner import ASSERTIONS, evaluate, load_cases  # noqa: E402


@pytest.fixture()
def pair(tmp_path):
    """造一对 (fixture 原件, 工作副本)。"""
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "a.py").write_text("print(1)\n", encoding="utf-8")
    (fx / "test_a.py").write_text("assert True\n", encoding="utf-8")
    wk = tmp_path / "wk"
    shutil.copytree(fx, wk)
    return str(fx), str(wk)


class TestFileUnchanged:
    """防作弊判定：模型偷改测试文件必须被抓住。"""

    def test_identical_passes(self, pair):
        fx, wk = pair
        ok, msg = ASSERTIONS["file_unchanged"](
            {"path": "test_a.py", "_fixture": fx}, wk, "")
        assert ok, msg

    def test_modified_fails(self, pair):
        fx, wk = pair
        (open(os.path.join(wk, "test_a.py"), "w", encoding="utf-8")).write("assert False\n")
        ok, msg = ASSERTIONS["file_unchanged"]({"path": "test_a.py", "_fixture": fx}, wk, "")
        assert not ok and "被改动" in msg

    def test_deleted_fails(self, pair):
        """直接删掉测试文件也是作弊的一种，pytest 照样会绿。"""
        fx, wk = pair
        os.remove(os.path.join(wk, "test_a.py"))
        ok, msg = ASSERTIONS["file_unchanged"]({"path": "test_a.py", "_fixture": fx}, wk, "")
        assert not ok and "删除" in msg


class TestNoFileModified:
    """Plan 模式那类负面 case 的守门判定。"""

    def test_clean_copy_passes(self, pair):
        fx, wk = pair
        ok, msg = ASSERTIONS["no_file_modified"]({"_fixture": fx}, wk, "")
        assert ok, msg

    def test_content_change_caught(self, pair):
        fx, wk = pair
        (open(os.path.join(wk, "a.py"), "w", encoding="utf-8")).write("print(2)\n")
        ok, msg = ASSERTIONS["no_file_modified"]({"_fixture": fx}, wk, "")
        assert not ok and "a.py" in msg

    def test_new_file_caught(self, pair):
        """新增文件同样算改动——否则模型"另存一份改好的"就能蒙混过关。"""
        fx, wk = pair
        (open(os.path.join(wk, "sneaky.py"), "w", encoding="utf-8")).write("x=1\n")
        ok, msg = ASSERTIONS["no_file_modified"]({"_fixture": fx}, wk, "")
        assert not ok and "sneaky.py" in msg

    def test_deletion_caught(self, pair):
        fx, wk = pair
        os.remove(os.path.join(wk, "a.py"))
        ok, _ = ASSERTIONS["no_file_modified"]({"_fixture": fx}, wk, "")
        assert not ok

    def test_pycache_ignored(self, pair):
        """跑一次命令就会生成 __pycache__，不该被判成"改动了工作区"。"""
        fx, wk = pair
        os.makedirs(os.path.join(wk, "__pycache__"), exist_ok=True)
        (open(os.path.join(wk, "__pycache__", "a.pyc"), "w")).write("x")
        ok, msg = ASSERTIONS["no_file_modified"]({"_fixture": fx}, wk, "")
        assert ok, msg


class TestCommandAssertion:
    def test_exit_code_match(self, tmp_path):
        ok, msg = ASSERTIONS["command"](
            {"run": "python -c \"import sys; sys.exit(0)\"", "exit_code": 0}, str(tmp_path), "")
        assert ok, msg

    def test_exit_code_mismatch(self, tmp_path):
        ok, msg = ASSERTIONS["command"](
            {"run": "python -c \"import sys; sys.exit(3)\"", "exit_code": 0}, str(tmp_path), "")
        assert not ok and "3" in msg


class TestOutputAssertions:
    def test_contains_requires_all(self):
        ok, _ = ASSERTIONS["output_contains"]({"contains": ["a.py", "b.py"]}, "", "见 a.py")
        assert not ok
        ok, _ = ASSERTIONS["output_contains"]({"contains": ["a.py", "b.py"]}, "", "a.py 和 b.py")
        assert ok

    def test_contains_any_needs_one(self):
        spec = {"contains_any": ["没有", "不存在", "未找到"]}
        ok, _ = ASSERTIONS["output_contains_any"](spec, "", "项目里不存在这个函数")
        assert ok
        ok, _ = ASSERTIONS["output_contains_any"](spec, "", "它的实现如下：def foo()")
        assert not ok

    def test_case_insensitive(self):
        ok, _ = ASSERTIONS["output_contains"]({"contains": ["MARKER_ABC"]}, "", "marker_abc")
        assert ok


class TestEvaluate:
    def test_all_must_pass(self, pair):
        fx, wk = pair
        case = {"assert": [
            {"type": "no_file_modified"},
            {"type": "output_contains", "contains": ["缺这个"]},
        ]}
        passed, details = evaluate(case, wk, fx, "无关输出")
        assert not passed and len(details) == 2

    def test_unknown_type_fails_loud(self, pair):
        """判定类型写错必须判失败，绝不能静默跳过——静默跳过等于这条约束消失了。"""
        fx, wk = pair
        passed, details = evaluate({"assert": [{"type": "typo_here"}]}, wk, fx, "")
        assert not passed and "未知判定类型" in details[0][1]

    def test_assertion_exception_is_failure(self, pair):
        """判定器自己抛异常算失败，不能让整轮评测崩掉。"""
        fx, wk = pair
        passed, details = evaluate(
            {"assert": [{"type": "file_contains", "path": "nope.py", "contains": ["x"]}]},
            wk, fx, "")
        assert not passed


class TestCases:
    def test_shipped_cases_are_valid(self):
        """随仓库带的 case 必须结构合法、判定类型都认识、fixture 都存在。"""
        cases = load_cases()
        assert len(cases) >= 5
        fx_dir = os.path.join(os.path.dirname(__file__), "evals", "fixtures")
        for c in cases:
            assert c.get("name") and c.get("prompt"), c
            assert c.get("mode") in ("act", "plan"), c
            assert os.path.isdir(os.path.join(fx_dir, c["fixture"])), c["fixture"]
            assert c.get("assert"), f"{c['id']} 没有任何判定"
            for spec in c["assert"]:
                assert spec["type"] in ASSERTIONS, spec["type"]

    def test_broken_calc_fixture_actually_fails(self):
        """001 的 fixture 必须真的是坏的——fixture 本身就绿的话这个 case 恒通过、毫无意义。"""
        import subprocess
        fx = os.path.join(os.path.dirname(__file__), "evals", "fixtures", "broken-calc")
        r = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=fx,
                           capture_output=True, text=True, timeout=120)
        assert r.returncode != 0, "broken-calc fixture 居然是全绿的"
