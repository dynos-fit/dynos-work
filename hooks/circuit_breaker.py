"""Circuit breaker for dynos-work tasks.

Encodes runtime conditions under which a task should be hard-aborted or
downgraded. Live-enforceable (EXECUTION) conditions:

    1. wasted_spawns_abort       (>= 3 wasted spawns; via ctl.py check-spawn-budget)
    2. small_task_token_overrun  (>= 1_000_000 tokens on a small bugfix/feature task)
    3. bugfix_token_overrun      (>= 5_000_000 tokens on a bugfix)
    4. small_task_token_downgrade  (>= 800_000 but < 1_000_000)
    5. bugfix_token_downgrade      (>= 4_000_000 but < 5_000_000)

Audit-time (AUDITING) condition:

    6. opus_auditor_zero_yield   (>= 3 opus audit spawns whose findings are [])

The module is callable but not wired into ctl.py / shell skills; integration
is a follow-up task.

Trust model
-----------
AUDITING arm: every audit-spawn event passing the phase/type/model filters is
cross-checked via _validate_audit_event_receipt before its findings are
counted. The helper verifies that a receipts/audit-{agent}.json exists and
that, when route_mode is not "generic", the sidecar sha256 matches the
receipt's injected_agent_sha256 field. Events failing validation are skipped
rather than counted, preventing forged token-usage.json entries from
triggering the zero-yield breaker. Closes SEC-CB-001 from task-20260507-003;
companion to PR 184's _SAFE_AGENT_RE addition.
EXECUTION arm: operator-trust boundary enforced by write_policy.py — cite
manifest.json deny, execution-graph.json wrapper-required, and
token-usage.json system-only blocks.

Temporal invariant: the breaker's AUDITING checkpoint runs only after all
auditors have returned and their receipts have been written. The receipt-
provenance cross-check therefore never races a legitimate audit's receipt
write — by the time the breaker checks, the receipts/_injected-auditor-
prompts/ sidecars and receipts/audit-<name>.json files are on disk for
every legitimate audit dispatched in this checkpoint cycle.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Resolved interpreter path — required by the repo-wide
# tests/test_no_raw_subprocess_python_git.py guard that forbids bare
# `"python3"` list-literals in hooks/.
_PYTHON3: str = shutil.which("python3") or sys.executable


# ---------------------------------------------------------------------------
# Module-level constants (AC-2..AC-6, AC-35, AC-36)
# ---------------------------------------------------------------------------

WASTED_SPAWN_ABORT_THRESHOLD: int = 3
SMALL_TASK_TOKEN_LIMIT: int = 1_000_000
BUGFIX_TOKEN_LIMIT: int = 5_000_000
OPUS_AUDITOR_ZERO_YIELD_THRESHOLD: int = 3
SMALL_TASK_FILES_THRESHOLD: int = 2
SMALL_TASK_TOKEN_DOWNGRADE_THRESHOLD: int = 800_000
BUGFIX_TOKEN_DOWNGRADE_THRESHOLD: int = 4_000_000


_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Internal helpers (no I/O at import time)
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Optional[dict]:
    """Best-effort JSON read; return None on missing/invalid JSON."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[circuit_breaker] failed to read {path}: {exc}", file=sys.stderr)
        return None


def _classification_type(manifest: dict) -> str:
    classification = manifest.get("classification") or {}
    if not isinstance(classification, dict):
        return ""
    value = classification.get("type")
    return value if isinstance(value, str) else ""


def _token_total(task_dir: Path) -> int:
    """Read token-usage.json total, falling back to input+output sum.

    Missing file → 0 (AC-23).
    """
    data = _read_json(task_dir / "token-usage.json")
    if not isinstance(data, dict):
        return 0
    total = data.get("total")
    if isinstance(total, int):
        return total
    if isinstance(total, float):
        return int(total)
    inp = data.get("total_input_tokens", 0)
    out = data.get("total_output_tokens", 0)
    try:
        return int(inp) + int(out)
    except (TypeError, ValueError):
        return 0


def _unique_files_count(task_dir: Path) -> Optional[int]:
    """Count distinct files_expected across all segments (AC-12).

    Returns None if execution-graph.json is missing/unreadable (AC-22).
    """
    graph_path = task_dir / "execution-graph.json"
    if not graph_path.exists():
        print(
            f"[circuit_breaker] execution-graph.json missing at {graph_path}; "
            "skipping small-task token condition.",
            file=sys.stderr,
        )
        return None
    graph = _read_json(graph_path)
    if not isinstance(graph, dict):
        return None
    segments = graph.get("segments")
    if not isinstance(segments, list):
        return 0
    unique: set[str] = set()
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        files = seg.get("files_expected") or []
        if not isinstance(files, list):
            continue
        for entry in files:
            if isinstance(entry, str):
                unique.add(entry)
    return len(unique)


def _check_spawn_budget(task_dir: Path) -> int:
    """Invoke ctl.py check-spawn-budget; return wasted-spawn count.

    On any failure (non-zero exit, bad JSON, missing field) log a warning
    and return 0 so the wasted_spawns arm does not fire spuriously.
    """
    cmd = [
        _PYTHON3,
        str(_REPO_ROOT / "hooks" / "ctl.py"),
        "check-spawn-budget",
        "--task-dir",
        str(task_dir),
    ]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"[circuit_breaker] check-spawn-budget subprocess failed: {exc}",
            file=sys.stderr,
        )
        return 0

    if completed.returncode != 0:
        print(
            "[circuit_breaker] check-spawn-budget exited "
            f"{completed.returncode}: {completed.stderr.strip()}",
            file=sys.stderr,
        )
        return 0

    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            f"[circuit_breaker] check-spawn-budget stdout not JSON: {exc}",
            file=sys.stderr,
        )
        return 0

    count = payload.get("count") if isinstance(payload, dict) else None
    if not isinstance(count, int):
        return 0
    return count


def _opus_zero_yield_count(task_dir: Path) -> int:
    """Count opus audit spawns whose audit-report has empty findings.

    Per AC-15, model is read from the per-event `model` field on the
    events array — not from `by_agent`. Per AC-16, an event without a
    matching audit-report file is skipped.
    """
    data = _read_json(task_dir / "token-usage.json")
    if not isinstance(data, dict):
        return 0
    events = data.get("events")
    if not isinstance(events, list):
        return 0

    audit_reports_dir = task_dir / "audit-reports"
    zero_yield = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("phase") != "audit":
            continue
        if event.get("type") != "spawn":
            continue
        if event.get("model") != "opus":
            continue
        # AC-6: receipt-provenance cross-check; absorbs the former inline
        # isinstance(agent, str) guard (now handled inside the helper).
        if not _validate_audit_event_receipt(event, task_dir):
            continue
        agent = event.get("agent")
        report = _lookup_audit_report(audit_reports_dir, agent)
        if report is None:
            # AC-16: missing audit report → skip this event
            continue
        findings = report.get("findings")
        if isinstance(findings, list) and len(findings) == 0:
            zero_yield += 1
    return zero_yield


_SAFE_AGENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_audit_event_receipt(event: dict, task_dir: Path) -> bool:
    """Cross-check an audit-spawn event against its on-disk receipt.

    Returns True iff:
      - event["agent"] is a non-empty string matching _SAFE_AGENT_RE
      - event["model"] is a non-empty string
      - a receipt file exists at receipts/audit-{agent}.json (primary) or
        receipts/audit-{agent}-auditor.json (fallback)
      - when receipt["injected_agent_sha256"] is a non-empty string, the
        sidecar at receipts/_injected-auditor-prompts/{agent}-{model}.sha256
        exists, is non-empty after strip, and its stripped content matches
        the receipt hash
      - when receipt["injected_agent_sha256"] is empty/missing AND
        receipt["route_mode"] == "generic", no sidecar is required

    Returns False on any validation failure; never raises.
    """
    # AC-2: validate agent name
    agent = event.get("agent")
    if not isinstance(agent, str) or not agent:
        return False
    if not _SAFE_AGENT_RE.match(agent):
        return False

    # Validate model field (needed for sidecar path construction).
    # Apply the same _SAFE_AGENT_RE clamp as agent — model is interpolated
    # into the sidecar filename ({agent}-{model}.sha256) and a path-traversal
    # value like "../../etc" would resolve outside _injected-auditor-prompts/.
    # Closes SEC-CB-CHK-001 from this task's checkpoint audit.
    model = event.get("model")
    if not isinstance(model, str) or not model:
        return False
    if not _SAFE_AGENT_RE.match(model):
        return False

    # AC-3: try primary then fallback receipt filename
    receipts_dir = task_dir / "receipts"
    primary = receipts_dir / f"audit-{agent}.json"
    fallback = receipts_dir / f"audit-{agent}-auditor.json"

    receipt: Optional[dict] = None
    if primary.exists():
        receipt = _read_json(primary)
    elif fallback.exists():
        receipt = _read_json(fallback)

    if not isinstance(receipt, dict):
        return False

    # AC-4 / AC-5: decide whether sidecar check is required
    injected_sha = receipt.get("injected_agent_sha256")
    route_mode = receipt.get("route_mode")

    if isinstance(injected_sha, str) and injected_sha:
        # Sidecar check is mandatory — compare contents
        sidecar_path = (
            receipts_dir
            / "_injected-auditor-prompts"
            / f"{agent}-{model}.sha256"
        )
        try:
            sidecar_content = sidecar_path.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if not sidecar_content:
            return False
        return sidecar_content == injected_sha

    # No injected_agent_sha256 (empty or missing)
    # AC-5: generic route skips sidecar requirement
    if route_mode == "generic":
        return True

    # Non-generic route with no injected sha is ambiguous; reject conservatively
    return False


def _lookup_audit_report(audit_reports_dir: Path, agent: str) -> Optional[dict]:
    """Find an audit report whose filename starts with the agent name.

    The audit-report writer commonly names files `<agent>.json` or
    `<agent>-<round>.json`; we accept either.

    The ``agent`` value originates from token-usage.json events which are
    operator-side state, but a malformed entry containing path separators,
    `..`, or glob metacharacters could escape ``audit_reports_dir`` (POSIX
    pathlib does not reject these in joins) or cause the glob to match
    unintended files. Reject any ``agent`` that does not match
    ``^[A-Za-z0-9_-]+$``.
    """
    if not audit_reports_dir.exists() or not audit_reports_dir.is_dir():
        return None
    if not isinstance(agent, str) or not _SAFE_AGENT_RE.match(agent):
        return None

    direct = audit_reports_dir / f"{agent}.json"
    if direct.exists():
        return _read_json(direct)

    try:
        candidates = sorted(audit_reports_dir.glob(f"{agent}*.json"))
    except OSError:
        return None
    for candidate in candidates:
        report = _read_json(candidate)
        if isinstance(report, dict):
            return report
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_circuit_breakers(task_dir: Path, stage: str) -> Optional[dict]:
    """Evaluate circuit-breaker conditions for the given task and stage.

    Returns:
        - None if no condition fires (or stage is irrelevant, or manifest
          is missing).
        - An abort dict with keys {abort, trigger, reason, limit, actual}
          if any abort condition fires.
        - A downgrade dict with keys {action, trigger, reason, suggestion,
          limit_warned, limit_abort, actual} if a downgrade tier fires.

    Does NOT mutate task files or call _apply_abort. The caller is
    responsible for invoking _apply_abort on a non-None return value.
    """
    if stage not in ("EXECUTION", "AUDITING"):
        return None  # AC-7

    task_dir = Path(task_dir)

    # AC-24: manifest.json missing → bail with stderr message.
    manifest = _read_json(task_dir / "manifest.json")
    if not isinstance(manifest, dict):
        print(
            f"[circuit_breaker] manifest.json missing or unreadable at {task_dir}; "
            "skipping circuit-breaker evaluation.",
            file=sys.stderr,
        )
        return None

    if stage == "AUDITING":
        return _evaluate_auditing(task_dir)

    # stage == "EXECUTION"
    return _evaluate_execution(task_dir, manifest)


def _evaluate_execution(task_dir: Path, manifest: dict) -> Optional[dict]:
    """Evaluate the EXECUTION-stage conditions in deterministic order.

    Order (per Implicit-Requirement, AC-9..AC-11, AC-37, AC-38):
        1. wasted_spawns_abort
        2. small_task_token_overrun
        3. bugfix_token_overrun
        4. small_task_token_downgrade
        5. bugfix_token_downgrade
    """
    # 1. wasted_spawns_abort (AC-9, AC-34)
    wasted = _check_spawn_budget(task_dir)
    if wasted >= WASTED_SPAWN_ABORT_THRESHOLD:
        return {
            "abort": True,
            "trigger": "wasted_spawns_abort",
            "reason": (
                "wasted spawn count reached the hard-abort threshold; "
                "the task is burning tokens with no useful output."
            ),
            "limit": WASTED_SPAWN_ABORT_THRESHOLD,
            "actual": wasted,
        }

    classification_type = _classification_type(manifest)
    tokens = _token_total(task_dir)
    unique_files = _unique_files_count(task_dir)

    is_small_eligible = (
        classification_type in {"bugfix", "feature"}
        and unique_files is not None
        and unique_files <= SMALL_TASK_FILES_THRESHOLD
    )

    # 2. small_task_token_overrun (AC-10) — inclusive at the limit so
    # boundary tokens == LIMIT triggers abort (not the downgrade tier
    # below, which is < LIMIT). Closes the AC-37/AC-38 boundary gap.
    if is_small_eligible and tokens >= SMALL_TASK_TOKEN_LIMIT:
        return {
            "abort": True,
            "trigger": "small_task_token_overrun",
            "reason": (
                "small task (<= 2 files, bugfix/feature) exceeded the 1M "
                "token ceiling; halting before further token burn."
            ),
            "limit": SMALL_TASK_TOKEN_LIMIT,
            "actual": tokens,
        }

    # 3. bugfix_token_overrun (AC-11) — inclusive at the limit (see
    # AC-37/AC-38 boundary fix above for the small-task case).
    if classification_type == "bugfix" and tokens >= BUGFIX_TOKEN_LIMIT:
        return {
            "abort": True,
            "trigger": "bugfix_token_overrun",
            "reason": (
                "bugfix task exceeded the 5M token ceiling; halting "
                "before unbounded burn."
            ),
            "limit": BUGFIX_TOKEN_LIMIT,
            "actual": tokens,
        }

    # 4. small_task_token_downgrade (AC-37)
    if (
        is_small_eligible
        and SMALL_TASK_TOKEN_DOWNGRADE_THRESHOLD
        <= tokens
        <= SMALL_TASK_TOKEN_LIMIT
    ):
        return {
            "action": "downgrade",
            "trigger": "small_task_token_downgrade",
            "reason": (
                "small task token usage crossed 80% of the 1M ceiling; "
                "downgrade remaining spawns before the hard abort."
            ),
            "suggestion": "downgrade remaining spawns from opus to sonnet/haiku",
            "limit_warned": SMALL_TASK_TOKEN_DOWNGRADE_THRESHOLD,
            "limit_abort": SMALL_TASK_TOKEN_LIMIT,
            "actual": tokens,
        }

    # 5. bugfix_token_downgrade (AC-38)
    if (
        classification_type == "bugfix"
        and BUGFIX_TOKEN_DOWNGRADE_THRESHOLD <= tokens <= BUGFIX_TOKEN_LIMIT
    ):
        return {
            "action": "downgrade",
            "trigger": "bugfix_token_downgrade",
            "reason": (
                "bugfix token usage crossed 80% of the 5M ceiling; "
                "downgrade remaining spawns before the hard abort."
            ),
            "suggestion": "downgrade remaining spawns from opus to sonnet/haiku",
            "limit_warned": BUGFIX_TOKEN_DOWNGRADE_THRESHOLD,
            "limit_abort": BUGFIX_TOKEN_LIMIT,
            "actual": tokens,
        }

    return None  # AC-8


def _evaluate_auditing(task_dir: Path) -> Optional[dict]:
    """Evaluate AUDITING-stage condition: opus zero-yield only (AC-13)."""
    zero_yield = _opus_zero_yield_count(task_dir)
    if zero_yield >= OPUS_AUDITOR_ZERO_YIELD_THRESHOLD:
        return {
            "abort": True,
            "trigger": "opus_auditor_zero_yield",
            "reason": (
                "three or more opus audit spawns produced empty findings; "
                "further opus audits are not yielding signal."
            ),
            "limit": OPUS_AUDITOR_ZERO_YIELD_THRESHOLD,
            "actual": zero_yield,
        }
    return None


def _apply_abort(task_dir: Path, decision: dict) -> None:
    """Persist the circuit-breaker decision to disk and (for aborts only)
    transition the task manifest to FAILED via ctl.py.

    Behavior is determined by the decision dict shape (AC-42):
      - abort=True            → escalation.md + [BUDGET-ABORT] log + transition FAILED
      - action=="downgrade"   → escalation.md + [BUDGET-DOWNGRADE] log only
      - anything else         → undefined; we no-op to be safe.
    """
    task_dir = Path(task_dir)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    is_abort = decision.get("abort") is True
    is_downgrade = (not is_abort) and decision.get("action") == "downgrade"

    if not is_abort and not is_downgrade:
        # Undefined behavior per AC-42; safest action is to do nothing.
        print(
            "[circuit_breaker] _apply_abort called with unrecognized decision shape; "
            "no action taken.",
            file=sys.stderr,
        )
        return

    trigger = str(decision.get("trigger", "unknown"))
    reason = str(decision.get("reason", ""))
    actual = decision.get("actual", "")
    if is_abort:
        limit = decision.get("limit", "")
        kind_heading = "Circuit Breaker Abort"
        log_tag = "[BUDGET-ABORT]"
        limit_block = f"- **Limit:** {limit}\n"
    else:
        limit_warned = decision.get("limit_warned", "")
        limit_abort = decision.get("limit_abort", "")
        suggestion = decision.get("suggestion", "")
        kind_heading = "Circuit Breaker Downgrade"
        log_tag = "[BUDGET-DOWNGRADE]"
        limit_block = (
            f"- **Warn threshold:** {limit_warned}\n"
            f"- **Abort threshold:** {limit_abort}\n"
            f"- **Suggestion:** {suggestion}\n"
        )

    escalation_path = task_dir / "escalation.md"
    log_path = task_dir / "execution-log.md"

    body = (
        f"# {kind_heading}\n\n"
        f"- **Trigger:** {trigger}\n"
        f"- **Reason:** {reason}\n"
        f"{limit_block}"
        f"- **Actual:** {actual}\n"
        f"- **Timestamp:** {timestamp}\n"
    )

    try:
        task_dir.mkdir(parents=True, exist_ok=True)
        escalation_path.write_text(body, encoding="utf-8")  # AC-18: overwrite
    except OSError as exc:
        print(
            f"[circuit_breaker] failed to write escalation.md at {escalation_path}: {exc}",
            file=sys.stderr,
        )

    log_line = f"{log_tag} {timestamp} trigger={trigger} reason={reason}\n"
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(log_line)
    except OSError as exc:
        print(
            f"[circuit_breaker] failed to append to execution-log.md: {exc}",
            file=sys.stderr,
        )

    if is_downgrade:
        return  # AC-42: do NOT transition on downgrade

    # AC-20: invoke ctl.py transition ... FAILED; swallow all errors.
    cmd = [
        _PYTHON3,
        str(_REPO_ROOT / "hooks" / "ctl.py"),
        "transition",
        "--task-dir",
        str(task_dir),
        "FAILED",
    ]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if completed.returncode != 0:
            print(
                f"[circuit_breaker] ctl.py transition FAILED returned "
                f"{completed.returncode}: {completed.stderr.strip()}",
                file=sys.stderr,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"[circuit_breaker] ctl.py transition FAILED raised: {exc}",
            file=sys.stderr,
        )


__all__ = [
    "WASTED_SPAWN_ABORT_THRESHOLD",
    "SMALL_TASK_TOKEN_LIMIT",
    "BUGFIX_TOKEN_LIMIT",
    "OPUS_AUDITOR_ZERO_YIELD_THRESHOLD",
    "SMALL_TASK_FILES_THRESHOLD",
    "SMALL_TASK_TOKEN_DOWNGRADE_THRESHOLD",
    "BUGFIX_TOKEN_DOWNGRADE_THRESHOLD",
    "check_circuit_breakers",
    "_apply_abort",
]
