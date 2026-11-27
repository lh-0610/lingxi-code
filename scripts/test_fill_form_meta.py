"""browser_fill_form 的外围：工具卡摘要、显示名、Plan 模式拦截、值的写法。
真浏览器里的填写见 scripts/test_browser_bridge.py 的 test_fill_form_*。"""
from src import streaming, tools, tools_browser as tb


def test_registered_with_a_display_name():
    assert tb.browser_fill_form in tools.ALL_TOOLS
    assert tools.TOOL_DISPLAY_NAMES["browser_fill_form"] == "🌐 浏览器填表"


def test_the_tool_card_shows_how_many_fields_not_the_whole_list():
    fields = [{"ref": f"e{i}", "value": "現金"} for i in range(130)]
    name, detail = streaming.describe_tool_call("browser_fill_form", {"fields": fields})
    assert detail == "130 个字段", detail
    _, d2 = streaming.describe_tool_call("browser_fill_form", {"fields": fields[:2], "frame": "f.0"})
    assert d2 == "2 个字段（frame f.0）", d2


def test_plan_mode_blocks_it_with_a_reason():
    assert "browser_fill_form" not in streaming.PLAN_MODE_READONLY_TOOLS
    assert "填表" in streaming._PLAN_REJECT_NOTES["browser_fill_form"]


def test_checkbox_values():
    for v in (True, "true", "1", "勾选", "checked", "ON"):
        assert tb._as_bool(v) is True, v
    for v in (False, "false", "0", "取消", "unchecked"):
        assert tb._as_bool(v) is False, v
    assert tb._as_bool("也许") is None


def test_fields_given_as_dicts_or_validated_objects():
    validated = tb.browser_fill_form.args_schema.model_validate(
        {"fields": [{"ref": "e1", "value": "仕入"}, {"ref": " e2 ", "value": True}]}).fields
    assert tb._field_pairs(validated) == [("e1", "仕入"), ("e2", True)]
    assert tb._field_pairs([{"ref": "e3", "value": 5}]) == [("e3", 5)]
