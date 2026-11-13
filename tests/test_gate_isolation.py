"""The test gate's own contract (#217): `-n auto` parses on every machine, and
the engine's import-time defaults never resolve into the real home."""
import os
import subprocess
import sys

import conftest
from conftest import ROOT

sys.path.insert(0, os.path.join(ROOT, "plugins"))

import memory_distill as md  # noqa: E402


def test_import_time_source_roots_resolve_into_the_empty_test_home():
    home = os.path.normcase(os.path.realpath(conftest._SESSION_HOME))
    for root in (md.codex.DEFAULT_ROOT, md.claude_code.DEFAULT_ROOT,
                 md.agy.DEFAULT_PATH, md.DEFAULT_STORE):
        assert os.path.normcase(os.path.realpath(root)).startswith(home + os.sep), root


def test_gate_accepts_numprocesses_with_or_without_xdist():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-n", "2", "-p", "no:cacheprovider",
         os.path.join("tests", "test_memory_sync_lock.py")],
        cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert " passed" in r.stdout
