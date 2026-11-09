#!/usr/bin/env python3
"""okfmem find — rank durable pages by a query, without reading MEMORY.md.

    okfmem find <term>... [--repo NAME] [--archive] [--limit N] [--json] [--store PATH]

Every term must appear somewhere in the page (case-insensitive). A hit in the
slug or `name` outranks one in `description`, which outranks `labels`, which
outranks the body. `--repo` keeps pages labelled `repo:NAME`; `--archive` also
searches `pages/archive/`. Read-only: it never touches `access_count`, so a
search does not reinforce pages against decay.

Exit 0 with matches, 1 with none, 2 on a usage or store error.
Store location: --store, else $OKFMEM_STORE, else ~/okfmem-store.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_frontmatter import find_frontmatter, parse_fields, parse_labels  # noqa: E402

DEFAULT_STORE = os.environ.get("OKFMEM_STORE", os.path.expanduser("~/okfmem-store"))
WEIGHTS = {"slug": 6, "name": 5, "description": 3, "labels": 2}
BODY_CAP = 5
SKIP = ("MEMORY.md", "STATE.md", "CONTEXT.md")


def page_files(store, archive):
    pages = os.path.join(store, "pages")
    dirs = [pages] + ([os.path.join(pages, "archive")] if archive else [])
    out = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name)
            if name.endswith(".md") and name not in SKIP and os.path.isfile(path):
                out.append(path)
    return out


def read_page(path):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    fm = find_frontmatter(text)
    fields = parse_fields(text, fm) if fm else {}
    labels = parse_labels(text, fm, warn=None) if fm else []
    body = text[fm[2] :] if fm else text
    slug = os.path.basename(path)[:-3]
    return {
        "slug": slug,
        "name": fields.get("name") or slug,
        "description": fields.get("description", ""),
        "labels": labels,
        "last_accessed": fields.get("last_accessed") or fields.get("created") or "",
        "body": body,
    }


def score(page, terms):
    """Weighted hit count, or 0 when any term is missing from the page."""
    fields = {
        "slug": page["slug"].lower().replace("-", " "),
        "name": page["name"].lower(),
        "description": page["description"].lower(),
        "labels": " ".join(page["labels"]).lower(),
    }
    body = page["body"].lower()
    total = 0
    for term in terms:
        hits = sum(w for k, w in WEIGHTS.items() if term in fields[k])
        hits += min(body.count(term), BODY_CAP)
        if not hits:
            return 0
        total += hits
    return total


def snippet(body, terms, width=140):
    for line in body.splitlines():
        low = line.lower()
        if any(t in low for t in terms) and not line.startswith("---"):
            line = line.strip()
            return line if len(line) <= width else line[: width - 1] + "…"
    return ""


def _day(iso):
    digits = iso.replace("-", "")
    return int(digits) if digits.isdigit() else 0


def find(store, terms, repo=None, archive=False, limit=10):
    terms = [t.lower() for t in terms if t.strip()]
    want = f"repo:{repo.lower()}" if repo else None
    results = []
    for path in page_files(store, archive):
        page = read_page(path)
        if page is None or (want and want not in page["labels"]):
            continue
        s = score(page, terms)
        if s:
            rel = os.path.relpath(path, store).replace(os.sep, "/")
            results.append(
                {
                    "path": rel,
                    "score": s,
                    "slug": page["slug"],
                    "name": page["name"],
                    "description": page["description"],
                    "labels": page["labels"],
                    "last_accessed": page["last_accessed"],
                    "snippet": snippet(page["body"], terms),
                }
            )
    results.sort(key=lambda r: (-r["score"], -_day(r["last_accessed"]), r["slug"]))
    return results[:limit] if limit else results


def format_result(r):
    head = f"{r['path']}  {r['name']}"
    if r["description"] and r["description"] != r["name"]:
        head += f" — {r['description']}"
    lines = [head]
    if r["labels"]:
        lines.append(f"    labels: {', '.join(r['labels'])}")
    if r["snippet"]:
        lines.append(f"    > {r['snippet']}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="okfmem find", description=__doc__.splitlines()[0]
    )
    ap.add_argument("terms", nargs="*")
    ap.add_argument("--repo")
    ap.add_argument("--archive", action="store_true")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--store", default=DEFAULT_STORE)
    args = ap.parse_args(argv)
    terms = args.terms
    if not terms:
        print(
            "okfmem find: give at least one search term, e.g. `okfmem find keychain`",
            file=sys.stderr,
        )
        return 2
    if not os.path.isdir(os.path.join(args.store, "pages")):
        print(
            f"okfmem find: no pages/ under {args.store}; pass --store or run `okfmem migrate-to-labels`",
            file=sys.stderr,
        )
        return 2
    results = find(args.store, terms, args.repo, args.archive, args.limit)
    if args.json:
        print(json.dumps(results, indent=2))
    elif results:
        print("\n".join(format_result(r) for r in results))
    else:
        scope = f" labelled repo:{args.repo}" if args.repo else ""
        hint = "" if args.archive else "; add --archive to search archived pages"
        print(
            f"okfmem find: no page{scope} matches {' '.join(terms)!r}{hint}",
            file=sys.stderr,
        )
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
