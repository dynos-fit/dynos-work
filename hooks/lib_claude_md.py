"""Deterministic rule-extraction pre-pass for the claude-md-auditor.

Reads CLAUDE.md files (local + global), classifies each non-empty line as
"hard" or "preference" using the AC-8 trigger regex, detects conflicts
between opposing rules on the same topic, and writes a structured JSON
artifact to {task_dir}/audit-reports/claude-md-rules-extracted.json.

Stdlib-only. No third-party imports.

CLI:
    python3 hooks/lib_claude_md.py \
        --root <repo-root> \
        --task-dir <.dynos/task-{id}> \
        [--local-claude-md <path>] \
        [--global-claude-md <path>]

Programmatic:
    from lib_claude_md import extract_rules
    result = extract_rules(local_path, global_path)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional, Union

# AC-8: tier classification regex (case-insensitive, applied to each non-empty line).
# Source: spec.md AC-8 — verbatim regex pattern.
TRIGGER_REGEX = re.compile(
    r'\b(never|must|do not|do[\s_]?NOT|always|important|critical|forbidden)\b',
    re.IGNORECASE,
)

# AC-10: hard per-file read cap in bytes.
# Source: spec.md AC-10 — 200,000 byte cap.
MAX_BYTES = 200_000

# Stopwords stripped during topic-token extraction for conflict detection.
# Source: spec.md "Conflict heuristic specifics".
STOPWORDS = frozenset({
    "the", "a", "an", "to", "of", "in", "on", "for", "is", "are",
    "use", "using", "when", "where", "with", "from", "as", "at", "by",
    "or", "and", "that", "this", "those", "these", "be", "will",
    "do", "does", "done", "all", "any", "every", "each",
})

# Affirmative triggers for conflict detection (AC-12).
AFFIRMATIVE_TRIGGERS = frozenset({"must", "always"})
# Prohibitive triggers for conflict detection (AC-12).
# Note: "do not" / "do_not" are matched as multi-token forms below.
PROHIBITIVE_TRIGGERS = frozenset({"never", "forbidden"})

# Regex used to identify directional triggers within a line (lowercased).
# Affirmative-only side: must / always.
_AFFIRMATIVE_LINE_REGEX = re.compile(r'\b(must|always)\b', re.IGNORECASE)
# Prohibitive-only side: never / do not / do_not / forbidden.
_PROHIBITIVE_LINE_REGEX = re.compile(
    r'\b(never|forbidden)\b|\bdo[\s_]+not\b',
    re.IGNORECASE,
)

# Regex to strip trigger phrases from a line for topic-token extraction.
# Includes multi-word trigger "do not" / "do_not".
_TRIGGER_STRIP_REGEX = re.compile(
    r'\b(never|must|always|important|critical|forbidden)\b|\bdo[\s_]+not\b',
    re.IGNORECASE,
)

# Regex to split into word tokens (alphanumerics, lowercased).
_WORD_TOKEN_REGEX = re.compile(r"[A-Za-z0-9]+")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_substantive_line(line: str) -> bool:
    """Return True if line is non-empty and not just a heading marker.

    Strips leading whitespace and "#" markers; if anything remains, the line
    is substantive.
    """
    stripped = line.strip()
    if not stripped:
        return False
    # Strip leading "#" and whitespace; if nothing remains, it's a bare heading marker.
    no_heading = stripped.lstrip("#").strip()
    return bool(no_heading)


def _classify_tier(line: str) -> str:
    """Apply AC-8 regex; return 'hard' if matched, else 'preference'."""
    return "hard" if TRIGGER_REGEX.search(line) else "preference"


def _read_file_with_cap(path: Path) -> tuple[Optional[str], Optional[dict]]:
    """Read a file with the AC-10 200,000-byte cap.

    Returns (text, truncation_warning_or_None). On read error or non-existent
    file, returns (None, None). On truncation, returns (text_of_first_200KB,
    warning_dict).
    """
    try:
        original_size = path.stat().st_size
    except (OSError, FileNotFoundError):
        return None, None

    truncated = False
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raw = raw[:MAX_BYTES]
            truncated = True
        # Decode tolerantly so a malformed multi-byte sequence at the cap
        # boundary cannot abort processing.
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return None, None

    warning: Optional[dict] = None
    if truncated:
        warning = {
            "file": str(path),
            "original_size_bytes": original_size,
            "truncated_at_bytes": MAX_BYTES,
            "message": (
                f"File exceeds {MAX_BYTES} bytes; processed first "
                f"{MAX_BYTES} bytes only."
            ),
        }
    return text, warning


def _extract_rules_from_file(path: Optional[Path]) -> tuple[list, Optional[dict]]:
    """Read a CLAUDE.md file and return (rules, truncation_warning_or_None).

    Missing or unreadable file → ([], None).
    """
    if path is None:
        return [], None
    if not path.exists() or not path.is_file():
        return [], None

    text, warning = _read_file_with_cap(path)
    if text is None:
        return [], None

    rules: list = []
    # splitlines() preserves logical line numbers (1-based).
    for idx, line in enumerate(text.splitlines(), start=1):
        if not _is_substantive_line(line):
            continue
        rules.append({
            "file": str(path),
            "line": idx,
            "text": line,
            "tier": _classify_tier(line),
        })

    return rules, warning


def _topic_token(text: str) -> str:
    """Extract the first non-trigger non-stopword token from a line.

    Lowercased. Returns empty string if no qualifying token is found.
    """
    # Strip triggers first so they don't leak into the topic.
    stripped = _TRIGGER_STRIP_REGEX.sub(" ", text)
    for match in _WORD_TOKEN_REGEX.finditer(stripped):
        token = match.group(0).lower()
        if token in STOPWORDS:
            continue
        return token
    return ""


def _direction(text: str) -> Optional[str]:
    """Return 'affirmative', 'prohibitive', 'mixed', or None.

    A rule is purely affirmative if it contains must/always but no
    never/do-not/forbidden. Purely prohibitive if it contains
    never/do-not/forbidden but no must/always. 'mixed' if both. None if
    neither (i.e., the rule is a preference-only line).
    """
    has_affirm = bool(_AFFIRMATIVE_LINE_REGEX.search(text))
    has_prohib = bool(_PROHIBITIVE_LINE_REGEX.search(text))
    if has_affirm and has_prohib:
        return "mixed"
    if has_affirm:
        return "affirmative"
    if has_prohib:
        return "prohibitive"
    return None


def _detect_conflicts(local_rules: list, global_rules: list) -> list:
    """Return conflict objects per AC-12.

    Conflict requires:
      (a) topic-token match (case-insensitive, on first non-stopword
          non-trigger token), AND
      (b) directional opposition (one purely affirmative vs one purely
          prohibitive).
    Skip if either topic is empty.
    """
    conflicts: list = []
    # Pre-compute topics + directions for global rules to avoid O(n*m) recompute.
    global_meta = []
    for grule in global_rules:
        gtext = grule["text"]
        gtopic = _topic_token(gtext)
        gdir = _direction(gtext)
        global_meta.append((grule, gtopic, gdir))

    for lrule in local_rules:
        ltext = lrule["text"]
        ltopic = _topic_token(ltext)
        if not ltopic:
            continue
        ldir = _direction(ltext)
        if ldir not in ("affirmative", "prohibitive"):
            continue
        for grule, gtopic, gdir in global_meta:
            if not gtopic:
                continue
            if ltopic != gtopic:
                continue
            if gdir not in ("affirmative", "prohibitive"):
                continue
            if ldir == gdir:
                continue
            # Directional opposition confirmed.
            conflicts.append({
                "local_rule": lrule,
                "global_rule": grule,
                "topic": ltopic,
                "description": (
                    f"Conflict on topic '{ltopic}': local rule is {ldir} "
                    f"('{ltext}'), global rule is {gdir} ('{grule['text']}')."
                ),
            })
    return conflicts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_rules(
    local_path: Union[str, Path, None],
    global_path: Union[str, Path, None],
) -> dict:
    """Extract rules from local + global CLAUDE.md files.

    Returns a dict with keys:
      - local_rules:        list[dict]  (may be empty)
      - global_rules:       list[dict]  (may be empty)
      - conflicts:          list[dict]  (may be empty)
      - no_rules_found:     bool
      - truncation_warnings: list[dict] (may be empty)

    Rule object fields (exactly these): file, line, text, tier.
    Missing/unreadable files yield empty rule lists with no warning.
    """
    local_p: Optional[Path] = Path(local_path) if local_path is not None else None
    global_p: Optional[Path] = Path(global_path) if global_path is not None else None

    local_rules, local_warn = _extract_rules_from_file(local_p)
    global_rules, global_warn = _extract_rules_from_file(global_p)

    truncation_warnings: list = []
    if local_warn is not None:
        truncation_warnings.append(local_warn)
    if global_warn is not None:
        truncation_warnings.append(global_warn)

    # AC-11: no_rules_found is True only if BOTH files are absent (or unreadable).
    # "Absent" is interpreted as: path is None OR path does not exist.
    local_present = local_p is not None and local_p.exists() and local_p.is_file()
    global_present = global_p is not None and global_p.exists() and global_p.is_file()
    no_rules_found = not (local_present or global_present)

    conflicts = _detect_conflicts(local_rules, global_rules)

    return {
        "local_rules": local_rules,
        "global_rules": global_rules,
        "conflicts": conflicts,
        "no_rules_found": no_rules_found,
        "truncation_warnings": truncation_warnings,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic rule-extraction pre-pass for the claude-md-auditor. "
            "Reads local + global CLAUDE.md files and writes a structured JSON "
            "artifact to {task_dir}/audit-reports/claude-md-rules-extracted.json."
        ),
    )
    parser.add_argument(
        "--root",
        required=True,
        help="Repository root path (used to locate the local CLAUDE.md by default).",
    )
    parser.add_argument(
        "--task-dir",
        required=True,
        help="Task working directory (e.g. .dynos/task-{id}); audit-reports/ is created here.",
    )
    parser.add_argument(
        "--local-claude-md",
        default=None,
        help="Override path for the local CLAUDE.md (default: <root>/CLAUDE.md).",
    )
    parser.add_argument(
        "--global-claude-md",
        default=None,
        help="Override path for the global CLAUDE.md (default: ~/.claude/CLAUDE.md).",
    )
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    root = Path(args.root)
    task_dir = Path(args.task_dir)

    local_md = (
        Path(args.local_claude_md)
        if args.local_claude_md is not None
        else root / "CLAUDE.md"
    )
    global_md = (
        Path(args.global_claude_md)
        if args.global_claude_md is not None
        else Path(os.path.expanduser("~/.claude/CLAUDE.md"))
    )

    result = extract_rules(local_md, global_md)

    # AC-34: ensure audit-reports/ directory exists before writing.
    audit_reports_dir = task_dir / "audit-reports"
    audit_reports_dir.mkdir(parents=True, exist_ok=True)

    # AC-37: output path.
    output_path = audit_reports_dir / "claude-md-rules-extracted.json"
    output_path.write_text(
        json.dumps(result, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
