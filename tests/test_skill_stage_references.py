"""Linter test: every stage name referenced in skills/*/SKILL.md must
exist in lib_core.STAGE_ORDER.

Caught the "REPAIR" bug at execute/SKILL.md:299 (the actual stage is
REPAIR_PLANNING; REPAIR has not existed since the state machine split).
Generally guards against skill prose drifting out of sync with the
state machine.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import STAGE_ORDER

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"

# AC12 (task-20260419-009): shared source-glob constant consumed by every
# lint below. Non-recursive per design-decision D9 — adding ``**`` would
# sweep unrelated template subdirs. Tuple shape is (base_dir, glob_pattern);
# tests iterate both slots and union the matches.
SKILL_SOURCE_GLOBS: list[tuple[Path, str]] = [
    (REPO_ROOT / "skills", "*/SKILL.md"),
    (REPO_ROOT / "cli" / "assets" / "templates" / "base", "*.md"),
]


def _iter_skill_sources() -> list[Path]:
    """Return every .md file named by SKILL_SOURCE_GLOBS, deterministically
    sorted. Non-existent bases are silently skipped so a fresh checkout
    without the templates dir does not spuriously pass."""
    found: list[Path] = []
    for base, pattern in SKILL_SOURCE_GLOBS:
        if not base.exists():
            continue
        found.extend(sorted(base.glob(pattern)))
    return found

# Patterns that indicate a stage name is being referenced as a transition target.
# These are the prose patterns the orchestrator follows literally.
_TRANSITION_PATTERNS = [
    # `python3 hooks/ctl.py transition .dynos/task-{id} STAGE_NAME`
    re.compile(r"hooks/ctl\.py\s+transition\s+\S+\s+([A-Z_][A-Z0-9_]+)"),
    # "Update manifest.json stage to STAGE_NAME" / "Update ... stage to `STAGE`"
    re.compile(r"Update\s+(?:manifest\.json\s+)?stage\s+to\s+`?([A-Z_][A-Z0-9_]+)`?"),
    # "transition_task(task_dir, "STAGE")" or "transition_task(.., 'STAGE')"
    re.compile(r"transition_task\([^)]*[\"']([A-Z_][A-Z0-9_]+)[\"']"),
]

# Names that look like stage references but are not actually stages
# (e.g., section headings, code-related identifiers).
_NOT_A_STAGE = {"STAGE", "STAGES", "TODO", "FIXME", "HACK", "NOTE", "PLUGIN_HOOKS",
                "PYTHONPATH", "EOF", "AGENT_JSON_OUTPUT", "EXECUTOR_PLAN_JSON"}


# Marker that delegates a stage transition to the scheduler event bus.
# Example: `<!-- scheduler-owned: SPEC_REVIEW -> PLANNING -->`
_SCHEDULER_OWNED_RE = re.compile(r"<!--\s*scheduler-owned:\s*([A-Z_][A-Z0-9_]*)\s*->\s*([A-Z_][A-Z0-9_]*)\s*-->")


# Substrings that together form the "manually set stage" anti-pattern. The
# detector flags any co-occurrence of these two substrings within a small
# byte window, mirroring how a human would notice the phrase at a glance.
_MANUAL_NEEDLE = b"manually set"
_STAGE_NEEDLE = b'"stage"'
_MANUAL_STAGE_WINDOW_BYTES = 80


def _extract_stage_references(text: str) -> set[str]:
    """Return the set of stage-like tokens referenced via transition patterns."""
    found: set[str] = set()
    for pattern in _TRANSITION_PATTERNS:
        for match in pattern.finditer(text):
            token = match.group(1)
            if token in _NOT_A_STAGE:
                continue
            found.add(token)
    return found


def _all_byte_offsets(haystack: bytes, needle: bytes) -> list[int]:
    """Return every starting byte offset where needle occurs in haystack.

    Pure literal substring search — no regex, no normalization. Overlaps
    are permitted because the detector cares about any co-occurrence.
    """
    offsets: list[int] = []
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            return offsets
        offsets.append(idx)
        start = idx + 1


def find_manual_stage_advice(data: bytes | str) -> tuple[int, int] | None:
    """Locate the `manually set` / `"stage"` anti-pattern within a window.

    Scans the raw byte payload for every occurrence of the case-sensitive
    literal substring `manually set` and every occurrence of the literal
    substring `"stage"` (with the double-quote characters). Returns the
    (manual_offset, stage_offset) pair whose nearest edges fall within
    `_MANUAL_STAGE_WINDOW_BYTES` of each other, or `None` if no such pair
    exists. The window is measured edge-to-edge: if `manually set` ends
    before `"stage"` begins, the gap is `stage_start - manual_end`; if
    `"stage"` ends before `manually set` begins, the gap is
    `manual_start - stage_end`; overlapping spans yield a gap of 0.

    Accepts either `bytes` or `str`; strings are encoded as UTF-8 so the
    raw-byte contract holds regardless of caller convenience.
    """
    if isinstance(data, str):
        buf = data.encode("utf-8")
    else:
        buf = data

    manual_offsets = _all_byte_offsets(buf, _MANUAL_NEEDLE)
    if not manual_offsets:
        return None
    stage_offsets = _all_byte_offsets(buf, _STAGE_NEEDLE)
    if not stage_offsets:
        return None

    manual_len = len(_MANUAL_NEEDLE)
    stage_len = len(_STAGE_NEEDLE)

    for m in manual_offsets:
        m_end = m + manual_len
        for s in stage_offsets:
            s_end = s + stage_len
            if m_end <= s:
                gap = s - m_end
            elif s_end <= m:
                gap = m - s_end
            else:
                gap = 0
            if gap <= _MANUAL_STAGE_WINDOW_BYTES:
                return (m, s)
    return None


def is_manual_stage_advice(data: bytes | str) -> bool:
    """Boolean form of `find_manual_stage_advice` for ergonomic assertions."""
    return find_manual_stage_advice(data) is not None


def test_all_skill_stage_references_are_valid():
    """Every stage name referenced in any skill's prose must exist in STAGE_ORDER.

    If this fails, either the skill is wrong (most likely) or the state
    machine was renamed without updating the skills.
    """
    valid_stages = set(STAGE_ORDER)
    failures: list[str] = []

    for skill_md in _iter_skill_sources():
        text = skill_md.read_text()
        refs = _extract_stage_references(text)
        unknown = refs - valid_stages
        if unknown:
            relpath = skill_md.relative_to(REPO_ROOT)
            for stage in sorted(unknown):
                failures.append(f"{relpath}: references unknown stage {stage!r}")

    assert not failures, (
        "Skill prose references stages that are not in lib_core.STAGE_ORDER:\n"
        + "\n".join(f"  - {f}" for f in failures)
    )


def test_extractor_finds_known_patterns():
    """Sanity check that the regex patterns match the prose conventions
    the skills actually use."""
    sample = """
    Some prose. Then `python3 hooks/ctl.py transition .dynos/task-{id} EXECUTION` happens.
    Update manifest.json stage to `CHECKPOINT_AUDIT` here.
    The code calls transition_task(task_dir, "DONE") at the end.
    """
    refs = _extract_stage_references(sample)
    assert "EXECUTION" in refs
    assert "CHECKPOINT_AUDIT" in refs
    assert "DONE" in refs


def test_extractor_ignores_non_stage_uppercase_tokens():
    sample = """
    Reference $PYTHONPATH and ${PLUGIN_HOOKS} freely — these are not stage refs.
    A heading like ## STAGE is not a transition target either.
    """
    refs = _extract_stage_references(sample)
    assert refs == set(), f"expected no stage refs, got {refs}"


# Detects the duplicate-stage-write pattern that polluted execution-log.md
# with double entries and out-of-order timestamps (root cause: skills told
# the orchestrator to manually append `[STAGE] → X` AND called transition_task,
# which already auto-appends the same line via _auto_log).
_FENCE_OPEN_OR_CLOSE = re.compile(r"^```")
_STAGE_LINE = re.compile(r"\[STAGE\]\s*→\s*([A-Z_]+)")


def _stage_lines_inside_fences(text: str) -> list[tuple[int, str]]:
    """Return (line_no, stage_name) for every `[STAGE] → X` that appears
    inside a fenced code block.

    Uses a line-based state machine instead of a regex pair-match so that
    closing fences can never be mistaken for openings. Toggles in_fence on
    each line whose first three chars are ```.
    """
    found: list[tuple[int, str]] = []
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        if _FENCE_OPEN_OR_CLOSE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        m = _STAGE_LINE.search(line)
        if m:
            found.append((line_no, m.group(1)))
    return found


def test_no_skill_prose_writes_stage_lines_inside_fenced_blocks():
    """Skill prose must not instruct the orchestrator to manually append
    `[STAGE] → X` lines inside fenced code blocks. transition_task() in
    hooks/lib_core.py:_auto_log is the single authoritative writer of
    those lines. Duplicate writers caused doubled and out-of-order log
    entries (see .dynos/task-20260417-014/execution-log.md for the
    historical evidence).

    Inline references like `[STAGE] → PLANNING` inside backticks (i.e.,
    OUTSIDE a fenced block) are allowed — those are descriptive
    meta-comments about what auto-log does, not instructions to write.
    """
    failures: list[str] = []
    for skill_md in _iter_skill_sources():
        text = skill_md.read_text()
        for line_no, stage in _stage_lines_inside_fences(text):
            relpath = skill_md.relative_to(REPO_ROOT)
            failures.append(
                f"{relpath}:{line_no}: '[STAGE] → {stage}' inside a fenced code block "
                f"(would be a duplicate write — transition_task auto-logs it)"
            )

    assert not failures, (
        "Found duplicate-stage-write instructions in skill prose:\n"
        + "\n".join(f"  - {f}" for f in failures)
        + "\n\nIf you need a literal '[STAGE]' line in a fenced block as "
        "documentation, move it to inline backticks (e.g. `[STAGE] → X`) "
        "instead — that signals 'auto-log writes this' rather than "
        "'agent should write this'."
    )


def test_state_machine_distinguishes_inline_from_fenced():
    """Inline backticks should NOT trigger; only true in-fence lines should."""
    sample = """
Some prose with `[STAGE] → PLANNING` in inline backticks (descriptive).

```text
{timestamp} [STAGE] → CHECKPOINT_AUDIT
```

More prose with `[STAGE] → DONE` inline (descriptive).
"""
    found = _stage_lines_inside_fences(sample)
    stages = [s for _, s in found]
    assert stages == ["CHECKPOINT_AUDIT"], (
        f"only the in-fence stage should be reported, got {stages}"
    )


def test_no_skill_prose_advises_manual_stage_edit():
    """Prose must never tell the orchestrator to hand-edit `manifest.json`
    around the `stage` key. The canonical path is `transition_task(...)`;
    any literal "manually set ... \"stage\"" sentence is an escape hatch
    that leaves `stage` and companion fields (e.g., `completion_at`) out
    of sync and bypasses the receipt-gate that `transition_task` enforces.

    Flags any `manually set` occurrence within 80 raw bytes of a literal
    `"stage"` occurrence (edges measured edge-to-edge; see
    `find_manual_stage_advice`). Reports both byte offsets so the
    offending phrase can be pinpointed without re-reading the file.
    """
    failures: list[str] = []
    for skill_md in _iter_skill_sources():
        raw = skill_md.read_bytes()
        hit = find_manual_stage_advice(raw)
        if hit is not None:
            manual_off, stage_off = hit
            relpath = skill_md.relative_to(REPO_ROOT)
            failures.append(
                f"{relpath}: 'manually set' at byte {manual_off} is within "
                f"{_MANUAL_STAGE_WINDOW_BYTES} bytes of '\"stage\"' at byte "
                f"{stage_off} (manual-stage-edit anti-pattern)"
            )

    assert not failures, (
        "Skill prose advises manually hand-editing the 'stage' key — use "
        "transition_task(...) instead:\n"
        + "\n".join(f"  - {f}" for f in failures)
    )


def test_manual_stage_advice_detector_flags_bad_fragment():
    """Regression test for the detector helper itself.

    A synthetic fragment with the two substrings within 80 bytes must
    return True; a fragment with the substrings >80 bytes apart must
    return False. Both assertions guard against future "fix" attempts
    that silently widen or collapse the detection window.
    """
    # Substrings sit a few bytes apart — well inside the 80-byte window.
    bad = 'If calling directly fails, manually set both "stage": "DONE" and ...'
    assert is_manual_stage_advice(bad) is True, (
        "detector failed to flag an obviously-bad fragment; "
        f"offset lookup returned {find_manual_stage_advice(bad)!r}"
    )

    # Substrings separated by well over 80 bytes of filler must NOT trip.
    filler = "x" * 200
    good = f"manually set a reminder {filler} then later tweak the config for \"stage\" builds"
    assert len(good.encode("utf-8")) > 200
    assert is_manual_stage_advice(good) is False, (
        "detector flagged a fragment whose substrings are >80 bytes apart; "
        f"offset lookup returned {find_manual_stage_advice(good)!r}"
    )


def test_scheduler_owned_transitions_are_exempt_from_transition_prose_requirement():
    """Any `<!-- scheduler-owned: FROM -> TO -->` marker present in skill
    prose must name a transition that is currently approved as
    scheduler-driven. For the POC scope, only `SPEC_REVIEW -> PLANNING`
    is delegated to the scheduler; every other pair is a premature
    handoff that would bypass `compute_next_stage` until the migration
    tasks extend it.
    """
    failures: list[str] = []
    for skill_md in _iter_skill_sources():
        text = skill_md.read_text()
        for match in _SCHEDULER_OWNED_RE.finditer(text):
            from_stage, to_stage = match.group(1), match.group(2)
            path = skill_md.relative_to(REPO_ROOT)
            if (from_stage, to_stage) != ("SPEC_REVIEW", "PLANNING"):
                failures.append(
                    f"scheduler-owned marker for {from_stage} -> {to_stage} in "
                    f"{path} is beyond POC scope; only SPEC_REVIEW -> PLANNING "
                    f"is permitted until follow-up migration tasks extend "
                    f"compute_next_stage"
                )

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# AC10 (task-20260419-009): "If available in this repo" anti-pattern lint.
#
# Any co-occurrence of the literal substring ``If available in this repo``
# within 20 lines (before OR after) of a fenced code block whose contents
# include ``ctl.py transition `` (trailing space, avoids matching
# ``transition_task``) or ``transition_task(`` is a transition-site escape
# hatch and fails the lint.
#
# AC9 exempts the five read-only helper skills: ``skills/status``,
# ``skills/resume``, ``skills/calibration``, ``skills/trajectory``,
# ``skills/execute``. These skills carry fallback read-only commands
# (``status``, ``resume``, ``validate-task``, etc.) whose anti-pattern is
# benign; only transition-forging sites are flagged.
# ---------------------------------------------------------------------------


_AC10_PROSE_NEEDLE = "If available in this repo"

# Transition-forging sub-strings inside a fenced block that trigger the
# lint. Note the trailing space on "ctl.py transition " — it makes the
# token distinct from ``transition_task`` so that helper-doc fences
# referencing the transition_task function alone are still flagged while
# ``ctl.py transition`` without arguments is not a false positive.
_AC10_FENCE_SUBSTRINGS = ("ctl.py transition ", "transition_task(")

_AC10_EXEMPT_SKILL_DIRS = {
    "status",
    "resume",
    "calibration",
    "trajectory",
    "execute",
}

_AC10_CONTEXT_LINES = 20


def _ac10_fenced_blocks(lines: list[str]) -> list[tuple[int, int, str]]:
    """Return (start_line, end_line, body_text) for every fenced block.

    Line numbers are 1-indexed, inclusive of fence markers. body_text is
    the joined content between fences (fences excluded).
    """
    blocks: list[tuple[int, int, str]] = []
    in_fence = False
    fence_start = 0
    body: list[str] = []
    for i, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            if in_fence:
                blocks.append((fence_start, i, "\n".join(body)))
                in_fence = False
                body = []
            else:
                in_fence = True
                fence_start = i
                body = []
            continue
        if in_fence:
            body.append(line)
    return blocks


def _ac10_is_exempt(path: Path) -> bool:
    """AC9: five read-only helper skills are exempt.

    The exemption applies ONLY when ``path`` lives under one of those
    ``skills/<exempt>/`` dirs. CLI templates under
    ``cli/assets/templates/base/`` are NEVER exempt — a future task can
    re-evaluate, but for this lint we enforce the full set.
    """
    try:
        rel = path.relative_to(REPO_ROOT)
    except ValueError:
        return False
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == "skills" and parts[1] in _AC10_EXEMPT_SKILL_DIRS:
        return True
    return False


def test_no_skill_prose_makes_deterministic_path_optional():
    """AC10: no "If available in this repo" prose within 20 lines of a
    fenced block that forges a state-machine transition.

    Scans every path under ``SKILL_SOURCE_GLOBS``. Reports every
    co-occurrence as ``{path}:{line}`` where {line} is the prose line
    and also the fenced-block start line. Exempts the five read-only
    helper skills (AC9) — see ``_AC10_EXEMPT_SKILL_DIRS``.
    """
    failures: list[str] = []

    for md in _iter_skill_sources():
        if _ac10_is_exempt(md):
            continue
        text = md.read_text(encoding="utf-8")
        lines = text.splitlines()

        # 1) Find every prose line with the needle.
        prose_lines = [
            i + 1 for i, line in enumerate(lines)
            if _AC10_PROSE_NEEDLE in line
        ]
        if not prose_lines:
            continue

        # 2) For each fenced block containing a transition-forging
        # substring, record its start line.
        fences = _ac10_fenced_blocks(lines)
        bad_fences = [
            (start, end) for (start, end, body) in fences
            if any(sub in body for sub in _AC10_FENCE_SUBSTRINGS)
        ]
        if not bad_fences:
            continue

        # 3) Check proximity: prose within 20 lines (before OR after) of
        # ANY bad fence is a failure.
        relpath = md.relative_to(REPO_ROOT)
        for prose_line in prose_lines:
            for (fstart, fend) in bad_fences:
                # Measure the shortest distance from prose_line to the
                # fence span [fstart..fend] inclusive.
                if fstart <= prose_line <= fend:
                    distance = 0
                elif prose_line < fstart:
                    distance = fstart - prose_line
                else:  # prose_line > fend
                    distance = prose_line - fend
                if distance <= _AC10_CONTEXT_LINES:
                    failures.append(
                        f"{relpath}:{prose_line}: 'If available in this repo' "
                        f"within {distance} lines of transition-forging fence "
                        f"at {relpath}:{fstart}"
                    )

    assert not failures, (
        "AC10: transition-forging fences must not be guarded by "
        "'If available in this repo' prose:\n"
        + "\n".join(f"  - {f}" for f in failures)
    )
