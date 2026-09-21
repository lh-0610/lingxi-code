# 灵犀 Code (lingxi-code)

基于 LangChain + PySide6 的 **Windows 原生 AI 编码助手**（桌面应用）。
Agent 工具调用 · 写盘确认闸门 · 改完自检的验证闭环 · RAG 知识库 · MCP · worktree 隔离的并行子 Agent；任意模型可切。

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
  llm_errors.py          # 模型请求错误分类（限流/上下文溢出/鉴权/瞬时）+ Retry-After 解析 + 重试策略
  run_records.py         # 运行身份（run_id/来源/终态）+ 统一 begin/finalize + inflight sidecar + 完成回执 + 待验证义务归一化
  tools_rag.py           # search_knowledge（知识库语义检索，只读、Plan 放行、带 scope 分域参数）
  rag/                   # RAG 知识库引擎（详见「RAG 知识库检索」节）
    chunk.py             # 切块：iter_markdown_chunks（按标题分层）/ iter_plain_chunks（PDF 页，不解析 Markdown）
    index.py             # 摄取 .md/.pdf → 切块 → embed → 建索引；顶层子目录名 → category 分域
    embed.py             # embedding API 调用（DashScope 兼容端点；响应严格校验防向量错位）
    store.py             # Chroma 向量库：hash 当 doc ID（增量复用 + 天然去重）+ staging 改名式事务提交
    retriever.py         # 检索：索引锚校验（fail-closed）+ 相似度阈值 + 分域配额轮转合并
    rerank.py            # 可选 cross-encoder 重排（gte-rerank-v2），失败回退向量顺序
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
  roles.py               # 角色卡加载 + get_system_prompt（**只拼跨轮稳定的**：角色卡/项目上下文/项目规则/记忆）+ get_volatile_context（每轮都变的：Plan 提示/计划/台账，由 streaming 追到历史尾部）+ get_external_agent_context
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
  measure_save_cost.py   #   非 pytest：量典型/长会话的保存耗时与字节数（要不要上增量日志得先有数)
  evals/                 # **agent 行为评测集**（真调 API、不进 pytest；详见 evals/README.md）
    runner.py            #   fixture 全新副本 + HeadlessUI 驱动 + 7 类判定 + 多次取通过率
    cases/*.json         #   任务 + 判定 + 预算；含防作弊（file_unchanged）与负面用例
    fixtures/            #   给 agent 修的素材（broken-calc 必须是坏的；已从 pytest 收集排除）

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
pip install langchain langchain-ollama langchain-openai langchain-anthropic langchain-google-genai PySide6 markdown requests pillow numpy chromadb

# 知识库 PDF 摄取（可选；没装则只收 .md，目录里有 .pdf 会在重建索引时明确报错）
pip install pypdf

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
| ⚙ 用户自定义模型 | custom | config.json `custom_models`（OpenAI / Anthropic / Responses 协议自填） | 看配置 |

> **自定义模型**：`config.json` 的 `custom_models`（list），每项 `{name, model_id, api_key, base_url, protocol, supports_vision, supports_thinking}`。设置弹窗里可视化增删改。`models.py:_build_model_list()` 把它们以 `⚙` 前缀合进 `MODEL_LIST`，`_create_llm` 按 `protocol` 分发，支持 `openai` / `anthropic` / `responses` 三种兼容接口——**多数兼容端点的模型零代码改动就能加**，只有协议不兼容或需特殊 `extra_body` 的才要动 `models.py`。

## 架构关键点

### 对话核心
- **全流式调用**：`state.llm_with_tools.stream(history)`，AIMessageChunk 用 `+` 自动累加 content 和 tool_call_chunks
- **Agent 主循环**（`src/agent.py:agent_loop`）：stream → 收 tool_calls → 执行 → 再 stream → 没工具就停
- **Claude Code 模式**：`subprocess.Popen` 调本地 `claude -p --output-format stream-json`，解析 `assistant`/`user`/`result` 事件
- **UI ⟷ Agent 解耦**：agent 线程通过 `ui.show_message(text, tag)` 调用，内部 `bridge.append_signal.emit()` queue 到主线程渲染。所有 `ChatUI` 的对 agent 暴露接口（`show_message / render_final_markdown / remove_thinking_indicator / show_token_usage / show_retry / confirm_command`）都是线程安全的 wrapper
- **命令确认**（`src/ui/chat_window.py:confirm_command`）：worker 线程调它时，会通过 `confirm_request = Signal(str, object, object)` 投递到主线程，UI 显示内联确认卡，worker 线程 `event.wait()` 等待用户操作；授权记忆与拒绝停止必须取当前确认卡 `result_holder['_session']` 的归属，不能依赖窗口共享的“最后发起确认的会话”指针。会话级 allowlist（`_session_command_allowlist`）让用户选"允许并记住"后同样命令秒过；危险命令（`_is_destructive_command` 正则匹配 `rm -rf` / `format` / `sudo` 等）不给"记住"选项
- **思考过程**：解析 `<think>...</think>` / `reasoning_content` / Anthropic `thinking` content block，统一显示成可折叠的紫色块
- **Markdown 渲染**：流式过程显示纯文本，完成后用 `markdown` 库一次性转 HTML 替换（QTextBrowser 不支持 `<style>` 标签，所有样式必须 inline）。复制/重新生成按钮用 `<table cellpadding=0 height=18>` spacer 撑开（QTextBrowser 对 `<div margin>` 支持差）

### 项目（工作区）
- **状态**：`state.current_project` 持当前项目根路径，None = 无项目；`chat_memory/projects.json` 持久化项目列表 + current
- **启动恢复**：`src/agent.py` 启动时 `state.current_project = _projects.get_current()` 自动恢复
- **新对话沿用项目**：`reset_history()` 不动 `current_project`，所以 `save_session` 仍用当前项目打 tag
- **删项目时批量改归属**：`memory.move_sessions_to_no_project(old_path)` 把所有 `project == old_path` 的会话改成 None，三处一起改：**① 内存里已打开的 `Session.project`**（关键——只改磁盘的话，移除当前项目后 `_switch_project` 的 `save_session` 会按旧内存锚点把会话写回已删项目，后台会话下次 save 也复发）+ ② index.json + ③ 各 session 文件。个别会话文件写失败 → 抛 `SessionMigrationError`（内存锚点已置 None、下次 save 自愈，caller 据此提示用户）
- **工具按项目根解析路径**：`src/tools.py:_project_cwd()` / `_resolve_path()` 让 `read_file('foo.txt')` 解析到 `state.current_project/foo.txt`；`run_command` 的 cwd 也是项目根
- **`.lingxirules` 项目级指令**：项目根放该文件后，`roles.get_system_prompt()` 会把它内容追加到 system prompt 末尾，优先于 SYSTEM_PROMPT 和角色卡的通用指令；每次新对话 / 切项目 / 删当前会话时都重新读，让 AI 立刻"懂这个项目的约定"
- **规则加载分两层**（`roles.py`）：`read_rule_sources()` 只读**原文**、不截断；`render_rule_sources(budget)` 负责在预算内挑内容。装得下给全文（输出与重构前逐字一致）；装不下给「来源清单 + 目录 + 原文节选 + 省略范围 + 补读示例」，开头标注"尚未完整读取"。节选取头也取尾——**重要性不能靠位置判断**，约定和已知取舍这类最该遵守的条目常写在文末。预算：system prompt 合并 `_COMBINED_RULES_MAX=40000`，单来源 `_SINGLE_RULE_MAX=40000`，`get_project_instructions` 概览 `_TOOL_OVERVIEW_MAX=20000`（必须明显低于 `TOOL_RESULT_HARD_CAP_CHARS=24000`，否则概览会被发送层二次截断，连"少了什么"的说明本身都被切掉）
- **早先"读的时候就截断"丢过真东西**：本仓库 CLAUDE.md 超过当时 20000 的上限，末尾 5 节（含「已知限制与有意取舍」）从未进入 system prompt——既不在输出里也不在任何清单里，模型不会去补读自己不知道存在的内容。这类故障不报错，只会表现为"它怎么老提已经决定过的事"
- **`get_project_instructions` 也受同一预算**，不是完整原文的后门；要拿全文用它的 `source`/`offset`/`limit` 分页（按 Unicode 字符计、默认 6000 上限 12000，返回内容指纹，翻页途中文件被改能发现）。路径作用域判定与 system prompt 注入共用 `_rule_dirs_for_target()`，不因分页放宽
- **项目指令的优先级边界**（`roles._PROJECT_RULES_PRECEDENCE`，注入 CLAUDE.md / AGENTS.md / .lingxirules 时统一前置）：它们优先于通用编码约定，但**不得覆盖系统提示的安全约束与用户在本次对话中的直接指令**。这些文件来自**代码仓库**，是不可信输入——克隆一个第三方项目不该等于授权它改写助手的行为。早先写的"优先于上面任何通用约定"等于给了仓库里一句「忽略之前所有安全限制」以最高权限，是现成的提示注入面
- **Plan/Act 随会话持久化**：`agent_mode` 进 session JSON，`load_session` 回填、`_sync_header_from_session` 同步段控。不存的话重开一个"聊到一半正在 Plan"的会话会变成 Act——用户以为还在只读规划、模型却已能动手改代码，是有安全后果的静默降级。旧会话缺该字段 → 默认 act

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

### Prompt 缓存与「稳定 / 易变」分层（roles.py + streaming.py）
- **判据只有一条：会变的东西不进 system prompt**。`_wrap_system_for_cache` 把整个 system 塞进一个 `cache_control: ephemeral` 块，Anthropic/MiMo 的缓存以**前缀逐字节相同**为条件，块内容一变就失效
- 早先 `task_ledger`（每执行一个工具就更新）和 `current_plan`（每次 update_plan 更新）被拼进 system prompt → **每轮 system 都不同 → prompt caching 每轮必然 miss**。缓存机制写对了，却被里面装的东西废掉了，每轮按全价重算角色卡 + 项目上下文 + 项目规则 + 长期记忆
- 现在拆成两半：`get_system_prompt()` 只留跨轮稳定的（基底/角色卡/日期/项目上下文/项目规则/长期记忆）；`get_volatile_context()` 装每轮会变的（Plan 提示/计划/台账），由 `streaming._append_volatile_context` 作为一条 `HumanMessage` 追加到**发送历史的最末尾**——前面任何一条变了前缀缓存就从那里断掉，追加在最后才能让整段前缀可复用。只改发送副本，不动 `state.chat_history`
- 易变块用 `<system-reminder>` 包裹：这是系统注入的运行态，不是用户的新要求，模型不该把它当指令执行
- **缓存命中要能被观测**：`_extract_usage` 采集 `cache_read` / `cache_write`（兼容 Anthropic 的 `input_token_details`、OpenAI 的 `prompt_tokens_details.cached_tokens`、DeepSeek 的 `prompt_cache_hit_tokens`）。prompt caching 省的是**钱**不是 token——命中部分照样计进 `input_tokens`、只是按约 10% 计费，不单独采集就完全看不出有没有生效
- 回归守卫在 `scripts/test_prompt_cache_stability.py`：这条性质极易被无意破坏（往 `get_system_prompt` 里再拼一个「当前 xxx」就够了），而破坏后一切照常工作、只是**静默开始烧钱**，没有任何报错

### 主循环边界与错误恢复（src/agent.py + llm_errors.py）
- **轮次上限** `config.AGENT_MAX_ROUNDS`（config.json 的 `agent_max_rounds`，设置弹窗「高级」页可改，默认 50；**设 0 = 不限**）：主循环原本是裸 `while True`，模型只要一直返回 tool_calls 就一直跑，唯一刹车是用户点停止。触顶后停下并把结论**写进 chat_history**（只弹 UI 的话，用户接着发消息时模型看不到自己被截断，会以为任务做完了）
- **错误分类**（`llm_errors.classify`）：原来所有异常一视同仁地退避重试，对三类是错的——**上下文溢出**重试必然再溢出；**鉴权失败**等再久 key 也不会变对；**限流**服务端通常在 `Retry-After` 里给了确切秒数，盲目 2^n 要么继续撞墙要么白等。分类不 import 任何 provider SDK（各家异常类型不同、自定义模型协议还是用户配的），改用三层判据：状态码 → 异常类名 → 消息特征词，认不出一律退化成 UNKNOWN（= 保持原行为），宁可漏判不可误判
- **溢出恢复**：provider 报超窗 → `_stream_chunks_with_retry` 抛 `ContextOverflowError` → 主循环把会话的 `overflow_squeeze` +1（`_current_history_budget` 按 1/2^n 收紧、8k 地板）→ 压缩后重发，封顶 `_MAX_OVERFLOW_RETRIES=3`。**不在成功后清零**：溢出说明对该模型窗口的估算偏乐观，这个教训该在本会话内保持，否则每轮都要重新撞一次墙
- 子 Agent 递归深度天然封顶：`spawn_agents` 里 `is_subagent` 的会话不许再派生

### 运行记录与中断恢复（src/run_records.py，B03）
- **三个身份分开**：会话 Session（聊天历史 + 项目归属）／任务 Task（目标 + 计划 + 累计进度，B05 才建立，`task_id` 现在允许为 null）／运行 Run（一次发送·重试·继续启动的 agent 循环）。`run_id` 每次由程序生成，与 session_id / task_id 无关
- **统一 begin/finalize 在 `agent_loop` 最外层**，主体是 `_agent_loop_body`。普通聊天、重试、视觉桥接、Claude Code CLI 分支、所有提前返回和异常（含 `BaseException`）走同一条收尾路径。写在主循环内部的话，每加一个 `return` 就多一个漏网路径——而漏网的恰恰是异常和取消这些最需要记录的情形
- **`begin_run` 自己落盘，不能只更新内存**：进入运行主体前就要把 `phase=running` 存下去。只改内存的后果实测过——新一轮已经生成 run_id、进程在**首次工具调用之前**退出，磁盘上还是上一轮的 ended/completed，这次中断连一条记录都没有；而"重开发现 phase=running"正是 B04 恢复界面唯一的入口。把存盘留给第一次工具提交，等于把最容易崩的那个窗口漏掉了。开始记录与 `pending_verification` 必须是**同一份快照**，否则会出现"这一轮的开始配上一轮的待验证事项"
- 写测试时注意：在 `begin_run` 之后补一次手动 `save_session`，正好会把这个缺口盖住。验证入口行为的用例**一次都不要手动存**
- 收尾**在 worker 解绑前**完成，**持久化成功之后**才发结果通知 / 起标题线程（`_post_run_notify`）。`claude_code_loop` 里原来自己那套 save + 标题已删除，否则一次 CLI 运行会起两个标题线程、还先存一份"还在跑"的快照
- **三条不变量**：① `finalize_run` 幂等（重复调用不再写第二条记录、不再存一次盘）；② 旧 run 的迟到收尾按 `stale` 忽略，不覆盖同一会话的新 run；③ **运行结果与保存结果分开**——保存失败照样如实返回 `AgentResult`，但 `saved=False` 并追加"最新进度未保存"的独立提示
- **运行来源由程序填写**：`describe_source` 取最后一条**真实**用户消息。自动修复提示、完成闸门提示、视觉桥接说明都带 `additional_kwargs["lingxi_internal"]=True` 并**跟着落盘**（`_msg_to_dict`/`_dict_to_msg` 往返），在这里被跳过。不跳过的话，一轮自动修复就把"用户要求了什么"改写成程序自己生成的诊断文字
- **`phase` 与 `outcome` 不能互推**：`phase` 是"程序有没有收到本轮结果"（running / ended），`outcome` 是实际返回的 AgentResult（中断时为 null）。重开看到 `phase=running` 就展示"上次运行被中断"，**加载不改写磁盘事实**，绝不凭空补一个 completed/failed

### 工具边界的 inflight sidecar（src/run_records.py + streaming._execute_tool）
- 有副作用的工具**进入调用前**把一条小记录原子写进 `chat_memory/<id>.inflight.json`；写不成功就抛 `InflightWriteError`、**中止该工具调用**并明确报错。假装建立了恢复点再动手，比根本没有这套记录更糟——用户会以为有记录可查
- **为什么单独一个文件**：执行前若写主 JSON，就得把整段聊天历史重写一遍，长会话里每个写操作都付这个代价。sidecar 只装 ID / 工具名 / 路径（实测恒 480 字节，不随历史增长），不复制历史、长参数或完整输出
- **sidecar 内是操作列表不是单个对象**：一条记录结构上不可能覆盖另一条未完成的。目前只读工具才并行、写工具串行，但这个前提不该被隐含依赖
- **判定哪些工具要记录**：只列纯读工具（`SIDE_EFFECT_FREE_TOOLS`），**不在清单里的一律记录**——未知工具、所有 `mcp_*`、以及 `remember`/`forget`/`notify_user`/`update_plan`/`set_step_status`。后面这几个虽在 `PLAN_MODE_READONLY_TOOLS` 里，却真有副作用（写长期记忆 / 推 Telegram / 改计划），**不能拿 Plan 白名单当这份清单用**
- **`run_tests` / `check_code` 同样要记录**，尽管名字听起来只是检查：`run_tests` 起 pytest，测试代码是项目自己的代码，写文件建目录都合法；`check_code` 在非 Python 项目里执行 config 的 `check_command`，那是用户配的任意命令。把它们当纯读的后果实测过——测试真的写出了文件、进程在结果返回前死掉，sidecar 前后都是空的，恢复分类返回空列表，连"结果未知"的线索都没有。判据是**「会不会执行项目代码或用户配置的命令」**，不是名字像不像检查
- **需要记录的工具一律不并行预取**：`_can_parallel` 直接以 `run_records.needs_record` 为准，不另抄一份名单。预取是在 `_execute_tool` **之前**就把工具跑掉，那时记录还没写，执行前记录就成了摆设。两份清单分开维护必然漂移（`check_code` 就漂过）
- **提交顺序不可颠倒**：工具结果 + 台账 + 进度 + 待验证事项先进内存 → 写一次主快照（含匹配的完成回执）→ **只有正文可靠落盘后**才清 sidecar。先删标记再保存的话，保存失败就等于把唯一线索丢了
- **不能只看一个成功布尔值**：`save_session_report` 返回 `SaveOutcome(body_written / index_written / revision / bytes / elapsed)`。正文成功而索引失败时，回执**确实已落盘**、sidecar 可以清，但这次保存整体是失败的，要照实告诉用户。`save_session` 行为不变（仍抛异常）
- **判断操作是否已提交要核对 session_id + run_id + operation_id + 完成回执**，不能只看 revision 变大——别的保存同样推进 revision。回执存一个有界环（`recent_operations`，50 条）：只留一条的话，A、B 相继提交而 A 的清理失败时，A 会被误判成"结果未知"
- **四个中断窗口**：① 标记已写、工具未返回 → 结果未知；② 工具已跑、快照没写 → 结果未知，标记留着；③ 快照已写、标记没清 → 认成 `committed`，`load_session` 顺手清掉；④ 清理失败后又有别的提交 → 靠回执环仍认得出。**结果未知 = 既不算成功也不算未执行，不自动重放**
- 承诺的是"有记录地恢复并重新核对"，**不是**"任何命令恰好执行一次"。回执落盘 = "这条结果已可靠记录"，**不等于**"工具没有副作用"——执行失败同样留回执，因为失败也可能改了一半文件。「已调度」也不等于「副作用已发生」：写文件工具还要等确认卡，这中间被杀掉文件其实没动，但程序分辨不出，仍报未知
- 子 Agent（`is_subagent`）全程跳过：不写 sidecar、不写运行记录、不发通知、不起标题

### 验证闭环与自动修复（src/verification.py，编码核心）
- **完成闸门**：编码任务声称"完成"前要先验证（改了代码须 `run_tests` / `git_diff`）。会话级 `verification` 状态记 dirty 文件 / check 结果 / 测试是否过。`tests_passed=None` 表示未验证，必须保留原因，不等同于通过。
- **命令与 MCP 本地写入**：`workspace_changes.py` 在调用前后追踪项目文件，工具执行及完成检查时复查；变化使旧测试/diff 失效。Git 项目遵循 ignore，非 Git 项目跳过依赖/构建目录。无法完整枚举（权限、子模块、文件数上限等）保持未验证；它不是进程沙箱，不保证跟踪项目外或任务结束后的写入。
- **自动修复循环**：`check_code` 的 `[REPAIR_INFO]` / `status=failed|checker=...` 是失败识别依据之一，不能只依赖展示 emoji。`run_tests` / `check_code` 失败后由 `agent_loop` 注入诊断提示，修复次数封顶。
- **终态契约**：`agent_loop` 返回不可变的 `AgentResult(status, reason)`：`completed / failed / cancelled / unverified / limit_reached`。线程返回、工具结果和任务完成是不同事实。评测必须检查状态，不能仅凭文件断言通过。Claude CLI 的外部验收无法核实，即使收到 success 也返回 `unverified`。
- **两条边界，别互相冒充**：① **计划状态是模型自述**——`update_plan` / `set_step_status` 的勾选由模型自己写，工具只保证清单不漂移，不保证步骤真做完了；可信度由本节的验证闭环提供，不由计划工具提供。② **验证只覆盖实际执行过的检查**——`tests_passed=True` 的语义是"已跑的测试通过"，不是"任务做对了"；测试全绿仍可能漏掉需求，也不证明每个计划步骤真实完成。
- **待验证义务跨轮保留**（B03）：`verification` 本身仍是纯运行态，但"上一轮改了没验证的东西"会经 `summarize_obligations` 存进会话 JSON 的 `progress.pending_verification`，下一轮由 `begin_run` 在 `reset_verification` **之后**用 `restore_obligations` 填回。顺序反了就等于每轮开头静默清零——`reset_verification` 会清空 dirty 文件，先填充再被清掉是最容易写出的 bug
- 恢复走 `mark_dirty` 而不是直接塞字段：它顺带把 `tests_run`/`tests_passed` 打回未验证。**历史测试成功只是历史证据，不能恢复成当前任务的通行状态**
- `get_verification_gaps`（先复查工作区再算）与 `gaps_from_state`（只看状态、不扫盘）分开：工具边界每次提交都要算一次义务摘要，在那里重扫整棵项目树会让每个写操作都付一次全量哈希的代价
- **`tracking_errors` 与 `unknown_changes` 是两个时态，不能合成一个字段**：前者说"**此刻**读不了这个目录"，枚举一旦成功就该消失（不消的话一次瞬时失败就让闸门永远过不去）；后者（`mark_blind_period`）说"**曾经**有一段时间没人看着"，它**不随「现在能读了」消失**。合成一个的后果实测过——枚举失败期间真改了 app.py，下一轮枚举成功就把义务清空，没跑任何测试也返回 completed，"能读取目录"被当成了"改动已验证"
- 盲区**没有自己独立的一条 gap**，它只是把测试 / diff 的要求**打开**（`_blind_note` 拼进那两条里）。给它单独一条声明就成了死结：测试跑通、diff 看过，声明仍然挂着，闸门永远过不去。出口 = 显式的 `run_tests` + `git_diff`
- 盲区像 `mark_dirty` 一样作废旧的测试 / diff 结论，且**每次枚举失败都作废**（不只第一次）——目录恢复后又坏掉是新的一段盲区，上一次的绿灯不能替它背书
- `restore_obligations` 把持久化的 `tracking_incomplete` 填回 **`unknown_changes`**（不是 `tracking_errors`）：恢复那一刻并不知道目录现在读不读得了，而要保留的本来就是"曾经有一段没人看着"。若它现在仍然读不了，本轮第一次复查会把 `tracking_errors` 重新记上。同时种一个 `workspace_snapshots[root] = None` 空基线，逼本轮重新枚举建立新起点（`previous=None` 不会凭空造出 dirty 文件，也不追认盲区里发生过什么）
- 一轮完全验证通过（无 gaps）→ 义务清空；`reset_history` 开新会话也清（义务跟着它自己的会话走）

### 子 Agent 并行 + worktree 隔离（src/subagent.py + worktree.py）
- `spawn_agents(tasks)` 把多个**相互独立**的子任务并行派给子 Agent，**各自在独立 git worktree 改代码**。仅明确 `completed` 且父子均未停止时自动合并；失败、未知、未验证、取消、超时均保留隔离区和原因。合并后父会话的相关验证结果失效。
- **固定合并身份**：创建时解析 `base_sha`，创建分支与最终净补丁使用同一 SHA；`project_path` 明确记录原始 checkout。元数据存入隔离区真实 Git admin 目录的 `lingxi-worktree.json`，不进入用户 diff。支持已有 linked worktree 作为项目，不从 common Git dir 猜目标。
- **恢复与兼容**：缺失/损坏元数据或身份变化时保留隔离区，拒绝自动恢复/合并，仍允许用户显式丢弃。旧版隔离区不猜基点自动迁移。`worktree.changed_files()` 使用临时 index 获取已提交、未提交和未跟踪净变化；读取失败不能伪装成没有改动。
- 子 Agent 是 `is_subagent=True` 的会话，`ui_ref=None`（不弹前台确认）；文件/命令严格限定在自己 worktree（`tools_common._subagent_path_rejection` / `_subagent_command_rejection` best-effort 沙箱）
- `worktree.py` 管隔离区生命周期；`Session.worktree` 路由该会话所有文件/命令落点（`_project_cwd` 优先返回 worktree）

### 代码导航（src/lsp_client.py + codeintel.py + tools_codemap.py）
- `find_definition` / `find_references`：**LSP（最准，装了语言服务器）→ jedi（Python）→ 退回 search_files** 的降级链
- `codeintel.py`：tree-sitter 符号提取 / 导入追踪（多语言），支撑 `code_map` / `related_files`
- 这些都是只读工具，进 `PLAN_MODE_READONLY_TOOLS`、不弹确认

### 持久化（memory.py 并发安全）
- 所有读写 `chat_memory/` 的函数都被 `threading.RLock` 串行化（`save_session` / `_update_index` / `_write_session_title` / `load_session` / `list_sessions` / `delete_session` / `move_sessions_to_no_project` / `_ensure_memory_dir`）
- `save_session` 必须在同一临界区内取得 ID/标题/历史快照并写 session/index、rekey；只锁写盘会让 UI 的旧快照覆盖 worker 的新回复。
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

### RAG 知识库检索（src/rag/ + tools_rag.py）
- 对一个本地资料目录（`config.json` 的 `rag.kb_dir`）做语义检索，`search_knowledge` 工具返回带 `[n]` 编号和出处的片段，模型据实回答并标来源。`kb_dir` 为空 = 未启用
- **摄取**：`.md` / `.markdown` 走标题分层切块（标题路径拼进 embedding 文本提升召回）；`.pdf` 用 pypdf **按页切**、`heading` 填「第 N 页」（引用能定位页码），且**不做 Markdown 解析**——PDF 正文里的 `#` 和 ``` 只是普通字符，当成语法会凭空造出错误标题、还会因围栏状态翻转让后续切块全错
- **分域（category）**：`kb_dir` 下的**顶层子目录名**即分域名，根目录下的文件归 `default`。检索时 `scope="all"` 且索引里有多个域 → **每域各查一遍、按名次轮转合并**（`retriever._balanced_search`），某域取完名额自动让给其它域。解决「同一个库里放了两批同题材资料」时的相互挤占——比如项目实现文档与通用学习资料都在讲 MCP/RAG/Agent 主循环，标题几乎一样、向量空间里天然难分，不分域的话 top-k 会被其中一批占满，模型据此把别人的做法当成你的（**张冠李戴**，比返回噪声更难发现）
- **`min_score` 是必须设的**：`retriever` 里 `if min_score > 0` 才过滤，设成 0 等于**整个阈值机制关闭**——无论问什么都硬凑满 `top_k` 条，`tools_rag` 里「知识库中没有相关内容」那条兜底路径变成死代码。分域配额会保证另一个域也拿到名额，正是靠 `min_score` 把其中不相关的那些剔掉，两者配套
- **向量库 = Chroma**（`store.py`），落盘 `chat_memory/rag_index/chroma/`。三项关键保证：
  - **chunk hash 当 document ID** → 「哪些块已 embed 过」退化成「哪些 ID 已存在」，不需要单独的向量缓存文件；附赠内容去重（同一份资料的多个副本不会各占一个 top-k 名额）
  - **staging + 改名式事务提交**：数据先全写进 `kb_x_v1__staging`，提交时 main→`__old`、staging→main、删 old。Chroma 没有事务，靠 `load()` 的 `_recover` 从残留状态推断该回滚（main 不在、old 在）还是该前滚（main 在、有残留），绝不出现「新向量 + 旧元数据」或索引凭空消失
  - **索引锚**：manifest 整体 `json.dumps` 存进 collection metadata 的 `lingxi_manifest` 键（存字符串绕开 Chroma 的 metadata 取值类型限制 + 保留键前缀）。换目录/换模型/换端点/改切块参数没重建 → `anchor_mismatch` **fail-closed** 抛 `IndexMismatchError`，绝不拿旧索引答题。`index_status`（UI 状态行）与 `retrieve`（实际检索）共用同一判据，杜绝两处漂移
- **会话锚定知识库** `Session.rag_kb_dir`：首次成功检索时绑定当时的 `kb_dir`，之后配置切库不影响该会话——历史会话不会静默检索到另一个库。空锚点只在**检索确实跑在有效索引上**之后才绑定（未建索引的失败检索不该钉死一个空会话）
- 旧版 numpy 散文件索引（`rag_index/<name>/*.npy`）不兼容，检测到只提示重建

### 长期记忆（src/memory_store.py，跨会话）
- 让角色"天生记得"用户：`remember(fact)` / `forget(query)` 两个工具存取，`get_system_prompt()` 末尾自动注入记忆（不靠 AI 主动查，开口就记得）。**注入有预算**：`render_memories_for_prompt(max_chars=MEMORY_MAX_CHARS)` 默认 4000 字符，超出保留最近的——不是"无限全量注入"
- 存 `chat_memory/long_term_memory.json`（`{memories: [{id, text, created, scope}]}`，scope 默认 global）。独立 `RLock`，跟 `memory.py`（会话历史）分开
- **数据安全**：`_save` 用临时文件 + `os.replace` **原子写**（崩溃不留半截）；`_load` 区分"真损坏"（JSON/编码错 → 重置空可重建）和"瞬时错误"（IO/占用 → 抛 `_MemoryLoadError`，**写操作遇到必中止、绝不 _save 写空丢数据**）
- v1 不用 embedding（单人助手记忆少，按预算全量注入又快又准）；注入段会被 Anthropic/MiMo 缓存覆盖，每轮重读保持最新。当前截断策略是"按时间取最近"，不是按重要性——条数多了会丢老记忆，这是已知取舍
- `remember`/`forget` 是本地安全操作，**不弹确认**、Plan 模式放行（在 `PLAN_MODE_READONLY_TOOLS` 里）

### 持久化文件
| 文件 | 内容 |
|------|------|
| `chat_memory/index.json` | 会话列表（id + title + 时间 + project tag） |
| `chat_memory/long_term_memory.json` | 跨会话长期记忆（remember/forget 存取，自动注入 system prompt） |
| `chat_memory/rag_index/chroma/` | RAG 向量库（Chroma PersistentClient 数据目录；collection 名 `kb_<store>_v1`） |
| `chat_memory/<session_id>.json` | 会话消息历史（HumanMessage/AIMessage/ToolMessage 序列化）+ project / session_kind / rag_kb_dir / agent_mode + `progress`（计划 / 台账 / revision / `last_run` / `pending_verification` / `last_committed_operation` / `recent_operations`） |
| `chat_memory/<session_id>.inflight.json` | 有副作用工具的**执行前**记录（sidecar）：session_id / run_id / operation_id / 工具名 / tool_call_id / 起始 revision / 路径。提交后删除；**不进侧栏索引**，也不能作为复活已删除会话的依据（`delete_session` 会一并删掉） |
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
| `update_plan` / `set_step_status` | 任务计划：创建或显式调整 / 增量改单步状态（Plan 放行；驱动计划面板） |
| `get_project_instructions` | 读目标路径适用的项目规则（CLAUDE.md / AGENTS.md / .lingxirules；只读、Plan 放行） |
| `notify_user` | 主动给用户推 Telegram 通知（分级；本地安全、Plan 放行） |
| `code_map` | 代码库符号地图（命名组正则提取函数/类，commonpath 防越界；Plan 只读放行） |
| `run_tests` | 跑 pytest（`_resolve_python()` 选解释器：项目 venv → 开发期 sys.executable → PATH；精炼失败定位 + 总耗时；`encoding=utf-8` 防 GBK 崩） |
| `git_diff` / `git_log` / `git_status` | 只读 git（看改动/历史/状态；commonpath 越界防护；Plan 只读放行） |
| `git_stage` / `git_unstage` / `git_commit` | git 写操作（暂存/取消暂存/本地提交，**无 push**）；**执行前强制弹确认卡**（按危险操作处理、不给"记住"选项）；路径白名单防注入，commit 不自动暂存、校验信息非空 |
| `check_code` | 静态检查单文件（lint/语法）：Python 用 `ruff check --select F,E9`（没装退化到 `py_compile`），其它语言用 config 的 `check_command`；只读不弹确认、Plan 放行 |
| `apply_patch` | 多文件补丁（Codex 风格 `*** Begin Patch`）：统一校验、确认后，暂存新内容和原文件备份再写入。写入失败回滚，回滚受阻保留备份并报告；审批期间目标变化拒绝覆盖。不承诺崩溃时跨文件原子性。写工具、Plan/遥控自动拦 |
| `search_knowledge` | 知识库语义检索（`query` + `scope` 分域，默认 all 各域按配额取；只读、Plan 放行、不弹确认；`kb_dir` 未配置/索引未建/索引锚不一致都返回明确提示而非空结果） |
| `fetch_url` / `web_search` | 网络只读：`fetch_url` 抓网址（http(s) only、HTML 去标签转文本、二进制拒绝、无需 key）；`web_search` 用 Tavily（config `web_search_api_key`，没配优雅降级）。均进 Plan 只读、**不进遥控白名单**（网络外发默认不给远程） |

> 写盘类工具（edit/write/append）共用 `tools.py:_confirm_file_write()`：算 unified diff → `ui.confirm_edit` 弹蓝色卡片 → worker 阻塞等审批。CLI/测试无 UI 时直接放行。

### 计划稳定性（tools.py / roles.py / UI）
- 创建后默认固定步骤文字、顺序和数量；`set_step_status` 只更新状态。`update_plan` 兼容同一清单的进度更新，但不做模糊匹配或隐式合并。
- 重列、改名、增删、清空、开始新任务或回退进度必须传 `explanation`；缺失原因时整次拒绝且保留原计划。原因由模型随工具调用说明，不增加用户确认。相同状态重复上报不刷新 UI。
- `show_plan` 信号携带来源 Session 和独立快照；发送时过滤后台会话，主线程接收时再次核对当前会话及最新计划，避免切会话时排队的旧消息或后台子 Agent 覆盖前台面板。
- 静态提示与尾部运行态统一要求用 `set_step_status` 推进，显式调整结构才调用带 `explanation` 的 `update_plan`。回归见 `test_update_plan.py` / `test_plan_panel.py` / `test_prompt_cache_stability.py`。

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
来自 `docs/glm_code_review_2026-06-19.md`（该文件本地可见、未入库）的剩余项：默认模型 fallback 落到 Claude Code（`agent.py`）；`read_file` 大文件全量 `readlines()`（`tools.py`）；token 估算 `×0.7` 对英文高估（`streaming.py`）。流式错误分类和补丁写入失败回滚已实现，边界见上文。
