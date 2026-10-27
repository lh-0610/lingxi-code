# 运行记录、结果卡与恢复

> 从 CLAUDE.md 拆出的架构说明；入口与文档地图见 [CLAUDE.md](../../CLAUDE.md)。

## 运行记录与中断恢复（src/run_records.py，B03）
- **三个身份分开**：会话 Session（聊天历史 + 项目归属）／任务 Task（目标 + 计划 + 累计进度，B05 才建立，`task_id` 现在允许为 null）／运行 Run（一次发送·重试·继续启动的 agent 循环）。`run_id` 每次由程序生成，与 session_id / task_id 无关
- **统一 begin/finalize 在 `agent_loop` 最外层**，主体是 `_agent_loop_body`。普通聊天、重试、视觉桥接、Claude Code CLI 分支、所有提前返回和异常（含 `BaseException`）走同一条收尾路径。写在主循环内部的话，每加一个 `return` 就多一个漏网路径——而漏网的恰恰是异常和取消这些最需要记录的情形
- **`begin_run` 自己落盘，不能只更新内存**：进入运行主体前就要把 `phase=running` 存下去。只改内存的后果实测过——新一轮已经生成 run_id、进程在**首次工具调用之前**退出，磁盘上还是上一轮的 ended/completed，这次中断连一条记录都没有；而"重开发现 phase=running"正是 B04 恢复界面唯一的入口。把存盘留给第一次工具提交，等于把最容易崩的那个窗口漏掉了。开始记录与 `pending_verification` 必须是**同一份快照**，否则会出现"这一轮的开始配上一轮的待验证事项"
- 写测试时注意：在 `begin_run` 之后补一次手动 `save_session`，正好会把这个缺口盖住。验证入口行为的用例**一次都不要手动存**
- 收尾**在 worker 解绑前**完成，**持久化成功之后**才发结果通知 / 起标题线程（`_post_run_notify`）。`claude_code_loop` 里原来自己那套 save + 标题已删除，否则一次 CLI 运行会起两个标题线程、还先存一份"还在跑"的快照
- **三条不变量**：① `finalize_run` 幂等（重复调用不再写第二条记录、不再存一次盘）；② 旧 run 的迟到收尾按 `stale` 忽略，不覆盖同一会话的新 run；③ **运行结果与保存结果分开**——保存失败照样如实返回 `AgentResult`，但 `saved=False` 并追加"最新进度未保存"的独立提示
- **运行来源由程序填写**：`describe_source` 取最后一条**真实**用户消息。自动修复提示、完成闸门提示、视觉桥接说明都带 `additional_kwargs["lingxi_internal"]=True` 并**跟着落盘**（`_msg_to_dict`/`_dict_to_msg` 往返），在这里被跳过。不跳过的话，一轮自动修复就把"用户要求了什么"改写成程序自己生成的诊断文字
- **`phase` 与 `outcome` 不能互推**：`phase` 是"程序有没有收到本轮结果"（running / ended），`outcome` 是实际返回的 AgentResult（中断时为 null）。重开看到 `phase=running` 就展示"上次运行被中断"，**加载不改写磁盘事实**，绝不凭空补一个 completed/failed

## 工具边界的 inflight sidecar（src/run_records.py + streaming._execute_tool）
- 有副作用的工具**进入调用前**把一条小记录原子写进 `chat_memory/<id>.inflight.json`；写不成功就抛 `InflightWriteError`、**中止该工具调用**并明确报错。假装建立了恢复点再动手，比根本没有这套记录更糟——用户会以为有记录可查
- **为什么单独一个文件**：执行前若写主 JSON，就得把整段聊天历史重写一遍，长会话里每个写操作都付这个代价。sidecar 只装 ID / 工具名 / 路径 / 工作目录（B03 实测恒 480 字节；B04 多记 `work_root` 后随项目路径长度增加，154 字符的路径实测 707 字节；都不随历史增长），不复制历史、长参数或完整输出
- **sidecar 内是操作列表不是单个对象**：一条记录结构上不可能覆盖另一条未完成的。目前只读工具才并行、写工具串行，但这个前提不该被隐含依赖
- **判定哪些工具要记录**：只列纯读工具（`SIDE_EFFECT_FREE_TOOLS`），**不在清单里的一律记录**——未知工具、所有 `mcp_*`、以及 `remember`/`forget`/`notify_user`/`update_plan`/`set_step_status`。后面这几个虽在 `PLAN_MODE_READONLY_TOOLS` 里，却真有副作用（写长期记忆 / 推 Telegram / 改计划），**不能拿 Plan 白名单当这份清单用**
- **`run_tests` / `check_code` 同样要记录**，尽管名字听起来只是检查：`run_tests` 起 pytest，测试代码是项目自己的代码，写文件建目录都合法；`check_code` 一旦配了 config 的 `check_command` 就执行它，那是用户配的任意命令（`_run_code_check` 先判 `check_command`、再判扩展名，所以 Python 文件同样走它，不限于非 Python 项目）。把它们当纯读的后果实测过——测试真的写出了文件、进程在结果返回前死掉，sidecar 前后都是空的，恢复分类返回空列表，连"结果未知"的线索都没有。判据是**「会不会执行项目代码或用户配置的命令」**，不是名字像不像检查
- **需要记录的工具一律不并行预取**：`_can_parallel` 直接以 `run_records.needs_record` 为准，不另抄一份名单。预取是在 `_execute_tool` **之前**就把工具跑掉，那时记录还没写，执行前记录就成了摆设。两份清单分开维护必然漂移（`check_code` 就漂过）
- **提交顺序不可颠倒**：工具结果 + 台账 + 进度 + 待验证事项先进内存 → 写一次主快照（含匹配的完成回执）→ **只有正文可靠落盘后**才清 sidecar。先删标记再保存的话，保存失败就等于把唯一线索丢了
- **不能只看一个成功布尔值**：`save_session_report` 返回 `SaveOutcome(body_written / index_written / revision / bytes / elapsed)`。正文成功而索引失败时，回执**确实已落盘**、sidecar 可以清，但这次保存整体是失败的，要照实告诉用户。`save_session` 行为不变（仍抛异常）
- **判断操作是否已提交要核对 session_id + run_id + operation_id + 完成回执**，不能只看 revision 变大——别的保存同样推进 revision。回执存一个有界环（`recent_operations`，50 条）：只留一条的话，A、B 相继提交而 A 的清理失败时，A 会被误判成"结果未知"
- **四个中断窗口**：① 标记已写、工具未返回 → 结果未知；② 工具已跑、快照没写 → 结果未知，标记留着；③ 快照已写、标记没清 → 认成 `committed`，`load_session` 顺手清掉；④ 清理失败后又有别的提交 → 靠回执环仍认得出。**结果未知 = 既不算成功也不算未执行，不自动重放**
- 承诺的是"有记录地恢复并重新核对"，**不是**"任何命令恰好执行一次"。回执落盘 = "这条结果已可靠记录"，**不等于**"工具没有副作用"——执行失败同样留回执，因为失败也可能改了一半文件。「已调度」也不等于「副作用已发生」：写文件工具还要等确认卡，这中间被杀掉文件其实没动，但程序分辨不出，仍报未知
- 子 Agent（`is_subagent`）全程跳过：不写 sidecar、不写运行记录、不发通知、不起标题

## 运行结果卡（src/result_view.py + ui/result_card.py，B06）
- **判断与绘制分开**：`result_view.describe(snapshot, live_run_id=...)` 算出标题/检查行/过期/待验证/保存提示，返回纯数据；`ui/result_card.py` 只负责画。卡片上"能不能说检查通过了"是要被逐条测试钉死的判断，混进布局代码就只能靠截图验
- **卡片上每一句都要有程序证据**。模型正文说"已完成"不改变结论；没跑过检查就不写"测试通过"，纯问答连检查面板都不画（空面板会让人以为哪里出错）；`unverified` **不翻译成**"代码写完了只是没测"——它并不证明实现完整
- **结构化证据在实际执行位置采集**（`verification.record_evidence` → `verification["evidence"]`，随 `last_run.evidence.validation_runs` 落盘）：`argv` / `command` / `cwd` / `started_at` / `duration_ms` / `exit_code` / `status`。`run_tests`、手动 `check_code`、编辑后的自动检查共用这一套
- **状态枚举分得细是有原因的**：`passed / failed / not_run / timeout / cancelled / error / unknown`。"没装 pytest"和"测试跑完挂了"在卡片上是完全不同的两件事，合成一个布尔就说不出来。**拿不到退出码就是 `None`，绝不填 0**；pytest 退出码 5（一个用例都没收集到）算 `not_run`，不算通过
- **"筛完没剩下诊断"不等于"检查跑成功了"**：mypy 退出码 ≥2 是致命错误（语法错/用法错），它根本没完成分析 → 记 `error` 并保留摘要。退出码 0/1 且高信号筛选无命中才算 `passed`（那是有意的筛选，退出码照实记，用户能自己看出差别）
- Python 检查跑了几个检查器就记几条（`_run_code_check` 返回 `metas` 列表）："ruff 过了、mypy 超时"是常见组合，合成一条就看不出来。没装 mypy / 开关关掉 → **不留记录**（列一条 not_run 会让用户以为自己少装了什么）
- **过期判定不能用 `progress_revision`**：那个每保存一次就 +1，一次纯保存就会把刚跑完的绿灯标成过期。改用 `verification["change_revision"]`，只随 `mark_dirty` / `mark_blind_period` 递增。过期与否在 `collect_evidence` 里**定格**成每条记录的 `stale` 落盘——跨轮比较 revision 是错的，下一轮它从 0 重来，历史记录反而全变"没过期"
- 检查后又改文件 → 记录**保留并标过期**，不删掉（删了就显示成"从未检查"）；再查一次则新记录是新鲜的
- **结果快照与可变状态隔离**：`build_result_snapshot` 走 JSON 深拷贝，在 `finalize_run` 里生成（worker 还没解绑、run_id 是手上那一个），挂到 `sess.last_result` 并经 `run_result = Signal(object, str, object)` 投到主线程。**不能在迟到的回调里读 `sess.last_run` 猜自己属于哪一轮**
- 归属判据是**会话最新那条运行记录的 id**：后台会话的结果不插进前台（只置 `needs_redraw`），旧 run 的迟到结果不覆盖新 run，同一 run 只画一张卡
- **`finished` 信号带 worker 令牌**（`Session.worker_token`）：旧 worker 的迟到 finished 不能把新一轮正在用的按钮恢复成可发送；`_run_agent` 的 finally 也只在令牌仍是自己时才清运行态
- 重绘来源：内存 `last_result` → 持久化 `last_run` 重建（`snapshot_from_loaded`）。**render_log 不能是唯一来源**，它只活在内存里，重开程序就没了
- **显示卡片不往 chat_history 塞伪造的 AIMessage**；不新增总结模型调用。「继续任务」按钮由 B04 接上（见下一节），只在 `result_view.is_resumable` 为真时出现
- 按钮带着**卡片自己的** `view`（含 session/work_dir/run_id）走，不读当前前台会话——用户完全可能在点之前切走了。"查看改动"明确标注是**当前状态**，不冒充那一轮保存过的历史 diff
- **"查看改动"用 `work_dir` 不用 `project`**：worktree 里跑的那轮改动在隔离区，拿项目根去 diff 只会回一句"工作区干净"。`work_dir` **持久化在 `last_run` 上**（`begin_run` 初始化、工具边界与收尾各刷一次）——只靠"重建时按当前会话现算"是不行的，重开后 `Session.worktree` 早没了，算出来永远是主仓库。记下来只为历史查看，**不会**被恢复成当前执行用的 `Session.worktree`
- **记录的目录不存在时禁止回退**：`_project_cwd()` 在路径不存在时会回退到**进程 cwd**，于是 `git_diff` 悄悄查了灵犀自己的目录、回一句"工作区干净"，而弹窗标题还写着那个不存在的路径。`_on_result_view_diff` 先 `os.path.isdir` 校验，失效就明说不可用
- **临时借用线程绑定必须用 `session.get_bound()` / `restore_bound()`**，不能用 `current_session()` 存旧值：未绑定的主线程拿到的是 active，"还原"反而把主线程**永久绑死**在它身上，之后 `get_active()` 与 `current_session()` 指向不同会话，所有经代理的保存都写错人
- **一条通过不代表整轮通过**：标题判据是 `summarize_checks`——**每项检查的最新有效结论**都 passed 才叫 `clear`，任何失败/未执行/超时/未知都让它变 `unresolved`（标题改成"检查未全部通过"）。用 `any(passed)` 的话，"静态检查过了、pytest 挂了"会被写成"已执行检查通过"。只剩过期记录也算 `unresolved`——它顶替不了一次有效通过
- **同一项检查重试通过，旧失败要让位**（`effective_rows` / `check_identity`）：身份用**采集时算的指纹** `verification.identity_fingerprint`（检查器 + 目录 + 目标 + **完整未截断**的 argv/命令的 sha256），同指纹后来的取代先前的，旧记录保留展示并标 `superseded`。不这么做的话，文件没改所以旧失败**不会过期**，`all()` 一直把它算进去，卡片停在"检查未全部通过"，而完成闸门早就认了 `tests_passed=True`——两边对同一件事给出相反结论。identity 必须带目录和参数：对两个文件跑 ruff、在主仓库和隔离区各跑一次 pytest，都是两项检查，不该互相顶替
- **指纹必须在截断之前算**：展示字段是要脱敏截断的（argv 每项 300 字、命令 400 字），拿它们比身份会把长参数的差异整个抹掉。实测两组 `pytest -k <702 字符>` 只有末尾不同，截断后完全一样，于是第一组的失败被第二组"取代"、卡片写成"已执行检查通过"，而第二组根本没跑那个失败用例。指纹是单向哈希，完整命令带密钥也不会因此泄露
- 没有指纹的旧记录（本批之前存下的）**一律各算各的、永不互相取代**：宁可停在"检查未全部通过"，也不能靠截断数据蒙一个"通过"出来
- 取代不等于免疫：最新那次之后又改了文件，照样过期
- **证据在工具边界就进 `last_run`**，不是只在 finalize 拷一次：进程死在"工具结果已提交"与收尾之间时，磁盘上工具输出和完成回执都在、检查记录却一条不剩，而那正是最需要它的中断场景。恢复出来的只作**历史展示**，不回填 `verification`，所以不会变成当前一轮的通行凭证
- **`last_result` 缓存要跟着失效**：`reset_history` 和 `load_session` 都清它（Session 对象会被复用来装别的会话），重绘前还要用 `_result_belongs_here` 核对 run_id + session_id。不清的话"新建对话"之后旧那轮的结果卡会重新画出来，而 `last_run` 明明已经空了
- **布局上的两个坑（都只有真渲染才暴露）**：① 把卡片包进一层 `QWidget` 再进布局，那层壳不向上传递 heightForWidth，整张卡被算矮、底部按钮被边框整条切掉——检查行和按钮行一律 `addLayout` 直接加，`MessageView.add_result_card` 也直接加卡；② 等宽字体的长行是撑宽卡片的元凶（消息流关掉了横向滚动条，撑宽就是被裁掉），路径中间省略、摘要压成一行省略、无断点长串按字符数强插换行

## 恢复检查与继续任务（src/recovery.py + workspace_anchor.py，B04）
- **两条入口、一套处置**，都排在 `begin_run` **之前**（`agent_loop` 里的 `recovery.before_run`）：
  ① 结果卡「继续任务」（mode=continue）；② 上一轮被中断 / 留下结果未知的操作后，用户**没点继续、直接发了新消息**（mode=message）。处置把现场变化和结果未知的操作写进 `pending_verification`，由 begin_run 在 `reset_verification` **之后**填回——排在 begin_run 后面就被本轮重置清掉了
- **第②条入口不能省**：中断前那次写操作从没进入 dirty 文件，新一轮的工作区基线又建立在它之后，完成闸门会拿"没有已知改动"推出"工作区没变"，一次没验证过的写入按 completed 收尾（方案 §5.5 明令禁止的推断）。这条路径**不阻断**用户的新请求、不做现场比对，只补占位、并入义务、告诉模型哪些结果未知。判定 `needs_adoption`：磁盘 `phase=running` 且不是本进程正在跑的那一轮，或 sidecar 里有没回执的记录；正常结束的会话只多一次 `os.path.exists`
- **结果未知的写操作一律进盲区**（`tracking_incomplete` → `unknown_changes`），不只是把记录里的路径标脏：`run_command` 这类操作可能写了记录之外的路径。出口仍是显式 `run_tests` + `git_diff`。`_NON_FS_TOOLS`（remember/forget/notify_user/update_plan/set_step_status）结果未知时不碰项目文件，不进盲区
- **盲区挂在工具当时实际的工作目录上**：执行前记录写下 `work_root`（`run_records._operation_root(sess)`：按**传进来的会话**借用绑定调 `_project_cwd()`，与工具解析路径同一套规则，不按当前线程碰巧绑着谁算）。无项目会话的工具落在进程工作目录——早先恢复只认"会话的项目根"，无项目时根为空，就把结果未知的写入整个丢掉、sidecar 照样摘掉，没跑任何检查也按 completed 收尾（首轮复核实测）。旧记录没有 `work_root` 就退回会话目录；**实在确定不了**挂在 `recovery.UNKNOWN_ROOT` 占位键上——目录不知道不等于没有改动。消息模式下恢复核对自己出错（`_hold_obligation`）同样留一段盲区，不拿"没核对成"推出"没有改动"
- **不存在的目录不种基线**（`verification.restore_obligations`）：给已不存在的目录（项目搬走、被删）或占位键种空基线，每次复查都会枚举失败、重新记一段盲区，把刚补做的测试和 diff 结论又作废——按提示补查也收不了尾。盲区照样保留，出口仍是显式 run_tests + git_diff，只是不再追着不存在的目录反复失败
- **sidecar 里解析不了的条目不丢**（`_read_inflight_raw` 放进 `malformed`，改写时原样带回，`classify_inflight` 报 `unreadable`）：早先静默过滤——唯一那条记录坏了就等于"没有未决操作"，下一次改写还会把它彻底抹掉。恢复按"读不懂"处理（开盲区、有副作用的悬空调用按可能已执行说明），交接后才摘除，否则每一轮都会为它重开一次盲区
- **展示可以截断，留底必须完整**（`run_records.archive_malformed`）：摘除前把这些条目的**完整原文**写进 `chat_memory/<id>.inflight.unreadable-<内容哈希>.json`，恢复说明里只放摘要（`unreadable_count` + 最多 20 条 300 字预览）和对这个文件的引用（`unreadable_archive`）；只有留底成功、恢复说明落盘之后才摘，且只摘写进留底的那几条（`drop_malformed(..., archived)`）。早先只存截断预览就删全部原文——21 条损坏记录只剩 20 条截断预览，末尾内容和第 21 条永久丢失（第二轮复核实测）。文件名取内容哈希，同一批交接两次不会越攒越多；`delete_session` 经 `discard_inflight` 一并删除
- **work_root 按工具自己的目录规则记**（`tools_common.SHELL_CWD_TOOLS` / `tool_work_root`）：`run_command` / `run_tests` / `check_code` 在 `_shell_cwd()`（跟随 cd）里执行，其余按项目根解析路径。早先一律记 `_project_cwd()`——`cd B` 之后命令写在 B，记录却是项目 A（第二轮复核实测）。改了某个工具用哪个目录要同步这份清单，`test_b04_second_review.py` 有一条用例按源码核对：tools.py 里调用 `_shell_cwd()` 的实现恰好就是清单里的工具
- **补齐悬空 tool 调用不冒充结果**（方案 §5.4）：占位写"应用恢复提示：未取得执行结果"，并区分「已调度、可能已经执行，不要直接重复」（sidecar 有记录没回执，或 sidecar 读不懂且是有副作用的工具）与「没有执行记录，按未执行处理」。占位带 `lingxi_internal` + `lingxi_kind` **落盘**，重开后不再悬空。`streaming._sanitize_tool_pairs` 的发送副本兜底文案同样改成不冒充成功/失败的说法
- **注意只读工具不存盘**：崩在某轮第一个有副作用工具里时，磁盘上**根本没有**那条 AIMessage（上一次存盘在它之前），只剩 sidecar。所以结果未知的操作必须在恢复说明里单独列出，不能只靠悬空调用的占位
- **交接 inflight 的顺序**：恢复说明 + 占位 + 义务**落盘成功之后**才从 sidecar 摘掉交接过的记录（`acknowledge`）；没存上就保留，下次打开仍会提示。摘掉的记录精简后挂在恢复说明的 `lingxi_recovery.operations` 上**随消息落盘**——方案要求"保留 inflight 记录用于诊断"，线索从"进行中"挪到了永久历史，不是删掉
- **现场锚点**（`last_run.workspace`）：只记**涉及文件**（dirty ∪ 待验证）的 sha256 + HEAD/分支/根提交，不哈希整个项目。begin_run 首拍、工具边界与收尾刷新。两条底线：指纹相同**不等于**测试通过；HEAD 相同**不等于**工作区相同（HEAD 没变照样逐文件比）。上次一个文件都没记录时不说"记录过的文件都没变"这种空话（`compare` 的 `checked` 计数）
- **只写文件的工具边界不起 git 子进程**（`run_records._FILE_ONLY_TOOLS`，沿用上一次的 HEAD/分支、只重算指纹）：一次 git 子进程几十毫秒，是 B03 整套工具边界提交（7–15 ms）的好几倍。命令、提交、测试、检查命令、MCP、子 Agent、未知工具照常刷新。沿用的信息只会更旧，比对时只会多报、不会漏报
- **仓库身份靠根提交**（`git rev-list --max-parents=0 HEAD`）：同一路径换成了另一个仓库 / 不再是 git 仓库 / 目录不在了 → 阻断继续。HEAD、分支、外部改动、删除、新出现的文件 → 报告；**上一轮确有待核对的东西时**（`at_stake`）HEAD/分支变化与"无法完整核对"才并入盲区——已结束的纯问答轮次不能因为有人提交了代码就把新一轮拖进测试要求
- **模型按稳定标识找回**（`last_run.model = {id, name, type}`，不存下标、不存密钥）：`find_model` 要求**类型一致**——同一个 model_id 换了类型就是另一种执行后端，不许静默落过去；找不到就阻断，用户可显式"改用当前模型继续"。配置缺 key 同样阻断。**用户在顶栏亲手换过模型**（`Session.model_user_choice`，`_on_model_changed` 置位；切会话的顶栏同步走 blockSignals 不会误标）就尊重那个选择，不再改回记录的模型——重开的会话模型下标是从前一个会话继承来的、谁也没选过，那种才按记录找回
- **Plan 仍是 Plan**：继续不改 `agent_mode`；预检只加一条说明
- **主线程只做便宜的事**（`ui/run_results.py` 的 `_on_result_continue`）：核对归属（卡片的 session_id + 该会话**最新**一条运行记录的 id）→ **屏障**等旧 worker 真正退出（`Session.last_worker.is_alive()`，不只看 `is_generating`——强制停止会立刻把它置 False，而旧线程还在收尾写这个会话）→ 预检 → 起 worker。屏障等待期间 `Session.resume_pending` 挡住再点继续、发送、遥控注入、重试；屏障之后**再核对一次归属**（等的那段时间里可能已经开出了新的一轮）。等待中切走会话 → 取消，不替看不见的会话在后台开跑
- **项目被挪走**：阻断对话框提供「重新选择项目目录…」（`_relocate_session_project`，按根提交核对是同一个仓库；判断不了就问用户，确定是另一个仓库就拒绝）和「复制原路径」。改挂后照常走继续前的全套核对，且"目录变了"会并入待验证义务
- **搬家要迁移活动追踪路径**（`recovery.relocate_obligations`）：确认是同一个项目后，`pending_verification` 里挂在旧位置的盲区根目录、旧位置下的绝对文件路径，以及内存验证状态里同样的键，全部换算到新位置；执行前记录里旧位置的 `work_root` 在并入义务时同样换算（`_fold_obligations` 的 `_follow_move`，原始写法与 realpath 两种都认）。**历史锚点不动**（`last_run.work_dir` / `last_run.workspace` 保留原值用于诊断），恢复比对本来就按现在的目录做
- **锚点逐字段严格校验**（`workspace_anchor.normalize`，永不抛异常）：坏了整张降级成带原因的 `{"invalid": ...}`，比对时报"现场记录已损坏、无法核对"（有待核对的东西时并入盲区），**不挑着留好字段**——比如坏的是 `errors`，"只记录了前 200 个文件"这类说明就悄悄没了，结论反而更乐观。早先 `errors` 被改成数字时 `load_session` 直接抛 TypeError、整段聊天打不开；保存路径也对旧文件做同一次归一化，于是之后每次保存都失败。`memory._normalize_progress` 现在也是永不抛异常的外壳：漏网的意外异常一律按"进度读不懂"处理（保存路径据此隔离留底，加载路径只作废进度、照常恢复聊天）
- **来源**：继续的那一轮 `source.kind="continue"`，文本仍是最后一条真实用户消息（任务是它提的）；中断后直接发消息的那一轮仍是 `user_message`
- **不恢复**旧确认许可、后台进程控制权、隔离区使用权——它们本来就不持久化；隔离区里跑的那一轮继续时回到主项目并明确说"旧隔离区不会被自动合并"
- **重绘**：内部 HumanMessage（`lingxi_internal`）一律不画成"你"的气泡。早先只认视觉桥接的文字前缀，自动修复 / 完成闸门的内部提示在重开会话后会被画成用户说的话；恢复说明画成程序说明，优先用当时实时显示的那段（`lingxi_recovery.note`）
- **测试里必须阻断真实模型与 CLI**：新建的 Session 默认模型下标可能落在 Claude Code 上（backlog 里的默认模型 fallback 问题），漏设一次模型就会真起一个 `claude` 进程——写 B04 的真进程用例时实测踩到过一次。`test_b04_recovery.py` 用 autouse 夹具把 `_create_llm` / `_claude_code_loop` 换成直接报错，子进程脚本里同样显式阻断
