"""侧栏「编码/知识库」模式入口 + 知识库管理卡片（rag_sidebar.py）UI 行为测试。

用轻量 host（RagSidebarMixin + QWidget）而非完整 ChatUI——不 import agent（避免
LLM 实例化 / MCP 子进程），只测 mixin 的 UI 状态机。QT_QPA_PLATFORM=offscreen。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

from src import session as _session
from src.ui.rag_sidebar import RagSidebarMixin
from src.ui.sidebar import SidebarMixin
from src.ui.widgets import SignalBridge


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _Host(RagSidebarMixin, QWidget):
    """最小宿主：提供 mixin 依赖的 bridge / _show_toast / new_chat_btn。"""

    def __init__(self):
        super().__init__()
        self.bridge = SignalBridge()
        self.bridge.kb_status.connect(self._on_kb_status)
        self.toasts = []
        lay = QVBoxLayout(self)
        self._build_rag_sidebar(lay)
        self.new_chat_btn = QPushButton("+ 新对话")
        lay.addWidget(self.new_chat_btn)

    def _show_toast(self, msg):
        self.toasts.append(msg)


@pytest.fixture()
def host(qapp):
    return _Host()


class _FakeThread:
    """同步执行的假线程：让重建流程确定性跑完（含 done 信号），便于断言。"""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class TestSidebarUISync:
    """侧栏入口/卡片的纯 UI 同步（不含工作区切换的会话激活——那走完整 ChatUI，见 TestWorkspace）。"""

    def test_code_mode_hides_kb_card(self, host):
        """编码模式（默认）：只显示模式入口，管理卡片隐藏。"""
        assert host.code_mode_btn.isChecked() and not host.kb_mode_btn.isChecked()
        assert host.kb_card.isHidden()

    def test_apply_rag_mode_to_ui(self, host):
        """卡片展开/收起 + 段控选中 + 新对话文案随 rag 开关。"""
        host._apply_rag_mode_to_ui(True)
        assert host.kb_mode_btn.isChecked() and not host.code_mode_btn.isChecked()
        assert not host.kb_card.isHidden()
        assert host.new_chat_btn.text() == "+ 新知识库对话"
        host._apply_rag_mode_to_ui(False)
        assert host.code_mode_btn.isChecked() and host.kb_card.isHidden()
        assert host.new_chat_btn.text() == "+ 新对话"

    def test_sync_from_session_by_kind(self, host):
        """侧栏跟随当前会话的 session_kind（会话级）。"""
        kb_sess = _session.Session()
        kb_sess.session_kind = "rag"
        _session.set_active(kb_sess)
        host._sync_rag_sidebar_from_session()
        assert host.kb_mode_btn.isChecked() and not host.kb_card.isHidden()
        _session.set_active(_session.Session())   # 默认 code
        host._sync_rag_sidebar_from_session()
        assert host.code_mode_btn.isChecked() and host.kb_card.isHidden()

    def test_generating_blocks_mode_switch(self, host):
        """生成期间拒绝切换工作区，并恢复正确的选中态（不改会话类型）。"""
        active = _session.get_active()          # 默认 code
        active.is_generating = True
        try:
            host._set_ui_mode("kb")
        finally:
            active.is_generating = False
        assert active.session_kind == "code"
        assert host.code_mode_btn.isChecked() and not host.kb_mode_btn.isChecked()
        assert any("生成中" in t for t in host.toasts)


class TestWorkspaceHelpers:
    """工作区 helper 的纯逻辑（不激活 UI）：会话构造 / 模型兼容 / 最近会话选取 / 锚点校验。"""

    def test_create_code_session(self, host):
        s = host.create_session_for_kind("code")
        assert s.session_kind == "code" and s.rag_mode is False

    def test_create_rag_session_decoupled(self, host, monkeypatch, tmp_path):
        from src import config
        kb = tmp_path / "kb"
        kb.mkdir()
        monkeypatch.setattr(config, "RAG_KB_DIR", str(kb))
        s = host.create_session_for_kind("rag")
        assert s.session_kind == "rag" and s.rag_mode is True
        assert s.project is None                       # 与代码项目彻底解耦
        assert s.rag_kb_dir                             # 锚定当前知识库目录（规范化）

    def test_rag_session_avoids_claude_code(self, host, monkeypatch):
        import src.agent as _agent
        # 造一个 Claude Code 在前、API 模型在后的列表
        fake = [("Claude Code", "claude-code", "claude", False),
                ("Qwen", "cloud", "qwen-max", False)]
        monkeypatch.setattr(_agent, "MODEL_LIST", fake)
        _session.get_active().current_model_index = 0   # 当前是 CC
        s = host.create_session_for_kind("rag")
        assert fake[s.current_model_index][1] != "claude-code"   # 换成了兼容模型

    def test_recent_of_kind_prefers_remembered_unsaved(self, host):
        s = _session.Session()
        s.session_kind = "rag"
        _session.register(s)                             # 无 id 的未存盘空会话
        host._last_ws_key["rag"] = s.key
        try:
            sid, live = host._recent_of_kind("rag")
            assert sid is None and live is s             # 未存盘 → 返回内存对象
        finally:
            _session.drop(s.key)

    def test_check_anchor_warns_on_mismatch(self, host, monkeypatch, tmp_path):
        from src import config
        monkeypatch.setattr(config, "RAG_KB_DIR", str(tmp_path / "A"))
        s = _session.Session()
        s.session_kind = "rag"
        s.rag_kb_dir = "/some/other/kb"
        host.toasts.clear()
        host._check_rag_session_anchor(s)
        assert any("知识库与当前配置不同" in t for t in host.toasts)


class TestKbStatusStates:
    def test_unconfigured(self, host, monkeypatch):
        from src import config
        monkeypatch.setattr(config, "RAG_KB_DIR", "")
        host._refresh_kb_status()
        assert host.kb_status_label.property("kbState") == "none"
        assert "未配置" in host.kb_status_label.text()

    def test_available_with_chunks(self, host, monkeypatch, tmp_path):
        from src import config
        import src.rag.retriever as rmod
        kb = tmp_path / "产品资料库"
        kb.mkdir()
        monkeypatch.setattr(config, "RAG_KB_DIR", str(kb))
        monkeypatch.setattr(rmod, "index_status",
                            lambda **kw: {"chunks": 11, "ok": True, "reason": ""})
        host._refresh_kb_status()
        assert host.kb_status_label.property("kbState") == "ok"
        assert "11" in host.kb_status_label.text()
        assert str(kb) in host.kb_status_label.toolTip()   # 悬停提示完整路径

    def test_needs_rebuild(self, host, monkeypatch, tmp_path):
        from src import config
        import src.rag.retriever as rmod
        kb = tmp_path / "kbdir"
        kb.mkdir()
        monkeypatch.setattr(config, "RAG_KB_DIR", str(kb))
        monkeypatch.setattr(rmod, "index_status",
                            lambda **kw: {"chunks": 5, "ok": False, "reason": "目录已切换，需重建索引"})
        host._refresh_kb_status()
        assert host.kb_status_label.property("kbState") == "stale"

    def test_rebuild_failed_state(self, host):
        host.bridge.kb_status.emit("error", "重建失败: boom", True)
        assert host.kb_status_label.property("kbState") == "error"
        assert "重建失败" in host.kb_status_label.toolTip()


class TestReindexFlow:
    def _setup(self, host, monkeypatch, tmp_path, reindex_fn):
        from src import config
        import src.rag.index as idx_mod
        kb = tmp_path / "kb"
        kb.mkdir()
        monkeypatch.setattr(config, "RAG_KB_DIR", str(kb))
        monkeypatch.setattr(config, "RAG_EMBED_API_KEY", "k")
        monkeypatch.setattr(idx_mod, "reindex", reindex_fn)
        # 同步假线程：threading 是方法内 import 的模块，patch 全局 threading.Thread
        import threading
        monkeypatch.setattr(threading, "Thread", _FakeThread)
        return kb

    def test_buttons_disabled_during_and_restored_after(self, host, monkeypatch, tmp_path):
        """重建期间两个管理按钮禁用 + 状态为「索引中」；结束后恢复。"""
        seen = {}

        def _fake_reindex(kb_dir, **kw):
            seen["dir_btn"] = host.kb_dir_btn.isEnabled()
            seen["reindex_btn"] = host.kb_reindex_btn.isEnabled()
            seen["state"] = host.kb_status_label.property("kbState")
            seen["flag"] = host._kb_reindexing
            return {"files": 1, "chunks": 2, "embedded": 2, "reused": 0}

        self._setup(host, monkeypatch, tmp_path, _fake_reindex)
        import src.rag.retriever as rmod
        monkeypatch.setattr(rmod, "index_status",
                            lambda **kw: {"chunks": 2, "ok": True, "reason": ""})
        host._kb_reindex()
        assert seen == {"dir_btn": False, "reindex_btn": False, "state": "indexing", "flag": True}
        # 结束后：按钮恢复、状态经 index_status 校验为可用
        assert host.kb_dir_btn.isEnabled() and host.kb_reindex_btn.isEnabled()
        assert host._kb_reindexing is False
        assert host.kb_status_label.property("kbState") == "ok"
        assert any("完成" in t for t in host.toasts)

    def test_failure_restores_buttons_with_error_state(self, host, monkeypatch, tmp_path):
        def _boom(kb_dir, **kw):
            raise RuntimeError("embed down")
        self._setup(host, monkeypatch, tmp_path, _boom)
        host._kb_reindex()
        assert host.kb_dir_btn.isEnabled() and host.kb_reindex_btn.isEnabled()
        assert host.kb_status_label.property("kbState") == "error"

    def test_stale_config_marks_expired(self, host, monkeypatch, tmp_path):
        """重建期间配置被改 → 结果标过期（stale），不冒充成功。"""
        from src import config

        def _fake_reindex(kb_dir, **kw):
            config.RAG_KB_DIR = str(tmp_path / "other")   # 模拟期间改配置
            return {"files": 1, "chunks": 1, "embedded": 1, "reused": 0}

        self._setup(host, monkeypatch, tmp_path, _fake_reindex)
        host._kb_reindex()
        assert host.kb_status_label.property("kbState") == "stale"
        assert host.kb_dir_btn.isEnabled() and host.kb_reindex_btn.isEnabled()

    def test_pick_dir_blocked_while_reindexing(self, host):
        host._kb_reindexing = True
        host._kb_pick_dir()
        assert any("重建中" in t for t in host.toasts)


class TestHeaderCleanup:
    def test_header_no_longer_creates_kb_controls(self):
        """顶栏不再创建旧知识库控件（迁移后无隐藏重复 UI）。"""
        path = os.path.join(os.path.dirname(__file__), "..", "src", "ui", "header.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for marker in ("kb_dir_btn", "kb_reindex_btn", "kb_status_label",
                       "uimode_seg", "code_mode_btn", "kb_mode_btn",
                       "_set_ui_mode", "_kb_pick_dir", "_kb_reindex(", "_refresh_kb_status"):
            assert marker not in src, f"header.py 仍残留知识库控件/方法: {marker}"


class _SidebarHost(SidebarMixin, QWidget):
    """最小宿主：只为测 _show_project_menu 的 rag 早退守卫（编码路径需完整 ChatUI）。"""

    def __init__(self):
        super().__init__()
        self.toasts = []

    def _show_toast(self, msg):
        self.toasts.append(msg)


class TestProjectMenuGuard:
    """P1#2：知识库模式禁用项目菜单——避免切项目产出 code+RAG 的混合会话。"""

    def test_rag_session_blocks_project_menu(self, qapp):
        host = _SidebarHost()
        rag = _session.Session()
        rag.session_kind = "rag"
        _session.set_active(rag)
        try:
            host._show_project_menu()          # rag → 早退，不建/不 exec 菜单
        finally:
            _session.set_active(_session.Session())   # 复位为默认 code
        assert any("知识库模式" in t for t in host.toasts)


class TestRagCollapseKey:
    """P2#3：知识库对话与编码历史会话都以 project_path=None 渲染，折叠状态必须独立。"""

    def test_rag_group_uses_distinct_collapse_key(self, qapp, monkeypatch):
        from PySide6.QtWidgets import QVBoxLayout, QWidget
        import src.ui.sidebar as sb

        calls = []

        class _H(SidebarMixin, QWidget):
            def __init__(self):
                super().__init__()
                self.history_widget = QWidget()
                self.history_layout = QVBoxLayout(self.history_widget)

            def _render_project_group(self, project_path, project_name, sessions,
                                      is_active, collapse_key=None):
                calls.append((project_name, project_path, collapse_key))

        monkeypatch.setattr(sb.agent, "list_sessions", lambda *a, **k: [])
        h = _H()
        rag = _session.Session()
        rag.session_kind = "rag"
        _session.set_active(rag)
        try:
            h._refresh_session_list()
        finally:
            _session.set_active(_session.Session())
        # RAG 组：project_path=None（用圆点/无项目语义），但 collapse_key 独立、非 None
        assert calls == [("知识库对话", None, h._RAG_FOLD_KEY)]
        assert h._RAG_FOLD_KEY is not None       # 与编码「历史会话」的 None 键不相撞


class TestSettingsBoolParsing:
    def test_string_false_does_not_check_rerank(self, qapp):
        """设置页必须与 config 运行时共用严格布尔解析，不能让 bool("false") 开启重排。"""
        from src.ui.settings_dialog import SettingsDialog

        class _SettingsHost:
            config = {"rag": {"rerank": "false"}}

            def __init__(self):
                self.fields = {}

            _get_nested = SettingsDialog._get_nested

        holder = QWidget()
        layout = QVBoxLayout(holder)
        host = _SettingsHost()
        SettingsDialog._add_bool(host, layout, "rag.rerank", "启用重排")
        assert host.fields["rag.rerank"].isChecked() is False
