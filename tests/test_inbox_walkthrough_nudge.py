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
    assert "okfmem inbox: 15 pending — run /okfmem-inbox in your agent" in captured.out


def test_inbox_pull_no_nudge_at_or_below_threshold(env, monkeypatch, capsys):
    _bulk_inbox(env["store"], 10)
    monkeypatch.setattr(mx, "fetch_inbox", lambda store: (True, ""))
    mx.pull(env["store"])
    captured = capsys.readouterr()
    assert "pending — run" not in captured.out


def test_status_nudge_when_over_threshold(env, capsys):
    _bulk_inbox(env["store"], 12)
    mi.cmd_status(env["store"])
    captured = capsys.readouterr()
    assert "okfmem inbox: 12 pending — run /okfmem-inbox in your agent" in captured.out


def test_pull_quiet_nudge_once_per_day(env, capsys):
    _bulk_inbox(env["store"], 14)
    # First quiet pull
    mp.check_inbox_nudge(env["store"], quiet=True)
    captured = capsys.readouterr()
    assert "okfmem inbox: 14 pending — run /okfmem-inbox in your agent" in captured.out

    # Second quiet pull on same day -> no output
    mp.check_inbox_nudge(env["store"], quiet=True)
    captured = capsys.readouterr()
    assert "pending — run" not in captured.out


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


def test_inbox_review_walkthrough_ctrl_c_exits_130(env, monkeypatch):
    _bulk_inbox(env["store"], 12)
    monkeypatch.setattr(mx, "_isatty", lambda: True)

    def boom(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr(sys, "argv", ["memory_inbox.py", "review", "--store", env["store"]])
    with pytest.raises(SystemExit) as exc:
        mx.main()
    assert exc.value.code == 130


def test_inbox_review_walkthrough_option_2_starts_web_review(env, monkeypatch):
    _bulk_inbox(env["store"], 12)
    calls = []
    monkeypatch.setattr(mx, "_isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "2")
    monkeypatch.setattr(mx, "git_clean", lambda d: True)
    monkeypatch.setattr(mx, "review_web",
                        lambda store, work, confirm, dest=None, into=None, threshold=None:
                        calls.append((store, dest, into, threshold)) or 0)
    assert mx.review_walkthrough(env["store"], into="proj", threshold=4) == 0
    assert calls == [(env["store"], None, "proj", 4)]


def test_inbox_review_web_forwards_threshold_to_review_web(env, monkeypatch):
    seen = []
    monkeypatch.setattr(mx, "git_clean", lambda d: True)
    monkeypatch.setattr(mx, "review_web", lambda *a, **k: seen.append(k.get("threshold")) or 0)
    monkeypatch.setattr(sys, "argv", ["memory_inbox.py", "review", "--store", env["store"],
                                      "--web", "--threshold", "3"])
    with pytest.raises(SystemExit) as exc:
        mx.main()
    assert exc.value.code == 0
    assert seen == [3]


def test_walkthrough_option_2_says_the_page_starts_undecided():
    text = mx.review_walkthrough_text()
    assert "starts undecided unless --dir holds a decisions.json" in text
    assert "/okfmem-inbox" in text


def test_nudge_line_names_triage_first():
    assert "run /okfmem-inbox in your agent" in mx.nudge_line(15)


def test_inbox_review_walkthrough_option_2_dirty_tree_exits_3(env, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "2")
    monkeypatch.setattr(mx, "git_clean", lambda d: False)
    monkeypatch.setattr(mx, "review_web", lambda *a, **k: pytest.fail("must not start"))
    assert mx.review_walkthrough(env["store"]) == 3


def test_quiet_nudge_keeps_store_tree_clean(env):
    _bulk_inbox(env["store"], 14)
    store = env["store"]
    mp.check_inbox_nudge(store, quiet=True)
    out = subprocess.run(["git", "-C", store, "status", "--porcelain"],
                         capture_output=True, text=True).stdout
    assert ".okfmem" not in out
