"""把运行结果快照翻译成**给人看的结论**（纯函数，不依赖 Qt）。

和 Qt 控件分开是有理由的：这一层承担的是"程序到底能断言什么"，那是可以被测试逐条
钉死的判断，不该混在控件布局里。`ui/result_card.py` 只负责把这里的结果画出来。

贯穿全文的取舍：**卡片上的每一句都必须有程序证据支撑**。模型说"已经完成"不算证据；
没跑过测试就不写"测试通过"；`unverified` 不翻译成"代码写完了只是没测"——它并不证明
代码实现是完整的。
"""


# 状态 → 标题。判据只看程序记录的终态，不看模型正文说了什么。
_TITLES = {
    "completed": "本轮回复结束",
    "failed": "本轮执行失败",
    "cancelled": "已停止",
    "unverified": "本轮结束，仍有未验证事项",
    "limit_reached": "已达到运行上限",
}

# 有效检查证据下的 completed 标题：明确限定在"已执行的检查"范围内，
# 不宣称需求都做对了。
_TITLE_COMPLETED_CHECKED = "本轮执行结束，已执行检查通过"
# 跑了检查但没全过 / 结论已失效。必须和上面那条分开——只要有一条没解决，
# 就不能把整轮概括成"检查通过"。
_TITLE_COMPLETED_UNRESOLVED = "本轮执行结束，检查未全部通过"
_TITLE_INTERRUPTED = "上次运行被中断"
_TITLE_RUNNING = "本轮仍在运行"
_TITLE_UNKNOWN = "本轮结果未知"

_STATUS_LABELS = {
    "passed": "通过",
    "failed": "失败",
    "not_run": "未执行",
    "timeout": "超时",
    "cancelled": "已取消",
    "error": "执行出错",
    "unknown": "结果未知",
}

_KIND_LABELS = {"tests": "测试", "check": "静态检查"}

# 文件列表超过这个数就折叠（卡片要能读，不是要把改动清单铺满屏幕）。
FILE_FOLD_THRESHOLD = 8


def _is_active_run(snapshot, live_run_id=None):
    """这张卡描述的是不是**正在跑**的那一轮。

    重绘时必须传 `live_run_id`：磁盘上 `phase=running` 有两种可能——真的被中断了，
    或者那一轮此刻仍在跑。把正在跑的显示成"上次运行被中断"是明确的误报。
    """
    return bool(live_run_id) and snapshot.get("run_id") == live_run_id


def validation_rows(snapshot):
    """实际执行过的检查，逐条给出可读结论。"""
    rows = []
    for record in (snapshot.get("evidence") or {}).get("validation_runs") or []:
        status = record.get("status") or "unknown"
        checker = record.get("checker") or _KIND_LABELS.get(record.get("kind"), "检查")
        label = _STATUS_LABELS.get(status, "结果未知")
        detail = []
        code = record.get("exit_code")
        if code is not None:
            detail.append(f"退出码 {code}")
        else:
            # 没有退出码就照实说没有，**不写 0**——"没跑起来"和"跑了且成功"是两回事。
            detail.append("无退出码")
        duration = record.get("duration_ms")
        if isinstance(duration, int) and duration > 0:
            detail.append(f"{duration / 1000:.1f}s")
        rows.append({
            "id": record.get("id") or "",
            # 归并身份用的指纹（采集时从完整值算），不是下面那些截断过的展示字段
            "identity": record.get("identity") or "",
            "kind": record.get("kind") or "check",
            "checker": checker,
            "path": record.get("path") or "",
            "status": status,
            "label": label,
            "exit_code": code,
            "duration_ms": duration if isinstance(duration, int) else 0,
            "stale": bool(record.get("stale")),
            "detail": "、".join(detail),
            "summary": record.get("summary") or "",
            "reason": record.get("reason") or "",
            "argv": record.get("argv"),
            "command": record.get("command") or "",
            "cwd": record.get("cwd") or "",
        })
    return rows


def check_identity(row):
    """两条记录是不是"同一项检查的两次执行"。

    用采集时算好的指纹（检查器 + 目录 + 目标 + **完整**命令的哈希），
    **不能拿这里的 argv / command 去比**——它们是展示字段，采集时已经截断过
    （argv 每项 300 字、命令 400 字）。实测两组 `pytest -k <702 字符>` 只有末尾不同，
    截断后完全一样，于是第一组的失败被第二组"取代"、卡片写成"已执行检查通过"，
    而第二组根本没跑那个失败用例。

    没有指纹的旧记录（本批之前存下的）**一律各算各的**，永不互相取代：
    宁可卡片停在"检查未全部通过"，也不能靠截断数据蒙一个"通过"出来。
    """
    fingerprint = row.get("identity")
    if fingerprint:
        return ("fp", fingerprint)
    return ("legacy", row.get("id") or id(row))


def effective_rows(snapshot):
    """每项检查只留**最后一次**执行的结论，并标出被取代的旧记录。

    同一条命令重试通过时，之前那次失败必须让位：文件没变，所以旧失败不会因为
    "过期"而退场，`all()` 会一直把它算进去，卡片就永远停在"检查未全部通过"，
    而完成闸门早就认了 `tests_passed=True`——两边对同一件事给出相反结论。

    记录按时间顺序，同一 identity 后来的覆盖先前的。旧记录**保留展示**，
    只是标成 `superseded`，不参与判定。
    """
    rows = validation_rows(snapshot)
    latest = {}
    for row in rows:
        latest[check_identity(row)] = row
    winners = set(id(r) for r in latest.values())
    for row in rows:
        row["superseded"] = id(row) not in winners
    return rows, list(latest.values())


def summarize_checks(snapshot):
    """汇总本轮检查：`"none"` 没跑过 / `"clear"` 都过了 / `"unresolved"` 还有没解决的。

    **不能用 `any(passed)`**：静态检查过了、pytest 挂了，那也是"有一条通过"，
    标题就会写成"已执行检查通过"——而这一轮恰恰是没通过的。判据是
    「**每项检查的最新有效结论**都通过」。

    过期记录不参与判定：它的结论已经不代表当前代码了。但它也不能顶替一次有效通过，
    所以"只剩过期记录"算 `unresolved`，不是 `clear`。
    """
    rows, latest = effective_rows(snapshot)
    if not rows:
        return "none"
    fresh = [r for r in latest if not r["stale"]]
    if not fresh:
        return "unresolved"          # 跑过，但结论全部失效
    if all(r["status"] == "passed" for r in fresh):
        return "clear"
    return "unresolved"


def has_fresh_pass(snapshot):
    """本轮已执行的检查是不是**全部**通过（结果卡据此决定能不能说"检查通过"）。"""
    return summarize_checks(snapshot) == "clear"


# 这些终态可以「继续任务」：没做完（停止 / 失败 / 触顶）或做完了但没验证完。
# completed 不给——任务已经正常结束，"继续"只会让人以为还有什么没做。
_RESUMABLE_OUTCOMES = ("cancelled", "failed", "limit_reached", "unverified")

_STOP_LABELS = {
    "cancelled": "用户停止",
    "failed": "本轮执行失败",
    "limit_reached": "达到轮数上限",
    "unverified": "仍有未验证事项",
}


def is_resumable(snapshot, live_run_id=None):
    """这张卡上能不能放「继续任务」。

    必须能认出归属（session_id + run_id）：继续前要核对"这张卡确实是该会话最新的一轮"，
    认不出归属的卡给了按钮也只能拒绝，放一个点了必然失败的按钮比不放更糟。
    正在跑的那一轮不给（它还没停）；磁盘上 phase=running 但不是正在跑的 = 被中断，给。
    """
    if not isinstance(snapshot, dict):
        return False
    if not snapshot.get("session_id") or not snapshot.get("run_id"):
        return False
    if snapshot.get("phase") == "running":
        return not _is_active_run(snapshot, live_run_id)
    return snapshot.get("outcome") in _RESUMABLE_OUTCOMES


def describe(snapshot, *, live_run_id=None):
    """把快照翻译成结果卡的全部内容。返回纯数据 dict，便于逐条断言。"""
    if not isinstance(snapshot, dict):
        return None

    phase = snapshot.get("phase") or ""
    outcome = snapshot.get("outcome")
    # 走 effective_rows 而不是 validation_rows：它顺带给每条打上 superseded，
    # 卡片才能把"后来重试通过了"的旧失败标出来，而不是并排摆两个矛盾的结论。
    rows, _latest = effective_rows(snapshot)
    files = list((snapshot.get("evidence") or {}).get("changed_files") or [])
    pending = snapshot.get("pending_verification") or {}

    # ── 标题 ──
    if phase == "running":
        if _is_active_run(snapshot, live_run_id):
            title, tone = _TITLE_RUNNING, "running"
        else:
            # 没收到终态。展示中断**不改写磁盘上的 phase/outcome**，只是这样呈现。
            title, tone = _TITLE_INTERRUPTED, "interrupted"
    elif outcome == "completed":
        checks = summarize_checks(snapshot)
        if checks == "clear":
            title, tone = _TITLE_COMPLETED_CHECKED, "ok"
        elif checks == "unresolved":
            # 跑过检查但没全过。既不能说"检查通过"，也不能只说"回复结束"
            # （那会把一次有失败项的运行说得像纯问答）。
            title, tone = _TITLE_COMPLETED_UNRESOLVED, "warn"
        else:
            # 纯问答 / 调研：没有检查证据就只说回复结束，绝不暗示"测过了"。
            title, tone = _TITLES["completed"], "ok"
    elif outcome in _TITLES:
        title = _TITLES[outcome]
        tone = {"failed": "bad", "cancelled": "muted",
                "unverified": "warn", "limit_reached": "warn"}[outcome]
    else:
        title, tone = _TITLE_UNKNOWN, "muted"

    # ── 保存状态：与运行结果完全分开的一行 ──
    save_note = ""
    saved = snapshot.get("saved")
    if saved is False:
        if snapshot.get("save_body_written") and not snapshot.get("save_index_written"):
            save_note = "会话正文已保存，但侧栏索引更新失败（下次打开会自动补回）。"
        else:
            detail = snapshot.get("save_error") or snapshot.get("save_skipped") or "原因未知"
            save_note = f"最新进度未保存：{detail}"

    # ── 未验证事项 ──
    pending_files = list(pending.get("files") or [])
    pending_reason = pending.get("reason") or ""

    # ── 继续入口（B04）──
    resumable = is_resumable(snapshot, live_run_id)
    if phase == "running":
        stop = "运行被中断" if resumable else ""
    else:
        stop = _STOP_LABELS.get(outcome, "")
    plan = snapshot.get("plan") if isinstance(snapshot.get("plan"), dict) else {}
    model = snapshot.get("model") if isinstance(snapshot.get("model"), dict) else {}

    return {
        "title": title,
        "tone": tone,
        "reason": snapshot.get("reason") or "",
        "run_id": snapshot.get("run_id") or "",
        "session_id": snapshot.get("session_id") or "",
        "project": snapshot.get("project"),
        # 本轮实际的落点（worktree 优先）。"查看改动"必须用它：拿项目根去 diff 一个
        # 在隔离区里跑的轮次，只会回一句"工作区干净"。
        "work_dir": snapshot.get("work_dir") or snapshot.get("project"),
        "task_id": snapshot.get("task_id"),
        "files": files,
        # 没有净差异证据，所以只说"本轮涉及"——写"净修改 N 个文件"是它给不出的结论。
        "files_caption": f"本轮涉及 {len(files)} 个文件" if files else "",
        "files_folded": len(files) > FILE_FOLD_THRESHOLD,
        "validations": rows,
        "has_validations": bool(rows),
        "has_stale": any(r["stale"] for r in rows),
        "has_superseded": any(r.get("superseded") for r in rows),
        "pending_files": pending_files,
        "pending_reason": pending_reason,
        "has_pending": bool(pending_files or pending_reason
                            or pending.get("tracking_incomplete")),
        "save_note": save_note,
        "diff_reviewed": bool((snapshot.get("evidence") or {}).get("diff_reviewed")),
        "display_source": snapshot.get("display_source") or "live",
        # 有真实资料才给入口：没有目录就点不出差异，没有证据就没有输出可看。
        "can_view_diff": bool(snapshot.get("work_dir") or snapshot.get("project")),
        "can_view_output": any(r["summary"] or r["reason"] or r["argv"] for r in rows),
        "resumable": resumable,
        "stop_label": stop,
        # 计划进度照实标成"模型记录"：勾选是模型自己写的，不是程序验收过的。
        "plan_done": int(plan.get("done") or 0),
        "plan_total": int(plan.get("total") or 0),
        "plan_next": str(plan.get("next") or ""),
        # 上一轮记录的模型。继续前会按它找回；找不到就要求重新选，不静默换后端。
        "model_label": str(model.get("name") or model.get("id") or ""),
        "agent_mode": snapshot.get("agent_mode") or "act",
    }


def plain_text(view) -> str:
    """结果卡的纯文本形态（日志 / 无 GUI 环境 / 测试断言用）。"""
    if not view:
        return ""
    lines = [view["title"]]
    if view["reason"]:
        lines.append(f"原因：{view['reason']}")
    if view["files_caption"]:
        lines.append(f"{view['files_caption']}：{'、'.join(view['files'])}")
    for row in view["validations"]:
        target = f" {row['path']}" if row["path"] else ""
        stale = ("（已过期）" if row["stale"]
                 else "（已被后一次复查取代）" if row.get("superseded") else "")
        lines.append(f"{_KIND_LABELS.get(row['kind'], '检查')}{target} · "
                     f"{row['checker']}：{row['label']}{stale}（{row['detail']}）")
    if view["has_pending"]:
        lines.append(f"未验证：{view['pending_reason'] or '存在未验证改动'}")
    if view.get("resumable"):
        lines.extend(resume_lines(view))
    if view["save_note"]:
        lines.append(f"保存：{view['save_note']}")
    return "\n".join(lines)


def resume_lines(view):
    """「继续任务」上方那几行（方案 §5.6）。卡片与纯文本共用，免得两处说法不一。"""
    lines = []
    if view.get("stop_label"):
        lines.append(f"上次停止：{view['stop_label']}")
    if view.get("plan_total"):
        line = f"计划进度：模型记录为 {view['plan_done']} / {view['plan_total']}"
        lines.append(line)
        if view.get("plan_next"):
            lines.append(f"下一步：检查现场后继续「{view['plan_next']}」")
    else:
        lines.append("下一步：检查现场后继续")
    # 只说"上次用的是什么"：卡片是纯数据，不知道那个模型现在还在不在配置里。
    # 实际用哪个模型由点击后的预检决定并当场显示，找不到就要求重选——卡片不替它打包票。
    mode = "Plan 模式（只调研，不改动）" if view.get("agent_mode") == "plan" else "Act 模式"
    model = view.get("model_label") or "未记录"
    lines.append(f"上次使用：{model} · {mode}")
    return lines
