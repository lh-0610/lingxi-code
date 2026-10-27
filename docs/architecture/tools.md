# 工具与改后自检

> 从 CLAUDE.md 拆出的架构说明；入口与文档地图见 [CLAUDE.md](../../CLAUDE.md)。

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
| `read_background_output` / `list_background_commands` / `stop_background_command` | 管理本会话后台命令（read·list 进 `PLAN_MODE_READONLY_TOOLS`、不弹确认）；通过 `background.py` 获取快照/请求停止，停止后保留结果，失败可重试；应用退出时 `stop_all_background` 统一有界清理 |
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
| `check_code` | 静态检查单文件（lint/语法）：Python 用 `ruff check --select F,E9`（没装退化到 `py_compile`），其它语言用 config 的 `check_command`；不弹确认；**Plan 模式拦截**（见下） |
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
- 让助手"改完自己发现错、自己修"（对标 Cline/Codex）。`edit_file`/`write_file`/`append_file` **成功后**调 `_auto_check_suffix(full_path)`：跑静态检查、把问题**追加到工具返回串**，模型在同一条 ToolMessage 里就看到「成功编辑 X」+「自动校验发现问题…」提示→ 下一轮自然去修
- `_run_code_check()` 是核心：Python 优先 `ruff check --select F,E9`（**只选 pyflakes 正确性 + 语法错，避开风格噪声**，否则模型会去追无意义的格式问题）。检测顺序：**① 随包 ruff**（`_bundled_ruff()`：打包后在 `_MEIPASS`/exe 旁，由 lingxi.spec 构建时定位系统 ruff 打入，开箱即用）→ **② 开发期 `sys.executable -m ruff`**（`find_spec` 检测，不看 PATH、用应用自己的 Python）→ **③ PATH 上的 ruff 二进制** → **④ 兜底内置 `compile()` 进程内查语法**（`_py_syntax_check`）
- **打包(frozen)安全**：`sys.executable` 在打包后 = `灵犀Code.exe`（不是 python.exe），所以 `sys.executable -m ruff/py_compile/pytest` 在产物里都跑不了。故 frozen 下不走 `sys.executable -m`：
    - check_code 的 ruff 用随包/系统二进制，语法检查用**内置 `compile()`（进程内、不起子进程）**
    - `run_tests` 用 `_resolve_python()` 选解释器：**项目内 venv（.venv/venv/env）→ 开发期 sys.executable → 系统 PATH 的 python**（frozen 下跳过 sys.executable）。顺带让它在真实项目里用对环境（项目自己的 venv + 依赖）而非应用的 Python
- 其它语言读 config `check_command`（`{file}` 占位，shell 执行）；可用 `auto_check_after_edit` 关掉自动触发
- 开关：config `auto_check_after_edit`（默认 true）；只检**刚改的那个文件**（快），防失控靠现有 agent loop 上限 + 模型没错就停
- `check_code` 工具是手动复查入口（同一套 `_run_code_check`）；编辑后自动触发不需要模型记得调它
- **Plan 模式禁用 `check_code`**（不在 `PLAN_MODE_READONLY_TOOLS` 里）：配了 config 的 `check_command` 之后它就执行那条命令，那是用户配的任意命令，无法保证只读。**不限于非 Python 项目**——`_run_code_check` 先判 `check_command`、再判扩展名，配了之后 Python 文件同样走它。名字像"检查"不代表只读——判据与 B03 的执行前记录同一条：**会不会执行项目代码或用户配置的命令**。拦截点在 `streaming._execute_tool` 的 Plan 闸，在 `invoke` 和执行前记录**之前**，拦住的是子进程有没有起来；光靠提示词劝模型别调不算拦截
- 拒绝时回一条 `tool_call_id` 对得上的 ToolMessage，用 `_PLAN_REJECT_NOTES` 给**针对性**说明（"Plan 模式只进行调研；执行代码检查请切换到 Act 模式"）。通用文案只说"只能用只读工具"，模型看到 `check_code` 被拦会以为是误判，转头用 `run_command` 跑等价的 ruff——所以要点明原因并明说不要换工具绕。程序不自动切模式、不代跑替代命令
- Act 模式不受影响；编辑后的 `_auto_check_suffix` 也不受影响（它挂在写工具后面，而写工具在 Plan 下本来就被拦）
