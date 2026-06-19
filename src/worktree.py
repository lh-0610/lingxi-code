"""Git Worktree 隔离模式。

让 AI 的文件修改在独立 worktree 中进行，主工作区保持不变。
功能：创建/完成/清理隔离 worktree，以及路径路由。
"""

import os
import re
import shutil
import subprocess
import logging
import tempfile
import threading
import functools

logger = logging.getLogger(__name__)

# 运行期活跃 worktree 注册表：session_id → {"path": str, "branch": str}
_WORKTREES: dict[str, dict] = {}

# 串行化所有 worktree 生命周期操作：_WORKTREES 是被 UI 线程（隔离开关）、worker 线程
# （子 Agent spawn）、退出清理共享的可变 dict，且并发 `git worktree add` 会撞 git 索引锁。
_WT_LOCK = threading.RLock()


def _synchronized(fn):
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        with _WT_LOCK:
            return fn(*args, **kwargs)
    return _wrapper


# ── helpers ───────────────────────────────────────────────────────────────────


def _cleanup_worktree(path: str) -> None:
    """尝试删除 worktree 目录（best-effort，用于测试 teardown）。"""
    shutil.rmtree(path, ignore_errors=True)


def has_uncommitted_changes(project_path: str) -> bool:
    """主工作区是否存在未提交改动。"""
    if not project_path or not os.path.isdir(project_path):
        return False
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        return result.returncode == 0 and bool((result.stdout or "").strip())
    except Exception:
        return False


def _worktree_info_from_path(wt_path: str) -> dict | None:
    """从 worktree 的 .git 文件恢复主仓库路径和 worktree 名称。"""
    git_file = os.path.join(wt_path, ".git")
    if not os.path.isfile(git_file):
        return None
    try:
        with open(git_file, encoding="utf-8", errors="replace") as f:
            content = f.read().strip()
    except OSError:
        return None
    if not content.startswith("gitdir: "):
        return None

    git_dir = content[8:].strip()
    git_dir_abs = os.path.realpath(os.path.join(wt_path, git_dir) if not os.path.isabs(git_dir) else git_dir)
    worktrees_dir = os.path.dirname(git_dir_abs)
    git_root = os.path.dirname(worktrees_dir)
    project_path = os.path.dirname(git_root)
    return {
        "git_dir": git_dir_abs,
        "name": os.path.basename(git_dir_abs),
        "project_path": project_path,
    }


def _branch_for_worktree(project_path: str, wt_path: str) -> str | None:
    """读取 worktree 当前分支名。"""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=wt_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
    except Exception:
        return None
    branch = (result.stdout or "").strip()
    if branch:
        return branch

    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=project_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
    except Exception:
        return None
    current_path = None
    for line in (result.stdout or "").splitlines():
        if line.startswith("worktree "):
            current_path = os.path.realpath(line[len("worktree "):])
        elif current_path == os.path.realpath(wt_path) and line.startswith("branch refs/heads/"):
            return line[len("branch refs/heads/"):]
    return None


def is_git_repo(path) -> bool:
    """判断路径是否是 git 工作树顶层目录。"""
    path = str(path)
    if not os.path.isdir(path):
        return False
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        if r.returncode != 0:
            return False
        return os.path.realpath(r.stdout.strip()) == os.path.realpath(path)
    except Exception:
        return False


def _ensure_worktree_excluded(project_path: str) -> None:
    """把 ``.lingxi-worktrees/`` 写进主仓库的 ``.git/info/exclude``（本地忽略）。

    隔离 worktree 建在主项目根的 ``.lingxi-worktrees/`` 下，否则它会以未跟踪文件出现在主项目
    ``git status`` —— 污染 git_status 工具、让 has_uncommitted_changes 误报、甚至被
    ``git add -A`` 误纳进提交。用 ``.git/info/exclude``（本地、不提交）而非用户的 ``.gitignore``
    （被跟踪文件，改它会脏化用户的工作区/提交）。best-effort：失败只记日志，不影响 worktree 创建。
    """
    line = ".lingxi-worktrees/"
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=project_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        if r.returncode != 0:
            return
        exclude_path = (r.stdout or "").strip()
        if not exclude_path:
            return
        if not os.path.isabs(exclude_path):
            exclude_path = os.path.join(project_path, exclude_path)

        existing = ""
        if os.path.isfile(exclude_path):
            with open(exclude_path, encoding="utf-8", errors="replace") as f:
                existing = f.read()
        # 整行精确匹配，避免把已有的 ".lingxi-worktrees" 子串误判成已存在
        if any(ln.strip() == line for ln in existing.splitlines()):
            return

        os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
        with open(exclude_path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(line + "\n")
        logger.info(f"已把 {line} 加入 {exclude_path}（本地忽略隔离目录）")
    except Exception as e:
        logger.warning(f"写 info/exclude 失败（不影响 worktree 创建）: {e}")


def _sanitize_branch(name: str) -> str:
    """把 session_id 转成合法 git 分支名。已合法的名字原样保留。"""
    if not name:
        name = "session"
    original = name
    # 替换所有不合法字符为连字符
    s = re.sub(r"[^a-zA-Z0-9._/-]", "-", name)
    # 去掉连续连字符
    s = re.sub(r"-+", "-", s)
    # 去掉开头的 -
    s = s.lstrip("-")
    # 确保非空
    s = s or f"session-{hash(name) & 0xFFFFFFFF:08x}"
    # 只在名字被修改（含特殊字符）时加 session- 前缀，已合法的名字原样保留
    if s != original and not s.startswith("session-") and not s.startswith("lingxi/"):
        s = f"session-{s}"
    # 截断
    s = s[:100].rstrip("-")
    return s


def _is_within(child, parent) -> bool:
    """判断 *child* 路径是否在 *parent* 之内。

    使用 ``realpath`` + ``commonpath`` 防止 ``..`` / 符号链接越界。
    """
    child_real = os.path.realpath(str(child))
    parent_real = os.path.realpath(str(parent))
    try:
        return os.path.commonpath([child_real, parent_real]) == parent_real
    except ValueError:
        # Windows 上不同盘符会抛 ValueError
        return False


# ── core API ──────────────────────────────────────────────────────────────────


@_synchronized
def create(session, project_path: str, session_id: str = None) -> str | None:
    """创建隔离 worktree，返回路径字符串；非 git 仓库返回 ``None``。

    幂等：同一 *session_id* 重复调用返回已有 worktree。
    设置 ``session.worktree`` 并注册到 ``_WORKTREES``。
    """
    if session_id is None:
        session_id = str(id(session))

    # 幂等：已有且目录还在就复用
    if session_id in _WORKTREES:
        info = _WORKTREES[session_id]
        if os.path.isdir(info["path"]):
            session.worktree = info["path"]
            return info["path"]

    project_path = str(project_path)
    if not is_git_repo(project_path):
        return None

    branch = f"lingxi/{_sanitize_branch(session_id)}"
    wt_dir = os.path.join(project_path, ".lingxi-worktrees")
    wt_path = os.path.join(wt_dir, session_id)

    try:
        os.makedirs(wt_dir, exist_ok=True)
        _ensure_worktree_excluded(project_path)  # 本地忽略隔离目录，别污染主项目 git status
        if not _is_within(wt_path, wt_dir):
            raise ValueError("worktree 路径越界")

        # 程序退出时会保留隔离区，重启后同一 session_id 应复用磁盘上的 worktree。
        if os.path.isdir(wt_path):
            info = _worktree_info_from_path(wt_path)
            if info and os.path.realpath(info["project_path"]) == os.path.realpath(project_path):
                _WORKTREES[session_id] = {"path": wt_path, "branch": branch}
                session.worktree = wt_path
                logger.info(f"已恢复隔离 worktree: {wt_path} (branch={branch})")
                return wt_path
            _cleanup_worktree(wt_path)

        # 若分支残留先删（从上次异常退出恢复）
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=project_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

        # 创建 worktree + 分支
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, wt_path, "HEAD"],
            cwd=project_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            check=True, timeout=30,
        )

        _WORKTREES[session_id] = {"path": wt_path, "branch": branch}
        session.worktree = wt_path
        logger.info(f"已创建隔离 worktree: {wt_path} (branch={branch})")
        return wt_path

    except Exception as e:
        logger.error(f"创建 worktree 失败: {e}")
        _cleanup_worktree(wt_path)
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=project_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        return None


@_synchronized
def finish(session, *, apply_changes: bool = False) -> tuple[bool, str]:
    """结束会话的 worktree。

    ``apply_changes=True`` 时先把隔离区相对 HEAD 的改动应用回主项目，成功后再清理；
    否则只丢弃隔离区并清理。返回 ``(success, message)``。
    """
    wt_path = session.worktree
    if not wt_path:
        return True, "没有活跃的 worktree。"

    # 从注册表反查 session_id 和 branch
    sid_found = None
    for sid, info in list(_WORKTREES.items()):
        if info["path"] == wt_path:
            sid_found = sid
            branch = info["branch"]
            break

    if sid_found is not None:
        _WORKTREES.pop(sid_found, None)
    else:
        branch = None

    info = _worktree_info_from_path(wt_path)
    project_path = info["project_path"] if info else None
    branch = branch or (_branch_for_worktree(project_path, wt_path) if project_path else None)

    if apply_changes:
        ok, msg = _apply_changes_to_project(wt_path, project_path)
        if not ok:
            if sid_found is not None:
                _WORKTREES[sid_found] = {"path": wt_path, "branch": branch}
            return False, msg

    if project_path and branch:
        _remove_worktree(wt_path, branch)
    else:
        _cleanup_worktree(wt_path)

    session.worktree = None
    if apply_changes:
        return True, "隔离区改动已应用回主项目，并已清理 worktree。"
    return True, "隔离区已丢弃并清理。"


@_synchronized
def cleanup_all() -> None:
    """清理所有注册的 worktree。调用时机：程序退出。"""
    for sid, info in list(_WORKTREES.items()):
        try:
            _remove_worktree(info["path"], info["branch"])
        except Exception as e:
            logger.warning(f"清理 worktree {sid} 失败: {e}")
            _cleanup_worktree(info["path"])
    _WORKTREES.clear()


# ── 内部 ──────────────────────────────────────────────────────────────────────


def _remove_worktree(wt_path: str, branch: str) -> None:
    """通过 ``git worktree remove`` 移除 worktree + 分支。"""
    info = _worktree_info_from_path(wt_path)
    project_path = info["project_path"] if info else None

    if project_path and os.path.isdir(project_path):
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", wt_path],
                cwd=project_path, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
        except Exception:
            _cleanup_worktree(wt_path)

        try:
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=project_path, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10,
            )
        except Exception:
            pass
    else:
        _cleanup_worktree(wt_path)


def _apply_changes_to_project(wt_path: str, project_path: str | None) -> tuple[bool, str]:
    """把 worktree 相对 HEAD 的所有改动应用到主项目工作区。"""
    temp_index = None
    try:
        if not project_path or not os.path.isdir(project_path):
            return False, "无法定位主项目，已保留隔离区未清理。"
        if not os.path.isdir(wt_path):
            return False, "隔离区目录不存在，无法恢复改动。"

        # base = 主项目当前 HEAD = worktree 的分叉基点（常态下主项目自 create 起未动）。
        # 必须用它而非 worktree 的 HEAD：AI 若在隔离区里 git_commit 过，工作区是干净的，按
        # worktree HEAD 取 diff 会判成"无改动"，提交过的工作就随 worktree 删除而静默丢失。
        # 对 base 取 diff（git add -A 后比 index 与 base）则把"已提交 + 未提交"净改动一起算进来。
        base_r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        base = (base_r.stdout or "").strip() if base_r.returncode == 0 else "HEAD"

        add = subprocess.run(
            ["git", "add", "-A"],
            cwd=wt_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        if add.returncode != 0:
            return False, f"暂存隔离区改动失败：{(add.stderr or '').strip() or '未知错误'}"

        changed = subprocess.run(
            # core.quotepath=false：非 ASCII（中文）文件名不被转义成 \xxx，否则快照/恢复对不上真路径
            ["git", "-c", "core.quotepath=false", "diff", "--cached", "--name-only", base],
            cwd=wt_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        if changed.returncode != 0:
            return False, f"读取隔离区改动失败：{(changed.stderr or '').strip() or '未知错误'}"
        changed_files = [line.strip() for line in (changed.stdout or "").splitlines() if line.strip()]
        if not changed_files:
            return True, "隔离区没有需要恢复的改动。"
        snapshots = _snapshot_project_files(project_path, changed_files)

        diff = subprocess.run(
            ["git", "diff", "--cached", "--binary", base],
            cwd=wt_path, capture_output=True,
            timeout=30,
        )
        if diff.returncode != 0:
            stderr = diff.stderr.decode("utf-8", errors="replace") if diff.stderr else ""
            return False, f"生成隔离区补丁失败：{stderr.strip() or '未知错误'}"
        if not diff.stdout:
            return True, "隔离区没有需要恢复的改动。"

        index_path = os.path.join(project_path, ".git", "index")
        tmp = tempfile.NamedTemporaryFile(prefix="lingxi-index-", delete=False)
        tmp.close()
        temp_index = tmp.name
        if os.path.exists(index_path):
            shutil.copy2(index_path, temp_index)

        git_env = os.environ.copy()
        git_env["GIT_INDEX_FILE"] = temp_index

        check = subprocess.run(
            ["git", "apply", "--check", "--3way", "--binary"],
            cwd=project_path, input=diff.stdout, capture_output=True,
            env=git_env,
            timeout=30,
        )
        if check.returncode != 0:
            _restore_project_files(project_path, snapshots)
            stderr = check.stderr.decode("utf-8", errors="replace") if check.stderr else ""
            return False, (
                "恢复隔离区改动失败，已保留 worktree。"
                f"\n{stderr.strip() or '请检查主项目是否有冲突或未提交改动。'}"
            )

        apply = subprocess.run(
            ["git", "apply", "--3way", "--binary"],
            cwd=project_path, input=diff.stdout, capture_output=True,
            env=git_env,
            timeout=30,
        )
        if apply.returncode != 0:
            _restore_project_files(project_path, snapshots)
            stderr = apply.stderr.decode("utf-8", errors="replace") if apply.stderr else ""
            return False, (
                "恢复隔离区改动失败，已保留 worktree。"
                f"\n{stderr.strip() or '请检查主项目是否有冲突或未提交改动。'}"
            )
        return True, "隔离区改动已应用到主项目工作区。"
    except subprocess.TimeoutExpired:
        return False, "恢复隔离区改动超时，已保留 worktree。"
    except Exception as e:
        return False, f"恢复隔离区改动异常，已保留 worktree：{e}"
    finally:
        if temp_index and os.path.exists(temp_index):
            try:
                os.remove(temp_index)
            except OSError:
                pass


def _snapshot_project_files(project_path: str, rel_paths: list[str]) -> dict[str, bytes | None]:
    snapshots: dict[str, bytes | None] = {}
    root = os.path.realpath(project_path)
    for rel in rel_paths:
        full = os.path.realpath(os.path.join(project_path, rel))
        try:
            if os.path.commonpath([root, full]) != root:
                continue
        except ValueError:
            continue
        if os.path.exists(full) and os.path.isfile(full):
            with open(full, "rb") as f:
                snapshots[rel] = f.read()
        else:
            snapshots[rel] = None
    return snapshots


def _restore_project_files(project_path: str, snapshots: dict[str, bytes | None]) -> None:
    root = os.path.realpath(project_path)
    for rel, data in snapshots.items():
        full = os.path.realpath(os.path.join(project_path, rel))
        try:
            if os.path.commonpath([root, full]) != root:
                continue
        except ValueError:
            continue
        if data is None:
            try:
                if os.path.isfile(full):
                    os.remove(full)
            except OSError:
                pass
        else:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(data)
