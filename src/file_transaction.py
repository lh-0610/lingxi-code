"""多文件写入的预备、失败回滚；不承诺进程崩溃时的跨文件原子性。"""
import os
import stat
import tempfile


class PatchApplyError(OSError):
    def __init__(self, message, *, rollback_failed=False):
        super().__init__(message)
        self.rollback_failed = rollback_failed


def _umask() -> int:
    """读当前 umask。os 只给了"设置并返回旧值"的接口，只能设一次再设回去。

    这里有竞态（两次调用之间别的线程建的文件会用到临时 umask 0），但本函数只在
    补丁准备阶段调用、且 apply_patch 本身是串行的；相比"新建文件恒为 0600"的
    确定性错误，这个窗口可以接受。
    """
    current = os.umask(0)
    os.umask(current)
    return current


def _read_bytes(path):
    if not os.path.lexists(path):
        return None
    if os.path.islink(path):
        raise OSError(f"目标变成了符号链接: {path}")
    with open(path, "rb") as stream:
        return stream.read()


def apply_file_changes(changes):
    """changes 为 (绝对路径, 审批前的 bytes 或 None, 新文本或 None)。"""
    prepared = []
    applied = []
    created_dirs = []
    retained = set()
    try:
        for path, expected, content in changes:
            if os.path.realpath(path) != path or _read_bytes(path) != expected:
                raise OSError(f"审批期间文件已变化，请重新生成补丁: {path}")
            parent = os.path.dirname(path)
            missing = []
            while not os.path.exists(parent):
                missing.append(parent)
                parent = os.path.dirname(parent)
            for directory in reversed(missing):
                os.mkdir(directory)
                created_dirs.append(directory)
            entry = {"path": path, "expected": expected, "staged": None,
                     "backup": None, "written": None}
            prepared.append(entry)
            # 临时文件必须和目标同目录：os.replace 要求同一文件系统才是原子替换，
            # 落到系统 temp 上跨卷时会退化成"复制+删除"，中途崩溃就留半个文件。
            for key, data in (("backup", expected), ("staged", content)):
                if data is None:
                    continue
                fd, temporary = tempfile.mkstemp(prefix=".lingxi-patch-", dir=os.path.dirname(path))
                entry[key] = temporary
                mode = "wb" if isinstance(data, bytes) else "w"
                kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8"}
                with os.fdopen(fd, mode, **kwargs) as stream:
                    stream.write(data)
                # mkstemp 建的文件是 0600。改已有文件要沿用它原来的权限位，否则
                # 一次补丁就把可执行脚本改成不可执行、把共享文件改成 owner-only；
                # 新建文件没有"原权限"可沿用，按 umask 走（与 write_file 一致），
                # 不然 apply_patch 建出来的文件权限会莫名比别的路径更严。
                if expected is not None:
                    os.chmod(temporary, stat.S_IMODE(os.stat(path).st_mode))
                else:
                    os.chmod(temporary, 0o666 & ~_umask())
            if entry["staged"]:
                entry["written"] = _read_bytes(entry["staged"])

        # 校验全部目标后再落盘，避免审批期间的外部编辑被旧补丁覆盖。
        for entry in prepared:
            path = entry["path"]
            if os.path.realpath(path) != path or _read_bytes(path) != entry["expected"]:
                raise OSError(f"审批期间文件已变化，请重新生成补丁: {path}")
        for entry in prepared:
            if entry["staged"] is None:
                os.remove(entry["path"])
            else:
                os.replace(entry["staged"], entry["path"])
            applied.append(entry)
    except Exception as error:
        rollback_errors = []
        for entry in reversed(applied):
            try:
                path = entry["path"]
                if os.path.realpath(path) != path or _read_bytes(path) != entry["written"]:
                    raise OSError("写入后目标被外部修改，拒绝覆盖")
                if entry["backup"] is None:
                    os.remove(path)
                else:
                    os.replace(entry["backup"], path)
            except Exception as rollback_error:
                if entry["backup"]:
                    retained.add(entry["backup"])
                rollback_errors.append(
                    f"{entry['path']}: {rollback_error}（备份: {entry['backup'] or '原文件不存在'}）")
        if rollback_errors:
            raise PatchApplyError(
                f"补丁写入失败: {error}；部分文件无法回滚，请保留现场并检查：\n"
                + "\n".join(rollback_errors), rollback_failed=True) from error
        raise PatchApplyError(f"补丁写入失败，已回滚本次写入: {error}") from error
    finally:
        for entry in prepared:
            for key in ("backup", "staged"):
                temporary = entry[key]
                if temporary and temporary not in retained and os.path.exists(temporary):
                    try:
                        os.remove(temporary)
                    except OSError:
                        pass
        for directory in reversed(created_dirs):
            try:
                os.rmdir(directory)
            except OSError:
                pass
