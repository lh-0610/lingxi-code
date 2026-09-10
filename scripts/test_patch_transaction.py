"""写盘故障发生在校验之后时，补丁仍不能留下未报告的半成品。"""
import os
import stat
from pathlib import Path

import pytest

from src import file_transaction, session, tools


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    monkeypatch.setattr(tools._checkpoint, "make_checkpoint", lambda *args: None)
    monkeypatch.setattr(tools, "_confirm_file_write", lambda *args: (True, None))
    monkeypatch.setattr(tools, "_run_code_check", lambda *args: ("", None))


def fail_destination(monkeypatch, destination):
    replace = os.replace

    def fail(source, target):
        if os.fspath(target) == str(destination):
            raise PermissionError("injected write failure")
        return replace(source, target)

    monkeypatch.setattr(file_transaction.os, "replace", fail)


@pytest.mark.parametrize("first_action", ["Update", "Add", "Delete"])
def test_second_write_failure_rolls_back_all(project_dir, monkeypatch, first_action):
    a, b = project_dir / "a.txt", project_dir / "b.txt"
    original = b"original\r\n"
    if first_action != "Add":
        a.write_bytes(original)
    b.write_text("before\n", encoding="utf-8")
    first = f"*** {first_action} File: a.txt\n"
    if first_action == "Update":
        first += "@@\n-original\n+changed\n"
    elif first_action == "Add":
        first += "+changed\n"
    fail_destination(monkeypatch, b)
    result = tools.apply_patch.func(
        "*** Begin Patch\n" + first
        + "*** Update File: b.txt\n@@\n-before\n+after\n*** End Patch\n")
    assert "已回滚" in result
    assert a.read_bytes() == original if first_action != "Add" else not a.exists()
    assert b.read_text(encoding="utf-8") == "before\n"
    assert session.get_verification()["dirty_files"] == []
    assert not list(project_dir.glob(".lingxi-patch-*"))


def test_changed_during_confirmation_is_not_overwritten(project_dir, monkeypatch):
    target = project_dir / "a.txt"
    target.write_text("before\n", encoding="utf-8")

    def confirm(*args):
        target.write_text("user edit\n", encoding="utf-8")
        return True, None

    monkeypatch.setattr(tools, "_confirm_file_write", confirm)
    result = tools.apply_patch.func(
        "*** Begin Patch\n*** Update File: a.txt\n@@\n-before\n+after\n*** End Patch\n")
    assert "文件已变化" in result
    assert target.read_text(encoding="utf-8") == "user edit\n"


def test_duplicate_target_is_rejected(project_dir):
    result = tools.apply_patch.func(
        "*** Begin Patch\n*** Add File: a.txt\n+one\n*** Add File: a.txt\n+two\n*** End Patch\n")
    assert "出现多次" in result
    assert not (project_dir / "a.txt").exists()


def test_staging_failure_does_not_modify_any_file(project_dir, monkeypatch):
    a, b = project_dir / "a.txt", project_dir / "sub" / "b.txt"
    a.write_bytes(b"before")
    mkstemp = file_transaction.tempfile.mkstemp
    calls = 0

    def fail(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("disk full")
        return mkstemp(**kwargs)

    monkeypatch.setattr(file_transaction.tempfile, "mkstemp", fail)
    with pytest.raises(file_transaction.PatchApplyError):
        file_transaction.apply_file_changes([(str(a), b"before", "after"), (str(b), None, "new")])
    assert a.read_bytes() == b"before"
    assert not b.parent.exists()
    assert not list(project_dir.glob(".lingxi-patch-*"))


def test_rollback_failure_keeps_recovery_backup(project_dir, monkeypatch):
    a, b = project_dir / "a.txt", project_dir / "b.txt"
    a.write_bytes(b"a-before")
    b.write_bytes(b"b-before")
    replace = os.replace
    calls = 0

    def fail_after_first(source, target):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise PermissionError("injected persistent failure")
        return replace(source, target)

    monkeypatch.setattr(file_transaction.os, "replace", fail_after_first)
    with pytest.raises(file_transaction.PatchApplyError) as failure:
        file_transaction.apply_file_changes([
            (str(a), b"a-before", "a-after"), (str(b), b"b-before", "b-after"),
        ])
    assert failure.value.rollback_failed
    backups = list(project_dir.glob(".lingxi-patch-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"a-before"
    assert str(backups[0]) in str(failure.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode")
def test_replacement_preserves_file_mode(project_dir):
    target = project_dir / "a.sh"
    target.write_bytes(b"before")
    target.chmod(0o750)
    file_transaction.apply_file_changes([(str(target), b"before", "after")])
    assert stat.S_IMODE(target.stat().st_mode) == 0o750
    assert Path(target).read_text() == "after"
