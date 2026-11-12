"""Shared limits for conversation, tools, and debug views."""


# 侧栏每个分组（项目 / 无项目 / 知识库）初始显示的会话条数，超出部分通过组尾的
# 「加载更多」按钮按步长追加。这两个数字**只影响界面展示数量**——会话历史
# （正文 + 索引条目）永远全量保留，删除只能由用户主动发起，与它们无关。
SIDEBAR_GROUP_VISIBLE_INITIAL = 30
SIDEBAR_GROUP_VISIBLE_STEP = 30

# agent 主循环单次交互的最大轮数**默认值**（一轮 = 一次模型调用 + 它请求的工具执行）。
# 实际生效值是 config.AGENT_MAX_ROUNDS（config.json 的 agent_max_rounds 可覆盖，
# 设置弹窗「高级」页可改；**设 0 = 不限轮次**）。
# 没有上限时主循环是裸 while True：模型只要一直返回 tool_calls 就一直跑，唯一的刹车是
# 用户点停止。小项目里的编码任务 10~20 轮就够，但在几百个文件的真实仓库里（评测 011：
# 灵犀修自己的启动崩溃，DeepSeek V4.1 Flash）正常要 60~70 轮，原来的 50 会把做到九成的活
# 拦下。100 留出这段余量；真打转由 agent._repeating_tool_call 认出来，触顶时说法不同。
AGENT_MAX_ROUNDS_DEFAULT = 100

HISTORY_TOKEN_BUDGET = 80_000

# M3: 按模型上下文窗口动态计算预算时的参数
HISTORY_SAFETY_MARGIN = 8_000
MAX_HISTORY_BUDGET = 200_000
HISTORY_KEEP_RECENT = 20

# 上下文回收线的**默认值**（估算 token）。实际生效值是 config.CONTEXT_TRIM_TOKENS（config.json 的
# context_trim_tokens，设置弹窗「高级」页可改；设 0 = 跟历史预算走）。
# 发送历史涨过这条线，就一次性把旧的大工具结果折成存根、降到它的一半，折过的记在会话里，之后每轮
# 原样照发。一次折一半是为了少触发：每触发一次，前缀缓存从最早那条被折的结果往后全部重建
# （DeepSeek 未命中比命中贵 50 倍），要四十来轮才回本。
# 线定在哪是钱和上下文长度的取舍，回放五个 68–115 轮的真实任务（不回收时峰值 10–17 万）：
#   10 万：五个都触发 1–2 次，成本合计 +5.9%（单个 +2%～+19%），峰值压在 10 万内；
#   12 万：四个各触发一次，合计 +5.6%；
#   15 万：只有最长的两个触发，但各 +13%，合计 +6.6%——触发得晚，剩下的轮数不够回本；
#   18 万以上：一次不触发，成本不变。
# 也就是在这种长度的任务里，只要触发就是净亏，线低只换来上下文短（短了模型是不是更好，没有数据）。
# 所以默认和历史预算上限（MAX_HISTORY_BUDGET）取齐：平时不碰，只拦住会撞预算的超长任务；
# 想要更短的上下文、或者在 100 万窗口的模型上放得更长，自己在设置里调。
CONTEXT_TRIM_TOKENS_DEFAULT = 200_000
# 设得太低会几轮就触发一次、每次都重建缓存；非 0 的值低于这个数按这个数算
CONTEXT_TRIM_TOKENS_MIN = 20_000

# 工具结果回收（M2）的参数：哪些结果能折、存根留多长
TOOL_RESULT_EVICT_KEEP_RECENT = 6      # 最近 N 条工具结果保持完整（不回收）
TOOL_RESULT_EVICT_MIN_CHARS = 1000     # 只回收内容超过这个字符数的旧工具结果（小结果不值当）
TOOL_RESULT_EVICT_PREVIEW_CHARS = 150  # 存根保留的开头预览长度

# 单条工具结果硬上限（M4）：超预算时,任何工具结果(含最近的)超过 cap 就截成 头+尾,
# 防一串大结果堆在最近、躲过回收和压缩、撑爆预算
TOOL_RESULT_HARD_CAP_CHARS = 24_000    # 超过这个字符数就截断（≈17k token；600 行左右的文件以内不动）
TOOL_RESULT_HARD_CAP_HEAD = 12_000     # 保留开头字符数
TOOL_RESULT_HARD_CAP_TAIL = 6_000      # 保留结尾字符数

STREAM_RETRY_ATTEMPTS = 3

READ_FILE_DEFAULT_LIMIT = 2000
# 一次读文件最多返回这么多字（按行截，末尾提示用 offset 接着读）。读进来的内容每轮都要重发：
# 任务八一次 read_files 把 markdown_render.py 整个读进来约 2.6 万 token，之后九十多轮每轮都带着它。
# 3 万字约是灵犀代码的四五百行，够看一段完整的实现；要看别处用 offset 跳过去。
READ_FILE_MAX_CHARS = 30_000
SEARCH_IN_FILE_DEFAULT_LIMIT = 50
SEARCH_IN_FILE_MAX_LIMIT = 200
SEARCH_FILES_MAX_RESULTS = 50
# search_files 每处匹配默认带前后几行；显示的匹配超过这个数就只列匹配行，免得结果撑得太长
SEARCH_FILES_CONTEXT_MAX_HITS = 20
# 搜索结果里单行最多显示这么多字。11-03 两次搜项目根各交回 3 万字（约 1 万 token，之后每轮都带着），
# 其中 95% 是会话 JSON 里一行两万多字的 "content"——要的只是"哪个文件第几行有"。
SEARCH_LINE_MAX_CHARS = 300
# search_files 一次最多交回这么多字，超了按行截断并说明还有多少处没列
SEARCH_FILES_MAX_CHARS = 8000
# git_diff 不指到单个文件时（整个项目 / 一个目录）一次最多交回这么多字，max_chars 传得再大也不超过：
# 模型常传 2–3 万把全部改动再看一遍，内容和它自己发出的改动重复，还会一直留在上下文里。
# 指到单个文件不受这个限制——那是看全一个大改动的唯一办法。
GIT_DIFF_OVERVIEW_MAX_CHARS = 8000

RUN_COMMAND_TIMEOUT_S = 300
RUN_COMMAND_MAX_OUTPUT_CHARS = 5000
TOOL_RESULT_PREVIEW_CHARS = 500
# 后台命令注册表里"已退出"的进程最多保留几个（仍可被 read_background_output 读最终输出）。
# 超过则淘汰最老的已退出项，防长会话里崩溃/跑完的后台任务连同 2000 行输出 deque 无限驻留。
# 运行中的进程从不被淘汰。
BG_MAX_RETAINED_EXITED = 10

# B10a 后台任务：输出上限（行数与总量双限；无换行的超长输出也必须受限）
BG_MAX_OUTPUT_LINES = 2000         # 单任务输出缓冲的行数上限（超出丢最老行，计入丢弃统计）
BG_MAX_OUTPUT_CHARS = 200_000      # 保留最新尾部，丢弃量单独计数
BG_MAX_NO_NEWLINE_BYTES = 65_536   # reader 无换行临时缓冲的字节上限（防无限积累）
BG_TOOL_OUTPUT_CHARS = 12_000      # 工具文案预算，低于发送层 24000 硬上限

DEBUG_MAX_RECORDS = 50
DEBUG_BASE64_PREVIEW_CHARS = 200
DEBUG_TEXT_PREVIEW_CHARS = 4000
DEBUG_SYSTEM_PROMPT_PREVIEW_CHARS = 1500
DEBUG_MESSAGE_PREVIEW_CHARS = 600
DEBUG_RESPONSE_PREVIEW_CHARS = 2000

# 会话历史压缩（Compaction）
COMPACTION_SUMMARY_MAX_CHARS = 1500  # 压缩摘要长度上限（字符）

# 长期记忆相关
MEMORY_MAX_CHARS = 4000  # 注入 system prompt 的记忆文本上限
MEMORY_FACT_MAX_LENGTH = 200  # 单条事实最大长度

# RAG 知识库切块/索引（防天量付费 embedding 请求）
RAG_MIN_CHUNK_SIZE = 100        # 切块下限：太小（如 1）会把 1MB 文档切成百万块 → 十万级 embedding 请求
RAG_MAX_TOTAL_CHUNKS = 100_000  # 单次重建的切片总数上限：超出直接拒绝（增大 chunk_size / 缩减目录）
RAG_MAX_SOURCE_FILE_BYTES = 50 * 1024 * 1024   # 单个 Markdown 最大 50 MiB，防 f.read() 先把进程打爆
RAG_MAX_TOTAL_SOURCE_BYTES = 200 * 1024 * 1024  # 单次扫描源文件总量上限

# ── B09b 运行中输入队列 ──
# 超限拒绝并保留输入，绝不静默截断：队列是用户消息的暂存区，容量是硬边界。
QUEUE_MAX_ITEMS = 20                 # 每会话排队条数上限
QUEUE_MAX_TEXT_CHARS = 20_000        # 单条正文上限（字符）
QUEUE_MAX_TOTAL_CHARS = 100_000      # 队列正文总量上限（字符）
QUEUE_EXCERPT_CHARS = 80             # 面板摘录长度（仅展示，保存仍用全文）
