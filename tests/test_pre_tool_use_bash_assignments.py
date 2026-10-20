"""D2: same-command literal shell assignments in Bash write destinations.

Auditors write `S=<report path>; cat > "$S" <<'JSON' ... JSON`. The hook
used to resolve the literal token `$S` against cwd and deny it as "path
escapes task boundary" — an error the auditor could not act on. These tests
pin the new contract:

* AC5 — `_extract_bash_destinations` substitutes counted same-command
  literal assignments (export-prefixed allowed, last one before the
  destination wins, one level of nesting, heredoc bodies ignored) and never
  consults os.environ.
* AC6 — any destination still containing `$` is denied fail-closed with
  reason `unresolved_shell_variable` and the canonical literal report path,
  never "path escapes task boundary"; bare relative audit writes stay denied
  by write_policy with the same hint appended.
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
from write_policy import (  # noqa: E402
    CANONICAL_AUDIT_REPORT_PATH,
    WriteAttempt,
    decide_write,
)

CANONICAL_LITERAL = ".dynos/task-{id}/audit-reports/{auditor_name}-{model}-attempt-{n}.json"


# ---------------------------------------------------------------------------
# AC5: substitution
# ---------------------------------------------------------------------------

def test_canonical_path_constant_is_the_spec_literal() -> None:
    assert CANONICAL_AUDIT_REPORT_PATH == CANONICAL_LITERAL


def test_simple_assignment_with_heredoc_is_substituted() -> None:
    command = (
        "S=.dynos/task-x/audit-reports/a.json; cat > \"$S\" <<'JSON'\n"
        '{"findings": []}\n'
        "JSON"
    )
    assert _extract_bash_destinations(command) == [".dynos/task-x/audit-reports/a.json"]


def test_export_and_nested_braced_assignment_is_substituted() -> None:
    command = (
        "export D=.dynos/task-x/audit-reports; F=\"$D/a.json\"; cat > \"${F}\""
    )
    assert _extract_bash_destinations(command) == [".dynos/task-x/audit-reports/a.json"]


def test_newline_separated_assignment_is_substituted() -> None:
    command = "S=.dynos/task-x/audit-reports/a.json\ncat > \"$S\""
    assert _extract_bash_destinations(command) == [".dynos/task-x/audit-reports/a.json"]


def test_last_assignment_before_destination_wins() -> None:
    command = "S=first.json; S=second.json; cat > \"$S\"; S=third.json"
    assert _extract_bash_destinations(command) == ["second.json"]


def test_assignment_after_destination_is_not_used() -> None:
    assert _extract_bash_destinations("cat > \"$S\"; S=late.json") == ["$S"]


def test_heredoc_body_assignment_is_ignored() -> None:
    command = (
        "cat > \"$S\" <<'EOF'\n"
        "S=.dynos/task-x/audit-reports/a.json\n"
        "EOF"
    )
    assert _extract_bash_destinations(command) == ["$S"]


def test_os_environ_is_never_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S", ".dynos/task-x/audit-reports/a.json")
    assert _extract_bash_destinations("cat > \"$S\" <<'JSON'\n{}\nJSON") == ["$S"]
    assert _extract_bash_destinations("cp src.json ${S}") == ["${S}"]


def test_nested_substitution_is_one_level_only() -> None:
    # A's value is substituted when B is assigned; the result is not rescanned.
    command = "A=1 B=$A/x; cat > \"$B\""
    assert _extract_bash_destinations(command) == ["1/x"]


@pytest.mark.parametrize(
    "command",
    [
        # Prefix assignment only scopes the command's env; $S is the outer S.
        "S=a.json cat > \"$S\"",
        # Append modifies S beyond what we can see literally.
        "S=a; S+=/../x; cat > \"$S\"",
        # Function body / trap / read / unset can rewrite S at run time.
        "f() { S=/etc; }; S=ok; f; cat > \"$S\"",
        "S=ok; trap \"S=/etc\" DEBUG; cat > \"$S\"",
        "S=ok; read S; cat > \"$S\"",
        # Conditional or subshell assignments are not certain.
        "true && S=bad; cat > \"$S\"",
        "(S=bad); cat > \"$S\"",
        # Values the shell would expand differently than the literal.
        "S=\"a b\"; cp x $S",
        "S=~/x; cat > \"$S\"",
        "S=*.json; cat > $S",
        "S=$(pwd)/x; cat > \"$S\"",
        # Literal dollars / eval / IFS defeat static analysis.
        "S=ok; echo '$S' > \"$S\"",
        "S=ok; eval x; cat > \"$S\"",
        "S=ok; IFS=/; cat > $S",
        # Parameter-expansion operators are not substituted.
        "S=ok; cat > \"${S:-y}\"",
    ],
)
def test_uncertain_assignments_leave_dollar_in_place(command: str) -> None:
    dests = _extract_bash_destinations(command)
    assert dests and all("$" in d for d in dests), dests


def test_variable_name_is_matched_greedily() -> None:
    assert _extract_bash_destinations("S=a; cat > \"$S_x\"") == ["$S_x"]


# ---------------------------------------------------------------------------
# AC6: fail-closed denial
# ---------------------------------------------------------------------------

def _make_task(tmp_path: Path, name: str) -> Path:
    task_dir = tmp_path / ".dynos" / name
    (task_dir / "audit-reports").mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({"stage": "CHECKPOINT_AUDIT"}))
    return task_dir


def _run_hook(
    monkeypatch: pytest.MonkeyPatch, task_dir: Path, role: str, command: str, cwd: Path
) -> tuple[int, str, list[dict]]:
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
    return code, stderr.getvalue(), events


def test_unresolved_variable_denied_with_literal_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _make_task(tmp_path, "task-20261019-901")
    # Even with an allowed path in the environment, $S is never expanded.
    monkeypatch.setenv("S", str(task_dir / "audit-reports" / "a.json"))
    resolve_calls: list[str] = []
    real_resolve = pre_tool_use._resolve_path

    def _spy(raw, cwd):
        resolve_calls.append(raw)
        return real_resolve(raw, cwd)

    monkeypatch.setattr(pre_tool_use, "_resolve_path", _spy)
    code, err, events = _run_hook(
        monkeypatch, task_dir, "audit-security", "cat > \"$S\" <<'JSON'\n{}\nJSON", tmp_path
    )
    assert code == 2
    assert "unresolved_shell_variable" in err
    assert "'$S'" in err
    assert CANONICAL_LITERAL in err
    assert "path escapes task boundary" not in err
    assert "$S" not in resolve_calls, "unresolved token must never reach _resolve_path"
    checks = [e for e in events if e["event"] == "pre_tool_use_bash_check"]
    assert checks, events
    assert checks[-1]["allowed"] is False
    assert checks[-1]["reason"] == "unresolved_shell_variable"
    assert checks[-1]["destination"] == "$S"


def test_command_substitution_destination_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _make_task(tmp_path, "task-20261019-902")
    code, err, _ = _run_hook(
        monkeypatch, task_dir, "audit-security", "S=$(pwd)/a.json; cat > \"$S\"", tmp_path
    )
    assert code == 2
    assert "unresolved_shell_variable" in err
    assert "path escapes task boundary" not in err


def test_legacy_fallback_dollar_destination_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _make_task(tmp_path, "task-20261019-903")
    # Unbalanced quote -> legacy regex fallback; its `$` token is still denied.
    code, err, _ = _run_hook(
        monkeypatch, task_dir, "audit-security", "echo 'oops > $OUT", tmp_path
    )
    assert code == 2
    assert "unresolved_shell_variable" in err


def test_assigned_canonical_report_write_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _make_task(tmp_path, "task-20261019-904")
    report = f".dynos/{task_dir.name}/audit-reports/security-auditor-opus-attempt-1.json"
    command = f"S={report}; cat > \"$S\" <<'JSON'\n{{}}\nJSON"
    code, err, events = _run_hook(monkeypatch, task_dir, "audit-security", command, tmp_path)
    assert code == 0, err
    checks = [e for e in events if e["event"] == "pre_tool_use_bash_check"]
    assert checks and checks[-1]["allowed"] is True


def test_assigned_path_outside_task_still_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _make_task(tmp_path, "task-20261019-905")
    code, err, _ = _run_hook(
        monkeypatch, task_dir, "audit-security", "S=elsewhere.json; cat > \"$S\"", tmp_path
    )
    assert code == 2
    assert "path escapes task boundary" in err
    assert CANONICAL_LITERAL in err


def test_bare_relative_audit_write_denied_with_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _make_task(tmp_path, "task-20261019-906")
    code, err, _ = _run_hook(
        monkeypatch, task_dir, "audit-security", "cat > report.json <<'JSON'\n{}\nJSON", tmp_path
    )
    assert code == 2
    assert "path escapes task boundary" in err
    assert CANONICAL_LITERAL in err

    decision = decide_write(
        WriteAttempt(
            role="audit-code-quality",
            task_dir=task_dir,
            path=tmp_path / "report.json",
            operation="modify",
            source="agent",
        )
    )
    assert decision.allowed is False
    assert decision.reason.startswith("path escapes task boundary")
    assert CANONICAL_LITERAL in decision.reason


def test_non_audit_boundary_denial_text_unchanged(tmp_path: Path) -> None:
    task_dir = _make_task(tmp_path, "task-20261019-907")
    decision = decide_write(
        WriteAttempt(
            role="planning",
            task_dir=task_dir,
            path=tmp_path / "report.json",
            operation="modify",
            source="agent",
        )
    )
    assert decision.allowed is False
    assert decision.reason == "path escapes task boundary"


# ---------------------------------------------------------------------------
# Audit repair (cycle 1): dynamic-name builtins, task scoping, observability
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        # Builtins that set a variable by a name computed at run time can
        # retarget S after the hook substituted it (sec-001 / tm-001).
        "S=.dynos/t/a.json; declare $'\\x53'=/etc/x; cat > \"$S\"",
        "S=.dynos/t/a.json; typeset S=/etc/x; cat > \"$S\"",
        "S=.dynos/t/a.json; local S=/etc/x; cat > \"$S\"",
        "S=.dynos/t/a.json; readonly S; cat > \"$S\"",
        "S=.dynos/t/a.json; printf -v S /etc/x; cat > \"$S\"",
        "S=.dynos/t/a.json; mapfile -t S < list; cat > \"$S\"",
        "S=.dynos/t/a.json; readarray S < list; cat > \"$S\"",
        "S=.dynos/t/a.json; let x=1; cat > \"$S\"",
        "S=.dynos/t/a.json; getopts a S; cat > \"$S\"",
        "S=.dynos/t/a.json; builtin read S; cat > \"$S\"",
        "S=.dynos/t/a.json; command read S; cat > \"$S\"",
        "S=.dynos/t/a.json; set -- x; cat > \"$S\"",
        "S=.dynos/t/a.json; export -n S; cat > \"$S\"",
        # Indirect expansion and locale quoting build names the text hides.
        "S=.dynos/t/a.json; N=S; cat > \"${!N}\"",
        "S=.dynos/t/a.json; echo $\"x\"; cat > \"$S\"",
    ],
)
def test_dynamic_name_builtins_disable_substitution(command: str) -> None:
    dests = _extract_bash_destinations(command)
    assert dests and all("$" in d for d in dests), dests


def test_plain_printf_does_not_disable_substitution() -> None:
    command = "S=.dynos/t/a.json; printf '%s\\n' x > \"$S\""
    assert _extract_bash_destinations(command) == [".dynos/t/a.json"]


def _run_hook_no_task_env(
    monkeypatch: pytest.MonkeyPatch, role: str | None, command: str, cwd: Path
) -> tuple[int, str, list[dict]]:
    events: list[dict] = []

    def _record(root, event_type, *, task=None, **payload):
        events.append({"event": event_type, "task": task, **payload})

    monkeypatch.setattr(lib_log, "log_event", _record)
    monkeypatch.setattr(pre_tool_use, "log_event", _record, raising=False)
    monkeypatch.delenv("DYNOS_TASK_DIR", raising=False)
    if role is None:
        monkeypatch.delenv("DYNOS_ROLE", raising=False)
    else:
        monkeypatch.setenv("DYNOS_ROLE", role)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = pre_tool_use.main()
    return code, stderr.getvalue(), events


@pytest.mark.parametrize(
    "command",
    ["echo hi > \"$TMPDIR/x.txt\"", "cp a.txt \"$(mktemp)\"", "echo hi > \"$HOME/notes.txt\""],
)
def test_variable_destination_outside_task_passes_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    # A directory with no .dynos: no task context, so no boundary to enforce
    # (api-001 / arch-001). Before D2 these commands were allowed.
    code, err, _events = _run_hook_no_task_env(monkeypatch, None, command, tmp_path)
    assert code == 0, err
    assert "unresolved_shell_variable" not in err


def test_literal_task_prefix_resolves_task_and_logs_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two active tasks and no DYNOS_TASK_DIR: ancestor lookup is ambiguous,
    # but the destination's literal prefix names the task, so the denial is
    # task-scoped and its event is recorded (obs-001).
    task_dir = _make_task(tmp_path, "task-20261019-801")
    _make_task(tmp_path, "task-20261019-802")
    command = f"cat > \"{task_dir}/audit-reports/$NAME.json\" <<'JSON'\n{{}}\nJSON"
    code, err, events = _run_hook_no_task_env(monkeypatch, "audit-security", command, tmp_path)
    assert code == 2, err
    assert "unresolved_shell_variable" in err
    checks = [
        e for e in events
        if e["event"] == "pre_tool_use_bash_check" and e.get("reason") == "unresolved_shell_variable"
    ]
    assert checks, events
    assert checks[-1]["task"] == task_dir.name
    assert checks[-1]["allowed"] is False
