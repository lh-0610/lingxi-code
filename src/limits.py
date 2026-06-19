"""Shared limits for conversation, tools, and debug views."""


SESSION_HISTORY_LIMIT = 50

HISTORY_TOKEN_BUDGET = 80_000

# M3: 按模型上下文窗口动态计算预算时的参数
HISTORY_SAFETY_MARGIN = 8_000
MAX_HISTORY_BUDGET = 200_000
HISTORY_KEEP_RECENT = 20

# 工具结果分级回收（M2）：超预算时先把"旧的大工具结果"截成存根，再走 LLM 压缩
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
SEARCH_IN_FILE_DEFAULT_LIMIT = 50
SEARCH_IN_FILE_MAX_LIMIT = 200
SEARCH_FILES_MAX_RESULTS = 50

RUN_COMMAND_TIMEOUT_S = 300
RUN_COMMAND_MAX_OUTPUT_CHARS = 5000
TOOL_RESULT_PREVIEW_CHARS = 500
# 后台命令注册表里"已退出"的进程最多保留几个（仍可被 read_background_output 读最终输出）。
# 超过则淘汰最老的已退出项，防长会话里崩溃/跑完的后台任务连同 2000 行输出 deque 无限驻留。
# 运行中的进程从不被淘汰。
BG_MAX_RETAINED_EXITED = 10

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
