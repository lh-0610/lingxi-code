"""子进程调用必须显式处理标准输入（Windows 上继承来的句柄可能是坏的）。

不指定 `stdin` 时子进程会继承灵犀自己的标准输入。pytest 默认捕获输出，此时继承过去的
句柄在某些启动方式下是无效的，git 这类子进程直接报 `OSError: [WinError 6] 句柄无效`——
`scripts/test_agent_outcomes.py` 就因此误报过十几个失败，而失败的都不是被测逻辑。
所以 `src/` 和 `scripts/` 下不需要标准输入的子进程调用一律显式给
`stdin=subprocess.DEVNULL`。

`scripts/` 自己也要扫：测试夹具建 git 仓库时同样起子进程，不补 stdin 就是同一个毛病
（`scripts/test_worktree_metadata.py` 用 pytest 默认方式跑全量时会直接失败）。

两种可以不给的写法（测试同样放行）：
- 已经用 `input=` 传了数据：`run()` 会自己接管 stdin（`input=` 与 `stdin=` 同时给会抛
  ValueError，所以这两者也不能并存，见下）；
- 已经自己指定了 `stdin=`（如 `PIPE`，要给子进程喂数据）。

用 AST 而不是正则：参数可能跨行、`subprocess.run` 也可能被别名引用，逐行扫字符串会漏。
"""
import ast
import os

SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "call", "check_call"}

# 项目根下的 src 和 scripts；测试在 scripts/ 里跑，也能被 pytest 从仓库根收集。
_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_SCAN_DIRS = [os.path.join(_ROOT, name) for name in ("src", "scripts")]

_SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "build", "dist"}


def _iter_source_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _module_bindings(tree):
    """本模块里 subprocess 的两种绑定：(模块别名集合, 直接导入的函数名集合)。

    只看 import，不按属性名瞎猜：`scripts/` 下有同名本地函数（如 test_explore.py 的
    `call()`），按名字收会把它们误报成子进程调用。
    """
    modules, funcs = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in SUBPROCESS_FUNCS:
                    funcs.add(alias.asname or alias.name)
    return modules, funcs


def _is_subprocess_call(node, bindings):
    """这个 Call 是不是 subprocess 的子进程入口（含 `from subprocess import run` 的裸调用）。"""
    if not isinstance(node, ast.Call):
        return False
    modules, funcs = bindings
    func = node.func
    if isinstance(func, ast.Attribute):
        # subprocess.run(...) / sp.Popen(...)：模块名必须真由 import subprocess 绑定。
        return (func.attr in SUBPROCESS_FUNCS
                and isinstance(func.value, ast.Name)
                and func.value.id in modules)
    return isinstance(func, ast.Name) and func.id in funcs


def find_calls_without_stdin(source, filename="<test>"):
    """返回没有指定标准输入的调用位置：[(行号, 调用名)]。

    既没 `stdin=` 也没 `input=` 才算问题。带 `**kwargs` 的调用无法静态判断，按已指定处理，
    不误报。
    """
    tree = ast.parse(source, filename=filename)
    bindings = _module_bindings(tree)
    problems = []
    for node in ast.walk(tree):
        if not _is_subprocess_call(node, bindings):
            continue
        names = {kw.arg for kw in node.keywords}
        if None in names:            # **kwargs：不知道里面有没有 stdin，不猜
            continue
        if "stdin" in names or "input" in names:
            continue
        func = node.func
        label = func.attr if isinstance(func, ast.Attribute) else func.id
        problems.append((node.lineno, label))
    return problems


def find_stdin_and_input(source, filename="<test>"):
    """返回同时给了 `stdin=` 和 `input=` 的调用位置——这两者共存会直接抛 ValueError。"""
    tree = ast.parse(source, filename=filename)
    bindings = _module_bindings(tree)
    problems = []
    for node in ast.walk(tree):
        if not _is_subprocess_call(node, bindings):
            continue
        names = {kw.arg for kw in node.keywords}
        if "stdin" in names and "input" in names:
            problems.append(node.lineno)
    return problems


class TestScanWorks:
    """扫描器自身的行为——否则下面的全仓检查可能是空转通过的。"""

    def test_flags_call_without_stdin(self):
        src = "import subprocess\nsubprocess.run(['git', 'status'])\n"
        assert find_calls_without_stdin(src) == [(2, "run")]

    def test_flags_popen_and_check_output(self):
        src = (
            "import subprocess\n"
            "subprocess.Popen(['a'], stdout=subprocess.PIPE)\n"
            "subprocess.check_output(['b'])\n"
        )
        assert [line for line, _ in find_calls_without_stdin(src)] == [2, 3]

    def test_flags_multiline_call(self):
        """参数跨行时按关键字判断，不看行。"""
        src = (
            "import subprocess\n"
            "subprocess.run(\n"
            "    ['git', 'diff'],\n"
            "    capture_output=True,\n"
            "    timeout=10,\n"
            ")\n"
        )
        assert find_calls_without_stdin(src) == [(2, "run")]

    def test_allows_devnull(self):
        src = ("import subprocess\n"
               "subprocess.run(['git'], stdin=subprocess.DEVNULL)\n")
        assert find_calls_without_stdin(src) == []

    def test_allows_input(self):
        src = ("import subprocess\n"
               "subprocess.run(['git'], input=b'x', capture_output=True)\n")
        assert find_calls_without_stdin(src) == []

    def test_allows_stdin_pipe(self):
        src = "import subprocess\nsubprocess.Popen(['claude'], stdin=subprocess.PIPE)\n"
        assert find_calls_without_stdin(src) == []

    def test_kwargs_are_not_guessed(self):
        """`**opts` 里可能就带着 stdin，静态看不出来就不报——宁可漏也不误报。"""
        src = "import subprocess\ndef f(opts):\n    subprocess.run(['git'], **opts)\n"
        assert find_calls_without_stdin(src) == []

    def test_bare_import_name_detected(self):
        """`from subprocess import run` 之后裸调 `run(...)` 同样管。"""
        src = "from subprocess import run\nrun(['git'])\n"
        assert find_calls_without_stdin(src) == [(2, "run")]

    def test_non_subprocess_call_ignored(self):
        """本地同名函数不算子进程调用——`scripts/` 下真有这种同名 helper（test_explore.py）。"""
        src = "def run(cmd):\n    return cmd\nrun(['git'])\n"
        assert find_calls_without_stdin(src) == []

    def test_module_alias_detected(self):
        """`import subprocess as sp` 之后 sp.run(...) 同样管。"""
        src = "import subprocess as sp\nsp.run(['git'])\n"
        assert find_calls_without_stdin(src) == [(2, "run")]

    def test_unrelated_module_attribute_ignored(self):
        """别的模块上有同名方法（`foo.run(...)`）不能误判成 subprocess。"""
        src = "import subprocess\nclass Foo:\n    def run(self, c):\n        pass\nFoo().run(['git'])\n"
        assert find_calls_without_stdin(src) == []

    def test_stdin_conflict_detected(self):
        src = ("import subprocess\n"
               "subprocess.run(['git'], input=b'x', stdin=subprocess.PIPE)\n")
        assert find_stdin_and_input(src) == [2]


class TestRepoCalls:
    """src 和 scripts 下的真实调用。"""

    def test_every_call_sets_stdin(self):
        offenders = []
        for root in _SCAN_DIRS:
            for path in _iter_source_files(root):
                with open(path, encoding="utf-8") as stream:
                    source = stream.read()
                rel = os.path.relpath(path, _ROOT)
                offenders.extend(f"{rel}:{line} subprocess.{name}(...)"
                                 for line, name in find_calls_without_stdin(source, path))
        assert not offenders, (
            "这些子进程调用既没 stdin= 也没 input=，会继承灵犀的标准输入"
            "（pytest 捕获输出时可能拿到坏句柄，git 报 WinError 6）：\n  "
            + "\n  ".join(offenders)
        )

    def test_no_stdin_plus_input(self):
        """`input=` 和 `stdin=` 同时给会抛 ValueError，不能同时出现。"""
        offenders = []
        for root in _SCAN_DIRS:
            for path in _iter_source_files(root):
                with open(path, encoding="utf-8") as stream:
                    source = stream.read()
                rel = os.path.relpath(path, _ROOT)
                offenders.extend(f"{rel}:{line}" for line in find_stdin_and_input(source, path))
        assert not offenders, "stdin= 与 input= 不能同时给：\n  " + "\n  ".join(offenders)

    def test_scripts_dir_is_scanned(self):
        """回归：扫描范围必须覆盖 scripts/，否则测试夹具里的子进程调用会漏掉。"""
        assert os.path.join("scripts") in [os.path.basename(d) for d in _SCAN_DIRS]
        assert any(os.path.isfile(os.path.join(d, "test_worktree_metadata.py"))
                   for d in _SCAN_DIRS)
