# Launch checklist

Tracked in [#3](https://github.com/116-Labs/okfmem/issues/3). The in-repo work
(license, sanitization, demo, comparison table, contributor docs, link audit) is
done. The steps below are **manual, post-release maintainer actions**: they run
after the release is merged, are performed by a person, and are never automated
by the engine or by CI.

## Before announcing

- [ ] `python3 scripts/check-leaks.py` passes.
- [ ] `python3 -m pytest -q` passes.
- [ ] Flip the repository to public.

## Announcements (maintainer, by hand)

- [ ] Post to r/ClaudeAI.
- [ ] Post a Show HN.
- [ ] LinkedIn build-in-public post.
- [ ] Submit to the awesome-claude-code list.

Draft copy and positioning notes are kept outside this repository.
