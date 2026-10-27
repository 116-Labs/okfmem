#!/usr/bin/env python3
"""okfmem P1 — decay-frontmatter backfill.

One-shot, idempotent stamp of decay maintenance metadata onto every durable
OKF page in the store. No behavior change: harnesses treat frontmatter as
prose. The metadata is consumed later by memory_consolidate.py (P3).

Stamps, in each page's leading `--- ... ---` block (byte-preserving, no PyYAML
so there is no runtime dependency — matches the sibling okf-*.py scripts):

    importance: <6|3|10>   # project=6, reference=3, user/feedback=10 (max)
    pinned: <true|false>   # user/feedback => true (decay-exempt), else false
    created: YYYY-MM-DD     # git first-commit date, else file mtime
    last_accessed: YYYY-MM-DD   # = created at backfill time
    access_count: 0
    status: active

Rules:
  * Idempotent — a page already carrying a top-level `access_count:` is skipped.
  * Skips ck_*.md (retired ck snapshots — cruft, not durable pages),
    MEMORY.md / STATE.md / CONTEXT.md (indexes/state), and any page with no
    frontmatter or no top-level `type:` (reported, never guessed).
  * New keys inserted immediately before the closing `---`, preserving the
    existing name/description/type keys and per-file CRLF/LF style.

Store location: --store, else $OKFMEM_STORE, else ~/okfmem-store.
"""
import argparse
import glob
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_frontmatter import TOP_TYPE_RE, find_frontmatter  # noqa: E402

SKIP_NAMES = {"MEMORY.md", "STATE.md", "CONTEXT.md"}

TOP_ACCESS_RE = re.compile(r"^access_count:\s*", re.MULTILINE)

# importance by type; user/feedback are pinned (decay-exempt) at max importance.
IMPORTANCE = {"user": 10, "feedback": 10, "project": 6, "reference": 3}
PINNED_TYPES = {"user", "feedback"}


def top_type(text, eol, body_start, body_end):
    for ln in text[body_start:body_end].split(eol):
        m = TOP_TYPE_RE.match(ln)
        if m:
            return m.group(1).strip()
    return None


def build_created_map(store):
    """One git pass → {repo-relative path: earliest add date YYYY-MM-DD}.

    `git log --diff-filter=A --name-only` walks newest→oldest, so the LAST add
    date seen for a path is its earliest commit. 2171 files in one subprocess
    instead of one call each.
    """
    created = {}
    try:
        out = subprocess.run(
            ["git", "-C", store, "log", "--diff-filter=A", "--name-only",
             "--format=%x00%as", "--", "projects", "pages"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception:
        return created
    cur_date = None
    for line in out.stdout.splitlines():
        if line.startswith("\x00"):
            cur_date = line[1:].strip()
        elif line.strip() and cur_date:
            created[line.strip()] = cur_date  # last-seen wins = oldest
    return created


def created_for(path, store, created_map):
    rel = os.path.relpath(path, store)
    if rel in created_map:
        return created_map[rel]
    ts = os.path.getmtime(path)
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def process_file(path, store, dry_run, created_map):
    with open(path, "r", encoding="utf-8", newline="") as f:
        text = f.read()

    fm = find_frontmatter(text)
    if not fm:
        return "no-frontmatter"
    eol, body_start, body_end = fm

    if TOP_ACCESS_RE.search(text[body_start:body_end]):
        return "already"

    typ = top_type(text, eol, body_start, body_end)
    if not typ:
        return "no-type"

    # Canonical 4-enum → deterministic importance/pinned. Any other top-level
    # type (e.g. `person` biographical pages) is durable but unclassified —
    # stamp it pinned (decay-exempt, importance max) so the consolidation job
    # never archives a page we could not confidently score.
    if typ in IMPORTANCE:
        importance = IMPORTANCE[typ]
        pinned = typ in PINNED_TYPES
        result = "stamped"
    else:
        importance = 10
        pinned = True
        result = "stamped-unknown"

    created = created_for(path, store, created_map)
    block = (
        f"importance: {importance}{eol}"
        f"pinned: {'true' if pinned else 'false'}{eol}"
        f"created: {created}{eol}"
        f"last_accessed: {created}{eol}"
        f"access_count: 0{eol}"
        f"status: active{eol}"
    )
    if not dry_run:
        new_text = text[:body_end] + block + text[body_end:]
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--store", default=os.environ.get("OKFMEM_STORE",
                    os.path.expanduser("~/okfmem-store")))
    args = ap.parse_args()

    store = os.path.abspath(os.path.expanduser(args.store))
    proj_root = os.path.join(store, "projects")
    pages_root = os.path.join(store, "pages")
    if not os.path.isdir(proj_root) and not os.path.isdir(pages_root):
        print(f"error: no projects/ or pages/ under {store}", file=sys.stderr)
        sys.exit(2)

    created_map = build_created_map(store)
    targets = []
    if os.path.isdir(proj_root):
        for d in sorted(os.listdir(proj_root)):
            p = os.path.join(proj_root, d)
            if os.path.isdir(p):
                targets.append((d, p))
    if os.path.isdir(pages_root):
        targets.append(("pages", pages_root))

    keys = ["scanned", "stamped", "stamped-unknown", "already", "no-type",
            "no-frontmatter", "skip-ck"]
    totals = {k: 0 for k in keys}
    print(f"{'project':22} scan stamp unkwn alrdy notyp nofm  ck")
    for name, target_dir in targets:
        c = {k: 0 for k in keys}
        for path in sorted(glob.glob(os.path.join(target_dir, "**", "*.md"),
                                     recursive=True)):
            base = os.path.basename(path)
            if base in SKIP_NAMES:
                continue
            if base.startswith("ck_"):
                c["skip-ck"] += 1
                continue
            rel = os.path.relpath(path, target_dir)
            if "archive" in rel.split(os.sep)[:-1]:
                continue
            c["scanned"] += 1
            try:
                status = process_file(path, store, args.dry_run, created_map)
            except Exception as e:
                print(f"  !! error {path}: {e}", file=sys.stderr)
                continue
            c[status] += 1
        for k in keys:
            totals[k] += c[k]
        print(f"{name:22} {c['scanned']:4} {c['stamped']:5} "
              f"{c['stamped-unknown']:5} {c['already']:5} "
              f"{c['no-type']:5} {c['no-frontmatter']:4} {c['skip-ck']:3}")
    print(f"{'TOTAL':22} {totals['scanned']:4} {totals['stamped']:5} "
          f"{totals['stamped-unknown']:5} {totals['already']:5} "
          f"{totals['no-type']:5} {totals['no-frontmatter']:4} {totals['skip-ck']:3}")
    print(f"\nstore: {store}")
    print(f"mode: {'DRY-RUN' if args.dry_run else 'APPLY'}")


if __name__ == "__main__":
    main()
