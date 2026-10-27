"""运行中输入队列的入队、派发与收尾（B09b）；队列面板本身见 queue_panel.py，状态机见 src/input_queue.py。

从 chat_window.py 拆出的 ChatUI mixin；方法原样搬移，依赖 ChatUI 上的其它属性。
"""


from .. import state


class QueueDispatchMixin:
    """运行中输入队列的入队、派发与收尾（B09b）；队列面板本身见 queue_panel.py，状态机见 src/input_queue.py。"""

    # ── B09b 运行中输入队列：入队 / 编辑 / 删除 / 暂停 / 立即处理 / 逐项派发 ──
    # 队列状态与持久化形状在 src/input_queue.py（纯状态，无 Qt）；这里负责处置：
    # 每次变更先落盘、正文失败回滚内存，派发统一走 B09a 的闸与唯一启动出口。

    def _queue_scene(self, sess):
        """入队/派发核对用的现场快照（项目 / 隔离区 / 任务归属）。"""
        from .. import session as _session
        project = getattr(sess, "project", None)
        if project is _session._UNSET:
            project = state.current_project
        task = getattr(sess, "current_task", None) or {}
        return {"project": project, "worktree": getattr(sess, "worktree", None),
                "task_id": task.get("id")}

    def _persist_queue_change(self, sess, undo=None):
        """队列变更落盘。回滚只看 body_written：正文没写进去（磁盘上确实没有这次
        变更）才 undo 内存；正文已落盘、只是索引失败时**不回滚**——回滚会让内存
        与磁盘分叉（界面说"未入队"、磁盘却有这条，用户再提交就是重复消息，实测
        踩过），改为保留已落盘状态、如实提示并暂停自动推进。"""
        from .. import input_queue as _iq
        from .. import memory
        rep = memory.save_session_report(session=sess)
        if not rep.body_written:
            if undo is not None:
                try:
                    undo()
                except Exception:
                    pass
            return False, (str(rep.error) if rep.error is not None
                           else (rep.skipped or "正文未写入"))
        if rep.fully_saved:
            return True, ""
        _iq.set_paused(sess.input_queue, True,
                       "索引更新失败，已暂停自动推进；重开会话列表会自动修复")
        return True, "会话列表索引更新失败（已保留；队列已暂停自动推进，重开会话列表会自动修复）"

    def _enqueue_input(self, sess, text, images, source="send") -> bool:
        """入队服务：桌面与遥控共用。入队不进 chat_history、不接 B05 来源——
        那是正式派发（接纳）时的事。持久化成功才报告"已排队"。"""
        from .. import input_queue as _iq
        scene = self._queue_scene(sess)
        item = _iq.new_queue_item(text, images, project=scene["project"],
                                  worktree=scene["worktree"],
                                  task_id=scene["task_id"], source=source)
        ok, reason = _iq.enqueue(sess.input_queue, item)
        if not ok:
            self._reject_input(source, reason)
            return False

        def _undo():
            _iq.delete(sess.input_queue, item["queue_item_id"])

        saved, err = self._persist_queue_change(sess, undo=_undo)
        self._refresh_queue_panel()
        if not saved:
            self._reject_input(source, "保存失败：" + err)
            return False
        if source == "remote":
            self._notify_remote_queued(index_note=err)
        else:
            self._show_toast("已排队，当前轮结束后处理" if not err
                             else "已排队，但" + err, 3000)
        return True

    def _reject_input(self, source, reason):
        """入队被拒（容量/保存失败）：桌面保留输入并说明；遥控给未接收回执。"""
        if source == "remote":
            self._notify_remote_unaccepted("未接收：" + reason)
        else:
            self._show_toast("未入队：" + reason + "；输入已保留", 4000)

    def _notify_remote_queued(self, index_note=""):
        try:
            from .. import telegram_push
            msg = "已排队，当前轮结束后处理" + (f"（{index_note}）" if index_note else "")
            telegram_push.push("info", "灵犀已收到", msg)
        except Exception:
            pass

    def _refresh_queue_panel(self):
        if not hasattr(self, "queue_panel"):
            return
        from .. import session as _session
        self.queue_panel.render_queue(_session.get_active().input_queue)

    def _queue_pause_for_switch(self, sess):
        """切走会话：尚未接纳的派发等待由 _cancel_pending_run 取消，条目保留并
        暂停；切回后通过面板「继续处理队列」明确恢复。"""
        from .. import input_queue as _iq
        q = getattr(sess, "input_queue", None)
        if not q or not q.get("items"):
            return
        for it in q["items"]:
            if it["state"] == _iq.DISPATCHING:
                _iq.unmark_dispatching(q, it["queue_item_id"], "已切换会话，本次派发已取消")
        _iq.set_paused(q, True, "已切换会话，队列暂停；切回后点「继续处理队列」恢复")
        self._persist_queue_change(sess)

    def _queue_hold_and_save(self, sess, item_id, reason):
        """派发前校验失败（附件/归属）：该条暂停并说明，队列一并暂停防空转。"""
        from .. import input_queue as _iq
        q = sess.input_queue
        _iq.hold(q, item_id, reason)
        _iq.set_paused(q, True, f"有条目被暂停：{reason}")
        self._persist_queue_change(sess)
        self._refresh_queue_panel()
        it = _iq.find(q, item_id)
        if it is not None and it.get("source") == "remote":
            self._notify_remote_unaccepted("已入队但暂停：" + reason)

    def _queue_dispatch_aborted(self, sess, item_id, reason):
        """等待接纳阶段失败（闸超时 / 归属复核失败 / 等待被取消）：条目回到
        queued 并如实说明；条目本身就是保留的输入，可在面板重试。"""
        from .. import input_queue as _iq
        q = sess.input_queue
        it = _iq.find(q, item_id)
        _iq.unmark_dispatching(q, item_id, error=reason)
        self._persist_queue_change(sess)
        self._refresh_queue_panel()
        if it is not None and it.get("source") == "remote":
            self._notify_remote_unaccepted("暂停派发：" + reason)

    def _queue_process_now(self, item_id):
        """「立即处理这条」：明确停止当前运行，等旧 worker 真正退出并收尾后派发
        选中项；其余条目保持暂停——本次主动停止不是"继续整个队列"。"""
        from .. import input_queue as _iq
        from .. import session as _session
        sess = _session.get_active()
        q = sess.input_queue
        item = _iq.find(q, item_id)
        if item is None or item["state"] != _iq.QUEUED:
            self._show_toast("该条当前状态不能立即处理", 2500)
            return
        if self._pending_run_for(sess) is not None or getattr(sess, "resume_pending", False):
            self._show_toast("已有一个启动请求在等待上一轮结束，请稍后再试", 3000)
            return
        if getattr(sess, "is_generating", False):
            # 非阻塞停止：is_generating 立即恢复，旧 worker 的退出由 B09a 屏障等待
            self._force_stop_generation()
        _iq.set_paused(q, True, "已选择「立即处理」，其余条目保持暂停")
        self._queue_begin_dispatch(sess, item_id)

    def _queue_begin_dispatch(self, sess, item_id) -> bool:
        """派发流水线：附件与归属核对 → 派发准备记录落盘 → 过 B09a 统一启动闸。
        任何一步失败都保留原条目并如实说明。"""
        from .. import input_queue as _iq
        q = sess.input_queue
        item = _iq.find(q, item_id)
        if item is None or item["state"] != _iq.QUEUED:
            return False
        pairs, problem = _iq.load_images(item)
        if problem:
            self._queue_hold_and_save(sess, item_id, problem)
            return False
        scene = self._queue_scene(sess)
        mismatch = _iq.ownership_change(item, project=scene["project"],
                                        worktree=scene["worktree"],
                                        task_id=scene["task_id"])
        if mismatch:
            self._queue_hold_and_save(sess, item_id, mismatch)
            return False
        if not _iq.mark_dispatching(q, item_id):
            return False
        saved, err = self._persist_queue_change(sess)
        if not saved:
            _iq.unmark_dispatching(q, item_id, error="保存失败，派发未开始：" + err[:80])
            _iq.set_paused(q, True, "保存失败，队列暂停；请稍后重试")
            self._persist_queue_change(sess)
            self._refresh_queue_panel()
            self._show_toast("派发未开始：" + err, 4000)
            if item.get("source") == "remote":
                self._notify_remote_unaccepted("派发未开始（保存失败）：" + err)
            return False
        if err:
            # 正文已提交但索引失败：提交成功 ≠ 允许继续执行。条目回到 queued
            # （磁盘随后一并更正），绝不带着没保存好的状态硬起线程（实测 worker 1→2）。
            _iq.unmark_dispatching(q, item_id, error="索引保存失败，派发未开始：" + err[:80])
            self._persist_queue_change(sess)
            self._refresh_queue_panel()
            self._show_toast("派发未开始：" + err, 4000)
            if item.get("source") == "remote":
                self._notify_remote_unaccepted("派发未开始：" + err)
            return False
        self._refresh_queue_panel()
        # 统一启动闸：运行占用仲裁与唯一派发出口与 B09a 单条请求共用
        verdict = self._gate_new_run_request(sess, "queue", text=item.get("text") or "",
                                             queue_item_id=item_id)
        if verdict == "start":
            self._admit_queue_dispatch(sess, item_id)
            return True
        if verdict == "busy":
            _iq.unmark_dispatching(q, item_id, error="会话忙或已有启动请求，稍后自动/手动重试")
            self._persist_queue_change(sess)
            self._refresh_queue_panel()
        return verdict == "queued"

    def _admit_queue_dispatch(self, sess, item_id):
        """正式接纳：沿用入队时的 message_id 进历史（B03/B05 照常接入）。
        一次性：条目必须是 dispatching 状态，重复回调 / 迟到信号到此为止。"""
        from .. import input_queue as _iq
        from .. import session as _session
        q = sess.input_queue
        item = _iq.find(q, item_id)
        if item is None or item["state"] != _iq.DISPATCHING:
            return
        if sess is not _session.get_active() or self._worker_alive(sess):
            # 等待期间前台变了或又有 worker：条目回到队列，绝不带病派发
            self._queue_dispatch_aborted(sess, item_id, "会话状态已变化，本次派发未开始")
            return
        pairs, problem = _iq.load_images(item)
        if problem:
            self._queue_dispatch_aborted(sess, item_id, problem)
            return
        scene = self._queue_scene(sess)
        mismatch = _iq.ownership_change(item, project=scene["project"],
                                        worktree=scene["worktree"],
                                        task_id=scene["task_id"])
        if mismatch:
            self._queue_dispatch_aborted(sess, item_id, mismatch)
            return
        if item.get("source") == "remote":
            state.remote_session = True
        ok = self._do_send(item.get("text") or "", pairs, message_id=item["message_id"])
        if ok:
            _iq.mark_admitted(q, item_id)
            self._refresh_queue_panel()
            return
        # 线程构造/启动失败（或视觉模型缺失）：保留原项 + 如实说明 + 可重试
        _iq.unmark_dispatching(q, item_id, error="启动失败：无法创建工作线程，可在面板重试")
        _iq.set_paused(q, True, "启动失败，队列暂停；请稍后重试")
        self._persist_queue_change(sess)
        self._refresh_queue_panel()
        self._show_toast("队列消息启动失败：无法创建工作线程，条目已保留，可重试", 4000)
        if item.get("source") == "remote":
            self._notify_remote_unaccepted("消息启动失败（无法创建工作线程），已在队列保留，可重试")

    def _settle_finished_queue_item(self, sess):
        """给已接纳的队列条目记录程序认定的终态（B03 记录，不是模型自述），
        并核实收尾保存真的成功。返回 (saved_ok, advance_ok, pause_reason)：
        自动推进只在 advance_ok 且保存成功时继续。

        归属必须核对：`last_run.source.message_id` == 条目 message_id 才能把这一轮
        记到它头上。否则（图片识别失败消息未进历史 / 恢复检查拒绝了本轮 / 运行
        记录缺失）绝不把上一轮的 completed 安在这条头上——实测把上一轮的 run_id
        和 completed 抄给了新条目还继续派发——改为如实记 failed、暂停并可重试。

        队列里**有**条目时无论有没有已接纳项都要落盘一次：上一轮可能是普通
        发送（没有对应队列项），它的 finalize 保存失败也必须在这里暴露，
        否则会带着未持久化的状态自动启动下一轮（实测：worker 1 → 2、队列
        不暂停）。队列空则无可推进，无需额外落盘。
        """
        from .. import input_queue as _iq
        q = sess.input_queue
        advance_ok, reason = True, ""
        it = _iq.admitted_item(q)
        if it is not None:
            last = sess.last_run or {}
            src = last.get("source") or {}
            if last.get("phase") == "ended" and \
                    src.get("message_id") == it.get("message_id"):
                outcome = last.get("outcome") or "unknown"
                _iq.mark_done(q, it["queue_item_id"], last.get("id") or "", outcome)
                if outcome != "completed":
                    advance_ok = False
                    reason = (f"上一轮未正常完成（{outcome}），队列已暂停；"
                              "可在面板处理后继续")
            else:
                # 本轮没有真正处理这条：如实记 failed（run_id 留空——没有属于
                # 它的运行），条目终态可见，用户可重新入队
                _iq.mark_done(q, it["queue_item_id"], "", "failed",
                              "本轮未能开始（运行记录缺失或不属于该条消息），"
                              "请检查上方错误后重新入队")
                advance_ok = False
                reason = ("上一轮未能开始（无运行记录或归属不符），队列已暂停；"
                          "可在面板处理后继续")
        elif (sess.last_run or {}).get("phase") == "ended":
            # 上一轮是普通发送（没有对应队列项）：终态与保存同样把关——
            # failed/cancelled/unverified/limit_reached 一律暂停自动推进
            outcome = sess.last_run.get("outcome") or "unknown"
            if outcome != "completed":
                advance_ok = False
                reason = (f"上一轮未正常完成（{outcome}），队列已暂停；"
                          "可在面板处理后继续")
        if not q["items"]:
            return True, True, ""
        saved, err = self._persist_queue_change(sess)
        # 本轮 finalize 的正文保存失败（即便这里的补存成功）→ 按约定暂停
        if getattr(sess, "last_run_save_failed", False):
            advance_ok = False
            reason = "上一轮保存失败，队列已暂停；请检查后继续"
        self._refresh_queue_panel()
        if not saved:
            self._show_toast("队列状态保存失败：" + err[:80], 4000)
        elif err:
            self._show_toast("队列已保存，但" + err, 4000)
        elif not advance_ok:
            self._show_toast(reason, 4000)
        return saved, advance_ok, reason

    def _maybe_auto_advance_queue(self, sess, *, settle_saved=True,
                                  advance_ok=True, pause_reason="", auto=True):
        """队列推进。auto=True：一轮结束后的自动评估——需要上一轮真正运行且
        程序终态为 completed、必要保存成功（由收尾结果 advance_ok / settle_saved
        判定）、队列未暂停、无待启动请求；auto=False：用户显式继续（恢复/重试/
        编辑后），上一轮的旧终态不再拦路，但仍要求前台会话且队列未暂停。
        第一版只自动派发前台会话。"""
        from .. import input_queue as _iq
        from .. import session as _session
        q = sess.input_queue
        if sess is not _session.get_active():
            return      # 后台会话照常执行，但不自动派发（第一版）
        if not settle_saved or not advance_ok:
            _iq.set_paused(q, True, pause_reason or "保存失败，队列已暂停；请稍后重试")
            self._persist_queue_change(sess)
            self._refresh_queue_panel()
            return
        if q.get("paused") or self._pending_run_for(sess) is not None \
                or getattr(sess, "resume_pending", False):
            return
        head, blocked = _iq.next_queued(q)
        if head is None:
            if blocked is not None:
                text = _iq.excerpt(blocked.get("text"), 20)
                _iq.set_paused(q, True, f"「{text}」被暂停：{blocked.get('hold_reason') or '原因见条目'}")
                self._persist_queue_change(sess)
                self._refresh_queue_panel()
            return
        self._queue_begin_dispatch(sess, head["queue_item_id"])

    # ── 队列面板动作（主线程槽） ──

    def _show_queue_edit_dialog(self, item):
        """编辑排队消息的对话框。独立成方法：测试可整只替换，不必戳控件树。
        返回新正文（strip 过）；取消返回 None。"""
        from PySide6.QtWidgets import QDialog, QPlainTextEdit, QVBoxLayout, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("编辑排队消息")
        edit = QPlainTextEdit(item.get("text") or "")
        edit.setMinimumSize(560, 220)
        lay = QVBoxLayout(dlg)
        lay.addWidget(edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if dlg.exec() != QDialog.Accepted:
            return None
        return edit.toPlainText().strip()

    def _on_queue_edit_requested(self, item_id):
        from .. import input_queue as _iq
        from .. import session as _session
        sess = _session.get_active()
        item = _iq.find(sess.input_queue, item_id)
        if item is None or item["state"] != _iq.QUEUED:
            self._show_toast("该条当前状态不能编辑", 2500)
            return
        # 编辑期间 hold 该条：对话框是模态但会处理 Qt 事件，上一轮可能恰好结束，
        # 自动推进会把【原文】派发出去；等用户点确定却得到"已进入派发"，新稿
        # 丢失（实测踩过）。hold 让自动推进在这条前面停下。
        was_paused = bool(sess.input_queue.get("paused"))
        _iq.hold(sess.input_queue, item_id, "正在编辑")
        hold_saved, hold_err = self._persist_queue_change(sess)
        if not hold_saved:
            # 入口保存失败：磁盘还停在 queued，内存与磁盘分叉——恢复可操作的原
            # 条目、暂停队列并【退出编辑】（不打开对话框）。照常编辑的话取消/
            # 确认两条路都会 worker 1 → 2（实测踩过）。
            _iq.retry(sess.input_queue, item_id)          # 恢复可操作（queued）
            _iq.set_paused(sess.input_queue, True,
                           "编辑状态保存失败，队列已暂停；请检查后继续")
            self._persist_queue_change(sess)   # best-effort：尽量把暂停落盘
            self._refresh_queue_panel()
            self._show_toast("修改未生效：编辑状态保存失败：" + hold_err[:80], 4000)
            return
        self._refresh_queue_panel()
        resume_auto = False
        edit_error = ""          # 非空 = 编辑未生效的原因（取消 = 未编辑，不算错误）
        new_text = None
        try:
            new_text = self._show_queue_edit_dialog(item)
        finally:
            # 确认/取消/异常都把条目放回 queued——set_text 的可编辑校验在
            # 状态恢复之后进行（cancel 分支必须无条件恢复）。
            _iq.retry(sess.input_queue, item_id)
            # 只解除【编辑造成】的暂停（blocked-head 原因含"正在编辑"）；编辑期间
            # 新出现的失败/保存暂停必须保留——不能拿编辑前的旧快照一刀切
            # （实测：编辑窗口里上一轮失败，点确定却把失败暂停解除并继续派发）。
            if not was_paused and \
                    "正在编辑" in (sess.input_queue.get("pause_reason") or ""):
                _iq.set_paused(sess.input_queue, False)
                resume_auto = True
            exit_saved, exit_err = self._persist_queue_change(sess)
            if not exit_saved:
                # 恢复状态（held → queued + 解除暂停）的保存失败：磁盘还停在旧状态，
                # 内存与磁盘分叉——明确暂停、提示并禁止自动推进，绝不能照常派发
                # （实测：worker 1 → 2，原消息照跑）。
                edit_error = "状态保存失败：" + exit_err[:80]
                resume_auto = False
                _iq.set_paused(sess.input_queue, True,
                               "编辑状态保存失败，队列已暂停；请检查后继续")
                self._persist_queue_change(sess)   # best-effort：尽量把暂停落盘
            self._refresh_queue_panel()
        if new_text is not None and new_text:
            ok, reason, backup = _iq.set_text(sess.input_queue, item_id, new_text)
            if not ok:
                edit_error = reason
            else:

                def _undo():
                    it = _iq.find(sess.input_queue, item_id)
                    if it is not None and it["text"] == new_text:
                        it["text"] = backup

                saved, err = self._persist_queue_change(sess, undo=_undo)
                self._refresh_queue_panel()
                if not saved:
                    edit_error = "修改未保存：" + err[:80]
                    # 编辑保存失败：队列状态没落盘，明确暂停、保留原条目，
                    # 让用户手动继续——不能带着未保存的状态自动执行旧消息
                    # （实测：worker 1 → 2，原消息照跑）。空内容/超限校验
                    # 不涉及保存，仍恢复原消息推进。
                    _iq.set_paused(sess.input_queue, True,
                                   "编辑保存失败，队列已暂停；请检查后继续")
                    resume_auto = False
                    self._persist_queue_change(sess)   # best-effort：尽量把暂停落盘
                    self._refresh_queue_panel()
                else:
                    self._show_toast("已修改" if not err
                                     else "已修改，但" + err, 2500)
        elif new_text is not None:
            edit_error = "内容为空，未修改"
        # 统一退出分支：编辑未生效（取消/空内容/校验失败）时，原消息也要恢复推进
        # ——否则条目永远停在 queued、面板却说"自动处理"（实测踩过）。编辑保存
        # 失败例外：上面已明确暂停，等用户手动继续。运行失败、保存失败造成的
        # 暂停由 advance 的 paused 守卫保留，不会被这里绕过。
        if resume_auto:
            self._maybe_auto_advance_queue(sess, auto=False)
        if edit_error:
            self._show_toast("修改未生效：" + edit_error[:80], 4000)
    def _on_queue_delete_requested(self, item_id):
        from .. import input_queue as _iq
        from .. import session as _session
        sess = _session.get_active()
        item = _iq.find(sess.input_queue, item_id)
        ok, reason, index = _iq.delete(sess.input_queue, item_id)
        if not ok:
            self._show_toast(reason, 3000)
            return

        def _undo():
            _iq.restore_at(sess.input_queue, item, index)

        saved, err = self._persist_queue_change(sess, undo=_undo)
        self._refresh_queue_panel()
        if not saved:
            self._show_toast("删除未保存：" + err[:80], 4000)
        else:
            self._show_toast("已删除" if not err else "已删除，但" + err, 2500)

    def _on_queue_process_now_requested(self, item_id):
        self._queue_process_now(item_id)

    def _on_queue_retry_requested(self, item_id):
        from .. import input_queue as _iq
        from .. import session as _session
        sess = _session.get_active()
        q = sess.input_queue
        item = _iq.find(q, item_id)
        if item is None:
            return
        if item["state"] == _iq.NEEDS_CHECK:
            # 派发中断窗口的条目：作为一条新输入重新入队（新身份），旧条目删除。
            # 这是【替换】——先删旧条目腾出容量再新增，否则满队列时容量检查
            # 会拒绝、恢复项永远换不进去（实测踩过）；保存失败按原位恢复旧项。
            scene = self._queue_scene(sess)
            ok_del, del_reason, old_index = _iq.delete(q, item_id)
            if not ok_del:
                self._show_toast(del_reason, 3000)
                return
            new_item, reason = _iq.requeue_as_new(
                q, item.get("text") or [], [], image_refs=item.get("images") or [],
                project=scene["project"], worktree=scene["worktree"],
                task_id=scene["task_id"], source=item.get("source") or "send")
            if new_item is None:
                _iq.restore_at(q, item, old_index)
                self._persist_queue_change(sess)
                self._refresh_queue_panel()
                self._show_toast(reason, 3000)
                return

            def _undo():
                _iq.delete(q, new_item["queue_item_id"])
                _iq.restore_at(q, item, old_index)

            saved, err = self._persist_queue_change(sess, undo=_undo)
            self._refresh_queue_panel()
            self._show_toast("已重新入队" if saved else "重新入队未保存：" + err[:80], 3000)
            return
        # held：重试前先重过附件与归属校验，通过了才回 queued
        pairs, problem = _iq.load_images(item)
        if problem:
            item["hold_reason"] = problem
            self._persist_queue_change(sess)
            self._refresh_queue_panel()
            self._show_toast("仍不能派发：" + problem, 4000)
            return
        scene = self._queue_scene(sess)
        mismatch = _iq.ownership_change(item, project=scene["project"],
                                        worktree=scene["worktree"],
                                        task_id=scene["task_id"])
        if mismatch:
            item["hold_reason"] = mismatch
            self._persist_queue_change(sess)
            self._refresh_queue_panel()
            self._show_toast("仍不能派发：" + mismatch, 4000)
            return
        ok, reason = _iq.retry(q, item_id)
        if not ok:
            self._show_toast(reason, 3000)
            return
        _iq.set_paused(q, False)
        saved, err = self._persist_queue_change(sess)
        self._refresh_queue_panel()
        if saved:
            self._maybe_auto_advance_queue(sess, auto=False)

    def _on_queue_discard_requested(self, item_id):
        from .. import input_queue as _iq
        from .. import session as _session
        sess = _session.get_active()
        item = _iq.find(sess.input_queue, item_id)
        ok, reason, index = _iq.delete(sess.input_queue, item_id)
        if not ok:
            self._show_toast(reason, 3000)
            return

        def _undo():
            _iq.restore_at(sess.input_queue, item, index)

        saved, err = self._persist_queue_change(sess, undo=_undo)
        self._refresh_queue_panel()
        if not saved:
            self._show_toast("丢弃未保存：" + err[:80], 4000)

    def _on_queue_resume_requested(self):
        from .. import input_queue as _iq
        from .. import session as _session
        sess = _session.get_active()
        q = sess.input_queue
        old_reason = q.get("pause_reason")
        _iq.set_paused(q, False)
        saved, err = self._persist_queue_change(sess)
        if not saved:
            # 回滚：内存回到暂停态，与磁盘一致（不能提示"恢复未保存"却已恢复）
            _iq.set_paused(q, True, old_reason or "")
            self._refresh_queue_panel()
            self._show_toast("恢复未保存：" + err[:80], 4000)
            return
        self._refresh_queue_panel()
        self._maybe_auto_advance_queue(sess, auto=False)
