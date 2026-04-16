#!/usr/bin/env python3
"""Runtime contract validation for dynos-work pipeline boundaries.

Reads contract.json files from skill directories and validates that
required inputs exist before a skill runs, and declared outputs exist
after it completes. Depends only on lib_core.
"""

from __future__ import annotations

import json
from pathlib import Path

from lib_core import load_json

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

def _check_type(path: Path, expected_type: str) -> list[str]:
    """Check that a file's content matches the expected type."""
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

        paths = _resolve_source(source, task_dir, project_root)

        if not paths and required:
            errors.append(f"missing required input: {field_name} (source: {source})")
        elif paths and (strict or required):
            for p in paths:
                type_errors = _check_type(p, expected_type)
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
        paths = _resolve_output(field_name, task_dir)
        if not paths:
            errors.append(f"missing declared output: {field_name}")
        else:
            for p in paths:
                type_errors = _check_type(p, expected_type)
                errors.extend(f"{field_name}: {e}" for e in type_errors)

    return errors


# ---------------------------------------------------------------------------
# Pipeline chain validation (dry-run)
# ---------------------------------------------------------------------------

PIPELINE_ORDER = ["start", "plan", "execute", "audit", "learn"]


def validate_chain() -> list[str]:
    """Validate that output schemas cover required input schemas across the pipeline.

    This is the dry-run validator: it checks contract compatibility
    without running any skills.
    """
    errors: list[str] = []
    contracts: dict[str, dict] = {}

    for skill_name in PIPELINE_ORDER:
        try:
            contracts[skill_name] = load_contract(skill_name)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            errors.append(f"cannot load contract for {skill_name}: {e}")

    # Build cumulative output set: artifacts persist across stages
    cumulative_outputs: set[str] = set()

    for i in range(len(PIPELINE_ORDER)):
        skill_name = PIPELINE_ORDER[i]
        if skill_name not in contracts:
            continue

        # Check this stage's required inputs against all prior outputs
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
                    errors.append(
                        f"chain gap: {skill_name} requires '{field_name}' "
                        f"but no prior stage declares it in output_schema"
                    )

        # Add this stage's outputs to the cumulative set
        cumulative_outputs.update(contracts[skill_name].get("output_schema", {}).keys())

    return errors
