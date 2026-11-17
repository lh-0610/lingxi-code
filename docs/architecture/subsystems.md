# 其他子系统

> 从 CLAUDE.md 拆出的架构说明；入口与文档地图见 [CLAUDE.md](../../CLAUDE.md)。

## 子 Agent 并行 + worktree 隔离（src/subagent.py + worktree.py）
- `spawn_agents(tasks)` 把多个**相互独立**的子任务并行派给子 Agent，**各自在独立 git worktree 改代码**。仅明确 `completed` 且父子均未停止时自动合并；失败、未知、未验证、取消、超时均保留隔离区和原因。合并后父会话的相关验证结果失效。
- **固定合并身份**：创建时解析 `base_sha`，创建分支与最终净补丁使用同一 SHA；`project_path` 明确记录原始 checkout。元数据存入隔离区真实 Git admin 目录的 `lingxi-worktree.json`，不进入用户 diff。支持已有 linked worktree 作为项目，不从 common Git dir 猜目标。
- **恢复与兼容**：缺失/损坏元数据或身份变化时保留隔离区，拒绝自动恢复/合并，仍允许用户显式丢弃。旧版隔离区不猜基点自动迁移。`worktree.changed_files()` 使用临时 index 获取已提交、未提交和未跟踪净变化；读取失败不能伪装成没有改动。
- 子 Agent 是 `is_subagent=True` 的会话，`ui_ref=None`（不弹前台确认）；文件/命令严格限定在自己 worktree（`tools_common._subagent_path_rejection` / `_subagent_command_rejection` best-effort 沙箱）
- `worktree.py` 管隔离区生命周期；`Session.worktree` 路由该会话所有文件/命令落点（`_project_cwd` 优先返回 worktree）

## 只读探索子代理（src/tools_explore.py）
- `explore(question, thoroughness)`：主 Agent 把要翻好几个文件才弄得清的问题派出去，子代理在自己的上下文里搜、读，只交回带 `路径:行号` 的报告。七家主流 coding agent 都这么做；评测 011 里主循环挨个读文件要 60 轮
- 子会话 `is_subagent=True`、`worktree=项目根`（只能读项目里的文件），不建 worktree；三个会话级字段：`allowed_tools`（`resolve_bound_llm` 只绑这些、`_execute_tool` 再硬拦）、`max_rounds`（quick 8 / medium 20 / thorough 40）、`system_prompt_override`（`get_system_prompt` 直接返回它——streaming 每轮都会重渲染系统提示词）
- 跑满上限时多调一次模型（不带工具）把已查到的写成报告，而不是只交回"已达上限"；父任务停止时子代理跟着停
- 遥控只读模式不放行：子代理能读项目里任何文件，绕得过敏感文件黑名单

## 代码模式与代码地图（src/tools_code.py + repo_map.py）
- DeepSeek 一轮只调 1.2~1.6 个工具、批量工具给了也不用，这两样不靠它自觉
- `repo_map.build_task_map`：大项目（≥30 个源码文件，任何语言）里每条新消息开跑前，按任务里的词（IDF 加权，中文切 2~4 字）和引用图挑最多 12 个文件，列出类 / 函数和行号，放在系统提示词末尾（`session.task_map`）。最高分 <15（任务词在代码里对不上）时改给项目概览：压缩过的三层目录树 + PageRank 挑的核心文件；消息短于 6 字什么都不给
- 同一会话尽量不换地图（`agent._prepare_task_map`）：地图变了系统提示词就变，整段历史按未命中计费。新消息算不出地图（"继续"）留旧的；算出了不同的，只在对话 ≤ 8000 token 时换
- `code_graph.py`：A 文件里出现 B 文件定义的类 / 函数名就记 A→B（不解析 import，所有语言通用）；方法名权重 0.2、定义在 >6 个文件的名字不算、太弱的边丢掉。git 仓库按 `git ls-files` 列文件（跳过 .gitignore）
- `run_code`：模型写一小段 Python 调只读工具（`CODE_TOOL_NAMES`），一轮做完多次读 / 搜。写工具不进程序——执行前记录、改动确认、验证追踪、Plan 拦截都挂在普通工具那条路上

## 代码导航（src/lsp_client.py + codeintel.py + tools_codemap.py）
- `find_definition` / `find_references`：**LSP（最准，装了语言服务器）→ jedi（Python）→ 退回 search_files** 的降级链
- **LSP 多语言**：按文件扩展名选语言组（python / typescript〔含 js/jsx/tsx〕/ go / rust / cpp〔含 .c/.h〕/ java），每组一个常驻服务器单例（`_servers` dict，不再写死一个 pyright）。默认命令 pyright-langserver→pylsp、typescript-language-server、gopls、rust-analyzer、clangd、jdtls；`config.lsp_servers` 覆盖 python、`config.lsp_languages`（对象，值为命令字符串或候选列表）覆盖其它组。`didOpen` 的 `languageId` 也按扩展名给（tsx→typescriptreact、jsx→javascriptreact、.h→cpp），不再写死 `"python"`——这是旧版只支持 Python 的根因。jdtls 启动补 `-data <工作区>`，工作区**不在项目根里**（早先建 `<项目根>/.lingxi-jdtls-workspace` 会污染用户项目、被 git 看见）：按项目根 realpath 的 sha256 前 16 位 keyed 到 `get_data_dir()/lsp_workspaces/<hash>`，dev 期数据根恰是项目根时退回临时目录（`_jdtls_workspace`，best-effort，起不来就降级）。**Windows 上 npm 装的语言服务器（`.cmd` shim）裸名字起不来**：`_LspServer.__init__` 用 `shutil.which(argv[0])` 解析成完整路径再交给 CreateProcess（它只认 `.exe`、不查 PATHEXT，裸名字会 WinError 2）。认不出扩展名 → 不起服务器、`definition/references` 返回 None
- **jedi 只兜 Python**：LSP 没命中后，非 `.py/.pyw` 文件不再拿 jedi 硬解析（会得到误导性空结果），而是诚实提示"装对应语言服务器 / 用 search_files"（`_non_python_nav_hint`）
- `codeintel.py`：tree-sitter 符号提取 / 导入追踪，支撑 `code_map` / `related_files` / 代码地图。Python / JS / TS 用官方单语言包和专门遍历；其余语言用 tree-sitter-language-pack 0.x（语法预编译在 wheel 里；1.x 运行时联网下载，不用）+ 通用遍历（按节点类型名认定义）；没有语法库的退回通用正则。Vue / Svelte 只解析 `<script>`
- `related_files`：Python / JS / TS 按 import 语句；其余语言按引用图给"它用到的 / 用到它的"
- 这些都是只读工具，进 `PLAN_MODE_READONLY_TOOLS`、不弹确认

## 持久化（memory.py 并发安全）
- 所有读写 `chat_memory/` 的函数都被 `threading.RLock` 串行化（`save_session` / `_update_index` / `_write_session_title` / `load_session` / `list_sessions` / `delete_session` / `move_sessions_to_no_project` / `rename_session` / `_ensure_memory_dir`）。例外：`search_sessions` 只在读 index 时持锁，逐个读正文在锁外（见「会话重命名与跨会话搜索」）
- save_session 必须在同一临界区内取得 ID/标题/历史快照并写 session/index、rekey；只锁写盘会让 UI 的旧快照覆盖 worker 的新回复。
- 用 RLock 不用 Lock：`save_session` 自己持锁时还会调 `_update_index`（也持锁），普通 Lock 会自死锁
- 修复了原来"快速发两条消息时，标题生成线程和 save_session 同时改 index.json 互相覆盖丢会话"的并发 bug

## 会话历史保留与侧栏加载更多
- **索引全量保留，删除只能由用户发起**：`_update_index` 不做任何数量截断，新会话插最前、旧条目原地更新（刷新 `updated`）。早先索引截断到 `SESSION_HISTORY_LIMIT=50` 并把挤出名额的会话**正文一并删掉**——内存里还开着的靠注册表检查幸免，没打开的连正文带索引一起消失且无法恢复。`SESSION_HISTORY_LIMIT` 常量已废除：不要把"历史保留"和"界面展示数量"混为一谈。
- **展示数量是界面状态**（`limits.SIDEBAR_GROUP_VISIBLE_INITIAL/STEP`，各 30）：侧栏每个分组初始显示 30 条，组尾「加载更多」按步长追加；每组按 `collapse_key`（项目路径 / 无项目 / RAG 专用键）**独立计数**——RAG 与编码"无项目"都以 project=None 渲染，计数与折叠共用同一套键区分，互不串用。计数是内存级（`_group_visible_counts`），普通刷新（重渲染）不重置；删除会话后剩余不足时按钮自然消失。
- **排序**：更新时间倒序，同刻按 id 倒序（id 含"年月日_时分秒_微秒"，跨重启稳定）。**只有对话内容变了的保存才刷新 `updated`**（`save_session_report` 比对正文 messages，没变就沿用旧值、`_update_index(touch=False)`），把有新活动的会话带回所在分组的近期位置。切会话前会先把当前会话存一遍——早先每次保存都刷新，看过一眼的会话就被顶到最前（用户实测：点谁谁跑到第一个），守它的是 `scripts/test_session_order_on_switch.py`。
- **正常保存永不触发历史清理**；用户主动删除（`delete_session`）连正文 + 索引条目 + 索引修复登记 + inflight sidecar 一起删，修复路径以正文为唯一真相（正文不在 → 销案不复活），已删除的会话不会因后续保存、索引修复或重启复活。不做孤儿 JSON 扫描重建索引——那会复活用户主动删除的会话，也不声称能恢复已被旧代码删除的数据。

## 对话时间轴：编辑 / 回退 / 分叉（src/memory.py + src/ui/timeline.py）
- 每条用户气泡下挂三个幽灵按钮（`MessageView.add_user_actions`，编辑/回退/分叉），点击发 `user_edit_requested / user_rewind_requested / user_fork_requested` 信号，参数是该消息在 `chat_history` 里的**绝对下标**。重绘（`_replay_history` 用 `enumerate`）和实时发送（`_do_send` 记下追加前的 `len`）两路都挂。
- 三个操作统一以「被点消息之前」为边界（`TimelineMixin`，src/ui/timeline.py）：**编辑** = 截断到该下标 + 把改后文本走 `_do_send` 重发（编辑框预填的是**用户原始输入**：`_user_message_text` 剥掉程序追加的 @ 引用提示与图片提示——否则重发会把提示当正文再追加一遍、越滚越长；原消息带图片时弹一行提醒「编辑重发只发文字、图片不保留」，图片块只存 base64 不存源路径、无法随文字重发，`_message_has_images` 按块结构判定）；**回退** = 截断并落盘；**分叉** = 把前缀复制成新会话（`fork_session`，继承模型/模式/权限/思考/分类，运行态从零、拿新 id），原会话不动。
- 后端核心 `truncate_history(sess, cut_index)`：在**同一 `snapshot_lock` 临界区**内截断历史 + 作废 compaction（`covered_upto` 是绝对下标）+ 按存活 ToolMessage 过滤 `evicted_tool_results` + `_clear_progress` 重置运行态（RLock 可重入）。`rewind_session` 再落盘：截断后 >1 条正常存盘覆盖旧正文；回退到空（只剩 SystemMessage）走 `delete_session(old_id, keep_file_history=True)`（存盘守卫 `len<=1` 会跳过空历史，故走删除），但**保留 `current_session_id` 与文件撤销材料**：磁盘改动并没被回退，顶栏撤销靠会话 id + file_history 定位，清掉 id 或删掉材料就让"回退到空"后的改动再也撤不回。
- **绝不撤销磁盘文件改动**：不碰 `file_history` 的记录/blob，顶栏「撤销」照常可用。没有"每轮进度快照"，所以回退/分叉把计划/台账/待验证义务/结果卡/任务身份重置为干净态，而非精确还原到某轮——诚实且无损坏。分叉的新会话 id 查不到旧撤销记录（不继承）。
- 都改/复制历史，先过 `_worker_alive(sess) or resume_pending` 闸（与 retry 同源：worker 还活着就拒绝，不排队）；回退另弹确认框说明"不撤销文件"。下标失效（消息已不在）→ 提示 + 重绘自愈。

## 会话重命名与跨会话搜索（src/memory.py + src/ui/sidebar.py）
- **重命名 `rename_session(session_id, title)`**：与 `move_sessions_to_no_project` 同样的"三处一起改"，同一 `_LOCK` 临界区内改 ① 内存里已打开会话的 `current_session_title`（经 `live_sessions()` 取一致快照；只改磁盘的话下次 `save_session` 会用旧内存标题覆盖回去——正常流程必现）② index.json 的 title ③ `<id>.json` 正文 title。**不刷新 `updated`**：改名是元数据编辑、不是新活动，不该把会话顶到分组最前打乱历史顺序。用户标题只折叠空白 + 截断 100 字符（`_sanitize_user_title`，不像自动标题那样剥标点/书名号）；空标题抛 `ValueError`。UI 走会话行右键菜单「重命名」→ `dialogs.get_text` 主题输入框（生成中也可改名，标题不动历史/运行态）。
- **跨会话搜索 `search_sessions(query, *, kind=None, project="__all__")`**：先 `list_sessions` 拿索引（只在读 index 时持 `_LOCK`，读完即释放），再**锁外**逐个读正文（`_atomic_write_json` 保证读到完整旧/新文件）。只搜真实用户输入 + 助手可见正文（`content_text` 跳过思考块），跳过 SystemMessage / 内部消息（`lingxi_internal`）/ 工具结果；大小写不敏感。结果按索引顺序（更新时间倒序），每项含 `snippet`（首个命中处上下文预览）/ `match_in_title` / `match_count`。单个正文读不出（损坏/版本过新）只跳过其正文、保留标题命中，不中断整次搜索。
- **搜索 UI 不卡主线程**：侧栏「历史记录」下的搜索框防抖 250ms（回车立即搜），扫描放 daemon worker，结果经 `SignalBridge.session_search_done(str, object)` 回主线程渲染。**worker 必须继承启动时刻的数据根**（`paths.get_data_dir()` → 线程内 `set_data_dir`，与 send worker 同套路）：`set_data_dir` 是线程本地的，不继承则 Web 多用户模式读到默认根（别人的数据）、测试读不到隔离目录。搜索态下 `_refresh_session_list` 改渲染扁平结果行（标题 + 项目名 + 命中预览，点击 `_load_session` 跳转）；过期结果（查询词已变）丢弃；切工作区（`switch_workspace`）清空搜索（kind 变了旧结果失效）。

## 角色卡
- `roles/*.md` 直接作为 system prompt
- 激活的角色记录在 `chat_memory/role_config.json`，启动时 `load_saved_role_card()` 自动恢复

## 系统托盘（src/floating.py）
- `create_tray(app, chat_window, icon_path)`：`QSystemTrayIcon` + 右键菜单（打开对话 / 退出）+ 单击唤起窗口；把自己挂到 `chat_window._tray`
- `main.py` 设 `setQuitOnLastWindowClosed(False)`；`ChatUI.closeEvent` 点 × 只隐藏（同 Codex，不弹框），第一次冒托盘气泡；托盘不可用时关窗等于退出
- 托盘"退出" → `ChatUI.request_quit`：还有对话（worker 线程活着）或后台命令在跑时弹 `QuitConfirmDialog`（默认取消），确认后置 `_quitting` 再 `QApplication.quit()`——Qt 6 的 quit 会去关可见窗口，不置标志 closeEvent 又会把它收进托盘、退出被打断
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

## 主题弹窗（src/ui/dialogs.py）
- **`src/ui/` 里不用原生 QMessageBox / QInputDialog**，一律走 `dialogs`（守它的是 `scripts/test_themed_dialogs.py::test_no_native_message_or_input_boxes_in_ui`，按 tokenize 只看代码里的名字）。QFileDialog 文件选择框和托盘气泡保留原生。设计稿是画布「灵犀弹窗设计」：无边框、背后一层变暗的遮罩、中间一张宽 440 圆角 14 的卡片；颜色全取 `theme` 的 `dlg_*` token（打开那一刻按当前主题取），明暗两套 key 必须一一对应
- **实现：窗内覆盖层**（`_OverlayMixin`，ThemedDialog / CardDialog 共用）：有可见的父窗口时，弹窗是**父窗口里的子控件**（`Qt.Widget`），盖满客户区画遮罩，卡片在中间偏上（上下留白 4:6）。最早做成盖在父窗口上的无边框顶层窗，结果是应用级模态——Windows 下主窗口标题栏的最小化 / 最大化 / 关闭都点不动（用户实测），窗口放大后遮罩也不跟着变。现在：标题栏按钮照常可点；父窗口装事件过滤器，缩放时遮罩和卡片跟着重排；遮罩吞掉鼠标；Tab 焦点只在弹窗里转（`focusNextPrevChild`）；`ShortcutOverride` 在弹窗接住，主窗口的快捷键不会越过弹窗触发；没处理的按键不往主窗口冒；关掉后焦点还给打开前的控件。仍是 `exec()`（QDialog 自己的局部事件循环），调用处保持同步写法，只能在主线程调。父窗口不可见（最小化 / 进托盘）或没给 parent 时退回无边框顶层窗：只开卡片大小、屏幕居中、不画遮罩
- **API 对齐 QMessageBox**：`question / information / warning / critical / success` 返回 `StandardButton`（数值与 QMessageBox 一致），`labels={dialogs.Yes: "删除"}` 改按钮字；`choose` 多出口返回动作 key（Esc / 关闭返回 None）；`get_text` 同 `QInputDialog.getText` 返回 `(文本, 是否确定)`；`confirm_danger` 返回 bool。图标底色表示类型：info 靛蓝 / success 绿 / warning 琥珀 / error 红，`glyph` 另选图形（rewind / trash / folder …）。路径和报错放 `detail`（灰色等宽框，带「复制」，长路径无空格也能折行：用 QTextEdit 不用 QLabel）
- **键盘**：Esc = 取消（关闭按钮、Alt+F4 同）；Enter = 默认按钮——普通对话框默认主按钮，调用方给了 `default` 按它（回退对话、移除项目沿用原来的默认「取消」），焦点在别的按钮上时按那个；**危险对话框（`danger=True`）默认聚焦取消，Enter 永远等于取消**，危险按钮只能点或 Tab 过去按空格（原来「跳过权限确认」的 QMessageBox 按 Enter 就直接切了）。按钮自绘（`DialogButton`）：主窗口的全局 QSS 会继承下来，焦点环又要画在按钮外面，QSS 盒模型做不到；autoDefault 关掉，Enter 统一由对话框处理
- **危险样式的调用点**：跳过权限确认（琥珀图标 + 红按钮「仍要切换」）、删除自定义模型。危险按钮实心 #c43d27（深色主题 #f09a8d 配深色字），对比度都过 4.5:1（测试里算）
- **表单对话框用 `CardDialog`**（设计稿「设置对话框 · 应用内外框」）：同样是无边框透明窗 + 遮罩，卡片圆角 16，顶部 56 高的标题栏（标题 + 关闭），中间 `self.body` 由子类排，底部 64 高的操作栏（左边一行说明 `set_hint`，右边 `add_button(文字, 角色, 槽, default=)`）。卡片按请求的宽高放中间，窗口小了就缩到窗口里留 32 边；Esc / 关闭 = reject，Enter 走 QDialog 的默认按钮。**子类的内容样式设到 `self.body` 上**，设到对话框本身会把遮罩和卡片的样式盖掉。用它的：设置（800×620，原来的 QTabWidget 只藏起标签栏，左侧 168 宽的导航按钮切页；roles / logs / config / 关于收在导航底部；重启提示从顶部横幅挪到操作栏说明）、自定义模型编辑、编辑固定限制（字数 / 拒绝保存的说明在编辑框下面，放操作栏会被挤成几行裁掉）、只读长文本 `TextViewerDialog`（验证输出，带「复制全部」）。退出确认 `QuitConfirmDialog` 是危险样式的 ThemedDialog（托盘里退出时主窗口隐藏，只开卡片、屏幕居中）；编辑消息 / 编辑排队消息改用 `get_text(multiline=True, banner=…, rows=6)`（设计稿是 4 行，消息编辑给到 6 行），Ctrl+Enter 确定
- **`src/ui/` 里除 dialogs.py 外没有直接用 QDialog 的了**：后台任务和调试（原 F12 独立置顶窗口）在第 3 批改成了右侧停靠栏的「后台」「调试」标签（`file_panel.open_widget_tab`，见 [background.md](background.md)）；`test_no_native_framed_dialogs_in_ui` 用 AST 守着不再出现 `class X(QDialog)` / `QDialog(...)` / `QDialogButtonBox(...)`
- **测试写法**：exec 是阻塞的，先挂带 receiver 的零秒定时器，等 `dialogs.active_dialog()` 出现再按键 / 点按钮（`test_themed_dialogs.drive`），另挂 5 秒安全阀防卡死；只想跳过弹窗的用例 patch `dialogs.ThemedDialog.exec` 或 `dialogs.get_text`。offscreen 下 `QTest.keyClicks` 打中文字符会卡住，输入框用 `setText`

## 侧栏浏览器（src/browser/ + tools_browser.py + ui/browser_panel.py）
- **面板长相**（`ui/browser_panel.py`，第 4 批照设计稿重做）：标签显示页面图标 + 标题，模型操作中带小圆点，出错显示「无法访问」（`file_panel._BrowserTabLabel`，`tab_state_changed` 驱动）。工具栏：后退 / 前进 / 刷新（加载中变停止）、地址胶囊（左边的来源标签：本机 · 已信任 / 本机 / 已信任 / 外部网站，按正在跑的驱动会话、否则前台会话的 `browser_origin_allowlist` 判定；不在编辑时从头显示网址）、F12、更多（系统浏览器打开 / 复制网址 / 关闭浏览器）；下面 2px 加载条。内容区三态：**空白**——列出后台任务里正在跑、`dev_server_url` 认得出的本机服务（点「打开」= 人主动打开灵犀自己起的服务，来源加进任务所属会话的白名单，和后台面板同一规则）+ 本次运行「最近打开」（进程内、不落盘）；**浏览中**——模型操作时页面描 2px 靛蓝边、底部浮状态卡「灵犀正在点击」+ 第几步 +「停止」（桥 `activity` 驱动，两次动作之间 900ms 内不收，免得一闪一闪）；「停止」停的是**驱动浏览器的那个会话**（`bridge.driver_session()`，`call()` 时记下的 worker 会话）：是前台会话走停止按钮那一套，后台会话只置它的 `stop_flag`；**连不上**——`loadingChanged` 报 LoadFailed（排除 ERR_ABORTED）时换成我们自己的错误页（重新加载 / 本机地址才给「去后台看看」/ 错误码），不用 Chromium 的英文页，模型读到的仍是页面本身。开发者工具停靠在页面下方（可拖分隔条、可弹成单独窗口；同一页面只能挂一份，弹出时停靠的那份收起），关标签时先拆开发者工具再释放页面。停靠栏顶部改成标签行在最上（右端「浏览器」入口和收起 ×），文件路径 / 说明只在文件标签下出现
- **模型和人共用一个页面**：浏览器在文件面板里开一个「浏览器」标签（`BrowserView`），嵌的就是 `page.get_browser().view`，模型的导航 / 点击在面板里实时可见。**懒加载是硬约束**：`browser/`、`tools_browser.py`、`browser_panel.py` 顶层都不 import QtWebEngine*，profile / page / view 等到第一次 `browser_open` 或手动点开标签才在主线程构造，不用浏览器就不存在
- **profile 常驻、off-the-record、NoCache**：`page._get_profile()` 不传 storageName（只在内存、不落盘）、不设 parent（模块持有；offscreen 下进程退出前销毁用过的 profile 会触发上游 Qt 段错误），关浏览器只销毁 view/page。关闭时清 cookie、HTTP 缓存、visited links；localStorage 没有清除 API，留到进程退出。安全默认值：下载、新窗口、alert/confirm/prompt、摄像头/麦克风/定位/通知一律拒，`acceptNavigationRequest` 只放 http(s) 和 about:blank
- **每次导航前清渲染进程的内存缓存**：NoCache 管不到 Blink 的内存缓存，它按 Last-Modified 认为旧脚本"还新鲜"——改完 app.js 重开同一地址、加 `?v=2`、Reload 都还跑旧代码（评测 012）。`bridge._clear_cache_then_load` 先 `clearHttpCache()`，等 `clearHttpCacheCompleted` 再 `page.load`（2 秒兜底）。**导航在途时不清**（`_page_loading`）：这时发同步 IPC 会堵死导航；新页面 / about:blank 也直接 load。关浏览器后的清缓存同理，轮询到桥空闲再清
- **tree.js 注入隔离世界**：`QWebEngineScript` 在 DocumentCreation、ApplicationWorld、含子 frame 注入，页面脚本看不到也改不了 `window.__lx`，闸门用的 info/state 才可信；`runJavaScript` 必须用同一个 worldId。隔离世界拦不到页面的 `console.*`，页面控制台走 `ConsolePage.javaScriptConsoleMessage` 这条 Python 通道
- **worker ↔ 主线程的桥**（`bridge.py`）：工具跑在 worker 线程，`call()` 经 Signal 投到主线程（`BrowserBridge` 建好后 `moveToThread(app.thread())`），自己在 `threading.Event` 上每 0.3s 分片等待、看 `stop_flag`，点停止立刻发 cancel 返回。**不能在主线程调 `call()`**：请求排在主线程事件队列里，主线程却在 `ev.wait` 上等它，死锁。`open` 也不嵌套 QEventLoop——嵌套期间主线程会处理关窗等事件，窗口关掉后访问已删除控件会崩；改成 loadFinished 回调写结果 + 带 receiver 的 QTimer 管超时
- **ref 跨文档单调递增、执行时核对 docId**：tree.js 每个文档生成一个 `docId`，ref（`e12`）在同一文档里稳定复用；桥用 `_ref_seed` 记已发过的最大编号、下一个文档接着往上数，换页后旧编号不会撞上新元素。`tools_browser._issued` 登记每个 ref 签发时的 docId，`click/type` 执行前先比对当前 docId，换了文档或配错 frame 一律报"ref 已失效"
- **有后果的操作走权限闸门**：`security.decide_action` 按权限模式 × 动作分级 × 来源（本机 / 已授权来源 / 其它）给出 allow / ask / ask_always / block，要问就调 `ui.confirm_command(kind=KIND_BROWSER)`，没界面 fail-closed。`click/type` 先取元素 info，`is_consequential` 判定提交按钮 / POST 表单 / 回车提交，有后果每次都问、不给记住。浏览器**不套 fetch_url 的内网拦截**（localhost 正是主用途），内网风险靠只读导航 + 动作确认控
- **页面内容放在 untrusted 围栏里**：`tree.fence` 把页面文字包进 `<browser-content url="…" untrusted>`，正文里伪造的 `</browser-content>` 会被转义，不能把后续文字送出围栏。动作默认附 all 模式前后的"变化行 diff"（`tree.compute_delta`），空 diff 等异步渲染再补一次，view 隐藏时 settle 更久（定时器被节流）
- **控制台按加载序号 nav 分段**：`ConsolePage` 每次 loadStarted 把 `nav_seq` 加一，控制台条目都带 `nav`；`browser_console` 默认只给最近一次加载的，改好代码重开后不会再看到上一次的旧报错。浏览器检查能顶替测试结论的条件见 [undo-and-verification.md](undo-and-verification.md)

## 持久化文件
| 文件 | 内容 |
|------|------|
| `chat_memory/index.json` | 会话列表（id + title + 时间 + project tag） |
| `chat_memory/long_term_memory.json` | 跨会话长期记忆（remember/forget 存取，自动注入 system prompt） |
| `chat_memory/rag_index/chroma/` | RAG 向量库（Chroma PersistentClient 数据目录；collection 名 `kb_<store>_v1`） |
| `chat_memory/<session_id>.json` | 会话消息历史（HumanMessage/AIMessage/ToolMessage 序列化；内部消息带 `lingxi_internal` / `lingxi_kind`，恢复说明另带 `lingxi_recovery` 交接记录）+ project / session_kind / rag_kb_dir / agent_mode + `progress`（计划 / 台账 / revision / `last_run`（含 `evidence.validation_runs` 结构化检查记录、`model` 稳定标识、`workspace` 现场锚点）/ `pending_verification`（含 `file_paths`：改动文件的实际位置）/ `last_committed_operation` / `recent_operations`） |
| `chat_memory/<session_id>.inflight.unreadable-<哈希>.json` | 执行前记录里无法解析的条目的**完整原文**（交接前隔离留底；恢复说明里只有摘要和对它的引用）。随 `delete_session` 删除 |
| `chat_memory/<session_id>.inflight.json` | 有副作用工具的**执行前**记录（sidecar）：session_id / run_id / operation_id / 工具名 / tool_call_id / 起始 revision / 路径 / `work_root`（工具实际的工作目录）。提交后删除；解析不了的条目原样保留、交接到恢复说明后才摘除；**不进侧栏索引**，也不能作为复活已删除会话的依据（`delete_session` 会一并删掉） |
| `chat_memory/file_history/<id>.json` + `.bin` | 文件撤销的恢复材料（B11a）：归属记录（session/run/task/operation_id + 工具 + 工作区 + 目标路径 + 写前/写后指纹 + 阶段）与写前原始字节（**移动记录无 `.bin`，只记指纹**）。随 `delete_session` 删除（回退到空 `keep_file_history=True` 保留）；不进侧栏索引 |
| `chat_memory/projects.json` | 注册的项目列表 + 当前激活项目（`{current, projects: [{path, name}]}`） |
| `chat_memory/role_config.json` | 当前激活的角色卡名 |
| `chat_memory/ui_prefs.json` | UI 偏好（如关闭按钮记住的选择） |
| `chat_memory/theme_config.json` | 主题选择（light / dark） |
| `logs/YYYYMMDD.log` | 按日期分的日志 |

这些位置全部由 `src/paths.py` 的函数在**调用时**按当前数据根算出（`memory_dir()` / `memory_index()` /
`theme_config_path()` / `rag_index_dir()` / `permission_rules_file()` …），不要在别的模块导入期拼成常量
——常量会把导入那一刻的数据根固化，`set_data_dir` 的线程本地上下文和测试隔离都改不动它。
测试里整个数据根默认被 `LINGXI_DATA_DIR` 搬到临时目录（`scripts/conftest.py`，守它的是
`scripts/test_test_isolation.py`）。
