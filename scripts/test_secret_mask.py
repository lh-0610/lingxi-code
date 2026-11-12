"""工具结果里的密钥打码（src/secret_mask.py）。

背景：run_command 的子进程环境已经剔掉了名字像密钥的变量（tools._scrubbed_env），
但工具**结果**本身没处理——read_file 读 config.json、命令输出里 echo 出 key，原文都会进
chat_history（落进会话 JSON）、日志、界面，并随下一轮请求发给模型服务商。
所以打码要发生在工具结果的出口：streaming._execute_tool 拿到 result 之后、
tools._run_command 的 _reader 把流式输出推到界面之前。

覆盖：
- 完整 key / 开头 10 个字符 / 出现在 JSON 里 / 多个 key 都打码；
- 短值（< 12）和非密钥字段不误伤；
- run_command 打印 key、read_file 读含 key 的文件：工具结果里都看不到原文；
- 流式推给界面的那份也打了码；
- 设置保存后调用 refresh 能让新 key 立刻生效。

不打码 config.json 本身（打码只作用于工具结果，配置照旧可读）。
"""
import json

import pytest

from src import paths, secret_mask


KEY = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
KEY2 = "tp-ZZZZYYYYXXXXWWWWVVVVUUUU"
MCP_ENV_KEY = "ghp_1234567890abcdefghijklmn"


# ══════════════════════════════════════════════════════════════
# 夹具：每例一份独立 config.json + 干净的打码表
# ══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _fresh_mask(monkeypatch):
    """每例前后都清掉缓存，避免某个用例的 key 串到下一个用例（打码表是进程级缓存）。"""
    monkeypatch.setattr(secret_mask, "_loaded", False)
    monkeypatch.setattr(secret_mask, "_secrets", {})
    yield
    monkeypatch.setattr(secret_mask, "_loaded", False)
    monkeypatch.setattr(secret_mask, "_secrets", {})


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """写一份临时 config.json 并指给 secret_mask（CONFIG_PATH 在调用时才读）。"""
    data = {
        "qwen_api_key": KEY,
        "custom_models": [{"name": "自建", "model_id": "m", "api_key": KEY2,
                           "base_url": "https://api.example.com/v1"}],
        "mcp_servers": {"srv": {"command": "node", "env": {"GITHUB_TOKEN": MCP_ENV_KEY}}},
        "ollama_base_url": "http://127.0.0.1:11434",
        "qwen_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "run_command_env_keep": [],
        "agent_max_rounds": 100,
        "short_api_key": "abc",              # 太短，不该被当成密钥
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(paths, "CONFIG_PATH", str(p))
    secret_mask.refresh()
    return data


# ══════════════════════════════════════════════════════════════
# 基本打码
# ══════════════════════════════════════════════════════════════

def test_full_key_is_masked(cfg):
    out = secret_mask.mask(f"当前使用的 key 是 {KEY}")
    assert KEY not in out
    assert "‹已隐藏：qwen_api_key›" in out


def test_only_first_ten_chars_are_masked(cfg):
    """排查问题时常见的 print(repr(key)[:10]) —— 前缀本身就能定位到具体 key。"""
    out = secret_mask.mask(f"repr: {KEY[:10]}...")
    assert KEY[:10] not in out
    assert "‹已隐藏：qwen_api_key›" in out


def test_short_prefix_below_threshold_is_left_alone(cfg):
    """少于 8 个字符的前缀不足以定位 key，打码没有意义（也避免误伤普通文本）。"""
    assert KEY[:7] in secret_mask.mask(KEY[:7])


def test_key_inside_json_is_masked(cfg):
    text = json.dumps(cfg, ensure_ascii=False)
    out = secret_mask.mask(text)
    assert KEY not in out and KEY2 not in out and MCP_ENV_KEY not in out
    # 结构没被破坏：打码后仍是合法 JSON
    restored = json.loads(out)
    assert restored["qwen_api_key"] == "‹已隐藏：qwen_api_key›"
    assert restored["custom_models"][0]["api_key"] == "‹已隐藏：api_key›"
    assert restored["mcp_servers"]["srv"]["env"]["GITHUB_TOKEN"] == "‹已隐藏：GITHUB_TOKEN›"


def test_multiple_keys_are_all_masked(cfg):
    out = secret_mask.mask(f"{KEY} 和 {KEY2} 以及 {MCP_ENV_KEY}")
    assert KEY not in out and KEY2 not in out and MCP_ENV_KEY not in out
    assert out.count("‹已隐藏：") == 3


def test_env_like_variable_is_masked(tmp_path, monkeypatch):
    """环境变量里名字像密钥的值同样收集（判定复用 tools._SECRET_ENV_PARTS）。"""
    monkeypatch.setattr(paths, "CONFIG_PATH", str(tmp_path / "none.json"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-abcdefghijklmnopqrstuvwxyz")
    secret_mask.refresh()
    out = secret_mask.mask("env: sk-env-abcdefghijklmnopqrstuvwxyz")
    assert "sk-env-abcdefghijklmnopqrstuvwxyz" not in out
    assert "OPENAI_API_KEY" in out


def test_refresh_picks_up_newly_saved_config(tmp_path, monkeypatch):
    """设置里保存配置后调 refresh(config) → 新 key 立刻打码，不用重启。"""
    monkeypatch.setattr(paths, "CONFIG_PATH", str(tmp_path / "none.json"))
    secret_mask.refresh()
    new_key = "sk-brand-new-key-0123456789abcdef"
    assert new_key in secret_mask.mask(new_key)

    secret_mask.refresh({"bocha_api_key": new_key})
    assert new_key not in secret_mask.mask(new_key)


# ══════════════════════════════════════════════════════════════
# 不误伤
# ══════════════════════════════════════════════════════════════

def test_short_values_are_not_collected(cfg):
    assert secret_mask.mask("abc") == "abc"


def test_non_secret_fields_are_untouched(cfg):
    text = "http://127.0.0.1:11434 https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert secret_mask.mask(text) == text


def test_a_credentials_variable_that_holds_a_path_is_not_a_secret(tmp_path, monkeypatch):
    """GOOGLE_APPLICATION_CREDENTIALS 装的是路径：当成密钥的话，前缀打码会把所有 C:\\Users… 换掉。"""
    monkeypatch.setattr(paths, "CONFIG_PATH", str(tmp_path / "none.json"))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", r"C:\Users\me\keys\service-account.json")
    monkeypatch.setenv("SSH_KEY_FILE", "/home/me/.ssh/id_ed25519")
    secret_mask.refresh()
    text = r"读取 C:\Users\me\project\main.py 和 /home/me/.ssh/config"
    assert secret_mask.mask(text) == text


def test_bearer_header_masks_the_token_not_the_word(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CONFIG_PATH", str(tmp_path / "none.json"))
    token = "abcDEF1234567890ghijKLMN"
    secret_mask.refresh({"mcp_servers": {"web": {"headers": {"Authorization": f"Bearer {token}"}}}})
    out = secret_mask.mask(f"Authorization: Bearer {token}；Bearer 认证")
    assert token not in out and out.count("Bearer") == 2


def test_a_word_like_password_is_masked_only_when_it_appears_in_full(tmp_path, monkeypatch):
    """纯字母口令开头 8 个字符就是普通词：只认完整出现，不按前缀打码误伤正文。"""
    monkeypatch.setattr(paths, "CONFIG_PATH", str(tmp_path / "none.json"))
    secret_mask.refresh({"smtp_password": "correcthorsebatterystaple"})
    assert secret_mask.mask("correcthorse 是个短语") == "correcthorse 是个短语"
    assert "correcthorsebatterystaple" not in secret_mask.mask("pw=correcthorsebatterystaple")


def test_a_list_of_keys_is_collected(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CONFIG_PATH", str(tmp_path / "none.json"))
    secret_mask.refresh({"api_keys": ["sk-list-one-0123456789ab", "sk-list-two-0123456789ab"]})
    out = secret_mask.mask("sk-list-one-0123456789ab sk-list-two-0123456789ab")
    assert "sk-list" not in out and out.count("‹已隐藏：api_keys›") == 2


def test_ordinary_text_and_non_strings_are_untouched(cfg):
    assert secret_mask.mask("def hello():\n    return 'hi'\n") == "def hello():\n    return 'hi'\n"
    assert secret_mask.mask(None) is None
    assert secret_mask.mask(42) == 42


# ══════════════════════════════════════════════════════════════
# 接到工具出口：run_command / read_file / 流式界面
# ══════════════════════════════════════════════════════════════

class _FakeUI:
    """run_command / _execute_tool 需要的最小 UI：记录推送内容、命令确认默认放行。"""

    def __init__(self):
        self.messages = []

    def show_message(self, text, tag=""):
        self.messages.append((tag, str(text)))

    def confirm_command(self, command, stdin="", **kwargs):
        return True, ""

    def confirm_edit(self, full, diff_text, op="edit"):
        return True, None


def test_run_command_output_is_masked(project_dir, monkeypatch, cfg):
    """命令把 key echo 出来：工具结果里看不到原文，界面那份也看不到。"""
    from src import state, tools
    ui = _FakeUI()
    monkeypatch.setattr(state, "ui_ref", ui)
    out = tools.run_command.func(f"echo {KEY}", timeout=60)
    assert KEY not in out, out
    assert "‹已隐藏：qwen_api_key›" in out
    streamed = "\n".join(text for _tag, text in ui.messages)
    assert KEY not in streamed, streamed
    assert "‹已隐藏：qwen_api_key›" in streamed


def test_run_command_stdin_script_printing_key_is_masked(project_dir, monkeypatch, cfg):
    """python - 脚本读配置后 print(key)：和命令行 echo 一样要打码。"""
    from src import state, tools
    ui = _FakeUI()
    monkeypatch.setattr(state, "ui_ref", ui)
    script = f"print('{KEY}')\n"
    out = tools.run_command.func("python -", stdin=script, timeout=60)
    assert KEY not in out, out


def test_run_command_masks_every_occurrence_in_long_output(project_dir, monkeypatch, cfg):
    """输出里多次出现同一个 key（含只露前缀的那次）：整段输出都打码，不只第一处。

    流式推送是逐行切片的，密钥恰好横跨两块时逐行打码会漏——所以 _run_command 在拼接
    完整输出后还要整段再打一次；这条用例也覆盖"不经 _execute_tool 直接调用"的路径。"""
    from src import state, tools
    ui = _FakeUI()
    monkeypatch.setattr(state, "ui_ref", ui)
    script = (
        f"print('first={KEY}')\n"
        f"print('prefix={KEY[:10]}')\n"
        f"print('third={KEY2}')\n"
    )
    out = tools.run_command.func("python -", stdin=script, timeout=60)
    assert KEY not in out, out
    assert KEY2 not in out, out
    assert KEY[:10] not in out, out
    assert out.count("‹已隐藏：") >= 3, out


def test_read_file_result_is_masked(project_dir, monkeypatch, cfg):
    """read_file 读含 key 的文件（这里就是 config.json 本身）：进 chat_history 的已打码。"""
    from langchain_core.messages import ToolMessage
    from src import state, streaming

    target = project_dir / "settings.json"
    target.write_text(json.dumps({"qwen_api_key": KEY}, ensure_ascii=False), encoding="utf-8")

    ui = _FakeUI()
    state.chat_history = []
    streaming._execute_tool({"name": "read_file", "args": {"path": str(target)},
                             "id": "call-mask"}, ui)

    last = state.chat_history[-1]
    assert isinstance(last, ToolMessage)
    assert KEY not in last.content, last.content
    assert "‹已隐藏：qwen_api_key›" in last.content
    # 界面那份同样不留原文
    shown = "\n".join(text for _tag, text in ui.messages)
    assert KEY not in shown, shown


def test_config_json_stays_readable_after_masking(project_dir, cfg):
    """不禁止读 config.json：打码只替换值，其余配置照样看得到。"""
    from src import state, streaming

    ui = _FakeUI()
    state.chat_history = []
    streaming._execute_tool(
        {"name": "read_file", "args": {"path": str(paths.CONFIG_PATH)}, "id": "call-cfg"}, ui)
    content = state.chat_history[-1].content
    assert KEY not in content
    assert "ollama_base_url" in content and "http://127.0.0.1:11434" in content
