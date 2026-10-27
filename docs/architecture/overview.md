# 项目结构与模型

> 从 CLAUDE.md 拆出的架构说明；入口与文档地图见 [CLAUDE.md](../../CLAUDE.md)。

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
  background.py          # 后台命令唯一注册表（B10a）：冻结归属 / 结构化快照 / 有界停止 / 有界输出；工具与后续面板共用
  tools_git.py           # git 工具：git_diff/log/status/stage/unstage/commit（写操作弹确认、无 push）
  tools_web.py           # 网络只读：fetch_url（SSRF 防护 + 重定向逐跳校验 + 默认不走代理）/ web_search（Tavily）
  tools_codemap.py       # code_map（符号地图）/ find_tests / related_files
  llm_errors.py          # 模型请求错误分类（限流/上下文溢出/鉴权/瞬时）+ Retry-After 解析 + 重试策略
  run_records.py         # 运行身份（run_id/来源/终态）+ 统一 begin/finalize + inflight sidecar + 完成回执 + 待验证义务归一化 + 结果快照
  result_view.py         # 运行结果快照 → 给人看的结论（纯函数、不依赖 Qt；标题/检查行/过期/保存提示/能否继续的判定都在这里）
  recovery.py            # 恢复检查与继续入口（B04）：主线程预检 / worker 里现场核对 / 补齐悬空调用 / 把结果未知的操作并入待验证义务 / 恢复说明
  workspace_anchor.py    # 现场锚点（B04）：涉及文件指纹 + HEAD/分支/根提交；恢复时比对，判断"还是不是那个现场、那个仓库"
  task_state.py          # 任务身份、来源追溯与边界控制（B05）：任务 ID / 白名单 / 固定限制校验 / 计划变动比对 / 新任务切换与撤销
  tools_rag.py           # search_knowledge（知识库语义检索，只读、Plan 放行、带 scope 分域参数）
  rag/                   # RAG 知识库引擎（详见 subsystems.md「RAG 知识库检索」节）
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
  file_history.py        # 文件改动的安全撤销底层（B11a）：写前逐文件备份 + 归属记录 + 预检/执行；edit/write/append 接入
  projects.py            # 项目（工作区）管理：chat_memory/projects.json 读写（RLock + 原子写 + 损坏备份）
  roles.py               # 角色卡加载 + get_system_prompt（**只拼跨轮稳定的**：角色卡/项目上下文/项目规则/记忆）+ get_volatile_context（每轮都变的：Plan 提示/计划/台账，由 streaming 追到历史尾部）+ get_external_agent_context
  images.py              # 图片输入格式归一化（视觉/多模态，Anthropic/OpenAI/Gemini 协议差异）
  debug_log.py           # F12 调试：请求/响应 record 环形缓冲 + Qt Signal
  claude_code.py         # 通过 subprocess 调本地 Claude Code CLI（permission-mode 映射 Plan/Act；--append-system-prompt-file + stdin 避 32K 命令行）
  floating.py            # 系统托盘 create_tray（关窗维持后台 + 双击唤起 + _restore_window 保留最大化）

  ui/                    # UI 包（chat_window.py 用 mixin 拆分）
    __init__.py          # 导出 ChatUI / SettingsDialog
    chat_window.py       # ChatUI 主窗口（__init__/build_ui/eventFilter/渲染原语 _append_html/show_message/_t），组装下列 mixin
    send.py              # SendMixin：发送入口（按钮/回车/遥控）、起 worker 跑 agent_loop、视觉桥接、结束回调、强制停止
    run_admission.py     # RunAdmissionMixin + _PendingRunRequest：待启动请求的准入（B09a 统一退出屏障）
    queue_dispatch.py    # QueueDispatchMixin：运行中输入队列的入队、派发与收尾（B09b）
    run_results.py       # RunResultMixin：运行结果卡（B06）与「继续任务」（B04）的 UI 侧
    message_view.py      # 消息流的块级真控件渲染（圆角卡/阴影/可展开思考块）—— 逐步替代 QTextBrowser
    result_card.py       # 运行结果卡控件（只画 result_view 算好的结论；判断在那边、画在这边）
    task_panel.py        # 任务面板浮层与限制编辑对话框（B05）：三段式要求/计划/执行状态，可撤销横幅，硬预算校验
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

## 支持模型

| 模型名称 | 类型 | 模型 ID | 视觉 |
|----------|------|---------|------|
| MiMo V2.5 Pro / V2.5 / V2 Pro | mimo | mimo-v2.5-pro / 2.5 / 2-pro | 否 |
| MiMo V2 Omni（多模态） | mimo | mimo-v2-omni | 是 |
| Claude Code | claude-code | 本地 `claude` CLI | 否 |
| Qwen3.5 本地 | ollama | qwen3.5:latest | 否 |
| Qwen-Plus / Max / Turbo / Qwen3.5-Plus | cloud | qwen-* | 否 |
| Claude Sonnet 4 / Haiku 3.5 | anthropic | claude-sonnet-4-20250514 / claude-3-5-haiku-20241022 | 是 |
| DeepSeek V4 Flash / Pro | deepseek | deepseek-v4-flash / pro | 否 |
| 用户自定义模型 | custom | config.json `custom_models`（OpenAI / Anthropic / Responses 协议自填） | 看配置 |

> **自定义模型**：`config.json` 的 `custom_models`（list），每项 `{name, model_id, api_key, base_url, protocol, supports_vision, supports_thinking}`。设置弹窗里可视化增删改。`models.py:_build_model_list()` 把它们以 `⚙` 前缀合进 `MODEL_LIST`，`_create_llm` 按 `protocol` 分发，支持 `openai` / `anthropic` / `responses` 三种兼容接口——**多数兼容端点的模型零代码改动就能加**，只有协议不兼容或需特殊 `extra_body` 的才要动 `models.py`。
