"""回复里的链接（设计稿「消息区：回复里的链接」）：

- 光秃秃的网址变成链接（以前只认 localhost，公网地址点不了），显示成「域名/路径前段…」；
- 网址末尾粘着的中文 / 英文标点、不配对的右括号、转义过的实体不算网址；
- 代码块、行内代码、已经是链接的不动；
- 左键：本机地址总在侧栏浏览器开，公网按设置（默认系统浏览器）；悬停提示说清楚点了会怎样。
"""
import pytest

from src.ui.markdown_render import (
    MarkdownRenderMixin, _autolink_bare_urls, _trim_url, short_url,
)


class _Host:
    def __init__(self):
        self._code_blocks = {}

    def _t(self, key):
        return {"mv_indigo": "#5b6cf0", "mv_panel_accent": "#c8cbf3"}.get(
            key, "transparent" if "border" in key else "#222838")

    def _inline_svg_img(self, *a, **k):
        return ""


def render(md):
    return MarkdownRenderMixin._md_to_html(_Host(), md)


@pytest.mark.parametrize("raw, url, tail", [
    ("https://x.com/a", "https://x.com/a", ""),
    ("https://x.com/a.", "https://x.com/a", "."),
    ("https://x.com/a),", "https://x.com/a", "),"),
    ("https://en.wikipedia.org/wiki/Foo_(bar)", "https://en.wikipedia.org/wiki/Foo_(bar)", ""),
    ("https://x.com/?a=1&amp;b=2", "https://x.com/?a=1&amp;b=2", ""),
    ("https://x.com/a&gt;", "https://x.com/a", "&gt;"),
    ("https://x.com/a;", "https://x.com/a", ";"),
])
def test_trailing_punctuation_is_not_part_of_the_url(raw, url, tail):
    assert _trim_url(raw) == (url, tail)


def test_bare_urls_are_linked_but_code_and_links_are_left_alone():
    html = ('<p>见 https://a.com/x。还有（https://b.com/y）</p>'
            '<p><code>https://c.com/z</code> <a href="https://d.com">已有</a></p>'
            '<pre><code>curl https://e.com/api</code></pre>')
    out = _autolink_bare_urls(html)
    assert '<a href="https://a.com/x">https://a.com/x</a>。' in out
    assert '<a href="https://b.com/y">https://b.com/y</a>）' in out, "中文括号不吞进网址"
    for untouched in ("https://c.com/z", "https://e.com/api"):
        assert f'href="{untouched}"' not in out
    assert out.count('href="https://d.com"') == 1
    en = _autolink_bare_urls("<p>See https://a.com/x. Or (https://b.com/y), ok</p>")
    assert '<a href="https://a.com/x">https://a.com/x</a>.' in en, "英文句号不算网址"
    assert '<a href="https://b.com/y">https://b.com/y</a>),' in en, "不配对的右括号不算网址"


@pytest.mark.parametrize("url, shown", [
    ("https://www.family.co.jp/", "family.co.jp"),
    ("https://x.com/famima_now/status/2119958227920027842", "x.com/famima_now/status/21199582279…"),
    ("http://localhost:5173/menu", "localhost:5173/menu"),
    ("https://a.com/p?q=1&amp;r=2", "a.com/p?q=1&r=2"),
])
def test_short_url(url, shown):
    assert short_url(url) == shown
    assert len(short_url(url)) <= 36


def test_titled_link_keeps_its_title_and_bare_one_is_shortened():
    html = render("官网：[全家三角芝士蛋糕官网](https://www.family.co.jp/campaign/spot/2611.html)\n\n"
                  "推文：https://x.com/famima_now/status/2119958227920027842")
    assert ">全家三角芝士蛋糕官网<" in html and "↗" in html
    assert ">x.com/famima_now/status/21199582279…<" in html
    assert 'href="https://x.com/famima_now/status/2119958227920027842"' in html, "链接地址是完整的"


def test_code_spans_with_a_url_keep_code_style():
    """复审 12：模型写成行内代码的本机地址照样是代码样式，只是变成可点的强调色。"""
    html = render("开 `http://localhost:5173/` 看看")
    assert '<a href="http://localhost:5173/"' in html and "<code" in html and "▸" in html
    assert "color:#5b6cf0" in html.split('<a href="http://localhost:5173/"', 1)[1].split("</a>")[0]


# ── 点击和悬停 ──────────────────────────────────────────────────

class _Chat:
    def __init__(self):
        self.panel, self.system, self.files = [], [], []

    def _open_browser_in_panel(self, href, sess=None):
        self.panel.append(href)

    def open_link_in_system(self, href):
        self.system.append(href)

    def _open_file_in_panel(self, rel, line=0):
        self.files.append((rel, line))

    def _settle_file_panel(self):
        pass


@pytest.mark.parametrize("mode, href, where", [
    ("system", "https://example.com/a", "system"),
    ("panel", "https://example.com/a", "panel"),
    ("system", "http://localhost:5173/", "panel"),     # 本机地址不看设置
    ("panel", "http://127.0.0.1:8000/x", "panel"),
])
def test_click_follows_the_setting(monkeypatch, mode, href, where):
    from src import config
    from src.ui.chat_window import ChatUI
    monkeypatch.setattr(config, "LINK_OPEN_MODE", mode)
    chat = _Chat()
    ChatUI._on_file_link_clicked(chat, href)
    assert getattr(chat, where) == [href]
    other = "panel" if where == "system" else "system"
    assert getattr(chat, other) == []


def test_file_links_still_open_in_the_file_panel(monkeypatch):
    from src.ui.chat_window import ChatUI
    chat = _Chat()
    ChatUI._on_file_link_clicked(chat, "lingxi-file:src/ui/task_panel.py:835")
    assert chat.files == [("src/ui/task_panel.py", 835)] and not chat.system and not chat.panel


def test_hover_tip_says_where_the_click_goes(monkeypatch):
    from src import config
    from src.ui.message_view import link_tip
    monkeypatch.setattr(config, "LINK_OPEN_MODE", "system")
    assert link_tip("https://example.com/a") == "https://example.com/a\n点击用系统浏览器打开 · 右键更多"
    assert "点击在侧栏浏览器打开" in link_tip("http://localhost:5173/")
    monkeypatch.setattr(config, "LINK_OPEN_MODE", "panel")
    assert "点击在侧栏浏览器打开" in link_tip("https://example.com/a")
    assert link_tip("lingxi-file:a.py") == "在右侧文件面板里打开"
    assert link_tip("") == ""
