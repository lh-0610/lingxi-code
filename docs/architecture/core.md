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
- **内容区卡顿（2026-11-23 实测，真实渲染、用户最长的会话）**：
  - **流式按段落拆块**（`AssistantTurn.append_body`）：以前整段回复塞进一个 BodyText，每来几个字 `setText` 全文、Qt 把整段富文本重排好几遍——九千字时每段 144ms，越写越卡。现在按空行拆，一段一个正文块（`_stream_parts`，块间留 `_STREAM_PARA_GAP`），只重设最后两段（文字没变不重设）；`finalize_markdown` 用 `_drop_stream_parts` 拿掉多出来的块、合回第一块写渲染好的 markdown。没走 finalize 就结束的（停止 / 报错）分段留着照样能看。「按正文块认」的逻辑（收起过程、最终回答、线上节点）都在回复结束之后才跑，那时已经合回一块
  - **BodyText 自己缓存「这个宽度多高」**（`_hfw_cache`，最多 6 个宽度）：QLabel 不缓存，布局一轮要问好几次、还按不同宽度问（实测流式时前面写完的每一段每次都被问 782 / 266 两个宽度）。`setText` / 字体 / 样式 / 内容边距变了就清。九千字每段 144ms → 14ms
  - **切会话清旧消息**（`MessageView.clear`）：藏起来 + deleteLater，不再先 `setParent(None)`——Qt 会把每一轮临时变成顶层窗口，长会话光这一步 140ms（挪到另一个藏着的父控件下也一样慢）。删之前它们还挂在 `_content` 下：搜索、定位步骤用 `_alive` 跳过 `_gone` 里的。清掉 140ms → 8ms，删挪到下一轮事件循环（约 75ms，那时新会话已经画出来）；打开长会话 1.05s → 0.93s
  - **打开长会话（2026-11-24，同样两个最长的会话，计到排版全部结束）**：每次 1.12–1.26s → 0.68–0.73s。几处：
    - **各轮藏着建、最后一次显示**（`MessageView.batch`，`_redraw_chat` 包着它）：往显示着的列表里加控件，每个子控件的显示都排进事件队列、各自触发排版，还要过一遍全局过滤器；藏着建，结束时 `show()`，子控件随父控件同步显示、整列只排一次版。收起最后一轮也挪进批里做（藏着收）。期间判断显隐只能用 `isVisibleTo`（相对本轮），`isVisible()` 在批里永远是假
    - **过渡回复不建复制按钮**（`MessageView.retire_message_actions`）：重画时一条 AIMessage 有正文又有工具调用，挂上的按钮马上就被后面的工具卡拿掉（`_drop_dangling_actions`，`setParent(None)` 一次约 1ms）。不建，只做建了再拿掉会留下的效果：上一条回答的按钮不再常显
    - **样式属性变了才 unpolish / polish**（`_set_prop` / `_set_style_prop`）：工具卡每次 `_sync` 都给状态和行头各重套一遍样式，两个会话 1830 次
    - **工具卡上平时用不上的五样用到才建**（`_ensure_script_info` / `_ensure_stats` / `_ensure_clock` / `_ensure_tail` / `_ensure_files`）：「脚本 N 行」、增删行数、走秒、运行中最新一行、改动的文件。插入位置按固定顺序找邻居，不靠建卡时占位
    - **`_icon` 按（SVG，颜色，尺寸）缓存**，返回副本（QPixmap 隐式共享，调用方在上面画东西不改缓存）
    - **重画期间文件链接的路径只查一次**（`markdown_render.cached_paths`）：每处行内代码都要 realpath + isfile，Windows 上 realpath 每次都开文件；缓存只活在这一次重画里，实时回复照常每次查（刚写出的文件要认得出）。两个会话 328 次 → 80 次
    - **任务面板主题没变不重设样式表**（`TaskPanel.apply_theme` 按主题色和浮层状态记一个键）：`render_session` 每次切会话都调它，十几次 `setStyleSheet` 约 30ms
    - 剩下的大头是控件本身的创建和那一次排版（收起的轮也照样建工具卡）；要再省得改成展开时才建，牵涉搜索、定位步骤、线上节点，没做
  - **改窗口大小**：空白页没显示就不摆它（以前每次改大小都把推荐按钮的图标重画一遍）；`ChatUI._svg_icon` 按（文件，颜色，缩放比例）缓存，不再每次读盘渲染；`_ElidedLabel.sizeHint` 的文字宽度按（文字，字体）记着。拖窗口每步 80ms → 68ms，剩下的是整个窗口的布局和绘制，以及应用级右键菜单过滤器每个事件一次 Python 回调的固定开销
  - 回归在 `scripts/test_content_perf.py`：不比毫秒，比「该省的那份工作真的没做」「省了之后显示照样对」；每处都做过突变检验（把优化改回去，测试要挂）
- **`_settle_layout` 连子布局一起失效**：`updateGeometry` 只让父控件的**顶层**布局失效，嵌在里面的子布局（气泡里的引用盒 `_quotes_box`）还缓存着旧高度，第一遍按旧的排、Qt 激活时才顺手清掉，第二遍才对——引用块展开时第一次调用整列纹丝不动（实测）。所以链上每个布局的子布局先 `invalidate`（`_invalidate_sublayouts`）
- **输出只存成文字，点开那一行才建输出框**：`ToolCallCard._lines`（最多 4000 行），展开时 `_build_body` 才建参数标签和 QPlainTextEdit；展开区和行头的底色左边对齐（`_BODY_INDENT = 0`，设计稿原是缩进 31 和动作文字对齐，展开后行头整条有底色、框缩进去一截看着像错位，用户实测后改），参数文字和框里的字对齐；最后一行输出 / 改动文件那两行小字没有框，仍缩进 31。早先每张卡都预建一个藏着的输出框，重画长会话时这一项占卡片耗时的三分之一。Ctrl+F 要搜到工具参数：`find_text` 先给参数里有关键词、还没建的行补建（`ensure_body`）。运行中的行图标换转圈、右边走秒（超过 2 秒才出，快的不闪），下面露最新一行输出；跑完行上不留用时（总用时在组头）
- **交了脚本的命令**（设计稿「消息区 · 运行脚本的卡片」）：`run_command` 带 stdin（`python -` + 一段脚本）时，`describe_tool_call` 在命令和脚本之间夹 `streaming.SCRIPT_MARK`（换行 + U+2063 不可见分隔符：不算空白也不算换行，`strip` / `splitlines` 都不动它；命令本身也可能多行，不能按第一个换行切）。重画历史也走 `describe_tool_call`，旧会话一样认得。工具卡据此分开：头上命令 + 浅色「脚本 N 行」（命令的 `_ElidedLabel` 这时按完整文字报 `sizeHint`、不占满，「脚本 N 行」紧跟其后；以前按省略后的文字报，按想要的宽度排就拿到 0 宽）；展开区「脚本 · Python」+ 脚本框（`QPlainTextEdit#toolScript`，和输出框同一套 QSS / 字体 / 定高：最多 16 行，多的在框里滚），有脚本时输出框上标「输出」。着色走文件面板那套 `syntax_highlight.highlight_rows`（tree-sitter），按行套进 `_ScriptHighlighter`；语言按解释器名认（`_script_language`：python / python3 / python3.14 / py / 带路径和 .exe → Python，node → JavaScript，其余不着色）。以前脚本和命令拼成一段塞进参数标签：「python -」写两遍、按 JSON 规则上色（只认数字和括号）、行距 1.7 像隔行（用户截图）。Ctrl+F 也搜脚本框：没展开过的先 `ensure_body`，命中用框自己的选区 + `ensureCursorVisible`（长脚本在框里滚到命中处）。输出框 / 脚本框要滚动时定高不再留 4 像素余量（余量只防「全放得下」时最后一行被切），不然框底露出第 17 行的上半截
- **样式一张表**（`mv_qss()`，挂在 MessageView 上，`apply_theme` 时按当前主题生成）：控件只打 objectName 或 `mv` 属性，不各自 setStyleSheet——每次都要解析 CSS、重算这一枝，一张卡十几次。范围用 `QScrollArea[mvRoot="true"]` 限定，**不用 `#messageView`**：ChatUI 把它的 objectName 改成了 chatArea，按 objectName 写的规则一条都不生效（实测：气泡没底色、按钮成了默认样式）。内容区原来挂着一条不带选择器的 `background`，会级联到每个子控件，于是每个子控件又写 `background:transparent` 盖回去——一并去掉。改前改后逐像素比对过（浅色 / 深色、收起 / 展开 / 底部），完全一致
- **一轮里各块的分寸**（设计稿「消息区：消息流整理」）：
  - 用户消息的编辑 / 回退 / 分叉放在气泡右边同一行（`UserTurn.set_actions`），鼠标移到这条消息上才出现；回答下面的复制 / 重新生成**只有最新一条常显**（`MessageView._latest_actions`），更早的移到那段回复上才出现；**过渡回复不挂**——一段文字后面又接了工具调用 / 思考 / 提示，就把它底下那排拿掉（`AssistantTurn._drop_dangling_actions`，实时中间轮和重绘历史都会先挂上），不然藏着也占位的那排成了中间回复和工具行之间的一块空白（用户实测）。显隐都走 `_HoverButtons`：按钮藏起来也占着位置（`setRetainSizeWhenHidden`）——不然长消息折行时按钮一出现气泡就被挤窄重排，回答下面那排一出现后面的内容就往下跳
  - 继续任务 / 中断恢复的说明走 `recovery_note` 消息（`run_results` 的「继续任务 · 使用模型…」、`recovery.prepare_and_apply` 的说明、重画时 `_recovery_note_text`），画成 `RecoveryNote`：线上一个小圆点 + 一行灰字，现场变化这些细节收进「详情」。实时继续时这两段紧挨着到，并成一条（标题相同的那句不重复）。以前是通栏灰底的程序提示，比用户自己的消息还显眼。其它程序提示（重试、拒绝调用）仍是 `Notice`
  - 收起的过程是一行（`_ProcessSummary`，整行是按钮），行尾箭头朝右 = 收着
  - **展开 / 收起当场排完版**（`_settle_layout`，组头、过程摘要、单条工具、思考块、恢复说明的「详情」点完都调）：Qt 从里往外排，里层先按外层还没变的旧高度排——收起时多出来的那截平均分给兄弟块，用户气泡瞬间被拉高几百像素再缩回（用户实测「闪一下」）；展开时最后那段回答先被挤扁。做法：由里往外 `updateGeometry`（清父布局里缓存的子块尺寸、标成要重排，光 `invalidate` 不清这份缓存，`activate` 会原样返回），消息列表直接改到新高度，再由外往里 `activate`。测试看「点完返回之后各块尺寸不再变」，Qt 在 `setVisible` 里同步出现的中间态画不出来，不算。**宽度变化：列表限宽、由滚动区居中摆，尺寸自己定**（`MessageView.fit_content`）。原来按视口宽度改左右边距：先排版后改边距，收起右侧面板那一帧整列按旧边距铺到右边、打开时被挤成窄条（用户实测「闪一下」）；而且每次缩放都把整列重排一遍——长会话开关侧栏 / 浏览器卡顿（用户实测「帧数很低」），同一段对话 12 轮一次 66ms、40 轮 203ms。现在：边距固定 `COLUMN_SIDE`，列表宽 = min(视口, `COLUMN_MAX` + 两侧)，`setAlignment(AlignHCenter)` 居中；窗口比栏宽时缩放只是平移、不重排（约 4ms，与对话长短无关）。不用 `widgetResizable`：它定高时按**视口宽度**算排版高度（列表比视口窄，算出来差十几像素，下一轮才补，贴底时跳一下）。`_Content.sizeHint` 直接报当前尺寸：默认实现按「建议宽度」把整列高度再算一遍，滚动区每次缩放都问，布局只缓存一个宽度的高度，两个宽度来回算互相冲掉（单这一项 4ms → 12ms）。`fit_content` 在视口 Resize（含滚动条出现 / 消失）和列表 LayoutRequest 时当场调
  - 线上的节点：用户消息空心圆、**最终回答实心圆**（`_Round.answer_body`，绘制时现找：最后一段回复的最后一个正文块，跳过那排按钮；这一轮不是以回答收尾——停在工具上、后面还有提示——就不画，过渡回复不能标成回答）。竖线从第一个节点画到这一轮内容的底（`_content_bottom`，不算藏着也占位的按钮排），离最后一个节点够远（≥24）时底端收一个小空心圆
  - 轮与轮之间一条很淡的分隔线（`#roundSep`，从线槽右边起；只加在两个 `_Round` 之间）。线上方少空 10：上一段回答末尾那排按钮藏着也占位
  - 回答正文（`markdown_render._md_to_html`）：段落只留下边距 8；标题换成带字号的段落（Qt 给 `<hN>` 套自己的字号级别，style 里的 font-size 盖不过，h1 是 xx-large），上 18 下 6，回答以标题开头时上边距归零；列表是两列无框表格（符号一列、内容一列），折行和文字对齐，嵌套列表落在外层那项的内容列里；行内代码用正文色 + 很浅的底（`md_code_*`），文件链接用强调色、无下划线（Qt 默认的纯蓝下划线在深色底上几乎看不清）
  - **引用**（设计稿「消息区：引用」，`ui/quotes.py` + `ui/quote_input.py`）：选中消息流里的文字，松开鼠标后选区上方浮出「引用 · 复制」（`quotes.SelectionBar`，挂在 MessageView 上；由 `context_menus` 的全局过滤器在 MouseButtonRelease 时叫出来——借已有的过滤器，不另装一个；只认 MessageView 里的只读文本，按下鼠标 / Esc / 右键菜单 / 滚动就收）。右键「引用到输入框」、网页里右键「把选中文字发到输入框」走同一个入口 `ChatUI.context_menu_quote`：**不再往输入框里塞「> 」文字**，而是输入框上方一张引用卡（`QuoteCard`，最多两行、× 去掉，最多 `MAX_QUOTES`=5 张，同一段不重复）。发送时 `_compose_input` 把卡片和打的字拼成 Markdown 引用（`quotes.compose`：每行「> 」、段与段 / 引用与正文空一行）——发给模型、进历史、进运行中队列的都是这一份；只引用不打字也能发；接纳后卡片和文字一起清（`_clear_sent_input` 比的是拼好的那份）。用户气泡按**开头**连续的「> 」行认引用（`split_leading_quotes`，正文中间的「>」原样留着），画成 `QuoteBlock`：小一号次要色 + 左边一条细线，最多三行、末尾省略号（`ClampText` 自己按行排版截断，QLabel 只能截高度、切半行），悬停看全文；以前右键塞进去的旧格式（「> …」+ 两个空行）重画时也一样认。实时发送（`append_user_text`）和重画历史（`handle("user_msg")`）同一套渲染（`UserTurn.set_text`，@文件 加粗也在这）。线上的节点锚在气泡上（不是正文：只有引用时正文藏起来，锚在正文上节点就没了）。编辑重发 / 编辑排队条目用的是对话框，里面引用仍是「> 」文字，可直接改
  - **引用收尾**（设计稿「消息区 · 引用收尾」）：**排队面板**的一行按 `split_leading_quotes` 把引用和正文分开写（`queue_panel._QueueText`）：一条细线 + 次要色的第一段引用（多段跟「等 N 段」），后面是正文；引用最多占一行的四成（没写正文时占满），悬停整行看全文。原来整段压成一行，「>」露在外面、引用和问题连成一句。子控件在 `resizeEvent` 里自己摆、不用布局——引用宽度取决于行宽，放进 QHBoxLayout 时布局先按旧约束排完，resizeEvent 里再定宽已经晚了，正文和引用叠在一起（实测）。**Ctrl+F 也搜引用块**：`find_text` 一次 `findChildren(QWidget)` 同时收可选中的 QLabel 和 `ClampText`（先序深度优先，顺序就是显示先后）；引用块里的命中由 `ClampText.show_hit` 画选区色（调色板 Highlight / HighlightedText，和正文命中的 QLabel 选中同色），命中在收起看不见的行里时临时展开到全文，跳到别处 / 关掉搜索（`clear_hit`）收回三行，变高变矮都走 `_settle_layout` 当场排好
  - 网页链接（设计稿「消息区：回复里的链接」）：正文里光秃秃的网址变成链接（`_autolink_bare_urls`，在 markdown 转出来的 HTML 文本节点上做，链接 / 行内代码 / 代码块里的不动；末尾的中英文标点、不配对的右括号、转义实体不算网址，`_trim_url`）——以前只认 localhost，公网地址点不了，写成 Markdown 链接点了也没反应。样子（`_style_web_link`）：强调色 + 浅一档的下划线（`text-decoration-color`，Qt 富文本画不了虚线，`text-underline-style` 不认）+ 箭头，公网 ↗、本机 ▸；模型写了标题就显示标题，光秃秃的网址显示成「域名/路径前段…」（`short_url`，最长 36 字），写成行内代码的保留代码样式（复审 12）。提示词要求模型写带标题的链接。悬停（`BodyText._show_link_tip` / `link_tip`）给完整网址和「点了会怎样」。左键（`ChatUI._on_file_link_clicked`）：本机地址总在侧栏浏览器开；公网按设置 `link_open_mode`（设置 → 高级，立即生效），默认系统浏览器——侧栏浏览器播不了 H.264 视频。右键（`context_menus._link_items`）两种打开方式都给，外加复制地址 / 复制链接文字
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
- **token 估算按真实用量拟合**（`_text_tokens` / `_tools_overhead_tokens`，2026-11-24）：8 次运行、120 多轮，拿账本里服务商报的输入 token，对会话文件重建出的每轮历史拟合，得中日韩 0.91 token / 字、其余 0.242 token / 字符、每轮固定约 1.36 万（误差 ±8%）。固定那份就是**工具定义**：内置 53 个工具的 schema 约 1.1 万，加上 MCP 工具对得上，以前完全没算——历史预算和回收线都按「只算消息」比，按 20 万预算回收时真实请求其实才 15 万出头（代码部分又估高约四分之一）。现在系数取 0.95 / 0.26（往偏多留一点），`_tools_overhead_tokens` 按当前会话实际绑定的工具（知识库模式 / 只读子代理绑的少）算 schema 的 token、按工具名缓存，`_current_history_budget` 和 `_trim_watermarks` 都扣掉它（回收线指整个请求，和服务商算的同一个数）；回收提示里的「约 Nk」也加上它。新估算 + 工具开销对 8 次运行全部落在偏多一侧（+5% ~ +17%）。回放现有 22 个会话 945 轮：行为不变（0 断裂、0 回收），估算总量降约 3.7%
- **回收线是设置项**：`config.CONTEXT_TRIM_TOKENS`（config.json 的 `context_trim_tokens`，设置弹窗「高级」页可改，默认 20 万，保存即生效；0 = 跟历史预算走；非 0 的值低于 2 万按 2 万算）。设得比 `MAX_HISTORY_BUDGET`（20 万）高时，`_current_history_budget` 的上限跟着抬到回收线——不然线设在 40 万、预算却在 20 万就开始压缩，那条线永远到不了；再高也不超过模型窗口，所以只对 DeepSeek 这类 100 万窗口的模型有意义
- **线设低是用钱换短上下文，不省钱**。用真实实现回放那五个任务（不回收时峰值 10–17 万）：10 万 → 五个都触发 1–2 次，成本合计 +5.9%（单个 +2%～+19%），两个任务里共 7 次改文件前读取已被折掉；12 万 → +5.6%；15 万 → 只有最长的两个触发、但各 +13%，合计 +6.6%（触发得晚，剩下的轮数不够回本）；18 万以上 → 一次不触发。每次触发要按未命中重付留下的那一半，之后每轮少带的部分按命中价算，要四十来轮才回本。上下文短了模型是不是更好，没有数据。所以默认和历史预算取齐：平时不碰，只拦住会撞预算的超长任务。估算不含工具 schema，实际输入约多 1.5 万
- **折过哪些随会话文件落盘**（`evicted_tool_results`，只留历史里还在的 id）：重开程序接着跑，发出去的前缀和关掉之前一样。旧会话没有这个键 → 当成没折过。新对话清空
- **合成 tool_call_id 必须会话内唯一**（`streaming._normalize_tool_calls`）：部分本地模型不返回 tool_call id，兜底用 `call_<name>_<uuid4 前 12 位>`，**不能用每轮重置的计数器**——回收按 tool_call_id 记且随 `evicted_tool_results` 落盘，跨轮重复的 id 会让新一轮的结果顶着旧 id 被当成"已回收"误折，哪怕它就在最近 keep_recent 条里
- 之后两步只在回收折不动、仍超出历史预算时才轮得到，仍是无状态的：`_cap_oversized_tool_results`（单条超 2.4 万字截头尾）、`_compact_history`（LLM 把中段压成摘要，留最近 20 条）。全程只动发送副本，`state.chat_history` 和界面保留完整历史
- 回归守卫在 `scripts/test_tool_eviction.py`（`test_prefix_stays_identical_between_trips`）：和 system prompt 那条一样，破坏之后一切照常工作，只是每轮开始按未命中计费
- **改发送流程 / 工具输出 / 回收逻辑后，跑一遍离线回放**：`python scripts/replay_sessions.py`（可传会话 ID 或 `--dir`，默认本地 `chat_memory/`）。它把已保存的会话按轮截到"模型即将发出第 N 条 AI 消息"那一刻，走**完整**的 `_prepare_stream_history`（不是只测 `_trim_tool_results` 一步），逐轮报告估算 token、与上一轮第一个不同的位置及原因、是否回收、是否会触发压缩，末尾汇总断裂次数与折算成本。前缀比对复用 `cache_fingerprints`、成本折算复用 `run_ledger` 的单价配置，都不另写一份。**不联网、不写盘、不改 chat_memory**：日志/配置/数据根指到临时目录、MCP 打桩、压缩要调的 LLM 换成占位实例（只报告"这一轮会触发压缩"，不真的压）。**成本是近似**：历史上每轮的 system 和易变运行态无法复原，用的是当前值，且不注入项目上下文/规则/长期记忆；命中按"与上一轮相同的前缀"估算，不是服务商真实上报值——用来看改动前后的**相对退化**。合成会话的回归在 `scripts/test_replay_regression.py`（几十轮、能触发几次回收，断言两次回收之间前缀不断、回收后下一轮起恢复稳定、只有末尾易变消息变化不算断裂；真实会话不进仓库、测试不读 `chat_memory/`）

## 设置热更新（config.py + config_apply.py + ui/config_watch.py）
- **改设置不用重启**：设置对话框点保存、手改 config.json，改动都当场生效。唯一例外是「使用系统标题栏」（`NATIVE_TITLE_BAR`，窗口建的时候就定了），只有它变了才弹「立即重启 / 稍后」
- **单一真相源是 config 模块，用的时候读**：`config.reload(data=None)` 重新执行 config.py（`importlib.reload`，默认值、非法值回退、告警和启动时是同一份代码），同一个模块对象上就地重算全部常量，返回值变了的常量名。所以别的模块要 `from . import config` 后读 `config.X`，或在函数里 `from .config import X`；**模块顶层 `from .config import X` 会把启动时的值抄走，热更新到不了**（models / notify / telegram_* / claude_code / lsp_client 原来就是这样，已改）。测试要改配置就 patch `config` 模块的属性，patch 各模块里的副本已经不起作用
- **读到一半的文件不清空配置**：`reload` 先自己读、校验（`config.read_file`，读不了 / 不是对象返回 None，调用方保持现状），再把内容通过 `_RELOAD_SEED` 交给重新执行的模块用，不读第二遍——两次读之间文件可能正被写一半，原来的启动代码读坏了会退成空配置、把密钥全清掉。设置对话框保存改成写临时文件再 `os.replace`
- **`config_apply.apply(changed)` 只管启动时定下来的东西**：其余配置本来就是用的时候读，reload 完就生效
  - 密钥 / 端点 / 各家模型清单 / 自定义模型变了（`_affects_models`：`*_API_KEY` `*_BASE_URL` `*_MODELS` `CUSTOM_MODELS`，排除 `RAG_` `WEB_SEARCH_` `BOCHA_`）→ `models.refresh_model_list()` **就地**重建 `BUILTIN_MODEL_LIST` / `MODEL_LIST`（`from .models import MODEL_LIST` 抄走的是同一个 list，换对象的话 agent 那边还指着旧表）并清 `_LLM_CACHE`；返回旧下标 → 新下标，按「类型 + model_id，显示名也同的优先」对；每个打开的会话的 `current_model_index` 对到新列表，模型被删掉的退回默认模型并报给界面；清 `agent._BOUND_LLM_CACHE`、重新激活当前模型。缓存键里没有密钥 / 端点，不清的话改了 key 还用旧客户端
  - 打码表 `secret_mask.refresh`，新填的 key 立刻进表
  - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `REMOTE_CONTROL` 变了 → 遥控轮询停掉重起。旧线程多半卡在 30s 长轮询里退不掉，轮询线程带**代数**（`telegram_poll._gen`），`shutdown` 让代数 +1，旧线程醒来发现不是自己的代就退，拿到的那批更新不处理，不会和新线程把同一条消息执行两遍
  - `MCP_SERVERS` / `MCP_ENABLED`（新加的常量，原来总开关只有 mcp_client 自己读文件）变了 → 后台线程 `mcp_client.restart()`：关掉旧连接、清掉上一轮的工具表 / 就绪信号 / 显示名，按文件重连，再清绑定缓存让模型拿到新工具；连续改两次排队（锁），最后一次读到的是最新配置
  - `LSP_SERVERS` / `LSP_LANGUAGES` 变了 → 关掉正在跑的语言服务器，下次查定义按新配置起
- **界面这一半**（`ConfigWatchMixin`）：设置对话框保存后自己调 `config_apply.reload_and_apply`，主窗口拿 `applied_result` 刷新模型按钮、提示「设置已保存，已生效」；模型被删、当前会话换了模型时提示换成了哪个。`QFileSystemWatcher` 同时看 config.json 和所在目录（写临时文件再替换会让文件从监视里掉出去，目录事件里加回来），只在 config.json 的 mtime 变了时等 400ms 重读；写坏了提示「格式不对，这次改动没有生效」、配置不动。两条路会撞（对话框保存也触发监视），重读比较的是值，第二次是空操作
- **已知边界**：正在跑的会话，其模型被删时下一轮就换成默认模型；主线程重建列表和 worker 读 `MODEL_LIST[下标]` 之间没有锁，窗口极小，不处理。`default_model_id` 只管启动时选哪个，改了不切换当前会话
- 测试 `scripts/test_config_hot_reload.py`（突变验证过：不用种子、列表换对象、不对下标、不清绑定缓存、旧轮询线程照收、保存不重载、监视不触发、总开关不看、重连不清旧表、标题栏不要求重启，各自都有用例挂）；`scripts/conftest.py` 的自动夹具在测试触发了热更新后把 config 和模型列表还原

## 主循环边界与错误恢复（src/agent.py + llm_errors.py）
- **轮次上限** `config.AGENT_MAX_ROUNDS`（config.json 的 `agent_max_rounds`，设置弹窗「高级」页可改，默认 50；**设 0 = 不限**）：主循环原本是裸 `while True`，模型只要一直返回 tool_calls 就一直跑，唯一刹车是用户点停止。触顶后停下并把结论**写进 chat_history**（只弹 UI 的话，用户接着发消息时模型看不到自己被截断，会以为任务做完了）
- **错误分类**（`llm_errors.classify`）：原来所有异常一视同仁地退避重试，对三类是错的——**上下文溢出**重试必然再溢出；**鉴权失败**等再久 key 也不会变对；**限流**服务端通常在 `Retry-After` 里给了确切秒数，盲目 2^n 要么继续撞墙要么白等。分类不 import 任何 provider SDK（各家异常类型不同、自定义模型协议还是用户配的），改用三层判据：状态码 → 异常类名 → 消息特征词，认不出一律退化成 UNKNOWN（= 保持原行为），宁可漏判不可误判
- **溢出恢复**：provider 报超窗 → `_stream_chunks_with_retry` 抛 `ContextOverflowError` → 主循环把会话的 `overflow_squeeze` +1（`_current_history_budget` 按 1/2^n 收紧、8k 地板）→ 压缩后重发，封顶 `_MAX_OVERFLOW_RETRIES=3`。**不在成功后清零**：溢出说明对该模型窗口的估算偏乐观，这个教训该在本会话内保持，否则每轮都要重新撞一次墙
- **停止不等网络**：`_stoppable_stream` 在旁路线程里拉流，worker 每 0.2s 看一次停止标志。原来 worker 直接迭代 `llm.stream()`，模型迟迟不回 chunk 时卡在网络读上（请求超时 30 分钟），停止后转圈一直走秒、旧 worker 不退，下一条消息排队 15 秒就放弃。旁路线程和 worker 绑同一会话与数据根，停止后它等到下一个 chunk 或请求结束自己关连接退出；重试退避也按停止标志提前结束。界面侧 `_force_stop_generation` 当场清掉「等待响应」指示器
- **排队提示**：DeepSeek 高峰期会让请求排队最多 10 分钟，期间只发 SSE 注释 `: keep-alive`（实测 `/responses` 和 `/chat/completions` 都是每 12 秒一次，SDK 不产出 chunk）。`WaitingIndicator` 按心跳报的阶段区分「等待响应」（一个 chunk 都没有）/「模型思考中」/「正在生成工具调用 X」，只有前者满 30 秒才在下面加一行「服务商可能在排队」。原来三种阶段界面上都显示「等待响应」，长的 write_file 参数生成会被误认成卡住
- **跑测试能停、工具卡走秒**：`run_tests` 原来用 `subprocess.run` 一直阻塞到进程退出，全量测试一跑 9 分钟，期间点停止没有反应、卡片上也一动不动。现在走 `_run_test_process`：`communicate` 每 0.2s 超时一次看停止标志（重试不丢输出），停止就杀进程树，结果如实记成「用户停止了测试，没有跑完」（证据 `cancelled`，不算通过也不算失败）。界面侧 `ToolCallCard`（一行一条）实时运行时走秒，超过 2 秒图标换转圈、右边「运行中 N 秒」，结束后行上不留用时、算进组头的总用时；重绘历史不走秒，点停止当场定格。跑着的测试同时登记进「后台任务」面板（见 background.md），能看实时输出、单独停
- **只记进度的一轮补一句提醒**：`agent._run_tool_calls` 执行完一轮，若整轮只调了 `set_step_status` / `update_plan`，在最后那条工具结果后面补一句"之后把进度更新和这一步的第一个动作放在同一轮发"。密钥打码那次 68 轮里有 7 轮是这样；系统提示词早就要求同一轮多发调用，DeepSeek 照样单发，对刚收到的工具结果更上心。提醒只进历史末尾（不碰缓存前缀），界面上的结果不变
- 子 Agent 递归深度天然封顶：`spawn_agents` 里 `is_subagent` 的会话不许再派生
