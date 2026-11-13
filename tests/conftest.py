import atexit
import os
import shutil
import sys
import tempfile

import pytest

# The engine modules live at the repo root, not under tests/ or a package —
# add it to sys.path so `import memory_sync` etc. work the same way the
# `okfmem` dispatcher's runpy invocation resolves them.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Module-level defaults (codex.DEFAULT_ROOT, DEFAULT_STORE, ...) resolve when the
# test modules import the engine, before any fixture runs; this conftest loads
# first, so they land in an empty folder, never the user's real data (#217).
_SESSION_HOME = tempfile.mkdtemp(prefix="okfmem-test-home-")
atexit.register(shutil.rmtree, _SESSION_HOME, ignore_errors=True)
os.environ["HOME"] = _SESSION_HOME
os.environ["USERPROFILE"] = _SESSION_HOME
for _var in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "OKFMEM_STORE", "OKFMEM_CACHE",
             "XDG_CACHE_HOME"):
    os.environ.pop(_var, None)

_XDIST_MISSING = False


def pytest_addoption(parser, pluginmanager):
    # The gate passes `-n auto` (#217); a machine without pytest-xdist runs the
    # suite serially rather than failing on an unknown option.
    global _XDIST_MISSING
    if pluginmanager.has_plugin("xdist") or pluginmanager.has_plugin("xdist.plugin"):
        return
    _XDIST_MISSING = True
    # Lowercase short options are reserved for plugins; xdist registers -n this way too.
    parser.getgroup("xdist")._addoption(
        "-n", "--numprocesses", dest="numprocesses", default=None, metavar="N",
        help="ignored: pytest-xdist is not installed, so the suite runs serially")


def pytest_terminal_summary(terminalreporter, config):
    if _XDIST_MISSING and config.getoption("numprocesses"):
        terminalreporter.write_line(
            "pytest-xdist is not installed: -n was ignored and the suite ran serially")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep tests and their child processes away from the developer's home."""
    home = tmp_path / ".isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
