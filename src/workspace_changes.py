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
        # stamp 是"内容可能变了吗"的廉价判据，命中就跳过哈希（每次工具调用都哈希整棵树太贵）。
        # 五个字段各挡一类漏判：mtime_ns 挡普通修改；size 挡同一纳秒内的等时长改写；
        # ctime_ns 挡"改完再把 mtime 改回原值"（POSIX 下 ctime 不可由用户直接设置）；
        # ino 挡"删掉重建一个同名同大小同时间的文件"；mode 挡只改权限位不改内容。
        stamp = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_mode)
        old = (previous or {}).get(key)
        if old and old[0] == stamp:
            digest = old[1]
        elif stat.S_ISLNK(info.st_mode):
            # 链接按"指向哪"比对，不读目标内容：目标在项目外时，读内容会把外部变化
            # 误记成项目改动，而且跟随链接读本身就绕出了项目边界。
            digest = os.readlink(path)
        else:
            # realpath 落到项目外 = 这个普通文件是透过 junction / 符号链接目录看到的，
            # 读它等于读项目外的内容，不能算进本项目的改动追踪。
            # 跨盘符时 commonpath 直接抛 ValueError（Windows 上把子目录 junction 到
            # 另一个盘很常见），归进同一个"跳出项目"分支——否则用户只会看到
            # "Paths don't have the same drive"，完全联想不到是某个目录链接
            # 导致验证闸门再也过不去。
            try:
                escaped = os.path.commonpath([root, os.path.realpath(path)]) != root
            except ValueError:
                escaped = True
            if escaped:
                raise OSError(
                    f"文件路径跳出项目（疑似目录链接指向项目外；跨盘链接同样如此）: {relative}")
            with open(path, "rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files[key] = (stamp, digest)
        # 上限是为了"别让一次工具调用卡死在遍历上"——追踪在每次工具调用前后各跑一次，
        # 10 万文件的全树 stat 已经到秒级。超限宁可整体抛错、降级成"未验证"，
        # 也不要静默只追踪一部分：那会让闸门误以为"没改动"而放行。
        if len(files) > 100_000:
            raise OSError("项目文件超过 100000 个，无法完整追踪命令的修改")
    return files


def refresh_workspace_tracking(v):
    from .verification import mark_blind_period, mark_dirty

    for root, previous in list(v.get("workspace_snapshots", {}).items()):
        try:
            current = _snapshot(root, previous)
        except (OSError, ValueError) as error:
            v.setdefault("tracking_errors", {})[root] = str(error)
            mark_blind_period(v, root, str(error))
            continue
        # 枚举成功 → 只消掉"**现在**读不了"这个事实。不消的话它是永久的（本函数只写不删），
        # 一次瞬时失败就让完成闸门再也过不去。
        #
        # 但**不能**顺手把验证义务也消掉，而这正是 mark_blind_period 存在的理由：
        # previous 为 None 时手上没有可比的旧基线，这次成功枚举只能建立一个**新**基线，
        # 对盲区期间发生过什么一无所知。"现在能读取目录"不能证明"之前改的代码已验证"——
        # 实测过：枚举失败期间真改了 app.py，下一轮枚举成功就把义务清空、没跑测试也返回
        # completed。盲区标记不随"现在能读了"消失，只能靠显式的测试 + diff 了结。
        v.get("tracking_errors", {}).pop(root, None)
        if previous is not None:
            for path in sorted(previous.keys() | current.keys()):
                before, after = previous.get(path), current.get(path)
                if before is None or after is None or before[1] != after[1] or before[0][-1] != after[0][-1]:
                    mark_dirty(v, path)
        v["workspace_snapshots"][root] = current


@contextmanager
def track_workspace_changes(root):
    from . import session
    from .verification import mark_blind_period

    v = session.get_verification()
    root = os.path.realpath(root)
    snapshots = v.setdefault("workspace_snapshots", {})
    if snapshots.get(root) is None:
        # `snapshots[root] is None` 有两个来源：上次建基线失败，或恢复未了义务时种下的
        # 空基线。两种都该在这里重试建基线——`root not in snapshots` 判不出后者。
        try:
            snapshots[root] = _snapshot(root)
            v.get("tracking_errors", {}).pop(root, None)
        except (OSError, ValueError) as error:
            snapshots[root] = None
            v.setdefault("tracking_errors", {})[root] = str(error)
            # 建不出基线就等于这次工具调用全程无人看守：记盲区，靠显式验证才能了结。
            mark_blind_period(v, root, str(error))
    else:
        refresh_workspace_tracking(v)
    try:
        yield
    finally:
        refresh_workspace_tracking(v)
