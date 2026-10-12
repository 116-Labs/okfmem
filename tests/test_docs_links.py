"""Docs hygiene: relative links and in-page anchors in the public docs resolve."""
import os
import re

from conftest import ROOT

DOCS = ["README.md", "CONTRIBUTING.md", os.path.join("docs", "LAUNCH.md")]
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")


def _slug(heading):
    text = heading.strip().lower().replace("`", "")
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path):
    with open(path, encoding="utf-8") as fh:
        return {_slug(m.group(1)) for m in re.finditer(r"^#+\s+(.*)$", fh.read(), re.M)}


def _links(path):
    with open(path, encoding="utf-8") as fh:
        text = re.sub(r"```.*?```", "", fh.read(), flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return LINK.findall(text)


def test_required_release_files_present():
    for rel in ("LICENSE", "CONTRIBUTING.md", ".github/ISSUE_TEMPLATE/bug_report.md",
                ".github/ISSUE_TEMPLATE/feature_request.md", "docs/LAUNCH.md"):
        assert os.path.exists(os.path.join(ROOT, rel)), rel


def test_relative_links_and_anchors_resolve():
    broken = []
    for rel in DOCS:
        src = os.path.join(ROOT, rel)
        for target in _links(src):
            if re.match(r"[a-z][a-z0-9+.-]*:", target):
                continue  # external URL
            path, _, frag = target.partition("#")
            dest = os.path.join(os.path.dirname(src), path) if path else src
            if not os.path.exists(dest):
                broken.append(f"{rel}: {target}")
            elif frag and dest.endswith(".md") and frag not in _anchors(dest):
                broken.append(f"{rel}: {target}")
    assert not broken, broken


def test_docs_do_not_link_private_design_docs():
    for rel in DOCS:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            text = fh.read()
        assert "okfmem-store/design" not in text, rel
        assert "internal/" not in text, rel
