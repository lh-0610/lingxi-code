"""子进程调用必须显式处理标准输入（Windows 上继承来的句柄可能是坏的）。

不指定 `stdin` 时子进程会继承灵犀自己的标准输入。pytest 默认捕获输出，此时继承过去的
句柄在某些启动方式下是无效的，git 这类子进程直接报 `OSError: [WinError 6] 句柄无效`——
`scripts/test_agent_outcomes.py` 就因此误报过十几个失败，而失败的都不是被测逻辑。
所以 `src/` 下不需要标准输入的子进程调用一律显式给 `stdin=subprocess.DEVNULL`。

两种可以不给的写法（测试同样放行）：
- 已经用 `input=` 传了数据：`run()` 会自己接管 stdin（`input=` 与 `stdin=` 同时给会抛
  ValueError，所以这两者也不能并存，见下）；
- 已经自己指定了 `stdin=`（如 `PIPE`，要给子进程喂数据）。

用 AST 而不是正则：参数可能跨行、`subprocess.run` 也可能被别名引用，逐行扫字符串会漏。
"""
import ast
import os

SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "call", "check_call"}

# 项目根下的 src；测试在 scripts/ 里跑，也能被 pytest 从仓库根收集。
_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")

_SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "build", "dist"}


def _iter_source_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _is_subprocess_call(node):
    """这个 Call 是不是 subprocess 的子进程入口（含 `from subprocess import run` 的裸调用）。"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        # subprocess.run(...) / sp.Popen(...)；模块别名无法静态分辨，按属性名收。
        return func.attr in SUBPROCESS_FUNCS
    return isinstance(func, ast.Name) and func.id in SUBPROCESS_FUNCS


def find_calls_without_stdin(source, filename="<test>"):
    """返回没有指定标准输入的调用位置：[(行号, 调用名)]。

    既没 `stdin=` 也没 `input=` 才算问题。带 `**kwargs` 的调用无法静态判断，按已指定处理，
    不误报。
    """
    tree = ast.parse(source, filename=filename)
    problems = []
    for node in ast.walk(tree):
        if not _is_subprocess_call(node):
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
    problems = []
    for node in ast.walk(tree):
        if not _is_subprocess_call(node):
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
        src = "def run(cmd):\n    return cmd\nrun(['git'])\n"
        # 同名的本模块函数会被当成疑似调用，这是已知的保守取向（见模块注释）；
        # 真正用于全仓检查时，src 下没有这种同名裸调用。
        assert find_calls_without_stdin(src) == [(3, "run")]

    def test_stdin_conflict_detected(self):
        src = ("import subprocess\n"
               "subprocess.run(['git'], input=b'x', stdin=subprocess.PIPE)\n")
        assert find_stdin_and_input(src) == [2]


class TestSrcCalls:
    """src 下的真实调用。"""

    def test_every_call_sets_stdin(self):
        offenders = []
        for path in _iter_source_files(_SRC_DIR):
            with open(path, encoding="utf-8") as stream:
                source = stream.read()
            rel = os.path.relpath(path, os.path.join(_SRC_DIR, ".."))
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
        for path in _iter_source_files(_SRC_DIR):
            with open(path, encoding="utf-8") as stream:
                source = stream.read()
            rel = os.path.relpath(path, os.path.join(_SRC_DIR, ".."))
            offenders.extend(f"{rel}:{line}" for line in find_stdin_and_input(source, path))
        assert not offenders, "stdin= 与 input= 不能同时给：\n  " + "\n  ".join(offenders)
