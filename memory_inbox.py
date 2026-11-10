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
      MEMORY.md and STATE.md are never touched. With --json, stdout is one object
      {"ref", "pages", "created"} (or {"error"} and a non-zero exit); text goes to stderr.
  okfmem inbox pull
      Fetch `refs/okfmem/inbox/*` and list what is pending, by project then slug.
  okfmem inbox review [--into STORE]
      Interactive: walk each pending page, flag a slug match or a near-duplicate
      name/description, then accept / merge / drop it. A page the leak gate hits
      can only be dropped or skipped. A push whose pages are all handled has its
      ref deleted on the remote. Ends with `okfmem sync`.

  okfmem inbox import [--into STORE] --non-interactive --policy FILE [--json]
      Import, without a terminal, only the candidates a written policy passes
      (allowed types and repos, size cap, leak gate always on, no existing slug);
      the rest stay in the inbox with a reason each. `--into` names an attached
      store; imported pages carry source/machine/ref/imported_at/imported_by (#141).

  okfmem inbox dump --out DIR [--into STORE] [--json]
      Read-only: every pending page as one record of DIR/pending.jsonl (id, ref,
      sha, meta, project, slug, text, near, leaks), for a judge outside the engine.
  okfmem inbox apply DIR/decisions.json [--into STORE] [--yes]
      Validate the decisions against the dump beside them (every id covered once,
      valid slugs, merge targets, no existing new slug, replaces found once), show
      a summary, ask [y/N], then write pages, delete the handled refs leased on
      their dumped sha in one push, rebuild views and `okfmem sync` (#165).

Never automatic: `okfmem pull`, `consolidate` and the Stop hook fetch only the
default refspec (branches), so nothing in the inbox reaches a loaded page
without `inbox review`.

Store location: --store, else $OKFMEM_STORE, else ~/okfmem-store.
"""
import argparse
import difflib
import getpass
import hashlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_consolidate import find_frontmatter, git_clean, parse_fields  # noqa: E402
from memory_frontmatter import merge_labels, normalize_label, parse_labels
from memory_init import (  # noqa: E402
    _load_registry,
    _resolve_aliases,
    _resolve_overrides,
    _seed_store_project,
    attached_stores,
    compile_view,
    has_layout_marker,
    resolve_project,
)
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
    # .get: pending() drops non-string meta values, so a shrunk push may lack them.
    msg = (f"okfmem inbox: {len(pages)} page(s) from "
           f"{meta.get('source') or '?'}@{meta.get('machine') or '?'}")
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


def push(store, src_dir, source, repo=None, ref=None, machine=None, out=print,
         result=None):
    """Returns (exit_code, inbox ref or None). `result`, when given, is filled with
    the machine-readable outcome: pages, created, and error on failure."""
    result = {} if result is None else result
    result.pop("error", None)
    result.update(pages=0, created=False)

    def fail(code, message):
        result["error"] = message
        out(f"okfmem inbox push: {message}")
        return code, None

    if not os.path.isdir(os.path.join(store, ".git")):
        return fail(2, f"{store} is not a git repo.")
    if not os.path.isdir(src_dir):
        return fail(2, f"{src_dir} is not a directory.")
    try:
        pages, skipped = collect_pages(src_dir)
    except OSError as e:
        return fail(1, str(e))
    for line in skipped:
        out(f"  {line}")
    if not pages:
        out("okfmem inbox push: no valid OKF pages — nothing pushed.")
        return 0, None
    result["pages"] = len(pages)
    machine = _ref_part(machine) if machine else default_machine()
    inbox_ref = f"{INBOX_NS}/{machine}/{content_id(pages)}"
    present = remote_has_ref(store, inbox_ref)
    if present is None:
        return fail(1, f"cannot read remote '{REMOTE}' — nothing pushed.")
    if present:
        out(f"okfmem inbox push: already in the inbox ({inbox_ref}) — nothing pushed.")
        return 0, inbox_ref
    meta = {"source": source, "repo": repo, "ref": ref, "machine": machine,
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pages": [name for name, _ in pages]}
    try:
        sha = build_commit(store, pages, meta)
    except RuntimeError as e:
        return fail(1, str(e))
    p = _git(store, "push", "-q", REMOTE, f"{sha}:{inbox_ref}")
    if p.returncode != 0:
        return fail(1, f"push failed: {_err(p)}")
    result["created"] = True
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


class InboxReadError(RuntimeError):
    """The pending listing could not be read; callers must not act on a partial one."""


def _cat_file_batch(store, specs):
    """Contents of each object spec from one `git cat-file --batch` process
    (bytes, or None when missing), in order."""
    if not specs:
        return []
    r = _git(store, "cat-file", "--batch", data=("\n".join(specs) + "\n").encode(),
             timeout=max(NET_TIMEOUT, len(specs) // 100))
    if r.returncode != 0:
        # An empty listing would make `review` delete every push as page-less.
        raise InboxReadError(_err(r) or f"git cat-file exited {r.returncode}")
    out, pos = [], 0
    buf = r.stdout
    for _ in specs:
        end = buf.find(b"\n", pos)
        if end < 0:
            out.append(None)
            continue
        header = buf[pos:end].split()
        pos = end + 1
        if len(header) != 3:
            out.append(None)
            continue
        size = int(header[2])
        out.append(buf[pos:pos + size])
        pos += size + 1
    return out


def _parse_tree(data, raw_len):
    """[(name, hex blob sha)] for the blob entries of a raw tree object."""
    entries, pos = [], 0
    while pos < len(data):
        nul = data.index(b"\0", pos)
        mode, _, name = data[pos:nul].partition(b" ")
        sha = data[nul + 1:nul + 1 + raw_len].hex()
        pos = nul + 1 + raw_len
        if mode.startswith(b"100"):
            entries.append((name.decode("utf-8", "replace"), sha))
    return entries


def pending(store, dest=None):
    """Every pending push, oldest first: dicts with ref, sha, meta, project, pages
    (each page: name, slug, text, fields). With `dest`, a push's project is
    resolved in that target store first, then in `store`."""
    r = _git(store, "for-each-ref", "--format=%(refname) %(objectname)", INBOX_NS)
    if r.returncode != 0:
        return []
    refs = [line.rsplit(" ", 1) for line in _out(r).splitlines()]
    trees = _cat_file_batch(store, [f"{sha}^{{tree}}" for _, sha in refs])
    entries = []  # per push: [(name, blob sha)]
    for (_, sha), tree in zip(refs, trees):
        entries.append(_parse_tree(tree, len(sha) // 2) if tree is not None else [])
    wanted = []
    for ents in entries:
        wanted.extend(b for n, b in ents if n == "meta.json" or importable_name(n))
    blobs = dict(zip(wanted, _cat_file_batch(store, wanted)))
    projects = {}
    pushes = []
    for (ref, sha), ents in zip(refs, entries):
        raw_meta = next((blobs.get(b) for n, b in ents if n == "meta.json"), None)
        try:
            meta = json.loads(raw_meta.decode("utf-8")) if raw_meta is not None else {}
        except (ValueError, UnicodeDecodeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta = {k: v for k, v in meta.items() if isinstance(v, str)}
        pages = []
        for name, blob_sha in ents:
            if not importable_name(name) or blobs.get(blob_sha) is None:
                continue
            text = blobs[blob_sha].decode("utf-8", "replace")
            fields = page_fields(text)
            if fields is None:
                continue
            pages.append({"name": name, "slug": name[:-3], "text": text,
                          "fields": fields})
        repo = meta.get("repo")
        if repo not in projects:
            in_dest = (resolve_repo_project(dest, repo)
                       if dest and dest != store else None)
            projects[repo] = in_dest or resolve_repo_project(store, repo)
        pushes.append({"ref": ref, "sha": sha, "meta": meta, "pages": pages,
                       "project": projects[repo]})
    pushes.sort(key=lambda p: (str(p["meta"].get("time") or ""), p["ref"]))
    return pushes


def _origin_line(meta):
    return _safe(f"source={meta.get('source') or '?'} repo={meta.get('repo') or '-'} "
                 f"ref={meta.get('ref') or '-'} machine={meta.get('machine') or '?'}")


def render_pending(pushes):
    rows = []
    for p in pushes:
        project = p["project"] or _safe(
            f"(unresolved: {p['meta'].get('repo') or 'no repo'})")
        for page in p["pages"]:
            rows.append((project, page["slug"], p["meta"]))
    if not rows:
        return ["okfmem inbox: nothing pending."]
    lines = [f"okfmem inbox: {len(rows)} pending page(s) in {len(pushes)} push(es)"]
    page_counts, push_counts = {}, {}
    for p in pushes:
        if not p["pages"]:
            continue
        project = p["project"] or _safe(
            f"(unresolved: {p['meta'].get('repo') or 'no repo'})")
        page_counts[project] = page_counts.get(project, 0) + len(p["pages"])
        push_counts[project] = push_counts.get(project, 0) + 1
    for project in sorted(page_counts):
        lines.append(f"{project}: {page_counts[project]:,} pages in "
                     f"{push_counts[project]:,} pushes")
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
    try:
        lines = render_pending(pending(store))
    except InboxReadError as e:
        out(f"okfmem inbox pull: could not read the inbox ({_safe(e)}) — "
            "nothing listed.")
        return 1
    for line in lines:
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
    if project and has_layout_marker(store):
        pages_dir = os.path.join(store, "pages")
        if not os.path.isdir(pages_dir):
            return found
        reg = _load_registry(os.path.join(store, "registry.json"))
        labels = {f"repo:{project}".lower(), *_resolve_aliases(reg, project)}
        with os.scandir(pages_dir) as entries:
            for entry in entries:
                if not importable_name(entry.name) or not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    with open(entry.path, encoding="utf-8") as f:
                        text = f.read()
                except (OSError, UnicodeDecodeError):
                    continue
                if labels.intersection(parse_labels(text, warn=None)):
                    found[entry.name[:-3]] = (entry.path, page_fields(text) or {})
        return found

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


def _close_by(page, fields):
    """The field ("name", then "description") close to the page's, else None."""
    if _similar(page["fields"].get("name"), fields.get("name")):
        return "name"
    if _similar(page["fields"].get("description"), fields.get("description")):
        return "description"
    return None


def near_existing(page, existing):
    """[(slug, why)] for the existing pages a candidate duplicates: why is
    "slug", "name" or "description"."""
    out = [(page["slug"], "slug")] if page["slug"] in existing else []
    for slug, (_, fields) in existing.items():
        why = _close_by(page, fields) if slug != page["slug"] else None
        if why:
            out.append((slug, why))
    return out


def flags_for(page, existing, others):
    """Human-readable reasons this candidate needs a closer look."""
    out = []
    for slug, why in near_existing(page, existing):
        out.append(f"slug matches existing page {slug}.md" if why == "slug"
                   else f"{why} close to existing page {slug}")
    for label, fields in others:
        why = _close_by(page, fields)
        if why:
            out.append(f"{why} close to pending page {label}")
    return out


def accept_page(store, project, page):
    if has_layout_marker(store):
        pages_dir = os.path.join(store, "pages")
        os.makedirs(pages_dir, exist_ok=True)
        dest = os.path.join(pages_dir, page["name"])
        text = merge_labels(page["text"], [f"repo:{project}"])
        try:
            with open(dest, "x", encoding="utf-8", newline="") as f:
                f.write(text)
        except FileExistsError:
            return None
        return dest

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


PROVENANCE_KEYS = ("source", "machine", "ref", "imported_at", "imported_by")


def default_importer():
    try:
        return getpass.getuser() or "unknown"
    except (KeyError, OSError, ImportError):
        return "unknown"


def _yaml_scalar(value):
    """Untrusted text as one double-quoted frontmatter scalar: no newline or
    quote can escape it into another key."""
    return json.dumps(" ".join(str(value).split()), ensure_ascii=False)


def with_provenance(text, meta, imported_by, now=None):
    """`text` with source/machine/ref/imported_at/imported_by set in its
    frontmatter. Candidate-supplied values for those keys are replaced, so a
    page cannot claim a provenance it was not imported with."""
    fm = find_frontmatter(text)
    if not fm:
        raise ValueError("page has no frontmatter")
    eol, body_start, body_end = fm
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    values = {"source": meta.get("source") or "unknown",
              "machine": meta.get("machine") or "unknown",
              "ref": meta.get("ref") or "unknown",
              "imported_at": now, "imported_by": imported_by}
    lines = text[body_start:body_end].split(eol)
    if lines and lines[-1] == "":
        lines.pop()
    keep = [ln for ln in lines
            if not any(re.match(rf"{k}[ \t]*:", ln) for k in PROVENANCE_KEYS)]
    keep += [f"{k}: {_yaml_scalar(values[k])}" for k in PROVENANCE_KEYS]
    return text[:body_start] + eol.join(keep) + eol + text[body_end:]


def with_provenance_page(page, meta, imported_by):
    return dict(page, text=with_provenance(page["text"], meta, imported_by))


# ---------------------------------------------------------------------------
# non-interactive import under a written policy
# ---------------------------------------------------------------------------
POLICY_KEYS = ("allowed_types", "allowed_repos", "max_page_bytes")
_POLICY_LIST_KEYS = ("allowed_types", "allowed_repos")


class PolicyError(ValueError):
    """The policy file is missing a key, has an unknown one, or is malformed."""


def _policy_item(raw, where):
    v = _unquote(raw.strip())
    if not v or any(c in v for c in "[]{},#"):
        raise PolicyError(f"{where}: bad list item {raw.strip()!r}")
    return v


def parse_policy(text):
    """Strict YAML subset: `key: [a, b]` or a `- a` block for the two allowlists,
    `max_page_bytes: N`, `#` comment lines. Every key is required, none may
    repeat, and anything else refuses the import."""
    values, current = {}, None
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        where = f"line {n}"
        item = re.match(r"^\s+-\s+(.*)$", line)
        if item:
            if current not in _POLICY_LIST_KEYS:
                raise PolicyError(f"{where}: list item outside an allowlist")
            values[current].append(_policy_item(item.group(1), where))
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):[ \t]*(.*?)\s*$", line)
        if not m:
            raise PolicyError(f"{where}: cannot parse {line.strip()[:40]!r}")
        key, rest = m.groups()
        if key not in POLICY_KEYS:
            raise PolicyError(f"unknown key '{key}'")
        if key in values:
            raise PolicyError(f"duplicate key '{key}'")
        current = key
        if key == "max_page_bytes":
            if not re.fullmatch(r"[1-9][0-9]*", rest):
                raise PolicyError(f"'{key}' must be a positive integer")
            values[key] = int(rest)
        elif rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1].strip()
            values[key] = [_policy_item(s, where) for s in inner.split(",")] if inner else []
        elif rest == "":
            values[key] = []
        else:
            raise PolicyError(f"'{key}' must be a list")
    for key in POLICY_KEYS:
        if key not in values:
            raise PolicyError(f"missing key '{key}'")
    return values


def load_policy(path):
    try:
        with open(path, encoding="utf-8") as f:
            return parse_policy(f.read())
    except (OSError, UnicodeDecodeError) as e:
        raise PolicyError(f"cannot read policy {path}: {e}")


SECRET_PATTERNS = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("API token", re.compile(r"\b(?:sk|xox[abp])-[A-Za-z0-9-]{20,}")),
    ("credential assignment",
     re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"]?[A-Za-z0-9/+_.-]{12,}")),
)


def _repo_leak_patterns():
    """Patterns of the repo's own leak gate, when this engine runs from a
    checkout that ships scripts/check-leaks.py."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "scripts", "check-leaks.py")
    if not os.path.isfile(path):
        return []
    try:
        spec = importlib.util.spec_from_file_location("okfmem_check_leaks", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return list(mod.PATTERNS)
    except Exception:
        return []


def leak_hits(text):
    """Labels of every secret or private-string pattern the page text matches."""
    return [label for label, pat in (*SECRET_PATTERNS, *_repo_leak_patterns())
            if pat.search(text)]


def policy_reasons(policy, push_, page, project, dest_pages):
    """One reason per failed check; empty means the candidate may be imported."""
    reasons = []
    typ = _unquote(page["fields"].get("type") or "").lower()
    if typ not in {t.lower() for t in policy["allowed_types"]}:
        reasons.append(f"type '{typ}' is not allowed")
    repo = (push_["meta"].get("repo") or "").lower()
    allowed = {r.lower() for r in policy["allowed_repos"]}
    if not repo:
        reasons.append("no repo: label (push carries no repo)")
    elif repo not in allowed and (project or "").lower() not in allowed:
        reasons.append(f"repo '{repo}' is not allowed")
    if not project:
        reasons.append("repo resolves to no project in the target or home store")
    size = len(page["text"].encode("utf-8"))
    if size > policy["max_page_bytes"]:
        reasons.append(f"size {size} bytes exceeds cap {policy['max_page_bytes']}")
    hits = leak_hits(page["text"])
    if hits:
        reasons.append("leak gate: " + ", ".join(hits))
    if page["slug"] in dest_pages:
        reasons.append(f"slug '{page['slug']}' already exists — hold for review")
    return reasons


def import_policy(store, dest, policy, imported_by, out=print, do_sync=True):
    """Import every pending candidate that passes `policy` into `dest`; the rest
    stay in the inbox. Returns {imported, held, skipped, held_pages}."""
    result = {"imported": 0, "held": 0, "skipped": 0, "held_pages": []}
    ok, err = fetch_inbox(store)
    if not ok:
        out(f"okfmem inbox import: fetch failed ({err or 'offline?'}) — "
            "importing what was fetched before.")
    try:
        pushes = pending(store, dest)
    except InboxReadError as e:
        out(f"okfmem inbox import: could not read the inbox ({_safe(e)}) — "
            "nothing imported or deleted.")
        result["error"] = str(e)
        return result
    changed, counts = set(), {"accepted": 0, "merged": 0, "dropped": 0, "deleted": 0}
    try:
        for push_ in pushes:
            project = push_["project"]
            handled = set()
            for page in push_["pages"]:
                dest_pages = project_pages(dest, project) if project else {}
                reasons = policy_reasons(policy, push_, page, project, dest_pages)
                if not reasons:
                    path = accept_page(dest, project,
                                       with_provenance_page(page, push_["meta"], imported_by))
                    if path is None:
                        reasons = [f"slug '{page['slug']}' already exists — hold for review"]
                if reasons:
                    result["held"] += 1
                    result["held_pages"].append({"ref": push_["ref"], "page": page["name"],
                                                 "reasons": reasons})
                    out(_safe(f"  held {page['name']} ({push_['ref']}): " + "; ".join(reasons)))
                    continue
                handled.add(page["name"])
                changed.add(project)
                result["imported"] += 1
                out(f"  imported -> {os.path.relpath(path, dest)}")
            record_handled(store, push_, handled, counts, out)
            if not push_["pages"]:
                result["skipped"] += 1
    finally:
        if changed and has_layout_marker(dest):
            _refresh_project_views(dest, changed, out)
    out(f"okfmem inbox import: imported {result['imported']}, held {result['held']}, "
        f"skipped {result['skipped']}")
    if do_sync and result["imported"]:
        res = sync_store(dest, f"okfmem inbox: imported {result['imported']} ({imported_by})")
        out(f"okfmem sync: {res['reason']}")
        if res["push_error"]:
            out(f"  push failed: {res['push_error']}")
    return result


def resolve_into(store, name):
    """Path of the store attached under `name`, or (None, why). Fail closed."""
    attached = attached_stores(store)
    for a in attached:
        if a["name"].casefold() == name.casefold():
            if not os.path.isdir(os.path.join(a["path"], ".git")):
                return None, f"attached store '{a['name']}' is not a git clone at {a['path']}"
            return a["path"], None
    names = ", ".join(a["name"] for a in attached) or "none"
    return None, f"no attached store named '{name}' (attached: {names})"


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


def review_page(store, project, push_, page, all_pending, decide, counts, out, today,
                dest_store=None, imported_by=None):
    """Show one candidate and return the action taken, or None if skipped.
    Accepted and merged pages land in `dest_store` (default: `store`)."""
    dest_store = dest_store or store
    imported_by = imported_by or default_importer()
    existing = project_pages(dest_store, project)
    others = [(f"{q['slug']} ({o['meta'].get('machine') or '?'})", q["fields"])
              for o, q in all_pending if q is not page]
    flags = flags_for(page, existing, others)
    leaks = leak_hits(page["text"])
    if leaks:
        flags.append("leak gate: " + ", ".join(leaks) + " — cannot be accepted or merged")
    show_page(project, push_, page, flags, out)
    while True:
        action, arg = decide(push_, page, flags)
        if action in ("accept", "merge") and leaks:
            out("  leak gate: " + ", ".join(leaks) + " — drop or skip it instead")
            continue
        if action == "accept":
            stamped = with_provenance_page(page, push_["meta"], imported_by)
            dest = accept_page(dest_store, project, stamped)
            if dest is None:
                out(f"  {page['name']} already exists in {project} — "
                    "merge or drop it instead")
                continue
            out(f"  accepted -> {os.path.relpath(dest, dest_store)}")
            counts["accepted"] += 1
        elif action == "merge":
            slug = (arg or "").removesuffix(".md")
            dest = (merge_page(dest_store, project, slug, page, push_["meta"], today)
                    if PAGE_NAME_RE.match(slug + ".md") else None)
            if dest is None:
                out(f"  no page {_safe(slug)!r} in {project}")
                continue
            out(f"  merged into {os.path.relpath(dest, dest_store)}")
            counts["merged"] += 1
        elif action == "drop":
            out("  dropped")
            counts["dropped"] += 1
        else:
            out("  skipped — left pending")
            return None
        return action


def _refresh_project_views(store, projects, out):
    reg = _load_registry(os.path.join(store, "registry.json"))
    current = resolve_project(store, cwd=os.curdir)
    for project in sorted(projects):
        context = current if current["project"] == project else None
        if context is None:
            for root, name in reg["map"].items():
                if name != project or not os.path.isdir(root):
                    continue
                resolved = resolve_project(store, cwd=root)
                if resolved["project"] == project:
                    context = resolved
                    break
        if context is None:
            org, stack = _resolve_overrides(reg, None, project, None, [])
        else:
            org, stack = context["org"], context["stack"]
        try:
            compile_view(store, project, org=org, stack=stack,
                         aliases=_resolve_aliases(reg, project), reg=reg)
        except OSError as exc:
            out(f"  could not rebuild view for {_safe(project)}: {_safe(exc)}")


def review(store, decide, ask_project, out=print, today=None, do_sync=True,
           dest=None, imported_by=None):
    """Walk every pending page. `decide(push, page, flags)` returns one of
    ("accept", None), ("merge", slug), ("drop", None), ("skip", None);
    `ask_project(repo)` names a project when the repo resolves to none.
    The inbox is read from `store`; pages are written into `dest` (default: `store`)."""
    dest = dest or store
    today = today or datetime.now(timezone.utc).date().isoformat()
    ok, err = fetch_inbox(store)
    if not ok:
        out(f"okfmem inbox review: fetch failed ({err or 'offline?'}) — "
            "reviewing what was fetched before.")
    counts = {"accepted": 0, "merged": 0, "dropped": 0, "deleted": 0}
    try:
        pushes = pending(store, dest)
    except InboxReadError as e:
        out(f"okfmem inbox review: could not read the inbox ({_safe(e)}) — "
            "nothing reviewed or deleted.")
        return counts
    if not pushes:
        out("okfmem inbox: nothing pending.")
        return counts
    all_pending = [(p, page) for p in pushes for page in p["pages"]]
    changed_projects = set()
    try:
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
            # A Ctrl-C mid-push still clears pages already handled.
            try:
                for page in push_["pages"]:
                    action = review_page(store, project, push_, page, all_pending,
                                         decide, counts, out, today, dest, imported_by)
                    if action:
                        handled.add(page["name"])
                    if action in ("accept", "merge"):
                        changed_projects.add(project)
            finally:
                record_handled(store, push_, handled, counts, out)
    finally:
        if changed_projects and has_layout_marker(dest):
            _refresh_project_views(dest, changed_projects, out)
    out("")
    out(f"okfmem inbox review: accepted {counts['accepted']}, merged "
        f"{counts['merged']}, dropped {counts['dropped']}; "
        f"{counts['deleted']} push(es) cleared")
    if do_sync and (counts["accepted"] or counts["merged"]):
        res = sync_store(dest, f"okfmem inbox: accepted {counts['accepted']}, "
                                f"merged {counts['merged']}")
        out(f"okfmem sync: {res['reason']}")
        if res["push_error"]:
            out(f"  push failed: {res['push_error']}")
    return counts


# ---------------------------------------------------------------------------
# dump / apply: a judge outside the engine writes decisions.json, the engine
# validates it whole and does every write (#165)
# ---------------------------------------------------------------------------
DUMP_FILE = "pending.jsonl"
ACTIONS = ("drop", "merge", "new")
TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class ApplyError(RuntimeError):
    """apply cannot start: the dump or decisions file is unreadable or malformed."""


def _inside(path, root):
    path = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(root))
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives on Windows
        return False


def dump_records(dest, pushes):
    """One record per pending page, ids p1..pN in pending order."""
    records, existing_by_project = [], {}
    for push_ in pushes:
        project = push_["project"]
        if project not in existing_by_project:
            existing_by_project[project] = project_pages(dest, project) if project else {}
        existing = existing_by_project[project]
        for page in push_["pages"]:
            near = [{"slug": slug, "why": why,
                     "description": existing[slug][1].get("description", "")}
                    for slug, why in near_existing(page, existing)]
            records.append({"id": f"p{len(records) + 1}", "ref": push_["ref"],
                            "sha": push_["sha"], "meta": push_["meta"],
                            "project": project, "slug": page["slug"],
                            "text": page["text"], "near": near,
                            "leaks": leak_hits(page["text"])})
    return records


def dump(store, out_dir, dest=None, out=print):
    """Write every pending page to `out_dir/pending.jsonl`; the store and its refs
    are left as they are. Returns (exit code, {out, pages, pushes} or {error})."""
    dest = dest or store
    for root in dict.fromkeys((store, dest)):
        if _inside(out_dir, root):
            msg = f"--out {out_dir} is inside the store {root} — pick a directory outside it."
            out(f"okfmem inbox dump: {msg}")
            return 2, {"error": msg}
    ok, err = fetch_inbox(store)
    if not ok:
        out(f"okfmem inbox dump: fetch failed ({err or 'offline?'}) — "
            "dumping what was fetched before.")
    try:
        pushes = pending(store, dest)
    except InboxReadError as e:
        out(f"okfmem inbox dump: could not read the inbox ({_safe(e)}) — nothing dumped.")
        return 1, {"error": f"could not read the inbox: {e}"}
    records = dump_records(dest, pushes)
    path = os.path.join(out_dir, DUMP_FILE)
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as e:
        out(f"okfmem inbox dump: cannot write {path}: {e}")
        return 1, {"error": f"cannot write {path}: {e}"}
    n_pushes = sum(1 for p in pushes if p["pages"])
    out(f"okfmem inbox dump: {len(records)} page(s) from {n_pushes} push(es) -> {path}")
    return 0, {"out": path, "pages": len(records), "pushes": n_pushes}


def load_dump(path):
    records, n = [], 0
    try:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                rec = json.loads(line)
                if (not isinstance(rec, dict)
                        or not all(isinstance(rec.get(k), str)
                                   for k in ("id", "ref", "sha", "slug", "text"))
                        or not isinstance(rec.get("meta"), dict)
                        or not isinstance(rec.get("project"), (str, type(None)))):
                    raise ApplyError(f"{path} line {n}: not an inbox dump record")
                records.append(rec)
    except OSError as e:
        raise ApplyError(f"cannot read the dump {path} ({e.strerror or e}) — "
                         "run `okfmem inbox dump --out DIR` and write DIR/decisions.json")
    except (ValueError, UnicodeDecodeError) as e:
        raise ApplyError(f"{path} line {n}: {e}")
    return records


def load_decisions(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise ApplyError(f"cannot read {path} ({e.strerror or e})")
    except (ValueError, UnicodeDecodeError) as e:
        raise ApplyError(f"{path} is not JSON: {e}")
    if not isinstance(data, list):
        raise ApplyError(f"{path} must hold a JSON array of decisions")
    return data


def remote_inbox_refs(store):
    """{ref: sha} of the remote's inbox refs, or None when the remote can't be read."""
    r = _git(store, "ls-remote", "--refs", REMOTE, f"{INBOX_NS}/*")
    if r.returncode != 0:
        return None
    refs = {}
    for line in _out(r).splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith(INBOX_NS + "/"):
            refs[ref] = sha
    return refs


def _with_eol(text, eol):
    return text.replace("\r\n", "\n").replace("\n", eol)


def _replace_state(text, old, new):
    """One of apply, applied (every `old` already sits inside a `new`), missing
    or ambiguous."""
    n_old, n_new = text.count(old), text.count(new)
    if n_new and n_old == n_new * new.count(old):
        return "applied"
    if n_old == 1:
        return "apply"
    return "missing" if n_old == 0 else "ambiguous"


def new_page_path(store, project, slug):
    if has_layout_marker(store):
        return os.path.join(store, "pages", slug + ".md")
    return os.path.join(store, "projects", project, slug + ".md")


def build_page(name, description, typ, labels, body):
    lines = ["---", f"name: {_yaml_scalar(name)}",
             f"description: {_yaml_scalar(description)}", f"type: {typ}"]
    if labels:
        lines.append(f"labels: [{', '.join(labels)}]")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.replace("\r\n", "\n").strip("\n") + "\n"


def _str_or_none(value):
    return value is None or isinstance(value, str)


class _Planner:
    """Validates decisions against a dump and the destination store, building
    every write in memory; nothing touches disk until `problems` is empty."""

    def __init__(self, dest, dumped, today, imported_by):
        self.dest, self.today, self.imported_by = dest, today, imported_by
        self.by_id = {r["id"]: r for r in dumped}
        self.dumped = dumped
        self.problems, self.low = [], []
        self.files = {}  # path -> {text, orig, new, project, slug, page}
        self.adds = {}  # path -> [text appended under one heading]
        self.counts = {"drop": 0, "merge": 0, "new": 0, "replace": 0}
        self.targets, self.touched = set(), set()
        self._existing = {}

    def existing(self, project):
        if project not in self._existing:
            self._existing[project] = project_pages(self.dest, project)
        return self._existing[project]

    def problem(self, where, msg):
        self.problems.append(f"{where}: {msg}")

    def project_of(self, where, pages):
        projects = {p["project"] for p in pages}
        if None in projects:
            bad = [p["id"] for p in pages if p["project"] is None]
            self.problem(where, f"{', '.join(bad)} resolve to no project — drop them, "
                                "or register the repo and dump again")
            return None
        if len(projects) > 1:
            self.problem(where, f"covers pages of several projects ({', '.join(sorted(projects))})")
            return None
        return projects.pop() if projects else None

    def leak_check(self, where, text):
        hits = leak_hits(text)
        if hits:
            self.problem(where, "leak gate: " + ", ".join(hits) + " — drop it or rewrite the text")

    def read(self, path):
        if path not in self.files:
            with open(path, encoding="utf-8", newline="") as f:
                text = f.read()
            self.files[path] = {"text": text, "orig": text, "new": False}
        return self.files[path]

    def parse(self, decisions):
        """[(where, decision, [dump records])] for well-formed decisions."""
        covered, parsed = {}, []
        for i, d in enumerate(decisions, 1):
            where = f"decision {i}"
            if not isinstance(d, dict):
                self.problem(where, "not a JSON object")
                continue
            if isinstance(d.get("id"), str):
                where += f" ({d['id']})"
            covers = d.get("covers", [])
            if not isinstance(d.get("id"), str):
                self.problem(where, "needs a string `id`")
                continue
            if not isinstance(covers, list) or not all(isinstance(c, str) for c in covers):
                self.problem(where, "`covers` must be a list of ids")
                continue
            if d.get("action") not in ACTIONS:
                self.problem(where, f"`action` must be one of {', '.join(ACTIONS)}")
                continue
            if d.get("confidence") not in (None, "high", "low"):
                self.problem(where, "`confidence` must be high or low")
            elif d.get("confidence") == "low":
                self.low.append(f"{where}: {d.get('question') or '(no question given)'}")
            pages = []
            for pid in [d["id"], *covers]:
                if pid not in self.by_id:
                    self.problem(where, f"unknown id {pid!r} (not in the dump)")
                elif pid in covered:
                    self.problem(where, f"{pid} is already covered by {covered[pid]}")
                else:
                    covered[pid] = where
                    pages.append(self.by_id[pid])
            parsed.append((where, d, pages))
        missing = [r["id"] for r in self.dumped if r["id"] not in covered]
        if missing:
            self.problems.append("not covered by any decision: " + ", ".join(missing))
        return parsed

    def plan_new(self, where, d, pages):
        primary = self.by_id.get(d["id"])
        project = self.project_of(where, pages)
        spec = d.get("page") or {}
        if not isinstance(spec, dict):
            self.problem(where, "`page` must be an object")
            return
        if not all(_str_or_none(spec.get(k)) for k in ("slug", "name", "description", "type")):
            self.problem(where, "`page` slug, name, description and type must be strings")
            return
        if not _str_or_none(d.get("add")):
            self.problem(where, "`add` must be a string")
            return
        if primary is None:
            return
        fields = page_fields(primary["text"]) or {}
        slug = spec.get("slug") or primary["slug"]
        name = spec.get("name") or fields.get("name") or slug
        description = spec.get("description") or fields.get("description") or ""
        typ = (spec.get("type") or fields.get("type") or "").lower()
        raw_labels = spec.get("labels") or []
        labels = ([normalize_label(x) if isinstance(x, str) else None for x in raw_labels]
                  if isinstance(raw_labels, list) else [None])
        body = d.get("add") or page_body(primary["text"])
        if not importable_name(slug + ".md"):
            self.problem(where, f"invalid page slug {slug!r}")
            return
        if not TYPE_RE.match(typ):
            self.problem(where, f"invalid page type {typ!r}")
        if None in labels:
            self.problem(where, "`page.labels` must be a list of valid labels")
        self.leak_check(where, f"{name}\n{description}\n{body}")
        if project is None or None in labels or not TYPE_RE.match(typ):
            return
        if has_layout_marker(self.dest):
            labels.append(f"repo:{project}")
        text = with_provenance(build_page(name, description, typ, labels, body),
                               primary["meta"], self.imported_by)
        path = new_page_path(self.dest, project, slug)
        if path in self.files:
            self.problem(where, f"new slug {slug!r} is already created by another decision")
            return
        if os.path.exists(path):
            try:
                current = self.read(path)
            except (OSError, UnicodeDecodeError) as e:
                self.problem(where, f"new slug {slug!r} already exists and cannot be read: {e}")
                return
            want = page_fields(text) or {}
            have = page_fields(current["text"]) or {}
            eol = "\r\n" if "\r\n" in current["text"] else "\n"
            if have.get("name") != want.get("name") or (
                    _with_eol(page_body(text), eol) not in current["text"]):
                self.problem(where, f"new slug {slug!r} already exists in the store")
                return
            current.update(project=project, slug=slug)  # applied by an earlier run
        else:
            self.files[path] = {"text": text, "orig": None, "new": True, "project": project,
                                "slug": slug}
            self.counts["new"] += 1
        self.touched.add(project)

    def plan_merge(self, where, d, pages):
        project = self.project_of(where, pages)
        target, add, replace = d.get("target"), d.get("add"), d.get("replace") or []
        if not isinstance(target, str) or not importable_name(target.removesuffix(".md") + ".md"):
            self.problem(where, f"merge needs a valid `target` slug, got {target!r}")
            return
        target = target.removesuffix(".md")
        if not _str_or_none(add):
            self.problem(where, "`add` must be a string")
            return
        if not isinstance(replace, list) or not all(
                isinstance(r, dict) and isinstance(r.get("old"), str) and r["old"]
                and isinstance(r.get("new"), str) and r["new"] and r["old"] != r["new"]
                for r in replace):
            self.problem(where, "`replace` must be a list of {old, new} with non-empty, "
                                "different strings")
            return
        self.leak_check(where, "\n".join([add or "", *(r["new"] for r in replace)]))
        if project is None:
            return
        path = None
        if target in self.existing(project):
            path = self.existing(project)[target][0]
        else:
            candidate = new_page_path(self.dest, project, target)
            if candidate in self.files:
                path = candidate
        if path is None:
            self.problem(where, f"merge target {target!r} is neither a page of {project} "
                                "nor created by a `new` decision")
            return
        try:
            entry = self.read(path)
        except (OSError, UnicodeDecodeError) as e:
            self.problem(where, f"cannot read merge target {target!r}: {e}")
            return
        eol = "\r\n" if "\r\n" in entry["text"] else "\n"
        for r in replace:
            old, new = _with_eol(r["old"], eol), _with_eol(r["new"], eol)
            state = _replace_state(entry["text"], old, new)
            if state == "apply":
                entry["text"] = entry["text"].replace(old, new, 1)
                self.counts["replace"] += 1
            elif state == "missing":
                self.problem(where, f"replace: {_preview(r['old'])!r} not found in {target}")
            elif state == "ambiguous":
                self.problem(where, f"replace: {_preview(r['old'])!r} found "
                                    f"{entry['text'].count(old)} times in {target}")
        block = _with_eol((add or "").strip("\r\n"), eol)
        queued = self.adds.setdefault(path, [])
        if block and block not in entry["text"] and block not in queued:
            queued.append(block)
        self.counts["merge"] += len(pages)
        self.targets.add(path)
        self.touched.add(project)

    def finish(self):
        for path, blocks in self.adds.items():
            if not blocks:
                continue
            entry = self.files[path]
            eol = "\r\n" if "\r\n" in entry["text"] else "\n"
            entry["text"] = (entry["text"].rstrip("\r\n") + eol + eol
                             + f"## From inbox ({self.today}){eol}{eol}"
                             + (eol + eol).join(blocks) + eol)

    def run(self, decisions):
        parsed = self.parse(decisions)
        # Every `new` first, so a merge may target a page created in the same file.
        for where, d, pages in parsed:
            if d["action"] == "new":
                self.plan_new(where, d, pages)
        for where, d, pages in parsed:
            if d["action"] == "merge":
                self.plan_merge(where, d, pages)
            elif d["action"] == "drop":
                self.counts["drop"] += len(pages)
        self.finish()


def _preview(text, width=40):
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[:width - 1] + "…"


def plan_apply(dest, dumped, decisions, remote, today, imported_by):
    """(plan, problems): the writes and ref deletions `decisions` asks for, or
    every problem that refuses the file."""
    planner = _Planner(dest, dumped, today, imported_by)
    planner.run(decisions)
    refs = {}
    for rec in dumped:
        refs.setdefault(rec["ref"], rec["sha"])
    delete, gone, moved = {}, [], []
    for ref, sha in sorted(refs.items()):
        if ref not in remote:
            gone.append(ref)
        elif remote[ref] != sha:
            moved.append(ref)
        else:
            delete[ref] = sha
    new = {p: e for p, e in planner.files.items() if e["new"]}
    changed = {p: e for p, e in planner.files.items()
               if not e["new"] and e["text"] != e["orig"]}
    plan = {"new": new, "changed": changed, "counts": planner.counts,
            "targets": len(planner.targets), "low": planner.low,
            "touched": planner.touched, "delete": delete, "gone": gone, "moved": moved}
    return plan, planner.problems


def plan_lines(plan, dest):
    c = plan["counts"]
    lines = [f"okfmem inbox apply: {c['drop']} drop / {c['merge']} merge into "
             f"{plan['targets']} page(s) / {c['new']} new"]
    for path in sorted(plan["new"]):
        lines.append(f"  new      {os.path.relpath(path, dest)}")
    for path in sorted(plan["changed"]):
        lines.append(f"  update   {os.path.relpath(path, dest)}")
    if c["replace"]:
        lines.append(f"  {c['replace']} in-place correction(s)")
    for low in plan["low"]:
        lines.append(_safe(f"  low confidence — {low}"))
    lines.append(f"  inbox: clear {len(plan['delete'])} push(es)"
                 + (f"; {len(plan['gone'])} already cleared" if plan["gone"] else "")
                 + (f"; {len(plan['moved'])} re-pushed since the dump, left pending"
                    if plan["moved"] else ""))
    return lines


def delete_refs(store, refs):
    """Delete every ref in `refs` ({ref: sha}) on the remote in one push, each
    leased on its sha so a ref re-pushed since it was read survives.
    Returns (deleted, {ref: why it is still there})."""
    if not refs:
        return [], {}
    leases = [f"--force-with-lease={ref}:{sha}" for ref, sha in sorted(refs.items())]
    p = _git(store, "push", "--porcelain", *leases, REMOTE,
             *(f":{ref}" for ref in sorted(refs)))
    deleted, failed = [], {}
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or ":" not in parts[1]:
            continue
        ref = parts[1].split(":", 1)[1]
        if ref not in refs:
            continue
        if parts[0] == "-":
            deleted.append(ref)
        else:
            failed[ref] = parts[2].strip()
    for ref in refs:
        if ref not in deleted and ref not in failed:
            failed[ref] = _err(p) or f"git push exited {p.returncode}"
    for ref in deleted:
        _git(store, "update-ref", "-d", ref)
    return deleted, failed


def write_plan(dest, plan):
    """Write the planned pages; returns the paths written."""
    written = []
    for path, entry in sorted(plan["new"].items()):
        page = {"name": entry["slug"] + ".md", "slug": entry["slug"], "text": entry["text"],
                "fields": page_fields(entry["text"])}
        got = accept_page(dest, entry["project"], page)
        if got is None:
            raise OSError(f"{path} appeared while applying — nothing more written")
        written.append(got)
    for path, entry in sorted(plan["changed"].items()):
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(entry["text"])
        written.append(path)
    return written


def apply(store, decisions_path, confirm, dest=None, out=print, today=None,
          imported_by=None, do_sync=True, hint=None):
    """Apply `decisions_path` against the `pending.jsonl` dumped beside it.
    `confirm()` returns True, False, or None when nobody can be asked.
    Returns the exit code."""
    dest = dest or store
    today = today or datetime.now(timezone.utc).date().isoformat()
    imported_by = imported_by or default_importer()
    dump_path = os.path.join(os.path.dirname(os.path.abspath(decisions_path)), DUMP_FILE)
    try:
        dumped = load_dump(dump_path)
        decisions = load_decisions(decisions_path)
    except ApplyError as e:
        out(f"okfmem inbox apply: {e} — nothing written.")
        return 2
    if not git_clean(dest):
        out("okfmem inbox apply: store working tree is dirty — run `okfmem sync` "
            "first so the apply's commit holds only its own pages.")
        return 3
    fetch_inbox(store)  # a failed fetch is fine: ls-remote below decides every ref
    remote = remote_inbox_refs(store)
    if remote is None:
        out(f"okfmem inbox apply: cannot read remote '{REMOTE}' — nothing written.")
        return 1
    plan, problems = plan_apply(dest, dumped, decisions, remote, today, imported_by)
    if problems:
        out(f"okfmem inbox apply: {len(problems)} problem(s) in {decisions_path} — "
            "nothing written:")
        for p in problems:
            out(_safe(f"  - {p}"))
        return 1
    for line in plan_lines(plan, dest):
        out(line)
    for ref in plan["moved"]:
        out(f"  still pending: {ref} (re-pushed since the dump)")
    if not (plan["new"] or plan["changed"] or plan["delete"]):
        out("okfmem inbox apply: nothing to do — already applied.")
        return 0
    answer = confirm()
    if answer is None:
        out("okfmem inbox apply: no terminal to confirm — nothing written. To apply, run:")
        out(f"  {hint or 'okfmem inbox apply ' + decisions_path + ' --yes'}")
        return 2
    if not answer:
        out("okfmem inbox apply: not confirmed — nothing written.")
        return 1
    try:
        written = write_plan(dest, plan)
    except OSError as e:
        out(f"okfmem inbox apply: write failed ({_safe(e)}) — no inbox ref deleted; "
            "fix it, then run `okfmem sync` and apply again.")
        return 1
    finally:
        if plan["touched"] and has_layout_marker(dest):
            _refresh_project_views(dest, plan["touched"], out)
    deleted, failed = delete_refs(store, plan["delete"])
    code = 0
    for ref, why in sorted(failed.items()):
        out(_safe(f"  still pending: {ref} ({why})"))
        if "stale info" not in why:
            code = 1
    out(f"okfmem inbox apply: wrote {len(written)} page(s); cleared {len(deleted)} push(es)")
    if do_sync and written:
        c = plan["counts"]
        res = sync_store(dest, f"okfmem inbox: applied {c['drop']} drop, {c['merge']} merge, "
                               f"{c['new']} new")
        out(f"okfmem sync: {res['reason']}")
        if res["push_error"]:
            out(f"  push failed: {res['push_error']}")
            code = 1
    return code


def _quote_arg(arg):
    """`arg` as one argument for bash, zsh, PowerShell and cmd alike: Windows
    paths keep their backslashes, spaces are double-quoted."""
    if re.fullmatch(r"[A-Za-z0-9_./:\\@%+=,~-]+", arg):
        return arg
    return '"' + arg.replace('"', '\\"') + '"'


def apply_hint(store, decisions_path, into=None):
    into_part = f" --into {_quote_arg(into)}" if into else ""
    return (f"okfmem inbox apply --store {_quote_arg(store)}{into_part} "
            f"{_quote_arg(decisions_path)} --yes")


def _interactive_confirm():
    try:
        ans = input("okfmem inbox apply: write these pages and clear the inbox refs? [y/N] ")
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes")


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
        return input(f"  project for repo {_safe(repo or '(none)')}: ").strip() or None
    except EOFError:
        return None


def _isatty():
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        return False


def no_terminal_hint(cmd, store, into=None):
    """What to run instead when review/import has no terminal; keeps `--into`."""
    base = f"okfmem inbox {cmd} --store {store}" + (f" --into {into}" if into else "")
    hint = f"okfmem inbox {cmd}: interactive only — run it in a terminal: {base}"
    if cmd == "import":
        hint += (" — or without a terminal: "
                 f"{base} --non-interactive --policy FILE")
    return hint


def _run_policy_import(store, dest, args):
    """Exit code for `inbox import --non-interactive`; --json keeps stdout to one object."""
    log = (lambda m: print(m, file=sys.stderr)) if args.json else print

    def fail(code, message):
        log(f"okfmem inbox import: {message}")
        if args.json:
            print(json.dumps({"error": " ".join(message.split())}))
        return code

    if not args.policy:
        return fail(2, "--non-interactive needs --policy FILE.")
    try:
        policy = load_policy(args.policy)
    except PolicyError as e:
        return fail(2, f"policy refused: {e}")
    if not git_clean(dest):
        return fail(3, "store working tree is dirty — run `okfmem sync` first.")
    imported_by = args.imported_by or default_importer()
    result = import_policy(store, dest, policy, imported_by, out=log)
    if result.get("error"):
        return fail(1, result["error"])
    if args.json:
        print(json.dumps({k: result[k] for k in ("imported", "held", "skipped", "held_pages")}))
    return 0


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
    p.add_argument("--json", action="store_true",
                   help="print one JSON object on stdout; messages go to stderr")
    p.add_argument("--store", default=argparse.SUPPRESS)
    q =sub.add_parser("pull", help="fetch the inbox and list pending pages")
    q.add_argument("--store", default=argparse.SUPPRESS)
    r = sub.add_parser("review", help="accept, merge or drop each pending page")
    r.add_argument("--store", default=argparse.SUPPRESS)
    r.add_argument("--into", metavar="STORE",
                   help="import into this attached store instead of the home store")
    im = sub.add_parser("import", help="import pending pages, interactively or "
                                       "non-interactively under a policy")
    im.add_argument("--store", default=argparse.SUPPRESS)
    im.add_argument("--into", metavar="STORE",
                    help="import into this attached store instead of the home store")
    im.add_argument("--non-interactive", action="store_true",
                    help="import only candidates that pass --policy")
    im.add_argument("--policy", metavar="FILE", help="written import policy (YAML)")
    im.add_argument("--imported-by", metavar="NAME",
                    help="who is importing (default: the current user)")
    im.add_argument("--json", action="store_true",
                    help="print {imported, held, skipped, held_pages} on stdout")
    du = sub.add_parser("dump", help="write every pending page to DIR/pending.jsonl "
                                     "(read-only)")
    du.add_argument("--store", default=argparse.SUPPRESS)
    du.add_argument("--into", metavar="STORE",
                    help="look for near pages in this attached store")
    du.add_argument("--out", required=True, metavar="DIR",
                    help="directory for pending.jsonl, outside the store")
    du.add_argument("--json", action="store_true",
                    help="print {out, pages, pushes} on stdout; messages go to stderr")
    ap_ = sub.add_parser("apply", help="apply a decisions.json written against a dump")
    ap_.add_argument("decisions", metavar="DIR/decisions.json")
    ap_.add_argument("--store", default=argparse.SUPPRESS)
    ap_.add_argument("--into", metavar="STORE",
                     help="write into this attached store instead of the home store")
    ap_.add_argument("--yes", action="store_true", help="skip the [y/N] confirmation")
    ap_.add_argument("--imported-by", metavar="NAME",
                     help="who is importing (default: the current user)")
    args = ap.parse_args()
    store = os.path.abspath(os.path.expanduser(args.store))
    dest = store
    if args.cmd in ("review", "import", "dump", "apply") and args.into:
        dest, why = resolve_into(store, args.into)
        if dest is None:
            print(f"okfmem inbox {args.cmd}: {why}", file=sys.stderr)
            sys.exit(2)

    if args.cmd == "push":
        if not args.json:
            code, _ = push(store, args.dir, args.source, args.repo, args.ref,
                           args.machine)
            sys.exit(code)
        result = {}
        code, inbox_ref = push(store, args.dir, args.source, args.repo, args.ref,
                               args.machine, out=lambda m: print(m, file=sys.stderr),
                               result=result)
        if result.get("error"):
            payload = {"error": " ".join(result["error"].split())}
        else:
            payload = {"ref": inbox_ref, "pages": result["pages"],
                       "created": result["created"]}
        print(json.dumps(payload))
        sys.exit(code)
    if args.cmd == "pull":
        sys.exit(pull(store))
    if args.cmd == "dump":
        log = (lambda m: print(m, file=sys.stderr)) if args.json else print
        code, result = dump(store, os.path.abspath(os.path.expanduser(args.out)), dest,
                            out=log)
        if args.json:
            if "error" in result:
                result = {"error": " ".join(result["error"].split())}
            print(json.dumps(result))
        sys.exit(code)
    if args.cmd == "apply":
        decisions = os.path.abspath(os.path.expanduser(args.decisions))
        if args.yes:
            confirm = lambda: True  # noqa: E731
        elif _isatty():
            confirm = _interactive_confirm
        else:
            confirm = lambda: None  # noqa: E731
        try:
            code = apply(store, decisions, confirm, dest=dest,
                         imported_by=args.imported_by,
                         hint=apply_hint(store, decisions, args.into))
        except KeyboardInterrupt:
            print("\nokfmem inbox apply: interrupted — run `okfmem status` to see "
                  "what was written.")
            code = 130
        sys.exit(code)
    non_interactive = args.cmd == "import" and args.non_interactive
    if args.cmd == "import" and (args.policy or args.json) and not non_interactive:
        print("okfmem inbox import: --policy and --json need --non-interactive.",
              file=sys.stderr)
        sys.exit(2)
    if non_interactive:
        sys.exit(_run_policy_import(store, dest, args))
    if not _isatty():
        print(no_terminal_hint(args.cmd, store, args.into))
        sys.exit(2)
    if not git_clean(dest):
        print(f"okfmem inbox {args.cmd}: store working tree is dirty — run "
              "`okfmem sync` first so the review's commit holds only its own pages.")
        sys.exit(3)
    try:
        review(store, _interactive_decide, _interactive_project, dest=dest,
               imported_by=getattr(args, "imported_by", None))
    except KeyboardInterrupt:
        print("\nokfmem inbox review: interrupted — handled pages stay written; "
              "run `okfmem sync` to commit them.")
        sys.exit(130)
    sys.exit(0)


if __name__ == "__main__":
    main()
