"""代码模式：run_code 工具——模型写一小段 Python，一轮里调好几次读 / 搜工具，只把打印出来的东西交回。

为什么要有：DeepSeek 在灵犀里平均一轮只调 1.2~1.6 个工具，读一个文件、搜一次就是一轮；给它 read_files、
explore 也不用。三个真实任务里读和搜占了大半轮数（任务三 62 轮里 38 轮）。DeepSeek Harness 的代码模式
（run_code，V4.1-Flash 官方说专门针对 Harness 调过）就是让模型写一段程序把这些串起来、一次做完。

第一版只开放**读 / 搜类**工具：它们没有副作用，在程序里直接调是安全的。改文件、跑命令仍然走普通工具——
那一侧挂着执行前记录、改动确认、验证追踪、Plan 模式拦截、自动检查，绕过任何一样都会出事。

安全：这不是一个能挡住恶意代码的沙箱，挡的是"模型顺手写出越界的东西"。三道：
1. 执行前过一遍语法树：只放行白名单里的语法；不许 import（`import re` 除外，给的是只有正则函数的
   精简版）、不许 class、不许生成器；
   不许访问下划线开头的属性，也不许访问能摸到栈帧 / 字节码的属性（gi_frame、f_globals……）；
   不许调 .format / .format_map（格式串里的 {0.__class__} 能绕过属性检查）。
2. 只给最基本的内置函数：没有 open / eval / exec / getattr / type / __import__ ……，文件只能经工具读。
3. 跑的时候按墙钟限时（sys.settrace，只跟踪这段程序自己的帧），死循环也会被掐掉。
"""
import ast
import builtins
import re as _re_module
import sys
import time
import types

from langchain_core.tools import tool

# 程序里能调的工具：只读、无副作用（和 run_records.SIDE_EFFECT_FREE_TOOLS 里的读类工具一致）
CODE_TOOL_NAMES = (
    "read_file", "read_files", "search_files", "search_in_file", "list_directory",
    "find_definition", "find_references", "code_map", "related_files", "find_tests",
    "git_diff", "git_log", "git_status", "get_project_instructions",
)

_TIME_LIMIT_S = 60
_OUTPUT_MAX_CHARS = 30_000
_FILENAME = "<run_code>"

_ALLOWED_NODES = (
    ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.For, ast.While, ast.If,
    ast.Break, ast.Continue, ast.Pass, ast.Return, ast.Try, ast.ExceptHandler, ast.Raise,
    ast.FunctionDef, ast.arguments, ast.arg, ast.Lambda,
    ast.Call, ast.keyword, ast.Name, ast.Load, ast.Store, ast.Del, ast.Delete, ast.Attribute,
    ast.Subscript, ast.Slice, ast.Starred,
    ast.Constant, ast.JoinedStr, ast.FormattedValue,
    ast.List, ast.Tuple, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp, ast.comprehension,
    ast.BoolOp, ast.And, ast.Or, ast.BinOp, ast.UnaryOp, ast.Compare, ast.IfExp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.BitAnd, ast.BitOr, ast.BitXor,
    ast.LShift, ast.RShift, ast.MatMult, ast.Not, ast.USub, ast.UAdd, ast.Invert,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
    ast.alias,      # 只会出现在 import 里；除了 import re，import 语句本身先就被拒了
)
# 程序里的 re：只给正则函数和常用标志，不给模块本身——re 模块对外挂着 enum / functools / copyreg，
# 顺着 re.enum.sys 就能摸到整个运行环境。模型在 run_code 里想 import 的头一名就是 re（日志里 11 次
# import 被拒，5 次是它），拿它从搜索结果里挑东西
_SAFE_RE = types.SimpleNamespace(**{name: getattr(_re_module, name) for name in (
    "compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split",
    "escape", "error", "IGNORECASE", "I", "MULTILINE", "M", "DOTALL", "S", "VERBOSE", "X", "ASCII", "A",
)})
# 别的 import 被拒时告诉它该去哪：不说的话它会换个写法再试一轮
_IMPORT_ELSEWHERE = ("要用别的库、读写文件、起子进程，改用 run_command(command=\"python -\", "
                     "stdin=整段代码)；正则可以直接用 re")
# 不带下划线、却能摸到栈帧 / 字节码 / 全局变量的属性（再顺着字典下标就能拿到 __builtins__）
_BLOCKED_ATTRS = {
    "gi_frame", "gi_code", "gi_yieldfrom", "gi_running", "cr_frame", "cr_code", "cr_await",
    "ag_frame", "ag_code", "f_globals", "f_locals", "f_builtins", "f_back", "f_code", "f_trace",
    "tb_frame", "tb_next", "co_code", "co_consts", "co_names", "func_globals", "func_code",
    "format", "format_map", "mro",
}
_SAFE_BUILTINS = {name: getattr(builtins, name) for name in (
    "len", "range", "enumerate", "zip", "sorted", "reversed", "min", "max", "sum", "any", "all",
    "str", "int", "float", "bool", "list", "dict", "set", "tuple", "abs", "round", "repr",
    "map", "filter", "isinstance", "Exception", "ValueError", "KeyError", "IndexError",
    "TypeError", "RuntimeError",
)}


class CodeRejected(Exception):
    """程序没过语法检查。消息给模型看，说清哪一行、为什么。"""


def check_code(code: str) -> ast.Module:
    """解析并检查程序；不合格抛 CodeRejected。"""
    try:
        tree = ast.parse(code, filename=_FILENAME, mode="exec")
    except SyntaxError as e:
        raise CodeRejected(f"第 {e.lineno} 行语法错误：{e.msg}")
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.Import) and all(
                a.name == "re" and not (a.asname or "").startswith("_") for a in node.names):
            continue                          # re 已经备好（精简版），执行前把这句换掉（_BindRe）
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = node.module if isinstance(node, ast.ImportFrom) else "、".join(a.name for a in node.names)
            raise CodeRejected(f"第 {line} 行不允许 import {names}：run_code 只能调 tools.xxx 和基本的 Python。"
                               + _IMPORT_ELSEWHERE)
        if not isinstance(node, _ALLOWED_NODES):
            what = {ast.Import: "import", ast.ImportFrom: "import", ast.ClassDef: "class",
                    ast.GeneratorExp: "生成器表达式（改用 [... for ...] 列表推导）", ast.Yield: "yield",
                    ast.YieldFrom: "yield", ast.Global: "global", ast.Nonlocal: "nonlocal",
                    ast.With: "with", ast.AsyncFunctionDef: "async", ast.Await: "await"}.get(type(node))
            raise CodeRejected(f"第 {line} 行不允许 {what or type(node).__name__}：程序里只能调 tools.xxx 和基本的 Python")
        if isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in _BLOCKED_ATTRS):
            raise CodeRejected(f"第 {line} 行不允许访问属性 .{node.attr}")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise CodeRejected(f"第 {line} 行不允许用下划线开头的名字 {node.id}")
        if isinstance(node, ast.FunctionDef) and (node.name.startswith("_") or node.decorator_list):
            raise CodeRejected(f"第 {line} 行的函数不能以下划线开头、不能带装饰器")
        if isinstance(node, ast.arg) and node.arg.startswith("_"):
            raise CodeRejected(f"第 {line} 行的参数名不能以下划线开头")
    return tree


class _BindRe(ast.NodeTransformer):
    """import re → 换成 pass（re 已经在程序的环境里）；import re as rx → rx = re。"""

    def visit_Import(self, node):
        binds = [ast.Assign(targets=[ast.Name(a.asname, ast.Store())], value=ast.Name("re", ast.Load()))
                 for a in node.names if a.asname and a.asname != "re"]
        return [ast.copy_location(b, node) for b in binds] or ast.copy_location(ast.Pass(), node)


class _Tools:
    """程序里的 tools：每个属性是一个只读工具，参数和直接调用工具时一样，返回工具的文本结果。"""

    def __getattr__(self, name):
        # 只有没这个工具时才走到这里：原来报 "'_Tools' object has no attribute 'check_code'"，
        # 模型看不出是"这里不让用"还是"拼错了"
        raise AttributeError(f"run_code 里没有 tools.{name}：这里只能调只读工具（{'、'.join(CODE_TOOL_NAMES)}）。"
                             "改文件、跑命令、跑测试、静态检查请直接调对应的工具")

    def __init__(self, calls):
        from .tools import get_tool_map
        tool_map = get_tool_map()
        for name in CODE_TOOL_NAMES:
            if name in tool_map:
                setattr(self, name, self._wrap(tool_map[name], calls))

    @staticmethod
    def _wrap(t, calls):
        def call(*args, **kwargs):
            if args:
                # 位置参数按工具签名的顺序对上（tools.read_file("a.py") 这种写法最自然）
                names = list(t.args.keys())
                kwargs = {**dict(zip(names, args)), **kwargs}
            calls.append(t.name)
            return str(t.invoke(kwargs))
        call.__name__ = t.name
        return call


def run_program(code: str, time_limit: float = _TIME_LIMIT_S) -> str:
    """检查、执行程序，返回交给模型的文本（打印内容 + 返回值 + 出错信息）。"""
    tree = _BindRe().visit(check_code(code))
    # 包成函数：模型可以在顶层 return，结果也一并交回
    wrapper = ast.parse("def _program():\n    pass\n", filename=_FILENAME)
    wrapper.body[0].body = tree.body or [ast.Pass()]
    ast.fix_missing_locations(wrapper)

    out, size = [], [0]

    def _print(*args, sep=" ", end="\n", **_ignored):
        text = sep.join(str(a) for a in args) + end
        if size[0] < _OUTPUT_MAX_CHARS:
            out.append(text[:_OUTPUT_MAX_CHARS - size[0]])
        size[0] += len(text)

    calls = []
    env = {"__builtins__": {**_SAFE_BUILTINS, "print": _print}, "tools": _Tools(calls), "re": _SAFE_RE}
    deadline = time.monotonic() + time_limit

    def _local_trace(frame, event, arg):
        if time.monotonic() > deadline:
            raise TimeoutError(f"程序运行超过 {time_limit:.0f} 秒，已中止")
        return _local_trace

    def _global_trace(frame, event, arg):
        # 只跟踪这段程序自己的帧；工具内部的代码不跟踪，不拖慢工具
        return _local_trace if frame.f_code.co_filename == _FILENAME else None

    result, error = None, None
    old_trace = sys.gettrace()
    sys.settrace(_global_trace)
    try:
        exec(compile(wrapper, _FILENAME, "exec"), env)
        result = env["_program"]()
    except TimeoutError as e:
        error = str(e)
    except Exception as e:
        tb = e.__traceback__
        line = None
        while tb is not None:
            if tb.tb_frame.f_code.co_filename == _FILENAME:
                line = tb.tb_lineno              # 搬进包函数的语句保留原来的行号
            tb = tb.tb_next
        detail = f"{type(e).__name__}: {str(e)[:400]}"
        if isinstance(e, NameError) and getattr(e, "name", "") == "open":
            detail += "（run_code 里没有 open：读文件用 tools.read_file / tools.read_files）"
        error = f"第 {line} 行出错：{detail}" if line else detail
    finally:
        sys.settrace(old_trace)

    text = "".join(out)
    if size[0] > _OUTPUT_MAX_CHARS:
        text += f"\n…（输出共 {size[0]} 字，只交回前 {_OUTPUT_MAX_CHARS} 字：少打印一点，只挑要的）"
    if result is not None:
        text += f"\n返回值：{result}"
    if error:
        text += f"\n❌ {error}"
    summary = f"（程序调了 {len(calls)} 次工具：{'、'.join(dict.fromkeys(calls)) or '无'}）"
    return (text.strip() or "（程序没有打印任何东西：用 print 把要的结果打出来）") + "\n" + summary


@tool
def run_code(code: str, description: str = "") -> str:
    """写一小段 Python，**一轮里**连着调好几次读 / 搜工具，只把 print 出来的交回——
    要看好几个文件、搜好几个词、或者要在结果里挑东西时用它，比一轮调一个工具快得多。

    程序里用 `tools.工具名(参数)` 调工具，参数和直接调用时一样，返回工具的文本结果：
    tools.read_file / read_files / search_files / search_in_file / list_directory / find_definition /
    find_references / code_map / related_files / find_tests / git_diff / git_log / git_status /
    get_project_instructions。只读：改文件、跑命令请直接调对应工具。

    例：
        for p in ["src/a.py", "src/b.py"]:
            print(tools.read_file(p, limit=400))
        hits = tools.search_files(regex="def send", path="src", context=0)
        print("\\n".join(l for l in hits.splitlines() if "ui/" in l))

    限制：除了 `re`（直接用，或写 import re）不能 import、不能定义 class、不能用下划线开头的名字或属性；
    内置函数只有基本的那些（len / range / sorted / str / dict …），文件只能经 tools 读；最多跑 60 秒；
    最多交回 3 万字。要用别的库、读写文件、起子进程：run_command(command="python -", stdin=代码)。
    code: 程序（可以在顶层 return 一个结果）
    description: 一句话说明这段程序做什么（显示在界面上）"""
    try:
        return run_program(code)
    except CodeRejected as e:
        return f"❌ 程序没有运行：{e}"
