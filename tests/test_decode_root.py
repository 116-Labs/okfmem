import os
import sys

import pytest

import memory_init as mi


def _encode(path):
    return mi.encode_root(path)


def test_simple_posix_path_round_trips(tmp_path):
    project = tmp_path / "myproject"
    project.mkdir()
    encoded = _encode(str(project))
    assert mi.decode_root(encoded) == os.path.normpath(str(project))


def test_hyphenated_directory_name_round_trips(tmp_path):
    # The whole point of decode_root: directory names containing '-' must
    # still resolve correctly by probing the filesystem.
    project = tmp_path / "worktree-autosync"
    project.mkdir()
    encoded = _encode(str(project))
    assert mi.decode_root(encoded) == os.path.normpath(str(project))


def test_nested_hyphenated_directories_round_trip(tmp_path):
    nested = tmp_path / "my-cool-project" / "sub-dir-here"
    nested.mkdir(parents=True)
    encoded = _encode(str(nested))
    assert mi.decode_root(encoded) == os.path.normpath(str(nested))


@pytest.mark.parametrize("leaf", [
    "Application Support", ".local", "my_proj", "v1.2-rc", "Application-Support",
])
def test_non_alphanumeric_directory_names_round_trip(tmp_path, leaf):
    project = tmp_path / leaf / "gaal"
    project.mkdir(parents=True)
    assert mi.decode_root(_encode(str(project))) == os.path.normpath(str(project))


def test_space_and_dash_siblings_backtrack_to_the_existing_path(tmp_path):
    # Both siblings encode to `Application-Support`; only one holds `gaal`.
    (tmp_path / "Application-Support").mkdir()
    project = tmp_path / "Application Support" / "gaal"
    project.mkdir(parents=True)
    assert mi.decode_root(_encode(str(project))) == os.path.normpath(str(project))


def test_legacy_separator_only_name_still_decodes(tmp_path):
    project = tmp_path / "Application Support" / ".local"
    project.mkdir(parents=True)
    legacy = mi._legacy_encode_root(str(project))
    assert mi.decode_root(legacy) == os.path.normpath(str(project))


def test_same_encoded_full_matches_are_reported_ambiguous(tmp_path):
    for leaf in ("Application Support", "Application-Support"):
        (tmp_path / leaf / "gaal").mkdir(parents=True)
    _, problem = mi._resolve_encoded(_encode(str(tmp_path / "Application Support" / "gaal")))
    assert problem and "several" in problem


def test_legacy_name_picks_the_sibling_it_spells(tmp_path):
    for leaf in ("Application Support", "Application-Support"):
        (tmp_path / leaf / "gaal").mkdir(parents=True)
    project = tmp_path / "Application Support" / "gaal"
    legacy = mi._legacy_encode_root(str(project))
    assert mi._resolve_encoded(legacy) == (os.path.normpath(str(project)), None)


def test_build_registry_reports_ambiguous_root_as_drift(tmp_path):
    store = tmp_path / "store"
    (store / "projects" / "gaal").mkdir(parents=True)
    for leaf in ("Application Support", "Application-Support"):
        (tmp_path / leaf / "gaal").mkdir(parents=True)
    proj_dir = tmp_path / "claude_projects" / _encode(str(tmp_path / "Application Support" / "gaal"))
    proj_dir.mkdir(parents=True)
    os.symlink(str(store / "projects" / "gaal"), str(proj_dir / "memory"),
               target_is_directory=True)
    reg, drift = mi.build_registry(str(store), str(tmp_path / "claude_projects"))
    assert reg["map"] == {}
    assert len(drift) == 1 and "several" in drift[0]


def test_unresolvable_tail_falls_back_to_verbatim_reconstruction(tmp_path):
    # A path whose leaf doesn't exist on disk is kept verbatim under the
    # deepest directory the probe reached.
    encoded = _encode(str(tmp_path)) + "-does-not-exist"
    result = mi.decode_root(encoded)
    assert result == os.path.normpath(
        os.path.join(str(tmp_path), "does-not-exist"))


@pytest.mark.skipif(sys.platform == "win32",
                    reason="asserts the POSIX (no-op) behavior specifically")
def test_windows_drive_letter_helper_is_noop_on_real_posix():
    # On an actual POSIX machine, os.name is "posix", so the helper must
    # return None regardless of what tokens look like.
    assert mi._windows_drive_root(["C", "Users", "name", "project"]) is None


def test_windows_drive_letter_detected(monkeypatch):
    # Simulate the Windows branch portably: force os.name to "nt" and stub
    # os.path.isdir so the helper believes "C:\\" exists, without requiring
    # an actual Windows filesystem. A live-Windows integration case is below,
    # gated to only run on a real Windows runner (CI's windows-latest).
    monkeypatch.setattr(mi.os, "name", "nt")
    real_isdir = mi.os.path.isdir

    def fake_isdir(path):
        return True if path == "C:\\" else real_isdir(path)

    monkeypatch.setattr(mi.os.path, "isdir", fake_isdir)

    assert mi._windows_drive_root(["C", "Users", "name", "project"]) == ("C:\\", 1)


def test_windows_dot_directory_under_drive_root_keeps_its_dash(monkeypatch):
    monkeypatch.setattr(mi.os, "name", "nt")
    monkeypatch.setattr(mi.os.path, "isdir", lambda path: True)
    monkeypatch.setattr(
        mi.os, "listdir", lambda path: [".dotfiles"] if path == "C:\\" else [])
    assert os.path.basename(mi.decode_root("C---dotfiles")) == ".dotfiles"


def test_windows_name_without_drive_is_left_unresolved(monkeypatch):
    monkeypatch.setattr(mi.os, "name", "nt")
    assert mi._resolve_encoded("--server-share-repo")[0] is None
    assert mi.decode_root("--server-share-repo") == "--server-share-repo"


def test_windows_drive_letter_rejects_non_drive_first_token(monkeypatch):
    monkeypatch.setattr(mi.os, "name", "nt")
    assert mi._windows_drive_root(["Users", "name", "project"]) is None
    assert mi._windows_drive_root([]) is None


def test_windows_drive_letter_returns_none_when_drive_missing(monkeypatch):
    monkeypatch.setattr(mi.os, "name", "nt")
    monkeypatch.setattr(mi.os.path, "isdir", lambda path: False)
    assert mi._windows_drive_root(["Z", "Users", "name"]) is None


@pytest.mark.skipif(sys.platform != "win32",
                    reason="requires a real Windows filesystem")
def test_decode_root_with_real_windows_drive():
    # Only meaningful on an actual Windows runner (CI's windows-latest).
    encoded = "C-Users-doesnotexist-project"
    result = mi.decode_root(encoded)
    assert result.lower().startswith("c:\\")
