# 工具与改后自检

> 从 CLAUDE.md 拆出的架构说明；入口与文档地图见 [CLAUDE.md](../../CLAUDE.md)。

## 工具列表

内置工具在 `tools.py` 的 `ALL_TOOLS`；MCP 远程工具运行时注入（`mcp_{server}_{tool}`）。`get_tool_map()` 动态合并内置 + MCP。

| 工具 | 功能 |
|------|------|
| `read_file` | 读取文件（`offset`/`limit` 分页，行号前缀）。**按扩展名分派**：`.ipynb` 解析 JSON 渲染每个 cell 的源码与输出（stream/error/display_data 的 text/plain），`.pdf` 用 pypdf 逐页提取文字（可选依赖，没装明确提示 `pip install pypdf`；加密试空密码、扫描件无文字如实说明）；图片扩展名直接指向 `read_image`；其它二进制**前 8KB 嗅探到 NUL 就给清晰说明**（Office/压缩包/可执行文件/音视频…），不再 utf-8 硬解出乱码或抛 codec 错。结构化结果与纯文本共用同一套按行分页（`_paginate_lines`） |
| `read_files` | 一次读多个文件（最多 8 个、总共 6 万字，超出的列出不读；遥控只读模式下每个路径都过黑名单） |
| `run_code` | 代码模式：一小段 Python 一轮里连着调好几次只读工具，只交回 print 的内容（`tools_code.py`；执行前语法树检查——不许 import / class / 生成器 / 下划线属性 / 栈帧属性 / .format，只给基本内置函数，60 秒限时；只读、可并行、Plan 放行、遥控拦）。`re` 直接可用（也可以写 `import re`），给的是只有正则函数和标志的精简版——完整模块挂着 enum / functools，顺着 `re.enum.sys` 能摸到整个运行环境；别的 import 被拒时指向 `run_command` 的 stdin（日志里 11 次 import 被拒，5 次是 re）。没有的 `tools.xxx`、`open` 报错时说清该用什么 |
| `explore` | 派只读探索子代理弄清一个问题，只交回带 `路径:行号` 的报告（`tools_explore.py`；只读、可并行、Plan 放行、遥控拦） |
| `read_image` | 读图（`tools_image.py`）：png/jpg/jpeg/gif/bmp/webp/tiff → 视觉模型识别成**文本**交回（`question` 指定侧重）。**复用用户上传图片时非视觉模型走的同一条 OCR 桥接**（`models.describe_images_with_vision`），不返回图片块——本项目工具结果一律是字符串（streaming 把 result `str()` 后打码/展示/装 ToolMessage），让 ToolMessage 带多模态块要改多处还要绕开 Anthropic 的 tool_result+图片 "Multimodal data corrupted"，代价是拿到识别文本而非原图。只认栅格图（SVG 是文本→read_file，ico 各家支持不一不收）；20MB 上限防超大 base64；没配视觉模型 / 识别出错 / 空结果都给明确文案。闸门同 fetch_url/web_search（只读+网络外发）：进 Plan 只读、可并行、不建运行记录；**不进遥控只读白名单、不进 explore 子代理工具集**（网络外发默认不给远程/子代理） |
| `write_file` | 创建/覆盖（**写盘前弹 diff 确认卡**；全量覆盖比 edit 危险） |
| `append_file` | 追加（**弹 diff 确认卡**） |
| `edit_file` | 精确字符串替换（比 write_file 安全省 token；**弹 diff 预览卡** + 路径白名单）；成功后回显改完的那一段（带行号、前后各 2 行，超过 16 行只露头尾）——以前只回"第 N 行附近替换 1 处"，模型常再读一遍确认 |
| `list_directory` | 列目录 |
| `run_command` | 执行命令（默认 300s 超时、可传 `timeout`；屏蔽交互式，**执行前弹内联确认卡**；cwd = 项目根；流式输出 + taskkill 杀进程树；**`background=True` 转后台**跑 dev server/长服务，立即返回 bg_id；**`stdin` 交给命令标准输入**：多行脚本用 `command="python -"` 加代码一次跑完，不用拼 `python -c`、不在项目里写临时脚本——文件面板语法高亮那次 87 轮里有 25 条 `python -c` 在试库的用法。确认卡在命令下面列出整段输入，危险判定和子 Agent 沙箱检查也扫它；「记住」和 python 命令共用（信任所有 python 类），旧版精确白名单不覆盖带 stdin 的调用；不能和 background 一起用） |
| `read_background_output` / `list_background_commands` / `stop_background_command` | 管理本会话后台命令（read·list 进 `PLAN_MODE_READONLY_TOOLS`、不弹确认）；通过 `background.py` 获取快照/请求停止，停止后保留结果，失败可重试；应用退出时 `stop_all_background` 统一有界清理 |
| `search_in_file` | 单文件关键词（`offset`/`limit` 分页）；单行超过 300 字只显示匹配附近 |
| `search_files` | 跨文件正则搜索（ripgrep 风格，忽略噪声目录）；`path` 给单个文件就只搜这一个、不按 `file_pattern` 筛——以前一律报"目录不存在"，三天里白费 24 轮。**单行超过 300 字只显示匹配附近的一段、一次最多交回 8000 字**（按行截，说明列了几处、还剩几处）：11-03 两次搜项目根各交回 3 万字（约 1 万 token，之后每轮都带着），95% 是会话 JSON 里一行两万多字的 `"content"` |
| `find_definition` / `find_references` | 跳符号定义 / 找所有引用（LSP→jedi→search 降级链，比正则准；只读、Plan 放行）。LSP **多语言**：按扩展名选服务器（Python/TS/JS/Go/Rust/C-C++/Java），jedi 只兜 Python，非 Python 且无服务器时提示装服务器 / 用 search_files |
| `find_tests` / `related_files` | 找某源文件的相关测试 / 列出导入·被导入·相关测试（只读、Plan 放行） |
| `remember` / `forget` | 长期记忆存取（本地安全操作，**不弹确认**，Plan 模式放行） |
| `spawn_agents` | 并行派生子 Agent 处理独立子任务（各自隔离 worktree、跑完合并；写工具，Plan/遥控拦） |
| `update_plan` / `set_step_status` | 任务计划：创建或显式调整 / 增量改单步状态（Plan 放行；驱动计划面板） |
| `get_project_instructions` | 读目标路径适用的项目规则（CLAUDE.md / AGENTS.md / .lingxirules；只读、Plan 放行） |
| `notify_user` | 主动给用户推 Telegram 通知（分级；本地安全、Plan 放行） |
| `code_map` | 代码库符号地图（命名组正则提取函数/类，commonpath 防越界；Plan 只读放行）；`path` 给单个文件就只列这一个 |
| `run_tests` | 跑项目测试，**自动认测试工具**（`test_runners.detect`）：Python→pytest、package.json→npm/pnpm/yarn test、pom.xml/build.gradle→Maven/Gradle（优先 mvnw/gradlew）、go.mod→go test、Cargo.toml→cargo test、.sln/.csproj→dotnet test；`k` 按名字筛用例各家语法各自映射。认不出的项目返回说明、改用 `run_command` 跑它自己的测试命令。Python 解释器由 `_resolve_python()` 选：项目 venv → 开发期 sys.executable → PATH；精炼失败定位 + 总耗时；`encoding=utf-8` 防 GBK 崩 |
| `git_diff` / `git_log` / `git_status` | 只读 git（看改动/历史/状态；commonpath 越界防护；Plan 只读放行）。`git_diff` **不指到单个文件时一次最多 8000 字，`max_chars` 传得再大也不超过**（模型常传 2–3 万把全部改动再看一遍，和它自己发出的改动重复）；超了按文件整段取舍，列出没展示的文件和各自的 `+增 −删`。**指到单个文件不设这个上限**——那是看全一个大改动的唯一办法，非 Git 项目的审阅义务也靠它了结。"还有没覆盖到的改动"那句在截断之后才附上，不会被截掉。**路径与范围都相对项目根**（`git diff --relative`）：项目根是仓库子目录时默认输出相对仓库根，按项目根拼会拼出不存在的路径；顺带把已跟踪改动的范围也收到项目内，与未跟踪新文件一致。截断时**整段展示出来的文件按文件记审阅**、范围看全了（或范围里的改动已被逐个看全）才记范围；**部分暂存的文件按「文件 + 哪一侧」记**（`git diff` 与 `--staged` 各只展示一半），两侧都看过才算这个文件看完，输出点名另一半用哪条命令看。**带路径的视图两个方向都核对另一侧**（未暂存视图查范围内的暂存改动、暂存视图查未暂存 + 未跟踪），完全没出现的文件挡住范围级记录并点名补看命令。判据见 [undo-and-verification.md](undo-and-verification.md) |
| `git_stage` / `git_unstage` / `git_commit` | git 写操作（暂存/取消暂存/本地提交，**无 push**）；**执行前强制弹确认卡**（按危险操作处理、不给"记住"选项）；路径白名单防注入，commit 不自动暂存、校验信息非空 |
| `check_code` | 静态检查（lint/语法/类型/编译）：Python 用 `ruff check --select F,E9`（没装退化到 `py_compile`）+ mypy；其它语言用 config 的 `check_command`，没配就跑项目的编译 / 类型检查（`static_checks.py`）；不弹确认；**Plan 模式拦截**（见下） |
| `apply_patch` | 多文件补丁（Codex 风格 `*** Begin Patch`）：统一校验、确认后，暂存新内容和原文件备份再写入。写入失败回滚，回滚受阻保留备份并报告；审批期间目标变化拒绝覆盖。不承诺崩溃时跨文件原子性。写工具、Plan/遥控自动拦 |
| `delete_path` / `move_path` / `copy_path` / `make_directory` | 文件系统动词（`tools_fs.py`）：删文件 / 移动·重命名 / 复制 / 建目录。**别用 `run_command` 跑 del、mv、cp、mkdir**——那样确认卡上只有一条命令，用户看不到动的是哪个文件，撤销也接不住。四条边界是刻意的：① **不递归删除**（非空目录直接拒绝，逐个文件删才能逐个确认、逐个撤销，备份量也有界）；② **不覆盖**（目标已存在就拒绝，移动/复制都不偷偷盖掉别人的文件）；③ **删 / 挪永远问、永远不给"记住"**（`confirm_bars._ALWAYS_ASK_PATH_OPS`，信任过的路径、accept_edits / auto 模式都不免；只有 bypass 跳过卡片，但仍留撤销记录），复制按普通写盘规则弹卡；**建目录（`make_directory`）只建空目录、不动已有文件，项目内刻意不弹卡**（建错随时删，无数据丢失风险），但仍守两道闸：**只能在项目根内建**（`_path_inside`，主 Agent 也不许往项目外 / 系统目录乱建）、**无确认界面时默认拒绝**（`_no_ui_refusal`，子 Agent 已在 worktree 沙箱内放行），同名**文件**已存在也拒绝；④ **目录操作不进撤销记录**（备份是逐文件字节的，目录树没有恢复材料），结果里如实写"本次改动不可撤销"。删/挪/复制都过 `_confirm_file_write(op=...)` 弹卡：删除给逐行 `-` 预览（超 200 行只露头尾并说明省了多少），移动给"从哪 → 挪到哪"，二进制文件只报大小和类型不硬塞乱码。符号链接一律拒绝（撤销按 realpath 认路径，链接顶替会恢复错地方）。Plan / 遥控只读 / RAG / 子 Agent 白名单**自动拦住这四个**（那些闸门是白名单，新写工具默认进不去），有测试钉住 |
| `search_knowledge` | 知识库语义检索（`query` + `scope` 分域，默认 all 各域按配额取；只读、Plan 放行、不弹确认；`kb_dir` 未配置/索引未建/索引锚不一致都返回明确提示而非空结果） |
| `fetch_url` / `web_search` | 网络只读：`fetch_url` 抓网址（http(s) only、二进制拒绝、无需 key；网站自己拒绝（401/403/429/451/5xx、超时、连接失败）且有 DeepSeek key 时改用 DeepSeek Fetch API 再读一次，被我们 SSRF 检查拦下的和 404 绝不转给第三方；解码依次信响应头 charset → 页面 meta charset → 能完整按 UTF-8 解开 → GB18030（响应头没写 charset 时 requests 给 ISO-8859-1，中文页曾经整页乱码）；HTML 只留正文——去掉导航 / 页眉页脚 / 侧栏 / 维基目录和引用角标，优先取 main / article，表格按行转成「列 | 列」；请求头中文优先，User-Agent 如实写灵犀，冒充 Chrome 实测无益反而被部分站拒）；`web_search` 用 DeepSeek / Tavily / 博查。DeepSeek 是它自家的 Search API（`/v1/tools/search`，和模型共用 `deepseek_api_key`，按成功请求计费、失败和空结果不收；时间范围用查询语法 `after:日期`，查询限 128 字；深度搜索时带 `need_content` 直接拿正文），实测中文结果最好、每次不到 1 秒、都带发布时间，英文会混进其它语言。（`web_search_provider`：auto = 中文先 DeepSeek → 博查 → Tavily，其余先 Tavily → DeepSeek → 博查，没配 key 的跳过；一个出错（超时 / 4xx / 额度用完 / 业务错误码）换下一个；deepseek / tavily / bocha = 只用它、不偷偷换。博查只接普通 Web Search，不接带大模型总结的 AI Search——总结由灵犀自己的模型做；time_range→freshness，news 没给范围按一周。深度搜索开关 `web_search_deep`：并发读前 3 个结果的原文（`_fetch_text`，与 fetch_url 同一套 SSRF/正文提取），按问题词（英文按词、中文按相邻两字）覆盖度挑 3 段，其余结果只留一句话；评测里每次结果更小更准，但模型照样多次追问，总 token 不一定更省，所以默认关。设置 → 高级 → 网络搜索 改这几项即时生效；可选 topic=news、time_range=day/week/month/year，depth=advanced 时段落挑得更准但每次多花一份额度，说明里要求先 basic；开 include_answer 把直接答案放最前；Tavily 的 content 是用 [...] 隔开的几段相关原文，按段保留、每段限 350 字、清掉引用标记——整条硬截会把后几段相关内容砍掉）。均进 Plan 只读、**不进遥控白名单**（网络外发默认不给远程） |

> 写盘类（edit/write/append）与路径类（delete/move/copy）工具共用 `tools.py:_confirm_file_write(full, old, new, op="edit", preview="")`：算 unified diff（`preview` 非空就直接用它——移动没有内容 diff，要展示的是"从哪挪到哪"）→ `ui.confirm_edit(path, diff_text, op)` 弹卡片 → worker 阻塞等审批。`op` 决定卡片标题/图标/徽标（`confirm_bars._PATH_OP_CARDS`）和能不能"记住"；它随 `result["_op"]` 走，Qt Signal 签名和侧栏 `pending_confirm` 回放都不用改。**无界面时默认拒绝**（`_no_ui_refusal`，按 op 说清是"不能改文件"还是"不能删除文件"），只有显式打开 `state.headless_auto_approve`（测试这类明确的无界面环境）才放行——以前 `ui_ref is None` 就直接放行，等于哪条路径没挂上界面就什么都不问。

### 工具结果里的密钥打码（src/secret_mask.py）
- **问题**：`run_command` 的子进程环境已剔掉名字像密钥的变量（`tools._scrubbed_env`），但工具**结果**本身没处理。`read_file` 读 config.json、`python -` 脚本 `print(key)`、命令 `echo` 出 key，原文都会进 `chat_history`（落进 chat_memory 的会话 JSON）、日志、界面，并随下一轮请求发给模型服务商；排查问题时常见的 `key[:10]` 同样是泄露。
- **收集**：`config.json` 里字段名含 `key` / `token` / `secret` / `password` / `passwd` / `credential` / `authorization` 的非空字符串（含这类字段下的字符串列表），**递归**进嵌套对象与数组（`custom_models`、`mcp_servers.*.env`、请求头都覆盖）；加环境变量里名字像密钥的值（判定直接复用 `tools._SECRET_ENV_PARTS`，不另抄一份）。值按空白切开取片段（`Bearer xxx` 只收 xxx），只收长度 `>= 12` 的片段；**路径、URL 不收**——`GOOGLE_APPLICATION_CREDENTIALS` 这类变量装的是路径，收了的话前缀打码会把输出里所有 `C:\Users…` 都换掉。
- **打码**：`mask(text)` 把命中的值换成 `‹已隐藏：字段名›`；字母、数字都有的随机样式值只出现开头一段（`>= 8` 字符，如 `key[:10]`）也打码——这么长的前缀已经能定位到具体 key；纯字母的口令只认完整出现（开头 8 个字母就是个普通词）。重叠命中取更长的那个。非字符串、内部异常一律原样返回，宁可漏打码也不让工具结果丢失。
- **接入点**（缺一不可）：`streaming._execute_tool` 拿到 `result` 之后（所有工具的唯一出口，含并行预取回放的 `_preinvoked`），以及 `tools._run_command` 的 `_reader` 逐行推给界面之前。此外 `_run_command` 在拼接完整输出后**再整段打一次**：流式按行/逐块切分，密钥恰好横跨两块时逐块打码会漏；不经 `_execute_tool` 直接调用（测试、子 Agent 内部）也要挡。
- **刷新**：进程内首次用到时构建一次；设置页 `_save()` 写完 config.json 后调 `secret_mask.refresh(self.config)`，新填的 key 立刻生效。打码表是进程级缓存，测试里要 patch `_loaded` / `_secrets` 隔离。
- **边界**：不改 config.json 的存储方式、不禁止读它（打码后照样能看配置，只是值被替换）；`verification.redact` 是另一套（证据摘要的正则打码），保持不动。回归见 `scripts/test_secret_mask.py`。

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
- 其它语言：配了 config `check_command`（`{file}` 占位，shell 执行）就用它；没配走 `src/static_checks.py`，两层可信度不同：
    - **编辑后：tree-sitter 语法提示**（`syntax_hints`）——只报这次编辑**新出现**的解析错误（和改前内容按说明文字比，行号会移动），**只提示、不记成检查没通过**。在真实代码上量过：Java / JS 0% 误报、TS 约 6%（.d.ts 的 `import("…")` 类型、JSX 属性 URL 里的 `&`）、C / C++ 七成文件报错、Kotlin 接口里的抽象方法也报；记成失败会把完成闸门卡在解析器的毛病上。C / C++ / Kotlin / Swift 等不查，.d.ts 不查
    - **check_code：项目自己的编译 / 类型检查**（`detect` + `run`）——tsc --noEmit（找 node_modules/typescript 用 node 直接跑，不用 npx；Vite 那种 `"files": []` + references 的总配置改用 include 覆盖了这个文件的子配置；`--incremental false` 不留 .tsbuildinfo）、vue-tsc、go vet、cargo check、Maven / Gradle 编译（项目自带 mvnw / gradlew 优先，测试源码用 test-compile / testClasses）、dotnet build；JS / PHP / Ruby 用 node --check / php -l / ruby -c 单文件查。权威结果，记验证证据。**只有这次改过的文件（`code_dirty_files` + 目标文件）里的错误算没通过**，别的文件里的照实列出"可能原来就有"但不拦；退出码非零却没有具体到文件的错误（依赖下载失败之类）记 error，不算没通过
    - 编译检查慢（tsc、cargo、mvn 动辄十几秒），不在每次编辑后跑；提示词要求改完 TS / Go / Rust / Java / C# 调一次 check_code（TS 的类型错误跑测试也发现不了）
- 可用 `auto_check_after_edit` 关掉自动触发
- 开关：config `auto_check_after_edit`（默认 true）；只检**刚改的那个文件**（快），防失控靠现有 agent loop 上限 + 模型没错就停
- `check_code` 工具是手动复查入口（同一套 `_run_code_check`）；编辑后自动触发不需要模型记得调它
- **Plan 模式禁用 `check_code`**（不在 `PLAN_MODE_READONLY_TOOLS` 里）：配了 config 的 `check_command` 之后它就执行那条命令，那是用户配的任意命令，无法保证只读。**不限于非 Python 项目**——`_run_code_check` 先判 `check_command`、再判扩展名，配了之后 Python 文件同样走它。名字像"检查"不代表只读——判据与 B03 的执行前记录同一条：**会不会执行项目代码或用户配置的命令**。拦截点在 `streaming._execute_tool` 的 Plan 闸，在 `invoke` 和执行前记录**之前**，拦住的是子进程有没有起来；光靠提示词劝模型别调不算拦截
- 拒绝时回一条 `tool_call_id` 对得上的 ToolMessage，用 `_PLAN_REJECT_NOTES` 给**针对性**说明（"Plan 模式只进行调研；执行代码检查请切换到 Act 模式"）。通用文案只说"只能用只读工具"，模型看到 `check_code` 被拦会以为是误判，转头用 `run_command` 跑等价的 ruff——所以要点明原因并明说不要换工具绕。程序不自动切模式、不代跑替代命令
- Act 模式不受影响；编辑后的 `_auto_check_suffix` 也不受影响（它挂在写工具后面，而写工具在 Plan 下本来就被拦）
