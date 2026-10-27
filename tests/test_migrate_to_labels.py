"""Tests for okfmem migrate-to-labels (#100)."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import memory_init as mi
import memory_migrate as mm
import memory_reindex as mr


def _init_git_store(store_path):
    """Create a git repo at store_path with git user set and clean initial commit."""
    os.makedirs(store_path, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(store_path)], check=True)
    subprocess.run(["git", "-C", str(store_path), "config", "user.email", "author@example.com"], check=True)
    subprocess.run(["git", "-C", str(store_path), "config", "user.name", "Author"], check=True)


def _commit_store(store_path, msg="initial"):
    subprocess.run(["git", "-C", str(store_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(store_path), "commit", "-m", msg, "--allow-empty"], check=True)


def _setup_fixture_store(tmp_path, with_collision=False):
    """Setup a legacy projects/ fixture store with pages, state, archive, and skipped files."""
    store = tmp_path / "fixture-store"
    _init_git_store(store)

    # Project alpha
    alpha_dir = store / "projects" / "alpha"
    alpha_dir.mkdir(parents=True)
    (alpha_dir / "STATE.md").write_text("modified: 2026-10-26T12:00:00Z\n\n# State Alpha\n", encoding="utf-8")
    (alpha_dir / "page1.md").write_text(
        "---\nname: Page One\nimportance: 6\nlabels: [stack:python]\n---\n\n# Page One\nAlpha page 1.\n",
        encoding="utf-8",
    )
    (alpha_dir / "MEMORY.md").write_text("# MEMORY\n- [Page 1](page1.md)\n", encoding="utf-8")
    (alpha_dir / "ck_snapshot.md").write_text("# Retired snapshot\n", encoding="utf-8")
    (alpha_dir / "legacy-ck").mkdir()
    (alpha_dir / "legacy-ck" / "cruft.md").write_text("# Legacy cruft\n", encoding="utf-8")
    (alpha_dir / "notes.txt").write_text("Some text note\n", encoding="utf-8")

    alpha_archive = alpha_dir / "archive"
    alpha_archive.mkdir()
    (alpha_archive / "old1.md").write_text(
        "---\nname: Old One\narchived_on: 2025-04-10\nstatus: archived\n---\n\n# Old One\n",
        encoding="utf-8",
    )

    # Project beta
    beta_dir = store / "projects" / "beta"
    beta_dir.mkdir(parents=True)
    (beta_dir / "STATE.md").write_text("modified: 2026-10-26T13:00:00Z\n\n# State Beta\n", encoding="utf-8")
    (beta_dir / "page2.md").write_text(
        "---\nname: Page Two\nimportance: 3\n---\n\n# Page Two\nBeta page 2.\n",
        encoding="utf-8",
    )

    beta_archive = beta_dir / "archive"
    beta_archive.mkdir()
    # Archive file with same slug as alpha to test year-directory clash handling
    (beta_archive / "old1.md").write_text(
        "---\nname: Old One Beta\narchived_on: 2025-08-15\nstatus: archived\n---\n\n# Old One Beta\n",
        encoding="utf-8",
    )

    # Optional collision
    if with_collision:
        (alpha_dir / "collide.md").write_text(
            "---\nname: Collide Alpha\nimportance: 6\n---\n\n# Collide Alpha\n",
            encoding="utf-8",
        )
        (beta_dir / "collide.md").write_text(
            "---\nname: Collide Beta\nimportance: 3\n---\n\n# Collide Beta\n",
            encoding="utf-8",
        )
        # Inbound link to the colliding page
        (alpha_dir / "linker.md").write_text(
            "---\nname: Linker\n---\n\n# Linker\nSee [Collide](collide.md).\n",
            encoding="utf-8",
        )

    (store / "registry.json").write_text(json.dumps({"map": {}, "overrides": {}}, indent=2) + "\n", encoding="utf-8")

    _commit_store(store, "fixture setup")
    return store


def test_dry_run_prints_plan_and_writes_nothing(tmp_path):
    """AC1: --dry-run prints full plan and writes nothing to disk."""
    store = _setup_fixture_store(tmp_path, with_collision=False)
    out = io.StringIO()

    code = mm.migrate(str(store), dry_run=True, out=out)
    assert code == 0

    stdout = out.getvalue()
    assert "Planned page moves and label injections:" in stdout
    assert "Planned STATE.md moves:" in stdout
    assert "Planned archive moves:" in stdout
    assert "Skipped files:" in stdout
    assert "Collision worklist:" in stdout
    assert "Planned symlink retargets:" in stdout

    # Verify nothing was written to disk
    assert not (store / "pages").exists()
    assert not (store / "state").exists()
    assert not (store / "layout.yml").exists()
    assert not (store / "migrate-collisions.json").exists()


def test_apply_moves_pages_injects_labels_moves_state_and_archives(tmp_path):
    """AC2: Apply moves non-colliding pages, injects labels, moves STATE.md byte-preserved, and moves archives."""
    store = _setup_fixture_store(tmp_path, with_collision=False)
    out = io.StringIO()

    code = mm.migrate(str(store), dry_run=False, typed_confirm="migrate", retarget_confirm=False, out=out)
    assert code == 0

    # Non-colliding pages moved with git mv to pages/<slug>.md
    page1 = store / "pages" / "page1.md"
    assert page1.is_file()
    assert not (store / "projects" / "alpha" / "page1.md").exists()
    text1 = page1.read_text(encoding="utf-8")
    assert "labels: [stack:python, repo:alpha]" in text1
    assert "name: Page One" in text1
    assert "Alpha page 1." in text1

    page2 = store / "pages" / "page2.md"
    assert page2.is_file()
    assert not (store / "projects" / "beta" / "page2.md").exists()
    text2 = page2.read_text(encoding="utf-8")
    assert "labels: [repo:beta]" in text2

    # STATE.md moved with top-level modified: field byte-preserved
    state_alpha = store / "state" / "alpha.md"
    assert state_alpha.is_file()
    assert not (store / "projects" / "alpha" / "STATE.md").exists()
    state_alpha_text = state_alpha.read_text(encoding="utf-8")
    assert "modified: 2026-10-26T12:00:00Z" in state_alpha_text

    state_beta = store / "state" / "beta.md"
    assert state_beta.is_file()
    assert "modified: 2026-10-26T13:00:00Z" in state_beta.read_text(encoding="utf-8")

    # Archived pages moved to pages/archive/<year>/ with label injection & clash handled
    arch1 = store / "pages" / "archive" / "2025" / "old1.md"
    arch2 = store / "pages" / "archive" / "2025" / "old1-2.md"
    assert arch1.is_file()
    assert arch2.is_file()
    assert "labels: [repo:alpha]" in arch1.read_text(encoding="utf-8")
    assert "labels: [repo:beta]" in arch2.read_text(encoding="utf-8")


def test_apply_refuses_on_collision_and_applies_after_resolution(tmp_path, capsys):
    """AC3: Collision set refuses apply, writes worklist, and applies after resolution."""
    store = _setup_fixture_store(tmp_path, with_collision=True)
    out = io.StringIO()

    code = mm.migrate(str(store), dry_run=False, typed_confirm="migrate", retarget_confirm=False, out=out)
    assert code == 1

    worklist_file = store / "migrate-collisions.json"
    assert worklist_file.is_file()
    worklist = json.loads(worklist_file.read_text(encoding="utf-8"))
    assert len(worklist) == 1
    assert worklist[0]["slug"] == "collide"
    assert "projects/alpha/collide.md" in worklist[0]["variants"]
    assert "projects/beta/collide.md" in worklist[0]["variants"]

    # Nothing moved automatically
    assert not (store / "pages" / "collide.md").exists()
    assert (store / "projects" / "alpha" / "collide.md").is_file()
    assert (store / "projects" / "beta" / "collide.md").is_file()

    # Now resolve the collision: merge into alpha/collide.md with both labels, remove beta/collide.md
    merged_content = (
        "---\nname: Collide Merged\nimportance: 6\nlabels: [repo:alpha, repo:beta]\n---\n\n"
        "# Collide Merged\nCombined facts.\n"
    )
    (store / "projects" / "alpha" / "collide.md").write_text(merged_content, encoding="utf-8")
    (store / "projects" / "beta" / "collide.md").unlink()
    # Inbound link in linker.md still points to [Collide](collide.md)
    _commit_store(store, "resolve collision")

    # Re-run apply
    out2 = io.StringIO()
    code2 = mm.migrate(str(store), dry_run=False, typed_confirm="migrate", retarget_confirm=False, out=out2)
    assert code2 == 0

    assert (store / "pages" / "collide.md").is_file()
    assert "labels: [repo:alpha, repo:beta]" in (store / "pages" / "collide.md").read_text(encoding="utf-8")
    assert not worklist_file.exists()


def test_skipped_files_not_moved_or_deleted(tmp_path):
    """AC4: MEMORY*.md, ck_*.md, legacy-ck/, and non-markdown files are not moved or deleted."""
    store = _setup_fixture_store(tmp_path, with_collision=False)
    out = io.StringIO()

    code = mm.migrate(str(store), dry_run=False, typed_confirm="migrate", retarget_confirm=False, out=out)
    assert code == 0

    alpha = store / "projects" / "alpha"
    assert (alpha / "MEMORY.md").is_file()
    assert (alpha / "ck_snapshot.md").is_file()
    assert (alpha / "legacy-ck" / "cruft.md").is_file()
    assert (alpha / "notes.txt").is_file()


def test_layout_marker_and_engine_refusal_before(tmp_path):
    """AC5: Apply writes layout: labels marker; engine refuses projects/ without marker."""
    store = _setup_fixture_store(tmp_path, with_collision=False)

    # Engine refuses before migration
    with pytest.raises(ValueError) as exc:
        mi.check_store_layout(str(store))
    assert "okfmem migrate-to-labels" in str(exc.value)

    out = io.StringIO()
    code = mm.migrate(str(store), dry_run=False, typed_confirm="migrate", retarget_confirm=False, out=out)
    assert code == 0

    # Layout marker written
    assert (store / "layout.yml").is_file()
    assert "layout: labels" in (store / "layout.yml").read_text(encoding="utf-8")
    reg = json.loads((store / "registry.json").read_text(encoding="utf-8"))
    assert reg.get("layout") == "labels"

    # Engine now accepts store
    mi.check_store_layout(str(store))


def test_symlink_retarget_prompt_and_decline_regression(tmp_path, monkeypatch):
    """AC6: Symlink retarget requires its own [y/N]; declining or non-TTY leaves links untouched and prints manual command."""
    store = _setup_fixture_store(tmp_path, with_collision=False)

    # Create dummy claude projects directory with memory link
    claude_home = tmp_path / "claude_home"
    projects_dir = claude_home / ".claude" / "projects" / "-encoded-root"
    projects_dir.mkdir(parents=True)
    link_path = projects_dir / "memory"
    os.symlink(str(store / "projects" / "alpha"), str(link_path))

    monkeypatch.setenv("HOME", str(claude_home))
    monkeypatch.setenv("USERPROFILE", str(claude_home))

    # Regression test 1: decline [y/N]
    out = io.StringIO()
    code = mm.migrate(
        str(store),
        dry_run=False,
        typed_confirm="migrate",
        retarget_confirm=False,
        out=out,
    )
    assert code == 0
    stdout = out.getvalue()
    assert "Skipping symlink retarget." in stdout
    assert mm.MANUAL_RETARGET_HINT in stdout
    # Link still points to old target
    assert os.path.realpath(str(link_path)) == os.path.realpath(str(store / "projects" / "alpha"))

    # Reset store for accept test
    projects_dir2 = claude_home / ".claude" / "projects" / "-encoded-root2"
    projects_dir2.mkdir(parents=True)
    store2 = _setup_fixture_store(tmp_path / "store2", with_collision=False)
    link_path2 = projects_dir2 / "memory"
    os.symlink(str(store2 / "projects" / "alpha"), str(link_path2))

    out2 = io.StringIO()
    code2 = mm.migrate(
        str(store2),
        dry_run=False,
        typed_confirm="migrate",
        retarget_confirm=True,
        out=out2,
    )
    assert code2 == 0
    assert "Retargeted ~/.claude memory links to views/." in out2.getvalue()
    assert os.path.realpath(str(link_path2)) == os.path.realpath(str(store2 / "views" / "alpha"))


def test_junction_retarget_uses_rmdir(tmp_path, monkeypatch):
    store = _setup_fixture_store(tmp_path, with_collision=False)
    claude_home = tmp_path / "claude_home"
    link_path = claude_home / ".claude" / "projects" / "-encoded-root" / "memory"
    link_path.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(claude_home))
    monkeypatch.setenv("USERPROFILE", str(claude_home))

    original_realpath = os.path.realpath
    target = str(store / "projects" / "alpha")
    junction_path = os.path.normcase(os.path.normpath(str(link_path)))

    def is_test_junction(path):
        return os.path.normcase(os.path.normpath(os.fspath(path))) == junction_path

    monkeypatch.setattr(
        mm.os.path,
        "realpath",
        lambda path: target if is_test_junction(path) else original_realpath(path),
    )
    monkeypatch.setattr(mm, "_is_junction", is_test_junction)
    linked = []
    monkeypatch.setattr(mm, "_make_link", lambda dest, link: linked.append((dest, link)))

    assert len(mm.scan_symlink_retargets(str(store))) == 1
    code = mm.migrate(
        str(store),
        dry_run=False,
        typed_confirm="migrate",
        retarget_confirm=True,
        out=io.StringIO(),
    )

    assert code == 0
    assert not link_path.exists()
    assert linked == [
        (str(store / "views" / "alpha"), os.path.normpath(str(link_path)))
    ]


def test_dirty_tree_refusal_typed_confirmation_and_git_revert(tmp_path):
    """AC7: Apply refuses dirty tree, requires typed confirmation, commits once, and git revert restores original."""
    store = _setup_fixture_store(tmp_path, with_collision=False)

    # 1. Dirty store working tree refusal
    (store / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
    out = io.StringIO()
    code = mm.migrate(str(store), dry_run=False, typed_confirm="migrate", retarget_confirm=False, out=out)
    assert code == 1

    (store / "uncommitted.txt").unlink()

    # 2. Cancelled on invalid typed confirmation
    out2 = io.StringIO()
    code2 = mm.migrate(str(store), dry_run=False, typed_confirm="yes", retarget_confirm=False, out=out2)
    assert code2 == 1
    assert not (store / "pages").exists()

    # 3. Successful apply creates exactly one commit
    head_before = subprocess.run(
        ["git", "-C", str(store), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    out3 = io.StringIO()
    code3 = mm.migrate(str(store), dry_run=False, typed_confirm="migrate", retarget_confirm=False, out=out3)
    assert code3 == 0

    count = subprocess.run(
        ["git", "-C", str(store), "rev-list", "--count", f"{head_before}..HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert count == "1"

    # 4. git revert restores original layout
    subprocess.run(["git", "-C", str(store), "revert", "--no-edit", "HEAD"], check=True)
    assert (store / "projects" / "alpha" / "page1.md").is_file()
    assert (store / "projects" / "alpha" / "STATE.md").is_file()
    assert not (store / "pages" / "page1.md").exists()
    assert not (store / "layout.yml").exists()


def test_idempotent_apply_and_partial_cleanup(tmp_path):
    """AC8: Re-running apply is a no-op; partial runs complete on re-run."""
    store = _setup_fixture_store(tmp_path, with_collision=False)

    out1 = io.StringIO()
    code1 = mm.migrate(str(store), dry_run=False, typed_confirm="migrate", retarget_confirm=False, out=out1)
    assert code1 == 0

    # Second run is an idempotent no-op
    out2 = io.StringIO()
    code2 = mm.migrate(str(store), dry_run=False, typed_confirm="migrate", retarget_confirm=False, out=out2)
    assert code2 == 0
    assert "Store is already migrated to label-indexed layout." in out2.getvalue()

    # Partial run: layout marker present, but unmigrated page remains under projects/
    late_page = store / "projects" / "alpha" / "late.md"
    late_page.parent.mkdir(parents=True, exist_ok=True)
    late_page.write_text("---\ntype: project\n---\n\n# Late Page\n\nBody.\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(store), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(store), "commit", "-m", "add late unmigrated page"], check=True)

    head_before = subprocess.run(
        ["git", "-C", str(store), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    out3 = io.StringIO()
    code3 = mm.migrate(str(store), dry_run=False, typed_confirm="migrate", retarget_confirm=False, out=out3)
    assert code3 == 0
    assert not late_page.exists()
    migrated_late = store / "pages" / "late.md"
    assert migrated_late.is_file()
    assert "repo:alpha" in migrated_late.read_text(encoding="utf-8")

    count = subprocess.run(
        ["git", "-C", str(store), "rev-list", "--count", f"{head_before}..HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert count == "1"


def test_reindex_verify_exits_zero_and_workstation_warning(tmp_path):
    """AC9: After apply, reindex --verify exits 0, and closing warning is emitted."""
    store = _setup_fixture_store(tmp_path, with_collision=False)
    out = io.StringIO()

    code = mm.migrate(str(store), dry_run=False, typed_confirm="migrate", retarget_confirm=False, out=out)
    assert code == 0
    stdout = out.getvalue()
    assert "IMPORTANT: Other workstations must update okfmem before pulling this store" in stdout

    # okfmem reindex --verify on compiled views
    view_alpha = store / "views" / "alpha"
    assert view_alpha.is_dir()
    assert mr.main(["--verify", str(view_alpha)]) == 0

    view_beta = store / "views" / "beta"
    assert view_beta.is_dir()
    assert mr.main(["--verify", str(view_beta)]) == 0
