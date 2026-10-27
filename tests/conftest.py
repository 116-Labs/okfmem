import os
import sys

import pytest

# The engine modules live at the repo root, not under tests/ or a package —
# add it to sys.path so `import memory_sync` etc. work the same way the
# `okfmem` dispatcher's runpy invocation resolves them.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep tests and their child processes away from the developer's home."""
    home = tmp_path / ".isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
