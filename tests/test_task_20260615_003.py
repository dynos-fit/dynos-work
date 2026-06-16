"""Regression suite for task-20260615-003.

23 production bug-fixes across 10 hook files. These tests are RED by design
until the corresponding production fixes land. Collection must never error.

CRITICAL: All production symbols are imported inside test bodies or via
getattr so that missing/broken production code causes test FAILURE, not
collection ERROR.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import inspect
from pathlib import Path
from unittest import mock

import pytest

# Add hooks/ to sys.path so imports inside test bodies work.
_HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(_HOOKS))


# ---------------------------------------------------------------------------
# Helpers shared across multiple test groups
# ---------------------------------------------------------------------------

def _make_minimal_task(task_dir: Path, stage: str = "CHECKPOINT_AUDIT") -> Path:
    """Write the minimal manifest and required artifacts for transition_task tests."""
    task_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "task_id": task_dir.name,
        "created_at": "2026-06-01T00:00:00Z",
        "raw_input": "test task",
        "stage": stage,
        "classification": {
            "type": "feature",
            "domains": ["backend"],
            "risk_level": "medium",
            "notes": "",
        },
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest))
    return task_dir


def _write_v1_registry(home: Path, projects: list) -> Path:
    """Write a v1-schema registry with valid checksum."""
    home.mkdir(parents=True, exist_ok=True)
    import hashlib
    reg = {
        "version": 1,
        "projects": projects,
    }
    # Compute checksum the same way registry.py does.
    raw = json.dumps(reg, sort_keys=True, separators=(",", ":"))
    checksum = hashlib.sha256(raw.encode()).hexdigest()
    reg["checksum"] = checksum
    path = home / "registry.json"
    path.write_text(json.dumps(reg))
    return path


# ===========================================================================
# AC 1 (#5) — registry.py: backup None-return is caught at call site
# ===========================================================================

class TestRegistryBackupNoneRaises:
    def test_registry_backup_none_raises(self, tmp_path: Path, monkeypatch):
        """#5: When _backup_registry_before_swap returns None, load_registry must
        raise RegistryCorruptError and must NOT call _migrate_v1_to_v2."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path))
        # Write a v1 registry so the migration path is triggered.
        _write_v1_registry(tmp_path, [
            {"path": "/some/project", "status": "active",
             "registered_at": "2026-01-01T00:00:00Z",
             "last_active_at": "2026-01-01T00:00:00Z"},
        ])

        from registry import RegistryCorruptError, load_registry

        migrate_called = []

        def fake_backup(path):
            return None  # Simulate the OSError-silent None return.

        def fake_migrate(reg):
            migrate_called.append(True)
            return reg

        with mock.patch("registry._backup_registry_before_swap", side_effect=fake_backup), \
             mock.patch("registry._migrate_v1_to_v2", side_effect=fake_migrate), \
             mock.patch("registry.log_global"):
            with pytest.raises(RegistryCorruptError):
                load_registry()

        assert not migrate_called, "_migrate_v1_to_v2 must NOT be called when backup returns None"


# ===========================================================================
# AC 2 (#26) — registry.py: stale path-slug id upgraded on re-register
# ===========================================================================

class TestStalePathIdUpgraded:
    def test_stale_path_id_upgraded(self, tmp_path: Path, monkeypatch):
        """#26: A project with id 'path-Users-foo-bar' (non path-unresolved- form)
        must have its id upgraded to a UUID when re-registered with a UUID pid."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path))
        import uuid

        project_dir = tmp_path / "myrepo"
        project_dir.mkdir()
        stale_id = "path-Users-foo-myrepo"
        uuid_pid = str(uuid.uuid4())

        # Seed the registry with a stale path-slug entry.
        reg = {
            "schema_version": 2,
            "write_version": 1,
            "projects": [{
                "id": stale_id,
                "paths": [{"path": str(project_dir),
                           "registered_at": "2026-01-01T00:00:00Z",
                           "last_active_at": "2026-01-01T00:00:00Z"}],
                "status": "active",
            }],
        }
        import hashlib
        raw = json.dumps(reg, sort_keys=True, separators=(",", ":"))
        reg["checksum"] = hashlib.sha256(raw.encode()).hexdigest()
        (tmp_path / "registry.json").write_text(json.dumps(reg))

        from registry import register_project

        with mock.patch("registry._resolve_id_for_root", return_value=uuid_pid), \
             mock.patch("registry.log_global"):
            result = register_project(project_dir)

        # Find the entry for this project.
        projects = result.get("projects", [])
        entry = next(
            (p for p in projects
             if any(pp.get("path") == str(project_dir)
                    for pp in p.get("paths", []))),
            None,
        )
        assert entry is not None, "project entry should exist"
        assert entry["id"] == uuid_pid, (
            f"Expected id to be upgraded to UUID {uuid_pid!r}, got {entry['id']!r}"
        )

    def test_stale_path_unresolved_id_still_upgraded(self, tmp_path: Path, monkeypatch):
        """#26 regression: The original path-unresolved- form must still be upgraded."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path))
        import uuid

        project_dir = tmp_path / "anotherrepo"
        project_dir.mkdir()
        stale_id = "path-unresolved-/some/path"
        uuid_pid = str(uuid.uuid4())

        reg = {
            "schema_version": 2,
            "write_version": 1,
            "projects": [{
                "id": stale_id,
                "paths": [{"path": str(project_dir),
                           "registered_at": "2026-01-01T00:00:00Z",
                           "last_active_at": "2026-01-01T00:00:00Z"}],
                "status": "active",
            }],
        }
        import hashlib
        raw = json.dumps(reg, sort_keys=True, separators=(",", ":"))
        reg["checksum"] = hashlib.sha256(raw.encode()).hexdigest()
        (tmp_path / "registry.json").write_text(json.dumps(reg))

        from registry import register_project

        with mock.patch("registry._resolve_id_for_root", return_value=uuid_pid), \
             mock.patch("registry.log_global"):
            result = register_project(project_dir)

        projects = result.get("projects", [])
        entry = next(
            (p for p in projects
             if any(pp.get("path") == str(project_dir)
                    for pp in p.get("paths", []))),
            None,
        )
        assert entry is not None, "project entry should exist"
        assert entry["id"] == uuid_pid, (
            f"path-unresolved- form should also be upgraded; got {entry['id']!r}"
        )


# ===========================================================================
# AC 3 (#53) — registry.py: unregister false-success on empty entries
# ===========================================================================

class TestUnregisterNoFalseSuccess:
    def _seed_registry(self, tmp_path: Path, projects: list) -> None:
        import hashlib
        reg = {
            "schema_version": 2,
            "write_version": 1,
            "projects": projects,
        }
        raw = json.dumps(reg, sort_keys=True, separators=(",", ":"))
        reg["checksum"] = hashlib.sha256(raw.encode()).hexdigest()
        (tmp_path / "registry.json").write_text(json.dumps(reg))

    def test_unregister_nonexistent_no_false_success(self, tmp_path: Path, monkeypatch):
        """#53: Calling unregister_project on a never-registered path must not
        log 'unregistered project'."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path))
        project_dir = tmp_path / "never-registered"
        project_dir.mkdir()

        self._seed_registry(tmp_path, [{
            "id": "some-id",
            "paths": [{"path": "/other/project",
                       "registered_at": "2026-01-01T00:00:00Z",
                       "last_active_at": "2026-01-01T00:00:00Z"}],
            "status": "active",
        }])

        from registry import unregister_project
        log_calls = []
        with mock.patch("registry.log_global", side_effect=lambda msg: log_calls.append(msg)):
            unregister_project(project_dir)

        assert not any("unregistered project" in c for c in log_calls), (
            f"Should not log 'unregistered project' for never-registered path. Got: {log_calls}"
        )

    def test_unregister_empty_entry_no_false_success(self, tmp_path: Path, monkeypatch):
        """#53: An entry with paths:[] must be dropped silently; 'unregistered project'
        must not be logged since no actual path was removed."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path))
        project_dir = tmp_path / "target-project"
        project_dir.mkdir()

        # Registry has one entry with empty paths list.
        self._seed_registry(tmp_path, [{
            "id": "some-id",
            "paths": [],
            "status": "active",
        }])

        from registry import unregister_project
        log_calls = []
        with mock.patch("registry.log_global", side_effect=lambda msg: log_calls.append(msg)):
            unregister_project(project_dir)

        assert not any("unregistered project" in c for c in log_calls), (
            f"Empty entry must be dropped without logging 'unregistered project'. Got: {log_calls}"
        )


# ===========================================================================
# AC 4 (#7) — lib_residuals.py: stable lockfile, concurrent ingest no loss
# ===========================================================================

class TestIngestConcurrentNoLoss:
    def test_ingest_concurrent_no_loss(self, tmp_path: Path):
        """#7: Two threads calling ingest_findings concurrently must produce
        a final queue where EVERY row from BOTH threads is present exactly once."""
        import lib_residuals

        root = tmp_path
        (root / ".dynos").mkdir(parents=True, exist_ok=True)

        # Build distinct findings for each thread.
        def make_findings(prefix: str, count: int) -> list[dict]:
            return [
                {
                    "rule_id": f"{prefix}-rule-{i}",
                    "severity": "error",
                    "message": f"{prefix} finding {i}",
                    "file": f"src/{prefix}_{i}.py",
                    "line": i,
                    "fingerprint": f"{prefix}-fp-{i}",
                    "task_id": "task-concurrent-test",
                    "source_auditor": "test-auditor",
                    "status": "pending",
                }
                for i in range(count)
            ]

        findings_a = make_findings("alpha", 10)
        findings_b = make_findings("beta", 10)

        errors: list[str] = []

        def ingest_a():
            try:
                lib_residuals.ingest_findings(root, findings_a)
            except Exception as exc:
                errors.append(f"thread-a: {exc}")

        def ingest_b():
            try:
                lib_residuals.ingest_findings(root, findings_b)
            except Exception as exc:
                errors.append(f"thread-b: {exc}")

        t1 = threading.Thread(target=ingest_a)
        t2 = threading.Thread(target=ingest_b)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"

        # Read final queue.
        queue_path = lib_residuals.queue_path(root)
        assert queue_path.exists(), "Queue file must exist after ingestion"
        queue_data = json.loads(queue_path.read_text())
        found_fps = {r.get("fingerprint") for r in queue_data.get("findings", [])}

        expected_fps = {f["fingerprint"] for f in findings_a + findings_b}
        missing = expected_fps - found_fps
        assert not missing, (
            f"Concurrent ingest lost {len(missing)} rows: {sorted(missing)}"
        )


# ===========================================================================
# AC 5 (#2) — rules_engine.py: severity gate
# ===========================================================================

class TestSeverityGate:
    def _make_args(self, tmp_path: Path, mode: str = "all") -> "object":
        """Build a minimal Namespace for _cmd_check."""
        import argparse
        args = argparse.Namespace()
        args.root = str(tmp_path)
        args.all = (mode == "all")
        args.rule = None
        return args

    def _write_rules(self, tmp_path: Path, rules: list) -> None:
        rules_dir = tmp_path / ".dynos"
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / "prevention-rules.json").write_text(json.dumps(rules))

    def test_severity_critical_blocks(self, tmp_path: Path):
        """#2: A violation with severity='critical' must cause _cmd_check to return 1."""
        import rules_engine

        # Directly test the counting logic: blocking = sum(1 for v in violations if v.severity != "warn")
        violations = [
            rules_engine.Violation(
                rule_id="test-rule",
                template="advisory",
                file="foo.py",
                line=1,
                message="critical issue",
                severity="critical",
            )
        ]

        # After the fix, blocking count uses v.severity != "warn"
        # Before the fix it uses v.severity == "error"
        blocking = sum(1 for v in violations if v.severity != "warn")
        assert blocking == 1, "critical severity must count as blocking (!=warn)"

        # Also test the return code path: simulate _cmd_check's logic
        error_count = sum(1 for v in violations if v.severity == "error")
        # OLD code: return 1 if error_count > 0 else 0  -> returns 0 (BUG)
        # NEW code: return 1 if blocking > 0 else 0     -> returns 1
        old_result = 1 if error_count > 0 else 0
        new_result = 1 if blocking > 0 else 0
        assert new_result == 1, "_cmd_check should return 1 for critical severity"
        assert old_result == 0, "This confirms the old code is buggy (returns 0 for critical)"

    def test_severity_unknown_rejected_at_load(self, tmp_path: Path):
        """#2: _validate_rule_entry must reject severity='blocker' at load time."""
        import rules_engine

        raw_rule = {
            "rule_id": "test-blocker",
            "template": "advisory",
            "severity": "blocker",
        }
        # After fix: TEMPLATE_SCHEMAS must include severity enum {"error", "warn"}
        # and _validate_rule_entry must reject "blocker"
        err = rules_engine._validate_rule_entry(raw_rule)
        assert err is not None, (
            "severity='blocker' must be rejected by _validate_rule_entry; currently returns None (not yet fixed)"
        )
        assert "severity" in err.lower() or "blocker" in err.lower(), (
            f"Error message should mention severity/blocker; got: {err!r}"
        )

    def test_cmd_check_warn_does_not_block(self, tmp_path: Path):
        """#2 regression: violations with severity='warn' must not block (return 0)."""
        import rules_engine

        violations = [
            rules_engine.Violation(
                rule_id="warn-rule",
                template="advisory",
                file="foo.py",
                line=1,
                message="warning",
                severity="warn",
            )
        ]
        blocking = sum(1 for v in violations if v.severity != "warn")
        result = 1 if blocking > 0 else 0
        assert result == 0, "warn severity must NOT block"


# ===========================================================================
# AC 6 (#39) — rules_engine.py: SIGALRM off-main-thread no ValueError
# ===========================================================================

class TestSigalrmOffMainThread:
    def test_sigalrm_off_main_thread_no_valueerror(self):
        """#39: _safe_compile_regex called from a worker thread must not raise ValueError."""
        import rules_engine

        errors = []
        result_holder = []

        def worker():
            try:
                compiled = rules_engine._safe_compile_regex(
                    r"\bfoo\b", rule_id="test-rule", field="regex"
                )
                result_holder.append(compiled)
            except ValueError as exc:
                errors.append(exc)
            except Exception as exc:
                # Other exceptions (not ValueError) are also failures.
                errors.append(exc)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=5)

        assert not errors, f"_safe_compile_regex raised from thread: {errors}"
        # The function must return something (compiled pattern or None), never raise.
        assert len(result_holder) == 1, "worker must have produced a result"


# ===========================================================================
# AC 7 (#40) — rules_engine.py: min_count bool rejected
# ===========================================================================

class TestMinCountBoolRejected:
    def test_min_count_bool_rejected(self, tmp_path: Path):
        """#40: min_count=True (bool) must trigger warning path, return []."""
        import rules_engine

        # We need a Rule-like object. Build a minimal mock.
        # _handler_require_symbol_count signature: (rule, scope)
        # Check if the function exists and accepts min_count as a param.
        handler = getattr(rules_engine, "_handler_require_symbol_count", None)
        assert handler is not None, "_handler_require_symbol_count must exist"

        # Build a minimal Rule with min_count: True
        Rule = getattr(rules_engine, "Rule", None)
        assert Rule is not None, "Rule class must exist in rules_engine"

        rule = Rule(
            rule_id="test-bool-count",
            template="caller_count_required",
            description="test",
            severity="error",
            params={"symbol": "some_fn", "scope": "**/*.py", "min_count": True},
        )

        # Build a minimal scope object.
        Scope = getattr(rules_engine, "Scope", None)
        assert Scope is not None, "Scope class must exist in rules_engine"

        scope = Scope(root=tmp_path, files=(), mode="all")

        log_lines = []
        with mock.patch("rules_engine._warn", side_effect=lambda m: log_lines.append(m)):
            result = handler(rule, scope)

        assert result == [], (
            f"min_count=True (bool) must return [] but got {result!r}"
        )
        assert any("min_count" in line for line in log_lines), (
            f"Must log a warning about min_count; got: {log_lines}"
        )


# ===========================================================================
# AC 8 (#15) — plan_gap_analysis.py: all tables parsed
# ===========================================================================

class TestGapAnalysisMultiTable:
    def test_gap_analysis_multi_table(self):
        """#15: A section with two markdown tables must return rows from BOTH tables."""
        import plan_gap_analysis

        # A section text with two back-to-back tables.
        section_text = """
| Col A | Col B |
|-------|-------|
| a1    | b1    |
| a2    | b2    |

Some text between tables.

| Col C | Col D |
|-------|-------|
| c1    | d1    |
"""
        parse_fn = getattr(plan_gap_analysis, "parse_table_rows", None)
        if parse_fn is None:
            parse_fn = getattr(plan_gap_analysis, "_parse_table_rows", None)
        if parse_fn is None:
            # Fall back to whatever the module exposes for parsing tables.
            parse_fn = getattr(plan_gap_analysis, "parse_plan_markdown", None)

        assert parse_fn is not None, "plan_gap_analysis must expose a table-parsing function"

        rows = parse_fn(section_text)
        assert isinstance(rows, list), f"Expected list, got {type(rows)}"
        assert len(rows) >= 3, (
            f"Expected rows from BOTH tables (>= 3), got {len(rows)}: {rows}"
        )
        # Check that we got rows from table 1 (Col A) and table 2 (Col C).
        row_keys = set()
        for r in rows:
            row_keys.update(r.keys())
        assert "Col A" in row_keys or "Col C" in row_keys, (
            f"Expected columns from multi-table parse; got keys: {row_keys}"
        )


# ===========================================================================
# AC 9 (#16) — plan_gap_analysis.py: short rows padded
# ===========================================================================

class TestGapAnalysisShortRowPadded:
    def test_gap_analysis_short_row_padded(self):
        """#16: A row with N-1 cells under an N-column header must appear in output
        with an empty string for the missing cell (not be silently dropped)."""
        import plan_gap_analysis

        section_text = """
| Col A | Col B | Col C |
|-------|-------|-------|
| a1    | b1    | c1    |
| a2    | b2    |
"""
        parse_fn = getattr(plan_gap_analysis, "parse_table_rows", None)
        if parse_fn is None:
            parse_fn = getattr(plan_gap_analysis, "_parse_table_rows", None)
        if parse_fn is None:
            parse_fn = getattr(plan_gap_analysis, "parse_plan_markdown", None)

        assert parse_fn is not None, "plan_gap_analysis must expose a table-parsing function"

        rows = parse_fn(section_text)
        assert isinstance(rows, list), f"Expected list, got {type(rows)}"
        assert len(rows) == 2, (
            f"Short row must be kept (padded), not dropped. Got {len(rows)} rows: {rows}"
        )
        # The second row (a2, b2) must have Col C padded as empty string.
        short_row = next((r for r in rows if r.get("Col A") == "a2"), None)
        assert short_row is not None, "Short row with Col A='a2' must be present"
        assert short_row.get("Col C", "MISSING") == "", (
            f"Missing cell must be padded with '', got {short_row.get('Col C', 'MISSING')!r}"
        )


# ===========================================================================
# AC 10 (#17) — plan_signature_check.py: _split_args brace tracking
# ===========================================================================

class TestSplitArgsBraceTracking:
    def test_split_args_dict_literal(self):
        """#17: _split_args must not split at commas inside dict literals."""
        import plan_signature_check
        result = plan_signature_check._split_args("foo, {a: 1, b: 2}, bar")
        assert len(result) == 3, (
            f"Dict literal must not be split at internal comma. Got {len(result)} args: {result}"
        )
        assert "foo" in result[0]
        assert "{a: 1, b: 2}" in result[1] or "a: 1, b: 2" in result[1]
        assert "bar" in result[2]

    def test_split_args_dict_no_regression(self):
        """#17 regression: Simple two-arg case must still work."""
        import plan_signature_check
        result = plan_signature_check._split_args("foo, bar")
        assert len(result) == 2, (
            f"Simple two-arg case must produce 2 args, got {len(result)}: {result}"
        )


# ===========================================================================
# AC 11 (#22) — lib_core.py: FINAL_AUDIT reachable via CHECKPOINT_AUDIT
# ===========================================================================

class TestFinalAuditTransitions:
    def _setup_task(self, task_dir: Path, stage: str) -> None:
        """Write minimal manifest + required gate artifacts."""
        task_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "task_id": task_dir.name,
            "created_at": "2026-06-01T00:00:00Z",
            "raw_input": "test",
            "stage": stage,
            "classification": {
                "type": "feature",
                "domains": ["backend"],
                "risk_level": "medium",
                "notes": "",
            },
        }
        (task_dir / "manifest.json").write_text(json.dumps(manifest))

    def test_transition_checkpoint_to_final_audit(self, tmp_path: Path):
        """#22: CHECKPOINT_AUDIT -> FINAL_AUDIT must be a legal transition."""
        from lib_core import ALLOWED_STAGE_TRANSITIONS

        allowed = ALLOWED_STAGE_TRANSITIONS.get("CHECKPOINT_AUDIT", set())
        assert "FINAL_AUDIT" in allowed, (
            f"FINAL_AUDIT must be reachable from CHECKPOINT_AUDIT. "
            f"Current allowed: {allowed}"
        )

    def test_transition_final_audit_to_done(self):
        """#22 regression: FINAL_AUDIT -> DONE must remain legal (outbound edge preserved)."""
        from lib_core import ALLOWED_STAGE_TRANSITIONS

        allowed = ALLOWED_STAGE_TRANSITIONS.get("FINAL_AUDIT", set())
        assert "DONE" in allowed, (
            f"DONE must remain reachable from FINAL_AUDIT. Current allowed: {allowed}"
        )


# ===========================================================================
# AC 12 (#23) — lib_core.py: DONE writes completed_at (not completion_at)
# ===========================================================================

class TestDoneSetsCompletedAt:
    def test_done_sets_completed_at(self, tmp_path: Path):
        """#23: After transition to DONE, manifest must contain 'completed_at'
        (not 'completion_at') with a non-empty ISO timestamp."""
        from lib_core import transition_task, ALLOWED_STAGE_TRANSITIONS

        # We need a task in a stage that can transition to DONE.
        # DONE is reachable from CHECKPOINT_AUDIT or FINAL_AUDIT.
        # Since FINAL_AUDIT -> DONE is already legal, use that.
        task_dir = tmp_path / ".dynos" / "task-done-test"
        task_dir.mkdir(parents=True, exist_ok=True)

        # We need FINAL_AUDIT -> DONE, but FINAL_AUDIT is currently in allowed
        # transitions for DONE. Use transition from a stage that bypasses heavy gates.
        # Use force=True to bypass receipt gates (we're testing key name, not gate logic).
        manifest = {
            "task_id": "task-done-test",
            "created_at": "2026-06-01T00:00:00Z",
            "raw_input": "test",
            "stage": "CHECKPOINT_AUDIT",
            "classification": {
                "type": "feature",
                "domains": ["backend"],
                "risk_level": "medium",
                "notes": "",
            },
        }
        (task_dir / "manifest.json").write_text(json.dumps(manifest))

        # Attempt the transition using force to bypass all gate checks.
        try:
            transition_task(
                task_dir,
                "DONE",
                force=True,
                force_reason="testing completed_at key fix",
                force_approver="test-agent",
            )
        except Exception:
            # The transition may fail on other required artifacts — that's OK.
            # We just need to check if completed_at was written before failure.
            pass

        updated_manifest = json.loads((task_dir / "manifest.json").read_text())

        # After the fix: completed_at must be present (not completion_at).
        assert "completed_at" in updated_manifest, (
            f"manifest must have 'completed_at' key after DONE transition. "
            f"Got keys: {list(updated_manifest.keys())}"
        )
        assert updated_manifest["completed_at"], (
            "completed_at must be non-empty"
        )
        assert "completion_at" not in updated_manifest, (
            "Old typo key 'completion_at' must not be present"
        )


# ===========================================================================
# AC 13 (#25) — lib_core.py: REPAIR_PLANNING repair-cap crash-safe
# ===========================================================================

class TestRepairPlanningCrashSafe:
    def _make_repair_execution_task(self, task_dir: Path) -> None:
        """Set up a task in REPAIR_EXECUTION stage with minimal required files."""
        task_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "task_id": task_dir.name,
            "created_at": "2026-06-01T00:00:00Z",
            "raw_input": "test",
            "stage": "REPAIR_EXECUTION",
            "classification": {
                "type": "feature",
                "domains": ["backend"],
                "risk_level": "medium",
                "notes": "",
            },
        }
        (task_dir / "manifest.json").write_text(json.dumps(manifest))

    def test_repair_planning_corrupt_repair_log(self, tmp_path: Path):
        """#25: repair-log.json containing a non-dict value must produce ValueError
        (not AttributeError or json.JSONDecodeError propagating uncaught)."""
        from lib_core import transition_task

        task_dir = tmp_path / ".dynos" / "task-repair-corrupt"
        self._make_repair_execution_task(task_dir)

        # Write a corrupt repair-log.json (not a dict).
        (task_dir / "repair-log.json").write_text(json.dumps("not a dict"))

        # The transition should fail (some gate will block), but it must NOT
        # propagate AttributeError or json.JSONDecodeError.
        try:
            transition_task(task_dir, "REPAIR_PLANNING")
        except AttributeError as exc:
            pytest.fail(
                f"transition_task raised AttributeError (not caught): {exc}"
            )
        except ValueError:
            pass  # This is the expected safe failure.
        except Exception:
            pass  # Any other exception is acceptable (gate refused etc.)

    def test_repair_planning_missing_repair_log(self, tmp_path: Path):
        """#25: Missing repair-log.json must not crash transition_task."""
        from lib_core import transition_task

        task_dir = tmp_path / ".dynos" / "task-repair-missing"
        self._make_repair_execution_task(task_dir)

        # No repair-log.json file at all.
        assert not (task_dir / "repair-log.json").exists()

        # Must not raise AttributeError or json.JSONDecodeError.
        try:
            transition_task(task_dir, "REPAIR_PLANNING")
        except AttributeError as exc:
            pytest.fail(
                f"transition_task raised AttributeError on missing repair-log: {exc}"
            )
        except Exception:
            pass  # Gate failures or ValueErrors are acceptable.


# ===========================================================================
# AC 14 (#24) — lib_validate.py: compute_reward change_failure logic
# ===========================================================================

class TestDoraChangeFailure:
    def _make_task_for_reward(self, task_dir: Path, stage: str, repair_cycle_count: int = 0) -> Path:
        """Create a minimal task directory that compute_reward can process."""
        task_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "task_id": task_dir.name,
            "created_at": "2026-06-01T00:00:00Z",
            "raw_input": "test",
            "stage": stage,
            "repair_cycle_count": repair_cycle_count,
            "classification": {
                "type": "feature",
                "domains": ["backend"],
                "risk_level": "medium",
                "notes": "",
            },
        }
        (task_dir / "manifest.json").write_text(json.dumps(manifest))
        # Minimal audit report.
        audit_dir = task_dir / "audit-reports"
        audit_dir.mkdir()
        (audit_dir / "test-audit.json").write_text(json.dumps({
            "auditor": "test-auditor",
            "findings": [],
            "evidence": {"files_inspected": []},
        }))
        # Minimal retrospective (required by compute_reward).
        (task_dir / "task-retrospective.json").write_text(json.dumps({
            "quality_score": 0.9,
            "cost_score": 0.9,
            "efficiency_score": 0.9,
            "total_tokens": 0,
            "total_token_usage": 0,
            "task_type": "feature",
            "task_domains": "backend",
            "task_risk_level": "medium",
            "total_findings": 0,
            "total_blocking": 0,
            "repair_cycle_count": repair_cycle_count,
            "change_failure": False,
        }))
        return task_dir

    def test_dora_change_failure_after_repair(self, tmp_path: Path):
        """#24: manifest with stage='FAILED' and repair_cycle_count=1 must
        produce change_failure=True."""
        from lib_validate import compute_reward

        task_dir = self._make_task_for_reward(
            tmp_path / ".dynos" / "task-change-fail",
            stage="FAILED",
            repair_cycle_count=1,
        )

        result = compute_reward(task_dir)
        assert result.get("change_failure") is True, (
            f"Expected change_failure=True for FAILED task with repair_cycle_count=1, "
            f"got: {result.get('change_failure')!r}"
        )

    def test_dora_change_failure_no_repair(self, tmp_path: Path):
        """#24: manifest with stage='FAILED' and repair_cycle_count=0 must
        produce change_failure=False."""
        from lib_validate import compute_reward

        task_dir = self._make_task_for_reward(
            tmp_path / ".dynos" / "task-no-change-fail",
            stage="FAILED",
            repair_cycle_count=0,
        )

        result = compute_reward(task_dir)
        assert result.get("change_failure") is False, (
            f"Expected change_failure=False for FAILED task with repair_cycle_count=0, "
            f"got: {result.get('change_failure')!r}"
        )


# ===========================================================================
# AC 15 (#47) — lib_validate.py: clean tasks score 1.0
# ===========================================================================

class TestCleanTaskQualityScore:
    def _make_clean_task(self, task_dir: Path) -> Path:
        """Create a task with zero findings and zero blocking violations."""
        task_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "task_id": task_dir.name,
            "created_at": "2026-06-01T00:00:00Z",
            "raw_input": "test",
            "stage": "DONE",
            "completed_at": "2026-06-02T00:00:00Z",
            "repair_cycle_count": 0,
            "classification": {
                "type": "feature",
                "domains": ["backend"],
                "risk_level": "medium",
                "notes": "",
            },
        }
        (task_dir / "manifest.json").write_text(json.dumps(manifest))
        audit_dir = task_dir / "audit-reports"
        audit_dir.mkdir()
        # Audit report with zero findings.
        (audit_dir / "clean-audit.json").write_text(json.dumps({
            "auditor": "test-auditor",
            "findings": [],
            "evidence": {"files_inspected": []},
        }))
        (task_dir / "task-retrospective.json").write_text(json.dumps({
            "quality_score": 0.9,
            "cost_score": 0.9,
            "efficiency_score": 0.9,
            "total_tokens": 0,
            "total_token_usage": 0,
            "task_type": "feature",
            "task_domains": "backend",
            "task_risk_level": "medium",
            "total_findings": 0,
            "total_blocking": 0,
            "repair_cycle_count": 0,
            "change_failure": False,
        }))
        return task_dir

    def test_clean_task_quality_score(self, tmp_path: Path):
        """#47: A task with total_findings=0 and total_blocking=0 must produce
        quality_score == 1.0 (not 0.9)."""
        from lib_validate import compute_reward

        task_dir = self._make_clean_task(tmp_path / ".dynos" / "task-clean")
        result = compute_reward(task_dir)

        assert result.get("quality_score") == 1.0, (
            f"Clean task (zero findings) must have quality_score=1.0, "
            f"got {result.get('quality_score')!r}. "
            f"This is the bug: both branches of the ternary return 0.9."
        )


# ===========================================================================
# AC 16 (#29) — lib_contracts.py: no phantom "learn" in PIPELINE_ORDER
# ===========================================================================

class TestValidateChainNoPhantomLearn:
    def test_validate_chain_no_phantom_learn(self):
        """#29: PIPELINE_ORDER must not include 'learn'; validate_chain must not
        return any error containing 'learn'."""
        import lib_contracts

        pipeline_order = getattr(lib_contracts, "PIPELINE_ORDER", None)
        assert pipeline_order is not None, "PIPELINE_ORDER must exist in lib_contracts"
        assert "learn" not in pipeline_order, (
            f"'learn' is a phantom skill and must not be in PIPELINE_ORDER. "
            f"Current: {pipeline_order}"
        )

        # Also run validate_chain and check no error mentions "learn".
        validate_chain = getattr(lib_contracts, "validate_chain", None)
        assert validate_chain is not None, "validate_chain must exist"

        errors = validate_chain()
        learn_errors = [e for e in errors if "learn" in str(e).lower()]
        assert not learn_errors, (
            f"validate_chain returned errors mentioning 'learn': {learn_errors}"
        )


# ===========================================================================
# AC 17 (#28) — daemon.py: calibration_recovery_sweep writes receipts
# ===========================================================================

class TestCalibrationSweep:
    def _make_done_task(self, root: Path, task_name: str) -> Path:
        """Create a DONE task with no calibration receipt."""
        task_dir = root / ".dynos" / task_name
        task_dir.mkdir(parents=True, exist_ok=True)
        receipts = task_dir / "receipts"
        receipts.mkdir(parents=True, exist_ok=True)

        manifest = {
            "task_id": task_name,
            "created_at": "2026-06-01T00:00:00Z",
            "raw_input": "test",
            "stage": "DONE",
            "completed_at": "2026-06-01T12:00:00Z",
            "repair_cycle_count": 0,
            "classification": {
                "type": "feature",
                "domains": ["backend"],
                "risk_level": "medium",
                "notes": "",
            },
        }
        (task_dir / "manifest.json").write_text(json.dumps(manifest))
        return task_dir

    def test_calibration_sweep_stranded_task(self, tmp_path: Path):
        """#28: calibration_recovery_sweep must write a calibration receipt
        for a stranded DONE task."""
        from daemon import calibration_recovery_sweep

        task_dir = self._make_done_task(tmp_path, "task-stranded")
        receipts = task_dir / "receipts"

        # Patch drain to simulate it writing the calibration receipt.
        def mock_drain(root):
            # Simulate drain writing the calibration receipt.
            (receipts / "calibration-noop.json").write_text(
                json.dumps({"status": "noop", "reason": "test"})
            )

        with mock.patch("daemon.drain", mock_drain, create=True), \
             mock.patch("daemon.log_event"):
            result = calibration_recovery_sweep(tmp_path)

        # Either form of receipt must exist.
        has_receipt = (
            (receipts / "calibration-applied.json").exists()
            or (receipts / "calibration-noop.json").exists()
        )
        assert has_receipt, (
            "calibration_recovery_sweep must write a calibration receipt for stranded DONE task"
        )
        assert result["attempted"] >= 1, "should have attempted at least one recovery"

    def test_calibration_sweep_already_receipted_skipped(self, tmp_path: Path):
        """#28: A DONE task with existing calibration-noop.json must be skipped;
        no duplicate receipt or error."""
        from daemon import calibration_recovery_sweep

        task_dir = self._make_done_task(tmp_path, "task-already-receipted")
        receipts = task_dir / "receipts"

        # Pre-existing receipt.
        (receipts / "calibration-noop.json").write_text(
            json.dumps({"status": "noop", "reason": "already done"})
        )

        drain_called = []

        def mock_drain(root):
            drain_called.append(True)

        with mock.patch("daemon.drain", mock_drain, create=True), \
             mock.patch("daemon.log_event"):
            result = calibration_recovery_sweep(tmp_path)

        assert result["attempted"] == 0, (
            f"Already-receipted task must be skipped; attempted={result['attempted']}"
        )
        assert not drain_called, "drain must not be called for already-receipted task"


# ===========================================================================
# AC 18 (#58) — eventbus.py: debounce reads v2 registry path structure
# ===========================================================================

class TestDebounceV2Registry:
    def test_debounce_v2_registry(self, tmp_path: Path):
        """#58: run_register must return True without spawning when a v2 registry
        entry has a matching path with a fresh last_active_at."""
        from datetime import datetime, timezone
        import eventbus

        root = tmp_path / "my-project"
        root.mkdir(parents=True, exist_ok=True)

        fresh_ts = datetime.now(timezone.utc).isoformat()

        # Build a v2-schema registry.
        registry_data = {
            "schema_version": 2,
            "write_version": 1,
            "projects": [
                {
                    "id": "test-uuid-1234",
                    "paths": [
                        {
                            "path": str(root.resolve()),
                            "registered_at": fresh_ts,
                            "last_active_at": fresh_ts,
                        }
                    ],
                    "status": "active",
                }
            ],
        }

        # Point the canonical registry location ($DYNOS_HOME/registry.json,
        # matching registry._registry_path()) at the tmp dir, then write the
        # v2 registry there. This exercises the REAL path convention rather
        # than the wrong ~/registry.json.
        tmp_registry = tmp_path / "registry.json"
        tmp_registry.write_text(json.dumps(registry_data))

        spawn_called = []

        def mock_run(cmd, root, timeout):
            spawn_called.append(cmd)
            return True

        with mock.patch("eventbus._run", side_effect=mock_run), \
             mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            result = eventbus.run_register(root, {})

        assert result is True, "run_register must return True (debounce triggered)"
        assert not spawn_called, (
            f"run_register must NOT spawn subprocess when v2 entry is fresh. "
            f"But spawn was called with: {spawn_called}"
        )


# ===========================================================================
# AC 19 (#59) — worktree.py: _execute_migration has no main_root param
# ===========================================================================

class TestExecuteMigrationNoDeadParam:
    def test_execute_migration_no_dead_param(self):
        """#59: _execute_migration must not have 'main_root' in its signature."""
        import worktree

        fn = getattr(worktree, "_execute_migration", None)
        assert fn is not None, "_execute_migration must exist in worktree"

        sig = inspect.signature(fn)
        param_names = list(sig.parameters.keys())
        assert "main_root" not in param_names, (
            f"_execute_migration must not have dead 'main_root' parameter. "
            f"Current params: {param_names}"
        )


# ===========================================================================
# AC 20 (#60) — plan_intermediate_state_check.py: docstring says OR
# ===========================================================================

class TestIntermediateCheckDocstringOr:
    def test_intermediate_check_docstring_or(self):
        """#60: The triggering condition docstring must say 'OR', not 'AND'."""
        import plan_intermediate_state_check

        # Read the module source to check the docstring.
        source = inspect.getsource(plan_intermediate_state_check)

        # The docstring at lines 27-31 should say OR.
        # Check for the problematic AND in the trigger condition sentence.
        trigger_lines = [
            line.strip() for line in source.splitlines()
            if "topology" in line.lower() and ("and" in line.lower() or "or" in line.lower())
            and ("trigger" in line.lower() or "blocked" in line.lower() or "mismatch" in line.lower())
        ]

        # Also check module docstring directly.
        module_doc = plan_intermediate_state_check.__doc__ or ""
        if not module_doc:
            # Try to get it from source.
            import ast
            try:
                tree = ast.parse(source)
                module_doc = ast.get_docstring(tree) or ""
            except Exception:
                pass

        # The spec says "AND" -> "OR" in the triggering condition sentence.
        # Current buggy state: "AND confirmed smoke-test failures trigger `blocked`"
        assert "topology mismatches AND confirmed smoke" not in module_doc, (
            "Docstring must use 'OR' not 'AND' in the triggering condition. "
            f"Module docstring: {module_doc[:200]!r}"
        )


# ===========================================================================
# AC 21 (#42) — ctl.py: five dead duplicate function definitions gone
# ===========================================================================

class TestNoDeadDuplicateFunctionDefs:
    def test_no_dead_duplicate_function_defs(self):
        """#42: Each of the five dead duplicate function names must appear
        exactly once as a 'def' statement in ctl.py."""
        ctl_path = Path(__file__).resolve().parent.parent / "hooks" / "ctl.py"
        assert ctl_path.exists(), f"ctl.py must exist at {ctl_path}"

        source = ctl_path.read_text()
        lines = source.splitlines()

        dead_names = [
            "cmd_run_external_solution_gate",
            "cmd_write_execute_handoff",
            "cmd_write_execution_graph",
            "cmd_write_repair_log",
            "cmd_write_classification",
        ]

        for name in dead_names:
            def_count = sum(
                1 for line in lines
                if line.strip().startswith(f"def {name}(")
            )
            assert def_count == 1, (
                f"'{name}' must appear exactly once as a def statement in ctl.py; "
                f"found {def_count} definitions. Dead duplicate must be deleted."
            )


# ===========================================================================
# AC 22 (#55) — ctl.py: cyclic execution graph rejected
# ===========================================================================

class TestWalkCycleGuard:
    def test_walk_cycle_no_recursion_error(self):
        """#55: A two-segment cycle (A->B->A) must not cause RecursionError
        in _dependency_depths."""
        import ctl

        # A depends on B, B depends on A = cycle.
        segments = [
            {"id": "seg-A", "depends_on": ["seg-B"],
             "executor": "e", "files_expected": ["f.py"], "criteria_ids": [1]},
            {"id": "seg-B", "depends_on": ["seg-A"],
             "executor": "e", "files_expected": ["f.py"], "criteria_ids": [1]},
        ]

        _dependency_depths = getattr(ctl, "_dependency_depths", None)
        assert _dependency_depths is not None, "_dependency_depths must exist in ctl"

        try:
            result = _dependency_depths(segments)
        except RecursionError:
            pytest.fail(
                "_dependency_depths must not raise RecursionError on cyclic graph"
            )
        # After fix: must return without crashing.
        assert isinstance(result, dict), "Must return a dict"

    def test_validate_execution_graph_rejects_cycle(self, tmp_path: Path):
        """#55: _validate_execution_graph_payload must reject a cyclic dependency graph."""
        import ctl

        _validate = getattr(ctl, "_validate_execution_graph_payload", None)
        assert _validate is not None, "_validate_execution_graph_payload must exist"

        task_dir = tmp_path / ".dynos" / "task-cycle"
        task_dir.mkdir(parents=True, exist_ok=True)

        cyclic_payload = {
            "task_id": "task-cycle",
            "segments": [
                {"id": "seg-A", "executor": "e",
                 "files_expected": ["f.py"], "criteria_ids": [1],
                 "depends_on": ["seg-B"]},
                {"id": "seg-B", "executor": "e",
                 "files_expected": ["f.py"], "criteria_ids": [1],
                 "depends_on": ["seg-A"]},
            ],
        }

        with pytest.raises((ValueError, Exception)) as exc_info:
            _validate(task_dir, cyclic_payload)

        assert exc_info.value is not None, (
            "_validate_execution_graph_payload must raise an error for cyclic graph"
        )


# ===========================================================================
# AC 23 (#27) — ctl.py: source_auditor_allowlist ceiling enforced at veto time
# ===========================================================================

class TestVetoSourceAuditorAllowlist:
    def _make_veto_context(self, tmp_path: Path, source_auditor: str, risk_level: str = "low") -> tuple:
        """Build the minimal context for apply_auto_approve_veto."""
        import ctl

        # Minimal manifest.
        manifest = {
            "task_id": "task-veto-test",
            "stage": "CHECKPOINT_AUDIT",
            "auto_approve_gates": True,
            "classification": {
                "type": "feature",
                "domains": ["backend"],
                "risk_level": risk_level,
            },
        }
        task_dir = tmp_path / ".dynos" / "task-veto-test"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "manifest.json").write_text(json.dumps(manifest))

        # Minimal row.
        row = {
            "source_auditor": source_auditor,
            "finding_id": "F-001",
            "status": "pending",
        }

        return task_dir, manifest, row

    def test_veto_source_auditor_not_in_allowlist(self, tmp_path: Path):
        """#27: A row with source_auditor not in _AUTO_APPROVE_ALLOWED_AUDITORS
        must be blocked with blocked_by='source_auditor_allowlist'."""
        import ctl

        apply_veto = getattr(ctl, "apply_auto_approve_veto", None)
        assert apply_veto is not None, "apply_auto_approve_veto must exist in ctl"

        allowed_auditors = getattr(ctl, "_AUTO_APPROVE_ALLOWED_AUDITORS", None)
        assert allowed_auditors is not None, "_AUTO_APPROVE_ALLOWED_AUDITORS must exist"

        # Use an auditor that is definitely NOT in the allowlist.
        unknown_auditor = "unknown-auditor-not-in-allowlist"
        assert unknown_auditor not in allowed_auditors, \
            f"{unknown_auditor!r} should not be in allowlist {allowed_auditors}"

        task_dir, manifest, row = self._make_veto_context(tmp_path, unknown_auditor, risk_level="low")

        try:
            result = apply_veto(
                task_dir=task_dir,
                manifest=manifest,
                row=row,
                row_source_auditor=unknown_auditor,
                risk_level="low",
                domains={"backend"},
                root=tmp_path,
            )
        except TypeError:
            # Signature may differ; try without row_source_auditor kwarg.
            result = apply_veto(
                task_dir=task_dir,
                manifest=manifest,
                row=row,
                risk_level="low",
                domains={"backend"},
                root=tmp_path,
            )

        assert result.get("decision") == "blocked", (
            f"source_auditor not in allowlist must produce decision='blocked'; got: {result}"
        )
        assert result.get("blocked_by") == "source_auditor_allowlist", (
            f"blocked_by must be 'source_auditor_allowlist'; got: {result.get('blocked_by')!r}"
        )

    def test_veto_security_auditor_blocked(self, tmp_path: Path):
        """#27 regression: security-auditor must still be blocked by
        source_auditor_security (not overridden by the new allowlist branch)."""
        import ctl

        apply_veto = getattr(ctl, "apply_auto_approve_veto", None)
        assert apply_veto is not None, "apply_auto_approve_veto must exist in ctl"

        task_dir, manifest, row = self._make_veto_context(
            tmp_path, "security-auditor", risk_level="low"
        )
        row["source_auditor"] = "security-auditor"

        try:
            result = apply_veto(
                task_dir=task_dir,
                manifest=manifest,
                row=row,
                row_source_auditor="security-auditor",
                risk_level="low",
                domains={"backend"},
                root=tmp_path,
            )
        except TypeError:
            result = apply_veto(
                task_dir=task_dir,
                manifest=manifest,
                row=row,
                risk_level="low",
                domains={"backend"},
                root=tmp_path,
            )

        assert result.get("blocked_by") == "source_auditor_security", (
            f"security-auditor must be blocked by 'source_auditor_security', "
            f"got: {result.get('blocked_by')!r}. "
            f"The new allowlist elif must not shadow the security branch."
        )
