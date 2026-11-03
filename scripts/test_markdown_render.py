"""Markdown 渲染：正文里的单个 * 保持字面量，代码里的 * 原样不动；代码块必须用等宽字体。"""
import re

from src.ui.markdown_render import MarkdownRenderMixin
from src.ui.markdown_render import _protect_literal_asterisks as protect


class _Host:
    """_md_to_html 只用到宿主的这几样。"""
    def __init__(self):
        self._code_blocks = {}

    def _t(self, key):
        return "transparent" if "border" in key or key == "md_blockquote_bg" else "#222838"

    def _inline_svg_img(self, *args, **kwargs):
        return ""


def render(md):
    return MarkdownRenderMixin._md_to_html(_Host(), md)


def test_fenced_code_keeps_asterisks():
    src = "看这段：\n```python\np, *rest = xs\nprint(*args, **kwargs)\n```\n"
    out = protect(src)
    assert "p, *rest = xs" in out
    assert "print(*args, **kwargs)" in out
    assert "\\" not in out


def test_inline_code_keeps_asterisks():
    assert protect("用 `*args` 收参数") == "用 `*args` 收参数"


def test_single_asterisk_in_prose_stays_literal():
    assert protect("*笑* 好的") == "\\*笑\\* 好的"


def test_bold_still_works():
    assert protect("**重点** 和 *动作*") == "**重点** 和 \\*动作\\*"


def test_prose_and_code_mixed():
    src = "*注意*：`a * b` 会溢出\n~~~\nx = *y\n~~~"
    out = protect(src)
    assert out.startswith("\\*注意\\*：`a * b` 会溢出")
    assert "x = *y" in out


def test_fenced_code_with_language_gets_mono_font():
    """带语言名的代码块（<code class="language-python">）也要套上等宽字体，
    否则 Qt 用默认的 Courier New。"""
    html = render("```python\np, *rest = xs\n```")
    codes = re.findall(r"<code[^>]*>", html)
    assert codes, html
    assert all("font-family:" in tag for tag in codes), codes
    assert "p, *rest = xs" in html


def test_inline_code_keeps_inline_style():
    html = render("用 `*args` 收参数")
    assert re.search(r"<code style=\"background:[^\"]*font-family:", html), html


def test_bold_around_inline_code_is_bold_not_literal_stars():
    """**`src/x.py`**（DeepSeek 常这么写）：开头结尾的 ** 不能被当成字面星号露出来。"""
    out = render("**`scripts/test_agent_outcomes.py`**（夹具 4 处）")
    text = re.sub(r"<[^>]+>", "", out)
    assert "**" not in text
    assert "<strong>" in out and "scripts/test_agent_outcomes.py" in out


def test_list_right_under_a_line_of_text_is_a_list():
    """列表紧跟在一行文字下面、中间没空行，也要渲染成列表，不能每行露出一个 "- "。"""
    out = render("改了两处：\n- 第一处\n- 第二处\n\n验证：\n1. 跑测试\n2. 看差异")
    text = re.sub(r"<[^>]+>", "", out)
    assert "- 第一处" not in text
    # 摊平成段落但留下项目符号和序号
    for item in ("• 第一处", "• 第二处", "1. 跑测试", "2. 看差异"):
        assert item in text


def test_nested_and_loose_lists_keep_markers_inline():
    out = render("步骤：\n\n1. 改代码\n\n2. 跑测试\n    - 只跑相关的\n")
    text = re.sub(r"<[^>]+>", "", out)
    assert "1. 改代码" in text and "2. 跑测试" in text and "• 只跑相关的" in text
    assert "margin:2px 0 2px 18px" in out, "嵌套的一层往里缩"


def test_list_fix_leaves_code_blocks_and_single_stars_alone():
    out = protect("说明：\n```\n- not a list\n**x**\n```\n看 *动作* 和 `p, *rest = xs`")
    assert "```\n- not a list\n**x**\n```" in out, "代码块里一个字都不动"
    assert r"\*动作\*" in out and "`p, *rest = xs`" in out
    from src.ui.markdown_render import _blank_line_before_lists
    fenced = "说明：\n```\n- a\n```"
    assert _blank_line_before_lists(fenced) == fenced
