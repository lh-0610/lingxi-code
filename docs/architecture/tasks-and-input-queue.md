# 任务身份、退出屏障与输入队列

> 从 CLAUDE.md 拆出的架构说明；入口与文档地图见 [CLAUDE.md](../../CLAUDE.md)。

## 任务身份、来源追溯与要求计划分离（src/task_state.py + ui/task_panel.py，B05）
- **稳定消息 ID 与来源追溯**：
  - 真实用户消息打上稳定 `msg-uuid`（`tag_user_message`）与 `lingxi_kind="user_input"`。
  - 程序内部消息打上 `tag_internal_message`（`resume`, `recovery`, `repair`, `gate`, `vision_bridge`），历史重载时不被误识别为用户要求。
  - 旧会话消息若缺少类型标记，一律回退为 `legacy_unknown`。
  - `memory._msg_to_dict` 严格执行元数据白名单（`lingxi_message_id`, `lingxi_internal`, `lingxi_kind`, `lingxi_recovery`），非法键名直接剔除，防提权与伪造。
- **任务身份建立与保持（`task_id`）**：
  - 首次有效计划建立任务身份（`create_or_attach_task`），生成 `task-<hex>` 并绑定当前会话与需求消息 ID。
  - 语法非法或未检出 checklist 的失败调用不产生半截任务。
  - 任务推进进度、调整步骤均保留原 `task_id`，绝不因换做法而变成新任务。
- **计划变动对比与版本递增**：
  - 单纯推进步骤（`[x]` / `[~]`）无需 explanation，更新计划但**不动** `plan_version` / `last_plan_change_reason` / 摘要（即使顺手带了 explanation）。早先每推进一步就把原因覆写成空串、版本 +1，面板和模型都看不到上一次结构调整为什么改；判据是「步骤文字/顺序/数量变了或有进度回退」，与 `set_step_status` 一致；
  - 清空计划不是"首次有效计划"：没有任务时 `update_plan("")` 只清计划、不建立任务身份；
  - 增删重排步骤、步骤回退或清空等结构变动，必须填写 `explanation` 说明具体原因，否则工具拒绝并原样回退，避免模型随意重排；
  - 结构变动生效时保留 `task_id`，递增 `plan_version`，记录 `last_plan_change_reason` 与 `last_plan_change_summary`（由 `summarize_plan_change` 比对）。
- **显式新任务切换（`update_plan(..., new_task=True)`）**：
  - 用户明确开始另一件事时设 `new_task=True`，必须附带 `explanation` 说明切换原因；
  - 旧任务自动快照归档进 `sess.archived_tasks`；
  - 生成新 `task_id` 并建立新任务；
  - 记录可撤销记录 `sess.last_task_switch`，递增 `task_switch_version`。
- **任务切换撤销与 Worker 屏障**：
  - 任务面板顶部展示可撤销横幅：`已开始新任务（原因：……）[撤销任务切换]`；
  - 用户点击撤销时核对 `switch_version` 与 `new_task_id`，恢复原任务要求、固定限制与计划；只移除这次切换归档的那一项，被撤销的新任务以 `archive_kind="undone_switch"` 留在归档里；
  - `undone_switch` 归档**不算认领**要求消息（它的要求已并回原任务）：否则撤销后没等用户再开口就重做切换，触发消息拿不回来，新任务一条来源都没有；
  - **核心准则：撤销任务切换绝不回滚文件改动，绝不删除新任务已执行的工具记录或待验证义务**；
  - 若模型正处于生成或执行中，走强制停止并与「继续任务」共用 `resume_pending` 屏障，等 worker **真正退出**后再撤销；超时或等待中切走会话就放弃，不硬改；
  - 保存用 `save_session_report` 区分结果：正文已落盘、只有索引失败时如实说"已保存、索引待修复"，不说成"尚未写入磁盘"。
- **固定限制（`pinned_constraints`）**：
  - 仅用户可编辑（对话框 `EditConstraintsDialog` 修改或删除，即刻落盘）；模型工具无权改写；
  - **按输入原文保存**（复验最终裁定）：不解析、不剥除任何行首字符——`- * 参数`、`- - 参数`、`--force`、`- 列表`、编号、圆点统统是用户内容；项目符号只存在于显示层（面板画 `•`，模型上下文每条一行，编辑框示例不引导打列表符号）。过程中的中间契约（正则剥 `-*•·`、首存剥一层 `- `、known 保护已存行）全部废弃——只要还根据内容猜哪些字符属于用户，改句尾、敲多层横线就会丢字符。逐行拆分只为"每行一条"的输入形态与字符计数，由此存入/还原/再次解析天然可逆。打开/保存/重开/修改/再存，用户写下的字符不得增删；
  - 打开 → 保存 → 重新打开 → 不修改再保存，内容必须逐字一致；字符计数、校验、存储、重载用同一约定（都按存储后的内容字符数计）；
  - 对话框编辑的是**打开时**那个任务：模态对话框挡不住 worker，编辑期间模型可能切换任务或建立首个任务。保存时任务 ID 对不上就拒绝这一次（内容保留、改指向现任务，用户再点一次才存），不把 A 的限制写到 B 上；
  - 保存失败只在**正文没写进去**时回滚内存（`save_session_report().body_written`）；正文已落盘而索引失败时保留并提示——回滚的话内存说"没保存"、磁盘上却有，下次保存前退出就是反的结论；
  - 实时字符计数，硬预算上限 1000 字符，超限拒绝保存，绝不静默截断；
  - 对话框带生效时机提示（下次模型调用生效）。
- **尾部运行态注入与 Prompt Caching 保护**：
  - 任务目标、用户固定限制、原始/补充要求来源以及计划变动摘要注入在 `roles.get_volatile_context()`（作为 `<system-reminder>` 挂在历史尾部）；
  - 任务上下文独立预算 3000 字符，**对最终实际输出计数（含标题、换行、各段分隔），是硬上限**：固定限制完整保留（内容 ≤1000 字符时最坏渲染成本 ≈2×1000+标题，必然放得下——上下文里每条限制独占一行、不加项目符号）；任务目标段内**原始目标保底**（选入顺序原始优先：标题 + 消息 ID + 最小摘录必然在场，最新补充其后，扩展时仍最新优先）——早先选入顺序最新优先，预算一紧原始目标的正文和来源会整个消失、只剩"另有 N 条未展示"；模型摘要这类装饰内容装不下时先让位；验证义务与计划说明装不下时折叠成一行摘要并注明省略了什么，补读入口只指向真实存在的任务面板与 run_tests / git_diff。计划说明连摘要都放不下时，把义务全文降为摘要换它入场——被省略的每段都要有交代，不悄悄消失；
  - `roles.get_system_prompt()` 保持稳定前缀，绝不将易变的任务信息注入顶层系统提示，确保 API 提供商 prompt caching 命中率不受影响。
- **验证状态只有一套结论（`task_state.verification_status`，复验收敛）**：
  - "还有哪些义务未解决"复用 `verification.gaps_from_state`（含检查范围、Git 工作区边界、证据过期、复查取代），面板与模型上下文共用，不另养一套放行规则；内部判定失败时如实报"无法核对"，不因异常显示成没有缺口；
  - `dirty_files` / `unknown_changes` 只是**记录**（本轮涉及过哪些改动、发生过哪些盲区），记录还在不等于义务没解除——解除与否以 gaps 为准。早先把"记录非空"当"义务未了"，验证通过后面板仍写"待验证改动 / 无法确认已验证"、上下文仍要求重复验证；
  - 两个时态：运行态里有内容（begin_run 之后，快照义务已并入）→ 以 `gaps_from_state` 实时判定为准（pending_verification 在运行中可能还没刷新，不能只信上一次持久化快照）；运行态还是全新的（刚加载、恢复未并入）→ 持久化快照是义务的唯一线索，原样转述、不做解除判断，此时也没有本轮检查证据，不可能显示"已验证"；
  - "checked" 只认当前运行态里未被改动作废的检查证据，历史成功不是当前运行的通行凭证；显示通过不靠删除记录——dirty/盲区记录与证据保留原样。
- **任务目标与计划面板（`ui/task_panel.py`）**：停靠在主窗口最右侧（固定 360 宽），顶栏右上角的面板按钮收起 / 展开（选择记在界面偏好里），窗口窄于 1200 自动收起（给对话和顶栏让位置）：
  - 区域 1：要求与固定限制（原始要求、补充要求及来源 ID，固定限制，带编辑限制按钮）；
  - 区域 2：执行计划步骤（可撤销切换横幅、自绘的完成 / 进行中 / 待办圆标、进度条、变动原因/摘要）；进行中的圆环只在本轮确实在跑时转动，跟随发送键的运行状态；
  - 区域 3：执行与验证状态（待验证文件列表与义务提示）；
  - 区域 4：验证证据（每项检查最后一次的结论，与结果卡共用 `result_view.effective_rows`；加本会话仍在跑的后台命令），面板显示时每 2 秒刷新；
  - 普通问答无任务无计划时整栏隐藏，保持界面极简不干扰用户。
- **消息流里的计划步骤分段（`ui/message_view.py`）**：计划的当前步骤换了（`show_plan` → `_on_plan_update` → `MessageView.on_plan`），就在这一轮里插一个带编号的步骤节点，之后的工具卡排在它下面；步骤来自程序记下的计划状态，不从回复文字里猜。重绘历史时按被工具接受了的 `update_plan` / `set_step_status` 推演（`replay_plan_change`），只用于画分段，面板仍以会话保存的计划为准。

## 统一退出屏障：停止后启动下一轮（src/ui/run_admission.py，B09a）
- **忙碌判据只有一条**：`_worker_alive(sess)` = `is_generating` 或 `last_worker.is_alive()`。强制停止会立刻把 `is_generating` 置 False 而旧线程还在收尾写会话；`finished` 信号也可能先于线程退出到达——两者都不能作为"可以开新一轮"的依据。各入口的忙碌判断统一走这个谓词（`_resume_wait` / `_wait_worker_and_undo_switch` 亦然），不各养一套。
- **统一启动闸 `_gate_new_run_request(sess, source, ...)`**：普通发送（按钮 / Enter）、遥控注入、重试都过它，返回 `start`（会话空闲，调用方自己启动）/ `queued`（已登记待启动请求）/ `busy`（拒绝）。`_do_send` 顶部还有一道**底层硬闸**（拒绝时不产生任何副作用）——防绕过入口直接调它双开 worker。`_spawn_worker_thread` 是起 worker 线程的唯一出口：**线程身份在 `start()` 之前就写进 `thread` / `last_worker`**——`start()` 返回后线程未必马上进入 target，这个窗口里点停止会清掉 `is_generating`，若屏障还看不到线程占用，下一条消息就会双开同一会话（实测复现）；启动失败回滚 `is_generating` + 按钮态并如实反馈，不让会话卡在"生成中"（`Thread(...)` 构造本身也可能抛，回滚要覆盖它）。
- **只有"已停止但旧 worker 还在收尾"的窗口才排队**。仍正常生成时发送被拒（点发送按钮 = 停止的语义不变，不代停、不新增排队）；**重试不排队**（它要动 `chat_history`，等旧 worker 退出前碰不得）——被拒时 toast 明确反馈。遥控注入被拒时经 `telegram_push.push` 给手机端明确回执（正常生成中、排队、超时、**立即发送启动失败**——两条路径都要回执，不静默丢弃）；归属在接收时绑定当时的前台会话。
- **停止按钮不再 join**：`_on_send_click` 的停止分支改走 `_force_stop_generation()`（wait=False）——不能在主线程 join 3 秒；收尾窗口由屏障兜住，这期间点发送只会登记待启动请求。
- **停止辅助函数停的是线程，不是标志**：`_force_stop_generation` / `sidebar._stop_session_generation` 的忙碌判据是 `_worker_alive`，不再因 `is_generating=False` 提前返回——非阻塞停止清掉标志后线程还在收尾，此时删正文，收尾的 save_session 会把已删会话连正文带索引原样存回来（实测复现）。`_delete_session` 用同一判据：worker 活着就 join 等真正退出再删；等不到（卡在长工具里）toast 明说"删除未执行"。已知边界：标题生成线程不属于 `last_worker`，删会话若恰好撞上它写标题，仍可能复活索引条目（先于本批存在，待单独立项）。
- **待启动请求 `_PendingRunRequest`**：一次请求的内存状态（不是多条输入队列），登记时定格内容与上下文——`source` / 文本与附件快照 / `old_worker`（登记时活着的旧线程）/ `session_id` / `session_project` / `global_project`；每会话最多一条（重复点击明确拒绝，不二次登记）。
- **接纳是渐进复核 + 一次性弹出**：worker 结束信号到达时主动 `_admit_pending_run_for`（轮询 100ms 是兜底），`_worker_alive` 仍忙则回到轮询（绝不带病启动）；弹出后逐项复核——active 未切换、`current_session_id` 未变（重置/重载）、项目归属未漂移、**隔离区（实际工作目录）未换**（项目根没动、worktree 从 A 换到 B 照样不算原来的现场）、`last_worker` 还是登记那条、请求未取消——任何一项漂移都拒绝发送，绝不把输入发到别处。重复回调、迟到 finished 因一次性 pop 而无第二次启动。
- **`_do_send` 的存盘在启动成功之后**：失败路径要像这轮从未发生过一样撤干净——历史 pop、**任务要求引用显式摘除**（`sync_user_requests` 只加不减，撤销必须自己从 `request_message_ids` / `request_sources` 里拿掉消息 ID）+ 聊天区内联失败说明；存盘后移保证磁盘上根本不会出现一条"从未发送"的消息（否则重开时它又变成已发送记录，实测复现）。
- **输入保留语义**：排队期间**不清输入、不画用户气泡、不进历史**（等待不画成已发送）；接纳成功后只清理实际发送的那份——输入框文本与快照**逐字相等**才清空（期间改过就保留新稿），附件按快照逐个移除（新加的附件保留）。超时 / 取消 / 失效时 toast 说明"未发送"，输入原样保留可重试。
- **超时不强行放行**（复用 `_RESUME_WAIT_MS × _RESUME_WAIT_LIMIT` = 15s）：超时只释放本次等待占用并说明，不启动新 worker、不清任何运行标记——旧线程仍在运行这一事实不被清除等待标记所掩盖。
- **与 B04/B05 互斥**：「继续任务」/「撤销任务切换」发现有待发消息时明确拒绝（先处理消息），不让两条请求在屏障后同时被接纳；反向的 `resume_pending` 挡发送在闸里照旧。
- **「继续任务」起线程必须带 `args=(sess,)`**（`_start_resume` → `_spawn_worker_thread`）：`_run_agent` 的 `sess=None` 会退回"线程启动那一刻的 active"——点继续后、线程进入主体前切了会话，worker 就会绑到别的会话上：覆盖那边的 worker 身份、恢复检查拒掉继续、收尾却把那个会话标成空闲，屏障随之失效（实测复现）。回归见 `TestContinueBindsItsOwnSession`（用延迟启动的 Thread 包装把线程钉在 target 第一行之前，恢复检查与 begin/finalize 走真实实现）。
- **明确取消钩子**：`_load_session` / `_activate_session`（含新建会话、切工作区）/ `_switch_project` / `_delete_session` / 角色卡切换与恢复默认（reset_history 会回收 Session 对象）各自调用 `_cancel_pending_run(sess, 说明)`——待发请求不跟着会话切走、不发给别的会话。
- **起线程前作废旧令牌**：`_spawn_worker_thread` 先 `sess.worker_token = None` 再 start——新运行已接纳，旧 worker 的 finished 无论何时到达都按迟到处理；否则"轮询先接纳、旧 finished 后到"会抢在新线程登记令牌之前把按钮恢复成可发送。
- **回归**：`scripts/test_b09_exit_barrier.py`（真实 ChatUI + 假 agent_loop + threading.Event + 真 Qt 事件循环）。测试造数注意：conftest 的全新 active 会话历史是**空列表**，`save_session` 会按"历史过短"跳过、会话拿不到 id——屏障用例须先补 SystemMessage + HumanMessage 再存（`_make_saved_session`），否则重置/重载核对（比对 `current_session_id`）形同虚设。要钉"已启动未登记"窗口，用 stub 替换 `_run_agent` 并在第一行阻塞——等它进入假模型循环就错过窗口了。
- **worker 线程的隔离数据根**（`paths.set_data_dir` 是**线程本地**的，`isolated_memory` 只覆盖主线程）：worker 里做真实存盘（恢复检查 / begin / finalize / save_session）的用例必须在主线程先 `paths.get_data_dir()` 取根、闭包捕获、线程入口 `set_data_dir(root)`、finally 还原——进线程后再取拿到的就是默认根（等于没隔离，实测一次用例越界往真实 `chat_memory/` 写正文 + index 共 6 次；B04 的 shaped loop 同病，同批修掉）。该文件用 autouse 的 `_memory_write_guard` 夹具盯 `memory._atomic_write_json` 的落盘路径，越界当场失败——其他有 worker 真实存盘的测试文件建议照搬。

## 运行中输入队列（src/input_queue.py + src/ui/queue_panel.py，B09b）
- **用户可见行为**：生成中 Enter 提交 → 入队（"已排队，当前轮结束后处理"），点发送按钮仍是停止——两个入口的语义靠面板与 toast 区分，不混淆。队列面板（输入区上方，空队列隐藏）列出正文摘录、附件数、状态与暂停原因；`queued` 可编辑/删除/立即处理，`held` 可重试/删除，`needs_check` 可重新入队/丢弃，`dispatching/admitted/done` 锁定编辑删除。用户点击「继续处理队列」是显式恢复，不被旧终态二次拦下（`_maybe_auto_advance_queue(check_outcome=False)`——终态门槛只约束"一轮结束后无人操作时的自动推进"）。
- **队列属于 Session**（`Session.input_queue`，随 progress 信封持久化）。条目有稳定 `queue_item_id` 与**入队时就分配的** `message_id`；**入队不进 chat_history、不调 sync_user_requests**——正式派发（接纳）时沿用该 message_id 走 `_do_send(..., message_id=...)`（视觉桥接路径同样透传），B03 run_id 与 B05 要求来源照常接入。B05 来源不重复：入队不占、派发恰记一次。模型无法触达队列；`legacy_unknown` 不升级。
- **状态机**：`queued → dispatching（派发准备记录先落盘，绝不先删条目再起线程）→ admitted（锁定）→ done`；派发前校验失败 → `held`（附件/归属原因，重试重过校验）；等待接纳阶段失败（闸超时/归属复核/等待被取消）→ 回 `queued` 带 error 说明；崩溃窗口（dispatching/admitted 落盘后进程没了）→ 加载时标 `needs_check`，不自动重发、不声称已处理。**每个状态转换先落盘再生效**；正文保存失败回滚内存（磁盘上从没有过这次变更），正文成功索引失败保留并如实提示、暂停自动推进。
- **持久化**：队列在 `_snapshot_progress` 的 snapshot_lock 内深拷贝、随会话 JSON 一次落盘（队列-接纳-历史同一快照）；损坏/版本不认识整块作废走 progress 隔离留底，聊天历史照常加载；旧会话无该键 → 空队列。**新进程恢复队列默认暂停**（normalize 就地落地，注意：恢复说明不是错误——`normalize` 的错误通道只给结构性损坏，把恢复说明塞进错误通道会让每次解析都判"损坏"，实测踩过）。`reset_history` 清队列（开新对话）。
- **回滚只看 `body_written`，提示与暂停看 `fully_saved`**（`_persist_queue_change`）：正文已落盘、只是索引失败时**不回滚内存**——回滚会让内存与磁盘分叉（界面"未入队"、磁盘却有条目，用户再提交即重复消息，实测踩过）；改为保留已落盘状态、如实提示并把队列暂停自动推进（暂停标志随后续保存落盘）。正文没写进去（含 skipped）才 undo。
- **与 B09a 的关系**：队列派发与单条待启动请求共用 `_gate_new_run_request` 仲裁与 `_spawn_worker_thread` 唯一出口；`_PendingRunRequest.queue_item_id` 非空表示这次启动是队列派发，接纳走 `_admit_queue_dispatch`（一次性：条目必须还是 dispatching）。立即处理 = 非阻塞 `_force_stop_generation()` + 队列暂停（其余条目不跟进）+ 该条走同一条派发流水线——旧 worker 真正退出并收尾后才起新一轮。
- **派发前核对**：附件重读 + 内容 sha256 比对（缺失/不可读/变化 → held 并点名，绝不静默去附件或换内容发送）；项目/隔离区/任务归属与入队快照比对（入队时 task_id 为 null、派发时有任务，是本轮建立身份的正常路径，不算漂移）。容量上限在 limits.py（条数/单条/总量），超限拒绝并保留输入，不截断。
- **收尾的身份核对与终态门槛**（`_settle_finished_queue_item` → `(saved_ok, advance_ok, pause_reason)`）：`last_run.source.message_id` == 条目 message_id 才能把这一轮记到它头上——图片识别失败时消息**先落历史再识别**（识别失败也不丢消息、身份也对得上），并在失败路径补 begin/finalize 留下真实的 failed 记录；恢复检查拒绝的本轮没有运行记录，绝不把上一轮的 completed 与 run_id 抄给新条目还继续派发（实测踩过）——如实记 failed、暂停、可重试。终态门槛覆盖两种情形：已接纳项的 outcome 非 completed、以及**普通发送那一轮**（无对应队列项）的 outcome 非 completed——后者漏掉就会带着 failed 的上一轮自动启动下一轮。本轮 finalize 的正文保存失败记录在 `Session.last_run_save_failed`（begin_run 开新轮时清零）：即便收尾补存成功，"保存失败后暂停"的约定依然生效。**advance 的终态门槛先于队头选择**——唯一条目记 done 后队列为空时，异常终态照样要让队列暂停（否则失败静默归零）。`auto=False` 用于显式继续（恢复/重试/编辑后）：旧终态不拦路，但仍要求前台会话且队列未暂停。后台会话的条目照常记终态但不自动推进（第一版只自动派发前台会话）。切走会话：取消尚未接纳的派发等待、条目保留并暂停（切回后明确恢复）；其他会话运行中的 worker 照常。
- **提交成功 ≠ 允许继续执行**（`_queue_begin_dispatch`）：派发准备的保存若不是 fully_saved（索引失败），条目回到 queued 并取消派发——实测队列已 paused=True、线程仍从 1 增至 2。编辑对话框期间 hold 该条（模态也处理 Qt 事件，上一轮可能恰好结束并把**原文**派发出去，用户确认时新稿已丢失，实测踩过）；确认/取消/异常都恢复 queued。**编辑后的暂停解除只针对【编辑造成】的那一层**（blocked-head 原因含"正在编辑"）：编辑窗口里上一轮失败/保存出错给队列新增的暂停必须保留，不能拿编辑前的旧快照一刀切（实测踩过）；**退出分支分两类**：空内容/校验失败不涉及保存，恢复推进原消息；**编辑的正文保存失败则明确暂停队列**（条目回滚保留、暂停原因"编辑保存失败"、不自动执行旧消息——实测 worker 1→2 原消息照跑），等用户手动继续。取消编辑也恢复推进。推进被跳过或失败的暂停由 advance 的 paused 守卫保留；`set_text` 必须在状态恢复回 queued 之后调用（hold 中的条目不可编辑）。丢弃/重新入队/恢复的正文保存失败一律回滚内存（丢弃不回滚 = 提示"未保存"而条目已消失、磁盘还在，实测踩过）；**编辑退出时恢复状态的那次保存失败同样要暂停、提示并禁止自动推进**——只回滚不暂停的话 worker 照常 1→2（实测踩过）。编辑流程中各次必要保存的结果统一检查、失败即暂停：**入口保存（queued → held）失败 = 恢复可操作的原条目 + 暂停 + 退出编辑（不打开对话框）**，退出恢复失败与正文保存失败 = 暂停 + 提示 + 禁止推进（三处都实测过：漏掉任何一处都是 worker 1→2 照常执行）。needs_check 的"重新入队"是**替换**：先删旧条目腾出容量再新增（满队列时先新增会被容量检查拒绝、恢复项永远换不进去），一次落盘，失败按原位恢复旧项。
- **按状态定义操作**：`EDITABLE_STATES`（仅 queued）、`DELETABLE_STATES`（queued/held/needs_check——held 删不掉、needs_check 丢弃不掉的话，旧按钮会一直生成副本，实测踩过）、派发与运行中一律锁定。容量按**待处理**计（`pending_count`/`pending_chars` 排除 done——done 只留作结果记录，否则处理满 20 条后队列永久满员）；编辑按**替换后**的总量校验（只查单条会被编辑撑破总量，实测 99,900 → 119,500）。
- **遥控共用入队服务**：`_on_remote_submit` 运行中 → 入队（回执"已排队"），空闲直发，收尾窗口走 B09a 待启动请求；`telegram_poll._dispatch` 不再以"正忙"拒绝。回执区分已排队/未保存/未接收/启动失败。
- **测试**（`scripts/test_b09b_input_queue.py`，34 条；3 组变异验证 + 复核修复 5 条回归确认能抓旧缺陷）：真新进程崩溃窗口用子进程脚本（child 里同样阻断数据根越界写入）；UI 用例用带 B03 记录的 shaped loop（`_worker_data_root` 把主线程数据根带进 worker——**必须在主线程取根再传参，进线程再取就是默认根**）+ autouse 写路径守卫；面板按钮按 `queue-row-<item_id>` objectName 定位，编辑对话框独立成 `_show_queue_edit_dialog` 便于整只替换。造数注意：入队/保存都需要会话历史 ≥2 条（否则 save 按"历史过短"跳过、无 ID）。
