"""顶栏构造 + 按钮样式（mixin for ChatUI）。

抽出来的整块顶栏 + 散落各处的按钮样式代码：

- 顶栏构造：模型选择 / Plan-Act 切换 / 撤销 / 思考 / 角色卡 / 主题切换
- 顶栏响应式：窗口窄到一定宽度时按钮压缩成"图标 + 短词"或纯图标
- 按钮样式：所有 `_style_*_btn` 都在这里（含输入区的 img/mic/tts 按钮）
- 角色卡 UI：加载 / 清除 / 状态恢复

依赖宿主：self._t / self._svg_icon / self.theme / self._toggle_sidebar /
self._toggle_theme / self._show_toast / self._append_html /
self._refresh_session_list / self._refresh_header_compactness
"""
import os

from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QMenu, QMessageBox,
    QPushButton, QSizePolicy, QWidget,
)

from .. import agent
from .. import state
from ..paths import logger
from ._base import BASE_DIR
from .helpers import _make_upload_icon


class HeaderMixin:
    """顶栏 + 全部按钮样式 + 角色卡 UI。"""

    # ── 顶栏构造 ──

    def _build_header(self, parent_layout):
        header = QWidget()
        header.setObjectName("header")
        header.setFixedHeight(72)   # design_handoff：72px 顶栏,更宽松
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 0, 24, 0)
        layout.setSpacing(8)  # 缩紧按钮间距，留点喘息空间给窄窗口

        toggle_btn = QPushButton("☰")
        toggle_btn.setObjectName("toggleBtn")
        toggle_btn.setCursor(Qt.PointingHandCursor)
        toggle_btn.clicked.connect(self._toggle_sidebar)
        layout.addWidget(toggle_btn)

        # 品牌字符 — 灵犀 (KaiTi 笔意，仅夜间主题显示)
        self.header_brand = QLabel("灵犀")
        self.header_brand.setObjectName("headerBrand")
        layout.addWidget(self.header_brand)
        self.header_brand_dot = QLabel("·")
        self.header_brand_dot.setObjectName("headerBrandDot")
        layout.addWidget(self.header_brand_dot)
        # 品牌已移到侧栏（灵 logo + 灵犀 / local & cloud），顶栏默认不重复显示，留空间给控件。
        # 由主题 token brand_visible 控制（当前两主题都为 "false"），与 _apply_theme 同源。
        brand_visible = self._t("brand_visible") == "true"
        self.header_brand.setVisible(brand_visible)
        self.header_brand_dot.setVisible(brand_visible)

        # 模型选择下拉框
        self.model_combo = QComboBox()
        self.model_combo.setCursor(Qt.PointingHandCursor)
        for name, _, _, _ in agent.MODEL_LIST:
            self.model_combo.addItem(name)
        # 跟启动时解析的默认模型（agent 里按 default_model_id 设的 current_model_index）
        # 同步，而不是写死 0（0 是 Claude Code）。在 connect 之前设，不触发回调。
        self.model_combo.setCurrentIndex(agent.current_model_index)
        # 关键:不让下拉框横向膨胀。QComboBox 默认会被 layout 拉伸去填满可用空间(实测能涨到
        # 640px),把后面的 addStretch 吃光、一路顶到撤销按钮 → 顶栏看起来"挤在一起/被挡"。
        # Maximum 策略让它停在 sizeHint(内容宽,受 stylesheet min-width 托底),多余空间归 stretch。
        self.model_combo.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self._style_model_combo()
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        layout.addWidget(self.model_combo)

        layout.addStretch()

        # 撤销按钮：把 AI 上次对文件的改动用 git stash 复原。无 checkpoint 时按钮禁用
        self.undo_btn = QPushButton("↶ 撤销")
        self.undo_btn.setCursor(Qt.PointingHandCursor)
        self.undo_btn.setToolTip("撤销 AI 最近一次有完整记录的文件修改（恢复写前内容）\n仅当前会话与当前工作区")
        self.undo_btn.clicked.connect(self._on_undo_click)
        self._style_undo_btn()
        layout.addWidget(self.undo_btn)

        # 隔离模式按钮（Git worktree 保护主目录）
        self.isolation_btn = QPushButton("隔离")
        self.isolation_btn.setCursor(Qt.PointingHandCursor)
        self.isolation_btn.setToolTip("隔离模式：AI 在独立 worktree 目录操作，不影响主项目\n需项目已启用版本控制")
        self.isolation_btn.clicked.connect(self._toggle_isolation)
        self._style_isolation_btn(active=False)
        self.isolation_btn.setVisible(False)  # 无项目时隐藏
        layout.addWidget(self.isolation_btn)

        # 计划 / 执行 段控（执行=Act 默认；计划=Plan 时 AI 只调研不动手）。
        # 两个 checkable 按钮装进 #modeSeg 容器，选中态由 QSS :checked 驱动（见 theme.py）。
        self.mode_seg = QWidget()
        self.mode_seg.setObjectName("modeSeg")
        # 关键:纯 QWidget 在 QHBoxLayout 里竖向默认 Preferred 会被拉伸填满 56px 顶栏高,
        # 变成一个高灰块（QPushButton 竖向 Fixed 不会）。设 Fixed + 居中 → 收成和兄弟钮齐平的小药丸。
        self.mode_seg.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        _seg_lay = QHBoxLayout(self.mode_seg)
        _seg_lay.setContentsMargins(3, 3, 3, 3)
        _seg_lay.setSpacing(3)
        self.plan_btn = QPushButton("计划")
        self.act_btn = QPushButton("执行")
        for _b in (self.plan_btn, self.act_btn):
            _b.setCheckable(True)
            _b.setCursor(Qt.PointingHandCursor)
            _b.setProperty("class", "segBtn")
            _seg_lay.addWidget(_b)
        self.act_btn.setChecked(True)
        self.plan_btn.setToolTip("计划模式：AI 只调研给方案，不动手改东西")
        self.act_btn.setToolTip("执行模式：AI 可直接执行工具、改文件")
        self.plan_btn.clicked.connect(lambda: self._set_agent_mode("plan"))
        self.act_btn.clicked.connect(lambda: self._set_agent_mode("act"))
        layout.addWidget(self.mode_seg, 0, Qt.AlignVCenter)
        # 注：「编码/知识库」模式入口和知识库管理控件在左侧栏（rag_sidebar.py），顶栏不放

        # 思考模式开关
        self.think_btn = QPushButton("思考")
        self.think_btn.setCursor(Qt.PointingHandCursor)
        self.think_btn.setCheckable(True)
        self.think_btn.setChecked(True)
        self._style_think_btn()
        self.think_btn.toggled.connect(lambda _: self._style_think_btn())
        self.think_btn.clicked.connect(self._toggle_thinking)
        layout.addWidget(self.think_btn)

        # 角色卡按钮
        self.role_btn = QPushButton("角色卡")
        self.role_btn.setCursor(Qt.PointingHandCursor)
        self._style_role_btn(active=False)
        self.role_btn.clicked.connect(self._load_role_card)
        layout.addWidget(self.role_btn)

        # 主题切换按钮（文字显示当前主题：浅色 / 深色）
        self.theme_btn = QPushButton("浅色" if self.theme == "light" else "深色")
        self.theme_btn.setObjectName("themeBtn")
        self.theme_btn.setCursor(Qt.PointingHandCursor)
        self.theme_btn.setToolTip("切到夜间模式" if self.theme == "light" else "切到白天模式")
        self.theme_btn.clicked.connect(self._toggle_theme)
        layout.addWidget(self.theme_btn)

        parent_layout.addWidget(header)

    # ── 顶栏响应式 ──

    def _refresh_header_compactness(self):
        """窗口窄到一定宽度时，把顶栏按钮压成"图标 + 短文字" / "纯图标"两档，避免互相重叠。

        阈值：
          - >= 1100 px：正常模式，全文字
          - 900 ~ 1100：紧凑模式，关键按钮（角色卡 / 思考 / Act / 撤销）只显示图标 + 短词
          - < 900     ：超紧凑，纯图标
        """
        # 用【顶栏自己的宽度】而非窗口宽度判折叠:顶栏在侧栏右侧,侧栏开着时它的可用宽度
        # = 窗口 − 侧栏(~280px)。原来按 self.width()(整窗)算,侧栏一开顶栏区域明明很窄、
        # 却仍判成"宽"不折叠 → 按钮在窄区域里挤成一团/被挡。header.width() 反映真实可用宽。
        w = self.width()
        _hdr = self.model_combo.parentWidget() if hasattr(self, "model_combo") else None
        if _hdr is not None and _hdr.width() > 0:
            w = _hdr.width()
        # 阈值偏大些,让按钮在挤之前就先折叠(模型框 + 撤销/隔离/Act/思考/角色卡 一排)。
        if w >= 1080:
            level = 0   # 正常
        elif w >= 860:
            level = 1   # 紧凑
        else:
            level = 2   # 超紧凑

        # think_btn —— 带开/关状态词；超紧凑只留图标
        if hasattr(self, "think_btn"):
            if level == 2:
                self.think_btn.setText("")
            else:
                self.think_btn.setText("思考 开" if self.think_btn.isChecked() else "思考 关")
            self.think_btn.setToolTip("思考模式：让模型显式输出 reasoning 过程")
        # 段控（计划|执行）始终显示两字短词，无需折叠
        # undo_btn
        if hasattr(self, "undo_btn"):
            if level == 0:
                self.undo_btn.setText("↶ 撤销")
            elif level == 1:
                self.undo_btn.setText("↶")
            else:
                self.undo_btn.setText("↶")
        # isolation_btn
        if hasattr(self, "isolation_btn") and self.isolation_btn.isVisible():
            from .. import session as _sess
            active = _sess.get_active()
            if active.worktree:
                self.isolation_btn.setText("" if level == 2 else "恢复")
            else:
                self.isolation_btn.setText("" if level == 2 else "隔离")
        # role_btn：显示「角色：<名>」，无角色时「角色：默认助手」
        if hasattr(self, "role_btn"):
            name = agent.get_current_role_name()
            if level >= 1 and name and len(name) > 4:
                self.role_btn.setText(f"角色：{name[:4]}")
                self.role_btn.setToolTip(f"当前角色：{name}")
            else:
                self.role_btn.setText(f"角色：{name}" if name else "角色：默认助手")
                self.role_btn.setToolTip("")
        # model_combo 宽度按档位约束。关键是 setMaximumWidth 硬上限:QComboBox 的 sizeHint
        # 取【下拉列表里最长的模型名】,会把框撑到 ~350px(哪怕当前选中的是短名),挤占右侧
        # 按钮。设硬上限后超长当前项用省略号显示,框不再当空间黑洞。
        if hasattr(self, "model_combo"):
            min_w = 280 if level == 0 else (180 if level == 1 else 130)
            max_w = 300 if level == 0 else (220 if level == 1 else 180)
            ss = self.model_combo.styleSheet()
            import re as _re
            ss = _re.sub(r"min-width:\s*\d+px;", f"min-width: {min_w}px;", ss)
            self.model_combo.setStyleSheet(ss)
            self.model_combo.setMaximumWidth(max_w)

    # ── 顶栏按钮交互 ──

    def _on_model_changed(self, index):
        from .. import session as _session
        if _session.get_active().is_generating:
            self._force_stop_generation()
        agent.switch_model(index)
        # 用户亲手选的模型：「继续任务」的预检据此不再改回上一轮记录的模型（B04）。
        # 切会话时的顶栏同步走 blockSignals，不经过这里，所以不会误标。
        _session.get_active().model_user_choice = True
        # 根据模型是否支持思考，更新开关状态
        _, _, _, supports_think = agent.MODEL_LIST[index]
        self.think_btn.setEnabled(supports_think)
        if not supports_think:
            self.think_btn.setChecked(False)
            agent.set_reasoning(False)
        self._show_current_model_config_warning()

    def _toggle_thinking(self):
        enabled = self.think_btn.isChecked()
        agent.set_reasoning(enabled)

    def _on_undo_click(self):
        """撤销按钮（B11a）：只撤当前会话、当前实际工作区里最近一次有完整记录的
        文件写入。全程走 file_history 的归属与现场校验，不取全局栈顶误撤其它会话。

        当前会话的 worker 真正退出前不执行（等待复用继续任务的轮询参数，超时取消、
        不到点硬做）；等待与执行期间占用 resume_pending，让既有的运行准入闸挡住
        新一轮发送 / 遥控 / 重试 / 队列派发，避免恢复与队列派发同时写入。
        """
        from .. import file_history as _fh
        from .. import session as _session
        sess = _session.get_active()
        if getattr(sess, "resume_pending", False):
            return      # 继续/撤销/任务切换正持有屏障：连点不叠加
        sid = getattr(sess, "current_session_id", None) or ""
        root = _fh.session_workspace()
        cand = _fh.latest_undoable(sid, root) if sid else None
        if cand is None:
            self._show_toast("当前会话没有可撤销的 AI 文件改动", 2500)
            self._style_undo_btn()
            return
        pre = _fh.precheck(cand["checkpoint_id"], session_id=sid, workspace=root)
        if pre["status"] != _fh.ST_RESTORABLE:
            # 材料与记录原样保留；原因里说明是冲突还是不支持
            self._show_toast(f"⚠ {pre['reason']}", 5000)
            self._style_undo_btn()
            return
        if self._pending_run_for(sess) is not None:
            # 有一条待发送的消息也在等旧 worker 退出：先处理消息，否则撤销一结束
            # 它就被接纳，两条写入路径撞在同一个会话上。
            self._show_toast("有消息正在等待上一轮结束，请先处理再撤销", 3000)
            return
        sess.resume_pending = True
        try:
            if self._worker_alive(sess):
                self._undo_wait(sess, cand["checkpoint_id"], sid, 0)
                return
            self._perform_undo(sess, cand["checkpoint_id"], root)
        except Exception:
            sess.resume_pending = False
            raise

    def _undo_wait(self, sess, checkpoint_id, sid, attempts):
        """屏障：旧 worker **真正退出**后才撤销。非阻塞轮询（带 receiver 上下文的
        QTimer），超时取消——绝不到点硬做。等待期间 resume_pending 已置位，
        运行准入闸挡住同一会话的新一轮与队列派发。"""
        if not self._undo_still_owned(sess, sid):
            return
        if self._worker_alive(sess):
            if attempts >= self._RESUME_WAIT_LIMIT:
                sess.resume_pending = False
                self._show_toast("上一轮仍在结束，撤销已取消；可稍后重试", 4000)
                return
            QTimer.singleShot(self._RESUME_WAIT_MS, self,
                              lambda: self._undo_wait(sess, checkpoint_id, sid, attempts + 1))
            return
        self._perform_undo(sess, checkpoint_id, self._undo_workspace_for(sess))

    def _undo_still_owned(self, sess, sid) -> bool:
        """等待期间核对归属：切走会话 / 会话被重置（id 变化）就取消，不替看不见的
        会话改文件。"""
        from .. import session as _session
        if sess is not _session.get_active() or \
                (getattr(sess, "current_session_id", None) or "") != sid:
            sess.resume_pending = False
            self._show_toast("已切换到其它会话，撤销已取消", 3000)
            return False
        return True

    def _undo_workspace_for(self, sess) -> str:
        """撤销执行时的实际工作区：仍按会话自己的规则现算（worktree → 项目 → 进程目录）。
        execute_undo 内部还会与记录里的工作区再核对一次，不一致会拒绝。"""
        from .. import file_history as _fh
        from .. import session as _session_mod
        previous = _session_mod.get_bound()
        _session_mod.bind_thread(sess)
        try:
            root = _fh.session_workspace()
        finally:
            _session_mod.restore_bound(previous)
        return root

    def _perform_undo(self, sess, checkpoint_id, root):
        """屏障通过后的实际撤销：执行 → 验证义务接入 → 保存。三类结果分开报告，
        不用一句"撤销成功"掩盖记录或保存失败。会话侧簿记与测试共用
        file_history.apply_undo_to_session，保证两条路径行为一致。

        「文件已经改变」与「恢复校验成功」是两回事：changed=True（哪怕读回校验
        失败）都说明目标文件被改过，必须作废旧测试/diff 结论并接入待验证义务；
        只有两边都没发生（conflict / unsupported / 恢复写入失败）才是"撤销未执行"。
        """
        from .. import file_history as _fh
        try:
            result = _fh.execute_undo(
                checkpoint_id,
                session_id=getattr(sess, "current_session_id", "") or "",
                workspace=root)
        finally:
            sess.resume_pending = False
            self._style_undo_btn()
        changed = bool(result.get("changed"))
        if result["status"] != "ok" and not changed:
            self._show_toast(f"⚠ 撤销未执行：{result['reason']}", 5000)
            return
        name = os.path.basename(result.get("path") or "文件")
        parts = []
        if result["status"] == "ok":
            parts.append(f"已撤销 {result['tool']} 对 {name} 的修改：{result['reason']}")
            if result.get("verified") is False:
                parts.append("⚠️ 恢复后读回校验未完成，请人工核对文件内容")
            if not result.get("record_saved", False):
                parts.append("⚠️ 撤销记录保存失败（下次启动无法确认它已撤销）")
        else:
            parts.append(f"⚠ 文件已按写前内容恢复，但恢复校验失败：{result['reason']}")
        if changed:
            book = _fh.apply_undo_to_session(sess, result)
            if book["verification_noted"]:
                parts.append("旧测试与 diff 结论已作废、重新要求验证")
            else:
                parts.append(f"⚠️ 验证义务更新失败：{book['verification_error'][:120]}")
            if book["saved"]:
                parts.append("撤销结果已保存")
            elif book["saved"] is False:
                parts.append(f"⚠️ 撤销结果保存失败：{book['save_error'][:120]}")
        self._show_toast("；".join(parts), 6000)

    def _set_agent_mode(self, mode):
        """段控点击：设置 state.agent_mode + 同步两个段按钮的选中态。"""
        state.agent_mode = mode
        if hasattr(self, "plan_btn"):
            self.plan_btn.setChecked(mode == "plan")
            self.act_btn.setChecked(mode == "act")
        # 提示用户切换效果（一闪即过的 toast）
        if mode == "plan":
            self._show_toast("🧠 已切到计划模式：AI 只给方案不动手")
        else:
            self._show_toast("⚡ 已切到执行模式：AI 可直接执行工具")

    def _sync_header_from_session(self):
        """切会话后把顶栏（模型下拉 / Plan-Act / 思考 / 隔离）同步到当前会话的状态。
        model/mode/思考 现在是会话级——切到哪个会话，顶栏就显示那个会话的选择。
        setCurrentIndex 会触发 _on_model_changed（含 force_stop），切会话时必须 blockSignals 屏蔽。"""
        from .. import session as _session
        from .. import state as _state
        sess = _session.get_active()
        if hasattr(self, "model_combo"):
            self.model_combo.blockSignals(True)
            self.model_combo.setCurrentIndex(sess.current_model_index)
            self.model_combo.blockSignals(False)
            _, _, _, supports_think = agent.MODEL_LIST[sess.current_model_index]
            if hasattr(self, "think_btn"):
                self.think_btn.setEnabled(supports_think)
                self.think_btn.setChecked(bool(sess.reasoning_enabled and supports_think))
        if hasattr(self, "plan_btn"):
            self.plan_btn.setChecked(sess.agent_mode == "plan")
            self.act_btn.setChecked(sess.agent_mode != "plan")
        # 侧栏「编码/知识库」入口跟随该会话的 session_kind（逻辑在 rag_sidebar.py）
        if hasattr(self, "_sync_rag_sidebar_from_session"):
            self._sync_rag_sidebar_from_session()
        # 知识库工作区是纯只读检索问答：隐藏编码专属控件（Plan/Act、撤销、角色卡）。
        # 项目/隔离由 _refresh_project_indicator 处理；模型/思考/主题保留。
        is_rag = getattr(sess, "session_kind", "code") == "rag"
        for _attr in ("mode_seg", "undo_btn", "role_btn"):
            _w = getattr(self, _attr, None)
            if _w is not None:
                _w.setVisible(not is_rag)
        # 隔离按钮：知识库模式恒隐藏；编码模式跟随"有无项目"+ worktree 高亮
        if hasattr(self, "isolation_btn"):
            self.isolation_btn.setVisible((not is_rag) and bool(_state.current_project))
            if not is_rag:
                self._style_isolation_btn(active=bool(sess.worktree))
        if hasattr(self, "_refresh_header_compactness"):
            self._refresh_header_compactness()

    # ── 角色卡 ──

    def _restore_role_card_ui(self):
        """启动时恢复角色卡按钮状态"""
        name = agent.get_current_role_name()
        if name:
            self.role_btn.setText(f"角色：{name}")
            self._style_role_btn(active=True)
        else:
            self.role_btn.setText("角色：默认助手")
            self._style_role_btn(active=False)
        # 让窗口窄的时候角色名也跟着截断
        if hasattr(self, "model_combo"):  # 主 UI 已构造完
            self._refresh_header_compactness()

    def _load_role_card(self):
        from .. import session as _session
        if _session.get_active().is_generating:
            self._force_stop_generation()

        def _apply_role_card(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                role_name = os.path.splitext(os.path.basename(path))[0]
                agent.set_role_card(content, role_name, path)

                # 新建对话应用角色
                # B09a：这个 Session 对象马上要被 reset_history"回收"成空白新对话，
                # 挂在它身上的待启动请求一并取消（输入保留在输入框）。
                from .. import session as _session
                self._cancel_pending_run(_session.get_active(),
                                         "已开始新对话，待发送的消息已取消（输入保留在输入框）")
                agent.reset_history()
                self.chat_area.clear()
                self._refresh_session_list()

                # 更新按钮样式
                display_name = agent.get_current_role_name() or role_name
                self.role_btn.setText(f"角色：{display_name}")
                self._style_role_btn(active=True)
                self._refresh_header_compactness()
                self._append_html(f"✅ 已加载角色卡: {display_name}\n\n", "tool_result")
            except Exception as e:
                QMessageBox.critical(self, "错误", f"读取角色卡失败: {e}")

        # 弹出菜单：roles/ 快捷切换 / 导入 / 清除
        menu = QMenu(self)
        role_actions = {}
        roles_dir = os.path.join(BASE_DIR, "roles")
        current = agent.get_current_role_name()
        current_path = os.path.normcase(os.path.abspath(agent.get_current_role_path() or ""))
        # 模板 / 说明类文件名不当作可切换角色（example.md / README.md / 模板.md）
        _ROLE_SKIP = {"example", "readme", "template", "模板", "示例"}
        role_files = []
        if os.path.isdir(roles_dir):
            try:
                role_files = sorted(
                    os.path.join(roles_dir, name)
                    for name in os.listdir(roles_dir)
                    if name.lower().endswith(".md")
                    and os.path.isfile(os.path.join(roles_dir, name))
                    and os.path.splitext(name)[0].lower() not in _ROLE_SKIP
                )
            except Exception:
                role_files = []

        for path in role_files:
            name = os.path.splitext(os.path.basename(path))[0]
            action = menu.addAction(self._svg_icon("id_card_lucide.svg", self._t("menu_text")), name)
            action.setCheckable(True)
            action.setChecked(os.path.normcase(os.path.abspath(path)) == current_path)
            role_actions[action] = path

        if role_files:
            menu.addSeparator()
        load_action = menu.addAction(self._svg_icon("folder_open_lucide.svg", self._t("menu_text")), "导入角色卡 (.md)")
        clear_action = menu.addAction(self._svg_icon("rotate_ccw_lucide.svg", self._t("menu_text")), "恢复默认角色")

        # 显示当前角色
        if current:
            menu.addSeparator()
            info = menu.addAction(f"当前: {current}")
            info.setEnabled(False)

        action = menu.exec(self.role_btn.mapToGlobal(self.role_btn.rect().bottomLeft()))

        if action in role_actions:
            _apply_role_card(role_actions[action])

        elif action == load_action:
            path, _ = QFileDialog.getOpenFileName(
                self, "选择角色卡文件", "",
                "Markdown 文件 (*.md);;文本文件 (*.txt);;所有文件 (*)"
            )
            if path:
                _apply_role_card(path)

        elif action == clear_action:
            agent.clear_role_card()
            # B09a：reset_history 会回收当前 Session 对象，先取消挂着的待启动请求
            from .. import session as _session
            self._cancel_pending_run(_session.get_active(),
                                     "已开始新对话，待发送的消息已取消（输入保留在输入框）")
            agent.reset_history()
            self.chat_area.clear()
            self._refresh_session_list()
            self.role_btn.setText("角色：默认助手")
            self._style_role_btn(active=False)
            self._append_html("✅ 已恢复默认角色\n\n", "tool_result")

    # ══════════════════════════════════════
    # 按钮样式（顶栏 + 输入区）
    # ══════════════════════════════════════

    def _style_model_combo(self):
        arrow_path = os.path.join(BASE_DIR, "icons", "chevron_down.svg").replace("\\", "/")
        self.model_combo.setStyleSheet(
            f"QComboBox {{ background: {self._t('combo_bg')}; border: 1px solid {self._t('combo_border')}; border-radius: 8px; "
            f"padding: 10px 38px 10px 14px; font-size: 13px; color: {self._t('combo_text')}; min-width: 280px; }}"
            f"QComboBox:hover {{ border-color: {self._t('combo_hover_border')}; color: {self._t('combo_hover_text')}; }}"
            f"QComboBox::drop-down {{ border: none; width: 34px; subcontrol-origin: padding; subcontrol-position: top right; }}"
            f"QComboBox::down-arrow {{ image: url({arrow_path}); width: 16px; height: 16px; margin-right: 10px; }}"
            f"QComboBox QAbstractItemView {{ background: {self._t('combo_view_bg')}; border: 1px solid {self._t('combo_view_border')}; "
            f"color: {self._t('combo_view_text')}; selection-background-color: {self._t('combo_view_sel_bg')}; "
            f"selection-color: {self._t('combo_view_sel_text')}; padding: 4px; outline: 0; }}"
        )

    def _style_think_btn(self):
        color = self._t("think_on_text") if self.think_btn.isChecked() else self._t("think_off_text")
        self.think_btn.setIcon(self._svg_icon("brain_lucide.svg", color))
        self.think_btn.setIconSize(QSize(16, 16))
        self.think_btn.setStyleSheet(
            f"QPushButton {{ border-radius: 8px; padding: 9px 16px; font-size: 12px; }}"
            f"QPushButton:checked {{ background: {self._t('think_on_bg')}; border: 1px solid {self._t('think_on_border')}; color: {self._t('think_on_text')}; }}"
            f"QPushButton:!checked {{ background: {self._t('think_off_bg')}; border: 1px solid {self._t('think_off_border')}; color: {self._t('think_off_text')}; }}"
            f"QPushButton:hover:checked {{ background: {self._t('think_on_hover')}; border-color: {self._t('think_on_hover_border')}; }}"
            f"QPushButton:hover:!checked {{ border-color: {self._t('think_off_hover_border')}; color: {self._t('think_off_hover_text')}; }}"
        )

    def _style_undo_btn(self):
        """撤销按钮配色：当前会话 + 当前实际工作区有可撤销记录时高亮，否则灰禁。

        只做便宜检查（记录存在、材料文件在）；完整的现场核对在点击后的 precheck /
        execute_undo 里做。样式沿用旧版（B11a 仅换数据来源）。"""
        from .. import file_history as _fh
        from .. import session as _session
        sess = _session.get_active()
        sid = getattr(sess, "current_session_id", None) or ""
        cand = None
        try:
            cand = _fh.latest_undoable(sid, _fh.session_workspace()) if sid else None
        except Exception as e:
            logger.warning(f"查询可撤销记录失败: {e}")
        has_cp = cand is not None
        self.undo_btn.setEnabled(has_cp)
        if has_cp:
            self.undo_btn.setStyleSheet(
                f"QPushButton {{ background: {self._t('think_off_bg')};"
                f"  border: 1px solid {self._t('think_off_border')};"
                f"  border-radius: 8px; padding: 9px 14px; font-size: 12px;"
                f"  color: {self._t('warn')}; font-weight: 600;"
                f"}}"
                f"QPushButton:hover {{ background: {self._t('history_hover_bg')};"
                f"  border-color: {self._t('warn')}; }}"
            )
            info = cand or {}
            tool = info.get("tool", "")
            path = info.get("path", "")
            name = path.split("/")[-1].split("\\")[-1] if path else ""
            self.undo_btn.setToolTip(
                f"撤销 AI 最近一次有完整记录的文件修改（恢复写前内容）\n上次：{tool} → {name}\n"
                "执行前会再次核对现场；文件在写入后又被改过时会拒绝"
            )
        else:
            self.undo_btn.setStyleSheet(
                f"QPushButton {{ background: transparent;"
                f"  border: 1px solid {self._t('input_border')};"
                f"  border-radius: 8px; padding: 9px 14px; font-size: 12px;"
                f"  color: {self._t('text_subtle')};"
                f"}}"
            )
            self.undo_btn.setToolTip("还没有可撤销的 AI 改动")

    def _style_isolation_btn(self, active: bool):
        """隔离按钮配色：active=True 时高亮表示正在隔离。

        只设图标 / 颜色 / tooltip，**不设文字**——文字（含窄屏折叠成纯图标）由
        _refresh_header_compactness 统一管理，否则两边抢着 setText、窄屏不折叠会重叠。
        """
        if active:
            self.isolation_btn.setIcon(self._svg_icon("unlock.svg", self._t("ai_label")))
            self.isolation_btn.setIconSize(QSize(16, 16))
            self.isolation_btn.setToolTip(
                "隔离模式已开启：AI 在独立 worktree 目录操作\n"
                "点击「恢复」：把隔离区改动应用回主项目 + 清理 worktree"
            )
            self.isolation_btn.setStyleSheet(
                f"QPushButton {{ background: {self._t('ai_label')}22;"
                f"  border: 1px solid {self._t('ai_label')};"
                f"  border-radius: 8px; padding: 9px 14px; font-size: 12px;"
                f"  color: {self._t('ai_label')}; font-weight: 600;"
                f"}}"
                f"QPushButton:hover {{ background: {self._t('ai_label')}33;"
                f"  border-color: {self._t('ai_label')}; }}"
            )
        else:
            self.isolation_btn.setIcon(self._svg_icon("lock.svg", self._t("text")))
            self.isolation_btn.setIconSize(QSize(16, 16))
            self.isolation_btn.setToolTip(
                "隔离模式：AI 在独立 worktree 目录操作，不影响主项目\n需项目已启用版本控制"
            )
            self.isolation_btn.setStyleSheet(
                f"QPushButton {{ background: {self._t('think_off_bg')};"
                f"  border: 1px solid {self._t('think_off_border')};"
                f"  border-radius: 8px; padding: 9px 14px; font-size: 12px;"
                f"  color: {self._t('text')};"
                f"}}"
                f"QPushButton:hover {{ background: {self._t('history_hover_bg')};"
                f"  border-color: {self._t('ai_label')}; }}"
            )
        # 文字按当前窗口宽度刷新（UI 已构造完才调，避免构造期半成品）
        if hasattr(self, "model_combo"):
            self._refresh_header_compactness()

    def _toggle_isolation(self):
        """切换隔离模式。"""
        from .. import worktree as _wt
        from .. import session as _sess
        from .. import state as _state
        active = _sess.get_active()
        project_dir = _state.current_project
        if not project_dir:
            return
        if active.is_generating:
            self._show_toast("⚠ 生成中不能切换隔离模式")
            return

        if active.worktree:
            # 恢复：先把 worktree 改动应用回主项目，再清理 worktree
            ok, msg = _wt.finish(active, apply_changes=True)
            if ok:
                self._style_isolation_btn(active=False)
                self._refresh_project_indicator()
                self._show_toast("✓ 隔离区改动已恢复到主项目")
            else:
                self._show_toast(f"⚠ {msg}", duration=7000)
        else:
            # 启动隔离
            if _wt.has_uncommitted_changes(project_dir):
                self._show_toast(
                    "⚠ 主工作区有未提交改动，隔离区只会基于 HEAD，"
                    "不会自动带入这些改动",
                    duration=7000,
                )
            session_id = active.current_session_id or active.key or str(id(active))
            wt_path = _wt.create(active, project_dir, session_id=session_id)
            if wt_path:
                self._style_isolation_btn(active=True)
                self._refresh_project_indicator()
                self._show_toast(f"🔒 隔离模式已开启，worktree: {wt_path}")
            else:
                self._show_toast("⚠ 无法创建隔离环境（非 git 仓库？）", duration=5000)

    def _style_role_btn(self, active):
        color = self._t("role_active_text") if active else self._t("role_text")
        self.role_btn.setIcon(self._svg_icon("id_card_lucide.svg", color))
        self.role_btn.setIconSize(QSize(16, 16))
        if active:
            self.role_btn.setStyleSheet(
                f"QPushButton {{ background: {self._t('role_active_bg')}; border: 1px solid {self._t('role_active_border')}; border-radius: 8px; "
                f"padding: 9px 16px; font-size: 12px; color: {self._t('role_active_text')}; font-weight: {self._t('role_active_weight')}; }}"
                f"QPushButton:hover {{ background: {self._t('role_active_hover_bg')}; border-color: {self._t('role_active_hover_border')}; color: {self._t('role_active_hover_text')}; }}"
            )
        else:
            self.role_btn.setStyleSheet(
                f"QPushButton {{ background: {self._t('role_bg')}; border: 1px solid {self._t('role_border')}; border-radius: 8px; "
                f"padding: 9px 16px; font-size: 12px; color: {self._t('role_text')}; }}"
                f"QPushButton:hover {{ background: {self._t('role_hover_bg')}; border-color: {self._t('role_hover_border')}; color: {self._t('role_hover_text')}; }}"
            )

    def _style_settings_btn(self):
        color = self._t('text_dim')
        hover_color = self._t('text')
        svg_path = os.path.join(BASE_DIR, "icons", "settings_lucide.svg")

        def _svg_to_icon(svg_str, clr, size=20):
            from PySide6.QtSvg import QSvgRenderer
            svg_filled = svg_str.replace('currentColor', clr)
            renderer = QSvgRenderer(svg_filled.encode('utf-8'))
            dpr = self.devicePixelRatioF() if hasattr(self, 'devicePixelRatioF') else 1.0
            px = QPixmap(int(size * dpr), int(size * dpr))
            px.fill(Qt.transparent)
            painter = QPainter(px)
            renderer.render(painter)
            painter.end()
            px.setDevicePixelRatio(dpr)
            return QIcon(px)

        if os.path.exists(svg_path):
            with open(svg_path, 'r', encoding='utf-8') as f:
                svg_tpl = f.read()
            self._settings_btn_icon = _svg_to_icon(svg_tpl, color)
            self._settings_btn_icon_hover = _svg_to_icon(svg_tpl, hover_color)
        else:
            self._settings_btn_icon = QIcon()
            self._settings_btn_icon_hover = QIcon()

        self.settings_btn.setText("")
        self.settings_btn.setIcon(self._settings_btn_icon)
        self.settings_btn.setIconSize(QSize(19, 19))

    def _style_img_btn(self):
        color = self._t('img_btn')
        hover_color = self._t('img_btn_hover')
        # 用 plus 图标（点击弹菜单：上传图片 / 导入项目）
        svg_path = os.path.join(BASE_DIR, "icons", "plus_lucide.svg")
        if os.path.exists(svg_path):
            from PySide6.QtSvg import QSvgRenderer
            with open(svg_path, 'r', encoding='utf-8') as f:
                svg_tpl = f.read()
            def _svg_to_icon(svg_str, clr):
                svg_filled = svg_str.replace('currentColor', clr)
                renderer = QSvgRenderer(svg_filled.encode('utf-8'))
                # 取设备像素比，画布渲染高分屏才不糊
                dpr = self.devicePixelRatioF() if hasattr(self, 'devicePixelRatioF') else 1.0
                px = QPixmap(int(24 * dpr), int(24 * dpr))
                px.fill(Qt.transparent)
                painter = QPainter(px)
                renderer.render(painter)
                painter.end()
                px.setDevicePixelRatio(dpr)
                return QIcon(px)
            self._img_btn_icon = _svg_to_icon(svg_tpl, color)
            self._img_btn_icon_hover = _svg_to_icon(svg_tpl, hover_color)
        else:
            self._img_btn_icon = _make_upload_icon(color)
            self._img_btn_icon_hover = _make_upload_icon(hover_color)
        self.img_btn.setIcon(self._img_btn_icon)
        self.img_btn.setIconSize(QSize(20, 20))
        self.img_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; padding: 4px; border-radius: 4px; }"
            "QPushButton:hover { background: rgba(0,0,0,0.06); }"
        )

    # 语音输入/朗读按钮样式（_style_mic_btn / _style_tts_btn）已随语音模块移除。
