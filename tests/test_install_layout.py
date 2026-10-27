import os
import subprocess
import sys
from pathlib import Path

import pytest

import memory_init as mi

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(sys.platform == "win32", reason="install.sh requires a POSIX environment")
def test_install_sh_fresh_store_creates_pages_and_marker(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True)
    store_dir = tmp_path / "store"

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["OKFMEM_STORE"] = str(store_dir)
    env["USERPROFILE"] = str(fake_home)

    res = subprocess.run(
        ["bash", str(REPO_ROOT / "install.sh")],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert res.returncode == 0, f"install.sh failed:\nstdout: {res.stdout}\nstderr: {res.stderr}"

    assert (store_dir / "pages").is_dir()
    assert not (store_dir / "projects").exists()
    layout_file = store_dir / "layout.yml"
    assert layout_file.is_file()
    assert "layout: labels" in layout_file.read_text(encoding="utf-8")
    assert mi.has_layout_marker(str(store_dir)) is True
    mi.check_store_layout(str(store_dir))


@pytest.mark.skipif(sys.platform == "win32", reason="install.sh requires a POSIX environment")
def test_fresh_install_init_and_consolidate_clean(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True)
    store_dir = tmp_path / "store"

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["OKFMEM_STORE"] = str(store_dir)
    env["USERPROFILE"] = str(fake_home)

    res = subprocess.run(
        ["bash", str(REPO_ROOT / "install.sh")],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert res.returncode == 0, res.stderr

    # Right after install: memory_init --yes emits no legacy projects/ layout error
    init_res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "memory_init.py"), "--yes", "--store", str(store_dir)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert "legacy projects/ layout" not in init_res.stdout
    assert "legacy projects/ layout" not in init_res.stderr

    # memory_consolidate --no-commit exits 0 on fresh install
    cons_res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "memory_consolidate.py"), "--no-commit", "--store", str(store_dir)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert cons_res.returncode == 0, f"consolidate failed:\nstdout: {cons_res.stdout}\nstderr: {cons_res.stderr}"


@pytest.mark.skipif(sys.platform == "win32", reason="install.sh requires a POSIX environment")
def test_install_sh_existing_store_leaves_unmigrated_store_without_marker(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True)
    store_dir = tmp_path / "legacy_store"
    (store_dir / "projects" / "myproject").mkdir(parents=True)
    subprocess.run(["git", "-C", str(store_dir), "init", "-q"], check=True)

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["OKFMEM_STORE"] = str(store_dir)
    env["USERPROFILE"] = str(fake_home)

    res = subprocess.run(
        ["bash", str(REPO_ROOT / "install.sh")],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert res.returncode == 0, res.stderr

    # Unmigrated store must NOT have marker added
    assert not (store_dir / "layout.yml").exists()
    assert mi.has_layout_marker(str(store_dir)) is False
    assert (store_dir / "projects").is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="install.sh requires a POSIX environment")
def test_install_sh_existing_migrated_store_preserved(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True)
    store_dir = tmp_path / "migrated_store"
    (store_dir / "pages").mkdir(parents=True)
    (store_dir / "layout.yml").write_text("layout: labels\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(store_dir), "init", "-q"], check=True)

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["OKFMEM_STORE"] = str(store_dir)
    env["USERPROFILE"] = str(fake_home)

    res = subprocess.run(
        ["bash", str(REPO_ROOT / "install.sh")],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert res.returncode == 0, res.stderr

    assert (store_dir / "layout.yml").read_text(encoding="utf-8") == "layout: labels\n"
    assert not (store_dir / "projects").exists()


def test_install_ps1_layout_contract():
    ps1_content = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")

    # Creates pages/ and layout.yml marker with layout: labels
    assert 'New-Item -ItemType Directory -Force -Path (Join-Path $StoreDir "pages")' in ps1_content
    assert 'Set-Content -Path (Join-Path $StoreDir "layout.yml") -Value "layout: labels"' in ps1_content

    # Does NOT create projects/
    assert 'Join-Path $StoreDir "projects"' not in ps1_content
    assert 'New-Item -ItemType Directory -Force -Path (Join-Path $StoreDir "projects")' not in ps1_content

    # Does not state that engine requires projects/
    assert "require a projects/ dir" not in ps1_content


def test_install_sh_layout_contract():
    sh_content = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")

    # Creates pages/ and layout.yml marker with layout: labels
    assert 'mkdir -p "$STORE_DIR/pages"' in sh_content
    assert 'echo "layout: labels" > "$STORE_DIR/layout.yml"' in sh_content

    # Does NOT create projects/
    assert 'mkdir -p "$STORE_DIR/projects"' not in sh_content

    # Does not state that engine requires projects/
    assert "require a projects/ dir" not in sh_content
