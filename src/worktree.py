"""Git Worktree 隔离模式。

让 AI 的文件修改在独立 worktree 中进行，主工作区保持不变。
功能：创建/完成/清理隔离 worktree，以及路径路由。
"""

import os
import json
from contextlib import contextmanager
import re
import shutil
import subprocess
import logging
import tempfile
import threading
import functools

logger = logging.getLogger(__name__)

# 运行期活跃 worktree 注册表：session_id → 路径、分支及持久化的基点/目标身份
_WORKTREES: dict[str, dict] = {}
_METADATA_FILE = "lingxi-worktree.json"


class WorktreeMetadataError(RuntimeError):
    """隔离区基点或目标身份无法验证；必须保留数据，不能自动合并。"""

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


def _git_output(path: str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True,
        encoding="utf-8", errors="strict", check=True, timeout=30,
    )
    return result.stdout.strip()


def _git_path(path: str, name: str) -> str:
    value = _git_output(path, "rev-parse", "--git-path", name)
    return os.path.realpath(os.path.join(path, value))


def _admin_dir(path: str) -> str:
    return os.path.realpath(_git_output(path, "rev-parse", "--absolute-git-dir"))


def _common_dir(path: str) -> str:
    value = _git_output(path, "rev-parse", "--git-common-dir")
    return os.path.realpath(os.path.join(path, value))


def _worktree_info_from_path(wt_path: str) -> dict:
    """从独立 Git admin 目录读取并验证基点和目标，绝不从 .git 指针猜 checkout。"""
    try:
        wt_path = os.path.realpath(wt_path)
        if not is_git_repo(wt_path):
            raise ValueError("隔离区不是有效的 Git 工作树根目录")
        admin = _admin_dir(wt_path)
        with open(os.path.join(admin, _METADATA_FILE), encoding="utf-8") as f:
            info = json.load(f)
        if not isinstance(info, dict) or info.get("version") != 1:
            raise ValueError("不支持的元数据格式")
        for key in ("path", "project_path", "git_dir", "project_git_dir"):
            value = info.get(key)
            if not isinstance(value, str) or not os.path.isabs(value):
                raise ValueError(f"缺少有效的 {key}")
            if os.path.realpath(value) != value:
                raise ValueError(f"{key} 的路径身份已改变")
        if info["path"] != wt_path or info["git_dir"] != admin:
            raise ValueError("隔离区路径或 Git admin 身份不符")
        project = info["project_path"]
        if project == wt_path or not is_git_repo(project):
            raise ValueError("目标项目不存在或不是 Git 工作树根目录")
        if _admin_dir(project) != info["project_git_dir"]:
            raise ValueError("目标项目 Git admin 身份已改变")
        if _common_dir(project) != _common_dir(wt_path):
            raise ValueError("隔离区与目标不属于同一仓库")
        base = info.get("base_sha")
        if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base):
            raise ValueError("缺少有效的 base_sha")
        if _git_output(wt_path, "rev-parse", "--verify", f"{base}^{{commit}}") != base:
            raise ValueError("基点提交无效")
        _git_output(wt_path, "merge-base", "--is-ancestor", base, "HEAD")
        branch = info.get("branch")
        if not isinstance(branch, str) or not branch.startswith("lingxi/"):
            raise ValueError("隔离分支元数据无效")
        if _git_output(wt_path, "symbolic-ref", "--short", "HEAD") != branch:
            raise ValueError("隔离分支已改变")
        for registered in _WORKTREES.values():
            if registered.get("path") == wt_path and registered != info:
                raise ValueError("磁盘元数据与注册表不一致")
        return info
    except Exception as exc:
        raise WorktreeMetadataError(
            f"隔离区元数据缺失或无效，拒绝自动合并，已保留隔离区；可显式丢弃：{exc}"
        ) from exc


@contextmanager
def _temporary_index(path: str):
    # 临时索引同时用于读取净变化和应用补丁，不能改变用户或子 Agent 的暂存状态。
    with tempfile.TemporaryDirectory(prefix="lingxi-index-") as directory:
        temp_index = os.path.join(directory, "index")
        index_path = _git_path(path, "index")
        if os.path.exists(index_path):
            shutil.copy2(index_path, temp_index)
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = temp_index
        if not os.path.exists(temp_index):
            subprocess.run(
                ["git", "read-tree", "HEAD"], cwd=path, env=env,
                capture_output=True, check=True, timeout=30,
            )
        yield env


def _worktree_changes(wt_path: str, base: str, *, include_patch: bool = False):
    with _temporary_index(wt_path) as env:
        subprocess.run(
            ["git", "add", "-A"], cwd=wt_path, env=env,
            capture_output=True, check=True, timeout=30,
        )
        result = subprocess.run(
            ["git", "diff", "--cached", "--no-renames", "--name-only", "-z", base, "--"],
            cwd=wt_path, env=env, capture_output=True, check=True, timeout=30,
        )
        files = [os.fsdecode(p) for p in result.stdout.split(b"\0") if p]
        patch = b""
        if include_patch and files:
            patch = subprocess.run(
                ["git", "diff", "--cached", "--no-renames", "--binary", base, "--"],
                cwd=wt_path, env=env, capture_output=True, check=True, timeout=30,
            ).stdout
        return files, patch


@_synchronized
def changed_files(wt_path: str) -> list[str]:
    """返回相对持久化 base_sha 的净变化路径（相对隔离区根）。

    包含已提交、未提交、未跟踪文件和删除，重命名返回旧/新两条路径；遵循 Git ignore。
    不改变实际索引。元数据无效抛 WorktreeMetadataError，Git 读取失败也抛异常，绝不伪装成空列表。
    """
    info = _worktree_info_from_path(wt_path)
    files, _ = _worktree_changes(wt_path, info["base_sha"])
    return files


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

    project_path = os.path.realpath(str(project_path))
    # 幂等恢复也必须重新验证磁盘元数据，不能让内存缓存掩盖基点丢失。
    if session_id in _WORKTREES:
        info = _WORKTREES[session_id]
        if os.path.isdir(info["path"]):
            session.worktree = info["path"]
            try:
                recovered = _worktree_info_from_path(info["path"])
                if recovered["project_path"] != project_path:
                    raise WorktreeMetadataError("目标项目与创建时不符，已保留隔离区。")
                return info["path"]
            except WorktreeMetadataError as exc:
                logger.error(str(exc))
                return None

    if not is_git_repo(project_path):
        return None

    branch = f"lingxi/{_sanitize_branch(session_id)}"
    wt_dir = os.path.join(project_path, ".lingxi-worktrees")
    wt_path = os.path.join(wt_dir, session_id)

    try:
        if not _is_within(wt_path, wt_dir) or os.path.realpath(wt_path) == os.path.realpath(wt_dir):
            raise ValueError("worktree 路径越界")
        os.makedirs(wt_dir, exist_ok=True)
        _ensure_worktree_excluded(project_path)

        # 未知或旧版隔离区不能删除重建；保留会话路径，允许用户显式丢弃。
        if os.path.lexists(wt_path):
            session.worktree = wt_path
            info = _worktree_info_from_path(wt_path)
            if info["project_path"] != project_path:
                raise WorktreeMetadataError("目标项目与创建时不符，已保留隔离区。")
            _WORKTREES[session_id] = info
            logger.info(f"已恢复隔离 worktree: {wt_path} (branch={info['branch']})")
            return wt_path

        base = _git_output(project_path, "rev-parse", "--verify", "HEAD^{commit}")
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, wt_path, base],
            cwd=project_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            check=True, timeout=30,
        )
        session.worktree = wt_path
        info = {
            "version": 1, "path": os.path.realpath(wt_path), "branch": branch,
            "base_sha": base, "project_path": project_path,
            "git_dir": _admin_dir(wt_path), "project_git_dir": _admin_dir(project_path),
        }
        metadata_path = os.path.join(info["git_dir"], _METADATA_FILE)
        with open(metadata_path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
        os.replace(metadata_path + ".tmp", metadata_path)
        _worktree_info_from_path(wt_path)
        _WORKTREES[session_id] = info
        logger.info(f"已创建隔离 worktree: {wt_path} (branch={branch}, base={base})")
        return wt_path

    except Exception as e:
        logger.error(f"创建或恢复 worktree 失败，现有隔离数据未清理: {e}")
        return None


@_synchronized
def finish(session, *, apply_changes: bool = False) -> tuple[bool, str]:
    """结束会话的 worktree。

    ``apply_changes=True`` 时先把隔离区相对创建基点的改动应用回原项目，成功后再清理；
    否则只丢弃隔离区并清理。返回 ``(success, message)``。
    """
    wt_path = session.worktree
    if not wt_path:
        return True, "没有活跃的 worktree。"

    if apply_changes:
        try:
            info = _worktree_info_from_path(wt_path)
        except WorktreeMetadataError as exc:
            return False, str(exc)
        ok, msg = _apply_changes_to_project(wt_path, info["project_path"])
        if not ok:
            return False, msg

    # 显式丢弃只需定位隔离区所在仓库，无须猜测原项目 checkout 或基点。
    branch = _branch_for_worktree(wt_path, wt_path)
    try:
        _remove_worktree(wt_path, branch)
    except Exception as exc:
        return False, f"清理隔离区失败，已保留会话路径：{exc}"
    for sid, info in list(_WORKTREES.items()):
        if info["path"] == wt_path:
            _WORKTREES.pop(sid, None)

    session.worktree = None
    if apply_changes:
        return True, "隔离区改动已应用回主项目，并已清理 worktree。"
    return True, "隔离区已丢弃并清理。"


@_synchronized
def cleanup_all() -> None:
    """清理所有注册的 worktree。调用时机：程序退出。"""
    for sid, info in list(_WORKTREES.items()):
        try:
            _worktree_info_from_path(info["path"])
            _remove_worktree(info["path"], info["branch"])
        except Exception as e:
            logger.warning(f"清理 worktree {sid} 失败，已保留隔离区: {e}")
        else:
            _WORKTREES.pop(sid, None)


# ── 内部 ──────────────────────────────────────────────────────────────────────


def _remove_worktree(wt_path: str, branch: str | None) -> None:
    """通过 Git common dir 移除隔离区，不把 common dir 当作目标 checkout。"""
    if not os.path.exists(wt_path):
        return
    if not is_git_repo(wt_path):
        _cleanup_worktree(wt_path)
        return
    common = _common_dir(wt_path)
    if _admin_dir(wt_path) == common:
        raise ValueError("拒绝删除非 linked worktree 的项目目录")
    subprocess.run(
        ["git", "--git-dir", common, "worktree", "remove", "--force", wt_path],
        capture_output=True, check=True, timeout=30,
    )
    if branch and branch.startswith("lingxi/"):
        subprocess.run(
            ["git", "--git-dir", common, "branch", "-D", branch],
            capture_output=True, timeout=10,
        )


def _apply_changes_to_project(wt_path: str, project_path: str | None) -> tuple[bool, str]:
    """把 worktree 相对持久化基点的净改动应用到创建时的项目工作区。"""
    snapshots = None
    try:
        info = _worktree_info_from_path(wt_path)
        if not project_path or os.path.realpath(project_path) != info["project_path"]:
            return False, "目标项目与隔离区元数据不符，已保留隔离区未清理。"
        changed, patch = _worktree_changes(wt_path, info["base_sha"], include_patch=True)
        if not patch:
            return True, "隔离区没有需要恢复的改动。"
        snapshots = _snapshot_project_files(project_path, changed)

        with _temporary_index(project_path) as git_env:
            check = subprocess.run(
                ["git", "apply", "--check", "--3way", "--binary"],
                cwd=project_path, input=patch, capture_output=True,
                env=git_env, timeout=30,
            )
            if check.returncode != 0:
                stderr = check.stderr.decode("utf-8", errors="replace") if check.stderr else ""
                return False, (
                    "恢复隔离区改动失败，已保留 worktree。"
                    f"\n{stderr.strip() or '请检查主项目是否有冲突或未提交改动。'}"
                )

            apply = subprocess.run(
                ["git", "apply", "--3way", "--binary"],
                cwd=project_path, input=patch, capture_output=True,
                env=git_env, timeout=30,
            )
            if apply.returncode != 0:
                _restore_project_files(project_path, snapshots)
                stderr = apply.stderr.decode("utf-8", errors="replace") if apply.stderr else ""
                return False, (
                    "恢复隔离区改动失败，已保留 worktree。"
                    f"\n{stderr.strip() or '请检查主项目是否有冲突或未提交改动。'}"
                )
        return True, "隔离区改动已应用到主项目工作区。"
    except Exception as e:
        if snapshots is not None:
            _restore_project_files(project_path, snapshots)
        return False, f"恢复隔离区改动异常，已保留 worktree：{e}"


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
