"""@文件引用测试：模糊匹配 / @检测 / 逐层目录列举 / 引用提示（调用读取，不注入内容）。

被测方法都不真正用到 self（只读 state / os / 入参），所以用 None 或 mock 当 self 直接调。
"""
from unittest.mock import MagicMock

from src.ui.chat_window import ChatUI
from src.ui.widgets import FileCompleter


# ─────────────── fuzzy_match_positions（子序列 + 命中位置）───────────────
class TestFuzzyMatch:
    def test_subsequence_match(self):
        score, pos = FileCompleter.fuzzy_match_positions("cwpy", "chat_window.py")
        assert score >= 0                 # c-w-p-y 跳着也算匹配
        assert len(pos) == 4

    def test_no_match(self):
        score, pos = FileCompleter.fuzzy_match_positions("xyz", "main.py")
        assert score < 0
        assert pos == []

    def test_positions_point_to_matched_chars(self):
        score, pos = FileCompleter.fuzzy_match_positions("mn", "main.py")
        assert score >= 0
        assert pos[0] == 0                 # m 在下标 0
        assert "main.py"[pos[1]] == "n"    # n 命中的是真正的 n

    def test_empty_query(self):
        score, pos = FileCompleter.fuzzy_match_positions("", "main.py")
        assert score >= 0                  # 空 query 视为全匹配
        assert pos == []


# ─────────────── _get_active_mention（@检测，mock entry）───────────────
def _mention(text, pos=None):
    """造一个只有 entry 的假 self，调 _get_active_mention。"""
    if pos is None:
        pos = len(text)
    fake = MagicMock()
    fake.entry.textCursor.return_value.position.return_value = pos
    fake.entry.toPlainText.return_value = text
    return ChatUI._get_active_mention(fake)


class TestActiveMention:
    def test_at_start(self):
        assert _mention("@foo") == (0, "foo")

    def test_after_space(self):
        assert _mention("hi @bar") == (3, "bar")

    def test_email_excluded(self):
        # user@domain：@ 前是字母 → 不当文件引用
        assert _mention("user@domain") is None

    def test_space_in_partial_stops(self):
        # 已完成的 @path 后跟空格 → partial 含空格 → 不再算 mention
        assert _mention("@foo bar") is None

    def test_bare_at(self):
        assert _mention("@") == (0, "")

    def test_no_at(self):
        assert _mention("hello world") is None


# ─────────────── _list_project_dir（逐层列单层）───────────────
class TestListProjectDir:
    def test_lists_and_folder_first(self, project_dir):
        (project_dir / "src").mkdir()
        (project_dir / "a.py").write_text("x", encoding="utf-8")
        (project_dir / ".git").mkdir()             # 噪声目录
        out = ChatUI._list_project_dir(None, "")
        names = [n for n, _ in out]
        assert "src" in names and "a.py" in names
        assert ".git" not in names                 # 跳噪声
        assert out[0][1] is True                   # 文件夹优先（src 排最前）

    def test_subdir(self, project_dir):
        (project_dir / "src").mkdir()
        (project_dir / "src" / "foo.py").write_text("x", encoding="utf-8")
        out = ChatUI._list_project_dir(None, "src")
        assert ("foo.py", False) in out

    def test_nonexistent_dir(self, project_dir):
        assert ChatUI._list_project_dir(None, "nope") == []


# ─────────────── _expand_file_mentions（追加提示，不注入内容）───────────────
class TestExpandFileMentions:
    def test_no_mention_unchanged(self, project_dir):
        assert ChatUI._expand_file_mentions(None, "hello") == "hello"

    def test_file_hint_not_content(self, project_dir):
        (project_dir / "a.py").write_text("SECRET_CONTENT", encoding="utf-8")
        out = ChatUI._expand_file_mentions(None, "看 @a.py")
        assert "看 @a.py" in out
        assert "read_file" in out              # 提示 AI 用 read_file 读
        assert "SECRET_CONTENT" not in out     # 【不】把文件内容塞进去

    def test_dir_hint_uses_list_directory(self, project_dir):
        (project_dir / "src").mkdir()
        out = ChatUI._expand_file_mentions(None, "@src")
        assert "list_directory" in out

    def test_nonexistent_ref_no_hint(self, project_dir):
        out = ChatUI._expand_file_mentions(None, "@nope.py")
        assert out == "@nope.py"               # 不存在的引用 → 原样、不加提示
