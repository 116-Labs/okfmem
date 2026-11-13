import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import memory_capture as mc
import memory_sync as ms

pytestmark = pytest.mark.os_real

PAGE_TEXT = ("---\nname: Runner image\ndescription: pin it\ntype: project\nlabels: [repo:proj]\n"
             "created: 2026-01-01\nlast_accessed: 2026-02-02\naccess_count: 7\n---\n\n"
             + "".join(f"line {i}\n" for i in range(300)))
STATE_OLD = "---\ntype: state\nproject: proj\nmodified: 2026-01-01T00:00:00Z\n---\n\n# STATE\n\nold\n"
STATE = {k: f"{k} text" for k in mc.STATE_SECTIONS}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def page(slug="new-page", action="create", body="# New\n\nfact\n", labels=("repo:proj",)):
    return {"slug": slug, "action": action, "name": "New page", "description": "a: hook",
            "type": "project", "labels": list(labels), "body": body}


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = tmp_path / "store"
    (store / "pages").mkdir(parents=True)
    (store / "state").mkdir()
    git(store, "init", "-q")
    for k, v in (("user.email", "t@e.st"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(store, "config", k, v)
    (store / ".gitignore").write_text(".okfmem/\nviews/\n.okfmem-sync.lock\n")
    (store / "pages" / "runner.md").write_bytes(PAGE_TEXT.encode())
    (store / "state" / "proj.md").write_text(STATE_OLD)
    git(store, "add", "-A")
    git(store, "commit", "-qm", "seed")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "user", "sessionId": "s1"}) + "\n")
    os.utime(transcript, (time.time() + 100, time.time() + 100))
    monkeypatch.setenv("OKFMEM_STORE", str(store))
    n = SimpleNamespace(store=str(store), seq=0)

    def make(pages=(), state=None, state_sha="__current__", page_shas=None):
        n.seq += 1
        rid = f"r{n.seq}"
        os.makedirs(mc.jobs_dir(n.store), exist_ok=True)
        cur = (store / "state" / "proj.md").read_bytes().decode()
        job = {"request_id": rid, "project": "proj", "ledger_key": "claude-code:s1", "session": "s1",
               "transcript": str(transcript), "fingerprint": "fp", "cwd": None, "upto": 10,
               "status": "captured", "attempts": 0}
        shas = page_shas if page_shas is not None else {
            "runner": sha(PAGE_TEXT)}
        result = {"request_id": rid, "project": "proj", "start": 0, "end": 10, "upto": 10,
                  "reply": {"pages": list(pages), "state": state, "summary": "s"},
                  "inputs": {"pages": {k: {"sha256": v} for k, v in shas.items()},
                             "state": {"path": "state/proj.md",
                                       "sha256": sha(cur) if state_sha == "__current__" else state_sha,
                                       "modified": None}}}
        mc._write_json(os.path.join(mc.jobs_dir(n.store), f"{rid}.json"), job)
        mc._write_json(os.path.join(mc.jobs_dir(n.store), f"{rid}.result.json"), result)
        return job

    n.make = make
    n.read = lambda rel: (store / rel).read_text()
    n.commits = lambda: git(store, "log", "--format=%s").splitlines()
    n.ledger = lambda: json.loads((store / ".okfmem" / "capture" / "ledger.json").read_text())["entries"]
    monkeypatch.setattr(mc.mt, "ledger_get", lambda s, k: None)
    return n


def ledger_offset(env):
    return env.ledger()["claude-code:s1"]["offset"]


def test_valid_result_writes_pages_and_state_in_one_capture_commit_without_unrelated_files(env):
    (Path(env.store) / "stray.txt").write_text("x")
    job = env.make([page()], STATE)
    mc.apply_result(env.store, job)
    assert env.commits()[0].startswith("capture(proj): 1 page, STATE") and len(env.commits()) == 2
    assert "stray.txt" not in git(env.store, "show", "--name-only", "--format=", "HEAD")
    assert "a: hook" in env.read("pages/new-page.md") and "summary text" in env.read("state/proj.md")
    assert "stray.txt" in git(env.store, "status", "--porcelain")
    assert ledger_offset(env) == 10 and job["status"] == "saved" and job["state"] == "written"


def test_crash_after_written_and_after_committed_replays_same_content_without_a_second_commit(env, monkeypatch):
    job = env.make([page()], STATE)
    real = ms.sync_store

    def die_after_commit(*a, **k):
        real(*a, **k)
        raise KeyboardInterrupt

    monkeypatch.setattr(ms, "sync_store", die_after_commit)
    with pytest.raises(KeyboardInterrupt):
        mc.apply_result(env.store, job)
    journal = json.load(open(mc._journal_path(env.store, "r1", 0, 10)))
    assert journal["phase"] == "written"
    body = env.read("pages/new-page.md")
    monkeypatch.setattr(ms, "sync_store", real)
    mc.apply_result(env.store, mc.iter_jobs(env.store)[0][1])
    assert env.read("pages/new-page.md") == body
    assert len([c for c in env.commits() if c.startswith("capture(")]) == 1
    assert ledger_offset(env) == 10
    # a crash after `committed` only advances the ledger
    journal = json.load(open(mc._journal_path(env.store, "r1", 0, 10)))
    assert journal["phase"] == "recorded"
    mc._save_journal(env.store, journal, "committed")
    os.remove(os.path.join(env.store, ".okfmem", "capture", "ledger.json"))
    mc.apply_result(env.store, mc.iter_jobs(env.store)[0][1])
    assert ledger_offset(env) == 10 and len([c for c in env.commits() if c.startswith("capture(")]) == 1


def test_second_result_for_one_job_uses_its_own_journal_and_advances_the_ledger(env):
    job = env.make([page("first-page")])
    job["upto"] = 20
    mc._write_json(os.path.join(mc.jobs_dir(env.store), "r1.json"), job)
    mc.apply_result(env.store, job)
    result_path = os.path.join(mc.jobs_dir(env.store), "r1.result.json")
    result = json.load(open(result_path))
    result.update(start=10, end=20, upto=20)
    result["reply"] = {"pages": [page("second-page")], "state": None, "summary": "second"}
    result["inputs"]["pages"] = {}
    mc._write_json(result_path, result)
    job.update(status="captured", cursor=10)
    mc._write_json(os.path.join(mc.jobs_dir(env.store), "r1.json"), job)
    mc.apply_result(env.store, job)
    assert "New page" in env.read("pages/second-page.md")
    assert ledger_offset(env) == 20
    assert len([c for c in env.commits() if c.startswith("capture(")]) == 2


def test_apply_write_or_commit_failures_leave_the_job_visible_and_offset_unchanged(env, monkeypatch):
    job = env.make([page()])
    write_files = mc._write_files
    monkeypatch.setattr(mc, "_write_files", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    mc.apply_result(env.store, job)
    assert job["status"] == "failed" and not os.path.exists(os.path.join(
        env.store, ".okfmem", "capture", "ledger.json"))

    job = env.make([page("retry-page")])

    def no_commit(store, message, paths, prepare, **kwargs):
        prepare(store)
        return {"committed": False, "pushed": False, "reason": "index.lock", "push_error": None}

    monkeypatch.setattr(mc, "_write_files", write_files)
    monkeypatch.setattr(ms, "sync_store", no_commit)
    mc.apply_result(env.store, job)
    assert job["status"] == "captured" and not os.path.exists(os.path.join(
        env.store, ".okfmem", "capture", "ledger.json"))


def test_update_that_changes_nothing_is_skipped_and_advances_the_ledger(env, monkeypatch):
    job = env.make([page("runner", "update")])
    real = ms.sync_store

    def noop_sync(store, message, **kwargs):
        kwargs["prepare"](store)
        return {"committed": False, "pushed": False, "reason": ms.NO_CHANGES_REASON, "push_error": None}

    monkeypatch.setattr(ms, "sync_store", noop_sync)
    mc.apply_result(env.store, job)
    monkeypatch.setattr(ms, "sync_store", real)
    assert job["status"] == "skipped" and "nothing to write" in job["outcome"]
    assert ledger_offset(env) == 10 and env.commits() == ["seed"]


def test_failed_git_add_defers_the_apply_instead_of_recording_a_skip(env, monkeypatch):
    job = env.make([page("new-page")])
    real_git = ms._git

    def git(store, *args, **kwargs):
        if args and args[0] == "add":
            return SimpleNamespace(returncode=128, stdout="", stderr="index.lock exists")
        return real_git(store, *args, **kwargs)

    monkeypatch.setattr(ms, "_git", git)
    mc.apply_result(env.store, job)
    assert job["status"] == "captured"
    assert not os.path.exists(os.path.join(env.store, ".okfmem", "capture", "ledger.json"))
    assert env.commits() == ["seed"]


def test_conflicted_dirty_state_is_not_staged_or_committed(env):
    state = Path(env.store) / "state" / "proj.md"
    state.write_text(STATE_OLD + "local edit\n")
    job = env.make([], STATE, state_sha="0" * 64)
    mc.apply_result(env.store, job)
    assert env.commits() == ["seed"]
    assert state.read_text().endswith("local edit\n")
    assert job["status"] == "skipped" and job["state"] == "conflict"


def test_drain_replays_a_captured_job_and_saved_jobs_stay_saved(env, monkeypatch):
    job = env.make([page()])
    monkeypatch.setattr(mc, "heartbeat", lambda *_: True)
    mc.drain(env.store, {}, "owner", set(), 0.0)
    assert mc.iter_jobs(env.store)[0][1]["status"] == "saved" and ledger_offset(env) == 10
    resumed = {"request_id": "saved", "harness": "claude-code", "transcript": job["transcript"],
               "fingerprint": "fp", "ledger_key": "claude-code:s1", "upto": 0,
               "saves": 1, "outcome": "saved first chunk"}
    monkeypatch.setattr(mc.mt, "transcript_identity", lambda *_: {"fingerprint": "fp"})
    mc._run_job(env.store, {}, "owner", resumed, SimpleNamespace())
    assert resumed["status"] == "saved"


def test_update_keeps_every_unchanged_line_and_decay_frontmatter(env):
    body = "# Runner image\n\n" + "".join(f"line {i}\n" for i in range(300)).replace("line 150\n", "CHANGED\n")
    job = env.make([page("runner", "update", body, labels=("repo:proj", "topic:ci"))])
    mc.apply_result(env.store, job)
    text = env.read("pages/runner.md")
    assert "CHANGED" in text and "line 149\n" in text and "line 299\n" in text
    assert "last_accessed: 2026-02-02" in text and "access_count: 7" in text
    assert "labels: [repo:proj, topic:ci]" in text and text.count("---") == 2


def test_update_whose_page_changed_before_apply_is_a_per_page_conflict_and_other_pages_apply(env):
    job = env.make([page("runner", "update"), page()])
    (Path(env.store) / "pages" / "runner.md").write_text(PAGE_TEXT + "edited\n")
    mc.apply_result(env.store, job)
    assert env.read("pages/runner.md") == PAGE_TEXT + "edited\n"
    assert "New page" in env.read("pages/new-page.md") and "runner: page changed" in job["outcome"]


def test_symlinked_page_target_is_rejected_and_the_outside_file_is_untouched(env, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    os.symlink(outside, os.path.join(env.store, "pages", "evil.md"))
    job = env.make([page("evil")])
    mc.apply_result(env.store, job)
    assert outside.read_text() == "secret" and "evil: target is a symlink" in job["outcome"]
    assert not [c for c in env.commits() if c.startswith("capture(")]


def test_session_ended_before_the_state_was_modified_is_superseded_but_pages_apply(env):
    (Path(env.store) / "state" / "proj.md").write_text(
        STATE_OLD.replace("2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z"))
    git(env.store, "commit", "-qam", "newer state")
    job = env.make([page()], STATE)
    old = time.time() - 1000
    os.utime(job["transcript"], (old, old))
    mc.apply_result(env.store, job)
    assert job["state"] == "superseded" and "2099" in env.read("state/proj.md")
    assert "New page" in env.read("pages/new-page.md")


def test_state_changed_after_the_model_read_it_is_a_conflict_and_not_overwritten(env):
    job = env.make([page()], STATE, state_sha="0" * 64)
    mc.apply_result(env.store, job)
    assert job["state"] == "conflict" and env.read("state/proj.md") == STATE_OLD
    assert "New page" in env.read("pages/new-page.md")


def test_missing_session_baseline_still_applies_pages_and_state(env):
    job = env.make([page()], STATE)
    assert not os.path.exists(os.path.join(env.store, ".okfmem", "sessions"))
    mc.apply_result(env.store, job)
    assert job["state"] == "written" and "New page" in env.read("pages/new-page.md")


def test_oversize_state_is_not_written(env):
    big = {**STATE, "summary": "x" * 9000}
    job = env.make([page()], big)
    mc.apply_result(env.store, job)
    assert job["state"] == "conflict" and env.read("state/proj.md") == STATE_OLD


def test_unreachable_remote_leaves_the_job_saved_and_a_later_sync_pushes_without_a_model(env, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    git(env.store, "remote", "add", "origin", str(origin))
    git(env.store, "push", "-q", "-u", "origin", "HEAD")
    git(env.store, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    job = env.make([page()])
    mc.apply_result(env.store, job)
    assert job["status"] == "saved" and job["unpushed"] is True
    assert "unpushed: 1 commit" in "\n".join(mc.status_lines(env.store))
    git(env.store, "remote", "set-url", "origin", str(origin))
    assert ms.sync_store(env.store, "sync")["pushed"] is True
    assert git(env.store, "rev-list", "--count", "@{u}..HEAD").strip() == "0"


def test_result_with_nothing_to_write_is_skipped_and_advances_the_ledger(env):
    job = env.make([], None)
    mc.apply_result(env.store, job)
    assert job["status"] == "skipped" and ledger_offset(env) == 10 and len(env.commits()) == 1


def test_busy_store_defers_then_fails_the_job_after_the_attempt_limit(env, monkeypatch):
    monkeypatch.setattr(ms, "_acquire_lock", lambda store, timeout=60: None)
    job = env.make([page()])
    for _ in range(mc.MAX_ATTEMPTS - 1):
        mc.apply_result(env.store, job)
        assert job["status"] == "captured"
    mc.apply_result(env.store, job)
    assert job["status"] == "failed" and not os.path.exists(os.path.join(env.store, "pages", "new-page.md"))
    assert not os.path.exists(os.path.join(env.store, ".okfmem", "capture", "ledger.json"))


def test_sync_store_paths_stages_only_the_given_paths_and_default_callers_are_unchanged(env):
    root = Path(env.store)
    (root / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")
    res = ms.sync_store(env.store, lambda: "only a", paths=["a.txt"], do_push=False)
    assert res["committed"] and git(env.store, "show", "--name-only", "--format=", "HEAD").split() == ["a.txt"]
    assert "b.txt" in git(env.store, "status", "--porcelain")
    assert ms.sync_store(env.store, "all", do_push=False)["committed"]
    assert git(env.store, "show", "--name-only", "--format=", "HEAD").split() == ["b.txt"]
