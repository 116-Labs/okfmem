import importlib
import os
import subprocess
import sys

import pytest


def _module(monkeypatch):
    update = importlib.import_module("memory_update")
    monkeypatch.setattr(update, "_ensure_git_repo", lambda: None)
    monkeypatch.setattr(update, "_short", lambda: "same")
    monkeypatch.setattr(update, "_engine_branch_state", lambda: ("main", "main"))
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


def _run(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """A temp engine clone of a bare origin, on its default branch `main`."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "engine"
    _run(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _run(tmp_path, "init", "-b", "main", str(seed))
    (seed / "VERSION").write_text("9.9.9\n")
    (seed / "tracked.txt").write_text("a\n")
    _run(seed, "add", ".")
    _run(seed, "commit", "-m", "init")
    _run(seed, "push", str(origin), "main")
    _run(tmp_path, "clone", str(origin), str(clone))
    update = importlib.import_module("memory_update")
    monkeypatch.setattr(update, "ENGINE", str(clone))
    return update, clone


def _exit_code(fn, *args, **kwargs):
    with pytest.raises(SystemExit) as exc:
        fn(*args, **kwargs)
    return exc.value.code


def test_update_refuses_clean_feature_branch(engine, capsys):
    update, clone = engine
    _run(clone, "switch", "-c", "some-feature")

    assert _exit_code(update.cmd_update, [], yes=True) == 1

    err = capsys.readouterr().err
    assert str(clone) in err
    assert "'some-feature', not 'main'" in err
    assert "9.9.9 at " in err
    assert f'git -C "{clone}" switch main' in err
    assert "already up to date" not in err


def test_update_refuses_dirty_feature_branch_with_branch_message(engine, capsys):
    update, clone = engine
    _run(clone, "switch", "-c", "some-feature")
    (clone / "tracked.txt").write_text("changed\n")

    assert _exit_code(update.cmd_update, [], yes=True) == 1

    err = capsys.readouterr().err
    assert "'some-feature', not 'main'" in err
    assert "uncommitted" not in err


def test_update_reports_detached_head(engine, capsys):
    update, clone = engine
    _run(clone, "switch", "--detach", "HEAD")

    assert _exit_code(update.cmd_update, [], yes=True) == 1

    err = capsys.readouterr().err
    assert f"a detached HEAD at {update._short()}" in err


def test_update_dirty_default_branch_counts_files_and_suggests_stash_u(engine, capsys):
    update, clone = engine
    (clone / "tracked.txt").write_text("changed\n")
    for name in ("u1.txt", "u2.txt", "u3.txt"):
        (clone / name).write_text("x\n")

    assert _exit_code(update.cmd_update, [], yes=True) == 1

    err = capsys.readouterr().err
    assert "4 file(s)" in err
    assert "tracked.txt" in err
    assert "u3.txt" not in err
    assert "stash -u" in err


def test_update_clean_default_branch_pulls_and_runs_init(engine, monkeypatch, capsys):
    update, clone = engine
    calls = []
    real_run = subprocess.run

    def fake_run(args, **kw):
        if args[0] == "git":
            return real_run(args, **kw)
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert _exit_code(update.cmd_update, [], yes=True) == 0

    assert "already up to date" in capsys.readouterr().out
    assert len(calls) == 1 and calls[0][1].endswith("memory_init.py")


def _diverge(tmp_path, clone):
    """Commit locally on the engine's main and push a different commit to origin."""
    (clone / "local.txt").write_text("mine\n")
    _run(clone, "add", "local.txt")
    _run(clone, "commit", "-m", "local work")
    seed = tmp_path / "seed"
    (seed / "upstream.txt").write_text("theirs\n")
    _run(seed, "add", "upstream.txt")
    _run(seed, "commit", "-m", "upstream work")
    _run(seed, "push", str(tmp_path / "origin.git"), "main")


def _stub_init(update, monkeypatch):
    real_run = subprocess.run

    def fake_run(args, **kw):
        if args[0] == "git":
            return real_run(args, **kw)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(update.subprocess, "run", fake_run)


def test_update_diverged_default_branch_names_keep_and_reset_commands(
        engine, tmp_path, monkeypatch, capsys):
    update, clone = engine
    _diverge(tmp_path, clone)
    _stub_init(update, monkeypatch)

    assert _exit_code(update.cmd_update, [], yes=True) == 1

    err = capsys.readouterr().err
    assert "fast-forward pull failed" in err
    assert "'main' has 1 local commit(s) not on origin/main" in err
    assert f'git -C "{clone}" branch local-engine-work-{update._short()}\n' in err
    assert f'git -C "{clone}" reset --keep origin/main' in err
    assert "stash" not in err


def test_update_succeeds_after_following_diverged_advice(engine, tmp_path, monkeypatch, capsys):
    update, clone = engine
    _diverge(tmp_path, clone)
    _stub_init(update, monkeypatch)
    keep = f"local-engine-work-{update._short()}"
    assert _exit_code(update.cmd_update, [], yes=True) == 1
    assert f"branch {keep}\n" in capsys.readouterr().err

    _run(clone, "branch", keep)
    _run(clone, "reset", "--keep", "origin/main")

    assert _exit_code(update.cmd_update, [], yes=True) == 0
    assert "re-running init" in capsys.readouterr().out
    assert (clone / "upstream.txt").exists()
    assert not (clone / "local.txt").exists()
    assert "local work" in subprocess.run(
        ["git", "-C", str(clone), "log", "-1", "--format=%s", keep],
        capture_output=True, text=True, check=True).stdout


def test_repeat_divergence_names_a_fresh_keep_branch(engine, tmp_path, monkeypatch, capsys):
    update, clone = engine
    _stub_init(update, monkeypatch)
    _diverge(tmp_path, clone)
    first = f"local-engine-work-{update._short()}"
    _run(clone, "branch", first)
    _run(clone, "reset", "--keep", "origin/main")
    assert _exit_code(update.cmd_update, [], yes=True) == 0
    (clone / "local.txt").write_text("again\n")
    _run(clone, "add", "local.txt")
    _run(clone, "commit", "-m", "more local work")
    seed = tmp_path / "seed"
    (seed / "upstream2.txt").write_text("theirs again\n")
    _run(seed, "add", "upstream2.txt")
    _run(seed, "commit", "-m", "more upstream work")
    _run(seed, "push", str(tmp_path / "origin.git"), "main")
    capsys.readouterr()

    assert _exit_code(update.cmd_update, [], yes=True) == 1

    err = capsys.readouterr().err
    assert f"branch local-engine-work-{update._short()}\n" in err
    assert f"branch {first}\n" not in err


def test_update_pull_failure_without_local_commits_gives_no_reset_advice(
        engine, monkeypatch, capsys):
    update, clone = engine
    real_git = update._git

    def failing_pull(*args, **kw):
        if args[0] == "pull":
            return subprocess.CompletedProcess(args, 1, "", "fatal: no upstream")
        return real_git(*args, **kw)

    monkeypatch.setattr(update, "_git", failing_pull)

    assert _exit_code(update.cmd_update, [], yes=True) == 1

    err = capsys.readouterr().err
    assert "fast-forward pull failed" in err
    assert "reset --keep" not in err
    assert "stash" not in err


def test_update_pull_failure_ahead_only_gives_no_reset_advice(engine, monkeypatch, capsys):
    update, clone = engine
    (clone / "local.txt").write_text("mine\n")
    _run(clone, "add", "local.txt")
    _run(clone, "commit", "-m", "local work")
    real_git = update._git

    def failing_pull(*args, **kw):
        if args[0] == "pull":
            return subprocess.CompletedProcess(args, 1, "", "fatal: unable to access origin")
        return real_git(*args, **kw)

    monkeypatch.setattr(update, "_git", failing_pull)

    assert _exit_code(update.cmd_update, [], yes=True) == 1

    err = capsys.readouterr().err
    assert "fast-forward pull failed" in err
    assert "local commit(s) not on" not in err
    assert "reset --keep" not in err


def test_check_diverged_off_default_branch_gives_no_reset_advice(engine, tmp_path, capsys):
    update, clone = engine
    _run(clone, "switch", "-c", "some-feature", "--track", "origin/main")
    _diverge(tmp_path, clone)

    update.cmd_check()

    out = capsys.readouterr().out
    assert "note: engine checkout is on 'some-feature', not 'main'" in out
    assert "also 1 local commit(s) ahead" in out
    assert "reset --keep" not in out


def test_check_diverged_default_branch_gives_same_advice(engine, tmp_path, capsys):
    update, clone = engine
    _diverge(tmp_path, clone)

    update.cmd_check()

    out = capsys.readouterr().out
    assert "1 commit(s) behind origin/main" in out
    assert "also 1 local commit(s) ahead" in out
    assert f'git -C "{clone}" branch local-engine-work-{update._short()}\n' in out
    assert f'git -C "{clone}" reset --keep origin/main' in out
    assert "stash" not in out


def test_check_notes_off_default_branch(engine, capsys):
    update, clone = engine
    _run(clone, "switch", "-c", "some-feature")

    _exit_code(update.cmd_check)

    assert "note: engine checkout is on 'some-feature', not 'main'" in capsys.readouterr().out
