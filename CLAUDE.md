# 灵犀 Code (lingxi-code)

基于 LangChain + PySide6 的 **多模型 AI 编码助手**（Windows 原生桌面应用，"Codex 体验、模型无关"）。

> 专注代码助手；桌面宠物等娱乐属性已移除，以后另开独立应用。
> 角色卡放 `roles/*.md` 加载（仓库附 `example.md` 模板）。

## 项目结构

```
main.py                  # 入口：高 DPI 配置 + 启动 Qt + 创建 ChatUI + 系统托盘
icon.ico
config.json              # API 密钥 / 路径配置（已 .gitignore）
config.example.json
lingxi.spec              # PyInstaller 打包配置（产物 exe 名：灵犀Code）

src/                     # 主代码
  __init__.py
  paths.py               # 路径常量 + logger 配置（启动清理 30 天前 .log）
  config.py              # 解析 config.json，对外暴露常量（含 CUSTOM_MODELS / MCP_SERVERS）
  limits.py              # 集中的魔法数字常量（会话上限/重试/截断/搜索分页/debug 预览长度）
  state.py               # 全局共享状态 + **会话级字段代理**：通过 ModuleType property 把 state.X 转发到「当前线程的当前会话」（见 session.py）
  session.py             # 会话级运行时状态容器（Session 对象）+ 注册表 + 线程路由（current_session / bind_thread / active）—— 多会话并发的地基
  models.py              # BUILTIN_MODEL_LIST + 自定义模型合并 → MODEL_LIST；_create_llm 工厂（带缓存）+ 视觉探测 + get_model_config_issues
  agent.py               # facade（__getattr__ 代理 state）+ agent_loop 主循环 + resolve_bound_llm（按会话 model 分发/bind_tools 缓存）+ 启动拉起 MCP
  streaming.py           # 全流式：_prepare_stream_history / _handle_stream_chunk / _stream_with_tools + 重试退避 + _execute_tool + 三级历史管理（截断/淘汰/压缩）
  verification.py        # 编码任务**完成闸门** + 自动修复循环（标记 dirty/check/test 状态；check_repair_allowed 封顶修复轮次）
  tools.py               # 内置 @tool 聚合器（read/write/edit/run_command/search/test/patch/plan/find_* 等）+ build_all_tools/get_tool_map（含 MCP）
  tools_common.py        # 工具共享底座（_project_cwd / _resolve_path / 子 Agent 沙箱 / 验证状态标记 / shell cwd）—— 不 import tools，避免循环
  tools_git.py           # git 工具：git_diff/log/status/stage/unstage/commit（写操作弹确认、无 push）
  tools_web.py           # 网络只读：fetch_url（SSRF 防护 + 重定向逐跳校验 + 默认不走代理）/ web_search（Tavily）
  tools_codemap.py       # code_map（符号地图）/ find_tests / related_files
  codeintel.py           # 代码智能（tree-sitter 符号提取 / 导入追踪，多语言）
  lsp_client.py          # find_definition/find_references 的后端：LSP → jedi → 降级链
  subagent.py            # 并行子 Agent（spawn_agents 实现：各自在隔离 worktree 跑、合并改动；HeadlessUI 内部协议）
  worktree.py            # git worktree 隔离区（创建/完成/清理；子 Agent 沙箱根）
  mcp_client.py          # MCP 客户端：常驻 asyncio loop 连外部 server，远程工具包成 StructuredTool 注入
  notify.py              # 统一通知入口（分级/节流/环形历史 → telegram_push）；notify_long 发完整分段
  telegram_push.py       # Telegram Bot API 推送（push/push_long 分段/push_confirm inline 按钮/answer_callback/edit_message_text）
  telegram_poll.py       # Telegram 遥控：后台长轮询 getUpdates → from.id 白名单 → 注入 ChatUI / 处理 inline 按钮回调
  memory_store.py        # 长期记忆持久化（原子写 + RLock；remember/forget 存取，注入 system prompt）
  memory.py              # 会话历史 JSON 持久化（RLock 串行化所有读写）+ _build_ai_message + move_sessions_to_no_project（同步磁盘 + 内存锚点）
  checkpoint.py          # edit/write/append 写盘前 git stash 快照 + 撤销（路径级 git checkout 恢复）
  projects.py            # 项目（工作区）管理：chat_memory/projects.json 读写（RLock + 原子写 + 损坏备份）
  roles.py               # 角色卡加载 + get_system_prompt（拼角色卡 / 项目上下文 / .lingxirules / 记忆）+ get_external_agent_context（给 Claude Code 等外部 agent 的精简上下文）
  images.py              # 图片输入格式归一化（视觉/多模态，Anthropic/OpenAI/Gemini 协议差异）
  debug_log.py           # F12 调试：请求/响应 record 环形缓冲 + Qt Signal
  claude_code.py         # 通过 subprocess 调本地 Claude Code CLI（permission-mode 映射 Plan/Act；--append-system-prompt-file + stdin 避 32K 命令行）
  floating.py            # 系统托盘 create_tray（关窗维持后台 + 双击唤起 + _restore_window 保留最大化）

  ui/                    # UI 包（chat_window.py 用 mixin 拆分）
    __init__.py          # 导出 ChatUI / SettingsDialog
    chat_window.py       # ChatUI 主窗口（__init__/build_ui/eventFilter/agent 集成/渲染原语 _append_html/show_message/_t）
    message_view.py      # 消息流的块级真控件渲染（圆角卡/阴影/可展开思考块）—— 逐步替代 QTextBrowser
    confirm_bars.py      # ConfirmBarsMixin：run_command 命令确认卡 + edit_file diff 预览卡 + 危险命令判定 + 白名单 + Telegram 双向确认
    markdown_render.py   # MarkdownRenderMixin：_md_to_html / render_final_markdown / 思考块管理
    search_overlay.py    # SearchOverlayMixin：Ctrl+F 浮窗搜索
    sidebar.py           # SidebarMixin：侧栏 + 会话列表（按项目分组）+ 项目添加/切换/移除
    header.py            # HeaderMixin：顶栏（模型/Plan-Act/撤销/思考/角色卡/主题）+ 所有 _style_*_btn + 角色卡多卡扫描
    debug_inspector.py   # F12 调试弹窗（请求/响应/usage 可视化）
    theme.py             # THEMES dict + build_stylesheet + build_tooltip_qss(app 级) + load/save_theme_choice
    widgets.py           # SignalBridge / DragDrop（粘贴强制纯文本）/ HistoryRow / CloseConfirmDialog
    settings_dialog.py   # 设置弹窗（provider 卡片式 API key + 自定义模型增删改）
    helpers.py           # _make_button_icon / _build_image_content_block / _escape
    prefs.py             # UI 偏好持久化（关闭按钮选择等）
    _base.py             # 共享 BASE_DIR / CONFIG_PATH / THEME_CONFIG_PATH 常量

scripts/                 # pytest 测试套件 + conftest fixtures

roles/                   # 角色卡 .md（启动自动恢复上次激活）
  example.md             # 角色卡模板

icons/                   # SVG 图标（Lucide 风格）
  upload_lucide.svg / settings_lucide.svg / arrow_up.svg / pause.svg / ...

chat_memory/             # 会话 JSON + index.json + projects.json + role_config.json + ui_prefs.json + theme_config.json
logs/                    # 按日期分文件的日志
docs/                    # 项目文档（含 TODO.md）
build/, dist/            # PyInstaller 产物（已 .gitignore）
```

## 运行

```bash
# 主依赖（完整清单见 requirements.txt）
pip install langchain langchain-ollama langchain-openai langchain-anthropic langchain-google-genai PySide6 markdown requests pillow numpy

# MCP 客户端（可选；没装则 MCP 功能静默跳过）
pip install mcp

# 代码导航（可选；装语言服务器走 LSP 最准 → 没有退 jedi → 都没有退回 search_files）
pip install jedi python-lsp-server   # 或 pip install pyright（需 Node）

# 代码地图增强（可选；tree-sitter 多语言符号提取，没装则回退内置正则）
pip install tree-sitter tree-sitter-python tree-sitter-javascript tree-sitter-typescript

# 配置
cp config.example.json config.json   # 编辑填入密钥

# Ollama（可选）
ollama serve && ollama pull qwen3.5:latest

python main.py
```

## 支持模型

| 模型名称 | 类型 | 模型 ID | 视觉 |
|----------|------|---------|------|
| MiMo V2.5 Pro / V2.5 / V2 Pro | mimo | mimo-v2.5-pro / 2.5 / 2-pro | ❌ |
| MiMo V2 Omni（多模态） | mimo | mimo-v2-omni | ✅ |
| Claude Code | claude-code | 本地 `claude` CLI | ❌ |
| Qwen3.5 本地 | ollama | qwen3.5:latest | ❌ |
| Qwen-Plus / Max / Turbo / Qwen3.5-Plus | cloud | qwen-* | ❌ |
| Claude Sonnet 4 / Haiku 3.5 | anthropic | claude-sonnet-4-20250514 / claude-3-5-haiku-20241022 | ✅ |
| DeepSeek V4 Flash / Pro | deepseek | deepseek-v4-flash / pro | ❌ |
| ⚙ 用户自定义模型 | custom | config.json `custom_models`（OpenAI/Anthropic 协议自填） | 看配置 |

> **自定义模型**：`config.json` 的 `custom_models`（list），每项 `{name, model_id, api_key, base_url, protocol, supports_vision, supports_thinking}`。设置弹窗里可视化增删改。`models.py:_build_model_list()` 把它们以 `⚙` 前缀合进 `MODEL_LIST`，`_create_llm` 按 `protocol`（openai/anthropic）分发。

## 架构关键点

### 对话核心
- **全流式调用**：`state.llm_with_tools.stream(history)`，AIMessageChunk 用 `+` 自动累加 content 和 tool_call_chunks
- **Agent 主循环**（`src/agent.py:agent_loop`）：stream → 收 tool_calls → 执行 → 再 stream → 没工具就停
- **Claude Code 模式**：`subprocess.Popen` 调本地 `claude -p --output-format stream-json`，解析 `assistant`/`user`/`result` 事件
- **UI ⟷ Agent 解耦**：agent 线程通过 `ui.show_message(text, tag)` 调用，内部 `bridge.append_signal.emit()` queue 到主线程渲染。所有 `ChatUI` 的对 agent 暴露接口（`show_message / render_final_markdown / remove_thinking_indicator / show_token_usage / show_retry / confirm_command`）都是线程安全的 wrapper
- **命令确认**（`src/ui/chat_window.py:confirm_command`）：worker 线程调它时，会通过 `confirm_request = Signal(str, object, object)` 投递到主线程，UI 显示内联确认卡，worker 线程 `event.wait(timeout=300)` 阻塞直到用户点完。会话级 allowlist（`_session_command_allowlist`）让用户选"允许并记住"后同样命令秒过；危险命令（`_is_destructive_command` 正则匹配 `rm -rf` / `format` / `sudo` 等）不给"记住"选项
- **思考过程**：解析 `<think>...</think>` / `reasoning_content` / Anthropic `thinking` content block，统一显示成可折叠的紫色块
- **Markdown 渲染**：流式过程显示纯文本，完成后用 `markdown` 库一次性转 HTML 替换（QTextBrowser 不支持 `<style>` 标签，所有样式必须 inline）。复制/重新生成按钮用 `<table cellpadding=0 height=18>` spacer 撑开（QTextBrowser 对 `<div margin>` 支持差）

### 项目（工作区）
- **状态**：`state.current_project` 持当前项目根路径，None = 无项目；`chat_memory/projects.json` 持久化项目列表 + current
- **启动恢复**：`src/agent.py` 启动时 `state.current_project = _projects.get_current()` 自动恢复
- **新对话沿用项目**：`reset_history()` 不动 `current_project`，所以 `save_session` 仍用当前项目打 tag
- **删项目时批量改归属**：`memory.move_sessions_to_no_project(old_path)` 把所有 `project == old_path` 的会话改成 None，三处一起改：**① 内存里已打开的 `Session.project`**（关键——只改磁盘的话，移除当前项目后 `_switch_project` 的 `save_session` 会按旧内存锚点把会话写回已删项目，后台会话下次 save 也复发）+ ② index.json + ③ 各 session 文件。个别会话文件写失败 → 抛 `SessionMigrationError`（内存锚点已置 None、下次 save 自愈，caller 据此提示用户）
- **工具按项目根解析路径**：`src/tools.py:_project_cwd()` / `_resolve_path()` 让 `read_file('foo.txt')` 解析到 `state.current_project/foo.txt`；`run_command` 的 cwd 也是项目根
- **`.lingxirules` 项目级指令**：项目根放该文件后，`roles.get_system_prompt()` 会把它内容追加到 system prompt 末尾，**优先级高于** SYSTEM_PROMPT 和角色卡的通用指令；每次新对话 / 切项目 / 删当前会话时都重新读，让 AI 立刻"懂这个项目的约定"。最长 20000 字（超过自动截断）

### 模块级 facade（src/agent.py）
- `agent.py` 通过模块级 `__getattr__` 把读取代理到 `state` —— 让 `src/ui/chat_window.py` 等模块继续用 `agent.stop_flag` 不报错
- **写入必须用 `state.X = ...`**（不要 `agent.X = ...`，那只污染 agent 模块）
- `state.ui_ref = self` 在 ChatUI 启动时设置，让 worker 线程的 tools 能找到主窗口弹确认框

### 多会话并发（src/session.py + state.py 代理）
- **会话级状态收进 `Session` 对象**：`chat_history / stop_flag / session_token_usage / compaction / current_plan / task_ledger / shell_cwd / current_model_index / agent_mode / reasoning_enabled / verification / worktree / project / role_snapshot` 等都是会话级（每会话一份），见 `session.py:_SESSION_FIELDS`
- **`state.py` 用 `ModuleType` property 代理**：`state.chat_history` 等读写自动落到「当前线程的当前会话」，所以几十处 `state.X` 老代码无需改动。**真正全局**的（`llm` / `ui_ref` / `current_project` 等）仍是 state.py 的普通变量
- **线程路由**（`session.current_session()`）：worker 线程进 `agent_loop` 时 `bind_thread(sess)` 把自己绑到该会话 → 该线程所有 `state.X` 都落到这个会话；主线程（UI）/ 未绑定线程 → `get_active()`（前台显示的会话）。这就是「后台会话边跑、前台切到别的会话」不互串的根基
- **注册表** `session.sessions`（key→Session）：`register` / `rekey`（存盘拿到 id 后把临时 `_new_N` 换成 id）/ `drop`
- **会话锚定项目** `Session.project`：首次 save 时锚定为当时的全局 `current_project`，之后不被切项目影响（`_UNSET` 哨兵区别于合法的 `None`=无项目）。修「后台会话 save 晚于主线程切项目、被打上新项目 tag」的 bug

### 验证闭环与自动修复（src/verification.py，编码核心）
- **完成闸门**：编码任务声称"完成"前要先验证（改了代码须 `run_tests` / `git_diff`）。会话级 `verification` 状态记 dirty 文件 / check 结果 / 测试是否过
- **自动修复循环**：`run_tests` / `check_code` 失败时，`check_repair_allowed()` 注入 `[REPAIR_INFO]` 修复提示让模型继续修，并**封顶 N 轮**（`failure_diagnosis.attempt >= max_attempts` 后停，交完成闸门/模型收尾）——见 `agent.py` 工具执行后、插 ToolMessage 前的调用
- 纯运行态，不持久化

### 子 Agent 并行 + worktree 隔离（src/subagent.py + worktree.py）
- `spawn_agents(tasks)` 把多个**相互独立**的子任务并行派给子 Agent，**各自在独立 git worktree 改代码**，跑完合并回主项目
- 子 Agent 是 `is_subagent=True` 的会话，`ui_ref=None`（不弹前台确认）；文件/命令严格限定在自己 worktree（`tools_common._subagent_path_rejection` / `_subagent_command_rejection` best-effort 沙箱）
- `worktree.py` 管隔离区生命周期；`Session.worktree` 路由该会话所有文件/命令落点（`_project_cwd` 优先返回 worktree）

### 代码导航（src/lsp_client.py + codeintel.py + tools_codemap.py）
- `find_definition` / `find_references`：**LSP（最准，装了语言服务器）→ jedi（Python）→ 退回 search_files** 的降级链
- `codeintel.py`：tree-sitter 符号提取 / 导入追踪（多语言），支撑 `code_map` / `related_files`
- 这些都是只读工具，进 `PLAN_MODE_READONLY_TOOLS`、不弹确认

### 持久化（memory.py 并发安全）
- 所有读写 `chat_memory/` 的函数都被 `threading.RLock` 串行化（`save_session` / `_update_index` / `_write_session_title` / `load_session` / `list_sessions` / `delete_session` / `move_sessions_to_no_project` / `_ensure_memory_dir`）
- 用 RLock 不用 Lock：`save_session` 自己持锁时还会调 `_update_index`（也持锁），普通 Lock 会自死锁
- 修复了原来"快速发两条消息时，标题生成线程和 save_session 同时改 index.json 互相覆盖丢会话"的并发 bug

### 角色卡
- `roles/*.md` 直接作为 system prompt
- 激活的角色记录在 `chat_memory/role_config.json`，启动时 `load_saved_role_card()` 自动恢复

### 系统托盘（src/floating.py）
- `create_tray(app, chat_window, icon_path)`：`QSystemTrayIcon` + 右键菜单（打开对话 / 退出）+ 双击唤起窗口
- `main.py` 设 `setQuitOnLastWindowClosed(False)`，关窗只隐藏、由托盘维持后台；托盘"退出"才真退
- `_restore_window` 唤起时**保留最大化/全屏状态**（不用 `showNormal()`，否则会缩回默认尺寸）
- 桌面宠物已移除（原 DesktopPet / GIF 动画 / `set_thinking` 钩子全部删除）；`thinking_indicator` 是聊天窗口自己的"思考中…"指示器，与托盘无关

### MCP 客户端（src/mcp_client.py，可选功能）
- 让灵犀连外部 MCP server（filesystem / fetch / context7 / memory 等），把远程工具动态注入到 `ALL_TOOLS`，跟内置工具一样被 AI 调用。**没装 `mcp` 包 / 没配 `mcp_servers` 时整段静默跳过**（零回归）
- 配置在 `config.json` 的 `mcp_servers`（dict，key=server 名）：`transport` 支持 `stdio`（command+args）/ `sse`（url）/ `streamable_http`
- **同步/异步桥接**：mcp SDK 是 asyncio 异步、灵犀 agent 是同步。`mcp_client.py` 起**一个常驻后台线程跑 asyncio loop**；每个 server 一个常驻协程，`async with stdio_client/sse_client ... await _shutdown_event.wait()` 挂住保持连接（**绝不能 return session 出去，上下文一退连接就断**）。工具调用走 `run_coroutine_threadsafe(session.call_tool(...), loop).result()` 从 agent 线程投进 loop
- **致命坑（已避开）**：不要在 loop 自己的线程上对同一 loop 用 `run_coroutine_threadsafe().result()` —— 自死锁。`_build_mcp_tools` 是纯同步、读 `_server_loop` 提前缓存好的 `_server_tools`
- 工具名加 `mcp_{server}_{tool}` 前缀（防撞内置工具）；`_execute_tool` 里 `name.startswith("mcp_")` 的工具走**执行前确认**（MCP 工具能干任意事）；Plan 模式当写工具拦截
- 启动时 `agent.py` 后台线程调 `init_mcp()`，工具就绪后清 `_BOUND_LLM_CACHE` 让下次 stream 重新 `bind_tools`；关窗 `main.py` 调 `shutdown()`
- 打包：`lingxi.spec` 用 `collect_submodules('mcp')` + `collect_data_files('jsonschema_specifications')`（懒导入 + 数据文件，静态分析抓不到）

### 长期记忆（src/memory_store.py，跨会话）
- 让角色"天生记得"用户：`remember(fact)` / `forget(query)` 两个工具存取，`get_system_prompt()` 末尾**无条件注入**全部记忆（不靠 AI 主动查，开口就记得）
- 存 `chat_memory/long_term_memory.json`（`{memories: [{id, text, created, scope}]}`，scope 默认 global）。独立 `RLock`，跟 `memory.py`（会话历史）分开
- **数据安全**：`_save` 用临时文件 + `os.replace` **原子写**（崩溃不留半截）；`_load` 区分"真损坏"（JSON/编码错 → 重置空可重建）和"瞬时错误"（IO/占用 → 抛 `_MemoryLoadError`，**写操作遇到必中止、绝不 _save 写空丢数据**）
- v1 不用 embedding（单人助手记忆少，全量注入又快又准）；注入段会被 Anthropic/MiMo 缓存覆盖，每轮重读保持最新
- `remember`/`forget` 是本地安全操作，**不弹确认**、Plan 模式放行（在 `PLAN_MODE_READONLY_TOOLS` 里）

### 持久化文件
| 文件 | 内容 |
|------|------|
| `chat_memory/index.json` | 会话列表（id + title + 时间 + project tag） |
| `chat_memory/long_term_memory.json` | 跨会话长期记忆（remember/forget 存取，自动注入 system prompt） |
| `chat_memory/<session_id>.json` | 会话消息历史（HumanMessage/AIMessage/ToolMessage 序列化）+ project 字段 |
| `chat_memory/projects.json` | 注册的项目列表 + 当前激活项目（`{current, projects: [{path, name}]}`） |
| `chat_memory/role_config.json` | 当前激活的角色卡名 |
| `chat_memory/ui_prefs.json` | UI 偏好（如关闭按钮记住的选择） |
| `chat_memory/theme_config.json` | 主题选择（light / dark） |
| `logs/YYYYMMDD.log` | 按日期分的日志 |

## 工具列表

内置工具在 `tools.py` 的 `ALL_TOOLS`；MCP 远程工具运行时注入（`mcp_{server}_{tool}`）。`get_tool_map()` 动态合并内置 + MCP。

| 工具 | 功能 |
|------|------|
| `read_file` | 读取文件（`offset`/`limit` 分页，行号前缀） |
| `write_file` | 创建/覆盖（**写盘前弹 diff 确认卡**；全量覆盖比 edit 危险） |
| `append_file` | 追加（**弹 diff 确认卡**） |
| `edit_file` | 精确字符串替换（比 write_file 安全省 token；**弹 diff 预览卡** + 路径白名单） |
| `list_directory` | 列目录 |
| `run_command` | 执行命令（默认 300s 超时、可传 `timeout`；屏蔽交互式，**执行前弹内联确认卡**；cwd = 项目根；流式输出 + taskkill 杀进程树；**`background=True` 转后台**跑 dev server/长服务，立即返回 bg_id） |
| `read_background_output` / `list_background_commands` / `stop_background_command` | 管理后台命令（read·list 进 `PLAN_MODE_READONLY_TOOLS`、不弹确认；`_bg_procs` 全程 `_bg_lock` 保护、杀进程锁外调；退出时 `stop_all_background` 清理防端口残留） |
| `search_in_file` | 单文件关键词（`offset`/`limit` 分页） |
| `search_files` | 跨文件正则搜索（ripgrep 风格，忽略噪声目录） |
| `find_definition` / `find_references` | 跳符号定义 / 找所有引用（LSP→jedi→search 降级链，比正则准；只读、Plan 放行） |
| `find_tests` / `related_files` | 找某源文件的相关测试 / 列出导入·被导入·相关测试（只读、Plan 放行） |
| `remember` / `forget` | 长期记忆存取（本地安全操作，**不弹确认**，Plan 模式放行） |
| `spawn_agents` | 并行派生子 Agent 处理独立子任务（各自隔离 worktree、跑完合并；写工具，Plan/遥控拦） |
| `update_plan` / `set_step_status` | 任务计划：整份重列 / 增量改单步状态（只读，Plan 放行；驱动计划面板） |
| `get_project_instructions` | 读目标路径适用的项目规则（CLAUDE.md / AGENTS.md / .lingxirules；只读、Plan 放行） |
| `notify_user` | 主动给用户推 Telegram 通知（分级；本地安全、Plan 放行） |
| `code_map` | 代码库符号地图（命名组正则提取函数/类，commonpath 防越界；Plan 只读放行） |
| `run_tests` | 跑 pytest（`_resolve_python()` 选解释器：项目 venv → 开发期 sys.executable → PATH；精炼失败定位 + 总耗时；`encoding=utf-8` 防 GBK 崩） |
| `git_diff` / `git_log` / `git_status` | 只读 git（看改动/历史/状态；commonpath 越界防护；Plan 只读放行） |
| `git_stage` / `git_unstage` / `git_commit` | git 写操作（暂存/取消暂存/本地提交，**无 push**）；**执行前强制弹确认卡**（按危险操作处理、不给"记住"选项）；路径白名单防注入，commit 不自动暂存、校验信息非空 |
| `check_code` | 静态检查单文件（lint/语法）：Python 用 `ruff check --select F,E9`（没装退化到 `py_compile`），其它语言用 config 的 `check_command`；只读不弹确认、Plan 放行 |
| `apply_patch` | 多文件原子补丁（Codex 风格 `*** Begin Patch`）：一次建/改/删多文件；hunk 复用 `_locate_edit` 连续块匹配（拒绝模糊猜测）；全量校验通过才落盘，任一失败整体中止；写工具、Plan/遥控自动拦 |
| `fetch_url` / `web_search` | 网络只读：`fetch_url` 抓网址（http(s) only、HTML 去标签转文本、二进制拒绝、无需 key）；`web_search` 用 Tavily（config `web_search_api_key`，没配优雅降级）。均进 Plan 只读、**不进遥控白名单**（网络外发默认不给远程） |

> 写盘类工具（edit/write/append）共用 `tools.py:_confirm_file_write()`：算 unified diff → `ui.confirm_edit` 弹蓝色卡片 → worker 阻塞等审批。CLI/测试无 UI 时直接放行。

### 自我校验闭环（src/tools.py，编码核心）
- 让助手"改完自己发现错、自己修"（对标 Cline/Codex）。`edit_file`/`write_file`/`append_file` **成功后**调 `_auto_check_suffix(full_path)`：跑静态检查、把问题**追加到工具返回串**，模型在同一条 ToolMessage 里就看到「成功编辑 X」+「⚠️ 自动校验发现问题…」→ 下一轮自然去修
- `_run_code_check()` 是核心：Python 优先 `ruff check --select F,E9`（**只选 pyflakes 正确性 + 语法错，避开风格噪声**，否则模型会去追无意义的格式问题）。检测顺序：**① 随包 ruff**（`_bundled_ruff()`：打包后在 `_MEIPASS`/exe 旁，由 lingxi.spec 构建时定位系统 ruff 打入，开箱即用）→ **② 开发期 `sys.executable -m ruff`**（`find_spec` 检测，不看 PATH、用应用自己的 Python）→ **③ PATH 上的 ruff 二进制** → **④ 兜底内置 `compile()` 进程内查语法**（`_py_syntax_check`）
- **打包(frozen)安全**：`sys.executable` 在打包后 = `灵犀Code.exe`（不是 python.exe），所以 `sys.executable -m ruff/py_compile/pytest` 在产物里都跑不了。故 frozen 下不走 `sys.executable -m`：
    - check_code 的 ruff 用随包/系统二进制，语法检查用**内置 `compile()`（进程内、不起子进程）**
    - `run_tests` 用 `_resolve_python()` 选解释器：**项目内 venv（.venv/venv/env）→ 开发期 sys.executable → 系统 PATH 的 python**（frozen 下跳过 sys.executable）。顺带让它在真实项目里用对环境（项目自己的 venv + 依赖）而非应用的 Python
- 其它语言读 config `check_command`（`{file}` 占位，shell 执行）；可用 `auto_check_after_edit` 关掉自动触发
- 开关：config `auto_check_after_edit`（默认 true）；只检**刚改的那个文件**（快），防失控靠现有 agent loop 上限 + 模型没错就停
- `check_code` 工具是手动复查入口（同一套 `_run_code_check`）；编辑后自动触发不需要模型记得调它

## 开发注意事项

### 通用
- **Python 3.14 环境**，`ConversationBufferMemory` 等旧版 LangChain API 不可用
- **路径**：`src/paths.py` 的 `_app_data_dir()` 在 dev 期返回 src 上一级目录（项目根），打包后返回 exe 目录
- **写入 state**：必须 `state.X = ...`（不是 `agent.X = ...`）
- **新增工具**：在 `src/tools.py` 用 `@tool` 装饰器定义，加进 `ALL_TOOLS` 和 `TOOL_MAP`；如果是文件类工具记得用 `_resolve_path()` 解析相对路径
- **新增模型**：编辑 `src/models.py` 的 `MODEL_LIST` 元组列表，并在 `_create_llm()` 加 dispatch 分支
- **新增 UI 子模块**：`src/ui/` 下放，注意从 `..` 引父包（`from .. import agent`），从 `.` 引同级（`from .theme import ...`）；`__init__.py` 只对外暴露 `ChatUI` / `SettingsDialog`

### Qt 相关
- 高 DPI：`QT_ENABLE_HIGHDPI_SCALING=1` + `setHighDpiScaleFactorRoundingPolicy(PassThrough)`
- 任务栏图标：`SetCurrentProcessExplicitAppUserModelID("lingxi.ai.desktop")`
- QTextBrowser **不支持 `<style>`**，Markdown HTML 必须 inline 样式
- QTextBrowser 对 `<div margin>` / `<p padding>` 支持差；要给消息按钮留垂直空白用**表格 spacer**（`<table><tr><td style="height:14px">`），HTML 邮件时代的老套路最稳
- Enter 发送、Shift+Enter 换行 通过 `eventFilter` 在 `self.entry` 上拦截
- **跨线程 QObject 调用必须用 Signal**：worker 直接动 `QTimer.start/stop` / `widget.update()` 会让 timer 失去 thread affinity 永久失活。范式参考 `SignalBridge.confirm_request`
- `QPixmap.setDevicePixelRatio(dpr)` 后拿物理像素尺寸要用 `deviceIndependentSize()` 否则在高 DPI 上偏移
- 加载 `.ico` 当 widget icon 时**别直接 `QPixmap(path).scaled()`**——会从 .ico 多分辨率位图里随便挑一张可能拿到 16×16 那张。要用 `QIcon(path).pixmap(QSize(256,256)).scaled(...)`，QIcon 会挑最接近目标尺寸的内嵌位图

### 配置
- `config.json` 含 API 密钥，**已 `.gitignore`，禁止提交**
- MiMo 模型走 Anthropic 兼容接口（`ChatAnthropic` + 自定义 `base_url`）
- DeepSeek V4 默认开 thinking，要显式 `extra_body={"thinking": {"type": "disabled"}}` 才能关
- PyInstaller 打包配置见 `lingxi.spec`（产物 exe 名为中文「灵犀Code」）

## 已知限制与有意取舍（避免 code review 反复重提）

> `docs/` 整个被 `.gitignore`、不进仓库，所以这类「单一真相」记录放这里（AGENTS.md：CLAUDE.md 是唯一真相源）。

### 已接受的残留（评估后暂不修）
- **`fetch_url` 对 DNS 重绑定的 GET 副作用是 blind SSRF**（`tools_web.py`）：peer IP 校验在 `requests.get()` 返回后才做，GET 请求已发出，只能挡读响应、挡不住内网 GET 的副作用。已缓解：`_ssrf_reject` 发请求前拦掉所有**静态**内网目标 + 默认直连（`fetch_url_allow_proxy=false`），残留仅「公网域名重绑定到内网 + 该端点对 GET 有副作用」，对单机本地工具很边缘。真要堵需发请求前**钉住已验证 IP**（HTTPS 要自定义 TLS adapter 保 SNI/证书，安全关键、需真实端点测试），故单独立项再做。
- **Claude Code（`claude -p`）不支持图片输入**（`claude_code.py`）：print 模式无受支持的图片传入方式，只能传文本；已在 UI 明确提示而非静默丢弃。

### 有意跳过（ROI 低 / 非问题）
- **C1** claude CLI 版本探测回退：现行 CLI 早稳定支持 `--permission-mode`，每次探测多起子进程不划算。
- **B3** `projects._load` 去锁：文件极小、读极快，且写前防清空正需要锁。
- **C2** `_project_cwd` 改公开别名：纯改名无价值。

### 待办 backlog（真问题，按优先级排期）
来自 `docs/glm_code_review_2026-06-19.md`（该文件本地可见、未入库）：默认模型 fallback 落到 Claude Code（`agent.py`）；`read_file` 大文件全量 `readlines()`（`tools.py`）；token 估算 `×0.7` 对英文高估（`streaming.py`）；流式重试不分错误类型（`streaming.py`）；`apply_patch` 自称原子但写盘逐文件（`tools.py`）。
