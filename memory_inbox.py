#!/usr/bin/env python3
"""okfmem inbox — candidate pages from other machines, reviewed before import (#84).

Headless agent runs write the lessons they learn as OKF pages on the worker
that ran them, never in the store: their input is untrusted and the store is
auto-loaded into the maintainer's sessions. The inbox carries those pages to
the maintainer without letting them anywhere near a loaded page:

  okfmem inbox push <dir> --source NAME [--repo OWNER/REPO] [--ref TEXT] [--machine NAME]
      One parentless commit (the dir's valid `*.md` pages + `meta.json`) pushed
      to `refs/okfmem/inbox/<machine>/<id>` on the store remote. The id comes
      from the pages' content, so a re-push adds nothing; a ref per push means
      machines never race on a branch. The store's working tree, branches,
      MEMORY.md and STATE.md are never touched.
  okfmem inbox pull
      Fetch `refs/okfmem/inbox/*` and list what is pending, by project then slug.
  okfmem inbox review
      Interactive: walk each pending page, flag a slug match or a near-duplicate
      name/description, then accept / merge / drop it. A push whose pages are
      all handled has its ref deleted on the remote. Ends with `okfmem sync`.

Never automatic: `okfmem pull`, `consolidate` and the Stop hook fetch only the
default refspec (branches), so nothing in the inbox reaches a loaded page
without `inbox review`.

Store location: --store, else $OKFMEM_STORE, else ~/okfmem-store.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_consolidate import find_frontmatter, git_clean, parse_fields  # noqa: E402
from memory_init import _load_registry, _seed_store_project  # noqa: E402
from memory_sync import sync_store  # noqa: E402

INBOX_NS = "refs/okfmem/inbox"
REMOTE = "origin"
REQUIRED_FIELDS = ("name", "description", "type")
SKIP_NAMES = {"MEMORY.md", "STATE.md", "CONTEXT.md"}
# Page names come from untrusted refs too, so this also blocks path traversal.
PAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")
REF_PART_RE = re.compile(r"[^A-Za-z0-9._-]+")
SIMILARITY_THRESHOLD = 0.85
NET_TIMEOUT = 60


def _git(store, *args, data=None, timeout=NET_TIMEOUT):
    """Bytes in, bytes out: page content must survive byte-for-byte on Windows,
    where text mode would decode with the locale codepage. A timeout comes back
    as a failed run, so every caller's returncode check covers a stalled remote."""
    cmd = ["git", "-C", store, *args]
    try:
        return subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, 124, b"", f"git {args[0]} timed out after {timeout}s".encode())


def _out(proc):
    return proc.stdout.decode("utf-8", "replace").strip()


def _err(proc):
    return proc.stderr.decode("utf-8", "replace").strip()


def importable_name(name):
    # Case-blind: on macOS and Windows `state.md` is the loaded STATE.md.
    upper = name.upper()
    return (bool(PAGE_NAME_RE.match(name))
            and upper not in {n.upper() for n in SKIP_NAMES}
            and not upper.startswith("MEMORY"))


def _ref_part(text):
    return REF_PART_RE.sub("-", text).strip(".-") or "unknown"


def default_machine():
    return _ref_part(socket.gethostname().split(".")[0].lower())


def page_fields(text):
    """Frontmatter fields of a valid OKF page, or None when it isn't one."""
    fm = find_frontmatter(text)
    if not fm:
        return None
    fields = parse_fields(text, fm)
    if not all(fields.get(k) for k in REQUIRED_FIELDS):
        return None
    return fields


def page_body(text):
    fm = find_frontmatter(text)
    if not fm:
        return text
    rest = text[fm[2]:]
    return re.sub(r"^---[ \t]*\r?\n?", "", rest, count=1).strip("\r\n")


CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")
PREVIEW_LINES = 20


def _safe(text):
    """Untrusted text made inert for the terminal: no escape sequences that
    could hide or spoof what the reviewer reads."""
    return CONTROL_RE.sub("?", str(text))


def _unquote(value):
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------
def collect_pages(src_dir):
    """Returns ([(name, bytes)], [skip message]) for the dir's top-level pages."""
    pages, skipped = [], []
    for name in sorted(os.listdir(src_dir)):
        path = os.path.join(src_dir, name)
        if not name.endswith(".md") or not os.path.isfile(path):
            continue
        if not importable_name(name):
            skipped.append(f"skip {name}: not a page name okfmem imports")
            continue
        with open(path, "rb") as f:
            data = f.read()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(f"skip {name}: not UTF-8")
            continue
        if page_fields(text) is None:
            skipped.append(f"skip {name}: no OKF frontmatter "
                           f"({', '.join(REQUIRED_FIELDS)})")
            continue
        pages.append((name, data))
    return pages, skipped


def content_id(pages):
    h = hashlib.sha256()
    for name, data in sorted(pages):
        h.update(name.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()[:16]


def _hash_blob(store, data):
    r = _git(store, "hash-object", "-w", "--stdin", data=data)
    if r.returncode != 0:
        raise RuntimeError(f"git hash-object failed: {_err(r)}")
    return _out(r)


def build_commit(store, pages, meta):
    """A parentless commit holding the pages + meta.json, written with plumbing
    only so no index, branch or working-tree file of the store changes."""
    entries = [(name, _hash_blob(store, data)) for name, data in pages]
    meta_bytes = (json.dumps(meta, indent=2, sort_keys=True) + "\n").encode("utf-8")
    entries.append(("meta.json", _hash_blob(store, meta_bytes)))
    listing = "".join(f"100644 blob {sha}\t{name}\n" for name, sha in sorted(entries))
    tree = _git(store, "mktree", data=listing.encode("utf-8"))
    if tree.returncode != 0:
        raise RuntimeError(f"git mktree failed: {_err(tree)}")
    msg = f"okfmem inbox: {len(pages)} page(s) from {meta['source']}@{meta['machine']}"
    commit = _git(store, "commit-tree", _out(tree), "-m", msg)
    if commit.returncode != 0:
        raise RuntimeError(f"git commit-tree failed: {_err(commit)}")
    return _out(commit)


def remote_has_ref(store, ref):
    """True/False, or None when the remote can't be read."""
    r = _git(store, "ls-remote", "--refs", REMOTE, ref)
    if r.returncode != 0:
        return None
    return any(line.split("\t")[-1] == ref for line in _out(r).splitlines())


def push(store, src_dir, source, repo=None, ref=None, machine=None, out=print):
    """Returns (exit_code, inbox ref or None)."""
    if not os.path.isdir(os.path.join(store, ".git")):
        out(f"okfmem inbox push: {store} is not a git repo.")
        return 2, None
    if not os.path.isdir(src_dir):
        out(f"okfmem inbox push: {src_dir} is not a directory.")
        return 2, None
    pages, skipped = collect_pages(src_dir)
    for line in skipped:
        out(f"  {line}")
    if not pages:
        out("okfmem inbox push: no valid OKF pages — nothing pushed.")
        return 0, None
    machine = _ref_part(machine) if machine else default_machine()
    inbox_ref = f"{INBOX_NS}/{machine}/{content_id(pages)}"
    present = remote_has_ref(store, inbox_ref)
    if present is None:
        out(f"okfmem inbox push: cannot read remote '{REMOTE}' — nothing pushed.")
        return 1, None
    if present:
        out(f"okfmem inbox push: already in the inbox ({inbox_ref}) — nothing pushed.")
        return 0, inbox_ref
    meta = {"source": source, "repo": repo, "ref": ref, "machine": machine,
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pages": [name for name, _ in pages]}
    try:
        sha = build_commit(store, pages, meta)
    except RuntimeError as e:
        out(f"okfmem inbox push: {e}")
        return 1, None
    p = _git(store, "push", "-q", REMOTE, f"{sha}:{inbox_ref}")
    if p.returncode != 0:
        out(f"okfmem inbox push: push failed: {_err(p)}")
        return 1, None
    out(f"okfmem inbox push: {len(pages)} page(s) -> {inbox_ref}")
    return 0, inbox_ref


# ---------------------------------------------------------------------------
# pull / pending listing
# ---------------------------------------------------------------------------
def fetch_inbox(store):
    """Mirror the remote's inbox refs locally (pruning handled ones). True on success."""
    r = _git(store, "fetch", "-q", "--prune", REMOTE,
             f"+{INBOX_NS}/*:{INBOX_NS}/*")
    return r.returncode == 0, _err(r)


def _normalise_repo(url):
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url.strip())
    return m.group(1).lower() if m else None


def resolve_repo_project(store, repo):
    """Store project for `owner/repo`: a registered checkout whose origin is that
    repo, else a registered root or store project named like the repo; else None."""
    if not repo:
        return None
    want = repo.strip().lower()
    reg = _load_registry(os.path.join(store, "registry.json"))
    entries = {**reg.get("map", {}), **reg.get("overrides", {})}
    for root, name in sorted(entries.items()):
        if not os.path.isdir(root):
            continue
        try:
            r = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and _normalise_repo(r.stdout) == want:
            return name
    base = want.rsplit("/", 1)[-1]
    for root, name in sorted(entries.items()):
        if os.path.basename(os.path.normpath(root)).lower() == base:
            return name
    proj_root = os.path.join(store, "projects")
    if os.path.isdir(proj_root):
        for d in sorted(os.listdir(proj_root)):
            if d.lower() == base and os.path.isdir(os.path.join(proj_root, d)):
                return d
    return None


def pending(store):
    """Every pending push, oldest first: dicts with ref, sha, meta, project, pages
    (each page: name, slug, text, fields)."""
    r = _git(store, "for-each-ref", "--format=%(refname) %(objectname)", INBOX_NS)
    if r.returncode != 0:
        return []
    pushes = []
    for line in _out(r).splitlines():
        ref, sha = line.rsplit(" ", 1)
        m = _git(store, "show", f"{sha}:meta.json")
        try:
            meta = json.loads(m.stdout.decode("utf-8")) if m.returncode == 0 else {}
        except (ValueError, UnicodeDecodeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        names = _git(store, "ls-tree", "--name-only", sha)
        pages = []
        for name in _out(names).splitlines():
            if not importable_name(name):
                continue
            blob = _git(store, "show", f"{sha}:{name}")
            if blob.returncode != 0:
                continue
            text = blob.stdout.decode("utf-8", "replace")
            fields = page_fields(text)
            if fields is None:
                continue
            pages.append({"name": name, "slug": name[:-3], "text": text,
                          "fields": fields})
        pushes.append({"ref": ref, "sha": sha, "meta": meta, "pages": pages,
                       "project": resolve_repo_project(store, meta.get("repo"))})
    pushes.sort(key=lambda p: (str(p["meta"].get("time") or ""), p["ref"]))
    return pushes


def _origin_line(meta):
    return (f"source={meta.get('source') or '?'} repo={meta.get('repo') or '-'} "
            f"ref={meta.get('ref') or '-'} machine={meta.get('machine') or '?'}")


def render_pending(pushes):
    rows = []
    for p in pushes:
        project = p["project"] or f"(unresolved: {p['meta'].get('repo') or 'no repo'})"
        for page in p["pages"]:
            rows.append((project, page["slug"], p["meta"]))
    if not rows:
        return ["okfmem inbox: nothing pending."]
    lines = [f"okfmem inbox: {len(rows)} pending page(s) in {len(pushes)} push(es)"]
    current = None
    for project, slug, meta in sorted(rows, key=lambda r: (r[0], r[1])):
        if project != current:
            lines.append(f"{project}:")
            current = project
        lines.append(f"  {slug}  ({_origin_line(meta)})")
    return lines


def pull(store, out=print):
    ok, err = fetch_inbox(store)
    if not ok:
        out(f"okfmem inbox pull: fetch failed ({err or 'offline?'}) — "
            "listing what was fetched before.")
    for line in render_pending(pending(store)):
        out(line)
    return 0


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------
def _norm(text):
    return re.sub(r"\s+", " ", _unquote(text or "")).strip().lower()


def _similar(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= SIMILARITY_THRESHOLD


def project_pages(store, project):
    """{slug: (path, fields)} for a project's live pages (archive excluded)."""
    found = {}
    proj_dir = os.path.join(store, "projects", project) if project else None
    if not proj_dir or not os.path.isdir(proj_dir):
        return found
    for dirpath, dirnames, filenames in os.walk(proj_dir):
        dirnames[:] = [d for d in dirnames if d != "archive" and not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".md") or fn in SKIP_NAMES or fn.startswith("MEMORY"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    fields = page_fields(f.read()) or {}
            except (OSError, UnicodeDecodeError):
                fields = {}
            found.setdefault(fn[:-3], (path, fields))
    return found


def flags_for(page, existing, others):
    """Human-readable reasons this candidate needs a closer look."""
    out = []
    if page["slug"] in existing:
        out.append(f"slug matches existing page {page['slug']}.md")
    name, desc = page["fields"].get("name"), page["fields"].get("description")
    pool = [("existing page", slug, fields) for slug, (_, fields) in existing.items()
            if slug != page["slug"]]
    pool += [("pending page", label, fields) for label, fields in others]
    for kind, label, fields in pool:
        if _similar(name, fields.get("name")):
            out.append(f"name close to {kind} {label}")
        elif _similar(desc, fields.get("description")):
            out.append(f"description close to {kind} {label}")
    return out


def accept_page(store, project, page):
    proj_dir = os.path.join(store, "projects", project)
    if not os.path.isdir(proj_dir):
        _seed_store_project(proj_dir, project)
    dest = os.path.join(proj_dir, page["name"])
    if os.path.exists(dest):
        return None
    with open(dest, "w", encoding="utf-8", newline="") as f:
        f.write(page["text"])
    title = _unquote(page["fields"]["name"])
    hook = _unquote(page["fields"]["description"])
    memory_md = os.path.join(proj_dir, "MEMORY.md")
    with open(memory_md, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    eol = "\r\n" if "\r\n" in text else "\n"
    if text and not text.endswith(("\n", "\r\n")):
        text += eol
    text += f"- [{title}]({page['name']}) — {hook}{eol}"
    with open(memory_md, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return dest


def merge_page(store, project, target_slug, page, meta, today):
    pages = project_pages(store, project)
    if target_slug not in pages:
        return None
    path = pages[target_slug][0]
    with open(path, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    eol = "\r\n" if "\r\n" in text else "\n"
    origin = f"{meta.get('source') or '?'}@{meta.get('machine') or '?'}"
    if meta.get("repo"):
        origin += f", {meta['repo']}"
    if meta.get("ref"):
        origin += f" {meta['ref']}"
    body = eol.join(page_body(page["text"]).splitlines())
    # A push the remote still lists after a failed ref update re-offers its
    # merged pages; merging one again must not append a second copy.
    if body and body in text:
        return path
    text = (text.rstrip("\r\n") + eol + eol
            + f"## Merged from inbox — {today} ({origin}){eol}{eol}"
            + body + eol)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


def delete_push(store, ref):
    """The local ref goes even when the remote refuses, so handled pages leave
    the pending list; the remote copy comes back only with the next fetch."""
    r = _git(store, "push", "-q", REMOTE, f":{ref}")
    _git(store, "update-ref", "-d", ref)
    if r.returncode != 0:
        return False, _err(r)
    return True, ""


def shrink_push(store, push_, remaining):
    """Re-point a partly reviewed push at only its unhandled pages, so a later
    review never offers an accepted or merged page again."""
    pages = [(p["name"], p["text"].encode("utf-8")) for p in remaining]
    meta = dict(push_["meta"], pages=[name for name, _ in pages])
    try:
        sha = build_commit(store, pages, meta)
    except RuntimeError as e:
        return False, str(e)
    r = _git(store, "push", "-q",
             f"--force-with-lease={push_['ref']}:{push_['sha']}",
             REMOTE, f"{sha}:{push_['ref']}")
    _git(store, "update-ref", push_["ref"], sha)
    if r.returncode != 0:
        return False, _err(r)
    return True, ""


def record_handled(store, push_, handled, counts, out):
    """Clear a push's handled pages from its ref: delete it when none are left,
    else shrink it to the rest. A push with nothing handled is left alone,
    unless it holds no importable page at all."""
    if not handled and push_["pages"]:
        return
    remaining = [p for p in push_["pages"] if p["name"] not in handled]
    if not remaining:
        ok, err = delete_push(store, push_["ref"])
        if ok:
            counts["deleted"] += 1
        else:
            out(f"  could not delete {push_['ref']} on the remote: {err}")
    else:
        ok, err = shrink_push(store, push_, remaining)
        if not ok:
            out(f"  could not update {push_['ref']} on the remote: {err}")


def show_page(project, push_, page, flags, out):
    out("")
    out(_safe(f"{project}/{page['slug']}  {_origin_line(push_['meta'])}"))
    out(f"  name: {_safe(page['fields'].get('name'))}")
    out(f"  description: {_safe(page['fields'].get('description'))}")
    body = page_body(page["text"]).splitlines()
    for line in body[:PREVIEW_LINES]:
        out(f"  | {_safe(line)}")
    if len(body) > PREVIEW_LINES:
        out(f"  | … {len(body) - PREVIEW_LINES} more line(s)")
    for fl in flags:
        out(f"  ! {_safe(fl)}")


def review_page(store, project, push_, page, all_pending, decide, counts, out, today):
    """Show one candidate and act on the decision. True when it was handled
    (accepted, merged or dropped), False when skipped."""
    existing = project_pages(store, project)
    others = [(f"{q['slug']} ({o['meta'].get('machine') or '?'})", q["fields"])
              for o, q in all_pending if q is not page]
    flags = flags_for(page, existing, others)
    show_page(project, push_, page, flags, out)
    while True:
        action, arg = decide(push_, page, flags)
        if action == "accept":
            dest = accept_page(store, project, page)
            if dest is None:
                out(f"  {page['name']} already exists in {project} — "
                    "merge or drop it instead")
                continue
            out(f"  accepted -> {os.path.relpath(dest, store)}")
            counts["accepted"] += 1
        elif action == "merge":
            slug = (arg or "").removesuffix(".md")
            dest = (merge_page(store, project, slug, page, push_["meta"], today)
                    if PAGE_NAME_RE.match(slug + ".md") else None)
            if dest is None:
                out(f"  no page {_safe(slug)!r} in {project}")
                continue
            out(f"  merged into {os.path.relpath(dest, store)}")
            counts["merged"] += 1
        elif action == "drop":
            out("  dropped")
            counts["dropped"] += 1
        else:
            out("  skipped — left pending")
            return False
        return True


def review(store, decide, ask_project, out=print, today=None, do_sync=True):
    """Walk every pending page. `decide(push, page, flags)` returns one of
    ("accept", None), ("merge", slug), ("drop", None), ("skip", None);
    `ask_project(repo)` names a project when the repo resolves to none."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    ok, err = fetch_inbox(store)
    if not ok:
        out(f"okfmem inbox review: fetch failed ({err or 'offline?'}) — "
            "reviewing what was fetched before.")
    pushes = pending(store)
    counts = {"accepted": 0, "merged": 0, "dropped": 0, "deleted": 0}
    if not pushes:
        out("okfmem inbox: nothing pending.")
        return counts
    all_pending = [(p, page) for p in pushes for page in p["pages"]]
    for push_ in pushes:
        project = push_["project"]
        if project is None and push_["pages"]:
            project = ask_project(push_["meta"].get("repo"))
            if project and not PAGE_NAME_RE.match(project + ".md"):
                out(f"  invalid project name {project!r}")
                project = None
            if not project:
                out(f"  {push_['ref']}: no project — left pending")
                continue
        handled = set()
        # finally: a Ctrl-C mid-push still clears the pages already written,
        # or the next review would offer them again.
        try:
            for page in push_["pages"]:
                if review_page(store, project, push_, page, all_pending, decide,
                               counts, out, today):
                    handled.add(page["name"])
        finally:
            record_handled(store, push_, handled, counts, out)
    out("")
    out(f"okfmem inbox review: accepted {counts['accepted']}, merged "
        f"{counts['merged']}, dropped {counts['dropped']}; "
        f"{counts['deleted']} push(es) cleared")
    if do_sync and (counts["accepted"] or counts["merged"]):
        res = sync_store(store, f"okfmem inbox: accepted {counts['accepted']}, "
                                f"merged {counts['merged']}")
        out(f"okfmem sync: {res['reason']}")
        if res["push_error"]:
            out(f"  push failed: {res['push_error']}")
    return counts


def _interactive_decide(push_, page, flags):
    while True:
        try:
            ans = input("  [a]ccept / [m]erge <slug> / [d]rop / [s]kip: ").strip()
        except EOFError:
            return ("skip", None)
        word, _, rest = ans.partition(" ")
        word = word.lower()
        if word in ("a", "accept"):
            return ("accept", None)
        if word in ("m", "merge"):
            target = rest.strip()
            if not target:
                try:
                    target = input("  merge into slug: ").strip()
                except EOFError:
                    return ("skip", None)
            return ("merge", target)
        if word in ("d", "drop"):
            return ("drop", None)
        if word in ("s", "skip", ""):
            return ("skip", None)


def _interactive_project(repo):
    try:
        return input(f"  project for repo {repo or '(none)'}: ").strip() or None
    except EOFError:
        return None


def _isatty():
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        return False


def main():
    ap = argparse.ArgumentParser(prog="okfmem inbox",
                                 description="Candidate pages pushed from other "
                                             "machines, reviewed before import.")
    ap.add_argument("--store", default=os.environ.get(
        "OKFMEM_STORE", os.path.expanduser("~/okfmem-store")))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("push", help="push a directory's OKF pages to the inbox")
    p.add_argument("dir")
    p.add_argument("--source", required=True, help="what produced the pages")
    p.add_argument("--repo", help="owner/repo the pages are about")
    p.add_argument("--ref", help="free text: issue, PR or run the pages came from")
    p.add_argument("--machine", help="this machine's name (default: hostname)")
    p.add_argument("--store", default=argparse.SUPPRESS)
    q = sub.add_parser("pull", help="fetch the inbox and list pending pages")
    q.add_argument("--store", default=argparse.SUPPRESS)
    r = sub.add_parser("review", help="accept, merge or drop each pending page")
    r.add_argument("--store", default=argparse.SUPPRESS)
    args = ap.parse_args()
    store = os.path.abspath(os.path.expanduser(args.store))

    if args.cmd == "push":
        code, _ = push(store, args.dir, args.source, args.repo, args.ref, args.machine)
        sys.exit(code)
    if args.cmd == "pull":
        sys.exit(pull(store))
    if not _isatty():
        print("okfmem inbox review: interactive only — run it in a terminal: "
              f"okfmem inbox review --store {store}")
        sys.exit(2)
    if not git_clean(store):
        print("okfmem inbox review: store working tree is dirty — run "
              "`okfmem sync` first so the review's commit holds only its own pages.")
        sys.exit(3)
    try:
        review(store, _interactive_decide, _interactive_project)
    except KeyboardInterrupt:
        print("\nokfmem inbox review: interrupted — handled pages stay written; "
              "run `okfmem sync` to commit them.")
        sys.exit(130)
    sys.exit(0)


if __name__ == "__main__":
    main()
