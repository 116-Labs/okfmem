import os
import sys
import subprocess
import pytest
import memory_inbox as mx
import memory_pull as mp
import memory_init as mi
from test_inbox import _bulk_inbox, env, _git, _read, _write  # noqa: F401


def test_inbox_pull_nudge_when_over_threshold(env, monkeypatch, capsys):
    _bulk_inbox(env["store"], 15)
    monkeypatch.setattr(mx, "fetch_inbox", lambda store: (True, ""))
    mx.pull(env["store"])
    captured = capsys.readouterr()
    assert "okfmem inbox: 15 pending — run okfmem inbox review" in captured.out


def test_inbox_pull_no_nudge_at_or_below_threshold(env, monkeypatch, capsys):
    _bulk_inbox(env["store"], 10)
    monkeypatch.setattr(mx, "fetch_inbox", lambda store: (True, ""))
    mx.pull(env["store"])
    captured = capsys.readouterr()
    assert "run okfmem inbox review" not in captured.out


def test_status_nudge_when_over_threshold(env, capsys):
    _bulk_inbox(env["store"], 12)
    mi.cmd_status(env["store"])
    captured = capsys.readouterr()
    assert "okfmem inbox: 12 pending — run okfmem inbox review" in captured.out


def test_pull_quiet_nudge_once_per_day(env, capsys):
    _bulk_inbox(env["store"], 14)
    # First quiet pull
    mp.check_inbox_nudge(env["store"], quiet=True)
    captured = capsys.readouterr()
    assert "okfmem inbox: 14 pending — run okfmem inbox review" in captured.out

    # Second quiet pull on same day -> no output
    mp.check_inbox_nudge(env["store"], quiet=True)
    captured = capsys.readouterr()
    assert "run okfmem inbox review" not in captured.out


def test_inbox_review_walkthrough_non_tty_exits_0(env, monkeypatch, capsys):
    _bulk_inbox(env["store"], 12)
    monkeypatch.setattr(mx, "_isatty", lambda: False)
    monkeypatch.setattr(sys, "argv", ["memory_inbox.py", "review", "--store", env["store"]])
    with pytest.raises(SystemExit) as exc:
        mx.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "How triage works:" in captured.out
    assert "1. In your harness: run the okfmem-inbox skill" in captured.out
    assert "2. In your browser: okfmem inbox review --web" in captured.out


def test_inbox_review_walkthrough_each_bypasses_walkthrough(env, monkeypatch):
    _bulk_inbox(env["store"], 12)
    monkeypatch.setattr(mx, "_isatty", lambda: False)
    # With --each and non-tty, it should fail with no_terminal_hint (code 2), not exit 0 from walkthrough
    monkeypatch.setattr(sys, "argv", ["memory_inbox.py", "review", "--store", env["store"], "--each"])
    with pytest.raises(SystemExit) as exc:
        mx.main()
    assert exc.value.code == 2


def test_inbox_review_walkthrough_custom_threshold(env, monkeypatch, capsys):
    _bulk_inbox(env["store"], 5)
    monkeypatch.setattr(mx, "_isatty", lambda: False)
    # With threshold 4, 5 pending triggers walkthrough
    monkeypatch.setattr(sys, "argv", ["memory_inbox.py", "review", "--store", env["store"], "--threshold", "4"])
    with pytest.raises(SystemExit) as exc:
        mx.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "How triage works:" in captured.out


def test_inbox_review_walkthrough_tty_option_1(env, monkeypatch, capsys):
    _bulk_inbox(env["store"], 12)
    monkeypatch.setattr(mx, "_isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "1")
    monkeypatch.setattr(sys, "argv", ["memory_inbox.py", "review", "--store", env["store"]])
    with pytest.raises(SystemExit) as exc:
        mx.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "Run /okfmem-inbox in your agent harness" in captured.out
