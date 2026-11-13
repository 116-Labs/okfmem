import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / "scripts" / "gate_test.py"

_spec = importlib.util.spec_from_file_location("gate_test", GATE)
gate_test = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate_test)


def test_pick_args_win32_runs_os_real_serially():
    name, args = gate_test.pick_args("win32", {})
    assert name == "os_real"
    assert "-m" in args and args[args.index("-m") + 1] == "os_real"
    assert "-n" not in args


def test_pick_args_posix_runs_full_suite_parallel():
    for platform in ("linux", "darwin"):
        name, args = gate_test.pick_args(platform, {})
        assert name == "full"
        assert args[args.index("-n") + 1] == "auto"
        assert "-m" not in args


def test_slice_full_forces_full_suite_on_win32():
    name, args = gate_test.pick_args("win32", {"OKFMEM_TEST_SLICE": "full"})
    assert name == "full"
    assert "-n" in args and "-m" not in args


def test_slice_os_real_forces_slice_on_linux():
    name, args = gate_test.pick_args("linux", {"OKFMEM_TEST_SLICE": "os_real"})
    assert name == "os_real"
    assert "-m" in args and "-n" not in args


def test_gate_script_prints_slice_first_and_returns_pytest_exit_code(tmp_path):
    failing = tmp_path / "test_fails.py"
    failing.write_text("def test_fails():\n    assert False\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(GATE), "-p", "no:cacheprovider", str(failing)],
        capture_output=True, text=True,
        env={**os.environ, "OKFMEM_TEST_SLICE": "full"},
    )
    assert result.returncode != 0
    assert result.stdout.splitlines()[0].startswith("gate_test: slice=full platform=")


def _is_windows_only_skipif(call):
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr == "skipif" and call.args):
        return False
    cond = ast.unparse(call.args[0]).replace("'", '"')
    return cond in ('os.name != "nt"', 'sys.platform != "win32"')


def _is_os_real(node):
    return isinstance(node, ast.Attribute) and node.attr == "os_real"


def unmarked_windows_only_tests(source):
    tree = ast.parse(source)
    module_marks = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            items = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) \
                else [node.value]
            module_marks = items
    module_os_real = any(_is_os_real(m) for m in module_marks)
    module_win_only = any(_is_windows_only_skipif(m) for m in module_marks)
    bad = []
    if module_win_only and not module_os_real:
        bad.append("<module>")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            decos = node.decorator_list
            if any(_is_windows_only_skipif(d) for d in decos) and not (
                    module_os_real or any(_is_os_real(d) for d in decos)):
                bad.append(node.name)
    return bad


def test_guard_flags_windows_only_skipif_without_os_real():
    src = (
        "import os, pytest\n"
        "@pytest.mark.skipif(os.name != 'nt', reason='x')\n"
        "def test_a():\n    pass\n"
    )
    assert unmarked_windows_only_tests(src) == ["test_a"]
    modsrc = (
        "import sys, pytest\n"
        "pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='x')\n"
    )
    assert unmarked_windows_only_tests(modsrc) == ["<module>"]


def test_guard_accepts_os_real_on_test_or_module():
    on_test = (
        "import os, pytest\n"
        "@pytest.mark.os_real\n"
        "@pytest.mark.skipif(os.name != 'nt', reason='x')\n"
        "def test_a():\n    pass\n"
    )
    on_module = (
        "import sys, pytest\n"
        "pytestmark = [pytest.mark.os_real,"
        " pytest.mark.skipif(sys.platform != 'win32', reason='x')]\n"
    )
    assert unmarked_windows_only_tests(on_test) == []
    assert unmarked_windows_only_tests(on_module) == []


def test_repository_tests_have_no_unmarked_windows_only_skipif():
    offenders = {}
    for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        bad = unmarked_windows_only_tests(path.read_text(encoding="utf-8"))
        if bad:
            offenders[path.name] = bad
    assert not offenders
