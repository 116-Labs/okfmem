"""okfmem inbox import (#141): policy-driven, non-interactive import into a named
attached store, with provenance and an always-on leak gate."""
import json
import os
import shutil

import pytest

import memory_inbox as mx
from memory_frontmatter import find_frontmatter, parse_fields
from test_inbox import _clone, _git, _page, _push, _read, _write, env  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git binary not available")

POLICY = ("allowed_types: [feedback, project]\n"
          "allowed_repos:\n  - acme/demo\n"
          "max_page_bytes: 2000\n")


@pytest.fixture
def team(env):  # noqa: F811
    tmp = env["tmp"]
    origin = os.path.join(tmp, "team-origin.git")
    _git(tmp, "init", "--bare", "-q", origin)
    path = _clone(origin, os.path.join(tmp, "team-store"))
    _write(os.path.join(path, "projects", "demo", "MEMORY.md"), "# MEMORY — demo\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "seed")
    _git(path, "push", "-q", "-u", "origin", "HEAD")
    reg = os.path.join(env["store"], "registry.json")
    data = json.loads(_read(reg))
    data["stores"] = [{"name": "team", "path": path}]
    _write(reg, json.dumps(data))
    _git(env["store"], "add", "-A")
    _git(env["store"], "commit", "-qm", "attach team")
    _git(env["store"], "push", "-q", "origin", "HEAD")
    return path


def _candidates(env, **pages):
    shutil.rmtree(env["cand"])
    for name, text in pages.items():
        _write(os.path.join(env["cand"], name + ".md"), text)


def _run(env, team, policy=POLICY):
    mx.fetch_inbox(env["store"])
    out = []
    result = mx.import_policy(env["store"], team, mx.parse_policy(policy), "ci-bot",
                              out=out.append, do_sync=False)
    return result, out


def test_import_takes_only_policy_passing_pages_and_holds_the_rest_with_reasons(env, team):
    _candidates(env,
                good=_page("Good", "Pin the runner image"),
                wrongtype=_page("Wrong", "Some reference page", typ="reference"),
                huge=_page("Huge", "A very large page", body="x" * 3000),
                existing=_page("Existing rule v2", "Run the leak gate first"))
    _write(os.path.join(team, "projects", "demo", "existing.md"),
           _page("Existing rule", "Always run the leak gate"))
    _push(env)
    result, _ = _run(env, team)
    assert result["imported"] == 1 and result["held"] == 3
    assert os.path.isfile(os.path.join(team, "projects", "demo", "good.md"))
    held = {h["page"]: h["reasons"] for h in result["held_pages"]}
    assert "type 'reference' is not allowed" in held["wrongtype.md"][0]
    assert "exceeds cap" in held["huge.md"][0]
    assert "already exists" in held["existing.md"][0]
    assert not os.path.exists(os.path.join(env["store"], "projects", "demo", "good.md"))
    pending = mx.pending(env["store"])
    assert sorted(p["name"] for push_ in pending for p in push_["pages"]) == [
        "existing.md", "huge.md", "wrongtype.md"]


def test_imported_page_carries_provenance_and_replaces_claimed_values(env, team):
    forged = _page("Forged", "Claims its own origin").replace(
        "type: feedback\n", 'type: feedback\nsource: "trusted"\nimported_by: root\n')
    _candidates(env, forged=forged)
    _push(env)
    _run(env, team)
    text = _read(os.path.join(team, "projects", "demo", "forged.md"))
    fields = parse_fields(text, find_frontmatter(text))
    assert fields["source"] == "gaal" and fields["machine"] == "ak47"
    assert fields["ref"] == "#12" and fields["imported_by"] == "ci-bot"
    assert fields["imported_at"].endswith("Z")
    assert text.count("source:") == 1 and text.count("imported_by:") == 1


def test_provenance_value_cannot_inject_another_key():
    text = mx.with_provenance(_page("P", "d"), {"source": "a\nlabels: [x]", "ref": "r"}, "me")
    fm = find_frontmatter(text)
    assert "labels" not in parse_fields(text, fm)


def test_leak_gate_holds_a_page_with_a_secret_even_when_policy_otherwise_passes(env, team):
    _candidates(env, leaky=_page("Leaky", "Has a key",
                                 body="-----BEGIN RSA PRIVATE KEY-----\nabc\n"))
    _push(env)
    result, _ = _run(env, team)
    assert result["imported"] == 0
    assert "leak gate" in result["held_pages"][0]["reasons"][0]


@pytest.mark.parametrize("bad, key", [
    (POLICY + "extra: 1\n", "unknown key 'extra'"),
    ("allowed_types: [feedback]\nmax_page_bytes: 10\n", "missing key 'allowed_repos'"),
    (POLICY + "allowed_types: [x]\n", "duplicate key"),
    (POLICY.replace("2000", "big"), "positive integer"),
])
def test_policy_fails_closed_naming_the_key(bad, key):
    with pytest.raises(mx.PolicyError, match=key):
        mx.parse_policy(bad)


def test_unlabelled_repo_is_held(env, team):
    _candidates(env, good=_page("Good", "Pin the runner image"))
    _push(env)
    result, _ = _run(env, team, POLICY.replace("acme/demo", "other/repo"))
    assert result["imported"] == 0 and "not allowed" in result["held_pages"][0]["reasons"][0]


def test_resolve_into_fails_closed_on_unattached_name(env, team):
    path, why = mx.resolve_into(env["store"], "missing")
    assert path is None and "no attached store named 'missing'" in why
    assert mx.resolve_into(env["store"], "TEAM")[0] == team


def test_cli_json_reports_counts_and_exit_codes(env, team, monkeypatch, capsys):
    _candidates(env, good=_page("Good", "Pin the runner image"),
                bad=_page("Bad", "Wrong type here", typ="reference"))
    _push(env)
    policy = os.path.join(env["tmp"], "p.yml")
    _write(policy, POLICY)
    monkeypatch.setattr("sys.argv", ["memory_inbox.py", "import", "--store", env["store"],
                                     "--into", "team", "--non-interactive",
                                     "--policy", policy, "--json"])
    monkeypatch.setattr(mx, "sync_store",
                        lambda *a, **k: {"reason": "ok", "push_error": None})
    with pytest.raises(SystemExit) as e:
        mx.main()
    assert e.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert (payload["imported"], payload["held"], payload["skipped"]) == (1, 1, 0)


def _review_into(env, team, decide):
    mx.fetch_inbox(env["store"])
    out = []
    counts = mx.review(env["store"], decide, lambda repo: None, out=out.append,
                       do_sync=False, dest=team, imported_by="maintainer")
    return counts, out


def _accept_then_skip():
    seen = set()

    def decide(push_, page, flags):
        if page["name"] in seen:
            return ("skip", None)
        seen.add(page["name"])
        return ("accept", None)
    return decide


def test_review_into_writes_the_accepted_page_to_dest_with_provenance(env, team):
    _candidates(env, good=_page("Good", "Pin the runner image"))
    _push(env)
    counts, _ = _review_into(env, team, _accept_then_skip())
    assert counts["accepted"] == 1
    text = _read(os.path.join(team, "projects", "demo", "good.md"))
    fields = parse_fields(text, find_frontmatter(text))
    assert (fields["source"], fields["machine"], fields["ref"]) == ("gaal", "ak47", "#12")
    assert fields["imported_by"] == "maintainer" and fields["imported_at"].endswith("Z")
    assert not os.path.exists(os.path.join(env["store"], "projects", "demo", "good.md"))


def test_review_into_refuses_to_accept_a_page_the_leak_gate_hits(env, team):
    _candidates(env, leaky=_page("Leaky", "Has a key",
                                 body="-----BEGIN RSA PRIVATE KEY-----\nabc\n"))
    _push(env)
    counts, out = _review_into(env, team, _accept_then_skip())
    assert counts["accepted"] == 0
    assert not os.path.exists(os.path.join(team, "projects", "demo", "leaky.md"))
    assert any("leak gate: private key" in line for line in out)
    assert [p["name"] for push_ in mx.pending(env["store"]) for p in push_["pages"]] == [
        "leaky.md"]


def test_project_known_only_to_the_target_store_is_imported_there(env, team):
    _write(os.path.join(team, "projects", "widgets", "MEMORY.md"), "# MEMORY — widgets\n")
    _git(team, "add", "-A")
    _git(team, "commit", "-qm", "widgets")
    _candidates(env, good=_page("Good", "Pin the runner image"))
    code, _ = mx.push(env["worker"], env["cand"], "gaal", repo="acme/widgets",
                      ref="#12", machine="ak47", out=[].append)
    assert code == 0
    result, _ = _run(env, team, POLICY.replace("acme/demo", "acme/widgets"))
    assert result["imported"] == 1, result["held_pages"]
    assert os.path.isfile(os.path.join(team, "projects", "widgets", "good.md"))


@pytest.mark.parametrize("cmd", ["review", "import"])
def test_no_terminal_hint_keeps_into(cmd):
    hint = mx.no_terminal_hint(cmd, "/stores/home", "team")
    assert f"okfmem inbox {cmd} --store /stores/home --into team" in hint
    assert ("--non-interactive --policy FILE" in hint) == (cmd == "import")


def test_cli_refuses_unknown_store_and_policy_without_non_interactive(env, team, monkeypatch):
    for argv in (["import", "--into", "nope", "--non-interactive", "--policy", "x"],
                 ["import", "--policy", "x"]):
        monkeypatch.setattr("sys.argv", ["memory_inbox.py", "--store", env["store"], *argv])
        with pytest.raises(SystemExit) as e:
            mx.main()
        assert e.value.code == 2
