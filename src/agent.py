"""主入口 + 启动初始化 + agent_loop 主循环。

这个模块既是 src/ 的"facade"，也持有 agent_loop 主体。

设计要点：
- 全局可变状态（current_model_index / chat_history / stop_flag 等）真身在 state.py
- 通过模块级 `__getattr__` 把读取代理到 state，让 ui.py 继续用 `agent.X` 不报错
- 写入 state 必须用 `state.X = ...`（不要 `agent.X = ...`，那只会污染 agent 模块本身）
"""
import re
import contextlib
import threading as _threading

from langchain_core.messages import HumanMessage, SystemMessage

from . import state as _state
from . import state  # 公开给 ui.py 直接用：ui 里所有"写入"改成 src.state.X = ...
from .paths import logger
# ⚠️ 本模块是 facade(门面):下面很多名字在 agent.py 内部"未使用",但它们是**出口**——
# UI 层(chat_window/sidebar/header)统一经 `agent.X` 访问(见 CLAUDE.md 架构说明)。
# 用 noqa: F401 标记,**任何 linter / AI 清理"未使用 import"都不许删**,删了 UI 运行时直接
# AttributeError(发图视觉桥接/角色卡切换/会话侧栏全炸,且单测覆盖不到这些 UI 路径)。
from .models import (
    MODEL_LIST,
    check_ollama,
    _create_llm,
    get_model_config_issues,        # noqa: F401  facade 出口 → chat_window
    has_usable_model,               # noqa: F401  facade 出口 → chat_window 首次上手引导
    current_model_supports_vision,  # noqa: F401  facade 出口 → chat_window 视觉桥接
    get_vision_model_index,         # noqa: F401  facade 出口 → chat_window 视觉桥接
    describe_images_with_vision,    # noqa: F401  facade 出口 → chat_window 视觉桥接
)
from .roles import (
    SYSTEM_PROMPT,
    get_system_prompt as _get_system_prompt,  # 内部用
    get_current_role_name,
    get_current_role_path,          # noqa: F401  facade 出口 → header 角色卡
    set_role_card,                  # noqa: F401  facade 出口 → header 角色卡
    clear_role_card,                # noqa: F401  facade 出口 → header 角色卡
    load_saved_role_card,
)
from .memory import (
    save_session,
    load_session,                   # noqa: F401  facade 出口 → sidebar
    list_sessions,                  # noqa: F401  facade 出口 → sidebar
    delete_session,                 # noqa: F401  facade 出口 → sidebar
    reset_history,                  # noqa: F401  facade 出口 → header/sidebar
    maybe_generate_session_title,
    move_sessions_to_no_project,    # noqa: F401  facade 出口 → sidebar 删项目
    _build_ai_message,
)
from .tools import ALL_TOOLS, build_all_tools  # noqa: F401  ALL_TOOLS 为 facade 出口
from .streaming import _stream_with_tools, _execute_tool, _extract_thinking
from .claude_code import claude_code_loop as _claude_code_loop
from . import session as _session_mod
from . import projects as _projects

_BOUND_LLM_CACHE = {}


# ══════════════════════════════════════
# 模块级读代理：保持 `agent.stop_flag` 等读取兼容
# ══════════════════════════════════════
# ui.py 里大量 `agent.current_model_index` / `agent.chat_history` / `agent.stop_flag`
# 通过这个 __getattr__ 自动从 state 模块取最新值，无需到处改 ui.py。
# 但**写入**仍然要写 `state.X = ...`（agent.X = ... 不会影响 state）。
def __getattr__(name):
    if hasattr(_state, name):
        return getattr(_state, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ══════════════════════════════════════
# 模型切换
# ══════════════════════════════════════

def resolve_bound_llm(session):
    """按 session 的 model_index + reasoning 取 / 建 (llm, llm_with_tools)，按 model 缓存。

    多会话并发时各 worker 用各自会话 model 的实例，互不串台——streaming 的并发路径走
    这个，不读全局 state.llm_with_tools（那是单值，会被别的会话的切换覆盖）。
    """
    mi = session.current_model_index
    _, mtype, model_id, supports_think = MODEL_LIST[mi]
    effective_reasoning = bool(session.reasoning_enabled and supports_think)
    key = (mi, mtype, model_id, effective_reasoning)
    if key not in _BOUND_LLM_CACHE:
        llm = _create_llm(mi, effective_reasoning)
        _BOUND_LLM_CACHE[key] = (llm, llm.bind_tools(build_all_tools()))
    return _BOUND_LLM_CACHE[key]


def _activate_llm():
    """把全局 state.llm / llm_with_tools 设为【当前会话】model 对应实例（主线程 / 调试用）。"""
    from . import session as _session
    state.llm, state.llm_with_tools = resolve_bound_llm(_session.current_session())


def switch_model(index):
    """切换模型"""
    state.current_model_index = index
    _activate_llm()
    name = MODEL_LIST[index][0]
    logger.info(f"切换模型: {name}")


def set_reasoning(enabled):
    """切换思考模式"""
    state.reasoning_enabled = enabled
    _activate_llm()
    logger.info(f"思考模式: {'开启' if enabled else '关闭'}")


# ══════════════════════════════════════
# 启动初始化
# ══════════════════════════════════════

# 1. 默认模型 + LLM
#    启动默认模型由 config 的 default_model_id 决定（默认 mimo-v2.5-pro）。
#    按 model_id 匹配而非写死 index——BUILTIN 顺序随 config 变，写死 index 不稳。
#    找不到该 model_id（如用户删了它）时退回列表第一个。
def _resolve_default_model_index():
    from .config import DEFAULT_MODEL_ID
    if DEFAULT_MODEL_ID:
        for i, (_, _, mid, _) in enumerate(MODEL_LIST):
            if mid == DEFAULT_MODEL_ID:
                return i
    return 0


state.current_model_index = _resolve_default_model_index()
_activate_llm()

# 1.5 MCP 守护线程（后台启动，不影响 UI 等待）
def _init_mcp_bg():
    try:
        from .mcp_client import init_mcp
        init_mcp()
        # 清掉旧的（启动时绑的、不含 MCP 的）bound 缓存，并**立即重新绑定**当前模型。
        # 注意：agent_loop 直接用 state.llm_with_tools、不会自己调 _activate_llm，
        # 所以光 clear 缓存不够——必须主动 _activate_llm() 把 state.llm_with_tools
        # 换成带 MCP 工具的版本，否则模型一直看不到 MCP 工具（除非用户手动切一次模型）。
        _BOUND_LLM_CACHE.clear()
        _activate_llm()
        logger.info("MCP 工具已就绪")
    except Exception as e:
        logger.warning(f"MCP 初始化失败: {e}", exc_info=True)


_threading.Thread(target=_init_mcp_bg, daemon=True).start()

# 2. 对话历史（先用纯 SYSTEM_PROMPT 占位）
state.chat_history = [SystemMessage(content=SYSTEM_PROMPT)]
state.current_session_id = None
state.current_session_title = None

# 3. 启动时恢复角色卡 + 当前项目，合并到系统提示词
load_saved_role_card()
state.current_project = _projects.get_current()
if isinstance(state.chat_history[0], SystemMessage):
    # 不管有没有角色卡和项目，统一让 get_system_prompt 拼好返回
    state.chat_history[0] = SystemMessage(content=_get_system_prompt())


# ══════════════════════════════════════
# Agent 循环（全流式）
# ══════════════════════════════════════

def agent_loop(ui):
    try:
        from .verification import reset_verification as _v_reset
        with contextlib.suppress(Exception):
            _v_reset(_session_mod.current_session().verification)

        mtype = MODEL_LIST[state.current_model_index][1]
        model_name = MODEL_LIST[state.current_model_index][0]

        # Claude Code 模式：直接调 CLI
        if mtype == "claude-code":
            _claude_code_loop(ui)
            return

        # 本地模型需要检测 Ollama 服务
        if mtype == "ollama" and not check_ollama():
            ui.show_message("\n⚠️ 无法连接 Ollama 服务，请先运行 ollama serve\n", "ai_msg")
            from .config import OLLAMA_BASE_URL
            logger.error(f"Ollama 服务不可用: {OLLAMA_BASE_URL}")
            return

        round_i = -1
        # 角色卡存在时用角色名替代模型名
        display_name = get_current_role_name() or model_name

        # 完成闸门：外层循环（验证间隙时重跑一轮）
        _gate_active = True
        while _gate_active:
            round_i += 1

            if state.stop_flag:
                logger.info("用户停止生成")
                break

            # 只在第一轮显示标签
            if round_i == 0:
                ui.show_message("\n", "spacer")
                ui.show_message(f"{display_name}\n", "ai_label")

            logger.info(f"第 {round_i+1} 轮流式调用...")

            # 全流式调用，实时显示思考过程 + 收集 tool_calls（Ollama 解析错误自动重试）
            retries = 0
            while True:
                try:
                    raw_text, tool_calls, round_usage, gathered = _stream_with_tools(ui)
                    break
                except Exception as stream_err:
                    retries += 1
                    err_msg = str(stream_err)
                    if retries <= 2 and ("XML syntax error" in err_msg or "ResponseError" in err_msg):
                        logger.warning(f"Ollama 解析错误，第 {retries} 次重试: {err_msg[:100]}")
                        ui.show_message(f"\n⚠️ 模型输出格式异常，正在重试({retries}/2)...\n", "tool_result")
                    else:
                        raise

            # 累计本轮 token 用量并通知 UI
            if round_usage and round_usage['total'] > 0:
                state.session_token_usage['input'] += round_usage['input']
                state.session_token_usage['output'] += round_usage['output']
                state.session_token_usage['total'] += round_usage['total']
                ui.show_token_usage(state.session_token_usage.copy(), round_usage)
                logger.info(f"Token 用量 - 输入: {round_usage['input']}, 输出: {round_usage['output']}, 总计: {round_usage['total']}")

            if state.stop_flag:
                # 被中断，保存已有内容（保留 thinking blocks 以便回传）
                clean = re.sub(r"<think>.*?</think>|<thought>.*?</thought>", "", raw_text, flags=re.DOTALL).strip()
                if clean or (gathered is not None and isinstance(gathered.content, list)):
                    state.chat_history.append(_build_ai_message(gathered, clean, []))
                break

            clean_text = re.sub(r"<think>.*?</think>|<thought>.*?</thought>", "", raw_text, flags=re.DOTALL).strip()

            if tool_calls:
                logger.info(f"工具调用: {[tc['name'] for tc in tool_calls]}")
                # 用 _build_ai_message 构造 AIMessage，保留 thinking blocks 供下轮回传
                ai_msg = _build_ai_message(gathered, clean_text, tool_calls)
                state.chat_history.append(ai_msg)
                if clean_text:
                    # 中间轮：只渲染 markdown，不朗读（朗读留给最终回复）
                    ui.render_final_markdown(clean_text, speak=False)
                # 该轮 AI 已固化到 chat_history → 清 render_log（切回靠 _redraw_chat 画它）
                from . import session as _session
                _session.seal_render_log()

                # 同一轮有多个【只读无副作用】工具 → 先并行 invoke 取结果（IO 并行提速），
                # 再按 tool_calls 原顺序串行渲染 + append（保证不交错、ToolMessage 顺序对）。
                # 混入写类 / 需确认 / 改状态的工具，或 Plan / 遥控模式，一律走串行（现状）。
                # 判定逻辑抽到 streaming._can_parallel（与 PARALLEL_SAFE_TOOLS/_parallel_invoke
                # 集中一处、可单元测）。注意它不要求 args 非空——带默认参数的只读工具用 {} 调也合法。
                from .streaming import _parallel_invoke, _can_parallel
                _pre = _parallel_invoke(tool_calls) if _can_parallel(tool_calls) else {}
                for i, tc in enumerate(tool_calls):
                    if state.stop_flag:
                        break
                    _execute_tool(tc, ui, _preinvoked=_pre.get(i))

                # ── 自动修复循环：工具失败后注入诊断 + 重试提示 ──
                # 当 run_tests / check_code 等返回失败时，自动注入一条 HumanMessage
                # 提示模型诊断原因并重新调用工具，最多修复 _MAX_REPAIR_ROUNDS 轮。
                if not state.stop_flag and state.agent_mode != "plan":
                    try:
                        from .verification import check_repair_allowed, inject_repair_prompt
                        _verification = _session_mod.current_session().verification
                        for tc in reversed(tool_calls):
                            tool_name = tc.get("name", "")
                            tool_id = tc.get("id", "")
                            # 从 chat_history 找刚执行的 ToolMessage
                            tool_output = ""
                            for msg in reversed(state.chat_history):
                                if (hasattr(msg, "tool_call_id")
                                        and msg.tool_call_id == tool_id):
                                    tool_output = msg.content or ""
                                    break
                            allowed, _reason = check_repair_allowed(
                                _verification, tool_name, tool_output,
                            )
                            if allowed:
                                prompt = inject_repair_prompt(_verification)
                                state.chat_history.append(HumanMessage(content=prompt))
                                try:
                                    ui.show_message(
                                        "\n⚠️ 验证失败，正在自动诊断并尝试修复…\n",
                                        "tool_result",
                                    )
                                except Exception:
                                    pass
                                break  # 每轮最多注入一条修复提示
                    except Exception as _re_err:
                        logger.debug(f"修复循环检查异常（已忽略）: {_re_err}")

                continue
            else:
                # 纯文本回复，先过完成闸门，再渲染 Markdown 并结束。
                if not clean_text and not raw_text:
                    # 这一轮没收到正文。两种成因要分开报，否则全甩锅"连接被中断"会误导：
                    #  ① 输出额度耗尽(stop_reason=max_tokens / finish_reason=length)：reasoning
                    #     模型思考太长把 max_tokens 吃光，根本没轮到吐正文。raw_text 只装正文不装
                    #     思考，于是为空。F12 表现：output 顶在 max_tokens、状态却是"成功"。
                    #  ② 真·空流：服务端 / 代理 idle 超时切断，一个 chunk 都没来。
                    stop_reason = ""
                    try:
                        _meta = getattr(gathered, "response_metadata", {}) or {}
                        stop_reason = _meta.get("stop_reason") or _meta.get("finish_reason") or ""
                    except Exception:
                        stop_reason = ""
                    if stop_reason in ("max_tokens", "length"):
                        _had_think = bool(_extract_thinking(gathered))
                        ui.show_retry(
                            "模型把本轮输出额度（max_tokens）用尽了"
                            + ("（深度思考占满，没轮到输出正文）" if _had_think else "")
                            + "。可关掉「思考」开关、把问题拆细后重试，或换更高额度的模型。"
                        )
                        logger.warning(
                            f"第 {round_i+1} 轮 output 到达 max_tokens（stop_reason={stop_reason}），"
                            f"无正文，疑似思考耗尽额度"
                        )
                    else:
                        ui.show_retry(
                            "连接被中断（服务端或代理在思考期间关闭了连接）。"
                            "请重试，或换一个模型。"
                        )
                        logger.warning(
                            f"第 {round_i+1} 轮流结束但未收到任何内容（stop_reason={stop_reason or '空'}），"
                            f"疑似服务端 idle timeout 中断"
                        )
                    break
                try:
                    from .verification import get_verification_gaps as _v_gaps
                    _cur_sess = _session_mod.current_session()
                    _verification = getattr(_cur_sess, "verification", None)
                    _gaps = _v_gaps(_verification) if _verification is not None else []
                    if _gaps and not state.stop_flag and getattr(state, "agent_mode", "act") != "plan":
                        if not _verification.get("gate_prompted"):
                            _verification["gate_prompted"] = True
                            _gap_msg = (
                                "[内部验证要求]\n"
                                "你刚才试图结束任务，但本轮改动尚未完成验证：\n"
                                + "\n".join(f"- {g}" for g in _gaps)
                                + "\n\n请继续调用合适的工具验证。若验证不可用或失败，"
                                  "允许最终结束，但必须明确说明原因和风险。"
                            )
                            ui.show_message("\n⚠️ 检测到改动尚未完成验证，正在继续检查…\n", "tool_result")
                            # Anthropic 只允许 SystemMessage 连续出现在历史开头。
                            # 这是对当前任务的内部续作指令，按 HumanMessage 注入最稳妥。
                            state.chat_history.append(HumanMessage(content=_gap_msg))
                            continue
                        clean_text = (
                            "⚠️ 验证仍未完整完成：\n"
                            + "\n".join(f"- {g}" for g in _gaps)
                            + "\n\n"
                            + (clean_text or "")
                        )
                except Exception as _ge:
                    logger.debug(f"验证闸门检查异常（已忽略）: {_ge}")

                state.chat_history.append(_build_ai_message(gathered, clean_text, []))
                if clean_text:
                    ui.render_final_markdown(clean_text)
                logger.info(f"回复完成: {clean_text[:100]}...")
                break

        # save_session 是本地写、很快，留在主流程；标题生成是一次 LLM 调用（可能几十秒），
        # **绝不能**在这里同步跑——否则它会拖在 finished 信号之前，让 is_generating 一直
        # 为 True、UI 卡在"生成中"点不动。挪到后台线程，完事再发信号刷新侧栏标题。
        try:
            save_session()
        except Exception as save_err:
            logger.error(f"保存会话失败: {save_err}", exc_info=True)

        from . import session as _session
        _title_sess = _session.current_session()

        # 子 Agent 是临时会话：不推手机通知、不起标题生成（也不落历史，见 save_session 守卫）
        if not getattr(_title_sess, "is_subagent", False):
            # Telegram 通知：任务完成——不分端都把【完整】回复发回手机（长则分段不截断）。
            # 走 notify_long（尊重 NOTIFY 开关 / 分级 / 节流，用户可在设置里关 done 通知）。
            try:
                from .notify import notify_long as _notify_long
                _notify_long("done", "灵犀回复", clean_text or "(无文本回复)", "agent_done")
            except Exception:
                pass

            # 标题生成在新线程跑，必须 bind 到这个会话，否则 maybe_generate_session_title
            # 读 state.current_session_id/title/model 会落到当时的 active（用户可能已切走），
            # 把标题/项目 tag 写错会话。
            def _gen_title_bg():
                _session.bind_thread(_title_sess)
                try:
                    maybe_generate_session_title()
                    bridge = getattr(ui, "bridge", None)
                    if bridge is not None:
                        bridge.sessions_refresh.emit()  # 标题出来后刷新侧栏（线程安全）
                except Exception as e:
                    logger.error(f"自动生成标题失败: {e}", exc_info=True)
                finally:
                    _session.unbind_thread()

            _threading.Thread(target=_gen_title_bg, daemon=True).start()

    except Exception as e:
        ui.remove_thinking_indicator()
        logger.error(f"agent_loop 异常: {e}", exc_info=True)
        # Telegram 通知：agent 异常
        try:
            from .notify import notify as _notify
            _notify("error", "Agent 异常", str(e)[:300], "agent_error")
        except Exception:
            pass
        # 简化错误信息显示
        err_msg = str(e)
        if "XML syntax error" in err_msg or "ResponseError" in err_msg:
            display_err = "Ollama 模型输出格式异常"
        elif "Connection" in err_msg or "refused" in err_msg:
            display_err = "无法连接 Ollama 服务"
        else:
            display_err = err_msg[:100]
        ui.show_retry(display_err)
