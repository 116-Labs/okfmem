import hashlib
import json
import os
from pathlib import Path
import re
import time

import pytest

import memory_transcript as mt


def write_jsonl(path, records, tail=b""):
    path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in records) + tail)


def claude(text, session="s1"):
    return {"type": "user", "sessionId": session, "timestamp": "now",
            "message": {"role": "user", "content": text}}


def codex_meta(session="real", cwd="project"):
    return {"type": "session_meta", "payload": {"id": session, "cwd": cwd}}


def codex_message(text):
    return {"type": "response_item", "timestamp": "now", "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}


@pytest.mark.parametrize("record_type", ["session-owner", "custom-title", "mode", "last-prompt"])
def test_claude_identity_reads_past_metadata_without_cwd(tmp_path, record_type):
    path = tmp_path / "sessions" / "session.jsonl"
    path.parent.mkdir()
    cwd = str(tmp_path / "repo")
    write_jsonl(path, [
        {"type": record_type, "sessionId": "S"},
        {"type": "queue-operation", "sessionId": "S"},
        {"type": "attachment", "sessionId": "S", "cwd": cwd},
    ])

    identity = mt.transcript_identity("claude-code", path)

    assert identity["session"] == "S" and identity["cwd"] == cwd
    first_line = path.read_bytes().splitlines(keepends=True)[0]
    assert identity["fingerprint"] == hashlib.sha256(first_line).hexdigest()


def test_claude_identity_keeps_fallbacks_when_cwd_is_beyond_record_limit(tmp_path):
    path = tmp_path / "sessions" / "session.jsonl"
    path.parent.mkdir()
    records = [{"type": "session-owner", "sessionId": "S"}]
    records.extend({"type": "queue-operation", "sessionId": "S"} for _ in range(199))
    records.append({"type": "attachment", "sessionId": "S", "cwd": str(tmp_path / "repo")})
    write_jsonl(path, records)

    identity = mt.transcript_identity("claude-code", path)

    assert identity["session"] == path.stem
    assert identity["cwd"] == "sessions"


def test_claude_identity_stops_at_byte_limit(tmp_path):
    path = tmp_path / "sessions" / "session.jsonl"
    path.parent.mkdir()
    records = [{"type": "session-owner", "sessionId": "S"}]
    records.extend({"type": "queue-operation", "sessionId": "S", "padding": "x" * 1500}
                   for _ in range(45))
    records.append({"type": "attachment", "sessionId": "S", "cwd": str(tmp_path / "repo")})
    write_jsonl(path, records)

    identity = mt.transcript_identity("claude-code", path)

    assert identity["session"] == path.stem
    assert identity["cwd"] == "sessions"


def test_read_since_is_contiguous_and_leaves_partial_line(tmp_path):
    path = tmp_path / "claude.jsonl"
    write_jsonl(path, [claude("one"), claude("two"), claude("three")], b'{"type":"user"')
    offset, seen = 0, []
    while True:
        turns, end, _ = mt.read_since("claude-code", path, offset, 12)
        seen.extend(turn["text"] for turn in turns)
        if end == offset:
            break
        offset = end
    assert seen == ["one", "two", "three"]
    assert offset < path.stat().st_size


def test_five_megabyte_transcript_is_covered_by_bounded_chunks(tmp_path):
    path = tmp_path / "large.jsonl"
    write_jsonl(path, [claude("x" * 2400) for _ in range(2100)])
    assert path.stat().st_size >= 5_000_000
    offset, seen = 0, []
    while offset < path.stat().st_size:
        turns, end, _ = mt.read_since("claude-code", path, offset, 200_000)
        assert len(mt.render_slice(turns).encode()) <= 200_000
        seen.extend(turn["text"] for turn in turns)
        assert end > offset
        offset = end
    assert seen == ["x" * 2400 for _ in range(2100)]


def test_chunk_that_fits_only_without_separator_stops_before_the_record(tmp_path):
    path = tmp_path / "exact.jsonl"
    write_jsonl(path, [claude("aaaa"), claude("bbbb")])
    turns, end, _ = mt.read_since("claude-code", path, 0, 20)
    assert len(mt.render_slice(turns).encode()) <= 20
    assert [turn["text"] for turn in turns] == ["aaaa"]
    turns, end, _ = mt.read_since("claude-code", path, end, 20)
    assert [turn["text"] for turn in turns] == ["bbbb"] and end == path.stat().st_size


def multi_block(texts):
    return {"type": "assistant", "sessionId": "s1", "timestamp": "now", "message": {
        "role": "assistant", "content": [{"type": "text", "text": t} for t in texts]}}


def test_oversized_multi_turn_record_keeps_every_turn_within_cap(tmp_path):
    path = tmp_path / "multi.jsonl"
    write_jsonl(path, [multi_block(["a" * 1000, "b" * 10, "c" * 1000])])
    turns, end, stats = mt.read_since("claude-code", path, 0, 300)
    rendered = mt.render_slice(turns)
    assert end == path.stat().st_size and stats["truncated_records"] == 1
    assert len(rendered.encode()) <= 300
    assert [turn["text"][0] for turn in turns] == ["a", "b", "c"]
    assert turns[1]["text"] == "b" * 10 and rendered.count("truncated") == 2


def test_oversized_record_with_more_turns_than_fit_folds_the_tail_within_cap(tmp_path):
    path = tmp_path / "many.jsonl"
    write_jsonl(path, [multi_block([f"t{i}-" + "x" * 200 for i in range(50)])])
    turns, end, stats = mt.read_since("claude-code", path, 0, 300)
    rendered = mt.render_slice(turns)
    assert end == path.stat().st_size and stats["truncated_records"] == 1
    assert len(rendered.encode()) <= 300 and len(turns) > 1
    assert "truncated" in turns[-1]["text"]


def test_core_memory_modules_do_not_import_plugins():
    root = Path(mt.__file__).parent
    imports = re.compile(r"^\s*(?:from\s+plugins(?:\.|\s)|import\s+plugins(?:\.|,|\s|$))")
    for path in root.glob("memory_*.py"):
        assert not any(imports.search(line) for line in path.read_text().splitlines()), path


def test_codex_identity_and_exec_wrapper_are_independent_of_slice_offset(tmp_path):
    path = tmp_path / "unknown.jsonl"
    call = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
            "input": "text(await tools.exec_command({cmd:'git status'}))"}}
    write_jsonl(path, [[], codex_meta(), codex_message("hello"), call])
    identity = mt.transcript_identity("codex", path)
    offset = len(json.dumps([]).encode()) + 1 + len(json.dumps(codex_meta()).encode()) + 1
    turns, _, _ = mt.read_since("codex", path, offset, 1000)
    assert identity["session"] == identity["thread"] == "real"
    assert identity["cwd"] == "project"
    assert mt.render_slice(turns).endswith("TOOL: git status")


def test_oversized_turn_is_truncated_and_secrets_and_tool_results_do_not_render(tmp_path):
    path = tmp_path / "x.jsonl"
    secret = "sk-abcdefghijklmnopqrst"
    write_jsonl(path, [claude("x" * 1_000_000 + secret), {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ghp_abcdefghijklmnop"}]}}])
    turns, end, stats = mt.read_since("claude-code", path, 0, 100)
    rendered = mt.render_slice(turns)
    assert end == path.stat().st_size and stats["truncated_records"] == 1
    assert len(rendered.encode()) <= 100 and "truncated" in rendered
    assert secret not in rendered and "ghp_" not in rendered


def test_ledger_round_trip_corruption_and_uncaptured_filters(tmp_path):
    store, root = tmp_path / "store", tmp_path / "sessions"
    root.mkdir()
    path = root / "s.jsonl"
    write_jsonl(path, [codex_meta("s"), codex_message("one")])
    old = time.time() - 20
    os.utime(path, (old, old))
    key = "codex:s"
    ledger = store / ".okfmem" / "capture" / "ledger.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"catchup_floor": "1970-01-01T00:00:00+00:00", "entries": {}}))
    identity = mt.transcript_identity("codex", path)
    mt.ledger_put(store, key, transcript=str(path), fingerprint=identity["fingerprint"], offset=1, updated="now", outcome="ok")
    assert mt.ledger_get(store, key)["offset"] == 1
    assert [item[0] for item in mt.uncaptured(store, "codex", [root], 1)] == [key]
    mt.ledger_put(store, key, transcript=str(path), fingerprint="f", offset=path.stat().st_size, updated="now", outcome="ok")
    path.write_bytes(b'{"type":"session_meta"}\n')
    reset = mt.ledger_get(store, key)
    assert reset["offset"] == 0 and reset["outcome"] == "reset"
    write_jsonl(path, [codex_meta("s"), codex_message("one")])
    identity = mt.transcript_identity("codex", path)
    mt.ledger_put(store, key, transcript=str(path), fingerprint=identity["fingerprint"], offset=path.stat().st_size, updated="now", outcome="ok")
    assert list(mt.uncaptured(store, "codex", [root], 1)) == []
    recent = root / "recent.jsonl"
    write_jsonl(recent, [codex_meta("recent"), codex_message("one")])
    old = root / "before-floor.jsonl"
    write_jsonl(old, [codex_meta("before-floor"), codex_message("one")])
    os.utime(old, (1262304000, 1262304000))
    ledger.write_text(json.dumps({"catchup_floor": "2000-01-01T00:00:00+00:00", "entries": {}}))
    assert {item[0] for item in mt.uncaptured(store, "codex", [root], 3600)} == {"codex:before-floor"}
    ledger.write_text(json.dumps({"catchup_floor": "2020-01-01T00:00:00+00:00", "entries": {}}))
    assert list(mt.uncaptured(store, "codex", [root], 3600)) == []
    ledger.write_text("not json")
    assert mt.ledger_get(store, key) is None
    assert list(ledger.parent.glob("ledger.json.corrupt-*"))
    replacement = json.loads(ledger.read_text())
    assert replacement["entries"] == {} and replacement["catchup_floor"]
