# Project Identity — Design Doc

**Status:** IMPLEMENTED
**Owner:** to be assigned
**Context branch:** none yet (this doc lives on main as a proposal)

---

## 1. Problem (historical — now resolved)

Before this design was implemented, a project was identified by a **path-derived slug**:

```
slug = git_toplevel.strip("/").replace("/", "-")    # old scheme, removed
# → "-Users-hassam-Documents-dynos-work"
```

`_persistent_project_dir(root)` returns `~/.dynos/projects/{slug}/`, and 60+ callers depend on this for postmortems, prevention rules, learned-agent registry, benchmark history, model/skip/route policy JSON, effectiveness scores, project_rules.md, and per-project policy.json.

The 2026-04-XX worktree fix made the slug derive from the **main worktree's** toplevel (via `git rev-parse --git-common-dir`), so multiple worktrees of one clone now share a slug. But the slug was still path-based, which broke identity in three ways:

1. **Path moves.** Renaming `~/code/foo` to `~/projects/foo` produces a different slug. Learned state orphans on disk.
2. **Cross-clone identity.** Two clones of the same OSS repo at `~/work/x` and `~/scratch/x` get two slugs. They could legitimately be two contexts (good) — but the system can't *tell* whether they should be unified.
3. **Cloud sync (planned).** Path-based slugs can't be globally unique. Two operators sync to the same bucket → collisions or silent merge.

This doc fixes the identity problem in a way that's stable across worktrees, machines, and clones, and is forward-compatible with cloud sync.

---

## 2. Goals & non-goals

### Goals

- G1. **Stable across worktrees of the same clone** (already true today; preserve).
- G2. **Stable across path moves** of the same clone (rename `~/code/foo` → `~/work/foo` does not orphan state).
- G3. **Globally unique without a central authority** — a fresh clone on machine A and a fresh clone on machine B produce different IDs by default.
- G4. **Cloud-sync-safe** — two operators of two forks of the same OSS repo never collide on the same ID.
- G5. **OSS-safe** — a public fork of `dynos-work` does not inherit the upstream maintainer's ID.
- G6. **Decentralized** — no server, no allocation step, no network calls in the hot path.
- G7. **Single seam** — every existing caller continues to work without modification. Only `_persistent_project_dir` and `registry.py` change internally.
- G8. **Migratable** — existing `~/.dynos/projects/-Users-...` state can be transparently moved to the new ID without manual intervention.

### Non-goals

- **NG1.** Cross-machine unification (same operator, two laptops). Solved later by an explicit `dynos link <id>` flow or by the cloud-sync handshake. Not in this doc.
- **NG2.** Identity for non-git directories. Currently we operate fine on those via the path fallback; we keep the fallback but never claim it's unique.
- **NG3.** Replacing the `~/.claude/projects/{slug}/` Claude Code mirror's slug *scheme* (it stays path-derived, keyed to Claude Code's directory hashing convention). However, §5.4 *does* harden it by routing through the same `sanitize_path_for_slug` helper as the dynos fallback (closes T-18). The function name and signature are unchanged.
- **NG4.** Submodule-specific identity. Outer repo's ID applies to the whole tree by default.

---

## 3. Implemented seam (what's now wired)

```
_persistent_project_dir(root)            ← lib_core.py:306
   │
   └─ slug = resolve_project_id(root)    ← hooks/lib_project_id.py (UUID4 or path-fallback)
      └─ return dynos_home / "projects" / slug
```

The slug derivation was changed to a single call to `resolve_project_id` from `hooks/lib_project_id.py` (wired at `lib_core.py` line 326: `slug = resolve_project_id(root)`). Every downstream caller is unaffected — the path shape and file format are unchanged.

The old path-derived slug derivation (`base.strip("/").replace("/", "-")`) no longer exists in the primary code path.

The worktree migration tool `hooks/worktree.py` already exists and consolidates orphaned worktree-slug dirs into the main slug. We extend it rather than replace it.

---

## 4. Design

### 4.1 Identity scheme

```
Project ID = UUID4 stored in <git-common-dir>/dynos-project-id
```

- **Location:** the file lives in the *git common dir* — i.e., the shared `.git/` of the main checkout. For a regular checkout this is `<root>/.git/dynos-project-id`. For a worktree it's `<main>/.git/dynos-project-id` (worktrees share the common dir by definition).
- **Format:** the file contains exactly one line — a UUID4 in canonical form (`xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx`), 36 chars, no whitespace, no comments, no trailing newline mandated (we tolerate one trailing newline on read).
- **Generation:** on first lookup, if the file is absent, generate a fresh UUID4 with `uuid.uuid4()` and write atomically (see §5.2).
- **Lifetime:** persists for the life of the clone. Survives history rewrites, branch deletions, and rebases. Lost only if `.git/` is deleted (i.e., the clone is destroyed).
- **Visibility:** never enters the working tree, never gets committed, never propagates by `git push`/`git pull`. A fresh `git clone` from origin starts with no `dynos-project-id` and generates its own on first dynos invocation.

### 4.2 Why this satisfies the goals

| Goal | How |
|---|---|
| G1 worktree-stable | All worktrees resolve to the same `<git-common-dir>` |
| G2 path-move-stable | ID is in `.git/`; renaming the working dir doesn't move it |
| G3 globally unique | UUID4 collision probability is negligible |
| G4 cloud-sync-safe | Each clone has its own UUID; no path-derived ambiguity |
| G5 OSS-safe | Fresh clones generate fresh UUIDs; upstream's ID never propagates |
| G6 decentralized | No server, no network |
| G7 single seam | Only `_persistent_project_dir` changes internally |
| G8 migratable | Old slugs map 1:1 to clones; one-pass migrator covers them |

### 4.3 Fallback for non-git roots

If `git rev-parse --git-common-dir` fails (not a git repo, no git installed, bare repo), fall back to the existing path-derived slug **prefixed with `path-`** to make it visually distinct from UUID dirs:

```
slug = "path-" + sanitize(abspath)
```

Where `sanitize` is **strict**, not "best effort":

- Result must match `^path-[a-zA-Z0-9._-]{1,200}$`.
- Reject any input whose `abspath` contains: control chars (`\x00-\x1f\x7f`), embedded newlines, the literal substrings `..`, `\`, or any non-ASCII char.
- `/` is replaced with `-` only after the above checks pass.
- A path that fails sanitization causes `resolve_project_id` to refuse-and-raise (not silently return a junk slug). The caller treats this as "no identity available" and `dynos status` surfaces the problem.

This is explicitly *not* unique across machines and is documented as such. We emit a warning event (`identity_fell_back_to_path`) the first time a project hits this branch. See §12 for the path-traversal threat this defends against.

### 4.4 Cross-machine unification (out of scope here, but designed-for)

When the cloud-sync feature lands, the sync handshake is the natural place to unify two `dynos-project-id` files representing the same logical project:

1. Operator runs `dynos sync enable` on machine A.
2. Server returns either a fresh canonical ID (first-time) or an existing ID (linked).
3. Local `<git-common-dir>/dynos-project-id` is overwritten with the canonical value.
4. Local `~/.dynos/projects/{old-uuid}/` is renamed to `~/.dynos/projects/{canonical-id}/`, with conflict-merge if the dir already exists.
5. Operator runs `dynos sync link <canonical-id>` on machine B; same overwrite + rename.

This doc does not implement step 1–5; it just guarantees the file format and location are sync-compatible.

---

## 5. Component map

### 5.1 New module: `hooks/lib_project_id.py`

Single-purpose module so the seam is testable and easy to mock. The module exposes:

```python
def resolve_project_id(root: Path) -> str:
    """Return the project identity slug for `root`.

    Priority order (deterministic):
      1. <git-common-dir>/dynos-project-id  → return as-is (UUID4 contents)
      2. <git-common-dir> resolves but file absent → generate UUID4, write atomically, return
      3. <git-common-dir> unavailable → return "path-{path-slug}" (fallback,
         emits identity_fell_back_to_path event once per process)

    NEVER raises. The fallback path is the safety net; any caller that
    expects globally-unique IDs must check the prefix.
    """

def is_uuid_id(slug: str) -> bool:
    """True iff `slug` matches the UUID4 format we write."""

def is_path_fallback_id(slug: str) -> bool:
    """True iff `slug` starts with the `path-` fallback prefix."""

def _git_common_dir(root: Path) -> Path | None:
    """Run `git rev-parse --git-common-dir` and return its absolute path.
    Returns None if git is unavailable or the call fails."""

def _read_or_generate_id(id_file: Path) -> str:
    """Atomic read-or-create. Acquires LOCK_EX on the parent dir, reads
    the file if present, else generates UUID4, writes via tmp+rename."""
```

### 5.2 Atomic create-or-read (security-hardened)

Two worktrees invoking `dynos` concurrently must not race on `.git/dynos-project-id`. The implementation must also defeat symlink-redirect attacks, env-var injection, and content-injection attacks (see §12 for the threat model).

```python
def _read_or_generate_id(common_dir: Path) -> str:
    # T-1 (§12): the common_dir itself must be a real directory we own,
    # not a symlink pointing outside HOME. Refuse if either fails.
    _assert_safe_common_dir(common_dir)

    # T-3: never follow symlinks when opening the dir. O_NOFOLLOW on the
    # final component; pre-check on intermediate components is implicit
    # because realpath was used in _assert_safe_common_dir.
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(common_dir), flags)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        id_file = common_dir / "dynos-project-id"

        # T-3: reject the file if it is a symlink (planted to redirect
        # the read to attacker-controlled content).
        if id_file.is_symlink():
            raise ProjectIdSecurityError(
                f"refusing to read {id_file}: file is a symlink"
            )

        if id_file.exists():
            content = id_file.read_text(encoding="utf-8", errors="strict")
            # T-4: strict format validation. Trailing newline is the
            # only whitespace tolerated. Anything else (path traversal
            # characters, control chars, multi-line content) is rejected.
            value = content.rstrip("\n")
            if not _UUID4_RE.match(value):
                raise ProjectIdSecurityError(
                    f"corrupted or hostile dynos-project-id at {id_file}; "
                    f"expected UUID4, got {value!r:.80}"
                )
            return value.lower()  # T-13: case-normalize for case-insensitive FS

        # File absent — generate fresh UUID4.
        new_id = str(uuid.uuid4())  # canonical lowercase form

        # Atomic write: tmp file in the SAME directory (so os.rename is
        # atomic — never crosses filesystems), strict perms, no symlink
        # follow.
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix="dynos-project-id.",
            suffix=".tmp",
            dir=str(common_dir),
        )
        try:
            os.write(tmp_fd, (new_id + "\n").encode("utf-8"))
            os.fchmod(tmp_fd, 0o600)  # owner-only read/write
            os.close(tmp_fd)
            tmp_fd = -1
            os.rename(tmp_path, str(id_file))
        finally:
            if tmp_fd != -1:
                os.close(tmp_fd)
            # If rename succeeded the tmp path no longer exists.
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass

        return new_id
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _assert_safe_common_dir(common_dir: Path) -> None:
    """Refuse to operate on a common dir that is:
      - not a directory
      - a symlink whose target escapes HOME
      - not owned by the current user (uid mismatch on POSIX)
    """
    if not common_dir.is_dir():
        raise ProjectIdSecurityError(f"not a directory: {common_dir}")
    real = common_dir.resolve(strict=True)
    home_real = Path.home().resolve(strict=True)
    try:
        real.relative_to(home_real)
    except ValueError:
        # T-1: outside HOME — refuse. Catches a planted .git symlink
        # pointing at /tmp or /etc.
        raise ProjectIdSecurityError(
            f"common_dir {real} is not under HOME {home_real}; refusing"
        )
    # T-2: ownership check (POSIX only). On Windows this is a no-op.
    if hasattr(os, "geteuid"):
        st = os.stat(str(real))
        if st.st_uid != os.geteuid():
            raise ProjectIdSecurityError(
                f"common_dir {real} is not owned by the current user; refusing"
            )
```

The lock is on the *common dir*, not the file, so two processes serialize before either of them sees the file's existence state.

The function also runs git with a **scrubbed environment** — see §5.7.

### 5.3 Modified: `hooks/lib_core.py`

```diff
 def _persistent_project_dir(root: Path) -> Path:
     dynos_home = Path(os.environ.get("DYNOS_HOME", str(Path.home() / ".dynos")))
-    resolved_str = str(root.resolve())
-    canonical = _resolve_git_toplevel(resolved_str)
-    base = canonical if canonical is not None else resolved_str
-    slug = base.strip("/").replace("/", "-")
+    from lib_project_id import resolve_project_id   # local import to avoid cycle
+    slug = resolve_project_id(root)
     return dynos_home / "projects" / slug
```

Behavior on existing call sites:

| Caller (file:count) | Input | Old output | New output | Wire status |
|---|---|---|---|---|
| `lib_core.py` (20 internal calls) | `root` | `~/.dynos/projects/-Users-…/` | `~/.dynos/projects/{uuid}/` | unchanged signature; tests pass via fixtures |
| `ctl.py` (11) | `task_dir.parent.parent` (root) | same shape | same shape | unchanged |
| `router.py` (8) | `self.root` | same shape | same shape | unchanged |
| `policy_engine.py` (6) | `root` | same shape | same shape | unchanged — but see §5.4 for the `~/.claude/` mirror |
| `postmortem_improve.py` (6) | `root` | same shape | same shape | unchanged |
| `postmortem_analysis.py` (5) | `root` | same shape | same shape | unchanged |
| `daemon.py` (4) | `root` | same shape | same shape | unchanged |
| `hooks_path_helper.py` (4) | `root` | same shape | same shape | unchanged |
| `eventbus.py` (3) | `root` | same shape | same shape | unchanged |
| `lib_log.py` (3) | `root` | same shape | same shape | unchanged |
| `agent_generator.py` (3) | `root` | same shape | same shape | unchanged |
| `rules_engine.py` (3) | `root` | same shape | same shape | unchanged |
| `lib_templates.py` (2) | `root` | same shape | same shape | unchanged |
| `lib_qlearn.py` (2) | `root` | same shape | same shape | unchanged |
| `worktree.py` (2) | `root` | same shape | same shape | **also rewires** — see §5.5 |
| `check_deferred_findings.py` (6) | `root` | same shape | same shape | unchanged (count-only — no JSON parse) |
| `planner.py` (1) | `root` | same shape | same shape | unchanged |
| `lib.py` (1) | re-export | re-export | re-export | unchanged |
| `postmortem.py` (1) | `root` | same shape | same shape | unchanged |

### 5.4 Hardened (slug stays path-derived, sanitization added): `memory/policy_engine.project_slug()`

```python
# memory/policy_engine.py:55-56 (current)
def project_slug(root: Path) -> str:
    return str(root.resolve()).replace("/", "-")
```

This is the slug for the **Claude Code memory mirror** at `~/.claude/projects/{slug}/memory/project_rules.md`. It is *not* the dynos identity — it's keyed to Claude Code's directory-hashing convention, which is path-based by design. We do **not** rename the function or change its signature. We **do** route it through the same `sanitize_path_for_slug` helper that §4.3 uses, so the mirror cannot be tricked by a control-char-bearing or `..`-bearing abspath into writing outside `~/.claude/projects/`:

```python
# new shape
from lib_project_id import sanitize_path_for_slug

def project_slug(root: Path) -> str:
    return sanitize_path_for_slug(str(root.resolve()))
```

`sanitize_path_for_slug` lives in `lib_project_id` (so it's the single source of truth for both fallbacks), enforces the same strict regex as §4.3, and raises `ProjectIdSecurityError` on inputs that fail validation. Callers of `project_slug` already operate in a "build a path under `~/.claude/projects/`" context, so a raise is preferable to a junk slug that could land in an unexpected location.

This closes T-18 (Claude Code mirror path injection) in the same release as the rest of this work. No working-tree or behavioral change for any existing well-formed path.

### 5.5 Modified: `hooks/worktree.py`

The existing `migrate` and `list-orphans` subcommands operate on the assumption that all dirs under `~/.dynos/projects/` are slugs. After §5.3, dirs are either UUIDs (new) or path-slugs (old/fallback). We extend:

- `list-orphans` learns the rule: a path-slug dir whose corresponding repo would *now* resolve to a UUID is an orphan, suggest `dynos migrate-id <slug>`.
- New subcommand: `dynos worktree migrate-id <slug>` — looks up `<slug>` in the registry, finds the path, runs `resolve_project_id(path)` to get the new UUID, calls the existing `_execute_migration` with target = `~/.dynos/projects/{uuid}/`.

The existing `migrate` subcommand stays as-is for the tail of pre-fix worktree-orphan migrations (slug → main-slug).

### 5.6 Scrubbed git subprocess

`_git_common_dir` invokes `git rev-parse --git-common-dir`. Git's behavior is influenced by env vars (`GIT_DIR`, `GIT_COMMON_DIR`, `GIT_WORK_TREE`, `GIT_CEILING_DIRECTORIES`, `GIT_CONFIG_*`), some of which let an attacker who controls env redirect git's notion of the repo. We scrub:

```python
_GIT_ENV_BLOCKLIST = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    # GIT_CONFIG_* (env-mediated config injection)
)

def _safe_git_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key in _GIT_ENV_BLOCKLIST or key.startswith("GIT_CONFIG_"):
            del env[key]
    return env

# subprocess invocation:
subprocess.run(
    [_GIT or "git", "-C", root_str, "rev-parse", "--git-common-dir"],
    capture_output=True, text=True, timeout=5, check=False,
    env=_safe_git_env(),                  # T-2: scrub injection vectors
    cwd=str(root_str),                    # explicit cwd; never inherit
)
```

We also **post-validate** the git output against expectations:

- The returned path must resolve to a directory.
- The directory must be named `.git` (T-5: defeats `name == ".git"` spoofing where attacker creates `/tmp/foo/.git/`-shaped paths and feeds them to us).
- `.git/`'s parent must contain `<root>` (i.e., the resolved common dir's parent must be a prefix of the input root). If not, treat as fallback.
- Refuse if the resolved path is a symlink whose target escapes HOME (already enforced by `_assert_safe_common_dir`).

Existing `_resolve_git_toplevel` in `hooks/lib_core.py:203-251` is updated in place to add the scrubbed env and post-validation.

### 5.7 Modified: `hooks/registry.py`

Today the registry keys projects by **path** under `~/.dynos/registry.json`:

```json
{
  "projects": [
    {"path": "/Users/h/work/foo", "registered_at": "...", "last_active_at": "...", "status": "active"}
  ]
}
```

New schema keys by **id**, with paths as a list of observed paths (one machine, possibly multiple worktree paths):

```json
{
  "projects": [
    {
      "id": "1f4a3d2e-b8c9-4f5a-9b1c-7e2f8d9a0b1c",
      "paths": [
        {"path": "/Users/h/work/foo", "registered_at": "...", "last_active_at": "..."},
        {"path": "/Users/h/work/foo-feature-branch", "registered_at": "...", "last_active_at": "..."}
      ],
      "status": "active"
    }
  ],
  "schema_version": 2
}
```

Lookup functions:

| Function | Old | New |
|---|---|---|
| `_find_project_entry(reg, root)` | match `entry["path"] == str(root.resolve())` | resolve `root` → uuid; match `entry["id"] == uuid` |
| `register_project(root)` | append entry | resolve uuid; append-or-merge into the entry's `paths[]` |
| `unregister_project(root)` | remove entry | resolve uuid; remove path from `paths[]`; remove entry if `paths` empty |
| `list_projects()` | return entries verbatim | flatten to one row per (id, path) for display |

**Trust boundary on registry-driven operations.** The migration tool iterates registry entries and runs `git -C <path> ...`. If the registry is tampered with (T-7), the path could point at an attacker-controlled directory whose `.git/config` injects shell commands via `core.fsmonitor`, `core.editor`, hooks, etc. Mitigation:

- Before any subprocess runs against a registry path, validate:
  - Path resolves (no broken symlinks).
  - Path is a directory.
  - Path is owned by the current user (`stat.st_uid == os.geteuid()`).
  - Path resolves under HOME or a configured allowlist of dev-root prefixes.
  - Path is not a symlink (or its target satisfies the above).
- Validation failure → skip the entry, emit `registry_path_rejected` event, surface in `dynos worktree list-orphans` output as "rejected: <reason>".
- The git subprocess uses the scrubbed env from §5.6.

This is the same pattern `worktree.py:_assert_source_contained` already uses for migration source paths; we extend it to registry-driven iteration.

---

## 6. Wiring map (the explicit edges)

This section enumerates **every data-flow edge** the change introduces or modifies. Each edge has a write-side, a read-side, and a wire-test that proves the connection.

### 6.1 New edges

| # | Write side | Read side | Format | Wire-test |
|---|---|---|---|---|
| W1 | `_read_or_generate_id` writes `<common-dir>/dynos-project-id` | `resolve_project_id` reads it | UUID4 string, single line | `test_resolve_project_id_persists_after_first_call` |
| W2 | `_persistent_project_dir` calls `resolve_project_id` | every existing caller of `_persistent_project_dir` | Path with UUID slug component | `test_persistent_project_dir_uses_uuid_when_git_repo` |
| W3 | `worktree.cmd_migrate_id` writes `~/.dynos/projects/{uuid}/...` | every caller of `_persistent_project_dir` reading those files | unchanged file shapes | `test_migrate_id_consolidates_old_slug_into_uuid` |
| W4 | `register_project` writes new schema (`id` + `paths[]`) | `list_projects`, `_find_project_entry` | schema_version=2 JSON | `test_registry_round_trip_v2` |
| W5 | first-time UUID generation emits `identity_uuid_generated` event | telemetry / dashboard | event line | `test_uuid_generation_emits_event` |
| W6 | path-fallback emits `identity_fell_back_to_path` event | telemetry / dashboard | event line | `test_path_fallback_emits_event_once_per_process` |

### 6.2 Modified edges (must continue to work after the change)

| # | Caller | Function consumed | Reason it still works |
|---|---|---|---|
| M1 | `policy_engine.write_patterns` → `~/.dynos/projects/{slug}/project_rules.md` | `_persistent_project_dir` | Slug derivation changes; path shape, file format unchanged |
| M2 | `agent_generator.init_registry` → `learned-agents/registry.json` | `_persistent_project_dir` | Same |
| M3 | `lib_qlearn` → policy state | `_persistent_project_dir` | Same |
| M4 | `bench.py` → `benchmarks/history.json` | `_persistent_project_dir` | Same |
| M5 | `fixture.py` → `benchmarks/index.json` | `_persistent_project_dir` | Same |
| M6 | `daemon.py` → daemon state files | `_persistent_project_dir` | Same |
| M7 | `lib_log` → events log path | `_persistent_project_dir` | Same |
| M8 | `lib_templates.py` → fix-templates.json | `_persistent_project_dir` | Same |
| M9 | `rules_engine.py` → prevention-rules.json | `_persistent_project_dir` | Same |
| M10 | `check_deferred_findings.py` → retrospective-count baseline | `_persistent_project_dir` | Same — no JSON parse, count-only |
| M11 | `eventbus.py` → cross-handler dispatch | `_persistent_project_dir` | Same |
| M12 | `hooks_path_helper.py` → path resolution | `_persistent_project_dir` | Same |
| M13 | `router.py` → routing decisions | `_persistent_project_dir` | Same |
| M14 | every test file using `_persistent_project_dir` | direct call | Tests use `monkeypatch.setattr(lib_core, "_persistent_project_dir", ...)` or `DYNOS_HOME` env var; both continue to work |

### 6.3 Anti-wires (paths the new code must NOT take)

| # | What | Why |
|---|---|---|
| A1 | Do not derive ID from `git config --get remote.origin.url` | Remote URL changes when org renames or fork moves |
| A2 | Do not derive ID from root commit SHA | All forks share root; cloud-sync collision (the user's specific concern) |
| A3 | Do not write `dynos-project-id` to the working tree | Would propagate via `git push` and pollute forks |
| A4 | Do not write to `<root>/.git/info/dynos-project-id` (per-worktree) | We want the *common* dir, not the per-worktree dir |
| A5 | Do not silently overwrite an existing `dynos-project-id` | Even on UUID mismatch — log and refuse instead, surfacing the conflict |

### 6.4 Migration edges (one-time)

| # | Write side | Read side | Wire-test |
|---|---|---|---|
| MIG1 | `cmd_migrate_id` mv `~/.dynos/projects/{old-slug}/*` → `~/.dynos/projects/{uuid}/*` | post-migration reads via `_persistent_project_dir` | `test_migrate_id_preserves_all_files` |
| MIG2 | `cmd_migrate_id` rewrites embedded paths in `learned-agents/registry.json` (e.g., `fixture_path`) from old slug to new UUID | benchmark/fixture lookups | `test_migrate_id_rewrites_embedded_paths` |
| MIG3 | `cmd_migrate_id` upgrades `~/.dynos/registry.json` from v1 → v2 schema | `register_project`, `list_projects` | `test_migrate_id_upgrades_registry_schema` |
| MIG4 | `cmd_migrate_id` archives non-mappable old slug dirs to `~/.dynos/projects/.archive/{old-slug}/` | none (manual recovery only) | `test_migrate_id_archives_unmappable_slugs` |

### 6.5 The single point of failure

The seam is **`resolve_project_id` returning the same value for every caller in the same project context**. Every wire above hangs off this. The most important test is therefore:

```
test_all_call_sites_observe_same_project_id:
    1. set up a temp git repo at root/
    2. for each function in (_persistent_project_dir,
                             policy_engine.local_patterns_path,
                             agent_generator.registry_path,
                             ...):
       call it with `root` and capture the slug component of the returned path
    3. assert all observed slugs are identical
    4. assert the slug is a UUID4
```

This is the **wiring guarantee** in one test. If it passes, the seam holds.

---

## 7. Test plan

### 7.1 Unit tests for `lib_project_id` (new file)

- `test_resolve_project_id_generates_uuid_in_fresh_repo`
- `test_resolve_project_id_returns_same_uuid_on_second_call`
- `test_resolve_project_id_same_across_worktrees` — create main + worktree, both resolve to same UUID
- `test_resolve_project_id_different_across_clones` — two `git clone`s of same source produce different UUIDs
- `test_resolve_project_id_concurrent_first_call_does_not_double_write` — two threads race; one UUID wins, both observers see it
- `test_resolve_project_id_falls_back_to_path_outside_git`
- `test_resolve_project_id_does_not_write_into_working_tree` — assert no file appears under `root/` excluding `root/.git/`
- `test_existing_dynos_project_id_file_is_respected_not_overwritten`

### 7.2 Integration tests for `_persistent_project_dir`

- `test_persistent_project_dir_returns_uuid_dir_for_git_repo`
- `test_persistent_project_dir_returns_path_fallback_for_non_git_dir`
- `test_existing_callers_observe_uuid_dir` — sample 5 callers from §6.2, assert they all hit the UUID dir
- `test_dynos_home_env_var_still_works` — `DYNOS_HOME=/tmp/foo` produces `/tmp/foo/projects/{uuid}/`

### 7.3 Wiring guarantee test (§6.5)

The single-point-of-failure test above. Block any future PR that breaks it.

### 7.4 Migration tests

- `test_migrate_id_dry_run_reports_plan_correctly`
- `test_migrate_id_execute_moves_all_state_files`
- `test_migrate_id_rewrites_embedded_fixture_paths`
- `test_migrate_id_upgrades_registry_v1_to_v2`
- `test_migrate_id_handles_two_worktrees_consolidating_to_one_uuid` — both old slugs map to same UUID; one wins, other archives
- `test_migrate_id_archives_unmappable_slug` — slug whose repo no longer exists

### 7.5 Regression tests

- `test_no_module_constructs_dynos_project_path_outside_lib_core` — AST/grep test that `Path.home() / ".dynos" / "projects"` appears only in `lib_core.py` (and `tests/`, `worktree.py` migration helpers)
- `test_no_module_calls_project_slug_outside_policy_engine` — same shape for the Claude Code mirror function

---

## 8. Migration plan

### 8.1 Compatibility window

For one release we run in dual-read mode:

- `_persistent_project_dir(root)` returns the **new UUID dir**.
- If the new UUID dir is empty/missing AND an old path-derived slug dir exists for the same root, emit warning event `identity_legacy_slug_in_use` once per process and **return the old dir**. This lets users keep working while we wait for the migration.

### 8.2 Active migration

After 1 release, drop dual-read mode. By then users will have run:

```
dynos worktree list-orphans   # shows path-slug dirs that should migrate
dynos worktree migrate-id <slug>   # migrates one
dynos worktree migrate-id --all    # migrates all, with conflict resolution
```

The `--all` form iterates the registry, computes new UUIDs, and consolidates conflicts (two slugs → same UUID happens when worktrees were registered separately during the pre-fix era).

### 8.3 Conflict resolution rules

When two old slugs migrate to the same UUID:

1. Keep the dir of whichever has the most recent `last_active_at` (read from registry).
2. For the other, run the `_plan_migration` / `_execute_migration` already in `worktree.py` (postmortems merge by name, prevention rules merge by `(source_task, source_finding, rule)`, learned-agents registry merges by `(agent_name, role, task_type)`, benchmark history dedups by `run_id`).
3. Move the loser's dir to `~/.dynos/projects/.archive/{old-slug}/` (preserves user recovery if the merge logic is wrong).

---

## 9. Failure modes

| Mode | Detection | Mitigation |
|---|---|---|
| `<git-common-dir>` is not writable (read-only checkout) | `_read_or_generate_id` catches OSError on tmp+rename | Fall back to path-derived slug, emit warning event |
| `dynos-project-id` is corrupted (not a UUID) | `resolve_project_id` validates with regex on read | Refuse to overwrite; emit `identity_corrupted_id_file` event; surface to user via `dynos status` |
| Two worktrees race the first generation | LOCK_EX on common dir serializes them | Only one UUID wins; second observer reads it |
| User's `~/.dynos/registry.json` v1→v2 migration is interrupted | Migration writes v2 atomically (tmp+rename) | If interrupted, v1 still on disk; rerun is idempotent |
| User deletes `dynos-project-id` to "fix" something | Next dynos call generates a fresh UUID | Old `~/.dynos/projects/{old-uuid}/` orphans on disk; surfaced by `list-orphans` |
| Submodule has its own `.git` file pointing to a different common-dir | Outer repo's common dir wins because we resolve `root`, not the submodule | Documented; explicit override via `.dynos/project-id` (working-tree opt-in) for users who want submodule-specific identity |
| Path-fallback ID collision across machines | `is_path_fallback_id(slug)` true; cloud sync refuses to enable | Documented limitation; user must convert to a git repo to enable sync |

---

## 10. Open questions

1. **Should the working-tree `.dynos/project-id` override be supported?** It would let users force-share an identity across clones (team-shared learned state without cloud sync). But it also lets a malicious commit force two unrelated repos to share state. Recommend: **no** for v1; revisit for cloud-sync-bound teams.

2. **What happens to `~/.claude/projects/{slug}/memory/project_rules.md`?** It stays path-based per §5.4 — but with sanitization, closing T-18. Once cloud sync exists, users may want this mirror keyed by the same UUID; defer that broader question until sync lands.

3. **Should we emit a `project_id_resolved` event on every dynos invocation?** Useful for telemetry; adds noise to `events.jsonl`. Recommend: emit once per process, not once per call (cache in module globals).

4. **Do we delete `hooks/worktree.py` after migration is complete?** It's still useful for the rare case of two worktree-orphan dirs predating the 2026-04-XX fix. Recommend: keep, but archive its prose to "legacy migration" in CHANGELOG.

5. **Telemetry of fallback usage:** if a meaningful fraction of users land on path-fallback (no git), that's a signal to invest in non-git identity. Recommend: count and surface in `dynos status` output.

---

## 11. Implementation segments (for `/dynos-work:start`)

If this design is approved, the implementation breaks into 6 segments along clean wire boundaries. Listed in dependency order:

| Seg | Files | Dependency | Estimated AC count |
|---|---|---|---|
| 1 | `hooks/lib_project_id.py` (new): `resolve_project_id`, `sanitize_path_for_slug`, `_safe_git_env`, `_assert_safe_common_dir`, `_read_or_generate_id`, `_UUID4_RE`, `ProjectIdSecurityError` + tests | none | 10 |
| 2 | `hooks/lib_core.py` `_persistent_project_dir` rewire + scrubbed env in `_resolve_git_toplevel` | seg 1 | 4 |
| 3 | `memory/policy_engine.py` `project_slug` routes through `sanitize_path_for_slug` (T-18 closure) | seg 1 | 2 |
| 4 | `hooks/registry.py` v2 schema + reader/writer + path validation gate before subprocess | seg 1 | 6 |
| 5 | `hooks/worktree.py` `migrate-id` subcommand + `list-orphans` extension | segs 1+2+4 | 6 |
| 6 | Wiring guarantee test (§6.5) + regression sentinel (§7.5) + AST scans (no `Path.home() / ".dynos"` outside seam, no `shell=True` in identity code) | segs 1+2+3 | 3 |
| 7 | Dual-read compat window + warning events (`identity_uuid_generated`, `identity_fell_back_to_path`, `identity_legacy_slug_in_use`, `registry_path_rejected`) | seg 2 | 3 |
| 8 | Threat-defense tests (every test in §12.7) | segs 1+2+3+4+5 | 16 |

Total ~50 acceptance criteria. Risk classification: **high** (touches a 60-call-site seam; trust-boundary implications for cloud sync; closes 18 enumerated attack vectors); will require security-auditor + spec-completion-auditor + dead-code-auditor in audit phase.

**Residual carved out of this work:** `manual:cloud-sync-id-forgery-defense` (T-16) — must be enqueued at task start as a deferred concern for the future cloud-sync design task.

---

## 12. Security review (second pass)

### 12.1 Threat model

**Attacker classes (in roughly increasing privilege):**

- **U1. Repository contributor** — can land a commit on a branch the user might check out. Cannot directly write to the user's filesystem.
- **U2. Local user on the same machine** — can write to `/tmp` and other world-writable dirs but not to the victim's `~/`.
- **U3. Same-uid process (compromised tool)** — can write anything the user can. Many of the threats below assume this attacker has *not yet* gained code-exec; the goal is to keep `dynos` from being the rung that grants it.
- **U4. Network attacker (future cloud sync)** — out of scope here; flagged for the cloud-sync design.

**Asset to protect:** the integrity and confidentiality of `~/.dynos/projects/{id}/` (learned policies, prevention rules, retros). Secondary: avoid letting `dynos` be a vector for attacker code execution against the user.

### 12.2 Threat table

Each threat has an ID (referenced inline in §4–§5), an attacker class, a mitigation, and a test that proves it.

| ID | Threat | Attacker | Mitigation | Test |
|---|---|---|---|---|
| **T-1** | `<git-common-dir>` is a symlink (or resolves) outside HOME — write goes to attacker location | U2/U3 | `_assert_safe_common_dir` (§5.2): refuse if `realpath(common_dir)` not under HOME, refuse if not user-owned | `test_resolve_rejects_common_dir_symlink_outside_home` |
| **T-2** | Env-var injection (`GIT_DIR`, `GIT_COMMON_DIR`, `GIT_CONFIG_*`) redirects git's idea of the repo | U3 | Scrubbed env in `_safe_git_env` (§5.6); blocklist + `GIT_CONFIG_*` prefix scrub | `test_resolve_ignores_GIT_DIR_env`, `test_resolve_ignores_GIT_CONFIG_env` |
| **T-3** | `dynos-project-id` file is a planted symlink — read returns attacker-controlled UUID | U2/U3 | `is_symlink()` check before read; `O_NOFOLLOW` on directory open (§5.2) | `test_resolve_rejects_symlinked_id_file` |
| **T-4** | `dynos-project-id` content is path-traversal (`../../etc`) or contains `/` — slug becomes traversal | U3 | Strict `_UUID4_RE` validation on read; refuse anything not a canonical UUID4 | `test_resolve_rejects_traversal_in_id_file_content` |
| **T-5** | `git rev-parse` returns a path *named* `.git` but planted in attacker dir; we trust the name check at lib_core.py:248 | U2 | Post-validate that `.git/`'s parent contains the input root (§5.6) | `test_resolve_rejects_planted_dot_git_dir` |
| **T-6** | `path-`-fallback abspath contains control chars / Unicode tricks producing a slug with embedded path components | U2 (planted symlink at known path that the user `cd`s into) | Strict regex on sanitized fallback (§4.3); refuse on failure | `test_path_fallback_rejects_control_characters`, `test_path_fallback_rejects_unicode_separators` |
| **T-7** | `~/.dynos/registry.json` is tampered (planted by U2 via NFS / shared mount / typo) — migration runs `git -C <attacker_path>` and `core.fsmonitor` RCEs the user | U2/U3 | Per-entry validation: must resolve, be a dir, user-owned, under HOME (§5.7); scrubbed env on subprocess | `test_migration_rejects_registry_paths_outside_home`, `test_migration_rejects_unowned_paths` |
| **T-8** | Migration `--all` follows symlinks under `~/.dynos/projects/` and operates on attacker-planted target | U2 | Existing `_assert_source_contained` + `is_symlink()` reject in `worktree.py`; **extend to migrate-id source iteration** | `test_migration_rejects_symlinked_source` (existing) + `test_migrate_id_rejects_symlinked_slug_dir` (new) |
| **T-9** | Concurrent `dynos` invocations from two worktrees both generate UUIDs; one wins, the other's ID file overwrites | U-self (race) | LOCK_EX on common_dir before existence check (§5.2); the loser observes the file-already-exists branch | `test_resolve_concurrent_first_call_does_not_double_write` |
| **T-10** | Case-insensitive FS (HFS+) treats two UUIDs differing only in case as one; user-edited file in uppercase confuses lookup | U-self (FS quirk) | Case-normalize on read (lower); UUID4 canonical form is lowercase | `test_resolve_normalizes_case_on_read` |
| **T-11** | Atomic write tmp file lands on a different filesystem than `<common-dir>`; `os.rename` fails non-atomically | U-self (env quirk) | `tempfile.mkstemp(dir=common_dir)` puts tmp in same FS by construction (§5.2) | `test_atomic_write_does_not_cross_filesystems` |
| **T-12** | A working-tree `.dynos/project-id` override file lets a malicious commit force the victim's dynos state into a UUID the attacker controls (e.g., to poison shared cloud state later) | U1 | **Reject** working-tree overrides in v1 (§10.1 confirmed); only `<git-common-dir>/dynos-project-id` is consulted | `test_resolve_ignores_working_tree_dynos_project_id_file` |
| **T-13** | Slug iteration in `~/.dynos/projects/` trusts unregistered UUID-shaped dirs (U2 plants `~/.dynos/projects/{some-uuid}/...`) | U2/U3 | Cross-check against registry; ignore unregistered dirs in active flows (only `list-orphans` may surface them, with a "not in registry" annotation) | `test_unregistered_uuid_dir_is_not_consulted_for_active_state` |
| **T-14** | `Path.resolve()` on `root` follows a symlink to an attacker-owned directory inside the user's HOME | U2 | After resolve, ownership check (`st.st_uid`) on the resolved path; if != euid, refuse | `test_resolve_rejects_root_symlinked_to_unowned_dir` |
| **T-15** | A future `.dynos/project-id` working-tree opt-in (revisit) inherits T-12 if not gated | U1 | Bracketed for future design; if added, MUST require an explicit operator confirmation step (e.g. `dynos accept-id <committed-id>`) | n/a until that feature is designed |
| **T-16** | Cloud sync handshake returns a server-issued ID; malicious server picks an ID colliding with another user's project | U4 | Server-side: HMAC-signed IDs with proof-of-novelty; client-side: refuse a pushed ID if it doesn't match an HMAC the server can prove. **Tracked as residual** `manual:cloud-sync-id-forgery-defense` — must be addressed before cloud sync ships. | deferred to cloud-sync design task |
| **T-17** | Subprocess invocations use `shell=True` somewhere, allowing command injection via path strings | U-self (regression) | Audit: every subprocess.run in `lib_core.py`, `worktree.py`, `registry.py` uses list-form, never `shell=True` | `test_no_shell_true_in_identity_code_path` (AST scan) |
| **T-18** | `~/.claude/projects/{slug}/` mirror slug is path-derived without sanitization; an abspath with `..` / control chars / unicode separators could write outside the mirror dir | U-self (path quirk) / U2 (planted symlink the user `cd`s into) | **In scope.** §5.4 routes `project_slug` through `sanitize_path_for_slug` (the same helper §4.3 uses), refusing on bad input | `test_claude_mirror_slug_rejects_traversal` |
| **T-19** | An interrupted migration leaves both old slug dir AND partial new UUID dir; subsequent reads pick wrong source | U-self (crash) | Migration writes are tmp-then-rename per file; backup preserved at `~/.dynos/projects/.archive/`; rerun is idempotent | `test_migration_rerun_after_interrupt_is_idempotent` |
| **T-20** | Host-only filesystem permissions: `.git/dynos-project-id` inherits `.git/` umask; in shared-clone scenarios (NFS, bind mount, multi-user dev box), other users may read | U2 (multi-user host) | `os.fchmod(0o600)` on the tmp file before rename (§5.2); document that shared clones are unsupported | `test_id_file_perms_are_0600` |

### 12.3 Defense-in-depth principles

1. **Validate at every boundary.** Don't trust git output, don't trust file content, don't trust env, don't trust the registry. Each is independently corruptible.
2. **Refuse rather than fall back silently.** Many of the mitigations above raise `ProjectIdSecurityError` instead of returning a "best-effort" slug. A loud refusal that surfaces in `dynos status` is safer than a silent fallback the user never sees.
3. **Containment.** Every path we operate on (common_dir, registry path, migration source) must be under HOME and user-owned. The `_assert_safe_common_dir` and `_assert_safe_registry_path` helpers are reused at every entry point.
4. **No working-tree-file-as-identity in v1.** Rejecting T-12 and T-15 closes the only attack vector that crosses the version-control boundary (where U1 lives).
5. **Single-seam discipline.** All identity decisions go through `lib_project_id.resolve_project_id`. No caller is permitted to derive a slug independently. The regression sentinel test (§7.5) enforces this with an AST scan.

### 12.4 What this design does NOT defend against

These are real but accepted risks, called out explicitly so a future reviewer doesn't think they were missed:

- **R-1.** A user who is already running an attacker-supplied tool with their own uid (U3 with code exec) can do anything the user can. We can't defend against that with file-format checks.
- **R-2.** A user who clones an OSS repo, opts into cloud sync, and accepts a server-issued ID is trusting the server. The cloud-sync design must address T-16 separately.
- **R-3.** If the user manually deletes `<git-common-dir>/dynos-project-id`, we generate a new UUID and orphan the old `~/.dynos/projects/{old-uuid}/` dir. `list-orphans` surfaces it. Not a security issue.
- **R-4.** Cross-machine unification (NG-1) is out of scope. Two machines with the same clone get two UUIDs by design.
- **R-5.** A determined U1 (committer) who lands a commit changing `.gitignore` or `.gitattributes` to influence git's treatment of `dynos-project-id` cannot — the file lives in `<git-common-dir>` (i.e., `.git/`), which is **not** subject to working-tree-level git config. T-12's mitigation is the only relevant defense for U1.

### 12.5 Required AST/static checks

Add to the regression-sentinel test in §7.5:

- **No `Path.home() / ".dynos"` outside `lib_core.py` and `lib_project_id.py`** — every path construction must go through the seam.
- **No `subprocess` call with `shell=True` in `lib_project_id.py`, `lib_core.py`, `worktree.py`, `registry.py`** — list-form only.
- **No `git` invocation without `env=_safe_git_env()`** in identity-resolution code paths — AST scan on `subprocess.run([..., "git", ...])` calls in the four files.
- **No `Path.resolve()` followed immediately by a write operation without an ownership check** — flag pattern visually; manual review at PR time.

### 12.6 Mapping to existing code

| Threat | Existing code that already mitigates | Gap closed by this doc |
|---|---|---|
| T-7 | `worktree.py:_assert_source_contained` (refuses non-projects-root sources, refuses symlinks) | Extend the same gate to registry-driven iteration in `migrate-id --all` |
| T-8 | `worktree.py` symlink rejection in `_execute_migration` (`if pm_file.is_symlink(): continue`) | Apply identically in `migrate-id` |
| T-2 | `lib_core.py:_resolve_git_toplevel` runs subprocess but does NOT scrub env | Add scrub; add post-validation |
| T-5 | `lib_core.py:248` checks `common_path.name != ".git"` but doesn't validate the parent contains the input root | Add the parent-contains-root check |
| T-17 | `worktree.py` and `lib_core.py` already use list-form subprocess | Add an AST-scan regression test to keep it that way |

### 12.7 Test plan additions for §7

Add these to §7.1 / §7.2 / §7.4 as appropriate:

- `test_resolve_rejects_common_dir_symlink_outside_home`
- `test_resolve_rejects_root_symlinked_to_unowned_dir`
- `test_resolve_ignores_GIT_DIR_env`
- `test_resolve_ignores_GIT_CONFIG_env`
- `test_resolve_rejects_symlinked_id_file`
- `test_resolve_rejects_traversal_in_id_file_content`
- `test_resolve_rejects_planted_dot_git_dir`
- `test_resolve_normalizes_case_on_read`
- `test_resolve_ignores_working_tree_dynos_project_id_file`
- `test_path_fallback_rejects_control_characters`
- `test_path_fallback_rejects_unicode_separators`
- `test_atomic_write_does_not_cross_filesystems`
- `test_id_file_perms_are_0600`
- `test_migration_rejects_registry_paths_outside_home`
- `test_migration_rejects_unowned_paths`
- `test_migration_rejects_symlinked_source` (extend existing)
- `test_migration_rerun_after_interrupt_is_idempotent`
- `test_no_shell_true_in_identity_code_path` (AST scan)
- `test_no_dynos_path_construction_outside_seam` (AST scan)
- `test_unregistered_uuid_dir_is_not_consulted_for_active_state`
- `test_claude_mirror_slug_rejects_traversal` (T-18)
- `test_claude_mirror_slug_rejects_control_characters` (T-18)
- `test_sanitize_path_for_slug_is_shared_between_fallback_and_claude_mirror` (single-source-of-truth check)

These bring the spec from ~27 ACs to **~47 ACs**. The risk classification stays at **high** but the trust-boundary surface is now exhaustively enumerated and individually tested.

---

## 13. Decision

This doc proposes the design above. The §6 wiring map and the §12 threat table are the two artifacts a reviewer should focus on. If both pass review, implementation follows the segments in §11 with the expanded test suite from §12.7.
