#!/usr/bin/env python3
"""Plan intermediate-state topology + smoke check.

For a task's execution-graph.json, walks segments in dependency order
and, for each intermediate state (after a non-terminal segment, before
the next one runs), verifies two things:

  1. Topology check (AST):
     Parse each file in the segment's `files_expected` and identify
     `def` functions with their public signatures (positional + kwarg
     counts).  Walk every other `hooks/*.py` source and look for
     `ast.Call` nodes that reference those functions; verify caller
     arg/kwarg shape matches the post-segment signature.  Files that
     do not exist on disk (NEW segments) are recorded as warnings,
     never as `blocked`.

  2. Smoke test (subprocess):
     Run a one-liner that imports the core foundry modules:
         from lib_log import log_event
         from lib_receipts import write_receipt
         from lib_core import transition_task, write_ctl_json
         from lib_tokens import _write_usage
     A clean exit + "OK" stdout means imports resolved successfully.

Output JSON to stdout.  Exit 0 on pass; 1 on blocked.

Conservatism rule (AC 41):
    AST parse failures, unresolvable patterns, and OS errors are
    recorded as warnings only.  Only confirmed topology mismatches
    AND confirmed smoke-test failures trigger `blocked`.  Err toward
    false negatives.

This module imports only from the standard library and does NOT import
any other `hooks/` module at module scope, so it works even when other
hooks/ modules have signature drift.

Usage:
    python3 hooks/plan_intermediate_state_check.py \
        --root <project-root> --task-dir <.dynos/task-id>
"""
from __future__ import annotations

import argparse
import ast
import json
import re  # noqa: F401  (kept per spec stdlib-import allowlist; used for path normalization helpers if extended)
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Smoke-test source. Computed from spec AC 37 (single subprocess invocation
# importing the core foundry modules).  See: spec.md → AC 37 (smoke test).
_SMOKE_SOURCE = (
    'import sys; sys.path.insert(0, "hooks"); '
    "from lib_log import log_event; "
    "from lib_receipts import write_receipt; "
    "from lib_core import transition_task, write_ctl_json; "
    "from lib_tokens import _write_usage; "
    'print("OK")'
)

# Maximum subprocess wall-clock per smoke test (seconds).
_SMOKE_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Graph loading + topo sort
# ---------------------------------------------------------------------------

def _load_graph(task_dir: Path) -> tuple[dict | None, str | None]:
    """Return (graph_dict, error_message). graph_dict is None on failure."""
    graph_path = task_dir / "execution-graph.json"
    if not graph_path.exists():
        return None, f"execution-graph.json missing at {graph_path}"
    try:
        text = graph_path.read_text(errors="ignore")
    except OSError as e:
        return None, f"execution-graph.json unreadable: {e}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"execution-graph.json invalid JSON: {e}"
    if not isinstance(data, dict):
        return None, "execution-graph.json root is not an object"
    return data, None


def _topo_sort(segments: list[dict]) -> list[dict]:
    """Return segments in dependency order (no-deps first).

    Falls back to the original order on cycle / malformed dep lists —
    this is a best-effort read-only check, not a graph validator.
    """
    by_id: dict[str, dict] = {}
    for s in segments:
        if isinstance(s, dict) and isinstance(s.get("id"), str):
            by_id[s["id"]] = s

    ordered: list[dict] = []
    seen: set[str] = set()
    visiting: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in seen or node_id not in by_id:
            return
        if node_id in visiting:
            # Cycle — bail; just append remaining nodes as-is later.
            return
        visiting.add(node_id)
        deps = by_id[node_id].get("depends_on") or []
        if isinstance(deps, list):
            for d in deps:
                if isinstance(d, str):
                    visit(d)
        visiting.discard(node_id)
        seen.add(node_id)
        ordered.append(by_id[node_id])

    for s in segments:
        if isinstance(s, dict) and isinstance(s.get("id"), str):
            visit(s["id"])

    # Append any segments that didn't make it (e.g. cycle-orphans), preserving order.
    for s in segments:
        if isinstance(s, dict) and isinstance(s.get("id"), str) and s["id"] not in seen:
            ordered.append(s)
            seen.add(s["id"])
    return ordered


# ---------------------------------------------------------------------------
# AST: extract public function signatures from a file
# ---------------------------------------------------------------------------

class _SigInfo:
    __slots__ = ("name", "min_pos", "max_pos", "kwonly_required", "kwonly_all", "has_vararg", "has_kwarg")

    def __init__(
        self,
        name: str,
        min_pos: int,
        max_pos: int,
        kwonly_required: set[str],
        kwonly_all: set[str],
        has_vararg: bool,
        has_kwarg: bool,
    ) -> None:
        self.name = name
        self.min_pos = min_pos
        self.max_pos = max_pos
        self.kwonly_required = kwonly_required
        self.kwonly_all = kwonly_all
        self.has_vararg = has_vararg
        self.has_kwarg = has_kwarg


def _signatures_from_source(source: str) -> dict[str, _SigInfo]:
    """Return mapping of function name → _SigInfo for top-level public defs."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        raise
    out: dict[str, _SigInfo] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            args = node.args
            posonly = list(args.posonlyargs or [])
            regular = list(args.args or [])
            all_pos = posonly + regular
            defaults = list(args.defaults or [])
            num_pos = len(all_pos)
            num_required = num_pos - len(defaults)
            kwonly = list(args.kwonlyargs or [])
            kwonly_defaults = list(args.kw_defaults or [])
            kwonly_required: set[str] = set()
            kwonly_all: set[str] = set()
            for i, a in enumerate(kwonly):
                kwonly_all.add(a.arg)
                # kw_defaults entries are None for required kwonly args.
                if i >= len(kwonly_defaults) or kwonly_defaults[i] is None:
                    kwonly_required.add(a.arg)
            sig = _SigInfo(
                name=name,
                min_pos=num_required,
                max_pos=num_pos,
                kwonly_required=kwonly_required,
                kwonly_all=kwonly_all,
                has_vararg=args.vararg is not None,
                has_kwarg=args.kwarg is not None,
            )
            out[name] = sig
    return out


# ---------------------------------------------------------------------------
# AST: find calls that reference a known function name
# ---------------------------------------------------------------------------

def _collect_imports(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    """Walk a parsed module and return:

      name_to_module: {local_name: source_module}
          Maps locally-bound names introduced by `from X import Y [as Z]`
          to their source module X.  For `from lib_receipts import
          validate_chain as vc`, this records `vc → lib_receipts`.

      module_aliases: {alias_or_name}
          Names that refer to whole modules (from `import X` or
          `import X as Y`).  Used to recognize attribute calls like
          `lib_receipts.validate_chain(...)`.
    """
    name_to_module: dict[str, str] = {}
    module_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                name_to_module[local] = mod
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                module_aliases.add(local)
    return name_to_module, module_aliases


def _iter_calls_to(
    source: str,
    names: set[str],
    target_module: str,
) -> list[tuple[str, ast.Call]]:
    """Return list of (called_name, ast.Call) for every Call that we can
    statically attribute to `target_module`'s function definitions.

    A call is attributed only when one of:
      - The called name was bound by `from <target_module> import <name>`
        (with or without `as`).
      - The call is `<target_module_alias>.<name>(...)` where the alias
        was bound via `import <target_module>` (or `as`).

    Conservative by design (AC 41): unattributable calls are skipped,
    avoiding false positives where two unrelated modules each define a
    `main` or `validate_chain`.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        raise
    name_to_module, module_aliases = _collect_imports(tree)

    # Determine which module aliases correspond to the target module.
    target_module_aliases: set[str] = set()
    if target_module in module_aliases:
        target_module_aliases.add(target_module)
    # Resolve `import target_module as alias` form by re-walking imports.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == target_module:
                    target_module_aliases.add(alias.asname or alias.name)

    found: list[tuple[str, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called: str | None = None
        attributed = False
        if isinstance(node.func, ast.Name):
            called = node.func.id
            # Only attribute if local name was imported from target module.
            if name_to_module.get(called) == target_module:
                attributed = True
        elif isinstance(node.func, ast.Attribute):
            called = node.func.attr
            base = node.func.value
            if isinstance(base, ast.Name) and base.id in target_module_aliases:
                attributed = True
        if attributed and called and called in names:
            found.append((called, node))
    return found


# ---------------------------------------------------------------------------
# Topology check
# ---------------------------------------------------------------------------

def _check_call_against_sig(call: ast.Call, sig: _SigInfo) -> str | None:
    """Return error message if call shape is incompatible with signature, else None."""
    # Count positional args (treat *expansion as unknown — skip).
    num_positional = 0
    has_starred_pos = False
    for a in call.args:
        if isinstance(a, ast.Starred):
            has_starred_pos = True
        else:
            num_positional += 1

    # Collect keyword arg names; treat **expansion as unknown.
    kw_names: set[str] = set()
    has_double_star = False
    for kw in call.keywords:
        if kw.arg is None:
            has_double_star = True
        else:
            kw_names.add(kw.arg)

    if has_starred_pos or has_double_star:
        return None  # cannot statically check; conservative pass

    # Some kwargs may bind to positional params.
    # Compute positional binding: positional args + kwargs that name regular params.
    if num_positional > sig.max_pos and not sig.has_vararg:
        return (
            f"too many positional arguments: call passes {num_positional}, "
            f"function {sig.name!r} accepts at most {sig.max_pos}"
        )

    # Distinguish kw names that match regular positional params vs kwonly vs unknown.
    # We don't know the regular-positional names from _SigInfo (we only kept counts);
    # treat any unknown kw name (not in kwonly_all) as binding to a positional param.
    # If kwarg accepts arbitrary kwargs (**kwargs), we cannot check unknowns.
    unknown_kw: set[str] = set()
    for name in kw_names:
        if name in sig.kwonly_all:
            continue
        unknown_kw.add(name)

    if unknown_kw and not sig.has_kwarg:
        # We cannot definitively say these are wrong because they may match
        # regular positional parameters by name. Without the parameter names
        # in _SigInfo, we conservatively skip this check — never report a
        # false positive.
        pass

    # Required-kwonly check: kwonly args that have no default must be passed.
    missing_kwonly = sig.kwonly_required - kw_names
    if missing_kwonly and not sig.has_kwarg:
        # Conservative: only flag if the function has NO **kwargs catch-all.
        # If there's a **kwargs param, the missing kwonly might be supplied
        # by some upstream pattern we can't statically detect.
        return (
            f"missing required keyword-only argument(s) for {sig.name!r}: "
            f"{sorted(missing_kwonly)}"
        )

    # Minimum positional check: too few positionals AND no kwargs filling them.
    if num_positional < sig.min_pos:
        # Some kwargs may fill positional slots; without parameter names we
        # can't tell. Be conservative: only flag if there are NO kwargs at all
        # AND no **kwargs.
        if not kw_names and not sig.has_kwarg:
            return (
                f"too few arguments: call passes {num_positional} positional, "
                f"function {sig.name!r} requires at least {sig.min_pos}"
            )

    return None


def _topology_check(
    root: Path,
    files_expected: list[str],
) -> tuple[bool, list[dict]]:
    """Run AST topology check for one segment's post-state.

    Returns (topology_ok, failures).  topology_ok is True unless a
    CONFIRMED arg-count / kwarg-shape mismatch was found.  Warnings
    (parse errors, missing files) are appended to failures but do NOT
    flip topology_ok.
    """
    failures: list[dict] = []
    confirmed_break = False

    # Per-target-module signature maps. Key: module_name (e.g. "lib_receipts"),
    # value: {function_name: _SigInfo}.
    target_sigs_by_module: dict[str, dict[str, _SigInfo]] = {}
    target_files_resolved: list[Path] = []

    for rel in files_expected:
        if not isinstance(rel, str):
            continue
        target = (root / rel)
        try:
            target_resolved = target.resolve()
            root_resolved = root.resolve()
            target_resolved.relative_to(root_resolved)
        except (OSError, ValueError):
            failures.append({
                "severity": "warning",
                "reason": f"path outside root or unresolvable: {rel}",
            })
            continue
        if not target.exists():
            # NEW file — tolerated as info-level warning (AC 39).
            failures.append({
                "severity": "info",
                "reason": f"new file (not yet on disk): {rel}",
            })
            continue
        if not target.is_file() or target.suffix != ".py":
            failures.append({
                "severity": "info",
                "reason": f"non-Python or non-file path skipped: {rel}",
            })
            continue
        try:
            source = target.read_text(errors="ignore")
        except OSError as e:
            failures.append({
                "severity": "warning",
                "reason": f"could not read {rel}: {e}",
            })
            continue
        try:
            sigs = _signatures_from_source(source)
        except (SyntaxError, ValueError) as e:
            # AC 41: parse failure is warning only.
            failures.append({
                "severity": "warning",
                "reason": f"AST parse error in {rel}: {e}",
            })
            continue
        # Module name = file stem (e.g. hooks/lib_receipts.py → "lib_receipts").
        module_name = Path(rel).stem
        target_sigs_by_module.setdefault(module_name, {}).update(sigs)
        target_files_resolved.append(target.resolve())

    if not target_sigs_by_module:
        return (True, failures)

    # Walk other hooks/*.py files and look for callers.
    hooks_dir = root / "hooks"
    if not hooks_dir.exists() or not hooks_dir.is_dir():
        # No hooks dir to scan — topology check is vacuously true.
        return (True, failures)

    try:
        candidate_files = sorted(hooks_dir.glob("*.py"))
    except OSError as e:
        failures.append({
            "severity": "warning",
            "reason": f"could not list hooks/: {e}",
        })
        return (True, failures)

    target_paths_set = {p for p in target_files_resolved}

    for caller_path in candidate_files:
        try:
            if caller_path.resolve() in target_paths_set:
                # Don't check a file against its own definitions.
                continue
        except OSError:
            continue
        try:
            caller_source = caller_path.read_text(errors="ignore")
        except OSError as e:
            failures.append({
                "severity": "warning",
                "reason": f"could not read {caller_path.name}: {e}",
            })
            continue

        for module_name, sigs in target_sigs_by_module.items():
            names = set(sigs.keys())
            try:
                calls = _iter_calls_to(caller_source, names, module_name)
            except (SyntaxError, ValueError) as e:
                failures.append({
                    "severity": "warning",
                    "reason": f"AST parse error in {caller_path.name}: {e}",
                })
                break  # don't try other target modules on the same broken file
            for called_name, call_node in calls:
                sig = sigs.get(called_name)
                if sig is None:
                    continue
                err = _check_call_against_sig(call_node, sig)
                if err is not None:
                    lineno = getattr(call_node, "lineno", "?")
                    failures.append({
                        "severity": "blocker",
                        "reason": (
                            f"topology mismatch in hooks/{caller_path.name}:{lineno} → "
                            f"{module_name}.{called_name}: {err}"
                        ),
                    })
                    confirmed_break = True

    return ((not confirmed_break), failures)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

_SMOKE_REQUIRED_FOUNDRY_FILES = (
    "hooks/lib_log.py",
    "hooks/lib_receipts.py",
    "hooks/lib_core.py",
    "hooks/lib_tokens.py",
)


def _smoke_test(root: Path) -> tuple[bool, str]:
    """Run the import-only smoke test in a subprocess.

    Returns (ok, message).

    Conservatism (AC 41): if the project root does not contain the
    foundry hook modules the smoke test asserts on, the smoke test is
    not applicable to this codebase — return (True, warning).  This
    prevents the check from incorrectly blocking on unrelated repos
    or synthetic test roots.
    """
    # Applicability gate — don't run if foundry isn't here.
    missing_foundry = [
        p for p in _SMOKE_REQUIRED_FOUNDRY_FILES if not (root / p).exists()
    ]
    if missing_foundry:
        return (
            True,
            f"smoke test not applicable (foundry modules absent): {missing_foundry}",
        )

    try:
        proc = subprocess.run(
            [sys.executable, "-c", _SMOKE_SOURCE],
            capture_output=True,
            text=True,
            timeout=_SMOKE_TIMEOUT,
            cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        return (False, f"smoke test timed out after {_SMOKE_TIMEOUT}s")
    except OSError as e:
        # AC 41: OS errors are warnings, not blockers.
        return (True, f"smoke test could not be launched (warning): {e}")

    if proc.returncode == 0 and "OK" in (proc.stdout or ""):
        return (True, "smoke test pass")
    return (
        False,
        f"smoke test failed: returncode={proc.returncode}, "
        f"stdout={proc.stdout!r}, stderr={proc.stderr!r}",
    )


# ---------------------------------------------------------------------------
# Main check pipeline
# ---------------------------------------------------------------------------

def run_check(root: Path, task_dir: Path) -> dict:
    """Return the structured result dict (without writing to stdout)."""
    result: dict = {
        "status": "pass",
        "intermediate_states": [],
        "failures": [],
    }

    graph, err = _load_graph(task_dir)
    if graph is None:
        # AC 36: fail-open.
        result["failures"].append(err or "execution-graph.json missing")
        return result

    segments = graph.get("segments")
    if not isinstance(segments, list) or not segments:
        result["failures"].append("execution-graph.json has no segments")
        return result

    ordered = _topo_sort(segments)

    # Intermediate states are after every segment EXCEPT the last one in topo order.
    if len(ordered) <= 1:
        return result

    intermediate_segments = ordered[:-1]

    any_blocker = False
    for seg in intermediate_segments:
        seg_id = seg.get("id") if isinstance(seg, dict) else None
        if not isinstance(seg_id, str):
            continue
        files_expected = seg.get("files_expected") if isinstance(seg, dict) else []
        if not isinstance(files_expected, list):
            files_expected = []

        topology_ok, topo_failures = _topology_check(root, files_expected)
        smoke_ok, smoke_msg = _smoke_test(root)

        entry_failures: list[dict] = list(topo_failures)
        if not smoke_ok:
            entry_failures.append({
                "severity": "blocker",
                "reason": smoke_msg,
            })

        # AC 41: only confirmed topology mismatch AND confirmed smoke failure
        # trigger blocked at the per-state level.  Either one can flip the
        # top-level status if its severity is 'blocker'.
        has_topology_blocker = any(
            f.get("severity") == "blocker" for f in topo_failures
        )
        has_smoke_blocker = not smoke_ok

        if has_topology_blocker or has_smoke_blocker:
            any_blocker = True

        result["intermediate_states"].append({
            "after_segment": seg_id,
            "topology_ok": topology_ok,
            "smoke_ok": smoke_ok,
            "failures": entry_failures,
        })

    if any_blocker:
        result["status"] = "blocked"
        # Surface a top-level summary line.
        result["failures"].append(
            "one or more intermediate states are blocked; see intermediate_states[]"
        )

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Plan intermediate-state topology + smoke check. Validates that "
            "between every pair of segments in execution-graph.json, the "
            "foundry codebase is in a topologically consistent and "
            "import-clean state."
        )
    )
    parser.add_argument("--root", required=True, help="Project root directory")
    parser.add_argument("--task-dir", required=True, help="Task directory (.dynos/task-{id})")
    args = parser.parse_args()

    try:
        root = Path(args.root).resolve()
    except OSError:
        # AC 36: fail-open on unresolvable root.
        out = {
            "status": "pass",
            "intermediate_states": [],
            "failures": [f"--root unresolvable: {args.root}"],
        }
        json.dump(out, sys.stdout)
        sys.stdout.write("\n")
        return 0

    try:
        task_dir = Path(args.task_dir).resolve()
    except OSError:
        out = {
            "status": "pass",
            "intermediate_states": [],
            "failures": [f"--task-dir unresolvable: {args.task_dir}"],
        }
        json.dump(out, sys.stdout)
        sys.stdout.write("\n")
        return 0

    result = run_check(root, task_dir)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
