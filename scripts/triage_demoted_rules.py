"""scripts/triage_demoted_rules.py — task-20260508-007 seg-2.

Triage all 15 prevention-rules entries carrying a _demote_reason field:
  - DELETE 8 past-AC / unrestorable rules
  - RE-PROMOTE 5 rules to their correct active templates
  - KEEP-PENDING 2 rules (rewrite _demote_reason to actionable recipe)

The mutation is a single atomic write under fcntl.LOCK_EX, replicating
the pattern from memory/postmortem_analysis.py lines 843-924.

Public API (imported by tests):
  REGISTRY_PATH   — Path constant
  apply_mutations — pure function; no file I/O
  compute_triage_doc — renders triage-decisions.md content
  main            — entry point; file I/O + subprocess calls
"""
from __future__ import annotations

import copy
import datetime
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

REGISTRY_PATH: Path = Path.home() / ".dynos/projects/Users-hassam-Documents-dynos-work/prevention-rules.json"

# repo root: two levels up from scripts/
_REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# lib_core import (load_json, write_json, now_iso)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(_REPO_ROOT / "hooks"))
from lib_core import load_json, write_json  # noqa: E402

# ---------------------------------------------------------------------------
# Mutation tables — exactly the 15 target rule_ids
# ---------------------------------------------------------------------------

_DELETE_IDS: frozenset[str] = frozenset({
    "sec-e91392f0686e",
    "test-aa258399ad2e",
    "test-fc380dbf5cf3",
    "test-d0c6fe4832c2",
    "sec-92f009e1851f",
    "sec-2bf3c6cd183c",
    "test-3d8b06cdfc58",
    "sec-dbbfd6d46b79",
})

# Maps rule_id -> (new_template, new_params); _demote_reason is removed.
_RE_PROMOTE_MAP: dict[str, dict[str, Any]] = {
    "proc-1b48ed8447a4": {
        "template": "caller_count_required",
        "params": {"symbol": "_normalize_auditor_key", "scope": "hooks/**/*.py", "min_count": 1},
    },
    "sec-caeca6dc4d41": {
        "template": "caller_count_required",
        "params": {"symbol": "_persistent_project_dir", "scope": "hooks/receipts/*.py", "min_count": 1},
    },
    "proc-e2deebce9e55": {
        "template": "caller_count_required",
        "params": {"symbol": "run_checks", "scope": "hooks/**/*.py", "min_count": 2},
    },
    "proc-1b8219ff2da0": {
        # stays advisory; only _demote_reason is removed; template/params unchanged
        "template": None,  # None = preserve existing
        "params": None,    # None = preserve existing
    },
    "test-15f7da751ace": {
        "template": "signature_lock",
        "params": {
            "module": "hooks.rules_engine",
            "function": "run_checks",
            "expected_params": ["root", "mode", "rule_filter", "raw_rules"],
        },
    },
}

# Maps rule_id -> exact new _demote_reason string; all other fields unchanged.
_KEEP_PENDING_MAP: dict[str, str] = {
    "sec-e019ea060c46": (
        "Re-promote as caller_count_required scope=hooks/circuit_breaker.py "
        "symbol=verify_signed_events min_count=1 once residual "
        "9afc5266-8949-4b9a-9ce7-3e141857b2e1 (SEC-CB-001) ships. "
        "Do not re-promote until that residual is closed."
    ),
    "sec-344b09c453df": (
        "Re-promote as pattern_must_not_appear or caller_count_required targeting "
        "co-hardening of circuit_breaker signed-event reads with policy_engine reads, "
        "once residual 9afc5266-8949-4b9a-9ce7-3e141857b2e1 (SEC-CB-001) ships and "
        "the function-level co-modification substrate exists. "
        "Do not re-promote until that residual is closed."
    ),
}

_ALL_TARGET_IDS: frozenset[str] = _DELETE_IDS | frozenset(_RE_PROMOTE_MAP) | frozenset(_KEEP_PENDING_MAP)

# rule_ids that trigger recovery if they fire violations after re-promotion
_RECOVERY_RULE_IDS: frozenset[str] = frozenset({
    "proc-1b48ed8447a4",
    "sec-caeca6dc4d41",
    "proc-e2deebce9e55",
    "proc-1b8219ff2da0",
    "test-15f7da751ace",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(rule_dict: dict) -> str:
    return hashlib.sha256(json.dumps(rule_dict, sort_keys=True).encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# apply_mutations — pure function, no file I/O
# ---------------------------------------------------------------------------

def apply_mutations(rules: list[dict]) -> tuple[list[dict], list[dict]]:
    """Apply all triage mutations to the rule list.

    Args:
        rules: Full list of rule dicts (not mutated in place).

    Returns:
        (mutated_rules, audit_rows)
        mutated_rules: new list with 8 deletes, 5 re-promotes, 2 keep-pending
        audit_rows: 15 dicts — one per target rule_id — with pre/post audit fields
    """
    mutated_rules: list[dict] = []
    audit_rows: list[dict] = []

    for rule in rules:
        rule_id = rule.get("rule_id", "")

        if rule_id in _DELETE_IDS:
            pre_hash = _sha256(rule)
            audit_rows.append({
                "rule_id": rule_id,
                "decision": "DELETE",
                "pre_template": rule.get("template"),
                "pre_params": rule.get("params"),
                "pre_demote_reason": rule.get("_demote_reason"),
                "pre_hash": pre_hash,
                "post_template": None,
                "post_params": None,
                "post_demote_reason": None,
                "post_demote_reason_present": False,
                "post_hash": "DELETED",
            })
            # Do not append to mutated_rules → deletion

        elif rule_id in _RE_PROMOTE_MAP:
            pre_hash = _sha256(rule)
            mutation = _RE_PROMOTE_MAP[rule_id]

            new_rule = copy.deepcopy(rule)
            # Apply template/params only if they are specified (non-None)
            if mutation["template"] is not None:
                new_rule["template"] = mutation["template"]
            if mutation["params"] is not None:
                new_rule["params"] = mutation["params"]
            # Remove _demote_reason unconditionally for all re-promotes
            new_rule.pop("_demote_reason", None)

            post_hash = _sha256(new_rule)
            audit_rows.append({
                "rule_id": rule_id,
                "decision": "RE-PROMOTE",
                "pre_template": rule.get("template"),
                "pre_params": rule.get("params"),
                "pre_demote_reason": rule.get("_demote_reason"),
                "pre_hash": pre_hash,
                "post_template": new_rule.get("template"),
                "post_params": new_rule.get("params"),
                "post_demote_reason": None,
                "post_demote_reason_present": False,
                "post_hash": post_hash,
            })
            mutated_rules.append(new_rule)

        elif rule_id in _KEEP_PENDING_MAP:
            pre_hash = _sha256(rule)
            new_demote_reason = _KEEP_PENDING_MAP[rule_id]

            new_rule = copy.deepcopy(rule)
            new_rule["_demote_reason"] = new_demote_reason

            post_hash = _sha256(new_rule)
            audit_rows.append({
                "rule_id": rule_id,
                "decision": "KEEP-PENDING",
                "pre_template": rule.get("template"),
                "pre_params": rule.get("params"),
                "pre_demote_reason": rule.get("_demote_reason"),
                "pre_hash": pre_hash,
                "post_template": new_rule.get("template"),
                "post_params": new_rule.get("params"),
                "post_demote_reason": new_demote_reason,
                "post_demote_reason_present": True,
                "post_hash": post_hash,
            })
            mutated_rules.append(new_rule)

        else:
            # Non-target rule: pass through unchanged
            mutated_rules.append(rule)

    return mutated_rules, audit_rows


# ---------------------------------------------------------------------------
# compute_triage_doc — renders Markdown for triage-decisions.md
# ---------------------------------------------------------------------------

def compute_triage_doc(
    audit_rows: list[dict],
    pre_count: int,
    post_count: int,
    check_output: str,
) -> str:
    """Render the triage-decisions Markdown document.

    Args:
        audit_rows: 15 audit dicts from apply_mutations
        pre_count: rule count before mutations (expected 209)
        post_count: rule count after mutations (expected 201)
        check_output: stdout from `rules_engine.py check --mode all`

    Returns:
        Markdown string suitable for writing to triage-decisions.md
    """
    lines: list[str] = [
        "# Triage Decisions — task-20260508-007",
        "",
        "## Summary",
        "",
        f"- Pre-mutation rule count: **{pre_count}**",
        f"- Post-mutation rule count: **{post_count}**",
        f"- Deleted: {len([r for r in audit_rows if r['decision'] == 'DELETE'])}",
        f"- Re-promoted: {len([r for r in audit_rows if r['decision'] == 'RE-PROMOTE'])}",
        f"- Keep-pending: {len([r for r in audit_rows if r['decision'] == 'KEEP-PENDING'])}",
        "",
        "## Per-rule Audit Trail",
        "",
    ]

    for row in audit_rows:
        rule_id = row["rule_id"]
        decision = row["decision"]
        lines.append(f"### {rule_id}  —  {decision}")
        lines.append("")
        lines.append("**Pre-state**")
        lines.append(f"- template: `{row['pre_template']}`")
        lines.append(f"- params: `{json.dumps(row['pre_params'], sort_keys=True)}`")
        if row["pre_demote_reason"] is not None:
            lines.append(f"- _demote_reason: {row['pre_demote_reason']}")
        lines.append(f"- pre_hash: `{row['pre_hash']}`")
        lines.append("")

        if decision == "DELETE":
            lines.append("**Post-state:** DELETED")
            lines.append(f"- post_hash: `DELETED`")
        else:
            lines.append("**Post-state**")
            lines.append(f"- template: `{row['post_template']}`")
            lines.append(f"- params: `{json.dumps(row['post_params'], sort_keys=True)}`")
            if row["post_demote_reason_present"]:
                lines.append(f"- _demote_reason: {row['post_demote_reason']}")
            else:
                lines.append("- _demote_reason: (absent)")
            lines.append(f"- post_hash: `{row['post_hash']}`")

        lines.append("")

    lines.append("## check --mode all output (filtered to re-promoted rule_ids)")
    lines.append("")
    lines.append("```")
    lines.append(check_output if check_output else "(none)")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    lock_path = REGISTRY_PATH.parent / "prevention-rules.json.lock"

    # Step 1: Acquire LOCK_EX
    lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            # Step 2: Load and validate pre-state
            data = load_json(REGISTRY_PATH)
            rules: list[dict] = data.get("rules", [])

            if len(rules) != 209:
                print(
                    f"ERROR: Expected 209 rules in pre-state, found {len(rules)}. Halting.",
                    file=sys.stderr,
                )
                sys.exit(1)

            present_ids = {r.get("rule_id") for r in rules}
            missing_ids = _ALL_TARGET_IDS - present_ids
            if missing_ids:
                print(
                    f"ERROR: The following expected rule_ids are missing from the registry: "
                    f"{sorted(missing_ids)}. Halting.",
                    file=sys.stderr,
                )
                sys.exit(1)

            # Step 3: Apply mutations (pure)
            mutated_rules, audit_rows = apply_mutations(rules)

            # Step 4: Assert post-state count
            if len(mutated_rules) != 201:
                print(
                    f"ERROR: Expected 201 rules after mutation, got {len(mutated_rules)}. Halting.",
                    file=sys.stderr,
                )
                sys.exit(1)

            # Step 5: Write atomically
            write_json(REGISTRY_PATH, {"rules": mutated_rules, "updated_at": _now_iso()})

        finally:
            # Step 6: Release LOCK_EX
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    # Step 7: validate-rules
    result_validate = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "hooks" / "rules_engine.py"), "validate-rules"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(_REPO_ROOT),
    )
    if result_validate.returncode != 0:
        print(
            f"ERROR: validate-rules exited {result_validate.returncode}.\n"
            f"stdout: {result_validate.stdout}\nstderr: {result_validate.stderr}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Step 8: check --mode all
    result_check = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "hooks" / "rules_engine.py"), "check", "--mode", "all"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(_REPO_ROOT),
    )
    check_output = result_check.stdout

    # Step 9: Parse violations for the 5 re-promoted rule_ids; recover if any
    offending_ids = {
        rid for rid in _RECOVERY_RULE_IDS
        if any(rid in line for line in check_output.splitlines())
    }

    if offending_ids:
        # Recovery: re-acquire lock, restore _demote_reason + template=advisory for offenders
        print(
            f"WARNING: check --mode all fired on re-promoted rule_ids: {sorted(offending_ids)}. "
            "Executing recovery.",
            file=sys.stderr,
        )
        lock_fd2 = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd2, "w") as lock_fh2:
            fcntl.flock(lock_fh2.fileno(), fcntl.LOCK_EX)
            try:
                data2 = load_json(REGISTRY_PATH)
                current_rules = data2.get("rules", [])

                # For each offending id, restore _demote_reason and set template=advisory
                # We need the original pre-state values from audit_rows
                pre_state_by_id = {row["rule_id"]: row for row in audit_rows}

                recovered_rules: list[dict] = []
                for r in current_rules:
                    rid = r.get("rule_id", "")
                    if rid in offending_ids and rid in pre_state_by_id:
                        row = pre_state_by_id[rid]
                        r = copy.deepcopy(r)
                        r["template"] = "advisory"
                        r["_demote_reason"] = (
                            f"post-execution falsification: check --mode all fired; "
                            f"original _demote_reason was: {row['pre_demote_reason']}"
                        )
                    recovered_rules.append(r)

                write_json(REGISTRY_PATH, {"rules": recovered_rules, "updated_at": _now_iso()})
            finally:
                fcntl.flock(lock_fh2.fileno(), fcntl.LOCK_UN)

        # Write triage-decisions.md noting recovery
        recovery_note = (
            f"RECOVERY EXECUTED: rules {sorted(offending_ids)} restored to advisory. "
            "See recovery path in script for details."
        )
        pre_count = 209
        post_count = len(mutated_rules)
        doc = compute_triage_doc(audit_rows, pre_count, post_count, check_output + "\n" + recovery_note)
        triage_doc_path = _REPO_ROOT / ".dynos/task-20260508-007/triage-decisions.md"
        triage_doc_path.write_text(doc)
        print(f"Recovery note written to {triage_doc_path}", file=sys.stderr)
        sys.exit(1)

    # Step 10: Compute triage doc
    pre_count = 209
    post_count = len(mutated_rules)

    # Filter check output to re-promoted rule_ids for clarity
    filtered_check_lines = [
        line for line in check_output.splitlines()
        if any(rid in line for rid in _RECOVERY_RULE_IDS)
    ]
    filtered_check_output = "\n".join(filtered_check_lines) if filtered_check_lines else "(no violations from re-promoted rule_ids)"

    doc = compute_triage_doc(audit_rows, pre_count, post_count, filtered_check_output)

    # Step 11: Write triage-decisions.md
    triage_doc_path = _REPO_ROOT / ".dynos/task-20260508-007/triage-decisions.md"
    triage_doc_path.parent.mkdir(parents=True, exist_ok=True)
    triage_doc_path.write_text(doc)

    # Step 12: Print success summary
    print(
        f"triage_demoted_rules: SUCCESS\n"
        f"  pre-state count : {pre_count}\n"
        f"  post-state count: {post_count}\n"
        f"  deleted         : 8\n"
        f"  re-promoted     : 5\n"
        f"  keep-pending    : 2\n"
        f"  validate-rules  : exit 0\n"
        f"  check --mode all: {len(offending_ids)} violations from re-promoted rule_ids (expected 0)\n"
        f"  triage doc      : {triage_doc_path}"
    )


if __name__ == "__main__":
    main()
