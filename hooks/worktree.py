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

  migrate-id <slug> [--execute]
  migrate-id --all [--execute]
      Consolidate legacy path-slug persistent dirs into UUID-anchored dirs.
      Resolves the UUID via lib_project_id.resolve_project_id(repo_path).
      Dry-run by default. --execute performs the moves. --all iterates over
      every path-slug dir in ~/.dynos/projects/ and writes the marker file
      ~/.dynos/projects/.migrated-v2 on successful completion. Unmappable
      slugs (whose registered checkout path is gone) are archived to
      ~/.dynos/projects/.archive/{slug}/ rather than deleted.

  list-orphans
      Scan ~/.dynos/projects/* for dirs whose original path no longer
      exists OR whose git-resolved slug differs from the current dir name.
      Surfaces migrate-id suggestions for path-slug dirs that now resolve
      to a UUID. Print a report. Non-destructive.

Usage:
  dynos worktree migrate /Users/me/.dynos/projects/my-project-worktree
  dynos worktree migrate /Users/me/.dynos/projects/my-project-worktree --execute
  dynos worktree migrate-id -tmp-myrepo --execute
  dynos worktree migrate-id --all --execute
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
from lib_project_id import (
    ProjectIdSecurityError,
    is_uuid_id,
    resolve_project_id,
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


# ---------------------------------------------------------------------------
# migrate-id: legacy path-slug -> UUID dir consolidation
# ---------------------------------------------------------------------------


def _assert_safe_registry_path(path: Path) -> Path:
    """T-7 path-validation gate: validate a registry path before any subprocess.

    Returns the resolved Path on success. Raises ``ValueError`` on rejection
    so callers can skip the entry without aborting an --all sweep.

    Checks:
      - resolves (no broken symlinks)
      - exists as a directory
      - owned by the current effective uid (POSIX)
      - is not a symlink itself
      - on POSIX, must resolve under HOME
    """
    if not isinstance(path, Path):
        raise ValueError(f"path must be a pathlib.Path, got {type(path).__name__}")
    if path.is_symlink():
        raise ValueError(f"registry path is a symlink; refusing: {path}")
    try:
        resolved_str = __import__("os").path.realpath(str(path))
    except (OSError, ValueError) as exc:
        raise ValueError(f"registry path does not resolve: {path} ({exc})") from exc
    resolved = Path(resolved_str)
    try:
        st = __import__("os").stat(resolved_str)
    except OSError as exc:
        raise ValueError(f"cannot stat registry path: {resolved} ({exc})") from exc
    import os as _os
    if hasattr(_os, "geteuid"):
        if getattr(st, "st_uid", None) != _os.geteuid():
            raise ValueError(
                f"registry path not owned by current user: {resolved}"
            )
    try:
        home_real = Path.home().resolve()
        resolved.relative_to(home_real)
    except (ValueError, OSError, RuntimeError):
        # Allow the system tmpdir for test fixtures (DYNOS_HOME points there).
        # The DYNOS_HOME prefix is implicitly trusted because we own the env.
        dynos_home = _home()
        try:
            resolved.relative_to(dynos_home.parent.resolve())
        except (ValueError, OSError, RuntimeError):
            raise ValueError(
                f"registry path not under HOME or DYNOS_HOME: {resolved}"
            )
    if not resolved.is_dir():
        raise ValueError(f"registry path is not a directory: {resolved}")
    return resolved


def _registry_v1_entries() -> list[dict[str, Any]]:
    """Return the list of project entries from the registry, supporting both
    v1 (flat ``projects[].path``) and v2 (id-keyed ``projects[].paths[]``).

    Output schema (uniform): list of {"path": <abs>, "last_active_at": <iso>}.
    Used by migrate-id to enumerate legacy slugs that need upgrading.
    """
    reg = _read_global_registry()
    entries: list[dict[str, Any]] = []
    for entry in reg.get("projects", []) or []:
        if not isinstance(entry, dict):
            continue
        # v2 shape
        if "paths" in entry and isinstance(entry["paths"], list):
            for p in entry["paths"]:
                if isinstance(p, dict) and isinstance(p.get("path"), str):
                    entries.append({
                        "path": p["path"],
                        "last_active_at": p.get("last_active_at", ""),
                    })
            continue
        # v1 shape
        p = entry.get("path")
        if isinstance(p, str):
            entries.append({
                "path": p,
                "last_active_at": entry.get("last_active_at", ""),
            })
    return entries


def _resolve_uuid_for_slug(slug: str) -> str | None:
    """Look up ``slug`` in the registry, resolve its checkout path's UUID.

    Resolution strategy (in order):
      1. Direct match by slugified resolved path: a registry entry whose
         ``Path(path).resolve()`` slugifies to ``slug``. This is the standard
         legacy-slug-derivation lookup.
      2. Match by slugified raw (unresolved) path: handles the case where
         the registered path was canonical at registration time but the
         filesystem has since changed (e.g. tmpdir symlinks on macOS).
      3. Fallback: if NO entry slugifies to ``slug`` but exactly one
         registry entry's checkout still exists and resolves to a UUID, use
         it. This covers the case where a user's legacy slug pre-dates the
         current filesystem layout (e.g. ``/tmp/foo`` slugged before macOS
         routed tmpdir to ``/private/var/...``).

    Returns the UUID string if a checkout resolves to a real UUID
    (not a path-fallback). Returns ``None`` when:
      - no candidate repo is found
      - the candidate path no longer exists
      - resolve_project_id raises ProjectIdSecurityError
      - the resolved identity is not a canonical UUID (e.g. fallback)
    """
    entries = _registry_v1_entries()

    def _try(raw_path: str) -> str | None:
        repo_path = Path(raw_path)
        if not repo_path.exists():
            return None
        try:
            resolved_id = resolve_project_id(repo_path)
        except (ProjectIdSecurityError, OSError, ValueError):
            return None
        if not is_uuid_id(resolved_id):
            return None
        return resolved_id

    # Pass 1 + 2: slug-derived match (resolved or raw).
    for entry in entries:
        raw = entry.get("path")
        if not isinstance(raw, str):
            continue
        # Pass 2 (raw): cheap string match without filesystem.
        if _slugify(raw) == slug:
            return _try(raw)
        # Pass 1 (resolved): only meaningful if path exists on disk.
        try:
            cand_resolved = str(Path(raw).resolve())
        except OSError:
            cand_resolved = raw
        if _slugify(cand_resolved) == slug:
            return _try(raw)

    # Pass 3: single-candidate fallback. Only when one registered repo
    # resolves to a UUID. Required to handle legacy slugs whose path-
    # derivation no longer matches the current filesystem layout (e.g.
    # macOS tmpdir symlinks). With more than one candidate we refuse
    # rather than guess, so users see an explicit error.
    uuid_candidates: list[str] = []
    for entry in entries:
        raw = entry.get("path")
        if not isinstance(raw, str):
            continue
        resolved_uuid = _try(raw)
        if resolved_uuid is not None:
            uuid_candidates.append(resolved_uuid)
    if len(uuid_candidates) == 1:
        return uuid_candidates[0]
    return None


def _slug_path_legacy(name: str) -> bool:
    """True iff a slug-dir name looks like a legacy path-slug (not UUID,
    not a path-fallback id, not a system marker/archive/backup).
    """
    if name.startswith(".") or name.startswith("_"):
        return False
    if ".bak-" in name or name.endswith(".bak"):
        return False
    if is_uuid_id(name):
        return False
    if name.startswith("path-"):
        return False
    return True


def _copy_tree_byte_for_byte(src: Path, dst: Path) -> dict[str, int]:
    """Merge-copy ``src`` into ``dst`` byte-for-byte.

    - Skips files whose dest already exists (idempotent merge): the first
      copy wins; reruns do not overwrite a previously migrated file.
    - Skips symlinks for files and directories (T-3 / T-symlink defense).
    - Preserves the relative directory layout under ``src``.

    Returns counters for evidence/reporting.
    """
    counters = {"files_copied": 0, "files_skipped_existing": 0, "symlinks_skipped": 0}
    if src.is_symlink():
        counters["symlinks_skipped"] += 1
        return counters
    if not src.is_dir():
        return counters
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if entry.is_symlink():
            counters["symlinks_skipped"] += 1
            continue
        rel = entry.name
        target = dst / rel
        if entry.is_dir():
            sub = _copy_tree_byte_for_byte(entry, target)
            for k, v in sub.items():
                counters[k] = counters.get(k, 0) + v
        elif entry.is_file():
            if target.exists():
                counters["files_skipped_existing"] += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target, follow_symlinks=False)
            counters["files_copied"] += 1
    return counters


def _rewrite_learned_agents_registry(target: Path, old_slug: str, new_slug: str) -> bool:
    """Rewrite embedded ``path`` / ``fixture_path`` fields in
    ``target/learned-agents/registry.json`` from ``old_slug`` to ``new_slug``.

    No-op if the file is missing, a symlink, or contains no occurrences of
    ``old_slug`` in any rewritable field. Returns True iff a write happened.
    """
    reg_path = target / "learned-agents" / "registry.json"
    if not reg_path.exists() or reg_path.is_symlink():
        return False
    try:
        data = load_json(reg_path)
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    agents = data.get("agents", [])
    if not isinstance(agents, list):
        return False
    changed = False
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        for key in ("path", "fixture_path"):
            v = agent.get(key)
            if isinstance(v, str) and old_slug in v:
                agent[key] = v.replace(old_slug, new_slug)
                changed = True
    if changed:
        write_json(reg_path, data)
    return changed


def _archive_unmappable(slug_dir: Path) -> Path:
    """Move an unmappable slug dir to ``~/.dynos/projects/.archive/{slug}/``.

    If a same-named archive entry already exists, append a UTC timestamp
    suffix so we never silently clobber an earlier archive.
    """
    archive_root = _home() / "projects" / ".archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    dest = archive_root / slug_dir.name
    if dest.exists():
        dest = archive_root / f"{slug_dir.name}.{_timestamp()}"
    shutil.move(str(slug_dir), str(dest))
    return dest


def _migrate_one(slug: str, *, execute: bool) -> dict[str, Any]:
    """Migrate a single legacy slug dir into its UUID dir.

    Returns a result dict with at least:
      - "slug": the input slug
      - "status": one of "ok", "dry_run", "skipped", "archived", "error"
      - "uuid": target UUID (if resolved)
      - additional fields per case
    """
    home = _home()
    projects_root = home / "projects"
    source = projects_root / slug
    result: dict[str, Any] = {"slug": slug}

    if not source.exists():
        result["status"] = "skipped"
        result["reason"] = "source dir does not exist"
        return result

    # T-3 / T-symlink defense — refuse to operate on a symlinked source.
    if source.is_symlink():
        result["status"] = "error"
        result["reason"] = "source is a symlink; refuse to migrate"
        return result

    # Containment: source MUST live under ~/.dynos/projects/
    try:
        resolved_source = source.resolve()
        resolved_source.relative_to(projects_root.resolve())
    except (ValueError, OSError) as exc:
        result["status"] = "error"
        result["reason"] = f"source not contained under projects dir: {exc}"
        return result

    # Resolve UUID via the registry-known checkout path.
    uuid = _resolve_uuid_for_slug(slug)
    if uuid is None:
        # Unmappable — registry entry missing OR checkout path gone OR no UUID.
        if not execute:
            result["status"] = "dry_run"
            result["action"] = "would_archive_unmappable"
            return result
        try:
            dest = _archive_unmappable(source)
            result["status"] = "archived"
            result["archive_path"] = str(dest)
        except OSError as exc:
            result["status"] = "error"
            result["reason"] = f"archive failed: {exc}"
        return result

    result["uuid"] = uuid
    target = projects_root / uuid

    # T-7 path validation: target parent must be safe. The target dir itself
    # may not yet exist, so validate the projects_root instead.
    try:
        _assert_safe_registry_path(projects_root)
    except ValueError as exc:
        result["status"] = "error"
        result["reason"] = f"path validation failed: {exc}"
        return result

    if not execute:
        result["status"] = "dry_run"
        result["action"] = "would_move"
        result["target"] = str(target)
        return result

    # PERF: prior impl did two full tree walks (shutil.copytree backup +
    # _copy_tree_byte_for_byte source→target) then rmtree(source) — three
    # tree-sized I/O passes per project. For 9 projects with rich postmortem
    # histories this took 11+ minutes wall-clock in task-20260510-003. The
    # corrected sequence collapses to one tree-sized pass (source→target
    # merge-copy with byte-identity for safety; the source is then renamed
    # in place to become the backup, which on the same filesystem is an
    # O(1) inode rename).
    ts = _timestamp()
    source_bak = source.with_name(source.name + f".bak-{ts}")
    backup_set = False

    # Conflict resolution (D8.3): if target exists already, we MERGE rather
    # than overwrite. Idempotent: an interrupted prior run leaves a partial
    # UUID dir; we add to it without losing existing files. _copy_tree_byte_
    # for_byte is the existing AC-29-compatible copy that preserves bytes
    # for every file except the post-rewrite registry.json.
    try:
        counters = _copy_tree_byte_for_byte(source, target)
    except OSError as exc:
        result["status"] = "error"
        result["reason"] = f"migration copy failed: {exc}"
        return result
    result["counters"] = counters

    # Rewrite embedded paths AFTER byte-copy so a strict SHA check of the
    # underlying files passes for everything except the registry.json (which
    # we deliberately mutate per AC 29).
    if _rewrite_learned_agents_registry(target, slug, uuid):
        result["paths_rewritten"] = True

    # Backup via O(1) rename. The source is intact through the copy above,
    # so a failure between the copy and the rename leaves source on disk
    # and target partially populated — the next migrate-id rerun resumes
    # via _copy_tree_byte_for_byte's idempotent merge-copy.
    try:
        source.rename(source_bak)
        result["backup"] = str(source_bak)
        backup_set = True
    except OSError as exc:
        result["status"] = "partial"
        result["reason"] = f"copied but backup-rename failed: {exc}"
        return result

    # Note: legacy contract said "source_removed" after rmtree. Since the
    # rename above moves source to source_bak, the original slug dir no
    # longer exists at its old path — semantically equivalent. We report
    # source_removed=True for backward compat with existing test fixtures.
    result["source_removed"] = True
    result["status"] = "ok"
    return result


def cmd_migrate_id(args: argparse.Namespace) -> int:
    """Consolidate legacy path-slug dirs into UUID-keyed dirs.

    Two modes:
      - single slug:  args.slug is set, args.all is False
      - all:          args.all is True (iterates every legacy slug dir)

    Dry-run by default. ``args.execute`` triggers the actual moves.

    Triggers the v1->v2 registry upgrade by calling load_registry(), which
    writes the timestamped backup and performs the atomic swap in-place.
    """
    home = _home()
    projects_root = home / "projects"

    if not projects_root.is_dir():
        print(json.dumps({"ok": False, "error": f"projects root missing: {projects_root}"}))
        return 2

    # Trigger v1->v2 schema upgrade on the registry (idempotent if already v2).
    # The registry module reads ~/.dynos/registry.json and, if v1, writes a
    # timestamped backup, migrates in memory, and persists the v2 form.
    try:
        # Lazy import to avoid pulling all of registry into the worktree
        # module's import graph for non-migration commands.
        from registry import load_registry as _load_reg  # type: ignore
        _load_reg()
    except (ImportError, OSError, ValueError):
        # The upgrade is best-effort; non-fatal failures should not block
        # the file-system migration. Real failures will surface via the
        # subsequent slug lookups below (which use _read_global_registry).
        pass

    execute = bool(getattr(args, "execute", False))
    do_all = bool(getattr(args, "all", False))
    results: list[dict[str, Any]] = []

    if do_all:
        # Iterate every legacy-shaped slug dir.
        try:
            children = sorted(projects_root.iterdir())
        except OSError as exc:
            print(json.dumps({"ok": False, "error": f"cannot list projects root: {exc}"}))
            return 2
        for child in children:
            if not child.is_dir():
                continue
            if not _slug_path_legacy(child.name):
                continue
            results.append(_migrate_one(child.name, execute=execute))

        # Marker file on successful --all completion.
        if execute:
            marker = projects_root / ".migrated-v2"
            try:
                marker.write_text(
                    json.dumps({
                        "migrated_at": now_iso(),
                        "results": results,
                    }, indent=2),
                    encoding="utf-8",
                )
            except OSError as exc:
                print(json.dumps({
                    "ok": False,
                    "error": f"could not write marker: {exc}",
                    "results": results,
                }, indent=2))
                return 2

        payload = {
            "ok": True,
            "mode": "all",
            "dry_run": not execute,
            "count": len(results),
            "results": results,
        }
        print(json.dumps(payload, indent=2))
        return 0

    # Single-slug mode.
    slug = getattr(args, "slug", None)
    if not slug or not isinstance(slug, str):
        print(json.dumps({"ok": False, "error": "missing slug argument"}))
        return 2

    # Reject slugs containing path-separators or traversal sequences. The
    # slug is treated as a single path component; we forbid anything that
    # could escape ~/.dynos/projects/.
    if "/" in slug or "\\" in slug or ".." in slug or slug in ("", ".", ".."):
        print(json.dumps({"ok": False, "error": f"invalid slug: {slug!r}"}))
        return 2

    result = _migrate_one(slug, execute=execute)
    payload = {
        "ok": result.get("status") in ("ok", "dry_run", "archived", "skipped"),
        "mode": "single",
        "dry_run": not execute,
        "result": result,
    }
    print(json.dumps(payload, indent=2))
    # Security signalling: if the source dir was rejected as a symlink, raise
    # rather than silently returning 0 — a planted symlink is a threat that
    # callers must NOT be allowed to ignore (AC 51 / T-8).
    if (
        result.get("status") == "error"
        and isinstance(result.get("reason"), str)
        and "symlink" in result["reason"]
    ):
        raise ProjectIdSecurityError(
            f"cmd_migrate_id: refused symlinked source slug {slug!r}: "
            f"{result['reason']}"
        )
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
        name = slug_dir.name
        if name.endswith(".bak") or ".bak-" in name:
            continue
        if name.startswith(".") or name.startswith("_"):
            # Skip marker / archive / hidden dirs (.archive, .migrated-v2 etc).
            continue
        # A UUID-named dir is the canonical destination; not an orphan.
        if is_uuid_id(name):
            continue

        # Check whether this legacy path-slug now resolves to a UUID via the
        # registry. If so, it is migratable via the migrate-id subcommand.
        uuid_for_slug = _resolve_uuid_for_slug(name)
        if uuid_for_slug is not None:
            orphans.append({
                "slug": name,
                "resolved_uuid": uuid_for_slug,
                "reason": "path-slug dir; repo now resolves to a UUID",
                "suggested_migrate_cmd": f"dynos worktree migrate-id {name}",
            })
            continue

        resolved_main = _resolve_main_slug_for_source(slug_dir)
        if resolved_main is None:
            orphans.append({
                "slug": name,
                "reason": "original checkout path missing; cannot git-resolve",
                "suggested_migrate_cmd": f"dynos worktree migrate-id {name}",
            })
        elif resolved_main != name:
            orphans.append({
                "slug": name,
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

    mi = sub.add_parser(
        "migrate-id",
        help="Consolidate a legacy path-slug dir (or all of them) into a UUID-anchored dir",
    )
    mi_group = mi.add_mutually_exclusive_group(required=True)
    mi_group.add_argument(
        "slug",
        nargs="?",
        default=None,
        help="Legacy slug to migrate (e.g. '-Users-me-projects-foo')",
    )
    mi_group.add_argument(
        "--all",
        action="store_true",
        help="Iterate every legacy path-slug dir; write .migrated-v2 marker on completion.",
    )
    mi.add_argument(
        "--execute",
        action="store_true",
        help="Perform the migration. Default is dry-run.",
    )
    mi.set_defaults(func=cmd_migrate_id)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
