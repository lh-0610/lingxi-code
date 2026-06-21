"""Claude Code 模式：通过 subprocess 调用本地 `claude` CLI。

跟其它模型走 langchain stream 不一样——Claude Code 不能多轮，要手动把
之前的对话拼成大 prompt，再让 CLI 一次性吐 stream-json 事件回来解析。
"""
import json as _json
import os
import signal
import subprocess
import tempfile
import threading as _threading
import time

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from . import state
from .paths import logger
from .config import CLAUDE_CODE_MODEL, CLAUDE_CODE_SKIP_PERMISSIONS
from .memory import save_session, maybe_generate_session_title
from .roles import get_external_agent_context, get_current_role_name


def _kill_proc_tree(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            return
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _build_claude_cmd(*, agent_mode, skip_permissions, model, system_prompt_file):
    """构造 claude CLI 参数列表（纯函数，便于测权限映射）。

    权限模式（三者互斥，分支只给其一；claude -p 非交互必须显式给，否则遇写操作会挂起）：
      灵犀 Plan        → --permission-mode plan：claude 只读探索、不改源文件（内核级强制）
      灵犀 Act + skip开 → --dangerously-skip-permissions：绕过全部检查、全自动
      灵犀 Act + skip关 → --permission-mode acceptEdits：自动批准编辑+常见文件命令、不挂起

    **prompt 一律走 stdin、system prompt 走 --append-system-prompt-file**，命令行只留 flag +
    文件路径——避开 Windows CreateProcess ~32K 命令行长度限制（项目规则可达 40K + 记忆 +
    角色卡，内联到命令行必然可能超限、导致 Claude Code 启动失败）。--append-system-prompt-file
    是【追加】（保留 claude 自带编码提示），不像 --system-prompt 整个替换。
    """
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    if agent_mode == "plan":
        cmd += ["--permission-mode", "plan"]
    elif skip_permissions:
        cmd += ["--dangerously-skip-permissions"]
    else:
        cmd += ["--permission-mode", "acceptEdits"]
    if model:
        cmd += ["--model", model]
    if system_prompt_file:
        cmd += ["--append-system-prompt-file", system_prompt_file]
    # 用户 prompt 不作位置参数，由调用方写进 stdin（claude -p 默认从 stdin 读文本 prompt）。
    return cmd


def claude_code_loop(ui):
    """通过 Claude Code CLI 处理消息"""

    # 构造带历史的 prompt（claude -p 不支持多轮，要手动拼）
    def _msg_text(msg):
        c = msg.content
        if isinstance(c, list):
            texts = [p["text"] for p in c if isinstance(p, dict) and p.get("type") == "text"]
            return texts[0] if texts else ""
        return c

    def _msg_has_images(msg):
        c = msg.content
        if isinstance(c, list):
            return any(isinstance(p, dict) and p.get("type") in ("image_url", "image") for p in c)
        return False

    history_parts = []
    last_user_msg = ""
    for msg in state.chat_history:
        if isinstance(msg, SystemMessage):
            continue
        if isinstance(msg, HumanMessage):
            text = _msg_text(msg)
            history_parts.append(f"[用户]: {text}")
            last_user_msg = text
        elif isinstance(msg, AIMessage) and msg.content:
            history_parts.append(f"[你之前的回复]: {msg.content}")

    if not last_user_msg:
        return

    # 检查最新用户消息是否包含图片
    has_images = False
    for msg in reversed(state.chat_history):
        if isinstance(msg, HumanMessage):
            has_images = _msg_has_images(msg)
            break

    # 如果有多轮历史，把之前的对话作为上下文拼进去
    if len(history_parts) > 1:
        context = "\n\n".join(history_parts[:-1])
        full_prompt = f"以下是我们之前的对话：\n\n{context}\n\n现在请继续回复用户的最新消息：\n\n{last_user_msg}"
    else:
        full_prompt = last_user_msg

    display_name = get_current_role_name() or "Claude Code"
    ui.show_message("\n", "spacer")
    ui.show_message(f"{display_name}\n", "ai_label")
    # Claude Code 的 -p 模式没有受支持的图片传入方式，明确告知而非静默丢弃图片。
    if has_images:
        ui.show_message("（注：Claude Code 模式暂不支持图片输入，本条仅按文本处理）\n", "ai_msg")
    ui.show_message(f"等待{display_name}回复...\n", "thinking_indicator")

    logger.info(f"Claude Code 调用: {last_user_msg[:100]}")

    # 心跳计时（提前定义，避免 Popen 异常时 NameError）
    heartbeat_stop = _threading.Event()
    heartbeat_started = False
    proc = None
    sys_prompt_file = None
    try:
        # 精简上下文：只给角色/项目规则/记忆，不注入灵犀自己的工具说明（claude 有自己的
        # 工具，注入会让它调用不存在的工具）。Plan/Act 的只读约束交给 --permission-mode 强制。
        # 写进临时文件用 --append-system-prompt-file 传，prompt 走 stdin —— 避开 32K 命令行限制。
        system_prompt = get_external_agent_context()
        if system_prompt:
            _tf = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", prefix="lingxi_cc_sys_",
                delete=False, encoding="utf-8")
            _tf.write(system_prompt)
            _tf.close()
            sys_prompt_file = _tf.name
        cmd = _build_claude_cmd(
            agent_mode=getattr(state, "agent_mode", "act"),
            skip_permissions=CLAUDE_CODE_SKIP_PERMISSIONS,
            model=CLAUDE_CODE_MODEL,
            system_prompt_file=sys_prompt_file,
        )
        stdin_data = full_prompt   # 用户 prompt 一律走 stdin，绝不进命令行

        # 在项目根/worktree 里跑 claude CLI，和其它工具的 cwd 语义一致；
        # 否则它会落到灵犀进程的启动目录（可能是 exe 所在处），无视当前项目。
        from .tools_common import _project_cwd
        run_cwd = _project_cwd() or None

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            cwd=run_cwd,
            start_new_session=(os.name != "nt"),
        )
        if stdin_data:
            proc.stdin.write(stdin_data)
        proc.stdin.close()

        stream_start = time.time()

        def _heartbeat():
            while not heartbeat_stop.is_set():
                elapsed = int(time.time() - stream_start)
                ui.update_thinking_indicator(f"{display_name}思考中... ({elapsed}s)\n")
                heartbeat_stop.wait(1)

        hb = _threading.Thread(target=_heartbeat, daemon=True)
        hb.start()
        heartbeat_started = True

        # 解析 stream-json 事件
        full_text = ""
        thinking_tokens = 0   # CLI 只上报思考 token 估算，不返回思维链原文
        diagnostic_lines = []
        indicator_removed = False
        for line in proc.stdout:
            if state.stop_flag:
                _kill_proc_tree(proc)
                break
            line = line.strip()
            if not line:
                continue
            try:
                event = _json.loads(line)
            except _json.JSONDecodeError:
                diagnostic_lines.append(line)
                continue

            etype = event.get("type")

            # 思考阶段：CLI 持续上报思考 token 估算（但不返回思维链原文）
            if etype == "system" and event.get("subtype") == "thinking_tokens":
                thinking_tokens = max(thinking_tokens, event.get("estimated_tokens", 0) or 0)
                continue

            # 首个"内容"事件才撤掉等待指示——思考阶段保留心跳，让用户看到在思考
            if not indicator_removed and etype in ("assistant", "user", "result"):
                indicator_removed = True
                heartbeat_stop.set()
                ui.remove_thinking_indicator()

            if etype == "assistant":
                # 助手消息（包含文本/工具调用）
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text", "")
                        full_text += text
                        ui.show_message(text, "ai_msg")
                    elif btype == "thinking":
                        # 思考内容。注意：claude CLI 默认不返回思维链原文，
                        # 只给一个带 signature 的空 thinking 块 + thinking_tokens 计数。
                        # 此时 thinking == ""，显示"已思考"活动而非静默留白。
                        thinking = block.get("thinking", "")
                        if thinking:
                            ui.show_message(f"{thinking}\n", "think_msg")
                        elif block.get("signature") or thinking_tokens:
                            n = f"约 {thinking_tokens} tokens" if thinking_tokens else "思维链未公开"
                            ui.show_message(
                                f"💭 已思考（{n}）—— Claude CLI 未返回思维链原文\n",
                                "think_msg",
                            )
                    elif btype == "redacted_thinking":
                        # 加密脱敏的思考块（安全原因），同样给可见标记而非留白
                        ui.show_message("💭 已思考（内容已脱敏）\n", "think_msg")
                    elif btype == "tool_use":
                        # 工具调用
                        tool_name = block.get("name", "?")
                        tool_input = block.get("input", {})
                        input_preview = _json.dumps(tool_input, ensure_ascii=False)[:80]
                        ui.show_message(f"\n🔧 {tool_name}  {input_preview}\n", "tool_tag")
            elif etype == "user":
                # 工具结果
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "tool_result":
                        result = block.get("content", "")
                        if isinstance(result, list):
                            result = " ".join(r.get("text", "") for r in result if isinstance(r, dict))
                        preview = str(result)[:200]
                        ui.show_message(f"{preview}\n", "tool_result")
            elif etype == "result":
                # 最终结果
                # 提取 Claude Code 返回的 token 用量
                usage_data = event.get('usage', {})
                input_t = usage_data.get('input_tokens', 0) or 0
                output_t = usage_data.get('output_tokens', 0) or 0
                total_t = input_t + output_t
                if total_t > 0:
                    round_usage = {'input': input_t, 'output': output_t, 'total': total_t}
                    state.session_token_usage['input'] += input_t
                    state.session_token_usage['output'] += output_t
                    state.session_token_usage['total'] += total_t
                    ui.show_token_usage(state.session_token_usage.copy(), round_usage)
                    logger.info(f"Token 用量 - 输入: {input_t}, 输出: {output_t}, 总计: {total_t}")

        heartbeat_stop.set()
        if not indicator_removed:
            ui.remove_thinking_indicator()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_proc_tree(proc)
            proc.wait(timeout=5)

        if proc.returncode != 0 and not state.stop_flag:
            err = "\n".join(diagnostic_lines).strip()
            if not err:
                err = f"Claude Code exited with code {proc.returncode}"
            logger.error(f"Claude Code 错误: {err}")
            ui.show_message(f"\n⚠️ {err}\n", "ai_msg")

        clean_text = full_text.strip()
        if clean_text:
            state.chat_history.append(AIMessage(content=clean_text))
            logger.info(f"Claude Code 回复完成: {clean_text[:100]}...")

        save_session()
        maybe_generate_session_title()

    except FileNotFoundError:
        heartbeat_stop.set()
        ui.remove_thinking_indicator()
        ui.show_message("\n⚠️ 未找到 claude 命令，请确认 Claude Code CLI 已安装\n", "ai_msg")
        logger.error("claude 命令未找到")
    except Exception as e:
        if heartbeat_started:
            heartbeat_stop.set()
        ui.remove_thinking_indicator()
        logger.error(f"Claude Code 异常: {e}", exc_info=True)
        ui.show_retry(str(e)[:100])
    finally:
        _kill_proc_tree(proc)
        if sys_prompt_file:
            try:
                os.remove(sys_prompt_file)   # 清理 system prompt 临时文件
            except OSError:
                pass
