#!/usr/bin/env python3
"""okfmem find — rank durable pages by a query, without reading MEMORY.md.

    okfmem find <term>... [--repo NAME] [--archive] [--limit N] [--related N] [--json] [--store PATH]

Every term must appear somewhere in the page (case-insensitive). A hit in the
slug or `name` outranks one in `description`, which outranks `labels`, which
outranks the body. `--repo` keeps pages labelled `repo:NAME`; `--archive` also
searches `pages/archive/`. Read-only: it never touches `access_count`, so a
search does not reinforce pages against decay. Among ties, a match that other matches
link to ranks first; `--related N` (default 3) appends pages one link from the top
matches, marked `match: link` with `via`.

Exit 0 with matches, 1 with none, 2 on a usage or store error.
Store location: --store, else $OKFMEM_STORE, else ~/okfmem-store.
"""

import argparse
import json
import os
import posixpath
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_frontmatter import LINK_RE, find_frontmatter, parse_fields, parse_labels  # noqa: E402

DEFAULT_STORE = os.environ.get("OKFMEM_STORE", os.path.expanduser("~/okfmem-store"))
# Ranked by per-tier hit counts compared in this order, never summed, so a name
# hit outranks any number of lower-tier hits even across terms; body breaks ties.
TIERS = ("name", "description", "labels")
BODY_CAP = 5
RELATED_DEFAULT = 3
RELATED_FROM = 5
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
        "type": fields.get("type", ""),
        "name": fields.get("name") or slug,
        "description": fields.get("description", ""),
        "labels": labels,
        "last_accessed": fields.get("last_accessed") or fields.get("created") or "",
        "body": body,
    }


def score(page, terms):
    """({tier: terms hitting it}, body hits), or None when any term is missing."""
    slug = page["slug"].lower()
    # The raw slug keeps a hyphenated query like `idle-pool-diagnosis` matching.
    fields = {
        "name": (slug, slug.replace("-", " "), page["name"].lower()),
        "description": (page["description"].lower(),),
        "labels": (" ".join(page["labels"]).lower(),),
    }
    body = page["body"].lower()
    tier_hits = dict.fromkeys(TIERS, 0)
    body_hits = 0
    for term in terms:
        hit = [t for t in TIERS if any(term in s for s in fields[t])]
        b = min(body.count(term), BODY_CAP)
        if not (hit or b):
            return None
        for t in hit:
            tier_hits[t] += 1
        body_hits += b
    return tier_hits, body_hits


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


def page_links(page, archive=False):
    """Keys (`x`, or `archive/x` with `archive`) of the pages this body links to."""
    base = "archive" if page["archived"] else ""
    out = set()
    for target in LINK_RE.findall(page["body"]):
        if "://" in target or target.startswith("/"):
            continue
        head, _, name = posixpath.normpath(posixpath.join(base, target)).rpartition("/")
        if head not in ("", "archive") or (head and not archive):
            continue
        out.add(f"{head}/{name[:-3]}" if head else name[:-3])
    return out


def _entry(path, page, match, **extra):
    return {
        "path": path,
        "tier_hits": dict.fromkeys(TIERS, 0),
        "body_hits": 0,
        "slug": page["slug"],
        "name": page["name"],
        "description": page["description"],
        "labels": page["labels"],
        "last_accessed": page["last_accessed"],
        "snippet": "",
        "match": match,
        **extra,
    }


def find(
    store, terms, repo=None, archive=False, limit=10, related=RELATED_DEFAULT, visible=None
):
    """Direct matches (`match: terms`), then up to `related` pages one link from the
    top matches (`match: link`, `via`). `visible(page)` hides pages before ranking."""
    terms = [t.lower() for t in terms if t.strip()]
    want = f"repo:{repo.lower()}" if repo else None
    pages = {}
    for path in page_files(store, archive):
        page = read_page(path)
        if page is None or (want and want not in page["labels"]):
            continue
        if visible and not visible(page):
            continue
        rel = os.path.relpath(path, store).replace(os.sep, "/")
        page["archived"] = rel.startswith("pages/archive/")
        pages[rel[len("pages/") : -len(".md")]] = (rel, page)
    out_links = {
        k: {t for t in page_links(p, archive) if t in pages and t != k}
        for k, (_, p) in pages.items()
    }
    direct = {}
    for key, (rel, page) in pages.items():
        s = score(page, terms)
        if s:
            direct[key] = _entry(
                rel, page, "terms", tier_hits=s[0], body_hits=s[1], snippet=snippet(page["body"], terms)
            )
    for key, r in direct.items():
        r["links_in"] = sum(1 for o in direct if o != key and key in out_links[o])
    ranked = sorted(
        direct,
        key=lambda k: (
            *(-direct[k]["tier_hits"][t] for t in TIERS),
            -direct[k]["body_hits"],
            -direct[k]["links_in"],
            -_day(direct[k]["last_accessed"]),
            direct[k]["slug"],
        ),
    )
    top = ranked[:RELATED_FROM]
    if limit:
        ranked = ranked[:limit]
    results = [direct[k] for k in ranked]
    if not related or not results:
        return results
    linked = []
    for key, (rel, page) in pages.items():
        if key in direct:
            continue
        via = [t for t in top if key in out_links[t] or t in out_links[key]]
        if via:
            linked.append((rel, page, via))
    linked.sort(key=lambda c: (-len(c[2]), -_day(c[1]["last_accessed"]), c[1]["slug"]))
    for rel, page, via in linked[:related]:
        results.append(_entry(rel, page, "link", via=[pages[t][1]["slug"] for t in via]))
    return results


def format_result(r):
    head = f"{r['path']}  {r['name']}"
    if r["description"] and r["description"] != r["name"]:
        head += f" — {r['description']}"
    lines = [head]
    if r["labels"]:
        lines.append(f"    labels: {', '.join(r['labels'])}")
    if r.get("via"):
        lines.append(f"    via {', '.join(r['via'])}")
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
    ap.add_argument("--limit", type=int, default=10, help="0 lists every match")
    ap.add_argument(
        "--related",
        type=int,
        default=RELATED_DEFAULT,
        help="pages one link from the top matches (0 turns it off)",
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--store", default=DEFAULT_STORE)
    args = ap.parse_args(argv)
    terms = [t for t in args.terms if t.strip()]
    if args.limit < 0:
        print("okfmem find: --limit must be 0 (no cap) or more", file=sys.stderr)
        return 2
    if args.related < 0:
        print("okfmem find: --related must be 0 (off) or more", file=sys.stderr)
        return 2
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
    results = find(args.store, terms, args.repo, args.archive, args.limit, args.related)
    if args.json:
        print(json.dumps(results, indent=2))
    elif results:
        direct = [r for r in results if r["match"] == "terms"]
        linked = [r for r in results if r["match"] == "link"]
        print("\n".join(format_result(r) for r in direct))
        if linked:
            print("related:")
            print("\n".join(format_result(r) for r in linked))
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
