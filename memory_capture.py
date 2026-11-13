#!/usr/bin/env python3
"""okfmem capture — queue a session for background capture, and the worker that runs it.

    okfmem capture --stdin-hook [--harness H] [--trigger NAME] [--store PATH]
    okfmem capture --now --harness H [--session ID] [--transcript PATH] [--cwd DIR]
                   [--focus FILE] [--trigger NAME] [--store PATH]
    okfmem capture --catch-up [--store PATH]
    okfmem capture --record --harness H [--session ID] [--transcript PATH] --upto N
                   [--outcome saved|skipped] [--store PATH]
    okfmem capture --worker [--store PATH]        # internal: started detached by a trigger

A trigger writes an immutable job to <store>/.okfmem/capture/jobs/ and starts a
detached worker. The worker reads the job's transcript slice in bounded chunks,
triages and captures each on a cheap model that has no tools, validates the JSON
reply whole, and writes jobs/<id>.result.json for the apply step. It never writes
pages, state or git itself.

Exit codes: --stdin-hook and --catch-up always exit 0 and log to
<store>/.okfmem/capture/capture.log. --now exits 0 once the job is on disk, 3 when
the harness has no runner (identity JSON on stdout, no job written), 4 on any other
enqueue failure (reason on stderr). --record marks a session covered up to N after a
foreground save, so a later job starts there; it exits 0 when recorded, 4 otherwise.
Store location: --store, else $OKFMEM_STORE, else ~/okfmem-store.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memory_find as mf  # noqa: E402
import memory_transcript as mt  # noqa: E402

DEFAULTS = {
    "triage_model": "haiku",
    "capture_model": "sonnet",
    "cap_bytes": 200_000,
    "pages_cap_bytes": 60_000,
    "budget_usd": 0.50,
    "run_budget_usd": 2.00,
    "max_chunks_per_job": 3,
}
MAX_ATTEMPTS = 3
# Above two 600 s model calls, so a live worker between heartbeats is never stale.
LOCK_STALE_S = 30 * 60
RUNNER_TIMEOUT_S = 600
MIN_CALL_USD = 0.01
MAX_PAGES = 8
MAX_BODY_BYTES = 20 * 1024
EXCERPT_LINES = 40
CATCHUP_IDLE_S = 600
CATCHUP_MAX_JOBS = 3
LOG_MAX_BYTES = 1_000_000
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
SESSION_RE = re.compile(r"[A-Za-z0-9._-]{1,200}")
PAGE_TYPES = ("user", "feedback", "project", "reference")
PAGE_KEYS = ("slug", "action", "name", "description", "type", "labels", "body")
STATE_SECTIONS = ("summary", "left_off", "next_steps", "decisions", "blockers", "goal")
PENDING = ("queued", "running")
HOST = socket.gethostname()

# Runners are keyed by model backend, independent of the transcript's harness. A
# backend for another harness's transcripts must be chosen explicitly, never here.
BACKEND_FOR_HARNESS = {"claude-code": "claude"}

TRIAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "reason"],
    "properties": {
        "decision": {"type": "string", "enum": ["save", "skip"]},
        "reason": {"type": "string"},
    },
}
# No regex or shell metacharacters in either schema: on Windows the argv passes
# through a `claude.cmd` shim. The engine validates every field itself anyway.
CAPTURE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pages", "state", "summary"],
    "properties": {
        "pages": {
            "type": "array",
            "maxItems": MAX_PAGES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(PAGE_KEYS),
                "properties": {
                    "slug": {"type": "string"},
                    "action": {"type": "string", "enum": ["create", "update"]},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "type": {"type": "string", "enum": list(PAGE_TYPES)},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "body": {"type": "string"},
                },
            },
        },
        "state": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(STATE_SECTIONS),
                    "properties": {k: {"type": "string"} for k in STATE_SECTIONS},
                },
            ]
        },
        "summary": {"type": "string"},
    },
}


class CaptureError(Exception):
    """A job attempt failed; `cost` is what the failed attempt still spent."""

    def __init__(self, message, cost=0.0):
        super().__init__(message)
        self.cost = cost


class RunnerError(CaptureError):
    pass


class Rejected(CaptureError):
    pass


class JobBudgetExhausted(CaptureError):
    pass


class RunBudgetReached(Exception):
    pass


class EnqueueError(Exception):
    pass


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_store(flag=None):
    raw = flag or os.environ.get("OKFMEM_STORE") or os.path.expanduser("~/okfmem-store")
    return os.path.abspath(os.path.expanduser(raw))


def capture_dir(store):
    return os.path.join(store, ".okfmem", "capture")


def jobs_dir(store):
    return os.path.join(capture_dir(store), "jobs")


def live_dir(store):
    return os.path.join(capture_dir(store), "live")


def skipped_unresolvable_path(store):
    return os.path.join(capture_dir(store), "skipped-unresolvable.json")


def record_unresolvable(store, key, cwd):
    path = skipped_unresolvable_path(store)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {"entries": {}}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        data = {"entries": {}}
    entries = data.setdefault("entries", {})
    if key in entries:
        return
    project = os.path.basename(os.path.normpath(cwd)) if isinstance(cwd, str) and cwd else "unknown"
    entries[key] = {"project": project, "updated": _utc()}
    _write_json(path, data)


def update_live_record(store, session, remove=False):
    if not isinstance(session, str) or not SESSION_RE.fullmatch(session):
        return
    path = os.path.join(live_dir(store), f"{session}.json")
    if remove:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(live_dir(store), exist_ok=True)
    _write_json(path, {"pid": os.getppid(), "host": HOST, "started": _utc()})


def _live_session(store, session):
    if not isinstance(session, str) or not SESSION_RE.fullmatch(session):
        return False
    path = os.path.join(live_dir(store), f"{session}.json")
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return False
    if isinstance(record, dict) and record.get("host") == HOST and _pid_alive(record.get("pid")):
        return True
    try:
        os.remove(path)
    except OSError:
        pass
    return False


def log(store, msg):
    try:
        os.makedirs(capture_dir(store), exist_ok=True)
        path = os.path.join(capture_dir(store), "capture.log")
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{_utc()} [{os.getpid()}] {msg}\n")
    except OSError:
        pass


def _write_json(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def load_config(store):
    cfg = dict(DEFAULTS)
    path = os.path.join(capture_dir(store), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as e:
        log(store, f"config ignored: {e}")
        return cfg
    if not isinstance(data, dict):
        log(store, "config ignored: not a JSON object")
        return cfg
    for key, default in DEFAULTS.items():
        value = data.get(key)
        if value is None:
            continue
        if isinstance(default, str) and isinstance(value, str) and value.strip():
            cfg[key] = value.strip()
        elif (not isinstance(default, str) and isinstance(value, (int, float))
              and not isinstance(value, bool) and value > 0):
            cfg[key] = type(default)(value)
        else:
            log(store, f"config {key} ignored: {value!r}")
    return cfg


# --- identity -----------------------------------------------------------------


def complete_end(path):
    """Offset just past the last newline-terminated record."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        while pos > 0:
            step = min(65536, pos)
            pos -= step
            fh.seek(pos)
            i = fh.read(step).rfind(b"\n")
            if i >= 0:
                return pos + i + 1
    return 0


def _session_files(harness):
    home = os.path.expanduser("~")
    if harness == "claude-code":
        root = os.path.join(home, ".claude", "projects")
        return [os.path.join(root, d) for d in _listdir(root)], "claude-code"
    root = os.path.join(os.environ.get("CODEX_HOME") or os.path.join(home, ".codex"), "sessions")
    return [root], "codex"


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def find_transcript(harness, session):
    if not SESSION_RE.fullmatch(session or ""):
        raise EnqueueError(f"invalid session id {session!r}")
    roots, kind = _session_files(harness)
    matches = []
    if kind == "claude-code":
        for d in roots:
            path = os.path.join(d, f"{session}.jsonl")
            if os.path.isfile(path) and _session_of(harness, path) == session:
                matches.append(path)
    else:
        for root in roots:
            for dirpath, _, files in os.walk(root):
                for name in files:
                    path = os.path.join(dirpath, name)
                    if name.endswith(".jsonl") and _session_of(harness, path) == session:
                        matches.append(path)
    if len(matches) != 1:
        raise EnqueueError(f"{len(matches)} {harness} transcripts match session {session}")
    return matches[0]


def _session_of(harness, path):
    try:
        return mt.transcript_identity(harness, path)["session"]
    except (OSError, ValueError):
        return None


def _project_for(store, cwd):
    import memory_init as mi

    try:
        return mi.resolve_project(store, cwd=cwd).get("repo")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def resolve_identity(store, harness, session=None, transcript=None, cwd=None):
    """(identity, upto) for a session; project comes from the recorded cwd (#183)."""
    if harness not in ("claude-code", "codex"):
        raise EnqueueError(f"no transcript reader for harness {harness!r}")
    if not transcript:
        if not session:
            raise EnqueueError("give --session or --transcript")
        transcript = find_transcript(harness, session)
    transcript = os.path.abspath(transcript)
    try:
        ident = mt.transcript_identity(harness, transcript)
        upto = complete_end(transcript)
    except OSError as e:
        raise EnqueueError(f"transcript unreadable: {e}") from e
    if session and ident["session"] != session:
        raise EnqueueError(f"{transcript} records session {ident['session']}, not {session}")
    recorded = ident["cwd"]
    if isinstance(recorded, str) and os.path.isabs(recorded) and os.path.isdir(recorded):
        use_cwd = recorded
    else:
        use_cwd = cwd if cwd and os.path.isdir(cwd) else None
    identity = {
        "harness": harness,
        "session": ident["session"],
        "thread": ident["thread"],
        "cwd": use_cwd,
        "fingerprint": ident["fingerprint"],
        "transcript": transcript,
        "ledger_key": f"{harness}:{ident['thread'] or ident['session']}",
        "project": _project_for(store, use_cwd) if use_cwd else None,
    }
    return identity, upto


# --- triggers -----------------------------------------------------------------


def new_request_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def enqueue(store, identity, upto, trigger, focus, defer_until_existing=False):
    if not identity.get("project"):
        raise EnqueueError(f"no project resolves for cwd {identity.get('cwd')!r}")
    os.makedirs(jobs_dir(store), exist_ok=True)
    rid = new_request_id()
    now = _utc()
    job = {
        "schema_version": 1,
        "request_id": rid,
        "created": now,
        "updated": now,
        "harness": identity["harness"],
        "backend": BACKEND_FOR_HARNESS[identity["harness"]],
        "ledger_key": identity["ledger_key"],
        "session": identity["session"],
        "transcript": identity["transcript"],
        "fingerprint": identity["fingerprint"],
        "store": store,
        "project": identity["project"],
        "cwd": identity["cwd"],
        "trigger": trigger,
        "defer_until_existing": bool(defer_until_existing),
        "focus": focus,
        "upto": upto,
        "cursor": None,
        "status": "queued",
        "attempts": 0,
        "cost_usd": 0.0,
    }
    path = os.path.join(jobs_dir(store), f"{rid}.json")
    _write_json(path, job)
    return job, path


def spawn_worker(store):
    argv = [sys.executable, os.path.join(HERE, "okfmem"), "capture", "--worker", "--store", store]
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": {**os.environ, "OKFMEM_INTERNAL": "1"},
        "cwd": capture_dir(store),
        "close_fds": True,
    }
    if os.name == "nt":
        # A hidden console, not DETACHED_PROCESS: detached leaves each console child
        # (git, claude) to open its own visible window (#216).
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        kwargs["creationflags"] = no_window | new_group
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)


def _spawn_or_log(store):
    try:
        spawn_worker(store)
    except OSError as e:
        log(store, f"worker spawn failed, jobs stay queued: {e}")
        return False
    return True


def trigger_now(args, store):
    harness = args.harness
    if not harness:
        print("okfmem capture: --now needs --harness", file=sys.stderr)
        return 4
    if harness not in BACKEND_FOR_HARNESS:
        try:
            identity, upto = resolve_identity(store, harness, args.session, args.transcript, args.cwd)
        except EnqueueError:
            identity, upto = None, None
        print(json.dumps({"status": "no-runner", "harness": harness, "identity": identity, "upto": upto}))
        return 3
    try:
        focus = None
        if args.focus:
            with open(args.focus, encoding="utf-8") as f:
                focus = f.read()
        identity, upto = resolve_identity(store, harness, args.session, args.transcript, args.cwd)
        job, path = enqueue(store, identity, upto, args.trigger or "manual", focus)
    except (EnqueueError, OSError, ValueError) as e:
        print(f"okfmem capture: not queued: {e}", file=sys.stderr)
        log(store, f"--now not queued: {e}")
        return 4
    log(store, f"queued {job['request_id']} ({job['trigger']}) {job['ledger_key']} upto {upto}")
    if not _spawn_or_log(store):
        print("okfmem capture: queued, but the worker did not start; the next trigger runs it",
              file=sys.stderr)
    print(json.dumps({"status": "queued", "request_id": job["request_id"], "job": path, "upto": upto}))
    return 0


def _ends_a_record(path, offset):
    with open(path, "rb") as fh:
        fh.seek(offset - 1)
        return fh.read(1) == b"\n"


def record_coverage(args, store):
    """Advance the ledger after a foreground save; never moves it backwards."""
    try:
        if not args.harness:
            raise EnqueueError("--record needs --harness")
        if args.upto is None or args.upto <= 0:
            raise EnqueueError("--record needs a positive --upto")
        identity, end = resolve_identity(store, args.harness, args.session, args.transcript, args.cwd)
        if args.upto > end or not _ends_a_record(identity["transcript"], args.upto):
            raise EnqueueError(f"--upto {args.upto} is not the end of a complete record (last is {end})")
        key = identity["ledger_key"]
        entry = mt.ledger_get(store, key) or {}
        offset = int(entry.get("offset") or 0)
        if args.upto > offset:
            mt.ledger_put(store, key, **{
                **entry, "transcript": identity["transcript"], "fingerprint": identity["fingerprint"],
                "offset": args.upto, "updated": _utc(), "outcome": f"{args.outcome} (foreground)"})
            offset = args.upto
    except (EnqueueError, OSError, ValueError) as e:
        print(f"okfmem capture: not recorded: {e}", file=sys.stderr)
        log(store, f"--record not recorded: {e}")
        return 4
    log(store, f"recorded {key} covered to {offset} ({args.outcome}, foreground)")
    print(json.dumps({"status": "recorded", "ledger_key": key, "offset": offset}))
    return 0


def trigger_stdin_hook(args, store):
    try:
        raw = sys.stdin.read() if sys.stdin else ""
        if not raw.strip():
            log(store, "stdin-hook: empty payload, nothing queued")
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("hook payload is not a JSON object")
        harness = args.harness or "claude-code"
        if harness not in BACKEND_FOR_HARNESS:
            log(store, f"stdin-hook: no runner for {harness}, nothing queued")
            return 0
        trigger = args.trigger or payload.get("hook_event_name") or "stdin-hook"
        session = payload.get("session_id")
        if trigger == "SessionStart":
            try:  # a failed live-record write must not skip the sweep below
                update_live_record(store, session)
            except Exception as e:
                log(store, f"stdin-hook: live record failed: {type(e).__name__}: {e}")
        elif trigger == "SessionEnd":
            update_live_record(store, session, remove=True)
        if trigger != "SessionStart":
            identity, upto = resolve_identity(store, harness, session=payload.get("session_id"),
                                              transcript=payload.get("transcript_path") or None,
                                              cwd=payload.get("cwd"))
            job, _ = enqueue(store, identity, upto, str(trigger), None)
            log(store, f"queued {job['request_id']} ({job['trigger']}) {job['ledger_key']} upto {upto}")
            _spawn_or_log(store)
        if getattr(args, "catch_up", False):
            # Its own try: a fresh session has no transcript yet, and that must not skip the sweep.
            try:
                trigger_catch_up(args, store, current_session=session, drain_existing=True)
            except Exception as e:
                log(store, f"stdin-hook: catch-up failed: {type(e).__name__}: {e}")
    except Exception as e:  # a hook never fails or delays the session it ends
        log(store, f"stdin-hook: not queued: {type(e).__name__}: {e}")
    return 0


def trigger_catch_up(args, store, current_session=None, drain_existing=False):
    try:
        # A failed job is never retried automatically, so its session is not swept again.
        busy = {job.get("ledger_key") for _, job in iter_jobs(store)
                if job.get("status") in PENDING + ("captured", "failed")}
        pending_before_sweep = drain_existing and any(
            job.get("status") in PENDING for _, job in iter_jobs(store))
        root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
        queued = 0
        for key, path, identity in mt.uncaptured(store, "claude-code", [root], CATCHUP_IDLE_S):
            if (key in busy or queued >= CATCHUP_MAX_JOBS
                    or identity.get("session") == current_session
                    or _live_session(store, identity.get("session"))):
                continue
            try:
                identity, upto = resolve_identity(store, "claude-code", transcript=path)
                enqueue(store, identity, upto, "catch-up", None,
                        defer_until_existing=pending_before_sweep)
            except (EnqueueError, OSError, ValueError) as e:
                log(store, f"catch-up: {key} not queued: {e}")
                if isinstance(e, EnqueueError) and str(e).startswith("no project resolves for cwd"):
                    record_unresolvable(store, key, identity.get("cwd"))
                continue
            busy.add(key)
            queued += 1
        if queued or pending_before_sweep:
            if queued:
                log(store, f"catch-up: queued {queued} session(s)")
            _spawn_or_log(store)
    except Exception as e:  # a SessionStart hook never fails the session
        log(store, f"catch-up: {type(e).__name__}: {e}")
    return 0


# --- lock ---------------------------------------------------------------------


def _lock_path(store):
    return os.path.join(capture_dir(store), "lock")


def _pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _win_pid_alive(pid):
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _read_lock(path):
    try:
        mtime = os.path.getmtime(path)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError):
        try:
            return {}, os.path.getmtime(path)
        except OSError:
            return None, None
    return (data if isinstance(data, dict) else {}), mtime


def lock_stale(owner, mtime):
    try:
        beat = datetime.fromisoformat(owner["heartbeat"]).timestamp()
    except (KeyError, TypeError, ValueError):
        beat = mtime
    if time.time() - beat > LOCK_STALE_S:
        return True
    return owner.get("host") == HOST and not _pid_alive(owner.get("pid"))


def acquire_lock(store):
    """The owner record on success; None while a live worker holds the lock."""
    path = _lock_path(store)
    os.makedirs(capture_dir(store), exist_ok=True)
    for _ in range(3):
        now = _utc()
        owner = {"pid": os.getpid(), "host": HOST, "started": now, "heartbeat": now}
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            held, mtime = _read_lock(path)
            if held is None:
                continue
            if not lock_stale(held, mtime):
                return None
            aside = f"{path}.stale-{os.getpid()}"
            try:
                os.replace(path, aside)
            except FileNotFoundError:
                continue
            except OSError:
                return None
            moved, _ = _read_lock(aside)
            if moved != held:
                # Another worker took the stale lock between our read and our move.
                try:
                    os.link(aside, path)
                except OSError:
                    pass
                _remove(aside)
                return None
            _remove(aside)
            log(store, f"worker: took over a stale lock from pid {held.get('pid')} on {held.get('host')}")
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(owner, f)
        return owner
    return None


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _owns(store, owner):
    held, _ = _read_lock(_lock_path(store))
    return bool(held) and all(held.get(k) == owner[k] for k in ("pid", "host", "started"))


def heartbeat(store, owner):
    if not _owns(store, owner):
        return False
    owner["heartbeat"] = _utc()
    try:
        _write_json(_lock_path(store), owner)
    except OSError:
        return False
    return True


def release_lock(store, owner):
    if _owns(store, owner):
        _remove(_lock_path(store))


# --- worker -------------------------------------------------------------------


def iter_jobs(store):
    out = []
    for name in _listdir(jobs_dir(store)):
        if not name.endswith(".json") or name.endswith((".result.json", ".apply.json")):
            continue
        path = os.path.join(jobs_dir(store), name)
        try:
            with open(path, encoding="utf-8") as f:
                job = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(job, dict) and job.get("request_id"):
            out.append((path, job))
    return out


class Budget:
    def __init__(self, job_left, run_left):
        self.job_left, self.run_left, self.spent = job_left, run_left, 0.0

    def cap(self):
        if self.run_left < MIN_CALL_USD:
            raise RunBudgetReached()
        if self.job_left < MIN_CALL_USD:
            raise JobBudgetExhausted("job budget exhausted")
        return min(self.job_left, self.run_left)

    def charge(self, cost):
        self.spent += cost
        self.job_left -= cost
        self.run_left -= cost


def run_worker(store):
    cfg = load_config(store)
    seen, spent = set(), 0.0
    while True:
        owner = acquire_lock(store)
        if owner is None:
            log(store, "worker: lock held by a live worker, leaving jobs queued")
            return 0
        try:
            spent = drain(store, cfg, owner, seen, spent)
        finally:
            release_lock(store, owner)
        if spent >= cfg["run_budget_usd"] - MIN_CALL_USD:
            return 0
        # A job that arrived after this pass started would otherwise wait for the next trigger.
        if not any(job.get("status") == "queued" and job["request_id"] not in seen
                   for _, job in iter_jobs(store)):
            return 0


def drain(store, cfg, owner, seen, spent):
    for _, job in iter_jobs(store):
        if job.get("status") == "captured" and heartbeat(store, owner):
            apply_result(store, job)
    jobs = sorted(iter_jobs(store),
                  key=lambda pair: (not pair[1].get("defer_until_existing", False),
                                    str(pair[1].get("updated") or "")), reverse=True)
    awaiting = {j.get("ledger_key") for _, j in jobs if j.get("status") == "captured"}
    for path, job in jobs:
        rid = job["request_id"]
        if rid in seen:
            continue
        if job.get("status") not in PENDING:
            seen.add(rid)
            continue
        if job.get("ledger_key") in awaiting:
            seen.add(rid)  # an earlier result for this session waits for the apply step
            continue
        if spent >= cfg["run_budget_usd"] - MIN_CALL_USD:
            log(store, "worker: run budget reached, remaining jobs stay queued")
            break
        if not heartbeat(store, owner):
            log(store, "worker: lost the lock, stopping")
            break
        seen.add(rid)
        spent += process_job(store, cfg, owner, path, job, cfg["run_budget_usd"] - spent)
        if job.get("status") == "captured":
            apply_result(store, job)
        if job.get("status") == "captured":
            awaiting.add(job.get("ledger_key"))
    return spent


def process_job(store, cfg, owner, path, job, run_left):
    if job.get("status") == "running":
        # Only a worker that died mid-job leaves this; count it so a crash cannot loop forever.
        job["attempts"] = int(job.get("attempts") or 0) + 1
        job["error"] = "a worker stopped mid-job"
        if job["attempts"] >= MAX_ATTEMPTS:
            job.update(status="failed", updated=_utc())
            _write_json(path, job)
            log(store, f"{job['request_id']}: failed: {job['error']}")
            return 0.0
    job.update(status="running", updated=_utc())
    _write_json(path, job)
    budget = Budget(cfg["budget_usd"] - float(job.get("cost_usd") or 0), run_left)
    try:
        _run_job(store, cfg, owner, job, budget)
    except RunBudgetReached:
        job["status"] = "queued"
        log(store, f"{job['request_id']}: run budget reached, stays queued")
    except JobBudgetExhausted as e:
        job.update(status="failed", error=str(e))
        log(store, f"{job['request_id']}: failed: {e}")
    except (CaptureError, OSError, ValueError, KeyError, TypeError) as e:
        budget.charge(getattr(e, "cost", 0.0))
        job["attempts"] = int(job.get("attempts") or 0) + 1
        job["error"] = f"{type(e).__name__}: {e}"
        job["status"] = "failed" if job["attempts"] >= MAX_ATTEMPTS else "queued"
        log(store, f"{job['request_id']}: attempt {job['attempts']} failed: {job['error']}")
    job["cost_usd"] = round(float(job.get("cost_usd") or 0) + budget.spent, 6)
    job["updated"] = _utc()
    _write_json(path, job)
    return budget.spent


def _run_job(store, cfg, owner, job, budget):
    if BACKEND_FOR_HARNESS.get(job["harness"]) not in RUNNERS:
        raise CaptureError(f"no runner for harness {job['harness']}")
    harness, transcript, upto = job["harness"], job["transcript"], int(job["upto"])
    if mt.transcript_identity(harness, transcript)["fingerprint"] != job["fingerprint"]:
        raise CaptureError("transcript was rewritten since the job was queued")
    entry = mt.ledger_get(store, job["ledger_key"]) or {}
    start = max(int(entry.get("offset") or 0), int(job.get("cursor") or 0))
    chunks, outcome = 0, None
    while start < upto and chunks < cfg["max_chunks_per_job"]:
        turns, end, _ = mt.read_since(harness, transcript, start, cfg["cap_bytes"], upto=upto)
        if end <= start:
            raise CaptureError(f"no complete record at offset {start}")
        chunks += 1
        heartbeat(store, owner)
        skip = handle_chunk(store, cfg, owner, job, turns, start, end, budget)
        job["cursor"] = end
        if skip is None:
            job.update(status="captured", outcome=f"result for bytes {start}-{end}")
            log(store, f"{job['request_id']}: captured bytes {start}-{end}, awaiting apply")
            return
        _advance_ledger(store, job, end, f"skipped ({skip})")
        outcome, start = f"skipped ({skip})", end
    if start >= upto:
        if job.get("saves"):
            job.update(status="saved")
        else:
            job.update(status="skipped", outcome=outcome or "skipped (nothing new)")
        log(store, f"{job['request_id']}: {job['outcome']}")
    else:
        job["status"] = "queued"
        log(store, f"{job['request_id']}: {chunks} chunk(s) handled, continues at {start}")


def _advance_ledger(store, job, end, outcome):
    data = mt.ledger_get(store, job["ledger_key"]) or {}
    mt.ledger_put(store, job["ledger_key"], **{
        **data, "transcript": job["transcript"], "fingerprint": job["fingerprint"],
        "offset": end, "updated": _utc(), "outcome": outcome})


def handle_chunk(store, cfg, owner, job, turns, start, end, budget):
    """None once a validated result is written; otherwise the skip reason."""
    focus = job.get("focus")
    if not focus and mt.activity(turns)["user_turns"] == 0:
        return "floor"
    opener = mt.session_opener(job["harness"], job["transcript"])
    chunk = mt.render_slice(turns)
    if not focus:
        text = _triage_input(job, opener, chunk)
        verdict = _call(store, job, cfg["triage_model"], "capture-triage.md", text, TRIAGE_SCHEMA, budget)
        if not isinstance(verdict, dict) or verdict.get("decision") not in ("save", "skip"):
            raise Rejected("triage reply has no save/skip decision")
        if verdict["decision"] == "skip":
            return f"triage: {str(verdict.get('reason') or '').strip()[:200]}"
        heartbeat(store, owner)
    candidates = gather_candidates(store, job["project"], (focus or "") + "\n" + chunk,
                                   cfg["pages_cap_bytes"])
    state = read_state(store, job["project"])
    text = _capture_input(store, job, opener, focus, chunk, state, candidates)
    reply = _call(store, job, cfg["capture_model"], "capture.md", text, CAPTURE_SCHEMA, budget)
    validate_capture(store, reply, job["project"], candidates)
    heartbeat(store, owner)
    result = {
        "schema_version": 1,
        "request_id": job["request_id"],
        "project": job["project"],
        "ledger_key": job["ledger_key"],
        "transcript": job["transcript"],
        "fingerprint": job["fingerprint"],
        "trigger": job["trigger"],
        "start": start,
        "end": end,
        "upto": job["upto"],
        "created": _utc(),
        "reply": reply,
        "inputs": {
            "pages": {c["slug"]: {"path": c["path"], "sha256": c["sha256"],
                                  "excerpt_only": c["excerpt_only"]} for c in candidates},
            "state": {k: state[k] for k in ("path", "sha256", "modified")},
        },
    }
    _write_json(os.path.join(jobs_dir(store), f"{job['request_id']}.result.json"), result)
    return None


def _prompt(name):
    with open(os.path.join(HERE, "prompts", name), encoding="utf-8") as f:
        return f.read()


def _call(store, job, model, prompt_name, text, schema, budget):
    cap = budget.cap()
    work = os.path.join(capture_dir(store), "work", job["request_id"])
    cwd = os.path.join(work, "cwd")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(cwd)
    input_file = os.path.join(work, "input.txt")
    with open(input_file, "w", encoding="utf-8") as f:
        f.write(text)
    runner = RUNNERS[BACKEND_FOR_HARNESS[job["harness"]]]
    try:
        reply, cost = runner(model, _prompt(prompt_name), input_file, schema, cap, cwd)
    except CaptureError as e:
        budget.charge(e.cost)
        e.cost = 0.0  # charged here, so process_job does not count it twice
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    budget.charge(cost)
    return reply


def _triage_input(job, opener, chunk):
    return (f"Project: {job['project']}\n\n## Session opener\n{opener or '(none)'}\n\n"
            f"## Transcript slice\n{chunk}\n")


def _capture_input(store, job, opener, focus, chunk, state, candidates):
    parts = [f"Project: {job['project']}", f"Today: {datetime.now(timezone.utc).date().isoformat()}",
             "", "## Session opener", opener or "(none)", ""]
    if focus:
        parts += ["## Focus (the user asked to save this)", focus.strip(), ""]
    parts += [f"## Current state file ({state['path']})", state["text"] or "(none)", "",
              "## Candidate pages"]
    if not candidates:
        parts.append("(none)")
    for c in candidates:
        mark = "excerpt_only: first 40 lines, never update it" if c["excerpt_only"] else "full"
        parts += [f"### {c['slug']} ({mark})", c["text"], ""]
    seen = {c["slug"] for c in candidates}
    others = [s for s in existing_slugs(store) if s not in seen]
    parts += ["", "## Other existing page slugs (never `create` one of these)",
              "\n".join(others) or "(none)", "", "## Transcript slice", chunk, ""]
    return "\n".join(parts)


def existing_slugs(store):
    """Slugs `validate_capture` refuses to `create`: the `pages/*.md` names, sorted."""
    try:
        names = os.listdir(os.path.join(store, "pages"))
    except OSError:
        return []
    return sorted(n[:-3] for n in names if n.endswith(".md"))


_WORD = re.compile(r"[a-z][a-z0-9_-]{3,}")
_STOP = frozenset(
    "that this with from have what when will would there their about which your them then than "
    "were been they into also just like should could does make only some here more need want "
    "know sure okay yeah thanks please file line code user assistant tool".split())


def _terms(text, limit=12):
    counts = {}
    for word in _WORD.findall(text.lower()):
        if word not in _STOP:
            counts[word] = counts.get(word, 0) + 1
    return sorted(counts, key=lambda w: (-counts[w], w))[:limit]


def gather_candidates(store, project, text, cap_bytes):
    """Up to 8 pages ranked by memory_find's scoring; full text within `cap_bytes`."""
    terms = _terms(text)
    repo = f"repo:{project}".lower()
    ranked = []
    for path in mf.page_files(store, archive=False):
        page = mf.read_page(path)
        if page is None:
            continue
        matched, tiers = 0, [0, 0, 0, 0]
        for term in terms:
            hit = mf.score(page, [term])
            if hit:
                matched += 1
                for i, tier in enumerate(mf.TIERS):
                    tiers[i] += hit[0][tier]
                tiers[3] += hit[1]
        if matched:
            ranked.append(((-matched, repo not in page["labels"], *(-t for t in tiers), page["slug"]), path))
    out, used = [], 0
    for _, path in sorted(ranked)[:MAX_PAGES]:
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        entry = {"slug": os.path.basename(path)[:-3],
                 "path": os.path.relpath(path, store).replace(os.sep, "/"),
                 "sha256": hashlib.sha256(raw).hexdigest()}
        if used + len(raw) <= cap_bytes:
            used += len(raw)
            entry.update(text=text, excerpt_only=False)
        else:
            entry.update(text="\n".join(text.splitlines()[:EXCERPT_LINES]), excerpt_only=True)
        out.append(entry)
    return out


def read_state(store, project):
    import memory_state

    path = memory_state.get_state_file_path(store, project)
    rel = os.path.relpath(path, store).replace(os.sep, "/")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {"path": rel, "sha256": None, "modified": None, "text": ""}
    text = raw.decode("utf-8", errors="replace")
    return {"path": rel, "sha256": hashlib.sha256(raw).hexdigest(),
            "modified": memory_state._extract_state_modified(text), "text": text}


def _one_line(value):
    return isinstance(value, str) and value.strip() and "\n" not in value and "\r" not in value


def validate_capture(store, reply, project, candidates):
    """Raise Rejected on any violation: the whole reply is refused, nothing is written."""
    if not isinstance(reply, dict):
        raise Rejected("capture reply is not a JSON object")
    problems = []
    pages = reply.get("pages")
    full = {c["slug"] for c in candidates if not c["excerpt_only"]}
    excerpt = {c["slug"] for c in candidates if c["excerpt_only"]}
    if not isinstance(pages, list):
        problems.append("pages is not a list")
        pages = []
    elif len(pages) > MAX_PAGES:
        problems.append(f"{len(pages)} pages, at most {MAX_PAGES}")
    slugs = set()
    for i, page in enumerate(pages):
        where = f"pages[{i}]"
        if not isinstance(page, dict) or set(page) != set(PAGE_KEYS):
            problems.append(f"{where} does not have exactly the fields {', '.join(PAGE_KEYS)}")
            continue
        slug = page["slug"]
        if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
            problems.append(f"{where} slug {slug!r} is not a kebab-case slug")
            continue
        where = f"page {slug}"
        if slug in slugs:
            problems.append(f"{where} appears twice")
        slugs.add(slug)
        if page["action"] not in ("create", "update"):
            problems.append(f"{where} action {page['action']!r}")
        elif page["action"] == "update" and slug not in full:
            problems.append(f"{where} update declined: " + (
                "given only as an excerpt" if slug in excerpt else "not a candidate page"))
        elif page["action"] == "create" and os.path.exists(os.path.join(store, "pages", f"{slug}.md")):
            problems.append(f"{where} create would overwrite an existing page")
        if page["type"] not in PAGE_TYPES:
            problems.append(f"{where} type {page['type']!r}")
        for key in ("name", "description"):
            if not _one_line(page[key]):
                problems.append(f"{where} {key} is not one non-empty line")
        labels = page["labels"]
        if (not isinstance(labels, list)
                or not all(_one_line(lb) and not set(lb) & set(",[]") for lb in labels)):
            problems.append(f"{where} labels are not a list of plain labels")
        elif f"repo:{project}" not in labels:
            problems.append(f"{where} labels lack repo:{project}")
        if not isinstance(page["body"], str) or not page["body"].strip():
            problems.append(f"{where} body is empty")
        elif len(page["body"].encode("utf-8")) > MAX_BODY_BYTES:
            problems.append(f"{where} body over {MAX_BODY_BYTES} bytes")
    state = reply.get("state", "missing")
    if state is not None and not (isinstance(state, dict) and set(state) == set(STATE_SECTIONS)
                                  and all(isinstance(v, str) for v in state.values())):
        problems.append("state is neither null nor the six sections")
    if not isinstance(reply.get("summary"), str):
        problems.append("summary is not a string")
    extra = set(reply) - {"pages", "state", "summary"}
    if extra:
        problems.append(f"unexpected fields {sorted(extra)}")
    if problems:
        raise Rejected("reply rejected: " + "; ".join(problems[:6]))


# --- apply --------------------------------------------------------------------


class ApplyDeferred(Exception):
    """The store was busy or in mid-merge; the journal stays and the job is retried."""


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _confined(store, rel):
    """Why `rel` may not be written (symlink or escape), else None."""
    target = os.path.join(store, rel)
    parent = os.path.realpath(os.path.dirname(target))
    root = os.path.realpath(store)
    if not parent.startswith(root + os.sep):
        return "directory is outside the store"
    if os.path.islink(target):
        return "target is a symlink"
    if not os.path.realpath(target).startswith(parent + os.sep):
        return "target resolves outside its directory"
    return None


def _yaml_value(value):
    plain = re.fullmatch(r"[A-Za-z0-9_(][^:#\[\]{}&*!|>'\"%@`,\\]*", value)
    return value if plain and not value.endswith(" ") else json.dumps(value, ensure_ascii=False)


def _new_page_text(page, today):
    labels = ", ".join(page["labels"])
    return (f"---\nname: {_yaml_value(page['name'])}\ndescription: {_yaml_value(page['description'])}\n"
            f"type: {page['type']}\nlabels: [{labels}]\ncreated: {today}\nlast_accessed: {today}\n"
            f"access_count: 0\n---\n\n{page['body'].strip()}\n")


def _updated_page_text(old, page):
    """Existing frontmatter lines kept, the three model fields and labels merged, body replaced."""
    import memory_frontmatter as fm

    found = fm.find_frontmatter(old)
    if not found:
        raise ValueError("page has no frontmatter")
    eol, start, end = found
    lines = old[start:end].split(eol)
    for key, value in (("name", _yaml_value(page["name"])),
                       ("description", _yaml_value(page["description"])), ("type", page["type"])):
        row = f"{key}: {value}"
        idx = next((i for i, ln in enumerate(lines) if ln.startswith(f"{key}:")), None)
        if idx is None:
            lines.insert(0, row)
        else:
            lines[idx] = row
    head = fm.merge_labels(old[:start] + eol.join(lines) + old[end:], page["labels"], warn=None)
    closing = fm.CLOSE_RE.search(head, start - len(eol))
    return head[:closing.end()].rstrip("\r\n") + eol + eol + page["body"].strip() + "\n"


def _state_text(state, project, today_iso):
    parts = [f"---\ntype: state\nproject: {project}\nmodified: {today_iso}\n"
             f"machine: {HOST.split('.')[0]}\n---\n\n# STATE — {project}\n"]
    for key, title in (("summary", "Summary"), ("left_off", "Left off"), ("next_steps", "Next steps"),
                       ("decisions", "Decisions"), ("blockers", "Blockers"), ("goal", "Goal")):
        parts.append(f"\n## {title}\n{state[key].strip()}\n")
    return "".join(parts)


def _journal_path(store, rid, start=None, end=None):
    """Each captured slice has its own recovery journal."""
    suffix = f".{start}-{end}" if start is not None and end is not None else ""
    return os.path.join(jobs_dir(store), f"{rid}{suffix}.apply.json")


def _capture_marker(journal):
    return f"{journal['request_id']}@{journal['start']}-{journal['end']}"


def _plan_apply(store, job):
    """The journal: every write with its exact new content and expected pre-hash."""
    rid = job["request_id"]
    with open(os.path.join(jobs_dir(store), f"{rid}.result.json"), encoding="utf-8") as f:
        result = json.load(f)
    if result.get("request_id") != rid or result.get("project") != job["project"]:
        raise ValueError("result file does not belong to this job")
    reply, inputs = result["reply"], result["inputs"]
    now = datetime.now(timezone.utc)
    files, conflicts = [], []
    for page in reply["pages"]:
        slug, rel = page["slug"], f"pages/{page['slug']}.md"
        entry = {"kind": "page", "slug": slug, "path": rel, "action": page["action"]}
        bad = _confined(store, rel)
        current = _read_bytes(os.path.join(store, rel))
        if bad:
            entry.update(outcome="rejected", reason=bad)
        elif page["action"] == "create":
            entry.update(pre_sha=None, content=_new_page_text(page, now.date().isoformat()))
        elif current is None or _sha(current) != (inputs["pages"].get(slug) or {}).get("sha256"):
            entry.update(outcome="conflict", reason="page changed since the model read it")
        else:
            try:
                new = _updated_page_text(current.decode("utf-8"), page)
            except (ValueError, UnicodeDecodeError) as e:
                entry.update(outcome="conflict", reason=str(e))
            else:
                entry.update(pre_sha=_sha(current), content=new)
        if entry.get("outcome"):
            conflicts.append(slug)
        files.append(entry)
    state = reply.get("state")
    journal = {"schema_version": 1, "request_id": rid, "project": job["project"], "phase": "planned",
               "start": int(result["start"]), "end": int(result["end"]),
               "ledger_key": job["ledger_key"], "files": files,
               "transcript_ended": os.path.getmtime(job["transcript"]), "state": None, "sha": None}
    if state is not None:
        rel = inputs["state"]["path"]
        text = _state_text(state, job["project"], now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        entry = {"kind": "state", "path": rel, "pre_sha": inputs["state"]["sha256"], "content": text}
        bad = _confined(store, rel)
        if bad:
            entry.update(outcome="rejected", reason=bad)
        elif len(text.encode("utf-8")) > _state_budget():
            entry.update(outcome="conflict", reason="new state is over the size budget")
        files.append(entry)
    return journal


def _state_budget():
    import memory_reindex

    return memory_reindex.STATE_BUDGET_BYTES


def _parse_ts(value):
    try:
        ts = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).timestamp()


def _state_gate(entry, current, journal):
    """None when the state may be written, else (outcome, reason)."""
    if current is not None and entry["pre_sha"] != _sha(current):
        return "conflict", "state changed since the model read it"
    if current is None and entry["pre_sha"] is not None:
        return "conflict", "state file vanished since the model read it"
    if current is not None:
        import memory_state

        modified = memory_state._extract_state_modified(current.decode("utf-8", errors="replace"))
        if modified is not None:
            stamp = _parse_ts(modified)
            if stamp is None or stamp >= journal["transcript_ended"]:
                return "superseded", f"state was modified at {modified}, after this session ended"
    return None


def _write_files(store, journal):
    """Under the sync lock, after the pull: write what still applies, record each outcome."""
    for entry in journal["files"]:
        if entry.get("outcome") in ("rejected", "conflict", "superseded"):
            continue
        path = os.path.join(store, entry["path"])
        current = _read_bytes(path)
        want = entry["content"].encode("utf-8")
        entry.pop("outcome", None), entry.pop("reason", None)
        if current is not None and _sha(current) == _sha(want):
            entry["outcome"] = "written"
            continue
        bad = _confined(store, entry["path"])
        if bad:
            entry.update(outcome="rejected", reason=bad)
        elif entry["kind"] == "state" and (gate := _state_gate(entry, current, journal)):
            entry.update(outcome=gate[0], reason=gate[1])
        elif entry["kind"] == "page" and (current is not None if entry["pre_sha"] is None
                                         else current is None or _sha(current) != entry["pre_sha"]):
            entry.update(outcome="conflict", reason="page changed since the model read it")
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(want)
            os.replace(tmp, path)
            entry["outcome"] = "written"


def _count(journal, kind):
    return sum(e["kind"] == kind and e.get("outcome") == "written" for e in journal["files"])


def _commit_message(journal):
    pages, state = _count(journal, "page"), _count(journal, "state")
    parts = ([f"{pages} page{'s' if pages != 1 else ''}"] if pages else []) + (["STATE"] if state else [])
    return (f"capture({journal['project']}): {', '.join(parts)}\n\n"
            f"okfmem-capture: {_capture_marker(journal)}\n")


def _find_commit(store, journal):
    import memory_sync as ms

    out = ms._git(store, "log", "-n", "1", "--format=%h", "--fixed-strings",
                  f"--grep=okfmem-capture: {_capture_marker(journal)}")
    return out.stdout.strip() or None if out.returncode == 0 else None


def _save_journal(store, journal, phase=None):
    if phase:
        journal["phase"] = phase
    _write_json(_journal_path(store, journal["request_id"], journal["start"], journal["end"]), journal)


def _ahead_of_upstream(store):
    import memory_sync as ms

    out = ms._git(store, "rev-list", "--count", "@{u}..HEAD")
    return int(out.stdout.strip() or 0) if out.returncode == 0 else 0


def _commit_phase(store, job, journal):
    import memory_sync as ms

    if (sha := _find_commit(store, journal)):
        journal["sha"] = sha
        return
    prepared = False

    def prepare(_):
        nonlocal prepared
        _write_files(store, journal)
        _save_journal(store, journal, "written")
        prepared = True
        if job.get("cwd") and os.path.isdir(job["cwd"]):
            try:
                import memory_init as mi

                mi.resolve_project(store, cwd=job["cwd"], compile=True)
            except Exception as e:  # the projection is derived; a failure never loses the capture
                log(store, f"{journal['request_id']}: view compile failed: {type(e).__name__}: {e}")
        return [e["path"] for e in journal["files"] if e.get("outcome") == "written"]

    res = ms.sync_store(store, lambda: _commit_message(journal), token=ms.store_token(),
                        paths=[], prepare=prepare)
    if not prepared:
        if not str(res.get("reason") or "").startswith("prepare failed:"):
            raise ApplyDeferred(res.get("reason") or "store sync did not run")
        raise CaptureError(res.get("reason") or "store sync did not prepare writes")
    wrote = any(e.get("outcome") == "written" for e in journal["files"])
    if wrote and not res["committed"] and res.get("reason") != ms.NO_CHANGES_REASON:
        raise ApplyDeferred(res.get("reason") or "store sync did not commit capture")
    journal["sha"] = res["sha"] if res["committed"] else None
    journal["pushed"] = bool(res["pushed"])
    if res.get("push_error"):
        log(store, f"{journal['request_id']}: saved locally, push failed: {res['push_error']}")


def _record(store, job, journal):
    """Advance the ledger and the job; the only step that moves the transcript offset."""
    outcomes = journal["files"]
    state = next((e for e in outcomes if e["kind"] == "state"), None)
    state_outcome = (state.get("outcome") or "conflict") if state else None
    pages = _count(journal, "page")
    held = [f"{e['slug']}: {e['reason']}" for e in outcomes
            if e["kind"] == "page" and e.get("outcome") in ("rejected", "conflict")]
    parts = []
    if journal["sha"]:
        parts.append(f"saved {journal['sha']}: {pages} page(s)" + (", state" if state_outcome == "written" else ""))
    else:
        parts.append("skipped (nothing to write)")
    if state_outcome and state_outcome != "written":
        parts.append(f"state {state_outcome}: {state.get('reason')}")
    if held:
        parts.append("pages held: " + "; ".join(held))
    outcome = "; ".join(parts)
    _advance_ledger(store, job, journal["end"], outcome)
    if state_outcome == "written" and job.get("session"):
        import memory_state

        memory_state.refresh_session_baseline(store, job["project"], job["session"])
    if journal["sha"]:
        job["saves"] = int(job.get("saves") or 0) + 1
    job.pop("error", None)
    job.update(outcome=outcome, state=state_outcome, cursor=journal["end"], updated=_utc(),
               unpushed=_ahead_of_upstream(store) > 0)
    done = journal["end"] >= int(job["upto"])
    job["status"] = ("saved" if job.get("saves") else "skipped") if done else "queued"
    _write_json(os.path.join(jobs_dir(store), f"{job['request_id']}.json"), job)
    log(store, f"{job['request_id']}: {outcome}")


def apply_result(store, job):
    """Apply a job's validated result to the store; resumes from the journal after a crash."""
    path = os.path.join(jobs_dir(store), f"{job['request_id']}.json")
    try:
        with open(os.path.join(jobs_dir(store), f"{job['request_id']}.result.json"), encoding="utf-8") as f:
            result = json.load(f)
        jpath = _journal_path(store, job["request_id"], result["start"], result["end"])
        if os.path.exists(jpath):
            with open(jpath, encoding="utf-8") as f:
                journal = json.load(f)
        else:
            journal = _plan_apply(store, job)
            _save_journal(store, journal)
        if journal["phase"] in ("planned", "written"):
            if any("content" in e for e in journal["files"]):
                _commit_phase(store, job, journal)
            else:
                _write_files(store, journal)
            _save_journal(store, journal, "committed")
        _record(store, job, journal)
        _save_journal(store, journal, "recorded")
    except ApplyDeferred as e:
        job["apply_attempts"] = int(job.get("apply_attempts") or 0) + 1
        job["error"] = f"apply deferred: {e}"
        job["status"] = "failed" if job["apply_attempts"] >= MAX_ATTEMPTS else "captured"
        log(store, f"{job['request_id']}: {job['error']}")
    except (CaptureError, OSError, ValueError, KeyError, TypeError) as e:
        job.update(status="failed", error=f"apply failed: {type(e).__name__}: {e}")
        log(store, f"{job['request_id']}: {job['error']}")
    else:
        return
    job["updated"] = _utc()
    _write_json(path, job)


# --- runners ------------------------------------------------------------------


def _usd(value):
    return f"{max(value, 0):.6f}".rstrip("0").rstrip(".")


def run_claude(model, prompt, input_file, schema, budget_usd, cwd):
    """One headless, tool-less `claude -p` call; returns (reply, cost_usd)."""
    exe = shutil.which("claude")
    if not exe:
        raise RunnerError("claude is not on PATH")
    # Never --bare: it skips OAuth/keychain credentials and breaks plan-login installs.
    argv = [exe, "-p", "--model", model, "--tools", "", "--strict-mcp-config",
            "--setting-sources", "project", "--settings", json.dumps({"disableAllHooks": True}),
            "--no-session-persistence", "--output-format", "json",
            "--json-schema", json.dumps(schema, separators=(",", ":")),
            "--max-budget-usd", _usd(budget_usd)]
    with open(input_file, encoding="utf-8") as f:
        stdin = prompt.rstrip() + "\n\n" + f.read()
    try:
        proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=cwd, timeout=RUNNER_TIMEOUT_S,
                              env={**os.environ, "OKFMEM_INTERNAL": "1"})
    except subprocess.TimeoutExpired as e:
        raise RunnerError(f"claude timed out after {RUNNER_TIMEOUT_S} s") from e
    except OSError as e:
        raise RunnerError(f"claude did not start: {e}") from e
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise RunnerError(f"claude exited {proc.returncode} without JSON output") from None
    if not isinstance(out, dict):
        raise RunnerError("claude output is not a JSON object")
    try:
        cost = float(out.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    if proc.returncode != 0 or out.get("is_error") or out.get("subtype", "success") != "success":
        raise RunnerError(f"claude failed: {out.get('subtype') or 'exit ' + str(proc.returncode)}", cost)
    reply = out.get("structured_output")
    if reply is None:
        try:
            reply = json.loads(out.get("result") or "")
        except (TypeError, ValueError):
            raise RunnerError("claude returned no structured output", cost) from None
    return reply, cost


RUNNERS = {"claude": run_claude}


# --- status -------------------------------------------------------------------


def status_lines(store):
    """`okfmem status` lines: per-project queued/failed counts and the latest outcome."""
    jobs = [job for _, job in iter_jobs(store)]
    if not jobs:
        return ["capture   never run"]
    by_project = {}
    for job in jobs:
        by_project.setdefault(str(job.get("project") or "?"), []).append(job)
    lines = []
    for project in sorted(by_project):
        group = by_project[project]
        queued = sum(j.get("status") in PENDING for j in group)
        failed = sum(j.get("status") == "failed" for j in group)
        done = [j for j in group if j.get("status") not in PENDING]
        last = max(done, key=lambda j: str(j.get("updated") or ""), default=None)
        if last is None:
            tail = "no finished job yet"
        else:
            detail = last.get("error") if last.get("status") == "failed" else last.get("outcome")
            tail = f"last {last.get('status')} {last.get('updated')}" + (f" — {detail}" if detail else "")
        lines.append(f"capture   {project}: queued {queued}, failed {failed}; {tail}")
    ahead = _ahead_of_upstream(store) if any(j.get("saves") for j in jobs) else 0
    if ahead:
        lines.append(f"capture   unpushed: {ahead} commit(s); `okfmem sync` pushes them")
    return lines


def hook_status_lines(store):
    settings = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    import memory_init as mi
    configured = False
    try:
        with open(settings, encoding="utf-8") as f:
            data = json.load(f)
        hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
        configured = any(
            isinstance(hook, dict) and mi._is_capture_command(str(hook.get("command", "")))
            for groups in hooks.values() if isinstance(groups, list)
            for group in groups if isinstance(group, dict)
            for hook in group.get("hooks", []) if isinstance(group.get("hooks", []), list)
        )
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        return ["capture hooks   settings.json unreadable"]
    jobs = [job for _, job in iter_jobs(store)]
    verified = any(job.get("trigger") in {"SessionEnd", "PreCompact", "catch-up"}
                   and job.get("status") not in PENDING for job in jobs)
    root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    try:
        backlog = sum(1 for _ in mt.uncaptured(store, "claude-code", [root], CATCHUP_IDLE_S))
    except (OSError, ValueError):
        backlog = "unavailable"
    lines = [f"capture hooks   {'configured' if configured else 'not configured'}; "
             f"{'verified active' if verified else 'not verified active'}; backlog {backlog}"]
    try:
        with open(skipped_unresolvable_path(store), encoding="utf-8") as f:
            entries = json.load(f).get("entries", {})
        counts = {}
        for entry in entries.values():
            project = str(entry.get("project") or "unknown")
            counts[project] = counts.get(project, 0) + 1
        if counts:
            lines.append("capture skipped unresolved: " + ", ".join(
                f"{project} {counts[project]}" for project in sorted(counts)))
    except (OSError, ValueError, TypeError):
        pass
    return lines


# --- CLI ----------------------------------------------------------------------


def _parser():
    ap = argparse.ArgumentParser(prog="okfmem capture", description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=False)
    mode.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--stdin-hook", action="store_true",
                      help="read a session-end hook payload from stdin and queue it (always exits 0)")
    mode.add_argument("--now", action="store_true", help="queue one session now")
    mode.add_argument("--record", action="store_true",
                      help="mark a session covered up to --upto after a foreground save")
    ap.add_argument("--catch-up", action="store_true",
                    help="queue Claude Code sessions whose capture never ran")
    ap.add_argument("--harness", help="claude-code or codex (stdin-hook default: claude-code)")
    ap.add_argument("--session", help="session id; its transcript is looked up")
    ap.add_argument("--transcript", help="transcript .jsonl path")
    ap.add_argument("--cwd", help="fallback cwd when the transcript records none")
    ap.add_argument("--focus", help="file whose text the user asked to save; skips triage")
    ap.add_argument("--trigger", help="trigger name recorded in the job")
    ap.add_argument("--upto", type=int, help="--record: byte offset the foreground save covered")
    ap.add_argument("--outcome", choices=("saved", "skipped"), default="saved",
                    help="--record: what the foreground save did (default: saved)")
    ap.add_argument("--store")
    return ap


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--worker" in argv:
        args = _parser().parse_args(argv)
        store = resolve_store(args.store)
        try:
            return run_worker(store)
        except Exception as e:  # detached: the log is the only place a crash shows
            log(store, f"worker crashed: {type(e).__name__}: {e}")
            return 1
    # Triggers only: the worker runs with OKFMEM_INTERNAL=1 and must still run.
    if os.environ.get("OKFMEM_INTERNAL") == "1":
        return 0
    args = _parser().parse_args(argv)
    if not (args.worker or args.stdin_hook or args.now or args.catch_up or args.record):
        _parser().error("one of --worker, --stdin-hook, --now, --catch-up, or --record is required")
    store = resolve_store(args.store)
    if args.now:
        return trigger_now(args, store)
    if args.catch_up and not args.stdin_hook:
        return trigger_catch_up(args, store)
    if args.record:
        return record_coverage(args, store)
    return trigger_stdin_hook(args, store)


if __name__ == "__main__":
    sys.exit(main())
