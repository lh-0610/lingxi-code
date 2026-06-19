"""图片协议归一化测试（src/images.py）：跨协议互转 / 多轮剥图 / 纯文本模型剥图 /
deepseek 清 reasoning。都依赖"当前模型类型"，用 monkeypatch 控制 images.MODEL_LIST。"""
import src.images as images
from src import state
from langchain_core.messages import HumanMessage, ToolMessage, AIMessage


def _set_model(monkeypatch, mtype):
    monkeypatch.setattr(images, "MODEL_LIST", [("T", mtype, "m", False)])
    monkeypatch.setattr(state, "current_model_index", 0)


class TestNormalizeImageBlocks:
    def test_image_url_to_anthropic(self, monkeypatch):
        _set_model(monkeypatch, "anthropic")
        h = [HumanMessage(content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,ABC"}}])]
        part = images._normalize_image_blocks_for_current_model(h)[0].content[0]
        assert part["type"] == "image"
        assert part["source"] == {"type": "base64", "media_type": "image/png", "data": "ABC"}

    def test_anthropic_image_to_openai(self, monkeypatch):
        _set_model(monkeypatch, "cloud")   # 非 anthropic
        h = [HumanMessage(content=[{"type": "image",
              "source": {"type": "base64", "media_type": "image/jpeg", "data": "XYZ"}}])]
        part = images._normalize_image_blocks_for_current_model(h)[0].content[0]
        assert part["type"] == "image_url"
        assert part["image_url"]["url"] == "data:image/jpeg;base64,XYZ"

    def test_mimo_treated_as_anthropic(self, monkeypatch):
        _set_model(monkeypatch, "mimo")
        h = [HumanMessage(content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,Q"}}])]
        assert images._normalize_image_blocks_for_current_model(h)[0].content[0]["type"] == "image"

    def test_plain_text_unchanged(self, monkeypatch):
        _set_model(monkeypatch, "anthropic")
        h = [HumanMessage(content="just text")]
        assert images._normalize_image_blocks_for_current_model(h)[0].content == "just text"


class TestStripFollowupRounds:
    def test_strips_image_when_toolmessage_present(self, monkeypatch):
        _set_model(monkeypatch, "anthropic")
        h = [
            HumanMessage(content=[
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "X"}},
                {"type": "text", "text": "hi"}]),
            AIMessage(content="", tool_calls=[]),
            ToolMessage(content="r", tool_call_id="1"),
        ]
        parts = images._strip_images_in_followup_rounds(h)[0].content
        assert not any(isinstance(p, dict) and p.get("type") in ("image", "image_url") for p in parts)
        assert any(isinstance(p, dict) and p.get("type") == "text" for p in parts)

    def test_no_toolmessage_unchanged(self, monkeypatch):
        _set_model(monkeypatch, "anthropic")
        h = [HumanMessage(content=[{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "X"}}])]
        assert images._strip_images_in_followup_rounds(h)[0].content[0]["type"] == "image"

    def test_non_anthropic_unchanged(self, monkeypatch):
        _set_model(monkeypatch, "cloud")
        h = [
            HumanMessage(content=[{"type": "image_url", "image_url": {"url": "x"}}]),
            ToolMessage(content="r", tool_call_id="1"),
        ]
        assert images._strip_images_in_followup_rounds(h)[0].content[0]["type"] == "image_url"


class TestStripTextOnlyModel:
    def test_strips_when_no_vision(self, monkeypatch):
        monkeypatch.setattr(images, "current_model_supports_vision", lambda: False)
        h = [HumanMessage(content=[{"type": "image_url", "image_url": {"url": "x"}}, {"type": "text", "text": "q"}])]
        out = images._strip_images_for_text_only_model(h)[0].content
        assert isinstance(out, str)
        assert "图片" in out and "q" in out

    def test_unchanged_when_vision(self, monkeypatch):
        monkeypatch.setattr(images, "current_model_supports_vision", lambda: True)
        h = [HumanMessage(content=[{"type": "image_url", "image_url": {"url": "x"}}])]
        assert images._strip_images_for_text_only_model(h)[0].content[0]["type"] == "image_url"


class TestStripReasoningDeepseek:
    def test_strips_for_deepseek(self, monkeypatch):
        _set_model(monkeypatch, "deepseek")
        msg = AIMessage(content="ans", additional_kwargs={"reasoning_content": "think"})
        assert "reasoning_content" not in images._strip_reasoning_for_deepseek([msg])[0].additional_kwargs

    def test_non_deepseek_unchanged(self, monkeypatch):
        _set_model(monkeypatch, "anthropic")
        msg = AIMessage(content="ans", additional_kwargs={"reasoning_content": "think"})
        assert images._strip_reasoning_for_deepseek([msg])[0].additional_kwargs.get("reasoning_content") == "think"
