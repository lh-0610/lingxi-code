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
import threading
from datetime import datetime

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage

from . import state
from .paths import logger, memory_dir, memory_index
from .roles import get_system_prompt
from .limits import SESSION_HISTORY_LIMIT


# 串行化 chat_memory/ 下所有文件的读-改-写。
# 用 RLock 是因为同一线程内 save_session() 已经持锁还会再调 _update_index()，
# 普通 Lock 会自死锁。
_LOCK = threading.RLock()


def _ensure_memory_dir():
    with _LOCK:
        os.makedirs(memory_dir(), exist_ok=True)
        if not os.path.exists(memory_index()):
            with open(memory_index(), "w", encoding="utf-8") as f:
                json.dump([], f)


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
            btype = block.get('type')
            if btype == 'thinking' and block.get('thinking'):
                content_blocks.append(block)
            elif btype == 'text' and block.get('text'):
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
    data = {
        "id": current_session_id,
        "title": title,
        "updated": datetime.now().isoformat(),
        "project": current_project,
        # list() 先快照：worker 线程可能正在 append（切会话时主线程存后台会话），
        # 直接迭代会撞 "list changed size during iteration"。
        "messages": [_msg_to_dict(m) for m in list(chat_history)],
    }
    with _LOCK:
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _update_index(current_session_id, title, current_project)
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
            if isinstance(block, dict):
                # 只取正文 text，跳过 thinking / 其它块
                if block.get("type") == "text" and block.get("text"):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
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
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


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
    _proj = _session_mod.current_session().project
    if _proj is _session_mod._UNSET:
        _proj = state.current_project
    _update_index(state.current_session_id, title, _proj)
    _write_session_title(state.current_session_id, title)
    logger.info(f"自动标题已生成: {state.current_session_id} - {title}")


def _update_index(session_id, title, project=None):
    with _LOCK:
        with open(memory_index(), "r", encoding="utf-8") as f:
            index = json.load(f)

        for item in index:
            if item["id"] == session_id:
                item["title"] = title
                item["updated"] = datetime.now().isoformat()
                item["project"] = project
                break
        else:
            index.insert(0, {
                "id": session_id,
                "title": title,
                "updated": datetime.now().isoformat(),
                "project": project,
            })

        kept_ids = {item["id"] for item in index[:SESSION_HISTORY_LIMIT]}
        dropped_ids = [item["id"] for item in index[SESSION_HISTORY_LIMIT:]]
        index = index[:SESSION_HISTORY_LIMIT]
        with open(memory_index(), "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
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
    session_file = os.path.join(memory_dir(), f"{session_id}.json")
    with _LOCK:
        if not os.path.exists(session_file):
            return False
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)

    if session is None:
        # 穿透 state 代理 → 当前活跃 Session（兼容旧调用）
        from . import session as _session_mod
        state.session_token_usage = {"input": 0, "output": 0, "total": 0}
        state.chat_history.clear()
        for d in data["messages"]:
            state.chat_history.append(_dict_to_msg(d))
        state.current_session_id = session_id
        state.current_session_title = data.get("title")
        state.current_plan = []
        state.task_ledger = state.new_task_ledger()
        state.compaction["summary"] = ""
        state.compaction["covered_upto"] = 0
        # 锚定会话所属项目为磁盘记录值（而非当前全局），切项目后再 save 不会改它
        _session_mod.current_session().project = data.get("project")
        _session_mod.current_session().worktree = None
    else:
        # 直接写目标 Session（可能是后台会话，不能经过 state 代理）
        session.session_token_usage = {"input": 0, "output": 0, "total": 0}
        session.chat_history.clear()
        for d in data["messages"]:
            session.chat_history.append(_dict_to_msg(d))
        session.current_session_id = session_id
        session.current_session_title = data.get("title")
        session.current_plan = []
        session.task_ledger = state.new_task_ledger()
        session.compaction = {"summary": "", "covered_upto": 0}
        session.project = data.get("project")  # 锚定为磁盘记录的项目归属
        session.worktree = None

    logger.info(f"会话已加载: {session_id}")
    return True


def list_sessions(project_filter="__current__"):
    """读取索引并按项目过滤。
    project_filter:
      - "__current__"（默认）：按 state.current_project 过滤
      - None：仅返回无项目的会话
      - "<path>"：返回该项目的会话
      - "__all__"：不过滤，返回全部
    """
    _ensure_memory_dir()
    with _LOCK:
        if not os.path.exists(memory_index()):
            return []
        with open(memory_index(), "r", encoding="utf-8") as f:
            index = json.load(f)

    if project_filter == "__all__":
        return index
    if project_filter == "__current__":
        project_filter = state.current_project
    # None 和具体路径都用同样的相等判断（旧会话没 project 字段 → 默认 None → 归"无项目"）
    return [s for s in index if s.get("project") == project_filter]


def move_sessions_to_no_project(old_path):
    """把所有 project==old_path 的会话改成"无项目（全局）"。
    用于：用户从列表移除一个项目时，把该项目下的历史会话也一起转到无项目，
    避免它们以"游离项目"的形式继续显示在侧栏。

    同时改 index.json 里的索引项 和 每个 <id>.json 里的 project 字段——
    后者保证下次重启或重新载入会话时也是 None。
    """
    if not old_path:
        return 0
    moved = 0
    with _LOCK:
        if not os.path.exists(memory_index()):
            return 0
        with open(memory_index(), "r", encoding="utf-8") as f:
            index = json.load(f)

        affected_ids = []
        for item in index:
            if item.get("project") == old_path:
                item["project"] = None
                affected_ids.append(item["id"])
                moved += 1

        if moved:
            with open(memory_index(), "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)

            # 同步改每个会话文件里的 project 字段
            for sid in affected_ids:
                session_file = os.path.join(memory_dir(), f"{sid}.json")
                if not os.path.exists(session_file):
                    continue
                try:
                    with open(session_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data["project"] = None
                    with open(session_file, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    logger.warning(f"改写会话 {sid} project 字段失败: {e}")

    if moved:
        logger.info(f"已把 {moved} 个会话从 {old_path} 转到无项目")
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
            with open(memory_index(), "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
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
        state.current_plan = []
        state.task_ledger = state.new_task_ledger()
        state.compaction["summary"] = ""
        state.compaction["covered_upto"] = 0
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
        session.current_plan = []
        session.task_ledger = state.new_task_ledger()
        session.compaction = {"summary": "", "covered_upto": 0}
