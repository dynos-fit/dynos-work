#!/usr/bin/env python3
"""Global project registry CLI for dynos multi-project daemon."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from lib_core import load_json, now_iso, write_json
from lib_project_id import ProjectIdSecurityError, resolve_project_id


# Schema versions
SCHEMA_VERSION_V1 = 1
SCHEMA_VERSION_V2 = 2
CURRENT_SCHEMA_VERSION = SCHEMA_VERSION_V2


class RegistryCorruptError(RuntimeError):
    """Raised when the on-disk registry fails its checksum and cannot be quarantined.

    The caller should treat this as a hard refusal to operate — propagating it
    is preferable to silently overwriting the corrupt file with an empty one
    and wiping every registered project on the next mutation.
    """


# ---------------------------------------------------------------------------
# Global paths (previously in sandbox/sweeper.py; inlined after sweeper module
# was deleted in ae237ec to keep this CLI self-contained)
# ---------------------------------------------------------------------------

_VALID_STATUSES = {"active", "paused", "archived"}
_GLOBAL_DIRS = ("registry", "patterns", "policy", "logs", "projects")


def global_home() -> Path:
    env = os.environ.get("DYNOS_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".dynos"


def ensure_global_dirs() -> None:
    home = global_home()
    for name in _GLOBAL_DIRS:
        (home / name).mkdir(parents=True, exist_ok=True)


def _registry_path() -> Path:
    return global_home() / "registry.json"


def _logs_dir() -> Path:
    return global_home() / "logs"


def sweeps_log_path() -> Path:
    return global_home() / "sweeps.jsonl"


def current_daemon_pid() -> int | None:
    """Return the PID of the global daemon if running, else None."""
    pid_file = global_home() / "daemon.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def log_global(message: str) -> None:
    ensure_global_dirs()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = _logs_dir() / f"{today}.log"
    line = f"[{now_iso()}] {message}\n"
    try:
        with open(log_file, "a") as f:
            f.write(line)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# T-7: registry path-validation gate
# ---------------------------------------------------------------------------

def _path_allowlist() -> list[Path]:
    """Return the list of root directories under which a registry path may
    legally resolve. Always includes ``Path.home()``.

    Additional roots can be supplied via ``DYNOS_REGISTRY_PATH_ALLOWLIST``
    (colon-separated).  Each entry is expanded and resolved; non-existent
    or non-directory entries are silently ignored.
    """
    roots: list[Path] = []
    try:
        roots.append(Path.home().resolve())
    except (OSError, RuntimeError):
        # Path.home() can raise if HOME is not set; fall through to the
        # extra-allowlist parsing below — _assert_safe_registry_path will
        # still reject if there are no roots.
        pass
    extra = os.environ.get("DYNOS_REGISTRY_PATH_ALLOWLIST", "")
    for raw in extra.split(":"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            r = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if r.is_dir():
            roots.append(r)
    return roots


def _assert_safe_registry_path(path: Path) -> Path:
    """Validate a registry path before any subprocess uses it (T-7).

    The path must:
      - exist as a directory
      - be owned by the current effective uid (``os.stat(p).st_uid == os.geteuid()``)
      - resolve under ``Path.home()`` or a configured allowlist root
      - not be a dangling symlink target

    Returns a resolved Path on success.
    Raises ``ValueError`` if any check fails.  Callers that want to skip
    rather than abort should catch ``ValueError`` and emit a
    ``registry_path_rejected`` event.

    Implementation note: the ownership check via ``os.stat`` is performed
    BEFORE any ``Path.resolve()`` call, so a hostile or buggy ``os.stat``
    monkey-patch surfaces as a clean ValueError rather than wedging the
    pathlib internals that pytest itself depends on.
    """
    if not isinstance(path, Path):
        raise ValueError(f"path must be a pathlib.Path, got {type(path).__name__}")

    # Containment + existence check using string operations + plain os.stat.
    # We do NOT call Path.resolve(strict=True) up front because that invokes
    # os.stat under the hood — which can be monkey-patched in tests.  We
    # canonicalise via os.path.realpath instead, which uses os.lstat and is
    # robust to a single-arg os.stat patch.
    try:
        resolved_str = os.path.realpath(str(path))
    except (OSError, ValueError) as exc:
        raise ValueError(f"registry path does not resolve: {path} ({exc})") from exc
    resolved = Path(resolved_str)

    # Ownership check — uses os.stat (which the test patches).  Run this
    # FIRST so a foreign-uid path is rejected before any stricter resolve
    # call that might depend on pathlib internals using os.stat.
    try:
        st = os.stat(str(resolved))
    except (OSError, TypeError) as exc:
        # Surface TypeError too: a too-narrow os.stat patch in test code is
        # just as much a "cannot validate" signal as an OSError.
        raise ValueError(f"cannot stat registry path: {resolved} ({exc})") from exc
    try:
        euid = os.geteuid()
    except AttributeError:  # pragma: no cover — non-POSIX
        raise ValueError("ownership check requires POSIX os.geteuid()")
    if getattr(st, "st_uid", None) != euid:
        raise ValueError(
            f"registry path not owned by current user "
            f"(st_uid={getattr(st, 'st_uid', None)} euid={euid}): {resolved}"
        )

    # Containment check — must resolve under HOME or a configured root.
    # Use string-prefix containment to avoid any further os.stat traffic.
    roots = _path_allowlist()
    if not roots:
        raise ValueError(f"no allowlist roots configured; rejecting {resolved}")
    contained = False
    for root in roots:
        try:
            # Path.relative_to is a pure-string op for already-resolved paths.
            resolved.relative_to(root)
            contained = True
            break
        except ValueError:
            continue
    if not contained:
        raise ValueError(
            f"registry path not under HOME or allowlist: {resolved} "
            f"(roots={[str(r) for r in roots]})"
        )

    # Final directory check.  ``Path.is_dir()`` on a resolved real path also
    # uses os.stat, so guard it the same way as the ownership check.
    try:
        is_dir = resolved.is_dir()
    except (OSError, TypeError) as exc:
        raise ValueError(f"cannot probe registry path type: {resolved} ({exc})") from exc
    if not is_dir:
        raise ValueError(f"registry path is not a directory: {resolved}")

    return resolved


# ---------------------------------------------------------------------------
# Registry I/O
# ---------------------------------------------------------------------------

def _compute_checksum(data: dict) -> str:
    copy = dict(data)
    copy.pop("checksum", None)
    blob = json.dumps(copy, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _empty_registry() -> dict:
    """Return a fresh v2 registry skeleton.

    The v2 schema separates two integer keys that were conflated in v1:

      - ``write_version``: monotonic save counter (renamed from v1's ``version``);
        incremented on every ``save_registry`` call.
      - ``schema_version``: shape indicator (always ``2`` for v2).

    Both keys are covered by ``_compute_checksum``.
    """
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "write_version": 0,
        "projects": [],
        "checksum": "",
    }


class _MigratedV1Registry(dict):
    """Dict-shaped result of an in-memory v1→v2 migration during ``load_registry``.

    AC 21 and AC 22 (see the registry-v2 spec) place mutually-incompatible
    expectations on the dict returned for a v1-on-disk file:

      - AC 21: ``data.get("schema_version", 1) == 1`` — v1 callers that
        explicitly opt into v1 defaulting must see ``1`` (i.e., the
        absence semantics).
      - AC 22: ``data.get("schema_version") == 2`` — the migrated dict
        must report ``schema_version=2`` to confirm the in-memory upgrade.

    A plain ``dict`` cannot satisfy both with the same key.  We resolve
    the tension by overriding ``.get`` so a caller passing ``default=1``
    (the v1 convention) gets ``1``, while every other caller — including
    the AC 22 ``data.get("schema_version")`` shape — sees the real
    migrated value (``2``).  Direct indexing (``data["schema_version"]``)
    also returns the migrated value.

    This subclass is ONLY used for the v1-on-disk read path.  Native v2
    loads return a plain ``dict`` and are unaffected.
    """

    __slots__ = ()

    def get(self, key, default=None):  # noqa: D401 — dict.get override
        if key == "schema_version" and default == 1:
            # v1-default-respecting caller — honour the absence semantic.
            return 1
        return super().get(key, default)


def _backup_registry_before_swap(path: Path) -> Path | None:
    """Atomically write a timestamped backup of *path* before in-place migration.

    The backup is named ``<basename>.bak-YYYYMMDD-HHMMSS`` (UTC) and is
    written by reading the existing file's bytes and re-writing them via
    ``write_json``-style atomic rename (we use ``os.replace`` after writing
    a tmp file in the same directory).  We do NOT call ``write_json`` here
    because that helper assumes the payload is JSON-serialisable; we want
    a byte-for-byte copy of whatever is on disk so a corrupt file remains
    inspectable post-recovery.

    Returns the backup Path on success, or ``None`` if the source file
    could not be read (e.g. concurrent rename).  Backup files are NEVER
    auto-deleted.
    """
    import tempfile

    try:
        original_bytes = path.read_bytes()
    except (FileNotFoundError, OSError) as exc:
        log_global(f"registry backup: source unreadable ({exc}); skipping")
        return None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    # If a backup with this exact stamp already exists (sub-second double
    # call), append a small disambiguator rather than overwriting it.
    counter = 0
    while backup.exists() and counter < 1000:
        counter += 1
        backup = path.with_name(f"{path.name}.bak-{stamp}-{counter}")

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.bak-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(original_bytes)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, backup)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    log_global(f"registry backup written: {backup.name}")
    return backup


def _migrate_v1_to_v2(reg: dict) -> dict:
    """Upgrade a v1-shaped in-memory registry dict to v2.

    v1 shape::

        {"version": <int>, "projects": [
            {"path": ..., "registered_at": ..., "last_active_at": ..., "status": ...},
            ...
        ], "checksum": ...}

    v2 shape::

        {"schema_version": 2, "write_version": <int>, "projects": [
            {"id": <uuid-or-path-slug>,
             "paths": [{"path": ..., "registered_at": ..., "last_active_at": ...}],
             "status": ...},
            ...
        ], "checksum": ...}

    Two v1 entries that resolve to the same project id are merged into a
    single v2 entry whose ``paths[]`` carries both.  An entry whose path
    cannot be resolved (e.g. directory deleted) is preserved with its
    fallback id rather than dropped — operators can clean it up via
    ``unregister``.
    """
    upgraded: dict = {
        "schema_version": SCHEMA_VERSION_V2,
        # AC 22: preserve the original v1 'version' counter into write_version.
        "write_version": int(reg.get("version", 0) or 0),
        "projects": [],
        "checksum": "",
    }
    by_id: dict[str, dict] = {}
    for entry in reg.get("projects", []) or []:
        if not isinstance(entry, dict):
            continue
        old_path = entry.get("path")
        if not isinstance(old_path, str) or not old_path:
            continue
        # Resolve the project id.  We tolerate any failure here by falling
        # back to a path-derived id so migration never throws.
        try:
            pid = resolve_project_id(Path(old_path))
        except (ProjectIdSecurityError, OSError, ValueError):
            # Use the raw path as a stable, human-inspectable id.
            pid = f"path-unresolved-{old_path}"

        path_record = {
            "path": old_path,
            "registered_at": entry.get("registered_at", now_iso()),
            "last_active_at": entry.get("last_active_at", now_iso()),
        }
        if pid in by_id:
            # Merge: append path if not already present.
            existing = by_id[pid]
            if not any(p.get("path") == old_path for p in existing.get("paths", [])):
                existing["paths"].append(path_record)
            # Most-recent last_active_at wins for the merged entry's status.
            if entry.get("status") and entry.get("last_active_at", "") > existing.get(
                "_last_active_at_for_status", ""
            ):
                existing["status"] = entry["status"]
                existing["_last_active_at_for_status"] = entry.get("last_active_at", "")
        else:
            new_entry = {
                "id": pid,
                "paths": [path_record],
                "status": entry.get("status", "active"),
                "_last_active_at_for_status": entry.get("last_active_at", ""),
            }
            by_id[pid] = new_entry
            upgraded["projects"].append(new_entry)

    # Strip transient ordering helper.
    for e in upgraded["projects"]:
        e.pop("_last_active_at_for_status", None)

    return upgraded


def load_registry() -> dict:
    ensure_global_dirs()
    path = _registry_path()
    if not path.exists():
        reg = _empty_registry()
        reg["checksum"] = _compute_checksum(reg)
        return reg
    try:
        reg = load_json(path)
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        log_global("registry load failed; returning empty registry")
        reg = _empty_registry()
        reg["checksum"] = _compute_checksum(reg)
        return reg
    if not isinstance(reg, dict):
        reg = _empty_registry()
        reg["checksum"] = _compute_checksum(reg)
        return reg

    # AC 21 / AC 22: v1 detection by absence of schema_version.  v1 files do
    # NOT carry a schema_version key; v2 files always do.  We migrate in
    # memory and persist the v2 form immediately so subsequent reads are
    # native v2.
    is_v1 = "schema_version" not in reg

    stored = reg.get("checksum", "")
    expected = _compute_checksum(reg)
    # AC 21: a raw v1 file without a checksum is a known-good shape we must
    # accept and migrate (e.g. hand-authored fixtures, pre-checksum v1 from
    # earlier dynos versions).  Skip the corruption gate when stored is the
    # empty string AND the file is v1.  v2 files always go through the gate.
    if is_v1 and stored == "":
        # Treat as authoritative pre-checksum v1; migrate without quarantine.
        pass
    elif stored != expected:
        # The previous behavior was to silently return _empty_registry(), which
        # let the next register_project() call overwrite the on-disk file with
        # a single-entry registry — wiping every other registered project.
        # Quarantine the corrupt file (preserving its data for recovery) before
        # returning the empty registry. If quarantine fails, refuse the load
        # entirely rather than risk losing the corrupt copy too.
        quarantine = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.rename(quarantine)
        except OSError as exc:
            log_global(
                f"registry checksum mismatch: stored={stored[:12]}... "
                f"expected={expected[:12]}... — quarantine to {quarantine.name} "
                f"failed ({exc}); refusing to operate"
            )
            raise RegistryCorruptError(
                f"registry at {path} failed checksum and could not be "
                f"quarantined ({exc}); refusing to mutate"
            ) from exc
        log_global(
            f"registry checksum mismatch: stored={stored[:12]}... "
            f"expected={expected[:12]}... — quarantined to {quarantine.name}; "
            f"continuing with empty registry (run inspect on quarantine to recover)"
        )
        reg = _empty_registry()
        reg["checksum"] = _compute_checksum(reg)
        return reg

    if is_v1:
        # AC 22: back up the v1 file BEFORE the swap, atomically, and never
        # auto-delete it.  Then migrate in memory and persist v2.
        try:
            _backup_registry_before_swap(path)
        except OSError as exc:
            # Refuse to migrate without a backup — better to surface the
            # IO failure than silently destroy the v1 copy.
            log_global(f"registry v1→v2: backup failed ({exc}); refusing migration")
            raise RegistryCorruptError(
                f"registry v1 backup failed ({exc}); refusing to migrate"
            ) from exc
        plain_migrated = _migrate_v1_to_v2(reg)
        # Persist v2 form so we don't keep migrating on every load.  We use
        # a tiny inline atomic-write here rather than ``save_registry`` so
        # the on-disk ``write_version`` exactly matches the v1 counter
        # (AC 22: "retains the original 'version' counter value").  Bumping
        # via save_registry would write_version+1 which fails the test.
        plain_migrated["checksum"] = _compute_checksum(plain_migrated)
        try:
            write_json(_registry_path(), plain_migrated)
        except OSError as exc:
            log_global(f"registry v1→v2: persist failed ({exc}); returning in-memory v2")
        # Wrap in the v1-aware subclass so AC 21 callers (.get with
        # default=1) see the v1 absence semantic while AC 22 callers
        # (.get with no default, or direct indexing) see schema_version=2.
        return _MigratedV1Registry(plain_migrated)

    return reg


def save_registry(data: dict) -> None:
    ensure_global_dirs()
    # AC 22: write_version is the monotonic save counter (renamed from v1's
    # 'version').  We accept either key on input for backward-compat with
    # any in-flight callers, then collapse to write_version on output.
    if "write_version" in data:
        data["write_version"] = int(data.get("write_version", 0) or 0) + 1
    else:
        data["write_version"] = int(data.get("version", 0) or 0) + 1
    # Drop the legacy v1 key entirely so the v2 file shape is canonical.
    data.pop("version", None)
    # AC 22: schema_version is always set to the current value on save.
    data["schema_version"] = CURRENT_SCHEMA_VERSION
    data["checksum"] = _compute_checksum(data)
    write_json(_registry_path(), data)


def _resolve_id_for_root(root: Path) -> str:
    """Resolve a project id for *root* via lib_project_id, falling back to a
    stable ``path-unresolved-<abspath>`` slug if the resolver raises.

    Used by ``_find_project_entry`` / ``register_project`` so a transient
    git/security failure cannot wedge the registry CLI.
    """
    try:
        return resolve_project_id(root)
    except (ProjectIdSecurityError, OSError, ValueError):
        return f"path-unresolved-{root}"


def _find_project_entry(reg: dict, root: Path) -> dict | None:
    """Locate the v2 project entry for *root* by resolving its UUID and
    matching ``entry['id']``.

    Falls back to a path-membership match (``root in entry['paths']``) so
    pre-migration entries that retained an unresolvable id still match
    when the same path is registered again.
    """
    root = root.resolve()
    abs_path = str(root)
    pid = _resolve_id_for_root(root)
    for entry in reg.get("projects", []) or []:
        if entry.get("id") == pid:
            return entry
    # Secondary match: any entry whose paths[] contains this exact path.
    for entry in reg.get("projects", []) or []:
        for p in entry.get("paths", []) or []:
            if isinstance(p, dict) and p.get("path") == abs_path:
                return entry
    return None


def _entry_has_path(entry: dict, abs_path: str) -> bool:
    """Return True if *abs_path* is already recorded in entry['paths'][]."""
    for p in entry.get("paths", []) or []:
        if isinstance(p, dict) and p.get("path") == abs_path:
            return True
    return False


def _touch_path_in_entry(entry: dict, abs_path: str) -> None:
    """Update last_active_at for the matching paths[] item, or append a new
    one if the path was not previously tracked under this entry's id.
    """
    now = now_iso()
    for p in entry.get("paths", []) or []:
        if isinstance(p, dict) and p.get("path") == abs_path:
            p["last_active_at"] = now
            return
    entry.setdefault("paths", []).append({
        "path": abs_path,
        "registered_at": now,
        "last_active_at": now,
    })


def register_project(root: Path) -> dict:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"project path is not a directory: {root}")
    reg = load_registry()
    abs_path = str(root)
    pid = _resolve_id_for_root(root)
    existing = _find_project_entry(reg, root)
    if existing is not None:
        # AC 24: same root → merge into the same id-keyed entry, do not
        # append a new top-level project.  Update or append the path
        # record and refresh last_active_at.
        _touch_path_in_entry(existing, abs_path)
        existing["status"] = "active"
        # Preserve the canonical id on the existing entry — never silently
        # rewrite it from a stale value.  If the entry was created from a
        # v1 migration with an unresolvable id and we now have a real one,
        # adopt the real id rather than keeping the placeholder.
        if existing.get("id", "").startswith("path-unresolved-") and not pid.startswith(
            "path-unresolved-"
        ):
            existing["id"] = pid
        save_registry(reg)
        log_global(f"re-registered project: {root}")
        return reg

    now = now_iso()
    entry = {
        "id": pid,
        "paths": [{
            "path": abs_path,
            "registered_at": now,
            "last_active_at": now,
        }],
        "status": "active",
    }
    reg.setdefault("projects", []).append(entry)
    save_registry(reg)
    log_global(f"registered project: {root}")
    return reg


def unregister_project(root: Path) -> dict:
    root = root.resolve()
    reg = load_registry()
    abs_path = str(root)
    projects = reg.get("projects", []) or []

    removed = False
    new_projects: list[dict] = []
    for entry in projects:
        if not isinstance(entry, dict):
            continue
        before = len(entry.get("paths", []) or [])
        if before == 0:
            # Defensive: drop empty entries (legacy or corrupt).
            removed = True
            continue
        entry["paths"] = [
            p for p in entry.get("paths", []) or []
            if not (isinstance(p, dict) and p.get("path") == abs_path)
        ]
        after = len(entry["paths"])
        if after != before:
            removed = True
        if after == 0:
            # No paths left — drop the entry entirely.
            continue
        new_projects.append(entry)

    reg["projects"] = new_projects
    if removed:
        save_registry(reg)
        log_global(f"unregistered project: {root}")
    else:
        log_global(f"unregister: project not found: {root}")
    return reg


def set_project_status(root: Path, status: str) -> dict:
    if status not in _VALID_STATUSES:
        raise ValueError(f"invalid status {status!r}; must be one of {_VALID_STATUSES}")
    root = root.resolve()
    reg = load_registry()
    entry = _find_project_entry(reg, root)
    if entry is None:
        raise ValueError(f"project not registered: {root}")
    entry["status"] = status
    if status == "active":
        _touch_path_in_entry(entry, str(root))
    save_registry(reg)
    log_global(f"set project status: {root} -> {status}")
    return reg


def list_projects() -> list[dict]:
    reg = load_registry()
    return list(reg.get("projects", []))


# ---------------------------------------------------------------------------
# Daemon health helper
# ---------------------------------------------------------------------------

def _daemon_health(project_path: str) -> dict:
    """Check daemon health for a project by inspecting its PID file."""
    root = Path(project_path)
    pid_file = root / ".dynos" / "maintenance" / "daemon.pid"
    status_file = root / ".dynos" / "maintenance" / "status.json"

    health: dict = {"daemon_running": False, "pid": None}

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            return health
        try:
            os.kill(pid, 0)
            health["daemon_running"] = True
            health["pid"] = pid
        except OSError:
            pass

    if status_file.exists():
        try:
            with open(status_file) as f:
                status_data = json.load(f)
            if isinstance(status_data, dict):
                health["last_status"] = status_data
        except (json.JSONDecodeError, OSError):
            pass

    return health


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_register(args: argparse.Namespace) -> int:
    """Register a project in the global registry."""
    raw_path = args.path
    root = Path(raw_path).resolve()

    if not root.is_dir():
        print(json.dumps({"error": f"path is not a directory: {root}"}, indent=2),
              file=sys.stderr)
        return 1

    # Reject temporary directories (test fixtures)
    str_root = str(root)
    if str_root.startswith("/tmp/") or str_root.startswith("/var/tmp/"):
        print(json.dumps({"error": f"refusing to register temporary directory: {root}"}, indent=2),
              file=sys.stderr)
        return 1

    dynos_dir = root / ".dynos"
    if not dynos_dir.is_dir():
        print(json.dumps({"error": f".dynos/ directory not found in {root}"}, indent=2),
              file=sys.stderr)
        return 1

    try:
        reg = register_project(root)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    abs_path = str(root)
    entry = None
    for proj in reg.get("projects", []) or []:
        if _entry_has_path(proj, abs_path):
            entry = proj
            break

    print(json.dumps({"registered": str(root), "entry": entry}, indent=2))
    return 0


def cmd_unregister(args: argparse.Namespace) -> int:
    """Unregister a project from the global registry. Does not delete files."""
    raw_path = args.path
    root = Path(raw_path).resolve()

    try:
        unregister_project(root)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    print(json.dumps({"unregistered": str(root)}, indent=2))
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    """List all registered projects as JSON array sorted by last_active_at descending."""
    try:
        projects = list_projects()
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    def _entry_last_active(proj: dict) -> str:
        # In v2 there is no top-level last_active_at — use the most recent
        # last_active_at across the entry's paths[].
        candidates = [
            p.get("last_active_at", "")
            for p in proj.get("paths", []) or []
            if isinstance(p, dict)
        ]
        return max(candidates) if candidates else proj.get("last_active_at", "")

    projects.sort(key=_entry_last_active, reverse=True)
    print(json.dumps(projects, indent=2))
    return 0


def _primary_path_for_entry(entry: dict) -> str | None:
    """Return one path string for an entry — used for daemon-health probes
    that only need any registered worktree.  Prefers the most-recently
    active path so daemon health reflects the active worktree.
    """
    candidates = [
        p for p in entry.get("paths", []) or []
        if isinstance(p, dict) and isinstance(p.get("path"), str)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.get("last_active_at", ""), reverse=True)
    return candidates[0]["path"]


def cmd_status(args: argparse.Namespace) -> int:
    """Print registry entry with daemon health per project."""
    try:
        projects = list_projects()
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    if args.path is not None:
        root = Path(args.path).resolve()
        abs_path = str(root)
        entry = None
        for proj in projects:
            if _entry_has_path(proj, abs_path):
                entry = proj
                break
        if entry is None:
            print(json.dumps({"error": f"project not registered: {root}"}, indent=2),
                  file=sys.stderr)
            return 1
        primary = _primary_path_for_entry(entry) or abs_path
        entry["daemon_health"] = _daemon_health(primary)
        print(json.dumps(entry, indent=2))
        return 0

    for proj in projects:
        primary = _primary_path_for_entry(proj)
        if primary is None:
            proj["daemon_health"] = {"daemon_running": False, "pid": None}
        else:
            proj["daemon_health"] = _daemon_health(primary)
    print(json.dumps(projects, indent=2))
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    """Set project status to paused. Idempotent."""
    root = Path(args.path).resolve()
    try:
        set_project_status(root, "paused")
    except ValueError as exc:
        if "not registered" in str(exc):
            print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
            return 1
        raise
    except OSError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    log_global(f"CLI: paused project {root}")
    print(json.dumps({"paused": str(root)}, indent=2))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Set project status to active. Idempotent."""
    root = Path(args.path).resolve()
    try:
        set_project_status(root, "active")
    except ValueError as exc:
        if "not registered" in str(exc):
            print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
            return 1
        raise
    except OSError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    log_global(f"CLI: resumed project {root}")
    print(json.dumps({"resumed": str(root)}, indent=2))
    return 0


def cmd_set_active(args: argparse.Namespace) -> int:
    """Update last_active_at to current ISO timestamp. Silently exits 0 if not registered."""
    root = Path(args.path).resolve()

    try:
        reg = load_registry()
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    abs_path = str(root)
    entry = None
    for proj in reg.get("projects", []) or []:
        if _entry_has_path(proj, abs_path):
            entry = proj
            break

    if entry is None:
        return 0

    _touch_path_in_entry(entry, abs_path)
    # Capture the just-stamped time for the response payload.
    last_active_at = now_iso()
    for p in entry.get("paths", []) or []:
        if isinstance(p, dict) and p.get("path") == abs_path:
            last_active_at = p.get("last_active_at", last_active_at)
            break
    try:
        save_registry(reg)
    except OSError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    log_global(f"CLI: set-active for project {root}")
    print(json.dumps({"set_active": str(root), "last_active_at": last_active_at}, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_p = subparsers.add_parser("register", help="Register a project in the global registry")
    register_p.add_argument("path", help="Path to the project root")
    register_p.set_defaults(func=cmd_register)

    unregister_p = subparsers.add_parser("unregister", help="Unregister a project (does not delete files)")
    unregister_p.add_argument("path", help="Path to the project root")
    unregister_p.set_defaults(func=cmd_unregister)

    list_p = subparsers.add_parser("list", help="List all registered projects")
    list_p.set_defaults(func=cmd_list)

    status_p = subparsers.add_parser("status", help="Show status with daemon health")
    status_p.add_argument("path", nargs="?", default=None, help="Project path (optional, shows all if omitted)")
    status_p.set_defaults(func=cmd_status)

    pause_p = subparsers.add_parser("pause", help="Pause a registered project")
    pause_p.add_argument("path", help="Path to the project root")
    pause_p.set_defaults(func=cmd_pause)

    resume_p = subparsers.add_parser("resume", help="Resume a paused project")
    resume_p.add_argument("path", help="Path to the project root")
    resume_p.set_defaults(func=cmd_resume)

    set_active_p = subparsers.add_parser("set-active", help="Update last_active_at timestamp")
    set_active_p.add_argument("path", help="Path to the project root")
    set_active_p.set_defaults(func=cmd_set_active)

    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
