"""工具共享底座：路径解析 / 项目根 / 子 Agent 沙箱 / 验证状态标记 / shell cwd。

从 tools.py 拆出，供 tools.py 及各兄弟工具模块（tools_git / tools_media / tools_codemap …）
共用。本模块只依赖 state / session / verification / paths，**不 import tools**，故无循环。
"""
import os
import re

from . import state
from . import session as _session
from .verification import mark_dirty as _v_mark_dirty, mark_check as _v_mark_check
from .paths import logger


# 搜索/扫描共享：噪声目录黑名单 + 单文件大小上限（search_files 与 codemap 的项目遍历共用）
_SEARCH_IGNORE_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", "bower_components",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "env", ".env",
    "build", "dist", "target", "out",
    ".next", ".nuxt", ".idea", ".vscode",
}
_SEARCH_MAX_FILE_SIZE = 1 * 1024 * 1024  # 单文件 > 1MB 跳过（大概率是二进制 / 数据文件）


def _project_cwd() -> str:
    """所有命令 / 文件工具的有效工作目录。

    优先用【当前会话】锚定的 project：多会话下 worker 跑工具时用它自己会话的项目根，
    不会因为用户在前台切了项目就把 A 会话的 read_file/run_command 落到 B。会话还没锚定
    （_UNSET，如刚新建没存盘）→ 回退全局 state.current_project；都没有 / 路径不存在 →
    进程 cwd。None 是合法的"无项目（全局）"。

    隔离模式：当前会话有 worktree 路径时，优先返回 worktree 目录。
    """
    from . import session as _session
    current = _session.current_session()
    if current:
        wt = current.worktree
        if getattr(current, "is_subagent", False):
            # 子 Agent 严格限定在自己 worktree（无则空，配合 _subagent_path_rejection 沙箱）
            return wt or ""
        if wt and os.path.isdir(wt):
            # 普通隔离会话：worktree 失效则回退主项目，别把操作落到死路径
            return wt
    proj = _session.current_project()   # 会话级：_UNSET 回退全局，统一来源
    if proj and os.path.isdir(proj):
        return proj
    return os.getcwd()


def _resolve_path(path: str) -> str:
    """相对路径按当前项目根解析；绝对路径原样返回。"""
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(_project_cwd(), path))


def _path_inside(path: str, root: str) -> bool:
    if not path or not root:
        return False
    try:
        path_real = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        root_real = os.path.normcase(os.path.realpath(os.path.abspath(root)))
        return os.path.commonpath([path_real, root_real]) == root_real
    except (OSError, ValueError):
        return False


def _current_subagent_worktree():
    try:
        current = _session.current_session()
    except Exception:
        return None
    if not getattr(current, "is_subagent", False):
        return None
    root = getattr(current, "worktree", None)
    if root and os.path.isdir(root):
        return root
    return ""


def _subagent_path_rejection(path: str, target: str = "path") -> str:
    root = _current_subagent_worktree()
    if root is None:
        return ""
    if not root:
        return "拒绝：子 Agent 没有有效 worktree，不能访问文件系统。"
    if not _path_inside(path, root):
        return f"拒绝：子 Agent 只能访问自己的 worktree，不能访问此{target}: {path}"
    return ""


def _subagent_command_rejection(command: str, cwd: str | None = None) -> str:
    root = _current_subagent_worktree()
    if root is None:
        return ""
    if not root:
        return "拒绝：子 Agent 没有有效 worktree，不能执行命令。"
    if cwd and not _path_inside(cwd, root):
        return f"拒绝：子 Agent 命令只能在自己的 worktree 内执行: {cwd}"

    text = command or ""
    if re.search(r"(^|[\\/\s\"'=])\.\.([\\/\s\"']|$)", text):
        return "拒绝：子 Agent 命令不能使用 '..' 跳出自己的 worktree。"

    # 这是 best-effort 防护(子 Agent 是用户派的协作 LLM、非对抗沙箱),正则永远抓不全所有
    # 路径写法;真正的边界是 run_command 的 cwd=worktree。已知未覆盖:未展开的环境变量
    # (%SystemRoot% / $HOME)、命令替换 $(...)。下面尽量堵常见的绝对路径形式。
    path_patterns = [
        r"[A-Za-z]:[\\/][^\s\"'<>|&;]+",        # 带盘符: C:\... / D:/...
        r"\\\\[^\s\"'<>|&;]+",                    # UNC: \\server\share
        # 盘符相对的绝对路径: \Windows\System32\... —— Windows 下解析到当前盘根，逃出 worktree。
        # 要求前面不是字符/冒号/反斜杠(避免命中 UNC 第二段或转义序列)。
        r"(?<![\w:\\])\\(?:[^\s\"'<>|&;\\/]+[\\/])*[^\s\"'<>|&;\\/]+",
        r"(?<![\w:])/(?:home|tmp|var|etc|usr|mnt|workspace|Users|opt|root)/[^\s\"'<>|&;]*",
    ]
    for pattern in path_patterns:
        for match in re.finditer(pattern, text):
            candidate = match.group(0).rstrip(".,)]}")
            if candidate and not _path_inside(candidate, root):
                return f"拒绝：子 Agent 命令引用了 worktree 外路径: {candidate}"
    return ""


def _norm_vpath(path: str) -> str:
    """规范化路径用于验证状态追踪：转成相对项目根的正斜杠路径。"""
    abs_path = _resolve_path(path)
    cwd = _project_cwd()
    abs_real = os.path.realpath(abs_path)
    cwd_real = os.path.realpath(cwd) if cwd else ""
    try:
        inside = cwd_real and os.path.normcase(os.path.commonpath([abs_real, cwd_real])) == os.path.normcase(cwd_real)
    except ValueError:
        inside = False
    if inside:
        rel = os.path.relpath(abs_real, cwd_real)
        return rel.replace("\\", "/")
    return os.path.normpath(abs_path).replace("\\", "/")


def _mark_current_dirty(full_path: str) -> None:
    try:
        _v_mark_dirty(_session.get_verification(), _norm_vpath(full_path))
    except Exception as e:
        logger.debug(f"验证状态标记 dirty 失败: {e}")


def _mark_current_check(full_path: str, passed, checker: str = "") -> None:
    try:
        _v_mark_check(_session.get_verification(), _norm_vpath(full_path), passed, checker or "")
    except Exception as e:
        logger.debug(f"验证状态标记 check 失败: {e}")


def _shell_cwd() -> str:
    """run_command 实际用的 cwd：shell_cwd（存在且是目录）否则退回项目根。"""
    base = getattr(state, "shell_cwd", None)
    if base and os.path.isdir(base):
        return base
    return _project_cwd()


def _parse_cd(command: str):
    """纯 cd 命令 → 返回目标【绝对路径】；非纯 cd（复合/重定向/非 cd）→ None。"""
    import re as _re
    s = command.strip()
    # 含 && || | ; 换行 > < 的复合/重定向命令不算"纯 cd"
    if any(op in s for op in ("&&", "||", "|", ";", "\n", ">", "<")):
        return None
    # "cd" / "cd X"；"cdrom" 不匹配（要求 cd 后面要么结尾要么空白）
    m = _re.match(r'^cd(?:\s+(.+))?$', s, _re.IGNORECASE)
    if not m:
        return None
    arg = (m.group(1) or "").strip().strip('"').strip("'")
    if not arg or arg == "~":
        return _project_cwd()                               # cd / cd ~ → 回项目根
    if arg.startswith("~") and (len(arg) == 1 or arg[1] in ("/", "\\")):
        # ~/sub 或 ~\sub → 项目根/sub
        arg = arg[2:].lstrip("/\\") or "."
        target = os.path.join(_project_cwd(), arg)
    else:
        target = arg if os.path.isabs(arg) else os.path.join(_shell_cwd(), arg)
    return os.path.normpath(target)
