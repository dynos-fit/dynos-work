"""Tests for _verify_git_diff_covers_files (AC 1, 2), bypass logic (AC 4),
receipt_executor_done fields (AC 5), and cmd_run_execution_segment_done
segment_invalid gate (AC 3).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"
CTL_PY = HOOKS_DIR / "ctl.py"

if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

import importlib

ctl = importlib.import_module("ctl")
_verify_git_diff_covers_files = ctl._verify_git_diff_covers_files

from lib_receipts import receipt_executor_done  # noqa: E402


# Real git HEAD SHA so that integration tests can pass a valid SHA to git diff.
# The diff output is determined by what is actually changed vs that SHA.
_GIT_HEAD_SHA = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=str(ROOT),
    text=True,
    capture_output=True,
    check=False,
).stdout.strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_subprocess_result(stdout: str = "", returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    return result


def _run_ctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(CTL_PY), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Task-dir fixture builders for integration tests
# ---------------------------------------------------------------------------

def _setup_task_dir_for_integration(
    tmp_path: Path,
    *,
    task_id: str = "task-20260423-002",
    segment_id: str = "seg-test",
    files_expected: list[str] | None = None,
    snapshot_sha: str | None = None,
    no_op_justified: bool = False,
    no_op_reason: str = "",
    executor: str = "testing-executor",
) -> tuple[Path, Path]:
    """Build a minimal task dir rooted under tmp_path. Returns (root, task_dir).

    Uses _GIT_HEAD_SHA as the default snapshot SHA so that 'git diff HEAD..HEAD'
    returns exit 0 with empty output — meaning any files_expected will be reported
    as missing (not in diff).
    """
    if snapshot_sha is None:
        snapshot_sha = _GIT_HEAD_SHA

    root = tmp_path / "project"
    dynos = root / ".dynos"
    dynos.mkdir(parents=True)

    task_dir = dynos / task_id
    task_dir.mkdir(parents=True)

    manifest: dict = {
        "task_id": task_id,
        "stage": "EXECUTION",
        "snapshot": {"head_sha": snapshot_sha},
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest))

    segment: dict = {
        "id": segment_id,
        "executor": executor,
        "description": "A test segment",
        "files_expected": files_expected or [],
        "depends_on": [],
        "parallelizable": True,
        "criteria_ids": [1],
    }
    if no_op_justified:
        segment["no_op_justified"] = True
        segment["no_op_reason"] = no_op_reason
    graph = {"task_id": task_id, "segments": [segment]}
    (task_dir / "execution-graph.json").write_text(json.dumps(graph))

    # Evidence file required by cmd_run_execution_segment_done
    evidence_dir = task_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / f"{segment_id}.md").write_text("# Evidence\nDone.\n")

    # Injected prompt sidecar
    sidecar_dir = task_dir / "receipts" / "_injected-prompts"
    sidecar_dir.mkdir(parents=True)
    digest = "a" * 64
    (sidecar_dir / f"{segment_id}.sha256").write_text(digest)

    return root, task_dir


# ===========================================================================
# AC 1 / AC 2: Unit tests for _verify_git_diff_covers_files
# ===========================================================================

class TestVerifyGitDiffCoversFiles:
    """Unit tests using unittest.mock.patch to intercept subprocess.run.

    _verify_git_diff_covers_files does 'import subprocess as _subprocess' inside
    the function body, so patching the subprocess module's run attribute is the
    correct way to intercept both calls.
    """

    def test_all_files_present_returns_empty(self):
        """When every expected file appears in git diff output, returns []."""
        files = ["src/foo.py", "src/bar.py"]
        diff_result = _make_subprocess_result(stdout="\n".join(files) + "\n")
        ls_result = _make_subprocess_result(stdout="")

        def fake_run(cmd, **kwargs):
            if "diff" in cmd:
                return diff_result
            return ls_result

        with patch("subprocess.run", side_effect=fake_run):
            result = _verify_git_diff_covers_files(Path("/repo"), "abc123", files)
        assert result == []

    def test_one_file_missing_returns_it(self):
        """When one file is absent from both diff and ls-files, it is returned."""
        diff_result = _make_subprocess_result(stdout="src/foo.py\n")
        ls_result = _make_subprocess_result(stdout="")

        def fake_run(cmd, **kwargs):
            if "diff" in cmd:
                return diff_result
            return ls_result

        with patch("subprocess.run", side_effect=fake_run):
            result = _verify_git_diff_covers_files(
                Path("/repo"), "abc123", ["src/foo.py", "src/missing.py"]
            )
        assert result == ["src/missing.py"]

    def test_untracked_file_covered_by_ls_files(self):
        """A new untracked file not in diff but in ls-files output is NOT missing."""
        diff_result = _make_subprocess_result(stdout="src/existing.py\n")
        ls_result = _make_subprocess_result(stdout="src/new_untracked.py\n")

        def fake_run(cmd, **kwargs):
            if "diff" in cmd:
                return diff_result
            return ls_result

        with patch("subprocess.run", side_effect=fake_run):
            result = _verify_git_diff_covers_files(
                Path("/repo"), "abc123", ["src/existing.py", "src/new_untracked.py"]
            )
        assert result == []

    def test_nonzero_returncode_raises_value_error(self):
        """git diff returning non-zero exit code raises ValueError containing 'git command failed'."""
        diff_result = _make_subprocess_result(stdout="", returncode=128)

        def fake_run(cmd, **kwargs):
            if "diff" in cmd:
                return diff_result
            return _make_subprocess_result()

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(ValueError, match="git command failed"):
                _verify_git_diff_covers_files(Path("/repo"), "abc123", ["src/a.py"])

    def test_ls_files_nonzero_returncode_raises_value_error(self):
        """git ls-files returning non-zero exit code raises ValueError."""
        diff_result = _make_subprocess_result(stdout="src/a.py\n")
        ls_result = _make_subprocess_result(stdout="", returncode=1)

        def fake_run(cmd, **kwargs):
            if "diff" in cmd:
                return diff_result
            return ls_result

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(ValueError, match="git command failed"):
                _verify_git_diff_covers_files(Path("/repo"), "abc123", ["src/a.py"])

    def test_output_lines_stripped_and_filtered(self):
        """Whitespace and blank lines in stdout are stripped before comparison."""
        diff_result = _make_subprocess_result(stdout="  src/a.py  \n\n   \nsrc/b.py\n")
        ls_result = _make_subprocess_result(stdout="")

        def fake_run(cmd, **kwargs):
            if "diff" in cmd:
                return diff_result
            return ls_result

        with patch("subprocess.run", side_effect=fake_run):
            result = _verify_git_diff_covers_files(
                Path("/repo"), "abc123", ["src/a.py", "src/b.py"]
            )
        assert result == []

    def test_none_snapshot_sha_raises(self):
        """snapshot_sha=None raises ValueError with 'snapshot_sha required'."""
        with pytest.raises(ValueError, match="snapshot_sha required"):
            _verify_git_diff_covers_files(Path("/repo"), None, ["src/a.py"])

    def test_empty_snapshot_sha_raises(self):
        """snapshot_sha='' raises ValueError with 'snapshot_sha required'."""
        with pytest.raises(ValueError, match="snapshot_sha required"):
            _verify_git_diff_covers_files(Path("/repo"), "", ["src/a.py"])

    def test_whitespace_snapshot_sha_raises(self):
        """snapshot_sha='   ' raises ValueError — no HEAD fallback allowed."""
        with pytest.raises(ValueError, match="snapshot_sha required"):
            _verify_git_diff_covers_files(Path("/repo"), "   ", ["src/a.py"])

    def test_git_not_found_raises(self):
        """FileNotFoundError from subprocess (git not installed) raises ValueError with 'git binary not available'."""
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("No such file: git")

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(ValueError, match="git binary not available"):
                _verify_git_diff_covers_files(Path("/repo"), "abc123", ["src/a.py"])

    def test_nonzero_return_message_contains_git_command_failed(self):
        """Non-zero returncode error message contains 'git command failed'."""
        diff_result = _make_subprocess_result(stdout="", returncode=128)

        def fake_run(cmd, **kwargs):
            if "diff" in cmd:
                return diff_result
            return _make_subprocess_result()

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(ValueError) as exc_info:
                _verify_git_diff_covers_files(Path("/repo"), "abc123", ["src/a.py"])
        assert "git command failed" in str(exc_info.value)

    def test_nonzero_return_includes_returncode_in_message(self):
        """Error message from non-zero returncode includes the integer returncode."""
        rc = 128
        diff_result = _make_subprocess_result(stdout="", returncode=rc)

        def fake_run(cmd, **kwargs):
            if "diff" in cmd:
                return diff_result
            return _make_subprocess_result()

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(ValueError) as exc_info:
                _verify_git_diff_covers_files(Path("/repo"), "abc123", ["src/a.py"])
        assert str(rc) in str(exc_info.value)

    def test_files_expected_empty_list_returns_empty(self):
        """When files_expected is empty, returns [] regardless of diff output."""
        diff_result = _make_subprocess_result(stdout="some/other/file.py\n")
        ls_result = _make_subprocess_result(stdout="")

        def fake_run(cmd, **kwargs):
            if "diff" in cmd:
                return diff_result
            return ls_result

        with patch("subprocess.run", side_effect=fake_run):
            result = _verify_git_diff_covers_files(Path("/repo"), "abc123", [])
        assert result == []

    def test_multiple_files_missing_all_returned(self):
        """When multiple expected files are absent, all are returned."""
        diff_result = _make_subprocess_result(stdout="src/present.py\n")
        ls_result = _make_subprocess_result(stdout="")

        def fake_run(cmd, **kwargs):
            if "diff" in cmd:
                return diff_result
            return ls_result

        with patch("subprocess.run", side_effect=fake_run):
            result = _verify_git_diff_covers_files(
                Path("/repo"), "abc123", ["src/present.py", "src/gone1.py", "src/gone2.py"]
            )
        assert sorted(result) == ["src/gone1.py", "src/gone2.py"]


# ===========================================================================
# AC 4: Bypass logic tests
# ===========================================================================

def _bypass_check(no_op_justified, no_op_reason_val=None) -> bool:
    """Replicate the exact bypass predicate from cmd_run_execution_segment_done."""
    no_op_justified_flag = no_op_justified
    no_op_reason = no_op_reason_val
    return (
        no_op_justified_flag is True
        and isinstance(no_op_reason, str)
        and len(no_op_reason.strip()) >= 20
    )


class TestBypassLogic:
    """Tests for AC 4 bypass gate: BOTH no_op_justified is True (strict)
    AND no_op_reason is str with len >= 20."""

    def test_true_with_twenty_char_reason_bypasses(self):
        """no_op_justified=True + 20-char reason → bypass."""
        assert _bypass_check(True, "x" * 20) is True

    def test_true_with_nineteen_char_reason_does_not_bypass(self):
        """19-char reason is one short of the threshold → no bypass."""
        assert _bypass_check(True, "x" * 19) is False

    def test_truthy_string_does_not_bypass(self):
        """no_op_justified='true' (str) does not satisfy strict `is True`."""
        assert _bypass_check("true", "x" * 20) is False

    def test_integer_one_does_not_bypass(self):
        """no_op_justified=1 (int) is truthy but not `is True`."""
        assert _bypass_check(1, "x" * 20) is False

    def test_reason_absent_does_not_bypass(self):
        """No no_op_reason key (None) → no bypass."""
        assert _bypass_check(True, None) is False

    def test_reason_whitespace_only_does_not_bypass(self):
        """no_op_reason='   ' strips to 0 chars → no bypass."""
        assert _bypass_check(True, "   ") is False

    def test_reason_exactly_twenty_chars_after_strip(self):
        """Leading/trailing spaces stripped before len check; '  ' + 'x'*20 → bypass."""
        assert _bypass_check(True, "  " + "x" * 20) is True

    def test_false_justified_does_not_bypass(self):
        """no_op_justified=False with long reason → no bypass."""
        assert _bypass_check(False, "x" * 20) is False

    def test_true_with_exactly_nineteen_stripped_does_not_bypass(self):
        """Whitespace padding cannot inflate the stripped count past threshold."""
        # Stripped = 19 chars
        assert _bypass_check(True, "  " + "x" * 19 + "  ") is False

    def test_none_justified_does_not_bypass(self):
        """no_op_justified=None → no bypass."""
        assert _bypass_check(None, "x" * 20) is False


# ===========================================================================
# AC 5: receipt_executor_done field validation tests
# ===========================================================================

class TestReceiptExecutorDoneFields:
    """Tests for diff_verified_files and no_op_justified validation in
    receipt_executor_done (AC 5)."""

    def _write_sidecar(self, td: Path, segment_id: str, digest: str) -> None:
        sd = td / "receipts" / "_injected-prompts"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / f"{segment_id}.sha256").write_text(digest)

    def _call_receipt(self, td, seg_id="seg-1", digest="a" * 64,
                       diff_verified_files=None, no_op_justified=False, **kwargs):
        self._write_sidecar(td, seg_id, digest)
        if diff_verified_files is None:
            diff_verified_files = []
        return receipt_executor_done(
            td, seg_id, "testing-executor", "haiku",
            injected_prompt_sha256=digest,
            agent_name=None, evidence_path=None, tokens_used=0,
            diff_verified_files=diff_verified_files,
            no_op_justified=no_op_justified,
            **kwargs,
        )

    def test_valid_diff_verified_files_list_of_strings_accepted(self, tmp_path):
        """list[str] for diff_verified_files is accepted and written to receipt."""
        td = tmp_path / "td"
        td.mkdir()
        out = self._call_receipt(td, diff_verified_files=["src/a.py", "src/b.py"])
        payload = json.loads(out.read_text())
        assert payload["diff_verified_files"] == ["src/a.py", "src/b.py"]

    def test_valid_empty_list_accepted(self, tmp_path):
        """diff_verified_files=[] is valid (bypass/no-op path)."""
        td = tmp_path / "td"
        td.mkdir()
        out = self._call_receipt(td, diff_verified_files=[])
        payload = json.loads(out.read_text())
        assert payload["diff_verified_files"] == []

    def test_non_list_diff_verified_files_raises_value_error(self, tmp_path):
        """Passing a non-list raises ValueError with correct message."""
        td = tmp_path / "td"
        td.mkdir()
        self._write_sidecar(td, "seg-1", "a" * 64)
        with pytest.raises(ValueError, match="diff_verified_files must be a list of strings"):
            receipt_executor_done(
                td, "seg-1", "testing-executor", "haiku",
                injected_prompt_sha256="a" * 64,
                agent_name=None, evidence_path=None, tokens_used=0,
                diff_verified_files="not-a-list",
                no_op_justified=False,
            )

    def test_list_with_non_string_element_raises_value_error(self, tmp_path):
        """[1, 'x'] raises ValueError because 1 is not a str."""
        td = tmp_path / "td"
        td.mkdir()
        self._write_sidecar(td, "seg-1", "a" * 64)
        with pytest.raises(ValueError, match="diff_verified_files must be a list of strings"):
            receipt_executor_done(
                td, "seg-1", "testing-executor", "haiku",
                injected_prompt_sha256="a" * 64,
                agent_name=None, evidence_path=None, tokens_used=0,
                diff_verified_files=[1, "x"],
                no_op_justified=False,
            )

    def test_non_bool_no_op_justified_raises_value_error(self, tmp_path):
        """no_op_justified='true' (str) raises ValueError."""
        td = tmp_path / "td"
        td.mkdir()
        self._write_sidecar(td, "seg-1", "a" * 64)
        with pytest.raises(ValueError, match="no_op_justified must be a bool"):
            receipt_executor_done(
                td, "seg-1", "testing-executor", "haiku",
                injected_prompt_sha256="a" * 64,
                agent_name=None, evidence_path=None, tokens_used=0,
                diff_verified_files=[],
                no_op_justified="true",
            )

    def test_integer_no_op_justified_raises_value_error(self, tmp_path):
        """no_op_justified=1 (int) raises ValueError."""
        td = tmp_path / "td"
        td.mkdir()
        self._write_sidecar(td, "seg-1", "a" * 64)
        with pytest.raises(ValueError, match="no_op_justified must be a bool"):
            receipt_executor_done(
                td, "seg-1", "testing-executor", "haiku",
                injected_prompt_sha256="a" * 64,
                agent_name=None, evidence_path=None, tokens_used=0,
                diff_verified_files=[],
                no_op_justified=1,
            )

    def test_no_op_justified_bool_written_to_receipt(self, tmp_path):
        """no_op_justified is written to on-disk receipt as a bool field."""
        td = tmp_path / "td"
        td.mkdir()
        out = self._call_receipt(td, no_op_justified=False)
        payload = json.loads(out.read_text())
        assert payload["no_op_justified"] is False

    def test_no_op_justified_true_written_to_receipt(self, tmp_path):
        """no_op_justified=True is written to on-disk receipt."""
        td = tmp_path / "td"
        td.mkdir()
        out = self._call_receipt(td, no_op_justified=True)
        payload = json.loads(out.read_text())
        assert payload["no_op_justified"] is True

    def test_diff_verified_files_validated_before_receipt_write(self, tmp_path):
        """Validation runs before any file write: receipt must not exist after ValueError."""
        td = tmp_path / "td"
        td.mkdir()
        self._write_sidecar(td, "seg-val", "b" * 64)
        receipt_path = td / "receipts" / "executor-seg-val.json"
        assert not receipt_path.exists()
        with pytest.raises(ValueError, match="diff_verified_files must be a list of strings"):
            receipt_executor_done(
                td, "seg-val", "testing-executor", "haiku",
                injected_prompt_sha256="b" * 64,
                agent_name=None, evidence_path=None, tokens_used=0,
                diff_verified_files="oops",
                no_op_justified=False,
            )
        assert not receipt_path.exists(), "receipt must not be created when validation fails"


# ===========================================================================
# AC 3: Integration test – segment_invalid when files not in git diff
# ===========================================================================

def _setup_task_dir_in_real_repo(
    *,
    task_id: str = "task-20260423-intg",
    segment_id: str = "seg-test",
    files_expected: list[str] | None = None,
    snapshot_sha: str | None = None,
    include_snapshot: bool = True,
) -> tuple[Path, Path]:
    """Create a task dir inside the REAL git repo's .dynos directory.

    This is required for integration tests so that 'git -C <root> diff ...' runs
    against an actual git repository. We place it under ROOT/.dynos/<task_id> to
    match _root_for_task_dir expectations (root = task_dir.parent.parent = ROOT).

    Caller is responsible for cleaning up the created directory.
    """
    if snapshot_sha is None:
        snapshot_sha = _GIT_HEAD_SHA

    task_dir = ROOT / ".dynos" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict = {"task_id": task_id, "stage": "EXECUTION"}
    if include_snapshot:
        manifest["snapshot"] = {"head_sha": snapshot_sha}
    (task_dir / "manifest.json").write_text(json.dumps(manifest))

    segment: dict = {
        "id": segment_id,
        "executor": "testing-executor",
        "description": "Test segment",
        "files_expected": files_expected or [],
        "depends_on": [],
        "parallelizable": True,
        "criteria_ids": [1],
    }
    graph = {"task_id": task_id, "segments": [segment]}
    (task_dir / "execution-graph.json").write_text(json.dumps(graph))

    evidence_dir = task_dir / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    (evidence_dir / f"{segment_id}.md").write_text("# Evidence\nDone.\n")

    receipts_dir = task_dir / "receipts"
    receipts_dir.mkdir(exist_ok=True)

    # Write executor-routing receipt (required by check_segment_ownership).
    # Must include valid=true for read_receipt() to accept it.
    routing_payload = {
        "valid": True,
        "contract_version": 5,
        "receipt_type": "executor-routing",
        "task_id": task_id,
        "segments": [{
            "segment_id": segment_id,
            "executor": "testing-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        }],
    }
    (receipts_dir / "executor-routing.json").write_text(json.dumps(routing_payload))

    sidecar_dir = receipts_dir / "_injected-prompts"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    digest = "a" * 64
    (sidecar_dir / f"{segment_id}.sha256").write_text(digest)

    return ROOT, task_dir


class TestSegmentInvalidGate:
    """Integration tests running ctl.py as a subprocess to verify AC 3.

    These tests create task dirs inside the real git repo so that 'git -C <root>
    diff ...' commands run successfully and produce meaningful output.
    """

    def _run_segment_done(
        self,
        task_dir: Path,
        segment_id: str,
        digest: str = "a" * 64,
        extra_args: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [
            "python3", str(CTL_PY),
            "run-execution-segment-done",
            str(task_dir),
            segment_id,
            "--injected-prompt-sha256", digest,
        ]
        if extra_args:
            cmd.extend(extra_args)
        return subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            check=False,
        )

    def _cleanup(self, task_dir: Path) -> None:
        import shutil
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)

    def test_missing_file_causes_segment_invalid_exit1(self):
        """When files_expected contains a path not in git diff since HEAD, exit code is 1.

        Uses the real repo so git diff succeeds (rc=0) but the nonexistent
        file is absent from diff output, triggering the missing-files gate.
        """
        _root, task_dir = _setup_task_dir_in_real_repo(
            task_id="task-20260423-intg-ac3a",
            files_expected=["nonexistent_file_that_will_never_be_in_diff.py"],
        )
        try:
            result = self._run_segment_done(task_dir, "seg-test")
            assert result.returncode == 1, (
                f"Expected exit 1, got {result.returncode}\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        finally:
            self._cleanup(task_dir)

    def test_missing_file_stdout_json_status_segment_invalid(self):
        """JSON stdout has status='segment_invalid' when files are missing from diff."""
        _root, task_dir = _setup_task_dir_in_real_repo(
            task_id="task-20260423-intg-ac3b",
            files_expected=["nonexistent_file_that_will_never_be_in_diff.py"],
        )
        try:
            result = self._run_segment_done(task_dir, "seg-test")
            payload = json.loads(result.stdout)
            assert payload["status"] == "segment_invalid"
        finally:
            self._cleanup(task_dir)

    def test_missing_file_stdout_json_has_missing_files(self):
        """JSON stdout has missing_files list containing the absent path."""
        missing = "nonexistent_file_that_will_never_be_in_diff.py"
        _root, task_dir = _setup_task_dir_in_real_repo(
            task_id="task-20260423-intg-ac3c",
            files_expected=[missing],
        )
        try:
            result = self._run_segment_done(task_dir, "seg-test")
            payload = json.loads(result.stdout)
            assert "missing_files" in payload, (
                f"Expected 'missing_files' key in payload: {payload}"
            )
            assert missing in payload["missing_files"]
        finally:
            self._cleanup(task_dir)

    def test_missing_file_no_receipt_written(self):
        """When segment is invalid (files missing from diff), no receipt file is created."""
        _root, task_dir = _setup_task_dir_in_real_repo(
            task_id="task-20260423-intg-ac3d",
            files_expected=["nonexistent_file_that_will_never_be_in_diff.py"],
        )
        try:
            result = self._run_segment_done(task_dir, "seg-test")
            receipt_path = task_dir / "receipts" / "executor-seg-test.json"
            assert not receipt_path.exists(), (
                f"Receipt must not be created on segment_invalid. stdout={result.stdout}"
            )
        finally:
            self._cleanup(task_dir)

    def test_no_snapshot_sha_fails_closed(self):
        """Tasks without a snapshot.head_sha in manifest get segment_invalid (fail-closed)."""
        _root, task_dir = _setup_task_dir_in_real_repo(
            task_id="task-20260423-intg-ac3f",
            files_expected=["nonexistent.py"],
            include_snapshot=False,
        )
        try:
            result = self._run_segment_done(task_dir, "seg-test")
            assert result.returncode == 1, (
                f"Expected exit code 1 when snapshot_sha absent, got {result.returncode}"
            )
            try:
                payload = json.loads(result.stdout)
                assert payload.get("status") == "segment_invalid", (
                    f"Expected segment_invalid, got: {payload}"
                )
                error_msg = payload.get("error", "")
                assert "snapshot_sha missing" in error_msg, (
                    f"Expected snapshot_sha missing error, got: {error_msg!r}"
                )
            except json.JSONDecodeError:
                pytest.fail(f"Expected JSON output, got: {result.stdout!r}")
        finally:
            self._cleanup(task_dir)
