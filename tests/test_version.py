"""`okfmem --version` / `okfmem version` (#94): one line, version from VERSION."""
import os
import re
import shutil
import subprocess
import sys

from conftest import ROOT


def _run(script, *args):
    return subprocess.run([sys.executable, script, *args], capture_output=True, text=True)


def _version():
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def test_version_flag_and_subcommand_print_one_line():
    for arg in ("--version", "version"):
        r = _run(os.path.join(ROOT, "okfmem"), arg)
        assert r.returncode == 0, r.stderr
        lines = r.stdout.splitlines()
        assert len(lines) == 1
        assert re.fullmatch(rf"okfmem {re.escape(_version())} \(([0-9a-f]{{7,}}|unknown)\)",
                            lines[0]), lines[0]


def test_version_reports_unknown_outside_a_git_checkout(tmp_path):
    for name in ("okfmem", "VERSION"):
        shutil.copy(os.path.join(ROOT, name), tmp_path / name)
    r = _run(str(tmp_path / "okfmem"), "--version")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"okfmem {_version()} (unknown)"


def test_version_reports_unknown_when_install_is_nested_in_another_checkout(tmp_path):
    if not shutil.which("git"):
        return
    engine = tmp_path / "nested"
    engine.mkdir()
    for name in ("okfmem", "VERSION"):
        shutil.copy(os.path.join(ROOT, name), engine / name)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "tester"],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@e.st"],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "nested"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "seed"], check=True)

    r = _run(str(engine / "okfmem"), "--version")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"okfmem {_version()} (unknown)"
