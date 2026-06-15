"""Central read-boundary policy for audit roles."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReadAttempt:
    """Represents an attempt to read a file via a tool.

    Fields:
        role: The executor role attempting the read
        task_dir: The task directory (or None for non-task operations)
        target: The resolved path being read
        tool_name: The tool being used ("Read", "Grep", or "Glob")
        raw_pattern: The raw pattern for Glob tool (None for Read/Grep)
    """
    role: str
    task_dir: Path | None
    target: Path
    tool_name: str  # "Read" | "Grep" | "Glob"
    raw_pattern: str | None


@dataclass(frozen=True)
class ReadDecision:
    """Represents a decision to allow or deny a read.

    Fields:
        allowed: True if the read is allowed, False otherwise
        reason: Human-readable explanation of the decision
        quota_used: Number of quota slots used (only for dead-code Grep quota)
    """
    allowed: bool
    reason: str
    quota_used: int | None


def decide_read(attempt: ReadAttempt, *, audit_plan: dict | None) -> ReadDecision:
    """Decide whether to allow a read based on role, task_dir, and allowlist.

    Decision tree (in order):
    1. Non-audit roles: fast-exit allow
    2. CLAUDE.md carve-out: allow for audit-claude-md only
    3. task_dir allowlist: allow if target inside task_dir
    4. diff_files allowlist: allow if target in audit_plan["diff_files"]
    5. Glob prefix matching: allow if pattern starts with task_dir prefix
    6. Dead-code Grep quota: allow up to 10, increment quota file
    7. Default deny
    """
    # Decision tree step 1: Non-audit roles bypass all checks
    if not attempt.role.startswith("audit-"):
        return ReadDecision(allowed=True, reason="non-audit role", quota_used=None)

    # Decision tree step 2: CLAUDE.md carve-out for audit-claude-md only.
    # Restricted to repo root, ~/.claude/, ~/.config/claude/ (sec-4 fix —
    # an unrestricted basename match allowed prompt-injection via attacker-
    # planted /tmp/evil/CLAUDE.md). Read-tool only — Globs/Greps don't
    # benefit from the basename match.
    if (
        attempt.role == "audit-claude-md"
        and attempt.tool_name == "Read"
        and attempt.target.name == "CLAUDE.md"
        and attempt.task_dir is not None
    ):
        try:
            target_resolved = attempt.target.resolve()
            allowed_roots = [
                attempt.task_dir.parent.parent.resolve(),
                (Path.home() / ".claude").resolve(),
                (Path.home() / ".config" / "claude").resolve(),
            ]
            for root in allowed_roots:
                try:
                    target_resolved.relative_to(root)
                    return ReadDecision(
                        allowed=True,
                        reason="audit-claude-md CLAUDE.md carve-out",
                        quota_used=None,
                    )
                except ValueError:
                    continue
        except Exception:
            pass

    # Decision tree step 3: task_dir allowlist
    if attempt.task_dir is not None:
        try:
            attempt.target.resolve().relative_to(attempt.task_dir.resolve())
            return ReadDecision(allowed=True, reason="inside task_dir", quota_used=None)
        except ValueError:
            # target is not relative to task_dir; continue to next check
            pass

    # Decision tree step 4: diff_files allowlist
    if audit_plan is not None and isinstance(audit_plan, dict):
        diff_files = audit_plan.get("diff_files")
        if isinstance(diff_files, list) and attempt.task_dir is not None:
            # Resolve each diff_files entry against task_dir.parent.parent (repo root)
            repo_root = attempt.task_dir.parent.parent
            for entry in diff_files:
                diff_path = (repo_root / entry).resolve()
                if diff_path == attempt.target.resolve():
                    return ReadDecision(
                        allowed=True,
                        reason="in diff_files allowlist",
                        quota_used=None,
                    )

    # Decision tree step 5: Glob prefix matching
    if attempt.tool_name == "Glob" and attempt.raw_pattern is not None:
        normalized = os.path.normpath(attempt.raw_pattern)
        if attempt.task_dir is not None:
            # Check if pattern starts with task_dir prefix
            try:
                task_dir_str = str(attempt.task_dir.resolve())
                norm_str = normalized
                # sec-2 fix: enforce directory boundary so task-XX-evil/* doesn't
                # match the prefix of task-XX/.
                if norm_str == task_dir_str or norm_str.startswith(task_dir_str + os.sep):
                    return ReadDecision(allowed=True, reason="glob task_dir prefix", quota_used=None)
            except Exception:
                pass

    # Decision tree step 6: Dead-code Grep quota
    if attempt.role == "audit-dead-code" and attempt.tool_name == "Grep":
        if attempt.task_dir is not None:
            return _handle_dead_code_grep_quota(attempt)

    # Decision tree step 7: Default deny
    return ReadDecision(
        allowed=False,
        reason=f"read-policy: target outside allowlist for role={attempt.role}",
        quota_used=None,
    )


def _handle_dead_code_grep_quota(attempt: ReadAttempt) -> ReadDecision:
    """Handle audit-dead-code Grep quota logic.

    - First 10 Greps allowed with quota_used incrementing
    - 11th+ Grep denied without mutating quota file
    - Unreadable quota file → fail-closed denial
    - FileNotFoundError on first read → treat as count=0
    """
    quota_file = attempt.task_dir / "audit-grep-quota.json"  # type: ignore[union-attr]

    try:
        # Try to read existing quota
        if quota_file.exists():
            try:
                quota_data = json.loads(quota_file.read_text())
                count = quota_data.get("count", 0)
            except (json.JSONDecodeError, OSError, PermissionError) as e:
                # Unreadable quota file → fail-closed
                return ReadDecision(
                    allowed=False,
                    reason="read-policy: quota state unreadable; denying repo-wide grep",
                    quota_used=None,
                )
        else:
            # File doesn't exist yet → treat as count=0
            count = 0

        # Check if quota exhausted
        if count >= 10:
            return ReadDecision(
                allowed=False,
                reason="read-policy: dead-code grep quota exhausted (10/10)",
                quota_used=None,
            )

        # Increment quota atomically
        new_count = count + 1
        try:
            # Write atomically via tempfile + os.replace
            with tempfile.NamedTemporaryFile(
                mode='w',
                dir=quota_file.parent,
                delete=False,
                suffix='.tmp',
            ) as tmp:
                json.dump({"count": new_count}, tmp)
                tmp_path = tmp.name

            # Atomic replace
            os.replace(tmp_path, quota_file)
        except Exception:
            # sec-3 / cq-001 fix: write failure must fail-closed, not silently
            # allow. The quota state is corrupt (slot consumed but not
            # persisted); subsequent calls would re-read the old count and
            # over-grant. Spec AC 9 mandates fail-closed on quota corruption.
            # AC 8: clean up the stray .tmp file if os.replace raised
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return ReadDecision(
                allowed=False,
                reason="read-policy: quota state unwritable; denying repo-wide grep",
                quota_used=None,
            )

        return ReadDecision(
            allowed=True,
            reason="dead-code quota slot consumed",
            quota_used=new_count,
        )
    except Exception:
        # Any unexpected exception → fail-closed
        return ReadDecision(
            allowed=False,
            reason="read-policy: quota state unreadable; denying repo-wide grep",
            quota_used=None,
        )


def _emit_read_policy_event(attempt: ReadAttempt, decision: ReadDecision) -> None:
    """Emit a read policy event to the event log.

    Mirrors write_policy._emit_policy_event:
    - Import lib_log lazily, swallow ImportError
    - If attempt.task_dir is None, return early
    - Emit "read_policy_allowed" or "read_policy_denied" event
    - Silently swallow all exceptions
    """
    if attempt.task_dir is None:
        return

    try:
        from lib_log import log_event
    except Exception:
        return

    try:
        root = attempt.task_dir.parent.parent
        task_id = attempt.task_dir.name

        # Compute relative path for the event
        rel = attempt.target.resolve()
        try:
            rel = rel.relative_to(root.resolve())
        except Exception:
            pass

        payload = {
            "role": attempt.role,
            "target": str(rel),
            "tool_name": attempt.tool_name,
            "reason": decision.reason,
        }

        if decision.allowed:
            log_event(root, "read_policy_allowed", task=task_id, **payload)
        else:
            log_event(root, "read_policy_denied", task=task_id, **payload)
    except Exception:
        pass
