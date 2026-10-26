#!/usr/bin/env python3
"""okfmem shared frontmatter parser/writer (stdlib only, byte-preserving).

The one place that knows how to read a page's leading `--- ... ---` block and
the `labels` / `scope` fields on top of it (#98). Consumers: memory_backfill,
memory_consolidate, and the label-pool work that builds on this schema.

    labels: [org:116-ideas, stack:typescript, repo:foo.js]   # or block `- a` form
    scope: evergreen | transient                              # absent => transient

Malformed input never raises from a parse function: it is dropped with a
warning on stderr and the run carries on.
"""
import os
import re
import sys

OPEN_RE = re.compile(r"^---(\r\n|\n)")
TOP_TYPE_RE = re.compile(r"^type:\s*(.*?)\s*$")
CLOSE_RE = re.compile(r"(\r\n|\n)---[ \t]*(\r\n|\n|$)")
FIELD_RE = re.compile(r"^([A-Za-z_][\w-]*):\s?(.*)$")
LABELS_RE = re.compile(r"^labels:\s*(.*?)\s*$")
BLOCK_ITEM_RE = re.compile(r"^\s+-(?:\s+(.*?))?\s*$|^-(?:\s+(.*?))?\s*$")
LABEL_RE = re.compile(r"^(?:[a-z0-9_.-]+:)?[a-z0-9_.-]+$")

NAMESPACES = ("repo", "org", "stack", "topic")
SCOPES = ("evergreen", "transient")
DEFAULT_SCOPE = "transient"


def _warn(msg):
    print(f"okfmem: warning: {msg}", file=sys.stderr)


def find_frontmatter(text):
    """Return (eol, body_start, body_end) for the leading YAML block, or None.

    body is between the opening delimiter line and the closing `---` line;
    body_end is the index where the closing-delimiter EOL begins (i.e. new keys
    inserted at body_end land as the last frontmatter lines).
    """
    m = OPEN_RE.match(text)
    if not m:
        return None
    eol = m.group(1)
    body_start = m.end()
    cm = CLOSE_RE.search(text, body_start - len(eol))
    if not cm:
        return None
    body_end = cm.start() + len(cm.group(1))
    return eol, body_start, body_end


def parse_fields(text, fm):
    """Top-level scalar key -> raw string value, over the frontmatter body."""
    eol, body_start, body_end = fm
    fields = {}
    for ln in text[body_start:body_end].split(eol):
        m = FIELD_RE.match(ln)
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return fields


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1].strip()
    return s


def normalize_label(raw):
    """Lowercased, trimmed label, or None when it is not a valid label."""
    label = _unquote(raw).lower()
    return label if LABEL_RE.match(label) else None


def _raw_labels(lines, i):
    """(raw items, index just past the field, ok) for the `labels:` line at i."""
    value = LABELS_RE.match(lines[i]).group(1)
    if value.startswith("["):
        if not value.endswith("]"):
            return [], i + 1, False
        inner = value[1:-1].strip()
        return ([s for s in inner.split(",")] if inner else []), i + 1, True
    if value:
        return [], i + 1, False
    items = []
    j = i + 1
    while j < len(lines):
        m = BLOCK_ITEM_RE.match(lines[j])
        if not m:
            break
        items.append(m.group(1) or m.group(2) or "")
        j += 1
    return items, j, True


def _find_labels_line(lines):
    for i, ln in enumerate(lines):
        if LABELS_RE.match(ln):
            return i
    return None


def parse_labels(text, fm=None, source="", warn=_warn):
    """Valid, deduplicated labels in page order; `[]` when absent or malformed."""
    fm = fm or find_frontmatter(text)
    if not fm:
        return []
    eol, body_start, body_end = fm
    lines = text[body_start:body_end].split(eol)
    i = _find_labels_line(lines)
    if i is None:
        return []
    where = f" in {source}" if source else ""
    raw, _end, ok = _raw_labels(lines, i)
    if not ok:
        if warn:
            warn(f"malformed `labels` field{where}; ignoring it")
        return []
    out = []
    for item in raw:
        label = normalize_label(item)
        if label is None:
            if warn and item.strip():
                warn(f"dropping invalid label {item.strip()!r}{where}")
            continue
        if label not in out:
            out.append(label)
    return out


def parse_scope(text, fm=None, source="", warn=_warn):
    """`evergreen` or `transient`; absent or unknown values mean transient."""
    fm = fm or find_frontmatter(text)
    if not fm:
        return DEFAULT_SCOPE
    value = _unquote(parse_fields(text, fm).get("scope", "")).lower()
    if not value:
        return DEFAULT_SCOPE
    if value in SCOPES:
        return value
    if warn:
        where = f" in {source}" if source else ""
        warn(f"unknown scope {value!r}{where}; treating as {DEFAULT_SCOPE}")
    return DEFAULT_SCOPE


def merge_labels(text, new_labels, source="", warn=_warn):
    """Return `text` with `new_labels` merged into its `labels` field.

    Existing labels keep their order, new ones are appended, duplicates are
    dropped. The field is written inline; every other byte is untouched, and
    text already carrying every label is returned unchanged.
    """
    fm = find_frontmatter(text)
    if not fm:
        raise ValueError("page has no frontmatter")
    eol, body_start, body_end = fm
    merged = parse_labels(text, fm, source, warn)
    for raw in new_labels:
        label = normalize_label(raw)
        if label is None:
            if warn:
                warn(f"dropping invalid label {raw!r}")
        elif label not in merged:
            merged.append(label)
    line = f"labels: [{', '.join(merged)}]"

    lines = text[body_start:body_end].split(eol)
    i = _find_labels_line(lines)
    if i is None:
        if not merged:
            return text
        return text[:body_end] + line + eol + text[body_end:]
    _raw, end, _ok = _raw_labels(lines, i)
    if lines[i:end] == [line]:
        return text
    lines[i:end] = [line]
    return text[:body_start] + eol.join(lines) + text[body_end:]


def summarize_pages(paths):
    """Read-only label/scope tallies over page files (unreadable ones skipped)."""
    ns_counts = {n: 0 for n in (*NAMESPACES, "other")}
    labeled = evergreen = transient = 0
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        fm = find_frontmatter(text)
        labels = parse_labels(text, fm, warn=None) if fm else []
        if labels:
            labeled += 1
        for label in labels:
            ns = label.split(":", 1)[0] if ":" in label else "other"
            ns_counts[ns if ns in NAMESPACES else "other"] += 1
        if fm and parse_scope(text, fm, warn=None) == "evergreen":
            evergreen += 1
        else:
            transient += 1
    return {"labeled": labeled, "namespaces": ns_counts,
            "evergreen": evergreen, "transient": transient}


def format_summary(summary):
    ns = summary["namespaces"]
    counts = ", ".join(f"{n} {ns[n]}" for n in (*NAMESPACES, "other"))
    return (f"labels: {summary['labeled']} labeled page(s) ({counts}); "
            f"scope: {summary['evergreen']} evergreen, "
            f"{summary['transient']} transient")


def page_paths(store):
    """Every durable page under `<store>/projects/*`, via the engine's rule."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from memory_reindex import page_files

    root = os.path.join(store, "projects")
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if os.path.isdir(d):
            out.extend(os.path.join(d, p) for p in page_files(d))
    return out
