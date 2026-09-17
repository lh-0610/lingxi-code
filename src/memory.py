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
from datetime import datetime

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage

from . import state
from .paths import logger, memory_dir, memory_index
from .roles import get_system_prompt
from .limits import SESSION_HISTORY_LIMIT
from .content_blocks import is_text_block, is_think_block, block_text


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


def _normalize_progress(raw, session_id=""):
    """把磁盘上的 progress 归一成可用结构，返回 (progress_dict, error_reason)。

    容错原则：**进度坏掉不能拖垮聊天历史**。任何字段不合法就整块作废、返回空进度
    并说明原因，由调用方决定怎么告诉用户；绝不把半信半疑的数据当成真进度用下去。
    也绝不"尽量抢救"——抢救出来的计划看起来正常，用户无从分辨它是不是真的。
    """
    empty = {"current_plan": [], "task_ledger": state.new_task_ledger(), "revision": 0}
    if raw is None:
        return empty, ""
    if not isinstance(raw, dict):
        return empty, "progress 不是对象"

    ver = raw.get("version")
    if ver is not None and (not isinstance(ver, int) or ver > _PROGRESS_VERSION):
        return empty, f"progress 版本 {ver!r} 无法识别"

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
        ledger["files"] = {str(k): str(v) for k, v in files.items()}
        ledger["commands"] = [
            {"cmd": str(c.get("cmd", "")), "brief": str(c.get("brief", ""))}
            for c in cmds if isinstance(c, dict)
        ]

    rev = raw.get("revision", 0)
    if not isinstance(rev, int) or rev < 0:
        rev = 0
    return {"current_plan": plan, "task_ledger": ledger, "revision": rev}, ""


def _snapshot_progress(sess):
    """在快照锁内取走计划 + 台账的**深拷贝**。

    深拷贝是必须的：直接引用会让两个会话（或存盘副本与运行态）共享同一个 list/dict，
    之后一边改另一边跟着变，表现为"另一个会话的计划莫名其妙动了"。
    """
    with sess.snapshot_lock:
        plan = [dict(it) for it in (getattr(sess, "current_plan", None) or [])]
        ledger_src = getattr(sess, "task_ledger", None) or state.new_task_ledger()
        ledger = {
            "files": dict(ledger_src.get("files") or {}),
            "commands": [dict(c) for c in (ledger_src.get("commands") or [])],
        }
        rev = int(getattr(sess, "progress_revision", 0) or 0)
    return plan, ledger, rev


def _read_existing_session(path, session_id=""):
    """读旧文件，用于保留未知字段并检查格式版本。读不出来就当没有（不阻断保存）。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            old = json.load(f)
    except Exception as e:
        logger.warning(f"读取旧会话文件失败（将按新内容写入）{path}: {e}")
        return {}
    if not isinstance(old, dict):
        return {}
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
    """新会话从零开始：计划、台账、修订号、进度错误一起清。

    漏清 revision 会让新会话继承旧会话的修订号，后续批次拿它判断"结果是否已落盘"
    就会对错门。
    """
    with sess.snapshot_lock:
        sess.current_plan = []
        sess.task_ledger = state.new_task_ledger()
        sess.progress_revision = 0
        sess.progress_error = ""


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
        # 会话文件已不在 → 用户后来删了它，**不要复活**，直接销案
        if not os.path.exists(os.path.join(memory_dir(), f"{sid}.json")):
            continue
        try:
            _update_index(sid, item.get("title") or "新对话", item.get("project"),
                          session_kind=item.get("session_kind", "code"),
                          rag_kb_dir=item.get("rag_kb_dir", "") or "")
            fixed += 1
            logger.info(f"已补回缺失的索引项: {sid}")
        except Exception as e:
            logger.warning(f"补索引项失败 {sid}: {e}")
            still.append(item)
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
    if isinstance(msg, AIMessage) and msg.tool_calls:
        d["tool_calls"] = msg.tool_calls
    if isinstance(msg, AIMessage):
        ak = getattr(msg, 'additional_kwargs', None) or {}
        if ak.get('reasoning_content'):
            d["reasoning_content"] = ak['reasoning_content']
    if isinstance(msg, ToolMessage):
        d["tool_call_id"] = msg.tool_call_id
    return d


def _dict_to_msg(d):
    t = d["type"]
    if t == "SystemMessage":
        return SystemMessage(content=d["content"])
    elif t == "HumanMessage":
        return HumanMessage(content=d["content"])
    elif t == "AIMessage":
        ak = {}
        if "reasoning_content" in d:
            ak["reasoning_content"] = d["reasoning_content"]
        msg = AIMessage(
            content=d["content"],
            tool_calls=d.get("tool_calls", []),
            additional_kwargs=ak,
        )
        return msg
    elif t == "ToolMessage":
        return ToolMessage(content=d["content"], tool_call_id=d.get("tool_call_id", ""))
    return HumanMessage(content=d["content"])


def _build_ai_message(gathered, clean_text, tool_calls):
    """从 gathered AIMessageChunk 构造写入 chat_history 的 AIMessage。
    保留 thinking content blocks 和 reasoning_content，让下一轮 API 调用
    能把它们回传给服务端（MiMo / DeepSeek 等要求回传 thinking 上下文）。
    """
    ak = dict(getattr(gathered, 'additional_kwargs', {}) or {}) if gathered else {}

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


def save_session(*, session=None):
    """保存当前会话到本地文件（追加/更新）。

    session=None → 从 state 代理读（兼容旧调用）；
    session=<Session> → 直接从该 Session 对象读（用于保存后台会话）。
    """
    # 快照与落盘必须在同一临界区，否则先取的旧快照会覆盖 worker 的最终回复。
    with _LOCK:
        _save_session_locked(session=session)


def _save_session_locked(*, session=None):
    _ensure_memory_dir()

    from . import session as _session_mod
    # 要保存的会话对象：None 模式经代理拿当前线程的会话（主线程=active / worker=它的会话）
    sess = _session_mod.current_session() if session is None else session

    # 子 Agent 是临时会话：不落盘、不进侧栏历史（其改动经 worktree 合并回主项目即可）
    if getattr(sess, "is_subagent", False):
        return

    if session is None:
        chat_history = state.chat_history
        current_session_id = state.current_session_id
        current_session_title = state.current_session_title
    else:
        chat_history = session.chat_history
        current_session_id = session.current_session_id
        current_session_title = session.current_session_title

    if len(chat_history) <= 1:
        return

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

    # 计划 + 台账在快照锁内一次取走并深拷贝：这两者由工具线程持续改动，
    # 分两次读可能拍到"计划已更新、台账还没更新"的半截状态。
    plan_snapshot, ledger_snapshot, rev = _snapshot_progress(sess)
    rev += 1

    data = {
        "id": current_session_id,
        "title": title,
        "updated": datetime.now().isoformat(),
        "project": current_project,
        "session_kind": session_kind,
        "rag_kb_dir": rag_kb_dir,
        # Plan/Act 跟着会话走：重开时恢复原模式，别让"正在规划"的会话静默变回可动手
        "agent_mode": agent_mode,
        "schema_version": _SCHEMA_VERSION,
        "progress": {
            "version": _PROGRESS_VERSION,
            "revision": rev,
            "current_plan": plan_snapshot,
            "task_ledger": ledger_snapshot,
        },
        # list() 先快照：worker 线程可能正在 append（切会话时主线程存后台会话），
        # 直接迭代会撞 "list changed size during iteration"。
        "messages": [_msg_to_dict(m) for m in list(chat_history)],
    }
    # 同版本里本程序不认识的顶层键原样带过去：读得懂的部分照常更新，读不懂的不丢。
    for k, v in existing.items():
        if k not in data:
            data[k] = v

    with _LOCK:
        # 正文先写、索引后写：正文是唯一真数据，索引丢了能补，正文丢了补不回来。
        _atomic_write_json(session_file, data)
        try:
            _update_index(current_session_id, title, current_project,
                          session_kind=session_kind, rag_kb_dir=rag_kb_dir)
        except Exception:
            # 正文已落盘、索引没跟上：登记一条待修记录，下次读盘时**只**补这个 id。
            # 仍然把异常抛出去——不能报"已保存"，调用方要知道这次没完全成功。
            _record_index_repair({
                "id": current_session_id, "title": title, "project": current_project,
                "session_kind": session_kind, "rag_kb_dir": rag_kb_dir,
                "recorded": datetime.now().isoformat(),
            })
            raise
    sess.progress_revision = rev
    logger.info(f"会话已保存: {current_session_id} - {title}")

    # 新会话存盘拿到 id 后，把注册表里的临时 key（_new_N）迁移成 id；
    # 否则 load_session(id) 用 id 查注册表查不到，会重复建一个 Session 与内存里的脱节。
    from . import session as _session
    target = session if session is not None else _session.current_session()
    if target is not None and target.key != current_session_id:
        _session.rekey(target, current_session_id)


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

        kept_ids = {item["id"] for item in index[:SESSION_HISTORY_LIMIT]}
        dropped_ids = [item["id"] for item in index[SESSION_HISTORY_LIMIT:]]
        index = index[:SESSION_HISTORY_LIMIT]
        _atomic_write_json(memory_index(), index)
        for old_id in dropped_ids:
            if old_id in kept_ids or old_id == state.current_session_id:
                continue
            # 内存里还开着的会话（前台或后台正在跑的）不删盘——否则正在用的旧会话被挤出
            # 50 名额时文件被删，重启即丢整段对话。
            try:
                from . import session as _session_mod
                if _session_mod.get(old_id) is not None:
                    continue
            except Exception:
                pass
            old_file = os.path.join(memory_dir(), f"{old_id}.json")
            try:
                if os.path.exists(old_file):
                    os.remove(old_file)
            except Exception as e:
                logger.warning(f"删除旧会话文件失败 {old_id}: {e}")


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
    if session is None:
        state.session_token_usage = {"input": 0, "output": 0, "total": 0}
        state.chat_history.clear()
        for d in data["messages"]:
            state.chat_history.append(_dict_to_msg(d))
        state.current_session_id = session_id
        state.current_session_title = data.get("title")
        state.compaction["summary"] = ""
        state.compaction["covered_upto"] = 0
        tgt = _session_mod.current_session()
    else:
        session.session_token_usage = {"input": 0, "output": 0, "total": 0}
        session.chat_history.clear()
        for d in data["messages"]:
            session.chat_history.append(_dict_to_msg(d))
        session.current_session_id = session_id
        session.current_session_title = data.get("title")
        session.compaction = {"summary": "", "covered_upto": 0}

    # 每个会话拿自己的深拷贝：共享容器会让两个会话的计划/台账互相串改。
    with tgt.snapshot_lock:
        tgt.current_plan = [dict(it) for it in progress["current_plan"]]
        tgt.task_ledger = {
            "files": dict(progress["task_ledger"].get("files") or {}),
            "commands": [dict(c) for c in (progress["task_ledger"].get("commands") or [])],
        }
        tgt.progress_revision = progress["revision"]
        tgt.progress_error = progress_error

    # 分类 / 项目 / rag 锚点 / rag_mode / Plan-Act（对两条路径统一设置在目标 Session 上）
    tgt.session_kind = _kind
    tgt.rag_kb_dir = _rag_dir
    tgt.project = _proj
    tgt.rag_mode = (_kind == "rag")
    tgt.agent_mode = _mode
    tgt.worktree = None

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


def delete_session(session_id):
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
        _clear_progress(_sess)
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
        _clear_progress(session)
