"""文件系统动词：删除 / 移动 / 复制 / 建目录。

以前这些只能靠 run_command 跑 `del` / `mv` / `mkdir`——确认卡上只有一条命令，
用户看不出到底哪个文件会消失，撤销底层也接不住（它只备份被写的文件）。这里给它们
各自的工具：确认卡展示**具体哪个路径、什么内容**，删除和移动接入 file_history 可撤销。

边界（都是有意的，别放宽）：
- **不递归删除**：delete_path 只删一个文件或一个**空**目录。非空目录一律拒绝——
  整棵树的备份无界，`rm -rf` 那种语义不给模型。要清目录就逐个文件删，每个都有卡有撤销。
- **不覆盖**：move_path / copy_path 的目标已存在就拒绝，绝不悄悄盖掉用户的文件。
- **删除和移动每次必问、不给"记住"**（confirm_bars._ALWAYS_ASK_PATH_OPS）：
  文件从路径上消失是永久数据丢失那一类，"接受编辑 / 自动"模式也不放行。
- 目录的删除/移动不建撤销记录（备份是逐文件字节），结果里如实说明不可撤销。
"""
import os
import shutil

from langchain_core.tools import tool

from . import file_history as _file_history
from . import state
from .tools_common import (
    _resolve_path, _subagent_path_rejection, _mark_current_dirty, _path_inside,
    _project_cwd, _current_subagent_worktree,
)

_PREVIEW_MAX_LINES = 200        # 删除/复制确认卡上最多展示这么多行
_BINARY_SNIFF = 8192
_DIR_NO_UNDO = "目录不在撤销跟踪范围内（撤销只恢复文件内容）"


def _read_bytes(full: str):
    """读原始字节。返回 (bytes|None, 错误说明)。"""
    try:
        with open(full, "rb") as f:
            return f.read(), ""
    except OSError as e:
        return None, str(e)


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:_BINARY_SNIFF]


def _describe(data: bytes) -> str:
    if _is_binary(data):
        return f"二进制文件，{len(data)} 字节（不展示内容）"
    lines = data.decode("utf-8", errors="replace").splitlines()
    return f"共 {len(lines)} 行 / {len(data)} 字节"


def _content_preview(base: str, data: bytes, *, removed: bool) -> str:
    """删除 / 复制卡的预览：整份内容都是减号（或加号）行，长文件只展示前若干行。"""
    sign = "-" if removed else "+"
    head = [f"--- a/{base}" if removed else "--- /dev/null",
            "+++ /dev/null" if removed else f"+++ b/{base}"]
    if _is_binary(data):
        what = "删除的是整个文件" if removed else "复制出的是完整副本"
        return "\n".join(head + [f"（{_describe(data)}，{what}）"]) + "\n"
    lines = data.decode("utf-8", errors="replace").splitlines()
    shown = lines[:_PREVIEW_MAX_LINES]
    head.append(f"@@ -1,{len(lines)} +0,0 @@" if removed
                else f"@@ -0,0 +1,{len(lines)} @@")
    body = [sign + line for line in shown]
    if len(lines) > len(shown):
        body.append(f"（只展示前 {len(shown)} 行；实际操作的是整个文件，{_describe(data)}）")
    return "\n".join(head + body) + "\n"


def _confirm(full: str, preview: str, op: str):
    """路径操作的确认（复用写文件那条通路：子 Agent 放行、无 UI 默认拒绝）。"""
    from .tools import _confirm_file_write
    return _confirm_file_write(full, "", "", op=op, preview=preview)


def _undo_note(prep, ok: bool, why: str) -> str:
    if prep.record is None:
        return f"\n⚠️ 本次改动不可撤销（{prep.reason}）"
    return "" if ok else f"\n⚠️ 本次改动不可撤销（{why}）"


@tool
def delete_path(path: str) -> str:
    """删除一个文件，或一个**空**目录。path: 要删除的路径（绝对或相对项目根）。

    每次都会弹确认卡给用户看删的是什么；删除文件后可以用顶栏"撤销"原样恢复。
    **不递归**：目录里还有东西就拒绝——要清空目录，先逐个 delete_path 里面的文件。
    只是想改内容用 edit_file / write_file，不要删了重建。
    """
    try:
        full = _resolve_path(path)
        reject = _subagent_path_rejection(full, "文件")
        if reject:
            return reject
        if not os.path.lexists(full):
            return f"删除失败: 路径不存在 {full}"
        if os.path.islink(full):
            return f"删除失败: 这是符号链接/junction，不代删 {full}"

        if os.path.isdir(full):
            entries = os.listdir(full)
            if entries:
                return (f"删除失败: 目录非空（{len(entries)} 项），本工具不递归删除。"
                        f"先逐个删除里面的文件，再删这个空目录: {full}")
            allowed, reject = _confirm(full, f"--- a/{path}\n+++ /dev/null\n（空目录）\n", "delete")
            if not allowed:
                return reject
            try:
                os.rmdir(full)
            except OSError as e:
                return f"删除失败: {e}"
            _mark_current_dirty(full)
            return f"成功删除空目录: {full}\n⚠️ {_DIR_NO_UNDO}"

        data, read_error = _read_bytes(full)
        if data is None:
            return f"删除失败: 读不到文件内容，无法给出确认预览（{read_error}）"
        allowed, reject = _confirm(full, _content_preview(os.path.basename(full), data,
                                                         removed=True), "delete")
        if not allowed:
            return reject
        with _file_history.write_lock():
            prep = _file_history.prepare_write("delete_path", full, expected_raw=data,
                                               kind=_file_history.KIND_DELETE)
            if prep.abort:
                return prep.reason
            error = ""
            try:
                os.remove(full)
            except OSError as e:
                error = str(e)
            # 失败也要定格：留在 prepared 会被当成"在途"永远不淘汰
            ok, why = _file_history.complete_delete(prep.record)
        if error:
            return f"删除失败: {error}" + _undo_note(prep, ok, why)
        _mark_current_dirty(full)
        return f"成功删除文件: {full}（{_describe(data)}）" + _undo_note(prep, ok, why)
    except Exception as e:
        return f"删除失败: {e}"


@tool
def move_path(source: str, destination: str) -> str:
    """移动 / 重命名一个文件或目录。source: 现在的路径, destination: 新路径。

    目标已存在就拒绝（不覆盖）；目标所在目录不存在会自动建。每次都会弹确认卡；
    移动**文件**后可以用顶栏"撤销"挪回原处（移动目录不跟踪撤销）。
    """
    try:
        src, dst, reject = _both_paths(source, destination)
        if reject:
            return reject
        if not os.path.lexists(src):
            return f"移动失败: 源路径不存在 {src}"
        if os.path.islink(src):
            return f"移动失败: 这是符号链接/junction，不代挪 {src}"
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
            return f"移动失败: 源和目标是同一个路径 {src}"
        if os.path.lexists(dst):
            return f"移动失败: 目标已存在，本工具不覆盖 {dst}"
        if _path_inside(dst, src):
            return f"移动失败: 不能把目录挪进它自己里面 {dst}"

        is_dir = os.path.isdir(src)
        data = None
        if is_dir:
            note = "整个目录，里面的文件原样跟着走"
        else:
            data, read_error = _read_bytes(src)
            if data is None:
                return f"移动失败: 读不到文件内容，无法给出确认预览（{read_error}）"
            note = _describe(data)
        allowed, reject = _confirm(src, f"--- a/{source}\n+++ b/{destination}\n"
                                        f"（移动，内容不变：{note}）\n", "move")
        if not allowed:
            return reject

        prep = None
        with _file_history.write_lock():
            if not is_dir:
                prep = _file_history.prepare_write("move_path", src, expected_raw=data,
                                                   kind=_file_history.KIND_MOVE, dest=dst)
                if prep.abort:
                    return prep.reason
            try:
                os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
                shutil.move(src, dst)
            except (OSError, shutil.Error) as e:
                if prep is not None:
                    _file_history.complete_move(prep.record)
                return f"移动失败: {e}"
            ok, why = (False, _DIR_NO_UNDO) if is_dir \
                else _file_history.complete_move(prep.record)
        _mark_current_dirty(src)
        _mark_current_dirty(dst)
        tail = _undo_note(prep, ok, why) if prep is not None \
            else f"\n⚠️ 本次改动不可撤销（{why}）"
        return f"成功移动: {src} → {dst}{tail}"
    except Exception as e:
        return f"移动失败: {e}"


@tool
def copy_path(source: str, destination: str) -> str:
    """复制一个文件到新路径。source: 源文件, destination: 新文件路径。

    目标已存在就拒绝（不覆盖）；目标所在目录不存在会自动建。只复制文件，不复制目录。
    复制出来的新文件可以用顶栏"撤销"删掉。
    """
    try:
        src, dst, reject = _both_paths(source, destination)
        if reject:
            return reject
        if not os.path.isfile(src):
            return f"复制失败: 源不是文件（本工具不复制目录）{src}"
        if os.path.lexists(dst):
            return f"复制失败: 目标已存在，本工具不覆盖 {dst}"
        data, read_error = _read_bytes(src)
        if data is None:
            return f"复制失败: 读不到源文件（{read_error}）"
        allowed, reject = _confirm(dst, _content_preview(os.path.basename(dst), data,
                                                        removed=False), "copy")
        if not allowed:
            return reject
        with _file_history.write_lock():
            prep = _file_history.prepare_write("copy_path", dst,
                                               expected_raw=_file_history.NO_FILE)
            if prep.abort:
                return prep.reason
            error = ""
            try:
                os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(data)
            except OSError as e:
                error = str(e)
            ok, why = _file_history.complete_write(prep.record, expected_bytes=data)
        if error:
            tail = ("；磁盘现状与复制内容核对一致，可撤销删掉它" if ok
                    else _undo_note(prep, ok, why))
            return f"复制失败: {error}{tail}"
        _mark_current_dirty(dst)
        return f"成功复制: {src} → {dst}（{_describe(data)}）" + _undo_note(prep, ok, why)
    except Exception as e:
        return f"复制失败: {e}"


@tool
def make_directory(path: str) -> str:
    """新建目录（父目录一起建，已存在则什么都不做）。path: 目录路径（相对项目根，须在项目内）。

    只建空目录、不动任何已有文件，所以项目内不弹确认卡（不像删除/移动有数据丢失风险，
    建错了随时删掉即可）；但仍守两道闸：只能在项目根内建，无确认界面时默认拒绝。
    同名的**文件**已存在时拒绝。
    """
    try:
        full = _resolve_path(path)
        reject = _subagent_path_rejection(full, "目录")
        if reject:
            return reject
        # 工作区边界：主 Agent 也只能在项目根内建目录，不能往项目外 / 系统目录乱建。
        root = _project_cwd()
        if not root or not _path_inside(full, root):
            return f"建目录失败: 超出项目范围，不允许（只能在项目根内建目录）{full}"
        if os.path.isfile(full):
            return f"建目录失败: 同名文件已存在 {full}"
        if os.path.isdir(full):
            return f"目录已存在，无需新建: {full}"
        # 无界面默认拒绝（与写盘类同一道闸）；子 Agent 已限定在自己 worktree 沙箱内，放行。
        if _current_subagent_worktree() is None and getattr(state, "ui_ref", None) is None:
            from .tools import _no_ui_refusal
            refusal = _no_ui_refusal("新建目录")
            if refusal:
                return refusal
        os.makedirs(full, exist_ok=True)
        return f"成功新建目录: {full}"
    except Exception as e:
        return f"建目录失败: {e}"


def _both_paths(source: str, destination: str):
    """解析两个路径并过子 Agent 沙箱。返回 (源绝对路径, 目标绝对路径, 拒绝文案)。"""
    src = _resolve_path(source)
    dst = _resolve_path(destination)
    for path in (src, dst):
        reject = _subagent_path_rejection(path, "文件")
        if reject:
            return src, dst, reject
    return src, dst, ""