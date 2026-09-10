"""命令/MCP 的项目内文件变化追踪，不替代进程权限隔离。"""
import hashlib
import os
import stat
import subprocess
from contextlib import contextmanager


def _paths(root):
    from .tools_common import _SEARCH_IGNORE_DIRS

    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, timeout=10,
        )
    except FileNotFoundError:
        result = None
    except subprocess.TimeoutExpired as error:
        raise OSError("枚举项目文件超时") from error
    if result is not None and result.returncode == 0:
        yield from sorted(set(os.fsdecode(result.stdout).split("\0")) - {""})
        return

    def fail(error):
        raise error

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=fail):
        dirs[:] = [name for name in dirs if name not in _SEARCH_IGNORE_DIRS
                   and name != ".lingxi-worktrees"]
        for name in names:
            if name != ".git" and not name.startswith(".lingxi-patch-"):
                yield os.path.relpath(os.path.join(directory, name), root)


def _snapshot(root, previous=None):
    if not os.path.isdir(root):
        raise OSError(f"项目目录不可用: {root}")
    files = {}
    for relative in _paths(root):
        path = os.path.join(root, relative)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(info.st_mode):
            raise OSError(f"子模块目录需要单独验证，不能当作无改动: {relative}")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            continue
        key = relative.replace("\\", "/")
        stamp = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_mode)
        old = (previous or {}).get(key)
        if old and old[0] == stamp:
            digest = old[1]
        elif stat.S_ISLNK(info.st_mode):
            digest = os.readlink(path)
        else:
            # 不跟随指向项目外的父目录链接。
            if os.path.commonpath([root, os.path.realpath(path)]) != root:
                raise OSError(f"文件路径跳出项目: {relative}")
            with open(path, "rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files[key] = (stamp, digest)
        if len(files) > 100_000:
            raise OSError("项目文件超过 100000 个，无法完整追踪命令的修改")
    return files


def refresh_workspace_tracking(v):
    from .verification import mark_dirty

    for root, previous in list(v.get("workspace_snapshots", {}).items()):
        try:
            current = _snapshot(root, previous)
        except (OSError, ValueError) as error:
            v.setdefault("tracking_errors", {})[root] = str(error)
            continue
        if previous is not None:
            for path in sorted(previous.keys() | current.keys()):
                before, after = previous.get(path), current.get(path)
                if before is None or after is None or before[1] != after[1] or before[0][-1] != after[0][-1]:
                    mark_dirty(v, path)
        v["workspace_snapshots"][root] = current


@contextmanager
def track_workspace_changes(root):
    from . import session

    v = session.get_verification()
    root = os.path.realpath(root)
    snapshots = v.setdefault("workspace_snapshots", {})
    if root not in snapshots:
        try:
            snapshots[root] = _snapshot(root)
        except (OSError, ValueError) as error:
            snapshots[root] = None
            v.setdefault("tracking_errors", {})[root] = str(error)
    else:
        refresh_workspace_tracking(v)
    try:
        yield
    finally:
        refresh_workspace_tracking(v)
