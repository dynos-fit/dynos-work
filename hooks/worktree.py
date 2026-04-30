#!/usr/bin/env python3
"""dynos worktree — migrate orphaned worktree persistent state into main.

Git worktrees used to get their own ~/.dynos/projects/{slug}/ directory
because _persistent_project_dir derived the slug from the absolute path of
the checkout. After the slug-normalization fix, worktrees fold back to the
main repo's slug — but existing split state is orphaned. This CLI
consolidates that state into main.

Subcommands:
  migrate <source-slug-path> [--execute]
      Move postmortems, prevention-rules, and learned-agents data from a
      worktree-slug persistent dir into the corresponding main-slug dir.
      Dry-run by default. --execute performs the migration.

  list-orphans
      Scan ~/.dynos/projects/* for dirs whose original path no longer
      exists OR whose git-resolved slug differs from the current dir name.
      Print a report. Non-destructive.

Usage:
  dynos worktree migrate /Users/me/.dynos/projects/my-project-worktree
  dynos worktree migrate /Users/me/.dynos/projects/my-project-worktree --execute
  dynos worktree list-orphans
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib_core import (
    _persistent_project_dir,
    _resolve_git_toplevel,
    load_json,
    now_iso,
    write_json,
)

# Resolve binaries at import time via PATH; never trust repo-relative paths.
_PYTHON3: str = shutil.which("python3") or _sys.executable
_GIT: str | None = shutil.which("git")


def _home() -> Path:
    import os
    return Path(__import__("os").environ.get("DYNOS_HOME", str(Path.home() / ".dynos")))


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _slugify(p: str) -> str:
    return p.strip("/").replace("/", "-")


def _read_global_registry() -> dict[str, Any]:
    """Load the global registry. Returns {"projects": []} on any error."""
    reg_path = _home() / "registry.json"
    if not reg_path.exists():
        return {"projects": []}
    try:
        return load_json(reg_path)
    except (json.JSONDecodeError, OSError):
        return {"projects": []}


def _original_path_for_slug(slug: str) -> Path | None:
    """Find the original checkout path for a persistent-dir slug.

    Resolution is ONLY via the global registry (`~/.dynos/registry.json`).
    Previously a "naive reverse" fallback replaced `-` with `/` and checked
    existence. That was unsafe: an attacker-crafted slug like `etc-passwd`
    would resolve to `/etc/passwd`, and the CLI would happily feed that path
    to subprocesses (patterns.py, registry.py unregister). Removed the
    fallback entirely. Unregistered projects are not migratable by slug;
    callers must register first or point at the source dir directly.
    """
    reg = _read_global_registry()
    for entry in reg.get("projects", []):
        p = entry.get("path")
        if not isinstance(p, str):
            continue
        if _slugify(str(Path(p).resolve())) == slug:
            return Path(p)
    return None


def _assert_source_contained(source: Path) -> None:
    """Reject source paths that resolve outside `~/.dynos/projects/`.

    Defense against a crafted symlink or typo that would let `--execute`
    `rm -rf` arbitrary directories. Also rejects symlinked source paths
    (we don't want to follow the link and operate on the target).
    """
    if source.is_symlink():
        raise ValueError(f"source is a symlink; refuse to migrate: {source}")
    projects_root = (_home() / "projects").resolve()
    source_resolved = source.resolve()
    try:
        source_resolved.relative_to(projects_root)
    except ValueError:
        raise ValueError(
            f"source must live under {projects_root}, got {source_resolved}"
        )
    # Also reject if source is projects_root itself or shallower
    if source_resolved == projects_root:
        raise ValueError("source must not be the projects root itself")


def _resolve_main_slug_for_source(source_dir: Path) -> str | None:
    """Given a persistent dir like ~/.dynos/projects/foo-bar-baz, find which
    ORIGINAL checkout path it represents (via the global registry), then ask
    git for that checkout's main-worktree path. Return the main slug, or None
    if we can't resolve.
    """
    slug = source_dir.name
    original = _original_path_for_slug(slug)
    if original is None or not original.exists():
        return None
    resolved = _resolve_git_toplevel(str(original))
    if resolved is None:
        return None
    return _slugify(resolved)


def _plan_migration(source: Path, target: Path) -> dict[str, Any]:
    """Compute what a migration would do — called by both dry-run and --execute."""
    plan: dict[str, Any] = {
        "source": str(source),
        "target": str(target),
        "postmortems_to_copy": [],
        "prevention_rules_new": [],
        "learned_agents_new": [],
        "benchmarks_new": [],
        "not_migrated": [],
        "warnings": [],
    }

    # Postmortems
    src_pm = source / "postmortems"
    tgt_pm = target / "postmortems"
    if src_pm.is_dir():
        for pm_file in sorted(src_pm.glob("*.json")):
            tgt_file = tgt_pm / pm_file.name
            if tgt_file.exists():
                plan["warnings"].append(f"postmortem {pm_file.name} already exists in target; skipping")
            else:
                plan["postmortems_to_copy"].append(pm_file.name)
        for pm_file in sorted(src_pm.glob("*.md")):
            tgt_file = tgt_pm / pm_file.name
            if tgt_file.exists():
                continue
            plan["postmortems_to_copy"].append(pm_file.name)

    # Prevention rules — dedup on (source_task, source_finding, rule)
    src_rules_path = source / "prevention-rules.json"
    tgt_rules_path = target / "prevention-rules.json"
    if src_rules_path.exists():
        src_data = load_json(src_rules_path)
        tgt_data: dict[str, Any] = {}
        if tgt_rules_path.exists():
            try:
                tgt_data = load_json(tgt_rules_path)
            except (json.JSONDecodeError, OSError):
                tgt_data = {}
        seen = {
            (r.get("source_task"), r.get("source_finding"), r.get("rule"))
            for r in tgt_data.get("rules", [])
        }
        for r in src_data.get("rules", []):
            k = (r.get("source_task"), r.get("source_finding"), r.get("rule"))
            if k not in seen:
                plan["prevention_rules_new"].append(r.get("rule", "?"))

    # Learned agents — empty in the observed case; note path rewriting requirement
    src_reg = source / "learned-agents" / "registry.json"
    if src_reg.exists():
        try:
            src_reg_data = load_json(src_reg)
        except (json.JSONDecodeError, OSError):
            src_reg_data = {}
        agents = src_reg_data.get("agents", [])
        if agents:
            for a in agents:
                plan["learned_agents_new"].append(a.get("agent_name", "?"))
            plan["warnings"].append(
                "learned-agents present in source — paths inside registry.json will be rewritten "
                "from the source slug to the target slug"
            )

    # Benchmarks history — dedup on run_id
    src_hist = source / "benchmarks" / "history.json"
    if src_hist.exists():
        try:
            src_hist_data = load_json(src_hist)
        except (json.JSONDecodeError, OSError):
            src_hist_data = {}
        tgt_hist_path = target / "benchmarks" / "history.json"
        tgt_hist_data: dict = {}
        if tgt_hist_path.exists():
            try:
                tgt_hist_data = load_json(tgt_hist_path)
            except (json.JSONDecodeError, OSError):
                tgt_hist_data = {}
        seen_runs = {r.get("run_id") for r in tgt_hist_data.get("runs", []) if isinstance(r, dict)}
        for r in src_hist_data.get("runs", []):
            if isinstance(r, dict) and r.get("run_id") not in seen_runs:
                plan["benchmarks_new"].append(r.get("run_id", "?"))

    # Explicitly not migrated
    for skip_name in ("policy.json", "route-policy.json", "skip-policy.json",
                      "model-policy.json", "effectiveness-scores.json",
                      "project_rules.md"):
        if (source / skip_name).exists():
            plan["not_migrated"].append(skip_name)

    return plan


def _execute_migration(source: Path, target: Path, main_root: Path) -> dict[str, Any]:
    """Actually perform the migration. Backs up both dirs first.

    Backups and copies use symlinks=True so planted symlinks inside the
    source dir are preserved AS symlinks (not followed). Migration then
    skips symlinked files explicitly to avoid ingesting arbitrary content
    into prevention-rules / postmortems / registry.
    """
    # Backup (preserve symlinks; do not follow them into the filesystem)
    ts = _timestamp()
    source_bak = source.with_name(source.name + f".bak-{ts}")
    target_bak = target.with_name(target.name + f".bak-{ts}")
    shutil.copytree(source, source_bak, symlinks=True)
    if target.exists():
        shutil.copytree(target, target_bak, symlinks=True)
    target.mkdir(parents=True, exist_ok=True)

    applied: dict[str, Any] = {
        "backups": {"source": str(source_bak), "target": str(target_bak)},
        "postmortems_copied": 0,
        "prevention_rules_added": 0,
        "learned_agents_added": 0,
        "benchmarks_added": 0,
    }

    # Postmortems — skip symlinks explicitly; we don't want to ingest
    # arbitrary file content into the target's postmortem dir.
    src_pm = source / "postmortems"
    tgt_pm = target / "postmortems"
    if src_pm.is_dir() and not src_pm.is_symlink():
        tgt_pm.mkdir(parents=True, exist_ok=True)
        for pm_file in sorted(src_pm.iterdir()):
            if pm_file.is_symlink():
                applied.setdefault("warnings", []).append(
                    f"skipped symlinked source: {pm_file.name}"
                )
                continue
            tgt_file = tgt_pm / pm_file.name
            if pm_file.is_file() and not tgt_file.exists():
                shutil.copy2(pm_file, tgt_file, follow_symlinks=False)
                applied["postmortems_copied"] += 1

    # Prevention rules JSON-merge (refuse symlinked source)
    src_rules = source / "prevention-rules.json"
    tgt_rules = target / "prevention-rules.json"
    if src_rules.exists() and not src_rules.is_symlink():
        src_data = load_json(src_rules)
        tgt_data: dict[str, Any] = {"rules": []}
        if tgt_rules.exists():
            try:
                tgt_data = load_json(tgt_rules)
            except (json.JSONDecodeError, OSError):
                tgt_data = {"rules": []}
        seen = {
            (r.get("source_task"), r.get("source_finding"), r.get("rule"))
            for r in tgt_data.get("rules", [])
        }
        added_rules = 0
        for r in src_data.get("rules", []):
            k = (r.get("source_task"), r.get("source_finding"), r.get("rule"))
            if k not in seen:
                tgt_data.setdefault("rules", []).append(r)
                seen.add(k)
                added_rules += 1
        if added_rules:
            tgt_data["updated_at"] = now_iso()
            write_json(tgt_rules, tgt_data)
        applied["prevention_rules_added"] = added_rules

    # Learned agents — copy .md files, merge registry.json with path rewrite.
    # Reject symlinked dirs/files throughout.
    src_la_dir = source / "learned-agents"
    tgt_la_dir = target / "learned-agents"
    if src_la_dir.is_dir() and not src_la_dir.is_symlink():
        for subdir in ("executors", "auditors", "skills"):
            src_sub = src_la_dir / subdir
            if src_sub.is_dir() and not src_sub.is_symlink():
                tgt_sub = tgt_la_dir / subdir
                tgt_sub.mkdir(parents=True, exist_ok=True)
                for md in src_sub.glob("*.md"):
                    if md.is_symlink():
                        continue
                    tgt_md = tgt_sub / md.name
                    if not tgt_md.exists():
                        shutil.copy2(md, tgt_md, follow_symlinks=False)
        # Merge registry.json with path rewrites
        src_reg = src_la_dir / "registry.json"
        if src_reg.exists() and not src_reg.is_symlink():
            src_reg_data = load_json(src_reg)
            tgt_reg_path = tgt_la_dir / "registry.json"
            tgt_reg_data: dict[str, Any] = {"version": 1, "agents": [], "benchmarks": []}
            if tgt_reg_path.exists():
                try:
                    tgt_reg_data = load_json(tgt_reg_path)
                except (json.JSONDecodeError, OSError):
                    pass
            source_slug = source.name
            target_slug = target.name
            existing_keys = {
                (a.get("agent_name"), a.get("role"), a.get("task_type"),
                 a.get("item_kind", "agent"))
                for a in tgt_reg_data.get("agents", [])
            }
            added_agents = 0
            for agent in src_reg_data.get("agents", []):
                key = (agent.get("agent_name"), agent.get("role"), agent.get("task_type"),
                       agent.get("item_kind", "agent"))
                if key in existing_keys:
                    continue
                # Rewrite embedded absolute paths
                rewritten = dict(agent)
                for pk in ("path", "fixture_path"):
                    v = rewritten.get(pk)
                    if isinstance(v, str) and source_slug in v:
                        rewritten[pk] = v.replace(source_slug, target_slug)
                # Repo-rooted paths (fixture_path) — rewrite if they embed the source repo's resolved path
                tgt_reg_data.setdefault("agents", []).append(rewritten)
                existing_keys.add(key)
                added_agents += 1
            if added_agents:
                tgt_reg_data["updated_at"] = now_iso()
                tgt_la_dir.mkdir(parents=True, exist_ok=True)
                write_json(tgt_reg_path, tgt_reg_data)
            applied["learned_agents_added"] = added_agents

    # Benchmarks history
    src_hist = source / "benchmarks" / "history.json"
    if src_hist.exists() and not src_hist.is_symlink():
        src_hist_data = load_json(src_hist)
        tgt_hist_path = target / "benchmarks" / "history.json"
        tgt_hist_data: dict[str, Any] = {"runs": []}
        if tgt_hist_path.exists():
            try:
                tgt_hist_data = load_json(tgt_hist_path)
            except (json.JSONDecodeError, OSError):
                tgt_hist_data = {"runs": []}
        seen_runs = {r.get("run_id") for r in tgt_hist_data.get("runs", []) if isinstance(r, dict)}
        added_hist = 0
        for r in src_hist_data.get("runs", []):
            if isinstance(r, dict) and r.get("run_id") not in seen_runs:
                tgt_hist_data.setdefault("runs", []).append(r)
                seen_runs.add(r.get("run_id"))
                added_hist += 1
        if added_hist:
            tgt_hist_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(tgt_hist_path, tgt_hist_data)
        applied["benchmarks_added"] = added_hist

    return applied


def cmd_migrate(args: argparse.Namespace) -> int:
    source_arg = Path(args.source)
    if not source_arg.exists():
        print(json.dumps({"ok": False, "error": f"source does not exist: {source_arg}"}))
        return 2
    # Containment + symlink guard MUST happen before resolve() on a symlink
    # would otherwise follow the link and let us operate on the target.
    try:
        _assert_source_contained(source_arg)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    source = source_arg.resolve()
    if not source.is_dir():
        print(json.dumps({"ok": False, "error": f"source is not a directory: {source}"}))
        return 2

    # Infer the main slug from the source slug
    target_slug = _resolve_main_slug_for_source(source)
    if target_slug is None:
        print(json.dumps({
            "ok": False,
            "error": (
                f"could not resolve source slug '{source.name}' to a main repo. "
                "The original checkout path may be missing, or not inside a git repo."
            )
        }))
        return 2

    target = _home() / "projects" / target_slug
    if target_slug == source.name:
        print(json.dumps({
            "ok": False,
            "error": f"source and target resolve to the same slug ({target_slug}); nothing to migrate"
        }))
        return 1

    plan = _plan_migration(source, target)

    if not args.execute:
        plan["dry_run"] = True
        print(json.dumps(plan, indent=2))
        return 0

    # Execute
    main_root = _original_path_for_slug(target_slug)
    applied = _execute_migration(source, target, main_root or target)

    # Regenerate project_rules.md on target (if main_root exists)
    if main_root and main_root.exists():
        patterns_py = Path(__file__).parent / "patterns.py"
        if patterns_py.exists():
            try:
                subprocess.run(
                    [_PYTHON3, str(patterns_py), "--root", str(main_root)],
                    check=False, capture_output=True, text=True, timeout=60,
                )
                applied["project_rules_regenerated"] = True
            except (subprocess.TimeoutExpired, OSError):
                applied["project_rules_regenerated"] = False

    # Unregister source from global registry (the original checkout path)
    source_original_path = _original_path_for_slug(source.name)
    if source_original_path is not None:
        registry_py = Path(__file__).parent / "registry.py"
        if registry_py.exists():
            subprocess.run(
                [_PYTHON3, str(registry_py), "unregister", str(source_original_path)],
                check=False, capture_output=True, text=True, timeout=10,
            )
            applied["registry_unregistered"] = str(source_original_path)

    # Remove source dir (backup was taken)
    shutil.rmtree(source)
    applied["source_removed"] = str(source)

    print(json.dumps({"ok": True, "applied": applied}, indent=2))
    return 0


def cmd_list_orphans(args: argparse.Namespace) -> int:
    """List persistent-dir slugs that don't match their git-resolved slug."""
    home = _home()
    projects_root = home / "projects"
    if not projects_root.is_dir():
        print(json.dumps({"orphans": [], "note": "no projects dir yet"}))
        return 0

    orphans = []
    for slug_dir in sorted(projects_root.iterdir()):
        if not slug_dir.is_dir():
            continue
        if slug_dir.name.endswith(".bak") or ".bak-" in slug_dir.name:
            continue
        resolved_main = _resolve_main_slug_for_source(slug_dir)
        if resolved_main is None:
            orphans.append({
                "slug": slug_dir.name,
                "reason": "original checkout path missing; cannot git-resolve",
            })
        elif resolved_main != slug_dir.name:
            orphans.append({
                "slug": slug_dir.name,
                "resolved_main_slug": resolved_main,
                "reason": "git-resolved slug differs — this dir is a worktree remnant",
                "suggested_migrate_cmd": f"dynos worktree migrate {slug_dir}",
            })

    print(json.dumps({"orphans": orphans, "count": len(orphans)}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate orphaned worktree persistent state into main")
    sub = parser.add_subparsers(dest="command", required=True)

    m = sub.add_parser("migrate", help="Migrate a worktree-slug persistent dir into main")
    m.add_argument("source", help="Path to the worktree's persistent dir (e.g. ~/.dynos/projects/foo-bar)")
    m.add_argument("--execute", action="store_true",
                   help="Perform the migration. Default is dry-run (print plan only).")
    m.set_defaults(func=cmd_migrate)

    l = sub.add_parser("list-orphans", help="List persistent-dir slugs that don't match their git-resolved slug")
    l.set_defaults(func=cmd_list_orphans)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
