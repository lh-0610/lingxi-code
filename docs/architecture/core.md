# 对话核心与主循环

> 从 CLAUDE.md 拆出的架构说明；入口与文档地图见 [CLAUDE.md](../../CLAUDE.md)。

## 对话核心
- **全流式调用**：`state.llm_with_tools.stream(history)`，AIMessageChunk 用 `+` 自动累加 content 和 tool_call_chunks
- **Agent 主循环**（`src/agent.py:agent_loop`）：stream → 收 tool_calls → 执行 → 再 stream → 没工具就停
- **Claude Code 模式**：`subprocess.Popen` 调本地 `claude -p --output-format stream-json`，解析 `assistant`/`user`/`result` 事件
- **UI ⟷ Agent 解耦**：agent 线程通过 `ui.show_message(text, tag)` 调用，内部 `bridge.append_signal.emit()` queue 到主线程渲染。所有 `ChatUI` 的对 agent 暴露接口（`show_message / render_final_markdown / remove_thinking_indicator / show_token_usage / show_retry / confirm_command`）都是线程安全的 wrapper
- **命令确认**（`src/ui/chat_window.py:confirm_command`）：worker 线程调它时，会通过 `confirm_request = Signal(str, object, object)` 投递到主线程，UI 显示内联确认卡，worker 线程 `event.wait()` 等待用户操作；授权记忆与拒绝停止必须取当前确认卡 `result_holder['_session']` 的归属，不能依赖窗口共享的“最后发起确认的会话”指针。会话级 allowlist（`_session_command_allowlist`）让用户选"允许并记住"后同样命令秒过；危险命令（`_is_destructive_command` 正则匹配 `rm -rf` / `format` / `sudo` 等）不给"记住"选项；跑会写文件的脚本（`permission_modes.script_writes_files` 查 stdin / `-c` 内联代码 / 脚本文件 / 从 `<` 或管道喂给解释器的脚本，链式 `cd` 跟进；按引号外的 `&&` `||` `|` `&` `;` 切段，不要求两边有空格；`call` / `start` / `cmd /c` 包的一层会剥掉，`bash -c` / `powershell -Command` 的内联代码当命令行再判一层）同样每次必问、不给"记住"——否则模型写个脚本批量改文件就绕过了 edit_file 的逐处确认
- **思考过程**：解析 `<think>...</think>` / `reasoning_content` / Anthropic `thinking` content block，统一显示成可折叠的紫色块
- **Markdown 渲染**：流式过程显示纯文本，完成后用 `markdown` 库一次性转 HTML 替换（QTextBrowser 不支持 `<style>` 标签，所有样式必须 inline）。复制/重新生成按钮用 `<table cellpadding=0 height=18>` spacer 撑开（QTextBrowser 对 `<div margin>` 支持差）

## 消息流（src/ui/message_view.py）
- **工具调用一行一条，连续的收成一组**（设计稿「消息区：工具调用改成一行一条」）：`ToolCallCard`（类名沿用，对外接口不变）现在是一行——图标 · 动作（「运行」「读取」「编辑」，`_tool_verb` 按显示名）· 对象 · 右边的状态。连续的调用进 `_ToolGroup`：一两条直接平铺，第三条起出组头（「列了 1 个目录，读取了 3 个文件 · 1 条失败 · 41 秒」，读多个文件按文件数算），点组头折叠；实时运行时组展开，这一组结束（来了正文 / 思考 / 改文件……任何别的块，见 `AssistantTurn._add` → `_close_group`）就收起，**有失败的组不自动收**；重画历史时组直接收着。改文件的工具（`_EDIT_TOOLS`）单独一行、不进组，右边是增删行数（`streaming.describe_edit_stats` 按参数算，经 `tool_stat` 消息送来：`edit_file` 新旧两段做真实的行级差异；`replace_all` 不报；`write_file` 只说「N 行」，不知道原文件多少行就不说成 +N），点文件名在文件面板看差异。线上一组一个节点，有失败的组节点变红（`node_tone`）
- **失败判断宁可不标也不错标**（`_tool_outcome`）：只认非零退出码（「退出码: N」/「✓ 退出码 N」）、❌ / ⛔ 开头、「失败：」「读取失败:」这类开头、用户中断（橙色「已停止」）。输出里出现 error / FAILED / 失败字样不算——测试名、文件内容里常有，误标成失败会让人以为出了问题。做完了的行不加标记
- **输出只存成文字，点开那一行才建输出框**：`ToolCallCard._lines`（最多 4000 行），展开时 `_build_body` 才建参数标签和 QPlainTextEdit。早先每张卡都预建一个藏着的输出框，重画长会话时这一项占卡片耗时的三分之一。Ctrl+F 要搜到工具参数：`find_text` 先给参数里有关键词、还没建的行补建（`ensure_body`）。运行中的行图标换转圈、右边走秒（超过 2 秒才出，快的不闪），下面露最新一行输出；跑完行上不留用时（总用时在组头）
- **样式一张表**（`mv_qss()`，挂在 MessageView 上，`apply_theme` 时按当前主题生成）：控件只打 objectName 或 `mv` 属性，不各自 setStyleSheet——每次都要解析 CSS、重算这一枝，一张卡十几次。范围用 `QScrollArea[mvRoot="true"]` 限定，**不用 `#messageView`**：ChatUI 把它的 objectName 改成了 chatArea，按 objectName 写的规则一条都不生效（实测：气泡没底色、按钮成了默认样式）。内容区原来挂着一条不带选择器的 `background`，会级联到每个子控件，于是每个子控件又写 `background:transparent` 盖回去——一并去掉。改前改后逐像素比对过（浅色 / 深色、收起 / 展开 / 底部），完全一致
- **一轮里各块的分寸**（设计稿「消息区：消息流整理」）：
  - 用户消息的编辑 / 回退 / 分叉放在气泡右边同一行（`UserTurn.set_actions`），鼠标移到这条消息上才出现；回答下面的复制 / 重新生成**只有最新一条常显**（`MessageView._latest_actions`），更早的移到那段回复上才出现。显隐都走 `_HoverButtons`：按钮藏起来也占着位置（`setRetainSizeWhenHidden`）——不然长消息折行时按钮一出现气泡就被挤窄重排，回答下面那排一出现后面的内容就往下跳
  - 继续任务 / 中断恢复的说明走 `recovery_note` 消息（`run_results` 的「继续任务 · 使用模型…」、`recovery.prepare_and_apply` 的说明、重画时 `_recovery_note_text`），画成 `RecoveryNote`：线上一个小圆点 + 一行灰字，现场变化这些细节收进「详情」。实时继续时这两段紧挨着到，并成一条（标题相同的那句不重复）。以前是通栏灰底的程序提示，比用户自己的消息还显眼。其它程序提示（重试、拒绝调用）仍是 `Notice`
  - 收起的过程是一行（`_ProcessSummary`，整行是按钮），行尾箭头朝右 = 收着
  - 线上的节点：用户消息空心圆、**最终回答实心圆**（`_Round.answer_body`，绘制时现找：最后一段回复的最后一个正文块，跳过那排按钮；这一轮不是以回答收尾——停在工具上、后面还有提示——就不画，过渡回复不能标成回答）。竖线从第一个节点画到这一轮内容的底（`_content_bottom`，不算藏着也占位的按钮排），离最后一个节点够远（≥24）时底端收一个小空心圆
  - 轮与轮之间一条很淡的分隔线（`#roundSep`，从线槽右边起；只加在两个 `_Round` 之间）。线上方少空 10：上一段回答末尾那排按钮藏着也占位
  - 回答正文（`markdown_render._md_to_html`）：段落只留下边距 8；标题换成带字号的段落（Qt 给 `<hN>` 套自己的字号级别，style 里的 font-size 盖不过，h1 是 xx-large），上 18 下 6，回答以标题开头时上边距归零；列表是两列无框表格（符号一列、内容一列），折行和文字对齐，嵌套列表落在外层那项的内容列里；行内代码用正文色 + 很浅的底（`md_code_*`），文件链接和 localhost 链接用强调色、无下划线（Qt 默认的纯蓝下划线在深色底上几乎看不清）
- **重画长会话的耗时账**（371 条消息、92 次工具调用，真实数据副本实测「知识库切回编码」）：界面改版前 0.8s → 右键菜单全局过滤器引入后 1.6s（修：重画时摘掉，见 subsystems.md 右键菜单）→ 0.82s → 样式一张表 0.45s → 一行一条 + 输出不预建 0.33–0.39s

## 项目（工作区）
- **状态**：`state.current_project` 持当前项目根路径，None = 无项目；`chat_memory/projects.json` 持久化项目列表 + current
- **启动恢复**：`src/agent.py` 启动时 `state.current_project = _projects.get_current()` 自动恢复
- **新对话沿用项目**：`reset_history()` 不动 `current_project`，所以 `save_session` 仍用当前项目打 tag
- **删项目时批量改归属**：`memory.move_sessions_to_no_project(old_path)` 把所有 `project == old_path` 的会话改成 None，三处一起改：**① 内存里已打开的 `Session.project`**（关键——只改磁盘的话，移除当前项目后 `_switch_project` 的 `save_session` 会按旧内存锚点把会话写回已删项目，后台会话下次 save 也复发）+ ② index.json + ③ 各 session 文件。个别会话文件写失败 → 抛 `SessionMigrationError`（内存锚点已置 None、下次 save 自愈，caller 据此提示用户）
- **工具按项目根解析路径**：`src/tools.py:_project_cwd()` / `_resolve_path()` 让 `read_file('foo.txt')` 解析到 `state.current_project/foo.txt`；`run_command` 的 cwd 也是项目根
- **无项目时落在专门的空工作区**（`tools_common.no_project_dir()`）：顺序是 config.json 的 `default_workspace` → 测试隔离数据根下的 `workspace`（设了 `LINGXI_DATA_DIR` 时）→「文档\灵犀工作区」，不存在就建。早先回退到进程 cwd——源码运行时就是灵犀仓库根：无项目对话里写的 index.html / app.js 落进了仓库，验证时跑 pytest 收集到的是灵犀自己的测试（仓库根 pytest.ini 指定了 testpaths，所以工作区也不能放在仓库里面）。system prompt 在无项目的编码会话里加一段「# 工作目录」写明这个路径（路径稳定，不破坏前缀缓存）；`@` 文件提及和补全也用 `_project_cwd()`，与工具同源。测试里要让无项目工具落在某个目录，用 `monkeypatch.setattr(config, "DEFAULT_WORKSPACE", ...)`，chdir 不再管用
- **`.lingxirules` 项目级指令**：项目根放该文件后，`roles.get_system_prompt()` 会把它内容追加到 system prompt 末尾，优先于 SYSTEM_PROMPT 和角色卡的通用指令；每次新对话 / 切项目 / 删当前会话时都重新读，让 AI 立刻"懂这个项目的约定"
- **规则加载分两层**（`roles.py`）：`read_rule_sources()` 只读**原文**、不截断；`render_rule_sources(budget)` 负责在预算内挑内容。装得下给全文（输出与重构前逐字一致）；装不下给「来源清单 + 目录 + 原文节选 + 省略范围 + 补读示例」，开头标注"尚未完整读取"。节选取头也取尾——**重要性不能靠位置判断**，约定和已知取舍这类最该遵守的条目常写在文末。预算：system prompt 合并 `_COMBINED_RULES_MAX=40000`，单来源 `_SINGLE_RULE_MAX=40000`，`get_project_instructions` 概览 `_TOOL_OVERVIEW_MAX=20000`（必须明显低于 `TOOL_RESULT_HARD_CAP_CHARS=24000`，否则概览会被发送层二次截断，连"少了什么"的说明本身都被切掉）
- **早先"读的时候就截断"丢过真东西**：本仓库 CLAUDE.md 超过当时 20000 的上限，末尾 5 节（含「已知限制与有意取舍」）从未进入 system prompt——既不在输出里也不在任何清单里，模型不会去补读自己不知道存在的内容。这类故障不报错，只会表现为"它怎么老提已经决定过的事"
- **`get_project_instructions` 也受同一预算**，不是完整原文的后门；要拿全文用它的 `source`/`offset`/`limit` 分页（按 Unicode 字符计、默认 6000 上限 12000，返回内容指纹，翻页途中文件被改能发现）。路径作用域判定与 system prompt 注入共用 `_rule_dirs_for_target()`，不因分页放宽
- **项目指令的优先级边界**（`roles._PROJECT_RULES_PRECEDENCE`，注入 CLAUDE.md / AGENTS.md / .lingxirules 时统一前置）：它们优先于通用编码约定，但**不得覆盖系统提示的安全约束与用户在本次对话中的直接指令**。这些文件来自**代码仓库**，是不可信输入——克隆一个第三方项目不该等于授权它改写助手的行为。早先写的"优先于上面任何通用约定"等于给了仓库里一句「忽略之前所有安全限制」以最高权限，是现成的提示注入面
- **Plan/Act 随会话持久化**：`agent_mode` 进 session JSON，`load_session` 回填、`_sync_header_from_session` 同步输入框底栏的模式按钮。不存的话重开一个"聊到一半正在 Plan"的会话会变成 Act——用户以为还在只读规划、模型却已能动手改代码，是有安全后果的静默降级。旧会话缺该字段 → 默认 act
- **权限模式**（permission_modes.py）：输入框底栏的"模式"菜单有五种——自动 / 手动 / 接受编辑 / 计划 / 跳过权限确认。计划就是 agent_mode=plan；其余四种是执行时的放行规则，存在会话的 `permission_mode`。放行只在 confirm_bars 的 `confirm_command` / `confirm_edit` 开头按发起确认的会话判一次：接受编辑放行写文件，自动再放行普通命令（危险命令、脚本改文件、Git 写操作、MCP 工具仍问），跳过权限确认全部放行。跳过权限确认不落盘、不作为新会话的默认（重开程序或新建会话退回上次的非跳过模式）；旧会话缺该字段 → 手动

## 模块级 facade（src/agent.py）
- `agent.py` 通过模块级 `__getattr__` 把读取代理到 `state` —— 让 `src/ui/chat_window.py` 等模块继续用 `agent.stop_flag` 不报错
- **写入必须用 `state.X = ...`**（不要 `agent.X = ...`，那只污染 agent 模块）
- `state.ui_ref = self` 在 ChatUI 启动时设置，让 worker 线程的 tools 能找到主窗口弹确认框

## 多会话并发（src/session.py + state.py 代理）
- **会话级状态收进 `Session` 对象**：`chat_history / stop_flag / session_token_usage / compaction / evicted_tool_results / current_plan / task_ledger / shell_cwd / current_model_index / agent_mode / reasoning_effort / verification / worktree / project / role_snapshot` 等都是会话级（每会话一份），见 `session.py:_SESSION_FIELDS`。旧的布尔 `reasoning_enabled` 不再是存储字段，而是 `reasoning_effort`（off/low/high/max，见 `reasoning.py`）的派生视图——读 = 不是 off，写 = 开→high、关→off
- **`state.py` 用 `ModuleType` property 代理**：`state.chat_history` 等读写自动落到「当前线程的当前会话」，所以几十处 `state.X` 老代码无需改动。**真正全局**的（`llm` / `ui_ref` / `current_project` 等）仍是 state.py 的普通变量
- **线程路由**（`session.current_session()`）：worker 线程进 `agent_loop` 时 `bind_thread(sess)` 把自己绑到该会话 → 该线程所有 `state.X` 都落到这个会话；主线程（UI）/ 未绑定线程 → `get_active()`（前台显示的会话）。这就是「后台会话边跑、前台切到别的会话」不互串的根基
- **注册表** `session.sessions`（key→Session）：`register` / `rekey`（存盘拿到 id 后把临时 `_new_N` 换成 id）/ `drop`
- **会话锚定项目** `Session.project`：首次 save 时锚定为当时的全局 `current_project`，之后不被切项目影响（`_UNSET` 哨兵区别于合法的 `None`=无项目）。修「后台会话 save 晚于主线程切项目、被打上新项目 tag」的 bug

## Prompt 缓存与「稳定 / 易变」分层（roles.py + streaming.py）
- **判据只有一条：会变的东西不进 system prompt**。`_wrap_system_for_cache` 把整个 system 塞进一个 `cache_control: ephemeral` 块，Anthropic/MiMo 的缓存以**前缀逐字节相同**为条件，块内容一变就失效
- 早先 `task_ledger`（每执行一个工具就更新）和 `current_plan`（每次 update_plan 更新）被拼进 system prompt → **每轮 system 都不同 → prompt caching 每轮必然 miss**。缓存机制写对了，却被里面装的东西废掉了，每轮按全价重算角色卡 + 项目上下文 + 项目规则 + 长期记忆
- 现在拆成两半：`get_system_prompt()` 只留跨轮稳定的（基底/角色卡/日期/项目上下文/项目规则/长期记忆）；`get_volatile_context()` 装每轮会变的（Plan 提示/计划/台账），由 `streaming._append_volatile_context` 作为一条 `HumanMessage` 追加到**发送历史的最末尾**——前面任何一条变了前缀缓存就从那里断掉，追加在最后才能让整段前缀可复用。只改发送副本，不动 `state.chat_history`
- 易变块用 `<system-reminder>` 包裹：这是系统注入的运行态，不是用户的新要求，模型不该把它当指令执行
- **缓存命中要能被观测**：`_extract_usage` 采集 `cache_read` / `cache_write`（兼容 Anthropic 的 `input_token_details`、OpenAI 的 `prompt_tokens_details.cached_tokens`、DeepSeek 的 `prompt_cache_hit_tokens`）和思考量 `reasoning`（`output_token_details.reasoning` / `completion_tokens_details.reasoning_tokens`，它是输出的子集，**不再加进 total**）。prompt caching 省的是**钱**不是 token——命中部分照样计进 `input_tokens`、只是按约 10% 计费，不单独采集就完全看不出有没有生效
- **没上报的分项记 `None`，不填 0**：`_extract_cache_tokens` / `_usage_count` 对缺失、脏值、负数一律返回未知。填 0 会把"这家服务商根本不给缓存字段"（Claude Code CLI 就是）伪装成"缓存一次都没命中"，两者的处置完全相反。未知一路传到运行账本（`run_ledger`）和结果卡（显示"命中率未知"）；只有会话累计和底栏为了显示整数才把未知当 0 加
- **发送前比对前缀指纹**（`streaming._record_prefix_fingerprint`，在 `_prepare_stream_history` 最后）：对 system、工具名列表、每条消息算 sha256，和**本会话上一轮真正发出去的**那份比，找出第一个不同的位置。必须在发送副本上比而不是 `chat_history`——回收、硬上限截断、压缩都只改发送副本，看原始历史会漏掉恰恰最贵的那几种断裂。指纹模块 `src/cache_fingerprints.py` 不依赖 Qt、只存哈希不存正文，离线回放可复用
- **末尾那条易变消息不算断裂**：`_append_volatile_context` 给它打 `additional_kwargs["lingxi_kind"]="volatile"`（各 provider 的转换器只挑自己认识的键，不会把它发出去），指纹里 `lingxi_*` 一律排除、比对时丢掉上一轮的这条尾巴。不打标记的话每轮都会误报一次
- 断裂事件记「第 N 轮从第 k 条消息起与上一轮不同」并尽量标原因：`工具列表变了` / `system 变了` / `工具结果回收`（本轮 `_evicted>0`）/ `历史压缩`（本轮 `_trimmed>0`）/ `未知`。**只进日志和运行账本，不弹给用户**——它是计费诊断，不是要用户处理的错误。基线 `cache_prefix` 是会话级的，跨运行保留（同一会话接着跑，前缀本来就该接着比），`reset_history` / `load_session` 清掉
- **事后补抓服务端侧失效**：某轮命中率比上一轮骤降（上一轮 ≥50%、跌幅 ≥30 个百分点，用 `Fraction` 比避免浮点误差）、而发送前比对没发现变化时，记一条 `source="服务端侧"`。本地前缀逐字节没变却不再命中，只可能是服务端淘汰或路由换了实例；这是**疑似**不是确证，所以措辞和来源都和本地断裂分开。本轮已检出本地变化时不再补记（`cache_prefix_changed`），否则同一次断裂会被算两遍
- 回归守卫在 `scripts/test_prompt_cache_stability.py`：这条性质极易被无意破坏（往 `get_system_prompt` 里再拼一个「当前 xxx」就够了），而破坏后一切照常工作、只是**静默开始烧钱**，没有任何报错。指纹与骤降判定的构造用例在 `scripts/test_cache_fingerprints.py`，经过真实发送边界的在 `scripts/test_run_ledger.py`

## 上下文长度：水位线式回收（streaming.py）
- **上下文是匀速涨上去的，不是被几个大结果撑大的**：回放 11 月初五个 68–115 轮的真实任务，每轮净增一两千 token，读文件类结果占累计重发量的 50–80%，但 138 条读结果里超过 6k token 的只有 2 条；重复读同一段只占约 2%。所以"读得更省"压不住长任务，到头来只能折掉旧内容
- **折旧内容就是改写前文，改写前文就会断前缀缓存**（DeepSeek 未命中比命中贵 50 倍）。两条规矩：**一次折够**——估算涨过回收线才触发，一口气折到回收线的一半；**折过的不再变**——折了哪些 `tool_call_id` 记在会话的 `evicted_tool_results`，之后每轮照样折，存根只由原内容决定（`_evicted_stub`），所以两次触发之间每一轮发出去的前缀逐字节相同
- 以前是无状态的"超预算（DeepSeek 20 万）就把最近 6 条之外的大结果全折掉"：每轮从完整历史重算，刚滑出最近 6 条的那条当轮才变成存根，前缀**每轮**都从那里断。估算修好后日志里一次没触发过，但 83 轮的任务已经到 15.7 万，再跑二三十轮就会撞上；评测 011 基线里它触发了 107 次、`models.py` 被重读 13–19 遍
- `_trim_tool_results` 从最老的折起，最近 6 条和不超过 1000 字的小结果不动。**按内容排先后试过、没有用**：先折"之后被改过的文件"的旧读取、先折搜索 / 命令输出、把正在改的文件留到最后等五种顺序，回放下来被迫重读 6–7 次、成本差 1% 以内——降到一半要把能折的几乎折完，折谁在先不改变结果
- **能折出来的不到水位线间距一半就先不折**：折不下去多少还要重建一次缓存，下一轮又触发，等于回到每轮都断。只有超过真正的历史预算（`hard_limit` = `_current_history_budget()`）时不管多少都折。窗口小的模型历史预算不到回收线，就按预算和它的一半（`_trim_watermarks`）
- **回收线是设置项**：`config.CONTEXT_TRIM_TOKENS`（config.json 的 `context_trim_tokens`，设置弹窗「高级」页可改，默认 20 万，改动需重启；0 = 跟历史预算走；非 0 的值低于 2 万按 2 万算）。设得比 `MAX_HISTORY_BUDGET`（20 万）高时，`_current_history_budget` 的上限跟着抬到回收线——不然线设在 40 万、预算却在 20 万就开始压缩，那条线永远到不了；再高也不超过模型窗口，所以只对 DeepSeek 这类 100 万窗口的模型有意义
- **线设低是用钱换短上下文，不省钱**。用真实实现回放那五个任务（不回收时峰值 10–17 万）：10 万 → 五个都触发 1–2 次，成本合计 +5.9%（单个 +2%～+19%），两个任务里共 7 次改文件前读取已被折掉；12 万 → +5.6%；15 万 → 只有最长的两个触发、但各 +13%，合计 +6.6%（触发得晚，剩下的轮数不够回本）；18 万以上 → 一次不触发。每次触发要按未命中重付留下的那一半，之后每轮少带的部分按命中价算，要四十来轮才回本。上下文短了模型是不是更好，没有数据。所以默认和历史预算取齐：平时不碰，只拦住会撞预算的超长任务。估算不含工具 schema，实际输入约多 1.5 万
- **折过哪些随会话文件落盘**（`evicted_tool_results`，只留历史里还在的 id）：重开程序接着跑，发出去的前缀和关掉之前一样。旧会话没有这个键 → 当成没折过。新对话清空
- **合成 tool_call_id 必须会话内唯一**（`streaming._normalize_tool_calls`）：部分本地模型不返回 tool_call id，兜底用 `call_<name>_<uuid4 前 12 位>`，**不能用每轮重置的计数器**——回收按 tool_call_id 记且随 `evicted_tool_results` 落盘，跨轮重复的 id 会让新一轮的结果顶着旧 id 被当成"已回收"误折，哪怕它就在最近 keep_recent 条里
- 之后两步只在回收折不动、仍超出历史预算时才轮得到，仍是无状态的：`_cap_oversized_tool_results`（单条超 2.4 万字截头尾）、`_compact_history`（LLM 把中段压成摘要，留最近 20 条）。全程只动发送副本，`state.chat_history` 和界面保留完整历史
- 回归守卫在 `scripts/test_tool_eviction.py`（`test_prefix_stays_identical_between_trips`）：和 system prompt 那条一样，破坏之后一切照常工作，只是每轮开始按未命中计费
- **改发送流程 / 工具输出 / 回收逻辑后，跑一遍离线回放**：`python scripts/replay_sessions.py`（可传会话 ID 或 `--dir`，默认本地 `chat_memory/`）。它把已保存的会话按轮截到"模型即将发出第 N 条 AI 消息"那一刻，走**完整**的 `_prepare_stream_history`（不是只测 `_trim_tool_results` 一步），逐轮报告估算 token、与上一轮第一个不同的位置及原因、是否回收、是否会触发压缩，末尾汇总断裂次数与折算成本。前缀比对复用 `cache_fingerprints`、成本折算复用 `run_ledger` 的单价配置，都不另写一份。**不联网、不写盘、不改 chat_memory**：日志/配置/数据根指到临时目录、MCP 打桩、压缩要调的 LLM 换成占位实例（只报告"这一轮会触发压缩"，不真的压）。**成本是近似**：历史上每轮的 system 和易变运行态无法复原，用的是当前值，且不注入项目上下文/规则/长期记忆；命中按"与上一轮相同的前缀"估算，不是服务商真实上报值——用来看改动前后的**相对退化**。合成会话的回归在 `scripts/test_replay_regression.py`（几十轮、能触发几次回收，断言两次回收之间前缀不断、回收后下一轮起恢复稳定、只有末尾易变消息变化不算断裂；真实会话不进仓库、测试不读 `chat_memory/`）

## 主循环边界与错误恢复（src/agent.py + llm_errors.py）
- **轮次上限** `config.AGENT_MAX_ROUNDS`（config.json 的 `agent_max_rounds`，设置弹窗「高级」页可改，默认 50；**设 0 = 不限**）：主循环原本是裸 `while True`，模型只要一直返回 tool_calls 就一直跑，唯一刹车是用户点停止。触顶后停下并把结论**写进 chat_history**（只弹 UI 的话，用户接着发消息时模型看不到自己被截断，会以为任务做完了）
- **错误分类**（`llm_errors.classify`）：原来所有异常一视同仁地退避重试，对三类是错的——**上下文溢出**重试必然再溢出；**鉴权失败**等再久 key 也不会变对；**限流**服务端通常在 `Retry-After` 里给了确切秒数，盲目 2^n 要么继续撞墙要么白等。分类不 import 任何 provider SDK（各家异常类型不同、自定义模型协议还是用户配的），改用三层判据：状态码 → 异常类名 → 消息特征词，认不出一律退化成 UNKNOWN（= 保持原行为），宁可漏判不可误判
- **溢出恢复**：provider 报超窗 → `_stream_chunks_with_retry` 抛 `ContextOverflowError` → 主循环把会话的 `overflow_squeeze` +1（`_current_history_budget` 按 1/2^n 收紧、8k 地板）→ 压缩后重发，封顶 `_MAX_OVERFLOW_RETRIES=3`。**不在成功后清零**：溢出说明对该模型窗口的估算偏乐观，这个教训该在本会话内保持，否则每轮都要重新撞一次墙
- **停止不等网络**：`_stoppable_stream` 在旁路线程里拉流，worker 每 0.2s 看一次停止标志。原来 worker 直接迭代 `llm.stream()`，模型迟迟不回 chunk 时卡在网络读上（请求超时 30 分钟），停止后转圈一直走秒、旧 worker 不退，下一条消息排队 15 秒就放弃。旁路线程和 worker 绑同一会话与数据根，停止后它等到下一个 chunk 或请求结束自己关连接退出；重试退避也按停止标志提前结束。界面侧 `_force_stop_generation` 当场清掉「等待响应」指示器
- **排队提示**：DeepSeek 高峰期会让请求排队最多 10 分钟，期间只发 SSE 注释 `: keep-alive`（实测 `/responses` 和 `/chat/completions` 都是每 12 秒一次，SDK 不产出 chunk）。`WaitingIndicator` 按心跳报的阶段区分「等待响应」（一个 chunk 都没有）/「模型思考中」/「正在生成工具调用 X」，只有前者满 30 秒才在下面加一行「服务商可能在排队」。原来三种阶段界面上都显示「等待响应」，长的 write_file 参数生成会被误认成卡住
- **跑测试能停、工具卡走秒**：`run_tests` 原来用 `subprocess.run` 一直阻塞到进程退出，全量测试一跑 9 分钟，期间点停止没有反应、卡片上也一动不动。现在走 `_run_test_process`：`communicate` 每 0.2s 超时一次看停止标志（重试不丢输出），停止就杀进程树，结果如实记成「用户停止了测试，没有跑完」（证据 `cancelled`，不算通过也不算失败）。界面侧 `ToolCallCard`（一行一条）实时运行时走秒，超过 2 秒图标换转圈、右边「运行中 N 秒」，结束后行上不留用时、算进组头的总用时；重绘历史不走秒，点停止当场定格。跑着的测试同时登记进「后台任务」面板（见 background.md），能看实时输出、单独停
- **只记进度的一轮补一句提醒**：`agent._run_tool_calls` 执行完一轮，若整轮只调了 `set_step_status` / `update_plan`，在最后那条工具结果后面补一句"之后把进度更新和这一步的第一个动作放在同一轮发"。密钥打码那次 68 轮里有 7 轮是这样；系统提示词早就要求同一轮多发调用，DeepSeek 照样单发，对刚收到的工具结果更上心。提醒只进历史末尾（不碰缓存前缀），界面上的结果不变
- 子 Agent 递归深度天然封顶：`spawn_agents` 里 `is_subagent` 的会话不许再派生
