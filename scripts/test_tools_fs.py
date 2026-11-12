"""文件系统动词工具（delete_path / move_path / copy_path / make_directory）的回归。

分四层：
- 工具行为：成功路径 + 每一条边界（不递归删、不覆盖、空目录、符号链接、同名文件）；
- 确认卡：卡路径与操作类型对不对、预览里看不看得到实际内容、拒绝后现场不动、无 UI 默认拒绝；
- 撤销：删除/移动/复制都能恢复，外部改动后判冲突不覆盖，版本链不完整拒绝；
- 闸门：Plan / 遥控只读 / 知识库模式拦得住，删除和移动在"接受编辑/自动"下仍然问、不给"记住"。

写入一律走 `streaming._execute_tool`（真实入口，含执行前记录），这样撤销记录才有运行归属。
"""
import os
import threading
from types import SimpleNamespace

import pytest

from src import file_history as fh
from src import memory, permission_rules as rules, session, state
from src.ui.confirm_bars import ConfirmBarsMixin


@pytest.fixture(autouse=True)
def _no_real_models(monkeypatch):
    from src import agent, claude_code, models

    def _forbidden(*a, **k):
        raise AssertionError("测试不许调用真实模型或 Claude CLI")

    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(agent, "_claude_code_loop", _forbidden)
    monkeypatch.setattr(claude_code, "claude_code_loop", _forbidden)
    import socket
    monkeypatch.setattr(socket.socket, "connect", _forbidden)


@pytest.fixture()
def workspace(tmp_path, isolated_memory):
    proj = tmp_path / "ws"
    proj.mkdir()
    state.current_project = str(proj)
    sess = session.get_active()
    sess.project = str(proj)
    return proj


@pytest.fixture()
def real_ui(monkeypatch, isolated_memory):
    """真 ChatUI（offscreen），模型和网络一律禁掉——只测确认卡这条路径。"""
    import socket
    from unittest.mock import Mock

    def _forbidden(*a, **k):
        raise AssertionError("确认测试里不许访问模型 / 网络")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    from src import agent, config, mcp_client, models, tools
    fake = Mock()
    fake.bind_tools.return_value = fake
    fake.invoke.side_effect = _forbidden
    fake.stream.side_effect = _forbidden
    monkeypatch.setattr(models, "_create_llm", lambda *a: fake)
    monkeypatch.setattr(agent, "_create_llm", lambda *a: fake)
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)
    from src.ui.chat_window import ChatUI
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(state, "ui_ref", None)
    ui = ChatUI()
    try:
        yield ui, app
    finally:
        ui._release_pending_confirm()
        ui._release_pending_edit()
        ui.hide()
        ui.deleteLater()
        app.processEvents()


class _CardUI:
    """最小 UI：记下每张确认卡的 (路径, 预览, 操作类型)，按 approve 决定放不放行。"""

    def __init__(self, approve=True):
        self.approve = approve
        self.cards = []
        self.messages = []
        self.on_confirm = None

    def show_message(self, text, tag=""):
        self.messages.append((tag, str(text)))

    def confirm_edit(self, path, diff_text, op="edit"):
        self.cards.append((path, diff_text, op))
        if self.on_confirm is not None:
            self.on_confirm()
        return self.approve, "" if self.approve else "别动这个文件"

    def confirm_command(self, *a, **k):
        raise AssertionError("路径工具不该走命令确认卡")


_counter = {"n": 0}


def _drive(name, args, ui=None, sess=None):
    """真实调用入口。返回 (工具结果文本, ui)。"""
    from src import streaming
    _counter["n"] += 1
    ui = ui if ui is not None else _CardUI()
    sess = sess or session.current_session()
    if getattr(sess, "active_run_id", None) is None:
        sess.active_run_id = f"run-fs-{_counter['n']}"
    state.ui_ref = ui
    tc = {"name": name, "args": args, "id": f"call-fs-{_counter['n']}"}
    streaming._execute_tool(tc, ui)
    msg = sess.chat_history[-1]
    assert msg.type == "tool" and msg.tool_call_id == tc["id"]
    return msg.content, ui


@pytest.fixture()
def bound(workspace):
    """落盘拿到 session_id 并绑定当前线程——撤销记录要求真实运行归属。"""
    from langchain_core.messages import HumanMessage, SystemMessage
    sess = session.get_active()
    sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="整理一下文件")]
    memory.save_session(session=sess)
    assert sess.current_session_id
    session.bind_thread(sess)
    yield sess
    session.unbind_thread()
    state.ui_ref = None


def _undo(sess):
    root = fh.session_workspace()
    cand = fh.latest_undoable(sess.current_session_id, root)
    assert cand is not None, "没有可撤销记录"
    return fh.execute_undo(cand["checkpoint_id"], session_id=sess.current_session_id,
                           workspace=root)


# ══════════════════════════════════════════════════════════════
# 删除
# ══════════════════════════════════════════════════════════════

def test_delete_file_then_undo_restores_bytes(bound, workspace):
    target = workspace / "app.txt"
    original = "第一行\r\nsecond\r\n".encode("utf-8")
    target.write_bytes(original)
    result, ui = _drive("delete_path", {"path": "app.txt"})
    assert result.startswith("成功删除文件"), result
    assert not target.exists()
    assert ui.cards[0][2] == "delete"
    assert "⚠️" not in result, "有运行归属时应当可撤销"

    done = _undo(bound)
    assert done["status"] == "ok" and done["restored"], done
    assert target.read_bytes() == original


def test_delete_undo_recreates_removed_parent_dir(bound, workspace):
    """删了文件后又把它所在的父目录也删掉，撤销仍要能把文件连同父目录一起建回来。"""
    sub = workspace / "sub"
    sub.mkdir()
    target = sub / "a.py"
    original = b"print('hi')\n"
    target.write_bytes(original)
    result, _ = _drive("delete_path", {"path": "sub/a.py"})
    assert result.startswith("成功删除文件"), result
    sub.rmdir()
    assert not sub.exists()

    done = _undo(bound)
    assert done["status"] == "ok" and done["restored"], done
    assert target.read_bytes() == original


def test_rewind_to_empty_keeps_file_undo(bound, workspace):
    """回退到第一条消息（清空对话）不能连带删掉文件撤销材料：顶栏撤销仍要能用。"""
    target = workspace / "keep.txt"
    original = b"important\n"
    target.write_bytes(original)
    result, _ = _drive("delete_path", {"path": "keep.txt"})
    assert result.startswith("成功删除文件"), result
    sid = bound.current_session_id
    root = fh.session_workspace()
    assert fh.latest_undoable(sid, root) is not None

    dropped = memory.rewind_session(bound, 1)
    assert dropped >= 1
    assert len(bound.chat_history) == 1
    # 会话 id 保留 + 撤销材料保留 → 顶栏撤销按钮（读 current_session_id）找得到
    assert bound.current_session_id == sid
    assert fh.latest_undoable(sid, root) is not None
    done = _undo(bound)
    assert done["status"] == "ok" and done["restored"], done
    assert target.read_bytes() == original


def test_delete_card_shows_the_actual_content(bound, workspace):
    (workspace / "a.py").write_text("import os\nprint('要没了')\n", encoding="utf-8")
    _, ui = _drive("delete_path", {"path": "a.py"})
    path, preview, op = ui.cards[0]
    assert op == "delete" and path.endswith("a.py")
    assert "-import os" in preview and "-print('要没了')" in preview
    assert "+++ /dev/null" in preview


def test_delete_long_file_card_says_it_deletes_everything(bound, workspace):
    (workspace / "big.txt").write_text("\n".join(f"line {i}" for i in range(500)),
                                       encoding="utf-8")
    _, ui = _drive("delete_path", {"path": "big.txt"})
    preview = ui.cards[0][1]
    assert "-line 0" in preview and "-line 499" not in preview
    assert "整个文件" in preview and "500 行" in preview


def test_delete_binary_file_card_does_not_pretend_to_show_text(bound, workspace):
    (workspace / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    _, ui = _drive("delete_path", {"path": "blob.bin"})
    assert "二进制文件" in ui.cards[0][1]


def test_delete_refuses_non_empty_directory(bound, workspace):
    d = workspace / "pkg"
    d.mkdir()
    (d / "keep.txt").write_text("别删我", encoding="utf-8")
    result, ui = _drive("delete_path", {"path": "pkg"})
    assert "不递归删除" in result and not result.startswith("成功")
    assert (d / "keep.txt").exists()
    assert ui.cards == [], "拒绝发生在确认之前，不该弹卡"


def test_delete_empty_directory_is_honest_about_undo(bound, workspace):
    d = workspace / "empty"
    d.mkdir()
    result, _ = _drive("delete_path", {"path": "empty"})
    assert result.startswith("成功删除空目录") and not d.exists()
    assert "目录不在撤销跟踪范围内" in result
    assert fh.latest_undoable(bound.current_session_id, fh.session_workspace()) is None


def test_delete_missing_path_never_asks(bound, workspace):
    result, ui = _drive("delete_path", {"path": "nope.txt"})
    assert "路径不存在" in result and ui.cards == []


def test_delete_refuses_symlink(bound, workspace):
    target = workspace / "real.txt"
    target.write_text("内容", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    result, ui = _drive("delete_path", {"path": "link.txt"})
    assert "符号链接" in result and target.exists() and ui.cards == []


def test_delete_rejected_by_user_keeps_file(bound, workspace):
    target = workspace / "keep.py"
    target.write_text("x = 1\n", encoding="utf-8")
    result, _ = _drive("delete_path", {"path": "keep.py"}, ui=_CardUI(approve=False))
    assert result.startswith("已拒绝") and "别动这个文件" in result
    assert target.read_text(encoding="utf-8") == "x = 1\n"
    assert fh.latest_undoable(bound.current_session_id, fh.session_workspace()) is None


def test_delete_aborts_when_file_changed_during_confirmation(bound, workspace):
    target = workspace / "race.txt"
    target.write_text("确认时看到的", encoding="utf-8")
    ui = _CardUI()
    ui.on_confirm = lambda: target.write_text("用户自己改的", encoding="utf-8")
    result, _ = _drive("delete_path", {"path": "race.txt"}, ui=ui)
    assert "写入前核对失败" in result
    assert target.read_text(encoding="utf-8") == "用户自己改的"


def test_delete_without_ui_is_refused(bound, workspace, monkeypatch):
    target = workspace / "x.txt"
    target.write_text("内容", encoding="utf-8")
    monkeypatch.setattr(state, "ui_ref", None)
    monkeypatch.setattr(state, "headless_auto_approve", False)
    from src import tools_fs
    out = tools_fs.delete_path.func("x.txt")
    assert "没有确认界面" in out and target.exists()


# ══════════════════════════════════════════════════════════════
# 移动
# ══════════════════════════════════════════════════════════════

def test_move_file_then_undo_moves_it_back(bound, workspace):
    src = workspace / "old.py"
    original = b"value = 1\r\n"
    src.write_bytes(original)
    result, ui = _drive("move_path", {"source": "old.py", "destination": "sub/new.py"})
    assert result.startswith("成功移动"), result
    assert not src.exists() and (workspace / "sub" / "new.py").read_bytes() == original
    assert ui.cards[0][2] == "move"
    assert "old.py" in ui.cards[0][1] and "sub/new.py" in ui.cards[0][1]

    done = _undo(bound)
    assert done["status"] == "ok" and done["restored"], done
    assert src.read_bytes() == original
    assert not (workspace / "sub" / "new.py").exists()


def test_move_does_not_store_a_full_byte_backup(bound, workspace):
    """移动只挪位置、不改内容：撤销靠把 dest 的文件挪回来 + 指纹核对，
    不该再存一份用不上的整份字节备份（#9）。"""
    (workspace / "old.py").write_bytes(b"value = 1\r\n" * 500)
    _drive("move_path", {"source": "old.py", "destination": "new.py"})
    records = [r for _c, r, _e in fh._scan_records() if r and r["tool"] == "move_path"]
    assert len(records) == 1
    rec = records[0]
    assert rec["pre"]["existed"] and rec["pre"]["sha256"], "移动仍要记指纹用于核对"
    assert not rec["pre"]["blob"], "移动不该存整份备份"
    assert not os.path.exists(fh._blob_path(rec["checkpoint_id"])), "不该落 .bin 备份文件"


def test_move_undo_works_even_if_byte_backup_is_gone(bound, workspace):
    """就算整份备份缺失（旧记录被清理 / 损坏），移动撤销仍要能用——
    真正的材料是 dest 处的文件本身，不是那份冗余备份（#9）。"""
    src = workspace / "old.py"
    original = b"value = 1\r\n"
    src.write_bytes(original)
    _drive("move_path", {"source": "old.py", "destination": "new.py"})
    records = [r for _c, r, _e in fh._scan_records() if r and r["tool"] == "move_path"]
    blob = fh._blob_path(records[0]["checkpoint_id"])
    if os.path.exists(blob):
        os.remove(blob)
    done = _undo(bound)
    assert done["status"] == "ok" and done["restored"], done
    assert src.read_bytes() == original
    assert not (workspace / "new.py").exists()


def test_move_refuses_to_overwrite(bound, workspace):
    (workspace / "a.txt").write_text("源", encoding="utf-8")
    (workspace / "b.txt").write_text("目标已有的内容", encoding="utf-8")
    result, ui = _drive("move_path", {"source": "a.txt", "destination": "b.txt"})
    assert "不覆盖" in result and ui.cards == []
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "源"
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "目标已有的内容"


def test_move_refuses_into_itself(bound, workspace):
    d = workspace / "pkg"
    d.mkdir()
    (d / "f.txt").write_text("x", encoding="utf-8")
    result, _ = _drive("move_path", {"source": "pkg", "destination": "pkg/inner"})
    assert "它自己里面" in result and (d / "f.txt").exists()


def test_move_same_path_is_refused(bound, workspace):
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    result, _ = _drive("move_path", {"source": "a.txt", "destination": "a.txt"})
    assert "同一个路径" in result


def test_move_directory_works_and_says_it_is_not_undoable(bound, workspace):
    d = workspace / "pkg"
    d.mkdir()
    (d / "f.txt").write_text("跟着走", encoding="utf-8")
    result, _ = _drive("move_path", {"source": "pkg", "destination": "renamed"})
    assert result.startswith("成功移动"), result
    assert (workspace / "renamed" / "f.txt").read_text(encoding="utf-8") == "跟着走"
    assert "目录不在撤销跟踪范围内" in result
    assert fh.latest_undoable(bound.current_session_id, fh.session_workspace()) is None


def test_move_aborts_when_destination_appears_during_confirmation(bound, workspace):
    (workspace / "a.txt").write_text("源", encoding="utf-8")
    ui = _CardUI()
    ui.on_confirm = lambda: (workspace / "b.txt").write_text("外部新建", encoding="utf-8")
    result, _ = _drive("move_path", {"source": "a.txt", "destination": "b.txt"}, ui=ui)
    assert "目标位置已经有东西" in result
    assert (workspace / "a.txt").exists()
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "外部新建"


# ══════════════════════════════════════════════════════════════
# 复制 / 建目录
# ══════════════════════════════════════════════════════════════

def test_copy_file_then_undo_removes_the_copy(bound, workspace):
    src = workspace / "a.txt"
    src.write_bytes(b"copy me\r\n")
    result, ui = _drive("copy_path", {"source": "a.txt", "destination": "out/b.txt"})
    assert result.startswith("成功复制"), result
    assert (workspace / "out" / "b.txt").read_bytes() == b"copy me\r\n"
    assert ui.cards[0][2] == "copy" and "+copy me" in ui.cards[0][1]

    done = _undo(bound)
    assert done["status"] == "ok", done
    assert not (workspace / "out" / "b.txt").exists()
    assert src.read_bytes() == b"copy me\r\n", "源文件不该被动"


def test_copy_refuses_to_overwrite_and_refuses_directories(bound, workspace):
    (workspace / "a.txt").write_text("源", encoding="utf-8")
    (workspace / "b.txt").write_text("已有", encoding="utf-8")
    result, _ = _drive("copy_path", {"source": "a.txt", "destination": "b.txt"})
    assert "不覆盖" in result
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "已有"

    (workspace / "d").mkdir()
    result, _ = _drive("copy_path", {"source": "d", "destination": "d2"})
    assert "不复制目录" in result and not (workspace / "d2").exists()


def test_make_directory_creates_parents_and_is_idempotent(bound, workspace):
    result, ui = _drive("make_directory", {"path": "a/b/c"})
    assert result.startswith("成功新建目录") and (workspace / "a" / "b" / "c").is_dir()
    assert ui.cards == [], "只建目录不该弹确认卡"
    again, _ = _drive("make_directory", {"path": "a/b/c"})
    assert "已存在" in again


def test_make_directory_refuses_existing_file(bound, workspace):
    (workspace / "f.txt").write_text("x", encoding="utf-8")
    result, _ = _drive("make_directory", {"path": "f.txt"})
    assert "同名文件已存在" in result
    assert (workspace / "f.txt").read_text(encoding="utf-8") == "x"


def test_make_directory_refuses_outside_workspace(bound, workspace):
    """主 Agent 也只能在项目根内建目录，不能往项目外 / 系统目录乱建。"""
    outside = workspace.parent / "outside_dir"
    result, ui = _drive("make_directory", {"path": str(outside)})
    assert "超出项目范围" in result and not result.startswith("成功"), result
    assert not outside.exists()
    assert ui.cards == []


def test_make_directory_without_ui_is_refused(bound, workspace, monkeypatch):
    """无确认界面、又没显式打开 headless_auto_approve 时默认拒绝（与写盘类同一道闸）。"""
    monkeypatch.setattr(state, "ui_ref", None)
    monkeypatch.setattr(state, "headless_auto_approve", False)
    from src import tools_fs
    out = tools_fs.make_directory.func("newdir")
    assert "没有确认界面" in out
    assert not (workspace / "newdir").exists()


# ══════════════════════════════════════════════════════════════
# 撤销的现场核对
# ══════════════════════════════════════════════════════════════

def test_undo_delete_refuses_when_path_was_recreated(bound, workspace):
    target = workspace / "gone.txt"
    target.write_text("原来的", encoding="utf-8")
    _drive("delete_path", {"path": "gone.txt"})
    target.write_text("用户新建的", encoding="utf-8")

    root = fh.session_workspace()
    cand = fh.latest_undoable(bound.current_session_id, root)
    pre = fh.precheck(cand["checkpoint_id"], session_id=bound.current_session_id, workspace=root)
    assert pre["status"] == fh.ST_CONFLICT and "又出现了文件" in pre["reason"]
    done = fh.execute_undo(cand["checkpoint_id"], session_id=bound.current_session_id,
                           workspace=root)
    assert done["status"] != "ok" and not done["changed"]
    assert target.read_text(encoding="utf-8") == "用户新建的"


def test_undo_move_refuses_when_moved_file_was_edited(bound, workspace):
    (workspace / "a.txt").write_text("移动前", encoding="utf-8")
    _drive("move_path", {"source": "a.txt", "destination": "b.txt"})
    (workspace / "b.txt").write_text("移动之后又改的", encoding="utf-8")

    root = fh.session_workspace()
    cand = fh.latest_undoable(bound.current_session_id, root)
    pre = fh.precheck(cand["checkpoint_id"], session_id=bound.current_session_id, workspace=root)
    assert pre["status"] == fh.ST_CONFLICT and "移动之后又被修改" in pre["reason"]
    assert not (workspace / "a.txt").exists()
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "移动之后又改的"


def test_undo_move_refuses_when_source_path_reappeared(bound, workspace):
    (workspace / "a.txt").write_text("移动前", encoding="utf-8")
    _drive("move_path", {"source": "a.txt", "destination": "b.txt"})
    (workspace / "a.txt").write_text("原路径又有了", encoding="utf-8")
    root = fh.session_workspace()
    cand = fh.latest_undoable(bound.current_session_id, root)
    pre = fh.precheck(cand["checkpoint_id"], session_id=bound.current_session_id, workspace=root)
    assert pre["status"] == fh.ST_CONFLICT
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "移动前"


def test_delete_then_recreate_is_a_conflict_not_a_silent_overwrite(bound, workspace):
    target = workspace / "a.txt"
    target.write_text("v0", encoding="utf-8")
    _drive("delete_path", {"path": "a.txt"})
    _drive("write_file", {"path": "a.txt", "content": "v1"})
    root = fh.session_workspace()
    records = [r for _cid, r, _e in fh._scan_records() if r is not None]
    delete_rec = next(r for r in records if r["tool"] == "delete_path")
    pre = fh.precheck(delete_rec["checkpoint_id"], session_id=bound.current_session_id,
                      workspace=root)
    assert pre["status"] != fh.ST_RESTORABLE
    done = fh.execute_undo(delete_rec["checkpoint_id"],
                           session_id=bound.current_session_id, workspace=root)
    assert not done["changed"]
    assert target.read_text(encoding="utf-8") == "v1"


def test_delete_chain_check_fires_even_when_the_path_is_empty_again(bound, workspace):
    """删 → 写 → 再删：原路径又空了、指纹也无从比对，但中间那两笔没撤销，仍须拒绝。"""
    target = workspace / "a.txt"
    target.write_text("v0", encoding="utf-8")
    _drive("delete_path", {"path": "a.txt"})
    _drive("write_file", {"path": "a.txt", "content": "v1"})
    _drive("delete_path", {"path": "a.txt"})
    root = fh.session_workspace()
    records = sorted((r for _cid, r, _e in fh._scan_records() if r is not None),
                     key=lambda r: r["created_at"])
    first_delete = records[0]
    pre = fh.precheck(first_delete["checkpoint_id"], session_id=bound.current_session_id,
                      workspace=root)
    assert pre["status"] == fh.ST_UNSUPPORTED and "版本链不完整" in pre["reason"]
    assert not target.exists()


def test_move_record_keeps_both_paths_in_the_version_chain(bound, workspace):
    """挪走之后在新位置写了一笔**内容相同**的：指纹对得上，但版本链不完整，仍须拒绝。"""
    (workspace / "a.txt").write_text("v0", encoding="utf-8")
    _drive("move_path", {"source": "a.txt", "destination": "b.txt"})
    _drive("write_file", {"path": "b.txt", "content": "v0"})
    root = fh.session_workspace()
    records = [r for _cid, r, _e in fh._scan_records() if r is not None]
    move_rec = next(r for r in records if r["tool"] == "move_path")
    pre = fh.precheck(move_rec["checkpoint_id"], session_id=bound.current_session_id,
                      workspace=root)
    assert pre["status"] == fh.ST_UNSUPPORTED and "版本链不完整" in pre["reason"]


def test_delete_record_survives_a_fresh_reader(bound, workspace):
    """记录落盘后按保存的材料核对（跨进程恢复同一条路径）。"""
    target = workspace / "a.txt"
    target.write_bytes(b"\xef\xbb\xbfcrlf\r\n")
    _drive("delete_path", {"path": "a.txt"})
    root = fh.session_workspace()
    cand = fh.latest_undoable(bound.current_session_id, root)
    assert cand["kind"] == fh.KIND_DELETE
    reloaded, error = fh._load_record(cand["checkpoint_id"])
    assert error == "" and reloaded["kind"] == fh.KIND_DELETE
    done = fh.execute_undo(cand["checkpoint_id"], session_id=bound.current_session_id,
                           workspace=root)
    assert done["status"] == "ok" and target.read_bytes() == b"\xef\xbb\xbfcrlf\r\n"


def test_corrupt_delete_record_is_rejected_not_guessed(bound, workspace):
    """手改坏 kind/dest 的记录一律作废：删除记录带着写后指纹就不能再拿它当"原路径为空"。"""
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    _drive("delete_path", {"path": "a.txt"})
    cand = fh.latest_undoable(bound.current_session_id, fh.session_workspace())
    path = fh._record_path(cand["checkpoint_id"])
    import json
    raw = json.loads(open(path, encoding="utf-8").read())
    raw["post"] = {"sha256": "0" * 64, "size": 1}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f)
    loaded, error = fh._load_record(cand["checkpoint_id"])
    assert loaded is None and "结构非法" in error


# ══════════════════════════════════════════════════════════════
# 闸门：模式 / 遥控 / 子 Agent
# ══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tool,args", [
    ("delete_path", {"path": "a.txt"}),
    ("move_path", {"source": "a.txt", "destination": "b.txt"}),
    ("copy_path", {"source": "a.txt", "destination": "b.txt"}),
    ("make_directory", {"path": "newdir"}),
])
def test_plan_mode_blocks_all_four(bound, workspace, tool, args):
    from src import permission_modes as pm
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    pm.apply(bound, pm.PLAN)
    result, ui = _drive(tool, args)
    assert "Plan 模式" in result and ui.cards == []
    assert (workspace / "a.txt").exists() and not (workspace / "b.txt").exists()
    assert not (workspace / "newdir").exists()


@pytest.mark.parametrize("tool,args", [
    ("delete_path", {"path": "a.txt"}),
    ("move_path", {"source": "a.txt", "destination": "b.txt"}),
    ("copy_path", {"source": "a.txt", "destination": "b.txt"}),
    ("make_directory", {"path": "newdir"}),
])
def test_remote_readonly_blocks_all_four(bound, workspace, monkeypatch, tool, args):
    from src import config
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(state, "remote_session", True)
    monkeypatch.setattr(config, "REMOTE_MODE", "safe_readonly")
    result, _ = _drive(tool, args)
    assert "只读模式" in result
    assert (workspace / "a.txt").exists() and not (workspace / "newdir").exists()


@pytest.mark.parametrize("mode", ["manual", "accept_edits", "auto"])
def test_delete_and_move_always_ask_even_in_accept_edits(bound, workspace, mode, monkeypatch):
    """放行判定在 confirm_edit 里，所以这一层要用真 mixin，不能用只会弹卡的假 UI。"""
    from src import config, permission_modes as pm
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)
    pm.apply(bound, mode)
    p = str(workspace / "a.txt")
    for op in ("delete", "move"):
        host = _AskHost()
        allowed, _ = host.confirm_edit(p, "--- a\n+++ b\n", op)
        assert not allowed and host.asked == [p], f"{mode} 下 {op} 仍必须问"


def test_bypass_skips_the_card(bound, workspace, monkeypatch):
    from src import config, permission_modes as pm
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)
    pm.apply(bound, "bypass")
    host = _AskHost()
    assert host.confirm_edit(str(workspace / "a.txt"), "-x", "delete") == (True, "")
    assert host.asked == []


@pytest.mark.parametrize("mode", ["accept_edits", "auto"])
def test_copy_follows_the_edit_rules(bound, workspace, mode, monkeypatch):
    from src import config, permission_modes as pm
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)
    pm.apply(bound, mode)
    host = _AskHost()
    assert host.confirm_edit(str(workspace / "b.txt"), "+x", "copy") == (True, "")
    assert host.asked == [], "复制是写文件那一类，接受编辑/自动下免问"


def test_trusted_path_still_asks_for_delete_and_move(bound, workspace, monkeypatch):
    """记住过这个文件（内存 + 落盘都有）也不能免掉删除/移动的确认；复制可以。"""
    from src import config
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)
    p = str(workspace / "a.txt")
    bound.edit_path_allowlist.add(p)
    rules.add_rule(rules.KIND_EDIT_PATH, p, str(workspace))

    for op in ("delete", "move"):
        host = _AskHost()
        allowed, _ = host.confirm_edit(p, "--- a\n+++ b\n", op)
        assert not allowed and host.asked == [p], f"{op} 必须仍然问"
    host = _AskHost()
    assert host.confirm_edit(p, "--- a\n+++ b\n", "copy")[0] is True
    assert host.asked == [], "复制走写文件那套，信任过的文件免问"


def test_remote_remember_cannot_trust_a_delete(bound, workspace, monkeypatch):
    """手机端 inline 按钮带 remember 也不给删除落规则。"""
    import time

    from src import config
    from src.ui import confirm_bars
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", True)
    monkeypatch.setattr(confirm_bars.telegram_push, "push_confirm", lambda *a, **k: None)
    monkeypatch.setattr(confirm_bars.telegram_push, "edit_message_text", lambda *a, **k: None)
    p = str(workspace / "a.txt")
    host = _RemoteHost()
    out = []

    def worker():
        session.bind_thread(bound)
        try:
            out.append(host.confirm_edit(p, "-x", "delete"))
        finally:
            session.unbind_thread()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    ids = []
    for _ in range(500):
        with confirm_bars._pending_lock:
            ids = list(confirm_bars._pending_remote_confirms)
        if ids:
            break
        time.sleep(0.01)
    assert ids, "没有推出手机确认"
    assert confirm_bars._resolve_remote_confirm(ids[0], True, remember=True)
    t.join(5)
    assert out == [(True, "")]
    assert rules.list_rules() == [], "删除不该落任何持久规则"
    assert not bound.edit_path_allowlist


class _AskHost(ConfirmBarsMixin):
    """真 mixin + 假 signal：要弹卡时记下"问了"并当场拒绝，不卡住等人点。"""

    def __init__(self):
        self.asked = []
        host = self

        class _Signal:
            def emit(self, *args):
                result, done = args[-2], args[-1]
                host.asked.append(args[0])
                result["allow"] = False
                done.set()

        self.bridge = SimpleNamespace(confirm_request=_Signal(),
                                      edit_confirm_request=_Signal())


class _RemoteHost(ConfirmBarsMixin):
    """只走手机确认、不弹 PC 卡的宿主。"""

    class _Sig:
        def emit(self, *args):
            pass

    bridge = SimpleNamespace(edit_confirm_request=_Sig(), confirm_request=_Sig())


def test_delete_card_hides_remember_and_says_it_deletes(real_ui, workspace):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    ui, app = real_ui
    sess = session.get_active()
    sess.project = str(workspace)
    session.bind_thread(sess)
    result, done = {"_session": sess, "_op": "delete"}, threading.Event()
    ui._on_edit_confirm_request(str(workspace / "a.txt"), "--- a/a.txt\n+++ /dev/null\n-x\n",
                                result, done)
    app.processEvents()
    assert ui._edit_confirm_title.text() == "确认删除文件"
    assert ui._edit_confirm_badge.text() == "会删文件"
    assert not ui._edit_trust_btn.isVisible()
    QTest.keyClick(ui.edit_confirm_bar, Qt.Key_3)     # 数字键仍会 click 隐藏的按钮
    assert done.is_set() and result["allow"] is True
    assert rules.list_rules() == [] and not sess.edit_path_allowlist
    session.unbind_thread()


def test_subagent_cannot_touch_paths_outside_its_worktree(bound, workspace, monkeypatch):
    outside = workspace.parent / "outside.txt"
    outside.write_text("外面的", encoding="utf-8")
    bound.is_subagent = True
    bound.worktree = str(workspace / "wt")
    (workspace / "wt").mkdir()
    from src import tools_fs
    assert "只能访问自己的 worktree" in tools_fs.delete_path.func(str(outside))
    assert "只能访问自己的 worktree" in tools_fs.move_path.func(str(outside), "x.txt")
    assert "只能访问自己的 worktree" in tools_fs.copy_path.func(str(outside), "x.txt")
    assert "只能访问自己的 worktree" in tools_fs.make_directory.func(str(outside))
    assert outside.exists()


# ══════════════════════════════════════════════════════════════
# 台账 / 展示
# ══════════════════════════════════════════════════════════════

def test_ledger_records_both_sides_of_a_move_and_only_the_copy_destination():
    led = state.new_task_ledger()
    state.record_tool_in_ledger(led, "move_path",
                                {"source": "a.py", "destination": "b.py"}, "成功移动: a → b")
    assert led["files"] == {"a.py": "已移动", "b.py": "已移动"}
    state.record_tool_in_ledger(led, "copy_path",
                                {"source": "b.py", "destination": "c.py"}, "成功复制: b → c")
    assert led["files"]["c.py"] == "已复制" and "b.py" in led["files"]
    assert led["files"]["b.py"] == "已移动", "复制不该改写源文件那条"
    state.record_tool_in_ledger(led, "delete_path", {"path": "c.py"}, "已拒绝：用户不允许")
    assert led["files"]["c.py"] == "已复制", "被拒绝的删除不进台账"


def test_tool_call_summary_shows_both_paths():
    from src.streaming import describe_tool_call
    name, detail = describe_tool_call("move_path", {"source": "a.py", "destination": "b.py"})
    assert "a.py → b.py" in detail and "移动" in name
    _, detail = describe_tool_call("delete_path", {"path": "gone.py"})
    assert detail == "gone.py"