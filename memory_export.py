#!/usr/bin/env python3
"""okfmem export — one repo's pages as a bundle a teammate imports through the inbox (#149).

    okfmem export --repo NAME --out DIR [--label LABEL]... [--archive] [--dry-run] [--json] [--store PATH]

Selects the live `pages/*.md` pages labelled `repo:NAME` or a registry alias of
it, and/or carrying any `--label` (both flags: a page must match both);
`--archive` adds `pages/archive/**`. Index, state and view files and
`type: user` pages are never exported. A page a secret pattern or the inbox
leak gate hits is held, not redacted, and listed with its reason.

Each exported page loses `last_accessed`, `access_count` and `pinned`, gains
the canonical `repo:NAME` label, and keeps its body byte-for-byte. DIR must be
new or empty; it receives the pages and `manifest.json`. The receiver imports
the bundle with `okfmem inbox push` + `okfmem inbox review`, unchanged.

Exit 0 when the bundle is written (or would be, with --dry-run), 1 when no page
is exported, 2 on a usage, store or DIR error.
Store location: --store, else $OKFMEM_STORE, else ~/okfmem-store.
"""

import argparse
import hashlib
import json
import os
import re
import runpy
import shlex
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.append(os.path.join(HERE, "plugins"))
from memory_frontmatter import (  # noqa: E402
    find_frontmatter,
    merge_labels,
    normalize_label,
    parse_fields,
    parse_labels,
)
from memory_inbox import _normalise_repo, importable_name, leak_hits, page_fields  # noqa: E402
from memory_init import (  # noqa: E402
    _load_registry,
    _resolve_aliases,
    check_store_layout,
    resolve_project,
)
from okfmem_session import _ASSIGN, _COMPILED, scrub  # noqa: E402

DEFAULT_STORE = os.environ.get("OKFMEM_STORE", os.path.expanduser("~/okfmem-store"))
FORMAT = 1
MANIFEST = "manifest.json"
# Decay state is the exporter's own; the importer's decay starts fresh.
PER_PERSON_RE = re.compile(r"^(?:last_accessed|access_count|pinned)[ \t]*:")
LINK_RE = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s<>#]+\.md)(?:#[^)\s>]*)?>?\s*\)")


def _never_exported(name):
    upper = name.upper()
    return upper.startswith("MEMORY") or upper in ("STATE.MD", "CONTEXT.MD")


def candidate_files(store, archive=False):
    """[(file name, path)]: top-level `pages/*.md`, then `pages/archive/**` with `archive`."""
    pages = os.path.join(store, "pages")
    out = []
    for name in sorted(os.listdir(pages)):
        path = os.path.join(pages, name)
        if name.endswith(".md") and os.path.isfile(path):
            out.append((name, path))
    if archive:
        for dirpath, dirnames, filenames in os.walk(os.path.join(pages, "archive")):
            dirnames.sort()
            for name in sorted(filenames):
                if name.endswith(".md"):
                    out.append((name, os.path.join(dirpath, name)))
    return [(n, p) for n, p in out if not _never_exported(n)]


def repo_labels(store, repo):
    reg = _load_registry(os.path.join(store, "registry.json"))
    return {f"repo:{repo}".lower(), *_resolve_aliases(reg, repo)}


def strip_per_person(text, fm):
    eol, body_start, body_end = fm
    lines = text[body_start:body_end].split(eol)
    keep = [ln for ln in lines if not PER_PERSON_RE.match(ln)]
    return text[:body_start] + eol.join(keep) + text[body_end:]


def rewrite(text, repo=None):
    """`text` without per-person fields and, with `repo`, labelled `repo:<repo>`;
    the body after the frontmatter is untouched."""
    text = strip_per_person(text, find_frontmatter(text))
    return merge_labels(text, [f"repo:{repo}"], warn=None) if repo else text


def secret_locations(text):
    """Where scrub() redacts, as `"<key>" on line N` or `a token-shaped value on
    line N`; never the matched value, so the reason itself leaks nothing."""

    def line(m):
        return text.count("\n", 0, m.start()) + 1

    found = {(line(m), "a token-shaped value") for rx in _COMPILED for m in rx.finditer(text)}
    found |= {(line(m), f'"{m.group(1)}"') for m in _ASSIGN.finditer(text)}
    return [f"{what} on line {n}" for n, what in sorted(found)]


def hold_reasons(name, text):
    reasons = []
    if not importable_name(name):
        reasons.append("not a page name okfmem imports")
    if page_fields(text) is None:
        reasons.append("no OKF frontmatter (name, description, type)")
    if scrub(text) != text:
        where = secret_locations(text)
        reasons.append(("secret pattern: " + ", ".join(where)) if where else "secret pattern")
    hits = leak_hits(text)
    if hits:
        reasons.append("leak gate: " + ", ".join(hits))
    return reasons


def unresolved_links(pages):
    names = {p["name"] for p in pages}
    out = []
    for page in pages:
        text = page["data"].decode("utf-8")
        fm = find_frontmatter(text)
        body = text[fm[2] :] if fm else text
        seen = set()
        for target in LINK_RE.findall(body):
            local = target[2:] if target.startswith("./") else target
            if "://" in target or local in names or target in seen:
                continue
            seen.add(target)
            out.append({"page": page["name"], "target": target})
    return out


def build_bundle(store, repo=None, labels=(), archive=False):
    """{pages: [{name, path, data, sha256}], held: [{name, path, reason}],
    unresolved_links, user_pages}. Reads only; writes nothing."""
    want_repo = repo_labels(store, repo) if repo else None
    want_labels = set(labels)
    pages, held, taken = [], [], set()
    user_pages = 0
    for name, path in candidate_files(store, archive):
        try:
            with open(path, "rb") as f:
                text = f.read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fm = find_frontmatter(text)
        page_labels = set(parse_labels(text, fm, warn=None)) if fm else set()
        if want_repo is not None and not want_repo & page_labels:
            continue
        if want_labels and not want_labels & page_labels:
            continue
        fields = parse_fields(text, fm) if fm else {}
        if fields.get("type", "").strip().lower() == "user":
            user_pages += 1
            continue
        rel = os.path.relpath(path, store).replace(os.sep, "/")
        reasons = hold_reasons(name, text)
        # Case-blind: on macOS and Windows `Rule.md` and `rule.md` are one file.
        if name.lower() in taken:
            reasons.insert(0, "same file name as another exported page")
        taken.add(name.lower())
        if reasons:
            held.append({"name": name, "path": rel, "reason": "; ".join(reasons)})
            continue
        data = rewrite(text, repo).encode("utf-8")
        pages.append(
            {"name": name, "path": rel, "data": data, "sha256": hashlib.sha256(data).hexdigest()}
        )
    return {
        "pages": pages,
        "held": held,
        "unresolved_links": unresolved_links(pages),
        "user_pages": user_pages,
    }


def engine_version():
    try:
        return runpy.run_path(os.path.join(HERE, "okfmem"), run_name="okfmem_version")[
            "version_line"
        ]()
    except (OSError, KeyError, SyntaxError):
        return "okfmem unknown"


def manifest(bundle, repo, labels, archive, now=None):
    return {
        "format": FORMAT,
        "exported_at": now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine": engine_version(),
        "filter": {"repo": repo, "labels": list(labels), "archive": archive},
        "pages": [{"name": p["name"], "sha256": p["sha256"]} for p in bundle["pages"]],
        "held": [{"name": h["name"], "reason": h["reason"]} for h in bundle["held"]],
        "unresolved_links": bundle["unresolved_links"],
    }


def _origin_slug(root):
    try:
        r = subprocess.run(
            ["git", "-C", root, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _normalise_repo(r.stdout) if r.returncode == 0 else None


def repo_slug(store, repo, cwd=os.curdir):
    """`owner/name` for the receiver's `inbox push --repo`: the origin of the
    current checkout when it is NAME's, else of a checkout the registry maps
    to NAME; else NAME itself."""
    roots = []
    try:
        current = resolve_project(store, cwd=cwd)
    except (OSError, ValueError):
        current = {}
    if (current.get("project") or "").lower() == repo.lower() and current.get("root"):
        roots.append(current["root"])
    reg = _load_registry(os.path.join(store, "registry.json"))
    mapping = reg.get("map") if isinstance(reg.get("map"), dict) else {}
    roots += [
        root
        for root, name in sorted(mapping.items())
        if isinstance(name, str) and name.lower() == repo.lower() and os.path.isdir(root)
    ]
    for root in roots:
        slug = _origin_slug(root)
        if slug:
            return slug
    return repo


def import_command(out_dir, slug=None):
    repo = f" --repo {shlex.quote(slug)}" if slug else ""
    return (
        f"okfmem inbox push {shlex.quote(out_dir)} --source export{repo}"
        " && okfmem inbox review"
    )


def write_bundle(out_dir, bundle, man):
    os.makedirs(out_dir, exist_ok=True)
    for page in bundle["pages"]:
        with open(os.path.join(out_dir, page["name"]), "xb") as f:
            f.write(page["data"])
    with open(os.path.join(out_dir, MANIFEST), "xb") as f:
        f.write((json.dumps(man, indent=2) + "\n").encode("utf-8"))


def out_dir_problem(out_dir):
    """Why `out_dir` cannot take a bundle, or None when it is new or empty."""
    if not os.path.exists(out_dir):
        return None
    if not os.path.isdir(out_dir):
        return f"{out_dir} is not a directory; give a new or empty one"
    if os.listdir(out_dir):
        return f"{out_dir} is not empty; give a new or empty directory"
    return None


def report(bundle, out_dir, slug, dry_run):
    n, held, links = len(bundle["pages"]), bundle["held"], bundle["unresolved_links"]
    verb = "would export" if dry_run else "exported"
    lines = [
        f"okfmem export: {verb} {n} page(s), held {len(held)}, "
        f"{len(links)} unresolved link(s) -> {out_dir}"
    ]
    lines += [f"  held {h['path']}: {h['reason']}" for h in held]
    lines += [f"  link {u['page']} -> {u['target']} is not in the bundle" for u in links]
    if bundle["user_pages"]:
        lines.append(f"  left out {bundle['user_pages']} type: user page(s); they are never shared")
    if n:
        lines += [
            "Read the bundle before you send it: export finds secret patterns, "
            "not names, hostnames or private context.",
            "The receiver imports it through review with:",
            f"  {import_command(out_dir, slug)}",
        ]
    return lines


def main(argv=None, cwd=os.curdir):
    ap = argparse.ArgumentParser(prog="okfmem export", description=__doc__.splitlines()[0])
    ap.add_argument("--repo")
    ap.add_argument("--label", action="append", default=[])
    ap.add_argument("--out", required=True)
    ap.add_argument("--archive", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--store", default=DEFAULT_STORE)
    args = ap.parse_args(argv)

    def fail(message):
        print(f"okfmem export: {message}", file=sys.stderr)
        return 2

    if not args.repo and not args.label:
        return fail("give --repo NAME, --label LABEL, or both")
    if args.repo and normalize_label(f"repo:{args.repo}") is None:
        return fail(f"--repo {args.repo!r} is not a valid repo name")
    labels = []
    for raw in args.label:
        label = normalize_label(raw)
        if label is None:
            return fail(f"--label {raw!r} is not a valid label")
        if label not in labels:
            labels.append(label)
    if not os.path.isdir(os.path.join(args.store, "pages")):
        return fail(
            f"no pages/ under {args.store}; pass --store or run `okfmem migrate-to-labels`"
        )
    try:
        check_store_layout(args.store)
    except ValueError as e:
        return fail(str(e))
    problem = out_dir_problem(args.out)
    if problem:
        return fail(problem)

    bundle = build_bundle(args.store, args.repo, labels, args.archive)
    man = manifest(bundle, args.repo, labels, args.archive)
    slug = repo_slug(args.store, args.repo, cwd) if args.repo else None
    if bundle["pages"] and not args.dry_run:
        try:
            write_bundle(args.out, bundle, man)
        except OSError as e:
            return fail(f"could not write {args.out}: {e}")
    say = sys.stderr if args.json else sys.stdout
    for line in report(bundle, args.out, slug, args.dry_run):
        print(line, file=say)
    if not bundle["pages"]:
        print("okfmem export: no page to export — nothing written", file=say)
    if args.json:
        print(json.dumps(man, indent=2))
    return 0 if bundle["pages"] else 1


if __name__ == "__main__":
    sys.exit(main())
