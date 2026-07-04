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
