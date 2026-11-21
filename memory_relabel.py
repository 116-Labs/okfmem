#!/usr/bin/env python3
"""okfmem relabel — carry a project's pages and state over to a new project name.

After a repo rename or move, `init` resolves the new name and the old
`repo:<old>` pages drop out of the loaded view (#238). `okfmem relabel OLD NEW`
adds `repo:NEW` beside `repo:OLD` on every page, copies `state/OLD.md` to
`state/NEW.md` when that is missing, and recompiles the view. Additive and
idempotent, so it asks for no confirmation; `--dry-run` previews it.
"""
import argparse
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_frontmatter import (  # noqa: E402
    find_frontmatter,
    merge_labels,
    page_paths,
    parse_labels,
)
from memory_init import (  # noqa: E402
    _lookup_reg,
    _is_junction,
    _load_registry,
    _managed_copy_target,
    _resolve_aliases,
    compile_view,
    encode_root,
    has_layout_marker,
    resolve_project,
)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _read(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _has_label(text, label):
    fm = find_frontmatter(text)
    return bool(fm) and label in parse_labels(text, fm, warn=None)


def pages_with_label(store, name):
    """Paths of the pages labelled `repo:<name>`."""
    label = f"repo:{name}".lower()
    out = []
    for path in page_paths(store):
        try:
            if _has_label(_read(path), label):
                out.append(path)
        except (OSError, UnicodeDecodeError):
            continue
    return out


def _state_path(store, name):
    return os.path.join(store, "state", f"{name}.md")


def _is_stub_state(path, name):
    """True when the state file is missing or exactly what init seeds for `name`."""
    try:
        text = _read(path)
    except OSError:
        return True
    fm = find_frontmatter(text)
    body = text[fm[2]:].lstrip() if fm else text
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if fm and lines[:1] == ["---"]:
        lines = lines[1:]
    seeded = (
        f"# STATE — {name}",
        "No session saved yet. Run `/okfmem-save` at the end of a session to populate this.",
    )
    return bool(lines) and lines[0] == seeded[0] and all(
        ln in seeded for ln in lines
    ) or not lines


def _linked_project(store, claude_projects, root):
    """Name of the view the checkout's harness memory link points at, or None."""
    link = os.path.join(claude_projects, encode_root(root), "memory")
    if os.path.islink(link) or _is_junction(link):
        pointed = os.path.realpath(link)
    else:
        target = _managed_copy_target(link)
        pointed = os.path.realpath(target) if target else None
    if not pointed:
        return None
    views = os.path.normcase(os.path.realpath(os.path.join(store, "views")))
    if os.path.normcase(os.path.dirname(pointed)) != views:
        return None
    return os.path.basename(pointed)


def relabel_hint(store, claude_projects, root, reg, new):
    """The old project name when `new` holds nothing while a project this
    checkout's link, registry entry or directory name points at holds pages or
    state; None otherwise. Read-only."""
    if not (new and root and has_layout_marker(store)):
        return None
    if pages_with_label(store, new) or not _is_stub_state(_state_path(store, new), new):
        return None
    candidates = [
        _linked_project(store, claude_projects, root),
        _lookup_reg(reg.get("map", {}), root),
        os.path.basename(root),
    ]
    for old in candidates:
        if not isinstance(old, str) or old.lower() == new.lower():
            continue
        if pages_with_label(store, old) or not _is_stub_state(_state_path(store, old), old):
            return old
    return None


def format_hint(old, new):
    return (
        f"project '{new}' has no pages but '{old}' does (renamed or moved repo?) "
        f"— run: okfmem relabel {old} {new}"
    )


def _recompile(store, new):
    if resolve_project(store).get("project") == new:
        resolve_project(store, compile=True)
        return
    reg = _load_registry(os.path.join(store, "registry.json"))
    compile_view(store, new, aliases=_resolve_aliases(reg, new), reg=reg)


def relabel(store, old, new, dry_run=False, out=print):
    """Returns a process exit code."""
    for name in (old, new):
        if not NAME_RE.match(name):
            out(f"error: '{name}' is not a project name")
            return 2
    if old == new:
        out("error: old and new project names are the same")
        return 2
    if not has_layout_marker(store):
        out("error: relabel needs a 'layout: labels' store; run okfmem migrate-to-labels")
        return 2
    old_state, new_state = _state_path(store, old), _state_path(store, new)
    old_pages = pages_with_label(store, old)
    copy_state = os.path.isfile(old_state) and _is_stub_state(new_state, new)
    if not old_pages and not os.path.isfile(old_state):
        out(f"error: nothing is labelled repo:{old} and state/{old}.md is missing")
        return 1

    new_label = f"repo:{new}".lower()
    todo = []
    for path in old_pages:
        text = _read(path)
        if not _has_label(text, new_label):
            todo.append((path, merge_labels(text, [new_label], warn=None)))
    verb = "would add" if dry_run else "added"
    out(f"{verb} {new_label} to {len(todo)} of {len(old_pages)} page(s)")
    if copy_state:
        out(f"{'would copy' if dry_run else 'copied'} state/{old}.md to state/{new}.md")
    if dry_run:
        return 0
    for path, text in todo:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    if copy_state:
        shutil.copy2(old_state, new_state)
    _recompile(store, new)
    out(f"recompiled views/{new}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="okfmem relabel", description=__doc__)
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    ap.add_argument(
        "--store",
        default=os.environ.get("OKFMEM_STORE", os.path.expanduser("~/okfmem-store")),
    )
    args = ap.parse_args(argv)
    store = os.path.abspath(os.path.expanduser(args.store))
    return relabel(store, args.old, args.new, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
