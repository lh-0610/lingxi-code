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

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QMenu, QMessageBox,
    QPushButton, QSizePolicy, QWidget,
)

from .. import agent
from .. import state
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
        self.undo_btn.setToolTip("撤销 AI 最近一次对文件的修改（git stash 恢复）\n仅 git 项目可用")
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
        """撤销按钮回调：调 checkpoint.undo_last_checkpoint。"""
        from .. import checkpoint as _cp
        ok, msg = _cp.undo_last_checkpoint()
        self._show_toast(("✓ " if ok else "⚠ ") + msg, duration=3000 if ok else 5000)
        self._style_undo_btn()  # 刷新按钮状态（栈空了就灰掉）

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
        # 隔离按钮：有项目时可见，根据会话 worktree 状态高亮
        if hasattr(self, "isolation_btn"):
            has_project = bool(_state.current_project)
            self.isolation_btn.setVisible(has_project)
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
        """撤销按钮配色：有 checkpoint 时高亮可点，无则灰禁。"""
        from .. import checkpoint as _cp
        has_cp = _cp.has_undoable_checkpoint()
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
            info = _cp.latest_checkpoint_info() or {}
            tool = info.get("tool", "")
            path = info.get("path", "")
            name = path.split("/")[-1].split("\\")[-1] if path else ""
            self.undo_btn.setToolTip(
                f"撤销 AI 最近一次对文件的修改\n上次：{tool} → {name}"
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
