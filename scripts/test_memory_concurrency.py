"""Event-controlled saves: no stale final reply or duplicate first-save ID."""
import json
import threading

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src import memory, paths, session


class ObservedRLock:
    """Expose whether the competing saver actually encountered the held lock."""

    def __init__(self):
        self.lock = threading.RLock()
        self.attempted = threading.Event()
        self.blocked = None

    def __enter__(self):
        if threading.current_thread().name == "competing-save" and self.blocked is None:
            acquired = self.lock.acquire(blocking=False)
            self.blocked = not acquired
            self.attempted.set()
            if acquired:
                return self
        self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()


def _race_save(monkeypatch, isolated_memory, sess, install_pause, mutate):
    lock = ObservedRLock()
    monkeypatch.setattr(memory, "_LOCK", lock)
    paused = threading.Event()
    finished = threading.Event()
    errors = []
    main = threading.current_thread()

    def scheduling_point():
        if threading.current_thread() is not main or paused.is_set():
            return
        paused.set()
        assert lock.attempted.wait(5), "competing saver never attempted the lock"
        # Old code can finish the newer save here; fixed code blocks until this save exits.
        if not lock.blocked:
            assert finished.wait(5), "unlocked competing save did not complete"

    install_pause(scheduling_point)

    def worker():
        paths.set_data_dir(str(isolated_memory.parent))
        session.bind_thread(sess)
        try:
            assert paused.wait(5), "first saver never reached its scheduling point"
            mutate()
            memory.save_session()
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
            session.unbind_thread()
            paths.set_data_dir(None)

    thread = threading.Thread(target=worker, name="competing-save", daemon=True)
    thread.start()
    try:
        memory.save_session(session=sess)
    finally:
        paused.set()
        thread.join(6)
    assert not thread.is_alive(), "competing saver deadlocked"
    assert not errors, errors


def _new_session():
    sess = session.Session()
    sess.chat_history = [SystemMessage(content="test"), HumanMessage(content="question")]
    session.register(sess)
    return sess


@pytest.mark.parametrize("saved", [False, True])
def test_concurrent_save_preserves_final_reply(monkeypatch, isolated_memory, saved):
    sess = _new_session()
    if saved:
        memory.save_session(session=sess)
    original = memory._msg_to_dict

    def install_pause(pause):
        def serialize(msg):
            if msg is sess.chat_history[1]:
                pause()
            return original(msg)
        monkeypatch.setattr(memory, "_msg_to_dict", serialize)

    def append_final():
        sess.chat_history.append(AIMessage(content="LATEST FINAL ANSWER"))
        sess.current_session_title = "Final title"

    try:
        _race_save(monkeypatch, isolated_memory, sess, install_pause, append_final)
        data = json.loads((isolated_memory / f"{sess.current_session_id}.json").read_text())
        assert [m["content"] for m in data["messages"]] == [
            "test", "question", "LATEST FINAL ANSWER",
        ]
        assert data["title"] == "Final title"
        index = json.loads((isolated_memory / "index.json").read_text())
        assert len(index) == 1
        assert index[0]["title"] == "Final title"
        restored = session.Session()
        assert memory.load_session(sess.current_session_id, session=restored)
        assert [m.content for m in restored.chat_history] == [m.content for m in sess.chat_history]
    finally:
        session.drop(sess.key)


def test_concurrent_first_save_allocates_one_id(monkeypatch, isolated_memory):
    sess = _new_session()
    original = memory.datetime

    def install_pause(pause):
        class PausedDatetime:
            @staticmethod
            def now():
                pause()
                return original.now()
        monkeypatch.setattr(memory, "datetime", PausedDatetime)

    try:
        _race_save(monkeypatch, isolated_memory, sess, install_pause, lambda: None)
        index = json.loads((isolated_memory / "index.json").read_text())
        assert [row["id"] for row in index] == [sess.current_session_id]
        assert sorted(p.name for p in isolated_memory.glob("*.json")) == sorted([
            "index.json", f"{sess.current_session_id}.json",
        ])
        assert session.get(sess.current_session_id) is sess
        assert [key for key, value in session.sessions.items() if value is sess] == [sess.current_session_id]
    finally:
        session.drop(sess.key)
