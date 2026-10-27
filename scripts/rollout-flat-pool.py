#!/usr/bin/env python3
r"""Bring one machine onto okfmem >= 0.2.1 and the migrated flat-pool store.

Steps (each prints what it did):
  0. preflight: restore tracked projects/ edits that only stamp frontmatter
     backfill/bookkeeping fields (regenerable; they block the pull because
     migrate-to-labels moved those paths); abort on any other local edit
  1. okfmem update   (fast-forward the engine clone at ~/okfmem)
  2. okfmem pull     (integrate the migrated store; only after step 3's gate)
  3. refuse (before pulling) unless engine >= 0.2.1; after pulling, unless
     the store carries `layout: labels`
  4. delete untracked projects/<repo>/STATE.md stubs a pre-0.2.1 init seeded
  5. `init --yes` from the engine dir (global pointers, hooks, skills, link);
     this also rewrites pointer blocks a leaking test aimed at a temp store
  6. every ~/.claude/projects/*/memory link aimed at projects/<name> or
     outside the store: `init --yes` in its repo when the repo exists here,
     else (projects/<name> only) repoint it to views/<name>
  7. report every memory link, pointer files, and the store's git status

--dry-run changes nothing: it reports step 0 and the links, and skips the
update and pull (an old engine must not pull the migrated store).

Run with the machine's own python (3.11+), after the engine has this file:
  git -C ~/okfmem pull --ff-only
  python3 ~/okfmem/scripts/rollout-flat-pool.py [--dry-run]
On Windows: python $HOME\okfmem\scripts\rollout-flat-pool.py
Exit 0 = clean, 1 = finished with problems listed, 2 = aborted unchanged.
"""

import argparse
import os
import re
import subprocess
import sys

HOME = os.path.expanduser("~")
ENGINE = os.path.join(HOME, "okfmem")
STORE = os.path.abspath(
    os.path.expanduser(
        os.environ.get("OKFMEM_STORE") or os.path.join(HOME, "okfmem-store")
    )
)
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
POINTER_FILES = [
    os.path.join(HOME, ".claude", "CLAUDE.md"),
    os.path.join(HOME, ".codex", "AGENTS.md"),
    os.path.join(HOME, ".codex", "hooks.json"),
    os.path.join(HOME, ".gemini", "config", "AGENTS.md"),
]
STUB_TEXT = "No session saved yet"
# Frontmatter keys `okfmem backfill` / consolidate write: deterministic or
# bookkeeping, so a local edit touching only these is safe to discard.
STAMP_KEYS = {
    "importance",
    "pinned",
    "created",
    "status",
    "last_accessed",
    "access_count",
    "modified",
    "decay_score",
    "last_reinforced",
}
STAMP_LINE = re.compile(r"^[+-](%s):" % "|".join(sorted(STAMP_KEYS)))


def run(args, cwd=None, timeout=600):
    """(exit code, combined output); a hung network child fails, never blocks."""
    try:
        r = subprocess.run(
            args,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(args)}"
    return r.returncode, (r.stdout + r.stderr).strip()


def changed_lines(diff):
    """+/- lines inside the hunks of a `git diff -U0`. The file header is
    skipped by position (before the first @@), never by prefix: a removed
    line reading `--x` shows as `---x` and must still count."""
    lines, in_hunk = [], False
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line[:1] in ("+", "-"):
            lines.append(line)
    return lines


def git(*args):
    return run(["git", "-C", STORE, *args])


def okfmem(*args, cwd=None):
    return run([sys.executable, os.path.join(ENGINE, "okfmem"), *args], cwd=cwd)


def init_yes(cwd):
    return run(
        [sys.executable, os.path.join(ENGINE, "memory_init.py"), "--yes"], cwd=cwd
    )


def say(step, msg):
    print(f"[{step}] {msg}", flush=True)


def version():
    """Engine version; (0, 0, 0) for engines older than the VERSION file."""
    try:
        with open(os.path.join(ENGINE, "VERSION"), encoding="utf-8") as f:
            return tuple(int(x) for x in f.read().strip().split("."))
    except (OSError, ValueError):
        return (0, 0, 0)


def under(path, parent):
    path, parent = os.path.normcase(path), os.path.normcase(parent)
    return path == parent or path.startswith(parent + os.sep)


def is_link(path):
    if os.path.islink(path):
        return True
    try:
        import memory_init as mi

        return bool(mi._is_junction(path))
    except (ImportError, AttributeError):
        return False


def memory_links():
    """(encoded dir name, link path) for every memory link/junction."""
    if not os.path.isdir(CLAUDE_PROJECTS):
        return
    for entry in sorted(os.listdir(CLAUDE_PROJECTS)):
        link = os.path.join(CLAUDE_PROJECTS, entry, "memory")
        if is_link(link):
            yield entry, link


def classify(link):
    """('views'|'projects'|'foreign', name-or-None) for where a link points."""
    target = os.path.realpath(link)
    for kind in ("views", "projects"):
        base = os.path.realpath(os.path.join(STORE, kind))
        if under(target, base) and target != base:
            return kind, os.path.relpath(target, base).split(os.sep)[0]
    return "foreign", None


def preflight(dry):
    """Step 0. True when the store may be pulled."""
    _, out = git("diff", "--name-only", "--", "projects")
    ok = True
    for path in [p for p in out.splitlines() if p.strip()]:
        _, diff = git("diff", "-U0", "--", path)
        changed = changed_lines(diff)
        if changed and all(STAMP_LINE.match(line) for line in changed):
            say(
                "preflight",
                f"{'would restore' if dry else 'restored'} {path} "
                f"({len(changed)} stamp line(s))",
            )
            if not dry:
                git("checkout", "--", path)
        else:
            say("preflight", f"KEEP {path}: has non-stamp edits; resolve by hand")
            ok = False
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    dry = ap.parse_args().dry_run
    sys.path.insert(0, ENGINE)

    say(
        "env",
        f"engine={ENGINE} {'.'.join(map(str, version()))} store={STORE} "
        f"python={sys.executable}",
    )

    # 0. preflight
    if not preflight(dry):
        say("abort", "local store edits need a human; nothing else changed")
        return 2

    # 1-2. update the engine, then pull with it
    if dry:
        say("update", "dry-run: " + okfmem("update", "--check")[1].splitlines()[-1])
        say("pull", "dry-run: skipped")
    else:
        code, out = okfmem("update")
        say(
            "update",
            f"exit {code}: "
            + " | ".join(
                line
                for line in out.splitlines()
                if line.startswith(("=>", "Updating", "Already", "error", "fatal"))
            ),
        )
        # 3. an old engine must never pull the migrated store: gate before pull
        if version() < (0, 2, 1):
            say(
                "abort",
                f"engine is {'.'.join(map(str, version()))}; need >= 0.2.1 "
                "(update failed or the v0.2.1 PR is not merged); store not pulled",
            )
            return 2
        code, out = okfmem("pull")
        say("pull", out.splitlines()[0] if out else f"exit {code}")
        if code != 0:
            say("abort", "pull did not integrate the store; resolve it, then re-run")
            return 2

        import memory_init as mi

        if not mi.has_layout_marker(STORE):
            say("abort", "store has no `layout: labels` marker after pull")
            return 2

    # 4. stub STATE.md files seeded into projects/ by a pre-0.2.1 init
    _, out = git("status", "--porcelain", "--untracked-files=all", "--", "projects")
    for line in out.splitlines():
        if not line.startswith("?? "):
            continue
        rel = line[3:].strip()
        path = os.path.join(STORE, rel)
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if rel.endswith("/STATE.md") and STUB_TEXT in text and len(text) < 400:
            say("stubs", f"{'would delete' if dry else 'deleted'} {rel}")
            if not dry:
                os.remove(path)
        else:
            say("stubs", f"KEPT untracked {rel} (not a stub; check it)")

    # 5. global wiring + the engine repo's own link
    if dry:
        say("init", "dry-run: would run init --yes in the engine dir")
    else:
        code, out = init_yes(ENGINE)
        say(
            "init",
            f"engine dir exit {code}: "
            + " | ".join(
                line.strip()
                for line in out.splitlines()
                if "memory link" in line
                or "pointers" in line
                or "error" in line.lower()
            ),
        )

    # 6. links still aimed at projects/<name>, or outside the store
    for entry, link in memory_links():
        kind, name = classify(link)
        if kind == "views":
            continue
        try:
            import memory_init as mi

            root = mi.decode_root(entry)
        except (ImportError, AttributeError):
            root = None
        has_repo = bool(
            root
            and os.path.isdir(root)
            and run(["git", "-C", root, "rev-parse", "--git-dir"])[0] == 0
        )
        if has_repo:
            if dry:
                say("relink", f"would run init --yes in {root} ({kind})")
                continue
            code, out = init_yes(root)
            say(
                "relink",
                f"init --yes in {root}: "
                + (
                    " | ".join(
                        line.strip()
                        for line in out.splitlines()
                        if "memory link" in line
                    )
                    or f"exit {code}"
                ),
            )
            kind, name = classify(link)
            if kind == "views":
                continue
        if kind == "projects":
            if dry:
                say("relink", f"would repoint orphan {entry} -> views/{name}")
                continue
            view = os.path.join(STORE, "views", name)
            if not os.path.isfile(os.path.join(view, "MEMORY.md")):
                mi.compile_view(
                    STORE,
                    name,
                    reg=mi._load_registry(os.path.join(STORE, "registry.json")),
                )
            if os.path.islink(link):
                os.remove(link)
            else:
                os.rmdir(link)  # junction: removes the reparse point only
            mi._make_link(os.path.realpath(view), link)
            say("relink", f"repointed orphan {entry} -> views/{name}")
        else:
            say(
                "relink",
                f"LEFT {entry} -> {os.path.realpath(link)} "
                "(outside the store, no repo here; check it)",
            )

    # 7. report
    bad = 0
    for entry, link in memory_links():
        kind, _ = classify(link)
        has_index = os.path.isfile(os.path.join(link, "MEMORY.md"))
        flag = "OK" if kind == "views" and has_index else kind.upper()
        bad += flag != "OK"
        say("links", f"{flag:9} {entry} -> {os.path.realpath(link)}")
    for path in POINTER_FILES:
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                leaked = "pytest-of-" in f.read()
            bad += leaked
            say("pointers", f"{'LEAK' if leaked else 'OK':9} {path}")
    _, out = git("status", "-sb")
    say("store", " | ".join(out.splitlines()[:6]))
    say("done", f"engine {'.'.join(map(str, version()))}; {bad} problem(s)")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
