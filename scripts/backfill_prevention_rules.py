#!/usr/bin/env python3
"""One-shot backfill script for prevention-rules.json.

Usage:
    python3 scripts/backfill_prevention_rules.py <registry_path>

Migration pipeline (AC 4):
  1. Load registry from <registry_path>.
  2. For each entry:
     - rule == "" AND legacy_text non-empty: copy legacy_text -> rule, drop
       legacy_text key, generate rule_id via _generate_rule_id(rule, category).
     - rule == "" AND legacy_text absent/empty: drop entry entirely.
     - rule non-empty AND missing rule_id: generate rule_id.
     - already valid: untouched.
  3. Enforcement audit (AC 8): for entries with template == "advisory" AND
     enforcement in the demote-set, rewrite enforcement -> "prompt-constraint".
  4. Dedup by rule_id (AC 10): retain only first by lower array index.
  5. Write to tempfile in same directory via tempfile module.
  6. Validate via run_checks (zero WARN). On failure: leave live untouched,
     exit non-zero.
  7. On success: os.replace(tmp, live).
  8. Emit prevention_rules_backfill event (AC 7).

Idempotency (AC 5): if every entry already has a valid rule_id and no
enforcement demotions are needed, prints "already_backfilled" and exits 0.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

# Allow imports from hooks/ and memory/
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "hooks"))
sys.path.insert(0, str(_ROOT / "memory"))

from lib_log import log_event  # noqa: E402
from postmortem_analysis import _generate_rule_id  # noqa: E402
from rules_engine import _validate_rule_entry, run_checks  # noqa: E402

# Templates that can legitimately carry enforced (non-advisory) labels.
_STRUCTURED_TEMPLATES = {
    "pattern_must_not_appear",
    "co_modification_required",
    "every_name_in_X_satisfies_Y",
    "signature_lock",
    "caller_count_required",
    "import_constant_only",
}

# Enforcement labels that are dishonest when paired with "advisory" template.
_DEMOTE_SET = {"ci-gate", "static-check", "runtime-guard", "lint", "test"}


def _sha256_file(path: Path) -> str:
    """Return hex SHA-256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _has_valid_rule_id(entry: dict) -> bool:
    """Return True if _validate_rule_entry reports no error."""
    return _validate_rule_entry(entry) is None


def run_backfill(registry_path: Path) -> int:
    """Run the backfill pipeline.

    Returns 0 on success (including no-op), non-zero on failure.
    Suitable for import-based testing (AC 5 requires module importability).
    """
    registry_path = Path(registry_path).resolve()

    # --- Load registry ---
    try:
        raw_text = registry_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[backfill] ERROR: cannot read {registry_path}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(data, dict):
        print(f"[backfill] ERROR: registry root is not a JSON object", file=sys.stderr)
        return 1

    original_rules: list = data.get("rules", [])
    if not isinstance(original_rules, list):
        print(f"[backfill] ERROR: 'rules' field is not a list", file=sys.stderr)
        return 1

    entries_before = len(original_rules)

    # --- AC 5: Idempotency check ---
    # Check if every entry already has a valid rule_id AND no enforcement demotions needed
    # AND no entries are missing a template (which would trigger Phase 1b).
    all_valid = all(_has_valid_rule_id(e) for e in original_rules if isinstance(e, dict))
    no_demotions_needed = not any(
        isinstance(e, dict)
        and e.get("template") == "advisory"
        and e.get("enforcement") in _DEMOTE_SET
        for e in original_rules
    )
    no_missing_templates = all(
        bool(e.get("template")) for e in original_rules if isinstance(e, dict)
    )
    if all_valid and no_demotions_needed and no_missing_templates:
        print("already_backfilled")
        return 0

    # --- Phase 1: Rule-text migration ---
    migrated: list[dict] = []
    for entry in original_rules:
        if not isinstance(entry, dict):
            # Drop non-dict entries silently.
            continue
        rule_text = entry.get("rule", "")
        legacy_text = entry.get("legacy_text", "")

        if not isinstance(rule_text, str):
            rule_text = ""
        if not isinstance(legacy_text, str):
            legacy_text = ""

        rule_text = rule_text.strip()
        legacy_text = legacy_text.strip()

        if not rule_text:
            # rule == "" branch
            if legacy_text:
                # Migrate: copy legacy_text -> rule, drop legacy_text key
                entry = dict(entry)  # shallow copy to avoid mutating caller's data
                entry["rule"] = legacy_text
                entry.pop("legacy_text", None)
                rule_text = legacy_text
            else:
                # Drop entirely (rule=="" and legacy_text absent/empty)
                continue
        elif "legacy_text" in entry:
            # rule non-empty: still clean up legacy_text key if present
            entry = dict(entry)
            entry.pop("legacy_text", None)

        # Ensure rule_text reflects updated state
        migrated.append(entry)

    # --- Phase 1b: Default missing template to "advisory" ---
    # Entries without a template field cannot pass _validate_rule_entry.
    # "advisory" is the safe default: it carries no engine action, and
    # advisory-style entries are the vast majority of free-form rule texts.
    for i, entry in enumerate(migrated):
        if not entry.get("template"):
            entry = dict(entry)
            entry["template"] = "advisory"
            migrated[i] = entry

    # --- Phase 2: rule_id generation ---
    for i, entry in enumerate(migrated):
        rule_text = entry.get("rule", "").strip()
        category = entry.get("category", "unknown") or "unknown"
        if _validate_rule_entry(entry) is not None:
            # Missing rule_id — regenerate.
            entry = dict(entry)
            entry["rule_id"] = _generate_rule_id(rule_text, category)
            migrated[i] = entry

    # --- Phase 3: Enforcement audit (AC 8) ---
    # Applied AFTER rule_id generation, BEFORE temp-file validation.
    for i, entry in enumerate(migrated):
        template = entry.get("template")
        enforcement = entry.get("enforcement")
        if template == "advisory" and enforcement in _DEMOTE_SET:
            entry = dict(entry)
            entry["enforcement"] = "prompt-constraint"
            migrated[i] = entry

    # --- Phase 4: Dedup by rule_id (AC 10) ---
    seen_ids: dict[str, int] = {}  # rule_id -> first index
    deduped: list[dict] = []
    for entry in migrated:
        rule_id = entry.get("rule_id", "")
        if rule_id in seen_ids:
            # Drop duplicate — emit event
            log_event(
                _ROOT,
                "prevention_rules_duplicate_dropped",
                rule_id=rule_id,
                source_task=entry.get("source_task", ""),
            )
        else:
            seen_ids[rule_id] = len(deduped)
            deduped.append(entry)

    entries_after = len(deduped)
    entries_dropped = entries_before - entries_after

    # --- Phase 5: Write to tempfile ---
    output_data = dict(data)
    output_data["rules"] = deduped

    registry_dir = registry_path.parent
    tmp_path: Path | None = None
    try:
        fd, tmp_str = tempfile.mkstemp(
            dir=str(registry_dir),
            prefix=".backfill-tmp-",
            suffix=".json",
        )
        tmp_path = Path(tmp_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(output_data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
        except Exception:
            os.unlink(tmp_str)
            raise
    except (OSError, Exception) as exc:
        print(f"[backfill] ERROR: cannot write tempfile: {exc}", file=sys.stderr)
        return 1

    # --- Phase 6: Validate temp file via run_checks (zero WARN) ---
    # run_checks uses the live registry path; we need to validate the tmp file.
    # We validate by calling _validate_rule_entry on each entry in the tempfile.
    try:
        tmp_text = tmp_path.read_text(encoding="utf-8")
        tmp_data = json.loads(tmp_text)
        tmp_rules = tmp_data.get("rules", [])
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[backfill] ERROR: cannot re-read tempfile for validation: {exc}", file=sys.stderr)
        tmp_path.unlink(missing_ok=True)
        return 1

    validation_errors: list[str] = []
    for entry in tmp_rules:
        err = _validate_rule_entry(entry)
        if err:
            validation_errors.append(err)

    if validation_errors:
        err_list = "\n  ".join(validation_errors)
        print(
            f"[backfill] ERROR: validation failed ({len(validation_errors)} errors):\n  {err_list}",
            file=sys.stderr,
        )
        tmp_path.unlink(missing_ok=True)
        return 1

    # --- Phase 7: Atomic replace ---
    old_sha256 = _sha256_file(registry_path)
    try:
        os.replace(str(tmp_path), str(registry_path))
    except OSError as exc:
        print(f"[backfill] ERROR: os.replace failed: {exc}", file=sys.stderr)
        tmp_path.unlink(missing_ok=True)
        return 1

    new_sha256 = _sha256_file(registry_path)

    # --- Phase 8: Emit backfill event (AC 7) ---
    log_event(
        _ROOT,
        "prevention_rules_backfill",
        old_sha256=old_sha256,
        new_sha256=new_sha256,
        entries_before=entries_before,
        entries_after=entries_after,
        entries_dropped=entries_dropped,
    )

    print(
        f"[backfill] OK: {entries_before} -> {entries_after} entries "
        f"({entries_dropped} dropped)"
    )
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        live_path = Path.home() / ".dynos" / "projects" / "Users-hassam-Documents-dynos-work" / "prevention-rules.json"
        registry_path = live_path
    else:
        registry_path = Path(sys.argv[1])

    return run_backfill(registry_path)


if __name__ == "__main__":
    sys.exit(main())
