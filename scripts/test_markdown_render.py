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


# ── 复审 12：行内代码 / 裸文本里的 localhost 地址要能点（浏览器面板打开）──

def test_inline_code_localhost_linkified():
    html = render("开 `http://localhost:5173/` 看看")
    assert '<a href="http://localhost:5173/"' in html
    assert "<code" in html, "链接文字仍保留行内代码样式"


def test_bare_localhost_linkified():
    html = render("dev server 在 http://localhost:5173/x 已起")
    assert '<a href="http://localhost:5173/x"' in html


def test_existing_localhost_md_link_not_doubled():
    html = render("[dev](http://localhost:5173/)")
    assert html.count('<a href="http://localhost:5173/"') == 1


def test_code_block_localhost_not_linkified():
    html = render("```\nhttp://localhost:5173/\n```")
    assert '<a href="http://localhost:5173/' not in html


def test_public_url_becomes_a_short_link():
    """设计稿「回复里的链接」：公网网址以前不是链接、点不了；现在变成链接，显示简短形式。"""
    html = render("见 http://www.example.com/docs/guide 文档")
    assert '<a href="http://www.example.com/docs/guide"' in html
    assert ">example.com/docs/guide<" in html, "去掉协议和 www."


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
    # 两列表格：符号 / 序号一列，内容一列（悬挂缩进）
    for item in ("•第一处", "•第二处", "1.跑测试", "2.看差异"):
        assert item in text


def test_nested_and_loose_lists_keep_markers_inline():
    out = render("步骤：\n\n1. 改代码\n\n2. 跑测试\n    - 只跑相关的\n")
    text = re.sub(r"<[^>]+>", "", out)
    assert "1.改代码" in text and "2.跑测试" in text and "•只跑相关的" in text
    # 嵌套的那层落在外层「跑测试」那项的内容列里（再往里缩一层）
    inner = out.index("只跑相关的")
    assert out.rfind("<table", 0, inner) > out.index("跑测试"), "嵌套列表是外层那项里的一张表"


def test_list_fix_leaves_code_blocks_and_single_stars_alone():
    out = protect("说明：\n```\n- not a list\n**x**\n```\n看 *动作* 和 `p, *rest = xs`")
    assert "```\n- not a list\n**x**\n```" in out, "代码块里一个字都不动"
    assert r"\*动作\*" in out and "`p, *rest = xs`" in out
    from src.ui.markdown_render import _blank_line_before_lists
    fenced = "说明：\n```\n- a\n```"
    assert _blank_line_before_lists(fenced) == fenced


# ── 2026-11-24 排查（docs/bug_review_2026-11-24.md R3 R4 R6）──

import pytest  # noqa: E402


@pytest.fixture()
def qt_app_md(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _plain(html):
    from PySide6.QtGui import QTextDocument
    d = QTextDocument()
    d.setHtml(html)
    return d.toPlainText()


def test_code_blocks_are_split_like_python_markdown_splits_them(qt_app_md):
    """R3：以前用非贪婪的 ```.*?``` 从任意位置配对，代码里出现 ``` 就切错位，后半段代码被当成正文。"""
    from src.ui.markdown_render import _split_code, _split_fences
    cases = {
        '```python\nif s.startswith("```"):\n    y = a*b\n```\n': ["a*b"],
        "````markdown\n```python\nx = a*b\n```\n````\n": ["a*b"],                # 四反引号包三反引号
        "用 `` ``` `` 包住代码，例如：\n\n```py\nx = c*d\n```\n": ["c*d"],          # 行内代码里有 ```
    }
    for md, inside in cases.items():
        for split in (_split_fences, _split_code):
            parts = split(md)
            assert "".join(parts) == md, "切完拼回去要和原文一模一样"
        text = _plain(render(md))
        for s in inside:
            assert s in text and "\\*" not in text, f"代码里的 * 被转义了：{text!r}"
    # 没有结尾的围栏不算代码块（Python-Markdown 也不认）；行内代码按同样长的一串反引号配对
    assert _split_fences("```\n没有结尾 *x*") == ["```\n没有结尾 *x*"]
    assert _split_code("a `x` b ``c`d`` e")[1::2] == ["`x`", "``c`d``"]


def test_code_block_text_is_not_rewritten(qt_app_md, tmp_path, monkeypatch):
    """R3：代码里的 `src/app.py` 不改写成链接、列表前不插空行。"""
    from src.ui import markdown_render as mr
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    monkeypatch.setattr(mr, "_link_root", lambda: str(tmp_path))
    md = '```python\nFENCE = "```"\nprint("`src/app.py`")\nHELP = """用法\n- a\n"""\n```\n'
    text = _plain(render(md))
    assert "lingxi-file:" not in text and "用法\n- a" in text, repr(text)
    # 代码块外面的照常变链接
    assert "lingxi-file:src/app.py" in render(md + "\n见 `src/app.py`")


def test_table_right_under_a_line_of_text_is_a_table(qt_app_md):
    """R6：「对比如下：」下一行紧跟表格（不空行），以前原样显示竖线和 |---|。"""
    html = render("对比如下：\n| 方案 | 耗时 |\n|---|:--:|\n| A | 1s |\n")
    assert "<table" in html and "|---|" not in _plain(html)
    # 上面那行正文自己带竖线（「用 a | b 表示」）也照样补
    html = render("用 a | b 表示：\n| x | y |\n|---|---|\n| 1 | 2 |\n")
    assert "<table" in html and "|---|" not in _plain(html), _plain(html)
    # 代码块里的不动
    md = "```\n说明\n| a | b |\n|---|---|\n```\n"
    assert "说明\n| a | b |" in _plain(render(md))


def test_copy_button_keeps_the_code_exactly(qt_app_md):
    """R4 附带：以前先换 &amp; 再换 &lt;，代码里本来写着 &lt;div&gt; 的被还原成 <div>。"""
    host = _Host()
    MarkdownRenderMixin._md_to_html(host, "```html\n&lt;div&gt; <p>a & b</p>\n```\n")
    assert host._code_blocks == {"0": "&lt;div&gt; <p>a & b</p>\n"}
