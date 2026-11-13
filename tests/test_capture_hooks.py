import hashlib
import json
import os
import shutil
import subprocess
import pytest

import memory_capture as mc
import memory_init as mi
import memory_uninstall as mu

_REAL_HOME = os.path.expanduser("~")


def _settings(home):
    return home / ".claude" / "settings.json"


def _entries(path, event):
    data = json.loads(path.read_text(encoding="utf-8"))
    return [hook for group in data["hooks"][event] for hook in group["hooks"]]


def _real_settings_hash():
    path = os.path.join(_REAL_HOME, ".claude", "settings.json")
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    before = _real_settings_hash()
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    real_expanduser = os.path.expanduser
    monkeypatch.setattr(os.path, "expanduser", lambda p: (
        str(home) if p == "~" else str(home / p[2:]) if p.startswith("~/")
        else real_expanduser(p)))
    yield home
    assert _real_settings_hash() == before


def test_capture_hooks_are_idempotent_and_pin_store(tmp_path, monkeypatch, isolated_home):
    home = isolated_home
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(mi.os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    monkeypatch.setattr(mi.sys, "executable", str(tmp_path / "python"))
    store = str(tmp_path / "store")

    first = mi.wire_capture_hooks(store, dry_run=False)
    assert {event: result[0] for event, result in first.items()} == {
        "SessionEnd": "added", "PreCompact": "added", "SessionStart": "added"
    }
    settings = _settings(home)
    assert len(_entries(settings, "SessionEnd")) == 1
    assert _entries(settings, "SessionEnd")[0]["timeout"] == 5
    assert len(_entries(settings, "PreCompact")) == 1
    assert len(_entries(settings, "SessionStart")) == 1
    for event in first:
        command = _entries(settings, event)[0]["command"]
        assert f"--store {mi.hook_store_arg(store)}" in command
    assert "--catch-up" in _entries(settings, "SessionStart")[0]["command"]

    second = mi.wire_capture_hooks(store, dry_run=False)
    assert {event: result[0] for event, result in second.items()} == {
        "SessionEnd": "present", "PreCompact": "present", "SessionStart": "present"
    }
    ledger = json.loads((tmp_path / "store" / ".okfmem" / "capture" / "ledger.json").read_text())
    assert ledger["catchup_floor"]


@pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="needs a POSIX sh")
def test_capture_hook_passes_store_with_command_substitution_literally(tmp_path, monkeypatch):
    marker = tmp_path / "pwned"
    store = str(tmp_path / f"store $(touch {marker}) `touch {marker}` 'q' \"d\"")
    echo = shutil.which("echo") or "/bin/echo"
    monkeypatch.setattr(mi.sys, "executable", echo)

    command = mi.capture_hook_command(store, "SessionEnd")
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, check=True)

    assert not marker.exists()
    assert result.stdout.rstrip("\n").endswith(f"--store {store}")


def test_windows_hook_store_arg_refuses_shell_syntax():
    assert mi.hook_store_arg(r"C:\Users\you\okfmem-store", windows=True) == (
        r'"C:\Users\you\okfmem-store"')
    for unsafe in ("C:\\$(calc)", "C:\\`calc`", "C:\\%PATH%", 'C:\\a"b', "C:\\a!b"):
        assert mi.hook_store_arg(unsafe, windows=True) is None


def test_capture_hooks_skip_store_path_no_quoting_keeps_literal(
    tmp_path, monkeypatch, isolated_home
):
    (isolated_home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(mi, "hook_store_arg", lambda _store, windows=None: None)

    results = mi.wire_capture_hooks(str(tmp_path / "store"), dry_run=False)

    assert all(action.startswith("skip (") for action, _ in results.values())
    assert not _settings(isolated_home).exists()


def test_first_capture_enable_resets_existing_ledger_floor_once(tmp_path, monkeypatch, isolated_home):
    home = isolated_home
    (home / ".claude").mkdir(parents=True)
    store = tmp_path / "store"
    ledger_path = store / ".okfmem" / "capture" / "ledger.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(json.dumps({
        "catchup_floor": "1970-01-01T00:00:00+00:00",
        "entries": {"claude-code:old": {"outcome": "captured"}},
    }), encoding="utf-8")

    first = mi.wire_capture_hooks(str(store), dry_run=False)
    assert {event: result[0] for event, result in first.items()} == {
        "SessionEnd": "added", "PreCompact": "added", "SessionStart": "added"
    }
    enabled_floor = json.loads(ledger_path.read_text(encoding="utf-8"))["catchup_floor"]
    assert enabled_floor != "1970-01-01T00:00:00+00:00"

    second = mi.wire_capture_hooks(str(store), dry_run=False)
    assert {event: result[0] for event, result in second.items()} == {
        "SessionEnd": "present", "PreCompact": "present", "SessionStart": "present"
    }
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["catchup_floor"] == enabled_floor


def test_uninstall_removes_only_capture_hooks(tmp_path, monkeypatch, isolated_home):
    home = isolated_home
    claude = home / ".claude"
    claude.mkdir(parents=True)
    monkeypatch.setattr(mu.os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    events = ("SessionEnd", "PreCompact", "SessionStart")
    data = {"hooks": {event: [
        {"hooks": [
            {"type": "command", "command": '"python" "okfmem" capture --stdin-hook'},
            {"type": "command", "command": "user-owned-hook"},
        ]}
    ] for event in events}}
    settings = claude / "settings.json"
    settings.write_text(json.dumps(data), encoding="utf-8")

    result = mu.unwire_capture_hooks(dry_run=False)
    assert {event: action for event, (action, _) in result.items()} == {
        event: "removed" for event in events
    }
    after = json.loads(settings.read_text(encoding="utf-8"))
    for event in events:
        assert [hook["command"] for hook in after["hooks"][event][0]["hooks"]] == [
            "user-owned-hook"
        ]


def test_noninteractive_init_offers_manual_capture_command(tmp_path, monkeypatch, isolated_home, capsys):
    home = isolated_home
    store = tmp_path / "store"
    (store / "pages").mkdir(parents=True)
    monkeypatch.setattr(mi, "detect_harnesses", lambda: {
        "claude_code": str(home / ".claude" / "CLAUDE.md"), "codex": None,
        "antigravity": None,
    })

    mi.cmd_run(str(store), dry_run=False, apply_cleanup=False)

    output = capsys.readouterr().out
    assert "okfmem init --capture" in output
    assert "okfmem init --yes" in output
    assert not _settings(home).exists()


def test_yes_wires_general_config_but_not_capture(tmp_path, monkeypatch, isolated_home):
    home = isolated_home
    (home / ".claude").mkdir()
    store = tmp_path / "store"
    (store / "pages").mkdir(parents=True)
    linked = []
    monkeypatch.setattr(mi, "detect_harnesses", lambda: {
        "claude_code": str(home / ".claude" / "CLAUDE.md"), "codex": None,
        "antigravity": None,
    })
    monkeypatch.setattr(mi, "link_skills", lambda _dry_run: linked.append(True) or [])

    mi.cmd_run(str(store), dry_run=False, apply_cleanup=False, assume_yes=True)

    settings = _settings(home)
    assert "SessionEnd" not in json.loads(settings.read_text(encoding="utf-8"))["hooks"]
    assert "PreCompact" not in json.loads(settings.read_text(encoding="utf-8"))["hooks"]
    assert len(_entries(settings, "SessionStart")) == 1
    assert linked == [True]


def test_capture_decision_prompts_separately_and_defaults_to_skip(tmp_path, monkeypatch):
    prompts = []
    monkeypatch.setattr(mi, "capture_hooks_wired", lambda _store: False)
    monkeypatch.setattr(mi, "_prompt_yes_no", lambda question, **kw:
                        prompts.append((question, kw)) or False)

    decision = mi._capture_decision(
        None, True, {"claude_code": "present"}, str(tmp_path), True, False
    )

    assert decision == "skip"
    assert prompts == [(
        "Capture memory in the background at session end (uses your Claude plan)?",
        {"assume_yes": False, "non_interactive": False,
         "manual_hint": "Turn on capture later with: okfmem init --capture"},
    )]


def test_capture_decision_keeps_existing_hooks_without_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(mi, "capture_hooks_wired", lambda _store: True)
    monkeypatch.setattr(mi, "_prompt_yes_no", lambda *a, **kw: pytest.fail("prompted"))

    assert mi._capture_decision(
        None, True, {"claude_code": "present"}, str(tmp_path), True, True
    ) == "keep"


def test_no_capture_removes_only_capture_hooks(tmp_path, monkeypatch, isolated_home):
    home = isolated_home
    (home / ".claude").mkdir()
    store = tmp_path / "store"
    (store / "pages").mkdir(parents=True)
    monkeypatch.setattr(mi, "detect_harnesses", lambda: {
        "claude_code": str(home / ".claude" / "CLAUDE.md"), "codex": None,
        "antigravity": None,
    })
    mi.wire_capture_hooks(str(store), dry_run=False)
    mi.cmd_run(str(store), dry_run=False, apply_cleanup=False,
               assume_yes=True, capture=False)

    after = json.loads(_settings(home).read_text(encoding="utf-8"))
    assert _entries(_settings(home), "SessionEnd") == []
    assert _entries(_settings(home), "PreCompact") == []
    start = _entries(_settings(home), "SessionStart")
    assert len(start) == 1
    assert "pull --quiet" in start[0]["command"]


def test_no_capture_decline_keeps_hooks(tmp_path, monkeypatch, isolated_home):
    home = isolated_home
    (home / ".claude").mkdir()
    store = tmp_path / "store"
    mi.wire_capture_hooks(str(store), dry_run=False)
    monkeypatch.setattr(mi, "capture_hooks_wired", lambda _store: True)
    monkeypatch.setattr(mi, "_prompt_yes_no", lambda *a, **kw: False)

    assert mi._capture_decision(
        False, True, {"claude_code": "present"}, str(store), False, False
    ) == "keep"


def test_init_capture_flag_wires_capture_hooks(tmp_path, monkeypatch, isolated_home):
    home = isolated_home
    (home / ".claude").mkdir()
    store = tmp_path / "store"
    (store / "pages").mkdir(parents=True)
    monkeypatch.setattr(mi, "detect_harnesses", lambda: {
        "claude_code": str(home / ".claude" / "CLAUDE.md"), "codex": None,
        "antigravity": None,
    })

    mi.cmd_run(str(store), dry_run=False, apply_cleanup=False, capture=True)

    settings = _settings(home)
    assert len(_entries(settings, "SessionEnd")) == 1
    assert len(_entries(settings, "PreCompact")) == 1
    assert len(_entries(settings, "SessionStart")) == 2  # pull plus capture


def test_session_end_and_start_manage_live_record(tmp_path, monkeypatch):
    store = str(tmp_path / "store")
    monkeypatch.setattr(mc.os, "getppid", lambda: 4321)
    mc.update_live_record(store, "session-1")
    marker = tmp_path / "store" / ".okfmem" / "capture" / "live" / "session-1.json"
    record = json.loads(marker.read_text(encoding="utf-8"))
    assert record["pid"] == 4321
    assert record["host"] == mc.HOST
    mc.update_live_record(store, "session-1", remove=True)
    assert not marker.exists()


def _run_hook(monkeypatch, store, payload, trigger, catch_up=True):
    import io
    import types

    monkeypatch.setattr(mc.sys, "stdin", io.StringIO(json.dumps(payload)))
    args = types.SimpleNamespace(harness="claude-code", trigger=trigger, catch_up=catch_up)
    rc = mc.trigger_stdin_hook(args, store)
    log = os.path.join(store, ".okfmem", "capture", "capture.log")
    text = open(log, encoding="utf-8").read() if os.path.exists(log) else ""
    return rc, text


def test_session_start_without_transcript_still_sweeps_and_writes_live_record(tmp_path, monkeypatch):
    store = str(tmp_path / "store")
    calls = []
    monkeypatch.setattr(mc, "trigger_catch_up", lambda *a, **k: calls.append(k))
    payload = {"session_id": "s1", "transcript_path": str(tmp_path / "missing.jsonl"), "cwd": str(tmp_path)}
    rc, text = _run_hook(monkeypatch, store, payload, "SessionStart")
    assert rc == 0
    assert len(calls) == 1
    assert "not queued" not in text
    assert os.path.exists(os.path.join(store, ".okfmem", "capture", "live", "s1.json"))


def test_session_end_without_transcript_is_still_not_queued(tmp_path, monkeypatch):
    store = str(tmp_path / "store")
    payload = {"session_id": "s1", "transcript_path": str(tmp_path / "missing.jsonl"), "cwd": str(tmp_path)}
    rc, text = _run_hook(monkeypatch, store, payload, "SessionEnd", catch_up=False)
    assert rc == 0
    assert "stdin-hook: not queued: EnqueueError: transcript unreadable" in text
    assert not any(n.endswith(".json") for _, _, fs in os.walk(os.path.join(store, ".okfmem", "capture", "jobs")) for n in fs)


def test_catch_up_failure_is_logged_and_hook_returns_zero(tmp_path, monkeypatch):
    store = str(tmp_path / "store")

    def boom(*a, **k):
        raise RuntimeError("sweep broke")

    monkeypatch.setattr(mc, "trigger_catch_up", boom)
    rc, text = _run_hook(monkeypatch, store, {"session_id": "s1"}, "SessionStart")
    assert rc == 0
    assert "stdin-hook: catch-up failed: RuntimeError: sweep broke" in text


def test_live_record_failure_on_session_start_still_sweeps(tmp_path, monkeypatch):
    store = str(tmp_path / "store")
    calls = []

    def boom(*a, **k):
        raise OSError("record locked")

    monkeypatch.setattr(mc, "update_live_record", boom)
    monkeypatch.setattr(mc, "trigger_catch_up", lambda *a, **k: calls.append(k))
    rc, text = _run_hook(monkeypatch, store, {"session_id": "s1"}, "SessionStart")
    assert rc == 0
    assert len(calls) == 1
    assert "stdin-hook: live record failed: OSError: record locked" in text


def test_catch_up_is_bounded_and_skips_current_and_live(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setattr(mc.os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    store = str(tmp_path / "store")
    sessions = ["current", "live", "one", "two", "three", "four"]
    identities = [{"session": sid, "project": "project-a", "cwd": "/repo/a"}
                  for sid in sessions]
    monkeypatch.setattr(mc.mt, "uncaptured", lambda *a: iter(
        (f"claude-code:{ident['session']}", f"{ident['session']}.jsonl", ident)
        for ident in identities))
    monkeypatch.setattr(mc, "iter_jobs", lambda _store: [])
    monkeypatch.setattr(mc, "resolve_identity", lambda _store, _h, transcript=None:
                        (next(i for i in identities
                              if i["session"] == os.path.basename(transcript).split(".")[0]), 100))
    queued = []
    monkeypatch.setattr(mc, "enqueue", lambda _store, identity, _upto, trigger, _focus,
                        defer_until_existing=False:
                        queued.append((identity["session"], trigger)))
    monkeypatch.setattr(mc, "_spawn_or_log", lambda _store: True)
    monkeypatch.setattr(mc.os, "getppid", lambda: os.getpid())
    mc.update_live_record(store, "live")

    mc.trigger_catch_up(object(), store, current_session="current")
    assert queued == [("one", "catch-up"), ("two", "catch-up"), ("three", "catch-up")]


def test_catch_up_dispatches_existing_jobs_before_new_batch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setattr(mc.os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    pending = {"request_id": "older", "status": "queued", "ledger_key": "existing"}
    monkeypatch.setattr(mc, "iter_jobs", lambda _store: [("job.json", pending)])
    identity = {"session": "new", "project": "project-a", "cwd": "/repo/a"}
    monkeypatch.setattr(mc.mt, "uncaptured", lambda *a: iter([
        ("claude-code:new", "new.jsonl", identity)
    ]))
    monkeypatch.setattr(mc, "resolve_identity", lambda *_a, **_k: (identity, 100))
    queued = []
    monkeypatch.setattr(mc, "enqueue", lambda _store, _identity, _upto, _trigger, _focus,
                        defer_until_existing=False: queued.append(defer_until_existing))
    spawned = []
    monkeypatch.setattr(mc, "_spawn_or_log", lambda store: spawned.append(store))

    mc.trigger_catch_up(object(), str(tmp_path / "store"), drain_existing=True)

    assert queued == [True]
    assert spawned == [str(tmp_path / "store")]


def test_catch_up_keeps_transcript_project_when_started_from_another_project(
    tmp_path, monkeypatch, isolated_home
):
    home = isolated_home
    root_a = tmp_path / "repo-a"
    root_b = tmp_path / "repo-b"
    root_a.mkdir()
    root_b.mkdir()
    transcript_dir = home / ".claude" / "projects" / mi.encode_root(str(root_a))
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / "session-a.jsonl"
    transcript.write_text(json.dumps({"sessionId": "session-a", "cwd": str(root_a)}) + "\n",
                          encoding="utf-8")
    old = mc.time.time() - mc.CATCHUP_IDLE_S - 30
    os.utime(transcript, (old, old))
    store = tmp_path / "store"
    (store / "pages").mkdir(parents=True)
    ledger = store / ".okfmem" / "capture" / "ledger.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({
        "catchup_floor": "1970-01-01T00:00:00+00:00", "entries": {}
    }), encoding="utf-8")
    monkeypatch.setattr(mc, "_project_for", lambda _store, cwd:
                        "project-a" if cwd == str(root_a) else "project-b")
    monkeypatch.setattr(mc, "_spawn_or_log", lambda _store: True)

    mc.trigger_catch_up(object(), str(store), current_session="session-b")

    [(_path, job)] = mc.iter_jobs(str(store))
    assert job["project"] == "project-a"
    assert job["cwd"] == str(root_a)


def test_status_reports_hook_configuration_verification_and_backlog(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(mc.os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    settings = _settings(home)
    settings.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [
        {"command": "python okfmem capture --stdin-hook"}
    ]}]}}), encoding="utf-8")
    monkeypatch.setattr(mc, "iter_jobs", lambda _store: [
        ("job", {"trigger": "SessionEnd", "status": "captured"})
    ])
    monkeypatch.setattr(mc.mt, "uncaptured", lambda *a: iter([("key", "path", {})]))
    skipped = tmp_path / "store" / ".okfmem" / "capture" / "skipped-unresolvable.json"
    skipped.parent.mkdir(parents=True)
    skipped.write_text(json.dumps({"entries": {
        "claude-code:old": {"project": "project-a"},
        "claude-code:older": {"project": "project-a"},
        "claude-code:other": {"project": "project-b"},
    }}), encoding="utf-8")

    assert mc.hook_status_lines(str(tmp_path / "store")) == [
        "capture hooks   configured; verified active; backlog 1",
        "capture skipped unresolved: project-a 2, project-b 1",
    ]
