"""文字树后处理（纯逻辑，不依赖 Qt / WebEngine，可单测）。

tree.js 在页面里算出文字树（role/name、折叠剪枝、上限），本模块做 Python 侧的收尾：
动作前后的"变化行 diff"（§3.2，省 token 的关键）、不可信围栏（§6.3）、字符上限兜底（§3.1）。
"""
import difflib
import re

from src.limits import TOOL_RESULT_HARD_CAP_CHARS

# 各类结果的默认上限（§3.1）。max_chars 传再大也不超单条工具结果硬上限 24000。
SNAPSHOT_DEFAULT_CHARS = 12000
READ_TEXT_DEFAULT_CHARS = 8000
DELTA_DEFAULT_CHARS = 2000
CONSOLE_DEFAULT_CHARS = 4000
VALUE_DEFAULT_CHARS = 100

# 只能说"文字树没变"，不能说"页面没变"：纯样式变化（颜色、隐藏的 class 切换）文字树看不见。
# 早先写"页面无可见变化"，模型点掉一条待办（只加删除线）后信以为真，绕去加诊断代码（实测）
DELTA_EMPTY = "文字树没有变化（纯样式变化看不到；要核对就 browser_screenshot 或 browser_read_text）"
_TRUNCATE_MARKER = "…[已截断]"
_FENCE_TAG_RE = re.compile(r"</?browser-content", re.IGNORECASE)


def effective_cap(requested, default: int) -> int:
    """把请求的上限夹到 [1, 硬上限]；没给（None/<=0）用默认。"""
    try:
        n = int(requested)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = default
    return max(1, min(n, TOOL_RESULT_HARD_CAP_CHARS))


def truncate(text: str, cap: int) -> str:
    """超 cap 就截尾加标记（快照/正文保头即可，模型要更多用 root_ref / offset）。"""
    if text is None:
        return ""
    if len(text) <= cap:
        return text
    keep = max(0, cap - len(_TRUNCATE_MARKER))
    return text[:keep] + _TRUNCATE_MARKER


def compute_delta(before: str, after: str, cap: int = DELTA_DEFAULT_CHARS) -> str:
    """all 模式快照前后的"变化行 diff"（§3.2）。

    只回增删行（+ 新增 / - 删除），带原缩进，不重发全树。实测点一次"添加一行"：
    diff 22 字 vs 全量 926 字（42×）。无变化时明确写 DELTA_EMPTY（文字树没变，不等于页面没变），
    防模型以为没生效反复点。交互树看不见纯内容变化（§3.6），所以 diff 必须用 all 模式算；
    删除线 / aria 状态 tree.js 会标成 [strikethrough] / [checked] / [pressed] / [selected]。
    """
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    diff = difflib.unified_diff(before_lines, after_lines, lineterm="", n=0)
    changed = [
        line for line in diff
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    if not changed:
        return DELTA_EMPTY
    return truncate("\n".join(changed), cap)


def fence(content: str, url: str) -> str:
    """给页面文字围上不可信来源标记（§6.3）。只标来源与属性，不放长段说教（每轮重发省 token）。

    内容里的围栏标签（含结束标记）转义掉：否则页面正文写一行 </browser-content>
    就能把后续文字送出围栏、伪装成围栏外的正常上下文（注入）。"""
    from src.browser.security import fence_url
    safe = _FENCE_TAG_RE.sub(lambda m: "&lt;" + m.group(0)[1:], content or "")
    return f'<browser-content url="{fence_url(url)}" untrusted>\n{safe}\n</browser-content>'