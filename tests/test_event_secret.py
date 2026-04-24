"""Tests for AC 11 & AC 12: _resolve_event_secret four-branch resolution
and unconditional signing in log_event.

AC 11:
  (a) DYNOS_EVENT_SECRET env var non-empty → returned as-is, no file created
  (b) In-process cache keyed on str(root.resolve()) → cached value returned
  (c) Cache file with strict 0o600 perm check; 0o644 → ValueError
  (d) Derived secret written atomically, FileExistsError → re-read from file

AC 12:
  log_event signing is UNCONDITIONAL. Always calls _resolve_event_secret(root).
  If raises, print WARNING to stderr, write event WITHOUT _sig.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"

if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

lib_log = importlib.import_module("lib_log")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(tmp_path: Path) -> tuple[Path, Path, str]:
    """Build a project root with a task dir + manifest. Returns (root, task_dir, task_id)."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dynos").mkdir()
    task_id = "task-20260423-998"
    task_dir = root / ".dynos" / task_id
    task_dir.mkdir()
    (task_dir / "manifest.json").write_text(json.dumps({"task_id": task_id}))
    return root, task_dir, task_id


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# Fixture: isolate per-test cache + DYNOS_HOME + env secret
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_secret_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear _EVENT_SECRET_CACHE before every test to prevent cache bleed."""
    lib_log._EVENT_SECRET_CACHE.clear()
    monkeypatch.delenv("DYNOS_EVENT_SECRET", raising=False)
    yield
    lib_log._EVENT_SECRET_CACHE.clear()


# ---------------------------------------------------------------------------
# AC 11 (a): env var non-empty → returned immediately, no file created
# ---------------------------------------------------------------------------

def test_env_var_present_returned_no_file_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DYNOS_EVENT_SECRET set → _resolve_event_secret returns env value; no secret file created."""
    monkeypatch.setenv("DYNOS_EVENT_SECRET", "mysecret")
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))

    result = lib_log._resolve_event_secret(tmp_path)

    assert result == "mysecret", f"Expected 'mysecret', got {result!r}"

    # No file should be created in tmp_path or under the dynos-home
    for found in tmp_path.rglob("event-secret"):
        pytest.fail(f"No event-secret file should be created when env var is set; found {found}")


# ---------------------------------------------------------------------------
# AC 11 (d): no env var, no existing file → file is created at correct path,
#             mode 0o600, returned value is 32 hex chars
# ---------------------------------------------------------------------------

def test_no_env_no_file_creates_secret_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env var, no file → creates event-secret at persistent dir with mode 0o600; value is 32 hex chars."""
    dynos_home = tmp_path / "dynos-home"
    dynos_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    root = tmp_path / "project"
    root.mkdir()

    result = lib_log._resolve_event_secret(root)

    # Must be 32 hex chars
    assert len(result) == 32, f"Expected 32-char derived secret, got {len(result)!r}"
    int(result, 16)  # raises if not valid hex

    # File must exist somewhere under dynos-home
    secret_files = list(dynos_home.rglob("event-secret"))
    assert len(secret_files) == 1, (
        f"Expected exactly one event-secret file under dynos-home; found {secret_files!r}"
    )
    secret_file = secret_files[0]

    # File mode must be 0o600
    mode = os.stat(secret_file).st_mode & 0o777
    assert mode == 0o600, f"event-secret file must have mode 0o600, got {oct(mode)}"

    # File contents must match returned value
    assert secret_file.read_text(encoding="utf-8").strip() == result


# ---------------------------------------------------------------------------
# AC 11 (c): cache file exists with unsafe mode 0o644 → ValueError
# ---------------------------------------------------------------------------

def test_file_with_unsafe_perms_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """event-secret file with mode 0o644 → ValueError containing 'event-secret perms unsafe'."""
    dynos_home = tmp_path / "dynos-home"
    dynos_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    root = tmp_path / "project"
    root.mkdir()

    # Resolve persistent dir the same way the implementation does
    # (using the same env var we just set)
    import lib_core
    persistent_dir = lib_core._persistent_project_dir(root)
    persistent_dir.mkdir(parents=True, exist_ok=True)
    secret_file = persistent_dir / "event-secret"
    secret_file.write_text("badsecret", encoding="utf-8")
    os.chmod(secret_file, 0o644)

    with pytest.raises(ValueError) as exc_info:
        lib_log._resolve_event_secret(root)

    assert "event-secret perms unsafe" in str(exc_info.value), (
        f"ValueError message must contain 'event-secret perms unsafe'; got {exc_info.value!r}"
    )


# ---------------------------------------------------------------------------
# AC 11 (c): cache file exists with safe mode 0o600 → contents returned
# ---------------------------------------------------------------------------

def test_file_with_safe_perms_returns_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """event-secret file with mode 0o600 and known content → that content returned."""
    dynos_home = tmp_path / "dynos-home"
    dynos_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    root = tmp_path / "project"
    root.mkdir()

    import lib_core
    persistent_dir = lib_core._persistent_project_dir(root)
    persistent_dir.mkdir(parents=True, exist_ok=True)
    secret_file = persistent_dir / "event-secret"

    fd = os.open(str(secret_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, b"abc123")
    finally:
        os.close(fd)

    result = lib_log._resolve_event_secret(root)

    assert result == "abc123", f"Expected 'abc123', got {result!r}"


# ---------------------------------------------------------------------------
# AC 11 (b): second call returns cached value without re-reading file
# ---------------------------------------------------------------------------

def test_in_process_cache_returns_cached_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second call with same root hits in-process cache and returns same value."""
    dynos_home = tmp_path / "dynos-home"
    dynos_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    root = tmp_path / "project"
    root.mkdir()

    first = lib_log._resolve_event_secret(root)
    # Tamper with the file so a re-read would return different content
    import lib_core
    persistent_dir = lib_core._persistent_project_dir(root)
    secret_file = persistent_dir / "event-secret"
    # Overwrite the file with different content (permissions must stay 0o600)
    os.chmod(secret_file, 0o600)
    fd = os.open(str(secret_file), os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, b"tampered_value")
    finally:
        os.close(fd)

    second = lib_log._resolve_event_secret(root)

    assert second == first, (
        "Second call must return cached value, not re-read from tampered file; "
        f"first={first!r}, second={second!r}"
    )


# ---------------------------------------------------------------------------
# AC 11 (a) wins over (c): env var set, file exists → env value returned
# ---------------------------------------------------------------------------

def test_env_var_wins_over_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DYNOS_EVENT_SECRET set even when 0o600 file exists → env value returned, not file."""
    dynos_home = tmp_path / "dynos-home"
    dynos_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))
    monkeypatch.setenv("DYNOS_EVENT_SECRET", "env-wins")

    root = tmp_path / "project"
    root.mkdir()

    import lib_core
    persistent_dir = lib_core._persistent_project_dir(root)
    persistent_dir.mkdir(parents=True, exist_ok=True)
    secret_file = persistent_dir / "event-secret"
    fd = os.open(str(secret_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, b"file-secret")
    finally:
        os.close(fd)

    result = lib_log._resolve_event_secret(root)
    assert result == "env-wins", (
        f"Env var must win over existing file; got {result!r}"
    )


# ---------------------------------------------------------------------------
# AC 12: every event written by log_event has '_sig' key
# ---------------------------------------------------------------------------

def test_every_event_has_sig_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """log_event always writes _sig (via auto-derived secret when env unset)."""
    root, task_dir, task_id = _make_project(tmp_path)
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))

    lib_log.log_event(root, "test_event_a", task=task_id, key="val1")
    lib_log.log_event(root, "test_event_b", task=task_id, key="val2")

    events_path = task_dir / "events.jsonl"
    assert events_path.exists(), "events.jsonl must be created by log_event"
    records = _read_jsonl(events_path)
    assert len(records) == 2

    for rec in records:
        assert "_sig" in rec, (
            f"Every event must have '_sig' key; offending record: {rec!r}"
        )


# ---------------------------------------------------------------------------
# AC 12 degrade: _resolve_event_secret failure → event written WITHOUT _sig,
#                WARNING printed to stderr
# ---------------------------------------------------------------------------

def test_resolve_secret_failure_degrades_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """If _resolve_event_secret raises, log_event still writes the event and prints WARNING to stderr."""
    root, task_dir, task_id = _make_project(tmp_path)
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))

    def _always_raise(r: Path) -> str:
        raise ValueError("simulated secret failure")

    monkeypatch.setattr(lib_log, "_resolve_event_secret", _always_raise)

    lib_log.log_event(root, "degrade_test_event", task=task_id, key="value")

    # Event must be on disk
    events_path = task_dir / "events.jsonl"
    assert events_path.exists(), "events.jsonl must be written even when secret resolution fails"
    records = _read_jsonl(events_path)
    assert len(records) >= 1, "At least one event must be written despite secret failure"

    # Event must NOT have _sig
    assert "_sig" not in records[0], (
        "When secret resolution fails, the event must be written without _sig"
    )

    # stderr must contain WARNING
    captured = capsys.readouterr()
    stderr_text = captured.err
    assert "WARNING" in stderr_text or "_resolve_event_secret" in stderr_text, (
        f"stderr must contain WARNING or _resolve_event_secret; got: {stderr_text!r}"
    )


# ---------------------------------------------------------------------------
# AC 12 unconditional: no env var → auto-derived secret → _sig present
# ---------------------------------------------------------------------------

def test_signing_uses_resolved_secret_not_only_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When DYNOS_EVENT_SECRET is unset, log_event uses auto-derived secret and still attaches _sig."""
    root, task_dir, task_id = _make_project(tmp_path)
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    # Env var is already unset by the autouse fixture

    lib_log.log_event(root, "derived_secret_event", task=task_id)

    events_path = task_dir / "events.jsonl"
    assert events_path.exists()
    records = _read_jsonl(events_path)
    assert len(records) == 1

    assert "_sig" in records[0], (
        "log_event must attach _sig using the auto-derived secret even without DYNOS_EVENT_SECRET"
    )
    # _sig must be a 64-char hex string (HMAC-SHA256)
    sig = records[0]["_sig"]
    assert len(sig) == 64, f"_sig must be 64-char hex; got {len(sig)!r}"
    int(sig, 16)  # raises if not valid hex
