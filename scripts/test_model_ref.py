"""模型标识解析：default_model_id / vision_model_id 可填显示名（显示名优先），其次才按 model_id。

现场：内置 deepseek-flash 与自定义「⚙ deepseek-v4.1-flash」共用同一个 model_id。
只按 model_id 匹配永远命中列表里靠前的内置那条，用户填多少都选不到自定义模型。
另外设置窗口的图片识别下拉如果把 model_id 当 itemData，同 id 的两条会回显串行、
保存后还会把配置改到另一个模型上，所以改成存显示名。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src import models, state

# 与真实列表同序：内置的 deepseek-flash 在自定义之前，两者 model_id 相同
ENTRIES = [
    ("Claude Code", "claude-code", "claude", False),
    ("mimo-v2.5-pro", "mimo", "mimo-v2.5-pro", False),
    ("deepseek-flash", "deepseek", "deepseek-flash", False),
    ("⚙ deepseek-v4.1-flash", "custom", "deepseek-flash", True),
    ("⚙ 看图模型", "custom", "my-vision", False),
]


@pytest.fixture()
def model_list(monkeypatch):
    monkeypatch.setattr(models, "MODEL_LIST", ENTRIES)
    return ENTRIES


class TestResolveModelRef:
    def test_display_name_wins_over_shared_model_id(self, model_list):
        assert models.resolve_model_ref("⚙ deepseek-v4.1-flash") == 3
        # 填 model_id 仍按列表顺序命中靠前的内置那条（向后兼容）
        assert models.resolve_model_ref("deepseek-flash") == 2

    def test_display_name_without_gear_prefix(self, model_list):
        """自定义模型带不带 ⚙ 前缀都能填。"""
        assert models.resolve_model_ref("deepseek-v4.1-flash") == 3

    def test_gear_prefixed_ref_matches_unprefixed_name(self, model_list):
        """反向也成立：给内置模型填带 ⚙ 的名字也能认出来。"""
        assert models.resolve_model_ref("⚙ mimo-v2.5-pro") == 1

    def test_exact_display_name_beats_model_id_even_if_later(self, model_list):
        """显示名完全一致的条目排在后面时，也要优先于靠前的同名 model_id。"""
        entries = [
            ("deepseek-flash", "deepseek", "deepseek-flash", False),
            ("deepseek-flash", "custom", "other-id", False),
        ]
        assert models.resolve_model_ref("deepseek-flash", model_list=entries) == 0
        entries = [
            ("ops", "custom", "deepseek-flash", False),
            ("deepseek-flash", "custom", "other-id", False),
        ]
        assert models.resolve_model_ref("deepseek-flash", model_list=entries) == 1

    def test_empty_or_unknown_returns_minus_one(self, model_list):
        assert models.resolve_model_ref("") == -1
        assert models.resolve_model_ref(None) == -1
        assert models.resolve_model_ref("   ") == -1
        assert models.resolve_model_ref("不存在的模型") == -1

    def test_exclude_types_skips_claude_code(self, model_list):
        assert models.resolve_model_ref("Claude Code") == 0
        assert models.resolve_model_ref(
            "Claude Code", exclude_types=("claude-code",)) == -1


class TestVisionModelRef:
    def test_picks_custom_entry_by_display_name(self, model_list, monkeypatch):
        from src import config
        monkeypatch.setattr(config, "VISION_MODEL_ID", "⚙ deepseek-v4.1-flash")
        assert models.get_vision_model_index() == 3

    def test_picks_custom_entry_without_gear(self, model_list, monkeypatch):
        from src import config
        monkeypatch.setattr(config, "VISION_MODEL_ID", "deepseek-v4.1-flash")
        assert models.get_vision_model_index() == 3

    def test_model_id_keeps_old_behaviour(self, model_list, monkeypatch):
        from src import config
        monkeypatch.setattr(config, "VISION_MODEL_ID", "deepseek-flash")
        assert models.get_vision_model_index() == 2

    def test_unset_or_removed_is_minus_one(self, model_list, monkeypatch):
        from src import config
        monkeypatch.setattr(config, "VISION_MODEL_ID", "")
        assert models.get_vision_model_index() == -1
        monkeypatch.setattr(config, "VISION_MODEL_ID", "已删除的模型")
        assert models.get_vision_model_index() == -1

    def test_claude_code_never_vision_model(self, model_list, monkeypatch):
        from src import config
        monkeypatch.setattr(config, "VISION_MODEL_ID", "Claude Code")
        assert models.get_vision_model_index() == -1

    def test_supports_vision_compares_entry_not_model_id(self, model_list, monkeypatch):
        """当前模型是内置 deepseek-flash、图片识别模型是自定义同 id 那个 → 不能算支持视觉。"""
        from src import config
        monkeypatch.setattr(config, "VISION_MODEL_ID", "⚙ deepseek-v4.1-flash")
        monkeypatch.setattr(state, "current_model_index", 3)
        assert models.current_model_supports_vision() is True
        monkeypatch.setattr(state, "current_model_index", 2)
        assert models.current_model_supports_vision() is False

    def test_supports_vision_false_when_unset(self, model_list, monkeypatch):
        from src import config
        monkeypatch.setattr(config, "VISION_MODEL_ID", "")
        monkeypatch.setattr(state, "current_model_index", 3)
        assert models.current_model_supports_vision() is False


class TestDefaultModelRef:
    def test_default_model_accepts_display_name(self, model_list, monkeypatch, isolated_memory):
        from src import agent, config
        monkeypatch.setattr(agent, "MODEL_LIST", model_list)
        monkeypatch.setattr(config, "DEFAULT_MODEL_ID", "⚙ deepseek-v4.1-flash")
        assert agent._resolve_default_model_index() == 3

    def test_default_model_without_gear(self, model_list, monkeypatch, isolated_memory):
        from src import agent, config
        monkeypatch.setattr(agent, "MODEL_LIST", model_list)
        monkeypatch.setattr(config, "DEFAULT_MODEL_ID", "deepseek-v4.1-flash")
        assert agent._resolve_default_model_index() == 3

    def test_unknown_default_falls_back_to_first(self, model_list, monkeypatch, isolated_memory):
        from src import agent, config
        monkeypatch.setattr(agent, "MODEL_LIST", model_list)
        monkeypatch.setattr(config, "DEFAULT_MODEL_ID", "不存在的模型")
        assert agent._resolve_default_model_index() == 0


# ── 设置窗口：图片识别模型下拉回显 / 保存 ──

@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _open_settings(qapp, tmp_path, monkeypatch, cfg, entries):
    """建一个 SettingsDialog（config 指到临时文件、模型列表换成 entries）。"""
    import json
    from PySide6.QtWidgets import QMessageBox
    from src.ui import settings_dialog as sd

    monkeypatch.setattr(models, "MODEL_LIST", entries)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(sd, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)     # 不弹"已保存"
    return sd.SettingsDialog(), cfg_path


class TestVisionPickerInSettings:
    @pytest.mark.parametrize("saved", [
        "⚙ deepseek-v4.1-flash",     # 本版本保存的（显示名）
        "deepseek-v4.1-flash",       # 手改去掉 ⚙
        "deepseek-flash",            # 旧配置：只有 model_id
    ])
    def test_echoes_the_configured_model(self, qapp, tmp_path, monkeypatch, saved):
        entries = [
            ("deepseek-flash", "deepseek", "deepseek-flash", False),
            ("⚙ deepseek-v4.1-flash", "custom", "deepseek-flash", True),
        ]
        dlg, _ = _open_settings(qapp, tmp_path, monkeypatch,
                                {"vision_model_id": saved}, entries)
        try:
            combo = dlg.fields["vision_model_id"]
            expected = "deepseek-flash" if saved == "deepseek-flash" else "⚙ deepseek-v4.1-flash"
            assert combo.currentData() == expected
        finally:
            dlg.deleteLater()

    def test_empty_config_shows_disabled(self, qapp, tmp_path, monkeypatch):
        dlg, _ = _open_settings(qapp, tmp_path, monkeypatch,
                                {"vision_model_id": ""}, ENTRIES)
        try:
            assert dlg.fields["vision_model_id"].currentData() == ""
        finally:
            dlg.deleteLater()

    def test_removed_model_falls_back_to_disabled(self, qapp, tmp_path, monkeypatch):
        dlg, _ = _open_settings(qapp, tmp_path, monkeypatch,
                                {"vision_model_id": "已删除的模型"}, ENTRIES)
        try:
            assert dlg.fields["vision_model_id"].currentData() == ""
        finally:
            dlg.deleteLater()

    def test_save_keeps_the_selected_entry(self, qapp, tmp_path, monkeypatch):
        """存自定义模型（与内置共 id）→ 保存后写的是它的显示名，重新打开仍选中它，不串到内置。"""
        import json
        cfg = {"vision_model_id": "⚙ deepseek-v4.1-flash"}
        dlg, cfg_path = _open_settings(qapp, tmp_path, monkeypatch, cfg, ENTRIES)
        try:
            dlg._save()
            saved = json.loads(cfg_path.read_text(encoding="utf-8"))
            assert saved["vision_model_id"] == "⚙ deepseek-v4.1-flash"
        finally:
            dlg.deleteLater()

        dlg2, _ = _open_settings(qapp, tmp_path, monkeypatch, saved, ENTRIES)
        try:
            assert dlg2.fields["vision_model_id"].currentData() == "⚙ deepseek-v4.1-flash"
        finally:
            dlg2.deleteLater()

    def test_switching_to_disabled_persists_empty(self, qapp, tmp_path, monkeypatch):
        import json
        dlg, cfg_path = _open_settings(
            qapp, tmp_path, monkeypatch, {"vision_model_id": "⚙ deepseek-v4.1-flash"}, ENTRIES)
        try:
            combo = dlg.fields["vision_model_id"]
            combo.setCurrentIndex(combo.findData(""))
            dlg._save()
            saved = json.loads(cfg_path.read_text(encoding="utf-8"))
            assert saved["vision_model_id"] == ""
        finally:
            dlg.deleteLater()
