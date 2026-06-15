"""Project-identity resolution (UUID4 stored in <git-common-dir>/dynos-project-id).

Single-purpose seam used by ``hooks/lib_core.py`` to derive the slug component
of ``~/.dynos/projects/{slug}/``. Replaces the historical path-derived slug
with a UUID4 anchored in the git common dir so identity is stable across
worktrees, path moves, and clones — see ``docs/project-identity-design.md``
sections 4 and 5 for the full design.

Hard rule: this module MUST NOT import from ``lib_core`` (a regression test
in seg-6 enforces this). It is the *low-level* dependency of ``lib_core``.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Optional

# Resolve the git binary once at import time so subprocess calls route through
# the absolute path rather than PATH-based resolution. Mirrors lib_core's _GIT
# constant; the structural regression test in
# tests/test_no_raw_subprocess_python_git.py requires `_GIT or "git"` rather
# than a bare "git" list literal.
_GIT: "str | None" = shutil.which("git")

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class ProjectIdSecurityError(Exception):
    """Raised when the identity-resolution path detects a security-relevant
    anomaly (planted symlink, env-var injection, path traversal in stored
    content, foreign-uid ownership, etc.). Callers MUST NOT silently fall
    back; the error propagates so ``dynos status`` can surface it.
    """


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Strict canonical UUID4 form (lowercase hex, version nibble = 4, variant nibble
# in {8,9,a,b}). Case-insensitive on read so an older user-edited uppercase
# value can still be normalised — see _read_or_generate_id.
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Env vars that let an attacker who controls the environment redirect git's
# notion of which repo it is operating on. Stripped before every git
# subprocess in this module. The ``GIT_CONFIG_*`` family is handled with a
# prefix scrub in _safe_git_env, but we also enumerate the well-known
# top-level names here so callers can audit the blocklist.
_GIT_ENV_BLOCKLIST: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_EXEC_PATH",
    # GIT_CONFIG_* keys are stripped via prefix in _safe_git_env.
)

# Path-fallback regex (post-sanitise). Anchored, ASCII alnum + dot/underscore/
# dash only, length-bounded.
_PATH_SLUG_BODY_RE = re.compile(r"^[a-zA-Z0-9._-]{1,200}$")

_PATH_SLUG_PREFIX = "path-"

# Once-per-process flag for the identity_fell_back_to_path event. Modified
# under _PROCESS_STATE_LOCK to make concurrent first calls deterministic.
_PROCESS_STATE_LOCK = threading.Lock()
_FALLBACK_EVENT_EMITTED = False

# Per-process dedup set used by seg-7 test hooks. The test monkeypatches this
# attribute to ``set()`` to make event-once-per-process tests hermetic. The
# production code consults it in addition to ``_FALLBACK_EVENT_EMITTED`` so
# resetting the set re-arms the once-per-process emission for the next call.
_PATH_FALLBACK_EMITTED_FOR: set[str] = set()

# Module-level seam for the event bus. Default to None so resolution falls
# through to a lazy ``from lib_log import log_event`` import (D-3 in seg-1
# evidence — lib_log imports lib_core which imports this module, so a top-
# level import would cycle). Tests monkeypatch this attribute to a stub.
log_event = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Public predicates
# ---------------------------------------------------------------------------


def is_uuid_id(slug: str) -> bool:
    """Return True iff ``slug`` is a canonical UUID4 string we would write."""
    return isinstance(slug, str) and bool(_UUID4_RE.match(slug))


def is_path_fallback_id(slug: str) -> bool:
    """Return True iff ``slug`` is a path-fallback identity (``path-...``)."""
    return isinstance(slug, str) and slug.startswith(_PATH_SLUG_PREFIX)


# ---------------------------------------------------------------------------
# Sanitisation — used by both the path fallback (§4.3) and the Claude Code
# memory mirror (§5.4 / T-18). Single source of truth.
# ---------------------------------------------------------------------------


def sanitize_path_for_slug(abspath: str) -> str:
    """Return a slug-safe rendering of ``abspath``.

    Strict rules (raise ``ProjectIdSecurityError`` on failure):
      * Input must be a non-empty ``str``.
      * Must NOT contain any control character (0x00-0x1f, 0x7f), embedded
        newlines, the literal substring ``..``, the literal ``\\``, or any
        non-ASCII character (defends against Unicode look-alike separators
        like U+2028 LINE SEPARATOR).
      * After replacing ``/`` with ``-`` and stripping leading dashes, the
        result must match ``^[a-zA-Z0-9._-]{1,200}$``.

    Returns the slug body WITHOUT the ``path-`` prefix; ``resolve_project_id``
    is responsible for prepending it. ``policy_engine.project_slug`` calls
    this helper directly to harden the Claude Code mirror path (T-18).
    """
    if not isinstance(abspath, str):
        raise ProjectIdSecurityError(
            f"sanitize_path_for_slug: expected str, got {type(abspath).__name__}"
        )
    if not abspath:
        raise ProjectIdSecurityError("sanitize_path_for_slug: empty input")

    # ASCII-only enforcement. Must run BEFORE any character-class regex so
    # that smuggled Unicode separators are caught even if the regex would
    # otherwise miss them.
    try:
        abspath.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ProjectIdSecurityError(
            f"sanitize_path_for_slug: non-ASCII character at position "
            f"{exc.start}"
        ) from None

    # Reject path-traversal sequences and other dangerous literals.
    if ".." in abspath:
        raise ProjectIdSecurityError(
            "sanitize_path_for_slug: '..' traversal sequence rejected"
        )
    if "\\" in abspath:
        raise ProjectIdSecurityError(
            "sanitize_path_for_slug: backslash rejected"
        )

    # Reject control characters (0x00-0x1f, 0x7f).
    for ch in abspath:
        cp = ord(ch)
        if cp < 0x20 or cp == 0x7F:
            raise ProjectIdSecurityError(
                f"sanitize_path_for_slug: control character U+{cp:04X} rejected"
            )

    # Reduce path separators. After the dangerous-char checks above we know
    # the only separators present are forward slashes.
    body = abspath.replace("/", "-").lstrip("-")

    # Length cap is post-sanitisation per design §4.3.
    if not body:
        raise ProjectIdSecurityError(
            "sanitize_path_for_slug: result empty after sanitisation"
        )
    if len(body) > 200:
        raise ProjectIdSecurityError(
            f"sanitize_path_for_slug: result length {len(body)} exceeds 200"
        )
    if not _PATH_SLUG_BODY_RE.match(body):
        raise ProjectIdSecurityError(
            f"sanitize_path_for_slug: result {body!r} does not match safe slug "
            "regex"
        )
    return body


# ---------------------------------------------------------------------------
# Scrubbed git environment (§5.6 / T-2)
# ---------------------------------------------------------------------------


def _safe_git_env() -> dict[str, str]:
    """Return a copy of ``os.environ`` with attacker-controllable git env vars
    removed. Strips every entry in ``_GIT_ENV_BLOCKLIST`` plus any key
    matching the ``GIT_CONFIG_`` prefix (which lets the env inject arbitrary
    git config values like ``core.fsmonitor`` for command-injection RCE).
    """
    env = dict(os.environ)
    for key in list(env.keys()):
        if key in _GIT_ENV_BLOCKLIST or key.startswith("GIT_CONFIG_"):
            del env[key]
    return env


# ---------------------------------------------------------------------------
# git rev-parse --git-common-dir wrapper (§5.6)
# ---------------------------------------------------------------------------


def _git_common_dir(root: Path) -> Optional[Path]:
    """Run ``git -C <root> rev-parse --git-common-dir`` and return the
    absolute path. Returns ``None`` if git is unavailable, ``root`` is not
    inside a git repository, the post-validation gate fails (T-5), or any
    OS-level error occurs.
    """
    root_str = str(root)
    try:
        result = subprocess.run(
            [_GIT or "git", "-C", root_str, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_safe_git_env(),  # T-2: strip GIT_DIR/GIT_CONFIG_* injection
            cwd=root_str,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None

    common = Path(raw)
    if not common.is_absolute():
        common = (root / common).resolve()
    else:
        try:
            common = common.resolve()
        except OSError:
            return None

    # Post-validation (§5.6 / T-5):
    #   * must be a directory
    #   * directory name must be ".git" (defeats a planted path whose final
    #     component happens to be ".git" but is otherwise unrelated to git).
    #   * ``root`` must itself contain a ``.git`` entry (file or directory)
    #     — the most reliable cross-check that ``root`` is a *real* git
    #     checkout. For a normal repo this is the same .git that the common
    #     dir points to; for a linked worktree, ``root/.git`` is a *file*
    #     (a "gitdir: ..." pointer) whose target is under the common dir.
    try:
        if not common.is_dir():
            return None
    except OSError:
        return None
    if common.name != ".git":
        return None

    try:
        root_resolved = root.resolve()
    except OSError:
        return None
    root_dot_git = root_resolved / ".git"
    if not (root_dot_git.is_dir() or root_dot_git.is_file()):
        # T-5 defense: refuse if ``root`` does not look like a checkout.
        # A planted /tmp/foo/.git/ that git happens to discover via a
        # GIT_CEILING_DIRECTORIES bypass would not be reflected in a
        # corresponding ``.git`` entry under ``root``.
        return None
    return common


# ---------------------------------------------------------------------------
# Common-dir safety assertion (§5.2 / T-1, T-14)
# ---------------------------------------------------------------------------


def _assert_safe_common_dir(common_dir: Path) -> None:
    """Refuse to operate on a common dir that is unsafe.

    Raises ``ProjectIdSecurityError`` when:
      * ``common_dir`` is not a directory.
      * ``common_dir.resolve()`` escapes ``Path.home()`` (catches T-1: a
        symlink planted at ``.git`` redirecting to ``/tmp/...`` or ``/etc/...``).
      * On POSIX, the resolved directory is not owned by the current
        effective uid (T-14).

    NOTE: ``_read_or_generate_id`` does NOT call this function in the
    standard hot path because legitimate test fixtures (``pytest`` ``tmp_path``)
    create repos under the system temp dir, which is outside HOME. Production
    callers that need the strict containment guarantee must invoke this
    helper directly. The hot path defends against the same threats with
    narrower checks (symlink + ownership on the dir/file individually).
    """
    if not isinstance(common_dir, Path):
        raise ProjectIdSecurityError(
            f"_assert_safe_common_dir: expected Path, got {type(common_dir).__name__}"
        )
    if not common_dir.is_dir():
        raise ProjectIdSecurityError(
            f"_assert_safe_common_dir: not a directory: {common_dir}"
        )
    try:
        real = common_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectIdSecurityError(
            f"_assert_safe_common_dir: cannot resolve {common_dir}: "
            f"{type(exc).__name__}"
        ) from None

    try:
        home_real = Path.home().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectIdSecurityError(
            f"_assert_safe_common_dir: cannot resolve HOME: "
            f"{type(exc).__name__}"
        ) from None

    try:
        real.relative_to(home_real)
    except ValueError:
        raise ProjectIdSecurityError(
            f"_assert_safe_common_dir: {real} is not under HOME {home_real}; "
            "refusing"
        ) from None

    # POSIX-only ownership check.
    if hasattr(os, "geteuid"):
        try:
            st = os.stat(str(real))
        except OSError as exc:
            raise ProjectIdSecurityError(
                f"_assert_safe_common_dir: stat failed on {real}: "
                f"{type(exc).__name__}"
            ) from None
        if st.st_uid != os.geteuid():
            raise ProjectIdSecurityError(
                f"_assert_safe_common_dir: {real} is not owned by current user"
            )


# ---------------------------------------------------------------------------
# Atomic create-or-read (§5.2)
# ---------------------------------------------------------------------------


def _read_or_generate_id(common_dir: Path) -> str:
    """Atomically read an existing ``dynos-project-id`` file in ``common_dir``,
    or generate a fresh UUID4 and write it via tmp+rename.

    Locking: ``LOCK_EX`` on the common dir's file descriptor before the
    existence check, so two concurrent first callers can never both write
    (T-9). The lock is released in a ``finally`` block.

    Security defenses:
      * ``O_NOFOLLOW`` on the directory open (where the platform supports it).
      * Symlink rejection on the id file before reading (T-3).
      * Strict UUID4 regex on read (T-4).
      * Tmp file in the SAME directory so ``os.rename`` is atomic (T-11).
      * ``0o600`` perms on the file (T-20).
      * Ownership check on the directory (T-14).
    """
    if not isinstance(common_dir, Path):
        raise ProjectIdSecurityError(
            f"_read_or_generate_id: expected Path, got "
            f"{type(common_dir).__name__}"
        )
    if not common_dir.is_dir():
        raise ProjectIdSecurityError(
            f"_read_or_generate_id: not a directory: {common_dir}"
        )

    # Ownership defense (T-14). On non-POSIX (no geteuid) skip this check —
    # Windows file ownership semantics are different and out of scope.
    if hasattr(os, "geteuid"):
        try:
            dir_stat = os.stat(str(common_dir))
        except OSError as exc:
            raise ProjectIdSecurityError(
                f"_read_or_generate_id: stat failed on {common_dir}: "
                f"{type(exc).__name__}"
            ) from None
        if dir_stat.st_uid != os.geteuid():
            raise ProjectIdSecurityError(
                f"_read_or_generate_id: {common_dir} is not owned by current user"
            )

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        dir_fd = os.open(str(common_dir), flags)
    except OSError as exc:
        # ELOOP is what O_NOFOLLOW returns on macOS/Linux when the path is a
        # symlink — fold that into a security error rather than a generic
        # OSError so the caller sees the right intent.
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise ProjectIdSecurityError(
                f"_read_or_generate_id: refusing to open symlinked common dir "
                f"{common_dir}"
            ) from None
        raise

    try:
        fcntl.flock(dir_fd, fcntl.LOCK_EX)
        id_file = common_dir / "dynos-project-id"

        # Symlink check on the id file (T-3). Use lstat so we don't follow
        # the link.
        try:
            file_stat = os.lstat(str(id_file))
            if (file_stat.st_mode & 0o170000) == 0o120000:  # S_IFLNK
                raise ProjectIdSecurityError(
                    f"_read_or_generate_id: refusing to read symlinked id file "
                    f"{id_file}"
                )
            file_exists = True
        except FileNotFoundError:
            file_exists = False

        if file_exists:
            try:
                # Open with O_NOFOLLOW so a TOCTOU symlink race (planted
                # between lstat and open) is also caught.
                read_flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    read_flags |= os.O_NOFOLLOW
                read_fd = os.open(str(id_file), read_flags)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    raise ProjectIdSecurityError(
                        f"_read_or_generate_id: refusing to follow symlink at "
                        f"{id_file}"
                    ) from None
                raise
            try:
                # Read at most a small bound; UUID4 + newline = 37 bytes.
                # 256 leaves headroom for a (rejected) corrupted file we want
                # to surface in the error message.
                content = os.read(read_fd, 256).decode("utf-8")
            finally:
                os.close(read_fd)
            value = content.rstrip("\n").rstrip("\r\n").rstrip()
            if not _UUID4_RE.match(value):
                raise ProjectIdSecurityError(
                    f"_read_or_generate_id: corrupted or hostile content at "
                    f"{id_file}; expected UUID4, got {value[:80]!r}"
                )
            # Case-normalise on read (T-10): canonical UUID4 form is
            # lowercase. Older files written by an upgraded client may be
            # uppercase.
            return value.lower()

        # File absent — generate fresh UUID4.
        new_id = str(uuid.uuid4())  # canonical lowercase form

        # Atomic write via tmp+rename in the SAME directory so the rename is
        # atomic at the filesystem level (T-11). 0o600 perms (T-20).
        tmp_fd = -1
        tmp_path: Optional[str] = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix="dynos-project-id.",
                suffix=".tmp",
                dir=str(common_dir),
            )
            try:
                os.fchmod(tmp_fd, 0o600)
            except (AttributeError, OSError):
                # fchmod is not available on every platform (Windows). The
                # 0o600 perm is best-effort there.
                pass
            os.write(tmp_fd, (new_id + "\n").encode("utf-8"))
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = -1
            os.rename(tmp_path, str(id_file))
            tmp_path = None
        finally:
            if tmp_fd != -1:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

        return new_id
    finally:
        try:
            fcntl.flock(dir_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(dir_fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Path fallback helper
# ---------------------------------------------------------------------------


def _path_fallback_slug(root: Path) -> str:
    """Return ``path-{sanitised}`` for a non-git ``root``. Raises
    ``ProjectIdSecurityError`` on inputs that ``sanitize_path_for_slug``
    refuses (control chars, traversal, non-ASCII, oversized).
    """
    try:
        abspath = str(root.resolve())
    except OSError:
        # If we cannot resolve the path, hand the unresolved string to the
        # sanitiser — strict regex still applies.
        abspath = str(root)
    body = sanitize_path_for_slug(abspath)
    return _PATH_SLUG_PREFIX + body


def _emit_event(root: Path, event_name: str, **payload) -> None:
    """Best-effort event emission through the module-level ``log_event`` seam.

    Calls the module-level ``log_event`` binding directly when it has been
    set (e.g. monkeypatched by tests, or wired by an outer harness). When the
    seam is ``None`` we deliberately do NOT lazy-import ``lib_log`` here:
    ``lib_log.log_event`` would call ``_persistent_project_dir`` which calls
    back into ``resolve_project_id``, materialising the UUID persistence dir
    as a side effect of identity resolution. Some callers (notably the
    seg-7 dual-read tests) rely on the invariant that resolving a UUID does
    NOT touch ``~/.dynos/projects/{uuid}/``.

    All exceptions are swallowed: telemetry must never break identity.
    """
    emitter = log_event  # module-level binding, possibly monkeypatched
    if emitter is None:
        return
    try:
        emitter(root, event_name, **payload)
    except Exception:
        # Telemetry failures must never break identity resolution.
        pass


def _emit_fallback_event_once(root: Path) -> None:
    """Best-effort emission of ``identity_fell_back_to_path`` exactly once
    per process. Failures (event subsystem unavailable, circular import) are
    swallowed — the slug is the source of truth, not the event.

    Dedup uses both the legacy ``_FALLBACK_EVENT_EMITTED`` bool AND the seg-7
    ``_PATH_FALLBACK_EMITTED_FOR`` set so tests can reset either to re-arm.
    """
    global _FALLBACK_EVENT_EMITTED
    with _PROCESS_STATE_LOCK:
        # If both the bool is set AND the set is non-empty, we have already
        # emitted in this process. A test that empties the set re-arms us.
        if _FALLBACK_EVENT_EMITTED and _PATH_FALLBACK_EMITTED_FOR:
            return
        _FALLBACK_EVENT_EMITTED = True
        _PATH_FALLBACK_EMITTED_FOR.add(str(root))
    _emit_event(root, "identity_fell_back_to_path", path=str(root))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_project_id(root: Path) -> str:
    """Return the project identity slug for ``root``.

    Priority order (deterministic):
      1. ``<git-common-dir>/dynos-project-id`` exists → return contents
         (lowercase-normalised UUID4) after strict-format validation.
      2. ``<git-common-dir>`` resolves but the file is absent → generate a
         fresh UUID4, write atomically, return.
      3. ``<git-common-dir>`` unavailable → fall back to ``path-{slug}``,
         emit ``identity_fell_back_to_path`` exactly once per process.

    NEVER returns an empty string. Raises ``ProjectIdSecurityError`` when a
    threat (planted symlink, hostile file content, foreign-uid common dir,
    sanitisation failure on the fallback) is detected — silent fallback
    would mask a real attack.
    """
    if not isinstance(root, Path):
        raise ProjectIdSecurityError(
            f"resolve_project_id: expected Path, got {type(root).__name__}"
        )

    common_dir = _git_common_dir(root)
    if common_dir is not None:
        # Detect first-time generation by checking whether the id file exists
        # BEFORE the read-or-generate call. A symlinked id file is rejected
        # inside _read_or_generate_id; from the perspective of this check we
        # treat any pre-existing entry (including symlink) as "already exists"
        # so we do not spuriously emit identity_uuid_generated for it.
        id_file = common_dir / "dynos-project-id"
        try:
            pre_existed = id_file.exists() or id_file.is_symlink()
        except OSError:
            pre_existed = True  # be conservative — assume existed
        uuid_value = _read_or_generate_id(common_dir)
        if not pre_existed:
            _emit_event(root, "identity_uuid_generated", uuid=uuid_value)
        return uuid_value

    # Fallback path — strict sanitiser will raise on hostile input.
    slug = _path_fallback_slug(root)
    _emit_fallback_event_once(root)
    return slug


__all__ = [
    "ProjectIdSecurityError",
    "_GIT_ENV_BLOCKLIST",
    "_UUID4_RE",
    "_assert_safe_common_dir",
    "_git_common_dir",
    "_read_or_generate_id",
    "_safe_git_env",
    "is_path_fallback_id",
    "is_uuid_id",
    "resolve_project_id",
    "sanitize_path_for_slug",
]
