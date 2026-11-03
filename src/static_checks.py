"""非 Python 代码的静态检查（Python 照旧 ruff + mypy，见 tools._run_code_check）。

两层，可信度不一样，用法也不一样：

- 编辑后的语法提示（syntax_hints）：tree-sitter 解析改后的文件，报这次编辑**新出现**的解析错误。
  不用装任何东西、一瞬间出结果，花括号没配上、引号没闭合这类改坏的地方当场就能看到。
  但它只是提示：在真实代码上量过，Java / JS 没有误报，TS 约 6%（.d.ts 里的 import("…") 类型、
  JSX 属性的 URL 里带 &），C / C++ 七成文件都报错（宏、预处理），Kotlin 接口里的抽象方法也报。
  所以不记成"检查没通过"（那会把完成闸门卡在解析器的毛病上），误报多的语言干脆不查。
- 编译 / 类型检查（detect + run，check_code 调用）：用项目自己的工具——tsc --noEmit、go vet、
  cargo check、Maven / Gradle 编译、dotnet build；单文件能查的用 node --check / php -l / ruby -c。
  这是权威结果，记进验证证据。只有**这次改过的文件**里的错误算没通过：项目原来就有的错误照实
  列出来，但不拦着交差——模型没法、也不该顺手去修一堆和任务无关的旧错误。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field

from . import codeintel
from .test_runners import _checked, _local

# 语法提示只查这些语言：在真实代码上没误报 / 误报很少。C / C++ / Kotlin / Swift 等不查
_SYNTAX_LANGS = {"javascript", "typescript", "tsx", "java", "go", "rust", "csharp",
                 "php", "ruby", "lua", "bash", "powershell"}
_MAX_HINTS = 8
_PROJECT_TIMEOUT = 180      # 第一次 cargo check / mvn compile 要下载依赖、编译依赖，给足时间


# ── 编辑后的语法提示 ─────────────────────────────────────────────

def syntax_errors(text: str, path: str) -> list[tuple[int, str]]:
    """[(行号, 说明)]：tree-sitter 解析不了的地方。不支持的语言、没装语法库返回 []。"""
    lower = path.lower()
    if lower.endswith(".d.ts"):        # 生成的声明文件，import("…") 类型解析器不认
        return []
    ext = os.path.splitext(lower)[1]
    if ext in codeintel._SFC_EXTS:
        blocks = codeintel.script_blocks(text)
    else:
        lang = codeintel._EXT_TO_LANG.get(ext)
        blocks = [(lang, 0, text)] if lang in _SYNTAX_LANGS else []
    out = []
    for lang, offset, code in blocks:
        parser = codeintel._get_parser(lang)
        if parser is None:
            continue
        source = code.encode("utf-8")
        try:
            root = parser.parse(source).root_node
        except Exception:
            continue
        if not root.has_error:
            continue
        stack = [root]
        while stack and len(out) < 50:
            node = stack.pop()
            if node.is_missing:
                out.append((node.start_point[0] + 1 + offset, f"缺少 {node.type}"))
            elif node.is_error:
                snippet = source[node.start_byte:node.end_byte].decode("utf-8", "replace").strip()
                snippet = snippet.splitlines()[0][:60] if snippet else ""
                out.append((node.start_point[0] + 1 + offset, f"解析不了：{snippet}" if snippet else "解析不了"))
            elif node.has_error:
                stack.extend(reversed(node.children))
    return sorted(out)


def syntax_hints(path: str, before: str | None, after: str) -> str:
    """这次编辑新出现的解析错误，拼成追加到工具结果里的提示；没有返回 ""。
    按说明文字比（行号会随编辑移动）：改之前就有的解析错误（多半是解析器不认的写法）不再提。"""
    try:
        errors = syntax_errors(after, path)
        if not errors:
            return ""
        old = Counter(msg for _ln, msg in syntax_errors(before, path)) if before else Counter()
    except Exception:
        return ""
    new = []
    for line, msg in errors:
        if old[msg] > 0:
            old[msg] -= 1
        else:
            new.append((line, msg))
    if not new:
        return ""
    lines = [f"  第 {line} 行：{msg}" for line, msg in new[:_MAX_HINTS]]
    if len(new) > _MAX_HINTS:
        lines.append(f"  …另有 {len(new) - _MAX_HINTS} 处")
    return ("\n\n语法提示（tree-sitter 解析改后的文件，这次编辑后新出现了解析不了的地方，多半是括号 / 引号"
            "没配上；也可能是解析器不认较新的写法，确认没问题可以忽略）：\n" + "\n".join(lines))


# ── 编译 / 类型检查 ──────────────────────────────────────────────

@dataclass
class Checker:
    name: str                 # tsc / vue-tsc / go vet / cargo check / maven / gradle / dotnet build / node --check …
    argv: list = field(default_factory=list)
    cwd: str = ""
    per_file: bool = False    # 只查这一个文件：退出码说了算，输出整段就是问题


@dataclass
class Result:
    status: str               # passed / failed / error / timeout / not_run
    issues: str = ""          # 这次改过的文件里的错误（failed 时）
    note: str = ""            # 补充说明：别的文件里原来就有的错误、没跑完的原因
    exit_code: int | None = None
    duration_ms: int = 0
    error_files: list = field(default_factory=list)   # 有错误的、这次改过的文件（绝对路径）


def _find_up(start: str, root: str, test) -> str | None:
    """从 start 往上找到 root 为止，第一个满足 test(目录) 的目录。"""
    d = os.path.abspath(start)
    root = os.path.abspath(root)
    while True:
        if test(d):
            return d
        if os.path.normcase(d) == os.path.normcase(root):
            return None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _has(name):
    return lambda d: os.path.isfile(os.path.join(d, name))


def _load_jsonc(path: str):
    """tsconfig 允许注释和尾逗号。逐字符去掉字符串外的注释（"@/*" 这种路径里的 /* 不能当注释）。"""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return None
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(ch)
        i += 1
    try:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))
    except ValueError:
        return None


def _tsconfig_for(file_path: str, root: str) -> str | None:
    """离文件最近的 tsconfig.json。Vite 模板那种"files": [] + references 的总配置本身什么都不查，
    改用 include 覆盖了这个文件的那个子配置（tsconfig.app.json）。"""
    d = _find_up(os.path.dirname(file_path), root, _has("tsconfig.json"))
    if d is None:
        return None
    cfg_path = os.path.join(d, "tsconfig.json")
    cfg = _load_jsonc(cfg_path) or {}
    refs = [r.get("path", "") for r in cfg.get("references") or [] if isinstance(r, dict)]
    if not refs or cfg.get("files") != []:
        return cfg_path
    rel = os.path.relpath(file_path, d).replace("\\", "/")
    candidates = []
    for ref in refs:
        p = os.path.normpath(os.path.join(d, ref))
        if os.path.isdir(p):
            p = os.path.join(p, "tsconfig.json")
        if not os.path.isfile(p):
            continue
        candidates.append(p)
        for inc in (_load_jsonc(p) or {}).get("include") or []:
            prefix = str(inc).split("*", 1)[0].removeprefix("./").rstrip("/")
            if not prefix or rel == prefix or rel.startswith(prefix + "/"):
                return p
    return candidates[0] if candidates else cfg_path


def _node_tool(start: str, root: str, rel_js: str) -> str | None:
    """项目 node_modules 里的某个 JS 入口（如 typescript/bin/tsc），往上找（monorepo 会提升到根）。
    直接用 node 跑它，不经 .cmd 包装脚本，也不用 npx（npx 找不到会去网上下载）。"""
    d = _find_up(start, root, lambda d: os.path.isfile(os.path.join(d, "node_modules", rel_js)))
    return os.path.join(d, "node_modules", rel_js) if d else None


def detect(file_path: str, root: str):
    """(Checker, "") 或 (None, 为什么没有)。"""
    ext = os.path.splitext(file_path)[1].lower()
    here = os.path.dirname(file_path)
    if ext in (".ts", ".tsx", ".mts", ".cts", ".vue"):
        cfg = _tsconfig_for(file_path, root)
        if cfg is None:
            return None, "没找到 tsconfig.json"
        node = shutil.which("node")
        if not node:
            return None, "没装 node"
        rel_js = "vue-tsc/bin/vue-tsc.js" if ext == ".vue" else "typescript/bin/tsc"
        tool = _node_tool(os.path.dirname(cfg), root, rel_js)
        if tool is None:
            pkg = "vue-tsc" if ext == ".vue" else "typescript"
            return None, f"项目里没装 {pkg}（node_modules 里找不到），先在项目里 npm install"
        argv = [node, tool, "--noEmit", "--pretty", "false", "-p", cfg]
        if not ((_load_jsonc(cfg) or {}).get("compilerOptions") or {}).get("composite"):
            argv += ["--incremental", "false"]   # 不写 .tsbuildinfo，检查不该在项目里留文件
        return Checker("vue-tsc" if ext == ".vue" else "tsc", argv, os.path.dirname(cfg)), ""
    if ext in (".js", ".mjs", ".cjs"):
        node = shutil.which("node")
        if not node:
            return None, "没装 node"
        return Checker("node --check", [node, "--check", file_path], here, per_file=True), ""
    if ext == ".go":
        d = _find_up(here, root, _has("go.mod"))
        if d is None:
            return None, "没找到 go.mod"
        go = shutil.which("go")
        if not go:
            return None, "找到了 go.mod，但没装 go"
        pkg = os.path.relpath(here, d).replace("\\", "/")
        return Checker("go vet", [go, "vet", "." if pkg == "." else "./" + pkg], d), ""
    if ext == ".rs":
        d = _find_up(here, root, _has("Cargo.toml"))
        if d is None:
            return None, "没找到 Cargo.toml"
        cargo = shutil.which("cargo")
        if not cargo:
            return None, "找到了 Cargo.toml，但没装 cargo"
        return Checker("cargo check", [cargo, "check", "--message-format", "short", "--quiet"], d), ""
    if ext in (".java", ".kt", ".kts", ".groovy", ".scala"):
        is_test = "/src/test/" in file_path.replace("\\", "/")
        d = _find_up(here, root, _has("pom.xml"))
        if d is not None:
            exe = (_local(d, "mvnw.cmd", "mvnw") if os.name == "nt" else _local(d, "mvnw")) or shutil.which("mvn")
            if not exe:
                return None, "找到了 pom.xml，但没有 mvn，也没有项目自带的 mvnw"
            checker = Checker("maven", [exe, "-q", "-B", "test-compile" if is_test else "compile"], d)
        else:
            d = _find_up(here, root, lambda d: _has("build.gradle")(d) or _has("build.gradle.kts")(d))
            if d is None:
                return None, "没找到 pom.xml / build.gradle"
            exe = (_local(d, "gradlew.bat", "gradlew") if os.name == "nt" else _local(d, "gradlew")) \
                or shutil.which("gradle")
            if not exe:
                return None, "找到了 build.gradle，但没有 gradle，也没有项目自带的 gradlew"
            checker = Checker("gradle", [exe, "-q", "testClasses" if is_test else "classes"], d)
        runner, reason = _checked(checker)
        return (runner, "") if runner else (None, reason)
    if ext == ".cs":
        d = _find_up(here, root, lambda d: any(f.endswith((".csproj", ".sln")) for f in _listdir(d)))
        if d is None:
            return None, "没找到 .csproj / .sln"
        dotnet = shutil.which("dotnet")
        if not dotnet:
            return None, "找到了 .csproj / .sln，但没装 dotnet"
        target = next((f for f in sorted(_listdir(d)) if f.endswith(".csproj")), None) \
            or next(f for f in sorted(_listdir(d)) if f.endswith(".sln"))
        return Checker("dotnet build", [dotnet, "build", os.path.join(d, target), "--nologo", "-v", "q",
                                        "-clp:NoSummary"], d), ""
    if ext == ".php":
        php = shutil.which("php")
        return (Checker("php -l", [php, "-l", file_path], here, per_file=True), "") if php else (None, "没装 php")
    if ext == ".rb":
        ruby = shutil.which("ruby")
        return (Checker("ruby -c", [ruby, "-c", file_path], here, per_file=True), "") if ruby else (None, "没装 ruby")
    return None, f"{ext or '没有扩展名的'} 文件没有对应的编译检查工具"


def _listdir(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


# 输出里的"文件:行号"：tsc 的 a.ts(12,5)、go / cargo / javac 的 a.go:12:5、Maven 的 /D:/x/A.java:[12,5]、
# Kotlin 的 file:///D:/x/A.kt:12:5、dotnet 的 D:\x\A.cs(12,5)
_LOC_RE = re.compile(
    r"(?:file:///|(?<![A-Za-z])/(?=[A-Za-z]:))?"
    r"(?P<path>(?:[A-Za-z]:)?[^\s:()\[\]\"'<>|]+?\.(?:ts|tsx|mts|cts|vue|js|jsx|mjs|cjs|go|rs|java|kt|kts|"
    r"groovy|scala|cs))(?:\((?P<l1>\d+),\d+\)|:\[?(?P<l2>\d+))")
_ERROR_RE = re.compile(r"\berror\b|^\s*e:\s|\[ERROR\]", re.IGNORECASE)


def _resolve(path: str, cwd: str, root: str) -> str:
    """输出里的路径 → 绝对路径（保留大小写：要显示给模型、要当验证状态的键）。
    cargo 在工作区里按工作区根写相对路径，所以往上逐级试。"""
    if os.path.isabs(path):
        return os.path.realpath(path)
    d = cwd
    while True:
        cand = os.path.join(d, path)
        if os.path.exists(cand):
            return os.path.realpath(cand)
        if os.path.normcase(d) == os.path.normcase(root) or os.path.dirname(d) == d:
            return os.path.realpath(os.path.join(cwd, path))
        d = os.path.dirname(d)


def same_file_key(path: str) -> str:
    """比较两个路径是不是同一个文件用（Windows 不分大小写）。"""
    return os.path.normcase(os.path.realpath(path))


def _rel(abs_path: str, root: str) -> str:
    try:
        return os.path.relpath(abs_path, root).replace("\\", "/")
    except ValueError:
        return abs_path


def parse_errors(output: str, checker: Checker, root: str):
    """[(绝对路径, 行号, 说明)]，去重。go vet 的每条带位置的输出都是问题（它不写 error 这个词）。"""
    seen, out = set(), []
    for line in output.splitlines():
        if line.startswith("#"):
            continue
        m = _LOC_RE.search(line)
        if not m:
            continue
        if checker.name != "go vet" and not _ERROR_RE.search(line):
            continue                              # 警告不算
        path = _resolve(m.group("path"), checker.cwd, root)
        lineno = int(m.group("l1") or m.group("l2"))
        # 去掉位置后面剩下的列号：go 的 ":34: "、Maven 的 ",5] "、tsc 的 ": "
        msg = re.sub(r"^[\s:,\])]*(?:\d+[\])]?)?[\s:,\]]*", "", line[m.end():]).strip()
        key = (same_file_key(path), lineno, msg)
        if key not in seen:
            seen.add(key)
            out.append((path, lineno, msg))
    return out


def run(checker: Checker, target: str, changed, root: str, timeout: int = _PROJECT_TIMEOUT) -> Result:
    """跑检查。changed：这次改过的文件（绝对路径）；只有它们里的错误算没通过。"""
    t0 = time.time()
    env = dict(os.environ, CI="true")
    try:
        r = subprocess.run(checker.argv, cwd=checker.cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return Result("timeout", note=f"{checker.name} 超过 {timeout} 秒没跑完", duration_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        return Result("not_run", note=f"{checker.name} 起不来：{e}", duration_ms=int((time.time() - t0) * 1000))
    ms = int((time.time() - t0) * 1000)
    output = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
    if r.returncode == 0:
        return Result("passed", exit_code=0, duration_ms=ms)
    if checker.per_file:
        text = output[:2000] + ("\n... [输出已截断]" if len(output) > 2000 else "")
        return Result("failed", issues=text or f"{checker.name} 返回 {r.returncode}（没有输出）",
                      exit_code=r.returncode, duration_ms=ms, error_files=[os.path.realpath(target)])
    errors = parse_errors(output, checker, root)
    mine_keys = {same_file_key(p) for p in (*changed, target)}
    mine = [e for e in errors if same_file_key(e[0]) in mine_keys]
    others = [e for e in errors if same_file_key(e[0]) not in mine_keys]
    other_note = ""
    if others:
        files = sorted({_rel(p, root) for p, _l, _m in others})
        shown = "\n".join(f"  {_rel(p, root)}:{ln}: {msg}" for p, ln, msg in others[:5])
        other_note = (f"项目里另有 {len(others)} 处错误在这次没改过的文件里（{len(files)} 个文件，可能原来就有，"
                      f"不算这次的问题）：\n{shown}" + ("\n  …" if len(others) > 5 else ""))
    if mine:
        issues = "\n".join(f"{_rel(p, root)}:{ln}: {msg}" for p, ln, msg in mine[:15])
        if len(mine) > 15:
            issues += f"\n…另有 {len(mine) - 15} 处"
        files = {same_file_key(p): p for p, _l, _m in mine}
        return Result("failed", issues=issues, note=other_note, exit_code=r.returncode, duration_ms=ms,
                      error_files=sorted(files.values()))
    if others:
        return Result("passed", note=other_note, exit_code=r.returncode, duration_ms=ms)
    head = "\n".join(output.splitlines()[:12])
    return Result("error", note=f"{checker.name} 没跑完（退出码 {r.returncode}），输出里没有具体到文件的错误：\n{head}",
                  exit_code=r.returncode, duration_ms=ms)
