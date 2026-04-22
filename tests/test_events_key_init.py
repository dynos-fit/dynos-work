"""TDD-first tests for task-20260420-001 D3 part 1 — per-task HMAC key init.

Covers acceptance criteria 11 and 12:

    AC11 task init creates .dynos/task-{id}/.events-key:
         - single line, base64-encoded 32 random bytes
         - mode 0o600 applied via os.chmod
    AC12 if the key file cannot be written, task init fails strictly
         (RuntimeError / OSError propagates; no silent fallback).

Plus the implicit-requirement: belt-and-braces gitignore check for the key
path — `.events-key` MUST be gitignored (inherited through .dynos/).

TODAY these tests FAIL because hooks/lib_task_init.py does not exist.
"""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))

# This import MUST fail today (module does not exist). That is red.
from lib_task_init import generate_events_key, init_task_key_gate  # noqa: E402


def _make_task_dir(tmp_path: Path, task_id: str = "task-20260420-001") -> Path:
    proj = tmp_path / "project"
    td = proj / ".dynos" / task_id
    td.mkdir(parents=True)
    return td


# ---------------------------------------------------------------------------
# AC11 — key created at init, correct shape, correct mode
# ---------------------------------------------------------------------------


def test_init_task_writes_events_key(tmp_path: Path):
    td = _make_task_dir(tmp_path)
    init_task_key_gate(td)
    key_path = td / ".events-key"
    assert key_path.exists()
    mode = key_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected mode 0o600, got {oct(mode)}"


def test_init_task_key_is_base64_32_bytes(tmp_path: Path):
    td = _make_task_dir(tmp_path)
    init_task_key_gate(td)
    key_path = td / ".events-key"
    raw = key_path.read_text(encoding="utf-8").rstrip("\n")
    decoded = base64.b64decode(raw)
    assert len(decoded) == 32, f"expected 32 decoded bytes, got {len(decoded)}"
    assert len(raw) in (43, 44), (
        f"standard b64 of 32 bytes is 43-44 chars without padding, got {len(raw)}"
    )


def test_generate_events_key_is_idempotent_or_well_defined(tmp_path: Path):
    """Either: generate_events_key refuses to overwrite an existing key
    (belt-and-braces — a new key silently replacing an old one destroys
    verifiability of prior signed events), OR it produces identical output.

    At minimum, calling once MUST succeed."""
    td = _make_task_dir(tmp_path)
    generate_events_key(td)
    assert (td / ".events-key").exists()


# ---------------------------------------------------------------------------
# AC12 — strict failure when key cannot be written
# ---------------------------------------------------------------------------


def test_init_task_fails_strict_on_unwriteable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If the OS refuses to write the key file, task init MUST raise.
    Silent failure would recreate the signed-substrate-but-no-key hazard."""
    td = _make_task_dir(tmp_path)

    real_write_bytes = Path.write_bytes

    def _boom(self, *a, **kw):
        if self.name == ".events-key":
            raise OSError("disk full / permission denied simulation")
        return real_write_bytes(self, *a, **kw)

    monkeypatch.setattr(Path, "write_bytes", _boom, raising=True)

    real_write_text = Path.write_text

    def _boom_text(self, *a, **kw):
        if self.name == ".events-key":
            raise OSError("disk full / permission denied simulation")
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", _boom_text, raising=True)

    with pytest.raises((RuntimeError, OSError)):
        init_task_key_gate(td)

    # And no residual key file on failure.
    assert not (td / ".events-key").exists()


def test_init_task_fails_if_task_dir_missing(tmp_path: Path):
    """A missing task directory at init time should not silently create
    a weird partial state — either fail or require the dir upfront."""
    td = tmp_path / "project" / ".dynos" / "task-missing"
    with pytest.raises((RuntimeError, OSError, FileNotFoundError)):
        init_task_key_gate(td)


# ---------------------------------------------------------------------------
# Belt-and-braces: the key path is gitignored
# ---------------------------------------------------------------------------


def test_events_key_path_is_gitignored():
    """`.dynos/` is gitignored at repo root, therefore the key inherits.
    This is the belt-and-braces check ADR-10 pins."""
    candidate = REPO_ROOT / ".dynos" / "task-20260420-001" / ".events-key"
    # git check-ignore returns 0 when ignored.
    res = subprocess.run(
        ["git", "check-ignore", "-q", str(candidate)],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    assert res.returncode == 0, (
        f"{candidate} must be gitignored (got rc={res.returncode}); "
        f"a leaked signing key is a security regression"
    )
