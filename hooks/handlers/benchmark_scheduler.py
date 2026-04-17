"""Auto-trigger benchmark scheduler after task completion.

Runs at most one benchmark per task-completed event. The scheduler's
windowing logic (shadow_rebenchmark_task_window) prevents over-firing.
"""
import os
import subprocess
from pathlib import Path

EVENT_TYPE = "task-completed"

SCRIPT_DIR = Path(__file__).resolve().parent.parent
AUTO_PY = SCRIPT_DIR.parent / "sandbox" / "calibration" / "auto.py"


def run(root, payload):
    if not AUTO_PY.exists():
        return True  # nothing to do, not an error
    env = {**os.environ, "PYTHONPATH": f"{SCRIPT_DIR}:{os.environ.get('PYTHONPATH', '')}"}
    try:
        result = subprocess.run(
            ["python3", str(AUTO_PY), "run", "--root", str(root), "--limit", "1"],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0 and result.stderr:
            print(f"  [warn] benchmark_scheduler: {result.stderr[:200]}", file=__import__('sys').stderr)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as e:
        print(f"  [warn] benchmark_scheduler: {e}", file=__import__('sys').stderr)
        return False
