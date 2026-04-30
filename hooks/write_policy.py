"""Central write-boundary policy for task and control-plane artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


WriteOperation = Literal["create", "modify", "delete"]
WriteSource = Literal[
    "agent",
    "inline",
    "ctl",
    "scheduler",
    "receipt-writer",
    "eventbus",
    "system",
]
WriteMode = Literal["direct", "wrapper", "deny"]

# Maps each privileged role to the set of modules whose call chain may claim it.
# ctl is included in receipt-writer because cmd_amend_artifact writes amendment
# receipts directly without routing through lib_receipts.write_receipt.
# lib_core is included in ctl because write_ctl_json is the canonical ctl-write
# wrapper used by lib_validate and other helpers that lack a direct ctl import.
# lib_log and lib_tokens are included in system because role='system' is the
# global-fallback write role used by their top-level event/token write paths.
_PRIVILEGED_ROLE_MODULE_MAP: dict[str, frozenset[str]] = {
    "eventbus": frozenset({"lib_log"}),
    "receipt-writer": frozenset({"lib_receipts", "ctl", "router"}),
    "ctl": frozenset({"ctl", "lib_core"}),
    "scheduler": frozenset({"scheduler"}),
    "system": frozenset({"lib_log", "lib_tokens"}),
}

# PRO-001: per-role capability-key sentinels replace sys._getframe stack-walking.
# Each privileged role gets a distinct object() identity that callers must
# import and pass explicitly. Forge-resistance equals Python object identity:
# without importing this module's private symbol, no caller can construct a
# matching token. The role-to-module mapping in _PRIVILEGED_ROLE_MODULE_MAP
# remains the policy source of truth for write permissions; the capability
# key only proves caller identity.
_CAPABILITY_KEYS: dict[str, object] = {
    role: object() for role in _PRIVILEGED_ROLE_MODULE_MAP
}


def get_capability_key(role: str) -> object:
    """Return the capability-key sentinel for a privileged role.

    Raises KeyError if role is not in _CAPABILITY_KEYS. Privileged callers
    must import this and pass the returned sentinel as the capability_key
    kwarg of require_write_allowed.
    """
    return _CAPABILITY_KEYS[role]


@dataclass(frozen=True)
class WriteAttempt:
    role: str
    task_dir: Path | None
    path: Path
    operation: WriteOperation
    source: WriteSource


@dataclass(frozen=True)
class WriteDecision:
    allowed: bool
    reason: str
    mode: WriteMode
    wrapper_command: str | None = None


_CONTROL_PLANE_EXACT = frozenset({
    "manifest.json",
    "external-solution-gate.json",
    "token-usage.json",
    "events.jsonl",
})

_WRAPPER_REQUIRED = {
    "execution-graph.json": "python3 hooks/ctl.py write-execution-graph <task_dir> --from <json>",
    "repair-log.json": "python3 hooks/ctl.py write-repair-log <task_dir> --from <json>",
    "classification.json": "python3 hooks/ctl.py write-classification <task_dir> --from <json>",
}


def _task_relative(path: Path, task_dir: Path | None) -> Path | None:
    if task_dir is None:
        return None
    try:
        return path.resolve().relative_to(task_dir.resolve())
    except Exception:
        return None


def _matches_role(role: str, pattern: str) -> bool:
    if pattern.endswith("*"):
        return role.startswith(pattern[:-1])
    return role == pattern


def is_control_plane_path(path: Path, task_dir: Path | None) -> bool:
    rel = _task_relative(path, task_dir)
    if rel is None:
        return False
    rel_posix = rel.as_posix()
    if rel_posix in _CONTROL_PLANE_EXACT:
        return True
    if rel_posix in _WRAPPER_REQUIRED:
        return True
    if rel_posix.startswith("receipts/"):
        return True
    if rel_posix.startswith("handoff-") and rel_posix.endswith(".json"):
        return True
    return False


def allowed_globs_for_role(role: str, task_dir: Path | None) -> list[str]:
    if _matches_role(role, "planning"):
        return [
            ".dynos/task-*/discovery-notes.md",
            ".dynos/task-*/design-decisions.md",
            ".dynos/task-*/spec.md",
            ".dynos/task-*/plan.md",
            # NOTE: classification.json and execution-graph.json require the ctl
            # wrapper — planning cannot write them directly. Use:
            #   python3 hooks/ctl.py write-classification <task_dir> --from <json>
            #   python3 hooks/ctl.py write-execution-graph <task_dir> --from <json>
            ".dynos/task-*/classification.json (via ctl wrapper only)",
            ".dynos/task-*/execution-graph.json (via ctl wrapper only)",
        ]
    if _matches_role(role, "repair-coordinator"):
        return [".dynos/task-*/repair-log.json"]
    if _matches_role(role, "audit-*"):
        return [".dynos/task-*/audit-reports/*.json"]
    if role == "execute-inline" or role.endswith("-executor"):
        return [
            "repo-owned source/test/doc/config files",
            ".dynos/task-*/evidence/*.md",
        ]
    if role == "ctl":
        return [
            ".dynos/task-*/manifest.json",
            ".dynos/task-*/external-solution-gate.json",
            ".dynos/task-*/handoff-*.json",
            ".dynos/task-*/classification.json",
            ".dynos/task-*/execution-graph.json",
            ".dynos/task-*/repair-log.json",
        ]
    if role == "receipt-writer":
        return [
            ".dynos/task-*/receipts/*.json",
            ".dynos/task-*/token-usage.json",
        ]
    if role in {"scheduler", "eventbus", "system"}:
        return [
            ".dynos/task-*/events.jsonl",
            ".dynos/events.jsonl",
            ".dynos/task-*/token-usage.json",
        ]
    return []


def decide_write(attempt: WriteAttempt) -> WriteDecision:
    path = attempt.path.resolve()
    rel = _task_relative(path, attempt.task_dir)
    rel_posix = rel.as_posix() if rel is not None else None

    if rel_posix is not None and rel_posix in _WRAPPER_REQUIRED:
        if attempt.role == "ctl":
            return WriteDecision(True, f"{rel_posix} persisted by ctl wrapper", "direct")
        return WriteDecision(
            False,
            f"{rel_posix} must be persisted through ctl wrapper",
            "wrapper",
            _WRAPPER_REQUIRED[rel_posix],
        )

    if rel_posix == "manifest.json":
        if attempt.role == "ctl":
            return WriteDecision(True, "manifest.json is code-owned ctl state", "direct")
        return WriteDecision(False, "manifest.json is code-owned control-plane state", "deny")

    if rel_posix == "external-solution-gate.json":
        if attempt.role == "ctl":
            return WriteDecision(True, "external-solution-gate.json is ctl-owned control-plane state", "direct")
        return WriteDecision(False, "external-solution-gate.json is code-owned control-plane state", "deny")

    if rel_posix == "token-usage.json":
        if attempt.role in {"system", "receipt-writer"}:
            return WriteDecision(True, "token-usage.json is system-owned usage state", "direct")
        return WriteDecision(False, "token-usage.json is code-owned control-plane state", "deny")

    if rel_posix == "events.jsonl":
        if attempt.role in {"eventbus", "system"}:
            return WriteDecision(True, "events.jsonl is eventbus/system-owned log state", "direct")
        return WriteDecision(False, "events.jsonl is code-owned control-plane state", "deny")

    if rel_posix is not None and rel_posix.startswith("receipts/"):
        if attempt.role == "receipt-writer":
            return WriteDecision(True, "receipts are receipt-writer-owned control-plane state", "direct")
        return WriteDecision(False, "receipts are code-owned control-plane state", "deny")

    if rel_posix is not None and rel_posix.startswith("handoff-") and rel_posix.endswith(".json"):
        if attempt.role == "ctl":
            return WriteDecision(True, "handoff json is ctl-owned control-plane state", "direct")
        return WriteDecision(False, "handoff json is code-owned control-plane state", "deny")

    if rel_posix is not None and rel_posix.startswith("audit-reports/"):
        if attempt.role.startswith("audit-"):
            return WriteDecision(True, "audit report is auditor-owned evidence", "direct")
        return WriteDecision(False, "audit reports are reserved for auditor roles", "deny")

    if rel_posix is not None and rel_posix.startswith("evidence/"):
        if attempt.role == "execute-inline" or attempt.role.endswith("-executor"):
            return WriteDecision(True, "evidence markdown is executor-owned", "direct")
        return WriteDecision(False, "evidence artifacts are reserved for executor roles", "deny")

    if rel_posix is None:
        # Path lives outside the task dir. Executor and planning roles
        # legitimately write repo artifacts (src/, tests/, docs/) as part of
        # their work — those are NOT "task boundary escapes." System roles
        # (scheduler/eventbus/system) likewise need the global log path.
        # Only reject out-of-task paths when the role has no authority to
        # write repo artifacts.
        if attempt.role == "execute-inline" or attempt.role.endswith("-executor"):
            return WriteDecision(True, "repo work artifact allowed for executor role", "direct")
        if attempt.role in {"scheduler", "eventbus", "system"}:
            return WriteDecision(True, "non-task system path allowed", "direct")
        if attempt.task_dir is not None:
            return WriteDecision(False, "path escapes task boundary", "deny")
        return WriteDecision(False, "non-task path requires system-owned writer", "deny")
    # plan.md and spec.md are human-approved artifacts locked at PLAN_REVIEW.
    # Executor roles must not mutate them — renaming or removing sections
    # bypasses gap-analysis and plan-validation gates without triggering any
    # downstream hash mismatch (the plan-validated receipt can be refreshed).
    # The planning role retains write access so it can author these files
    # before approval; after approval only ctl/operator paths should touch them.
    if rel_posix in {"plan.md", "spec.md"}:
        if attempt.role == "execute-inline" or attempt.role.endswith("-executor"):
            return WriteDecision(
                False,
                f"{rel_posix} is a human-approved artifact; executor roles may not write it after PLAN_REVIEW",
                "deny",
            )

    # ctl-owned aggregation artifacts at task root — explicit deny with a clear
    # reason so agents receive a useful message instead of the catch-all below.
    if rel_posix in {"audit-summary.json", "audit-plan.json"}:
        if attempt.role != "ctl":
            return WriteDecision(
                False,
                f"{rel_posix} is a ctl-owned aggregation artifact written by "
                "`python3 hooks/ctl.py run-audit-setup` / `run-audit-summary`; "
                "agents must not write it directly",
                "deny",
            )

    if attempt.role == "execute-inline" or attempt.role.endswith("-executor"):
        return WriteDecision(True, "repo work artifact allowed for executor role", "direct")
    if attempt.role == "planning":
        if rel_posix in {"discovery-notes.md", "design-decisions.md", "spec.md", "plan.md"}:
            return WriteDecision(True, f"{rel_posix} is planner-owned judgment output", "direct")
        return WriteDecision(False, "planning role may only write planning artifacts", "deny")
    if attempt.role == "repair-coordinator":
        return WriteDecision(False, "repair-coordinator may only persist repair-log via wrapper", "wrapper", _WRAPPER_REQUIRED["repair-log.json"])
    if attempt.role == "ctl":
        return WriteDecision(True, "ctl direct write allowed", "direct")
    if attempt.role in {"scheduler", "eventbus", "system", "receipt-writer"}:
        return WriteDecision(True, f"{attempt.role} write allowed", "direct")
    return WriteDecision(False, f"no write policy matched for role={attempt.role}", "deny")


def _emit_policy_event(attempt: WriteAttempt, decision: WriteDecision) -> None:
    if attempt.task_dir is None:
        return
    try:
        from lib_log import log_event
    except Exception:
        return
    try:
        root = attempt.task_dir.parent.parent
        task_id = attempt.task_dir.name
        rel = attempt.path.resolve()
        try:
            rel = rel.relative_to(root.resolve())
        except Exception:
            pass
        payload = {
            "role": attempt.role,
            "path": str(rel),
            "operation": attempt.operation,
            "reason": decision.reason,
        }
        if decision.mode == "direct":
            log_event(root, "write_policy_allowed", task=task_id, mode=decision.mode, **payload)
        elif decision.mode == "wrapper":
            log_event(
                root,
                "write_policy_wrapper_required",
                task=task_id,
                wrapper_command=decision.wrapper_command,
                **payload,
            )
        else:
            log_event(root, "write_policy_denied", task=task_id, **payload)
    except Exception:
        pass


def require_write_allowed(
    attempt: WriteAttempt, *, capability_key: object, emit_event: bool = True
) -> None:
    # PRO-001: identity-check the capability key against the per-role sentinel.
    # For privileged roles (in _CAPABILITY_KEYS) the key must match the dict
    # entry. For non-privileged roles the key must be None — there is no
    # registered sentinel to match. Either path is enforced by the same
    # identity check: dict.get returns None for missing roles, so non-privileged
    # callers passing capability_key=None pass; non-privileged callers passing
    # any non-None key fail.
    if capability_key is not _CAPABILITY_KEYS.get(attempt.role):
        raise ValueError(
            f"capability_key mismatch for role {attempt.role!r}: "
            f"token does not match the registered sentinel"
        )
    decision = decide_write(attempt)
    if emit_event:
        _emit_policy_event(attempt, decision)
    if not decision.allowed:
        raise ValueError(decision.reason)


def find_write_violations(
    *,
    role: str,
    task_dir: Path | None,
    paths: list[Path],
    source: WriteSource,
    operation: WriteOperation = "modify",
) -> list[str]:
    """Return human-readable violations for a batch of touched paths."""
    violations: list[str] = []
    for path in paths:
        attempt = WriteAttempt(
            role=role,
            task_dir=task_dir,
            path=path,
            operation=operation,
            source=source,
        )
        decision = decide_write(attempt)
        _emit_policy_event(attempt, decision)
        if not decision.allowed:
            label = str(path)
            if task_dir is not None:
                rel = _task_relative(path.resolve(), task_dir)
                if rel is not None:
                    label = rel.as_posix()
            violations.append(f"{label}: {decision.reason}")
    return violations
