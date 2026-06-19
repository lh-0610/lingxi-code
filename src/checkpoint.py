"""AI 改文件前自动 checkpoint（git stash），改坏了能一键撤销。

设计：
- 每次 `edit_file` / `write_file` / `append_file` 之前调 `make_checkpoint(project_root)`：
    - 如果项目是 git 仓库 → `git stash push -u -m "lingxi-checkpoint <ts>"` 把当前修改打包，
      但**立刻 pop 回来**（这样工作目录不变，只是 stash 列表里多了一份"动手前快照"）
    - 不是 git 仓库 → 当前 fallback 是"不做 checkpoint"，并通过返回值告诉调用方
- 每次成功 checkpoint 都把 stash ref 推进 `_checkpoint_stack`；
  `undo_last_checkpoint()` pop 栈顶，用 `git stash apply <ref> --force` 恢复
- 栈 cap 在 50，更旧的自动丢弃；同时 `_prune_checkpoint_stashes` 会把对应的
  旧 git stash 真正 `drop` 掉，防止 stash 无限堆积拖慢 git
- checkpoint 额外记录目标文件改动前是否存在 / 是否已被 git 跟踪：
    - AI 新建的文件撤销时删除
    - 原本未追踪的文件从 stash 的第三个 parent 恢复

为什么 stash + 立即 pop？因为 stash 默认会清空工作目录。我们要"快照但不影响当前"，
所以 stash 后立刻 pop 回来。stash 列表里仍保留那个 ref，apply 时就拿到了"动手前"的版本。

注：这套是 best-effort。AI 改文件 ↔ 用户改文件交错时，撤销可能会有冲突。冲突时
git 会自然报错，前端展示给用户决定怎么办。
"""
import os
import re
import subprocess
import threading
import time
import functools as _functools
from datetime import datetime
from typing import Optional


# 已创建的 checkpoint 栈：[(project_root, stash_ref, timestamp, tool_name, path)]
_checkpoint_stack: list = []
_lock = threading.Lock()
_MAX_STACK = 50

# 串行化整个 checkpoint/undo：两会话同项目并发时，stash push 与紧随的 rev-parse refs/stash
# 之间若被另一次 push 插入，refs/stash 会指向别人的快照 → 串改。RLock 覆盖 push→rev-parse→apply。
_git_lock = threading.RLock()


def _git_synchronized(fn):
    @_functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        with _git_lock:
            return fn(*args, **kwargs)
    return _wrapper

# 不同 project root 的 git 仓库检测缓存，避免每次 edit 都 fork git
_is_git_cache: dict = {}


def _is_git_repo(project_root: str) -> bool:
    """是否 git 仓库（缓存版）。"""
    if not project_root or not os.path.isdir(project_root):
        return False
    cached = _is_git_cache.get(project_root)
    if cached is not None:
        return cached
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=project_root, capture_output=True, timeout=3,
        )
        ok = r.returncode == 0 and b"true" in r.stdout
    except Exception:
        ok = False
    _is_git_cache[project_root] = ok
    return ok


def _has_changes(project_root: str) -> bool:
    """工作目录有未提交改动？（含 untracked）"""
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root, capture_output=True, timeout=3,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def _path_is_tracked(project_root: str, path: str) -> bool:
    """目标路径是否已被 git 跟踪。"""
    try:
        rel = os.path.relpath(path, project_root) if os.path.isabs(path) else path
        r = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", rel],
            cwd=project_root, capture_output=True, timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


@_git_synchronized
def make_checkpoint(project_root: str, tool_name: str, path: str) -> Optional[str]:
    """打一个快照。返回 stash ref（成功）或 None（跳过 / 失败）。

    思路：`git stash push -u -m "..."` 把当前工作区+暂存区+未追踪文件全打包；
    立刻 `git stash apply` 把内容恢复回工作区，让用户感知不到变化。
    stash 列表里仍然挂着那个 ref，undo 时拿来 apply。
    """
    if not _is_git_repo(project_root):
        return None  # 非 git 项目，无 checkpoint 能力
    full_path = path if os.path.isabs(path) else os.path.join(project_root, path)
    existed = os.path.exists(full_path)
    tracked = _path_is_tracked(project_root, full_path) if existed else False
    if not _has_changes(project_root):
        # 工作区干净 → 不需要 stash（HEAD 就是回退点）
        # 我们记一个特殊标记，让 undo 时知道走 `git checkout -- <path>` 路线
        ts = datetime.now().strftime("%H:%M:%S")
        ref = f"__HEAD__:{ts}"
        _push_stack(project_root, ref, tool_name, full_path, existed=existed, tracked=tracked)
        return ref

    msg = f"lingxi-checkpoint {datetime.now().strftime('%Y%m%d-%H%M%S')} {tool_name} {path}"
    try:
        r = subprocess.run(
            ["git", "stash", "push", "-u", "-m", msg],
            cwd=project_root, capture_output=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        ref_r = subprocess.run(
            ["git", "rev-parse", "--verify", "refs/stash"],
            cwd=project_root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5,
        )
        if ref_r.returncode != 0:
            subprocess.run(["git", "stash", "drop"], cwd=project_root, timeout=5)
            return None
        ref = (ref_r.stdout or "").strip()
        # 立刻 apply 回工作区，并使用稳定的 stash commit hash，避免后续 stash 顺序变化。
        r2 = subprocess.run(
            ["git", "stash", "apply", ref],
            cwd=project_root, capture_output=True, timeout=10,
        )
        if r2.returncode != 0:
            # 极端情况 apply 失败，回退一下不留半成品
            subprocess.run(["git", "stash", "drop"], cwd=project_root, timeout=5)
            return None
    except Exception:
        return None

    _push_stack(project_root, ref, tool_name, full_path, existed=existed, tracked=tracked)
    # 真正把超额的旧快照从 git 里删掉，否则 stash 会无限堆积
    # （之前只 pop 内存里的栈、没 drop git stash，攒到几百张拖慢 git）
    _prune_checkpoint_stashes(project_root)
    return ref


def _prune_checkpoint_stashes(project_root: str, keep: int = _MAX_STACK):
    """把超过 keep 上限的旧 lingxi-checkpoint stash 真正从 git 里 drop 掉。

    - 只动我们自己打的（消息含 'lingxi-checkpoint'），用户手动的 stash 一律不碰
    - stash@{0} 最新、index 越大越旧；保留最新 keep 个，更旧的删除
    - 必须从高 index 往低 drop——drop 掉一个后，比它旧的 index 会整体前移
    """
    try:
        r = subprocess.run(
            ["git", "stash", "list"],
            cwd=project_root, capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            return
        ours = []  # [(index, ref)]
        for line in r.stdout.decode("utf-8", errors="replace").splitlines():
            if "lingxi-checkpoint" not in line:
                continue
            m = re.match(r"(stash@\{(\d+)\})", line)
            if m:
                ours.append((int(m.group(2)), m.group(1)))
        if len(ours) <= keep:
            return
        ours.sort()              # 按 index 升序：新 → 旧
        stale = ours[keep:]      # 超出上限的最旧那批
        for _, ref in sorted(stale, reverse=True):  # 高 index 先 drop
            subprocess.run(
                ["git", "stash", "drop", ref],
                cwd=project_root, capture_output=True, timeout=5,
            )
    except Exception:
        pass


def _push_stack(project_root, ref, tool_name, path, existed=True, tracked=True):
    with _lock:
        _checkpoint_stack.append({
            "project_root": project_root,
            "ref": ref,
            "ts": time.time(),
            "tool": tool_name,
            "path": path,
            "existed": existed,
            "tracked": tracked,
        })
        while len(_checkpoint_stack) > _MAX_STACK:
            _checkpoint_stack.pop(0)


def has_undoable_checkpoint() -> bool:
    """是否有可撤销的 checkpoint。"""
    with _lock:
        return bool(_checkpoint_stack)


def latest_checkpoint_info() -> Optional[dict]:
    """返回栈顶 checkpoint 的元信息（给 UI 显示 "上次 AI 改了 X" 用）。"""
    with _lock:
        return dict(_checkpoint_stack[-1]) if _checkpoint_stack else None


@_git_synchronized
def undo_last_checkpoint() -> tuple[bool, str]:
    """撤销最近一次 checkpoint。

    返回 (success, message)。
    - 工作区干净时的 checkpoint（__HEAD__ 标记）：用 `git checkout HEAD -- <path>` 复原
    - 其它情况：`git stash apply <ref>` 把当时的快照覆盖回工作区
    """
    with _lock:
        if not _checkpoint_stack:
            return False, "没有可撤销的 checkpoint"
        cp = _checkpoint_stack[-1]   # 先 peek 不 pop：撤销可能失败（文件占用/冲突/越界），
                                     # 失败要保留快照让用户重试，成功才 _consume() 弹出。

    def _consume():
        """撤销成功后才把这条快照弹出栈（仅当它仍是栈顶，防并发误弹）。"""
        with _lock:
            if _checkpoint_stack and _checkpoint_stack[-1] is cp:
                _checkpoint_stack.pop()

    project_root = cp["project_root"]
    ref = cp["ref"]
    path = cp["path"]

    if not _is_git_repo(project_root):
        return False, "项目不再是 git 仓库，无法撤销"

    full_path = path if os.path.isabs(path) else os.path.join(project_root, path)
    rel = os.path.relpath(full_path, project_root)
    try:
        if os.path.commonpath([os.path.realpath(full_path), os.path.realpath(project_root)]) != os.path.realpath(project_root):
            return False, "目标路径超出项目范围，拒绝撤销"
    except ValueError:
        return False, "目标路径超出项目范围，拒绝撤销"

    if not cp.get("existed", True):
        try:
            if os.path.isfile(full_path) or os.path.islink(full_path):
                os.remove(full_path)
                _consume()
                return True, f"已撤销新建文件 {rel}"
            if not os.path.exists(full_path):
                _consume()
                return True, f"新建文件 {rel} 已不存在，无需撤销"
            return False, f"撤销失败：{rel} 已不是普通文件，拒绝删除"
        except Exception as e:
            return False, f"撤销失败：{e}"

    if ref.startswith("__HEAD__"):
        # 工作区当时干净，只要 checkout HEAD -- <path> 就回到改前
        # 但如果是绝对路径，要转为相对项目根
        try:
            r = subprocess.run(
                ["git", "checkout", "HEAD", "--", rel],
                cwd=project_root, capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                _consume()
                return True, f"已撤销对 {rel} 的改动（恢复到 HEAD）"
            return False, f"git checkout 失败：{r.stderr.decode('utf-8', errors='replace')[:200]}"
        except Exception as e:
            return False, f"撤销失败：{e}"

    # 普通 stash apply。用"按路径精确恢复"语义：
    # `git checkout <stash_ref> -- <path>` 把 stash 里那个文件覆盖到工作区，
    # 比 `git stash apply` 更稳——后者在工作区脏（AI 已改了其它东西）时会
    # 报 "would be overwritten" 然后失败。
    try:
        target = ref
        if not cp.get("tracked", True):
            target = f"{target}^3"
        r = subprocess.run(
            ["git", "checkout", target, "--", rel],
            cwd=project_root, capture_output=True, timeout=10,
        )
        if r.returncode == 0:
            _consume()
            return True, f"已撤销 {cp['tool']} 对 {os.path.basename(path)} 的改动"
        return False, (
            "git checkout 失败："
            + r.stderr.decode("utf-8", errors="replace")[:200]
        )
    except Exception as e:
        return False, f"撤销失败：{e}"


def clear_all_checkpoints():
    """清空栈（不删 stash 列表，只是 UI 上"不再追踪"）。"""
    with _lock:
        _checkpoint_stack.clear()
