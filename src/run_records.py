"""运行记录（run）与工具边界的 inflight 标记。

三个概念必须分开，不能互相冒充：

- **会话 Session**：聊天历史与项目归属，可以先后承载多个任务。
- **任务 Task**：用户目标、计划、累计进度。B05 才建立任务身份，本批 `task_id` 允许为空。
- **运行 Run**：一次点击发送 / 重试 / 继续所启动的一轮 agent 循环。一个任务可有多次运行。

`run_id` 由程序生成，与 `session_id`、`task_id` 无关；来源信息也由程序填写——**自动修复
提示、完成闸门提示、视觉桥接说明都是程序注入的 HumanMessage，不是新的真实用户要求**，
它们带 `additional_kwargs["lingxi_internal"]=True`，本模块识别并跳过。

## 两个文件、两件事

主会话 JSON（`<id>.json`）的 `progress` 里存**结果**：`last_run`（本轮终态）、
`pending_verification`（尚未解决的验证义务）、`last_committed_operation` 与
`recent_operations`（完成回执）。

独立 sidecar（`<id>.inflight.json`）存**正在进行的有副作用操作**。它单独成文件是因为：
工具执行前若要写主 JSON，就得把整段聊天历史重写一遍，长会话里每个写操作都付这个代价。
sidecar 只装 ID、工具名和路径，不复制历史、长参数或完整输出。

## 这套机制承诺什么、不承诺什么

- 承诺：崩溃后能**有记录地**说出"哪个操作已调度、结果是否已落盘"，据此提示重新核对。
- 不承诺：任何命令恰好执行一次。进程可能死在"副作用已发生"与"结果已保存"之间，
  这一段不是原子写能消除的。此时操作标为**结果未知**，既不算成功也不算未执行。
- 「已调度」不等于「副作用已发生」：写文件工具还要等用户在确认卡上点同意，
  记录先写、确认后执行，这中间被杀掉的话文件其实没动过——但程序无从分辨，故仍报未知。
- 回执落盘 = 「这条工具结果已可靠记录」，**不等于**「工具没有产生副作用」。
  工具执行失败同样会留回执，因为失败也可能改了一半文件。
"""
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .paths import logger, memory_dir


RUN_RECORD_VERSION = 1
INFLIGHT_VERSION = 1

# 完成回执环形缓冲上限。只留一条不够：A、B 相继提交而 A 的 sidecar 清理失败时，
# 回执已经是 B 的，A 就会被误判成"结果未知"——明明它已经落盘了。
_RECEIPT_RING = 50

# 运行来源文本预览上限：记录是用来认人的，不是用来存原文的（原文在 messages 里）。
_SOURCE_PREVIEW = 200

_VALID_PHASE = ("running", "ended", "interrupted")
# 与 AgentResult.status 一致；None = 中断，程序从未收到本轮的返回值。
_VALID_OUTCOME = ("completed", "failed", "cancelled", "unverified", "limit_reached")


class InflightWriteError(Exception):
    """执行前记录没写成功。

    此时**必须中止该工具调用**：恢复保障建立不起来却照样动手，等于崩溃后既不知道
    改了什么、也没有任何线索——比不做这套记录更糟，因为用户会以为有记录。
    """


@dataclass(frozen=True)
class SaveOutcome:
    """一次主快照保存的分步结果。

    不能用一个布尔值概括：正文写成功而索引写失败时，**操作确实已提交**（回执在正文里），
    可以清理 sidecar；但这次保存整体是失败的，调用方要照实告诉用户。
    """
    body_written: bool = False
    index_written: bool = False
    revision: int = 0
    skipped: str = ""          # 非失败的跳过原因（子 Agent / 历史太短）
    error: BaseException | None = None
    elapsed_ms: float = 0.0
    bytes_written: int = 0

    @property
    def fully_saved(self) -> bool:
        return self.body_written and self.index_written


@dataclass(frozen=True)
class CommitReport:
    """工具边界提交的结果。"""
    committed: bool             # 结果是否已可靠落盘（正文写成功即为真）
    save: SaveOutcome
    marker_cleared: bool        # sidecar 里的匹配记录是否已清掉
    clear_error: str = ""


@dataclass(frozen=True)
class FinalizeReport:
    """统一收尾的结果。运行结果与保存结果分开，互不冒充。"""
    result: object              # AgentResult，原样返回
    saved: bool
    save: SaveOutcome | None = None
    stale: bool = False         # 旧 run 的迟到收尾，已忽略
    duplicate: bool = False     # 重复 finalize，未再生成结束记录


@dataclass
class Operation:
    """一次有副作用工具调用的本地记录（内存态）。"""
    operation_id: str
    run_id: str
    session_id: str
    tool: str
    tool_call_id: str
    base_revision: int
    paths: list = field(default_factory=list)
    started_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


# ══════════════════════════════════════════════════════════════
# 哪些工具需要执行前记录
# ══════════════════════════════════════════════════════════════

# **只**列纯读工具；判据是「崩溃后不会留下任何需要重新核对的持久变化」。
# 不在这份清单里的一律记录——包括未知工具与所有 mcp_* 远程工具。宁可多写一个小文件，
# 也不要因为清单没跟上新工具，让一个真会改盘的操作悄悄溜过执行前记录。
#
# 注意 remember / forget / notify_user / update_plan / set_step_status 虽然在
# Plan 模式白名单里，但它们**有副作用**（写长期记忆 / 推 Telegram / 改计划），
# 不能拿 PLAN_MODE_READONLY_TOOLS 当这份清单用。
#
# **`run_tests` / `check_code` 也不在这份清单里**，尽管它们"看起来只是检查"：
# `run_tests` 起 pytest，测试代码是**项目自己的代码**，写文件、建目录、连数据库都合法；
# `check_code` 在非 Python 项目里执行 config 的 `check_command`，那是用户配的任意命令。
# 早先把这两个当纯读，后果实测过：测试真的写出了文件、进程在结果返回前死掉，
# 而 sidecar 前后都没有记录，恢复分类返回空列表——连"结果未知"的线索都没有。
# 判据是「这个工具会不会执行项目代码或用户配置的命令」，不是「它的名字听起来像不像检查」。
SIDE_EFFECT_FREE_TOOLS = frozenset({
    "read_file", "list_directory", "search_in_file", "search_files",
    "code_map", "find_definition", "find_references", "find_tests", "related_files",
    "git_diff", "git_log", "git_status",
    "read_background_output", "list_background_commands",
    "get_project_instructions", "search_knowledge",
    # 网络只读：GET 语义、不落盘。已知边界见 CLAUDE.md 的 fetch_url DNS 重绑定条目——
    # 重绑定到某个对 GET 有副作用的内网端点时，这里确实不会留记录。
    "fetch_url", "web_search",
})

# 从参数里挑"值得记进 sidecar 的路径"。体积要控住：不复制 content/new_string 这类长参数。
_PATH_ARG_KEYS = ("path", "file", "file_path", "target", "cwd")


def extract_paths(args) -> list:
    if not isinstance(args, dict):
        return []
    out = []
    for key in _PATH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            out.append(value[:300])
    return out[:8]


def needs_record(tool_name: str) -> bool:
    return tool_name not in SIDE_EFFECT_FREE_TOOLS


def records_enabled(sess) -> bool:
    """子 Agent 是临时会话：不落盘、不进侧栏，运行记录同样不写。

    统一 finalize 不能顺手让子 Agent 开始往主会话历史里写东西或发额外通知。
    """
    return not getattr(sess, "is_subagent", False)


# ══════════════════════════════════════════════════════════════
# 归一化：磁盘 → 内存
# ══════════════════════════════════════════════════════════════

def normalize_last_run(raw):
    """校验磁盘上的 last_run，返回 (记录 或 None, 错误原因)。

    与进度的其它部分同一套取舍：读不懂就整块作废并说明原因，绝不"尽量抢救"——
    抢救出来的运行记录看起来完全正常，用户无从分辨它是不是真的。
    """
    if raw is None:
        return None, ""
    if not isinstance(raw, dict):
        return None, "last_run 不是对象"
    ver = raw.get("version")
    if ver is not None and (not isinstance(ver, int) or ver > RUN_RECORD_VERSION):
        return None, f"last_run 版本 {ver!r} 无法识别"
    run_id = raw.get("id")
    if not isinstance(run_id, str) or not run_id:
        return None, "last_run 缺少 id"
    phase = raw.get("phase")
    if phase not in _VALID_PHASE:
        return None, f"last_run.phase={phase!r} 非法"
    outcome = raw.get("outcome")
    # 严格比对枚举：用 truthy 判断会把任意字符串当成有效终态，
    # 于是"中断"被读成"完成"——这正是最不能出错的方向。
    if outcome is not None and outcome not in _VALID_OUTCOME:
        return None, f"last_run.outcome={outcome!r} 非法"
    task_id = raw.get("task_id")
    if task_id is not None and not isinstance(task_id, str):
        return None, "last_run.task_id 类型非法"
    source = raw.get("source")
    if source is not None and not isinstance(source, dict):
        return None, "last_run.source 不是对象"
    evidence = raw.get("evidence")
    if evidence is not None and not isinstance(evidence, dict):
        return None, "last_run.evidence 不是对象"
    for key in ("started_at", "ended_at", "reason"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            return None, f"last_run.{key} 类型非法"
    return {
        "version": RUN_RECORD_VERSION,
        "id": run_id,
        "task_id": task_id,
        "phase": phase,
        "outcome": outcome,
        "reason": raw.get("reason") or "",
        "started_at": raw.get("started_at") or "",
        "ended_at": raw.get("ended_at") or "",
        "source": dict(source) if source else {},
        "evidence": _normalize_evidence(evidence),
    }, ""


def _normalize_evidence(raw):
    empty = {"changed_files": [], "validation_runs": [], "diff_reviewed": False}
    if not isinstance(raw, dict):
        return empty
    files = raw.get("changed_files")
    runs = raw.get("validation_runs")
    return {
        "changed_files": [f for f in files if isinstance(f, str)] if isinstance(files, list) else [],
        "validation_runs": [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else [],
        "diff_reviewed": raw.get("diff_reviewed") is True,
    }


def empty_pending_verification():
    return {"files": [], "code_files": [], "tracking_incomplete": {},
            "reason": "", "run_id": ""}


def normalize_pending_verification(raw):
    """校验磁盘上的待验证义务，返回 (义务, 错误原因)。

    读不懂时**回空并报错**，由调用方决定怎么提示。这里回空不代表"已验证"——
    上层要把 `progress_error` 一起展示，否则"读不懂"会被误读成"没有待验证项"。
    """
    empty = empty_pending_verification()
    if raw is None:
        return empty, ""
    if not isinstance(raw, dict):
        return empty, "pending_verification 不是对象"
    files = raw.get("files", [])
    code_files = raw.get("code_files", [])
    tracking = raw.get("tracking_incomplete", {})
    if not isinstance(files, list) or not isinstance(code_files, list):
        return empty, "pending_verification 的文件列表类型非法"
    if not isinstance(tracking, dict):
        return empty, "pending_verification.tracking_incomplete 不是对象"
    for item in list(files) + list(code_files):
        if not isinstance(item, str):
            return empty, "pending_verification 含非字符串路径"
    for key, value in tracking.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return empty, "pending_verification.tracking_incomplete 含非字符串项"
    reason = raw.get("reason", "")
    run_id = raw.get("run_id", "")
    if not isinstance(reason, str) or not isinstance(run_id, str):
        return empty, "pending_verification 的说明字段类型非法"
    return {"files": list(files), "code_files": list(code_files),
            "tracking_incomplete": dict(tracking), "reason": reason,
            "run_id": run_id}, ""


def _normalize_receipt(raw):
    if not isinstance(raw, dict):
        return None
    for key in ("operation_id", "run_id", "session_id"):
        if not isinstance(raw.get(key), str) or not raw.get(key):
            return None
    revision = raw.get("revision", 0)
    return {
        "operation_id": raw["operation_id"],
        "run_id": raw["run_id"],
        "session_id": raw["session_id"],
        "tool": raw.get("tool") if isinstance(raw.get("tool"), str) else "",
        "tool_call_id": (raw.get("tool_call_id")
                         if isinstance(raw.get("tool_call_id"), str) else ""),
        "revision": revision if isinstance(revision, int) and revision >= 0 else 0,
        "committed_at": (raw.get("committed_at")
                         if isinstance(raw.get("committed_at"), str) else ""),
    }


def normalize_receipts(raw_last, raw_recent):
    """返回 (last_committed_operation, recent_operations)。坏条目丢弃、不整块作废：

    回执是**附加证据**，少一条只会让某个操作退回"结果未知"（保守方向）；
    而把整块作废会让所有已提交操作一起退回未知，反而更不准。
    """
    recent = []
    if isinstance(raw_recent, list):
        for item in raw_recent:
            entry = _normalize_receipt(item)
            if entry is not None:
                recent.append(entry)
    last = _normalize_receipt(raw_last)
    if last is not None and not any(r["operation_id"] == last["operation_id"] for r in recent):
        recent.append(last)
    return last, recent[-_RECEIPT_RING:]


# ══════════════════════════════════════════════════════════════
# 运行开始 / 结束
# ══════════════════════════════════════════════════════════════

def _is_internal(msg) -> bool:
    kwargs = getattr(msg, "additional_kwargs", None) or {}
    return kwargs.get("lingxi_internal") is True


def describe_source(chat_history):
    """由程序判定本轮来源：最后一条**真实**用户消息。

    自动修复提示、完成闸门提示、视觉桥接说明都带 lingxi_internal 标记，在这里被跳过。
    不跳过的话，一轮自动修复就会把来源改写成程序自己写的那段文字，
    于是"用户要求了什么"这条线索在最需要它的长任务里最先失真。
    """
    from langchain_core.messages import HumanMessage
    skipped = 0
    for index in range(len(chat_history) - 1, -1, -1):
        msg = chat_history[index]
        if not isinstance(msg, HumanMessage):
            continue
        if _is_internal(msg):
            skipped += 1
            continue
        content = msg.content
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content
                     if isinstance(p, dict) and p.get("type") == "text"]
            text = texts[0] if texts else "[图片]"
        else:
            text = str(content)
        return {"kind": "user_message", "message_index": index,
                "text": text[:_SOURCE_PREVIEW], "internal_skipped": skipped}
    return {"kind": "unknown", "message_index": -1, "text": "", "internal_skipped": skipped}


def begin_run(sess, *, task_id=None, ui=None):
    """开一轮运行：生成 run_id、记录来源与开始时间、重置并恢复验证义务、**落盘**。

    返回运行记录 dict（子 Agent 返回 None）。**验证重置放在这里**是有意的：
    `reset_verification` 会清空 dirty 文件，恢复逻辑必须排在它之后，
    否则先填充再被清掉——未解决的验证义务就在每轮开头静默蒸发了。

    **开始记录必须在进入运行主体之前落盘**，否则整套中断识别都是空的：实测过，
    新一轮已经生成 run_id、进程在首次工具调用前退出，磁盘上却还是上一轮的
    ended/completed——这次中断连一条记录都没有，而"重开发现 phase=running"正是
    B04 恢复界面唯一的入口。只更新内存等于把这件事留给了第一次工具提交，
    而那之前的窗口恰恰是最容易崩的。

    保存失败**不中止本轮**：用户的消息已经进了历史，为了一条记录把整轮拒掉不成比例。
    但要明确告诉用户并记 error 日志，不假装恢复点已经建立。
    """
    from .verification import reset_verification, restore_obligations

    verification = getattr(sess, "verification", None)
    if verification is not None:
        reset_verification(verification)
        # 子 Agent 不落盘，但内存里的未了义务同样不该被本轮重置清掉。
        restore_obligations(verification, getattr(sess, "pending_verification", None))
    if not records_enabled(sess):
        return None

    run = {
        "version": RUN_RECORD_VERSION,
        "id": new_id("run"),
        "task_id": task_id,
        "phase": "running",
        "outcome": None,
        "reason": "",
        "started_at": _now(),
        "ended_at": "",
        "source": describe_source(getattr(sess, "chat_history", None) or []),
        "evidence": {"changed_files": [], "validation_runs": [], "diff_reviewed": False},
    }
    with sess.snapshot_lock:
        sess.last_run = run
        sess.active_run_id = run["id"]
    # 义务摘要改挂到这一轮，再连同 running 记录一起存——两者必须是同一份快照，
    # 否则崩溃后会看到"这一轮的开始记录"配"上一轮的待验证事项"。
    refresh_pending_verification(sess, run_id=run["id"])
    outcome = save_snapshot(sess)
    # 不把"存没存上"写进 run 字典：那是**本进程**的一次性事实，重开之后毫无意义
    # （能读到这条记录就说明它存上了），塞进去只会多一个 normalize 不认识、
    # 往返一次就丢的字段。明确处理 = 记日志 + 告诉用户，不是多一个持久化字段。
    if not outcome.body_written and not outcome.skipped:
        detail = outcome.error if outcome.error is not None else "未知原因"
        logger.error(f"运行开始记录未能落盘 run_id={run['id']}: {detail}")
        if ui is not None:
            try:
                ui.show_message(
                    f"\n⚠️ 本轮的开始记录未能保存（{str(detail)[:200]}）。"
                    "任务照常执行，但如果中途异常退出，重开时可能认不出这一轮被中断。\n",
                    "tool_result")
            except Exception:
                pass
    logger.info(f"运行开始 run_id={run['id']} 来源={run['source'].get('kind')}")
    return run


def collect_evidence(verification):
    """把验证状态整理成本轮证据。纯读，不碰磁盘。"""
    if not isinstance(verification, dict):
        return {"changed_files": [], "validation_runs": [], "diff_reviewed": False}
    runs = []
    if verification.get("tests_run"):
        runs.append({"kind": "tests", "passed": verification.get("tests_passed"),
                     "reason": verification.get("tests_reason") or ""})
    for path, result in (verification.get("checks") or {}).items():
        if isinstance(result, dict):
            runs.append({"kind": "check", "path": path,
                         "passed": result.get("passed"),
                         "checker": result.get("checker") or ""})
    return {
        "changed_files": list(verification.get("dirty_files") or []),
        "validation_runs": runs,
        "diff_reviewed": bool(verification.get("diff_reviewed")),
    }


def finalize_run(sess, run, result, *, ui=None) -> FinalizeReport:
    """统一收尾：正常完成、取消、异常、提前返回都走这里。

    三条不变量：
    1. **幂等**——重复调用不会再生成一份结束记录，也不会重复保存。
    2. **旧 run 不覆盖新 run**——同一会话里迟到的收尾按 stale 忽略。
    3. **运行结果与保存结果分开**——保存失败照样如实返回运行结果，
       但 `saved=False`，调用方要追加"最新进度未保存"的独立提示。
    """
    if run is None or not records_enabled(sess):
        return FinalizeReport(result=result, saved=False)

    with sess.snapshot_lock:
        active = getattr(sess, "active_run_id", None)
        current = getattr(sess, "last_run", None)
        if active is not None and active != run["id"]:
            logger.info(f"忽略旧运行 {run['id']} 的收尾（当前运行是 {active}）")
            return FinalizeReport(result=result, saved=False, stale=True)
        if (isinstance(current, dict) and current.get("id") == run["id"]
                and current.get("phase") == "ended"):
            return FinalizeReport(result=result, saved=False, duplicate=True)
        if isinstance(current, dict) and current.get("id") != run["id"]:
            # last_run 已经被另一个 run 顶掉，但 active_run_id 不是我们也不是它——
            # 按 stale 处理（宁可少写一条记录，也不要改写别人的事实）。
            logger.warning(f"运行 {run['id']} 的收尾与 last_run {current.get('id')!r} 不符，已忽略")
            return FinalizeReport(result=result, saved=False, stale=True)

        status = getattr(result, "status", None)
        run["phase"] = "ended"
        run["outcome"] = status if status in _VALID_OUTCOME else None
        run["reason"] = str(getattr(result, "reason", "") or "")
        run["ended_at"] = _now()
        run["evidence"] = collect_evidence(getattr(sess, "verification", None))
        sess.last_run = run

    refresh_pending_verification(sess, run_id=run["id"])
    outcome = save_snapshot(sess)
    with sess.snapshot_lock:
        if getattr(sess, "active_run_id", None) == run["id"]:
            sess.active_run_id = None

    if not outcome.fully_saved and not outcome.skipped and ui is not None:
        detail = outcome.error if outcome.error is not None else "未知原因"
        try:
            ui.show_message(f"\n⚠️ 最新进度未保存（{str(detail)[:200]}），"
                            "本轮的实际运行结果见上方。\n", "tool_result")
        except Exception:
            pass
    return FinalizeReport(result=result, saved=outcome.fully_saved, save=outcome)


def refresh_pending_verification(sess, *, run_id=""):
    """把当前验证状态里尚未解决的部分写回会话的待验证义务。纯内存，不重扫磁盘。"""
    from .verification import summarize_obligations

    pending = summarize_obligations(getattr(sess, "verification", None))
    pending["run_id"] = run_id or (getattr(sess, "last_run", None) or {}).get("id", "")
    with sess.snapshot_lock:
        sess.pending_verification = pending
    return pending


def describe_last_run(last_run):
    """把磁盘上的 last_run 翻译成给用户看的一句话，**不改写磁盘事实**。

    磁盘记录为 running = 程序从未收到本轮的返回值，展示"上次运行被中断"；
    绝不因此凭空生成一个 completed 或 failed 的 outcome。
    """
    if not isinstance(last_run, dict):
        return ""
    if last_run.get("phase") == "running":
        return "上次运行被中断（程序未收到本轮结果）"
    mapping = {
        "completed": "上次运行正常完成",
        "failed": "上次运行失败",
        "cancelled": "上次运行被用户停止",
        "unverified": "上次运行结束但改动未完成验证",
        "limit_reached": "上次运行达到轮数上限",
    }
    outcome = last_run.get("outcome")
    if outcome in mapping:
        reason = last_run.get("reason") or ""
        return f"{mapping[outcome]}{('：' + reason[:120]) if reason else ''}"
    return "上次运行结果未知"


# ══════════════════════════════════════════════════════════════
# 主快照保存
# ══════════════════════════════════════════════════════════════

def save_snapshot(sess) -> SaveOutcome:
    """保存主会话 JSON，返回分步结果而不是抛异常。

    调用方需要知道"正文写没写成功"而不只是"这次保存成不成功"：正文写成功
    就意味着完成回执已经落盘，sidecar 可以清；只看一个成功布尔值会把这两件事混成一件。
    """
    from . import memory
    return memory.save_session_report(session=sess)


# ══════════════════════════════════════════════════════════════
# inflight sidecar
# ══════════════════════════════════════════════════════════════

def inflight_path(session_id) -> str:
    return os.path.join(memory_dir(), f"{session_id}.inflight.json")


def _read_inflight_raw(session_id):
    path = inflight_path(session_id)
    if not os.path.exists(path):
        return {"version": INFLIGHT_VERSION, "session_id": session_id, "operations": []}, ""
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except Exception as error:
        return None, str(error)
    if not isinstance(data, dict) or not isinstance(data.get("operations"), list):
        return None, "inflight 文件结构非法"
    ver = data.get("version")
    if ver is not None and (not isinstance(ver, int) or ver > INFLIGHT_VERSION):
        return None, f"inflight 版本 {ver!r} 无法识别"
    ops = [op for op in data["operations"] if isinstance(op, dict) and op.get("operation_id")]
    return {"version": INFLIGHT_VERSION, "session_id": data.get("session_id") or session_id,
            "operations": ops}, ""


def _write_inflight(session_id, doc):
    from . import memory
    memory._atomic_write_json(inflight_path(session_id), doc)


def _inflight_lock(sess):
    lock = getattr(sess, "inflight_lock", None)
    if lock is None:
        lock = threading.RLock()
        sess.inflight_lock = lock
    return lock


def begin_operation(sess, *, tool, tool_call_id, args=None, paths=None):
    """有副作用的工具进入调用**前**，可靠写下这次操作的记录。

    写不成功就抛 `InflightWriteError`，由调用方中止该工具调用——
    假装已经建立恢复点再继续写文件，比根本没有这套机制更危险。

    sidecar 里是**操作列表**而不是单个对象：同一会话理论上可能有多个操作同时在途
    （目前只读工具才并行、写工具串行，但这个前提不该被隐含依赖），
    列表结构下一个操作不可能覆盖另一个未完成的操作。
    """
    if not records_enabled(sess):
        return None
    session_id = getattr(sess, "current_session_id", None)
    if not session_id:
        # 还没拿到会话 id（首次保存前）。此时会话文件本身都不存在，恢复无从谈起；
        # 如实返回 None，不假装记录已建立。
        return None
    run_id = getattr(sess, "active_run_id", None) or ""
    with sess.snapshot_lock:
        base_revision = int(getattr(sess, "progress_revision", 0) or 0)
    operation = Operation(
        operation_id=new_id("op"),
        run_id=run_id,
        session_id=session_id,
        tool=tool,
        tool_call_id=tool_call_id or "",
        base_revision=base_revision,
        paths=list(paths) if paths is not None else extract_paths(args),
        started_at=_now(),
    )
    entry = {
        "operation_id": operation.operation_id,
        "run_id": operation.run_id,
        "session_id": operation.session_id,
        "tool": operation.tool,
        "tool_call_id": operation.tool_call_id,
        "base_revision": operation.base_revision,
        "paths": operation.paths,
        "started_at": operation.started_at,
        # 「已调度」而不是「已执行」：写文件工具还要等用户在确认卡上点同意。
        "state": "dispatched",
    }
    with _inflight_lock(sess):
        doc, error = _read_inflight_raw(session_id)
        if doc is None:
            # 读不懂现有 sidecar：不能直接新建覆盖（那会抹掉另一个未完成操作的唯一线索），
            # 也不能继续执行。中止本次调用并报错。
            raise InflightWriteError(
                f"已有的执行前记录无法读取（{error}），已中止 {tool} 以免丢失恢复线索。")
        doc["operations"].append(entry)
        try:
            _write_inflight(session_id, doc)
        except Exception as write_error:
            raise InflightWriteError(
                f"写执行前记录失败（{write_error}），已中止 {tool}："
                "没有恢复点就动手，崩溃后无法判断这个操作是否发生过。") from write_error
    return operation


def commit_operation(sess, operation, *, ui=None) -> CommitReport:
    """工具结果、台账、进度与待验证事项都进内存之后，提交一次主快照，再清 sidecar。

    顺序不可颠倒：**只有主快照连同匹配的完成回执可靠落盘，才允许清理 inflight**。
    先删标记再保存的话，保存失败就等于把唯一线索丢了。
    """
    if operation is None or not records_enabled(sess):
        return CommitReport(committed=False, save=SaveOutcome(skipped="无需记录"),
                            marker_cleared=False)

    receipt = {
        "operation_id": operation.operation_id,
        "run_id": operation.run_id,
        "session_id": operation.session_id,
        "tool": operation.tool,
        "tool_call_id": operation.tool_call_id,
        "revision": 0,              # 由 _save_session_locked 填成本次保存的 revision
        "committed_at": _now(),
    }
    refresh_pending_verification(sess)
    with sess.snapshot_lock:
        sess.last_committed_operation = receipt
        recent = list(getattr(sess, "recent_operations", None) or [])
        recent.append(receipt)
        sess.recent_operations = recent[-_RECEIPT_RING:]

    outcome = save_snapshot(sess)
    if not outcome.body_written:
        # 正文没写上 → 回执没落盘 → 标记必须留着。这次操作在崩溃后仍按"结果未知"处理。
        if ui is not None and not outcome.skipped:
            detail = outcome.error if outcome.error is not None else "未知原因"
            try:
                ui.show_message(f"\n⚠️ 工具结果未能保存（{str(detail)[:200]}），"
                                "该操作在下次启动时会显示为结果未知。\n", "tool_result")
            except Exception:
                pass
        return CommitReport(committed=False, save=outcome, marker_cleared=False)

    cleared, clear_error = _clear_operation(sess, operation.session_id, operation.operation_id)
    if not outcome.index_written and ui is not None:
        try:
            ui.show_message("\n⚠️ 会话正文已保存，但侧栏索引更新失败（下次打开会自动补回）。\n",
                            "tool_result")
        except Exception:
            pass
    return CommitReport(committed=True, save=outcome, marker_cleared=cleared,
                        clear_error=clear_error)


def _clear_operation(sess, session_id, operation_id):
    """删掉 sidecar 里的一条记录。失败**不回滚已保存的结果**——结果确实已经存好了。

    残留记录靠回执识别：下次启动时 `classify_inflight` 见到有匹配回执的记录，
    知道它其实已提交，清掉即可。
    """
    with _inflight_lock(sess):
        doc, error = _read_inflight_raw(session_id)
        if doc is None:
            return False, error
        remaining = [op for op in doc["operations"] if op.get("operation_id") != operation_id]
        try:
            if remaining:
                doc["operations"] = remaining
                _write_inflight(session_id, doc)
            else:
                path = inflight_path(session_id)
                if os.path.exists(path):
                    os.remove(path)
        except Exception as remove_error:
            logger.warning(f"清理 inflight 记录失败 {operation_id}: {remove_error}")
            return False, str(remove_error)
    return True, ""


def discard_inflight(session_id):
    """删除会话的整个 sidecar（删会话时用）。sidecar 不进侧栏索引，
    也不能作为复活已删除会话的依据。"""
    path = inflight_path(session_id)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as error:
        logger.warning(f"删除 inflight 文件失败 {session_id}: {error}")


def classify_inflight(session_id, *, last_receipt=None, recent_receipts=None):
    """核对 sidecar 与主 JSON 的完成回执，给每条记录一个诚实的结论。

    判据是 **session_id + run_id + operation_id + 完成回执**，
    不能只看 revision 变大——别的保存同样会推进 revision，拿它当"我这个操作提交了"的
    证据，会把结果未知的操作说成已完成。

    返回 ([{operation, status, detail}], 读取错误)，status ∈
      - `committed`  结果已落盘，只是标记没清（可安全清理）
      - `unknown`    结果未知：可能执行了、可能没执行，不自动重放
    """
    doc, error = _read_inflight_raw(session_id)
    if doc is None:
        return [], error
    if last_receipt is None and recent_receipts is None:
        last_receipt, recent_receipts = _load_receipts_from_disk(session_id)
    receipts = list(recent_receipts or [])
    if last_receipt and not any(r.get("operation_id") == last_receipt.get("operation_id")
                                for r in receipts):
        receipts.append(last_receipt)

    out = []
    for op in doc["operations"]:
        match = None
        for receipt in receipts:
            if (receipt.get("operation_id") == op.get("operation_id")
                    and receipt.get("run_id") == op.get("run_id")
                    and receipt.get("session_id") == op.get("session_id")):
                match = receipt
                break
        if match is not None:
            out.append({"operation": op, "status": "committed",
                        "detail": f"结果已在 revision {match.get('revision', 0)} 落盘，标记未清理。"})
        else:
            out.append({"operation": op, "status": "unknown",
                        "detail": (f"`{op.get('tool')}` 已调度但没有完成回执："
                                   "可能已执行、也可能没执行，请核对现场后再决定，不自动重放。")})
    return out, ""


def _load_receipts_from_disk(session_id):
    path = os.path.join(memory_dir(), f"{session_id}.json")
    if not os.path.exists(path):
        return None, []
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except Exception:
        return None, []
    progress = data.get("progress") if isinstance(data, dict) else None
    if not isinstance(progress, dict):
        return None, []
    return normalize_receipts(progress.get("last_committed_operation"),
                              progress.get("recent_operations"))


def sweep_committed(sess, session_id):
    """清掉 sidecar 里已经有匹配回执的残留记录，返回仍然结果未知的那些。

    这是"删除失败不回滚"的下半场：上次没删掉的记录在这里按回执认出来并清理。
    """
    entries, error = classify_inflight(session_id)
    if error:
        return [], error
    for entry in entries:
        if entry["status"] == "committed":
            _clear_operation(sess, session_id, entry["operation"]["operation_id"])
    return [e for e in entries if e["status"] == "unknown"], ""
