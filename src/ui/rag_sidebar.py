"""侧栏「编码 / 知识库」模式入口 + 知识库管理卡片（mixin for ChatUI）。

位置：侧栏 Logo 下方、"+ 新对话"按钮上方。编码模式只显示模式段控；知识库模式
展开管理卡片（状态行 + 选择目录 / 重建索引）。业务槽函数从 header.py 整体迁入
（顶栏不再有任何知识库控件）。

卡片六态（kbState 属性驱动卡片底色 / 边框 / 文字颜色，见 theme.py 的 #kbCard 段；
设计稿「左侧栏：编码与知识库两个工作区」）：
  none=还没选目录（只给「选择目录」主按钮）/ reading=读取中 / ok=就绪·N 块 /
  stale=需要重建（「重建索引」变主按钮）/ indexing=索引中（忙碌条，按钮禁用）/
  error=读取或重建失败（「重建索引」改叫「重试」）

线程规则：重建在后台线程跑，完成经 bridge.kb_status(state, text, done) 信号回到
主线程更新控件（worker 绝不直接碰 Qt 控件）。读状态（index_status）同样在后台线程：
第一次要加载向量库、打开索引，约 1.8s，放在主线程上第一次切到知识库会卡住整个界面
（实测）。结果经 bridge.kb_status_read 回来，带请求序号——只认最新一次，旧结果
（期间换了目录 / 开始重建）直接丢弃。启动后空闲时预热一次（_prewarm_kb_status）。

依赖宿主：self.bridge / self._show_toast / self.new_chat_btn（sidebar 建）
"""
import os

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from .. import agent
from . import dialogs


class RagSidebarMixin:
    """侧栏模式入口 + 知识库管理卡片 + RAG 模式切换/重建索引逻辑。"""

    # ── 构造（由 sidebar._build_sidebar 在 Logo 之后调用）──

    def _build_rag_sidebar(self, parent_layout):
        self._kb_reindexing = False
        # 分别记住最近一次活动的 code / rag 会话 key，切回该工作区时优先恢复它
        self._last_ws_key = {"code": None, "rag": None}

        # 编码 | 知识库 工作区切换：撑满侧栏可用宽度，带图标
        self.uimode_seg = QWidget()
        self.uimode_seg.setObjectName("ragModeSeg")
        self.uimode_seg.setAttribute(Qt.WA_StyledBackground, True)
        seg_lay = QHBoxLayout(self.uimode_seg)
        seg_lay.setContentsMargins(3, 3, 3, 3)
        seg_lay.setSpacing(3)
        self.code_mode_btn = QPushButton("编码")
        self.kb_mode_btn = QPushButton("知识库")
        for _b in (self.code_mode_btn, self.kb_mode_btn):
            _b.setCheckable(True)
            _b.setCursor(Qt.PointingHandCursor)
            _b.setProperty("class", "segBtn")
            _b.setFixedHeight(30)
            _b.setIconSize(QSize(15, 15))
            seg_lay.addWidget(_b, 1)          # 平分宽度
        self.code_mode_btn.setChecked(True)
        self.code_mode_btn.setToolTip("编码模式：正常的 AI 编码助手")
        self.kb_mode_btn.setToolTip("知识库模式：对本地资料库做 RAG 检索问答")
        self.code_mode_btn.clicked.connect(lambda: self._set_ui_mode("code"))
        self.kb_mode_btn.clicked.connect(lambda: self._set_ui_mode("kb"))
        parent_layout.addWidget(self.uimode_seg)

        # 知识库卡片（仅知识库模式可见）：标题行（图标 + 库名 + 状态词）/ 说明 / 忙碌条 / 按钮
        self.kb_card = QWidget()
        self.kb_card.setObjectName("kbCard")
        self.kb_card.setAttribute(Qt.WA_StyledBackground, True)
        card_lay = QVBoxLayout(self.kb_card)
        card_lay.setContentsMargins(12, 11, 12, 12)
        card_lay.setSpacing(8)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.kb_card_icon = QLabel()
        self.kb_card_icon.setFixedSize(16, 16)
        self.kb_card_name = QLabel()
        self.kb_card_name.setObjectName("kbCardName")
        self.kb_card_name.setMinimumWidth(0)
        self.kb_card_state = QLabel()
        self.kb_card_state.setObjectName("kbCardState")
        head.addWidget(self.kb_card_icon)
        head.addWidget(self.kb_card_name, 1)
        head.addWidget(self.kb_card_state)
        card_lay.addLayout(head)

        self.kb_status_label = QLabel()          # 说明行（N 块 / 原因 / 引导），测试按它判状态
        self.kb_status_label.setObjectName("kbCardStatus")
        self.kb_status_label.setWordWrap(True)
        card_lay.addWidget(self.kb_status_label)

        self.kb_busy_bar = QProgressBar()
        self.kb_busy_bar.setObjectName("kbCardBusy")
        self.kb_busy_bar.setRange(0, 0)          # 不确定进度：重建只在完成时回报，不画假百分比
        self.kb_busy_bar.setTextVisible(False)
        self.kb_busy_bar.setFixedHeight(4)
        self.kb_busy_bar.setVisible(False)
        card_lay.addWidget(self.kb_busy_bar)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(6)
        self.kb_dir_btn = QPushButton("选择目录")
        self.kb_dir_btn.setToolTip("选择知识库目录（放 .md / .pdf 的文件夹）")
        self.kb_dir_btn.clicked.connect(self._kb_pick_dir)
        self.kb_reindex_btn = QPushButton("重建索引")
        self.kb_reindex_btn.setToolTip(
            "扫描目录下所有 .md / .pdf，切块 + embedding 建索引\n"
            "顶层子目录名即分域，检索时各域按配额取名额，互不挤占")
        self.kb_reindex_btn.clicked.connect(self._kb_reindex)
        for _b in (self.kb_dir_btn, self.kb_reindex_btn):
            _b.setCursor(Qt.PointingHandCursor)
            _b.setProperty("class", "kbCardBtn")
            _b.setFixedHeight(30)
            btn_row.addWidget(_b, 1)
        card_lay.addLayout(btn_row)

        self.kb_card.setVisible(False)        # 默认编码模式
        parent_layout.addWidget(self.kb_card)

    def _style_mode_seg_icons(self):
        """段控图标随选中态和主题换色（QSS 管不到图标颜色）。"""
        for btn, icon in ((self.code_mode_btn, "code_lucide.svg"),
                          (self.kb_mode_btn, "book-open.svg")):
            color = self._t("sb_seg_active_text" if btn.isChecked() else "sb_seg_idle_text")
            btn.setIcon(self._svg_icon(icon, color))
        if hasattr(self, "kb_card_icon") and self.kb_card.property("kbState"):
            self._paint_kb_icon(self.kb_card.property("kbState"))

    def _paint_kb_icon(self, state_key):
        if not hasattr(self, "_svg_icon"):
            return                      # 轻量宿主（测试）没有主题图标
        color ={"stale": "kb_stale_text", "error": "kb_error_text"}.get(state_key, "sb_accent")
        icon = "triangle-alert.svg" if state_key == "error" else "book.svg"
        pm = self._svg_icon(icon, self._t(color)).pixmap(QSize(16, 16))
        self.kb_card_icon.setPixmap(pm)

    def _check_rag_session_anchor(self, sess):
        """加载 rag 历史会话时校验它的知识库锚点：与当前配置目录不同时明确提示，
        绝不静默改全局目录、也不静默用别的库回答历史会话。刷新卡片状态。"""
        self._refresh_kb_status()
        anchor = getattr(sess, "rag_kb_dir", "") or ""
        if not anchor:
            return
        if anchor != self._normalized_kb_dir():
            self._show_toast("该对话原本使用的知识库与当前配置不同，请切换目录或新建知识库对话")

    # ── 状态行渲染 ──

    _KB_STATE_WORDS = {"ok": "就绪", "reading": "读取中…", "indexing": "索引中…",
                       "stale": "需要重建", "error": "出错了", "none": ""}

    def _set_kb_status(self, state_key: str, text: str, tooltip: str = "", *, word: str = ""):
        """更新知识库卡片：kbState 属性驱动配色（卡片和说明行都挂），说明行放 text。
        none 态没有库名：标题改成引导语、只留「选择目录」主按钮。"""
        from .. import config
        card = self.kb_card
        for w in (card, self.kb_status_label):
            w.setProperty("kbState", state_key)
        kb = config.RAG_KB_DIR or ""
        if state_key == "none":
            self.kb_card_name.setText("还没选知识库目录")
            self.kb_card_name.setToolTip("")
            self.kb_card_icon.setVisible(False)
        else:
            name = os.path.basename(kb.rstrip("/\\")) or kb
            fm = QFontMetrics(self.kb_card_name.font())
            avail = max(80, card.width() - 130) if card.width() > 40 else 120
            self.kb_card_name.setText(fm.elidedText(name, Qt.ElideRight, avail))
            self.kb_card_name.setToolTip(kb)
            self.kb_card_icon.setVisible(True)
            self._paint_kb_icon(state_key)
        self.kb_card_state.setText(word or self._KB_STATE_WORDS.get(state_key, ""))
        self.kb_status_label.setText(text)
        self.kb_status_label.setToolTip(tooltip or text)
        self.kb_busy_bar.setVisible(state_key == "indexing")
        self.kb_reindex_btn.setVisible(state_key != "none")
        self.kb_reindex_btn.setText("重试" if state_key == "error" else "重建索引")
        self.kb_reindex_btn.setProperty("primary", "true" if state_key == "stale" else "false")
        self.kb_dir_btn.setProperty("primary", "true" if state_key == "none" else "false")
        for w in [card] + card.findChildren(QWidget):
            w.style().unpolish(w)
            w.style().polish(w)

    def _refresh_kb_status(self):
        """按 index_status（与检索同一套锚校验）刷新状态行——索引若是旧目录/旧模型建的，
        显示「需要重建」而不是把当前目录名和旧索引块数拼在一起误导用户。

        读取在后台线程（见模块说明）：先显示「读取中…」，结果回来再更新。"""
        if not hasattr(self, "kb_status_label"):
            return
        from .. import config
        kb = config.RAG_KB_DIR
        self._kb_status_seq = getattr(self, "_kb_status_seq", 0) + 1
        if not kb:
            self._set_kb_status("none", "指向一个放 .md / .pdf 的文件夹，灵犀会建索引，"
                                        "回答时引用里面的资料。",
                                "点「选择目录」指向存放 .md / .pdf 的文件夹")
            return
        if getattr(self, "_kb_reindexing", False):
            return                      # 重建中：状态行归重建进度管，完成后会再刷新
        snap = self._kb_status_key()
        cached = getattr(self, "_kb_status_cache", None)
        if cached and cached[0] == snap:
            # 同一目录、同一套配置读过：先直接显示上次的结果（不闪「读取中」），后台再静默核对
            self._apply_kb_status(kb, cached[1])
        else:
            self._set_kb_status("reading", "正在读取索引…", kb)
        self._read_kb_status_async(self._kb_status_seq, kb, snap)

    def _kb_status_key(self):
        from .. import config
        return (config.RAG_KB_DIR, config.RAG_EMBED_MODEL, config.RAG_EMBED_BASE_URL,
                config.RAG_CHUNK_SIZE, config.RAG_CHUNK_OVERLAP)

    def _read_kb_status_async(self, seq, kb, key):
        snap = dict(zip(("kb_dir", "embed_model", "embed_base_url", "chunk_size", "chunk_overlap"),
                        key))
        bridge = self.bridge
        import threading

        def _work():
            try:
                from ..rag.retriever import index_status
                st = index_status(**snap)
            except Exception as e:
                st = f"{type(e).__name__}: {e}"
            bridge.kb_status_read.emit(seq, kb, st)

        threading.Thread(target=_work, daemon=True, name="kb-status").start()

    def _prewarm_kb_status(self):
        """启动后空闲时读一次（加载向量库），第一次切到知识库就不用等。界面不在知识库也照样
        读——结果只是落到隐藏的状态行上。"""
        from .. import config
        if config.RAG_KB_DIR and hasattr(self, "kb_status_label") \
                and not getattr(self, "_kb_status_seq", 0):
            self._refresh_kb_status()

    def _on_kb_status_read(self, seq, kb, st):
        """bridge.kb_status_read 槽（主线程）：只认最新一次请求的结果。"""
        if not hasattr(self, "kb_status_label") or seq != getattr(self, "_kb_status_seq", 0):
            return
        if getattr(self, "_kb_reindexing", False):
            return
        from .. import config
        if kb != config.RAG_KB_DIR:
            return
        if isinstance(st, dict):
            self._kb_status_cache = (self._kb_status_key(), st)
        self._apply_kb_status(kb, st)

    def _apply_kb_status(self, kb, st):
        if not isinstance(st, dict):
            self._set_kb_status("error", f"状态读取失败：{st}", f"{st}\n{kb}", word="读取失败")
            return
        if st["ok"]:
            # 分域信息进 tooltip：多域库检索时按域配额取，用户该能一眼看到库里分了哪几域
            cats = st.get("categories") or []
            tip = kb if len(cats) <= 1 else f"{kb}\n分域：{'、'.join(cats)}"
            detail = f"{st['chunks']} 块"
            if len(cats) > 1:
                detail += f" · {len(cats)} 个分域：{'、'.join(cats)}"
            self._set_kb_status("ok", detail, tip)
        else:
            self._set_kb_status("stale", f"{st['reason']}。",
                                f"{st['reason']}\n{kb}")

    # ── 工作区切换（编码 / 知识库）──
    # 左侧段控是【工作区切换器】：切到"另一个会话"，不是改当前会话的 session_kind。

    def _set_ui_mode(self, mode):
        """段控点击 → 切换工作区。kind: code / rag。"""
        from .. import session as _session
        kind = "rag" if mode == "kb" else "code"
        active = _session.get_active()
        # 当前活动会话正在生成时禁止切工作区，恢复段控选中态到当前工作区
        if active.is_generating:
            self._show_toast("生成中不能切换编码/知识库工作区，请先停止")
            self._sync_rag_sidebar_from_session()
            return
        if getattr(active, "session_kind", "code") == kind:
            self._sync_rag_sidebar_from_session()   # 已在该工作区，纠正误点的选中态
            return
        self.switch_workspace(kind)

    def switch_workspace(self, kind: str):
        """切到目标工作区：保存并记住当前会话 → 恢复该工作区最近会话（无则建新空会话）。
        不修改任何现有会话的 session_kind。"""
        from .. import session as _session
        active = _session.get_active()
        self._clear_sidebar_search()               # 切工作区清掉跨会话搜索（kind 变了，旧结果失效）
        agent.save_session()                       # 保存当前会话活动状态
        self._remember_ws_active(active)           # 记住它作为其工作区的最近会话
        sid, sess = self._recent_of_kind(kind)
        if sess is not None:
            self._activate_session(sess)           # 内存里的会话（可能未存盘的空会话）
        elif sid is not None:
            self._load_session(sid)                # 已存盘 → 走完整加载路径（含重绘）
        else:
            self._activate_session(self.create_session_for_kind(kind))
        self._show_toast("知识库工作区" if kind == "rag" else "编码工作区")

    def _remember_ws_active(self, sess):
        if sess is not None and getattr(sess, "key", None):
            self._last_ws_key[getattr(sess, "session_kind", "code")] = sess.key

    def _recent_of_kind(self, kind: str):
        """返回 (已存盘会话 id | None, 内存 Session | None)：该工作区最近会话。
        优先记住的最近活动会话（可能是未存盘空会话）→ 否则 index 里该 kind 最近更新的一条。"""
        from .. import session as _session
        key = self._last_ws_key.get(kind)
        if key:
            s = _session.get(key)
            if s is not None and getattr(s, "session_kind", "code") == kind:
                sid = getattr(s, "current_session_id", None)
                # 已存盘（含后台生成中）→ 走 _load_session 完整路径（重绘/render_log/确认卡）；
                # 未存盘的空会话没有 id，只能直接激活内存对象。
                return (sid, None) if sid else (None, s)
        entries = agent.list_sessions("__all__", kind=kind)
        if entries:
            entries.sort(key=lambda e: e.get("updated", ""), reverse=True)
            return entries[0]["id"], None
        return None, None

    def create_session_for_kind(self, kind: str):
        """按工作区类型建一个新 Session（不激活）。继承 model/mode/思考；
        rag 会话 project=None、锚当前知识库目录、避开 Claude Code 模型。"""
        from .. import session as _session
        from ..roles import get_system_prompt
        from langchain_core.messages import SystemMessage
        _prev = _session.get_active()
        s = _session.Session()
        s.session_kind = kind
        s.current_model_index = _prev.current_model_index
        s.agent_mode = _prev.agent_mode
        from .prefs import inherited_permission_mode
        s.permission_mode = inherited_permission_mode(_prev)
        s.reasoning_effort = _prev.reasoning_effort
        if kind == "rag":
            s.rag_mode = True
            s.project = None                       # 与代码项目彻底解耦，不继承
            s.rag_kb_dir = self._normalized_kb_dir()
            s.current_model_index = self._compatible_model_index(_prev.current_model_index)
        else:
            s.rag_mode = False
            # code：project 保持 _UNSET，首次 save 时锚定为当时全局项目（现有语义）
        _session.register(s)
        # 系统提示词按目标会话的 rag_mode 生成——临时把 active 切过去再取，避免误读旧会话
        _session.set_active(s)
        s.chat_history.append(SystemMessage(content=get_system_prompt()))
        return s

    def _normalized_kb_dir(self) -> str:
        from .. import config
        kb = config.RAG_KB_DIR
        if not kb:
            return ""
        try:
            from ..rag.index import norm_kb_dir
            return norm_kb_dir(kb)
        except Exception:
            return kb

    def _compatible_model_index(self, idx: int) -> int:
        """知识库会话不能用 Claude Code（CLI 无 search_knowledge）。继承到它时换成
        第一个非 CLI 模型；没有兼容模型则原样返回（agent 层仍有硬拦截 + UI 会提示）。"""
        ml = agent.MODEL_LIST
        if 0 <= idx < len(ml) and ml[idx][1] != "claude-code":
            return idx
        for i, m in enumerate(ml):
            if m[1] != "claude-code":
                return i
        return idx

    def _activate_session(self, sess):
        """把一个内存 Session 设为前台并同步全部 UI（新建 / 未存盘会话走这条；
        已存盘历史会话走 _load_session 的完整重绘路径）。"""
        from .. import session as _session
        _prev = _session.get_active()
        if _prev is not None and _prev is not sess:
            # B09a：离开当前会话，挂待启动请求的消息明确取消（输入保留在输入框）
            self._cancel_pending_run(_prev, "已切换到其它会话，待发送的消息已取消（输入保留在输入框）")
            # B09b：旧会话的队列同样暂停（条目保留，切回后明确恢复）
            self._queue_pause_for_switch(_prev)
        _session.set_active(sess)
        sess.needs_redraw = False
        self.chat_area.clear()
        self._reset_render_state()
        # 系统提示词随目标会话（此刻 state.rag_mode == sess.rag_mode）
        try:
            from ..roles import get_system_prompt
            from langchain_core.messages import SystemMessage
            if sess.chat_history and isinstance(sess.chat_history[0], SystemMessage):
                sess.chat_history[0] = SystemMessage(content=get_system_prompt())
        except Exception:
            pass
        if len(sess.chat_history) > 1:
            self._redraw_chat()
            self._scroll_to_bottom()
        else:
            self._show_empty_state()
        self._refresh_session_list()
        self._refresh_queue_panel()      # B09b：队列面板切到新会话的队列
        self._sync_header_from_session()          # 内含 _sync_rag_sidebar_from_session
        self._refresh_project_indicator()
        self._refresh_token_label_from_session()
        self._update_btn_state("enabled" if self._has_input else "disabled")

    def _apply_rag_mode_to_ui(self, is_kb: bool):
        """把 rag_mode 落到侧栏控件：段控选中态 / 卡片可见性 / 新对话文案。
        （纯 UI 状态更新，切工作区与切会话同步共用，不复制业务逻辑）"""
        self.code_mode_btn.setChecked(not is_kb)
        self.kb_mode_btn.setChecked(is_kb)
        self.kb_card.setVisible(is_kb)
        if hasattr(self, "new_chat_btn"):
            self.new_chat_btn.setText("新知识库对话" if is_kb else "新对话")
        if hasattr(self, "_t"):
            self._style_mode_seg_icons()
        # 附件入口：知识库模式暂不支持附件（未做摄取），禁用并提示，绝不静默忽略
        if hasattr(self, "img_btn"):
            self.img_btn.setEnabled(not is_kb)
            self.img_btn.setToolTip(
                "知识库模式暂不支持附件，请先把资料加入知识库目录并重建索引"
                if is_kb else "上传图片 / 导入项目")
        if is_kb:
            self._refresh_kb_status()

    def _sync_rag_sidebar_from_session(self):
        """切会话后把侧栏模式入口/卡片同步到当前会话（按 session_kind，会话级）。"""
        from .. import session as _session
        if not hasattr(self, "code_mode_btn"):
            return
        sess = _session.get_active()
        self._apply_rag_mode_to_ui(getattr(sess, "session_kind", "code") == "rag")

    # ── 目录选择 / 重建索引 ──

    def _kb_pick_dir(self):
        from .. import config
        if getattr(self, "_kb_reindexing", False):
            self._show_toast("索引重建中，暂不能切换目录")
            return
        d = QFileDialog.getExistingDirectory(
            self, "选择知识库目录（放 .md 的文件夹）", config.RAG_KB_DIR or "")
        if not d:
            return
        if config.set_rag_kb_dir(d):
            self._refresh_kb_status()   # 切换目录后立即刷新（通常显示「需要重建」）
            self._show_toast("已设知识库目录，点「重建索引」开始建库")
        else:
            dialogs.critical(self, "知识库目录没保存", "写入 config.json 失败，请检查文件权限。")

    def _kb_reindex(self):
        from .. import config
        if getattr(self, "_kb_reindexing", False):
            return   # 防重复任务（按钮已禁用，双保险）
        if not config.RAG_KB_DIR:
            dialogs.information(self, "知识库", "请先选择知识库目录（选择目录）。")
            return
        if not config.RAG_EMBED_API_KEY:
            dialogs.warning(self, "知识库",
                            "缺 embedding key（默认复用 qwen_api_key），请在 config.json 配置。")
            return
        # 开始前快照全部配置：worker 全程用快照（不读活配置），完成时校验快照是否仍与
        # 当前配置一致——期间配置被改（手工编辑 config 等）则结果标过期，不冒充成功。
        snap = dict(kb_dir=config.RAG_KB_DIR,
                    model=config.RAG_EMBED_MODEL,
                    base_url=config.RAG_EMBED_BASE_URL,
                    api_key=config.RAG_EMBED_API_KEY,
                    chunk_size=config.RAG_CHUNK_SIZE,
                    chunk_overlap=config.RAG_CHUNK_OVERLAP)
        self._kb_reindexing = True
        self._kb_status_seq = getattr(self, "_kb_status_seq", 0) + 1   # 作废在途的状态读取
        self.kb_reindex_btn.setEnabled(False)
        self.kb_dir_btn.setEnabled(False)   # 重建期间禁止切换目录
        self._set_kb_status("indexing", "切走也不会中断，完成后会提示。", snap["kb_dir"])
        import threading

        def _work():
            try:
                from ..rag.index import reindex
                s = reindex(snap["kb_dir"],
                            embed_model=snap["model"],
                            embed_base_url=snap["base_url"],
                            embed_api_key=snap["api_key"],
                            chunk_size=snap["chunk_size"],
                            chunk_overlap=snap["chunk_overlap"])
                if (config.RAG_KB_DIR != snap["kb_dir"]
                        or config.RAG_EMBED_MODEL != snap["model"]
                        or config.RAG_EMBED_BASE_URL != snap["base_url"]):
                    self.bridge.kb_status.emit(
                        "stale", "重建期间配置已变更，该索引已过期，请重新重建", True)
                else:
                    _dup = f"·去重 {s['duplicates']}" if s.get("duplicates") else ""
                    _cats = s.get("categories") or {}
                    _cat_txt = ("　分域：" + "、".join(f"{k} {v}" for k, v in sorted(_cats.items()))
                                if len(_cats) > 1 else "")
                    self.bridge.kb_status.emit(
                        "ok",
                        f"完成：{s['files']} 文件 / {s['chunks']} 块"
                        f"（新 {s['embedded']}·复用 {s['reused']}{_dup}）{_cat_txt}",
                        True)
            except Exception as e:
                self.bridge.kb_status.emit("error", f"重建失败: {e}", True)

        threading.Thread(target=_work, daemon=True).start()

    def _on_kb_status(self, state_key, text, done):
        """bridge.kb_status 槽（主线程）：更新状态行 + 结束时恢复按钮。"""
        if not hasattr(self, "kb_status_label"):
            return
        if done:
            self._kb_reindexing = False
            self._kb_status_cache = None     # 索引变了：缓存的块数作废
            self.kb_reindex_btn.setEnabled(True)
            self.kb_dir_btn.setEnabled(True)
        if done and state_key == "ok":
            # 成功：统计进 toast，状态行用 index_status 重新校验后的真实状态
            self._show_toast(text)
            self._refresh_kb_status()
        else:
            word = "重建失败" if state_key == "error" else ""
            detail = text[len("重建失败: "):] if text.startswith("重建失败: ") else text
            self._set_kb_status(state_key, detail, text, word=word)
