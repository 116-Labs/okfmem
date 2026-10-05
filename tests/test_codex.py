import json
import subprocess
from pathlib import Path

import memory_codex as mc
import memory_init as mi
from plugins.adapters import codex


def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / "custom codex"))
    monkeypatch.setattr(
        mi.shutil, "which", lambda name: "/bin/codex" if name == "codex" else None
    )
    monkeypatch.setattr(mc, "supported", lambda: True)
    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    store = tmp_path / "private store"
    (store / "projects").mkdir(parents=True)
    return home, repo, store


def test_codex_only_init_idempotent(tmp_path, monkeypatch):
    home, repo, store = isolated(tmp_path, monkeypatch)
    override = Path(mi.codex_home()) / "AGENTS.override.md"
    override.parent.mkdir()
    override.write_text("My preferences\n")
    (store / "registry.json").write_text(
        json.dumps({"map": {}, "overrides": {str(repo): "renamed"}})
    )
    mi.cmd_run(str(store), False, False, assume_yes=True)
    resolved = mi.resolve_project(str(store))
    assert resolved["state"] == "ready" and resolved["project"] == "renamed"
    assert not (home / ".claude").exists()
    assert override.read_text().startswith("My preferences\n")
    assert str(store) in override.read_text()
    before = override.read_text()
    mi.cmd_run(str(store), False, False, assume_yes=True)
    assert override.read_text() == before
    assert mi.project_link_state(str(store))[0] == "no-claude"
    assert (home / ".agents/skills/okfmem-save").is_symlink()


def test_dry_run_and_decline(tmp_path, monkeypatch, capsys):
    _home, _repo, store = isolated(tmp_path, monkeypatch)
    mi.cmd_run(str(store), True, False)
    assert not Path(mi.codex_home()).exists()
    assert list((store / "projects").iterdir()) == []
    mi.cmd_run(str(store), False, False)
    assert not Path(mi.codex_home()).exists()
    assert mi.resolve_project(str(store))["state"] == "ready"
    assert "okfmem init --yes" in capsys.readouterr().out


def test_hook_preservation_and_teardown(tmp_path, monkeypatch):
    isolated(tmp_path, monkeypatch)
    path = Path(mi.codex_home()) / "hooks.json"
    path.parent.mkdir()
    other = {"hooks": [{"type": "command", "command": "echo unrelated"}]}
    path.write_text(json.dumps({"extra": True, "hooks": {"Stop": [other]}}))
    mc.wire_hooks("/tmp/store with spaces")
    data = json.loads(path.read_text())
    assert data["extra"] and data["hooks"]["Stop"][0] == other
    assert mc.wire_hooks("/tmp/store with spaces") == "unchanged"
    mc.wire_hooks("/tmp/store with spaces", remove=True)
    assert json.loads(path.read_text())["hooks"] == {"Stop": [other]}


def test_codex_rollouts(tmp_path, monkeypatch):
    _, repo, store = isolated(tmp_path, monkeypatch)
    (store / "registry.json").write_text(
        json.dumps({"overrides": {str(repo): "mapped"}})
    )
    records = [
        {
            "type": "session_meta",
            "payload": {"id": "synthetic-session", "cwd": str(repo)},
        },
        {
            "timestamp": "2026-01-01",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "u1",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "hello sk-abcdefghijklmnopqrstuv"}
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hello duplicate"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "tool1",
                "name": "exec_command",
                "arguments": '{"command":"git status"}',
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": "PRIVATE TOOL BODY"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "answer"}],
            },
        },
    ]
    path = tmp_path / "sessions" / "rollout.jsonl"
    path.parent.mkdir()
    path.write_text(
        "\n".join(json.dumps(r) for r in records + [records[1], [], {"payload": []}])
        + "\nmalformed"
    )
    turns = list(codex.iter_turns(path.parent, store=str(store)))
    assert len(turns) == 3
    assert all(
        t["project"] == "mapped" and t["session_id"] == "synthetic-session"
        for t in turns
    )
    assert "REDACTED" in turns[0]["text"]
    assert "PRIVATE" not in str(turns)
    assert list(codex.iter_turns(tmp_path / "missing")) == []
    assert list(codex.iter_turns(path.parent, project="other")) == []


def test_hook_boundary_ignores_transcript(tmp_path, monkeypatch):
    import io

    calls = []
    monkeypatch.setattr(
        mc.sys, "argv", ["memory_codex.py", "--event", "Stop", "--store", str(tmp_path)]
    )
    monkeypatch.setattr(
        mc.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {"cwd": str(tmp_path), "transcript_path": "/never/read/private.jsonl"}
            )
        ),
    )
    monkeypatch.setattr(
        mc.subprocess, "run", lambda argv, **kw: calls.append((argv, kw))
    )
    mc.main()
    assert len(calls) == 1
    assert "memory_consolidate.py" in calls[0][0][1]
    assert "--stdin-hook" not in calls[0][0] and "--transcript" not in calls[0][0]


def test_unsupported_and_absent_uninstall(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "not created"))
    monkeypatch.setattr(mc, "supported", lambda: False)
    assert "unsupported" in mc.wire_hooks(str(tmp_path))
    assert mc.wire_hooks(str(tmp_path), remove=True) == "absent"
    assert not Path(mi.codex_home()).exists()


def test_resolve_worktree_identity(tmp_path, monkeypatch):
    _, repo, store = isolated(tmp_path, monkeypatch)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "seed",
        ],
        check=True,
    )
    wt = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-qb", "branch", str(wt)],
        check=True,
    )
    assert mi.resolve_project(str(store), str(wt))["root"] == str(repo)


def test_search_and_distill_corpus_include_codex(tmp_path, monkeypatch):
    _, repo, store = isolated(tmp_path, monkeypatch)
    from plugins import memory_distill, memory_search

    root = tmp_path / "history"
    root.mkdir()
    (root / "rollout.jsonl").write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {"type": "session_meta", "payload": {"id": "test", "cwd": str(repo)}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "synthetic knowledge phrase",
                            }
                        ],
                    },
                },
            ]
        )
    )
    assert (
        next(
            iter(
                memory_search._all_turns(
                    str(tmp_path / "none"),
                    str(tmp_path / "none"),
                    str(root),
                    str(store),
                )
            )
        )["harness"]
        == "codex"
    )
    assert (
        next(
            iter(
                memory_distill.iter_corpus(
                    str(tmp_path / "none"),
                    str(tmp_path / "none"),
                    codex_root=str(root),
                    store=str(store),
                )
            )
        )["project"]
        == repo.name
    )


def test_uninstall_preserves_unrelated_codex_link(tmp_path, monkeypatch):
    home, _, _ = isolated(tmp_path, monkeypatch)
    import memory_uninstall as mu

    dest = home / ".agents" / "skills"
    dest.mkdir(parents=True)
    other = tmp_path / "user skill"
    other.mkdir()
    (dest / "okfmem").symlink_to(other)
    mu.unlink_skills(False, include_codex=True)
    assert (dest / "okfmem").is_symlink()


def test_cleanup_without_codex_binary(tmp_path, monkeypatch):
    home, _, store = isolated(tmp_path, monkeypatch)
    mi.cmd_run(str(store), False, False, assume_yes=True)
    import memory_uninstall as mu

    monkeypatch.setattr(mi.shutil, "which", lambda _: None)
    instructions = Path(mi.codex_home()) / "AGENTS.md"
    assert mu.remove_pointer(str(instructions), False) == "removed"
    mu.unlink_skills(False, include_codex=True)
    assert not (home / ".agents/skills/okfmem-save").exists()
    assert not (Path(mi.codex_home()) / "skills/okfmem-save").exists()
    assert mc.wire_hooks(str(store), remove=True) == "removed"
    assert (store / "projects").is_dir()


def test_codex_maintenance_preserves_claude_status(tmp_path, monkeypatch):
    import io
    from datetime import date
    from types import SimpleNamespace

    import memory_consolidate as consolidate

    _home, _, store = isolated(tmp_path, monkeypatch)
    badge = Path(consolidate.status_file_path())
    badge.parent.mkdir()
    badge.write_text("saved")
    trail = store / ".session-trail.md"
    trail.write_text("existing Claude session trail")
    monkeypatch.setattr(
        mc.sys, "argv", ["memory_codex.py", "--event", "Stop", "--store", str(store)]
    )
    monkeypatch.setattr(
        mc.sys,
        "stdin",
        io.StringIO(
            json.dumps({"cwd": str(tmp_path), "transcript_path": "/never/read"})
        ),
    )

    def child(argv, **kw):
        assert kw["env"]["OKFMEM_NO_STATUS"] == "1"
        with monkeypatch.context() as child_env:
            child_env.setenv("OKFMEM_NO_STATUS", kw["env"]["OKFMEM_NO_STATUS"])
            consolidate.update_statusline(
                SimpleNamespace(transcript=None, stdin_hook=False, dry_run=False),
                str(store),
                date(2026, 1, 1),
            )

    monkeypatch.setattr(mc.subprocess, "run", child)
    mc.main()
    assert badge.read_text() == "saved"
    assert trail.read_text() == "existing Claude session trail"


def test_codex_exec_cmd_signature(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "git status"}),
                },
            }
        )
    )
    assert next(iter(codex.extract_file(path)))["text"] == "git status"
