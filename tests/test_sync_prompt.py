"""`okfmem sync` without -m must not block on a non-tty stdin."""

import os
import sys

import pytest

import memory_sync as ms


def _run_main(monkeypatch, capsys, stdin, input_fn):
    seen = {}

    def fake_sync_store(store, message, do_push=True, do_pull=True):
        seen["message"] = message
        return {"reason": "ok", "pushed": False, "push_error": None, "conflict": False}

    monkeypatch.setattr(ms, "sync_store", fake_sync_store)
    monkeypatch.setattr("builtins.input", input_fn)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "argv", ["okfmem-sync", "--store", "unused"])
    with pytest.raises(SystemExit) as exc:
        ms.main()
    assert exc.value.code == 0
    return seen["message"], capsys.readouterr().out


def _no_input(*_a, **_k):
    raise AssertionError("input() must not be called on a non-tty stdin")


def test_open_pipe_stdin_uses_default_without_prompting(monkeypatch, capsys):
    r, w = os.pipe()  # write end stays open: no EOF, input() would block
    try:
        with os.fdopen(r) as pipe_in:
            msg, out = _run_main(monkeypatch, capsys, pipe_in, _no_input)
    finally:
        os.close(w)
    assert msg.startswith("okfmem sync ")
    assert "non-interactive stdin" in out
    assert msg in out


def test_missing_stdin_uses_default(monkeypatch, capsys):
    msg, out = _run_main(monkeypatch, capsys, None, _no_input)
    assert msg.startswith("okfmem sync ")
    assert "non-interactive stdin" in out


def test_stdin_without_isatty_uses_default(monkeypatch, capsys):
    msg, out = _run_main(monkeypatch, capsys, object(), _no_input)
    assert msg.startswith("okfmem sync ")
    assert "non-interactive stdin" in out
    assert msg in out


def test_stdin_with_noncallable_isatty_uses_default(monkeypatch, capsys):
    class _Odd:
        isatty = True

    msg, out = _run_main(monkeypatch, capsys, _Odd(), _no_input)
    assert msg.startswith("okfmem sync ")
    assert "non-interactive stdin" in out


def test_closed_stdin_uses_default(monkeypatch, capsys):
    r, w = os.pipe()
    os.close(w)
    pipe_in = os.fdopen(r)
    pipe_in.close()  # isatty() now raises ValueError
    msg, out = _run_main(monkeypatch, capsys, pipe_in, _no_input)
    assert msg.startswith("okfmem sync ")
    assert "non-interactive stdin" in out


class _Tty:
    def isatty(self):
        return True


def test_tty_enter_takes_default(monkeypatch, capsys):
    msg, out = _run_main(monkeypatch, capsys, _Tty(), lambda prompt: "")
    assert msg.startswith("okfmem sync ")
    assert "non-interactive" not in out


def test_tty_typed_message_wins(monkeypatch, capsys):
    msg, _ = _run_main(monkeypatch, capsys, _Tty(), lambda prompt: "my msg")
    assert msg == "my msg"
