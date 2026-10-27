#!/usr/bin/env python3
"""okfmem state — inspect and guard active-state files (STATE.md / state/<repo>.md).

Subcommands:
  check  Compare the current state file against a baseline (session record or
         explicit timestamp/hash). Read-only; exits 0 on match, non-zero on mismatch.
"""
import argparse
import hashlib
import json
import os
import re
import sys

import memory_init as mi


def _extract_state_modified(content):
    """Extract the string timestamp from YAML frontmatter `modified:`."""
    m = re.search(r"^\s*modified:\s*[\"']?([^\r\n\"']+)", content, re.MULTILINE)
    return m.group(1).strip() if m else None


def get_state_file_path(store, project):
    """Resolve the active-state file path for a project in the given store."""
    candidates = [
        os.path.join(store, "state", f"{project}.md"),
        os.path.join(store, "projects", project, "STATE.md"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    if mi.has_layout_marker(store):
        return candidates[0]
    return candidates[1]


def check_state(store, project=None, session_id=None, since=None, expected_hash=None, cwd=None):
    """Check if state file matches the baseline.

    Returns (exit_code, message). Exit code 0 means match; non-zero means mismatch or error.
    """
    store = os.path.abspath(os.path.expanduser(store))

    if not project:
        info = mi.resolve_project(store, cwd=cwd or os.getcwd())
        project = info.get("repo") or info.get("project")

    if not project:
        return 1, "okfmem state check: cannot resolve project (specify --project <name>)"

    state_path = get_state_file_path(store, project)
    rel_path = os.path.relpath(state_path, store)

    baseline_modified = None
    baseline_hash = None

    if session_id:
        safe_id = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", session_id)
        sess_file = os.path.join(store, ".okfmem", "sessions", f"{safe_id}.json")
        if not os.path.isfile(sess_file):
            return 1, f"okfmem state check: session record not found: {sess_file}"
        try:
            with open(sess_file, "r", encoding="utf-8") as f:
                sess_data = json.load(f)
        except (OSError, ValueError) as e:
            return 1, f"okfmem state check: failed reading session record '{sess_file}': {e}"

        entry = sess_data.get(project) or sess_data.get("repos", {}).get(project)
        if not entry or not isinstance(entry, dict):
            return 1, f"okfmem state check: project '{project}' not found in session '{session_id}'"

        baseline_modified = entry.get("modified")
        baseline_hash = entry.get("hash")
    elif since is not None or expected_hash is not None:
        baseline_modified = since
        baseline_hash = expected_hash
    else:
        return 2, "okfmem state check: specify either --session <id> or --since <modified> / --hash <hash>"

    if not os.path.isfile(state_path):
        if baseline_modified is None and baseline_hash is None:
            return 0, f"okfmem state check: {rel_path} absent and baseline was empty"
        return 1, f"okfmem state check: state file does not exist: {rel_path}"

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return 1, f"okfmem state check: failed reading state file '{rel_path}': {e}"

    curr_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    curr_modified = _extract_state_modified(content)

    if baseline_modified is not None and baseline_modified != curr_modified:
        return 1, (f"okfmem state check: {rel_path} has changed since baseline "
                   f"(modified: expected '{baseline_modified}', current '{curr_modified}')")

    if baseline_hash is not None and baseline_hash != curr_hash:
        return 1, (f"okfmem state check: {rel_path} has changed since baseline "
                   f"(hash: expected '{baseline_hash}', current '{curr_hash}')")

    return 0, f"okfmem state check: {rel_path} matches baseline"


def refresh_session_baseline(store, project, session_id):
    """Advance one session's baseline to the state file it just wrote."""
    store = os.path.abspath(os.path.expanduser(store))
    safe_id = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", session_id or "")
    sess_file = os.path.join(store, ".okfmem", "sessions", f"{safe_id}.json")
    state_path = get_state_file_path(store, project)
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            content = f.read()
        with open(sess_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("invalid session record")
        record = {"modified": _extract_state_modified(content),
                  "hash": hashlib.sha256(content.encode("utf-8")).hexdigest()}
        data.setdefault("repos", {})[project] = record
        data[project] = record
        tmp_file = sess_file + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_file, sess_file)
    except (OSError, ValueError) as e:
        return 1, f"okfmem state refresh: {e}"
    return 0, f"okfmem state refresh: baseline advanced for {project}"


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    ap = argparse.ArgumentParser(description="Manage and check okfmem active-state files.")
    sub = ap.add_subparsers(dest="subcommand", required=True)

    chk = sub.add_parser("check", help="Check state file against a baseline")
    chk.add_argument("--store", default=os.environ.get("OKFMEM_STORE", os.path.expanduser("~/okfmem-store")))
    chk.add_argument("--project", "--repo", dest="project", help="Project name (defaults to current repo)")
    chk.add_argument("--session", help="Session ID in .okfmem/sessions/<id>.json")
    chk.add_argument("--since", help="Expected modified timestamp string")
    chk.add_argument("--hash", dest="expected_hash", help="Expected content hash")

    refresh = sub.add_parser("refresh", help="Advance a session baseline after saving state")
    refresh.add_argument("--store", default=os.environ.get("OKFMEM_STORE", os.path.expanduser("~/okfmem-store")))
    refresh.add_argument("--project", "--repo", dest="project", required=True)
    refresh.add_argument("--session", required=True)

    args = ap.parse_args(argv)

    if args.subcommand == "check":
        code, msg = check_state(
            args.store,
            project=args.project,
            session_id=args.session,
            since=args.since,
            expected_hash=args.expected_hash,
        )
        if code != 0:
            print(msg, file=sys.stderr)
        sys.exit(code)
    if args.subcommand == "refresh":
        code, msg = refresh_session_baseline(args.store, args.project, args.session)
        print(msg, file=sys.stderr if code else sys.stdout)
        sys.exit(code)


if __name__ == "__main__":
    main()
