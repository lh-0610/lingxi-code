"""通知 + 遥控逻辑测试：notify 分级/节流 · notify_long · push_long 分段 ·
telegram callback 解析+白名单 · 遥控敏感文件黑名单。

全部 mock 掉网络（telegram_push.push / push_long / answer_callback），只测逻辑。
"""
import pytest

import src.notify as notify_mod
import src.telegram_push as tp
import src.telegram_poll as poll
import src.ui.confirm_bars as cb
from src.streaming import _hits_remote_blocklist


# ─────────────────────── notify 分级 / 节流 / 开关 ───────────────────────
@pytest.fixture()
def notify_env(monkeypatch):
    calls = []
    monkeypatch.setattr(tp, "push", lambda level, title, msg: calls.append((level, title, msg)) or True)
    monkeypatch.setattr(notify_mod, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(notify_mod, "NOTIFY_LEVELS", ["error", "action_needed", "done"])
    monkeypatch.setattr(notify_mod, "NOTIFY_THROTTLE_SECONDS", 10)
    notify_mod._last_sent.clear()
    return calls


class TestNotify:
    def test_basic_send(self, notify_env):
        assert notify_mod.notify("done", "T", "M", "evt") is True
        assert len(notify_env) == 1

    def test_disabled_blocks(self, notify_env, monkeypatch):
        monkeypatch.setattr(notify_mod, "NOTIFY_ENABLED", False)
        assert notify_mod.notify("done", "T", "M", "evt") is False
        assert notify_env == []

    def test_level_not_in_list_blocked(self, notify_env):
        # "info" 不在 NOTIFY_LEVELS → 不发
        assert notify_mod.notify("info", "T", "M", "evt") is False
        assert notify_env == []

    def test_throttle_same_event(self, notify_env):
        assert notify_mod.notify("done", "T", "M", "evt") is True
        # 10s 内同 event_type 再发 → 被节流
        assert notify_mod.notify("done", "T", "M2", "evt") is False
        assert len(notify_env) == 1

    def test_different_event_not_throttled(self, notify_env):
        notify_mod.notify("done", "T", "M", "evt_a")
        notify_mod.notify("done", "T", "M", "evt_b")
        assert len(notify_env) == 2


# ─────────────────────── notify_long（完整分段 + 同样判断） ───────────────────────
@pytest.fixture()
def notify_long_env(monkeypatch):
    calls = []
    monkeypatch.setattr(tp, "push_long", lambda level, title, msg: calls.append((level, title, msg)) or True)
    monkeypatch.setattr(notify_mod, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(notify_mod, "NOTIFY_LEVELS", ["error", "action_needed", "done"])
    monkeypatch.setattr(notify_mod, "NOTIFY_THROTTLE_SECONDS", 10)
    notify_mod._last_sent.clear()
    return calls


class TestNotifyLong:
    def test_sends_via_push_long(self, notify_long_env):
        assert notify_mod.notify_long("done", "回复", "很长的完整内容", "agent_done") is True
        assert notify_long_env == [("done", "回复", "很长的完整内容")]

    def test_respects_disabled(self, notify_long_env, monkeypatch):
        monkeypatch.setattr(notify_mod, "NOTIFY_ENABLED", False)
        assert notify_mod.notify_long("done", "回复", "x", "agent_done") is False
        assert notify_long_env == []

    def test_respects_throttle(self, notify_long_env):
        notify_mod.notify_long("done", "回复", "x", "agent_done")
        assert notify_mod.notify_long("done", "回复", "y", "agent_done") is False
        assert len(notify_long_env) == 1


# ─────────────────────── push_long 分段 ───────────────────────
class TestPushLongChunking:
    def test_short_single_chunk(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tp, "push", lambda l, t, m: calls.append((t, m)) or True)
        tp.push_long("done", "标题", "hello", chunk_size=3500)
        assert len(calls) == 1
        assert calls[0] == ("标题", "hello")

    def test_long_splits_and_rejoins(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tp, "push", lambda l, t, m: calls.append((t, m)) or True)
        msg = "x" * 8000
        tp.push_long("done", "标题", msg, chunk_size=3500)
        assert len(calls) == 3                          # 3500 + 3500 + 1000
        assert "".join(m for _, m in calls) == msg      # 拼回原文不丢
        assert calls[0][0] == "标题"
        assert "续" in calls[1][0] and "续" in calls[2][0]

    def test_empty_message_placeholder(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tp, "push", lambda l, t, m: calls.append((t, m)) or True)
        tp.push_long("done", "标题", "", chunk_size=3500)
        assert len(calls) == 1
        assert calls[0][1] == "(无内容)"


# ─────────────────────── telegram callback 解析 + 白名单 ───────────────────────
@pytest.fixture()
def callback_env(monkeypatch):
    answers = []
    resolves = []
    monkeypatch.setattr(tp, "answer_callback", lambda cid, text="": answers.append((cid, text)) or True)
    monkeypatch.setattr(cb, "_resolve_remote_confirm",
                        lambda cid, allow, remember=False: resolves.append((cid, allow, remember)) or True)
    monkeypatch.setattr(poll, "TELEGRAM_CHAT_ID", "12345")
    return answers, resolves


def _cbq(data, from_id=12345):
    return {"id": "q1", "from": {"id": from_id}, "data": data,
            "message": {"chat": {"id": 12345}}}


class TestCallbackQuery:
    def test_whitelist_rejects_other_user(self, callback_env):
        answers, resolves = callback_env
        poll._handle_callback_query(_cbq("c:5:a", from_id=999))
        assert resolves == []                           # 没人有权解析
        assert answers and "无权限" in answers[0][1]

    def test_allow(self, callback_env):
        answers, resolves = callback_env
        poll._handle_callback_query(_cbq("c:5:a"))
        assert resolves == [("5", True, False)]
        assert answers                                  # answerCallbackQuery 必须回

    def test_deny(self, callback_env):
        _, resolves = callback_env
        poll._handle_callback_query(_cbq("c:5:d"))
        assert resolves == [("5", False, False)]

    def test_remember(self, callback_env):
        _, resolves = callback_env
        poll._handle_callback_query(_cbq("c:5:r"))
        assert resolves == [("5", True, True)]

    def test_legacy_format(self, callback_env):
        # 兼容旧格式 "allow:CID"
        _, resolves = callback_env
        poll._handle_callback_query(_cbq("allow:7"))
        assert resolves == [("7", True, False)]

    def test_unknown_data(self, callback_env):
        answers, resolves = callback_env
        poll._handle_callback_query(_cbq("garbage_no_colon"))
        assert resolves == []
        assert answers and "未知操作" in answers[0][1]


# ─────────────────────── 遥控敏感文件黑名单 ───────────────────────
class TestRemoteBlocklist:
    @pytest.mark.parametrize("path", [
        "config.json",
        "src/config.json",                # basename 命中
        "config.example.json",
        ".env",
        "long_term_memory.json",
        "role_config.json",
        "secret.key",                     # 后缀
        "server.pem",
        "store.pfx",
        "CONFIG.JSON",                    # 大小写不敏感
    ])
    def test_blocked(self, path):
        assert _hits_remote_blocklist(path) is True, f"应拦截: {path}"

    @pytest.mark.parametrize("path", [
        "main.py",
        "README.md",
        "src/agent.py",
        "data.json",                      # 普通 json 不拦
        "",
    ])
    def test_allowed(self, path):
        assert _hits_remote_blocklist(path) is False, f"不应拦截: {path}"
