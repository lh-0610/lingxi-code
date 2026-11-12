"""对话历史 JSON 序列化 + 会话管理。

- `_msg_to_dict` / `_dict_to_msg`：LangChain Message ↔ JSON
- `save_session` / `load_session` / `list_sessions` / `delete_session`：会话 CRUD
- `maybe_generate_session_title`：第一轮结束后用 LLM 生成短标题
- `reset_history`：清空当前对话开新会话
- `_build_ai_message`：从 stream 累积块构造 AIMessage（保留 thinking blocks）
"""
import re
import os
import json
import tempfile
import threading
import time
import uuid
from datetime import datetime

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage

from . import state
from .paths import logger, memory_dir, memory_index
from .roles import get_system_prompt
from .content_blocks import is_text_block, is_think_block, block_text, content_text


# 串行化 chat_memory/ 下所有文件的读-改-写。
# 用 RLock 是因为同一线程内 save_session() 已经持锁还会再调 _update_index()，
# 普通 Lock 会自死锁。
_LOCK = threading.RLock()


# 会话 JSON 的格式版本。progress 是可选信封，旧文件没有它照常读。
_SCHEMA_VERSION = 2
_PROGRESS_VERSION = 1

# 索引修复登记：正文写成功但索引写失败时记在这里，下次读盘时**只**修这些 id。
# 不能改成"扫描目录里所有孤儿 JSON 就重建索引"——那会把用户主动删掉的会话复活，
# 而删除恰恰是最不该被撤销的操作。
_INDEX_REPAIR_NAME = "index_repair.json"


def _index_repair_file():
    return os.path.join(memory_dir(), _INDEX_REPAIR_NAME)


class SessionUnreadableError(Exception):
    """已有的会话文件读不出来，因此**无法确认**覆盖它是否安全。

    读失败和"文件不存在"必须分开：当成新文件直接写，会把读不到的那份连同它的格式版本
    和未知字段一起抹掉——实测 schema_version=99 被改写成 2。磁盘暂时故障、文件被占用、
    JSON 损坏都会走到这里，而这几种情况下"保守地不动它"都比"覆盖"正确。
    """
    def __init__(self, path, detail):
        self.path = path
        self.detail = detail
        super().__init__(f"无法读取已有会话文件 {path}（{detail}），已中止保存以免覆盖未知内容。")


class SessionFormatTooNewError(Exception):
    """磁盘上的会话格式版本高于本程序能理解的版本。

    这时**拒绝覆盖**：按当前字段重建整个字典会把不认识的部分悄悄丢掉，
    而用户多半只是临时退回了旧版本，数据还要拿回去用。宁可存不上并明确报错。
    """
    def __init__(self, session_id, found, supported):
        self.session_id = session_id
        self.found = found
        self.supported = supported
        super().__init__(
            f"会话 {session_id} 的格式版本 {found} 高于本程序支持的 {supported}，"
            "已拒绝覆盖以免丢失新版本字段。请升级后再打开该会话。"
        )


_VALID_STEP_STATUS = ("pending", "in_progress", "done")


def _new_input_queue():
    from . import input_queue as _iq
    return _iq.new_queue()


def _normalize_progress(raw, session_id=""):
    """`_normalize_progress_checked` 的外壳：**永不抛异常**。

    两个调用点都不能被它打断：加载时抛出去，整段聊天就打不开；保存前核对旧文件时抛出去，
    这个会话之后的每次保存都会失败。各字段的校验应当自己兜住（锚点就是这样降级的），
    这里是最后一道：意外异常一律按"进度读不懂"处理——保存路径据此把原进度隔离留底，
    加载路径据此只作废进度、照常恢复聊天。
    """
    try:
        return _normalize_progress_checked(raw, session_id)
    except Exception as error:
        logger.warning(f"会话 {session_id} 的进度解析异常：{error}", exc_info=True)
        from . import run_records as _rr
        empty = {"current_plan": [], "task_ledger": state.new_task_ledger(), "revision": 0,
                 "task": None, "archived_tasks": [], "last_task_switch": None, "task_switch_version": 0,
                 "last_run": None, "pending_verification": _rr.empty_pending_verification(),
                 "last_committed_operation": None, "recent_operations": [],
                 "input_queue": _new_input_queue()}
        return empty, f"进度无法解析（{type(error).__name__}: {str(error)[:120]}）"


def _normalize_progress_checked(raw, session_id=""):
    """把磁盘上的 progress 归一成可用结构，返回 (progress_dict, error_reason)。

    容错原则：**进度坏掉不能拖垮聊天历史**。任何字段不合法就整块作废、返回空进度
    并说明原因，由调用方决定怎么告诉用户；绝不把半信半疑的数据当成真进度用下去。
    也绝不"尽量抢救"——抢救出来的计划看起来正常，用户无从分辨它是不是真的。

    唯一的例外是完成回执（`recent_operations`）：它是**附加证据**，坏掉一条只会让
    对应操作退回"结果未知"（保守方向）；整块作废反而会让所有已提交操作一起退回未知。
    """
    from . import run_records as _rr
    from . import task_state as _ts

    empty = {"current_plan": [], "task_ledger": state.new_task_ledger(), "revision": 0,
             "task": None, "archived_tasks": [], "last_task_switch": None,
             "task_switch_version": 0,
             "last_run": None, "pending_verification": _rr.empty_pending_verification(),
             "last_committed_operation": None, "recent_operations": [],
             "input_queue": _new_input_queue()}
    if raw is None:
        return empty, ""
    if not isinstance(raw, dict):
        return empty, "progress 不是对象"

    ver = raw.get("version")
    if ver is not None and (not isinstance(ver, int) or ver > _PROGRESS_VERSION):
        return empty, f"progress 版本 {ver!r} 无法识别"

    # ── B09b 输入队列：损坏/版本不认识整块作废，原始数据由调用方隔离留底 ──
    from . import input_queue as _iq
    queue, queue_why = _iq.normalize(raw.get("input_queue"))
    if queue_why:
        return empty, f"input_queue {queue_why}"

    plan_raw = raw.get("current_plan", [])
    if not isinstance(plan_raw, list):
        return empty, "current_plan 不是列表"
    plan = []
    for item in plan_raw:
        if not isinstance(item, dict):
            return empty, "计划步骤不是对象"
        text = item.get("text")
        status = item.get("status")
        # 枚举必须严格比对：用 truthy 判断会把 "yes" / 1 / "完成了" 都当成 done，
        # 于是恢复出来的进度比实际乐观——这正是最不能出错的方向。
        if not isinstance(text, str) or status not in _VALID_STEP_STATUS:
            return empty, "计划步骤字段非法"
        plan.append({"text": text, "status": status})

    ledger_raw = raw.get("task_ledger", None)
    ledger = state.new_task_ledger()
    if ledger_raw is not None:
        if not isinstance(ledger_raw, dict):
            return empty, "task_ledger 不是对象"
        files = ledger_raw.get("files", {})
        cmds = ledger_raw.get("commands", [])
        if not isinstance(files, dict) or not isinstance(cmds, list):
            return empty, "task_ledger 结构非法"
        # 逐项严格校验，**不做 str() 转换、不静默丢弃非法条目**。
        # 早先那样写的后果是：嵌套对象被转成 "{'invalid': 'nested'}" 这种字符串塞进台账、
        # 非法命令条目被悄悄过滤，而 progress_error 是空的——损坏被抹平成"看起来正常"，
        # 恰恰违背本函数"结构非法就整块作废并说明原因"的约定。
        for k, v in files.items():
            if not isinstance(k, str) or not isinstance(v, str):
                return empty, "task_ledger.files 含非字符串键或值"
        for c in cmds:
            if not isinstance(c, dict):
                return empty, "task_ledger.commands 含非对象条目"
            if not isinstance(c.get("cmd", ""), str) or not isinstance(c.get("brief", ""), str):
                return empty, "task_ledger.commands 条目字段类型非法"
        ledger["files"] = dict(files)
        ledger["commands"] = [{"cmd": c.get("cmd", ""), "brief": c.get("brief", "")}
                              for c in cmds]

    rev = raw.get("revision", 0)
    if not isinstance(rev, int) or rev < 0:
        rev = 0

    # ── B05 任务身份、来源与切换 ──
    task, why = _ts.normalize_task(raw.get("task"), session_id)
    if why:
        return empty, why
    archived_tasks, why = _ts.normalize_archived_tasks(raw.get("archived_tasks"), session_id)
    if why:
        return empty, why
    last_switch, why = _ts.normalize_task_switch(raw.get("last_task_switch"), session_id)
    if why:
        return empty, why
    task_switch_ver = raw.get("task_switch_version", 0)
    if not isinstance(task_switch_ver, int) or task_switch_ver < 0:
        task_switch_ver = 0

    # ── B03 的运行记录与待验证义务 ──
    last_run, why = _rr.normalize_last_run(raw.get("last_run"))
    if why:
        return empty, why
    pending, why = _rr.normalize_pending_verification(raw.get("pending_verification"))
    if why:
        return empty, why
    last_op, recent_ops = _rr.normalize_receipts(raw.get("last_committed_operation"),
                                                 raw.get("recent_operations"))

    return {"current_plan": plan, "task_ledger": ledger, "revision": rev,
            "task": task, "archived_tasks": archived_tasks,
            "last_task_switch": last_switch, "task_switch_version": task_switch_ver,
            "last_run": last_run, "pending_verification": pending,
            "last_committed_operation": last_op, "recent_operations": recent_ops,
            "input_queue": queue}, ""



def _snapshot_progress(sess):
    """在快照锁内一次取走整份进度的**深拷贝**。

    深拷贝是必须的：直接引用会让两个会话（或存盘副本与运行态）共享同一个 list/dict，
    之后一边改另一边跟着变，表现为"另一个会话的计划莫名其妙动了"。

    计划、台账、运行记录、待验证义务、完成回执必须在**同一个临界区**里取：
    分几次读会拍到"工具结果已记进台账、但对应回执还没写上"这种半截状态，
    而恢复时正是靠回执与 inflight 配对来判断操作提交没提交的。
    """
    from . import run_records as _rr

    with sess.snapshot_lock:
        plan = [dict(it) for it in (getattr(sess, "current_plan", None) or [])]
        ledger_src = getattr(sess, "task_ledger", None) or state.new_task_ledger()
        ledger = {
            "files": dict(ledger_src.get("files") or {}),
            "commands": [dict(c) for c in (ledger_src.get("commands") or [])],
        }
        rev = int(getattr(sess, "progress_revision", 0) or 0)
        last_run = getattr(sess, "last_run", None)
        last_run = json.loads(json.dumps(last_run)) if isinstance(last_run, dict) else None
        pending = getattr(sess, "pending_verification", None)
        if isinstance(pending, dict):
            pending = {"files": list(pending.get("files") or []),
                       "code_files": list(pending.get("code_files") or []),
                       "tracking_incomplete": dict(pending.get("tracking_incomplete") or {}),
                       # 改动文件的实际位置：放行时要求检查覆盖到这里（B04 复核）
                       "file_paths": dict(pending.get("file_paths") or {}),
                       "reason": pending.get("reason") or "",
                       "run_id": pending.get("run_id") or ""}
        else:
            pending = _rr.empty_pending_verification()
        last_op = getattr(sess, "last_committed_operation", None)
        last_op = dict(last_op) if isinstance(last_op, dict) else None
        recent = [dict(r) for r in (getattr(sess, "recent_operations", None) or [])
                  if isinstance(r, dict)]
        task = getattr(sess, "current_task", None)
        task = json.loads(json.dumps(task)) if isinstance(task, dict) else None
        archived_tasks = [json.loads(json.dumps(t)) for t in (getattr(sess, "archived_tasks", None) or [])
                          if isinstance(t, dict)]
        last_switch = getattr(sess, "last_task_switch", None)
        last_switch = json.loads(json.dumps(last_switch)) if isinstance(last_switch, dict) else None
        task_switch_ver = int(getattr(sess, "task_switch_version", 0) or 0)
        # ── B09b 输入队列：与历史/运行记录同一临界区取走，队列-接纳关联才是同一快照 ──
        from . import input_queue as _iq
        queue_snap = _iq.snapshot(getattr(sess, "input_queue", None) or _iq.new_queue())
    return {"plan": plan, "ledger": ledger, "revision": rev,
            "task": task, "archived_tasks": archived_tasks,
            "last_task_switch": last_switch, "task_switch_version": task_switch_ver,
            "last_run": last_run, "pending_verification": pending,
            "last_committed_operation": last_op, "recent_operations": recent,
            "input_queue": queue_snap}


def _json_fingerprint(value):
    """保留 JSON 类型差异的内容指纹，用于比对两份数据是否真的相同。

    不能直接用 `==`：Python 里 `1 == True`、`0 == False`，于是 status 为数字 1 的进度
    和 status 为布尔 true 的进度会被判成同一份——第二份隔离被跳过，数据无声丢失。
    json.dumps 会把它们分别写成 `1` 和 `true`，类型差异得以保留；sort_keys 抹平键序，
    免得内容相同只是顺序不同的两份被当成不同的。
    """
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=repr)
    except Exception:
        # 理论上进不来（隔离的数据都来自 JSON），兜底也要保住类型区分：repr(1) != repr(True)
        return repr(value)


def _append_quarantine(existing_entries, progress, reason):
    """把一份读不懂的进度追加进隔离清单，按内容去重，返回新的清单。

    用 list 而不是单个对象：同一个会话可能先后出现多份读不懂的进度（升级 → 退回 →
    再升级 → 再退回）。只留一份的话，第二份在保存时被直接覆盖成空进度，而且悄无声息。
    去重按原始内容比对——反复保存同一份不该攒出一堆一模一样的副本。
    """
    entries = []
    if isinstance(existing_entries, list):
        entries = [e for e in existing_entries if isinstance(e, dict)]
    elif isinstance(existing_entries, dict):
        entries = [existing_entries]      # 兼容更早的单对象形态
    fingerprint = _json_fingerprint(progress)
    for e in entries:
        if _json_fingerprint(e.get("data")) == fingerprint:
            return entries                # 这份已经留过底了
    entries.append({
        "reason": reason,
        "quarantined_at": datetime.now().isoformat(),
        "data": progress,
    })
    return entries


def _read_existing_session(path, session_id=""):
    """读旧文件，用于保留未知字段并检查格式版本。

    **存在但读不出来时抛 SessionUnreadableError 中止保存**，不能当成新文件写——
    版本保护正是靠这一步生效的，绕过它就会把读不到的内容连同格式版本一起抹掉。
    """
    if not os.path.exists(path):
        return {}          # 真的是新文件，放心写
    try:
        with open(path, "r", encoding="utf-8") as f:
            old = json.load(f)
    except Exception as e:
        # 存在但读不出来 → 中止。不能退化成"当新文件写"，那会把读不到的内容连同
        # 它的格式版本一起抹掉，而版本保护正是靠读这一步生效的。
        raise SessionUnreadableError(path, str(e)) from e
    if not isinstance(old, dict):
        raise SessionUnreadableError(path, "文件内容不是 JSON 对象")
    found = old.get("schema_version")
    if isinstance(found, int) and found > _SCHEMA_VERSION:
        raise SessionFormatTooNewError(session_id or old.get("id", "?"), found, _SCHEMA_VERSION)
    return old


def _record_index_repair(entry):
    """登记一条"正文已存盘但索引没更新"的待修记录（尽力而为，失败只记日志）。"""
    try:
        pending = []
        path = _index_repair_file()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("pending"), list):
                pending = [p for p in data["pending"]
                           if isinstance(p, dict) and p.get("id") != entry["id"]]
        pending.append(entry)
        _atomic_write_json(path, {"pending": pending})
    except Exception as e:
        logger.warning(f"登记索引待修记录失败 {entry.get('id')}: {e}")


def _drop_index_repair(session_id):
    """销掉某会话的索引待修登记（删除会话时调用）。"""
    path = _index_repair_file()
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        pending = [p for p in (data.get("pending") or [])
                   if isinstance(p, dict) and p.get("id") != session_id]
        if pending:
            _atomic_write_json(path, {"pending": pending})
        else:
            os.remove(path)
    except Exception as e:
        logger.warning(f"销掉索引待修记录失败 {session_id}: {e}")


def _clear_progress(sess):
    """新会话从零开始：计划、台账、修订号、运行记录、待验证义务、回执一起清。

    漏清 revision 会让新会话继承旧会话的修订号，后续批次拿它判断"结果是否已落盘"
    就会对错门。漏清 last_run / 回执同理——新会话会带着上一个会话的运行身份，
    恢复时把别人的中断记在自己头上。

    待验证义务在这里**清掉**是对的：`reset_history` 是"开一个新会话"，不是"继续同一任务"；
    旧会话的义务留在它自己的文件里，跟着那个会话走。
    """
    from . import run_records as _rr

    with sess.snapshot_lock:
        sess.current_plan = []
        sess.task_ledger = state.new_task_ledger()
        sess.progress_revision = 0
        sess.progress_error = ""
        sess.last_run = None
        sess.pending_verification = _rr.empty_pending_verification()
        sess.last_committed_operation = None
        sess.recent_operations = []
        sess.active_run_id = None
        # 结果卡的快照缓存也要清：重绘优先读它，不清的话"新建对话"之后
        # 旧那一轮的结果卡会重新画出来，而 last_run 明明已经空了。
        sess.last_result = None
        sess.current_task = None
        sess.archived_tasks = []
        sess.last_task_switch = None
        # B09b：新会话不继承旧会话的待发队列（reset_history = 开新对话）。
        from . import input_queue as _iq
        sess.input_queue = _iq.new_queue()
        sess.task_switch_version = 0


def _repair_pending_index_entries():
    """把登记过的待修索引项补回 index.json。只修登记过的 id。

    调用方需已持 _LOCK。没有登记文件时是一次 os.path.exists，开销可忽略。
    """
    path = _index_repair_file()
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        pending = data.get("pending") if isinstance(data, dict) else None
        if not isinstance(pending, list):
            pending = []
    except Exception as e:
        logger.warning(f"读取索引待修记录失败: {e}")
        return 0

    still = []
    fixed = 0
    for item in pending:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        sid = item["id"]
        body_path = os.path.join(memory_dir(), f"{sid}.json")
        # 会话文件已不在 → 用户后来删了它，**不要复活**，直接销案
        if not os.path.exists(body_path):
            continue
        # **以当前正文为准**，不用登记时的快照。登记里的标题/项目可能已经过期——
        # 实测过：登记后用户改了标题和项目并成功保存，刷新列表时旧登记把索引改了回去，
        # 等于用一条陈旧记录回滚了更新的数据。正文才是唯一真相。
        try:
            with open(body_path, "r", encoding="utf-8") as f:
                body = json.load(f)
            if not isinstance(body, dict):
                raise ValueError("正文不是 JSON 对象")
        except Exception as e:
            logger.warning(f"补索引项时读不了正文 {sid}: {e}")
            still.append({"id": sid, "recorded": item.get("recorded", "")})
            continue
        try:
            _update_index(sid, body.get("title") or "新对话", body.get("project"),
                          session_kind=body.get("session_kind", "code"),
                          rag_kb_dir=body.get("rag_kb_dir", "") or "")
            fixed += 1
            logger.info(f"已补回缺失的索引项: {sid}")
        except Exception as e:
            logger.warning(f"补索引项失败 {sid}: {e}")
            still.append({"id": sid, "recorded": item.get("recorded", "")})
    try:
        if still:
            _atomic_write_json(path, {"pending": still})
        elif os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning(f"更新索引待修记录失败: {e}")
    return fixed


class SessionMigrationError(Exception):
    """move_sessions_to_no_project 在【部分】会话文件改写失败时抛出。

    迁移已尽力完成（index + 内存锚点 + 能写的文件都已改），failed_ids 是没能落盘的会话——
    它们的内存锚点已置 None、会在下次 save 自愈；caller 可据此提示用户而非静默吞掉。
    """
    def __init__(self, moved, failed_ids):
        self.moved = moved
        self.failed_ids = list(failed_ids)
        super().__init__(f"{len(self.failed_ids)} 个会话文件改写失败: {self.failed_ids}")


# 替换阶段撞上"目标文件被别人打开"时的有界重试（见 _atomic_write_json 内注释）。
# 总等待上限 ~80ms：足够躲过一次读句柄/扫描，又不会让保存明显卡顿。
_REPLACE_RETRIES = 5
_REPLACE_RETRY_DELAY = 0.02


def _discard_temp(tmp, fd=None):
    """失败路径上的尽力清理：关掉还归我们的 fd、删掉临时文件。

    清理本身的异常一律吞掉——残留一个临时文件是小事，把"为什么没存上"的真实原因
    （磁盘满 / 权限 / IO 错）换成"删不掉临时文件"才是排障灾难。
    不吞 BaseException：KeyboardInterrupt 仍要传出去。

    fd=None 表示所有权已转给文件对象，由它负责关闭，这里只清文件。
    """
    if fd is not None:
        try:
            os.close(fd)
        except Exception:
            pass
    try:
        os.unlink(tmp)
    except Exception:
        pass


def _atomic_write_json(path, data, *, ensure_ascii=False, indent=2):
    """本模块所有 chat_memory/*.json 写入的唯一出口：同目录临时文件 → os.replace。

    原来每处都是 `open(path, "w")`：打开即把目标文件截断成 0 字节，序列化/写入中途出错
    或进程在这期间退出，磁盘上留下的是半截 JSON——下次 `json.load` 直接抛，整段会话history
    或整个 index.json 就此读不回来。改成"写别处、再原子改名"后，目标文件在任何时刻要么是
    完整的旧内容、要么是完整的新内容。

    **边界（不要过度承诺）**：
    - 只保证**单个文件**的替换原子性，**不是**跨文件事务。会话正文与 index.json 仍是两次
      独立替换，中间崩溃会留下"正文已更新、索引未更新"的不一致（可修复，属后续批次）。
    - fsync 覆盖进程被杀 / 应用崩溃这类常见情形；不同硬件与文件系统对断电的保证不同，
      **不声称绝对断电无损**。
    """
    # 先整体序列化成字符串再落盘：不可序列化的对象在这一步就抛，磁盘上连临时文件都不会
    # 创建，旧文件原样保留。（若沿用 json.dump(obj, f) 边算边写，失败时还要清理半截临时文件。）
    text = json.dumps(data, ensure_ascii=ensure_ascii, indent=indent)

    # 临时文件必须与目标**同目录**：os.replace 要求源和目标在同一文件系统，跨文件系统会
    # 直接抛 OSError（POSIX 是 EXDEV；Windows 的 MoveFileEx 未带 MOVEFILE_COPY_ALLOWED
    # 同样失败）——**不会**自动退化成拷贝，所以"放同目录"是硬性前提而非优化。
    # mkstemp 独占创建且名字唯一——固定的 "<name>.tmp" 会让并发写同一文件的两个线程互相
    # 截断（本模块虽有 _LOCK，但辅助函数不该依赖调用方持锁这个隐含前提）。
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory,
                               prefix=os.path.basename(path) + ".", suffix=".tmp")

    # fd 所有权：**closefd=False** 让文件对象只负责缓冲与编码、永不关闭 fd，所有权自始至终
    # 留在本函数手里，退出路径统一关一次。
    #
    # 不这么做的话所有权是含糊的：io.open 内部先构造 FileIO（此刻已接管 fd），再构造缓冲层、
    # 文本层；后两步失败时它会把 FileIO 连同 fd 一起关掉，然后才抛。调用方看到的只是
    # "fdopen 抛了异常"，**无从判断 fd 还在不在**——若按"没接管"再 close 一次，而这个 fd 号
    # 此刻已被别的线程复用，关掉的就是一个无关文件（比句柄泄漏更难查，症状是别处莫名其妙
    # 读写失败）。closefd=False 把这个歧义从根上消除：除了我们，没人会关它。
    fd_open = True
    try:
        # 不传 newline=""：保持与原来 open(path, "w", encoding="utf-8") 相同的换行翻译，
        # 免得这次重构顺带把已落盘文件的 CRLF 悄悄改成 LF（格式选项要求原样保留）。
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # closefd=False 下 with 只关文件对象，fd 还开着——必须由我们关掉**再**替换：
        # Windows 上 os.replace 覆盖自己仍打开着的文件会 PermissionError。
        #
        # 责任要在**调用之前**交出去，不能等 close 返回后再置标志：按 PEP 475，close()
        # 报错时 fd **可能已经被释放**（各平台行为不一致，这正是它被定为不可重试的原因），
        # 所以"close 报错"推不出"fd 还开着"。若把置位留在后面，close 抛错时 fd_open 仍为
        # True，异常清理就会对一个可能已释放的号再关一次——那一瞬它可能已被别的线程复用，
        # 关掉的是无关文件。
        fd_open = False
        os.close(fd)
        # 不止"自己没关"：目标文件被别的进程打开时，能否替换取决于对方开文件时用的
        # **共享模式**——只有带 FILE_SHARE_DELETE 打开的句柄才允许替换。Python 自带的
        # open() 不带这一位，所以另一个灵犀实例、编辑器或杀毒软件正在读 index.json 时，
        # replace 就会失败。这类占用通常是毫秒级的，一次失败就放弃等于"用户这一轮白说了"。
        # 故做有界重试；重试用尽仍失败就原样抛出，不吞错、不退化成非原子的直接覆盖。
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == _REPLACE_RETRIES - 1:
                    raise
                time.sleep(_REPLACE_RETRY_DELAY)
    except BaseException:
        # 走到这里 replace 必然没成功（成功后 try 里已无可抛之处），临时文件一律删。
        # fd 只在还没关时才关——正常路径已经关过，重复关同样有误关复用号的风险。
        _discard_temp(tmp, fd=fd if fd_open else None)
        raise


def _ensure_memory_dir():
    with _LOCK:
        os.makedirs(memory_dir(), exist_ok=True)
        if not os.path.exists(memory_index()):
            # 保留原 json.dump([], f) 的默认格式选项（ensure_ascii=True、无缩进）
            _atomic_write_json(memory_index(), [], ensure_ascii=True, indent=None)


def _msg_to_dict(msg):
    if msg is None:
        return {"type": "Unknown", "content": ""}
    d = {"type": msg.__class__.__name__}
    # content 可能是 str 或 list（含 thinking blocks 等），直接保留原结构
    d["content"] = msg.content or ""

    # 稳定消息 ID（B05）：真实用户消息与内部消息均分配 stable ID，存盘重载保持一致。
    # 约定：AI / Tool 消息的 id 多半是服务商给的（如 run-xxx），照样原样保存——它只是
    # 消息身份，不是来源凭据。能否算"用户要求"只看类型 + lingxi_kind，不看 id。
    from . import task_state as _ts
    mid = getattr(msg, "id", None)
    kwargs = getattr(msg, "additional_kwargs", None) or {}
    if not mid and kwargs.get("lingxi_message_id"):
        mid = kwargs.get("lingxi_message_id")
    if isinstance(mid, str) and mid:
        d["id"] = mid
        d["lingxi_message_id"] = mid

    if isinstance(msg, AIMessage) and msg.tool_calls:
        d["tool_calls"] = msg.tool_calls
    if isinstance(msg, AIMessage):
        ak = getattr(msg, 'additional_kwargs', None) or {}
        if ak.get('reasoning_content'):
            d["reasoning_content"] = ak['reasoning_content']
        model = ak.get("lingxi_model")
        if isinstance(model, str) and model:
            d["lingxi_model"] = model[:80]
    if isinstance(msg, ToolMessage):
        d["tool_call_id"] = msg.tool_call_id

    # 元数据白名单（B05）：只持久化允许的键名，防止任意 kwargs 原样保存或冒充授权
    if kwargs.get("lingxi_internal") is True:
        d["lingxi_internal"] = True
        kind = kwargs.get("lingxi_kind")
        if isinstance(kind, str) and kind in _ts.VALID_MESSAGE_KINDS:
            d["lingxi_kind"] = kind
        recovery = kwargs.get("lingxi_recovery")
        if isinstance(recovery, dict):
            d["lingxi_recovery"] = recovery
    else:
        kind = kwargs.get("lingxi_kind")
        if isinstance(kind, str) and kind in _ts.VALID_MESSAGE_KINDS:
            d["lingxi_kind"] = kind
    return d


def _dict_to_msg(d):
    from . import task_state as _ts
    t = d["type"]
    internal = {}
    mid = d.get("id") or d.get("lingxi_message_id") or ""
    if isinstance(mid, str) and mid:
        internal["lingxi_message_id"] = mid

    if d.get("lingxi_internal") is True:
        internal["lingxi_internal"] = True
        kind = d.get("lingxi_kind")
        if isinstance(kind, str) and kind in _ts.VALID_MESSAGE_KINDS:
            internal["lingxi_kind"] = kind
        recovery = d.get("lingxi_recovery")
        if isinstance(recovery, dict):
            internal["lingxi_recovery"] = recovery
    else:
        kind = d.get("lingxi_kind")
        if isinstance(kind, str) and kind in _ts.VALID_MESSAGE_KINDS:
            internal["lingxi_kind"] = kind
        elif t == "HumanMessage":
            # 旧版消息无来源元数据时，如实标为 legacy_unknown，不追认为用户原话（B05）
            internal["lingxi_kind"] = "legacy_unknown"

    msg = None
    if t == "SystemMessage":
        msg = SystemMessage(content=d["content"])
    elif t == "HumanMessage":
        msg = HumanMessage(content=d["content"], additional_kwargs=internal)
    elif t == "AIMessage":
        ak = dict(internal)
        if "reasoning_content" in d:
            ak["reasoning_content"] = d["reasoning_content"]
        if isinstance(d.get("lingxi_model"), str) and d["lingxi_model"]:
            ak["lingxi_model"] = d["lingxi_model"][:80]
        msg = AIMessage(
            content=d["content"],
            tool_calls=d.get("tool_calls", []),
            additional_kwargs=ak,
        )
    elif t == "ToolMessage":
        msg = ToolMessage(content=d["content"], tool_call_id=d.get("tool_call_id", ""),
                          additional_kwargs=internal)
    else:
        msg = HumanMessage(content=d["content"], additional_kwargs=internal)

    if mid and isinstance(mid, str):
        msg.id = mid
    return msg


def _build_ai_message(gathered, clean_text, tool_calls):
    """从 gathered AIMessageChunk 构造写入 chat_history 的 AIMessage。
    保留 thinking content blocks 和 reasoning_content，让下一轮 API 调用
    能把它们回传给服务端（MiMo / DeepSeek 等要求回传 thinking 上下文）。
    """
    ak = dict(getattr(gathered, 'additional_kwargs', {}) or {}) if gathered else {}
    # 记下是哪个模型写的：重绘历史时回复上方能标出模型名。只做展示，不参与任何判断。
    try:
        from .models import MODEL_LIST
        ak["lingxi_model"] = str(MODEL_LIST[state.current_model_index][0])[:80]
    except Exception:
        pass

    # content blocks：保留 thinking 块 + 去掉空块
    content_blocks = []
    if gathered is not None and isinstance(gathered.content, list):
        for block in gathered.content:
            if not isinstance(block, dict):
                continue
            # 思考块整块保留：Anthropic thinking 带 signature、Responses reasoning 带 id，
            # 都要原样回传给服务端（尤其工具调用回合缺 reasoning item 会 400），故不看是否有可见文本。
            if is_think_block(block):
                content_blocks.append(block)
            elif is_text_block(block) and block.get('text'):
                content_blocks.append(block)

    if content_blocks:
        # 有 list 形式的 content blocks（Anthropic 协议），直接用
        return AIMessage(
            content=content_blocks,
            tool_calls=tool_calls or [],
            additional_kwargs=ak,
        )
    else:
        return AIMessage(
            content=clean_text,
            tool_calls=tool_calls or [],
            additional_kwargs=ak,
        )


def _evicted_ids_for_save(sess, chat_history) -> list:
    """要落盘的"已回收工具结果"清单：只留历史里还在的（消息被删 / 重新生成后，对应的记录没用了）。"""
    alive = {m.tool_call_id for m in list(chat_history) if isinstance(m, ToolMessage)}
    return [i for i in list(getattr(sess, "evicted_tool_results", None) or []) if i in alive]


def _evicted_ids_from_data(data) -> list:
    """从会话文件读"已回收工具结果"清单。旧会话没有这个键 → 空；手改坏了的项丢掉。"""
    raw = data.get("evicted_tool_results")
    return [i for i in raw if isinstance(i, str) and i] if isinstance(raw, list) else []


def save_session(*, session=None):
    """保存当前会话到本地文件（追加/更新）。失败抛异常（行为与本批之前一致）。

    session=None → 从 state 代理读（兼容旧调用）；
    session=<Session> → 直接从该 Session 对象读（用于保存后台会话）。
    """
    outcome = save_session_report(session=session)
    if outcome.error is not None:
        raise outcome.error


def save_session_report(*, session=None):
    """同 `save_session`，但把**分步结果**作为 `SaveOutcome` 返回而不是抛异常。

    存在的理由：调用方需要区分"正文写没写成功"和"这次保存整体成不成功"。
    正文写成功而索引写失败时，完成回执其实已经落盘、inflight 标记可以清；
    只给一个成功布尔值的话，这两件事会被混成一件，于是要么误删唯一线索、
    要么把已经存好的结果说成未保存。
    """
    from .run_records import SaveOutcome

    started = time.perf_counter()
    # 快照与落盘必须在同一临界区，否则先取的旧快照会覆盖 worker 的最终回复。
    with _LOCK:
        try:
            return _save_session_locked(session=session, started=started)
        except Exception as error:
            return SaveOutcome(error=error,
                               elapsed_ms=round((time.perf_counter() - started) * 1000, 2))


def _save_session_locked(*, session=None, started=None):
    from .run_records import SaveOutcome

    if started is None:
        started = time.perf_counter()

    def _elapsed():
        return round((time.perf_counter() - started) * 1000, 2)

    _ensure_memory_dir()

    from . import session as _session_mod
    # 要保存的会话对象：None 模式经代理拿当前线程的会话（主线程=active / worker=它的会话）
    sess = _session_mod.current_session() if session is None else session

    # 子 Agent 是临时会话：不落盘、不进侧栏历史（其改动经 worktree 合并回主项目即可）
    if getattr(sess, "is_subagent", False):
        return SaveOutcome(skipped="子 Agent 会话不落盘", elapsed_ms=_elapsed())

    if session is None:
        chat_history = state.chat_history
        current_session_id = state.current_session_id
        current_session_title = state.current_session_title
    else:
        chat_history = session.chat_history
        current_session_id = session.current_session_id
        current_session_title = session.current_session_title

    if len(chat_history) <= 1:
        return SaveOutcome(skipped="历史过短，尚无可保存内容", elapsed_ms=_elapsed())

    is_first_save = not current_session_id
    if is_first_save:
        current_session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        if session is None:
            state.current_session_id = current_session_id
        else:
            session.current_session_id = current_session_id

    # project tag 取**会话自己锚定的归属**，不取全局 current_project：会话**首次落盘**
    # （拿到 id 那一刻）锚定为当时的全局项目，之后即使切了项目也不变。修"无项目会话被
    # 切项目后误归到新项目"——根因是 worker 的 save 可能晚于主线程 set_current，取全局
    # 就被打上新 tag。（兜底：已有 id 但 project 未锚定，如异常路径，也用当时全局。）
    # 会话分类（持久化）+ rag 目录锚点。rag 会话与代码项目彻底解耦：project 恒为 None。
    session_kind = getattr(sess, "session_kind", "code")
    if session_kind not in ("code", "rag"):
        logger.warning(f"未知 session_kind={session_kind!r}，按 code 处理")
        session_kind = "code"
        sess.session_kind = "code"
    rag_kb_dir = getattr(sess, "rag_kb_dir", "") or ""
    agent_mode = getattr(sess, "agent_mode", "act")
    if agent_mode not in ("plan", "act"):
        agent_mode = "act"
    # "跳过权限确认"不落盘（存成手动）：重开程序后不该有会话悄悄停在"什么都不问"的状态
    from .permission_modes import normalize as _normalize_permission
    permission_mode = _normalize_permission(getattr(sess, "permission_mode", "manual"))
    if permission_mode == "bypass":
        permission_mode = "manual"
    # 思考强度跟着会话走：重开/"切回"时恢复用户当时选的档位。手改坏的值收成默认档。
    from .reasoning import normalize as _normalize_effort
    reasoning_effort = _normalize_effort(getattr(sess, "reasoning_effort", None))

    if session_kind == "rag":
        sess.project = None            # 清理可能残留的 project；rag 不锚代码项目
        current_project = None
    else:
        if is_first_save or getattr(sess, "project", _session_mod._UNSET) is _session_mod._UNSET:
            sess.project = state.current_project
        current_project = sess.project

    title = current_session_title or "新对话"
    for msg in chat_history:
        if isinstance(msg, HumanMessage):
            c = msg.content
            if isinstance(c, list):
                texts = [p["text"] for p in c if isinstance(p, dict) and p.get("type") == "text"]
                c = texts[0] if texts else "[图片]"
            if not current_session_title:
                title = c[:30].replace("\n", " ")
            break

    session_file = os.path.join(memory_dir(), f"{current_session_id}.json")
    # 格式版本高于本程序 → 抛 SessionFormatTooNewError，不覆盖（见该异常的说明）
    existing = _read_existing_session(session_file, current_session_id)

    # 进度（计划 / 台账 / 运行记录 / 待验证义务 / 完成回执）在快照锁内一次取走并深拷贝：
    # 它们由工具线程持续改动，分几次读可能拍到"计划已更新、台账还没更新"的半截状态。
    snap = _snapshot_progress(sess)
    rev = snap["revision"] + 1
    # 完成回执要带上**本次**保存的 revision：回执的意义是"这条结果在哪一版落的盘"，
    # 内存里写它的时候还不知道 revision（由这里 +1 得出），所以在序列化这一刻补齐。
    last_op = dict(snap["last_committed_operation"]) if snap["last_committed_operation"] else None
    recent_ops = [dict(r) for r in snap["recent_operations"]]
    if last_op is not None and not last_op.get("revision"):
        last_op["revision"] = rev
        for entry in recent_ops:
            if entry.get("operation_id") == last_op.get("operation_id"):
                entry["revision"] = rev

    data = {
        "id": current_session_id,
        "title": title,
        "updated": datetime.now().isoformat(),
        "project": current_project,
        "session_kind": session_kind,
        "rag_kb_dir": rag_kb_dir,
        # Plan/Act 跟着会话走：重开时恢复原模式，别让"正在规划"的会话静默变回可动手
        "agent_mode": agent_mode,
        "permission_mode": permission_mode,
        # 思考强度（off/low/high/max）。旧程序读不懂这个键会原样带过去（下面的未知键透传）。
        "reasoning_effort": reasoning_effort,
        # 已折成存根的工具结果：重开后照样折，发给模型的前缀和关掉之前一样（不然缓存整段重建）
        "evicted_tool_results": _evicted_ids_for_save(sess, chat_history),
        "schema_version": _SCHEMA_VERSION,
        "progress": {
            "version": _PROGRESS_VERSION,
            "revision": rev,
            "current_plan": snap["plan"],
            "task_ledger": snap["ledger"],
            # ── B05 任务身份、来源与切换 ──
            "task": snap["task"],
            "archived_tasks": snap["archived_tasks"],
            "last_task_switch": snap["last_task_switch"],
            "task_switch_version": snap["task_switch_version"],
            # 本轮运行记录：phase 说的是"程序有没有收到本轮结果"，outcome 是实际返回的
            # AgentResult。重开时看到 phase=running 就展示"上次运行被中断"，
            # 绝不凭空补一个 completed/failed。
            "last_run": snap["last_run"],
            # 尚未解决的验证义务。下一轮初始化重置验证状态后会**填回**它，
            # 历史测试成功不因此变成当前任务的通行状态。
            "pending_verification": snap["pending_verification"],
            # 完成回执：辨认"结果已落盘但 inflight 标记没清"。只看 revision 变大不够——
            # 别的保存同样推进 revision。
            "last_committed_operation": last_op,
            "recent_operations": recent_ops,
            # ── B09b 输入队列：随同一份快照落盘，队列-接纳-历史保持一致 ──
            "input_queue": snap["input_queue"],
        },
        # list() 先快照：worker 线程可能正在 append（切会话时主线程存后台会话），
        # 直接迭代会撞 "list changed size during iteration"。
        "messages": [_msg_to_dict(m) for m in list(chat_history)],
    }
    # 读不懂的原始进度在被覆盖前先隔离保存下来。
    # 运行态可以作废它（不拿半信半疑的数据冒充真进度），但**不能让一次自动保存消灭唯一副本**——
    # 用户可能只是临时退回了旧版本，那份进度还要拿回去用。
    #
    # 判据是「**这份**进度有没有留过底」，不是「这个会话有没有隔离记录」。后者会丢数据：
    # 进度 A 隔离后，会话再次出现另一份读不懂的进度 B（比如又升一次级再退回来），
    # 因为"已有隔离记录"就跳过，B 直接被空进度覆盖掉。所以按内容分别留底、相同内容去重。
    old_progress = existing.get("progress")
    if old_progress is not None:
        _, why = _normalize_progress(old_progress, current_session_id)
        if why:
            data["quarantined_progress"] = _append_quarantine(
                existing.get("quarantined_progress"), old_progress, why)
            logger.warning(f"会话 {current_session_id} 的原进度无法识别（{why}），"
                           "已隔离保存到 quarantined_progress，不会被本次保存抹掉")

    # 同版本里本程序不认识的顶层键原样带过去：读得懂的部分照常更新，读不懂的不丢。
    for k, v in existing.items():
        if k not in data:
            data[k] = v

    with _LOCK:
        # 正文先写、索引后写：正文是唯一真数据，索引丢了能补，正文丢了补不回来。
        _atomic_write_json(session_file, data)
        # 正文一落盘，这个 revision 就已经提交出去了——**立刻推进内存修订号**，
        # 不能等索引也成功。否则索引失败后下次保存会复用同一个 revision，
        # 两份不同内容顶着同一个号，靠它区分保存版本的能力就废了。
        sess.progress_revision = rev
        # 回执的 revision 也就地补齐：下次保存不会再改写它（上面只在 revision 为 0 时填），
        # 否则同一条回执会跟着每次保存漂移，恢复时对不上它当初落的那一版。
        with sess.snapshot_lock:
            mem_op = getattr(sess, "last_committed_operation", None)
            if isinstance(mem_op, dict) and not mem_op.get("revision"):
                mem_op["revision"] = rev
        try:
            written_bytes = os.path.getsize(session_file)
        except OSError:
            written_bytes = 0
        try:
            _update_index(current_session_id, title, current_project,
                          session_kind=session_kind, rag_kb_dir=rag_kb_dir)
        except Exception as index_error:
            # 正文已落盘、索引没跟上：登记一条待修记录，下次读盘时**只**补这个 id。
            # 仍然把错误带出去——不能报"已保存"，调用方要知道这次没完全成功；
            # 但 body_written=True 让它知道回执确实落盘了，inflight 标记可以清。
            # 只登记 id：修复时回头读正文，不用这里的快照（见 _repair_pending_index_entries）
            _record_index_repair({"id": current_session_id,
                                  "recorded": datetime.now().isoformat()})
            return SaveOutcome(body_written=True, index_written=False, revision=rev,
                               error=index_error, elapsed_ms=_elapsed(),
                               bytes_written=written_bytes)
        # 正文与索引都成功 → 销掉可能存在的旧登记。不销的话，那条陈旧记录会在下次
        # 刷新列表时把索引改回登记当时的样子，回滚掉这次的更新。
        _drop_index_repair(current_session_id)
    logger.info(f"会话已保存: {current_session_id} - {title}")

    # 新会话存盘拿到 id 后，把注册表里的临时 key（_new_N）迁移成 id；
    # 否则 load_session(id) 用 id 查注册表查不到，会重复建一个 Session 与内存里的脱节。
    from . import session as _session
    target = session if session is not None else _session.current_session()
    if target is not None and target.key != current_session_id:
        _session.rekey(target, current_session_id)
    return SaveOutcome(body_written=True, index_written=True, revision=rev,
                       elapsed_ms=_elapsed(), bytes_written=written_bytes)


def _first_user_text():
    """返回当前会话第一条用户文本，用于生成标题。"""
    for msg in state.chat_history:
        if isinstance(msg, HumanMessage):
            c = msg.content
            if isinstance(c, list):
                texts = [
                    p.get("text", "")
                    for p in c
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                return (texts[0] if texts else "[图片]").strip()
            return str(c).strip()
    return ""


def _extract_text_content(resp):
    """从 LLM 响应里取纯文本。

    OpenAI 协议：resp.content 是字符串，直接用。
    Anthropic / MiMo（尤其开思考时）：resp.content 是 content block 列表
    （thinking 块 + text 块），要拼接其中的 text 块，否则把 list 丢给
    re.sub 会 TypeError、退回丑截断。
    """
    content = getattr(resp, "content", None)
    if content is None:
        return str(resp)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            else:
                # 正文块(Anthropic text / Responses output_text)，跳过思考 / 其它块
                t = block_text(block)
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return str(content)


def _sanitize_title(title):
    title = re.sub(r"[\r\n\t]+", " ", title or "").strip()
    title = title.strip("「」『』《》\"'`*#：:，,。. ")
    if not title:
        return ""
    return title[:16]


def _write_session_title(session_id, title):
    """更新当前会话文件中的 title 字段。"""
    session_file = os.path.join(memory_dir(), f"{session_id}.json")
    with _LOCK:
        if not os.path.exists(session_file):
            return
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["title"] = title
        data["updated"] = datetime.now().isoformat()
        _atomic_write_json(session_file, data)


def maybe_generate_session_title():
    """新会话首轮结束后自动生成短标题。失败时保留首句标题。"""
    if state.current_session_title or not state.current_session_id:
        return

    first_text = _first_user_text()
    if not first_text:
        return

    # 当前模型是 Claude Code（CLI 模式，model_id "claude" 不是真 API 模型，
    # 用它调 _create_llm 会打到 Anthropic API 报 404）→ 不值得为标题起 CLI 子进程，
    # 跟"太短问候"一样直接用首句截断。
    from .models import MODEL_LIST
    is_cli_model = MODEL_LIST[state.current_model_index][1] == "claude-code"

    # 太短的问候直接作为标题，不额外花一次模型调用。
    if len(first_text) <= 8 or is_cli_model:
        title = _sanitize_title(first_text)
    else:
        try:
            # 延迟 import 避免循环依赖（models.py 不依赖 memory）
            from .models import _create_llm
            # 标题任务强制关思考：1) 又快又省 token；2) 开思考时 Anthropic/MiMo
            # 的 resp.content 是 content block 列表，会让下面的提取出错退回截断
            title_llm = _create_llm(reasoning=False)
            prompt = (
                "请为下面这段对话生成一个简短中文标题。"
                "要求：不超过10个汉字，不要标点，不要解释，只输出标题。\n\n"
                f"用户：{first_text[:500]}"
            )
            resp = title_llm.invoke([
                SystemMessage(content="你只负责生成聊天标题。"),
                HumanMessage(content=prompt),
            ])
            title = _sanitize_title(_extract_text_content(resp))
        except Exception as e:
            logger.warning(f"自动生成标题失败: {e}，使用首句截断作为标题")
            title = ""

    # 降级方案：LLM 生成失败时，使用首句截断作为标题
    if not title:
        title = _sanitize_title(first_text)
        if not title:
            title = "新对话"

    state.current_session_title = title
    _ensure_memory_dir()
    # project tag 用【本会话】锚定的归属，不取全局 current_project：标题生成是后台线程，
    # 跑的时候用户可能已切到别的项目，取全局会把这个会话的 tag 写错。
    from . import session as _session_mod
    _sess = _session_mod.current_session()
    _kind = getattr(_sess, "session_kind", "code")
    _proj = None if _kind == "rag" else _sess.project
    if _proj is _session_mod._UNSET:
        _proj = state.current_project
    _update_index(state.current_session_id, title, _proj,
                  session_kind=_kind, rag_kb_dir=getattr(_sess, "rag_kb_dir", "") or "")
    _write_session_title(state.current_session_id, title)
    logger.info(f"自动标题已生成: {state.current_session_id} - {title}")


def _update_index(session_id, title, project=None, session_kind="code", rag_kb_dir=""):
    """更新索引条目：已存在的原地更新（刷新 updated，把它带回列表的近期位置），
    新会话插到最前。

    索引**全量保留**，不做任何数量截断：会话历史是用户数据，删除只能由用户主动
    发起（delete_session 连正文一起删）。早先这里把索引截断到 SESSION_HISTORY_LIMIT=50，
    并把挤出名额的会话**正文一并删掉**——内存里还开着的靠注册表检查幸免，
    没打开的连正文带索引一起消失且无法恢复。50 只是旧的展示数量；如今界面展示
    数量由侧栏的分组折叠 + 「加载更多」承担（limits.SIDEBAR_GROUP_VISIBLE_*），
    与历史保留彻底无关。
    """
    with _LOCK:
        with open(memory_index(), "r", encoding="utf-8") as f:
            index = json.load(f)

        for item in index:
            if item["id"] == session_id:
                item["title"] = title
                item["updated"] = datetime.now().isoformat()
                item["project"] = project
                item["session_kind"] = session_kind
                item["rag_kb_dir"] = rag_kb_dir
                break
        else:
            index.insert(0, {
                "id": session_id,
                "title": title,
                "updated": datetime.now().isoformat(),
                "project": project,
                "session_kind": session_kind,
                "rag_kb_dir": rag_kb_dir,
            })

        _atomic_write_json(memory_index(), index)


def load_session(session_id, *, session=None):
    """加载指定会话文件到内存。

    session=None → 写当前前台 Session（经 state 代理）；
    session=<Session> → 直接写目标 Session 对象（用于加载后台会话，避免污染前台）。
    """
    from . import session as _session_mod
    session_file = os.path.join(memory_dir(), f"{session_id}.json")
    with _LOCK:
        _repair_pending_index_entries()
        if not os.path.exists(session_file):
            return False
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)

    # 分类字段：旧会话缺失 → 默认 code / 空；未知 kind → 按 code 处理并告警。
    _kind = data.get("session_kind", "code")
    if _kind not in ("code", "rag"):
        logger.warning(f"会话 {session_id} 的 session_kind={_kind!r} 未知，按 code 处理")
        _kind = "code"
    _rag_dir = data.get("rag_kb_dir", "") or ""
    # Plan/Act 模式随会话持久化：不存的话，重开一个"聊到一半正在 Plan"的会话会变成
    # Act——用户以为还在只读规划，模型却已经能动手改代码了，是个有安全后果的静默降级。
    # 旧会话没这个字段 → 默认 act（与持久化之前的行为一致）。
    _mode = data.get("agent_mode", "act")
    if _mode not in ("plan", "act"):
        logger.warning(f"会话 {session_id} 的 agent_mode={_mode!r} 未知，按 act 处理")
        _mode = "act"
    # rag 会话与项目彻底解耦：忽略磁盘上可能残留的 project（下次保存会清成 None）。
    _proj = None if _kind == "rag" else data.get("project")

    # 进度：两条加载路径共用同一套校验/归一化，避免前台和后台会话行为分叉。
    # 坏掉的 progress 只作废进度本身，聊天历史照常恢复。
    progress, progress_error = _normalize_progress(data.get("progress"), session_id)
    if progress_error:
        logger.warning(f"会话 {session_id} 的进度无法恢复：{progress_error}")

    tgt = _session_mod.current_session() if session is None else session
    if tgt.current_session_id != session_id:
        tgt.background_owner_id = uuid.uuid4().hex
    if session is None:
        state.session_token_usage = {"input": 0, "output": 0, "total": 0}
        state.chat_history.clear()
        for d in data["messages"]:
            state.chat_history.append(_dict_to_msg(d))
        state.current_session_id = session_id
        state.current_session_title = data.get("title")
        state.compaction["summary"] = ""
        state.compaction["covered_upto"] = 0
        state.evicted_tool_results = _evicted_ids_from_data(data)
        tgt = _session_mod.current_session()
    else:
        session.session_token_usage = {"input": 0, "output": 0, "total": 0}
        session.chat_history.clear()
        for d in data["messages"]:
            session.chat_history.append(_dict_to_msg(d))
        session.current_session_id = session_id
        session.current_session_title = data.get("title")
        session.compaction = {"summary": "", "covered_upto": 0}
        session.evicted_tool_results = _evicted_ids_from_data(data)

    # 每个会话拿自己的深拷贝：共享容器会让两个会话的计划/台账互相串改。
    with tgt.snapshot_lock:
        tgt.current_plan = [dict(it) for it in progress["current_plan"]]
        tgt.task_ledger = {
            "files": dict(progress["task_ledger"].get("files") or {}),
            "commands": [dict(c) for c in (progress["task_ledger"].get("commands") or [])],
        }
        tgt.progress_revision = progress["revision"]
        tgt.progress_error = progress_error
        # 运行记录原样恢复，**不改写磁盘上的 phase**：phase=running 意思是程序从未收到
        # 那一轮的结果，展示成"上次运行被中断"即可（run_records.describe_last_run），
        # 不在加载时把它改成 interrupted——那是用我们的推断覆盖当时的事实记录。
        _lr = progress["last_run"]
        tgt.last_run = json.loads(json.dumps(_lr)) if isinstance(_lr, dict) else None
        _pv = progress["pending_verification"]
        tgt.pending_verification = {
            "files": list(_pv.get("files") or []),
            "code_files": list(_pv.get("code_files") or []),
            "tracking_incomplete": dict(_pv.get("tracking_incomplete") or {}),
            "file_paths": dict(_pv.get("file_paths") or {}),
            "reason": _pv.get("reason") or "",
            "run_id": _pv.get("run_id") or "",
        }
        _lo = progress["last_committed_operation"]
        tgt.last_committed_operation = dict(_lo) if isinstance(_lo, dict) else None
        tgt.recent_operations = [dict(r) for r in progress["recent_operations"]]
        # ── B05 任务身份、历史与切换恢复 ──
        tgt.current_task = json.loads(json.dumps(progress["task"])) if isinstance(progress.get("task"), dict) else None
        tgt.archived_tasks = [json.loads(json.dumps(t)) for t in (progress.get("archived_tasks") or [])
                              if isinstance(t, dict)]
        tgt.last_task_switch = json.loads(json.dumps(progress["last_task_switch"])) if isinstance(progress.get("last_task_switch"), dict) else None
        tgt.task_switch_version = int(progress.get("task_switch_version", 0) or 0)
        # ── B09b 输入队列：normalize 已按进程重启语义恢复（默认暂停、待核对）──
        tgt.input_queue = json.loads(json.dumps(progress.get("input_queue")
                                               or _new_input_queue()))
        # 加载不等于在跑：活动 run 是纯运行态，恢复出来的会话没有正在跑的 run。
        tgt.active_run_id = None
        # 同一个 Session 对象可能被复用来装另一个会话（侧栏切换就是这么干的）。
        # 结果快照缓存必须跟着作废，否则新装进来的会话会显示上一个会话的结果卡；
        # 要展示的话由 run_records.snapshot_from_loaded 从刚读进来的 last_run 重建。
        tgt.last_result = None

    # 分类 / 项目 / rag 锚点 / rag_mode / Plan-Act（对两条路径统一设置在目标 Session 上）
    tgt.session_kind = _kind
    tgt.rag_kb_dir = _rag_dir
    tgt.project = _proj
    tgt.rag_mode = (_kind == "rag")
    tgt.agent_mode = _mode
    # 旧会话没有这个字段 → 手动（和以前每次都问一致）；bypass 本来就不落盘，读到了也按手动
    from .permission_modes import normalize as _normalize_permission
    _perm = _normalize_permission(data.get("permission_mode"))
    tgt.permission_mode = "manual" if _perm == "bypass" else _perm
    # 思考强度：新会话文件直接读 reasoning_effort；更老的会话只有布尔 reasoning_enabled，
    # 按它推断（True → high，False → off）。两个都没有 → 默认档（与旧版默认"开"一致）。
    from .reasoning import legacy_to_effort, normalize as _normalize_effort
    _effort = data.get("reasoning_effort")
    if _effort is None:
        _effort = legacy_to_effort(data.get("reasoning_enabled", True))
    tgt.reasoning_effort = _normalize_effort(_effort)
    tgt.worktree = None

    # 清掉 sidecar 里**已经有匹配回执**的残留记录：它们的结果确实已经落盘，只是上次
    # 删标记时失败了，留着只会在恢复界面上报一个不存在的"结果未知"。
    # 没有回执的那些**原样保留**——它们是真正的诊断材料，由恢复界面（B04）呈现给用户。
    try:
        from .run_records import sweep_committed
        unknown, sweep_error = sweep_committed(tgt, session_id)
        if sweep_error:
            logger.warning(f"会话 {session_id} 的执行前记录无法读取：{sweep_error}")
        elif unknown:
            logger.info(f"会话 {session_id} 有 {len(unknown)} 个操作结果未知（不自动重放）")
    except Exception as _sweep_err:
        logger.warning(f"核对执行前记录失败 {session_id}: {_sweep_err}")

    logger.info(f"会话已加载: {session_id}（kind={_kind}, mode={_mode}）")
    return True


def list_sessions(project_filter="__current__", *, kind=None):
    """读取索引并按项目过滤（仅读 index.json，不逐个加载会话文件）。
    project_filter:
      - "__current__"（默认）：按 state.current_project 过滤
      - None：仅返回无项目的会话
      - "<path>"：返回该项目的会话
      - "__all__"：不过滤，返回全部
    kind: None=不按分类过滤；"code"/"rag"=只返回该分类（旧条目缺字段默认 code）。
    """
    _ensure_memory_dir()
    with _LOCK:
        # 补回上次"正文写成功但索引没写上"的条目，否则那些会话在侧栏里看不见
        _repair_pending_index_entries()
        if not os.path.exists(memory_index()):
            return []
        with open(memory_index(), "r", encoding="utf-8") as f:
            index = json.load(f)

    if kind is not None:
        index = [s for s in index if s.get("session_kind", "code") == kind]
    if project_filter == "__all__":
        return index
    if project_filter == "__current__":
        project_filter = state.current_project
    # None 和具体路径都用同样的相等判断（旧会话没 project 字段 → 默认 None → 归"无项目"）
    return [s for s in index if s.get("project") == project_filter]


def session_kind_of(session_id) -> str:
    """从 index 取会话分类（缺失/未知 → code）。不逐个读会话文件。"""
    for s in list_sessions("__all__"):
        if s["id"] == session_id:
            k = s.get("session_kind", "code")
            return k if k in ("code", "rag") else "code"
    return "code"


def move_sessions_to_no_project(old_path):
    """把所有 project==old_path 的会话改成"无项目（全局）"。
    用于：用户从列表移除一个项目时，把该项目下的历史会话也一起转到无项目，
    避免它们以"游离项目"的形式继续显示在侧栏。

    三处一起改，缺一不可：
      1. **内存里已打开的 Session.project**（关键）：只改磁盘的话，移除当前项目后
         _switch_project 的 save_session 会按旧内存锚点把刚迁移的会话写回已删项目，
         后台会话下次 save 同样复发——这是正常流程必现、非罕见磁盘错。
      2. index.json 的索引项；3. 每个 <id>.json 的 project 字段（保证重启后也是 None）。

    部分会话文件改写失败 → 抛 SessionMigrationError（内存锚点已置 None、可自愈，
    且让 caller 能提示用户，而非静默吞掉）。返回成功迁移的会话数。
    """
    if not old_path:
        return 0
    moved = 0
    failed_ids = []
    with _LOCK:
        from . import session as _session_mod
        # 1. 同步内存中所有已打开会话的项目锚点（最关键的一步）。
        #    用 live_sessions() 在 session._lock 内取一致快照——直接 list(sessions.values())
        #    在后台会话并发 register/rekey/drop 时可能抛 RuntimeError，原来被宽 except 吞成
        #    warning → 锚点同步被静默跳过 → "移除项目不复发"的修复在并发下原样失效。
        #    锁序：此处持 memory._LOCK 再取 session._lock，与 save_session→rekey 一致，无死锁。
        for _sess in _session_mod.live_sessions():
            # rag 会话与代码项目无关，项目迁移必须跳过（它 project 恒 None，防御性再判 kind）
            if getattr(_sess, "session_kind", "code") == "rag":
                continue
            if getattr(_sess, "project", _session_mod._UNSET) == old_path:
                _sess.project = None

        # 2 + 3. 磁盘 index.json + 各会话文件
        if not os.path.exists(memory_index()):
            return moved
        with open(memory_index(), "r", encoding="utf-8") as f:
            index = json.load(f)

        affected_ids = []
        for item in index:
            if item.get("session_kind", "code") == "rag":
                continue                       # rag 会话不参与项目迁移
            if item.get("project") == old_path:
                item["project"] = None
                affected_ids.append(item["id"])
                moved += 1

        if moved:
            _atomic_write_json(memory_index(), index)

            for sid in affected_ids:
                session_file = os.path.join(memory_dir(), f"{sid}.json")
                if not os.path.exists(session_file):
                    continue
                try:
                    with open(session_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data["project"] = None
                    _atomic_write_json(session_file, data)
                except Exception as e:
                    logger.warning(f"改写会话 {sid} project 字段失败: {e}")
                    failed_ids.append(sid)

    if moved:
        logger.info(f"已把 {moved} 个会话从 {old_path} 转到无项目")
    if failed_ids:
        raise SessionMigrationError(moved, failed_ids)
    return moved


def delete_session(session_id, *, keep_file_history=False):
    from .session import drop as drop_session

    session_file = os.path.join(memory_dir(), f"{session_id}.json")
    with _LOCK:
        if os.path.exists(session_file):
            os.remove(session_file)

        if os.path.exists(memory_index()):
            with open(memory_index(), "r", encoding="utf-8") as f:
                index = json.load(f)
            index = [i for i in index if i["id"] != session_id]
            _atomic_write_json(memory_index(), index)
        # 主动删除优先于任何待修登记：不销案的话，下次读盘会把它当"索引丢了"补回来，
        # 于是用户删掉的会话又出现在侧栏里。
        _drop_index_repair(session_id)
        # sidecar 跟着会话一起删。它不进侧栏索引，也不能作为复活已删除会话的依据——
        # 留着只会让下次启动为一个不存在的会话报"有操作结果未知"。
        from .run_records import discard_inflight
        discard_inflight(session_id)
        # B11a：文件撤销恢复材料（写前字节 + 归属记录）通常随会话删除。
        # 只删 file_history 自己目录下的记录文件，不碰用户 stash / Git。
        # keep_file_history=True 用于"回退到空"：对话清空了，但磁盘改动还在，
        # 顶栏撤销仍要能撤回它们——恢复材料必须留下（会话 id 也保留，见 rewind_session）。
        if not keep_file_history:
            try:
                from .file_history import discard_session
                discard_session(session_id)
            except Exception as error:
                logger.warning(f"清理文件撤销记录失败 {session_id}: {error}")
    # 同步清除会话注册表（不再持有该 Session 对象）
    drop_session(session_id)
    logger.info(f"会话已删除: {session_id}")


def reset_history(*, session=None):
    """重置聊天历史。

    session=None → 重置当前前台 Session（经 state 代理）；
    session=<Session> → 重置指定 Session。
    """
    if session is None:
        # 穿透 state 代理 → 当前活跃 Session
        from . import session as _session_mod
        _sess = _session_mod.current_session()
        _old_id = _sess.current_session_id
        state.session_token_usage = {"input": 0, "output": 0, "total": 0}
        save_session()   # 旧会话内容先存盘（save 内部会把对象 re-key 进注册表）
        state.chat_history.clear()
        state.chat_history.append(SystemMessage(content=get_system_prompt()))
        state.current_session_id = None
        state.current_session_title = None
        state.shell_cwd = None
        state.compaction["summary"] = ""
        state.compaction["covered_upto"] = 0
        state.evicted_tool_results = []
        _clear_progress(_sess)
        _sess.background_owner_id = uuid.uuid4().hex
        # 关键：这个 Session 对象已被"回收"成空白新对话，但注册表里还以旧 id 指向它。
        # 必须把旧 id 摘掉 + 清 key，否则点击侧栏旧会话会命中这个被清空的对象、显示空白
        # 且不重读盘（本会话"加载不出来"）。摘掉后旧会话内容仍在盘上，点击时重新读盘恢复。
        if _old_id:
            _session_mod.drop(_old_id)
            _sess.key = None
    else:
        # 直接操作目标 Session
        save_session(session=session)
        session.session_token_usage = {"input": 0, "output": 0, "total": 0}
        session.chat_history = [SystemMessage(content=get_system_prompt())]
        session.current_session_id = None
        session.current_session_title = None
        session.shell_cwd = None
        session.compaction = {"summary": "", "covered_upto": 0}
        session.evicted_tool_results = []
        _clear_progress(session)
        session.background_owner_id = uuid.uuid4().hex


# ── 对话时间轴：回退 / 编辑 / 分叉 ──────────────────────────────────────────────
# 设计契约（与 file_history 的"绝不回滚磁盘"一致）：
#   这些操作只动**对话与运行态**，绝不撤销磁盘文件改动。要撤销文件改动用顶栏的撤销按钮
#   （file_history.execute_undo），它独立于这里被清掉的运行态。
#   没有"每轮进度快照"，所以回退/分叉把运行态（计划/台账/待验证义务/结果卡/任务身份）
#   重置为干净状态，而不是精确还原到某一轮——这是诚实且无损坏的选择。


def truncate_history(sess, cut_index):
    """把会话历史回退到 cut_index：保留 chat_history[:cut_index]，丢弃其后所有消息。

    cut_index 是"保留多少条"（1 基计数，至少保留 index 0 的 SystemMessage）。
    在同一 snapshot_lock 临界区内完成截断 + 运行态重置，避免并发存盘拍到半截状态。
    **不存盘、不碰 file_history**。返回实际丢弃的消息数（0 = 没动）。
    """
    hist = sess.chat_history
    if not isinstance(cut_index, int) or cut_index < 1 or cut_index > len(hist):
        raise ValueError(f"cut_index 越界: {cut_index}（历史长度 {len(hist)}）")
    dropped = len(hist) - cut_index
    if dropped == 0:
        return 0
    with sess.snapshot_lock:
        del hist[cut_index:]
        # covered_upto 是绝对历史下标，截断后可能指向已删消息 → 摘要必须作废重算
        sess.compaction = {"summary": "", "covered_upto": 0}
        sess.evicted_tool_results = _evicted_ids_for_save(sess, hist)
        # 运行态整体重置（RLock 可重入）：上一轮的 run/结果卡/义务/任务身份都已不属于这段对话
        _clear_progress(sess)
    return dropped


def rewind_session(sess, cut_index):
    """回退会话到 cut_index 并落盘。返回丢弃的消息数。

    截断后仍有内容（>1 条）→ 正常存盘覆盖旧正文；
    回退到空（只剩 SystemMessage）→ 这段对话的正文已不存在，删正文+索引（退出侧栏），
    会话对象留作空白新对话。但**保留会话 id 与文件撤销材料**：磁盘改动并未被回退，
    顶栏撤销按钮（读 current_session_id + file_history）仍要能撤回它们；下次发消息时
    save_session 复用同一 id 重新落盘、重进侧栏，撤销链也接得上。删除只清会话正文，
    **不撤销任何磁盘文件改动**。
    """
    dropped = truncate_history(sess, cut_index)
    if dropped == 0:
        return 0
    if len(sess.chat_history) > 1:
        save_session(session=sess)
        return dropped
    old_id = sess.current_session_id
    if old_id:
        delete_session(old_id, keep_file_history=True)
    # 保留 current_session_id：撤销按钮靠它定位恢复材料。清掉它会让"回退到空"
    # 之后顶栏撤销立刻失效（材料还在却找不到归属）。key 清空、退出注册表与侧栏，
    # 下次 save_session 再按 old_id 重新注册。
    sess.current_session_title = None
    sess.key = None
    return dropped


def fork_session(sess, cut_index):
    """从 sess 的 chat_history[:cut_index] 分叉出一个新会话，返回新 Session（已注册、未激活）。

    复制对话前缀与用户设置（模型/模式/权限/思考强度/分类/项目），但运行态从零开始、
    拿新 id（首次存盘时铸造），**不继承 file_history 撤销记录**（新会话 id 查不到旧记录）。
    调用方负责激活（_activate_session 会重建系统提示词并重绘）与后续存盘。
    """
    from . import session as _sm
    hist = sess.chat_history
    if not isinstance(cut_index, int) or cut_index < 1 or cut_index > len(hist):
        raise ValueError(f"cut_index 越界: {cut_index}（历史长度 {len(hist)}）")
    new = _sm.Session()
    new.current_model_index = sess.current_model_index
    new.agent_mode = sess.agent_mode
    new.permission_mode = sess.permission_mode
    new.reasoning_effort = sess.reasoning_effort
    new.session_kind = sess.session_kind
    new.rag_mode = sess.rag_mode
    new.rag_kb_dir = sess.rag_kb_dir
    new.project = sess.project
    new.chat_history = list(hist[:cut_index])
    _sm.register(new)
    return new


# ── 会话重命名 / 跨会话搜索 ─────────────────────────────────────────────────────


def _sanitize_user_title(title):
    """用户手动命名的标题：只折叠空白 + 截断，不像自动标题那样剥掉标点/书名号。
    用户输入的标点是有意的，保留；上限 100 字符（侧栏显示截断到 12，全名进 tooltip）。
    """
    title = re.sub(r"[\r\n\t]+", " ", title or "").strip()
    return title[:100]


def rename_session(session_id, title):
    """把会话标题改成用户指定的 title。返回是否命中并改名（找不到该 id → False）。

    与 move_sessions_to_no_project 同样的"三处一起改"，缺一不可：
      1. **内存里已打开的 Session.current_session_title**（前台或后台）：只改磁盘的话，
         下次 save_session 会按旧内存标题把盘上的新名字覆盖回去——正常流程必现。
      2. index.json 的 title；3. <id>.json 正文的 title（重启后仍是新名）。
    三处在同一 _LOCK 临界区内完成。

    **不刷新 updated**：改名是元数据编辑、不是新活动，不该把会话顶到分组最前、
    打乱用户的历史顺序。空标题（去空白后为空）→ ValueError，交由调用方拦截。
    """
    if not session_id:
        return False
    clean = _sanitize_user_title(title)
    if not clean:
        raise ValueError("会话标题不能为空")
    renamed = False
    with _LOCK:
        from . import session as _sm
        # 1. 同步内存锚点（最关键）：live_sessions() 在 session._lock 内取一致快照，
        #    避免后台会话并发 register/rekey/drop 时遍历抛 RuntimeError。
        #    锁序 memory._LOCK → session._lock，与 save_session→rekey 一致，无死锁。
        for _sess in _sm.live_sessions():
            if getattr(_sess, "current_session_id", None) == session_id:
                _sess.current_session_title = clean

        # 2. index.json
        if os.path.exists(memory_index()):
            with open(memory_index(), "r", encoding="utf-8") as f:
                index = json.load(f)
            for item in index:
                if item.get("id") == session_id:
                    item["title"] = clean
                    renamed = True
                    break
            if renamed:
                _atomic_write_json(memory_index(), index)

        # 3. 正文（保留 updated 等其它字段，只改 title）
        session_file = os.path.join(memory_dir(), f"{session_id}.json")
        if os.path.exists(session_file):
            with open(session_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["title"] = clean
            _atomic_write_json(session_file, data)
            renamed = True
    if renamed:
        logger.info(f"会话已重命名: {session_id} → {clean}")
    return renamed


def _search_snippet(text, idx, qlen, radius=30):
    """命中处前后各取 radius 字符做预览，折叠空白，越界端加省略号。"""
    start = max(0, idx - radius)
    end = min(len(text), idx + qlen + radius)
    frag = re.sub(r"\s+", " ", text[start:end]).strip()
    return ("…" if start > 0 else "") + frag + ("…" if end < len(text) else "")


def search_sessions(query, *, kind=None, project="__all__"):
    """跨会话搜索：命中标题或对话正文（用户 + 助手的可见文本）的会话列表。

    结果按索引顺序（更新时间倒序）返回，每项：
      {id, title, project, session_kind, updated, snippet, match_in_title, match_count}
    snippet 是正文首个命中处的上下文预览（仅标题命中时为 ""）；match_count 是命中的消息条数。

    搜索范围：跳过 SystemMessage、内部消息（resume/recovery/repair/gate/vision_bridge）、
    思考块与工具结果——只搜真实用户输入与助手可见回复，避免噪声与巨串拖慢。大小写不敏感。

    并发：list_sessions 只在读 index 时持 _LOCK，读完即释放；随后逐个读正文都在锁外
    （_atomic_write_json 保证读到的是完整旧/新文件，不会读到半截）。本函数不持锁做重 IO，
    可安全放到 worker 线程跑，调用方（UI）负责不在主线程调用。空查询 → []。
    """
    q = (query or "").strip()
    if not q:
        return []
    needle = q.casefold()
    results = []
    for entry in list_sessions(project, kind=kind):
        sid = entry.get("id")
        if not sid:
            continue
        title = entry.get("title") or ""
        title_hit = needle in title.casefold()
        path = os.path.join(memory_dir(), f"{sid}.json")
        try:
            data = _read_existing_session(path, sid)
        except Exception as e:
            # 单个会话读不出（损坏/版本过新）不该中断整次搜索，跳过其正文、仍保留标题命中
            logger.debug(f"搜索跳过无法读取的会话 {sid}: {e}")
            data = {}
        snippet = ""
        match_count = 0
        for m in data.get("messages", []):
            if not isinstance(m, dict):
                continue
            if m.get("type") not in ("HumanMessage", "AIMessage"):
                continue
            if m.get("lingxi_internal") is True:
                continue
            text = content_text(m.get("content"))
            if not text:
                continue
            idx = text.casefold().find(needle)
            if idx < 0:
                continue
            match_count += 1
            if not snippet:
                snippet = _search_snippet(text, idx, len(q))
        if title_hit or match_count:
            results.append({
                "id": sid,
                "title": title,
                "project": entry.get("project"),
                "session_kind": entry.get("session_kind", "code"),
                "updated": entry.get("updated", ""),
                "snippet": snippet,
                "match_in_title": title_hit,
                "match_count": match_count,
            })
    return results
