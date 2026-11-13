import io
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import memory_capture as mc
import memory_init as mi
import memory_transcript as mt

ROOT = Path(mc.__file__).parent
OKFMEM = str(ROOT / "okfmem")

STUB = r'''
import json, os, sys, time
data = sys.stdin.read()
log = os.environ["STUB_LOG"]
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "internal": os.environ.get("OKFMEM_INTERNAL"),
                        "stdin": data, "cwd": os.getcwd(), "cwd_files": os.listdir(".")}) + "\n")
with open(log, encoding="utf-8") as f:
    n = sum(1 for _ in f) - 1
path = os.environ.get("STUB_RESPONSES", "")
responses = json.load(open(path, encoding="utf-8")) if path and os.path.exists(path) else []
resp = (responses[n] if n < len(responses) else responses[-1]) if responses else {}
time.sleep(resp.get("sleep", 0))
print(json.dumps({"type": "result", "subtype": resp.get("subtype", "success"), "is_error": False,
                  "total_cost_usd": resp.get("cost", 0.0), "structured_output": resp.get("reply")}))
sys.exit(resp.get("exit", 0))
'''

GIT_STUB = r'''
import os, sys
with open(os.environ["GIT_STUB_LOG"], "a", encoding="utf-8") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
sys.exit(1)
'''


def _install_stub(bindir, name, source):
    script = bindir / f"{name}_stub.py"
    script.write_text(source, encoding="utf-8")
    if os.name == "nt":
        (bindir / f"{name}.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = bindir / name
        exe.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def cap(tmp_path, monkeypatch):
    store = tmp_path / "store"
    (store / "pages").mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@e.st"], ["config", "user.name", "t"],
                 ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", str(store), *args], check=True)
    (store / ".gitignore").write_text(".okfmem/\nviews/\n.okfmem-sync.lock\n")
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    sessions = Path(os.environ["HOME"]) / ".claude" / "projects" / "-proj"
    sessions.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _install_stub(bindir, "claude", STUB)
    log, responses = tmp_path / "stub.log", tmp_path / "responses.json"
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("STUB_RESPONSES", str(responses))
    monkeypatch.setenv("OKFMEM_STORE", str(store))
    monkeypatch.delenv("OKFMEM_INTERNAL", raising=False)
    spawned = []
    monkeypatch.setattr(mc, "spawn_worker", lambda s: spawned.append(s))
    monkeypatch.setattr(mc, "apply_result", lambda s, j: None)  # the engine tests stop at the result

    def transcript(records, sid="s1", name=None):
        path = sessions / f"{name or sid}.jsonl"
        path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in records))
        return path

    def calls():
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def respond(items):
        responses.write_text(json.dumps(items), encoding="utf-8")

    def config(**values):
        path = store / ".okfmem" / "capture" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values), encoding="utf-8")

    def job(transcript_path, focus=None):
        identity, upto = mc.resolve_identity(str(store), "claude-code", transcript=str(transcript_path))
        return mc.enqueue(str(store), identity, upto, "test", focus)

    def jobs():
        return [j for _, j in mc.iter_jobs(str(store))]

    def ledger_offset(key="claude-code:s1"):
        path = store / ".okfmem" / "capture" / "ledger.json"
        entry = json.loads(path.read_text())["entries"].get(key) if path.exists() else None
        return entry["offset"] if entry else None

    return SimpleNamespace(store=store, repo=repo, sessions=sessions, bindir=bindir, log=log,
                           transcript=transcript, calls=calls, respond=respond, config=config,
                           job=job, jobs=jobs, ledger_offset=ledger_offset, spawned=spawned,
                           tmp=tmp_path)


def user(text, cwd, sid="s1"):
    return {"type": "user", "sessionId": sid, "cwd": str(cwd), "timestamp": "t",
            "message": {"role": "user", "content": text}}


def assistant(text, cwd, sid="s1"):
    return {"type": "assistant", "sessionId": sid, "cwd": str(cwd), "timestamp": "t",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def tool_use(cwd, sid="s1"):
    return {"type": "assistant", "sessionId": sid, "cwd": str(cwd), "timestamp": "t",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}}]}}


def page(slug="runner-image-pin", action="create", body="# Pin the runner image\n\nDrift broke CI.\n",
         labels=("repo:proj",)):
    return {"slug": slug, "action": action, "name": "Pin the runner image",
            "description": "Unpinned runner images drift", "type": "project",
            "labels": list(labels), "body": body}


def reply(*pages, state=None):
    return {"pages": list(pages) or [page()], "state": state, "summary": "captured"}


TRIAGE_SAVE = {"reply": {"decision": "save", "reason": "a decision"}, "cost": 0.0123}
TRIAGE_SKIP = {"reply": {"decision": "skip", "reason": "routine"}, "cost": 0.01}


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def test_now_returns_at_once_and_the_detached_worker_calls_the_stub_internally(cap, monkeypatch):
    cap.transcript([user("We decided to pin the runner image because drift broke CI", cap.repo)])
    cap.respond([{**TRIAGE_SAVE, "sleep": 5}, {"reply": reply(), "cost": 0.1}])
    started = time.monotonic()
    r = subprocess.run([sys.executable, OKFMEM, "capture", "--now", "--harness", "claude-code",
                        "--session", "s1", "--store", str(cap.store)],
                       capture_output=True, text=True, timeout=60)
    elapsed = time.monotonic() - started
    assert r.returncode == 0, r.stderr
    assert elapsed < 1.0, elapsed
    assert json.loads(r.stdout)["status"] == "queued"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if any(j["status"] not in mc.PENDING + ("captured",) for j in cap.jobs()):
            break
        time.sleep(0.2)
    calls = cap.calls()
    assert calls and all(c["internal"] == "1" for c in calls)
    assert [j["status"] for j in cap.jobs()] == ["saved"]


def test_internal_flag_makes_trigger_consolidate_pull_and_codex_exit_without_model_or_git(cap, monkeypatch):
    _install_stub(cap.bindir, "git", GIT_STUB)
    git_log = cap.tmp / "git.log"
    env = {**os.environ, "OKFMEM_INTERNAL": "1", "GIT_STUB_LOG": str(git_log)}
    payload = json.dumps({"session_id": "s1", "transcript_path": "x", "cwd": str(cap.repo)})
    runs = [
        [sys.executable, OKFMEM, "capture", "--stdin-hook", "--store", str(cap.store)],
        [sys.executable, str(ROOT / "memory_consolidate.py"), "--stdin-hook", "--store", str(cap.store)],
        [sys.executable, OKFMEM, "pull", "--store", str(cap.store)],
        [sys.executable, str(ROOT / "memory_codex.py"), "--event", "Stop", "--store", str(cap.store)],
    ]
    for argv in runs:
        r = subprocess.run(argv, input=payload, capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, (argv, r.stdout, r.stderr)
    assert cap.calls() == []
    assert not git_log.exists() or git_log.read_text() == ""
    assert cap.jobs() == []


def test_stdin_hook_with_empty_stdin_exits_zero_and_logs(cap, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert mc.main(["--stdin-hook", "--store", str(cap.store)]) == 0
    log = (cap.store / ".okfmem" / "capture" / "capture.log").read_text()
    assert "empty payload" in log
    assert cap.jobs() == []


def test_stdin_hook_payload_queues_a_job_and_starts_the_worker(cap, monkeypatch):
    path = cap.transcript([user("prefer tabs", cap.repo)])
    payload = {"session_id": "s1", "transcript_path": str(path), "cwd": str(cap.repo),
               "hook_event_name": "SessionEnd"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert mc.main(["--stdin-hook", "--store", str(cap.store)]) == 0
    [job] = cap.jobs()
    assert job["trigger"] == "SessionEnd" and job["project"] == "proj"
    assert job["upto"] == path.stat().st_size and cap.spawned == [str(cap.store)]


def test_now_for_codex_exits_3_with_identity_and_writes_no_job(cap, capsys):
    rollout = cap.tmp / "rollout.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": "c1", "cwd": str(cap.repo)}}) + "\n")
    assert mc.main(["--now", "--harness", "codex", "--transcript", str(rollout)]) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["identity"]["session"] == "c1" and out["upto"] == rollout.stat().st_size
    assert mc.main(["--now", "--harness", "codex", "--session", "missing"]) == 3
    assert json.loads(capsys.readouterr().out)["identity"] is None
    assert not (cap.store / ".okfmem" / "capture" / "jobs").exists()
    assert cap.spawned == []


def test_now_with_unwritable_jobs_dir_exits_4(cap, capsys):
    cap.transcript([user("hello", cap.repo)])
    jobs = cap.store / ".okfmem" / "capture" / "jobs"
    jobs.parent.mkdir(parents=True)
    jobs.write_text("not a directory")
    assert mc.main(["--now", "--harness", "claude-code", "--session", "s1"]) == 4
    assert "not queued" in capsys.readouterr().err
    assert cap.spawned == []


def test_two_now_calls_with_different_focus_files_create_two_jobs(cap, capsys):
    records = [user("first point", cap.repo)]
    path = cap.transcript(records)
    focus_a, focus_b = cap.tmp / "a.txt", cap.tmp / "b.txt"
    focus_a.write_text("remember A")
    focus_b.write_text("remember B")
    assert mc.main(["--now", "--harness", "claude-code", "--session", "s1", "--focus", str(focus_a)]) == 0
    first_upto = path.stat().st_size
    cap.transcript(records + [user("second point", cap.repo)])
    assert mc.main(["--now", "--harness", "claude-code", "--session", "s1", "--focus", str(focus_b)]) == 0
    jobs = cap.jobs()
    assert [(j["focus"], j["upto"]) for j in jobs] == [("remember A", first_upto),
                                                        ("remember B", path.stat().st_size)]
    assert len({j["request_id"] for j in jobs}) == 2


def test_focus_slice_goes_straight_to_capture(cap):
    path = cap.transcript([user("from now on we always pin the runner image", cap.repo)])
    cap.job(path, focus="pin the runner image")
    cap.respond([{"reply": reply(), "cost": 0.2}])
    assert mc.run_worker(str(cap.store)) == 0
    [call] = cap.calls()
    assert _flag(call["argv"], "--model") == "sonnet"
    [job] = cap.jobs()
    assert job["status"] == "captured" and job["cost_usd"] == 0.2
    result = json.loads((cap.store / ".okfmem" / "capture" / "jobs" / f"{job['request_id']}.result.json").read_text())
    assert result["reply"]["pages"][0]["slug"] == "runner-image-pin"
    assert result["end"] == path.stat().st_size
    assert cap.ledger_offset() is None  # the apply step advances it for captured chunks


def test_same_slice_without_focus_reaches_triage_first(cap):
    path = cap.transcript([user("from now on we always pin the runner image", cap.repo)])
    cap.job(path)
    cap.respond([TRIAGE_SAVE, {"reply": reply(), "cost": 0.1}])
    mc.run_worker(str(cap.store))
    assert [_flag(c["argv"], "--model") for c in cap.calls()] == ["haiku", "sonnet"]


def test_triage_skip_marks_the_job_skipped_and_advances_the_ledger(cap):
    path = cap.transcript([user("what does this function do?", cap.repo)])
    cap.job(path)
    cap.respond([TRIAGE_SKIP])
    mc.run_worker(str(cap.store))
    [job] = cap.jobs()
    assert job["status"] == "skipped" and "routine" in job["outcome"]
    assert cap.ledger_offset() == path.stat().st_size
    assert len(cap.calls()) == 1


def test_slice_without_a_user_turn_makes_no_model_call(cap):
    path = cap.transcript([assistant("done", cap.repo), tool_use(cap.repo)])
    cap.job(path)
    mc.run_worker(str(cap.store))
    assert cap.calls() == []
    [job] = cap.jobs()
    assert job["status"] == "skipped" and job["outcome"] == "skipped (floor)"
    assert cap.ledger_offset() == path.stat().st_size


def test_slice_over_cap_is_read_in_chunks_and_ledger_stops_before_unhandled_chunk(cap):
    texts = [f"chunk{i} " + "word " * 40 for i in range(3)]
    path = cap.transcript([user(t, cap.repo) for t in texts])
    first_end = len(json.dumps(user(texts[0], cap.repo)).encode()) + 1
    cap.config(cap_bytes=300)
    cap.job(path)
    cap.respond([TRIAGE_SKIP, TRIAGE_SAVE, {"reply": reply(), "cost": 0.1}])
    mc.run_worker(str(cap.store))
    calls = cap.calls()
    assert [_flag(c["argv"], "--model") for c in calls] == ["haiku", "haiku", "sonnet"]
    slices = [c["stdin"].split("## Transcript slice", 1)[1] for c in calls]
    assert "chunk0" in slices[0] and "chunk1" not in slices[0]
    assert "chunk1" in slices[1] and "chunk0" not in slices[1] and "chunk2" not in slices[1]
    assert cap.ledger_offset() == first_end
    [job] = cap.jobs()
    assert job["status"] == "captured" and first_end < job["cursor"] < path.stat().st_size


def test_chunks_beyond_the_per_run_limit_stay_queued_as_a_continuation(cap):
    texts = [f"chunk{i} " + "word " * 40 for i in range(3)]
    path = cap.transcript([user(t, cap.repo) for t in texts])
    cap.config(cap_bytes=300, max_chunks_per_job=2)
    cap.job(path)
    cap.respond([TRIAGE_SKIP])
    mc.run_worker(str(cap.store))
    assert len(cap.calls()) == 2 and cap.jobs()[0]["status"] == "queued"
    assert cap.ledger_offset() < path.stat().st_size
    mc.run_worker(str(cap.store))
    assert len(cap.calls()) == 3 and cap.jobs()[0]["status"] == "skipped"
    assert cap.ledger_offset() == path.stat().st_size


def test_capture_argv_is_tool_less_hookless_and_budgeted_after_triage(cap):
    path = cap.transcript([user("we decided to drop the cache", cap.repo)])
    cap.job(path)
    cap.respond([TRIAGE_SAVE, {"reply": reply(), "cost": 0.1}])
    mc.run_worker(str(cap.store))
    triage, capture = cap.calls()
    argv = capture["argv"]
    assert _flag(argv, "--tools") == ""
    assert _flag(argv, "--model") == "sonnet"
    assert "--no-session-persistence" in argv and "--json-schema" in argv
    assert json.loads(_flag(argv, "--json-schema")) == mc.CAPTURE_SCHEMA
    settings = _flag(argv, "--settings")
    assert '"disableAllHooks": true' in settings and json.loads(settings) == {"disableAllHooks": True}
    assert float(_flag(argv, "--max-budget-usd")) == pytest.approx(0.50 - 0.0123)
    assert float(_flag(triage["argv"], "--max-budget-usd")) == pytest.approx(0.50)
    assert "--bare" not in argv and "--bare" not in triage["argv"]
    assert _flag(argv, "--setting-sources") == "project" and "--strict-mcp-config" in argv
    capture_dir = os.path.realpath(cap.store / ".okfmem" / "capture")
    assert os.path.realpath(capture["cwd"]).startswith(capture_dir) and capture["cwd_files"] == []
    assert not (cap.store / ".okfmem" / "capture" / "work" / cap.jobs()[0]["request_id"]).exists()


def _existing_page(cap):
    text = ("---\nname: Runner image pinning\ndescription: pin the runner image\ntype: project\n"
            "labels: [repo:proj]\n---\n\n# Runner image pinning\n\n" + "runner image detail\n" * 20)
    (cap.store / "pages" / "runner-image.md").write_text(text)


@pytest.mark.parametrize("bad", ["traversal", "excerpt", "ninth", "big"])
def test_invalid_capture_reply_is_rejected_whole_and_nothing_is_written(cap, bad):
    _existing_page(cap)
    cap.config(pages_cap_bytes=10)
    before = {p.name: p.read_bytes() for p in (cap.store / "pages").iterdir()}
    replies = {
        "traversal": reply(page(), page(slug="../x")),
        "excerpt": reply(page(), page(slug="runner-image", action="update")),
        "ninth": reply(*[page(slug=f"p{i}") for i in range(9)]),
        "big": reply(page(), page(slug="huge", body="x" * 30 * 1024)),
    }
    path = cap.transcript([user("the runner image pinning rule changed", cap.repo)])
    cap.job(path, focus="runner image pinning")
    cap.respond([{"reply": replies[bad], "cost": 0.1}])
    mc.run_worker(str(cap.store))
    assert len(cap.calls()) == 1
    [job] = cap.jobs()
    assert job["status"] == "queued" and job["attempts"] == 1 and "rejected" in job["error"]
    assert not list((cap.store / ".okfmem" / "capture" / "jobs").glob("*.result.json"))
    assert {p.name: p.read_bytes() for p in (cap.store / "pages").iterdir()} == before
    assert cap.ledger_offset() is None
    if bad == "excerpt":
        assert "excerpt" in job["error"]


def test_capture_input_lists_non_candidate_slugs_and_create_of_one_is_rejected(cap):
    unrelated = ("---\nname: Zebra notes\ndescription: zebra\ntype: project\n"
                 "labels: [repo:other]\n---\n\n# Zebra notes\n\nzebra\n")
    (cap.store / "pages" / "runner-image-pin.md").write_text(unrelated)
    path = cap.transcript([user("we decided to drop the cache", cap.repo)])
    cap.job(path, focus="drop the cache")
    cap.respond([{"reply": reply(page(slug="runner-image-pin")), "cost": 0.1}])
    mc.run_worker(str(cap.store))
    [call] = cap.calls()
    candidates, others = call["stdin"].split("## Candidate pages", 1)[1].split(
        "## Other existing page slugs", 1)
    assert "runner-image-pin" not in candidates
    assert "runner-image-pin" in others.split("## Transcript slice", 1)[0]
    [job] = cap.jobs()
    assert job["status"] == "queued" and "overwrite an existing page" in job["error"]
    assert (cap.store / "pages" / "runner-image-pin.md").read_text() == unrelated


def test_update_of_a_candidate_given_in_full_is_accepted_with_its_hash(cap):
    _existing_page(cap)
    path = cap.transcript([user("the runner image pinning rule changed", cap.repo)])
    cap.job(path, focus="runner image pinning")
    cap.respond([{"reply": reply(page(slug="runner-image", action="update")), "cost": 0.1}])
    mc.run_worker(str(cap.store))
    [job] = cap.jobs()
    assert job["status"] == "captured", job.get("error")
    result = json.loads(next((cap.store / ".okfmem" / "capture" / "jobs").glob("*.result.json")).read_text())
    assert result["inputs"]["pages"]["runner-image"]["excerpt_only"] is False
    assert "runner-image" in cap.calls()[0]["stdin"]


def _lock(cap, pid, heartbeat=None):
    lock = cap.store / ".okfmem" / "capture" / "lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    beat = heartbeat or datetime.now(timezone.utc).isoformat()
    lock.write_text(json.dumps({"pid": pid, "host": mc.HOST, "started": beat, "heartbeat": beat}))
    return lock


def test_held_lock_with_fresh_heartbeat_leaves_jobs_intact(cap):
    path = cap.transcript([user("decided X", cap.repo)])
    cap.job(path, focus="X")
    lock = _lock(cap, os.getpid())
    cap.respond([{"reply": reply(), "cost": 0.1}])
    assert mc.run_worker(str(cap.store)) == 0
    assert cap.calls() == [] and cap.jobs()[0]["status"] == "queued"
    assert lock.exists()


def test_lock_owned_by_a_dead_pid_is_taken_over(cap):
    path = cap.transcript([user("decided X", cap.repo)])
    cap.job(path, focus="X")
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    lock = _lock(cap, proc.pid)
    cap.respond([{"reply": reply(), "cost": 0.1}])
    mc.run_worker(str(cap.store))
    assert len(cap.calls()) == 1 and cap.jobs()[0]["status"] == "captured"
    assert not lock.exists()


def test_lock_with_an_old_heartbeat_is_stale_even_for_a_live_pid(cap):
    old = datetime.fromtimestamp(time.time() - mc.LOCK_STALE_S - 60, timezone.utc).isoformat()
    assert mc.lock_stale({"pid": os.getpid(), "host": mc.HOST, "heartbeat": old}, time.time())
    now = datetime.now(timezone.utc).isoformat()
    assert not mc.lock_stale({"pid": os.getpid(), "host": mc.HOST, "heartbeat": now}, time.time())


def test_job_failing_three_times_is_marked_failed_and_not_retried(cap):
    path = cap.transcript([user("decided X", cap.repo)])
    cap.job(path, focus="X")
    cap.respond([{"subtype": "error_during_execution", "exit": 1, "cost": 0.01}])
    for _ in range(3):
        mc.run_worker(str(cap.store))
    [job] = cap.jobs()
    assert job["status"] == "failed" and job["attempts"] == 3
    mc.run_worker(str(cap.store))
    assert len(cap.calls()) == 3
    assert any("failed 1" in line and "error_during_execution" in line
               for line in mc.status_lines(str(cap.store)))


def test_job_budget_is_shared_by_triage_and_capture_across_attempts(cap):
    path = cap.transcript([user("decided X", cap.repo)])
    cap.job(path)
    cap.respond([{**TRIAGE_SAVE, "cost": 0.495}])
    mc.run_worker(str(cap.store))
    [job] = cap.jobs()
    assert job["status"] == "failed" and "budget" in job["error"]
    assert len(cap.calls()) == 1


def test_status_lines_show_per_project_counts_and_latest_outcome(cap, capsys):
    assert mc.status_lines(str(cap.store)) == ["capture   never run"]
    path = cap.transcript([assistant("done", cap.repo)])
    cap.job(path)
    cap.job(path)
    mc.run_worker(str(cap.store))
    cap.job(path)
    lines = mc.status_lines(str(cap.store))
    assert len(lines) == 1
    assert lines[0].startswith("capture   proj: queued 1, failed 0; last skipped")
    mi.cmd_status(str(cap.store))
    assert "capture   proj: queued 1, failed 0; last skipped" in capsys.readouterr().out


def test_catch_up_queues_idle_uncaptured_sessions_once(cap):
    path = cap.transcript([user("decided X", cap.repo)])
    old = time.time() - mc.CATCHUP_IDLE_S - 60
    os.utime(path, (old, old))
    ledger = cap.store / ".okfmem" / "capture" / "ledger.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"catchup_floor": "1970-01-01T00:00:00+00:00", "entries": {}}))
    assert mc.main(["--catch-up", "--store", str(cap.store)]) == 0
    assert mc.main(["--catch-up", "--store", str(cap.store)]) == 0
    [job] = cap.jobs()
    assert job["trigger"] == "catch-up" and job["ledger_key"] == "claude-code:s1"
    assert cap.spawned == [str(cap.store)]


def test_catch_up_resolves_project_from_cwd_after_claude_metadata(cap):
    path = cap.transcript([
        {"type": "session-owner", "sessionId": "s1"},
        {"type": "queue-operation", "sessionId": "s1"},
        {"type": "attachment", "sessionId": "s1", "cwd": str(cap.repo)},
    ])
    old = time.time() - mc.CATCHUP_IDLE_S - 60
    os.utime(path, (old, old))
    ledger = cap.store / ".okfmem" / "capture" / "ledger.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"catchup_floor": "1970-01-01T00:00:00+00:00", "entries": {}}))

    assert mc.main(["--catch-up", "--store", str(cap.store)]) == 0

    [job] = cap.jobs()
    assert job["trigger"] == "catch-up" and job["project"] == "proj"
    assert job["cwd"] == str(cap.repo)
    assert cap.spawned == [str(cap.store)]
    capture_log = cap.store / ".okfmem" / "capture" / "capture.log"
    assert "no project resolves" not in capture_log.read_text(encoding="utf-8")


def test_job_left_running_by_a_dead_worker_counts_as_an_attempt(cap):
    path = cap.transcript([user("decided X", cap.repo)])
    job, job_path = cap.job(path, focus="X")
    Path(job_path).write_text(json.dumps({**job, "status": "running", "attempts": 2}))
    cap.respond([{"reply": reply(), "cost": 0.1}])
    mc.run_worker(str(cap.store))
    assert cap.calls() == []
    [job] = cap.jobs()
    assert job["status"] == "failed" and job["attempts"] == 3


def test_catch_up_does_not_requeue_a_session_whose_job_failed(cap):
    path = cap.transcript([user("decided X", cap.repo)])
    job, job_path = cap.job(path)
    Path(job_path).write_text(json.dumps({**job, "status": "failed", "attempts": 3}))
    old = time.time() - mc.CATCHUP_IDLE_S - 60
    os.utime(path, (old, old))
    ledger = cap.store / ".okfmem" / "capture" / "ledger.json"
    ledger.write_text(json.dumps({"catchup_floor": "1970-01-01T00:00:00+00:00", "entries": {}}))
    assert mc.main(["--catch-up", "--store", str(cap.store)]) == 0
    assert len(cap.jobs()) == 1 and cap.spawned == []


def test_worker_rescans_for_jobs_queued_while_it_held_the_lock(cap, monkeypatch):
    path = cap.transcript([assistant("done", cap.repo)])
    cap.job(path)
    real = mc.process_job
    late = []

    def process_and_enqueue(*args):
        if not late:
            late.append(cap.job(path))
        return real(*args)

    monkeypatch.setattr(mc, "process_job", process_and_enqueue)
    mc.run_worker(str(cap.store))
    assert [j["status"] for j in cap.jobs()] == ["skipped", "skipped"]


def test_read_since_upto_stops_before_later_records(cap):
    records = [user("one", cap.repo), user("two", cap.repo)]
    path = cap.transcript(records)
    first_end = len(json.dumps(records[0]).encode()) + 1
    turns, end, _ = mt.read_since("claude-code", path, 0, 10_000, upto=first_end)
    assert [t["text"] for t in turns] == ["one"] and end == first_end


def _result(cap, job):
    return json.loads((cap.store / ".okfmem" / "capture" / "jobs" / f"{job['request_id']}.result.json").read_text())


def test_one_turn_no_tool_session_with_a_focus_file_reaches_the_capture_call(cap, capsys):
    cap.transcript([user("keep this: we always pin the runner image", cap.repo)])
    focus = cap.tmp / "focus.md"
    focus.write_text("- pin the runner image: drift broke CI twice\n", encoding="utf-8")
    argv = ["--now", "--harness", "claude-code", "--session", "s1", "--cwd", str(cap.repo),
            "--focus", str(focus), "--trigger", "manual", "--store", str(cap.store)]
    assert mc.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "queued"
    assert cap.spawned == [str(cap.store)]
    focus.write_text("- a later save's notes\n", encoding="utf-8")
    cap.respond([{"reply": reply(), "cost": 0.1}])
    mc.run_worker(str(cap.store))
    [call] = cap.calls()
    assert _flag(call["argv"], "--model") == "sonnet"
    assert "drift broke CI twice" in call["stdin"] and "a later save" not in call["stdin"]
    [job] = cap.jobs()
    assert job["status"] == "captured" and job["trigger"] == "manual"


def test_record_after_a_fallback_save_makes_a_later_now_start_there(cap, capsys):
    first = [user("we decided to drop the cache because it hid stale reads", cap.repo)]
    path = cap.transcript(first)
    upto = path.stat().st_size
    assert mc.main(["--record", "--harness", "claude-code", "--session", "s1", "--upto", str(upto),
                    "--outcome", "saved", "--store", str(cap.store)]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "recorded", "ledger_key": "claude-code:s1",
                                                   "offset": upto}
    assert cap.ledger_offset() == upto
    cap.transcript(first + [user("from now on prefer tabs in the Makefile", cap.repo)])
    assert mc.main(["--now", "--harness", "claude-code", "--session", "s1"]) == 0
    cap.respond([TRIAGE_SAVE, {"reply": reply(), "cost": 0.1}])
    mc.run_worker(str(cap.store))
    slices = [c["stdin"].split("## Transcript slice", 1)[1] for c in cap.calls()]
    assert len(slices) == 2
    assert all("prefer tabs" in s and "drop the cache" not in s for s in slices)
    [job] = cap.jobs()
    assert _result(cap, job)["start"] == upto


def test_record_refuses_an_offset_inside_a_record_and_never_moves_the_ledger_back(cap, capsys):
    records = [user("one", cap.repo), user("two", cap.repo)]
    path = cap.transcript(records)
    first_end, size = len(json.dumps(records[0]).encode()) + 1, path.stat().st_size
    record = ["--record", "--harness", "claude-code", "--session", "s1", "--upto"]
    assert mc.main(record + [str(size)]) == 0
    assert mc.main(record + [str(first_end)]) == 0
    assert cap.ledger_offset() == size
    for bad in (first_end + 2, size + 10, 0):
        assert mc.main(record + [str(bad)]) == 4
    assert "not recorded" in capsys.readouterr().err
    assert mc.main(["--record", "--session", "s1", "--upto", str(size)]) == 4
    assert cap.ledger_offset() == size


def test_record_works_for_codex_which_has_no_runner(cap, capsys):
    rollout = cap.tmp / "rollout.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": "c1", "cwd": str(cap.repo)}}) + "\n")
    assert mc.main(["--now", "--harness", "codex", "--transcript", str(rollout)]) == 3
    upto = json.loads(capsys.readouterr().out)["upto"]
    assert mc.main(["--record", "--harness", "codex", "--transcript", str(rollout), "--upto", str(upto)]) == 0
    assert cap.ledger_offset("codex:c1") == upto
