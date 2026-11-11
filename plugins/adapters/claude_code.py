"""Claude Code extractor adapter — JSONL session transcripts -> normalized turns.

Source: ~/.claude/projects/<project>/<uuid>.jsonl  (one JSON object per line).
v1 target per issue #2 — the easy, clean harness.

Parsing of Claude's on-disk format lives in core `memory_transcript`
(`_claude_turns`); this adapter only finds the files and delegates. Emits the
harness-neutral turn schema: drops every tool_result body, scrubs every text,
keeps tool_name + a short signature off the tool_use side.
"""
from __future__ import annotations

import os
import sys

# import the shared lib whether run as a module or via runpy
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
from memory_transcript import extract_file as _extract_file  # noqa: E402

HARNESS = "claude-code"
DEFAULT_ROOT = os.path.expanduser("~/.claude/projects")


def _project_from_path(path):
    """Claude encodes the project as the parent dir name (e.g. -Users-you-myproject)."""
    return os.path.basename(os.path.dirname(path))


def extract_file(path):
    """Yield normalized turns for one .jsonl transcript."""
    yield from _extract_file(HARNESS, path)


def iter_turns(root=DEFAULT_ROOT, project=None):
    """Walk all (or one) project dir(s) under root, yielding turns from every .jsonl."""
    root = os.path.expanduser(root)
    if not os.path.isdir(root):
        return
    for proj in sorted(os.listdir(root)):
        if project and proj != project:
            continue
        pdir = os.path.join(root, proj)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if fn.endswith(".jsonl"):
                yield from extract_file(os.path.join(pdir, fn))
