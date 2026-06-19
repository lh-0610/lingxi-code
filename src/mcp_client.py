"""MCP Client —— 与外部 MCP Server 进程通信，把远程 Tools 暴露为 LangChain Tool。

架构：
- 后台守护线程运行一个 asyncio event loop，持有所有 MCP 连接
- UI 主线程调用 ``init_mcp()`` → 后台并发连接各 Server → 全局 MCP_TOOLS 列表就绪
- ``shutdown()`` 停止所有连接（在应用退出时调用）

依赖：``pip install mcp`` （不在 requirements.txt 核心依赖中，可选）
"""

import os
import re
import sys
import json
import time
import asyncio
import threading
import contextlib
from typing import Any, Optional

from . import state
from .paths import logger, CONFIG_PATH


# ═══════════════════════════════════════════════════════
# 全局状态（仅由 MCP 线程写入，主线程只读）
# ═══════════════════════════════════════════════════════

# 可被 bind_tools / TOOL_MAP 使用的 LangChain Tool 列表
MCP_TOOLS: list = []

# MCP 工具的 display name 映射（wrapped_name → 带 🔌 前缀的显示名）
MCP_DISPLAY_NAMES: dict[str, str] = {}

# 唯一的后台 asyncio 事件循环（init_mcp 时创建）
_mcp_loop: Optional[asyncio.AbstractEventLoop] = None

# 每个 server 的 AsyncExitStack 保留到 shutdown（保活 MCP 连接）
_exit_stacks: dict[str, Any] = {}  # name → AsyncExitStack
_sessions: dict[str, Any] = {}     # name → ClientSession

# 每个 server 的 tools 列表（由 _server_loop 在 loop 线程写入，_build_mcp_tools 同步读取）
_server_tools: dict[str, list] = {}  # name → [mcp.ToolInfo, ...]

# 每个 server 的就绪信号（_server_loop 写入后 set）
_server_ready_events: dict[str, asyncio.Event] = {}

# 通知 shutdown 的信号
_shutdown_event: Optional[asyncio.Event] = None


# ═══════════════════════════════════════════════════════
# Windows / env 补丁
# ═══════════════════════════════════════════════════════

def _fix_windows_asyncio():
    """Windows 必须用 ProactorEventLoop（默认 SelectorEventLoop 不支持 subprocess）。"""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def _merge_env(user_env: dict) -> dict:
    """在当前进程环境变量基础上叠加用户指定的 env（PATH 保留）。"""
    merged = os.environ.copy()
    merged.update(user_env or {})
    return merged


def _resolve_stdio_command(command: str) -> str:
    """Resolve path-like stdio commands relative to config.json."""
    raw = os.path.expandvars(os.path.expanduser(str(command)))
    if os.path.isabs(raw):
        return raw

    seps = [sep for sep in (os.sep, os.altsep) if sep]
    if raw.startswith(".") or any(sep in raw for sep in seps):
        return os.path.abspath(os.path.join(os.path.dirname(CONFIG_PATH), raw))

    return raw


# ═══════════════════════════════════════════════════════
# 配置读取
# ═══════════════════════════════════════════════════════

def _load_mcp_config() -> dict:
    """从 config.json 读取 mcp_servers 字典，配置缺失时返回空字典。

    同时兼容 ``mcp_servers`` 和 ``mcp`` 两种 key 名称（优先前者）。
    """
    if not os.path.isfile(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # 总开关：mcp_enabled=false 时完全不连任何 server（缺省视为开，兼容老配置）
        if not cfg.get("mcp_enabled", True):
            logger.info("[MCP] mcp_enabled=false，已禁用 MCP")
            return {}
        return cfg.get("mcp_servers") or cfg.get("mcp") or {}
    except Exception as e:
        logger.error(f"[MCP] 读取配置失败: {e}")
        return {}


# ═══════════════════════════════════════════════════════
# HTTP 传输连接（SSE / Streamable HTTP / Auto）
# ═══════════════════════════════════════════════════════

async def _connect_http(stack, url, transport):
    """建立 HTTP 传输连接，返回 (read_stream, write_stream, actual_transport)。

    当 transport="auto" 时，先尝试 Streamable HTTP，失败则回退 SSE。
    """
    if transport in ("streamable_http", "auto"):
        try:
            from mcp.client.streamable_http import streamablehttp_client
            result = await stack.enter_async_context(streamablehttp_client(url))
            # streamablehttp_client 可能返回 2 或 3 个值
            if len(result) == 3:
                read_stream, write_stream, _ = result
            else:
                read_stream, write_stream = result
            actual = "streamable_http"
            if transport == "auto":
                logger.info(f"[MCP] Auto 检测: {url} → Streamable HTTP")
            return read_stream, write_stream, actual
        except Exception as e:
            if transport != "auto":
                raise
            # auto 模式下 streamable HTTP 失败，回退 SSE
            logger.info(f"[MCP] Streamable HTTP 失败（{e}），回退 SSE ...")

    # SSE（显式指定 或 auto 回退）
    from mcp.client.sse import sse_client
    read_stream, write_stream = await stack.enter_async_context(sse_client(url))
    if transport == "auto":
        logger.info(f"[MCP] Auto 检测: {url} → SSE")
    return read_stream, write_stream, "sse"


# ═══════════════════════════════════════════════════════
# 单个 Server 生命周期（在守护线程 asyncio loop 内运行）
# ═══════════════════════════════════════════════════════

async def _server_loop(name: str, cfg: dict):
    """启动并维持一个 MCP Server 连接，直到 _shutdown_event 被 set。

    支持三种传输方式：
    - stdio（默认）：需要 ``command`` 和 ``args``
    - sse：需要 ``url``（显式指定 SSE 协议）
    - streamable_http：需要 ``url``（显式指定 Streamable HTTP 协议）
    - auto：需要 ``url``（自动尝试 streamable HTTP → 回退 SSE）
    """
    transport = cfg.get("transport", "stdio")

    # ── SSE / Streamable HTTP / Auto ─────────────────────
    if transport in ("sse", "streamable_http", "auto"):
        url = cfg.get("url")
        if not url:
            logger.warning(f"[MCP] {name}: transport={transport} 但缺少 url，跳过")
            _server_ready_events.get(name) and _server_ready_events[name].set()
            return

        logger.info(f"[MCP] 正在连接 server（{transport}）: {name} → {url}")

        stack = contextlib.AsyncExitStack()
        try:
            await stack.__aenter__()
            _exit_stacks[name] = stack

            read_stream, write_stream, actual_transport = await _connect_http(
                stack, url, transport
            )

            from mcp import ClientSession
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            _sessions[name] = session

            # 拉取 Tool 列表并存到模块级 _server_tools（给 _build_mcp_tools 同步读取）
            tools_resp = await session.list_tools()
            _server_tools[name] = tools_resp.tools
            tool_names = [t.name for t in tools_resp.tools]
            logger.info(f"[MCP] ✅ {name}: 已连接（{actual_transport}），{len(tool_names)} 个工具: {tool_names}")

            # 通知就绪
            if name in _server_ready_events:
                _server_ready_events[name].set()

            await _shutdown_event.wait()

        except asyncio.CancelledError:
            logger.info(f"[MCP] {name}: 任务被取消")
        except Exception as e:
            logger.error(f"[MCP] ❌ {name}: 连接失败 - {e}")
        finally:
            # 无论成功失败，都标记就绪（避免 _main 永远等不到）
            if name in _server_ready_events and not _server_ready_events[name].is_set():
                _server_ready_events[name].set()
            with contextlib.suppress(Exception):
                await stack.__aexit__(None, None, None)
            _exit_stacks.pop(name, None)
            _sessions.pop(name, None)
            _server_tools.pop(name, None)
            logger.info(f"[MCP] {name}: 已断开")
        return

    # ── stdio（默认）──────────────────────────────────────
    command = cfg.get("command")
    if not command:
        logger.warning(f"[MCP] {name}: 缺少 command，跳过")
        _server_ready_events.get(name) and _server_ready_events[name].set()
        return

    command = _resolve_stdio_command(command)
    args = cfg.get("args", [])
    env = cfg.get("env", {})

    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    logger.info(f"[MCP] 正在启动 server（stdio）: {name} ({command} {' '.join(args)})")

    stack = contextlib.AsyncExitStack()
    try:
        await stack.__aenter__()
        _exit_stacks[name] = stack

        merged_env = _merge_env(env)
        server_params = StdioServerParameters(command=command, args=args, env=merged_env)

        # 进入 stdio transport（启动子进程，保活直到 stack close）
        read_stream, write_stream = await stack.enter_async_context(
            stdio_client(server_params)
        )

        # 进入 ClientSession
        from mcp import ClientSession
        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        _sessions[name] = session

        # 拉取 Tool 列表并存到模块级 _server_tools
        tools_resp = await session.list_tools()
        _server_tools[name] = tools_resp.tools
        tool_names = [t.name for t in tools_resp.tools]
        logger.info(f"[MCP] ✅ {name}: 已连接，{len(tool_names)} 个工具: {tool_names}")

        # 通知就绪
        if name in _server_ready_events:
            _server_ready_events[name].set()

        # 持续运行，直到应用退出
        await _shutdown_event.wait()

    except asyncio.CancelledError:
        logger.info(f"[MCP] {name}: 任务被取消")
    except Exception as e:
        logger.error(f"[MCP] ❌ {name}: 启动失败 - {e}")
    finally:
        # 无论成功失败，都标记就绪
        if name in _server_ready_events and not _server_ready_events[name].is_set():
            _server_ready_events[name].set()
        # 关闭 session 和 transport（AsyncExitStack.__aexit__）
        with contextlib.suppress(Exception):
            await stack.__aexit__(None, None, None)
        _exit_stacks.pop(name, None)
        _sessions.pop(name, None)
        _server_tools.pop(name, None)
        logger.info(f"[MCP] {name}: 已断开")


# ═══════════════════════════════════════════════════════
# JSON Schema → Pydantic Model 转换
# ═══════════════════════════════════════════════════════

def _schema_to_model(name: str, schema: dict):
    """把 MCP inputSchema（JSON Schema dict）转成 pydantic model。

    遍历 properties，按 type 映射；required 列表决定 Optional 与否。
    """
    from pydantic import create_model, Field

    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    field_defs = {}
    for pname, pschema in props.items():
        py_type = type_map.get(pschema.get("type", "string"), str)
        if pname not in required:
            py_type = (py_type | None)  # Optional
            field_defs[pname] = (py_type, Field(default=None, description=pschema.get("description", "")))
        else:
            field_defs[pname] = (py_type, Field(description=pschema.get("description", "")))
    # pydantic v2: create_model 返回 Model 类
    return create_model(name, **field_defs)


# ═══════════════════════════════════════════════════════
# LangChain Tool 包装器（纯同步，不调 run_coroutine_threadsafe）
# ═══════════════════════════════════════════════════════

def _build_mcp_tools() -> list:
    """遍历 _server_tools（已在 _server_loop 里填充），把每个远程 Tool 包装成 LangChain Tool。

    **纯同步**——不调 asyncio.run_coroutine_threadsafe，可安全在 _mcp_loop 线程调用。
    """
    from langchain_core.tools import StructuredTool

    all_tools = []
    _seen_names = set()   # 防同 server 内不同原名清洗后撞名（如 search-web / search_web）互相覆盖

    for server_name, tools_list in _server_tools.items():
        for mcp_tool in tools_list:
            original_name = mcp_tool.name
            description = mcp_tool.description or f"MCP tool: {original_name}"
            input_schema = mcp_tool.inputSchema or {"type": "object", "properties": {}}

            # 命名规则：mcp_{server}_{tool}（非法字符 → _，防撞内置工具）
            safe_name = re.sub(r"[^a-zA-Z0-9]", "_", original_name).strip("_")
            wrapped_name = f"mcp_{server_name}_{safe_name}"
            if wrapped_name in _seen_names:   # 撞名 → 加数字后缀，别让后者悄悄覆盖前者
                _suffix = 2
                while f"{wrapped_name}_{_suffix}" in _seen_names:
                    _suffix += 1
                wrapped_name = f"{wrapped_name}_{_suffix}"
            _seen_names.add(wrapped_name)

            # display name 加 🔌 前缀
            MCP_DISPLAY_NAMES[wrapped_name] = f"🔌 {original_name}（{server_name}）"

            # 转成 pydantic model（StructuredTool 需要 pydantic model 作为 args_schema）
            args_model = _schema_to_model(f"{wrapped_name}Args", input_schema)

            # 用闭包正确捕获 server_name 和 original_name
            def _make_invoker(srv=server_name, tn=original_name):
                def _invoke(**kwargs) -> str:
                    sess = _sessions.get(srv)
                    if sess is None:
                        return f"错误：MCP Server '{srv}' 未连接"
                    # 剔除 None：pydantic 给可选字段填的默认 None 不该发给 server，
                    # 否则像 fetch 的 max_length 会被当 integer 校验、收到 None 直接报错。
                    # 可选字段没传就省略，让 server 用它自己的默认值。
                    call_args = {k: v for k, v in kwargs.items() if v is not None}
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            sess.call_tool(tn, arguments=call_args), _mcp_loop
                        )
                        # 分片等待：每 0.3s 检查一次 stop_flag，让用户点暂停能立刻生效，
                        # 而不是傻等整个工具调用（最长 120s）返回。
                        import concurrent.futures as _cf
                        deadline = time.time() + 120
                        result = None
                        while True:
                            if state.stop_flag:
                                future.cancel()
                                return "已取消：用户停止了生成。"
                            try:
                                result = future.result(timeout=0.3)
                                break
                            except _cf.TimeoutError:
                                if time.time() > deadline:
                                    future.cancel()
                                    return "MCP 工具调用超时（120s）。"
                        # result.content: list[TextContent | ImageContent | ...]
                        parts = []
                        for item in result.content:
                            if hasattr(item, "text"):
                                parts.append(item.text)
                            else:
                                parts.append(str(item))
                        return "\n".join(parts) or "(工具未返回内容)"
                    except Exception as e:
                        logger.error(f"[MCP] 工具 {tn} 调用失败: {e}")
                        return f"MCP 工具执行失败: {e}"
                return _invoke

            invoker = _make_invoker()

            tool = StructuredTool(
                name=wrapped_name,
                description=f"[MCP:{server_name}] {description}",
                args_schema=args_model,
                func=invoker,
            )
            all_tools.append(tool)

    return all_tools


# ═══════════════════════════════════════════════════════
# 守护线程入口
# ═══════════════════════════════════════════════════════

def _background_thread(servers: dict):
    """守护线程：运行 asyncio loop，管理所有 MCP 连接。"""
    global _mcp_loop, _shutdown_event, MCP_TOOLS

    _fix_windows_asyncio()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _mcp_loop = loop
    _shutdown_event = asyncio.Event()

    async def _main():
        global MCP_TOOLS

        # 为每个 server 创建就绪信号
        for name in servers:
            _server_ready_events[name] = asyncio.Event()

        # 并发启动所有 server
        tasks = []
        for name, cfg in servers.items():
            task = asyncio.create_task(_server_loop(
                name=name,
                cfg=cfg,
            ))
            tasks.append(task)

        # 等待所有 server 就绪（完成连接或超时），最多 15 秒
        try:
            await asyncio.wait_for(
                asyncio.gather(*(_server_ready_events[n].wait() for n in servers)),
                timeout=15,
            )
            logger.info("[MCP] 所有 Server 就绪信号已收到")
        except asyncio.TimeoutError:
            ready = [n for n, ev in _server_ready_events.items() if ev.is_set()]
            not_ready = [n for n, ev in _server_ready_events.items() if not ev.is_set()]
            logger.warning(f"[MCP] 等待超时：已就绪 {ready}，未就绪 {not_ready}")

        # 纯同步遍历 _server_tools 构建 LangChain Tool 列表（无死锁风险）
        MCP_TOOLS = _build_mcp_tools()
        if MCP_TOOLS:
            logger.info(f"[MCP] 共注册 {len(MCP_TOOLS)} 个远程工具: "
                        f"{[t.name for t in MCP_TOOLS]}")
        else:
            logger.info("[MCP] 未注册任何远程工具（所有 Server 可能启动失败）")

        # 保持运行直到 shutdown
        await _shutdown_event.wait()

        # shutdown 后取消所有 server task
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    try:
        loop.run_until_complete(_main())
    except Exception as e:
        logger.error(f"[MCP] 守护线程异常退出: {e}")
    finally:
        loop.close()
        logger.info("[MCP] 后台线程已退出")


# ═══════════════════════════════════════════════════════
# 公开 API（UI 主线程调用）
# ═══════════════════════════════════════════════════════

_bg_thread: Optional[threading.Thread] = None


def init_mcp() -> list:
    """读取配置 → 启动 MCP 守护线程 → 等待就绪 → 返回 MCP_TOOLS 列表。

    此函数**同步阻塞**（最多约 15 秒，等待 server 启动），应在 UI Worker 线程中调用。
    若 mcp 未安装，静默返回空列表。
    """
    global _bg_thread

    servers = _load_mcp_config()
    if not servers:
        logger.info("[MCP] 未配置任何 mcp_server，跳过 MCP 初始化")
        return []

    # 检测 mcp 包是否可用
    try:
        import mcp  # noqa: F401
    except ImportError:
        logger.warning("[MCP] 未安装 mcp 包（pip install mcp），MCP 功能不可用")
        return []

    logger.info(f"[MCP] 发现 {len(servers)} 个 Server 配置: {list(servers.keys())}")

    _bg_thread = threading.Thread(
        target=_background_thread,
        args=(servers,),
        daemon=True,
        name="mcp-daemon",
    )
    _bg_thread.start()

    # 等待守护线程完成工具注册（最多 20 秒）
    for _ in range(200):
        if not _bg_thread.is_alive():
            break
        if MCP_TOOLS or (_mcp_loop and not _mcp_loop.is_running()):
            break
        import time
        time.sleep(0.1)

    return list(MCP_TOOLS)


def get_mcp_display_names() -> dict[str, str]:
    """返回 MCP 工具的 wrapped_name → display name 映射。"""
    return dict(MCP_DISPLAY_NAMES)


def is_mcp_tool(name: str) -> bool:
    """判断工具名是否是 MCP 远程工具。"""
    return name.startswith("mcp_")


def shutdown():
    """通知 MCP 后台线程退出（应用关闭时调用）。"""
    global _bg_thread, MCP_TOOLS

    if _mcp_loop is None or _shutdown_event is None:
        return

    # .set() 是同步方法，用 call_soon_threadsafe 调度到 loop 线程执行
    _mcp_loop.call_soon_threadsafe(_shutdown_event.set)

    # 等待线程结束（最多 5 秒）
    if _bg_thread and _bg_thread.is_alive():
        _bg_thread.join(timeout=5)

    MCP_TOOLS = []
    logger.info("[MCP] 已关闭所有 MCP 连接")
