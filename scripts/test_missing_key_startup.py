"""key 没填不能连窗口都打不开（启动容错）。

现场：用户把 DeepSeek 的 key 清空后，启动时 `import src.agent` 直接抛
`openai.OpenAIError: Missing credentials`——因为模块末尾就调 `_activate_llm()` 建实例。
正确的行为是：启动照常完成（窗口能开、能进设置），等真正发消息时再像别的配置问题
一样提示"去设置里填 key"。

这组测试分两层：
- 子进程真启动一次（import 缓存不能复用，必须干净进程），缺 key 也要 0 退出码；
- 单元层面：_activate_llm 吞掉构造失败、resolve_bound_llm 转成可读的中文配置错误。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import agent, models, session, state
from src.llm_errors import AUTH, ModelConfigError, classify, describe, retry_plan

REPO_ROOT = Path(__file__).resolve().parents[1]

# 子进程脚本：在 import src.agent **之前**改 config 常量，模拟用户清空 key 后重启。
_BOOT_SCRIPT = '''
import src.paths as paths
paths.set_data_dir({data_dir!r})          # 隔离 chat_memory（别读到真实的 last_model.json）

import src.config as cfg
cfg.DEEPSEEK_API_KEY = {key!r}
cfg.RESPONSES_API_KEY = {key!r}
cfg.DEEPSEEK_MODELS = ["deepseek-flash"]  # 固定成一个 OpenAI 协议的模型
cfg.RESPONSES_MODELS = []
cfg.DEFAULT_MODEL_ID = "deepseek-flash"

import src.agent  # noqa: F401  关键：这一行以前会抛 Missing credentials
import src.state as state
from src.models import MODEL_LIST

print("BOOTED", MODEL_LIST[state.current_model_index][1], state.llm is None)
'''


def _boot(tmp_path, key):
    """在干净子进程里按指定 key 启动一次，返回 (exit_code, stdout+stderr)。

    输出走文件重定向而不是 capture_output：后者在 Windows 上要 DuplicateHandle
    那对管道句柄，实测直接 WinError 50（不支持该请求），与被测逻辑无关。
    """
    script = Path(tmp_path) / "boot.py"
    script.write_text(
        _BOOT_SCRIPT.format(data_dir=str(Path(tmp_path) / "data"), key=key),
        encoding="utf-8")
    out_path = Path(tmp_path) / "boot.out"
    env = {
        **os.environ,
        "LINGXI_LOG_DIR": str(Path(tmp_path) / "logs"),   # 别把测试记录写进用户真实日志
        # 脚本放在 tmp_path 下，sys.path[0] 就是那里；仓库根得自己加进去才能 import src
        "PYTHONPATH": os.pathsep.join(
            [str(REPO_ROOT)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])),
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        code = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(REPO_ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,       # 同理：不继承 pytest 换过的 stdin 句柄
            timeout=300).returncode
    return code, out_path.read_text(encoding="utf-8", errors="replace")


class TestBootWithMissingKey:
    def test_boots_with_empty_deepseek_key(self, tmp_path):
        """key 为空 → 照常启动，llm 置空但不抛（窗口能开、能进设置）。"""
        code, out = _boot(tmp_path, "")
        assert code == 0, f"缺 key 时启动失败：\n{out}"
        assert "BOOTED deepseek True" in out, f"期望启动完成且 llm 置空，实际：\n{out}"

    def test_boots_with_key_present(self, tmp_path):
        """有 key → 正常建出实例，不能被这次的容错逻辑弄坏。"""
        code, out = _boot(tmp_path, "sk-live-not-a-placeholder")
        assert code == 0, f"有 key 时启动失败：\n{out}"
        assert "BOOTED deepseek False" in out, f"期望正常建出 llm，实际：\n{out}"


class _Stub:
    def bind_tools(self, tools):
        return "BOUND-STUB"


class TestActivateLlmToleratesConfigError:
    @pytest.fixture(autouse=True)
    def _restore_state(self, monkeypatch):
        """_activate_llm 会改全局 state.llm：记下原值，测完还原，别串到后面的测试。"""
        monkeypatch.setattr(state, "llm", state.llm)
        monkeypatch.setattr(state, "llm_with_tools", state.llm_with_tools)

    def test_activate_does_not_raise(self, monkeypatch):
        """_activate_llm 建不出实例时只记日志、把 llm 置空，绝不把异常抛给启动流程。"""
        def _boom(*args, **kwargs):
            raise RuntimeError("Missing credentials")

        monkeypatch.setattr(agent, "_create_llm", _boom)
        monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})

        agent._activate_llm()          # 不应抛

        assert state.llm is None
        assert state.llm_with_tools is None

    def test_activate_recovers_after_key_filled(self, monkeypatch):
        """失败不进缓存：key 填好后（切一次模型）能立刻建出来。"""
        fail = {"v": True}

        def _create(*args, **kwargs):
            if fail["v"]:
                raise RuntimeError("Missing credentials")
            return _Stub()

        monkeypatch.setattr(agent, "_create_llm", _create)
        monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})

        agent._activate_llm()
        assert state.llm is None

        fail["v"] = False             # 用户填好 key 后重新切一次模型
        agent._activate_llm()
        assert isinstance(state.llm, _Stub)
        assert state.llm_with_tools == "BOUND-STUB"


class TestSendTimeConfigMessage:
    """真正发消息时才报配置问题，而且说的是"去设置里填哪个 key"。"""

    def _deepseek_index(self):
        idx = next((i for i, m in enumerate(models.MODEL_LIST) if m[1] == "deepseek"), -1)
        assert idx >= 0, "config 里需要有 deepseek 类型的模型"
        return idx

    def test_resolve_bound_llm_raises_readable_error(self, monkeypatch):
        sess = session.Session()
        sess.current_model_index = self._deepseek_index()
        monkeypatch.setattr(models, "DEEPSEEK_API_KEY", "")
        monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})

        def _boom(*args, **kwargs):
            raise RuntimeError(
                "Missing credentials. Please pass an `api_key` or set OPENAI_API_KEY")

        monkeypatch.setattr(agent, "_create_llm", _boom)

        with pytest.raises(ModelConfigError) as err:
            agent.resolve_bound_llm(sess)

        msg = str(err.value)
        assert "设置" in msg, f"提示应指向设置页，实际：{msg}"
        assert "deepseek_api_key" in msg, f"应说清缺哪个 key，实际：{msg}"
        assert "Missing credentials" not in msg, "英文原文不该甩给用户"

    def test_config_error_is_terminal_and_shown_verbatim(self):
        """配置错误不重试、原样展示——重试再多次也还是同一处构造失败。"""
        exc = ModelConfigError("deepseek-flash 需要先在设置里填 deepseek_api_key。")

        assert classify(exc) == AUTH
        assert describe(exc) == "deepseek-flash 需要先在设置里填 deepseek_api_key。"
        should_retry, delay, reason = retry_plan(exc, 0, 3)
        assert should_retry is False and delay == 0.0
        assert reason == "deepseek-flash 需要先在设置里填 deepseek_api_key。"

    def test_loop_error_shows_config_message_as_is(self):
        """界面上就是那一句：不加"英文原文记在日志里"（请求没发出去，没有英文原文），不出现"。。"。"""
        from src.subagent import HeadlessUI
        ui = HeadlessUI()
        result = agent._report_loop_error(
            ui, ModelConfigError("deepseek-flash 需要先在设置里填 deepseek_api_key。"))
        assert result.status == "failed"
        assert [text for tag, text in ui.buffer if tag == "retry"] == [
            "deepseek-flash 需要先在设置里填 deepseek_api_key。"]

    def test_custom_model_issue_names_it_once(self, monkeypatch):
        """自定义模型的提示只说一次模型名，并指到「设置 → 自定义模型」。"""
        monkeypatch.setattr(models, "MODEL_LIST", [("⚙ my-model", "custom", "my-id", False)])
        monkeypatch.setattr(models, "_lookup_custom_model",
                            lambda model_id: {"api_key": "", "protocol": "openai"})
        assert models.get_model_config_issues(0) == ["⚙ my-model 需要先在设置 → 自定义模型里填 api_key。"]

    def test_unknown_build_failure_still_propagates(self, monkeypatch):
        """不是配置问题（没有 issues）的构造失败要原样抛出，不许被包装成"去填 key"。"""
        sess = session.Session()
        sess.current_model_index = self._deepseek_index()
        monkeypatch.setattr(models, "DEEPSEEK_API_KEY", "sk-real-key-value")
        monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})

        def _boom(*args, **kwargs):
            raise RuntimeError("some other SDK problem")

        monkeypatch.setattr(agent, "_create_llm", _boom)

        with pytest.raises(RuntimeError) as err:
            agent.resolve_bound_llm(sess)
        assert not isinstance(err.value, ModelConfigError)
        assert "some other SDK problem" in str(err.value)

    def test_key_present_builds_normally(self, monkeypatch):
        """key 齐全时不该被新加的检查误伤（不回归）。"""
        sess = session.Session()
        sess.current_model_index = self._deepseek_index()
        monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
        monkeypatch.setattr(agent, "_create_llm", lambda *a, **k: _Stub())

        llm, bound = agent.resolve_bound_llm(sess)
        assert isinstance(llm, _Stub)
        assert bound == "BOUND-STUB"
