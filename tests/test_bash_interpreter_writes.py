"""Best-effort interpreter write detection + same-command `$T` resolution.

7.6.0 field report:

* bug 2 — the orchestrator's Edit of docs/follow-ups/T19b.md was denied at
  REPAIR_EXECUTION, but `python3 -c "open('docs/...','w')..."` and a
  heredoc'd script wrote the same file unchallenged: the Bash pre-filter
  only saw redirects and argv write verbs. Common literal-path interpreter
  and in-place-editor writes are now extracted as destinations. This is
  still best-effort (computed paths stay invisible).
* `$T` — `set -euo pipefail; T=.dynos/task-x; ... > "$T/_scratch/f"` and
  `T=...; awk '{print $1}' ... > "$T/f"` were denied as
  unresolved_shell_variable because `set` / any single-quoted `$` disabled
  same-command substitution globally.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import lib_log  # noqa: E402
import pre_tool_use  # noqa: E402
from pre_tool_use import _extract_bash_destinations  # noqa: E402

DOC = "docs/follow-ups/T19b.md"


@pytest.mark.parametrize(
    "command",
    [
        f"python3 -c \"open('{DOC}','w').write('x')\"",
        f"python3 -c \"open('{DOC}', mode='a').write('x')\"",
        f"python -I -c \"from pathlib import Path; Path('{DOC}').write_text('x')\"",
        f"python3 -c \"import shutil; shutil.copy('a.md', '{DOC}')\"",
        f"python3 -c \"import os; os.replace('a.md', '{DOC}')\"",
        f"python3 - <<'PY'\nopen('{DOC}', 'w').write('x')\nPY",
        f"python3 <<'PY'\nfrom pathlib import Path\nPath('{DOC}').write_text('x')\nPY",
        f"cat <<'PY' | python3 -\nopen('{DOC}', 'w')\nPY",
        f"bash <<'SH'\necho x > {DOC}\nSH",
        f"node -e \"require('fs').writeFileSync('{DOC}', 'x')\"",
        f"node -e \"fs.appendFileSync('{DOC}', 'x')\"",
        f"node -e \"fs.copyFileSync('a.md', '{DOC}')\"",
        f"ruby -e \"File.write('{DOC}', 'x')\"",
        f"perl -e 'open(my $fh, \">\", \"{DOC}\")'",
        f"perl -e 'open(FH, \">>{DOC}\")'",
        f"perl -pi -e 's/a/b/' {DOC}",
        f"perl -i.bak -pe 's/a/b/' {DOC}",
        f"sed -i 's/a/b/' {DOC}",
        f"sed -i '' 's/a/b/' {DOC}",
        f"sed -i.bak -e 's/a/b/' {DOC}",
        f"sed -Ei 's/a/b/' {DOC}",
        f"sed --in-place 's/a/b/' {DOC}",
        f"dd if=/dev/zero of={DOC} count=1",
        f"echo x | tee {DOC}",
        f"cp a.md {DOC}",
        f"mv a.md {DOC}",
        f"install -m 644 a.md {DOC}",
    ],
)
def test_interpreter_and_in_place_writes_are_destinations(command: str) -> None:
    assert DOC in _extract_bash_destinations(command)


@pytest.mark.parametrize(
    "command",
    [
        f"python3 -c \"print(open('{DOC}').read())\"",
        f"python3 -c \"open('{DOC}', 'rb').read()\"",
        "python3 -c \"import json; print(json.load(open('a.json')))\"",
        f"sed -n 's/a/b/p' {DOC}",
        f"sed 's/a/b/' {DOC}",
        f"perl -ne 'print' {DOC}",
        f"perl -e 'open(my $fh, \"<\", \"{DOC}\")'",
        f"perl -e 'open(my $fh, \"+<\", \"x\")'",
        f"cat <<'EOF' > notes.txt\nopen('{DOC}', 'w')\nEOF",
        "python3 hooks/ctl.py run-audit-setup .dynos/task-x",
    ],
)
def test_reads_and_data_heredocs_are_not_destinations(command: str) -> None:
    assert DOC not in _extract_bash_destinations(command)


def test_computed_interpreter_paths_stay_invisible() -> None:
    # Documented limitation: best effort covers literal paths only.
    assert _extract_bash_destinations("python3 -c \"p='x'; open(p,'w')\"") == []


def test_heredoc_body_dollar_is_never_substituted() -> None:
    command = "T=.dynos/task-x; python3 - <<PY\nopen('$T/f', 'w')\nPY"
    assert "$T/f" in _extract_bash_destinations(command)


def test_inline_code_double_quoted_dollar_is_substituted() -> None:
    command = "T=.dynos/task-x; python3 -c \"open('$T/f','w')\""
    assert _extract_bash_destinations(command) == [".dynos/task-x/f"]


# ---------------------------------------------------------------------------
# `$T` same-command resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "set -euo pipefail; T=.dynos/task-x; echo x > \"$T/_scratch/f\"",
        "set -e\nT=.dynos/task-x\necho x > $T/_scratch/f",
        "set -o pipefail; T=.dynos/task-x; echo x > $T/_scratch/f",
        "T=.dynos/task-x; awk '{print $1}' a > \"$T/_scratch/f\"",
        "T=.dynos/task-x; sed 's/$//' a > \"$T/_scratch/f\"",
        "T=.dynos/task-x; command -v jq >/dev/null; jq . a > \"$T/_scratch/f\"",
        "T=.dynos/task-x && echo x > \"${T}/_scratch/f\"",
        "T=.dynos/task-x; jq . a.json > \"$T/_scratch/f\"",
    ],
)
def test_same_command_assignment_resolves(command: str) -> None:
    assert _extract_bash_destinations(command) == [".dynos/task-x/_scratch/f"]


@pytest.mark.parametrize(
    "command",
    [
        # Literal `$T` (single-quoted / escaped) must still poison T.
        "T=.dynos/task-x; echo '$T' > \"$T/f\"",
        "T=.dynos/task-x; echo \\$T > \"$T/f\"",
        # `set --` / operands keep disabling substitution.
        "T=.dynos/task-x; set -- a; echo > \"$T/f\"",
        "T=.dynos/task-x; set -x foo; echo > \"$T/f\"",
        # `.` in command position sources a file that can rewrite T.
        "T=.dynos/task-x; . ./env.sh; echo > \"$T/f\"",
        # `command read` can still rewrite T.
        "T=.dynos/task-x; command read T; echo > \"$T/f\"",
        # Never assigned in this command (an earlier Bash call is invisible).
        "echo > \"$T/f\"",
        "T=$(ls -d .dynos/task-*); echo > \"$T/f\"",
    ],
)
def test_uncertain_assignments_still_unresolved(command: str) -> None:
    dests = _extract_bash_destinations(command)
    assert dests and all("$" in d for d in dests), dests


# ---------------------------------------------------------------------------
# End to end through pre_tool_use.main()
# ---------------------------------------------------------------------------

def _make_task(tmp_path: Path, stage: str) -> Path:
    task_dir = tmp_path / ".dynos" / "task-20261031-001"
    (task_dir / "_scratch").mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({"stage": stage}))
    return task_dir


def _run_hook(monkeypatch, task_dir: Path, role: str, command: str, cwd: Path):
    events: list[dict] = []

    def _record(root, event_type, *, task=None, **payload):
        events.append({"event": event_type, "task": task, **payload})

    monkeypatch.setattr(lib_log, "log_event", _record)
    monkeypatch.setattr(pre_tool_use, "log_event", _record, raising=False)
    monkeypatch.setenv("DYNOS_ROLE", role)
    monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = pre_tool_use.main()
    return code, stderr.getvalue()


@pytest.mark.parametrize(
    "command",
    [
        f"python3 -c \"open('{DOC}','w').write('x')\"",
        f"python3 - <<'PY'\nopen('{DOC}', 'w').write('x')\nPY",
        f"sed -i 's/a/b/' {DOC}",
    ],
)
@pytest.mark.parametrize("stage", ["REPAIR_EXECUTION", "CHECKPOINT_AUDIT"])
def test_orchestrator_interpreter_write_to_repo_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, stage: str
) -> None:
    task_dir = _make_task(tmp_path, stage)
    code, err = _run_hook(monkeypatch, task_dir, "orchestrator", command, tmp_path)
    assert code == 2, err
    assert "orchestrator may not write repo files" in err


def test_orchestrator_scratch_write_with_set_e_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _make_task(tmp_path, "REPAIR_EXECUTION")
    command = f"set -euo pipefail; T={task_dir}; echo x > \"$T/_scratch/notes.txt\""
    code, err = _run_hook(monkeypatch, task_dir, "orchestrator", command, tmp_path)
    assert code == 0, err


def test_unresolved_denial_explains_earlier_call_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _make_task(tmp_path, "REPAIR_EXECUTION")
    code, err = _run_hook(
        monkeypatch, task_dir, "orchestrator", "echo x > \"$T/_scratch/f\"", tmp_path
    )
    assert code == 2
    assert "unresolved_shell_variable" in err
    assert "EARLIER Bash call" in err
