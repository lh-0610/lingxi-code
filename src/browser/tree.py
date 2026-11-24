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


def compute_delta(before: str, after: str, cap: int = DELTA_DEFAULT_CHARS, *,
                  before_cut: bool = False, after_cut: bool = False) -> str:
    """all 模式快照前后的"变化行 diff"（§3.2）。

    只回增删行（+ 新增 / - 删除），带原缩进，不重发全树。实测点一次"添加一行"：
    diff 22 字 vs 全量 926 字（42×）。无变化时明确写 DELTA_EMPTY（文字树没变，不等于页面没变），
    防模型以为没生效反复点。交互树看不见纯内容变化（§3.6），所以 diff 必须用 all 模式算；
    删除线 / aria 状态 tree.js 会标成 [strikethrough] / [checked] / [pressed] / [selected]。
    before_cut / after_cut：那一份快照被截断了（tree.js 的 stats.truncated）。
    """
    before_lines = _drop_cut_line((before or "").splitlines())
    after_lines = _drop_cut_line((after or "").splitlines())
    sm = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    ops = sm.get_opcodes()
    # 截断的快照只有前一截：页面上面插一条，最底下那条就被挤出上限，逐行比会多出一行假的
    # 「- 第 859 条」（复审 #3）。最后一段相同的内容一直延伸到某一侧的末尾时，另一侧在它
    # 之后多出来的行只是被截掉 / 没截到的尾巴，不算增删。
    last_eq = next((op for op in reversed(ops) if op[0] == "equal"), None)
    tail_before = tail_after = None
    if last_eq is not None:
        _, i1, i2, j1, j2 = last_eq
        if j2 == len(after_lines) and (after_cut or _cut(after)):
            tail_before = i2           # before 里这之后的「删除」是 after 截掉的尾巴
        if i2 == len(before_lines) and (before_cut or _cut(before)):
            tail_after = j2            # after 里这之后的「新增」是 before 截掉的尾巴
    changed = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        if tag in ("delete", "replace") and not (tail_before is not None and i1 >= tail_before):
            changed.extend("-" + ln for ln in before_lines[i1:i2])
        if tag in ("insert", "replace") and not (tail_after is not None and j1 >= tail_after):
            changed.extend("+" + ln for ln in after_lines[j1:j2])
    if not changed:
        return DELTA_EMPTY
    text = "\n".join(changed)
    if len(text) <= cap:
        return text
    # 放不下：先列新增的行，消失的只报行数。单页应用换路由时整页都变，以前「-」行在前，
    # 把上限占满，新页面的「+」行全被截掉——模型要看的恰恰是现在页面上有什么（复审 #12）
    added = [ln for ln in changed if ln.startswith("+")]
    gone = len(changed) - len(added)
    note = f"\n（变化太多：只列了新增的行，另有 {gone} 行消失，没列出）" if gone else ""
    return truncate("\n".join(added), max(200, cap - len(note))) + note


def _cut(text: str) -> bool:
    return (text or "").rstrip().endswith(_TRUNCATE_MARKER)


def _drop_cut_line(lines):
    """截断那一行只剩半截：不拿它比（前后截在不同位置，半截行永远对不上）。"""
    if lines and lines[-1].rstrip().endswith(_TRUNCATE_MARKER):
        return lines[:-1]
    return lines


def fence(content: str, url: str) -> str:
    """给页面文字围上不可信来源标记（§6.3）。只标来源与属性，不放长段说教（每轮重发省 token）。

    内容里的围栏标签（含结束标记）转义掉：否则页面正文写一行 </browser-content>
    就能把后续文字送出围栏、伪装成围栏外的正常上下文（注入）。"""
    from src.browser.security import fence_url
    safe = _FENCE_TAG_RE.sub(lambda m: "&lt;" + m.group(0)[1:], content or "")
    return f'<browser-content url="{fence_url(url)}" untrusted>\n{safe}\n</browser-content>'