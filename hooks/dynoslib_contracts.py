#!/usr/bin/env python3
"""Runtime contract validation for dynos-work pipeline boundaries.

Reads contract.json files from skill directories and validates that
required inputs exist before a skill runs, and declared outputs exist
after it completes. Depends only on dynoslib_core.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

from dynoslib_core import load_json

# ---------------------------------------------------------------------------
# Plugin root detection
# ---------------------------------------------------------------------------

_HOOKS_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _HOOKS_DIR.parent


# ---------------------------------------------------------------------------
# Contract loading
# ---------------------------------------------------------------------------

def load_contract(skill_name: str) -> dict:
    """Load contract.json for a given skill.

    Raises FileNotFoundError if the skill or contract does not exist.
    Raises ValueError if skill_name contains path traversal characters.
    """
    if "/" in skill_name or "\\" in skill_name or ".." in skill_name:
        raise ValueError(f"Invalid skill name: {skill_name}")
    contract_path = _PLUGIN_ROOT / "skills" / skill_name / "contract.json"
    return load_json(contract_path)


def list_contracts() -> list[dict]:
    """Load all contract.json files from all skills."""
    contracts = []
    skills_dir = _PLUGIN_ROOT / "skills"
    if not skills_dir.exists():
        return contracts
    for skill_dir in sorted(skills_dir.iterdir()):
        contract_path = skill_dir / "contract.json"
        if contract_path.exists():
            try:
                contract = load_json(contract_path)
                contract["_skill_dir"] = str(skill_dir)
                contracts.append(contract)
            except (json.JSONDecodeError, OSError):
                continue
    return contracts


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------

def _resolve_source(source: str, task_dir: Path, project_root: Path) -> list[Path]:
    """Resolve a contract source spec to actual file path(s).

    Returns list of paths that exist. Empty list means the source was not found.
    """
    if not source:
        return []

    # "user prompt" is always satisfied (it comes from the user, not a file)
    if "user prompt" in source:
        return [Path("/dev/null")]

    # Glob patterns: "glob .dynos/task-*/task-retrospective.json"
    if source.startswith("glob "):
        pattern = source[5:].strip()
        return sorted(project_root.glob(pattern))

    # Task-relative paths: ".dynos/task-{id}/manifest.json"
    if ".dynos/task-{id}/" in source:
        relative = source.split(".dynos/task-{id}/", 1)[1]
        candidate = task_dir / relative
        if "*" in relative:
            return sorted(task_dir.glob(relative))
        return [candidate] if candidate.exists() else []

    # Project memory paths (optional by nature)
    if "memory/" in source or "~/" in source:
        return []

    # Manifest field references
    if "manifest.json" in source and "snapshot" in source:
        manifest_path = task_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = load_json(manifest_path)
                if manifest.get("snapshot", {}).get("head_sha"):
                    return [manifest_path]
            except (json.JSONDecodeError, OSError):
                pass
        return []

    # Direct file reference
    candidate = task_dir / source
    if candidate.exists():
        return [candidate]

    return []


def _resolve_output(field_name: str, task_dir: Path) -> list[Path]:
    """Resolve a contract output field name to actual file path(s)."""
    # Handle glob-like patterns in field names
    if "{" in field_name or "*" in field_name:
        pattern = field_name
        for placeholder in ("{segment-id}", "{auditor}", "{timestamp}", "{auditor}-checkpoint-{timestamp}"):
            pattern = pattern.replace(placeholder, "*")
        return sorted(task_dir.glob(pattern))

    # Special non-file outputs
    if field_name in ("snapshot", "status_report", "terminal_report", "maintenance_result",
                       "benchmark_summary", "automation_status"):
        return [Path("/dev/null")]  # conceptual output, not a file

    candidate = task_dir / field_name
    return [candidate] if candidate.exists() else []


# ---------------------------------------------------------------------------
# Type checking
# ---------------------------------------------------------------------------

def _check_type(path: Path, expected_type: str, required_fields: list[str] | None = None,
                 field_types: dict[str, str] | None = None) -> list[str]:
    """Check that a file's content matches the expected type and schema.

    Args:
        path: File to validate.
        expected_type: Top-level type ("object", "array", "string").
        required_fields: If set, every key in this list must exist in each
            top-level object (or in each element if type is array-of-objects).
        field_types: Map of field_name -> expected python type name
            ("str", "list", "bool", "int", "float"). Checked only for
            fields that are present.
    """
    if not path.exists() or str(path) == "/dev/null":
        return []
    try:
        content = path.read_text()
    except OSError as e:
        return [f"cannot read {path}: {e}"]

    if expected_type == "object":
        try:
            data = json.loads(content)
            if not isinstance(data, dict):
                return [f"expected JSON object, got {type(data).__name__}"]
            return _check_fields(data, path.name, required_fields, field_types)
        except json.JSONDecodeError as e:
            return [f"invalid JSON: {e}"]
    elif expected_type == "array":
        try:
            data = json.loads(content)
            if not isinstance(data, list):
                return [f"expected JSON array, got {type(data).__name__}"]
        except json.JSONDecodeError as e:
            return [f"invalid JSON: {e}"]
    # "string" and "directory" types: existence is sufficient
    return []


_PY_TYPE_MAP: dict[str, type] = {
    "str": str, "string": str,
    "list": list, "array": list,
    "bool": bool, "boolean": bool,
    "int": int, "float": float, "number": (int, float),  # type: ignore[dict-item]
}


def _check_fields(data: dict, filename: str,
                  required_fields: list[str] | None,
                  field_types: dict[str, str] | None) -> list[str]:
    """Validate required fields and field types on a single JSON object."""
    errors: list[str] = []
    if required_fields:
        for field in required_fields:
            if field not in data or data[field] is None:
                errors.append(f"{filename}: missing required field '{field}'")
    if field_types:
        for field, expected in field_types.items():
            if field not in data or data[field] is None:
                continue  # only validate if present
            expected_py = _PY_TYPE_MAP.get(expected)
            if expected_py and not isinstance(data[field], expected_py):
                actual = type(data[field]).__name__
                errors.append(f"{filename}: field '{field}' expected {expected}, got {actual}")
    return errors


# ---------------------------------------------------------------------------
# Validation API
# ---------------------------------------------------------------------------

def validate_inputs(
    skill_name: str,
    task_dir: Path,
    project_root: Path | None = None,
    *,
    strict: bool = False,
) -> list[str]:
    """Validate that all required inputs for a skill exist and match types.

    Returns list of error strings. Empty = valid.
    In strict mode, also checks optional inputs for type correctness.
    """
    if project_root is None:
        # Walk up from task_dir to find .dynos parent
        project_root = task_dir.parent.parent

    try:
        contract = load_contract(skill_name)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return [f"cannot load contract for {skill_name}: {e}"]

    errors: list[str] = []
    for field_name, spec in contract.get("input_schema", {}).items():
        required = spec.get("required", False)
        source = spec.get("source", "")
        expected_type = spec.get("type", "string")
        req_fields = spec.get("required_fields")
        fld_types = spec.get("field_types")

        paths = _resolve_source(source, task_dir, project_root)

        if not paths and required:
            errors.append(f"missing required input: {field_name} (source: {source})")
        elif paths and (strict or required):
            for p in paths:
                type_errors = _check_type(p, expected_type, req_fields, fld_types)
                errors.extend(f"{field_name}: {e}" for e in type_errors)

    return errors


def validate_outputs(skill_name: str, task_dir: Path) -> list[str]:
    """Validate that all outputs declared in the contract exist after skill completion.

    Returns list of error strings. Empty = valid.
    """
    try:
        contract = load_contract(skill_name)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return [f"cannot load contract for {skill_name}: {e}"]

    errors: list[str] = []
    for field_name, spec in contract.get("output_schema", {}).items():
        expected_type = spec.get("type", "string")
        req_fields = spec.get("required_fields")
        fld_types = spec.get("field_types")
        optional = spec.get("optional", False)
        paths = _resolve_output(field_name, task_dir)
        if not paths:
            if not optional:
                errors.append(f"missing declared output: {field_name}")
        else:
            for p in paths:
                type_errors = _check_type(p, expected_type, req_fields, fld_types)
                errors.extend(f"{field_name}: {e}" for e in type_errors)

    return errors


# ---------------------------------------------------------------------------
# Pipeline chain validation (dry-run)
# ---------------------------------------------------------------------------

PIPELINE_ORDER = ["start", "plan", "execute", "audit", "learn"]


def _normalize_glob(field_name: str) -> str:
    """Convert contract field name placeholders to fnmatch-compatible glob patterns.

    Examples:
        "audit-reports/{auditor}-checkpoint-{timestamp}.json"
          -> "audit-reports/*-checkpoint-*.json"
        "evidence/{segment-id}.md"
          -> "evidence/*.md"
        "audit-reports/*.json"
          -> "audit-reports/*.json"  (unchanged)
    """
    return re.sub(r"\{[^}]+\}", "*", field_name)


def _field_names_match(consumer_field: str, producer_field: str) -> bool:
    """Check whether a consumer input field name matches a producer output field name.

    Supports exact matches and glob-style matching where either side may
    contain placeholders ({...}) or wildcards (*).
    """
    if consumer_field == producer_field:
        return True
    consumer_glob = _normalize_glob(consumer_field)
    producer_glob = _normalize_glob(producer_field)
    # Try matching in both directions: consumer pattern against producer
    # name, and producer pattern against consumer name.
    return (fnmatch.fnmatch(producer_field, consumer_glob)
            or fnmatch.fnmatch(consumer_field, producer_glob)
            or fnmatch.fnmatch(producer_glob, consumer_glob)
            or fnmatch.fnmatch(consumer_glob, producer_glob))


def validate_chain() -> list[str]:
    """Validate that output schemas cover required input schemas across the pipeline.

    This is the dry-run validator: it checks contract compatibility
    without running any skills.  It performs two passes:

    1. **Name-level check** -- every required input artifact must be declared
       as an output by some prior stage.
    2. **Field-level check** -- when a consumer input declares
       ``required_fields``, the matching producer output must also declare
       (at least) those fields.
    """
    errors: list[str] = []
    contracts: dict[str, dict] = {}

    for skill_name in PIPELINE_ORDER:
        try:
            contracts[skill_name] = load_contract(skill_name)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            errors.append(f"cannot load contract for {skill_name}: {e}")

    # cumulative_outputs maps output field_name -> {skill, spec} for the
    # producing contract so that the field-level pass can inspect the full
    # output spec.
    cumulative_outputs: dict[str, dict] = {}

    for i in range(len(PIPELINE_ORDER)):
        skill_name = PIPELINE_ORDER[i]
        if skill_name not in contracts:
            continue

        # --- Pass 1: name-level check (original logic) ---
        if i > 0:
            to_inputs = contracts[skill_name].get("input_schema", {})
            for field_name, spec in to_inputs.items():
                if not spec.get("required", False):
                    continue
                source = spec.get("source", "")
                # Skip non-file sources
                if "user prompt" in source or "memory/" in source or "glob" in source:
                    continue
                # Skip manifest field references (snapshot.head_sha etc.)
                if "manifest.json" in source and field_name != "manifest.json":
                    continue
                # Check by exact field name match in cumulative outputs
                matched = field_name in cumulative_outputs
                if not matched:
                    # Try glob-aware matching
                    matched = any(
                        _field_names_match(field_name, out_name)
                        for out_name in cumulative_outputs
                    )
                if not matched:
                    errors.append(
                        f"chain gap: {skill_name} requires '{field_name}' "
                        f"but no prior stage declares it in output_schema"
                    )

        # --- Pass 2: field-level cross-check ---
        if i > 0:
            to_inputs = contracts[skill_name].get("input_schema", {})
            for field_name, spec in to_inputs.items():
                consumer_req_fields = spec.get("required_fields")
                if not consumer_req_fields:
                    continue
                consumer_fields_set = set(consumer_req_fields)

                # Find the producing output entry
                producer_entry = cumulative_outputs.get(field_name)
                if producer_entry is None:
                    # Try glob-aware lookup
                    for out_name, entry in cumulative_outputs.items():
                        if _field_names_match(field_name, out_name):
                            producer_entry = entry
                            break

                if producer_entry is None:
                    # No producer found at all -- the name-level check
                    # already reported this if the field is required,
                    # so skip to avoid duplicate noise.
                    continue

                producer_skill = producer_entry["skill"]
                producer_spec = producer_entry["spec"]
                producer_req_fields = producer_spec.get("required_fields")

                if producer_req_fields is None:
                    errors.append(
                        f"field-level gap: {skill_name} input '{field_name}' "
                        f"requires fields {sorted(consumer_fields_set)} but "
                        f"{producer_skill} output '{producer_entry['field_name']}' "
                        f"declares no required_fields"
                    )
                else:
                    producer_fields_set = set(producer_req_fields)
                    missing = consumer_fields_set - producer_fields_set
                    if missing:
                        errors.append(
                            f"field-level gap: {skill_name} input '{field_name}' "
                            f"requires fields {sorted(missing)} not declared in "
                            f"{producer_skill} output"
                        )

        # Add this stage's outputs to the cumulative set
        for out_name, out_spec in contracts[skill_name].get("output_schema", {}).items():
            cumulative_outputs[out_name] = {
                "skill": skill_name,
                "field_name": out_name,
                "spec": out_spec,
            }

    return errors
