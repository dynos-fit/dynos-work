"""hooks/plan_signature_check.py - signature-claim verification.

Extracts function-signature claims from spec.md and compares them to plan.md
`### Component:` subsections. Warnings on ambiguity, findings only on
demonstrable spec/plan mismatch for the same fn name.

Stdlib only. CLI: --root --task-dir. Exit 0 always.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Regexes (kept simple/anchored to avoid catastrophic backtracking)
# ---------------------------------------------------------------------------

# Find AC headings like "**AC-1:**" or "**AC-12:**" at line start.
_AC_HEADING_RE = re.compile(r"^\*\*AC-(\d+):\*\*", re.MULTILINE)

# Typed-arg call/def pattern inside an AC bullet:
#   fn_name(arg: Type[, ...]) [-> Ret]
# We require ":" inside the parens (i.e., a type annotation) to qualify as
# a "signature claim" per AC-10.
_TYPED_CALL_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(([^()\n]*:\s*[^()\n]*)\)"
)

# def line inside fenced python block: def fn_name(...) [-> Ret]:
_DEF_RE = re.compile(
    r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*(?:->\s*([^:\n]+))?\s*:",
    re.MULTILINE,
)

# Fenced code block starting with ```python (any case) until closing ```.
_PY_FENCE_RE = re.compile(
    r"```(?:python|py)\b(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

# `### Component: ...` heading
_COMPONENT_HEADING_RE = re.compile(r"^###\s+Component:\s*(.+?)\s*$", re.MULTILINE)

# Backtick-wrapped signature in a plan bullet:
#   - `fn_name(args) -> Ret` ...
_BACKTICK_SIG_RE = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_]*)\s*\(([^`)]*)\)\s*(?:->\s*([^`]+?))?`"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_args(arg_str: str) -> list[str]:
    """Split a top-level comma-separated argument list, respecting [] brackets.

    Conservative: used only for surface-level token counting/normalization.
    """
    args: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in arg_str:
        if ch in "[(":
            depth += 1
            buf.append(ch)
        elif ch in "])":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            tok = "".join(buf).strip()
            if tok:
                args.append(tok)
            buf = []
        else:
            buf.append(ch)
    tok = "".join(buf).strip()
    if tok:
        args.append(tok)
    return args


def _arg_name(arg: str) -> str:
    """Return the bare arg name (strip type annotation, default value, *, **)."""
    name = arg.strip().lstrip("*").strip()
    if ":" in name:
        name = name.split(":", 1)[0].strip()
    if "=" in name:
        name = name.split("=", 1)[0].strip()
    return name


def _is_ambiguous_args(args: list[str]) -> bool:
    """*args / **kwargs / empty unparseable -> ambiguous."""
    for a in args:
        s = a.strip()
        if s.startswith("*"):
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_signature_claims(spec_text: str) -> list[dict]:
    """Extract function-signature claims from spec.md text.

    Each claim: {ac_index: int (1-based), fn_name: str, raw_text: str}.
    Triggers on either:
      1. A typed-arg call pattern (fn(arg: Type)) inside an AC bullet, OR
      2. A `def fn(...)` line inside a fenced python block within the AC.

    Conservative: must not crash on malformed spec.
    """
    if not isinstance(spec_text, str) or not spec_text:
        return []

    claims: list[dict] = []

    try:
        # Locate all AC headings and slice the text into per-AC chunks.
        matches = list(_AC_HEADING_RE.finditer(spec_text))
        if not matches:
            return []

        for i, m in enumerate(matches):
            try:
                ac_index = int(m.group(1))
            except (ValueError, IndexError):
                continue
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(spec_text)
            ac_body = spec_text[start:end]

            seen_in_ac: set[str] = set()

            # 1) Fenced python blocks first (more authoritative).
            try:
                for fence_m in _PY_FENCE_RE.finditer(ac_body):
                    block = fence_m.group(1) or ""
                    for def_m in _DEF_RE.finditer(block):
                        fn_name = def_m.group(1)
                        if not fn_name or fn_name in seen_in_ac:
                            continue
                        raw_text = def_m.group(0).strip()
                        claims.append({
                            "ac_index": ac_index,
                            "fn_name": fn_name,
                            "raw_text": raw_text,
                        })
                        seen_in_ac.add(fn_name)
            except re.error:
                pass

            # 2) Typed-call pattern in the bullet text (skip code-fences body
            #    we've already processed by simply allowing duplicates to be
            #    suppressed via seen_in_ac).
            try:
                for call_m in _TYPED_CALL_RE.finditer(ac_body):
                    fn_name = call_m.group(1)
                    # Skip Python keywords that look like calls.
                    if not fn_name or fn_name in {
                        "if", "for", "while", "return", "with", "print",
                        "def", "class", "elif", "and", "or", "not", "in",
                        "is", "lambda", "yield", "raise", "except", "import",
                        "from", "as", "assert", "del", "global", "nonlocal",
                        "pass", "break", "continue",
                    }:
                        continue
                    if fn_name in seen_in_ac:
                        continue
                    raw_text = call_m.group(0).strip()
                    claims.append({
                        "ac_index": ac_index,
                        "fn_name": fn_name,
                        "raw_text": raw_text,
                    })
                    seen_in_ac.add(fn_name)
            except re.error:
                pass

    except Exception:
        # Conservative: never crash. Return whatever we accumulated so far.
        return claims

    return claims


def extract_plan_signatures(plan_text: str) -> dict[str, dict]:
    """Extract per-component function signatures from plan.md text.

    Returns: {fn_name: {args: list[str], returns: str|None, source_section: str}}.
    Only scans inside `### Component:` subsections. Empty dict if none found.
    """
    if not isinstance(plan_text, str) or not plan_text:
        return {}

    out: dict[str, dict] = {}

    try:
        headings = list(_COMPONENT_HEADING_RE.finditer(plan_text))
        if not headings:
            return {}

        for i, h in enumerate(headings):
            section_label = "### Component: " + (h.group(1) or "").strip()
            start = h.end()
            # End at the next ### heading (component or otherwise) or EOF.
            next_heading_re = re.compile(r"^###\s+", re.MULTILINE)
            sub = plan_text[start:]
            nxt = next_heading_re.search(sub)
            section_body = sub[: nxt.start()] if nxt else sub

            # 1) Backtick-wrapped signatures (preferred form in plan bullets).
            try:
                for sm in _BACKTICK_SIG_RE.finditer(section_body):
                    fn_name = sm.group(1)
                    if not fn_name or fn_name in out:
                        continue
                    arg_str = sm.group(2) or ""
                    ret = sm.group(3)
                    args = _split_args(arg_str)
                    out[fn_name] = {
                        "args": args,
                        "returns": ret.strip() if ret else None,
                        "source_section": section_label,
                    }
            except re.error:
                pass

            # 2) `def fn(...)` lines (e.g., in fenced python blocks).
            try:
                for dm in _DEF_RE.finditer(section_body):
                    fn_name = dm.group(1)
                    if not fn_name or fn_name in out:
                        continue
                    arg_str = dm.group(2) or ""
                    ret = dm.group(3)
                    args = _split_args(arg_str)
                    out[fn_name] = {
                        "args": args,
                        "returns": ret.strip() if ret else None,
                        "source_section": section_label,
                    }
            except re.error:
                pass

    except Exception:
        return out

    return out


def _parse_claim_signature(raw_text: str) -> dict | None:
    """Best-effort parse of a claim's raw_text into {args, returns}.

    Returns None when the surface text isn't parseable enough for a
    definitive comparison (ambiguity -> warning by caller).
    """
    if not isinstance(raw_text, str) or not raw_text:
        return None

    text = raw_text.strip()

    # Try as a `def ...:` line via AST first.
    try:
        candidate = text
        if candidate.startswith("def "):
            if not candidate.rstrip().endswith(":"):
                candidate = candidate.rstrip() + ":"
            tree = ast.parse(candidate + "\n    ...")
            for node in tree.body:
                if isinstance(node, ast.FunctionDef):
                    args = []
                    for a in node.args.args:
                        args.append(a.arg)
                    if node.args.vararg:
                        args.append("*" + node.args.vararg.arg)
                    if node.args.kwarg:
                        args.append("**" + node.args.kwarg.arg)
                    ret = None
                    if node.returns is not None:
                        try:
                            ret = ast.unparse(node.returns).strip()
                        except Exception:
                            ret = None
                    return {"args": args, "returns": ret, "raw": text}
    except (SyntaxError, ValueError):
        pass
    except Exception:
        pass

    # Fall back to regex on a call-style claim: fn(arg: Type, ...) [-> Ret]
    try:
        m = re.match(
            r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*(?:->\s*(.+?))?\s*$",
            text,
        )
        if not m:
            return None
        arg_str = m.group(2) or ""
        ret = m.group(3)
        args_full = _split_args(arg_str)
        # Reject if any arg is impossible to interpret (empty token, etc.)
        if any(not _arg_name(a) for a in args_full if a.strip()):
            return None
        return {
            "args": args_full,
            "returns": ret.strip() if ret else None,
            "raw": text,
        }
    except re.error:
        return None
    except Exception:
        return None


def compare_signatures(claims, plan_sigs):
    """Compare spec claims against plan signatures.

    Returns (findings, warnings).
      finding: {ac_index, fn_name, spec_claim, plan_actual, severity="mismatch"}
      warning: {ac_index, fn_name, reason}

    Conservative rules:
      - Missing plan section -> warning ("function not found in plan Component sections").
      - *args / **kwargs / unparseable on either side -> warning.
      - Finding ONLY when both sides parseable AND args/returns demonstrably differ.
    """
    findings: list[dict] = []
    warnings: list[dict] = []

    if claims is None:
        return findings, warnings
    if plan_sigs is None:
        plan_sigs = {}

    try:
        for claim in claims:
            try:
                fn_name = claim.get("fn_name")
                ac_index = claim.get("ac_index")
                raw_text = claim.get("raw_text", "")
            except AttributeError:
                continue
            if not fn_name:
                continue

            # Missing plan section -> warning.
            if fn_name not in plan_sigs:
                warnings.append({
                    "ac_index": ac_index,
                    "fn_name": fn_name,
                    "reason": "function not found in plan Component sections",
                })
                continue

            plan_entry = plan_sigs[fn_name] or {}
            plan_args = plan_entry.get("args") or []
            plan_ret = plan_entry.get("returns")

            # Ambiguity from plan side.
            if _is_ambiguous_args(plan_args):
                warnings.append({
                    "ac_index": ac_index,
                    "fn_name": fn_name,
                    "reason": "plan signature uses *args/**kwargs; cannot compare definitively",
                })
                continue

            # Try to parse the spec claim.
            parsed = _parse_claim_signature(raw_text)
            if parsed is None:
                warnings.append({
                    "ac_index": ac_index,
                    "fn_name": fn_name,
                    "reason": "spec claim text could not be parsed for comparison",
                })
                continue

            spec_args = parsed["args"]
            spec_ret = parsed["returns"]

            # Ambiguity from spec side.
            if _is_ambiguous_args(spec_args):
                warnings.append({
                    "ac_index": ac_index,
                    "fn_name": fn_name,
                    "reason": "spec claim uses *args/**kwargs; cannot compare definitively",
                })
                continue

            # Demonstrable mismatch:
            # 1) arg-count differs.
            # 2) returns both present AND differ literally (after strip).
            mismatch_reason = None
            if len(spec_args) != len(plan_args):
                mismatch_reason = (
                    f"arg count differs: spec={len(spec_args)} plan={len(plan_args)}"
                )
            elif spec_ret is not None and plan_ret is not None:
                if spec_ret.strip() != plan_ret.strip():
                    mismatch_reason = (
                        f"return type differs: spec={spec_ret!r} plan={plan_ret!r}"
                    )

            if mismatch_reason:
                findings.append({
                    "ac_index": ac_index,
                    "fn_name": fn_name,
                    "spec_claim": raw_text,
                    "plan_actual": {
                        "args": plan_args,
                        "returns": plan_ret,
                        "source_section": plan_entry.get("source_section", ""),
                    },
                    "severity": "mismatch",
                })

    except Exception:
        # Never raise. Return whatever was accumulated.
        return findings, warnings

    return findings, warnings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _empty_payload(task_id: str, spec_path: str, plan_path: str, reason: str) -> dict:
    return {
        "task_id": task_id,
        "spec_path": spec_path,
        "plan_path": plan_path,
        "claims_found": 0,
        "findings": [],
        "warnings": [],
        "skipped": True,
        "reason": reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan signature check - compare spec.md function-signature claims to plan.md ### Component subsections",
    )
    parser.add_argument("--root", required=True, help="Project root directory")
    parser.add_argument("--task-dir", required=True, help="Task directory (.dynos/task-{id})")
    args = parser.parse_args()

    task_dir_str = args.task_dir
    try:
        task_dir = Path(task_dir_str)
        task_id = task_dir.name
        spec_path = task_dir / "spec.md"
        plan_path = task_dir / "plan.md"

        if not spec_path.is_file() or not plan_path.is_file():
            payload = _empty_payload(
                task_id,
                str(spec_path),
                str(plan_path),
                "spec.md or plan.md not found",
            )
        else:
            try:
                spec_text = spec_path.read_text(errors="ignore")
            except OSError as exc:
                payload = _empty_payload(
                    task_id,
                    str(spec_path),
                    str(plan_path),
                    f"could not read spec.md: {type(exc).__name__}",
                )
                print(json.dumps(payload, indent=2))
                return 0

            try:
                plan_text = plan_path.read_text(errors="ignore")
            except OSError as exc:
                payload = _empty_payload(
                    task_id,
                    str(spec_path),
                    str(plan_path),
                    f"could not read plan.md: {type(exc).__name__}",
                )
                print(json.dumps(payload, indent=2))
                return 0

            claims = extract_signature_claims(spec_text)
            plan_sigs = extract_plan_signatures(plan_text)
            findings, warnings = compare_signatures(claims, plan_sigs)
            payload = {
                "task_id": task_id,
                "spec_path": str(spec_path),
                "plan_path": str(plan_path),
                "claims_found": len(claims),
                "findings": findings,
                "warnings": warnings,
                "skipped": False,
                "reason": None,
            }

        print(json.dumps(payload, indent=2))
    except Exception as exc:
        try:
            tid = Path(task_dir_str).name
        except Exception:
            tid = ""
        print(json.dumps(_empty_payload(
            tid,
            "",
            "",
            f"unhandled error: {type(exc).__name__}",
        ), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
