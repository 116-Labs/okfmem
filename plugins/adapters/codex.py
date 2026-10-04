"""Codex 0.160 rollout JSONL adapter (format is not a stable public API).

Reads session_meta and response_item chat/function calls; event_msg chat mirrors
are ignored to avoid duplicate representations. Raw tool outputs never escape.
Missing/unreadable history and malformed records are skipped. Roots are configurable.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from okfmem_session import Turn, scrub, strip_signature

from memory_init import resolve_project

HARNESS = "codex"
DEFAULT_ROOT = os.path.join(
    os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex")), "sessions"
)


def extract_file(path, store=None):
    store = store or os.environ.get(
        "OKFMEM_STORE", os.path.expanduser("~/okfmem-store")
    )
    session, project, idx = Path(path).stem, "unknown", 0
    seen = set()
    try:
        fh = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
    except OSError:
        return
    with fh:
        for line in fh:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if record.get("type") == "session_meta":
                session = (
                    payload["id"] if isinstance(payload.get("id"), str) else session
                )
                cwd = payload.get("cwd")
                if isinstance(cwd, str):
                    try:
                        resolved = resolve_project(store, cwd)
                        project = (
                            resolved["project"]
                            or os.path.basename(cwd.rstrip("/\\"))
                            or "unknown"
                        )
                    except OSError:
                        project = "unknown"
                continue
            if record.get("type") != "response_item":
                continue
            kind = payload.get("type")
            tool = None
            if kind == "message" and payload.get("role") in ("user", "assistant"):
                role = payload["role"]
                content = payload.get("content", [])
                if not isinstance(content, list):
                    continue
                text = "\n".join(
                    b["text"]
                    for b in content
                    if isinstance(b, dict)
                    and b.get("type") in ("input_text", "output_text", "text")
                    and isinstance(b.get("text"), str)
                )
            elif kind in ("function_call", "custom_tool_call"):
                role, tool = "tool", payload.get("name", "unknown")
                if not isinstance(tool, str):
                    continue
                raw = payload.get("arguments", payload.get("input", ""))
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except ValueError:
                        pass
                if tool == "exec_command" and isinstance(raw, dict) and "cmd" in raw:
                    raw = {**raw, "command": raw["cmd"]}
                text = strip_signature(tool, raw)
            else:
                continue
            text = scrub(text).strip()
            # Stable response ids / call ids distinguish real repeated messages.
            identity = payload.get("id") or payload.get("call_id")
            if not isinstance(identity, str):
                identity = None
            timestamp = record.get("timestamp")
            if not isinstance(timestamp, (str, int, float, type(None))):
                timestamp = None
            key = (identity, role, text) if identity else (timestamp, role, text)
            if not text or key in seen:
                continue
            seen.add(key)
            yield Turn(
                HARNESS,
                project,
                session,
                timestamp,
                idx,
                role,
                text,
                tool,
            )
            idx += 1


def iter_turns(root=DEFAULT_ROOT, project=None, store=None):
    root = Path(root).expanduser()
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.jsonl")):
        for turn in extract_file(path, store):
            if project is None or turn["project"] == project:
                yield turn
