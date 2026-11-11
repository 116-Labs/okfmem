"""Codex 0.160 rollout JSONL adapter (format is not a stable public API).

Reads session_meta and response_item chat/function calls; event_msg chat mirrors
are ignored to avoid duplicate representations. Raw tool outputs never escape.
Missing/unreadable history and malformed records are skipped. Roots are configurable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from memory_transcript import extract_file as _extract_file

from memory_init import resolve_project

HARNESS = "codex"
DEFAULT_ROOT = os.path.join(
    os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex")), "sessions"
)


def extract_file(path, store=None):
    for turn in _extract_file(HARNESS, path):
        try:
            project = resolve_project(store or os.path.expanduser("~/okfmem-store"), turn["project"])["project"]
        except OSError:
            project = None
        yield {**turn, "project": project or os.path.basename(turn["project"].rstrip("/\\")) or "unknown"}


def iter_turns(root=DEFAULT_ROOT, project=None, store=None):
    root = Path(root).expanduser()
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.jsonl")):
        for turn in extract_file(path, store):
            if project is None or turn["project"] == project:
                yield turn
