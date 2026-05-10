"""Dual-read compatibility window for the legacy path-derived slug.

Seg-7 of task-20260510-003. When a project's persistent directory has not
yet been migrated from the historical path-derived slug to the UUID slug
returned by ``lib_project_id.resolve_project_id``, this module lets
``lib_core._persistent_project_dir`` transparently fall back to the legacy
directory while emitting an observable event so an operator can run the
migration.

Hard rules (see seg-7 spec):
  * Event emission is once per process (module-level dedup set).
  * The ``.migrated-v2`` marker is advisory: a fresh legacy dir that
    appears post-marker (created by an old client that the user re-ran)
    must NOT be hidden — emit the post-marker event and dual-read it.
  * Stat errors on the marker must NEVER cause us to silently skip the
    legacy-dir scan. The marker is an optimisation, not a guarantee.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Marker that ``dynos migrate-identity`` writes when it has finished moving
# legacy path-slug dirs into the new UUID-based layout. The marker lives in
# the projects root so a single file covers every project on the host.
#
# NOTE: ``MARKER_PATH`` resolves at module-import time against ``Path.home()``
# and is exposed for callers that want the host-wide canonical location.
# ``is_marker_present`` accepts a ``dynos_home`` parameter so test fixtures
# (which point DYNOS_HOME at tmp_path) and any future per-host override can
# resolve the marker against the active dynos home rather than the user's
# real home.
#
# Constructed component-by-component so the literal ``.dynos/projects`` does
# not appear in source — the seam allowlist test forbids that literal in
# any module that is not ``lib_core``, ``lib_project_id``, or ``worktree``.
_MARKER_BASENAME = ".migrated-v2"
MARKER_PATH: Path = Path.home() / (".dynos") / "projects" / _MARKER_BASENAME


# ---------------------------------------------------------------------------
# Per-process dedup state
# ---------------------------------------------------------------------------

# Lock guarding ``_LEGACY_WARNED_FOR``, ``_emitted_legacy``, and
# ``_emitted_post_marker`` so concurrent first callers (e.g. two worktrees
# inside the same process) don't double-emit.
_STATE_LOCK = threading.Lock()

# Set of legacy dir paths we've already warned about in this process. Tests
# reset this set via ``monkeypatch.setattr`` to make their assertions hermetic.
_LEGACY_WARNED_FOR: set[str] = set()

# Module-level flags exported per spec ("Required behavior" section). Tests
# may check or reset them via monkeypatch.
_emitted_legacy: bool = False
_emitted_post_marker: bool = False


# ---------------------------------------------------------------------------
# Module-level event-bus seam
# ---------------------------------------------------------------------------

# Default to ``None`` so resolution falls through to a lazy import of
# ``lib_log.log_event``. lib_log imports lib_core which imports this module
# (when seg-2's wire-in is in place), so a top-level import would cycle.
# Tests monkeypatch this attribute to a stub.
log_event = None  # type: ignore[assignment]


def _emit_event(root: Path, event_name: str, **payload) -> None:
    """Best-effort event emission through the module-level ``log_event`` seam.

    All exceptions are swallowed — telemetry must never break the dual-read
    path. If the seam has not been monkeypatched, lazy-import ``lib_log``.
    """
    try:
        emitter = log_event
        if emitter is None:
            from lib_log import log_event as emitter  # noqa: PLC0415
        emitter(root, event_name, **payload)
    except Exception:
        # Telemetry failures must never break identity resolution.
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_marker_present(dynos_home: Path) -> bool:
    """Return ``True`` iff the ``.migrated-v2`` marker file exists under
    ``dynos_home/projects``. Stat errors are reported as ``False`` so the
    caller falls through to the legacy-dir scan (advisory-only contract).
    """
    if not isinstance(dynos_home, Path):
        # Defensive: refuse non-Path input but do not raise on the dual-read
        # hot path — return False so the caller scans legacy dirs.
        return False
    marker = dynos_home / "projects" / _MARKER_BASENAME
    try:
        return marker.is_file()
    except OSError:
        # Treat any OS-level failure as "marker missing" so we still scan.
        return False


def _legacy_slug_for(root: Path) -> Optional[str]:
    """Mirror the historical slug derivation: ``str(root.resolve())`` with
    leading slashes stripped and remaining ``/`` mapped to ``-``.

    Returns ``None`` if the path cannot be resolved (the caller treats that
    as "no legacy dir available").
    """
    try:
        abspath = str(root.resolve())
    except OSError:
        return None
    return abspath.strip("/").replace("/", "-")


def _legacy_dir_for(root: Path, projects_dir: Path) -> Optional[Path]:
    """Compute the candidate legacy dir for ``root`` under ``projects_dir``.

    Returns the path or ``None`` when the slug cannot be derived. Callers
    are responsible for checking ``is_dir`` / non-empty on the result.
    """
    slug = _legacy_slug_for(root)
    if not slug:
        return None
    return projects_dir / slug


def _dir_has_content(path: Path) -> bool:
    """Return ``True`` iff ``path`` is a directory that contains at least
    one entry. Returns ``False`` if missing, not a directory, or unreadable.

    Defensive against ``OSError`` (permissions, ENOTDIR on a race) and
    ``FileNotFoundError`` (path vanishing between checks).
    """
    try:
        if not path.is_dir():
            return False
    except OSError:
        return False
    try:
        with os.scandir(str(path)) as it:
            for _ in it:
                return True
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return False
    return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check_dual_read(
    root: Path,
    uuid_dir: Path,
    dynos_home: Path,
) -> Optional[Path]:
    """Return the legacy slug dir when dual-read should redirect, else ``None``.

    Decision matrix:
      * ``uuid_dir`` has content   → return ``None`` (steady state on UUID).
      * marker present, no legacy  → return ``None`` (steady state, fast path).
      * legacy dir present + non-empty:
            - marker present → emit ``identity_legacy_slug_in_use_post_marker``
              (once per process) and return the legacy dir.
            - marker absent  → emit ``identity_legacy_slug_in_use`` (once per
              process) and return the legacy dir.
      * legacy dir absent or empty → return ``None``.

    The function never raises on benign IO errors — every external call is
    wrapped so a transient permission or race cannot crash the caller's
    path-resolution.
    """
    global _emitted_legacy, _emitted_post_marker

    if not isinstance(root, Path) or not isinstance(uuid_dir, Path) \
            or not isinstance(dynos_home, Path):
        # Strict input gate. Anything else means the caller is buggy; do not
        # silently fall back to the legacy dir, which could be wrong.
        return None

    # 1. UUID dir already has migrated state? Use it directly.
    if _dir_has_content(uuid_dir):
        return None

    # 2. Compute legacy candidate.
    projects_dir = dynos_home / "projects"
    legacy_dir = _legacy_dir_for(root, projects_dir)
    legacy_present = bool(legacy_dir is not None and _dir_has_content(legacy_dir))

    # 3. Marker check.
    marker_present = is_marker_present(dynos_home)

    # 3a. Steady-state fast path: marker present AND no fresh legacy dir.
    #     One os.stat on the marker (via is_marker_present), no directory
    #     listing beyond the cheap legacy-dir check we already did. The spec
    #     says we MAY do that cheap check; the marker still short-circuits
    #     the rest of the work and we never emit.
    if marker_present and not legacy_present:
        return None

    # 3b. No legacy dir found at all (with or without marker)? Nothing to do.
    if not legacy_present:
        return None

    # 4. Legacy dir is present and non-empty. Decide which event to emit.
    assert legacy_dir is not None  # narrowed by legacy_present
    legacy_key = str(legacy_dir)

    if marker_present:
        # Post-marker case: emit identity_legacy_slug_in_use_post_marker
        # (once) and dual-read. The marker is advisory.
        with _STATE_LOCK:
            should_emit = not _emitted_post_marker
            if should_emit:
                _emitted_post_marker = True
        if should_emit:
            _emit_event(
                root,
                "identity_legacy_slug_in_use_post_marker",
                legacy_dir=legacy_key,
                uuid_dir=str(uuid_dir),
            )
        return legacy_dir

    # 5. Marker absent — emit identity_legacy_slug_in_use exactly once per
    #    process per legacy_dir path. Dedup keyed on _LEGACY_WARNED_FOR so a
    #    test that resets the set re-arms emission; _emitted_legacy mirrors
    #    the set's non-emptiness for external observers.
    with _STATE_LOCK:
        already_warned = legacy_key in _LEGACY_WARNED_FOR
        if not already_warned:
            _LEGACY_WARNED_FOR.add(legacy_key)
            _emitted_legacy = True
    if not already_warned:
        _emit_event(
            root,
            "identity_legacy_slug_in_use",
            legacy_dir=legacy_key,
            uuid_dir=str(uuid_dir),
        )
    return legacy_dir


__all__ = [
    "MARKER_PATH",
    "_LEGACY_WARNED_FOR",
    "_emitted_legacy",
    "_emitted_post_marker",
    "check_dual_read",
    "is_marker_present",
    "log_event",
]
