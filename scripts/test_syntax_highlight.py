"""文件面板的语法高亮：分类是否分开、diff 行底色是否保住、认不出语言 / 大文件是否安静退回。

纯解析部分直接测 syntax_highlight（不建窗口）；和面板有关的用 offscreen 的 QPlainTextEdit
验证真正写进文档的字符格式。界面里不上 emoji。
"""
import pytest

from src.ui import syntax_highlight
from src.ui.theme import THEMES


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _kind_at(spans, index, text, needle):
    """needle 在 text 里的那个字符属于哪一类。"""
    pos = text.find(needle)
    if pos < 0:
        return None
    for start, end, category in spans.get(index, []):
        if start <= pos < end:
            return category
    return None


# ── 纯解析：Python（走单语言包的 highlights.scm）────────────────

def test_python_keywords_strings_comments_numbers_are_separated():
    rows = [
        (None, "hunk", "@@ -1,3 +1,4 @@"),
        (1, "ctx", " import os"),
        (2, "add", '+def greet(name: str = "x") -> int:'),
        (3, "add", "+    # 打个招呼"),
        (4, "add", "+    return len(name) + 42"),
    ]
    spans = syntax_highlight.highlight_rows(rows, "a.py")
    assert spans, "Python 有单语言包的 highlights.scm，应该高亮得出来"

    assert _kind_at(spans, 2, rows[2][2], "def") == "keyword"
    assert _kind_at(spans, 2, rows[2][2], '"x"') == "string"
    assert _kind_at(spans, 2, rows[2][2], "int") == "type"
    assert _kind_at(spans, 3, rows[3][2], "# 打个招呼") == "comment"
    assert _kind_at(spans, 4, rows[4][2], "42") == "number"
    assert _kind_at(spans, 4, rows[4][2], "len") == "function", "内置函数也算函数名"


def test_python_class_and_function_names_are_highlighted():
    rows = [
        (1, "ctx", "class WidgetBase:"),
        (2, "ctx", "    def render(self):"),
        (3, "ctx", "        pass"),
    ]
    spans = syntax_highlight.highlight_rows(rows, "a.py")
    assert _kind_at(spans, 0, rows[0][2], "WidgetBase") == "type", "类名单独上色"
    assert _kind_at(spans, 1, rows[1][2], "render") == "function", "函数名单独上色"


def test_diff_prefix_is_not_part_of_the_code_and_colors_stay_aligned():
    """unified diff 的行首 +/-/空格不是代码：要跳过，否则所有偏移都错一位。"""
    rows = [
        (None, "hunk", "@@ -1,1 +1,1 @@"),
        (1, "add", '+x = "hi"'),
        (None, "del", '-x = "old"'),
    ]
    spans = syntax_highlight.highlight_rows(rows, "a.py")
    assert _kind_at(spans, 1, rows[1][2], '"hi"') == "string"
    assert _kind_at(spans, 2, rows[2][2], '"old"') == "string"
    # 行首那个 "+" / "-" 不上色
    assert all(start >= 1 for start, _e, _c in spans[1]), "diff 前缀不参与高亮"
    assert all(start >= 1 for start, _e, _c in spans[2])


def test_plain_file_without_hunk_keeps_leading_whitespace():
    """不是 unified diff（新文件 / 无差异给的全文）时不能剥首字符，否则缩进没了、语法就变了。"""
    rows = [(1, "ctx", "    def f():"), (2, "ctx", "        return 1")]
    spans = syntax_highlight.highlight_rows(rows, "a.py")
    assert _kind_at(spans, 0, rows[0][2], "def") == "keyword"
    assert spans[0][0][0] == 4, "从第 4 个字符（缩进之后）才开始，不是第 3 个"


def test_non_ascii_lines_keep_offsets():
    """范围按字符下标给，中文注释 / 字符串里的多字节字符不能把后面的偏移算歪。"""
    rows = [(1, "ctx", 'msg = "中文注释"  # 说明')]
    spans = syntax_highlight.highlight_rows(rows, "a.py")
    assert _kind_at(spans, 0, rows[0][2], '"中文注释"') == "string"
    assert _kind_at(spans, 0, rows[0][2], "# 说明") == "comment"


def test_one_long_minified_line_with_chinese_is_fast_and_aligned():
    """压缩过的 JS / JSON 一整行二十几万字：原来每一段都重新编码、解码整行开头，
    带中文的要卡 23 秒（面板在界面线程上画）。偏移也不能被前面的多字节字符带歪。"""
    import time
    text = "".join(f"function 函数{i}(a){{return '中文'+a*{i}}};" for i in range(6000))
    t0 = time.perf_counter()
    spans = syntax_highlight.highlight_rows([(1, "add", text)], "bundle.min.js")
    assert time.perf_counter() - t0 < 5
    last = text.rfind("'中文'")
    hit = [category for start, end, category in spans[0] if start <= last < end]
    assert hit == ["string"], "行尾那一段字符串前面有上万个中文字符，位置要照样对得上"


def test_multiline_string_spans_several_rows():
    rows = [
        (1, "ctx", 'doc = """'),
        (2, "ctx", "第二行"),
        (3, "ctx", '"""'),
    ]
    spans = syntax_highlight.highlight_rows(rows, "a.py")
    assert _kind_at(spans, 1, rows[1][2], "第二行") == "string", "跨行的字符串整段都是字符串色"


# ── 纯解析：没有 highlights.scm 的语言（language pack）─────────

def test_language_pack_language_is_classified_by_node_types():
    """Java 之类只有语法、没有 query：按节点类型名分类（和 codeintel 的通用遍历一个思路）。"""
    rows = [
        (1, "ctx", "public class Foo {"),
        (2, "ctx", "    private int n = 42;"),
        (3, "ctx", '    String s = "hi"; // 说明'),
    ]
    spans = syntax_highlight.highlight_rows(rows, "Foo.java")
    assert spans, "language pack 里的语言应该能按节点类型分类"
    assert _kind_at(spans, 0, rows[0][2], "public") == "keyword"
    assert _kind_at(spans, 0, rows[0][2], "Foo") == "type"
    assert _kind_at(spans, 1, rows[1][2], "42") == "number"
    assert _kind_at(spans, 2, rows[2][2], '"hi"') == "string"
    assert _kind_at(spans, 2, rows[2][2], "// 说明") == "comment"
    assert _kind_at(spans, 2, rows[2][2], "    String") is None, "普通标识符保持正文色"


def test_missing_grammar_is_quiet(monkeypatch):
    """语法库没装 / 打包时没带这个语言：返回空，界面照原样显示。"""
    monkeypatch.setattr("src.codeintel.parser_for_path", lambda path: (None, None))
    rows = [(1, "ctx", "def f():"), (2, "ctx", "    return 1")]
    assert syntax_highlight.highlight_rows(rows, "a.py") == {}


def test_unknown_extension_is_quiet():
    rows = [(1, "ctx", "some text here")]
    assert syntax_highlight.highlight_rows(rows, "notes.txt") == {}
    assert syntax_highlight.highlight_rows(rows, "noext") == {}


def test_broken_parser_never_raises(monkeypatch):
    class _Boom:
        def parse(self, source):
            raise RuntimeError("boom")

    monkeypatch.setattr("src.codeintel.parser_for_path", lambda path: (_Boom(), "python"))
    rows = [(1, "ctx", "def f():"), (2, "ctx", "    return 1")]
    assert syntax_highlight.highlight_rows(rows, "a.py") == {}, "解析炸了也只能退回纯文本"


# ── 大文件上限 ────────────────────────────────────────────────

def test_too_many_lines_is_not_highlighted():
    rows = [(i, "ctx", f"x_{i} = {i}") for i in range(1, 10)]
    spans = syntax_highlight.highlight_rows(rows, "a.py", max_lines=8)
    assert spans == {}, "超过上限直接不高亮，别为了几行颜色把面板卡住"


def test_huge_file_is_not_highlighted():
    rows = [(i, "ctx", "x = " + "1" * 50) for i in range(1, 100)]
    assert syntax_highlight.highlight_rows(rows, "a.py", max_chars=1000) == {}


def test_default_limits_are_reasonable():
    assert 500 <= syntax_highlight.MAX_HIGHLIGHT_LINES <= 5000
    assert syntax_highlight.MAX_HIGHLIGHT_CHARS >= 100_000


# ── 主题 token ────────────────────────────────────────────────

def test_every_category_has_a_token_in_both_themes():
    for theme in ("light", "dark"):
        for category, token in syntax_highlight.TOKEN_BY_CATEGORY.items():
            assert token in THEMES[theme], f"{theme} 缺 {token}"
            assert THEMES[theme][token].startswith("#"), f"{token} 得是颜色值"


def test_light_and_dark_define_the_same_syntax_tokens():
    light = {k for k in THEMES["light"] if k.startswith("syn_")}
    dark = {k for k in THEMES["dark"] if k.startswith("syn_")}
    assert light and light == dark, "两套主题的 syn_* 键必须一致"


def test_highlight_uses_theme_colors_not_hardcoded_values():
    """类别 → token 的映射里不该出现裸颜色值（颜色只放主题）。"""
    for token in syntax_highlight.TOKEN_BY_CATEGORY.values():
        assert token.startswith("syn_")


# ── 面板里真正写进去的字符格式 ────────────────────────────────

def _panel(theme="light"):
    from src.ui.file_panel import FilePanel
    return FilePanel(None, theme_lookup=lambda k: THEMES[theme][k])


def _block_colors(view, block):
    from PySide6.QtGui import QTextCursor
    cursor = QTextCursor(view.document())
    colors = []
    for i in range(max(0, block.length() - 1)):
        # charFormat() 给的是光标【前】一个字符的格式，所以位置要 +1 才能拿到第 i 个字符的
        cursor.setPosition(block.position() + i + 1)
        colors.append(cursor.charFormat().foreground().color().name())
    return colors


def _code_colors(view, block):
    """只取正文那一段的颜色：前面的行号列固定是 mv_muted2，别混进断言里。"""
    colors = _block_colors(view, block)
    text = block.text()
    start = text.find("│ ")
    return colors[(start + 2):] if start >= 0 else colors


def _blocks(view):
    out, block = [], view.document().begin()
    while block.isValid():
        out.append(block)
        block = block.next()
    return out


@pytest.fixture()
def panels():
    made = []

    def _make(theme="light"):
        panel = _panel(theme)
        made.append(panel)
        return panel

    yield _make
    from PySide6.QtCore import QEvent
    for panel in made:
        panel.close()
        panel.deleteLater()
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is not None:
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()


def test_panel_paints_syntax_colors_on_top_of_the_diff_tint(qapp, panels):
    panel = panels()
    rows = [
        (None, "hunk", "@@ -1,2 +1,3 @@"),
        (1, "ctx", ' import os'),
        (2, "add", '+def greet(x: int = 3):'),
        (3, "add", '+    return "hi"'),
    ]
    panel.open_file("a.py", rows)
    view = panel._panes["a.py"]
    blocks = _blocks(view)

    add_block = blocks[2]
    colors = _block_colors(view, add_block)
    assert THEMES["light"]["syn_keyword"] in colors, "def 上关键字色"
    assert THEMES["light"]["syn_string"] not in colors  # 这一行没有字符串
    assert THEMES["light"]["mv_ok"] in colors, "没被语法覆盖的部分仍是新增绿"

    tint = add_block.blockFormat().background().color().name()
    assert tint != THEMES["light"]["mv_view_bg"], "新增行整行的淡绿底要保留（不是靠文字色）"

    string_block = blocks[3]
    assert THEMES["light"]["syn_string"] in _block_colors(view, string_block)


def test_deleted_rows_keep_their_red_tint_under_syntax_colors(qapp, panels):
    panel = panels()
    rows = [
        (None, "hunk", "@@ -1,1 +1,1 @@"),
        (None, "del", '-old = "gone"'),
        (1, "add", '+old = "new"'),
    ]
    panel.open_file("a.py", rows)
    view = panel._panes["a.py"]
    del_block = _blocks(view)[1]
    assert THEMES["light"]["syn_string"] in _block_colors(view, del_block)
    assert del_block.blockFormat().background().color().name() != THEMES["light"]["mv_view_bg"], \
        "删除行整行的淡红底保留"


def test_panel_falls_back_to_row_colors_without_a_grammar(qapp, panels, monkeypatch):
    monkeypatch.setattr("src.codeintel.parser_for_path", lambda path: (None, None))
    panel = panels()
    panel.open_file("a.py", [(1, "add", "+x = 1"), (None, "del", "-y = 2")])
    view = panel._panes["a.py"]
    blocks = _blocks(view)
    assert THEMES["light"]["mv_ok"] in _block_colors(view, blocks[0])
    assert THEMES["light"]["mv_bad"] in _block_colors(view, blocks[1])


def test_theme_switch_recolors_without_reparsing(qapp, panels, monkeypatch):
    panel = panels()
    panel.open_file("a.py", [(1, "ctx", "def f(): pass")])
    view = panel._panes["a.py"]
    assert THEMES["light"]["syn_keyword"] in _block_colors(view, _blocks(view)[0])

    calls = []
    original = syntax_highlight.highlight_rows

    def _counting(rows, path, *args, **kwargs):
        calls.append(path)
        return original(rows, path, *args, **kwargs)

    monkeypatch.setattr(syntax_highlight, "highlight_rows", _counting)
    panel.theme_lookup = lambda k: THEMES["dark"][k]
    panel.apply_theme()
    assert THEMES["dark"]["syn_keyword"] in _block_colors(view, _blocks(view)[0]), \
        "切主题：语法色跟着换"
    assert calls == [], "内容没变，切主题不该再解析一遍"


def test_reopening_the_same_file_reparses_after_content_changes(qapp, panels, monkeypatch):
    panel = panels()
    panel.open_file("a.py", [(1, "ctx", "x = 1")])
    calls = []
    original = syntax_highlight.highlight_rows

    def _counting(rows, path, *args, **kwargs):
        calls.append(list(rows))
        return original(rows, path, *args, **kwargs)

    monkeypatch.setattr(syntax_highlight, "highlight_rows", _counting)
    panel.open_file("a.py", [(1, "ctx", 'x = "two"')])
    assert len(calls) == 1, "内容换了要重新解析"
    view = panel._panes["a.py"]
    assert THEMES["light"]["syn_string"] in _block_colors(view, _blocks(view)[0])


def test_closing_a_tab_drops_its_highlight_cache(qapp, panels):
    panel = panels()
    panel.open_file("a.py", [(1, "ctx", "x = 1")])
    assert "a.py" in panel._spans
    panel.close_file("a.py")
    assert "a.py" not in panel._spans, "关掉标签别留着解析结果"


def test_big_file_in_the_panel_stays_plain_and_fast(qapp, panels):
    panel = panels()
    rows = [(i, "ctx", f"x_{i} = {i}  # 注释") for i in range(1, 4000)]
    panel.open_file("a.py", rows)
    view = panel._panes["a.py"]
    assert "x_3999" in view.toPlainText()
    colors = set(_code_colors(view, _blocks(view)[0]))
    assert colors == {THEMES["light"]["mv_text"]}, "超过上限不高亮，正文色就好"


def test_panel_text_has_no_emoji(qapp, panels):
    panel = panels()
    panel.open_file("a.py", [(1, "ctx", "def f(): pass")])
    panel.open_file("b.java", [(1, "ctx", "class B {}")])
    text = "".join(view.toPlainText() for view in panel._panes.values())
    text += panel.path_label.text() + panel.hint_label.text()
    assert not [ch for ch in text if ord(ch) > 0x1F000], "界面里不用 emoji"
