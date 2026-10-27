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
