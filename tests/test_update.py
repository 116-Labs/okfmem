import importlib
import os
import subprocess
import sys


def _module(monkeypatch):
    update = importlib.import_module("memory_update")
    monkeypatch.setattr(update, "_ensure_git_repo", lambda: None)
    monkeypatch.setattr(update, "_short", lambda: "same")
    monkeypatch.setattr(update, "_git", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 0, "", ""
    ))
    return update


def test_headless_update_without_yes_fails_before_init(monkeypatch, capsys):
    update = _module(monkeypatch)
    monkeypatch.setattr(update.sys.stdin, "isatty", lambda: False)

    try:
        update.cmd_update([])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("headless update unexpectedly succeeded")

    captured = capsys.readouterr()
    assert "fully wired" not in captured.out
    assert "okfmem update --yes" in captured.err


def test_headless_update_with_yes_passes_yes_to_init(monkeypatch):
    update = _module(monkeypatch)
    calls = []
    monkeypatch.setattr(update.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        update.subprocess,
        "run",
        lambda args: calls.append(args) or subprocess.CompletedProcess(args, 0),
    )

    try:
        update.cmd_update(["--store", "/store"], yes=True)
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("update did not exit after init")

    assert calls == [[sys.executable, os.path.join(update.ENGINE, "memory_init.py"), "--store", "/store", "--yes"]]
