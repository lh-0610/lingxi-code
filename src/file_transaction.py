"""多文件写入的预备、失败回滚；不承诺进程崩溃时的跨文件原子性。"""
import os
import stat
import tempfile


class PatchApplyError(OSError):
    def __init__(self, message, *, rollback_failed=False):
        super().__init__(message)
        self.rollback_failed = rollback_failed


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
            for key, data in (("backup", expected), ("staged", content)):
                if data is None:
                    continue
                fd, temporary = tempfile.mkstemp(prefix=".lingxi-patch-", dir=os.path.dirname(path))
                entry[key] = temporary
                mode = "wb" if isinstance(data, bytes) else "w"
                kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8"}
                with os.fdopen(fd, mode, **kwargs) as stream:
                    stream.write(data)
                if expected is not None:
                    os.chmod(temporary, stat.S_IMODE(os.stat(path).st_mode))
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
