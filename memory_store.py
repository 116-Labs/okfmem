#!/usr/bin/env python3
"""okfmem store — merge two stores, or split projects out of one (#78).

  okfmem store merge <source> [--store PATH] [--dry-run] [--yes] [--preserve-history]
  okfmem store split --projects a,b --dest PATH [--store PATH] [--delete-source]
                     [--dry-run] [--yes]

Both work on the flat label layout: a project is its `repo:<name>` label on pages
(archive included, registry aliases honoured) plus `state/<name>.md`.

An import never overwrites a page: a file that differs from the target's lands
beside it as `<path>.orphan` (`.orphan.2`, ...). A state file goes to the side
with the newer `modified:` stamp (else its last commit time). registry.json is
unioned (`overrides.org` / `.stack` one level deeper), the target winning per key. `views/` is compiled, never copied: the
target's views are rebuilt after a merge.

Confirmation (CLAUDE.md "Confirmation discipline"): merge and a copy-only split
are rung 2 (`[y/N]`, `--yes` skips it, a non-interactive run without `--yes`
writes nothing). `split --delete-source` is rung 3: typing SPLIT at a terminal;
`--yes` never covers it, and a non-interactive run copies but keeps the source.
"""
import argparse
import filecmp
import fnmatch
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_init as mi  # noqa: E402
from memory_frontmatter import parse_labels  # noqa: E402
from memory_sync import _git, _side_commit_ts, _state_modified_ts  # noqa: E402

SPLIT_WORD = "SPLIT"

# Never carried between stores: git internals, compiled views and the
# per-machine runtime files of the managed .gitignore block.
_RUNTIME_DIRS = {".git"} | {p[:-1] for p in mi.GITIGNORE_LINES if p.endswith("/")}
_RUNTIME_FILES = [p for p in mi.GITIGNORE_LINES if not p.endswith("/")]
# Each store keeps its own layout marker and ignore rules; the registry is
# merged, never copied.
_STORE_OWN = {"layout.yml", ".gitignore", "registry.json"}


def _default_store():
    return os.environ.get("OKFMEM_STORE", os.path.expanduser("~/okfmem-store"))


def _err(msg):
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _norm(path):
    return os.path.normcase(os.path.realpath(path))


def _overlaps(a, b):
    """True if a and b are the same directory or one holds the other."""
    a, b = _norm(a), _norm(b)
    try:
        common = os.path.commonpath([a, b])
    except ValueError:
        return False
    return common in (a, b)


def _store_error(store, role):
    """Why `store` is not a usable label-layout store, or None."""
    if not os.path.isdir(store):
        return f"{role} store '{store}' does not exist"
    try:
        mi.check_store_layout(store)
    except ValueError as exc:
        return f"{role} {exc}"
    if not mi.has_layout_marker(store):
        return f"{role} '{store}' is not an okfmem store (no 'layout: labels' marker)"
    return None


def _is_git_root(store):
    r = _git(store, "rev-parse", "--show-toplevel")
    return r.returncode == 0 and _norm(r.stdout.strip()) == _norm(store)


def _is_clean(store):
    r = _git(store, "status", "--porcelain")
    return r.returncode == 0 and r.stdout.strip() == ""


def _commit(store, message, force=False):
    """Stage everything and commit. Returns None, or the error text."""
    r = _git(store, "add", "-A")
    if r.returncode != 0:
        return r.stderr.strip() or "git add failed"
    if not force and _is_clean(store):
        return None
    r = _git(store, "commit", "-q", "-m", message)
    if r.returncode != 0:
        return (r.stderr or r.stdout).strip() or "git commit failed"
    return None


def store_files(store):
    """Relative ('/'-separated) paths of the files a store carries, sorted."""
    out = []
    for root, dirs, files in os.walk(store):
        dirs[:] = sorted(d for d in dirs if d not in _RUNTIME_DIRS
                         and not os.path.islink(os.path.join(root, d)))
        rel_root = os.path.relpath(root, store)
        for name in sorted(files):
            rel = name if rel_root == "." else f"{rel_root}/{name}".replace(os.sep, "/")
            # A worktree or submodule has a `.git` file holding a local gitdir path.
            if (rel in _STORE_OWN or name == ".git"
                    or os.path.islink(os.path.join(root, name))):
                continue
            if any(fnmatch.fnmatch(name, p) for p in _RUNTIME_FILES):
                continue
            out.append(rel)
    return out


def _native(store, rel):
    return os.path.join(store, *rel.split("/"))


def _is_state(rel):
    return rel.startswith("state/") and rel.endswith(".md") and rel.count("/") == 1


def _state_ts(store, rel):
    try:
        with open(_native(store, rel), encoding="utf-8") as f:
            ts = _state_modified_ts(f.read())
    except (OSError, UnicodeDecodeError):
        ts = None
    return ts or _side_commit_ts(store, rel, "HEAD")


def _same(a, b):
    try:
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def _orphan_path(src, target, rel, taken):
    """(free `.orphan[.N]` path, False), or (path, True) when an earlier
    merge already preserved this exact content there."""
    base = _native(target, rel) + mi.ORPHAN_SUFFIX
    cand, n = base, 2
    while os.path.lexists(cand) or os.path.normcase(cand) in taken:
        if os.path.normcase(cand) not in taken and _same(src, cand):
            return cand, True
        cand = f"{base}.{n}"
        n += 1
    return cand, False


def plan_import(source, target, rels):
    """One action per source file: copy, same, orphan, state-source or
    state-target. Nothing is written."""
    actions, taken = [], set()
    for rel in rels:
        src, dst = _native(source, rel), _native(target, rel)
        if not os.path.lexists(dst) and os.path.normcase(dst) not in taken:
            kind = "copy"
        elif os.path.isfile(dst) and not os.path.islink(dst) and _same(src, dst):
            kind = "same"
        elif _is_state(rel) and os.path.isfile(dst):
            s_ts, t_ts = _state_ts(source, rel), _state_ts(target, rel)
            if s_ts and t_ts and s_ts != t_ts:
                kind = "state-source" if s_ts > t_ts else "state-target"
            else:
                kind = "orphan"
        else:
            kind = "orphan"
        if kind == "orphan":
            dst, preserved = _orphan_path(src, target, rel, taken)
            if preserved:
                kind = "same"
        if kind in ("copy", "orphan"):
            taken.add(os.path.normcase(dst))
        actions.append({"kind": kind, "rel": rel, "src": src, "dst": dst})
    return actions


def apply_import(actions):
    for a in actions:
        if a["kind"] in ("copy", "orphan", "state-source"):
            os.makedirs(os.path.dirname(a["dst"]), exist_ok=True)
            shutil.copy2(a["src"], a["dst"])


def read_registry(store):
    """registry.json as a dict ({} when absent). Raises ValueError on a
    malformed file, so a merge never silently drops its entries."""
    path = os.path.join(store, "registry.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or not all(
            isinstance(data.get(k, {}), dict) for k in ("map", "overrides", "aliases")):
        raise ValueError(f"{path} is not a valid registry")
    return data


_NESTED_OVERRIDES = ("org", "stack")


def _registry_sections(reg, nested=()):
    """{section: entries}, each `overrides.<name>` in `nested` split out as a
    section of its own, so its keys are counted and reported one by one."""
    sections = {key: dict(reg.get(key, {})) for key in ("map", "overrides", "aliases")}
    for name in nested:
        if isinstance(sections["overrides"].get(name), dict):
            sections[f"overrides.{name}"] = sections["overrides"].pop(name)
    return sections


def merge_registry(target_reg, source_reg):
    """(merged registry, ['section: key' entries where the target won])."""
    merged = mi._merge_registry(source_reg, target_reg)
    # Attaching a store is a trust decision (#139), so the target's list stands.
    for key in ("layout", "stores"):
        merged.pop(key, None)
        if key in target_reg:
            merged[key] = target_reg[key]
    aliases = {**source_reg.get("aliases", {}), **target_reg.get("aliases", {})}
    if aliases:
        merged["aliases"] = dict(sorted(aliases.items()))
    nested = []
    for name in _NESTED_OVERRIDES:
        src = source_reg.get("overrides", {}).get(name)
        tgt = target_reg.get("overrides", {}).get(name)
        if isinstance(src, dict) and isinstance(tgt, dict):
            merged["overrides"][name] = dict(sorted({**src, **tgt}.items()))
            nested.append(name)
    src_sections = _registry_sections(source_reg, nested)
    tgt_sections = _registry_sections(target_reg, nested)
    conflicts = sorted(
        f"{section}: {k}"
        for section, entries in src_sections.items()
        for k, v in entries.items()
        if k in tgt_sections.get(section, {}) and tgt_sections[section][k] != v
    )
    return merged, conflicts


def partition_registry(reg, projects):
    """The part of `reg` that belongs to `projects`: their map roots, the
    overrides keyed by those roots or names, and their aliases."""
    names = {p.lower() for p in projects}
    roots = {r: p for r, p in reg.get("map", {}).items()
             if isinstance(p, str) and p.lower() in names}

    def mine(key):
        return key in roots or key.lower() in names

    overrides = {}
    for key, val in reg.get("overrides", {}).items():
        if key in ("org", "stack") and isinstance(val, dict):
            sub = {k: v for k, v in val.items() if mine(k)}
            if sub:
                overrides[key] = sub
        elif key in ("org", "stack") or mine(key):
            overrides[key] = val
    aliases = {k: v for k, v in reg.get("aliases", {}).items()
               if k.lower().removeprefix("repo:") in names}
    out = {"map": roots, "overrides": overrides}
    if aliases:
        out["aliases"] = aliases
    return out


def _write_registry(store, reg):
    with open(os.path.join(store, "registry.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(reg, indent=2) + "\n")


def _page_labels(store, rel):
    try:
        with open(_native(store, rel), encoding="utf-8") as f:
            return parse_labels(f.read(), warn=None)
    except (OSError, UnicodeDecodeError):
        return []


def projects_in(store, rels):
    """Project names a set of store files carries (repo labels and state files)."""
    names = set()
    for rel in rels:
        if _is_state(rel):
            names.add(rel[len("state/"):-3])
        elif rel.startswith("pages/") and rel.endswith(".md"):
            names.update(lb[5:] for lb in _page_labels(store, rel) if lb.startswith("repo:"))
    return sorted(names, key=str.lower)


def _report(actions, reg_note, out):
    labels = {
        "copy": "copy", "orphan": "orphan", "state-source": "state",
        "state-target": "state",
    }
    for a in actions:
        kind = a["kind"]
        if kind == "same":
            continue
        line = f"  {labels[kind]:<7}{a['rel']}"
        if kind == "orphan":
            line += f" -> {os.path.basename(a['dst'])} (differs; target kept)"
        elif kind == "state-source":
            line += " (source newer; replaces target)"
        elif kind == "state-target":
            line += " (target newer; kept)"
        print(line, file=out)
    if reg_note:
        print(f"  {'merge':<7}{reg_note}", file=out)
    counts = {k: sum(1 for a in actions if a["kind"] == k)
              for k in ("copy", "orphan", "state-source", "state-target", "same")}
    print(
        f"\nSummary: {counts['copy']} new, {counts['orphan']} preserved as .orphan, "
        f"{counts['state-source']} state replaced, {counts['state-target']} state kept, "
        f"{counts['same']} identical skipped",
        file=out,
    )


def _registry_note(before, merged, conflicts, exists):
    if exists and merged == before:
        return ""
    old = _registry_sections(before, _NESTED_OVERRIDES)
    new = _registry_sections(merged, _NESTED_OVERRIDES)
    added = {k: len(set(entries) - set(old.get(k, {}))) for k, entries in new.items()}
    added["overrides"] = sum(n for k, n in added.items() if k.startswith("overrides"))
    note = (f"registry.json: +{added['map']} map, +{added['overrides']} overrides, "
            f"+{added['aliases']} aliases")
    if conflicts:
        note += f"; target kept on {', '.join(conflicts)}"
    return note


def _refresh_views(store):
    from memory_consolidate import _refresh_all_views
    _refresh_all_views(store)


def merge_stores(target, source, *, dry_run=False, assume_yes=False,
                 preserve_history=False, non_interactive=None, out=None):
    """Merge store `source` into store `target`. Returns an exit code."""
    out = out or sys.stdout
    target = os.path.abspath(os.path.expanduser(target))
    source = os.path.abspath(os.path.expanduser(source))
    if non_interactive is None:
        non_interactive = not sys.stdin.isatty()
    problem = _store_error(source, "source") or _store_error(target, "target")
    if problem:
        return _err(problem)
    if _overlaps(source, target):
        return _err("source and target stores must be separate directories")
    try:
        source_reg, target_reg = read_registry(source), read_registry(target)
    except ValueError as exc:
        return _err(str(exc))

    rels = store_files(source)
    actions = plan_import(source, target, rels)
    reg_merged, reg_conflicts = merge_registry(target_reg, source_reg)
    reg_exists = os.path.exists(os.path.join(target, "registry.json"))
    reg_note = _registry_note(target_reg, reg_merged, reg_conflicts, reg_exists) if source_reg else ""

    print(f"okfmem store merge · {'dry-run' if dry_run else 'apply'}", file=out)
    print(f"  source: {source}\n  target: {target}", file=out)
    print(f"  projects in source: {', '.join(projects_in(source, rels)) or 'none'}\n", file=out)
    _report(actions, reg_note, out)
    if preserve_history:
        print("History: the source's commits become a parent of the merge commit.", file=out)
    if dry_run:
        return 0

    if not _is_git_root(target):
        return _err(f"target store '{target}' is not the root of a git repository")
    if not _is_clean(target):
        return _err(f"target store '{target}' has uncommitted changes; commit or sync them first")
    if preserve_history and _git(source, "rev-parse", "--verify", "-q", "HEAD").returncode != 0:
        return _err(f"--preserve-history needs a git history in '{source}'")

    pending = [a for a in actions if a["kind"] in ("copy", "orphan", "state-source")]
    if not pending and not reg_note and not preserve_history:
        print("Nothing to merge: every source file is already in the target.", file=out)
        return 0
    hint = f'Run it later with: okfmem store merge "{source}" --store "{target}" --yes'
    if not mi._prompt_yes_no(
            f"Merge {len(pending)} file(s) from '{source}' into '{target}'?",
            assume_yes=assume_yes, non_interactive=non_interactive, manual_hint=hint):
        return 0

    if preserve_history:
        r = _git(target, "fetch", "-q", source, "HEAD")
        if r.returncode != 0:
            return _err(f"could not fetch the source history: {r.stderr.strip()}")
        r = _git(target, "merge", "-q", "--allow-unrelated-histories", "--no-ff",
                 "--no-commit", "-s", "ours", "FETCH_HEAD")
        if r.returncode != 0:
            _git(target, "merge", "--abort")
            return _err(f"could not start the history merge: {(r.stderr or r.stdout).strip()}")

    try:
        apply_import(actions)
        if reg_note:
            _write_registry(target, reg_merged)
    except OSError as exc:
        return _err(f"could not write the target store: {exc}; "
                    f"review it with: git -C \"{target}\" status")
    in_merge = _git(target, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0
    problem = _commit(target, f"okfmem store merge: import {os.path.basename(source)}",
                      force=in_merge)
    if problem:
        return _err(f"files merged but the commit failed: {problem}")
    _refresh_views(target)
    print("Merged. Run okfmem sync to push the store.", file=out)
    return 0


def _parse_projects(raw):
    names = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    return names


def select_projects(store, reg, projects, rels):
    """(selected rels, rels also labelled for another project, missing names)."""
    labels_of = {p.lower(): {f"repo:{p.lower()}", *mi._resolve_aliases(reg, p)}
                 for p in projects}
    wanted = set().union(*labels_of.values())
    selected, shared = [], set()
    found = {p.lower() for p in reg.get("map", {}).values()
             if isinstance(p, str) and p.lower() in labels_of}
    for rel in rels:
        if _is_state(rel):
            name = rel[len("state/"):-3].lower()
            if name in labels_of:
                selected.append(rel)
                found.add(name)
            continue
        if not (rel.startswith("pages/") and rel.endswith(".md")):
            continue
        labels = set(_page_labels(store, rel))
        hit = labels & wanted
        if not hit:
            continue
        selected.append(rel)
        found.update(p for p, own in labels_of.items() if own & hit)
        if any(lb.startswith("repo:") for lb in labels - wanted):
            shared.add(rel)
    missing = [p for p in projects if p.lower() not in found]
    return selected, shared, missing


def _is_fresh(dest):
    return not os.path.exists(dest) or (
        os.path.isdir(dest) and set(os.listdir(dest)) <= {".git"})


def _scaffold(dest):
    os.makedirs(os.path.join(dest, "pages"), exist_ok=True)
    if not os.path.isdir(os.path.join(dest, ".git")):
        r = _git(dest, "init", "-q")
        if r.returncode != 0:
            raise OSError(r.stderr.strip() or "git init failed")
    mi.ensure_store_gitignore(os.path.join(dest, ".gitignore"), dry_run=False)
    with open(os.path.join(dest, "layout.yml"), "w", encoding="utf-8") as f:
        f.write("layout: labels\n")


def _typed_confirm(prompt, word):
    try:
        return input(prompt).strip() == word
    except EOFError:
        return False


def split_store(source, projects, dest, *, delete_source=False, dry_run=False,
                assume_yes=False, non_interactive=None, out=None):
    """Copy `projects` from store `source` into store `dest`, scaffolding it
    when absent; with delete_source, then remove them from `source`."""
    out = out or sys.stdout
    source = os.path.abspath(os.path.expanduser(source))
    dest = os.path.abspath(os.path.expanduser(dest))
    if non_interactive is None:
        non_interactive = not sys.stdin.isatty()
    if not projects:
        return _err("--projects names no project")
    bad = [p for p in projects if not mi._STORE_NAME_RE.match(p)]
    if bad:
        return _err(f"invalid project name(s): {', '.join(bad)}")
    problem = _store_error(source, "source")
    if problem:
        return _err(problem)
    if _overlaps(source, dest):
        return _err("--dest must be a directory outside the source store")
    fresh = _is_fresh(dest)
    if not fresh:
        problem = _store_error(dest, "destination")
        if problem:
            return _err(problem)
    try:
        source_reg = read_registry(source)
        dest_reg = {} if fresh else read_registry(dest)
    except ValueError as exc:
        return _err(str(exc))

    rels = store_files(source)
    selected, shared, missing = select_projects(source, source_reg, projects, rels)
    if missing:
        return _err(f"no pages, state or registry entry for project(s): {', '.join(missing)}")
    actions = plan_import(source, dest, selected)
    part = partition_registry(source_reg, projects)
    reg_merged, reg_conflicts = merge_registry(dest_reg, part)
    reg_note = _registry_note(dest_reg, reg_merged, reg_conflicts, not fresh and bool(dest_reg))
    deletions = [r for r in selected if r not in shared]

    print(f"okfmem store split · {'dry-run' if dry_run else 'apply'}", file=out)
    print(f"  projects: {', '.join(projects)}\n  source: {source}", file=out)
    print(f"  dest: {dest}{' (new store)' if fresh else ''}\n", file=out)
    _report(actions, reg_note, out)
    if delete_source:
        print(f"\nRemove from source: {len(deletions)} file(s)", file=out)
        for rel in deletions:
            print(f"  delete {rel}", file=out)
        for rel in sorted(shared):
            print(f"  keep   {rel} (also labelled for another project)", file=out)
    if dry_run:
        return 0

    if delete_source and not (_is_git_root(source) and _is_clean(source)):
        return _err(f"source store '{source}' has uncommitted changes or is not a git "
                    "repository; --delete-source needs a clean, committed source")
    if not fresh and not (_is_git_root(dest) and _is_clean(dest)):
        return _err(f"destination store '{dest}' must be a clean git repository")

    cmd = (f'okfmem store split --projects "{",".join(projects)}" --dest "{dest}" '
           f'--store "{source}"')
    do_delete = False
    if delete_source and not non_interactive:
        if not _typed_confirm(
                f"This copies {len(selected)} file(s) to '{dest}' and deletes "
                f"{len(deletions)} from '{source}'. Type '{SPLIT_WORD}' to confirm: ",
                SPLIT_WORD):
            return _err(f"split cancelled (typed confirmation did not match '{SPLIT_WORD}')")
        do_delete = True
    else:
        if not mi._prompt_yes_no(
                f"Copy {len(selected)} file(s) of {', '.join(projects)} into '{dest}'?",
                assume_yes=assume_yes, non_interactive=non_interactive,
                manual_hint=f"Run it later with: {cmd} --yes"):
            return 0
        if delete_source:
            print("Source deletion skipped (non-interactive run; it needs a typed "
                  f"confirmation). Run it from a terminal: {cmd} --delete-source", file=out)

    try:
        if fresh:
            _scaffold(dest)
        apply_import(actions)
        if fresh or reg_note:
            _write_registry(dest, reg_merged)
    except OSError as exc:
        return _err(f"could not write the destination store: {exc}")
    problem = _commit(dest, f"okfmem store split: import {', '.join(projects)} "
                            f"from {os.path.basename(source)}")
    if problem:
        return _err(f"files copied but the destination commit failed: {problem}")
    _refresh_views(dest)
    print(f"Copied into '{dest}'.", file=out)

    if do_delete:
        for rel in deletions:
            os.remove(_native(source, rel))
        problem = _commit(source, f"okfmem store split: move {', '.join(projects)} "
                                  f"to {os.path.basename(dest)}")
        if problem:
            return _err(f"source files removed but the commit failed: {problem}")
        _refresh_views(source)
        print(f"Removed {len(deletions)} file(s) from '{source}'.", file=out)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="okfmem store",
                                 description="Merge stores, or split projects out of one.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("merge", help="merge another store into this one")
    m.add_argument("source", help="path of the store to import")
    s = sub.add_parser("split", help="copy (or move) projects into another store")
    s.add_argument("--projects", required=True, help="comma-separated project names")
    s.add_argument("--dest", required=True, help="destination store (scaffolded if absent)")
    s.add_argument("--delete-source", action="store_true",
                   help="remove the projects from this store (typed confirmation)")
    for p in (m, s):
        p.add_argument("--store", default=_default_store(),
                       help="this store (default: $OKFMEM_STORE or ~/okfmem-store)")
        p.add_argument("--dry-run", action="store_true", help="report only; write nothing")
        p.add_argument("--yes", action="store_true", help="skip the [y/N] confirmation")
    m.add_argument("--preserve-history", action="store_true",
                   help="record the source's git history as a merge parent")
    args = ap.parse_args(argv)
    if args.cmd == "merge":
        code = merge_stores(args.store, args.source, dry_run=args.dry_run,
                            assume_yes=args.yes, preserve_history=args.preserve_history)
    else:
        code = split_store(args.store, _parse_projects(args.projects), args.dest,
                           delete_source=args.delete_source, dry_run=args.dry_run,
                           assume_yes=args.yes)
    sys.exit(code)


if __name__ == "__main__":
    main()
