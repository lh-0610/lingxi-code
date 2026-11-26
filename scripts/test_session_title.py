"""自动标题（memory.maybe_generate_session_title）：首句看不出在做什么时参考助手的回复，
标题模型回「无法确定」这类没内容的话不当标题用。

实测：首句只有网址 +「答一下」（簿记模试），标题模型只看首句，回了「无法确定」，侧栏就叫这个。
"""
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src import memory, models, session

FIRST = "https://inuboki.com/3qboki-moshi1/\n答一下"
REPLY = "这份是「いぬぼき」日商簿記3級 模擬試験 第1回，下面是完整答案。"


class _FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append(messages[-1].content)
        return AIMessage(content=self.answer)


@pytest.fixture()
def sess(isolated_memory):
    s = session.Session()
    s.chat_history = [
        SystemMessage(content="sys"),
        HumanMessage(content=FIRST),
        AIMessage(content="我先打开这个页面看看。",
                  tool_calls=[{"name": "browser_open", "args": {"url": "x"}, "id": "t1"}]),
        ToolMessage(content="已打开", tool_call_id="t1"),
        AIMessage(content=REPLY),
        HumanMessage(content="第二轮"),
        AIMessage(content="第二轮的回复不该进标题提示"),
    ]
    s.current_session_title = None
    session.register(s)
    session.bind_thread(s)
    memory.save_session(session=s)
    yield s
    session.unbind_thread()


def _title_on_disk(isolated_memory, sid):
    return json.loads((isolated_memory / f"{sid}.json").read_text(encoding="utf-8"))["title"]


def _run(monkeypatch, answer):
    # 测试配置的默认模型是 Claude Code（CLI，不调模型直接截断首句），换成一个 API 模型
    from src import state
    idx = next(i for i, m in enumerate(models.MODEL_LIST) if m[1] != "claude-code")
    monkeypatch.setattr(state, "current_model_index", idx)
    llm = _FakeLLM(answer)
    monkeypatch.setattr(models, "_create_llm", lambda **k: llm)
    memory.maybe_generate_session_title()
    return llm


def test_the_title_prompt_includes_the_first_reply(sess, monkeypatch, isolated_memory):
    llm = _run(monkeypatch, "簿记三级模试答案")
    prompt = llm.prompts[0]
    assert FIRST[:30] in prompt
    assert "日商簿記3級 模擬試験" in prompt, "首句只有网址时，要靠回复知道在做什么"
    assert "第二轮" not in prompt, "只看第一轮"
    assert sess.current_session_title == "簿记三级模试答案"
    assert _title_on_disk(isolated_memory, sess.current_session_id) == "簿记三级模试答案"


@pytest.mark.parametrize("answer", ["无法确定", "无法确定。", "「无法判断」", "不确定", "抱歉，我无法生成标题"])
def test_a_vague_answer_falls_back_to_the_first_sentence(sess, monkeypatch, isolated_memory, answer):
    _run(monkeypatch, answer)
    assert sess.current_session_title == memory._sanitize_title(FIRST)
    assert sess.current_session_title.startswith("https://inuboki")


@pytest.mark.parametrize("answer", ["不能启动排查", "无法连接数据库", "未知错误定位"])
def test_real_titles_that_start_like_a_refusal_are_kept(sess, monkeypatch, answer):
    _run(monkeypatch, answer)
    assert sess.current_session_title == answer
