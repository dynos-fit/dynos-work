"""
Security regression tests for sandbox/calibration/bench.py _validate_command —
AC 8, AC 9, AC 10 (task-20260616-002, finding #33).

The allowlist currently includes "sh" and "bash" and has no guard against the
`-c` inline-code argument for python3/python, so a fixture author can run
arbitrary code via the sandbox (`['bash','-c', payload]` or
`['python3','-c', payload]`). These tests encode the FIXED behaviour:
sh/bash are removed from the allowlist, and `-c` is rejected for python3/python
while legitimate script invocations still pass.

bench.py lives under sandbox/calibration/ and bootstraps its own sys.path; the
test imports it via the same path-insertion pattern used by the sandbox modules.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sandbox" / "calibration"))
sys.path.insert(0, str(ROOT / "hooks"))


def _import_bench():
    try:
        import bench
        return bench
    except ModuleNotFoundError as exc:  # pragma: no cover
        pytest.fail(f"bench module not importable: {exc}")


@pytest.fixture
def sandbox_path(tmp_path):
    return tmp_path


# ---------------------------------------------------------------------------
# AC 8: sh/bash removed from the allowlist.
# ---------------------------------------------------------------------------

def test_bench_allowlist_excludes_sh_and_bash():
    """ALLOWED_COMMAND_PREFIXES must not contain 'sh' or 'bash'."""
    bench = _import_bench()
    assert "bash" not in bench.ALLOWED_COMMAND_PREFIXES
    assert "sh" not in bench.ALLOWED_COMMAND_PREFIXES


def test_bench_allowlist_exact_set():
    """The allowlist must be exactly the ten permitted interpreters/runners."""
    bench = _import_bench()
    assert set(bench.ALLOWED_COMMAND_PREFIXES) == {
        "python3", "python", "node", "npm", "npx", "pytest", "jest",
        "go", "cargo", "make",
    }


def test_bench_rejects_bash_c(sandbox_path):
    """['bash','-c','echo x'] must raise SystemExit (bash no longer allowed)."""
    bench = _import_bench()
    with pytest.raises(SystemExit):
        bench._validate_command(["bash", "-c", "echo x"], sandbox_path)


def test_bench_rejects_sh_c(sandbox_path):
    """['sh','-c','echo x'] must raise SystemExit (sh no longer allowed)."""
    bench = _import_bench()
    with pytest.raises(SystemExit):
        bench._validate_command(["sh", "-c", "echo x"], sandbox_path)


# ---------------------------------------------------------------------------
# AC 9: -c rejected for python3/python.
# ---------------------------------------------------------------------------

def test_bench_rejects_python3_c(sandbox_path):
    """['python3','-c','import os'] must raise SystemExit (-c inline code)."""
    bench = _import_bench()
    with pytest.raises(SystemExit):
        bench._validate_command(["python3", "-c", "import os"], sandbox_path)


def test_bench_rejects_python_c(sandbox_path):
    """['python','-c','import os'] must raise SystemExit (-c inline code)."""
    bench = _import_bench()
    with pytest.raises(SystemExit):
        bench._validate_command(["python", "-c", "import os"], sandbox_path)


def test_bench_rejects_python3_c_anywhere(sandbox_path):
    """'-c' appearing later in the arg list must also be rejected."""
    bench = _import_bench()
    with pytest.raises(SystemExit):
        bench._validate_command(["python3", "-X", "dev", "-c", "import os"], sandbox_path)


# ---------------------------------------------------------------------------
# AC 10: legitimate fixture commands must still pass (non-regression).
# ---------------------------------------------------------------------------

def test_bench_allows_python3_script(sandbox_path):
    """['python3','run.py'] must not raise — a script invocation is legit."""
    bench = _import_bench()
    bench._validate_command(["python3", "run.py"], sandbox_path)


def test_bench_allows_pytest(sandbox_path):
    """['pytest','tests/'] must not raise."""
    bench = _import_bench()
    bench._validate_command(["pytest", "tests/"], sandbox_path)


def test_bench_allows_python3_config_flag(sandbox_path):
    """'--config' must NOT be treated as a '-c' match (exact-string, not
    substring). ['python3','--config','x','run.py'] must not raise."""
    bench = _import_bench()
    bench._validate_command(["python3", "--config", "x", "run.py"], sandbox_path)
