import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_script(name: str) -> str:
    path = REPO_ROOT / name
    assert path.is_file(), f"missing script: {name}"
    return path.read_text(encoding="utf-8")


def test_install_ps1_defines_invoke_git_with_eap_relaxation():
    content = _read_script("install.ps1")
    assert "function Invoke-Git" in content
    assert "function Invoke-GitQuiet" in content
    assert "$ErrorActionPreference = 'Continue'" in content
    assert "finally {" in content


def test_uninstall_ps1_defines_invoke_git_with_eap_relaxation():
    content = _read_script("uninstall.ps1")
    assert "function Invoke-Git" in content
    assert "function Invoke-GitQuiet" in content
    assert "$ErrorActionPreference = 'Continue'" in content
    assert "finally {" in content


def test_no_unwrapped_git_stderr_redirection_in_install_ps1():
    content = _read_script("install.ps1")
    lines = content.splitlines()
    in_invoke_git = False
    unwrapped = []

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "function Invoke-Git " in line or "function Invoke-Git{" in line:
            in_invoke_git = True
        if in_invoke_git:
            if stripped == "}":
                in_invoke_git = False
            continue

        if re.search(r"\bgit\b.*(2>&1|2>\$null)", line):
            unwrapped.append((idx, line))

    assert not unwrapped, f"unwrapped git stderr redirection found: {unwrapped}"


def test_no_unwrapped_git_stderr_redirection_in_uninstall_ps1():
    content = _read_script("uninstall.ps1")
    lines = content.splitlines()
    in_invoke_git = False
    unwrapped = []

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "function Invoke-Git " in line or "function Invoke-Git{" in line:
            in_invoke_git = True
        if in_invoke_git:
            if stripped == "}":
                in_invoke_git = False
            continue

        if re.search(r"\bgit\b.*(2>&1|2>\$null)", line):
            unwrapped.append((idx, line))

    assert not unwrapped, f"unwrapped git stderr redirection found: {unwrapped}"


def test_install_ps1_defines_invoke_gh_with_eap_relaxation():
    content = _read_script("install.ps1")
    match = re.search(r"function Invoke-Gh \{.*?\n\}", content, re.DOTALL)
    assert match, "install.ps1 should define Invoke-Gh"
    body = match.group(0)
    assert "$ErrorActionPreference = 'Continue'" in body
    assert "finally {" in body


def test_no_unwrapped_gh_stderr_redirection_in_install_ps1():
    content = _read_script("install.ps1")
    in_invoke_gh = False
    unwrapped = []

    for idx, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "function Invoke-Gh " in line or "function Invoke-Gh{" in line:
            in_invoke_gh = True
        if in_invoke_gh:
            if stripped == "}":
                in_invoke_gh = False
            continue

        if re.search(r"\bgh\b.*(2>&1|2>\$null)", line):
            unwrapped.append((idx, line))

    assert not unwrapped, f"unwrapped gh stderr redirection found: {unwrapped}"


def test_install_ps1_judges_checkout_and_push_by_lastexitcode():
    content = _read_script("install.ps1")
    checkout_match = re.search(r"\$checkout\s*=\s*\(Invoke-Git\s+.*checkout", content)
    assert checkout_match, "checkout should be called via Invoke-Git"
    checkout_slice = content[checkout_match.end() : checkout_match.end() + 200]
    assert "$LASTEXITCODE" in checkout_slice

    push_match = re.search(r"\$push\s*=\s*\(Invoke-Git\s+.*push", content)
    assert push_match, "push should be called via Invoke-Git"
    push_slice = content[push_match.end() : push_match.end() + 200]
    assert "$LASTEXITCODE" in push_slice


def test_install_ps1_checkout_comment_explains_eap_protection():
    content = _read_script("install.ps1")
    checkout_pos = content.find("Invoke-Git @('-C', $StoreDir, 'checkout'")
    assert checkout_pos != -1
    preceding_comment = content[max(0, checkout_pos - 400) : checkout_pos]
    assert "NativeCommandError" in preceding_comment
    assert "EAP" in preceding_comment or "ErrorActionPreference" in preceding_comment
