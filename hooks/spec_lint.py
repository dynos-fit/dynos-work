"""hooks/spec_lint.py - spec.md anti-pattern linter.

Detects backtick-quoted entities that appear in BOTH measurement-target ACs
and structural-change ACs. Such co-location is unsafe: structural change
invalidates measurement baseline.

Stdlib only. CLI: --spec <path>. Exit 0 always.
Bypass: per-AC `<!-- spec-lint: ack -->` (same/adjacent line); whole-spec
        `<!-- spec-lint: ack-all -->` (anywhere).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_MEASUREMENT_KEYWORDS = (
    "at most",
    "at least",
    "byte-identical",
    "byte-for-byte",
    "sha-256",
    "unchanged",
    "≤",  # ≤
    "lines",
)
_STRUCTURAL_KEYWORDS = (
    "extract",
    "split",
    "promote",
    "move",
    "restructure",
    "refactor",
    "decompose",
)
_ACK_MARKER = "<!-- spec-lint: ack -->"
_ACK_ALL_MARKER = "<!-- spec-lint: ack-all -->"

# Backtick-quoted identifier. The surrounding backticks ARE the word boundary —
# so `log` inside `log_event` captures `log_event`, never the bare `log`.
_BACKTICK_ENTITY_RE = re.compile(r"`(\w+)`")

# AC bullet header. Matches lines beginning with "**AC-<n>:**" (preferred form
# in dynos-work specs) OR a bare "<n>. " ordered-list bullet at line start.
# Both are common in spec.md files we lint.
_AC_HEADER_RE = re.compile(
    r"^(?:\*\*AC-(\d+):\*\*|(\d+)\.\s+)",
    re.MULTILINE,
)


def _split_into_acs(spec_text: str) -> dict:
    """Return {ac_index: (ac_text, header_line_idx_0based, end_line_idx_0based_exclusive)}.

    Locates AC bullets and groups the body up to the next AC bullet OR a markdown
    heading line (starts with "#"). Indexes are 0-based offsets into the line list.
    Never raises.
    """
    result = {}
    if not isinstance(spec_text, str) or not spec_text:
        return result
    try:
        lines = spec_text.splitlines()
    except Exception:
        return result

    # Find every AC header location: (line_idx, ac_number).
    headers = []
    for i, line in enumerate(lines):
        m = _AC_HEADER_RE.match(line)
        if not m:
            continue
        num_str = m.group(1) or m.group(2)
        try:
            ac_num = int(num_str)
        except (TypeError, ValueError):
            continue
        headers.append((i, ac_num))

    if not headers:
        return result

    # Determine the body end for each header: next header line OR next heading.
    for idx, (line_i, ac_num) in enumerate(headers):
        if idx + 1 < len(headers):
            next_boundary = headers[idx + 1][0]
        else:
            next_boundary = len(lines)
        # Also stop at a markdown heading inside the range.
        end = next_boundary
        for j in range(line_i + 1, next_boundary):
            stripped = lines[j].lstrip()
            if stripped.startswith("#"):
                end = j
                break
        body_lines = lines[line_i:end]
        ac_text = "\n".join(body_lines)
        # Last-write-wins on duplicate AC numbers; conservative.
        result[ac_num] = (ac_text, line_i, end)
    return result


def extract_entities_per_ac(spec_text: str) -> dict:
    """Per AC, return the set of backtick-quoted \\b-anchored entities.

    Returns {ac_index: set[str]}. Empty dict if no ACs detected. Never raises.
    The backtick delimiters in the regex enforce the word boundary — `log`
    inside `log_event` captures `log_event` only.
    """
    out = {}
    try:
        acs = _split_into_acs(spec_text)
    except Exception:
        return out
    for ac_num, payload in acs.items():
        try:
            ac_text, _start, _end = payload
            matches = _BACKTICK_ENTITY_RE.findall(ac_text)
            out[ac_num] = set(matches)
        except Exception:
            # Malformed AC — skip silently.
            continue
    return out


def classify_ac_intent(ac_text: str) -> set:
    """Return subset of {"measurement", "structural"} via case-insensitive
    substring match against the keyword tuples.

    Never raises; non-string input returns empty set.
    """
    intent = set()
    if not isinstance(ac_text, str) or not ac_text:
        return intent
    try:
        lowered = ac_text.lower()
    except Exception:
        return intent
    for kw in _MEASUREMENT_KEYWORDS:
        if kw in lowered:
            intent.add("measurement")
            break
    for kw in _STRUCTURAL_KEYWORDS:
        if kw in lowered:
            intent.add("structural")
            break
    return intent


def _ac_is_acked(spec_lines: list, header_line_idx: int, body_end_idx: int) -> bool:
    """Return True if `_ACK_MARKER` appears on:
      - the AC header line, OR
      - the line immediately preceding the AC header, OR
      - the line immediately following the AC body.
    Bounds-checked. Marker is exact-case match.
    """
    candidates = []
    # Immediately preceding line.
    if header_line_idx - 1 >= 0:
        candidates.append(spec_lines[header_line_idx - 1])
    # AC header line itself.
    if 0 <= header_line_idx < len(spec_lines):
        candidates.append(spec_lines[header_line_idx])
    # Line immediately following the AC body (body_end_idx is exclusive end).
    if 0 <= body_end_idx < len(spec_lines):
        candidates.append(spec_lines[body_end_idx])
    for line in candidates:
        if isinstance(line, str) and _ACK_MARKER in line:
            return True
    return False


def detect_anti_patterns(spec_text: str):
    """Return (findings, acked) — both lists of dicts.

    Per backtick entity that appears in >=1 measurement AC AND >=1 structural AC,
    emit one item with shape:
        {entity, measurement_acs: list[int], structural_acs: list[int], message}

    Bypass rules:
      - Whole-spec `_ACK_ALL_MARKER` anywhere -> ALL findings move to acked.
      - Per-AC `_ACK_MARKER` (same/prev/next line) removes that AC's contribution.
        If removal empties either side, the entire finding moves to acked.
    Never raises.
    """
    findings = []
    acked = []
    if not isinstance(spec_text, str) or not spec_text:
        return findings, acked

    try:
        ack_all_active = _ACK_ALL_MARKER in spec_text
        spec_lines = spec_text.splitlines()
        acs = _split_into_acs(spec_text)
        if not acs:
            return findings, acked

        # Precompute per-AC: entities, intent, acked-flag.
        per_ac = {}
        for ac_num, (ac_text, line_i, end) in acs.items():
            try:
                entities = set(_BACKTICK_ENTITY_RE.findall(ac_text))
            except Exception:
                entities = set()
            try:
                intent = classify_ac_intent(ac_text)
            except Exception:
                intent = set()
            try:
                is_acked = _ac_is_acked(spec_lines, line_i, end)
            except Exception:
                is_acked = False
            per_ac[ac_num] = {
                "entities": entities,
                "intent": intent,
                "acked": is_acked,
            }

        # Build entity -> (measurement_acs, structural_acs, ack_contributors)
        # ack_contributors tracks per-AC suppressions: AC numbers whose entity
        # contribution is acked.
        entity_map = {}
        for ac_num, data in per_ac.items():
            for entity in data["entities"]:
                if not isinstance(entity, str):
                    continue
                rec = entity_map.setdefault(
                    entity,
                    {
                        "measurement_acs": [],
                        "structural_acs": [],
                        "acked_measurement_acs": [],
                        "acked_structural_acs": [],
                    },
                )
                if "measurement" in data["intent"]:
                    if data["acked"]:
                        rec["acked_measurement_acs"].append(ac_num)
                    else:
                        rec["measurement_acs"].append(ac_num)
                if "structural" in data["intent"]:
                    if data["acked"]:
                        rec["acked_structural_acs"].append(ac_num)
                    else:
                        rec["structural_acs"].append(ac_num)

        for entity, rec in entity_map.items():
            m_acs = sorted(rec["measurement_acs"])
            s_acs = sorted(rec["structural_acs"])
            am_acs = sorted(rec["acked_measurement_acs"])
            as_acs = sorted(rec["acked_structural_acs"])
            full_m = sorted(set(m_acs) | set(am_acs))
            full_s = sorted(set(s_acs) | set(as_acs))

            # Only emit if entity straddles BOTH categories at all (acked or not).
            if not (full_m and full_s):
                continue

            message = (
                f"Entity `{entity}` appears in measurement AC(s) {full_m} "
                f"AND structural AC(s) {full_s}. Structural change invalidates "
                f"measurement baseline."
            )
            item = {
                "entity": entity,
                "measurement_acs": full_m,
                "structural_acs": full_s,
                "message": message,
            }

            if ack_all_active:
                acked.append(item)
                continue

            # Per-AC ack: if removing acked contributors empties either side,
            # the entity is fully acked.
            if not m_acs or not s_acs:
                acked.append(item)
            else:
                findings.append(item)

        # Stable, deterministic ordering.
        findings.sort(key=lambda d: d["entity"])
        acked.sort(key=lambda d: d["entity"])
    except Exception:
        # Conservative: never crash. Drop partial work, return empties.
        return [], []

    return findings, acked


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="spec_lint",
        description="Lint a spec.md for measurement/structural anti-patterns.",
    )
    parser.add_argument("--spec", required=True, help="Path to spec.md")
    try:
        args = parser.parse_args()
    except SystemExit:
        # argparse exits non-zero on parse failure (e.g., missing --spec).
        # AC-23 mandates exit 0 always. Emit minimal JSON and return 0.
        try:
            print(
                json.dumps(
                    {
                        "spec_path": "",
                        "findings": [],
                        "acked": [],
                        "ack_all_active": False,
                        "skipped": True,
                    },
                    indent=2,
                )
            )
        except Exception:
            pass
        return 0

    spec_arg = args.spec
    try:
        spec_path = Path(spec_arg)
        if not spec_path.is_file():
            print(
                json.dumps(
                    {
                        "spec_path": str(spec_path),
                        "findings": [],
                        "acked": [],
                        "ack_all_active": False,
                        "skipped": True,
                    },
                    indent=2,
                )
            )
            return 0
        # errors="ignore" handles binary/garbage input without crashing.
        spec_text = spec_path.read_text(errors="ignore")
        findings, acked = detect_anti_patterns(spec_text)
        print(
            json.dumps(
                {
                    "spec_path": str(spec_path.resolve()),
                    "findings": findings,
                    "acked": acked,
                    "ack_all_active": _ACK_ALL_MARKER in spec_text,
                    "skipped": False,
                },
                indent=2,
            )
        )
    except Exception as exc:
        try:
            print(
                json.dumps(
                    {
                        "spec_path": str(spec_arg),
                        "findings": [],
                        "acked": [],
                        "ack_all_active": False,
                        "skipped": True,
                    },
                    indent=2,
                )
            )
        except Exception:
            pass
        try:
            print(
                f"spec_lint internal warning: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
