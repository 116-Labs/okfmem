#!/usr/bin/env python3
"""okfmem migrate-to-labels — flatten projects/ into pages/ with injected repo labels.

Big-bang migration from legacy `projects/<project>/` layout to flat `pages/`
label-indexed layout (#100).
- Moves non-colliding pages to `pages/<slug>.md` with `git mv`.
- Injects `labels: [repo:<project>]` (merged and deduplicated with existing labels).
- Moves `STATE.md` to `state/<project>.md`, byte-preserving the `modified:` field.
- Moves archived pages to `pages/archive/<year>/<slug>.md` with injected labels.
- Skips `MEMORY*.md`, `ck_*.md`, `legacy-ck/`, and non-markdown files (never deleted).
- Detects slug collisions across projects; prints collision worklist and emits
  `migrate-collisions.json` on apply, refusing to proceed until resolved.
- Writes layout marker (`layout.yml` and `registry.json`).
- Prompts for typed confirmation ('migrate') and symlink retarget ([y/N]).
- Commits as exactly one git commit, so `git revert` is the rollback.
- Builds initial views in `views/<repo>/` and validates link integrity.
"""
import argparse
from datetime import datetime, timezone
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_frontmatter import find_frontmatter, merge_labels, parse_fields  # noqa: E402
from memory_init import (  # noqa: E402
    _make_link,
    _is_junction,
    compile_view,
    has_layout_marker,
)
import memory_reindex as mr  # noqa: E402

MANUAL_RETARGET_HINT = "Retarget links later with: okfmem init"


def is_git_clean(store):
    """True if git status --porcelain reports no changes in store."""
    try:
        res = subprocess.run(
            ["git", "-C", store, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0 and res.stdout.strip() == ""
    except OSError:
        return False


def _git_mv_or_rename(store, rel_src, rel_dest):
    """Move file in store via git mv, falling back to os.rename if needed."""
    dest_path = os.path.join(store, rel_dest)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    try:
        r = subprocess.run(
            ["git", "-C", store, "mv", rel_src, rel_dest],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode == 0:
            return
    except OSError:
        pass
    src_path = os.path.join(store, rel_src)
    os.rename(src_path, dest_path)


def _inject_repo_label(text, project):
    """Inject `repo:<project>` into page frontmatter, preserving other bytes."""
    fm = find_frontmatter(text)
    if not fm:
        return f"---\nlabels: [repo:{project}]\n---\n\n{text}"
    return merge_labels(text, [f"repo:{project}"])


def _archive_year(text, default_year):
    """Extract archived_on year from frontmatter, falling back to created or default."""
    fm = find_frontmatter(text)
    if fm:
        fields = parse_fields(text, fm)
        arch_on = fields.get("archived_on", "")
        if len(arch_on) >= 4 and arch_on[:4].isdigit():
            return arch_on[:4]
        created = fields.get("created", "")
        if len(created) >= 4 and created[:4].isdigit():
            return created[:4]
    return default_year


def scan_store(store):
    """Scan store/projects/ and collect planned moves, skipped files, and collisions."""
    projects_dir = os.path.join(store, "projects")
    state_moves = []
    archive_moves = []
    skipped = []
    pages_by_slug = {}
    planned_archive_dests = set()
    current_year = str(datetime.now(timezone.utc).year)

    if not os.path.isdir(projects_dir):
        return {
            "state_moves": [],
            "archive_moves": [],
            "skipped": [],
            "unique_pages": [],
            "collisions": {},
            "projects": [],
        }

    for proj in sorted(os.listdir(projects_dir)):
        proj_dir = os.path.join(projects_dir, proj)
        if not os.path.isdir(proj_dir):
            skipped.append((f"projects/{proj}", "non-directory file under projects/"))
            continue

        # 1. STATE.md
        state_file = os.path.join(proj_dir, "STATE.md")
        if os.path.isfile(state_file):
            state_moves.append({
                "src": state_file,
                "rel_src": f"projects/{proj}/STATE.md",
                "dest": os.path.join(store, "state", f"{proj}.md"),
                "rel_dest": f"state/{proj}.md",
                "project": proj,
            })

        # 2. archive/
        archive_dir = os.path.join(proj_dir, "archive")
        if os.path.isdir(archive_dir):
            for fname in sorted(os.listdir(archive_dir)):
                afpath = os.path.join(archive_dir, fname)
                rel_path = f"projects/{proj}/archive/{fname}"
                if os.path.isdir(afpath):
                    skipped.append((rel_path, "directory"))
                    continue
                if not fname.endswith(".md"):
                    skipped.append((rel_path, "non-markdown file"))
                    continue
                try:
                    with open(afpath, "r", encoding="utf-8") as f:
                        text = f.read()
                except (OSError, UnicodeDecodeError):
                    text = ""
                slug = fname[:-3]
                year = _archive_year(text, current_year)
                dest_dir = os.path.join(store, "pages", "archive", year)
                cand_dest = os.path.join(dest_dir, f"{slug}.md")
                n = 2
                while cand_dest in planned_archive_dests or os.path.lexists(cand_dest):
                    cand_dest = os.path.join(dest_dir, f"{slug}-{n}.md")
                    n += 1
                planned_archive_dests.add(cand_dest)
                archive_moves.append({
                    "src": afpath,
                    "rel_src": rel_path,
                    "dest": cand_dest,
                    "rel_dest": os.path.relpath(cand_dest, store).replace("\\", "/"),
                    "project": proj,
                    "slug": slug,
                    "year": year,
                })

        # 3. Durable pages and skipped entries
        for fname in sorted(os.listdir(proj_dir)):
            fpath = os.path.join(proj_dir, fname)
            rel_path = f"projects/{proj}/{fname}"
            if fname in ("STATE.md", "archive"):
                continue
            if os.path.isdir(fpath):
                if fname == "legacy-ck":
                    skipped.append((rel_path, "legacy-ck directory"))
                else:
                    skipped.append((rel_path, "directory"))
                continue
            if fname.startswith("MEMORY") and fname.endswith(".md"):
                skipped.append((rel_path, "index regenerated by views"))
                continue
            if fname.startswith("ck_") and fname.endswith(".md"):
                skipped.append((rel_path, "retired ck snapshot"))
                continue
            if not fname.endswith(".md"):
                skipped.append((rel_path, "non-markdown file"))
                continue

            slug = fname[:-3]
            pages_by_slug.setdefault(slug, []).append({
                "src": fpath,
                "rel_src": rel_path,
                "dest": os.path.join(store, "pages", f"{slug}.md"),
                "rel_dest": f"pages/{slug}.md",
                "project": proj,
                "slug": slug,
            })

    collisions = {s: items for s, items in pages_by_slug.items() if len(items) > 1}
    unique_pages = [items[0] for s, items in pages_by_slug.items() if len(items) == 1]
    all_projects = sorted({
        p["project"] for p in unique_pages + state_moves + archive_moves
    })

    return {
        "state_moves": state_moves,
        "archive_moves": archive_moves,
        "skipped": skipped,
        "unique_pages": unique_pages,
        "collisions": collisions,
        "projects": all_projects,
    }


def scan_symlink_retargets(store):
    """Find ~/.claude/projects/*/memory links pointing to this store's projects."""
    claude_projects = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    retargets = []
    if not os.path.isdir(claude_projects):
        return retargets

    store_projects = os.path.realpath(os.path.join(store, "projects"))
    normalized_store_projects = os.path.normcase(store_projects)
    for entry in sorted(os.listdir(claude_projects)):
        link_path = os.path.join(claude_projects, entry, "memory")
        if os.path.islink(link_path) or _is_junction(link_path):
            try:
                target = os.path.realpath(link_path)
            except OSError:
                continue
            normalized_target = os.path.normcase(target)
            if (normalized_target == normalized_store_projects
                    or normalized_target.startswith(normalized_store_projects + os.sep)):
                proj_name = os.path.basename(target)
                new_target = os.path.join(store, "views", proj_name)
                retargets.append({
                    "link": link_path,
                    "entry": entry,
                    "project": proj_name,
                    "old_target": target,
                    "new_target": new_target,
                })
    return retargets


def format_plan(plan, symlinks):
    """Format human-readable migration plan."""
    lines = []
    lines.append("Planned page moves and label injections:")
    if plan["unique_pages"]:
        for p in plan["unique_pages"]:
            lines.append(f"  git mv {p['rel_src']} -> {p['rel_dest']} (inject label: repo:{p['project']})")
    else:
        lines.append("  none")
    lines.append("")

    lines.append("Planned STATE.md moves:")
    if plan["state_moves"]:
        for sm in plan["state_moves"]:
            lines.append(f"  git mv {sm['rel_src']} -> {sm['rel_dest']} (modified: preserved)")
    else:
        lines.append("  none")
    lines.append("")

    lines.append("Planned archive moves:")
    if plan["archive_moves"]:
        for am in plan["archive_moves"]:
            lines.append(f"  git mv {am['rel_src']} -> {am['rel_dest']} (inject label: repo:{am['project']})")
    else:
        lines.append("  none")
    lines.append("")

    lines.append("Skipped files:")
    if plan["skipped"]:
        for path, reason in plan["skipped"]:
            lines.append(f"  {path} (skipped: {reason})")
    else:
        lines.append("  none")
    lines.append("")

    lines.append("Collision worklist:")
    if plan["collisions"]:
        for slug, items in sorted(plan["collisions"].items()):
            lines.append(f"  slug: {slug}")
            for it in items:
                lines.append(f"    - {it['rel_src']}")
    else:
        lines.append("  none (no slug collisions)")
    lines.append("")

    lines.append("Planned symlink retargets:")
    if symlinks:
        for s in symlinks:
            lines.append(f"  {s['link']} -> {s['new_target']}")
    else:
        lines.append("  none")

    return "\n".join(lines)


def has_unmigrated_content(plan):
    """True if there are pages, state files, or archive files left to migrate."""
    return bool(plan["unique_pages"] or plan["state_moves"] or plan["archive_moves"] or plan["collisions"])


def migrate(
    store,
    dry_run=False,
    typed_confirm=None,
    retarget_confirm=None,
    out=sys.stdout,
):
    """Run migration or print dry-run plan."""
    store = os.path.abspath(os.path.expanduser(store))
    plan = scan_store(store)
    symlinks = scan_symlink_retargets(store)

    # Idempotent check: if layout marker is set and nothing remains to migrate
    if has_layout_marker(store) and not has_unmigrated_content(plan):
        print("Store is already migrated to label-indexed layout.", file=out)
        return 0

    mode = "dry-run" if dry_run else "apply"
    print(f"okfmem migrate-to-labels · {mode}\n", file=out)
    print(format_plan(plan, symlinks), file=out)

    if dry_run:
        return 0

    # Apply preconditions
    if not is_git_clean(store):
        print(
            f"error: store at '{store}' has uncommitted changes; "
            "working tree must be clean before migrating",
            file=sys.stderr,
        )
        return 1

    if plan["collisions"]:
        # Write migrate-collisions.json worklist in store root
        worklist = []
        for slug in sorted(plan["collisions"].keys()):
            variants = [it["rel_src"] for it in plan["collisions"][slug]]
            worklist.append({
                "slug": slug,
                "variants": variants,
                "paths": variants,
            })
        worklist_path = os.path.join(store, "migrate-collisions.json")
        with open(worklist_path, "w", encoding="utf-8") as f:
            json.dump(worklist, f, indent=2)
            f.write("\n")

        print(
            f"\nerror: migration blocked by {len(plan['collisions'])} slug collision set(s).\n"
            "Worklist written to migrate-collisions.json.\n"
            "Use the one-time migration prompt to resolve collisions before re-running migrate-to-labels.",
            file=sys.stderr,
        )
        return 1

    # Typed confirmation (destructive rung)
    print(
        "\nThis operation flattens projects/ into pages/ and commits all changes.",
        file=out,
    )
    if typed_confirm is not None:
        ans = typed_confirm
    else:
        try:
            ans = input("Type 'migrate' to confirm: ").strip()
        except EOFError:
            ans = ""
    if ans != "migrate":
        print(
            "error: migration cancelled (typed confirmation did not match 'migrate').",
            file=sys.stderr,
        )
        return 1

    # Symlink retarget confirmation (outward config rung)
    do_retarget = False
    if symlinks:
        if retarget_confirm is not None:
            do_retarget = retarget_confirm
            if not do_retarget:
                print("Skipping symlink retarget.", file=out)
                print(MANUAL_RETARGET_HINT, file=out)
        elif not sys.stdin.isatty():
            do_retarget = False
            print("Skipping symlink retarget (non-interactive run).", file=out)
            print(MANUAL_RETARGET_HINT, file=out)
        else:
            try:
                rans = input("Retarget ~/.claude memory links to views/? [y/N] ").strip().lower()
            except EOFError:
                rans = "n"
            if rans in ("y", "yes"):
                do_retarget = True
            else:
                do_retarget = False
                print("Skipping symlink retarget.", file=out)
                print(MANUAL_RETARGET_HINT, file=out)

    # Perform file moves
    os.makedirs(os.path.join(store, "pages"), exist_ok=True)
    os.makedirs(os.path.join(store, "state"), exist_ok=True)

    # 1. Unique pages
    for p in plan["unique_pages"]:
        _git_mv_or_rename(store, p["rel_src"], p["rel_dest"])
        dest_path = p["dest"]
        try:
            with open(dest_path, "r", encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            text = ""
        new_text = _inject_repo_label(text, p["project"])
        with open(dest_path, "w", encoding="utf-8") as f:
            f.write(new_text)

    # 2. STATE.md moves (byte-preserved)
    for sm in plan["state_moves"]:
        _git_mv_or_rename(store, sm["rel_src"], sm["rel_dest"])

    # 3. Archive moves
    for am in plan["archive_moves"]:
        os.makedirs(os.path.dirname(am["dest"]), exist_ok=True)
        _git_mv_or_rename(store, am["rel_src"], am["rel_dest"])
        dest_path = am["dest"]
        try:
            with open(dest_path, "r", encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            text = ""
        new_text = _inject_repo_label(text, am["project"])
        with open(dest_path, "w", encoding="utf-8") as f:
            f.write(new_text)

    # 4. Write layout marker
    layout_file = os.path.join(store, "layout.yml")
    with open(layout_file, "w", encoding="utf-8") as f:
        f.write("layout: labels\n")

    reg_path = os.path.join(store, "registry.json")
    if os.path.isfile(reg_path):
        try:
            with open(reg_path, "r", encoding="utf-8") as f:
                reg_data = json.load(f)
            if isinstance(reg_data, dict):
                reg_data["layout"] = "labels"
                with open(reg_path, "w", encoding="utf-8") as f:
                    json.dump(reg_data, f, indent=2)
                    f.write("\n")
        except (OSError, ValueError):
            pass

    # Clean up collision file if it was previously present
    worklist_path = os.path.join(store, "migrate-collisions.json")
    if os.path.isfile(worklist_path):
        try:
            os.remove(worklist_path)
        except OSError:
            pass

    # 5. Commit as exactly one git commit
    subprocess.run(["git", "-C", store, "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", store, "commit", "-m", "chore(migrate): migrate store to flat label-indexed layout"],
        check=True,
    )

    # 6. Symlink retarget
    if do_retarget:
        for s in symlinks:
            link = s["link"]
            new_target = s["new_target"]
            os.makedirs(new_target, exist_ok=True)
            try:
                if _is_junction(link):
                    os.rmdir(link)
                else:
                    os.unlink(link)
            except OSError:
                pass
            _make_link(new_target, link)
        print("Retargeted ~/.claude memory links to views/.", file=out)

    # 7. Build initial views
    for proj in plan["projects"]:
        compile_view(store, proj)

    # 8. Post-migration verify
    for proj in plan["projects"]:
        vdir = os.path.join(store, "views", proj)
        if os.path.isdir(vdir):
            scan = mr.Scan(vdir)
            if not scan.ok:
                print(
                    f"error: dangling links or orphans in compiled view '{proj}'",
                    file=sys.stderr,
                )
                return 1

    print("Post-migration check: okfmem reindex --verify passed.", file=out)
    print("\nMigration complete.", file=out)
    print(
        "IMPORTANT: Other workstations must update okfmem before pulling this store,\n"
        "otherwise older engine versions will misread the new layout.",
        file=out,
    )
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="okfmem migrate-to-labels",
        description="Flatten projects/ into pages/ with injected repo labels.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report planned moves and label injections without modifying disk.",
    )
    ap.add_argument(
        "--store",
        default=os.environ.get("OKFMEM_STORE", os.path.expanduser("~/okfmem-store")),
        help="Store directory (default: $OKFMEM_STORE or ~/okfmem-store).",
    )
    args = ap.parse_args(argv)
    code = migrate(args.store, dry_run=args.dry_run)
    sys.exit(code)


if __name__ == "__main__":
    main()
