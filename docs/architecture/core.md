# 对话核心与主循环

> 从 CLAUDE.md 拆出的架构说明；入口与文档地图见 [CLAUDE.md](../../CLAUDE.md)。

## 对话核心
- **全流式调用**：`state.llm_with_tools.stream(history)`，AIMessageChunk 用 `+` 自动累加 content 和 tool_call_chunks
- **Agent 主循环**（`src/agent.py:agent_loop`）：stream → 收 tool_calls → 执行 → 再 stream → 没工具就停
- **Claude Code 模式**：`subprocess.Popen` 调本地 `claude -p --output-format stream-json`，解析 `assistant`/`user`/`result` 事件
- **UI ⟷ Agent 解耦**：agent 线程通过 `ui.show_message(text, tag)` 调用，内部 `bridge.append_signal.emit()` queue 到主线程渲染。所有 `ChatUI` 的对 agent 暴露接口（`show_message / render_final_markdown / remove_thinking_indicator / show_token_usage / show_retry / confirm_command`）都是线程安全的 wrapper
- **命令确认**（`src/ui/chat_window.py:confirm_command`）：worker 线程调它时，会通过 `confirm_request = Signal(str, object, object)` 投递到主线程，UI 显示内联确认卡，worker 线程 `event.wait()` 等待用户操作；授权记忆与拒绝停止必须取当前确认卡 `result_holder['_session']` 的归属，不能依赖窗口共享的“最后发起确认的会话”指针。会话级 allowlist（`_session_command_allowlist`）让用户选"允许并记住"后同样命令秒过；危险命令（`_is_destructive_command` 正则匹配 `rm -rf` / `format` / `sudo` 等）不给"记住"选项
- **思考过程**：解析 `<think>...</think>` / `reasoning_content` / Anthropic `thinking` content block，统一显示成可折叠的紫色块
- **Markdown 渲染**：流式过程显示纯文本，完成后用 `markdown` 库一次性转 HTML 替换（QTextBrowser 不支持 `<style>` 标签，所有样式必须 inline）。复制/重新生成按钮用 `<table cellpadding=0 height=18>` spacer 撑开（QTextBrowser 对 `<div margin>` 支持差）

## 项目（工作区）
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

## 模块级 facade（src/agent.py）
- `agent.py` 通过模块级 `__getattr__` 把读取代理到 `state` —— 让 `src/ui/chat_window.py` 等模块继续用 `agent.stop_flag` 不报错
- **写入必须用 `state.X = ...`**（不要 `agent.X = ...`，那只污染 agent 模块）
- `state.ui_ref = self` 在 ChatUI 启动时设置，让 worker 线程的 tools 能找到主窗口弹确认框

## 多会话并发（src/session.py + state.py 代理）
- **会话级状态收进 `Session` 对象**：`chat_history / stop_flag / session_token_usage / compaction / current_plan / task_ledger / shell_cwd / current_model_index / agent_mode / reasoning_enabled / verification / worktree / project / role_snapshot` 等都是会话级（每会话一份），见 `session.py:_SESSION_FIELDS`
- **`state.py` 用 `ModuleType` property 代理**：`state.chat_history` 等读写自动落到「当前线程的当前会话」，所以几十处 `state.X` 老代码无需改动。**真正全局**的（`llm` / `ui_ref` / `current_project` 等）仍是 state.py 的普通变量
- **线程路由**（`session.current_session()`）：worker 线程进 `agent_loop` 时 `bind_thread(sess)` 把自己绑到该会话 → 该线程所有 `state.X` 都落到这个会话；主线程（UI）/ 未绑定线程 → `get_active()`（前台显示的会话）。这就是「后台会话边跑、前台切到别的会话」不互串的根基
- **注册表** `session.sessions`（key→Session）：`register` / `rekey`（存盘拿到 id 后把临时 `_new_N` 换成 id）/ `drop`
- **会话锚定项目** `Session.project`：首次 save 时锚定为当时的全局 `current_project`，之后不被切项目影响（`_UNSET` 哨兵区别于合法的 `None`=无项目）。修「后台会话 save 晚于主线程切项目、被打上新项目 tag」的 bug

## Prompt 缓存与「稳定 / 易变」分层（roles.py + streaming.py）
- **判据只有一条：会变的东西不进 system prompt**。`_wrap_system_for_cache` 把整个 system 塞进一个 `cache_control: ephemeral` 块，Anthropic/MiMo 的缓存以**前缀逐字节相同**为条件，块内容一变就失效
- 早先 `task_ledger`（每执行一个工具就更新）和 `current_plan`（每次 update_plan 更新）被拼进 system prompt → **每轮 system 都不同 → prompt caching 每轮必然 miss**。缓存机制写对了，却被里面装的东西废掉了，每轮按全价重算角色卡 + 项目上下文 + 项目规则 + 长期记忆
- 现在拆成两半：`get_system_prompt()` 只留跨轮稳定的（基底/角色卡/日期/项目上下文/项目规则/长期记忆）；`get_volatile_context()` 装每轮会变的（Plan 提示/计划/台账），由 `streaming._append_volatile_context` 作为一条 `HumanMessage` 追加到**发送历史的最末尾**——前面任何一条变了前缀缓存就从那里断掉，追加在最后才能让整段前缀可复用。只改发送副本，不动 `state.chat_history`
- 易变块用 `<system-reminder>` 包裹：这是系统注入的运行态，不是用户的新要求，模型不该把它当指令执行
- **缓存命中要能被观测**：`_extract_usage` 采集 `cache_read` / `cache_write`（兼容 Anthropic 的 `input_token_details`、OpenAI 的 `prompt_tokens_details.cached_tokens`、DeepSeek 的 `prompt_cache_hit_tokens`）。prompt caching 省的是**钱**不是 token——命中部分照样计进 `input_tokens`、只是按约 10% 计费，不单独采集就完全看不出有没有生效
- 回归守卫在 `scripts/test_prompt_cache_stability.py`：这条性质极易被无意破坏（往 `get_system_prompt` 里再拼一个「当前 xxx」就够了），而破坏后一切照常工作、只是**静默开始烧钱**，没有任何报错

## 主循环边界与错误恢复（src/agent.py + llm_errors.py）
- **轮次上限** `config.AGENT_MAX_ROUNDS`（config.json 的 `agent_max_rounds`，设置弹窗「高级」页可改，默认 50；**设 0 = 不限**）：主循环原本是裸 `while True`，模型只要一直返回 tool_calls 就一直跑，唯一刹车是用户点停止。触顶后停下并把结论**写进 chat_history**（只弹 UI 的话，用户接着发消息时模型看不到自己被截断，会以为任务做完了）
- **错误分类**（`llm_errors.classify`）：原来所有异常一视同仁地退避重试，对三类是错的——**上下文溢出**重试必然再溢出；**鉴权失败**等再久 key 也不会变对；**限流**服务端通常在 `Retry-After` 里给了确切秒数，盲目 2^n 要么继续撞墙要么白等。分类不 import 任何 provider SDK（各家异常类型不同、自定义模型协议还是用户配的），改用三层判据：状态码 → 异常类名 → 消息特征词，认不出一律退化成 UNKNOWN（= 保持原行为），宁可漏判不可误判
- **溢出恢复**：provider 报超窗 → `_stream_chunks_with_retry` 抛 `ContextOverflowError` → 主循环把会话的 `overflow_squeeze` +1（`_current_history_budget` 按 1/2^n 收紧、8k 地板）→ 压缩后重发，封顶 `_MAX_OVERFLOW_RETRIES=3`。**不在成功后清零**：溢出说明对该模型窗口的估算偏乐观，这个教训该在本会话内保持，否则每轮都要重新撞一次墙
- 子 Agent 递归深度天然封顶：`spawn_agents` 里 `is_subagent` 的会话不许再派生
