"""测试不只认 pytest：按项目认测试工具，run_command 跑的测试命令也算测试结果。

以前非 Python 项目改了代码，测试永远标不成通过（run_tests 只会跑 pytest），任务每次都以
"仍有未验证事项"收尾。可执行文件一律打桩（shutil.which / subprocess.run），不依赖本机装了什么。
"""
import json
import subprocess

import pytest

from src import session, test_runners as tr, tools


@pytest.fixture
def which(monkeypatch):
    """假装这些命令都装了：which(x) → 一个假路径。"""
    monkeypatch.setattr(tr.shutil, "which", lambda name: f"C:/bin/{name}.exe")


def _npm_project(d, script="vitest run"):
    (d / "package.json").write_text(json.dumps({"scripts": {"test": script}}), encoding="utf-8")


# ── 认测试工具 ─────────────────────────────────────────────

def test_python_project_stays_on_pytest(tmp_path, which):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    runner, _ = tr.detect(str(tmp_path), str(tmp_path), python="py")
    assert runner.name == "pytest" and runner.argv[:3] == ["py", "-m", "pytest"]


def test_no_markers_falls_back_to_pytest_like_before(tmp_path, which):
    runner, _ = tr.detect(str(tmp_path), str(tmp_path), python="py")
    assert runner.name == "pytest"


@pytest.mark.parametrize("files, name, args", [
    ({"pom.xml": ""}, "maven", ["-B", "test"]),
    ({"build.gradle.kts": ""}, "gradle", ["test"]),
    ({"go.mod": "module x"}, "go", ["test", "./..."]),
    ({"Cargo.toml": ""}, "cargo", ["test"]),
    ({"App.csproj": ""}, "dotnet", ["test"]),
])
def test_each_ecosystem_is_recognised(tmp_path, which, files, name, args):
    for fname, text in files.items():
        (tmp_path / fname).write_text(text, encoding="utf-8")
    runner, why = tr.detect(str(tmp_path), str(tmp_path))
    assert runner is not None, why
    assert runner.name == name and runner.argv[1:] == args and runner.cwd == str(tmp_path)


def test_node_picks_the_package_manager_from_the_lockfile(tmp_path, which):
    _npm_project(tmp_path)
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    runner, _ = tr.detect(str(tmp_path), str(tmp_path), k="login")
    assert runner.name == "pnpm" and runner.argv[1:] == ["test", "--", "-t", "login"]


def test_npm_placeholder_script_is_not_a_test_suite(tmp_path, which):
    _npm_project(tmp_path, 'echo "Error: no test specified" && exit 1')
    runner, _ = tr.detect(str(tmp_path), str(tmp_path))
    assert runner.name == "pytest", "npm init 的占位脚本等于没有测试"


def test_project_wrapper_scripts_win_over_global_tools(tmp_path, which):
    (tmp_path / "pom.xml").write_text("", encoding="utf-8")
    wrapper = tmp_path / ("mvnw.cmd" if tr.os.name == "nt" else "mvnw")
    wrapper.write_text("", encoding="utf-8")
    runner, _ = tr.detect(str(tmp_path), str(tmp_path), paths=[str(tmp_path / "src" / "UserTest.java")])
    assert runner.argv[0] == str(wrapper) and runner.argv[-1] == "-Dtest=UserTest"


def test_path_extension_picks_the_tool_in_a_mixed_repo(tmp_path, which):
    """前端 + 后端放在一个仓库：给了 .ts 文件就跑前端的测试，不拿 pytest 去跑它。"""
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    web = tmp_path / "web"
    web.mkdir()
    _npm_project(web)
    runner, _ = tr.detect(str(web), str(tmp_path), paths=[str(web / "src" / "a.test.ts")])
    assert runner.name == "npm" and runner.cwd == str(web)
    assert "src/a.test.ts" in runner.argv


def test_missing_tool_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(tr.shutil, "which", lambda name: None)
    (tmp_path / "go.mod").write_text("module x", encoding="utf-8")
    runner, why = tr.detect(str(tmp_path), str(tmp_path))
    assert runner is None and "没装 go" in why


def test_cmd_wrappers_refuse_special_characters(tmp_path, monkeypatch):
    """.cmd / .bat 由 cmd.exe 解释，参数里的 & | " 能拼出另一条命令。"""
    monkeypatch.setattr(tr.shutil, "which", lambda name: f"C:/bin/{name}.cmd")
    _npm_project(tmp_path)
    runner, why = tr.detect(str(tmp_path), str(tmp_path), k='x" & calc & "')
    assert runner is None and "特殊字符" in why


# ── 认出 run_command 里的测试命令 ───────────────────────────

@pytest.mark.parametrize("command, name", [
    ("python -m pytest scripts -q 2>&1", "pytest"),
    (r"C:\Python\python.exe -m pytest", "pytest"),
    ("npm test", "node"),
    ("npm run test -- --run", "node"),
    ("npx vitest run", "node"),
    ("mvn -q test", "maven"),
    (r".\mvnw.cmd test -Dtest=UserTest", "maven"),
    ("gradlew.bat test", "gradle"),
    ("go test ./...", "go"),
    ("cargo test", "cargo"),
    ("dotnet test", "dotnet"),
    ('set "CI=1" && npm test', "node"),
])
def test_test_commands_are_recognised(command, name):
    assert tr.recognize(command, "D:/p")[0] == name


@pytest.mark.parametrize("command", [
    "npm test | tail -5",            # 管道：退出码是最后一段的
    "npm test || echo failed",
    "npm test && rm -rf dist",       # 测试后面还接了别的命令
    "npm install",
    "echo go test",
    "python build.py",
])
def test_other_commands_are_not_test_results(command):
    assert tr.recognize(command, "D:/p") is None


def test_cd_prefix_gives_the_real_directory():
    name, where = tr.recognize("cd /d web && npm test", "D:/p")
    assert name == "node" and where.replace("\\", "/").endswith("D:/p/web")


# ── 接到验证状态上 ──────────────────────────────────────────

def test_run_tests_runs_the_detected_tool_and_counts(project_dir, monkeypatch, which):
    _npm_project(project_dir)
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, cwd=kwargs.get("cwd"), env=kwargs.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, stdout="Tests  3 passed (3)\n", stderr="")

    monkeypatch.setattr(tools, "_run_test_process", fake_run)
    out = tools.run_tests.func("")
    v = session.get_verification()
    assert seen["argv"][1] == "test" and seen["env"].get("CI") == "true", "jest / vitest 别进 watch 模式"
    assert "测试通过" in out and "Tests  3 passed" in out
    assert v["tests_passed"] is True and v["evidence"][-1]["checker"] == "npm"


def test_run_tests_failure_is_a_failure(project_dir, monkeypatch, which):
    (project_dir / "go.mod").write_text("module x", encoding="utf-8")
    monkeypatch.setattr(tools, "_run_test_process", lambda argv, **k: subprocess.CompletedProcess(
        argv, 1, stdout="--- FAIL: TestLogin (0.00s)\nFAIL\n", stderr=""))
    out = tools.run_tests.func("")
    assert "没通过" in out and "--- FAIL: TestLogin" in out
    assert session.get_verification()["tests_passed"] is False


def test_a_test_command_run_through_run_command_counts(project_dir):
    """模型自己用 run_command 跑 `mvn test` 通过了，也要算测试通过（以前不算）。"""
    tools._record_command_test_result("mvn -B test", str(project_dir), 0, "BUILD SUCCESS", 0.0)
    v = session.get_verification()
    assert v["tests_passed"] is True and v["evidence"][-1]["checker"] == "maven"
    tools._record_command_test_result("npm install", str(project_dir), 0, "", 0.0)
    assert v["evidence"][-1]["checker"] == "maven", "不是测试命令就不记"
