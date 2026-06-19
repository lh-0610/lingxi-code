"""Git 工具：只读（git_diff / git_log / git_status）+ 写操作（git_stage / git_unstage /
git_commit，执行前强制弹确认卡、绝不 push）。从 tools.py 拆出的兄弟模块。

build_git_write_confirmation 供 streaming 在执行 git 写工具前弹确认卡用（tools.py re-export）。
"""
import os
import subprocess

from langchain_core.tools import tool

from .tools_common import _project_cwd, _resolve_path
from .verification import mark_diff_reviewed as _v_mark_diff_reviewed


@tool
def git_diff(path: str = "", staged: bool = False, max_chars: int = 8000) -> str:
    """查看 git 改动（默认未暂存的工作区改动）。path: 限定文件/目录（相对项目根，空=全部）。
    staged=True 看已暂存（git add 过）的改动。只读、调研用。"""
    try:
        import shutil as _shutil
        if not _shutil.which("git"):
            return "git 未安装或不在 PATH 中，无法查看 diff。"

        cwd = _project_cwd()
        cmd = ["git", "diff"]

        if staged:
            cmd.append("--staged")

        if path:
            resolved = _resolve_path(path)
            root = _project_cwd()
            try:
                if os.path.commonpath([os.path.realpath(resolved), os.path.realpath(root)]) != os.path.realpath(root):
                    return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
            except ValueError:
                return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
            cmd.extend(["--", path])

        result = subprocess.run(
            cmd, cwd=cwd,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if "not a git repository" in stderr.lower():
                return "当前项目不是 git 仓库。"
            return f"git diff 执行出错: {stderr or '未知错误'}"

        output = result.stdout or ""
        if not output.strip():
            return "暂存区没有改动。" if staged else "工作区干净，没有未提交改动。"

        if len(output) > max_chars:
            output = (
                output[:max_chars]
                + f"\n\n... [输出已截断（共 {len(output)} 字符），"
                f"可用 path 参数缩小到具体文件/目录查看]"
            )
        # 标记 diff 已审查（验证状态）
        try:
            from . import session as _session
            _v_mark_diff_reviewed(_session.get_verification())
        except Exception:
            pass
        return output

    except FileNotFoundError:
        return "git 未安装或不在 PATH 中，无法查看 diff。"
    except subprocess.TimeoutExpired:
        return "git diff 执行超时（10s），仓库可能过大。"
    except Exception as e:
        return f"git diff 执行异常: {e}"


@tool
def git_log(path: str = "", limit: int = 15) -> str:
    """查看最近 git 提交历史（短 hash + 日期 + 提交信息 + 改动文件）。
    path: 限定某文件/目录（相对项目根）。limit: 条数。只读。"""
    try:
        import shutil as _shutil
        if not _shutil.which("git"):
            return "git 未安装或不在 PATH 中，无法查看 log。"

        cwd = _project_cwd()
        cmd = [
            "git", "log",
            "-n", str(limit),
            "--date=short",
            "--pretty=format:%h %ad %s",
            "--stat",
        ]

        if path:
            resolved = _resolve_path(path)
            root = _project_cwd()
            try:
                if os.path.commonpath([os.path.realpath(resolved), os.path.realpath(root)]) != os.path.realpath(root):
                    return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
            except ValueError:
                return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
            cmd.extend(["--", path])

        result = subprocess.run(
            cmd, cwd=cwd,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if "not a git repository" in stderr.lower():
                return "当前项目不是 git 仓库。"
            if "does not have any commits" in stderr:
                return "仓库还没有任何提交记录。"
            return f"git log 执行出错: {stderr or '未知错误'}"

        output = result.stdout or ""
        if not output.strip():
            return "没有提交历史（仓库可能为空或 path 下无提交记录）。"

        if len(output) > 8000:
            output = output[:8000] + "\n\n... [输出已截断，可减小 limit 或用 path 限定查看]"
        return output

    except FileNotFoundError:
        return "git 未安装或不在 PATH 中，无法查看 log。"
    except subprocess.TimeoutExpired:
        return "git log 执行超时（10s）。"
    except Exception as e:
        return f"git log 执行异常: {e}"


def _is_inside_git_repo(cwd: str) -> bool:
    """检测 cwd 是否在 git 仓库内。"""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _git_available() -> bool:
    """git 是否可用。"""
    import shutil as _shutil
    return _shutil.which("git") is not None


def build_git_write_confirmation(name: str, args: dict | None = None) -> str:
    """构造 Git 写操作确认文案；只读，不改变索引或工作区。"""
    args = args or {}
    if name == "git_commit":
        message = str(args.get("message", "") or "").strip()
        cwd = _project_cwd()
        staged = ""
        if _git_available() and _is_inside_git_repo(cwd):
            try:
                result = subprocess.run(
                    ["git", "diff", "--cached", "--name-status"],
                    cwd=cwd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=10,
                )
                staged = (result.stdout or "").strip()
            except Exception:
                staged = ""
        staged_text = staged or "（暂存区为空，工具会拒绝提交）"
        return (
            "将执行 Git 写操作：创建本地提交\n\n"
            f"提交信息：{message or '（空）'}\n\n"
            "将提交当前暂存区的全部内容：\n"
            f"{staged_text}\n\n"
            "不会自动暂存其它文件，也不会 push。是否允许？"
        )

    paths = args.get("paths") or []
    if isinstance(paths, list):
        path_text = "\n".join(f"- {p}" for p in paths) or "（未提供路径）"
    else:
        path_text = str(paths)
    action = "暂存指定路径" if name == "git_stage" else "取消暂存指定路径"
    effect = (
        "会修改 Git 暂存区，不会创建提交，也不会 push。"
        if name == "git_stage"
        else "只修改 Git 暂存区，工作区文件内容保持不变。"
    )
    return (
        f"将执行 Git 写操作：{action}\n\n"
        f"目标路径：\n{path_text}\n\n"
        f"{effect}\n是否允许？"
    )


def _validate_git_paths(paths, root: str) -> tuple[list[str] | None, str]:
    """校验路径列表：非空、无通配符、无越界、存在性。

    返回 (rel_paths, error_msg)。成功时 error_msg 为空串。
    """
    if not paths:
        return None, "失败：路径列表不能为空，请指定具体文件或目录。"
    if not isinstance(paths, list):
        return None, "失败：paths 参数必须是字符串列表。"

    root_real = os.path.realpath(root)
    rel_paths = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            return None, f"失败：路径无效（非空字符串）：{p!r}"
        p_clean = p.strip()
        # 拒绝通配符 / shell 危险字符
        if p_clean in (".", "*", "./"):
            return None, f"失败：不允许暂存 '{p_clean}'（只允许具体路径）。"
        if any(c in p_clean for c in ("&", "|", ";", "$", "`", "\n", "\r")):
            return None, f"失败：路径包含非法字符：{p_clean!r}"
        # 解析并检查越界
        resolved = _resolve_path(p_clean)
        abs_real = os.path.realpath(resolved)
        try:
            if os.path.commonpath([abs_real, root_real]) != root_real:
                return None, f"失败：路径超出项目范围：{p_clean}"
        except ValueError:
            return None, f"失败：路径超出项目范围：{p_clean}"
        # 转成相对路径传给 git
        rel = os.path.relpath(abs_real, root_real).replace("\\", "/")
        rel_paths.append(rel)
    return rel_paths, ""


@tool
def git_status(max_chars: int = 12000) -> str:
    """查看当前仓库状态，包含分支、ahead/behind、暂存/未暂存/未跟踪文件。只读。"""
    try:
        if not _git_available():
            return "git 未安装或不在 PATH 中。"
        cwd = _project_cwd()
        if not _is_inside_git_repo(cwd):
            return "当前项目不是 git 仓库。"

        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            return f"git status 执行出错: {stderr or '未知错误'}"

        output = result.stdout or ""
        if not output.strip():
            output = "(仓库干净，无任何改动)\n"

        # 截断提示
        truncated = ""
        if len(output) > max_chars:
            truncated = f"\n\n... [输出已截断（共 {len(output)} 字符），可用 path 参数缩小范围]"
            output = output[:max_chars]

        # 解析分支行
        lines = output.strip().splitlines()
        branch_line = lines[0] if lines else ""
        # 统计文件状态
        staged = sum(1 for ln in lines[1:] if ln and ln[0] in "MADRC")
        unstaged = sum(1 for ln in lines[1:] if len(ln) > 1 and ln[1] in "MD")
        untracked = sum(1 for ln in lines[1:] if ln.startswith("??"))

        summary_parts = []
        if staged:
            summary_parts.append(f"已暂存: {staged}")
        if unstaged:
            summary_parts.append(f"未暂存: {unstaged}")
        if untracked:
            summary_parts.append(f"未跟踪: {untracked}")

        result_text = output.rstrip() + truncated
        result_text += "\n\n--- 摘要 ---"
        if branch_line:
            result_text += f"\n{branch_line}"
        if summary_parts:
            result_text += "\n" + " | ".join(summary_parts)
        else:
            result_text += "\n工作区干净。"
        result_text += "\n\n💡 提交前建议先运行 git_diff(staged=True) 查看暂存区内容。"
        return result_text

    except FileNotFoundError:
        return "git 未安装或不在 PATH 中。"
    except subprocess.TimeoutExpired:
        return "git status 执行超时（10s）。"
    except Exception as e:
        return f"git status 执行异常: {e}"


@tool
def git_stage(paths: list[str]) -> str:
    """暂存指定路径。只接受明确路径列表，不接受空列表、通配符或 '.'。"""
    try:
        if not _git_available():
            return "git 未安装或不在 PATH 中。"
        cwd = _project_cwd()
        if not _is_inside_git_repo(cwd):
            return "当前项目不是 git 仓库。"

        rel_paths, err = _validate_git_paths(paths, cwd)
        if err:
            return err

        cmd = ["git", "add", "--"] + rel_paths
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            return f"git add 执行出错: {stderr or '未知错误'}"

        # 获取暂存区摘要
        cached = subprocess.run(
            ["git", "diff", "--cached", "--name-status"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        cached_output = (cached.stdout or "").strip()

        output = "✅ 已暂存以下路径:\n" + "\n".join(f"  · {p}" for p in rel_paths)
        if cached_output:
            output += f"\n\n--- 当前暂存区 ---\n{cached_output}"
        output += "\n\n💡 建议继续用 git_diff(staged=True) 审查暂存内容，确认无误后再提交。"
        return output

    except FileNotFoundError:
        return "git 未安装或不在 PATH 中。"
    except subprocess.TimeoutExpired:
        return "git add 执行超时（30s）。"
    except Exception as e:
        return f"git stage 执行异常: {e}"


@tool
def git_unstage(paths: list[str]) -> str:
    """取消暂存指定路径，不修改工作区内容。"""
    try:
        if not _git_available():
            return "git 未安装或不在 PATH 中。"
        cwd = _project_cwd()
        if not _is_inside_git_repo(cwd):
            return "当前项目不是 git 仓库。"

        rel_paths, err = _validate_git_paths(paths, cwd)
        if err:
            return err

        # 优先用 git restore --staged（Git 2.23+），不支持则降级到 git reset --
        cmd = ["git", "restore", "--staged", "--"] + rel_paths
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        if result.returncode != 0:
            # 降级
            cmd = ["git", "reset", "--"] + rel_paths
            result = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                return f"git unstage 执行出错: {stderr or '未知错误'}"

        # 获取暂存区摘要
        cached = subprocess.run(
            ["git", "diff", "--cached", "--name-status"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        cached_output = (cached.stdout or "").strip()

        output = "✅ 已取消暂存:\n" + "\n".join(f"  · {p}" for p in rel_paths)
        if cached_output:
            output += f"\n\n--- 当前暂存区 ---\n{cached_output}"
        else:
            output += "\n\n--- 当前暂存区为空 ---"
        output += "\n\nℹ️ 工作区文件内容未被修改。"
        return output

    except FileNotFoundError:
        return "git 未安装或不在 PATH 中。"
    except subprocess.TimeoutExpired:
        return "git unstage 执行超时（30s）。"
    except Exception as e:
        return f"git unstage 执行异常: {e}"


@tool
def git_commit(message: str) -> str:
    """基于当前暂存区创建本地提交。不会自动暂存文件，不会 push。"""
    try:
        if not _git_available():
            return "git 未安装或不在 PATH 中。"
        cwd = _project_cwd()
        if not _is_inside_git_repo(cwd):
            return "当前项目不是 git 仓库。"

        # 校验 message
        if not message or len(message.strip()) < 3:
            return "失败：提交信息不能为空，且去掉空白后长度需 >= 3 个字符。"

        # 检查暂存区是否为空
        diff_cached = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        if diff_cached.returncode == 0:
            # returncode=0 表示暂存区和 HEAD 一样 → 没有暂存内容
            return "失败：暂存区为空，没有可提交的内容。请先用 git_stage 暂存文件。"

        # 检查工作区未暂存改动 / 未跟踪文件（仅提示，不阻止）
        warnings = []
        status_result = subprocess.run(
            ["git", "status", "--short"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        if status_result.returncode == 0:
            status_lines = (status_result.stdout or "").strip().splitlines()
            unstaged_files = []
            untracked_files = []
            for line in status_lines:
                if not line or len(line) < 2:
                    continue
                # 第二列是工作区状态（暂存区由 commit 处理）
                if line[0] == "?" and line[1] == "?":
                    untracked_files.append(line[3:].strip() if len(line) > 3 else "")
                elif line[1] in "MD":
                    unstaged_files.append(line[3:].strip() if len(line) > 3 else "")
            if unstaged_files:
                warnings.append("⚠️ 以下文件有未暂存改动，不会进入本次提交:\n" +
                                "\n".join(f"  · {f}" for f in unstaged_files[:20]))
            if untracked_files:
                warnings.append("⚠️ 以下未跟踪文件不会进入本次提交:\n" +
                                "\n".join(f"  · {f}" for f in untracked_files[:20]))

        # 获取暂存文件列表（给提交后的摘要用）
        staged_names = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        staged_files = [f for f in (staged_names.stdout or "").strip().splitlines() if f.strip()]

        # 执行提交
        cmd = ["git", "commit", "-m", message.strip()]
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            # 常见失败：user.name/email 未配置 / pre-commit hook
            msg = stderr or stdout or "未知错误"
            if "user.name" in msg or "user.email" in msg or "Author identity" in msg:
                return f"提交失败：Git 用户信息未配置。请先运行:\n  git config user.name \"你的名字\"\n  git config user.email \"你的邮箱\"\n\n详细: {msg}"
            if "pre-commit" in msg.lower() or "hook" in msg.lower():
                return f"提交失败：pre-commit hook 拦截。请修复后重试。\n\n详细: {msg}"
            return f"提交失败: {msg}"

        # 解析 commit hash
        hash_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5,
        )
        commit_hash = (hash_result.stdout or "").strip() or "unknown"

        output = f"✅ 提交成功: {commit_hash}\n"
        output += f"   信息: {message.strip()}\n"
        if staged_files:
            output += f"   包含 {len(staged_files)} 个文件:\n"
            output += "\n".join(f"     · {f}" for f in staged_files[:30])
            if len(staged_files) > 30:
                output += f"\n     ... 等共 {len(staged_files)} 个文件"
        if warnings:
            output += "\n\n" + "\n\n".join(warnings)
        output += "\n\nℹ️ 仅创建本地提交，未执行 push。"
        return output

    except FileNotFoundError:
        return "git 未安装或不在 PATH 中。"
    except subprocess.TimeoutExpired:
        return "git commit 执行超时（30s）。"
    except Exception as e:
        return f"git commit 执行异常: {e}"
