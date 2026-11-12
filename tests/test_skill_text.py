import re
from pathlib import Path

import memory_capture as mc

ROOT = Path(mc.__file__).parent
SAVE = ROOT / "skills" / "okfmem-save" / "SKILL.md"
OVERVIEW = ROOT / "skills" / "okfmem" / "SKILL.md"
CAPTURE_PROMPT = ROOT / "prompts" / "capture.md"
STATE_HEADINGS = ("## Summary", "## Left off", "## Next steps", "## Decisions", "## Blockers", "## Goal")
CAPTURE_BAR = "Never capture what a tool already records"


def _docs():
    return sorted([*(ROOT / "skills").rglob("*.md"), *(ROOT / "prompts").rglob("*.md")])


def _lines_with_headings(text):
    """(h3, h4, line) for each line outside a code fence; headings inside fences are code."""
    h3 = h4 = None
    fenced = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("### "):
            h3, h4 = line, None
        elif not fenced and line.startswith("#### "):
            h4 = line
        yield h3, h4, line


def _bash_commands(text, needle):
    blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
    joined = [cmd for b in blocks for cmd in b.replace("\\\n", " ").splitlines()]
    return [" ".join(c.split()) for c in joined if needle in c]


def test_no_skill_asks_whether_to_save():
    for path in (ROOT / "skills").rglob("*.md"):
        text = path.read_text(encoding="utf-8").lower()
        assert "save this" not in text, path
        assert "yes / edit" not in text, path
    save = SAVE.read_text(encoding="utf-8").lower()
    assert "after confirmation" not in save
    assert not re.search(r"save (it|these|them)\?", save)


def test_save_hands_off_with_focus_store_and_the_harness_session_id():
    commands = _bash_commands(SAVE.read_text(encoding="utf-8"), "okfmem capture --now")
    by_harness = {re.search(r"--harness (\S+)", c).group(1): c for c in commands}
    assert set(by_harness) == {"claude-code", "codex"}
    assert '--session "${CLAUDE_SESSION_ID}"' in by_harness["claude-code"]
    assert '--session "$CODEX_THREAD_ID"' in by_harness["codex"]
    assert "CLAUDE_SESSION_ID" not in by_harness["codex"]
    assert '--focus "$STORE_PATH/.okfmem/capture/focus-${CLAUDE_SESSION_ID}.md"' in by_harness["claude-code"]
    assert '--focus "$STORE_PATH/.okfmem/capture/focus-$CODEX_THREAD_ID.md"' in by_harness["codex"]
    for cmd in commands:
        assert "--focus " in cmd and '--store "$STORE_PATH"' in cmd and "--trigger manual" in cmd
        assert "--transcript" not in cmd


def test_exit_0_path_writes_nothing_and_sync_runs_only_on_the_fallback():
    text = SAVE.read_text(encoding="utf-8")
    exit0 = "\n".join(line for _, h4, line in _lines_with_headings(text)
                      if h4 and h4.startswith("#### Exit 0"))
    assert "queued" in exit0
    for forbidden in ("okfmem sync", "$PAGES_DIR", "$STATE_FILE", "`Write`", "state refresh", "--compile"):
        assert forbidden not in exit0, forbidden
    syncs = [(h3, line) for h3, _, line in _lines_with_headings(text) if "okfmem sync" in line]
    assert syncs
    for h3, line in syncs:
        assert h3 and "fallback" in h3.lower(), (h3, line)


def test_capture_bar_and_state_headings_live_only_in_the_capture_prompt():
    for path in _docs():
        text = path.read_text(encoding="utf-8")
        lines = set(text.splitlines())
        owner = path == CAPTURE_PROMPT
        assert (CAPTURE_BAR in text) == owner, path
        for heading in STATE_HEADINGS:
            assert (heading in lines) == owner, (path, heading)


def test_capture_prompt_state_shape_matches_what_the_engine_writes():
    written = mc._state_text({k: k for k in mc.STATE_SECTIONS}, "demo", "2026-01-01T00:00:00Z")
    engine = [ln for ln in written.splitlines() if ln.startswith("## ")]
    prompt = [ln for ln in CAPTURE_PROMPT.read_text(encoding="utf-8").splitlines()
              if ln in STATE_HEADINGS]
    assert engine == prompt == list(STATE_HEADINGS)


def test_save_documents_the_fallback_record_step_and_unknown_identity():
    text = SAVE.read_text(encoding="utf-8")
    assert "#### Exit 3" in text and "#### Exit 4" in text
    [record] = _bash_commands(text, "okfmem capture --record")
    assert "--upto" in record and "--outcome saved" in record and '--store "$STORE_PATH"' in record
    assert "automatic deduplication unavailable (session identity unknown)" in text
    assert "Codex/AGY" not in text


def test_overview_skill_describes_the_background_flow():
    rows = [ln for ln in OVERVIEW.read_text(encoding="utf-8").splitlines() if ln.startswith("| **")]
    assert "focus notes, background capture (foreground fallback)" in rows[0]
