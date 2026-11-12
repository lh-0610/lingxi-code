"""Git 工具：只读（git_diff / git_log / git_status）+ 写操作（git_stage / git_unstage /
git_commit，执行前强制弹确认卡、绝不 push）。从 tools.py 拆出的兄弟模块。

build_git_write_confirmation 供 streaming 在执行 git 写工具前弹确认卡用（tools.py re-export）。
"""
import os
import subprocess

from langchain_core.tools import tool

from .limits import GIT_DIFF_OVERVIEW_MAX_CHARS
from .tools_common import _project_cwd, _resolve_path
from .verification import covers as _v_covers
from .verification import mark_diff_reviewed as _v_mark_diff_reviewed
from .verification import record_diff_files as _v_record_diff_files
from .verification import record_diff_side as _v_record_diff_side


def _mark_reviewed(root):
    """把"已审阅 diff"记进当前会话的验证状态（无会话/无状态时静默跳过）。

    root：这次 diff **实际覆盖**的范围——全工作区就是执行目录，限定了 path 就是那个路径。
    按目录记着的义务只能被覆盖到它的 diff 了结。
    """
    try:
        from . import session as _session
        _v_mark_diff_reviewed(_session.get_verification(), root=root)
    except Exception:
        pass


def _record_diff_files(cwd, rel_paths):
    """整段展示出来的文件按绝对路径记进审阅记录，**不**解除范围级义务（见 record_diff_files）。"""
    try:
        from . import session as _session
        _v_record_diff_files(_session.get_verification(),
                             [os.path.join(cwd, rel) for rel in rel_paths if rel])
    except Exception:
        pass


def _record_diff_sides(cwd, shown_rel, staged, other):
    """整段展示出来的文件记下"这一侧看过了"，返回**只看到一半**的那些（相对项目根）。

    other 是另一侧此刻有改动的文件集合，None = 枚举不出来（按"还有改动"算，不知道就不放行）。
    另一侧确实没改动的、或两侧都已经记过的，才算这个文件看完。
    """
    if not shown_rel:
        return []
    from . import session as _session
    try:
        v = _session.get_verification()
    except Exception:
        return list(shown_rel)
    side = "staged" if staged else "unstaged"
    partial = []
    for rel in shown_rel:
        if not rel:
            continue
        both = _v_record_diff_side(v, os.path.join(cwd, rel), side)
        if not both and (other is None or rel in other):
            partial.append(rel)
    return partial


# 提示里最多点名几个文件，其余只报数量——工具结果要能读，不是要把仓库列一遍。
_UNCOVERED_SAMPLE = 20


def _render_change(rel, abs_path, baseline, room):
    """非 Git 项目里一个改动文件的展示文字 + 是否完整展示了。

    baseline 是 file_history.session_baseline 的结果：有可信的改前状态就给对比，没有就给
    完整的当前内容。放不下（超过 room）就不展示，交给调用方记成"没看全"。"""
    import difflib
    before, known = baseline
    try:
        with open(abs_path, "rb") as stream:
            now = stream.read()
    except FileNotFoundError:
        now = None
    except OSError as error:
        return f"### {rel}\n读取失败（{error}），无法展示。", False
    if any(b is not None and b"\0" in b[:8192] for b in (before, now)):
        size = len(now) if now is not None else 0
        return f"### {rel}\n二进制文件（现在 {size} 字节），不展示内容。", True

    def _lines(data):
        return data.decode("utf-8", errors="replace").splitlines(keepends=True) if data else []

    if known:
        title = f"### {rel}（对比本会话里 AI 第一次改它之前的版本）"
        if now is None:
            title += "（文件已删除）"
        body = "".join(difflib.unified_diff(_lines(before), _lines(now),
                                            fromfile=f"a/{rel}", tofile=f"b/{rel}")) or "（和改前完全一样）"
    elif now is None:
        return f"### {rel}\n文件已经不存在了，也没有改前版本可对比。", True
    else:
        title = f"### {rel}（没有改前版本，以下是完整的当前内容）"
        body = now.decode("utf-8", errors="replace")
    text = f"{title}\n{body}"
    if len(text) > room:
        return (f"{title}\n（内容太长，这次没展示。单独看：git_diff(path=\"{rel}\", max_chars=30000)）",
                False)
    return text, True


def _review_without_git(path: str, max_chars: int, reason: str) -> str:
    """当前项目不是 Git 仓库（或没装 git）时的"看改动"：逐个展示本轮改过的文件。

    **全部改动文件都完整展示了才记为已审阅**。原来这里只回一句"不是 git 仓库"、从不记审阅，
    于是非 Git 项目里改完代码这一轮永远收不了 completed，完成闸门还会把模型打回去白跑几轮。
    看不全的照旧不放行，并点名去哪儿单独看。"""
    from . import file_history as _fh
    from . import session as _session

    v = _session.get_verification()
    located = dict(v.get("dirty_abs") or {})
    targets = [(rel, located.get(rel) or _resolve_path(rel)) for rel in v.get("dirty_files") or []]
    if path:
        want = os.path.realpath(_resolve_path(path))
        targets = [(rel, p) for rel, p in targets
                   if os.path.realpath(p) == want
                   or os.path.realpath(p).startswith(want.rstrip(os.sep) + os.sep)]
    if not targets:
        where = f"{path} 在本轮" if path else "本轮"
        return f"{reason}{where}也没有记录到文件改动。"

    sid = getattr(_session.current_session(), "current_session_id", "") or ""
    workspace = _fh.session_workspace()
    try:
        budget = max(1000, min(int(max_chars), 60000))
    except (TypeError, ValueError):
        budget = 8000
    parts = [f"{reason}下面逐个列出本轮改过的文件：有改前版本的给出对比，没有的给出完整的当前内容。"]
    shown, missing = [], []
    for rel, abs_path in targets:
        room = budget - sum(len(p) + 2 for p in parts)
        section, complete = _render_change(rel, abs_path, _fh.session_baseline(sid, workspace, abs_path), room)
        parts.append(section)
        (shown if complete else missing).append((rel, abs_path))
    if missing:
        parts.append("以下文件这次没展示全，这一项先不算看过：" + "、".join(rel for rel, _ in missing))
    else:
        for _rel, abs_path in shown:
            _mark_reviewed(abs_path)
    return "\n\n".join(parts)


def _uncovered_changes(cwd):
    """默认 `git diff` 看不见的那些改动：(已暂存文件, 未跟踪文件, 错误原因)。

    只用于判断"空 diff 能不能代表整个项目干净"。任何一步失败都返回错误原因，
    由调用方保留义务——核对不了就是不知道，不知道不放行。
    """
    def _names(cmd):
        done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10,
                              stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            raise RuntimeError((done.stderr or "").strip() or f"{' '.join(cmd)} 返回非零")
        return [line for line in (done.stdout or "").splitlines() if line.strip()]

    try:
        staged_files = _names(["git", "diff", "--cached", "--name-only", "--relative"])
        # --exclude-standard 尊重 .gitignore：构建产物、缓存不该被当成"未审阅的改动"
        untracked = _names(["git", "ls-files", "--others", "--exclude-standard"])
    except subprocess.TimeoutExpired:
        return [], [], "核对超时（10s）"
    except Exception as e:
        return [], [], str(e)
    return staged_files, untracked, ""


# git_diff 一次最多把这么多个未跟踪的新文件整份附上，再多的只列名字、不算看过
_NEW_FILES_MAX = 10


def _untracked_as_diff(cwd, path, max_chars):
    """范围内（path 为空 = 整个项目）未跟踪的新文件，写成"整文件新增"的 diff。

    返回 (文本, 已整份给出的文件, 范围内全部新文件)。被 .gitignore 忽略的不算。
    """
    listing = _git_text(cwd, ["ls-files", "--others", "--exclude-standard", "--", path or "."])
    files = [f for f in (listing or "").splitlines() if f.strip()]
    parts, shown = [], []
    for rel in files[:_NEW_FILES_MAX]:
        head = f"diff --git a/{rel} b/{rel}\nnew file (git 还没跟踪)\n--- /dev/null\n+++ b/{rel}"
        try:
            content = _read_text(cwd, rel, max_chars + 1)
        except OSError as e:
            parts.append(f"{head}\n（读取失败：{e}）")
            continue
        if len(content) > max_chars:
            # 读不全就不算"整份给出了"：这份记录后面要用来判目录是不是看全了，
            # 半截内容混进去等于把没看过的改动记成看过。
            parts.append(f"{head}\n（文件超过 {max_chars} 字，这次没展示。"
                         f"单独看：git_diff(path=\"{rel}\", max_chars=30000)）")
            continue
        if "\x00" in content:
            parts.append(f"{head}\n（二进制文件，不显示内容）")
        else:
            parts.append(head + "".join(f"\n+{ln}" for ln in content.splitlines()))
        shown.append(rel)
    if len(files) > _NEW_FILES_MAX:
        rest = files[_NEW_FILES_MAX:]
        parts.append(f"（还有 {len(rest)} 个未跟踪的新文件没附上：{', '.join(rest[:_UNCOVERED_SAMPLE])}"
                     " —— 用 git_diff(path=...) 逐个看）")
    return ("\n".join(parts) + "\n") if parts else "", shown, files


def _describe_uncovered(staged_files, untracked, empty: bool, path: str = ""):
    """告诉模型：默认 diff 还有没看到的东西，以及去哪儿看。

    措辞按 diff 空不空分开——上面明明列着未暂存的改动还说"没有未暂存的改动"是自相矛盾
    （与核对失败那条提前返回同一个坑）。path 非空时说的是**那个范围**，不是整个工作区，
    补看的命令也带上同一个 path。
    """
    head = (("这个范围" if path else "工作区") + "没有**未暂存**的改动，但默认 diff 看不到下面这些，"
            if empty else "上面的改动之外，默认 diff 还看不到下面这些，")
    parts = [head + ("因此还不能认为这个范围已经审阅过：" if path
                     else "因此还不能认为整个项目已经审阅过：")]
    staged_how = f'git_diff(path="{path}", staged=True)' if path else "git_diff(staged=True)"
    for label, files, how in (
        ("已暂存的修改", staged_files, f"用 {staged_how} 查看"),
        ("未跟踪的新文件", untracked, "用 git_diff(path=...) 逐个查看"),
    ):
        if not files:
            continue
        shown = ", ".join(files[:_UNCOVERED_SAMPLE])
        more = f" 等 {len(files)} 个" if len(files) > _UNCOVERED_SAMPLE else ""
        parts.append(f"- {label}（{len(files)} 个）：{shown}{more} —— {how}")
    return "\n".join(parts)


def _describe_half_shown(partial, staged: bool, path: str) -> str:
    """点名只看到一半的文件，并说清另一半用哪条命令看。"""
    if path:
        how = f'git_diff(path="{path}"' + ("" if staged else ", staged=True") + ")"
    else:
        how = "git_diff()" if staged else "git_diff(staged=True)"
    names = ", ".join(partial[:_UNCOVERED_SAMPLE])
    more = f" 等 {len(partial)} 个" if len(partial) > _UNCOVERED_SAMPLE else ""
    return ("下面这些文件**另一侧的改动还没看过**，这次只展示了一半，不算审阅过：\n"
            f"- {names}{more} —— 用 {how} 看另一半")


def _diff_char_limit(path: str, max_chars) -> tuple[int, bool]:
    """(这次最多交回多少字, 是不是只看单个文件)。

    整个项目 / 一个目录的 diff 封顶 GIT_DIFF_OVERVIEW_MAX_CHARS，max_chars 传得再大也不超过；
    指到单个文件的按 max_chars 给——大改动只能这样看全，非 Git 项目的审阅义务也靠它了结。"""
    try:
        limit = max(1000, int(max_chars))
    except (TypeError, ValueError):
        limit = GIT_DIFF_OVERVIEW_MAX_CHARS
    single = bool(path) and not os.path.isdir(_resolve_path(path))
    return (limit if single else min(limit, GIT_DIFF_OVERVIEW_MAX_CHARS)), single


def _section_path(section: str) -> str:
    """一个文件的 diff 段 → 它改的是哪个文件（相对仓库根）。认不出返回空串。

    认不出就不记：`core.quotepath` 会把非 ASCII 路径写成八进制转义的引号串，
    照着它拼出来的路径指向一个不存在的文件，记进去只会污染审阅记录。
    """
    first = section.split("\n", 1)[0]
    if " b/" not in first:
        return ""
    name = first.rsplit(" b/", 1)[-1].strip()
    return "" if name.startswith('"') else name


def _diff_file_summary(section: str) -> str:
    """一个文件的 diff 段 → `路径（+增 −删）`。"""
    first = section.split("\n", 1)[0]
    name = _section_path(section) or first.strip()
    body = [ln for ln in section.splitlines()[1:] if not ln.startswith(("+++", "---"))]
    added = sum(1 for ln in body if ln.startswith("+"))
    removed = sum(1 for ln in body if ln.startswith("-"))
    return f"{name}（+{added} −{removed}）"


def _clip_diff(output: str, limit: int, single: bool) -> tuple[str, list[str]]:
    """diff 超过 limit 时截短，并交回**整段展示出来了**的那些文件的相对路径。

    第二项是给审阅记录用的：只有整段留在输出里的文件才算看过。单文件截断、
    以及"第一个文件就超长、只展示了它的开头"都一个都不算——半截内容不是审阅。
    """
    import re as _re
    sections = [s for s in _re.split(r"(?m)^(?=diff --git )", output) if s]
    if len(output) <= limit:
        return output, [_section_path(s) for s in sections]
    if single:
        return (output[:limit]
                + f"\n\n... [输出已截断（共 {len(output)} 字符），要看后面的部分把 max_chars 调大]"), []
    kept, size, left = [], 0, []
    for section in sections:
        if not left and size + len(section) <= limit:
            kept.append(section)
            size += len(section)
        else:
            left.append(section)
    if kept:
        shown = "".join(kept).rstrip("\n")
        head = f"改动共 {len(output)} 字符，这次只完整展示了前 {len(kept)} 个文件"
    else:
        shown = left[0][:limit]
        head = f"改动共 {len(output)} 字符，第一个文件就超出了长度，只展示了它的开头"
    names = "、".join(_diff_file_summary(s) for s in left[:_UNCOVERED_SAMPLE])
    more = f" 等 {len(left)} 个" if len(left) > _UNCOVERED_SAMPLE else ""
    text = (f"{shown}\n\n... [{head}。没展示全的：{names}{more}。"
            "要看哪个就 git_diff(path=\"那个文件\")，单个文件不受这个长度限制]")
    return text, [_section_path(s) for s in kept]


def _changed_in_dir(cwd: str, rel_scope: str) -> list[str] | None:
    """这个目录范围内此刻有改动的文件（相对项目根）。枚举不出来返回 None。

    未暂存 + 已暂存 + 未跟踪**全都算**，调用方看的是不是暂存区都一样：判"目录看全了没有"
    时，只数暂存区就会把没暂存的改动当成不存在，逐个看完暂存文件即可替整个目录背书。
    """
    scope = ["--", rel_scope] if rel_scope else []
    files = []
    for args in (["diff", "--name-only", "--relative", *scope],
                 ["diff", "--cached", "--name-only", "--relative", *scope],
                 ["ls-files", "--others", "--exclude-standard", "--", rel_scope or "."]):
        out = _git_text(cwd, args)
        if out is None:
            return None
        files += [ln for ln in out.splitlines() if ln.strip()]
    return files


def _unstaged_leftovers(cwd: str, path: str) -> bool:
    """暂存区之外还有没有改动（未暂存 + 未跟踪）。枚举不出来按"有"算——不知道就不放行。"""
    scope = ["--", path] if path else []
    for args in (["diff", "--name-only", "--relative", *scope],
                 ["ls-files", "--others", "--exclude-standard", "--", path or "."]):
        out = _git_text(cwd, args)
        if out is None or out.strip():
            return True
    return False


def _other_side_changes(cwd: str, path: str, staged: bool) -> set[str] | None:
    """这次**没**展示的那一侧有哪些改动文件（相对项目根）。枚举不出来返回 None。

    暂存视图看不见未暂存的改动，未暂存视图看不见已暂存的改动。未跟踪的新文件只有一侧
    （整份附上就是全部），不在这里数。
    """
    args = ["diff", "--name-only", "--relative"]
    if not staged:
        args.append("--cached")
    if path:
        args.extend(["--", path])
    out = _git_text(cwd, args)
    if out is None:
        return None
    return {ln for ln in out.splitlines() if ln.strip()}


def _mark_range_if_fully_reviewed(cwd: str, path: str) -> None:
    """范围内的改动是不是已经被逐个完整看过了？是就把这个范围记成已审阅。

    盲区义务按**目录**记着，而目录级 diff 有 8000 字硬上限、max_chars 抬不动：改动一多
    就必然截断，逐个文件看又只记到文件路径，`covers(文件, 目录)` 永远 False——两头都够不着，
    义务无解。这里反过来用累计的按文件记录判目录：范围内**每一个**改动文件都被某条记录
    覆盖才记目录。`covers` 同时守住 Git 工作区边界，嵌套仓库 / 隔离区的改动不会被主项目
    这边的审阅顺带清掉。枚举失败、或有任何一个文件没被覆盖，都不记。
    """
    from . import session as _session
    try:
        v = _session.get_verification()
        roots = v.get("diff_roots") or []
        if not roots:
            return
        candidates = [cwd]
        if path:
            resolved = _resolve_path(path)
            if os.path.isdir(resolved) and os.path.realpath(resolved) != os.path.realpath(cwd):
                candidates.insert(0, resolved)
        cache = {}
        for directory in candidates:
            scope = os.path.relpath(directory, cwd).replace(os.sep, "/") if directory != cwd else ""
            files = _changed_in_dir(cwd, scope)
            if files is None:
                continue
            targets = [os.path.join(cwd, rel) for rel in files]
            if not all(any(_v_covers(r, t, cache=cache) for r in roots) for t in targets):
                continue
            # 目录本身也得和这些文件在同一个工作区里，否则"覆盖"是假的
            if all(_v_covers(directory, t, cache=cache) for t in targets):
                _mark_reviewed(directory)
    except Exception:
        pass


@tool
def git_diff(path: str = "", staged: bool = False, max_chars: int = 8000) -> str:
    """查看 git 改动（默认未暂存的工作区改动）。path: 限定文件/目录（相对项目根，空=全部）。
    staged=True 看已暂存（git add 过）的改动。只读、调研用。
    不指到单个文件时一次最多交回 8000 字（max_chars 调大也一样），放不下的文件会列出名字和改动量；
    要看全某个文件的改动，path 指到那个文件。"""
    try:
        import shutil as _shutil
        max_chars, single = _diff_char_limit(path, max_chars)
        if not _shutil.which("git"):
            return _review_without_git(path, max_chars, "没有找到 git，")

        cwd = _project_cwd()
        # --relative：路径相对**项目根**而不是仓库根。项目根是仓库子目录时（monorepo 里
        # 只打开 backend/），默认输出给的是 backend/src/a.py，按项目根拼就成了
        # backend/backend/src/a.py——审阅记录指向不存在的路径，按文件记和目录反判全失效。
        # 顺带把范围也收到项目内，与 _untracked_as_diff（ls-files 本来就相对 cwd）一致。
        cmd = ["git", "diff", "--relative"]

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
            stdin=subprocess.DEVNULL,
        )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if "not a git repository" in stderr.lower():
                return _review_without_git(path, max_chars, "当前项目不是 git 仓库，")
            return f"git diff 执行出错: {stderr or '未知错误'}"

        tracked = result.stdout or ""
        # 未跟踪的新文件 `git diff` 一个字都看不见：把范围内的新文件按"整文件新增"附在后面。
        # 以前只能靠 git_status 看见文件名，带 path 查一个新文件还会回"工作区干净"——
        # 新建了文件的任务按文件逐个审阅时，这份义务永远解不开（C1 为此调了 16 次 git_diff）。
        new_text, new_shown, new_all = ("", [], []) if staged else _untracked_as_diff(cwd, path, max_chars)
        output = tracked + (("\n" if tracked and not tracked.endswith("\n") else "") + new_text if new_text else "")
        empty = not output.strip()

        # 标记 diff 已审查（验证状态）。判据是"这次有没有把该看的范围**完整**展示"，
        # 与 _review_without_git 同一原则：看不全就不放行，并点名去哪儿单独看。
        #   - 被长度截断（truncated）：整个范围不算看过，但**整段展示出来的那几个文件按文件记**
        #     （record_diff_files，不动 diff_reviewed）。早先在截断之前就按整个范围记，
        #     被截掉的文件也被当成审阅过；后来改成一律不记，又让模型把已经看全的文件再看一遍。
        #   - 带 path 限定且为空：只看了一个没改动的文件，说明不了别处 → 不记。
        #   - staged=True 且为空：暂存区空不代表工作区干净 → 不记。非空但范围内还有未暂存 /
        #     未跟踪改动的，整个范围也不记（只按文件记）——暂存区的 diff 看不见它们。
        #   - 部分暂存的文件（两侧各有改动）：任一视图都只展示一半，按"文件 + 侧"记，
        #     两侧都看过才算这个文件看完（见 _record_diff_sides）。
        #   - 带 path 的未暂存视图：范围里的暂存改动一个字都看不见，**再核对一次**，有完全
        #     没出现的就不按整个范围记（与默认视图的 _uncovered_changes 对称）。
        #   - 全工作区且非 staged：默认 diff 看不见已暂存改动和未跟踪新文件，**再核对一次**，
        #     还有没覆盖到的就不记（见 _uncovered_changes）。
        #   - 范围内改动都完整展示了（含"全工作区确实干净"）→ 按整个范围记。
        #   - 范围没看全，但范围里的改动已经被逐个看全了 → 也按范围记（盲区目录义务靠它了结，
        #     见 _mark_range_if_fully_reviewed）。
        # 执行失败（git 缺失 / 非仓库 / 非零退出 / 超时 / 异常）一律走不到这里，不放行。
        uncovered_note = ""
        covered = _resolve_path(path) if path else cwd
        new_left_out = [f for f in new_all if f not in new_shown]
        clipped, shown_rel = _clip_diff(output, max_chars, single)
        # 新文件那几段可能只是"读取失败 / 太长没展示"的占位说明，段在输出里不等于内容看全了
        shown_rel = [rel for rel in shown_rel if rel not in new_left_out]
        truncated = len(output) > max_chars
        if not path and not staged:
            # 默认 `git diff` **只看已跟踪文件的未暂存改动**——已 `git add` 的修改和
            # 未跟踪的新文件它一个都看不见。所以"输出为空"不等于"整个项目干净"：
            # 实测过 app.py 改完 add 了、以及新增一个未跟踪的 new_module.py，
            # 两种情况下这条命令都回"工作区干净"，义务被清空、任务按 completed 收尾，
            # 而那些改动从头到尾没在任何 diff 里露过面。
            # 所以解除**全局**义务之前，再核对暂存区与未跟踪文件；有没覆盖到的就不解除。
            staged_files, untracked, probe_error = _uncovered_changes(cwd)
            if probe_error:
                # 核对失败 = 不知道干不干净。不知道就不放行（与追踪枚举失败同一取舍）。
                # 改动非空时也会走到这儿，所以一样要截断（否则 8000 字上限被这条返回绕开），
                # 措辞按 diff 空不空分开：上面列着改动还说"没有未暂存的改动"是自相矛盾。
                head = ("工作区没有未暂存的改动，但无法核对暂存区和未跟踪文件" if empty
                        else "上面的改动之外，无法核对暂存区和未跟踪文件")
                return ((clipped + "\n\n" if not empty else "")
                        + head + f"（{probe_error}），因此不能确认整个项目干净。"
                        "请用 git_status 手动确认后再判断是否完成。")
            # 上面已经整份给出的新文件算看过了；没给出的（太多被截掉的）照旧算没覆盖
            untracked = [f for f in untracked if f not in new_shown]
            if staged_files or untracked:
                uncovered_note = _describe_uncovered(staged_files, untracked, empty)
            all_shown = not truncated and not staged_files and not untracked
            other = set(staged_files)   # 暂存区上面已经枚举过了，不必再跑一次 git
        else:
            # 限定 path / staged：范围内有内容、且没被截断、新文件也都附上了，才算看全。
            all_shown = not empty and not truncated and not new_left_out
            other = _other_side_changes(cwd, path, staged)
            if staged:
                if all_shown:
                    # 暂存区的 diff 看不见未暂存的改动和未跟踪的新文件。只看暂存区就把整个范围
                    # 记成已审阅，等于替那些没看过的改动背书——与默认 diff 看不见暂存区是对称的坑。
                    all_shown = not _unstaged_leftovers(cwd, path)
            else:
                # 对称的另一半：带路径的未暂存视图看不见范围里的暂存改动。`git_diff(path=".")`
                # 原来绕过了只有默认视图才有的核对，把只有暂存改动、这次一个字都没出现的文件
                # 连同整个范围一起记成已审阅，目录反判再顺带把项目根也记上。
                # 只列**完全没出现**的那些：部分暂存、已展示了一半的由 _record_diff_sides 管。
                left_out = sorted(other - set(shown_rel)) if other is not None else []
                if left_out or other is None:
                    all_shown = False
                if left_out:
                    uncovered_note = _describe_uncovered(left_out, [], empty, path)
        # 部分暂存的文件：两侧各有改动，这次只展示了一半。按整个文件记的话，下面的目录反判
        # 会认为改动全覆盖了，把没露过面的那一半连同整个项目根一起放行。所以先按"文件 + 侧"
        # 记，两侧齐了才算这个文件看完（只有一侧有改动的照旧看一次就算数）。
        half_shown = _record_diff_sides(cwd, shown_rel, staged, other)
        if half_shown:
            all_shown = False
        if all_shown:
            _mark_reviewed(covered)
        else:
            _record_diff_files(cwd, [rel for rel in shown_rel if rel not in half_shown])
        # 全工作区已经整体记过了就不用再核（候选只有它自己）；其余情形核一次目录是否已逐个看全
        if not empty and not (all_shown and not path):
            _mark_range_if_fully_reviewed(cwd, path)

        if empty:
            if staged:
                return "暂存区没有改动。"
            if uncovered_note:
                return uncovered_note
            return "工作区干净，没有未提交改动。"
        # 先截 diff 再附"还有没覆盖到的改动"：这句是完成闸门要模型看到的，不能被截掉
        output = clipped
        if uncovered_note:
            output = output.rstrip("\n") + "\n\n" + uncovered_note
        if half_shown and (path or staged):
            # 默认全局视图的 uncovered_note 已经点名了这些文件、也说了去哪儿看，不再重复一遍
            output = output.rstrip("\n") + "\n\n" + _describe_half_shown(half_shown, staged, path)
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
            stdin=subprocess.DEVNULL,
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
            stdin=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except Exception:
        return False


def _git_available() -> bool:
    """git 是否可用。"""
    import shutil as _shutil
    return _shutil.which("git") is not None


# ── 给界面用的只读查询（任务面板"改动的文件"）──
# 不走 git_diff 工具：那个工具会把 diff 记成"已审阅"，那是模型自己的验证义务，
# 用户在界面上点开看一眼不能替模型解除它。这里只读、不碰验证状态。

def _norm_rel(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _git_text(cwd: str, args: list[str], timeout: float = 5) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None


def diff_numstat(cwd: str, rel_paths: list[str]) -> dict | None:
    """这些文件相对上次提交的增删行数：{相对路径: (增, 删, 是否新文件)}。

    工作区对比 HEAD（暂存过的也算进去）；二进制文件的行数是 None；git 还没跟踪的新文件
    按现有行数算"增"。不是 git 仓库或 git 不可用时返回 None——说不出就不说。"""
    if not rel_paths or not _git_available() or not cwd or not _is_inside_git_repo(cwd):
        return None
    wanted = {_norm_rel(p): p for p in rel_paths}
    base = ["diff", "--numstat", "--relative", "HEAD", "--"]
    out = _git_text(cwd, base + list(rel_paths))
    if out is None:     # 还没有任何提交（没有 HEAD）：退回工作区对比暂存区
        out = _git_text(cwd, ["diff", "--numstat", "--relative", "--"] + list(rel_paths)) or ""
    result = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        key = wanted.get(_norm_rel(path))
        if key is not None:
            result[key] = (int(added) if added.isdigit() else None,
                           int(removed) if removed.isdigit() else None, False)
    untracked = _git_text(cwd, ["ls-files", "--others", "--exclude-standard", "--"] + list(rel_paths)) or ""
    for path in untracked.splitlines():
        key = wanted.get(_norm_rel(path))
        if key is None:
            continue
        try:
            with open(os.path.join(cwd, path), "r", encoding="utf-8", errors="replace") as f:
                lines = sum(1 for _ in f)
        except OSError:
            lines = None
        result[key] = (lines, 0 if lines is not None else None, True)
    return result


# 新文件（git 还没跟踪）没有旧版本可对比：先给一行说明，后面接当前内容
_NEW_FILE_NOTE = "新文件（git 还没跟踪），没有旧版本可对比。当前内容："
# 和上次提交没有差异：说明一行，后面照样给出当前内容（点回复里的文件多半是这种情况，
# 只写一句"没有差异"等于什么都没看到）
_NO_DIFF_NOTE = "这个文件和上次提交相比没有差异（可能改了又改回去，或改动已经提交）。当前内容："
_NO_DIFF_PLAIN = "这个文件和上次提交相比没有差异（可能改了又改回去，或改动已经提交）。"
_EMPTY_FILE_NOTE = _NO_DIFF_PLAIN + "文件是空的。"
_BINARY_FILE_NOTE = _NO_DIFF_PLAIN + "这是二进制文件，不显示内容。"
# 没有差异时给全文：差异的 20000 字上限对整个文件太小（chat_window.py 只看得到前几百行，
# 回复里带行号的链接就滚不到），全文单独放宽；再长就截断并写明
_FULL_TEXT_MAX_CHARS = 400_000
_TRUNCATED_PREFIX = "... [文件太长，只显示前 "


def _read_text(cwd: str, rel_path: str, max_chars: int) -> str:
    """读工作区里这个文件（按行截断），读不了抛 OSError。"""
    with open(os.path.join(cwd, rel_path), "r", encoding="utf-8", errors="replace") as f:
        return f.read(max_chars)


def _file_diff_raw(cwd: str, rel_path: str, max_chars: int = 20000) -> str:
    """一个文件此刻相对上次提交的差异（只读，不记"已审阅"）。新文件 / 没有差异给出当前内容。"""
    if not _git_available():
        return "git 未安装或不在 PATH 中，无法查看差异。"
    if not cwd or not _is_inside_git_repo(cwd):
        return "当前项目不是 git 仓库，没有可对比的旧版本。"
    text = _git_text(cwd, ["diff", "--relative", "HEAD", "--", rel_path], timeout=10)
    if text is None:
        text = _git_text(cwd, ["diff", "--relative", "--", rel_path], timeout=10) or ""
    if not text.strip():
        untracked = _git_text(cwd, ["ls-files", "--others", "--exclude-standard", "--", rel_path]) or ""
        if untracked.strip():
            try:
                content = _read_text(cwd, rel_path, max_chars)
            except OSError as e:
                return f"新文件（git 还没跟踪），读取失败：{e}"
            return _NEW_FILE_NOTE + "\n\n" + content
        # 没有差异：点回复里的文件多半是这种情况，给出当前全文而不是只说一句"没有差异"
        cap = max(max_chars, _FULL_TEXT_MAX_CHARS)
        try:
            content = _read_text(cwd, rel_path, cap + 1)
        except OSError as e:
            return f"{_NO_DIFF_PLAIN}读取当前内容失败：{e}"
        if "\x00" in content:
            return _BINARY_FILE_NOTE      # 别把乱码当正文
        if not content.strip():
            return _EMPTY_FILE_NOTE
        if len(content) > cap:
            content = content[:cap] + f"\n{_TRUNCATED_PREFIX}{cap} 字符]"
        return _NO_DIFF_NOTE + "\n\n" + content
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [差异太长，只显示前 {max_chars} 字符]"
    return text


def file_diff_text(cwd: str, rel_path: str, max_chars: int = 20000) -> str:
    """一个文件此刻相对上次提交的差异（纯文本，给纯文本展示用）。"""
    return _file_diff_raw(cwd, rel_path, max_chars)


# 差异里每一行的种类，界面按它上色（新增绿 / 删除红）：
#   "add" / "del"：新增 / 删除的正文行；"hunk"：@@ 段落头；"meta"：diff/index/---/+++ 头
#   "ctx"：上下文行；"note"：不是 diff 的说明文字（新文件、没差异、git 不可用等）
_HUNK_PREFIXES = ("diff ", "index ", "--- ", "+++ ", "new file", "deleted file ",
                  "old mode", "new mode", "similarity index", "rename ", "copy ",
                  "\\ No newline")


def _classify_diff_line(line: str) -> str:
    if line.startswith("@@"):
        return "hunk"
    if line.startswith("+") and not line.startswith("+++"):
        return "add"
    if line.startswith("-") and not line.startswith("---"):
        return "del"
    if (line[:1] in ("+", "-")) or line.startswith(_HUNK_PREFIXES):
        return "meta"
    return "ctx"


def _hunk_numbers(line: str):
    """从 `@@ -a,b +c,d @@` 里取出新文件一侧的起始行号；取不到返回 None。"""
    try:
        part = line.split("+", 1)[1].split("@@", 1)[0].strip()
        return int(part.split(",")[0])
    except (IndexError, ValueError):
        return None


def file_diff_lines(cwd: str, rel_path: str, max_chars: int = 20000) -> list[tuple]:
    """file_diff_text 的逐行版本：[(行号, 种类, 文本)]。

    行号是新文件一侧的行号（删除行没有新行号，取 None），界面把它按显示行号处理；
    不是 unified diff 的说明文字整段按 "note" 给出，行号 None。新文件没有 diff
    （没有旧版本），说明那行按 "note"、当前内容按 "add" 逐行编号，界面同样能按绿
    色显示——否则点一个刚建的文件只会看到一段灰字。

    和上次提交**没有差异**的文件（点回复里的文件多半是这种情况）同样先给一行说明、
    再接当前内容的逐行编号：按普通上下文行（"ctx"）显示，不是新增绿也不是删除红。
    """
    text = _file_diff_raw(cwd, rel_path, max_chars)
    if text.startswith(_NEW_FILE_NOTE):
        rows = [(None, "note", _NEW_FILE_NOTE)]
        body = text[len(_NEW_FILE_NOTE):].lstrip("\n").splitlines()
        for no, line in enumerate(body, 1):
            rows.append((no, "add", line))
        return rows
    if text.startswith(_NO_DIFF_NOTE):
        rows = [(None, "note", _NO_DIFF_NOTE)]
        body = text[len(_NO_DIFF_NOTE):].lstrip("\n").splitlines()
        for no, line in enumerate(body, 1):
            rows.append((None, "note", line) if line.startswith(_TRUNCATED_PREFIX) else (no, "ctx", line))
        return rows
    if not text.startswith("diff ") and "--- " not in text.splitlines()[:3]:
        return [(None, "note", ln) for ln in text.splitlines()]
    rows = []
    new_no = None
    for ln in text.splitlines():
        if ln.startswith("... [差异太长"):
            rows.append((None, "note", ln))
            continue
        kind = _classify_diff_line(ln)
        if kind == "hunk":
            new_no = _hunk_numbers(ln)
            rows.append((None, "hunk", ln))
            continue
        if kind == "add":
            rows.append((new_no, "add", ln))
            new_no = None if new_no is None else new_no + 1
            continue
        if kind == "del":
            rows.append((None, "del", ln))
            continue
        if kind == "meta":
            rows.append((None, "meta", ln))
            continue
        rows.append((new_no, "ctx", ln))
        new_no = None if new_no is None else new_no + 1
    return rows


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
                    stdin=subprocess.DEVNULL,
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
            stdin=subprocess.DEVNULL,
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
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            return f"git add 执行出错: {stderr or '未知错误'}"

        # 获取暂存区摘要
        cached = subprocess.run(
            ["git", "diff", "--cached", "--name-status"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
            stdin=subprocess.DEVNULL,
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
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            # 降级
            cmd = ["git", "reset", "--"] + rel_paths
            result = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                return f"git unstage 执行出错: {stderr or '未知错误'}"

        # 获取暂存区摘要
        cached = subprocess.run(
            ["git", "diff", "--cached", "--name-status"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
            stdin=subprocess.DEVNULL,
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
            stdin=subprocess.DEVNULL,
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
            stdin=subprocess.DEVNULL,
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
            stdin=subprocess.DEVNULL,
        )
        staged_files = [f for f in (staged_names.stdout or "").strip().splitlines() if f.strip()]

        # 执行提交
        cmd = ["git", "commit", "-m", message.strip()]
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
            stdin=subprocess.DEVNULL,
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
            stdin=subprocess.DEVNULL,
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
