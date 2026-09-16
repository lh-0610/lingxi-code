"""chat_memory/*.json 的原子写回归（B02a）。

改之前 memory.py 七处都是 `open(path, "w")`——打开即截断成 0 字节。序列化/写入中途
出错或进程在这一刻退出，盘上留下半截 JSON，下次 `json.load` 直接抛：整段会话历史或
整个 index.json 就此读不回来。

这些测试盯住的性质只有一条：**任何失败路径下，目标文件都保持完整的旧内容**。
外加两条容易在后续重构里被无意破坏的约束：落盘字节格式不变、临时文件不残留。
"""
import json
import os
import threading
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src import memory, paths, session


def _temps(directory):
    """目录里残留的临时文件（原子写用的 <name>.xxxx.tmp）。"""
    return sorted(p.name for p in directory.glob("*.tmp"))


def _write_legacy(path, data):
    """改动前的写法，用作字节级对照基准。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


SAMPLE = {"id": "s1", "title": "中文标题", "messages": [{"type": "HumanMessage", "content": "你好\n世界"}]}


# ══════════════════════════════════════════════════════════════
# 辅助函数本身
# ══════════════════════════════════════════════════════════════

def test_creates_new_file(tmp_path):
    target = tmp_path / "new.json"
    memory._atomic_write_json(str(target), SAMPLE)
    assert json.loads(target.read_text(encoding="utf-8")) == SAMPLE
    assert _temps(tmp_path) == []


def test_replaces_existing_file(tmp_path):
    target = tmp_path / "x.json"
    _write_legacy(target, {"old": True})
    memory._atomic_write_json(str(target), SAMPLE)
    assert json.loads(target.read_text(encoding="utf-8")) == SAMPLE
    assert _temps(tmp_path) == []


def test_byte_format_matches_legacy_writer(tmp_path):
    """落盘字节必须与改动前逐字一致。

    换行翻译（Windows 上 open(...,"w") 会把 \\n 写成 \\r\\n）和 ensure_ascii 一旦漂移，
    整个 chat_memory 目录的既有文件与新写入的会混用两种格式——不报错，但 diff 全是噪声，
    也会让任何按字节比对的工具失效。
    """
    legacy = tmp_path / "legacy.json"
    atomic = tmp_path / "atomic.json"
    _write_legacy(legacy, SAMPLE)
    memory._atomic_write_json(str(atomic), SAMPLE)
    assert atomic.read_bytes() == legacy.read_bytes()


def test_empty_index_format_matches_legacy(tmp_path):
    """_ensure_memory_dir 那处用的是默认格式选项（ensure_ascii=True、无缩进），不能被统一掉。"""
    legacy = tmp_path / "legacy.json"
    atomic = tmp_path / "atomic.json"
    with open(legacy, "w", encoding="utf-8") as f:
        json.dump([], f)
    memory._atomic_write_json(str(atomic), [], ensure_ascii=True, indent=None)
    assert atomic.read_bytes() == legacy.read_bytes()


def test_temp_file_created_in_target_directory(tmp_path, monkeypatch):
    """临时文件必须与目标同目录，否则 os.replace 跨文件系统不再原子。"""
    seen = {}
    real_mkstemp = memory.tempfile.mkstemp

    def spy(*args, **kwargs):
        seen["dir"] = kwargs.get("dir")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(memory.tempfile, "mkstemp", spy)
    sub = tmp_path / "nested"
    sub.mkdir()
    memory._atomic_write_json(str(sub / "a.json"), SAMPLE)
    assert seen["dir"] == str(sub)


def test_concurrent_writers_never_truncate(tmp_path):
    """临时名必须唯一：固定的 "<name>.tmp" 会让并发写同一目标的线程互相截断。

    辅助函数不该依赖"调用方一定持锁"这个隐含前提。
    """
    target = tmp_path / "shared.json"
    _write_legacy(target, {"seed": True})
    errors = []

    def worker(n):
        try:
            for _ in range(20):
                memory._atomic_write_json(str(target), {"writer": n, "payload": "x" * 500})
        except BaseException as exc:      # noqa: BLE001 - 线程内异常要带回主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors, errors
    assert json.loads(target.read_text(encoding="utf-8"))["payload"] == "x" * 500
    assert _temps(tmp_path) == []


def test_replace_retries_past_transient_reader(tmp_path):
    """Windows 上别人打开着目标文件，os.replace 会 PermissionError。

    真实来源：用户拿编辑器开着 index.json、杀毒软件扫描、第二个灵犀实例。这类占用是
    短暂的，一次失败就放弃等于"用户这一轮白说了"，所以替换阶段有界重试。
    这里让读者持有句柄 40ms（小于 ~80ms 重试预算），写入必须最终成功。
    """
    target = tmp_path / "x.json"
    _write_legacy(target, {"old": True})
    holding = threading.Event()

    def reader():
        with open(target, "r", encoding="utf-8"):
            holding.set()
            time.sleep(0.04)

    t = threading.Thread(target=reader)
    t.start()
    assert holding.wait(5)
    memory._atomic_write_json(str(target), SAMPLE)      # 不该抛
    t.join(5)
    assert json.loads(target.read_text(encoding="utf-8")) == SAMPLE
    assert _temps(tmp_path) == []


def test_permanent_permission_error_still_raises(tmp_path, monkeypatch):
    """重试是为短暂占用兜底，不能把持续性失败也吞掉——用尽后必须如实抛出。"""
    target = tmp_path / "x.json"
    _write_legacy(target, {"old": True})
    calls = []

    def always_denied(_src, _dst):
        calls.append(1)
        raise PermissionError(13, "permanently locked")

    monkeypatch.setattr(memory.os, "replace", always_denied)
    with pytest.raises(PermissionError, match="permanently locked"):
        memory._atomic_write_json(str(target), SAMPLE)
    assert len(calls) == memory._REPLACE_RETRIES     # 确实重试过，且有上界
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert _temps(tmp_path) == []


# ══════════════════════════════════════════════════════════════
# 失败路径：旧内容必须完好
# ══════════════════════════════════════════════════════════════

class _FailingWriter:
    """文件对象替身：write 必炸，用来模拟写到一半失败（磁盘满 / IO 错）。"""

    def __init__(self, f):
        self._f = f

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()
        return False

    def write(self, _s):
        raise OSError("simulated disk full")


def test_serialization_failure_keeps_old_file(tmp_path):
    """不可序列化的对象在写盘前就该抛，磁盘上连临时文件都不该出现。"""
    target = tmp_path / "x.json"
    _write_legacy(target, {"old": True})
    with pytest.raises(TypeError):
        memory._atomic_write_json(str(target), {"bad": object()})
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert _temps(tmp_path) == []


def test_write_failure_keeps_old_file(tmp_path, monkeypatch):
    target = tmp_path / "x.json"
    _write_legacy(target, {"old": True})
    real_fdopen = os.fdopen
    monkeypatch.setattr(memory.os, "fdopen",
                        lambda fd, *a, **kw: _FailingWriter(real_fdopen(fd, *a, **kw)))
    with pytest.raises(OSError, match="simulated disk full"):
        memory._atomic_write_json(str(target), SAMPLE)
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert _temps(tmp_path) == []


def test_fsync_failure_keeps_old_file(tmp_path, monkeypatch):
    """写完但没能落稳也算失败：不能把可能没写全的内容 replace 上去。"""
    target = tmp_path / "x.json"
    _write_legacy(target, {"old": True})

    def boom(_fd):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(memory.os, "fsync", boom)
    with pytest.raises(OSError, match="simulated fsync failure"):
        memory._atomic_write_json(str(target), SAMPLE)
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert _temps(tmp_path) == []


def test_replace_failure_keeps_old_file(tmp_path, monkeypatch):
    target = tmp_path / "x.json"
    _write_legacy(target, {"old": True})

    def boom(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(memory.os, "replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        memory._atomic_write_json(str(target), SAMPLE)
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert _temps(tmp_path) == []


def _spy_mkstemp(monkeypatch, sink):
    """记录 mkstemp 发出的 fd 与临时路径，供测试在 finally 里做真实清理。"""
    real = memory.tempfile.mkstemp

    def spy(*args, **kwargs):
        fd, tmp = real(*args, **kwargs)
        sink["fd"], sink["tmp"] = fd, tmp
        return fd, tmp

    monkeypatch.setattr(memory.tempfile, "mkstemp", spy)


def test_normal_path_closes_original_fd_exactly_once(tmp_path, monkeypatch):
    """正常路径：mkstemp 给的 fd 必须由我们恰好关闭一次。

    closefd=False 意味着文件对象不会替我们关——漏关就是每保存一次泄漏一个句柄；
    关两次则可能命中别的线程刚拿到的同号描述符，把无关文件关掉。两边都不能错。
    """
    target = tmp_path / "x.json"
    created = {}
    _spy_mkstemp(monkeypatch, created)
    closed = []
    real_close = os.close

    def counting_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(memory.os, "close", counting_close)
    memory._atomic_write_json(str(target), SAMPLE)

    assert closed == [created["fd"]], f"期望恰好关闭一次原始 fd，实际 {closed}"
    assert json.loads(target.read_text(encoding="utf-8")) == SAMPLE
    assert _temps(tmp_path) == []


def test_fdopen_early_failure_closes_fd_exactly_once(tmp_path, monkeypatch):
    """构造**早期**失败：io.open 还没接管 fd 就抛（如模式/编码非法）。

    此时 fd 仍归我们，必须自己关一次。漏关的泄漏不报错，只会在很久以后以"莫名其妙
    打不开文件"的形式出现。
    """
    target = tmp_path / "x.json"
    _write_legacy(target, {"old": True})
    created = {}
    _spy_mkstemp(monkeypatch, created)
    closed = []
    real_close = os.close
    monkeypatch.setattr(memory.os, "close",
                        lambda fd: (closed.append(fd), real_close(fd))[1])
    # 早期失败的忠实模型：不碰 fd，直接抛（closefd=False 下 io.open 也不会关它）
    monkeypatch.setattr(memory.os, "fdopen",
                        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("early construction failure")))

    with pytest.raises(OSError, match="early construction failure"):
        memory._atomic_write_json(str(target), SAMPLE)

    assert closed == [created["fd"]], f"期望恰好关闭一次原始 fd，实际 {closed}"
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert _temps(tmp_path) == []


def test_fdopen_late_failure_never_closes_unrelated_fd(tmp_path, monkeypatch):
    """构造**后期**失败：io.open 已接管 fd，构造文本层时才失败。

    这是 closefd=True 的陷阱：io.open 会把 FileIO 连同 fd 一起关掉再抛，调用方只看到
    "fdopen 抛了异常"，无从判断 fd 还在不在；若按"没接管"再关一次，而这个 fd 号已被别的
    线程复用，关掉的就是一个无关文件。

    这里用 _pyio（纯 Python 实现，构造过程可注入）真实走一遍，并在异常缝隙里抢占 fd 号
    模拟竞态。只要实现坚持传 closefd=False，io.open 就不会关我们的 fd，哨兵句柄安然无恙；
    一旦有人改回 closefd=True，哨兵会被误关，这个用例立刻变红。
    """
    import _pyio

    target = tmp_path / "x.json"
    _write_legacy(target, {"old": True})
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"do-not-touch")
    seen = {}

    class _BoomTextWrapper:
        def __init__(self, *_a, **_kw):
            raise RuntimeError("late construction failure")

    monkeypatch.setattr(_pyio, "TextIOWrapper", _BoomTextWrapper)

    def fake_fdopen(fd, mode="r", buffering=-1, encoding=None, **kwargs):
        closefd = kwargs.get("closefd", True)
        seen["fd"] = fd
        seen["closefd"] = closefd
        try:
            return _pyio.open(fd, mode, buffering, encoding, closefd=closefd)
        except BaseException:
            # io.open 若真把 fd 关了，这一瞬间别的线程就会拿到同一个号——这里用哨兵占位
            seen["sentinel_fd"] = os.open(str(sentinel), os.O_RDONLY)
            raise

    monkeypatch.setattr(memory.os, "fdopen", fake_fdopen)
    try:
        with pytest.raises(RuntimeError, match="late construction failure"):
            memory._atomic_write_json(str(target), SAMPLE)

        assert seen["closefd"] is False, "必须以 closefd=False 打开，否则 fd 所有权含糊"
        assert seen["sentinel_fd"] != seen["fd"], \
            "fd 被 io.open 内部关闭并被哨兵复用——closefd 传错了"
        os.fstat(seen["sentinel_fd"])          # 仍然有效 = 无关文件没被误关
        assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
        assert _temps(tmp_path) == []
    finally:
        if "sentinel_fd" in seen:
            try:
                os.close(seen["sentinel_fd"])
            except OSError:
                pass


def test_close_failure_does_not_close_the_same_fd_twice(tmp_path, monkeypatch):
    """PEP 475：os.close 报错时 fd 可能已被释放，因此不能盲目重复关闭同一编号。

    也就是说"关闭报错"**推不出**"fd 还开着"。若异常清理据此再关一次，而这个号在那一瞬
    已被别的线程复用，关掉的就是一个无关文件——症状是别处莫名其妙读写失败，极难定位。
    修法是在调用 close **之前**就把关闭责任交出去，而不是在它返回之后。
    """
    target = tmp_path / "x.json"
    _write_legacy(target, {"old": True})
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"do-not-touch")
    created = {}
    _spy_mkstemp(monkeypatch, created)
    closed = []
    seen = {}
    real_close = os.close

    def close_then_fail(fd):
        closed.append(fd)
        if len(closed) == 1:
            real_close(fd)                                  # fd 已真实释放
            seen["sentinel_fd"] = os.open(str(sentinel), os.O_RDONLY)   # 号立刻被复用
            raise OSError("close reported failure after releasing fd")
        real_close(fd)                                      # 第二次 = 误关哨兵

    monkeypatch.setattr(memory.os, "close", close_then_fail)
    try:
        with pytest.raises(OSError, match="close reported failure"):
            memory._atomic_write_json(str(target), SAMPLE)

        assert closed == [created["fd"]], f"同一 fd 号被关了不止一次：{closed}"
        assert seen["sentinel_fd"] == created["fd"], "未复现 fd 号复用，测试前提不成立"
        os.fstat(seen["sentinel_fd"])          # 哨兵仍有效 = 无关文件没被误关
        assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
        assert _temps(tmp_path) == []          # 关闭失败仍要尽力删掉临时文件
    finally:
        if "sentinel_fd" in seen:
            try:
                real_close(seen["sentinel_fd"])
            except OSError:
                pass


def test_fdopen_failure_cleanup_errors_do_not_mask_original(tmp_path, monkeypatch):
    """fdopen 失败路径上，关 fd 和删临时文件再怎么失败，抛出去的仍是原始异常。

    注意本例刻意让 os.close / os.unlink 都失败，于是**真实的** fd 和临时文件都会留下来。
    monkeypatch 只恢复替身、不会替我们收尾，所以这里自己抓住 mkstemp 发出的 fd 与路径，
    在 finally 里用**打补丁前抓到的真函数**清理——断言失败也能收尾，不把句柄泄漏留给
    后续测试（泄漏的 fd 会被后面的 open 复用，制造难查的串扰）。
    """
    target = tmp_path / "x.json"
    _write_legacy(target, {"old": True})
    created = {}
    _spy_mkstemp(monkeypatch, created)
    real_close, real_unlink = os.close, os.unlink

    def boom_fdopen(_fd, *_a, **_kw):
        raise RuntimeError("ORIGINAL FDOPEN FAILURE")

    monkeypatch.setattr(memory.os, "fdopen", boom_fdopen)
    monkeypatch.setattr(memory.os, "close",
                        lambda _fd: (_ for _ in ()).throw(OSError("close also failed")))
    monkeypatch.setattr(memory.os, "unlink",
                        lambda _p: (_ for _ in ()).throw(OSError("unlink also failed")))
    try:
        with pytest.raises(RuntimeError, match="ORIGINAL FDOPEN FAILURE"):
            memory._atomic_write_json(str(target), SAMPLE)
        assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    finally:
        if "fd" in created:
            try:
                real_close(created["fd"])
            except OSError:
                pass
        if created.get("tmp"):
            try:
                real_unlink(created["tmp"])
            except OSError:
                pass


def test_cleanup_failure_does_not_mask_original_error(tmp_path, monkeypatch):
    """清理临时文件失败时，抛出去的必须仍是"为什么没存上"的原因。

    反过来（让 unlink 的错覆盖原错）会把排障线索彻底换掉：用户看到"删不掉临时文件"，
    真正的磁盘满 / 权限问题却不见了。
    """
    target = tmp_path / "x.json"
    _write_legacy(target, {"old": True})

    def boom_replace(_src, _dst):
        raise RuntimeError("ORIGINAL FAILURE")

    def boom_unlink(_p):
        raise OSError("cleanup also failed")

    monkeypatch.setattr(memory.os, "replace", boom_replace)
    monkeypatch.setattr(memory.os, "unlink", boom_unlink)
    with pytest.raises(RuntimeError, match="ORIGINAL FAILURE"):
        memory._atomic_write_json(str(target), SAMPLE)
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}


# ══════════════════════════════════════════════════════════════
# 七个入口的集成验证
# ══════════════════════════════════════════════════════════════

def _new_session(title=None):
    sess = session.Session()
    sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="问题"),
                         AIMessage(content="回答")]
    sess.current_session_title = title
    session.register(sess)
    return sess


def test_all_entry_points_leave_no_temp_files(isolated_memory):
    """save / 标题更新 / 索引 / 迁移 / 删除 跑一遍，目录里不许留临时文件。"""
    sess = _new_session()
    memory.save_session(session=sess)
    sid = sess.current_session_id
    memory._write_session_title(sid, "新标题")
    memory._update_index(sid, "新标题", None)
    memory.move_sessions_to_no_project("D:/nonexistent")
    memory.delete_session(sid)
    assert _temps(isolated_memory) == []


def test_save_session_replace_failure_keeps_previous_session_file(isolated_memory, monkeypatch):
    """第二次保存在 replace 阶段失败 → 盘上仍是第一次那份完整会话，不是半截。"""
    sess = _new_session()
    memory.save_session(session=sess)
    sid = sess.current_session_id
    session_file = isolated_memory / f"{sid}.json"
    before = session_file.read_bytes()

    sess.chat_history.append(AIMessage(content="第二轮回答"))
    real_replace = os.replace           # 先抓真函数：boom 里再调 os.replace 会命中自己

    def boom(src, dst):
        if dst.endswith(f"{sid}.json"):
            raise OSError("simulated replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(memory.os, "replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        memory.save_session(session=sess)

    assert session_file.read_bytes() == before
    assert json.loads(session_file.read_text(encoding="utf-8"))["id"] == sid
    assert _temps(isolated_memory) == []


def test_index_write_failure_keeps_previous_index(isolated_memory, monkeypatch):
    sess = _new_session()
    memory.save_session(session=sess)
    index_file = isolated_memory / "index.json"
    before = index_file.read_bytes()

    real_replace = os.replace

    def boom(src, dst):
        if dst.endswith("index.json"):
            raise OSError("simulated index failure")
        return real_replace(src, dst)

    monkeypatch.setattr(memory.os, "replace", boom)
    other = _new_session()
    with pytest.raises(OSError, match="simulated index failure"):
        memory.save_session(session=other)

    assert index_file.read_bytes() == before
    assert json.loads(index_file.read_text(encoding="utf-8"))          # 仍是合法 JSON
    assert _temps(isolated_memory) == []


def test_migration_reports_failed_ids_not_silent(isolated_memory, monkeypatch, tmp_path):
    """调用方异常语义不变：单个会话文件写失败仍走 SessionMigrationError，不是整批抛出。"""
    proj = str(tmp_path / "proj")
    from src import state
    monkeypatch.setattr(state, "current_project", proj)
    sess = _new_session()
    sess.project = proj
    memory.save_session(session=sess)
    sid = sess.current_session_id

    real_replace = os.replace

    def boom(src, dst):
        if dst.endswith(f"{sid}.json"):
            raise OSError("simulated per-session failure")
        return real_replace(src, dst)

    monkeypatch.setattr(memory.os, "replace", boom)
    with pytest.raises(memory.SessionMigrationError) as excinfo:
        memory.move_sessions_to_no_project(proj)
    assert excinfo.value.failed_ids == [sid]
    # index.json 先于会话文件写，迁移在索引层已生效
    idx = json.loads((isolated_memory / "index.json").read_text(encoding="utf-8"))
    assert [i for i in idx if i["id"] == sid][0]["project"] is None
    assert _temps(isolated_memory) == []


def test_concurrent_session_saves_stay_readable(isolated_memory):
    """多会话并发保存：每个文件任何时刻都可解析，且不留临时文件。"""
    sessions = [_new_session(title=f"会话{i}") for i in range(4)]
    errors = []
    root = str(isolated_memory.parent)

    def worker(sess):
        paths.set_data_dir(root)     # set_data_dir 是线程本地的，新线程必须自己设
        try:
            for i in range(10):
                sess.chat_history.append(AIMessage(content=f"回答{i}"))
                memory.save_session(session=sess)
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)
        finally:
            paths.set_data_dir(None)

    threads = [threading.Thread(target=worker, args=(s,)) for s in sessions]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors

    for sess in sessions:
        data = json.loads((isolated_memory / f"{sess.current_session_id}.json")
                          .read_text(encoding="utf-8"))
        assert data["id"] == sess.current_session_id
    json.loads((isolated_memory / "index.json").read_text(encoding="utf-8"))
    assert _temps(isolated_memory) == []
