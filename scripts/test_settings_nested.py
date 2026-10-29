"""SettingsDialog 嵌套配置读写（_get_nested/_set_nested）。

_set_nested 走非绑定方法 + SimpleNamespace(config=...)，只测纯逻辑（不建 QDialog、
免 QApplication）。重点：损坏的中间节点（如 "rag": "oops"）保存时必须能自愈成 dict，
否则旧实现 setdefault 拿回字符串 → 下标赋值抛 TypeError，用户无法在设置页改回来。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from src.ui.settings_dialog import SettingsDialog


def _fake(cfg):
    return SimpleNamespace(config=cfg)


def test_set_nested_creates_intermediate_dict():
    f = _fake({})
    SettingsDialog._set_nested(f, "rag.embed_model", "m")
    assert f.config == {"rag": {"embed_model": "m"}}


def test_set_nested_preserves_sibling_keys():
    f = _fake({"rag": {"kb_dir": "/kb"}})
    SettingsDialog._set_nested(f, "rag.embed_model", "m")
    assert f.config["rag"] == {"kb_dir": "/kb", "embed_model": "m"}


def test_set_nested_heals_corrupt_non_dict_node():
    """P2#3：中间节点是字符串（损坏配置）→ 替换成 {} 而非抛 TypeError。"""
    f = _fake({"rag": "oops"})
    SettingsDialog._set_nested(f, "rag.embed_model", "m")   # 不应抛异常
    assert f.config["rag"] == {"embed_model": "m"}


def test_get_nested_safe_on_corrupt_node():
    """读取端对损坏节点回退默认值（启动/加载不崩）。"""
    f = _fake({"rag": "oops"})
    assert SettingsDialog._get_nested(f, "rag.embed_model", "def") == "def"


def test_search_settings_save_and_apply_without_restart(tmp_path, monkeypatch):
    """设置 → 高级 → 网络搜索：保存写进 config.json，并立即反映到运行中的程序（web_search 每次
    调用才读这几个值），不用重启。"""
    import json
    from PySide6.QtWidgets import QApplication, QMessageBox
    from src import config
    from src.ui import settings_dialog as sd

    QApplication.instance() or QApplication([])
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"web_search_api_key": "tvly-old"}), encoding="utf-8")
    monkeypatch.setattr(sd, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)       # 不弹"已保存"
    for name in ("WEB_SEARCH_API_KEY", "BOCHA_API_KEY", "WEB_SEARCH_PROVIDER", "WEB_SEARCH_DEEP"):
        monkeypatch.setattr(config, name, getattr(config, name))   # 测完还原

    dlg = sd.SettingsDialog()
    assert dlg.fields["web_search_api_key"].text() == "tvly-old"
    assert dlg.fields["web_search_provider"].currentData() == "auto"
    assert dlg.fields["web_search_deep"].isChecked() is False
    dlg.fields["bocha_api_key"].setText("sk-bocha")
    dlg.fields["web_search_provider"].setCurrentIndex(
        dlg.fields["web_search_provider"].findData("bocha"))
    dlg.fields["web_search_deep"].setChecked(True)
    dlg._save()

    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["bocha_api_key"] == "sk-bocha" and saved["web_search_provider"] == "bocha"
    assert saved["web_search_deep"] is True
    assert (config.BOCHA_API_KEY, config.WEB_SEARCH_PROVIDER, config.WEB_SEARCH_DEEP) == \
        ("sk-bocha", "bocha", True)
    dlg.deleteLater()
