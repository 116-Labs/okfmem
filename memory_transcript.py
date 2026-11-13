"""Transcript slices for background capture.

Codex slices contain user/assistant text and command signatures only. Tool output
is deliberately excluded, so a root cause visible only in tool output is absent.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("harness", "project", "session_id", "ts", "idx", "role", "text", "tool_name")
_REDACT = "«REDACTED»"
_TOKEN_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{16,}", r"gh[pousr]_[A-Za-z0-9]{16,}",
    r"github_pat_[A-Za-z0-9_]{20,}", r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"AKIA[0-9A-Z]{16}", r"AIza[A-Za-z0-9_-]{20,}",
    r"lin_api_[A-Za-z0-9]{20,}",
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}",
]
_COMPILED = [re.compile(p) for p in _TOKEN_PATTERNS]
_ASSIGN = re.compile(
    r"(?i)((?:api[_-]?key|secret|password|passwd|authorization|auth[_-]?token|"
    r"access[_-]?token|bearer|token|LINEAR_API[A-Z_]*))(\s*[:=]\s*|\s+)"
    r"['\"]?([A-Za-z0-9._\-/+]{8,})['\"]?"
)
_EXEC_CMD = re.compile(r"exec_command\s*\(\s*\{\s*cmd\s*:\s*(['\"])(.*?)\1", re.S)


def Turn(harness, project, session_id, ts, idx, role, text, tool_name=None):
    return {"harness": harness, "project": project, "session_id": session_id,
            "ts": ts, "idx": idx, "role": role, "text": text,
            "tool_name": tool_name}


def scrub(text):
    if not text:
        return text
    for rx in _COMPILED:
        text = rx.sub(_REDACT, text)
    return _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACT}", text)


def strip_signature(tool_name, tool_input):
    if isinstance(tool_input, dict):
        for key in ("command", "description", "query", "pattern", "file_path", "path", "url", "prompt"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                signature = value.strip().splitlines()[0]
                break
        else:
            signature = " ".join(sorted(tool_input))
    elif isinstance(tool_input, str):
        signature = tool_input.strip().splitlines()[0] if tool_input.strip() else ""
    else:
        signature = ""
    signature = scrub(signature)
    return signature[:120] + ("…" if len(signature) > 120 else "")


def _project(path):
    return os.path.basename(os.path.dirname(path))


def _codex_meta(record, path):
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    session = payload.get("id") if isinstance(payload.get("id"), str) else Path(path).stem
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else "unknown"
    return session, cwd


def transcript_identity(harness, path):
    if harness not in {"claude-code", "codex"}:
        raise ValueError(f"unsupported harness: {harness}")
    first = b""
    session, cwd = Path(path).stem, _project(path)
    with open(path, "rb") as raw:
        first = raw.readline()
    fingerprint = hashlib.sha256(first).hexdigest()
    if harness == "codex":
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                meta = _codex_meta(record, path)
                if meta:
                    session, cwd = meta
                    break
    else:
        scanned = records = 0
        candidate_session = None
        with open(path, "rb") as fh:
            while scanned < 64 * 1024 and records < 200:
                line = fh.readline(64 * 1024 - scanned)
                if not line:
                    break
                scanned += len(line)
                records += 1
                try:
                    record = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                session_id = record.get("sessionId")
                if session_id:
                    candidate_session = session_id
                if candidate_session and session_id and record.get("cwd"):
                    session, cwd = candidate_session, record["cwd"]
                    break
    return {"harness": harness, "session": session, "thread": session,
            "cwd": cwd, "fingerprint": fingerprint}


def _claude_turns(record, identity, idx):
    if not isinstance(record, dict) or record.get("type") not in ("user", "assistant"):
        return []
    msg = record.get("message") or {}
    role, ts = msg.get("role") or record.get("type"), record.get("timestamp")
    content = msg.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    if not isinstance(blocks, list):
        return []
    turns = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        typ = block.get("type")
        if typ in ("text", "thinking"):
            text = scrub(block.get("text", block.get("thinking", ""))).strip()
            if text:
                turns.append(Turn("claude-code", identity["cwd"], identity["session"], ts, idx + len(turns),
                                  "thinking" if typ == "thinking" else role, text))
        elif typ == "tool_use":
            turns.append(Turn("claude-code", identity["cwd"], identity["session"], ts, idx + len(turns),
                              "tool", strip_signature(block.get("name"), block.get("input")), block.get("name")))
    return turns


def _codex_turns(record, identity, idx):
    if not isinstance(record, dict) or record.get("type") != "response_item":
        return []
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return []
    kind, tool = payload.get("type"), None
    if kind == "message" and payload.get("role") in ("user", "assistant"):
        role = payload["role"]
        content = payload.get("content", [])
        if not isinstance(content, list):
            return []
        text = "\n".join(b["text"] for b in content if isinstance(b, dict)
                         and b.get("type") in ("input_text", "output_text", "text")
                         and isinstance(b.get("text"), str))
    elif kind in ("function_call", "custom_tool_call"):
        role, tool = "tool", payload.get("name", "unknown")
        if not isinstance(tool, str):
            return []
        raw = payload.get("arguments", payload.get("input", ""))
        if isinstance(raw, str):
            if tool == "exec":
                match = _EXEC_CMD.search(raw)
                raw = {"command": match.group(2)} if match else "exec"
            else:
                try:
                    raw = json.loads(raw)
                except ValueError:
                    pass
        if tool == "exec_command" and isinstance(raw, dict) and "cmd" in raw:
            raw = {**raw, "command": raw["cmd"]}
        text = strip_signature(tool, raw)
    else:
        return []
    text = scrub(text).strip()
    return [Turn("codex", identity["cwd"], identity["session"], record.get("timestamp"), idx, role, text, tool)] if text else []


def _parse(harness, record, identity, idx):
    return _claude_turns(record, identity, idx) if harness == "claude-code" else _codex_turns(record, identity, idx)


def extract_file(harness, path):
    identity = transcript_identity(harness, path)
    idx = 0
    seen = set()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            turns = _parse(harness, record, identity, idx)
            if harness == "codex" and turns:
                payload = record.get("payload", {})
                event_id = payload.get("id") or payload.get("call_id")
                event_id = event_id if isinstance(event_id, str) else record.get("timestamp")
                unique = []
                for turn in turns:
                    key = (event_id, turn["role"], turn["text"])
                    if key not in seen:
                        seen.add(key)
                        unique.append(turn)
                turns = unique
            yield from turns
            idx += len(turns)


def render_slice(turns):
    lines = []
    for turn in turns:
        if turn["role"] == "user": lines.append(f"USER: {turn['text']}")
        elif turn["role"] == "assistant": lines.append(f"ASSISTANT: {turn['text']}")
        elif turn["role"] == "tool": lines.append(f"TOOL: {turn['text']}")
    return "\n".join(lines)


_PREFIX = {"user": "USER: ", "assistant": "ASSISTANT: ", "tool": "TOOL: "}
# Smallest per-turn share that still holds a role prefix and a truncation marker.
_MIN_TURN_BYTES = 64


def _truncate_turn(turn, cap, source_bytes=None):
    prefix = _PREFIX.get(turn["role"], "")
    encoded = (prefix + turn["text"]).encode()
    if len(encoded) <= cap:
        return turn
    marker = "[… {} bytes truncated]".format((source_bytes or len(encoded)) - cap)
    room = max(0, cap - len((prefix + marker).encode()))
    text = turn["text"].encode()[:room].decode(errors="ignore") + marker
    return {**turn, "text": text}


def _fair_shares(sizes, budget):
    shares, left = [0] * len(sizes), budget
    for n, i in enumerate(sorted(range(len(sizes)), key=sizes.__getitem__)):
        shares[i] = min(sizes[i], left // (len(sizes) - n))
        left -= shares[i]
    return shares


def _truncate_record(turns, cap):
    # The record is consumed whole, so every turn keeps a fair share of `cap` (#183).
    shown = [turn for turn in turns if turn["role"] in _PREFIX]
    most = max(1, (cap + 1) // (_MIN_TURN_BYTES + 1))
    if len(shown) > most:
        tail = shown[most - 1:]
        folded = "\n".join([tail[0]["text"]] + [render_slice([turn]) for turn in tail[1:]])
        shown = shown[:most - 1] + [{**tail[0], "text": folded}]
    sizes = [len(render_slice([turn]).encode()) for turn in shown]
    shares = _fair_shares(sizes, cap - (len(shown) - 1))
    return [_truncate_turn(turn, share) for turn, share in zip(shown, shares)]


def read_since(harness, path, offset, cap_bytes, upto=None):
    """`upto` bounds the slice: no record starting at or after it is read."""
    if harness not in {"claude-code", "codex"}:
        raise ValueError(f"unsupported harness: {harness}")
    identity = transcript_identity(harness, path)
    turns, total, end, stats = [], 0, offset, {"truncated_records": 0}
    with open(path, "rb") as fh:
        fh.seek(offset)
        while True:
            before = fh.tell()
            if upto is not None and before >= upto:
                break
            raw = fh.readline()
            if not raw or not raw.endswith(b"\n"):
                break
            try:
                record = json.loads(raw)
            except ValueError:
                end = fh.tell()
                continue
            parsed = _parse(harness, record, identity, len(turns))
            rendered = render_slice(parsed).encode()
            separator = 1 if total and rendered else 0
            if parsed and total + separator + len(rendered) > cap_bytes:
                if not turns and rendered and len(rendered) > cap_bytes:
                    stats["truncated_records"] += 1
                    turns.extend(_truncate_record(parsed, cap_bytes)); end = fh.tell()
                    total = cap_bytes
                    continue
                else:
                    fh.seek(before)
                break
            turns.extend(parsed)
            total += separator + len(rendered)
            end = fh.tell()
    return turns, end, stats


def session_opener(harness, path, max_bytes=4096):
    for turn in extract_file(harness, path):
        if turn["role"] == "user":
            return _truncate_turn(turn, max_bytes)["text"]
    return ""


def activity(turns):
    return {"user_turns": sum(t["role"] == "user" for t in turns),
            "tool_turns": sum(t["role"] == "tool" for t in turns),
            "work_tools": sum(t.get("tool_name") in {"Edit", "Write", "NotebookEdit", "MultiEdit"} for t in turns)}


def _ledger_path(store):
    return Path(store) / ".okfmem" / "capture" / "ledger.json"


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _load_ledger(store):
    path = _ledger_path(store)
    if not path.exists(): return {"catchup_floor": _utc(), "entries": {}}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict): raise ValueError("not an object")
        data.setdefault("entries", {})
        data.setdefault("catchup_floor", _utc())
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        corrupt = path.with_name(f"ledger.json.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
        fresh = {"catchup_floor": _utc(), "entries": {}}
        try:
            path.replace(corrupt)
        except OSError:
            logging.warning("corrupt capture ledger could not be moved aside: %s", path)
            return fresh
        logging.warning("corrupt capture ledger moved aside: %s", corrupt)
        _write_ledger(store, fresh)
        return fresh


def _write_ledger(store, data):
    path = _ledger_path(store); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n")
    os.replace(tmp, path)


def ledger_get(store, key):
    data = _load_ledger(store)
    entry = data["entries"].get(key)
    if not entry:
        return None
    transcript = entry.get("transcript")
    try:
        identity = transcript_identity(key.split(":", 1)[0], transcript)
        stale = (Path(transcript).stat().st_size < entry.get("offset", 0)
                 or identity["fingerprint"] != entry.get("fingerprint"))
    except (OSError, TypeError, ValueError):
        stale = True
    if stale:
        entry = {**entry, "offset": 0, "updated": _utc(), "outcome": "reset"}
        data["entries"][key] = entry
        _write_ledger(store, data)
    return entry


def ledger_put(store, key, **fields):
    data = _load_ledger(store)
    data["entries"][key] = fields
    _write_ledger(store, data)
    return fields


def uncaptured(store, harness, roots, older_than_s):
    data = _load_ledger(store); floor = data.get("catchup_floor", "")
    try: floor_ts = datetime.fromisoformat(floor).timestamp()
    except ValueError: floor_ts = 0
    found = []
    for root in roots:
        for path in Path(root).rglob("*.jsonl"):
            stat = path.stat()
            if time.time() - stat.st_mtime < older_than_s or stat.st_mtime < floor_ts: continue
            identity = transcript_identity(harness, path)
            key = f"{harness}:{identity['thread'] or identity['session']}"
            entry = ledger_get(store, key) or {}
            if stat.st_size > entry.get("offset", 0): found.append((stat.st_mtime, key, str(path), identity))
    for _, key, path, identity in sorted(found, reverse=True): yield key, path, identity
