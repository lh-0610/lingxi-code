"""非 Python 代码的静态检查（src/static_checks.py）：编辑后的语法提示 + check_code 的编译 / 类型检查。

语法提示只是提示（tree-sitter 偶尔不认合法的新写法），不能记成检查没通过；编译检查只拿这次改过的
文件里的错误拦人，项目原来就有的错误照实说但不拦。
"""
import os
import shutil
import sys

import pytest

from src import codeintel, config, static_checks as sc
from src.static_checks import Checker

requires_pack = pytest.mark.skipif(codeintel._ts_pack is None, reason="tree-sitter-language-pack not installed")

_BROKEN_JAVA = "public class A {\n  void f() {\n    int x = (1 + 2;\n  }\n}\n"
_GOOD_JAVA = "public class A {\n  void f() {\n    int x = (1 + 2);\n  }\n}\n"


# ── 语法提示 ─────────────────────────────────────────────────────

@requires_pack
def test_syntax_errors_point_at_the_broken_line():
    errors = sc.syntax_errors(_BROKEN_JAVA, "A.java")
    assert errors and errors[0][0] == 3
    assert sc.syntax_errors(_GOOD_JAVA, "A.java") == []


@requires_pack
def test_languages_with_noisy_grammars_are_not_checked():
    """C / C++ 七成真实文件都报错（宏、预处理），Kotlin 接口里的抽象方法也报：干脆不查。"""
    assert sc.syntax_errors("int f( {", "a.c") == []
    assert sc.syntax_errors("interface B { fun z() }", "a.kt") == []
    assert sc.syntax_errors("export declare const x: import('./a').T;\n(", "types.d.ts") == []


@requires_pack
def test_hints_only_report_errors_the_edit_introduced():
    before = _BROKEN_JAVA
    assert sc.syntax_hints("A.java", before, before) == "", "改之前就有的不算这次的"
    after = _BROKEN_JAVA.replace("  }\n}\n", "  }\n  void g() { if (x { }\n}\n")
    hint = sc.syntax_hints("A.java", before, after)
    assert "语法提示" in hint and "第 5 行" in hint and "第 3 行" not in hint
    assert sc.syntax_hints("A.java", None, _GOOD_JAVA) == ""


def test_vue_script_block_errors_use_file_line_numbers():
    code = "<template>\n  <div/>\n</template>\n<script>\nfunction f( {\n</script>\n"
    errors = sc.syntax_errors(code, "A.vue")
    assert errors and errors[0][0] == 5


# ── 认检查工具 ───────────────────────────────────────────────────

def test_jsonc_with_comments_trailing_commas_and_glob_paths(tmp_path):
    p = tmp_path / "tsconfig.json"
    p.write_text('{\n  // 注释\n  "compilerOptions": { "paths": { "@/*": ["./src/*"] }, },\n  /* 块注释 */\n}\n',
                 encoding="utf-8")
    assert sc._load_jsonc(str(p)) == {"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}


def test_vite_style_tsconfig_picks_the_referenced_config(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{ "files": [], "references": [{ "path": "./tsconfig.node.json" }, { "path": "./tsconfig.app.json" }] }',
        encoding="utf-8")
    (tmp_path / "tsconfig.app.json").write_text('{ "include": ["src"] }', encoding="utf-8")
    (tmp_path / "tsconfig.node.json").write_text('{ "include": ["vite.config.ts"] }', encoding="utf-8")
    picked = sc._tsconfig_for(str(tmp_path / "src" / "a.ts"), str(tmp_path))
    assert os.path.basename(picked) == "tsconfig.app.json"


def test_detect_explains_what_is_missing(tmp_path, monkeypatch):
    root = str(tmp_path)
    assert sc.detect(str(tmp_path / "a.go"), root) == (None, "没找到 go.mod")
    assert sc.detect(str(tmp_path / "a.ts"), root) == (None, "没找到 tsconfig.json")
    assert sc.detect(str(tmp_path / "A.cs"), root) == (None, "没找到 .csproj / .sln")
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    monkeypatch.setattr(sc.shutil, "which", lambda name: None)
    checker, reason = sc.detect(str(tmp_path / "A.java"), root)
    assert checker is None and "mvn" in reason
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sc.shutil, "which", lambda name: "node")
    checker, reason = sc.detect(str(tmp_path / "a.ts"), root)
    assert checker is None and "npm install" in reason, "不用 npx：它找不到会去网上下载"


def test_detect_uses_project_wrappers_and_test_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(sc.shutil, "which", lambda name: None)
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    wrapper = tmp_path / ("mvnw.cmd" if os.name == "nt" else "mvnw")
    wrapper.write_text("", encoding="utf-8")
    test_file = tmp_path / "src" / "test" / "java" / "ATest.java"
    checker, _ = sc.detect(str(test_file), str(tmp_path))
    assert checker.argv[0] == str(wrapper) and checker.argv[-1] == "test-compile"


# ── 解析编译输出 ─────────────────────────────────────────────────

@pytest.mark.parametrize("name, line, expected", [
    ("tsc", "src/a.ts(12,5): error TS2322: Type 'string' is not assignable", ("a.ts", 12, "error TS2322")),
    ("go vet", "./sub/a.go:3:34: undefined: undefinedThing", ("a.go", 3, "undefined: undefinedThing")),
    ("cargo check", "src/main.rs:2:18: error[E0308]: mismatched types", ("main.rs", 2, "error[E0308]")),
    ("maven", "[ERROR] /D:/p/src/main/java/A.java:[7,9] cannot find symbol", ("A.java", 7, "cannot find symbol")),
    ("gradle", "e: file:///D:/p/src/main/kotlin/B.kt:4:5 Unresolved reference: foo", ("B.kt", 4, "Unresolved")),
    ("dotnet build", r"D:\p\C.cs(9,13): error CS0103: The name 'y' does not exist [D:\p\C.csproj]",
     ("C.cs", 9, "error CS0103")),
])
def test_error_locations_in_each_tool_output(tmp_path, name, line, expected):
    errors = sc.parse_errors(line, Checker(name, [], str(tmp_path)), str(tmp_path))
    assert len(errors) == 1
    path, lineno, msg = errors[0]
    assert os.path.basename(path) == expected[0] and lineno == expected[1] and msg.startswith(expected[2])


def test_warnings_and_duplicates_are_dropped(tmp_path):
    out = ("src/main.rs:3:9: warning: unused variable: `x`\n"
           "[ERROR] /D:/p/A.java:[7,9] cannot find symbol\n"
           "[ERROR] /D:/p/A.java:[7,9] cannot find symbol\n")
    assert len(sc.parse_errors(out, Checker("maven", [], str(tmp_path)), str(tmp_path))) == 1


def _fake(tmp_path, output, code):
    script = tmp_path / "fake_checker.py"
    script.write_text(f"import sys\nsys.stdout.write({output!r})\nsys.exit({code})\n", encoding="utf-8")
    return Checker("tsc", [sys.executable, str(script)], str(tmp_path))


def test_only_errors_in_changed_files_fail_the_check(tmp_path):
    for name in ("a.ts", "old.ts"):
        (tmp_path / name).write_text("", encoding="utf-8")
    checker = _fake(tmp_path, "a.ts(2,1): error TS1: new\nold.ts(5,1): error TS2: legacy\n", 2)
    mine = sc.run(checker, str(tmp_path / "a.ts"), [], str(tmp_path))
    assert mine.status == "failed" and "a.ts:2" in mine.issues and "old.ts" not in mine.issues
    assert "old.ts:5" in mine.note and "原来就有" in mine.note
    other = sc.run(checker, str(tmp_path / "b.ts"), [], str(tmp_path))
    assert other.status == "passed", "项目里原来就有的错误不拦着交差"
    changed_elsewhere = sc.run(checker, str(tmp_path / "b.ts"), [str(tmp_path / "old.ts")], str(tmp_path))
    assert changed_elsewhere.status == "failed", "这次改过的别的文件里出错也算（改签名把调用处弄坏了）"


def test_a_checker_that_dies_is_not_a_failure(tmp_path):
    result = sc.run(_fake(tmp_path, "could not resolve dependencies\n", 1), str(tmp_path / "a.ts"), [], str(tmp_path))
    assert result.status == "error" and "没跑完" in result.note


@pytest.mark.skipif(not shutil.which("go"), reason="go not installed")
def test_real_go_vet(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "go.mod").write_text("module example.com/m\n\ngo 1.21\n", encoding="utf-8")
    (tmp_path / "sub" / "a.go").write_text("package sub\n\nfunc Add(a int) int { return a + nope }\n",
                                           encoding="utf-8")
    target = str(tmp_path / "sub" / "a.go")
    checker, _ = sc.detect(target, str(tmp_path))
    result = sc.run(checker, target, [], str(tmp_path))
    assert result.status == "failed" and "sub/a.go:3" in result.issues and "nope" in result.issues


# ── 接进工具 ─────────────────────────────────────────────────────

@requires_pack
def test_write_file_hints_but_does_not_fail_the_check(project_dir, monkeypatch):
    from src import session, tools
    monkeypatch.setattr(config, "CHECK_COMMAND", "")
    monkeypatch.setattr(config, "AUTO_CHECK_AFTER_EDIT", True)
    out = tools.write_file.func("A.java", _BROKEN_JAVA)
    assert "成功写入" in out and "语法提示" in out and "第 3 行" in out
    check = session.get_verification()["checks"].get("A.java")
    assert check is None or check["passed"] is None, "语法提示不能把完成闸门卡住"
    out = tools.edit_file.func("A.java", "int x = (1 + 2;", "int x = (1 + 2);")
    assert "语法提示" not in out


def test_check_code_explains_when_there_is_no_compiler(project_dir, monkeypatch):
    from src import tools
    monkeypatch.setattr(config, "CHECK_COMMAND", "")
    (project_dir / "main.go").write_text("package main\n", encoding="utf-8")
    out = tools.check_code.func("main.go")
    assert "没有可用的检查器" in out and "go.mod" in out


def test_check_code_failure_marks_the_check_and_offers_repair(project_dir, monkeypatch):
    from src import session, tools
    monkeypatch.setattr(config, "CHECK_COMMAND", "")
    (project_dir / "a.ts").write_text("export const x: number = 'no';\n", encoding="utf-8")
    checker = _fake(project_dir, "a.ts(1,14): error TS2322: Type 'string' is not assignable to type 'number'.\n", 2)
    monkeypatch.setattr(sc, "detect", lambda path, root: (checker, ""))
    out = tools.check_code.func("a.ts")
    assert "tsc 检查发现问题" in out and "a.ts:1" in out and "[REPAIR_INFO]" in out
    v = session.get_verification()
    assert v["checks"]["a.ts"]["passed"] is False
    assert any(r["kind"] == "check" and r["status"] == "failed" for r in v["evidence"])
