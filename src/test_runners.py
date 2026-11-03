"""认项目用的是什么测试工具：让 run_tests 和完成闸门不只认 pytest。

以前 run_tests 一律跑 `python -m pytest`，完成闸门又只认 run_tests 的结果——在 Java / Go / JS
项目里改了代码，测试永远标不成通过，任务每次都以"仍有未验证事项"收尾；模型自己用命令跑
`mvn test`、`npm test` 也不算数。

两件事：
- `detect`：按项目里的标志文件认出测试工具，给 run_tests 用。认不出、或没装对应的命令时
  返回原因，不瞎猜。Python 项目（有 pyproject / pytest.ini 等，或什么标志都没有）照旧 pytest。
- `recognize`：认出 run_command 里跑的是不是测试命令（只由 cd / set 前缀加一条测试命令组成），
  是的话那条命令的退出码就算测试证据。run_command 要用户确认，任意命令都走它；run_tests
  不问用户，所以只跑这里认出来的固定命令，不接受模型给的整条命令。
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field


@dataclass
class Runner:
    name: str               # pytest / npm / pnpm / yarn / maven / gradle / go / cargo / dotnet
    argv: list = field(default_factory=list)
    cwd: str = ""           # 在哪个目录跑（找到标志文件的那一层）


# 路径扩展名 → 该用哪一类工具（给了 path 时按它选，混合项目里不会拿 pytest 去跑 .ts）
_EXT_KIND = {
    ".py": "python",
    ".js": "node", ".jsx": "node", ".ts": "node", ".tsx": "node", ".mjs": "node",
    ".cjs": "node", ".vue": "node", ".svelte": "node",
    ".java": "jvm", ".kt": "jvm", ".kts": "jvm", ".groovy": "jvm", ".scala": "jvm",
    ".go": "go",
    ".rs": "rust",
    ".cs": "dotnet", ".fs": "dotnet", ".vb": "dotnet",
}
_PY_MARKERS = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "setup.py", "conftest.py")
# npm init 生成的占位脚本：等于"没有测试"
_NPM_PLACEHOLDER = "no test specified"
# .cmd / .bat 由 cmd.exe 解释，参数里这些字符能拼出另一条命令（BatBadBut），一律不收
_CMD_UNSAFE = re.compile(r'["&|<>^%!\r\n]')


def _kind_of(path: str) -> str:
    return _EXT_KIND.get(os.path.splitext(path or "")[1].lower(), "")


def _dirs_up(start: str, root: str):
    """从 start 往上到 root（含）逐层；start 不在 root 里就只看 start。"""
    d = os.path.realpath(start)
    root = os.path.realpath(root or start)
    while True:
        yield d
        if d == root:
            return
        parent = os.path.dirname(d)
        try:
            if parent == d or os.path.commonpath([parent, root]) != root:
                return
        except ValueError:
            return
        d = parent


def _has(d: str, *names: str) -> bool:
    return any(os.path.isfile(os.path.join(d, n)) for n in names)


def _npm_test_script(d: str) -> str | None:
    try:
        with open(os.path.join(d, "package.json"), encoding="utf-8") as f:
            scripts = (json.load(f) or {}).get("scripts") or {}
    except (OSError, ValueError):
        return None
    test = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(test, str) or not test.strip() or _NPM_PLACEHOLDER in test:
        return None
    return test


def _local(d: str, *names: str) -> str | None:
    """项目里自带的包装脚本（mvnw / gradlew），Windows 优先 .cmd / .bat。"""
    for n in names:
        p = os.path.join(d, n)
        if os.path.isfile(p):
            return p
    return None


def _found_kinds(d: str) -> list[str]:
    kinds = []
    if _npm_test_script(d) is not None:
        kinds.append("node")
    if _has(d, "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"):
        kinds.append("jvm")
    if _has(d, "go.mod"):
        kinds.append("go")
    if _has(d, "Cargo.toml"):
        kinds.append("rust")
    try:
        if any(n.endswith((".sln", ".csproj", ".fsproj")) for n in os.listdir(d)):
            kinds.append("dotnet")
    except OSError:
        pass
    return kinds


def detect(cwd: str, root: str, paths=(), k: str = "", python: str = "python"):
    """认出该用哪个测试工具。返回 (Runner, "") 或 (None, 认不出 / 跑不了的原因)。

    paths 是已经校验过、在项目里的绝对路径；k 是按名字筛用例。
    """
    paths = list(paths or [])
    want = _kind_of(paths[0]) if paths else ""
    for d in _dirs_up(cwd, root):
        if want in ("", "python") and _has(d, *_PY_MARKERS):
            return _pytest(python, d, cwd, paths, k), ""
        kinds = _found_kinds(d)
        if want and want != "python":
            if want in kinds:
                return _build(want, d, paths, k)
            continue
        if kinds:
            return _build(kinds[0], d, paths, k)
    if want and want != "python":
        return None, (f"没在项目里找到 {want} 项目的测试配置（{_KIND_HINT[want]}），"
                      "用 run_command 跑这个项目的测试命令（会算作测试结果）")
    # 什么标志都没有：照旧 pytest（以前的行为；没有用例时 pytest 会如实说没收集到）
    return _pytest(python, cwd, cwd, paths, k), ""


_KIND_HINT = {
    "node": "package.json 里没有 test 脚本",
    "jvm": "没有 pom.xml / build.gradle",
    "go": "没有 go.mod",
    "rust": "没有 Cargo.toml",
    "dotnet": "没有 .sln / .csproj",
}


def _pytest(python, where, cwd, paths, k):
    argv = [python, "-m", "pytest", "--tb=short", "-q", *paths]
    if k:
        argv += ["-k", k]
    # pytest 照旧在 shell 工作目录跑（以前就是这样，rootdir 由 pytest 自己往上找）
    return Runner("pytest", argv, cwd)


def _rel(paths, d):
    out = []
    for p in paths:
        try:
            out.append(os.path.relpath(p, d).replace("\\", "/"))
        except ValueError:
            out.append(p)
    return out


def _build(kind, d, paths, k):
    rel = _rel(paths, d)
    if kind == "node":
        if _has(d, "pnpm-lock.yaml"):
            pm = "pnpm"
        elif _has(d, "yarn.lock"):
            pm = "yarn"
        else:
            pm = "npm"
        exe = shutil.which(pm)
        if not exe:
            return None, f"找到了 package.json 的 test 脚本，但没装 {pm}"
        extra = [*rel, *(["-t", k] if k else [])]
        argv = [exe, "test", *(([] if pm == "yarn" else ["--"]) + extra if extra else [])]
        return _checked(Runner(pm, argv, d))
    if kind == "jvm":
        classes = [os.path.splitext(os.path.basename(p))[0] for p in paths if p.endswith((".java", ".kt"))]
        selector = k or ",".join(classes)
        if _has(d, "pom.xml"):
            exe = _local(d, "mvnw.cmd", "mvnw") if os.name == "nt" else _local(d, "mvnw")
            exe = exe or shutil.which("mvn")
            if not exe:
                return None, "找到了 pom.xml，但没有 mvn，也没有项目自带的 mvnw"
            argv = [exe, "-B", "test", *([f"-Dtest={selector}"] if selector else [])]
            return _checked(Runner("maven", argv, d))
        exe = _local(d, "gradlew.bat", "gradlew") if os.name == "nt" else _local(d, "gradlew")
        exe = exe or shutil.which("gradle")
        if not exe:
            return None, "找到了 build.gradle，但没有 gradle，也没有项目自带的 gradlew"
        argv = [exe, "test", *(["--tests", selector] if selector else [])]
        return _checked(Runner("gradle", argv, d))
    if kind == "go":
        exe = shutil.which("go")
        if not exe:
            return None, "找到了 go.mod，但没装 go"
        targets = []
        for r in rel or ["."]:
            r = os.path.dirname(r) if r.endswith(".go") else r
            targets.append("./" + (r.strip("./") + "/..." if r.strip("./") else "..."))
        return _checked(Runner("go", [exe, "test", *targets, *(["-run", k] if k else [])], d))
    if kind == "rust":
        exe = shutil.which("cargo")
        if not exe:
            return None, "找到了 Cargo.toml，但没装 cargo"
        return _checked(Runner("cargo", [exe, "test", *([k] if k else [])], d))
    if kind == "dotnet":
        exe = shutil.which("dotnet")
        if not exe:
            return None, "找到了 .sln / .csproj，但没装 dotnet"
        return _checked(Runner("dotnet", [exe, "test", *(["--filter", k] if k else [])], d))
    return None, "认不出这个项目用什么测试工具"


def _checked(runner):
    """经 cmd.exe 解释的包装脚本（.cmd / .bat）不收带特殊字符的参数。"""
    if runner.argv and runner.argv[0].lower().endswith((".cmd", ".bat")):
        if any(_CMD_UNSAFE.search(a) for a in runner.argv[1:]):
            return None, "测试参数里有 cmd 的特殊字符（引号、&、| 等），不能安全地传给这个测试工具"
    return runner, ""


# ── 认出 run_command 里跑的测试命令 ─────────────────────────────

_TEST_COMMANDS = (
    ("pytest", r"(?:python3?|py)(?:\.exe)?\s+(?:-\S+\s+)*-m\s+pytest\b|pytest(?:\.exe)?\b"),
    ("node", r"(?:npm|pnpm|yarn|bun)(?:\.cmd)?\s+(?:run\s+)?test\b|npx\s+(?:vitest|jest)\b|(?:vitest|jest)\b"),
    ("maven", r"(?:\.[\\/])?mvnw(?:\.cmd)?\s+(?:.*\s)?(?:test|verify)\b|mvn(?:\.cmd)?\s+(?:.*\s)?(?:test|verify)\b"),
    ("gradle", r"(?:\.[\\/])?gradlew(?:\.bat)?\s+(?:.*\s)?test\b|gradle\s+(?:.*\s)?test\b"),
    ("go", r"go\s+test\b"),
    ("cargo", r"cargo\s+test\b"),
    ("dotnet", r"dotnet\s+test\b"),
    ("ctest", r"ctest\b"),
)
# 允许带路径写（C:\...\python.exe -m pytest、./node_modules/.bin/vitest）
_TEST_RES = [(name, re.compile(rf'^"?(?:[^\s"]*[\\/])?(?:{pat})', re.IGNORECASE))
             for name, pat in _TEST_COMMANDS]
# 命令分段：&& / ; / 单个 &（cmd 的顺序执行）；2>&1 这类重定向里的 & 不算
_SPLIT = re.compile(r"\s*(?:&&|;|(?<![<>&0-9])&(?![&0-9>]))\s*")
_CD = re.compile(r'^(?:cd|chdir|pushd)(?:\s+/d)?\s+"?([^"]+?)"?\s*$', re.IGNORECASE)
_SET = re.compile(r"^set\s+", re.IGNORECASE)


def recognize(command: str, cwd: str):
    """这条命令是不是"跑一次测试"。是就返回 (工具名, 实际运行目录)，否则 None。

    只认"cd / set 前缀 + 一条测试命令"：管道（|）和 || 会改掉退出码的含义，
    测试后面再接别的命令时退出码也不再是测试的结果——这些都不算。
    """
    text = (command or "").strip()
    if not text or "||" in text or re.search(r"(?<![|])\|(?![|])", text):
        return None
    parts = [p for p in _SPLIT.split(text) if p.strip()]
    where = cwd
    for part in parts[:-1]:
        m = _CD.match(part.strip())
        if m:
            where = os.path.normpath(os.path.join(where, m.group(1).strip()))
        elif not _SET.match(part.strip()):
            return None
    last = parts[-1].strip() if parts else ""
    last = re.sub(r"\s+\d?>\s*&?\s*\S+\s*$", "", last)     # 去掉末尾 2>&1 / >log 这类重定向
    for name, rx in _TEST_RES:
        if rx.match(last):
            return name, where
    return None


# ── 非 pytest 工具的结果摘要 ───────────────────────────────────

# 各家"一共几个、挂了几个"的那一行；摘出来放在最前面，其余给输出末尾
_SUMMARY_LINES = re.compile(
    r"^\s*(Tests?:\s|Test Files\s|Tests run:|test result:|ok\s+\S|FAIL\b|--- FAIL|BUILD (SUCCESS|FAILURE)|"
    r"Passed!|Failed!|\d+ (passing|failing|pending)\b|Total tests:|FAILED\b)",
    re.IGNORECASE)
_TAIL_LINES = 40


def summarize(name: str, output: str, returncode: int, elapsed: float) -> str:
    lines = (output or "").splitlines()
    picked = [ln.strip() for ln in lines if _SUMMARY_LINES.match(ln)][-12:]
    head = (f"✅ {name} 测试通过（{elapsed:.1f}s，退出码 0）" if returncode == 0
            else f"❌ {name} 测试没通过（{elapsed:.1f}s，退出码 {returncode}）")
    parts = [head]
    if picked:
        parts.append("结果摘要：\n" + "\n".join(picked))
    tail = lines[-_TAIL_LINES:]
    if tail:
        parts.append(f"输出最后 {len(tail)} 行：\n" + "\n".join(tail))
    return "\n\n".join(parts)
