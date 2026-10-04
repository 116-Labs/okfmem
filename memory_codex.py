"""Codex lifecycle integration, validated with CLI 0.160.0.

Trust is owned by Codex's /hooks UI, never forged by okfmem. This boundary
intentionally discards transcript_path before invoking Claude maintenance.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys

import memory_init as mi

MIN_VERSION = (0, 160, 0)


def supported():
    try:
        result = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        import re

        match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout)
        return bool(match and tuple(map(int, match.groups())) >= MIN_VERSION)
    except (OSError, subprocess.TimeoutExpired):
        return False


def managed(handler):
    return (
        isinstance(handler, dict)
        and isinstance(handler.get("command"), str)
        and "memory_codex.py" in handler.get("command", "")
        and "--event" in handler.get("command", "")
    )


def wire_hooks(store, dry_run=False, remove=False):
    path = os.path.join(mi.codex_home(), "hooks.json")
    if not remove and not supported():
        return "unsupported runtime; use instruction recall and okfmem consolidate manually"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        if remove:
            return "absent"
        data = {}
    except (OSError, ValueError):
        return "invalid/unreadable hooks.json; left untouched"
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        return "invalid hooks.json; left untouched"
    before = json.dumps(data, sort_keys=True)
    events = data.setdefault("hooks", {})
    for event in ("SessionStart", "Stop"):
        groups = events.get(event, [])
        if not isinstance(groups, list) or any(
            not isinstance(g, dict) or not isinstance(g.get("hooks", []), list)
            for g in groups
        ):
            return "invalid hook entries; left untouched"
        kept = []
        for group in groups:
            handlers = [h for h in group.get("hooks", []) if not managed(h)]
            if handlers or not group.get("hooks"):
                kept.append({**group, "hooks": handlers})
        if not remove:
            argv = [
                sys.executable,
                os.path.abspath(__file__),
                "--event",
                event,
                "--store",
                store,
            ]
            kept.append(
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": shlex.join(argv),
                            "commandWindows": subprocess.list2cmdline(argv),
                            "timeout": 120,
                        }
                    ]
                }
            )
        if kept:
            events[event] = kept
        else:
            events.pop(event, None)
    if json.dumps(data, sort_keys=True) == before:
        return "unchanged"
    if not dry_run:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path):
            mi.shutil.copy2(path, path + ".okfmem-backup")
        with open(path + ".okfmem-tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(path + ".okfmem-tmp", path)
    return (
        "would remove"
        if remove and dry_run
        else "removed"
        if remove
        else "would configure"
        if dry_run
        else "configured (execution unverified)"
    )


def status(store):
    if not mi.shutil.which("codex"):
        return "not installed"
    instructions = mi.codex_instructions()
    try:
        with open(instructions, encoding="utf-8") as f:
            wired = mi.MARKER_OPEN in f.read()
    except OSError:
        wired = False
    try:
        with open(os.path.join(mi.codex_home(), "hooks.json"), encoding="utf-8") as f:
            data = json.load(f)
        events = data.get("hooks", {})
        hooks = all(
            any(
                managed(h)
                for group in events.get(event, [])
                for h in group.get("hooks", [])
            )
            for event in ("SessionStart", "Stop")
        )
    except (OSError, ValueError, AttributeError, TypeError):
        hooks = False
    ready = mi.resolve_project(store)["state"]
    skills = all(
        os.path.isfile(
            os.path.join(os.path.expanduser("~"), ".agents", "skills", name, "SKILL.md")
        )
        for name in ("okfmem", "okfmem-save", "okfmem-curate", "okfmem-reindex")
    )
    return (
        f"skills {'available' if skills else 'missing'}; instructions {'wired' if wired else 'missing'} ({instructions}); project {ready}; "
        f"hooks {'configured, trust/active state unverified (/hooks)' if hooks else 'not configured'}; "
        f"runtime {'supported' if supported() else 'unsupported'}; external-store access requires runtime permission"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", choices=("SessionStart", "Stop"), required=True)
    parser.add_argument("--store", required=True)
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not os.path.isdir(cwd):
            return
        os.chdir(cwd)
        if payload.get("stop_hook_active"):
            return
        engine = os.path.dirname(os.path.abspath(__file__))
        module = (
            "memory_pull.py"
            if args.event == "SessionStart"
            else "memory_consolidate.py"
        )
        argv = [sys.executable, os.path.join(engine, module), "--store", args.store]
        if args.event == "SessionStart":
            argv.append("--quiet")
        subprocess.run(
            argv,
            env={**os.environ, "OKFMEM_NO_STATUS": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            timeout=110,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass  # fail open; hook output never contains private session data


if __name__ == "__main__":
    main()
