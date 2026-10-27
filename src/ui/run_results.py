"""运行结果卡（B06）与「继续任务」（B04）：接收结果快照、画卡、查看改动/输出、恢复前的归属检查与项目重定位。

从 chat_window.py 拆出的 ChatUI mixin；方法原样搬移，依赖 ChatUI 上的其它属性。
"""
import os

from PySide6.QtCore import Qt, QTimer

from .. import state


class RunResultMixin:
    """运行结果卡（B06）与「继续任务」（B04）：接收结果快照、画卡、查看改动/输出、恢复前的归属检查与项目重定位。"""

    # ── 运行结果卡（B06）──

    def deliver_run_result(self, sess, run_id, snapshot):
        """线程安全：worker 解绑前把结果快照投到 UI 主线程。

        快照已经是不可变拷贝，信号排队期间下一轮再怎么改会话状态都影响不到它。
        """
        try:
            self.bridge.run_result.emit(sess, str(run_id or ""), snapshot)
        except Exception as e:
            from ..paths import logger
            logger.warning(f"投递结果卡失败: {e}")

    def _on_run_result(self, sess, run_id, snapshot):
        """主线程槽：只画属于当前前台会话、且确实是最新那一轮的卡。"""
        from .. import session as _session
        if sess is not _session.get_active():
            # 后台会话跑完了：结果已经存在它自己的 last_result 上，切回去时重绘。
            # 绝不插进当前前台会话——那是另一个任务的结果。
            sess.needs_redraw = True
            self._refresh_session_list()
            return
        # 归属判据是**会话最新的那条运行记录**，不是"最近渲染过什么"：
        # 旧 run 的迟到信号必须认得出来，否则它会把新一轮的卡顶掉。
        latest = (getattr(sess, "last_run", None) or {}).get("id")
        if run_id and latest and latest != run_id:
            return      # 迟到的旧 run：它仍然是历史事实，只是不再代表当前状态
        if self._rendered_result_run_id == run_id and run_id:
            return      # 重复信号：同一轮只画一张卡
        self._render_result_card(snapshot)

    def _render_result_card(self, snapshot):
        from .. import result_view
        from .. import session as _session
        from .result_card import ResultCard
        sess = _session.get_active()
        # 正在跑的那一轮不能被显示成"上次运行被中断"——把当前活动 run_id 交给判定层。
        live = getattr(sess, "active_run_id", None) if sess.is_generating else None
        view = result_view.describe(snapshot, live_run_id=live)
        if not view:
            return
        card = ResultCard(view)
        card.view_diff_requested.connect(self._on_result_view_diff)
        card.view_output_requested.connect(self._on_result_view_output)
        card.continue_requested.connect(
            lambda v, c=card: self._on_result_continue(v, card=c))
        self.chat_area.add_result_card(card)
        self._rendered_result_run_id = view.get("run_id") or ""

    def _redraw_result_card(self):
        """切回会话 / 重开程序后，把最近一次运行结果重新画出来。

        来源优先级：内存里的 `last_result`（本进程刚跑完的那一轮）→ 持久化的 `last_run`
        重建。**不依赖 render_log**——它只活在内存里，重开程序就没了，
        拿它当唯一来源等于"重启后看不到上一轮结果"。
        """
        from .. import run_records
        from .. import session as _session
        sess = _session.get_active()
        snapshot = getattr(sess, "last_result", None)
        # 缓存要**核对归属**再用：Session 对象会被复用来装别的会话，缓存也可能比
        # 当前 last_run 旧。对不上就丢掉、从持久化记录重建，宁可少画一张卡，
        # 也不要把另一轮（甚至另一个会话）的结果显示成这一轮的。
        if snapshot and not self._result_belongs_here(sess, snapshot):
            snapshot = None
        if not snapshot:
            snapshot = run_records.snapshot_from_loaded(sess)
        if not snapshot:
            return
        self._rendered_result_run_id = ""
        self._render_result_card(snapshot)

    @staticmethod
    def _result_belongs_here(sess, snapshot):
        """这份缓存的结果快照，是不是**这个会话当前这条**运行记录的。

        两种对不上的情形都必须挡掉：① Session 对象被复用装了别的会话（侧栏切换）；
        ② 会话被重置 / 加载成了另一段历史，`last_run` 已经换人或清空。
        """
        run = getattr(sess, "last_run", None)
        if not isinstance(run, dict) or not run.get("id"):
            return False        # 没有运行记录了（比如刚重置），缓存一律作废
        if snapshot.get("run_id") != run.get("id"):
            return False
        sid = getattr(sess, "current_session_id", None) or ""
        return (snapshot.get("session_id") or "") == sid

    def _on_result_view_diff(self, view):
        """只读查看**当前**差异。它不是那一轮保存过的历史 diff，文案必须说清楚。"""
        # 用本轮**实际的工作目录**：子 Agent / worktree 里跑的那轮，改动在隔离区里，
        # 拿主仓库路径去 diff 会回一句"工作区干净"，而改动明明就在那儿。
        work_dir = view.get("work_dir") or view.get("project")
        if not work_dir:
            self._show_toast("该运行没有关联目录，无法查看改动")
            return
        import os
        if not os.path.isdir(work_dir):
            # **目录没了就明说，绝不将就。** `_project_cwd()` 在路径不存在时会回退到
            # 进程 cwd，于是 git_diff 悄悄查了灵犀自己的目录、回一句"工作区干净"，
            # 而弹窗标题还写着那个已经不存在的路径——用户会以为那轮改动没了。
            self._show_text_dialog(
                "查看改动（目录已不可用）",
                f"这一轮当时的工作目录是：\n{work_dir}\n\n"
                "它现在不存在了（可能被移动、删除，或隔离区已合并/丢弃）。"
                "为避免查到无关目录的差异，这里不做回退查询。")
            return
        from .. import tools_git
        from .. import session as _session
        # **存原始绑定状态，不是 current_session()**：主线程本来没绑定，
        # 用 current_session() 存下来的是 active，"还原"时会把主线程永久绑死在它身上，
        # 之后切会话就出现 get_active() 与 current_session() 指向不同会话的错乱。
        prev = _session.get_bound()
        holder = _session.Session()
        holder.project = work_dir         # 按**卡片自己的**目录取 diff，不借前台会话推断
        _session.bind_thread(holder)
        try:
            text = tools_git.git_diff.func("")
        except Exception as e:
            text = f"查看改动失败: {e}"
        finally:
            _session.restore_bound(prev)
        self._show_text_dialog(
            "查看改动（当前状态）",
            f"以下是 {work_dir} 此刻的差异，**不是**该次运行保存下来的历史 diff。\n\n{text}")

    def _on_result_view_output(self, view):
        lines = []
        for row in view.get("validations") or []:
            head = f"{row['checker']}"
            if row.get("path"):
                head += f" · {row['path']}"
            lines.append(f"── {head} · {row['label']}"
                         + ("（已过期）" if row.get("stale") else ""))
            if row.get("argv"):
                lines.append("命令: " + " ".join(row["argv"]))
            elif row.get("command"):
                lines.append("命令: " + row["command"])
            else:
                lines.append("命令: （进程内执行，无 argv）")
            lines.append(f"目录: {row.get('cwd') or '（未记录）'}   {row.get('detail') or ''}")
            if row.get("summary"):
                lines.append(row["summary"])
            if row.get("reason"):
                lines.append(f"原因: {row['reason']}")
            lines.append("")
        if not lines:
            lines = ["本轮没有留下验证记录。"]
        lines.append("完整工具输出在上面的对话记录里；这里只保留卡片采集到的摘要。")
        self._show_text_dialog("验证输出", "\n".join(lines))

    @staticmethod
    def _resume_ownership_problem(sess, view):
        """这张卡还能不能代表该会话去"继续"。能 → 空串；不能 → 给用户看的原因。

        判据是**会话 id + 该会话最新一条运行记录的 id**：卡片是某一轮的快照，
        这之后只要开过新的一轮（哪怕是用户直接发的消息），它就不再代表当前状态。
        """
        sid = getattr(sess, "current_session_id", None) or ""
        if not view.get("session_id") or view.get("session_id") != sid:
            return "这张结果卡不属于当前会话，没有继续"
        latest = (getattr(sess, "last_run", None) or {}).get("id")
        if not latest or latest != view.get("run_id"):
            return "这个会话已经有更新的一轮运行，没有从旧结果继续"
        return ""

    def _on_result_continue(self, view, card=None, accept_current_model=False):
        """结果卡的「继续任务」。主线程；不跑任何命令、不调模型。"""
        from .. import session as _session
        sess = _session.get_active()
        problem = self._resume_ownership_problem(sess, view)
        if problem:
            self._show_toast(problem, 2500)
            return
        if sess.resume_pending:
            return          # 连点：第一次点击已经在准备了，只起一个 worker
        if self._pending_run_for(sess) is not None:
            # B09a：有一条待发送的消息也在等旧 worker 退出。两条请求不能同时接纳，
            # 让用户先处理消息（发出去或等它超时撤回），否则它会在继续之后突然发出。
            self._show_toast("有消息正在等待上一轮结束，请先处理再继续任务", 3000)
            return
        sess.resume_pending = True
        # 按会话记"哪张卡发起了继续"：后台会话的那一轮还没结束、用户又在别的会话点了继续，
        # 单个槽位会被覆盖，前一个会话的卡就再也收不了尾。
        self._resume_cards_by_session()[sess] = card
        if card is not None:
            card.set_continue_busy(True)
        self._resume_wait(sess, dict(view), card, accept_current_model, 0)

    def _resume_wait(self, sess, view, card, accept, attempts):
        """屏障：旧 worker **真正退出**之后才起新的。

        只看按钮 / is_generating 不够：强制停止会立刻把 is_generating 置 False，
        而旧线程可能还在收尾、还会写这个会话（存盘、结束记录、标题线程）。
        忙碌判据统一走 _worker_alive（B09a）。
        """
        if self._worker_alive(sess):
            if attempts >= self._RESUME_WAIT_LIMIT:
                self._resume_abort(sess, card, "上一轮仍在结束，请稍后再点「继续任务」")
                return
            QTimer.singleShot(self._RESUME_WAIT_MS, self,
                              lambda: self._resume_wait(sess, view, card, accept, attempts + 1))
            return
        try:
            self._start_resume(sess, view, card, accept)
        except Exception as e:
            from ..paths import logger
            logger.error(f"继续任务启动失败: {e}", exc_info=True)
            self._resume_abort(sess, card, f"继续任务启动失败：{str(e)[:80]}")

    def _resume_cards_by_session(self):
        cards = getattr(self, "_resume_cards", None)
        if cards is None:
            cards = self._resume_cards = {}
        return cards

    def _resume_abort(self, sess, card, message=""):
        sess.resume_pending = False
        self._resume_cards_by_session().pop(sess, None)
        if card is not None:
            try:
                card.set_continue_busy(False)
            except RuntimeError:
                pass        # 卡片已随重绘销毁
        if message:
            self._show_toast(message, 2500)

    def _start_resume(self, sess, view, card, accept):
        from .. import recovery
        from .. import session as _session
        if sess is not _session.get_active():
            # 等待期间切走了。不在后台悄悄起：顶栏 / 按钮 / 模型同步都属于前台会话，
            # 替一个看不见的会话切模型、改按钮状态只会让两个会话的状态互相串。
            self._resume_abort(sess, card, "已切换到其它会话，继续任务已取消")
            return
        problem = self._resume_ownership_problem(sess, view)
        if problem:
            self._resume_abort(sess, card, problem)
            return
        pre = recovery.precheck(sess, accept_current_model=accept)
        if pre.blocking:
            self._resume_abort(sess, card)
            self._show_resume_blocked(sess, view, card, pre)
            return
        if pre.model_index is not None and pre.model_index != sess.current_model_index:
            # 按记录的稳定标识找回原模型，顶栏跟着显示实际要用的那一个。
            sess.current_model_index = pre.model_index
            self._sync_header_from_session()
        mode = "Plan 模式（只调研，不改动）" if sess.agent_mode == "plan" else "Act 模式"
        self._append_html(f"\n继续任务 · 使用模型「{pre.model_label}」· {mode}\n", "tool_result")
        resume = {"expected_run_id": view.get("run_id") or "", "notes": list(pre.notes)}
        _session.register(sess)
        sess.is_generating = True
        sess.stop_flag = False
        self._update_btn_state("stop")
        # 放开 pending 与起线程在同一个主线程回调里：中间插不进别的点击；
        # 之后再点，is_generating 已为 True，屏障会一直等到这一轮结束，再由归属核对拒绝。
        # args=(sess,) 必须带上：_run_agent 的 sess=None 会退回"线程启动那一刻的
        # active"——点继续后、线程进入主体前切了会话，worker 就会绑到别的会话上
        # （覆盖人家的 worker 身份、恢复检查拒了继续，收尾却把那个会话标成空闲）。
        if not self._spawn_worker_thread(sess, self._run_agent,
                                         args=(sess,), kwargs={"resume": resume}):
            self._resume_abort(sess, card, "继续任务启动失败：无法创建工作线程")
            return
        sess.resume_pending = False
        self._refresh_session_list()      # 侧栏的"生成中"标记跟上

    def _settle_resume_card(self, sess):
        """继续发起的那一轮结束了：没开出新一轮（被拦下）就把按钮放开，开出了就收起来。"""
        card = self._resume_cards_by_session().pop(sess, None)
        if card is None:
            return
        latest = (getattr(sess, "last_run", None) or {}).get("id")
        try:
            if latest and latest == card.view.get("run_id"):
                card.set_continue_busy(False)
            else:
                card.mark_continued()
        except RuntimeError:
            pass            # 卡片已随重绘销毁

    def _show_resume_blocked(self, sess, view, card, pre):
        """预检没过：说清原因，给出能走的下一步（改用当前模型 / 重新选择项目目录）。"""
        from PySide6.QtWidgets import QApplication, QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle("暂时无法继续任务")
        box.setIcon(QMessageBox.Warning)
        box.setText("继续之前需要先处理：\n\n" + "\n".join(f"• {b}" for b in pre.blocking))
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        use_current = relocate = copy_path = None
        if pre.model_missing and not pre.project_missing:
            from ..models import MODEL_LIST
            idx = int(getattr(sess, "current_model_index", 0) or 0)
            name = MODEL_LIST[idx][0] if 0 <= idx < len(MODEL_LIST) else "当前模型"
            use_current = box.addButton(f"改用当前模型「{name}」继续", QMessageBox.AcceptRole)
        if pre.project_missing:
            relocate = box.addButton("重新选择项目目录…", QMessageBox.ActionRole)
            copy_path = box.addButton("复制原路径", QMessageBox.ActionRole)
        box.addButton("关闭", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is None:
            return
        if clicked is use_current:
            self._on_result_continue(view, card=card, accept_current_model=True)
        elif clicked is copy_path:
            QApplication.clipboard().setText(pre.project_missing)
            self._show_toast("已复制原项目路径")
        elif clicked is relocate:
            self._resume_relocate_project(sess, view, card, pre.project_missing)

    def _resume_relocate_project(self, sess, view, card, old_path):
        """项目被移动了：让用户指到新位置，**核对是同一个仓库**后再把会话改挂过去。

        不核对就改挂，等于允许把一个会话的未验证改动、恢复说明套到另一个项目上——
        方案要求"实际指向另一个项目时阻止直接继续"，这里就是那道闸。
        """
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from .. import session as _session
        picked = QFileDialog.getExistingDirectory(self, "选择项目现在的位置", "")
        if not picked:
            return
        if sess is not _session.get_active():
            self._show_toast("已切换到其它会话，未修改项目位置")
            return

        def _confirm(why, path):
            reply = QMessageBox.question(
                self, "无法确认是同一个项目",
                f"{why}。\n\n仍要把这个会话改到下面的目录吗？\n{path}\n\n"
                "（继续后会重新核对现场，并要求补跑测试、查看改动。）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            return reply == QMessageBox.Yes

        ok, message = self._relocate_session_project(sess, picked, confirm=_confirm)
        if not ok:
            if message:
                QMessageBox.warning(self, "没有修改项目位置", message)
            return
        if message:
            self._show_toast(message, 3000)
        self._refresh_session_list()
        if hasattr(self, "_refresh_project_indicator"):
            self._refresh_project_indicator()
        self._on_result_continue(view, card=card)

    def _relocate_session_project(self, sess, picked, *, confirm):
        """把会话改挂到项目的新位置。返回 (是否改了, 给用户的话)。

        判据是**仓库身份**（根提交），不是目录名：确定是另一个仓库就拒绝；判断不了就问用户
        （`confirm(原因, 路径) -> bool`），用户不点头就不动。改挂后照常经过继续前的全套核对。
        """
        from .. import memory, projects as _projects, recovery, workspace_anchor
        picked = os.path.normpath(picked).replace("\\", "/")
        run = getattr(sess, "last_run", None) or {}
        anchor = run.get("workspace")
        same, why = workspace_anchor.same_repository(anchor, picked)
        if same is False:
            return False, f"{picked}\n\n{why}。没有修改这个会话的项目。"
        if same is None and not confirm(why, picked):
            return False, ""
        # 确认是同一个项目之后，挂在旧位置上的**活动**追踪路径跟着搬过去——否则完成闸门会
        # 一直去枚举那个不存在的旧目录，把补做的测试和 diff 结论反复作废。历史锚点保留原值。
        old = getattr(sess, "project", None)
        old_roots = [old if isinstance(old, str) else "", run.get("work_dir") or "",
                     (anchor or {}).get("root") if isinstance(anchor, dict) else ""]
        old_roots += [os.path.realpath(r) for r in list(old_roots) if r]
        recovery.relocate_obligations(sess, old_roots, picked)
        _projects.add_project(picked)            # 已在列表里会返回 False，无妨
        note = ""
        if not _projects.set_current(picked):
            note = "项目切换未能保存（重启后可能恢复为原项目）"
        state.current_project = picked
        sess.project = picked
        sess.shell_cwd = None
        outcome = memory.save_session_report(session=sess)
        if not outcome.body_written and not outcome.skipped:
            note = f"会话的新项目位置未能保存：{str(outcome.error)[:60]}"
        return True, note
