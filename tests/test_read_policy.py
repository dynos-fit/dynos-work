"""Tests for read_policy.py — enforce auditor diff-scope read budget.

TDD-first: these tests are written before the production module exists.
Running `pytest tests/test_read_policy.py -x` will fail with ImportError
until hooks/read_policy.py is implemented. That is expected.

All 17 unit/integration tests correspond to named test functions in the
plan.md Test Strategy table and cover ACs 1-18 (AC 16 is covered in
test_write_policy_hook.py::test_non_audit_role_read_passthrough).

Tests import read_policy directly (no subprocess) except AC 13, 14, 15
which require subprocess hook invocation because they assert events emitted
by the pre_tool_use.py handler layer (not by decide_read).
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers shared by all tests
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "hooks" / "pre-tool-use"
HOOKS_DIR = ROOT / "hooks"


def _hook_available() -> bool:
    if not HOOK_PATH.exists():
        return False
    try:
        mode = HOOK_PATH.stat().st_mode
    except OSError:
        return False
    return bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def _read_policy_available() -> bool:
    """True when hooks/read_policy.py exists (production code landed)."""
    return (HOOKS_DIR / "read_policy.py").exists()


def _make_task_dir(tmp_path: Path, stage: str = "CHECKPOINT_AUDIT") -> tuple[Path, Path]:
    """Create a minimal project + task_dir structure with manifest.json."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".dynos").mkdir()
    task_dir = project_root / ".dynos" / "task-20260501-tst"
    task_dir.mkdir()
    (task_dir / "manifest.json").write_text(
        json.dumps({"task_id": task_dir.name, "stage": stage})
    )
    return project_root, task_dir


def _invoke_hook(
    *,
    payload: dict,
    env_extra: dict[str, str] | None = None,
    cwd: Path | None = None,
    drop_keys: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ}
    for key in drop_keys:
        env.pop(key, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(HOOK_PATH)],
        cwd=str(cwd or ROOT),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _import_read_policy():
    """Import read_policy from hooks/ directory, inserting it on sys.path."""
    import sys
    hooks_str = str(HOOKS_DIR)
    if hooks_str not in sys.path:
        sys.path.insert(0, hooks_str)
    import importlib
    return importlib.import_module("read_policy")


# ---------------------------------------------------------------------------
# pytestmark: skip the subprocess-based tests if the hook is not present yet
# ---------------------------------------------------------------------------
# NOTE: The unit tests (test_interface_exports etc.) use a different skip
# guard — they skip only when read_policy.py doesn't exist yet.
# The integration tests (AC 13, 14, 15) need the hook AND read_policy.py.

_SUBPROCESS_TESTS_AVAILABLE = _hook_available()

_hook_mark = pytest.mark.skipif(
    not _hook_available(),
    reason="hooks/pre-tool-use not present or not executable yet (TDD-first)",
)

_unit_mark = pytest.mark.skipif(
    not _read_policy_available(),
    reason="hooks/read_policy.py not yet implemented (TDD-first)",
)


# ===========================================================================
# AC 2: Interface exports — ReadAttempt, ReadDecision, decide_read,
#        _emit_read_policy_event with correct dataclass field shapes.
# ===========================================================================

@_unit_mark
def test_interface_exports() -> None:
    """AC 2: read_policy exports exactly the declared public symbols with correct shapes."""
    rp = _import_read_policy()

    # Required exports must exist
    assert hasattr(rp, "ReadAttempt"), "read_policy must export ReadAttempt"
    assert hasattr(rp, "ReadDecision"), "read_policy must export ReadDecision"
    assert hasattr(rp, "decide_read"), "read_policy must export decide_read"
    assert hasattr(rp, "_emit_read_policy_event"), "read_policy must export _emit_read_policy_event"

    # ReadAttempt must be a frozen dataclass
    ReadAttempt = rp.ReadAttempt
    assert dataclasses.is_dataclass(ReadAttempt), "ReadAttempt must be a dataclass"
    ra_fields = {f.name for f in dataclasses.fields(ReadAttempt)}
    assert ra_fields == {"role", "task_dir", "target", "tool_name", "raw_pattern"}, (
        f"ReadAttempt fields mismatch: got {ra_fields}"
    )
    # Must be frozen
    instance = ReadAttempt(
        role="audit-security",
        task_dir=None,
        target=Path("/tmp"),
        tool_name="Read",
        raw_pattern=None,
    )
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        instance.role = "mutated"  # type: ignore[misc]

    # ReadDecision must be a frozen dataclass
    ReadDecision = rp.ReadDecision
    assert dataclasses.is_dataclass(ReadDecision), "ReadDecision must be a dataclass"
    rd_fields = {f.name for f in dataclasses.fields(ReadDecision)}
    assert rd_fields == {"allowed", "reason", "quota_used"}, (
        f"ReadDecision fields mismatch: got {rd_fields}"
    )
    decision = ReadDecision(allowed=True, reason="ok", quota_used=None)
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        decision.allowed = False  # type: ignore[misc]

    # decide_read must be callable with (attempt, *, audit_plan)
    sig = inspect.signature(rp.decide_read)
    params = list(sig.parameters.keys())
    assert "attempt" in params, "decide_read must have 'attempt' parameter"
    assert "audit_plan" in params, "decide_read must have 'audit_plan' keyword parameter"
    audit_plan_param = sig.parameters["audit_plan"]
    assert audit_plan_param.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ), "audit_plan should be a keyword parameter"

    # No unexpected public symbols (only the 4 declared + private helpers + constants)
    public_names = {
        name for name in dir(rp)
        if not name.startswith("_") and not name.startswith("__")
    }
    # These are the only permitted public names
    permitted_public = {"ReadAttempt", "ReadDecision", "decide_read"}
    unexpected = public_names - permitted_public
    # Filter out stdlib re-exports (dataclasses, Path, etc.) that may appear
    # if the module does `from x import y` at module level
    stdlib_reexports = {
        name for name in unexpected
        if name[0].islower() and name not in ("decide_read",)
    }
    # The spec says "No additional public symbols" — we allow lowercase helpers
    # that are clearly stdlib re-exports but disallow any extra capitalized names
    extra_capitalized = {n for n in unexpected if n[0].isupper()} - {"Path"}
    assert not extra_capitalized, (
        f"read_policy has unexpected public capitalized symbols: {extra_capitalized}"
    )


# ===========================================================================
# AC 1: "audit-claude-md" in _EXECUTOR_ROLE_ALLOWLIST
# ===========================================================================

@_unit_mark
def test_audit_claude_md_role_in_allowlist() -> None:
    """AC 1: pre_tool_use._EXECUTOR_ROLE_ALLOWLIST contains 'audit-claude-md'."""
    import sys
    hooks_str = str(HOOKS_DIR)
    if hooks_str not in sys.path:
        sys.path.insert(0, hooks_str)
    import importlib
    ptu = importlib.import_module("pre_tool_use")
    allowlist = ptu._EXECUTOR_ROLE_ALLOWLIST
    assert "audit-claude-md" in allowlist, (
        f"'audit-claude-md' missing from _EXECUTOR_ROLE_ALLOWLIST; "
        f"current allowlist: {sorted(allowlist)}"
    )


# ===========================================================================
# AC 4: audit-* role + task_dir read is allowed
# ===========================================================================

@_unit_mark
def test_audit_role_task_dir_read_allowed(tmp_path: Path) -> None:
    """AC 4: Read of a file inside task_dir is allowed for any audit-* role."""
    rp = _import_read_policy()
    _, task_dir = _make_task_dir(tmp_path)

    # Create a real file inside task_dir for the target
    target_file = task_dir / "audit-reports" / "findings.json"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("{}")

    attempt = rp.ReadAttempt(
        role="audit-security",
        task_dir=task_dir,
        target=target_file.resolve(),
        tool_name="Read",
        raw_pattern=None,
    )
    decision = rp.decide_read(attempt, audit_plan=None)

    assert decision.allowed is True, (
        f"Expected allowed=True for task_dir read, got: "
        f"allowed={decision.allowed}, reason={decision.reason!r}"
    )


# ===========================================================================
# AC 5: audit-* role + diff-scoped file read is allowed
# ===========================================================================

@_unit_mark
def test_audit_role_diff_scoped_read_allowed(tmp_path: Path) -> None:
    """AC 5: Read of a diff-scoped file is allowed; diff_files entries are repo-relative."""
    rp = _import_read_policy()
    project_root, task_dir = _make_task_dir(tmp_path)

    # Create the actual source file at repo root / src/foo.py
    src_file = project_root / "src" / "foo.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("# production code\n")

    # audit_plan uses repo-relative paths; they must resolve against task_dir.parent.parent
    audit_plan = {"diff_files": ["src/foo.py"]}

    attempt = rp.ReadAttempt(
        role="audit-security",
        task_dir=task_dir,
        target=src_file.resolve(),
        tool_name="Read",
        raw_pattern=None,
    )
    decision = rp.decide_read(attempt, audit_plan=audit_plan)

    assert decision.allowed is True, (
        f"Expected allowed=True for diff-scoped read, got: "
        f"allowed={decision.allowed}, reason={decision.reason!r}"
    )


# ===========================================================================
# AC 6: audit-* role + path outside allowlist is denied
# ===========================================================================

@_unit_mark
def test_audit_role_outside_allowlist_denied(tmp_path: Path) -> None:
    """AC 6: Read of a path outside task_dir and diff_files is denied."""
    rp = _import_read_policy()
    _, task_dir = _make_task_dir(tmp_path)

    # Target is completely outside the task_dir and not in diff_files
    outside_path = Path("/etc/passwd").resolve()

    attempt = rp.ReadAttempt(
        role="audit-security",
        task_dir=task_dir,
        target=outside_path,
        tool_name="Read",
        raw_pattern=None,
    )
    decision = rp.decide_read(attempt, audit_plan=None)

    assert decision.allowed is False, (
        f"Expected allowed=False for out-of-allowlist path, got: "
        f"allowed={decision.allowed}, reason={decision.reason!r}"
    )
    assert decision.reason, "reason must be non-empty on denial"
    assert decision.quota_used is None, (
        f"quota_used must be None for non-dead-code deny, got: {decision.quota_used}"
    )


# ===========================================================================
# AC 7: audit-dead-code — first 10 repo-wide Greps are allowed
# ===========================================================================

@_unit_mark
def test_dead_code_quota_10_allowed(tmp_path: Path) -> None:
    """AC 7: First 10 repo-wide Greps from audit-dead-code are allowed; quota file increments."""
    rp = _import_read_policy()
    _, task_dir = _make_task_dir(tmp_path)

    # Grep path is the repo root (outside task_dir, not in diff_files)
    # This simulates a repo-wide grep
    repo_root = task_dir.parent.parent

    quota_file = task_dir / "audit-grep-quota.json"

    for i in range(1, 11):
        attempt = rp.ReadAttempt(
            role="audit-dead-code",
            task_dir=task_dir,
            target=repo_root.resolve(),
            tool_name="Grep",
            raw_pattern="some_symbol",
        )
        decision = rp.decide_read(attempt, audit_plan=None)

        assert decision.allowed is True, (
            f"Expected allowed=True on call {i}/10, got: "
            f"allowed={decision.allowed}, reason={decision.reason!r}"
        )
        assert decision.quota_used == i, (
            f"Expected quota_used={i} on call {i}, got: {decision.quota_used}"
        )

    # After 10 calls, quota file must contain {"count": 10}
    assert quota_file.exists(), "audit-grep-quota.json must exist after 10 quota-consuming calls"
    quota_data = json.loads(quota_file.read_text())
    assert quota_data == {"count": 10}, (
        f"Expected quota file to contain {{\"count\": 10}}, got: {quota_data}"
    )


# ===========================================================================
# AC 8: audit-dead-code — 11th repo-wide Grep is denied
# ===========================================================================

@_unit_mark
def test_dead_code_quota_11th_denied(tmp_path: Path) -> None:
    """AC 8: 11th repo-wide Grep from audit-dead-code is denied with exact reason; quota unchanged."""
    rp = _import_read_policy()
    _, task_dir = _make_task_dir(tmp_path)

    # Pre-write exhausted quota file
    quota_file = task_dir / "audit-grep-quota.json"
    quota_file.write_text(json.dumps({"count": 10}))

    repo_root = task_dir.parent.parent

    attempt = rp.ReadAttempt(
        role="audit-dead-code",
        task_dir=task_dir,
        target=repo_root.resolve(),
        tool_name="Grep",
        raw_pattern="some_symbol",
    )
    decision = rp.decide_read(attempt, audit_plan=None)

    assert decision.allowed is False, (
        f"Expected allowed=False on 11th call, got: "
        f"allowed={decision.allowed}, reason={decision.reason!r}"
    )
    assert decision.reason == "read-policy: dead-code grep quota exhausted (10/10)", (
        f"Exact reason string mismatch; got: {decision.reason!r}"
    )
    assert decision.quota_used is None, (
        f"quota_used must be None on denial, got: {decision.quota_used}"
    )

    # Quota file must NOT be mutated on denial
    quota_after = json.loads(quota_file.read_text())
    assert quota_after == {"count": 10}, (
        f"Quota file must not be mutated on denial; got: {quota_after}"
    )


# ===========================================================================
# AC 9: quota file unreadable → fail-closed
# ===========================================================================

@_unit_mark
def test_dead_code_quota_unreadable_fail_closed(tmp_path: Path) -> None:
    """AC 9: Unreadable quota file → fail-closed denial; no exception propagates."""
    rp = _import_read_policy()
    _, task_dir = _make_task_dir(tmp_path)

    quota_file = task_dir / "audit-grep-quota.json"
    # Write corrupt JSON — this simulates unreadable/corrupt state
    quota_file.write_bytes(b"not valid json {{{")

    repo_root = task_dir.parent.parent

    attempt = rp.ReadAttempt(
        role="audit-dead-code",
        task_dir=task_dir,
        target=repo_root.resolve(),
        tool_name="Grep",
        raw_pattern="some_symbol",
    )

    # Must not raise — all exceptions are swallowed inside decide_read
    decision = rp.decide_read(attempt, audit_plan=None)

    assert decision.allowed is False, (
        f"Expected fail-closed denial on unreadable quota, got: "
        f"allowed={decision.allowed}, reason={decision.reason!r}"
    )
    assert decision.reason == "read-policy: quota state unreadable; denying repo-wide grep", (
        f"Exact reason string mismatch for unreadable quota; got: {decision.reason!r}"
    )
    assert decision.quota_used is None, (
        f"quota_used must be None on fail-closed denial, got: {decision.quota_used}"
    )


# ===========================================================================
# AC 10: audit-claude-md reading any */CLAUDE.md is allowed; carve-out
#          does NOT apply to other audit roles
# ===========================================================================

@_unit_mark
def test_claude_md_auditor_carveout_allowed(tmp_path: Path) -> None:
    """AC 10: audit-claude-md may read any CLAUDE.md; other audit roles cannot."""
    rp = _import_read_policy()
    project_root, task_dir = _make_task_dir(tmp_path)

    # Three different CLAUDE.md locations per the spec
    paths_to_test = [
        project_root / "CLAUDE.md",                     # repo root
        project_root / "foo" / "CLAUDE.md",             # subdirectory
        Path.home() / ".claude" / "CLAUDE.md",          # ~/.claude/CLAUDE.md
    ]

    for claude_path in paths_to_test:
        attempt = rp.ReadAttempt(
            role="audit-claude-md",
            task_dir=task_dir,
            target=claude_path.resolve(),
            tool_name="Read",
            raw_pattern=None,
        )
        decision = rp.decide_read(attempt, audit_plan=None)
        assert decision.allowed is True, (
            f"Expected audit-claude-md to be allowed to read {claude_path}; "
            f"got allowed={decision.allowed}, reason={decision.reason!r}"
        )

    # Carve-out must NOT apply to other audit roles
    for claude_path in paths_to_test:
        attempt_other = rp.ReadAttempt(
            role="audit-security",
            task_dir=task_dir,
            target=claude_path.resolve(),
            tool_name="Read",
            raw_pattern=None,
        )
        decision_other = rp.decide_read(attempt_other, audit_plan=None)
        # audit-security has no CLAUDE.md carve-out and the paths are outside task_dir
        # so this must be denied (these paths are not in task_dir and not in diff_files)
        assert decision_other.allowed is False, (
            f"audit-security must NOT have CLAUDE.md carve-out for {claude_path}; "
            f"got allowed={decision_other.allowed}, reason={decision_other.reason!r}"
        )


# ===========================================================================
# AC 11: Glob pattern **/*.py for audit-* is denied
# ===========================================================================

@_unit_mark
def test_audit_glob_wildcard_denied(tmp_path: Path) -> None:
    """AC 11: Glob pattern '**/*.py' for audit-* is denied; pattern field checked (not path)."""
    rp = _import_read_policy()
    _, task_dir = _make_task_dir(tmp_path)

    # The target for Glob is os.path.normpath of the pattern, not a real file path
    import os as _os
    pattern = "**/*.py"
    target = Path(_os.path.normpath(pattern))

    attempt = rp.ReadAttempt(
        role="audit-security",
        task_dir=task_dir,
        target=target,
        tool_name="Glob",
        raw_pattern=pattern,
    )
    decision = rp.decide_read(attempt, audit_plan=None)

    assert decision.allowed is False, (
        f"Expected Glob '**/*.py' to be denied for audit-security; "
        f"got allowed={decision.allowed}, reason={decision.reason!r}"
    )


# ===========================================================================
# AC 12: Glob pattern rooted in task_dir is allowed
# ===========================================================================

@_unit_mark
def test_audit_glob_task_dir_prefix_allowed(tmp_path: Path) -> None:
    """AC 12: Glob pattern starting with task_dir prefix is allowed."""
    rp = _import_read_policy()
    _, task_dir = _make_task_dir(tmp_path)

    import os as _os
    pattern = f"{task_dir}/audit-reports/*.json"
    target = Path(_os.path.normpath(pattern))

    attempt = rp.ReadAttempt(
        role="audit-security",
        task_dir=task_dir,
        target=target,
        tool_name="Glob",
        raw_pattern=pattern,
    )
    decision = rp.decide_read(attempt, audit_plan=None)

    assert decision.allowed is True, (
        f"Expected Glob rooted in task_dir to be allowed; "
        f"got allowed={decision.allowed}, reason={decision.reason!r}"
    )


# ===========================================================================
# AC 13: missing audit-plan.json → task_dir-only fallback + event emitted
# AC 13 tests the pre_tool_use.py handler via subprocess
# ===========================================================================

@_hook_mark
def test_missing_audit_plan_fallback_task_dir_only(tmp_path: Path) -> None:
    """AC 13: Missing audit-plan.json → pre_tool_use_audit_plan_missing event emitted;
    diff-scoped read denied because audit_plan=None means no diff-scoped allowlist."""
    project_root, task_dir = _make_task_dir(tmp_path, stage="CHECKPOINT_AUDIT")

    # No audit-plan.json — deliberately absent
    assert not (task_dir / "audit-plan.json").exists()

    # A diff-scoped file at repo root level (outside task_dir)
    diff_file = project_root / "src" / "service.py"
    diff_file.parent.mkdir(parents=True, exist_ok=True)
    diff_file.write_text("# service code\n")

    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(diff_file)},
        "cwd": str(project_root),
    }
    env = {
        "DYNOS_ROLE": "audit-security",
        "DYNOS_TASK_DIR": str(task_dir),
    }

    result = _invoke_hook(payload=payload, env_extra=env, cwd=project_root)

    # Should be denied (no diff-scoped allowlist without audit-plan.json)
    assert result.returncode == 2, (
        f"Expected exit 2 (denied) when audit-plan.json is missing; "
        f"got {result.returncode}; stderr={result.stderr!r}"
    )

    # pre_tool_use_audit_plan_missing event must be in events.jsonl
    events_path = task_dir / "events.jsonl"
    assert events_path.exists(), f"events.jsonl must exist; looked at {events_path}"
    events_text = events_path.read_text()
    assert "pre_tool_use_audit_plan_missing" in events_text, (
        f"Expected 'pre_tool_use_audit_plan_missing' event in events.jsonl; "
        f"content: {events_text!r}"
    )
    # Must NOT contain the "invalid" event (it's missing, not malformed)
    assert "pre_tool_use_audit_plan_invalid" not in events_text, (
        f"Must not emit 'pre_tool_use_audit_plan_invalid' for a missing file; "
        f"content: {events_text!r}"
    )


# ===========================================================================
# AC 14: malformed audit-plan.json → same fallback + distinct event
# AC 14 tests the pre_tool_use.py handler via subprocess
# ===========================================================================

@_hook_mark
def test_malformed_audit_plan_fallback_distinct_event(tmp_path: Path) -> None:
    """AC 14: Malformed audit-plan.json → pre_tool_use_audit_plan_invalid event (distinct from missing)."""
    project_root, task_dir = _make_task_dir(tmp_path, stage="CHECKPOINT_AUDIT")

    # audit-plan.json exists but is malformed (missing diff_files key)
    (task_dir / "audit-plan.json").write_text(json.dumps({"not_diff_files": []}))

    diff_file = project_root / "src" / "service.py"
    diff_file.parent.mkdir(parents=True, exist_ok=True)
    diff_file.write_text("# service code\n")

    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(diff_file)},
        "cwd": str(project_root),
    }
    env = {
        "DYNOS_ROLE": "audit-security",
        "DYNOS_TASK_DIR": str(task_dir),
    }

    result = _invoke_hook(payload=payload, env_extra=env, cwd=project_root)

    assert result.returncode == 2, (
        f"Expected exit 2 (denied) when audit-plan.json is malformed; "
        f"got {result.returncode}; stderr={result.stderr!r}"
    )

    events_path = task_dir / "events.jsonl"
    assert events_path.exists(), f"events.jsonl must exist; looked at {events_path}"
    events_text = events_path.read_text()

    # Must emit the INVALID event (not the missing event)
    assert "pre_tool_use_audit_plan_invalid" in events_text, (
        f"Expected 'pre_tool_use_audit_plan_invalid' event in events.jsonl; "
        f"content: {events_text!r}"
    )
    # The missing event must NOT appear (file existed, just malformed)
    assert "pre_tool_use_audit_plan_missing" not in events_text, (
        f"Must not emit 'pre_tool_use_audit_plan_missing' when file exists but is malformed; "
        f"content: {events_text!r}"
    )


# ===========================================================================
# AC 15: stage guard — EXECUTION stage denies audit-* Read/Grep/Glob
# AC 15 tests the pre_tool_use.py handler via subprocess
# ===========================================================================

@_hook_mark
def test_stage_guard_execution_denied(tmp_path: Path) -> None:
    """AC 15: manifest.json stage=EXECUTION → audit-* Read is denied; stderr contains 'not permitted at stage'."""
    project_root, task_dir = _make_task_dir(tmp_path, stage="EXECUTION")

    # Write an audit-plan.json so we don't hit the missing-plan fallback first
    (task_dir / "audit-plan.json").write_text(
        json.dumps({"diff_files": []})
    )

    target_file = task_dir / "audit-reports" / "anything.json"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("{}")

    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target_file)},
        "cwd": str(project_root),
    }
    env = {
        "DYNOS_ROLE": "audit-security",
        "DYNOS_TASK_DIR": str(task_dir),
    }

    result = _invoke_hook(payload=payload, env_extra=env, cwd=project_root)

    assert result.returncode == 2, (
        f"Expected exit 2 when manifest stage=EXECUTION for audit role; "
        f"got {result.returncode}; stderr={result.stderr!r}"
    )
    assert "not permitted at stage" in result.stderr, (
        f"Expected stderr to contain 'not permitted at stage'; "
        f"stderr={result.stderr!r}"
    )


# ===========================================================================
# AC 17: path traversal resolves outside allowlist → denied
# ===========================================================================

@_unit_mark
def test_path_traversal_denied(tmp_path: Path) -> None:
    """AC 17: Path traversal via '..' resolves outside allowlist; deny uses resolved path."""
    rp = _import_read_policy()
    project_root, task_dir = _make_task_dir(tmp_path)

    # Simulate what pre_tool_use._resolve_path would produce for "../../etc/passwd"
    # when cwd is task_dir. The result escapes far above the project root.
    traversal_raw = "../../etc/passwd"
    # Resolve from task_dir as cwd (mimicking _resolve_path behavior)
    traversal_resolved = (task_dir / traversal_raw).resolve()

    attempt = rp.ReadAttempt(
        role="audit-security",
        task_dir=task_dir,
        target=traversal_resolved,
        tool_name="Read",
        raw_pattern=None,
    )
    decision = rp.decide_read(attempt, audit_plan=None)

    assert decision.allowed is False, (
        f"Expected path traversal to be denied after resolve(); "
        f"resolved target={traversal_resolved}, "
        f"allowed={decision.allowed}, reason={decision.reason!r}"
    )
    # Verify the resolved path actually escapes task_dir (sanity check)
    assert not str(traversal_resolved).startswith(str(task_dir)), (
        f"Test setup error: traversal target {traversal_resolved} should be outside task_dir {task_dir}"
    )


# ===========================================================================
# AC 18: symlink inside task_dir pointing outside → denied after resolve
# ===========================================================================

@_unit_mark
def test_symlink_outside_allowlist_denied(tmp_path: Path) -> None:
    """AC 18: Symlink inside task_dir pointing outside resolves via Path.resolve() → denied."""
    rp = _import_read_policy()
    _, task_dir = _make_task_dir(tmp_path)

    # Create a symlink inside task_dir pointing to an external path
    symlink_path = task_dir / "evil_link"

    # Point to /etc/passwd (or any path we know is outside task_dir)
    # On test machines /etc/passwd always exists; if not, use /tmp
    external_target = Path("/etc/passwd") if Path("/etc/passwd").exists() else Path("/tmp")

    try:
        symlink_path.symlink_to(external_target)
    except (OSError, NotImplementedError):
        pytest.skip("Cannot create symlinks on this system")

    # decide_read receives the resolved path (after Path.resolve() removes the symlink)
    resolved_target = symlink_path.resolve()

    attempt = rp.ReadAttempt(
        role="audit-security",
        task_dir=task_dir,
        target=resolved_target,
        tool_name="Read",
        raw_pattern=None,
    )
    decision = rp.decide_read(attempt, audit_plan=None)

    assert decision.allowed is False, (
        f"Expected symlink pointing outside task_dir to be denied; "
        f"symlink={symlink_path}, resolved={resolved_target}, "
        f"allowed={decision.allowed}, reason={decision.reason!r}"
    )
    # Verify the resolved path is truly outside task_dir (sanity check)
    assert not str(resolved_target).startswith(str(task_dir.resolve())), (
        f"Test setup error: resolved path {resolved_target} should be outside task_dir {task_dir}"
    )


# ===========================================================================
# AC 19: existing write enforcement is unchanged
# ===========================================================================

@_unit_mark
def test_existing_write_policy_unchanged() -> None:
    """AC 19: decide_write still works with same WriteAttempt shape after read_policy is added."""
    # Import write_policy directly to verify it is unmodified
    import sys
    hooks_str = str(HOOKS_DIR)
    if hooks_str not in sys.path:
        sys.path.insert(0, hooks_str)
    import importlib
    wp = importlib.import_module("write_policy")

    # Smoke-test: executor writing a repo file is allowed
    attempt = wp.WriteAttempt(
        role="backend-executor",
        task_dir=None,
        path=Path("/tmp/some_repo_file.py"),
        operation="modify",
        source="agent",
    )
    decision = wp.decide_write(attempt)
    assert decision.allowed is True, (
        f"decide_write broken: executor writing repo file should be allowed; "
        f"got allowed={decision.allowed}, reason={decision.reason!r}"
    )

    # Smoke-test: executor writing manifest.json is denied
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        task_dir = Path(td) / ".dynos" / "task-smoke"
        task_dir.mkdir(parents=True)
        manifest = task_dir / "manifest.json"
        manifest.write_text("{}")

        attempt_denied = wp.WriteAttempt(
            role="backend-executor",
            task_dir=task_dir,
            path=manifest,
            operation="modify",
            source="agent",
        )
        decision_denied = wp.decide_write(attempt_denied)
        assert decision_denied.allowed is False, (
            f"decide_write broken: executor writing manifest.json should be denied; "
            f"got allowed={decision_denied.allowed}, reason={decision_denied.reason!r}"
        )


# ===========================================================================
# Additional regression guard: non-audit role passes through decide_read
# This is tested at the unit level to verify the fast-exit branch in decide_read.
# The integration test for AC 16 is in test_write_policy_hook.py.
# ===========================================================================

@_unit_mark
def test_non_audit_role_decide_read_passthrough(tmp_path: Path) -> None:
    """AC 16 (unit): Non-audit role returns allowed=True without consulting allowlist."""
    rp = _import_read_policy()
    _, task_dir = _make_task_dir(tmp_path)

    # Deliberately use an out-of-bounds path to confirm the allowlist is not checked
    outside_path = Path("/etc/passwd").resolve()

    attempt = rp.ReadAttempt(
        role="backend-executor",
        task_dir=task_dir,
        target=outside_path,
        tool_name="Read",
        raw_pattern=None,
    )
    decision = rp.decide_read(attempt, audit_plan=None)

    assert decision.allowed is True, (
        f"Expected non-audit role to always get allowed=True from decide_read; "
        f"got allowed={decision.allowed}, reason={decision.reason!r}"
    )


# ---------------------------------------------------------------------------
# TDD-first regression — task-20260615-002 — AC 8
# ---------------------------------------------------------------------------


def test_quota_tempfile_cleaned_up_on_replace_failure(tmp_path: Path) -> None:
    """AC 8: When os.replace raises OSError, the .tmp file must be unlinked and
    ReadDecision.allowed must be False (fail-closed).

    References _handle_dead_code_grep_quota INSIDE the function body so that
    collection never errors if the fix isn't yet implemented.
    """
    import sys
    import os
    import importlib
    from unittest.mock import patch

    hooks_str = str(ROOT / "hooks")
    if hooks_str not in sys.path:
        sys.path.insert(0, hooks_str)
    rp = importlib.import_module("read_policy")

    fn = getattr(rp, "_handle_dead_code_grep_quota", None)
    assert fn is not None, "_handle_dead_code_grep_quota must exist in read_policy"

    _, task_dir = _make_task_dir(tmp_path)

    attempt = rp.ReadAttempt(
        role="audit-dead-code",
        task_dir=task_dir,
        target=tmp_path / "hooks" / "some_module.py",
        tool_name="Grep",
        raw_pattern=None,
    )

    # Capture the tmp_path that _handle_dead_code_grep_quota creates so we can
    # assert it was cleaned up. We intercept os.replace to raise after the
    # NamedTemporaryFile has been created and closed (delete=False).
    created_tmp_files: list[str] = []
    original_replace = os.replace

    def _raising_replace(src: str, dst: str) -> None:
        created_tmp_files.append(src)
        raise OSError("injected failure for test")

    with patch.object(rp.os, "replace", side_effect=_raising_replace):
        decision = fn(attempt)

    assert decision.allowed is False, (
        f"ReadDecision must be fail-closed (allowed=False) when os.replace raises; "
        f"got {decision.allowed!r}"
    )
    assert len(created_tmp_files) > 0, (
        "os.replace must have been called (i.e. the NamedTemporaryFile was created)"
    )
    tmp_file_path = created_tmp_files[0]
    assert not os.path.exists(tmp_file_path), (
        f"The .tmp file {tmp_file_path!r} must be unlinked after os.replace failure; "
        "it still exists, indicating the cleanup fix is missing"
    )
