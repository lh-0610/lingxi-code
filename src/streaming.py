"""全流式调用 + 工具执行。

- `_extract_usage`: 从累积的 AIMessageChunk 提取 token 用量
- `_stream_with_tools`: 边出 chunk 边显示 + 收集 tool_calls + 解析思考过程
- `_execute_tool`: 执行单个工具调用，把结果回写到 chat_history
"""
import os
import time
import threading as _threading

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from . import state
from . import debug_log
from .paths import logger
from .models import MODEL_LIST, current_model_supports_vision
from .tools import TOOL_DISPLAY_NAMES, build_git_write_confirmation, get_tool_map
from .limits import (
    COMPACTION_SUMMARY_MAX_CHARS,
    HISTORY_KEEP_RECENT,
    HISTORY_SAFETY_MARGIN,
    HISTORY_TOKEN_BUDGET,
    MAX_HISTORY_BUDGET,
    STREAM_RETRY_ATTEMPTS,
    TOOL_RESULT_EVICT_KEEP_RECENT,
    TOOL_RESULT_EVICT_MIN_CHARS,
    TOOL_RESULT_EVICT_PREVIEW_CHARS,
    TOOL_RESULT_HARD_CAP_CHARS,
    TOOL_RESULT_HARD_CAP_HEAD,
    TOOL_RESULT_HARD_CAP_TAIL,
    TOOL_RESULT_PREVIEW_CHARS,
)
from .images import (
    _normalize_image_blocks_for_current_model,
    _strip_images_in_followup_rounds,
    _strip_images_for_text_only_model,
    _strip_reasoning_for_deepseek,
)


def _pretty_args(args) -> str:
    """把工具参数 dict 美化成多行 JSON（给 UI 展示用，非合法 JSON）。

    json.dumps(indent=2) 只让 JSON 结构换行，但字符串值里的【真换行符】会被
    转义成字面 "\\n" 压成一行（如 sequentialthinking 的 thought 长文本）。展示
    时把这些换行还原成真换行，更好读。

    坑：不能无脑 replace("\\n")——Windows 路径 `C:\\name`（dumps 后是 `C:\\\\name`）
    里的反斜杠会被误伤。先用占位符保护字面双反斜杠，再还原换行/制表，最后把
    双反斜杠还原成单个（展示一个反斜杠即可）。
    """
    import json as _json
    try:
        text = _json.dumps(args, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(args)
    return (
        text.replace("\\\\", "\x00")   # 保护字面反斜杠
            .replace("\\n", "\n")      # 字符串值内换行 → 真换行
            .replace("\\t", "\t")      # 制表
            .replace("\x00", "\\")     # 还原：字面反斜杠展示成单个
    )


# 这些工具在执行过程中会自己把进度/输出 push 到 UI（边跑边显示），
# `_execute_tool` 完成后不再二次 display 工具结果，避免重复
STREAMING_TOOLS = {"run_command"}


# Plan mode 下允许调用的"只读"工具白名单。AI 若试图调其它工具会被 _execute_tool 拦
PLAN_MODE_READONLY_TOOLS = {
    "read_file", "list_directory", "search_in_file", "search_files",
    "remember", "forget",  # 记笔记不该被 Plan 拦
    "update_plan",  # 列计划是 Plan 模式的核心动作
    "set_step_status",  # 更新单步状态是 Plan 模式的核心动作
    "notify_user",  # 通知用户不该被 Plan 拦
    "read_background_output", "list_background_commands",  # 读后台输出也算只读
    "code_map",  # 代码地图只读扫描
    "git_diff", "git_log", "git_status",  # 只读 git：只看 diff/log/status，绝不碰 commit/add/push
    "check_code",  # 静态检查只读分析（lint/语法），不改文件
    "fetch_url",  # 抓取网页只读
    "web_search",  # 网络搜索只读
    "find_definition", "find_references",  # jedi 代码导航，只读分析
    "find_tests", "related_files",  # 测试发现 / 关联文件，只读分析
    "get_project_instructions",  # 读取项目规则文件，只读
}


# 遥控 safe_readonly 模式的敏感文件黑名单（内置默认，用户可在 config 追加）。
# 命中则远程禁止读取，防 config 密钥 / 长期记忆 / 角色配置等隐私外流到 Telegram。
_DEFAULT_REMOTE_BLOCK = {
    "config.json", "config.example.json",
    ".env", ".env.local",
    "long_term_memory.json", "role_config.json",
    "ui_prefs.json", "theme_config.json",
}
_DEFAULT_REMOTE_BLOCK_SUFFIX = (".key", ".pem", ".pfx", ".keystore")


def _hits_remote_blocklist(path: str) -> bool:
    """路径 basename 是否命中遥控敏感黑名单（内置 + config 追加）。大小写不敏感。"""
    if not path:
        return False
    from .config import REMOTE_BLOCKLIST
    base = os.path.basename(str(path)).lower()
    names = _DEFAULT_REMOTE_BLOCK | {n.lower() for n in (REMOTE_BLOCKLIST or [])}
    if base in names:
        return True
    return any(base.endswith(s) for s in _DEFAULT_REMOTE_BLOCK_SUFFIX)


# 会话长度阈值现在【按模型】算（见 _current_history_budget：窗口 − 输出预留 − 余量，再夹
# MAX_HISTORY_BUDGET 上限）。超阈值先回收旧工具结果(M2)、再 LLM 压缩中段(保留首条 system +
# 最近 KEEP_RECENT 条)。HISTORY_TOKEN_BUDGET(80K) 现仅作预算计算出错时的兜底默认。
def _history_has_image_blocks(messages) -> bool:
    for msg in messages or []:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") in ("image", "image_url"):
                return True
    return False


def _normalize_nonleading_system_messages(messages):
    """把历史中段的 SystemMessage 转成内部 HumanMessage。

    Anthropic 允许开头有连续 system 消息，但拒绝 human/assistant/tool 消息之后
    再出现 system。旧会话可能已经保存过这种消息；这里只清洗发送副本，不修改
    state.chat_history。
    """
    normalized: list = []   # 异质：SystemMessage / HumanMessage / 透传的原消息都进这里
    seen_non_system = False
    for msg in messages or []:
        if isinstance(msg, SystemMessage):
            if seen_non_system:
                normalized.append(HumanMessage(
                    content=f"[内部系统指令]\n{msg.content or ''}"
                ))
            else:
                normalized.append(msg)
            continue
        seen_non_system = True
        normalized.append(msg)
    return normalized


def _sanitize_tool_pairs(messages):
    """保证发给 API 的历史里 tool_use 必配 tool_result（Anthropic / MiMo 硬性要求）。

    停止生成 / 删除会话 / 历史压缩都可能留下"AIMessage 有 tool_call 但缺对应
    ToolMessage"（或反之的孤儿 ToolMessage），原样发出去会 400
    （tool_use ids must have corresponding tool_result）。这里统一兜底：
      - AIMessage 的某个 tool_call 没人应答 → 紧跟其后补一条占位 ToolMessage
      - 没有对应 tool_use 的孤儿 ToolMessage（如压缩把 tool_use 压走了）→ 丢弃
    只清洗发送副本，不动 state.chat_history。一处兜住停止/删除/压缩的悬空块。
    """
    from langchain_core.messages import AIMessage, ToolMessage

    def _tcid(tc):
        return tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)

    valid_ids = set()
    answered = set()
    for m in messages or []:
        if isinstance(m, AIMessage):
            for tc in (getattr(m, "tool_calls", None) or []):
                if _tcid(tc):
                    valid_ids.add(_tcid(tc))
        elif isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None):
            answered.add(m.tool_call_id)

    out = []
    for m in messages or []:
        if isinstance(m, ToolMessage):
            if getattr(m, "tool_call_id", None) in valid_ids:
                out.append(m)          # 有对应 tool_use 才保留
            continue                   # 否则丢弃孤儿 result
        out.append(m)
        if isinstance(m, AIMessage):
            for tc in (getattr(m, "tool_calls", None) or []):
                tid = _tcid(tc)
                if tid and tid not in answered:
                    out.append(ToolMessage(
                        content="[工具调用被中断，无结果]", tool_call_id=tid))
                    answered.add(tid)   # 防同一 id 重复补
    return out


def _stream_chunks_with_retry(llm_with_tools, messages, ui=None):
    """Retry transient stream startup failures before any chunk is displayed."""
    for attempt in range(STREAM_RETRY_ATTEMPTS):
        yielded = False
        try:
            for chunk in llm_with_tools.stream(messages):
                yielded = True
                yield chunk
            return
        except Exception:
            if yielded or state.stop_flag or attempt >= STREAM_RETRY_ATTEMPTS - 1:
                raise
            delay = 2 ** attempt
            logger.warning(f"模型流式请求失败，{delay}s 后重试（{attempt + 1}/{STREAM_RETRY_ATTEMPTS}）", exc_info=True)
            if ui is not None:
                try:
                    ui.show_message(f"\n⚠️ 模型请求失败，{delay}s 后自动重试...\n", "tool_result")
                except Exception:
                    pass
            time.sleep(delay)


def _estimate_tokens(messages) -> int:
    """粗估 token 数，不引外部库（tiktoken）。1 字符 ≈ 0.7 token（中英混排经验值）。
    多模态 image block 估 1000 token / 张，其它非 text block 估 200 token。"""
    total = 0
    for msg in messages:
        content = getattr(msg, "content", "") or ""
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict):
                    bt = blk.get("type")
                    if bt == "text":
                        total += len(blk.get("text", "") or "")
                    elif bt in ("image", "image_url"):
                        total += 1000
                    elif bt == "thinking":
                        total += len(blk.get("thinking", "") or "")
                    else:
                        total += 200
        # tool_calls 里的 args
        tcs = getattr(msg, "tool_calls", None) or []
        for tc in tcs:
            if isinstance(tc, dict):
                total += len(str(tc.get("args", {})))
    return int(total * 0.7)


def _maybe_trim_history(messages, budget=HISTORY_TOKEN_BUDGET, keep_recent=HISTORY_KEEP_RECENT):
    """估算 token 超阈值就裁中段，保留 system + 最近 keep_recent 条。

    返回 (新 messages, dropped_count)。dropped_count > 0 表示真的裁了。
    被裁掉的中段会被替换成一条 SystemMessage 占位 "[已自动裁剪 N 条旧消息]"，
    让 AI 知道历史里有空白，不会因为缺失上下文困惑。
    """
    from langchain_core.messages import SystemMessage as _SM
    est = _estimate_tokens(messages)
    if est <= budget:
        return messages, 0
    if len(messages) <= keep_recent + 1:
        return messages, 0  # 实在裁不动了

    has_system = bool(messages) and isinstance(messages[0], _SM)
    head = messages[:1] if has_system else []
    tail = messages[-keep_recent:]
    # 去重：head 可能跟 tail 头一条重合（极端短历史）
    if head and head[0] in tail:
        tail = [m for m in tail if m is not head[0]]
    dropped = len(messages) - len(head) - len(tail)
    if dropped <= 0:
        return messages, 0
    placeholder = _SM(
        content=f"[历史已自动裁剪：跳过中间 {dropped} 条消息以控制上下文长度。"
        f"如需查阅，请在 UI 上滚动查看完整对话。]"
    )
    return head + [placeholder] + tail, dropped


# ── 会话历史压缩（Compaction）──
# 超 token 预算时把中段旧消息总结成一条摘要，替代直接丢弃。


def _cap_oversized_tool_results(
    messages,
    budget=HISTORY_TOKEN_BUDGET,
    cap=TOOL_RESULT_HARD_CAP_CHARS,
    head=TOOL_RESULT_HARD_CAP_HEAD,
    tail=TOOL_RESULT_HARD_CAP_TAIL,
):
    """超预算时,把【任何】内容超过 cap 字符的 ToolMessage(含最近的)截成 头 + 尾 + 标记。

    与 M2 回收互补:回收只削"旧"结果(削成存根),这里管"巨无霸"(含最近的,削成头+尾)——
    防一串大结果堆在受保护的最近区、躲过回收和压缩、撑爆预算。
    只动发送副本,保留 ToolMessage 本体和 tool_call_id(不破坏配对)。
    返回 (新 messages, capped_count)。未超预算 → 原样返回, 0。
    """
    from langchain_core.messages import ToolMessage
    if _estimate_tokens(messages) <= budget:
        return messages, 0
    out = []
    capped = 0
    for m in messages:
        if isinstance(m, ToolMessage):
            content = m.content if isinstance(m.content, str) else str(m.content)
            if len(content) > cap:
                cut = len(content) - head - tail
                trimmed = (
                    content[:head]
                    + f"\n\n…[工具结果过大,中段 {cut} 字符已截断;"
                      f"需要这部分请重新调用对应工具读指定范围]…\n\n"
                    + content[-tail:]
                )
                out.append(ToolMessage(content=trimmed, tool_call_id=m.tool_call_id))
                capped += 1
                continue
        out.append(m)
    return out, capped


def _evict_old_tool_results(
    messages,
    budget=HISTORY_TOKEN_BUDGET,
    keep_recent=TOOL_RESULT_EVICT_KEEP_RECENT,
    min_chars=TOOL_RESULT_EVICT_MIN_CHARS,
):
    """超预算时，把"最近 keep_recent 条之外、且内容超过 min_chars 的"工具结果截成存根。

    存根 = 原内容前 PREVIEW 字符 + "[已回收，需要重新调用工具]"。保留 ToolMessage 本体和
    tool_call_id（不破坏 tool_use/tool_result 配对）。只动发送副本，不碰 state.chat_history。
    返回 (新 messages, evicted_count)。未超预算或没啥可回收 → 原样返回, 0。
    """
    if _estimate_tokens(messages) <= budget:
        return messages, 0
    # 所有 ToolMessage 的下标；最近 keep_recent 条保持完整，其余的算"旧"
    tool_idx = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    if len(tool_idx) <= keep_recent:
        return messages, 0
    evict_set = set(tool_idx[:-keep_recent])
    out = []
    evicted = 0
    for i, m in enumerate(messages):
        if i in evict_set and isinstance(m, ToolMessage):
            content = m.content if isinstance(m.content, str) else str(m.content)
            if len(content) > min_chars:
                preview = content[:TOOL_RESULT_EVICT_PREVIEW_CHARS].replace("\n", " ")
                stub = (
                    f"{preview}…\n"
                    f"[旧工具结果已回收：原 {len(content)} 字符。需要完整内容请重新调用对应工具获取。]"
                )
                out.append(ToolMessage(content=stub, tool_call_id=m.tool_call_id))
                evicted += 1
                continue
        out.append(m)
    return out, evicted


_COMPACT_SYSTEM_PROMPT = (
    "下面是一段较早的对话历史。请压缩成一段简洁摘要，保留对后续工作有用的信息：\n"
    "- 用户最初的核心需求 / 目标\n"
    "- 已经完成的操作（改过哪些文件、跑了什么命令/测试、结论）\n"
    "- 重要的发现、决定、踩过的坑\n"
    "- 尚未完成的事项\n"
    f"丢掉寒暄和中间试错的冗余细节。用中文，{COMPACTION_SUMMARY_MAX_CHARS} 字以内，分点列出。"
)


def _msg_to_plain_text(msg) -> str:
    """把单条消息渲染成可读纯文本（给压缩调用用，避免 tool_use/tool_result 配对问题）。"""
    cls = msg.__class__.__name__
    content = getattr(msg, "content", "") or ""

    # 提取文本：content 可能是 str 或 list of content blocks
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text"):
                parts.append(blk["text"])
        text = "\n".join(parts)
    else:
        text = str(content)

    if cls == "HumanMessage":
        return f"用户: {text}"
    elif cls == "AIMessage":
        tcs = getattr(msg, "tool_calls", None) or []
        if tcs:
            tc_str = ", ".join(
                f"{tc.get('name', '?')}({tc.get('args', {})})" for tc in tcs
            )
            return f"助手: [调用工具 {tc_str}]" + (f"\n{text}" if text.strip() else "")
        return f"助手: {text}"
    elif cls == "ToolMessage":
        tc_id = getattr(msg, "tool_call_id", "") or ""
        return f"[工具 {tc_id} 结果]: {text[:500]}"
    elif cls == "SystemMessage":
        return f"[系统]: {text}"
    return text


def _compact_history(
    messages, budget=HISTORY_TOKEN_BUDGET, keep_recent=HISTORY_KEEP_RECENT
):
    """超预算时用 LLM 压缩中段消息为摘要（滚动缓存 + 失败降级）。

    返回 (新 messages, dropped_count)。不 mutate state.chat_history。
    缓存命中（covered_upto >= cut）时不调 LLM，零成本复用。
    """
    est = _estimate_tokens(messages)
    if est <= budget:
        return messages, 0

    # 分离：system + tail（保留最近 keep_recent 条原始消息）
    has_system = bool(messages) and isinstance(messages[0], SystemMessage)
    system_msg = messages[0] if has_system else None
    tail = messages[-keep_recent:] if keep_recent > 0 else messages[-1:]
    # 去重：system 可能和 tail 头一条重合
    if system_msg and tail and system_msg is tail[0]:
        tail = list(tail[1:])

    # 压缩区间 [1, cut)，cut = len(messages) - keep_recent
    cut = len(messages) - keep_recent
    if cut <= 1:
        return messages, 0  # 没有中段可压缩

    comp = state.compaction
    prev_summary = comp.get("summary", "")
    prev_covered = comp.get("covered_upto", 0)

    if prev_covered >= cut and prev_summary:
        # ── 缓存命中：中段没新增，直接复用旧摘要，不调 LLM ──
        summary = prev_summary
    else:
        # ── 需要压缩：旧摘要 + 「上次覆盖点之后的新增中段」喂 LLM 总结 ──
        # 有旧摘要时只取 messages[prev_covered:cut] 的新增段，避免把已压进
        # prev_summary 的旧消息又作为原文重发一遍（滚动压缩的关键，否则二次
        # 压缩反而比不压更费 token）。首次压缩（无旧摘要）取整个中段 [1:cut]。
        mid_start = prev_covered if (prev_summary and prev_covered >= 1) else 1
        mid_msgs = messages[mid_start:cut]
        mid_msgs = _strip_images_for_text_only_model(mid_msgs)
        mid_text = "\n".join(_msg_to_plain_text(m) for m in mid_msgs)

        if prev_summary:
            full_text = f"[之前已有摘要]:\n{prev_summary}\n\n[新增对话]:\n{mid_text}"
        else:
            full_text = mid_text

        compress_msgs = [
            SystemMessage(content=_COMPACT_SYSTEM_PROMPT),
            HumanMessage(content=full_text),
        ]

        try:
            # 压缩用全局 state.llm（当前 active model）。压缩只是生成摘要、不影响回复正确性，
            # 没必要为它让每个 worker 各按自己 model 创建实例（也便于测试 mock state.llm）。
            resp = state.llm.invoke(compress_msgs)
            # 提取纯文本（兼容 Anthropic content block list 和 OpenAI 字符串）
            raw = getattr(resp, "content", "") or ""
            if isinstance(raw, list):
                parts = [
                    b.get("text", "")
                    for b in raw
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                summary = "\n".join(parts).strip()
            else:
                summary = str(raw).strip()
            # 裁剪过长摘要（留 1 字符给省略号）
            if len(summary) > COMPACTION_SUMMARY_MAX_CHARS:
                summary = summary[: COMPACTION_SUMMARY_MAX_CHARS - 1] + "…"
            # 更新缓存
            comp["summary"] = summary
            comp["covered_upto"] = cut
        except Exception:
            # ── 失败降级：回退到现有的直接裁剪 ──
            logger.warning("会话历史压缩失败，降级到直接裁剪", exc_info=True)
            return _maybe_trim_history(messages, budget, keep_recent)

    # ── 组装：system + 摘要 + tail ──
    summary_msg = SystemMessage(content=f"[历史摘要]:\n{summary}")
    result = ([system_msg] if system_msg else []) + [summary_msg] + list(tail)
    dropped = len(messages) - len(result)
    return result, dropped


def _wrap_system_for_cache(messages, fresh_system_text: str, provider: str):
    """生成发送用的 history。第一条 SystemMessage 用 `fresh_system_text` 替换掉
    （让 .lingxirules / 画图按需注入等"最新状态"生效）；对 Anthropic / MiMo 走
    content block + cache_control 形态开启 prompt caching。

    OpenAI 兼容协议（DeepSeek / Qwen 等）的 SDK 期望 content 是字符串，**不能**
    传 content block——它们要么自动 cache（DeepSeek 是），要么不支持 cache（多
    数兼容接口）；这里直接保持纯字符串形态。
    """
    from langchain_core.messages import SystemMessage as _SM
    if not messages:
        return messages
    head = messages[0]
    if not isinstance(head, _SM):
        return messages

    # 判断是否能用 cache_control：内置 anthropic/mimo 直接进；custom 类型要看
    # 用户配的 protocol 是不是 anthropic
    use_anthropic_cache = provider in ("anthropic", "mimo")
    if provider == "custom":
        from .models import MODEL_LIST as _ML, _lookup_custom_model
        model_id = _ML[state.current_model_index][2]
        cm = _lookup_custom_model(model_id) or {}
        use_anthropic_cache = (cm.get("protocol") or "openai").lower() == "anthropic"

    if use_anthropic_cache:
        # Anthropic prompt caching：content 写成 content blocks，给那一块标
        # `cache_control: {"type": "ephemeral"}`。命中缓存后该部分按 ~10% 计费。
        # 注意：完整 prompt 必须超过模型最小缓存阈值（Sonnet 是 1024 token，
        # 一般 system prompt 都够）才会真的进缓存。
        new_head = _SM(content=[
            {
                "type": "text",
                "text": fresh_system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ])
    else:
        # 其它 provider：纯字符串（兼容 OpenAI / Ollama / DeepSeek 等）
        new_head = _SM(content=fresh_system_text)

    return [new_head] + list(messages[1:])


def _llm_endpoint() -> str:
    """从当前 LLM 实例抓出可见的接口地址（给 Debug Inspector 看）。
    Anthropic / OpenAI / Ollama 等都有 base_url 字段；抓不到就返回空字符串。"""
    llm = getattr(state, "llm", None)
    if llm is None:
        return ""
    for attr in ("anthropic_api_url", "openai_api_base", "base_url", "endpoint_url"):
        url = getattr(llm, attr, None)
        if isinstance(url, str) and url:
            return url
    return ""


def _extract_usage(gathered):
    """从累加的 AIMessageChunk 提取 token 用量"""
    usage = {"input": 0, "output": 0, "total": 0}
    if gathered is None:
        return usage

    try:
        # LangChain >= 0.2 的 usage_metadata（Anthropic / OpenAI 均支持）
        um = getattr(gathered, 'usage_metadata', None)
        if um and isinstance(um, dict):
            usage["input"] = um.get("input_tokens", 0) or 0
            usage["output"] = um.get("output_tokens", 0) or 0
            usage["total"] = um.get("total_tokens", 0) or 0
            if usage["total"] == 0 and (usage["input"] or usage["output"]):
                usage["total"] = usage["input"] + usage["output"]
            return usage

        # 回退: response_metadata（OpenAI 兼容协议）
        rm = getattr(gathered, 'response_metadata', None) or {}
        tu = rm.get('token_usage', rm.get('usage', {}))
        if tu and isinstance(tu, dict):
            usage["input"] = tu.get("prompt_tokens", tu.get("input_tokens", 0)) or 0
            usage["output"] = tu.get("completion_tokens", tu.get("output_tokens", 0)) or 0
            usage["total"] = tu.get("total_tokens", 0) or 0
            if usage["total"] == 0 and (usage["input"] or usage["output"]):
                usage["total"] = usage["input"] + usage["output"]
            return usage

        # 最后尝试从 additional_kwargs 提取
        ak = getattr(gathered, 'additional_kwargs', None) or {}
        tu = ak.get('token_usage', ak.get('usage', {}))
        if tu and isinstance(tu, dict):
            usage["input"] = tu.get("prompt_tokens", tu.get("input_tokens", 0)) or 0
            usage["output"] = tu.get("completion_tokens", tu.get("output_tokens", 0)) or 0
            usage["total"] = tu.get("total_tokens", 0) or 0
            if usage["total"] == 0 and (usage["input"] or usage["output"]):
                usage["total"] = usage["input"] + usage["output"]
            return usage

    except Exception as e:
        logger.warning(f"提取 token 用量失败: {e}")
        return usage

    return usage


class _StreamState:
    """一轮流式调用跨 chunk 共享的可变状态。

    把原来散在 _stream_with_tools 里的一堆局部变量收进来，方便 _handle_stream_chunk
    原地改、主循环只管编排。
    """
    __slots__ = ("raw_text", "in_think", "think_started", "think_mode",
                 "think_done", "first_token", "gathered",
                 "tool_call_start", "tool_call_last",
                 "tool_hb_stop", "tool_hb_thread")

    def __init__(self):
        self.raw_text = ""
        self.in_think = False
        self.think_started = False
        self.think_mode = None  # None / "reasoning" / "tag"
        self.think_done = False
        self.first_token = True
        self.gathered = None
        # 工具调用参数流式生成期的指示器状态（None = 没在生成工具调用）
        self.tool_call_start = None   # 开始生成工具调用的时间戳
        self.tool_call_last = 0.0     # 上次刷新指示器的时间戳（节流用）
        self.tool_hb_stop = None      # 工具调用心跳线程的停止事件
        self.tool_hb_thread = None    # 工具调用心跳线程


def _current_history_budget() -> int:
    """根据当前模型的上下文窗口动态计算历史预算。

    公式: 窗口 - max_tokens - SAFETY_MARGIN，上限 MAX_HISTORY_BUDGET。
    异常时回退到 HISTORY_TOKEN_BUDGET。
    """
    try:
        from .models import context_window_for, _max_tokens_for
        name, mtype, model_id, _think = MODEL_LIST[state.current_model_index]
        cwin = context_window_for(mtype, model_id)
        max_out = _max_tokens_for(mtype, model_id)
        budget = cwin - max_out - HISTORY_SAFETY_MARGIN
        return min(budget, MAX_HISTORY_BUDGET)
    except Exception:
        return HISTORY_TOKEN_BUDGET


def _prepare_stream_history(ui):
    """构造本轮真正发给 LLM 的 history + 建 Debug record。

    依次：归一化图片 → 剥跟随轮图片 / 文本模型图片 / DeepSeek reasoning →
    （文本模型有图时提示一次）→ 按需重渲染 system prompt（画图按需注入 +
    .lingxirules + Anthropic 缓存）→ 滑动窗口裁剪 → 开 Debug record。

    返回 (history_for_send, debug_rec)。
    """
    history_for_send = _normalize_image_blocks_for_current_model(state.chat_history)
    history_for_send = _normalize_nonleading_system_messages(history_for_send)
    text_only_image_warning = (
        not current_model_supports_vision()
        and _history_has_image_blocks(history_for_send)
    )
    history_for_send = _strip_images_in_followup_rounds(history_for_send)
    history_for_send = _strip_images_for_text_only_model(history_for_send)
    history_for_send = _strip_reasoning_for_deepseek(history_for_send)
    if text_only_image_warning:
        warning_key = (state.current_session_id, state.current_model_index)
        if getattr(state, "_last_text_only_image_warning", None) != warning_key:
            state._last_text_only_image_warning = warning_key
            try:
                ui.show_message(
                    "\n⚠️ 当前模型不支持视觉，历史图片已转为文本占位发送；如需让模型看图，请切换到支持图片的模型。\n",
                    "tool_result",
                )
            except Exception:
                pass

    # ── 按需重渲染 system prompt ──
    # 1. 拿到最新的 .lingxirules / 项目上下文（用户中途改这些文件也立刻生效）
    # 2. 对 Anthropic / MiMo 把 system message 转成 content block + cache_control，
    #    开启 prompt caching（缓存命中后该部分按 ~10% 价计费）
    from .roles import get_system_prompt as _get_system_prompt
    if history_for_send and history_for_send[0].__class__.__name__ == "SystemMessage":
        fresh_system = _get_system_prompt()
        history_for_send = _wrap_system_for_cache(
            history_for_send, fresh_system, provider=MODEL_LIST[state.current_model_index][1],
        )

    # 先做便宜的工具结果回收（超预算时把旧的大工具结果截成存根，模型要详情可重新调工具）。
    # 常常回收完就压回预算内，下面的 LLM 压缩自然 no-op，省钱省延迟。
    _budget = _current_history_budget()
    # M4: 先把单条巨无霸(含最近的)截成头+尾,防它躲过回收/压缩撑爆预算
    history_for_send, _capped = _cap_oversized_tool_results(history_for_send, budget=_budget)
    if _capped > 0:
        logger.info(f"工具结果硬上限:本轮把 {_capped} 条过大结果截成头+尾")
        try:
            ui.show_message(
                f"\n✂️ 有 {_capped} 条过大的工具结果被截断为首尾摘要(需要细节我会重新读指定范围)。\n",
                "tool_result",
            )
        except Exception:
            pass
    history_for_send, _evicted = _evict_old_tool_results(history_for_send, budget=_budget)
    if _evicted > 0:
        logger.info(f"工具结果回收：本轮截断 {_evicted} 条旧的大工具结果为存根")
        try:
            ui.show_message(
                f"\n♻️ 上下文偏长，本轮把 {_evicted} 条较早的大工具结果折叠为摘要存根"
                f"（最近 {TOOL_RESULT_EVICT_KEEP_RECENT} 条保持完整；需要详情我会重新读）。\n",
                "tool_result",
            )
        except Exception:
            pass

    # 会话历史压缩：超过预算时用 LLM 压缩中段为摘要（滚动缓存，失败降级裁剪）。
    # state.chat_history 本身不改，UI 上保留完整历史，只是发给 LLM 的 history_for_send 被压缩。
    history_for_send, _trimmed = _compact_history(history_for_send, budget=_budget)
    if _trimmed > 0:
        logger.info(f"会话历史超阈值，本轮压缩裁剪 {_trimmed} 条旧消息")
        try:
            ui.show_message(
                f"\n⚠️ 对话历史过长，本轮自动压缩中间 {_trimmed} 条旧消息为摘要（保留首条 system 提示 + 最近"
                f" {HISTORY_KEEP_RECENT} 条）。UI 上仍保留完整。\n",
                "tool_result",
            )
        except Exception:
            pass

    # 发送前兜底：保证 tool_use/tool_result 配对（停止/删除/压缩留下的悬空会 400）。
    # 放在压缩之后——压缩切断的 pair 也一并修。
    history_for_send = _sanitize_tool_pairs(history_for_send)

    # ── Debug Inspector：开始一条 record（即使用户没打开 F12 也照收）──
    # 把首条 SystemMessage 单拿出来当 system_prompt 字段，messages 列表里不再重复
    # （否则 Inspector 上同样的提示词会显示两次）
    _model_name, _provider = MODEL_LIST[state.current_model_index][:2]
    _system_prompt = ""
    _messages_for_record = history_for_send
    if history_for_send and history_for_send[0].__class__.__name__ == "SystemMessage":
        _system_prompt = str(history_for_send[0].content or "")
        _messages_for_record = history_for_send[1:]
    debug_rec = debug_log.make_record(
        model=_model_name,
        provider=_provider,
        endpoint=_llm_endpoint(),
        messages=_messages_for_record,
        tools=list(get_tool_map().keys()),
        system_prompt=_system_prompt,
        max_tokens=getattr(state.llm, "max_tokens", None),
    )
    return history_for_send, debug_rec


def _chunk_has_visible(chunk) -> bool:
    """这个 chunk 有没有可显示的内容（reasoning / text / thinking）。
    用来区分"纯工具调用参数 chunk"和"带正文的 chunk"。"""
    if getattr(chunk, 'additional_kwargs', {}).get('reasoning_content'):
        return True
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return bool(content)
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("text", "thinking"):
                if b.get("text") or b.get("thinking"):
                    return True
    return False


def _current_tool_name(gathered) -> str:
    """从累加结果里取当前正在生成的工具名（拿不到就返回'工具'）。"""
    if gathered is None:
        return "工具"
    tcs = getattr(gathered, "tool_calls", None) or []
    if tcs and tcs[-1].get("name"):
        return tcs[-1]["name"]
    for c in reversed(getattr(gathered, "tool_call_chunks", None) or []):
        if c.get("name"):
            return c["name"]
    return "工具"


def _start_tool_call_heartbeat(st, ui):
    """工具调用参数生成期的独立心跳：主动每秒刷新计时，不依赖 chunk 到达。

    之前计时靠"每个纯 tool_call chunk 到达时刷新"被动驱动；MiMo 一次性吐完
    arguments 后没有后续 chunk，计时就卡在 0s。改成独立 daemon 线程主动 tick。
    """
    if getattr(st, "tool_hb_thread", None) is not None:
        return
    st.tool_hb_stop = _threading.Event()

    def _run():
        # wait(1) 超时返回 False（继续刷新），被 set 时返回 True（退出）
        while not st.tool_hb_stop.wait(1):
            try:
                nm = _current_tool_name(st.gathered)
                el = int(time.time() - st.tool_call_start)
                ui.update_thinking_indicator(f"🔧 正在生成工具调用 {nm}... ({el}s)\n")
            except Exception:
                break

    st.tool_hb_thread = _threading.Thread(target=_run, daemon=True)
    st.tool_hb_thread.start()


def _refresh_tool_call_indicator(st, ui, heartbeat_stop):
    """进入工具调用参数生成期：停主心跳、清残留、显示指示器、起独立心跳主动计时。

    只在首次进入时做这些；后续 chunk 进来啥也不用做（独立心跳线程负责刷新计时）。
    收尾在 _stream_with_tools 末尾统一 remove 指示器 + 停心跳。
    """
    if st.tool_call_start is not None:
        return  # 已在生成期，心跳线程主动刷新中
    st.tool_call_start = time.time()
    st.first_token = False
    heartbeat_stop.set()            # 停掉等待/思考主心跳，避免两个心跳打架
    ui.remove_thinking_indicator()  # 清掉残留的"等待响应/思考中"指示器
    name = _current_tool_name(st.gathered)
    ui.show_message(f"🔧 正在生成工具调用 {name}... (0s)\n", "thinking_indicator")
    _start_tool_call_heartbeat(st, ui)  # 主动每秒刷新，不靠 chunk 驱动


def _handle_stream_chunk(st, chunk, ui, heartbeat_stop, heartbeat_phase):
    """处理单个 chunk：累加 gathered + 解析 reasoning/thinking/正文 + 推送到 UI。

    原地改 st。原 _stream_with_tools 主循环体逐行搬来，`continue` → `return`
    （外层 chunk 循环用；内层 block 循环的 continue 保持不变）。
    """
    # 累加 chunk —— LangChain 自动合并 content 和 tool_call_chunks
    st.gathered = chunk if st.gathered is None else st.gathered + chunk

    # 工具调用参数流式生成期：chunk 带 tool_call_chunks 但没有可显示正文。
    # 这段可能很长（比如生成大文件 write_file 的 content 参数，要几十秒），
    # 期间没有任何可见输出，UI 看着像卡死。挂一个实时指示器。
    if getattr(chunk, "tool_call_chunks", None) and not _chunk_has_visible(chunk):
        _refresh_tool_call_indicator(st, ui, heartbeat_stop)
        return

    # 提取 reasoning_content（思考过程）
    reasoning = getattr(chunk, 'additional_kwargs', {}).get('reasoning_content', '')
    if reasoning:
        if not st.think_started:
            st.think_started = True
            st.in_think = True
            st.think_mode = "reasoning"
            heartbeat_stop.set()
            if st.first_token:
                st.first_token = False
            ui.remove_thinking_indicator()
            ui.show_message("Thinking...\n", "think_header")
        ui.show_message(reasoning, "think_msg")
        return
    elif not chunk.content and st.first_token:
        heartbeat_phase[0] = "thinking"

    # Anthropic 协议（MiMo / Claude Sonnet 等）：content 是 list of content blocks
    if isinstance(chunk.content, list):
        for block in chunk.content:
            if not isinstance(block, dict):
                continue
            btype = block.get('type')
            if btype == 'thinking':
                r = block.get('thinking', '')
                if r:
                    if not st.think_started:
                        st.think_started = True
                        st.in_think = True
                        st.think_mode = "reasoning"
                        heartbeat_stop.set()
                        if st.first_token:
                            st.first_token = False
                        ui.remove_thinking_indicator()
                        ui.show_message("Thinking...\n", "think_header")
                    ui.show_message(r, "think_msg")
            elif btype == 'text':
                t = block.get('text', '')
                if t:
                    if st.first_token:
                        st.first_token = False
                        ui.remove_thinking_indicator()
                    if st.in_think and st.think_started:
                        st.in_think = False
                        st.think_mode = None
                        st.think_done = True
                        heartbeat_stop.set()
                        ui.show_message("", "think_collapse")
                        ui.show_message("\n\n", "spacer")
                    st.raw_text += t
                    ui.show_message(t, "ai_msg")
        return

    # OpenAI 协议：content 是字符串
    token = chunk.content
    if not token:
        return
    st.raw_text += token

    if st.first_token:
        st.first_token = False
        ui.remove_thinking_indicator()

    # reasoning_content 模式下思考结束，切换到正文。
    # 显式 <think>...</think> 标签需要等到 </think> 再折叠，否则正文会被误塞进思考块。
    if st.in_think and st.think_started and st.think_mode == "reasoning":
        st.in_think = False
        st.think_mode = None
        st.think_done = True
        heartbeat_stop.set()
        ui.show_message("", "think_collapse")
        ui.show_message("\n\n", "spacer")

    # 解析 <think> / <thought> 标签（兼容非 reasoning 模式）
    _has_open = "<think>" in st.raw_text or "<thought>" in st.raw_text
    _open_tag = "<think>" if "<think>" in token else "<thought>" if "<thought>" in token else None
    if _has_open and not st.think_started:
        st.think_started = True
        st.in_think = True
        st.think_mode = "tag"
        heartbeat_phase[0] = "thinking"
        ui.show_message("Thinking...\n", "think_header")
        _first_tag = "<think>" if "<think>" in st.raw_text else "<thought>"
        display = token.split(_first_tag, 1)[-1] if _first_tag in token else st.raw_text.split(_first_tag, 1)[-1]
        if "</think>" in display or "</thought>" in display:
            _close = "</think>" if "</think>" in display else "</thought>"
            before, after = display.split(_close, 1)
            st.in_think = False
            st.think_mode = None
            st.think_done = True
            heartbeat_stop.set()
            if before:
                ui.show_message(before, "think_msg")
            ui.show_message("", "think_collapse")
            ui.show_message("\n\n", "spacer")
            if after:
                ui.show_message(after, "ai_msg")
            return
    elif "</think>" in token or "</thought>" in token:
        _close = "</think>" if "</think>" in token else "</thought>"
        before, after = token.split(_close, 1)
        st.in_think = False
        st.think_mode = None
        st.think_done = True
        heartbeat_stop.set()
        if before:
            ui.show_message(before, "think_msg")
        ui.show_message("", "think_collapse")
        ui.show_message("\n\n", "spacer")
        if after:
            ui.show_message(after, "ai_msg")
        return
    else:
        display = token
        if not st.in_think and st.think_done:
            heartbeat_stop.set()

    if display:
        if st.in_think:
            ui.show_message(display, "think_msg")
        else:
            ui.show_message(display, "ai_msg")


def _collect_tool_calls(gathered):
    """从累加结果提取合法 tool_calls（args 已由 LangChain 自动 JSON 解析为 dict）。"""
    valid_tool_calls = []
    if gathered is None:
        return valid_tool_calls
    # 合成 id 兜底用单调序号：部分 provider（某些本地模型）不返回 tool_call id，原来回退成
    # 工具名——同一轮调两次同名工具时两条 id 撞车，AIMessage.tool_calls 与 ToolMessage 配对
    # 错乱，下一轮 Anthropic/OpenAI 因「重复 tool_use id」抛 400。带序号保证唯一；provider
    # 给了真 id 时完全不变（or 短路）。序号每次迭代都自增，跨两个 loop 也不重复。
    _i = 0
    for tc in (gathered.tool_calls or []):
        name = tc.get("name", "")
        if name and name in get_tool_map():
            valid_tool_calls.append({
                "name": name,
                "args": tc.get("args") or {},
                "id": tc.get("id") or f"call_{name}_{_i}",
            })
        _i += 1
    # 兼容 args JSON 解析失败的工具调用：保持原 fail-open 行为（args={}）
    for tc in (getattr(gathered, 'invalid_tool_calls', None) or []):
        name = tc.get("name", "") or ""
        if name and name in get_tool_map():
            valid_tool_calls.append({
                "name": name,
                "args": {},
                "id": tc.get("id") or f"call_{name}_{_i}",
            })
        _i += 1
    return valid_tool_calls


def _extract_thinking(gathered):
    """从累加结果提取思考文本（Anthropic thinking block / reasoning_content），供 Debug record。"""
    _thinking = ""
    try:
        if gathered is not None and isinstance(gathered.content, list):
            _thinking = "\n".join(
                b.get("thinking", "") for b in gathered.content
                if isinstance(b, dict) and b.get("type") == "thinking"
            )
        if not _thinking and gathered is not None:
            _thinking = (getattr(gathered, "additional_kwargs", {}) or {}).get("reasoning_content", "") or ""
    except Exception:
        pass
    return _thinking


def _stream_with_tools(ui):
    """
    全流式调用：实时显示思考过程和回复，同时收集 tool_calls。
    返回 (raw_text, tool_calls, usage, gathered)

    编排：准备 history → 起心跳线程 → 逐 chunk 派发给 _handle_stream_chunk →
    收尾（折叠思考 / 收 tool_calls / 提 usage / finalize Debug record）。
    """
    st = _StreamState()
    stream_start = time.time()

    ui.show_message("等待响应...\n", "thinking_indicator")

    # 心跳线程：持续更新计时，直到收到第一个文本 token
    heartbeat_stop = _threading.Event()
    heartbeat_phase = ["waiting"]  # "waiting" → "thinking"

    def _heartbeat():
        """心跳线程：更新等待/思考计时指示器。UI 无响应时静默退出。"""
        while not heartbeat_stop.is_set():
            try:
                elapsed = int(time.time() - stream_start)
                if heartbeat_phase[0] == "thinking":
                    ui.update_thinking_indicator(f"模型思考中... ({elapsed}s)\n")
                else:
                    ui.update_thinking_indicator(f"等待响应... ({elapsed}s)\n")
            except Exception as e:
                logger.warning(f"心跳线程 UI 更新失败: {e}")
                break
            heartbeat_stop.wait(1)

    hb_thread = _threading.Thread(target=_heartbeat, daemon=True)
    hb_thread.start()

    history_for_send, _debug_rec = _prepare_stream_history(ui)

    try:
        # 用【本会话】model 的 llm_with_tools（worker 线程绑的会话），不读全局 state.llm_with_tools
        from . import agent as _agent, session as _session
        _lwt = _agent.resolve_bound_llm(_session.current_session())[1]
        for chunk in _stream_chunks_with_retry(_lwt, history_for_send, ui):
            if state.stop_flag:
                break
            _handle_stream_chunk(st, chunk, ui, heartbeat_stop, heartbeat_phase)
    except Exception as _err:
        heartbeat_stop.set()
        _ths = getattr(st, "tool_hb_stop", None)
        if _ths is not None:
            _ths.set()  # 停工具调用心跳
        # 记录失败：把异常信息也写进 record 再 raise，让 Inspector 能看到错误
        try:
            import traceback as _tb
            debug_log.finalize_record(
                _debug_rec, text=st.raw_text,
                error=f"{type(_err).__name__}: {_err}\n{_tb.format_exc(limit=5)}",
            )
        except Exception:
            pass
        raise

    heartbeat_stop.set()
    _ths = getattr(st, "tool_hb_stop", None)
    if _ths is not None:
        _ths.set()  # 停工具调用心跳

    if st.first_token:
        ui.remove_thinking_indicator()

    # 收尾移除工具调用指示器（"🔧 正在生成工具调用..."），随后 _execute_tool 会显示实际工具标签
    if st.tool_call_start is not None:
        ui.remove_thinking_indicator()

    # 兜底：思考过但没在流中折叠（例如思考后直接调工具，没有正文）
    if st.think_started and st.in_think:
        ui.show_message("", "think_collapse")
        ui.show_message("\n", "spacer")

    valid_tool_calls = _collect_tool_calls(st.gathered)
    usage = _extract_usage(st.gathered)
    _thinking = _extract_thinking(st.gathered)

    # 正常路径 finalize 一条完整 record（含 reasoning / tool_calls / usage）
    try:
        debug_log.finalize_record(
            _debug_rec, text=st.raw_text,
            tool_calls=valid_tool_calls, usage=usage, thinking=_thinking,
        )
    except Exception:
        pass

    return st.raw_text, valid_tool_calls, usage, st.gathered


# 纯读、无副作用、无确认卡的工具——同一轮里有多个这种时可并行 invoke 提速 IO
# （写类 / run_command / 生图 / MCP / 改状态的都不在内，保持串行，避免确认卡冲突与副作用竞争）
PARALLEL_SAFE_TOOLS = {
    "read_file", "search_in_file", "search_files", "list_directory", "code_map",
    "find_definition", "find_references",  # jedi 代码导航，纯读无副作用
    "find_tests", "related_files",  # 测试发现 / 关联文件，纯读无副作用
    "git_diff", "git_log", "git_status", "check_code", "read_background_output",
    "list_background_commands", "fetch_url", "web_search",
    "get_project_instructions",  # 读取项目规则文件，只读
}


# 这些只读工具所有参数都有默认值，空参（{}）调用合法（用默认值）。必须和 _execute_tool 的
# 空参保护协同：模型空参调它们是对的，别误判成"生成中断"拦掉——尤其它们被并行预取成功后，
# 回放阶段若被拦，预取结果会被丢弃（Codex review 4 的 P2）。read_file/search_files 等有必填
# 参数的工具不在此列：它们空参确实是错误调用，该拦下并请模型重新完整调用。
NO_ARG_OK_TOOLS = {
    "list_directory", "code_map", "git_diff", "git_log", "git_status", "list_background_commands",
    "get_project_instructions",  # 所有参数均可选，空参合法（默认读当前项目）
    "find_tests",  # 所有参数均可选，空参合法（默认项目根）
}


def _can_parallel(tool_calls):
    """同一轮的多个工具能否并行执行：>1 个、全是只读白名单工具、且非 Plan / 遥控模式。
    混入写类/需确认/改状态的工具，或 Plan / 遥控模式，一律退回串行。
    每个工具还要么带参数、要么是 NO_ARG_OK_TOOLS（默认参数齐全、空参合法）——
    这层和 _execute_tool 的空参保护用同一判据：read_file/search_files 等必填参数工具
    用 {} 调是错误调用，不该进并行预取（否则预取必失败、白白产生失败日志，回放阶段还要
    再被空参保护拦一次），让它走串行被拦下、拿到清晰的重试指引。"""
    return (
        len(tool_calls) > 1
        and getattr(state, "agent_mode", "act") != "plan"
        and not getattr(state, "remote_session", False)
        and all(
            tc.get("name") in PARALLEL_SAFE_TOOLS
            and (bool(tc.get("args")) or tc.get("name") in NO_ARG_OK_TOOLS)
            for tc in tool_calls
        )
    )


def _parallel_invoke(tool_calls):
    """并行 invoke 多个只读工具，返回 {index: result}。子线程绑定到 worker 当前的会话，
    让工具内的 _project_cwd / current_session 仍用对会话（不串到 active）。"""
    import concurrent.futures as _cf
    from . import session as _session
    _worker_sess = _session.current_session()
    tmap = get_tool_map()

    def _one(tc):
        _session.bind_thread(_worker_sess)
        try:
            return tmap[tc["name"]].invoke(tc.get("args") or {})
        except Exception as e:
            # 对齐串行 _execute_tool 的失败日志（line ~1049）；并行子线程里不碰 notify/UI
            # （线程安全），但必须留日志，否则并行工具失败时无从排查。exc_info 给出 traceback。
            logger.error(f"工具 {tc.get('name')} 执行失败（并行）: {e}", exc_info=True)
            return f"工具执行失败: {e}"
        finally:
            _session.unbind_thread()

    results = {}
    with _cf.ThreadPoolExecutor(max_workers=min(len(tool_calls), 6)) as ex:
        futs = {ex.submit(_one, tc): i for i, tc in enumerate(tool_calls)}
        for fut in _cf.as_completed(futs):
            results[futs[fut]] = fut.result()
    return results


def _execute_tool(tc, ui, _preinvoked=None):
    name = tc.get("name", "") if isinstance(tc, dict) else tc["name"]
    args = tc.get("args", {}) if isinstance(tc, dict) else tc["args"]
    call_id = tc.get("id", name) if isinstance(tc, dict) else tc["id"]

    if name not in get_tool_map():
        ui.show_message(f"\n⚠️ 未知工具: {name}\n", "tool_tag")
        state.chat_history.append(ToolMessage(content=f"未知工具: {name}", tool_call_id=call_id))
        logger.warning(f"未知工具: {name}")
        return

    # Plan 模式硬拦截：AI 不听话非要调写工具时，挡住并把拒绝信息回灌给 AI
    if getattr(state, "agent_mode", "act") == "plan" and name not in PLAN_MODE_READONLY_TOOLS:
        ui.show_message(f"\n⛔ Plan 模式拒绝调用 {name}（只允许调研类工具）\n", "tool_tag")
        state.chat_history.append(ToolMessage(
            content=(
                f"已拒绝执行 `{name}`：当前是 **Plan 模式**，只能用只读工具（"
                f"{', '.join(sorted(PLAN_MODE_READONLY_TOOLS))}）。"
                "请先给用户一个完整方案，让用户切回 Act 模式后再实际执行。"
            ),
            tool_call_id=call_id,
        ))
        logger.info(f"Plan 模式拒绝调用 {name}")
        return

    # 遥控安全分级拦截（按 config 的 remote_control.mode）：
    #   chat_only     —— 禁所有工具，纯对话
    #   safe_readonly —— 只放行 read/search_in_file/list，且过敏感文件黑名单
    #   unrestricted  —— 不拦
    if getattr(state, "remote_session", False):
        from .config import REMOTE_MODE, REMOTE_ALLOW_WEB
        # 联网查询独立开关:fetch_url / web_search 是只读网络工具(fetch_url 有 SSRF 防护、
        # 拒内网/本机/云元数据)。默认仍不给远程(网络外发保守),但 allow_web_search=true 时
        # 不论 chat_only / safe_readonly 都放行它俩——只加网络查询,不碰文件读写。
        if name in ("fetch_url", "web_search") and REMOTE_ALLOW_WEB:
            pass
        elif REMOTE_MODE == "chat_only":
            ui.show_message(f"\n🔒 远程遥控（纯对话模式），拒绝工具 {name}\n", "tool_tag")
            state.chat_history.append(ToolMessage(
                content=(
                    f"已拒绝 `{name}`：当前遥控为**纯对话模式**（chat_only），"
                    "禁用所有工具以防信息外流。读写文件 / 命令请在 PC 操作。"
                ),
                tool_call_id=call_id,
            ))
            logger.info(f"遥控 chat_only 拒绝工具: {name}")
            return
        elif REMOTE_MODE == "safe_readonly":
            # 跨目录搜(search_files)无法逐一过滤黑名单 → 直接禁；只放行可定位单文件的只读
            _RO_ALLOWED = {"read_file", "search_in_file", "list_directory"}
            if name not in _RO_ALLOWED:
                ui.show_message(f"\n🔒 远程遥控（只读模式），拒绝 {name}\n", "tool_tag")
                state.chat_history.append(ToolMessage(
                    content=(
                        f"已拒绝 `{name}`：当前遥控为**只读模式**（safe_readonly），"
                        f"只允许 {', '.join(sorted(_RO_ALLOWED))}。"
                        "写文件 / 命令 / 跨目录搜请在 PC 操作。"
                    ),
                    tool_call_id=call_id,
                ))
                logger.info(f"遥控 safe_readonly 拒绝非只读工具: {name}")
                return
            target = args.get("path") or args.get("file") or ""
            if _hits_remote_blocklist(target):
                ui.show_message("\n🔒 该文件在遥控敏感黑名单，拒绝远程读取\n", "tool_tag")
                state.chat_history.append(ToolMessage(
                    content=(
                        f"已拒绝读取 `{target}`：该文件在遥控敏感黑名单"
                        "（config / 密钥 / 长期记忆等），不允许远程读取以防泄露。"
                    ),
                    tool_call_id=call_id,
                ))
                logger.info(f"遥控黑名单拒绝读取: {target}")
                return
        # unrestricted: 不拦，照常往下执行

    # 空参数保护：模型流式生成 tool_call 时 arguments 没传全 / JSON 解析失败
    # （常见于 new_string 很长、或服务端流式不稳），会 fallback 成 {}。直接 invoke
    # 只会撞一坨 pydantic 校验错、对模型没指引。这里给清晰中文让它重新完整调用。
    # NO_ARG_OK_TOOLS（list_directory/code_map/git_diff/git_log/list_background_commands）
    # 所有参数都有默认值、空参合法，放行；MCP 工具的空参数由远程 server 自己校验，也放行。
    if not args and name not in NO_ARG_OK_TOOLS and not name.startswith("mcp_"):
        ui.show_message(f"\n⚠️ {name} 的参数为空（生成中断），已请模型重试\n", "tool_result")
        state.chat_history.append(ToolMessage(
            content=(
                f"工具 `{name}` 的调用参数为空——可能是流式生成中断或参数过长没传完整。"
                "请重新调用该工具，一次性完整给出所有必填参数。"
            ),
            tool_call_id=call_id,
        ))
        logger.warning(f"工具 {name} 参数为空，跳过执行并请模型重试")
        return

    display_name = TOOL_DISPLAY_NAMES.get(name, f"🔧 {name}")

    if name in ("read_file", "write_file", "append_file", "edit_file"):
        detail = args.get("path", "")
    elif name == "list_directory":
        detail = args.get("path", ".")
    elif name == "run_command":
        detail = args.get("command", "")
    elif name == "search_in_file":
        detail = f"{args.get('path', '')} → '{args.get('keyword', '')}'"
    else:
        detail = _pretty_args(args)

    ui.show_message(f"\n{display_name}", "tool_tag")
    ui.show_message(f"  {detail}\n", "tool_detail")
    logger.info(f"执行工具: {name}({detail})")

    # ── MCP / Git 写操作执行前确认 ──
    _git_write_tools = {"git_stage", "git_unstage", "git_commit"}
    if name.startswith("mcp_") or name in _git_write_tools:
        _ui = ui if hasattr(ui, "confirm_command") else getattr(state, "ui_ref", None)
        if _ui is None and name in _git_write_tools:
            state.chat_history.append(ToolMessage(
                content=f"已拒绝执行 `{name}`：当前没有可用的用户确认界面。",
                tool_call_id=call_id,
            ))
            logger.warning(f"无确认界面，拒绝 Git 写工具: {name}")
            return
        if _ui is not None:
            if name in _git_write_tools:
                _msg = build_git_write_confirmation(name, args)
            else:
                _display = TOOL_DISPLAY_NAMES.get(name, name)
                _msg = (
                    f"将调用 MCP 工具 {_display}，参数:\n"
                    + _pretty_args(args)
                )
            allowed, user_feedback = _ui.confirm_command(_msg)
            if not allowed:
                _reject_msg = f"已拒绝：用户不允许执行工具 `{name}`。"
                if user_feedback:
                    _reject_msg += f"\n用户补充说明：{user_feedback}"
                logger.info(f"用户拒绝执行工具: {name}")
                state.chat_history.append(ToolMessage(
                    content=_reject_msg,
                    tool_call_id=call_id,
                ))
                return

    try:
        # 并行预取过结果就直接用（agent_loop 对多个只读工具并行 invoke 后，按序走这里渲染+append）
        result = _preinvoked if _preinvoked is not None else get_tool_map()[name].invoke(args)
    except Exception as e:
        result = f"工具执行失败: {e}"
        logger.error(f"工具 {name} 执行失败: {e}")
        # Telegram 通知：工具失败
        try:
            from .notify import notify as _notify
            _notify("error", f"工具失败: {name}", str(e)[:300], "tool_error")
        except Exception:
            pass

    # 流式工具（run_command）执行过程中已经把每行 stdout 实时 push 到 UI 了；
    # 这里若再 push 一次 result，会把所有输出在末尾**重复显示一遍**。
    # 所以对流式工具跳过 UI display；AI 那边仍然拿到完整 result。
    if name not in STREAMING_TOOLS:
        display_result = str(result)
        if len(display_result) > TOOL_RESULT_PREVIEW_CHARS:
            display_result = display_result[:TOOL_RESULT_PREVIEW_CHARS] + "\n... [结果已截断]"
        ui.show_message(f"{display_result}\n", "tool_result")
    logger.info(f"工具结果: {str(result)[:200]}...")

    # 工具结束后重置 AI 回复跟踪，避免最终 markdown 渲染把工具结果覆盖
    ui.show_message("", "reset_ai_reply")

    state.chat_history.append(ToolMessage(content=str(result), tool_call_id=call_id))
    # 自动任务台账（M1）：记"已改文件/已跑命令"，逐轮注入 system prompt、不受压缩影响
    try:
        state.record_tool_in_ledger(state.task_ledger, name, args, result)
    except Exception:
        pass
    # 工具结果已固化到 chat_history → 清 render_log（切回靠 _redraw_chat 画 ToolMessage）
    from . import session as _session
    _session.seal_render_log()
