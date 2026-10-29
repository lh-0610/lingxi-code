# 其他子系统

> 从 CLAUDE.md 拆出的架构说明；入口与文档地图见 [CLAUDE.md](../../CLAUDE.md)。

## 子 Agent 并行 + worktree 隔离（src/subagent.py + worktree.py）
- `spawn_agents(tasks)` 把多个**相互独立**的子任务并行派给子 Agent，**各自在独立 git worktree 改代码**。仅明确 `completed` 且父子均未停止时自动合并；失败、未知、未验证、取消、超时均保留隔离区和原因。合并后父会话的相关验证结果失效。
- **固定合并身份**：创建时解析 `base_sha`，创建分支与最终净补丁使用同一 SHA；`project_path` 明确记录原始 checkout。元数据存入隔离区真实 Git admin 目录的 `lingxi-worktree.json`，不进入用户 diff。支持已有 linked worktree 作为项目，不从 common Git dir 猜目标。
- **恢复与兼容**：缺失/损坏元数据或身份变化时保留隔离区，拒绝自动恢复/合并，仍允许用户显式丢弃。旧版隔离区不猜基点自动迁移。`worktree.changed_files()` 使用临时 index 获取已提交、未提交和未跟踪净变化；读取失败不能伪装成没有改动。
- 子 Agent 是 `is_subagent=True` 的会话，`ui_ref=None`（不弹前台确认）；文件/命令严格限定在自己 worktree（`tools_common._subagent_path_rejection` / `_subagent_command_rejection` best-effort 沙箱）
- `worktree.py` 管隔离区生命周期；`Session.worktree` 路由该会话所有文件/命令落点（`_project_cwd` 优先返回 worktree）

## 代码导航（src/lsp_client.py + codeintel.py + tools_codemap.py）
- `find_definition` / `find_references`：**LSP（最准，装了语言服务器）→ jedi（Python）→ 退回 search_files** 的降级链
- `codeintel.py`：tree-sitter 符号提取 / 导入追踪（多语言），支撑 `code_map` / `related_files`
- 这些都是只读工具，进 `PLAN_MODE_READONLY_TOOLS`、不弹确认

## 持久化（memory.py 并发安全）
- 所有读写 `chat_memory/` 的函数都被 `threading.RLock` 串行化（`save_session` / `_update_index` / `_write_session_title` / `load_session` / `list_sessions` / `delete_session` / `move_sessions_to_no_project` / `_ensure_memory_dir`）
- save_session 必须在同一临界区内取得 ID/标题/历史快照并写 session/index、rekey；只锁写盘会让 UI 的旧快照覆盖 worker 的新回复。
- 用 RLock 不用 Lock：`save_session` 自己持锁时还会调 `_update_index`（也持锁），普通 Lock 会自死锁
- 修复了原来"快速发两条消息时，标题生成线程和 save_session 同时改 index.json 互相覆盖丢会话"的并发 bug

## 会话历史保留与侧栏加载更多
- **索引全量保留，删除只能由用户发起**：`_update_index` 不做任何数量截断，新会话插最前、旧条目原地更新（刷新 `updated`）。早先索引截断到 `SESSION_HISTORY_LIMIT=50` 并把挤出名额的会话**正文一并删掉**——内存里还开着的靠注册表检查幸免，没打开的连正文带索引一起消失且无法恢复。`SESSION_HISTORY_LIMIT` 常量已废除：不要把"历史保留"和"界面展示数量"混为一谈。
- **展示数量是界面状态**（`limits.SIDEBAR_GROUP_VISIBLE_INITIAL/STEP`，各 30）：侧栏每个分组初始显示 30 条，组尾「加载更多」按步长追加；每组按 `collapse_key`（项目路径 / 无项目 / RAG 专用键）**独立计数**——RAG 与编码"无项目"都以 project=None 渲染，计数与折叠共用同一套键区分，互不串用。计数是内存级（`_group_visible_counts`），普通刷新（重渲染）不重置；删除会话后剩余不足时按钮自然消失。
- **排序**：更新时间倒序，同刻按 id 倒序（id 含"年月日_时分秒_微秒"，跨重启稳定）。保存旧会话会刷新它的 `updated`，把它带回所在分组的近期位置。
- **正常保存永不触发历史清理**；用户主动删除（`delete_session`）连正文 + 索引条目 + 索引修复登记 + inflight sidecar 一起删，修复路径以正文为唯一真相（正文不在 → 销案不复活），已删除的会话不会因后续保存、索引修复或重启复活。不做孤儿 JSON 扫描重建索引——那会复活用户主动删除的会话，也不声称能恢复已被旧代码删除的数据。

## 角色卡
- `roles/*.md` 直接作为 system prompt
- 激活的角色记录在 `chat_memory/role_config.json`，启动时 `load_saved_role_card()` 自动恢复

## 系统托盘（src/floating.py）
- `create_tray(app, chat_window, icon_path)`：`QSystemTrayIcon` + 右键菜单（打开对话 / 退出）+ 双击唤起窗口
- `main.py` 设 `setQuitOnLastWindowClosed(False)`，关窗只隐藏、由托盘维持后台；托盘"退出"才真退
- `_restore_window` 唤起时**保留最大化/全屏状态**（不用 `showNormal()`，否则会缩回默认尺寸）
- 桌面宠物已移除（原 DesktopPet / GIF 动画 / `set_thinking` 钩子全部删除）；`thinking_indicator` 是聊天窗口自己的"思考中…"指示器，与托盘无关

## MCP 客户端（src/mcp_client.py，可选功能）
- 让灵犀连外部 MCP server（filesystem / fetch / context7 / memory 等），把远程工具动态注入到 `ALL_TOOLS`，跟内置工具一样被 AI 调用。**没装 `mcp` 包 / 没配 `mcp_servers` 时整段静默跳过**（零回归）
- 配置在 `config.json` 的 `mcp_servers`（dict，key=server 名）：`transport` 支持 `stdio`（command+args）/ `sse`（url）/ `streamable_http`
- **同步/异步桥接**：mcp SDK 是 asyncio 异步、灵犀 agent 是同步。`mcp_client.py` 起**一个常驻后台线程跑 asyncio loop**；每个 server 一个常驻协程，`async with stdio_client/sse_client ... await _shutdown_event.wait()` 挂住保持连接（**绝不能 return session 出去，上下文一退连接就断**）。工具调用走 `run_coroutine_threadsafe(session.call_tool(...), loop).result()` 从 agent 线程投进 loop
- **致命坑（已避开）**：不要在 loop 自己的线程上对同一 loop 用 `run_coroutine_threadsafe().result()` —— 自死锁。`_build_mcp_tools` 是纯同步、读 `_server_loop` 提前缓存好的 `_server_tools`
- **包装工具**：`_wrap_mcp_tool` 逐个包装，一个工具出错只跳过它并记日志（异常冒到守护线程会让整个 loop 退出、已连上的 server 一起断）。`requirements` 是 `mcp>=1.9`：1.x 的字段是驼峰（`inputSchema` / `isError`），2.x 改成下划线（`input_schema` / `is_error`），统一走 `_field` 两种都认；参数 `type` 可能是列表（如 `["integer", "null"]`）。工具结果 `is_error` 时返回「MCP 工具报错：…」。stdio 启动前先查命令存不存在，找不到直接说缺哪个命令
- 工具名加 `mcp_{server}_{tool}` 前缀（防撞内置工具）；`_execute_tool` 里 `name.startswith("mcp_")` 的工具走**执行前确认**（MCP 工具能干任意事）；Plan 模式当写工具拦截
- 启动时 `agent.py` 后台线程调 `init_mcp()`，工具就绪后清 `_BOUND_LLM_CACHE` 让下次 stream 重新 `bind_tools`；关窗 `main.py` 调 `shutdown()`
- 打包：`lingxi.spec` 用 `collect_submodules('mcp')` + `collect_data_files('jsonschema_specifications')`（懒导入 + 数据文件，静态分析抓不到）

## RAG 知识库检索（src/rag/ + tools_rag.py）
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

## 长期记忆（src/memory_store.py，跨会话）
- 让角色"天生记得"用户：`remember(fact)` / `forget(query)` 两个工具存取，`get_system_prompt()` 末尾自动注入记忆（不靠 AI 主动查，开口就记得）。**注入有预算**：`render_memories_for_prompt(max_chars=MEMORY_MAX_CHARS)` 默认 4000 字符，超出保留最近的——不是"无限全量注入"
- 存 `chat_memory/long_term_memory.json`（`{memories: [{id, text, created, scope}]}`，scope 默认 global）。独立 `RLock`，跟 `memory.py`（会话历史）分开
- **数据安全**：`_save` 用临时文件 + `os.replace` **原子写**（崩溃不留半截）；`_load` 区分"真损坏"（JSON/编码错 → 重置空可重建）和"瞬时错误"（IO/占用 → 抛 `_MemoryLoadError`，**写操作遇到必中止、绝不 _save 写空丢数据**）
- v1 不用 embedding（单人助手记忆少，按预算全量注入又快又准）；注入段会被 Anthropic/MiMo 缓存覆盖，每轮重读保持最新。当前截断策略是"按时间取最近"，不是按重要性——条数多了会丢老记忆，这是已知取舍
- `remember`/`forget` 是本地安全操作，**不弹确认**、Plan 模式放行（在 `PLAN_MODE_READONLY_TOOLS` 里）

## 持久化文件
| 文件 | 内容 |
|------|------|
| `chat_memory/index.json` | 会话列表（id + title + 时间 + project tag） |
| `chat_memory/long_term_memory.json` | 跨会话长期记忆（remember/forget 存取，自动注入 system prompt） |
| `chat_memory/rag_index/chroma/` | RAG 向量库（Chroma PersistentClient 数据目录；collection 名 `kb_<store>_v1`） |
| `chat_memory/<session_id>.json` | 会话消息历史（HumanMessage/AIMessage/ToolMessage 序列化；内部消息带 `lingxi_internal` / `lingxi_kind`，恢复说明另带 `lingxi_recovery` 交接记录）+ project / session_kind / rag_kb_dir / agent_mode + `progress`（计划 / 台账 / revision / `last_run`（含 `evidence.validation_runs` 结构化检查记录、`model` 稳定标识、`workspace` 现场锚点）/ `pending_verification`（含 `file_paths`：改动文件的实际位置）/ `last_committed_operation` / `recent_operations`） |
| `chat_memory/<session_id>.inflight.unreadable-<哈希>.json` | 执行前记录里无法解析的条目的**完整原文**（交接前隔离留底；恢复说明里只有摘要和对它的引用）。随 `delete_session` 删除 |
| `chat_memory/<session_id>.inflight.json` | 有副作用工具的**执行前**记录（sidecar）：session_id / run_id / operation_id / 工具名 / tool_call_id / 起始 revision / 路径 / `work_root`（工具实际的工作目录）。提交后删除；解析不了的条目原样保留、交接到恢复说明后才摘除；**不进侧栏索引**，也不能作为复活已删除会话的依据（`delete_session` 会一并删掉） |
| `chat_memory/file_history/<id>.json` + `.bin` | 文件撤销的恢复材料（B11a）：归属记录（session/run/task/operation_id + 工具 + 工作区 + 目标路径 + 写前/写后指纹 + 阶段）与写前原始字节。随 `delete_session` 删除；不进侧栏索引 |
| `chat_memory/projects.json` | 注册的项目列表 + 当前激活项目（`{current, projects: [{path, name}]}`） |
| `chat_memory/role_config.json` | 当前激活的角色卡名 |
| `chat_memory/ui_prefs.json` | UI 偏好（如关闭按钮记住的选择） |
| `chat_memory/theme_config.json` | 主题选择（light / dark） |
| `logs/YYYYMMDD.log` | 按日期分的日志 |
