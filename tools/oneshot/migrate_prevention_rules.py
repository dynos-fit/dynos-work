#!/usr/bin/env python3
"""Migrate legacy free-text prevention rules to template-based engine schema.

This script converts the historical prevention-rules.json (free-text `rule`
strings, optional `enforcement` hint) into the structured template/params
format consumed by `hooks.rules_engine`.

Auto-classifier:
    Deterministic regex match on `rule.text + enforcement` selects exactly one
    template. Multiple-template-match → ambiguous. Zero-match → ambiguous.

Modes:
    --dry-run         : print before/after diff to stdout, do NOT write file.
    --non-interactive : ambiguous rules become {template: "advisory"};
                        ambiguous count summarised to stderr.
    interactive (default):
        ambiguous rules prompt operator with numbered candidate list,
        plus 'a' (advisory), 's' (skip — keep raw rule untouched),
        'q' (abort migration). EOF on stdin → 's'. KeyboardInterrupt → exit 1.

Migration log:
    tools/migration-logs/migration-{YYYY-MM-DD-HHMMSS}.json
    Always written, even on --dry-run, so re-runs can be reasoned about.
    (On abort the partial log is still flushed.)

Exit codes:
    0  success (migration completed or --dry-run finished)
    1  operator aborted ('q' selection, KeyboardInterrupt)
    2  file I/O failure (cannot read rules / cannot write rules / cannot
       write log)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# hooks/ lives next to tools/. Add it to sys.path so we can import lib_core
# and rules_engine without forcing a package install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _REPO_ROOT / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

from lib_core import _persistent_project_dir  # noqa: E402
from rules_engine import TEMPLATE_SCHEMAS  # noqa: E402


# ---------------------------------------------------------------------------
# Auto-classifier
# ---------------------------------------------------------------------------


def _normalise_text(rule: dict) -> str:
    """Return the lowercase concatenation of rule.text + enforcement."""
    parts = [
        str(rule.get("rule") or ""),
        str(rule.get("text") or ""),
        str(rule.get("enforcement") or ""),
        str(rule.get("rationale") or ""),
        str(rule.get("category") or ""),
    ]
    return " ".join(p for p in parts if p).lower()


def _signature_all_satisfies_callable(text: str, rule: dict) -> Optional[dict]:
    """Rule mentions __all__ AND (callable OR defined) →
    every_name_in_X_satisfies_Y w/ container=__all__, predicate=callable.
    """
    has_all = "__all__" in text
    has_callable_or_defined = (
        re.search(r"\bcallable\b", text) is not None
        or re.search(r"\bdefin(ed|ition|itions)\b", text) is not None
    )
    if not (has_all and has_callable_or_defined):
        return None
    return {
        "template": "every_name_in_X_satisfies_Y",
        "params": {
            "module": "TODO_MODULE",
            "container": "__all__",
            "predicate": "callable",
        },
        "_classifier": "all+callable/defined",
    }


def _signature_handler_set(text: str, rule: dict) -> Optional[dict]:
    """Rule mentions handler-set + register →
    every_name_in_X_satisfies_Y w/ container=_LEARNING_HANDLERS.
    """
    handler_words = ("handler", "handlers", "_handlers")
    has_handler = any(w in text for w in handler_words)
    has_register = re.search(r"\bregister(ed)?\b", text) is not None
    if not (has_handler and has_register):
        return None
    return {
        "template": "every_name_in_X_satisfies_Y",
        "params": {
            "module": "TODO_MODULE",
            "container": "_LEARNING_HANDLERS",
            "predicate": "callable",
        },
        "_classifier": "handler+register",
    }


_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _signature_backtick_pattern(text: str, rule: dict) -> Optional[dict]:
    """Rule mentions explicit regex-looking pattern in backticks →
    pattern_must_not_appear.
    """
    raw_text = str(rule.get("rule") or rule.get("text") or "")
    matches = _BACKTICK_RE.findall(raw_text)
    if not matches:
        return None
    # Heuristic: at least one backtick token contains a regex
    # metachar OR ends in `()` (a function-call signature).
    candidates = [
        m for m in matches
        if re.search(r"[\\.\*\+\?\(\)\[\]\^\$]", m) or m.endswith("()")
    ]
    if not candidates:
        return None
    pattern = candidates[0]
    # Convert literal `time.time()` style into a regex.
    if not any(c in pattern for c in r"\^$*+?[]"):
        regex_src = re.escape(pattern)
    else:
        regex_src = pattern
    return {
        "template": "pattern_must_not_appear",
        "params": {
            "regex": regex_src,
            "scope": "**/*.py",
        },
        "_classifier": "backtick-pattern",
    }


def _signature_time_time(text: str, rule: dict) -> Optional[dict]:
    """Rule mentions `time.time()` AND throttles → pattern_must_not_appear
    regex `\\btime\\.time\\(\\)`.
    """
    has_time_time = "time.time()" in text or "time.time" in text
    has_throttle_word = any(
        w in text
        for w in ("throttle", "throttles", "ttl", "rate limit", "rate-limit", "monotonic")
    )
    if not (has_time_time and has_throttle_word):
        return None
    return {
        "template": "pattern_must_not_appear",
        "params": {
            "regex": r"\btime\.time\(\)",
            "scope": "**/*.py",
        },
        "_classifier": "time.time()+throttle",
    }


def _signature_o_nofollow_lock(text: str, rule: dict) -> Optional[dict]:
    """Rule mentions `O_NOFOLLOW` AND lock → pattern_must_not_appear (forbids
    plain `open(lock_path` patterns in lock-related code).
    """
    has_nofollow = "o_nofollow" in text or "nofollow" in text
    has_lock = "lock" in text
    if not (has_nofollow and has_lock):
        return None
    return {
        "template": "pattern_must_not_appear",
        "params": {
            # Catches the anti-pattern: plain open() of a *lock* file.
            "regex": r"open\([^)]*lock[^)]*\)",
            "scope": "**/*.py",
        },
        "_classifier": "O_NOFOLLOW+lock",
    }


def _signature_signature_lock(text: str, rule: dict) -> Optional[dict]:
    """Rule mentions signature/params/inspect → signature_lock."""
    keywords = ("signature", "inspect.signature", "ordered param", "parameter list")
    has_sig = any(k in text for k in keywords)
    if not has_sig:
        return None
    # Need both a function reference and a parameter mention to be
    # confident; otherwise the term "signature" might mean something else.
    if not (re.search(r"\bparam(s|eter|eters)?\b", text) or "inspect" in text):
        return None
    return {
        "template": "signature_lock",
        "params": {
            "module": "TODO_MODULE",
            "function": "TODO_FUNCTION",
            "expected_params": [],
        },
        "_classifier": "signature+params/inspect",
    }


def _signature_caller_count(text: str, rule: dict) -> Optional[dict]:
    """Rule mentions caller count or `>=N` uses → caller_count_required."""
    if re.search(r"\bcaller(s|-count| count)?\b", text):
        return {
            "template": "caller_count_required",
            "params": {
                "symbol": "TODO_SYMBOL",
                "scope": "**/*.py",
                "min_count": 1,
            },
            "_classifier": "caller-count",
        }
    if re.search(r">=\s*\d+\s*(use|uses|call|calls|caller|callers)", text):
        return {
            "template": "caller_count_required",
            "params": {
                "symbol": "TODO_SYMBOL",
                "scope": "**/*.py",
                "min_count": 1,
            },
            "_classifier": "caller-count-N",
        }
    return None


def _signature_import_constant_only(text: str, rule: dict) -> Optional[dict]:
    """Rule mentions string literal/constant only in X → import_constant_only."""
    has_literal_or_constant = any(
        w in text for w in ("string literal", "literal", "constant", "constants")
    )
    has_only_in = bool(
        re.search(r"only (in|allowed in|defined in|imported)", text)
        or "single source of truth" in text
    )
    if not (has_literal_or_constant and has_only_in):
        return None
    return {
        "template": "import_constant_only",
        "params": {
            "literal_pattern": "TODO_PATTERN",
            "allowed_files": ["TODO_FILE.py"],
        },
        "_classifier": "literal+only-in",
    }


def _signature_co_modification(text: str, rule: dict) -> Optional[dict]:
    """Rule mentions co-modify/spec amendment → co_modification_required."""
    keywords = (
        "co-modif", "co modif", "co-amend", "spec amendment", "spec amendments",
        "same commit", "co-modification", "co-edit", "co edit",
    )
    if not any(k in text for k in keywords):
        return None
    return {
        "template": "co_modification_required",
        "params": {
            "trigger_glob": "TODO_TRIGGER_GLOB",
            "must_modify_glob": "TODO_MUST_MODIFY_GLOB",
        },
        "_classifier": "co-modify/spec-amendment",
    }


# Order matters only for stable diagnostics; logic still runs them all.
_SIGNATURES = [
    ("all+callable", _signature_all_satisfies_callable),
    ("handler+register", _signature_handler_set),
    ("backtick-pattern", _signature_backtick_pattern),
    ("time.time()+throttle", _signature_time_time),
    ("O_NOFOLLOW+lock", _signature_o_nofollow_lock),
    ("signature+params", _signature_signature_lock),
    ("caller-count", _signature_caller_count),
    ("import-constant-only", _signature_import_constant_only),
    ("co-modify", _signature_co_modification),
]


def classify(rule: dict) -> tuple[str, list[dict]]:
    """Return (status, candidates) for a single raw rule.

    status:
        "single"    — exactly one template matched; candidates has 1 entry.
        "ambiguous" — zero or many candidates; operator must choose.
    """
    text = _normalise_text(rule)
    candidates: list[dict] = []
    for _name, fn in _SIGNATURES:
        try:
            cand = fn(text, rule)
        except Exception:
            cand = None
        if cand is not None:
            candidates.append(cand)
    if len(candidates) == 1:
        return ("single", candidates)
    return ("ambiguous", candidates)


# ---------------------------------------------------------------------------
# Migration core
# ---------------------------------------------------------------------------


def _strip_classifier_meta(template_dict: dict) -> dict:
    """Remove auto-classifier debug fields from a template dict."""
    out = dict(template_dict)
    out.pop("_classifier", None)
    return out


_RULE_ID_RE = re.compile(r"[^a-z0-9]+")


def _derive_rule_id(rule: dict, idx: int) -> str:
    """Derive a stable rule_id for a legacy rule that lacks one."""
    existing = rule.get("rule_id") or rule.get("id")
    if existing:
        return str(existing)
    src = rule.get("source_finding") or rule.get("source_task") or ""
    cat = rule.get("category") or "rule"
    base = f"{cat}-{src}-{idx}".lower()
    base = _RULE_ID_RE.sub("-", base).strip("-")
    return base or f"rule-{idx}"


def _build_migrated(
    rule: dict,
    template_dict: dict,
    idx: int,
) -> dict:
    """Construct the migrated rule dict, preserving legacy metadata."""
    template_dict = _strip_classifier_meta(template_dict)
    migrated = {
        "rule_id": _derive_rule_id(rule, idx),
        "template": template_dict["template"],
        "params": template_dict.get("params", {}),
        "severity": str(rule.get("severity", "error")),
        "source_finding": str(rule.get("source_finding", "")),
        "rationale": str(rule.get("rationale", "")),
    }
    # Preserve legacy fields that are non-engine but useful for audit trail.
    for k in ("category", "executor", "added_at", "source_task"):
        if k in rule:
            migrated[k] = rule[k]
    # Keep the original free-text rule under a separate field for traceability.
    if rule.get("rule"):
        migrated["legacy_text"] = rule["rule"]
    return migrated


def _diff_lines(before: list[dict], after: list[dict]) -> list[str]:
    """Return human-readable per-rule before/after diff lines."""
    lines = []
    for i, (b, a) in enumerate(zip(before, after)):
        b_summary = (b.get("rule") or b.get("text") or "")[:80]
        a_template = a.get("template", "?")
        a_params = json.dumps(a.get("params", {}), sort_keys=True)
        lines.append(f"--- rule {i} ---")
        lines.append(f"  before: {b_summary!r}")
        lines.append(f"  after : template={a_template} params={a_params}")
    return lines


def _prompt_for_choice(
    idx: int,
    rule: dict,
    candidates: list[dict],
) -> str:
    """Prompt the operator and return one of:
    "advisory", "skip", "abort", or a template name (when chosen by number).
    EOF → "skip". KeyboardInterrupt is *not* caught here; caller handles.
    """
    raw = (rule.get("rule") or rule.get("text") or "").strip()
    summary = raw if len(raw) <= 200 else raw[:200] + "..."
    sys.stderr.write(
        f"\n--- rule {idx} (ambiguous) ---\n"
        f"  text: {summary}\n"
        f"  enforcement: {rule.get('enforcement', '<none>')}\n"
    )
    if candidates:
        sys.stderr.write("  candidate templates:\n")
        for i, c in enumerate(candidates, start=1):
            sys.stderr.write(
                f"    [{i}] {c['template']}  "
                f"(matched by {c.get('_classifier', '?')})\n"
            )
    else:
        sys.stderr.write("  candidate templates: <none>\n")
    n = len(candidates)
    prompt = f"Choose [1-{n}], 'a' for advisory, 's' to skip, 'q' to abort: "
    # Iterative re-prompt loop (SEC-002 fix: was recursive, now bounded).
    # Hard cap on attempts prevents infinite-input DoS via piped junk.
    MAX_ATTEMPTS = 100
    for _ in range(MAX_ATTEMPTS):
        sys.stderr.write(prompt)
        sys.stderr.flush()
        try:
            line = input()
        except EOFError:
            return "skip"
        choice = (line or "").strip().lower()
        if choice == "a":
            return "advisory"
        if choice == "s" or choice == "":
            return "skip"
        if choice == "q":
            return "abort"
        if choice.isdigit():
            i = int(choice)
            if 1 <= i <= n:
                return f"__index_{i - 1}"
        sys.stderr.write("  (unrecognised input)\n")
    # Bounded retry exhausted — treat as skip rather than crashing.
    sys.stderr.write(
        f"  (max {MAX_ATTEMPTS} attempts reached; defaulting to skip)\n"
    )
    return "skip"


def _write_log(log_dir: Path, log: dict) -> Optional[Path]:
    """Write the migration log JSON. Returns the path written or None on error.
    Errors are reported to stderr and converted to exit code 2 by the caller.
    """
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.stderr.write(f"ERROR: cannot create log dir {log_dir}: {exc}\n")
        return None
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    log_path = log_dir / f"migration-{ts}.json"
    try:
        with log_path.open("w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, sort_keys=True)
            f.write("\n")
    except OSError as exc:
        sys.stderr.write(f"ERROR: cannot write log {log_path}: {exc}\n")
        return None
    return log_path


def _atomic_write_rules(rules_path: Path, payload: dict) -> bool:
    """Atomically write the migrated rules file. Returns False on I/O error."""
    try:
        rules_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.stderr.write(
            f"ERROR: cannot create rules-file parent {rules_path.parent}: {exc}\n"
        )
        return False
    tmp_path = rules_path.with_suffix(rules_path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp_path, rules_path)
    except OSError as exc:
        sys.stderr.write(f"ERROR: cannot write rules file {rules_path}: {exc}\n")
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        return False
    return True


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_prevention_rules",
        description=(
            "Migrate legacy free-text prevention rules to the template-based "
            "rules-engine schema."
        ),
    )
    parser.add_argument(
        "--rules-path",
        dest="rules_path",
        default=None,
        help=(
            "Path to prevention-rules.json. Defaults to "
            "_persistent_project_dir(repo_root)/prevention-rules.json."
        ),
    )
    parser.add_argument(
        "--non-interactive",
        dest="non_interactive",
        action="store_true",
        help="Default ambiguous rules to template='advisory' instead of prompting.",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Print before/after diff to stdout but do NOT write the rules file.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    repo_root = _REPO_ROOT
    rules_path = (
        Path(args.rules_path) if args.rules_path
        else _persistent_project_dir(repo_root) / "prevention-rules.json"
    )

    log_dir = repo_root / "tools" / "migration-logs"

    # ---- Load existing rules ------------------------------------------------
    if not rules_path.exists():
        sys.stderr.write(f"ERROR: rules file not found at {rules_path}\n")
        return 2
    try:
        with rules_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"ERROR: cannot read {rules_path}: {exc}\n")
        return 2

    if not isinstance(data, dict):
        sys.stderr.write(
            f"ERROR: top-level JSON in {rules_path} must be an object\n"
        )
        return 2

    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list):
        sys.stderr.write(
            f"ERROR: 'rules' field in {rules_path} must be a list\n"
        )
        return 2

    # ---- Classify and (optionally) prompt ----------------------------------
    migrated: list[dict] = []
    log_entries: list[dict] = []
    auto_count = 0
    advisory_default_count = 0
    operator_chosen_count = 0
    skipped_count = 0
    aborted = False

    for idx, rule in enumerate(raw_rules):
        if not isinstance(rule, dict):
            log_entries.append({
                "index": idx,
                "status": "skipped",
                "reason": "not a dict",
            })
            skipped_count += 1
            # Keep the malformed entry verbatim so we don't lose data.
            migrated.append(rule)
            continue

        status, candidates = classify(rule)

        if status == "single":
            chosen = candidates[0]
            migrated.append(_build_migrated(rule, chosen, idx))
            log_entries.append({
                "index": idx,
                "status": "auto",
                "template": chosen["template"],
                "classifier": chosen.get("_classifier"),
            })
            auto_count += 1
            continue

        # status == "ambiguous"
        if args.non_interactive:
            chosen = {
                "template": "advisory",
                "params": {},
                "_classifier": "ambiguous-default",
            }
            migrated.append(_build_migrated(rule, chosen, idx))
            log_entries.append({
                "index": idx,
                "status": "advisory-default",
                "candidate_count": len(candidates),
            })
            advisory_default_count += 1
            continue

        # Interactive mode: prompt the operator.
        try:
            decision = _prompt_for_choice(idx, rule, candidates)
        except KeyboardInterrupt:
            sys.stderr.write(
                "\nABORT: KeyboardInterrupt during prompt; "
                "saving partial log and exiting\n"
            )
            log_entries.append({
                "index": idx,
                "status": "aborted",
                "reason": "KeyboardInterrupt",
            })
            aborted = True
            # Persist partial log before exit (errors here are non-fatal).
            _write_log(log_dir, {
                "rules_path": str(rules_path),
                "dry_run": bool(args.dry_run),
                "non_interactive": bool(args.non_interactive),
                "aborted": True,
                "summary": {
                    "total_input": len(raw_rules),
                    "auto": auto_count,
                    "advisory_default": advisory_default_count,
                    "operator_chosen": operator_chosen_count,
                    "skipped": skipped_count,
                },
                "entries": log_entries,
            })
            return 1

        if decision == "abort":
            sys.stderr.write("\nABORT: operator chose 'q'; exiting\n")
            log_entries.append({
                "index": idx,
                "status": "aborted",
                "reason": "operator chose 'q'",
            })
            aborted = True
            _write_log(log_dir, {
                "rules_path": str(rules_path),
                "dry_run": bool(args.dry_run),
                "non_interactive": bool(args.non_interactive),
                "aborted": True,
                "summary": {
                    "total_input": len(raw_rules),
                    "auto": auto_count,
                    "advisory_default": advisory_default_count,
                    "operator_chosen": operator_chosen_count,
                    "skipped": skipped_count,
                },
                "entries": log_entries,
            })
            return 1

        if decision == "skip":
            log_entries.append({
                "index": idx,
                "status": "skipped",
                "reason": "operator skipped (or EOF)",
            })
            skipped_count += 1
            # Preserve the original rule untouched.
            migrated.append(rule)
            continue

        if decision == "advisory":
            chosen = {"template": "advisory", "params": {}}
            migrated.append(_build_migrated(rule, chosen, idx))
            log_entries.append({
                "index": idx,
                "status": "operator-advisory",
            })
            operator_chosen_count += 1
            continue

        if decision.startswith("__index_"):
            i = int(decision[len("__index_"):])
            if 0 <= i < len(candidates):
                chosen = candidates[i]
                migrated.append(_build_migrated(rule, chosen, idx))
                log_entries.append({
                    "index": idx,
                    "status": "operator-chosen",
                    "template": chosen["template"],
                })
                operator_chosen_count += 1
                continue
        # Defensive fallback: treat unknown decision as advisory.
        chosen = {"template": "advisory", "params": {}}
        migrated.append(_build_migrated(rule, chosen, idx))
        log_entries.append({
            "index": idx,
            "status": "operator-fallback-advisory",
        })
        advisory_default_count += 1

    # ---- Build payload ------------------------------------------------------
    new_payload = {
        "rules": migrated,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "migration_version": 1,
    }
    # Preserve unknown top-level keys (forward-compat).
    for k, v in data.items():
        if k not in {"rules", "updated_at"}:
            new_payload.setdefault(k, v)

    # ---- Diff (always for --dry-run, else summary line only) ---------------
    if args.dry_run:
        for line in _diff_lines(raw_rules, migrated):
            print(line)
        sys.stdout.flush()

    # ---- Write log ---------------------------------------------------------
    log = {
        "rules_path": str(rules_path),
        "dry_run": bool(args.dry_run),
        "non_interactive": bool(args.non_interactive),
        "aborted": aborted,
        "summary": {
            "total_input": len(raw_rules),
            "auto": auto_count,
            "advisory_default": advisory_default_count,
            "operator_chosen": operator_chosen_count,
            "skipped": skipped_count,
        },
        "entries": log_entries,
    }
    log_path = _write_log(log_dir, log)
    if log_path is None:
        # Log write failure is fatal — operators rely on the log for audit.
        return 2

    # ---- Write rules file (unless dry-run) ---------------------------------
    if not args.dry_run:
        ok = _atomic_write_rules(rules_path, new_payload)
        if not ok:
            return 2

    # ---- Final stderr summary ----------------------------------------------
    total = len(raw_rules)
    sys.stderr.write(
        f"{auto_count} auto, "
        f"{advisory_default_count} advisory-default, "
        f"{operator_chosen_count} operator-chosen, "
        f"{skipped_count} skipped, "
        f"{total} total\n"
    )
    sys.stderr.write(f"log: {log_path}\n")
    if args.dry_run:
        sys.stderr.write("dry-run: rules file NOT modified\n")
    sys.stderr.flush()
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        sys.stderr.write("\nABORT: KeyboardInterrupt at top level\n")
        rc = 1
    sys.exit(rc)
