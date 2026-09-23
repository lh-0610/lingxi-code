"""恢复检查与继续入口（B04）。

两条入口，一套处置：

- **点「继续任务」**（mode=continue）：主线程先 `precheck`（归属 / 项目目录 / 模型 / 模式，
  只读、不起子进程）→ 新 worker 里 `inspect_site`（结果未知的操作、现场锚点比对、仓库身份）
  → 有阻断就停、如实告诉用户 → 没有就 `apply_resume` → agent_loop 正常开始新的一轮。
- **中断后直接发新消息**（mode=message）：用户没点继续，但上一轮确实被中断过、或留下了结果未知的
  操作。这时不阻断用户的新请求，也不做现场比对，只做必须做的三件事：补齐悬空的 tool 调用、
  把结果未知的写操作并入待验证义务、告诉模型哪些结果未知。**不做这一步的后果**：中断前那次
  写操作从未进入 dirty 文件，新一轮的工作区基线又是在它之后才建立的，完成闸门会拿"没有已知改动"
  推出"工作区没变"，一次没验证过的写入就按 completed 收尾（方案 §5.5 明令禁止的推断）。

两者都排在 `begin_run` **之前**：义务写进 `pending_verification`，由 begin_run 在
`reset_verification` 之后填回。排在后面就被这一轮的重置清掉了——B03 反复强调过的顺序陷阱。

几条不能松的边界：

- **恢复检查只读**，点了"继续"才启动模型；检查本身不改任何项目文件。
- **结果未知的操作绝不自动重放**——它可能已经执行了。恢复说明里明确列出来，
  由模型核对现场后自己决定。
- **补齐悬空 tool 调用时不冒充结果**：内容写"应用恢复提示：未取得执行结果"，
  并区分"已调度、结果未知"与"没有执行记录"两种情形。
- **Plan 会话恢复后仍是 Plan**，不暗中切 Act。
- **原模型不在了就要求重新选**，不能静默落到另一种执行后端。
- **不恢复**旧的确认许可、后台进程控制权、隔离区使用权——那些都不持久化，也不该持久化。
"""
import os
from dataclasses import dataclass, field

from .paths import logger


# 这些工具结果未知时**不会**改动项目文件（改的是长期记忆 / 计划 / 通知）。
# 除此之外的一律按"可能动过文件"处理：命令、写文件、补丁、MCP、子 Agent、测试（测试代码
# 本身可以写文件）、自定义检查命令都在其列。清单只列"确定不碰文件"的，宁可多疑。
_NON_FS_TOOLS = frozenset({"remember", "forget", "notify_user",
                           "update_plan", "set_step_status"})

# 停止原因 → 给人看的一句话（方案 §5.6）
_STOP_LABELS = {
    "cancelled": "用户停止",
    "failed": "本轮执行失败",
    "limit_reached": "达到轮数上限",
    "unverified": "仍有未验证事项",
    "completed": "已正常完成",
}

# 用户点「继续任务」写入的恢复说明。来源判定据此把这一轮标成 continue。
RESUME_KIND = "resume"
# 中断后用户直接发新消息时补的恢复提示。它**不是**继续：本轮来源仍是用户那条新消息。
RECOVERY_KIND = "recovery"
RECOVERY_KINDS = (RESUME_KIND, RECOVERY_KIND)

# "工作目录无法确定"时盲区用的占位键。它不是路径：restore_obligations 不会为它种基线
# （种了也枚举不了，只会让闸门永远过不去），但盲区照样打开"必须 run_tests + git_diff"的要求。
# 目录不知道 ≠ 没有改动——早先正是拿"没有根目录"当理由把结果未知的写入整个丢掉的。
UNKNOWN_ROOT = "（工作目录无法确定）"

# 恢复说明上附带的操作记录条数上限（诊断用，不是日志）
_MAX_RECORDED_OPS = 20
# 恢复说明里逐条列出的现场变化上限。锚点最多记 200 个文件，全列出来会把说明撑得很长；
# 没列出的照样并入待验证义务，不会因为没写进说明就被放过。
_MAX_LISTED_CHANGES = 30


def stop_label(last_run):
    if not isinstance(last_run, dict):
        return "没有运行记录"
    if last_run.get("phase") == "running":
        return "运行被中断"
    return _STOP_LABELS.get(last_run.get("outcome"), "结果未知")


def plan_progress(plan):
    """(已完成步数, 总步数, 下一步文字)。**这是模型自己报的进度**，不是验收结论。"""
    from .run_records import _plan_summary     # 与结果卡同一套算法，别各算各的
    summary = _plan_summary(plan)
    return summary["done"], summary["total"], summary["next"]


# ══════════════════════════════════════════════════════════════
# 主线程预检（便宜、只读）
# ══════════════════════════════════════════════════════════════

@dataclass
class Precheck:
    blocking: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    model_index: int | None = None     # 该用哪个模型（None = 沿用当前）
    model_missing: bool = False         # 原模型已不存在（UI 据此提供"改用当前模型"）
    model_label: str = ""
    project_missing: str = ""           # 不存在的项目目录（UI 据此提供"重新选择"）


def find_model(record):
    """按**稳定标识**找回上一轮的模型，找不到返回 None。

    类型必须一致：同一个 model_id 换了类型就是另一种执行后端（比如 API ↔ 本地 CLI、
    或者同名模型挂在另一个协议下），那正是方案禁止的"静默落到另一种后端"。
    名字只用来在多个同 id 同类型的配置里挑一个更贴切的（自定义模型可能同 id 不同端点）。
    """
    from .models import MODEL_LIST
    if not isinstance(record, dict) or not record.get("id"):
        return None
    candidates = [i for i, m in enumerate(MODEL_LIST) if m[2] == record["id"]]
    if record.get("type"):
        candidates = [i for i in candidates if MODEL_LIST[i][1] == record["type"]]
    if not candidates:
        return None
    named = [i for i in candidates if MODEL_LIST[i][0] == record.get("name")]
    return (named or candidates)[0]


def precheck(sess, *, accept_current_model=False):
    """点「继续」后立刻在主线程做的检查。只读，不起子进程。"""
    from .models import MODEL_LIST, get_model_config_issues
    from . import session as _session_mod

    out = Precheck()
    run = getattr(sess, "last_run", None) or {}

    # ── 项目目录 ──
    project = getattr(sess, "project", _session_mod._UNSET)
    if (getattr(sess, "session_kind", "code") == "code"
            and isinstance(project, str) and project and not os.path.isdir(project)):
        out.project_missing = project
        out.blocking.append(f"项目目录已不存在：{project}")
    work_dir = run.get("work_dir") or ""
    if (work_dir and isinstance(project, str) and project
            and not _same_path(work_dir, project)):
        if os.path.isdir(work_dir):
            # 隔离区的使用权不跨进程恢复：继续会在主项目里进行，隔离区不会被自动合并。
            out.notes.append(f"上一轮在 {work_dir} 里运行；继续将在 {project} 中进行，"
                             "那里的改动不会被自动合并过来。")
        else:
            out.notes.append(f"上一轮的工作目录 {work_dir} 已不存在；继续将在 {project} 中进行，"
                             "现场按新位置重新核对。")

    # ── 模型：用稳定标识找，不用下标 ──
    record = run.get("model")
    current = int(getattr(sess, "current_model_index", 0) or 0)
    index = current
    # 用户在顶栏亲手换过模型 = 已经做了选择。重开的会话模型下标是继承来的、谁也没选过，
    # 那种才按记录找回；特意换过的不能又悄悄改回去。
    chosen = bool(getattr(sess, "model_user_choice", False))
    current_name = MODEL_LIST[current][0] if 0 <= current < len(MODEL_LIST) else "当前模型"
    if record:
        found = find_model(record)
        label = record.get("name") or record.get("id")
        if found is None:
            out.model_missing = True
            if accept_current_model or chosen:
                out.notes.append(f"上一轮使用的模型「{label}」已不可用，按你的选择改用「{current_name}」。")
            else:
                out.blocking.append(f"上一轮使用的模型「{label}」已不可用，请选择要使用的模型。")
        elif found != current:
            if chosen:
                out.notes.append(f"按你在顶栏的选择使用「{current_name}」（上一轮用的是「{label}」）。")
            else:
                index = found
                out.notes.append(f"已恢复上一轮使用的模型「{MODEL_LIST[found][0]}」。")
    else:
        out.notes.append("上一轮没有记录所用模型，将使用当前选择的模型。")
    if 0 <= index < len(MODEL_LIST):
        out.model_index = index
        out.model_label = MODEL_LIST[index][0]
        # 原模型缺失且用户还没确认换用时，不去报"当前模型缺 key"——那会让人以为要修的是它。
        if not (out.model_missing and not (accept_current_model or chosen)):
            out.blocking.extend(get_model_config_issues(index))
    else:
        out.blocking.append("当前模型选择无效，请重新选择模型。")

    # ── 模式：照原样，不改 ──
    if getattr(sess, "agent_mode", "act") == "plan":
        out.notes.append("会话处于 Plan 模式：继续后仍只做调研和方案，不会动手修改。")
    return out


# ══════════════════════════════════════════════════════════════
# worker 里的现场检查（只读，有 IO）
# ══════════════════════════════════════════════════════════════

@dataclass
class SiteCheck:
    blocking: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    unknown_ops: list = field(default_factory=list)   # sidecar 里没有回执的操作
    dangling: list = field(default_factory=list)      # 历史里没有结果的 tool 调用
    site: dict = field(default_factory=dict)          # workspace_anchor.compare 的报告
    compared: bool = False                            # 是否真的做了现场比对
    at_stake: bool = False                            # 上一轮有没有需要核对的改动
    root: str = ""                                    # 继续将要落在的目录
    inflight_unreadable: str = ""                     # sidecar 读不懂的原因
    unreadable_entries: list = field(default_factory=list)  # sidecar 里解析不了的条目（截断预览）
    unreadable_raw: list = field(default_factory=list)      # 同一批条目的完整原文（留底用）
    unreadable_archive: str = ""                      # 完整原文的隔离文件名（留底成功才有）


def _dangling_calls(history):
    """历史里「AIMessage 发起了、但没有 ToolMessage 应答」的调用，按出现顺序。"""
    from langchain_core.messages import AIMessage, ToolMessage
    answered = {getattr(m, "tool_call_id", None) for m in history
                if isinstance(m, ToolMessage)}
    out = []
    for index, msg in enumerate(history):
        if not isinstance(msg, AIMessage):
            continue
        for call in (getattr(msg, "tool_calls", None) or []):
            call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
            if call_id and call_id not in answered:
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
                out.append({"id": call_id, "name": name or "", "ai_index": index})
    return out


def _unknown_operations(sess):
    """sidecar 里仍没有完成回执的操作，以及解析不了的条目。

    返回 (未知操作, 畸形条目 [{"preview", "raw"}], 读取错误)。回执用**内存里的**：
    本进程里它不比磁盘旧。
    """
    from . import run_records
    session_id = getattr(sess, "current_session_id", None)
    if not session_id:
        return [], [], ""
    entries, error = run_records.classify_inflight(
        session_id,
        last_receipt=getattr(sess, "last_committed_operation", None),
        recent_receipts=list(getattr(sess, "recent_operations", None) or []))
    unknown = [e["operation"] for e in entries if e["status"] == "unknown"]
    unreadable = [{"preview": e["operation"].get("raw", ""), "raw": e.get("raw")}
                  for e in entries if e["status"] == "unreadable"]
    return unknown, unreadable, error


def inspect_site(sess, *, compare_site=True):
    """只读核对：结果未知的操作、悬空调用、现场与锚点的差别、仓库是否还是那个仓库。"""
    from . import run_records, workspace_anchor
    check = SiteCheck()
    run = getattr(sess, "last_run", None) or {}

    check.unknown_ops, unreadable, error = _unknown_operations(sess)
    check.unreadable_entries = [u["preview"] for u in unreadable]
    check.unreadable_raw = [u["raw"] for u in unreadable]
    if error:
        check.inflight_unreadable = error
        check.warnings.append(f"执行前记录无法读取（{error}），无法确认中断时有没有结果未知的操作")
    elif check.unreadable_entries:
        # 认不出是哪个操作——和整个文件读不懂同样对待：可能有写入，要求补验证。
        check.inflight_unreadable = f"执行前记录中有 {len(check.unreadable_entries)} 条无法解析"
        check.warnings.append(f"{check.inflight_unreadable}，不知道是哪些操作，按结果未知处理")

    check.dangling = _dangling_calls(list(getattr(sess, "chat_history", None) or []))

    pending = getattr(sess, "pending_verification", None) or {}
    evidence = run.get("evidence") or {}
    check.at_stake = bool(pending.get("files") or pending.get("tracking_incomplete")
                          or evidence.get("changed_files") or check.unknown_ops
                          or check.inflight_unreadable)

    # 继续将落在**现在的**工作目录上（重开后 worktree 早没了，就是项目根），
    # 锚点里记的是当时的目录——两者不同就在这个目录上按相对路径比对，仓库身份另由根提交把关。
    check.root = run_records._work_dir_of(sess) or run.get("work_dir") or ""
    if compare_site and check.root:
        check.site = workspace_anchor.compare(run.get("workspace"), check.root)
        check.compared = True
        check.blocking.extend(check.site.get("blocking") or [])
        recorded = (run.get("workspace") or {}).get("root") or run.get("work_dir") or ""
        if recorded and os.path.normcase(os.path.normpath(recorded)) \
                != os.path.normcase(os.path.normpath(check.root)):
            check.site.setdefault("incomplete", []).append(
                f"上次记录的目录是 {recorded}，现在在 {check.root} 继续")
    return check


# ══════════════════════════════════════════════════════════════
# 应用：补齐悬空调用 / 并入义务 / 写恢复说明 / 交接 inflight
# ══════════════════════════════════════════════════════════════

def _placeholder(call, unknown_by_call, inflight_unreadable=""):
    from . import run_records
    op = unknown_by_call.get(call["id"])
    tool = call.get("name") or (op or {}).get("tool") or "工具"
    if op is not None or (inflight_unreadable and run_records.needs_record(tool)):
        return (f"应用恢复提示：未取得执行结果。这次 `{tool}` 调用已调度，但程序在拿到结果之前"
                "中断了——它可能已经执行，也可能没有。请先核对现场，不要直接重复这个操作。")
    return (f"应用恢复提示：未取得执行结果。程序没有这次 `{tool}` 调用的执行记录，"
            "按未执行处理；如仍需要，请先核对现场再重新发起。")


def _insert_placeholders(history, dangling, unknown_by_call, *, kind, inflight_unreadable=""):
    """把占位 ToolMessage 插在对应 AIMessage 的结果区末尾（协议要求结果紧跟调用）。"""
    from langchain_core.messages import ToolMessage
    by_ai = {}
    for call in dangling:
        by_ai.setdefault(call["ai_index"], []).append(call)
    # 从后往前插，前面的下标不受影响
    for ai_index in sorted(by_ai, reverse=True):
        insert_at = ai_index + 1
        while insert_at < len(history) and isinstance(history[insert_at], ToolMessage):
            insert_at += 1
        for offset, call in enumerate(by_ai[ai_index]):
            history.insert(insert_at + offset, ToolMessage(
                content=_placeholder(call, unknown_by_call, inflight_unreadable),
                tool_call_id=call["id"],
                additional_kwargs={"lingxi_internal": True, "lingxi_kind": kind}))


def _rel(root, path):
    """与 tools_common._norm_vpath 同一套规则：项目内给相对正斜杠路径，项目外给规范绝对路径。

    不直接调它是因为它依赖 `_project_cwd()`，而后者在目录不存在时会回退到进程 cwd——
    恢复要处理的恰恰可能是"项目目录没了"的情形。
    """
    if not path:
        return ""
    full = path if os.path.isabs(path) or not root else os.path.join(root, path)
    if root:
        try:
            real_full = os.path.realpath(full)
            real_root = os.path.realpath(root)
            if (os.path.normcase(os.path.commonpath([real_full, real_root]))
                    == os.path.normcase(real_root)):
                return os.path.relpath(real_full, real_root).replace("\\", "/")
        except ValueError:
            pass
    return os.path.normpath(full).replace("\\", "/")


def _same_path(a, b):
    return bool(a) and bool(b) and \
        os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _under(path, root):
    """path 是不是 root 本身或其下的路径（按字符串规范化比，不要求路径还存在）。"""
    if not path or not root:
        return False
    try:
        p = os.path.normcase(os.path.normpath(path))
        r = os.path.normcase(os.path.normpath(root))
        return os.path.commonpath([p, r]) == r
    except ValueError:
        return False


def _map_moved(path, old_root, new_root):
    """old_root 下的路径换算到 new_root 下；不在 old_root 下的原样返回。"""
    if not _under(path, old_root) or not new_root:
        return path
    suffix = os.path.relpath(os.path.normpath(path), os.path.normpath(old_root))
    return os.path.normpath(new_root) if suffix in (".", "") else \
        os.path.normpath(os.path.join(new_root, suffix))


def _add_reason(tracking, root, reason):
    existing = tracking.get(root)
    if not existing:
        tracking[root] = reason
    elif reason not in existing:
        tracking[root] = f"{existing}；{reason}"


def _fold_obligations(sess, check):
    """把现场变化和结果未知的操作并入待验证义务，交给 begin_run 在重置之后填回。

    走 pending_verification 而不是直接改 verification：新一轮的 begin_run 会先
    reset_verification 再 restore_obligations，直接改的会被重置清掉。

    盲区挂在**工具当时实际落在的目录**上（执行前记录里的 `work_root`），不是挂在
    "这个会话的项目"上：无项目会话的工具落在进程工作目录，拿会话项目当根的话它是空的，
    早先就因为这个空根把结果未知的写入整个丢掉了。目录无法确定时挂在 `UNKNOWN_ROOT` 上——
    目录不知道不等于没有改动。
    """
    run = getattr(sess, "last_run", None) or {}
    current = os.path.realpath(check.root) if check.root else ""
    # 项目被搬走过：记录里的旧目录已不存在、现在在新位置继续。旧目录下的操作换算过去，
    # 否则盲区挂在一个不存在的目录上，而那里本来就没法再核对。
    recorded = (run.get("workspace") or {}).get("root") or run.get("work_dir") or ""
    moved_from = recorded if (recorded and current and not _same_path(recorded, current)
                              and not os.path.isdir(recorded)) else ""
    # 记录里的旧根可能是原始写法、执行前记录里是 realpath：两种写法都要认得出来。
    moved_candidates = [r for r in dict.fromkeys([moved_from, os.path.realpath(moved_from)
                                                   if moved_from else ""]) if r]

    def _follow_move(path):
        for old in moved_candidates:
            if _under(path, old):
                return _map_moved(path, old, current)
        return path

    pending = dict(getattr(sess, "pending_verification", None) or {})
    files = list(pending.get("files") or [])
    tracking = dict(pending.get("tracking_incomplete") or {})
    # 文件的**实际位置**也要记下：放行时要求检查覆盖到这里，相对路径本身说不出它在哪个目录
    located = {f: _follow_move(p) for f, p in dict(pending.get("file_paths") or {}).items()}

    def _add_file(rel, absolute):
        if rel and rel not in files:
            files.append(rel)
        if rel and absolute and os.path.isabs(absolute):
            located[rel] = os.path.normpath(absolute)

    site = check.site or {}
    for rel in list(site.get("changed") or []) + list(site.get("deleted") or []) \
            + list(site.get("appeared") or []):
        # 现场比对的路径是相对于现在这个目录的
        _add_file(rel, os.path.join(current, rel) if current and not os.path.isabs(rel) else rel)

    # 盲区原因：有了它，restore_obligations 会打开"必须 run_tests + git_diff"的要求。
    # 结果未知的写操作**一律**进盲区——它可能写了记录里没有的路径（run_command 尤其如此），
    # 光把已知路径标脏等于又一次拿"没有已知 dirty 文件"推断工作区没变。
    fs_unknown = [op for op in check.unknown_ops if op.get("tool") not in _NON_FS_TOOLS]
    for op in fs_unknown:
        op_root = op.get("work_root") or current        # 老记录没有 work_root：退回会话目录
        if op_root:
            op_root = _follow_move(op_root)
        for path in op.get("paths") or []:
            absolute = path if os.path.isabs(path) else \
                (os.path.join(op_root, path) if op_root else path)
            absolute = _follow_move(absolute)
            rel = _rel(current, absolute) if os.path.isabs(absolute) else absolute.replace("\\", "/")
            _add_file(rel, absolute)
        _add_reason(tracking, op_root or UNKNOWN_ROOT,
                    f"上次运行中断时有结果未知的操作（{op.get('tool') or '?'}），期间的写入无法确认")
    if check.inflight_unreadable:
        _add_reason(tracking, current or UNKNOWN_ROOT,
                    f"{check.inflight_unreadable}，无法确认中断时有没有写操作")
    if check.at_stake and current:
        # 上一轮没有待核对的东西时，HEAD / 分支变化只报告、不变成义务：
        # 已完成历史任务的旧成功结果只展示，不能把每次新问答都拖进测试要求。
        if site.get("head_change"):
            old, new = site["head_change"]
            _add_reason(tracking, current, f"Git HEAD 已变化（{(old or '?')[:10]} → "
                                           f"{(new or '?')[:10]}），工作区可能整体变了")
        if site.get("branch_change"):
            old, new = site["branch_change"]
            _add_reason(tracking, current, f"分支已变化（{old or '?'} → {new or '?'}）")
        if site.get("incomplete"):
            _add_reason(tracking, current, "现场未能完整核对：" + "；".join(site["incomplete"][:3]))

    pending["files"] = files
    pending["code_files"] = list(pending.get("code_files") or [])
    pending["tracking_incomplete"] = tracking
    pending["file_paths"] = {f: p for f, p in located.items() if f in files}
    pending.setdefault("reason", "")
    pending.setdefault("run_id", "")
    with sess.snapshot_lock:
        sess.pending_verification = pending


def relocate_obligations(sess, old_roots, new_root):
    """项目搬家后，把属于旧位置的**活动**追踪路径迁到新位置。返回迁移了几项。

    只迁"接下来还要用"的：待验证义务里的盲区根目录与绝对文件路径，以及内存验证状态里
    同样挂在旧目录上的键。不迁的后果实测过：搬家并继续、真跑测试通过、真看 diff 干净，
    完成闸门复查时却去枚举那个已不存在的旧目录，把测试和 diff 结论重新作废——
    用户按提示补查也收不了尾。

    **历史锚点不动**（`last_run.work_dir` / `last_run.workspace`）：它们记录的是"当时在哪儿"，
    留着原值才能诊断；恢复比对本来就按现在的目录做，用不着改写历史。
    """
    new_real = os.path.realpath(new_root) if new_root else ""
    olds = [r for r in dict.fromkeys(old_roots or []) if isinstance(r, str) and r]
    if not new_real or not olds:
        return 0
    moved = 0

    def _target(path):
        nonlocal moved
        for old in olds:
            if _under(path, old):
                moved += 1
                return _map_moved(path, old, new_real)
        return path

    def _file(path):
        # 旧目录下的绝对路径改成项目相对路径：新位置按相对路径就能找到同一个文件。
        nonlocal moved
        for old in olds:
            if os.path.isabs(path) and _under(path, old):
                moved += 1
                rel = os.path.relpath(os.path.normpath(path), os.path.normpath(old))
                return rel.replace("\\", "/")
        return path

    pending = dict(getattr(sess, "pending_verification", None) or {})
    tracking = {}
    for key, reason in (pending.get("tracking_incomplete") or {}).items():
        _add_reason(tracking, _target(key), reason)
    pending["tracking_incomplete"] = tracking
    for name in ("files", "code_files"):
        seen = []
        for path in pending.get(name) or []:
            mapped = _file(path)
            if mapped not in seen:
                seen.append(mapped)
        pending[name] = seen
    # 文件的实际位置：旧位置下的换算到新位置，键跟着上面相对化后的路径走
    pending["file_paths"] = {_file(f): _target(p)
                             for f, p in dict(pending.get("file_paths") or {}).items()}
    pending.setdefault("reason", "")
    pending.setdefault("run_id", "")

    verification = getattr(sess, "verification", None)
    with sess.snapshot_lock:
        sess.pending_verification = pending
        if isinstance(verification, dict):
            for name in ("unknown_changes", "tracking_errors"):
                source = verification.get(name)
                if isinstance(source, dict):
                    remapped = {}
                    for key, reason in source.items():
                        _add_reason(remapped, _target(key), reason)
                    verification[name] = remapped
            snapshots = verification.get("workspace_snapshots")
            if isinstance(snapshots, dict):
                # 旧目录的基线作废：新位置由下一轮的 restore 重新种、重新枚举
                for key in [k for k in snapshots if any(_under(k, old) for old in olds)]:
                    snapshots.pop(key, None)
                    moved += 1
            located = verification.get("dirty_abs")
            if isinstance(located, dict):
                verification["dirty_abs"] = {f: _target(p) for f, p in located.items()}
    return moved


def _compact_op(op):
    """交接出去的执行前记录的精简形态：随恢复说明落盘，诊断用。不含参数原文。"""
    paths = [p[:300] for p in (op.get("paths") or []) if isinstance(p, str)][:8]
    return {key: op.get(key) for key in ("operation_id", "run_id", "tool", "tool_call_id",
                                         "started_at", "base_revision")} | {"paths": paths}


def build_summary(sess, check, notes=(), *, mode="continue"):
    """给模型的恢复说明。是**程序生成的运行态**，不是用户的新要求。"""
    run = getattr(sess, "last_run", None) or {}
    done, total, nxt = plan_progress(getattr(sess, "current_plan", None))
    if mode == "continue":
        lines = ["[继续任务 · 程序生成的恢复说明，不是用户的新要求]"]
    else:
        lines = ["[恢复提示 · 程序生成的说明，不是用户的新要求；用户的新消息在这条之前]"]
    reason = (run.get("reason") or "")[:300]
    lines.append(f"上次运行：{stop_label(run)}" + (f"（{reason}）" if reason else ""))
    if total:
        lines.append(f"计划进度（这是你自己记录的，不是验收结论）：{done} / {total}"
                     + (f"；下一步：「{nxt}」" if nxt else ""))
    if check.unknown_ops:
        lines.append("结果未知的操作——可能已经执行，**不要直接重复**，先核对现场：")
        for op in check.unknown_ops[:_MAX_RECORDED_OPS]:
            target = "、".join(op.get("paths") or []) or "（无路径信息）"
            where = f"（工作目录 {op['work_root']}）" if op.get("work_root") else ""
            lines.append(f"- {op.get('tool')}：{target}{where}")
    if check.unreadable_entries:
        kept = (f"原文已完整留存在 chat_memory/{check.unreadable_archive}"
                if check.unreadable_archive else "原文暂时留在执行前记录里")
        lines.append(f"另有 {len(check.unreadable_entries)} 条执行前记录无法解析，不知道是哪些操作——"
                     f"同样按可能已经执行处理（{kept}）。")
    site = check.site or {}
    changes = []
    for rel in site.get("changed") or []:
        changes.append(f"- {rel}：内容与上次记录不同（可能被外部修改）")
    for rel in site.get("deleted") or []:
        changes.append(f"- {rel}：已被删除")
    for rel in site.get("appeared") or []:
        changes.append(f"- {rel}：上次记录时不存在，现在出现了")
    if site.get("head_change"):
        old, new = site["head_change"]
        changes.append(f"- Git HEAD：{(old or '?')[:10]} → {(new or '?')[:10]}")
    if site.get("branch_change"):
        old, new = site["branch_change"]
        changes.append(f"- 分支：{old or '?'} → {new or '?'}")
    if changes:
        lines.append("现场与上次记录相比：")
        lines.extend(changes[:_MAX_LISTED_CHANGES])
        if len(changes) > _MAX_LISTED_CHANGES:
            lines.append(f"- ……另有 {len(changes) - _MAX_LISTED_CHANGES} 处变化未列出"
                         "（都已并入待验证事项）")
    elif check.compared and site.get("checked") and not site.get("incomplete"):
        # HEAD 相同也不代表工作区相同——只能说"记录过的文件没变"。一个都没记录过就不说这句：
        # 那是一句空话，却读起来像"现场核对过了、没问题"。
        lines.append(f"上次记录过的 {site['checked']} 个文件内容与现在一致"
                     "（这不代表测试通过，也不代表工作区其它部分没变）。")
    for reason in site.get("incomplete") or []:
        lines.append(f"- 无法完整核对：{reason}")
    for note in notes or ():
        lines.append(f"- {note}")
    for warning in check.warnings:
        lines.append(f"- {warning}")
    pending = getattr(sess, "pending_verification", None) or {}
    if pending.get("files") or pending.get("tracking_incomplete"):
        lines.append("仍有待验证事项：完成前需要重新运行相关检查并查看改动。")
    if mode == "continue":
        lines.append("请先核对现场，再继续未完成的部分。")
    else:
        lines.append("处理用户的新消息时，凡涉及上面这些操作或文件，请先核对现场。")
    return "\n".join(lines)


def visible_note(check, notes=(), *, mode="continue"):
    """给用户看的那一段。和给模型的说明同源，但更短。"""
    parts = ["🔄 继续任务" if mode == "continue" else "🔄 上次运行没有正常结束，已补充恢复提示"]
    if check.unknown_ops:
        parts.append("结果未知、不会自动重放的操作：" + "、".join(
            f"{op.get('tool')}" for op in check.unknown_ops[:_MAX_RECORDED_OPS]))
    site = check.site or {}
    counts = []
    if site.get("changed"):
        counts.append(f"{len(site['changed'])} 个文件内容已变")
    if site.get("deleted"):
        counts.append(f"{len(site['deleted'])} 个文件已删除")
    if site.get("appeared"):
        counts.append(f"{len(site['appeared'])} 个文件新出现")
    if site.get("head_change"):
        counts.append("Git HEAD 已变化")
    if site.get("branch_change"):
        counts.append("分支已变化")
    if counts:
        parts.append("现场变化：" + "，".join(counts))
    if site.get("incomplete"):
        parts.append("部分现场无法完整核对")
    parts.extend(check.warnings)
    parts.extend(notes or [])
    return "\n".join(parts)


def apply_resume(sess, check, *, notes=(), mode="continue"):
    """把恢复检查的结论落到会话里。返回交接出去的 inflight 操作 id（落盘后再清理）。"""
    from langchain_core.messages import HumanMessage

    kind = RESUME_KIND if mode == "continue" else RECOVERY_KIND
    unknown_by_call = {op.get("tool_call_id"): op for op in check.unknown_ops
                       if op.get("tool_call_id")}
    history = sess.chat_history
    if check.dangling:
        _insert_placeholders(history, check.dangling, unknown_by_call, kind=kind,
                             inflight_unreadable=check.inflight_unreadable)
    _fold_obligations(sess, check)
    run = getattr(sess, "last_run", None) or {}
    note = visible_note(check, notes, mode=mode)
    # 交接出去的执行前记录跟着恢复说明落盘：sidecar 里那几条随后会被清掉（否则下次打开
    # 又为同一批操作报一遍"结果未知"），诊断线索不能跟着一起没了。
    record = {
        "mode": mode,
        "from_run_id": run.get("id") or "",
        "from_phase": run.get("phase") or "",
        "from_outcome": run.get("outcome"),
        "note": note,
        "operations": [_compact_op(op) for op in check.unknown_ops[:_MAX_RECORDED_OPS]],
        # 解析不了的条目：历史里只放**摘要**（条数 + 截断预览）和对隔离文件的**引用**，
        # 完整原文在那个文件里。只存预览的话，摘除 sidecar 之后超长部分和超出条数的记录
        # 就永久丢了（复核实测：21 条只剩 20 条截断预览）。
        "unreadable_count": len(check.unreadable_entries),
        "unreadable_entries": list(check.unreadable_entries[:_MAX_RECORDED_OPS]),
        "unreadable_archive": check.unreadable_archive,
        "dangling": [{"id": c["id"], "name": c["name"]} for c in check.dangling[:50]],
    }
    history.append(HumanMessage(
        content=build_summary(sess, check, notes, mode=mode),
        additional_kwargs={"lingxi_internal": True, "lingxi_kind": kind,
                           "lingxi_recovery": record}))
    return [op.get("operation_id") for op in check.unknown_ops if op.get("operation_id")], note


def acknowledge(sess, operation_ids, *, archived_malformed=None):
    """恢复说明**落盘之后**，把已交接的 inflight 记录从 sidecar 里摘掉。

    记录已经写进了会话历史（恢复说明 + 占位结果 + 附带的操作记录），诊断信息不会丢；
    不摘掉的话，下次打开会为同一批操作再报一遍"结果未知"。只摘交接过的那几条，不碰别的。
    `archived_malformed`：已经**完整**写进隔离文件的那批无法解析的条目，只摘它们。
    """
    from . import run_records
    session_id = getattr(sess, "current_session_id", None)
    if not session_id:
        return
    for op_id in operation_ids or []:
        run_records._clear_operation(sess, session_id, op_id)
    if archived_malformed:
        run_records.drop_malformed(sess, session_id, archived_malformed)


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════

def needs_adoption(sess):
    """不点「继续」、直接发新消息时，这一轮开始前是否要先做恢复处置。

    只看两件事：上一轮是不是被中断（磁盘上 phase=running 且不是本进程正在跑的那一轮），
    以及有没有结果未知的执行前记录。正常结束的会话这里只多一次 `os.path.exists`。
    """
    from . import run_records
    run = getattr(sess, "last_run", None) or {}
    if (run.get("phase") == "running" and run.get("id")
            and getattr(sess, "active_run_id", None) != run.get("id")):
        return True
    session_id = getattr(sess, "current_session_id", None)
    if not session_id or not os.path.exists(run_records.inflight_path(session_id)):
        return False
    unknown, unreadable, error = _unknown_operations(sess)
    return bool(unknown or unreadable or error)


def _hold_obligation(sess, reason):
    """恢复处置自己出了错：不知道中断时发生过什么，就按"可能有写入"留一段盲区。

    只在确实有东西要处置（上一轮被中断 / 有结果未知的记录）时才会走到这里。
    放过去照常收尾的话，又是拿"没核对成"推出"没有改动"。
    """
    from . import run_records
    try:
        root = run_records._work_dir_of(sess) or ""
        key = os.path.realpath(root) if root else UNKNOWN_ROOT
        pending = dict(getattr(sess, "pending_verification", None) or {})
        tracking = dict(pending.get("tracking_incomplete") or {})
        _add_reason(tracking, key, reason)
        pending["tracking_incomplete"] = tracking
        for name in ("files", "code_files"):
            pending[name] = list(pending.get(name) or [])
        pending.setdefault("reason", "")
        pending.setdefault("run_id", "")
        with sess.snapshot_lock:
            sess.pending_verification = pending
    except Exception as error:          # pragma: no cover - 兜底的兜底
        logger.error(f"保留验证义务失败: {error}", exc_info=True)


def before_run(sess, *, ui=None, resume=None):
    """agent_loop 在 begin_run 之前调用。返回 False = 本轮不启动（原因已告诉用户）。"""
    from . import run_records
    if not run_records.records_enabled(sess):
        return True
    if resume is not None:
        try:
            return prepare_and_apply(sess, ui=ui, mode="continue",
                                     expected_run_id=resume.get("expected_run_id") or "",
                                     notes=resume.get("notes") or ())
        except Exception as e:
            # 继续是"在核对过的现场上往下走"；核对这一步自己坏了，就不替用户往下走。
            logger.error(f"继续任务的恢复处置失败: {e}", exc_info=True)
            _say(ui, f"\n⛔ 恢复处置出错（{str(e)[:200]}），为安全起见没有继续。\n")
            return False
    try:
        if not needs_adoption(sess):
            return True
    except Exception as e:
        # 连"要不要处置"都判断不了：照常处理新消息，但不能当作没有东西要核对。
        logger.warning(f"恢复判定失败（照常处理新消息，保留验证义务）: {e}", exc_info=True)
        _hold_obligation(sess, f"恢复判定出错（{str(e)[:120]}），上次中断时的写入无法确认")
        return True
    try:
        return prepare_and_apply(sess, ui=ui, mode="message")
    except Exception as e:
        # 用户发的是新消息：恢复处置出错不能拦住它（旧记录还在，下次仍会处理），
        # 但本轮也不能因此按"没有改动"收尾。
        logger.warning(f"恢复处置失败（照常处理新消息，保留验证义务）: {e}", exc_info=True)
        _hold_obligation(sess, f"恢复处置出错（{str(e)[:120]}），上次中断时的写入无法确认")
        return True


def prepare_and_apply(sess, *, ui=None, mode="continue", expected_run_id="", notes=()):
    """worker 里、begin_run 之前跑的那一段。返回 False = 不继续（原因已告诉用户）。"""
    from . import memory
    run = getattr(sess, "last_run", None) or {}
    if mode == "continue" and expected_run_id and run.get("id") != expected_run_id:
        _say(ui, "\n⚠️ 这个会话已经有更新的一轮运行，没有从旧的结果继续。\n")
        return False

    try:
        check = inspect_site(sess, compare_site=(mode == "continue"))
    except Exception as e:
        logger.error(f"恢复检查失败: {e}", exc_info=True)
        if mode == "continue":
            # 核对不了就是不知道；不知道就不替用户往下走。
            _say(ui, f"\n⛔ 恢复检查出错（{str(e)[:200]}），为安全起见没有继续。\n")
            return False
        # 新消息照常处理（拦住它不成比例，旧记录下次还在），但要保留验证义务：
        # 核对不成不等于没有改动。
        _hold_obligation(sess, f"恢复检查出错（{str(e)[:120]}），上次中断时的写入无法确认")
        return True
    if check.blocking and mode == "continue":
        _say(ui, "\n⛔ 无法继续：\n" + "\n".join(f"- {b}" for b in check.blocking)
             + "\n请恢复原目录、或重新选择项目后再继续。\n")
        return False

    # 无法解析的条目先**完整**隔离留底，恢复说明里只放摘要和引用。留底失败就不摘原文：
    # 截断预览顶替不了原文，摘了就真丢了。
    if check.unreadable_raw:
        from . import run_records
        check.unreadable_archive, archive_error = run_records.archive_malformed(
            getattr(sess, "current_session_id", None) or "", check.unreadable_raw)
        if archive_error:
            check.warnings.append(f"无法解析的执行前记录未能单独留底（{archive_error[:120]}），"
                                  "原文先保留在执行前记录里")
    op_ids, note = apply_resume(sess, check, notes=notes, mode=mode)
    outcome = memory.save_session_report(session=sess)
    if outcome.body_written:
        acknowledge(sess, op_ids,
                    archived_malformed=check.unreadable_raw if check.unreadable_archive else None)
    else:
        # 没存上就不摘记录：恢复说明只在内存里，摘了就真的什么线索都不剩了。
        logger.warning(f"恢复说明未能落盘，保留 inflight 记录: {outcome.error}")
        if not outcome.skipped:
            _say(ui, "\n⚠️ 恢复说明未能保存；执行前记录已保留，下次打开仍会提示。\n")
    _say(ui, "\n" + note + "\n")
    return True


def _say(ui, text):
    if ui is None:
        return
    try:
        ui.show_message(text, "tool_result")
    except Exception:
        pass
